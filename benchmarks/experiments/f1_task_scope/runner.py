"""F1 task-scope runner — arms A0/C/B/A over the f1-mixed corpus (F1 §1.3).

Usage (from the repository root):

    python benchmarks/experiments/f1_task_scope/runner.py             # collect-only
    python benchmarks/experiments/f1_task_scope/runner.py --record    # write the run

THE DEFAULT INVOCATION REFUSES TO RECORD RESULTS (the E3-runner canon,
F1 §4.3). Collect-only executes every arm over the real corpus, prints
the run manifest and the paired outcome summary, and persists NOTHING:
the first recorded F1 run is a deliberate human decision taken AFTER
this infrastructure merges and after 2026-09-27 (the owner-gated
window). ``--record`` is the explicit opt-in; recorded runs are
WRITE-ONCE (content-addressed run id over the deterministic core:
corpus fingerprint + analyzed-qid ledger state + code version + arm
flag block + budget; a second ``--record`` of the same id fails loud)
and append the §9 run-ledger entry to the E-file as part of the same
deliberate step.

Arms (F1 §1.3, registered exactly as implemented):

* **A0** — reference / baseline, context-free retrieval: plain
  ``assemble_context`` for every query — no task knowledge, no lens.
* **C** — honest tag-filter (the pre-Ф0 client-side pattern):
  ``mgr.search(query, project, limit=RECALL_DEPTH, tags=["task:<slug>"])``
  on task-class queries, top-5 formatted through the standard block
  formatter at the SAME budget; plain assembly on cross-class (filter
  dropped — the honest switch).
* **B** — emulated task context without the primitives: the task's
  ``TASK_DISPLAY`` label prefixed to the query text on task-class
  queries; plain assembly on cross-class (prefix dropped).
* **A** — the Ф0 treatment: ``task=<slug> + lens=CODE`` on task-class
  queries; ``lens=CODE`` only on cross-class (task dropped, lens
  retained — the registered switching policy).

Common block, every arm (F1 §1.3): ``lanes_enabled=false`` (the E3
verdict stands), ``type_boost=true`` (the E3 survivor, applied
identically in all arms — controlled), ``expand_ccr=false``,
``mode=sync``, ``budget=2048`` tokens (§4.1 equal-budget), top-5
blocks, deterministic embedder, scanner off, isolated byte-identical
store copy per arm (one seeded corpus build cloned through the SQLite
backup API — the S4/S5 precedent), frozen clock (retrieval stamps
pre-seeded at ``RUN_NOW``; wall-clock never enters metrics), fixed arm
order A0 → C → B → A.

What is measured and persisted (F1 §2/§4.3 — NOTHING else):

* per-query outcome tuples per arm: binary hit (gold id in the issued
  top-5), assembled token count, issued block slugs, foreign-task
  leakage slugs, lens activation + the G4a pre/post-lens gold probes;
* every L-neg outcome row carries the committed ``substratum`` flag
  ("trap" | "mixed", §8 entry 15): the G4b corridor's trap-family
  scope and the mixed-phrasing activation-cost descriptive measure are
  read from the artifact data, never re-derived by the analysis wave;
* discordance tallies for the registered comparisons (T-gold: A/A0,
  A/C, A/B, C/B; X-gold and L-neg: A/A0);
* the recorded artifact additionally carries the linkage key
  ``run_id`` (stamped by ``--record``, schema-legal exactly once and
  equal to the paired manifest's id — the on-disk file re-verifies
  under the same exact-key contract, #382);
* NO statistics at run time: no p-values, no CIs, no verdicts, no
  rates — single-look analysis runs once, outside this runner, after
  the recorded run (§6.6). The ban is schema (exact key allowlists +
  a recursive stat-key scan), not convention.

Run invariants (F1 §2.8/§4.5, void-on-breach): V1 prefix byte-stability
probe (arm A cross-class vs A0, lens-inactive queries byte-identical;
lens-active queries order-preserving subsets — V3's metric shadow), V2
lanes-off, V4 canary scan at issuance (any canary content in any
issued block of any arm = VOID), V5 tag-contract verification at load,
V6 manifest completeness + write-once. All logged in the manifest; any
breach fails the collect loud.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import itertools
import json
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid as uuid_mod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.corpus.deterministic_embedder import LexicalHashEmbedder  # noqa: E402
from benchmarks.experiments.f1_task_scope import corpus as f1_corpus  # noqa: E402
from benchmarks.experiments.f1_task_scope import ledger as f1_ledger  # noqa: E402
from benchmarks.experiments.f1_task_scope.corpus import (  # noqa: E402
    Corpus,
    CorpusRow,
    GoldQuery,
)
from benchmarks.stands.s4_availability.store_copy import clone_store  # noqa: E402

# `mnemos_version` spelling kept for manifest compatibility with the E3
# runners (recorded verbatim in run manifests).
from vesmaro import __version__ as mnemos_version  # noqa: E402
from vesmaro.assemble import (  # noqa: E402
    RECALL_DEPTH,
    assemble_context,
    build_provenance,
    estimate_tokens,
)
from vesmaro.config import Settings  # noqa: E402
from vesmaro.filter.pipeline import apply_filter  # noqa: E402
from vesmaro.lens import Lens  # noqa: E402
from vesmaro.manager import MemoryManager  # noqa: E402
from vesmaro.models import MemoryCreate, MemorySource, MemoryStatus  # noqa: E402

RUNNER_VERSION = "f1-task-scope-runner-3"
EXPERIMENT = "f1-task-scope"
SPEC = "docs/experiments/f1-task-scope.md §1.3, §2.8, §3, §4, §6.5, §9"

#: F1 §1.3/§4.1 — the equal assembled-context token budget.
F1_TOKEN_BUDGET: int = 2048
#: F1 §6.1 — primary k, frozen.
TOP_K: int = 5

#: The frozen scenario clock (§4.2): every retrieval stamp is pre-seeded
#: with this instant; wall-clock quantities never enter metrics.
RUN_NOW = datetime(2026, 9, 20, 12, 0, 0, tzinfo=UTC)

#: One session id across all arms — REQUIRED by the V1 byte-identity
#: probe (provenance carries the session-scoped ``retrieved=<iso>``
#: stamp; identical session + identical frozen stamp ⇒ byte-comparable).
SESSION = "f1-task-scope"

#: F1 §1.3 — the fixed arm order.
ARM_ORDER: tuple[str, ...] = ("A0", "C", "B", "A")

RUNS_DIR = Path(__file__).resolve().parent / "runs"

#: The E-file whose §9 ledger `--record` appends to (never edited by
#: collect-only runs).
DOC_PATH = ROOT / "docs" / "experiments" / "f1-task-scope.md"


# ── arms (F1 §1.3, registered exactly as implemented) ─────────────────────────


@dataclass(frozen=True)
class ArmDefinition:
    """One arm = one mechanism over the same (query, class, task) triple."""

    arm: str
    mechanism: str

    def config_dict(self) -> dict[str, Any]:
        cfg: dict[str, Any] = {
            "mechanism": self.mechanism,
            "task_class_call": self.task_class_call,
            "cross_class_call": self.cross_class_call,
        }
        return cfg

    @property
    def task_class_call(self) -> str:
        if self.arm == "A0":
            return "assemble_context(query=q)"
        if self.arm == "C":
            return 'mgr.search(query=q, tags=["task:<slug>"]) + standard block formatter'
        if self.arm == "B":
            return 'assemble_context(query=f"{TASK_DISPLAY[slug]}: {q}")'
        return "assemble_context(query=q, task=<slug>, lens=CODE)"

    @property
    def cross_class_call(self) -> str:
        if self.arm == "A":
            return "assemble_context(query=q, lens=CODE) — task dropped, lens retained"
        return "assemble_context(query=q) — the honest switch (filter/prefix dropped)"


ARMS: tuple[ArmDefinition, ...] = (
    ArmDefinition(arm="A0", mechanism="context-free retrieval (baseline)"),
    ArmDefinition(arm="C", mechanism="honest tag-filter (pre-Ф0 client-side pattern)"),
    ArmDefinition(arm="B", mechanism="emulated task context (query-prefix, zero Ф0)"),
    ArmDefinition(arm="A", mechanism="task-scoped composition (Ф0 flags on)"),
)

#: The common block every arm runs under (F1 §1.3).
COMMON_BLOCK: dict[str, Any] = {
    "lanes_enabled": False,
    "type_boost": True,
    "expand_ccr": False,
    "mode": "sync",
    "budget": F1_TOKEN_BUDGET,
    "top_k": TOP_K,
    "embedder": "deterministic LexicalHashEmbedder",
    "scanner": False,
    "store": "single seeded build, byte-identical clone per arm (SQLite backup API)",
    "clock": "frozen (retrieval stamps pre-seeded at RUN_NOW)",
    "arm_order": list(ARM_ORDER),
}


# ── harness-only deterministic memory ids (the tests/_seeded_ids technique) ───


_ID_NAMESPACE = uuid_mod.UUID("f11e5c0a-7d34-4f1a-9c2b-6a8d4e2f1b90")


@contextlib.contextmanager
def seeded_memory_ids(tag: str) -> Iterator[None]:
    """Deterministic uuid draw for the corpus build (harness property).

    Same technique and rationale as ``tests/_seeded_ids`` (#280): the
    corpus build is COMMITTED content, so its store — ids included —
    must reproduce byte-identically on every collect; the seeded
    uuid5 counter fixes the id draw (hence the deterministic search
    tiebreak order) without touching the production uuid4 path. Never
    used outside this benchmark harness.
    """
    counter = itertools.count()

    def _fixed_uuid4() -> uuid_mod.UUID:
        return uuid_mod.uuid5(_ID_NAMESPACE, f"{tag}:{next(counter)}")

    with mock.patch.object(uuid_mod, "uuid4", _fixed_uuid4):
        yield


# ── store construction (one build, byte-identical clones) ─────────────────────


def _store_settings(root: Path) -> Settings:
    """Settings for a fresh isolated store (the strata-tests shape)."""
    settings = Settings(
        vesma={
            "vault_path": str(root / "vault"),
            "data_dir": str(root / "data"),
            "db_name": "f1.db",
        },
        scanner={"enabled": False},
    )
    settings.resolve_paths()
    return settings


def build_store(root: Path, corpus: Corpus) -> tuple[dict[str, str], dict[str, Any]]:
    """Ingest the corpus ONCE into a fresh manager (fixed order, seeded ids).

    Returns ``(slug_to_id, verification)``. V5 loader-side verification:
    every row's tag set carries at most one ``task:`` tag matching the
    contract regex — the generator pins it, the loader re-verifies it.
    Canary semantics (§3.5): the 8 planted secret-shaped rows are
    EXPECTED to fail the publication gate (refused ⇒ RAW ⇒ never
    recalled — inert by the quarantine semantics); every other row
    must ingest PUBLISHED exactly as authored (seed hygiene).
    """
    settings = _store_settings(root)
    mgr = MemoryManager(settings)
    mgr._embedder = LexicalHashEmbedder()
    slug_to_id: dict[str, str] = {}
    v5_violations = 0
    canaries_inert = 0
    try:
        for row in corpus.rows:
            # V5 (loader side): generator-pinned, loader-verified.
            task_tags = [t for t in row.tags if t.startswith("task:")]
            if len(task_tags) > 1 or any(not f1_corpus.TASK_TAG_RE.match(t) for t in task_tags):
                v5_violations += 1
            data = MemoryCreate(
                content=row.content,
                title=row.title,
                tags=list(row.tags),
                source=MemorySource.MANUAL,
                status=MemoryStatus.PUBLISHED,
                metadata=dict(row.metadata),
            )
            memory = mgr.add(data, project=row.project, agent=f1_corpus.AGENT)
            if row.planted_secret:
                if memory.status == MemoryStatus.PUBLISHED:
                    raise AssertionError(
                        f"canary {row.slug!r} ingested PUBLISHED — the secret-shaped "
                        "canaries must stay inert (RAW) per the quarantine semantics"
                    )
                canaries_inert += 1
            elif memory.status != MemoryStatus.PUBLISHED:
                raise AssertionError(
                    f"stratum seed {row.slug!r} was demoted to {memory.status} at "
                    "ingest — fix the seed content, not this loader"
                )
            slug_to_id[row.slug] = memory.id
    finally:
        mgr.close()
    return slug_to_id, {
        "rows_verified": len(corpus.rows),
        "v5_violations": v5_violations,
        "canaries_inert": canaries_inert,
    }


def _store_content_digest(settings: Settings) -> str:
    """Content digest of a store's memories (ordered id+content rows)."""
    conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True)
    try:
        digest = hashlib.sha256()
        for row_id, content in conn.execute("SELECT id, content FROM memories ORDER BY id"):
            digest.update(row_id.encode())
            digest.update(b"\x00")
            digest.update(content.encode())
        return digest.hexdigest()
    finally:
        conn.close()


