"""VESMA service foundations (wave W1, board card svc-w1-foundations).

Contract reader side, implemented against:

- ``specs/component-manifest/v1`` (1.0.0-draft.2) — manifest form, strict
  validation, §4 error-code registry: :mod:`.manifest`, :mod:`.errors`;
- ``specs/layout/v1`` (1.0.0-draft.2) — canonical user-profile paths and
  explicit-mode primitives: :mod:`.layout`;
- fail-closed env files: :mod:`.envfile`; argv placeholder expansion:
  :mod:`.placeholders`.

This wave is pure foundation — no CLI, no supervisor, no install flow
(later waves build on these modules). The bundled pack manifests live in
``vesmaro.service.components`` (``board``, ``metrics``).
"""

from __future__ import annotations

from vesmaro.service.errors import ManifestError
from vesmaro.service.manifest import (
    SCHEMA_CONTRACT_VERSION,
    SUPPORTED_API_VERSIONS,
    ComponentManifest,
    load_bundled_manifest,
    load_installation,
    load_manifest,
)

__all__ = [
    "SCHEMA_CONTRACT_VERSION",
    "SUPPORTED_API_VERSIONS",
    "ComponentManifest",
    "ManifestError",
    "load_bundled_manifest",
    "load_installation",
    "load_manifest",
]
