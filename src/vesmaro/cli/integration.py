"""Mnemos integration layer — deploy instructions/skills/prompts to agent harnesses.

This module is the engine behind the `mnemos util-*` CLI subcommands. It:

* Detects installed agent harnesses (Copilot, generic Copilot, Cursor) via
  ``integrations/targets.yaml``.
* Deploys the shipped pack (``integrations/{instructions,skills,prompts}/``)
  into each detected harness, stamping every file with a version header so
  later runs can detect stale files and safely uninstall only our own.
* Injects the always-on behavioral pack (``agents_md`` kind) as a stamped
  BEGIN/END block INTO the user's ``AGENTS.md``-standard file (targets
  ``agents``, ``zcode``, ``opencode``) — user content around the block is
  never touched.
* Verifies deployed files against the current package version.
* Updates stale files in place.
* Uninstalls only stamped files — never user-created content.
* Deploys the canon JSON Schemas (``schemas`` kind) BYTE-IDENTICAL —
  never inline-stamped, because a schema file must stay valid JSON for
  schema validators; ownership is recorded in a sidecar manifest.

The version stamp is a Markdown HTML comment on the first non-shebang line::

    <!-- mnemos-integration: v2.0.0 -->

This is invisible in rendered Markdown but trivially greppable.

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
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "AGENTS_MD_BLOCK_RE",
    "SCHEMAS_MANIFEST_NAME",
    "SCHEMAS_SOURCE_PIN",
    "ArtefactKind",
    "DeployResult",
    "DeployStatus",
    "IntegrationManager",
    "Target",
    "TargetsConfig",
    "VerifyResult",
    "load_targets",
    "read_agents_md_version",
    "render_agents_md_block",
    "schemas_manifest",
    "strip_agents_md_block",
]

# ── Constants ─────────────────────────────────────────────────────────────────

#: The stamp injected into every deployed file (first useful line).
STAMP_PATTERN = re.compile(r"<!--\s*mnemos-integration:\s*v(\S+?)\s*-->")

#: Paired block markers for the ``agents_md`` deployment kind. Unlike file
#: stamps, an ``agents_md`` deployment lives INSIDE a user-owned file (an
#: ``AGENTS.md``-standard standing-instructions file), so the injected region
#: is wrapped in paired BEGIN/END comments and only that region is ever
#: mutated — user content around it is preserved byte-for-byte.
AGENTS_MD_BLOCK_RE = re.compile(
    r"<!--\s*mnemos:integration:v(?P<version>\S+?)\s+BEGIN\s*-->\r?\n"
    r"(?P<body>.*?)"
    r"<!--\s*mnemos:integration:v(?P<end_version>\S+?)\s+END\s*-->\r?\n?",
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
#: raw text carries the ``mnemos-integration`` marker), which keeps
#: verify/uninstall ownership detection uniform across kinds.
SCHEMAS_MANIFEST_NAME = "mnemos-schemas.manifest.json"

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

    ``home`` lets ``mnemos integration setup --home <dir>`` deploy into a
    foreign environment (another container's home, a dotfiles repo, …)
    without rewriting targets.yaml.
    """
    if path.startswith("~") and home is not None:
        rest = path[1:].lstrip("/")
        return home / rest if rest else home
    return Path(path).expanduser()


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
        targets.append(
            Target(
                name=name,
                detect_paths=detect_paths,
                deploy_map=deploy_map,
                format=fmt,
                layout=str(spec.get("layout", "flat")),
                mcp_config=mcp_config,
                mcp_format=mcp_format,
            )
        )

    return TargetsConfig(targets=tuple(targets))


# ── Version stamping ──────────────────────────────────────────────────────────


