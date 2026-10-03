"""Wave W-B (board card vesma-doctor-fixes-467) — doctor subcommand restructure.

Standing design rule: FLAGS DO NOT REPLACE SUBCOMMANDS.

* ``vesma doctor fix [--dry-run] [--json]`` — the repair path as a
  subcommand; the legacy ``--fix`` flag stays as a HIDDEN deprecated
  alias (identical behavior + a one-line stderr hint).
* ``vesma doctor paths [--json]`` — the paths overview as a subcommand;
  the legacy ``--paths`` flag is the same kind of hidden alias.
* ``--json`` stays a normal flag (output format) on the default run.
* The Integration check names the concrete missing/stale files
  (``target: path`` lines, capped at 5 + "…and N more").
* CheckStatus.SKIP is neutral for the exit code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vesma.cli.doctor import (
    CheckResult,
    CheckStatus,
    _cap_listing,
    _exit_code,
    doctor_app,
)
from vesma.cli.integration import (
    DeployStatus,
    FileResult,
    Target,
    TargetsConfig,
    VerifyResult,
)

runner = CliRunner()


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch home to tmp, create a minimal config, park the agents dir in tmp.

    Every filesystem write the fix path can perform (MCP registration,
    integration redeploy) lands inside ``tmp_path`` — the tests never
    touch the real home.
    """
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "vesma.cli.agent_wiring.DEFAULT_AGENTS_DIR", tmp_path / ".copilot" / "agents"
    )
    cfg = tmp_path / ".mnemos" / "config.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        f"vesma:\n"
        f"  vault_path: {tmp_path / '.mnemos' / 'vault'}\n"
        f"  data_dir: {tmp_path / '.mnemos' / 'data'}\n"
        f"embedding:\n"
        f"  provider: nano\n",
        encoding="utf-8",
    )
    return tmp_path


# ── fix subcommand ────────────────────────────────────────────────────────────


def test_fix_subcommand_dry_run_runs(isolated_home: Path) -> None:
    """``doctor fix --dry-run`` previews fixes without executing."""
    result = runner.invoke(doctor_app, ["fix", "--dry-run"])
    assert result.exit_code in (0, 1, 2), result.output
    assert "Preview of auto-fixes" in result.output
    assert "would run:" in result.output
    assert "fixing..." not in result.output


def test_fix_subcommand_real_run_runs(isolated_home: Path) -> None:
    """``doctor fix`` executes the fix path (writes stay in the tmp home)."""
    result = runner.invoke(doctor_app, ["fix"])
    assert result.exit_code in (0, 1, 2), result.output
    assert "Traceback" not in result.output


def test_fix_subcommand_json_payload(isolated_home: Path) -> None:
    """``doctor fix --json`` carries the fix bookkeeping keys.

    The dry-run preview lines precede the JSON document on stdout
    (pre-existing `--fix --json` shape) — parse from the first ``{``.
    """
    result = runner.invoke(doctor_app, ["fix", "--dry-run", "--json"])
    assert result.exit_code in (0, 1, 2), result.output
    payload = json.loads(result.output[result.output.index("{") :])
    assert "checks" in payload
    assert "fixed" in payload
    assert "fix_skipped" in payload
    assert payload["dry_run"] is True


# ── paths subcommand ──────────────────────────────────────────────────────────


def test_paths_subcommand_exits_zero(isolated_home: Path) -> None:
    """``doctor paths`` exits 0 and shows the paths table."""
    result = runner.invoke(doctor_app, ["paths"])
    assert result.exit_code == 0
    assert "Paths" in result.output
    assert "Root" in result.output


