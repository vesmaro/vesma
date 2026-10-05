"""Component manifest loading and validation (contract component-manifest v1).

Implements the reader side of ``specs/component-manifest/v1/spec.md``
(``1.0.0-draft.2``): strict JSON-Schema 2020-12 validation against the
vendored schema plus the normative §3 rules that carry their own §4 error
codes. Design points:

- fail-closed: any problem raises :class:`ManifestError`; nothing is
  silently skipped or defaulted;
- dedicated-code checks run BEFORE the JSON-Schema pass so the reported
  code is the contract code (DURATION_INVALID, SHELL_IN_ARGV, ...), not a
  generic schema failure; precise-JSON-path diagnostics that the schema
  cannot express (core-tier artifact hash, restart clamps) also run early;
- ``load_installation`` is fail-closed per layout §3.5: EVERY file in the
  manifests directory must be a valid manifest (stray non-YAML files are
  rejected, never silently skipped), name uniqueness and depends_on
  acyclicity are checked across the directory.

The JSON-Schema file is VENDORED byte-identical from the specs repo (see
``SCHEMA_VENDORED_PROVENANCE``); the normative source for both schema and
rules stays the specs repo.
"""

from __future__ import annotations

import dataclasses
import importlib.resources
import json
import logging
import os
import re
from importlib.resources import files as resource_files
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from vesmaro.service import layout
from vesmaro.service.errors import (
    APIVERSION_UNSUPPORTED,
    CLAMP_VIOLATION,
    DEPENDS_CYCLE,
    DEPENDS_MISSING,
    DURATION_INVALID,
    ENV_FILE_UNSAFE,
    MANIFEST_SCHEMA_INVALID,
    NAME_DUPLICATED,
    PLACEHOLDER_UNKNOWN,
    SECRET_IN_VARS,
    SHELL_IN_ARGV,
    ManifestError,
)

logger = logging.getLogger("vesmaro.service.manifest")

#: Contract version of the vendored schema (specs repo, draft.2).
SCHEMA_CONTRACT_VERSION = "1.0.0-draft.2"

#: Provenance of the vendored schema file
#: (``vesmaro/service/schemas/component-manifest.schema.json``): a
#: byte-identical copy of the specs repo file
#: ``specs/component-manifest/v1/schema/component-manifest.schema.json``.
#: NO local edits (provenance lives here, not in a ``$comment`` key) so a
#: re-vendor diff against the specs file stays empty: copy verbatim, bump
#: ``SCHEMA_CONTRACT_VERSION``, update this constant.
SCHEMA_VENDORED_PROVENANCE = (
    "specs repo vesma-specs @ d30e668a9ebdfe32274fc08b30d3be18862ec602, "
    "file specs/component-manifest/v1/schema/component-manifest.schema.json "
    "(last touched by 7419679e5c44242bdb5b4e66aad23946191dad8a), contract "
    "component-manifest 1.0.0-draft.2; byte-identical copy — re-vendor by "
    "verbatim copy + SCHEMA_CONTRACT_VERSION bump"
)

#: apiVersion values this loader accepts (CM §3.2: rejections MUST name
#: the supported set).
SUPPORTED_API_VERSIONS: tuple[str, ...] = ("vesma.component/v1",)

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_RESERVED_NAMES = {"venv", "venvs"}
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[a-zA-Z-][0-9a-zA-Z-]*))*))?"
    r"(?:\+([0-9a-zA-Z-]+(?:\.[0-9a-zA-Z-]+)*))?$"
)
_HTTPS_URL_RE = re.compile(r"^https://\S+$")
_SPDX_RE = re.compile(r"^[A-Za-z0-9.-]+(\+[A-Za-z0-9.-]+)?$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_DURATION_RE = re.compile(r"^([0-9]+)(ms|s|m|h)$")
_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")

#: CM §2 allowlist — the ONLY expandable argv placeholders.
PLACEHOLDER_ALLOWLIST: frozenset[str] = frozenset(
    {"config_path", "data_dir", "runtime_dir", "venv_bin"}
)

#: CM §3.5: shell metacharacters forbidden in argv elements.
_SHELL_META_CHARS = frozenset("|&;<>()$`\\\"'*?")
_SHELL_BASENAMES = frozenset(
    {"sh", "bash", "dash", "ash", "zsh", "ksh", "busybox", "cmd", "powershell"}
)
_SHELL_FLAGS = frozenset({"-c", "-lc"})

#: CM §3.5: secret-like substrings banned from env.vars key names.
_SECRET_KEY_TOKENS = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "private_key",
    "credential",
)

