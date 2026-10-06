"""``vesma awareness`` — operator surface for the native awareness heartbeat.

Board card ``vesma-ops-mode-ux`` (ADR-0035): today switching the heartbeat
mode means a manual YAML edit + a service restart, and reading the wave-0
gate metrics means raw SQL against the metrics sidecar. This module ends
both:

* ``vesma awareness get`` / ``vesma awareness set <mode>`` — read and
  switch ``awareness.native_heartbeat_mode`` in the resolved config file
  (``off | shadow | canary | on``, validated against the field's own enum
  in :class:`vesmaro.config.AwarenessConfig` — an unknown value is refused
  with the allowed list, never written); the applied value is echoed with
  the restart note, because a running server reads the config at startup.
* ``vesma awareness stats`` — the wave-0 gate metrics (delivery,
  suppressions per reason, calm/delta split, token budget, tool-call
  denominator) read from the ``awareness_events`` table of the vitals
  sidecar through :class:`vesmaro.metrics.sink.MetricsStore` — no raw
  operator SQL; the SQL lives inside this module against the born-final
  schema, and the sidecar path is never spelled here: the store comes
  from :func:`vesmaro.metrics.boundary.create_vitals_store` (the exact
  construction the server uses).

Zero peer content: every aggregate here is a count/enum over the
zero-content event rows (CWE-359 per ADR-0035); nothing prints project /
agent / session values.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from rich.console import Console

from vesmaro.cli._manager import get_manager

if TYPE_CHECKING:
    from vesmaro.metrics.sink import MetricsStore

console = Console()

awareness_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="awareness",
    help=(
        "Native awareness heartbeat (ADR-0035): view and switch the delivery "
        "mode, read the wave-0 gate metrics.\n\n"
        "`get` / `set` manage `awareness.native_heartbeat_mode` in the "
        "resolved config file (off | shadow | canary | on); `stats` prints "
        "the funnel metrics from the metrics sidecar without raw SQL. "
        "Changing the mode takes effect on the next server (re)start."
    ),
    no_args_is_help=True,
)

ConfigOption = typer.Option(None, "--config", "-c", help="Path to config.yaml")

#: The mode ladder (ADR-0035 §Configuration) — mirrored from the born-final
#: Literal in :class:`vesmaro.config.AwarenessConfig`; kept as the tuple the
#: CLI validates against (the Settings class itself remains the real gate:
#: an out-of-range value is refused at load by pydantic anyway).
AWARENESS_MODES: tuple[str, ...] = ("off", "shadow", "canary", "on")

#: Config key the set/get surface manages (the card's scope: exactly this
#: knob, not the whole awareness section).
AWARENESS_MODE_KEY = "awareness.native_heartbeat_mode"

#: The event kinds a suppressed row's meta may carry is the born-final
#: reason enum (mirrored from :mod:`vesmaro.metrics.schema` — the read
#: side degrades on anything else rather than refusing the row).
_SUPPRESS_REASONS: tuple[str, ...] = (
    "no_delta",
    "rate_cap",
    "budget",
    "presence_stale",
    "probe_error",
)


def _resolved_config_path(config: str | None) -> Path | None:
    """The config file ``load_settings`` would resolve, or None (zero-config).

    When no config file exists, ``set`` has nothing to edit — the command
    explains the zero-config profile instead of silently creating a file
    (creating one would change which defaults other surfaces see).
    """
    from vesmaro.config import find_config_file

    return find_config_file(config)


def _current_mode(config: str | None) -> tuple[str | None, Path | None]:
    """``(mode value as written, resolved path)`` without booting a manager.

    Reads the raw YAML mapping (safe_load) — the RAW FILE value is what an
    operator edits, and the effective-settings value can differ when the
    env override wins; ``vesma awareness get`` reports both.
    """
    path = _resolved_config_path(config)
    if path is None:
        return None, None
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        console.print(f"[red]✗[/red] {path} does not contain a YAML mapping")
        raise typer.Exit(1)
    awareness = data.get("awareness")
    value = awareness.get("native_heartbeat_mode") if isinstance(awareness, dict) else None
    return (str(value) if value is not None else None), path


def _write_mode(path: Path, value: str) -> None:
    """Set ``awareness.native_heartbeat_mode`` in the YAML file (atomic).

    Preserves every other mapping exactly as ``yaml.safe_load`` sees it and
    writes through a sibling tmp file + os.replace (never a half-written
    config on crash). Sections not present in the file are added (`awareness:`
    mapping created with only the one key — an additive edit, existing
    sibling keys untouched).
    """
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        console.print(f"[red]✗[/red] {path} does not contain a YAML mapping")
        raise typer.Exit(1)
    awareness = data.get("awareness")
    if not isinstance(awareness, dict):
        awareness = {}
        data["awareness"] = awareness
    awareness["native_heartbeat_mode"] = value

    tmp_fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".awareness-tmp-", suffix=".yaml"
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False, allow_unicode=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, path.stat().st_mode & 0o777 or 0o600)
        os.replace(tmp_name, path)
    except Exception:
        with suppress(OSError):
            os.unlink(tmp_name)
        raise


@awareness_app.command(name="get")
def mode_get(config: str = ConfigOption) -> None:
    """Show the awareness heartbeat mode (config file + effective value).

    Prints the RAW value as written in the resolved config file and the
    EFFECTIVE value (settings load — env overrides included, the
    `VESMA_AWARENESS__NATIVE_HEARTBEAT_MODE` env override wins over the
    file per the dual-source precedence). A file without the key prints
    the effective default (`off`) and says the file carries no explicit
    value.
    """
    path = _resolved_config_path(config)
    if path is None:
        console.print("[cyan]zero-config[/cyan] — no config file found; effective mode: off")
        console.print(
            "[dim]create one first (see getting-started), then `vesma awareness set`[/dim]"
        )
        raise typer.Exit(1)

    raw, path = _current_mode(config)
    settings = get_manager(config).settings
    effective = getattr(getattr(settings, "awareness", None), "native_heartbeat_mode", "off")
    console.print(f"  [bold]config file[/bold]: {path}")
    console.print(f"  [bold]{AWARENESS_MODE_KEY}[/bold]: {raw if raw else '(not set → off)'}")
    console.print(f"  [bold]effective[/bold]: {effective}")
    if env_mode := os.environ.get("VESMA_AWARENESS__NATIVE_HEARTBEAT_MODE"):
        console.print(
            f"  [cyan]env override active[/cyan]: VESMA_AWARENESS__NATIVE_HEARTBEAT_MODE={env_mode}"
        )


@awareness_app.command(name="set")
def mode_set(
    value: Annotated[
        str,
        typer.Argument(help=f"New mode: one of {', '.join(AWARENESS_MODES)}."),
    ],
    config: str = ConfigOption,
) -> None:
    """Switch the awareness heartbeat mode end-to-end (ADR-0035 mode ladder).

    Validates `value` against the born-final mode ladder and writes
    `awareness.native_heartbeat_mode` into the resolved config file — the
    manual-YAML path stops being the documented way. An unknown value is
    refused with the allowed list and NOTHING is written. The change takes
    effect on the NEXT server start: a running `vesma serve` /
    `vesma service` process reads the config once at startup, so the
    command always prints the restart note (and the service restart
    command when the supervisor answers on its control socket).
    """
    if value not in AWARENESS_MODES:
        console.print(
            f"[red]✗[/red] unknown mode {value!r} — allowed: {', '.join(AWARENESS_MODES)}"
        )
        raise typer.Exit(1)

    path = _resolved_config_path(config)
    if path is None:
        console.print(
            "[red]✗[/red] no config file found (zero-config) — create "
            "`~/.mnemos/config.yaml` (or point --config at one) first; "
            "refusing to invent a config behind the operator's back"
        )
        raise typer.Exit(1)

    raw, _ = _current_mode(config)
    _write_mode(path, value)
    was = raw if raw else "off default"
    console.print(f"[green]✓[/green] {AWARENESS_MODE_KEY} = {value} (was: {was})")
    console.print(f"  written: {path}")
    _print_restart_note(config)


def _print_restart_note(config: str | None) -> None:
    """The restart guard: a running server reads the config at startup only.

    Probes the supervisor control socket (best-effort, no manager boot) —
    when it answers, name the exact `vesma service restart` verb; either
    way the note states the takes-effect-next-start rule (never silently
    assume a restart happened — silent degradation is banned).

    The probe deliberately does NOT go through
    :func:`vesmaro.cli.service._with_client`: that helper SPEAKS for every
    failure (red pre-flight banner) and ends with ``typer.Exit``, which a
    broad ``except`` here would swallow — the P2 finding of the 2026-10-06
    review. A bare probe that returns None on any refusal keeps the quiet
    dim-line fallback honest.
    """
    console.print(
        "[yellow]note[/yellow] the new mode takes effect on the NEXT server start"
        " (running processes read the config once at startup)"
    )


def _print_restart_note(config: str | None) -> None:
    """The restart guard: a running server reads the config at startup only.

    Probes the supervisor control socket (best-effort, no manager boot) —
    when it answers, name the exact `vesma service restart` verb; either
    way the note states the takes-effect-next-start rule (never silently
    assume a restart happened — silent degradation is banned).

    The probe deliberately does NOT go through
    :func:`vesmaro.cli.service._with_client`: that helper SPEAKS for every
    failure (red pre-flight banner) and ends with ``typer.Exit``, which a
    broad ``except`` here would swallow — the P2 finding of the 2026-10-06
    review. The bare probe keeps the quiet dim-line fallback honest.
    """
    console.print(
        "[yellow]note[/yellow] the new mode takes effect on the NEXT server start"
        " (running processes read the config once at startup)"
    )
    fallback = "  [dim]start/restart the server with: vesma serve (or vesma service run)[/dim]"
    try:
        from vesmaro.service.client import ControlClientError, control_socket_path

        client = _open_probe_client(control_socket_path()[0])
        try:
            state = client.status("core")
        finally:
            client.close()
    except ControlClientError:
        console.print(fallback)
    except Exception:
        console.print(fallback)
    else:
        console.print(
            f"  restart the running service now: [bold]vesma service restart core[/bold] ({state})"
        )


def _open_probe_client(socket: Path) -> Any:
    """Bare supervisor probe opener for the restart note — no CLI chatter.

    Imports :func:`vesmaro.cli.service._open_client` (the same client
    construction the service verbs use) WITHOUT its `_with_client`
    wrapper — no red pre-flight banner, no ``typer.Exit``; every
    refusal surfaces as the raised error and the caller's quiet dim
    fallback. Test-visible so a fake client can stand in for a live
    supervisor.
    """
    from vesmaro.cli.service import _open_client

    return _open_client(socket)


# ── stats: the wave-0 gate metrics over awareness_events ─────────────────────


def _awareness_stats(
    store: MetricsStore,
    *,
    window_hours: int,
    project: str | None,
) -> dict[str, Any]:
    """Read the awareness funnel aggregates from the sidecar. SQL-in-module.

    Deliberately NOT parameterized SQL input from the operator: the queries
    are fixed literals against the born-final schema; only the int window
    and the (already slug-shaped) project filter bind as parameters. The
    store arrives from :func:`vesmaro.metrics.boundary.create_vitals_store`
    — the sidecar's FILENAME is never spelled outside the metrics package
    (the C1 naming tripwire, tests/test_vitals_phase_a.py) — and its
    connection bootstrap carries the schema/migration ride-along. An
    unusable plane answers ``available=False`` instead of raising
    (telemetry must not stop the operator's console).
    """
    empty: dict[str, Any] = {
        "available": False,
        "sidecar_path": str(store.db_path),
    }
    conn = store._conn()
    if conn is None:
        empty["hint"] = "metrics sidecar unavailable (the plane is broken or disabled)"
        return empty
    cutoff = (datetime.now(UTC) - timedelta(hours=window_hours)).timestamp()
    conds = ["ts >= ?"]
    params: list[Any] = [cutoff]
    if project is not None:
        conds.append("project = ?")
        params.append(project)
    where = " AND ".join(conds)

    by_kind: dict[str, int] = {
        k: 0
        for k in (
            "peer_write",
            "delta_available",
            "heartbeat_delivery",
            "heartbeat_suppressed",
            "tool_call",
            "conflict_hint_emitted",
        )
    }
    for row in conn.execute(
        f"SELECT kind, COUNT(*) AS n FROM awareness_events WHERE {where}"  # nosec B608 - fixed schema literal
        " GROUP BY kind",
        params,
    ):
        if row["kind"] in by_kind:
            by_kind[row["kind"]] = int(row["n"])

    suppressions_by_reason: dict[str, int] = {r: 0 for r in _SUPPRESS_REASONS}
    deliveries_state: dict[str, int] = {"calm": 0, "delta": 0}
    tokens_sum = 0
    tokens_n = 0
    for row in conn.execute(
        f"SELECT kind, meta_json FROM awareness_events WHERE {where}"  # nosec B608 - fixed schema literal
        " AND kind IN ('heartbeat_suppressed','heartbeat_delivery')",
        params,
    ):
        try:
            meta = json.loads(row["meta_json"]) if row["meta_json"] else {}
        except (ValueError, TypeError):
            continue  # unreadable meta — skip the row, never crash stats
        if row["kind"] == "heartbeat_suppressed":
            reason = meta.get("reason")
            if reason in suppressions_by_reason:
                suppressions_by_reason[reason] += 1
        else:
            state = meta.get("state")
            if state in deliveries_state:
                deliveries_state[state] += 1
            tokens = meta.get("tokens_est")
            if isinstance(tokens, int) and not isinstance(tokens, bool):
                tokens_sum += tokens
                tokens_n += 1

    return {
        "available": True,
        "sidecar_path": str(store.db_path),
        "window_hours": window_hours,
        "project": project,
        "events": by_kind,
        "suppressions_by_reason": suppressions_by_reason,
        "deliveries_state": deliveries_state,
        "tokens_sum": tokens_sum,
        "tokens_n": tokens_n,
    }


def _fmt_int(n: int) -> str:
    return str(n)


@awareness_app.command(name="stats")
def awareness_stats(
    window_hours: Annotated[
        int,
        typer.Option("--window-hours", "-w", min=1, max=24 * 90, help="Window in hours."),
    ] = 24,
    project: Annotated[
        str | None,
        typer.Option("--project", "-p", help="Scope the funnel to one project slug."),
    ] = None,
    config: str = ConfigOption,
) -> None:
    """Show the wave-0 gate metrics from the metrics sidecar (no raw SQL).

    Prints the ADR-0035 wave-0 funnel over the `awareness_events` table:
    tool_call denominator, peer_write, delta_available, heartbeat_delivery
    (calm/delta split), heartbeat_suppressed per reason, and the token
    cost of delivering (sum + mean estimated tail tokens). The window is
    the last `--window-hours` (default 24); `--project` scopes to one
    project slug. Exit 0 even when the plane is unavailable — the metrics
    sidecar must not make console reads fatal.
    """
    mgr = get_manager(config)
    store: MetricsStore | None = None
    data: dict[str, Any]
    try:
        # The exact construction the server uses (create_vitals_store):
        # disabled plane / C3 refusal answer as unavailable, never fatal.
        from vesmaro.metrics.boundary import create_vitals_store

        store = create_vitals_store(mgr.settings)
        data = (
            _awareness_stats(store, window_hours=window_hours, project=project)
            if store is not None
            else {
                "available": False,
                "sidecar_path": str(mgr.settings.mnemos.data_dir),
                "hint": "metrics plane disabled by config (vitals.enabled=false"
                " or the C3 session-collection refusal)",
            }
        )
    finally:
        if store is not None:
            store.close()
        mgr.close()

    console.print(
        "[bold]awareness heartbeat — wave-0 funnel[/bold] "
        f"(window {window_hours}h" + (f", project={project})" if project else ")")
    )
    if not data["available"]:
        console.print(
            f"  [yellow]sidecar unavailable:[/yellow] {data.get('hint', data['sidecar_path'])}"
        )
        return
    events = data["events"]
    tool_calls = events["tool_call"]
    deliveries = events["heartbeat_delivery"]
    suppressions = events["heartbeat_suppressed"]
    delta_available = events["delta_available"]
    console.print(f"  tool_call (denominator): {_fmt_int(tool_calls)}")
    console.print(f"  peer_write: {_fmt_int(events['peer_write'])}")
    console.print(f"  delta_available: {_fmt_int(delta_available)}")
    console.print(f"  heartbeat_delivery: {_fmt_int(deliveries)}")
    state = data["deliveries_state"]
    if deliveries:
        console.print(
            f"    — state: calm {_fmt_int(state['calm'])} / delta {_fmt_int(state['delta'])}"
        )
    console.print(f"  heartbeat_suppressed: {_fmt_int(suppressions)}")
    if suppressions:
        reasons = data["suppressions_by_reason"]
        rendered = ", ".join(f"{r}={reasons[r]}" for r in _SUPPRESS_REASONS if reasons[r])
        console.print(f"    — by reason: {rendered if rendered else '(none recorded)'}")
    console.print(f"  conflict_hint_emitted: {_fmt_int(events['conflict_hint_emitted'])}")
    if data["tokens_n"]:
        mean = data["tokens_sum"] / data["tokens_n"]
        console.print(
            f"  tail token cost: sum {_fmt_int(data['tokens_sum'])} over "
            f"{_fmt_int(data['tokens_n'])} deliveries (mean ~{mean:.0f})"
        )
    console.print(f"  [dim]sidecar: {data['sidecar_path']}[/dim]")


__all__ = ["awareness_app", "mode_get", "mode_set"]
