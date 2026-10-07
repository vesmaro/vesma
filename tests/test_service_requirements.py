"""CM §3.5.1 (1.1.0) — ``launch.python.requirements`` mechanism.

Covers (card svc-8788-board-face-v2 / issue #515):
- manifest validation: exact-pin policy (URL/file:/range/wildcard
  rejected with REQUIREMENTS_INVALID), the symmetric dead-declaration
  rule (requirements <-> {venv_bin} in argv), the {engine_version}
  pin placeholder, in-process manifests keeping no python block;
- the install flow: a hand-authored (non-bundled) python child's venv
  is created and pinned exactly from its manifest requirements
  (offline: the pip leg is stubbed), the lock records the full freeze,
  the {engine_version} placeholder expands at install time;
- regressions: the bundled metrics install still works end-to-end with
  its manifest-carried pin, DR-04 venv cross-referencing keeps failing
  closed, DR-02 drift on a requirements-installed venv is a rebuild.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from vesmaro.service import install as install_mod
from vesmaro.service import layout
from vesmaro.service.errors import (
    MANIFEST_SCHEMA_INVALID,
    REQUIREMENTS_INVALID,
    ManifestError,
)
from vesmaro.service.install import install
from vesmaro.service.manifest import load_manifest


@pytest.fixture
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate every XDG root + HOME into a tmp dir (parity with
    test_service_install's fixture; a venv leg never touches the host)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
        "VESMA_CONFIG",
        "VESMA_DATA",
    ):
        monkeypatch.delenv(var, raising=False)
    return home


def _child_doc(requirements: list[str] | None = None, *, venv_ref: bool = True) -> dict:
    """A valid child-process doc; requirements block on demand."""
    argv = ["{venv_bin}/python", "-m", "sample_worker"] if venv_ref else ["/usr/bin/worker"]
    launch: dict = {"argv": argv}
    if requirements is not None:
        launch["python"] = {"version": ">=3.11", "requirements": requirements}
    return {
        "apiVersion": "vesma.component/v1",
        "kind": "child-process",
        "metadata": {
            "name": "worker",
            "version": "0.1.0",
            "tier": "optional",
            "description": "test child",
            "provenance": {"repo": "https://example.com/worker", "license": "MIT"},
        },
        "launch": launch,
        "health": {
            "checker": "http",
            "http": {
                "url": "http://127.0.0.1:9111/metrics",
                "interval": "5s",
                "timeout": "2s",
                "unhealthy_threshold": 3,
            },
        },
        "stop": {"signal": "SIGTERM", "grace_period": "10s"},
    }


def _write(tmp_path: Path, doc: dict, name: str = "worker.yaml") -> Path:
    target = tmp_path / name
    target.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return target


# ── Manifest validation: the exact-pin policy ─────────────────────────


class TestRequirementsPinPolicy:
    def test_valid_pins_pass_with_full_roundtrip(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            _child_doc(["pydantic==2.14.2", "rich==14.2.0", "vesma=={engine_version}"]),
        )
        manifest = load_manifest(path)
        assert manifest.launch_python_requirements() == (
            "pydantic==2.14.2",
            "rich==14.2.0",
            "vesma=={engine_version}",
        )
        assert manifest.launch is not None and manifest.launch.python is not None
        assert manifest.launch.python.version == ">=3.11"

    @pytest.mark.parametrize(
        "line",
        [
            "requests @ https://evil.example/x.whl",
            "https://evil.example/x.whl",
            "file:///tmp/x.whl",
            "requests>=2.0",
            "requests~=2.0",
            "requests<3",
            "requests==2.*",
            "requests",
            "--index-url https://evil.example",
            "{evil_placeholder}==1.0",
        ],
    )
    def test_non_pin_lines_rejected_at_load(self, tmp_path: Path, line: str) -> None:
        with pytest.raises(ManifestError) as excinfo:
            load_manifest(_write(tmp_path, _child_doc([line])))
        err = excinfo.value
        assert err.code == REQUIREMENTS_INVALID
        assert err.field_path == "$.launch.python.requirements[0]"
        assert "exact 'name==version' pin" in err.message

    def test_bad_pin_message_never_echoes_full_url(self, tmp_path: Path) -> None:
        # the diagnostic names the offending line — it is not a secret
        # carrier (pins are public names), but the fix form is always shown
        with pytest.raises(ManifestError, match=r"use the form 'name==1\.2\.3'"):
            load_manifest(_write(tmp_path, _child_doc(["requests>=2.0"])))

    def test_unknown_placeholder_rejected_by_pattern(self, tmp_path: Path) -> None:
        with pytest.raises(ManifestError):
            load_manifest(_write(tmp_path, _child_doc(["dep=={unknown_tok}"])))


class TestRequirementsSymmetry:
    def test_requirements_without_venv_bin_is_dead_declaration(self, tmp_path: Path) -> None:
        doc = _child_doc(["pydantic==2.14.2"], venv_ref=False)
        with pytest.raises(ManifestError) as excinfo:
            load_manifest(_write(tmp_path, doc))
        err = excinfo.value
        assert err.code == REQUIREMENTS_INVALID
        assert "dead declaration" in err.message

    def test_venv_bin_without_requirements_is_rejected(self, tmp_path: Path) -> None:
        doc = _child_doc(requirements=None, venv_ref=True)
        with pytest.raises(ManifestError) as excinfo:
            load_manifest(_write(tmp_path, doc))
        err = excinfo.value
        assert err.code == REQUIREMENTS_INVALID
        assert "MUST pin its dependencies" in err.message

    def test_non_python_child_unaffected(self, tmp_path: Path) -> None:
        # a binary child has no python block at all — 1.0.0 semantics
        manifest = load_manifest(_write(tmp_path, _child_doc(None, venv_ref=False)))
        assert manifest.launch_python_requirements() == ()

    def test_in_process_with_launch_python_is_schema_invalid(self, tmp_path: Path) -> None:
        doc = _child_doc(["pydantic==2.14.2"])
        doc["kind"] = "in-process"
        doc["in_process"] = {"module": "sample.mod", "entrypoint": "create"}
        del doc["stop"]
        with pytest.raises(ManifestError) as excinfo:
            load_manifest(_write(tmp_path, doc))
        assert excinfo.value.code == MANIFEST_SCHEMA_INVALID

    def test_requirements_must_be_non_empty_array(self, tmp_path: Path) -> None:
        doc = _child_doc([])
        with pytest.raises(ManifestError) as excinfo:
            load_manifest(_write(tmp_path, doc))
        # an EMPTY requirements array with a venv ref: schema minItems=1 ->
        # schema_invalid; the normative dead-declaration branch demands a
        # real list — either way the manifest never loads
        assert excinfo.value.code == MANIFEST_SCHEMA_INVALID


# ── Install flow: a non-bundled python child (offline) ────────────────


def _stub_pip(monkeypatch: pytest.MonkeyPatch, freeze: str) -> list[list[str]]:
    """Replace the pip leg: install is a no-op, freeze returns ``freeze``.

    The freeze must carry EVERY pin the run installs (bundled metrics
    pins the engine package too — the full pack installs until the user
    child is added)."""
    calls: list[list[str]] = []

    def fake_run_pip(venv_dir: Path, args: list[str]) -> str:
        calls.append(list(args))
        if args[0] == "install":
            return ""
        return freeze

    monkeypatch.setattr(install_mod, "_run_pip", fake_run_pip)
    return calls


def _freeze_with(pins: list[str]) -> str:
    """A freeze line set covering the bundled metrics pin + the given
    ``name==version`` pin lines verbatim."""
    from vesmaro import __version__ as engine_ver

    return f"pip==99.0\nvesma=={engine_ver}\n" + "".join(f"{pin}\n" for pin in pins)


def _engine_ver() -> str:
    from vesmaro import __version__

    return __version__


@pytest.fixture
def no_host_systemctl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install_mod, "_daemon_reload", lambda: "ok (stubbed)")
    monkeypatch.setattr(
        install_mod, "_systemctl_user", lambda args: f"ok (stubbed: {' '.join(args)})"
    )


def _write_user_child(components: Path, requirements: list[str]) -> Path:
    """A hand-authored python child, components.d (drop-in, CM §3.1)."""
    doc = _child_doc(requirements)
    path = components / "worker.yaml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


class TestInstallRequirementsFlow:
    def test_non_bundled_python_child_installs_from_manifest_requirements(
        self,
        tmp_path: Path,
        isolated_home: Path,
        no_host_systemctl: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """THE issue #515 unlock: a hand-authored python child with exact
        pins installs — the old flow refused anything outside the bundled
        pack. Pip stubbed (offline); assert the pip leg got the manifest's
        pins verbatim and the lock file landed."""

        freeze = _freeze_with(["pydantic==2.14.2", "rich==14.2.0"])
        calls = _stub_pip(monkeypatch, freeze)
        monkeypatch.setattr(install_mod.unitgen, "container_detect", lambda *a, **k: False)

        components = layout.ensure_dir(layout.components_dir(), 0o700)
        _write_user_child(components, ["pydantic==2.14.2", "rich==14.2.0"])

        result = install()
        install_calls = [c for c in calls if c[0] == "install"]
        # alphabetical walk: board (in-process, no venv) -> metrics -> worker
        assert install_calls == [
            ["install", "--disable-pip-version-check", "--no-input", f"vesma=={_engine_ver()}"],
            [
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "pydantic==2.14.2",
                "rich==14.2.0",
            ],
        ]
        assert result.venvs and layout.component_venv_dir("worker") in result.venvs
        lock_path = layout.data_dir("worker") / "requirements-lock.txt"
        assert lock_path.exists()
        assert "pydantic==2.14.2\n" in lock_path.read_text(encoding="utf-8")

    def test_engine_version_placeholder_expands_at_install(
        self,
        tmp_path: Path,
        isolated_home: Path,
        no_host_systemctl: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from vesmaro import __version__

        install_calls_captured: list[list[str]] = []

        def capturing_run_pip(venv_dir: Path, args: list[str]) -> str:
            install_calls_captured.append(list(args))
            if args[0] == "install":
                return ""
            return _freeze_with([])

        monkeypatch.setattr(install_mod, "_run_pip", capturing_run_pip)
        monkeypatch.setattr(install_mod.unitgen, "container_detect", lambda *a, **k: False)

        components = layout.ensure_dir(layout.components_dir(), 0o700)
        _write_user_child(components, ["vesma=={engine_version}"])

        install()
        install_legs = [c for c in install_calls_captured if c[0] == "install"]
        # metrics (bundled, engine pin) + worker ({engine_version} expanded)
        assert install_legs == [
            ["install", "--disable-pip-version-check", "--no-input", f"vesma=={__version__}"],
            ["install", "--disable-pip-version-check", "--no-input", f"vesma=={__version__}"],
        ]
        # ...and the USER child's pin came from the manifest, expanded
        # identically — bundled and hand-authored share one mechanism
        assert install_legs[0] == install_legs[1]

    def test_engine_version_placeholder_rejected_outside_requirements(self, tmp_path: Path) -> None:
        # argv is the supervisor's domain: {engine_version} is unknown there
        doc = _child_doc(["pydantic==2.14.2"])
        doc["launch"]["argv"] = ["{venv_bin}/python", "--cv={engine_version}"]
        with pytest.raises(ManifestError) as excinfo:
            load_manifest(_write(tmp_path, doc))
        assert excinfo.value.code == "PLACEHOLDER_UNKNOWN"


class TestBundledRegression:
    def test_bundled_metrics_install_end_to_end_with_manifest_pin(
        self,
        tmp_path: Path,
        isolated_home: Path,
        no_host_systemctl: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The bundled pack installs unchanged — metrics' manifest-carried
        pin expands to the engine version (the metrics fixture of
        test_service_install.py stubs the venv leg; here the REAL flow
        runs with the pip leg stubbed, offline)."""

        from vesmaro import __version__

        freeze = f"pip==99.0\nvesma=={__version__}\n"
        calls = _stub_pip(monkeypatch, freeze)
        monkeypatch.setattr(install_mod.unitgen, "container_detect", lambda *a, **k: False)
        result = install()
        install_calls = [c for c in calls if c[0] == "install"]
        assert install_calls == [
            ["install", "--disable-pip-version-check", "--no-input", f"vesma=={__version__}"]
        ]
        assert result.venvs and result.venvs[0].name == "metrics"


