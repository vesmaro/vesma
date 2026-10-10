"""Tests for the update check + ``vesma self-update`` (issue #445).

NO REAL NETWORK anywhere in this file: the HTTP fetch is always injected
(``fetcher=``) or monkeypatched; dist detection and subprocesses are
monkeypatched at the module seam.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import typer
from typer.testing import CliRunner

from vesma import updates
from vesma.updates import UpdateInfo, check_for_update

runner = CliRunner()

updates_cli = pytest.importorskip("vesma.cli.update_cmd")

LATEST = "9.9.9"
INSTALLED = "5.1.1"


def _info(update_available: bool = True, latest: str = LATEST) -> UpdateInfo:
    return UpdateInfo(
        installed=INSTALLED,
        latest=latest,
        dist="vesma-memory-server",
        update_available=update_available,
        checked_at=datetime.now(UTC).isoformat(),
    )


@pytest.fixture
def cache_file(tmp_path: Path) -> Path:
    return tmp_path / "update-check.json"


@pytest.fixture(autouse=True)
def deterministic_dist(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin dist detection so tests never depend on the dev env's metadata."""

    def fake_version(name: str) -> str:
        if name == "vesma-memory-server":
            return INSTALLED
        from importlib.metadata import PackageNotFoundError

        raise PackageNotFoundError(name)

    monkeypatch.setattr(updates, "_md_version", fake_version)


@pytest.fixture(autouse=True)
def clear_suite_opt_out_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo the conftest suite-wide kill switch for THIS file.

    Every test here injects its fetcher (no network by construction), and
    the opt-out behaviour tests set ``VESMA_UPDATES_CHECK`` explicitly —
    the guard would otherwise short-circuit before those paths run.
    """
    monkeypatch.delenv("VESMA_UPDATES_CHECK", raising=False)


# ── core check: cache write / read / expiry ─────────────────────────────────


def test_check_fetches_and_writes_cache(cache_file: Path) -> None:
    calls: list[str] = []

    def fetcher(dist: str) -> str:
        calls.append(dist)
        return LATEST

    info = check_for_update(cache_path=cache_file, fetcher=fetcher)
    assert info is not None
    assert info.latest == LATEST
    assert info.installed == INSTALLED
    assert info.dist == "vesma-memory-server"
    assert info.update_available is True
    assert info.stale is False
    assert calls == ["vesma-memory-server"]
    payload = json.loads(cache_file.read_text())
    assert payload["ok"] is True
    assert payload["latest"] == LATEST


def test_fresh_cache_answers_without_refetch(cache_file: Path) -> None:
    def fetcher(dist: str) -> str:  # pragma: no cover — must never run
        raise AssertionError("fetcher called despite fresh cache")

    assert check_for_update(cache_path=cache_file, fetcher=lambda d: LATEST) is not None
    info = check_for_update(cache_path=cache_file, fetcher=fetcher)
    assert info is not None
    assert info.latest == LATEST
    assert info.stale is False


def test_expired_cache_refetches(cache_file: Path) -> None:
    calls: list[str] = []

    def fetcher(dist: str) -> str:
        calls.append(dist)
        return LATEST

    past = datetime.now(UTC) - timedelta(hours=25)
    assert check_for_update(cache_path=cache_file, fetcher=fetcher, now=past) is not None
    assert check_for_update(cache_path=cache_file, fetcher=fetcher) is not None
    assert len(calls) == 2


def test_no_update_when_latest_le_installed(cache_file: Path) -> None:
    info = check_for_update(cache_path=cache_file, fetcher=lambda d: "0.0.1")
    assert info is not None
    assert info.update_available is False


def _seed_cache(path: Path, *, latest: str, ok: bool, checked_at: datetime) -> None:
    """Hand-write a cache payload in the on-disk schema."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": 1,
                "checked_at": checked_at.isoformat(),
                "dist": "vesma-memory-server",
                "installed": "5.0.0",
                "latest": latest,
                "ok": ok,
            }
        ),
        encoding="utf-8",
    )


# ── self-upgrade drift: fresh cache older than the install (#460) ────────────


def test_fresh_positive_cache_with_installed_newer_refetches(
    cache_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updates, "_md_version", lambda name: "5.1.2")
    _seed_cache(cache_file, latest="5.1.0", ok=True, checked_at=datetime.now(UTC))
    calls: list[str] = []

    def fetcher(dist: str) -> str:
        calls.append(dist)
        return "5.1.2"

    info = check_for_update(cache_path=cache_file, fetcher=fetcher)
    assert info is not None
    assert info.latest == "5.1.2"
    assert info.update_available is False
    assert info.stale is False
    assert calls == ["vesma-memory-server"], "stale-drift cache must re-check once"
    payload = json.loads(cache_file.read_text())
    assert payload["latest"] == "5.1.2"
    assert payload["ok"] is True

    def never(dist: str) -> str:  # pragma: no cover — must never run
        raise AssertionError("fetcher called despite refreshed cache")

    info2 = check_for_update(cache_path=cache_file, fetcher=never)
    assert info2 is not None
    assert info2.latest == "5.1.2"


def test_fresh_positive_cache_drift_refetch_failure_serves_stale(
    cache_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updates, "_md_version", lambda name: "5.1.2")
    _seed_cache(cache_file, latest="5.1.0", ok=True, checked_at=datetime.now(UTC))

    def fetcher(dist: str) -> str:
        raise OSError("no network")

    info = check_for_update(cache_path=cache_file, fetcher=fetcher)
    assert info is not None
    assert info.stale is True
    assert info.latest == "5.1.0"
    assert info.update_available is False
    payload = json.loads(cache_file.read_text())
    assert payload["ok"] is False
    assert payload["latest"] == "5.1.0"


def test_fresh_negative_cache_drift_serves_stale_without_fetch(
    cache_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updates, "_md_version", lambda name: "5.1.2")
    _seed_cache(cache_file, latest="5.1.0", ok=False, checked_at=datetime.now(UTC))

    def never(dist: str) -> str:  # pragma: no cover — must never run
        raise AssertionError("fresh negative cache must not be re-fetched")

    info = check_for_update(cache_path=cache_file, fetcher=never)
    assert info is not None
    assert info.stale is True
    assert info.latest == "5.1.0"


# ── offline behaviour ────────────────────────────────────────────────────────