#: Duration-bearing leaves (section path -> leaf names), mirroring CM
#: §3.5/3.7/3.8/3.10 and the specs conformance runner's vocabulary.
_DURATION_LEAVES: dict[tuple[str, ...], tuple[str, ...]] = {
    ("health", "http"): ("interval", "timeout"),
    ("health", "tcp"): ("interval", "timeout"),
    ("health", "exec"): ("interval", "timeout"),
    ("health", "startup"): ("grace", "interval", "timeout"),
    ("stop",): ("grace_period",),
    ("restart", "backoff"): ("base", "max", "reset_after"),
    ("restart", "window"): ("per",),
}

# Restart clamps (CM §3.10): base >= 500ms, max <= 5min, attempts >= 3.
_RESTART_BASE_MIN_MS = 500
_RESTART_MAX_MAX_MS = 5 * 60 * 1000
_RESTART_ATTEMPTS_MIN = 3

_DURATION_MULTIPLIERS_MS = {"ms": 1, "s": 1000, "m": 60_000, "h": 3_600_000}

_MANIFEST_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ── Schema access ─────────────────────────────────────────────────────


def _format_json_path(parts: tuple[Any, ...]) -> str:
    rendered = "$"
    for part in parts:
        rendered += f"[{part}]" if isinstance(part, int) else f".{part}"
    return rendered


def _schema_validator() -> jsonschema.Draft202012Validator:
    """Draft-2020-12 validator over the vendored contract schema."""
    schema_text = (
        resource_files("vesmaro")
        .joinpath("service/schemas/component-manifest.schema.json")
        .read_text(encoding="utf-8")
    )
    schema = json.loads(schema_text)
    return jsonschema.Draft202012Validator(schema)


# ── Duration helper ───────────────────────────────────────────────────


def duration_to_ms(value: str, *, field_path: str = "$") -> int:
    """Parse a contract duration (``500ms`` / ``10s`` / ``5m`` / ``1h``) to ms.

    Raises ManifestError(DURATION_INVALID) for anything outside
    ``^[0-9]+(ms|s|m|h)$`` — composite values and bare numbers are invalid.
    """
    match = _DURATION_RE.match(value) if isinstance(value, str) else None
    if match is None:
        raise ManifestError(
            DURATION_INVALID,
            field_path,
            f"duration {value!r} does not match ^[0-9]+(ms|s|m|h)$ "
            "(composite values like 1h30m and bare numbers are invalid)",
        )
    return int(match.group(1)) * _DURATION_MULTIPLIERS_MS[match.group(2)]


# ── Data model (mirror of the manifest sections) ──────────────────────


@dataclasses.dataclass(frozen=True)
class Provenance:
    repo: str
    license: str
    artifact_sha256: str | None = None


@dataclasses.dataclass(frozen=True)
class Metadata:
    name: str
    version: str
    tier: str
    description: str
    provenance: Provenance


@dataclasses.dataclass(frozen=True)
class Env:
    vars: dict[str, str]
    env_file: str | None = None


@dataclasses.dataclass(frozen=True)
class Launch:
    argv: list[str]
    cwd: str | None = None
    env: Env | None = None


@dataclasses.dataclass(frozen=True)
class InProcess:
    module: str
    entrypoint: str
    python_version: str | None = None


@dataclasses.dataclass(frozen=True)
class HttpProbe:
    url: str
    interval: str
    timeout: str
    unhealthy_threshold: int


@dataclasses.dataclass(frozen=True)
class TcpProbe:
    host: str
    port: int
    interval: str
    timeout: str
    unhealthy_threshold: int


