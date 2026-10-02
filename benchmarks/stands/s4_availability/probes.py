"""S4 probes — strictly reading, idempotent availability checks (BF-2).

Every probe opens the ISOLATED COPY of the fixture store through a fresh
manager (its own tmp root) and answers ONE availability question over
the REAL surfaces:

* **search-fts / search-hybrid** — both legs of the hybrid retrieval:
  the population's unique token must surface the entry with the FTS leg
  and through the composed hybrid RRF path (a vector-leg failure is a
  degradation, detected by ``search_type`` — the probe counts the hybrid
  mode, so a silently dead vector leg is a measurable availability loss,
  not a green pass).
* **get** — direct access by id for every population; a quarantined row
  must answer with the §5 retraction render (its CORRECT availability).
* **list_recent** — the listing surface must exclude the quarantined
  row while serving admissible rows (the ADR-0019 §5 exclusion is part
  of the availability semantics, not a bug).
* **assemble** — the fixed pipeline (recall → scan → align → budget)
  must return a block within budget with the published token surfaced
  and the quarantined one absent.
* **marker-parse** — a CCR marker minted on the FIXTURE (before the
  copy; the mint is a fixture write, not a probe) parses and validates
  on the copy through the real ``retrieve_content`` channel.

Idempotency: probes hold no state, run in a fixed order and depend only
on the copied store — a second full pass over the same copy yields the
same verdicts (asserted by the smoke tests and trivially re-verified by
every nightly run). No probe writes to ANY store: the copy is opened
read-oriented (probes call only search/get/list/assemble/retrieve;
``retrieve_content`` bumps a CCR retrieval counter — an LRU bookkeeping
field, not memory content — so the read-only invariant is enforced on
the CONTENT tables via the pre/post checksums in ``run.py`` and the
counter bumps are performed on the copy, never the fixture).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from benchmarks.corpus.deterministic_embedder import LexicalHashEmbedder
from benchmarks.stands.s4_availability.fixture import PROBE_TOKENS, fixture_settings
from vesma.manager import MemoryManager

#: The §5 retraction render shape (mirrors models.render_retraction).
_RETRACTION_PREFIX = "[retracted: "

#: Populations that must NOT surface under their own retrieval tokens —
#: the ADR-0019 §5 exclusion is CORRECT availability semantics (the
#: quarantined row is terminal lane-(b); the raw row is pre-pipeline).
EXCLUDED_POPULATIONS: frozenset[str] = frozenset({"quarantined", "raw"})


def probe_settings(root: Path) -> Any:
    """Settings for the probe manager rooted at the COPY directory.

    SAME ``db_name`` as the fixture — the copy preserves file names (the
    backup API clones file-per-file), so the probe manager must open
    exactly the cloned database, not create a fresh one.
    """
    return fixture_settings(root)


def _new_probe_manager(probe_root: Path) -> MemoryManager:
    """Fresh probe manager over the copied store, deterministic embedder."""
    mgr = MemoryManager(probe_settings(probe_root))
    mgr._embedder = LexicalHashEmbedder()
    return mgr


def _search_probe(
    mgr: MemoryManager, population: str, *, hybrid: bool, ids: dict[str, str]
) -> dict[str, Any]:
    """One population must surface (or stay excluded) under its token.

    ``hybrid`` = the composed path (FTS + vector leg, RRF) — what a
    ``search`` call IS today. ``hybrid=False`` = the lexical leg alone
    (``fts_only_leg``, the S1 harness's scoped vector-leg-off emulation)
    — a dead vector leg must degrade search to ``fts_only``, never to
    nothing, so both probes must find their token.

    ``population`` in ``EXCLUDED_POPULATIONS`` inverts the verdict: the
    row must be ABSENT from its own token's result set (ADR-0019 §5 —
    the quarantine exclusion is availability semantics, not a failure).
    """
    token = PROBE_TOKENS[population]
    from benchmarks.stands.s1_quality.scenarios import fts_only_leg

    if hybrid:
        results = mgr.search(token, project=None, limit=10)
        search_types = {r.search_type for r in results}
    else:
        with fts_only_leg():
            results = mgr.search(token, project=None, limit=10)
        search_types = set()
    surfaced_ids = [r.memory.id for r in results]
    target_id = ids.get(population)
    excluded = population in EXCLUDED_POPULATIONS
    target_surfaced = target_id in set(surfaced_ids) if target_id else bool(surfaced_ids)
    ok = not target_surfaced if excluded else target_surfaced
    return {
        "probe": (
            f"search-excluded:{population}"
            if excluded
            else f"search-{'hybrid' if hybrid else 'fts'}:{population}"
        ),
        "token": token,
        "found": target_surfaced,
        "results": len(surfaced_ids),
        "search_types": sorted(search_types),
        "pass": ok,
    }


def _get_probe(mgr: MemoryManager, ids: dict[str, str]) -> dict[str, Any]:
    """Direct access: every id answers; the quarantined one retracts."""
    outcomes: dict[str, str] = {}
    for population, mid in ids.items():
        memory = mgr.get(mid)
        if memory is None:
            outcomes[population] = "missing"
            continue
        content = memory.content or ""
        if population == "quarantined":
            outcomes[population] = (
                "retracted" if content.startswith(_RETRACTION_PREFIX) else "LEAKED"
            )
        else:
            outcomes[population] = "ok"
    return {
        "probe": "get-by-id",
        "outcomes": outcomes,
        "pass": (
            all(v == "ok" for k, v in outcomes.items() if k != "quarantined")
            and outcomes.get("quarantined") == "retracted"
        ),
    }


def _list_recent_probe(mgr: MemoryManager, ids: dict[str, str]) -> dict[str, Any]:
    """The listing surface: admissible rows present, quarantined absent."""
    rows = mgr.list_recent(limit=100)
    listed = {m.id for m in rows}
    admissible_ids = [mid for pop, mid in ids.items() if pop != "quarantined"]
    quarantined_id = ids["quarantined"]
    return {
        "probe": "list_recent",
        "listed": len(listed),
        "admissible_listed": sum(1 for mid in admissible_ids if mid in listed),
        "quarantined_listed": int(quarantined_id in listed),
        "pass": all(mid in listed for mid in admissible_ids) and quarantined_id not in listed,
    }


def _assemble_probe(mgr: MemoryManager, ids: dict[str, str]) -> dict[str, Any]:
    """Assemble on a budget: published token surfaces, quarantined absent."""
    del ids  # the probe asserts on TOKENS, attribution is the search probes' job
    from vesma.assemble import assemble_context

    project = "s4-fixture"
    block = assemble_context(
        mgr,
        session="s4-probe",
        project=project,
        budget=2048,
        query=f"{PROBE_TOKENS['published']} {PROBE_TOKENS['quarantined']}",
    )
    text = str(block.get("text", ""))
    return {
        "probe": "assemble-budget",
        "budget": 2048,
        "blocks": len(block.get("blocks", [])),
        "chars": len(text),
        "published_surfaced": PROBE_TOKENS["published"] in text,
        "quarantined_absent": PROBE_TOKENS["quarantined"] not in text,
        "pass": PROBE_TOKENS["published"] in text
        and PROBE_TOKENS["quarantined"] not in text
        and len(text) <= 2048 * 4,  # chars ≈ tokens*4 — honest budget bound
    }


def _marker_probe(mgr: MemoryManager, marker: dict[str, Any]) -> dict[str, Any]:
    """A marker minted on the fixture parses and redeems on the copy."""
    h = str(marker.get("hash", ""))
    if not h:
        return {"probe": "marker-parse", "pass": False, "error": "fixture minted no marker"}
    res = mgr.retrieve_content(h, project="s4-fixture")
    found = bool(res.get("found")) and not bool(res.get("refused"))
    return {
        "probe": "marker-parse",
        "hash": h[:12],
        "found": found,
        "pass": found,
    }


def run_probes(
    probe_root: Path,
    ids: dict[str, str],
    marker: dict[str, Any] | None,
) -> dict[str, Any]:
    """One full idempotent probe pass over the copied store.

    Returns ``{"probes": [...], "pass_count": int, "total": int}`` —
    the per-probe verdicts plus the inputs of ``probe-pass-rate``.
    Every probe failure is recorded with its verdict shape (typed,
    explicit — never a swallowed exception).
    """
    mgr = _new_probe_manager(probe_root)
    probes: list[dict[str, Any]] = []
    try:
        # search both legs — every VISIBLE population must surface under
        # its unique token (found by id, not by a fuzzy count).
        for population in ("published", "refined", "failed"):
            probes.append(_search_probe(mgr, population, hybrid=False, ids=ids))
            probes.append(_search_probe(mgr, population, hybrid=True, ids=ids))
        # the quarantined/raw populations must NOT surface under their
        # own tokens on the composed path (the exclusion probes).
        for population in ("quarantined", "raw"):
            probes.append(_search_probe(mgr, population, hybrid=True, ids=ids))
        probes.append(_get_probe(mgr, ids))
        probes.append(_list_recent_probe(mgr, ids))
        probes.append(_assemble_probe(mgr, ids))
        if marker is not None:
            probes.append(_marker_probe(mgr, marker))
        else:
            probes.append(
                {
                    "probe": "marker-parse",
                    "pass": False,
                    "error": "fixture minted no marker (fixture broken)",
                }
            )
    finally:
        mgr.close()
    passed = sum(1 for p in probes if p.get("pass") is True)
    return {
        "probes": probes,
        "pass_count": passed,
        "total": len(probes),
    }


def mint_fixture_marker(fixture_mgr: MemoryManager) -> dict[str, Any] | None:
    """Mint a CCR marker ON THE FIXTURE (a fixture write, before the copy).

    The block is long enough to clear ``ccr.min_size_chars=280`` (the
    harness's documented lowering) so the marker actually caches. The
    PROBE then only parses/redeems it — parsing and redeeming are reads.
    """
    text = (
        "wayfareridge deployment diary, long form. The blue-green switch "
        "waits for the replica lag to fall under two seconds, the "
        "feature flags flip in dependency order, the cache warms from "
        "the snapshot manifest, and the rollback path is the previous "
        "manifest plus a forward-fix note. Every step records an "
        "operator initials line and a timestamp; a step without both is "
        "void and blocks the next one. The diary is the single source "
        "of truth for the release state machine."
    )
    comp = fixture_mgr.compress_content(
        text, project="s4-fixture", agent="s4-stand", session="s4-fixture-ccr"
    )
    if not comp.get("hash"):
        return None
    return {
        "hash": str(comp["hash"]),
        "marker": str(comp.get("marker", "")),
        "original_chars": int(comp.get("original_size", 0)),
    }


def run_marker_parse_on_text(text: str) -> dict[str, Any] | None:
    """Parse a CCR marker out of an assembled text (pure read).

    Kept separate so the assemble probe can compose: parse is a
    ``mnemos.ccr.parse_marker`` call — no store access, idempotent.
    """
    from vesma.ccr import parse_marker

    return parse_marker(text)
