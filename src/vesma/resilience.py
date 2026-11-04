"""Resilience plane (ADR-0051, wave W4) — degraded write mode, spool, replay.

When the core's MCP plane is unreachable, harness writes must not vanish
(the 29.10 incident class: a week of records written around the prod,
noticed by accident). ADR-0051 ratifies an explicit state machine with a
durable spool, a verified replay, a TTL and loud signage; THIS module is
its single implementation point ("единый модуль resilience" — the same
state resolver serves the front, CLI writers and hooks; no ad-hoc
spools).

The machine (ADR-0051 §Решение)::

    HEALTHY ──(N failed probes, default 3)──▶ DEGRADED
    DEGRADED ──(probe OK)──▶ REPLAY ──(100% verified, spool removed)──▶ HEALTHY
    DEGRADED ──(TTL expired)──▶ BLOCKED_WRITES ──(core recovered)──▶ REPLAY

Pieces, in file order:

* :class:`ResilienceMode` / :class:`DegradedTTLExceeded` /
  :class:`SpoolQuotaExceeded` — the vocabulary and the typed refusals
  (a BLOCKED_WRITES refusal never leaks spool paths or contents);
* :func:`parse_duration` — the systemd-style duration grammar shared by
  the config layer and the front's env.d slot (``7d``, ``12h``, ``30m``,
  ``600s``, bare seconds);
* the op classifier (:data:`WRITE_TOOLS`, :func:`classify_tool_call`) —
  WHICH ``tools/call`` names are durable writes (the only calls that
  enter the spool; reads stay transparent and 503 honestly);
* :class:`Spool` — the append-only JSONL spool with a per-row SHA-256
  hash chain, fsync on every append, 0600/0700 tree, secret-scan at
  write (the row is MARKED, never silently accepted unflagged), byte
  quota accounting, torn-tail recovery, atomic done-rename;
* :class:`MachineState` + :class:`StateStore` — the persisted state file
  + flock in the state home, the header-vs-state arbitration (a valid
  spool header OLDER than the state file wins — a crash between spool
  creation and the state write must not lose the degraded context), the
  wall-clock TTL with the time-jump detector (``last_seen_wall`` in the
  state; a shift beyond :data:`TIME_JUMP_THRESHOLD_SEC` is signposted
  and the TTL decision logged);
* :class:`ReplayReport` + :func:`run_replay` — the verified replay under
  the FRONT SERVICE TOKEN (the front never stores harness tokens — C2 —
  so per-row client re-authz is approximated: a refused service token
  aborts the run with nothing applied; a per-actor policy seam exists
  for a future core status API), counters
  planned/replayed/superseded/rejected-reauthz/failed, chain
  verification, secret re-scan, tail-skip tolerance, and the atomic
  done-rename that commits the spool's removal only after 100%;
* :class:`FrontResilienceSettings` — the front child's env.d config
  (fail-closed: no ``VESMA_FRONT_DEGRADED_MAX_DURATION`` in the env
  slot ⇒ the front boots refuse-writes with a loud line, never spooling
  silently);
* :class:`ResilienceCoordinator` — the front-side orchestrator: probe
  observations (request outcomes double as probes), the poll loop with
  backoff (30 s → 10 m), signage (WARN on entering DEGRADED, then every
  ``log_interval``), the best-effort notify webhook (DEGRADED entry and
  the 80% TTL crossing), the write interception that spools and answers
  a receipt, and the healthz snapshot.

Invariants that must survive refactors (pin-tested):

* the spool NEVER becomes a second engine: replay goes through the
  core's own write-API plane (the same ``tools/call`` dispatch) —
  writer-lock, mint-protection and the write-path scans all apply;
* write-only until replay: spool contents never enter any read path;
* refusals never leak: :class:`DegradedTTLExceeded` /
  :class:`SpoolQuotaExceeded` messages carry no paths, no op contents;
* secrets are scanned at spool-write time (the never-weaker rule:
  degradation must not buy a weaker guarantee than the normal path)
  and again at replay;
* no metrics here — the metrics axis is a separate line (ADR-0051
  signage matrix row «Метрики», its own card).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Format version of the on-disk spool artifacts (header + state).
SPOOL_FORMAT: Final[int] = 1

#: Default TTL of the degraded mode (the DOCUMENTED recommendation; the
#: VALUE itself is mandatory — a deployment that does not name it runs
#: refuse-writes, fail-closed per ADR-0051).
DEFAULT_MAX_DURATION_SEC: Final[int] = 7 * 24 * 3600

#: Default probe cadence and its backoff ceiling (ADR-0051: poll 30 s,
#: backoff to 10 m).
DEFAULT_POLL_INTERVAL_SEC: Final[int] = 30
DEFAULT_BACKOFF_MAX_SEC: Final[int] = 600

#: Default spool quota (ADR-0051: 1 GiB; exhaustion ⇒ BLOCKED_WRITES).
DEFAULT_SPOOL_MAX_BYTES: Final[int] = 1024 * 1024 * 1024

#: Default signage cadence (ADR-0051: every 15 m after the entry WARN).
DEFAULT_SIGNAGE_INTERVAL_SEC: Final[int] = 900

#: How many consecutive failed probes flip HEALTHY → DEGRADED.
DEFAULT_PROBE_FAILURE_THRESHOLD: Final[int] = 3

#: Wall-clock shift beyond which the time-jump detector signposts.
#: Covers suspend/resume and NTP corrections; the TTL itself ALWAYS
#: evaluates on the wall clock from the persisted ``degraded_since`` —
#: the jump only controls signage + the logged decision.
TIME_JUMP_THRESHOLD_SEC: Final[int] = 6 * 3600

#: TTL consumption fraction beyond which the notify webhook fires (the
#: escalation ladder's WARN leg) and the doctor escalates to FAIL
#: (ADR-0051 signage matrix: the 50/80/100% ladder).
TTL_FAIL_FRACTION: Final[float] = 0.8


# ── Vocabulary + typed refusals ───────────────────────────────────────


class ResilienceMode:
    """The four machine states (plain string constants keep the wire and
    log forms stable)."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    REPLAY = "REPLAY"
    BLOCKED_WRITES = "BLOCKED_WRITES"


#: All states, for membership checks and the doctor's report.
MODES: Final[frozenset[str]] = frozenset(
    {
        ResilienceMode.HEALTHY,
        ResilienceMode.DEGRADED,
        ResilienceMode.REPLAY,
        ResilienceMode.BLOCKED_WRITES,
    }
)


class DegradedTTLExceeded(Exception):  # noqa: N818 - the ADR-ratified name
    """A write refused because the degraded TTL expired (BLOCKED_WRITES).

    The message is a FIXED typed refusal: no spool paths, no operation
    contents, no instance internals. The frozen rows stay in the spool
    forever — never auto-deleted (ADR-0051 §BLOCKED_WRITES).
    """

    def __init__(self, expired_by_sec: int) -> None:
        self.expired_by_sec = expired_by_sec
        super().__init__(
            "DegradedTTLExceeded: the degraded-mode TTL has expired; writes are "
            "blocked. Frozen operations wait in the spool for a managed replay — "
            "reads remain available (stale)."
        )


class SpoolQuotaExceeded(Exception):  # noqa: N818 - the ADR-ratified name
    """A write refused because the spool quota is exhausted.

    Same refusal discipline: no paths, no contents. The machine moves
    to BLOCKED_WRITES until the spool is drained by a replay.
    """

    def __init__(self, limit_bytes: int) -> None:
        self.limit_bytes = limit_bytes
        super().__init__(
            "SpoolQuotaExceeded: the degraded-mode spool has reached its "
            "configured byte quota; writes are blocked until the spool is "
            "drained by a replay."
        )


class ReplayAuthzError(Exception):
    """The core refused the replay IDENTITY (the front service token).

    Raised by the replay's write leg; aborts the whole run — nothing
    further is applied, the spool stays intact, the report counts the
    remaining rows as ``rejected-reauthz``.
    """


class ReplayTransientError(Exception):
    """The replay leg hit TRANSIENT trouble — the row must stay in the
    spool and retry on a later pass (F-N1, W4 final slice).

    Raised by the production write leg for the INFRA-CLASS outcomes that
    are NOT semantic refusals: a non-200 core answer (502/503/504 —
    overload, restart), an unparsable/truncated answer, and the core's
    GENERIC ``❌ Error: …`` rendering (``mcp_server``'s catch-all —
    store locked, disk full, store still opening). The replay's
    ``except Exception`` leg maps this to ``failed`` + fail-stop: the
    row stays, nothing is pocketed, nothing is lost. ONLY genuine
    semantic refusals (namespace violation, tag contract, invalid
    status — the typed renderings) may travel the ``rejected-permanent``
    pocket.
    """


# ── Duration grammar (systemd-style, shared by config + env.d) ────────

_DURATION_SUFFIXES: Final[dict[str, int]] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 7 * 86400,
}


def parse_duration(value: str | int) -> int:
    """Parse a systemd-style duration into whole seconds.

    Accepts ``7d`` / ``12h`` / ``30m`` / ``600s`` / ``2w`` and bare
    integers (seconds). Returns a POSITIVE int; anything else raises
    :class:`ValueError` (fail-closed — callers refuse to boot on it).
    """
    if isinstance(value, bool):  # bool is an int subclass — refuse it
        raise ValueError(f"duration must be a number or <n>(s|m|h|d|w), got {value!r}")
    if isinstance(value, int):
        seconds = value
    else:
        text = str(value).strip().lower()
        if not text:
            raise ValueError("duration must not be empty")
        suffix = text[-1:]
        if suffix in _DURATION_SUFFIXES and text[:-1].isdigit():
            seconds = int(text[:-1]) * _DURATION_SUFFIXES[suffix]
        elif text.isdigit():
            seconds = int(text)
        else:
            raise ValueError(f"duration must be <n>(s|m|h|d|w) or plain seconds, got {value!r}")
    if seconds <= 0:
        raise ValueError(f"duration must be positive, got {value!r}")
    return seconds


