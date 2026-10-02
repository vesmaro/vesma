"""Triggers — M5: event-driven pipeline triggers.

Hooks into status transitions and vault watcher events to fire
automated actions via the policy engine.

Events:
  - memory_saved   : new or updated memory
  - status_changed : raw → processing → processed → published
  - vault_write    : file written to Obsidian vault (debounced)

Each trigger evaluates policy rules and executes actions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

from vesma.models import Memory, MemoryStatus
from vesma.policy.engine import (
    PolicyAction,
    PolicyRule,
    evaluate_rules,
    load_rules_from_dict,
)

if TYPE_CHECKING:
    from vesma.manager import MemoryManager

logger = logging.getLogger(__name__)


def _get_rules(mgr: MemoryManager) -> list[PolicyRule]:
    """Load rules from config or empty list if none configured."""
    raw = getattr(mgr.settings, "policies", None)
    if raw is None:
        # An empty `[]` literal narrows to list[Never] which is not
        # assignable to list[PolicyRule]. Annotating the local as
        # list[PolicyRule] makes the function-scope narrowing work
        # without resorting to `cast`.
        empty: list[PolicyRule] = []
        return empty
    if isinstance(raw, dict):
        return load_rules_from_dict(raw)
    # `raw` came from `getattr(mgr.settings, "policies", None)` which is
    # typed `Any` in absence of a settings overload — narrow defensively.
    if isinstance(raw, list):
        return cast(list[PolicyRule], raw)
    return []


def on_memory_saved(mgr: MemoryManager, mem: Memory) -> None:
    """Trigger fired after any memory is saved."""
    rules = _get_rules(mgr)
    if not rules:
        return
    actions = evaluate_rules(mem, rules)
    for act in actions:
        _execute_action(mgr, mem, act)


def on_status_changed(
    mgr: MemoryManager,
    mem: Memory,
    old_status: MemoryStatus,
    new_status: MemoryStatus,
) -> None:
    """Trigger fired when a memory transitions between pipeline statuses."""
    logger.debug(
        "trigger: %s %s → %s",
        mem.id[:8],
        old_status.value,
        new_status.value,
    )
    # Auto-publish on processed→published if policy allows
    if new_status == MemoryStatus.PROCESSED:
        rules = _get_rules(mgr)
        actions = evaluate_rules(mem, rules)
        for act in actions:
            if act.action == "publish":
                _execute_action(mgr, mem, act)


def _execute_action(
    mgr: MemoryManager,
    mem: Memory,
    act: PolicyAction,
) -> None:
    """Execute a single policy action."""

    action = act.action
    if action == "publish":
        from vesma.pipeline.publish import publish_memory

        result = publish_memory(mgr, mem.id)
        if not result.published:
            logger.warning("trigger: auto-publish failed for %s", mem.id[:8])
    elif action == "archive":
        mem.status = MemoryStatus.ARCHIVED
        mgr.sqlite.save(mem)
        logger.info("trigger: archived %s", mem.id[:8])
    elif action == "alert":
        msg = act.params.get("message", "Policy alert")
        logger.warning("trigger: ALERT %s — %s", mem.id[:8], msg)
    elif action == "defer":
        logger.info("trigger: deferred %s (no action)", mem.id[:8])
    elif action == "trigger_cluster":
        logger.info("trigger: cluster request for project=%s", mem.project)
    else:
        logger.warning("trigger: unknown action '%s' for %s", action, mem.id[:8])
