"""Component verbs — ``vesma service components`` home + hidden L1 aliases.

The owner-facing component UX (wave 61, board card vesma-61-wave): ONE
unified verb set that works for EVERY component — the built-in bundle
(``board`` / ``metrics``) and operator-authored external ones (any valid
manifest dropped into ``~/.config/vesma/components.d/``, e.g.
``mesh.yaml``) alike.

CLI topology v2 (ArchCom 2026-10-23, ratified; waves A+B): the CANONICAL
home for the component verbs is ``vesma service components
install|update|configs|status`` — runtime-owned component definitions
under the supervisor's domain — while ``start/stop/restart`` batch
semantics live directly on ``vesma service start/stop/restart``. The
former top-level spellings remain as HIDDEN deprecated aliases (one
stderr hint per verb, identical behavior, removal not before 9.0), and
the former top-level ``vesma logs`` name belongs to the pipeline-trace
viewer (:mod:`vesma.cli.logs`) — ``vesma logs NAME`` answers the old
component habit with a loud redirect to ``vesma service logs NAME``.

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
* COLLISION RESOLUTION: ``vesma update`` was the application self-update
  group — the legacy FLAG forms the shipped systemd timer depends on
  keep working as hidden aliases on the top-level ``update`` alias
  (delegating to the self-update machinery with a stderr hint).

``status`` and ``configs`` degrade gracefully when the supervisor is
down — the registry view is disk-state and works offline; the live state
column says so explicitly instead of failing the whole command. Operator
data (paths, names, error text) is rendered through ``rich.text.Text``
— never passed through markup.
"""

from __future__ import annotations

