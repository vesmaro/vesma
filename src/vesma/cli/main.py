"""Vesma CLI — Typer-based command interface.

Entry point: vesma (declared in pyproject.toml [project.scripts]).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from vesma.cli._manager import get_manager, load_settings_or_exit
from vesma.cli.migrate_store_cmd import migrate_store
from vesma.config import find_config_file
from vesma.fs_hardening import ensure_private_dir
from vesma.logging_setup import setup_logging
from vesma.models import (
    AgentRecallQuery,
    Memory,
    MemoryCreate,
    MemorySource,
    MemoryType,
    normalize_tag_aliases,
)
from vesma.storage.sqlite_store import (
    EDGE_STATS_LAST_PURGE_META_KEY,
    EDGE_STATS_TOTAL_ROWS_CAP,
)

# ``get_manager`` lives in the leaf module ``_manager`` so that CLI
# subcommand modules (export_cmd, import_cmd) can import it without forming
# a circular import with this module (which imports them to register the
# Typer sub-apps). Re-exported here for backward compatibility.

if TYPE_CHECKING:
    from vesma.api.auth_store import AuthStore
    from vesma.manager import MemoryManager


app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="vesma",
    help="Vesma — standalone memory & knowledge server for AI agents.",
    no_args_is_help=True,
    # UX-4: the hint under the Commands table (rendered as the epilog).
    epilog="Tip: add -h to any command for help.",
    # typer's generated completion scripts carry no candidate descriptions in
    # any shell (verified for click 8.5's shell_complete path — typer drops the
    # help values). `vesma completion` installs the custom engine-backed
    # scripts instead, and owns --install-completion below so the builtin flag
    # can never install the description-less variants.
    add_completion=False,
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
    ``VESMA_UPDATES_CHECK`` env kill switch) via ``check_for_update``.
    """
    try:
        from vesma.updates import check_for_update

        info = check_for_update()
    except Exception:
        return
    if info is not None and info.update_available:
        print(f"update available: {info.latest} (run 'vesma update --check')", file=sys.stderr)


def _version_callback(value: bool) -> None:
    if value:
        from vesma import __version__

        console.print(f"vesma {__version__}")
        _print_update_hint()
        raise typer.Exit()


