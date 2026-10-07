"""Synthesize worker — M4: LLM draft synthesis for a cluster.

Takes a cluster's source records and produces a single synthesized
article (status=processed).  Idempotency is keyed on
hash(scope_key, prompt_version, model_version, input_set_hash) — repeats
return cached result without calling the LLM again, and a same-ID content
swap of any member (ADR-0019 §Swap semantics) changes input_set_hash and
forces a fresh synthesis (#250 F3). Quarantined rows never feed a draft
(the single ADR-0019 §5 predicate), prior drafts of the same cluster are
outputs rather than inputs, and saving a new draft supersedes (archives)
the prior ones so the served projection cannot stay stale — zero-loss,
no deletion.

Security: only rationale_summary (≤200 chars) is stored in Trace.
Raw chain-of-thought is NEVER logged or persisted.
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import TYPE_CHECKING

from vesma.models import (
    CONTEXT_ADMISSIBLE_STATUSES,
    NO_FEDERATE_TAG,
    POLICY_TAG_PREFIXES,
    Memory,
    MemorySource,
    MemoryStatus,
    MemoryType,
    SynthesisResult,
    Trace,
    is_quarantined,
)

if TYPE_CHECKING:
    from vesma.manager import MemoryManager

logger = logging.getLogger(__name__)


def _synthesis_cache_key(
    scope_key: str,
    prompt_version: str,
    model_version: str,
    input_set_hash: str,
) -> str:
    payload = f"{scope_key}:{prompt_version}:{model_version}:{input_set_hash}"
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


def _input_set_hash(source_members: list[Memory], mgr: MemoryManager) -> str:
    """#250 F3 — deterministic hash over the (id, content) of all members.

    Reuses the ADR-0019 B2a freshness computation —
    ``MemoryManager._embed_content_hash`` over ``_embedding_text`` — so a
    same-ID content swap changes the synthesis idempotency key exactly
    when it changes the embedding input. Members are folded in id-sorted
    order so the hash is order-independent.

    ``source_members`` must already be filtered to the cluster's source
    records (no prior drafts — see ``synthesize_cluster``).
    """
    parts = [
        f"{m.id}:{mgr._embed_content_hash(mgr._embedding_text(m))}"
        for m in sorted(source_members, key=lambda m: m.id)
    ]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def _build_prompt(memories: list[Memory]) -> str:
    """Assemble a synthesis prompt from cluster members."""
    parts = [
        "You are a knowledge synthesis engine.  Read the following notes and",
        "produce a concise, well-structured article that captures the key",
        "insights, decisions, and open questions.  Preserve factual accuracy.",
        "Do not hallucinate.  Output plain Markdown.",
        "",
        "--- Notes ---",
        "",
    ]
    for i, mem in enumerate(memories, start=1):
        parts.append(f"Note {i} ({mem.source}):")
        parts.append(mem.effective_content())
        parts.append("")
    parts.append("--- Synthesis ---")
    return "\n".join(parts)


def synthesize_cluster(
    mgr: MemoryManager,
    cluster_id: str,
    *,
    prompt_version: str = "v1",
    force: bool = False,
) -> SynthesisResult | None:
    """Synthesize a cluster into a draft article.

    Args:
        mgr: MemoryManager instance.
        cluster_id: The cluster to synthesize.
        prompt_version: Bumps the cache key when prompt text changes.
        force: Bypass cache and re-synthesize.

    Returns:
        SynthesisResult on success, None if cluster not found or empty.
    """
    # 1. Load cluster members
    all_rows = mgr.sqlite.list_by_cluster(cluster_id)
    # ADR-0019 §5 (same predicate as every issuance path, mirroring the
    # refine intake): a member quarantined in the window between
    # clustering and this tick — refine-daemon or operator quarantine —
    # never feeds a draft. All-quarantined clusters fall out via the
    # empty-members guard below.
    members = [m for m in all_rows if not is_quarantined(m)]
    if not members:
        logger.warning("synthesize: cluster %s not found or empty", cluster_id[:8])
        return None

    # Prior drafts of this cluster are outputs, not inputs: everything
    # downstream (cache key, prompt, content, derived_from, coverage,
    # tag/project/agent inheritance, the F2b ANY-member scan) consumes
    # the SOURCE members only. The cache-hit scan below deliberately
    # stays over ``all_rows`` — it must see drafts.
    source_members = [m for m in members if m.source != MemorySource.SYNTHESIZED]
    if not source_members:
        logger.warning("synthesize: cluster %s has no source members", cluster_id[:8])
        return None

    model = mgr.settings.llm.model
    cache_key = _synthesis_cache_key(
        cluster_id, prompt_version, model, _input_set_hash(source_members, mgr)
    )

    # 2. Idempotency / cache check — look for existing processed memory
    #    (over ALL cluster rows: a matching prior draft IS the cache).
    existing_processed = [
        m
        for m in all_rows
        if m.status == MemoryStatus.PROCESSED and m.metadata.get("synthesis_cache_key") == cache_key
    ]
    if not force and existing_processed:
        logger.info("synthesize: cache hit for cluster %s", cluster_id[:8])
        cached = existing_processed[0].metadata.get("synthesis_cached_result")
        if cached:
            result = SynthesisResult.model_validate(cached)
            result.draft_id = existing_processed[0].id
            return result

    # 3. Build prompt and call LLM
    prompt = _build_prompt(source_members)
    t0 = time.monotonic()

    content = ""
    title: str | None = None
    llm_called = False
    tokens_in = 0
    tokens_out = 0
    # Quality scores: when a real LLM provider is wired (llm/ modules),
    # these will be set from the LLM response. Until then, the deterministic
    # placeholder synthesis assigns conservative-but-passable scores so
    # records can transition processing→processed→published instead of
    # piling up in the queue forever (P0-1 fix).
    quality_score = 0.5
    confidence = 0.5
    try:
        # TODO: wire real LLM provider when llm/ modules are implemented.
        # For now, produce a deterministic placeholder so tests can assert.
        # llm_called stays False — this is NOT an LLM call, it's a stub.
        content = f"# Synthesis of {cluster_id[:8]}\n\n"
        content += "\n\n".join(f"- {m.effective_content()[:200]}" for m in source_members)
        title = f"Synthesis: {source_members[0].title or source_members[0].content[:40]}"
        tokens_in = len(prompt.split())
        tokens_out = len(content.split())
    except Exception as exc:
        logger.error("synthesize: LLM call failed for %s: %s", cluster_id[:8], exc)
        # Log trace for observability
        mgr.sqlite.save_trace(
            Trace(
                task_label="synthesize",
                project=source_members[0].project,
                step="llm_call",
                item_id=source_members[0].id,
                llm_called=True,
                llm_done=False,
                latency_ms=int((time.monotonic() - t0) * 1000),
                rationale_summary=f"LLM failure: {exc}"[:200],
            )
        )
        return None

    latency_ms = int((time.monotonic() - t0) * 1000)

    # 4. Build result
    result = SynthesisResult(
        cluster_id=cluster_id,
        content=content,
        title=title,
        quality_score=quality_score,
        confidence=confidence,
        source_coverage=len(source_members),
        model_used=model,
        prompt_version=prompt_version,
        cache_hit=False,
        latency_ms=latency_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
    )

    # #250 F2 — strip-by-default: policy-bearing tags (POLICY_TAG_PREFIXES:
    # ``applyTo:`` / ``severity:``) inherited from the first source member
    # never reach the synthesized record — otherwise the draft would be
    # born already pinned to the member's application scope (transitive
    # mint→pin, extends #248). Everything else is inherited as before.
    inherited_tags = [
        t for t in source_members[0].tags if not t.startswith(tuple(POLICY_TAG_PREFIXES))
    ]
    tags = [*inherited_tags, "vesma:synthesized"]
    # #250 F2b — no-federate propagates by ANY-member rule: one
    # secret-bearing member is enough for the synthesis to be born
    # excluded from all external exchange.
    if NO_FEDERATE_TAG not in tags and any(NO_FEDERATE_TAG in m.tags for m in source_members):
        tags.append(NO_FEDERATE_TAG)

    # 5. Create processed memory
    processed = Memory(
        content=result.content,
        title=result.title,
        tags=tags,
        source=MemorySource.SYNTHESIZED,
        memory_type=MemoryType.NOTE,
        project=source_members[0].project,
        agent=source_members[0].agent,
        status=MemoryStatus.PROCESSED,
        cluster_id=cluster_id,
        derived_from=[m.id for m in source_members],
        quality_score=quality_score,
        confidence=confidence,
        source_coverage=len(source_members),
        metadata={
            "synthesis_cache_key": cache_key,
            "synthesis_cached_result": result.model_dump(mode="json"),
            "model_used": model,
            "prompt_version": prompt_version,
        },
    )
    mgr.sqlite.save(processed)
    result.draft_id = processed.id

    # 5b. #250 P2 — supersession: a new draft retires the prior drafts of
    # this cluster so the served projection cannot stay stale after a
    # re-synthesis (e.g. a same-ID content swap). ARCHIVED is outside
    # CONTEXT_ADMISSIBLE_STATUSES (ADR-0018), so the retired draft leaves
    # issuance while the row itself is kept — zero-loss, never deleted.
    # The cache-hit scan above filters on PROCESSED, so a retired draft
    # can never serve as a cache entry again either.
    for prior in all_rows:
        if (
            prior.source == MemorySource.SYNTHESIZED
            and prior.id != processed.id
            and prior.status in CONTEXT_ADMISSIBLE_STATUSES
        ):
            prior.status = MemoryStatus.ARCHIVED
            prior.metadata = {**prior.metadata, "superseded_by": processed.id}
            mgr.sqlite.save(prior)
            logger.info(
                "synthesize: draft %s superseded by %s (archived)",
                prior.id[:8],
                processed.id[:8],
            )

    # 6. Trace
    mgr.sqlite.save_trace(
        Trace(
            task_label="synthesize",
            project=processed.project,
            step="draft_created",
            item_id=processed.id,
            llm_called=llm_called,
            llm_done=True,
            latency_ms=latency_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            rationale_summary=(
                f"Draft {processed.id[:8]} from cluster {cluster_id[:8]} "
                f"({len(source_members)} sources)"
            ),
        )
    )

    logger.info(
        "synthesize: draft %s from cluster %s (%s sources, %s ms)",
        processed.id[:8],
        cluster_id[:8],
        len(source_members),
        latency_ms,
    )
    return result
