"""Wave 5 (layout v2) — pin tests for ``migrate-store --target=system``.

Every test pins a numbered contract point of ADR-0047 (C1-C15, ratified
ArchCom 2026-10-22). The fake system profile uses the dest-dir technique:
``fhs.default_prefix`` is monkeypatched to a temp prefix, the systemctl
probes are monkeypatched (hermetic: the host's real unit state must not
decide a test). The C13 activation gate is OPEN in the product (owner
ratification 2026-10-23), so apply-path tests run against the real
constant; the typed refusal stays pinned via the ``gate_closed``
fixture, which flips the constant back to False.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import stat as stat_mod
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

import vesma.service.fhs as fhs_mod
import vesma.service.system_install as system_install_mod
import vesma.store_migration as sm
from vesma.cli.main import app
from vesma.service import fhs, system_install
from vesma.service.doctor_checks import Finding, Severity, system_target_forecast
from vesma.storage.sqlite_store import _DB_SCHEMA

runner = CliRunner()

STORE_DB_NAME = "vesma.db"
LEGACY_NAME = "mnemos.db"


def _output(result: Any) -> str:
    try:
        return result.output + (result.stderr or "")
    except ValueError:  # pragma: no cover - click <8.2 mixes the streams
        return result.output


# ── Fabrication helpers ───────────────────────────────────────────────────────


def _insert_records(db_path: Path, *, canonical: bool = False) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(_DB_SCHEMA)
        tags = (
            '["vesma:note","agent:user","mnemos:no-federate"]'
            if canonical
            else '["project:mnemos","agent:user","mnemos:learning"]'
        )
        project = "vesma" if canonical else "mnemos"
        conn.execute(
            """INSERT INTO memories
               (id, content, title, tags, source, memory_type, created_at,
                updated_at, metadata, project, agent, status, marker_version)
               VALUES ('id-1', 'content-one — «юникод»', 'title-1', ?, 'mcp',
                       'note', '2026-10-22T00:00:00Z', '2026-10-22T00:00:00Z',
                       '{}', ?, 'user', 'published', 1)""",
            (tags, project),
        )
        conn.commit()
    finally:
        conn.close()


def make_source_5x(tmp_path: Path) -> Path:
    """A fabricated 5.x user store: data/mnemos.db + siblings + vault,
    carrying the byte-stable trust marker (C11 pin material)."""
    home = tmp_path / "old-home"
    data = home / "data"
    data.mkdir(parents=True)
    conn = sqlite3.connect(str(data / LEGACY_NAME))
    try:
        conn.executescript(_DB_SCHEMA)
        conn.execute(
            """INSERT INTO memories
               (id, content, title, tags, source, memory_type, created_at,
                updated_at, metadata, project, agent, status, marker_version)
               VALUES ('id-1', 'content-one — «юникод»', 'title-1', ?, 'mcp',
                       'note', '2026-10-22T00:00:00Z', '2026-10-22T00:00:00Z',
                       '{}', 'mnemos', 'user', 'published', 1)""",
            ('["project:mnemos","agent:user","mnemos:learning","mnemos:no-federate"]',),
        )
        conn.commit()
    finally:
        conn.close()
    vectors = sqlite3.connect(str(data / "vectors.db"))
    try:
        vectors.execute("CREATE TABLE vec (id TEXT PRIMARY KEY)")
        vectors.execute("INSERT INTO vec VALUES ('id-1')")
        vectors.commit()
    finally:
        vectors.close()
    (data / "plain.txt").write_text("sibling plain file", encoding="utf-8")
    (data / "agent-token-signing.key").write_bytes(b"\x01\x02signing")
    vault = home / "vault"
    (vault / "sub").mkdir(parents=True)
    (vault / "a.md").write_text("# vault note", encoding="utf-8")
    (vault / "sub" / "b.txt").write_text("vault-b", encoding="utf-8")
    (home / "config.yaml").write_text(
        "vesma:\n  db_name: vesma.db\n  data_dir: %s\n" % (home / "data"),
        encoding="utf-8",
    )
    return home


def make_source_6x(tmp_path: Path) -> Path:
    """A fabricated already-migrated 6.0 user store (the EXPECTED system
    source: zero rewrite counters → C14 fast path)."""
    home = tmp_path / "six-home"
    data = home / "data"
    data.mkdir(parents=True)
    _insert_records(data / STORE_DB_NAME, canonical=True)
    (data / "readme.txt").write_text("plain", encoding="utf-8")
    vault = home / "vault"
    vault.mkdir()
    (vault / "a.md").write_text("# note", encoding="utf-8")
    return home


def make_system_prefix(
    tmp_path: Path, *, extra_artifacts: list[dict[str, Any]] | None = None
) -> Path:
    """A fake system profile prefix: install-state + empty data/config roots."""
    prefix = tmp_path / "prefix"
    data_root = prefix / "var" / "lib" / "vesma"
    config_root = prefix / "etc" / "vesma"
    data_root.mkdir(parents=True)
    config_root.mkdir(parents=True)
    artifacts: list[dict[str, Any]] = [
        {
            "role": "data_root",
            "kind": "dir",
            "path": str(data_root),
            "mode": "0750",
            "owner": "vesma",
            "group": "vesma",
            "status": "created",
            "preserve": True,
            "remove_mode": "skip",
        },
        {
            "role": "config_root",
            "kind": "dir",
            "path": str(config_root),
            "mode": "0750",
            "owner": "root",
            "group": "vesma",
            "status": "created",
            "preserve": False,
            "remove_mode": "strict-dir",
        },
    ]
    artifacts.extend(extra_artifacts or [])
    state = {
        "schema": 1,
        "profile": "system",
        "generation": "v2",
        "engine_version": "test",
        "prefix": str(prefix),
        "artifacts": artifacts,
    }
    (data_root / "install-state.yaml").write_text(yaml.safe_dump(state), encoding="utf-8")
    return prefix


@pytest.fixture
def sys_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Fake system profile + hermetic probes; returns the path bundle."""
    prefix = make_system_prefix(tmp_path)
    monkeypatch.setattr(fhs_mod, "default_prefix", lambda: prefix)
    monkeypatch.setattr(
        sm, "_run_systemctl", lambda args: "inactive" if "is-active" in args else ""
    )
    return {
        "prefix": prefix,
        "data_root": prefix / "var" / "lib" / "vesma",
        "config_root": prefix / "etc" / "vesma",
        "config": prefix / "etc" / "vesma" / "config.yaml",
    }


