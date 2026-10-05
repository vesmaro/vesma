"""VESMA service package (waves W1/W4; board cards svc-w1-foundations,
svc-w4-install-unitgen-doctor).

Contract reader side, implemented against:

- ``specs/component-manifest/v1`` (1.0.0-draft.2) — manifest form, strict
  validation, §4 error-code registry: :mod:`.manifest`, :mod:`.errors`;
- ``specs/layout/v1`` (1.0.0-draft.2) — canonical user-profile paths and
  explicit-mode primitives: :mod:`.layout`; doctor checks DR-01…DR-13:
  :mod:`.doctor_checks`;
- fail-closed env files: :mod:`.envfile`; argv placeholder expansion:
  :mod:`.placeholders`;
- ``specs/service-lifecycle/v1`` (1.0.0-draft.2) §3.6 — systemd user-unit
  generation (SL-17/SL-18, container downgrades): :mod:`.unitgen`;
  install/uninstall flow: :mod:`.install` (CLI: ``vesma service
  install|uninstall``; doctor: ``vesma doctor service``).

No supervisor/FSM and no control-socket client yet (waves W2/W3 own
those); ``vesma service run`` is referenced by the generated unit per
contract and arrives with W3. The bundled pack manifests live in
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
