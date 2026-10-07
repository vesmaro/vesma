"""UX-1/UX-3 hotfix wave — `-h` everywhere + help depth on the full tree.

Two guarantees, both enforced against the LIVE click tree built from the
typer app (no hardcoded command list — a new command is covered automatically):

* ``-h`` AND ``--help`` exit 0 with a usage block on every visible command
  and group (the UX-1 defect: ``-h`` was rejected on all 30 top-level
  commands with exit 2);
* every visible node carries a non-empty one-line short help plus a full
  body (what/how/why — the UX-3 bar, mechanically: body >= 80 chars), and
  the root help carries the ``Tip: add -h to any command for help.`` line.

Help options are processed during click's parse phase, so invoking a
command with ``-h`` never executes its body — the sweep is side-effect
free by construction.
"""

from __future__ import annotations

from typing import Any

import click
import pytest
from click.testing import CliRunner
from typer.main import get_command

from vesma.cli.main import app

runner = CliRunner()


def _visible_commands(cmd: Any, path: str = "") -> list[str]:
    """Every visible command path in the tree, in registration order."""
    subs = dict(getattr(cmd, "commands", None) or {})
    out: list[str] = []
    for name, sub in subs.items():
        if getattr(sub, "hidden", False):
            continue
        p = f"{path} {name}".strip()
        out.append(p)
        out.extend(_visible_commands(sub, p))
    return out


#: The click tree built ONCE at collection — the sweep below invokes it
#: ~190 times and must not rebuild it per invocation.
CLICK_ROOT: click.Command = get_command(app)

#: Every visible command path, collected once alongside the tree.
COMMAND_PATHS: list[str] = _visible_commands(CLICK_ROOT)


def test_tree_has_expected_breadth() -> None:
    """Guard the sweep itself: the tree is the full 30-command surface, not a
    truncated build (if this drops, the sweep is silently covering less)."""
    top_level = {p for p in COMMAND_PATHS if " " not in p}
    assert len(top_level) >= 30, f"top-level commands: {sorted(top_level)}"
    assert len(COMMAND_PATHS) >= 90, f"tree nodes: {len(COMMAND_PATHS)}"


@pytest.mark.parametrize("help_flag", ["-h", "--help"])
@pytest.mark.parametrize("command_path", COMMAND_PATHS)
def test_help_flag_works_on_every_command(command_path: str, help_flag: str) -> None:
    """UX-1: exit 0 + usage output for -h AND --help at EVERY level."""
    result = runner.invoke(CLICK_ROOT, [*command_path.split(" "), help_flag])
    assert result.exit_code == 0, f"`vesma {command_path} {help_flag}` exited {result.exit_code}"
    assert "Usage:" in result.output, f"`vesma {command_path} {help_flag}` printed no usage"


def test_minus_h_fragment_reaches_complete_engine() -> None:
    """UX-1 regression: the __complete engine receives -h/--help as DATA —
    click must not intercept them as the engine's own help option (pinned
    via help_option_names=[] on the hidden command)."""
    for fragment in ("-h", "--help"):
        result = runner.invoke(CLICK_ROOT, ["__complete", "search", fragment, "1"])
        assert result.exit_code == 0
        assert "Usage:" not in result.output, (
            f"__complete intercepted {fragment!r} as a help option — "
            "option-name completion is broken"
        )


def test_every_node_has_short_help_and_body() -> None:
    """UX-3: non-empty one-line short help + a real body on every node.

    The short help is what parent command lists show; the body is the
    what/how/why. The 80-char floor is the mechanical bar the hotfix wave
    established (58 nodes were one-liners before it)."""
    problems: list[str] = []
    for path in COMMAND_PATHS:
        cmd: Any = CLICK_ROOT
        for part in path.split(" "):
            cmd = cmd.commands[part]
        help_text = (getattr(cmd, "help", None) or "").strip()
        if not help_text:
            problems.append(f"{path}: EMPTY help")
            continue
        lines = help_text.splitlines()
        body = " ".join(lines[1:]).strip()
        if len(body) < 80:
            problems.append(f"{path}: body too thin ({len(body)} chars)")
    assert not problems, "help depth violations:\n" + "\n".join(problems)


def test_root_help_carries_minus_h_tip() -> None:
    """UX-4: the discoverability line under the Commands table."""
    result = runner.invoke(CLICK_ROOT, ["--help"])
    assert result.exit_code == 0
    assert "Tip: add -h to any command for help." in result.output
