"""Agent token scheme for the W3-v1 AgentGateway leg (ADR-0018 + ADR-0018-T).

Implements the Vesma side of the token contract mandated by the W3 threat
model (``docs: ADR-0018-threat-model.md``, normative):

* **Envelope** (TM §3) — versioned, JWT-like but intentionally NOT a JWT
  library product::

      base64url(header) . base64url(payload) . base64url(signature)

  header  = ``{"typ": "mnemos-agent-token", "alg": "Ed25519", "v": 1}``
  payload = ``{"v", "iss", "sub", "aud", "scope", "iat", "exp", "jti"}``

  The signature is Ed25519 over the ASCII bytes ``header.payload``. The
  header is cleartext BY DESIGN: the mesh reads it for routing, audit, and
  fast-fail only (``aud`` early reject); signature verification,
  revocation, and scope→ACL resolution happen in Vesma on EVERY request
  (criterion 5 — the mesh is not an ACL authority).

* **Why Ed25519 and not HMAC-SHA256** — ``cryptography>=50`` is already a
  direct runtime dependency (Fernet for TOTP-at-rest), so the asymmetric
  scheme costs nothing new; a symmetric MAC would require shipping the
  shared secret to every future verifier, violating "the signing key never
  leaves Vesma" the moment anyone besides the minter must verify. The
  Ed25519 seed is 256-bit, satisfying the token entropy floor against
  offline brute force (TM §4 T8): without the seed an attacker cannot
  forge a token regardless of how many envelopes they observe.

* **TTL** (TM §3) — mandatory, max 24 h, default 8 h. Enforced at issue
  time; ``exp`` is carried in the envelope and re-checked per request.

* **Node binding** (TM §3) — ``aud`` = the gateway node id the agent
  dials; ``validate_agent_token`` rejects a token presented to a different
  node.

* **Revocation** (TM §3/§5) — mnemos-side denylist keyed by ``jti``
  (single token) and ``agent_id`` (revoke-all, the compromise response),
  checked per request. Rotation marks the old ``jti`` live for a grace
  window only, then it fails closed.

* **Storage** — token plaintexts are NEVER stored: only ``jti`` + issuance
  metadata land in the ``agent_tokens`` table (additive ``CREATE TABLE IF
  NOT EXISTS``, same pattern as :class:`vesmaro.api.auth_store.AuthStore`).

The bearer token travels in gRPC ``authorization`` metadata end-to-end and
MUST never appear in proto message fields (TM §2) — fields reach logs.

Launch condition (TM §8): the validation hook here lands BEFORE the mesh
forwards the first agent request (wiring in the gateway server is part 2
of W3-v1).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
import threading
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

logger = logging.getLogger(__name__)

__all__ = [
    "AGENT_TOKEN_HEADER_TYPE",
    "DEFAULT_TTL_HOURS",
    "MAX_TTL_HOURS",
    "ROTATE_GRACE_MINUTES",
    "AgentTokenClaims",
    "AgentTokenError",
    "AgentTokenStore",
    "AgentTokenVerdict",
    "BadSignatureError",
    "MalformedTokenError",
    "ScopeError",
    "UnsupportedVersionError",
    "issue_agent_token",
    "load_or_create_signing_key",
    "parse_scope",
    "rotate_agent_token",
    "scope_allows_write",
    "signing_key_path",
    "token_project_grants",
    "validate_agent_token",
]

#: Envelope type tag — distinguishes the custom envelope from JWTs and from
#: other bearer schemes on the same metadata key.
AGENT_TOKEN_HEADER_TYPE: Final[str] = "mnemos-agent-token"

#: Envelope version. v1 is the only version; a token with a different
#: ``v`` is rejected (no silent upgrade — a future v2 is an explicit cutover).
AGENT_TOKEN_VERSION: Final[int] = 1

#: TM §3: TTL is mandatory, max 24 h, default 8 h.
DEFAULT_TTL_HOURS: Final[float] = 8.0
MAX_TTL_HOURS: Final[float] = 24.0

#: TM §5: rotate honours the old token for a grace window, default 5 min.
ROTATE_GRACE_MINUTES: Final[int] = 5

#: Scope class grants. ``rw`` ⊃ ``read``: the write RPC requires ``rw``.
SCOPE_READ: Final[str] = "read"
SCOPE_RW: Final[str] = "rw"

#: Default signing-key filename under ``vesma.data_dir`` (TM §3: the key
#: lives in Vesma config territory and never leaves the host).
DEFAULT_KEY_FILENAME: Final[str] = "agent-token-signing.key"

_DEFAULT_ISSUER: Final[str] = "vesma"

_MAX_AGENT_ID_LEN: Final[int] = 128
_MAX_NODE_ID_LEN: Final[int] = 128
_MAX_SLUG_LEN: Final[int] = 64


# ── Errors ────────────────────────────────────────────────────────────────────


class AgentTokenError(ValueError):
    """Base class for agent-token failures (typed, never swallowed)."""


class MalformedTokenError(AgentTokenError):
    """The envelope is not parseable (segments, base64, JSON, claim types)."""


class BadSignatureError(AgentTokenError):
    """The Ed25519 signature does not verify against the signing key."""


class UnsupportedVersionError(AgentTokenError):
    """The envelope carries an unknown version or type/alg."""


class ScopeError(AgentTokenError):
    """The ``--scope`` spec is not expressible in the v1 grant grammar."""


# ── Scope grammar ─────────────────────────────────────────────────────────────


def _valid_project_slug(slug: str) -> bool:
    """Structural slug check consistent with the config gates.

    Mirrors :func:`vesmaro.config._reject_degenerate_project_slugs`
    semantics: blank is refused (matches every untagged record), ``*`` is
    refused (wildcards are per-peer ACL concepts, never token grants), and
    the structural delimiters of the scope grammar (``:``, ``,``, space)
    are refused so a grant cannot smuggle a second grant.
    """
    if not slug or len(slug) > _MAX_SLUG_LEN:
        return False
    if slug.strip() != slug:
        return False
    return not any(ch in slug for ch in ":, \t*")


def parse_scope(spec: str) -> list[str]:
    """Normalize a CLI ``--scope`` spec into canonical grant order.

    Grammar (comma-separated grants):

    * ``read`` — read-only class grant (default when only project grants
      are given);
    * ``rw`` — read+write class grant (write RPC becomes available);
    * ``project:<slug>`` — narrow the token to one project; repeatable.
      An empty project set means "all projects the effective scope
      (token ∩ transport-peer ACL) allows" — projects can only narrow.

    At most one class grant is allowed. Canonical output order: the class
    grant first, then project grants sorted. Examples::

        ""                     -> ["read"]
        "rw"                   -> ["rw"]                    (TM §5 form)
        "project:foo"          -> ["read", "project:foo"]
        "rw,project:foo,project:bar" -> ["rw", "project:bar", "project:foo"]

    Raises :class:`ScopeError` on unknown grants, duplicate class grants,
    degenerate slugs, or a slug-count overflow (≤ 32 projects per token).
    """
    grants = [g.strip() for g in spec.split(",")]
    grants = [g for g in grants if g]
    if not grants:
        return [SCOPE_READ]

    class_grant: str | None = None
    projects: set[str] = set()
    for grant in grants:
        if grant in (SCOPE_READ, SCOPE_RW):
            if class_grant is not None and grant != class_grant:
                raise ScopeError(f"scope: at most one class grant allowed (read/rw), got {spec!r}")
            class_grant = grant
        elif grant.startswith("project:"):
            slug = grant[len("project:") :]
            if not _valid_project_slug(slug):
                raise ScopeError(
                    f"scope: invalid project grant {grant!r} — the slug must be "
                    "non-blank, ≤64 chars, without ':', ',', spaces, or '*'"
                )
            projects.add(slug)
        else:
            raise ScopeError(
                f"scope: unknown grant {grant!r} — expected 'read', 'rw', or 'project:<slug>'"
            )
    if len(projects) > 32:
        raise ScopeError("scope: at most 32 project grants per token")
    if class_grant is None:
        class_grant = SCOPE_READ
    return [class_grant, *(f"project:{p}" for p in sorted(projects))]


def scope_allows_write(scope: Sequence[str]) -> bool:
    """True when the (parsed or stored) scope carries the ``rw`` grant."""
    grants = scope.split(",") if isinstance(scope, str) else scope
    return SCOPE_RW in grants


def token_project_grants(scope: Sequence[str]) -> list[str]:
    """Project slugs a token scope narrows to (``[]`` = no narrowing).

    The scope grammar's ``project:<slug>`` grants can only NARROW the
    effective scope (token ∩ transport-peer ACL, TM §3); an empty result
    means "every project the transport peer's ACL already allows". Used
    by the core-leg data gate (W3 part 3) to intersect the peer's
    effective allowed set with the token's project grants — an agent can
    never see more than the underlying peer leg permits.
    """
    grants = scope.split(",") if isinstance(scope, str) else scope
    return [g[len("project:") :] for g in grants if g.startswith("project:")]


# ── Claims + envelope ─────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class AgentTokenClaims:
    """The cleartext payload claims (TM §3 envelope)."""

    iss: str
    sub: str
    aud: str
    scope: list[str]
    iat: int
    exp: int
    jti: str
    v: int = AGENT_TOKEN_VERSION


@dataclass(frozen=True, slots=True)
class AgentTokenVerdict:
    """Result of :func:`validate_agent_token` — per-check outcomes, not a
    bare bool, so the gateway (part 2) can log precise reject reasons."""

    valid: bool
    agent_id: str | None
    scope: list[str] = field(default_factory=list)
    jti: str | None = None
    aud: str | None = None
    exp_ok: bool = False
    revoked: bool = False
    reason: str | None = None


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except (ValueError, TypeError) as exc:
        raise MalformedTokenError(f"invalid base64url segment: {exc}") from exc


def encode_agent_token(claims: AgentTokenClaims, key: Ed25519PrivateKey) -> str:
    """Serialize + sign claims into the three-segment envelope."""
    header = {"typ": AGENT_TOKEN_HEADER_TYPE, "alg": "Ed25519", "v": claims.v}
    payload = {
        "v": claims.v,
        "iss": claims.iss,
        "sub": claims.sub,
        "aud": claims.aud,
        "scope": claims.scope,
        "iat": claims.iat,
        "exp": claims.exp,
        "jti": claims.jti,
    }
    header_b64 = _b64url_encode(json.dumps(header, separators=(",", ":")).encode())
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":")).encode())
    signature = key.sign(f"{header_b64}.{payload_b64}".encode("ascii"))
    return f"{header_b64}.{payload_b64}.{_b64url_encode(signature)}"


def decode_agent_token(token: str, key: Ed25519PrivateKey) -> AgentTokenClaims:
    """Verify signature + structure and return the claims.

    Fail-closed ordering: structure first (cheap), then signature, then
    claim-shape validation. Any failure raises a subclass of
    :class:`AgentTokenError`.
    """
    segments = token.strip().split(".")
    if len(segments) != 3 or not all(segments):
        raise MalformedTokenError(
            "envelope must have exactly three non-empty dot-separated segments"
        )
    header_b64, payload_b64, signature_b64 = segments
    try:
        header = json.loads(_b64url_decode(header_b64))
        payload = json.loads(_b64url_decode(payload_b64))
        signature = _b64url_decode(signature_b64)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MalformedTokenError(f"envelope segments are not valid JSON: {exc}") from exc

    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise MalformedTokenError("header/payload must be JSON objects")

    if header.get("typ") != AGENT_TOKEN_HEADER_TYPE or header.get("alg") != "Ed25519":
        raise UnsupportedVersionError(
            f"unexpected envelope header {header.get('typ')!r}/{header.get('alg')!r}"
        )
    if header.get("v") != AGENT_TOKEN_VERSION or payload.get("v") != AGENT_TOKEN_VERSION:
        raise UnsupportedVersionError(f"unsupported envelope version {payload.get('v')!r}")

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    try:
        # Ed25519 verification lives on the PUBLIC key (the private key
        # object exposes only sign()). InvalidSignature is deliberately
        # caught EXPLICITLY: it does not subclass ValueError in
        # `cryptography`, and a bare `except Exception` would mask an
        # AttributeError as a signature failure (that exact bug shipped
        # and was caught by these tests' first run).
        key.public_key().verify(signature, signing_input)
    except InvalidSignature as exc:
        raise BadSignatureError("Ed25519 signature verification failed") from exc

    return _claims_from_payload(payload)


def _claims_from_payload(payload: dict[str, Any]) -> AgentTokenClaims:
    """Validate payload claim shapes (after signature verification)."""
    try:
        claims = AgentTokenClaims(
            iss=_require_str(payload, "iss", _MAX_AGENT_ID_LEN),
            sub=_require_str(payload, "sub", _MAX_AGENT_ID_LEN),
            aud=_require_str(payload, "aud", _MAX_NODE_ID_LEN),
            scope=_validated_scope(payload),
            iat=_require_int(payload, "iat"),
            exp=_require_int(payload, "exp"),
            jti=_require_str(payload, "jti", 64),
            v=int(payload["v"]),
        )
    except AgentTokenError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise MalformedTokenError(f"claim shape invalid: {exc}") from exc
    if claims.exp <= claims.iat:
        raise MalformedTokenError(f"exp ({claims.exp}) must be after iat ({claims.iat})")
    return claims


def _require_str(payload: dict[str, Any], name: str, max_len: int) -> str:
    value = payload[name]
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise MalformedTokenError(f"claim {name!r} must be a non-empty str ≤{max_len}")
    return value


def _require_int(payload: dict[str, Any], name: str) -> int:
    value = payload[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedTokenError(f"claim {name!r} must be an integer")
    return value


def _validated_scope(payload: dict[str, Any]) -> list[str]:
    scope = payload.get("scope")
    if not isinstance(scope, list) or not scope:
        raise MalformedTokenError("claim 'scope' must be a non-empty list")
    for grant in scope:
        if not isinstance(grant, str):
            raise MalformedTokenError("scope grants must be strings")
    if scope_allows_write(scope) and SCOPE_READ in scope:
        raise MalformedTokenError("scope cannot carry both 'read' and 'rw'")
    normalized = parse_scope(",".join(scope))
    if normalized != scope:
        raise MalformedTokenError(f"scope {scope!r} is not in canonical form")
    return scope


# ── Signing-key management ────────────────────────────────────────────────────


def signing_key_path(data_dir: Path, *, override: str | None = None) -> Path:
    """Resolve the signing-key path (config override wins, TM §3)."""
    if override:
        return Path(override).expanduser()
    return data_dir / DEFAULT_KEY_FILENAME


def load_or_create_signing_key(path: Path) -> Ed25519PrivateKey:
    """Load the Ed25519 signing key, minting it on first use (mode 0600).

    TM §3: the signing key never leaves Vesma; generation happens at
    first start, storage is a PEM file readable by the vesma user only.

    The file is created with ``O_EXCL`` and mode ``0o600`` (umask cannot
    widen it) and written atomically — a torn key file can never exist
    under this path because the bytes land in a fresh inode that is only
    linked under the final name after a successful ``fsync``.
    """
    if path.exists():
        return _load_signing_key(path)

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    # Review N2: write via temp + os.replace so a crash mid-write can
    # never leave a torn PEM under the final path (the docstring
    # promises exactly that; a direct O_EXCL write to the final name
    # did not uphold it).
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _load_signing_key(path)  # concurrent first-use race: load the winner
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)
    logger.info("agent_tokens: generated new Ed25519 signing key at %s", path)
    return _load_signing_key(path)  # round-trip through disk: one code path


def _load_signing_key(path: Path) -> Ed25519PrivateKey:
    try:
        loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
    except (ValueError, OSError) as exc:
        raise AgentTokenError(f"cannot load signing key {path}: {exc}") from exc
    if not isinstance(loaded, Ed25519PrivateKey):
        raise AgentTokenError(f"signing key {path} is {type(loaded).__name__}, expected Ed25519")
    return loaded


# ── Storage ───────────────────────────────────────────────────────────────────

#: Additive DDL — safe next to the main schema and AuthStore (same file,
#: WAL; ``CREATE TABLE IF NOT EXISTS``). Token plaintexts are NEVER stored:
#: only jti + issuance metadata (the plaintext exists only in the CLI
#: output and the harness's 0600 file).
_AGENT_TOKEN_DDL = """
CREATE TABLE IF NOT EXISTS agent_tokens (
    jti         TEXT PRIMARY KEY,
    agent_id    TEXT NOT NULL,
    aud         TEXT NOT NULL,
    scope       TEXT NOT NULL,
    iat         INTEGER NOT NULL,
    exp         INTEGER NOT NULL,
    revoked_at  INTEGER,
    grace_until INTEGER
);
CREATE INDEX IF NOT EXISTS idx_agent_tokens_agent ON agent_tokens(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_tokens_exp  ON agent_tokens(exp);
"""


class AgentTokenStore:
    """Thread-safe agent-token registry backed by the shared SQLite DB.

    Same concurrency contract as :class:`vesmaro.api.auth_store.AuthStore`:
    one ``threading.RLock`` around one ``check_same_thread=False`` WAL
    connection, safe alongside the main store connection.
    """

    def __init__(self, db_path: Path) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(db_path), check_same_thread=False, detect_types=sqlite3.PARSE_DECLTYPES
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(_AGENT_TOKEN_DDL)
            self._conn.commit()

    # ── writes ───────────────────────────────────────────────────────────

    def register(self, claims: AgentTokenClaims) -> None:
        """Record a freshly minted token (jti + metadata, no plaintext)."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO agent_tokens (jti, agent_id, aud, scope, iat, exp)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    claims.jti,
                    claims.sub,
                    claims.aud,
                    ",".join(claims.scope),
                    claims.iat,
                    claims.exp,
                ),
            )
            self._conn.commit()

    def revoke_jti(self, jti: str, *, now: int) -> bool:
        """Denylist one token by jti. Returns False when the jti is unknown
        or already revoked (idempotent denylist write)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE agent_tokens SET revoked_at = ? WHERE jti = ? AND revoked_at IS NULL",
                (now, jti),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def revoke_agent(self, agent_id: str, *, now: int) -> int:
        """Revoke ALL live tokens of an agent (compromise response, TM §5).

        Returns the number of tokens flipped to revoked.
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE agent_tokens SET revoked_at = ? WHERE agent_id = ? AND revoked_at IS NULL",
                (now, agent_id),
            )
            self._conn.commit()
            return cur.rowcount

    def set_grace(self, jti: str, *, grace_until: int) -> bool:
        """Shorten a token's life to the rotate grace window (TM §5)."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE agent_tokens SET grace_until = ? WHERE jti = ? AND revoked_at IS NULL",
                (grace_until, jti),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ── reads ────────────────────────────────────────────────────────────

    def get(self, jti: str) -> dict[str, Any] | None:
        """Fetch one row by jti (metadata only — plaintexts are not stored)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT jti, agent_id, aud, scope, iat, exp, revoked_at, grace_until"
                " FROM agent_tokens WHERE jti = ?",
                (jti,),
            ).fetchone()
        return dict(row) if row is not None else None

    def live_rows_for_agent(self, agent_id: str) -> list[dict[str, Any]]:
        """Not-revoked, not-expired rows for an agent (rotate candidates)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT jti, agent_id, aud, scope, iat, exp, revoked_at, grace_until"
                " FROM agent_tokens WHERE agent_id = ? AND revoked_at IS NULL"
                " ORDER BY iat DESC",
                (agent_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_tokens(self) -> list[dict[str, Any]]:
        """All rows, newest first — metadata only (no secrets exist to leak)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT jti, agent_id, aud, scope, iat, exp, revoked_at, grace_until"
                " FROM agent_tokens ORDER BY iat DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> AgentTokenStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


