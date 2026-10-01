"""Vesma CLI — Typer-based command interface.

Entry point: mnemos (declared in pyproject.toml [project.scripts]).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from rich.console import Console
from rich.table import Table

from vesmaro.cli._manager import get_manager
from vesmaro.config import find_config_file, load_settings
from vesmaro.logging_setup import setup_logging
from vesmaro.models import MemoryCreate, MemorySource, MemoryType
from vesmaro.storage.sqlite_store import (
    EDGE_STATS_LAST_PURGE_META_KEY,
    EDGE_STATS_TOTAL_ROWS_CAP,
)

# ``get_manager`` lives in the leaf module ``_manager`` so that CLI
# subcommand modules (export_cmd, import_cmd) can import it without forming
# a circular import with this module (which imports them to register the
# Typer sub-apps). Re-exported here for backward compatibility.

if TYPE_CHECKING:
    from vesmaro.api.auth_store import AuthStore

app = typer.Typer(
    name="mnemos",
    help="Vesma — standalone memory & knowledge server for AI agents.",
    no_args_is_help=True,
)
console = Console()

# Module-level verbose flag — set by the top-level callback via ``--verbose``
# so that subcommands (serve, mcp-server) can pick it up without re-parsing.
_verbose: bool = False


def _print_update_hint() -> None:
    """One stderr line when a newer release exists (issue #445).

    Best-effort by contract: the check never raises, is served from the
    24h disk cache when warm, and is capped by the module's 3s HTTP
    timeout. Honors both opt-outs (``updates.check_enabled`` and the
    ``VESMARO_UPDATES_CHECK`` env kill switch) via ``check_for_update``.
    """
    try:
        from vesmaro.updates import check_for_update

        info = check_for_update()
    except Exception:
        return
    if info is not None and info.update_available:
        print(f"update available: {info.latest} (run 'vesma update --check')", file=sys.stderr)


def _version_callback(value: bool) -> None:
    if value:
        from vesmaro import __version__

        console.print(f"vesma {__version__}")
        _print_update_hint()
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show the Vesma version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Enable DEBUG logging (overrides config log level).",
        ),
    ] = False,
) -> None:
    """Vesma — standalone memory & knowledge server for AI agents."""
    global _verbose
    _verbose = verbose


ConfigOption = typer.Option(None, "--config", "-c", help="Path to config.yaml")


# ── add ────────────────────────────────────────────────────────────────────────


@app.command()
def add(
    content: str = typer.Argument(None, help="Text content to remember"),
    title: str = typer.Option(None, "--title", "-t"),
    tags: str = typer.Option("", "--tags", "-T", help="Comma-separated tags"),
    file: Annotated[Path | None, typer.Option("--file", "-f", help="Import from file")] = None,
    url: str = typer.Option(None, "--url", "-u", help="Import from URL"),
    source: Annotated[MemorySource, typer.Option("--source", "-s")] = MemorySource.CLI,
    memory_type: Annotated[MemoryType, typer.Option("--type")] = MemoryType.NOTE,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show context-filter stats (profile, token reduction, dedup, noise) "
            "without saving the memory.",
        ),
    ] = False,
    config: str = ConfigOption,
) -> None:
    """Add a new memory entry.

    With ``--dry-run``: validates the tag contract, runs the context filter
    pipeline on the content, and prints filter stats **without saving**.
    Useful for previewing how the M10 Context Filter will transform input
    before committing it to the store.
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    # ── --dry-run: validate tags + run filter, then exit without saving ──
    if dry_run:
        if url:
            console.print(
                "[red]--dry-run is not supported with --url (content is fetched at "
                "ingest time).[/red]"
            )
            raise typer.Exit(1)
        if file:
            text = Path(file).read_text(encoding="utf-8")
        elif content:
            text = content
        else:
            stdin_text = sys.stdin.read().strip()
            if not stdin_text:
                console.print("[red]No content provided.[/red]")
                raise typer.Exit(1)
            text = stdin_text

        # Validate tag contract (raises TagContractError in strict mode).
        from vesmaro.config import load_settings as _load_settings
        from vesmaro.filter.pipeline import apply_filter
        from vesmaro.models import validate_tag_contract

        settings = _load_settings(config)
        validate_tag_contract(tag_list, strict=settings.mnemos.strict_tag_contract)

        result = apply_filter(text)
        stats = result["stats"]
        tokens_in = len(text) // 4 or 1
        tokens_out = stats["tokens"]["estimated_tokens"]
        reduction_pct = round((1 - tokens_out / tokens_in) * 100, 1) if tokens_in else 0.0

        console.print("[cyan][dry-run][/cyan] Filter preview (no memory saved):")
        console.print(f"  Input:     {tokens_in} tokens")
        console.print(f"  Output:    {tokens_out} tokens ({reduction_pct}% reduction)")
        console.print(f"  Profile:   {result['profile']} (auto-detected)")
        dedup = stats.get("dedup", {})
        console.print(
            f"  Dedup:     {dedup.get('exact_dups', 0)} exact, "
            f"{dedup.get('near_dups', 0)} near-duplicates removed"
        )
        noise = stats.get("noise", {})
        noise_lines = (
            noise.get("removed_ansi", 0)
            + noise.get("removed_progress", 0)
            + noise.get("removed_timestamps", 0)
            + noise.get("removed_separators", 0)
        )
        console.print(f"  Noise:     {noise_lines} lines cleaned")
        budget = stats["tokens"].get("budget")
        console.print(f"  Budget:    {budget if budget else 'not set (no truncation)'}")
        console.print("[dim][dry-run] Memory would be saved with these filter stats.[/dim]")
        return

    mgr = get_manager(config)

    if url:
        project = next((t[len("project:") :] for t in tag_list if t.startswith("project:")), "")
        agent = next((t[len("agent:") :] for t in tag_list if t.startswith("agent:")), "")
        with console.status("Fetching URL..."):
            memory = mgr.ingest_url(url, tags=tag_list, project=project, agent=agent)
    elif file:
        text = Path(file).read_text()
        data = MemoryCreate(content=text, title=title, tags=tag_list, source=source)
        project = next((t[len("project:") :] for t in tag_list if t.startswith("project:")), "")
        agent = next((t[len("agent:") :] for t in tag_list if t.startswith("agent:")), "")
        memory = mgr.add(data, project=project, agent=agent)
    elif content:
        data = MemoryCreate(
            content=content, title=title, tags=tag_list, source=source, memory_type=memory_type
        )
        project = next((t[len("project:") :] for t in tag_list if t.startswith("project:")), "")
        agent = next((t[len("agent:") :] for t in tag_list if t.startswith("agent:")), "")
        memory = mgr.add(data, project=project, agent=agent)
    else:
        stdin_text = sys.stdin.read().strip()
        if not stdin_text:
            console.print("[red]No content provided.[/red]")
            raise typer.Exit(1)
        data = MemoryCreate(content=stdin_text, title=title, tags=tag_list, source=source)
        memory = mgr.add(data)

    console.print(f"[green]✓[/green] Saved: {memory.auto_title()} ({memory.id})")


# ── search ─────────────────────────────────────────────────────────────────────


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(10, "--limit", "-l", help="Max results"),
    project: str = typer.Option(None, "--project", "-p", help="Filter by project slug"),
    tags: str = typer.Option(None, "--tags", "-T", help="Comma-separated tags to filter by"),
    include_raw: bool = typer.Option(
        True,
        "--include-raw/--published-only",
        help=(
            "Include raw/processing entries. Default: include — a just-added "
            "memory stays raw until the knowledge pipeline publishes it, and "
            "the interactive CLI must find what was just added (issue #123). "
            "Use --published-only to restrict to pipeline-published knowledge."
        ),
    ),
    status: str = typer.Option(
        None,
        "--status",
        help=(
            "Filter by status (raw/processing/processed/published/archived). "
            "Takes precedence over --include-raw."
        ),
    ),
    config: str = ConfigOption,
) -> None:
    """Search long-term memory (hybrid FTS + vector).

    By default the CLI surfaces every status except archived — unlike the
    MCP/HTTP surfaces (published-only by default), the interactive CLI must
    complete the first add → search roundtrip from a clean install with no
    running server and no pipeline pass yet (ADR-0017 Phase 0).
    """
    from vesmaro.models import MemoryStatus

    mgr = get_manager(config)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None
    status_enum = MemoryStatus(status) if status else None
    results = mgr.search(
        query=query,
        tags=tag_list,
        project=project,
        limit=limit,
        include_raw=include_raw,
        status=status_enum,
    )
    if not results:
        console.print("[yellow]No results found.[/yellow]")
        return
    table = Table("Score", "Title", "Tags", "Status")
    for r in results:
        table.add_row(
            f"{r.score:.3f}",
            r.memory.auto_title(),
            ", ".join(r.memory.tags[:5]),
            r.memory.status,
        )
    console.print(table)