# ── Op classifier: which tools/call names are durable writes ──────────

#: Tool names whose call MUTATES durable store state (memory rows, tag
#: registries, cache rows, server-side registrations). Only these enter
#: the spool on a degraded front; everything else is a read and stays
#: transparent (the honest 503). The classifier is pinned against the
#: canonical tool registry by tests (every registered name classified).
WRITE_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "vesma_add",
        "vesma_filter",  # re-filters a memory → clean_content write
        "vesma_save_context",
        "vesma_tags_rename",
        "vesma_import",  # merge-mode import writes rows
        "vesma_reprocess",  # pipeline state mutation
        "vesma_context_rewrite",  # stores a rewrite event
        "vesma_compress",  # CCR cache rows are durable store state
        "vesma_ingest_url",
        "vesma_ingest_document",
        "vesma_watch_start",  # server-side registration rows
        "vesma_watch_stop",
    }
)

#: Dual-mode tools: the ``action`` argument decides. A name not listed
#: here and not in :data:`WRITE_TOOLS` is a READ.
_DUAL_MODE_TOOLS: Final[dict[str, tuple[str, frozenset[str]]]] = {
    # (argument key, argument values that make the call a WRITE)
    "vesma_tags": ("action", frozenset({"rename", "remove", "add"})),
    "vesma_workflow": ("action", frozenset({"set"})),
    "vesma_awareness": ("action", frozenset({"record_abstention"})),
    "vesma_hooks": ("action", frozenset({"post_tool_call"})),
}


def classify_tool_call(name: str, arguments: Mapping[str, Any] | None) -> str:
    """Return ``"write"`` or ``"read"`` for a ``tools/call``.

    Dual-mode tools route on their action argument (``vesma_tags`` with
    ``action=get`` is a read); a dual tool with a MISSING/unreadable
    action classifies as a WRITE (fail-safe direction: an unclassified
    potential write must never be dropped — spooling a read by mistake
    costs one harmless replayed call, dropping a write costs data).
    Unknown tool names are READS (the front does not guess; the core
    answers them normally once it is back).
    """
    if name in WRITE_TOOLS:
        return "write"
    dual = _DUAL_MODE_TOOLS.get(name)
    if dual is None:
        return "read"
    key, write_values = dual
    args = arguments or {}
    action = args.get(key)
    if not isinstance(action, str):
        return "write"  # unclassifiable — fail safe (never drop a write)
    return "write" if action in write_values else "read"


def tool_call_from_body(body: bytes) -> tuple[str, dict[str, Any], Any] | None:
    """Extract ``(tool_name, arguments, jsonrpc_id)`` from a JSON-RPC body.

    Returns ``None`` for anything that is not a ``tools/call`` request
    (other JSON-RPC verbs are reads; unparsable bodies are reads — the
    core answers them with its own error once it is back). Never raises
    on client input.
    """
    try:
        request = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(request, dict) or request.get("method") != "tools/call":
        return None
    params = request.get("params")
    if not isinstance(params, dict):
        return None
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return None
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    return name, arguments, request.get("id")


def jsonrpc_result(jsonrpc_id: Any, text: str, *, is_error: bool = False) -> dict[str, Any]:
    """A minimal MCP ``tools/call`` result envelope (receipt / refusal).

    Shaped so SDK-typed clients parse it as a normal TextContent tool
    result; the degraded metadata rides IN the text (a human-readable
    receipt), never as extra envelope keys (strict clients would choke).
    """
    return {
        "jsonrpc": "2.0",
        "id": jsonrpc_id,
        "result": {
            "content": [{"type": "text", "text": text}],
            "isError": is_error,
        },
    }


# ── Small helpers ─────────────────────────────────────────────────────


def sanitize_label(label: str, *, what: str) -> str:
    """Validate a path label (instance / actor) for the spool tree.

    The spool path is built from these labels — a hostile or careless
    label must never escape the tree (``..``, separators, empty). The
    grammar matches the instance-id / agent-slug families already in the
    platform.
    """
    clean = label.strip()
    if not clean or len(clean) > 128:
        raise ValueError(f"{what}: must be 1..128 chars, got {label!r}")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
    if not set(clean) <= allowed or clean.startswith("."):
        raise ValueError(f"{what}: only [A-Za-z0-9._-] (no leading dot) allowed, got {label!r}")
    return clean


def resolve_state_home(env: Mapping[str, str] | None = None) -> Path:
    """The state home for the spool tree (XDG semantics).

    ``VESMA_STATE_HOME`` > ``XDG_STATE_HOME`` > ``~/.local/state``. The
    spool lands at ``<state_home>/vesma/spool/`` — deliberately OUTSIDE
    every store home (invariant C6: the resilience plane must not leave
    files in a store home).
    """
    source = os.environ if env is None else env
    raw = source.get("VESMA_STATE_HOME") or source.get("XDG_STATE_HOME")
    if raw:
        return Path(raw).expanduser()
    home = source.get("HOME")
    if home:
        return Path(home) / ".local" / "state"
    return Path.home() / ".local" / "state"


def resolve_instance(
    env: Mapping[str, str] | None = None, *, store_home: Path | None = None
) -> str:
    """The spool's ``<instance>`` label for this front process.

    ``VESMA_FRONT_INSTANCE`` > the persisted instance-id file at the
    (default) store home > the literal ``default`` (the default
    instance's plane name — the same vocabulary the core's aud binding
    uses). Fail-soft: a damaged id file degrades to ``default`` (the
    label only keys the spool directory; it is not an authz input).
    """
    source = os.environ if env is None else env
    explicit = source.get("VESMA_FRONT_INSTANCE")
    if explicit:
        return sanitize_label(explicit, what="VESMA_FRONT_INSTANCE")
    id_file = (store_home or Path("~/.vesma/data").expanduser()) / "instance-id"
    try:
        candidate = id_file.read_text(encoding="utf-8").strip()
    except OSError:
        return "default"
    if not candidate:
        return "default"
    try:
        return sanitize_label(candidate, what="instance-id")
    except ValueError:
        return "default"


def _canonical_json(obj: Any) -> str:
    """Byte-stable JSON for hashing and persistence (sorted keys, tight)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _row_hash(prev_hash: str | None, core: dict[str, Any]) -> str:
    material = _canonical_json({"prev": prev_hash, **core})
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _utc_iso(epoch: float) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _parse_ts(ts_utc: str) -> float:
    """ISO-8601 → epoch seconds; 0.0 for a damaged stamp (the header's
    wall-clock authority is the epoch float stored alongside)."""
    try:
        return datetime.fromisoformat(ts_utc.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


def _fsync_dir(path: Path) -> None:
    """fsync a directory so a rename/creation survives power loss."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _ensure_private_dir(directory: Path) -> None:
    from vesma.fs_hardening import ensure_private_dir

    ensure_private_dir(directory)