def _arm_settings(root: Path, arm: str) -> Settings:
    return _store_settings(root / f"arm-{arm}")


def _open_arm_manager(settings: Settings) -> MemoryManager:
    """Open one arm's clone with the F1 common block applied + frozen clock."""
    mgr = MemoryManager(settings)
    mgr._embedder = LexicalHashEmbedder()
    mgr.settings.lanes.enabled = False  # V2 (the E3 verdict stands)
    mgr.settings.lanes.type_boost = True  # §1.3 common block
    if (mgr.settings.lanes.enabled, mgr.settings.lanes.type_boost) != (False, True):
        raise AssertionError("arm manager common block drifted (lanes/type_boost)")
    # Frozen clock (§4.2): pre-seed the session's retrieval stamp so the
    # provenance bytes are identical across arms and across collects.
    mgr._retrieval_iso[SESSION] = RUN_NOW.isoformat()
    if mgr.retrieval_iso(SESSION) != RUN_NOW.isoformat():
        raise AssertionError("frozen retrieval stamp did not take")
    return mgr


# ── arm C: the standard block formatter (§4.1) ────────────────────────────────


def format_search_blocks(
    mgr: MemoryManager,
    results: list[Any],
    *,
    project: str,
    budget: int,
    retrieved_iso: str,
) -> list[dict[str, Any]]:
    """Format search-level results through the standard block formatter.

    The same per-block stages the assembled pipeline runs — context
    filter, issuance scan, CacheAligner, provenance wrap, greedy
    whole-block budget inclusion — applied to ``mgr.search`` output so
    arm C's top-5 surface is comparable byte-for-byte with the
    assembled arms under the SAME budget (§4.1: "comparability of the
    top-5 surface is a runner requirement registered here").
    """
    included: list[dict[str, Any]] = []
    remaining = budget
    for result in results:
        memory = result.memory
        content = memory.effective_content()
        filtered = apply_filter(content, profile=None, budget=None)
        content = str(filtered["clean_content"])
        scan = mgr.scan_issuance(content, context=f"f1-arm-c:{memory.id}")
        if scan.refused:
            continue  # the assembled scan stage drops refused blocks too
        content = scan.text
        aligned = mgr.align_prefix(content, profile=str(filtered["profile"]))
        content = str(aligned["aligned_text"])
        provenance = build_provenance(memory, retrieved_iso, project=project)
        block_text = f"{provenance}\n{content}"
        tokens = estimate_tokens(block_text)
        if tokens > remaining:
            continue
        remaining -= tokens
        included.append(
            {
                "memory_id": memory.id,
                "content": content,
                "provenance": provenance,
                "tokens": tokens,
                "score": result.score,
                "search_type": result.search_type,
            }
        )
    return included


