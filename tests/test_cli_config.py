"""CLI topology v2 wave C pins — the TWO config domains.

``vesma config`` (USER profile, read-only: show/validate/paths) and
``vesma system config`` (SYSTEM profile: show/validate/paths + the ONE
write, ``set --file``). The contract pins (handoff
cli-topology-v2-inventory-20261023.md, ArchCom 2026-10-23):

* TWO EXPLICIT verbs — a config command never switches its target by
  euid/env (scope-isolation pins below);
* validation is ONE implementation (``load_settings`` — the doctor's own
  check) and the user paths table is the doctor's own collector
  (shared-seam pins);
* ``set --file`` validates the candidate BEFORE any mutation (invalid →
  tree untouched), backs up same-dir, installs atomically with the
  manifest role's mode/owner, and installs NO mover provenance header —
  the F9 adopt predicate keeps refusing operator-installed conffiles;
* unreadable-as-non-root system paths answer with the loud sudo hint and
  exit 1 — the command NEVER escalates itself (G1/G2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import vesma.service.system_install as si
from vesma.cli.main import app
from vesma.service import fhs

runner = CliRunner()

_VALID_YAML = """\
# a small but complete user config
vesma:
  data_dir: /tmp/vesma-test-data
api:
  port: 9999
"""

_BROKEN_YAML = "not: [closed\n"

#: The mover's provenance stamp (F9) — ``set --file`` must NEVER add it.
_MOVER_HEADER = "# written by vesma migrate-store"


@pytest.fixture
def user_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HOME with no config anywhere on the search path."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)  # ./config.yaml must not leak in
    for var in ("VESMA_CONFIG", "VESMA_DATA_DIR", "VESMA_VAULT__VAULT_PATH"):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture
def system_prefix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The dest-dir deployment prefix: /etc/vesma exists, FHS resolvers
    resolve under it (the call-time ``fhs.default_prefix`` seam)."""
    prefix = tmp_path / "deploy"
    (prefix / "etc" / "vesma").mkdir(parents=True)
    monkeypatch.setattr(fhs, "default_prefix", lambda: prefix)
    return prefix


class _FakeAccount:
    def __init__(self, uid: int, gid: int) -> None:
        self.pw_uid = uid
        self.pw_gid = gid
        self.gr_gid = gid


