"""Hermes Agent adapter on the ADR-0017 D1 provider contract (#125, Wave 5).

Migration target for the legacy Hermes plugin (``integrations/hermes/``):
that plugin spoke BESPOKE raw HTTP against ~15 REST endpoints with its own
urllib client, TOTP/login/session-auth flow, circuit breaker, background
threads, and an auto-publish bypass — adapter-private context delivery,
exactly what ADR-0017 gap 1 names. This module replaces that path: the
Hermes-side plugin becomes a thin shim over THIS adapter, and every memory
operation routes through the D1 contract surfaces — the
:class:`vesmaro.sdk.VesmaSDK` facade (``remember`` / ``recall`` /
``stats`` / ``rewrite``) and the W3 lifecycle hooks
(:mod:`vesmaro.hooks` — ``pre_llm_call`` / ``on_session_start`` /
``post_tool_call``) — in-process, no HTTP hop::

    Hermes MemoryProvider ABC
        ↓ (thin shim, deploy-only: integrations/hermes/__init__.py)
    HermesMemoryAdapter                      ← THIS module (no Hermes imports)
        ↓ VesmaSDK facade + vesmaro.hooks
    MemoryManager
        ↓
    SQLite + vectors + Obsidian vault

Where each legacy duty went:

* bespoke HTTP client + auth/TOTP flow + circuit breaker → GONE. The SDK is
  in-process (loopback by construction, ADR-0017 D6); failures are typed
  exceptions raised by the contract channels, not network partitions to
  survive.
* prefetch → ``pre_llm_call`` hook → ``assemble_context`` (the D1 fixed
  pipeline: recall → filter → MANDATORY scan → align → budget — provenance
  on every block). The raw ``/search`` prefetch could leak secrets; the
  contract pipeline cannot.
* sync_turn / on_memory_write / on_session_end writes → ``VesmaSDK.remember``
  (tag contract validated at the channel BEFORE any write; the write-path
  secret scan runs inside ``add``).
* on_pre_compress (facts lost to Hermes' context compression) → the
  ADR-0018 ``on_context_rewrite`` event via ``VesmaSDK.rewrite`` — the
  original lands in LTM losslessly and idempotently.
* auto-publish with ``skip_quality_check`` → GONE (ADR-0019 Phase D).
  Immediate visibility is honest SERVER semantics since B2b: the adapter
  writes WITHOUT an explicit ``status`` and the ``vesmaro.visibility``
  policy owns the initial visibility through the fail-closed ingest gate
  — ``immediate`` (default) publishes clean content at once with
  ``pipeline_state=pending`` (refused content is stored RAW,
  zero-loss); ``curated`` holds the row RAW + pending until the refine
  cycle gates the refined projection. The legacy ``publish_on_write``
  constructor knob is still ACCEPTED (the Hermes shim passes it) but has
  no write-path effect: per-adapter visibility flags were explicitly
  rejected by the ADR-0019 committee — visibility is a server-level
  default.

What deliberately stays HERE (harness policy, not contract): the
write-sparingly significance rules (``sync_min_user_chars`` /
``sync_interval``), the session-summary extraction, the builtin-memory
mirror mapping. The adapter owns no retrieval/filter/scanning logic of its
own — those stages live in the manager paths the contract surfaces call.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from vesmaro.hooks import on_session_start as _hook_on_session_start
from vesmaro.hooks import post_tool_call as _hook_post_tool_call
from vesmaro.hooks import pre_llm_call as _hook_pre_llm_call
from vesmaro.models import (
    AgentRecallQuery,
    Memory,
    MemorySource,
    MemoryType,
    validate_tag_contract,
)
from vesmaro.sdk import VesmaSDK

logger = logging.getLogger(__name__)

#: Legacy Hermes plugin posture — a turn is significant when the user
#: message exceeds this many characters (write-sparingly harness policy).
SYNC_MIN_USER_CHARS = 50

#: Default "sync every Nth turn" interval (harness policy).
SYNC_INTERVAL = 10

#: Channel marker stamped on every adapter write (provenance; the legacy
#: plugin mislabeled its HTTP writes ``source="mcp"``).
_CHANNEL = "hermes-adapter"

#: Checkpoint markdown sections, mirroring the mnemos_save_context
#: MCP/REST channels so recall stays format-compatible.
_CHECKPOINT_FIELDS = ("goals", "completed", "in_progress", "decisions", "context")


def _require_str(value: str, label: str) -> str:
    """Boundary validation: non-empty string or raise."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required and must be a non-empty string")
    return value


