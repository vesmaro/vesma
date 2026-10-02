"""Agent MCP wiring — add ``mnemos/*`` tools to Copilot agent frontmatter.

This module extends the integration layer with **agent MCP wiring**: it
detects ``*.agent.md`` files in ``~/.copilot/agents/``, parses their YAML
frontmatter, and adds ``mnemos/*`` (wildcard) or individual
``mnemos/mnemos_*`` tool references to the ``tools:`` array.

Design constraints:

* **Only ``tools:`` is touched** — never ``model:``, ``model_tier:``,
  ``agents:``, or any other frontmatter key.
* **Agents with ``tool_profile:`` are skipped** — those are resolved by
  the Copilot installer, not by us. Mutating them would be overwritten on the
  next ``make install-all``.
* **Idempotent** — re-running does not duplicate ``mnemos/*`` entries.
* **Formatting preserved** — we use ``python-frontmatter`` which round-trips
  the YAML structure without mangling block/flow styles.
* **Safe** — the original file is only rewritten when the frontmatter
  actually changes. A backup is not written (the change is trivially
  reversible by removing the token), but ``--dry-run`` is supported.

Public API:

* :func:`detect_agents` — scan a directory, return parsed agent metadata.
* :func:`wire_agent` — wire a single agent file (wildcard or precise mode).
* :func:`wire_agents` — batch wire a list of agents.
* :func:`verify_agents` — aggregate wiring status for ``integration verify``.
* :class:`AgentInfo`, :class:`WireResult`, :class:`WireStatus` — data models.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import frontmatter

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "DEFAULT_AGENTS_DIR",
    "VESMARO_TOOLS",
    "VESMARO_WILDCARD",
    "AgentInfo",
    "AgentVerifySummary",
    "WireResult",
    "WireStatus",
    "detect_agents",
    "verify_agents",
    "wire_agent",
    "wire_agents",
]

# ── Constants ─────────────────────────────────────────────────────────────────

#: Default Copilot agents directory (``~/.copilot/agents``).
DEFAULT_AGENTS_DIR = Path.home() / ".copilot" / "agents"

#: Individual vesma MCP tool names used in **precise** mode.
#:
#: ``watch_*`` tools are admin-only (start/stop file watchers) and are
#: intentionally excluded from precise mode — they are not appropriate for
#: general agent wiring.
VESMARO_TOOLS: tuple[str, ...] = (
    "mnemos/mnemos_add",
    "mnemos/mnemos_search",
    "mnemos/mnemos_recall_context",
    "mnemos/mnemos_agent_recall",
    "mnemos/mnemos_save_context",
    "mnemos/mnemos_list_recent",
    "mnemos/mnemos_list_tags",
    "mnemos/mnemos_ingest_url",
    # ADR-0027 Ф3 (epic #308): the document ingest (born-quarantined
    # doc chunks, swept at completion) joins the wired tool set.
    "mnemos/mnemos_ingest_document",
    "mnemos/mnemos_stats",
    "mnemos/mnemos_auto_collect_status",
)

#: Wildcard token granting all vesma tools in one entry.
VESMARO_WILDCARD = "mnemos/*"


# ── Data models ───────────────────────────────────────────────────────────────


class WireStatus(StrEnum):
    """Outcome of a single ``wire_agent`` call."""

    WIRED = "wired"
    ALREADY_WIRED = "already_wired"
    SKIPPED_TOOL_PROFILE = "skipped_tool_profile"
    SKIPPED_NO_FRONTMATTER = "skipped_no_frontmatter"
    ERROR = "error"
    DRY_RUN = "dry_run"


@dataclass(frozen=True)
class AgentInfo:
    """Parsed metadata for a single agent file.

    Returned by :func:`detect_agents`. Immutable so callers can safely
    aggregate and sort without mutation risk.
    """

    path: Path
    name: str
    filename: str
    has_mnemos: bool
    uses_tool_profile: bool
    tools_count: int
    has_tools: bool


@dataclass
class WireResult:
    """Result of wiring a single agent file."""

    path: Path
    name: str
    status: WireStatus
    note: str = ""
    tools_added: list[str] = field(default_factory=list)


# ── Detection ─────────────────────────────────────────────────────────────────


def _has_mnemos_in_tools(tools: object) -> bool:
    """Return ``True`` if any element in ``tools`` starts with ``mnemos/``."""
    if not isinstance(tools, list):
        return False
    return any(isinstance(t, str) and t.startswith("mnemos/") for t in tools)


def detect_agents(agents_dir: Path | None = None) -> list[AgentInfo]:
    """Scan ``agents_dir`` for ``*.agent.md`` files and parse frontmatter.

    Args:
        agents_dir: Directory to scan. Defaults to
            :data:`DEFAULT_AGENTS_DIR` (``~/.copilot/agents``).

    Returns:
        Sorted list of :class:`AgentInfo` (by agent ``name``, fallback
        filename). Agents with unparseable frontmatter are included with
        ``name="<filename>"`` and ``has_tools=False`` so the caller can
        report them — they are never silently dropped.
    """
    directory = agents_dir or DEFAULT_AGENTS_DIR
    if not directory.is_dir():
        return []

    infos: list[AgentInfo] = []
    for path in sorted(directory.glob("*.agent.md")):
        info = _parse_agent(path)
        infos.append(info)
    return infos


def _quote_unquoted_yaml_value(value: str) -> str:
    """If ``value`` contains a colon and isn't quoted, wrap it in double quotes.

    Real-world agent files have descriptions like::

        description: (Copilot) Curator ... STUB mode: operates on file memory.

    The ``:`` in ``STUB mode:`` is parsed by YAML as a nested mapping key,
    which raises ``mapping values are not allowed in this context``. Quoting
    the whole scalar resolves the ambiguity.

    Args:
        value: The raw scalar text after ``key: `` (already stripped of the
            key and the separating colon).

    Returns:
        The value, double-quoted (with inner double quotes escaped) if it
        contained a colon and wasn't already quoted. Otherwise unchanged.
    """
    if not value:
        return value
    v = value.strip()
    # Already quoted (single or double) — leave as-is.
    if v.startswith('"') or v.startswith("'"):
        return value
    # No colon → no ambiguity, no need to quote.
    if ":" not in v:
        return value
    # Contains a colon — quote to prevent YAML mapping interpretation.
    escaped = v.replace('"', '\\"')
    return f'"{escaped}"'


# Matches a top-level frontmatter line: ``key: value`` (no nested indentation).
_KV_LINE_RE = re.compile(r"^(\s*)([a-zA-Z_][\w-]*)\s*:\s*(.*)$")


def _preprocess_yaml(content: str) -> str:
    """Quote unquoted scalar values containing ``:`` in YAML frontmatter.

    Splits the document on ``---`` delimiters, scans the frontmatter block
    for ``key: value`` lines where ``value`` contains a colon and isn't
    already quoted, and wraps those values in double quotes.

    This is a targeted fix for the ``mapping values are not allowed`` error
    caused by descriptions like ``STUB mode: operates on ...``. It does not
    attempt to be a general YAML preprocessor — block scalars, flow
    collections, and multi-line values are left untouched (they don't
    trigger the error in practice).

    Args:
        content: The full file text (with ``---`` frontmatter delimiters).

    Returns:
        The content with ambiguous scalar values quoted. If the document
        has no frontmatter (fewer than 3 ``---``-split parts), it is
        returned unchanged.
    """
    parts = content.split("---", 2)
    if len(parts) < 3:
        return content

    fm = parts[1]
    fixed_lines: list[str] = []
    for line in fm.split("\n"):
        m = _KV_LINE_RE.match(line)
        if m:
            indent, key, value = m.groups()
            quoted = _quote_unquoted_yaml_value(value)
            if quoted != value:
                line = f"{indent}{key}: {quoted}"
        fixed_lines.append(line)

    fixed_fm = "\n".join(fixed_lines)
    # Reassemble: leading text + --- + fixed frontmatter + --- + body.
    body = parts[2] if len(parts) > 2 else ""
    return f"{parts[0]}---{fixed_fm}---{body}"


def _regex_fallback_parse(content: str) -> dict[str, Any]:
    """Last-resort parser for malformed YAML frontmatter.

    Extracts ``name``, ``description``, ``tools``, ``tool_profile``,
    ``model``, and ``agents`` using simple regex. Handles inline lists,
    block lists, and unquoted scalars. This is only invoked when
    :func:`_preprocess_yaml` + :func:`frontmatter.loads` both fail — a
    rare edge case for truly broken files.

    Args:
        content: The full file text (with ``---`` frontmatter delimiters).

    Returns:
        A dict of parsed fields. Empty dict if no frontmatter is found.
    """
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}
    fm = parts[1]
    result: dict[str, Any] = {}

    # Extract simple scalars: key: value (quoted or unquoted).
    # Skip lines that look like block-list items (start with ``-``).
    for m in re.finditer(r"^([a-zA-Z_][\w-]*)\s*:\s*(.+?)\s*$", fm, re.MULTILINE):
        key, raw = m.groups()
        v = raw.strip()
        if v.startswith('"') and v.endswith('"'):
            v = v[1:-1].replace('\\"', '"')
        elif v.startswith("'") and v.endswith("'"):
            v = v[1:-1]
        # Skip inline-list values — handled separately below.
        if v.startswith("[") and v.endswith("]"):
            continue
        result[key] = v

    # Extract tools: [a, b, c] (inline list).
    tools_match = re.search(r"^tools\s*:\s*\[([^\]]*)\]", fm, re.MULTILINE)
    if tools_match:
        items = [
            t.strip().strip('"').strip("'") for t in tools_match.group(1).split(",") if t.strip()
        ]
        result["tools"] = items
    elif "tools" not in result:
        # Extract tools: block list (- a, - b).
        tools_match = re.search(r"^tools\s*:\s*\n((?:\s*-\s*.+\n?)+)", fm, re.MULTILINE)
        if tools_match:
            items = []
            for line_m in re.finditer(r"^\s*-\s*(.+?)\s*$", tools_match.group(1), re.MULTILINE):
                items.append(line_m.group(1).strip().strip('"').strip("'"))
            result["tools"] = items

    return result


def _parse_frontmatter(content: str) -> dict[str, Any]:
    """Parse YAML frontmatter with preprocessing and regex fallback.

    Tries (in order):

    1. :func:`_preprocess_yaml` + :func:`frontmatter.loads` — the normal
       path, with unquoted-colon values fixed first.
    2. :func:`_regex_fallback_parse` — a regex-based extractor for truly
       broken YAML.

    Args:
        content: The full file text.

    Returns:
        A metadata dict. Empty dict if both parsers fail.
    """
    try:
        preprocessed = _preprocess_yaml(content)
        post = frontmatter.loads(preprocessed)
        return dict(post.metadata)
    except Exception as exc:
        logger.warning(
            "frontmatter.loads failed even after preprocessing: %s; using regex fallback",
            exc,
        )
        return _regex_fallback_parse(content)


def _parse_agent(path: Path) -> AgentInfo:
    """Parse a single agent file into :class:`AgentInfo`.

    Robust against malformed frontmatter: if parsing fails, the agent is
    returned with safe defaults so it appears in reports but is skipped
    during wiring.
    """
    filename = path.name
    metadata = _parse_frontmatter(path.read_text(encoding="utf-8"))

    if not metadata:
        # Complete failure — return safe defaults.
        return AgentInfo(
            path=path,
            name=filename,
            filename=filename,
            has_mnemos=False,
            uses_tool_profile=False,
            tools_count=0,
            has_tools=False,
        )

    name = str(metadata.get("name", filename))
    has_tool_profile = "tool_profile" in metadata
    tools = metadata.get("tools")
    has_tools = isinstance(tools, list)
    tools_count = len(tools) if isinstance(tools, list) else 0
    has_mnemos = _has_mnemos_in_tools(tools)

    return AgentInfo(
        path=path,
        name=name,
        filename=filename,
        has_mnemos=has_mnemos,
        uses_tool_profile=has_tool_profile,
        tools_count=tools_count,
        has_tools=has_tools,
    )


# ── Wiring ────────────────────────────────────────────────────────────────────


def _build_tools_to_add(mode: str) -> list[str]:
    """Return the list of tool tokens to add for a given wiring mode.

    Args:
        mode: ``"wildcard"`` (default) or ``"precise"``.

    Raises:
        ValueError: if ``mode`` is not recognised.
    """
    if mode == "wildcard":
        return [VESMARO_WILDCARD]
    if mode == "precise":
        return list(VESMARO_TOOLS)
    raise ValueError(f"Unknown wiring mode: {mode!r} (expected 'wildcard' or 'precise')")


def _filter_missing(tools: list[str], existing: list[str]) -> list[str]:
    """Return tokens from ``tools`` not already present in ``existing``.

    Compares by exact string match. This keeps the operation idempotent:
    re-running with the same mode adds nothing.
    """
    existing_set = set(existing)
    return [t for t in tools if t not in existing_set]


def wire_agent(
    agent_path: Path,
    mode: str = "wildcard",
    *,
    dry_run: bool = False,
) -> WireResult:
    """Wire a single agent file with vesma MCP tools.

    Args:
        agent_path: Path to the ``*.agent.md`` file.
        mode: ``"wildcard"`` (add ``mnemos/*``) or ``"precise"`` (add
            individual ``mnemos/mnemos_*`` tokens).
        dry_run: If ``True``, report what would change without writing.

    Returns:
        :class:`WireResult` describing the outcome.

    The function is idempotent: an agent that already has the requested
    tokens is reported as ``ALREADY_WIRED`` and left untouched.
    """
    to_add = _build_tools_to_add(mode)

    raw = agent_path.read_text(encoding="utf-8")
    metadata = _parse_frontmatter(raw)

    if not metadata:
        # Malformed frontmatter — skip this agent gracefully rather than
        # failing the whole wiring batch. The error is logged inside
        # ``_parse_frontmatter`` so the user can fix the agent file, but
        # other agents still wire normally.
        return WireResult(
            path=agent_path,
            name=agent_path.name,
            status=WireStatus.SKIPPED_NO_FRONTMATTER,
            note="frontmatter parse failed (preprocessing + regex fallback exhausted)",
        )

    name = str(metadata.get("name", agent_path.name))

    # Skip agents that use tool_profile — the installer owns their tools.
    if "tool_profile" in metadata:
        return WireResult(
            path=agent_path,
            name=name,
            status=WireStatus.SKIPPED_TOOL_PROFILE,
            note="uses tool_profile (resolved by Copilot installer)",
        )

    tools = metadata.get("tools")
    existing_tools: list[str] = list(tools) if isinstance(tools, list) else []

    # Determine which tokens are actually missing.
    missing = _filter_missing(to_add, existing_tools)

    if not missing:
        return WireResult(
            path=agent_path,
            name=name,
            status=WireStatus.ALREADY_WIRED,
            note=f"already has {mode} vesma tools",
        )

    # Build the new tools list (preserve order, append missing at end).
    new_tools = [*existing_tools, *missing]

    if dry_run:
        return WireResult(
            path=agent_path,
            name=name,
            status=WireStatus.DRY_RUN,
            note=f"would add {len(missing)} tool(s): {', '.join(missing)}",
            tools_added=missing,
        )

    # Mutate metadata and write back. We re-parse the preprocessed content
    # (with colons already quoted) so that ``frontmatter.dump`` round-trips
    # cleanly without re-introducing the original YAML ambiguity.
    metadata["tools"] = new_tools
    try:
        preprocessed = _preprocess_yaml(raw)
        post = frontmatter.loads(preprocessed)
        post.metadata["tools"] = new_tools
        frontmatter.dump(post, agent_path)
    except Exception as exc:
        return WireResult(
            path=agent_path,
            name=name,
            status=WireStatus.ERROR,
            note=f"write failed: {exc}",
        )

    return WireResult(
        path=agent_path,
        name=name,
        status=WireStatus.WIRED,
        note=f"added {len(missing)} tool(s)",
        tools_added=missing,
    )


def wire_agents(
    agents: Sequence[AgentInfo],
    mode: str = "wildcard",
    *,
    dry_run: bool = False,
) -> list[WireResult]:
    """Wire a batch of agents, returning one result per agent.

    Agents that use ``tool_profile`` or are already wired are skipped
    (reported, not mutated). Agents that produced an error are included
    in the result list so the caller can report them.
    """
    results: list[WireResult] = []
    for agent in agents:
        result = wire_agent(agent.path, mode=mode, dry_run=dry_run)
        results.append(result)
    return results


# ── Verification ──────────────────────────────────────────────────────────────


@dataclass
class AgentVerifySummary:
    """Aggregate wiring status for ``integration verify`` and ``doctor``."""

    total: int = 0
    wired: int = 0
    already_wired: int = 0
    skipped_tool_profile: int = 0
    unwired: int = 0
    errors: int = 0
    unwired_names: list[str] = field(default_factory=list)

    @property
    def all_wired(self) -> bool:
        """``True`` if every agent is wired or legitimately skipped."""
        return self.total > 0 and self.unwired == 0 and self.errors == 0


def verify_agents(agents_dir: Path | None = None) -> AgentVerifySummary:
    """Aggregate wiring status across all agents in ``agents_dir``.

    Used by ``vesma integration verify`` (agents section) and
    ``vesma doctor`` (agent wiring check).
    """
    infos = detect_agents(agents_dir)
    summary = AgentVerifySummary(total=len(infos))

    for info in infos:
        if info.uses_tool_profile:
            summary.skipped_tool_profile += 1
        elif info.has_mnemos:
            summary.wired += 1
        else:
            summary.unwired += 1
            summary.unwired_names.append(info.name)

    return summary
