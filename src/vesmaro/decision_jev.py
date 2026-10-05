"""ADR-0004 implementation (c) — the external Jev adapter (opt-in, default-off).

The SECOND implementation of the interface ratified in vesma-canon
[ADR 0004](https://github.com/vesmaro/vesma-canon/blob/main/docs/decisions/0004-decision-provider.md):
an outbound LLM leg over OpenRouter's ``typesafe/jev-router``. The seam
contract (:mod:`vesmaro.decision_provider`) is unchanged — prepared
canon state in, typed decision out — and so are its rules:

1. **Policy lives in product code** — the provider answers the question
   it is given; it never invents what to ask.
2. **Prepared state only** — :class:`CanonRecordView` (envelope + body +
   title + tags) is the entire input surface. Store-only fields never
   cross.
3. **Privacy gate is universal** — here it actually binds: the gate
   (secret scan + danger detectors + ``mnemos:no-federate`` tag) runs
   over the prepared state BEFORE any network I/O and aborts the WHOLE
   call on any finding. Refusal is typed, never negotiated per call
   site.
4. **Calibration before trust** — the adapter is measured against the
   deterministic baseline under the pre-registered methodology (canon
   repo, ``docs/experiments/provider-calibration.md``); it does not
   participate in product decisions until that gate passes.

── Jev-router live contract (TL probe, 2026-09-29 — binding) ─────────

OpenRouter exposes ONLY ``typesafe/jev-router`` (the native
``typesafe/jev-latest`` ids are «not a valid model ID»); the router
itself runs on Jev and picks the upstream per request, and its
``supported_parameters`` list is EMPTY. Consequently this adapter sends
exactly ``{"model", "messages"}`` — no temperature, no response_format,
nothing else — and every answer travels as a strictly-formatted yes/no
probe whose parse failure is a TYPED error
(:class:`JevResponseError`), never a fabricated probability.

Because the router picks the upstream, the response's ``model`` field
is the ROUTED model, and the adapter records it per response
(:attr:`JevRouterProvider.routed_models`, one entry per completed HTTP
response including ones whose verdict failed the strict parse). A
change in the routed-models set is a RE-CALIBRATION TRIGGER (canon
preregistration addendum, 2026-09-29) — the calibration runner reports
the set alongside the metrics.

Default-off, enforced twice: the config default is
``mnemos.decision_provider="deterministic"`` (zero I/O), and even with
``"jev"`` the factory refuses the activation BEFORE any network attempt
when the key env var is missing — with a machine-parseable
``code=JEV-E-CONFIG class=provider-class`` warn (§4 -CONFIG: the config
is rejected, the active implementation stays ``deterministic``). The key
travels by env-NAME indirection
(``decision_jev_api_key_env``, default ``VESMA_OPENROUTER_API_KEY``; the
deprecated ``VESMARO_OPENROUTER_API_KEY`` stays honoured until 6.0)
— the secret itself never enters config files, git or logs.

W5 conformance additions (contract 1.0.0-draft.2, review-2): every
privacy refusal and scanner failure warns with ``class=call-class``
BEFORE any network I/O (§3.5, §4 GATE row); the gate is scanner
fail-closed — an exception or wall-clock timeout inside
``detect_secrets``/``danger_detect`` aborts the whole call
(:class:`JevScannerError`); and the wiring hands out
:class:`JevFailOpenProvider`, so a probe failure degrades THAT verdict
to the deterministic rule (``class=verdict-class``) instead of blocking
the product path (§3.8). Every answered request carries a §3.9
telemetry line (primitive / implementation / confidence).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as ScanTimeout
from functools import partial
from typing import Any, Final, TypeVar

from vesmaro.config import VesmaConfig
from vesmaro.danger_detectors import detect as danger_detect
from vesmaro.decision_provider import (
    ACTION_CLASS_CALL,
    ACTION_CLASS_PROVIDER,
    ACTION_CLASS_VERDICT,
    QUESTION_IS_DUPLICATE,
    SPECTRUM_RECORD_QUALITY,
    CanonRecordView,
    CanonState,
    CortexError,
    CortexPinError,
    DecisionPrimitive,
    DecisionProvider,
    DecisionRequest,
    DeterministicProvider,
    GoalOverlapRequest,
    IsDuplicateRequest,
    MissingEvidenceError,
    Noul,
    RecordQualityRequest,
    Score,
    UnsupportedPrimitiveError,
    VesmaProvider,
    log_decision_telemetry,
)
from vesmaro.http_guard import validate_url_ssrf
from vesmaro.secrets_detector import detect_secrets, findings_by_pattern

logger = logging.getLogger(__name__)

_ScanResult = TypeVar("_ScanResult")

#: The only model id OpenRouter accepts for the Jev router (TL live
#: probe 2026-09-29; the native ids are rejected as invalid).
JEV_ROUTER_MODEL: Final[str] = "typesafe/jev-router"

#: OpenRouter's chat-completions endpoint (the adapter's one outbound URL).
OPENROUTER_CHAT_COMPLETIONS_URL: Final[str] = "https://openrouter.ai/api/v1/chat/completions"

#: The exclusion tag (mesh/federation posture) — a record carrying it in
#: its prepared-state tags NEVER leaves the machine through this adapter.
NO_FEDERATE_TAG: Final[str] = "mnemos:no-federate"

#: Default env-NAME indirection for the OpenRouter key (config may point
#: at another name; the value itself is read from the environment only).
#: Rebrand train 5.3.0: canonical name carries the ``VESMA_`` prefix; the
#: deprecated ``VESMARO_OPENROUTER_API_KEY`` twin stays honoured when the
#: canonical name is unset (dual period until 6.0, ADR-0031 pattern).
DEFAULT_JEV_KEY_ENV: Final[str] = "VESMA_OPENROUTER_API_KEY"

#: Deprecated pre-5.3 name of the OpenRouter key env var — read ONLY as a
#: fallback when :data:`DEFAULT_JEV_KEY_ENV` (or a config pointing at it)
#: resolves to an unset variable.
_DEPRECATED_JEV_KEY_ENV: Final[str] = "VESMARO_OPENROUTER_API_KEY"

#: Outbound HTTP timeout for one probe (matches the ingest leg's 30s).
JEV_HTTP_TIMEOUT_SECONDS: Final[float] = 30.0

#: Wall-clock guard for ONE privacy-gate detector pass (spec §3.5
#: fail-closed, review-2: a scanner TIMEOUT aborts the call exactly like
#: any scanner failure). The detectors are synchronous pure-python
#: scanners — a catastrophic-backtracking hang is the realistic timeout
#: case. Read at call time (monkeypatchable in tests).
JEV_SCAN_TIMEOUT_SECONDS: Final[float] = 10.0


def _scan_fail_closed(description: str, scan: Callable[[], _ScanResult]) -> _ScanResult:
    """Run one detector pass under the §3.5 fail-closed contract.

    A scanner EXCEPTION or a wall-clock TIMEOUT is a scanner failure:
    the pass cannot prove the state safe, so the whole external call is
    aborted — typed :class:`JevScannerError` plus a
    ``code=JEV-E-SCANNER class=call-class`` warn; continuing an outbound
    call with an unscanned state is forbidden. The pass runs on a worker
    thread so a hung detector cannot hang the gate; CPython cannot kill
    the abandoned worker (it is joined at interpreter exit), but the
    CALL still aborts deterministically — that is the guaranteed part.
    """
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jev-privacy-scan")
    try:
        future = executor.submit(scan)
        return future.result(timeout=JEV_SCAN_TIMEOUT_SECONDS)
    except ScanTimeout as exc:
        logger.warning(
            "code=JEV-E-SCANNER class=%s privacy-gate scanner timed out after %.1fs (%s) — "
            "aborting the external call",
            ACTION_CLASS_CALL,
            JEV_SCAN_TIMEOUT_SECONDS,
            description,
        )
        raise JevScannerError(f"{description} timed out after {JEV_SCAN_TIMEOUT_SECONDS}s") from exc
    except Exception as exc:
        logger.warning(
            "code=JEV-E-SCANNER class=%s privacy-gate scanner failed (%s): %s — aborting "
            "the external call",
            ACTION_CLASS_CALL,
            description,
            type(exc).__name__,
        )
        raise JevScannerError(f"{description} failed: {type(exc).__name__}") from exc
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


#: Honest confidence for the adapter's Score answers: the maximum-entropy
#: level. The adapter's calibration exists for the is-duplicate Brier
#: gate; its record-quality CONFIDENCE is uncalibrated (NO-DATA per the
#: preregistration), and claiming more than 0.5 would be decoration —
#: the same honesty rule the deterministic placeholder follows.
JEV_SCORE_CONFIDENCE: Final[float] = 0.5

#: The strictly-formatted probe contract: one word, lowercase, nothing
#: else. Anything else is a :class:`JevResponseError` — parse failure is
#: never smoothed over into a guess.
YES_NO_SYSTEM_PROMPT: Final[str] = (
    "You are answering a single screening question for a record-keeping "
    "pipeline. Reply with exactly one word — yes or no. No punctuation, "
    "no explanation, no other text."
)

#: Transport seam: ``(url, json_payload, api_key) -> parsed JSON object``.
#: The default is :func:`_http_post_json` (SSRF-guarded httpx); tests
#: inject a scripted callable — the ONLY thing they should need to mock.
JevTransport = Callable[[str, dict[str, Any], str], dict[str, Any]]

# ── Typed errors (explicit refusals — the adapter never fabricates) ──────────


class JevConfigError(ValueError):
    """``decision_provider=jev`` requested but the setup is incomplete.

    Raised at construction/wiring time — BEFORE any network attempt —
    when the key env var named by ``decision_jev_api_key_env`` is
    missing or empty.
    """


class JevPrivacyRefusalError(ValueError):
    """The privacy gate fired BEFORE any network I/O; the call is aborted whole.

    ``reason`` is one of ``"secret-detected"`` / ``"danger-detected"`` /
    ``"no-federate-tag"``; ``detail`` carries pattern NAMES and counts
    only (both detectors build their findings log-safe by construction —
    matched secret values never appear here).
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"jev privacy gate refused ({reason}): {detail}")
        self.reason = reason
        self.detail = detail


