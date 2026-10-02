"""E2 experimental-corpus loader — ingest only, no experiment runs.

Builds a fresh in-process manager holding the COMBINED experimental
corpus (golden 81 + G-gov 100 + checkpoint padding 240) in fixed order,
with the deterministic BLAKE2b embedder installed before ingest (same
discipline as the S1 golden harness). This is corpus-preparation
infrastructure for the future E3 runner: NO assemble_context calls, no
lanes, no metrics, no leg logic live here.

Two ingest semantics meet here, and the loader keeps them distinct:

* **Stratum seeds** (governance + checkpoint padding) FAIL LOUD when the
  write boundary demotes them (ADR-0019 N1): E0 §4.4 seed hygiene makes
  injection-acceptance an invariant (= 1.000 at the screen; a demotion
  means the seed content is contaminated and the corpus, not the
  assertion, gets fixed).
* **Golden entries** keep the golden-harness semantics: the corpus's
  PLANTED entries model the residual-threat population the issuance
  scans exist for, so a detector demotion is restored at the store
  level (status + re-embed) exactly as ``harness.build_golden_manager``
  does — the experimental corpus must ingest the golden 81 with the
  same admissibility the S1-pinned baseline measures.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from benchmarks.corpus.corpus import CORPUS, GoldenEntry
from benchmarks.corpus.deterministic_embedder import LexicalHashEmbedder
from benchmarks.strata.e2_gov.checkpoints import CHECKPOINT_ENTRIES
from benchmarks.strata.e2_gov.profile import experimental_corpus
from benchmarks.strata.e2_gov.records import GOV_RECORDS
from vesma.embeddings import EmbeddingProvider
from vesma.manager import MemoryManager
from vesma.models import MemoryCreate, MemorySource, MemoryStatus

_SOURCE_TO_ENUM: dict[str, MemorySource] = {s.value: s for s in MemorySource}
_STATUS_TO_ENUM: dict[str, MemoryStatus] = {
    "published": MemoryStatus.PUBLISHED,
    "processed": MemoryStatus.PROCESSED,
    "raw": MemoryStatus.RAW,
}

#: Golden slugs — planted-secret entries whose detector demotion is
#: restored at the store level (golden-harness semantics), as opposed to
#: stratum seeds whose demotion is a hard failure (E0 §4.4 hygiene).
_GOLDEN_SLUGS: frozenset[str] = frozenset(e.slug for e in CORPUS)


def _entry_tags(entry: GoldenEntry) -> list[str]:
    tags = [f"project:{entry.project}", f"agent:{entry.agent}"]
    tags += [f"mnemos:{t}" for t in entry.mnemos_tags]
    tags += list(entry.free_tags)
    return tags


def build_experimental_manager(
    root: Path, embedder: EmbeddingProvider | None = None
) -> tuple[MemoryManager, dict[str, str]]:
    """Ingest the combined experimental corpus into a fresh manager.

    Returns ``(manager, slug_to_id)``. Fixed order (golden → governance
    → checkpoints), deterministic embedder by default, scanner off —
    the same runtime shape the golden harness pins.
    """
    # Masquerade guard (issue #277, defense in depth): ingest routes by
    # slug membership in _GOLDEN_SLUGS, so a stratum seed whose slug
    # collided with a golden slug would silently take the golden-restore
    # branch and dodge the fail-loud stratum screen below. The suite
    # already pins slug disjointness (test_strata_are_outside_the_s1_
    # measured_corpus); this asserts it AT the routing boundary too.
    stratum_slugs = {e.slug for e in GOV_RECORDS} | {e.slug for e in CHECKPOINT_ENTRIES}
    masquerading = stratum_slugs & _GOLDEN_SLUGS
    if masquerading:
        raise AssertionError(
            f"stratum seed slug(s) masquerade as golden slugs: {sorted(masquerading)} "
            "— they would silently dodge the fail-loud demotion screen "
            "(loader.py, issue #277); fix the slug collision, not this guard"
        )

    # Local import to avoid a module-level stand dependency (mirrors
    # harness.py's lazy mnemos imports).
    from benchmarks.stands.s1_quality.harness import golden_settings

    installed = embedder if embedder is not None else LexicalHashEmbedder()
    mgr = MemoryManager(golden_settings(root))
    mgr._embedder = installed
    slug_to_id: dict[str, str] = {}
    try:
        for entry in experimental_corpus():
            data = MemoryCreate(
                content=entry.content,
                title=entry.title,
                tags=_entry_tags(entry),
                source=_SOURCE_TO_ENUM[entry.source],
                status=_STATUS_TO_ENUM[entry.status],
            )
            memory = mgr.add(data, project=entry.project, agent=entry.agent)
            intended = _STATUS_TO_ENUM[entry.status]
            if memory.status != intended and entry.slug in _GOLDEN_SLUGS:
                # Golden PLANTED entry: detector demotion is expected and
                # restored at the store level, mirroring
                # harness.build_golden_manager (ADR-0019 N1) — the golden
                # 81 must carry the same admissibility the S1 baseline
                # measures, planted population included.
                mgr.sqlite.update_status(memory.id, intended)
                mgr.vectors.upsert(
                    memory.id,
                    mgr.embed_for(memory),
                    {"project": memory.project, "agent": memory.agent},
                )
                memory.status = intended
            if memory.status != intended:
                # Stratum seeds must pass the write boundary as-authored:
                # a demotion here is seed contamination (E0 §4.4 hygiene),
                # not harness behavior to paper over.
                raise AssertionError(
                    f"stratum seed {entry.slug!r} was demoted to "
                    f"{memory.status} at ingest — fix the seed content "
                    "(danger-detector trip), not this loader"
                )
            slug_to_id[entry.slug] = memory.id
    except BaseException:
        mgr.close()
        raise
    return mgr, slug_to_id


@contextmanager
def fresh_experimental_manager(
    root: Path | None = None, embedder: EmbeddingProvider | None = None
) -> Iterator[tuple[MemoryManager, dict[str, str]]]:
    """Build, yield, close — temp-dir hygiene for tests and future runners."""
    tmp: TemporaryDirectory[str] | None = None
    if root is None:
        tmp = TemporaryDirectory(prefix="mnemos-e2-")
        root = Path(tmp.name)
    mgr, slug_to_id = build_experimental_manager(root, embedder=embedder)
    try:
        yield mgr, slug_to_id
    finally:
        mgr.close()
        if tmp is not None:
            tmp.cleanup()


def stratum_record_ids(slug_to_id: dict[str, str]) -> dict[str, str]:
    """Slug → memory id for the E2 stratum entries (governance + padding)."""
    wanted = [e.slug for e in GOV_RECORDS + CHECKPOINT_ENTRIES]
    return {slug: slug_to_id[slug] for slug in wanted if slug in slug_to_id}
