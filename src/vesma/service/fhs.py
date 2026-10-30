"""FHS manifest — the single source of truth of the system profile
(layout v2 §3.12; ADR-0045 Decision 5).

The distribution ships exactly ONE declarative manifest
(``fhs_manifest.yaml``, package data) enumerating every system-profile
path with its user correspondence: ``role`` / ``user_path`` /
``system_path`` / ``kind`` / ``mode`` / ``owner`` / ``group`` /
``flags``. Consumers — the ``install --system`` installer
(:mod:`vesma.service.system_install`), the doctor's system legs
(DR-14/DR-15) and future deb/rpm or container builds — derive their
artifacts from THIS manifest; hardcoded system paths anywhere else in
the engine violate the contract (SY-02).

This module is the ONLY resolution point for system-profile paths:

- :func:`load_fhs_manifest` parses + validates the manifest fail-closed
  (unique roles, unique system paths, octal modes, a closed flag
  vocabulary — an invalid shipped manifest is a build bug and must be
  loud);
- :func:`resolve_system_path` maps a manifest entry onto a concrete
  ``Path`` under the deployment prefix. The prefix defaults to ``/``
  (canonical FHS); tests run the whole install flow against a temp
  prefix instead (the dest-dir technique — root-dependent conformance
  tests without root, per the ADR-0045 rollout);
- the ``role``-named helpers (:func:`system_config_root`,
  :func:`system_data_root`, …) are what other engine modules call —
  never path math of their own.

Template entries carry ``<name>`` (a component name) or ``<rev>``
(a venv revision id); substitution validates the filler against the
component-name grammar (``^[a-z][a-z0-9-]{0,62}$`` — same as manifests)
or the revision grammar, so a manifest-derived path can never escape
its slot (no separators, no traversal).
"""

from __future__ import annotations

import dataclasses
import os
import re
from pathlib import Path
from typing import Any, Final

import yaml

from vesma.service.manifest import _NAME_RE

#: The packaged manifest (ONE file per distribution, §3.12).
MANIFEST_RESOURCE: Final[str] = "fhs_manifest.yaml"

#: Revision-id grammar for ``<rev>`` substitution (venv content-id; no
#: path separators, no leading dot — a revision is a directory suffix).
_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,62}$")

#: The closed flag vocabulary (§3.12: narrowing/renaming = MAJOR; growth
#: is additive). The installer/doctor read specific flags.
KNOWN_FLAGS: frozenset[str] = frozenset(
    {
        "conffile",
        "opt-in",
        "symlink-revision",
        "installer-materialized",
        "generated",
        "os-mandated-location",
        "runtime-directory",
        "read-only-runtime",
        "append-only",
        "regenerable",
        "never-secrets",
        "installer-only-writer",
        "fail-closed",
        "drop-in",
        "operator-data",
        "engine-managed",
    }
)

_KINDS: frozenset[str] = frozenset({"dir", "file", "symlink"})
_MODE_RE = re.compile(r"^[0-7]{4}$")
_TEMPLATE_RE = re.compile(r"<(name|rev)>")


class FhsManifestError(Exception):
    """The shipped FHS manifest is invalid — a build bug, loud by design."""


@dataclasses.dataclass(frozen=True)
class FhsEntry:
    """One manifest row (the §3.12 field set; form per the spec example)."""

    role: str
    system_path: str  # canonical FHS path, possibly with <name>/<rev>
    user_path: str | None
    kind: str  # dir | file | symlink
    mode: int  # parsed from the 4-digit octal string
    owner: str | None
    group: str | None
    flags: frozenset[str]

    def has_flag(self, flag: str) -> bool:
        return flag in self.flags


@dataclasses.dataclass(frozen=True)
class FhsManifest:
    """The validated manifest: entries by role, creation order kept."""

    entries: tuple[FhsEntry, ...]

    def entry(self, role: str) -> FhsEntry:
        """The entry for ``role`` — KeyError is a programming error."""
        for entry in self.entries:
            if entry.role == role:
                return entry
        raise KeyError(f"role {role!r} is not in the FHS manifest")

    def entry_opt(self, role: str) -> FhsEntry | None:
        for entry in self.entries:
            if entry.role == role:
                return entry
        return None


def default_prefix() -> Path:
    """The deployment prefix the system profile resolves under.

    ``/`` in production — every canonical path comes out byte-exact. The
    dest-dir technique (root-less conformance tests, ADR-0045 rollout)
    monkeypatches this to a temp dir; call-time evaluation keeps
    monkeypatched environments from seeing a stale prefix (the
    :mod:`vesma.service.layout` precedent).
    """
    return Path(os.sep)