def _atomic_write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (0600, fsync, rename)."""
    _ensure_private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


# ── Spool (ADR-0051 §Спул) ────────────────────────────────────────────


@dataclass(frozen=True)
class SpoolRow:
    """One spooled write operation (one JSONL line).

    HONEST LIMIT (W4 cascade review, F8): ``secret_flagged`` is advisory
    metadata OUTSIDE the chain hash — :meth:`chain_core` covers only the
    op fields — so the same uid that wrote the row can strip or forge
    the flag, and NO code path consumes it today (the replay re-scans
    the payload itself; the flag exists for human inspection of the
    spool file). It is a hint, never a control.
    """

    op_id: str
    ts_utc: str
    ts: float
    actor: str
    op: str
    payload: dict[str, Any]
    prev_hash: str | None
    row_hash: str
    secret_flagged: tuple[str, ...] = ()

    def chain_core(self) -> dict[str, Any]:
        """Exactly the persisted fields the row hash covers."""
        return {
            "op_id": self.op_id,
            "ts_utc": self.ts_utc,
            "actor": self.actor,
            "op": self.op,
            "payload": self.payload,
        }

    def to_json(self) -> str:
        row: dict[str, Any] = {
            "op_id": self.op_id,
            "ts_utc": self.ts_utc,
            "actor": self.actor,
            "op": self.op,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "row_hash": self.row_hash,
        }
        if self.secret_flagged:
            row["secret_flagged"] = list(self.secret_flagged)
        return _canonical_json(row)

    @classmethod
    def from_json(cls, line: str) -> SpoolRow | None:
        """Parse one line; ``None`` for a torn/invalid line (the caller
        decides whether that line is a skippable tail or a crime)."""
        try:
            raw = json.loads(line)
        except ValueError:
            return None
        if not isinstance(raw, dict):
            return None
        for key in ("op_id", "ts_utc", "actor", "op", "row_hash"):
            if not isinstance(raw.get(key), str) or not raw[key]:
                return None
        if not isinstance(raw.get("payload"), dict):
            return None
        prev = raw.get("prev_hash")
        if not isinstance(prev, str) and prev is not None:
            return None
        flagged = raw.get("secret_flagged")
        return cls(
            op_id=raw["op_id"],
            ts_utc=raw["ts_utc"],
            ts=_parse_ts(raw["ts_utc"]),
            actor=raw["actor"],
            op=raw["op"],
            payload=dict(raw["payload"]),
            prev_hash=prev,
            row_hash=raw["row_hash"],
            secret_flagged=tuple(flagged) if isinstance(flagged, list) else (),
        )


@dataclass
class SpoolScan:
    """The F3-faithful read of one ops.jsonl.

    ``torn_tail_bytes`` counts ONLY the trailing segment without its
    terminating newline (the power-loss class — the sole class auto-
    repair may touch). ``broken_lines`` lists the indices of COMPLETE
    lines that do not parse — hard corruption, kept as evidence, replay
    aborts loudly; never auto-truncated.
    """

    rows: list[SpoolRow]
    torn_tail_bytes: int
    broken_lines: list[int]


class Spool:
    """One actor's append-only JSONL spool under ``<root>/<instance>/<actor>/``.

    Layout (``root`` = ``<state_home>/vesma/spool``)::

        <root>/reconcile.lock            # HOST-level replay serialization
        <root>/<instance>/state.lock     # per-instance machine-state flock
        <root>/<instance>/state.json     # machine state (the cache)
        <root>/<instance>/<actor>/header.json  # the truth anchor
        <root>/<instance>/<actor>/ops.jsonl    # the hash-chained rows

    Every directory is 0700, every file 0600; appends fsync; the hash
    chain makes truncation/tampering detectable; the done-rename makes
    "fully replayed" an atomic filesystem fact.
    """

    HEADER_NAME = "header.json"
    OPS_NAME = "ops.jsonl"
    DONE_SUFFIX = ".replayed"

    def __init__(self, directory: Path, *, actor: str) -> None:
        self.dir = directory
        self.actor = actor
        self._head: str | None = None
        self._append_offset = 0

    # ── construction / discovery ──────────────────────────────────

    @property
    def header_path(self) -> Path:
        return self.dir / self.HEADER_NAME

    @property
    def ops_path(self) -> Path:
        return self.dir / self.OPS_NAME

    def exists(self) -> bool:
        return self.header_path.exists()

    @classmethod
    def open(
        cls,
        spool_root: Path,
        *,
        instance: str,
        actor: str,
        degraded_since: float,
        max_duration_sec: int,
        clock: Callable[[], float] | None = None,
    ) -> Spool:
        """Open (create-if-absent) an actor spool for a degradation.

        The header is written and fsynced FIRST when absent — it is the
        arbitration anchor (a crash after this point must still resolve
        to DEGRADED). Re-opening an existing degradation recovers the
        chain head and repairs a torn tail from a dead append.
        """
        now = (clock or time.time)()
        instance = sanitize_label(instance, what="instance")
        actor = sanitize_label(actor, what="actor")
        directory = spool_root / instance / actor
        _ensure_private_dir(directory)
        spool = cls(directory, actor=actor)
        if not spool.exists():
            header = {
                "spool_format": SPOOL_FORMAT,
                "instance": instance,
                "actor": actor,
                "degraded_since": degraded_since,
                "degraded_since_utc": _utc_iso(degraded_since),
                "ttl_sec": max_duration_sec,
                "opened_at": now,
                "chain_origin": None,
            }
            _atomic_write_private(spool.header_path, _canonical_json(header))
            _fsync_dir(directory)
            logger.warning(
                "resilience: spool opened instance=%s actor=%s since=%s ttl=%ds",
                instance,
                actor,
                header["degraded_since_utc"],
                max_duration_sec,
            )
        else:
            spool.recover_for_append()
        return spool

    @classmethod
    def discover(cls, instance_dir: Path) -> list[Spool]:
        """Every actor spool currently on disk for the instance (sorted)."""
        if not instance_dir.is_dir():
            return []
        found: list[Spool] = []
        for child in sorted(instance_dir.iterdir()):
            if child.is_dir() and (child / cls.HEADER_NAME).exists():
                found.append(cls(child, actor=child.name))
        return found

    # ── header ────────────────────────────────────────────────────

    def header(self) -> dict[str, Any] | None:
        """The parsed header, or ``None`` when absent/unparsable (an
        unparsable header is NOT a valid arbitration anchor)."""
        try:
            raw = json.loads(self.header_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return raw if isinstance(raw, dict) else None

    # ── appends (write-only until replay) ─────────────────────────

    def append(
        self,
        *,
        op: str,
        payload: dict[str, Any],
        clock: Callable[[], float] | None = None,
    ) -> SpoolRow:
        """Append one operation; fsync; return the row (for the receipt).

        ``clock`` threads the OWNER's clock (F7: the coordinator's — so
        row stamps share the state file's notion of now instead of
        diverging through a raw ``time.time()``); ``None`` falls back to
        the wall clock.

        The secret scan runs HERE (never-weaker: degradation must not
        buy a weaker guarantee than the normal write path). Findings
        MARK the row with the pattern NAMES — the values never enter
        the flags, and the row itself still lands (losing the write
        would be the worse harm; the replay re-scans and the core's own
        write-path scan stays the final authority).
        """
        if self._append_offset == 0 and self._head is None:
            self.recover_for_append()
        now = (clock or time.time)()
        from vesma.secrets_detector import detect_secrets

        findings = detect_secrets(_canonical_json(payload))
        flagged = tuple(sorted({f.pattern_name for f in findings}))
        op_id = str(uuid.uuid4())
        row = SpoolRow(
            op_id=op_id,
            ts_utc=_utc_iso(now),
            ts=now,
            actor=self.actor,
            op=op,
            payload=payload,
            prev_hash=self._head,
            row_hash="",
            secret_flagged=flagged,
        )
        row = SpoolRow(
            op_id=row.op_id,
            ts_utc=row.ts_utc,
            ts=row.ts,
            actor=row.actor,
            op=row.op,
            payload=row.payload,
            prev_hash=row.prev_hash,
            row_hash=_row_hash(row.prev_hash, row.chain_core()),
            secret_flagged=flagged,
        )
        encoded = (row.to_json() + "\n").encode("utf-8")
        fd = os.open(self.ops_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        self._head = row.row_hash
        self._append_offset += len(encoded)
        return row

    def recover_for_append(self) -> None:
        """Recover ``(head_hash, append_offset)`` from disk before new
        appends chain onto the file.

        F3 (W4 cascade review): ONLY a torn LAST segment (a partial
        append without its terminating newline — the power-loss class)
        is repaired (truncated, loud line). A COMPLETE line that fails
        to parse is HARD CORRUPTION: it is never truncated here, the
        file keeps it as evidence, and every replay of this spool
        aborts loudly (see :meth:`verify_chain`) until a human resolves
        it — appends continue after the last VALID row.
        """
        scan = self._scan()
        if scan.broken_lines:
            logger.error(
                "resilience: actor=%s has %d corrupted mid-file row(s) — kept as "
                "evidence, file left UNTOUCHED (no auto-repair of any segment); "
                "replay will abort loudly until manual resolution "
                "(ADR-0051 tamper semantics)",
                self.actor,
                len(scan.broken_lines),
            )
            # The head still chains from the last VALID row so a human can
            # append after resolving; the corrupt segments stay mid-file.
            self._head = scan.rows[-1].row_hash if scan.rows else None
            return
        if scan.torn_tail_bytes:
            with self.ops_path.open("r+b") as fh:
                fh.truncate(self._append_offset)
                os.fsync(fh.fileno())
            logger.warning(
                "resilience: repaired torn spool tail (%d bytes dropped) actor=%s",
                scan.torn_tail_bytes,
                self.actor,
            )
        self._head = scan.rows[-1].row_hash if scan.rows else None

    # ── reading / verification (replay + doctor read-only paths) ──

    def read_rows(self) -> tuple[list[SpoolRow], bool]:
        """All rows plus a ``torn_tail`` flag (a torn LAST segment
        tolerated on the read side — skip-and-report per ADR-0051).
        Mid-file corruption is NOT reported here as torn: it surfaces
        through :meth:`verify_chain` as hard chain problems."""
        scan = self._scan()
        return scan.rows, scan.torn_tail_bytes > 0

    def verify_chain(self) -> tuple[list[SpoolRow], list[str]]:
        """Rows + a list of chain problems (empty list = intact).

        A problem is a row whose recomputed hash mismatches, or whose
        ``prev_hash`` does not chain to its predecessor, or a COMPLETE
        line that does not parse at all (F3: hard mid-file corruption —
        the power-loss class is the torn tail only). Any problem ABORTS
        a replay loudly; the rows stay on disk.
        """
        scan = self._scan()
        problems: list[str] = []
        if scan.broken_lines:
            for index in scan.broken_lines:
                problems.append(
                    f"line {index}: complete row does not parse (hard "
                    f"corruption, mid-file) — replay aborted, rows kept"
                )
            # Continuity past the corruption gap is unverifiable — the
            # broken-line problems above already abort the whole spool.
            return scan.rows, problems
        expected_prev: str | None = None
        for index, row in enumerate(scan.rows):
            if row.prev_hash != expected_prev:
                problems.append(
                    f"row {index} ({row.op_id}): prev_hash does not chain to "
                    f"its predecessor (chain break or deletion)"
                )
            if _row_hash(expected_prev, row.chain_core()) != row.row_hash:
                problems.append(f"row {index} ({row.op_id}): row_hash mismatch")
            expected_prev = row.row_hash
        return scan.rows, problems

    def _scan(self) -> SpoolScan:
        """Read ops.jsonl into a :class:`SpoolScan`.

        F3 semantics: a line is TORN only when it is the trailing
        segment WITHOUT its terminating newline (a partial append — the
        only class auto-repair is allowed to touch). A complete line
        that fails to parse is recorded in ``broken_lines`` (hard
        corruption); scanning CONTINUES past it so the surviving rows
        stay visible for reports.
        """
        self._append_offset = 0
        rows: list[SpoolRow] = []
        broken: list[int] = []
        torn = 0
        if not self.ops_path.exists():
            return SpoolScan(rows=rows, torn_tail_bytes=0, broken_lines=broken)
        raw = self.ops_path.read_bytes()
        offset = 0
        line_index = 0
        while offset < len(raw):
            newline = raw.find(b"\n", offset)
            if newline == -1:
                torn = len(raw) - offset
                break
            line = raw[offset:newline]
            row = SpoolRow.from_json(line.decode("utf-8", errors="replace"))
            if row is None:
                broken.append(line_index)
            else:
                rows.append(row)
                self._append_offset = newline + 1
            offset = newline + 1
            line_index += 1
        return SpoolScan(rows=rows, torn_tail_bytes=torn, broken_lines=broken)

    # ── quota accounting ──────────────────────────────────────────

    def bytes_used(self) -> int:
        """ops.jsonl size + header size (the quota's unit of account)."""
        total = 0
        for path in (self.ops_path, self.header_path):
            try:
                total += path.stat().st_size
            except OSError:
                continue
        return total

    # ── atomic completion ─────────────────────────────────────────

    def mark_replayed_and_remove(self) -> None:
        """The atomic commit a 100% verified replay performs.

        Order matters: the RENAME is the commit point (ops.jsonl gone ⇒
        "replayed" is a filesystem fact), the removal is cleanup. A
        crash between them leaves ``ops.jsonl.replayed`` + the header —
        the reconciler's empty-spool recovery clears it; a crash BEFORE
        the rename leaves the rows listable — they replay again (the
        crash-window duplicate-apply residual is accepted for W4: the
        op_id envelope rides in the payload for a future core-side dedup).
        """
        if self.ops_path.exists():
            done = self.ops_path.with_name(self.OPS_NAME + self.DONE_SUFFIX)
            os.replace(self.ops_path, done)
            _fsync_dir(self.dir)
        self.remove_tree()

    def remove_tree(self) -> None:
        """Remove the spool directory (leftover artifacts included)."""
        import shutil

        shutil.rmtree(self.dir, ignore_errors=True)
        with contextlib.suppress(OSError):
            # Drop the instance dir when the last actor left.
            self.dir.parent.rmdir()


# ── Machine state (state file + flock, arbitration, TTL) ──────────────


@dataclass
class MachineState:
    """The persisted machine state (``state.json`` — the CACHE; a valid
    spool header older than this file wins the arbitration)."""

    mode: str = ResilienceMode.HEALTHY
    degraded_since: float | None = None
    ttl_sec: int = DEFAULT_MAX_DURATION_SEC
    probe_failures: int = 0
    poll_backoff_sec: int = DEFAULT_POLL_INTERVAL_SEC
    last_seen_wall: float = 0.0
    last_signage_wall: float = 0.0
    notify_degraded_sent: bool = False
    notify_ttl80_sent: bool = False
    quota_blocked: bool = False
    replay_problems: list[str] = field(default_factory=list)
    # F2 (W4 cascade review) — the PER-ROW COMMIT watermark: actor →
    # op_id of the last permanently-consumed row (applied or
    # rejected-permanent). Persisted after EVERY consumed row, so a
    # crash mid-replay costs at most ONE re-application — the sibling
    # duplicate-multiplier (one poison row re-applying the whole spool
    # every poll) is gone.
    replay_watermarks: dict[str, str] = field(default_factory=dict)
    # The LAST replay report's summary (kept across HEALTHY so DR-24 and
    # the operator keep seeing rejected-permanent pockets after the
    # spool is gone).
    last_replay_summary: str = ""

    def to_json(self) -> str:
        return _canonical_json(
            {
                "state_format": SPOOL_FORMAT,
                "mode": self.mode,
                "degraded_since": self.degraded_since,
                "ttl_sec": self.ttl_sec,
                "probe_failures": self.probe_failures,
                "poll_backoff_sec": self.poll_backoff_sec,
                "last_seen_wall": self.last_seen_wall,
                "last_signage_wall": self.last_signage_wall,
                "notify_degraded_sent": self.notify_degraded_sent,
                "notify_ttl80_sent": self.notify_ttl80_sent,
                "quota_blocked": self.quota_blocked,
                "replay_problems": self.replay_problems,
                "replay_watermarks": self.replay_watermarks,
                "last_replay_summary": self.last_replay_summary,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> MachineState | None:
        """Parse; ``None`` for garbage (the caller falls back to HEALTHY
        — arbitration against a valid header restores the truth)."""
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("mode") not in MODES:
            return None
        state = cls()
        state.mode = str(data["mode"])
        since = data.get("degraded_since")
        if isinstance(since, (int, float)):
            state.degraded_since = float(since)
        ttl = data.get("ttl_sec")
        if isinstance(ttl, (int, float)) and int(ttl) > 0:
            state.ttl_sec = int(ttl)
        failures = data.get("probe_failures")
        if isinstance(failures, (int, float)):
            state.probe_failures = max(0, int(failures))
        backoff = data.get("poll_backoff_sec")
        if isinstance(backoff, (int, float)) and int(backoff) > 0:
            state.poll_backoff_sec = int(backoff)
        seen = data.get("last_seen_wall")
        if isinstance(seen, (int, float)):
            state.last_seen_wall = float(seen)
        signage = data.get("last_signage_wall")
        if isinstance(signage, (int, float)):
            state.last_signage_wall = float(signage)
        state.notify_degraded_sent = bool(data.get("notify_degraded_sent", False))
        state.notify_ttl80_sent = bool(data.get("notify_ttl80_sent", False))
        state.quota_blocked = bool(data.get("quota_blocked", False))
        problems = data.get("replay_problems")
        if isinstance(problems, list):
            state.replay_problems = [str(p) for p in problems]
        watermarks = data.get("replay_watermarks")
        if isinstance(watermarks, dict):
            state.replay_watermarks = {
                str(k): str(v) for k, v in watermarks.items() if isinstance(v, str)
            }
        summary = data.get("last_replay_summary")
        if isinstance(summary, str):
            state.last_replay_summary = summary
        return state


class StateStore:
    """The ``state.json`` + ``state.lock`` pair for one instance.

    The flock lives on a DEDICATED lock file (never on state.json — the
    state file is replaced atomically, and a rename would silently fork
    the lock identity). Every read-modify-write holds the lock; the
    load applies the header-vs-state arbitration.
    """

    STATE_NAME = "state.json"
    LOCK_NAME = "state.lock"

    def __init__(self, instance_dir: Path) -> None:
        self.dir = instance_dir
        self.state_path = instance_dir / self.STATE_NAME
        self.lock_path = instance_dir / self.LOCK_NAME
        self._fd: int | None = None

    def _acquire(self) -> None:
        if self._fd is None:
            _ensure_private_dir(self.dir)
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None

    def load_unlocked(self) -> MachineState:
        """Read the state WITHOUT the lock (doctor/report paths only)."""
        try:
            raw = self.state_path.read_text(encoding="utf-8")
        except OSError:
            return MachineState()
        loaded = MachineState.from_json(raw)
        return loaded if loaded is not None else MachineState()

    def load(self) -> MachineState:
        """Read the state under the lock, applying header arbitration."""
        self._acquire()
        try:
            state = self.load_unlocked()
            return arbitrate(state, self.dir)
        finally:
            self.release()

    def save(self, state: MachineState) -> None:
        """Persist the state under the lock (atomic replace + fsync)."""
        self._acquire()
        try:
            _atomic_write_private(self.state_path, state.to_json())
        finally:
            self.release()


def oldest_header_since(instance_dir: Path) -> tuple[str, float, int | None] | None:
    """The OLDEST valid spool header in the instance tree.

    Returns ``(actor, degraded_since, ttl_sec)`` or ``None`` when no
    valid header exists (an unparsable header is not a valid anchor).
    ``ttl_sec`` rides along when the header declares it (the anchor's
    TTL is adopted together with its anchor on a restore). Read-only.
    """
    oldest: tuple[str, float, int | None] | None = None
    if not instance_dir.is_dir():
        return None
    for child in sorted(instance_dir.iterdir()):
        if not child.is_dir():
            continue
        header = Spool(child, actor=child.name).header()
        if header is None:
            continue
        since = header.get("degraded_since")
        if not isinstance(since, (int, float)):
            continue
        ttl_raw = header.get("ttl_sec")
        ttl = int(ttl_raw) if isinstance(ttl_raw, (int, float)) and int(ttl_raw) > 0 else None
        if oldest is None or float(since) < oldest[1]:
            oldest = (child.name, float(since), ttl)
    return oldest


def arbitrate(state: MachineState, instance_dir: Path) -> MachineState:
    """ADR-0051 arbitration: a valid spool header OLDER than the state
    file wins.

    Covers the crash between spool-header creation and the state write:
    the state says HEALTHY (or carries a younger ``degraded_since``)
    while a header proves the degradation started earlier — the header's
    anchor (and its TTL declaration) restores the machine to DEGRADED.
    A state file that already carries an equal-or-older anchor wins (its
    TTL/REPLAY decisions stand; the header belongs to a degradation the
    state already tracks).
    """
    oldest = oldest_header_since(instance_dir)
    if oldest is None:
        return state
    _actor, header_since, header_ttl = oldest
    if state.mode == ResilienceMode.HEALTHY or state.degraded_since is None:
        logger.warning(
            "resilience: arbitration — spool header (actor=%s, since=%.0f) is "
            "older than the state file; restoring DEGRADED from the header",
            oldest[0],
            header_since,
        )
        state.mode = ResilienceMode.DEGRADED
        state.degraded_since = header_since
        if header_ttl is not None:
            state.ttl_sec = header_ttl
        state.notify_degraded_sent = True  # no duplicate webhook on recovery
    elif header_since < state.degraded_since:
        logger.warning(
            "resilience: arbitration — spool header (actor=%s, since=%.0f) is "
            "older than the state anchor (%.0f); adopting the older anchor",
            oldest[0],
            header_since,
            state.degraded_since,
        )
        state.degraded_since = header_since
    return state


# ── Replay (the reconciler's verified pass) ───────────────────────────


@dataclass
class ReplayReport:
    """Counters + problems of one replay pass.

    ADR-0051 requires the full counter set. ``superseded`` stays 0
    until the core-side conditional write lands (the front cannot
    compare store freshness); per-row client re-authz is approximated
    by the service token + the actor-policy seam — both residuals are
    honest and documented.

    F2 (W4 cascade review): a tool-level REFUSAL is no longer a
    transient ``failed`` — it lands in the ``rejected_permanent``
    pocket (counted, NEVER retried, watermark-consumed) so one poison
    row cannot hold the spool and multiply its siblings. ``failed``
    keeps only TRANSIENT trouble (transport errors, mid-call
    exceptions, secret-flagged rows) — the fail-stop class that keeps
    the spool for a manual/next-pass resolution.

    F-N1 (W4 final slice): the pocket takes ONLY SEMANTIC refusals —
    the typed renderings (namespace violation, tag contract, invalid
    status). The production leg raises :class:`ReplayTransientError`
    for the INFRA-class outcomes (non-200 answers, the generic
    ``❌ Error: …`` catch-all rendering, malformed answers), and those
    land in ``failed``/fail-stop with the row kept in the spool — the
    29.10 data-loss class stays closed in both directions (no pocketing
    of transient trouble, no re-applying siblings).
    """

    planned: int = 0
    replayed: int = 0
    superseded: int = 0
    rejected_reauthz: int = 0
    rejected_permanent: int = 0
    failed: int = 0
    skipped_tail: bool = False
    chain_problems: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    spools_removed: int = 0
    spools_purged: int = 0
    service_token_used: bool = False

    @property
    def all_applied(self) -> bool:
        """True when nothing transient failed and no chain problem
        exists — every planned row was applied, superseded, or pocketed
        as rejected-permanent (all CONSUMED; the spool removal follows).
        ``planned == 0`` passes: fully-consumed/purged spools complete
        the machine's return to HEALTHY (the F4 anti-flap leg)."""
        return self.failed == 0 and not self.chain_problems

    def summary(self) -> str:
        return (
            f"planned={self.planned} replayed={self.replayed} "
            f"superseded={self.superseded} rejected_reauthz={self.rejected_reauthz} "
            f"rejected_permanent={self.rejected_permanent} "
            f"failed={self.failed} spools_removed={self.spools_removed} "
            f"spools_purged={self.spools_purged}"
        )


#: The replay's write leg: POST one spooled op to the core's own MCP
#: plane (the write-API path — never a second engine). Returns True when
#: the core ACCEPTED the write, False on a tool-level refusal; raises
#: :class:`ReplayAuthzError` when the core refused the IDENTITY.
CoreCall = Callable[[str, dict[str, Any]], Awaitable[bool]]

#: Per-actor re-authz seam (ADR-0051 §REPLAY). Production wires None
#: (the front cannot know a client token's state — it never stored it,
#: C2); a deployment that CAN answer "is this actor still authorized"
#: (a future core status API) plugs a callable here. Returning False
#: marks the row ``rejected-reauthz``: counted, NOT applied, kept loud.
ActorPolicy = Callable[[SpoolRow], bool]

#: The payload envelope key carrying the op_id to the core (today's
#: tools ignore the unknown key — dispatch is name-based; a future
#: core-side dedup reads it. ADR-0051 keeps core-side dedup OUT of W4).
REPLAY_OP_ID_KEY = "vesma_degraded_op_id"


def replay_envelope(row: SpoolRow) -> dict[str, Any]:
    """The payload sent to the core: the ORIGINAL arguments plus the
    op_id envelope key (see :data:`REPLAY_OP_ID_KEY`)."""
    envelope = dict(row.payload)
    envelope[REPLAY_OP_ID_KEY] = row.op_id
    return envelope


#: The loud-refusal marker the core renders typed tool failures with
#: (``vesma.mcp_server.call_tool``'s dispatch wrapper: namespace
#: violation, tag contract, generic exception, in-tool input
#: refusals).
LOUD_REFUSAL_MARKER = "❌"


def core_answer_accepted(answer: Any) -> bool:
    """Is this JSON-RPC ``tools/call`` answer a REAL success?

    F1 (W4 cascade review, CRITICAL): the replay's "verified" claim
    hangs on this predicate. A refusal is ANY of:

    * a JSON-RPC ``error`` object;
    * ``result.isError=True`` (the MCP standard semantic-error flag —
      the core sets it on every ``❌``-rendered refusal since the F1a
      fix in ``mcp_server._on_call_tool``);
    * a ``❌``-prefixed first content text (defense-in-depth: the typed
      refusal rendering is recognizable even if ``isError`` ever
      regresses — a regression must degrade to loud re-delivery
      attempts, never to silent spool deletion);
    * a malformed answer with no ``result`` object (a valid MCP success
      always carries one — this plane never refuses quietly; the
      production leg classifies malformed answers as TRANSIENT —
      see :class:`ReplayTransientError` — so they retry, never pocket).

    CLASSIFICATION (F-N1, W4 final slice): this predicate answers "is
    this a success" — it does NOT decide the pocket. The production
    leg (:meth:`vesma.mcp_front.McpFront._production_core_call`) runs
    :func:`core_answer_transient` FIRST and raises
    :class:`ReplayTransientError` for the infra-class refusals, so only
    the SEMANTIC refusals (namespace violation / tag contract / invalid
    status — the typed renderings) ever reach the rejected-permanent
    pocket through ``accepted=False``.
    """
    if not isinstance(answer, dict) or "error" in answer:
        return False
    result = answer.get("result")
    if not isinstance(result, dict):
        return False
    if result.get("isError") is True:
        return False
    content = result.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if (
            isinstance(first, dict)
            and isinstance(first.get("text"), str)
            and first["text"].startswith(LOUD_REFUSAL_MARKER)
        ):
            return False
    return True


#: The core's GENERIC exception rendering (``mcp_server.call_tool``'s
#: catch-all ``except Exception``). It carries INFRA-CLASS trouble
#: (store locked, disk full, store still opening) — transient by
#: nature, never a semantic refusal to pocket.
GENERIC_ERROR_MARKER = "❌ Error:"


def core_answer_transient(answer: Any) -> bool:
    """Is this answer an INFRA-CLASS refusal (F-N1) — transient trouble
    that must keep the row in the spool?

    True for the generic ``❌ Error: …`` rendering. Run this BEFORE
    :func:`core_answer_accepted` in the production leg: a True verdict
    raises :class:`ReplayTransientError` (→ ``failed`` + fail-stop),
    and only a False verdict may travel to the pocket decision.
    """
    if not isinstance(answer, dict):
        return False
    result = answer.get("result")
    if not isinstance(result, dict):
        return False
    content = result.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if (
            isinstance(first, dict)
            and isinstance(first.get("text"), str)
            and first["text"].startswith(GENERIC_ERROR_MARKER)
        ):
            return True
    return False


async def run_replay(
    spools: list[Spool],
    *,
    core_call: CoreCall,
    actor_policy: ActorPolicy | None = None,
    service_token_available: bool = True,
    remove_on_success: Callable[[Spool], None] | None = None,
    resume_from: Mapping[str, str] | None = None,
    on_row_consumed: Callable[[Spool, SpoolRow], None] | None = None,
) -> ReplayReport:
    """Replay every spool's rows in order through ``core_call``.

    Deterministic order: the caller passes spools sorted (instance →
    actor); rows replay in append order (== op ts order). F2 (W4
    cascade review) row lifecycle — every row is either CONSUMED or the
    pass fail-stops:

    * **applied** (``core_call`` accepted) — consumed;
    * **rejected-permanent** — the core REFUSED the operation (an
      ``isError``/``❌`` answer, F1) — counted, NEVER retried, consumed;
      a namespace-violation row (a client ``agent:`` tag under the
      service token) lands here: delivery into the CLIENT's namespace
      is a future core mechanism (documented residual), the pocket is
      loud in the report, the persisted ``last_replay_summary`` and
      DR-24;
    * **rejected-reauthz** — the per-actor policy refused (consumed);
    * **failed** — TRANSIENT only (transport error, mid-call exception,
      secret-flagged row): the pass FAIL-STOPS this spool at the row
      (strict order preservation — a later op may depend on an earlier
      one), the row stays for the next pass.

    Per-row commit: after EVERY consumed row the ``on_row_consumed``
    callback fires (the coordinator persists the watermark) — a crash
    mid-replay costs at most ONE re-application, never the sibling
    duplicate-multiplier. ``resume_from`` (actor → op_id) skips the
    already-consumed prefix; a watermark whose row no longer exists
    restarts that spool from zero (the spool was replaced).

    A spool is removed ONLY when all of its rows are consumed (nothing
    pending); a fully-consumed spool from an earlier pass and a
    header-only spool are removed here too (the F4 anti-flap leg).
    Chain problems (F3: torn tail tolerated, mid-file corruption is
    hard) abort THAT spool loudly with the rows kept.
    """
    report = ReplayReport()
    if not service_token_available:
        for spool in spools:
            rows, _torn = spool.read_rows()
            report.planned += len(rows)
        report.rejected_reauthz = report.planned
        report.problems.append(
            "no front service token configured — replay refused, nothing applied"
        )
        return report

    report.service_token_used = True
    from vesma.secrets_detector import detect_secrets

    for spool in spools:
        rows, torn = spool.read_rows()
        if torn:
            report.skipped_tail = True
            report.problems.append(f"actor={spool.actor}: torn spool tail skipped (partial append)")
        rows, problems = spool.verify_chain()
        if problems:
            report.chain_problems.extend(problems)
            report.problems.append(
                f"actor={spool.actor}: chain verification FAILED — replay "
                f"aborted for this spool, rows kept ({len(problems)} problem(s))"
            )
            report.planned += len(rows)
            continue

        # Resume: skip the already-consumed prefix (the watermark row's
        # op_id pins the position; a vanished row restarts the spool).
        start = 0
        resume_op = (resume_from or {}).get(spool.actor)
        if resume_op:
            for index, row in enumerate(rows):
                if row.op_id == resume_op:
                    start = index + 1
                    break

        work = rows[start:]
        report.planned += len(work)
        spool_consumed_all = True
        for row in work:
            findings = detect_secrets(_canonical_json(row.payload))
            if findings:
                # Permanent-by-content but kept TRANSIENT-by-policy: the
                # ADR rule "a refused row is loud and the spool is NOT
                # removed" — the dirty row stays, the pass fail-stops, a
                # human resolves (never pocket-and-delete a secret-bearing
                # payload silently).
                report.failed += 1
                spool_consumed_all = False
                names = ", ".join(sorted({f.pattern_name for f in findings}))
                report.problems.append(
                    f"actor={spool.actor} op={row.op_id}: secret re-scan flagged "
                    f"({names}) — NOT applied, row kept loud; pass fail-stopped"
                )
                break
            if actor_policy is not None and not actor_policy(row):
                report.rejected_reauthz += 1
                report.problems.append(
                    f"actor={spool.actor} op={row.op_id}: actor re-authz "
                    f"refused (rejected-reauthz) — consumed, never retried"
                )
                _consume_row(spool, row, on_row_consumed)
                continue
            try:
                accepted = await core_call(row.op, replay_envelope(row))
            except ReplayAuthzError as exc:
                # The CURRENT row's refusal IS an authz refusal; the
                # already-consumed rows keep their persisted watermarks.
                report.rejected_reauthz += 1
                report.problems.append(
                    f"actor={spool.actor}: core refused the replay identity "
                    f"({exc}) — run aborted, nothing further applied; the "
                    f"per-row watermarks already persisted stay valid"
                )
                return report
            except Exception as exc:  # transport/mid-call failure: fail-stop
                report.failed += 1
                spool_consumed_all = False
                report.problems.append(
                    f"actor={spool.actor} op={row.op_id}: replay call failed "
                    f"({type(exc).__name__}) — row kept; pass fail-stopped"
                )
                break
            if accepted:
                report.replayed += 1
                _consume_row(spool, row, on_row_consumed)
            else:
                # F1 semantics: the core REFUSED the operation (semantic
                # tool error) — the rejected-permanent pocket.
                report.rejected_permanent += 1
                report.problems.append(
                    f"actor={spool.actor} op={row.op_id}: core REJECTED the "
                    f"operation (rejected-permanent) — consumed, never retried, "
                    f"visible in DR-24"
                )
                _consume_row(spool, row, on_row_consumed)
        if spool_consumed_all and rows:
            if remove_on_success is None:
                spool.mark_replayed_and_remove()
            else:
                remove_on_success(spool)
            report.spools_removed += 1
    return report


def _consume_row(
    spool: Spool,
    row: SpoolRow,
    on_row_consumed: Callable[[Spool, SpoolRow], None] | None,
) -> None:
    """Mark one row permanently consumed (the F2 per-row commit)."""
    if on_row_consumed is None:
        logger.debug("resilience: row consumed actor=%s op=%s", spool.actor, row.op_id)
        return
    on_row_consumed(spool, row)


# ── Front env.d settings (fail-closed) ────────────────────────────────


@dataclass(frozen=True)
class FrontResilienceSettings:
    """The front child's resilience knobs (the env.d slot carries them;
    the canonical YAML section mirrors the same values for the
    deployment declaration).

    FAIL-CLOSED: without ``VESMA_FRONT_DEGRADED_MAX_DURATION`` the front
    runs REFUSE-WRITES (degraded mode disabled, W2 behavior) with a
    loud boot line — it never silently invents a TTL. An UNPARSABLE
    PRESENT value raises :class:`ValueError` (the caller refuses to
    boot the child on garbage).
    """

    enabled: bool
    max_duration_sec: int = DEFAULT_MAX_DURATION_SEC
    poll_interval_sec: int = DEFAULT_POLL_INTERVAL_SEC
    probe_failure_threshold: int = DEFAULT_PROBE_FAILURE_THRESHOLD
    backoff_max_sec: int = DEFAULT_BACKOFF_MAX_SEC
    spool_max_bytes: int = DEFAULT_SPOOL_MAX_BYTES
    signage_interval_sec: int = DEFAULT_SIGNAGE_INTERVAL_SEC
    notify_url: str | None = None
    service_token: str | None = None
    instance: str = "default"

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
    ) -> tuple[FrontResilienceSettings, str | None]:
        """Build from the env slot; return ``(settings, refusal_line)``.

        ``refusal_line`` is non-None exactly when degraded mode is
        DISABLED (the loud refuse-writes line the caller must log at
        boot). A malformed PRESENT value raises :class:`ValueError`.
        """
        source = os.environ if env is None else env
        raw_max = source.get("VESMA_FRONT_DEGRADED_MAX_DURATION")
        if raw_max is None or not raw_max.strip():
            problem = (
                "refuse-writes: VESMA_FRONT_DEGRADED_MAX_DURATION is not set — "
                "degraded mode disabled (ADR-0051 fail-closed); writes to an "
                "unreachable core fail loudly instead of spooling"
            )
            return cls(enabled=False, instance=resolve_instance(source)), problem
        try:
            max_duration = parse_duration(raw_max)
            poll = parse_duration(source.get("VESMA_FRONT_DEGRADED_POLL_INTERVAL", "30s"))
            backoff = parse_duration(source.get("VESMA_FRONT_DEGRADED_BACKOFF_MAX", "600s"))
            signage = parse_duration(source.get("VESMA_FRONT_DEGRADED_SIGNAGE_INTERVAL", "15m"))
            raw_quota = source.get("VESMA_FRONT_SPOOL_MAX_BYTES", str(DEFAULT_SPOOL_MAX_BYTES))
            quota = int(raw_quota)
            if quota <= 0:
                raise ValueError(f"VESMA_FRONT_SPOOL_MAX_BYTES must be positive, got {raw_quota!r}")
            threshold_raw = source.get("VESMA_FRONT_PROBE_FAILURE_THRESHOLD")
            threshold = int(threshold_raw) if threshold_raw else DEFAULT_PROBE_FAILURE_THRESHOLD
            if threshold < 1:
                raise ValueError(
                    f"VESMA_FRONT_PROBE_FAILURE_THRESHOLD must be >= 1, got {threshold_raw!r}"
                )
            notify_url = source.get("VESMA_FRONT_DEGRADED_NOTIFY_URL") or None
            if notify_url is not None:
                # F9: the SAME http(s) guard the YAML validator applies —
                # the webhook POSTs there; an accidentally-configured
                # file:// or unknown scheme refuses (parity, not luck).
                scheme = urlsplit(notify_url).scheme.lower()
                if scheme not in ("http", "https"):
                    raise ValueError(
                        "VESMA_FRONT_DEGRADED_NOTIFY_URL must be an http(s) URL, "
                        f"got scheme {scheme!r}"
                    )
        except ValueError as exc:
            raise ValueError(f"front resilience env slot: {exc}") from exc
        token = source.get("VESMA_FRONT_SERVICE_TOKEN") or None
        settings = cls(
            enabled=True,
            max_duration_sec=max_duration,
            poll_interval_sec=poll,
            probe_failure_threshold=threshold,
            backoff_max_sec=backoff,
            spool_max_bytes=quota,
            signage_interval_sec=signage,
            notify_url=notify_url,
            service_token=token,
            instance=resolve_instance(source),
        )
        if token is None:
            logger.warning(
                "resilience: VESMA_FRONT_SERVICE_TOKEN is not set — spooling "
                "will work but REPLAY cannot authenticate and will refuse "
                "(set the front service token in the env.d slot)"
            )
        return settings, None


# ── Coordinator (front-side orchestrator) ─────────────────────────────


@dataclass
class PollOutcome:
    """What one poll cycle did (for tests and the healthz snapshot)."""

    probed_ok: bool
    mode_before: str
    mode_after: str
    replay: ReplayReport | None = None
    time_jump: str | None = None


class ResilienceCoordinator:
    """The front's resilience brain: state machine + spool + replay.

    Injected into :class:`vesma.mcp_front.McpFront` as ``resilience=``;
    ``None`` keeps the W2 byte-identical behavior. Request outcomes
    double as probe observations (traffic drives the machine even when
    the poller is not running); the poller (:meth:`run_poller`) adds
    the quiet-period cadence, the signage, the notify webhook and the
    replay passes.
    """

    def __init__(
        self,
        settings: FrontResilienceSettings,
        *,
        state_home: Path | None = None,
        actor: str = "mcp-front",
        clock: Callable[[], float] = time.time,
        probe: Callable[[], Awaitable[bool]] | None = None,
        core_call: CoreCall | None = None,
        notify: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.settings = settings
        self.actor = actor
        self._clock = clock
        self._probe_fn = probe
        self._core_call = core_call
        self._notify_fn = notify
        self.spool_root = (state_home or resolve_state_home()) / "vesma" / "spool"
        self.instance_dir = self.spool_root / settings.instance
        self._store = StateStore(self.instance_dir)

    # ── state access ──────────────────────────────────────────────

    def current_mode(self) -> str:
        return self.state().mode

    def state(self) -> MachineState:
        return self._store.load()

    def ttl_left_sec(self, state: MachineState | None = None) -> int | None:
        state = state or self.state()
        if state.degraded_since is None:
            return None
        return int(state.degraded_since + state.ttl_sec - self._clock())

    # ── spool handles ─────────────────────────────────────────────

    def open_spool(self, actor: str, state: MachineState) -> Spool:
        """Open (create-if-absent) the actor's spool anchored at the
        state's degradation (the header is the truth anchor — see
        :func:`arbitrate`)."""
        since = state.degraded_since if state.degraded_since is not None else self._clock()
        return Spool.open(
            self.spool_root,
            instance=self.settings.instance,
            actor=actor,
            degraded_since=since,
            max_duration_sec=state.ttl_sec,
            clock=self._clock,
        )

    def discover_spools(self) -> list[Spool]:
        return Spool.discover(self.instance_dir)

    def spool_bytes(self) -> int:
        return sum(spool.bytes_used() for spool in self.discover_spools())

    def pending_ops(self) -> int:
        total = 0
        for spool in self.discover_spools():
            rows, _torn = spool.read_rows()
            total += len(rows)
        return total

    # ── machine transitions ───────────────────────────────────────

    def observe_request(self, ok: bool) -> None:
        """Feed a request outcome into the probe counters (state-file
        I/O only on an actual change). A success while DEGRADED/BLOCKED
        proves the core is listening again — the machine moves to REPLAY
        (the poller's next pass runs the delivery; a live request proves
        liveness just like a probe does)."""
        state = self._store.load()
        if ok:
            if state.mode in (ResilienceMode.DEGRADED, ResilienceMode.BLOCKED_WRITES):
                state.mode = ResilienceMode.REPLAY
                state.probe_failures = 0
                state.poll_backoff_sec = self.settings.poll_interval_sec
                logger.warning(
                    "resilience: live request succeeded — REPLAY pending (instance=%s)",
                    self.settings.instance,
                )
                self._save(state)
                return
            if state.probe_failures:
                state.probe_failures = 0
                state.poll_backoff_sec = self.settings.poll_interval_sec
                self._save(state)
            return
        state.probe_failures += 1
        state.poll_backoff_sec = min(
            self.settings.backoff_max_sec,
            max(self.settings.poll_interval_sec, state.poll_backoff_sec * 2),
        )
        if (
            state.mode == ResilienceMode.HEALTHY
            and state.probe_failures >= self.settings.probe_failure_threshold
        ):
            self._enter_degraded(state)
        elif (
            # F10: the SAME threshold lowers REPLAY back to DEGRADED —
            # the "live core" the reads proved has flapped away; the
            # per-row watermarks survive (consumed rows stay consumed).
            state.mode == ResilienceMode.REPLAY
            and state.probe_failures >= self.settings.probe_failure_threshold
        ):
            state.mode = ResilienceMode.DEGRADED
            logger.warning(
                "resilience: replay aborted — core flapped away (%d failed "
                "requests); back to DEGRADED, consumed rows stay consumed",
                state.probe_failures,
            )
        self._save(state)

    def _enter_degraded(self, state: MachineState) -> None:
        now = self._clock()
        state.mode = ResilienceMode.DEGRADED
        if state.degraded_since is None:
            state.degraded_since = now
        state.ttl_sec = self.settings.max_duration_sec
        state.notify_degraded_sent = False
        state.notify_ttl80_sent = False
        logger.warning(
            "resilience: entering DEGRADED (instance=%s, since=%s, ttl=%ds) — "
            "writes will be spooled",
            self.settings.instance,
            _utc_iso(state.degraded_since),
            state.ttl_sec,
        )

    def _save(self, state: MachineState) -> None:
        state.last_seen_wall = self._clock()
        self._store.save(state)

    def check_write_allowed(self) -> None:
        """The typed gate every degraded write passes: TTL then quota.

        Raises :class:`DegradedTTLExceeded` (TTL expired) or
        :class:`SpoolQuotaExceeded` (byte quota) — the REFUSAL TYPE names
        WHY the machine is blocked (a quota-blocked state must not masquerade
        as an expired TTL); both persist BLOCKED_WRITES loudly before
        refusing.
        """
        state = self.state()
        if self._evaluate_ttl(state):
            self._save(state)
        if state.mode == ResilienceMode.BLOCKED_WRITES:
            if state.quota_blocked:
                raise SpoolQuotaExceeded(self.settings.spool_max_bytes)
            expired_by = 0
            if state.degraded_since is not None:
                expired_by = max(0, int(self._clock() - (state.degraded_since + state.ttl_sec)))
            raise DegradedTTLExceeded(expired_by)
        if state.quota_blocked or self.spool_bytes() >= self.settings.spool_max_bytes:
            if not state.quota_blocked:
                state.quota_blocked = True
                state.mode = ResilienceMode.BLOCKED_WRITES
                self._save(state)
                logger.error(
                    "resilience: spool quota exhausted (%d bytes) — BLOCKED_WRITES",
                    self.settings.spool_max_bytes,
                )
            raise SpoolQuotaExceeded(self.settings.spool_max_bytes)

    def _evaluate_ttl(self, state: MachineState) -> bool:
        """Apply the wall-clock TTL to a DEGRADED state (in place).

        Returns True when the state changed. The TTL ALWAYS evaluates on
        the wall clock from the persisted anchor — the time-jump
        detector only adds signage + the logged decision.
        """
        if state.mode != ResilienceMode.DEGRADED or state.degraded_since is None:
            return False
        if self._clock() < state.degraded_since + state.ttl_sec:
            return False
        state.mode = ResilienceMode.BLOCKED_WRITES
        state.poll_backoff_sec = self.settings.poll_interval_sec
        logger.error(
            "resilience: degraded TTL expired (since=%s, ttl=%ds) — "
            "BLOCKED_WRITES; frozen operations wait for a managed replay",
            _utc_iso(state.degraded_since),
            state.ttl_sec,
        )
        return True

    def _detect_time_jump(self, state: MachineState) -> str | None:
        """The wall-clock jump detector (signage only; the TTL decision
        rides the log line)."""
        if state.last_seen_wall <= 0:
            return None
        shift = self._clock() - state.last_seen_wall
        if abs(shift) <= TIME_JUMP_THRESHOLD_SEC:
            return None
        decision = (
            "TTL re-evaluated on the wall clock from the persisted anchor"
            if shift > 0
            else "backwards jump — the persisted anchor stands (no TTL credit)"
        )
        text = f"resilience: wall-clock jump detected ({shift:+.0f}s since last seen) — {decision}"
        logger.warning(text)
        return text

    # ── signage + notify ──────────────────────────────────────────

    def _signage(self, state: MachineState, *, force: bool = False) -> None:
        if state.mode == ResilienceMode.HEALTHY or state.degraded_since is None:
            return
        now = self._clock()
        if not force and now - state.last_signage_wall < self.settings.signage_interval_sec:
            return
        age = int(now - state.degraded_since)
        left = self.ttl_left_sec(state)
        logger.warning(
            "resilience: %s for %ds (instance=%s, ttl_left=%s, spool_ops=%d)",
            state.mode,
            age,
            self.settings.instance,
            f"{left}s" if left is not None else "n/a",
            self.pending_ops(),
        )
        state.last_signage_wall = now
        self._save(state)

    def _notify_due(self, state: MachineState) -> str | None:
        """Which notify mark is due (``degraded`` entry / ``ttl80``), or
        ``None``. Marks are once-per-degradation; the caller fires the
        webhook and persists the mark."""
        if state.mode not in (ResilienceMode.DEGRADED, ResilienceMode.BLOCKED_WRITES):
            return None
        if not state.notify_degraded_sent:
            return "degraded"
        if (
            not state.notify_ttl80_sent
            and state.mode == ResilienceMode.DEGRADED
            and state.degraded_since is not None
        ):
            consumed = (self._clock() - state.degraded_since) / max(1, state.ttl_sec)
            if consumed >= TTL_FAIL_FRACTION:
                return "ttl80"
        return None

    async def _fire_notify(self, state: MachineState, which: str) -> None:
        """The out-of-machine escalation channel (best-effort).

        The in-machine channels (logs, header, doctor) are useless if
        nobody watches them — the notify webhook fires on DEGRADED entry
        and on the 80% TTL crossing, once per degradation. A failure
        degrades to a log line, never to a crash.
        """
        if not self.settings.notify_url or self._notify_fn is None:
            state_flag = "notify_degraded_sent" if which == "degraded" else "notify_ttl80_sent"
            setattr(state, state_flag, True)
            self._save(state)
            return
        try:
            await self._notify_fn(self.settings.notify_url)
            logger.warning("resilience: notify webhook delivered (%s)", which)
        except Exception as exc:
            logger.warning("resilience: notify webhook failed (%s: %s)", type(exc).__name__, exc)
        state_flag = "notify_degraded_sent" if which == "degraded" else "notify_ttl80_sent"
        setattr(state, state_flag, True)
        self._save(state)

    # ── the probe/poll cycle ──────────────────────────────────────

    async def _probe(self) -> bool:
        if self._probe_fn is not None:
            return await self._probe_fn()
        return True  # no probe wired: reachable (tests inject one)

    async def poll_once(self) -> PollOutcome:
        """One machine tick: jump detection → TTL → probe → transitions
        → replay (when REPLAY) → signage/notify."""
        state = self._store.load()
        before = state.mode
        jump = self._detect_time_jump(state)
        if self._evaluate_ttl(state):
            pass  # mutated in place; saved below
        probed_ok = await self._probe()
        replay_report: ReplayReport | None = None

        if probed_ok:
            state.probe_failures = 0
            state.poll_backoff_sec = self.settings.poll_interval_sec
            if state.mode in (ResilienceMode.DEGRADED, ResilienceMode.BLOCKED_WRITES):
                state.mode = ResilienceMode.REPLAY
                logger.warning(
                    "resilience: core recovered — entering REPLAY (instance=%s)",
                    self.settings.instance,
                )
        else:
            state.probe_failures += 1
            state.poll_backoff_sec = min(
                self.settings.backoff_max_sec,
                max(self.settings.poll_interval_sec, state.poll_backoff_sec * 2),
            )
            if (
                state.mode == ResilienceMode.HEALTHY
                and state.probe_failures >= self.settings.probe_failure_threshold
            ):
                self._enter_degraded(state)

        if state.mode == ResilienceMode.REPLAY:
            replay_report = await self._run_replay_pass(state)
            if replay_report is not None and replay_report.all_applied:
                state.mode = ResilienceMode.HEALTHY
                state.degraded_since = None
                state.quota_blocked = False
                state.replay_problems = []
                state.replay_watermarks = {}  # fresh degradation starts clean
                state.last_replay_summary = replay_report.summary()
                logger.warning(
                    "resilience: replay complete (%s) — back to HEALTHY",
                    replay_report.summary(),
                )
            elif replay_report is not None:
                state.mode = ResilienceMode.DEGRADED
                state.replay_problems = list(replay_report.problems)[:20]
                state.last_replay_summary = replay_report.summary()
                logger.error(
                    "resilience: replay incomplete (%s) — back to DEGRADED, unconsumed rows kept",
                    replay_report.summary(),
                )
            else:
                # Another reconciler held the lock — retry on the next poll.
                state.mode = ResilienceMode.DEGRADED

        self._save(state)
        which = self._notify_due(state)
        if which is not None:
            await self._fire_notify(state, which)
        self._signage(state)
        return PollOutcome(
            probed_ok=probed_ok,
            mode_before=before,
            mode_after=state.mode,
            replay=replay_report,
            time_jump=jump,
        )

    async def _run_replay_pass(self, state: MachineState) -> ReplayReport | None:
        """One serialized replay pass (the host-level reconciler lock).

        The F2 per-row commit wires ``on_row_consumed`` to persist the
        watermark into ``state`` IMMEDIATELY after every consumed row —
        a crash mid-pass costs at most ONE re-application.
        """
        _ensure_private_dir(self.spool_root)
        lock_path = self.spool_root / "reconcile.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                logger.info(
                    "resilience: another reconciler holds the replay lock — "
                    "skipping this pass (one reconciler per host)"
                )
                return None

            def on_row_consumed(spool: Spool, row: SpoolRow) -> None:
                state.replay_watermarks[spool.actor] = row.op_id
                self._store.save(state)

            spools, purged = self._purge_consumed_spools()
            if not spools:
                report = ReplayReport()
                report.spools_purged = purged
                # An empty/fully-purged spool tree with a REPLAY state
                # (the crash-between-rename-and-cleanup corner, the F4
                # flap source): nothing pending — trivially complete.
                return report
            report = await run_replay(
                spools,
                core_call=self._core_call or _refusing_core_call,
                service_token_available=self.settings.service_token is not None,
                resume_from=dict(state.replay_watermarks),
                on_row_consumed=on_row_consumed,
            )
            report.spools_purged = purged
            return report
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _purge_consumed_spools(self) -> tuple[list[Spool], int]:
        """F4 (W4 cascade review), refined by F-N2 (W4 final slice): while
        the machine is in REPLAY, remove only artifacts with NOTHING
        pending — a header-only spool (the stale anchor that flapped the
        machine back to DEGRADED against a LIVE core) and a leftover with
        NO live rows.

        F-N2 corner this closes: a rename-crash leftover (header +
        ``.replayed``) can FLAP the machine to DEGRADED, and a fresh
        receipted write then lands in a NEW ``ops.jsonl`` of the SAME
        directory — a blind ``done_file.exists()`` purge would destroy a
        receipted row. The rule now: live ``ops.jsonl`` rows ⇒ the
        directory SURVIVES (run_replay consumes them; the stale
        ``.replayed`` file — a copy of ALREADY-APPLIED rows — is set
        aside), and a headerless directory WITH rows is NEVER
        auto-deleted (the F3 manual-resolution semantics, loud).
        Purge happens only when ``ops.jsonl`` is absent or holds zero
        rows — literally nothing pending, zero data to lose.
        """
        import shutil

        purged = 0
        if self.instance_dir.is_dir():
            for child in sorted(self.instance_dir.iterdir()):
                if not child.is_dir():
                    continue
                done_file = child / (Spool.OPS_NAME + Spool.DONE_SUFFIX)
                live_ops = child / Spool.OPS_NAME
                has_header = (child / Spool.HEADER_NAME).exists()
                leftover = done_file.exists() or not has_header
                if not leftover:
                    continue
                if live_ops.exists() and live_ops.stat().st_size > 0:
                    # Live rows in the same directory — the receipted
                    # writes run_replay must consume. Set the stale
                    # applied-rows copy aside; keep the directory.
                    if done_file.exists():
                        logger.warning(
                            "resilience: actor=%s has live rows beside a stale "
                            ".replayed leftover — leftover set aside, rows kept "
                            "for the replay pass",
                            child.name,
                        )
                        with contextlib.suppress(OSError):
                            done_file.unlink()
                    else:
                        logger.warning(
                            "resilience: actor=%s is headerless but holds rows — "
                            "NEVER auto-deleted (manual resolution, ADR-0051 "
                            "tamper semantics)",
                            child.name,
                        )
                    continue
                logger.warning(
                    "resilience: purging leftover spool dir actor=%s "
                    "(.replayed leftover / headerless — nothing pending)",
                    child.name,
                )
                shutil.rmtree(child, ignore_errors=True)
                purged += 1
        survivors: list[Spool] = []
        for spool in self.discover_spools():
            rows, _torn = spool.read_rows()
            if not rows:
                logger.warning(
                    "resilience: purging empty spool actor=%s (header-only — nothing pending)",
                    spool.actor,
                )
                spool.remove_tree()
                purged += 1
                continue
            survivors.append(spool)
        return survivors, purged

    async def run_poller(self, stop: Any = None) -> None:
        """The quiet-period loop: poll every ``poll_backoff_sec`` while
        degraded; sleep coarse while healthy (request outcomes keep the
        machine fed either way). ``stop`` is an ``asyncio.Event``-like
        object (``is_set()``)."""
        while True:
            if stop is not None and stop.is_set():
                return
            state = self._store.load()
            if state.mode == ResilienceMode.HEALTHY:
                await asyncio.sleep(self.settings.poll_interval_sec)
                continue
            try:
                await self.poll_once()
            except Exception:
                logger.exception("resilience: poll cycle failed")
            await asyncio.sleep(max(1, state.poll_backoff_sec))

    # ── the write interception (front 503-branch) ─────────────────

    def intercept_write(self, body: bytes) -> tuple[dict[str, Any], str] | None:
        """Spool a WRITING ``tools/call`` body; return ``(result, receipt)``.

        ``None`` = not a spoolable write (a read or a non-JSON-RPC body)
        — the caller falls back to the honest 503. The typed gates
        (TTL, quota) are enforced FIRST, then classification, then the
        append.
        """
        self.check_write_allowed()
        parsed = tool_call_from_body(body)
        if parsed is None:
            return None
        name, arguments, jsonrpc_id = parsed
        if classify_tool_call(name, arguments) != "write":
            return None
        state = self.state()
        spool = self.open_spool(self.actor, state)
        row = spool.append(op=name, payload=arguments, clock=self._clock)
        if self.spool_bytes() >= self.settings.spool_max_bytes:
            state = self.state()
            state.quota_blocked = True
            state.mode = ResilienceMode.BLOCKED_WRITES
            self._save(state)
            logger.error("resilience: spool quota exhausted after append — BLOCKED_WRITES")
        left = self.ttl_left_sec()
        # F2: NO delivery promise — identity-tagged rows may land in the
        # rejected-permanent pocket under the service token; the receipt
        # points at DR-24 for the delivery conditions instead.
        receipt = (
            f"[vesma degraded mode] The core is unreachable; your write was "
            f"recorded to the local resilience spool. Delivery runs "
            f"automatically when the core recovers; any held-back or "
            f"rejected operations are reported by DR-24 (`vesma doctor`) "
            f"and the front log — check them after recovery. "
            f"op_id={row.op_id}; ttl_left={left if left is not None else 'n/a'}s; "
            f"tool={name}. Do NOT retry this write manually."
        )
        logger.warning(
            "resilience: spooled write op=%s tool=%s actor=%s (flagged=%s)",
            row.op_id,
            name,
            self.actor,
            ",".join(row.secret_flagged) or "none",
        )
        return jsonrpc_result(jsonrpc_id, receipt), receipt

    # ── healthz snapshot ──────────────────────────────────────────

    def healthz_snapshot(self) -> dict[str, Any]:
        """The ``/healthz`` degraded section (fields + counters)."""
        state = self.state()
        left = self.ttl_left_sec(state)
        return {
            "degraded": {
                "mode": state.mode,
                "since_utc": _utc_iso(state.degraded_since) if state.degraded_since else None,
                "ttl_left_sec": left,
                "spool_bytes": self.spool_bytes(),
                "spool_ops_pending": self.pending_ops(),
                "probe_failures": state.probe_failures,
            }
        }


async def _refusing_core_call(op: str, payload: dict[str, Any]) -> bool:
    """The default replay leg when the owner injected none — a
    coordinator without ``core_call`` must never pretend to replay."""
    raise ReplayAuthzError("no replay core_call wired — refusing to replay")


__all__ = [
    "DEFAULT_BACKOFF_MAX_SEC",
    "DEFAULT_MAX_DURATION_SEC",
    "DEFAULT_POLL_INTERVAL_SEC",
    "DEFAULT_PROBE_FAILURE_THRESHOLD",
    "DEFAULT_SIGNAGE_INTERVAL_SEC",
    "DEFAULT_SPOOL_MAX_BYTES",
    "MODES",
    "REPLAY_OP_ID_KEY",
    "TIME_JUMP_THRESHOLD_SEC",
    "TTL_FAIL_FRACTION",
    "WRITE_TOOLS",
    "DegradedTTLExceeded",
    "FrontResilienceSettings",
    "MachineState",
    "PollOutcome",
    "ReplayAuthzError",
    "ReplayReport",
    "ReplayTransientError",
    "ResilienceCoordinator",
    "ResilienceMode",
    "Spool",
    "SpoolQuotaExceeded",
    "SpoolRow",
    "StateStore",
    "arbitrate",
    "classify_tool_call",
    "core_answer_accepted",
    "core_answer_transient",
    "oldest_header_since",
    "parse_duration",
    "replay_envelope",
    "resolve_instance",
    "resolve_state_home",
    "run_replay",
    "sanitize_label",
    "tool_call_from_body",
]
