"""Component verbs — the ``vesma service components`` home.

The owner-facing component UX (wave 61, board card vesma-61-wave): ONE
unified verb set that works for EVERY component — the built-in bundle
(``board`` / ``metrics``) and operator-authored external ones (any valid
manifest dropped into ``~/.config/vesma/components.d/``, e.g.
``mesh.yaml``) alike.

CLI topology FINAL (owner doctrine 2026-10-24, 7.0.0): ONE spelling per
operation — the component verbs live on ``vesma service components
install|update|configs|status`` and the lifecycle batch semantics on
``vesma service start/stop/restart``; the former top-level spellings
(``vesma install|update|status|configs|start|stop|restart``) are REMOVED
with no alias layer, and the pipeline-trace viewer owns ``vesma traces``
(:mod:`vesma.cli.traces`) while "logs" belongs solely to the daemon
world (``vesma service logs``).

Design contract (TL, wave 61 — unchanged):

* THIN DELEGATION ONLY — every verb reuses the existing core: manifests
  via :mod:`vesma.service.install` (single-writer + ``.pre-regen.bak``
  backup discipline), lifecycle via the supervisor control client (the
  same ``vesma.cli.service`` plumbing ``vesma service start`` uses).
  Zero supervisor logic lives here.
* BARE CALLS NEVER BULK-MUTATE: ``start/stop/restart/update`` without a
  NAME refuse loudly (with the component list and the ``--all`` hint);
  ``install`` lists what is installable.
* ORIGIN DISCRIMINATOR (documented choice): a components.d file is
  BUNDLE-OWNED iff its name is one of
  :data:`vesma.service.install.BUNDLED_COMPONENTS` — the same rule the
  6.1.0 post-upgrade reconciliation applies. Anything else is
  operator-authored and NEVER rewritten by the engine; a byte-diff on a
  bundled manifest means "stale" (regenerated behind a ``.pre-regen.bak``
  backup), not "operator-owned".
* COLLISION RESOLUTION: the application self-update family lives on
  ``vesma self-update`` — a completely different object than component
  manifests (the user-profile pip copy vs runtime-owned definitions).

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

from vesma.cli.service import _open_client, _with_client, service_app
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


@dataclasses.dataclass(frozen=True)
class ComponentRecord:
    """One registry row: a component as THIS machine knows it."""

    name: str
    origin: str  # ORIGIN_BUNDLED | ORIGIN_OPERATOR
    version: str  # manifest metadata.version, "-" when absent
    manifest: ComponentManifest | None  # None = bundled, not installed
    manifest_path: Path | None


# ── registry (disk state) ─────────────────────────────────────────────


def _registry(
    install_hint: str = "vesma service components install --all",
) -> tuple[list[ComponentRecord], str | None]:
    """The component registry: installed manifests, or the bundle view.

    When ``components.d`` does not exist NOTHING is installed — the
    release bundle (board/metrics) is shown as the installable set with
    a notice, so bare ``status``/``install`` answer usefully on a fresh
    machine instead of failing. An EXISTING components.d loads
    fail-closed (a broken manifest is an error, never a silent skip).
    ``install_hint`` names the install spelling for that notice — every
    surface here teaches the canonical ``service components`` spelling
    (security P3-1: the notice names the install spelling of the surface
    that shows it).
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
            f"the release bundle; install it with `{install_hint}`"
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


def _registry_or_fail(
    install_hint: str = "vesma service components install --all",
) -> tuple[list[ComponentRecord], str | None]:
    """``_registry`` with the fail-closed load rendered as a CLEAN refusal.

    A broken components.d must never surface as a traceback — the verbs
    that READ the registry answer with one actionable line. The ``update``
    verb deliberately does NOT go through here: healing a broken
    installation is its job (same warn-based discipline as the 6.1.0
    post-upgrade reconciliation).
    """
    try:
        return _registry(install_hint=install_hint)
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

    Cross-comment (cascade dedup finding): ``supervisor._topological_order``
    is a SECOND implementation of this (Kahn, cycle-raising). The CLI
    deliberately does NOT import the supervisor module — it is the
    server-side runtime and would drag the whole control-plane import
    graph into every CLI invocation. The two share the CONTRACT —
    dependencies first, alphabetical tie-break, and any valid order is
    correct for lifecycle sequencing (the exact sequences of a DFS and
    Kahn pass may differ, and nothing may depend on the difference).
    Unifying both on one shared helper in ``vesma.service.manifest``
    belongs to the supervisor refactor window, not the CLI surface —
    until then this comment is the linkage. Cycles and dangling
    references are impossible here anyway: ``load_installation`` already
    refuses them (DEPENDS_CYCLE / DEPENDS_MISSING), so this DFS variant
    tolerates them for free instead of raising.
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