# ── Operations (CLI + part-2 servicer entry points) ───────────────────────────


def issue_agent_token(
    *,
    agent_id: str,
    node_id: str,
    scope_spec: str,
    ttl_hours: float = DEFAULT_TTL_HOURS,
    store: AgentTokenStore,
    key: Ed25519PrivateKey,
    issuer_id: str = _DEFAULT_ISSUER,
    now: int | None = None,
) -> tuple[str, AgentTokenClaims]:
    """Mint + register a token. Returns ``(token, claims)``.

    The plaintext exists exactly here and in the caller's single print;
    only the jti + metadata are persisted (TM §5: token prints ONCE).
    """
    if not agent_id or len(agent_id) > _MAX_AGENT_ID_LEN:
        raise AgentTokenError(f"agent id must be 1..{_MAX_AGENT_ID_LEN} chars")
    if not node_id or len(node_id) > _MAX_NODE_ID_LEN:
        raise AgentTokenError(f"node id must be 1..{_MAX_NODE_ID_LEN} chars")
    if not 0.0 < ttl_hours <= MAX_TTL_HOURS:
        raise AgentTokenError(
            f"ttl-hours must be in (0, {MAX_TTL_HOURS:g}] — TM §3 caps agent tokens at 24h"
        )
    scope = parse_scope(scope_spec)
    issued_at = now if now is not None else int(datetime.now(UTC).timestamp())
    claims = AgentTokenClaims(
        iss=issuer_id,
        sub=agent_id,
        aud=node_id,
        scope=scope,
        iat=issued_at,
        exp=issued_at + int(ttl_hours * 3600),
        jti=uuid.uuid4().hex,
    )
    token = encode_agent_token(claims, key)
    store.register(claims)
    logger.info(
        "agent_tokens: issued jti=%s agent_id=%s aud=%s scope=%s exp=%s",
        claims.jti,
        agent_id,
        node_id,
        ",".join(scope),
        claims.exp,
    )
    return token, claims