def test_offline_no_cache_returns_none_and_writes_negative_cache(cache_file: Path) -> None:
    def fetcher(dist: str) -> str:
        raise OSError("no network")

    assert check_for_update(cache_path=cache_file, fetcher=fetcher) is None
    payload = json.loads(cache_file.read_text())
    assert payload["ok"] is False
    assert payload["latest"] is None


def test_offline_with_expired_cache_serves_stale(cache_file: Path) -> None:
    past = datetime.now(UTC) - timedelta(hours=25)
    first = check_for_update(cache_path=cache_file, fetcher=lambda d: LATEST, now=past)
    assert first is not None

    def fetcher(dist: str) -> str:
        raise OSError("no network")

    info = check_for_update(cache_path=cache_file, fetcher=fetcher)
    assert info is not None
    assert info.stale is True
    assert info.latest == LATEST


def test_negative_cache_bounds_offline_cost(cache_file: Path) -> None:
    calls: list[str] = []

    def fetcher(dist: str) -> str:
        calls.append(dist)
        raise OSError("no network")

    assert check_for_update(cache_path=cache_file, fetcher=fetcher) is None
    assert check_for_update(cache_path=cache_file, fetcher=fetcher) is None
    assert len(calls) == 1, "fresh negative cache must suppress the second fetch"


def test_corrupt_cache_is_ignored(cache_file: Path) -> None:
    cache_file.write_text("not json at all{")
    info = check_for_update(cache_path=cache_file, fetcher=lambda d: LATEST)
    assert info is not None
    assert info.update_available is True


# ── dist detection ───────────────────────────────────────────────────────────


def test_dist_detection_first_hit_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    from importlib.metadata import PackageNotFoundError

    seen: list[str] = []

    def fake_version(name: str) -> str:
        seen.append(name)
        if name == "vesma-memory-server":
            raise PackageNotFoundError(name)
        return "1.2.3"

    monkeypatch.setattr(updates, "_md_version", fake_version)
    assert updates.detect_installed_dist() == ("vesma", "1.2.3")
    assert seen == ["vesma-memory-server", "vesma"]


def test_dist_detection_none_when_nothing_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from importlib.metadata import PackageNotFoundError

    monkeypatch.setattr(
        updates, "_md_version", lambda name: (_ for _ in ()).throw(PackageNotFoundError(name))
    )
    assert updates.detect_installed_dist() is None
    assert check_for_update(cache_path=Path("/nonexistent/x.json")) is None


