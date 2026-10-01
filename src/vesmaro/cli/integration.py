"""Vesma integration layer — deploy instructions/skills/prompts to agent harnesses.

This module is the engine behind the ``vesma integration *`` CLI
subcommands. It:

* Detects installed agent harnesses (Copilot, generic Copilot, Cursor) via
  ``integrations/targets.yaml``.
* Deploys the shipped pack (``integrations/{instructions,skills,prompts}/``)
  into each detected harness, stamping every file with a version header so
  later runs can detect stale files and safely uninstall only our own.
* Injects the always-on behavioral pack (``agents_md`` kind) as a stamped
  BEGIN/END block INTO the user's ``AGENTS.md``-standard file (targets
  ``agents``, ``zcode``, ``opencode``, ``codex``, ``claude-code``) — user
  content around the block is never touched.
* Registers the MCP server per target: additive JSON merges that preserve
  every other key (zcode, agents, cursor, claude-code, windsurf,
  opencode), a surgical TOML table merge for the Codex config (stdlib
  ``tomllib`` validation, byte-preserving outside the managed table), a
  TypeScript bridge for Pi, and the historical ``mcp-setup.sh`` fallback.
* Verifies deployed files against the current package version.
* Updates stale files in place.
* Uninstalls only stamped files — never user-created content.
* Deploys the canon JSON Schemas (``schemas`` kind) BYTE-IDENTICAL —
  never inline-stamped, because a schema file must stay valid JSON for
  schema validators; ownership is recorded in a sidecar manifest.

The version stamp is a Markdown HTML comment on the first non-shebang line::

    <!-- vesma-integration: v5.2.0 -->

This is invisible in rendered Markdown but trivially greppable. Stamps from
the previous brand generation (``mnemos-integration``) are still RECOGNIZED
for ownership detection during the migration window (2-3 releases); the
first deploy/update re-stamps such files to the current generation, and
``verify`` reports them with the dedicated ``OLD_STAMP`` status.

For the ``schemas`` kind the same discipline lives in a stamped JSON
sidecar manifest (``mnemos-schemas.manifest.json``) written NEXT TO the
deployed schema files: it carries the deploy version, the pin provenance
and the sha256 of every deployed schema file.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import yaml

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "AGENTS_MD_BLOCK_RE",
    "DEFAULT_PRECEDENCE",
    "ENGINE_MANIFEST_NAME",
    "PRECEDENCE_MODES_KNOWN",
    "SCHEMAS_MANIFEST_NAME",
    "SCHEMAS_SOURCE_PIN",
    "ArtefactKind",
    "DeployResult",
    "DeployStatus",
    "IntegrationManager",
    "Target",
    "TargetsConfig",
    "VerifyResult",
    "has_legacy_stamp",
    "load_engine_manifest",
    "load_targets",
    "read_agents_md_version",
    "render_agents_md_block",
    "schemas_manifest",
    "strip_agents_md_block",
    "validate_precedence",
]

# ── Constants ─────────────────────────────────────────────────────────────────

#: The stamp injected into every deployed file (first useful line).
#: DUAL-PATTERN (stamp migration, ArchCom 2026-10-01): both the current
#: ``vesma-integration`` generation and the legacy ``mnemos-integration``
#: generation are recognized, so ownership detection keeps working across
#: the migration window. New stamps are only ever written in the current
#: generation; the first deploy/update re-stamps legacy files.
STAMP_PATTERN = re.compile(r"<!--\s*(?:vesma-integration|mnemos-integration):\s*v(\S+?)\s*-->")

#: Legacy-generation stamps only (``mnemos-integration``). Used to report
#: the dedicated ``OLD_STAMP`` verify status — a file we own whose stamp
#: predates the current brand generation.
STAMP_LEGACY_PATTERN = re.compile(r"<!--\s*mnemos-integration:\s*v(\S+?)\s*-->")

#: Paired block markers for the ``agents_md`` deployment kind. Unlike file
#: stamps, an ``agents_md`` deployment lives INSIDE a user-owned file (an
#: ``AGENTS.md``-standard standing-instructions file), so the injected region
#: is wrapped in paired BEGIN/END comments and only that region is ever
#: mutated — user content around it is preserved byte-for-byte.
AGENTS_MD_BLOCK_RE = re.compile(
    r"<!--\s*(?:vesma|mnemos):integration:v(?P<version>\S+?)\s+BEGIN\s*-->\r?\n"
    r"(?P<body>.*?)"
    r"<!--\s*(?:vesma|mnemos):integration:v(?P<end_version>\S+?)\s+END\s*-->\r?\n?",
    re.DOTALL,
)

#: Artefact sub-directories inside the shipped ``integrations/`` pack.
ARTEFACT_DIRS: tuple[str, ...] = (
    "instructions",
    "skills",
    "prompts",
    "extensions",
    "schemas",
    "agents_md",
)

#: File extensions considered deployable (skip ``.gitkeep`` and READMEs).
DEPLOYABLE_SUFFIXES: tuple[str, ...] = (".md", ".yaml", ".yml", ".json", ".txt", ".ts")

#: Suffixes whose comment syntax requires a ``//`` prefix for the stamp
#: (TypeScript/JavaScript integration artefacts, e.g. the Pi bridge).
LINE_COMMENT_SUFFIXES: frozenset[str] = frozenset({".ts", ".js", ".mjs", ".cjs"})

#: Sidecar manifest written next to deployed schema files (kind
#: ``schemas``). JSON Schema files are deployed BYTE-IDENTICAL to the
#: canon pin — an inline ``<!-- -->`` stamp would make them invalid JSON —
#: so the version stamp, provenance and per-file checksums live here
#: instead. The manifest is a stamped file in the ordinary sense (its
#: raw text carries the regular version stamp — either brand generation
#: is recognized), which keeps verify/uninstall ownership detection
#: uniform across kinds.
SCHEMAS_MANIFEST_NAME = "vesma-schemas.manifest.json"
#: Manifest file name used by pre-rebrand deployments. Recognized for
#: ownership detection during the stamp-migration window; the first
#: deploy/update writes the current name and removes the legacy file.
LEGACY_SCHEMAS_MANIFEST_NAME = "mnemos-schemas.manifest.json"

#: Registered MCP server key per config format. Brand-primary key is
#: ``vesma``; the legacy ``mnemos`` key is recognized (and migrated on
#: register, removed on unregister) during the migration window.
MCP_SERVER_KEY = "vesma"
MCP_LEGACY_SERVER_KEY = "mnemos"

#: Codex (OpenAI Codex CLI) config root table for MCP servers —
#: ``[mcp_servers.<name>]`` in ``~/.codex/config.toml``. Handled by the
#: TOML-merge engine (ADR-0024 P-B.3), not the JSON one.
CODEX_MCP_ROOT = "mcp_servers"

#: A TOML table header line: ``[a.b]``, ``[[a.b]]``, optionally quoted and
#: commented. Group ``name`` carries the raw path between the brackets
#: (whitespace-trimmed; quotes are stripped by the caller when comparing).
TOML_TABLE_HEADER_RE = re.compile(r"^\s*\[\s*\[?(?P<name>[^\[\]]+?)\]?\s*\]\s*(?:#.*)?$")

#: Memory-switch precedence modes (ADR-0034, contract MS-0).
#:
#: ``overlay+mirror`` is the ONLY enforced mode: the deployed vesma pack
#: overlays the harness's own canon (local instructions win on divergence)
#: while memory writes mirror into the vesma store. ``replace`` and ``off``
#: are RECOGNIZED so targets.yaml / engine manifests can already name them,
#: but they are not enforced yet — they arrive with the spec-repo MS-1 wave
#: (honest staging per the ADR-0034 contract). An unknown mode is rejected
#: at parse time (fail-closed, never silently defaulted).
PRECEDENCE_OVERLAY_MIRROR = "overlay+mirror"
PRECEDENCE_REPLACE = "replace"
PRECEDENCE_OFF = "off"
#: The default and the only currently enforced mode.
DEFAULT_PRECEDENCE = PRECEDENCE_OVERLAY_MIRROR
#: Recognized-but-not-yet-enforced modes (documented for MS-1).
PRECEDENCE_MODES_ENFORCED: frozenset[str] = frozenset({PRECEDENCE_OVERLAY_MIRROR})
#: Every value ``precedence:`` may legally carry today.
PRECEDENCE_MODES_KNOWN: frozenset[str] = frozenset(
    {PRECEDENCE_OVERLAY_MIRROR, PRECEDENCE_REPLACE, PRECEDENCE_OFF}
)

#: Shipped engine manifest (memory-switch MS-0, ADR-0034) — generalized from
#: the Hermes ``plugin.yaml``. A PLAIN pack file: it describes the ENGINE (not
#: a per-harness artefact), so it ships inside the wheel (the ``integrations/``
#: force-include) but is deliberately NOT deployed by the kind loop — consumers
#: load it from the installed pack root via :func:`load_engine_manifest`.
ENGINE_MANIFEST_NAME = "engine-manifest.yaml"

#: Provenance of the vendored canon schemas (ADR-0003 pin protocol).
#: Bumping the pin changes these literals, the vendored files under
#: ``integrations/schemas/`` and their sha256 table in
#: ``integrations/schemas/README.md`` — in the same change.
SCHEMAS_SOURCE_PIN = {
    "repo": "github.com/vesmaro/vesmaro-canon",
    "tag": "canon-v1.0.0",
    "commit": "d4e998089acde88a9d57551fa71cfa2ef3c23fdf",
    # Per-file sha256 of the pinned schema content. Kept as a nested dict
    # with line breaks: file name + 64-hex digest cannot fit the 100-char
    # ruff line budget otherwise.
    "sha256": {
        "envelope.schema.json": (
            "3b7a57bcda64eb2758460e025ef1756236a03a481d0c4ae8c39cf206ba583f8f"
        ),
        "checkpoint.schema.json": (
            "e71ad139e4e21ef911c2ab3278df1b0ccbe6c94a9fea8c3db056803261808161"
        ),
        "task.schema.json": ("a08ad9fc718e1be977bd337bbb681a87e11403df1ebe5b024f4a84857a834b0c"),
        "decision.schema.json": (
            "1a832e8c283c3e076b7f1252a28184544285aa5d32f0a872d30c313067047a3a"
        ),
        "report.schema.json": ("0936b9d147dcb5c5cc1740a9ac32648dae4127b58e47d97fa273906e0595a51a"),
    },
}


class ArtefactKind(StrEnum):
    """Logical kind of an integration artefact — maps to a deploy key."""

    INSTRUCTIONS = "instructions"
    SKILLS = "skills"
    PROMPTS = "prompts"
    EXTENSION = "extensions"
    #: Canon JSON Schemas, deployed BYTE-IDENTICAL (no inline stamp —
    #: schema validators require valid JSON). Ownership lives in the
    #: stamped sidecar manifest (``SCHEMAS_MANIFEST_NAME``) written next
    #: to the files; the deploy-map value is the schemas directory.
    SCHEMAS = "schemas"
    #: Always-on behavioral pack injected as a stamped block INTO a shared
    #: user-owned ``AGENTS.md``-standard file. The deploy-map value is the
    #: FILE to inject into (not a directory) — handled by dedicated block
    #: logic, never by the file-copy path.
    AGENTS_MD = "agents_md"


class DeployStatus(StrEnum):
    """Per-file outcome of a deploy/verify/update operation."""

    DEPLOYED = "deployed"
    UPDATED = "updated"
    CURRENT = "current"
    STALE = "stale"
    MISSING = "missing"
    SKIPPED = "skipped"
    #: We own the file (stamp/block present) but it carries the legacy
    #: ``mnemos-integration`` generation marker. Safe, but the next
    #: deploy/update re-stamps it to the current generation.
    OLD_STAMP = "old-stamp"


# ── Pack-root resolution ─────────────────────────────────────────────────────


def _resolve_pack_targets() -> Path:
    """Find the shipped ``targets.yaml`` across install layouts.

    Tries the source-tree path first (editable installs / repo checkout),
    then falls back to the installed-package location via
    ``importlib.resources`` (wheels that ship ``mnemos/integrations/``).
    """
    # 1. Source-tree layout: src/mnemos/cli/integration.py → up 4 levels.
    source_candidate = (
        Path(__file__).resolve().parent.parent.parent.parent / "integrations" / "targets.yaml"
    )
    if source_candidate.is_file():
        return source_candidate

    # 2. Installed-package layout via importlib.resources.
    try:
        from importlib.resources import files

        pack_targets = files("vesmaro") / "integrations" / "targets.yaml"
        if pack_targets.is_file():
            return Path(str(pack_targets))
    except (ImportError, ModuleNotFoundError, FileNotFoundError):
        pass

    # 3. Upward search for an integrations/ sibling of any parent.
    here = Path(__file__).resolve()
    for parent in here.parents:
        maybe = parent / "integrations" / "targets.yaml"
        if maybe.is_file():
            return maybe

    # 4. Last resort: CWD (used in tests).
    return Path.cwd() / "integrations" / "targets.yaml"


# ── Config model ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Target:
    """A single harness target (e.g. ``copilot``, ``cursor``)."""

    name: str
    detect_paths: tuple[Path, ...]
    deploy_map: dict[str, Path]
    format: str = "copy"
    #: ``nested`` stores each skill as ``<skills-dir>/<name>/SKILL.md``
    #: instead of the flat ``<name>.md`` pack layout (zcode, agents).
    layout: str = "flat"
    #: Memory-switch precedence mode (ADR-0034 MS-0). Default
    #: ``overlay+mirror`` (the only enforced mode); ``replace`` / ``off``
    #: are recognized-but-not-yet-enforced values that arrive with MS-1.
    precedence: str = DEFAULT_PRECEDENCE
    #: Config file to register the MCP server in (JSON merge), if the
    #: target declares one. ``None`` → fall back to ``mcp-setup.sh``.
    mcp_config: Path | None = None
    mcp_format: str | None = None

    def is_detected(self) -> bool:
        """A target is detected if ANY of its detect paths exists."""
        return any(p.exists() for p in self.detect_paths)

    def dest_for(self, kind: str, rel: Path) -> Path:
        """Map a pack-relative artefact path to its deploy destination.

        ``layout: nested`` targets rewrite a flat ``mnemos-recall.md`` skill
        into ``mnemos-recall/SKILL.md``. Pack sources that are ALREADY
        directory-shaped (``<name>/SKILL.md``) pass through unchanged, so
        both pack layouts work with both target layouts.
        """
        base = self.deploy_map[kind]
        if self.layout == "nested" and kind == "skills" and rel.name != "SKILL.md":
            return base / rel.parent / rel.stem / "SKILL.md"
        return base / rel


@dataclass(frozen=True)
class TargetsConfig:
    """Parsed ``targets.yaml`` — immutable collection of targets."""

    targets: tuple[Target, ...]

    def get(self, name: str) -> Target | None:
        return next((t for t in self.targets if t.name == name), None)

    def detected(self) -> tuple[Target, ...]:
        return tuple(t for t in self.targets if t.is_detected())


def _expand(path: str, home: Path | None = None) -> Path:
    """Expand ``~`` in a path, optionally against an alternate home.

    ``home`` lets ``vesma integration setup --home <dir>`` deploy into a
    foreign environment (another container's home, a dotfiles repo, …)
    without rewriting targets.yaml.
    """
    if path.startswith("~") and home is not None:
        rest = path[1:].lstrip("/")
        return home / rest if rest else home
    return Path(path).expanduser()


def validate_precedence(name: str, value: object) -> str:
    """Validate a ``precedence:`` value (ADR-0034 MS-0); return it as ``str``.

    Unknown modes raise ``ValueError`` (fail-closed — a typo must never
    silently deploy under the default mode). ``replace`` / ``off`` pass
    validation but are not enforced yet (see ``PRECEDENCE_MODES_KNOWN``).
    """
    if not isinstance(value, str) or value not in PRECEDENCE_MODES_KNOWN:
        known = ", ".join(sorted(PRECEDENCE_MODES_KNOWN))
        raise ValueError(
            f"targets.yaml: target '{name}'.precedence must be one of: {known} (got {value!r})"
        )
    if value not in PRECEDENCE_MODES_ENFORCED:
        # Recognized but honest: the mode is accepted so configs can be
        # written ahead of MS-1, with a loud log line.
        logger.warning(
            "target %r: precedence %r is recognized but not enforced yet "
            "(arrives with the memory-switch MS-1 wave, ADR-0034) — "
            "deploying under the default %r behavior",
            name,
            value,
            DEFAULT_PRECEDENCE,
        )
    return value


def load_targets(config_path: Path | None = None, home: Path | None = None) -> TargetsConfig:
    """Load and parse ``integrations/targets.yaml``.

    Args:
        config_path: Explicit path to a ``targets.yaml``. When ``None`` the
            file shipped inside the package tree is used. Resolution order:

            1. Source-tree layout (``src/mnemos/.../integrations/targets.yaml``)
               — works for editable / repo checkouts.
            2. Installed-package layout via ``importlib.resources`` — works
               for wheels that ship ``mnemos/integrations/targets.yaml``
               (added in v2.0.1 via ``[tool.hatch.build.targets.wheel.force-include]``).
            3. Upward search for an ``integrations/`` sibling of any parent.
            4. CWD fallback (used in tests).
        home: Alternate home directory for ``~`` expansion (see ``_expand``).

    Raises:
        FileNotFoundError: if the config file does not exist.
        ValueError: if the YAML is structurally invalid.
    """
    if config_path is None:
        config_path = _resolve_pack_targets()

    if not config_path.exists():
        raise FileNotFoundError(f"targets.yaml not found: {config_path}")

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or "targets" not in raw:
        raise ValueError(
            f"targets.yaml: expected top-level 'targets' key, got {type(raw).__name__}"
        )

    targets_raw = raw["targets"]
    if not isinstance(targets_raw, dict):
        raise ValueError(
            f"targets.yaml: 'targets' must be a mapping, got {type(targets_raw).__name__}"
        )

    targets: list[Target] = []
    for name, spec in targets_raw.items():
        if not isinstance(spec, dict):
            raise ValueError(f"targets.yaml: target '{name}' must be a mapping")

        detect_raw = spec.get("detect", [])
        if not isinstance(detect_raw, list):
            raise ValueError(f"targets.yaml: target '{name}'.detect must be a list")
        detect_paths = tuple(
            _expand(d["path"], home) for d in detect_raw if isinstance(d, dict) and "path" in d
        )

        deploy_raw = spec.get("deploy", {})
        if not isinstance(deploy_raw, dict):
            raise ValueError(f"targets.yaml: target '{name}'.deploy must be a mapping")
        deploy_map = {
            kind: _expand(path, home) for kind, path in deploy_raw.items() if isinstance(path, str)
        }
        agents_md_path = deploy_raw.get(ArtefactKind.AGENTS_MD.value)
        if isinstance(agents_md_path, str) and agents_md_path.rstrip().endswith("/"):
            raise ValueError(
                f"targets.yaml: target '{name}'.deploy.agents_md must be a FILE path "
                "(the stamped block inside it), not a directory"
            )

        mcp_raw = spec.get("mcp")
        if mcp_raw is not None and not isinstance(mcp_raw, dict):
            raise ValueError(f"targets.yaml: target '{name}'.mcp must be a mapping")
        mcp_config: Path | None = None
        mcp_format: str | None = None
        if isinstance(mcp_raw, dict) and isinstance(mcp_raw.get("config"), str):
            mcp_config = _expand(mcp_raw["config"], home)
            mcp_format = str(mcp_raw.get("format", "agents")) if mcp_raw.get("format") else None

        fmt = str(spec.get("format", "copy"))
        precedence = validate_precedence(name, spec.get("precedence", DEFAULT_PRECEDENCE))
        targets.append(
            Target(
                name=name,
                detect_paths=detect_paths,
                deploy_map=deploy_map,
                format=fmt,
                layout=str(spec.get("layout", "flat")),
                precedence=precedence,
                mcp_config=mcp_config,
                mcp_format=mcp_format,
            )
        )

    return TargetsConfig(targets=tuple(targets))


# ── Version stamping ──────────────────────────────────────────────────────────


def make_stamp(version: str) -> str:
    """Build the stamp comment for a given version (current generation)."""
    return f"<!-- vesma-integration: v{version} -->"


def stamp_content(content: str, version: str, *, line_comment: bool = False) -> str:
    """Inject or replace the version stamp in file content.

    The stamp is placed on the first line after any shebang (``#!``) or
    YAML front-matter block (``--- ... ---``). It MUST sit after the
    front-matter so it never breaks parsers that require the file to
    start with ``---`` (skill loaders, front-matter extractors).

    If a stamp already exists — wherever it is — it is removed first
    and re-inserted at the correct position. This self-heals files
    stamped by older releases that placed the stamp *before* the
    front-matter delimiter, which broke skill loading (``description
    is required``) by hiding the front-matter from parsers.

    ``line_comment=True`` prefixes the stamp with ``//`` so it stays a valid
    comment in TypeScript/JavaScript artefacts (the Pi MCP bridge) — the
    underlying stamp text is identical, only the comment syntax adapts.
    """
    stamp = make_stamp(version)
    prefix = "// " if line_comment else ""

    # Strip every existing stamp line first, wherever it sits. A stamp
    # before the opening ``---`` is the bug we are healing; a stamp after
    # front-matter is the correct case we are refreshing. Either way the
    # canonical position is recomputed below so the result is identical.
    lines = content.splitlines(keepends=True)
    cleaned = [line for line in lines if not STAMP_PATTERN.search(line)]

    # Find the insertion point: after a leading shebang and/or front-matter.
    insert_at = 0
    in_frontmatter = False
    for i, line in enumerate(cleaned):
        stripped = line.strip()
        if stripped.startswith("#!"):
            insert_at = i + 1
            continue
        if stripped == "---":
            if not in_frontmatter:
                # Opening front-matter delimiter — skip the whole block.
                in_frontmatter = True
                continue
            # Closing delimiter — insert after this line.
            in_frontmatter = False
            insert_at = i + 1
            continue
        if in_frontmatter:
            continue
        break

    cleaned.insert(insert_at, prefix + stamp + "\n")
    return "".join(cleaned)


def read_stamp(content: str) -> str | None:
    """Extract the version from a stamped file, or ``None`` if unstamped.

    Recognizes BOTH stamp generations (``vesma-integration`` and the
    legacy ``mnemos-integration``) — see :data:`STAMP_PATTERN`.
    """
    match = STAMP_PATTERN.search(content)
    return match.group(1) if match else None


def has_legacy_stamp(content: str) -> bool:
    """True when the content carries only the legacy ``mnemos-integration`` stamp.

    Used by ``verify`` to report the dedicated ``OLD_STAMP`` status for
    files we own whose marker predates the current brand generation.
    """
    return STAMP_LEGACY_PATTERN.search(content) is not None


# ── Schemas kind (byte-identical deployment + sidecar manifest) ───────────────


def schemas_manifest(version: str, checksums: dict[str, str]) -> str:
    """Render the stamped sidecar manifest for the ``schemas`` kind.

    The schema files themselves deploy byte-identical to the canon pin (a
    JSON Schema must stay valid JSON for schema validators — an inline
    HTML-comment stamp would break parsing), so the ordinary file stamp
    lives on this sidecar instead: its RAW text carries the regular
    ``mnemos-integration`` marker, which keeps ownership detection and
    uninstall discipline uniform across kinds, while ``json.loads`` of the
    body still works for any consumer that reads the manifest itself.

    The trailing ``\\n`` plus the marker line after the closing brace keep
    the payload parseable via the standard trick of ignoring trailing
    non-JSON content (``JSONDecoder.raw_decode``).
    """
    ordered = dict(sorted(checksums.items()))
    payload = {
        "schema_version": 1,
        "kind": ArtefactKind.SCHEMAS.value,
        "source": dict(SCHEMAS_SOURCE_PIN),
        "deployed_version": version,
        "files": ordered,
    }
    body = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)
    return f"{make_stamp(version)}\n{body}\n"


# ── AGENTS.md block engine ────────────────────────────────────────────────────


def _read_user_text(dest: Path) -> str:
    """Read a user-owned text file verbatim (no newline translation).

    ``open(..., newline="")`` keeps ``\r\n`` sequences intact so the
    never-clobber guarantee holds for CRLF files too. Raises
    ``UnicodeDecodeError`` for non-UTF-8 content — callers treat that as
    "not ours, skip" rather than crashing the run.
    """
    with dest.open("r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _atomic_write_text(dest: Path, text: str) -> None:
    """Write ``text`` verbatim (no newline translation), atomically.

    The payload lands in a same-directory temp file first and is moved
    into place with ``os.replace`` — a crash mid-write can never truncate
    the user's standing-instructions file.
    """
    tmp = dest.with_name(dest.name + ".mnemos-tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            tmp.unlink()


def render_agents_md_block(content: str, version: str) -> str:
    """Wrap ``content`` in the stamped BEGIN/END block markers.

    Markers are emitted in the CURRENT brand generation
    (``vesma:integration``); blocks from the legacy ``mnemos:integration``
    generation are still matched by :data:`AGENTS_MD_BLOCK_RE` and replaced
    in place on the next deploy/update. The result always ends with a
    newline so appending further user content (or a future block refresh)
    never glues onto the END marker.
    """
    body = content if content.endswith("\n") else content + "\n"
    return (
        f"<!-- vesma:integration:v{version} BEGIN -->\n"
        f"{body}"
        f"<!-- vesma:integration:v{version} END -->\n"
    )


def strip_agents_md_block(content: str) -> tuple[str, str | None]:
    """Remove every paired mnemos block from ``content``.

    Returns ``(cleaned_content, version_of_first_removed_block)``. Only
    PAIRED blocks (BEGIN … END, any versions) are removed — an unpaired
    marker (e.g. half a block a user edited away) is left untouched, since
    removing text without its terminator could eat user content. Note the
    scope: EVERY paired mnemos-marked block is removed, including one a
    user has quoted inside their own notes — the markers are treated as
    owned by vesmaro.

    Everything outside the removed regions is preserved byte-for-byte.
    """
    match = AGENTS_MD_BLOCK_RE.search(content)
    if match is None:
        return content, None
    version = match.group("version")
    cleaned = AGENTS_MD_BLOCK_RE.sub("", content)
    return cleaned, version


def read_agents_md_version(content: str) -> str | None:
    """Extract the version from the first mnemos block, or ``None``."""
    match = AGENTS_MD_BLOCK_RE.search(content)
    return match.group("version") if match else None


# ── Codex TOML merge engine (ADR-0024 P-B.3) ──────────────────────────────────
#
# The Codex CLI config (``~/.codex/config.toml``) is TOML, and the stdlib
# ``tomllib`` (3.11+) is deliberately read-only — there is no stdlib TOML
# writer and no third-party dependency is allowed. The engine therefore
# merges at the TEXT level: the managed ``[mcp_servers.<key>]`` region
# (its header line through the line before the next table header) is
# regenerated and spliced in place; every byte outside that region is
# preserved verbatim. Both the original and the merged text are validated
# with ``tomllib``, and the merge is additionally checked to change
# nothing outside ``mcp_servers`` — any drift refuses the write
# (fail-closed, never corrupt the user's config).


def _toml_escape(value: str) -> str:
    """Render a Python string as a TOML basic-string literal."""
    out = value.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")
    return f'"{out}"'


def _toml_value(value: Any) -> str:
    """Render a scalar as a TOML value literal (str/bool/int/float only)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return _toml_escape(value)
    raise ValueError(f"unsupported TOML value type: {type(value).__name__}")


def _toml_header_name(raw: str) -> str:
    """Normalize a TOML header path (trim whitespace, strip outer quotes)."""
    name = raw.strip()
    if len(name) >= 2 and name[0] == name[-1] and name[0] in ("'", '"'):
        name = name[1:-1].strip()
    return name


def _toml_table_spans(text: str) -> list[tuple[str, int, int]]:
    """All TOML table headers in *text* as ``(path, start, body_start)``.

    ``start`` is the offset of the header line's first character;
    ``body_start`` is the offset just after the header line's newline.
    Multi-line values whose continuation lines happen to look like headers
    can confuse this text-level scan — callers never trust it alone: every
    produced merge is re-parsed and shape-compared before any write.
    """
    spans: list[tuple[str, int, int]] = []
    offset = 0
    for line in text.splitlines(keepends=True):
        match = TOML_TABLE_HEADER_RE.match(line)
        if match:
            spans.append((_toml_header_name(match.group("name")), offset, offset + len(line)))
        offset += len(line)
    return spans


def _toml_locate_table(text: str, table: str) -> tuple[int, int] | None:
    """Span of the ``[table]`` region: header start → next header / EOF.

    Child tables (``[table.sub]``) are NOT part of the span — each has its
    own header, so a span rewrite can never swallow their content.
    """
    spans = _toml_table_spans(text)
    for i, (name, start, _) in enumerate(spans):
        if name == table:
            end = spans[i + 1][1] if i + 1 < len(spans) else len(text)
            return start, end
    return None


def _codex_server_table(name: str, command: str, args: list[str], env: dict[str, Any]) -> str:
    """Render the managed ``[mcp_servers.<name>]`` table text.

    Shape mirrors the Codex preset (``integrations/mcp-presets.md``):
    ``command``, ``args``, optional ``env`` inline table. Raises
    ``ValueError`` for env values that have no minimal TOML rendering.
    """
    lines = [f"[{CODEX_MCP_ROOT}.{name}]"]
    lines.append(f"command = {_toml_escape(command)}")
    lines.append("args = [" + ", ".join(_toml_escape(a) for a in args) + "]")
    if env:
        body = ", ".join(f"{_toml_escape(k)} = {_toml_value(v)}" for k, v in env.items())
        lines.append(f"env = {{ {body} }}")
    return "\n".join(lines) + "\n"


# ── Result types ──────────────────────────────────────────────────────────────


@dataclass
class FileResult:
    """Outcome for a single file in a deploy/verify/update/uninstall run."""

    source: Path
    destination: Path
    status: DeployStatus
    deployed_version: str | None = None
    note: str = ""


@dataclass
class DeployResult:
    """Aggregate result of a deploy operation across one or more targets."""

    target_name: str
    files: list[FileResult] = field(default_factory=list)
    mcp_registered: bool = False
    mcp_note: str = ""

    @property
    def deployed_count(self) -> int:
        return sum(
            1 for f in self.files if f.status in (DeployStatus.DEPLOYED, DeployStatus.UPDATED)
        )

    @property
    def skipped_count(self) -> int:
        return sum(1 for f in self.files if f.status == DeployStatus.SKIPPED)


@dataclass
class VerifyResult:
    """Aggregate result of a verify operation."""

    target_name: str
    files: list[FileResult] = field(default_factory=list)

    @property
    def all_current(self) -> bool:
        return all(f.status == DeployStatus.CURRENT for f in self.files) and len(self.files) > 0

    @property
    def stale_count(self) -> int:
        return sum(1 for f in self.files if f.status == DeployStatus.STALE)

    @property
    def old_stamp_count(self) -> int:
        return sum(1 for f in self.files if f.status == DeployStatus.OLD_STAMP)

    @property
    def missing_count(self) -> int:
        return sum(1 for f in self.files if f.status == DeployStatus.MISSING)


@dataclass
class UninstallResult:
    """Aggregate result of an uninstall operation."""

    target_name: str
    removed: list[Path] = field(default_factory=list)
    skipped_user_files: list[Path] = field(default_factory=list)
    #: SEC-major #2 (ArchCom 2026-10-01): whether the MCP server entry the
    #: pack registered was removed (and the operator-facing note).
    mcp_unregistered: bool = False
    mcp_note: str = ""


# ── Manager ───────────────────────────────────────────────────────────────────


class IntegrationManager:
    """Orchestrates detection, deploy, verify, update, uninstall.

    The manager is stateless aside from the resolved pack root and version.
    All operations are idempotent.
    """

    def __init__(
        self,
        version: str,
        pack_root: Path | None = None,
        targets_config: TargetsConfig | None = None,
        home: Path | None = None,
    ) -> None:
        self.version = version
        self.pack_root = pack_root or self._default_pack_root()
        self.home = Path(home) if home is not None else Path.home()
        self.targets = targets_config or load_targets(home=home)

    @staticmethod
    def _default_pack_root() -> Path:
        """Resolve the shipped ``integrations/`` directory.

        Works both in editable installs (``src/mnemos/...``) and wheel
        installs where the package lives under ``site-packages``. Resolution
        order mirrors :func:`_resolve_pack_targets`:

        1. Source-tree layout (``src/mnemos/.../integrations``).
        2. Installed-package layout via ``importlib.resources``.
        3. Upward search for an ``integrations/`` sibling.
        4. CWD fallback (used in tests).
        """
        here = Path(__file__).resolve()
        # 1. Editable / repo layout: src/mnemos/cli/integration.py → up 4 levels
        candidate = here.parent.parent.parent.parent / "integrations"
        if candidate.is_dir():
            return candidate
        # 2. Installed-package layout via importlib.resources.
        try:
            from importlib.resources import files

            pack_integrations = files("vesmaro") / "integrations"
            if pack_integrations.is_dir():
                return Path(str(pack_integrations))
        except (ImportError, ModuleNotFoundError, FileNotFoundError):
            pass
        # 3. Fallback: search upward for an integrations/ sibling.
        for parent in here.parents:
            maybe = parent / "integrations"
            if maybe.is_dir():
                return maybe
        # 4. Last resort: assume CWD (used in tests).
        return Path.cwd() / "integrations"

    # ── Pack discovery ────────────────────────────────────────────────────────

    def _pack_files(self, kind: ArtefactKind) -> list[Path]:
        """Return sorted deployable files for a given artefact kind."""
        directory = self.pack_root / kind.value
        if not directory.is_dir():
            return []
        files: list[Path] = []
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            if path.name == ".gitkeep":
                continue
            if (
                kind is ArtefactKind.SCHEMAS
                and path.parent == directory
                and path.name == "README.md"
            ):
                # The provenance README documents the vendored pack — it is
                # pack documentation, not a deployable schema artefact.
                continue
            if path.suffix not in DEPLOYABLE_SUFFIXES:
                continue
            files.append(path)
        return files

    def _all_pack_files(self) -> dict[ArtefactKind, list[Path]]:
        return {kind: self._pack_files(kind) for kind in ArtefactKind}

    def _target_expected_dests(
        self,
        target: Target,
        all_files: dict[ArtefactKind, list[Path]] | None = None,
    ) -> set[Path]:
        """Every destination path ANY artefact kind of this target may own.

        Issue #448 (P3): several kinds may map into the SAME deploy
        directory (hermes deploys both ``instructions`` and ``skills``
        into ``~/.hermes/skills/``). Extra-file scans and orphan removal
        must judge a file against the destinations of the whole TARGET,
        not of a single kind — otherwise each kind misclassifies the
        other kind's freshly deployed files as stale orphans (verify
        false ``stale``/``missing`` warnings; ``update`` even deleted
        them). The schemas kind also owns its sidecar manifests (both
        name generations — stamp migration).
        """
        files_by_kind = all_files if all_files is not None else self._all_pack_files()
        expected: set[Path] = set()
        for kind, files in files_by_kind.items():
            dest_dir = target.deploy_map.get(kind.value)
            if dest_dir is None:
                continue
            if kind is ArtefactKind.AGENTS_MD:
                # The deploy-map value is the shared FILE the block lives in.
                expected.add(dest_dir)
            elif kind is ArtefactKind.SCHEMAS:
                expected.update(dest_dir / src.name for src in files)
                expected.add(dest_dir / SCHEMAS_MANIFEST_NAME)
                expected.add(dest_dir / LEGACY_SCHEMAS_MANIFEST_NAME)
            else:
                for src in files:
                    rel = src.relative_to(self.pack_root / kind.value)
                    expected.add(target.dest_for(kind.value, rel))
        return expected

    def _agents_md_content(self) -> tuple[str, Path | None]:
        """Concatenate the ``agents_md`` pack fragments into one block body.

        Returns ``(content, first_source_path)``. Multiple fragments (sorted
        by path) are joined with a blank line, so the pack can grow extra
        always-on fragments without schema changes. With an empty pack
        returns ``("", None)`` — deploy/verify then skip the kind.
        """
        files = self._pack_files(ArtefactKind.AGENTS_MD)
        if not files:
            return "", None
        content = "\n\n".join(p.read_text(encoding="utf-8").strip() for p in files) + "\n"
        return content, files[0]

    # ── Schemas kind: byte-identical files + sidecar manifest ────────────────

    def _schema_checksums(self, files: list[Path]) -> dict[str, str]:
        """Compute sha256 of each schema file keyed by its file name."""
        checksums: dict[str, str] = {}
        for src in files:
            digest = hashlib.sha256(src.read_bytes()).hexdigest()
            checksums[src.name] = digest
        return checksums

    def _deploy_schemas(
        self, dest_dir: Path, files: list[Path], *, dry_run: bool
    ) -> list[FileResult]:
        """Deploy the schema pack byte-identical plus the stamped manifest.

        Unlike every other file-copy kind, schema files are written
        WITHOUT the inline version stamp: JSON Schema files must stay
        valid JSON for schema validators. Ownership is recorded in
        ``vesma-schemas.manifest.json`` (stamped, JSON body) written into
        the same directory. Idempotency contract per file: CURRENT when
        the deployed bytes are identical AND the manifest carries the
        current version; UPDATED when bytes match but the manifest is
        stale (version bump re-deploy); DEPLOYED for new files.

        Cascade review SEC P3-4: BEFORE any write, the pack file
        checksums are verified against ``SCHEMAS_SOURCE_PIN["sha256"]``
        — a tampered pack (drifted digest, unpinned pack file, or stale
        pin entry) raises ``ValueError`` and NOTHING deploys. The pin
        protocol (ADR-0003) must never mint a clean-pin manifest over
        wrong bytes.
        """
        results: list[FileResult] = []

        checksums = self._schema_checksums(files)
        # SCHEMAS_SOURCE_PIN is a heterogeneous literal; its "sha256"
        # member is the name -> digest table (kept as a cast — the pin
        # table shape is pinned by test_source_pin_table_matches_shipped_bytes).
        pin_sha = cast(dict[str, str], SCHEMAS_SOURCE_PIN["sha256"])
        drifted = sorted(n for n, d in checksums.items() if pin_sha.get(n) != d)
        unpinned = sorted(set(checksums) - set(pin_sha))
        stale_pin = sorted(set(pin_sha) - set(checksums))
        if drifted or unpinned or stale_pin:
            raise ValueError(
                "schemas pack does not match SCHEMAS_SOURCE_PIN "
                f"({SCHEMAS_SOURCE_PIN['repo']}@{SCHEMAS_SOURCE_PIN['tag']}) — "
                f"drifted={drifted} unpinned={unpinned} stale_pin={stale_pin}; "
                "refusing to deploy (re-vendor the pack from the pin tag)"
            )

        manifest_path = dest_dir / SCHEMAS_MANIFEST_NAME
        legacy_manifest_path = dest_dir / LEGACY_SCHEMAS_MANIFEST_NAME
        existing_manifest_version: str | None = None
        if manifest_path.exists():
            existing_manifest_version = read_stamp(
                manifest_path.read_text(encoding="utf-8", errors="replace")
            )
        elif legacy_manifest_path.exists():
            # Migration window: ownership evidence lives in the legacy name.
            existing_manifest_version = read_stamp(
                legacy_manifest_path.read_text(encoding="utf-8", errors="replace")
            )

        manifest = schemas_manifest(self.version, checksums)

        for src in files:
            dest = dest_dir / src.name
            if dest.exists():
                existing = dest.read_bytes()
                if existing == src.read_bytes() and existing_manifest_version == self.version:
                    results.append(
                        FileResult(
                            source=src,
                            destination=dest,
                            status=DeployStatus.CURRENT,
                            deployed_version=self.version,
                            note="byte-identical and manifest up to date",
                        )
                    )
                    continue
                if not dry_run:
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(src.read_bytes())
                results.append(
                    FileResult(
                        source=src,
                        destination=dest,
                        status=DeployStatus.UPDATED,
                        deployed_version=self.version,
                        note=(
                            f"manifest refreshed from v{existing_manifest_version}"
                            if existing_manifest_version
                            else "content refreshed"
                        ),
                    )
                )
                continue

            if not dry_run:
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(src.read_bytes())
            results.append(
                FileResult(
                    source=src,
                    destination=dest,
                    status=DeployStatus.DEPLOYED,
                    deployed_version=self.version,
                    note="byte-identical schema deployed",
                )
            )

        # Refresh the manifest whenever its version is stale or absent, or
        # whenever the legacy-named manifest still carries ownership (stamp
        # migration: first deploy/update re-mints under the current name).
        needs_manifest_write = (
            existing_manifest_version != self.version or legacy_manifest_path.exists()
        )
        if needs_manifest_write and not dry_run:
            dest_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(manifest_path, manifest)
            # Stamp migration: a legacy-named manifest is superseded by the
            # freshly written current-name manifest — remove the old file.
            if legacy_manifest_path.exists():
                legacy_manifest_path.unlink()
                results.append(
                    FileResult(
                        source=Path("<schemas-manifest>"),
                        destination=legacy_manifest_path,
                        status=DeployStatus.UPDATED,
                        deployed_version=self.version,
                        note=(
                            f"legacy manifest {LEGACY_SCHEMAS_MANIFEST_NAME} migrated "
                            f"to {SCHEMAS_MANIFEST_NAME}"
                        ),
                    )
                )

        results.append(
            FileResult(
                source=Path("<schemas-manifest>"),
                destination=manifest_path,
                status=(
                    DeployStatus.CURRENT
                    if existing_manifest_version == self.version
                    else (
                        DeployStatus.UPDATED
                        if existing_manifest_version is not None
                        else DeployStatus.DEPLOYED
                    )
                ),
                deployed_version=self.version,
                note="stamped sidecar manifest",
            )
        )
        return results

    # ── Deploy ─────────────────────────────────────────────────────────────────

    def deploy(
        self,
        target_name: str,
        *,
        dry_run: bool = False,
    ) -> DeployResult:
        """Deploy all pack files to a single target.

        Files are stamped with the current version and copied into the
        target's deploy directories. Existing stamped files are updated;
        user files are never touched.
        """
        target = self.targets.get(target_name)
        if target is None:
            raise ValueError(f"Unknown target: {target_name!r}")

        result = DeployResult(target_name=target_name)

        for kind, files in self._all_pack_files().items():
            if kind is ArtefactKind.AGENTS_MD:
                # Block injection — handled after the file-copy loop (the
                # deploy-map value is a FILE, not a directory).
                continue
            dest_dir = target.deploy_map.get(kind.value)
            if dest_dir is None:
                # Target doesn't accept this artefact kind — skip silently.
                # Not every target supports every kind (e.g. generic-copilot
                # only has prompts, copilot has instructions+skills). Logging a
                # noisy "no deploy map" row for every unsupported kind makes
                # the output look like something is broken when it isn't.
                logger.debug(
                    "target %r has no deploy map for %s — skipping silently",
                    target_name,
                    kind.value,
                )
                continue

            if kind is ArtefactKind.SCHEMAS:
                # Byte-identical copy path with the sidecar manifest —
                # handled by the dedicated schemas engine (no inline stamp).
                result.files.extend(self._deploy_schemas(dest_dir, files, dry_run=dry_run))
                continue

            for src in files:
                rel = src.relative_to(self.pack_root / kind.value)
                dest = target.dest_for(kind.value, rel)
                file_result = self._deploy_file(src, dest, dry_run=dry_run)
                result.files.append(file_result)

        agents_md_dest = target.deploy_map.get(ArtefactKind.AGENTS_MD.value)
        if agents_md_dest is not None:
            result.files.append(self._deploy_agents_md(agents_md_dest, dry_run=dry_run))

        return result

    def _assemble_agents_md_file(self, existing: str, block_body: str) -> tuple[str, str | None]:
        """Build the desired full content of an AGENTS.md file.

        Returns ``(desired_content, existing_block_version)`` where
        ``existing_block_version`` is the version of the block currently in
        the content (``None`` if absent). The user's content is preserved
        byte-for-byte, INCLUDING line-ending style. An existing block is
        replaced IN PLACE — spliced at its exact offset, so instruction
        ordering relative to user content never changes. Only on first
        injection is a missing trailing newline on the user's last line
        repaired (one ``\\n``) so the injected block never glues onto
        user text.
        """
        new_block = render_agents_md_block(block_body, self.version)
        match = AGENTS_MD_BLOCK_RE.search(existing)
        if match is None:
            base = existing
            if base and not base.endswith("\n"):
                base += "\n"
            return base + new_block, None
        # Splice at the old block's exact span — ordering is preserved.
        desired = existing[: match.start()] + new_block + existing[match.end() :]
        return desired, match.group("version")

    def _deploy_agents_md(self, dest: Path, *, dry_run: bool) -> FileResult:
        """Inject or refresh the stamped block inside a shared AGENTS.md file.

        Idempotent: re-running with the same pack content and version reports
        CURRENT and writes nothing. An existing block (older version or
        drifted content) is replaced IN PLACE — user content around it is
        never touched.
        """
        block_body, src = self._agents_md_content()
        if src is None:
            return FileResult(
                source=Path("<agents-md-pack>"),
                destination=dest,
                status=DeployStatus.SKIPPED,
                note="no agents_md pack content shipped",
            )

        try:
            existing = _read_user_text(dest) if dest.exists() else ""
        except UnicodeDecodeError:
            return FileResult(
                source=src,
                destination=dest,
                status=DeployStatus.SKIPPED,
                note="destination is not valid UTF-8 — refusing to touch a file we cannot parse",
            )
        desired, existing_version = self._assemble_agents_md_file(existing, block_body)

        if existing_version is not None and existing == desired:
            return FileResult(
                source=src,
                destination=dest,
                status=DeployStatus.CURRENT,
                deployed_version=self.version,
                note="block already up to date",
            )

        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(dest, desired)
        return FileResult(
            source=src,
            destination=dest,
            status=DeployStatus.UPDATED if existing_version is not None else DeployStatus.DEPLOYED,
            deployed_version=self.version,
            note=(
                f"block updated from v{existing_version}"
                if existing_version is not None
                else "block injected into AGENTS.md"
            ),
        )

    def _deploy_file(self, src: Path, dest: Path, *, dry_run: bool) -> FileResult:
        """Deploy a single file, returning the outcome.

        Issue #448 (multi-target one-pass contract): a destination file we
        cannot parse (non-UTF-8) is reported SKIPPED and left untouched —
        the same refuse-to-touch discipline as the ``agents_md`` kind —
        instead of raising. An unguarded read here used to abort the whole
        CLI target loop, so every target AFTER the failing one silently
        never deployed.
        """
        content = src.read_text(encoding="utf-8")
        stamped = stamp_content(
            content, self.version, line_comment=src.suffix in LINE_COMMENT_SUFFIXES
        )

        if dest.exists():
            try:
                existing = dest.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                return FileResult(
                    source=src,
                    destination=dest,
                    status=DeployStatus.SKIPPED,
                    note=(
                        "destination is not valid UTF-8 — refusing to touch a file we cannot parse"
                    ),
                )
            existing_version = read_stamp(existing)
            if existing_version == self.version and existing == stamped:
                return FileResult(
                    source=src,
                    destination=dest,
                    status=DeployStatus.CURRENT,
                    deployed_version=self.version,
                    note="already up to date",
                )
            # Update in place (stale or content changed).
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(stamped, encoding="utf-8")
            return FileResult(
                source=src,
                destination=dest,
                status=DeployStatus.UPDATED,
                deployed_version=self.version,
                note=(
                    f"updated from v{existing_version}" if existing_version else "content refreshed"
                ),
            )

        # New deployment.
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(stamped, encoding="utf-8")
        return FileResult(
            source=src,
            destination=dest,
            status=DeployStatus.DEPLOYED,
            deployed_version=self.version,
        )

    # ── Verify ────────────────────────────────────────────────────────────────

    def verify(self, target_name: str) -> VerifyResult:
        """Compare deployed files against the shipped pack.

        For each pack file, checks if the deployed copy exists and is current.
        Also scans deploy directories for extra files (user-created or stale
        mnemos files no longer in the pack) and reports them as SKIPPED.
        """
        target = self.targets.get(target_name)
        if target is None:
            raise ValueError(f"Unknown target: {target_name!r}")

        result = VerifyResult(target_name=target_name)
        all_files = self._all_pack_files()
        # Target-wide destination set (issue #448 P3): with several kinds
        # sharing one deploy directory, a file owned by another kind of the
        # SAME target must never be reported as an extra/stale orphan here.
        expected_dests = self._target_expected_dests(target, all_files)

        for kind, files in all_files.items():
            if kind is ArtefactKind.AGENTS_MD:
                # Block presence/version/content — checked after the loop.
                continue
            dest_dir = target.deploy_map.get(kind.value)
            if dest_dir is None:
                continue

            if kind is ArtefactKind.SCHEMAS:
                # Byte-identical content + manifest version/stamp checks.
                result.files.extend(self._verify_schemas(dest_dir, files))
                continue

            for src in files:
                rel = src.relative_to(self.pack_root / kind.value)
                dest = target.dest_for(kind.value, rel)
                result.files.append(self._verify_file(src, dest))

            # Scan for extra files in the deploy dir (user files or stale mnemos files).
            if dest_dir.exists():
                for path in sorted(dest_dir.rglob("*")):
                    if not path.is_file() or path in expected_dests:
                        continue
                    if path.name == ".gitkeep":
                        continue
                    content = path.read_text(encoding="utf-8", errors="replace")
                    deployed_version = read_stamp(content)
                    if deployed_version is not None:
                        # Stamped but not in pack — stale mnemos file (removed from pack).
                        result.files.append(
                            FileResult(
                                source=Path("<not-in-pack>"),
                                destination=path,
                                status=DeployStatus.STALE,
                                deployed_version=deployed_version,
                                note="stamped file no longer in pack — safe to uninstall",
                            )
                        )
                    else:
                        result.files.append(
                            FileResult(
                                source=Path("<user-file>"),
                                destination=path,
                                status=DeployStatus.SKIPPED,
                                note="user file — not managed by Vesma",
                            )
                        )

        agents_md_dest = target.deploy_map.get(ArtefactKind.AGENTS_MD.value)
        if agents_md_dest is not None:
            result.files.append(self._verify_agents_md(agents_md_dest))

        return result

    def _verify_agents_md(self, dest: Path) -> FileResult:
        """Verify the stamped block in a shared AGENTS.md file."""
        _, src = self._agents_md_content()
        source = src if src is not None else Path("<agents-md-pack>")

        if not dest.exists():
            return FileResult(
                source=source,
                destination=dest,
                status=DeployStatus.MISSING,
                note="no AGENTS.md file — block not deployed",
            )

        try:
            existing = _read_user_text(dest)
        except UnicodeDecodeError:
            return FileResult(
                source=source,
                destination=dest,
                status=DeployStatus.SKIPPED,
                note="destination is not valid UTF-8 — cannot verify",
            )
        deployed_version = read_agents_md_version(existing)
        if deployed_version is None:
            return FileResult(
                source=source,
                destination=dest,
                status=DeployStatus.MISSING,
                note="no vesma block in file — not injected yet",
            )
        if deployed_version != self.version:
            return FileResult(
                source=source,
                destination=dest,
                status=DeployStatus.STALE,
                deployed_version=deployed_version,
                note=f"block v{deployed_version} != current v{self.version}",
            )
        if "mnemos:integration:v" in existing:
            return FileResult(
                source=source,
                destination=dest,
                status=DeployStatus.OLD_STAMP,
                deployed_version=deployed_version,
                note=(
                    "legacy mnemos:integration block markers — "
                    "next deploy/update re-stamps to vesma:integration"
                ),
            )

        block_body, _ = self._agents_md_content()
        desired, _ = self._assemble_agents_md_file(existing, block_body)
        if existing != desired:
            return FileResult(
                source=source,
                destination=dest,
                status=DeployStatus.STALE,
                deployed_version=deployed_version,
                note="block content drifted from pack — update restores it",
            )
        return FileResult(
            source=source,
            destination=dest,
            status=DeployStatus.CURRENT,
            deployed_version=self.version,
        )

    def _verify_file(self, src: Path, dest: Path) -> FileResult:
        if not dest.exists():
            return FileResult(
                source=src,
                destination=dest,
                status=DeployStatus.MISSING,
                note="not deployed",
            )

        try:
            existing = dest.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            # Same contract as _deploy_file (issue #448): an unreadable file
            # at a pack destination is reported, never fatal.
            return FileResult(
                source=src,
                destination=dest,
                status=DeployStatus.SKIPPED,
                note="destination is not valid UTF-8 — cannot verify",
            )
        deployed_version = read_stamp(existing)
        if deployed_version is None:
            return FileResult(
                source=src,
                destination=dest,
                status=DeployStatus.SKIPPED,
                note="no vesma stamp — user file, not ours",
            )
        if deployed_version != self.version:
            return FileResult(
                source=src,
                destination=dest,
                status=DeployStatus.STALE,
                deployed_version=deployed_version,
                note=f"deployed v{deployed_version} != current v{self.version}",
            )
        if has_legacy_stamp(existing):
            return FileResult(
                source=src,
                destination=dest,
                status=DeployStatus.OLD_STAMP,
                deployed_version=deployed_version,
                note=(
                    "legacy mnemos-integration stamp — "
                    "next deploy/update re-stamps to vesma-integration"
                ),
            )
        return FileResult(
            source=src,
            destination=dest,
            status=DeployStatus.CURRENT,
            deployed_version=self.version,
        )

    def _verify_schemas(self, dest_dir: Path, files: list[Path]) -> list[FileResult]:
        """Verify deployed schemas: byte-identity + manifest stamp/version.

        The manifest doubles as the ownership marker for the directory: it
        must carry the regular stamp. Schema files are verified by content
        (sha256 of the deployed bytes vs the pack), never by an inline
        stamp. Extra ``*.json`` files in the directory are classified the
        usual way — stamped-but-unmanaged (schema files have no inline
        stamp, so a schema file can only be stamped if it is a manifest
        copy from another deploy, which is stale) vs unstamped user files.
        """
        results: list[FileResult] = []

        manifest_path = dest_dir / SCHEMAS_MANIFEST_NAME
        legacy_manifest_path = dest_dir / LEGACY_SCHEMAS_MANIFEST_NAME
        manifest_version: str | None = None
        manifest_exists = False
        if manifest_path.exists():
            manifest_version = read_stamp(
                manifest_path.read_text(encoding="utf-8", errors="replace")
            )
            manifest_exists = True
        elif legacy_manifest_path.exists():
            # Migration window: the legacy-named manifest still proves
            # ownership, but is reported OLD_STAMP so the next update
            # migrates it to the current name.
            manifest_version = read_stamp(
                legacy_manifest_path.read_text(encoding="utf-8", errors="replace")
            )
            manifest_exists = True
        if not manifest_exists:
            results.append(
                FileResult(
                    source=Path("<schemas-manifest>"),
                    destination=manifest_path,
                    status=DeployStatus.MISSING,
                    note="no sidecar manifest — schemas not deployed",
                )
            )

        # The manifest is always a reported row: it is the ownership marker
        # for the whole kind (see _deploy_schemas / _uninstall_schemas).
        if manifest_exists:
            if manifest_version is None:
                results.append(
                    FileResult(
                        source=Path("<schemas-manifest>"),
                        destination=manifest_path,
                        status=DeployStatus.SKIPPED,
                        note="manifest carries no vesma stamp — not ours",
                    )
                )
            elif manifest_version != self.version:
                results.append(
                    FileResult(
                        source=Path("<schemas-manifest>"),
                        destination=manifest_path,
                        status=DeployStatus.STALE,
                        deployed_version=manifest_version,
                        note=f"manifest v{manifest_version} != current v{self.version}",
                    )
                )
            elif manifest_path.exists() and has_legacy_stamp(
                manifest_path.read_text(encoding="utf-8", errors="replace")
            ):
                results.append(
                    FileResult(
                        source=Path("<schemas-manifest>"),
                        destination=manifest_path,
                        status=DeployStatus.OLD_STAMP,
                        deployed_version=manifest_version,
                        note=(
                            "legacy mnemos-integration stamp — "
                            "update re-stamps to vesma-integration"
                        ),
                    )
                )
            elif legacy_manifest_path.exists():
                results.append(
                    FileResult(
                        source=Path("<schemas-manifest>"),
                        destination=legacy_manifest_path,
                        status=DeployStatus.OLD_STAMP,
                        deployed_version=manifest_version,
                        note=(
                            f"legacy manifest name {LEGACY_SCHEMAS_MANIFEST_NAME} — "
                            f"update migrates it to {SCHEMAS_MANIFEST_NAME}"
                        ),
                    )
                )
            else:
                results.append(
                    FileResult(
                        source=Path("<schemas-manifest>"),
                        destination=manifest_path,
                        status=DeployStatus.CURRENT,
                        deployed_version=self.version,
                    )
                )

        seen_dests: set[Path] = {manifest_path, legacy_manifest_path}
        for src in files:
            dest = dest_dir / src.name
            seen_dests.add(dest)
            if not dest.exists():
                results.append(
                    FileResult(
                        source=src,
                        destination=dest,
                        status=DeployStatus.MISSING,
                        note="not deployed",
                    )
                )
                continue
            if dest.read_bytes() != src.read_bytes():
                results.append(
                    FileResult(
                        source=src,
                        destination=dest,
                        status=DeployStatus.STALE,
                        deployed_version=manifest_version,
                        note="deployed bytes differ from pack — update restores them",
                    )
                )
                continue
            if manifest_version is None:
                results.append(
                    FileResult(
                        source=src,
                        destination=dest,
                        status=DeployStatus.SKIPPED,
                        note="bytes match pack but no stamped manifest — not ours",
                    )
                )
                continue
            if manifest_version != self.version:
                results.append(
                    FileResult(
                        source=src,
                        destination=dest,
                        status=DeployStatus.STALE,
                        deployed_version=manifest_version,
                        note=f"manifest v{manifest_version} != current v{self.version}",
                    )
                )
                continue
            results.append(
                FileResult(
                    source=src,
                    destination=dest,
                    status=DeployStatus.CURRENT,
                    deployed_version=self.version,
                )
            )

        # Extra files in the deploy dir: classify as ours-stale vs user.
        if dest_dir.exists():
            for path in sorted(dest_dir.rglob("*")):
                if not path.is_file() or path in seen_dests:
                    continue
                if path.name == ".gitkeep":
                    continue
                content = path.read_text(encoding="utf-8", errors="replace")
                deployed_version = read_stamp(content)
                if deployed_version is not None:
                    results.append(
                        FileResult(
                            source=Path("<not-in-pack>"),
                            destination=path,
                            status=DeployStatus.STALE,
                            deployed_version=deployed_version,
                            note="stamped file no longer in pack — safe to uninstall",
                        )
                    )
                else:
                    results.append(
                        FileResult(
                            source=Path("<user-file>"),
                            destination=path,
                            status=DeployStatus.SKIPPED,
                            note="user file — not managed by Vesma",
                        )
                    )
        return results

    # ── Update ────────────────────────────────────────────────────────────────

    def update(self, target_name: str, *, dry_run: bool = False) -> DeployResult:
        """Bring stale deployed files to the current version and remove orphans.

        Equivalent to ``deploy`` but, after deploying pack files, ALSO scans
        each target's deploy directories for stamped files that are no longer
        in the pack (orphans from a previous release) and removes them. This
        makes ``update`` symmetric with ``verify``: whatever ``verify`` flags
        as STALE, ``update`` clears — whether the staleness is an outdated
        stamp on an in-pack file (handled by ``deploy``) or a stamped file
        removed from the pack (handled here).

        User files (no mnemos stamp) are never touched.
        """
        # deploy() already updates stale in-pack files in place.
        result = self.deploy(target_name, dry_run=dry_run)
        # Now remove orphaned stamped files not in the current pack.
        self._remove_orphans(target_name, result, dry_run=dry_run)
        return result

    def _remove_orphans(
        self,
        target_name: str,
        result: DeployResult,
        *,
        dry_run: bool,
    ) -> None:
        """Remove stamped files in deploy dirs that are not in the current pack.

        Reuses the orphan-detection logic from :meth:`verify` (scan deploy dir
        for stamped files not in pack) and the safe-removal logic from
        :meth:`uninstall` (unlink + clean empty parents). Appends one
        ``FileResult`` per removed orphan to ``result.files`` so callers see
        what was cleaned up.
        """
        target = self.targets.get(target_name)
        if target is None:
            raise ValueError(f"Unknown target: {target_name!r}")

        # Target-wide destination set (issue #448 P3): with several kinds
        # sharing one deploy directory (hermes: instructions + skills both
        # → ~/.hermes/skills/), orphan removal must NEVER remove a file
        # that another kind of the same target just deployed. The old
        # per-kind expected-set wiped the whole shared directory on every
        # update (deploy re-wrote it, then each kind deleted the other
        # kind's files as "orphans").
        all_files = self._all_pack_files()
        expected_dests = self._target_expected_dests(target, all_files)

        scanned_dirs: set[Path] = set()
        for kind in all_files:
            if kind is ArtefactKind.AGENTS_MD:
                # The block lives inside a shared user file — orphan removal
                # does not apply (update refreshes it in place instead).
                continue
            dest_dir = target.deploy_map.get(kind.value)
            if dest_dir is None or not dest_dir.exists() or dest_dir in scanned_dirs:
                continue
            scanned_dirs.add(dest_dir)

            for path in sorted(dest_dir.rglob("*")):
                if not path.is_file() or path in expected_dests:
                    continue
                if path.name == ".gitkeep":
                    continue
                content = path.read_text(encoding="utf-8", errors="replace")
                deployed_version = read_stamp(content)
                if deployed_version is None:
                    # User file — not ours, leave it alone.
                    continue
                # Stamped but not in pack — orphan. Remove it.
                if not dry_run:
                    path.unlink()
                    self._cleanup_empty_parents(path, dest_dir)
                result.files.append(
                    FileResult(
                        source=Path("<not-in-pack>"),
                        destination=path,
                        status=DeployStatus.UPDATED,
                        deployed_version=deployed_version,
                        note="orphaned stamped file removed (no longer in pack)",
                    )
                )

    # ── Uninstall ──────────────────────────────────────────────────────────────

    def uninstall(self, target_name: str, *, dry_run: bool = False) -> UninstallResult:
        """Remove ONLY files carrying the pack's version stamp.

        Recognizes both stamp generations (``vesma-integration`` and the
        legacy ``mnemos-integration``). User-created files (no stamp) are
        never deleted. The method scans each deploy directory recursively
        for stamped files and also unregisters the MCP server entry the
        pack registered (SEC-major #2, ArchCom 2026-10-01) — foreign MCP
        entries are never touched.
        """
        target = self.targets.get(target_name)
        if target is None:
            raise ValueError(f"Unknown target: {target_name!r}")

        result = UninstallResult(target_name=target_name)

        for kind in ArtefactKind:
            if kind is ArtefactKind.AGENTS_MD:
                # Shared user file — strip only the stamped block below.
                continue
            dest_dir = target.deploy_map.get(kind.value)
            if dest_dir is None or not dest_dir.exists():
                continue

            if kind is ArtefactKind.SCHEMAS:
                self._uninstall_schemas(dest_dir, result, dry_run=dry_run)
                continue

            for path in sorted(dest_dir.rglob("*")):
                if not path.is_file():
                    continue
                content = path.read_text(encoding="utf-8", errors="replace")
                if read_stamp(content) is not None:
                    if not dry_run:
                        path.unlink()
                        # Clean up empty parent dirs (but not the deploy root).
                        self._cleanup_empty_parents(path, dest_dir)
                    result.removed.append(path)
                else:
                    result.skipped_user_files.append(path)

        agents_md_dest = target.deploy_map.get(ArtefactKind.AGENTS_MD.value)
        if agents_md_dest is not None:
            removed = self._uninstall_agents_md(agents_md_dest, dry_run=dry_run)
            if removed is not None:
                result.removed.append(removed)

        # SEC-major #2: take down the MCP registration this pack added.
        # Dry-run reports what would happen without touching the config.
        if not dry_run:
            ok, note = self.unregister_mcp(target_name)
            result.mcp_unregistered = ok
            result.mcp_note = note
        else:
            result.mcp_note = "MCP entry removal skipped in dry-run"

        return result

    def _uninstall_schemas(self, dest_dir: Path, result: UninstallResult, *, dry_run: bool) -> None:
        """Uninstall deployed schemas — provably ours, never user files.

        Ownership evidence hierarchy (cascade review SEC P3-3):

        1. The stamped sidecar manifest is always ours → removed.
        2. A ``*.schema.json`` is ours ONLY by an inline stamp or by
           BYTE-IDENTITY with a CURRENT pack schema (same name, same
           sha256 — schema files deploy byte-identical, so a genuine
           deployment always matches). Manifest checksums are NOT
           ownership proof for the files they sit next to: the manifest
           is a plain on-disk file, and a co-writer of the deploy
           directory can forge one naming any foreign file — under the
           old rule that made uninstall delete files we never wrote.
           File-NAME overlap is equally insufficient (a foreign file
           named like a pack schema is still foreign).

        Consequence (the safe direction): a deployed copy that drifted
        from the current pack bytes — edited locally, left over from an
        older pack version, or foreign — is NOT provably ours and
        SURVIVES uninstall as a user file; ``update`` restores drifted
        genuine deployments in place before any uninstall would matter.

        Empty parent dirs are cleaned up to the deploy root afterwards.
        """
        manifest_path = dest_dir / SCHEMAS_MANIFEST_NAME
        legacy_manifest_path = dest_dir / LEGACY_SCHEMAS_MANIFEST_NAME

        # The manifest itself is stamped → ordinary ownership rule applies.
        # Both name generations are ours (stamp migration window).
        for manifest_file in (manifest_path, legacy_manifest_path):
            if manifest_file.exists():
                if not dry_run:
                    manifest_file.unlink()
                result.removed.append(manifest_file)

        # name → sha256 of the CURRENT pack schemas — the only
        # byte-identity registry ownership may rest on.
        pack_checksums = self._schema_checksums(self._pack_files(ArtefactKind.SCHEMAS))
        for path in sorted(dest_dir.rglob("*.schema.json")):
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            # An inline stamp is proof of ownership in every other kind —
            # keep that invariant here (verify flags such orphans as
            # "safe to uninstall"; uninstall must agree). Beyond that,
            # ONLY byte-identity with the current pack proves ownership;
            # anything else (drifted, older-version, foreign, or named in
            # a forged manifest) is a user file.
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            ours = read_stamp(content) is not None or pack_checksums.get(path.name) == digest
            if ours:
                if not dry_run:
                    path.unlink()
                result.removed.append(path)
            else:
                result.skipped_user_files.append(path)

        if not dry_run:
            self._cleanup_empty_parents(manifest_path, dest_dir)

    def _uninstall_agents_md(self, dest: Path, *, dry_run: bool) -> Path | None:
        """Remove the mnemos block(s) from a shared AGENTS.md file.

        Removes every PAIRED mnemos-marked block (any version — markers are
        treated as owned by mnemos, including one a user has quoted); see
        :func:`strip_agents_md_block`. Returns the destination path when a
        block was found (the removal target), or ``None`` when there is
        nothing of ours in the file. If
        nothing but whitespace remains after the strip, the file itself is
        removed — deploy created it, and whitespace-only content is not user
        content. A file that still carries user content is kept.
        """
        if not dest.exists():
            return None
        try:
            existing = _read_user_text(dest)
        except UnicodeDecodeError:
            return None
        cleaned, version = strip_agents_md_block(existing)
        if version is None:
            return None
        if not dry_run:
            if cleaned.strip() == "":
                dest.unlink()
            else:
                _atomic_write_text(dest, cleaned)
        return dest

    @staticmethod
    def _cleanup_empty_parents(path: Path, root: Path) -> None:
        """Remove empty directories left after file deletion, up to root."""
        parent = path.parent
        while parent != root and parent.exists():
            try:
                next(parent.iterdir())
                return  # not empty — stop
            except StopIteration:
                parent.rmdir()
                parent = parent.parent

    # ── MCP registration ──────────────────────────────────────────────────────

    @staticmethod
    def _find_mcp_setup_script() -> Path | None:
        """Find ``mcp-setup.sh`` in source-tree, wheel, or upward search.

        Resolution order:

        1. **Source-tree layout** — ``src/mnemos/cli/`` → up 4 levels →
           ``scripts/mcp-setup.sh`` (editable / repo installs).
        2. **Wheel layout** — ``importlib.resources.files("vesmaro") /
           "scripts" / "mcp-setup.sh"`` (pip-installed wheel).
        3. **Upward search** — walk parents of this file looking for a
           ``scripts/`` sibling (fallback for unusual layouts).

        Returns the first existing path, or ``None`` if not found anywhere.
        """
        here = Path(__file__).resolve()
        # 1. Source-tree layout: src/mnemos/cli/integration.py → up 4 levels
        candidate = here.parent.parent.parent.parent / "scripts" / "mcp-setup.sh"
        if candidate.is_file():
            return candidate
        # 2. Wheel layout via importlib.resources.
        try:
            from importlib.resources import files

            script = files("vesmaro") / "scripts" / "mcp-setup.sh"
            if script.is_file():
                return Path(str(script))
        except (ImportError, ModuleNotFoundError, FileNotFoundError):
            pass
        # 3. Upward search for a scripts/ sibling.
        for parent in here.parents:
            candidate = parent / "scripts" / "mcp-setup.sh"
            if candidate.is_file():
                return candidate
        return None

    def register_mcp(
        self, target_name: str | None = None, mnemos_bin: str | None = None
    ) -> tuple[bool, str]:
        """Register the MCP server for a target (or the legacy VS Code path).

        Targets that declare ``mcp.config`` in targets.yaml (zcode, agents,
        cursor, claude-code, windsurf) are registered by an in-place JSON
        merge that preserves every other key in the file; the ``codex``
        target goes through the dedicated TOML-merge engine. Targets
        without one fall back to ``mcp-setup.sh`` (VS Code ``mcp.json``),
        keeping the historical behaviour.
        """
        target = self.targets.get(target_name) if target_name else None
        if target is not None and target.mcp_format == "pi":
            return self._register_mcp_pi(target)
        if target is not None and target.mcp_format == "codex":
            return self._register_mcp_toml(target, mnemos_bin=mnemos_bin)
        if target is not None and target.mcp_config is not None:
            return self._register_mcp_json(target, mnemos_bin=mnemos_bin)
        return self._register_mcp_script(mnemos_bin=mnemos_bin)

    # ── MCP: Pi extension bridge ──────────────────────────────────────────────

    def _register_mcp_pi(self, target: Target) -> tuple[bool, str]:
        """ "Register" MCP for Pi by confirming the bridge extension is deployed.

        Pi has no MCP config file to merge into: TypeScript extensions ARE
        the tool surface. Registration therefore reduces to verifying that
        the stamped bridge (``integrations/extensions/vesma-mcp.ts``) sits
        in the target's extensions directory — which ``deploy()`` (always
        run before this in ``setup()``) has just placed there.
        """
        ext = target.mcp_config
        assert ext is not None  # guaranteed by targets.yaml schema
        if not ext.exists():
            return False, f"bridge extension missing: {ext} — run deploy first"
        deployed_version = read_stamp(ext.read_text(encoding="utf-8", errors="replace"))
        if deployed_version is None:
            return False, f"{ext} carries no mnemos stamp — not our file"
        if deployed_version != self.version:
            return False, f"{ext} is stale (v{deployed_version} != v{self.version})"
        return True, f"MCP bridge deployed: {ext} (restart Pi or /reload to connect)"

    # ── MCP: Codex TOML-merge registration ────────────────────────────────────

    def _register_mcp_toml(self, target: Target, *, mnemos_bin: str | None) -> tuple[bool, str]:
        """Merge the ``vesma`` server table into the Codex TOML config.

        See the module-level ``Codex TOML merge engine`` section: the merge
        is a text-level splice of the managed ``[mcp_servers.vesma]``
        region — every byte outside it is preserved, both texts are
        validated with ``tomllib`` and the merged text must differ from
        the original only inside ``mcp_servers`` (any drift refuses the
        write). A legacy ``[mcp_servers.mnemos]`` entry that passes the
        ownership evidence check is migrated to the brand-primary key in
        the same pass; the user-tuned ``env`` values are kept (only
        missing keys are filled in), mirroring the JSON engine.
        """
        cfg_path = target.mcp_config
        assert cfg_path is not None  # guaranteed by register_mcp dispatch
        try:
            text = _read_user_text(cfg_path) if cfg_path.exists() else ""
        except (OSError, UnicodeDecodeError) as exc:
            return False, f"cannot read {cfg_path}: {exc}"
        try:
            data = tomllib.loads(text) if text.strip() else {}
        except tomllib.TOMLDecodeError as exc:
            return False, f"cannot parse {cfg_path}: {exc}"
        servers = data.get(CODEX_MCP_ROOT)
        if servers is not None and not isinstance(servers, dict):
            return False, f"{cfg_path}: [{CODEX_MCP_ROOT}] is not a table"
        servers = servers if isinstance(servers, dict) else {}

        existing = servers.get(MCP_SERVER_KEY)
        legacy = servers.get(MCP_LEGACY_SERVER_KEY)
        ours = existing if isinstance(existing, dict) else None
        if ours is None and isinstance(legacy, dict) and self._mcp_entry_is_ours(legacy):
            ours = legacy  # stamp migration: legacy entry adopts the primary key
        source_key = MCP_SERVER_KEY if isinstance(existing, dict) else MCP_LEGACY_SERVER_KEY

        span: tuple[int, int] | None = None
        if ours is not None:
            span = _toml_locate_table(text, f"{CODEX_MCP_ROOT}.{source_key}")
            if span is None:
                # Present in parsed data but not as a plain [table]
                # (inline table / dotted keys) — a text splice cannot edit
                # it surgically; refuse rather than mint a redefinition.
                return False, (
                    f"{cfg_path}: [{CODEX_MCP_ROOT}.{source_key}] is not a plain TOML table "
                    "(inline or dotted form) — manual merge required"
                )

        raw_env = ours.get("env") if ours else None
        kept_env: dict[str, Any] = dict(raw_env) if isinstance(raw_env, dict) else {}
        for key, value in self._mcp_env_defaults().items():
            kept_env.setdefault(key, value)

        bin_path = self._resolve_mnemos_bin(mnemos_bin)
        try:
            new_table = _codex_server_table(MCP_SERVER_KEY, bin_path, ["mcp-server"], kept_env)
        except ValueError as exc:
            return False, f"{cfg_path}: cannot render server table: {exc}"

        if span is not None:
            tail = text[span[1] :]
            sep = "" if (not tail or tail.startswith("\n")) else "\n"
            merged = text[: span[0]] + new_table + sep + tail
        else:
            base = text
            if base and not base.endswith("\n"):
                base += "\n"
            if base.strip():
                base += "\n"  # blank separator before the appended table
            merged = base + new_table

        ok, problem = self._validate_toml_merge(data, servers, merged)
        if not ok:
            return False, f"{cfg_path}: {problem}"
        if merged == text:
            return True, f"MCP server already registered in {cfg_path}"
        try:
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(cfg_path, merged)
        except OSError as exc:
            return False, f"cannot write {cfg_path}: {exc}"
        return True, f"MCP server registered in {cfg_path}"

    def _validate_toml_merge(
        self,
        original_data: dict[str, Any],
        original_servers: dict[str, Any],
        merged: str,
    ) -> tuple[bool, str]:
        """Fail-closed validation of a TOML merge — before any write.

        The merged text must (a) parse, (b) keep every top-level table
        outside ``mcp_servers`` equal (as parsed data) to the original and
        (c) keep every foreign server entry inside ``mcp_servers`` equal.
        A memory-preserving splice always satisfies all three; a violation
        means the text scan misfired, so the merge is refused.
        """
        try:
            check = tomllib.loads(merged) if merged.strip() else {}
        except tomllib.TOMLDecodeError as exc:
            return False, f"merged TOML failed validation ({exc}) — file left untouched"
        before = {k: v for k, v in original_data.items() if k != CODEX_MCP_ROOT}
        after = {k: v for k, v in check.items() if k != CODEX_MCP_ROOT}
        if before != after:
            return False, "merged TOML drifts outside [mcp_servers] — file left untouched"
        merged_servers = check.get(CODEX_MCP_ROOT)
        managed = (MCP_SERVER_KEY, MCP_LEGACY_SERVER_KEY)
        before_srv = {k: v for k, v in original_servers.items() if k not in managed}
        after_srv = (
            {k: v for k, v in merged_servers.items() if k not in managed}
            if isinstance(merged_servers, dict)
            else {}
        )
        if before_srv != after_srv:
            return False, "merged TOML drifts into foreign MCP servers — file left untouched"
        return True, ""

    def _unregister_mcp_toml(self, target: Target) -> tuple[bool, str]:
        """Remove the pack-owned ``[mcp_servers.vesma]``/``.mnemos`` tables.

        The TOML mirror of :meth:`unregister_mcp`: a table is removed only
        when its parsed entry passes the same ownership evidence check as
        the JSON engine (:meth:`_mcp_entry_is_ours`); foreign tables,
        unrelated config and everything outside the managed spans are
        preserved as data. The merged text is re-validated with
        ``tomllib`` before the atomic write.
        """
        cfg_path = target.mcp_config
        assert cfg_path is not None  # guaranteed by unregister_mcp dispatch
        if not cfg_path.exists():
            return False, f"{cfg_path} does not exist — nothing to unregister"
        try:
            text = _read_user_text(cfg_path)
        except (OSError, UnicodeDecodeError) as exc:
            return False, f"cannot read {cfg_path}: {exc}"
        try:
            data = tomllib.loads(text) if text.strip() else {}
        except tomllib.TOMLDecodeError as exc:
            return False, f"cannot parse {cfg_path}: {exc}"
        servers = data.get(CODEX_MCP_ROOT)
        if not isinstance(servers, dict):
            return False, f"{cfg_path}: no [{CODEX_MCP_ROOT}] table — nothing to unregister"

        removed: list[str] = []
        kept_foreign: list[str] = []
        cuts: list[tuple[int, int]] = []
        for key in (MCP_SERVER_KEY, MCP_LEGACY_SERVER_KEY):
            entry = servers.get(key)
            if not isinstance(entry, dict):
                continue
            if not self._mcp_entry_is_ours(entry):
                kept_foreign.append(key)
                continue
            span = _toml_locate_table(text, f"{CODEX_MCP_ROOT}.{key}")
            if span is None:
                return False, (
                    f"{cfg_path}: [{CODEX_MCP_ROOT}.{key}] is not a plain TOML table "
                    "(inline or dotted form) — manual removal required"
                )
            cuts.append(span)
            removed.append(key)
        if not removed:
            note = "no vesma-owned MCP entry found"
            if kept_foreign:
                note += f" (foreign entry under {', '.join(kept_foreign)} kept untouched)"
            return False, note

        merged = text
        for start, end in sorted(cuts, reverse=True):
            merged = merged[:start] + merged[end:]

        ok, problem = self._validate_toml_merge(data, servers, merged)
        if not ok:
            return False, f"{cfg_path}: {problem}"
        try:
            _atomic_write_text(cfg_path, merged)
        except OSError as exc:
            return False, f"cannot write {cfg_path}: {exc}"
        note = f"MCP entry removed from {cfg_path}: {', '.join(removed)}"
        if kept_foreign:
            note += f"; foreign entry under {', '.join(kept_foreign)} kept untouched"
        return True, note

    # ── MCP: JSON-merge registration (zcode / agents) ─────────────────────────

    def _register_mcp_json(self, target: Target, *, mnemos_bin: str | None) -> tuple[bool, str]:
        """Merge the ``vesma`` server entry into the target's JSON config.

        The merge is additive: unknown top-level keys and other MCP servers
        are preserved untouched. An existing entry keeps its user-tuned
        ``env`` values (only missing keys are filled in). A legacy ``mnemos``
        key written by pre-rebrand packs is migrated to ``vesma`` in the
        same pass (stamp-migration window discipline).
        """
        cfg_path = target.mcp_config
        assert cfg_path is not None  # guaranteed by register_mcp dispatch
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"cannot read {cfg_path}: {exc}"
        if not isinstance(data, dict):
            return False, f"{cfg_path}: expected a JSON object at top level"

        if target.mcp_format == "zcode":
            servers = data.setdefault("mcp", {}).setdefault("servers", {})
        elif target.mcp_format == "opencode":
            # OpenCode: the "mcp" key maps server names DIRECTLY to entries
            # ({"type": "local", "command": [...]}) — no "servers" level.
            servers = data.setdefault("mcp", {})
        else:
            servers = data.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            return False, f"{cfg_path}: server map is not an object"

        # Migration: legacy-key entry moves to the brand-primary key.
        if MCP_SERVER_KEY not in servers and isinstance(servers.get(MCP_LEGACY_SERVER_KEY), dict):
            servers[MCP_SERVER_KEY] = servers.pop(MCP_LEGACY_SERVER_KEY)

        existing = servers.get(MCP_SERVER_KEY)
        if not isinstance(existing, dict):
            existing = None
        if target.mcp_format == "opencode":
            servers[MCP_SERVER_KEY] = self._mcp_entry_opencode(mnemos_bin, existing)
        else:
            servers[MCP_SERVER_KEY] = self._mcp_entry(mnemos_bin, existing)

        try:
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(cfg_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        except OSError as exc:
            return False, f"cannot write {cfg_path}: {exc}"
        return True, f"MCP server registered in {cfg_path}"

    # ── MCP: unregistration (SEC-major #2, ArchCom 2026-10-01) ─────────────────

    @staticmethod
    def _mcp_entry_is_ours(entry: Any) -> bool:
        """Evidence check that an MCP server entry was written by this pack.

        An entry is ours when its command resolves to the memory-server
        binary (``vesma``/``mnemos`` basename) or its argv carries the
        ``mcp-server`` subcommand. A foreign entry that merely REUSES the
        ``vesma`` server key but points elsewhere does NOT match and is
        never touched by :meth:`unregister_mcp`.
        """
        if not isinstance(entry, dict):
            return False
        command = entry.get("command")
        argv: list[str] = []
        if isinstance(command, str):
            argv.append(command)
            argv.extend(str(a) for a in entry.get("args", []) if isinstance(a, str))
        elif isinstance(command, list):
            argv.extend(str(a) for a in command if isinstance(a, (str, int)))
        if not argv:
            return False
        exe = Path(argv[0]).name.lower()
        exe = exe.removesuffix(".exe")
        ours_bin = exe in {"vesma", "mnemos"}
        subcommand = "mcp-server" in argv[1:]
        return ours_bin or subcommand

    def unregister_mcp(self, target_name: str | None = None) -> tuple[bool, str]:
        """Remove the MCP server entry THIS pack registered (SEC-major #2).

        The reverse of :meth:`register_mcp` for JSON-merge targets (zcode,
        agents, cursor, claude-code, windsurf, opencode) and, via the TOML
        engine, the codex target. Ownership rules:

        * only the pack's server keys (``vesma``, legacy ``mnemos``) are
          considered;
        * a key is removed only when the entry passes the
          :meth:`_mcp_entry_is_ours` evidence check — a foreign tool that
          registered a server under the same key keeps its entry;
        * every other server entry and every other config key is preserved
          as data.

        The Pi target needs no unregistration here (its bridge extension is
        a stamped file removed by :meth:`uninstall`); the legacy VS Code
        script path manages its own registration and is not touched.
        """
        target = self.targets.get(target_name) if target_name else None
        if target is not None and target.mcp_format == "codex":
            return self._unregister_mcp_toml(target)
        if target is None or target.mcp_config is None or target.mcp_format in ("pi",):
            return False, "no JSON MCP registration known for this target — nothing to unregister"

        cfg_path = target.mcp_config
        if not cfg_path.exists():
            return False, f"{cfg_path} does not exist — nothing to unregister"
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"cannot read {cfg_path}: {exc}"
        if not isinstance(data, dict):
            return False, f"{cfg_path}: expected a JSON object at top level"

        if target.mcp_format == "zcode":
            mcp = data.get("mcp")
            servers = mcp.get("servers") if isinstance(mcp, dict) else None
        elif target.mcp_format == "opencode":
            servers = data.get("mcp") if isinstance(data.get("mcp"), dict) else None
        else:
            servers = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else None
        if servers is None:
            return False, f"{cfg_path}: no server map — nothing to unregister"

        removed: list[str] = []
        kept_foreign: list[str] = []
        for key in (MCP_SERVER_KEY, MCP_LEGACY_SERVER_KEY):
            if key not in servers:
                continue
            if self._mcp_entry_is_ours(servers[key]):
                del servers[key]
                removed.append(key)
            else:
                kept_foreign.append(key)
        if not removed:
            note = "no vesma-owned MCP entry found"
            if kept_foreign:
                note += f" (foreign entry under {', '.join(kept_foreign)} kept untouched)"
            return False, note

        # Drop now-empty containers so we do not leave structural litter.
        if target.mcp_format == "zcode":
            mcp = data.get("mcp")
            if isinstance(mcp, dict) and not mcp.get("servers"):
                mcp.pop("servers", None)
                if not mcp:
                    data.pop("mcp", None)
        elif target.mcp_format == "opencode":
            if isinstance(data.get("mcp"), dict) and not data["mcp"]:
                data.pop("mcp", None)
        else:
            if isinstance(data.get("mcpServers"), dict) and not data["mcpServers"]:
                data.pop("mcpServers", None)

        try:
            _atomic_write_text(cfg_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        except OSError as exc:
            return False, f"cannot write {cfg_path}: {exc}"
        note = f"MCP entry removed from {cfg_path}: {', '.join(removed)}"
        if kept_foreign:
            note += f"; foreign entry under {', '.join(kept_foreign)} kept untouched"
        return True, note

    def _mcp_entry(self, mnemos_bin: str | None, existing: dict[str, Any] | None) -> dict[str, Any]:
        """Build the stdio server entry, preserving user tuning where present.

        Env defaults mirror ``mcp-setup.sh``: ``<home>/.mnemos/{data,vault}``.
        A pre-existing entry keeps its env verbatim, so cross-layout installs
        never clobber tuned paths.
        """
        bin_path = self._resolve_mnemos_bin(mnemos_bin)
        env = self._mcp_env_defaults()
        entry = dict(existing) if isinstance(existing, dict) else {}
        raw_env = entry.get("env")
        kept_env: dict[str, Any] = raw_env if isinstance(raw_env, dict) else {}
        for key, value in env.items():
            kept_env.setdefault(key, value)
        entry["env"] = kept_env
        entry.update({"type": "stdio", "command": bin_path, "args": ["mcp-server"]})
        return entry

    def _resolve_mnemos_bin(self, mnemos_bin: str | None) -> str:
        """Explicit bin > ``which`` > the installer's well-known venv path."""
        return mnemos_bin or shutil.which("mnemos") or str(self.home / ".mnemos/venv/bin/mnemos")

    def _mcp_env_defaults(self) -> dict[str, str]:
        """Env defaults shared by every MCP entry shape (mirror mcp-setup.sh)."""
        return {
            "VESMARO_DATA_DIR": str(self.home / ".mnemos/data"),
            "VESMARO_VAULT__VAULT_PATH": str(self.home / ".mnemos/vault"),
        }

    def _mcp_entry_opencode(
        self, mnemos_bin: str | None, existing: dict[str, Any] | None
    ) -> dict[str, Any]:
        """Build the OpenCode local server entry, preserving user tuning.

        Shape (OpenCode ``opencode.json``): ``{"type": "local", "command":
        ["mnemos", "mcp-server"], "enabled": true, "environment": {...}}`` —
        the command is ONE argv array (unlike the split ``command``/``args``
        of the ``mcpServers`` formats) and env vars ride the ``environment``
        key. Env defaults mirror :meth:`_mcp_entry`.
        """
        bin_path = self._resolve_mnemos_bin(mnemos_bin)
        env = self._mcp_env_defaults()
        entry = dict(existing) if isinstance(existing, dict) else {}
        raw_env = entry.get("environment")
        kept_env: dict[str, Any] = raw_env if isinstance(raw_env, dict) else {}
        for key, value in env.items():
            kept_env.setdefault(key, value)
        entry["environment"] = kept_env
        entry.update({"type": "local", "command": [bin_path, "mcp-server"], "enabled": True})
        return entry

    # ── MCP: legacy script registration (VS Code) ─────────────────────────────

    def _register_mcp_script(self, mnemos_bin: str | None = None) -> tuple[bool, str]:
        """Invoke ``mcp-setup.sh`` to register the MCP server in VS Code.

        Returns ``(success, note)``. This is a thin wrapper — the heavy
        lifting lives in the shell script. We call it rather than reimplement
        the JSON merging to avoid drift.
        """
        import subprocess  # nosec B404 — used for trusted local mcp-setup.sh, not untrusted input

        script = self._find_mcp_setup_script()
        if script is None:
            return False, "mcp-setup.sh not found (not in wheel, not in source tree)"

        cmd: list[str] = ["bash", str(script)]
        if mnemos_bin:
            cmd += ["--command", mnemos_bin]

        try:
            proc = subprocess.run(  # nosec B603 — runs trusted local mcp-setup.sh with list args
                cmd,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except FileNotFoundError as exc:
            return False, f"bash not available: {exc}"
        except subprocess.TimeoutExpired:
            return False, "mcp-setup.sh timed out after 60s"

        if proc.returncode == 0:
            return True, "MCP server registered"
        return False, f"mcp-setup.sh exited {proc.returncode}: {proc.stderr.strip()[:200]}"

    # ── Full setup ─────────────────────────────────────────────────────────────

    def setup(
        self,
        target_name: str,
        *,
        dry_run: bool = False,
        register_mcp: bool = True,
        mnemos_bin: str | None = None,
    ) -> DeployResult:
        """Unified setup: deploy files + register MCP + verify summary.

        This is the single entry point per owner request — ``vesma
        integration setup`` calls this for each detected target.
        """
        result = self.deploy(target_name, dry_run=dry_run)

        if register_mcp and not dry_run:
            ok, note = self.register_mcp(target_name=target_name, mnemos_bin=mnemos_bin)
            result.mcp_registered = ok
            result.mcp_note = note

        return result


def detect_all(config: TargetsConfig | None = None) -> list[Target]:
    """Return all detected targets (convenience for CLI)."""
    cfg = config or load_targets()
    return list(cfg.detected())


def deployable_targets(config: TargetsConfig | None = None) -> Sequence[str]:
    """Return names of all targets defined in the config."""
    cfg = config or load_targets()
    return [t.name for t in cfg.targets]


# ── Engine manifest (memory-switch protocol MS-0, ADR-0034) ──────────────────


def load_engine_manifest(pack_root: Path | None = None) -> dict[str, Any]:
    """Load and validate the shipped engine manifest (ADR-0034, MS-0).

    The manifest (``integrations/engine-manifest.yaml``) is the engine-side
    identity card for the memory-switch protocol — generalized from the
    Hermes ``plugin.yaml``. It is a PLAIN pack file: it ships inside the
    wheel (the ``integrations/`` force-include) but describes the ENGINE,
    so it is deliberately never deployed into harness directories.

    The ``version`` field is NOT hardcoded in the YAML: this loader injects
    the runtime package version (``vesmaro.__version__``) so the manifest
    can never drift from the installed engine.

    Args:
        pack_root: Explicit pack root. Defaults to the same resolution the
            IntegrationManager uses (source tree → wheel → upward search).

    Returns:
        The validated manifest dict with ``version`` injected.

    Raises:
        FileNotFoundError: if the manifest is not shipped.
        ValueError: if required keys are missing or a declared precedence
            mode is not a recognized value.
    """
    root = pack_root or IntegrationManager._default_pack_root()
    path = root / ENGINE_MANIFEST_NAME
    if not path.is_file():
        raise FileNotFoundError(f"engine manifest not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{ENGINE_MANIFEST_NAME}: expected a mapping at top level")

    required = ("name", "capabilities", "mcp", "precedence_modes", "attach_points")
    missing = [key for key in required if key not in raw]
    if missing:
        raise ValueError(f"{ENGINE_MANIFEST_NAME}: missing required keys: {', '.join(missing)}")

    # Single source of truth for the engine version is the package.
    import vesmaro

    raw["version"] = vesmaro.__version__

    modes = raw["precedence_modes"]
    if not isinstance(modes, list) or not modes:
        raise ValueError(f"{ENGINE_MANIFEST_NAME}: 'precedence_modes' must be a non-empty list")
    for mode in modes:
        if mode not in PRECEDENCE_MODES_KNOWN:
            known = ", ".join(sorted(PRECEDENCE_MODES_KNOWN))
            raise ValueError(
                f"{ENGINE_MANIFEST_NAME}: unknown precedence mode {mode!r} (known: {known})"
            )

    if not isinstance(raw["capabilities"], list) or not raw["capabilities"]:
        raise ValueError(f"{ENGINE_MANIFEST_NAME}: 'capabilities' must be a non-empty list")
    if not isinstance(raw["attach_points"], dict) or not raw["attach_points"]:
        raise ValueError(f"{ENGINE_MANIFEST_NAME}: 'attach_points' must be a non-empty mapping")
    if not isinstance(raw["mcp"], dict) or not raw["mcp"]:
        raise ValueError(f"{ENGINE_MANIFEST_NAME}: 'mcp' must be a non-empty mapping")

    return raw