# ── arm execution ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class QueryOutcome:
    """The per-(query, arm) outcome tuple (F1 §4.3 — tuples, nothing else).

    Token bases (the arm-C dual basis, §8/14): ``tokens`` keeps the
    REGISTERED basis — arm C: the top-5 formatted blocks; A0/B/A: the
    full assembled estimate — while ``tokens_full`` additionally records
    arm C's FULL formatted-assembly estimate (every budget-included
    block, not just the top-5), the basis directly comparable with the
    assembled arms' ``tokens``. On A0/B/A the two fields are equal (the
    assembled estimate already IS the full basis).
    """

    qid: str
    arm: str
    hit: bool
    tokens: int
    tokens_full: int
    text_sha256: str
    block_slugs: tuple[str, ...]
    foreign_leak_slugs: tuple[str, ...]
    lens_active: bool | None
    pre_lens_gold_top5: bool | None
    post_lens_gold_top5: bool | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "hit": self.hit,
            "tokens": self.tokens,
            "tokens_full": self.tokens_full,
            "block_slugs": list(self.block_slugs),
            "foreign_leak_slugs": list(self.foreign_leak_slugs),
            "lens_active": self.lens_active,
            "pre_lens_gold_top5": self.pre_lens_gold_top5,
            "post_lens_gold_top5": self.post_lens_gold_top5,
            "text_sha256": self.text_sha256,
        }


def _leak_slugs(
    block_ids: list[str], id_to_row: dict[str, CorpusRow], current_task: str
) -> tuple[str, ...]:
    """Issued blocks carrying a task: tag ≠ the current task (G2 §2.5).

    Task-less shared rows are NOT leakage (registered definition).
    """
    leaks = []
    for mid in block_ids:
        row = id_to_row.get(mid)
        if row is not None and row.task is not None and row.task != current_task:
            leaks.append(row.slug)
    return tuple(leaks)


def _content_type_of_result(result: Any) -> str:
    stored = result.memory.metadata.get("content_type")
    return stored if isinstance(stored, str) and stored in ("code", "prose") else "prose"


def execute_arm(
    arm: str,
    corpus: Corpus,
    queries: tuple[GoldQuery, ...],
    slug_to_id: dict[str, str],
    settings: Settings,
) -> tuple[dict[str, QueryOutcome], dict[str, Any]]:
    """Execute one arm over every query under the F1 common block.

    Returns ``(outcomes, arm_report)`` — the report carries the V3
    candidate-level probe (arm A only; see ``evaluate_invariants``).
    """
    id_to_row = {slug_to_id[row.slug]: row for row in corpus.rows}
    mgr = _open_arm_manager(settings)
    canary_hits: list[str] = []
    sentinels = tuple(s.lower() for s in f1_corpus.CANARY_SENTINELS)
    v3_probes = v3_subset_ok = v3_code_ok = 0
    v3_failures: list[str] = []
    outcomes: dict[str, QueryOutcome] = {}
    try:
        for q in queries:
            gold_id = slug_to_id[q.gold_slug]
            lens_active: bool | None = None
            pre_gold: bool | None = None
            post_gold: bool | None = None

            if arm == "C" and q.query_class == "task":
                results = mgr.search(
                    query=q.text,
                    project=q.project,
                    limit=RECALL_DEPTH,
                    tags=[f"task:{q.current_task}"],
                )
                blocks = format_search_blocks(
                    mgr,
                    results,
                    project=q.project,
                    budget=F1_TOKEN_BUDGET,
                    retrieved_iso=RUN_NOW.isoformat(),
                )
                top_ids = [b["memory_id"] for b in blocks[:TOP_K]]
                tokens = sum(b["tokens"] for b in blocks[:TOP_K])
                # Dual basis (§8/14): the full formatted assembly — every
                # budget-included block, comparable with the assembled
                # arms' full estimates.
                tokens_full = sum(b["tokens"] for b in blocks)
                text = "\n\n".join(f"{b['provenance']}\n{b['content']}" for b in blocks[:TOP_K])
                block_contents = [b["content"] for b in blocks[:TOP_K]]
            else:
                # A0 (every query); B/C cross-class (honest switch); A
                # cross-class (task dropped, lens retained) — the §1.3
                # switching policy, one call shape per (arm, class).
                query_text = q.text
                effective_task: str | None = None
                effective_lens: Lens | None = None
                if arm == "A":
                    effective_lens = Lens.CODE
                    if q.query_class == "task":
                        effective_task = q.current_task
                elif arm == "B" and q.query_class == "task":
                    query_text = f"{f1_corpus.TASK_DISPLAY[q.current_task]}: {q.text}"
                result = assemble_context(
                    mgr,
                    session=SESSION,
                    project=q.project,
                    query=query_text,
                    task=effective_task,
                    lens=effective_lens,
                    budget=F1_TOKEN_BUDGET,
                )
                if result["tokens"]["budget"] != F1_TOKEN_BUDGET:
                    raise AssertionError(
                        f"{q.qid}/{arm}: equal-budget breach — assembly ran at "
                        f"{result['tokens']['budget']} != {F1_TOKEN_BUDGET} (§4.1)"
                    )
                issued = result["blocks"][:TOP_K]
                top_ids = [b["memory_id"] for b in issued]
                tokens = int(result["tokens"]["estimated"])
                tokens_full = tokens  # the assembled estimate already IS the full basis
                text = str(result["text"])
                block_contents = [str(b["content"]) for b in issued]
                if arm == "A":
                    lens_stats = result["stats"]["recall"].get("lens")
                    lens_active = bool(lens_stats["active"]) if lens_stats else False

            # G4a probes (arm A, task-class): the pre-lens recall order is
            # the tags-filtered search (exactly what _recall_stage issues);
            # the post-lens order is its code-only order-preserving
            # subsequence when the lens activated.
            if arm == "A" and q.query_class == "task":
                probe = mgr.search(
                    query=q.text,
                    project=q.project,
                    limit=RECALL_DEPTH,
                    tags=[f"task:{q.current_task}"],
                )
                pre_ids = [r.memory.id for r in probe[:TOP_K]]
                pre_gold = gold_id in pre_ids
                if lens_active:
                    post_ids = [r.memory.id for r in probe if _content_type_of_result(r) == "code"][
                        :TOP_K
                    ]
                    post_gold = gold_id in post_ids
                else:
                    post_gold = pre_gold  # identity projection keeps everything

            # V3 shadow probe (arm A, cross-class, lens ACTIVE): the lens
            # only narrows — arm A's ISSUED blocks must be an
            # order-preserving subset of the same query's PRE-LENS
            # candidate order (the unfiltered search = exactly what
            # _recall_stage issues for A0), and every issued row must be
            # code-typed (the lens admits only its target). The budget
            # stage's reflow makes issued-set comparisons against A0's
            # issued list illegitimate — candidates are the honest level.
            if arm == "A" and q.query_class == "cross" and lens_active:
                v3_probes += 1
                probe = mgr.search(query=q.text, project=q.project, limit=RECALL_DEPTH)
                probe_ids = [r.memory.id for r in probe]
                issued_ids = list(top_ids)
                positions = [probe_ids.index(mid) for mid in issued_ids if mid in probe_ids]
                subset_ok = len(positions) == len(issued_ids) and positions == sorted(positions)
                code_ok = all(
                    _content_type_of_result(r) == "code"
                    for r in probe
                    if r.memory.id in set(issued_ids)
                )
                if subset_ok:
                    v3_subset_ok += 1
                if code_ok:
                    v3_code_ok += 1
                if not (subset_ok and code_ok):
                    v3_failures.append(q.qid)

            # V4: repeat secret scan at issuance (any canary trace = void).
            for content in block_contents:
                low = content.lower()
                for sentinel in sentinels:
                    if sentinel in low:
                        canary_hits.append(f"{q.qid}/{arm}")

            outcomes[q.qid] = QueryOutcome(
                qid=q.qid,
                arm=arm,
                hit=gold_id in top_ids,
                tokens=tokens,
                tokens_full=tokens_full,
                text_sha256=hashlib.sha256(text.encode()).hexdigest(),
                block_slugs=tuple(id_to_row[mid].slug for mid in top_ids if mid in id_to_row),
                foreign_leak_slugs=_leak_slugs(top_ids, id_to_row, q.current_task),
                lens_active=lens_active,
                pre_lens_gold_top5=pre_gold,
                post_lens_gold_top5=post_gold,
            )
    finally:
        mgr.close()
    if canary_hits:
        raise AssertionError(
            f"V4 BREACH — canary content in issued blocks of arm {arm}: "
            f"{canary_hits[:5]} ({len(canary_hits)} total) — run VOID (§2.8)"
        )
    report: dict[str, Any] = {
        "v3": {
            "probes": v3_probes,
            "subset_ok": v3_subset_ok,
            "code_only_ok": v3_code_ok,
            "failures": v3_failures,
        }
    }
    return outcomes, report


