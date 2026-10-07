"""Component-manifest loader tests (contract specs/component-manifest/v1).

Positive: the engine's own bundled pack manifests (board, metrics) pass
the FULL check set. Negative: one inline fixture per error code, asserting
code + field path (CM §4: the receiver gets code, field path, fix hint).
"""

from __future__ import annotations

import copy
import json
from importlib.resources import files as resource_files
from pathlib import Path

import jsonschema
import pytest
import yaml

from vesma.service import board
from vesma.service.errors import (
    APIVERSION_UNSUPPORTED,
    CLAMP_VIOLATION,
    DEPENDS_CYCLE,
    DEPENDS_MISSING,
    DURATION_INVALID,
    ENV_FILE_UNSAFE,
    MANIFEST_SCHEMA_INVALID,
    NAME_DUPLICATED,
    PLACEHOLDER_UNKNOWN,
    SECRET_IN_VARS,
    SHELL_IN_ARGV,
    ManifestError,
)
from vesma.service.manifest import (
    SCHEMA_CONTRACT_VERSION,
    SCHEMA_VENDORED_PROVENANCE,
    SUPPORTED_API_VERSIONS,
    bundled_manifest_path,
    duration_to_ms,
    load_bundled_manifest,
    load_installation,
    load_manifest,
)

# ── Fixtures ──────────────────────────────────────────────────────────


def _in_process_doc() -> dict:
    return {
        "apiVersion": "vesma.component/v1",
        "kind": "in-process",
        "metadata": {
            "name": "sample",
            "version": "1.0.0",
            "tier": "optional",
            "description": "test component",
            "provenance": {
                "repo": "https://example.com/sample",
                "license": "Apache-2.0",
            },
        },
        "in_process": {
            "module": "sample.mod",
            "entrypoint": "create",
            "python": {"version": ">=3.11"},
        },
        "health": {
            "checker": "callback",
            "callback": "sample.mod:health",
            "startup": {"grace": "30s", "interval": "5s", "timeout": "2s"},
        },
        "config": {"schema_inline": {"type": "object"}},
    }


def _child_doc() -> dict:
    return {
        "apiVersion": "vesma.component/v1",
        "kind": "child-process",
        "metadata": {
            "name": "worker",
            "version": "0.1.0",
            "tier": "optional",
            "description": "test child",
            "provenance": {
                "repo": "https://example.com/worker",
                "license": "MIT",
            },
        },
        "launch": {
            "argv": ["{venv_bin}/python", "-m", "sample_worker"],
            "python": {"version": ">=3.11", "requirements": ["pydantic==2.14.2"]},
        },
        "health": {
            "checker": "http",
            "http": {
                "url": "http://127.0.0.1:9110/metrics",
                "interval": "5s",
                "timeout": "2s",
                "unhealthy_threshold": 3,
            },
        },
        "stop": {"signal": "SIGTERM", "grace_period": "10s"},
    }


def _write(tmp_path: Path, doc: dict, name: str = "sample.yaml") -> Path:
    target = tmp_path / name
    target.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return target


# ── Positive: bundled pack manifests pass the full check set ──────────


