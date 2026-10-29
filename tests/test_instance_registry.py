"""ADR-0049 wave 0: the instance registry skeleton (XDG state, JSON).

Pins: round-trip upsert/load/forget, the advisory contract (a corrupt or
unwritable registry never fails a reader with an exception and never
fails an install), atomic 0600 writes, and the settings-derived user
profile writer.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vesma.service.instance_registry import (
    InstanceEntry,
    forget_instance,
    forget_profile_instance,
    load_registry,
    record_instance,
    record_system_profile_instance,
    record_user_profile_instance,
    registry_path,
)


@pytest.fixture(autouse=True)
def _hermetic_state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


def _entry(profile: str = "user", home: str = "/h1") -> InstanceEntry:
    return InstanceEntry(
        profile=profile,
        home=home,
        endpoint="http://127.0.0.1:8787",
        unit="vesma.service",
        instance_id=None,
        updated_at="2026-10-28T00:00:00+00:00",
    )


def test_missing_registry_reads_empty() -> None:
    state = load_registry()
    assert state.entries == ()
    assert state.problem is None


def test_round_trip_upsert_load_forget() -> None:
    assert record_instance(_entry()) is None
    state = load_registry()
    assert len(state.entries) == 1
    row = state.entries[0]
    assert row.profile == "user"
    assert row.home == "/h1"
    assert row.endpoint == "http://127.0.0.1:8787"
    assert row.instance_id is None

    # Upsert by (profile, home): the row is REPLACED, not duplicated.
    updated = InstanceEntry(
        profile="user",
        home="/h1",
        endpoint=None,
        unit="new.service",
        instance_id=None,
        updated_at="2026-10-28T01:00:00+00:00",
    )
    assert record_instance(updated) is None
    state = load_registry()
    assert len(state.entries) == 1
    assert state.entries[0].unit == "new.service"

    assert forget_instance(profile="user", home="/h1") is None
    assert load_registry().entries == ()
    # Forgetting an absent row is not an error.
    assert forget_instance(profile="user", home="/h1") is None


def test_two_profiles_coexist() -> None:
    assert record_instance(_entry(profile="user", home="/h1")) is None
    assert record_instance(_entry(profile="system", home="/var/lib/vesma")) is None
    state = load_registry()
    assert [item.profile for item in state.entries] == ["system", "user"]  # sorted


def test_corrupt_registry_reads_empty_with_problem() -> None:
    path = registry_path()
    path.parent.mkdir(parents=True)
    path.write_text("{ not json", encoding="utf-8")
    state = load_registry()
    assert state.entries == ()
    assert state.problem is not None and "unreadable" in state.problem


def test_unparsable_row_reads_empty_with_problem() -> None:
    path = registry_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"version": 1, "instances": [{"nope": 1}]}), encoding="utf-8")
    state = load_registry()
    assert state.entries == ()
    assert state.problem is not None and "unparsable row" in state.problem


def test_write_is_atomic_0600() -> None:
    assert record_instance(_entry()) is None
    path = registry_path()
    assert (path.stat().st_mode & 0o777) == 0o600
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert len(payload["instances"]) == 1
    assert not list(path.parent.glob("*.tmp")), "no tmp residue"


def test_forget_profile_drops_whole_profile() -> None:
    assert record_instance(_entry(profile="user", home="/h1")) is None
    assert forget_profile_instance(profile="user") is None
    assert load_registry().entries == ()
    assert forget_profile_instance(profile="user") is None  # already absent


def test_user_profile_writer_uses_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VESMA_DATA_DIR", str(tmp_path / "custom-data"))
    monkeypatch.setenv("VESMA_API__PORT", "9001")
    assert record_user_profile_instance("vesma.service") is None
    state = load_registry()
    assert len(state.entries) == 1
    row = state.entries[0]
    assert row.profile == "user"
    assert row.home == str((tmp_path / "custom-data").resolve())
    assert row.endpoint == "http://127.0.0.1:9001"
    assert row.unit == "vesma.service"


def test_unwritable_registry_reports_problem_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file", encoding="utf-8")  # parent exists as a FILE
    monkeypatch.setattr(
        "vesma.service.instance_registry.registry_path",
        lambda: blocker / "vesma" / "instances.json",
    )
    problem = record_instance(_entry())
    assert problem is not None and "update failed" in problem


# ── wave 1 (ADR-0049 Lock 3): instance_id is minted and recorded ──────


class TestWave1InstanceIdFill:
    def test_user_writer_mints_and_records(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from vesma.service.instance import id_file_path

        home = tmp_path / "home" / ".vesma" / "data"
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.delenv("VESMA_INSTANCE_ID", raising=False)
        assert record_user_profile_instance("vesma.service") is None
        state = load_registry()
        assert len(state.entries) == 1
        row = state.entries[0]
        assert row.instance_id is not None
        # the SAME identity is persisted in the store home (stable across
        # restarts; the boot path re-reads exactly this file)
        assert id_file_path(home).read_text(encoding="utf-8").strip() == row.instance_id

    def test_user_writer_records_env_claimed_identity(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("VESMA_INSTANCE_ID", "work")
        # The assertion targets the DEFAULT endpoint: ambient VESMA_API__*
        # overrides (e.g. leaked by an earlier in-process serve invocation)
        # must not redefine what "default" means here.
        monkeypatch.delenv("VESMA_API__HOST", raising=False)
        monkeypatch.delenv("VESMA_API__PORT", raising=False)
        assert record_user_profile_instance("vesma.service") is None
        row = load_registry().entries[0]
        assert row.instance_id == "work"
        assert row.endpoint == "http://127.0.0.1:8787"

    def test_system_writer_mints_and_records(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import grp
        import os
        import pwd

        from vesma.service import fhs

        system_root = tmp_path / "var" / "lib" / "vesma"
        monkeypatch.setattr(fhs, "system_data_root", lambda prefix=None: system_root, raising=False)
        # Ownership resolves to THIS test account: a non-root chown to
        # another account would be refused — the assertion targets the
        # mint + readability, the root-only account transfer is an
        # installer-flow concern.
        monkeypatch.setattr("vesma.service.unitgen.SYSTEM_USER", pwd.getpwuid(os.geteuid()).pw_name)
        monkeypatch.setattr("vesma.service.unitgen.SYSTEM_GROUP", grp.getgrgid(os.getgid()).gr_name)

        result = record_system_profile_instance("vesma.service")
        state = load_registry()
        assert len(state.entries) == 1
        row = state.entries[0]
        assert row.profile == "system"
        assert row.instance_id is not None, result
        id_file = system_root / "instance-id"
        assert id_file.exists()
        assert id_file.read_text(encoding="utf-8").strip() == row.instance_id
        assert id_file.stat().st_mode & 0o777 == 0o644
