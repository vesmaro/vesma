"""S1 extension scenarios (ADR-0020 BF-1) — quarantine, retraction, FP, neutrality.

Everything here is deterministic (no wall-clock, no RNG, no network)
and runs against the REAL manager/pipeline surfaces:

* **SC-S1 write-find** — the ADR-0019 immediate-findability contract:
  ``add`` → ``search`` with no delay in between; the row must surface
  under its own id (LTV's causal half; the latency half belongs to S2).
* **SC-S2 supersede-refind** — write → find → SUBSTITUTE the served
  projection (``update`` on the same id, embed healed the way the
  production sweeper heals it) → find again: the new projection is
  served, the id is unchanged, the old projection is gone from
  retrieval.
* **SC-S3 refuse-render** — processing refusal → retraction render: a
  curated deployment, real ``refine_single`` cycles, danger-bearing
  rows land in the terminal lane-(b) quarantine (reason = detector
  class codes), clean rows publish; the quarantined rows answer
  direct access with ``[retracted: <iso-ts>]``, are excluded from
  issuance, remain retrievable by id, and a CCR-cached original of a
  quarantined row is retracted too.
* **detector-quarantine-fp** — text-level over the independently
  labelled corpus (``danger_labels``) plus a live ingest observation
  (real ``add`` path: how many benign tech-pattern entries the N1
  gate demotes).
* **render-neutrality** — no detector class may appear in any
  issuance render (ADR-0020 F7 invariant; ADR-0019 §5 amendment).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from benchmarks.corpus.corpus import GoldenEntry
from benchmarks.corpus.danger_labels import DANGEROUS_SLUGS, LABELLED_ENTRIES
from benchmarks.corpus.deterministic_embedder import LexicalHashEmbedder
from benchmarks.corpus.tech_patterns import TECH_PATTERN_ENTRIES
from benchmarks.stands.s1_quality.harness import (
    SOURCE_TO_ENUM,
    STATUS_TO_ENUM,
    _entry_tags,
    golden_settings,
)
from vesmaro.config import Settings
from vesmaro.danger_detectors import DETECTOR_CLASSES, detect
from vesmaro.manager import MemoryManager
from vesmaro.models import MemoryCreate, MemoryStatus, MemoryUpdate
from vesmaro.pipeline.refine import refine_single
from vesmaro.storage.vector_store import VectorStore

#: The §5 retraction render format (cause-neutral — anything else is a
#: render-neutrality violation, e.g. a leaked reason).
RETRACTION_RE = re.compile(r"^\[retracted: \S+\]$")

#: Detector class tokens that are UNAMBIGUOUS in any render. The bare
#: class code ``secret`` collides with legitimate prose (corpus entries
#: contain the word), so its leak is caught by the reason-composition
#: and format checks on retraction-family renders instead — a render
#: that sticks to ``[retracted: <iso-ts>]`` cannot carry any class.
UNAMBIGUOUS_CLASS_TOKENS: tuple[str, ...] = ("prompt-injection", "detector-error")

_S1_TOKEN = "zephyrpoint"
_S1_TEXT = (
    "zephyrpoint telemetry beacon calibration table. Beacon A: offset +2.1, "
    "drift 0.004/h. Beacon B: offset -0.7, drift 0.001/h. Recalibrate when "
    "the accumulated drift exceeds 0.5 — the table is the single source."
)
_V1_TOKEN = "obsidianmark"
_V1_TEXT = (
    "obsidianmark scheduling policy v1. Cadence: weekly, Mondays only. "
    "The scheduler picks up the queue at 09:00 UTC, drains it fully, and "
    "writes a completion summary. Overflow rolls to the next Monday slot."
)
_V2_TEXT = (
    "obsidianmark scheduling policy v2. Cadence: daily, with a weekend "
    "freeze window from Saturday 00:00 UTC to Monday 06:00 UTC. Overflow "
    "rolls to the next unfrozen slot instead of the next Monday."
)
_V1_ONLY_QUERY = "weekly Mondays cadence"
# The v1-only lexical marker for the SC-S2 gone-probe: "Mondays" occurs in
# _V1_TEXT and nowhere in _V2_TEXT ("Monday 06:00" is not the plural token),
# while "cadence" is shared by BOTH projections (search v2, issue #313 —
# see scenario_supersede_refind).
_V1_UNIQUE_TOKEN = "Mondays"
_V2_ONLY_QUERY = "weekend freeze window"
_CCR_TEXT = (
    "Lanternkeep release checklist, long form. Verify the wheel version "
    "matches the tag, run the smoke container, check the migration diary "
    "for pending entries, confirm the backup rotation kept seven daily "
    "snapshots, and only then flip the canary route. Every step records "
    "an operator initials line; a checklist without initials is void. "
    "The rollback path is the previous tag plus a forward-fix note."
)
_CCR_TOKEN = "lanternkeep"


def scenario_settings(root: Path, *, visibility: str = "immediate") -> Settings:
    """Golden-shaped settings with a chosen ``mnemos.visibility`` policy."""
    settings = Settings.model_validate(
        {
            "vesma": {
                "vault_path": str(root / "vault"),
                "data_dir": str(root / "data"),
                "db_name": f"s1-{visibility}.db",
                "visibility": visibility,
            },
            "scanner": {"enabled": False},
            "ccr": {"min_size_chars": 280},
        }
    )
    settings.resolve_paths()
    return settings


def deterministic_manager(settings: Settings) -> MemoryManager:
    """A manager with the deterministic lexical embedder installed.

    Every scenario manager goes through here — same determinism
    contract as the golden harness (no ONNX download, no model-version
    drift, byte-reproducible runs).
    """
    mgr = MemoryManager(settings)
    mgr._embedder = LexicalHashEmbedder()
    return mgr


def _add_published(mgr: MemoryManager, entry: GoldenEntry) -> Any:
    """Add one fixture entry through the real ``add`` path (explicit status)."""
    data = MemoryCreate(
        content=entry.content,
        title=entry.title,
        tags=_entry_tags(entry),
        source=SOURCE_TO_ENUM[entry.source],
        status=STATUS_TO_ENUM[entry.status],
    )
    return mgr.add(data, project=entry.project, agent=entry.agent)


def _add_default(mgr: MemoryManager, entry: GoldenEntry) -> Any:
    """Add one fixture entry letting the visibility policy pick the status."""
    data = MemoryCreate(
        content=entry.content,
        title=entry.title,
        tags=_entry_tags(entry),
        source=SOURCE_TO_ENUM[entry.source],
    )
    return mgr.add(data, project=entry.project, agent=entry.agent)


def _scenario_entry(slug: str, project: str, agent: str, title: str, content: str) -> GoldenEntry:
    return GoldenEntry(
        slug=slug,
        project=project,
        agent=agent,
        title=title,
        content=content,
        mnemos_tags=("rule",),
        free_tags=("s1-scenario",),
    )


def _result_ids(mgr: MemoryManager, text: str, project: str) -> set[str]:
    return {r.memory.id for r in mgr.search(text, project=project, limit=10)}


# ── SC-S1: write → immediate find ────────────────────────────────────────────


def scenario_write_find(mgr: MemoryManager) -> dict[str, Any]:
    """ADR-0019 contract S1: findable immediately after add, no sleeps."""
    entry = _scenario_entry(
        "s1-write-find", "mnemos-core", "s1-stand", "zephyrpoint calibration", _S1_TEXT
    )
    memory = _add_published(mgr, entry)
    ids = _result_ids(mgr, _S1_TOKEN, entry.project)  # immediately, zero delay
    row = mgr.sqlite.get(memory.id)
    return {
        "scenario": "write-find",
        "found_immediately": memory.id in ids,
        "id_stable_after_search": row is not None and row.id == memory.id,
        "pass": memory.id in ids and row is not None and row.id == memory.id,
    }


# ── SC-S2: write → find → supersede → find again ─────────────────────────────


def scenario_supersede_refind(mgr: MemoryManager) -> dict[str, Any]:
    """ADR-0019 contract S2: the substituted projection serves, id unchanged.

    The substitution runs through the real ``update`` path (same id,
    Layer-1 scanner re-run included). Two honest observations along the
    way: (a) the filter projection freshness right after a content
    edit — recorded as an informational flag; it observed ``true``
    until issue #193 (a stale ``clean_content`` kept serving the OLD
    filtered text) and must observe ``false`` since the fix resets the
    projection in the same transaction as the content write — the flag
    is the regression tripwire for that fix; (b) the projection is
    then regenerated the way the pipeline does
    (``apply_context_filter``) and the embed is healed the way the
    sweeper heals it, after which the new projection serves and the
    old one is gone from the lexical leg (checked with the vector leg
    off — fuzzy vector similarity is retrieval, not a stale
    projection).
    """
    entry = _scenario_entry(
        "s2-supersede", "mnemos-core", "s1-stand", "obsidianmark scheduling", _V1_TEXT
    )
    memory = _add_published(mgr, entry)
    found_v1 = memory.id in _result_ids(mgr, _V1_TOKEN, entry.project)

    updated = mgr.update(memory.id, MemoryUpdate(content=_V2_TEXT))
    assert updated is not None, "update dropped the row — same-ID semantics broken"
    stale = mgr.sqlite.get(memory.id)
    assert stale is not None
    stale_projection = _V1_TEXT[:40] in stale.effective_content()

    mgr.apply_context_filter(memory.id)  # regenerate the served projection
    healed = mgr.sqlite.get(memory.id)
    assert healed is not None
    mgr.upsert_embedding(healed)  # the sweeper's stale-embed heal

    served = healed.effective_content()
    with fts_only_leg():
        v2_found_fts = memory.id in _result_ids(mgr, _V2_ONLY_QUERY, entry.project)
        # Search v2 (issue #313, ADR-0029): the gone-check must prove the
        # OLD PROJECTION is not served, so it probes a token that exists
        # ONLY in the v1 projection ("Mondays"; "cadence" is shared by
        # both projections). The pre-v2 whole-phrase probe ("weekly
        # Mondays cadence") only worked because the M15.2 phrase
        # semantics hid the shared token; under v2 semantics the AND leg
        # is empty and the ranked OR fallback legitimately re-surfaces
        # the row — which serves the NEW projection (the contract under
        # test), not the old one. A single-token probe is also the exact
        # shape that never triggers the OR fallback (single-term queries
        # do not retry), so the flag stays a strict stale-projection
        # tripwire under both query generations.
        v1_gone_fts = memory.id not in _result_ids(mgr, _V1_UNIQUE_TOKEN, entry.project)
    serves_new = _V2_TEXT[:40] in served and _V1_TEXT[:40] not in served
    return {
        "scenario": "supersede-refind",
        "found_v1_before": found_v1,
        "id_unchanged_by_substitution": updated.id == memory.id,
        "filter_projection_stale_after_update": stale_projection,
        "served_projection_regenerated": serves_new,
        "new_projection_findable": v2_found_fts,
        "old_projection_gone_from_lexical_leg": v1_gone_fts,
        "pass": (
            found_v1 and updated.id == memory.id and serves_new and v2_found_fts and v1_gone_fts
        ),
    }


# ── SC-S3: processing refusal → quarantine → retraction render ───────────────


def scenario_refuse_render(root: Path) -> dict[str, Any]:
    """ADR-0019 contract S3 + §5 B2b: refusal quarantines; renders retract.

    A curated deployment (the policy knob that routes visibility
    through the publication gate) runs REAL ``refine_single`` cycles:
    the deterministic stub produces no artifact for solo rows, so each
    completion hands the row to the curated publication gate — clean ⇒
    published, danger ⇒ lane-(b) quarantine with the detector class
    codes as the reason. The quarantined rows are then probed on every
    §5 surface: direct access, issuance, id retrievability, and the
    CCR cached original.
    """
    from benchmarks.corpus.corpus import PLANTED_SECRETS

    mgr = deterministic_manager(scenario_settings(root, visibility="curated"))
    try:
        danger_secret = _scenario_entry(
            "s3-refuse-secret",
            "mnemos-core",
            "s1-stand",
            "nightly backup note",
            f"Nightly backup note. Rotate the keyring before the dump: "
            f"{PLANTED_SECRETS['aws-key']} stays valid for the window.",
        )
        danger_injection = _scenario_entry(
            "s3-refuse-injection",
            "mnemos-core",
            "s1-stand",
            "chat transcript excerpt",
            "Support transcript excerpt. The customer message contained an "
            "embedded control token <|im_start|> that the UI escaped; keep "
            "raw transcripts quarantined from prompt assembly.",
        )
        clean = _scenario_entry(
            "s3-clean", "mnemos-core", "s1-stand", "contributing guide", _CCR_TEXT
        )
        m_secret = _add_default(mgr, danger_secret)
        m_injection = _add_default(mgr, danger_injection)
        m_clean = _add_default(mgr, clean)

        outcomes = {
            "secret": refine_single(mgr, m_secret.id),
            "injection": refine_single(mgr, m_injection.id),
            "clean": refine_single(mgr, m_clean.id),
        }
        q_secret = mgr.sqlite.get(m_secret.id)
        q_injection = mgr.sqlite.get(m_injection.id)
        p_clean = mgr.sqlite.get(m_clean.id)
        assert q_secret is not None and q_injection is not None and p_clean is not None

        # CCR cached original of a row that then gets quarantined (the
        # terminal transition the refusal itself produces).
        comp = mgr.compress_content(
            _CCR_TEXT, project=clean.project, agent="s1-stand", session="s3-ccr"
        )
        ccr_hash = str(comp.get("hash", ""))
        assert ccr_hash, "scenario fixture broken: CCR block was not cached"
        assert mgr.quarantine_entry(m_clean.id, reason="secret", source="s1-stand")
        ccr = mgr.retrieve_content(ccr_hash, project=clean.project)
        q_clean = mgr.sqlite.get(m_clean.id)
        assert q_clean is not None

        served_secret = mgr.get(m_secret.id)
        served_injection = mgr.get(m_injection.id)
        assert served_secret is not None and served_injection is not None

        reasons_ok = (
            q_secret.quarantine_reason in DETECTOR_CLASSES
            and set(str(q_injection.quarantine_reason or "").split(",")) <= set(DETECTOR_CLASSES)
            and q_secret.quarantine_reason is not None
            and q_injection.quarantine_reason is not None
        )
        renders_ok = all(
            RETRACTION_RE.match(s.content or "") is not None
            for s in (served_secret, served_injection)
        )
        titles_withheld = served_secret.title is None and served_injection.title is None
        originals_withheld = PLANTED_SECRETS["aws-key"] not in (
            served_secret.content or ""
        ) and "im_start" not in (served_injection.content or "")
        issuance_excluded = (
            m_secret.id not in _result_ids(mgr, "keyring", danger_secret.project)
            and m_secret.id not in _result_ids(mgr, "transcript", danger_injection.project)
            and m_injection.id not in _result_ids(mgr, "transcript", danger_injection.project)
        )
        ccr_retracted = (
            bool(ccr.get("retracted"))
            and RETRACTION_RE.match(str(ccr.get("original", ""))) is not None
            and _CCR_TOKEN not in str(ccr.get("original", ""))
        )
        clean_published_before_ccr_quarantine = p_clean.status == MemoryStatus.PUBLISHED

        return {
            "scenario": "refuse-render",
            "refine_outcomes": outcomes,
            "refusal_reasons_are_class_codes": reasons_ok,
            "quarantine_reasons": {
                "secret": q_secret.quarantine_reason,
                "injection": q_injection.quarantine_reason,
                "ccr-row": q_clean.quarantine_reason,
            },
            "retraction_render_format": renders_ok,
            "titles_withheld": titles_withheld,
            "original_content_withheld": originals_withheld,
            "quarantine_exclusion_from_issuance": issuance_excluded,
            "retrievable_by_id": served_secret is not None and served_injection is not None,
            "ccr_original_retracted": ccr_retracted,
            "clean_row_published": clean_published_before_ccr_quarantine,
            "pass": (
                reasons_ok
                and renders_ok
                and titles_withheld
                and originals_withheld
                and issuance_excluded
                and ccr_retracted
                and clean_published_before_ccr_quarantine
            ),
            # Surfaces for the render-neutrality sweep.
            "_retraction_renders": [
                served_secret.content or "",
                served_injection.content or "",
                str(ccr.get("original", "")),
            ],
            "_quarantine_reasons": [
                str(q_secret.quarantine_reason),
                str(q_injection.quarantine_reason),
                str(q_clean.quarantine_reason),
            ],
        }
    finally:
        mgr.close()


# ── detector-quarantine-fp ───────────────────────────────────────────────────


def measure_detector_quarantine_fp(mgr: MemoryManager | None = None) -> dict[str, Any]:
    """FP observability over the independently labelled corpus.

    Text level: ``detect`` over every labelled entry; a benign label
    with a positive signal is a false positive, a dangerous label with
    no signal is a false negative (the planted literals are
    catalogue-shaped, so FN must stay 0). Live level (when ``mgr`` is
    given): ingest the tech-pattern class through the real ``add``
    path and count N1 demotions — the operational cost of the FPs.
    """
    tp = fp = fn = tn = 0
    fp_slugs: list[str] = []
    fn_slugs: list[str] = []
    for entry in LABELLED_ENTRIES:
        positive = detect(entry.content, entry.title).positive
        dangerous = entry.slug in DANGEROUS_SLUGS
        if dangerous and positive:
            tp += 1
        elif dangerous and not positive:
            fn += 1
            fn_slugs.append(entry.slug)
        elif not dangerous and positive:
            fp += 1
            fp_slugs.append(entry.slug)
        else:
            tn += 1
    benign = fp + tn

    live: dict[str, Any] | None = None
    if mgr is not None:
        demoted: list[str] = []
        for entry in TECH_PATTERN_ENTRIES:
            memory = _add_published(mgr, entry)
            if memory.status != MemoryStatus.PUBLISHED:
                demoted.append(entry.slug)
        live = {
            "ingested": len(TECH_PATTERN_ENTRIES),
            "demoted_to_raw_by_n1": len(demoted),
            "demoted_slugs": demoted,
        }
    return {
        "labelled_entries": len(LABELLED_ENTRIES),
        "benign_entries": benign,
        "dangerous_entries": tp + fn,
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "true_negatives": tn,
        "fp_rate_over_benign": fp / benign if benign else 0.0,
        "fn_rate_over_dangerous": fn / (tp + fn) if (tp + fn) else 0.0,
        "fp_slugs": fp_slugs,
        "fn_slugs": fn_slugs,
        "live_ingest": live,
        "note": (
            "conditional corridor (ADR-0020): applies only while "
            "injection-acceptance = 1.000 and quarantine-exclusion = 1.000; "
            "the FP rate must never be 'improved' by weakening detectors"
        ),
    }


# ── render-neutrality invariant ──────────────────────────────────────────────


def check_render_neutrality(
    issuance_surfaces: dict[str, list[str]],
    retraction_renders: list[str],
    quarantine_reasons: list[str],
) -> dict[str, Any]:
    """F7 invariant: no detector class in any issuance render.

    Two-tier check (documented scoping): the unambiguous class tokens
    are swept over EVERY render; the class-code compositions (incl.
    the prose-colliding bare ``secret`` code) are checked on the
    retraction-family renders, which are format-constrained and short
    enough that any embedded reason sticks out — a render matching
    ``[retracted: <iso-ts>]`` cannot carry a class.
    """
    violations: list[dict[str, str]] = []

    def _flag(surface: str, kind: str, detail: str) -> None:
        violations.append({"surface": surface, "kind": kind, "detail": detail})

    for surface, texts in issuance_surfaces.items():
        for text in texts:
            low = text.lower()
            for token in UNAMBIGUOUS_CLASS_TOKENS:
                if token in low:
                    _flag(surface, "class-token", token)
    for i, render in enumerate(retraction_renders):
        low = render.lower()
        if RETRACTION_RE.match(render) is None:
            _flag(f"retraction[{i}]", "format", render[:60])
        for token in UNAMBIGUOUS_CLASS_TOKENS:
            if token in low:
                _flag(f"retraction[{i}]", "class-token", token)
        for reason in quarantine_reasons:
            if reason and reason.lower() in low:
                _flag(f"retraction[{i}]", "reason-leak", reason)

    surfaces_checked = sum(len(v) for v in issuance_surfaces.values()) + len(retraction_renders)
    return {
        "surfaces_checked": surfaces_checked,
        "issuance_surface_kinds": sorted(issuance_surfaces),
        "violations": violations,
        "ok": not violations,
    }


# ── McNemar jig (interim) ────────────────────────────────────────────────────


def _binom_two_sided_p(b: int, c: int) -> float:
    """Exact two-sided sign-test p-value on discordant pairs (no RNG)."""
    from math import comb

    n = b + c
    if n == 0:
        return 1.0
    m = min(b, c)
    tail = sum(comb(n, k) for k in range(0, m + 1)) / (2**n)
    return min(1.0, 2 * tail)


def mcnemar_interim_hits(
    hybrid_hits: list[bool], comparison_hits: list[bool], *, pair: str
) -> dict[str, Any]:
    """Interim McNemar jig (ADR-0020): sign test on discordant pairs.

    ``hit`` is a per-query binary outcome (full recall@10 reached).
    ``b`` = comparison hit & hybrid miss, ``c`` = comparison miss &
    hybrid hit. Full McNemar is deferred until the rated corpus grows
    48 → ~192 (underpowered at 48); the sign test is the interim.
    """
    if len(hybrid_hits) != len(comparison_hits):
        raise ValueError("paired legs must cover the same queries in the same order")
    b = sum(1 for h, f in zip(comparison_hits, hybrid_hits, strict=True) if f and not h)
    c = sum(1 for h, f in zip(comparison_hits, hybrid_hits, strict=True) if h and not f)
    return {
        "pair": pair,
        "queries": len(hybrid_hits),
        "hits_leg_a": sum(comparison_hits),
        "hits_leg_b": sum(hybrid_hits),
        "discordant_b": b,
        "discordant_c": c,
        "p_two_sided": _binom_two_sided_p(b, c),
        "note": (
            "interim per ADR-0020 (48 judged queries are underpowered for "
            "McNemar); the same jig re-targets raw-vs-refined projections "
            "when deterministic refined projections exist"
        ),
    }


def fts_only_leg() -> Any:
    """Scoped FTS-only emulation: the vector leg returns nothing.

    Usage: ``with fts_only_leg(): ...`` — the deterministic interim
    pairing is FTS-only ("raw" single-leg retrieval) vs the composed
    hybrid RRF path, the only deterministic composition available
    without an LLM seam.
    """
    return _VectorLegOff()


class _VectorLegOff:
    """Context manager zeroing the vector leg (scoped, restores on exit)."""

    def __enter__(self) -> _VectorLegOff:
        self._original = VectorStore.search

        def silent(
            self: VectorStore,
            query_embedding: list[float],
            limit: int = 20,
            *,
            project: str | None = None,
        ) -> list[tuple[str, float]]:
            del self, query_embedding, limit, project
            return []

        VectorStore.search = silent  # type: ignore[method-assign, unused-ignore]
        return self

    def __exit__(self, *exc: object) -> None:
        VectorStore.search = self._original  # type: ignore[method-assign, unused-ignore]


__all__ = [
    "RETRACTION_RE",
    "check_render_neutrality",
    "fts_only_leg",
    "golden_settings",
    "mcnemar_interim_hits",
    "measure_detector_quarantine_fp",
    "scenario_refuse_render",
    "scenario_settings",
    "scenario_supersede_refind",
    "scenario_write_find",
]