@dataclasses.dataclass(frozen=True)
class ExecProbe:
    argv: list[str]
    interval: str
    timeout: str
    unhealthy_threshold: int


@dataclasses.dataclass(frozen=True)
class Startup:
    grace: str
    interval: str
    timeout: str


@dataclasses.dataclass(frozen=True)
class Health:
    checker: str
    http: HttpProbe | None = None
    tcp: TcpProbe | None = None
    exec: ExecProbe | None = None
    callback: str | None = None
    startup: Startup | None = None


@dataclasses.dataclass(frozen=True)
class Stop:
    signal: str
    grace_period: str


@dataclasses.dataclass(frozen=True)
class Config:
    schema_file: str | None = None
    schema_inline: dict[str, Any] | None = None


@dataclasses.dataclass(frozen=True)
class Backoff:
    base: str
    max: str
    reset_after: str


@dataclasses.dataclass(frozen=True)
class Window:
    attempts: int
    per: str


@dataclasses.dataclass(frozen=True)
class Restart:
    backoff: Backoff | None = None
    window: Window | None = None


@dataclasses.dataclass(frozen=True)
class ComponentManifest:
    """Typed mirror of one validated component manifest."""

    path: Path
    api_version: str
    kind: str
    metadata: Metadata
    launch: Launch | None = None
    in_process: InProcess | None = None
    health: Health | None = None
    stop: Stop | None = None
    config: Config | None = None
    restart: Restart | None = None
    depends_on: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return self.metadata.name


# ── Builders (schema-validated shapes; defensive anyway) ──────────────


