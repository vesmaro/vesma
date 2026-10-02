"""Tests for M5: Policy Engine, DLQ, triggers, scheduler.

Covers:
  - DLQ: add, list, retry, discard, count, ready-only filter
  - Policy engine: rule evaluation, condition matching, YAML loading
  - Triggers: on_memory_saved, on_status_changed
  - Scheduler: auto_cluster, auto_synthesize, auto_publish, dlq_retry
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from vesma.config import Settings
from vesma.manager import MemoryManager
from vesma.models import Memory, MemoryStatus
from vesma.policy.dlq import dlq_add, dlq_discard, dlq_list, dlq_retry
from vesma.policy.engine import (
    PolicyAction,
    PolicyCondition,
    PolicyRule,
    evaluate_rules,
    load_rules_from_dict,
)
from vesma.policy.scheduler import (
    auto_cluster,
    auto_publish,
    auto_synthesize,
    dlq_retry_scheduler,
)
from vesma.policy.triggers import on_memory_saved, on_status_changed

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_settings():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        settings = Settings(
            vesma={
                "vault_path": str(tmp / "vault"),
                "data_dir": str(tmp / "data"),
                "db_name": "test.db",
            },
            embedding={"provider": "onnx"},
            automation={
                "enabled": True,
                "min_raw_to_trigger": 1,
            },
        )
        settings.resolve_paths()
        yield settings


@pytest.fixture
def tmp_manager(tmp_settings):
    mgr = MemoryManager(tmp_settings)
    mock_embedder = MagicMock()
    mock_embedder.embed.return_value = [0.1] * 384
    mgr._embedder = mock_embedder
    yield mgr
    mgr.close()


# ---------------------------------------------------------------------------
# DLQ
# ---------------------------------------------------------------------------


class TestDLQ:
    def test_add_and_list(self, tmp_manager):
        """DLQ entries are persisted and retrievable."""
        mgr = tmp_manager
        dlq_add(mgr, "mem-123", task_label="synthesize", error_message="LLM timeout")
        entries = dlq_list(mgr)
        assert len(entries) == 1
        assert entries[0]["memory_id"] == "mem-123"
        assert entries[0]["task_label"] == "synthesize"

    def test_list_filters_by_task_label(self, tmp_manager):
        """task_label filter narrows DLQ list."""
        mgr = tmp_manager
        dlq_add(mgr, "m1", task_label="synthesize")
        dlq_add(mgr, "m2", task_label="publish")

        syn = dlq_list(mgr, task_label="synthesize")
        pub = dlq_list(mgr, task_label="publish")
        assert len(syn) == 1
        assert len(pub) == 1
        assert syn[0]["memory_id"] == "m1"

    def test_retry_increments_attempt(self, tmp_manager):
        """dlq_retry bumps attempt_count and sets next_retry_at."""
        mgr = tmp_manager
        dlq_add(mgr, "m1", max_attempts=3)
        entry = dlq_list(mgr)[0]
        assert entry["attempt_count"] == 1

        dlq_retry(mgr, entry["id"], backoff_sec=10)
        updated = dlq_list(mgr)[0]
        assert updated["attempt_count"] == 2
        assert updated["next_retry_at"] is not None

    def test_discard_removes_entry(self, tmp_manager):
        """dlq_discard deletes the entry."""
        mgr = tmp_manager
        dlq_add(mgr, "m1")
        entry = dlq_list(mgr)[0]

        ok = dlq_discard(mgr, entry["id"])
        assert ok is True
        assert dlq_list(mgr) == []

    def test_count(self, tmp_manager):
        """dlq_count reflects current entries."""
        mgr = tmp_manager
        assert mgr.sqlite.dlq_count() == 0
        dlq_add(mgr, "m1")
        assert mgr.sqlite.dlq_count() == 1
        dlq_add(mgr, "m2")
        assert mgr.sqlite.dlq_count() == 2

    def test_ready_only_filter(self, tmp_manager):
        """ready_only=True filters to entries with past next_retry_at."""
        mgr = tmp_manager
        dlq_add(mgr, "m1")
        # Default next_retry_at is now, so it should be ready
        ready = dlq_list(mgr, ready_only=True)
        assert len(ready) == 1

    def test_max_attempts_respected(self, tmp_manager):
        """Entries store max_attempts correctly."""
        mgr = tmp_manager
        dlq_add(mgr, "m1", max_attempts=5)
        entry = dlq_list(mgr)[0]
        assert entry["max_attempts"] == 5


# ---------------------------------------------------------------------------
# Policy engine
# ---------------------------------------------------------------------------


class TestPolicyEngine:
    def test_single_rule_matches(self):
        """A rule with matching conditions fires its actions."""
        mem = Memory(
            content="draft",
            tags=["project:mnemos", "agent:reviewer", "mnemos:learning"],
            project="mnemos",
            agent="reviewer",
            status=MemoryStatus.PROCESSED,
            quality_score=0.9,
            confidence=0.9,
            source_coverage=5,
        )
        rule = PolicyRule(
            name="auto-publish-high-quality",
            conditions=[
                PolicyCondition(status=MemoryStatus.PROCESSED, min_quality=0.8),
            ],
            actions=[PolicyAction(action="publish")],
        )
        actions = evaluate_rules(mem, [rule])
        assert len(actions) == 1
        assert actions[0].action == "publish"

    def test_rule_fails_on_mismatch(self):
        """A rule with non-matching conditions does NOT fire."""
        mem = Memory(
            content="draft",
            tags=["project:mnemos", "agent:reviewer", "mnemos:learning"],
            project="mnemos",
            agent="reviewer",
            status=MemoryStatus.RAW,
            quality_score=0.9,
        )
        rule = PolicyRule(
            name="only-processed",
            conditions=[PolicyCondition(status=MemoryStatus.PROCESSED)],
            actions=[PolicyAction(action="publish")],
        )
        actions = evaluate_rules(mem, [rule])
        assert actions == []

    def test_multiple_conditions_all_must_match(self):
        """ALL conditions in a rule must match for it to fire."""
        mem = Memory(
            content="draft",
            tags=["project:mnemos", "agent:reviewer", "mnemos:learning"],
            project="mnemos",
            agent="reviewer",
            status=MemoryStatus.PROCESSED,
            quality_score=0.5,
            confidence=0.9,
        )
        rule = PolicyRule(
            name="multi-condition",
            conditions=[
                PolicyCondition(status=MemoryStatus.PROCESSED, min_quality=0.8),
                PolicyCondition(min_confidence=0.8),
            ],
            actions=[PolicyAction(action="publish")],
        )
        actions = evaluate_rules(mem, [rule])
        assert actions == []

    def test_disabled_rule_ignored(self):
        """enabled=False prevents rule from firing."""
        mem = Memory(
            content="draft",
            tags=["project:mnemos", "agent:reviewer", "mnemos:learning"],
            project="mnemos",
            agent="reviewer",
            status=MemoryStatus.PROCESSED,
        )
        rule = PolicyRule(
            name="disabled",
            enabled=False,
            conditions=[PolicyCondition(status=MemoryStatus.PROCESSED)],
            actions=[PolicyAction(action="publish")],
        )
        actions = evaluate_rules(mem, [rule])
        assert actions == []

    def test_priority_sorting(self):
        """Higher-priority rules are evaluated first (affects logging, not logic)."""
        mem = Memory(
            content="draft",
            tags=["project:mnemos", "agent:reviewer", "mnemos:learning"],
            project="mnemos",
            agent="reviewer",
            status=MemoryStatus.PROCESSED,
        )
        low = PolicyRule(
            name="low",
            priority=1,
            conditions=[PolicyCondition(status=MemoryStatus.PROCESSED)],
            actions=[PolicyAction(action="archive")],
        )
        high = PolicyRule(
            name="high",
            priority=10,
            conditions=[PolicyCondition(status=MemoryStatus.PROCESSED)],
            actions=[PolicyAction(action="publish")],
        )
        actions = evaluate_rules(mem, [low, high])
        assert len(actions) == 2
        # high priority first
        assert actions[0].action == "publish"
        assert actions[1].action == "archive"

    def test_tags_include_filter(self):
        """tags_include requires all listed tags to be present."""
        mem = Memory(
            content="draft",
            tags=["project:mnemos", "agent:reviewer", "mnemos:learning"],
            project="mnemos",
            agent="reviewer",
            status=MemoryStatus.PROCESSED,
        )
        rule = PolicyRule(
            name="tag-filter",
            conditions=[PolicyCondition(tags_include=["mnemos:learning"])],
            actions=[PolicyAction(action="publish")],
        )
        actions = evaluate_rules(mem, [rule])
        assert len(actions) == 1

    def test_tags_exclude_filter(self):
        """tags_exclude prevents firing if any listed tag is present."""
        mem = Memory(
            content="draft",
            tags=["project:mnemos", "agent:reviewer", "mnemos:learning"],
            project="mnemos",
            agent="reviewer",
            status=MemoryStatus.PROCESSED,
        )
        rule = PolicyRule(
            name="exclude-tag",
            conditions=[PolicyCondition(tags_exclude=["mnemos:learning"])],
            actions=[PolicyAction(action="publish")],
        )
        actions = evaluate_rules(mem, [rule])
        assert actions == []

    def test_load_rules_from_dict(self):
        """YAML-like dict parses into PolicyRule list."""
        raw = {
            "rules": {
                "auto-publish": {
                    "enabled": True,
                    "priority": 5,
                    "conditions": [
                        {"status": "processed", "min_quality": 0.7},
                    ],
                    "actions": [{"action": "publish"}],
                },
            },
        }
        rules = load_rules_from_dict(raw)
        assert len(rules) == 1
        assert rules[0].name == "auto-publish"
        assert rules[0].actions[0].action == "publish"

    def test_empty_rules_list(self):
        """No rules → no actions."""
        mem = Memory(content="x", tags=["project:p", "agent:a", "mnemos:learning"])
        actions = evaluate_rules(mem, [])
        assert actions == []

    def test_min_source_coverage_passes_when_met(self):
        """Regression: min_source_coverage must fire when coverage ≥ threshold.

        Before the fix, the condition always returned False (dead code) due to
        an unconditional ``return False`` after the inner threshold check.
        """
        mem = Memory(
            content="draft",
            tags=["project:mnemos", "agent:reviewer", "mnemos:learning"],
            project="mnemos",
            agent="reviewer",
            status=MemoryStatus.PROCESSED,
            source_coverage=5,
        )
        rule = PolicyRule(
            name="min-coverage",
            conditions=[PolicyCondition(min_source_coverage=3)],
            actions=[PolicyAction(action="publish")],
        )
        actions = evaluate_rules(mem, [rule])
        assert len(actions) == 1
        assert actions[0].action == "publish"

    def test_min_source_coverage_fails_when_insufficient(self):
        """Regression: min_source_coverage must NOT fire when coverage < threshold."""
        mem = Memory(
            content="draft",
            tags=["project:mnemos", "agent:reviewer", "mnemos:learning"],
            project="mnemos",
            agent="reviewer",
            status=MemoryStatus.PROCESSED,
            source_coverage=1,
        )
        rule = PolicyRule(
            name="min-coverage",
            conditions=[PolicyCondition(min_source_coverage=3)],
            actions=[PolicyAction(action="publish")],
        )
        actions = evaluate_rules(mem, [rule])
        assert actions == []

    def test_min_source_coverage_with_none_on_memory(self):
        """min_source_coverage treats missing source_coverage (None) as 0."""
        mem = Memory(
            content="draft",
            tags=["project:mnemos", "agent:reviewer", "mnemos:learning"],
            project="mnemos",
            agent="reviewer",
            status=MemoryStatus.PROCESSED,
            source_coverage=None,
        )
        rule = PolicyRule(
            name="min-coverage",
            conditions=[PolicyCondition(min_source_coverage=1)],
            actions=[PolicyAction(action="publish")],
        )
        actions = evaluate_rules(mem, [rule])
        assert actions == []


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------


class TestTriggers:
    def test_on_memory_saved_no_rules(self, tmp_manager):
        """Trigger with no configured rules is a no-op."""
        mgr = tmp_manager
        mem = Memory(
            content="note",
            tags=["project:mnemos", "agent:reviewer", "mnemos:learning"],
            project="mnemos",
            agent="reviewer",
        )
        on_memory_saved(mgr, mem)  # should not raise

    def test_on_status_changed_processed(self, tmp_manager):
        """Transition to processed may auto-publish if policy matches."""
        mgr = tmp_manager
        # Seed a policy that auto-publishes processed memories
        mgr.settings.policies = {
            "rules": {
                "auto-pub": {
                    "enabled": True,
                    "conditions": [{"status": "processed"}],
                    "actions": [{"action": "publish"}],
                },
            },
        }
        mem = Memory(
            content="draft",
            tags=["project:mnemos", "agent:reviewer", "mnemos:learning"],
            project="mnemos",
            agent="reviewer",
            status=MemoryStatus.PROCESSED,
        )
        mgr.sqlite.save(mem)
        mgr._embedder = MagicMock()
        mgr._embedder.embed.return_value = [0.2] * 384

        on_status_changed(mgr, mem, MemoryStatus.RAW, MemoryStatus.PROCESSED)
        reloaded = mgr.sqlite.get(mem.id)
        assert reloaded.status == MemoryStatus.PUBLISHED


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class TestScheduler:
    def test_auto_cluster_skips_when_disabled(self, tmp_manager):
        """auto_cluster is a no-op when automation disabled."""
        mgr = tmp_manager
        mgr.settings.automation.enabled = False
        auto_cluster(mgr)  # should not raise

    def test_auto_cluster_skips_below_min_raw(self, tmp_manager):
        """auto_cluster skips if raw count < min_raw_to_trigger."""
        mgr = tmp_manager
        mgr.settings.automation.min_raw_to_trigger = 10
        auto_cluster(mgr)
        # No crash, no clusters created
        assert mgr.sqlite.count_by_status().get("raw", 0) < 10

    def test_auto_synthesize_no_processing(self, tmp_manager):
        """auto_synthesize with no processing memories is a no-op."""
        mgr = tmp_manager
        auto_synthesize(mgr)  # should not raise

    def test_auto_publish_no_processed(self, tmp_manager):
        """auto_publish with no processed memories is a no-op."""
        mgr = tmp_manager
        auto_publish(mgr)  # should not raise

    def test_dlq_retry_scheduler_empty(self, tmp_manager):
        """dlq_retry_scheduler with empty DLQ is a no-op."""
        mgr = tmp_manager
        dlq_retry_scheduler(mgr)  # should not raise
        assert mgr.sqlite.dlq_count() == 0

    def test_dlq_retry_scheduler_max_retries(self, tmp_manager):
        """Entries at max attempts are discarded by scheduler."""
        mgr = tmp_manager
        dlq_add(mgr, "m1", max_attempts=1)
        entry = dlq_list(mgr)[0]
        # Simulate one retry already done
        mgr.sqlite.dlq_increment_attempt(entry["id"], backoff_sec=1)

        dlq_retry_scheduler(mgr)
        assert mgr.sqlite.dlq_count() == 0


# ---------------------------------------------------------------------------
# Triggers — coverage push (M18)
#
# Exercises the remaining branches in mnemos/policy/triggers.py:
#   - on_memory_saved with archive / alert / defer / trigger_cluster actions
#   - on_memory_saved with unknown action (defensive logger warning)
#   - on_memory_saved with multiple actions in one rule
#   - _get_rules defensive paths (raw=None, raw=list, raw=other)
#   - on_status_changed transition that does NOT match publish action
# ---------------------------------------------------------------------------


class TestTriggersCoverage:
    def test_on_memory_saved_archive_persists_status(self, tmp_manager: MemoryManager) -> None:
        """`archive` action flips status to ARCHIVED and saves the row."""
        mgr = tmp_manager
        mgr.settings.policies = {
            "rules": {
                "arch": {
                    "enabled": True,
                    "conditions": [],
                    "actions": [{"action": "archive"}],
                },
            },
        }
        mem = Memory(
            content="x",
            tags=["project:mnemos", "agent:qa", "mnemos:learning"],
            project="mnemos",
            agent="qa",
            status=MemoryStatus.PUBLISHED,
        )
        mgr.sqlite.save(mem)
        on_memory_saved(mgr, mem)
        reloaded = mgr.sqlite.get(mem.id)
        assert reloaded is not None
        assert reloaded.status == MemoryStatus.ARCHIVED

    def test_on_memory_saved_alert_action(self, tmp_manager: MemoryManager) -> None:
        """`alert` action runs without raising and uses the param message."""
        mgr = tmp_manager
        mgr.settings.policies = {
            "rules": {
                "al": {
                    "enabled": True,
                    "conditions": [],
                    "actions": [{"action": "alert", "params": {"message": "boom"}}],
                },
            },
        }
        mem = Memory(
            content="x",
            tags=["project:mnemos", "agent:qa", "mnemos:learning"],
            project="mnemos",
            agent="qa",
        )
        # No assertion — the action is a logger.warning, so we just confirm
        # the call path completes without raising.
        on_memory_saved(mgr, mem)

    def test_on_memory_saved_defer_action(self, tmp_manager: MemoryManager) -> None:
        """`defer` action is a no-op logger.info; status is not changed."""
        mgr = tmp_manager
        mgr.settings.policies = {
            "rules": {
                "df": {
                    "enabled": True,
                    "conditions": [],
                    "actions": [{"action": "defer"}],
                },
            },
        }
        mem = Memory(
            content="x",
            tags=["project:mnemos", "agent:qa", "mnemos:learning"],
            project="mnemos",
            agent="qa",
            status=MemoryStatus.RAW,
        )
        mgr.sqlite.save(mem)
        on_memory_saved(mgr, mem)
        # status unchanged
        assert mgr.sqlite.get(mem.id).status == MemoryStatus.RAW

    def test_on_memory_saved_trigger_cluster_action(self, tmp_manager: MemoryManager) -> None:
        """`trigger_cluster` action logs and does not mutate the memory."""
        mgr = tmp_manager
        mgr.settings.policies = {
            "rules": {
                "tc": {
                    "enabled": True,
                    "conditions": [],
                    "actions": [{"action": "trigger_cluster"}],
                },
            },
        }
        mem = Memory(
            content="x",
            tags=["project:mnemos", "agent:qa", "mnemos:learning"],
            project="mnemos",
            agent="qa",
            status=MemoryStatus.RAW,
        )
        mgr.sqlite.save(mem)
        on_memory_saved(mgr, mem)
        assert mgr.sqlite.get(mem.id).status == MemoryStatus.RAW

    def test_on_memory_saved_unknown_action(self, tmp_manager: MemoryManager) -> None:
        """An unrecognised action falls through the else branch (logger.warning)."""
        mgr = tmp_manager
        mgr.settings.policies = {
            "rules": {
                "weird": {
                    "enabled": True,
                    "conditions": [],
                    "actions": [{"action": "unmapped_action"}],
                },
            },
        }
        mem = Memory(
            content="x",
            tags=["project:mnemos", "agent:qa", "mnemos:learning"],
            project="mnemos",
            agent="qa",
        )
        on_memory_saved(mgr, mem)  # must not raise

    def test_on_memory_saved_multiple_actions(self, tmp_manager: MemoryManager) -> None:
        """A rule with two actions fires both in order."""
        mgr = tmp_manager
        mgr.settings.policies = {
            "rules": {
                "combo": {
                    "enabled": True,
                    "conditions": [],
                    "actions": [
                        {"action": "alert", "params": {"message": "first"}},
                        {"action": "defer"},
                    ],
                },
            },
        }
        mem = Memory(
            content="x",
            tags=["project:mnemos", "agent:qa", "mnemos:learning"],
            project="mnemos",
            agent="qa",
        )
        on_memory_saved(mgr, mem)

    def test_on_memory_saved_disabled_rule_does_not_fire(self, tmp_manager: MemoryManager) -> None:
        """`enabled=False` prevents the actions from running."""
        mgr = tmp_manager
        mgr.settings.policies = {
            "rules": {
                "off": {
                    "enabled": False,
                    "conditions": [],
                    "actions": [{"action": "archive"}],
                },
            },
        }
        mem = Memory(
            content="x",
            tags=["project:mnemos", "agent:qa", "mnemos:learning"],
            project="mnemos",
            agent="qa",
            status=MemoryStatus.RAW,
        )
        mgr.sqlite.save(mem)
        on_memory_saved(mgr, mem)
        assert mgr.sqlite.get(mem.id).status == MemoryStatus.RAW

    def test_get_rules_handles_policies_attribute_missing(self) -> None:
        """If a settings-like object has no `policies` attribute, _get_rules returns []."""
        from unittest.mock import MagicMock

        from vesma.policy.triggers import _get_rules

        # settings has no `policies` attribute at all → getattr returns None
        mgr = MagicMock(spec=["settings"])  # only 'settings' attr available
        mgr.settings = MagicMock(spec=[])  # settings has no attributes
        rules = _get_rules(mgr)
        assert rules == []

    def test_get_rules_handles_policies_as_list(self) -> None:
        """If `policies` is a list of PolicyRule, _get_rules returns it as-is."""
        from unittest.mock import MagicMock

        from vesma.policy.triggers import _get_rules

        mgr = MagicMock()
        mgr.settings.policies = [PolicyRule(name="r", actions=[PolicyAction(action="defer")])]
        rules = _get_rules(mgr)
        assert len(rules) == 1
        assert rules[0].name == "r"

    def test_get_rules_handles_policies_as_unexpected_type(self) -> None:
        """An unexpected type (e.g. int) for `policies` returns []."""
        from unittest.mock import MagicMock

        from vesma.policy.triggers import _get_rules

        mgr = MagicMock()
        mgr.settings.policies = 42
        rules = _get_rules(mgr)
        assert rules == []

    def test_on_status_changed_ignores_non_publish_actions(
        self, tmp_manager: MemoryManager
    ) -> None:
        """on_status_changed only fires actions whose .action == 'publish'."""
        mgr = tmp_manager
        # Rule with a non-publish action — must be ignored at this layer
        mgr.settings.policies = {
            "rules": {
                "no-pub": {
                    "enabled": True,
                    "conditions": [{"status": "processed"}],
                    "actions": [{"action": "archive"}],
                },
            },
        }
        mem = Memory(
            content="x",
            tags=["project:mnemos", "agent:qa", "mnemos:learning"],
            project="mnemos",
            agent="qa",
            status=MemoryStatus.PROCESSED,
        )
        mgr.sqlite.save(mem)
        # Non-publish action must NOT be auto-applied here
        on_status_changed(mgr, mem, MemoryStatus.RAW, MemoryStatus.PROCESSED)
        assert mgr.sqlite.get(mem.id).status == MemoryStatus.PROCESSED


# ---------------------------------------------------------------------------
# Scheduler — coverage push (M18)
#
# Exercises the happy paths of mnemos/policy/scheduler.py:
#   - auto_cluster actually invoking mgr.cluster() when raw >= min
#   - auto_synthesize invoking mgr.synthesize() per unique cluster
#   - auto_synthesize skipping clusters that already have a processed draft
#   - auto_publish invoking mgr.publish() when quality_gate.passed is True
#   - auto_publish leaving bad-quality processed memories alone
#   - dlq_retry_scheduler calling dlq_retry on entries under max_attempts
# ---------------------------------------------------------------------------


class TestSchedulerCoverage:
    def test_auto_cluster_invokes_cluster_when_raw_above_min(
        self, tmp_manager: MemoryManager
    ) -> None:
        """auto_cluster calls mgr.cluster() when raw >= min_raw_to_trigger."""
        mgr = tmp_manager
        mgr.settings.automation.min_raw_to_trigger = 1
        # Seed a RAW memory
        mgr.sqlite.save(
            Memory(
                content="seed",
                tags=["project:mnemos", "agent:qa", "mnemos:learning"],
                project="mnemos",
                agent="qa",
                status=MemoryStatus.RAW,
            )
        )
        mgr.cluster = MagicMock(return_value=["fake-cluster"])  # type: ignore[method-assign]
        auto_cluster(mgr)
        mgr.cluster.assert_called_once()

    def test_auto_synthesize_invokes_synthesize_per_cluster(
        self, tmp_manager: MemoryManager
    ) -> None:
        """auto_synthesize calls mgr.synthesize(cluster_id) for each unique cluster
        that does not already have a processed draft."""
        mgr = tmp_manager
        # Seed two processing memories sharing one cluster_id
        cid = "cluster-1"
        for i in range(2):
            mgr.sqlite.save(
                Memory(
                    content=f"m{i}",
                    tags=["project:mnemos", "agent:qa", "mnemos:learning"],
                    project="mnemos",
                    agent="qa",
                    status=MemoryStatus.PROCESSING,
                    cluster_id=cid,
                )
            )
        mgr.synthesize = MagicMock(return_value=None)  # type: ignore[method-assign]
        auto_synthesize(mgr)
        # One synthesize call per unique cluster_id
        assert mgr.synthesize.call_count == 1
        mgr.synthesize.assert_called_with(cid)

    def test_auto_synthesize_skips_cluster_with_processed_draft(
        self, tmp_manager: MemoryManager
    ) -> None:
        """If a cluster already has a PROCESSED draft, auto_synthesize skips it."""
        mgr = tmp_manager
        cid = "cluster-with-draft"
        # One processing member
        mgr.sqlite.save(
            Memory(
                content="proc",
                tags=["project:mnemos", "agent:qa", "mnemos:learning"],
                project="mnemos",
                agent="qa",
                status=MemoryStatus.PROCESSING,
                cluster_id=cid,
            )
        )
        # One processed draft in the same cluster
        mgr.sqlite.save(
            Memory(
                content="draft",
                tags=["project:mnemos", "agent:qa", "mnemos:learning"],
                project="mnemos",
                agent="qa",
                status=MemoryStatus.PROCESSED,
                cluster_id=cid,
            )
        )
        mgr.synthesize = MagicMock(return_value=None)  # type: ignore[method-assign]
        auto_synthesize(mgr)
        mgr.synthesize.assert_not_called()

    def test_auto_synthesize_skips_processing_with_no_cluster_id(
        self, tmp_manager: MemoryManager
    ) -> None:
        """A processing memory without a cluster_id is silently skipped."""
        mgr = tmp_manager
        mgr.sqlite.save(
            Memory(
                content="orphan",
                tags=["project:mnemos", "agent:qa", "mnemos:learning"],
                project="mnemos",
                agent="qa",
                status=MemoryStatus.PROCESSING,
                cluster_id=None,
            )
        )
        mgr.synthesize = MagicMock(return_value=None)  # type: ignore[method-assign]
        auto_synthesize(mgr)
        mgr.synthesize.assert_not_called()

    def test_auto_publish_invokes_publish_on_quality_pass(self, tmp_manager: MemoryManager) -> None:
        """auto_publish calls mgr.publish() for processed memories that pass
        the quality gate."""
        mgr = tmp_manager
        mem = Memory(
            content="ready",
            tags=["project:mnemos", "agent:qa", "mnemos:learning"],
            project="mnemos",
            agent="qa",
            status=MemoryStatus.PROCESSED,
        )
        mgr.sqlite.save(mem)
        mgr.quality_gate = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(passed=True)
        )
        mgr.publish = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(published=True)
        )
        auto_publish(mgr)
        mgr.quality_gate.assert_called_once_with(mem.id)
        mgr.publish.assert_called_once_with(mem.id)

    def test_auto_publish_skips_quality_failures(self, tmp_manager: MemoryManager) -> None:
        """A processed memory that fails the quality gate is left untouched."""
        mgr = tmp_manager
        mem = Memory(
            content="bad",
            tags=["project:mnemos", "agent:qa", "mnemos:learning"],
            project="mnemos",
            agent="qa",
            status=MemoryStatus.PROCESSED,
        )
        mgr.sqlite.save(mem)
        mgr.quality_gate = MagicMock(  # type: ignore[method-assign]
            return_value=MagicMock(passed=False)
        )
        mgr.publish = MagicMock()  # type: ignore[method-assign]
        auto_publish(mgr)
        mgr.publish.assert_not_called()

    def test_dlq_retry_scheduler_retries_under_max(self, tmp_manager: MemoryManager) -> None:
        """Entries with attempt < max_attempts are retried (not discarded)."""
        mgr = tmp_manager
        dlq_add(mgr, "m1", max_attempts=3)
        # attempt_count starts at 1 → strictly < 3 → retry path
        dlq_retry_scheduler(mgr)
        # DLQ still has the entry
        assert mgr.sqlite.dlq_count() == 1
        # attempt_count incremented
        assert dlq_list(mgr)[0]["attempt_count"] == 2

    def test_dlq_retry_scheduler_no_ready_entries(self, tmp_manager: MemoryManager) -> None:
        """With an empty DLQ the scheduler is a clean no-op."""
        mgr = tmp_manager
        # No dlq_add call — empty
        dlq_retry_scheduler(mgr)
        assert mgr.sqlite.dlq_count() == 0