# ── invariants (F1 §2.8/§4.5 — any breach = void run) ──────────────────────────


def evaluate_invariants(
    per_arm: dict[str, dict[str, QueryOutcome]],
    arm_reports: dict[str, dict[str, Any]],
    queries: tuple[GoldQuery, ...],
    build_verification: dict[str, Any],
    store_digest: str,
    digest_equal: bool,
) -> dict[str, Any]:
    """V1-V6 over the executed arms; raises AssertionError on any breach."""
    by_qid = {q.qid: q for q in queries}
    a0, a = per_arm["A0"], per_arm["A"]

    # V1 — prefix byte-stability on the fixed probe set (every cross-class
    # lens-INACTIVE query: arm A's identity projection must be byte-identical
    # to A0 — the frozen session stamp makes the texts comparable).
    v1_probes = v1_ok = 0
    v1_failures: list[str] = []
    for qid, a_out in a.items():
        q = by_qid[qid]
        if q.query_class != "cross" or a_out.lens_active:
            continue
        v1_probes += 1
        if a_out.text_sha256 == a0[qid].text_sha256:
            v1_ok += 1
        else:
            v1_failures.append(qid)
    if v1_failures:
        raise AssertionError(
            f"V1 BREACH — {len(v1_failures)}/{v1_probes} lens-inactive cross queries "
            f"changed bytes between A0 and A: {v1_failures[:5]} — run VOID (§2.8)"
        )

    # V3's metric shadow — candidate-level (the arm-A report of
    # execute_arm): an ACTIVE lens only narrows.
    v3 = arm_reports["A"]["v3"]
    if v3["failures"]:
        raise AssertionError(
            f"V3 shadow BREACH — {len(v3['failures'])}/{v3['probes']} lens-active "
            f"cross queries violate order-preserving code-only narrowing: "
            f"{v3['failures'][:5]} — run VOID (§2.8)"
        )

    issued_blocks = sum(
        len(out.block_slugs) for arm_outcomes in per_arm.values() for out in arm_outcomes.values()
    )
    v5 = {
        "rows_verified": build_verification["rows_verified"],
        "violations": build_verification["v5_violations"],
        "canaries_inert": build_verification["canaries_inert"],
    }
    if v5["violations"]:
        raise AssertionError(f"V5 BREACH — tag contract violations at load: {v5}")
    if v5["canaries_inert"] != f1_corpus.CORPUS_COUNTS["canaries"]:
        raise AssertionError(f"V4 precondition — inert canaries {v5['canaries_inert']} != 8")
    if not digest_equal:
        raise AssertionError("store-copy breach — arm clones are not content-identical")

    return {
        "V1": {
            "probe": "arm A (cross, lens-inactive) vs A0: assembled text bytes identical",
            "probes": v1_probes,
            "identical": v1_ok,
            "failures": v1_failures,
        },
        "V3_shadow": {
            "probe": (
                "arm A (cross, lens-active): issued blocks are an "
                "order-preserving subset of the pre-lens candidate order "
                "AND every issued row is code-typed (the lens narrows only)"
            ),
            "probes": v3["probes"],
            "subset_ok": v3["subset_ok"],
            "code_only_ok": v3["code_only_ok"],
            "failures": v3["failures"],
        },
        "V2": {"lanes_enabled": False, "arms": list(ARM_ORDER)},
        "V4": {
            "canaries": f1_corpus.CORPUS_COUNTS["canaries"],
            "canaries_inert": v5["canaries_inert"],
            "issued_blocks_scanned": issued_blocks,
            "violations": 0,
        },
        "V5": v5,
        "V6": {
            "manifest_verified": True,
            "write_once": "record_run refuses an existing run id (fails loud)",
            "outcomes_reverified": "the written outcomes.json is re-verified on disk pre-rename",
            "statistics_ban": "no p-values/CIs/verdicts/rates in artifacts (schema-enforced)",
        },
        "store_copy": {
            "method": (
                "SQLite backup API (benchmarks.stands.s4_availability.store_copy.clone_store)"
            ),
            "content_digest_sha256": store_digest,
            "digests_equal_across_arms": digest_equal,
        },
    }


# ── outcomes (F1 §4.3 — tuples + discordance tallies, nothing else) ───────────


#: The exact top-level key set of an outcomes artifact (schema-enforced).
#: The linkage field ``run_id`` (stamped by ``record_run`` onto the
#: recorded copy, exactly-once and equal to the paired manifest's id) is
#: part of the schema, so a WRITTEN artifact re-verifies under the same
#: exact-key contract as the in-memory dict — "unexpected == missing ==
#: broken" holds for every recorded file too (#382).
_OUTCOME_TOP_KEYS: frozenset[str] = frozenset(
    {"spec", "pairing", "strata", "arm_order", "budget", "top_k", "queries", "discordance"}
)
_OUTCOME_LINKAGE_KEYS: frozenset[str] = frozenset({"run_id"})

#: The exact per-arm key set of a query tuple.
_ARM_TUPLE_KEYS: frozenset[str] = frozenset(
    {
        "hit",
        "tokens",
        "tokens_full",
        "block_slugs",
        "foreign_leak_slugs",
        "lens_active",
        "pre_lens_gold_top5",
        "post_lens_gold_top5",
        "text_sha256",
    }
)

#: The exact key set of a per-query outcome row. L-neg rows carry ONE
#: additional key — ``substratum`` ("trap" | "mixed") — the committed
#: evaluation-scope flag (§8 entry 15): the G4b corridor reads the trap
#: family, the mixed-phrasing activation-cost descriptive measure reads
#: the mixed family. Non-L-neg rows carry no substratum key (there is
#: no sub-split to name there).
_QUERY_ROW_KEYS: frozenset[str] = frozenset(
    {"qid", "stratum", "query_class", "task", "gold_slug", "axis", "arms"}
)

#: The legal substratum values (L-neg only).
_SUBSTRATA: frozenset[str] = frozenset({"trap", "mixed"})

#: Registered comparisons per stratum (§2.1-§2.4, §2.7b, §6.1).
_COMPARISONS: dict[str, tuple[tuple[str, str], ...]] = {
    "t_gold": (("A", "A0"), ("A", "C"), ("A", "B"), ("C", "B")),
    "x_gold": (("A", "A0"),),
    "l_neg": (("A", "A0"),),
}

#: Any key matching this pattern ANYWHERE in an artifact marks it as
#: carrying statistics — refused. The e3 spelling ``ci\d*`` (zero-or-
#: more digits) would false-fire on ordinary English keys containing
#: "ci" (the committed task slug ``q3-capacity-audit``); ``ci[\W_]?\d+``
#: keeps the ban's semantics (interval-shaped keys: ci95, ci_95) without
#: the prose false-positive ("capacity" is ci + letters, never matched).
#: The repair-wave extension additionally bans the bare statistic
#: spellings — a key that IS ``p`` (word-bounded, so ``top_k`` /
#: ``hybrid_alpha`` / ``step`` stay clean), ``pval``/``pvals``, any
#: ``p(value|values|-value|…)`` spelling, and ``power`` (right-bounded:
#: ``statistical_power`` fires, no artifact key contains it) — so a lone
#: ``"p": 0.03`` or ``"power": 0.82`` cannot ride along unnamed.
#: Registered as implemented (§8 entry 14).
_STAT_KEY_RE = re.compile(
    r"p.?values?|\bpvals?\b|power\b|\bp\b|ci[\W_]?\d+|verdict|signif|confiden"
)


def _stat_key_offenders(node: Any, path: str) -> list[str]:
    offenders: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if _STAT_KEY_RE.search(str(key)):
                offenders.append(here)
            offenders.extend(_stat_key_offenders(value, here))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            offenders.extend(_stat_key_offenders(value, f"{path}[{index}]"))
    return offenders


