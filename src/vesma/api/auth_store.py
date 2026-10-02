"""Auth storage layer: tokens, sessions, and challenges (T-AUTH, ADR-0014).

All bearer-token and session plaintexts are **never stored** — only their
SHA-256 hex digests are persisted.  The plaintext is shown once at creation
and never again.

Thread-safety: a single ``threading.RLock`` guards every method.  The
connection is opened with ``check_same_thread=False`` and WAL mode so it can
coexist with the main ``SQLiteStore`` connection on the same database file.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "mnk_"  # nosec B105 - a bearer token prefix, not a credential
LOCKOUT_MINUTES = 15
LOGIN_LOCKOUT_THRESHOLD = 10
TOTP_LOCKOUT_THRESHOLD = 3
CHALLENGE_MAX_ATTEMPTS = 5
CHALLENGE_TTL_SEC = 120


def _parse_datetime_utc(value: str) -> datetime:
    """Parse an ISO-8601 string and return an offset-aware UTC datetime.

    Handles both offset-aware (``2026-07-07T23:05:15+00:00``) and
    offset-naive (``2027-12-31`` or ``2027-12-31T00:00:00``) strings.
    Naive strings are assumed to be UTC.
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


# auth-12: absolute session cap (7 days) regardless of sliding refresh.
MAX_SESSION_LIFETIME_SEC = 7 * 24 * 3600

# DDL is idempotent — running it alongside _DB_SCHEMA in sqlite_store.py is safe.
_AUTH_DDL = """
CREATE TABLE IF NOT EXISTS auth_tokens (
    token_id              TEXT PRIMARY KEY,
    token_sha256          TEXT NOT NULL UNIQUE,
    name                  TEXT,
    totp_secret_encrypted BLOB,
    created_at            TEXT NOT NULL,
    expires_at            TEXT,
    disabled_at           TEXT,
    failure_count         INTEGER NOT NULL DEFAULT 0,
    totp_failure_count    INTEGER NOT NULL DEFAULT 0,
    totp_last_step        INTEGER,
    revoked               INTEGER NOT NULL DEFAULT 0,
    totp_required         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    session_sha256 TEXT PRIMARY KEY,
    token_id       TEXT NOT NULL REFERENCES auth_tokens(token_id) ON DELETE CASCADE,
    created_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    client_ip      TEXT
);

CREATE TABLE IF NOT EXISTS auth_challenges (
    challenge_id TEXT PRIMARY KEY,
    token_id     TEXT NOT NULL REFERENCES auth_tokens(token_id) ON DELETE CASCADE,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    attempts     INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_auth_tokens_sha256      ON auth_tokens(token_sha256);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires   ON auth_sessions(expires_at);
CREATE INDEX IF NOT EXISTS idx_auth_challenges_expires ON auth_challenges(expires_at);
"""


def hash_token(token: str) -> str:
    """SHA-256 hex digest of a plaintext token or session string."""
    return hashlib.sha256(token.encode()).hexdigest()


