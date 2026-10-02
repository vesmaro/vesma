"""VesmaSDK — the thin typed facade over :class:`vesmaro.manager.MemoryManager`.

The programmatic contract surface for adapters (vesma #125, Wave 3):
the Hermes migration (next wave) and any in-process harness consume the
memory server through THIS class instead of weaving manager calls into
adapter code. Local-first by construction — the SDK talks to the
manager directly (no HTTP/MCP hop); remote deployments use the MCP /
REST surfaces, which call the same manager methods.

The facade owns no DOMAIN logic — validation, gating, idempotency, and
observability live in the manager paths exactly as the MCP/REST surfaces
see them. Two channel-boundary duties are the facade's OWN, mirroring
what every other surfaced channel does at its edge (W3 security review
F1/F2):

* ``recall`` scans every echoed item at issuance
  (``MemoryManager.scan_issuance_item`` over content + title —
  per-item redactions, refuse-mode drop), exactly like the
  ``mnemos_search`` / REST ``/search`` channels. The SDK returns
  SCANNED item dicts, never stored rows.
* ``remember`` validates caller-supplied tags against the tag contract
  (``validate_tag_contract``, deployment strictness knob), exactly like
  the ``mnemos_add`` / REST write channels.

Verb → manager method map:

============================  ===========================================
SDK verb                      Manager method
============================  ===========================================
``remember``                  ``MemoryManager.add`` (knowledge pipeline:
                              enters ``raw``, write-path secret scan;
                              tag contract validated at THIS channel —
                              F2) + context-reachable after the
                              pipeline advances it
``recall``                    ``MemoryManager.search`` (hybrid RRF, A9
                              project predicate pre-RRF) + ISSUANCE SCAN
                              of every echoed item at this channel (F1)
``forget``                    ``MemoryManager.get`` (project guard) +
                              ``MemoryManager.delete``
``stats``                     ``MemoryManager.stats`` (project slice =
                              presentation, see the method docstring)
``assemble_context``          ``MemoryManager.assemble_context``
                              (ADR-0017 D1 pipeline; the ADR-0018 entry
                              invariant — scan, provenance, status gate
                              — runs inside)
``rewrite``                   ``MemoryManager.context_rewrite``
                              (ADR-0018 ``on_context_rewrite`` event:
                              idempotent, version-less)
============================  ===========================================

Construction mirrors the manager (local-first): pass ``settings`` to
get a manager-owned SDK, or pass an existing ``manager`` (tests,
embedding into a server that already holds one). Exactly one of the two.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from vesmaro.models import Memory, MemoryCreate, validate_tag_contract

if TYPE_CHECKING:
    from vesmaro.config import Settings
    from vesmaro.manager import MemoryManager

logger = logging.getLogger(__name__)


class VesmaSDK:
    """Typed facade over ``MemoryManager`` — see the module docstring."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        manager: MemoryManager | None = None,
    ) -> None:
        """Create the SDK over exactly one manager.

        Args:
            settings: Construct a NEW ``MemoryManager`` from these
                settings (local-first single-process use).
            manager: Reuse an EXISTING manager (tests spy on it; servers
                embed the SDK over their own instance).

        Raises:
            ValueError: Neither or both arguments were given.
        """
        from vesmaro.manager import MemoryManager as _ConcreteManager

        if manager is not None:
            if settings is not None:
                raise ValueError("pass exactly one of settings or manager")
            self._manager: MemoryManager = manager
        else:
            if settings is None:
                raise ValueError("pass exactly one of settings or manager")
            self._manager = _ConcreteManager(settings)

    @property
    def manager(self) -> MemoryManager:
        """The underlying manager (escape hatch for surfaced operations)."""
        return self._manager

    def close(self) -> None:
        """Release the underlying manager."""
        self._manager.close()

    # ── Five verbs + rewrite ─────────────────────────────────────────────

    def remember(
        self,
        content: str,
        project: str,
        agent: str,
        **kw: Any,
    ) -> Memory:
        """Store one memory (tag contract validated HERE, then ``add``).

        ``kw`` passes through to the ``MemoryCreate`` fields (``title``,
        ``tags``, ``memory_type``, ``source``, ``metadata``, …). Caller
        tags are validated against the tag contract at THIS channel
        (W3 review F2 — ``validate_tag_contract`` with the deployment's
        ``vesmaro.strict_tag_contract`` knob, mirroring the ``mnemos_add``
        / REST write channels): a violation raises
        :class:`vesmaro.models.TagContractError` (a ``ValueError``)
        BEFORE any write. The entry then enters the knowledge pipeline
        at ``raw`` with the write-path secret scan.
        """
        if "tags" in kw:
            kw["tags"] = validate_tag_contract(
                list(kw["tags"]),
                strict=self._manager.settings.vesma.strict_tag_contract,
            )
        data = MemoryCreate(content=content, **kw)
        memory = self._manager.add(data, project=project, agent=agent)
        # vesma-canon v1.0.0 (canon §9): the manager canon gate already
        # logged + attached violations to ``metadata["canon_warnings"]``;
        # THIS channel echoes the codes at its edge so an SDK caller sees
        # the warning without parsing manager logs (write still succeeds —
        # warn mode never blocks).
        warnings = memory.metadata.get("canon_warnings")
        if warnings:
            codes = ",".join(w["code"] for w in warnings)
            logger.warning(
                "sdk.remember: canon warnings id=%s codes=%s — see "
                "metadata['canon_warnings'] on the returned memory",
                memory.id,
                codes,
            )
        logger.info("sdk.remember: project=%s agent=%s id=%s", project, agent, memory.id)
        return memory

    def recall(
        self,
        query: str,
        project: str,
        **kw: Any,
    ) -> list[dict[str, Any]]:
        """Hybrid RRF recall, issuance-scanned at THIS channel (F1).

        ``kw`` passes through to ``MemoryManager.search`` (``tags``,
        ``agent``, ``limit``, ``hybrid_alpha``, …). Every echoed item is
        then scanned with ``scan_issuance_item`` over content + title —
        matched spans become ``<REDACTED:<pattern>>`` in the returned
        copy (per-item ``redactions`` / ``redacted_patterns``), refuse
        mode (``ccr.retrieve_refuse_on_secret``) DROPS the item, exactly
        like the ``mnemos_search`` / REST ``/search`` channels. The SDK
        therefore returns SCANNED item dicts (``id``, ``title``,
        ``content``, ``tags``, ``score``, ``search_type``, ``status``,
        ``redactions``, optional ``redacted_patterns``) — never stored
        rows; a raw-row escape hatch is ``VesmaSDK.manager.search``.
        """
        results = self._manager.search(query=query, project=project, **kw)
        items: list[dict[str, Any]] = []
        total_redactions = 0
        for r in results:
            scan = self._manager.scan_issuance_item(
                r.memory.effective_content(),
                title=r.memory.auto_title(),
                context=f"sdk:recall:{r.memory.id}",
            )
            if scan.refused:
                continue
            item: dict[str, Any] = {
                "id": r.memory.id,
                "title": scan.title,
                "content": scan.content,
                "tags": r.memory.tags,
                "score": r.score,
                "search_type": r.search_type,
                "status": r.memory.status,
                "redactions": scan.redactions,
            }
            if scan.redactions:
                item["redacted_patterns"] = scan.redacted_patterns
            items.append(item)
            total_redactions += scan.redactions
        logger.info(
            "sdk.recall: query=%s project=%s hits=%d issued=%d redactions=%d",
            query,
            project,
            len(results),
            len(items),
            total_redactions,
        )
        return items

    def forget(self, memory_id: str, project: str) -> bool:
        """Delete one memory, project-guarded (``get`` + ``delete``).

        ``delete`` itself is global by id, so a facade caller scoped to a
        project must not be able to remove another project's memory.
        Unknown id → ``False``; known id under a DIFFERENT project →
        ``ValueError`` (no cross-project deletion oracle beyond the
        error itself).
        """
        memory = self._manager.get(memory_id)
        if memory is None:
            return False
        if memory.project != project:
            raise ValueError("memory belongs to a different project (cross-project delete denied)")
        deleted = self._manager.delete(memory_id)
        if deleted:
            logger.info("sdk.forget: project=%s id=%s", project, memory_id)
        return deleted

    def stats(self, project: str | None = None) -> dict[str, Any]:
        """Server stats (delegates to ``MemoryManager.stats``).

        ``project=None`` returns the full manager envelope. With a
        ``project``, the same envelope gains two presentation keys —
        ``project`` and ``project_total`` (from the manager's own
        per-project counts) — so adapter dashboards get their slice
        without a second data path.
        """
        result = self._manager.stats()
        if project is not None:
            projects = result.get("projects", {})
            if not isinstance(projects, dict):
                projects = {}
            result["project"] = project
            result["project_total"] = int(projects.get(project, 0))
        return result

    def assemble_context(
        self,
        session: str,
        project: str,
        **kw: Any,
    ) -> dict[str, Any]:
        """Assemble the model-facing context block.

        Delegates to ``MemoryManager.assemble_context`` — the ADR-0017
        D1 fixed pipeline (recall → optional CCR → filter → MANDATORY
        secret scan → CacheAligner → budget) with provenance on every
        injected block. ``kw`` passes through (``file``, ``budget``,
        ``mode``, ``expand_ccr``, ``agent``, ``query``, ``task``,
        ``lens``, …): ``task`` (ADR-0027 Phase 0) is the bare task slug
        narrowing recall to ``task:<slug>`` rows; ``lens`` selects a
        code-defined preset (``vesmaro.lens.Lens`` value) — both
        optional, tail-only, absent from the result unless given.
        """
        return self._manager.assemble_context(session=session, project=project, **kw)

    def rewrite(
        self,
        original_content: str,
        project: str,
        agent: str,
        session: str,
        **kw: Any,
    ) -> dict[str, Any]:
        """Report one ``on_context_rewrite`` event (ADR-0018).

        Delegates to ``MemoryManager.context_rewrite`` — idempotent
        (content-addressed event key), version-less; the original is
        stored losslessly through the knowledge pipeline. ``kw`` passes
        through (``supersedes``, ``diff``, ``include_marker``).
        """
        return self._manager.context_rewrite(
            content=original_content,
            project=project,
            agent=agent,
            session=session,
            **kw,
        )
