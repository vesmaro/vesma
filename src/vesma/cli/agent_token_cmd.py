"""``vesma agent-token`` CLI — W3-v1 agent token issuance UX (ADR-0018-T §5).

Launch condition (Product Architect amendment 5): without usable issuance,
owners share tokens. Four commands over
:mod:`vesma.agent_tokens`:

* ``issue``  — mint + print a token ONCE (jti registered, plaintext never
  persisted);
* ``rotate`` — new jti; the old token is honoured only for the grace
  window (default 5 min), then fails closed;
* ``revoke`` — instant denylist write by ``--jti`` (one token) or
  ``--agent`` (revoke-all, the compromise response);
* ``list``   — issuance metadata only; no token values exist to leak.

The gateway address / node fingerprint echo promised by ADR-0018-T §5 is
delivered by the mesh side (W3 part 2, gateway listener) — this module
prints the ``aud`` hint the operator needs to correlate.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from rich.console import Console
from rich.table import Table

from vesma.agent_tokens import (
    AgentTokenError,
    AgentTokenStore,
    issue_agent_token,
    load_or_create_signing_key,
    rotate_agent_token,
    signing_key_path,
)
from vesma.cli._manager import load_settings_or_exit
from vesma.config import Settings

console = Console()

agent_token_app = typer.Typer(
    context_settings={"help_option_names": ["-h", "--help"]},
    name="agent-token",
    help=(
        "Manage W3 AgentGateway tokens (issue/rotate/revoke/list).\n\n"
        "Short-lived Ed25519-signed tokens agents use to authenticate to "
        "federation peers: `issue` mints one (plaintext shown once), `rotate` "
        "replaces a live token with a grace window, `revoke` denylists "
        "immediately, `list` shows metadata only."
    ),
    no_args_is_help=True,
)

ConfigOption = Annotated[str | None, typer.Option("--config", "-c", help="Path to config.yaml")]


def _resolve_settings(config: str | None) -> Settings:
    """Load settings, resolve paths, ensure the data dir exists."""
    settings = load_settings_or_exit(config)
    settings.resolve_paths()
    settings.vesma.data_dir.mkdir(parents=True, exist_ok=True)
    return settings


def _signing_key(settings: Settings) -> Ed25519PrivateKey:
    """Open (or mint on first use) the Ed25519 agent-token signing key."""
    path = signing_key_path(
        settings.vesma.data_dir, override=settings.federation.agent_token_key_path
    )
    try:
        return load_or_create_signing_key(Path(path))
    except AgentTokenError as exc:
        console.print(f"[red]Signing key error: {exc}[/red]")
        raise typer.Exit(1) from None


def _fmt_ts(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%d %H:%M:%SZ")


@agent_token_app.command("issue")
def token_issue(
    agent: Annotated[str, typer.Option("--agent", help="Agent (harness) id — the token sub.")],
    node: Annotated[
        str, typer.Option("--node", help="Gateway node id — the token aud (node binding).")
    ],
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="Comma-separated grants: 'read' (default), 'rw', 'project:<slug>' "
            "(repeatable; narrows the token).",
        ),
    ] = "read",
    ttl_hours: Annotated[
        float,
        typer.Option("--ttl-hours", help="Token TTL in hours; max 24 (TM §3), default 8."),
    ] = 8.0,
    config: ConfigOption = None,
) -> None:
    """Mint a new agent token and print it ONCE (plaintext is never stored).

    Signs a token binding `--agent` (the subject) to `--node` (the gateway
    it may call), scoped by comma-separated `--scope` grants (default
    read-only) with a TTL capped at 24 hours (`--ttl-hours`, default 8 —
    TM §3). Only the signed plaintext is printed; the store keeps issuance
    metadata only, so copy it now.
    """
    settings = _resolve_settings(config)
    store = AgentTokenStore(settings.db_path)
    try:
        token, claims = issue_agent_token(
            agent_id=agent,
            node_id=node,
            scope_spec=scope,
            ttl_hours=ttl_hours,
            store=store,
            key=_signing_key(settings),
            issuer_id=settings.federation.agent_token_issuer,
        )
    except AgentTokenError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    finally:
        store.close()
    console.print("[green]✓[/green] Agent token issued:")
    console.print(f"  agent    : [bold]{claims.sub}[/bold]")
    console.print(f"  node/aud : [bold]{claims.aud}[/bold]")
    console.print(f"  scope    : [bold]{', '.join(claims.scope)}[/bold]")
    console.print(f"  expires  : [bold]{_fmt_ts(claims.exp)}[/bold]")
    console.print(f"  jti      : [dim]{claims.jti}[/dim]")
    # soft_wrap: the ~330-char envelope must stay ONE logical line — a
    # hard-wrapped token cannot be copy-pasted (and would break scripted
    # extraction); width-constrained terminals soft-overflow instead.
    console.print(f"  token    : [bold yellow]{token}[/bold yellow]", soft_wrap=True)
    console.print(
        "[red]Store this token now (0600 file, never in env vars or CLI args) — "
        "it will not be shown again.[/red]"
    )


@agent_token_app.command("rotate")
def token_rotate(
    agent: Annotated[str, typer.Option("--agent", help="Agent whose live token rotates.")],
    grace_minutes: Annotated[
        int,
        typer.Option(
            "--grace-minutes",
            help="Grace window the OLD token stays valid (default 5, TM §5).",
        ),
    ] = 5,
    config: ConfigOption = None,
) -> None:
    """Rotate: new jti; the old token dies at the end of the grace window.

    Issues a fresh token for `--agent` and lets the OLD one keep working for
    `--grace-minutes` (default 5, TM §5) so in-flight jobs finish — after
    the window the old jti is denylisted. Prints the new plaintext once;
    the standard key-hygiene operation, run it per agent regularly.
    """
    settings = _resolve_settings(config)
    store = AgentTokenStore(settings.db_path)
    try:
        token, claims, graced = rotate_agent_token(
            agent_id=agent,
            grace_minutes=grace_minutes,
            store=store,
            key=_signing_key(settings),
        )
    except LookupError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    except AgentTokenError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    finally:
        store.close()
    console.print(f"[green]✓[/green] Rotated agent [bold]{agent}[/bold]:")
    console.print(f"  old tokens graced : {graced} (valid until grace window ends)")
    console.print(f"  new expires       : [bold]{_fmt_ts(claims.exp)}[/bold]")
    console.print(f"  new jti           : [dim]{claims.jti}[/dim]")
    console.print(f"  token             : [bold yellow]{token}[/bold yellow]", soft_wrap=True)
    console.print("[red]Store this token now — it will not be shown again.[/red]")


@agent_token_app.command("revoke")
def token_revoke(
    agent: Annotated[
        str | None,
        typer.Option("--agent", help="Revoke ALL live tokens of this agent."),
    ] = None,
    jti: Annotated[
        str | None,
        typer.Option("--jti", help="Revoke a single token by jti."),
    ] = None,
    config: ConfigOption = None,
) -> None:
    """Instant denylist write (TM §5): --jti for one token, --agent for all.

    Emergency kill switch — a revoked jti is rejected on sight, no grace
    window. Exactly one of `--jti <id>` (a single token) or `--agent <id>`
    (every live token of that agent) must be given; passing both is refused
    loudly instead of silently favouring one.
    """
    if agent is None and jti is None:
        console.print("[red]Provide --agent <id> (revoke all) or --jti <id> (revoke one).[/red]")
        raise typer.Exit(1)
    if agent is not None and jti is not None:
        # Review N4: giving both silently favoured --jti — refuse loudly.
        console.print("[red]--agent and --jti are mutually exclusive.[/red]")
        raise typer.Exit(1)
    settings = _resolve_settings(config)
    store = AgentTokenStore(settings.db_path)
    try:
        now = int(datetime.now(UTC).timestamp())
        if jti is not None:
            ok = store.revoke_jti(jti, now=now)
            if ok:
                console.print(f"[green]✓[/green] Token {jti} revoked.")
            else:
                console.print(f"[red]Token {jti} not found or already revoked.[/red]")
                raise typer.Exit(1)
        else:
            count = store.revoke_agent(agent or "", now=now)
            console.print(f"[green]✓[/green] Revoked {count} live token(s) of agent {agent}.")
    finally:
        store.close()


@agent_token_app.command("list")
def token_list(config: ConfigOption = None) -> None:
    """List issuance metadata (jti, scope, exp, status) — no token values.

    Audit view of every issued token: which agent, what grants, when it
    expires, live or rotated/revoked. Secrets never appear here — use it to
    find the `--jti` for a revoke or to spot tokens worth rotating.
    """
    settings = _resolve_settings(config)
    store = AgentTokenStore(settings.db_path)
    try:
        rows = store.list_tokens()
    finally:
        store.close()
    if not rows:
        console.print("[yellow]No agent tokens found.[/yellow]")
        return
    now = int(datetime.now(UTC).timestamp())
    table = Table("jti", "agent", "node/aud", "scope", "iat", "exp", "status")
    for row in rows:
        status = "revoked"
        if row["revoked_at"] is None:
            if int(row["exp"]) <= now:
                status = "expired"
            elif row["grace_until"] is not None:
                status = "grace" if now < int(row["grace_until"]) else "grace-expired"
            else:
                status = "live"
        table.add_row(
            str(row["jti"]),
            str(row["agent_id"]),
            str(row["aud"]),
            str(row["scope"]),
            _fmt_ts(int(row["iat"])),
            _fmt_ts(int(row["exp"])),
            status,
        )
    console.print(table)
