"""vesmaro.service.board — minimal v1 skeleton, supervisor wave completes it.

The bundled manifest ``src/vesmaro/service/components/board.yaml``
declares this module as ``in_process.module`` and ``vesmaro.service.
board:health`` as the health callback; the module path must resolve and
import cleanly BEFORE wave W2 lands the real panel. Nothing here may pull
heavy dependencies — the supervisor will import it in-process.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BoardComponent:
    """Handle the supervisor receives from the entrypoint factory (CM §3.6)."""

    name: str = "board"
    kind: str = "in-process"


def create_component() -> BoardComponent:
    """Entrypoint factory required by the manifest (``() -> Component``)."""
    return BoardComponent()


def health() -> dict[str, str]:
    """Health callback (CM §3.7 signature ``() -> {state, detail?}``)."""
    return {"state": "healthy"}


__all__ = ["BoardComponent", "create_component", "health"]