# ── recall ─────────────────────────────────────────────────────────────────────


@app.command()
def recall(
    project: str = typer.Option(None, "--project", "-p"),
    agent: str = typer.Option(None, "--agent", "-a", help="Filter by agent slug (M3)"),
    limit: int = typer.Option(10, "--limit", "-l"),
    config: str = ConfigOption,
) -> None:
    """Recall recent memories, optionally filtered by project or agent."""
    from vesmaro.models import AgentRecallQuery

    mgr = get_manager(config)
    if agent:
        results = mgr.agent_recall(AgentRecallQuery(agent=agent, project=project, limit=limit))
        memories = [r.memory for r in results]
    else:
        memories = mgr.recall_context(project=project or "", limit=limit)

    if not memories:
        console.print("[yellow]No memories found.[/yellow]")
        return
    for m in memories:
        console.print(f"[cyan]{m.auto_title()}[/cyan]  ({m.id[:8]}…)")
        console.print(f"  tags: {', '.join(m.tags)}")
        console.print()


# ── tags (M2) ─────────────────────────────────────────────────────────────────
# Subcommand tree:
#   mnemos tags validate <vault>   — validate tag contract across a vault

_tags_app = typer.Typer(name="tags", help="Manage and validate memory tags.", no_args_is_help=True)
app.add_typer(_tags_app, name="tags")


@_tags_app.command(name="validate")
def tags_validate(
    vault: Annotated[Path, typer.Argument(help="Path to Vesma vault directory")],
    config: str = ConfigOption,
) -> None:
    """Validate tag contract across an existing vault. Reports non-conformant entries."""

    console.print(f"[bold]Validating tag contract in:[/bold] {vault}")
    # TODO (M2): scan SQLite + vault markdown files
    console.print(
        "[yellow]Full vault scan not yet implemented (M2 storage layer pending).[/yellow]"
    )


@_tags_app.command(name="normalize")
def tags_normalize(
    config: str = ConfigOption,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview changes without writing to the database."),
    ] = False,
) -> None:
    """Normalize project:/agent: tag case to lowercase across all memories.

    Scans every memory in the SQLite store, lowercases the slug portion of
    project: and agent: tags and replaces spaces with hyphens (matching the
    lax-mode normalization in ``validate_tag_contract``), and updates
    memories that changed via ``update_fields`` (plain UPDATE). The FTS5
    ``AFTER UPDATE`` trigger fires on UPDATE so the search index stays
    consistent; using ``save()`` (INSERT OR REPLACE) here would risk
    desyncing the FTS5 external content table. The denormalised ``project``
    and ``agent`` columns are updated in the same statement.
    """

    mgr = get_manager(config)
    # Page through all memories — list_all paginates with limit/offset.
    page_size = 500
    offset = 0
    scanned = 0
    normalized_count = 0
    changed_projects: set[str] = set()
    changed_agents: set[str] = set()

    while True:
        batch = mgr.sqlite.list_all(limit=page_size, offset=offset)
        if not batch:
            break
        offset += len(batch)

        for mem in batch:
            scanned += 1
            new_tags = list(mem.tags)
            modified = False

            for i, tag in enumerate(new_tags):
                for prefix in ("project:", "agent:"):
                    if tag.startswith(prefix):
                        slug = tag[len(prefix) :]
                        # Match validate_tag_contract lax-mode normalization:
                        # strip, lowercase AND replace spaces with hyphens.
                        # Without the strip step, `project:My Project ` would
                        # become `project:-my-project-` (leading/trailing
                        # hyphens from unstripped spaces). Without the
                        # space→hyphen step the CLI diverged from the
                        # contract, leaving `project:My Project` as
                        # `project:my project` (invalid slug with a space).
                        lower = slug.strip().lower().replace(" ", "-")
                        if lower != slug:
                            new_tags[i] = prefix + lower
                            modified = True
                            if prefix == "project:":
                                changed_projects.add(f"{slug} → {lower}")
                            else:
                                changed_agents.add(f"{slug} → {lower}")

            if not modified:
                continue

            normalized_count += 1
            if dry_run:
                continue

            # Use update_fields (UPDATE) not save() (INSERT OR REPLACE).
            # INSERT OR REPLACE can desync the FTS5 external content table
            # (`content=memories`), causing "missing row from content
            # table" errors on subsequent searches. A plain UPDATE fires
            # the `memories_au` AFTER UPDATE trigger which keeps the FTS5
            # index consistent. Also update the denormalised `project` and
            # `agent` columns so per-project / per-agent queries stay in
            # sync with the normalized tags.
            new_project = next(
                (t[len("project:") :] for t in new_tags if t.startswith("project:")),
                mem.project,
            )
            new_agent = next(
                (t[len("agent:") :] for t in new_tags if t.startswith("agent:")),
                mem.agent,
            )
            mgr.sqlite.update_fields(
                mem.id,
                tags=new_tags,
                project=new_project,
                agent=new_agent,
            )

    console.print(f"[bold]Scanned:[/bold] {scanned} memories")
    console.print(f"[bold]Normalized:[/bold] {normalized_count} memories")
    if dry_run:
        console.print("[yellow](dry-run — no changes written)[/yellow]")
    if changed_projects:
        console.print("\n[bold]Changed project slugs:[/bold]")
        for entry in sorted(changed_projects):
            console.print(f"  {entry}")
    if changed_agents:
        console.print("\n[bold]Changed agent slugs:[/bold]")
        for entry in sorted(changed_agents):
            console.print(f"  {entry}")


