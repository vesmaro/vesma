"""``vesma graph`` — project-graph registration lifecycle (#450/#454).

Operator/agent commands around the PG2 registration table:

* ``vesma graph register <project> <root>`` — register a project root
  without the Python REPL (the agent-facing twin of the
  ``mnemos_register_project`` MCP tool, #454).
* ``vesma graph repoint <project> <new-root>`` — re-point a GHOST
  registration (root moved on disk) at its new location, purging the
  stale index (#450).

Both are thin adapters over :class:`vesma.codegraph.service.
CodeGraphService` — the confinement gates (existing dir, marker-or-
``.git``, not ``$HOME``/fs-root, one-root-one-graph), the PG7
attribution binding and the audit trail live in the service, exactly
like the MCP surface. A service refusal prints its reason and exits 1
(loud, never a traceback).
"""

from __future__ import annotations

from typing import Annotated, Any

import typer
from rich.console import Console

from vesma.cli._manager import get_manager

console = Console()

graph_app = typer.Typer(
    name="graph",
    help="Project-graph registration lifecycle (register / repoint).",
    no_args_is_help=True,
)

#: Default audit actor for CLI invocations (the human at the terminal —
#: PG7 attribution is binding even without a calling agent).
CLI_ACTOR = "cli-operator"

ConfigOption = typer.Option(None, "--config", "-c", help="Path to config.yaml")


def _service(config: str | None) -> Any:
    """Build the graph service over the CLI manager singleton."""
    from vesma.codegraph.service import get_graph_service

    return get_graph_service(get_manager(config))


def _refused(exc: Exception) -> None:
    """Print a service refusal and exit non-zero (loud, no traceback)."""
    console.print(f"[red]refused:[/red] {exc}")
    raise typer.Exit(1) from exc


@graph_app.command(name="register")
def register_cmd(
    project: Annotated[str, typer.Argument(help="Project id or name to register.")],
    root: Annotated[str, typer.Argument(help="Absolute path to the project root on disk.")],
    agent: Annotated[
        str, typer.Option("--agent", help="Audit actor (PG7 attribution).")
    ] = CLI_ACTOR,
    config: str = ConfigOption,
) -> None:
    """Register a project root for the code graph (#454).

    The root must exist, be an absolute path, carry a packaging manifest
    or a .git, and not be $HOME/the filesystem root. Idempotent when the
    same root is already registered (audit manual-register-reused).
    Explicit registration does NOT count against auto_register_max_projects.
    """
    from vesma.codegraph.service import GraphToolError

    try:
        # source="operator" (#464 P2-1): the CLI is the operator's call
        # path — registration here is never gated by
        # code_graph.agent_registration (that gate bounds the MCP agent
        # path only; the service default is source="agent").
        result = _service(config).register_project(project, root, agent=agent, source="operator")
    except GraphToolError as exc:
        _refused(exc)
    console.print(
        f"[green]{result['status']}[/green] project {result['project']!r} "
        f"at {result['root']}" + (f" — {result['note']}" if result.get("note") else "")
    )
    console.print("[dim]next: vesma mcp / mnemos_index_project to build the graph[/dim]")


@graph_app.command(name="repoint")
def repoint_cmd(
    project: Annotated[str, typer.Argument(help="Project id or name (the ghost).")],
    new_root: Annotated[str, typer.Argument(help="Absolute path to the moved root.")],
    agent: Annotated[
        str, typer.Option("--agent", help="Audit actor (PG7 attribution).")
    ] = CLI_ACTOR,
    reason: Annotated[
        str, typer.Option("--reason", help="Audit reason (default: graph-repoint).")
    ] = "graph-repoint",
    config: str = ConfigOption,
) -> None:
    """Re-point a ghost registration at its moved root (#450).

    The old root is gone on disk, so indexing is stuck; this rewrites the
    registration's root, purges the stale index (it describes the OLD
    tree — derived, rebuildable data) and leaves the next index run to
    rebuild fresh. Audited as action 'repoint'.
    """
    from vesma.codegraph.service import GraphToolError

    try:
        result = _service(config).repoint_project(project, new_root, agent=agent, reason=reason)
    except GraphToolError as exc:
        _refused(exc)
    console.print(
        f"[green]{result['status']}[/green] project {result['project']!r} "
        f"at {result['root']}"
        + (
            f" ({result['purged_nodes']} stale nodes purged)"
            if result.get("status") == "repointed"
            else ""
        )
    )
    if result.get("status") == "repointed":
        console.print("[dim]next: index the project to rebuild the graph[/dim]")
