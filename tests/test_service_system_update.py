"""System-profile updater tests (ADR-0045 rollout wave 3).

The same dest-dir simulation as the installer tests (no root):
:func:`vesma.service.fhs.default_prefix` monkeypatched to a temp prefix,
every subprocess seam stubbed. Covers the wave's conformance items:

- SY-05 — conffile discipline: an operator edit is NEVER overwritten
  (keep-user), the new default lands next to it as ``<name>.yaml.rpmnew``,
  a pending .rpmnew is never clobbered, ``config.yaml`` is never seeded;
- SY-06 — journald stays primary: the update never creates the opt-in
  ``/var/log/vesma`` tree and never touches an operator-created one;
- SY-07 — the update mechanics: new revision nearby, atomic symlink
  swap, health gate BEFORE retirement, rollback on gate failure, no
  pip-as-root (uv seams only), foreign venvs never written;
- SY-11 — mode-aware preconditions: refuses without an installation,
  refuses on DR-17 drift before ANY action, idempotent re-runs.

Mapping note: the layout v2 spec (2.0.0-draft.1) is NOT local to this
repo — the SY numbering here follows the ADR-0045 rollout list (rollout
3 owns SY-05…SY-07, SY-10, SY-11) with the item semantics taken from the
ADR text (Decisions 4/6) and this wave's task definition.
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from vesma.service import fhs
from vesma.service import system_install as si
from vesma.service import system_update as su
from vesma.service.system_install import SystemInstallError, install_system, read_system_state
from vesma.service.system_update import update_system

ENGINE_VERSION = si._engine_version()
OLD_VERSION = "6.0.0-old"


# ── Fixtures (dest-dir simulation, no root) ───────────────────────────


@dataclass
class Seam:
    """Recorded subprocess-seam calls (assertion surface)."""

    sysusers: list[Path] = field(default_factory=list)
    systemctl: list[list[str]] = field(default_factory=list)
    venvs: list[tuple[Path, tuple[str, ...]]] = field(default_factory=list)
    probes: list[Path] = field(default_factory=list)


def _version_from_pins(requirements: tuple[str, ...]) -> str:
    for line in requirements:
        if line.startswith("vesma=="):
            return line.split("==", 1)[1]
    raise AssertionError(f"no vesma pin in {requirements}")


def _make_fake_materializer(record: Seam):  # type: ignore[no-untyped-def]
    """Materialize executable sh-stubs whose `--version` self-reports the
    pin the venv was built from (the real health probe runs against them)."""

    def fake_materialize(
        venv_dir: Path, requirements: tuple[str, ...], mode: int, **_kwargs: object
    ) -> dict[str, str]:
        version = _version_from_pins(requirements)
        record.venvs.append((venv_dir, requirements))
        (venv_dir / "bin").mkdir(parents=True)
        vesma_bin = venv_dir / "bin" / "vesma"
        vesma_bin.write_text(f"#!/bin/sh\necho vesma {version}\n", encoding="utf-8")
        vesma_bin.chmod(0o755)
        python_bin = venv_dir / "bin" / "python"
        python_bin.write_text("#!/bin/sh\necho 3.13.1\n", encoding="utf-8")
        python_bin.chmod(0o755)
        return {"vesma": version}

    return fake_materialize


@pytest.fixture
def sim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The temp deployment prefix: /etc, /var/lib, /usr/lib under tmp."""
    prefix = tmp_path / "deploy"
    for segment in ("etc", "var/lib", "var/cache", "var/log", "usr/lib", "usr/share", "run"):
        (prefix / segment).mkdir(parents=True)
    monkeypatch.setattr(fhs, "default_prefix", lambda: prefix)
    return prefix


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> Seam:
    record = Seam()
    monkeypatch.setattr(
        si, "_apply_sysusers", lambda fragment: record.sysusers.append(fragment) or "ok (stub)"
    )
    monkeypatch.setattr(
        si, "_systemctl_system", lambda args: record.systemctl.append(args) or "ok (stub)"
    )
    monkeypatch.setattr(si, "_materialize_venv", _make_fake_materializer(record))
    return record


