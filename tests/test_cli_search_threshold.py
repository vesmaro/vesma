"""`vesma search` relevance gate (cli-audit 2026-10-08, finding #9).

The fused RRF score is rank-based: even a garbage query surfaced the
WHOLE store at ~0.008 and "no results" was unreachable. The CLI now
gates on the RAW vector cosine: a row with no lexical (FTS) match must
clear the relevance floor to surface. The bundled nano embedder is
anisotropic (garbage ≈ 0.5, related ≈ 0.88 — measured 2026-10-09), so
the default floor 0.70 separates them; --threshold 0 disables the gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from vesma.cli.main import app

runner = CliRunner()


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point VESMA_CONFIG at an empty YAML so the CLI uses tmp_path."""
    from vesma.cli._manager import reset_manager

    reset_manager()
    cfg = tmp_path / "vesma.yaml"
    cfg.write_text(
        f"vesma:\n"
        f"  vault_path: {tmp_path / 'vault'}\n"
        f"  data_dir: {tmp_path / 'data'}\n"
        f"  db_name: cli-search-threshold.db\n"
        f"embedding:\n"
        f"  provider: nano\n"
    )
    monkeypatch.setenv("VESMA_CONFIG", str(cfg))
    yield cfg
    reset_manager()


def _seed(isolated_config: Path, content: str) -> None:
    """Save through mgr.add so the row lands in the VECTOR store too (the
    gate's raw cosines come from the vector leg — a bare sqlite.save row
    is invisible to it, which is not the production shape)."""
    from vesma.cli._manager import get_manager
    from vesma.models import MemoryCreate, MemorySource

    mgr = get_manager(str(isolated_config))
    mgr.add(
        MemoryCreate(
            content=content,
            tags=["project:proj", "agent:cli", "vesma:learning"],
            source=MemorySource.CLI,
        ),
        project="proj",
        agent="cli",
    )


def test_garbage_query_returns_no_relevant_results(isolated_config: Path) -> None:
    """A garbage query does NOT return the whole store (audit P1 #9)."""
    _seed(isolated_config, "vesma release train: tag, github release, ghcr image")
    result = runner.invoke(app, ["search", "zzzqqqxyzzy"])
    assert result.exit_code == 0, result.output
    assert "No relevant results" in result.output
    assert "threshold" in result.output
    assert "Score" not in result.output, "no results table may render for a garbage query"


def test_real_query_still_surfaces_lexical_match(isolated_config: Path) -> None:
    """A real query keeps its results (lexical evidence is never gated)."""
    _seed(isolated_config, "vesma release train: tag, github release, ghcr image")
    result = runner.invoke(app, ["search", "release train"])
    assert result.exit_code == 0, result.output
    assert "release train" in result.output.lower()
    assert "No relevant results" not in result.output


def test_threshold_zero_disables_gate(isolated_config: Path) -> None:
    """--threshold 0 restores the pure-ranking behavior (whole store back)."""
    _seed(isolated_config, "vesma release train: tag, github release, ghcr image")
    result = runner.invoke(app, ["search", "zzzqqqxyzzy", "--threshold", "0"])
    assert result.exit_code == 0, result.output
    assert "No relevant results" not in result.output
    assert "Score" in result.output, "the ungated table must render"


def test_explicit_threshold_admits_semantic_matches(isolated_config: Path) -> None:
    """--threshold below the garbage band lets semantic-only rows surface."""
    _seed(isolated_config, "vesma release train: tag, github release, ghcr image")
    result = runner.invoke(app, ["search", "zzzqqqxyzzy", "--threshold", "0.1"])
    assert result.exit_code == 0, result.output
    assert "Score" in result.output, "a low floor must admit the semantic-only rows"


def test_manager_marks_semantic_only_rows_with_vector_score(isolated_config: Path) -> None:
    """SearchResult.vector_score: set for semantic-only rows, None for lexical."""
    from vesma.cli._manager import get_manager

    _seed(isolated_config, "vesma release train: tag, github release, ghcr image")
    mgr = get_manager(str(isolated_config))

    garbage = mgr.search("zzzqqqxyzzy", limit=10)
    assert garbage, "the vector leg must still rank the store"
    assert all(r.vector_score is not None for r in garbage), (
        "every garbage-query row is semantic-only and must carry its raw cosine"
    )

    lexical = mgr.search("release train", limit=10)
    assert lexical
    assert all(r.vector_score is None for r in lexical), (
        "lexically-corroborated rows carry None (the gate's keep-marker)"
    )


