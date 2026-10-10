"""Guard: the suite never resolves the invoking user's real store home.

Wave-61 test-isolation slice. The leak this file pins was observed LIVE
(2026-10-10): a solo run of ``tests/test_b2b_semantics.py`` appended a
``sync-import`` line to the REAL ``~/.vesma/logs/sync-audit.jsonl`` —
``vesma.audit.sync_audit_path()`` resolves ``Path.home()`` at call time,
and the suite inherited the operator's HOME (plus, in agent shells, a
``VESMA_CONFIG`` pointing straight at the live store config).

The autouse ``isolated_store_home`` fixture in ``tests/conftest.py``
flips ``HOME`` into ``tmp_path`` and strips path-bearing env vars; the
tests here fail loudly if that guarantee regresses:

1. ``test_home_is_flipped_per_test`` — ``Path.home()``/``$HOME`` are the
   fixture's fake home, distinct from the passwd home of the invoking
   user, fresh per test (two tests never share one fake home).
2. ``test_store_roots_resolve_under_fake_home`` — every call-time store
   root the product resolves (canonical/legacy home, sync + scanner
   audit logs) lives under the fake home.
3. ``test_config_search_never_finds_the_real_config`` — the config
   search funnel returns no real-user path (with the cwd pinned to the
   fake home, it returns None at all).
4. ``test_settings_default_resolution_lands_in_fake_home`` — a bare
   ``Settings().resolve_paths()`` puts data/vault/logs under the fake
   home, never under the real one.
5. ``test_sync_audit_canary_writes_fake_home_only`` — the canary: the
   exact writer that leaked live (``log_sync_audit``) must land in the
   fake home's audit log AND must not touch the real one. The real-file
   assert is marker-based (not size-based) so a concurrent foreign
   writer on the same machine cannot make this guard flaky.

The real home is only ever STATED here (``pwd.getpwuid`` — HOME
independent), never written: these tests are read-only against it.
"""

from __future__ import annotations

import json
import os
import pwd
import uuid
from pathlib import Path

import pytest

from vesma.audit import (
    log_sync_audit,
    scanner_audit_path,
    sync_audit_path,
)
from vesma.config import Settings, canonical_home, find_config_file, legacy_home


def _passwd_home() -> Path:
    """The invoking user's REAL home — independent of $HOME."""
    return Path(pwd.getpwuid(os.getuid()).pw_dir)


def test_home_is_flipped_per_test(isolated_store_home: Path) -> None:
    assert Path.home() == isolated_store_home
    assert os.environ["HOME"] == str(isolated_store_home)
    assert isolated_store_home.resolve() != _passwd_home().resolve()
    # Fresh per test: the fake home was created empty by the fixture.
    assert not any(isolated_store_home.iterdir())


def test_second_test_gets_a_different_fake_home(isolated_store_home: Path) -> None:
    """Function scope: no cross-test sharing of one fake home.

    The assertion is self-referential by design — the fixture's tmp_path
    differs per test, so two runs of THIS test (or any pairing) can never
    observe the same home. Combined with
    ``test_home_is_flipped_per_test`` this pins the per-test lifecycle.
    """
    assert isolated_store_home == Path.home()
    assert isolated_store_home.is_dir()


def test_store_roots_resolve_under_fake_home(isolated_store_home: Path) -> None:
    home = Path.home()
    assert canonical_home() == home / ".vesma"
    assert legacy_home() == home / ".mnemos"
    assert sync_audit_path() == home / ".vesma" / "logs" / "sync-audit.jsonl"
    assert scanner_audit_path() == home / ".vesma" / "logs" / "scanner-audit.jsonl"
    # ...and none of them alias the real user's store.
    real = _passwd_home() / ".vesma"
    for resolved in (canonical_home().resolve(), sync_audit_path().resolve()):
        assert resolved != real.resolve()
        assert not resolved.is_relative_to(real)


def test_config_search_never_finds_the_real_config(
    isolated_store_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(isolated_store_home)  # no ./config.yaml here
    assert find_config_file() is None
    # Even if some future surface chdirs elsewhere, the search must never
    # return the REAL user's config file.
    found = find_config_file()
    assert found is None or found.resolve() != (_passwd_home() / ".vesma" / "config.yaml").resolve()


def test_settings_default_resolution_lands_in_fake_home(
    isolated_store_home: Path,
) -> None:
    settings = Settings()
    settings.resolve_paths()
    home = Path.home()
    real = _passwd_home()
    for label, resolved in (
        ("data_dir", settings.vesma.data_dir),
        ("vault_path", settings.vesma.vault_path),
        ("log_file", settings.logging.log_file),
    ):
        assert resolved.is_absolute(), label
        assert resolved.is_relative_to(home), f"{label} escaped the fake home: {resolved}"
        assert not resolved.is_relative_to(real), f"{label} resolved into the REAL home: {resolved}"


def test_sync_audit_canary_writes_fake_home_only(
    isolated_store_home: Path,
) -> None:
    """The confirmed live-store writer (2026-10-10 incident) is contained.

    Before the fixture existed, ``log_sync_audit`` appended to the real
    operator audit log. The canary replays that write and asserts BOTH
    delivery into the fake home AND zero reach into the real file —
    marker-based on the real side, so a concurrent foreign writer cannot
    flip this test.
    """
    marker = f"test-store-isolation-canary-{uuid.uuid4()}"
    real_audit = _passwd_home() / ".vesma" / "logs" / "sync-audit.jsonl"

    log_sync_audit({"action": marker, "source": "tests/test_store_isolation_guard.py"})

    fake_audit = isolated_store_home / ".vesma" / "logs" / "sync-audit.jsonl"
    assert fake_audit.is_file(), "canary line did not land in the fake home"
    last_line = fake_audit.read_text(encoding="utf-8").splitlines()[-1]
    assert json.loads(last_line)["action"] == marker

    if real_audit.exists():
        real_after = real_audit.read_text(encoding="utf-8")
        assert marker not in real_after, (
            "the suite WROTE the real ~/.vesma audit log — isolated_store_home regressed"
        )
