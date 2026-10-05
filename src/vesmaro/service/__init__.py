"""VESMA service package (waves W1/W2/W4; board cards svc-w1-foundations,
svc-w2-supervisor, svc-w4-install-unitgen-doctor).

Contract reader side, implemented against:

- ``specs/component-manifest/v1`` (1.0.0-draft.2) — manifest form, strict
  validation, §4 error-code registry: :mod:`.manifest`, :mod:`.errors`;
- ``specs/layout/v1`` (1.0.0-draft.2) — canonical user-profile paths and
  explicit-mode primitives: :mod:`.layout`; doctor checks DR-01…DR-13:
  :mod:`.doctor_checks`;
- fail-closed env files: :mod:`.envfile`; argv placeholder expansion:
  :mod:`.placeholders`;
- ``specs/service-lifecycle/v1`` (1.0.0-draft.2, waves W2/W4) — the
  component supervisor: child FSM (:mod:`.fsm`), health checkers
  (:mod:`.health`), structural lines + history journal + logsink
  (:mod:`.logsink`), and the :class:`.supervisor.Supervisor` orchestration
  (process model, restart policies, env canon, stop order); systemd
  user-unit generation (SL-17/SL-18, container downgrades):
  :mod:`.unitgen`; install/uninstall flow: :mod:`.install` (CLI:
  ``vesma service install|uninstall``; doctor: ``vesma doctor service``).

Still out of scope (W3): the control socket and its CLI verbs. The bundled
pack manifests live in ``vesmaro.service.components`` (``board``,
``metrics``); the in-process panel is :mod:`.board`.
"""

from __future__ import annotations

from vesmaro.service.board import BoardComponent, create_component
from vesmaro.service.board import health as board_health
from vesmaro.service.errors import ManifestError
from vesmaro.service.fsm import (
    TRANSITION_TABLE,
    ChildFsm,
    ChildState,
    FsmEvent,
    TransitionError,
    TransitionResult,
)
from vesmaro.service.logsink import (
    HistoryJournal,
    Logsink,
    SupervisorLine,
    build_degraded_line,
    build_exit_line,
    build_health_line,
    build_spawn_line,
    journal_socket_available,
    make_logsink,
)
from vesmaro.service.manifest import (
    SCHEMA_CONTRACT_VERSION,
    SUPPORTED_API_VERSIONS,
    ComponentManifest,
    load_bundled_manifest,
    load_installation,
    load_manifest,
)
from vesmaro.service.supervisor import (
    Clock,
    ExitRecord,
    RestartPolicy,
    Supervisor,
    set_child_subreaper,
)

__all__ = [
    "SCHEMA_CONTRACT_VERSION",
    "SUPPORTED_API_VERSIONS",
    "TRANSITION_TABLE",
    "BoardComponent",
    "ChildFsm",
    "ChildState",
    "Clock",
    "ComponentManifest",
    "ExitRecord",
    "FsmEvent",
    "HistoryJournal",
    "Logsink",
    "ManifestError",
    "RestartPolicy",
    "Supervisor",
    "SupervisorLine",
    "TransitionError",
    "TransitionResult",
    "board_health",
    "build_degraded_line",
    "build_exit_line",
    "build_health_line",
    "build_spawn_line",
    "create_component",
    "journal_socket_available",
    "load_bundled_manifest",
    "load_installation",
    "load_manifest",
    "make_logsink",
    "set_child_subreaper",
]
