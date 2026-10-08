"""Vesma — standalone memory & knowledge server for AI agents."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

try:
    # PyPI distribution name (pyproject [project].name). 6.0 is the clean
    # sheet: ``vesma`` is the only distribution this package resolves.
    __version__ = _pkg_version("vesma")
except PackageNotFoundError:  # pragma: no cover — source checkout
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
