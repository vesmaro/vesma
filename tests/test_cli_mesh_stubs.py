"""serve/fetch mesh-leg resilience + wheel stub vendoring (cli-audit 2026-10-08, findings #2/#3).

* bare `vesma fetch` must be a clean usage error even when the gRPC
  stubs are absent (the mesh imports moved BELOW the argument guards);
* `vesma serve` on a mesh-enabled config must DEGRADE to HTTP-only
  when the mesh leg fails to start, not crash before the HTTP bind;
* the wheel vendors the generated stubs (vesma/_mesh_gen_stubs) and
  the shim resolves them as candidate 0.
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
        f"  db_name: cli-mesh-stubs.db\n"
        f"embedding:\n"
        f"  provider: nano\n"
    )
    monkeypatch.setenv("VESMA_CONFIG", str(cfg))
    yield cfg
    reset_manager()


# ── fetch ─────────────────────────────────────────────────────────────────────


def test_fetch_no_args_is_clean_usage_error(isolated_config: Path) -> None:
    """Bare `vesma fetch` → clean usage error, no traceback, no ImportError."""
    result = runner.invoke(app, ["fetch"])
    assert result.exit_code == 1
    assert "no --id given" in result.output
    assert "Traceback" not in result.output
    assert "generated stubs" not in result.output


def test_fetch_with_id_and_missing_stubs_is_actionable(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With --id but broken mesh imports: the fix ladder, not a traceback."""
    import sys

    isolated_config.write_text(
        f"vesma:\n"
        f"  vault_path: {isolated_config.parent / 'vault'}\n"
        f"  data_dir: {isolated_config.parent / 'data'}\n"
        f"  db_name: cli-mesh-stubs.db\n"
        f"embedding:\n"
        f"  provider: nano\n"
        f"federation:\n"
        f"  fetch:\n"
        f"    mesh_config_path: {isolated_config.parent / 'mesh.yaml'}\n"
    )
    # None in sys.modules = "import of lazy_fetch halted" ImportError —
    # the same failure shape a stub-less wheel produces when the mesh
    # shim cannot resolve the generated modules.
    monkeypatch.setitem(sys.modules, "vesma.lazy_fetch", None)
    result = runner.invoke(app, ["fetch", "--id", "fed:agent:uuid"])
    assert result.exit_code == 1
    assert "mesh federation is unavailable" in result.output
    assert "Traceback" not in result.output


# ── serve ─────────────────────────────────────────────────────────────────────


def test_serve_degrades_to_http_only_when_mesh_leg_fails(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """serve with a failing mesh leg: WARN + HTTP bind, not a crash."""
    cfg = isolated_config
    cfg.write_text(
        f"vesma:\n"
        f"  vault_path: {isolated_config.parent / 'vault'}\n"
        f"  data_dir: {isolated_config.parent / 'data'}\n"
        f"  db_name: cli-mesh-stubs.db\n"
        f"embedding:\n"
        f"  provider: nano\n"
        f"mesh:\n"
        f"  enabled: true\n"
        f"  socket_path: {isolated_config.parent / 'mesh.sock'}\n"
    )

    def _boom(*a: object, **k: object) -> object:
        raise RuntimeError("simulated mesh leg failure")

    monkeypatch.setattr("vesma.service.backend.start_mesh_legs", _boom)
    uvicorn_calls: list[dict[str, object]] = []

    class _FakeUvicorn:
        @staticmethod
        def run(*args: object, **kwargs: object) -> None:
            uvicorn_calls.append(kwargs)

    monkeypatch.setattr("uvicorn.run", _FakeUvicorn.run)
    result = runner.invoke(app, ["serve"])
    assert result.exit_code == 0, result.output
    assert "HTTP-only (degraded)" in result.output
    assert uvicorn_calls, "the HTTP server must still start"


# ── stub resolution (wheel vendoring, issue #514 tail) ────────────────────────


def test_mesh_gen_prefers_the_in_package_vendored_copy() -> None:
    """Candidate 0 is the in-package vesma/_mesh_gen_stubs dir (wheel vendoring)."""
    from vesma import _mesh_gen

    candidates = _mesh_gen._candidate_gen_dirs()
    assert candidates[0].name == "_mesh_gen_stubs"
    assert candidates[0].parent == Path(_mesh_gen.__file__).resolve().parent
    # The resolution itself must succeed in this environment (the gate runs
    # gen-proto.sh; the vendored copy exists in built wheels).
    assert _mesh_gen._GEN_DIR.is_dir()


def test_mesh_gen_missing_override_names_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bogus VESMA_MESH_GEN_DIR fails loud naming the variable (no fallback)."""
    from vesma import _mesh_gen

    monkeypatch.setenv(_mesh_gen.GEN_DIR_ENV_VAR, "/nonexistent/stubs-dir")
    with pytest.raises(ImportError, match="VESMA_MESH_GEN_DIR"):
        _mesh_gen._resolve_gen_dir()


def test_mesh_gen_no_candidates_message_names_the_fix_ladder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Without any candidate the message addresses the INSTALL, not a checkout."""
    import vesma._mesh_gen as mg

    monkeypatch.delenv(mg.GEN_DIR_ENV_VAR, raising=False)
    monkeypatch.setattr(
        mg, "_candidate_gen_dirs", lambda: (tmp_path / "a", tmp_path / "b", tmp_path / "c")
    )
    with pytest.raises(ImportError) as excinfo:
        mg._resolve_gen_dir()
    message = str(excinfo.value)
    assert "mesh federation is unavailable" in message
    assert "reinstall" in message  # the pip-user fix comes first
    assert "gen-proto.sh" in message  # the source-checkout fix is still named