@pytest.fixture
def gate_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Close the C13 gate (the product surface is open since the
    2026-10-23 ratification): pins the belt-and-braces typed refusal."""
    monkeypatch.setattr(sm, "_ACTIVATION_GATE_OPEN", False)


def _tree_digest(root: Path) -> list[tuple[str, int, int, int, str]]:
    """C6 pin material: (relpath, mode, uid, gid, sha256) of every file."""
    rows: list[tuple[str, int, int, int, str]] = []
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        st = path.lstat()
        if stat_mod.S_ISREG(st.st_mode):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            digest = ""
        rows.append((rel, stat_mod.S_IMODE(st.st_mode), st.st_uid, st.st_gid, digest))
    return rows


def _target_db(data_root: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(data_root / STORE_DB_NAME))
    conn.row_factory = sqlite3.Row
    return conn


def _plan_system(home: Path, **kwargs: Any) -> sm.MigrationPlan:
    return sm.build_plan(home, None, apply=kwargs.pop("apply", False), target="system", **kwargs)


# ── C1: placement + preconditions ─────────────────────────────────────────────


def test_c1_no_install_state_refuses_typed_with_remediation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fhs_mod, "default_prefix", lambda: tmp_path / "prefix")
    monkeypatch.setattr(sm, "_run_systemctl", lambda args: "")
    home = make_source_5x(tmp_path)
    result = runner.invoke(app, ["migrate-store", "--from", str(home), "--target", "system"])
    assert result.exit_code == 10, _output(result)
    text = _output(result)
    assert "vesma system install" in text
    assert "NEVER" in text or "never" in text  # no auto-install (C1)
    # nothing was written anywhere (no snapshot next to the source)
    assert not list((tmp_path).glob("migrate-snapshot-*"))


def test_c1_unparsable_install_state_refuses(sys_env: dict[str, Path], tmp_path: Path) -> None:
    (sys_env["data_root"] / "install-state.yaml").write_text("{ not: yaml", encoding="utf-8")
    home = make_source_5x(tmp_path)
    result = runner.invoke(app, ["migrate-store", "--from", str(home), "--target", "system"])
    assert result.exit_code == 10, _output(result)


def test_c1_wrong_profile_refuses(sys_env: dict[str, Path], tmp_path: Path) -> None:
    state_doc = yaml.safe_load(
        (sys_env["data_root"] / "install-state.yaml").read_text(encoding="utf-8")
    )
    state_doc["profile"] = "user"
    (sys_env["data_root"] / "install-state.yaml").write_text(
        yaml.safe_dump(state_doc), encoding="utf-8"
    )
    home = make_source_5x(tmp_path)
    result = runner.invoke(app, ["migrate-store", "--from", str(home), "--target", "system"])
    assert result.exit_code == 10, _output(result)
    assert "profile='user'" in _output(result)


def test_c1_profile_isolation_source_inside_data_root_refuses(
    sys_env: dict[str, Path],
) -> None:
    home = sys_env["data_root"] / "nested" / "store"
    (home / "data").mkdir(parents=True)
    _insert_records(home / "data" / LEGACY_NAME)
    result = runner.invoke(app, ["migrate-store", "--from", str(home), "--target", "system"])
    assert result.exit_code == 12, _output(result)
    assert "DR-14" in _output(result)


def test_c1_dr17_drift_refuses(sys_env: dict[str, Path], tmp_path: Path) -> None:
    state_doc = yaml.safe_load(
        (sys_env["data_root"] / "install-state.yaml").read_text(encoding="utf-8")
    )
    state_doc["artifacts"].append(
        {
            "role": "component_data",
            "kind": "dir",
            "path": str(sys_env["data_root"] / "gone-component"),
            "mode": "0750",
            "owner": "vesma",
            "group": "vesma",
            "status": "created",
            "preserve": False,
            "remove_mode": "strict-dir",
        }
    )
    (sys_env["data_root"] / "install-state.yaml").write_text(
        yaml.safe_dump(state_doc), encoding="utf-8"
    )
    home = make_source_5x(tmp_path)
    result = runner.invoke(app, ["migrate-store", "--from", str(home), "--target", "system"])
    assert result.exit_code == 13, _output(result)
    assert "DR-17" in _output(result)


@pytest.mark.parametrize(
    "plant",
    ["vesma.db", "mnemos.db", "data", "vault", ".migrate-store-applied.json"],
)
def test_c1_store_content_in_data_root_refuses(
    sys_env: dict[str, Path], tmp_path: Path, plant: str
) -> None:
    victim = sys_env["data_root"] / plant
    if plant.endswith(".db") or plant.endswith(".json"):
        victim.write_text("", encoding="utf-8")
    else:
        victim.mkdir()
    home = make_source_5x(tmp_path)
    result = runner.invoke(app, ["migrate-store", "--from", str(home), "--target", "system"])
    assert result.exit_code == 11, _output(result)
    assert plant in _output(result)


def test_c1_installer_scaffolding_is_allowed(sys_env: dict[str, Path], tmp_path: Path) -> None:
    """The narrowed rule: scaffolding present, store content absent → plan."""
    (sys_env["data_root"] / "history").mkdir()
    (sys_env["data_root"] / "board").mkdir()  # component data slot
    home = make_source_5x(tmp_path)
    result = runner.invoke(app, ["migrate-store", "--from", str(home), "--target", "system"])
    assert result.exit_code == 0, _output(result)


def test_c1_zero_hardcoded_system_paths_in_mover_source() -> None:
    """C1 pin: the mover code resolves system paths ONLY through the FHS
    resolvers — the canonical literals never appear in its source."""
    source = Path(sm.__file__).read_text(encoding="utf-8")
    assert "/var/lib/vesma" not in source
    assert "/etc/vesma" not in source


def test_c1_to_flag_forbidden_with_system_target(sys_env: dict[str, Path], tmp_path: Path) -> None:
    home = make_source_5x(tmp_path)
    result = runner.invoke(
        app,
        [
            "migrate-store",
            "--from",
            str(home),
            "--to",
            str(tmp_path / "elsewhere"),
            "--target",
            "system",
        ],
    )
    assert result.exit_code == 2, _output(result)


# ── C2: cargo + honest advisory ───────────────────────────────────────────────


def test_c2_cargo_is_store_only_and_advisory_lists_remainder(
    sys_env: dict[str, Path], tmp_path: Path
) -> None:
    home = make_source_6x(tmp_path)
    (home / "env.d").mkdir()
    (home / "env.d" / "prod.env").write_text("K=v\n", encoding="utf-8")
    (home / "components.d").mkdir()
    (home / "venv").mkdir()
    plan = _plan_system(home)
    planned_names = {f.source.name for f in plan.planned_files}
    assert "env.d" not in planned_names
    assert "components.d" not in planned_names
    assert "venv" not in planned_names
    assert any("env.d" in line for line in plan.remaining_cargo)
    report = sm.run_migration(_plan_system(home, apply=True), progress=lambda _l: None)
    data_root_names = {p.name for p in sys_env["data_root"].iterdir()}
    assert "env.d" not in data_root_names
    assert "components.d" not in data_root_names
    assert "venv" not in data_root_names
    assert any("env.d" in line for line in report.remaining_cargo)


# ── C3: conffile discipline ──────────────────────────────────────────────────


def _apply_system(home: Path, sys_env: dict[str, Path]) -> sm.MigrationReport:
    lines: list[str] = []
    return sm.run_migration(_plan_system(home, apply=True), progress=lines.append)


def test_c3_nonempty_conffile_never_touched_incoming_lands_as_migrated(
    sys_env: dict[str, Path], tmp_path: Path
) -> None:
    sys_env["config"].write_text("api:\n  host: 127.0.0.1\n", encoding="utf-8")
    original = sys_env["config"].read_bytes()
    home = make_source_6x(tmp_path)
    report = _apply_system(home, sys_env)
    assert sys_env["config"].read_bytes() == original  # byte-identical
    migrated = list(sys_env["config_root"].glob("config.yaml.migrated-*"))
    assert len(migrated) == 1
    assert "vesma" in migrated[0].read_text(encoding="utf-8")
    assert report.config_disposition == "migrated-<ts>"


@pytest.mark.parametrize("payload", ["", "   \n", "\n"])
def test_c3_empty_or_absent_conffile_gets_the_write(
    sys_env: dict[str, Path], tmp_path: Path, payload: str
) -> None:
    sys_env["config"].write_text(payload, encoding="utf-8")
    home = make_source_6x(tmp_path)
    report = _apply_system(home, sys_env)
    assert report.config_disposition == "written"
    assert "data_dir" in sys_env["config"].read_text(encoding="utf-8")


# ── C4: ownership by FHS manifest roles ──────────────────────────────────────


def test_c4_published_modes_equal_manifest_roles(sys_env: dict[str, Path], tmp_path: Path) -> None:
    """Modes come FROM THE MANIFEST (never literals, never a blanket 0700
    floor) — dirs take the data-root role, files the store-db role."""
    manifest = fhs.load_fhs_manifest()
    dir_mode = manifest.entry("data_root").mode
    file_mode = manifest.entry("store_db").mode
    cfg_mode = manifest.entry("shared_config").mode
    home = make_source_6x(tmp_path)
    _apply_system(home, sys_env)
    db = sys_env["data_root"] / STORE_DB_NAME
    vault_dir = sys_env["data_root"] / "vault"
    assert stat_mod.S_IMODE(db.stat().st_mode) == file_mode
    assert stat_mod.S_IMODE(vault_dir.stat().st_mode) == dir_mode
    assert stat_mod.S_IMODE(vault_dir.rglob("a.md").__next__().stat().st_mode) == file_mode
    assert stat_mod.S_IMODE(sys_env["config"].stat().st_mode) == cfg_mode


def test_c4_expectation_roles_derived_from_manifest_roles(
    sys_env: dict[str, Path], tmp_path: Path
) -> None:
    home = make_source_6x(tmp_path)
    _plan_system(home)  # warms nothing; direct builder check below
    staging = tmp_path / "staging-sample"
    (staging / "data" / "sub").mkdir(parents=True)
    (staging / "data" / "vesma.db").write_text("db", encoding="utf-8")
    (staging / "data" / "sub" / "x.txt").write_text("x", encoding="utf-8")
    config_final = sys_env["config"]
    entries = sm._plan_publish_entries(
        staging, sys_env["data_root"], staging / "config.yaml", config_final
    )
    exps = sm._publish_ownership_expectations(entries, None)
    roles = {e.role for e in exps}
    assert roles == {"data_root", "store_db", "shared_config"}
    manifest = fhs.load_fhs_manifest()
    for exp in exps:
        expected_mode = {
            "data_root": manifest.entry("data_root").mode,
            "store_db": manifest.entry("store_db").mode,
            "shared_config": manifest.entry("shared_config").mode,
        }[exp.role]
        assert exp.mode == expected_mode


def test_c4_post_publish_audit_rejects_mode_drift(sys_env: dict[str, Path], tmp_path: Path) -> None:
    home = make_source_6x(tmp_path)
    _apply_system(home, sys_env)
    db = sys_env["data_root"] / STORE_DB_NAME
    db.chmod(0o644)  # drifted wide after the run
    manifest = fhs.load_fhs_manifest()
    exps = [
        sm._OwnershipExpectation(
            db,
            db,
            "store_db",
            manifest.entry("store_db").mode,
            manifest.entry("store_db").owner,
            manifest.entry("store_db").group,
        )
    ]
    problems = sm._stat_audit_problems(exps, ownership_applied=False)
    assert problems and "0644" in problems[0]


def test_c4_nonroot_run_records_intent_not_applied(
    sys_env: dict[str, Path], tmp_path: Path
) -> None:
    home = make_source_6x(tmp_path)
    report = _apply_system(home, sys_env)
    assert report.ownership_applied is False  # non-root simulation
    assert any("RECORDED as intent" in w for w in report.warnings)


# ── C5: piecewise publish + interruption finally-path ────────────────────────


def test_c5_publish_interruption_rolls_back_and_keeps_snapshot(
    sys_env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Ctrl-C pin: source untouched, staging removed, snapshot kept,
    the data root back to fresh (published entries rolled back)."""
    home = make_source_6x(tmp_path)
    source_before = _tree_digest(home)
    real_publish = sm._publish_entry
    calls = {"n": 0}

    def flaky(entry: sm._PublishEntry) -> None:
        calls["n"] += 1
        if calls["n"] == 2:  # interrupt after the FIRST successful move
            raise KeyboardInterrupt
        real_publish(entry)

    monkeypatch.setattr(sm, "_publish_entry", flaky)
    with pytest.raises(KeyboardInterrupt):
        sm.run_migration(_plan_system(home, apply=True), progress=lambda _l: None)
    assert _tree_digest(home) == source_before  # source untouched (C6 too)
    assert not list(sys_env["data_root"].parent.glob("vesma.staging-*"))  # staging gone
    snapshots = list(tmp_path.glob("migrate-snapshot-*"))
    assert len(snapshots) == 1  # the rollback artifact stays
    data_names = {p.name for p in sys_env["data_root"].iterdir()}
    assert STORE_DB_NAME not in data_names  # rolled back
    assert "vault" not in data_names