class JevTransportError(RuntimeError):
    """The HTTP leg failed (network error, non-2xx status, redirect).

    No decision is fabricated on a transport failure — the caller gets
    the typed error and retries are its policy.
    """


class JevScannerError(RuntimeError):
    """The privacy-gate scanner itself failed (exception or timeout).

    Fail-closed (spec §3.5, review-2): a broken or hanging scanner
    cannot prove the state safe — the WHOLE call is aborted before any
    network I/O, never resumed with an unscanned payload. The
    accompanying warn carries ``class=call-class``.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(f"jev privacy-gate scanner failed: {detail}")
        self.detail = detail


class JevResponseError(ValueError):
    """The response violated the strictly-formatted probe contract.

    Covers: non-JSON body, missing/empty choices, missing routed
    ``model`` (routed-model transparency is binding), and a verdict that
    is not exactly ``yes``/``no``. A parse failure is a typed error,
    NEVER a fabricated probability.
    """


# ── The default transport (the engine's outbound-HTTP posture) ───────────────


def _http_post_json(url: str, payload: dict[str, Any], api_key: str) -> dict[str, Any]:
    """One SSRF-guarded POST to the OpenRouter endpoint.

    Engine posture (``MemoryManager.ingest_url`` is the precedent): the
    URL passes :func:`vesmaro.http_guard.validate_url_ssrf` BEFORE any
    connection, and redirects are NEVER followed — ``follow_redirects=False``
    plus «any non-2xx (3xx included) is a transport error». That is
    stricter than the ingest leg's per-hop re-validation and correct
    here: a fixed API endpoint has no business redirecting, so no hop
    escapes the guard by construction. The api key rides the
    ``Authorization`` header only — it never enters payloads, logs or
    error messages.
    """
    validate_url_ssrf(url)
    import httpx

    try:
        with httpx.Client(follow_redirects=False) as client:
            resp = client.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=JEV_HTTP_TIMEOUT_SECONDS,
            )
    except httpx.HTTPError as exc:
        raise JevTransportError(f"jev transport error: {type(exc).__name__}") from exc
    if not resp.is_success:
        # 3xx lands here too (follow_redirects=False): a redirecting API
        # endpoint is a transport anomaly, not something to chase.
        raise JevTransportError(f"jev endpoint returned HTTP {resp.status_code}")
    body: Any = resp.json()
    if not isinstance(body, dict):
        raise JevResponseError("jev endpoint returned a non-object JSON body")
    return body


# ── Response parsing (strictly-formatted probe) ──────────────────────────────


def _parse_probe_response(response: dict[str, Any]) -> tuple[str, str]:
    """Extract ``(normalized_verdict, routed_model)`` from a response body.

    Typed errors on every contract violation; the routed ``model`` is
    REQUIRED — recording it per response is the transparency contract
    the re-calibration trigger hangs on.
    """
    model = response.get("model")
    if not isinstance(model, str) or not model:
        raise JevResponseError("response carries no routed 'model' — transparency is binding")
    choices = response.get("choices")
    if not isinstance(choices, list) or not choices:
        raise JevResponseError("response has no choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise JevResponseError("response choices[0] is not an object")
    message = first.get("message")
    if not isinstance(message, dict):
        raise JevResponseError("response choices[0].message is not an object")
    content = message.get("content")
    if not isinstance(content, str):
        raise JevResponseError("response message content is not a string")
    return content.strip().lower(), model


# ── The provider ─────────────────────────────────────────────────────────────


class JevRouterProvider:
    """The external Jev leg behind the ADR-0004 interface.

    Answers the seam's primitives with strictly-formatted yes/no probes:

    * «record-quality» → yes→1.0 / no→0.0 on the quality question
      (:class:`Score`, confidence :data:`JEV_SCORE_CONFIDENCE`);
    * «is-duplicate» → yes→1.0 / no→0.0 on the near-duplicate question
      (:class:`Noul`; the request's cosine ``threshold`` is product
      policy for the deterministic leg — the semantic leg answers the
      question, it does not re-implement the engine's step function);
    * «goal-overlap» → typed refusal: its acceptance criterion is
      deliberately NOT pre-registered yet (preregistration rule 5), and
      answering ahead of a criterion would be calibration-before-trust
      in reverse.

    Every ``evaluate`` runs the privacy gate BEFORE any network I/O.
    Every completed HTTP response appends its routed model to
    :attr:`routed_models` (a change in that set is a re-calibration
    trigger — the calibration runner reports the set).
    """

    name: Final[str] = "jev-router"

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = OPENROUTER_CHAT_COMPLETIONS_URL,
        router_model: str = JEV_ROUTER_MODEL,
        transport: JevTransport | None = None,
    ) -> None:
        if not api_key:
            raise JevConfigError(
                "JevRouterProvider requires a non-empty api_key (env-NAME "
                "indirection: resolve it via resolve_decision_provider)"
            )
        self._api_key = api_key
        self._endpoint = endpoint
        self._router_model = router_model
        self._transport: JevTransport = transport if transport is not None else _http_post_json
        #: Routed upstream model per completed HTTP response, in call
        #: order (binding transparency contract). ONE SET CHANGE ANYWHERE
        #: = re-calibration trigger (canon preregistration addendum).
        self.routed_models: list[str] = []

    # ── DecisionProvider protocol ────────────────────────────────────────

    def evaluate(self, request: DecisionRequest, state: CanonState) -> DecisionPrimitive:
        """Answer one product-fixed question over the prepared state."""
        self._privacy_gate(state)
        answer: DecisionPrimitive
        if isinstance(request, RecordQualityRequest):
            quality = self._ask(
                "record-quality",
                "Record:\n"
                f"{self._render(state.record)}\n\n"
                "Question: is this a high-quality canon record?",
            )
            answer = Score(
                spectrum=SPECTRUM_RECORD_QUALITY,
                value=1.0 if quality else 0.0,
                confidence=JEV_SCORE_CONFIDENCE,
            )
        elif isinstance(request, IsDuplicateRequest):
            if state.candidate is None:
                raise MissingEvidenceError(
                    "is-duplicate is pairwise: state.candidate (the prior record) is required"
                )
            duplicate = self._ask(
                "is-duplicate",
                "Record A:\n"
                f"{self._render(state.record)}\n\n"
                "Record B:\n"
                f"{self._render(state.candidate)}\n\n"
                "Question: is Record B a near-duplicate of Record A?",
            )
            answer = Noul(question=QUESTION_IS_DUPLICATE, probability=1.0 if duplicate else 0.0)
        elif isinstance(request, GoalOverlapRequest):
            raise UnsupportedPrimitiveError(
                "goal-overlap has no pre-registered acceptance criterion yet "
                "(canon preregistration rule 5) — the jev adapter declines it"
            )
        else:
            raise UnsupportedPrimitiveError(f"no jev heuristic for {type(request).__name__}")
        log_decision_telemetry(self.name, answer)
        return answer

    # ── Internals ────────────────────────────────────────────────────────

    @staticmethod
    def _render(view: CanonRecordView) -> str:
        """The exact prepared-state bytes a probe carries (the view's own
        embedding-text surface: title + body + tags, 4096 cap) — what the
        privacy gate scanned is byte-identical to what goes on the wire."""
        return view.embedding_text()

    def _ask(self, kind: str, question: str) -> bool:
        """One probe round-trip; returns the strict yes/no verdict."""
        payload: dict[str, Any] = {
            "model": self._router_model,
            "messages": [
                {"role": "system", "content": YES_NO_SYSTEM_PROMPT},
                {"role": "user", "content": question},
            ],
        }
        try:
            response = self._transport(self._endpoint, payload, self._api_key)
        except (JevTransportError, JevResponseError):
            # Failure-context line only (code=, no class=): the verdict's
            # DEGRADATION is recorded by the wiring wrapper
            # (:class:`JevFailOpenProvider`) — one degradation, one
            # class-carrying line (spec §3.8).
            logger.warning("code=JEV-E-INFER jev decision failed: question=%s", kind)
            raise
        except Exception as exc:
            logger.warning("code=JEV-E-INFER jev decision failed: question=%s", kind)
            raise JevTransportError(f"jev transport raised {type(exc).__name__}") from exc
        verdict, routed = _parse_probe_response(response)
        self.routed_models.append(routed)
        if verdict == "yes":
            logger.info("jev decision ok: question=%s routed_model=%s", kind, routed)
            return True
        if verdict == "no":
            logger.info("jev decision ok: question=%s routed_model=%s", kind, routed)
            return False
        logger.warning(
            "code=JEV-E-INFER jev decision failed: question=%s routed_model=%s reason=strict-parse",
            kind,
            routed,
        )
        raise JevResponseError(
            f"jev verdict violated the strictly-formatted probe contract: "
            f"expected 'yes'/'no', routed_model={routed}"
        )

    @staticmethod
    def _privacy_gate(state: CanonState) -> None:
        """Scan the prepared state BEFORE any network I/O (ADR-0004 rule 3).

        Draft.2 review-2 order: the ``no-federate`` tag check runs FIRST
        on every view, before any scan (assembly-side exclusion is the
        first line of the gate — :meth:`CanonRecordView.from_memory`
        refuses tagged rows; this per-view check is the second), and the
        request message is assembled only after the gate passed. The
        gate is fail-closed over BOTH records (subject and candidate):
        a ``mnemos:no-federate`` tag, a secret finding, a danger-detector
        positive or a SCANNER FAILURE (exception or timeout — a broken
        scanner cannot prove the state safe, and the danger detector
        reports its internal errors in the result, never as exceptions)
        aborts the WHOLE call with a typed refusal and a
        ``code=JEV-E-GATE``/``code=JEV-E-SCANNER`` warn carrying
        ``class=call-class`` — zero network attempts. Findings are
        reported by pattern names and counts only; matched values never
        leave the process.
        """
        views: list[tuple[str, CanonRecordView]] = [("record", state.record)]
        if state.candidate is not None:
            views.append(("candidate", state.candidate))
        for label, view in views:
            if NO_FEDERATE_TAG in view.tags:
                logger.warning(
                    "code=JEV-E-GATE class=%s privacy refusal before any network I/O (%s "
                    "carries %s)",
                    ACTION_CLASS_CALL,
                    label,
                    NO_FEDERATE_TAG,
                )
                raise JevPrivacyRefusalError(
                    "no-federate-tag", f"{label} carries {NO_FEDERATE_TAG}"
                )
            text = view.embedding_text()
            secret_findings = _scan_fail_closed(f"{label}:secrets", partial(detect_secrets, text))
            if secret_findings:
                summary = findings_by_pattern(secret_findings)
                logger.warning(
                    "code=JEV-E-GATE class=%s privacy refusal before any network I/O (%s: %s)",
                    ACTION_CLASS_CALL,
                    label,
                    summary,
                )
                raise JevPrivacyRefusalError("secret-detected", f"{label}: {summary}")
            danger = _scan_fail_closed(
                f"{label}:danger",
                partial(danger_detect, view.body, title=view.title),
            )
            if danger.error is not None:
                # The danger detector converts its own internal failures
                # into ``error`` (never exceptions) — that IS the
                # scanner-failure signal; refuse fail-closed (§3.5).
                logger.warning(
                    "code=JEV-E-SCANNER class=%s privacy-gate danger scanner failed (%s: %s) "
                    "— aborting the external call",
                    ACTION_CLASS_CALL,
                    label,
                    danger.error,
                )
                raise JevScannerError(f"{label}:danger failed: {danger.error}")
            if not danger.clean:
                by_class = danger.patterns_by_class()
                logger.warning(
                    "code=JEV-E-GATE class=%s privacy refusal before any network I/O (%s: %s)",
                    ACTION_CLASS_CALL,
                    label,
                    by_class,
                )
                raise JevPrivacyRefusalError("danger-detected", f"{label}: {by_class}")


# ── The seam factory (the single wiring point for future call sites) ─────────


class JevFailOpenProvider:
    """The §3.8 fail-open wiring layer around the wired external adapter.

    The adapter answers honestly and raises typed errors — the
    DEGRADATION decision belongs to the wiring (spec §3.8: ANY provider
    error degrades to baseline; the product path is never blocked).
    Per verdict: a transport/inference failure degrades THIS answer to
    the deterministic rule with a machine-parseable
    ``code=JEV-E-INFER class=verdict-class`` warn. A privacy refusal
    (``JEV-E-GATE``) or scanner failure (``JEV-E-SCANNER``) has already
    been warned ``class=call-class`` by the gate — the verdict degrades
    to the deterministic rule WITHOUT a second warn (one event, one
    line; §4 GATE row). Engine-wide typed refusals
    (:class:`MissingEvidenceError`, :class:`UnsupportedPrimitiveError`)
    pass through: they are unanswerable for EVERY provider, including
    the baseline (documented reconciliation, spec §10 style).

    Calibration honesty: the calibration runner drives the RAW
    :class:`JevRouterProvider` (never this wrapper), so a measured
    failure stays a loud measurement event; the wrapper guards only the
    product wiring handed out by :func:`resolve_decision_provider`.
    """

    name: Final[str] = "jev-router"

    def __init__(self, inner: JevRouterProvider) -> None:
        self._inner = inner
        self._baseline = DeterministicProvider()

    @property
    def routed_models(self) -> list[str]:
        """Transparency pass-through (the routed-model re-calibration trigger)."""
        return self._inner.routed_models

    def evaluate(self, request: DecisionRequest, state: CanonState) -> DecisionPrimitive:
        """One probe with per-verdict fail-open to the deterministic rule.

        The degraded answer's §3.9 telemetry is emitted by the baseline
        provider itself — attributed to ``deterministic``, the
        implementation that ACTUALLY answered (§4 INFER row).
        """
        try:
            return self._inner.evaluate(request, state)
        except (MissingEvidenceError, UnsupportedPrimitiveError):
            raise
        except (JevPrivacyRefusalError, JevScannerError):
            # The gate already warned ``class=call-class`` — degrade quietly.
            return self._baseline.evaluate(request, state)
        except Exception as exc:  # fail-open boundary (§3.8): degrade, never block
            logger.warning(
                "code=JEV-E-INFER class=%s jev verdict degraded to the deterministic rule: %s",
                ACTION_CLASS_VERDICT,
                exc,
            )
            return self._baseline.evaluate(request, state)


def resolve_decision_provider(
    settings: VesmaConfig,
    *,
    embedder_fingerprint: str | None = None,
) -> DecisionProvider | None:
    """Build the configured ADR-0004 provider (or ``None`` when off).

    * ``off`` → ``None`` — the seam is disabled entirely.
    * ``deterministic`` (default) → the local baseline provider: zero
      I/O, so the default posture «no flag, no key, no network attempt»
      holds by construction.
    * ``vesma`` → :class:`~vesmaro.decision_provider.VesmaProvider`, the
      bundled vesma-cortex-v1 artifact (W5d). FAIL-OPEN (inference-v1.md
      §7): the caller must supply the LIVE embedder fingerprint via
      ``embedder_fingerprint`` (from ``Settings.embedding``, e.g.
      ``config_fingerprint(settings.embedding)``) so the artifact's
      ``embedder_pin`` can be asserted; a missing fingerprint is an
      unassertable pin — refused loudly, degraded to deterministic. Any
      :class:`~vesmaro.decision_provider.CortexError` degrades to the
      deterministic provider with a machine-parseable ``code=`` warn —
      the engine path is never blocked. A ``CORTEX-E-PIN`` failure is
      additionally telegraphed as a RECALIBRATION EVENT (not routine
      degradation): the artifact cannot silently run on another
      embedding geometry.
    * ``jev`` → :class:`JevFailOpenProvider` around
      :class:`JevRouterProvider`; the key is read at wiring time from the
      env variable NAMED by ``decision_jev_api_key_env``. Missing/empty
      env → a ``code=JEV-E-CONFIG class=provider-class`` warn and the
      deterministic provider (§4 -CONFIG: config rejected, active
      implementation stays deterministic, zero network attempts). The
      secret never enters config files, git or logs.

    Every non-deterministic answer travels with §3.9 telemetry; the Jev
    wiring is fail-open per verdict (§3.8) via :class:`JevFailOpenProvider`.
    """
    mode = settings.decision_provider
    if mode == "off":
        return None
    if mode == "deterministic":
        return DeterministicProvider()
    if mode == "vesma":
        if not embedder_fingerprint:
            logger.warning(
                "code=CORTEX-E-PIN class=%s decision_provider=vesma without a live embedder "
                "fingerprint — pin unassertable (recalibration-class refusal), "
                "degrading to deterministic",
                ACTION_CLASS_PROVIDER,
            )
            return DeterministicProvider()
        try:
            return VesmaProvider(embedder_fingerprint=embedder_fingerprint)
        except CortexPinError as exc:
            logger.warning(
                "code=%s class=%s cortex recalibration event: %s — degrading to deterministic "
                "(re-calibrate before re-enabling decision_provider=vesma)",
                exc.code,
                ACTION_CLASS_PROVIDER,
                exc,
            )
            return DeterministicProvider()
        except CortexError as exc:
            logger.warning(
                "code=%s class=%s decision_provider=vesma failed to load, degrading to "
                "deterministic: %s",
                exc.code,
                ACTION_CLASS_PROVIDER,
                exc,
            )
            return DeterministicProvider()
    key_env = settings.decision_jev_api_key_env
    key = os.environ.get(key_env, "")
    if not key and key_env == DEFAULT_JEV_KEY_ENV:
        # Deprecated twin fallback (VESMARO_ retires no earlier than 6.0):
        # deployments that exported only the legacy name keep working, with
        # a visible warning instead of a silent swap.
        key = os.environ.get(_DEPRECATED_JEV_KEY_ENV, "")
        if key:
            logger.warning(
                "code=DEPRECATED-ENV %s is unset; using deprecated %s (accepted until 6.0)",
                DEFAULT_JEV_KEY_ENV,
                _DEPRECATED_JEV_KEY_ENV,
            )
    if not key:
        hint = (
            f" (deprecated ${_DEPRECATED_JEV_KEY_ENV} also accepted until 6.0)"
            if key_env == DEFAULT_JEV_KEY_ENV
            else ""
        )
        # §4 -CONFIG row: the activation is refused (flag WITHOUT a key),
        # the ACTIVE implementation stays deterministic, and the refusal
        # is logged machine-parseably with the contractual action class.
        logger.warning(
            "code=JEV-E-CONFIG class=%s decision_provider=jev refused: requires a non-empty "
            "$%s%s (env-NAME indirection — the key itself never enters config); the active "
            "implementation stays deterministic",
            ACTION_CLASS_PROVIDER,
            key_env,
            hint,
        )
        return DeterministicProvider()
    logger.info("jev adapter wired: key_env=%s", key_env)
    return JevFailOpenProvider(JevRouterProvider(api_key=key))