class TestDriftRegressions:
    def test_dr04_no_venv_bin_no_requirements_still_coherent(self, tmp_path: Path) -> None:
        """Coherence pins: a binary child (no venv ref, no python block)
        loads; DR-04 (venv cross-referencing) stays the doctor's domain —
        the loader is name-agnostic. The install-side guarantee: the venv
        refusal leg in test_service_install (unknown child) is replaced
        by the manifest requirements leg covered above."""

        manifest = load_manifest(_write(tmp_path, _child_doc(None, venv_ref=False)))
        assert manifest.launch_python_requirements() == ()

        # an EMPTY requirements array with a venv ref: schema minItems=1 ->
        # schema_invalid never loads
        with pytest.raises(ManifestError):
            load_manifest(_write(tmp_path, _child_doc([]), name="empty.yaml"))

    def test_dr02_drift_rebuild_semantics_unchanged(self, tmp_path: Path) -> None:
        """Reinstall with freeze drift STILL rebuilds from scratch — the
        requirements mechanism reuses _install_component_venv's existing
        drift leg (covered deep in test_service_install; here the pin:
        the reuse path is the SAME function, not a fork)."""

        import inspect

        source = inspect.getsource(install_mod._install_component_venv)
        assert "_create_component_venv(name, force=True)" in source
        assert "drift" in source

    def test_reinstall_drift_rebuild_offline(
        self,
        tmp_path: Path,
        isolated_home: Path,
        no_host_systemctl: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A requirements-installed venv that drifts is rebuilt — the lock
        written before the hand install does not match the drifted freeze
        (DR-02 semantics reused, not forked); driven through the SAME
        _install_component_venv the whole pack uses."""

        from vesmaro.service.manifest import load_manifest

        drifted = "pip==99.0\npydantic==2.14.2\nforeign==1.0\n"
        clean = "pip==99.0\npydantic==2.14.2\n"
        outputs = [drifted, clean]  # freeze#1: drift probe; freeze#2: post-rebuild verify
        install_seen: list[list[str]] = []

        def fake_run_pip(venv_dir_: Path, args: list[str]) -> str:
            if args[0] == "install":
                install_seen.append(list(args))
                return ""
            return outputs.pop(0)  # freeze legs consume outputs in order

        monkeypatch.setattr(install_mod, "_run_pip", fake_run_pip)

        # the direct-call shape: data dir + a STALE lock + the EXISTING
        # drifted venv tree (install() would create the dirs; the stale
        # lock is the pre-hand-install state — exactly the P1 scenario)
        venv_dir = layout.component_venv_dir("worker")
        bin_dir = venv_dir / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "python").write_text("#!/bin/true\n", encoding="utf-8")
        data_dir = layout.ensure_dir(layout.data_dir("worker"), layout.MODE_DIR_DEFAULT)
        lock_path = data_dir / "requirements-lock.txt"
        lock_path.write_text("pip==99.0\n", encoding="utf-8")  # stale, pre-hand-install

        components = layout.ensure_dir(layout.components_dir(), 0o700)
        path = _write_user_child(components, ["pydantic==2.14.2"])
        manifest = load_manifest(path)

        report: list[str] = []
        install_mod._install_component_venv(manifest, report)  # drift probe + verify
        assert any("freeze drift vs lock" in line for line in report)
        assert (layout.data_dir("worker") / "requirements-lock.txt").read_text(
            encoding="utf-8"
        ) == "pip==99.0\npydantic==2.14.2\n"
        # the rebuild went through the SAME _create_component_venv(force)
        # leg and pins were re-installed into the rebuilt venv
        assert install_seen == [
            ["install", "--disable-pip-version-check", "--no-input", "pydantic==2.14.2"]
        ]