import dataclasses
import functools
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
    install_hint: str = "vesma install --all",
) -> tuple[list[ComponentRecord], str | None]:
    """The component registry: installed manifests, or the bundle view.

    When ``components.d`` does not exist NOTHING is installed — the
    release bundle (board/metrics) is shown as the installable set with
    a notice, so bare ``status``/``install`` answer usefully on a fresh
    machine instead of failing. An EXISTING components.d loads
    fail-closed (a broken manifest is an error, never a silent skip).
    ``install_hint`` names the install spelling for that notice: the
    hidden top-level aliases keep the frozen legacy default; the
    ``service components`` home teaches its own (topology v2, security
    P3-1).
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
    install_hint: str = "vesma install --all",
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
    records: list[ComponentRecord], name: str, registry_hint: str = "vesma status"
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
    *,
    invoked_as: str = "vesma install",
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
    records, notice = _registry_or_fail(install_hint=f"{invoked_as} --all")
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
    console.print(f"install one component:  {invoked_as} NAME")
    console.print(f"install everything (manifests, venvs, unit):  {invoked_as} --all")


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
    *,
    registry_hint: str = "vesma status",
    install_hint: str = "vesma install --all",
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


# ── vesma start / stop / restart ──────────────────────────────────────


def _lifecycle_targets(
    records: list[ComponentRecord],
    name: str | None,
    all_components: bool,
    verb: str,
    *,
    invoked_as: str | None = None,
    install_hint: str = "vesma install --all",
    registry_hint: str = "vesma status",
) -> list[str]:
    """The explicit target list — bare lifecycle calls never bulk-mutate.

    ``invoked_as`` (default ``vesma <verb>``) names the spelling the
    refusal should teach — the top-level aliases keep their frozen
    wording, the ``vesma service <verb>`` home names its own.
    """
    spelling = invoked_as or f"vesma {verb}"
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
    install_hint: str = "vesma install --all",
    registry_hint: str = "vesma status",
) -> None:
    """Start a component (or every one with --all) via the supervisor.

    Thin delegation to the same control-plane client the service verbs
    use; idempotent — an already-running component is reported as-is.
    Bare invocation refuses: name a component or pass --all. The
    same-function canonical home is `vesma service start`; the top-level
    spelling is a hidden deprecated alias (removal not before 9.0).
    Check `vesma service health` afterwards to confirm readiness.
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
    install_hint: str = "vesma install --all",
    registry_hint: str = "vesma status",
) -> None:
    """Stop a component (or every one with --all) via the supervisor.

    Thin delegation to the same control-plane client the service verbs
    use; idempotent — an already-stopped component is reported as-is.
    `--force` skips the graceful phase (for a wedged process only). Bare
    invocation refuses: name a component or pass --all. The same-function
    canonical home is `vesma service stop`; the top-level spelling is a
    hidden deprecated alias (removal not before 9.0).
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
    install_hint: str = "vesma install --all",
    registry_hint: str = "vesma status",
) -> None:
    """Restart a component (or every one with --all) via the supervisor.

    Thin delegation to the same control-plane client the service verbs
    use; a restart is a real stop-then-start cycle, NOT idempotent — a
    retry after a lost response performs a second restart. Bare
    invocation refuses: name a component or pass --all. The same-function
    canonical home is `vesma service restart`; the top-level spelling is
    a hidden deprecated alias (removal not before 9.0). Confirm readiness
    with `vesma service health`.
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

    Hidden deprecated alias of `vesma service components update` (CLI
    topology v2 wave A): identical component behavior, one stderr hint,
    removal not before 9.0. With NAME: a bundled component's manifest is
    regenerated when its bytes differ from this release's bundle (the
    previous bytes are kept as a one-time .pre-regen.bak backup); an
    operator-authored manifest is REFUSED — managed by operator, edit the
    file. With --all: every stale bundled manifest at once. Bare
    invocation refuses — it is never a bulk mutation.

    The legacy application self-update FLAG forms (--yes, --check, --to,
    --scope, --install-timer, --uninstall-timer) still work here as
    hidden aliases so installed units and scripts keep running (they
    predate the rename; the family lives on `vesma self-update`). A
    legacy flag TOGETHER with a component NAME is refused loudly (P2,
    cascade fix): the flag belongs to the self-update family and would
    otherwise silently swallow the NAME — the operator gets both
    spellings instead of the wrong mutation.
    """
    legacy_flags: list[tuple[str, str]] = [
        entry
        for entry, used in (
            (("--check", "vesma self-update check"), check),
            (("--yes", "vesma self-update apply"), yes),
            (("--verbose", "vesma self-update apply"), verbose),
            (("--scope", "vesma self-update apply --scope user"), scope is not None),
            (("--to", "vesma self-update apply --to VERSION"), to is not None),
            (("--install-timer", "vesma self-update timer install"), install_timer),
            (("--uninstall-timer", "vesma self-update timer uninstall"), uninstall_timer),
            (("--timer", "vesma self-update timer install"), timer_alias),
        )
        if used
    ]
    if name is not None and legacy_flags:
        # P2-1 (cascade fix): the legacy dispatch runs the APPLICATION
        # self-update — it must never silently swallow a component NAME.
        flags = ", ".join(flag for flag, _canonical in legacy_flags)
        _fail(
            f"{flags} belongs to `vesma self-update`; given NAME — either "
            f"`vesma update {name}` or `{legacy_flags[0][1]}` (without the "
            "component name)"
        )
    if legacy_flags:
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
    _update_components(name, all_components)


def _update_components(
    name: str | None, all_components: bool, *, invoked_as: str = "vesma update"
) -> None:
    """The component-manifest half of the update verb (both spellings).

    Called by the hidden top-level alias (frozen wording via the default
    ``invoked_as``) and by ``vesma service components update`` (the
    canonical home). The update verb resolves targets in the CORE
    (single source of truth — see regenerate_component_manifest), by
    FILE STEM, not through the fail-closed registry: a stale
    (schema-invalid) bundled manifest is exactly what `update` exists to
    heal — demanding a valid components.d here would make the medicine
    require the patient to be healthy first (the same warn-based
    discipline as the 6.1.0 post-upgrade reconciliation).
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
            f"refusing to update without an explicit target — bare `{invoked_as}` "
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
    *,
    registry_hint: str = "vesma status",
    install_hint: str = "vesma install --all",
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

    Same implementation as the hidden top-level `vesma install` alias,
    taught under its canonical spelling. With NAME: the per-component
    manifest install — bundled components are (re)written from this
    release's bundle (a diverging installed manifest is preserved as a
    one-time .pre-regen.bak backup), operator-authored manifests are
    validated fail-closed and left untouched, data dirs and config
    schemas are materialized. Bare invocation lists what is installable
    and how to proceed (never mutates).
    """
    install_command(
        name,
        all_components,
        invoked_as="vesma service components install",
    )


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

    The component-manifest half of the update verb under its canonical
    spelling (the hidden top-level `vesma update` alias delegates here
    and additionally carries the legacy application self-update FLAG
    forms for units installed before the rename). A bundled component's
    manifest is regenerated when its bytes differ from this release's
    bundle; an operator-authored manifest is REFUSED — managed by
    operator, edit the file. Bare invocation refuses: it is never a bulk
    mutation. The application self-update lives on `vesma self-update`.
    """
    _update_components(name, all_components, invoked_as="vesma service components update")


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
    config fields (origin, schema, env file, venv). Same implementation
    as the hidden top-level `vesma configs` alias. Writing configs is
    out of scope — edit the manifest or config file directly.
    """
    configs_command(
        name,
        json_output,
        registry_hint="vesma service components status",
        install_hint="vesma service components install --all",
    )


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
    machine-readably. Same implementation as the hidden top-level
    `vesma status` alias, taught under its canonical spelling.
    """
    status_command(
        name,
        json_output,
        socket,
        registry_hint="vesma service components status",
        install_hint="vesma service components install --all",
    )