# ── cascade fix 2026-10-09 (P2): the three-state gate contract ────────────────
#
# min_relevance was overloaded: 0 meant "unset" AND was the documented
# machine-wide OFF switch, but the CLI resolve silently fell back to the
# 0.70 built-in floor — an operator's `min_relevance: 0` re-armed the very
# gate it was meant to turn off. The contract now: min_relevance is a PURE
# threshold (0 = no explicit floor → built-in default while the gate is
# on), and machine-wide OFF is the explicit `search.cli_relevance_gate:
# false` key.


def _add_search_section(isolated_config: Path, search_yaml: str) -> None:
    """Overwrite the fixture config with a `search:` section BEFORE the CLI
    first instantiates the manager (the fixture only resets the singleton)."""
    body = isolated_config.read_text(encoding="utf-8")
    isolated_config.write_text(body + f"search:\n{search_yaml}", encoding="utf-8")


def test_config_gate_false_disables_gate(isolated_config: Path) -> None:
    """cli_relevance_gate: false → machine-wide OFF: a garbage query returns
    the whole store (the P2 case the 0-overload silently broke)."""
    _add_search_section(isolated_config, "  cli_relevance_gate: false\n")
    _seed(isolated_config, "vesma release train: tag, github release, ghcr image")
    result = runner.invoke(app, ["search", "zzzqqqxyzzy"])
    assert result.exit_code == 0, result.output
    assert "No relevant results" not in result.output
    assert "Score" in result.output, "the ungated table must render with the gate off"


def test_config_gate_false_ignores_min_relevance(isolated_config: Path) -> None:
    """The OFF switch wins over a configured threshold — no silent re-arm."""
    _add_search_section(isolated_config, "  cli_relevance_gate: false\n  min_relevance: 0.99\n")
    _seed(isolated_config, "vesma release train: tag, github release, ghcr image")
    result = runner.invoke(app, ["search", "zzzqqqxyzzy"])
    assert result.exit_code == 0, result.output
    assert "No relevant results" not in result.output
    assert "Score" in result.output


def test_config_gate_false_explicit_threshold_still_applies(isolated_config: Path) -> None:
    """An explicit --threshold is a per-call override — honored even when the
    machine-wide gate is off."""
    _add_search_section(isolated_config, "  cli_relevance_gate: false\n")
    _seed(isolated_config, "vesma release train: tag, github release, ghcr image")
    result = runner.invoke(app, ["search", "zzzqqqxyzzy", "--threshold", "0.99"])
    assert result.exit_code == 0, result.output
    assert "No relevant results" in result.output


def test_config_min_relevance_is_a_pure_threshold(isolated_config: Path) -> None:
    """A >0 min_relevance is applied verbatim: a 0.10 floor (below the nano
    garbage band ≈0.5) admits semantic-only rows instead of the 0.70 default."""
    _add_search_section(isolated_config, "  min_relevance: 0.1\n")
    _seed(isolated_config, "vesma release train: tag, github release, ghcr image")
    result = runner.invoke(app, ["search", "zzzqqqxyzzy"])
    assert result.exit_code == 0, result.output
    assert "No relevant results" not in result.output
    assert "Score" in result.output


def test_config_min_relevance_zero_is_unset_not_off(isolated_config: Path) -> None:
    """min_relevance: 0 with the gate ON keeps the built-in floor — documented
    contract: 0 means 'no explicit floor', machine-wide OFF is the gate key."""
    _add_search_section(isolated_config, "  min_relevance: 0.0\n")
    _seed(isolated_config, "vesma release train: tag, github release, ghcr image")
    result = runner.invoke(app, ["search", "zzzqqqxyzzy"])
    assert result.exit_code == 0, result.output
    assert "No relevant results" in result.output, (
        "0 must stay 'unset' (built-in floor), not a disabled gate"
    )


def test_config_high_min_relevance_gates_garbage(isolated_config: Path) -> None:
    """A high configured floor gates the garbage band like the default does."""
    _add_search_section(isolated_config, "  min_relevance: 0.99\n")
    _seed(isolated_config, "vesma release train: tag, github release, ghcr image")
    result = runner.invoke(app, ["search", "zzzqqqxyzzy"])
    assert result.exit_code == 0, result.output
    assert "No relevant results" in result.output