def rotate_agent_token(
    *,
    agent_id: str,
    grace_minutes: int = ROTATE_GRACE_MINUTES,
    store: AgentTokenStore,
    key: Ed25519PrivateKey,
    now: int | None = None,
) -> tuple[str, AgentTokenClaims, int]:
    """Rotate an agent's tokens: grace-mark the live ones, mint a fresh jti.

    TM §5: the old token is honoured ONLY for the grace window, then
    dropped. The new token inherits the scope/aud of the most recent live
    token and its original lifetime (capped by the 24 h TM maximum).

    Returns ``(new_token, new_claims, number_graced)``.
    :exc:`LookupError` when the agent has no live token to rotate.
    """
    if grace_minutes < 0 or grace_minutes > 60:
        raise AgentTokenError("grace-minutes must be in [0, 60]")
    moment = now if now is not None else int(datetime.now(UTC).timestamp())
    live = store.live_rows_for_agent(agent_id)
    current = next(
        (row for row in live if int(row["exp"]) > moment and row["grace_until"] is None),
        None,
    )
    if current is None:
        raise LookupError(
            f"agent {agent_id!r} has no live (un-revoked, un-expired, "
            "un-graced) token to rotate — issue a new one instead"
        )
    grace_until = moment + grace_minutes * 60
    graced = 0
    for row in live:
        eligible = int(row["exp"]) > moment and row["grace_until"] is None
        if eligible and store.set_grace(str(row["jti"]), grace_until=grace_until):
            graced += 1
    lifetime = min(int(current["exp"]) - int(current["iat"]), int(MAX_TTL_HOURS * 3600))
    ttl_hours = max(lifetime / 3600.0, 1.0 / 3600.0)  # keep > 0 even for edge lifetimes
    token, claims = issue_agent_token(
        agent_id=agent_id,
        node_id=str(current["aud"]),
        scope_spec=str(current["scope"]),
        ttl_hours=ttl_hours,
        store=store,
        key=key,
        now=moment,
    )
    logger.info(
        "agent_tokens: rotated agent_id=%s graced=%d grace_until=%s new_jti=%s",
        agent_id,
        graced,
        grace_until,
        claims.jti,
    )
    return token, claims, graced