@pytest.fixture
def root_seams(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """euid==0 with fake account resolution and a recorded chown — the
    ownership leg runs without a real vesma group on this machine."""
    import grp as grp_mod
    import pwd as pwd_mod

    events: list[str] = []
    accounts: dict[str, Any] = {"root": pwd_mod.getpwnam("root")}
    groups: dict[str, Any] = {"root": grp_mod.getgrnam("root")}
    accounts["vesma"] = _FakeAccount(4242, 4242)
    groups["vesma"] = _FakeAccount(4242, 4242)

    monkeypatch.setattr(si.pwd, "getpwnam", lambda n: accounts[n])
    monkeypatch.setattr(si.grp, "getgrnam", lambda n: groups[n])
    monkeypatch.setattr(si.os, "geteuid", lambda: 0)
    monkeypatch.setattr(si.os, "chown", lambda p, u, g: events.append(f"chown:{u}:{g}"))
    return events


#: Whitespace-normalized output — rich wraps long paths mid-path, so
# substring assertions must not depend on line breaks.
def _flat(text: str) -> str:
    return " ".join(text.split())


# ── the USER domain: vesma config ──────────────────────────────────────


class TestUserConfigDomain:
    def test_show_resolves_env_config_and_renders_effective(
        self, user_env: Path, tmp_path: Path
    ) -> None:
        cfg = tmp_path / "my-config.yaml"
        cfg.write_text(_VALID_YAML, encoding="utf-8")
        import os

        os.environ["VESMA_CONFIG"] = str(cfg)
        try:
            result = runner.invoke(app, ["config", "show"])
        finally:
            del os.environ["VESMA_CONFIG"]
        assert result.exit_code == 0, result.output
        assert str(cfg) in result.output
        assert "data_dir" in result.output and "/tmp/vesma-test-data" in result.output
        assert "9999" in result.output

    def test_show_json_payload_shape(self, user_env: Path, tmp_path: Path) -> None:
        import json
        import os

        cfg = tmp_path / "my-config.yaml"
        cfg.write_text(_VALID_YAML, encoding="utf-8")
        os.environ["VESMA_CONFIG"] = str(cfg)
        try:
            result = runner.invoke(app, ["config", "show", "--json"])
        finally:
            del os.environ["VESMA_CONFIG"]
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["profile"] == "user"
        assert payload["config_file"] == str(cfg)
        assert payload["effective"]["api"]["port"] == 9999

    def test_show_zero_config_is_explicit_not_an_error(self, user_env: Path) -> None:
        result = runner.invoke(app, ["config", "show"])
        assert result.exit_code == 0, result.output
        assert "zero-config" in result.output

    def test_validate_ok_exit_zero(self, user_env: Path, tmp_path: Path) -> None:
        import os

        cfg = tmp_path / "my-config.yaml"
        cfg.write_text(_VALID_YAML, encoding="utf-8")
        os.environ["VESMA_CONFIG"] = str(cfg)
        try:
            result = runner.invoke(app, ["config", "validate"])
        finally:
            del os.environ["VESMA_CONFIG"]
        assert result.exit_code == 0, result.output
        assert "valid" in result.output

    def test_validate_broken_yaml_exit_one_with_label(self, user_env: Path, tmp_path: Path) -> None:
        import os

        cfg = tmp_path / "my-config.yaml"
        cfg.write_text(_BROKEN_YAML, encoding="utf-8")
        os.environ["VESMA_CONFIG"] = str(cfg)
        try:
            result = runner.invoke(app, ["config", "validate"])
        finally:
            del os.environ["VESMA_CONFIG"]
        assert result.exit_code == 1
        assert str(cfg) in result.output

    def test_validate_zero_config_is_valid(self, user_env: Path) -> None:
        result = runner.invoke(app, ["config", "validate"])
        assert result.exit_code == 0, result.output
        assert "zero-config" in result.output

    def test_paths_reuses_the_doctors_collector_seam(
        self, user_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shared-seam pin: `config paths` renders through the SAME
        `_collect_paths` the doctor renders — the surfaces can never
        disagree."""
        import vesma.cli.doctor as doctor_mod

        def sentinel(_settings: Any) -> dict[str, str]:
            return {"root": "/sentinel-root", "db_path": "/sentinel-db"}

        monkeypatch.setattr(doctor_mod, "_collect_paths", sentinel)
        result = runner.invoke(app, ["config", "paths"])
        assert result.exit_code == 0, result.output
        assert "/sentinel-root" in _flat(result.output)
        assert "/sentinel-db" in _flat(result.output)

    def test_paths_json_carries_the_collector_output(
        self, user_env: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        import vesma.cli.doctor as doctor_mod

        monkeypatch.setattr(doctor_mod, "_collect_paths", lambda _s: {"Root": "/sentinel-root"})
        result = runner.invoke(app, ["config", "paths", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["paths"]["Root"] == "/sentinel-root"


# ── the SYSTEM domain: vesma system config ─────────────────────────────


class TestSystemConfigDomain:
    def test_show_absent_is_zero_config_exit_zero(self, system_prefix: Path) -> None:
        result = runner.invoke(app, ["system", "config", "show"], env={"COLUMNS": "400"})
        assert result.exit_code == 0, result.output
        assert "absent" in result.output and "zero-config" in result.output

    def test_show_present_renders_system_file(self, system_prefix: Path, user_env: Path) -> None:
        target = fhs.system_config_path()
        target.write_text(_VALID_YAML, encoding="utf-8")
        result = runner.invoke(app, ["system", "config", "show"], env={"COLUMNS": "400"})
        assert result.exit_code == 0, result.output
        assert str(target) in _flat(result.output)

    def test_show_unreadable_file_says_sudo_exit_one(
        self, system_prefix: Path, user_env: Path
    ) -> None:
        target = fhs.system_config_path()
        target.write_text(_VALID_YAML, encoding="utf-8")
        target.chmod(0o000)  # unreadable even for the owner (non-root)
        result = runner.invoke(app, ["system", "config", "show"], env={"COLUMNS": "400"})
        assert result.exit_code == 1
        assert "sudo vesma system config show" in result.output

    def test_show_unreadable_tree_says_sudo_exit_one(
        self, system_prefix: Path, user_env: Path
    ) -> None:
        (fhs.system_config_root() / "config.yaml").write_text(_VALID_YAML, encoding="utf-8")
        fhs.system_config_root().chmod(0o000)
        result = runner.invoke(app, ["system", "config", "show"], env={"COLUMNS": "400"})
        fhs.system_config_root().chmod(0o750)
        assert result.exit_code == 1
        assert "sudo" in result.output

    def test_validate_absent_ok_present_ok_broken_refused(
        self, system_prefix: Path, user_env: Path
    ) -> None:
        assert runner.invoke(app, ["system", "config", "validate"]).exit_code == 0
        target = fhs.system_config_path()
        target.write_text(_VALID_YAML, encoding="utf-8")
        assert runner.invoke(app, ["system", "config", "validate"]).exit_code == 0
        target.write_text(_BROKEN_YAML, encoding="utf-8")
        result = runner.invoke(app, ["system", "config", "validate"], env={"COLUMNS": "400"})
        assert result.exit_code == 1
        assert str(target) in _flat(result.output)

    def test_paths_rows_come_from_the_fhs_resolvers(
        self, system_prefix: Path, user_env: Path
    ) -> None:
        result = runner.invoke(app, ["system", "config", "paths"], env={"COLUMNS": "400"})
        assert result.exit_code == 0, result.output
        flat = _flat(result.output)
        assert str(system_prefix / "etc" / "vesma" / "config.yaml") in flat
        assert str(system_prefix / "var" / "lib" / "vesma") in flat

    def test_system_never_consults_the_user_search_path(
        self, system_prefix: Path, user_env: Path, tmp_path: Path
    ) -> None:
        """Scope isolation: a BROKEN user config on $VESMA_CONFIG must not
        affect the system domain (its target is the FHS path, explicit)."""
        import os

        broken_user = tmp_path / "broken-user.yaml"
        broken_user.write_text(_BROKEN_YAML, encoding="utf-8")
        os.environ["VESMA_CONFIG"] = str(broken_user)
        try:
            fhs.system_config_path().write_text(_VALID_YAML, encoding="utf-8")
            result = runner.invoke(app, ["system", "config", "validate"], env={"COLUMNS": "400"})
        finally:
            del os.environ["VESMA_CONFIG"]
        assert result.exit_code == 0, result.output


# ── the ONLY write: vesma system config set --file ─────────────────────


class TestSystemConfigSet:
    def test_happy_first_install_no_provenance_header(
        self,
        system_prefix: Path,
        user_env: Path,
        tmp_path: Path,
        root_seams: list[str],
    ) -> None:
        """First install: validated bytes land atomically with the
        manifest mode; NO mover provenance header is added — the F9 adopt
        predicate keeps refusing operator-installed conffiles."""
        candidate = tmp_path / "candidate.yaml"
        candidate.write_text(_VALID_YAML, encoding="utf-8")
        result = runner.invoke(app, ["system", "config", "set", "--file", str(candidate)])
        assert result.exit_code == 0, result.output
        target = fhs.system_config_path()
        installed = target.read_text(encoding="utf-8")
        assert installed == _VALID_YAML
        assert _MOVER_HEADER not in installed
        assert "restart" in result.output
        assert "sudoedit" in result.output
        assert target.stat().st_mode & 0o777 == fhs.load_fhs_manifest().entry("shared_config").mode
        assert any(e == "chown:0:4242" for e in root_seams)

    def test_second_install_backs_up_previous_bytes(
        self, system_prefix: Path, user_env: Path, tmp_path: Path, root_seams: list[str]
    ) -> None:
        target = fhs.system_config_path()
        target.write_text(_VALID_YAML, encoding="utf-8")
        candidate = tmp_path / "candidate.yaml"
        candidate.write_text(_VALID_YAML.replace("9999", "7777"), encoding="utf-8")
        result = runner.invoke(app, ["system", "config", "set", "--file", str(candidate)])
        assert result.exit_code == 0, result.output
        backups = sorted(target.parent.glob("config.yaml.bak-*"))
        assert len(backups) == 1, "exactly one same-dir backup"
        assert backups[0].read_text(encoding="utf-8") == _VALID_YAML
        assert "9999" not in target.read_text(encoding="utf-8")
        assert "7777" in target.read_text(encoding="utf-8")

    def test_invalid_candidate_refused_tree_untouched(
        self, system_prefix: Path, user_env: Path, tmp_path: Path, root_seams: list[str]
    ) -> None:
        target = fhs.system_config_path()
        target.write_text(_VALID_YAML, encoding="utf-8")
        before = target.read_bytes()
        candidate = tmp_path / "candidate.yaml"
        candidate.write_text(_BROKEN_YAML, encoding="utf-8")
        result = runner.invoke(app, ["system", "config", "set", "--file", str(candidate)])
        assert result.exit_code == 1
        assert "refused" in result.output or "NOT valid" in result.output
        assert target.read_bytes() == before  # NOTHING was touched
        assert not list(target.parent.glob("config.yaml.bak-*")), "no backup either"

    def test_non_root_real_prefix_refused_with_the_canonical_hint(
        self, user_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real FHS prefix with euid != 0 → typed refusal naming THIS
        verb's canonical sudo spelling (the command never escalates)."""
        monkeypatch.setattr(si.os, "geteuid", lambda: 1000)
        candidate = tmp_path / "candidate.yaml"
        candidate.write_text(_VALID_YAML, encoding="utf-8")
        result = runner.invoke(app, ["system", "config", "set", "--file", str(candidate)])
        assert result.exit_code == 1
        assert "sudo vesma system config set" in _flat(result.output)

    def test_missing_config_root_refuses_with_install_hint(
        self, system_prefix: Path, user_env: Path, tmp_path: Path, root_seams: list[str]
    ) -> None:
        """`set` configures an INSTALLED profile — it never lays down the
        FHS tree itself (the config-root refusal names the install verb)."""
        fhs.system_config_root().rmdir()
        candidate = tmp_path / "candidate.yaml"
        candidate.write_text(_VALID_YAML, encoding="utf-8")
        result = runner.invoke(app, ["system", "config", "set", "--file", str(candidate)])
        assert result.exit_code == 1
        assert "sudo vesma system install" in result.output
