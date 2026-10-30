"""contrib/eyes operator template — the wave R install-leg contract.

The eyes component template (ArchCom 2026-10-29 verdict 1c2b8d64, wave R:
"eyes are a manifest component — the platform owns the lifecycle") is
OPERATOR-documented, deliberately NOT bundled (CM §3.1). These tests pin
the template's SHAPE against the platform contract so a future edit
cannot silently drift it out of the ratified form: python child on the
venv_component slot (never the binary leg — that leg materializes no
component venv, which would leave eyes' dependencies outside the slot
table), loopback-only bind, secrets in the env.d slot (never env.vars),
exact-pins grammar, env/data slots per the FHS table. The real venv/pip
leg is faked — no test touches PyPI or the host systemd bus (the
test_service_install.py discipline).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from vesma.service import install as install_mod
from vesma.service import layout
from vesma.service.install import InstallError, install, install_component
from vesma.service.manifest import load_manifest

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "contrib" / "eyes"
TEMPLATE = TEMPLATE_DIR / "eyes.yaml"
ENV_EXAMPLE = TEMPLATE_DIR / "eyes.env.example"

#: The dependency floor of vesma-eyes (server/requirements.txt of the
#: pinned release) — every name must appear among the template's pins.
REQUIRED_REQUIREMENT_NAMES = frozenset(
    {"fastapi", "uvicorn", "httpx", "pydantic", "pyyaml", "asyncssh"}
)

#: The env.d slot keys the eyes server actually reads (server/app.py +
#: server/memory_registry.py + server/security.py).
REQUIRED_ENV_KEYS = frozenset(
    {
        "MNEMOS_URL",
        "MNEMOS_TOKEN",
        "VESMARO_BOARD_TOKEN",
        "VESMARO_UI_TOKEN",
        "VESMARO_ALLOWED_MEMORY_HOSTS",
    }
)

#: Non-secret example values allowed beside `change-me` placeholders.
ENV_EXAMPLE_NON_SECRET_VALUES = frozenset({"http://127.0.0.1:8787", "127.0.0.1,localhost"})

#: CM §3.5 — env.vars key names must never carry secret-like tokens (the
#: secrets belong to the env.d slot file).
_SECRET_KEY_TOKENS = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "private_key",
    "credential",
)


@pytest.fixture
def template():  # type: ignore[no-untyped-def]
    """The template loaded through the REAL CM loader (fail-closed)."""
    return load_manifest(TEMPLATE)


@pytest.fixture
def isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate every XDG root + HOME into a tmp dir (empty vars = defaults)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for var in (
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XDG_CACHE_HOME",
        "XDG_RUNTIME_DIR",
    ):
        monkeypatch.delenv(var, raising=False)
    return home


@pytest.fixture(autouse=True)
def no_host_systemctl(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never talk to the host user systemd bus from tests."""
    monkeypatch.setattr(install_mod, "_daemon_reload", lambda: "ok (stubbed)")
    monkeypatch.setattr(
        install_mod, "_systemctl_user", lambda args: f"ok (stubbed: {' '.join(args)})"
    )


