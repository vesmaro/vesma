"""Top-level component verbs (wave 61 component UX) — verb x component matrix.

The unified surface: ``vesma install|status|start|stop|restart|logs|update|
configs [NAME]``. Covered here, per the wave acceptance matrix:

* bare calls never bulk-mutate (install lists; start/stop/restart/update
  refuse with the component list + the --all hint; logs is a usage error);
* NAME semantics: bundled manifests install/regenerate from the release
  bundle behind a one-time ``.pre-regen.bak`` backup, operator-authored
  manifests are validated but NEVER rewritten ("managed by operator"),
  unknown names refuse with the installed list;
* ``logs`` empty output says so explicitly with the source named (the P2
  cli-audit 2026-10-08 finding: `service logs` printed NOTHING);
* collision resolution: ``vesma self-update`` / ``vesma task-logs`` are
  live aliases, and the legacy self-update flag forms still delegate.

Test discipline: XDG roots isolated into tmp dirs, the supervisor is
NEVER contacted live — lifecycle verbs run against a bogus socket path
(error-path) or a stubbed ``_with_client`` seam; no PyPI, no systemd.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vesma.cli.components import _lifecycle_targets, _registry, _topo_order
from vesma.cli.main import app
from vesma.service.install import bundled_manifest_path
from vesma.service.manifest import ComponentManifest

runner = CliRunner()

#: Minimal VALID operator-authored manifest (schema-checked by the real
#: loader in tests via load_installation) — the shape of the owner's Go
#: sidecar mesh.yaml, in-process so no venv leg exists.
MESH_MANIFEST = """\
apiVersion: vesma.component/v1
kind: in-process
metadata:
  name: mesh
  version: 0.4.2
  tier: optional
  description: "Operator-authored sidecar (owner's Go component) - test fixture shape."
  provenance:
    repo: https://example.invalid/mesh
    license: MIT
in_process:
  module: vesma.service.board
  entrypoint: create_component
  python:
    version: ">=3.11"
depends_on: [board]
"""


@pytest.fixture
def components_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated XDG home with an INSTALLED components.d (bundle + operator)."""
    home = tmp_path / "home"
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
    components = home / ".config" / "vesma" / "components.d"
    components.mkdir(parents=True)
    for name in ("board", "metrics"):
        (components / f"{name}.yaml").write_bytes(bundled_manifest_path(name).read_bytes())
    (components / "mesh.yaml").write_text(MESH_MANIFEST, encoding="utf-8")
    return components


@pytest.fixture
def bundle_only_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated XDG home with NO components.d — the fresh-machine view."""
    home = tmp_path / "home"
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
    return home


# ── install ───────────────────────────────────────────────────────────


class TestInstall:
    def test_bare_lists_installables_and_hints(self, components_home: Path) -> None:
        result = runner.invoke(app, ["install"])
        assert result.exit_code == 0, result.output
        for needle in ("board", "metrics", "mesh", "bundled", "operator", "--all"):
            assert needle in result.output, f"bare install must list {needle}"

    def test_bare_mutates_nothing(self, components_home: Path) -> None:
        before = {p.name: p.read_bytes() for p in components_home.iterdir()}
        runner.invoke(app, ["install"])
        after = {p.name: p.read_bytes() for p in components_home.iterdir()}
        assert before == after, "bare `vesma install` must not touch components.d"

    def test_bundled_installs_bundle_bytes(self, bundle_only_home: Path) -> None:
        target = bundle_only_home / ".config" / "vesma" / "components.d" / "board.yaml"
        result = runner.invoke(app, ["install", "board"])
        assert result.exit_code == 0, result.output
        assert target.read_bytes() == bundled_manifest_path("board").read_bytes()

    def test_bundled_is_idempotent(self, components_home: Path) -> None:
        target = components_home / "board.yaml"
        for _ in range(2):
            result = runner.invoke(app, ["install", "board"])
            assert result.exit_code == 0, result.output
        assert target.read_bytes() == bundled_manifest_path("board").read_bytes()
        assert not list(components_home.parent.glob("*.pre-regen.bak")), (
            "an up-to-date manifest must not produce a backup"
        )

    def test_bundled_divergent_backs_up_then_regenerates(self, components_home: Path) -> None:
        target = components_home / "board.yaml"
        operator_edit = "apiVersion: vesma.component/v1\nmetadata:\n  name: board\n"
        target.write_text(operator_edit, encoding="utf-8")
        result = runner.invoke(app, ["install", "board"])
        assert result.exit_code == 0, result.output
        backup = components_home.parent / "board.yaml.pre-regen.bak"
        assert backup.read_text(encoding="utf-8") == operator_edit
        assert target.read_bytes() == bundled_manifest_path("board").read_bytes()

    def test_operator_authored_validated_not_touched(self, components_home: Path) -> None:
        target = components_home / "mesh.yaml"
        before = target.read_bytes()
        result = runner.invoke(app, ["install", "mesh"])
        assert result.exit_code == 0, result.output
        assert "operator-authored" in result.output
        assert target.read_bytes() == before, "install must never rewrite operator manifests"

    def test_unknown_name_refuses(self, components_home: Path) -> None:
        result = runner.invoke(app, ["install", "ghost"])
        assert result.exit_code == 1, result.output
        assert "ghost" in result.output and "bundled" in result.output

    def test_name_and_all_are_exclusive(self, components_home: Path) -> None:
        result = runner.invoke(app, ["install", "board", "--all"])
        assert result.exit_code == 1, result.output
        assert "not both" in result.output

    def test_python_child_gets_venv_pointer(self, components_home: Path) -> None:
        result = runner.invoke(app, ["install", "metrics"])
        assert result.exit_code == 0, result.output
        assert "vesma service install" in result.output, (
            "a venv-needing component must be told where venvs are built"
        )

    def test_oserror_renders_as_clean_refusal(
        self, components_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P3-2 (cascade): a disk-level OSError is one red line, not a
        traceback — same refusal shape as the typed errors."""

        def raise_disk_full(name: str) -> object:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr("vesma.cli.components.install_component", raise_disk_full)
        result = runner.invoke(app, ["install", "board"])
        assert result.exit_code == 1, result.output
        assert "No space left on device" in result.output
        assert "Traceback" not in result.output


