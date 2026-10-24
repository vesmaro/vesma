"""CLI topology FINAL (owner doctrine 2026-10-24, 7.0.0) — alias removal.

The no-aliases doctrine: ONE spelling per operation. This suite pins the
post-removal contract:

* ``vesma service components install|update|configs|status`` — the
  component registry home (the ONLY home now);
* ``vesma service start|stop|restart`` — the lifecycle home (``--all`` +
  topological order, wave B);
* every REMOVED top-level spelling (``vesma
  install|update|status|configs|start|stop|restart|memory|task-logs|logs``)
  answers with the standard typer error (exit 2) — no stubs, no
  redirects, not tab-completable;
* the removed FLAG forms (``self-update --yes``-class, ``doctor
  --fix/--paths``, ``add --file/--url``, ``recall --agent``, ``service
  <verb> --system``) are usage errors too;
* ``vesma doctor`` is the ONE doctor (the ``system doctor`` additional
  registration is gone); G2: under euid==0 doctor names the resolved
  config+data root loudly;
* ``vesma system uninstall`` — typed confirmation (G4): interactive
  ``[y/N]``, ``--yes`` for scripts, ``--dry-run`` for the phase-1 plan.

Test discipline mirrors test_cli_system_domain.py / test_cli_components.py:
XDG roots isolated, impl seams monkeypatched (no root, no systemd, no
PyPI, no live supervisor).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from typer.main import get_command
from typer.testing import CliRunner

import vesma.cli.components as components_cli
import vesma.cli.service as service_cli
import vesma.cli.system_cmd as system_cli
from vesma.cli.main import app
from vesma.service.install import bundled_manifest_path

runner = CliRunner()

#: Every top-level spelling the 7.0.0 removal wave deleted (the FINAL
#: tree, §3: «чего больше нет»).
REMOVED_L1_SPELLINGS = (
    "install",
    "update",
    "status",
    "configs",
    "start",
    "stop",
    "restart",
    "logs",
    "memory",
    "task-logs",
)


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


# ── registration topology (post-removal) ──────────────────────────────


class TestRegistrationTopology:
    def test_service_components_group_visible_with_four_verbs(self) -> None:
        click_root = get_command(app)
        group = click_root.commands["service"].commands["components"]
        assert set(group.commands) == {"install", "update", "configs", "status"}
        assert not group.hidden, "the components home must be visible"

    def test_removed_top_level_spellings_are_gone(self) -> None:
        """The doctrine: no hidden deprecated aliases remain — every
        removed spelling is NOT registered and answers exit 2 (the
        standard typer error, byte-for-byte the same as any unknown
        command)."""
        click_root = get_command(app)
        for name in REMOVED_L1_SPELLINGS:
            assert name not in click_root.commands, f"{name} must not be registered"
        for name in REMOVED_L1_SPELLINGS:
            result = runner.invoke(app, [name])
            assert result.exit_code == 2, (name, result.output)

    def test_no_hidden_deprecated_aliases_remain(self) -> None:
        """Zero hidden aliases: the ONLY hidden top-level command is the
        completion plumbing (``__complete`` — the engine the shell
        scripts call, not a deprecated spelling)."""
        click_root = get_command(app)
        hidden = {n for n, c in click_root.commands.items() if getattr(c, "hidden", False)}
        assert hidden == {"__complete"}, hidden

    def test_removed_spellings_not_completable(self, tmp_path: Path) -> None:
        """Gone from help → gone from completion: the engine's exact-match
        path must NOT answer a removed spelling (prefix completion already
        excludes non-registered commands)."""
        for name in ("task-logs", "update", "install"):
            exact = runner.invoke(app, ["__complete", name, "0"], env={"HOME": str(tmp_path)})
            assert not exact.output.startswith(f"{name}\t"), (name, exact.output)

    def test_l1_breadth_guard_still_green(self) -> None:
        """The ≥30 visible L1 guard (test_cli_help_depth) must survive the
        removal wave: the deleted spellings were hidden already, so the
        visible count only trades `logs` for `traces` (same count)."""
        click_root = get_command(app)
        visible = [n for n, c in click_root.commands.items() if not c.hidden]
        assert len(visible) >= 30


# ── the components home works (the ONLY home) ─────────────────────────


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

    def test_update_bare_refuses_with_new_spelling(self, components_home: Path) -> None:
        result = runner.invoke(app, ["service", "components", "update"])
        assert result.exit_code == 1, result.output
        flat = " ".join(result.output.split())
        assert "`vesma service components update`" in flat

    def test_configs_answers(self, components_home: Path) -> None:
        result = runner.invoke(app, ["service", "components", "configs"])
        assert result.exit_code == 0, result.output
        assert "Vesma component configs" in result.output


# ── the removed FLAG forms are usage errors ───────────────────────────


class TestRemovedFlagForms:
    @pytest.mark.parametrize(
        "argv",
        (
            ["self-update", "--yes"],
            ["self-update", "--check"],
            ["self-update", "--install-timer"],
            ["self-update", "--uninstall-timer"],
            ["doctor", "--fix"],
            ["doctor", "--paths"],
            ["add", "--url", "https://example.com/x"],
            ["add", "--file", "note.md"],
            ["recall", "--agent", "cli"],
        ),
    )
    def test_flag_form_is_a_usage_error(self, argv: list[str]) -> None:
        """Flags never replace subcommands — and since 7.0.0 they do not
        PARSE either: each removed flag form is the standard usage error
        (exit 2), pointing the operator at ``<cmd> -h``."""
        result = runner.invoke(app, argv)
        assert result.exit_code == 2, (argv, result.output)

    def test_service_system_flags_are_gone(self) -> None:
        """``service install/uninstall/update --system``: the flags left
        the surface entirely (the system profile is its own domain)."""
        click_root = get_command(app)
        service = click_root.commands["service"]
        for verb in ("install", "uninstall", "update"):
            options = {p.name for p in service.commands[verb].params}
            assert "system" not in options, f"--system must be gone from service {verb}"
        for argv in (
            ["service", "install", "--system"],
            ["service", "uninstall", "--system"],
            ["service", "update", "--system"],
        ):
            result = runner.invoke(app, argv)
            assert result.exit_code == 2, (argv, result.output)


# ── the ONE doctor + G2 root line ─────────────────────────────────────


class TestOneDoctor:
    def test_system_doctor_registration_is_gone(self) -> None:
        click_root = get_command(app)
        assert "doctor" not in click_root.commands["system"].commands

    def test_l1_doctor_still_answers(self, components_home: Path) -> None:
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code in (0, 1, 2), result.output

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


# ── integration memory (the ONE home) ─────────────────────────────────


class TestIntegrationMemory:
    def test_new_home_answers(self, components_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pin the surface with DETERMINISTIC detection: ambient harness
        discovery depends on the runner's real home/config (an earlier
        suite in a full run can leave detectable state behind), so the
        report is pinned against a patched-empty registry."""
        import vesma.cli.memory_status as ms_mod
        from vesma.cli.integration import TargetsConfig

        empty_cfg = TargetsConfig(targets=())
        monkeypatch.setattr(ms_mod, "load_targets", lambda config_path=None, home=None: empty_cfg)
        result = runner.invoke(app, ["integration", "memory"])
        assert result.exit_code == 0, result.output
        assert "No agent harnesses detected" in result.output
        assert result.stderr == ""


# ── wave B: --all + topological order on the service verbs ───────────


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


class TestFreshMachineNoticeSpelling:
    """Security spot-check P3-1: the fresh-machine registry notice («no
    installation found … install it with …») names the install spelling of
    the surface that shows it — the ``service components`` home teaches
    its own verb; the DEFAULT hints are canonical too (no dead spellings
    taught anywhere)."""

    def test_notice_names_the_components_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            components_cli.layout,
            "components_dir",
            lambda: tmp_path / "absent-components.d",
        )
        _, notice = components_cli._registry()
        assert notice is not None
        assert "`vesma service components install --all`" in notice

    def test_default_hints_teach_no_dead_spellings(self) -> None:
        """Every default hint parameter is the canonical spelling — grep-
        level guard against a future reintroduction of a removed one."""
        import inspect

        dead = ("`vesma install", "`vesma update", "`vesma status", "`vesma start")
        for fn in (
            components_cli._registry,
            components_cli._registry_or_fail,
            components_cli.status_command,
            components_cli.configs_command,
            components_cli._require_component,
            components_cli._lifecycle_targets,
        ):
            src = inspect.getsource(fn)
            for spelling in dead:
                assert spelling not in src, (fn.__name__, spelling)
