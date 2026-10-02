"""Boundary wiring: config → store, and the two collection points.

Phase A integration of the vitals plane (ADR-0026; ArchCom core
``7ec9dda3``). The store is created once per process from Settings;
the MCP handler and the ``pre_llm_call`` hook call
``MemoryManager.record_assemble_vitals(result)`` after the assemble
result exists. Everything here is non-fatal by contract: a broken or
disabled metrics plane never blocks the server.
"""

from __future__ import annotations

import logging

from vesmaro.config import Settings
from vesmaro.metrics.schema import SIDECAR_FILENAME
from vesmaro.metrics.sink import MetricsStore

logger = logging.getLogger("vesmaro.metrics.boundary")


def create_vitals_store(settings: Settings) -> MetricsStore | None:
    """Build the sidecar store from Settings, or ``None`` when disabled.

    Default-on in local-first (ArchCom ``7ec9dda3`` + owner directive
    2026-09-20): a local attacker holding the sidecar already holds the
    main store with full content, so collection adds no exposure.

    C3 config-lint: per-request rows carry a ``session`` column; under
    ``auth_enabled=true`` (multi-principal deployment) collection
    REFUSES to start without the explicit
    ``vitals.ack_session_collection=true`` acknowledgement. The server
    continues without metrics — refusal, not crash.
    """
    vitals = settings.vitals
    if not vitals.enabled:
        logger.info("vitals: disabled by config (vitals.enabled=false)")
        return None
    if settings.api.auth_enabled and not vitals.ack_session_collection:
        logger.warning(
            "vitals: per-request collection REFUSED (C3 config-lint) —"
            " api.auth_enabled=true without vitals.ack_session_collection=true;"
            " the server continues without metrics"
        )
        return None
    try:
        data_dir = settings.vesma.data_dir.expanduser()
        data_dir.mkdir(parents=True, exist_ok=True)
        return MetricsStore(data_dir / SIDECAR_FILENAME)
    except Exception as exc:
        logger.warning("vitals: store unavailable (non-fatal): %s", exc)
        return None


#: Per-data-dir cache for manager-less call sites (federation client,
#: CLI). The store is a guest: creation failure → disabled, never fatal.
_STANDALONE_STORES: dict[str, MetricsStore] = {}


def record_verb_standalone(settings: Settings, **kwargs: object) -> None:
    """Record a verb without a manager (best-effort, non-fatal).

    Used by call sites that hold Settings but no MemoryManager — the
    federation client and the CLI entry wrapper.
    """
    try:
        key = str(settings.vesma.data_dir)
        store = _STANDALONE_STORES.get(key)
        if store is None:
            store = create_vitals_store(settings)
            if store is None:
                return
            _STANDALONE_STORES[key] = store
        store.record_verb(**kwargs)  # type: ignore[arg-type]
    except Exception:  # guest contract: never fatal
        logger.warning("vitals: standalone verb record failed (non-fatal)", exc_info=True)
