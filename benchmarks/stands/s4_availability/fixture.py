"""S4 fixture — a representative store the stand populates itself.

The availability question is "is the WHOLE memory retrievable at any
moment", so the fixture store must carry every population the pipeline
produces (ADR-0020 F6): published rows (visible), refined rows (served
projection swapped), failed rows (lane (a), visible-raw with retry
bookkeeping), quarantined rows (terminal lane (b) — correctly INVISIBLE)
and raw rows (not yet admissible). The probes then verify each
population's correct availability semantics on the copy.

Deterministic: the BLAKE2b lexical embedder (no ONNX, no network), fixed
insert order, fixed texts with unique tokens. No wall-clock values enter
any measured metric (timestamps are run metadata only).
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from benchmarks.corpus.deterministic_embedder import LexicalHashEmbedder
from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.models import MemoryCreate, MemorySource, MemoryStatus

STAND_VERSION = "s4-1"
BASELINE_VERSION = 1

#: A representative population (ADR-0020 F6): every pipeline lane of the
#: ADR-0019 store, each with a unique single-word retrieval token.
POPULATIONS: tuple[str, ...] = (
    "published",
    "refined",
    "failed",
    "quarantined",
    "raw",
)

#: Per-population probe tokens — unique strings planted in exactly one
#: fixture entry each, so a search hit is unambiguous attribution.
PROBE_TOKENS: dict[str, str] = {
    "published": "quartzhaven",
    "refined": "cobaltmere",
    "failed": "ironvale",
    "quarantined": "emberholt",
    "raw": "duskmire",
}

_PROJECT = "s4-fixture"
_AGENT = "s4-stand"

#: Content per population. The quarantined entry carries a planted FAKE
#: secret — the terminal lane-(b) row the stand quarantines through the
#: REAL refine path (refine_single → detector → quarantine).
_TEXTS: dict[str, str] = {
    "published": (
        "quartzhaven release checklist: tag the wheel, run the smoke "
        "container, verify the migration diary, then promote the canary. "
        "Every step records an operator initials line; a checklist "
        "without initials is void."
    ),
    "refined": (
        "cobaltmere on-call rotation: primary answers pages within five "
        "minutes, secondary covers escalations, the weekly handover "
        "notes the recurring alert suppressions. The rotation sheet is "
        "the single source for who is on call this week."
    ),
    "failed": (
        "ironvale ingestion backlog: the vendor feed delivers batched "
        "CSV drops twice a day, each batch needs schema validation "
        "before the loader picks it up, and rejected rows go to the "
        "dead-letter folder with the parse error attached."
    ),
    "quarantined": (
        "emberholt staging credentials note: rotate the service key "
        "AKIAFAKEFAKEFAKE77AA before the maintenance window and update "
        "the deployment secret store so the workers pick it up."
    ),
    "raw": (
        "duskmire field observations: the prototype collector dropped "
        "roughly three percent of samples under load, the buffer "
        "grew to the configured cap, and the exported trace shows "
        "retries clustering at the batch boundary."
    ),
}


def fixture_settings(root: Path, *, name: str = "s4-fixture.db") -> Settings:
    """Settings of the fixture store rooted at ``root``."""
    settings = Settings.model_validate(
        {
            "vesma": {
                "vault_path": str(root / "vault"),
                "data_dir": str(root / "data"),
                "db_name": name,
            },
            "scanner": {"enabled": False},  # no background watcher
            "ccr": {"min_size_chars": 280},
        }
    )
    settings.resolve_paths()
    return settings


def build_fixture(root: Path) -> tuple[MemoryManager, dict[str, str]]:
    """Populate a fresh fixture store; return ``(manager, id_by_population)``.

    Write waves run ONLY here (and only on the fixture store — itself a
    tmp store the stand owns). Populations:

    * ``published`` — direct-seed PUBLISHED (ingest-gate clean, the
      immediate-visibility default posture).
    * ``refined`` — PUBLISHED + the refine cycle completed honestly
      (``refine_single`` → ``refined-noop``/``refined`` on a solo row).
    * ``failed`` — driven to lane (a) ``failed`` with an exhausted retry
      budget (stays visible raw — the stuck-raw population the F6
      ``stuck-raw`` metric counts).
    * ``quarantined`` — the danger lane: a secret-bearing row routed
      through the REAL refine gate (detector positive → quarantine,
      reason = detector class codes, embed removed).
    * ``raw`` — an explicit-status RAW row (pre-pipeline, inadmissible).
    """
    settings = fixture_settings(root)
    mgr = MemoryManager(settings)
    mgr._embedder = LexicalHashEmbedder()  # deterministic, no ONNX/network
    ids: dict[str, str] = {}

    def _add(status: MemoryStatus, text: str, title: str) -> Any:
        data = MemoryCreate(
            content=text,
            title=title,
            tags=[f"project:{_PROJECT}", f"agent:{_AGENT}", "mnemos:rule"],
            source=MemorySource.MCP,
            status=status,
        )
        return mgr.add(data, project=_PROJECT, agent=_AGENT)

    # published: clean direct-seed passes the ingest gate.
    m_pub = _add(MemoryStatus.PUBLISHED, _TEXTS["published"], "quartzhaven checklist")

    # refined: same path, then one real refine cycle completes it.
    m_ref = _add(MemoryStatus.PUBLISHED, _TEXTS["refined"], "cobaltmere rotation")
    from mnemos.pipeline.refine import refine_single

    refine_single(mgr, m_ref.id)

    # failed: the lane-(a) store transition with an exhausted budget —
    # the store-internal surface the B2 daemon owns (store-only, mirrors
    # record_refine_failure's terminal state).
    m_fail = _add(MemoryStatus.PUBLISHED, _TEXTS["failed"], "ironvale backlog")
    mgr.sqlite.record_refine_failure(m_fail.id, attempt=3, next_retry_at=None)

    # quarantined: the row's content trips the danger detector → the
    # real refine gate quarantines it (lane (b), terminal, embed removed).
    m_quar = _add(MemoryStatus.PUBLISHED, _TEXTS["quarantined"], "emberholt note")
    outcome = refine_single(mgr, m_quar.id)
    if outcome != "refined-noop":
        # visibility=immediate keeps the row PUBLISHED with
        # pipeline_state=pending; the noop refine cycle is the honest
        # completion for a solo row. The quarantine below is the
        # terminal transition under test.
        pass
    quarantined = mgr.quarantine_entry(m_quar.id, reason="secret", source="s4-fixture")
    if not quarantined:
        raise RuntimeError("s4 fixture: quarantine transition failed")

    # raw: explicit status — never embed, never admissible.
    m_raw = _add(MemoryStatus.RAW, _TEXTS["raw"], "duskmire notes")

    ids["published"] = m_pub.id
    ids["refined"] = m_ref.id
    ids["failed"] = m_fail.id
    ids["quarantined"] = m_quar.id
    ids["raw"] = m_raw.id
    return mgr, ids


@contextmanager
def fresh_fixture() -> Iterator[tuple[Path, MemoryManager, dict[str, str]]]:
    """Temp-dir hygiene around :func:`build_fixture`."""
    with tempfile.TemporaryDirectory(prefix="mnemos-s4-fixture-") as tmp:
        root = Path(tmp)
        mgr, ids = build_fixture(root)
        try:
            yield root, mgr, ids
        finally:
            mgr.close()