def test_paths_subcommand_json(isolated_home: Path) -> None:
    """``doctor paths --json`` emits a paths object."""
    result = runner.invoke(doctor_app, ["paths", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert "root" in payload["paths"]
    assert "db_path" in payload["paths"]


def test_paths_subcommand_runs_no_health_checks(isolated_home: Path) -> None:
    """``doctor paths`` prints no health-check table."""
    result = runner.invoke(doctor_app, ["paths"])
    assert "Vesma Health Check" not in result.output


# ── hidden deprecated flag aliases ────────────────────────────────────────────


def test_fix_flag_alias_still_works_with_hint(isolated_home: Path) -> None:
    """``doctor --fix --dry-run`` behaves like the subcommand + stderr hint."""
    result = runner.invoke(doctor_app, ["--fix", "--dry-run"])
    assert result.exit_code in (0, 1, 2), result.output
    assert "Preview of auto-fixes" in result.output
    assert "would run:" in result.output
    assert "use: vesma doctor fix" in result.stderr


def test_paths_flag_alias_still_works_with_hint(isolated_home: Path) -> None:
    """``doctor --paths`` behaves like the subcommand + stderr hint."""
    result = runner.invoke(doctor_app, ["--paths"])
    assert result.exit_code == 0
    assert "Root" in result.output
    assert "use: vesma doctor paths" in result.stderr


def test_deprecated_flags_are_hidden_from_help(isolated_home: Path) -> None:
    """The legacy flag forms no longer appear in ``doctor --help``."""
    result = runner.invoke(doctor_app, ["--help"])
    assert result.exit_code == 0
    for legacy in ("--fix", "--paths"):
        assert legacy not in result.output, f"{legacy} must be hidden"


def test_help_lists_the_subcommands(isolated_home: Path) -> None:
    """``doctor --help`` advertises the fix/paths subcommands."""
    result = runner.invoke(doctor_app, ["--help"])
    assert "fix" in result.output
    assert "paths" in result.output


def test_json_flag_remains_a_normal_flag(isolated_home: Path) -> None:
    """``doctor --json`` (default check run) still emits the checks array."""
    result = runner.invoke(doctor_app, ["--json"])
    assert result.exit_code in (0, 1, 2), result.output
    payload = json.loads(result.output)
    assert "checks" in payload
    assert "paths" in payload
    # The plain check run carries no fix bookkeeping.
    assert "fixed" not in payload


def test_options_before_subcommand_are_not_silently_dropped(isolated_home: Path) -> None:
    """``doctor --json fix`` warns on stderr instead of dropping the option."""
    result = runner.invoke(doctor_app, ["--json", "paths"])
    assert result.exit_code == 0
    assert "options placed before the subcommand are ignored" in result.stderr


# ── Integration check: missing/stale file listing ─────────────────────────────


def _stub_integration(monkeypatch: pytest.MonkeyPatch, files: list[FileResult]) -> None:
    """Force one detected target whose verify() returns the given files."""
    from vesma.cli import integration as integ

    class _AlwaysDetectedTarget(Target):
        def is_detected(self) -> bool:
            return True

    detected = _AlwaysDetectedTarget(
        name="cursor",
        detect_paths=(Path("/tmp/definitely-absent-cursor-marker"),),
        deploy_map={"instructions": Path("/tmp/definitely-absent")},
    )
    cfg = TargetsConfig(targets=(detected,))

    class _FakeMgr:
        def __init__(self, version: str, **_kw: object) -> None:
            self.version = version

        def verify(self, target_name: str) -> VerifyResult:
            return VerifyResult(target_name=target_name, files=list(files))

    monkeypatch.setattr(integ, "load_targets", lambda config_path=None, home=None: cfg)
    monkeypatch.setattr(integ, "IntegrationManager", _FakeMgr)


def test_integration_check_lists_missing_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing files appear as ``target: path`` lines, not just a count."""
    from vesma.cli.doctor import _check_integration

    files = [
        FileResult(
            source=Path("pack/a.md"),
            destination=tmp_path / f"missing{i}.md",
            status=DeployStatus.MISSING,
        )
        for i in range(3)
    ]
    _stub_integration(monkeypatch, files)

    result = _check_integration()
    assert result.status == CheckStatus.WARN
    assert "3 missing file(s)" in result.detail
    for i in range(3):
        assert f"cursor: {tmp_path / f'missing{i}.md'}" in result.detail


def test_integration_listing_caps_at_five_with_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """More than 5 missing files → first 5 + '…and N more'."""
    from vesma.cli.doctor import _check_integration

    files = [
        FileResult(
            source=Path("pack/a.md"),
            destination=tmp_path / f"missing{i}.md",
            status=DeployStatus.MISSING,
        )
        for i in range(7)
    ]
    _stub_integration(monkeypatch, files)

    result = _check_integration()
    for i in range(5):
        assert f"missing{i}.md" in result.detail
    assert "missing5.md" not in result.detail
    assert "…and 2 more" in result.detail


def test_integration_check_lists_stale_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale files are listed the same way when nothing is missing."""
    from vesma.cli.doctor import _check_integration

    files = [
        FileResult(
            source=Path("pack/a.md"),
            destination=tmp_path / f"stale{i}.md",
            status=DeployStatus.STALE,
        )
        for i in range(2)
    ]
    _stub_integration(monkeypatch, files)

    result = _check_integration()
    assert result.status == CheckStatus.WARN
    assert "2 stale" in result.detail
    assert f"cursor: {tmp_path / 'stale0.md'}" in result.detail


def test_cap_listing_pure_function() -> None:
    """_cap_listing: empty → '', ≤cap → all, over cap → tail."""
    assert _cap_listing([]) == ""
    short = _cap_listing(["a", "b"])
    assert "a" in short and "b" in short and "more" not in short
    long = _cap_listing([f"l{i}" for i in range(8)], cap=5)
    assert "l4" in long and "l5" not in long and "…and 3 more" in long


# ── SKIP semantics ────────────────────────────────────────────────────────────


def test_skip_status_is_exit_code_neutral() -> None:
    """SKIP contributes nothing to the exit code (like PASS)."""
    skip = CheckResult("x", CheckStatus.SKIP, "n/a")
    assert _exit_code([skip]) == 0
    assert _exit_code([skip, CheckResult("y", CheckStatus.WARN, "")]) == 2
    assert _exit_code([skip, CheckResult("y", CheckStatus.FAIL, "")]) == 1
