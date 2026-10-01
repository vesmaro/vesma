"""``mnemos integration *`` CLI subcommands — integration layer management.

Subcommand tree::

    mnemos integration detect    — print detected harnesses + deploy paths
    mnemos integration setup     — deploy files + register MCP (unified entry point)
    mnemos integration update    — bring stale files to current version
    mnemos integration verify    — compare deployed files against shipped pack
    mnemos integration uninstall — remove only stamped files

All commands support ``--dry-run`` and ``--target`` (default: all detected).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from vesmaro import __version__
from vesmaro.cli.agent_wiring import (
    DEFAULT_AGENTS_DIR,
    AgentInfo,
    WireResult,
    WireStatus,
    detect_agents,
    verify_agents,
    wire_agent,
)
from vesmaro.cli.integration import (
    DeployResult,
    DeployStatus,
    IntegrationManager,
    VerifyResult,
    load_targets,
)

console = Console()

integration_app = typer.Typer(
    name="integration",
    help="Manage Vesma integration layer (instructions, skills, prompts, MCP).",
    no_args_is_help=True,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _manager(pack_root: Path | None = None, home: Path | None = None) -> IntegrationManager:
    """Build an IntegrationManager bound to the current package version."""
    return IntegrationManager(version=__version__, pack_root=pack_root, home=home)


HomeOption = typer.Option(
    "--home",
    help="Deploy into an alternate home directory (cross-environment installs, "
    "e.g. another container's home or a dotfiles checkout). ~ in targets.yaml "
    "resolves against it.",
)


def _resolve_targets(target: str, home: Path | None = None) -> list[str]:
    """Resolve ``--target`` value to a concrete list of target names.

    ``all`` → every detected target. A specific name is validated against
    the config and must be detected (or we warn and skip).
    """
    cfg = load_targets(home=home)
    if target == "all":
        detected = cfg.detected()
        if not detected:
            console.print("[yellow]No agent harnesses detected.[/yellow]")
            console.print(
                "  Looked for: " + ", ".join(str(p) for t in cfg.targets for p in t.detect_paths)
            )
            return []
        return [t.name for t in detected]

    tgt = cfg.get(target)
    if tgt is None:
        console.print(f"[red]Unknown target: {target}[/red]")
        console.print(f"  Available: {', '.join(t.name for t in cfg.targets)}")
        raise typer.Exit(1)

    if not tgt.is_detected():
        console.print(f"[yellow]Target {target!r} not detected (paths missing).[/yellow]")
        console.print("  Detect paths:")
        for p in tgt.detect_paths:
            console.print(f"    {p} {'✓' if p.exists() else '✗'}")
        return []

    return [target]


def _print_deploy_result(result: DeployResult, *, dry_run: bool) -> None:
    """Pretty-print a DeployResult."""
    prefix = "[dry-run] " if dry_run else ""
    table = Table(title=f"{prefix}Target: {result.target_name}", show_lines=False)
    table.add_column("Status", style="bold")
    table.add_column("File")
    table.add_column("Version")
    table.add_column("Note")

    for f in result.files:
        status_style = {
            DeployStatus.DEPLOYED: "green",
            DeployStatus.UPDATED: "yellow",
            DeployStatus.CURRENT: "dim",
            DeployStatus.SKIPPED: "dim",
        }.get(f.status, "white")
        table.add_row(
            f"[{status_style}]{f.status.value}[/{status_style}]",
            str(f.destination),
            f.deployed_version or "—",
            f.note,
        )
    console.print(table)

    if result.mcp_registered or result.mcp_note:
        icon = "[green]✓[/green]" if result.mcp_registered else "[yellow]⚠[/yellow]"
        console.print(f"  {icon} MCP: {result.mcp_note}")


def _print_verify_result(result: VerifyResult) -> None:
    """Pretty-print a VerifyResult."""
    table = Table(title=f"Verify: {result.target_name}")
    table.add_column("Status", style="bold")
    table.add_column("File")
    table.add_column("Deployed")
    table.add_column("Note")

    for f in result.files:
        status_style = {
            DeployStatus.CURRENT: "green",
            DeployStatus.STALE: "yellow",
            DeployStatus.MISSING: "red",
            DeployStatus.OLD_STAMP: "yellow",
            DeployStatus.SKIPPED: "dim",
        }.get(f.status, "white")
        table.add_row(
            f"[{status_style}]{f.status.value}[/{status_style}]",
            str(f.destination),
            f.deployed_version or "—",
            f.note,
        )
    console.print(table)


# ── Agent wiring helpers ──────────────────────────────────────────────────────


def _print_agent_wiring_results(results: list[WireResult], *, dry_run: bool) -> None:
    """Pretty-print a batch of agent wiring results."""
    prefix = "[dry-run] " if dry_run else ""
    table = Table(title=f"{prefix}Agent MCP wiring", show_lines=False)
    table.add_column("Status", style="bold")
    table.add_column("Agent")
    table.add_column("Note")

    for r in results:
        status_style = {
            WireStatus.WIRED: "green",
            WireStatus.DRY_RUN: "cyan",
            WireStatus.ALREADY_WIRED: "dim",
            WireStatus.SKIPPED_TOOL_PROFILE: "dim",
            WireStatus.SKIPPED_NO_FRONTMATTER: "dim",
            WireStatus.ERROR: "red",
        }.get(r.status, "white")
        table.add_row(
            f"[{status_style}]{r.status.value}[/{status_style}]",
            r.name,
            r.note,
        )
    console.print(table)


def _resolve_agents_to_wire(
    agents: list[AgentInfo],
    *,
    select: str | None,
    wire_all: bool,
) -> list[AgentInfo]:
    """Filter the detected agents to the set the user wants to wire.

    Selection logic:

    * ``--all`` → every agent that is not already wired and does not use
      ``tool_profile``.
    * ``--select name1,name2`` → agents whose ``name`` or filename stem
      matches one of the comma-separated selectors. Already-wired and
      ``tool_profile`` agents are still skipped (with a note) to keep the
      operation safe and idempotent.
    """
    if select:
        wanted = {s.strip().lower() for s in select.split(",") if s.strip()}
        selected: list[AgentInfo] = []
        for agent in agents:
            stem = agent.path.stem.removesuffix(".agent")
            keys = {agent.name.lower(), stem.lower(), agent.filename.lower()}
            if keys & wanted:
                selected.append(agent)
        return selected

    if wire_all:
        return [agent for agent in agents if not agent.has_mnemos and not agent.uses_tool_profile]

    # No selection — wire nothing (the caller handles the interactive prompt).
    return []


def _prompt_wire_agents_default(agents: list[AgentInfo]) -> list[AgentInfo]:
    """Interactive Y/n prompt for agent wiring (default flow, no flags).

    When stdin is a TTY: shows a summary and asks ``[Y/n]``.
    When non-interactive (CI / pipe): **skips wiring** (safe default —
    don't modify agent files in CI without an explicit ``--wire-agents``).
    """
    unwired = [agent for agent in agents if not agent.has_mnemos and not agent.uses_tool_profile]
    already = sum(1 for agent in agents if agent.has_mnemos)
    skipped = sum(1 for agent in agents if agent.uses_tool_profile)

    console.print(f"\nFound [bold]{len(agents)}[/bold] agents in [cyan]{DEFAULT_AGENTS_DIR}[/cyan]")
    console.print(
        f"  [green]{already}[/green] already wired, "
        f"[yellow]{len(unwired)}[/yellow] need wiring, "
        f"[dim]{skipped} skipped (tool_profile)[/dim]"
    )

    if not unwired:
        console.print("  [dim]Nothing to wire — all agents already have Vesma tools.[/dim]")
        return []

    # Non-interactive: safe default is to SKIP (don't modify agent files in CI).
    if not sys.stdin.isatty():
        console.print(
            "[dim]Non-interactive terminal — skipping agent wiring "
            "(use --wire-agents to force).[/dim]"
        )
        return []

    answer = console.input("Wire Vesma MCP to all Copilot agents? [Y/n] ").strip().lower()
    if answer in ("", "y", "yes"):
        return unwired
    return []


def _prompt_wire_agents_interactive(agents: list[AgentInfo]) -> list[AgentInfo]:
    """Interactive numbered prompt for ``--wire-agents`` without ``--all``.

    Offers three choices: wire all, select by name, or skip. Falls back
    to wiring all unwired agents when stdin is not a TTY (CI / pipe) —
    the user explicitly asked for wiring via ``--wire-agents``.
    """
    unwired = [agent for agent in agents if not agent.has_mnemos and not agent.uses_tool_profile]
    already = sum(1 for agent in agents if agent.has_mnemos)
    skipped = sum(1 for agent in agents if agent.uses_tool_profile)

    console.print(f"\nFound [bold]{len(agents)}[/bold] agents in [cyan]{DEFAULT_AGENTS_DIR}[/cyan]")
    console.print(
        f"  [green]{already}[/green] already wired, "
        f"[yellow]{len(unwired)}[/yellow] need wiring, "
        f"[dim]{skipped} skipped (tool_profile)[/dim]"
    )

    if not unwired:
        console.print("  [dim]Nothing to wire — all agents already have Vesma tools.[/dim]")
        return []

    # Non-interactive fallback: user passed --wire-agents, so wire all.
    if not sys.stdin.isatty():
        console.print("[yellow]⚠ Non-interactive terminal — wiring all unwired agents.[/yellow]")
        return unwired

    console.print("\n  [bold]Options:[/bold]")
    console.print("  [1] Wire all unwired agents")
    console.print("  [2] Select specific agents by name")
    console.print("  [3] Skip agent wiring")
    choice = console.input("\n  Choice [1-3] (default 1): ").strip() or "1"

    if choice == "3":
        return []
    if choice == "2":
        raw = console.input("  Enter agent names (comma-separated, e.g. tech-lead,code-reviewer): ")
        return _resolve_agents_to_wire(agents, select=raw, wire_all=False)

    return unwired


def _run_agent_wiring(
    agents: list[AgentInfo],
    *,
    mode: str,
    dry_run: bool,
) -> list[WireResult]:
    """Wire a list of agents and print the results. Returns the results."""
    results = [wire_agent(agent.path, mode=mode, dry_run=dry_run) for agent in agents]
    _print_agent_wiring_results(results, dry_run=dry_run)
    return results


# ── Commands ──────────────────────────────────────────────────────────────────


@integration_app.command(name="detect")
def detect_cmd(
    home: Annotated[Path | None, HomeOption] = None,
) -> None:
    """Print detected agent harnesses and their deploy paths."""
    cfg = load_targets(home=home)
    detected = cfg.detected()

    if not detected:
        console.print("[yellow]No agent harnesses detected.[/yellow]")
        console.print("\nSearched for:")
        for t in cfg.targets:
            for p in t.detect_paths:
                console.print(f"  {t.name}: {p}")
        return

    table = Table(title="Detected harnesses")
    table.add_column("Target", style="bold cyan")
    table.add_column("Detect path")
    table.add_column("Deploy map")

    for t in detected:
        detect_str = "\n".join(str(p) for p in t.detect_paths)
        deploy_str = "\n".join(f"{k} → {v}" for k, v in t.deploy_map.items())
        table.add_row(t.name, detect_str, deploy_str)
    console.print(table)

    console.print(
        "\n[dim]Run [bold]mnemos integration setup --target all[/bold] "
        "to deploy the integration pack.[/dim]"
    )


@integration_app.command(name="setup")
def setup_cmd(
    target: Annotated[
        str,
        typer.Option(
            "--target",
            "-t",
            help="Target harness: all | any name in targets.yaml, e.g. copilot, cursor, "
            "hermes, zcode, agents, pi, opencode (default: all detected)",
        ),
    ] = "all",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would be deployed without writing"),
    ] = False,
    no_mcp: Annotated[
        bool,
        typer.Option("--no-mcp", help="Skip MCP server registration"),
    ] = False,
    mnemos_bin: Annotated[
        str | None,
        typer.Option("--mnemos-bin", help="Path to mnemos executable for MCP registration"),
    ] = None,
    wire_agents: Annotated[
        bool,
        typer.Option(
            "--wire-agents",
            help="Wire mnemos/* into Copilot agent tools: frontmatter (prompts if interactive).",
        ),
    ] = False,
    no_wire_agents: Annotated[
        bool,
        typer.Option(
            "--no-wire-agents",
            help="Skip agent MCP wiring (explicit opt-out, no prompt).",
        ),
    ] = False,
    all_agents: Annotated[
        bool,
        typer.Option(
            "--all",
            help="With --wire-agents: wire all unwired agents (no prompt).",
        ),
    ] = False,
    select_agents: Annotated[
        str | None,
        typer.Option(
            "--select",
            help="With --wire-agents: comma-separated agent names/stems to wire.",
        ),
    ] = None,
    precise: Annotated[
        bool,
        typer.Option(
            "--precise",
            help="Use individual mnemos/mnemos_* tool names instead of mnemos/* wildcard.",
        ),
    ] = False,
    home: Annotated[Path | None, HomeOption] = None,
) -> None:
    """Deploy instructions + skills + prompts, register MCP, and wire agents.

    This is the single entry point: running ``mnemos integration setup`` wires
    everything — file deployment, MCP registration, and agent MCP wiring — in
    one pass. Idempotent: re-running updates stale files without duplicating.

    Agent wiring flags:

    * ``--wire-agents`` — enable agent wiring (interactive prompt by default).
    * ``--wire-agents --all`` — wire all unwired agents without prompting.
    * ``--wire-agents --select name1,name2`` — wire only specified agents.
    * ``--no-wire-agents`` — skip agent wiring entirely (no prompt).
    * ``--precise`` — use individual ``mnemos/mnemos_*`` tokens instead of
      the ``mnemos/*`` wildcard.
    * ``--dry-run`` — show what would change without modifying files.

    If neither ``--wire-agents`` nor ``--no-wire-agents`` is passed, the
    command prompts interactively (``[Y/n]``) when stdin is a TTY. In a
    non-interactive terminal (CI / pipe), agent wiring is **skipped** as a
    safe default — use ``--wire-agents`` to force wiring in CI.
    """
    if wire_agents and no_wire_agents:
        console.print("[red]--wire-agents and --no-wire-agents are mutually exclusive.[/red]")
        raise typer.Exit(1)

    targets = _resolve_targets(target, home)
    if not targets:
        return

    mgr = _manager(home=home)
    any_failed = False

    for name in targets:
        if dry_run:
            console.print(f"[cyan][dry-run][/cyan] Would set up target: [bold]{name}[/bold]")
        else:
            console.print(f"Setting up target: [bold]{name}[/bold]")

        result = mgr.setup(
            name,
            dry_run=dry_run,
            register_mcp=not no_mcp,
            mnemos_bin=mnemos_bin,
        )
        _print_deploy_result(result, dry_run=dry_run)

        if result.mcp_note and not result.mcp_registered and not no_mcp and not dry_run:
            any_failed = True

    # ── Agent MCP wiring ────────────────────────────────────────────────────
    mode = "precise" if precise else "wildcard"
    if not no_wire_agents:
        agents = detect_agents()
        if not agents:
            if wire_agents:
                console.print(
                    f"[yellow]No agents found in {DEFAULT_AGENTS_DIR} — skipping wiring.[/yellow]"
                )
        elif wire_agents:
            to_wire = _resolve_agents_to_wire(agents, select=select_agents, wire_all=all_agents)
            if not to_wire and not select_agents and not all_agents:
                to_wire = _prompt_wire_agents_interactive(agents)
            if to_wire:
                _run_agent_wiring(to_wire, mode=mode, dry_run=dry_run)
            else:
                console.print("[dim]No agents selected for wiring.[/dim]")
        else:
            # Default flow (no --wire-agents / --no-wire-agents): prompt
            # interactively. Non-interactive terminals skip safely.
            to_wire = _prompt_wire_agents_default(agents)
            if to_wire:
                _run_agent_wiring(to_wire, mode=mode, dry_run=dry_run)

    if any_failed:
        console.print("\n[yellow]⚠ Some steps had issues — see above.[/yellow]")
        raise typer.Exit(1)

    console.print("\n[green]✓[/green] Setup complete.")


@integration_app.command(name="update")
def update_cmd(
    target: Annotated[
        str,
        typer.Option("--target", "-t", help="Target: all | <name> (default: all detected)"),
    ] = "all",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would be updated without writing"),
    ] = False,
    home: Annotated[Path | None, HomeOption] = None,
) -> None:
    """Update already-deployed files to the current package version.

    Uses the version stamp to detect stale files. Only files carrying an
    outdated mnemos-integration stamp are touched.
    """
    targets = _resolve_targets(target, home)
    if not targets:
        return

    mgr = _manager(home=home)
    for name in targets:
        console.print(f"Updating target: [bold]{name}[/bold]")
        result = mgr.update(name, dry_run=dry_run)
        _print_deploy_result(result, dry_run=dry_run)

    console.print("\n[green]✓[/green] Update complete.")


@integration_app.command(name="verify")
def verify_cmd(
    target: Annotated[
        str,
        typer.Option("--target", "-t", help="Target: all | <name> (default: all detected)"),
    ] = "all",
    home: Annotated[Path | None, HomeOption] = None,
) -> None:
    """Compare deployed files against the shipped pack.

    Reports: installed (version), stale, missing. Exits 0 if all current,
    exits 1 if any stale or missing files are found.
    """
    targets = _resolve_targets(target, home)
    if not targets:
        return

    mgr = _manager(home=home)
    has_issues = False

    for name in targets:
        result = mgr.verify(name)
        _print_verify_result(result)

        if result.stale_count > 0 or result.missing_count > 0:
            has_issues = True
            console.print(
                f"  [yellow]{result.stale_count} stale, {result.missing_count} missing[/yellow]"
            )
        if result.old_stamp_count > 0:
            console.print(
                f"  [yellow]{result.old_stamp_count} old-stamp "
                f"(legacy mnemos-integration marker — `integration update` re-stamps)[/yellow]"
            )

    # ── Agent wiring section (informational — does not affect exit code) ────
    # Agent wiring status is reported here for visibility, but it does not
    # change the verify exit code. The dedicated ``mnemos doctor`` check
    # governs the wiring health gate. This keeps ``verify`` focused on file
    # staleness/missing, which is what CI pipelines expect.
    agent_summary = verify_agents()
    if agent_summary.total > 0:
        console.print(
            f"\nAgents:        [green]{agent_summary.wired}[/green]/"
            f"{agent_summary.total} wired, "
            f"[dim]{agent_summary.skipped_tool_profile} skipped (tool_profile)[/dim], "
            f"[yellow]{agent_summary.unwired} unwired[/yellow], "
            f"[red]{agent_summary.errors} errors[/red]"
        )
        if agent_summary.unwired_names:
            preview = ", ".join(agent_summary.unwired_names[:10])
            more = (
                f" ... and {len(agent_summary.unwired_names) - 10} more"
                if len(agent_summary.unwired_names) > 10
                else ""
            )
            console.print(f"  Unwired:     {preview}{more}")
        if agent_summary.unwired > 0:
            console.print(
                "  [dim]Run `mnemos integration setup --wire-agents --all` to wire.[/dim]"
            )

    if has_issues:
        console.print(
            "\n[yellow]⚠ Stale or missing files detected. "
            "Run [bold]mnemos integration update[/bold] to fix.[/yellow]"
        )
        raise typer.Exit(1)
    else:
        console.print("\n[green]✓[/green] All files current.")


@integration_app.command(name="uninstall")
def uninstall_cmd(
    target: Annotated[
        str,
        typer.Option("--target", "-t", help="Target: all | <name> (default: all detected)"),
    ] = "all",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would be removed without deleting"),
    ] = False,
    home: Annotated[Path | None, HomeOption] = None,
) -> None:
    """Remove ONLY files carrying the pack's version stamp (both generations).

    User-created files are never deleted. Also unregisters the MCP server
    entry the pack registered (foreign entries are never touched). Lists
    what was removed (or would be removed with ``--dry-run``).
    """
    targets = _resolve_targets(target, home)
    if not targets:
        return

    mgr = _manager(home=home)
    total_removed = 0
    total_skipped = 0

    for name in targets:
        prefix = "[dry-run] " if dry_run else ""
        console.print(f"{prefix}Uninstalling from target: [bold]{name}[/bold]")
        result = mgr.uninstall(name, dry_run=dry_run)

        if result.mcp_note:
            icon = "[green]✓[/green]" if result.mcp_unregistered else "[dim]·[/dim]"
            console.print(f"  {icon} MCP: {result.mcp_note}")

        if result.removed:
            console.print(f"  [green]Removed ({len(result.removed)}):[/green]")
            for p in result.removed:
                console.print(f"    ✗ {p}")
            total_removed += len(result.removed)
        else:
            console.print("  [dim]No stamped files found.[/dim]")

        if result.skipped_user_files:
            console.print(f"  [dim]Skipped user files ({len(result.skipped_user_files)}):[/dim]")
            for p in result.skipped_user_files[:10]:
                console.print(f"    ✓ {p} (no stamp)")
            if len(result.skipped_user_files) > 10:
                console.print(f"    ... and {len(result.skipped_user_files) - 10} more")
            total_skipped += len(result.skipped_user_files)

    prefix = "[dry-run] " if dry_run else ""
    console.print(
        f"\n{prefix}[green]✓[/green] Uninstall complete: "
        f"{total_removed} files removed, {total_skipped} user files preserved."
    )