def test_c5_quiesce_double_run_plan_and_prepublish(
    sys_env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    real = sm.system_quiesce_problems

    def counting(*args: Any, **kwargs: Any) -> sm.SystemQuiesce:
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(sm, "system_quiesce_problems", counting)
    home = make_source_6x(tmp_path)
    sm.run_migration(_plan_system(home, apply=True), progress=lambda _l: None)
    assert calls["n"] >= 2  # plan phase + right before publish (C7)


def test_c5_midrun_store_content_refuses_publish(sys_env: dict[str, Path], tmp_path: Path) -> None:
    """A racing actor planting store content between plan and publish hits
    the pre-publish freshness guard, never an overwrite."""
    home = make_source_6x(tmp_path)
    plan = _plan_system(home, apply=True)
    (sys_env["data_root"] / STORE_DB_NAME).write_text("raced", encoding="utf-8")
    with pytest.raises(sm.TargetStoreContentError):
        sm.run_migration(plan, progress=lambda _l: None)
    assert (sys_env["data_root"] / STORE_DB_NAME).read_text(encoding="utf-8") == "raced"


# ── C6: the source is never written ──────────────────────────────────────────


def test_c6_source_tree_byte_and_stat_untouched(sys_env: dict[str, Path], tmp_path: Path) -> None:
    home = make_source_5x(tmp_path)
    before = _tree_digest(home)
    _apply_system(home, sys_env)
    assert _tree_digest(home) == before
    assert not (home / "data" / "mnemos.db-wal").exists()  # no checkpoint/sidecars


def test_c6_foreign_euid_probes_read_only(
    sys_env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = make_source_6x(tmp_path)
    owner_uid = (home / "data" / STORE_DB_NAME).stat().st_uid
    monkeypatch.setattr(sm.os, "geteuid", lambda: owner_uid + 1)
    plan = _plan_system(home, apply=True)
    assert plan.source_owner_uid == owner_uid
    before = _tree_digest(home)
    report = sm.run_migration(plan, progress=lambda _l: None)
    assert report.records == 1
    assert _tree_digest(home) == before  # read-only probes wrote nothing
    assert any("read-only" in w for w in report.warnings)


def test_c6_recovery_required_typed_refusal() -> None:
    """Classification pin: a read-only probe failing recovery-shaped is the
    owner's repair, typed (modern SQLite heap-memory recovery rarely
    surfaces it — the classifier is the defensive contract)."""
    err = sqlite3.OperationalError("attempt to write a readonly database")
    typed = sm._classify_source_ro_failure(Path("/x/db"), err)
    assert isinstance(typed, sm.SourceRecoveryRequiredError)
    assert typed.exit_code == 14
    other = sm._classify_source_ro_failure(Path("/x/db"), sqlite3.OperationalError("nope"))
    assert isinstance(other, sm.UsageError)


# ── C7: triple target quiesce gate ───────────────────────────────────────────


@pytest.mark.parametrize("state", ["active", "activating"])
def test_c7_unit_active_is_fatal(
    sys_env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    monkeypatch.setattr(sm, "_run_systemctl", lambda args: state if "is-active" in args else "")
    home = make_source_6x(tmp_path)
    result = runner.invoke(app, ["migrate-store", "--from", str(home), "--target", "system"])
    assert result.exit_code == 4, _output(result)
    assert "vesma.service" in _output(result)


def test_c7_enabled_unit_is_warning_not_fatal(
    sys_env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        sm,
        "_run_systemctl",
        lambda args: "inactive" if "is-active" in args else "enabled",
    )
    home = make_source_6x(tmp_path)
    result = runner.invoke(app, ["migrate-store", "--from", str(home), "--target", "system"])
    assert result.exit_code == 0, _output(result)
    assert "enabled" in _output(result)


def test_c7_control_socket_is_warning_not_fatal(sys_env: dict[str, Path], tmp_path: Path) -> None:
    sock_dir = sys_env["prefix"] / "run" / "vesma"
    sock_dir.mkdir(parents=True)
    (sock_dir / "control.sock").write_text("", encoding="utf-8")
    home = make_source_6x(tmp_path)
    result = runner.invoke(app, ["migrate-store", "--from", str(home), "--target", "system"])
    assert result.exit_code == 0, _output(result)
    assert "control.sock" in _output(result)
    # the NEW constant resolves through the manifest under the prefix
    assert sm._system_control_socket(None) == sock_dir / "control.sock"


def test_c7_locked_sqlite_under_data_root_is_fatal(
    sys_env: dict[str, Path], tmp_path: Path
) -> None:
    locked = sys_env["data_root"] / "board" / "board.db"
    locked.parent.mkdir()
    conn = sqlite3.connect(str(locked))
    conn.execute("CREATE TABLE t (x)")
    conn.execute("BEGIN IMMEDIATE")  # a live writer holds the write lock
    try:
        home = make_source_6x(tmp_path)
        result = runner.invoke(app, ["migrate-store", "--from", str(home), "--target", "system"])
        assert result.exit_code == 4, _output(result)
    finally:
        conn.rollback()
        conn.close()


# ── C8: verification strengthenings ──────────────────────────────────────────


def test_c8_sha256_parity_detects_tampered_plain_file(
    sys_env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = make_source_6x(tmp_path)
    real_copy = sm.shutil.copy2

    def corrupt_copy(src: Any, dst: Any, **kwargs: Any) -> None:
        real_copy(src, dst, **kwargs)
        # corrupt the STAGING copy only — the snapshot must stay honest,
        # otherwise both sides tamper identically and parity would hold
        if Path(dst).name == "readme.txt" and ".staging-" in str(dst):
            Path(dst).write_text("tampered", encoding="utf-8")

    monkeypatch.setattr(sm.shutil, "copy2", corrupt_copy)
    with pytest.raises(sm.VerificationError) as excinfo:
        _apply_system(home, sys_env)
    assert "sha256 mismatch" in str(excinfo.value)


def test_c8_sibling_table_parity_detects_count_drift(
    sys_env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = make_source_5x(tmp_path)
    real_copy_sqlite = sm._copy_sqlite

    def corrupting_copy(src: Path, dst: Path, **kwargs: Any) -> None:
        real_copy_sqlite(src, dst, **kwargs)
        # corrupt the STAGING copy only (see the sha256-parity test)
        if Path(dst).name == "vectors.db" and ".staging-" in str(dst):
            conn = sqlite3.connect(str(dst))
            try:
                conn.execute("INSERT INTO vec VALUES ('ghost')")
                conn.commit()
            finally:
                conn.close()

    monkeypatch.setattr(sm, "_copy_sqlite", corrupting_copy)
    with pytest.raises(sm.VerificationError) as excinfo:
        _apply_system(home, sys_env)
    assert "table vec" in str(excinfo.value)


def test_c8_sample_size_grows_on_large_stores() -> None:
    assert sm._sample_size_for(999_999) == 10
    assert sm._sample_size_for(1_000_001) == 100


def test_c8_verification_dict_carries_system_legs(sys_env: dict[str, Path], tmp_path: Path) -> None:
    home = make_source_6x(tmp_path)
    report = _apply_system(home, sys_env)
    assert report.verification is not None
    assert report.verification["sha256_parity"] == "ok"
    assert report.verification["sibling_table_parity"] == "ok"
    assert report.verification["stat_audit"] == "ok"


# ── C9: snapshot placement ───────────────────────────────────────────────────


def test_c9_snapshot_defaults_next_to_source_and_mode_0700(
    sys_env: dict[str, Path], tmp_path: Path
) -> None:
    home = make_source_6x(tmp_path)
    _apply_system(home, sys_env)
    snapshots = list(tmp_path.glob("migrate-snapshot-*"))
    assert len(snapshots) == 1
    st = snapshots[0].stat()
    assert stat_mod.S_IMODE(st.st_mode) == 0o700
    assert st.st_uid == os.getuid()  # operator-owned (non-root run: self)


def test_c9_snapshot_placement_refusals(sys_env: dict[str, Path], tmp_path: Path) -> None:
    home = make_source_6x(tmp_path)
    for bad in (
        home / "inside-source",
        sys_env["data_root"] / "inside-target",
        sys_env["data_root"].parent / "inside-varlib",
        sys_env["config_root"] / "inside-etc",
    ):
        result = runner.invoke(
            app,
            [
                "migrate-store",
                "--from",
                str(home),
                "--target",
                "system",
                "--snapshot-dir",
                str(bad),
            ],
        )
        assert result.exit_code == 2, f"{bad}: {_output(result)}"


def test_c9_explicit_snapshot_dir_honored(sys_env: dict[str, Path], tmp_path: Path) -> None:
    home = make_source_6x(tmp_path)
    explicit = tmp_path / "snaps"
    report = sm.run_migration(
        _plan_system(home, apply=True, snapshot_dir=explicit), progress=lambda _l: None
    )
    assert report.snapshot_dir == explicit
    assert explicit.is_dir()


# ── C10: keys and secrets ────────────────────────────────────────────────────


def test_c10_totp_precondition_printed_in_dry_run(sys_env: dict[str, Path], tmp_path: Path) -> None:
    home = make_source_6x(tmp_path)
    (home / "config.yaml").write_text("api:\n  totp_enabled: true\n", encoding="utf-8")
    result = runner.invoke(app, ["migrate-store", "--from", str(home), "--target", "system"])
    assert result.exit_code == 0, _output(result)
    text = _output(result)
    assert "VESMARO_API__TOTP_MASTER_KEY" in text
    assert "env.d" in text or str(fhs.system_env_dir(None)) in text


def test_c10_out_of_home_secret_path_warned_by_key_name_only(
    sys_env: dict[str, Path], tmp_path: Path
) -> None:
    home = make_source_6x(tmp_path)
    outside = tmp_path / "outside" / "signing.key"
    (home / "config.yaml").write_text(
        f"federation:\n  agent_token_key_path: {outside}\n", encoding="utf-8"
    )
    result = runner.invoke(app, ["migrate-store", "--from", str(home), "--target", "system"])
    assert result.exit_code == 0, _output(result)
    text = _output(result)
    assert "agent_token_key_path" in text
    assert "NOT carried" in text
    assert str(outside) not in text  # the VALUE never prints (privacy)


def test_c10_signing_key_travels_with_data_siblings(
    sys_env: dict[str, Path], tmp_path: Path
) -> None:
    home = make_source_5x(tmp_path)
    _apply_system(home, sys_env)
    assert (sys_env["data_root"] / "agent-token-signing.key").is_file()


# ── C11: inherited equality gates ────────────────────────────────────────────


def test_c11_trust_marker_byte_stable_and_counts_equal(
    sys_env: dict[str, Path], tmp_path: Path
) -> None:
    home = make_source_5x(tmp_path)
    _apply_system(home, sys_env)
    conn = _target_db(sys_env["data_root"])
    try:
        row = conn.execute("SELECT tags, project FROM memories WHERE id='id-1'").fetchone()
        tags = json.loads(str(row["tags"]))
        assert "mnemos:no-federate" in tags  # byte-stable (never re-slaged)
        assert "vesma:learning" in tags  # re-slaged
        assert str(row["project"]) == "vesma"
        assert conn.execute("SELECT count(*) FROM memories").fetchone()[0] == 1
    finally:
        conn.close()


# ── C12: the mover never writes the install-state ────────────────────────────


def test_c12_install_state_bytes_untouched(sys_env: dict[str, Path], tmp_path: Path) -> None:
    home = make_source_6x(tmp_path)
    state_path = sys_env["data_root"] / "install-state.yaml"
    before = state_path.read_bytes()
    _apply_system(home, sys_env)
    assert state_path.read_bytes() == before
    # no installer artifacts were invented: the state still records exactly
    # the two fixture rows
    doc = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    assert len(doc["artifacts"]) == 2


def test_c12_uninstall_never_silently_deletes_a_migrated_store(
    sys_env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The C12 safety property: whatever `uninstall --system` does after a
    migration (the data root is recorded preserve-by-design), the migrated
    store survives — the mover wrote NO store rows into the state."""
    monkeypatch.setattr(system_install_mod, "_systemctl_system", lambda args: "ok")
    home = make_source_6x(tmp_path)
    _apply_system(home, sys_env)
    db = sys_env["data_root"] / STORE_DB_NAME
    with contextlib.suppress(system_install.SystemInstallError):
        # a loud refusal is also acceptable — the store must survive
        system_install.uninstall_system(sys_env["prefix"])
    assert db.is_file()
    assert (sys_env["data_root"] / "vault" / "a.md").is_file()


# ── C13: activation gate ─────────────────────────────────────────────────────


def test_c13_gate_closed_refusal_stays_typed(
    sys_env: dict[str, Path], tmp_path: Path, gate_closed: None
) -> None:
    """Belt-and-braces pin: with the gate CLOSED (monkeypatched — the
    product surface opened by the 2026-10-23 ratification), a system-target
    apply still refuses with the typed exit 15 and writes nothing. The
    refusal machinery must not rot."""
    home = make_source_6x(tmp_path)
    result = runner.invoke(
        app, ["migrate-store", "--from", str(home), "--target", "system", "--apply"]
    )
    assert result.exit_code == 15, _output(result)
    text = _output(result)
    assert "soak" in text
    assert "ADR-0045" in text
    # and nothing was written
    assert not list(tmp_path.glob("migrate-snapshot-*"))
    assert not list(sys_env["data_root"].parent.glob("vesma.staging-*"))


def test_c13_gate_open_machinery_works_end_to_end(sys_env: dict[str, Path], tmp_path: Path) -> None:
    home = make_source_6x(tmp_path)
    report = _apply_system(home, sys_env)
    assert (sys_env["data_root"] / STORE_DB_NAME).is_file()
    assert report.verification is not None


def test_c13_dry_run_works_ungated_with_forecast(sys_env: dict[str, Path], tmp_path: Path) -> None:
    home = make_source_6x(tmp_path)
    result = runner.invoke(app, ["migrate-store", "--from", str(home), "--target", "system"])
    assert result.exit_code == 0, _output(result)
    footer = " ".join(_output(result).split())  # the console wraps lines
    assert "dry-run: nothing was written" in footer
    assert "--apply for the system target is active since 6.5.0" in footer
    forecast = system_target_forecast()
    assert [f.check_id for f in forecast] == ["DR-14", "DR-15", "DR-17"]


def test_c13_forecast_uses_the_doctor_functions_one_implementation(
    sys_env: dict[str, Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vesma.service import doctor_checks

    sentinel = Finding("DR-XX", "sentinel", Severity.WARN, "sentinel detail", None)

    def fake_dr14() -> Finding:
        return sentinel

    monkeypatch.setattr(doctor_checks, "_dr14", fake_dr14)
    forecast = system_target_forecast()
    assert any(f.check_id == "DR-XX" for f in forecast)
    home = make_source_6x(tmp_path)
    result = runner.invoke(app, ["migrate-store", "--from", str(home), "--target", "system"])
    assert "forecast: DR-XX" in _output(result)  # rendered marked as forecast


# ── C14: fast path + progress + preflight ────────────────────────────────────


def test_c14_six_zero_source_takes_fast_path(sys_env: dict[str, Path], tmp_path: Path) -> None:
    home = make_source_6x(tmp_path)
    plan = _plan_system(home)
    assert plan.fast_path is True
    report = _apply_system(home, sys_env)
    assert report.fast_path is True
    assert report.tag_subtypes_rewritten == 0
    assert report.records == 1


def test_c14_legacy_source_full_sweep_not_fast(sys_env: dict[str, Path], tmp_path: Path) -> None:
    home = make_source_5x(tmp_path)
    plan = _plan_system(home)
    assert plan.fast_path is False
    report = _apply_system(home, sys_env)
    assert report.fast_path is False
    assert report.tag_subtypes_rewritten >= 1


def test_c14_already_migrated_source_allowed_for_system_target(
    sys_env: dict[str, Path], tmp_path: Path
) -> None:
    """The B3 exit-7 refusal must NOT fire for the system target — the 6.0
    store is its EXPECTED source (relocation, not namespace conversion)."""
    home = make_source_6x(tmp_path)
    assert (home / "data" / STORE_DB_NAME).is_file()  # positive evidence
    result = runner.invoke(app, ["migrate-store", "--from", str(home), "--target", "system"])
    assert result.exit_code == 0, _output(result)


def test_c14_no_fetchall_anywhere_in_mover_source() -> None:
    source = Path(sm.__file__).read_text(encoding="utf-8")
    assert "fetchall" not in source  # C14: bulk reads fetch in batches


def test_c14_free_space_refusal_carries_numbers(
    sys_env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = make_source_6x(tmp_path)

    class FakeUsage:
        total = 10**12
        used = 0
        free = 1  # one byte free

    monkeypatch.setattr(sm.shutil, "disk_usage", lambda path: FakeUsage())
    result = runner.invoke(app, ["migrate-store", "--from", str(home), "--target", "system"])
    assert result.exit_code == 16, _output(result)
    text = _output(result)
    assert "bytes free" in text and "bytes needed" in text


def test_c14_wal_size_advisory_in_plan(sys_env: dict[str, Path], tmp_path: Path) -> None:
    home = make_source_6x(tmp_path)
    # fabricate a non-empty WAL sidecar beside the source db
    (home / "data" / "vesma.db-wal").write_bytes(b"\x00" * 128)
    plan = _plan_system(home)
    assert plan.wal_bytes == 128


def test_c14_sidecar_pin_blocks_publish(sys_env: dict[str, Path], tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "data").mkdir(parents=True)
    (tree / "data" / "vesma.db-wal").write_text("", encoding="utf-8")
    with pytest.raises(sm.VerificationError):
        sm._assert_no_sidecars(tree)


def test_c14_progress_banners_stream_no_record_content(
    sys_env: dict[str, Path], tmp_path: Path
) -> None:
    home = make_source_5x(tmp_path)
    lines: list[str] = []
    sm.run_migration(_plan_system(home, apply=True), progress=lines.append)
    assert any("1/7" in line for line in lines)
    assert any("7/7" in line for line in lines)
    joined = "\n".join(lines)
    assert "content-one" not in joined  # privacy: numbers and paths only


# ── C15: exit-code taxonomy ──────────────────────────────────────────────────


def test_c15_error_taxonomy_unique_and_additive() -> None:
    seen: dict[int, str] = {}
    for name in dir(sm):
        obj = getattr(sm, name)
        if (
            isinstance(obj, type)
            and issubclass(obj, sm.StoreMigrationError)
            and obj is not sm.StoreMigrationError
        ):
            assert obj.exit_code not in seen, f"exit code clash: {name} vs {seen[obj.exit_code]}"
            seen[obj.exit_code] = name
    new_codes = [
        code
        for code, name in seen.items()
        if getattr(sm, name).__module__ == sm.__name__ and code >= 10
    ]
    assert {10, 11, 12, 13, 14, 15, 16} <= set(new_codes)


# ── Round-trip equivalence (user → system → fresh user) ─────────────────────


def test_round_trip_user_to_system_to_fresh_user_equivalence(
    sys_env: dict[str, Path], tmp_path: Path
) -> None:
    """A 5.x store migrates to a 6.0 user home, relocates into the system
    data root, and re-lands on a fresh user home — logical equivalence
    (counts, id digest, per-table counts, vault sha256) at every hop."""
    original = make_source_5x(tmp_path)

    # hop 1: 5.x → 6.0 user home (the classic mover)
    user6 = tmp_path / "user6"
    result = runner.invoke(
        app, ["migrate-store", "--from", str(original), "--to", str(user6), "--apply"]
    )
    assert result.exit_code == 0, _output(result)

    def stats_of(home: Path) -> tuple[int, str, dict[str, int]]:
        data_dir = home / "data"
        db = data_dir / STORE_DB_NAME
        assert db.is_file(), home
        s = sm.read_store_stats(db)
        return (s.total, s.ids_digest, dict(s.project_breakdown))

    base = stats_of(user6)

    # hop 2: user6 → system data root (fast path — zero counters; an
    # explicit snapshot dir keeps the hop-1 artifact collision away)
    system_report = sm.run_migration(
        _plan_system(user6, apply=True, snapshot_dir=tmp_path / "hop2-snapshot"),
        progress=lambda _l: None,
    )
    system_stats = sm.read_store_stats(sys_env["data_root"] / STORE_DB_NAME)
    assert (system_stats.total, system_stats.ids_digest) == base[:2]
    assert system_report.fast_path is True

    # vault parity user6 ↔ system (per-file sha256)
    def vault_shas(home: Path) -> dict[str, str]:
        return {
            str(p.relative_to(home / "vault")): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((home / "vault").rglob("*"))
            if p.is_file()
        }

    assert vault_shas(user6) == vault_shas(sys_env["data_root"])

    # hop 3: system → fresh user home (internal seam; the CLI keeps refusing
    # migrated sources for the user target)
    fresh = tmp_path / "fresh-user"
    plan3 = sm.build_plan(
        sys_env["data_root"],
        fresh,
        apply=True,
        allow_migrated_source=True,
        snapshot_dir=tmp_path / "hop3-snapshot",
    )
    assert plan3.target == "user"
    sm.run_migration(plan3)
    assert stats_of(fresh) == base
    assert vault_shas(fresh) == vault_shas(user6)


# ── Wave-5 P3 tails (security spot-check follow-ups) ─────────────────────────


def test_p3_apply_phase_pre_stats_reread_classified_exit14(
    sys_env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3-1: the apply-phase source re-read goes through the SAME typed
    classifier as the plan phase — a source that turned recovery-needing
    between plan and apply is exit 14, never a raw traceback."""
    home = make_source_6x(tmp_path)
    plan = _plan_system(home, apply=True)
    source_db = home / "data" / STORE_DB_NAME
    real = sm.read_store_stats

    def failing(db_path: Path, *, read_only: bool = False) -> Any:
        if read_only and db_path == source_db:
            raise sqlite3.OperationalError("unable to open database file")
        return real(db_path, read_only=read_only)

    monkeypatch.setattr(sm, "read_store_stats", failing)
    with pytest.raises(sm.SourceRecoveryRequiredError) as ei:
        sm.run_migration(plan, progress=lambda _l: None)
    assert ei.value.exit_code == 14


def test_p3_apply_phase_live_stats_reread_classified_exit14(
    sys_env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3-1 (snapshot leg): the liveness comparison re-read of the SOURCE
    inside make_snapshot is classified too — the first read-only pass
    (pre_stats) succeeds, the snapshot-time re-read fails recovery-shaped."""
    home = make_source_6x(tmp_path)
    plan = _plan_system(home, apply=True)
    source_db = home / "data" / STORE_DB_NAME
    real = sm.read_store_stats
    seen = {"n": 0}

    def failing_second(db_path: Path, *, read_only: bool = False) -> Any:
        if read_only and db_path == source_db:
            seen["n"] += 1
            if seen["n"] == 2:  # pre_stats passed; the snapshot re-read fails
                raise sqlite3.OperationalError("attempt to write a readonly database")
        return real(db_path, read_only=read_only)

    monkeypatch.setattr(sm, "read_store_stats", failing_second)
    with pytest.raises(sm.SourceRecoveryRequiredError) as ei:
        sm.run_migration(plan, progress=lambda _l: None)
    assert ei.value.exit_code == 14
    assert seen["n"] == 2


def _race_at_publish_gate(
    monkeypatch: pytest.MonkeyPatch, sys_env: dict[str, Path], payload: bytes
) -> None:
    """Lands a conffile mutation exactly between the phase-5 disposition
    and the publish loop — the phase-6 gate call is the seam."""
    real_gate = sm.system_quiesce_problems

    def racing_gate(data_root: Path, *, with_wal_sampling: bool) -> Any:
        if with_wal_sampling:  # the pre-publish (phase 6) call only
            sys_env["config"].write_bytes(payload)
        return real_gate(data_root, with_wal_sampling=with_wal_sampling)

    monkeypatch.setattr(sm, "system_quiesce_problems", racing_gate)


def test_p3_conffile_toctou_emptied_to_nonempty_keeps_contents(
    sys_env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3-2: empty at the phase-5 disposition, NON-EMPTY by publish — the
    fresh stat re-decides through the phase-5 decision path: the racing
    conffile is never overwritten, the incoming config lands beside it."""
    sys_env["config"].write_text("", encoding="utf-8")
    home = make_source_6x(tmp_path)
    plan = _plan_system(home, apply=True)
    _race_at_publish_gate(monkeypatch, sys_env, b"raced: true\n")
    report = sm.run_migration(plan, progress=lambda _l: None)
    assert sys_env["config"].read_bytes() == b"raced: true\n"
    migrated = list(sys_env["config_root"].glob("config.yaml.migrated-*"))
    assert len(migrated) == 1
    assert "vesma" in migrated[0].read_text(encoding="utf-8")
    assert report.config_disposition == "migrated-<ts>"


def test_p3_conffile_toctou_nonempty_to_empty_gets_the_write(
    sys_env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3-2 (vice versa): non-empty at the disposition, emptied by publish —
    the fresh stat gives the EMPTY conffile the write (C3), no sidecar."""
    sys_env["config"].write_text("stale: full\n", encoding="utf-8")
    home = make_source_6x(tmp_path)
    plan = _plan_system(home, apply=True)
    _race_at_publish_gate(monkeypatch, sys_env, b"")
    report = sm.run_migration(plan, progress=lambda _l: None)
    assert "data_dir" in sys_env["config"].read_text(encoding="utf-8")
    assert not list(sys_env["config_root"].glob("config.yaml.migrated-*"))
    assert report.config_disposition == "written"


def test_p3_conffile_toctou_unreadable_at_publish_refuses_typed_and_rolls_back(
    sys_env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3-2 (typed-refusal leg): a conffile that turned unreadable by
    publish hits the SAME ConfigMigrationError as the phase-5 decision —
    and the already-published entries roll back (no silent fallback)."""
    sys_env["config"].write_text("", encoding="utf-8")
    home = make_source_6x(tmp_path)
    plan = _plan_system(home, apply=True)
    _race_at_publish_gate(monkeypatch, sys_env, b"\xff\xfe not utf-8")
    with pytest.raises(sm.ConfigMigrationError) as ei:
        sm.run_migration(plan, progress=lambda _l: None)
    assert ei.value.exit_code == 9
    assert sys_env["config"].read_bytes() == b"\xff\xfe not utf-8"
    assert not (sys_env["data_root"] / STORE_DB_NAME).exists()  # rolled back


def test_p3_phase7_audit_refusal_says_published_no_retry(
    sys_env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3-3: the post-publish audit refusal must not read as retryable —
    the store IS published; a re-run is refused with exit 11."""
    home = make_source_6x(tmp_path)
    plan = _plan_system(home, apply=True)
    monkeypatch.setattr(
        sm, "_stat_audit_problems", lambda exps, *, ownership_applied: ["fake drift row"]
    )
    with pytest.raises(sm.VerificationError) as ei:
        sm.run_migration(plan, progress=lambda _l: None)
    assert ei.value.exit_code == 6
    message = str(ei.value)
    assert "IS PUBLISHED" in message
    assert "exit 11" in message
    assert (sys_env["data_root"] / STORE_DB_NAME).is_file()  # the message is honest


def test_p3_json_dry_run_payload_carries_forecast(sys_env: dict[str, Path], tmp_path: Path) -> None:
    """P3-4: the --json dry-run payload carries the C13 DR forecast (the
    same doctor findings the text render shows); the classic --to payload
    does not."""
    home = make_source_6x(tmp_path)
    result = runner.invoke(
        app, ["migrate-store", "--from", str(home), "--target", "system", "--json"]
    )
    assert result.exit_code == 0, _output(result)
    payload = json.loads(result.output)
    assert [f["check_id"] for f in payload["forecast"]] == ["DR-14", "DR-15", "DR-17"]
    for finding in payload["forecast"]:
        assert set(finding) == {"check_id", "title", "severity", "detail", "fix_command"}
        assert finding["severity"] in {"OK", "WARN", "FAIL"}
    classic_home = make_source_5x(tmp_path)
    result = runner.invoke(
        app,
        ["migrate-store", "--from", str(classic_home), "--to", str(tmp_path / "classic"), "--json"],
    )
    assert result.exit_code == 0, _output(result)
    assert "forecast" not in json.loads(result.output)


# ── Wave-5 soak pins (round-trip soak 2026-10-22): parity x WAL sidecars
#    (F6) and cross-fs publish ownership (F7) ─────────────────────────────


def test_c8_sha_parity_ignores_sidecars_materialized_by_readonly_opens(
    tmp_path: Path,
) -> None:
    """F6 soak pin: the staging build reads the SNAPSHOT's databases
    through read-only URIs (`_copy_sqlite` source side); a WAL-mode
    database materializes transient ``-wal``/``-shm`` sidecars on ANY
    open, and a read-only connection never removes them on close. The
    C8 parity set must exclude sqlite read machinery on BOTH sides — a
    real engine store (every db journal_mode=wal) refused with exit 6
    before the fix. The fixture db is REAL WAL mode: the old delete-mode
    fixtures are exactly why the hole passed the suite."""
    snap = tmp_path / "snapshot" / "data"
    stag = tmp_path / "staging" / "data"
    snap.mkdir(parents=True)
    stag.mkdir(parents=True)
    db = snap / "vectors.db"
    conn = sqlite3.connect(str(db))
    try:
        assert conn.execute("PRAGMA journal_mode=wal").fetchone()[0] == "wal"
        conn.execute("CREATE TABLE vec (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO vec VALUES ('id-1')")
        conn.commit()
    finally:
        conn.close()
    # WAL mode lives in the header (write/read format version bytes); the
    # backup-API copy keeps it — that is what makes sidecars reappear.
    assert db.read_bytes()[18:20] == b"\x02\x02"
    # a clean writer close checkpointed the sidecars away…
    assert not (snap / "vectors.db-wal").exists()
    # …and the mover's read-only reopen materializes them again — the
    # exact shape of the live failure.
    ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        ro.execute("SELECT count(*) FROM vec").fetchone()
    finally:
        ro.close()
    assert (snap / "vectors.db-wal").exists() or (snap / "vectors.db-shm").exists()
    # the staging copy folds the WAL in through the backup API: content
    # intact, and no sidecar ever lands on the staging side
    sm._copy_sqlite(db, stag / "vectors.db")
    assert sm._table_counts(stag / "vectors.db") == {"vec": 1}
    assert not (stag / "vectors.db-wal").exists()
    assert not (stag / "vectors.db-shm").exists()
    assert sm._sha256_parity_problems(snap, stag) == []


def test_c8_sha_parity_still_validates_plain_files_and_excludes_sidecars_both_sides(
    tmp_path: Path,
) -> None:
    """F6 inverse pin: sidecars are excluded from BOTH sides (even a
    one-sided or content-mismatched one is never reported — they are
    sqlite read machinery, never cargo), while foreign plain files keep
    the full validation: content mismatches and one-sided presence are
    still problems, with the directional messages intact."""
    snap = tmp_path / "s"
    stag = tmp_path / "t"
    snap.mkdir()
    stag.mkdir()
    (snap / "readme.txt").write_text("ok", encoding="utf-8")
    (stag / "readme.txt").write_text("ok", encoding="utf-8")
    # sidecars: both sides, mismatched content, one-sided presence
    (snap / "vectors.db-wal").write_bytes(b"\x00")
    (stag / "vectors.db-wal").write_bytes(b"\x01")
    (stag / "vectors.db-shm").write_bytes(b"\x00")
    assert sm._sha256_parity_problems(snap, stag) == []
    # a foreign plain file is still validated: tamper → mismatch
    (stag / "readme.txt").write_text("tampered", encoding="utf-8")
    problems = sm._sha256_parity_problems(snap, stag)
    assert problems == ["readme.txt: sha256 mismatch between snapshot and staging"]
    # missing on one side keeps the directional messages
    (stag / "readme.txt").unlink()
    assert sm._sha256_parity_problems(snap, stag) == [
        "readme.txt: in the snapshot but missing in staging"
    ]
    (stag / "readme.txt").write_text("ok", encoding="utf-8")
    (snap / "readme.txt").unlink()
    assert sm._sha256_parity_problems(snap, stag) == [
        "readme.txt: in staging but not in the snapshot"
    ]


def test_c8_wal_mode_sibling_db_does_not_break_the_apply(
    sys_env: dict[str, Path], tmp_path: Path
) -> None:
    """The live soak failure end-to-end: a REAL engine store keeps its
    databases in journal_mode=wal; before the fix this apply died with
    exit 6 (sidecars in the snapshot flagged by the sha256 parity). The
    pin builds such a sibling and walks the FULL system-target apply."""
    home = make_source_6x(tmp_path)
    conn = sqlite3.connect(str(home / "data" / "vectors.db"))
    try:
        assert conn.execute("PRAGMA journal_mode=wal").fetchone()[0] == "wal"
        conn.execute("CREATE TABLE vec (id TEXT PRIMARY KEY)")
        conn.execute("INSERT INTO vec VALUES ('id-1')")
        conn.commit()
    finally:
        conn.close()
    report = _apply_system(home, sys_env)
    assert report.verification["sha256_parity"] == "ok"
    assert report.verification["sibling_table_parity"] == "ok"
    assert sm._table_counts(sys_env["data_root"] / "vectors.db") == {"vec": 1}


def test_c4_cross_fs_publish_fchowns_tmp_from_staged_when_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F7 soak pin: the cross-fs publish must carry the ownership pass's
    uid/gid onto the tmp file BEFORE fsync/replace — shutil.copy2 copies
    the mode but never the owner/group, so a root run published the
    config root:root and failed the C4 stat audit. Non-root CI cannot
    fchown for real, so the pin mocks euid→0 and captures the call."""
    staged_dir = tmp_path / "staging"
    staged_dir.mkdir()
    staged = staged_dir / "config.yaml"
    staged.write_text("vesma:\n  data_dir: /var/lib/vesma\n", encoding="utf-8")
    staged.chmod(0o640)
    final_dir = tmp_path / "etc"
    final_dir.mkdir()
    entry = sm._PublishEntry(staged, final_dir / "config.yaml", cross_fs=True)

    events: list[tuple[str, int, int] | tuple[str]] = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: events.append(("fchown", uid, gid)))
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        events.append(("fsync",))
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)

    sm._publish_entry(entry)
    staged_stat = staged.stat()
    # ownership rides onto the tmp fd BEFORE the durability barrier (the
    # trailing fsync is _fsync_dir's post-replace directory barrier)
    assert events[0] == ("fchown", staged_stat.st_uid, staged_stat.st_gid)
    assert events[1] == ("fsync",)
    final = final_dir / "config.yaml"
    assert final.read_text(encoding="utf-8") == "vesma:\n  data_dir: /var/lib/vesma\n"
    assert stat_mod.S_IMODE(final.stat().st_mode) == 0o640  # copy2 carries the mode
    assert not (final_dir / ".config.yaml.publish-tmp").exists()  # atomic replace


def test_c4_cross_fs_publish_skips_fchown_for_non_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F7 inverse pin: a non-root run keeps the recorded-intent contract —
    no fchown is attempted, the publish itself is unchanged (the C4 audit
    checks modes only there)."""
    staged_dir = tmp_path / "staging"
    staged_dir.mkdir()
    staged = staged_dir / "config.yaml"
    staged.write_text("api:\n  host: 127.0.0.1\n", encoding="utf-8")
    staged.chmod(0o640)
    final_dir = tmp_path / "etc"
    final_dir.mkdir()
    entry = sm._PublishEntry(staged, final_dir / "config.yaml", cross_fs=True)

    fchown_calls: list[Any] = []
    monkeypatch.setattr(os, "geteuid", lambda: 2345)
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: fchown_calls.append((fd, uid, gid)))

    sm._publish_entry(entry)
    assert fchown_calls == []
    final = final_dir / "config.yaml"
    assert final.is_file()
    assert stat_mod.S_IMODE(final.stat().st_mode) == 0o640


# ── F9: uninstall adopts the migrated conffile (installer wave 2026-10-23) ────


def test_f9_round_trip_install_migrate_uninstall_adopt(
    sys_env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F9 pin 1 — the happy round trip: install → migrate apply →
    uninstall exits clean; the adopted conffile is unlinked, the config
    root removed, the data plane + snapshot stay intact, the report
    carries the adopt line, and nothing else moved (store db, vault and
    marker keep their bytes)."""
    monkeypatch.setattr(system_install_mod, "_systemctl_system", lambda args: "ok")
    home = make_source_6x(tmp_path)
    report = _apply_system(home, sys_env)
    config = sys_env["config"]
    marker = sys_env["data_root"] / sm.MIGRATION_MARKER_NAME
    db = sys_env["data_root"] / STORE_DB_NAME
    vault_note = sys_env["data_root"] / "vault" / "a.md"
    db_sha = hashlib.sha256(db.read_bytes()).hexdigest()
    marker_sha = hashlib.sha256(marker.read_bytes()).hexdigest()
    vault_sha = hashlib.sha256(vault_note.read_bytes()).hexdigest()
    # The REAL mover shape: the marker carries the conffile record
    # (canonical path + sha as published) and the published config
    # carries the provenance header.
    marker_doc = json.loads(marker.read_text(encoding="utf-8"))
    assert marker_doc["conffile"]["path"] == str(config)
    assert marker_doc["conffile"]["sha256"] == hashlib.sha256(config.read_bytes()).hexdigest()
    assert marker_doc["source_home"] == str(home)
    assert config.read_text(encoding="utf-8").startswith(sm.MIGRATE_CONFFILE_HEADER)
    snapshot = Path(report.snapshot_dir)
    assert snapshot.is_dir()

    result = system_install.uninstall_system(sys_env["prefix"])
    adopt_lines = [line for line in result.lines if line.startswith("adopted-from-migrate-store")]
    assert len(adopt_lines) == 1
    assert str(config) in adopt_lines[0]
    assert str(snapshot) in adopt_lines[0]  # recoverability pointer
    assert not config.exists()
    assert not sys_env["config_root"].exists()
    # The data plane survives untouched: store, vault, marker.
    assert hashlib.sha256(db.read_bytes()).hexdigest() == db_sha
    assert hashlib.sha256(marker.read_bytes()).hexdigest() == marker_sha
    assert hashlib.sha256(vault_note.read_bytes()).hexdigest() == vault_sha


def test_f9_narrowness_preexisting_conffile_refuses_uninstall(
    sys_env: dict[str, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F9 pin 2 — narrowness: a pre-existing NON-EMPTY conffile makes the
    mover land the incoming config as a .migrated-<ts> sidecar; the
    uninstall then REFUSES — the operator file AND the sidecar are
    foreign (the marker's canonical-path sha cannot match operator
    bytes)."""
    monkeypatch.setattr(system_install_mod, "_systemctl_system", lambda args: "ok")
    sys_env["config"].write_text("api:\n  host: 127.0.0.1\n", encoding="utf-8")
    operator_bytes = sys_env["config"].read_bytes()
    home = make_source_6x(tmp_path)
    _apply_system(home, sys_env)
    assert sys_env["config"].read_bytes() == operator_bytes  # never touched
    sidecars = list(sys_env["config_root"].glob("config.yaml.migrated-*"))
    assert len(sidecars) == 1

    with pytest.raises(system_install.SystemInstallError, match="foreign content"):
        system_install.uninstall_system(sys_env["prefix"])
    # Nothing was removed: the operator file and the sidecar survive.
    assert sys_env["config"].read_bytes() == operator_bytes
    assert sidecars[0].is_file()


def test_f9_source_scan_no_install_state_write_in_mover() -> None:
    """F9 pin 6 — mirror of the C1 zero-literals pin: the mover NEVER
    writes the install-state (C12 verbatim; the adopt row lives in the
    uninstaller's memory for one run, and the marker is the only
    evidence the mover publishes)."""
    source = Path(sm.__file__).read_text(encoding="utf-8")
    assert "_write_state" not in source
    assert "install_state.yaml" not in source
