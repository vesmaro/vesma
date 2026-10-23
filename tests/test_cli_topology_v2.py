"""CLI topology v2 waves A+B (ArchCom 2026-10-23, all seats ACCEPT-with-amendments).

Two-sided pins for every moved verb — the NEW home works, the OLD spelling
keeps working byte-identically (hint on stderr, stdout clean, exit codes
unchanged; removal not before 9.0):

* ``vesma service components install|update|configs|status`` — the
  component registry home; the top-level spellings are hidden aliases;
* ``vesma service start|stop/restart`` gain ``--all`` + topological order
  (wave B); the top-level lifecycle spellings are hidden aliases calling
  the SAME functions;
* ``vesma system doctor`` — the SAME doctor app additionally registered
  under the system domain (visible); L1 ``vesma doctor`` stays primary;
  G2: under euid==0 doctor names the resolved config+data root loudly;
* ``vesma integration memory`` — the memory-status home; the top-level
  ``memory`` group is a hidden alias;
* ``vesma logs`` — the pipeline-trace viewer (renamed from task-logs);
  ``vesma logs NAME`` (component habit) gets a loud stderr redirect to
  ``vesma service logs NAME`` AND still runs the trace view;
* ``vesma update`` legacy self-update FLAG dispatch (installed systemd
  timers depend on it) still dispatches inside the alias;
* ``vesma system uninstall`` — typed confirmation (G4): interactive
  ``[y/N]``, ``--yes`` for scripts, ``--dry-run`` for the phase-1 plan.

Test discipline mirrors test_cli_system_domain.py / test_cli_components.py:
XDG roots isolated, impl seams monkeypatched (no root, no systemd, no
PyPI, no live supervisor).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.main import get_command
from typer.testing import CliRunner

import vesma.cli.components as components_cli
import vesma.cli.service as service_cli
import vesma.cli.system_cmd as system_cli
from vesma.cli.main import app
from vesma.service.install import bundled_manifest_path

runner = CliRunner()


@pytest.fixture
def components_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated XDG home with an INSTALLED components.d (board+metrics)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
        "VESMA_CONFIG",
    ):
        monkeypatch.delenv(var, raising=False)
    components = home / ".config" / "vesma" / "components.d"
    components.mkdir(parents=True)
    for name in ("board", "metrics"):
        (components / f"{name}.yaml").write_bytes(bundled_manifest_path(name).read_bytes())
    return home


def _stub_with_client(monkeypatch: pytest.MonkeyPatch, result: str = "done") -> None:
    """Stub the control-plane seam: every lifecycle request 'succeeds'."""

    def fake_with_client(socket: Path | None, action: Callable[[object], object]) -> str:
        return result

    monkeypatch.setattr(components_cli, "_with_client", fake_with_client)


# ── wave A: registration topology ─────────────────────────────────────


class TestRegistrationTopology:
    def test_service_components_group_visible_with_four_verbs(self) -> None:
        click_root = get_command(app)
        group = click_root.commands["service"].commands["components"]
        assert set(group.commands) == {"install", "update", "configs", "status"}
        assert not group.hidden, "the components home must be visible"

    def test_eight_top_level_aliases_hidden(self) -> None:
        """Hidden from --help (removal not before 9.0) — but still registered."""
        click_root = get_command(app)
        for name in ("install", "update", "status", "configs", "start", "stop", "restart"):
            assert name in click_root.commands, f"alias {name} disappeared"
            assert click_root.commands[name].hidden, f"{name} must be hidden"

    def test_memory_group_and_task_logs_alias_hidden(self) -> None:
        click_root = get_command(app)
        assert click_root.commands["memory"].hidden
        assert click_root.commands["task-logs"].hidden

    def test_l1_breadth_guard_still_green(self) -> None:
        """The ≥30 visible L1 guard (test_cli_help_depth) must survive the
        wave: 45 visible before, 9 spellings hidden, `logs` stays = 36."""
        click_root = get_command(app)
        visible = [n for n, c in click_root.commands.items() if not c.hidden]
        assert len(visible) >= 30

    def test_hidden_aliases_still_completable(self, tmp_path: Path) -> None:
        """Hidden ≠ gone: the completion engine's exact-match path answers
        the alias name (prefix completion excludes hidden — the engine
        contract pinned by test_completion for __complete itself)."""
        result = runner.invoke(app, ["__complete", "task-logs", "0"], env={"HOME": str(tmp_path)})
        assert result.exit_code == 0
        assert result.output.startswith("task-logs\t"), result.output
        for verb in ("install", "update", "status", "configs", "start", "stop", "restart"):
            exact = runner.invoke(app, ["__complete", verb, "0"], env={"HOME": str(tmp_path)})
            assert exact.output.startswith(f"{verb}\t"), (verb, exact.output)


# ── wave A: the new components home works ─────────────────────────────


class TestServiceComponentsHome:
    def test_status_answers(self, components_home: Path) -> None:
        result = runner.invoke(app, ["service", "components", "status"])
        assert result.exit_code == 0, result.output
        assert "Vesma components" in result.output
        assert result.stderr == ""

    def test_install_bare_lists_with_new_spelling(self, components_home: Path) -> None:
        result = runner.invoke(app, ["service", "components", "install"])
        assert result.exit_code == 0, result.output
        assert "vesma service components install NAME" in result.output
        assert "vesma service components install --all" in result.output
        assert "vesma install NAME" not in result.output, "no deprecated spellings taught"

    def test_update_bare_refuses_with_new_spelling(self, components_home: Path) -> None:
        result = runner.invoke(app, ["service", "components", "update"])
        assert result.exit_code == 1, result.output
        flat = " ".join(result.output.split())
        assert "`vesma service components update`" in flat

    def test_configs_answers(self, components_home: Path) -> None:
        result = runner.invoke(app, ["service", "components", "configs"])
        assert result.exit_code == 0, result.output
        assert "Vesma component configs" in result.output


# ── wave A: two-sided alias pins ──────────────────────────────────────


class TestAliasesTwoSided:
    @pytest.mark.parametrize(
        ("alias_args", "home_args"),
        (
            (["status"], ["service", "components", "status"]),
            (["configs"], ["service", "components", "configs"]),
            (["status", "--json"], ["service", "components", "status", "--json"]),
        ),
    )
    def test_registry_verbs_stdout_identical(
        self, components_home: Path, alias_args: list[str], home_args: list[str]
    ) -> None:
        """Old spelling: stdout BYTE-IDENTICAL to the new home + hint on
        stderr only; new home: clean stderr."""
        alias = runner.invoke(app, alias_args)
        home = runner.invoke(app, home_args)
        assert alias.exit_code == home.exit_code == 0, (alias.output, home.output)
        assert alias.stdout == home.stdout, "the alias must not change stdout"
        assert "[deprecated]" in alias.stderr
        assert "deprecated" not in alias.stdout
        assert home.stderr == ""

    @pytest.mark.parametrize("verb", ["start", "stop", "restart"])
    def test_lifecycle_alias_same_function_output(
        self, components_home: Path, monkeypatch: pytest.MonkeyPatch, verb: str
    ) -> None:
        """G5 same-function: `vesma <verb> board` prints exactly what
        `vesma service <verb> board` prints (the alias only adds stderr)."""
        _stub_with_client(monkeypatch)
        alias = runner.invoke(app, [verb, "board"])
        home = runner.invoke(app, ["service", verb, "board"])
        assert alias.exit_code == home.exit_code == 0, (alias.output, home.output)
        assert alias.stdout == home.stdout
        assert f"use: vesma service {verb}" in alias.stderr
        assert alias.stderr != ""

    def test_install_alias_keeps_dual_dispatch_hint(self, components_home: Path) -> None:
        """The install alias hint names BOTH targets (component vs user
        profile); the bare listing keeps its frozen old wording."""
        result = runner.invoke(app, ["install"])
        assert result.exit_code == 0, result.output
        assert "vesma service components install (component)" in result.stderr
        assert "vesma service install (user profile)" in result.stderr
        assert "vesma install NAME" in result.stdout, "frozen pre-wave wording"

    def test_update_alias_hint_is_profile_aware(self, components_home: Path) -> None:
        """Product amendment: the update alias hint names the components
        home AND the application self-update family."""
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 1, result.output
        assert "vesma service components update" in result.stderr
        assert "vesma self-update" in result.stderr


# ── wave A: the vesma update legacy self-update flags (timer pin) ─────


class TestUpdateLegacyFlagPin:
    """Installed systemd timers call `vesma update --yes` / `--check` /
    `--to` / `--scope` / `--install-timer` / `--uninstall-timer` — every
    flag MUST keep dispatching to the self-update machinery."""

    @pytest.mark.parametrize(
        ("flag_args", "expected"),
        (
            (["--check"], {"check": True, "yes": False, "install_timer": False}),
            (["--yes"], {"check": False, "yes": True, "install_timer": False}),
            (["--verbose"], {"check": False, "yes": False, "verbose": True}),
            (["--to", "9.9.9"], {"check": False, "yes": False, "to": "9.9.9"}),
            (["--scope", "user"], {"check": False, "yes": False, "scope": "user"}),
            (["--install-timer"], {"check": False, "yes": False, "install_timer": True}),
            (["--uninstall-timer"], {"check": False, "yes": False, "uninstall_timer": True}),
        ),
    )
    def test_each_legacy_flag_dispatches(
        self,
        components_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        flag_args: list[str],
        expected: dict[str, Any],
    ) -> None:
        recorded: dict[str, Any] = {}

        def fake_dispatch(console: object, **kwargs: Any) -> None:
            recorded.update(kwargs)

        import vesma.cli.update_cmd as update_cmd

        monkeypatch.setattr(update_cmd, "_legacy_flag_dispatch", fake_dispatch)
        result = runner.invoke(app, ["update", *flag_args])
        assert result.exit_code == 0, result.output
        for key, value in expected.items():
            assert recorded.get(key) == value, (flag_args, key, recorded)
        # The alias hint fires too — stderr only.
        assert "[deprecated] `vesma update`" in result.stderr

    def test_legacy_flag_with_name_still_refuses(self, components_home: Path) -> None:
        result = runner.invoke(app, ["update", "--check", "board"])
        assert result.exit_code == 1, result.output
        assert "belongs to `vesma self-update`" in result.output


# ── wave A: system doctor (same app, two doors) + G2 root line ────────


class TestSystemDoctor:
    def test_system_doctor_is_the_same_pass(
        self, components_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`vesma system doctor` and `vesma doctor` run the SAME checks —
        pinned at the seam: one shared invocation counter."""
        import vesma.cli.doctor as doctor_cli

        calls: list[int] = []
        original = doctor_cli._run_all_checks

        def counting() -> list[object]:
            calls.append(1)
            return original()

        monkeypatch.setattr(doctor_cli, "_run_all_checks", counting)
        via_system = runner.invoke(app, ["system", "doctor"])
        via_l1 = runner.invoke(app, ["doctor"])
        assert via_system.exit_code == via_l1.exit_code
        assert len(calls) == 2

    def test_g2_root_line_names_resolved_roots(
        self, components_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """G2: under euid==0 the doctor banner names the config + data root
        it is diagnosing — one loud line, stdout, never in --json. The
        banner text is matched whitespace-normalized: rich wraps long lines
        by console width, and the pin must not depend on the wrap point."""
        monkeypatch.setattr("vesma.cli.doctor.os.geteuid", lambda: 0)
        result = runner.invoke(app, ["doctor"])
        assert result.output.count("root shell") == 1, "exactly ONE loud line"
        flat = " ".join(result.output.split())
        assert "config:" in flat and "data root:" in flat
        assert "/etc/vesma" in flat, "the system-profile boundary is named"

    def test_g2_line_absent_for_non_root(
        self, components_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("vesma.cli.doctor.os.geteuid", lambda: 1000)
        result = runner.invoke(app, ["doctor"])
        assert "root shell" not in result.output

    def test_g2_line_absent_in_json(
        self, components_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("vesma.cli.doctor.os.geteuid", lambda: 0)
        result = runner.invoke(app, ["doctor", "--json"])
        assert "root shell" not in result.output


# ── wave A: integration memory + the memory alias ─────────────────────


class TestIntegrationMemory:
    def test_new_home_answers(self, components_home: Path) -> None:
        result = runner.invoke(app, ["integration", "memory"])
        assert result.exit_code == 0, result.output
        assert "No agent harnesses detected" in result.output
        assert result.stderr == ""

    def test_memory_alias_same_stdout_plus_hint(self, components_home: Path) -> None:
        alias = runner.invoke(app, ["memory", "status"])
        home = runner.invoke(app, ["integration", "memory"])
        assert alias.exit_code == home.exit_code == 0
        assert alias.stdout == home.stdout
        assert "use: vesma integration memory" in alias.stderr
        assert "deprecated" not in alias.stdout


# ── wave A: logs rename (traces) + the component redirect ─────────────


class TestLogsRedirect:
    def _isolate_config(self, monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
        cfg = home / "vesma.yaml"
        cfg.write_text(
            f"vesma:\n  vault_path: {home}/vault\n  data_dir: {home}/data\n"
            "  db_name: t.db\nembedding:\n  provider: nano\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("VESMA_CONFIG", str(cfg))

    def test_component_positional_redirects_and_still_runs(
        self, components_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._isolate_config(monkeypatch, components_home)
        result = runner.invoke(app, ["logs", "board"])
        assert result.exit_code == 0, result.output
        assert "component logs moved" in result.stderr
        assert "`vesma service logs board`" in result.stderr
        assert "No traces found" in result.output, "no silent substitution"

    def test_non_component_positional_is_usage_error(
        self, components_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._isolate_config(monkeypatch, components_home)
        result = runner.invoke(app, ["logs", "not-a-component"])
        assert result.exit_code == 2, result.output
        assert "unexpected extra argument" in result.output


# ── wave B: --all + topological order on the service verbs ────────────


class TestServiceAllLifecycle:
    def test_start_all_dependency_order(
        self, components_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Parsed from the printed per-target lines (the wave-61 pin
        discipline: the order is observable in the output)."""
        _stub_with_client(monkeypatch)
        result = runner.invoke(app, ["service", "start", "--all"])
        assert result.exit_code == 0, result.output
        printed = [line.split(":")[0] for line in result.output.splitlines() if ":" in line]
        assert printed.index("board") < printed.index("metrics")

    def test_stop_all_reverse_dependency_order(
        self, components_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stub_with_client(monkeypatch)
        result = runner.invoke(app, ["service", "stop", "--all"])
        assert result.exit_code == 0, result.output
        printed = [line.split(":")[0] for line in result.output.splitlines() if ":" in line]
        assert printed.index("metrics") < printed.index("board")

    def test_bare_service_verb_refuses_naming_service_spelling(self, components_home: Path) -> None:
        result = runner.invoke(app, ["service", "start"])
        assert result.exit_code == 1, result.output
        flat = " ".join(result.output.split())
        assert "`vesma service start`" in flat, "the refusal teaches the new spelling"

    def test_nothing_installed_all_hint_names_components_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "fresh"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        for var in (
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
            "XDG_STATE_HOME",
            "XDG_CACHE_HOME",
            "XDG_RUNTIME_DIR",
        ):
            monkeypatch.delenv(var, raising=False)
        result = runner.invoke(app, ["service", "start", "--all"])
        assert result.exit_code == 1, result.output
        assert "vesma service components install --all" in result.output


# ── wave B: G4 — typed confirmation on system uninstall ───────────────


class TestSystemUninstallConfirm:
    def _fake(self, monkeypatch: pytest.MonkeyPatch, calls: list[bool]) -> None:
        from vesma.service import system_install as si

        def fake(*, dry_run: bool = False) -> si.SystemUninstallResult:
            calls.append(dry_run)
            return si.SystemUninstallResult(lines=("removed: x", "preserved (operator data): y"))

        monkeypatch.setattr(system_cli, "uninstall_system", fake)

    def test_yes_dispatches_without_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[bool] = []
        self._fake(monkeypatch, calls)
        result = runner.invoke(system_cli.system_app, ["uninstall", "--yes"])
        assert result.exit_code == 0, result.output
        assert calls == [False]
        assert "Uninstall complete" in result.output

    def test_declined_prompt_aborts_without_dispatch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[bool] = []
        self._fake(monkeypatch, calls)
        result = runner.invoke(system_cli.system_app, ["uninstall"], input="n\n")
        assert result.exit_code == 0, result.output
        assert calls == [], "a declined confirmation must never dispatch"
        assert "Aborted — nothing was removed" in result.output

    def test_eof_stdin_aborts_loudly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Scripts without --yes and without a TTY must not uninstall."""
        calls: list[bool] = []
        self._fake(monkeypatch, calls)
        result = runner.invoke(system_cli.system_app, ["uninstall"], input="")
        assert result.exit_code == 1, result.output
        assert calls == []
        assert "No confirmation received" in result.output

    def test_dry_run_shows_plan_without_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[bool] = []
        self._fake(monkeypatch, calls)
        result = runner.invoke(system_cli.system_app, ["uninstall", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert calls == [True], "the impl seam must see dry_run=True"
        assert "Dry-run complete — nothing was removed." in result.output


# ── wave B: service logs Text-safety + empty-source note ──────────────


class TestServiceLogsPort:
    def _stub_client(self, monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> None:
        class FakeClient:
            def logs(self, name: str, tail: int) -> list[str]:
                return lines

            def close(self) -> None:
                pass

        monkeypatch.setattr(service_cli, "_open_client", lambda s: FakeClient())

    def test_markup_renders_literally(
        self, components_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Security F1: a `[red]` line from a component prints LITERALLY."""
        self._stub_client(monkeypatch, ["[red]evil[/red] plain"])
        result = runner.invoke(app, ["service", "logs", "board"])
        assert result.exit_code == 0, result.output
        assert "[red]evil[/red]" in result.output

    def test_empty_output_names_the_source(
        self, components_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._stub_client(monkeypatch, [])
        result = runner.invoke(app, ["service", "logs", "board"])
        assert result.exit_code == 0, result.output
        assert "no log lines available for board" in result.output
        assert "source: supervisor ring buffer" in result.output