@_tags_app.command(name="rename")
def tags_rename(
    from_prefix: Annotated[
        str,
        typer.Option(
            "--from",
            help="Source prefix (e.g. 'gcw:'). Must end with ':'",
        ),
    ],
    to_prefix: Annotated[
        str,
        typer.Option(
            "--to",
            help="Target prefix (e.g. 'mnemos:'). Must end with ':'",
        ),
    ],
    subtypes: Annotated[
        list[str] | None,
        typer.Option(
            "--subtypes",
            help="Restrict rename to these subtypes (repeatable). Default: all.",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Preview changes without writing to the database (default: True).",
        ),
    ] = True,
    no_dry_run: Annotated[
        bool,
        typer.Option(
            "--no-dry-run",
            help="Actually write the rename (overrides --dry-run).",
        ),
    ] = False,
    project: Annotated[
        str | None,
        typer.Option("--project", help="Scope the scan to a project slug."),
    ] = None,
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Scope the scan to an agent slug."),
    ] = None,
    invalid_to_legacy: Annotated[
        bool,
        typer.Option(
            "--invalid-to-legacy",
            help="Rename invalid subtypes to <to_prefix>legacy instead of skipping.",
        ),
    ] = False,
    config: str = ConfigOption,
) -> None:
    """Bulk rename tags matching ``--from <prefix>`` → ``--to <prefix>``.

    Safe replacement for ``mnemos migrate tags``. Uses ``update_fields``
    (plain UPDATE) so the FTS5 external-content index stays consistent.
    Idempotent — a second run with the same args reports ``renamed=0``.

    Default is ``--dry-run`` (preview only). Pass ``--no-dry-run`` to apply.
    """

    mgr = get_manager(config)
    report = mgr.tags_rename(
        from_prefix=from_prefix,
        to_prefix=to_prefix,
        subtypes=subtypes,
        dry_run=dry_run and not no_dry_run,
        project=project,
        agent=agent,
        invalid_subtypes_to_legacy=invalid_to_legacy,
    )

    table = Table(title="Tags rename report", show_header=True, header_style="bold")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="white")
    table.add_row("From prefix", str(report["from_prefix"]))
    table.add_row("To prefix", str(report["to_prefix"]))
    table.add_row("Scanned", str(report["scanned"]))
    table.add_row("Renamed", str(report["renamed"]))
    table.add_row("Skipped (invalid)", str(report["skipped_invalid"]))
    table.add_row("Errors", str(len(report["errors"])))
    if report["dry_run"]:
        table.add_row("Mode", "[yellow]dry-run (no writes)[/yellow]")
    else:
        table.add_row("Mode", "[green]applied[/green]")
    console.print(table)

    if report["errors"]:
        console.print("\n[bold red]Errors:[/bold red]")
        for err in report["errors"][:20]:
            console.print(f"  - {err}")
        if len(report["errors"]) > 20:
            console.print(f"  ... ({len(report['errors']) - 20} more)")


# ── workflow (#96) ────────────────────────────────────────────────────────────
#
# Thin CLI wrappers over MemoryManager.workflow_set / workflow_get /
# workflow_history. The state machine + 5 guardrails are enforced server-side
# in the manager, so these commands only translate ValueError (guardrail /
# state-machine violation) into a red error line + exit 1 — mirroring how the
# MCP tool and REST layer surface the same violations.

_workflow_app = typer.Typer(
    name="workflow",
    help=(
        "Manage the workflow lifecycle of a memory "
        "(states: open, in-progress, blocked, resolved, done, withdrawn)."
    ),
    no_args_is_help=True,
)
app.add_typer(_workflow_app, name="workflow")


@_workflow_app.command(name="get")
def workflow_get(
    memory_id: Annotated[str, typer.Argument(help="Target memory id.")],
    config: str = ConfigOption,
) -> None:
    """Show the current workflow status + lock owner for a memory."""

    mgr = get_manager(config)
    result = mgr.workflow_get(memory_id)
    if result is None:
        console.print(f"[red]✗[/red] Memory {memory_id!r} not found")
        raise typer.Exit(1)

    table = Table(title=f"Workflow — {memory_id}", show_header=False)
    table.add_column("Field", style="cyan")
    table.add_column("Value", style="white")
    table.add_row("workflow_status", result["workflow_status"])
    table.add_row("locked_by", str(result["locked_by"]))
    table.add_row("locked_at", str(result["locked_at"]))
    console.print(table)


@_workflow_app.command(name="set")
def workflow_set(
    memory_id: Annotated[str, typer.Argument(help="Target memory id.")],
    to: Annotated[
        str,
        typer.Option(
            "--to",
            help="Target status: open|in-progress|blocked|resolved|done|withdrawn.",
        ),
    ],
    actor: Annotated[
        str,
        typer.Option("--actor", help="Free-form actor id (Phase 1 weak identity)."),
    ],
    reason: Annotated[
        str, typer.Option("--reason", help="Human-readable reason. Required with --force.")
    ] = "",
    force: Annotated[
        bool,
        typer.Option("--force", help="Override a lock held by another actor (requires --reason)."),
    ] = False,
    config: str = ConfigOption,
) -> None:
    """Transition a memory's workflow status through the server-enforced state machine."""

    mgr = get_manager(config)
    try:
        result = mgr.workflow_set(memory_id, to, actor=actor, reason=reason, force=force)
    except ValueError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(1) from None

    table = Table(title=f"Workflow transition — {memory_id}", show_header=False)
    table.add_column("Field", style="cyan")
    table.add_column("Value", style="white")
    table.add_row("from_status", str(result["from_status"]))
    table.add_row("to_status", result["to_status"])
    table.add_row("actor", result["actor"])
    table.add_row("locked_by", str(result["locked_by"]))
    table.add_row("force_used", str(result["force_used"]))
    table.add_row("stale_lock_released", str(result["stale_lock_released"]))
    table.add_row("idempotent", str(result["idempotent"]))
    table.add_row("recorded", str(result["recorded"]))
    if result["terminal"]:
        table.add_row("terminal", "[yellow]yes — no further transitions[/yellow]")
    console.print(table)


@_workflow_app.command(name="history")
def workflow_history(
    memory_id: Annotated[str, typer.Argument(help="Target memory id.")],
    limit: Annotated[int, typer.Option("--limit", help="Max rows to show (newest first).")] = 50,
    config: str = ConfigOption,
) -> None:
    """Show the workflow transition audit log for a memory (newest first)."""

    mgr = get_manager(config)
    rows = mgr.workflow_history(memory_id, limit=limit)
    if not rows:
        console.print(f"[yellow]No workflow transitions recorded for {memory_id!r}.[/yellow]")
        return

    table = Table(title=f"Workflow history — {memory_id}")
    table.add_column("Time (UTC)", style="cyan")
    table.add_column("From", style="white")
    table.add_column("To", style="green")
    table.add_column("Actor", style="white")
    table.add_column("Force", style="yellow")
    table.add_column("Reason", style="white")
    for row in rows:
        table.add_row(
            row.get("created_at", ""),
            str(row.get("from_status")),
            str(row.get("to_status")),
            str(row.get("actor")),
            "yes" if row.get("force_used") else "",
            str(row.get("reason") or ""),
        )
    console.print(table)


# ── stats ──────────────────────────────────────────────────────────────────────