def _require_component(
    records: list[ComponentRecord],
    name: str,
    registry_hint: str = "vesma service components status",
) -> ComponentRecord:
    for record in records:
        if record.name == name:
            return record
    # Honest detail line (cascade consistency finding): on a fresh
    # machine the registry is the BUNDLE VIEW — board/metrics are the
    # installable release bundle, NOT installed components, and the
    # message must not contradict the "no installation found" notice.
    installed = [record.name for record in records if record.manifest is not None]
    if installed:
        detail = f"installed: {', '.join(installed)}"
    else:
        bundle = [record.name for record in records if record.origin == ORIGIN_BUNDLED]
        detail = (
            f"nothing installed — the release bundle provides: {', '.join(bundle)}"
            if bundle
            else "nothing installed"
        )
    _fail(
        f"component {name!r} is not installed ({detail}) — see `{registry_hint}` for the registry"
    )


# ── install (the `service components install` implementation) ─────────


#: The canonical install spelling every hint here teaches (ONE spelling
#: per operation — owner doctrine 2026-10-24).
_INSTALL_SPELLING = "vesma service components install"


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
    `--all` (or `vesma service install`) owns them.

    Bare invocation changes nothing — it lists what is installable and
    how to proceed.
    """
    if name is not None:
        if all_components:
            _fail("specify either a component NAME or --all, not both")
        try:
            result: InstallComponentResult = install_component(name)
        except (InstallError, ManifestError, OSError) as exc:
            # OSError (disk full, EACCES, ...) renders as one clean line
            # like every other refusal — never a traceback (P3-2).
            _fail(str(exc))
        for line in result.lines:
            console.print("[green]✓[/green]", Text(line))
        console.print(f"Component {name} installed ({result.origin}).", style="green")
        return
    if all_components:
        try:
            full = install_service()
        except (InstallError, ManifestError, OSError) as exc:
            _fail(str(exc))
        for line in full.lines:
            downgraded = line.startswith("CONTAINER DOWNGRADE")
            style = "[yellow]⚠[/yellow]" if downgraded else "[green]✓[/green]"
            console.print(style, Text(line))
        console.print(f"Service installed ({full.unit_path}).", style="green")
        return
    # Bare: the installable set + how to proceed. Informational — exit 0.
    records, notice = _registry_or_fail(install_hint=f"{_INSTALL_SPELLING} --all")
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
    console.print(f"install one component:  {_INSTALL_SPELLING} NAME")
    console.print(f"install everything (manifests, venvs, unit):  {_INSTALL_SPELLING} --all")


# ── status (the `service components status` implementation) ───────────────────────────────────────


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
    *,
    registry_hint: str = "vesma service components status",
    install_hint: str = "vesma service components install --all",
) -> None:
    """Show every component: origin, version, live state (or one component).

    The registry half is disk-state and works with the supervisor down;
    the live state (running/stopped/failed + pid) comes from the
    supervisor over its control socket and reads "unknown" with an
    explicit note when it does not answer. `--json` emits the same data
    machine-readably — the supervisor reachability travels INSIDE the
    payload, so pipes never parse prose.
    """
    records, notice = _registry_or_fail(install_hint=install_hint)
    if name is not None:
        records = [_require_component(records, name, registry_hint=registry_hint)]
    # P2-2 (cascade fix): the live leg is queried for the BUNDLE VIEW
    # too — `service run` on a fresh machine falls back to the bundled
    # manifests without any components.d, so a RUNNING component must
    # not be reported "not installed" just because no manifest is on
    # disk. Live state overrides the disk-derived label.
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
    """One State cell — live data wins over the disk-derived label.

    With the supervisor answering, a bundle-fallback component shows its
    REAL state marked "(bundle fallback)"; only a dead supervisor leaves
    the disk verdict ("not installed" / "unknown") standing.
    """
    if live is not None:
        entry = live.get(record.name)
        if isinstance(entry, dict):
            state = str(entry.get("state", "unknown"))
            pid = entry.get("pid")
            label = f"{state} (pid {pid})" if pid else state
            if record.manifest is None:
                label += " (bundle fallback)"
            return label
        return "not loaded" if record.manifest is not None else "not installed"
    if record.manifest is None:
        return "not installed"
    return "unknown (supervisor not reachable)"


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


# ── start / stop / restart (the `service <verb>` implementations) ─────────────────────────────────


def _lifecycle_targets(
    records: list[ComponentRecord],
    name: str | None,
    all_components: bool,
    verb: str,
    *,
    invoked_as: str | None = None,
    install_hint: str = "vesma service components install --all",
    registry_hint: str = "vesma service components status",
) -> list[str]:
    """The explicit target list — bare lifecycle calls never bulk-mutate.

    ``invoked_as`` (default ``vesma service <verb>``) names the spelling
    the refusal should teach — every lifecycle surface lives on
    ``vesma service <verb>`` (ONE spelling per operation).
    """
    spelling = invoked_as or f"vesma service {verb}"
    if name is not None:
        if all_components:
            _fail("specify either a component NAME or --all, not both")
        return [_require_component(records, name, registry_hint=registry_hint).name]
    if not all_components:
        installed = [record.name for record in records if record.manifest is not None]
        _fail(
            f"refusing to {verb} without an explicit target — bare `{spelling}` "
            f"is not a bulk mutation (installed: {', '.join(installed) or 'nothing'}) "
            "— specify NAME or --all"
        )
    manifests = {record.name: record.manifest for record in records if record.manifest is not None}
    if not manifests:
        _fail(f"nothing installed — install components first (`{install_hint}`)")
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
    *,
    invoked_as: str | None = None,
    install_hint: str = "vesma service components install --all",
    registry_hint: str = "vesma service components status",
) -> None:
    """Start a component (or every one with --all) via the supervisor.

    Thin delegation to the same control-plane client the service verbs
    use; idempotent — an already-running component is reported as-is.
    Bare invocation refuses: name a component or pass --all. The
    canonical home is `vesma service start`. Check `vesma service
    health` afterwards to confirm readiness.
    """
    records, _notice = _registry_or_fail()
    for target in _lifecycle_targets(
        records,
        name,
        all_components,
        "start",
        invoked_as=invoked_as,
        install_hint=install_hint,
        registry_hint=registry_hint,
    ):
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
    *,
    invoked_as: str | None = None,
    install_hint: str = "vesma service components install --all",
    registry_hint: str = "vesma service components status",
) -> None:
    """Stop a component (or every one with --all) via the supervisor.

    Thin delegation to the same control-plane client the service verbs
    use; idempotent — an already-stopped component is reported as-is.
    `--force` skips the graceful phase (for a wedged process only). Bare
    invocation refuses: name a component or pass --all. The canonical
    home is `vesma service stop`.
    """
    records, _notice = _registry_or_fail()
    for target in _lifecycle_targets(
        records,
        name,
        all_components,
        "stop",
        invoked_as=invoked_as,
        install_hint=install_hint,
        registry_hint=registry_hint,
    ):
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
    *,
    invoked_as: str | None = None,
    install_hint: str = "vesma service components install --all",
    registry_hint: str = "vesma service components status",
) -> None:
    """Restart a component (or every one with --all) via the supervisor.

    Thin delegation to the same control-plane client the service verbs
    use; a restart is a real stop-then-start cycle, NOT idempotent — a
    retry after a lost response performs a second restart. Bare
    invocation refuses: name a component or pass --all. The canonical
    home is `vesma service restart`. Confirm readiness with `vesma
    service health`.
    """
    records, _notice = _registry_or_fail()
    for target in _lifecycle_targets(
        records,
        name,
        all_components,
        "restart",
        invoked_as=invoked_as,
        install_hint=install_hint,
        registry_hint=registry_hint,
    ):
        state = _with_client(socket, _lifecycle_action("restart", target))
        console.print(f"{target}: {state}")


# ── update (the `service components update` implementation) ───────────


def _update_components(name: str | None, all_components: bool) -> None:
    """The component-manifest update verb (``vesma service components
    update`` — the one spelling since 7.0.0).

    The update verb resolves targets in the CORE (single source of truth
    — see regenerate_component_manifest), by FILE STEM, not through the
    fail-closed registry: a stale (schema-invalid) bundled manifest is
    exactly what `update` exists to heal — demanding a valid
    components.d here would make the medicine require the patient to be
    healthy first (the same warn-based discipline as the 6.1.0
    post-upgrade reconciliation).
    """
    if name is not None:
        if all_components:
            _fail("specify either a component NAME or --all, not both")
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
            "refusing to update without an explicit target — bare `vesma service "
            "components update` is not a bulk mutation "
            f"(components: {known}) — specify NAME or --all; the application "
            "self-update lives on `vesma self-update`"
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


# ── configs (the `service components configs` implementation) ─────────────────────────────────────


def configs_command(
    name: Annotated[
        str | None, typer.Argument(help="Optional single component.", show_default=False)
    ] = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Machine-readable config paths.")
    ] = False,
    *,
    registry_hint: str = "vesma service components status",
    install_hint: str = "vesma service components install --all",
) -> None:
    """Show component config paths (all components, or one in detail).

    Bare: the per-component manifest / env-file / data-dir table. With
    NAME: the effective config path the component receives plus its key
    config fields (origin, schema, env file, venv). Writing configs is
    out of scope — edit the manifest or config file directly.
    """
    records, notice = _registry_or_fail(install_hint=install_hint)
    if name is not None:
        records = [_require_component(records, name, registry_hint=registry_hint)]
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


# ── the canonical home: `vesma service components` ────────────────────


service_components_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="components",
    help=(
        "Component registry verbs: install, update, configs, status.\n\n"
        "The component surface of the USER profile (rootless by design — "
        "the runtime-owned definitions the supervisor loads): manifests "
        "for the bundled bundle (board, metrics) and operator-authored "
        "components.d entries alike, their config paths, and the "
        "registry+live-state table. Lifecycle control (start/stop/"
        "restart/logs of a RUNNING component) lives one level up on "
        "`vesma service` itself."
    ),
    no_args_is_help=True,
)


@service_components_app.command(name="install")
def components_install(
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
    """Install a component's manifest (NAME), or the whole installation (--all).

    With NAME: the per-component manifest install — bundled components
    are (re)written from this release's bundle (a diverging installed
    manifest is preserved as a one-time .pre-regen.bak backup),
    operator-authored manifests are validated fail-closed and left
    untouched, data dirs and config schemas are materialized. Bare
    invocation lists what is installable and how to proceed (never
    mutates).
    """
    install_command(name, all_components)


@service_components_app.command(name="update")
def components_update(
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
) -> None:
    """Regenerate component manifests from this release (NAME or --all).

    A bundled component's manifest is regenerated when its bytes differ
    from this release's bundle; an operator-authored manifest is REFUSED
    — managed by operator, edit the file. Bare invocation refuses: it is
    never a bulk mutation. The application self-update lives on `vesma
    self-update`.
    """
    _update_components(name, all_components)


@service_components_app.command(name="configs")
def components_configs(
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
    configs_command(name, json_output)


@service_components_app.command(name="status")
def components_status(
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
    """Show the component registry: origin, version, live state per component.

    The registry half is disk-state and works with the supervisor down;
    the live state (running/stopped/failed + pid) comes from the
    supervisor over its control socket and reads "unknown" with an
    explicit note when it does not answer. `--json` emits the same data
    machine-readably.
    """
    status_command(name, json_output, socket)


# Registered at import time (the same module-level discipline main.py
# uses for every add_typer): the service domain owns the components
# sub-app; components.py already imports service.py for the control
# client, so the registration direction introduces no cycle.
service_app.add_typer(service_components_app, name="components")