def build_outcomes(
    queries: tuple[GoldQuery, ...], per_arm: dict[str, dict[str, QueryOutcome]]
) -> dict[str, Any]:
    """Per-query outcome tuples per arm + discordance tallies ONLY."""
    rows = []
    for q in queries:
        row: dict[str, Any] = {
            "qid": q.qid,
            "stratum": q.stratum,
            "query_class": q.query_class,
            "task": q.current_task,
            "gold_slug": q.gold_slug,
            "axis": q.axis,
            "arms": {arm: per_arm[arm][q.qid].as_dict() for arm in ARM_ORDER},
        }
        if q.stratum == "l_neg":
            # The committed evaluation-scope flag (§8 entry 15): the
            # corridor/descriptive split rides IN the artifact, not in
            # analysis-wave prose.
            substratum = q.substratum
            assert substratum is not None  # l_neg by the branch above
            row["substratum"] = substratum
        rows.append(row)

    def _tally(stratum: str, first: str, second: str) -> dict[str, int]:
        qs = [q for q in queries if q.stratum == stratum]
        return {
            f"{first}_only": sum(
                1 for q in qs if per_arm[first][q.qid].hit and not per_arm[second][q.qid].hit
            ),
            f"{second}_only": sum(
                1 for q in qs if per_arm[second][q.qid].hit and not per_arm[first][q.qid].hit
            ),
        }

    discordance = {
        stratum: {
            f"{first}_vs_{second}": _tally(stratum, first, second) for first, second in comparisons
        }
        for stratum, comparisons in _COMPARISONS.items()
    }
    return {
        "spec": SPEC,
        "pairing": (
            "per-query exact McNemar pairs across arms on the identical corpus "
            "build (F1 §6.1); pairing key = the gold qid (ft-/fx-/fl-NNN-ph/pr, "
            "two phrasings per gold record); discordance tallies only — the "
            "registered analysis runs once, outside this runner (§6.6)"
        ),
        "strata": dict(f1_corpus.ANALYZED_COUNTS),
        "arm_order": list(ARM_ORDER),
        "budget": F1_TOKEN_BUDGET,
        "top_k": TOP_K,
        "queries": rows,
        "discordance": discordance,
    }


def verify_outcomes(outcomes: dict[str, Any]) -> None:
    """Structural check of an outcomes artifact (fail loud on drift).

    Accepts BOTH shapes — the collect-only in-memory dict (exactly the
    base keys) and the recorded on-disk copy (base keys + the stamped
    linkage key ``run_id``, optional exactly once, and only here: a
    ``run_id`` anywhere deeper in the artifact still trips the exact-key
    checks below — per-row keys, per-arm tuple keys and, since the
    round-2 tightening (#386 review), the discordance tally keys are
    all exactly-checked too). Re-verifying a written artifact therefore
    passes under the same contract that built it (#382)."""
    linkage = set(outcomes) & _OUTCOME_LINKAGE_KEYS
    if outcomes.get("run_id") is not None and not (
        isinstance(outcomes["run_id"], str) and outcomes["run_id"]
    ):
        raise AssertionError("outcomes run_id, when stamped, must be a non-empty string")
    missing = _OUTCOME_TOP_KEYS - set(outcomes)
    extra = set(outcomes) - _OUTCOME_TOP_KEYS - linkage
    if missing or extra:
        raise AssertionError(
            f"outcomes top-level keys must be exactly {sorted(_OUTCOME_TOP_KEYS)} — "
            f"missing={sorted(missing)}, unexpected={sorted(extra)}"
        )
    offenders = _stat_key_offenders(outcomes, "")
    if offenders:
        raise AssertionError(
            "outcomes artifact must not carry statistical keys "
            f"({_STAT_KEY_RE.pattern!r}) — forbidden at: {', '.join(offenders)}"
        )
    if outcomes["strata"] != f1_corpus.ANALYZED_COUNTS:
        raise AssertionError(f"strata must be exactly {f1_corpus.ANALYZED_COUNTS}")
    if outcomes["arm_order"] != list(ARM_ORDER):
        raise AssertionError(f"arm order must be exactly {list(ARM_ORDER)}")
    if outcomes["budget"] != F1_TOKEN_BUDGET or outcomes["top_k"] != TOP_K:
        raise AssertionError("budget/top-k drifted from the registration")
    queries = outcomes["queries"]
    if len(queries) != sum(f1_corpus.ANALYZED_COUNTS.values()):
        raise AssertionError("queries must cover the full analyzed set")
    qids = [row["qid"] for row in queries]
    if len(set(qids)) != len(qids):
        raise AssertionError("pairing keys (qids) must be unique")
    per_stratum: dict[str, int] = {}
    substratum_counts: dict[str, int] = {}
    for row in queries:
        per_stratum[row["stratum"]] = per_stratum.get(row["stratum"], 0) + 1
        expected_keys = _QUERY_ROW_KEYS | ({"substratum"} if row["stratum"] == "l_neg" else set())
        if set(row) != expected_keys:
            raise AssertionError(
                f"{row['qid']}: row keys must be exactly {sorted(expected_keys)} — "
                f"got {sorted(row)}"
            )
        if row["stratum"] == "l_neg":
            if row["substratum"] not in _SUBSTRATA:
                raise AssertionError(
                    f"{row['qid']}: substratum must be one of {sorted(_SUBSTRATA)}, "
                    f"got {row['substratum']!r}"
                )
            substratum_counts[row["substratum"]] = substratum_counts.get(row["substratum"], 0) + 1
        if set(row["arms"]) != set(ARM_ORDER):
            raise AssertionError(f"{row['qid']}: arms must be exactly {list(ARM_ORDER)}")
        for arm in ARM_ORDER:
            if set(row["arms"][arm]) != _ARM_TUPLE_KEYS:
                raise AssertionError(
                    f"{row['qid']}/{arm}: tuple keys must be exactly {sorted(_ARM_TUPLE_KEYS)}"
                )
            if not isinstance(row["arms"][arm]["hit"], bool):
                raise AssertionError(f"{row['qid']}/{arm}: hit must be boolean")
            if len(row["arms"][arm]["block_slugs"]) > TOP_K:
                raise AssertionError(f"{row['qid']}/{arm}: more than top-5 blocks issued")
    if per_stratum != f1_corpus.ANALYZED_COUNTS:
        raise AssertionError(f"stratum coverage drifted: {per_stratum}")
    # The L-neg sub-split must cover the stratum exactly (§8 entry 15);
    # the 16-trap/8-mixed bootstrap composition itself is pinned by the
    # manifest + tests, not hard-coded here (a §3.5 adjudication that
    # replaces a rejected mixed pair with trap-shaped surplus legitimately
    # shifts the split — the manifest records the run's own composition).
    if sum(substratum_counts.values()) != f1_corpus.ANALYZED_COUNTS["l_neg"]:
        raise AssertionError(f"l_neg substrata must cover the stratum exactly: {substratum_counts}")
    expected_disc = {s: {f"{f}_vs_{sec}" for f, sec in comps} for s, comps in _COMPARISONS.items()}
    if set(outcomes["discordance"]) != set(expected_disc):
        raise AssertionError("discordance strata drifted")
    for stratum, comparisons in expected_disc.items():
        if set(outcomes["discordance"][stratum]) != comparisons:
            raise AssertionError(f"discordance comparisons drifted on {stratum}")
        for comparison, tally in outcomes["discordance"][stratum].items():
            # Exact tally keys, derived from the same registered pair as
            # the comparison name itself — single source of truth
            # (_COMPARISONS). Renaming a tally key (e.g. "A_only" →
            # "run_id") must fail exactly like any other schema layer.
            left, right = comparison.split("_vs_")
            expected_tally_keys = {f"{left}_only", f"{right}_only"}
            if set(tally) != expected_tally_keys:
                raise AssertionError(
                    f"discordance tally keys on {stratum}/{comparison} must be exactly "
                    f"{sorted(expected_tally_keys)} — got {sorted(tally)}"
                )
            if len(tally) != 2 or not all(isinstance(v, int) for v in tally.values()):
                raise AssertionError("discordance tallies must be exactly two integer counts")


# ── manifest ──────────────────────────────────────────────────────────────────


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _git_state() -> dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        return {"git_commit": commit, "git_dirty": bool(status.strip())}
    except (OSError, subprocess.CalledProcessError):
        return {"git_commit": "none", "git_dirty": None}


