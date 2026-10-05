"""Typed errors and the validation-code registry for the service package.

The registry mirrors contract ``specs/component-manifest/v1/spec.md`` §4
(component-manifest ``1.0.0-draft.2``) one-to-one: every loader/install
rejection carries one of these codes plus the JSON-path of the offending
field and — where the contract mandates it — a ready fix command.

Codes the foundation wave only DECLARES (their checks execute in later
waves): ``CONFIG_INVALID`` (component config validated against its
manifest schema at start — supervisor), ``ARTIFACT_HASH_MISMATCH``
(spawn-time artifact hash check — supervisor), ``CALLBACK_FAILED``
(health-callback isolation boundary — supervisor).
"""

from __future__ import annotations

# ── Error-code registry (CM spec §4) ──────────────────────────────────

APIVERSION_UNSUPPORTED = "APIVERSION_UNSUPPORTED"
MANIFEST_SCHEMA_INVALID = "MANIFEST_SCHEMA_INVALID"
NAME_DUPLICATED = "NAME_DUPLICATED"
SHELL_IN_ARGV = "SHELL_IN_ARGV"
PLACEHOLDER_UNKNOWN = "PLACEHOLDER_UNKNOWN"
SECRET_IN_VARS = "SECRET_IN_VARS"  # nosec B105 - contract error-code constant (CM §4), not a credential
ENV_FILE_UNSAFE = "ENV_FILE_UNSAFE"
DURATION_INVALID = "DURATION_INVALID"
CLAMP_VIOLATION = "CLAMP_VIOLATION"
DEPENDS_CYCLE = "DEPENDS_CYCLE"
DEPENDS_MISSING = "DEPENDS_MISSING"
CONFIG_INVALID = "CONFIG_INVALID"
ARTIFACT_HASH_MISMATCH = "ARTIFACT_HASH_MISMATCH"
CALLBACK_FAILED = "CALLBACK_FAILED"

__all__ = [
    "APIVERSION_UNSUPPORTED",
    "ARTIFACT_HASH_MISMATCH",
    "CALLBACK_FAILED",
    "CLAMP_VIOLATION",
    "CONFIG_INVALID",
    "DEPENDS_CYCLE",
    "DEPENDS_MISSING",
    "DURATION_INVALID",
    "ENV_FILE_UNSAFE",
    "MANIFEST_SCHEMA_INVALID",
    "NAME_DUPLICATED",
    "PLACEHOLDER_UNKNOWN",
    "SECRET_IN_VARS",
    "SHELL_IN_ARGV",
    "ManifestError",
]


class ManifestError(Exception):
    """One contract violation of a component manifest / installation.

    Attributes:
        code: registry constant from CM spec §4 (see module docstring).
        field_path: JSON-style path of the offending field, e.g.
            ``$.launch.argv[2]``; ``$`` for document-level problems.
        message: human-readable explanation (never prints secret values).
        fix_hint: ready-to-run fix command where the contract mandates one
            (e.g. ``chmod 600 <path>`` for ENV_FILE_UNSAFE); ``None`` when
            no single command applies.
    """

    def __init__(
        self,
        code: str,
        field_path: str,
        message: str,
        fix_hint: str | None = None,
    ) -> None:
        self.code = code
        self.field_path = field_path
        self.message = message
        self.fix_hint = fix_hint
        rendered = f"[{code}] {field_path}: {message}"
        if fix_hint:
            rendered = f"{rendered} (fix: {fix_hint})"
        super().__init__(rendered)
