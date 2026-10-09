"""Import shim for gRPC-generated stubs (vesma-mesh Phase 3, issue #105 M3).

The gRPC Python plugin emits flat top-level imports
(``import mnemos_core_api_pb2 as ...``) inside the generated
``*_pb2_grpc.py`` files. The generated directory
(``federation/gen/python/``) is gitignored and lives outside the
``vesma`` package tree, so the generated modules are not importable as
ordinary package members.

This shim resolves that by inserting the generated directory on
``sys.path`` *once* and re-exporting the four generated modules under
stable, package-qualified names. Importers use::

    from vesma._mesh_gen import core_pb2, core_pb2_grpc, fed_pb2

instead of touching ``sys.path`` themselves. The generated directory
location is resolved from the package itself AND this file's own path AND
the ``VESMA_MESH_GEN_DIR`` environment variable, in candidate order:

1. **Env override** ``VESMA_MESH_GEN_DIR`` — authoritative when set (an
   operator/diagnostic redirect must never be silently second-guessed);
   when the named directory does not exist the resolution fails loudly
   naming the variable, with NO fallback to the structural candidates.
2. **In-package wheel copy** — ``vesma/_mesh_gen_stubs/`` (issue #514
   tail; cli-audit 2026-10-08 finding #2): the wheel force-includes the
   generated stubs INTO the package, so a plain ``pip install`` carries
   them and the mesh legs work out of the box. This candidate wins
   whenever the stubs were vendored at build time.
3. **Source-checkout layout** — ``src/vesma/_mesh_gen.py`` (also the
   ``pip install -e`` shape): three ``parent`` hops reach the repo root,
   stubs live at ``<repo>/federation/gen/python`` (issue #514 layout a).
4. **Site-packages layout** — an installed wheel:
   ``<venv>/lib/python3.14/site-packages/vesma/_mesh_gen.py`` (a
   ``lib64`` symlink is resolved away first): two ``parent`` hops reach
   ``site-packages``, stubs live at
   ``<venv>/lib/python3.14/site-packages/federation/gen/python``
   (issue #514 layout b — the manual placement path, kept for
   operator-managed installs).

The first EXISTING candidate wins; when none exists the failure is a
clear :class:`ImportError` enumerating every probed path and the
generation command (issue #514: the old single-candidate resolution
assumed the source-checkout depth and made wheel installs unfixable).

This is the import strategy documented in :mod:`vesma.mesh_client`.
Generated code is dynamically imported via :func:`importlib.import_module`,
so the attributes below are typed as ``Any`` by mypy (the project's
``ignore_missing_imports = true`` config treats the generated modules as
``Any``); callers apply targeted ``# type: ignore[name-defined]`` at the
proto-message construction sites in :mod:`vesma.mesh_client`.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from typing import Any, Final

#: Environment override for the generated stubs directory (operators/
#: diagnostics). Authoritative when set: see the resolution order in the
#: module docstring; an unset-or-empty value leaves the structural
#: candidates in charge.
GEN_DIR_ENV_VAR: Final[str] = "VESMA_MESH_GEN_DIR"


def _candidate_gen_dirs() -> tuple[Path, ...]:
    """Structural candidates, in resolution order (vendored copy first).

    Candidate shapes (issue #514 + the #514-tail wheel vendoring), derived
    from THIS file's location after ``resolve()`` (follows ``lib64 -> lib``
    symlinks in wheel installs):

    - in-package wheel copy: ``.../vesma/_mesh_gen_stubs/`` — the wheel
      force-includes the generated stubs into the package (cli-audit
      2026-10-08 finding #2), so a pip install works out of the box;
    - source checkout: ``<repo>/src/vesma/_mesh_gen.py`` ->
      ``parents[2]`` == repo root (three ``parent`` hops: vesma -> src
      -> ``<repo>``), stubs at ``<repo>/federation/gen/python``;
    - site-packages: ``<venv>/lib/python3.14/site-packages/vesma/`` ->
      ``parents[1]`` == ``site-packages`` (two ``parent`` hops), stubs at
      ``<venv>/lib/python3.14/site-packages/federation/gen/python``.
    """
    here = Path(__file__).resolve()
    return (
        here.parent / "_mesh_gen_stubs",
        here.parent.parent.parent / "federation" / "gen" / "python",
        here.parent.parent / "federation" / "gen" / "python",
    )


def _probed_listing(candidates: tuple[Path, ...]) -> str:
    return "\n".join(f"  - {candidate}" for candidate in candidates)


def _resolve_gen_dir() -> Path:
    """First existing candidate wins; no candidate = loud ImportError.

    The env override (``VESMA_MESH_GEN_DIR``) is checked FIRST and is
    authoritative when set: a missing override directory is an error
    naming the variable -- falling through to the structural candidates
    would silently discard the operator's stated intent (the exact
    diagnostic confusion the override exists to resolve).
    """
    env_value = os.environ.get(GEN_DIR_ENV_VAR, "").strip()
    if env_value:
        overridden = Path(env_value).expanduser()
        if overridden.is_dir():
            return overridden
        raise ImportError(
            f"{GEN_DIR_ENV_VAR}={env_value!r} — the directory does not exist. "
            f"The override is authoritative (no silent fallback): fix the path "
            f"(the directory must contain the generated modules, e.g. "
            f"mnemos_core_api_pb2.py) or unset {GEN_DIR_ENV_VAR}."
        )
    candidates = _candidate_gen_dirs()
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    # cli-audit 2026-10-08 (finding #2): the fix hint must address the
    # INSTALL the user actually has — "run gen-proto.sh from a source
    # checkout" is meaningless to a pip user. Reinstalling the wheel
    # rebuilds nothing; the stubs ride the wheel only when the BUILD
    # machine had generated them. The honest fix ladder: upgrade to a
    # wheel built with the stubs, or generate from source.
    raise ImportError(
        "gRPC generated stubs not found — mesh federation is unavailable "
        "in this install. Probed:\n"
        f"{_probed_listing(candidates)}\n"
        "Fix (any one):\n"
        "  1. reinstall vesma from an official distribution built WITH the\n"
        "     mesh stubs vendored (pip install --upgrade vesma);\n"
        "  2. from a source checkout run `bash scripts/gen-proto.sh` and keep\n"
        "     the stubs at <repo>/federation/gen/python;\n"
        "  3. place the generated directory next to site-packages\n"
        "     (<site-packages>/federation/gen/python), or point\n"
        "     VESMA_MESH_GEN_DIR at the directory containing\n"
        "     mnemos_core_api_pb2.py."
    )


#: Absolute path to the gRPC-generated Python stubs directory.
#:
#: Resolved once at import time: the env override when set and existing,
#: else the first existing structural candidate; no candidate at all is
#: an :class:`ImportError` at import (fail-fast — the mesh legs cannot
#: work without the stubs). Kept as a resolved ``Path`` so the shim works
#: regardless of the current working directory.
_GEN_DIR: Path = _resolve_gen_dir()


def _ensure_gen_dir_on_path() -> None:
    """Insert the generated stubs directory on ``sys.path`` once.

    Idempotent: a no-op if the directory is already present. Called at
    import time so callers do not need to invoke it manually.
    """
    gen_dir_str = str(_GEN_DIR)
    if gen_dir_str not in sys.path:
        sys.path.insert(0, gen_dir_str)


_ensure_gen_dir_on_path()

#: ``mnemos_core_api_pb2`` — request/response messages for the MnemosCore
#: service (ListMemories, WriteMemory, GetSubscriptionState, Heartbeat).
core_pb2: Any = importlib.import_module("mnemos_core_api_pb2")

#: ``mnemos_core_api_pb2_grpc`` — ``MnemosCoreStub`` / ``VesmaCoreServicer``
#: for the core service over the Unix socket.
core_pb2_grpc: Any = importlib.import_module("mnemos_core_api_pb2_grpc")

#: ``federation_pb2`` — ``CompactRecord``, ``TriggerCodes`` and the other
#: federation.v1 messages shared between the peer and core APIs.
fed_pb2: Any = importlib.import_module("federation_pb2")

#: ``agent_gateway_pb2`` — W3-v1 AgentGateway service messages (agent leg,
#: ADR-0018 variant (c)). Loaded LAZILY via module ``__getattr__`` (PEP
#: 562): the generated stubs are gitignored and environments that have not
#: re-run ``scripts/gen-proto.sh`` since W3 do not have the file — an eager
#: import here would break them at ``vesma`` import time. The bare
#: annotations below (no assignment) document the lazy names for static
#: tools without creating the attributes.
_AGENT_LAZY_MODULES: Final[dict[str, str]] = {
    "gateway_pb2": "agent_gateway_pb2",
    "gateway_pb2_grpc": "agent_gateway_pb2_grpc",
}

gateway_pb2: Any
gateway_pb2_grpc: Any


def __getattr__(name: str) -> Any:
    """Lazy re-export of the W3 agent-gateway generated modules.

    Raises ``AttributeError`` with a pointer to ``scripts/gen-proto.sh``
    when the stubs are missing, instead of a bare import error (the known
    stale-gen trap documented in the script header).
    """
    module_name = _AGENT_LAZY_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    _ensure_gen_dir_on_path()
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        raise AttributeError(
            f"{name} unavailable — generated stubs missing. Run: bash scripts/gen-proto.sh"
        ) from exc


__all__ = ["_GEN_DIR", "core_pb2", "core_pb2_grpc", "fed_pb2", "gateway_pb2", "gateway_pb2_grpc"]