# ── status ────────────────────────────────────────────────────────────


class TestStatus:
    def test_table_lists_every_component_with_origin(self, components_home: Path) -> None:
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        for needle in ("board", "metrics", "mesh", "bundled", "operator"):
            assert needle in result.output
        assert "unknown (supervisor not reachable)" in result.output, (
            "with no supervisor the live column degrades EXPLICITLY"
        )

    def test_json_shape(self, components_home: Path) -> None:
        result = runner.invoke(app, ["status", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["supervisor"]["reachable"] is False
        names = {row["name"] for row in payload["components"]}
        assert names == {"board", "metrics", "mesh"}
        origins = {row["name"]: row["origin"] for row in payload["components"]}
        assert origins == {
            "board": "bundled",
            "metrics": "bundled",
            "mesh": "operator",
        }
        for row in payload["components"]:
            assert row["manifest_path"], f"{row['name']} must carry its manifest path"
            assert row["installed"] is True

    def test_single_component(self, components_home: Path) -> None:
        result = runner.invoke(app, ["status", "mesh"])
        assert result.exit_code == 0, result.output
        assert "mesh" in result.output and "board" not in result.output.split("mesh")[0]

    def test_unknown_refuses(self, components_home: Path) -> None:
        result = runner.invoke(app, ["status", "ghost"])
        assert result.exit_code == 1, result.output

    def test_fresh_machine_shows_bundle(self, bundle_only_home: Path) -> None:
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        assert "not installed" in result.output
        assert "no installation found" in result.output

    def test_bundle_fallback_live_state_wins(
        self, bundle_only_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P2-2 (cascade): `service run` without components.d runs the
        BUNDLE — a live board must show its real state marked "bundle
        fallback", not the disk-derived "not installed"."""

        class FakeClient:
            def status(self, component: str | None) -> dict[str, object]:
                return {
                    "components": {"board": {"state": "running", "pid": 4242}},
                }

            def close(self) -> None:
                pass

        monkeypatch.setattr("vesma.cli.components._open_client", lambda socket: FakeClient())
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        assert "running (pid 4242) (bundle fallback)" in result.output, result.output
        assert result.output.count("not installed") == 1, (
            "metrics has no live entry — the disk label stands for it alone"
        )

    def test_bundle_fallback_live_state_in_json(
        self, bundle_only_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeClient:
            def status(self, component: str | None) -> dict[str, object]:
                return {
                    "components": {"board": {"state": "running", "pid": 4242}},
                }

            def close(self) -> None:
                pass

        monkeypatch.setattr("vesma.cli.components._open_client", lambda socket: FakeClient())
        result = runner.invoke(app, ["status", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["supervisor"]["reachable"] is True
        states = {row["name"]: row["state"] for row in payload["components"]}
        assert states["board"] == "running (pid 4242) (bundle fallback)"


# ── start / stop / restart ────────────────────────────────────────────


class TestLifecycleBare:
    @pytest.mark.parametrize("verb", ["start", "stop", "restart"])
    def test_bare_refuses_with_list_and_hint(self, components_home: Path, verb: str) -> None:
        result = runner.invoke(app, [verb])
        assert result.exit_code == 1, result.output
        assert "not a bulk mutation" in result.output
        assert "board, mesh, metrics" in result.output
        assert "specify NAME or --all" in result.output

    def test_unknown_name_refuses(self, components_home: Path) -> None:
        result = runner.invoke(app, ["start", "ghost"])
        assert result.exit_code == 1, result.output

    def test_bogus_socket_error_path(self, components_home: Path) -> None:
        result = runner.invoke(app, ["start", "board", "--socket", "/nonexistent/bogus.sock"])
        assert result.exit_code == 1, result.output
        assert "supervisor running?" in result.output, (
            "the thin-client error path must name its cause"
        )


class TestLifecycleBulk:
    def _stub_client(self, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
        calls: list[tuple[str, str]] = []

        def fake_with_client(socket: Path | None, action: Callable[[object], object]) -> str:
            return "running"

        monkeypatch.setattr("vesma.cli.components._with_client", fake_with_client)
        return calls

    def test_start_all_is_dependency_ordered(
        self, components_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[str] = []
        real_targets = _lifecycle_targets

        def spy(records: object, name: object, all_components: object, verb: str) -> list[str]:
            targets = real_targets(records, name, all_components, verb)  # type: ignore[arg-type]
            if verb != "stop":
                seen.extend(targets)
            return targets

        monkeypatch.setattr("vesma.cli.components._lifecycle_targets", spy)
        self._stub_client(monkeypatch)
        result = runner.invoke(app, ["start", "--all"])
        assert result.exit_code == 0, result.output
        assert seen.index("board") < seen.index("mesh"), "depends_on must launch before dependents"

    def test_stop_all_is_reverse_dependency_ordered(
        self, components_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_with_client(socket: Path | None, action: Callable[[object], object]) -> str:
            return "stopped"

        monkeypatch.setattr("vesma.cli.components._with_client", fake_with_client)
        result = runner.invoke(app, ["stop", "--all"])
        assert result.exit_code == 0, result.output
        printed = [line.split(":")[0] for line in result.output.splitlines() if ":" in line]
        assert printed.index("mesh") < printed.index("board"), (
            "stop must tear down dependents first"
        )

    def test_topo_order_unit(self) -> None:
        def manifest(name: str, deps: tuple[str, ...]) -> ComponentManifest:
            from vesma.service.manifest import Metadata, Provenance

            return ComponentManifest(
                path=Path(f"/tmp/{name}.yaml"),
                api_version="vesma.component/v1",
                kind="in-process",
                metadata=Metadata(
                    name=name,
                    version="1",
                    tier="optional",
                    description="-",
                    provenance=Provenance(repo="-", license="-"),
                ),
                depends_on=deps,
            )

        manifests = {
            "c": manifest("c", ("b",)),
            "a": manifest("a", ()),
            "b": manifest("b", ("a",)),
            "d": manifest("d", ("a", "c")),
        }
        order = _topo_order(manifests)
        assert order.index("a") < order.index("b") < order.index("c") < order.index("d")


# ── logs ──────────────────────────────────────────────────────────────


class TestLogs:
    def test_bare_is_usage_error_with_list(self, components_home: Path) -> None:
        result = runner.invoke(app, ["logs"])
        assert result.exit_code == 2, result.output
        assert "board, mesh, metrics" in result.output
        assert "task-logs" in result.output, "the alias hint must name the old surface"

    def test_empty_output_is_explicit(
        self, components_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_with_client(socket: Path | None, action: Callable[[object], object]) -> list[str]:
            return []

        monkeypatch.setattr("vesma.cli.components._with_client", fake_with_client)
        result = runner.invoke(app, ["logs", "board"])
        assert result.exit_code == 0, result.output
        assert "no log lines available for board" in result.output
        assert "source:" in result.output, "the empty message must name the source"

    def test_lines_are_printed(
        self, components_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fake_with_client(socket: Path | None, action: Callable[[object], object]) -> list[str]:
            return ["board line one", "board line two"]

        monkeypatch.setattr("vesma.cli.components._with_client", fake_with_client)
        result = runner.invoke(app, ["logs", "board", "--tail", "5"])
        assert result.exit_code == 0, result.output
        assert "board line one" in result.output and "board line two" in result.output

    def test_legacy_trace_flags_delegate(
        self, components_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded: dict[str, object] = {}

        def fake_logs_cmd(**kwargs: object) -> None:
            recorded.update(kwargs)

        monkeypatch.setattr("vesma.cli.components.logs_cmd", fake_logs_cmd)
        result = runner.invoke(app, ["logs", "--task", "cluster", "--limit", "7"])
        assert result.exit_code == 0, result.output
        assert recorded.get("task") == "cluster" and recorded.get("limit") == 7
        assert "task-logs" in result.output, "the deprecation hint must name the alias"

    def test_log_lines_render_as_data_not_markup(
        self, components_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Security F1 (cascade): a component's log text is DATA — a child
        emitting rich markup must not style the trusted CLI."""

        def fake_with_client(socket: Path | None, action: Callable[[object], object]) -> list[str]:
            return ["[red]injected[/red] plain line"]

        monkeypatch.setattr("vesma.cli.components._with_client", fake_with_client)
        result = runner.invoke(app, ["logs", "board"])
        assert result.exit_code == 0, result.output
        assert "[red]injected[/red]" in result.output, (
            "markup must reach the terminal verbatim (Text), not be interpreted"
        )


# ── update ────────────────────────────────────────────────────────────


class TestUpdate:
    def test_bare_refuses_and_names_self_update(self, components_home: Path) -> None:
        result = runner.invoke(app, ["update"])
        assert result.exit_code == 1, result.output
        assert "not a bulk mutation" in result.output
        assert "vesma self-update" in result.output

    def test_bundled_current_says_so(self, components_home: Path) -> None:
        result = runner.invoke(app, ["update", "board"])
        assert result.exit_code == 0, result.output
        assert "current" in result.output
        assert not list(components_home.parent.glob("*.pre-regen.bak"))

    def test_bundled_stale_regenerates_with_backup(self, components_home: Path) -> None:
        stale = components_home / "metrics.yaml"
        bundle = bundled_manifest_path("metrics").read_text(encoding="utf-8")
        stale.write_text(
            "".join(
                line
                for line in bundle.splitlines(keepends=True)
                if not line.strip().startswith(("requirements:", '- "vesma=='))
            ),
            encoding="utf-8",
        )
        result = runner.invoke(app, ["update", "metrics"])
        assert result.exit_code == 0, result.output
        assert "regenerated" in result.output
        assert "restart vesma.service" in result.output
        backup = components_home.parent / "metrics.yaml.pre-regen.bak"
        assert backup.exists(), "the previous bytes must be preserved once"
        assert stale.read_bytes() == bundled_manifest_path("metrics").read_bytes()

    def test_operator_authored_refuses(self, components_home: Path) -> None:
        result = runner.invoke(app, ["update", "mesh"])
        assert result.exit_code == 1, result.output
        assert "managed by operator" in result.output
        assert "edit the manifest file" in result.output
        assert (components_home / "mesh.yaml").read_text(encoding="utf-8") == MESH_MANIFEST

    def test_unknown_refuses(self, components_home: Path) -> None:
        result = runner.invoke(app, ["update", "ghost"])
        assert result.exit_code == 1, result.output

    def test_all_regenerates_stale_bundled(self, components_home: Path) -> None:
        stale = components_home / "metrics.yaml"
        stale.write_text(
            "apiVersion: vesma.component/v1\nmetadata:\n  name: metrics\n", encoding="utf-8"
        )
        result = runner.invoke(app, ["update", "--all"])
        assert result.exit_code == 0, result.output
        assert "regenerated" in result.output
        assert stale.read_bytes() == bundled_manifest_path("metrics").read_bytes()
        assert (components_home / "mesh.yaml").read_text(encoding="utf-8") == MESH_MANIFEST, (
            "--all must never rewrite operator manifests"
        )

    def test_legacy_yes_form_delegates_to_self_update(
        self, components_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded: dict[str, object] = {}

        def fake_dispatch(console: object, **kwargs: object) -> None:
            recorded.update(kwargs)

        monkeypatch.setattr("vesma.cli.update_cmd._legacy_flag_dispatch", fake_dispatch)
        result = runner.invoke(app, ["update", "--yes", "--scope=user"])
        assert result.exit_code == 0, result.output
        assert recorded.get("yes") is True and recorded.get("scope") == "user", (
            "the shipped systemd unit's ExecStart form must keep applying"
        )

    def test_legacy_flag_with_name_refuses_loudly(
        self, components_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P2-1 (cascade): `vesma update board --yes` must NOT silently
        swallow the NAME and run the APPLICATION self-update — it refuses
        naming both spellings instead."""

        def boom(console: object, **kwargs: object) -> None:
            raise AssertionError("legacy dispatch must not run for NAME + legacy flag")

        monkeypatch.setattr("vesma.cli.update_cmd._legacy_flag_dispatch", boom)
        result = runner.invoke(app, ["update", "board", "--yes"])
        assert result.exit_code == 1, result.output
        assert "--yes" in result.output and "vesma self-update" in result.output
        assert "vesma update board" in result.output, (
            "the refusal must spell out BOTH unambiguous spellings"
        )

    def test_timer_alias_delegates(
        self, components_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recorded: dict[str, object] = {}

        def fake_dispatch(console: object, **kwargs: object) -> None:
            recorded.update(kwargs)

        monkeypatch.setattr("vesma.cli.update_cmd._legacy_flag_dispatch", fake_dispatch)
        result = runner.invoke(app, ["update", "--timer"])
        assert result.exit_code == 0, result.output
        assert recorded.get("install_timer") is True


# ── configs ───────────────────────────────────────────────────────────


class TestConfigs:
    def test_table_lists_every_component(self, components_home: Path) -> None:
        result = runner.invoke(app, ["configs"])
        assert result.exit_code == 0, result.output
        for needle in ("board", "metrics", "mesh", "components.d", "env"):
            assert needle in result.output

    def test_json_shape(self, components_home: Path) -> None:
        result = runner.invoke(app, ["configs", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["config_path"].endswith("vesma.yaml")
        rows = {row["name"]: row for row in payload["components"]}
        assert set(rows) == {"board", "metrics", "mesh"}
        assert rows["mesh"]["config_schema"] == "none"
        assert rows["board"]["manifest_path"].endswith("board.yaml")

    def test_name_detail_shows_key_fields(self, components_home: Path) -> None:
        result = runner.invoke(app, ["configs", "board"])
        assert result.exit_code == 0, result.output
        for needle in ("config path:", "env file:", "data dir:", "config schema:"):
            assert needle in result.output
        assert "bind" in result.output and "log_level" in result.output, (
            "inline schema properties are the component's key config fields"
        )

    def test_unknown_refuses(self, components_home: Path) -> None:
        result = runner.invoke(app, ["configs", "ghost"])
        assert result.exit_code == 1, result.output


# ── collisions / aliases / registry invariants ────────────────────────


class TestSurface:
    def test_self_update_alias_is_live(self, bundle_only_home: Path) -> None:
        result = runner.invoke(app, ["self-update", "--help"])
        assert result.exit_code == 0, result.output
        assert "Usage:" in result.output

    def test_task_logs_alias_is_live(self, bundle_only_home: Path) -> None:
        result = runner.invoke(app, ["task-logs", "--help"])
        assert result.exit_code == 0, result.output
        assert "Usage:" in result.output

    def test_old_update_subcommand_spelling_hints_at_alias(self, components_home: Path) -> None:
        result = runner.invoke(app, ["update", "check"])
        assert result.exit_code == 1, result.output
        assert "self-update" in result.output, (
            "the moved subcommand spellings must point at their new home"
        )

    def test_origin_survives_operator_edit_on_bundled_file(self, components_home: Path) -> None:
        """Documented discriminator: origin = name ∈ BUNDLED_COMPONENTS.

        An operator edit on a bundled manifest makes it STALE (regenerated
        behind a backup on update), never operator-OWNED.
        """
        edited = (
            bundled_manifest_path("board")
            .read_text(encoding="utf-8")
            .replace("5.4.0-dev", "9.9.9-operator")
        )
        (components_home / "board.yaml").write_text(edited, encoding="utf-8")
        records, _notice = _registry()
        origins = {record.name: record.origin for record in records}
        assert origins["board"] == "bundled"
        assert origins["mesh"] == "operator"

    def test_registry_fails_closed_on_broken_manifest(self, components_home: Path) -> None:
        (components_home / "broken.yaml").write_text(
            "apiVersion: vesma.component/v1\nmetadata:\n  name: broken\n", encoding="utf-8"
        )
        from vesma.service.manifest import ManifestError

        with pytest.raises(ManifestError):
            _registry()