@app.command()
def stats(config: str = ConfigOption) -> None:
    """Display Vesma health statistics."""
    mgr = get_manager(config)
    s = mgr.stats()
    for k, v in s.items():
        console.print(f"  [bold]{k}[/bold]: {v}")


@app.command(name="fts")
def fts_cmd(
    action: str = typer.Argument(..., help="Action: rebuild"),
    config: str = ConfigOption,
) -> None:
    """FTS5 index management."""
    mgr = get_manager(config)
    if action == "rebuild":
        count = mgr.sqlite.rebuild_fts_index()
        console.print(f"[green]✓ FTS5 index rebuilt: {count} rows indexed[/green]")
    else:
        console.print(f"[red]Unknown action: {action}. Use 'rebuild'.[/red]")
        raise typer.Exit(1)
    mgr.close()


@app.command(name="processor")
def processor_cmd(
    action: str = typer.Argument(..., help="Action: status|run|start|stop"),
    config: str = ConfigOption,
) -> None:
    """Background processor management."""
    mgr = get_manager(config)
    if action == "status":
        s = mgr.stats()
        proc = s.get("processor", {})
        console.print(f"  queue_depth: {proc.get('queue_depth', 'N/A')}")
        console.print(f"  last_processed_at: {proc.get('last_processed_at', 'N/A')}")
        console.print(f"  running: {mgr.processor_running}")
    elif action == "run":
        result = mgr.run_pipeline()
        console.print(f"  clusters: {result['clusters']}")
        console.print(f"  synthesized: {result['synthesized']}")
        console.print(f"  published: {result['published']}")
        console.print(f"  failed_quality_gate: {result['failed_quality_gate']}")
    elif action == "start":
        mgr.start_background_processor()
        console.print("[green]✓ Background processor started[/green]")
    elif action == "stop":
        mgr.stop_background_processor()
        console.print("[green]✓ Background processor stopped[/green]")
    else:
        console.print(f"[red]Unknown action: {action}. Use: status|run|start|stop[/red]")
        raise typer.Exit(1)
    mgr.close()


# ── reindex ───────────────────────────────────────────────────────────────────


@app.command(name="reindex")
def reindex_cmd(
    batch_size: int = typer.Option(100, "--batch-size", "-b", help="Batch size for embedding"),
    config: str = ConfigOption,
) -> None:
    """Rebuild the vector index for all published memories.

    Re-embeds every published memory and upserts into the vector store.
    Use after enabling embeddings or switching embedding models.
    """
    mgr = get_manager(config)
    result = mgr.rebuild_vector_index(batch_size=batch_size)
    console.print(f"  [cyan]total: {result['total']}[/cyan]")
    console.print(f"  [green]indexed: {result['indexed']}[/green]")
    console.print(f"  [red]failed: {result['failed']}[/red]")
    mgr.close()


# ── search v2 maintenance (issue #313) ──────────────────────────────────────


@app.command(name="backfill-embedding-ids")
def backfill_embedding_ids_cmd(
    apply: bool = typer.Option(
        False,
        "--apply",
        help=(
            "Write the stamps (default is a dry run that only reports the "
            "counts: an operator reviews and then re-runs with --apply)."
        ),
    ),
    config: str = ConfigOption,
) -> None:
    """Stamp memories.embedding_id from the vector store (search v2, issue #313).

    Pre-v2 writes never set the column (live DB: NULL for every row) —
    the vector leg still RESOLVED by memory id and worked; the column is
    diagnostics. Idempotent; new writes are stamped automatically from
    search v2 on. Dry run by default: run without --apply first.
    """
    mgr = get_manager(config)
    result = mgr.backfill_embedding_ids(dry_run=not apply)
    if not apply:
        console.print("  [cyan]dry run (no writes)[/cyan] — re-run with --apply to stamp")
    console.print(f"  [cyan]vectors in store: {result['vectors_total']}[/cyan]")
    console.print(f"  [yellow]missing embedding_id: {result['missing']}[/yellow]")
    console.print(f"  [green]stamped: {result['stamped']}[/green]")
    console.print(f"  already set (skipped): {result['skipped_already_set']}")
    mgr.close()


# ── edge-stats maintenance (ADR-0030 A0, review #338 N2) ────────────────────


@app.command(name="edge-stats")
def edge_stats_cmd(
    action: str = typer.Argument("stats", help="Action: stats | purge"),
    keep_last: int = typer.Option(
        None,
        "--keep-last",
        "-k",
        help=(
            "Purge retention target: the NEWEST N edge_stats rows survive, "
            "everything older is dropped. Required for 'purge' — there is no "
            "default retention by design (an operator states it explicitly)."
        ),
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help=(
            "Execute the purge (default is a dry run that only reports what "
            "would be dropped: review it, then re-run with --apply)."
        ),
    ),
    config: str = ConfigOption,
) -> None:
    """edge_stats (used/rejected feedback capture) maintenance.

    The table is append-only (I5): UPDATE and DELETE abort at the DB
    level, and capture volume is bounded per principal AND globally
    (EDGE_STATS_EVENTS_PER_PRINCIPAL_CAP / EDGE_STATS_TOTAL_ROWS_CAP —
    over-cap events are dropped, never an error). 'purge' is the ONE
    operator path that may shrink it: it drops the OLDEST rows past the
    --keep-last retention inside a single maintenance transaction
    (trigger dropped and recreated atomically, the purge stamped into
    the meta audit trail). There is NO automatic eviction — once the
    global cap is reached, capture stays dropped until an operator runs
    this. Run 'stats' first; run purge as a dry run first.

    \b
    Examples:
      vesmaro edge-stats stats
      vesmaro edge-stats purge --keep-last 100000          (dry run)
      vesmaro edge-stats purge --keep-last 100000 --apply  (executes)
    """
    mgr = get_manager(config)
    try:
        if action == "stats":
            by_kind = mgr.sqlite.count_edge_stats_by_kind()
            console.print(
                f"  [cyan]rows total: {sum(by_kind.values())}[/cyan] "
                f"(global cap {EDGE_STATS_TOTAL_ROWS_CAP})"
            )
            for k in sorted(by_kind):
                console.print(f"  {k}: {by_kind[k]}")
            console.print(f"  capture flag: {mgr.settings.search.feedback_capture_enabled}")
            stamp = mgr.sqlite.get_meta(EDGE_STATS_LAST_PURGE_META_KEY)
            console.print(f"  last purge: {stamp or 'never'}")
        elif action == "purge":
            if keep_last is None:
                console.print("[red]'purge' requires --keep-last N (no default retention)[/red]")
                raise typer.Exit(1)
            if keep_last < 0:
                console.print("[red]--keep-last must be >= 0[/red]")
                raise typer.Exit(1)
            result = mgr.sqlite.purge_edge_stats_oldest(keep_last=keep_last, dry_run=not apply)
            if result["dry_run"]:
                console.print("  [cyan]dry run (no writes)[/cyan] — re-run with --apply to purge")
            console.print(f"  rows before: {result['rows_before']}")
            color = "green" if apply else "yellow"
            console.print(
                f"  [{color}]{'purged' if apply else 'would purge'}: {result['purged']}[/{color}]"
            )
            console.print(f"  rows after: {result['rows_after']}")
        else:
            console.print("[red]Unknown action: {action}. Use 'stats' or 'purge'.[/red]")
            raise typer.Exit(1)
    finally:
        mgr.close()


