"""S2 phase 2 — the background federation metadata poller (ADR-0021 Q10.2).

ArchCom ruling Q10.1 (2026-09-20): ORCHESTRATION lives in the Vesma
process; the mesh is TRANSPORT. This module is the orchestration side
of the poll-first metadata sync: a asyncio background task that, every
tick, asks each configured peer for the next page of its
``federation_index`` by shelling out to the mesh CLI

    <mesh_bin> sync-meta --config <mesh.yaml> --peer <id> --json [--since <rev>]

(the Go-side subcommand is built by the parallel mesh track; this
module codes against its JSON contract and never imports it), parses
the envelope, and imports the records IN-PROCESS through
:meth:`vesmaro.storage.sqlite_store.SQLiteStore.upsert_index_entries`
with ``sender_peer_id`` set — every gate (no-federate, title
blocklist, origin-mutation guard, LWW) is already in the store.

Isolation (task brief): the poller touches ONLY ``federation_index``
(via the upsert) and ``federation_poll_state`` (its watermark). It
never writes ``memories``, never runs the pipeline, and never emits
content — the pages it consumes are metadata-only by wire contract.

Watermark: per-peer ``since_rev`` persisted after every successfully
imported page (``mark_poll_ok``), so a crash mid-peer resumes from the
last good page next tick. The next request carries it as ``--since``.
At-least-once semantics; the idempotent LWW upsert makes replay
harmless.

Failure policy: one peer's error (non-zero CLI exit, broken JSON,
subprocess timeout, store failure) NEVER kills the loop — it is logged
at INFO, recorded in ``federation_poll_state.last_error``, and the
peer is retried on the next tick (the interval IS the backoff).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from vesmaro.compact import FederationIndexEntry
from vesmaro.config import FederationConfig
from vesmaro.storage.sqlite_store import SQLiteStore

logger = logging.getLogger(__name__)

#: Import batch size for :meth:`SQLiteStore.upsert_index_entries` — the
#: store loops per-entry inside one transaction, so the batch bounds the
#: transaction length (WAL lock hold time) while keeping per-page row
#: counts in the hundreds.
META_POLL_BATCH: Final[int] = 200

#: Page cap per peer per tick. ``has_more: true`` triggers an immediate
#: follow-up page in the SAME tick until the peer is drained or this cap
#: is hit; on cap exhaustion the remainder waits for the next tick (the
#: watermark already points past the consumed rows).
META_POLL_MAX_PAGES: Final[int] = 10

#: Wall-clock budget for one mesh CLI invocation. A hung CLI must wedge
#: at most one page of one tick, never the loop.
META_POLL_PAGE_TIMEOUT_S: Final[float] = 30.0

#: stderr tail kept in the error surface — diagnostics, not a log sink.
_STDERR_TAIL_CHARS: Final[int] = 300

#: stdout tail kept when JSON-envelope extraction fails (noise strategy
#: (c) below) — shows the operator WHAT was on the wire, not a log sink.
_STDOUT_TAIL_CHARS: Final[int] = 300


class MetaPollError(Exception):
    """Typed poller failure for one peer-page fetch/parse.

    Carries enough context (peer id, argv-exit status, reason) for the
    INFO log line and the ``last_error`` column; never aborts the tick
    loop.
    """

    def __init__(self, peer_id: str, reason: str) -> None:
        self.peer_id = peer_id
        self.reason = reason
        super().__init__(f"peer={peer_id}: {reason}")


class SyncMetaPage(BaseModel):
    """Wire contract of ``mnemos-mesh sync-meta ... --json`` stdout.

    ``{"records": [MetadataRecord...], "latest_rev": N, "has_more": B}``
    — the mesh-track JSON envelope. Extra fields are ignored
    (forward-compat with additive Go-side fields, e.g. trigger codes);
    a missing ``has_more`` defaults to ``False`` (stop paging — the
    safe direction for a misbehaving CLI). ``records`` are kept RAW:
    per-record validation happens in
    :meth:`MetaPoller._parse_entries` so one malformed record never
    aborts a whole page.
    """

    model_config = ConfigDict(extra="ignore")

    records: list[dict[str, Any]] = Field(default_factory=list)
    latest_rev: int = Field(default=0, ge=0)
    has_more: bool = False


@dataclass(frozen=True, slots=True)
class PeerPollResult:
    """One peer's outcome for one poll pass (tick or CLI run).

    ``fetched`` counts records received from the CLI; ``accepted`` /
    ``rejected_by_gate`` / ``stale`` map 1:1 onto the store's
    :class:`IndexUpsertStats` (written / refused / LWW-superseded),
    with schema-invalid records folded into ``rejected_by_gate`` (the
    import gate refuses what it cannot validate). ``error`` is ``None``
    on success.
    """

    peer_id: str
    fetched: int = 0
    accepted: int = 0
    rejected_by_gate: int = 0
    stale: int = 0
    pages: int = 0
    latest_rev: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _chunks(seq: list[FederationIndexEntry], size: int) -> Sequence[Sequence[FederationIndexEntry]]:
    """Yield ``seq`` in ``size``-sized slices (import batching)."""
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def _validate_envelope(peer_id: str, payload: Any) -> SyncMetaPage:
    """Validate a parsed JSON payload as a :class:`SyncMetaPage`.

    Raises :class:`MetaPollError` naming the contract violation — a
    syntactically valid JSON stdout that is not an envelope is a peer
    bug, reported as honestly as broken JSON.
    """
    try:
        return SyncMetaPage.model_validate(payload)
    except ValidationError as exc:
        raise MetaPollError(peer_id, f"sync-meta envelope violates contract: {exc}") from exc


def _parse_sync_meta_stdout(peer_id: str, stdout: bytes) -> SyncMetaPage:
    """Parse the ``sync-meta`` JSON envelope out of possibly noisy stdout.

    The mesh track moved its logs to stderr, but the poller must SURVIVE
    a noisy stdout (an older mesh build, a misconfigured CLI, a plugin
    printing to fd 1). Strategies, in order:

    a. the whole stdout parses as JSON — the clean-wire case;
    b. otherwise find the FIRST line starting with ``{`` and accumulate
       from it to EOF — the envelope is ONE object printed LAST, so any
       chatter before that line is skipped;
    c. neither parses → an honest :class:`MetaPollError` carrying the
       stdout TAIL, so ``last_error`` shows what was actually on the
       wire (trailing noise AFTER the envelope is not tolerated: the
       contract says the envelope ends the output).

    Decoding uses ``errors="replace"``: replacement characters inside
    noise lines are harmless to (b); if the envelope itself is
    undecodable the JSON parse fails and surfaces as (c) — never a
    silent skip.
    """
    text = stdout.decode("utf-8", errors="replace")
    # (a) clean wire: the whole stdout is the envelope.
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        pass
    else:
        return _validate_envelope(peer_id, payload)
    # (b) prefix noise: the envelope opens at the first "{"-prefixed
    # line and runs to EOF (one object, printed last).
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("{")), None)
    if start is not None:
        try:
            payload = json.loads("\n".join(lines[start:]))
        except json.JSONDecodeError:
            pass
        else:
            return _validate_envelope(peer_id, payload)
    # (c) honest error with the tail of what was on the wire.
    tail = text[-_STDOUT_TAIL_CHARS:].strip()
    raise MetaPollError(
        peer_id,
        f"sync-meta stdout is not valid JSON (no envelope found): {tail or 'empty stdout'!r}",
    )


class MetaPoller:
    """Per-process federation metadata poller (S2 phase 2).

    Lifecycle mirrors :class:`vesmaro.scanner.BackgroundScanner` but on
    asyncio (the poller spends its life awaiting a subprocess, which
    must not occupy a thread): :meth:`start` launches :meth:`run` as a
    task (idempotent), :meth:`stop` wakes the loop and awaits it.
    Constructing the poller is side-effect free — the background task
    only exists between ``start`` and ``stop``.

    Multi-worker caveat: with ``runtime.uvicorn_workers > 1`` every
    worker process runs its own poller (the FastAPI lifespan is
    per-worker). Redundant fetches are wasted work, never corruption —
    the upsert is idempotent (LWW) and the watermark is per-peer — but
    operators running workers > 1 should keep that in mind; ``serve``
    logs a warning.
    """

    def __init__(self, store: SQLiteStore, federation: FederationConfig) -> None:
        self._store = store
        self._federation = federation
        self._config = federation.meta_poll
        self._title_blocklist = list(federation.index_title_blocklist)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        #: The in-flight mesh CLI child, if any (see :meth:`stop`).
        self._inflight: asyncio.subprocess.Process | None = None

    # ── Peer resolution ───────────────────────────────────────────────────

    def peer_ids(self) -> list[str]:
        """Resolve the poll target list (deterministic order).

        ``"all"`` → every :attr:`FederationConfig.peers` key; an
        explicit list → verbatim (already validated at the config
        boundary against the peers map).
        """
        peers = self._config.peers
        if isinstance(peers, str):
            return sorted(self._federation.peers)
        return list(dict.fromkeys(peers))

    # ── Background lifecycle ──────────────────────────────────────────────

    @property
    def running(self) -> bool:
        """Whether the background task is alive."""
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        """Launch the background loop (idempotent).

        The first tick runs immediately — a freshly enabled poller
        should converge the mirror now, not after one interval.
        """
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self.run(), name="mnemos-meta-poller")
        logger.info(
            "meta poller started interval=%ds peers=%s mesh_bin=%s mesh_config=%s",
            self._config.interval_seconds,
            self.peer_ids(),
            self._config.mesh_bin,
            self._config.mesh_config_path,
        )

    async def stop(self, grace_s: float = 5.0) -> None:
        """Signal the loop to stop and await it (bounded by ``grace_s``).

        On grace expiry the loop task is cancelled AND the in-flight
        mesh CLI child is killed FIRST (review thread: a cancel
        delivered mid-``communicate()`` aborts the pipe readers but does
        NOT stop the subprocess — without the kill the child would run
        on until its own 30s page timeout, wedging shutdown).
        """
        self._stop.set()
        task = self._task
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=grace_s)
        except TimeoutError:
            self._kill_inflight()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._task = None

    def _kill_inflight(self) -> None:
        """Kill the in-flight mesh CLI child, if one exists (best-effort).

        Idempotent and race-safe: a child that already exited (or was
        never spawned) is a no-op; ``ProcessLookupError`` from a
        concurrent reap is suppressed.
        """
        proc = self._inflight
        self._inflight = None
        if proc is None or proc.returncode is not None:
            return
        logger.info("meta poller: killing in-flight mesh CLI child pid=%s on stop", proc.pid)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()

    async def run(self) -> None:
        """The background loop: tick, then wait the interval (or stop)."""
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:
                logger.exception("meta poller: unexpected tick failure (loop continues)")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._config.interval_seconds)
            except TimeoutError:
                continue

    # ── One tick ──────────────────────────────────────────────────────────

    async def tick(self) -> list[PeerPollResult]:
        """Poll every configured peer once, sequentially.

        One peer's failure is contained: the result carries the error
        and the remaining peers still run. Sequential by design — one
        subprocess at a time keeps peak load on the local mesh leg
        predictable and the log lines readable.
        """
        return [await self.poll_peer(peer_id) for peer_id in self.peer_ids()]

    async def poll_once(self, peer_id: str) -> PeerPollResult:
        """Single-peer one-shot entry point (the ``meta-poll`` CLI)."""
        return await self.poll_peer(peer_id)

    async def poll_peer(self, peer_id: str) -> PeerPollResult:
        """Drain one peer's index pages for this pass.

        Pages are fetched with ``--since <watermark>`` and imported in
        :data:`META_POLL_BATCH` batches; after every successfully
        imported page the watermark is persisted
        (:meth:`SQLiteStore.mark_poll_ok`). ``has_more: true`` chains
        the next page inside this pass up to :data:`META_POLL_MAX_PAGES`;
        beyond the cap the peer resumes next tick.

        Any failure (CLI exit, JSON, timeout, store) returns a result
        with ``error`` set and records ``last_error`` in the poll state
        — the watermark is NOT advanced, so the failed page re-fetches.
        """
        next_ts = (datetime.now(UTC) + timedelta(seconds=self._config.interval_seconds)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        state = self._store.get_poll_state(peer_id)
        since_rev = state.since_rev if state is not None else 0
        result = PeerPollResult(peer_id=peer_id, latest_rev=since_rev)
        try:
            for _page in range(META_POLL_MAX_PAGES):
                page = await self._fetch_page(peer_id, since_rev)
                entries, invalid = self._parse_entries(peer_id, page.records)
                fetched = len(page.records)
                accepted = refused = stale = 0
                for batch in _chunks(entries, META_POLL_BATCH):
                    stats = self._store.upsert_index_entries(
                        batch,
                        sender_peer_id=peer_id,
                        title_blocklist=self._title_blocklist,
                    )
                    accepted += stats.written
                    refused += stats.refused
                    stale += stats.stale
                # Watermark never regresses (a peer that rebuilt its DB
                # with lower rowids must not rewind our checkpoint).
                since_rev = max(since_rev, page.latest_rev)
                self._store.mark_poll_ok(peer_id, since_rev)
                result = PeerPollResult(
                    peer_id=peer_id,
                    fetched=result.fetched + fetched,
                    accepted=result.accepted + accepted,
                    rejected_by_gate=result.rejected_by_gate + refused + invalid,
                    stale=result.stale + stale,
                    pages=result.pages + 1,
                    latest_rev=since_rev,
                )
                if not page.has_more:
                    break
            else:
                logger.info(
                    "meta poll peer=%s page cap reached (%d pages) — resuming next tick",
                    peer_id,
                    META_POLL_MAX_PAGES,
                )
        except (MetaPollError, OSError, sqlite3.Error) as exc:
            reason = str(exc)
            logger.info("meta poll peer=%s error=%s", peer_id, reason)
            self._store.mark_poll_error(peer_id, reason)
            return PeerPollResult(
                peer_id=peer_id,
                fetched=result.fetched,
                accepted=result.accepted,
                rejected_by_gate=result.rejected_by_gate,
                stale=result.stale,
                pages=result.pages,
                latest_rev=result.latest_rev,
                error=reason,
            )
        logger.info(
            "meta poll peer=%s fetched=%d accepted=%d rejected_by_gate=%d stale=%d "
            "latest_rev=%d next=%s",
            peer_id,
            result.fetched,
            result.accepted,
            result.rejected_by_gate,
            result.stale,
            result.latest_rev,
            next_ts,
        )
        return result

    # ── Mesh CLI leg ──────────────────────────────────────────────────────

    async def _fetch_page(self, peer_id: str, since_rev: int) -> SyncMetaPage:
        """Run one ``sync-meta`` invocation and parse its JSON envelope.

        Raises :class:`MetaPollError` on: executable not found
        (:class:`OSError` is re-raised as-is by the caller for the
        generic no-binary case — here we wrap what we own), non-zero
        exit, timeout, non-JSON stdout, or an invalid envelope. Each is
        an honest per-peer error: INFO log + ``last_error`` + next tick.
        """
        argv = [
            self._config.mesh_bin,
            "sync-meta",
            "--config",
            self._config.mesh_config_path,
            "--peer",
            peer_id,
            "--json",
        ]
        if since_rev > 0:
            # First poll (no watermark) omits --since: the peer serves
            # from rev 0 and we learn its latest_rev.
            argv += ["--since", str(since_rev)]
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise MetaPollError(peer_id, f"cannot execute mesh bin {argv[0]!r}: {exc}") from exc
        try:
            # Track the child so stop() can kill it on grace-cancel (a
            # cancelled communicate() orphans a live subprocess).
            self._inflight = proc
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=META_POLL_PAGE_TIMEOUT_S
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise MetaPollError(
                peer_id, f"sync-meta timed out after {META_POLL_PAGE_TIMEOUT_S:.0f}s"
            ) from exc
        finally:
            self._inflight = None
        if proc.returncode != 0:
            tail = stderr.decode("utf-8", errors="replace")[-_STDERR_TAIL_CHARS:].strip()
            raise MetaPollError(
                peer_id, f"sync-meta exited {proc.returncode}: {tail or 'no stderr'}"
            )
        return _parse_sync_meta_stdout(peer_id, stdout)

    # ── Record parsing ────────────────────────────────────────────────────

    @staticmethod
    def _parse_entries(
        peer_id: str, raw_records: list[dict[str, Any]]
    ) -> tuple[list[FederationIndexEntry], int]:
        """Validate page records into entries, tolerating bad records.

        A record that fails :class:`FederationIndexEntry` validation is
        SKIPPED with an INFO log (counted by the caller as a gate
        rejection) — one malformed record never aborts the page, and a
        misbehaving peer cannot wedge the poller with garbage.
        """
        entries: list[FederationIndexEntry] = []
        invalid = 0
        for record in raw_records:
            try:
                entries.append(FederationIndexEntry.model_validate(record))
            except ValidationError as exc:
                invalid += 1
                logger.info(
                    "meta poll peer=%s invalid record skipped id=%r: %s",
                    peer_id,
                    record.get("id"),
                    exc.errors()[0].get("msg", "validation error"),
                )
        return entries, invalid