@pytest.fixture
def installed_old(sim: Path, seams: Seam, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A system profile installed as version 6.0.0-old (the 'previous
    deployment'); afterwards the RUNNING build's version is restored."""
    monkeypatch.setattr(si, "_engine_version", lambda: OLD_VERSION)
    install_system()
    monkeypatch.setattr(si, "_engine_version", lambda: ENGINE_VERSION)
    return sim


def _readlink(path: Path) -> str:
    return os.readlink(path)


# ── SY-11: mode-aware preconditions ───────────────────────────────────


class TestUpdatePreconditions:
    def test_refuses_without_installation(self, sim: Path, seams: Seam) -> None:
        with pytest.raises(SystemInstallError, match="no system profile is installed"):
            update_system()

    def test_refuses_drift_before_any_action(self, installed_old: Path, seams: Seam) -> None:
        # Someone ate the unit: DR-17 drift. The update must refuse
        # BEFORE materializing anything or touching the tree.
        (installed_old / "etc/systemd/system/vesma.service").unlink()
        materialized_before = len(seams.venvs)
        before = sorted(str(p.relative_to(installed_old)) for p in installed_old.rglob("*"))
        with pytest.raises(SystemInstallError, match="refusing the update BEFORE"):
            update_system()
        after = sorted(str(p.relative_to(installed_old)) for p in installed_old.rglob("*"))
        assert before == after, "the drift refusal must be a no-op"
        assert len(seams.venvs) == materialized_before, "nothing may be materialized on drift"

    def test_refuses_real_prefix_without_root(
        self, monkeypatch: pytest.MonkeyPatch, seams: Seam
    ) -> None:
        monkeypatch.setattr(fhs, "default_prefix", lambda: Path(os.sep))
        monkeypatch.setattr(si.os, "geteuid", lambda: 1000)
        with pytest.raises(SystemInstallError, match="sudo vesma system install"):
            update_system()

    def test_refuses_revision_path_outside_code_plane(
        self, installed_old: Path, seams: Seam, tmp_path: Path
    ) -> None:
        """P1 containment pin (cascade fix): a tampered install-state row
        (role venv_engine_revision, path = a decoy dir OUTSIDE the code
        plane, with content) must be refused BEFORE any mutation — the
        decoy survives byte-for-byte and nothing is materialized."""
        import yaml

        decoy = tmp_path / "decoy-outside-code-plane"
        decoy.mkdir()
        (decoy / "precious.txt").write_text("operator data\n", encoding="utf-8")
        state_path = installed_old / "var/lib/vesma/install-state.yaml"
        doc = yaml.safe_load(state_path.read_text(encoding="utf-8"))
        doc["artifacts"].append(
            {
                "role": "venv_engine_revision",
                "kind": "dir",
                "path": str(decoy),
                "mode": "0755",
                "owner": None,
                "group": None,
                "status": "created",
                "remove_mode": "rmtree",
            }
        )
        state_path.write_text(
            yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        before = sorted(str(p.relative_to(installed_old)) for p in installed_old.rglob("*"))
        with pytest.raises(SystemInstallError, match="OUTSIDE the code"):
            update_system()
        after = sorted(str(p.relative_to(installed_old)) for p in installed_old.rglob("*"))
        assert before == after, "the containment refusal must be a no-op"
        assert (decoy / "precious.txt").read_text(encoding="utf-8") == "operator data\n"
        assert len(seams.venvs) == 2, "nothing may be materialized on a tampered state"

    def test_refuses_malformed_artifact_records(self, installed_old: Path, seams: Seam) -> None:
        """Coverage pin (cascade fix): a malformed install-state row makes
        system_state_drift report the malformed leg and the updater
        refuse BEFORE any action."""
        import yaml

        state_path = installed_old / "var/lib/vesma/install-state.yaml"
        doc = yaml.safe_load(state_path.read_text(encoding="utf-8"))
        doc["artifacts"].append({"role": "broken-row"})  # no path/kind/mode
        state_path.write_text(
            yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        drift = si.system_state_drift()
        assert any("malformed" in problem for problem in drift)
        with pytest.raises(SystemInstallError, match="refusing the update BEFORE"):
            update_system()


# ── SY-07: the update mechanics ───────────────────────────────────────


class TestUpdateMechanics:
    def test_new_revision_materialized_swapped_old_retired(
        self, installed_old: Path, seams: Seam
    ) -> None:
        result = update_system()
        engine = installed_old / "usr/lib/vesma/venv"
        assert _readlink(engine) == f"venv-{ENGINE_VERSION}"
        assert (installed_old / "usr/lib/vesma" / f"venv-{ENGINE_VERSION}").is_dir()
        assert not (installed_old / "usr/lib/vesma" / f"venv-{OLD_VERSION}").exists(), (
            "the previous engine revision must be retired after the gate"
        )
        assert not (installed_old / "usr/lib/vesma/venvs" / f"metrics-{OLD_VERSION}").exists()
        assert any("retired previous revision" in line for line in result.lines)
        assert any("health:" in line for line in result.lines)
        # The state describes the NEW revision set (exact-inverse stays exact).
        doc = read_system_state()
        assert doc is not None
        assert doc["engine_version"] == ENGINE_VERSION
        recorded_paths = {a["path"] for a in doc["artifacts"]}
        assert str(installed_old / "usr/lib/vesma" / f"venv-{ENGINE_VERSION}") in recorded_paths
        assert str(installed_old / "usr/lib/vesma" / f"venv-{OLD_VERSION}") not in recorded_paths

    def test_same_build_pins_uv_seams_only_no_foreign_venvs(
        self, installed_old: Path, seams: Seam
    ) -> None:
        update_system()
        pins = {reqs for _, reqs in seams.venvs}
        assert ("vesma==" + ENGINE_VERSION,) in pins, (
            "the new revision is built from the SAME build's wheel pin"
        )
        code_root = str(installed_old / "usr/lib/vesma")
        for venv_dir, _ in seams.venvs:
            assert str(venv_dir).startswith(code_root), (
                "venvs are only ever materialized in the manifest code plane"
                " (foreign venvs are never written, owner rule 8)"
            )
        # No pip anywhere: the fake never ran, but the contract holds
        # structurally — the updater only calls the installer's uv seams.

    def test_health_failure_rolls_back(
        self, installed_old: Path, seams: Seam, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The new revision materializes but fails its health probe.
        monkeypatch.setattr(
            su, "_probe_revision", lambda rev_dir, marker, *, expected: (False, "stub failure")
        )
        engine = installed_old / "usr/lib/vesma/venv"
        state_before = (installed_old / "var/lib/vesma/install-state.yaml").read_text(
            encoding="utf-8"
        )
        with pytest.raises(SystemInstallError, match=r"health gate FAILED.*ROLLED BACK"):
            update_system()
        # The previous revision is ACTIVE again (symlink rolled back).
        assert _readlink(engine) == f"venv-{OLD_VERSION}"
        assert (installed_old / "usr/lib/vesma" / f"venv-{OLD_VERSION}").is_dir()
        # The failed new revision stays on disk for diagnosis (DR-16 leftover).
        assert (installed_old / "usr/lib/vesma" / f"venv-{ENGINE_VERSION}").is_dir()
        # The state is byte-restored: it describes the ACTIVE revision.
        assert (installed_old / "var/lib/vesma/install-state.yaml").read_text(
            encoding="utf-8"
        ) == state_before
        doc = read_system_state()
        assert doc is not None
        assert doc["engine_version"] == OLD_VERSION

    def test_install_failure_rolls_back(
        self, installed_old: Path, seams: Seam, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken_install(prefix: Path | None = None) -> si.SystemInstallResult:
            raise SystemInstallError("uv is not on PATH (simulated)")

        monkeypatch.setattr(su.si, "install_system", broken_install)
        with pytest.raises(SystemInstallError, match="uv is not on PATH"):
            update_system()
        assert _readlink(installed_old / "usr/lib/vesma/venv") == f"venv-{OLD_VERSION}"
        doc = read_system_state()
        assert doc is not None
        assert doc["engine_version"] == OLD_VERSION

    def test_idempotent_rerun(self, installed_old: Path, seams: Seam) -> None:
        first = update_system()
        materialized_after_first = len(seams.venvs)
        second = update_system()
        assert len(seams.venvs) == materialized_after_first, (
            "the re-run reuses healthy revisions — nothing re-materialized"
        )
        assert any("refresh run" in line for line in second.lines)
        assert "retired previous revision" not in "\n".join(second.lines), (
            "a refresh retires nothing — its old set IS the new set"
        )
        assert first.engine_venv == second.engine_venv

    def test_probe_revision_contract(self, tmp_path: Path) -> None:
        rev = tmp_path / "rev"
        (rev / "bin").mkdir(parents=True)
        ok, detail = su._probe_revision(rev, "bin/vesma", expected="1.0")
        assert not ok and "done marker" in detail
        entrypoint = rev / "bin" / "vesma"
        entrypoint.write_text("#!/bin/sh\necho vesma 1.0\n", encoding="utf-8")
        entrypoint.chmod(0o755)
        ok, detail = su._probe_revision(rev, "bin/vesma", expected="1.0")
        assert ok, detail
        ok, detail = su._probe_revision(rev, "bin/vesma", expected="9.9")
        assert not ok and "expected" in detail
        failing = rev / "bin" / "boom"
        failing.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
        failing.chmod(0o755)
        ok, detail = su._probe_revision(rev, "bin/boom", expected=None)
        assert not ok and "exit 3" in detail

    @pytest.mark.parametrize(
        ("printed", "expected", "accept"),
        [
            ("vesma 1.0", "1.0", True),  # exact token passes
            ("vesma 1.01", "1.0", False),  # prefix-padded version must NOT pass
            ("vesma 1.0rc1", "1.0", False),  # suffix-attached must NOT pass
            ("vesma 21.0", "1.0", False),  # digit-prepended must NOT pass
            ("1.0.5", "1.0", False),  # longer dotted token must NOT pass
            ("vesma 1.0\n", "1.0", True),  # trailing newline is stripped upstream
        ],
    )
    def test_probe_version_token_boundary(
        self, tmp_path: Path, printed: str, expected: str, accept: bool
    ) -> None:
        """P2 pin (cascade fix): the version check is token-boundary, not
        substring — "1.01"/"1.0rc1"/"21.0" must never pass as "1.0"."""
        rev = tmp_path / "rev"
        (rev / "bin").mkdir(parents=True)
        entrypoint = rev / "bin" / "vesma"
        entrypoint.write_text(f"#!/bin/sh\necho '{printed}'\n", encoding="utf-8")
        entrypoint.chmod(0o755)
        ok, detail = su._probe_revision(rev, "bin/vesma", expected=expected)
        assert ok is accept, (printed, expected, detail)

    def test_rollback_failure_reported_in_error(
        self, installed_old: Path, seams: Seam, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """P2 pin (cascade fix): when the ROLLBACK itself partly fails,
        the raised error carries the restore lines and says INCOMPLETE —
        it must not claim a clean rollback."""
        monkeypatch.setattr(
            su, "_probe_revision", lambda rev_dir, marker, *, expected: (False, "stub failure")
        )

        # Fail the ROLLBACK's symlink restores only — install_system's own
        # swaps (engine + metrics) must succeed so the run reaches the
        # health gate with the rollback leg broken.
        real_symlink = su.si._atomic_symlink
        calls = {"n": 0}

        def flaky_symlink(target: Path, link_path: Path) -> None:
            calls["n"] += 1
            if calls["n"] > 2:
                raise OSError("simulated restore failure")
            real_symlink(target, link_path)

        monkeypatch.setattr(su.si, "_atomic_symlink", flaky_symlink)
        with pytest.raises(SystemInstallError) as excinfo:
            update_system()
        text = str(excinfo.value)
        assert "ROLLBACK INCOMPLETE" in text, text
        assert "could not restore" in text, text
        assert "health gate FAILED" in text
        # The state text itself still restored (only the symlink leg failed).
        doc = read_system_state()
        assert doc is not None
        assert doc["engine_version"] == OLD_VERSION

    def test_clean_rollback_verdict_without_restore_failures(
        self, installed_old: Path, seams: Seam, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            su, "_probe_revision", lambda rev_dir, marker, *, expected: (False, "stub failure")
        )
        with pytest.raises(SystemInstallError) as excinfo:
            update_system()
        text = str(excinfo.value)
        assert "ROLLED BACK to the previous revision(s)" in text
        assert "INCOMPLETE" not in text
        assert "Rollback log:" in text  # the log rides the error (CLI prints lines on success only)

    def test_no_shell_subprocess_anywhere(self) -> None:
        """Security pin (the wave is lifecycle/trusted-boundary code):
        every subprocess call in the updater uses an explicit argv — no
        ``shell=True`` anywhere in the module."""
        import inspect

        source = inspect.getsource(su)
        assert "shell=True" not in source


# ── SY-05: conffile discipline ────────────────────────────────────────


class TestSy05ConffileDiscipline:
    def test_operator_edit_kept_new_default_as_rpmnew(
        self, installed_old: Path, seams: Seam
    ) -> None:
        manifest_path = installed_old / "etc/vesma/components.d/board.yaml"
        original = manifest_path.read_text(encoding="utf-8")
        manifest_path.write_text(original + "\n# operator edit\n", encoding="utf-8")
        edited = manifest_path.read_text(encoding="utf-8")
        update_system()
        assert manifest_path.read_text(encoding="utf-8") == edited, (
            "SY-05: the operator's edit is the file of record — never overwritten"
        )
        rpmnew = installed_old / "etc/vesma/components.d/board.yaml.rpmnew"
        assert rpmnew.is_file(), "the new default lands next to the kept file"
        assert rpmnew.read_text(encoding="utf-8") == original
        # The pending default is installer-owned: recorded in the state.
        doc = read_system_state()
        assert doc is not None
        recorded = {a["path"] for a in doc["artifacts"]}
        assert str(rpmnew) in recorded

    def test_pending_rpmnew_never_clobbered(self, installed_old: Path, seams: Seam) -> None:
        manifest_path = installed_old / "etc/vesma/components.d/board.yaml"
        original = manifest_path.read_text(encoding="utf-8")
        manifest_path.write_text(original + "\n# operator edit\n", encoding="utf-8")
        update_system()
        rpmnew = installed_old / "etc/vesma/components.d/board.yaml.rpmnew"
        first = rpmnew.read_text(encoding="utf-8")
        mtime = rpmnew.stat().st_mtime_ns
        update_system()  # second run: content still differs, rpmnew pending
        assert rpmnew.read_text(encoding="utf-8") == first
        assert rpmnew.stat().st_mtime_ns == mtime, (
            "an unreviewed pending default is never rewritten"
        )

    def test_byte_identical_reinstall_touches_nothing(self, sim: Path, seams: Seam) -> None:
        install_system()
        manifest_path = sim / "etc/vesma/components.d/metrics.yaml"
        first = manifest_path.read_text(encoding="utf-8")
        mtime = manifest_path.stat().st_mtime_ns
        install_system()
        update_system()
        assert manifest_path.read_text(encoding="utf-8") == first
        assert manifest_path.stat().st_mtime_ns == mtime, (
            "a byte-identical conffile is not rewritten (mtime discipline)"
        )
        assert not (sim / "etc/vesma/components.d/metrics.yaml.rpmnew").exists()

    def test_config_yaml_never_seeded_or_touched(self, installed_old: Path, seams: Seam) -> None:
        config = installed_old / "etc/vesma/config.yaml"
        assert not config.exists()
        update_system()
        assert not config.exists(), (
            "config.yaml is operator-provisioned — the installer/updater never seeds it (SY-05)"
        )

    def test_rpmnew_records_survive_next_install_state_rewrite(
        self, installed_old: Path, seams: Seam
    ) -> None:
        manifest_path = installed_old / "etc/vesma/components.d/board.yaml"
        manifest_path.write_text(
            manifest_path.read_text(encoding="utf-8") + "\n# operator edit\n",
            encoding="utf-8",
        )
        update_system()
        install_system()  # a plain reinstall rewrites the state from scratch
        doc = read_system_state()
        assert doc is not None
        recorded = {a["path"] for a in doc["artifacts"]}
        rpmnew = installed_old / "etc/vesma/components.d/board.yaml.rpmnew"
        assert str(rpmnew) in recorded, (
            "a pending .rpmnew stays recorded across state rewrites — it"
            " must never become invisible to the exact-inverse uninstall"
        )

    def test_loader_tolerates_rpmnew_refuses_other_strays(self, tmp_path: Path) -> None:
        from vesma.service.manifest import ManifestError, bundled_manifest_path, load_installation

        comp_dir = tmp_path / "components.d"
        comp_dir.mkdir()
        shutil.copy(bundled_manifest_path("board"), comp_dir / "board.yaml")
        (comp_dir / "board.yaml.rpmnew").write_text("name: pending\n", encoding="utf-8")
        loaded = load_installation(comp_dir)
        assert "board" in loaded
        (comp_dir / "editor-dropping.bak").write_text("stray\n", encoding="utf-8")
        with pytest.raises(ManifestError, match="unexpected file"):
            load_installation(comp_dir)


# ── SY-06: journald primary, /var/log/vesma opt-in only ───────────────


class TestSy06OptInLogs:
    def test_update_never_creates_the_log_root(self, installed_old: Path, seams: Seam) -> None:
        update_system()
        assert not (installed_old / "var/log/vesma").exists(), (
            "file logs are opt-in only (§3.9): the update never creates"
            " /var/log/vesma — journald stays primary"
        )

    def test_operator_created_log_root_survives(self, installed_old: Path, seams: Seam) -> None:
        log_dir = installed_old / "var/log/vesma/board"
        log_dir.mkdir(parents=True)
        marker = log_dir / "app.log"
        marker.write_text("operator's opt-in log\n", encoding="utf-8")
        update_system()
        assert marker.read_text(encoding="utf-8") == "operator's opt-in log\n", (
            "an operator-created opt-in log tree is never touched"
        )

    def test_unit_keeps_optional_log_mount(self, installed_old: Path, seams: Seam) -> None:
        update_system()
        unit = (installed_old / "etc/systemd/system/vesma.service").read_text(encoding="utf-8")
        assert f"-{installed_old}/var/log/vesma" in unit, (
            "the opt-in log root renders with the systemd '-' prefix — a"
            " missing tree must not brick the unit"
        )


# ── boundary: modes stay manifest-exact after update ──────────────────


class TestTreeRightsAfterUpdate:
    def test_new_revision_and_state_keep_manifest_modes(
        self, installed_old: Path, seams: Seam
    ) -> None:
        update_system()
        new_rev = installed_old / "usr/lib/vesma" / f"venv-{ENGINE_VERSION}"
        assert stat.S_IMODE(new_rev.stat().st_mode) == 0o755
        state = installed_old / "var/lib/vesma/install-state.yaml"
        assert stat.S_IMODE(state.stat().st_mode) == 0o640


# ── CLI wiring: `vesma service update --system` ───────────────────────


class TestCliWiring:
    def test_update_system_flag_dispatches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from typer.testing import CliRunner

        from vesma.cli import service as service_cli

        called: list[bool] = []

        def fake_update_system() -> su.SystemUpdateResult:
            called.append(True)
            return su.SystemUpdateResult(
                lines=("health: ok",),
                engine_venv=Path("/tmp/venv"),
                revisions=(),
                rolled_back=False,
            )

        monkeypatch.setattr(service_cli, "update_system", fake_update_system)
        result = CliRunner().invoke(service_cli.service_app, ["update", "--system"])
        assert result.exit_code == 0, result.output
        assert called == [True]

    def test_update_without_system_refuses_pointing_at_self_update(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from vesma.cli import service as service_cli

        monkeypatch.setattr(
            service_cli, "update_system", lambda: pytest.fail("must refuse before dispatch")
        )
        result = CliRunner().invoke(service_cli.service_app, ["update"])
        assert result.exit_code == 1
        assert "self-update" in result.output