# ── filter (M10) ───────────────────────────────────────────────────────────────


@app.command(name="filter")
def filter_cmd(
    memory_id: str = typer.Argument(
        None,
        help=(
            "Memory ID to filter. Run context filter on a single memory: "
            "shows clean content + reduction stats. Omit with --all to re-filter every memory."
        ),
    ),
    profile: str = typer.Option(
        None,
        "--profile",
        "-p",
        help="log|terminal|code|docs|web|default (auto-detected if omitted)",
    ),
    budget: int = typer.Option(None, "--budget", "-b", help="Token budget for truncation"),
    all_memories: bool = typer.Option(
        False,
        "--all",
        help=(
            "Re-run context filter on ALL memories. Existing clean_content is "
            "overwritten with fresh filter output. Reports aggregate stats."
        ),
    ),
    config: str = ConfigOption,
) -> None:
    """Run the Context Filter on a memory. Shows clean content + reduction stats.

    Note: re-filtering with a different profile produces different clean_content.
    The filter is idempotent only when the same profile is used.
    """
    mgr = get_manager(config)

    if all_memories:
        with console.status("[bold green]Re-filtering all memories..."):
            summary = mgr.filter_all(profile=profile, budget=budget)
        console.print(f"[green]✓[/green] Filtered: {summary['filtered']}")
        console.print(f"  total:   {summary['total']}")
        console.print(f"  failed:  {summary['failed']}")
        console.print(f"  skipped: {summary['skipped']}")
        return

    if not memory_id:
        console.print("[red]Provide a memory ID or use --all.[/red]")
        raise typer.Exit(1)

    result = mgr.apply_context_filter(memory_id, profile=profile, budget=budget)
    if result.get("status") == "error":
        console.print(f"[red]✗[/red] {result.get('error', 'unknown error')}")
        raise typer.Exit(1)

    console.print(f"[green]✓[/green] Filtered: {memory_id}")
    console.print(f"  profile: {result['filter_profile']}")
    stats_data = result.get("stats", {})
    if "dedup" in stats_data:
        console.print(f"  dedup:   {stats_data['dedup']}")
    if "tokens" in stats_data:
        console.print(f"  tokens:  {stats_data['tokens']}")
    console.print(f"  clean_content:\n{result['clean_content']}")


# ── serve ──────────────────────────────────────────────────────────────────────


@app.command()
def serve(
    host: str = typer.Option(None, "--host"),
    port: int = typer.Option(None, "--port"),
    log_file: Annotated[
        Path | None,
        typer.Option("--log-file", help="Override config log file path (enables file logging)."),
    ] = None,
    config: str = ConfigOption,
) -> None:
    """Start the Mnemos HTTP API server (+ the MnemosCore mesh gRPC server when mesh is enabled)."""
    import os

    import uvicorn

    settings = load_settings(config)
    if log_file is not None:
        settings.logging.log_file = log_file
        settings.resolve_paths()
    setup_logging(settings, verbose=_verbose)
    # Issue #445 — one INFO line when a newer release exists. Synchronous
    # by design: cache-first (24h sidecar), 3s HTTP cap, never raises.
    from vesmaro.updates import log_update_if_available

    log_update_if_available(settings)
    h = host or settings.api.host
    p = port or settings.api.port
    # Zero-config profile notice (ADR-0017 Phase 0, D6): when no config file
    # exists anywhere on the search path, say so once at startup so the
    # operator knows which defaults are in effect and where data lives.
    if find_config_file(config) is None:
        console.print(
            "[cyan]zero-config[/cyan] — no config file found, using built-in safe defaults"
        )
        console.print(
            f"  bind: http://{h}:{p} (loopback only; non-loopback binds require auth + TOTP + TLS)"
        )
        console.print(f"  data: {settings.mnemos.data_dir}  vault: {settings.mnemos.vault_path}")
    # Propagate effective bind to the app process so the startup guard and
    # AuthMiddleware see the real host/port (CLI overrides must reach
    # load_settings() inside the worker - finding auth-1).
    os.environ["VESMARO_API__HOST"] = h
    os.environ["VESMARO_API__PORT"] = str(p)

    # Native mesh serve wiring (W2, ROADMAP-v2 go-live): when the mesh is
    # enabled, serve the MnemosCore gRPC server on the configured Unix
    # socket in THIS process, next to the HTTP API — the mnemos-mesh Go
    # binary dials that socket. Mirrors the reference wiring in
    # mnemos-mesh/test/integration/serve-with-mesh.py. Additive: with
    # ``mesh.enabled: false`` (the default) the command behaves exactly
    # as before (uvicorn only).
    mesh_server = None
    if settings.mesh.enabled:
        from vesmaro.api.main import get_manager as get_api_manager
        from vesmaro.mesh_server import MeshServer

        # Seed the api.main singleton from serve's own --config so the
        # MeshServer shares the manager with the in-process HTTP app
        # (uvicorn workers=1) instead of building a second one.
        mesh_manager = get_api_manager(config)
        mesh_server = MeshServer(settings.mesh.socket_path, mesh_manager, settings)
        mesh_server.start()  # logs: mesh server listening on <path>

    # S2 phase 2: the meta poller starts in the FastAPI lifespan, which
    # is PER WORKER — with uvicorn workers > 1 every worker polls. The
    # upsert is idempotent (LWW) so this is wasted fetches, never
    # corruption, but the operator should know.
    if settings.federation.meta_poll.enabled and settings.runtime.uvicorn_workers > 1:
        console.print(
            "[yellow]warning[/yellow] federation.meta_poll enabled with "
            f"runtime.uvicorn_workers={settings.runtime.uvicorn_workers}: "
            "every worker process runs its own poller (idempotent imports, "
            "redundant fetches)"
        )

    try:
        uvicorn.run(
            "vesmaro.api.main:app",
            host=h,
            port=p,
            workers=settings.runtime.uvicorn_workers,
        )
    finally:
        # Graceful shutdown with uvicorn: uvicorn traps SIGINT/SIGTERM,
        # drains the HTTP side, and run() returns — then the gRPC server
        # drains (2s grace, matching the reference wiring) and removes
        # its socket file. Also covers uvicorn startup failures.
        if mesh_server is not None:
            mesh_server.stop(grace=2.0)


# ── fetch (S2 lazy fetch) ─────────────────────────────────────────────────────