def _section(value: str | list[str] | None) -> str | None:
    """Normalize a checkpoint field (str | list[str]) to markdown text."""
    if value is None:
        return None
    if isinstance(value, list):
        value = "\n".join(str(v) for v in value)
    value = value.strip()
    return value or None


class HermesMemoryAdapter:
    """Hermes Agent memory verbs mapped onto the D1 provider contract.

    The adapter is Hermes-shaped (the verb names mirror the Hermes
    ``MemoryProvider`` lifecycle) but imports NO Hermes code — it is
    constructible and testable in-process over any ``VesmaSDK``.

    Identity threading: ``project`` + ``agent`` are fixed at construction
    (validated against the tag contract up front — a bad slug fails HERE,
    not on the first write); the ``session`` is bound via
    :meth:`bind_session` (the shim binds it from Hermes'
    ``initialize(session_id=…)``) and threaded onto every session-scoped
    verb, including the A2 strict-mode CCR issuer context inside
    ``assemble_context`` and the N2 identity mandate of
    ``post_tool_call``.

    Visibility semantics (ADR-0019 Phase D): every write goes out WITHOUT
    an explicit ``status``, so the server's ``vesmaro.visibility`` policy
    owns the initial visibility through the fail-closed ingest gate
    (``immediate`` (default): clean content is PUBLISHED at once with
    ``pipeline_state=pending``; ``curated``: stored RAW + pending until
    the refine cycle gates the projection). The adapter no longer
    publishes on its own — the Hermes ``publish_on_write`` bypass is
    removed; the constructor knob is accepted for shim compatibility
    only and is a documented no-op.
    """

    def __init__(
        self,
        sdk: VesmaSDK,
        *,
        project: str,
        agent: str,
        auto_sync: bool = True,
        publish_on_write: bool = True,
        sync_min_user_chars: int = SYNC_MIN_USER_CHARS,
        sync_interval: int = SYNC_INTERVAL,
    ) -> None:
        _require_str(project, "project")
        _require_str(agent, "agent")
        # Fail fast on contract-breaking slugs: this is the exact tag set
        # every write below composes, validated with the deployment's own
        # strictness knob (the same call VesmaSDK.remember makes later).
        validate_tag_contract(
            [f"project:{project}", f"agent:{agent}", "mnemos:session"],
            strict=sdk.manager.settings.mnemos.strict_tag_contract,
        )
        if sync_interval < 1:
            raise ValueError(f"sync_interval must be >= 1, got {sync_interval!r}")
        if sync_min_user_chars < 0:
            raise ValueError(f"sync_min_user_chars must be >= 0, got {sync_min_user_chars!r}")
        self._sdk = sdk
        self._project = project
        self._agent = agent
        self._auto_sync = auto_sync
        self._sync_min_user_chars = sync_min_user_chars
        self._sync_interval = sync_interval
        self._session = ""
        self._turn_counter = 0
        # ADR-0019 Phase D: publish_on_write is accepted for shim
        # compatibility and deliberately NOT stored — it no longer has a
        # write-path effect. Say so loudly once: an operator flipping it
        # expects the old auto-publish and must learn the knob moved to
        # the vesmaro.visibility server policy.
        if not publish_on_write:
            logger.info(
                "hermes_adapter: publish_on_write=False is a no-op since "
                "ADR-0019 Phase D — initial visibility is owned by the "
                "vesmaro.visibility server policy (immediate publishes at "
                "ingest; curated holds the row raw+pending until refine)"
            )
        logger.info(
            "hermes_adapter: ready project=%s agent=%s auto_sync=%s publish_on_write=%s",
            project,
            agent,
            auto_sync,
            publish_on_write,
        )

    # ── Identity ────────────────────────────────────────────────────────

    @property
    def project(self) -> str:
        return self._project

    @property
    def agent(self) -> str:
        return self._agent

    @property
    def sdk(self) -> VesmaSDK:
        """The underlying SDK (surfaced-operations escape hatch for shims)."""
        return self._sdk

    @property
    def session(self) -> str:
        """The bound Hermes session id ("" until :meth:`bind_session`)."""
        return self._session

    def bind_session(self, session: str) -> None:
        """Bind (or rebind) the Hermes session id threaded onto verbs."""
        self._session = _require_str(session, "session")
        logger.debug("hermes_adapter: session bound %s", self._session)

    def _session_or_raise(self) -> str:
        if not self._session:
            raise ValueError("no session bound — call bind_session() before session-scoped verbs")
        return self._session

    def close(self) -> None:
        """Release the underlying SDK (shim shutdown hook)."""
        self._sdk.close()

    # ── Lifecycle hooks (W3 surfaces) ───────────────────────────────────

    def pre_llm_call(
        self,
        *,
        query: str | None = None,
        file: str | None = None,
        budget: int = 2048,
    ) -> dict[str, Any]:
        """Assemble the pre-LLM-call injection block (the D1 contract).

        Routes through the ``pre_llm_call`` hook → ``assemble_context``:
        recall (status-gated) → filter → mandatory secret scan →
        CacheAligner → budget, provenance on every block. ``query`` is the
        upcoming call's focus (the hook's ``context_hint`` — the explicit
        recall query). Result ``text`` is what the harness prepends to the
        model prompt.
        """
        session = self._session_or_raise()
        return _hook_pre_llm_call(
            self._sdk.manager,
            session=session,
            project=self._project,
            agent=self._agent,
            context_hint=query,
            file=file,
            budget=budget,
        )

    def session_start(self, *, limit: int = 5) -> dict[str, Any]:
        """Recall recent checkpoints (scanned at the channel) for bootstrap."""
        session = self._session_or_raise()
        return _hook_on_session_start(
            self._sdk.manager,
            session=session,
            project=self._project,
            agent=self._agent,
            limit=limit,
        )

    def post_tool_call(
        self,
        *,
        tool_name: str,
        output_text: str,
        auto_compress: bool | None = None,
        profile: str | None = None,
    ) -> dict[str, Any]:
        """CCR-autocompress one tool output (ADR-0018 entry point).

        Identity ``(agent, session)`` is threaded onto the cache row — the
        A2 register N2 mandate; there is no identity-less mode.
        """
        session = self._session_or_raise()
        return _hook_post_tool_call(
            self._sdk.manager,
            session=session,
            project=self._project,
            agent=self._agent,
            tool_name=tool_name,
            output_text=output_text,
            auto_compress=auto_compress,
            profile=profile,
        )

    # ── Writes (VesmaSDK.remember — tag contract at the channel) ───────

    def add_memory(
        self,
        content: str,
        tags: list[str],
        *,
        title: str | None = None,
        memory_type: MemoryType = MemoryType.NOTE,
        metadata: dict[str, Any] | None = None,
    ) -> Memory:
        """Store one caller-tagged memory (the ``mnemos_add`` counterpart).

        Tags pass through ``VesmaSDK.remember``'s channel validation —
        contract-breaking tags raise before any write. No explicit
        ``status``: the ``vesmaro.visibility`` server policy owns the
        initial visibility (ADR-0019 Phase D).
        """
        _require_str(content, "content")
        if not tags:
            raise ValueError("tags are required: project:<slug>, agent:<slug>, mnemos:<subtype>")
        meta = {"channel": _CHANNEL, **(metadata or {})}
        memory = self._sdk.remember(
            content,
            self._project,
            self._agent,
            title=title,
            tags=tags,
            memory_type=memory_type,
            source=MemorySource.MANUAL,
            metadata=meta,
        )
        return memory

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
    ) -> Memory | None:
        """Persist one significant turn as a ``mnemos:session`` entry.

        Write-sparingly harness policy (the legacy plugin's, kept): a turn
        is significant when the user message exceeds
        ``sync_min_user_chars`` OR this is every ``sync_interval``-th
        turn. Insignificant turns return ``None`` (nothing written).
        """
        if not self._auto_sync:
            return None
        self._turn_counter += 1
        significant = (
            len(user_content) > self._sync_min_user_chars
            or self._turn_counter % self._sync_interval == 0
        )
        if not significant:
            return None
        content = f"## User\n{user_content[:1000]}\n\n## Assistant\n{assistant_content[:1000]}"
        memory = self._sdk.remember(
            content,
            self._project,
            self._agent,
            tags=[f"project:{self._project}", f"agent:{self._agent}", "mnemos:session"],
            memory_type=MemoryType.CONVERSATION,
            source=MemorySource.MANUAL,
            metadata={
                "channel": _CHANNEL,
                "session_id": self._session,
                "turn": self._turn_counter,
            },
        )
        logger.info(
            "hermes_adapter.sync_turn: turn=%d project=%s id=%s",
            self._turn_counter,
            self._project,
            memory.id,
        )
        return memory

    def mirror_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> Memory | None:
        """Mirror one Hermes builtin memory write (MEMORY.md / USER.md).

        Only ``action == "add"`` mirrors (the legacy posture); ``target ==
        "user"`` is the USER.md mirror — tagged ``agent:user`` +
        ``mnemos:rule`` — everything else is the assistant's own learning.
        """
        if not self._auto_sync or action != "add" or not content.strip():
            return None
        is_user = target == "user"
        agent_tag = "user" if is_user else self._agent
        subtype = "rule" if is_user else "learning"
        memory = self._sdk.remember(
            content,
            self._project,
            agent_tag,
            tags=[f"project:{self._project}", f"agent:{agent_tag}", f"mnemos:{subtype}"],
            memory_type=MemoryType.FACT,
            source=MemorySource.MANUAL,
            metadata={
                "channel": _CHANNEL,
                "mirror_of": "hermes-builtin-memory",
                "target": target,
                **(metadata or {}),
            },
        )
        logger.debug("hermes_adapter.mirror_memory_write: target=%s id=%s", target, memory.id)
        return memory

    def session_end(self, messages: list[dict[str, Any]]) -> Memory | None:
        """Synthesize and store one ``mnemos:session`` summary per session.

        Harness policy: extract non-trivial user messages (>50 chars, last
        10, 300-char excerpts) and assistant responses (>50 chars, last 5),
        one entry per session — not per turn. Fewer than 2 messages or
        nothing extractable → ``None``.
        """
        if not self._auto_sync or len(messages) < 2:
            return None
        user_msgs: list[str] = []
        assistant_msgs: list[str] = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not isinstance(content, str) or not content:
                continue
            if role == "user" and len(content) > SYNC_MIN_USER_CHARS:
                user_msgs.append(content[:300])
            elif role == "assistant" and len(content) > SYNC_MIN_USER_CHARS:
                assistant_msgs.append(content[:300])
        if not user_msgs and not assistant_msgs:
            return None
        sections: list[str] = []
        if user_msgs:
            sections.append("## Key User Messages\n" + "\n".join(f"- {m}" for m in user_msgs[-10:]))
        if assistant_msgs:
            sections.append(
                "## Key Assistant Responses\n" + "\n".join(f"- {m}" for m in assistant_msgs[-5:])
            )
        content = "\n\n".join(sections)
        session = self._session or "unknown"
        memory = self._sdk.remember(
            content,
            self._project,
            self._agent,
            title=f"Session {session[:8]} summary",
            tags=[f"project:{self._project}", f"agent:{self._agent}", "mnemos:session"],
            memory_type=MemoryType.CONVERSATION,
            source=MemorySource.MANUAL,
            metadata={
                "channel": _CHANNEL,
                "session_id": self._session,
                "turn_count": self._turn_counter,
                "user_msg_count": len(user_msgs),
                "assistant_msg_count": len(assistant_msgs),
            },
        )
        logger.info(
            "hermes_adapter.session_end: project=%s id=%s turns=%d user_msgs=%d",
            self._project,
            memory.id,
            self._turn_counter,
            len(user_msgs),
        )
        return memory

    def save_checkpoint(
        self,
        *,
        goals: str | list[str] | None = None,
        completed: str | list[str] | None = None,
        in_progress: str | list[str] | None = None,
        decisions: str | list[str] | None = None,
        context: str | list[str] | None = None,
    ) -> Memory:
        """Store a structured ``mnemos:checkpoint`` (session checkpoint).

        Builds the same sectioned markdown as the ``mnemos_save_context``
        MCP/REST channels (format-compatible recall) but threads the
        ADAPTER's agent identity instead of the hardcoded ``agent:user``.
        """
        parts = [f"# Session checkpoint — {datetime.now(UTC).isoformat()}\n"]
        fields = zip(
            _CHECKPOINT_FIELDS,
            (goals, completed, in_progress, decisions, context),
            strict=True,
        )
        for field, value in fields:
            body = _section(value)
            if body:
                parts.append(f"## {field.replace('_', ' ').title()}\n{body}\n")
        content = "\n".join(parts)
        session = self._session or "unknown"
        memory = self._sdk.remember(
            content,
            self._project,
            self._agent,
            tags=[f"project:{self._project}", f"agent:{self._agent}", "mnemos:checkpoint"],
            memory_type=MemoryType.SESSION_CONTEXT,
            source=MemorySource.MANUAL,
            metadata={"channel": _CHANNEL, "session_id": session},
        )
        logger.info("hermes_adapter.save_checkpoint: project=%s id=%s", self._project, memory.id)
        return memory

    # ── Reads (VesmaSDK.recall / stats — issuance-scanned channels) ────

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        tags: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid recall through the SDK channel (issuance scan inside)."""
        _require_str(query, "query")
        return self._sdk.recall(query, self._project, limit=limit, tags=tags)

    def recall_checkpoints(
        self,
        *,
        query: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Recall recent checkpoints, scanned at THIS channel.

        Mirrors the ``mnemos_recall_context`` / ``on_session_start``
        channels: ``MemoryManager.recall_context`` returns raw stored rows,
        so this channel owns the boundary scan (refuse mode drops the
        checkpoint, logged with the memory id).
        """
        memories = self._sdk.manager.recall_context(project=self._project, query=query, limit=limit)
        checkpoints: list[dict[str, Any]] = []
        for m in memories:
            scan = self._sdk.manager.scan_issuance_item(
                m.effective_content(),
                context=f"hermes-adapter:recall_checkpoints:{m.id}",
            )
            if scan.refused:
                logger.warning(
                    "hermes_adapter.recall_checkpoints: checkpoint %s dropped (refuse mode)",
                    m.id,
                )
                continue
            item: dict[str, Any] = {
                "id": m.id,
                "content": scan.content,
                "created_at": m.created_at.isoformat(),
                "redactions": scan.redactions,
            }
            if scan.redactions:
                item["redacted_patterns"] = scan.redacted_patterns
            checkpoints.append(item)
        return checkpoints

    def agent_recall(
        self,
        agent: str | None = None,
        *,
        query: str | None = None,
        limit: int = 20,
        include_raw: bool = False,
    ) -> list[dict[str, Any]]:
        """Agent-scoped recall, scanned at THIS channel (M3 counterpart).

        Mirrors the ``mnemos_agent_recall`` MCP channel: content AND title
        are scanned; refuse mode drops the item.
        """
        recall_query = AgentRecallQuery(
            agent=agent or self._agent,
            project=self._project,
            query=query,
            limit=limit,
            include_raw=include_raw,
        )
        results = self._sdk.manager.agent_recall(recall_query)
        items: list[dict[str, Any]] = []
        for r in results:
            scan = self._sdk.manager.scan_issuance_item(
                r.memory.effective_content(),
                title=r.memory.auto_title(),
                context=f"hermes-adapter:agent_recall:{r.memory.id}",
            )
            if scan.refused:
                continue
            item: dict[str, Any] = {
                "id": r.memory.id,
                "title": scan.title,
                "content": scan.content,
                "tags": r.memory.tags,
                "created_at": r.memory.created_at.isoformat(),
                "status": r.memory.status,
                "redactions": scan.redactions,
            }
            if scan.redactions:
                item["redacted_patterns"] = scan.redacted_patterns
            items.append(item)
        return items

    def stats(self) -> dict[str, Any]:
        """Store stats with this deployment's project slice."""
        return self._sdk.stats(self._project)

    # ── ADR-0018 bridge ─────────────────────────────────────────────────

    def report_context_rewrite(
        self,
        original_content: str,
        *,
        supersedes: str | None = None,
        diff: str | None = None,
    ) -> dict[str, Any]:
        """Report Hermes' context rewrite (``on_context_rewrite`` event).

        The D1-contract replacement for the legacy ``on_pre_compress``
        text-only hint: the original of the block Hermes' compressor is
        about to discard lands in LTM losslessly (idempotent,
        content-addressed event; ``supersedes`` threads replacement
        lineage). Receipt: ``stored`` or ``deduplicated``.
        """
        _require_str(original_content, "original_content")
        session = self._session_or_raise()
        return self._sdk.rewrite(
            original_content,
            self._project,
            self._agent,
            session,
            supersedes=supersedes,
            diff=diff,
        )
