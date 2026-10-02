"""W3-v1 agent token scheme tests (ADR-0018 + ADR-0018-T §3/§5).

Coverage contract (every TM-mandated property has a test):

* envelope round-trip + tamper evidence (signature);
* lifetime gate (expired / not-yet-valid);
* node binding (``aud`` mismatch);
* denylist: revoke by jti, revoke-all by agent;
* rotation grace window: old+new both valid INSIDE the window, old fails
  closed after it;
* fail-closed unknown jti;
* signing-key lifecycle: minted on first use, mode 0600, stable reload;
* TTL ceiling (24 h) and scope grammar;
* CLI end-to-end: issue → validate → rotate → revoke → invalid, with the
  plaintext appearing exactly once (issue) and never in ``list``.
"""

from __future__ import annotations

import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from typer.testing import CliRunner

from vesmaro.agent_tokens import (
    MAX_TTL_HOURS,
    ROTATE_GRACE_MINUTES,
    AgentTokenClaims,
    AgentTokenError,
    AgentTokenStore,
    ScopeError,
    decode_agent_token,
    encode_agent_token,
    issue_agent_token,
    load_or_create_signing_key,
    parse_scope,
    rotate_agent_token,
    scope_allows_write,
    signing_key_path,
    validate_agent_token,
)
from vesmaro.cli.main import app as cli_app

runner = CliRunner()

T0 = 1_800_000_000  # fixed epoch base — deterministic iat/exp in tests
NODE = "mesh-node-alpha"
AGENT = "harness-zcode"


@pytest.fixture()
def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture()
def store(tmp_path: Path) -> AgentTokenStore:
    st = AgentTokenStore(tmp_path / "test.db")
    yield st
    st.close()


def _issue(
    store: AgentTokenStore,
    key: Ed25519PrivateKey,
    *,
    agent: str = AGENT,
    node: str = NODE,
    scope: str = "read",
    ttl_hours: float = 8.0,
    now: int = T0,
) -> tuple[str, AgentTokenClaims]:
    return issue_agent_token(
        agent_id=agent,
        node_id=node,
        scope_spec=scope,
        ttl_hours=ttl_hours,
        store=store,
        key=key,
        now=now,
    )


# ── envelope ──────────────────────────────────────────────────────────────────


def test_round_trip_claims_survive(store: AgentTokenStore, key: Ed25519PrivateKey) -> None:
    token, claims = _issue(store, key, scope="rw,project:foo")
    decoded = decode_agent_token(token, key)
    assert decoded == claims
    assert decoded.scope == ["rw", "project:foo"]
    # three dot-separated base64url segments (TM §3 envelope shape)
    assert len(token.split(".")) == 3


def test_tampered_signature_rejected(store: AgentTokenStore, key: Ed25519PrivateKey) -> None:
    token, _ = _issue(store, key)
    h, p, s = token.split(".")
    flipped = ("A" if not s.startswith("A") else "B") + s[1:]
    verdict = validate_agent_token(f"{h}.{p}.{flipped}", NODE, store=store, key=key, now=T0 + 1)
    assert not verdict.valid
    assert verdict.reason == "bad_signature"


def test_tampered_payload_rejected(store: AgentTokenStore, key: Ed25519PrivateKey) -> None:
    import base64
    import json

    token, _ = _issue(store, key)
    h, p, s = token.split(".")
    payload = json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))
    payload["sub"] = "harness-other"
    forged = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    verdict = validate_agent_token(f"{h}.{forged}.{s}", NODE, store=store, key=key, now=T0 + 1)
    assert not verdict.valid
    assert verdict.reason == "bad_signature"  # agent_id spoof via payload fails (T3)


def test_malformed_envelope_rejected(store: AgentTokenStore, key: Ed25519PrivateKey) -> None:
    for junk in ("", "abc", "a.b", "a.b.c.d", "!!!.???.***"):
        verdict = validate_agent_token(junk, NODE, store=store, key=key, now=T0)
        assert not verdict.valid
        assert verdict.reason in ("malformed", "unsupported_version")