@app.command(name="fetch")
def fetch_cmd(
    record_ids: Annotated[
        list[str] | None,
        typer.Option(
            "--id",
            help="Federation record id (fed:<agent>:<uuid>); repeat for multiple records.",
        ),
    ] = None,
    yes: Annotated[
        bool,
        typer.Option(
            "--yes",
            help="Skip the interactive confirmation (required when stdin is not a TTY).",
        ),
    ] = False,
    config: str = ConfigOption,
) -> None:
    """Fetch full records from an origin peer (S2 lazy fetch, explicit).

    The index mirror carries metadata only; this command resolves each
    --id to its origin peer (federation_index), shows the plan, asks
    for an interactive y/N confirmation (or --yes), pulls the compact
    records through the mesh CLI, and imports them IN-PROCESS through
    the same path WriteMemory uses (ACL, duplicate gate, moderation,
    secrets scanner). Requires federation.fetch.mesh_config_path in the
    config. Exit code is 1 when any record failed to fetch/import
    (errors); policy refusals (gated) exit 0 with a warning.
    """
    import sys

    from vesmaro.lazy_fetch import (
        FetchResolutionError,
        run_fetch,
    )

    settings = load_settings(config)
    setup_logging(settings, verbose=_verbose)
    fed = settings.federation
    if not record_ids:
        console.print("[red]✗[/red] no --id given — name at least one federation record id")
        raise typer.Exit(1)
    if not fed.fetch.mesh_config_path.strip():
        console.print(
            "[red]✗[/red] federation.fetch.mesh_config_path is not set — "
            "the mesh CLI needs the path to the peer-leg mesh yaml"
        )
        raise typer.Exit(1)

    mgr = get_manager(config)

    try:
        stats = run_fetch(
            mgr.sqlite,
            mgr,
            settings,
            record_ids,
            assume_yes=yes,
            stdin=sys.stdin,
            stdout=sys.stdout,
            is_tty=sys.stdin.isatty,
        )
    except FetchResolutionError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(1) from exc

    if stats.aborted:
        # A declined/impossible confirmation is an abort, exit non-zero
        # (same contract as mnemos-mesh pull — a script must never
        # mistake an aborted fetch for a done one).
        raise typer.Exit(1)
    if (
        not stats.fetched
        and not stats.imported
        and not stats.errors
        and not stats.not_found
        and not stats.gated
    ):
        # "Nothing to fetch" is only honest when every id resolved to a
        # local skip. not_found>0 means the peer LOST (or gates) records
        # the plan asked for — the operator must see the summary, not a
        # green skip message (review F1).
        console.print(
            "[green]✓[/green] nothing to fetch — every id resolved to a skip "
            "(already local / tombstoned at origin)"
        )
        return

    console.print(
        f"[cyan]summary[/cyan] fetched={stats.fetched} imported={stats.imported} "
        f"duplicates={stats.duplicates} gated={stats.gated} "
        f"not_found={stats.not_found} skipped={stats.skipped} errors={stats.errors}"
    )
    if stats.gated:
        console.print(
            f"[yellow]⚠[/yellow] {stats.gated} record(s) rejected by the import gate "
            "(ACL/moderation) — fetch otherwise succeeded"
        )
    if stats.errors:
        raise typer.Exit(1)


# ── meta-poll (S2 phase 2) ────────────────────────────────────────────────────


@app.command(name="meta-poll")
def meta_poll(
    peer: Annotated[
        str | None,
        typer.Option("--peer", help="Poll only this peer id (default: all poll targets)."),
    ] = None,
    config: str = ConfigOption,
) -> None:
    """Run one federation meta-poll pass now (S2 phase 2, manual/diagnostic).

    The same path the background loop runs per tick — the mesh CLI
    (``mnemos-mesh sync-meta``) per configured peer, pages imported via
    the gated upsert, watermark persisted — executed once, in the
    foreground, with a per-peer summary. Metadata-only: touches
    ``federation_index`` and the poll state, never ``memories``.

    Works regardless of ``federation.meta_poll.enabled`` (an explicit
    run is an operator decision); requires
    ``federation.meta_poll.mesh_config_path`` in the config.
    """
    import asyncio

    from vesmaro.meta_poller import MetaPoller, PeerPollResult

    settings = load_settings(config)
    setup_logging(settings, verbose=_verbose)
    fed = settings.federation
    if not fed.meta_poll.mesh_config_path.strip():
        console.print(
            "[red]✗[/red] federation.meta_poll.mesh_config_path is not set — "
            "the mesh CLI needs the path to the peer-leg mesh yaml"
        )
        raise typer.Exit(1)

    mgr = get_manager(config)
    poller = MetaPoller(mgr.sqlite, fed)
    if peer is not None:
        targets = poller.peer_ids()
        if peer not in targets:
            console.print(
                f"[red]✗[/red] peer {peer!r} is not a meta_poll target "
                f"(configured: {targets or 'none'})"
            )
            raise typer.Exit(1)
        target_ids = [peer]
    else:
        target_ids = poller.peer_ids()
        if not target_ids:
            console.print("[yellow]⚠[/yellow] no federation peers configured — nothing to poll")
            raise typer.Exit(1)

    async def _run() -> list[PeerPollResult]:
        return [await poller.poll_once(p) for p in target_ids]

    results = asyncio.run(_run())
    total_fetched = total_accepted = total_rejected = total_stale = 0
    failed = 0
    for r in results:
        if r.ok:
            console.print(
                f"[green]✓[/green] peer={r.peer_id} fetched={r.fetched} "
                f"accepted={r.accepted} rejected_by_gate={r.rejected_by_gate} "
                f"stale={r.stale} pages={r.pages} latest_rev={r.latest_rev}"
            )
        else:
            failed += 1
            console.print(
                f"[red]✗[/red] peer={r.peer_id} error={r.error} "
                f"(fetched={r.fetched} pages={r.pages} latest_rev={r.latest_rev})"
            )
        total_fetched += r.fetched
        total_accepted += r.accepted
        total_rejected += r.rejected_by_gate
        total_stale += r.stale
    console.print(
        f"[cyan]summary[/cyan] peers={len(results)} failed={failed} "
        f"fetched={total_fetched} accepted={total_accepted} "
        f"rejected_by_gate={total_rejected} stale={total_stale}"
    )
    if failed:
        raise typer.Exit(1)


# ── mcp-server ─────────────────────────────────────────────────────────────────


@app.command(name="mcp-server")
def mcp_server_cmd(config: str = ConfigOption) -> None:
    """Start the MCP server (stdio transport)."""
    import asyncio

    from vesmaro.mcp_server import main as mcp_main

    settings = load_settings(config)
    setup_logging(settings, verbose=_verbose)
    # Issue #445 — one INFO line when a newer release exists (cache-first,
    # 3s cap, never raises; runs before the stdio loop starts).
    from vesmaro.updates import log_update_if_available

    log_update_if_available(settings)
    asyncio.run(mcp_main())


# ── migrate (M13) ──────────────────────────────────────────────────────────────
# Subcommand tree:
#   mnemos migrate from-ai-brain   — migrate ai-brain data to Vesma format
#   mnemos migrate tags            — migrate gcw: tags → mnemos: tags

_migrate_app = typer.Typer(
    name="migrate", help="Migrate data from other memory systems.", no_args_is_help=True
)
app.add_typer(_migrate_app, name="migrate")

_DEFAULT_AI_BRAIN_SOURCE = Path("~/.ai-brain").expanduser()
_DEFAULT_BRAIN_VAULT = Path("~/brain-vault").expanduser()


