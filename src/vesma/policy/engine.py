"""Policy engine — M5: declarative YAML rule evaluation.

Rules are loaded from ~/.vesma/policies.yaml (or injected via config).
Each rule has conditions (status, age, quality thresholds) and actions
(auto-publish, archive, alert, trigger-cluster).

Evaluation is stateless: given a Memory + list of PolicyRule,
returns the list of actions that fire.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from vesma.models import Memory, MemoryStatus

logger = logging.getLogger(__name__)


# ── Rule model ───────────────────────────────────────────────────────────────


class PolicyCondition(BaseModel):
    """A single condition clause."""

    status: MemoryStatus | None = None
    min_quality: float | None = None
    min_confidence: float | None = None
    min_source_coverage: int | None = None
    max_age_hours: float | None = None  # memory.created_at must be within N hours
    tags_include: list[str] = Field(default_factory=list)
    tags_exclude: list[str] = Field(default_factory=list)


class PolicyAction(BaseModel):
    """A single action to execute when rule fires."""

    action: str  # "publish" | "archive" | "alert" | "trigger_cluster" | "defer"
    params: dict[str, Any] = Field(default_factory=dict)


class PolicyRule(BaseModel):
    """One declarative rule."""

    name: str
    description: str = ""
    enabled: bool = True
    priority: int = 0  # higher = evaluated first
    conditions: list[PolicyCondition] = Field(default_factory=list)
    actions: list[PolicyAction] = Field(default_factory=list)


# ── Evaluation ─────────────────────────────────────────────────────────────


def _condition_matches(mem: Memory, cond: PolicyCondition) -> bool:
    """Check if a memory satisfies a single condition."""
    if cond.status is not None and mem.status != cond.status:
        return False
    if cond.min_quality is not None and (mem.quality_score or 0.0) < cond.min_quality:
        return False
    if cond.min_confidence is not None and (mem.confidence or 0.0) < cond.min_confidence:
        return False
    if (
        cond.min_source_coverage is not None
        and (mem.source_coverage or 0) < cond.min_source_coverage
    ):
        return False
    if cond.max_age_hours is not None:
        age = datetime.now(UTC) - mem.created_at
        if age > timedelta(hours=cond.max_age_hours):
            return False
    mem_tags = set(mem.tags)
    if cond.tags_include and not all(t in mem_tags for t in cond.tags_include):
        return False
    return not (cond.tags_exclude and any(t in mem_tags for t in cond.tags_exclude))


def evaluate_rules(
    mem: Memory,
    rules: list[PolicyRule],
) -> list[PolicyAction]:
    """Evaluate all rules against a memory and return fired actions.

    Rules are sorted by priority (desc).  The first rule whose ALL
    conditions match fires its actions; subsequent rules are still
    evaluated (multiple rules may fire).

    Args:
        mem: The memory to evaluate.
        rules: Loaded policy rules (e.g. from YAML).

    Returns:
        Flat list of PolicyAction from all matching rules.
    """
    fired: list[PolicyAction] = []
    for rule in sorted(rules, key=lambda r: r.priority, reverse=True):
        if not rule.enabled:
            continue
        if not rule.conditions:
            # No conditions = always fires
            fired.extend(rule.actions)
            logger.debug("policy: rule '%s' fired (no conditions)", rule.name)
            continue
        if all(_condition_matches(mem, c) for c in rule.conditions):
            fired.extend(rule.actions)
            logger.info(
                "policy: rule '%s' fired for %s — actions: %s",
                rule.name,
                mem.id[:8],
                [a.action for a in rule.actions],
            )
    return fired


def load_rules_from_dict(raw: dict[str, Any]) -> list[PolicyRule]:
    """Parse a policies.yaml dict into PolicyRule objects."""
    rules: list[PolicyRule] = []
    for name, data in raw.get("rules", {}).items():
        if not isinstance(data, dict):
            continue
        rule_data = dict(data)
        rule_data["name"] = name
        try:
            rules.append(PolicyRule.model_validate(rule_data))
        except Exception as exc:
            logger.warning("policy: failed to parse rule '%s': %s", name, exc)
    return rules