def test_unsupported_version_rejected(store: AgentTokenStore, key: Ed25519PrivateKey) -> None:
    claims = AgentTokenClaims(
        iss="mnemos",
        sub=AGENT,
        aud=NODE,
        scope=["read"],
        iat=T0,
        exp=T0 + 3600,
        jti=uuid.uuid4().hex,
        v=2,
    )
    token = encode_agent_token(claims, key)
    verdict = validate_agent_token(token, NODE, store=store, key=key, now=T0 + 1)
    assert not verdict.valid
    assert verdict.reason == "unsupported_version"


# ── lifetime + node binding ───────────────────────────────────────────────────


def test_expired_token_rejected(store: AgentTokenStore, key: Ed25519PrivateKey) -> None:
    token, _ = _issue(store, key, ttl_hours=1.0)
    verdict = validate_agent_token(token, NODE, store=store, key=key, now=T0 + 3601)
    assert not verdict.valid
    assert not verdict.exp_ok
    assert verdict.reason == "expired"


def test_not_yet_valid_token_rejected(store: AgentTokenStore, key: Ed25519PrivateKey) -> None:
    token, _ = _issue(store, key, now=T0 + 600)
    verdict = validate_agent_token(token, NODE, store=store, key=key, now=T0)
    assert not verdict.valid
    assert verdict.reason == "not_yet_valid"


def test_wrong_aud_rejected(store: AgentTokenStore, key: Ed25519PrivateKey) -> None:
    token, _ = _issue(store, key, node="mesh-node-other")
    verdict = validate_agent_token(token, NODE, store=store, key=key, now=T0 + 1)
    assert not verdict.valid
    assert verdict.aud == "mesh-node-other"
    assert verdict.reason == "aud_mismatch"  # node binding (TM §3)


# ── denylist + grace ──────────────────────────────────────────────────────────


def test_revoked_jti_rejected(store: AgentTokenStore, key: Ed25519PrivateKey) -> None:
    token, claims = _issue(store, key)
    assert store.revoke_jti(claims.jti, now=T0 + 10)
    verdict = validate_agent_token(token, NODE, store=store, key=key, now=T0 + 11)
    assert not verdict.valid
    assert verdict.revoked
    assert verdict.reason == "revoked"
    # idempotent denylist write: second revoke returns False
    assert not store.revoke_jti(claims.jti, now=T0 + 12)


def test_revoke_all_for_agent(store: AgentTokenStore, key: Ed25519PrivateKey) -> None:
    t1, _c1 = _issue(store, key)
    t2, _c2 = _issue(store, key, scope="rw")
    assert store.revoke_agent(AGENT, now=T0 + 5) == 2
    for token in (t1, t2):
        verdict = validate_agent_token(token, NODE, store=store, key=key, now=T0 + 6)
        assert not verdict.valid
        assert verdict.reason == "revoked"


def test_unknown_jti_fails_closed(store: AgentTokenStore, key: Ed25519PrivateKey) -> None:
    token, claims = _issue(store, key)
    with store._lock:  # simulate the minting record vanishing (DB loss)
        store._conn.execute("DELETE FROM agent_tokens WHERE jti = ?", (claims.jti,))
        store._conn.commit()
    verdict = validate_agent_token(token, NODE, store=store, key=key, now=T0 + 1)
    assert not verdict.valid
    assert verdict.reason == "unknown_jti"


def test_grace_rotation_old_and_new_both_valid_in_window(
    store: AgentTokenStore, key: Ed25519PrivateKey
) -> None:
    old_token, old_claims = _issue(store, key, ttl_hours=8.0)
    rotate_at = T0 + 600
    new_token, new_claims, graced = rotate_agent_token(
        agent_id=AGENT,
        grace_minutes=ROTATE_GRACE_MINUTES,
        store=store,
        key=key,
        now=rotate_at,
    )
    assert graced == 1
    assert new_claims.jti != old_claims.jti
    # inside the grace window: BOTH valid (TM §5)
    mid = rotate_at + 60
    assert validate_agent_token(old_token, NODE, store=store, key=key, now=mid).valid
    assert validate_agent_token(new_token, NODE, store=store, key=key, now=mid).valid
    # after the grace window: old fails closed, new still valid
    late = rotate_at + ROTATE_GRACE_MINUTES * 60 + 1
    old_verdict = validate_agent_token(old_token, NODE, store=store, key=key, now=late)
    assert not old_verdict.valid
    assert old_verdict.reason == "grace_expired"
    assert validate_agent_token(new_token, NODE, store=store, key=key, now=late).valid


