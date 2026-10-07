"""ADR-0023 guard: the MCP SDK ships in core, but stays lazily imported.

Two contracts are pinned here:

1. **Import isolation** — `mcp` is imported ONLY by `vesma.mcp_server`,
   which itself is loaded only by the `mnemos mcp-server` CLI subcommand
   handler. No other core path may pull the SDK (security review condition,
   architectural committee 2026-09-06): it keeps CLI/API/storage startups
   free of the SDK import surface and makes the isolation property
   fail-loud instead of drifting silently.

2. **Core wiring** — the installed distribution declares `mcp` as a base
   requirement (the whole point of ADR-0023), and `vesma.mcp_server` is
   importable in a bare environment.

Honest limits: the AST scan is a lexical tripwire — it does not catch
`importlib.import_module("mcp")`, `__import__("mcp")`, dynamically composed
module names, or `exec`-based imports; the subprocess test partially
compensates for the CLI entry path. Guard scope is `src/vesma` (the wheel's
force-included `scripts/` holds only shell code).
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "vesma"
ALLOWED_FILES = {"mcp_server.py"}


def _mcp_import_files() -> list[str]:
    """Return repo-relative names of src files that import the mcp SDK."""
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
                if any(n == "mcp" or n.startswith("mcp.") for n in names):
                    offenders.append(rel)
                    break
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                if root == "mcp":
                    offenders.append(rel)
                    break
    return offenders


def test_mcp_sdk_imported_only_by_mcp_server() -> None:
    offenders = [name for name in _mcp_import_files() if name not in ALLOWED_FILES]
    assert not offenders, (
        "mcp SDK imported outside vesma.mcp_server (ADR-0023 isolation): "
        f"{offenders} — route the usage through mcp_server or justify a new "
        "allow-listed file"
    )


def test_mcp_server_itself_declares_its_imports() -> None:
    assert "mcp_server.py" in _mcp_import_files(), (
        "guard contract drift: mnemos/mcp_server.py no longer imports the mcp "
        "SDK directly — re-point ALLOWED_FILES at the real consumer"
    )


def test_cli_import_does_not_pull_mcp_sdk() -> None:
    """Runtime isolation: importing the CLI entry must not load the SDK."""
    code = (
        # 6.0.0: the legacy `import mnemos` shim is gone — the canonical
        # package is `vesma`; the guard imports it to pin the CLI entry path.
        "import sys, vesma, vesma.cli.main\n"
        "loaded = [m for m in sys.modules if m == 'mcp' or m.startswith('mcp.')]\n"
        "assert not loaded, f'mcp SDK loaded by CLI import: {loaded}'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(SRC.parent.parent),
    )
    assert result.returncode == 0, f"isolation broken:\n{result.stderr}"
    assert "mcp SDK loaded" not in result.stdout


def test_mcp_server_imports_cleanly() -> None:
    """With the SDK in core, the module must import in a bare subprocess."""
    code = "import vesma.mcp_server\nprint(':ok')"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(SRC.parent.parent),
    )
    assert result.returncode == 0, f"vesma.mcp_server import failed:\n{result.stderr}"


def test_distribution_declares_mcp_as_core_requirement() -> None:
    """The installed dist metadata must require mcp at the ADR-0023 floor.

    Skipped when the environment's dist metadata is stale (editable install
    predating the rename) — the metadata condition is asserted in CI and in
    any freshly built wheel.
    """
    from importlib import metadata

    import pytest
    from packaging.requirements import Requirement
    from packaging.version import Version

    try:
        dist_version = metadata.version("mnemos-memory-server")
        requires = metadata.requires("mnemos-memory-server") or []
    except metadata.PackageNotFoundError:
        pytest.skip(
            "no mnemos-memory-server dist metadata in this environment — "
            "reinstall `pip install -e .`"
        )
    if Version(dist_version) < Version("4.1.0"):
        pytest.skip(
            f"stale dist metadata {dist_version} predates ADR-0023 — reinstall `pip install -e .`"
        )
    core_requires = [Requirement(r).name for r in requires if "; extra ==" not in r]
    assert "mcp" in core_requires, (
        f"mcp is not a core requirement of the installed dist: {core_requires}"
    )