def _code_version() -> dict[str, Any]:
    version_file = (ROOT / "VERSION").read_text().strip()
    return {
        "mnemos_version": mnemos_version,
        "version_file": version_file,
        "python_version": " ".join(sys.version.split()),
        "sqlite_version": sqlite3.sqlite_version,
        **_git_state(),
    }


def build_manifest_core(
    corpus: Corpus, ledger_state: dict[str, Any], analyzed: tuple[GoldQuery, ...]
) -> dict[str, Any]:
    """The pre-run manifest core — pure state capture, NO arm execution.

    The BLAKE2b corpus fingerprint is stamped HERE, before any arm runs
    (§4.2 anti-cherry-picking); the content-addressed run id hashes this
    core, so identical corpus + ledger + code + arm flags + budget
    produce the identical run id — and any drift produces a new one.
    """
    audit_pairs = f1_corpus.audit_subsample(corpus)
    # The run's own L-neg sub-split (§8 entry 15), computed from the
    # ACTIVE analyzed set: bootstrap = 16 trap + 8 mixed; a §3.5
    # adjudication replacing a rejected mixed pair with trap-shaped
    # surplus shifts it — the manifest records what THIS run measured.
    l_neg_composition: dict[str, int] = {"trap": 0, "mixed": 0}
    for q in analyzed:
        if q.stratum == "l_neg":
            substratum = q.substratum
            assert substratum is not None  # l_neg by the branch above
            l_neg_composition[substratum] += 1
    # Observed lens activation over the ACTIVE analyzed set (§8 entry
    # 16): a run-time fact of THIS code version's lens regexes, stamped
    # into the manifest — content, not statistics. Never asserted at
    # build time: a broadened lens must produce red corridor DATA, not
    # a corpus-build crash.
    lens_observed = f1_corpus.lens_activation_observed(corpus, analyzed)
    return {
        "runner_version": RUNNER_VERSION,
        "experiment": EXPERIMENT,
        "spec": SPEC,
        "arms": {arm.arm: arm.config_dict() for arm in ARMS},
        "arm_order": list(ARM_ORDER),
        "common_block": dict(COMMON_BLOCK),
        "equal_budget": F1_TOKEN_BUDGET,
        "top_k": TOP_K,
        "retrieval": {
            # The RRF fusion weight every arm search runs under — the
            # config default of the shared _store_settings shape (probe
            # finding 6, #300; the E3-runner canon). The root argument
            # is inert: only the search defaults are read.
            "hybrid_alpha": _store_settings(ROOT / ".manifest-pin").search.hybrid_alpha,
            "recall_depth": RECALL_DEPTH,
        },
        "corpus": {
            "generator_version": corpus.generator_version,
            "stratum_version": f1_corpus.STRATUM_VERSION,
            "seed": corpus.seed,
            "fingerprint_blake2b": f1_corpus.corpus_fingerprint(corpus),
            "counts": {
                **f1_corpus.segment_counts(corpus),
                "task_tagged": f1_corpus.CORPUS_COUNTS["task_tagged"],
                "distractors": f1_corpus.CORPUS_COUNTS["distractors"],
                "total": len(corpus.rows),
            },
            "task_display": dict(f1_corpus.TASK_DISPLAY),
            "tasks": list(f1_corpus.TASKS),
            "projects": dict(f1_corpus.TASK_PROJECTS),
            "agent": f1_corpus.AGENT,
            "l_neg_composition": l_neg_composition,
            "lens_observed": lens_observed,
        },
        "ledger": {
            "artifact": "benchmarks/experiments/f1_task_scope/adjudication_ledger.json",
            "state_sha256": f1_ledger.ledger_state_hash(ledger_state),
            "rejects": len(ledger_state["rejects"]),
            "replacements": len(ledger_state["replacements"]),
            "analyzed": dict(f1_corpus.ANALYZED_COUNTS),
            "audit_subsample_pairs": len(audit_pairs),
            "audit_rule": "sha256(pair-id) hex ascending, top ceil(20%), double-annotated (§3.5)",
        },
        "queries": {
            "total": len(analyzed),
            "qid_set_sha256": _sha256(_canonical_json(sorted(q.qid for q in analyzed))),
        },
        "clock": {
            "run_now": RUN_NOW.isoformat(),
            "session": SESSION,
            "note": (
                "frozen clock — retrieval stamps pre-seeded; wall-clock "
                "quantities never enter metrics"
            ),
        },
        "runtime": {
            "scanner_enabled": False,
            "embedder": "deterministic LexicalHashEmbedder",
            "store_isolation": (
                "single seeded build (fixed order, seeded uuid draw), cloned per "
                "arm through the SQLite backup API — byte-identical store copies"
            ),
            "lanes_enabled": False,
            "type_boost": True,
            "expand_ccr": False,
            "mode": "sync",
        },
        "code": _code_version(),
    }


def _core(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        k: v
        for k, v in manifest.items()
        if k not in ("created", "run_id", "manifest_sha256", "invariants")
    }


def manifest_core_hash(manifest: dict[str, Any]) -> str:
    """Content hash over the deterministic manifest core."""
    return _sha256(_canonical_json(_core(manifest)))


def finalize_manifest(core: dict[str, Any], invariants: dict[str, Any]) -> dict[str, Any]:
    """Stamp run id, creation time, invariants and the integrity hash."""
    staged = {**core, "invariants": invariants}
    core_hash = manifest_core_hash(staged)
    finalized = {
        **staged,
        "run_id": f"{EXPERIMENT}-{core_hash[:12]}",
        "created": datetime.now(UTC).isoformat(),
    }
    integrity = _sha256(
        _canonical_json({k: v for k, v in finalized.items() if k != "manifest_sha256"})
    )
    return {**finalized, "manifest_sha256": integrity}


def verify_manifest(manifest: dict[str, Any]) -> None:
    """Fail loud when a manifest's integrity hash or structure is broken."""
    required = {
        "runner_version",
        "experiment",
        "spec",
        "arms",
        "arm_order",
        "common_block",
        "equal_budget",
        "top_k",
        "retrieval",
        "corpus",
        "ledger",
        "queries",
        "clock",
        "runtime",
        "invariants",
        "code",
        "run_id",
        "created",
        "manifest_sha256",
    }
    missing = required - set(manifest)
    extra = set(manifest) - required
    if missing or extra:
        # Exact key set (repair 2c): an unexpected top-level key is as
        # broken as a missing one — schema, not convention (§4.3).
        raise AssertionError(
            f"manifest top-level keys must be exactly {sorted(required)} — "
            f"missing={sorted(missing)}, unexpected={sorted(extra)}"
        )
    offenders = _stat_key_offenders(manifest, "")
    if offenders:
        raise AssertionError(
            f"manifest must not carry statistical keys — forbidden at: {', '.join(offenders)}"
        )
    body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    expected = _sha256(_canonical_json(body))
    if manifest["manifest_sha256"] != expected:
        raise AssertionError("manifest_sha256 does not match the manifest body")
    if manifest["run_id"] != f"{EXPERIMENT}-{manifest_core_hash(manifest)[:12]}":
        raise AssertionError("run_id does not match the manifest core hash")
    if set(manifest["arms"]) != set(ARM_ORDER):
        raise AssertionError(f"arms must be exactly {list(ARM_ORDER)}")
    if manifest["arm_order"] != list(ARM_ORDER):
        raise AssertionError("arm order drifted from the registration")
    if manifest["equal_budget"] != F1_TOKEN_BUDGET or manifest["top_k"] != TOP_K:
        raise AssertionError("budget/top-k drifted from the registration")
    counts = manifest["corpus"]["counts"]
    locked = {
        "task_tagged": 360,
        "shared": 120,
        "checkpoint": 278,
        "misc": 194,
        "canary": 8,
        "total": 960,
    }
    for key, expected_count in locked.items():
        if counts.get(key) != expected_count:
            raise AssertionError(f"corpus count {key}={counts.get(key)} != {expected_count} (§3.4)")
    if manifest["ledger"]["analyzed"] != f1_corpus.ANALYZED_COUNTS:
        raise AssertionError("ledger analyzed strata drifted off 192/48/24")
    composition = manifest["corpus"]["l_neg_composition"]
    if (
        not isinstance(composition, dict)
        or set(composition) - {"trap", "mixed"}
        or sum(composition.values()) != f1_corpus.ANALYZED_COUNTS["l_neg"]
    ):
        raise AssertionError(
            f"l_neg_composition must cover the stratum over trap/mixed only: {composition!r}"
        )
    lens_observed = manifest["corpus"]["lens_observed"]
    if (
        not isinstance(lens_observed, dict)
        or set(lens_observed) != {"trap_activations", "mixed_activations", "code_axis_activations"}
        or not all(isinstance(v, int) and v >= 0 for v in lens_observed.values())
        or lens_observed["trap_activations"] + lens_observed["mixed_activations"]
        > f1_corpus.ANALYZED_COUNTS["l_neg"]
    ):
        raise AssertionError(
            f"lens_observed must be the observed activation counts over the three "
            f"registered families: {lens_observed!r}"
        )
    if manifest["clock"]["run_now"] != RUN_NOW.isoformat():
        raise AssertionError("scenario clock drifted from the frozen RUN_NOW")
    if manifest["corpus"]["fingerprint_blake2b"] != f1_corpus.corpus_fingerprint():
        raise AssertionError("corpus fingerprint drifted from the committed build")
    invariants = manifest["invariants"]
    if invariants["V1"]["identical"] != invariants["V1"]["probes"] or invariants["V1"]["failures"]:
        raise AssertionError("V1 prefix byte-stability failed")
    if (
        invariants["V3_shadow"]["failures"]
        or invariants["V3_shadow"]["subset_ok"] != (invariants["V3_shadow"]["probes"])
        or invariants["V3_shadow"]["code_only_ok"] != invariants["V3_shadow"]["probes"]
    ):
        raise AssertionError("V3 order-preservation shadow failed")
    if invariants["V4"]["violations"] != 0:
        raise AssertionError("V4 canary violations must be zero")
    if invariants["V5"]["violations"] != 0:
        raise AssertionError("V5 tag-contract violations must be zero")
    if not invariants["store_copy"]["digests_equal_across_arms"]:
        raise AssertionError("store copies drifted apart")