# Registered at import time (the same module-level discipline main.py
# uses for every add_typer): the service domain owns the components
# sub-app; components.py already imports service.py for the control
# client, so the registration direction introduces no cycle.
service_app.add_typer(service_components_app, name="components")


# ── the hidden top-level aliases ──────────────────────────────────────


def _deprecated_alias_hint(old: str, new: str) -> None:
    """One-line deprecation hint for a hidden legacy top-level verb (stderr).

    CLI topology v2 (ArchCom 2026-10-23): the top-level supervisor
    shortcuts re-parented under their domain homes; the old spellings
    keep working — identical behavior, scripts must not be able to tell
    the difference — but point at the canonical spelling. stderr only:
    stdout stays clean for pipes and JSON consumers. Removal not before
    9.0 (MAJOR).
    """
    typer.echo(f"[deprecated] `{old}` is deprecated — use: {new}", err=True)


def _hidden_alias(old: str, new: str, fn: Callable[..., None]) -> Callable[..., None]:
    """Wrap ``fn`` as a hidden deprecated alias printing the stderr hint.

    ``functools.wraps`` preserves the full typer signature (params,
    defaults, help text) — the alias IS the same command under the old
    spelling; the only difference is the one stderr line before dispatch.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        _deprecated_alias_hint(old, new)
        fn(*args, **kwargs)

    return wrapper


def register_component_commands(app: typer.Typer) -> None:
    """Attach the top-level component verbs as HIDDEN deprecated aliases.

    CLI topology v2 wave A: the canonical homes are ``vesma service
    components install|update|configs|status`` and ``vesma service
    start|stop|restart``; the former top-level spellings stay registered
    (hidden from ``--help``, still tab-completable, byte-identical
    behavior + one stderr hint) until the 9.0 MAJOR. The former
    top-level ``vesma logs`` name belongs to the pipeline-trace viewer
    (``vesma.cli.logs``); its component habit is caught by that viewer's
    positional redirect.
    """
    app.command(name="install", hidden=True)(
        _hidden_alias(
            "vesma install",
            "vesma service components install (component) / vesma service install (user profile)",
            install_command,
        )
    )
    app.command(name="update", hidden=True)(
        _hidden_alias(
            "vesma update",
            "vesma service components update (application: vesma self-update)",
            update_command,
        )
    )
    app.command(name="status", hidden=True)(
        _hidden_alias("vesma status", "vesma service components status", status_command)
    )
    app.command(name="configs", hidden=True)(
        _hidden_alias("vesma configs", "vesma service components configs", configs_command)
    )
    app.command(name="start", hidden=True)(
        _hidden_alias("vesma start", "vesma service start", start_command)
    )
    app.command(name="stop", hidden=True)(
        _hidden_alias("vesma stop", "vesma service stop", stop_command)
    )
    app.command(name="restart", hidden=True)(
        _hidden_alias("vesma restart", "vesma service restart", restart_command)
    )