@_migrate_app.command(name="from-ai-brain")
def migrate(
    source: Annotated[
        Path, typer.Option("--source", help="ai-brain data dir")
    ] = _DEFAULT_AI_BRAIN_SOURCE,
    vault: Annotated[
        Path, typer.Option("--vault", help="ai-brain vault dir")
    ] = _DEFAULT_BRAIN_VAULT,
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be migrated"),
    config: str = ConfigOption,
) -> None:
    """Migrate existing ai-brain data to Vesma format. (M13)"""
    from vesmaro.cli.migrate import migrate_from_ai_brain

    settings = load_settings(config)
    db_path = source / "ai_brain.db"
    vault_path = vault if vault.exists() else None

    with console.status("[bold green]Migrating ai-brain → Vesma..."):
        summary = migrate_from_ai_brain(
            db_path,
            vault_path,
            dry_run=dry_run,
            settings=settings,
        )

    console.print(f"[green]✓[/green] Memories migrated: {summary['memories_migrated']}")
    if vault_path:
        console.print(f"[green]✓[/green] Vault files migrated: {summary['vault_files_migrated']}")
    if summary["errors"]:
        console.print(f"[yellow]⚠[/yellow] Errors: {len(summary['errors'])}")
    if dry_run:
        console.print("[cyan]Dry run — no changes written.[/cyan]")


@_migrate_app.command(name="tags")
def migrate_tags(
    config: str = ConfigOption,
) -> None:
    """Migrate legacy gcw: tags to mnemos: tags in the database.

    .. deprecated::
        Use ``mnemos tags rename --from gcw: --to mnemos: --no-dry-run``
        instead. This command now delegates to the safe ``tags_rename``
        path (plain UPDATE via ``update_fields``) so the FTS5 index stays
        consistent. The old raw-``sqlite3`` implementation in
        ``cli.migrate.migrate_gcw_to_mnemos_tags`` is no longer called.
    """
    console.print(
        "[yellow]⚠ migrate tags is deprecated — use "
        "`mnemos tags rename --from gcw: --to mnemos: --no-dry-run` instead.[/yellow]"
    )

    mgr = get_manager(config)
    settings = mgr.settings
    settings.resolve_paths()
    settings.apply_runtime_env()
    db_path = settings.db_path

    if not db_path.exists():
        console.print(f"[red]✗[/red] Database not found: {db_path}")
        raise typer.Exit(1)

    with console.status("[bold green]Migrating gcw: → mnemos: tags (safe path)..."):
        report = mgr.tags_rename(
            from_prefix="gcw:",
            to_prefix="mnemos:",
            dry_run=False,
            invalid_subtypes_to_legacy=True,
        )

    console.print(f"[green]✓[/green] Memories renamed: {report['renamed']}")
    console.print(f"[green]✓[/green] Skipped (invalid): {report['skipped_invalid']}")
    if report["errors"]:
        console.print(f"[yellow]⚠[/yellow] Errors: {len(report['errors'])}")
    if report["renamed"] == 0 and not report["errors"]:
        console.print("[cyan]No gcw: tags found — nothing to migrate.[/cyan]")


# ── auth (T-AUTH, ADR-0014) ───────────────────────────────────────────────────
# Subcommand tree:
#   mnemos auth token create [--name <label>] [--expires <iso8601>]
#   mnemos auth token list
#   mnemos auth token revoke <token_id>
#   mnemos auth totp enroll  --token-id <id>
#   mnemos auth totp disable --token-id <id>
#   mnemos auth totp test    --token-id <id> --code <123456>

_auth_app = typer.Typer(
    name="auth", help="Manage API auth tokens and TOTP 2FA.", no_args_is_help=True
)
_token_app = typer.Typer(name="token", help="Manage bearer tokens.", no_args_is_help=True)
_totp_app = typer.Typer(name="totp", help="Manage TOTP 2FA enrollment.", no_args_is_help=True)

app.add_typer(_auth_app, name="auth")
_auth_app.add_typer(_token_app, name="token")
_auth_app.add_typer(_totp_app, name="totp")


def _auth_store(config: str | None = None) -> AuthStore:
    from vesmaro.api.auth_store import AuthStore  # lazy: avoids circular deps

    settings = load_settings(config)
    settings.resolve_paths()
    settings.mnemos.data_dir.mkdir(parents=True, exist_ok=True)
    return AuthStore(settings.db_path)


# importing here to satisfy mypy (used in type annotation above)


@_token_app.command("create")
def token_create(
    name: str = typer.Option(None, "--name", "-n", help="Human-readable label"),
    expires: str = typer.Option(None, "--expires", "-e", help="ISO-8601 expiry, e.g. 2027-01-01"),
    no_totp: bool = typer.Option(
        False,
        "--no-totp",
        help="Create a token that can be used directly as a bearer without "
        "the login/verify/session flow (sets totp_required=false). By default "
        "tokens require TOTP.",
    ),
    config: str = ConfigOption,
) -> None:
    """Mint a new bearer token and print it ONCE."""
    store = _auth_store(config)
    try:
        # Normalize expires_at to offset-aware ISO-8601 (UTC).
        # Accepts naive dates like "2027-12-31" and converts to "2027-12-31T00:00:00+00:00".
        normalized_expires: str | None = None
        if expires:
            try:
                from datetime import UTC, datetime

                dt = datetime.fromisoformat(expires)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                normalized_expires = dt.isoformat()
            except ValueError:
                console.print(f"[red]Invalid expiry format: {expires}[/red]")
                console.print(
                    "[dim]Expected ISO-8601, e.g. 2027-01-01 or 2027-01-01T00:00:00[/dim]"
                )
                raise typer.Exit(1) from None
        token_id, plaintext = store.create_token(
            name=name, expires_at=normalized_expires, totp_required=not no_totp
        )
    finally:
        store.close()
    console.print("[green]✓[/green] Token created:")
    console.print(f"  token_id : [bold]{token_id}[/bold]")
    console.print(f"  bearer   : [bold yellow]{plaintext}[/bold yellow]")
    console.print("[red]Store this token now — it will not be shown again.[/red]")


@_token_app.command("list")
def token_list(config: str = ConfigOption) -> None:
    """List all tokens (IDs and metadata — no secrets)."""
    store = _auth_store(config)
    try:
        tokens = store.list_tokens()
    finally:
        store.close()
    if not tokens:
        console.print("[yellow]No tokens found.[/yellow]")
        return
    table = Table("token_id", "name", "created_at", "expires_at", "disabled_at", "totp_required")
    for t in tokens:
        totp_req = t.get("totp_required", 1)
        totp_str = "no" if totp_req is not None and int(str(totp_req)) == 0 else "yes"
        table.add_row(
            str(t.get("token_id", "")),
            str(t.get("name") or ""),
            str(t.get("created_at", "")),
            str(t.get("expires_at") or ""),
            str(t.get("disabled_at") or ""),
            totp_str,
        )
    console.print(table)


@_token_app.command("revoke")
def token_revoke(
    token_id: str = typer.Argument(..., help="token_id to permanently revoke"),
    config: str = ConfigOption,
) -> None:
    """Permanently revoke a token."""
    store = _auth_store(config)
    try:
        ok = store.revoke_token(token_id)
    finally:
        store.close()
    if ok:
        console.print(f"[green]✓[/green] Token {token_id} revoked.")
    else:
        console.print(f"[red]Token {token_id} not found.[/red]")
        raise typer.Exit(1)