# ── collect / record ──────────────────────────────────────────────────────────


def collect_run(
    ledger: dict[str, Any] | None = None, root: Path | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Execute all arms and build (manifest, outcomes). Persists NOTHING."""
    state = ledger if ledger is not None else f1_ledger.load_ledger()
    corpus = f1_corpus.build_corpus()
    analyzed = f1_ledger.active_analyzed_queries(state)

    own_tmp: tempfile.TemporaryDirectory[str] | None = None
    if root is None:
        own_tmp = tempfile.TemporaryDirectory(prefix="mnemos-f1-")
        root = Path(own_tmp.name)
    try:
        # 1. The pre-run manifest core: the corpus fingerprint is stamped
        #    into the manifest BEFORE any arm executes (§4.2).
        core = build_manifest_core(corpus, state, analyzed)

        # 2. One seeded corpus build; byte-identical clones per arm.
        build_root = root / "build"
        with seeded_memory_ids("f1-task-scope-corpus"):
            slug_to_id, build_verification = build_store(build_root, corpus)
        build_settings = _store_settings(build_root)
        digests: dict[str, str] = {}
        for arm in ARM_ORDER:
            arm_settings = _arm_settings(root, arm)
            arm_settings.vesma.data_dir.mkdir(parents=True, exist_ok=True)
            clone_store(build_settings, arm_settings)
            digests[arm] = _store_content_digest(arm_settings)
        digest_equal = len(set(digests.values())) == 1

        # 3. Fixed arm order A0 → C → B → A over the identical query set.
        per_arm: dict[str, dict[str, QueryOutcome]] = {}
        arm_reports: dict[str, dict[str, Any]] = {}
        for arm in ARM_ORDER:
            per_arm[arm], arm_reports[arm] = execute_arm(
                arm, corpus, analyzed, slug_to_id, _arm_settings(root, arm)
            )

        # 4. Invariants (any breach raises — the run is void, §4.5).
        invariants = evaluate_invariants(
            per_arm, arm_reports, analyzed, build_verification, digests["A0"], digest_equal
        )

        # 5. Outcomes + finalized manifest (run id over the PRE-RUN core).
        outcomes = build_outcomes(analyzed, per_arm)
        manifest = finalize_manifest(core, invariants)
        verify_manifest(manifest)
        verify_outcomes(outcomes)
        return manifest, outcomes
    finally:
        if own_tmp is not None:
            own_tmp.cleanup()


def record_run(manifest: dict[str, Any], outcomes: dict[str, Any], runs_dir: Path) -> Path:
    """Write the run artifacts — WRITE-ONCE, ATOMIC, never overwritten."""
    run_id = str(manifest["run_id"])
    run_dir: Path = runs_dir / run_id
    if run_dir.exists():
        raise FileExistsError(
            f"run {run_id} already recorded at {run_dir} — "
            "recorded runs are write-once; a re-record is a new run state"
        )
    verify_manifest(manifest)
    verify_outcomes(outcomes)
    if outcomes.get("run_id") not in (None, manifest["run_id"]):
        raise AssertionError("outcomes/manifest run id mismatch")
    runs_dir.mkdir(parents=True, exist_ok=True)
    staging = runs_dir / f".tmp-{run_id}"
    if staging.exists():
        shutil.rmtree(staging)  # leftover of an earlier crashed attempt
    staging.mkdir()
    try:
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        recorded = {**outcomes, "run_id": manifest["run_id"]}
        (staging / "outcomes.json").write_text(json.dumps(recorded, indent=2) + "\n")
        # The WRITTEN artifact must satisfy the runner's own schema (#382):
        # what lands on disk is exactly what re-verification accepts — a
        # recorded outcomes.json that would fail its own verify_outcomes
        # never reaches the write-once directory.
        verify_outcomes(json.loads((staging / "outcomes.json").read_text()))
        try:
            staging.rename(run_dir)
        except OSError as exc:
            raise FileExistsError(
                f"run {run_id} already recorded at {run_dir} (rename race): {exc}"
            ) from exc
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return run_dir


# ── §9 run ledger (appends ONLY as part of the deliberate --record step) ──────


def run_ledger_entry(manifest: dict[str, Any]) -> str:
    """The §9 append-only entry line for one recorded run (§6.6)."""
    corpus = manifest["corpus"]
    ledger = manifest["ledger"]
    return (
        f"**{manifest['created'][:10]} — RUN — arms {manifest['arm_order'][0]}/"
        f"{manifest['arm_order'][1]}/{manifest['arm_order'][2]}/{manifest['arm_order'][3]} "
        f"— `{manifest['run_id']}` — recorded by explicit `--record`.** Corpus "
        f"fingerprint `{corpus['fingerprint_blake2b']}` (generator "
        f"{corpus['generator_version']}, seed {corpus['seed']}, 960 rows); ledger "
        f"state `{ledger['state_sha256'][:12]}…` (rejects={ledger['rejects']}, "
        f"replacements={ledger['replacements']}, analyzed "
        f"{ledger['analyzed']['t_gold']}/{ledger['analyzed']['x_gold']}/"
        f"{ledger['analyzed']['l_neg']} — L-neg split "
        f"{corpus['l_neg_composition']['trap']} trap + "
        f"{corpus['l_neg_composition']['mixed']} mixed, §8/15); "
        f"budget {manifest['equal_budget']}, top-"
        f"{manifest['top_k']}, lanes off, type-boost on, frozen clock "
        f"{manifest['clock']['run_now']}; code {manifest['code']['git_commit'][:12]}…"
        f" (mnemos {manifest['code']['mnemos_version']}, python "
        f"{manifest['code']['python_version'].split()[0]}). Artifacts: "
        f"`benchmarks/experiments/f1_task_scope/runs/{manifest['run_id']}/` "
        "(gitignored by design; committed deliberately by the report wave when "
        "citing). Invariants V1-V6 verified in the manifest; no statistics at "
        "run time — single-look analysis follows collection (§6.6).\n"
    )


def _ledger_append_check(text: str, run_id: str) -> None:
    """Shared §9 appendability guard (duplicate id + §9-is-last)."""
    if run_id in text:
        raise FileExistsError(f"run {run_id} already present in the §9 run ledger")
    # §9 must be the LAST section — an EOF append is then a §9 append and
    # nothing else (sections 1-7 are untouchable; §8 precedes §9).
    last_header = text[text.rindex("## ") :]
    if not last_header.startswith("## 9."):
        raise AssertionError("refusing to append to the §9 run ledger: the last section is not §9")


def validate_ledger_appendable(manifest: dict[str, Any], doc_path: Path) -> None:
    """Pre-validate §9 appendability BEFORE any artifact is written.

    Repair 2b (§8 entry 14): ``--record`` must not reach a state where
    the run artifacts exist but the §9 entry could not be appended. The
    same guards ``append_run_ledger`` enforces — duplicate run id in the
    doc, §9 not the last section — are checked up front; a failure here
    means NOTHING was written. If the append STILL fails after the
    artifacts exist (a race, a doc edit between the two steps), the run
    dir is quarantined (``<run_id>.UNLEDGERED``) and the run exits loud
    — never a silently half-recorded state.
    """
    _ledger_append_check(doc_path.read_text(), str(manifest["run_id"]))


def _quarantine_run_dir(run_dir: Path) -> Path:
    """Rename an unledgered run dir aside — artifacts without their §9
    entry are not a recorded run."""
    quarantine = run_dir.with_name(f"{run_dir.name}.UNLEDGERED")
    n = 1
    while quarantine.exists():
        quarantine = run_dir.with_name(f"{run_dir.name}.UNLEDGERED-{n}")
        n += 1
    run_dir.rename(quarantine)
    return quarantine


def append_run_ledger(manifest: dict[str, Any], doc_path: Path = DOC_PATH) -> Path:
    """Append the §9 run-ledger entry to the E-file (append-only, §9).

    Fires ONLY from the deliberate ``--record`` step. The doc's §9 is
    the final section, so an EOF append is a §9 append; the guard
    refuses a duplicate run id. Sections 1-7 are never touched.
    """
    text = doc_path.read_text()
    _ledger_append_check(text, str(manifest["run_id"]))
    doc_path.write_text(text.rstrip("\n") + "\n\n" + run_ledger_entry(manifest))
    return doc_path


# ── CLI ───────────────────────────────────────────────────────────────────────


def _print_summary(manifest: dict[str, Any], outcomes: dict[str, Any]) -> None:
    print(f"run_id: {manifest['run_id']}")
    lens_observed = manifest["corpus"]["lens_observed"]
    print(
        f"corpus: fingerprint={manifest['corpus']['fingerprint_blake2b'][:12]}… "
        f"rows={manifest['corpus']['counts']['total']} "
        f"(generator {manifest['corpus']['generator_version']}, "
        f"seed {manifest['corpus']['seed']}) | lens_observed: "
        f"trap {lens_observed['trap_activations']}, "
        f"mixed {lens_observed['mixed_activations']}, "
        f"code-axis {lens_observed['code_axis_activations']} (§8/16)"
    )
    print(
        f"ledger: {manifest['ledger']['state_sha256'][:12]}… "
        f"(rejects={manifest['ledger']['rejects']}, "
        f"replacements={manifest['ledger']['replacements']})"
    )
    arm_hits: dict[str, dict[str, int]] = {arm: {} for arm in ARM_ORDER}
    l_neg_split_hits: dict[str, dict[str, int]] = {arm: {} for arm in ARM_ORDER}
    for row in outcomes["queries"]:
        for arm in ARM_ORDER:
            stratum = row["stratum"]
            bucket = arm_hits[arm].setdefault(stratum, 0)
            arm_hits[arm][stratum] = bucket + (1 if row["arms"][arm]["hit"] else 0)
            if stratum == "l_neg":
                sub_bucket = l_neg_split_hits[arm].setdefault(row["substratum"], 0)
                l_neg_split_hits[arm][row["substratum"]] = sub_bucket + (
                    1 if row["arms"][arm]["hit"] else 0
                )
    split = manifest["corpus"]["l_neg_composition"]
    for arm in ARM_ORDER:
        hits = arm_hits[arm]
        sub = l_neg_split_hits[arm]
        counts = f1_corpus.ANALYZED_COUNTS
        print(
            f"arm {arm:>2}: t-gold hits {hits.get('t_gold', 0)}/{counts['t_gold']}   "
            f"x-gold hits {hits.get('x_gold', 0)}/{counts['x_gold']}   "
            f"l-neg hits {hits.get('l_neg', 0)}/{counts['l_neg']} "
            f"(trap {sub.get('trap', 0)}/{split['trap']} corridor, "
            f"mixed {sub.get('mixed', 0)}/{split['mixed']} descriptive — §8/15)"
        )
    for stratum, comparisons in outcomes["discordance"].items():
        for comparison, tally in comparisons.items():
            print(f"discordance {stratum} {comparison}: {tally}")
    invariants = manifest["invariants"]
    print(
        f"invariants: V1 {invariants['V1']['identical']}/{invariants['V1']['probes']} "
        f"byte-identical | V3-shadow {invariants['V3_shadow']['subset_ok']}/"
        f"{invariants['V3_shadow']['probes']} subsets "
        f"({invariants['V3_shadow']['code_only_ok']} code-only) | V4 violations "
        f"{invariants['V4']['violations']} | V5 violations "
        f"{invariants['V5']['violations']} | store copies identical: "
        f"{invariants['store_copy']['digests_equal_across_arms']}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="F1 task-scope runner — arms A0/C/B/A over the f1-mixed corpus (§1.3)"
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help=(
            "write the run artifacts (write-once) under the runs directory and "
            "append the §9 run-ledger entry; the DEFAULT refuses to record — "
            "the first real F1 run is an owner-gated deliberate decision"
        ),
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=RUNS_DIR,
        help=f"runs directory (default: {RUNS_DIR})",
    )
    parser.add_argument(
        "--doc-path",
        type=Path,
        default=DOC_PATH,
        help=f"the E-file whose §9 run ledger --record appends to (default: {DOC_PATH})",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress the summary")
    args = parser.parse_args(argv)

    print(
        "f1: collecting arms A0/C/B/A over the f1-mixed corpus (deterministic)…",
        file=sys.stderr,
    )
    manifest, outcomes = collect_run()

    if not args.quiet:
        _print_summary(manifest, outcomes)

    if not args.record:
        print(
            "f1: REFUSING to record — collect-only. The first recorded F1 run is "
            "an owner-gated deliberate human decision (F1 §4.3 anti-HARKing "
            "window); re-run with --record to persist this run.",
            file=sys.stderr,
        )
        return 0

    # Pre-validate §9 appendability BEFORE writing artifacts (repair 2b):
    # a non-appendable ledger means nothing gets written at all.
    try:
        validate_ledger_appendable(manifest, args.doc_path)
    except (AssertionError, FileExistsError, OSError) as exc:
        print(
            f"f1: FAIL — §9 run ledger is not appendable, nothing recorded: {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        run_dir = record_run(manifest, outcomes, args.runs_dir)
    except FileExistsError as exc:
        print(f"f1: FAIL — {exc}", file=sys.stderr)
        return 1

    # The append was pre-validated; if it STILL fails (a doc edit or a
    # race between validation and append), quarantine the artifacts —
    # a half-recorded state (artifacts without the §9 entry) must never
    # survive silently.
    try:
        append_run_ledger(manifest, args.doc_path)
    except (AssertionError, FileExistsError, OSError) as exc:
        quarantine = _quarantine_run_dir(run_dir)
        print(
            f"f1: FAIL — §9 append failed after artifacts were written ({exc}); "
            f"run dir quarantined as {quarantine.name} under {quarantine.parent}. "
            "No half-recorded state: fix the ledger, then re-run --record.",
            file=sys.stderr,
        )
        return 1
    print(
        f"f1: run recorded (write-once) → {run_dir}; §9 run-ledger entry appended",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