def load_fhs_manifest(path: Path | None = None) -> FhsManifest:
    """Load + validate the packaged manifest (or an explicit path)."""
    if path is None:
        raw = (Path(__file__).parent / MANIFEST_RESOURCE).read_text(encoding="utf-8")
    else:
        raw = path.read_text(encoding="utf-8")
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise FhsManifestError(f"FHS manifest is not valid YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise FhsManifestError("FHS manifest must be a mapping")
    if doc.get("manifest") != "vesma-fhs" or doc.get("profile") != "system":
        raise FhsManifestError(
            "FHS manifest header mismatch: expected manifest=vesma-fhs, profile=system"
        )
    rows = doc.get("paths")
    if not isinstance(rows, list) or not rows:
        raise FhsManifestError("FHS manifest must carry a non-empty 'paths' list")

    entries: list[FhsEntry] = []
    seen_roles: set[str] = set()
    seen_paths: set[str] = set()
    for row in rows:
        entries.append(_validate_row(row, seen_roles, seen_paths))
    return FhsManifest(entries=tuple(entries))


def _validate_row(row: Any, seen_roles: set[str], seen_paths: set[str]) -> FhsEntry:
    """Fail-closed validation of one manifest row (§3.12 field set)."""
    if not isinstance(row, dict):
        raise FhsManifestError(f"manifest row must be a mapping: {row!r}")
    role = row.get("role")
    system_path = row.get("system_path")
    kind = row.get("kind")
    mode = row.get("mode")
    if not isinstance(role, str) or not role:
        raise FhsManifestError(f"row without a role: {row!r}")
    if role in seen_roles:
        raise FhsManifestError(f"duplicate manifest role: {role}")
    seen_roles.add(role)
    if not isinstance(system_path, str) or not system_path.startswith("/"):
        raise FhsManifestError(f"{role}: system_path must be an absolute FHS path")
    if system_path in seen_paths:
        raise FhsManifestError(f"{role}: duplicate system_path {system_path}")
    seen_paths.add(system_path)
    if kind not in _KINDS:
        raise FhsManifestError(f"{role}: kind must be one of {sorted(_KINDS)}, got {kind!r}")
    if not isinstance(mode, str) or _MODE_RE.match(mode) is None:
        raise FhsManifestError(f"{role}: mode must be a 4-digit octal string, got {mode!r}")
    flags_raw = row.get("flags", [])
    if not isinstance(flags_raw, list):
        raise FhsManifestError(f"{role}: flags must be a list")
    flags = frozenset(flags_raw)
    unknown = flags - KNOWN_FLAGS
    if unknown:
        raise FhsManifestError(f"{role}: unknown flag(s) {sorted(unknown)}")
    user_path = row.get("user_path")
    if user_path is not None and not isinstance(user_path, str):
        raise FhsManifestError(f"{role}: user_path must be a string or null")
    owner = row.get("owner")
    group = row.get("group")
    if owner is not None and not isinstance(owner, str):
        raise FhsManifestError(f"{role}: owner must be a string or null")
    if group is not None and not isinstance(group, str):
        raise FhsManifestError(f"{role}: group must be a string or null")
    return FhsEntry(
        role=role,
        system_path=system_path,
        user_path=user_path,
        kind=kind,
        mode=int(mode, 8),
        owner=owner,
        group=group,
        flags=flags,
    )


def resolve_system_path(
    entry: FhsEntry,
    prefix: Path | None = None,
    *,
    name: str | None = None,
    rev: str | None = None,
) -> Path:
    """The concrete path of ``entry`` under ``prefix`` (canonical = ``/``).

    ``<name>``/``<rev>`` templates are substituted with validated
    fillers — a name outside the component grammar or a revision outside
    :data:`_REVISION_RE` is a loud ValueError, never a mangled path.
    """
    prefix = default_prefix() if prefix is None else prefix
    path_str = entry.system_path
    if "<name>" in path_str:
        if name is None or _NAME_RE.match(name) is None:
            raise ValueError(
                f"{entry.role}: <name> substitution requires a component name "
                f"matching {_NAME_RE.pattern}, got {name!r}"
            )
        path_str = path_str.replace("<name>", name)
    if "<rev>" in path_str:
        if rev is None or _REVISION_RE.match(rev) is None:
            raise ValueError(
                f"{entry.role}: <rev> substitution requires a revision id "
                f"matching {_REVISION_RE.pattern}, got {rev!r}"
            )
        path_str = path_str.replace("<rev>", rev)
    relative = path_str.lstrip("/")
    if not relative:
        return prefix
    return prefix / relative


# ── Role-named resolvers (what other engine modules call) ─────────────


def system_config_root(prefix: Path | None = None) -> Path:
    """/etc/vesma (§3.4 — the configuration root)."""
    return resolve_system_path(load_fhs_manifest().entry("config_root"), prefix)


def system_components_dir(prefix: Path | None = None) -> Path:
    """/etc/vesma/components.d (drop-in manifests)."""
    return resolve_system_path(load_fhs_manifest().entry("manifest_dir"), prefix)


def system_env_dir(prefix: Path | None = None) -> Path:
    """/etc/vesma/env.d (env secrets; files 0600 — the DR-15 FAIL gate)."""
    return resolve_system_path(load_fhs_manifest().entry("env_dir"), prefix)


def system_config_path(prefix: Path | None = None) -> Path:
    """/etc/vesma/config.yaml (conffile zone)."""
    return resolve_system_path(load_fhs_manifest().entry("shared_config"), prefix)


def system_data_root(prefix: Path | None = None) -> Path:
    """/var/lib/vesma (the store home + component data)."""
    return resolve_system_path(load_fhs_manifest().entry("data_root"), prefix)


def system_component_data_dir(name: str, prefix: Path | None = None) -> Path:
    """/var/lib/vesma/<name> — component data, default cwd."""
    entry = load_fhs_manifest().entry("component_data")
    return resolve_system_path(entry, prefix, name=name)


def system_install_state_path(prefix: Path | None = None) -> Path:
    """/var/lib/vesma/install-state.yaml (§3.13 — exact-inverse uninstall)."""
    return resolve_system_path(load_fhs_manifest().entry("install_state"), prefix)


def system_unit_path(prefix: Path | None = None) -> Path:
    """/etc/systemd/system/vesma.service (coexists with the user unit)."""
    return resolve_system_path(load_fhs_manifest().entry("system_unit"), prefix)


def system_sysusers_path(prefix: Path | None = None) -> Path:
    """/usr/local/lib/sysusers.d/vesma.conf (creates vesma:vesma, §3.13)."""
    return resolve_system_path(load_fhs_manifest().entry("sysusers_fragment"), prefix)


def system_venv_engine(prefix: Path | None = None) -> Path:
    """/usr/local/lib/vesma/venv — the engine venv symlink (active revision)."""
    return resolve_system_path(load_fhs_manifest().entry("venv_engine"), prefix)


def system_toolchain_pythons(prefix: Path | None = None) -> Path:
    """/usr/local/lib/vesma/pythons — the canonical uv toolchain install dir
    (UV_PYTHON_INSTALL_DIR for the installer's materialize leg; never
    $HOME-dependent, never the data plane)."""
    return resolve_system_path(load_fhs_manifest().entry("toolchain_pythons"), prefix)


def system_venv_engine_revision(rev: str, prefix: Path | None = None) -> Path:
    """/usr/local/lib/vesma/venv-<rev> — one materialized engine revision."""
    entry = load_fhs_manifest().entry("venv_engine_revision")
    return resolve_system_path(entry, prefix, rev=rev)


def system_venvs_root(prefix: Path | None = None) -> Path:
    """/usr/local/lib/vesma/venvs — parent of component venv symlinks."""
    return resolve_system_path(load_fhs_manifest().entry("venv_component_root"), prefix)


def system_venv_component(name: str, prefix: Path | None = None) -> Path:
    """/usr/local/lib/vesma/venvs/<name> — component venv symlink."""
    entry = load_fhs_manifest().entry("venv_component")
    return resolve_system_path(entry, prefix, name=name)


def system_venv_component_revision(name: str, rev: str, prefix: Path | None = None) -> Path:
    """/usr/local/lib/vesma/venvs/<name>-<rev> — one materialized revision."""
    entry = load_fhs_manifest().entry("venv_component_revision")
    return resolve_system_path(entry, prefix, name=name, rev=rev)


def system_binary_bin_root(prefix: Path | None = None) -> Path:
    """/usr/local/lib/vesma/bin — parent of the binary child slots
    (component-platform wave Q). Installer-owned whole-tree (rmtree on
    uninstall, the toolchain_pythons discipline)."""
    return resolve_system_path(load_fhs_manifest().entry("binary_bin_root"), prefix)


def system_binary_artifact(name: str, prefix: Path | None = None) -> Path:
    """/usr/local/lib/vesma/bin/<name> — one binary child's pinned
    artifact slot (materialized by the installer, sha256-pinned by the
    manifest's provenance.artifact_sha256)."""
    entry = load_fhs_manifest().entry("binary_artifact")
    return resolve_system_path(entry, prefix, name=name)


def system_cache_root(prefix: Path | None = None) -> Path:
    """/var/cache/vesma (regenerable, never secrets)."""
    return resolve_system_path(load_fhs_manifest().entry("cache_root"), prefix)


def system_static_root(prefix: Path | None = None) -> Path:
    """/usr/local/share/vesma (distro static, read-only at runtime)."""
    return resolve_system_path(load_fhs_manifest().entry("distro_static"), prefix)


def system_ipc_dir(prefix: Path | None = None) -> Path:
    """/run/vesma (RuntimeDirectory=vesma — systemd creates it)."""
    return resolve_system_path(load_fhs_manifest().entry("ipc_runtime"), prefix)


__all__ = [
    "KNOWN_FLAGS",
    "MANIFEST_RESOURCE",
    "FhsEntry",
    "FhsManifest",
    "FhsManifestError",
    "default_prefix",
    "load_fhs_manifest",
    "resolve_system_path",
    "system_binary_artifact",
    "system_binary_bin_root",
    "system_cache_root",
    "system_component_data_dir",
    "system_components_dir",
    "system_config_path",
    "system_config_root",
    "system_data_root",
    "system_env_dir",
    "system_install_state_path",
    "system_ipc_dir",
    "system_static_root",
    "system_sysusers_path",
    "system_toolchain_pythons",
    "system_unit_path",
    "system_venv_component",
    "system_venv_component_revision",
    "system_venv_engine",
    "system_venv_engine_revision",
    "system_venvs_root",
]