def make_stamp(version: str) -> str:
    """Build the stamp comment for a given version."""
    return f"<!-- mnemos-integration: v{version} -->"


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
    """Extract the version from a stamped file, or ``None`` if unstamped."""
    match = STAMP_PATTERN.search(content)
    return match.group(1) if match else None


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

    The result always ends with a newline so appending further user content
    (or a future block refresh) never glues onto the END marker.
    """
    body = content if content.endswith("\n") else content + "\n"
    return (
        f"<!-- mnemos:integration:v{version} BEGIN -->\n"
        f"{body}"
        f"<!-- mnemos:integration:v{version} END -->\n"
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
    def missing_count(self) -> int:
        return sum(1 for f in self.files if f.status == DeployStatus.MISSING)


@dataclass
class UninstallResult:
    """Aggregate result of an uninstall operation."""

    target_name: str
    removed: list[Path] = field(default_factory=list)
    skipped_user_files: list[Path] = field(default_factory=list)


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
        ``mnemos-schemas.manifest.json`` (stamped, JSON body) written into
        the same directory. Idempotency contract per file: CURRENT when
        the deployed bytes are identical AND the manifest carries the
        current version; UPDATED when bytes match but the manifest is
        stale (version bump re-deploy); DEPLOYED for new files.
        """
        results: list[FileResult] = []

        manifest_path = dest_dir / SCHEMAS_MANIFEST_NAME
        existing_manifest_version: str | None = None
        if manifest_path.exists():
            existing_manifest_version = read_stamp(
                manifest_path.read_text(encoding="utf-8", errors="replace")
            )

        checksums = self._schema_checksums(files)
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

        # Refresh the manifest whenever its version is stale or absent.
        if existing_manifest_version != self.version and not dry_run:
            dest_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(manifest_path, manifest)

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
        """Deploy a single file, returning the outcome."""
        content = src.read_text(encoding="utf-8")
        stamped = stamp_content(
            content, self.version, line_comment=src.suffix in LINE_COMMENT_SUFFIXES
        )

        if dest.exists():
            existing = dest.read_text(encoding="utf-8")
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

        for kind, files in self._all_pack_files().items():
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

            # Track which dest paths correspond to pack files.
            seen_dests: set[Path] = set()
            for src in files:
                rel = src.relative_to(self.pack_root / kind.value)
                dest = target.dest_for(kind.value, rel)
                seen_dests.add(dest)
                result.files.append(self._verify_file(src, dest))

            # Scan for extra files in the deploy dir (user files or stale mnemos files).
            if dest_dir.exists():
                for path in sorted(dest_dir.rglob("*")):
                    if not path.is_file() or path in seen_dests:
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
                                note="user file — not managed by mnemos",
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
                note="no mnemos block in file — not injected yet",
            )
        if deployed_version != self.version:
            return FileResult(
                source=source,
                destination=dest,
                status=DeployStatus.STALE,
                deployed_version=deployed_version,
                note=f"block v{deployed_version} != current v{self.version}",
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

        existing = dest.read_text(encoding="utf-8")
        deployed_version = read_stamp(existing)
        if deployed_version is None:
            return FileResult(
                source=src,
                destination=dest,
                status=DeployStatus.SKIPPED,
                note="no mnemos stamp — user file, not ours",
            )
        if deployed_version != self.version:
            return FileResult(
                source=src,
                destination=dest,
                status=DeployStatus.STALE,
                deployed_version=deployed_version,
                note=f"deployed v{deployed_version} != current v{self.version}",
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
        manifest_version: str | None = None
        if manifest_path.exists():
            manifest_version = read_stamp(
                manifest_path.read_text(encoding="utf-8", errors="replace")
            )
        else:
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
        if manifest_path.exists():
            if manifest_version is None:
                results.append(
                    FileResult(
                        source=Path("<schemas-manifest>"),
                        destination=manifest_path,
                        status=DeployStatus.SKIPPED,
                        note="manifest carries no mnemos stamp — not ours",
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
            else:
                results.append(
                    FileResult(
                        source=Path("<schemas-manifest>"),
                        destination=manifest_path,
                        status=DeployStatus.CURRENT,
                        deployed_version=self.version,
                    )
                )

        seen_dests: set[Path] = {manifest_path}
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
                            note="user file — not managed by mnemos",
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

        for kind, files in self._all_pack_files().items():
            if kind is ArtefactKind.AGENTS_MD:
                # The block lives inside a shared user file — orphan removal
                # does not apply (update refreshes it in place instead).
                continue
            dest_dir = target.deploy_map.get(kind.value)
            if dest_dir is None or not dest_dir.exists():
                continue

            # Build the set of dest paths the pack expects (same mapping as deploy).
            expected_dests: set[Path] = set()
            if kind is ArtefactKind.SCHEMAS:
                # Schema files are flat copies; the stamped sidecar manifest
                # belongs to us even though it ships from no pack file.
                expected_dests = {dest_dir / src.name for src in files}
                expected_dests.add(dest_dir / SCHEMAS_MANIFEST_NAME)
            else:
                for src in files:
                    rel = src.relative_to(self.pack_root / kind.value)
                    expected_dests.add(target.dest_for(kind.value, rel))

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
        """Remove ONLY files carrying the mnemos-integration stamp.

        User-created files (no stamp) are never deleted. The method scans
        each deploy directory recursively for stamped files.
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

        return result

    def _uninstall_schemas(self, dest_dir: Path, result: UninstallResult, *, dry_run: bool) -> None:
        """Uninstall deployed schemas — manifest-owned, never user files.

        Ownership evidence hierarchy:

        1. The stamped sidecar manifest is always ours → removed.
        2. A deployed ``*.schema.json`` whose bytes match a pack schema is
           ours (schema files deploy byte-identical, they can never carry
           an inline stamp) → removed. A drifted/renamed file the manifest
           does not know about is NOT provably ours → skipped as a user
           file; the manifest checksums are the registry, so an edited
           copy survives uninstall (the safe direction).

        Empty parent dirs are cleaned up to the deploy root afterwards.
        """
        manifest_path = dest_dir / SCHEMAS_MANIFEST_NAME
        known_checksums: dict[str, str] = {}
        if manifest_path.exists():
            raw = manifest_path.read_text(encoding="utf-8", errors="replace")
            if read_stamp(raw) is not None:
                try:
                    payload = json.loads(raw[raw.index("{") :])
                except (ValueError, json.JSONDecodeError):
                    payload = None
                if isinstance(payload, dict) and isinstance(payload.get("files"), dict):
                    known_checksums = {
                        str(k): str(v) for k, v in payload["files"].items() if isinstance(v, str)
                    }

        # The manifest itself is stamped → ordinary ownership rule applies.
        if manifest_path.exists():
            if not dry_run:
                manifest_path.unlink()
            result.removed.append(manifest_path)

        pack_names = set(self._schema_checksums(self._pack_files(ArtefactKind.SCHEMAS)))
        for path in sorted(dest_dir.rglob("*.schema.json")):
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            # An inline stamp is proof of ownership in every other kind —
            # keep that invariant here (verify flags such orphans as
            # "safe to uninstall"; uninstall must agree). Pack-byte-identity
            # and manifest-checksum membership cover the unstamped copies
            # this kind actually deploys; anything else is a user file.
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            ours = (
                read_stamp(content) is not None
                or path.name in pack_names
                or digest in known_checksums.values()
            )
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

        Targets that declare ``mcp.config`` in targets.yaml (zcode, agents)
        are registered by an in-place JSON merge that preserves every other
        key in the file. Targets without one fall back to ``mcp-setup.sh``
        (VS Code ``mcp.json``), keeping the historical behaviour.
        """
        target = self.targets.get(target_name) if target_name else None
        if target is not None and target.mcp_format == "pi":
            return self._register_mcp_pi(target)
        if target is not None and target.mcp_config is not None:
            return self._register_mcp_json(target, mnemos_bin=mnemos_bin)
        return self._register_mcp_script(mnemos_bin=mnemos_bin)

    # ── MCP: Pi extension bridge ──────────────────────────────────────────────

    def _register_mcp_pi(self, target: Target) -> tuple[bool, str]:
        """ "Register" MCP for Pi by confirming the bridge extension is deployed.

        Pi has no MCP config file to merge into: TypeScript extensions ARE
        the tool surface. Registration therefore reduces to verifying that
        the stamped bridge (``integrations/extensions/mnemos-mcp.ts``) sits
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

    # ── MCP: JSON-merge registration (zcode / agents) ─────────────────────────

    def _register_mcp_json(self, target: Target, *, mnemos_bin: str | None) -> tuple[bool, str]:
        """Merge a ``mnemos`` server entry into the target's JSON config.

        The merge is additive: unknown top-level keys and other MCP servers
        are preserved untouched. An existing ``mnemos`` entry keeps its
        user-tuned ``env`` values (only missing keys are filled in).
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

        existing = servers.get("mnemos")
        if target.mcp_format == "opencode":
            servers["mnemos"] = self._mcp_entry_opencode(mnemos_bin, existing)
        else:
            servers["mnemos"] = self._mcp_entry(mnemos_bin, existing)

        try:
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(cfg_path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        except OSError as exc:
            return False, f"cannot write {cfg_path}: {exc}"
        return True, f"MCP server registered in {cfg_path}"

    def _mcp_entry(self, mnemos_bin: str | None, existing: dict[str, Any] | None) -> dict[str, Any]:
        """Build the stdio server entry, preserving user tuning where present.

        Env defaults mirror ``mcp-setup.sh``: ``<home>/.mnemos/{data,vault}``.
        A pre-existing ``mnemos`` entry keeps its env verbatim, so cross-layout
        installs never clobber tuned paths.
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

        This is the single entry point per owner request — ``mnemos util-setup``
        calls this for each detected target.
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
