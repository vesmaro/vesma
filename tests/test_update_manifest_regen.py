"""P0 (cli-audit 2026-10-08): `update apply` reconciles installed manifests.

After an engine upgrade the installed components.d manifests stayed on
the old era — the next `vesma service run` died fail-closed with
[REQUIREMENTS_INVALID] and any supervisor restart meant downtime. The
updater now detects stale bundled manifests (board/metrics), regenerates
them from THIS engine's bundle, leaves operator-authored manifests
(mesh.yaml) untouched with a structure WARN, prints the
"regenerated for <version> — restart vesma.service" line, and refuses
exit 0 when the installation still fails fail-closed validation.
"""

from __future__ import annotations

from collections import namedtuple
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from vesma.cli.update_cmd import update_app
from vesma.service.install import bundled_manifest_path
from vesma.service.install import regenerate_stale_manifests as regen

runner = CliRunner()


@pytest.fixture
def components_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated XDG config home with a components.d directory."""
    cfg_home = tmp_path / "xdg-config"
    components = cfg_home / "vesma" / "components.d"
    components.mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    return components


def _stale_metrics_manifest(components: Path) -> Path:
    """Write an OLD-ERA metrics manifest: the bundle with the requirements
    section stripped — exactly the shape `service run` refuses with
    REQUIREMENTS_INVALID (launch.argv references {venv_bin}, the
    requirements section is gone)."""
    bundle = bundled_manifest_path("metrics").read_text(encoding="utf-8")
    lines = [
        line
        for line in bundle.splitlines(keepends=True)
        if not line.strip().startswith(("requirements:", '- "vesma=='))
    ]
    stale = components / "metrics.yaml"
    stale.write_text("".join(lines), encoding="utf-8")
    return stale


# ── unit: regenerate_stale_manifests ──────────────────────────────────────────


def test_stale_bundled_manifest_is_regenerated(components_home: Path) -> None:
    """An old-era metrics.yaml is replaced by THIS engine's bundle."""
    stale = _stale_metrics_manifest(components_home)
    assert "requirements:" not in stale.read_text(encoding="utf-8")

    lines, regenerated = regen()

    assert regenerated >= 1
    assert any("regenerated" in line for line in lines)
    assert stale.read_text(encoding="utf-8") == bundled_manifest_path("metrics").read_text(
        encoding="utf-8"
    ), "the installed manifest must be byte-identical to the new bundle"


def test_current_bundled_manifest_is_left_alone(components_home: Path) -> None:
    """An up-to-date bundle manifest is not rewritten (idempotent)."""
    target = components_home / "board.yaml"
    target.write_text(bundled_manifest_path("board").read_text(encoding="utf-8"), encoding="utf-8")
    before_mtime = target.stat().st_mtime_ns

    _lines, regenerated = regen()

    assert regenerated == 0
    assert target.stat().st_mtime_ns == before_mtime


def test_operator_authored_manifest_is_preserved_but_warned(components_home: Path) -> None:
    """mesh.yaml stays byte-identical; a broken structure gets a loud WARN."""
    operator = components_home / "mesh.yaml"
    operator.write_text("launch:\n  argv: ['sh', '-c', 'echo hi']\n", encoding="utf-8")

    lines, _regenerated = regen()

    assert operator.read_text(encoding="utf-8") == operator.read_text(encoding="utf-8")
    assert any("mesh.yaml" in line and "WARN" in line for line in lines), lines


def test_missing_components_dir_is_a_no_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No components.d → nothing to reconcile, no error."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-xdg"))
    lines, regenerated = regen()
    assert lines == []
    assert regenerated == 0


def test_absent_bundled_manifest_is_not_resurrected(components_home: Path) -> None:
    """A deliberately uninstalled component is not re-added by the updater."""
    _stale_metrics_manifest(components_home)  # metrics.yaml: present, stale
    # board.yaml: ABSENT (operator uninstalled it) — the updater must not
    # resurrect it; only the stale metrics.yaml is reconciled.

    lines, regenerated = regen()

    names = {p.name for p in components_home.glob("*.yaml")}
    assert "board.yaml" not in names, "an uninstalled component must not be resurrected"
    assert regenerated == 1
    assert any("metrics.yaml" in line for line in lines)


# ── CLI: `vesma update apply` runs the reconciliation ─────────────────────────


def _fake_update_env(monkeypatch: pytest.MonkeyPatch, version: str) -> None:
    """Stub the pip/npm legs of update apply: pip succeeds, npm absent."""
    import vesma.cli.update_cmd as uc

    Detected = namedtuple("Detected", ["dist", "version"])
    monkeypatch.setattr(uc, "detect_installed_dist", lambda: Detected("vesma", "6.0.0"))
    monkeypatch.setattr(uc, "fetch_latest", lambda dist: version)
    monkeypatch.setattr(
        uc,
        "_run_cmd",
        lambda cmd, timeout=900: CompletedProcess(args=cmd, returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(uc.shutil, "which", lambda name: None)  # no npm leg
    monkeypatch.setattr(uc, "_append_history", lambda **kwargs: None)
    monkeypatch.setenv("VESMA_UPDATES_CHECK", "off")


def test_update_apply_reconciles_stale_manifests(
    components_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: stale metrics.yaml + update apply → regenerated + restart hint."""
    stale = _stale_metrics_manifest(components_home)
    _fake_update_env(monkeypatch, "6.0.1")

    result = runner.invoke(update_app, ["apply"])

    assert result.exit_code == 0, result.output
    assert "service manifests regenerated" in result.output
    assert "restart vesma.service" in result.output
    assert stale.read_text(encoding="utf-8") == bundled_manifest_path("metrics").read_text(
        encoding="utf-8"
    )


def test_update_apply_prints_current_when_nothing_stale(
    components_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Current manifests → the 'current for <version>' line, no rewrite."""
    (components_home / "board.yaml").write_text(
        bundled_manifest_path("board").read_text(encoding="utf-8"), encoding="utf-8"
    )
    _fake_update_env(monkeypatch, "6.0.1")

    result = runner.invoke(update_app, ["apply"])

    assert result.exit_code == 0, result.output
    assert "service manifests current" in result.output
    assert "regenerated for" not in result.output


def test_update_apply_failure_to_reconcile_is_loud(
    components_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reconciliation crash is exit 1 with the manual remediation, not silent."""
    _fake_update_env(monkeypatch, "6.0.1")

    with patch(
        "vesma.service.install.regenerate_stale_manifests",
        side_effect=OSError("disk gone"),
    ):
        result = runner.invoke(update_app, ["apply"])

    assert result.exit_code == 1
    assert "manifest reconciliation failed" in result.output
    assert "vesma service install" in result.output
