"""Unit generation tests (service-lifecycle v1 §3.6, SL-17 + SL-18).

Positive: every directive of the MUST table is present with the exact
value; the hardening block is complete; ExecStart is one static line
without ``sh -c`` and without component argv; ExecStop is never
generated. Structural comparison against the specs example
(``examples/example-unit.service``) — not byte-equal, paths differ.
Negative: a downgrade attempt outside the filesystem allowlist raises.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from vesmaro.service import unitgen

HOME = Path("/home/testuser")
ENGINE_VENV = HOME / ".local/share/vesma/venv"
RW_PATHS = (HOME / ".local/state/vesma", HOME / ".cache/vesma", HOME / ".local/share/vesma")
RO_PATHS = (HOME / ".local/share/vesma/venv", HOME / ".local/share/vesma/venvs")


def _generate(downgraded: frozenset[str] = frozenset()) -> str:
    return unitgen.generate(
        home=HOME,
        engine_venv=ENGINE_VENV,
        read_write_paths=RW_PATHS,
        read_only_paths=RO_PATHS,
        downgraded=downgraded,
    )


SPECS_EXAMPLE = (
    Path(__file__).resolve().parent.parent.parent
    / "vesma-specs"
    / "specs"
    / "service-lifecycle"
    / "v1"
    / "examples"
    / "example-unit.service"
)


# ── SL-17: the MUST table, directive by directive ─────────────────────

_MUST_ACTIVE = {
    # [Service]
    "Type": "exec",
    "KillSignal": "SIGTERM",
    "KillMode": "mixed",
    "TimeoutStopSec": "90",
    "Restart": "on-failure",
    "RestartSec": "5s",
    "StandardOutput": "journal",
    "StandardError": "journal",
    # [Unit] (StartLimit* live in [Unit] per systemd >= 230, as the example)
    "StartLimitIntervalSec": "300",
    "StartLimitBurst": "5",
    # [Install]
    "WantedBy": "default.target",
}


class TestMustTable:
    def test_every_must_directive_exact(self) -> None:
        active, _ = unitgen.parse_unit(_generate())
        for name, value in _MUST_ACTIVE.items():
            assert active.get(name) == value, f"{name}= must be exactly {value!r}"

    def test_execstart_one_static_line_no_shell(self) -> None:
        text = _generate()
        active, _ = unitgen.parse_unit(text)
        exec_start = active["ExecStart"]
        # ONE static supervisor launch line — the engine venv's console
        # script, never sh -c, never component argv.
        assert exec_start == "%h/.local/share/vesma/venv/bin/vesma service run"
        assert "sh -c" not in exec_start
        assert "\n" not in exec_start
        assert "python" not in exec_start  # no component interpreter
        assert "board" not in exec_start and "metrics" not in exec_start
        # Exactly one ExecStart line in the whole text.
        assert len(re.findall(r"^ExecStart=", text, re.M)) == 1

    def test_no_execstop_generated(self) -> None:
        text = _generate()
        active, _ = unitgen.parse_unit(text)
        assert "ExecStop" not in active
        assert not re.search(r"^ExecStop=", text, re.M)  # not even commented
        # The absence is loud, not accidental.
        assert "ExecStop is intentionally NOT generated" in text

    def test_header_generated_do_not_edit(self) -> None:
        text = _generate()
        assert "do NOT edit by hand" in text
        assert "Regenerate with: vesma service install" in text
        assert f"# vesma:generator={unitgen.GENERATOR_VERSION}" in text


# ── SL-18: the hardening block ────────────────────────────────────────

_HARDENING = {
    "NoNewPrivileges": "true",
    "ProtectSystem": "strict",
    "ReadWritePaths": "%h/.local/state/vesma %h/.cache/vesma %h/.local/share/vesma",
    "ReadOnlyPaths": "%h/.local/share/vesma/venv %h/.local/share/vesma/venvs",
    "ProtectHome": "read-only",
    "PrivateTmp": "yes",
    "PrivateDevices": "yes",
    "ProtectKernelTunables": "yes",
    "ProtectKernelModules": "yes",
    "ProtectKernelLogs": "yes",
    "ProtectControlGroups": "yes",
    "RestrictSUIDSGID": "yes",
    "LockPersonality": "yes",
    "RestrictRealtime": "yes",
    "CapabilityBoundingSet": "",
    "RestrictAddressFamilies": "AF_UNIX AF_NETLINK AF_INET AF_INET6",
}


class TestHardening:
    def test_full_hardening_block_present(self) -> None:
        active, _ = unitgen.parse_unit(_generate())
        for name, value in _HARDENING.items():
            assert active.get(name) == value, f"{name}= must be exactly {value!r}"

    def test_systemcallfilter_commented_tier_b(self) -> None:
        text = _generate()
        active, commented = unitgen.parse_unit(text)
        assert "SystemCallFilter" not in active
        assert commented.get("SystemCallFilter") == "@system-service"
        assert "Tier B" in text  # loud explanation stays next to the line

    def test_memorydenywriteexecute_deliberately_absent(self) -> None:
        text = _generate()
        # Neither active nor a commented directive — only the prose
        # explanation may mention it.
        assert not re.search(r"^#?\s*MemoryDenyWriteExecute\s*=", text, re.M)
        assert "MemoryDenyWriteExecute is deliberately ABSENT" in text

    def test_readwritepaths_covers_layout_roots(self) -> None:
        active, _ = unitgen.parse_unit(_generate())
        covered = set(active["ReadWritePaths"].split())
        assert covered == {
            "%h/.local/state/vesma",
            "%h/.cache/vesma",
            "%h/.local/share/vesma",
        }

    def test_readonlypaths_covers_venv_trees(self) -> None:
        active, _ = unitgen.parse_unit(_generate())
        covered = set(active["ReadOnlyPaths"].split())
        assert covered == {
            "%h/.local/share/vesma/venv",
            "%h/.local/share/vesma/venvs",
        }


# ── Structural comparison with the specs example ──────────────────────


class TestSpecsExampleParity:
    @pytest.mark.skipif(not SPECS_EXAMPLE.exists(), reason="specs repo not checked out next to vesma")
    def test_structurally_equal_to_specs_example(self) -> None:
        """Same directive set and values; only paths and prose may differ."""
        example_text = SPECS_EXAMPLE.read_text(encoding="utf-8")
        example_active, example_commented = unitgen.parse_unit(example_text)
        ours_active, ours_commented = unitgen.parse_unit(_generate())

        for directive, value in {**_MUST_ACTIVE, **_HARDENING}.items():
            assert example_active.get(directive) == value, f"specs example {directive}"
            assert ours_active.get(directive) == value, f"generated {directive}"

        # Same *kind* of commented markers (Tier B filter, no ExecStop).
        assert "SystemCallFilter" in example_commented
        assert "SystemCallFilter" in ours_commented
        assert set(example_active) == set(ours_active) | {"Documentation"} or set(
            example_active
        ) == set(ours_active)
        # Section structure identical.
        for section in ("[Unit]", "[Service]", "[Install]"):
            assert section in example_text and section in _generate()


# ── Container downgrades ──────────────────────────────────────────────


class TestContainerDowngrades:
    def test_allowlisted_downgrade_comments_directives_and_marks(self) -> None:
        text = _generate(frozenset({"ProtectSystem", "PrivateTmp"}))
        active, commented = unitgen.parse_unit(text)
        assert "ProtectSystem" not in active
        assert "PrivateTmp" not in active
        assert commented["ProtectSystem"] == "strict"
        assert commented["PrivateTmp"] == "yes"
        # Everything else stays active.
        assert active["NoNewPrivileges"] == "true"
        assert active["KillMode"] == "mixed"
        # Machine-readable marker, sorted names.
        assert unitgen.parse_downgrade_marker(text) == ["PrivateTmp", "ProtectSystem"]
        # Loud explanation next to each downgraded directive.
        assert text.count("loud documented downgrade") == 2

    def test_full_filesystem_downgrade_set_is_exactly_the_allowlist(self) -> None:
        text = _generate(frozenset(unitgen.DOWNGRADE_ALLOWED))
        active, _ = unitgen.parse_unit(text)
        for name in unitgen.DOWNGRADE_ALLOWED:
            assert name not in active
        assert unitgen.parse_downgrade_marker(text) == sorted(unitgen.DOWNGRADE_ALLOWED)

    @pytest.mark.parametrize(
        "directive", ["Restart", "NoNewPrivileges", "SystemCallFilter", "KillMode"]
    )
    def test_downgrade_outside_allowlist_raises(self, directive: str) -> None:
        with pytest.raises(unitgen.UnitGenerationError, match=directive):
            _generate(frozenset({directive}))

    def test_downgrade_allowed_set_matches_contract(self) -> None:
        assert unitgen.DOWNGRADE_ALLOWED == frozenset(
            {"ProtectSystem", "ProtectHome", "ReadOnlyPaths", "ReadWritePaths", "PrivateTmp"}
        )


# ── Container detection ───────────────────────────────────────────────


class TestContainerDetect:
    def test_distrobox_env(self) -> None:
        assert unitgen.container_detect(env={"DISTROBOX_ENTER_ENV": "1"}, root=Path("/nonexistent"))

    def test_container_id_env(self) -> None:
        assert unitgen.container_detect(env={"CONTAINER_ID": "abc"}, root=Path("/nonexistent"))

    def test_empty_env_value_does_not_count(self) -> None:
        assert not unitgen.container_detect(env={"CONTAINER_ID": "  "}, root=Path("/nonexistent"))

    def test_dockerenv_file(self, tmp_path: Path) -> None:
        (tmp_path / ".dockerenv").touch()
        assert unitgen.container_detect(env={}, root=tmp_path)

    def test_podman_containerenv_file(self, tmp_path: Path) -> None:
        (tmp_path / "run" / ".containerenv").parent.mkdir(parents=True)
        (tmp_path / "run" / ".containerenv").touch()
        assert unitgen.container_detect(env={}, root=tmp_path)

    def test_bare_host(self, tmp_path: Path) -> None:
        assert not unitgen.container_detect(env={}, root=tmp_path)


# ── Path rendering + parsing helpers ─────────────────────────────────


class TestHelpers:
    def test_render_path_under_home(self) -> None:
        assert unitgen.render_path(HOME / ".cache/vesma", HOME) == "%h/.cache/vesma"

    def test_render_path_outside_home_is_absolute(self) -> None:
        assert unitgen.render_path(Path("/srv/vesma"), HOME) == "/srv/vesma"

    def test_render_path_home_itself(self) -> None:
        assert unitgen.render_path(HOME, HOME) == "%h"

    def test_parse_unit_roundtrip(self) -> None:
        text = _generate(frozenset({"ProtectHome"}))
        active, commented = unitgen.parse_unit(text)
        assert active["ProtectSystem"] == "strict"
        assert commented["ProtectHome"] == "read-only"
        assert unitgen.parse_downgrade_marker(text) == ["ProtectHome"]

    def test_parse_downgrade_marker_absent(self) -> None:
        assert unitgen.parse_downgrade_marker(_generate()) == []

    def test_parse_unit_ignores_prose_comments(self) -> None:
        _, commented = unitgen.parse_unit(_generate())
        # The prose explanations must not leak in as "commented directives".
        assert "ExecStop" not in commented
        assert "MemoryDenyWriteExecute" not in commented
