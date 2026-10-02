"""D5 measurement harness (ADR-0017 D5 + ADR-0018 metric pair, #125 W4).

Library module — the pytest surface lives in ``test_golden_baseline.py``.
Everything here is deterministic by construction:

- fresh manager per measurement (temp dir), corpus ingested in fixed
  order, deterministic lexical embedder (see ``deterministic_embedder``);
- no network, no wall-clock dependence, no RNG (feature hashing replaces
  all randomness);
- A9 variants are implemented as scoped patches, never edits to src/.

Measured axes:

1. retrieval quality — macro precision@k / recall@k (k ∈ {5, 10}) over
   the golden queries via ``MemoryManager.search`` (both legs: FTS5 +
   deterministic vector leg, RRF fusion);
2. injection-acceptance — every planted-secret entry that surfaces in a
   result set is issued through the REAL issuance channel
   (``scan_issuance_item`` mirroring ``mnemos_search`` / REST ``/search``)
   and must not leak its planted literal; acceptance = 1 - leak_rate;
3. A9 before/after — recall with the vector-leg project predicate ON
   (current code) vs OFF (``VectorStore.search`` patched to ignore the
   project kwarg), crossed with the over-fetch factor (x4 current vs x2
   pre-A9 depth), validating the ``VECTOR_LEG_OVERFETCH_FACTOR`` constant;
4. the ADR-0018 pair — replace-hit-rate / replace-regret-rate over the
   scripted rewrite scenario (see ``rewrite_scenario.py`` for the exact
   numerators/denominators).

ADR-0029 interplay (#315 search v2): every A9 leg runs the FULL modern
pipeline, soft project fallback included — a scoped query that returns
zero rows is retried once without the scope and surfaces cross-project
rows TAGGED ``project_scope_fallback=True``. Those rows are by-design
retrievals, not resolve-guard leaks: ``foreign_project_surfaced`` counts
UNTAGGED foreign rows only (the zero-tolerance invariant), tagged rows
are visible in ``foreign_project_fallback_surfaced`` and belt-checked
for all-or-nothing tagging (``scope_fallback_*`` metrics).
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from benchmarks.corpus.corpus import CORPUS, PLANTED_SECRETS, GoldenEntry
from benchmarks.corpus.deterministic_embedder import LexicalHashEmbedder
from benchmarks.corpus.queries import GOLDEN_QUERIES
from benchmarks.corpus.rewrite_scenario import (
    REWRITE_EVENTS,
    _block,
    control_blocks,
)
from vesmaro.config import Settings
from vesmaro.embeddings import EmbeddingProvider
from vesmaro.manager import MemoryManager
from vesmaro.models import MemoryCreate, MemorySource, MemoryStatus
from vesmaro.storage.vector_store import VectorStore

K_VALUES: tuple[int, ...] = (5, 10)

_SLUG_PROJECT: dict[str, str] = {e.slug: e.project for e in CORPUS}
_SLUG_ENTRY: dict[str, GoldenEntry] = {e.slug: e for e in CORPUS}


def _project_of(slug: str) -> str | None:
    return _SLUG_PROJECT.get(slug)


STATUS_TO_ENUM: dict[str, MemoryStatus] = {
    "published": MemoryStatus.PUBLISHED,
    "processed": MemoryStatus.PROCESSED,
    "raw": MemoryStatus.RAW,
}
SOURCE_TO_ENUM: dict[str, MemorySource] = {s.value: s for s in MemorySource}


# ── manager bootstrap ────────────────────────────────────────────────────────


def golden_settings(root: Path) -> Settings:
    """Settings for a golden manager.

    ``ccr.min_size_chars`` is lowered to 280 so the scripted scenario
    blocks (~330-800 chars, structurally realistic rewrite targets)
    actually enter the CCR cache; everything else keeps production
    defaults (redact-on-secret issuance, rrf fusion, status gating).
    """
    # ``model_validate`` (not the constructor): the Settings constructor's
    # static signature demands the nested config models, while runtime
    # pydantic coerces dicts either way — validate keeps mypy --strict
    # honest without hand-building three config models.
    settings = Settings.model_validate(
        {
            "vesma": {
                "vault_path": str(root / "vault"),
                "data_dir": str(root / "data"),
                "db_name": "golden.db",
            },
            "scanner": {"enabled": False},  # no background watcher in the harness
            "ccr": {"min_size_chars": 280},
        }
    )
    settings.resolve_paths()
    return settings


def build_golden_manager(
    root: Path, embedder: EmbeddingProvider | None = None
) -> tuple[MemoryManager, dict[str, str]]:
    """Ingest the golden corpus into a fresh manager.

    Returns ``(manager, slug_to_id)``. The deterministic embedder is
    installed BEFORE ingest so published entries embed consistently with
    query-time embeddings.

    ``embedder`` overrides the installed provider (S1m, ADR-0021 NM-0):
    passing the PRODUCTION embedder runs the same corpus + queries
    through the real model while keeping this ingest path identical —
    the model contour reuses the golden machinery instead of growing a
    parallel one. ``None`` keeps the BLAKE2b reference default.
    """
    installed = embedder if embedder is not None else LexicalHashEmbedder()
    mgr = MemoryManager(golden_settings(root))
    mgr._embedder = installed
    slug_to_id: dict[str, str] = {}
    for entry in CORPUS:  # fixed corpus order — determinism
        data = MemoryCreate(
            content=entry.content,
            title=entry.title,
            tags=_entry_tags(entry),
            source=SOURCE_TO_ENUM[entry.source],
            status=STATUS_TO_ENUM[entry.status],
        )
        memory = mgr.add(data, project=entry.project, agent=entry.agent)
        intended = STATUS_TO_ENUM[entry.status]
        if memory.status != intended:
            # ADR-0019 N1: a direct-seed publication whose content trips
            # the danger detectors is demoted to RAW by ``manager.add``.
            # The corpus's PLANTED entries model the residual-threat
            # population the issuance scans exist for (secrets introduced
            # by processing / pre-gate rows), so the harness restores the
            # intended status at the store level and re-embeds — the
            # search/ranking semantics the golden baseline pins are
            # unchanged.
            mgr.sqlite.update_status(memory.id, intended)
            mgr.vectors.upsert(
                memory.id,
                mgr.embed_for(memory),
                {"project": memory.project, "agent": memory.agent},
            )
            memory.status = intended
        slug_to_id[entry.slug] = memory.id
    return mgr, slug_to_id


def _entry_tags(entry: GoldenEntry) -> list[str]:
    tags = [f"project:{entry.project}", f"agent:{entry.agent}"]
    tags += [f"mnemos:{t}" for t in entry.mnemos_tags]
    tags += list(entry.free_tags)
    return tags


@contextlib.contextmanager
def fresh_golden_manager(
    root: Path, embedder: EmbeddingProvider | None = None
) -> Iterator[tuple[MemoryManager, dict[str, str]]]:
    """Build, yield, close — temp-dir hygiene for the pytest suite."""
    mgr, slug_to_id = build_golden_manager(root, embedder=embedder)
    try:
        yield mgr, slug_to_id
    finally:
        mgr.close()


# ── A9 variants ──────────────────────────────────────────────────────────────


@contextlib.contextmanager
def vector_predicate_off() -> Iterator[None]:
    """Scoped PRE-A9 emulation of the vector-leg store predicate.

    Wraps ``VectorStore.search`` so the ``project`` kwarg is dropped —
    candidates come from the WHOLE store at global rank (pre-A9
    behaviour). The manager-side authoritative resolve guard remains
    active, so no UNTAGGED foreign row leaks into results; what changes
    is which candidates fill the leg's contribution depth (global
    ranking instead of project-scoped ranking) — exactly the
    recall-relevant difference.

    ADR-0029 note: this emulation predates the #315 soft project
    fallback and runs INSIDE the full modern pipeline — a scoped query
    that zeroes out (the pre-A9 x2 depth makes that contour reachable)
    is retried without the scope and surfaces cross-project rows TAGGED
    ``project_scope_fallback=True``. Tagged rows are by-design ADR-0029
    behavior (see ``SearchMetrics``): the zero-tolerance invariant guards
    the resolve path only.
    """
    original = VectorStore.search

    def unscoped(
        self: VectorStore,
        query_embedding: list[float],
        limit: int = 20,
        *,
        project: str | None = None,
    ) -> list[tuple[str, float]]:
        result: list[tuple[str, float]] = original(self, query_embedding, limit, project=None)
        return result

    VectorStore.search = unscoped  # type: ignore[method-assign, unused-ignore]
    try:
        yield
    finally:
        VectorStore.search = original  # type: ignore[method-assign, unused-ignore]


@contextlib.contextmanager
def overfetch_factor(factor: int) -> Iterator[None]:
    """Scoped override of the vector-leg over-fetch constant.

    ``manager.search`` resolves ``VECTOR_LEG_OVERFETCH_FACTOR`` from
    module globals at call time, so the patch takes effect for every
    search inside the context. x4 is the current constant; x2 reproduces
    the pre-A9 contribution depth.
    """
    import mnemos.manager as manager_mod

    # Direct assignment with paired suppressions: the constant is
    # Final[int] in src (documentation of intent, not immutability), so
    # mypy --strict wants setattr (mypy misc), while ruff B010 wants
    # plain assignment — this line intentionally rebinds a Final
    # test-double, which is the one shape both tools flag by design.
    original = manager_mod.VECTOR_LEG_OVERFETCH_FACTOR
    manager_mod.VECTOR_LEG_OVERFETCH_FACTOR = factor  # type: ignore[misc, unused-ignore]
    try:
        yield
    finally:
        manager_mod.VECTOR_LEG_OVERFETCH_FACTOR = original  # type: ignore[misc, unused-ignore]


# ── metric primitives ────────────────────────────────────────────────────────


@dataclass
class QueryMeasurement:
    """Per-query raw outcome (slug-space, corpus-order stable)."""

    qid: str
    text: str
    project: str | None
    expected: frozenset[str]
    result_slugs: list[str] = field(default_factory=list)
    search_types: list[str] = field(default_factory=list)
    # Per-row ``project_scope_fallback`` (ADR-0029), parallel to
    # ``result_slugs`` — appended in lockstep, hence ``strict=True`` zips.
    result_fallback: list[bool] = field(default_factory=list)


@dataclass
class SearchMetrics:
    """Aggregated retrieval metrics + safety counters for one variant."""

    label: str
    precision: dict[int, float] = field(default_factory=dict)
    recall: dict[int, float] = field(default_factory=dict)
    judged_queries: int = 0
    probe_queries: int = 0
    non_admissible_surfaced: int = 0
    # UNTAGGED out-of-project rows only — the resolve-guard invariant
    # (must stay 0). ADR-0029 soft-fallback rows carry
    # ``project_scope_fallback=True`` and are by-design cross-project
    # retrievals; they are counted separately right below so the A0-review
    # telemetry keeps its visibility.
    foreign_project_surfaced: int = 0
    foreign_project_fallback_surfaced: int = 0
    # ADR-0029 belts: result sets that came from the fallback retry (any
    # tagged row), the manager-side ``project_scope_fallback_total``
    # counter delta over the same window, and result sets whose tagging
    # is not all-or-nothing (the retry tags EVERY row it surfaces).
    scope_fallback_queries: int = 0
    scope_fallback_events: int = 0
    scope_fallback_tagging_violations: int = 0
    hybrid_queries: int = 0
    planted_appearances: int = 0
    planted_leaks: int = 0


def _measure_queries(
    mgr: MemoryManager,
    slug_to_id: dict[str, str],
    *,
    limit: int = 10,
) -> list[QueryMeasurement]:
    id_to_slug = {v: k for k, v in slug_to_id.items()}
    out: list[QueryMeasurement] = []
    for q in GOLDEN_QUERIES:  # fixed query order — determinism
        results = mgr.search(q.text, project=q.project, limit=limit)
        m = QueryMeasurement(qid=q.qid, text=q.text, project=q.project, expected=q.expected)
        for r in results:
            slug = id_to_slug.get(r.memory.id)
            if slug is not None:
                m.result_slugs.append(slug)
                m.search_types.append(r.search_type)
                m.result_fallback.append(r.project_scope_fallback)
        out.append(m)
    return out


def _issue_and_check_injection(
    mgr: MemoryManager, slug_to_id: dict[str, str], m: QueryMeasurement
) -> tuple[int, int]:
    """Issue every planted-secret hit through the real scan channel.

    Mirrors the ``mnemos_search`` / REST ``/search`` issuance semantics
    (``scan_issuance_item``: content + title scanned, redact-or-refuse).
    A leak is a planted literal present in the issued content or title.
    """
    del slug_to_id  # m.result_slugs already maps through ids
    leaks = 0
    appearances = 0
    for slug in m.result_slugs:
        entry = _SLUG_ENTRY[slug]
        if not entry.planted:
            continue
        appearances += 1
        scan = mgr.scan_issuance_item(
            entry.content,
            title=entry.title,
            context=f"golden:injection:{m.qid}",
        )
        issued = (scan.content or "") + "\n" + (scan.title or "")
        for key in entry.planted:
            if PLANTED_SECRETS[key] in issued:
                leaks += 1
    return appearances, leaks


def measure_search(
    mgr: MemoryManager,
    slug_to_id: dict[str, str],
    *,
    label: str,
) -> SearchMetrics:
    """Run the golden queries and aggregate one variant's metrics.

    ADR-0029 split (#315): ``foreign_project_surfaced`` counts UNTAGGED
    out-of-project rows (the resolve-guard invariant); fallback-tagged
    rows land in ``foreign_project_fallback_surfaced``, with the
    ``scope_fallback_*`` belts proving every surfaced fallback row is
    tagged (all-or-nothing per result set, counter parity).
    """
    from benchmarks.corpus.corpus import NON_ADMISSIBLE_SLUGS

    fallback_events_before = int(mgr.search_stats()["project_scope_fallback_total"])
    measurements = _measure_queries(mgr, slug_to_id)
    metrics = SearchMetrics(label=label)
    prec_sums: dict[int, float] = {k: 0.0 for k in K_VALUES}
    rec_sums: dict[int, float] = {k: 0.0 for k in K_VALUES}

    for m in measurements:
        query = next(q for q in GOLDEN_QUERIES if q.qid == m.qid)
        if query.expect_no_results:
            metrics.probe_queries += 1
        else:
            metrics.judged_queries += 1
        metrics.non_admissible_surfaced += sum(
            1 for s in m.result_slugs if s in NON_ADMISSIBLE_SLUGS
        )
        tagged_rows = sum(1 for fb in m.result_fallback if fb)
        if tagged_rows:
            metrics.scope_fallback_queries += 1
            if tagged_rows != len(m.result_fallback):
                # ADR-0029: the fallback retry tags EVERY row it surfaces
                # (manager.search) — a mixed set is a tagging bug, not a
                # scope decision.
                metrics.scope_fallback_tagging_violations += 1
        if m.project is not None:
            foreign_untagged: set[str] = set()
            foreign_fallback: set[str] = set()
            for slug, fb in zip(m.result_slugs, m.result_fallback, strict=True):
                slug_project = _project_of(slug)
                if slug_project is None or slug_project == m.project:
                    continue
                (foreign_fallback if fb else foreign_untagged).add(slug)
            metrics.foreign_project_surfaced += len(foreign_untagged)
            metrics.foreign_project_fallback_surfaced += len(foreign_fallback)
        if any(st == "hybrid" for st in m.search_types):
            metrics.hybrid_queries += 1
        appearances, leaks = _issue_and_check_injection(mgr, slug_to_id, m)
        metrics.planted_appearances += appearances
        metrics.planted_leaks += leaks
        if not m.expected:
            continue
        for k in K_VALUES:
            topk = set(m.result_slugs[:k])
            hits = len(topk & m.expected)
            prec_sums[k] += hits / k  # strict denominator (documented)
            rec_sums[k] += hits / len(m.expected)

    for k in K_VALUES:
        metrics.precision[k] = prec_sums[k] / metrics.judged_queries
        metrics.recall[k] = rec_sums[k] / metrics.judged_queries
    metrics.scope_fallback_events = (
        int(mgr.search_stats()["project_scope_fallback_total"]) - fallback_events_before
    )
    return metrics


# ── ADR-0018 rewrite pair ────────────────────────────────────────────────────


@dataclass
class RewriteMetrics:
    """replace-hit-rate / replace-regret-rate over the scripted scenario."""

    rewrite_events: int = 0
    follow_up_retrieves: int = 0
    hits: int = 0
    whole_redemptions: int = 0
    controls: int = 0
    control_hits: int = 0
    planted_redemption_leaks: int = 0

    @property
    def hit_rate(self) -> float:
        return self.hits / self.follow_up_retrieves if self.follow_up_retrieves else 0.0

    @property
    def regret_rate(self) -> float:
        return self.whole_redemptions / self.rewrite_events if self.rewrite_events else 0.0


def measure_rewrite(mgr: MemoryManager) -> RewriteMetrics:
    """Execute the scripted rewrite scenario against a live manager.

    Every event runs through the real ``context_rewrite`` path
    (idempotency, pipeline storage, supersedes edges, marker minting);
    every follow-up runs through the real ``retrieve_content`` channel
    (snippet mode for detail needs, full mode for whole needs).
    """
    from mnemos.context_rewrite import context_rewrite

    from benchmarks.corpus.corpus import PLANTED_SECRETS as SECRETS

    metrics = RewriteMetrics()
    hash_by_slug: dict[str, str] = {}
    memory_id_by_slug: dict[str, str] = {}

    # Phase 1 — N rewrite events, in scenario order, with real markers.
    for spec in REWRITE_EVENTS:
        # ``supersedes`` takes the MEMORY id of the replaced block's
        # original (the previous event's stored memory), not its hash.
        supersedes_id = memory_id_by_slug.get(spec.supersedes) if spec.supersedes else None
        receipt = context_rewrite(
            mgr,
            content=_block(spec),
            project=spec.project,
            agent=spec.agent,
            session=spec.session,
            supersedes=supersedes_id,
            include_marker=True,
        )
        hash_by_slug[spec.slug] = str(receipt.get("ccr_marker", {}).get("hash", ""))
        memory_id_by_slug[spec.slug] = str(receipt.get("memory_id", ""))
        metrics.rewrite_events += 1
        # Fixture-integrity guard (fail loud, not a soft miss): every
        # scenario block must actually enter the CCR cache — a block that
        # dipped below ``ccr.min_size_chars`` would mint no marker and
        # every follow-up on it would silently count as a mechanism miss.
        marker = receipt.get("ccr_marker", {})
        assert marker.get("cached") is True and marker.get("hash"), (
            f"rewrite fixture broken: {spec.slug} block was not CCR-cached "
            f"(len={len(_block(spec))} vs ccr.min_size_chars="
            f"{mgr.settings.ccr.min_size_chars}) — lengthen the block, do "
            "not lower the threshold"
        )

    # Phase 2 — M follow-up retrieves (detail → snippet mode, whole → full).
    for spec in REWRITE_EVENTS:
        if spec.later_need == "none":
            continue
        h = hash_by_slug[spec.slug]
        if not h:
            metrics.follow_up_retrieves += 1  # no marker minted = guaranteed miss
            continue
        if spec.later_need == "detail":
            metrics.follow_up_retrieves += 1
            res = mgr.retrieve_content(
                h, query=spec.detail_query, snippet_count=2, project=spec.project
            )
            snippets = res.get("snippets") or []
            if res.get("found") and not res.get("refused") and snippets:
                metrics.hits += 1
        else:  # whole
            metrics.follow_up_retrieves += 1
            res = mgr.retrieve_content(h, project=spec.project)
            if res.get("found") and not res.get("refused") and res.get("original"):
                metrics.hits += 1
                metrics.whole_redemptions += 1
                if spec.planted is not None and SECRETS[spec.planted] in str(
                    res.get("original", "")
                ):
                    metrics.planted_redemption_leaks += 1

    # Phase 3 — control channel (never-rewritten CCR entries).
    for ctl in control_blocks():
        comp = mgr.compress_content(
            ctl.text, project=ctl.project, agent="golden-control", session="ctl"
        )
        ctl_hash = str(comp.get("hash", ""))
        metrics.controls += 1
        if not ctl_hash:
            continue
        res = mgr.retrieve_content(ctl_hash, query=ctl.query, snippet_count=2, project=ctl.project)
        snippets = res.get("snippets") or []
        if res.get("found") and not res.get("refused") and snippets:
            metrics.control_hits += 1

    return metrics


# ── assemble-context injection cross-check ───────────────────────────────────


def assemble_leak_check(mgr: MemoryManager, slug_to_id: dict[str, str]) -> dict[str, Any]:
    """End-to-end D1 injection-path check over every planted entry.

    For each planted-secret entry, run ``assemble_context`` (explicit
    query mode, generous budget) with a probe query that makes the entry
    rank, then scan the assembled text for the planted literal. Also
    verifies each planted entry actually entered the assembled block —
    otherwise the check would silently pass by never surfacing it.
    """
    from mnemos.assemble import assemble_context

    leaks: list[str] = []
    surfaced: set[str] = set()
    per_probe: list[dict[str, Any]] = []
    for slug, probe in _PLANTED_PROBES.items():
        entry = _SLUG_ENTRY[slug]
        block = assemble_context(
            mgr,
            session="golden-injection-check",
            project=entry.project,
            query=probe,
            budget=4000,
        )
        block_ids = {b.get("memory_id") for b in block.get("blocks", [])}
        surfaced_flag = slug_to_id[slug] in block_ids
        if surfaced_flag:
            surfaced.add(slug)
        for key in entry.planted:
            if PLANTED_SECRETS[key] in block["text"]:
                leaks.append(f"{slug}:{key}")
        per_probe.append(
            {
                "slug": slug,
                "probe": probe,
                "blocks": len(block["blocks"]),
                "candidates": block["stats"]["recall"]["candidates"],
                "surfaced": surfaced_flag,
            }
        )
    return {
        "leaks": leaks,
        "probes": per_probe,
        "all_planted_surfaced": surfaced == set(_PLANTED_PROBES),
    }


# Per-planted-entry probe queries — each ranks its entry in the project's
# top-10 recall so the assembled block would carry it if the scan stage
# failed to redact.
_PLANTED_PROBES: dict[str, str] = {
    "aurora-db-conn-leak": "staging",
    "aurora-ci-token-note": "release workflow",
    "aurora-jwt-session-log": "bearer",
    "vaultui-slack-webhook-note": "critique bot",
    "vaultui-embed-key-note": "visual regression",
    "mnemos-aws-log": "backup",
    "mnemos-pem-note": "federation TLS",
    "atlas-secret-spill-log": "dsn",
}
