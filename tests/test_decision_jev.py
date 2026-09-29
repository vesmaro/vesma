"""W4c — the external Jev adapter (ADR-0004 implementation (c)): offline tests.

Store card vesmaro-canon-w4c-jev-adapter. EVERYTHING here is offline by
construction: the HTTP transport is a scripted callable injected at
provider construction (``JevRouterProvider(transport=...)``) — the only
seam tests should ever need to mock. The binding live-probe facts
(OpenRouter exposes only ``typesafe/jev-router``; ``supported_parameters``
EMPTY → payload pins to exactly ``{"model", "messages"}``; the routed
``model`` recorded per response) are pinned as offline payload/response
shapes.

Honest coverage note: the DEFAULT transport's live-HTTP paths (2xx JSON
parse, 3xx-as-error, timeout) are NOT exercised offline — that leg is
covered by the recorded LIVE calibration run (canon repo,
``docs/experiments/provider-jev-calibration-w4c.md``); offline, the
default transport is pinned at its SSRF-guard boundary (loopback refused
before any connection).
"""

from __future__ import annotations

import json
import socket
from typing import Any, cast

import pytest
from pydantic import ValidationError

from vesmaro.config import MnemosConfig
from vesmaro.danger_detectors import PROMPT_INJECTION_PATTERNS
from vesmaro.decision_jev import (
    DEFAULT_JEV_KEY_ENV,
    JEV_ROUTER_MODEL,
    NO_FEDERATE_TAG,
    OPENROUTER_CHAT_COMPLETIONS_URL,
    YES_NO_SYSTEM_PROMPT,
    JevConfigError,
    JevPrivacyRefusalError,
    JevResponseError,
    JevRouterProvider,
    JevTransportError,
    _http_post_json,
    resolve_decision_provider,
)
from vesmaro.decision_provider import (
    QUESTION_IS_DUPLICATE,
    SPECTRUM_RECORD_QUALITY,
    CanonRecordView,
    CanonState,
    DecisionProvider,
    DeterministicProvider,
    GoalOverlapRequest,
    IsDuplicateRequest,
    MissingEvidenceError,
    Noul,
    RecordQualityRequest,
    Score,
    UnsupportedPrimitiveError,
    run_streaming_baseline,
)

from .test_decision_provider import load_corpus

#: Synthetic key shape — NEVER a real credential; must not leak into any
#: repr/config dump assertion below.
FAKE_KEY = "sk-or-unittest-FAKE-KEY-never-real"

#: Routed-upstream shapes from the TL live probe (2026-09-29): the router
#: picked ``openai/gpt-6-luna``; the second entry models a ROUTE CHANGE —
#: the re-calibration trigger the transparency contract hangs on.
ROUTED_A = "openai/gpt-6-luna"
ROUTED_B = "another/upstream-9"


def _openrouter_body(content: str, model: str) -> dict[str, Any]:
    """The OpenRouter chat-completion response shape (routed ``model`` in)."""
    return {
        "id": "gen-unittest",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }


class ScriptedTransport:
    """A transport that answers from a fixed script and records calls.

    Records ``(url, payload, key_present_flag)`` — deliberately NOT the
    key value, so no test artifact ever carries a credential-shaped
    string beyond :data:`FAKE_KEY`.
    """

    def __init__(
        self,
        script: list[str | Exception],
        models: list[str] | None = None,
    ) -> None:
        self._script = list(script)
        self._models = models or [ROUTED_A]
        self.calls: list[tuple[str, dict[str, Any], bool]] = []

    def __call__(self, url: str, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
        self.calls.append((url, payload, bool(api_key)))
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        model = self._models[min(len(self.calls) - 1, len(self._models) - 1)]
        return _openrouter_body(step, model)


def _provider(script: list[str | Exception], models: list[str] | None = None) -> JevRouterProvider:
    transport = ScriptedTransport(script, models)
    return JevRouterProvider(api_key=FAKE_KEY, transport=transport)


def _state(
    body: str = "checkpoint body about the release gate",
    title: str = "checkpoint",
    tags: tuple[str, ...] = (),
    candidate: CanonRecordView | None = None,
) -> CanonState:
    record = CanonRecordView(title=title, body=body, tags=tags)
    return CanonState(record=record, candidate=candidate)


# ── Provider semantics (mocked transport) ────────────────────────────────────


@pytest.mark.parametrize(("answer", "value"), [("yes", 1.0), ("no", 0.0)])
def test_record_quality_maps_strict_verdict_to_score(answer: str, value: float) -> None:
    provider = _provider([answer])
    decision = provider.evaluate(RecordQualityRequest(), _state())
    assert decision == Score(spectrum=SPECTRUM_RECORD_QUALITY, value=value, confidence=0.5)


@pytest.mark.parametrize(("answer", "prob"), [("yes", 1.0), ("no", 0.0)])
def test_is_duplicate_maps_strict_verdict_to_noul(answer: str, prob: float) -> None:
    candidate = CanonRecordView(title="prior", body="an earlier record")
    provider = _provider([answer])
    decision = provider.evaluate(IsDuplicateRequest(), _state(candidate=candidate))
    assert decision == Noul(question=QUESTION_IS_DUPLICATE, probability=prob)


def test_is_duplicate_without_candidate_refuses_before_network() -> None:
    transport = ScriptedTransport([])
    provider = JevRouterProvider(api_key=FAKE_KEY, transport=transport)
    with pytest.raises(MissingEvidenceError):
        provider.evaluate(IsDuplicateRequest(), _state())
    assert transport.calls == []  # pairwise without a candidate → zero probes


def test_goal_overlap_declined_until_preregistered() -> None:
    """Preregistration rule 5: no acceptance criterion for goal-overlap —
    the adapter declines instead of answering ahead of a criterion."""
    transport = ScriptedTransport([])
    provider = JevRouterProvider(api_key=FAKE_KEY, transport=transport)
    with pytest.raises(UnsupportedPrimitiveError, match="pre-registered"):
        provider.evaluate(
            GoalOverlapRequest(),
            _state(candidate=CanonRecordView(title="", body="neighbor goal")),
        )
    assert transport.calls == []


def test_unknown_request_declined() -> None:
    provider = _provider([])
    request = cast(Any, object())  # not a known request class
    with pytest.raises(UnsupportedPrimitiveError):
        provider.evaluate(request, _state())


# ── Probe wire format (live-probe facts pinned offline) ─────────────────────


def test_probe_payload_is_exactly_model_and_messages() -> None:
    """``supported_parameters`` is EMPTY (TL live probe): the payload pins
    to exactly ``{"model", "messages"}`` — no temperature, no
    response_format, nothing the router didn't declare."""
    transport = ScriptedTransport(["yes"])
    provider = JevRouterProvider(api_key=FAKE_KEY, transport=transport)
    provider.evaluate(RecordQualityRequest(), _state())
    assert len(transport.calls) == 1
    url, payload, key_present = transport.calls[0]
    assert url == OPENROUTER_CHAT_COMPLETIONS_URL
    assert set(payload) == {"model", "messages"}
    assert payload["model"] == JEV_ROUTER_MODEL
    assert payload["messages"][0] == {"role": "system", "content": YES_NO_SYSTEM_PROMPT}
    assert payload["messages"][1]["role"] == "user"
    assert key_present is True  # flag only — the value itself is never recorded


def test_routed_model_recorded_per_response() -> None:
    """The transparency contract: every completed response's routed
    ``model`` lands in ``routed_models`` — including a ROUTE CHANGE, the
    re-calibration trigger."""
    provider = _provider(["yes", "no"], models=[ROUTED_A, ROUTED_B])
    provider.evaluate(RecordQualityRequest(), _state())
    candidate = CanonRecordView(title="prior", body="earlier record")
    provider.evaluate(IsDuplicateRequest(), _state(candidate=candidate))
    assert provider.routed_models == [ROUTED_A, ROUTED_B]


@pytest.mark.parametrize("bad", ["maybe", "", "yes.", "yes no"])
def test_parse_failure_is_typed_not_fabricated(bad: str) -> None:
    """A verdict that is not exactly yes/no is a TYPED error — the provider
    never invents a probability from a malformed answer."""
    transport = ScriptedTransport([bad])
    provider = JevRouterProvider(api_key=FAKE_KEY, transport=transport)
    with pytest.raises(JevResponseError, match="strictly-formatted"):
        provider.evaluate(RecordQualityRequest(), _state())
    assert len(transport.calls) == 1  # no retry, no second guess


def test_case_normalization_is_limited_to_strip_and_lower() -> None:
    provider = _provider(["  YES\n"])
    decision = provider.evaluate(RecordQualityRequest(), _state())
    assert isinstance(decision, Score) and decision.value == 1.0


def test_missing_routed_model_is_a_typed_error() -> None:
    """Routed-model transparency is binding: a response without ``model``
    violates the contract → typed error, nothing recorded."""

    def model_less(url: str, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
        body = _openrouter_body("yes", ROUTED_A)
        del body["model"]
        return body

    provider = JevRouterProvider(api_key=FAKE_KEY, transport=model_less)
    with pytest.raises(JevResponseError, match="model"):
        provider.evaluate(RecordQualityRequest(), _state())
    assert provider.routed_models == []


def test_transport_failure_wraps_typed_error() -> None:
    provider = _provider([ConnectionError("network gone")])
    with pytest.raises(JevTransportError):
        provider.evaluate(RecordQualityRequest(), _state())


# ── Privacy gate (BEFORE any network — zero-call pins) ──────────────────────


def test_secret_in_record_refuses_with_zero_calls() -> None:
    token = "AKIA" + "T" * 16  # aws-key shape, synthetic
    provider = _provider([])
    with pytest.raises(JevPrivacyRefusalError) as excinfo:
        provider.evaluate(RecordQualityRequest(), _state(body=f"rotate the key {token} this week"))
    assert excinfo.value.reason == "secret-detected"
    assert "aws-key" in excinfo.value.detail  # pattern NAME is log-safe
    assert token not in excinfo.value.detail  # matched VALUE never surfaces
    assert provider.routed_models == []


def test_secret_in_candidate_refuses_with_zero_calls() -> None:
    token = "AKIA" + "T" * 16
    candidate = CanonRecordView(title="prior", body=f"key: {token}")
    provider = _provider([])
    with pytest.raises(JevPrivacyRefusalError) as excinfo:
        provider.evaluate(IsDuplicateRequest(), _state(candidate=candidate))
    assert excinfo.value.reason == "secret-detected"


def test_secret_hidden_in_tags_is_caught() -> None:
    """The gate scans the FULL wire surface (title + body + tags, the
    view's embedding-text bytes) — a secret cannot ride in a tag."""
    token = "AKIA" + "T" * 16
    provider = _provider([])
    with pytest.raises(JevPrivacyRefusalError, match="secret-detected"):
        provider.evaluate(RecordQualityRequest(), _state(tags=("kind:note", token)))


def test_danger_content_refuses_with_zero_calls() -> None:
    _, pattern = PROMPT_INJECTION_PATTERNS[0]
    provider = _provider([])
    with pytest.raises(JevPrivacyRefusalError) as excinfo:
        provider.evaluate(RecordQualityRequest(), _state(body=f"harmless {pattern} prose"))
    assert excinfo.value.reason == "danger-detected"
    assert provider.routed_models == []


@pytest.mark.parametrize("side", ["record", "candidate"])
def test_no_federate_tag_refuses_whole_call(side: str) -> None:
    """The exclusion tag on EITHER record keeps the call on this machine —
    same posture as the mesh/federation exclusion, enforced here too."""
    nofederate = CanonRecordView(title="t", body="b", tags=(NO_FEDERATE_TAG,))
    state = _state(tags=(NO_FEDERATE_TAG,)) if side == "record" else _state(candidate=nofederate)
    provider = _provider([])
    with pytest.raises(JevPrivacyRefusalError) as excinfo:
        provider.evaluate(RecordQualityRequest(), state)
    assert excinfo.value.reason == "no-federate-tag"
    assert provider.routed_models == []


# ── Default-off / config pins (zero-network guarantees) ─────────────────────


def test_config_defaults_pin_deterministic_and_env_name() -> None:
    config = MnemosConfig()
    assert config.decision_provider == "deterministic"  # default-off posture
    assert config.decision_jev_api_key_env == DEFAULT_JEV_KEY_ENV
    assert DEFAULT_JEV_KEY_ENV == "VESMARO_OPENROUTER_API_KEY"


def test_config_rejects_empty_env_name() -> None:
    with pytest.raises(ValidationError):
        MnemosConfig(decision_jev_api_key_env="")


def test_resolve_default_is_deterministic() -> None:
    provider = resolve_decision_provider(MnemosConfig())
    assert isinstance(provider, DeterministicProvider)
    assert isinstance(provider, DecisionProvider)  # the ADR-0004 protocol


def test_resolve_off_returns_none() -> None:
    assert resolve_decision_provider(MnemosConfig(decision_provider="off")) is None


def test_resolve_jev_without_key_fails_closed_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No flag + no key → ZERO network attempts, pinned mechanically: any
    socket construction (the precondition of every outbound leg) fails
    the test — and the factory must raise the typed config error first."""
    monkeypatch.delenv(DEFAULT_JEV_KEY_ENV, raising=False)

    def _no_sockets(*args: object, **kwargs: object) -> None:
        raise AssertionError("network attempt: socket constructed")

    monkeypatch.setattr(socket, "socket", _no_sockets)
    with pytest.raises(JevConfigError, match="VESMARO_OPENROUTER_API_KEY"):
        resolve_decision_provider(MnemosConfig(decision_provider="jev"))


def test_resolve_jev_reads_key_from_env_name_indirection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_name = "VESMARO_TEST_JEV_KEY"
    monkeypatch.setenv(env_name, FAKE_KEY)
    config = MnemosConfig(decision_provider="jev", decision_jev_api_key_env=env_name)
    provider = resolve_decision_provider(config)
    assert isinstance(provider, JevRouterProvider)
    assert isinstance(provider, DecisionProvider)
    assert FAKE_KEY not in repr(provider)  # the key never surfaces via repr


def test_key_value_never_enters_config_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    """Secrets hygiene pin: even with the env var present, the config's
    serialized form carries the env NAME only — the VALUE must not appear
    anywhere in ``model_dump_json``."""
    monkeypatch.setenv("VESMARO_TEST_JEV_KEY", FAKE_KEY)
    config = MnemosConfig(decision_provider="jev", decision_jev_api_key_env="VESMARO_TEST_JEV_KEY")
    dump = json.dumps(json.loads(config.model_dump_json()))
    assert FAKE_KEY not in dump
    assert "VESMARO_TEST_JEV_KEY" in dump


# ── Default transport: pinned at its SSRF-guard boundary (offline) ──────────


def test_default_transport_validates_url_before_connecting() -> None:
    """The engine's outbound posture: the SSRF guard rejects a loopback
    endpoint BEFORE any connection is attempted (offline-provable leg of
    the default transport; the live-HTTP paths are covered by the
    recorded LIVE calibration run, see the module docstring)."""
    with pytest.raises(ValueError, match="SSRF"):
        _http_post_json("http://127.0.0.1:1/v1/chat/completions", {}, FAKE_KEY)


# ── Runner compatibility (the calibration contract, offline) ────────────────


def test_streaming_baseline_consumes_the_jev_provider() -> None:
    """Like-for-like by construction: the shared W4b runner drives the Jev
    provider unchanged — 21 quality probes + 20 duplicate probes, every
    HTTP response's routed model recorded (41 transparency entries)."""
    corpus = load_corpus()
    # Call order under the streaming policy: quality for record 1, then
    # (quality, duplicate) for every record with a prior — 41 probes total.
    transport = ScriptedTransport(["no"] + ["no", "yes"] * 20)
    provider = JevRouterProvider(api_key=FAKE_KEY, transport=transport)

    def flat_cosine(a: CanonRecordView, b: CanonRecordView) -> float:
        return 0.99  # offline leg — the real cosine pin lives in the W4b tests

    verdicts = run_streaming_baseline(corpus, provider, flat_cosine)
    assert len(verdicts) == 21
    assert verdicts[0].is_duplicate is None  # first record: not askable
    assert all(
        v.is_duplicate is not None and v.is_duplicate.probability == 1.0 for v in verdicts[1:]
    )
    assert all(v.record_quality.value == 0.0 for v in verdicts)
    assert len(transport.calls) == 41
    assert len(provider.routed_models) == 41