def _install_completion_callback(value: bool) -> None:
    """`--install-completion` — install the custom completion for $SHELL.

    Delegates to the exact `vesma completion` path (auto-detect + install),
    so the flag can never fall back to typer's description-less generated
    scripts (disabled via ``add_completion=False`` on the root app).
    """
    if not value:
        return
    from vesma.cli.completion import _detect_shell, _install

    target = _detect_shell()
    if target is None:
        console.print(
            "[red]Could not auto-detect your shell from $SHELL.[/red]\n"
            "Pass an explicit shell: [bold]vesma completion bash|zsh|fish[/bold]"
        )
        raise typer.Exit(1)
    if not _install(target):
        raise typer.Exit(1)
    raise typer.Exit(0)


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
    install_completion: Annotated[
        bool,
        typer.Option(
            "--install-completion",
            help="Install shell completion for the current shell ($SHELL) — tab "
            "completion with command/option descriptions where the shell can "
            "render them. Same as `vesma completion`.",
            callback=_install_completion_callback,
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


def _parse_project_agent(tag_list: list[str]) -> tuple[str, str]:
    """Extract the ``project:`` / ``agent:`` slugs from a ``--tags`` comma list."""
    project = next((t[len("project:") :] for t in tag_list if t.startswith("project:")), "")
    agent = next((t[len("agent:") :] for t in tag_list if t.startswith("agent:")), "")
    return project, agent


def _save_url_ingest(mgr: MemoryManager, url: str, tag_list: list[str]) -> Memory:
    """Shared URL-ingest save path (W3): fetch, extract, save as a memory.

    Used by the canonical ``vesma ingest url`` and by the deprecated
    ``add --url`` alias — one implementation, two surfaces.
    """
    project, agent = _parse_project_agent(tag_list)
    with console.status("Fetching URL..."):
        return mgr.ingest_url(url, tags=tag_list, project=project, agent=agent)


def _save_file_ingest(
    mgr: MemoryManager, *, text: str, title: str | None, tag_list: list[str], source: MemorySource
) -> Memory:
    """Shared file-ingest save path (W3).

    Used by the canonical ``vesma ingest file`` and by the deprecated
    ``add --file`` alias — one implementation, two surfaces. The caller
    reads the file (keeping each surface's historical read semantics).
    """
    data = MemoryCreate(content=text, title=title, tags=tag_list, source=source)
    project, agent = _parse_project_agent(tag_list)
    return mgr.add(data, project=project, agent=agent)


def _print_saved(memory: Memory) -> None:
    console.print(f"[green]✓[/green] Saved: {memory.auto_title()} ({memory.id})")


def _validate_cli_tags(tag_list: list[str], *, strict: bool) -> list[str]:
    """Single CLI tag-contract gate — the mirror of the MCP surface.

    cli-audit 2026-10-08 (P1 #7): ``add`` used to save contract-violating
    rows silently while the MCP tool refuses them. Every CLI save surface
    validates here — ``add`` and, since the cascade fix 2026-10-09 (P2),
    ``ingest file`` / ``ingest url`` too (they used to save silently while
    their own ``--dry-run`` refused): strict mode refuses with a clean
    typed error (same contract the MCP ``vesma_add`` enforces), lax mode
    applies the same auto-patching the MCP surface gets and returns the
    patched list.
    """
    from vesma.models import TagContractError, validate_tag_contract

    try:
        return validate_tag_contract(tag_list, strict=strict)
    except TagContractError as exc:
        console.print(f"[red]✗ Tag contract violation:[/red] {exc}")
        console.print(
            "[dim]Pass --tags with at least one project:<slug>, one agent:<slug> and "
            "one vesma:<subtype> tag — e.g. "
            "--tags 'project:myproj,agent:user,vesma:note'.[/dim]"
        )
        raise typer.Exit(1) from None


def _dry_run_filter_preview(text: str, tag_list: list[str], config: str | None) -> None:
    """Validate the tag contract and print context-filter stats without saving.

    Shared by ``add --dry-run`` (content / stdin / file text) and
    ``ingest file --dry-run`` (W3).
    """
    from vesma.filter.pipeline import apply_filter

    settings = load_settings_or_exit(config)
    # cli-audit 2026-10-08 (P1 #7): a violation here used to escape as an
    # unhandled TagContractError traceback; now it is the same clean typed
    # error the save path prints.
    tag_list = _validate_cli_tags(tag_list, strict=settings.vesma.strict_tag_contract)

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


@app.command()
def add(
    content: str = typer.Argument(None, help="Text content to remember"),
    title: str = typer.Option(None, "--title", "-t"),
    tags: str = typer.Option(
        "",
        "--tags",
        "-T",
        help=(
            "Comma-separated tags. `vesma:` is the canonical storage prefix "
            "(subtypes like vesma:decision); the legacy `mnemos:` spelling is "
            "accepted as an input alias everywhere."
        ),
    ),
    file: Annotated[
        Path | None,
        typer.Option(
            "--file",
            "-f",
            help="Deprecated flag form — use: `vesma ingest file PATH`.",
            hidden=True,
        ),
    ] = None,
    url: str = typer.Option(
        None,
        "--url",
        "-u",
        help="Deprecated flag form — use: `vesma ingest url URL`.",
        hidden=True,
    ),
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
    """Add a new memory entry (quick-capture).

    URL and file ingest are subcommands now (CLI-architecture rework W3):
    `vesma ingest url URL` / `vesma ingest file PATH`. The old flag forms
    (--url / --file) still work — hidden deprecated aliases with identical
    behavior and a stderr hint; removal is not before 6.0.

    With ``--dry-run``: validates the tag contract, runs the context filter
    pipeline on the content, and prints filter stats **without saving**.
    Useful for previewing how the M10 Context Filter will transform input
    before committing it to the store.
    """
    # Input boundary: legacy mnemos:* spellings normalize to the canonical
    # vesma:* form before anything downstream sees the tag list.
    tag_list = (
        normalize_tag_aliases([t.strip() for t in tags.split(",") if t.strip()]) if tags else []
    )

    if url:
        _deprecated_flag_hint("vesma add --url URL", "vesma ingest url URL")
    if file:
        _deprecated_flag_hint("vesma add --file PATH", "vesma ingest file PATH")

    # cli-audit 2026-10-08 (P1 #7): enforce the tag contract BEFORE anything
    # saves or previews — the CLI must not silently save a row the MCP
    # surface refuses (strict default), and in lax mode it patches exactly
    # like the MCP surface.
    tag_list = _validate_cli_tags(
        tag_list, strict=load_settings_or_exit(config).vesma.strict_tag_contract
    )

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
        _dry_run_filter_preview(text, tag_list, config)
        return

    mgr = get_manager(config)

    if url:
        memory = _save_url_ingest(mgr, url, tag_list)
    elif file:
        memory = _save_file_ingest(
            mgr, text=Path(file).read_text(), title=title, tag_list=tag_list, source=source
        )
    elif content:
        data = MemoryCreate(
            content=content, title=title, tags=tag_list, source=source, memory_type=memory_type
        )
        project, agent = _parse_project_agent(tag_list)
        memory = mgr.add(data, project=project, agent=agent)
    else:
        stdin_text = sys.stdin.read().strip()
        if not stdin_text:
            console.print("[red]No content provided.[/red]")
            raise typer.Exit(1)
        data = MemoryCreate(content=stdin_text, title=title, tags=tag_list, source=source)
        memory = mgr.add(data)

    _print_saved(memory)


# ── ingest (CLI-architecture rework W3) ───────────────────────────────────────
# Standing design rule (docs/project/cli-architecture-rework.md §2.5): a
# subcommand names the function (WHAT), a flag only configures it (HOW).
# URL and file ingest moved here from `add --url/--file`; the flag forms
# stay on `add` as hidden deprecated aliases (identical behavior + stderr
# hint; soft mode — removal not before 6.0, design doc §3).

_ingest_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="ingest",
    help=(
        "Ingest external content into the memory store.\n\n"
        "Subcommands: `url` — fetch a web page, extract the main text, save "
        "it as a memory; `file` — save a local file's text content as a "
        "memory. (Former `add --url` / `add --file` flag forms; those remain "
        "as hidden deprecated aliases.)"
    ),
    no_args_is_help=True,
)
app.add_typer(_ingest_app, name="ingest")


@_ingest_app.command(name="url")
def ingest_url(
    url: str = typer.Argument(..., help="URL to fetch, extract, and save as a memory."),
    tags: str = typer.Option("", "--tags", "-T", help="Comma-separated tags"),
    config: str = ConfigOption,
) -> None:
    """Ingest a web page: fetch it, extract the main text, save as a memory.

    Fetches the URL, extracts the readable main text (boilerplate stripped),
    and stores it as a memory. Tags are required per the tag contract — pass
    at least one `--tags` entry, typically including a `project:` slug.
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    # Input boundary + cascade fix 2026-10-09 (P2): normalize legacy
    # mnemos:* spellings, then the SAME tag-contract gate as `add` —
    # ingest used to save contract-violating rows silently while its own
    # --dry-run refused them. Validated BEFORE the fetch (no network work
    # for a call that cannot be saved).
    tag_list = normalize_tag_aliases(tag_list)
    tag_list = _validate_cli_tags(
        tag_list, strict=load_settings_or_exit(config).vesma.strict_tag_contract
    )
    mgr = get_manager(config)
    memory = _save_url_ingest(mgr, url, tag_list)
    _print_saved(memory)


@_ingest_app.command(name="file")
def ingest_file(
    path: Annotated[Path, typer.Argument(help="File whose text content is saved as a memory.")],
    title: str = typer.Option(None, "--title", "-t"),
    tags: str = typer.Option("", "--tags", "-T", help="Comma-separated tags"),
    source: Annotated[MemorySource, typer.Option("--source", "-s")] = MemorySource.CLI,
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
    """Ingest a local file: save its text content as a memory.

    Saves the file's text content as a memory with an optional `--title`,
    `--source` and comma-separated `--tags`. `--dry-run` shows the
    context-filter stats (profile, token reduction, dedup, noise) WITHOUT
    saving — a preview of what the content would look like in context.
    A missing or binary file is a clean error, not a traceback.
    """
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    # Input boundary + cascade fix 2026-10-09 (P2): normalize legacy
    # mnemos:* spellings, then the SAME tag-contract gate as `add` —
    # ingest used to save contract-violating rows silently while its own
    # --dry-run refused them (the add/add-dry-run asymmetry, mirrored).
    tag_list = normalize_tag_aliases(tag_list)
    tag_list = _validate_cli_tags(
        tag_list, strict=load_settings_or_exit(config).vesma.strict_tag_contract
    )
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError is a ValueError, not an OSError: without it a
        # binary file would traceback on the canonical surface.
        detail = (
            exc.strerror
            if isinstance(exc, OSError)
            else "binary file — not ingested (content is not valid UTF-8 text)"
        )
        console.print(f"[red]Cannot read {path}: {detail}[/red]")
        raise typer.Exit(1) from exc
    if dry_run:
        _dry_run_filter_preview(text, tag_list, config)
        return
    mgr = get_manager(config)
    memory = _save_file_ingest(mgr, text=text, title=title, tag_list=tag_list, source=source)
    _print_saved(memory)


# ── search ─────────────────────────────────────────────────────────────────────

#: Built-in relevance floor for the CLI `search` (cli-audit 2026-10-08 P1
#: #9), calibrated against the BUNDLED nano embedder (measured 2026-10-09):
#: a garbage query's raw cosine lands ≈ 0.47-0.63 (the model is
#: anisotropic (everything correlates) correlates), a genuinely related text ≈ 0.88.
#: 0.70 cuts the garbage band with margin while keeping related hits.
#: Override per call with --threshold, per machine with
#: ``search.min_relevance`` in the config (a pure threshold — 0 keeps this
#: built-in floor); the gate itself switches off machine-wide via
#: ``search.cli_relevance_gate: false``. --threshold 0 disables the gate
#: for the call.
#: (Other embedders have different scales — hashing fixtures are
#: orthogonal at 0.0 — hence the knob, not a hardcoded universal.)
_DEFAULT_SEARCH_RELEVANCE = 0.70


@app.command()
def search(
    query: str = typer.Argument(..., help="Search query"),
    limit: int = typer.Option(10, "--limit", "-l", help="Max results"),
    project: str = typer.Option(None, "--project", "-p", help="Filter by project slug"),
    tags: str = typer.Option(
        None,
        "--tags",
        "-T",
        help=(
            "Comma-separated tags to filter by. `vesma:` is the canonical storage "
            "prefix; the legacy `mnemos:` spelling is accepted as an input alias."
        ),
    ),
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
    threshold: Annotated[
        float | None,
        typer.Option(
            "--threshold",
            help=(
                "Minimum semantic (vector-leg cosine) relevance for a result with "
                "no lexical match. Default: search.min_relevance from the config "
                "when set (>0), else a calibrated floor for the bundled embedder. "
                "Machine-wide off: search.cli_relevance_gate: false in the config. "
                "0 disables the gate for this call (pure ranking, garbage queries "
                "return the whole store)."
            ),
        ),
    ] = None,
    config: str = ConfigOption,
) -> None:
    """Search long-term memory (hybrid FTS + vector).

    By default the CLI surfaces every status except archived — unlike the
    MCP/HTTP surfaces (published-only by default), the interactive CLI must
    complete the first add → search roundtrip from a clean install with no
    running server and no pipeline pass yet (ADR-0017 Phase 0).

    A relevance gate drops results that carry no lexical (FTS) match and
    whose raw semantic similarity is below the threshold — a garbage query
    says "no relevant results" instead of returning the whole store with
    near-zero scores.
    """
    from vesma.models import MemoryStatus

    mgr = get_manager(config)
    # Input boundary: normalize legacy mnemos:* spellings so an old-style
    # filter matches the canonical vesma:* tags (exact-match filter).
    tag_list = (
        normalize_tag_aliases([t.strip() for t in tags.split(",") if t.strip()]) if tags else None
    )
    status_enum = MemoryStatus(status) if status else None
    results = mgr.search(
        query=query,
        tags=tag_list,
        project=project,
        limit=limit,
        include_raw=include_raw,
        status=status_enum,
    )

    # cli-audit 2026-10-08 (P1 #9): the fused RRF score is rank-based —
    # even a garbage query surfaces the whole store at ≈0.008 and "no
    # results" is unreachable. Gate on the raw vector cosine: a row with
    # NO lexical (FTS) match must clear the semantic floor to surface.
    # Cascade fix 2026-10-09 (P2): min_relevance is a pure threshold —
    # 0 is indistinguishable from unset, so machine-wide OFF is the
    # explicit ``search.cli_relevance_gate: false`` key (the old "0
    # disables the gate" contract silently re-armed the built-in floor).
    effective_threshold = threshold
    if effective_threshold is None and mgr.settings.search.cli_relevance_gate:
        effective_threshold = (
            mgr.settings.search.min_relevance
            if mgr.settings.search.min_relevance > 0
            else _DEFAULT_SEARCH_RELEVANCE
        )
    if effective_threshold is not None and effective_threshold > 0 and results:
        relevant = [
            r for r in results if r.vector_score is None or r.vector_score >= effective_threshold
        ]
        dropped = len(results) - len(relevant)
        if not relevant and results:
            best = max(r.vector_score for r in results if r.vector_score is not None)
            console.print(
                f"[yellow]No relevant results[/yellow] — best semantic score "
                f"{best:.3f} is below the relevance threshold "
                f"{effective_threshold:.2f} "
                "(lower it with --threshold, or pass 0 to disable the gate)."
            )
            return
        results = relevant
        # Cascade fix 2026-10-09 (P3): a PARTIAL drop used to render as a
        # mysteriously short table — say what the gate hid and how to see it.
        if dropped > 0:
            console.print(
                f"[dim]{dropped} low-relevance result(s) hidden — raise the floor "
                "or pass --threshold 0 to see everything.[/dim]"
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
# CLI-architecture rework W2 (docs/project/cli-architecture-rework.md §2.4):
# standing design rule — a subcommand names the function (WHAT), a flag only
# configures it (HOW). `recall` is a group whose callback keeps the bare
# `vesma recall` behavior byte-identical; per-agent recall moved to the
# canonical subcommand `vesma recall agent SLUG [QUERY]`; the legacy `--agent`
# flag stays as a hidden deprecated alias with a stderr hint (identical
# behavior; removal not before 6.0, design doc §3).


def _deprecated_flag_hint(old_form: str, new_form: str) -> None:
    """One-line deprecation hint for a hidden legacy flag form (stderr).

    Per-module copy of the W-C precedent (update_cmd.py, doctor.py):
    each CLI module that retracts a flag form owns its hint helper.
    stdout stays clean for pipes and JSON consumers.
    """
    typer.echo(f"[deprecated] `{old_form}` is deprecated — use: {new_form}", err=True)


def _print_recall_memories(memories: list[Memory]) -> None:
    """Shared printer for the recall group (bare form and agent form alike)."""
    if not memories:
        console.print("[yellow]No memories found.[/yellow]")
        return
    for m in memories:
        console.print(f"[cyan]{m.auto_title()}[/cyan]  ({m.id[:8]}…)")
        console.print(f"  tags: {', '.join(m.tags)}")
        console.print()


_recall_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="recall",
    help=(
        "Recall recent memories, optionally scoped to an agent or a project.\n\n"
        "Bare `vesma recall` prints the most recent memories (default 10 — "
        "`--limit` to change), filtered by `--project`. Per-agent recall is a "
        "subcommand: `vesma recall agent SLUG [QUERY]`."
    ),
    invoke_without_command=True,
)
app.add_typer(_recall_app, name="recall")


@_recall_app.callback(invoke_without_command=True)
def recall(
    ctx: typer.Context,
    project: str = typer.Option(
        None, "--project", "-p", help="Filter by project slug (e.g. `project:vesma`)."
    ),
    agent: str | None = typer.Option(
        None,
        "--agent",
        "-a",
        help="Deprecated flag form — use: `vesma recall agent SLUG`.",
        hidden=True,
    ),
    limit: int = typer.Option(10, "--limit", "-l", help="Max memories to print."),
    config: str = ConfigOption,
) -> None:
    """Recall recent memories, optionally filtered by project.

    Per-agent recall (M3) is a subcommand now: `vesma recall agent SLUG
    [QUERY]`. The old flag form (--agent) still works — a hidden
    deprecated alias with identical behavior and a stderr hint; removal
    is not before 6.0.
    """
    if ctx.invoked_subcommand is not None:
        # The subcommand (`agent`) runs its own logic; options placed
        # BEFORE the subcommand word would be silently dropped otherwise.
        if project is not None or agent is not None or limit != 10 or config is not None:
            typer.echo(
                "note: options placed before the subcommand are ignored — "
                "pass them after it (e.g. `vesma recall agent SLUG --project x`)",
                err=True,
            )
        return
    mgr = get_manager(config)
    if agent:
        _deprecated_flag_hint("vesma recall --agent SLUG", "vesma recall agent SLUG")
        results = mgr.agent_recall(AgentRecallQuery(agent=agent, project=project, limit=limit))
        _print_recall_memories([r.memory for r in results])
        return
    # cli-audit 2026-10-08 (P1 #5): bare recall used to delegate to
    # recall_context — checkpoint-scoped by contract — so only
    # vesma:checkpoint rows ever surfaced while the help promised "the
    # most recent memories". recall_recent is the unscoped listing the
    # help describes; recall_context (checkpoints) keeps its MCP/REST
    # semantics untouched.
    _print_recall_memories(mgr.recall_recent(project=project or "", limit=limit))


@_recall_app.command(name="agent")
def recall_agent(
    agent: str = typer.Argument(..., help="Agent slug to recall for."),
    query: str | None = typer.Argument(
        None,
        help="Optional query — hybrid search scoped to the agent's entries "
        "(without it: the N most recent entries for the agent).",
    ),
    project: str = typer.Option(
        None, "--project", "-p", help="Filter by project slug (e.g. `project:vesma`)."
    ),
    limit: int = typer.Option(10, "--limit", "-l", help="Max memories to print."),
    config: str = ConfigOption,
) -> None:
    """Per-agent recall (M3): one agent's entries, optionally query-matched.

    Without QUERY: the N most recent entries for the agent (created_at
    desc). With QUERY: hybrid search scoped to the agent's entries
    (raw included). Same data the MCP tool vesma_agent_recall returns.
    """
    mgr = get_manager(config)
    results = mgr.agent_recall(
        AgentRecallQuery(agent=agent, project=project, query=query, limit=limit)
    )
    _print_recall_memories([r.memory for r in results])


# ── tags (M2) ─────────────────────────────────────────────────────────────────
# Subcommand tree:
#   vesma tags validate   — validate the tag contract across the live store

_tags_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="tags",
    help=(
        "Manage and validate memory tags.\n\n"
        "The tag contract (`project:<slug>` / `agent:<slug>` lowercase slugs) "
        "is what project- and agent-scoped recall searches against. These "
        "verbs audit a vault for contract violations and bulk-normalize or "
        "bulk-rename tags when conventions drift."
    ),
    no_args_is_help=True,
)
app.add_typer(_tags_app, name="tags")


@_tags_app.command(name="validate")
def tags_validate(
    vault: Annotated[
        Path | None,
        typer.Argument(
            help="Deprecated positional — accepted for backward compatibility; "
            "the scan always covers the live store from --config."
        ),
    ] = None,
    config: str = ConfigOption,
) -> None:
    """Validate the tag contract across the live store. Exit 1 on violations.

    Every entry needs at least one `project:*`, one `agent:*` and one
    subtype tag (`vesma:*`; legacy `mnemos:*` counts) — the same contract
    the doctor's tag-contract check and `vesma tags audit` enforce. Each
    non-conformant entry is reported with its id, current tags and the
    missing prefixes. Exits 1 when violations exist (CI-friendly), 0 on a
    clean store. Pair with `vesma tags audit --apply` for the bulk heal.
    """

    if vault is not None:
        console.print(
            "[dim]note: the positional vault path is not used — validation runs "
            "against the live store from --config (same scan as `vesma tags audit`)."
            "[/dim]"
        )

    mgr = get_manager(config)

    rows: list[tuple[Any, ...]] = []
    db_path = mgr.sqlite.db_path
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT id, content, title, tags, project FROM memories ORDER BY created_at DESC"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                rows = []  # fresh/empty store — nothing to validate
            else:
                console.print(f"[red]✗ Tag validation failed:[/red] {exc}")
                raise typer.Exit(1) from exc
        except sqlite3.Error as exc:
            console.print(f"[red]✗ Tag validation failed:[/red] {exc}")
            raise typer.Exit(1) from exc
        finally:
            conn.close()

    findings: list[dict[str, Any]] = []
    for mem_id, content, title, raw_tags, project_col in rows:
        _, missing, unparseable = _audit_heal_tags(str(raw_tags or ""), str(project_col or ""))
        if not missing:
            continue
        if unparseable:
            tags_display = "(unparseable)"
        else:
            try:
                tags_display = ", ".join(json.loads(raw_tags)) if raw_tags else ""
            except (json.JSONDecodeError, TypeError):
                tags_display = "(unparseable)"
        snippet = f"{title or content or ''}".strip().replace("\n", " ")
        if len(snippet) > 60:
            snippet = snippet[:57] + "…"
        findings.append(
            {"id": str(mem_id), "snippet": snippet, "tags": tags_display, "missing": missing}
        )

    console.print(
        f"[bold]Validating tag contract:[/bold] {len(rows):,} entries scanned, "
        f"{len(findings):,} non-conformant"
    )
    if not findings:
        console.print("[green]✓ All entries conform to the tag contract.[/green]")
        return

    table = Table(title="Tag contract violations")
    table.add_column("ID", style="cyan")
    table.add_column("Entry", style="white", max_width=60)
    table.add_column("Tags", style="dim")
    table.add_column("Missing", style="red")
    for f in findings:
        table.add_row(f["id"][:8] + "…", f["snippet"], f["tags"], ", ".join(f["missing"]))
    console.print(table)
    console.print("[yellow]Heal with: vesma tags audit --apply[/yellow]")
    raise typer.Exit(1)


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
            help="Target prefix (e.g. 'vesma:'). Must end with ':'",
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
            help="Preview changes without writing to the database.",
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

    Safe replacement for ``vesma migrate tags``. Uses ``update_fields``
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


# ── tags audit (board card vesma-doctor-fixes-467) ───────────────────────────


def _audit_slug(value: str) -> str:
    """Lax-mode slug normalization for audit heal values.

    Matches ``tags normalize`` / ``validate_tag_contract`` lax mode:
    strip, lowercase, spaces → hyphens.
    """
    return value.strip().lower().replace(" ", "-")


def _audit_heal_tags(raw_tags: str, project_col: str) -> tuple[list[str], list[str], bool]:
    """Compute the healed tag list for one memory row.

    The owner-approved heal policy is ADDITIVE-ONLY: existing (parseable)
    tags are never removed; only the missing tag-contract prefixes are
    appended. ``project:*`` heals to the row's ``project`` column value
    slug-normalized, or ``project:unsorted`` when the column is empty;
    ``agent:*`` heals to ``agent:user``; the subtype tag heals to
    ``vesma:legacy``.

    Returns ``(healed_tags, missing_prefixes, unparseable)`` where
    ``missing_prefixes`` is empty exactly when the row already conforms.
    A row whose tags JSON cannot be parsed is non-conformant by
    definition; nothing is salvageable, so the healed list is the three
    contract prefixes.
    """
    unparseable = False
    tags: list[str] | None
    try:
        parsed = json.loads(raw_tags) if raw_tags else []
    except (json.JSONDecodeError, TypeError):
        parsed = None
    if isinstance(parsed, list) and all(isinstance(t, str) for t in parsed):
        tags = list(parsed)
    else:
        tags = []
        # Non-empty raw value that did not parse into a list of strings —
        # corrupt by the contract. An empty/NULL column is just "no tags".
        unparseable = bool((raw_tags or "").strip())

    missing: list[str] = []
    healed = list(tags or [])
    if not any(t.startswith("project:") for t in healed):
        slug = _audit_slug(project_col or "")
        healed.append(f"project:{slug or 'unsorted'}")
        missing.append("project:*")
    if not any(t.startswith("agent:") for t in healed):
        healed.append("agent:user")
        missing.append("agent:*")
    if not any(t.startswith("vesma:") or t.startswith("mnemos:") for t in healed):
        healed.append("vesma:legacy")
        missing.append("vesma:*")
    return healed, missing, unparseable


@_tags_app.command(name="audit")
def tags_audit(
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Heal: add the missing contract prefixes to every non-conformant "
            "entry (additive only — existing tags are never removed; idempotent). "
            "Default is a dry-run report.",
        ),
    ] = False,
    limit: Annotated[
        int,
        typer.Option(
            "--limit",
            "-l",
            help="Cap the number of LISTED rows (0 = list all). The scan itself "
            "always covers the whole store.",
        ),
    ] = 0,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Emit the report as JSON (for scripting / CI)."),
    ] = False,
    config: str = ConfigOption,
) -> None:
    """Audit memories for tag-contract conformance; optionally heal.

    Every entry needs at least one ``project:*``, one ``agent:*`` and one
    subtype tag (``vesma:*`` canon; legacy ``mnemos:*`` counts — the same
    contract the doctor's tag-contract check enforces); entries whose tags
    JSON is unparseable are flagged too.

    Default (dry-run): report only. With ``--apply``: add the missing
    prefixes — never remove existing tags (idempotent by construction).
    """
    mgr = get_manager(config)

    rows: list[tuple[Any, ...]] = []
    db_path = mgr.sqlite.db_path
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        try:
            rows = conn.execute(
                "SELECT id, content, title, tags, project FROM memories ORDER BY created_at DESC"
            ).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                rows = []  # fresh/empty store — nothing to audit
            else:
                console.print(f"[red]✗ Tag scan failed:[/red] {exc}")
                raise typer.Exit(1) from exc
        except sqlite3.Error as exc:
            console.print(f"[red]✗ Tag scan failed:[/red] {exc}")
            raise typer.Exit(1) from exc
        finally:
            conn.close()

    findings: list[dict[str, Any]] = []
    for mem_id, content, title, raw_tags, project_col in rows:
        healed, missing, unparseable = _audit_heal_tags(str(raw_tags or ""), str(project_col or ""))
        if not missing:
            continue
        if unparseable:
            tags_display = "(unparseable)"
        else:
            try:
                tags_display = ", ".join(json.loads(raw_tags)) if raw_tags else ""
            except (json.JSONDecodeError, TypeError):
                tags_display = "(unparseable)"
        snippet = f"{title or content or ''}".strip().replace("\n", " ")
        if len(snippet) > 60:
            snippet = snippet[:57] + "…"
        findings.append(
            {
                "id": str(mem_id),
                "snippet": snippet,
                "tags": tags_display,
                "missing": missing,
                "healed": healed,
            }
        )

    shown = findings if limit <= 0 else findings[:limit]
    tags_added: dict[str, int] = {}
    healed_count = 0
    if apply:
        for f in findings:
            if mgr.sqlite.update_fields(f["id"], tags=f["healed"]):
                healed_count += 1
            for prefix in f["missing"]:
                tags_added[prefix] = tags_added.get(prefix, 0) + 1

    if json_output:
        payload: dict[str, Any] = {
            "mode": "applied" if apply else "dry-run",
            "scanned": len(rows),
            "non_conformant": len(findings),
            "rows": [
                {"id": f["id"], "snippet": f["snippet"], "tags": f["tags"], "missing": f["missing"]}
                for f in shown
            ],
        }
        if apply:
            payload["healed"] = healed_count
            payload["tags_added"] = tags_added
        console.print_json(json.dumps(payload))
        return

    mode_label = "[green]applied[/green]" if apply else "[yellow]dry-run (no writes)[/yellow]"
    title = f"Tags audit — {len(rows):,} scanned, {len(findings):,} non-conformant — {mode_label}"
    table = Table(title=title)
    table.add_column("ID", style="cyan")
    table.add_column("Entry", style="white", max_width=60)
    table.add_column("Tags", style="dim")
    table.add_column("Missing", style="red")
    for f in shown:
        table.add_row(f["id"][:8] + "…", f["snippet"], f["tags"], ", ".join(f["missing"]))
    console.print(table)
    if len(findings) > len(shown):
        console.print(f"[dim]…and {len(findings) - len(shown)} more (raise --limit)[/dim]")

    if apply:
        plural = "y" if healed_count == 1 else "ies"
        console.print(f"[green]Healed:[/green] {healed_count} entr{plural}")
        for prefix in ("project:*", "agent:*", "vesma:*"):
            if prefix in tags_added:
                console.print(f"  {prefix}: {tags_added[prefix]} tag(s) added")
        console.print(
            "[dim]Re-run `vesma tags audit` — a second pass must report 0 non-conformant.[/dim]"
        )
    elif findings:
        console.print("[yellow]Dry-run only — re-run with --apply to heal.[/yellow]")


# ── workflow (#96) ────────────────────────────────────────────────────────────
#
# Thin CLI wrappers over MemoryManager.workflow_set / workflow_get /
# workflow_history. The state machine + 5 guardrails are enforced server-side
# in the manager, so these commands only translate ValueError (guardrail /
# state-machine violation) into a red error line + exit 1 — mirroring how the
# MCP tool and REST layer surface the same violations.

_workflow_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="workflow",
    help=(
        "Manage the workflow lifecycle of a memory "
        "(states: open, in-progress, blocked, resolved, done, withdrawn).\n\n"
        "Transitions are enforced by a server-side state machine with "
        "guardrails (an agent may only take `set` while holding the memory "
        "lock); `get` shows the current status and lock owner, `history` the "
        "transition audit log."
    ),
    no_args_is_help=True,
)
app.add_typer(_workflow_app, name="workflow")


@_workflow_app.command(name="get")
def workflow_get(
    memory_id: Annotated[str, typer.Argument(help="Target memory id.")],
    config: str = ConfigOption,
) -> None:
    """Show the current workflow status + lock owner for a memory.

    Prints the memory's workflow_status, who holds its lock (locked_by) and
    since when — the pre-flight check before `workflow set`: a transition on
    a memory locked by another actor fails unless `--force` with `--reason`
    is used. Exits 1 when the memory id does not exist.
    """

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
    """Transition a memory's workflow status through the server-enforced state machine.

    The target status goes in `--to` (open|in-progress|blocked|resolved|done|
    withdrawn); `--actor` identifies who is acting. Illegal transitions and
    guardrail violations (locked by someone else, terminal state) exit 1 with
    the server's reason. `--force` overrides another actor's lock but REQUIRES
    `--reason` — the override lands in the audit log.
    """

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
    """Show the workflow transition audit log for a memory (newest first).

    Every transition — including forced ones and idempotent replays — is
    recorded with timestamp, from/to status, actor and reason. Use it to
    answer "who moved this memory and why"; `--limit` caps the rows shown
    (default 50).
    """

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
    """Display Vesma health statistics.

    One-line-per-key dump of the manager's health snapshot: memory counts,
    vector-store state, and the knowledge-pipeline block (queue depth, last
    processed timestamp, running flag). The first thing to run when the
    store "feels wrong" — `vesma doctor` adds deeper per-subsystem checks.
    """
    mgr = get_manager(config)
    s = mgr.stats()
    for k, v in s.items():
        console.print(f"  [bold]{k}[/bold]: {v}")


# ── fts / processor / edge-stats sub-apps (CLI-architecture rework W1) ────────
#
# Standing design rule: subcommand = function (WHAT), flag = configuration
# (HOW). The former positional-action commands (`vesma fts rebuild`,
# `vesma processor status|run|start|stop`, `vesma edge-stats stats|purge`)
# are real typer sub-apps now. The restructuring is textually compatible —
# the old positional spellings ARE the new subcommand spellings, so no
# alias cycle is needed (design doc docs/project/cli-architecture-rework.md
# §2.1-2.3, §3.5). Behavioral deltas: an unknown verb is a typer usage
# error (exit 2, was a custom message + exit 1) and the bare
# `vesma edge-stats` shows help instead of running `stats`.

_fts_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="fts",
    help=(
        "FTS5 full-text index maintenance.\n\n"
        "Subcommand: `rebuild` — rebuild the index and report the number of "
        "rows indexed. The former positional form (`vesma fts rebuild`) keeps "
        "the same spelling."
    ),
    no_args_is_help=True,
)
app.add_typer(_fts_app, name="fts")


@_fts_app.command(name="rebuild")
def fts_rebuild(config: str = ConfigOption) -> None:
    """Rebuild the FTS5 index and report the number of rows indexed.

    Drops and repopulates the FTS5 full-text index from the memory table —
    the fix when search results go stale or wrong after a crash, a restore,
    or an FTS-trigger desync. Safe to re-run; reports the row count indexed.
    """
    mgr = get_manager(config)
    try:
        count = mgr.sqlite.rebuild_fts_index()
        console.print(f"[green]✓ FTS5 index rebuilt: {count} rows indexed[/green]")
    finally:
        mgr.close()


_processor_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="processor",
    help=(
        "Background processor (knowledge pipeline) management.\n\n"
        "Subcommands: `status` (queue depth, last processed timestamp, running "
        "flag), `run` (one synchronous pipeline pass: cluster → synthesize → "
        "quality gate → publish), `start` / `stop` (the background loop). The "
        "former positional form (`vesma processor run`) keeps the same spelling."
    ),
    no_args_is_help=True,
)
app.add_typer(_processor_app, name="processor")


@_processor_app.command(name="status")
def processor_status(config: str = ConfigOption) -> None:
    """Queue depth, last processed timestamp, running flag.

    The pipeline's vital signs without touching it: how many raw memories
    wait to be clustered/published, when the pipeline last ran, and whether
    the background loop is alive. Use after `vesma add` to see the entry
    queued, and before `processor run`/`start` to see what is pending.
    """
    mgr = get_manager(config)
    try:
        s = mgr.stats()
        proc = s.get("processor", {})
        console.print(f"  queue_depth: {proc.get('queue_depth', 'N/A')}")
        console.print(f"  last_processed_at: {proc.get('last_processed_at', 'N/A')}")
        console.print(f"  running: {mgr.processor_running}")
    finally:
        mgr.close()


@_processor_app.command(name="run")
def processor_run(config: str = ConfigOption) -> None:
    """Run one synchronous pipeline pass (cluster → synthesize → quality gate → publish).

    Drains the pending queue once in the foreground and prints the per-stage
    counts (clusters formed, entries synthesized, published, and how many
    failed the quality gate). Use it in cron jobs or to debug the pipeline
    without starting the background loop; repeated runs until the queue is
    empty are safe.
    """
    mgr = get_manager(config)
    try:
        result = mgr.run_pipeline()
        console.print(f"  clusters: {result['clusters']}")
        console.print(f"  synthesized: {result['synthesized']}")
        console.print(f"  published: {result['published']}")
        console.print(f"  failed_quality_gate: {result['failed_quality_gate']}")
    finally:
        mgr.close()


@_processor_app.command(name="start")
def processor_start(config: str = ConfigOption) -> None:
    """Start the background processor loop (the daemon for CLI-only deployments).

    Starts the in-process loop that keeps advancing the pipeline (cluster →
    synthesize → publish) without a `processor run` per batch — for setups
    that run the CLI without the HTTP service. In service deployments the
    supervisor owns the loop instead; check liveness with `processor status`.
    """
    mgr = get_manager(config)
    try:
        mgr.start_background_processor()
        console.print("[green]✓ Background processor started[/green]")
    finally:
        mgr.close()


@_processor_app.command(name="stop")
def processor_stop(config: str = ConfigOption) -> None:
    """Stop the background processor loop (a no-op when it is not running).

    Signals the loop to finish cleanly and exit; a pipeline pass in flight
    completes before the loop stops, so nothing is left half-processed.
    Stopping an already-stopped loop succeeds quietly — safe in stop scripts.
    """
    mgr = get_manager(config)
    try:
        # P3 (cli-audit 2026-10-08): stopping an already-stopped loop used
        # to claim "✓ stopped" — distinguish the no-op.
        was_running = mgr.processor_running
        mgr.stop_background_processor()
        if was_running:
            console.print("[green]✓ Background processor stopped[/green]")
        else:
            console.print("[cyan]· Background processor not running (no-op)[/cyan]")
    finally:
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


# ── edge-stats maintenance (ADR-0030 A0, review #338 N2; sub-app since W1) ───


_edge_stats_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="edge-stats",
    help=(
        "edge_stats (used/rejected feedback capture) maintenance (ADR-0030 A0).\n\n"
        "Subcommands: `stats` (row counts vs the global cap, capture flag, last "
        "purge stamp), `purge` (drop the OLDEST rows past an explicit "
        "--keep-last retention; dry run by default). The former positional verb "
        "forms (`vesma edge-stats stats|purge`) keep the same spelling; the "
        "former bare form `vesma edge-stats` (which ran `stats`) now shows this "
        "help — spell the subcommand."
    ),
    no_args_is_help=True,
)
app.add_typer(_edge_stats_app, name="edge-stats")


@_edge_stats_app.command(name="stats")
def edge_stats_stats(config: str = ConfigOption) -> None:
    """Report edge_stats row counts vs the global cap, capture flag, last purge.

    Read-only maintenance view for the ADR-0030 feedback table: rows per
    kind against the global cap, whether feedback capture is currently
    enabled, and when `edge-stats purge` last ran. Pair with `edge-stats
    purge --keep-last N` when the table approaches the cap.
    """
    mgr = get_manager(config)
    try:
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
    finally:
        mgr.close()


@_edge_stats_app.command(name="purge")
def edge_stats_purge(
    keep_last: Annotated[
        int | None,
        typer.Option(
            "--keep-last",
            "-k",
            help=(
                "Purge retention target: the NEWEST N edge_stats rows survive, "
                "everything older is dropped. Required for 'purge' — there is no "
                "default retention by design (an operator states it explicitly)."
            ),
        ),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help=(
                "Execute the purge (default is a dry run that only reports what "
                "would be dropped: review it, then re-run with --apply)."
            ),
        ),
    ] = False,
    config: str = ConfigOption,
) -> None:
    """Drop the OLDEST edge_stats rows past an explicit --keep-last retention.

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
      vesma edge-stats stats
      vesma edge-stats purge --keep-last 100000          (dry run)
      vesma edge-stats purge --keep-last 100000 --apply  (executes)
    """
    mgr = get_manager(config)
    try:
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
    """Start the Vesma HTTP API server (+ the MnemosCore mesh gRPC server when mesh is enabled).

    Serves the REST API on `--host`/`--port` (defaults from config; loopback
    only with built-in zero-config defaults — non-loopback binds require
    auth + TOTP + TLS). With `mesh.enabled` in config, the MnemosCore gRPC
    server joins the same process on its Unix socket. `--log-file` overrides
    the config log path; `--config` selects a non-default config.yaml.
    """
    import os

    import uvicorn

    settings = load_settings_or_exit(config)
    if log_file is not None:
        settings.logging.log_file = log_file
        settings.resolve_paths()
    setup_logging(settings, verbose=_verbose)
    # Issue #445 — one INFO line when a newer release exists. Synchronous
    # by design: cache-first (24h sidecar), 3s HTTP cap, never raises.
    from vesma.updates import log_update_if_available

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
        console.print(f"  data: {settings.vesma.data_dir}  vault: {settings.vesma.vault_path}")
    # Propagate effective bind to the app process so the startup guard and
    # AuthMiddleware see the real host/port (CLI overrides must reach
    # load_settings() inside the worker - finding auth-1). Written under the
    # canonical VESMA_ prefix (6.0.0: the only honoured spelling).
    os.environ["VESMA_API__HOST"] = h
    os.environ["VESMA_API__PORT"] = str(p)

    # Native mesh serve wiring (W2, ROADMAP-v2 go-live): when the mesh is
    # enabled, serve the MnemosCore gRPC server on the configured Unix
    # socket in THIS process, next to the HTTP API — the mnemos-mesh Go
    # binary dials that socket. Mirrors the reference wiring in
    # vesma-mesh/test/integration/serve-with-mesh.py. Additive: with
    # ``mesh.enabled: false`` (the default) the command behaves exactly
    # as before (uvicorn only).
    # Issue #510: the wiring itself lives in the SHARED helper
    # (backend.start_mesh_legs) so `service run` reaches the identical
    # unix+tcp parity without duplicating the manager-singleton seeding.
    mesh_server = None
    if settings.mesh.enabled:
        from vesma.service.backend import start_mesh_legs

        # cli-audit 2026-10-08 (P1 #2): a failed mesh leg (missing gRPC
        # stubs in the install, socket busy, …) used to kill serve BEFORE
        # the HTTP API ever bound — the whole product was down because one
        # optional leg was. `serve` DEGRADES now: a loud warning and a
        # working HTTP API. `service run` keeps its fail-fast contract
        # (a supervised unit must fail loudly, not come up half-alive).
        try:
            mesh_server = start_mesh_legs(settings, config)
        except Exception as exc:
            console.print(
                "[yellow]warning[/yellow] mesh leg failed to start — serving "
                f"HTTP-only (degraded): {exc}"
            )
            console.print(
                "[dim]mesh stays disabled for this process; fix the cause and "
                "restart to re-enable.[/dim]"
            )

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
            "vesma.api.main:app",
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
            from vesma.service.backend import stop_mesh_legs

            stop_mesh_legs(mesh_server, grace=2.0)


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

    settings = load_settings_or_exit(config)
    setup_logging(settings, verbose=_verbose)
    fed = settings.federation
    # cli-audit 2026-10-08 (P1 #3): the mesh modules import lazily, BELOW
    # the argument guards — a wheel without the gRPC stubs used to die
    # with ImportError even on bare `vesma fetch` (where a clean usage
    # error belongs).
    if not record_ids:
        console.print("[red]✗[/red] no --id given — name at least one federation record id")
        console.print("[dim]usage: vesma fetch --id fed:<agent>:<uuid> [--id …] [--yes][/dim]")
        raise typer.Exit(1)
    if not fed.fetch.mesh_config_path.strip():
        console.print(
            "[red]✗[/red] federation.fetch.mesh_config_path is not set — "
            "the mesh CLI needs the path to the peer-leg mesh yaml"
        )
        raise typer.Exit(1)

    mgr = get_manager(config)

    # cli-audit 2026-10-08 (P1 #2/#3): a wheel whose build machine never
    # generated the gRPC stubs reaches here — the operator gets the fix
    # ladder, not a raw traceback. (The import and the run are separate
    # try blocks: except clauses evaluate in order, so one shared block
    # would hit an UnboundLocalError on the exception type's own name.)
    try:
        from vesma import lazy_fetch as _lazy_fetch
    except ImportError as exc:
        console.print(f"[red]✗[/red] mesh federation is unavailable in this install: {exc}")
        raise typer.Exit(1) from exc

    try:
        stats = _lazy_fetch.run_fetch(
            mgr.sqlite,
            mgr,
            settings,
            record_ids,
            assume_yes=yes,
            stdin=sys.stdin,
            stdout=sys.stdout,
            is_tty=sys.stdin.isatty,
        )
    except _lazy_fetch.FetchResolutionError as exc:
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

    from vesma.meta_poller import MetaPoller, PeerPollResult

    settings = load_settings_or_exit(config)
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
    """Start the MCP server (stdio transport).

    The Model Context Protocol surface for AI agents: memory tools over
    stdio, speaking to the same store as the CLI and REST API. Register it
    in the agent's MCP client config as `vesma mcp-server` — no port, no
    bind; the transport is the agent's stdin/stdout.
    """
    import asyncio

    from vesma.mcp_server import main as mcp_main

    settings = load_settings_or_exit(config)
    setup_logging(settings, verbose=_verbose)
    # Issue #445 — one INFO line when a newer release exists (cache-first,
    # 3s cap, never raises; runs before the stdio loop starts).
    from vesma.updates import log_update_if_available

    log_update_if_available(settings)
    asyncio.run(mcp_main())


# ── migrate (M13) ──────────────────────────────────────────────────────────────
# Subcommand tree:
#   vesma migrate from-ai-brain   — migrate ai-brain data to Vesma format
#   vesma migrate tags            — migrate gcw: tags → vesma: tags

_migrate_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="migrate",
    help=(
        "Migrate data from other memory systems.\n\n"
        "One-shot importers for supported sources — currently `from-ai-brain` "
        "(the historical ~/.ai-brain layout) and `tags` (gcw: tag → vesma: "
        "tag renaming). Each importer reports what it converted; source data "
        "is read, never modified."
    ),
    no_args_is_help=True,
)
app.add_typer(_migrate_app, name="migrate")

# ── migrate-store (B3, 6.0.0 #9) ──────────────────────────────────────────────
#   vesma migrate-store --from <5.x home> --to <6.0 home> [--apply]
# Top-level name deliberately distinct from the M13 `vesma migrate` tree
# above (mover contract point 11: the ai-brain path keeps working untouched).
app.command(name="migrate-store")(migrate_store)

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
    """Migrate existing ai-brain data to Vesma format. (M13)

    Reads the ai-brain SQLite database (default `~/.ai-brain`, override with
    `--source`) and optional vault directory (`--vault`), converts every
    memory to the Vesma store, and prints the migrated counts. `--dry-run`
    reports what WOULD migrate without writing anything; the source is only
    ever read.
    """
    from vesma.cli.migrate import migrate_from_ai_brain

    settings = load_settings_or_exit(config)
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
    """Migrate legacy gcw: tags to the canonical vesma:* namespace.

    DEPRECATED: use ``vesma tags rename --from gcw: --to vesma: --no-dry-run``
        instead. This command now delegates to the safe ``tags_rename``
        path (plain UPDATE via ``update_fields``) so the FTS5 index stays
        consistent. The old raw-``sqlite3`` implementation in
        ``cli.migrate.migrate_gcw_to_vesma_tags`` is no longer called.
    """
    console.print(
        "[yellow]⚠ migrate tags is deprecated — use "
        "`vesma tags rename --from gcw: --to vesma: --no-dry-run` instead.[/yellow]"
    )

    mgr = get_manager(config)
    settings = mgr.settings
    settings.resolve_paths()
    settings.apply_runtime_env()
    db_path = settings.db_path

    if not db_path.exists():
        console.print(f"[red]✗[/red] Database not found: {db_path}")
        raise typer.Exit(1)

    with console.status("[bold green]Migrating gcw: → vesma: tags (safe path)..."):
        report = mgr.tags_rename(
            from_prefix="gcw:",
            to_prefix="vesma:",
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
#   vesma auth token create [--name <label>] [--expires <iso8601>]
#   vesma auth token list
#   vesma auth token revoke <token_id>
#   vesma auth totp enroll  --token-id <id>
#   vesma auth totp disable --token-id <id>
#   vesma auth totp test    --token-id <id> --code <123456>

_auth_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="auth",
    help=(
        "Manage API auth tokens and TOTP 2FA.\n\n"
        "Operator-side credential management for non-loopback API binds: "
        "`auth token` mints, lists and revokes bearer tokens; `auth totp` "
        "enrolls, tests and disables the second factor per token."
    ),
    no_args_is_help=True,
)
_token_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="token",
    help=(
        "Manage bearer tokens.\n\n"
        "`create` prints the plaintext token ONCE (store it immediately), "
        "`list` shows metadata without secrets, `revoke` permanently disables "
        "a token. By default new tokens require the TOTP second factor; "
        "`create --no-totp` opts out for service-to-machine use."
    ),
    no_args_is_help=True,
)
_totp_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="totp",
    help=(
        "Manage TOTP 2FA enrollment.\n\n"
        "Enroll a token with `enroll` (prints the provisioning URI for the "
        "authenticator app), smoke-test it with `test`, retire it with "
        "`disable`. Requires VESMA_API__TOTP_MASTER_KEY to encrypt the "
        "secret at rest."
    ),
    no_args_is_help=True,
)

app.add_typer(_auth_app, name="auth")
_auth_app.add_typer(_token_app, name="token")
_auth_app.add_typer(_totp_app, name="totp")


def _auth_store(config: str | None = None) -> AuthStore:
    from vesma.api.auth_store import AuthStore  # lazy: avoids circular deps

    settings = load_settings_or_exit(config)
    settings.resolve_paths()
    ensure_private_dir(settings.vesma.data_dir)
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
    """Mint a new bearer token and print it ONCE.

    Creates the token record and prints the token_id plus the plaintext
    bearer — the only time the secret is ever shown, so store it now.
    `--name` labels the token for `token list` audits, `--expires` takes an
    ISO-8601 stamp (naive dates are normalized to UTC), and `--no-totp`
    creates a token usable directly as a bearer (default: TOTP required).
    """
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
    """List all tokens (IDs and metadata — no secrets).

    One row per token: id, label, created/expires stamps, disabled state and
    whether TOTP is required. Use it for credential audits and to find the
    token_id that `token revoke` or `totp enroll` need.
    """
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
    """Permanently revoke a token.

    Disables the token immediately and permanently — requests bearing it
    fail auth from that moment on. There is no un-revoke: mint a fresh
    token if the credentials are needed again. Exits 1 when the token_id
    is unknown.
    """
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
    """Generate a TOTP secret and print the provisioning URI + optional QR code.

    Enrolls the token for time-based 2FA: a fresh secret is generated,
    encrypted with VESMA_API__TOTP_MASTER_KEY, and stored; the provisioning
    URI goes to your authenticator app. Re-enrolling replaces the previous
    secret. Afterwards verify with `totp test` before relying on it.
    """
    import pyotp

    from vesma.api.auth import encrypt_totp_secret

    settings = load_settings_or_exit(config)
    master_key = settings.api.totp_master_key.get_secret_value()
    if not master_key:
        console.print(
            "[red]VESMA_API__TOTP_MASTER_KEY is not set — cannot encrypt TOTP secret.[/red]"
        )
        raise typer.Exit(1)

    totp_secret = pyotp.random_base32(32)
    totp = pyotp.TOTP(totp_secret)
    uri = totp.provisioning_uri(name="operator", issuer_name="vesma")

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
    """Remove the TOTP secret from a token (disables 2FA for that token).

    Clears the enrolled secret so the token authenticates with the bearer
    alone again — for retiring a lost authenticator or converting the token
    to service use (pair with `token create --no-totp` semantics). The
    token itself stays valid; revoke it separately if it should not be.
    """
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
    """Verify a TOTP code against the enrolled secret (smoke-test for the operator).

    Runs the same verification the API login performs against the current
    code from your authenticator app — the end-to-end check that enrollment
    worked before you depend on it. Pass the token with `--token-id` and the
    6-digit code with `--code`; a mismatch is a clean failure, not a lockout.
    """
    import pyotp

    from vesma.api.auth import decrypt_totp_secret

    settings = load_settings_or_exit(config)
    master_key = settings.api.totp_master_key.get_secret_value()
    if not master_key:
        console.print("[red]VESMA_API__TOTP_MASTER_KEY is not set.[/red]")
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

from vesma.cli.util import integration_app  # noqa: E402

app.add_typer(integration_app, name="integration")


# ── memory (memory-switch status surface, ADR-0034 MS-0) ──────────────────────
# Subcommand tree:
#   vesma memory status — read-only per-harness memory attachment report

from vesma.cli.memory_status import memory_app  # noqa: E402

app.add_typer(memory_app, name="memory")


# ── completion ─────────────────────────────────────────────────────────────────
# Auto-detect current shell, generate completion script, auto-install into rc.

from vesma.cli.completion import completion_app  # noqa: E402

app.add_typer(completion_app, name="completion")

# ── __complete (custom completion engine — hidden plumbing) ────────────────────
# Backing engine for the shell scripts installed by `vesma completion`.
# Hidden from --help; argv contract documented in vesma/cli/complete_cmd.py.

from vesma.cli.complete_cmd import complete as complete_engine  # noqa: E402

app.command(
    name="__complete",
    hidden=True,
    # The engine receives raw words that legitimately start with `-`
    # (option-name completion): they are data, not options of __complete.
    # help_option_names=[] pins that: the global -h/--help sweep (UX-1) must
    # never plant a help option HERE, or `-h<TAB>` / `--help<TAB>` would be
    # intercepted by click instead of reaching the engine as data.
    context_settings={
        "ignore_unknown_options": True,
        "allow_extra_args": True,
        "help_option_names": [],
    },
)(complete_engine)


# ── doctor ─────────────────────────────────────────────────────────────────────
# Health-check: config + data dir + vault + SQLite + vectors + MCP + integration + tags.

from vesma.cli.doctor import doctor_app  # noqa: E402

app.add_typer(doctor_app, name="doctor")


# ── export / import / logs (M17 — backup/restore + trace viewer) ──────────────

from vesma.cli.agent_token_cmd import agent_token_app  # noqa: E402
from vesma.cli.export_cmd import export_app  # noqa: E402
from vesma.cli.import_cmd import import_cmd  # noqa: E402
from vesma.cli.logs import logs_app  # noqa: E402
from vesma.cli.scanner_cmd import scanner_app  # noqa: E402
from vesma.cli.sync_cmd import sync_app  # noqa: E402
from vesma.cli.update_cmd import update_app  # noqa: E402

app.add_typer(agent_token_app, name="agent-token")
app.add_typer(export_app, name="export")
# A PLAIN command, not a sub-app: a group parses options only BEFORE the
# first positional (click MultiCommand), so `import f.json --mode merge`
# died with "Missing argument 'source'" (cli-audit 2026-10-08 #4).
app.command(name="import")(import_cmd)
app.add_typer(logs_app, name="logs")
app.add_typer(sync_app, name="sync")
app.add_typer(scanner_app, name="scanner")

# ── graph (project-graph registration lifecycle, #450/#454) ───────────────────
# Subcommand tree:
#   vesma graph register <project> <root>   — register a root (agent twin of
#                                             vesma_register_project, #454)
#   vesma graph repoint <project> <root>    — re-point a ghost registration
#                                             whose root moved on disk (#450)
#   vesma graph delete <project>            — drop the graph index; a ghost
#                                             (root missing) is removed
#                                             entirely behind --force +
#                                             --confirm-name

from vesma.cli.graph_cmd import graph_app  # noqa: E402

app.add_typer(graph_app, name="graph")

# update family: a sub-app (board card vesma-update-family-components) —
# plain `vesma update` keeps the 5.2.0 report+prompt behavior via the
# group callback; check/apply/timer/components are subcommands and the
# old flags remain hidden deprecated aliases (the shipped systemd unit's
# ExecStart depends on them).
app.add_typer(update_app, name="update")

# ── service (engine waves W3/W4, one sub-app) — install/uninstall (W4)
#    plus the supervisor control plane status/health/start/stop/restart/
#    logs/run (W3, control-socket v1) ──────────────────────────────────

from vesma.cli.service import service_app  # noqa: E402

app.add_typer(service_app, name="service")

# ── awareness (ADR-0035 operator surface, board card vesma-ops-mode-ux) ──
#    get/set — the heartbeat mode switch in the resolved config file (the
#    manual-YAML + blind-restart path stops being the documented way);
#    stats — the wave-0 funnel read off the metrics sidecar without
#    operator SQL.

from vesma.cli.awareness_cmd import awareness_app  # noqa: E402

app.add_typer(awareness_app, name="awareness")


def cli_main() -> None:
    """Console-script entry: time the whole CLI invocation as one verb.

    Vitals boundary #10 (A2). Best-effort on every step — the recorder
    needs Settings (default resolution; a --config run still records
    against the default data dir or skips), and a broken plane never
    changes the command's exit code.
    """
    import sys as _sys
    import time as _time

    from vesma.config import load_settings as _load

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
            from vesma.metrics.boundary import record_verb_standalone

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
