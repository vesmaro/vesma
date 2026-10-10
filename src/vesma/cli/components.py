"""Top-level component verbs — ``vesma install|status|start|stop|restart|logs|update|configs``.

The owner-facing component UX (wave 61, board card vesma-61-wave): ONE
unified verb set that works for EVERY component — the built-in bundle
(``board`` / ``metrics``) and operator-authored external ones (any valid
manifest dropped into ``~/.config/vesma/components.d/``, e.g.
``mesh.yaml``) alike.

Design contract (TL, wave 61):

* THIN DELEGATION ONLY — every verb reuses the existing core: manifests
  via :mod:`vesma.service.install` (single-writer + ``.pre-regen.bak``
  backup discipline), lifecycle via the supervisor control client (the
  same ``vesma.cli.service`` plumbing ``vesma service start`` uses).
  Zero supervisor logic lives here.
* BARE CALLS NEVER BULK-MUTATE: ``start/stop/restart/update`` without a
  NAME refuse loudly (with the component list and the ``--all`` hint);
  ``install`` lists what is installable; ``logs`` is a usage error.
* ORIGIN DISCRIMINATOR (documented choice): a components.d file is
  BUNDLE-OWNED iff its name is one of
  :data:`vesma.service.install.BUNDLED_COMPONENTS` — the same rule the
  6.1.0 post-upgrade reconciliation applies. Anything else is
  operator-authored and NEVER rewritten by the engine; a byte-diff on a
  bundled manifest means "stale" (regenerated behind a ``.pre-regen.bak``
  backup), not "operator-owned".
* COLLISION RESOLUTION: ``vesma update`` was the application self-update
  group and ``vesma logs`` the pipeline-trace viewer — the component
  verbs win the top-level names, the old surfaces survive as aliases
  (``vesma self-update`` / ``vesma task-logs``), and the legacy FLAG
  forms the shipped systemd timer depends on keep working as hidden
  aliases on the new ``update`` verb (delegating to the self-update
  machinery with a stderr hint).

``status`` and ``configs`` degrade gracefully when the supervisor is
down — the registry view is disk-state and works offline; the live state
column says so explicitly instead of failing the whole command. Operator
data (paths, names, error text) is rendered through ``rich.text.Text``
— never passed through markup.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from vesma.cli.logs import logs_cmd
from vesma.cli.service import _open_client, _with_client
from vesma.service import layout
from vesma.service.client import ControlClient, ControlClientError
from vesma.service.errors import ManifestError
from vesma.service.install import (
    BUNDLED_COMPONENTS,
    InstallComponentResult,
    InstallError,
    install_component,
    regenerate_component_manifest,
    regenerate_stale_manifests,
)
from vesma.service.install import (
    install as install_service,
)
from vesma.service.manifest import (
    ComponentManifest,
    load_bundled_manifest,
    load_installation,
)

console = Console(width=200)

#: Origin labels for the status/configs tables and --json payloads.
ORIGIN_BUNDLED = "bundled"
ORIGIN_OPERATOR = "operator"

#: The log source named by the explicit empty-output message (the P2
#: cli-audit 2026-10-08 finding: `service logs` printed NOTHING on an
#: empty tail — silence is never an answer).
LOG_SOURCE_NOTE = (
    "source: supervisor ring buffer via the control socket — lines emitted "
    "since the supervisor started; the component may not have run yet"
)


@dataclasses.dataclass(frozen=True)
class ComponentRecord:
    """One registry row: a component as THIS machine knows it."""

    name: str
    origin: str  # ORIGIN_BUNDLED | ORIGIN_OPERATOR
    version: str  # manifest metadata.version, "-" when absent
    manifest: ComponentManifest | None  # None = bundled, not installed
    manifest_path: Path | None


# ── registry (disk state) ─────────────────────────────────────────────


def _registry() -> tuple[list[ComponentRecord], str | None]:
    """The component registry: installed manifests, or the bundle view.

    When ``components.d`` does not exist NOTHING is installed — the
    release bundle (board/metrics) is shown as the installable set with
    a notice, so bare ``status``/``install`` answer usefully on a fresh
    machine instead of failing. An EXISTING components.d loads
    fail-closed (a broken manifest is an error, never a silent skip).
    """
    components_dir = layout.components_dir()
    if not components_dir.is_dir():
        records = [
            ComponentRecord(
                name=name,
                origin=ORIGIN_BUNDLED,
                version=_bundle_version(name),
                manifest=None,
                manifest_path=None,
            )
            for name in BUNDLED_COMPONENTS
        ]
        notice = (
            f"no installation found ({components_dir} does not exist) — showing "
            "the release bundle; install it with `vesma install --all`"
        )
        return records, notice
    installation = load_installation(components_dir)
    records = [
        ComponentRecord(
            name=manifest.name,
            origin=ORIGIN_BUNDLED if manifest.name in BUNDLED_COMPONENTS else ORIGIN_OPERATOR,
            version=manifest.metadata.version or "-",
            manifest=manifest,
            manifest_path=manifest.path,
        )
        for manifest in sorted(installation.values(), key=lambda m: m.name)
    ]
    return records, None


def _registry_or_fail() -> tuple[list[ComponentRecord], str | None]:
    """``_registry`` with the fail-closed load rendered as a CLEAN refusal.

    A broken components.d must never surface as a traceback — the verbs
    that READ the registry answer with one actionable line. The ``update``
    verb deliberately does NOT go through here: healing a broken
    installation is its job (same warn-based discipline as the 6.1.0
    post-upgrade reconciliation).
    """
    try:
        return _registry()
    except ManifestError as exc:
        _fail(
            f"components.d fails fail-closed validation: {exc} — fix the "
            "manifests or re-run `vesma service install`"
        )


def _manifest_stems() -> list[str]:
    """Component names by file stem — NO validation, never raises.

    The light listing the ``update`` verb resolves targets with: a stale
    (schema-invalid) bundled manifest must still be reachable — updating
    is how it gets healed.
    """
    components_dir = layout.components_dir()
    if not components_dir.is_dir():
        return list(BUNDLED_COMPONENTS)
    stems = {path.stem for path in [*components_dir.glob("*.yaml"), *components_dir.glob("*.yml")]}
    return sorted(stems)


def _bundle_version(name: str) -> str:
    try:
        return load_bundled_manifest(name).metadata.version or "-"
    except ManifestError:
        return "-"


def _topo_order(manifests: dict[str, ComponentManifest]) -> list[str]:
    """Dependency-first order (``depends_on`` before dependents).

    Deterministic: ties break alphabetically. Cycles and dangling
    references are impossible here — ``load_installation`` already
    refuses them (DEPENDS_CYCLE / DEPENDS_MISSING).
    """
    order: list[str] = []
    seen: set[str] = set()

    def visit(name: str, stack: set[str]) -> None:
        if name in seen or name in stack:
            return
        stack.add(name)
        for dep in sorted(manifests[name].depends_on):
            if dep in manifests:
                visit(dep, stack)
        stack.discard(name)
        seen.add(name)
        order.append(name)

    for name in sorted(manifests):
        visit(name, set())
    return order


def _fail(message: str) -> NoReturn:
    """One red refusal line, exit 1 — typed errors never traceback."""
    console.print(Text(f"error: {message}"), style="red")
    raise typer.Exit(code=1) from None


def _names_line(records: list[ComponentRecord]) -> str:
    return ", ".join(record.name for record in records) or "nothing"


def _require_component(records: list[ComponentRecord], name: str) -> ComponentRecord:
    for record in records:
        if record.name == name:
            return record
    _fail(
        f"component {name!r} is not installed (installed: {_names_line(records)}) — "
        "see `vesma status` for the registry"
    )


# ── vesma install ─────────────────────────────────────────────────────


def install_command(
    name: Annotated[
        str | None,
        typer.Argument(
            help="Component name — a bundled one (board, metrics) or any operator "
            "manifest in components.d.",
            show_default=False,
        ),
    ] = None,
    all_components: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Install everything: manifests, per-component venvs and the "
            "systemd user unit (the full service-install flow).",
        ),
    ] = False,
) -> None:
    """Install components: manifests (NAME) or the whole installation (--all).

    With NAME: the per-component manifest install — bundled components
    are (re)written from this release's bundle (a diverging installed
    manifest is preserved as a one-time .pre-regen.bak backup),
    operator-authored manifests are validated fail-closed and left
    untouched, data dirs and config schemas are materialized. Idempotent.
    Component venvs and the systemd unit are installation-wide artifacts:
    `vesma install --all` (or `vesma service install`) owns them.

    Bare `vesma install` changes nothing — it lists what is installable
    and how to proceed.
    """
    if name is not None:
        if all_components:
            _fail("specify either a component NAME or --all, not both")
        try:
            result: InstallComponentResult = install_component(name)
        except (InstallError, ManifestError) as exc:
            _fail(str(exc))
        for line in result.lines:
            console.print("[green]✓[/green]", Text(line))
        console.print(f"Component {name} installed ({result.origin}).", style="green")
        return
    if all_components:
        try:
            full = install_service()
        except (InstallError, ManifestError) as exc:
            _fail(str(exc))
        for line in full.lines:
            downgraded = line.startswith("CONTAINER DOWNGRADE")
            style = "[yellow]⚠[/yellow]" if downgraded else "[green]✓[/green]"
            console.print(style, Text(line))
        console.print(f"Service installed ({full.unit_path}).", style="green")
        return
    # Bare: the installable set + how to proceed. Informational — exit 0.
    records, notice = _registry_or_fail()
    if notice is not None:
        console.print(f"note: {notice}", style="yellow")
    table = Table(title="Installable components", show_header=True, header_style="bold")
    table.add_column("Component")
    table.add_column("Origin")
    table.add_column("Version")
    table.add_column("State")
    for record in records:
        version = record.version if record.origin == ORIGIN_BUNDLED else "-"
        state = "installed" if record.manifest is not None else "not installed"
        table.add_row(record.name, record.origin, version, state)
    console.print(table)
    console.print("install one component:  vesma install NAME")
    console.print("install everything (manifests, venvs, unit):  vesma install --all")


# ── vesma status ──────────────────────────────────────────────────────


def _live_states(socket: Path | None, name: str | None) -> tuple[dict[str, Any] | None, str | None]:
    """Live component states from the supervisor, or ``(None, reason)``.

    The registry view is disk-state and answers offline; the live leg is
    best-effort BY DESIGN and its absence is reported, never hidden.
    """
    try:
        client = _open_client(socket)
    except ControlClientError as exc:
        return None, str(exc)
    try:
        payload = client.status(name)
    except ControlClientError as exc:
        return None, str(exc)
    finally:
        client.close()
    components = payload.get("components", {}) if isinstance(payload, dict) else {}
    return (components if isinstance(components, dict) else {}), None


def status_command(
    name: Annotated[
        str | None, typer.Argument(help="Optional single component.", show_default=False)
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Machine-readable registry + live states.")
    ] = False,
    socket: Annotated[
        Path | None, typer.Option("--socket", help="Control socket override (tests).")
    ] = None,
) -> None:
    """Show every component: origin, version, live state (or one component).

    The registry half is disk-state and works with the supervisor down;
    the live state (running/stopped/failed + pid) comes from the
    supervisor over its control socket and reads "unknown" with an
    explicit note when it does not answer. `--json` emits the same data
    machine-readably — the supervisor reachability travels INSIDE the
    payload, so pipes never parse prose.
    """
    records, notice = _registry_or_fail()
    if name is not None:
        records = [_require_component(records, name)]
    live: dict[str, Any] | None = None
    live_reason: str | None = None
    if all(record.manifest is not None for record in records):
        live, live_reason = _live_states(socket, name)

    if json_output:
        supervisor: dict[str, Any] = {"reachable": live is not None}
        if live_reason is not None:
            supervisor["reason"] = live_reason
        console.print_json(
            json.dumps(
                {
                    "supervisor": supervisor,
                    "components": [_status_json_row(record, live) for record in records],
                }
            )
        )
        return
    if notice is not None:
        console.print(f"note: {notice}", style="yellow")
    if live is None and live_reason is not None:
        console.print(f"live state unavailable: {live_reason}", style="dim")
    table = Table(title="Vesma components", show_header=True, header_style="bold")
    table.add_column("Component")
    table.add_column("Origin")
    table.add_column("Version")
    table.add_column("State")
    for record in records:
        table.add_row(record.name, record.origin, record.version, _state_cell(record, live))
    console.print(table)


def _state_cell(record: ComponentRecord, live: dict[str, Any] | None) -> str:
    if record.manifest is None:
        return "not installed"
    if live is None:
        return "unknown (supervisor not reachable)"
    entry = live.get(record.name)
    if not isinstance(entry, dict):
        return "not loaded"
    state = str(entry.get("state", "unknown"))
    pid = entry.get("pid")
    return f"{state} (pid {pid})" if pid else state


def _status_json_row(record: ComponentRecord, live: dict[str, Any] | None) -> dict[str, Any]:
    entry = live.get(record.name) if live else None
    pid = entry.get("pid") if isinstance(entry, dict) else None
    return {
        "name": record.name,
        "origin": record.origin,
        "version": record.version,
        "installed": record.manifest is not None,
        "state": _state_cell(record, live),
        "pid": pid,
        "manifest_path": str(record.manifest_path) if record.manifest_path else None,
    }


# ── vesma start / stop / restart ──────────────────────────────────────


def _lifecycle_targets(
    records: list[ComponentRecord], name: str | None, all_components: bool, verb: str
) -> list[str]:
    """The explicit target list — bare lifecycle calls never bulk-mutate."""
    if name is not None:
        if all_components:
            _fail("specify either a component NAME or --all, not both")
        return [_require_component(records, name).name]
    if not all_components:
        installed = [record.name for record in records if record.manifest is not None]
        _fail(
            f"refusing to {verb} without an explicit target — bare `vesma {verb}` "
            f"is not a bulk mutation (installed: {', '.join(installed) or 'nothing'}) "
            "— specify NAME or --all"
        )
    manifests = {record.name: record.manifest for record in records if record.manifest is not None}
    if not manifests:
        _fail("nothing installed — install components first (`vesma install --all`)")
    order = _topo_order(manifests)
    return list(reversed(order)) if verb == "stop" else order


def _lifecycle_action(
    verb: str, target: str, *, force: bool = False
) -> Callable[[ControlClient], Any]:
    """The one control-plane request for a lifecycle verb, typed for
    ``_with_client`` (a plain lambda does not survive mypy inference here
    and a loop-bound lambda is a B023 trap)."""

    def action(client: ControlClient) -> Any:
        if verb == "start":
            return client.start(target)
        if verb == "stop":
            return client.stop(target, force=force)
        return client.restart(target)

    return action


def start_command(
    name: Annotated[str | None, typer.Argument(help="Component name.", show_default=False)] = None,
    all_components: Annotated[
        bool, typer.Option("--all", help="Start every installed component (dependency order).")
    ] = False,
    socket: Annotated[
        Path | None, typer.Option("--socket", help="Control socket override (tests).")
    ] = None,
) -> None:
    """Start a component (or every one with --all) via the supervisor.

    Thin delegation to the same control-plane client `vesma service
    start` uses; idempotent — an already-running component is reported
    as-is. Bare `vesma start` refuses: name a component or pass --all.
    Check `vesma service health` afterwards to confirm readiness.
    """
    records, _notice = _registry_or_fail()
    for target in _lifecycle_targets(records, name, all_components, "start"):
        state = _with_client(socket, _lifecycle_action("start", target))
        console.print(f"{target}: {state}")


def stop_command(
    name: Annotated[str | None, typer.Argument(help="Component name.", show_default=False)] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Skip the graceful phase (SIGKILL after a short delay)."),
    ] = False,
    all_components: Annotated[
        bool,
        typer.Option("--all", help="Stop every installed component (reverse dependency order)."),
    ] = False,
    socket: Annotated[
        Path | None, typer.Option("--socket", help="Control socket override (tests).")
    ] = None,
) -> None:
    """Stop a component (or every one with --all) via the supervisor.

    Thin delegation to the same control-plane client `vesma service
    stop` uses; idempotent — an already-stopped component is reported
    as-is. `--force` skips the graceful phase (for a wedged process
    only). Bare `vesma stop` refuses: name a component or pass --all.
    """
    records, _notice = _registry_or_fail()
    for target in _lifecycle_targets(records, name, all_components, "stop"):
        state = _with_client(socket, _lifecycle_action("stop", target, force=force))
        console.print(f"{target}: {state}")


def restart_command(
    name: Annotated[str | None, typer.Argument(help="Component name.", show_default=False)] = None,
    all_components: Annotated[
        bool, typer.Option("--all", help="Restart every installed component (dependency order).")
    ] = False,
    socket: Annotated[
        Path | None, typer.Option("--socket", help="Control socket override (tests).")
    ] = None,
) -> None:
    """Restart a component (or every one with --all) via the supervisor.

    Thin delegation to the same control-plane client `vesma service
    restart` uses; a restart is a real stop-then-start cycle, NOT
    idempotent — a retry after a lost response performs a second
    restart. Bare `vesma restart` refuses: name a component or pass
    --all. Confirm readiness with `vesma service health`.
    """
    records, _notice = _registry_or_fail()
    for target in _lifecycle_targets(records, name, all_components, "restart"):
        state = _with_client(socket, _lifecycle_action("restart", target))
        console.print(f"{target}: {state}")


# ── vesma logs ────────────────────────────────────────────────────────


def logs_command(
    name: Annotated[str | None, typer.Argument(help="Component name.", show_default=False)] = None,
    follow: Annotated[
        bool, typer.Option("--follow", "-f", help="Stream new lines until the component stops.")
    ] = False,
    tail: Annotated[
        int, typer.Option("--tail", "-n", min=1, max=10000, help="Last N lines (<= 10000).")
    ] = 100,
    socket: Annotated[
        Path | None, typer.Option("--socket", help="Control socket override (tests).")
    ] = None,
    task: Annotated[
        str | None,
        typer.Option(
            "--task", "-t", hidden=True, help="Deprecated — pipeline traces moved to task-logs."
        ),
    ] = None,
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", hidden=True, help="Deprecated — use `vesma task-logs`."),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option("--since", hidden=True, help="Deprecated — use `vesma task-logs`."),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", "-l", hidden=True, help="Deprecated — use `vesma task-logs`."),
    ] = None,
    config: Annotated[
        str | None,
        typer.Option("--config", "-c", hidden=True, help="Deprecated — use `vesma task-logs`."),
    ] = None,
) -> None:
    """Show a component's recent logs; --follow streams until it stops.

    Prints the last --tail lines from the component's log source through
    the supervisor control socket; an EMPTY result says so explicitly
    with the source named — silence is never an answer. Bare `vesma
    logs` is a usage error listing the components. The former
    pipeline-trace viewer lives on as `vesma task-logs` (its
    --task/--project/--since/--limit flags keep working here as hidden
    aliases with a deprecation hint).
    """
    if task or project or since or limit is not None or config is not None:
        typer.echo("[deprecated] pipeline traces moved — use: vesma task-logs", err=True)
        logs_cmd(
            task=task,
            project=project,
            limit=limit if limit is not None else 50,
            since=since,
            follow=follow,
            config=config,
        )
        return
    records, _notice = _registry_or_fail()
    if name is None:
        console.print(
            Text(f"error: specify a component — installed: {_names_line(records)}"), style="red"
        )
        console.print("usage: vesma logs NAME [--tail N] [--follow]")
        console.print(
            "pipeline traces (the old bare `vesma logs`) moved to: `vesma task-logs`",
            style="dim",
        )
        raise typer.Exit(code=2) from None
    _require_component(records, name)
    if follow:
        _with_client(socket, lambda client: client.follow_logs(name, console.print, tail=tail))
        return
    lines = _with_client(socket, lambda client: client.logs(name, tail=tail))
    if not lines:
        console.print(f"no log lines available for {name} ({LOG_SOURCE_NOTE})", style="yellow")
        return
    for line in lines:
        console.print(line)


# ── vesma update ──────────────────────────────────────────────────────


def update_command(
    name: Annotated[
        str | None,
        typer.Argument(help="Component whose manifest to regenerate.", show_default=False),
    ] = None,
    all_components: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Regenerate every stale bundled manifest (operator-authored are never rewritten).",
        ),
    ] = False,
    check: Annotated[
        bool,
        typer.Option(
            "--check", hidden=True, help="Deprecated flag form — use: `vesma self-update check`."
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            "-y",
            hidden=True,
            help="Deprecated flag form — use: `vesma self-update apply`.",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose", hidden=True, help="Deprecated flag form — use: `vesma self-update apply`."
        ),
    ] = False,
    scope: Annotated[
        str | None,
        typer.Option(
            "--scope",
            hidden=True,
            help="Deprecated flag form — use: `vesma self-update apply --scope user`.",
        ),
    ] = None,
    to: Annotated[
        str | None,
        typer.Option(
            "--to",
            hidden=True,
            help="Deprecated flag form — use: `vesma self-update apply --to VERSION`.",
        ),
    ] = None,
    install_timer: Annotated[
        bool,
        typer.Option(
            "--install-timer",
            hidden=True,
            help="Deprecated flag form — use: `vesma self-update timer install`.",
        ),
    ] = False,
    uninstall_timer: Annotated[
        bool,
        typer.Option(
            "--uninstall-timer",
            hidden=True,
            help="Deprecated flag form — use: `vesma self-update timer uninstall`.",
        ),
    ] = False,
    timer_alias: Annotated[
        bool,
        typer.Option(
            "--timer",
            hidden=True,
            help="Deprecated flag form — use: `vesma self-update timer install`.",
        ),
    ] = False,
) -> None:
    """Update component manifests: regenerate bundled ones from this release.

    With NAME: a bundled component's manifest is regenerated when its
    bytes differ from this release's bundle (the previous bytes are kept
    as a one-time .pre-regen.bak backup); an operator-authored manifest
    is REFUSED — managed by operator, edit the file. With --all: every
    stale bundled manifest at once. Bare `vesma update` refuses — it is
    never a bulk mutation. The application self-update family moved to
    `vesma self-update`; its old flag forms (--yes, --check, --to,
    --scope, --install-timer, --uninstall-timer) still work here as
    hidden aliases so installed units and scripts keep running.
    """
    if (
        check
        or yes
        or verbose
        or scope is not None
        or to is not None
        or install_timer
        or uninstall_timer
        or timer_alias
    ):
        from vesma.cli.update_cmd import _legacy_flag_dispatch

        _legacy_flag_dispatch(
            console,
            check=check,
            yes=yes,
            verbose=verbose,
            scope=scope,
            to=to,
            install_timer=install_timer or timer_alias,
            uninstall_timer=uninstall_timer,
        )
        return
    # The update verb resolves targets by FILE STEM, not through the
    # fail-closed registry: a stale (schema-invalid) bundled manifest is
    # exactly what `vesma update` exists to heal — demanding a valid
    # components.d here would make the medicine require the patient to be
    # healthy first (the same warn-based discipline as the 6.1.0
    # post-upgrade reconciliation).
    components_dir = layout.components_dir()
    if name is not None:
        if all_components:
            _fail("specify either a component NAME or --all, not both")
        target = components_dir / f"{name}.yaml"
        alt = components_dir / f"{name}.yml"
        if name in BUNDLED_COMPONENTS:
            if not target.exists() and not alt.exists():
                _fail(
                    f"component {name!r} is not installed ({target} does not exist) — "
                    f"install it first: `vesma install {name}`"
                )
        elif target.exists() or alt.exists():
            _fail(
                f"component {name!r} is operator-authored — managed by operator — "
                f"edit the manifest file ({target}); the engine never rewrites it"
            )
        else:
            known = ", ".join(_manifest_stems()) or "nothing"
            _fail(
                f"component {name!r} is not installed (components: {known}) — if you "
                "meant the APPLICATION self-update: `vesma self-update check|apply|timer`"
            )
        try:
            lines, regenerated = regenerate_component_manifest(name)
        except (InstallError, ManifestError, OSError) as exc:
            _fail(str(exc))
        for line in lines:
            warn = line.startswith(("WARN:", "note:"))
            style = "[yellow]⚠[/yellow]" if warn else "[cyan]·[/cyan]"
            console.print(style, Text(line))
        if regenerated:
            console.print(f"{name} manifest regenerated — restart vesma.service", style="green")
        else:
            console.print(f"{name} manifest current", style="cyan")
        return
    if not all_components:
        known = ", ".join(_manifest_stems()) or "nothing"
        _fail(
            "refusing to update without an explicit target — bare `vesma update` "
            f"is not a bulk mutation (components: {known}) — specify "
            "NAME or --all; the application self-update moved to `vesma self-update`"
        )
    try:
        all_lines, regenerated_count = regenerate_stale_manifests()
    except (InstallError, ManifestError, OSError) as exc:
        _fail(f"manifest regeneration failed: {exc}")
    if not all_lines:
        console.print("no components.d installation — nothing to update", style="cyan")
        return
    for line in all_lines:
        warn = line.startswith("WARN:")
        style = "[yellow]⚠[/yellow]" if warn else "[cyan]·[/cyan]"
        console.print(style, Text(line))
    if regenerated_count:
        console.print("service manifests regenerated — restart vesma.service", style="green")
    else:
        console.print("service manifests current", style="cyan")


# ── vesma configs ─────────────────────────────────────────────────────


def configs_command(
    name: Annotated[
        str | None, typer.Argument(help="Optional single component.", show_default=False)
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Machine-readable config paths.")
    ] = False,
) -> None:
    """Show component config paths (all components, or one in detail).

    Bare: the per-component manifest / env-file / data-dir table. With
    NAME: the effective config path the component receives plus its key
    config fields (origin, schema, env file, venv). Writing configs is
    out of scope — edit the manifest or config file directly.
    """
    records, notice = _registry_or_fail()
    if name is not None:
        records = [_require_component(records, name)]
    if json_output:
        console.print_json(
            json.dumps(
                {
                    "config_path": str(layout.canonical_config_path()),
                    "components": [_configs_json_row(record) for record in records],
                }
            )
        )
        return
    if notice is not None:
        console.print(f"note: {notice}", style="yellow")
    if name is None:
        table = Table(title="Vesma component configs", show_header=True, header_style="bold")
        table.add_column("Component")
        # fold, not ellipsis: a truncated path is a wrong path — operators
        # copy these strings into shells.
        table.add_column("Manifest", overflow="fold")
        table.add_column("Env file", overflow="fold")
        table.add_column("Data dir", overflow="fold")
        for record in records:
            table.add_row(record.name, *_configs_cells(record))
        console.print(table)
        return
    record = records[0]
    console.print(f"component:     {record.name}")
    console.print(f"origin:        {record.origin}")
    console.print(f"manifest:      {record.manifest_path or '-'}")
    console.print(f"config path:   {layout.canonical_config_path()}")
    console.print(f"env file:      {layout.env_file_path(record.name)}")
    console.print(f"data dir:      {layout.data_dir(record.name)}")
    console.print(f"runtime dir:   {layout.resolve_runtime_dir().path}")
    console.print(f"venv bin:      {layout.component_venv_bin(record.name)}")
    schema = record.manifest.config if record.manifest is not None else None
    if schema is None:
        console.print("config schema: no config schema declared")
    elif schema.schema_inline is not None:
        keys = ", ".join(sorted(schema.schema_inline.get("properties", {})))
        console.print(f"config schema: inline — properties: {keys or '-'}")
    else:
        console.print(f"config schema: file — {schema.schema_file}")


def _configs_cells(record: ComponentRecord) -> tuple[str, str, str]:
    manifest = str(record.manifest_path) if record.manifest_path else "-"
    env = str(layout.env_file_path(record.name))
    data = str(layout.data_dir(record.name))
    return manifest, env, data


def _configs_json_row(record: ComponentRecord) -> dict[str, Any]:
    schema = record.manifest.config if record.manifest is not None else None
    schema_desc: str
    if schema is None:
        schema_desc = "none"
    elif schema.schema_inline is not None:
        schema_desc = "inline"
    else:
        schema_desc = str(schema.schema_file)
    return {
        "name": record.name,
        "origin": record.origin,
        "manifest_path": str(record.manifest_path) if record.manifest_path else None,
        "config_path": str(layout.canonical_config_path()),
        "env_file": str(layout.env_file_path(record.name)),
        "data_dir": str(layout.data_dir(record.name)),
        "venv_bin": str(layout.component_venv_bin(record.name)),
        "config_schema": schema_desc,
    }


# ── registration ──────────────────────────────────────────────────────


def register_component_commands(app: typer.Typer) -> None:
    """Attach the top-level component verbs to the root CLI app."""
    app.command(name="install", rich_help_panel="Components")(install_command)
    app.command(name="status", rich_help_panel="Components")(status_command)
    app.command(name="start", rich_help_panel="Components")(start_command)
    app.command(name="stop", rich_help_panel="Components")(stop_command)
    app.command(name="restart", rich_help_panel="Components")(restart_command)
    app.command(name="logs", rich_help_panel="Components")(logs_command)
    app.command(name="update", rich_help_panel="Components")(update_command)
    app.command(name="configs", rich_help_panel="Components")(configs_command)