def validate_agent_token(
    token: str,
    gateway_node_id: str,
    *,
    store: AgentTokenStore,
    key: Ed25519PrivateKey,
    now: int | None = None,
) -> AgentTokenVerdict:
    """Full mnemos-side validation (TM §3 — runs on EVERY gateway request).

    Check order (fail-closed at each step):

    1. envelope structure + signature (Ed25519);
    2. version (v1);
    3. lifetime: ``iat <= now < exp``;
    4. node binding: ``aud == gateway_node_id``;
    5. denylist: jti known (fail-closed on an unknown jti — the minting
       record is authoritative), not revoked, grace window not passed.

    The verdict carries per-check outcomes so the caller (part 2 servicer)
    can log a precise reject reason. ``iss`` is deliberately NOT compared:
    possession of a valid signature over the envelope already proves the
    token was minted by THIS Vesma instance (single issuer per deployment, one key
    per host) — a string compare would add no security.
    """
    moment = now if now is not None else int(datetime.now(UTC).timestamp())

    # Review N3 (defense-in-depth): a sane length cap BEFORE parsing —
    # gRPC metadata limits will bound this on the wire, but the validator
    # stays cheap and bounded standalone too.
    if not token or len(token) > 8192:
        return AgentTokenVerdict(valid=False, agent_id=None, reason="malformed")

    try:
        claims = decode_agent_token(token, key)
    except AgentTokenError as exc:
        logger.info("agent_tokens: rejected envelope (%s)", exc)
        return AgentTokenVerdict(
            valid=False,
            agent_id=None,
            reason=_reason_for(exc),
        )

    exp_ok = claims.iat <= moment < claims.exp
    if not exp_ok:
        reason = "expired" if moment >= claims.exp else "not_yet_valid"
        logger.info("agent_tokens: jti=%s rejected (%s)", claims.jti, reason)
        return AgentTokenVerdict(
            valid=False,
            agent_id=claims.sub,
            scope=claims.scope,
            jti=claims.jti,
            aud=claims.aud,
            exp_ok=False,
            reason=reason,
        )

    if claims.aud != gateway_node_id:
        logger.info(
            "agent_tokens: jti=%s rejected aud_mismatch (token aud=%s, node=%s)",
            claims.jti,
            claims.aud,
            gateway_node_id,
        )
        return AgentTokenVerdict(
            valid=False,
            agent_id=claims.sub,
            scope=claims.scope,
            jti=claims.jti,
            aud=claims.aud,
            exp_ok=True,
            reason="aud_mismatch",
        )

    row = store.get(claims.jti)
    if row is None:
        logger.info("agent_tokens: jti=%s rejected unknown_jti (fail-closed)", claims.jti)
        return AgentTokenVerdict(
            valid=False,
            agent_id=claims.sub,
            scope=claims.scope,
            jti=claims.jti,
            aud=claims.aud,
            exp_ok=True,
            reason="unknown_jti",
        )
    if row["revoked_at"] is not None:
        logger.info("agent_tokens: jti=%s rejected revoked", claims.jti)
        return AgentTokenVerdict(
            valid=False,
            agent_id=claims.sub,
            scope=claims.scope,
            jti=claims.jti,
            aud=claims.aud,
            exp_ok=True,
            revoked=True,
            reason="revoked",
        )
    grace_until = row["grace_until"]
    if grace_until is not None and moment >= int(grace_until):
        logger.info("agent_tokens: jti=%s rejected grace_expired", claims.jti)
        return AgentTokenVerdict(
            valid=False,
            agent_id=claims.sub,
            scope=claims.scope,
            jti=claims.jti,
            aud=claims.aud,
            exp_ok=True,
            reason="grace_expired",
        )

    return AgentTokenVerdict(
        valid=True,
        agent_id=claims.sub,
        scope=claims.scope,
        jti=claims.jti,
        aud=claims.aud,
        exp_ok=True,
    )


def _reason_for(exc: AgentTokenError) -> str:
    if isinstance(exc, BadSignatureError):
        return "bad_signature"
    if isinstance(exc, UnsupportedVersionError):
        return "unsupported_version"
    return "malformed"
