"""``vesma __complete`` — custom shell-completion engine (hidden command).

Typer's built-in completion scripts carry no candidate descriptions in any
shell (verified for the generated zsh/fish scripts), so ``vesma completion``
installs its own per-shell scripts (see :mod:`vesma.cli.completion`) that
all call THIS command. The engine introspects the LIVE typer/click command
tree — no hardcoded command tables — so future CLI restructuring is
automatically reflected in completions.

Argv contract
-------------

::

    vesma __complete WORD1 WORD2 ... N

* ``WORD1..WORDk`` — the words of the command line being completed,
  **after the program name** (the shell glue strips ``COMP_WORDS[0]`` /
  zsh ``words[1]`` / fish's first ``(commandline -opc)`` token).
* ``N`` — 0-based index of the word being completed, into the preceding
  WORDS. ``N`` may equal ``len(WORDS)``: the word being completed is a new,
  empty token (fish produces this shape when the cursor sits in an empty
  token). An empty fragment is passed as an explicit empty-string word by
  bash/zsh glue.
* The final argument is ALWAYS the index ``N`` (any other shape is treated
  as malformed and produces empty output — see below).

Output contract
---------------

* One candidate per line: ``value<TAB>description``. The description is the
  command's short help (first line), the option help, or empty (bare value
  when there is nothing to show).
* No headers, no Rich markup, empty output when nothing matches.
* Exit code 0 ALWAYS — a completion engine must never make the shell beep
  or print errors into the command line. Unexpected internal errors print a
  diagnostic to stderr (not stdout) and still exit 0.
* No database access, no network. Cost is one Python import + one click
  tree build (~tens of ms) — acceptable for a per-keystroke subprocess on
  the shells where it matters (zsh/fish render the descriptions; bash's
  readline cannot show descriptions at all and uses only the value column).

Completion semantics
--------------------

* Top-level commands, nested subcommands at any depth (``vesma auth token
  <TAB>``), option names (long form only) and option values (choice/enum
  values where derivable from the param type) are all completed.
* ``--flag=`` prefix form completes the value part.
* When the previous token is an option that expects a value, the current
  word completes that option's values.
* Hidden commands are excluded from candidates (this command hides itself)
  but ARE traversable by exact name, so their own subcommands complete.
"""

from __future__ import annotations

import sys
from typing import Annotated, Any

import typer

#: Commands/params with this attribute set are excluded from candidates.
_HIDDEN_ATTR = "hidden"


def get_completions(words: list[str], cword: int) -> list[tuple[str, str]]:
    """Compute ``(value, description)`` candidates for a completion request.

    Pure introspection — see the module docstring for the contract. Never
    raises: unexpected errors are swallowed into an empty candidate list
    (completion must not break the shell).
    """
    try:
        return _compute(words, cword)
    except Exception as exc:  # engine must never raise at the shell
        print(f"vesma __complete: internal error: {exc}", file=sys.stderr)
        return []


def _compute(words: list[str], cword: int) -> list[tuple[str, str]]:
    if cword > len(words):
        return []  # malformed glue: index past the word list (== len is the fish shape)
    root = _root_command()
    typed = words[:cword]
    fragment = words[cword] if cword < len(words) else ""

    current = root
    pending_value_param: Any = None  # option whose value the NEXT word would be

    for token in typed:
        if pending_value_param is not None:
            # This word is the value of the previous option — consume it.
            pending_value_param = None
            continue
        if token.startswith("-"):
            if "=" in token:
                pending_value_param = None  # --flag=value: value is inline
                continue
            param = _find_option(current, token)
            if param is not None and _option_takes_value(param):
                pending_value_param = param
            continue
        subcommands = _subcommands(current)
        if token in subcommands:
            # Hidden commands are traversable by exact name, just never listed.
            current = subcommands[token]
            continue
        # A positional/argument value — nothing further to descend into.

    # ── Build the candidate pool for the fragment ──
    candidates: list[tuple[str, str]] = []
    prefix = fragment
    if pending_value_param is not None:
        # Completing the value of the previous option token.
        candidates = [(c, "") for c in _param_choices(pending_value_param)]
    elif fragment.startswith("-") and "=" in fragment:
        # Completing the value of a --flag=<TAB> prefix form.
        name, _, value_fragment = fragment.partition("=")
        prefix = value_fragment
        param = _find_option(current, name)
        if param is not None:
            candidates = [(c, "") for c in _param_choices(param)]
    elif fragment.startswith("-"):
        candidates = _option_candidates(current)
    else:
        candidates = _subcommand_candidates(current)
        candidates += _argument_candidates(current)
        candidates += _option_candidates(current)

    matches = [(v, d) for v, d in candidates if v.startswith(prefix)]

    # Exact-match path for hidden commands: never listed by prefix, but when
    # the fragment IS the hidden name exactly, surface it (so plumbing like
    # this command stays tab-navigable).
    for name, sub in _subcommands(current).items():
        if _is_hidden(sub) and name == fragment:
            matches.append((name, _short_help(sub)))
    return matches


