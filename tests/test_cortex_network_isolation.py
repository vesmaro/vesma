"""W5d guard: the vesma-cortex provider leg carries ZERO network imports.

Import-isolation tripwire for :class:`vesmaro.decision_provider.VesmaProvider`
(inference-v1.md §9): the artifact and its wrapper must not import
``socket`` / ``urllib`` / ``http`` / ``requests`` / ``httpx`` / ``aiohttp`` /
``ftp`` — the cortex leg is local-only by construction, and the property
must fail loud instead of drifting silently. Pattern: the established
ADR-0023 tripwire ``tests/test_mcp_core_isolation.py`` (the mcp SDK
isolation guard).

Scope is the MODULE hosting the provider — ``src/vesmaro/decision_provider.py``.
The sibling ``decision_jev.py`` is deliberately NOT scanned: its httpx leg
is the sanctioned outbound Jev adapter with its own privacy gate (a
different, opt-in implementation of the seam), while the cortex leg's
contract is «zero network, forever».

Honest limits (same as the ADR-0023 tripwire): the AST scan is a lexical
tripwire — it does not catch ``importlib.import_module("httpx")``,
``__import__``, dynamically composed module names or ``exec``-based
imports.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "vesmaro"
#: The module that hosts VesmaProvider, the feature builder and the
#: bundle loader — the entire cortex runtime surface.
GUARDED_FILE = "decision_provider.py"

#: Forbidden import ROOTS (inference-v1.md §9). No allow-list, zero
#: tolerance: the cortex leg has no sanctioned network call, ever.
FORBIDDEN_NETWORK_ROOTS = frozenset(
    {"socket", "urllib", "http", "requests", "httpx", "aiohttp", "ftp"}
)


def _network_import_roots(path: Path) -> set[str]:
    """Import roots under the network ban found in ``path``'s AST."""
    offenders: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in FORBIDDEN_NETWORK_ROOTS:
                    offenders.add(root)
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in FORBIDDEN_NETWORK_ROOTS and node.level == 0:
                offenders.add(root)
    return offenders


def test_cortex_provider_module_has_no_network_imports() -> None:
    offenders = _network_import_roots(SRC / GUARDED_FILE)
    assert not offenders, (
        f"{GUARDED_FILE} imports network modules {sorted(offenders)} — the "
        "vesma-cortex leg is local-only by contract (inference-v1.md §9)"
    )


def test_cortex_tripwire_still_guards_the_real_module() -> None:
    """Guard-contract drift check: VesmaProvider must live in the guarded
    module — if the provider moves, re-point GUARDED_FILE at its new home
    (pattern: ``test_mcp_server_itself_declares_its_imports``)."""
    source = (SRC / GUARDED_FILE).read_text(encoding="utf-8")
    assert "class VesmaProvider" in source, (
        f"VesmaProvider is no longer defined in {GUARDED_FILE} — re-point the "
        "network-isolation tripwire at the module that hosts it now"
    )


def test_tripwire_detects_a_network_import() -> None:
    """The scanner itself works: a module importing httpx is flagged."""
    probe = ast.parse("import httpx\nfrom urllib.request import urlopen\n")
    offenders: set[str] = set()
    for node in ast.walk(probe):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_NETWORK_ROOTS:
                    offenders.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in FORBIDDEN_NETWORK_ROOTS and node.level == 0:
                offenders.add(root)
    assert offenders == {"httpx", "urllib"}
