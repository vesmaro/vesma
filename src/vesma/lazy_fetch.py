"""S2 lazy fetch — the explicit, operator-confirmed content fetch.

Chairman ruling (ADR-0021 Q10.3): the sync is RARE and EXPLICIT. The
``federation_index`` mirror (populated by the meta-poller,
:mod:`vesma.meta_poller`) carries metadata-only rows; this module
turns an operator's ``vesma fetch --id <fed-id>...`` into

1. **Resolution** — each id is looked up in ``federation_index``; the
   row's ``origin_peer`` (re-stamped by the import gate to the
   authenticated sender id — a ``"self"``/empty value means OUR corpus)
   names the peer that holds the content body:

   * ``origin_peer`` configured in ``federation.peers`` → FETCH from it;
   * ``origin_peer`` unknown → an honest error listing the configured
     peers (the plan refuses to guess);
   * ``"self"``/empty → the row claims local origin: check ``memories``
     by ``fed_id`` — present = "already local" (skip), absent = an
     index-consistency error (a self row with no local body means the
     mirror and the corpus disagree — never paper over it);
   * ``content_state=tombstoned`` → skip (the origin deleted the body;
     there is nothing to fetch).
2. **Plan + confirmation** — a summary (id / title / origin peer /
   project) is printed and confirmed interactively: ``y/N`` on a TTY.
   A non-TTY stdin without ``--yes`` REFUSES to run (same gate as
   ``mnemos-mesh pull``: confirmation is a human at a terminal or an
   explicit flag, never ``echo y | vesma fetch``).
3. **Execution** — one mesh CLI invocation per origin peer
   (``<mesh_bin> fetch --config <mesh.yaml> --peer <origin> --id ...
   --json``; the Go-track subcommand, coded against its JSON contract
   ``{"records": [CompactRecord...], "not_found": [...]}`` — never
   imported).
4. **Import** — IN-PROCESS through the very same path :rpc:`WriteMemory`
   uses (:meth:`VesmaCoreServicer.import_compact_record`): the ACL
   gate, the #359/#362 duplicate gate, moderation and the Layer 1
   secrets scanner all apply unchanged. There is deliberately NO
   dedup logic here — the shared import path owns it.

Counters (the command summary line): ``fetched`` (records received from
the peer), ``imported`` (WRITTEN), ``duplicates`` (ALREADY_EXHAUSTED),
``gated`` (ACL/moderation refusal — policy, not a transport failure),
``not_found`` (ids the peer could not serve), ``skipped``
(already-local / tombstoned ids resolved at plan time), ``errors``
(transport/import failures — CLI exit, broken JSON, timeout, contract
violation). Exit code is non-zero iff ``errors > 0``.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, TextIO

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vesma.compact import CompactRecord, FederationIndexEntry
from vesma.config import FederationConfig, Settings
from vesma.manager import MemoryManager
from vesma.mesh_server import CompactImportStatus, VesmaCoreServicer
from vesma.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

#: Wall-clock budget for one mesh CLI invocation (all ids of one peer
#: travel together). A hung CLI must fail the run, never wedge it.
FETCH_TIMEOUT_S: Final[float] = 60.0

#: stderr tail kept in the error surface — diagnostics, not a log sink.
_STDERR_TAIL_CHARS: Final[int] = 300

#: stdout tail kept when JSON-envelope extraction fails — shows the
#: operator WHAT was on the wire.
_STDOUT_TAIL_CHARS: Final[int] = 300

#: Skip reasons attached to plan items that will NOT be fetched.
SKIP_ALREADY_LOCAL: Final[str] = "already_local"
SKIP_TOMBSTONED: Final[str] = "tombstoned"


class FetchResolutionError(Exception):
    """The fetch plan cannot be built — a fatal, pre-flight condition.

    Raised for: ids absent from ``federation_index``, unknown origin
    peers, and ``self``-origin rows whose body is not in the local
    corpus. The command aborts BEFORE any network leg runs (fail-fast:
    a half-resolved plan must never fetch a partial id set).
    """


class FetchPeerError(Exception):
    """One peer's fetch leg failed (CLI exit, JSON, timeout, contract).

    Recorded per peer as ``errors`` (exit non-zero) — the run continues
    with the remaining peers so one dead peer does not hide the others'
    results.
    """

    def __init__(self, peer_id: str, reason: str) -> None:
        self.peer_id = peer_id
        self.reason = reason
        super().__init__(f"peer={peer_id}: {reason}")


class FetchEnvelope(BaseModel):
    """Wire contract of ``mnemos-mesh fetch ... --json`` stdout.

    ``{"records": [CompactRecord fields...], "not_found": [fed-id...]}``
    — the mesh-track JSON envelope. Extra fields are ignored
    (forward-compat with additive Go-side fields); ``records`` are kept
    RAW: per-record validation happens in :func:`_parse_records` so the
    batch outcome is decided once, per record.
    """

    model_config = ConfigDict(extra="ignore")

    records: list[dict[str, Any]] = Field(default_factory=list)
    not_found: list[str] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class FetchPlanItem:
    """One requested id after resolution."""

    fed_id: str
    title: str
    origin_peer: str
    project: str
    #: ``None`` = fetch from ``origin_peer``; otherwise a skip reason
    #: (:data:`SKIP_ALREADY_LOCAL` / :data:`SKIP_TOMBSTONED`).
    skip_reason: str | None = None


@dataclass(frozen=True, slots=True)
class FetchPlan:
    """The resolved shape of a fetch run (pre-confirmation)."""

    items: list[FetchPlanItem] = field(default_factory=list)

    @property
    def fetch_items(self) -> list[FetchPlanItem]:
        """Items that will be fetched (skip-free), in request order."""
        return [i for i in self.items if i.skip_reason is None]

    @property
    def skipped_items(self) -> list[FetchPlanItem]:
        """Items resolved to a skip (already local / tombstoned)."""
        return [i for i in self.items if i.skip_reason is not None]

    def peer_groups(self) -> dict[str, list[str]]:
        """Origin peer → the fed-ids to request from it (stable order)."""
        groups: dict[str, list[str]] = {}
        for item in self.fetch_items:
            groups.setdefault(item.origin_peer, []).append(item.fed_id)
        return groups


@dataclass(slots=True)
class FetchStats:
    """Counters for one ``vesma fetch`` run (the summary line).

    ``aborted`` marks a DECLINED confirmation (all-zero counters, the
    CLI turns it into a non-zero exit) — distinct from a clean
    all-skipped plan (``skipped > 0``, everything else zero, exit 0).
    """

    fetched: int = 0
    imported: int = 0
    duplicates: int = 0
    gated: int = 0
    not_found: int = 0
    skipped: int = 0
    errors: int = 0
    aborted: bool = False


# ── Resolution ───────────────────────────────────────────────────────────────


def resolve_fetch_plan(
    store: SQLiteStore, federation: FederationConfig, fed_ids: Sequence[str]
) -> FetchPlan:
    """Resolve requested ids into a :class:`FetchPlan` (fail-fast).

    Raises :class:`FetchResolutionError` when ANY id cannot be resolved
    — blank id, unknown id, unknown origin peer, or a ``self`` row with
    no local body. Nothing is fetched on a raise (the caller aborts the
    command before the network leg).
    """
    ids = list(dict.fromkeys(fed_ids))  # de-dup, order preserved
    if any(not i.strip() for i in ids):
        raise FetchResolutionError("blank --id value is not a federation record id")
    entries = store.get_index_entries(ids)
    missing = [i for i in ids if i not in entries]
    if missing:
        raise FetchResolutionError(
            f"id(s) not present in the local federation_index mirror: {missing} — "
            "the mirror only knows what the meta-poller imported "
            "(run `vesma meta-poll` first)"
        )
    items: list[FetchPlanItem] = []
    for fed_id in ids:
        entry = entries[fed_id]
        origin = entry.origin_peer.strip()
        if origin in ("", "self"):
            # The row claims OUR corpus as origin: the body must be
            # here. Present → already local (skip); absent → the mirror
            # and the corpus disagree — an honest error, never a fetch.
            if store.find_federated_duplicate(fed_id=fed_id) is not None:
                items.append(_skip_item(entry, SKIP_ALREADY_LOCAL))
            else:
                raise FetchResolutionError(
                    f"id {fed_id!r} has origin_peer={entry.origin_peer!r} but no "
                    "local memory carries it — federation_index is inconsistent "
                    "with the corpus (re-poll the peer or rebuild the index)"
                )
            continue
        if origin not in federation.peers:
            raise FetchResolutionError(
                f"id {fed_id!r} has origin_peer={origin!r} which is not a configured "
                f"federation peer (configured: {sorted(federation.peers)})"
            )
        if entry.content_state == "tombstoned":
            items.append(_skip_item(entry, SKIP_TOMBSTONED))
            continue
        items.append(
            FetchPlanItem(
                fed_id=fed_id,
                title=entry.title,
                origin_peer=origin,
                project=entry.project,
            )
        )
    return FetchPlan(items=items)


def _skip_item(entry: FederationIndexEntry, reason: str) -> FetchPlanItem:
    """Build a skip plan item from an index entry."""
    return FetchPlanItem(
        fed_id=entry.id,
        title=entry.title,
        origin_peer=entry.origin_peer,
        project=entry.project,
        skip_reason=reason,
    )


# ── Plan rendering + confirmation ────────────────────────────────────────────


def render_plan(plan: FetchPlan) -> str:
    """Render the operator-facing plan (shown BEFORE the confirmation)."""
    lines = ["Fetch plan:"]
    for item in plan.items:
        if item.skip_reason is None:
            lines.append(
                f"  fetch   {item.fed_id}  {item.title!r}  "
                f"origin={item.origin_peer} project={item.project or '-'}"
            )
        elif item.skip_reason == SKIP_ALREADY_LOCAL:
            lines.append(f"  skip    {item.fed_id}  {item.title!r}  already local")
        else:
            lines.append(f"  skip    {item.fed_id}  {item.title!r}  tombstoned at origin")
    for peer, ids in plan.peer_groups().items():
        lines.append(f"  peer {peer}: {len(ids)} record(s) via the mesh CLI")
    lines.append("  target: local corpus (WriteMemory import path, MERGE)")
    return "\n".join(lines)


def confirm_fetch(stdin: TextIO, stdout: TextIO, is_tty: Callable[[], bool]) -> bool:
    """Ask ``y/N`` on stdin — the explicit-operation gate.

    Mirrors ``mnemos-mesh pull`` (``cmd/mnemos-mesh/pull.go``
    ``confirmPull``):

    * stdin not a TTY → refusal with the ``--yes`` hint (cron/pipe
      cannot confirm);
    * anything but ``y``/``Y``/``yes`` (case-insensitive) → refusal,
      including EOF and an empty line.
    """
    if not is_tty():
        stdout.write(
            "fetch: confirmation required but stdin is not interactive — pass --yes to proceed\n"
        )
        return False
    stdout.write("Proceed with fetch? [y/N]: ")
    stdout.flush()
    line = stdin.readline()
    return line.strip().lower() in ("y", "yes")


# ── Mesh CLI leg ─────────────────────────────────────────────────────────────


def _extract_json_object(stdout: bytes) -> Any:
    """Pull the JSON envelope out of possibly noisy stdout.

    Same strategy as the meta-poller's ``sync-meta`` parser: (a) the
    whole stdout parses as JSON — the clean-wire case; (b) otherwise
    the envelope opens at the first ``{``-prefixed line and runs to EOF
    (one object, printed last); (c) neither parses → ``None`` (the
    caller raises with the stdout tail).
    """
    text = stdout.decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("{")), None)
    if start is not None:
        try:
            return json.loads("\n".join(lines[start:]))
        except json.JSONDecodeError:
            pass
    return None


def _parse_records(peer_id: str, raw_records: list[dict[str, Any]]) -> list[CompactRecord]:
    """Validate wire records into :class:`CompactRecord` bodies.

    A record that fails validation raises :class:`FetchPeerError` —
    unlike the poller's per-record tolerance, a lazy fetch is an
    explicit operator-confirmed batch for SPECIFIC ids: silently
    dropping part of what was confirmed would defeat the confirmation.
    """
    records: list[CompactRecord] = []
    for raw in raw_records:
        try:
            records.append(CompactRecord.model_validate(raw))
        except ValidationError as exc:
            raise FetchPeerError(
                peer_id,
                "fetch record violates the CompactRecord contract: "
                f"{exc.errors()[0].get('msg', 'validation error')}",
            ) from exc
    return records


def fetch_from_peer(
    mesh_bin: str, mesh_config_path: str, peer_id: str, fed_ids: Sequence[str]
) -> FetchEnvelope:
    """Run one ``mesh fetch`` invocation and parse its JSON envelope.

    Raises :class:`FetchPeerError` on: executable not found, non-zero
    exit, timeout, non-JSON stdout, or an invalid envelope. On timeout
    ``subprocess.run`` kills the child (no orphaned CLI process).
    """
    argv = [mesh_bin, "fetch", "--config", mesh_config_path, "--peer", peer_id]
    for fed_id in fed_ids:
        argv += ["--id", fed_id]
    argv += ["--json"]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=FETCH_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError as exc:
        raise FetchPeerError(peer_id, f"cannot execute mesh bin {mesh_bin!r}: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise FetchPeerError(peer_id, f"fetch timed out after {FETCH_TIMEOUT_S:.0f}s") from exc
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", errors="replace")[-_STDERR_TAIL_CHARS:].strip()
        raise FetchPeerError(peer_id, f"fetch exited {proc.returncode}: {tail or 'no stderr'}")
    payload = _extract_json_object(proc.stdout)
    if payload is None:
        tail = proc.stdout.decode("utf-8", errors="replace")[-_STDOUT_TAIL_CHARS:].strip()
        raise FetchPeerError(
            peer_id,
            f"fetch stdout is not valid JSON (no envelope found): {tail or 'empty stdout'!r}",
        )
    try:
        return FetchEnvelope.model_validate(payload)
    except ValidationError as exc:
        raise FetchPeerError(peer_id, f"fetch envelope violates contract: {exc}") from exc


# ── Orchestration ────────────────────────────────────────────────────────────


def run_fetch(
    store: SQLiteStore,
    manager: MemoryManager,
    settings: Settings,
    fed_ids: Sequence[str],
    *,
    assume_yes: bool,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    is_tty: Callable[[], bool] | None = None,
) -> FetchStats:
    """Resolve → confirm → fetch → import; returns the run counters.

    The module entry the CLI wraps. :class:`FetchResolutionError`
    propagates (the command aborts, nothing fetched). A declined or
    impossible confirmation returns all-zero counters — the CLI turns
    that into the "aborted" exit. ``assume_yes`` skips the interactive
    gate (the non-TTY/script escape hatch, same as mesh pull's
    ``--yes``).
    """
    plan = resolve_fetch_plan(store, settings.federation, fed_ids)
    stats = FetchStats(skipped=len(plan.skipped_items))
    if not plan.fetch_items:
        return stats
    # The plan precedes the confirmation (the operator confirms WHAT
    # they see: ids, titles, origins, peers).
    stdout.write(render_plan(plan) + "\n")
    if not assume_yes:
        tty_probe = is_tty if is_tty is not None else stdin.isatty
        if not confirm_fetch(stdin, stdout, tty_probe):
            stats.aborted = True
            return stats
    servicer = VesmaCoreServicer(manager, settings=settings)
    for peer_id, ids in plan.peer_groups().items():
        try:
            envelope = fetch_from_peer(
                settings.federation.fetch.mesh_bin,
                settings.federation.fetch.mesh_config_path,
                peer_id,
                ids,
            )
            records = _parse_records(peer_id, envelope.records)
        except FetchPeerError as exc:
            logger.error("lazy fetch peer=%s error=%s", peer_id, exc.reason)
            stats.errors += len(ids)
            continue
        stats.fetched += len(records)
        # Ids the peer did not return: its declared not_found set plus
        # anything it silently dropped (records+not_found should cover
        # the request; a gap counts as not found, never as success).
        accounted = {r.id for r in records} | set(envelope.not_found)
        stats.not_found += len(envelope.not_found) + len([i for i in ids if i not in accounted])
        for record in records:
            try:
                result = servicer.import_compact_record(record, peer_id=peer_id)
            except Exception:
                # Containment boundary for the batch: a store/pipeline
                # failure on one record must not abort the remaining
                # records — log it fully and count it as an error (the
                # exit code reflects it).
                logger.exception("lazy fetch import failed fed_id=%s peer=%s", record.id, peer_id)
                stats.errors += 1
                continue
            if result.status is CompactImportStatus.WRITTEN:
                stats.imported += 1
            elif result.status is CompactImportStatus.DUPLICATE:
                stats.duplicates += 1
            else:  # REFUSED_ACL / REFUSED_MODERATION — policy, not transport
                logger.warning(
                    "lazy fetch gated fed_id=%s peer=%s reason=%s",
                    record.id,
                    peer_id,
                    result.reason,
                )
                stats.gated += 1
    return stats