# ── Click-tree introspection ──────────────────────────────────────────────────


_ROOT_CACHE: Any = None


def _root_command() -> Any:
    """Build (and cache per-process) the click command for the live typer app."""
    global _ROOT_CACHE
    if _ROOT_CACHE is None:
        import typer.main

        from vesma.cli.main import app

        _ROOT_CACHE = typer.main.get_command(app)
    return _ROOT_CACHE


def _is_hidden(obj: Any) -> bool:
    return bool(getattr(obj, _HIDDEN_ATTR, False))


def _short_help(obj: Any) -> str:
    """First line of a command/param help text ('' when absent)."""
    text = getattr(obj, "help", None)
    if not text:
        return ""
    stripped = str(text).strip()
    return stripped.splitlines()[0].strip() if stripped else ""


def _subcommands(cmd: Any) -> dict[str, Any]:
    commands = getattr(cmd, "commands", None)
    return dict(commands) if commands else {}


def _subcommand_candidates(cmd: Any) -> list[tuple[str, str]]:
    """Visible subcommands with their short help (hidden ones excluded)."""
    out: list[tuple[str, str]] = []
    for name, sub in _subcommands(cmd).items():
        if _is_hidden(sub):
            continue
        out.append((name, _short_help(sub)))
    return out


def _option_takes_value(param: Any) -> bool:
    if getattr(param, "is_flag", False) or getattr(param, "count", False):
        return False
    # --on/--off style switches never consume the next word.
    return not getattr(param, "secondary_opts", None)


def _find_option(cmd: Any, token: str) -> Any | None:
    for param in getattr(cmd, "params", []) or []:
        opts = list(getattr(param, "opts", []) or []) + list(
            getattr(param, "secondary_opts", []) or []
        )
        if token in opts:
            return param
    return None


def _param_choices(param: Any) -> list[str]:
    """Choice/enum values derivable from a param type (click.Choice and the
    typer TyperChoice twin — duck-typed via the ``choices`` attribute)."""
    choices = getattr(getattr(param, "type", None), "choices", None)
    if not choices:
        return []
    return [str(c) for c in choices]


def _option_candidates(cmd: Any) -> list[tuple[str, str]]:
    """Long option names (primary + secondary) with their help text."""
    out: list[tuple[str, str]] = []
    for param in getattr(cmd, "params", []) or []:
        if not hasattr(param, "opts"):
            continue  # argument, not an option
        if _is_hidden(param):
            continue
        help_text = _short_help(param)
        for opt in list(getattr(param, "opts", []) or []) + list(
            getattr(param, "secondary_opts", []) or []
        ):
            if opt.startswith("--"):
                out.append((opt, help_text))
    return out


def _argument_candidates(cmd: Any) -> list[tuple[str, str]]:
    """Choice values for positional arguments whose type carries choices."""
    out: list[tuple[str, str]] = []
    for param in getattr(cmd, "params", []) or []:
        if hasattr(param, "opts"):
            continue  # option, not an argument
        for choice in _param_choices(param):
            out.append((choice, ""))
    return out


# ── CLI surface ───────────────────────────────────────────────────────────────


def complete(
    words: Annotated[
        list[str] | None,
        typer.Argument(
            help="Command-line words being completed, with the 0-based index of "
            "the word being completed as the FINAL argument. See the module "
            "docstring for the full argv contract.",
        ),
    ] = None,
) -> None:
    """Shell-completion engine backing the scripts installed by `vesma completion`.

    Not user-facing (hidden). Prints one ``value<TAB>description`` candidate
    per line; exits 0 always. No DB, no network.
    """
    argv = list(words or [])
    # Strict contract: the LAST argument is the 0-based completion index;
    # everything before it are the words. Anything else is malformed and
    # yields empty output (exit 0 — never break the shell).
    if not argv or not argv[-1].isdigit():
        return
    cword = int(argv[-1])
    word_list = argv[:-1]
    if cword < 0:
        return
    for value, description in get_completions(word_list, cword):
        if description:
            print(f"{value}\t{description}")
        else:
            print(value)