class TestBundledPackManifests:
    def test_board_manifest_full_check_set(self) -> None:
        manifest = load_bundled_manifest("board")
        assert manifest.kind == "in-process"
        assert manifest.name == "board"
        assert manifest.metadata.tier == "optional"
        assert manifest.metadata.provenance.license == "Apache-2.0"
        assert manifest.in_process is not None
        assert manifest.in_process.module == "vesma.service.board"
        assert manifest.in_process.entrypoint == "create_component"
        assert manifest.health is not None
        assert manifest.health.checker == "callback"
        assert manifest.health.callback == "vesma.service.board:health"
        assert manifest.health.startup is not None
        assert duration_to_ms(manifest.health.startup.grace) == 30_000
        assert manifest.stop is None  # forbidden for in-process
        assert manifest.config is not None
        assert manifest.config.schema_inline is not None
        assert manifest.config.schema_file is None

    def test_metrics_manifest_full_check_set(self) -> None:
        manifest = load_bundled_manifest("metrics")
        assert manifest.kind == "child-process"
        assert manifest.name == "metrics"
        assert manifest.launch is not None
        assert manifest.launch.argv[0] == "{venv_bin}/python"
        assert "vesma.metrics.exposer" in manifest.launch.argv
        assert manifest.launch.env is None  # no env vars, no env_file needed
        assert manifest.health is not None
        assert manifest.health.checker == "http"
        assert manifest.health.http is not None
        assert manifest.health.http.url == "http://127.0.0.1:9110/metrics"
        assert manifest.stop is not None
        assert manifest.stop.signal == "SIGTERM"
        assert manifest.in_process is None  # forbidden for child-process

    def test_bundled_manifests_together_form_valid_installation(self, tmp_path: Path) -> None:
        for name in ("board", "metrics"):
            source = bundled_manifest_path(name)
            (tmp_path / f"{name}.yaml").write_text(source.read_text(encoding="utf-8"))
        installation = load_installation(tmp_path)
        assert sorted(installation) == ["board", "metrics"]

    def test_board_module_path_resolves_and_imports(self) -> None:
        component = board.create_component()
        assert component.name == "board"
        assert board.health() == {"state": "healthy"}

    def test_vendored_schema_is_valid_2020_12(self) -> None:
        schema = json.loads(
            resource_files("vesma")
            .joinpath("service/schemas/component-manifest.schema.json")
            .read_text(encoding="utf-8")
        )
        jsonschema.Draft202012Validator.check_schema(schema)
        assert SCHEMA_CONTRACT_VERSION == "1.1.0"
        assert SUPPORTED_API_VERSIONS == ("vesma.component/v1",)

    def test_vendored_schema_is_byte_identical_vendor(self) -> None:
        # NO local edits — provenance lives in SCHEMA_VENDORED_PROVENANCE,
        # not in an inline $comment key, so a re-vendor diff against the
        # specs file stays empty (the specs file is not a runtime dep, so
        # byte-identity itself is asserted by the re-vendor procedure).
        schema = json.loads(
            resource_files("vesma")
            .joinpath("service/schemas/component-manifest.schema.json")
            .read_text(encoding="utf-8")
        )
        assert "$comment" not in schema
        assert "1.1.0" in SCHEMA_VENDORED_PROVENANCE
        assert "e006c30939154528806df098054426b80e67dc41" in SCHEMA_VENDORED_PROVENANCE


# ── Positive: well-formed documents load with full structure ─────────


class TestPositiveLoad:
    def test_in_process_manifest_roundtrip(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _in_process_doc())
        manifest = load_manifest(path)
        assert manifest.name == "sample"
        assert manifest.api_version == "vesma.component/v1"
        assert manifest.in_process is not None
        assert manifest.in_process.python_version == ">=3.11"
        assert manifest.depends_on == ()

    def test_child_process_manifest_roundtrip(self, tmp_path: Path) -> None:
        path = _write(tmp_path, _child_doc(), name="worker.yaml")
        manifest = load_manifest(path)
        assert manifest.launch is not None
        assert manifest.launch.argv == ["{venv_bin}/python", "-m", "sample_worker"]
        assert manifest.stop is not None

    def test_duration_to_ms(self) -> None:
        assert duration_to_ms("500ms") == 500
        assert duration_to_ms("10s") == 10_000
        assert duration_to_ms("5m") == 300_000
        assert duration_to_ms("1h") == 3_600_000
        for bad in ("1h30m", "10", "s", "-5s", "1.5s"):
            with pytest.raises(ManifestError) as exc:
                duration_to_ms(bad)
            assert exc.value.code == DURATION_INVALID


# ── Negative: one fixture per error code ─────────────────────────────