class AuthStore:
    """Thread-safe auth storage backed by the shared Vesma SQLite database."""

    def __init__(self, db_path: Path) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(_AUTH_DDL)
            self._ensure_columns()
            self._conn.commit()

    def _ensure_columns(self) -> None:
        """Idempotently add columns introduced after the initial DDL.

        ``CREATE TABLE IF NOT EXISTS`` does NOT add new columns to an
        existing table, so columns added in later revisions need an
        explicit ALTER on first open.
        """
        cur = self._conn.execute("PRAGMA table_info(auth_tokens)")
        existing = {row["name"] for row in cur.fetchall()}
        if "totp_last_step" not in existing:
            self._conn.execute("ALTER TABLE auth_tokens ADD COLUMN totp_last_step INTEGER")
        if "revoked" not in existing:
            self._conn.execute(
                "ALTER TABLE auth_tokens ADD COLUMN revoked INTEGER NOT NULL DEFAULT 0"
            )
            # auth-9 one-shot data migration: retire the legacy literal that
            # used to overload disabled_at. Runs exactly once per database,
            # right after the column is added.
            self._conn.execute("UPDATE auth_tokens SET revoked = 1 WHERE disabled_at = 'permanent'")
            self._conn.execute(
                "UPDATE auth_tokens SET disabled_at = NULL WHERE disabled_at = 'permanent'"
            )
        if "totp_required" not in existing:
            self._conn.execute(
                "ALTER TABLE auth_tokens ADD COLUMN totp_required INTEGER NOT NULL DEFAULT 1"
            )

    # ── Token management ──────────────────────────────────────────────────────

    def create_token(
        self,
        name: str | None = None,
        expires_at: str | None = None,
        totp_required: bool = True,
    ) -> tuple[str, str]:
        """Mint a new bearer token.

        Returns ``(token_id, plaintext_bearer)``.  The plaintext is shown
        **once** — only its SHA-256 hash is stored.

        When ``totp_required`` is ``False`` the token can be used directly as
        a bearer for API requests (no login/verify/session flow needed). The
        default ``True`` preserves backward-compatible TOTP-enforcement.
        """
        plaintext = TOKEN_PREFIX + secrets.token_urlsafe(32)
        sha256 = hash_token(plaintext)
        token_id = "tid_" + secrets.token_hex(8)
        now = datetime.now(UTC).isoformat()
        totp_flag = 1 if totp_required else 0
        with self._lock:
            self._conn.execute(
                "INSERT INTO auth_tokens "
                "(token_id, token_sha256, name, created_at, expires_at, totp_required) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (token_id, sha256, name, now, expires_at, totp_flag),
            )
            self._conn.commit()
        return token_id, plaintext

    def list_tokens(self) -> list[dict[str, object]]:
        """Return all tokens (hash and secret fields excluded)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT token_id, name, created_at, expires_at, disabled_at, revoked, "
                "failure_count, totp_failure_count, totp_required "
                "FROM auth_tokens ORDER BY created_at"
            )
            return [dict(row) for row in cur.fetchall()]

    def get_token_by_hash(self, sha256_hex: str) -> dict[str, object] | None:
        """Look up a token row by the SHA-256 of the plaintext bearer."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM auth_tokens WHERE token_sha256 = ?",
                (sha256_hex,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def get_token_by_id(self, token_id: str) -> dict[str, object] | None:
        """Look up a token row by its ``token_id``."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM auth_tokens WHERE token_id = ?",
                (token_id,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def is_token_active(self, token_row: dict[str, object]) -> bool:
        """Return ``True`` iff the token is neither permanently revoked nor
        currently in a temporary lockout, nor expired."""
        if token_row.get("revoked"):
            return False
        disabled_at = token_row.get("disabled_at")
        if disabled_at is not None:
            try:
                disabled_dt = datetime.fromisoformat(str(disabled_at))
                if datetime.now(UTC) < disabled_dt + timedelta(minutes=LOCKOUT_MINUTES):
                    return False
                # Lockout window expired — auto-clear
                self._clear_lockout(str(token_row["token_id"]))
            except ValueError:
                return False
        expires_at = token_row.get("expires_at")
        if expires_at is not None:
            try:
                if datetime.now(UTC) > _parse_datetime_utc(str(expires_at)):
                    return False
            except ValueError:
                return False
        return True

    def _clear_lockout(self, token_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE auth_tokens "
                "SET disabled_at = NULL, failure_count = 0, totp_failure_count = 0 "
                "WHERE token_id = ?",
                (token_id,),
            )
            self._conn.commit()

    def increment_login_failure(self, token_id: str) -> int:
        """Increment ``failure_count`` after a bad bearer lookup.

        Locks the token when ``LOGIN_LOCKOUT_THRESHOLD`` is reached.
        Returns the new count.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE auth_tokens SET failure_count = failure_count + 1 WHERE token_id = ?",
                (token_id,),
            )
            self._conn.commit()
            cur = self._conn.execute(
                "SELECT failure_count FROM auth_tokens WHERE token_id = ?",
                (token_id,),
            )
            row = cur.fetchone()
            count: int = int(row["failure_count"]) if row else 0
            if count >= LOGIN_LOCKOUT_THRESHOLD:
                self._conn.execute(
                    "UPDATE auth_tokens SET disabled_at = ? WHERE token_id = ?",
                    (datetime.now(UTC).isoformat(), token_id),
                )
                self._conn.commit()
                logger.warning("auth: login lockout for token_id=%s (failures=%d)", token_id, count)
        return count

    def increment_totp_failure(self, token_id: str) -> int:
        """Increment ``totp_failure_count`` after a bad TOTP code.

        Locks the token when ``TOTP_LOCKOUT_THRESHOLD`` is reached.
        Returns the new count.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE auth_tokens "
                "SET totp_failure_count = totp_failure_count + 1 WHERE token_id = ?",
                (token_id,),
            )
            self._conn.commit()
            cur = self._conn.execute(
                "SELECT totp_failure_count FROM auth_tokens WHERE token_id = ?",
                (token_id,),
            )
            row = cur.fetchone()
            count: int = int(row["totp_failure_count"]) if row else 0
            if count >= TOTP_LOCKOUT_THRESHOLD:
                self._conn.execute(
                    "UPDATE auth_tokens SET disabled_at = ? WHERE token_id = ?",
                    (datetime.now(UTC).isoformat(), token_id),
                )
                self._conn.commit()
                logger.warning("auth: TOTP lockout for token_id=%s (failures=%d)", token_id, count)
        return count

    def reset_failures(self, token_id: str) -> None:
        """Reset all failure counters on successful authentication."""
        with self._lock:
            self._conn.execute(
                "UPDATE auth_tokens "
                "SET failure_count = 0, totp_failure_count = 0 WHERE token_id = ?",
                (token_id,),
            )
            self._conn.commit()

    def revoke_token(self, token_id: str) -> bool:
        """Permanently revoke a token.  Returns ``True`` if the row existed."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE auth_tokens SET revoked = 1 WHERE token_id = ?",
                (token_id,),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def set_totp_secret(self, token_id: str, encrypted_secret: bytes) -> None:
        """Store an encrypted TOTP secret blob for a token."""
        with self._lock:
            self._conn.execute(
                "UPDATE auth_tokens SET totp_secret_encrypted = ? WHERE token_id = ?",
                (encrypted_secret, token_id),
            )
            self._conn.commit()

    def clear_totp_secret(self, token_id: str) -> None:
        """Remove the TOTP secret (disables TOTP for this token)."""
        with self._lock:
            self._conn.execute(
                "UPDATE auth_tokens SET totp_secret_encrypted = NULL WHERE token_id = ?",
                (token_id,),
            )
            self._conn.commit()

    def get_totp_last_step(self, token_id: str) -> int | None:
        """Return the last TOTP step index accepted for this token (auth-5)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT totp_last_step FROM auth_tokens WHERE token_id = ?",
                (token_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        value = row["totp_last_step"]
        return int(value) if value is not None else None

    def set_totp_last_step(self, token_id: str, step: int) -> None:
        """Record the last successfully verified TOTP step (auth-5 replay guard)."""
        with self._lock:
            self._conn.execute(
                "UPDATE auth_tokens SET totp_last_step = ? WHERE token_id = ?",
                (int(step), token_id),
            )
            self._conn.commit()

    # ── Challenges ────────────────────────────────────────────────────────────

    def create_challenge(self, token_id: str, ttl_sec: int = CHALLENGE_TTL_SEC) -> str:
        """Create a single-use TOTP challenge.

        Any previous challenge for the same token is purged first (one active
        challenge per token at a time).  Returns the new ``challenge_id``.
        """
        challenge_id = "chg_" + secrets.token_hex(16)
        now = datetime.now(UTC)
        expires = (now + timedelta(seconds=ttl_sec)).isoformat()
        with self._lock:
            self._conn.execute(
                "DELETE FROM auth_challenges WHERE token_id = ?",
                (token_id,),
            )
            self._conn.execute(
                "INSERT INTO auth_challenges "
                "(challenge_id, token_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (challenge_id, token_id, now.isoformat(), expires),
            )
            self._conn.commit()
        return challenge_id

    def get_challenge(self, challenge_id: str) -> dict[str, object] | None:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM auth_challenges WHERE challenge_id = ?",
                (challenge_id,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def is_challenge_valid(self, challenge_row: dict[str, object]) -> bool:
        """Return ``True`` iff the challenge is unexpired and has attempts remaining."""
        try:
            expires = _parse_datetime_utc(str(challenge_row["expires_at"]))
        except ValueError:
            return False
        if datetime.now(UTC) > expires:
            return False
        return int(str(challenge_row.get("attempts", 0))) < CHALLENGE_MAX_ATTEMPTS

    def increment_challenge_attempts(self, challenge_id: str) -> int:
        """Increment the per-challenge attempt counter.  Returns new count."""
        with self._lock:
            self._conn.execute(
                "UPDATE auth_challenges SET attempts = attempts + 1 WHERE challenge_id = ?",
                (challenge_id,),
            )
            self._conn.commit()
            cur = self._conn.execute(
                "SELECT attempts FROM auth_challenges WHERE challenge_id = ?",
                (challenge_id,),
            )
            row = cur.fetchone()
        return int(row["attempts"]) if row else 0

    def invalidate_challenge(self, challenge_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM auth_challenges WHERE challenge_id = ?",
                (challenge_id,),
            )
            self._conn.commit()

    # ── Sessions ──────────────────────────────────────────────────────────────

    def create_session(
        self,
        token_id: str,
        ttl_sec: int,
        client_ip: str | None = None,
    ) -> tuple[str, str]:
        """Create a session token.

        Returns ``(plaintext_session_token, expires_at_iso)``.  The plaintext
        is placed in the ``Set-Cookie`` header and JSON body.  Only the
        SHA-256 hash is stored.
        """
        plaintext = secrets.token_urlsafe(32)
        sha256 = hash_token(plaintext)
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=ttl_sec)
        expires_iso = expires.isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT INTO auth_sessions "
                "(session_sha256, token_id, created_at, expires_at, last_seen_at, client_ip) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sha256, token_id, now.isoformat(), expires_iso, now.isoformat(), client_ip),
            )
            self._conn.commit()
        return plaintext, expires_iso

    def get_session_by_hash(self, sha256_hex: str) -> dict[str, object] | None:
        """Look up a session by the SHA-256 of the session token."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM auth_sessions WHERE session_sha256 = ?",
                (sha256_hex,),
            )
            row = cur.fetchone()
        return dict(row) if row else None

    def is_session_valid(
        self,
        session_row: dict[str, object],
        client_ip: str | None = None,
        session_pin_ip: bool = False,
    ) -> bool:
        """Return ``True`` iff the session is unexpired (and IP-pinned if configured).

        auth-12: also rejects sessions older than ``MAX_SESSION_LIFETIME_SEC``
        from ``created_at``, regardless of how often ``touch_session`` slid
        ``expires_at`` forward.
        """
        try:
            expires = _parse_datetime_utc(str(session_row["expires_at"]))
            created = _parse_datetime_utc(str(session_row["created_at"]))
        except ValueError:
            return False
        now = datetime.now(UTC)
        if now > expires:
            return False
        if now > created + timedelta(seconds=MAX_SESSION_LIFETIME_SEC):
            return False
        return not session_pin_ip or client_ip is None or session_row.get("client_ip") == client_ip

    def touch_session(self, sha256_hex: str, ttl_sec: int) -> None:
        """Slide the session TTL forward (refresh on each authenticated request).

        auth-12: the new ``expires_at`` is clamped so it can never exceed
        ``created_at + MAX_SESSION_LIFETIME_SEC``.
        """
        now = datetime.now(UTC)
        with self._lock:
            cur = self._conn.execute(
                "SELECT created_at FROM auth_sessions WHERE session_sha256 = ?",
                (sha256_hex,),
            )
            row = cur.fetchone()
            if row is None:
                return
            try:
                created = datetime.fromisoformat(str(row["created_at"]))
            except ValueError:
                return
            absolute_cap = created + timedelta(seconds=MAX_SESSION_LIFETIME_SEC)
            sliding = now + timedelta(seconds=ttl_sec)
            new_expires = min(sliding, absolute_cap)
            self._conn.execute(
                "UPDATE auth_sessions SET last_seen_at = ?, expires_at = ? "
                "WHERE session_sha256 = ?",
                (now.isoformat(), new_expires.isoformat(), sha256_hex),
            )
            self._conn.commit()

    def revoke_session(self, sha256_hex: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM auth_sessions WHERE session_sha256 = ?",
                (sha256_hex,),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
