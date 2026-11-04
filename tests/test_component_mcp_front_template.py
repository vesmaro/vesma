"""contrib/mcp-front operator template — the W2 install-leg contract.

The mcp-front component template (ADR-0050 W2 / Ф2: the MCP front is a
PLATFORM component — a stateless streamable-HTTP proxy that gives
harnesses ONE entry point to the core /mcp plane) is OPERATOR-documented,
deliberately NOT bundled (CM §3.1). These tests pin the template's SHAPE
against the platform contract, mirroring the eyes-template discipline
(test_component_eyes_template.py), so a future edit cannot silently
drift it out of the ratified form: python child on the venv_component
slot (never the binary leg), loopback-only bind, port↔health consistency
(the CM grammar has no port placeholder — the literals are the
contract), NO secrets anywhere (the front is an identity CARRIER —
harness tokens pass through, nothing is stored), env.d as the fail-closed
config channel, exact-pins grammar. The real venv/pip leg is faked — no
test touches PyPI or the host systemd bus (the test_service_install.py
discipline).
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

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "contrib" / "mcp-front"
TEMPLATE = TEMPLATE_DIR / "mcp-front.yaml"
ENV_EXAMPLE = TEMPLATE_DIR / "mcp-front.env.example"

#: The dependency floor of the front module — every name must appear
#: among the template's pins. The engine itself is pinned because the
#: argv runs ``python -m vesma.mcp_front`` from the component venv (the
#: bundled metrics-manifest precedent); uvicorn is the ASGI server,
#: httpx the upstream client.
REQUIRED_REQUIREMENT_NAMES = frozenset({"vesma", "uvicorn", "httpx"})

#: The env.d slot keys the front actually reads (vesma/mcp_front.py).
REQUIRED_ENV_KEYS = frozenset({"VESMA_FRONT_TARGET"})

#: Non-secret example values allowed in the committed example. The front
#: stores NO secrets (C2: tokens pass through) — the target URL is the
#: only key and is deliberately non-secret.
ENV_EXAMPLE_NON_SECRET_VALUES = frozenset({"http://127.0.0.1:8787/mcp/"})

#: CM §3.5 — env.vars key names must never carry secret-like tokens.
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

    Verbatim is the point (the eyes rule): the operator's first install
    must pass fail-closed validation BEFORE the OPERATOR-EDIT values are
    filled in (paths are not validated at install time — they gate the
    START).
    """
    components = layout.components_dir()
    components.mkdir(parents=True, exist_ok=True)
    target = components / "mcp-front.yaml"
    target.write_text(TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    return target


class TestTemplateShape:
    """The template as a contract artifact — CM loader + leg classification."""

    def test_template_dir_is_complete(self) -> None:
        assert TEMPLATE.is_file()
        assert ENV_EXAMPLE.is_file()
        assert (TEMPLATE_DIR / "README.md").is_file()

    def test_loads_as_python_child(self, template):  # type: ignore[no-untyped-def]
        assert template.name == "mcp-front"
        assert template.kind == "child-process"
        assert template.metadata.tier == "optional"
        # The venv leg (python child), never the binary leg:
        assert install_mod._has_venv(template)
        assert not install_mod.is_binary_child(template)
        # No launch.binary and NO sha pin: the artifact_sha256 gate hashes
        # argv[0] at every start — on a python child that would be the venv
        # interpreter, not a front artifact. Dependencies pin via requirements.
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
                "operator template — not a release-train-tracked bundled manifest; "
                "the vesma pin tracks the release train BY HAND (bump on upgrade)"
            )
        names = {line.split("==", 1)[0] for line in requirements}
        assert REQUIRED_REQUIREMENT_NAMES.issubset(names)

    def test_engine_pin_and_metadata_version_agree(
        self,
        template,  # type: ignore[no-untyped-def]
    ) -> None:
        """metadata.version is the vesma release the template was pinned
        against — it must never drift from the vesma requirement pin."""
        vesma_pin = next(
            line for line in template.launch.python.requirements if line.startswith("vesma==")
        )
        assert template.metadata.version == vesma_pin.split("==", 1)[1]

    def test_argv_runs_the_front_module_with_loopback_bind(
        self,
        template,  # type: ignore[no-untyped-def]
    ) -> None:
        argv = template.launch.argv
        assert argv[0] == "{venv_bin}/python"
        assert argv[1:3] == ["-m", "vesma.mcp_front"], (
            "the child runs the engine's own front module (no launcher binary)"
        )
        host_index = argv.index("--host")
        assert argv[host_index + 1] == "127.0.0.1", (
            "loopback-only is the v1 platform posture (public-bind edge)"
        )
        timeout_index = argv.index("--timeout")
        assert int(argv[timeout_index + 1]) > 0, "the upstream timeout is pinned explicitly"

    def test_loopback_bind_and_port_consistency(self, template):  # type: ignore[no-untyped-def]
        """The CM grammar has NO port placeholder — argv literal and
        health.url must move together (the eyes rule, front edition)."""
        argv = template.launch.argv
        port_index = argv.index("--port")
        url = template.health.http.url
        assert url.startswith("http://127.0.0.1:"), "health probe follows the bind posture"
        assert url.rsplit(":", 1)[1].split("/", 1)[0] == argv[port_index + 1], (
            "argv --port and health.url must move together"
        )
        assert url.endswith("/healthz"), "the probe is the front's LOCAL healthz (never forwarded)"

    def test_data_slot_is_not_declared(self, template):  # type: ignore[no-untyped-def]
        """The front is STATELESS — no env.vars literals at all (the eyes
        VESMARO_DATA slot has no front equivalent; the install still
        materializes an empty reserved component_data dir)."""
        assert template.launch.env is not None
        assert set(template.launch.env.vars) == set()

    def test_config_channel_lives_in_the_env_d_slot(
        self,
        template,
        isolated_home: Path,  # type: ignore[no-untyped-def]
    ) -> None:
        env_file = template.launch.env.env_file
        assert env_file is not None
        resolved = Path(env_file).expanduser()
        assert resolved.parent.name == "env.d", "the config channel belongs to the env.d slot"
        # Outside the manifests dir (the envfile fail-closed class) — both
        # the canonical slot and the declaring template's directory.
        for base in (layout.components_dir(), TEMPLATE_DIR):
            assert base not in resolved.parents, "env_file must lie outside the manifests dir"
        for key in template.launch.env.vars:
            lowered = key.lower()
            for token in _SECRET_KEY_TOKENS:
                assert token not in lowered, f"secret-shaped key in env.vars: {key}"