class TestNegativeByCode:
    def test_unknown_field(self, tmp_path: Path) -> None:
        doc = _in_process_doc()
        doc["harness"] = {"evil": True}
        with pytest.raises(ManifestError) as exc:
            load_manifest(_write(tmp_path, doc))
        assert exc.value.code == MANIFEST_SCHEMA_INVALID
        assert "harness" in exc.value.message

    def test_argv_string(self, tmp_path: Path) -> None:
        doc = _child_doc()
        doc["launch"]["argv"] = "python -m sample_worker"
        with pytest.raises(ManifestError) as exc:
            load_manifest(_write(tmp_path, doc, name="worker.yaml"))
        assert exc.value.code == MANIFEST_SCHEMA_INVALID
        assert exc.value.field_path == "$.launch.argv"

    def test_shell_in_argv_metacharacters(self, tmp_path: Path) -> None:
        doc = _child_doc()
        doc["launch"]["argv"] = ["{venv_bin}/python", "-m", "x; rm -rf /"]
        with pytest.raises(ManifestError) as exc:
            load_manifest(_write(tmp_path, doc, name="worker.yaml"))
        assert exc.value.code == SHELL_IN_ARGV
        assert exc.value.field_path == "$.launch.argv[2]"

    def test_shell_in_argv_invocation(self, tmp_path: Path) -> None:
        doc = _child_doc()
        doc["launch"]["argv"] = ["/bin/sh", "-c", "echo hi"]
        with pytest.raises(ManifestError) as exc:
            load_manifest(_write(tmp_path, doc, name="worker.yaml"))
        assert exc.value.code == SHELL_IN_ARGV

    def test_secret_in_vars(self, tmp_path: Path) -> None:
        doc = _child_doc()
        doc["launch"]["env"] = {"vars": {"API_TOKEN": "placeholder"}}
        with pytest.raises(ManifestError) as exc:
            load_manifest(_write(tmp_path, doc, name="worker.yaml"))
        assert exc.value.code == SECRET_IN_VARS
        assert exc.value.field_path == "$.launch.env.vars.API_TOKEN"

    def test_bad_duration(self, tmp_path: Path) -> None:
        doc = _in_process_doc()
        doc["health"]["startup"]["grace"] = "1h30m"
        with pytest.raises(ManifestError) as exc:
            load_manifest(_write(tmp_path, doc))
        assert exc.value.code == DURATION_INVALID
        assert exc.value.field_path == "$.health.startup.grace"

    def test_clamp_violation_base(self, tmp_path: Path) -> None:
        doc = _child_doc()
        doc["restart"] = {
            "backoff": {"base": "100ms", "max": "1m", "reset_after": "5m"},
            "window": {"attempts": 3, "per": "1h"},
        }
        with pytest.raises(ManifestError) as exc:
            load_manifest(_write(tmp_path, doc, name="worker.yaml"))
        assert exc.value.code == CLAMP_VIOLATION
        assert exc.value.field_path == "$.restart.backoff.base"

    def test_clamp_violation_max_and_attempts(self, tmp_path: Path) -> None:
        doc = _child_doc()
        doc["restart"] = {
            "backoff": {"base": "500ms", "max": "6m", "reset_after": "5m"},
            "window": {"attempts": 2, "per": "1h"},
        }
        with pytest.raises(ManifestError) as exc:
            load_manifest(_write(tmp_path, doc, name="worker.yaml"))
        assert exc.value.code == CLAMP_VIOLATION
        assert exc.value.field_path == "$.restart.backoff.max"

    def test_clamp_violation_attempts(self, tmp_path: Path) -> None:
        doc = _child_doc()
        doc["restart"] = {
            "backoff": {"base": "500ms", "max": "5m", "reset_after": "5m"},
            "window": {"attempts": 2, "per": "1h"},
        }
        with pytest.raises(ManifestError) as exc:
            load_manifest(_write(tmp_path, doc, name="worker.yaml"))
        assert exc.value.code == CLAMP_VIOLATION
        assert exc.value.field_path == "$.restart.window.attempts"

    def test_partial_backoff_section_reaches_schema_pass(self, tmp_path: Path) -> None:
        # Pre-schema clamp checks must not crash on a partial section
        # (missing required leaves are the schema pass's domain).
        doc = _child_doc()
        doc["restart"] = {"backoff": {"max": "1m"}}
        with pytest.raises(ManifestError) as exc:
            load_manifest(_write(tmp_path, doc, name="worker.yaml"))
        assert exc.value.code == MANIFEST_SCHEMA_INVALID

    def test_non_integer_attempts_is_schema_domain(self, tmp_path: Path) -> None:
        doc = _child_doc()
        doc["restart"] = {"window": {"attempts": "abc", "per": "1h"}}
        with pytest.raises(ManifestError) as exc:
            load_manifest(_write(tmp_path, doc, name="worker.yaml"))
        assert exc.value.code == MANIFEST_SCHEMA_INVALID

    def test_boolean_attempts_is_schema_domain_not_clamp(self, tmp_path: Path) -> None:
        doc = _child_doc()
        doc["restart"] = {"window": {"attempts": True, "per": "1h"}}
        with pytest.raises(ManifestError) as exc:
            load_manifest(_write(tmp_path, doc, name="worker.yaml"))
        assert exc.value.code == MANIFEST_SCHEMA_INVALID

    def test_reserved_name(self, tmp_path: Path) -> None:
        doc = _in_process_doc()
        doc["metadata"]["name"] = "venv"
        with pytest.raises(ManifestError) as exc:
            load_manifest(_write(tmp_path, doc))
        assert exc.value.code == MANIFEST_SCHEMA_INVALID
        assert exc.value.field_path == "$.metadata.name"

    def test_bad_name_pattern(self, tmp_path: Path) -> None:
        doc = _in_process_doc()
        doc["metadata"]["name"] = "Bad_Name"
        with pytest.raises(ManifestError) as exc:
            load_manifest(_write(tmp_path, doc))
        assert exc.value.code == MANIFEST_SCHEMA_INVALID
        assert exc.value.field_path == "$.metadata.name"

    def test_callback_on_child(self, tmp_path: Path) -> None:
        doc = _child_doc()
        doc["health"] = {
            "checker": "callback",
            "callback": "sample.mod:health",
        }
        with pytest.raises(ManifestError) as exc:
            load_manifest(_write(tmp_path, doc, name="worker.yaml"))
        assert exc.value.code == MANIFEST_SCHEMA_INVALID

    def test_stop_on_in_process(self, tmp_path: Path) -> None:
        doc = _in_process_doc()
        doc["stop"] = {"signal": "SIGTERM", "grace_period": "10s"}
        with pytest.raises(ManifestError) as exc:
            load_manifest(_write(tmp_path, doc))
        assert exc.value.code == MANIFEST_SCHEMA_INVALID

    def test_checker_mismatch(self, tmp_path: Path) -> None:
        doc = _child_doc()
        doc["health"]["tcp"] = {
            "host": "127.0.0.1",
            "port": 9110,
            "interval": "5s",
            "timeout": "2s",
            "unhealthy_threshold": 3,
        }
        with pytest.raises(ManifestError) as exc:
            load_manifest(_write(tmp_path, doc, name="worker.yaml"))
        assert exc.value.code == MANIFEST_SCHEMA_INVALID

    def test_core_without_artifact_sha(self, tmp_path: Path) -> None:
        doc = _in_process_doc()
        doc["metadata"]["tier"] = "core"
        with pytest.raises(ManifestError) as exc:
            load_manifest(_write(tmp_path, doc))
        assert exc.value.code == MANIFEST_SCHEMA_INVALID
        assert exc.value.field_path == "$.metadata.provenance.artifact_sha256"

    def test_env_file_inside_manifests_dir(self, tmp_path: Path) -> None:
        doc = _child_doc()
        doc["launch"]["env"] = {"env_file": "./secrets.env"}
        manifest_path = _write(tmp_path, doc, name="worker.yaml")
        with pytest.raises(ManifestError) as exc:
            load_manifest(manifest_path)
        assert exc.value.code == ENV_FILE_UNSAFE
        assert exc.value.field_path == "$.launch.env.env_file"
        assert exc.value.fix_hint is not None

    def test_placeholder_unknown(self, tmp_path: Path) -> None:
        doc = _child_doc()
        doc["launch"]["argv"] = ["{venv_bin}/python", "-m", "{home_dir}/run"]
        with pytest.raises(ManifestError) as exc:
            load_manifest(_write(tmp_path, doc, name="worker.yaml"))
        assert exc.value.code == PLACEHOLDER_UNKNOWN
        assert exc.value.field_path == "$.launch.argv[2]"

    def test_api_version_unsupported(self, tmp_path: Path) -> None:
        doc = _in_process_doc()
        doc["apiVersion"] = "vesma.component/v2"
        with pytest.raises(ManifestError) as exc:
            load_manifest(_write(tmp_path, doc))
        assert exc.value.code == APIVERSION_UNSUPPORTED
        assert "vesma.component/v1" in exc.value.message

    def test_missing_manifest_file(self, tmp_path: Path) -> None:
        with pytest.raises(ManifestError) as exc:
            load_manifest(tmp_path / "absent.yaml")
        assert exc.value.code == MANIFEST_SCHEMA_INVALID

    def test_depends_missing_single_manifest(self, tmp_path: Path) -> None:
        doc = _in_process_doc()
        doc["depends_on"] = ["ghost"]
        with pytest.raises(ManifestError) as exc:
            load_manifest(_write(tmp_path, doc), installation_names={"sample", "other"})
        assert exc.value.code == DEPENDS_MISSING


