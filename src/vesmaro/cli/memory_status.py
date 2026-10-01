"""``vesma memory status`` — read-only memory-attachment status (ADR-0034, MS-0).

One read-only surface that answers «what is the memory situation on this
harness?» per detected target:

* **Pack attachment** — the vesma integration pack state (stamps): current /
  stale / missing counts, from :meth:`IntegrationManager.verify`.
* **MCP registration** — whether the vesma MCP server entry is present in the
  harness's config, plus the OTHER (external) memory engines seen there.
* **Built-in vesma store** — marker files of the local store: data dir, vault
  dir, SQLite DB — existence and mtime ONLY.
* **Active precedence** — the target's memory-switch mode (default
  ``overlay+mirror``; ADR-0034).

Hygiene contract (ArchCom B3, owner directive 2026-10-01):

* Only paths from the STATIC registry are ever read — ``targets.yaml`` detect
  / ``mcp.config`` paths, the pack deploy maps, and the well-known store
  marker locations below. Nothing is discovered dynamically, nothing is
  fetched (no network).
* File NAMES and mtimes are reported — NEVER file contents, env values or
  command lines. MCP configs are read to enumerate server KEYS only.
* The command is read-only: no writes anywhere, exit code is always 0 when
  the report itself was produced.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from vesmaro import __version__
from vesmaro.cli.integration import (
    MCP_LEGACY_SERVER_KEY,
    MCP_SERVER_KEY,
    IntegrationManager,
    load_targets,
)

console = Console()

memory_app = typer.Typer(
    name="memory",
    help="Memory-switch status surface (ADR-0034).",
    no_args_is_help=True,
)

#: Well-known legacy VS Code MCP config (where ``mcp-setup.sh`` registers the
#: server for targets without their own ``mcp.config``). Keys only are read.
VSCODE_MCP_CONFIG = Path("~/.config/Code/User/mcp.json")

#: Built-in store markers (existence + mtime only, never contents). Same
#: defaults as the zero-config Settings and the MCP env defaults.
STORE_MARKERS: tuple[str, ...] = (
    ".mnemos/data",
    ".mnemos/vault",
    ".mnemos/data/mnemos.db",
)


def _manager(home: Path | None = None) -> IntegrationManager:
    """Build an IntegrationManager bound to the current package version."""
    return IntegrationManager(version=__version__, home=home)


def _server_keys(cfg_path: Path | None, home: Path | None) -> dict[str, Any]:
    """Read the server KEYS from a harness MCP config (never values).

    Returns ``{"vesma": bool, "external": [names...]}``. A missing or
    unreadable config is an honest "no config" — never an error (status is
    read-only and must not fail on a half-set-up machine).
    """
    if cfg_path is None:
        cfg_path = VSCODE_MCP_CONFIG
    resolved = cfg_path
    if home is not None and str(cfg_path).startswith("~"):
        resolved = home / str(cfg_path)[1:].lstrip("/")
    if not resolved.is_file():
        return {"vesma": False, "external": [], "config": None}
    try:
        if resolved.suffix == ".toml":
            # Codex config (~/.codex/config.toml) — TOML, server names sit
            # under [mcp_servers.<name>]. KEYS only, as with JSON configs.
            import tomllib

            data: Any = tomllib.loads(resolved.read_text(encoding="utf-8"))
        else:
            data = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        # JSONDecodeError and TOMLDecodeError are both ValueError subclasses.
        return {"vesma": False, "external": [], "config": str(resolved)}
    if not isinstance(data, dict):
        return {"vesma": False, "external": [], "config": str(resolved)}

    mcp = data.get("mcp")
    servers: Any = None
    if isinstance(mcp, dict):
        servers = mcp.get("servers") if isinstance(mcp.get("servers"), dict) else mcp
    if not isinstance(servers, dict):
        servers = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else None
    if not isinstance(servers, dict):
        servers = data.get("mcp_servers") if isinstance(data.get("mcp_servers"), dict) else None
    if not isinstance(servers, dict):
        return {"vesma": False, "external": [], "config": str(resolved)}

    ours = MCP_SERVER_KEY in servers or MCP_LEGACY_SERVER_KEY in servers
    external = sorted(str(k) for k in servers if k not in (MCP_SERVER_KEY, MCP_LEGACY_SERVER_KEY))
    return {"vesma": ours, "external": external, "config": str(resolved)}


def _store_markers(home: Path) -> list[tuple[str, bool, str]]:
    """Existence + mtime of the built-in store markers (names, never contents)."""
    rows: list[tuple[str, bool, str]] = []
    for marker in STORE_MARKERS:
        path = home / marker
        if path.exists():
            mtime = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
            rows.append((marker, True, mtime))
        else:
            rows.append((marker, False, "—"))
    return rows


def _format_markers(rows: list[tuple[str, bool, str]]) -> str:
    """Render marker rows compactly: ``data ✓ 10:01 · vault ✗ · db ✓ 10:02``."""
    parts: list[str] = []
    for name, exists, when in rows:
        short = name.rsplit("/", 1)[-1]
        icon = "[green]✓[/green]" if exists else "[dim]✗[/dim]"
        parts.append(f"{short} {icon} {when}" if exists else f"{short} {icon}")
    return " · ".join(parts)


@memory_app.command(name="status")
def memory_status_cmd(
    target: Annotated[
        list[str] | None,
        typer.Option(
            "--target",
            "-t",
            help="Narrow to specific harness(es); repeatable. Default: all detected.",
        ),
    ] = None,
    home: Annotated[
        Path | None,
        typer.Option(
            "--home",
            help="Inspect an alternate home directory (cross-environment installs).",
        ),
    ] = None,
) -> None:
    """Show the memory-attachment status per detected harness (read-only).

    Reports, per harness: the vesma pack attachment (stamps), the MCP
    registration (server KEYS only — the vesma entry and any external
    memory engines), the built-in store markers (existence + mtime, never
    contents) and the active memory-switch precedence (ADR-0034).
    """
    cfg = load_targets(home=home)

    if target:
        wanted: list[str] = []
        for name in target:
            if cfg.get(name) is None:
                console.print(f"[red]Unknown target: {name}[/red]")
                console.print(f"  Available: {', '.join(t.name for t in cfg.targets)}")
                raise typer.Exit(1)
            if name not in wanted:
                wanted.append(name)
        detected = tuple(t for t in cfg.targets if t.name in wanted and t.is_detected())
    else:
        detected = cfg.detected()

    if not detected:
        console.print("[yellow]No agent harnesses detected — nothing to report.[/yellow]")
        return

    mgr = _manager(home=home)
    effective_home = home if home is not None else Path.home()

    table = Table(title="Memory status (read-only — names/mtimes only, never contents)")
    table.add_column("Harness", style="bold cyan")
    table.add_column("Pack")
    table.add_column("MCP")
    table.add_column("External engines")
    table.add_column("Vesma store")
    table.add_column("Precedence")

    for tgt in detected:
        try:
            verify = mgr.verify(tgt.name)
            if verify.missing_count and not any(f.status.value == "current" for f in verify.files):
                pack = "[red]not deployed[/red]"
            elif verify.stale_count or verify.missing_count:
                pack = (
                    f"[yellow]stale {verify.stale_count} · missing {verify.missing_count}[/yellow]"
                )
            else:
                pack = f"[green]attached ({len(verify.files)} files)[/green]"
        except Exception as exc:  # status never dies on one harness
            pack = f"[red]error: {exc}[/red]"

        keys = _server_keys(tgt.mcp_config, home)
        mcp = "[green]vesma ✓[/green]" if keys["vesma"] else "[dim]vesma ✗[/dim]"
        external = ", ".join(keys["external"]) if keys["external"] else "[dim]—[/dim]"
        markers = _format_markers(_store_markers(effective_home))

        table.add_row(tgt.name, pack, mcp, external, markers, tgt.precedence)

    console.print(table)
    console.print(
        "[dim]External engines are MCP server keys seen in the harness config; "
        "store markers are existence/mtime only. Precedence default "
        "overlay+mirror (ADR-0034); replace/off arrive with MS-1.[/dim]"
    )
