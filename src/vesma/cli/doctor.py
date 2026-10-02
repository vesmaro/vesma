"""``vesma doctor`` CLI subcommand — health check.

Runs a series of checks against the local Vesma installation and reports
status. Exit codes:

* 0 — all checks pass
* 1 — one or more checks failed
* 2 — one or more checks warn (e.g. stale integration) but nothing is broken
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any

import typer
from rich.console import Console
from rich.table import Table

from vesma import __version__
from vesma.config import load_settings

logger = logging.getLogger(__name__)
console = Console()

doctor_app = typer.Typer(
    name="doctor",
    help="Run Vesma health checks (config, vault, DB, pending refine queue, MCP, "
    "integration, tags).",
    no_args_is_help=False,
)


# ── Check result model ────────────────────────────────────────────────────────


class CheckStatus(StrEnum):
    PASS = "pass"  # nosec B105 — status enum value, not a password
    WARN = "warn"
    FAIL = "fail"


@dataclass
class CheckResult:
    name: str
    status: CheckStatus
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# ── Individual checks ─────────────────────────────────────────────────────────


def _check_config() -> CheckResult:
    """Config loads and is valid."""
    try:
        settings = load_settings()
        settings.resolve_paths()
    except Exception as exc:  # doctor must report, not crash
        return CheckResult("Config", CheckStatus.FAIL, f"load failed: {exc}")
    # See vesma.config.find_config_file for the full search order.
    cfg_path = os.environ.get("VESMA_CONFIG") or str(Path.home() / ".mnemos" / "config.yaml")
    return CheckResult(
        "Config",
        CheckStatus.PASS,
        f"{cfg_path} (valid)",
        extra={"settings": settings},
    )


def _check_data_dir(settings: Any) -> CheckResult:
    """Data dir exists and is writable."""
    data_dir = settings.vesma.data_dir
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        test_file = data_dir / ".mnemos_doctor_write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
    except OSError as exc:
        return CheckResult("Data dir", CheckStatus.FAIL, f"{data_dir} not writable: {exc}")

    # Size estimate.
    try:
        total = sum(f.stat().st_size for f in data_dir.rglob("*") if f.is_file())
        size_mb = total / (1024 * 1024)
        size_str = f"{size_mb:.1f} MB"
    except OSError:
        size_str = "size unknown"

    return CheckResult("Data dir", CheckStatus.PASS, f"{data_dir} (writable, {size_str})")


def _check_vault(settings: Any) -> CheckResult:
    """Vault path exists, is writable, and count markdown files."""
    vault = settings.vesma.vault_path
    try:
        vault.mkdir(parents=True, exist_ok=True)
        test_file = vault / ".mnemos_doctor_write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
    except OSError as exc:
        return CheckResult("Vault", CheckStatus.FAIL, f"{vault} not writable: {exc}")

    note_count = sum(1 for _ in vault.rglob("*.md"))
    return CheckResult("Vault", CheckStatus.PASS, f"{vault} (writable, {note_count} notes)")


def _check_sqlite(settings: Any) -> CheckResult:
    """SQLite DB exists, opens, and reports row count."""
    db_path = settings.db_path
    if not db_path.exists():
        return CheckResult(
            "SQLite DB",
            CheckStatus.WARN,
            f"{db_path} (missing — run `vesma add` to create)",
        )
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute("SELECT COUNT(*) FROM memories").fetchone()
            count = row[0] if row else 0
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return CheckResult("SQLite DB", CheckStatus.FAIL, f"{db_path} (error: {exc})")
    return CheckResult("SQLite DB", CheckStatus.PASS, f"{db_path} (healthy, {count:,} entries)")


def _check_vector_store(settings: Any) -> CheckResult:
    """Vector store DB exists, opens, reports embedding count + vintage.

    Vintage mismatch (ADR-0021 embedder swap): rows whose metadata
    ``model_fingerprint`` differs from the fingerprint of the configured
    embedder were cut by another embedding geometry. Mixing spaces in
    one index silently degrades vector search; the background heal
    sweeper re-embeds them gradually, or `vesma reindex` rebuilds in
    one pass. Diagnostics only — the doctor never re-embeds.
    """
    vectors_path = settings.vesma.data_dir / "vectors.db"
    if not vectors_path.exists():
        return CheckResult(
            "Vector store",
            CheckStatus.WARN,
            f"{vectors_path} (missing — created on first add)",
        )

    # Current embedder identity WITHOUT loading a provider session (the
    # nano leg only hashes the bundled artifact file).
    try:
        from vesma.embeddings import config_fingerprint

        current_fp: str | None = config_fingerprint(settings.embedding)
    except Exception as exc:  # doctor must report, not crash
        logger.debug("doctor embedder fingerprint unavailable: %s", exc)
        current_fp = None

    try:
        conn = sqlite3.connect(str(vectors_path))
        try:
            # The table name is `embeddings` in the current VectorStore schema.
            row = conn.execute("SELECT COUNT(*) FROM embeddings").fetchone()
            count = row[0] if row else 0
            metadata_rows = (
                conn.execute("SELECT metadata FROM embeddings").fetchall() if count else []
            )
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return CheckResult("Vector store", CheckStatus.FAIL, f"{vectors_path} (error: {exc})")

    vintage_stale = 0
    if current_fp is not None and metadata_rows:
        for (raw_meta,) in metadata_rows:
            try:
                meta = json.loads(raw_meta) if raw_meta else {}
            except (json.JSONDecodeError, TypeError):
                meta = {}
            if not isinstance(meta, dict) or meta.get("model_fingerprint") != current_fp:
                # Missing key = pre-fingerprint vintage (pre round-3 swap).
                vintage_stale += 1

    if vintage_stale:
        return CheckResult(
            "Vector store",
            CheckStatus.WARN,
            f"{vectors_path} (healthy, {count:,} embeddings, "
            f"{vintage_stale:,} cut by another embedder — the background heal "
            "re-embeds them gradually; `vesma reindex` rebuilds in one pass)",
        )
    detail_suffix = "" if current_fp is None else ", vintage current"
    return CheckResult(
        "Vector store",
        CheckStatus.PASS,
        f"{vectors_path} (healthy, {count:,} embeddings{detail_suffix})",
    )


def _check_mcp_transport() -> CheckResult:
    """Import smoke-check for the MCP transport module (#185, direction C).

    The MCP SDK major bump (1.x → 2.x) removed the 1.x decorator API; a
    version/SDK mismatch previously killed the stdio transport *silently*
    (the CLI kept working, so nothing surfaced the breakage). This check
    imports ``vesma.mcp_server`` — which exercises the full
    ``mcp`` SDK import surface — and reports the exact failure with the
    remediation hint, so a broken transport is loud, not silent.
    """
    try:
        import vesma.mcp_server

        tools: list[str] = []
        # Best-effort tool-count probe; failures here fall back to the
        # plain import result (the tool manifest needs settings/DB access).
        try:
            import asyncio

            manifest = asyncio.run(vesma.mcp_server.list_tools())
            tools = [t.name for t in manifest]
        except Exception as exc:  # pragma: no cover — depends on local env
            logger.debug("doctor tool-manifest probe skipped: %s", exc)
        sdk = mcp_sdk_version()
        detail = (
            f"vesma.mcp_server imports OK (SDK {sdk})"
            if not tools
            else f"vesma.mcp_server imports OK (SDK {sdk}, {len(tools)} tools listed)"
        )
        return CheckResult("MCP transport", CheckStatus.PASS, detail)
    except ImportError as exc:
        return CheckResult(
            "MCP transport",
            CheckStatus.FAIL,
            f"MCP transport broken: {exc}; reinstall the package "
            "(pip install --force-reinstall mnemos-memory-server) — mcp>=2.0,<3.0 is a "
            "core dependency since 4.1.0",
        )
    except AttributeError as exc:
        # Classic 1.x-decorator-on-2.x-SDK (or vice versa) signature break.
        return CheckResult(
            "MCP transport",
            CheckStatus.FAIL,
            f"MCP transport broken: {exc!r} — the installed mcp SDK version "
            "does not match vesma.mcp_server (expects mcp>=2.0,<3.0); "
            "reinstall the package (pip install --force-reinstall mnemos-memory-server) "
            "or fix the installed mcp SDK version",
        )
    except Exception as exc:  # doctor reports, doesn't crash
        return CheckResult(
            "MCP transport",
            CheckStatus.FAIL,
            f"MCP transport broken: unexpected {type(exc).__name__}: {exc}; "
            "reinstall mnemos-memory-server (mcp>=2.0,<3.0 is core since 4.1.0)",
        )


def mcp_sdk_version() -> str:
    """Return the installed mcp SDK version, or '?' when not importable."""
    try:
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as _pkg_version

        return _pkg_version("mcp")
    except PackageNotFoundError:  # pragma: no cover — mcp absent
        return "not installed"
    except Exception:  # pragma: no cover — metadata unreadable
        return "?"


def _mcp_keys_seen(path: Path, mcp_format: str | None) -> frozenset[str] | None:
    """Server key generations present in a registry MCP config file.

    Returns ``None`` when the file exists but cannot be parsed (corrupt
    JSON/TOML, non-UTF-8) — the check must not claim evidence either
    way. Otherwise a subset of {``vesma``, ``mnemos``}: both key
    generations count during the dual period until 6.0 (#467).

    The lookup shape comes from the target's registry ``mcp.format`` —
    never guessed from the filename: ``codex`` is TOML with servers
    under ``[mcp_servers.<key>]``; ``zcode`` nests them under
    ``mcp.servers``; ``opencode`` maps server names DIRECTLY under the
    top-level ``mcp`` key; every other JSON format uses the top-level
    ``mcpServers``.
    """
    from vesma.cli.integration import CODEX_MCP_ROOT, MCP_LEGACY_SERVER_KEY, MCP_SERVER_KEY

    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    servers: Any = None
    if mcp_format == "codex":
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return None
        servers = data.get(CODEX_MCP_ROOT)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if isinstance(data, dict):
            if mcp_format == "zcode":
                mcp = data.get("mcp")
                servers = mcp.get("servers") if isinstance(mcp, dict) else None
            elif mcp_format == "opencode":
                servers = data.get("mcp")
            else:
                servers = data.get("mcpServers")

    if not isinstance(servers, dict):
        return frozenset()
    return frozenset(k for k in (MCP_SERVER_KEY, MCP_LEGACY_SERVER_KEY) if k in servers)


def _check_mcp_server() -> CheckResult:
    """Check every registry MCP surface for a Vesma server entry (#467).

    Registry-driven (ADR-0024 — no hardcoded paths): every target in
    ``integrations/targets.yaml`` that declares an ``mcp.config`` is
    scanned with its declared format. Both key generations count (dual
    period until 6.0): the brand-primary ``vesma`` key and the legacy
    ``mnemos`` key. The pi target has no server key — its deployed
    TypeScript bridge IS the registration. Report-only: this check
    never writes or migrates configs.
    """
    from vesma.cli.integration import MCP_LEGACY_SERVER_KEY, MCP_SERVER_KEY, load_targets

    try:
        cfg = load_targets()
    except Exception as exc:  # doctor reports, doesn't crash
        return CheckResult("MCP server", CheckStatus.FAIL, f"targets.yaml load failed: {exc}")

    notes: list[str] = []
    absent = 0
    vesma_seen = False
    legacy_seen = False
    mcp_surfaces = 0
    for target in cfg.targets:
        if target.mcp_config is None:
            continue
        mcp_surfaces += 1
        path = target.mcp_config
        if target.mcp_format == "pi":
            # The TypeScript bridge is the registration itself — presence
            # is the wiring, there is no server key inside.
            if path.exists():
                notes.append(f"{target.name}: MCP bridge deployed ({path})")
                vesma_seen = True
            else:
                absent += 1
            continue
        if not path.exists():
            absent += 1
            continue
        seen = _mcp_keys_seen(path, target.mcp_format)
        if seen is None:
            notes.append(f"{target.name}: config unreadable ({path})")
        elif MCP_SERVER_KEY in seen and MCP_LEGACY_SERVER_KEY in seen:
            notes.append(f"{target.name}: vesma + legacy mnemos keys ({path})")
            vesma_seen = True
        elif MCP_SERVER_KEY in seen:
            notes.append(f"{target.name}: vesma key ({path})")
            vesma_seen = True
        elif MCP_LEGACY_SERVER_KEY in seen:
            notes.append(f"{target.name}: legacy mnemos key ({path})")
            legacy_seen = True
        else:
            notes.append(f"{target.name}: config without a vesma entry ({path})")

    if vesma_seen:
        extra = f" ({absent} registry surface(s) absent)" if absent else ""
        return CheckResult("MCP server", CheckStatus.PASS, "; ".join(notes) + extra)
    if legacy_seen:
        return CheckResult(
            "MCP server",
            CheckStatus.PASS,
            "; ".join(notes)
            + " — LEGACY mnemos key only; re-run `vesma integration setup` to migrate to vesma",
        )
    if mcp_surfaces == 0:
        # The pack declares no MCP-config surfaces at all (minimal/fake
        # packs, MCP-less registries): nothing to verify, never a warning —
        # otherwise --fix would "register" against paths this registry
        # does not describe.
        return CheckResult(
            "MCP server",
            CheckStatus.PASS,
            "registry declares no MCP-config surfaces — check not applicable",
        )
    prefix = "; ".join(notes) + " — " if notes else ""
    return CheckResult(
        "MCP server",
        CheckStatus.WARN,
        prefix + "not registered in any known harness — run `vesma integration setup`",
    )


def _check_integration() -> CheckResult:
    """Verify integration layer: installed version + stale status."""
    try:
        from vesma.cli.integration import IntegrationManager, load_targets

        mgr = IntegrationManager(version=__version__)
        cfg = load_targets()
        detected = cfg.detected()
    except Exception as exc:  # doctor reports, doesn't crash
        return CheckResult("Integration", CheckStatus.FAIL, f"init failed: {exc}")

    if not detected:
        return CheckResult(
            "Integration",
            CheckStatus.WARN,
            "no agent harnesses detected — run `vesma integration detect`",
        )

    # Aggregate verify across all detected targets.
    total_stale = 0
    total_missing = 0
    target_notes: list[str] = []
    for target in detected:
        target_notes.append(f"{target.name} ({target.precedence})")
        try:
            result = mgr.verify(target.name)
            total_stale += result.stale_count
            total_missing += result.missing_count
        except Exception as exc:  # one target failing shouldn't abort
            logger.warning("integration verify failed for target %s: %s", target.name, exc)
            total_missing += 1

    if total_missing > 0:
        return CheckResult(
            "Integration",
            CheckStatus.WARN,
            f"installed v{__version__}, targets: {', '.join(target_notes)}, "
            f"{total_missing} missing file(s) — run `vesma integration setup`",
        )
    if total_stale > 0:
        return CheckResult(
            "Integration",
            CheckStatus.WARN,
            f"installed v{__version__}, targets: {', '.join(target_notes)}, "
            f"{total_stale} stale — run `vesma integration update`",
        )
    return CheckResult(
        "Integration",
        CheckStatus.PASS,
        f"installed v{__version__}, targets: {', '.join(target_notes)}, stale: no",
    )


def _check_pending_refine(settings: Any) -> CheckResult:
    """ADR-0019 Phase D — pending-refinement queue diagnostics.

    Rows with ``pipeline_state='pending'`` are advanced only by the
    BACKGROUND processor (the B2a daemon). The B1 migration backfilled
    bypass-era PUBLISHED rows as pending, so a CLI-only deployment (no
    daemon running) can accumulate a queue that never drains — entries
    stay visible-raw but never refine. This check makes the queue
    visible. Diagnostics ONLY: the doctor deliberately does not run the
    processor — it is a server-side service (``vesma processor start``).
    """
    db_path = settings.db_path
    if not db_path.exists():
        return CheckResult("Pending refine", CheckStatus.PASS, "no database yet")
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM memories WHERE pipeline_state = 'pending'"
            ).fetchone()
            count = int(row[0]) if row else 0
        finally:
            conn.close()
    except sqlite3.OperationalError as exc:
        if "no such column" in str(exc):
            # Pre-ADR-0019 schema: the pipeline lifecycle column does not
            # exist → no optimistic-publication queue by definition.
            return CheckResult(
                "Pending refine",
                CheckStatus.PASS,
                "pre-ADR-0019 schema (no pipeline lifecycle column)",
            )
        return CheckResult("Pending refine", CheckStatus.FAIL, f"scan error: {exc}")
    except sqlite3.Error as exc:
        return CheckResult("Pending refine", CheckStatus.FAIL, f"scan error: {exc}")
    if count:
        plural = "entry is" if count == 1 else "entries are"
        return CheckResult(
            "Pending refine",
            CheckStatus.WARN,
            f"{count:,} {plural} awaiting async refinement (pipeline_state=pending) "
            "— start the background processor: `vesma processor start` "
            "(CLI-only deployments have no daemon; the queue never drains on its own)",
        )
    return CheckResult("Pending refine", CheckStatus.PASS, "0 entries awaiting refinement")


def _check_tag_contract(settings: Any) -> CheckResult:
    """Report tag contract mode + non-conformant entry count (if fast)."""
    strict = settings.vesma.strict_tag_contract
    mode = "strict" if strict else "lenient"

    # Count non-conformant entries only if the DB exists and the scan is cheap.
    db_path = settings.db_path
    if not db_path.exists():
        return CheckResult(
            "Tag contract",
            CheckStatus.WARN,
            f"{mode} mode, DB missing — cannot scan",
        )

    non_conformant = 0
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            # tags is stored as JSON array; we check for project:/agent:/mnemos: prefixes.
            rows = conn.execute("SELECT tags FROM memories").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return CheckResult("Tag contract", CheckStatus.FAIL, f"{mode} mode, scan error: {exc}")

    for (raw_tags,) in rows:
        try:
            tags = json.loads(raw_tags) if raw_tags else []
        except (json.JSONDecodeError, TypeError):
            non_conformant += 1
            continue
        has_project = any(t.startswith("project:") for t in tags)
        has_agent = any(t.startswith("agent:") for t in tags)
        has_mnemos = any(t.startswith("mnemos:") for t in tags)
        if not (has_project and has_agent and has_mnemos):
            non_conformant += 1

    if non_conformant > 0:
        return CheckResult(
            "Tag contract",
            CheckStatus.WARN,
            f"{mode} mode, {non_conformant} non-conformant entries",
        )
    return CheckResult(
        "Tag contract", CheckStatus.PASS, f"{mode} mode, {non_conformant} non-conformant entries"
    )


def _check_agent_wiring() -> CheckResult:
    """Check Copilot agent MCP wiring status in ``~/.copilot/agents``.

    * PASS — all detected agents have vesma tools wired (or are skipped
      via ``tool_profile``).
    * WARN — some agents are unwired (lists the count).
    * SKIP — no agents directory found (non-Copilot setup).
    """
    try:
        from vesma.cli.agent_wiring import DEFAULT_AGENTS_DIR, verify_agents

        if not DEFAULT_AGENTS_DIR.is_dir():
            return CheckResult(
                "Agent wiring",
                CheckStatus.WARN,
                f"no agents directory at {DEFAULT_AGENTS_DIR} (non-Copilot setup)",
            )

        summary = verify_agents()
    except Exception as exc:  # doctor reports, doesn't crash
        return CheckResult("Agent wiring", CheckStatus.FAIL, f"check crashed: {exc}")

    if summary.total == 0:
        return CheckResult(
            "Agent wiring",
            CheckStatus.WARN,
            f"no .agent.md files in {DEFAULT_AGENTS_DIR}",
        )

    if summary.unwired == 0 and summary.errors == 0:
        return CheckResult(
            "Agent wiring",
            CheckStatus.PASS,
            f"{summary.wired}/{summary.total} wired, "
            f"{summary.skipped_tool_profile} skipped (tool_profile)",
        )

    return CheckResult(
        "Agent wiring",
        CheckStatus.WARN,
        f"{summary.wired}/{summary.total} wired, {summary.unwired} unwired, "
        f"{summary.skipped_tool_profile} skipped — run `vesma integration setup`",
    )


# ── Paths overview ────────────────────────────────────────────────────────────


def _collect_paths(settings: Any) -> dict[str, str]:
    """Collect all relevant Vesma paths as display strings.

    Returns a dict with keys: root, config, data_dir, db_path, vault, logs,
    cache, completion, mcp_config.
    """
    home = Path.home()
    root = home / ".mnemos"
    mcp_cfg = home / ".config" / "Code" / "User" / "mcp.json"

    # Use ~ abbreviation for display where possible.
    def _display(p: Path) -> str:
        s = str(p)
        home_s = str(home)
        if s.startswith(home_s):
            return "~" + s[len(home_s) :]
        return s

    return {
        "root": _display(root),
        "config": _display(root / "config.yaml"),
        "data_dir": _display(settings.vesma.data_dir),
        "db_path": _display(settings.db_path),
        "vault": _display(settings.vesma.vault_path),
        "logs": _display(settings.logging.log_file)
        if settings.logging.log_file
        else "(stderr only)",
        "cache": _display(root / "cache"),
        "completion": _display(root / "completion"),
        "mcp_config": _display(mcp_cfg),
    }


def _render_paths(paths: dict[str, str]) -> None:
    """Print the paths table to the console."""
    console.print()
    console.print("[bold cyan]── Paths ──────────────────────────────────────[/bold cyan]")
    labels = [
        ("Root", "root"),
        ("Config", "config"),
        ("Data dir", "data_dir"),
        ("DB", "db_path"),
        ("Vault", "vault"),
        ("Logs", "logs"),
        ("Cache", "cache"),
        ("Completion", "completion"),
        ("MCP config", "mcp_config"),
    ]
    for label, key in labels:
        console.print(f"  {label:<13} {paths.get(key, '?')}")


# ── Runner ───────────────────────────────────────────────────────────────────


# Settings-dependent checks (take the resolved settings object).
_SETTINGS_CHECKS = (
    _check_data_dir,
    _check_vault,
    _check_sqlite,
    _check_vector_store,
    _check_pending_refine,
    _check_tag_contract,
)


def _run_all_checks() -> list[CheckResult]:
    """Run every check in order, threading the settings object through.

    The doctor must never crash the CLI: each check is wrapped so an
    unexpected exception becomes a FAIL result with the exception message.
    """
    results: list[CheckResult] = []

    # Config first — other checks depend on its resolved settings.
    try:
        result = _check_config()
        settings: Any = result.extra.get("settings")
        results.append(result)
    except Exception as exc:  # doctor must never crash
        results.append(CheckResult("_check_config", CheckStatus.FAIL, f"check crashed: {exc}"))
        settings = None

    # No-arg checks.
    for check in (_check_mcp_server, _check_integration, _check_agent_wiring, _check_mcp_transport):
        try:
            results.append(check())
        except Exception as exc:  # doctor must never crash
            results.append(CheckResult(check.__name__, CheckStatus.FAIL, f"check crashed: {exc}"))

    # Settings-dependent checks.
    for settings_check in _SETTINGS_CHECKS:
        try:
            results.append(settings_check(settings))
        except Exception as exc:  # doctor must never crash
            name = settings_check.__name__
            results.append(CheckResult(name, CheckStatus.FAIL, f"check crashed: {exc}"))

    return results


def _exit_code(results: list[CheckResult]) -> int:
    """Compute the exit code from the check results."""
    if any(r.status == CheckStatus.FAIL for r in results):
        return 1
    if any(r.status == CheckStatus.WARN for r in results):
        return 2
    return 0


def _warn_names(results: list[CheckResult]) -> list[str]:
    """Names of all WARN-level checks in the results."""
    return [r.name for r in results if r.status == CheckStatus.WARN]


# ── Auto-fix actions (--fix) ──────────────────────────────────────────────────


@dataclass
class _FixAction:
    """A callable that attempts to fix a WARN-level check."""

    description: str
    run: Callable[[], tuple[bool, str]]


def _fix_integration_stale() -> tuple[bool, str]:
    """Run ``integration update`` to bring stale files to current version."""
    from vesma.cli.integration import IntegrationManager, load_targets

    mgr = IntegrationManager(version=__version__)
    cfg = load_targets()
    detected = cfg.detected()
    if not detected:
        return False, "no agent harnesses detected"
    updated = 0
    for target in detected:
        mgr.update(target.name)
        # Verify after update — count targets that are now fully current.
        verify = mgr.verify(target.name)
        if verify.stale_count == 0 and verify.missing_count == 0:
            updated += 1
    return True, f"updated {updated}/{len(detected)} target(s) to v{__version__}"


def _fix_agent_wiring() -> tuple[bool, str]:
    """Wire mnemos/* into all unwired Copilot agents."""
    from vesma.cli.agent_wiring import detect_agents, wire_agents
    from vesma.cli.util import _resolve_agents_to_wire

    agents = detect_agents()
    if not agents:
        return False, "no agents found"
    to_wire = _resolve_agents_to_wire(agents, select=None, wire_all=True)
    if not to_wire:
        return True, "all agents already wired"
    results = wire_agents(to_wire, mode="wildcard")
    wired = sum(1 for r in results if r.status.value == "wired")
    return True, f"wired {wired}/{len(to_wire)} agent(s)"


def _fix_mcp_registration() -> tuple[bool, str]:
    """Register the MCP server: JSON targets first, then mcp-setup.sh."""
    from vesma.cli.integration import IntegrationManager

    mgr = IntegrationManager(version=__version__)
    notes: list[str] = []
    any_ok = False
    for target in mgr.targets.detected():
        if target.mcp_config is not None:
            ok, note = mgr.register_mcp(target.name)
            any_ok = any_ok or ok
            notes.append(note)
    ok, note = mgr.register_mcp()  # legacy VS Code path
    any_ok = any_ok or ok
    notes.append(note)
    return any_ok, "; ".join(notes)


def _fix_action_for(check_name: str) -> _FixAction | None:
    """Return the fix action for a WARN-level check, or None if not fixable."""
    actions: dict[str, _FixAction] = {
        "Integration": _FixAction(
            description="vesma integration update (redeploy stale files)",
            run=_fix_integration_stale,
        ),
        "Agent wiring": _FixAction(
            description="vesma integration setup (wires all agents by default)",
            run=_fix_agent_wiring,
        ),
        "MCP server": _FixAction(
            description="MCP server registration (mcp-setup.sh)",
            run=_fix_mcp_registration,
        ),
    }
    return actions.get(check_name)


def _render(results: list[CheckResult]) -> None:
    """Render the results as a rich table."""
    table = Table(title="Vesma Health Check", show_header=True, header_style="bold")
    table.add_column("Status", style="bold", width=4)
    table.add_column("Check", style="bold cyan")
    table.add_column("Detail")

    for r in results:
        if r.status == CheckStatus.PASS:
            icon = "[green]✓[/green]"
        elif r.status == CheckStatus.WARN:
            icon = "[yellow]⚠[/yellow]"
        else:
            icon = "[red]✗[/red]"
        table.add_row(icon, r.name, r.detail)

    console.print(table)


# ── Command ───────────────────────────────────────────────────────────────────


@doctor_app.callback(invoke_without_command=True)
def doctor(
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit results as JSON (for scripting / CI) instead of a table.",
        ),
    ] = False,
    fix: Annotated[
        bool,
        typer.Option(
            "--fix",
            help="Auto-fix WARN-level checks (stale integration, unwired agents, "
            "missing MCP registration). FAIL-level checks are not auto-fixable.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="With --fix: preview what would be fixed without executing.",
        ),
    ] = False,
    paths_only: Annotated[
        bool,
        typer.Option(
            "--paths",
            help="Show only the paths overview table (skip health checks). "
            "Useful for quick reference.",
        ),
    ] = False,
) -> None:
    """Run Vesma health checks and report status.

    Checks: config, data dir, vault, SQLite DB, vector store, pending
    refinement queue, MCP server, integration layer, agent wiring, tag
    contract.

    Exit codes: 0 = all pass, 1 = one or more failed, 2 = warnings only.

    With ``--fix``: attempts to auto-fix WARN-level checks (stale
    integration → ``integration update``, unwired agents →
    ``vesma integration setup``, missing MCP → MCP setup).
    After fixes, re-runs the affected checks and reports the new status.
    ``--fix --dry-run`` previews what would be fixed without executing.

    With ``--paths``: prints only the paths overview table and exits 0.
    """
    # ── --paths: quick reference, no health checks ──────────────────────
    if paths_only:
        try:
            settings = load_settings()
            settings.resolve_paths()
        except Exception as exc:  # doctor must not crash
            console.print(f"[red]✗[/red] Cannot load settings: {exc}")
            raise typer.Exit(1) from exc
        paths = _collect_paths(settings)
        if json_output:
            console.print_json(json.dumps({"paths": paths}))
        else:
            _render_paths(paths)
        raise typer.Exit(0)

    results = _run_all_checks()

    # Collect paths from the config check's settings (already loaded).
    settings_obj: Any = None
    for r in results:
        if r.name == "Config":
            settings_obj = r.extra.get("settings")
            break
    paths = _collect_paths(settings_obj) if settings_obj else {}

    fixed: list[str] = []
    fix_skipped: list[str] = []

    if fix:
        if dry_run:
            console.print("\n[cyan][dry-run][/cyan] Preview of auto-fixes:")
        for r in results:
            if r.status != CheckStatus.WARN:
                continue
            action = _fix_action_for(r.name)
            if action is None:
                fix_skipped.append(r.name)
                continue
            if dry_run:
                console.print(f"  [yellow]⚠[/yellow] {r.name} → would run: {action.description}")
                continue
            console.print(f"\n[yellow]⚠[/yellow] {r.name} → fixing...")
            ok, note = action.run()
            if ok:
                console.print(f"  [green]✓[/green] {r.name}: {note}")
                fixed.append(r.name)
            else:
                console.print(f"  [red]✗[/red] {r.name}: {note}")
                fix_skipped.append(r.name)

        if not dry_run and fixed:
            # Re-run the fixed checks to confirm the new status.
            new_results = _run_all_checks()
            results = new_results

    if json_output:
        exit_code = _exit_code(results)
        payload: dict[str, Any] = {
            "version": __version__,
            "checks": [
                {"name": r.name, "status": r.status.value, "detail": r.detail} for r in results
            ],
            "paths": paths,
            "exit_code": exit_code,
        }
        if fix:
            payload["fixed"] = fixed
            payload["fix_skipped"] = fix_skipped
            payload["dry_run"] = dry_run
        console.print_json(json.dumps(payload))
        raise typer.Exit(exit_code)

    _render(results)
    if paths:
        _render_paths(paths)
    code = _exit_code(results)
    console.print()
    if fix:
        if dry_run:
            console.print(f"[cyan][dry-run][/cyan] Would fix {len(_warn_names(results))} issue(s).")
        elif fixed:
            console.print(f"[green]Fixed: {len(fixed)} issue(s).[/green]")
        if fix_skipped:
            console.print(f"[yellow]Could not auto-fix: {', '.join(fix_skipped)}[/yellow]")
    if code == 0:
        console.print("[green]All checks passed. Vesma is healthy.[/green]")
    elif code == 2:
        console.print("[yellow]⚠ Some checks warn — see above.[/yellow]")
    else:
        console.print("[red]✗ One or more checks failed — see above.[/red]")
    raise typer.Exit(code)


if __name__ == "__main__":  # pragma: no cover — manual invocation
    doctor()