# ── Installation-level: dup / missing / cycle ────────────────────────


class TestInstallation:
    def test_empty_dir_loads_empty(self, tmp_path: Path) -> None:
        assert load_installation(tmp_path) == {}

    def test_missing_dir_fails_closed(self, tmp_path: Path) -> None:
        with pytest.raises(ManifestError):
            load_installation(tmp_path / "absent")

    def test_duplicate_name(self, tmp_path: Path) -> None:
        doc = _in_process_doc()
        _write(tmp_path, doc, name="first.yaml")
        _write(tmp_path, doc, name="second.yaml")
        with pytest.raises(ManifestError) as exc:
            load_installation(tmp_path)
        assert exc.value.code == NAME_DUPLICATED
        assert "first.yaml" in exc.value.message
        assert "second.yaml" in exc.value.message

    def test_missing_dependency_in_installation(self, tmp_path: Path) -> None:
        doc = _in_process_doc()
        doc["depends_on"] = ["ghost"]
        _write(tmp_path, doc, name="sample.yaml")
        with pytest.raises(ManifestError) as exc:
            load_installation(tmp_path)
        assert exc.value.code == DEPENDS_MISSING
        assert "ghost" in exc.value.message

    def test_cyclic_depends_on_reports_cycle(self, tmp_path: Path) -> None:
        doc_a = _in_process_doc()
        doc_a["metadata"]["name"] = "alpha"
        doc_a["depends_on"] = ["beta"]
        doc_b = _child_doc()
        doc_b["metadata"]["name"] = "beta"
        doc_b["depends_on"] = ["alpha"]
        _write(tmp_path, doc_a, name="alpha.yaml")
        _write(tmp_path, doc_b, name="beta.yaml")
        with pytest.raises(ManifestError) as exc:
            load_installation(tmp_path)
        assert exc.value.code == DEPENDS_CYCLE
        assert "alpha -> beta -> alpha" in exc.value.message

    def test_invalid_sibling_fails_closed_whole_installation(self, tmp_path: Path) -> None:
        good = _in_process_doc()
        good["metadata"]["name"] = "good"
        _write(tmp_path, good, name="good.yaml")
        (tmp_path / "broken.yaml").write_text("kind: in-process", encoding="utf-8")
        with pytest.raises(ManifestError):
            load_installation(tmp_path)

    def test_stray_non_manifest_file_fails_closed(self, tmp_path: Path) -> None:
        # Layout §3.5: EVERY file in the manifests directory must be a
        # valid manifest — a stray file is a load error, never a silent skip.
        doc = _in_process_doc()
        _write(tmp_path, doc, name="sample.yaml")
        (tmp_path / "notes.txt").write_text("operator notes", encoding="utf-8")
        with pytest.raises(ManifestError) as exc:
            load_installation(tmp_path)
        assert exc.value.code == MANIFEST_SCHEMA_INVALID
        assert "notes.txt" in exc.value.message

    def test_valid_pair_with_dependency_loads(self, tmp_path: Path) -> None:
        doc_a = _in_process_doc()
        doc_a["metadata"]["name"] = "alpha"
        doc_a["depends_on"] = ["beta"]
        doc_b = _child_doc()
        doc_b["metadata"]["name"] = "beta"
        _write(tmp_path, doc_a, name="alpha.yaml")
        _write(tmp_path, doc_b, name="beta.yaml")
        installation = load_installation(tmp_path)
        assert installation["alpha"].depends_on == ("beta",)
        assert installation["beta"].depends_on == ()

    def test_self_dependency_is_a_cycle(self, tmp_path: Path) -> None:
        doc = _in_process_doc()
        doc["depends_on"] = ["sample"]
        _write(tmp_path, doc, name="sample.yaml")
        with pytest.raises(ManifestError) as exc:
            load_installation(tmp_path)
        assert exc.value.code == DEPENDS_CYCLE


# ── Guard: mutation helpers never leak between tests ─────────────────


def test_fixture_builder_returns_fresh_documents() -> None:
    first = _in_process_doc()
    second = _in_process_doc()
    first["metadata"]["name"] = "mutated"
    assert copy.deepcopy(second)["metadata"]["name"] == "sample"
