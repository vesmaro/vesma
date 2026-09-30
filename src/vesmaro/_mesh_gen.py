"""Import shim for gRPC-generated stubs (mnemos-mesh Phase 3, issue #105 M3).

The gRPC Python plugin emits flat top-level imports
(``import mnemos_core_api_pb2 as ...``) inside the generated
``*_pb2_grpc.py`` files. The generated directory
(``federation/gen/python/``) is gitignored and lives outside the
``vesmaro`` package tree, so the generated modules are not importable as
ordinary package members.

This shim resolves that by inserting the generated directory on
``sys.path`` *once* and re-exporting the four generated modules under
stable, package-qualified names. Importers use::

    from vesmaro._mesh_gen import core_pb2, core_pb2_grpc, fed_pb2

instead of touching ``sys.path`` themselves. The generated directory
location is resolved relative to the repo root (``federation/gen/python``)
so the shim works both from a source checkout and after ``pip install -e``.

This is the import strategy documented in :mod:`vesmaro.mesh_client`.
Generated code is dynamically imported via :func:`importlib.import_module`,
so the attributes below are typed as ``Any`` by mypy (the project's
``ignore_missing_imports = true`` config treats the generated modules as
``Any``); callers apply targeted ``# type: ignore[name-defined]`` at the
proto-message construction sites in :mod:`vesmaro.mesh_client`.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Final

#: Absolute path to the gRPC-generated Python stubs directory.
#:
#: Resolved relative to this file: ``src/vesmaro/_mesh_gen.py`` ->
#: ``../../federation/gen/python``. Kept as a resolved ``Path`` so the
#: shim works regardless of the current working directory.
_GEN_DIR: Path = Path(__file__).resolve().parent.parent.parent / "federation" / "gen" / "python"


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

#: ``mnemos_core_api_pb2_grpc`` — ``MnemosCoreStub`` / ``MnemosCoreServicer``
#: for the core service over the Unix socket.
core_pb2_grpc: Any = importlib.import_module("mnemos_core_api_pb2_grpc")

#: ``federation_pb2`` — ``CompactRecord``, ``TriggerCodes`` and the other
#: federation.v1 messages shared between the peer and core APIs.
fed_pb2: Any = importlib.import_module("federation_pb2")

#: ``agent_gateway_pb2`` — W3-v1 AgentGateway service messages (agent leg,
#: ADR-0018 variant (c)). Loaded LAZILY via module ``__getattr__`` (PEP
#: 562): the generated stubs are gitignored and environments that have not
#: re-run ``scripts/gen-proto.sh`` since W3 do not have the file — an eager
#: import here would break them at ``vesmaro`` import time. The bare
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