class TestEnvExample:
    """The config-channel template: right keys, NO secrets — ever."""

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

    def test_required_keys_present_and_exclusive(self) -> None:
        """The front carries NO secrets — the target URL is the ONLY key
        (C2: harness tokens pass through, nothing is stored). A second
        key here is a contract smell and must be reviewed, not merged."""
        assert set(self._entries()) == REQUIRED_ENV_KEYS

    def test_no_real_secrets(self) -> None:
        for key, value in self._entries().items():
            assert value in ENV_EXAMPLE_NON_SECRET_VALUES, (
                f"{key} carries a value outside the sanctioned non-secret set "
                "in a committed example"
            )
            for token in _SECRET_KEY_TOKENS:
                assert token not in key.lower(), f"secret-shaped key: {key}"


class TestInstallLeg:
    """The operator's install legs against the RAW (verbatim) template."""

    @pytest.fixture
    def front_manifest(self, isolated_home: Path) -> Path:
        return _install_template(isolated_home)

    def test_per_component_verb_validates_and_leaves_venv_to_service_install(
        self,
        isolated_home: Path,
        front_manifest: Path,  # type: ignore[no-untyped-def]
    ) -> None:
        result = install_component("mcp-front")
        assert result.origin == "operator"
        assert any("needs a component venv" in line for line in result.lines), (
            "the per-component verb hints at `vesma service install` for the venv"
        )
        assert any(
            line.startswith("manifest:") and "operator-authored" in line for line in result.lines
        )
        # The component_data slot materializes at install time — the front
        # is stateless, so it stays an empty, reserved dir.
        assert layout.data_dir("mcp-front").is_dir()
        # No binary leg ran (the front is a python child — no bin slot).
        assert not layout.binary_artifact_dir("mcp-front").exists()

    def test_per_component_verb_refuses_binary_flag(
        self,
        isolated_home: Path,
        front_manifest: Path,
        tmp_path: Path,  # type: ignore[no-untyped-def]
    ) -> None:
        artifact = tmp_path / "not-a-binary"
        artifact.write_bytes(b"\x7fELF fake")
        with pytest.raises(InstallError, match="applies to binary child"):
            install_component("mcp-front", binary=artifact)

    def test_full_install_materializes_the_venv_component_slot(
        self,
        isolated_home: Path,
        front_manifest: Path,
        fake_venv_installer: list[str],  # type: ignore[no-untyped-def]
    ) -> None:
        install()
        assert "mcp-front" in fake_venv_installer, "python child — the venv leg ran"
        venv_slot = layout.component_venv_dir("mcp-front")
        assert venv_slot.is_dir()
        mode = stat.S_IMODE(venv_slot.stat().st_mode)
        assert mode == 0o700, "the component venv slot is owner-private (layout §3.8)"
