"""``vesma integration *`` CLI subcommands — integration layer management.

Subcommand tree::

    vesma integration detect    — print detected harnesses + deploy paths
    vesma integration setup     — deploy to ALL detected harnesses + wire all agents
    vesma integration update    — bring stale files to current version
    vesma integration verify    — compare deployed files against shipped pack
    vesma integration uninstall — remove only stamped files

The plain ``setup`` command is the full host deployment (owner ruling,
board card ``vesma-integration-setup-default-all``): every detected target
gets the pack and every agent gets wired, non-interactively. Flags narrow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from vesma import __version__
from vesma.cli.agent_wiring import (
    DEFAULT_AGENTS_DIR,
    AgentInfo,
    WireResult,
    WireStatus,
    detect_agents,
    verify_agents,
    wire_agent,
)
from vesma.cli.integration import (
    DeployResult,
    DeployStatus,
    IntegrationManager,
    VerifyResult,
    load_targets,
)

console = Console()

integration_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="integration",
    help=(
        "Manage Vesma integration layer (instructions, skills, prompts, MCP).\n\n"
        "Deploys and verifies the per-harness integration pack into detected "
        "agent tools (instructions, skills, prompts, MCP registration). "
        "`detect` prints what is installed; every deploy verb supports "
        "`--dry-run` and an alternate `--home` for cross-environment installs."
    ),
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


def _resolve_targets(targets: list[str] | str | None, home: Path | None = None) -> list[str]:
    """Resolve ``--target`` value(s) to a concrete list of target names.

    ``None`` or ``"all"`` / ``["all"]`` → every detected target (the
    plain-command default). A plain string is accepted for the commands
    that still use a single ``--target``. Specific names are validated
    against the config; a name that is not detected warns and is skipped,
    an unknown name exits 1. Duplicates are collapsed, order preserved.
    """
    if isinstance(targets, str):
        targets = [targets]
    cfg = load_targets(home=home)

    def _detected_names() -> list[str]:
        detected = cfg.detected()
        if not detected:
            console.print("[yellow]No agent harnesses detected.[/yellow]")
            console.print(
                "  Looked for: " + ", ".join(str(p) for t in cfg.targets for p in t.detect_paths)
            )
            return []
        return [t.name for t in detected]

    if not targets or targets == ["all"]:
        return _detected_names()

    resolved: list[str] = []
    for target in targets:
        if target == "all":
            resolved.extend(_detected_names())
            continue
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
            continue

        if target not in resolved:
            resolved.append(target)

    return resolved


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
        return [agent for agent in agents if not agent.has_vesma and not agent.uses_tool_profile]

    # No selection — wire nothing (the caller handles the interactive prompt).
    return []


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
    """Print detected agent harnesses and their deploy paths.

    Scans the machine for supported harnesses (editors, agent CLIs) and
    shows, per harness, which integration targets exist and where their
    files would deploy. Read-only — the pre-flight before an `install`
    or `update` run, and the way to check what a `--home` override sees.
    """
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
        "\n[dim]Run [bold]vesma integration setup[/bold] to deploy the "
        "integration pack to every detected harness.[/dim]"
    )


@integration_app.command(name="setup")
def setup_cmd(
    target: Annotated[
        list[str] | None,
        typer.Option(
            "--target",
            "-t",
            help="Narrow to specific harness(es); repeatable, e.g. --target copilot "
            "--target zcode. Default: ALL detected harnesses ('all' accepted).",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what would be deployed without writing"),
    ] = False,
    no_mcp: Annotated[
        bool,
        typer.Option("--no-mcp", help="Skip MCP server registration"),
    ] = False,
    vesma_bin: Annotated[
        str | None,
        typer.Option("--vesma-bin", help="Path to the vesma executable for MCP registration"),
    ] = None,
    wire_agents: Annotated[
        bool,
        typer.Option(
            "--wire-agents",
            help="Accepted for backward compatibility — agent wiring is already "
            "the default (no-op).",
        ),
    ] = False,
    no_wire_agents: Annotated[
        bool,
        typer.Option(
            "--no-wire-agents",
            help="Skip agent MCP wiring entirely.",
        ),
    ] = False,
    all_agents: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Accepted for backward compatibility — wiring already covers all "
            "unwired agents (no-op).",
        ),
    ] = False,
    select_agents: Annotated[
        str | None,
        typer.Option(
            "--select",
            help="Narrow agent wiring to these comma-separated agent names/stems.",
        ),
    ] = None,
    precise: Annotated[
        bool,
        typer.Option(
            "--precise",
            help="Use individual vesma/vesma_* tool names instead of vesma/* wildcard.",
        ),
    ] = False,
    home: Annotated[Path | None, HomeOption] = None,
) -> None:
    """Deploys to ALL detected harnesses on this host and wires all agents.

    Use flags to narrow. This is the single entry point: the plain command
    performs the full host deployment — file deployment, MCP registration,
    and agent MCP wiring — in ONE non-interactive pass. Idempotent:
    re-running refreshes stale files without duplicating.

    Narrowing / custom flags:

    * ``--target <name>`` (repeatable) — deploy only the named harness(es);
      ``all`` is accepted and means the default.
    * ``--no-wire-agents`` — skip agent MCP wiring.
    * ``--wire-agents`` — accepted for backward compatibility (docs and
      scripts reference it); wiring is already the default, so alone it is
      a no-op.
    * ``--wire-agents --select a,b`` (or just ``--select a,b``) — wire only
      the named agents.
    * ``--all`` — accepted for backward compatibility; wiring already covers
      all unwired agents, so it is a no-op.
    * ``--precise`` — use individual ``vesma/vesma_*`` tokens instead of
      the ``vesma/*`` wildcard.
    * ``--no-mcp``, ``--dry-run``, ``--home`` — unchanged.

    There is NO interactive prompt anywhere in the default path (owner
    ruling, board card ``vesma-integration-setup-default-all``). A failure
    on one target is reported loudly and never blocks the remaining
    targets (issue #448).
    """
    if wire_agents and no_wire_agents:
        console.print("[red]--wire-agents and --no-wire-agents are mutually exclusive.[/red]")
        raise typer.Exit(1)

    targets = _resolve_targets(target, home)
    if not targets:
        return

    mgr = _manager(home=home)
    failed: list[str] = []

    for name in targets:
        if dry_run:
            console.print(f"[cyan][dry-run][/cyan] Would set up target: [bold]{name}[/bold]")
        else:
            console.print(f"Setting up target: [bold]{name}[/bold]")

        try:
            result = mgr.setup(
                name,
                dry_run=dry_run,
                register_mcp=not no_mcp,
                vesma_bin=vesma_bin,
            )
        except Exception as exc:
            console.print(f"[red]✗ Target {name}: {exc}[/red]")
            failed.append(name)
            continue
        _print_deploy_result(result, dry_run=dry_run)

        if result.mcp_note and not result.mcp_registered and not no_mcp and not dry_run:
            failed.append(name)

    # ── Agent MCP wiring (default-on; --no-wire-agents opts out) ────────────
    mode = "precise" if precise else "wildcard"
    if not no_wire_agents:
        agents = detect_agents()
        if not agents:
            console.print(
                f"[yellow]No agents found in {DEFAULT_AGENTS_DIR} — nothing to wire.[/yellow]"
            )
        else:
            if select_agents:
                to_wire = _resolve_agents_to_wire(agents, select=select_agents, wire_all=False)
            else:
                to_wire = [
                    agent for agent in agents if not agent.has_vesma and not agent.uses_tool_profile
                ]
            if to_wire:
                _run_agent_wiring(to_wire, mode=mode, dry_run=dry_run)
            else:
                console.print("[dim]All agents already wired (or skipped via tool_profile).[/dim]")

    if failed:
        console.print(
            f"\n[yellow]⚠ {len(failed)} target(s) had issues "
            f"({', '.join(failed)}) — see above.[/yellow]"
        )
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
    outdated vesma-integration stamp are touched.
    """
    targets = _resolve_targets(target, home)
    if not targets:
        return

    mgr = _manager(home=home)
    for name in targets:
        console.print(f"Updating target: [bold]{name}[/bold]")
        result = mgr.update(name, dry_run=dry_run)
        _print_deploy_result(result, dry_run=dry_run)

    # cli-audit 2026-10-08 (P3): a dry run must not claim completion.
    if dry_run:
        console.print("\n[cyan]✓[/cyan] Preview complete — nothing written.")
    else:
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
    cfg = load_targets(home=home)
    has_issues = False

    for name in targets:
        result = mgr.verify(name)
        _print_verify_result(result)

        # Memory-switch precedence (ADR-0034 MS-0): show the active mode
        # per wired harness so the operator always sees what governs the
        # relationship between the harness canon and the vesma pack.
        tgt = cfg.get(name)
        if tgt is not None:
            console.print(f"  precedence: {tgt.precedence}")

        if result.stale_count > 0 or result.missing_count > 0:
            has_issues = True
            console.print(
                f"  [yellow]{result.stale_count} stale, {result.missing_count} missing[/yellow]"
            )

    # ── Agent wiring section (informational — does not affect exit code) ────
    # Agent wiring status is reported here for visibility, but it does not
    # change the verify exit code. The dedicated ``vesma doctor`` check
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
            console.print("  [dim]Run `vesma integration setup` to wire.[/dim]")

    if has_issues:
        console.print(
            "\n[yellow]⚠ Stale or missing files detected. "
            "Run [bold]vesma integration update[/bold] to fix.[/yellow]"
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
            removed_label = "Would remove" if dry_run else "Removed"
            console.print(f"  [green]{removed_label} ({len(result.removed)}):[/green]")
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
    if dry_run:
        # cli-audit 2026-10-08 (P1 #8): a dry run used to claim "Uninstall
        # complete: 119 files removed" while nothing was deleted — the plan
        # and the done-deed must never share one wording.
        console.print(
            f"\n[cyan]✓[/cyan] Dry run complete: would remove {total_removed} files, "
            f"{total_skipped} user files would be preserved (nothing deleted — "
            "re-run without --dry-run to apply)."
        )
    else:
        console.print(
            f"\n{prefix}[green]✓[/green] Uninstall complete: "
            f"{total_removed} files removed, {total_skipped} user files preserved."
        )