def test_rotate_inherits_scope_and_aud(store: AgentTokenStore, key: Ed25519PrivateKey) -> None:
    _issue(store, key, scope="rw,project:foo", ttl_hours=24.0)
    _, new_claims, _ = rotate_agent_token(agent_id=AGENT, store=store, key=key, now=T0 + 60)
    assert new_claims.scope == ["rw", "project:foo"]
    assert new_claims.aud == NODE
    assert new_claims.exp - new_claims.iat == 24 * 3600


def test_rotate_without_live_token_raises(store: AgentTokenStore, key: Ed25519PrivateKey) -> None:
    with pytest.raises(LookupError):
        rotate_agent_token(agent_id="ghost", store=store, key=key, now=T0)


# ── scope grammar ─────────────────────────────────────────────────────────────


def test_parse_scope_defaults_and_forms() -> None:
    assert parse_scope("") == ["read"]
    assert parse_scope("read") == ["read"]
    assert parse_scope("rw") == ["rw"]  # TM §5 form
    assert parse_scope("project:foo") == ["read", "project:foo"]
    assert parse_scope("rw,project:foo,project:bar") == [
        "rw",
        "project:bar",
        "project:foo",
    ]


def test_parse_scope_rejects_degenerate_grants() -> None:
    for bad in ("read,rw", "admin", "project:", "project:*", "project: a ", "rw,rw,b"):
        with pytest.raises(ScopeError):
            parse_scope(bad)


def test_scope_allows_write() -> None:
    assert not scope_allows_write(["read", "project:foo"])
    assert scope_allows_write(["rw", "project:foo"])
    assert scope_allows_write("rw,project:foo")  # stored comma form


def test_ttl_ceiling_enforced(store: AgentTokenStore, key: Ed25519PrivateKey) -> None:
    with pytest.raises(AgentTokenError, match="24"):
        _issue(store, key, ttl_hours=MAX_TTL_HOURS + 1)
    with pytest.raises(AgentTokenError):
        _issue(store, key, ttl_hours=0)


# ── signing key lifecycle ─────────────────────────────────────────────────────


def test_signing_key_minted_0600_and_stable(tmp_path: Path) -> None:
    key_path = tmp_path / "agent-token-signing.key"
    key1 = load_or_create_signing_key(key_path)
    assert key_path.exists()
    mode = stat.S_IMODE(key_path.stat().st_mode)
    assert mode == 0o600
    key2 = load_or_create_signing_key(key_path)
    pub1 = key1.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    pub2 = key2.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    assert pub1 == pub2  # reload yields the SAME key (no silent re-mint)
    assert key_path.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")


def test_signing_key_path_override(tmp_path: Path) -> None:
    override = tmp_path / "custom" / "k.pem"
    resolved = signing_key_path(tmp_path / "data", override=str(override))
    assert resolved == override