@_totp_app.command("enroll")
def totp_enroll(
    token_id: str = typer.Option(..., "--token-id", help="token_id to enroll TOTP for"),
    config: str = ConfigOption,
) -> None:
    """Generate a TOTP secret and print the provisioning URI + optional QR code."""
    import pyotp

    from vesmaro.api.auth import encrypt_totp_secret

    settings = load_settings(config)
    master_key = settings.api.totp_master_key.get_secret_value()
    if not master_key:
        console.print(
            "[red]VESMARO_API__TOTP_MASTER_KEY is not set — cannot encrypt TOTP secret.[/red]"
        )
        raise typer.Exit(1)

    totp_secret = pyotp.random_base32(32)
    totp = pyotp.TOTP(totp_secret)
    uri = totp.provisioning_uri(name="operator", issuer_name="mnemos")

    store = _auth_store(config)
    try:
        row = store.get_token_by_id(token_id)
        if row is None:
            console.print(f"[red]Token {token_id!r} not found.[/red]")
            raise typer.Exit(1)
        encrypted = encrypt_totp_secret(totp_secret, master_key)
        store.set_totp_secret(token_id, encrypted)
    finally:
        store.close()

    console.print(f"[green]✓[/green] TOTP enrolled for {token_id}")
    console.print(f"  otpauth URI: [bold]{uri}[/bold]")
    try:
        import qrcode

        qr = qrcode.QRCode()
        qr.add_data(uri)
        qr.make(fit=True)
        qr.print_ascii()
    except ImportError:
        console.print("  (install qrcode[pil] for ASCII QR display)")


@_totp_app.command("disable")
def totp_disable(
    token_id: str = typer.Option(..., "--token-id", help="token_id to disable TOTP for"),
    config: str = ConfigOption,
) -> None:
    """Remove the TOTP secret from a token (disables 2FA for that token)."""
    store = _auth_store(config)
    try:
        row = store.get_token_by_id(token_id)
        if row is None:
            console.print(f"[red]Token {token_id!r} not found.[/red]")
            raise typer.Exit(1)
        store.clear_totp_secret(token_id)
    finally:
        store.close()
    console.print(f"[green]✓[/green] TOTP disabled for {token_id}.")


@_totp_app.command("test")
def totp_test(
    token_id: str = typer.Option(..., "--token-id", help="token_id to test TOTP for"),
    code: str = typer.Option(..., "--code", help="6-digit TOTP code to verify"),
    config: str = ConfigOption,
) -> None:
    """Verify a TOTP code against the enrolled secret (smoke-test for the operator)."""
    import pyotp

    from vesmaro.api.auth import decrypt_totp_secret

    settings = load_settings(config)
    master_key = settings.api.totp_master_key.get_secret_value()
    if not master_key:
        console.print("[red]VESMARO_API__TOTP_MASTER_KEY is not set.[/red]")
        raise typer.Exit(1)

    store = _auth_store(config)
    try:
        row = store.get_token_by_id(token_id)
        if row is None:
            console.print(f"[red]Token {token_id!r} not found.[/red]")
            raise typer.Exit(1)
        encrypted_blob = row.get("totp_secret_encrypted")
        if not isinstance(encrypted_blob, bytes):
            console.print(f"[red]TOTP not enrolled for {token_id}.[/red]")
            raise typer.Exit(1)
        totp_secret = decrypt_totp_secret(encrypted_blob, master_key)
    finally:
        store.close()

    if totp_secret is None:
        console.print("[red]Failed to decrypt TOTP secret — check master key.[/red]")
        raise typer.Exit(1)

    totp = pyotp.TOTP(totp_secret)
    if totp.verify(code, valid_window=1):
        console.print("[green]✓[/green] Code is valid.")
    else:
        console.print("[red]✗[/red] Code is invalid or expired.")
        raise typer.Exit(1)


# ── integration (integration layer) ────────────────────────────────────────────
# Subcommand tree:
#   vesma integration detect     — print detected harnesses + deploy paths
#   vesma integration setup      — deploy files + register MCP (unified entry point)
#   vesma integration update     — bring stale files to current version
#   vesma integration verify     — compare deployed files against shipped pack
#   vesma integration uninstall  — remove only stamped files

from vesmaro.cli.util import integration_app  # noqa: E402

app.add_typer(integration_app, name="integration")


# ── memory (memory-switch status surface, ADR-0034 MS-0) ──────────────────────
# Subcommand tree:
#   vesma memory status — read-only per-harness memory attachment report

from vesmaro.cli.memory_status import memory_app  # noqa: E402

app.add_typer(memory_app, name="memory")


# ── completion ─────────────────────────────────────────────────────────────────
# Auto-detect current shell, generate completion script, auto-install into rc.

from vesmaro.cli.completion import completion_app  # noqa: E402

app.add_typer(completion_app, name="completion")


# ── doctor ─────────────────────────────────────────────────────────────────────
# Health-check: config + data dir + vault + SQLite + vectors + MCP + integration + tags.

from vesmaro.cli.doctor import doctor_app  # noqa: E402

app.add_typer(doctor_app, name="doctor")


# ── export / import / logs (M17 — backup/restore + trace viewer) ──────────────

from vesmaro.cli.agent_token_cmd import agent_token_app  # noqa: E402
from vesmaro.cli.export_cmd import export_app  # noqa: E402
from vesmaro.cli.import_cmd import import_app  # noqa: E402
from vesmaro.cli.logs import logs_app  # noqa: E402
from vesmaro.cli.scanner_cmd import scanner_app  # noqa: E402
from vesmaro.cli.sync_cmd import sync_app  # noqa: E402
from vesmaro.cli.update_cmd import update as update_cmd  # noqa: E402

app.add_typer(agent_token_app, name="agent-token")
app.add_typer(export_app, name="export")
app.add_typer(import_app, name="import")
app.add_typer(logs_app, name="logs")
app.add_typer(sync_app, name="sync")
app.add_typer(scanner_app, name="scanner")

# ── graph (project-graph registration lifecycle, #450/#454) ───────────────────
# Subcommand tree:
#   vesma graph register <project> <root>   — register a root (agent twin of
#                                             mnemos_register_project, #454)
#   vesma graph repoint <project> <root>    — re-point a ghost registration
#                                             whose root moved on disk (#450)

from vesmaro.cli.graph_cmd import graph_app  # noqa: E402

app.add_typer(graph_app, name="graph")
app.command(name="update", help="Check for updates / update the user-site install (issue #445).")(
    update_cmd
)


def cli_main() -> None:
    """Console-script entry: time the whole CLI invocation as one verb.

    Vitals boundary #10 (A2). Best-effort on every step — the recorder
    needs Settings (default resolution; a --config run still records
    against the default data dir or skips), and a broken plane never
    changes the command's exit code.
    """
    import sys as _sys
    import time as _time

    from vesmaro.config import load_settings as _load

    t0 = _time.monotonic()
    status, code = "ok", 0
    try:
        app()
    except SystemExit as exc:  # typer exits non-zero on failures
        code = int(exc.code or 0)
        status = "ok" if code == 0 else "error"
        raise
    except Exception:
        status = "error"
        raise
    finally:
        try:
            argv = _sys.argv
            cfg = None
            if "--config" in argv:
                i = argv.index("--config")
                if i + 1 < len(argv):
                    cfg = argv[i + 1]
            settings = _load(cfg)
            verb = f"cli:{argv[1]}" if len(argv) > 1 else "cli"
            from vesmaro.metrics.boundary import record_verb_standalone

            record_verb_standalone(
                settings,
                surface="cli",
                verb=verb,
                status=status,
                latency_ms=(_time.monotonic() - t0) * 1000,
                meta={"retry": code} if code else None,
            )
        except Exception:
            pass