# ── opt-out ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["off", "0", "false", "no", "OFF"])
def test_env_opt_out_disables_check(
    cache_file: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("VESMA_UPDATES_CHECK", value)

    def fetcher(dist: str) -> str:  # pragma: no cover — must never run
        raise AssertionError("fetcher called while opted out")

    assert check_for_update(cache_path=cache_file, fetcher=fetcher) is None
    assert not cache_file.exists()


def test_config_knob_disables_check(monkeypatch: pytest.MonkeyPatch) -> None:
    from vesma.config import Settings

    monkeypatch.delenv("VESMA_UPDATES_CHECK", raising=False)  # clear the suite-wide guard
    settings = Settings()
    settings.updates.check_enabled = False

    def fetcher(dist: str) -> str:  # pragma: no cover — must never run
        raise AssertionError("fetcher called while config-disabled")

    assert (
        check_for_update(settings, cache_path=Path("/nonexistent/x.json"), fetcher=fetcher) is None
    )


# ── stats payload ────────────────────────────────────────────────────────────


@pytest.fixture
def isolated_manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    """A real MemoryManager over an isolated tmp config (as test_cli does)."""
    from vesma.cli._manager import get_manager, reset_manager

    cfg = tmp_path / "updates-test.yaml"
    cfg.write_text(
        f"vesma:\n"
        f"  vault_path: {tmp_path / 'vault'}\n"
        f"  data_dir: {tmp_path / 'data'}\n"
        f"  db_name: updates-test.db\n"
    )
    monkeypatch.setenv("VESMA_CONFIG", str(cfg))
    reset_manager()
    mgr = get_manager(str(cfg))
    yield mgr
    reset_manager()
    mgr.close()


def test_stats_payload_includes_update_available(
    isolated_manager,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setattr(updates, "check_for_update", lambda settings=None, **kw: _info())
    payload = isolated_manager.stats()
    assert payload["update_available"] is not None
    assert payload["update_available"]["latest"] == LATEST
    assert payload["update_available"]["update_available"] is True


def test_stats_payload_none_when_check_returns_none(
    isolated_manager,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    monkeypatch.setattr(updates, "check_for_update", lambda settings=None, **kw: None)
    assert isolated_manager.stats()["update_available"] is None


def test_stats_payload_none_when_config_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,  # type: ignore[no-untyped-def]
) -> None:
    from vesma.cli._manager import get_manager, reset_manager

    cfg = tmp_path / "updates-off.yaml"
    cfg.write_text(
        f"vesma:\n"
        f"  vault_path: {tmp_path / 'vault'}\n"
        f"  data_dir: {tmp_path / 'data'}\n"
        f"updates:\n"
        f"  check_enabled: false\n"
    )
    monkeypatch.setenv("VESMA_CONFIG", str(cfg))
    monkeypatch.delenv("VESMA_UPDATES_CHECK", raising=False)  # clear the suite-wide guard
    reset_manager()
    try:
        mgr = get_manager(str(cfg))
        # Real check path: the config knob short-circuits BEFORE any
        # network/cache touch, so this is deterministic and offline-safe.
        assert mgr.stats()["update_available"] is None
        mgr.close()
    finally:
        reset_manager()


def test_update_stats_payload_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(settings=None, **kw):  # pragma: no cover — exercised via assert
        raise RuntimeError("boom")

    monkeypatch.setattr(updates, "check_for_update", boom)
    assert updates.update_stats_payload() is None


# ── CLI: --version hint ──────────────────────────────────────────────────────


def _invoke_version(monkeypatch: pytest.MonkeyPatch, info: UpdateInfo | None):  # type: ignore[no-untyped-def]
    from vesma.cli.main import app

    monkeypatch.setattr(updates, "check_for_update", lambda settings=None, **kw: info)
    return runner.invoke(app, ["--version"])


def test_version_hint_on_stderr_when_update_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _invoke_version(monkeypatch, _info(update_available=True))
    assert result.exit_code == 0
    assert "vesma" in result.output
    assert f"update available: {LATEST}" in result.stderr
    assert "vesma self-update --check" in result.stderr


def test_version_no_hint_when_up_to_date(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke_version(monkeypatch, _info(update_available=False))
    assert result.exit_code == 0
    assert "update available" not in result.stderr


def test_version_no_hint_when_check_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke_version(monkeypatch, None)
    assert result.exit_code == 0
    assert "update available" not in result.stderr


def test_version_silent_when_check_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VESMA_UPDATES_CHECK", "off")
    # With the env kill switch on, the real check answers None without any
    # network — run it UNPATCHED on purpose to prove the whole path.
    from vesma.cli.main import app

    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "update available" not in result.stderr


def test_version_hint_never_crashes_on_broken_check(monkeypatch: pytest.MonkeyPatch) -> None:
    from vesma.cli.main import app

    def boom(settings=None, **kw):  # pragma: no cover — exercised via assert
        raise RuntimeError("boom")

    monkeypatch.setattr(updates, "check_for_update", boom)
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "vesma" in result.output


# ── CLI: vesma self-update ────────────────────────────────────────────────────────


@pytest.fixture
def quiet_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HOME + no report-only surfaces, so output is deterministic."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(updates_cli, "_PROD_VENV_BASES", (tmp_path / "nonexistent-share",))
    monkeypatch.setattr("shutil.which", lambda name: None)
    return tmp_path


def _invoke_update(args: list[str], monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    from vesma.cli.main import app

    return runner.invoke(app, ["self-update", *args])


def test_update_check_output_shape(quiet_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    monkeypatch.setattr(updates_cli, "check_for_update", lambda settings=None, **kw: _info())
    monkeypatch.setattr(updates_cli, "family_latest", lambda: LATEST)
    result = _invoke_update([], monkeypatch)
    assert result.exit_code == 0
    assert "vesma" in result.output
    assert INSTALLED in result.output
    assert LATEST in result.output
    assert "UPDATE AVAILABLE" in result.output
    assert "npm not found" in result.output
    assert "never touched" in result.output


def test_update_check_pip_latest_unknown_offline(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    monkeypatch.setattr(updates_cli, "check_for_update", lambda settings=None, **kw: None)
    monkeypatch.setattr(updates_cli, "family_latest", lambda: None)
    result = _invoke_update(["--check"], monkeypatch)
    assert result.exit_code == 0
    assert "latest unknown" in result.output


def test_update_check_reports_prod_venv_and_go_binaries(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    share = quiet_home / ".local" / "share"
    (share / "mnemos-prod").mkdir(parents=True)
    (share / "mnemos-prod" / "venv").mkdir()
    (quiet_home / ".local" / "bin").mkdir(parents=True)
    (quiet_home / ".local" / "bin" / "mnemos-mesh").write_text("#!/bin/sh\n")
    monkeypatch.setattr(updates_cli, "_PROD_VENV_BASES", (share,))
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: None)
    monkeypatch.setattr(updates_cli, "family_latest", lambda: None)
    result = _invoke_update([], monkeypatch)
    assert result.exit_code == 0
    assert "MANUAL GATE" in result.output
    assert "mnemos-mesh" in result.output
    assert "goreleaser" in result.output


def test_update_yes_runs_pip_user_upgrade(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[list[str]] = []

    def fake_run(cmd, timeout=900):
        recorded.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(updates_cli, "_run_cmd", fake_run)
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    monkeypatch.setattr(updates_cli, "_pep668_externally_managed", lambda: False)
    result = _invoke_update(["--yes", "--scope=user"], monkeypatch)
    assert result.exit_code == 0, result.output
    assert recorded, "pip must have been invoked"
    cmd = recorded[0]
    assert cmd[0] == sys.executable
    assert cmd[1:5] == ["-m", "pip", "install", "--user"]
    assert "--upgrade" in cmd
    assert "vesma" in cmd
    assert "restart clients" in result.output
    history = json.loads((quiet_home / ".local/share/vesma/update-history.json").read_text())
    assert history[0]["from"] == INSTALLED
    assert history[0]["rc"] == 0


def test_update_yes_pinned_to_version_is_rollback_path(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[list[str]] = []

    def fake_run(cmd, timeout=900):
        recorded.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(updates_cli, "_run_cmd", fake_run)
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    result = _invoke_update(["--yes", "--to", "5.1.0"], monkeypatch)
    assert result.exit_code == 0, result.output
    cmd = recorded[0]
    assert any(c == "vesma==5.1.0" for c in cmd)
    assert "--upgrade" not in cmd


def test_update_yes_adds_break_system_packages_under_pep668(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[list[str]] = []

    def fake_run(cmd, timeout=900):
        recorded.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(updates_cli, "_run_cmd", fake_run)
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    monkeypatch.setattr(updates_cli, "_pep668_externally_managed", lambda: True)
    result = _invoke_update(["--yes"], monkeypatch)
    assert result.exit_code == 0, result.output
    assert "--break-system-packages" in recorded[0]


def test_update_yes_records_failure_rc(quiet_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, timeout=900):
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="pip exploded")

    monkeypatch.setattr(updates_cli, "_run_cmd", fake_run)
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    result = _invoke_update(["--yes"], monkeypatch)
    assert result.exit_code == 1
    history = json.loads((quiet_home / ".local/share/vesma/update-history.json").read_text())
    assert history[0]["rc"] == 1


def test_update_yes_fails_cleanly_offline(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(dist):
        raise OSError("no network")

    monkeypatch.setattr(updates_cli, "fetch_latest", boom)
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    result = _invoke_update(["--yes"], monkeypatch)
    assert result.exit_code == 1
    assert "cannot reach PyPI" in result.output


def test_update_yes_without_pip_dist_fails(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: None)
    result = _invoke_update(["--yes"], monkeypatch)
    assert result.exit_code == 1
    assert "no pip-installed" in result.output


def test_update_rejects_unknown_scope(quiet_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    result = _invoke_update(["--yes", "--scope=binaries"], monkeypatch)
    assert result.exit_code == 1
    assert "only --scope=user" in result.output


# ── CLI: interactive confirm by default + -y alias (#460) ────────────────────


@pytest.fixture
def no_pypi(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the apply path fully offline: inject the PyPI answer."""
    monkeypatch.setattr(updates_cli, "fetch_latest", lambda dist: LATEST)


@pytest.fixture
def pending_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    monkeypatch.setattr(updates_cli, "check_for_update", lambda settings=None, **kw: _info())
    monkeypatch.setattr(updates_cli, "family_latest", lambda: LATEST)
    monkeypatch.setattr(updates_cli, "_pep668_externally_managed", lambda: False)


def _spy_confirm(monkeypatch: pytest.MonkeyPatch, answer: bool) -> list[str]:
    calls: list[str] = []

    def fake_confirm(prompt: str, **kwargs: object) -> bool:
        calls.append(str(prompt))
        return answer

    monkeypatch.setattr(typer, "confirm", fake_confirm)
    return calls


@pytest.fixture
def forbid_pip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any subprocess call in a check-only test is a failure."""

    def forbidden(cmd, timeout=900):  # pragma: no cover — exercised via assertion
        raise AssertionError(f"subprocess invoked on a check-only path: {cmd}")

    monkeypatch.setattr(updates_cli, "_run_cmd", forbidden)


def test_update_plain_tty_pending_confirm_yes_applies(
    quiet_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_pypi: None,
    pending_check: None,
) -> None:
    recorded: list[list[str]] = []

    def fake_run(cmd, timeout=900):
        recorded.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(updates_cli, "_run_cmd", fake_run)
    monkeypatch.setattr(updates_cli, "_stdin_is_tty", lambda: True)
    calls = _spy_confirm(monkeypatch, answer=True)

    result = _invoke_update([], monkeypatch)
    assert result.exit_code == 0, result.output
    assert calls == ["Apply update?"], "interactive default must ask before applying"
    assert recorded, "confirm-yes must run the pip upgrade"
    assert "restart clients" in result.output
    history = json.loads((quiet_home / ".local/share/vesma/update-history.json").read_text())
    assert history[0]["rc"] == 0


def test_update_plain_tty_confirm_no_stays_check_only(
    quiet_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_pypi: None,
    pending_check: None,
    forbid_pip: None,
) -> None:
    monkeypatch.setattr(updates_cli, "_stdin_is_tty", lambda: True)
    calls = _spy_confirm(monkeypatch, answer=False)

    result = _invoke_update([], monkeypatch)
    assert result.exit_code == 0
    assert calls == ["Apply update?"]
    assert "UPDATE AVAILABLE" in result.output, "the report must still be shown"


def test_update_plain_nontty_pending_check_only_with_hint(
    quiet_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_pypi: None,
    pending_check: None,
    forbid_pip: None,
) -> None:
    monkeypatch.setattr(updates_cli, "_stdin_is_tty", lambda: False)

    result = _invoke_update([], monkeypatch)
    assert result.exit_code == 0
    assert "apply with: vesma self-update apply" in result.output
    assert "UPDATE AVAILABLE" in result.output


def test_update_plain_no_pending_never_prompts(
    quiet_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    forbid_pip: None,
) -> None:
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    monkeypatch.setattr(
        updates_cli,
        "check_for_update",
        lambda settings=None, **kw: _info(update_available=False, latest="5.1.1"),
    )
    monkeypatch.setattr(updates_cli, "family_latest", lambda: "5.1.1")
    monkeypatch.setattr(updates_cli, "_stdin_is_tty", lambda: True)
    calls = _spy_confirm(monkeypatch, answer=True)

    result = _invoke_update([], monkeypatch)
    assert result.exit_code == 0
    assert not calls, "no pending update — no prompt"


def test_update_check_flag_never_prompts_even_when_pending(
    quiet_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_pypi: None,
    pending_check: None,
    forbid_pip: None,
) -> None:
    monkeypatch.setattr(updates_cli, "_stdin_is_tty", lambda: True)
    calls = _spy_confirm(monkeypatch, answer=True)

    result = _invoke_update(["--check"], monkeypatch)
    assert result.exit_code == 0
    assert not calls, "--check is always check-only"
    assert "apply with" not in result.output


def test_update_y_alias_applies_without_prompt(
    quiet_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_pypi: None,
    pending_check: None,
) -> None:
    recorded: list[list[str]] = []

    def fake_run(cmd, timeout=900):
        recorded.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(updates_cli, "_run_cmd", fake_run)
    monkeypatch.setattr(updates_cli, "_stdin_is_tty", lambda: True)
    calls = _spy_confirm(monkeypatch, answer=True)

    result = _invoke_update(["-y"], monkeypatch)
    assert result.exit_code == 0, result.output
    assert not calls, "-y must skip the prompt"
    assert recorded, "-y must apply"


# ── CLI: quiet pip capture + --verbose (#460) ────────────────────────────────


def test_update_yes_quiet_pip_success_prints_summary_line(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firehose = "\n".join(f"Requirement already satisfied: noise-{i:02d}" for i in range(20))
    versions = iter([("vesma", INSTALLED), ("vesma", LATEST)])

    def fake_run(cmd, timeout=900):
        return subprocess.CompletedProcess(cmd, 0, stdout=firehose, stderr="")

    monkeypatch.setattr(updates_cli, "_run_cmd", fake_run)
    monkeypatch.setattr(updates_cli, "fetch_latest", lambda dist: LATEST)
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: next(versions))
    result = _invoke_update(["--yes"], monkeypatch)
    assert result.exit_code == 0, result.output
    assert f"pip: vesma {INSTALLED} → {LATEST}" in result.output
    assert "Requirement already satisfied" not in result.output
    assert "$ " not in result.output, "quiet mode must not echo the command lines"


def test_update_yes_quiet_pip_already_current(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(cmd, timeout=900):
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(updates_cli, "_run_cmd", fake_run)
    monkeypatch.setattr(updates_cli, "fetch_latest", lambda dist: INSTALLED)
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    result = _invoke_update(["--yes"], monkeypatch)
    assert result.exit_code == 0, result.output
    assert "already current" in result.output


def test_update_yes_verbose_prints_full_pip_output(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firehose = "\n".join(f"Requirement already satisfied: noise-{i:02d}" for i in range(20))

    def fake_run(cmd, timeout=900):
        return subprocess.CompletedProcess(cmd, 0, stdout=firehose, stderr="")

    monkeypatch.setattr(updates_cli, "_run_cmd", fake_run)
    monkeypatch.setattr(updates_cli, "fetch_latest", lambda dist: LATEST)
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    result = _invoke_update(["--yes", "--verbose"], monkeypatch)
    assert result.exit_code == 0, result.output
    assert "Requirement already satisfied: noise-00" in result.output
    assert "Requirement already satisfied: noise-19" in result.output
    assert "$ " in result.output, "verbose mode echoes the command lines as before"


def test_update_yes_quiet_pip_failure_shows_tail_and_verbose_hint(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    firehose = "\n".join(f"Collecting noise-{i:02d}" for i in range(25))

    def fake_run(cmd, timeout=900):
        return subprocess.CompletedProcess(cmd, 1, stdout=firehose, stderr="pip exploded")

    monkeypatch.setattr(updates_cli, "_run_cmd", fake_run)
    monkeypatch.setattr(updates_cli, "fetch_latest", lambda dist: LATEST)
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    result = _invoke_update(["--yes"], monkeypatch)
    assert result.exit_code == 1
    assert "pip upgrade failed" in result.output
    assert "pip exploded" in result.output
    assert "Collecting noise-24" in result.output, "the captured tail must be shown"
    assert "Collecting noise-09" not in result.output, "only the tail, not the firehose"
    assert "re-run with --verbose" in result.output


def test_update_check_installed_newer_than_latest_wording(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Wide console: rich would otherwise wrap the table Note cell and break
    # the exact-phrase assertion below.
    monkeypatch.setenv("COLUMNS", "300")
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: ("vesma", "5.1.2"))
    monkeypatch.setattr(
        updates_cli,
        "check_for_update",
        lambda settings=None, **kw: _info(update_available=False, latest="5.1.1"),
    )
    monkeypatch.setattr(updates_cli, "family_latest", lambda: None)
    result = _invoke_update([], monkeypatch)
    assert result.exit_code == 0
    assert "newer than published latest (local build?)" in result.output
    assert "up to date" not in result.output


# ── CLI: timer install / removal ─────────────────────────────────────────────


def test_install_timer_writes_units_and_enables(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(updates_cli, "_systemctl", lambda *args: calls.append(args) or 0)
    result = _invoke_update(["--install-timer"], monkeypatch)
    assert result.exit_code == 0, result.output
    unit_dir = quiet_home / ".config/systemd/user"
    service = (unit_dir / "vesma-update.service").read_text()
    timer = (unit_dir / "vesma-update.timer").read_text()
    assert "--yes --scope=user" in service
    assert "Type=oneshot" in service
    assert "OnCalendar=weekly" in timer
    assert "Persistent=true" in timer
    assert "RandomizedDelaySec=1h" in timer
    assert ("daemon-reload",) in calls
    assert ("enable", "--now", "vesma-update.timer") in calls


def test_install_timer_env_fallback_exec_start(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updates_cli, "_systemctl", lambda *args: 0)
    result = _invoke_update(["--install-timer"], monkeypatch)
    assert result.exit_code == 0, result.output
    service = (quiet_home / ".config/systemd/user/vesma-update.service").read_text()
    # quiet_home has no ~/.local/bin/vesma launcher — /usr/bin/env fallback.
    assert "/usr/bin/env vesma self-update --yes --scope=user" in service


def test_install_timer_prints_instructions_when_systemctl_unavailable(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(updates_cli, "_systemctl", lambda *args: None)
    result = _invoke_update(["--install-timer"], monkeypatch)
    assert result.exit_code == 0, result.output
    assert "systemctl --user daemon-reload" in result.output
    assert "enable --now vesma-update.timer" in result.output


def test_uninstall_timer_disables_and_removes(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(updates_cli, "_systemctl", lambda *args: calls.append(args) or 0)
    assert _invoke_update(["--install-timer"], monkeypatch).exit_code == 0
    unit_dir = quiet_home / ".config/systemd/user"
    assert (unit_dir / "vesma-update.service").exists()
    result = _invoke_update(["--uninstall-timer"], monkeypatch)
    assert result.exit_code == 0, result.output
    assert not (unit_dir / "vesma-update.service").exists()
    assert not (unit_dir / "vesma-update.timer").exists()
    assert ("disable", "--now", "vesma-update.timer") in calls


def test_timer_flags_are_mutually_exclusive(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _invoke_update(["--install-timer", "--uninstall-timer"], monkeypatch)
    assert result.exit_code == 1
    assert "mutually exclusive" in result.output


# ── CLI: timer install / removal inside a distrobox (#468) ───────────────────


@pytest.fixture(autouse=True)
def not_a_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake the machine container signals OFF for deterministic verdicts.

    This dev machine runs inside a distrobox (``/run/.containerenv``
    exists AND ``CONTAINER_ID``/``DISTROBOX_ENTER_ENV`` are set), so the
    real signals would hijack every host-path test. Box detection still
    works via the HOME ``/.distrobox/`` layout; the marker-only test
    below re-points the constant at a real file.
    """
    monkeypatch.setattr(updates_cli, "_CONTAINERENV", Path("/nonexistent/.containerenv"))
    monkeypatch.delenv("CONTAINER_ID", raising=False)
    monkeypatch.delenv("DISTROBOX_ENTER_ENV", raising=False)


def _make_box_home(tmp_path: Path, box: str) -> tuple[Path, Path]:
    """(box_home, host_home): HOME layout of a distrobox on a host."""
    host_home = tmp_path / "hosthome"
    box_home = host_home / ".distrobox" / box / "home"
    box_home.mkdir(parents=True)
    return box_home, host_home


@pytest.fixture
def box_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """HOME inside a distrobox 'ubuntu' whose host home is <tmp>/hosthome."""
    box_home, host_home = _make_box_home(tmp_path, "ubuntu")
    monkeypatch.setenv("HOME", str(box_home))
    monkeypatch.setattr(updates_cli, "_PROD_VENV_BASES", (tmp_path / "nonexistent-share",))
    monkeypatch.setattr("shutil.which", lambda name: None)
    return box_home, host_home


def _spy_systemctl(monkeypatch: pytest.MonkeyPatch, rc: int | None) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []

    def fake_run(cmd, timeout=60):
        calls.append(tuple(cmd))
        if rc is None:
            raise OSError("systemctl unavailable")
        return subprocess.CompletedProcess(cmd, rc, stdout="", stderr="")

    monkeypatch.setattr(updates_cli, "_run_cmd", fake_run)
    return calls


def test_distrobox_context_host_vs_box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    assert updates_cli._distrobox_context() == (False, None, None)
    box_home, host_home = _make_box_home(tmp_path, "ubuntu")
    monkeypatch.setenv("HOME", str(box_home))
    assert updates_cli._distrobox_context() == (True, "ubuntu", host_home)


def test_distrobox_context_container_marker_without_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / ".containerenv"
    marker.write_text("")
    monkeypatch.setattr(updates_cli, "_CONTAINERENV", marker)
    monkeypatch.setenv("HOME", str(tmp_path))
    # In a container, but the host home is not derivable → (True, None, None).
    assert updates_cli._distrobox_context() == (True, None, None)


def test_install_timer_in_box_writes_host_units(
    box_env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    box_home, host_home = box_env
    monkeypatch.setattr(updates_cli, "_host_user", lambda: "hostuser")
    calls = _spy_systemctl(monkeypatch, rc=0)
    result = _invoke_update(["timer", "install", "--force"], monkeypatch)
    assert result.exit_code == 0, result.output
    unit_dir = host_home / ".config/systemd/user"
    service = (unit_dir / "vesma-update.service").read_text()
    assert (
        f"ExecStart={host_home}/.local/bin/distrobox-enter -n ubuntu -- /bin/sh -c "
        "'exec \"$HOME/.local/bin/vesma\" self-update --yes --scope=user'"
    ) in service
    timer = (unit_dir / "vesma-update.timer").read_text()
    assert "OnCalendar=weekly" in timer
    # The dead-units regression: nothing may land in the BOX home.
    assert not (box_home / ".config/systemd").exists()
    transport = ("systemctl", "--user", "-M", "hostuser@.host")
    assert (*transport, "daemon-reload") in calls
    assert (*transport, "enable", "--now", "vesma-update.timer") in calls


def test_install_timer_second_box_accumulates_exec_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, host_home = _make_box_home(tmp_path, "ubuntu")
    second_home = host_home / ".distrobox" / "vscode-box" / "home"
    second_home.mkdir(parents=True)
    monkeypatch.setattr(updates_cli, "_host_user", lambda: "hostuser")
    _spy_systemctl(monkeypatch, rc=0)

    monkeypatch.setenv("HOME", str(host_home / ".distrobox" / "ubuntu" / "home"))
    assert _invoke_update(["timer", "install", "--force"], monkeypatch).exit_code == 0
    monkeypatch.setenv("HOME", str(second_home))
    assert _invoke_update(["timer", "install", "--force"], monkeypatch).exit_code == 0
    # Idempotent re-run must not duplicate a line.
    assert _invoke_update(["timer", "install", "--force"], monkeypatch).exit_code == 0

    service = (host_home / ".config/systemd/user/vesma-update.service").read_text()
    exec_lines = [ln for ln in service.splitlines() if ln.startswith("ExecStart=")]
    assert len(exec_lines) == 2, f"accumulation + idempotency broken:\n{service}"
    assert "-n ubuntu --" in exec_lines[0]
    assert "-n vscode-box --" in exec_lines[1]


def test_uninstall_timer_in_box_removes_only_own_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, host_home = _make_box_home(tmp_path, "ubuntu")
    second_home = host_home / ".distrobox" / "vscode-box" / "home"
    second_home.mkdir(parents=True)
    monkeypatch.setattr(updates_cli, "_host_user", lambda: "hostuser")
    calls = _spy_systemctl(monkeypatch, rc=0)

    monkeypatch.setenv("HOME", str(host_home / ".distrobox" / "ubuntu" / "home"))
    assert _invoke_update(["timer", "install", "--force"], monkeypatch).exit_code == 0
    monkeypatch.setenv("HOME", str(second_home))
    assert _invoke_update(["timer", "install", "--force"], monkeypatch).exit_code == 0

    monkeypatch.setenv("HOME", str(host_home / ".distrobox" / "ubuntu" / "home"))
    result = _invoke_update(["--uninstall-timer"], monkeypatch)
    assert result.exit_code == 0, result.output
    unit_dir = host_home / ".config/systemd/user"
    service = (unit_dir / "vesma-update.service").read_text()
    assert "-n ubuntu --" not in service
    assert "-n vscode-box --" in service, "removing one box must not unschedule the others"
    assert (unit_dir / "vesma-update.timer").exists()
    assert not any("disable" in c for c in calls), "timer stays enabled for the other box"


def test_uninstall_timer_last_box_removes_units(
    box_env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, host_home = box_env
    monkeypatch.setattr(updates_cli, "_host_user", lambda: "hostuser")
    _spy_systemctl(monkeypatch, rc=0)
    assert _invoke_update(["timer", "install", "--force"], monkeypatch).exit_code == 0

    calls = _spy_systemctl(monkeypatch, rc=0)
    result = _invoke_update(["--uninstall-timer"], monkeypatch)
    assert result.exit_code == 0, result.output
    unit_dir = host_home / ".config/systemd/user"
    assert not (unit_dir / "vesma-update.service").exists()
    assert not (unit_dir / "vesma-update.timer").exists()
    transport = ("systemctl", "--user", "-M", "hostuser@.host")
    assert (*transport, "disable", "--now", "vesma-update.timer") in calls, (
        "the last box out must disable via the host transport"
    )
    assert (*transport, "daemon-reload") in calls


def test_install_timer_in_box_prints_manual_commands_when_host_manager_unreachable(
    box_env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, host_home = box_env
    monkeypatch.setattr(updates_cli, "_host_user", lambda: "hostuser")
    _spy_systemctl(monkeypatch, rc=1)
    result = _invoke_update(["timer", "install", "--force"], monkeypatch)
    assert result.exit_code == 0, "an unreachable host manager must not fail the command"
    assert "systemctl --user -M hostuser@.host daemon-reload" in result.output
    assert "systemctl --user -M hostuser@.host enable --now vesma-update.timer" in result.output
    assert "sudo loginctl enable-linger hostuser" in result.output
    # The units are still in place — only the enabling is left to the host.
    assert (host_home / ".config/systemd/user/vesma-update.service").exists()


def test_install_timer_in_container_without_distrobox_layout_refuses(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = quiet_home / "fake-containerenv"
    marker.write_text("")
    monkeypatch.setattr(updates_cli, "_CONTAINERENV", marker)
    calls = _spy_systemctl(monkeypatch, rc=0)
    result = _invoke_update(["timer", "install"], monkeypatch)
    assert result.exit_code == 1, "a container with no derivable host home must refuse"
    assert "host home cannot be derived" in result.output
    assert "distrobox-enter" in result.output, "the hint names the per-box ExecStart recipe"
    assert not calls, "nothing may be executed on the refusal path"
    assert not (quiet_home / ".config/systemd").exists(), "no dead units in the container"


# ── contrib drift guard ──────────────────────────────────────────────────────


def test_contrib_unit_files_match_install_templates() -> None:
    """The repo contrib units are the templates with the default ExecStart.

    The templates are the INSTALL source (pip installs ship no contrib/);
    this test keeps the repo copies from drifting.
    """
    repo = Path(__file__).resolve().parent.parent
    contrib_service = (repo / "contrib" / "vesma-update.service").read_text()
    contrib_timer = (repo / "contrib" / "vesma-update.timer").read_text()
    assert contrib_service == updates_cli._SERVICE_TEMPLATE.format(
        exec_start=updates_cli._DEFAULT_EXEC_START
    )
    assert contrib_timer == updates_cli._TIMER_TEMPLATE


# ── subcommand routing: check / apply (board card W-C) ───────────────────────


def test_update_check_subcommand_never_prompts_or_applies(
    quiet_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_pypi: None,
    pending_check: None,
    forbid_pip: None,
) -> None:
    monkeypatch.setattr(updates_cli, "_stdin_is_tty", lambda: True)
    calls = _spy_confirm(monkeypatch, answer=True)

    result = _invoke_update(["check"], monkeypatch)
    assert result.exit_code == 0
    assert not calls, "check must never prompt"
    assert "UPDATE AVAILABLE" in result.output
    assert "apply with" not in result.output


def test_update_check_subcommand_matches_deprecated_flag(
    quiet_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_pypi: None,
    pending_check: None,
    forbid_pip: None,
) -> None:
    """`check` and the deprecated `--check` render the same report."""
    plain = _invoke_update(["check"], monkeypatch)
    flag = _invoke_update(["--check"], monkeypatch)
    assert plain.exit_code == flag.exit_code == 0
    assert "UPDATE AVAILABLE" in plain.output
    assert "UPDATE AVAILABLE" in flag.output


def test_update_apply_subcommand_runs_the_yes_path(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[list[str]] = []

    def fake_run(cmd, timeout=900):
        recorded.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(updates_cli, "_run_cmd", fake_run)
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    monkeypatch.setattr(updates_cli, "_pep668_externally_managed", lambda: False)
    monkeypatch.setattr(updates_cli, "_stdin_is_tty", lambda: True)
    calls = _spy_confirm(monkeypatch, answer=False)

    result = _invoke_update(["apply"], monkeypatch)
    assert result.exit_code == 0, result.output
    assert not calls, "apply is already the explicit confirmation — no prompt"
    assert recorded, "apply must run the pip upgrade"
    assert "restart clients" in result.output


def test_update_apply_subcommand_rejects_unknown_scope(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch, forbid_pip: None
) -> None:
    result = _invoke_update(["apply", "--scope", "binaries"], monkeypatch)
    assert result.exit_code == 1
    assert "only --scope=user" in result.output


def test_update_apply_yes_flag_is_a_noop(quiet_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`apply -y` works for muscle memory but changes nothing."""
    recorded: list[list[str]] = []

    def fake_run(cmd, timeout=900):
        recorded.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(updates_cli, "_run_cmd", fake_run)
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    result = _invoke_update(["apply", "-y"], monkeypatch)
    assert result.exit_code == 0, result.output
    assert recorded


# ── hidden deprecated flag aliases + stderr hints (standing rule) ─────────────


def test_deprecated_check_alias_hints_on_stderr_stdout_clean(
    quiet_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_pypi: None,
    pending_check: None,
    forbid_pip: None,
) -> None:
    result = _invoke_update(["--check"], monkeypatch)
    assert result.exit_code == 0
    assert "use: vesma self-update check" in result.stderr
    assert "[deprecated]" in result.stderr
    assert "use: vesma self-update check" not in result.stdout, "stdout stays clean for pipes/JSON"
    assert "UPDATE AVAILABLE" in result.stdout, "the flag still works identically"


def test_deprecated_yes_alias_hints_and_applies(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[list[str]] = []

    def fake_run(cmd, timeout=900):
        recorded.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(updates_cli, "_run_cmd", fake_run)
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    result = _invoke_update(["--yes"], monkeypatch)
    assert result.exit_code == 0, result.output
    assert "use: vesma self-update apply" in result.stderr
    assert "deprecated" not in result.stdout
    assert recorded, "the alias must keep working"


def test_deprecated_to_and_scope_aliases_hint(
    quiet_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_pypi: None,
    pending_check: None,
    forbid_pip: None,
) -> None:
    result = _invoke_update(["--to", "5.1.0", "--scope=user"], monkeypatch)
    assert result.exit_code == 0
    assert "use: vesma self-update apply --to VERSION" in result.stderr
    assert "use: vesma self-update apply --scope user" in result.stderr


def test_deprecated_uninstall_timer_alias_hints_and_removes(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy_systemctl(monkeypatch, rc=0)
    result = _invoke_update(["--uninstall-timer"], monkeypatch)
    assert result.exit_code == 0, result.output
    assert "use: vesma self-update timer uninstall" in result.stderr
    assert "removed" in result.stdout
    assert calls, "the alias must route to the same removal"


def test_deprecated_flags_hidden_from_help(quiet_home: Path) -> None:
    from vesma.cli.main import app

    result = runner.invoke(app, ["self-update", "--help"])
    assert result.exit_code == 0
    # The deprecation NOTE mentions --check once; a visible option would
    # render a second time in the Options panel.
    assert result.output.count("--check") == 1, "deprecated flags stay hidden options"
    assert result.output.count("--install-timer") == 1
    for subcommand in ("check", "apply", "timer", "components"):
        assert subcommand in result.output
    assert "deprecated" in result.output, "the help must carry the deprecation note"


def test_options_before_subcommand_warn_not_drop(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[list[str]] = []

    def fake_run(cmd, timeout=900):
        recorded.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(updates_cli, "_run_cmd", fake_run)
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    result = _invoke_update(["--yes", "apply"], monkeypatch)
    assert result.exit_code == 0, result.output
    assert "options placed before the subcommand are ignored" in result.stderr
    assert recorded, "the subcommand still runs (the misplaced option is dropped)"


def test_systemd_unit_exec_start_forms_still_work(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scripted dependency: the shipped unit's `--yes --scope=user` applies."""
    recorded: list[list[str]] = []

    def fake_run(cmd, timeout=900):
        recorded.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr(updates_cli, "_run_cmd", fake_run)
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: ("vesma", INSTALLED))
    result = _invoke_update(["--yes", "--scope=user"], monkeypatch)
    assert result.exit_code == 0, result.output
    assert recorded, "the unit's ExecStart form must keep applying"


# ── pip alias family row (board card W-C) ─────────────────────────────────────


def test_family_row_single_row_installed_as_alias(
    quiet_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_pypi: None,
    forbid_pip: None,
) -> None:
    monkeypatch.setattr(
        updates_cli, "detect_installed_dist", lambda: ("vesma-memory-server", INSTALLED)
    )
    monkeypatch.setattr(
        updates_cli,
        "check_for_update",
        lambda settings=None, **kw: _info(update_available=False, latest=INSTALLED),
    )
    monkeypatch.setattr(updates_cli, "family_latest", lambda: INSTALLED)
    monkeypatch.setenv("COLUMNS", "300")

    result = _invoke_update(["check"], monkeypatch)
    assert result.exit_code == 0
    assert "pip: vesma (family)" in result.output
    assert "not installed" not in result.output, "aliases must not show as missing rows"
    assert (
        "up to date (installed as vesma-memory-server — same codebase, alias package)"
        in result.output
    )


def test_family_row_nothing_installed(
    quiet_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_pypi: None,
    forbid_pip: None,
) -> None:
    monkeypatch.setattr(updates_cli, "detect_installed_dist", lambda: None)
    monkeypatch.setattr(updates_cli, "check_for_update", lambda settings=None, **kw: None)
    monkeypatch.setattr(updates_cli, "family_latest", lambda: LATEST)
    monkeypatch.setenv("COLUMNS", "300")

    result = _invoke_update(["check"], monkeypatch)
    assert result.exit_code == 0
    assert "pip: vesma (family)" in result.output
    assert "not installed — pip install --user vesma" in result.output
    assert result.output.count("pip:") == 1, "exactly ONE family row"


def test_family_row_update_available_wording_points_at_apply(
    quiet_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_pypi: None,
    pending_check: None,
    forbid_pip: None,
) -> None:
    monkeypatch.setenv("COLUMNS", "300")
    result = _invoke_update(["check"], monkeypatch)
    assert result.exit_code == 0
    assert "UPDATE AVAILABLE — run 'vesma self-update apply'" in result.output


# ── family_latest (updates.py) ────────────────────────────────────────────────


def test_family_latest_takes_max_over_aliases(cache_file: Path) -> None:
    prices = {"vesma-memory-server": "5.3.0", "vesma": "6.0.0"}

    def fetcher(dist: str) -> str:
        return prices[dist]

    assert updates.family_latest(cache_path=cache_file, fetcher=fetcher) == "6.0.0"
    payload = json.loads(cache_file.read_text())
    assert payload["family"]["latest"] == "6.0.0"
    assert payload["family"]["ok"] is True


def test_family_latest_reuses_fresh_per_dist_cache(cache_file: Path) -> None:
    _seed_cache(cache_file, latest="9.9.9", ok=True, checked_at=datetime.now(UTC))
    calls: list[str] = []

    def fetcher(dist: str) -> str:
        calls.append(dist)
        return "1.0.0"

    assert updates.family_latest(cache_path=cache_file, fetcher=fetcher) == "9.9.9"
    assert calls == ["vesma"], "the fresh per-dist answer must not be re-fetched"


def test_family_latest_negative_cache_bounds_offline(cache_file: Path) -> None:
    calls: list[str] = []

    def fetcher(dist: str) -> str:
        calls.append(dist)
        raise OSError("no network")

    assert updates.family_latest(cache_path=cache_file, fetcher=fetcher) is None
    payload = json.loads(cache_file.read_text())
    assert payload["family"]["ok"] is False
    assert updates.family_latest(cache_path=cache_file, fetcher=fetcher) is None
    assert len(calls) == 2, "within the 1h negative TTL there must be no re-fetch"


def test_family_latest_respects_env_opt_out(
    cache_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def never(dist: str) -> str:  # pragma: no cover — must never run
        raise AssertionError("fetch despite opt-out")

    monkeypatch.setenv("VESMA_UPDATES_CHECK", "off")
    assert updates.family_latest(cache_path=cache_file, fetcher=never) is None


# ── #468: the loud guard around `timer install` inside a box ──────────────────


def test_timer_install_refused_in_box_with_force_hint(
    box_env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, forbid_pip: None
) -> None:
    box_home, host_home = box_env
    calls = _spy_systemctl(monkeypatch, rc=0)
    result = _invoke_update(["timer", "install"], monkeypatch)
    assert result.exit_code == 1, "a box install must refuse loudly"
    assert "dead units" in result.output
    assert "issue #468" in result.output
    assert "--force" in result.output, "the refusal names the escape hatch"
    assert "on the HOST" in result.output
    assert not calls, "nothing may be executed on the refusal path"
    assert not (host_home / ".config/systemd").exists(), "no units written"
    assert not (box_home / ".config/systemd").exists(), "no dead units in the box home"


def test_timer_install_alias_refused_in_box(
    box_env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch, forbid_pip: None
) -> None:
    _spy_systemctl(monkeypatch, rc=0)
    result = _invoke_update(["--install-timer"], monkeypatch)
    assert result.exit_code == 1
    assert "use: vesma self-update timer install" in result.stderr
    assert "dead units" in result.output


def test_timer_install_container_env_only_detection(
    quiet_home: Path, monkeypatch: pytest.MonkeyPatch, forbid_pip: None
) -> None:
    """CONTAINER_ID alone (no HOME layout, no marker) must trigger the guard."""
    monkeypatch.setenv("CONTAINER_ID", "ci-box")
    calls = _spy_systemctl(monkeypatch, rc=0)
    result = _invoke_update(["timer", "install"], monkeypatch)
    assert result.exit_code == 1
    assert "dead units" in result.output
    assert not calls


def test_timer_status_reports_box_context(
    box_env: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _spy_systemctl(monkeypatch, rc=1)  # nothing enabled locally
    result = _invoke_update(["timer", "status", "--json"], monkeypatch)
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["container"] is True
    assert payload["box"] == "ubuntu"
    assert payload["state"] == "not installed"