def test_signing_key_rejects_wrong_key_type(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    wrong = X25519PrivateKey.generate()
    key_path = tmp_path / "k.pem"
    key_path.write_bytes(
        wrong.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with pytest.raises(AgentTokenError, match="Ed25519"):
        load_or_create_signing_key(key_path)


# ── CLI end-to-end: issue → validate → rotate → revoke → invalid ──────────────


def _cli_config(tmp_path: Path) -> list[str]:
    """Write an isolated config.yaml; the machine's ~/.mnemos must never be
    reachable from the test (config-file values outrank env vars — a bare
    env override silently loads the owner's real data dir)."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"vesma:\n  data_dir: {tmp_path / 'data'}\n", encoding="utf-8")
    return ["--config", str(cfg)]


def _extract_token(output: str) -> str:
    for line in output.splitlines():
        if "token" in line and "." in line and "Token" not in line:
            part = line.split(":", 1)[-1].strip()
            # rich may wrap ansi; CliRunner strips it by default
            if part.count(".") == 2:
                return part
    raise AssertionError(f"token not printed once in output:\n{output}")


def test_cli_issue_validate_rotate_revoke_flow(tmp_path: Path) -> None:
    cfg = _cli_config(tmp_path)

    result = runner.invoke(
        cli_app,
        [
            "agent-token",
            "issue",
            "--agent",
            AGENT,
            "--node",
            NODE,
            "--scope",
            "rw,project:foo",
            *cfg,
        ],
    )
    assert result.exit_code == 0, result.output
    token1 = _extract_token(result.output)
    assert "Store this token now" in result.output  # printed-ONCE warning

    # list must NOT contain the plaintext token (metadata only)
    listing = runner.invoke(cli_app, ["agent-token", "list", *cfg], env={"COLUMNS": "300"})
    assert listing.exit_code == 0, listing.output
    assert token1 not in listing.output
    assert AGENT in listing.output

    # validate via the exported hook (launch condition TM §8)
    settings_db = tmp_path / "data" / "mnemos.db"
    key = load_or_create_signing_key(tmp_path / "data" / "agent-token-signing.key")
    with AgentTokenStore(settings_db) as st:
        verdict = validate_agent_token(token1, NODE, store=st, key=key)
        assert verdict.valid
        assert verdict.agent_id == AGENT
        assert verdict.scope == ["rw", "project:foo"]
        assert scope_allows_write(verdict.scope)

    # rotate: both valid inside the grace window
    rotated = runner.invoke(
        cli_app,
        ["agent-token", "rotate", "--agent", AGENT, "--grace-minutes", "5", *cfg],
    )
    assert rotated.exit_code == 0, rotated.output
    token2 = _extract_token(rotated.output)

    with AgentTokenStore(settings_db) as st:
        assert validate_agent_token(token1, NODE, store=st, key=key).valid  # grace
        assert validate_agent_token(token2, NODE, store=st, key=key).valid

    # revoke-all: both dead instantly (compromise response)
    revoked = runner.invoke(cli_app, ["agent-token", "revoke", "--agent", AGENT, *cfg])
    assert revoked.exit_code == 0, revoked.output

    with AgentTokenStore(settings_db) as st:
        for token in (token1, token2):
            verdict = validate_agent_token(token, NODE, store=st, key=key)
            assert not verdict.valid
            assert verdict.reason == "revoked"

    final = runner.invoke(cli_app, ["agent-token", "list", *cfg], env={"COLUMNS": "300"})
    assert "revoked" in final.output


def test_cli_revoke_requires_target(tmp_path: Path) -> None:
    result = runner.invoke(cli_app, ["agent-token", "revoke", *_cli_config(tmp_path)])
    assert result.exit_code == 1
    assert "--agent" in result.output or "--jti" in result.output


def test_cli_issue_rejects_over_ceiling_ttl(tmp_path: Path) -> None:
    result = runner.invoke(
        cli_app,
        [
            "agent-token",
            "issue",
            "--agent",
            AGENT,
            "--node",
            NODE,
            "--ttl-hours",
            "25",
            *_cli_config(tmp_path),
        ],
    )
    assert result.exit_code == 1


def test_cli_list_empty(tmp_path: Path) -> None:
    result = runner.invoke(cli_app, ["agent-token", "list", *_cli_config(tmp_path)])
    assert result.exit_code == 0
    assert "No agent tokens" in result.output


# ── real-clock sanity (one test outside the fixed T0 base) ────────────────────


def test_issue_with_real_clock_stamp(store: AgentTokenStore, key: Ed25519PrivateKey) -> None:
    before = int(datetime.now(UTC).timestamp())
    _, claims = _issue(store, key, now=None)
    assert claims.iat >= before