def _opt_dict(doc: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = doc.get(key)
    return value if isinstance(value, dict) else None


def _build_metadata(raw: dict[str, Any]) -> Metadata:
    prov_raw = raw.get("provenance") or {}
    provenance = Provenance(
        repo=str(prov_raw.get("repo", "")),
        license=str(prov_raw.get("license", "")),
        artifact_sha256=prov_raw.get("artifact_sha256"),
    )
    return Metadata(
        name=str(raw.get("name", "")),
        version=str(raw.get("version", "")),
        tier=str(raw.get("tier", "")),
        description=str(raw.get("description", "")),
        provenance=provenance,
    )


def _build_launch(raw: dict[str, Any]) -> Launch:
    env_raw = raw.get("env")
    env = None
    if isinstance(env_raw, dict):
        env = Env(
            vars={str(k): str(v) for k, v in (env_raw.get("vars") or {}).items()},
            env_file=env_raw.get("env_file"),
        )
    return Launch(
        argv=[str(a) for a in raw.get("argv", [])],
        cwd=raw.get("cwd"),
        env=env,
    )


def _build_in_process(raw: dict[str, Any]) -> InProcess:
    python_raw = raw.get("python")
    python_version = python_raw.get("version") if isinstance(python_raw, dict) else None
    return InProcess(
        module=str(raw.get("module", "")),
        entrypoint=str(raw.get("entrypoint", "")),
        python_version=python_version,
    )


def _build_health(raw: dict[str, Any]) -> Health:
    http_raw = _opt_dict(raw, "http")
    tcp_raw = _opt_dict(raw, "tcp")
    exec_raw = _opt_dict(raw, "exec")
    startup_raw = _opt_dict(raw, "startup")
    return Health(
        checker=str(raw.get("checker", "")),
        http=HttpProbe(
            url=str(http_raw["url"]),
            interval=str(http_raw["interval"]),
            timeout=str(http_raw["timeout"]),
            unhealthy_threshold=int(http_raw["unhealthy_threshold"]),
        )
        if http_raw
        else None,
        tcp=TcpProbe(
            host=str(tcp_raw["host"]),
            port=int(tcp_raw["port"]),
            interval=str(tcp_raw["interval"]),
            timeout=str(tcp_raw["timeout"]),
            unhealthy_threshold=int(tcp_raw["unhealthy_threshold"]),
        )
        if tcp_raw
        else None,
        exec=ExecProbe(
            argv=[str(a) for a in exec_raw["argv"]],
            interval=str(exec_raw["interval"]),
            timeout=str(exec_raw["timeout"]),
            unhealthy_threshold=int(exec_raw["unhealthy_threshold"]),
        )
        if exec_raw
        else None,
        callback=raw.get("callback"),
        startup=Startup(
            grace=str(startup_raw["grace"]),
            interval=str(startup_raw["interval"]),
            timeout=str(startup_raw["timeout"]),
        )
        if startup_raw
        else None,
    )


def _build_restart(raw: dict[str, Any]) -> Restart:
    backoff_raw = _opt_dict(raw, "backoff")
    window_raw = _opt_dict(raw, "window")
    return Restart(
        backoff=Backoff(
            base=str(backoff_raw["base"]),
            max=str(backoff_raw["max"]),
            reset_after=str(backoff_raw["reset_after"]),
        )
        if backoff_raw
        else None,
        window=Window(attempts=int(window_raw["attempts"]), per=str(window_raw["per"]))
        if window_raw
        else None,
    )


# ── Normative checks (CM §3 rules that carry dedicated §4 codes) ──────


def _check_api_version(doc: dict[str, Any]) -> str:
    api_version = doc.get("apiVersion")
    if api_version not in SUPPORTED_API_VERSIONS:
        raise ManifestError(
            APIVERSION_UNSUPPORTED,
            "$.apiVersion",
            f"apiVersion {api_version!r} is not supported; "
            f"supported versions: {list(SUPPORTED_API_VERSIONS)}",
        )
    return str(api_version)


def _check_metadata_fields(doc: dict[str, Any]) -> None:
    """Precise-path diagnostics for metadata rules the schema also encodes.

    The JSON-Schema pass would reject these too (MANIFEST_SCHEMA_INVALID);
    running them first yields the exact field path and a readable message
    (CM §4: the receiver gets the field path, not a raw schema dump).
    """
    metadata = doc.get("metadata")
    if not isinstance(metadata, dict):
        return  # shape problems are the schema pass's domain
    name = metadata.get("name")
    if isinstance(name, str):
        if not _NAME_RE.match(name):
            raise ManifestError(
                MANIFEST_SCHEMA_INVALID,
                "$.metadata.name",
                f"name {name!r} does not match ^[a-z][a-z0-9-]{{0,62}}$",
            )
        if name in _RESERVED_NAMES:
            raise ManifestError(
                MANIFEST_SCHEMA_INVALID,
                "$.metadata.name",
                f"name {name!r} is reserved (venv directories of the layout, "
                "specs/layout/v1 §3.8); reserved names: "
                f"{sorted(_RESERVED_NAMES)}",
            )
    version = metadata.get("version")
    if isinstance(version, str) and not _SEMVER_RE.match(version):
        raise ManifestError(
            MANIFEST_SCHEMA_INVALID,
            "$.metadata.version",
            f"version {version!r} is not a valid SemVer 2.0.0 version",
        )
    description = metadata.get("description")
    if isinstance(description, str) and len(description) > 200:
        raise ManifestError(
            MANIFEST_SCHEMA_INVALID,
            "$.metadata.description",
            f"description is {len(description)} characters long (max 200)",
        )
    provenance = metadata.get("provenance")
    if not isinstance(provenance, dict):
        return
    repo = provenance.get("repo")
    if isinstance(repo, str) and not _HTTPS_URL_RE.match(repo):
        raise ManifestError(
            MANIFEST_SCHEMA_INVALID,
            "$.metadata.provenance.repo",
            f"repo {repo!r} must be an https URL of the component's public repository",
        )
    license_id = provenance.get("license")
    if isinstance(license_id, str) and not _SPDX_RE.match(license_id):
        raise ManifestError(
            MANIFEST_SCHEMA_INVALID,
            "$.metadata.provenance.license",
            f"license {license_id!r} is not of SPDX identifier form",
        )
    artifact_sha256 = provenance.get("artifact_sha256")
    if isinstance(artifact_sha256, str) and not _HEX64_RE.match(artifact_sha256):
        raise ManifestError(
            MANIFEST_SCHEMA_INVALID,
            "$.metadata.provenance.artifact_sha256",
            "artifact_sha256 must be hex64 (lowercase hex, 64 characters)",
        )
    tier = metadata.get("tier")
    if tier == "core" and not artifact_sha256:
        # CM §3.3: tier core REQUIRES the artifact hash (the schema's
        # if/then enforces it too; this gives the precise field path).
        raise ManifestError(
            MANIFEST_SCHEMA_INVALID,
            "$.metadata.provenance.artifact_sha256",
            "tier 'core' requires provenance.artifact_sha256 (hex64)",
        )


def _check_durations(doc: dict[str, Any]) -> None:
    for section_path, leaves in _DURATION_LEAVES.items():
        node: Any = doc
        for key in section_path:
            node = node.get(key) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            continue
        for leaf in leaves:
            if leaf in node:
                duration_to_ms(str(node[leaf]), field_path=f"$.{'.'.join(section_path)}.{leaf}")


def _check_shell_in_argv(argv: Any, where: str) -> None:
    if not isinstance(argv, list) or not all(isinstance(e, str) for e in argv):
        return  # non-list argv is the schema pass's domain
    for position, element in enumerate(argv):
        forbidden = sorted({c for c in element if c in _SHELL_META_CHARS or c.isspace()})
        if forbidden:
            raise ManifestError(
                SHELL_IN_ARGV,
                f"{where}[{position}]",
                f"argv element {element!r} contains forbidden character(s) "
                f"{''.join(forbidden)!r} (shell metacharacters and whitespace are "
                "forbidden, CM §3.5)",
            )
    if argv and os.path.basename(argv[0]) in _SHELL_BASENAMES:
        raise ManifestError(
            SHELL_IN_ARGV,
            f"{where}[0]",
            f"shell invocation (basename {os.path.basename(argv[0])!r}) is forbidden",
        )
    for position, element in enumerate(argv):
        if element in _SHELL_FLAGS:
            raise ManifestError(
                SHELL_IN_ARGV,
                f"{where}[{position}]",
                f"shell flag {element!r} is forbidden",
            )


def _check_argv(doc: dict[str, Any]) -> None:
    launch = _opt_dict(doc, "launch")
    if launch is not None:
        _check_shell_in_argv(launch.get("argv"), "$.launch.argv")
    health = _opt_dict(doc, "health")
    if health is not None:
        exec_raw = _opt_dict(health, "exec")
        if exec_raw is not None:
            _check_shell_in_argv(exec_raw.get("argv"), "$.health.exec.argv")


def _check_placeholders(argv: Any, where: str) -> None:
    if not isinstance(argv, list):
        return
    for position, element in enumerate(argv):
        if not isinstance(element, str):
            continue
        for match in _PLACEHOLDER_RE.finditer(element):
            token = match.group(1)
            if token not in PLACEHOLDER_ALLOWLIST:
                raise ManifestError(
                    PLACEHOLDER_UNKNOWN,
                    f"{where}[{position}]",
                    f"placeholder {{{token}}} is outside the allowlist "
                    f"{sorted(PLACEHOLDER_ALLOWLIST)}",
                )


def _check_placeholder_allowlist(doc: dict[str, Any]) -> None:
    launch = _opt_dict(doc, "launch")
    if launch is not None:
        _check_placeholders(launch.get("argv"), "$.launch.argv")
    health = _opt_dict(doc, "health")
    if health is not None:
        exec_raw = _opt_dict(health, "exec")
        if exec_raw is not None:
            _check_placeholders(exec_raw.get("argv"), "$.health.exec.argv")


def _check_secret_in_vars(doc: dict[str, Any]) -> None:
    launch = _opt_dict(doc, "launch")
    if launch is None:
        return
    env = _opt_dict(launch, "env")
    if env is None:
        return
    vars_map = env.get("vars")
    if not isinstance(vars_map, dict):
        return
    for key in vars_map:
        lowered = str(key).lower()
        if any(token in lowered for token in _SECRET_KEY_TOKENS):
            raise ManifestError(
                SECRET_IN_VARS,
                f"$.launch.env.vars.{key}",
                f"env.vars key {key!r} looks like a secret; secrets belong ONLY in "
                "env.env_file (CM §3.5) — delete the key and move the value into "
                "the env file",
            )


def _check_restart_clamps(doc: dict[str, Any]) -> None:
    restart = _opt_dict(doc, "restart")
    if restart is None:
        return
    backoff = _opt_dict(restart, "backoff")
    if backoff is not None:
        # Only clamp leaves that are PRESENT: a missing/ill-typed one is a
        # shape problem — the schema pass (required/type) reports it; these
        # pre-schema checks must not crash on partial sections.
        if "base" in backoff:
            base_path = "$.restart.backoff.base"
            base_ms = duration_to_ms(str(backoff["base"]), field_path=base_path)
            if base_ms < _RESTART_BASE_MIN_MS:
                raise ManifestError(
                    CLAMP_VIOLATION,
                    base_path,
                    f"backoff.base {backoff['base']!r} is below the 500ms clamp",
                )
        if "max" in backoff:
            max_path = "$.restart.backoff.max"
            max_ms = duration_to_ms(str(backoff["max"]), field_path=max_path)
            if max_ms > _RESTART_MAX_MAX_MS:
                raise ManifestError(
                    CLAMP_VIOLATION,
                    max_path,
                    f"backoff.max {backoff['max']!r} is above the 5min clamp",
                )
    window = _opt_dict(restart, "window")
    if window is not None:
        attempts = window.get("attempts")
        # bool is an int subclass — a YAML `true` is a schema type error,
        # not a clamp violation; absent/non-int attempts is the schema's
        # domain as well.
        if (
            isinstance(attempts, int)
            and not isinstance(attempts, bool)
            and attempts < _RESTART_ATTEMPTS_MIN
        ):
            raise ManifestError(
                CLAMP_VIOLATION,
                "$.restart.window.attempts",
                f"window.attempts {attempts!r} is below the minimum of {_RESTART_ATTEMPTS_MIN}",
            )


def _manifests_dir_of(path: Path) -> Path:
    return path.resolve().parent


def _is_inside(child: Path, parent: Path) -> bool:
    child_real = Path(os.path.realpath(child))
    parent_real = Path(os.path.realpath(parent))
    return child_real == parent_real or parent_real in child_real.parents


def _check_env_file_placement(doc: dict[str, Any], manifest_path: Path) -> None:
    launch = _opt_dict(doc, "launch")
    if launch is None:
        return
    env = _opt_dict(launch, "env")
    if env is None:
        return
    env_file = env.get("env_file")
    if not isinstance(env_file, str) or not env_file.strip():
        return  # shape is the schema pass's domain
    expanded = os.path.expanduser(env_file.strip())
    resolved = Path(
        expanded
        if os.path.isabs(expanded)
        else os.path.join(_manifests_dir_of(manifest_path), expanded)
    )
    # XDG-aware at call time (parity with the fail-closed envfile loader);
    # never a cached import-time path.
    canonical_components_dir = layout.components_dir()
    inside = []
    if _is_inside(resolved, _manifests_dir_of(manifest_path)):
        inside.append(f"the manifests directory ({_manifests_dir_of(manifest_path)})")
    if _is_inside(resolved, canonical_components_dir):
        inside.append(f"the canonical manifests directory ({canonical_components_dir})")
    if inside:
        raise ManifestError(
            ENV_FILE_UNSAFE,
            "$.launch.env.env_file",
            f"env_file {env_file!r} resolves to {resolved}, which is inside "
            f"{'; '.join(inside)} — env files must lie outside the manifests "
            "directory (CM §3.5)",
            fix_hint=f"move the file to ~/.config/vesma/env/<name>.env "
            f"(canonical place) and update the manifest: {env_file!r}",
        )


def _check_schema(doc: dict[str, Any]) -> None:
    errors = sorted(
        _schema_validator().iter_errors(doc),
        key=lambda e: list(e.absolute_path),
    )
    if not errors:
        return
    first = errors[0]
    path = _format_json_path(tuple(first.absolute_path))
    more = f" (+{len(errors) - 1} more schema error(s))" if len(errors) > 1 else ""
    raise ManifestError(
        MANIFEST_SCHEMA_INVALID,
        path,
        f"manifest violates the component-manifest schema: {first.message}{more}",
    )


def _check_depends(
    name: str,
    depends_on: tuple[str, ...],
    installation_names: set[str] | None,
) -> None:
    if installation_names is None:
        return
    for dep in depends_on:
        if dep not in installation_names:
            raise ManifestError(
                DEPENDS_MISSING,
                "$.depends_on",
                f"depends_on entry {dep!r} does not match any manifest of the "
                f"installation (known: {sorted(installation_names)})",
            )
    # Self-loops are detectable even for a single manifest; wider cycles are
    # the installation pass's domain.
    if name in depends_on:
        raise ManifestError(
            DEPENDS_CYCLE,
            "$.depends_on",
            f"depends_on cycle: {name} -> {name}",
        )


# ── Public API ────────────────────────────────────────────────────────


def load_manifest(
    path: str | os.PathLike[str],
    installation_names: set[str] | None = None,
) -> ComponentManifest:
    """Load and validate one manifest file; raise ManifestError on any violation.

    ``installation_names`` — names of the OTHER manifests of the same
    installation, when known: enables NAME_DUPLICATED and DEPENDS_MISSING
    checks. ``None`` (default) skips installation-context checks.
    """
    # expanduser for parity with the envfile loader — manifest paths may
    # legitimately arrive as `~/...` from operator config in later waves.
    manifest_path = Path(path).expanduser()
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(
            MANIFEST_SCHEMA_INVALID,
            "$",
            f"cannot read manifest file: {exc}",
        ) from exc
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(
            MANIFEST_SCHEMA_INVALID,
            "$",
            f"manifest is not parseable YAML: {exc}",
        ) from exc
    if not isinstance(doc, dict):
        raise ManifestError(
            MANIFEST_SCHEMA_INVALID,
            "$",
            f"manifest must be a YAML mapping, got {type(doc).__name__}",
        )

    api_version = _check_api_version(doc)
    _check_metadata_fields(doc)
    _check_durations(doc)
    _check_argv(doc)
    _check_placeholder_allowlist(doc)
    _check_secret_in_vars(doc)
    _check_restart_clamps(doc)
    _check_env_file_placement(doc, manifest_path)
    _check_schema(doc)

    metadata = _build_metadata(doc.get("metadata") or {})
    launch_raw = _opt_dict(doc, "launch")
    in_process_raw = _opt_dict(doc, "in_process")
    health_raw = _opt_dict(doc, "health")
    stop_raw = _opt_dict(doc, "stop")
    config_raw = _opt_dict(doc, "config")
    restart_raw = _opt_dict(doc, "restart")
    depends_raw = doc.get("depends_on")
    depends_on = tuple(str(d) for d in depends_raw) if isinstance(depends_raw, list) else ()

    _check_depends(metadata.name, depends_on, installation_names)

    logger.info(
        "manifest loaded: name=%s kind=%s path=%s",
        metadata.name,
        doc.get("kind"),
        manifest_path,
    )
    return ComponentManifest(
        path=manifest_path,
        api_version=api_version,
        kind=str(doc.get("kind", "")),
        metadata=metadata,
        launch=_build_launch(launch_raw) if launch_raw else None,
        in_process=_build_in_process(in_process_raw) if in_process_raw else None,
        health=_build_health(health_raw) if health_raw else None,
        stop=Stop(
            signal=str(stop_raw["signal"]),
            grace_period=str(stop_raw["grace_period"]),
        )
        if stop_raw
        else None,
        config=Config(
            schema_file=config_raw.get("schema_file"),
            schema_inline=_opt_dict(config_raw, "schema_inline"),
        )
        if config_raw
        else None,
        restart=_build_restart(restart_raw) if restart_raw else None,
        depends_on=depends_on,
    )


def _find_cycle(graph: dict[str, tuple[str, ...]], start: str) -> list[str] | None:
    """Return a cycle path through ``start`` (a -> b -> a), else None."""
    stack: list[tuple[str, list[str]]] = [(start, [start])]
    seen: set[str] = set()
    while stack:
        node, path = stack.pop()
        for nxt in graph.get(node, ()):
            if nxt == start:
                return [*path, start]
            if nxt in seen or nxt not in graph:
                continue
            seen.add(nxt)
            stack.append((nxt, [*path, nxt]))
    return None


def load_installation(dir_path: str | os.PathLike[str]) -> dict[str, ComponentManifest]:
    """Load every manifest of an installation; fail closed on ANY invalid file.

    Layout §3.5: every file in the manifests directory MUST be a valid
    manifest (``*.yaml`` / ``*.yml``; any stray file is a load error) —
    an invalid file is a load error,
    never a silent skip. Also enforces name uniqueness (NAME_DUPLICATED),
    depends_on resolvability (DEPENDS_MISSING) and acyclicity
    (DEPENDS_CYCLE, the cycle is reported in the message).
    """
    manifests_dir = Path(dir_path).expanduser()
    if not manifests_dir.is_dir():
        raise ManifestError(
            MANIFEST_SCHEMA_INVALID,
            "$",
            f"installation directory does not exist: {manifests_dir}",
            fix_hint="create the installation with `vesma service install`",
        )
    files = sorted(
        [*manifests_dir.glob("*.yaml"), *manifests_dir.glob("*.yml")],
        key=lambda p: p.name,
    )
    # Layout §3.5: EVERY file in the manifests directory must be a valid
    # manifest — a stray non-YAML file is a load error, never a silent skip.
    stray = sorted(
        p for p in manifests_dir.iterdir() if p.is_file() and p.suffix not in {".yaml", ".yml"}
    )
    if stray:
        raise ManifestError(
            MANIFEST_SCHEMA_INVALID,
            "$",
            "unexpected file(s) in the manifests directory: "
            + ", ".join(p.name for p in stray)
            + " — every file in the manifests directory must be a valid "
            "manifest (fail-closed, specs/layout/v1 §3.5)",
            fix_hint="remove the file(s) or rename valid manifests to *.yaml / *.yml",
        )
    # First pass WITHOUT installation-context checks (the name set is only
    # complete after the whole directory is read — a depends_on entry may
    # reference a manifest that sorts later).
    loaded: dict[str, ComponentManifest] = {}
    for file_path in files:
        manifest = load_manifest(file_path)
        if manifest.name in loaded:
            raise ManifestError(
                NAME_DUPLICATED,
                "$.metadata.name",
                f"name {manifest.name!r} is already defined by "
                f"{loaded[manifest.name].path.name}; conflicting file: "
                f"{file_path.name}",
            )
        loaded[manifest.name] = manifest

    graph = {m.name: m.depends_on for m in loaded.values()}
    for name, deps in graph.items():
        for dep in deps:
            if dep not in graph:
                raise ManifestError(
                    DEPENDS_MISSING,
                    "$.depends_on",
                    f"{name}: depends_on entry {dep!r} does not match any manifest "
                    f"of the installation (known: {sorted(graph)})",
                )
    for name in sorted(graph):
        cycle = _find_cycle(graph, name)
        if cycle:
            raise ManifestError(
                DEPENDS_CYCLE,
                "$.depends_on",
                "depends_on cycle in the installation: " + " -> ".join(cycle),
            )
    logger.info("installation loaded: dir=%s components=%s", manifests_dir, sorted(loaded))
    return loaded


def bundled_manifest_path(name: str) -> Path:
    """Resolve a bundled pack manifest (``board`` / ``metrics``) to a real path."""
    if not _MANIFEST_NAME_RE.match(name):
        raise ValueError(f"invalid bundled manifest name: {name!r}")
    anchor = resource_files("vesmaro").joinpath(f"service/components/{name}.yaml")
    with importlib.resources.as_file(anchor) as file_path:
        return file_path


def load_bundled_manifest(
    name: str,
    installation_names: set[str] | None = None,
) -> ComponentManifest:
    """Load one of the manifests shipped inside the engine package."""
    return load_manifest(bundled_manifest_path(name), installation_names)
