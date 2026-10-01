"""Vesmaro — standalone memory & knowledge server for AI agents.

Formerly "mnemos". Productionised for the GCW agent family and Hermes Agent.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    # PyPI distribution name (pyproject [project].name). The standalone
    # server dist and legacy dist names keep resolving through the
    # dual-import period (ADR-0031).
    __version__ = _pkg_version("vesma")
except PackageNotFoundError:
    try:
        __version__ = _pkg_version("vesma-memory-server")
    except PackageNotFoundError:
        try:
            __version__ = _pkg_version("vesmaro")
        except PackageNotFoundError:
            try:
                __version__ = _pkg_version("mnemos-memory-server")
            except PackageNotFoundError:  # pragma: no cover — source checkout
                __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