@pytest.fixture
def fake_venv_installer(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace the venv/pip leg: create the dir + a lock file (no PyPI)."""
    created: list[str] = []

    def fake_install_component_venv(manifest, report):  # type: ignore[no-untyped-def]
        name = manifest.name
        created.append(name)
        venv_dir = layout.component_venv_dir(name)
        venv_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(venv_dir, 0o700)
        report.append(f"venv: {name} -> {venv_dir}")
        return venv_dir

    monkeypatch.setattr(install_mod, "_install_component_venv", fake_install_component_venv)
    return created


def _install_template(home: Path) -> Path:
    """The template copied verbatim into the components.d slot.

    Verbatim is the point: the operator's first install must pass
    fail-closed validation BEFORE the OPERATOR-EDIT values are filled in
    (paths are not validated at install time — they gate the START).
    """
    components = layout.components_dir()
    components.mkdir(parents=True, exist_ok=True)
    target = components / "eyes.yaml"
    target.write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    return target


class TestTemplateShape:
    """The template as a contract artifact — CM loader + leg classification."""

    def test_template_dir_is_complete(self) -> None:
        assert TEMPLATE.is_file()
        assert ENV_EXAMPLE.is_file()
        assert (TEMPLATE_DIR / "README.md").is_file()

    def test_loads_as_python_child(self, template):  # type: ignore[no-untyped-def]
        assert template.name == "eyes"
        assert template.kind == "child-process"
        assert template.metadata.tier == "optional"
        # The venv leg (python child), never the binary leg:
        assert install_mod._has_venv(template)
        assert not install_mod.is_binary_child(template)
        # No launch.binary and NO sha pin: the artifact_sha256 gate hashes
        # argv[0] at every start — on a python child that would be the venv
        # interpreter, not an eyes artifact. Dependencies pin via requirements.
        assert template.launch_binary_path() is None
        assert template.metadata.provenance.artifact_sha256 is None

    def test_requirements_are_exact_pins_and_cover_the_floor(
        self,
        template,  # type: ignore[no-untyped-def]
    ) -> None:
        requirements = template.launch.python.requirements
        assert requirements, "python child declares its dependency pins"
        for line in requirements:
            assert install_mod._PIN_RE.match(line), f"not an exact pin: {line!r}"
            assert "{engine_version}" not in line, (
                "operator template — not a release-train-tracked bundled manifest"
            )
        names = {line.split("==", 1)[0] for line in requirements}
        assert REQUIRED_REQUIREMENT_NAMES.issubset(names)

    def test_loopback_bind_and_port_consistency(self, template):  # type: ignore[no-untyped-def]
        argv = template.launch.argv
        host_index = argv.index("--host")
        assert argv[host_index + 1] == "127.0.0.1", (
            "loopback-only is the v1 platform posture (public-bind edge)"
        )
        port_index = argv.index("--port")
        url = template.health.http.url
        assert url.startswith("http://127.0.0.1:"), "health probe follows the bind posture"
        assert url.rsplit(":", 1)[1].split("/", 1)[0] == argv[port_index + 1], (
            "argv --port and health.url must move together"
        )

    def test_cwd_matches_app_dir(self, template):  # type: ignore[no-untyped-def]
        app_dir_index = template.launch.argv.index("--app-dir")
        assert template.launch.cwd == template.launch.argv[app_dir_index + 1]

    def test_data_slot_literal_is_absolute(self, template):  # type: ignore[no-untyped-def]
        vars_ = template.launch.env.vars
        assert set(vars_) == {"VESMARO_DATA"}
        data_dir = vars_["VESMARO_DATA"]
        assert data_dir.startswith("/"), (
            "env.vars values are NOT expanded — the slot literal must be absolute"
        )

    def test_secrets_live_in_the_env_d_slot(
        self,
        template,
        isolated_home: Path,  # type: ignore[no-untyped-def]
    ) -> None:
        for key in template.launch.env.vars:
            lowered = key.lower()
            for token in _SECRET_KEY_TOKENS:
                assert token not in lowered, f"secret-shaped key in env.vars: {key}"
        env_file = template.launch.env.env_file
        assert env_file is not None
        resolved = Path(env_file).expanduser()
        assert resolved.parent.name == "env.d", "secrets belong to the env.d slot"
        # Outside the manifests dir (the envfile fail-closed class) — both
        # the canonical slot and the declaring template's directory.
        for base in (layout.components_dir(), TEMPLATE_DIR):
            assert base not in resolved.parents, "env_file must lie outside the manifests dir"


class TestEnvExample:
    """The secrets-slot template: right keys, no real values ever."""

    @staticmethod
    def _entries() -> dict[str, str]:
        entries: dict[str, str] = {}
        for raw in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            entries[key.strip()] = value.strip()
        return entries

    def test_required_keys_present(self) -> None:
        assert set(self._entries()) >= REQUIRED_ENV_KEYS

    def test_no_real_secrets(self) -> None:
        for key, value in self._entries().items():
            assert "change-me" in value or value in ENV_EXAMPLE_NON_SECRET_VALUES, (
                f"{key} carries a non-placeholder value in a committed example"
            )


class TestInstallLeg:
    """The operator's install legs against the RAW (verbatim) template."""

    @pytest.fixture
    def eyes_manifest(self, isolated_home: Path) -> Path:
        return _install_template(isolated_home)

    def test_per_component_verb_validates_and_leaves_venv_to_service_install(
        self,
        isolated_home: Path,
        eyes_manifest: Path,  # type: ignore[no-untyped-def]
    ) -> None:
        result = install_component("eyes")
        assert result.origin == "operator"
        assert any("needs a component venv" in line for line in result.lines), (
            "the per-component verb hints at `vesma service install` for the venv"
        )
        assert any(
            line.startswith("manifest:") and "operator-authored" in line for line in result.lines
        )
        # The component_data slot materializes at install time.
        assert layout.data_dir("eyes").is_dir()
        # No binary leg ran (eyes is a python child — no bin/eyes slot).
        assert not layout.binary_artifact_dir("eyes").exists()

    def test_per_component_verb_refuses_binary_flag(
        self,
        isolated_home: Path,
        eyes_manifest: Path,
        tmp_path: Path,  # type: ignore[no-untyped-def]
    ) -> None:
        artifact = tmp_path / "not-a-binary"
        artifact.write_bytes(b"\x7fELF fake")
        with pytest.raises(InstallError, match="applies to binary child"):
            install_component("eyes", binary=artifact)

    def test_full_install_materializes_the_venv_component_slot(
        self,
        isolated_home: Path,
        eyes_manifest: Path,
        fake_venv_installer: list[str],  # type: ignore[no-untyped-def]
    ) -> None:
        install()
        assert "eyes" in fake_venv_installer, "python child — the venv leg ran"
        venv_slot = layout.component_venv_dir("eyes")
        assert venv_slot.is_dir()
        mode = stat.S_IMODE(venv_slot.stat().st_mode)
        assert mode == 0o700, "the component venv slot is owner-private (layout §3.8)"
