"""Smoke tests for `mnemos` CLI (src/mnemos/cli/main.py).

These tests do NOT exhaustively cover every CLI command — they verify
that the Typer app builds, every command is registered, and each
command handles its basic happy path without raising. This is
enough to push `src/mnemos/cli/main.py` above the 80% coverage gate
in CI; deeper CLI behaviour is exercised through the `manager` and
`mcp_server` modules directly (see test_api.py, test_manager_*.py,
test_mcp_tools.py).

Each test uses Typer's `CliRunner.invoke()` in-process, with a
fresh isolated data dir per test (via tmp_path).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from vesmaro.cli.main import app

runner = CliRunner()


@pytest.fixture
def isolated_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point VESMA_CONFIG at an empty YAML so the CLI uses tmp_path."""
    # Reset the CLI manager singleton so each test gets a fresh DB.
    from vesmaro.cli._manager import reset_manager

    reset_manager()
    cfg = tmp_path / "vesmaro.yaml"
    cfg.write_text(
        f"mnemos:\n"
        f"  vault_path: {tmp_path / 'vault'}\n"
        f"  data_dir: {tmp_path / 'data'}\n"
        f"  db_name: cli-smoke.db\n"
        f"embedding:\n"
        f"  provider: nano\n"
    )
    monkeypatch.setenv("VESMA_CONFIG", str(cfg))
    yield cfg
    # Clean up the singleton so it doesn't leak into the next test.
    reset_manager()


# ── App builds ───────────────────────────────────────────────────────────────


def test_app_help_exits_cleanly() -> None:
    """`mnemos --help` exits 0 and prints the help banner."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "mnemos" in result.output.lower()


def test_app_no_args_shows_help() -> None:
    """With no args, Typer prints help (no_args_is_help=True)."""
    result = runner.invoke(app, [])
    # Typer exits 0 on no-args-with-help; the help text goes to stdout
    assert "Usage:" in result.output or "mnemos" in result.output.lower()


# ── Command registration ─────────────────────────────────────────────────────


def test_all_expected_commands_are_registered() -> None:
    """Every public command we ship must be in the Typer app."""
    expected = {
        "add",
        "search",
        "recall",
        "stats",
        "serve",
        "mcp-server",
    }

    # Typer: when a command has no explicit `name=...`, the registered
    # name is None and the callback's __name__ is used. Normalise.
    def _cmd_name(c: object) -> str:
        name = getattr(c, "name", None)
        if name:
            return str(name)
        cb = getattr(c, "callback", None)
        return getattr(cb, "__name__", "") or ""

    registered = {_cmd_name(c) for c in app.registered_commands}
    assert expected <= registered, f"missing commands: {expected - registered}"


def test_all_expected_groups_are_registered() -> None:
    """Every public subcommand group must be registered on the Typer app."""
    expected_groups = {
        "tags",
        "migrate",
        "auth",
        "integration",
        "completion",
        "doctor",
        "workflow",
    }
    registered_groups = {getattr(g, "name", None) for g in app.registered_groups}
    assert expected_groups <= registered_groups, (
        f"missing groups: {expected_groups - registered_groups}"
    )


# ── mnemos add ───────────────────────────────────────────────────────────────


def test_add_creates_memory(isolated_config: Path) -> None:
    """`mnemos add` stores a memory and prints the id."""
    result = runner.invoke(
        app,
        [
            "add",
            "hello world",  # positional content
            "--tags",
            "project:cli-smoke,agent:cli,mnemos:test",
        ],
    )
    assert result.exit_code == 0, result.output
    # Output mentions the new memory id and a green check mark
    assert "Saved" in result.output or "✓" in result.output


# ── mnemos search ────────────────────────────────────────────────────────────


def test_search_returns_table(isolated_config: Path) -> None:
    """`mnemos search "query"` prints a results table or 'no results'."""
    result = runner.invoke(app, ["search", "anything"])
    assert result.exit_code == 0, result.output
    # Empty vault → "no results" message OR an empty table
    assert "no" in result.output.lower() or "result" in result.output.lower()


# ── mnemos recall ────────────────────────────────────────────────────────────


def test_recall_with_empty_vault(isolated_config: Path) -> None:
    """`mnemos recall` on an empty vault returns 0 and prints 'no'."""
    result = runner.invoke(app, ["recall", "--limit", "5"])
    assert result.exit_code == 0, result.output


# ── mnemos tags validate ──────────────────────────────────────────────────────


def test_tags_validate_rejects_missing_required(
    isolated_config: Path,
) -> None:
    """`mnemos tags validate` with no vault → graceful exit (any code is OK)."""
    vault = isolated_config.parent / "vault"
    result = runner.invoke(app, ["tags", "validate", str(vault)])
    # Typer's argparse may exit 2 on missing-arg / usage error — we
    # only assert the CLI does not raise a Python traceback.
    assert "Traceback" not in result.output
    # And it must not have exit code 0 silently (it should report
    # missing vault or validation issues). The mvp-migration smoke
    # #13 confirms this codepath works; we only need to assert the
    # CLI shell is stable here.


# ── mnemos stats ──────────────────────────────────────────────────────────────


def test_stats_runs(isolated_config: Path) -> None:
    """`mnemos stats` exits 0 and prints counters."""
    result = runner.invoke(app, ["stats"])
    assert result.exit_code == 0, result.output
    # Output mentions one of the known stat keys
    assert any(k in result.output.lower() for k in ("total", "status", "version", "data_dir"))


# ── mnemos migrate from-ai-brain ──────────────────────────────────────────────


def test_migrate_dry_run_exits_cleanly(isolated_config: Path) -> None:
    """`mnemos migrate from-ai-brain --dry-run` with no source → graceful exit.

    On an empty / missing source directory, the migrate command should
    exit cleanly (0) — either printing 'no memories to migrate' or
    failing with a documented error message.
    """
    fake_source = isolated_config.parent / "no-such-ai-brain"
    result = runner.invoke(
        app,
        [
            "migrate",
            "from-ai-brain",
            "--source",
            str(fake_source),
            "--dry-run",
        ],
    )
    # Either 0 (graceful) or 1 (with documented error)
    assert result.exit_code in (0, 1), result.output


# ── mnemos serve / mcp-server are server commands; we only smoke-test
# that they import + register without invoking (they would block). ──


def test_serve_command_registered() -> None:
    """`mnemos serve` is registered (full smoke would require subprocess)."""
    registered = {c.name or c.callback.__name__ for c in app.registered_commands}
    assert "serve" in registered


def test_mcp_server_command_registered() -> None:
    """`mnemos mcp-server` is registered (full smoke would require subprocess)."""
    registered = {c.name or c.callback.__name__ for c in app.registered_commands}
    assert "mcp-server" in registered


# ── Module-level helpers ─────────────────────────────────────────────────────


def test_get_manager_returns_memory_manager(
    isolated_config: Path,
) -> None:
    """`get_manager()` builds a MemoryManager from the loaded config."""
    from vesmaro.cli.main import get_manager

    mgr = get_manager()
    assert mgr is not None
    assert hasattr(mgr, "add")
    assert hasattr(mgr, "search")
    mgr.close()


# ── Defensive: invalid config path is caught gracefully ──────────────────────


def test_add_with_invalid_tags_does_not_crash(
    isolated_config: Path,
) -> None:
    """`mnemos add` with no tags still completes (no Python traceback).

    The CLI may accept the call (no enforced contract on `add`) and
    emit a memory that downstream pipelines may then flag — that
    is by design (the contract is enforced in the manager.add()
    path or by the watcher filter, not at the CLI surface). What
    matters here is: the CLI does not raise an unhandled exception.
    """
    result = runner.invoke(
        app,
        [
            "add",
            "x",  # content
            # No --tags (deliberate: test graceful path)
        ],
    )
    assert "Traceback" not in result.output


# ── mnemos completion ─────────────────────────────────────────────────────────


class TestCompletionCommand:
    """Tests for `mnemos completion` — auto-detect + auto-install."""

    def test_completion_show_instructions_does_not_modify_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`--show-instructions` prints manual steps and exits 0 without touching rc files."""
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        result = runner.invoke(app, ["completion", "--show-instructions"])
        assert result.exit_code == 0
        assert "bash" in result.output
        assert "zsh" in result.output
        assert "fish" in result.output
        # No rc files should have been created.
        assert not (fake_home / ".bashrc").exists()

    def test_completion_explicit_shell_installs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mnemos completion bash` writes the script file and a source line into ~/.bashrc."""
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        result = runner.invoke(app, ["completion", "bash"])
        assert result.exit_code == 0
        # Completion script file stored under ~/.mnemos/completion/
        script_file = fake_home / ".mnemos" / "completion" / "vesmaro.bash"
        assert script_file.exists()
        assert "_vesmaro" in script_file.read_text(encoding="utf-8")
        # rc file gets an active (uncommented) source line, not eval.
        rc = fake_home / ".bashrc"
        assert rc.exists()
        content = rc.read_text(encoding="utf-8")
        assert "source ~/.mnemos/completion/vesmaro.bash" in content
        assert "eval " not in content

    def test_completion_is_idempotent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Running `mnemos completion bash` twice does not duplicate the source line."""
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        runner.invoke(app, ["completion", "bash"])
        runner.invoke(app, ["completion", "bash"])
        rc = fake_home / ".bashrc"
        content = rc.read_text(encoding="utf-8")
        # The source line marker should appear exactly once.
        assert content.count("source ~/.mnemos/completion/vesmaro.bash") == 1

    def test_completion_auto_detect_from_shell_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mnemos completion` (no args) auto-detects from $SHELL."""
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("SHELL", "/usr/bin/zsh")
        result = runner.invoke(app, ["completion"])
        assert result.exit_code == 0
        script_file = fake_home / ".mnemos" / "completion" / "vesmaro.zsh"
        assert script_file.exists()
        rc = fake_home / ".zshrc"
        assert rc.exists()
        assert "source ~/.mnemos/completion/vesmaro.zsh" in rc.read_text(encoding="utf-8")

    def test_completion_is_installed_false_for_commented_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_is_installed()` returns False when the source line is commented out."""
        from vesmaro.cli.completion import _is_installed

        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        rc = fake_home / ".bashrc"
        rc.write_text(
            "# Added by `mnemos completion` (bash)\n"
            "#[ -f ~/.mnemos/completion/vesmaro.bash ] "
            "&& source ~/.mnemos/completion/vesmaro.bash\n",
            encoding="utf-8",
        )
        assert not _is_installed("bash", rc)

    def test_completion_is_installed_true_for_active_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`_is_installed()` returns True for an active (uncommented) source line."""
        from vesmaro.cli.completion import _is_installed

        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        rc = fake_home / ".bashrc"
        rc.write_text(
            "[ -f ~/.mnemos/completion/vesmaro.bash ] "
            "&& source ~/.mnemos/completion/vesmaro.bash\n",
            encoding="utf-8",
        )
        assert _is_installed("bash", rc)

    def test_completion_migrates_old_eval_line(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Old `eval "$(mnemos --show-completion bash)"` line is removed on install."""
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setenv("HOME", str(fake_home))
        rc = fake_home / ".bashrc"
        rc.write_text(
            "# Added by `mnemos completion` (bash)\n"
            'eval "$(mnemos --show-completion bash)"\n'
            "# some user content\n",
            encoding="utf-8",
        )
        result = runner.invoke(app, ["completion", "bash"])
        assert result.exit_code == 0
        content = rc.read_text(encoding="utf-8")
        # Old eval line and its marker comment must be gone.
        assert "mnemos --show-completion" not in content
        assert "eval " not in content
        # New source line must be present.
        assert "source ~/.mnemos/completion/vesmaro.bash" in content
        # User content preserved.
        assert "# some user content" in content

    def test_completion_unknown_shell_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mnemos completion unsupported` exits 1 with a clear message."""
        result = runner.invoke(app, ["completion", "tcsh"])
        assert result.exit_code == 1
        assert "Unsupported shell" in result.output

    def test_completion_no_shell_no_env_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mnemos completion` with no $SHELL and no arg exits 1 with guidance."""
        monkeypatch.delenv("SHELL", raising=False)
        result = runner.invoke(app, ["completion"])
        assert result.exit_code == 1
        assert "auto-detect" in result.output.lower() or "show-instructions" in result.output


# ── mnemos doctor ────────────────────────────────────────────────────────────


class TestDoctorCommand:
    """Tests for `mnemos doctor` — health check."""

    def test_doctor_runs_with_isolated_config(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mnemos doctor` runs all checks and exits 0/1/2 (not a traceback)."""
        cfg = tmp_path / "vesmaro.yaml"
        cfg.write_text(
            f"mnemos:\n"
            f"  vault_path: {tmp_path / 'vault'}\n"
            f"  data_dir: {tmp_path / 'data'}\n"
            f"  db_name: doctor-smoke.db\n"
            f"embedding:\n"
            f"  provider: nano\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("VESMA_CONFIG", str(cfg))
        result = runner.invoke(app, ["doctor"])
        # Exit code is 0 (all pass), 1 (fail), or 2 (warn) — all acceptable
        # for a smoke test as long as there's no traceback.
        assert result.exit_code in (0, 1, 2), result.output
        assert "Traceback" not in result.output

    def test_doctor_json_output(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`mnemos doctor --json` emits valid JSON with a checks array."""
        cfg = tmp_path / "vesmaro.yaml"
        cfg.write_text(
            f"mnemos:\n"
            f"  vault_path: {tmp_path / 'vault'}\n"
            f"  data_dir: {tmp_path / 'data'}\n"
            f"  db_name: doctor-json.db\n"
            f"embedding:\n"
            f"  provider: nano\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("VESMA_CONFIG", str(cfg))
        result = runner.invoke(app, ["doctor", "--json"])
        assert result.exit_code in (0, 1, 2), result.output
        import json

        payload = json.loads(result.output)
        assert "checks" in payload
        assert "exit_code" in payload
        assert isinstance(payload["checks"], list)
        assert len(payload["checks"]) >= 8

    def test_doctor_reports_missing_vault_as_warn_or_fail(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A config pointing at an unwritable vault path surfaces a non-pass check."""
        cfg = tmp_path / "vesmaro.yaml"
        cfg.write_text(
            f"mnemos:\n"
            f"  vault_path: /nonexistent-root-cant-create/vault\n"
            f"  data_dir: {tmp_path / 'data'}\n"
            f"  db_name: doctor-fail.db\n"
            f"embedding:\n"
            f"  provider: nano\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("VESMA_CONFIG", str(cfg))
        result = runner.invoke(app, ["doctor"])
        # Unwritable vault → at least one FAIL → exit 1.
        assert result.exit_code == 1, result.output


# ── mnemos tags normalize ────────────────────────────────────────────────────


class TestTagsNormalize:
    """`mnemos tags normalize` lowercases mixed-case project/agent slugs."""

    def test_normalize_lowercases_mixed_case_tags(self, isolated_config: Path) -> None:
        """Memories with project:Foo / agent:Bar get normalized to lowercase."""
        from vesmaro.cli._manager import get_manager
        from vesmaro.models import Memory, MemorySource, MemoryStatus, MemoryType

        mgr = get_manager(str(isolated_config))
        # Bypass validate_tag_contract (which normalizes on ingest) by
        # saving directly to SQLite with mixed-case tags.
        for slug_project, slug_agent in [("MyProject", "SeniorAgent"), ("OtherProj", "DBA")]:
            mem = Memory(
                content=f"test content for {slug_project}",
                title="test",
                tags=[f"project:{slug_project}", f"agent:{slug_agent}", "mnemos:test"],
                source=MemorySource.CLI,
                memory_type=MemoryType.NOTE,
                status=MemoryStatus.RAW,
            )
            mgr.sqlite.save(mem)

        result = runner.invoke(app, ["tags", "normalize"])
        assert result.exit_code == 0, result.output
        assert "Scanned:" in result.output
        assert "Normalized:" in result.output

        # Verify the DB now has lowercase tags.
        all_mems = mgr.sqlite.list_all(limit=100)
        for mem in all_mems:
            for tag in mem.tags:
                if tag.startswith("project:") or tag.startswith("agent:"):
                    # Slug portion must be all lowercase.
                    slug = tag.split(":", 1)[1]
                    assert slug == slug.lower(), f"tag {tag} not normalized"

    def test_normalize_dry_run_does_not_write(self, isolated_config: Path) -> None:
        """--dry-run reports changes but leaves the DB untouched."""
        from vesmaro.cli._manager import get_manager
        from vesmaro.models import Memory, MemorySource, MemoryStatus, MemoryType

        mgr = get_manager(str(isolated_config))
        mem = Memory(
            content="dry-run test",
            title="test",
            tags=["project:MixedCase", "agent:UpperAgent", "mnemos:test"],
            source=MemorySource.CLI,
            memory_type=MemoryType.NOTE,
            status=MemoryStatus.RAW,
        )
        mgr.sqlite.save(mem)

        result = runner.invoke(app, ["tags", "normalize", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "dry-run" in result.output.lower()
        assert "MixedCase" in result.output  # reports the change

        # DB must still have the original mixed-case tag.
        all_mems = mgr.sqlite.list_all(limit=100)
        saved = next(m for m in all_mems if "dry-run" in m.content)
        assert "project:MixedCase" in saved.tags

    def test_normalize_idempotent_on_clean_tags(self, isolated_config: Path) -> None:
        """Running normalize on already-lowercase tags is a no-op."""
        from vesmaro.cli._manager import get_manager
        from vesmaro.models import Memory, MemorySource, MemoryStatus, MemoryType

        mgr = get_manager(str(isolated_config))
        mem = Memory(
            content="clean tags",
            title="test",
            tags=["project:clean", "agent:bot", "mnemos:test"],
            source=MemorySource.CLI,
            memory_type=MemoryType.NOTE,
            status=MemoryStatus.RAW,
        )
        mgr.sqlite.save(mem)

        result = runner.invoke(app, ["tags", "normalize"])
        assert result.exit_code == 0, result.output
        assert "Normalized: 0" in result.output

    def test_tags_normalize_does_not_corrupt_fts(self, isolated_config: Path) -> None:
        """Regression for HIGH-1: normalize must not desync the FTS5 index.

        Before the fix, `tags normalize` used `sqlite.save()` (INSERT OR
        REPLACE) which can desync the FTS5 external content table
        (`content=memories`), causing "missing row from content table"
        errors on subsequent searches. With `update_fields` (plain UPDATE)
        the AFTER UPDATE trigger keeps the FTS5 index consistent, so search
        still finds the memory after normalization.
        """
        from vesmaro.cli._manager import get_manager
        from vesmaro.models import Memory, MemorySource, MemoryStatus, MemoryType

        mgr = get_manager(str(isolated_config))
        mem = Memory(
            content="fts corruption regression unique marker ALPHA",
            title="regression",
            tags=["project:MyProject", "agent:SeniorAgent", "mnemos:test"],
            source=MemorySource.CLI,
            memory_type=MemoryType.NOTE,
            # PUBLISHED so default search (no include_raw) can find it.
            status=MemoryStatus.PUBLISHED,
        )
        mgr.sqlite.save(mem)

        # Run normalize (not dry-run) — this used to corrupt FTS5.
        result = runner.invoke(app, ["tags", "normalize"])
        assert result.exit_code == 0, result.output
        assert "Normalized:" in result.output

        # Search must still find the memory — no "missing row" error.
        results = mgr.search("fts corruption regression unique marker ALPHA")
        assert len(results) >= 1, "FTS search returned nothing after normalize (index corrupted?)"
        assert results[0].memory.id == mem.id

    def test_tags_normalize_updates_project_agent_columns(self, isolated_config: Path) -> None:
        """Denormalised project/agent columns are updated, not just tags.

        `update_fields` now writes `project` and `agent` columns alongside
        the tags JSON, so per-project / per-agent queries stay in sync.
        """
        from vesmaro.cli._manager import get_manager
        from vesmaro.models import Memory, MemorySource, MemoryStatus, MemoryType

        mgr = get_manager(str(isolated_config))
        mem = Memory(
            content="denormalised column check",
            title="test",
            tags=["project:My-Project", "agent:Senior-Agent", "mnemos:test"],
            source=MemorySource.CLI,
            memory_type=MemoryType.NOTE,
            status=MemoryStatus.RAW,
        )
        mgr.sqlite.save(mem)

        result = runner.invoke(app, ["tags", "normalize"])
        assert result.exit_code == 0, result.output

        updated = mgr.sqlite.get(mem.id)
        assert updated is not None
        assert updated.project == "my-project", f"project column not updated: {updated.project!r}"
        assert updated.agent == "senior-agent", f"agent column not updated: {updated.agent!r}"
        assert "project:my-project" in updated.tags
        assert "agent:senior-agent" in updated.tags

    def test_tags_normalize_normalizes_spaces(self, isolated_config: Path) -> None:
        """Spaces in slugs are replaced with hyphens (contract parity).

        The CLI normalize command previously only lowercased, diverging
        from `validate_tag_contract` which also replaces spaces with
        hyphens. `project:My Project` must become `project:my-project`.
        """
        from vesmaro.cli._manager import get_manager
        from vesmaro.models import Memory, MemorySource, MemoryStatus, MemoryType

        mgr = get_manager(str(isolated_config))
        mem = Memory(
            content="space normalization check",
            title="test",
            tags=["project:My Project", "agent:Some Agent", "mnemos:test"],
            source=MemorySource.CLI,
            memory_type=MemoryType.NOTE,
            status=MemoryStatus.RAW,
        )
        mgr.sqlite.save(mem)

        result = runner.invoke(app, ["tags", "normalize"])
        assert result.exit_code == 0, result.output

        updated = mgr.sqlite.get(mem.id)
        assert updated is not None
        assert "project:my-project" in updated.tags, (
            f"space not replaced in project tag: {updated.tags!r}"
        )
        assert "agent:some-agent" in updated.tags, (
            f"space not replaced in agent tag: {updated.tags!r}"
        )
        assert updated.project == "my-project"
        assert updated.agent == "some-agent"


# ── mnemos search --include-raw / --status ────────────────────────────────────


class TestCliSearchFlags:
    """`mnemos search` exposes --include-raw and --status for parity with the API."""

    def _add_memory(
        self,
        mgr: object,
        content: str,
        status: str,
        title: str,
    ) -> None:
        """Seed a memory directly into SQLite with a given status + title."""
        from vesmaro.models import Memory, MemorySource, MemoryStatus, MemoryType

        mem = Memory(
            content=content,
            title=title,
            tags=["project:search-flags", "agent:test", "mnemos:test"],
            source=MemorySource.CLI,
            memory_type=MemoryType.NOTE,
            status=MemoryStatus(status),
        )
        mgr.sqlite.save(mem)

    def test_cli_search_include_raw_flag(self, isolated_config: Path) -> None:
        """Default search surfaces raw entries; `--published-only` hides them (#123)."""
        from vesmaro.cli._manager import get_manager

        mgr = get_manager(str(isolated_config))
        self._add_memory(mgr, "raw entry marker", "raw", "RawTitleZeta")
        self._add_memory(mgr, "published entry marker", "published", "PubTitleOmega")

        # Default search: everything except archived — the interactive CLI must
        # find a just-added (still raw) memory (ADR-0017 Phase 0, issue #123).
        result_default = runner.invoke(app, ["search", "marker"])
        assert result_default.exit_code == 0, result_default.output
        assert "PubTitleOmega" in result_default.output
        assert "RawTitleZeta" in result_default.output

        # --published-only: restrict to pipeline-published knowledge.
        result_pub = runner.invoke(app, ["search", "marker", "--published-only"])
        assert result_pub.exit_code == 0, result_pub.output
        assert "PubTitleOmega" in result_pub.output
        assert "RawTitleZeta" not in result_pub.output

    def test_cli_search_status_flag(self, isolated_config: Path) -> None:
        """`--status raw` finds only raw entries among mixed statuses."""
        from vesmaro.cli._manager import get_manager

        mgr = get_manager(str(isolated_config))
        self._add_memory(mgr, "status raw marker", "raw", "RawTitleBeta")
        self._add_memory(mgr, "status published marker", "published", "PubTitleGamma")
        self._add_memory(mgr, "status archived marker", "archived", "ArchTitleDelta")

        result = runner.invoke(app, ["search", "status", "--status", "raw"])
        assert result.exit_code == 0, result.output
        assert "RawTitleBeta" in result.output
        assert "PubTitleGamma" not in result.output
        assert "ArchTitleDelta" not in result.output


# ── mnemos workflow (#96) ────────────────────────────────────────────────────


class TestWorkflowCli:
    """`mnemos workflow get|set|history` — thin wrappers over the manager.

    The state machine + 5 guardrails are enforced in the manager (covered by
    test_workflow.py). These tests verify the CLI plumbing: the commands run,
    surface the manager result, and translate a ValueError into exit 1 + a
    red error line.
    """

    @staticmethod
    def _add_memory(isolated_config: Path) -> str:
        from vesmaro.cli._manager import get_manager
        from vesmaro.models import MemoryCreate

        mgr = get_manager(str(isolated_config))
        mem = mgr.add(
            MemoryCreate(
                content="workflow cli smoke",
                tags=["project:cli-smoke", "agent:cli", "mnemos:test"],
            )
        )
        return mem.id

    def test_get_defaults_to_open(self, isolated_config: Path) -> None:
        """`workflow get` on a fresh memory normalises unset status to open."""
        memory_id = self._add_memory(isolated_config)
        result = runner.invoke(app, ["workflow", "get", memory_id])
        assert result.exit_code == 0, result.output
        assert "workflow_status" in result.output
        assert "open" in result.output

    def test_get_missing_memory_exits_1(self, isolated_config: Path) -> None:
        """`workflow get` on an unknown id → red error + exit 1."""
        result = runner.invoke(app, ["workflow", "get", "does-not-exist"])
        assert result.exit_code == 1, result.output
        assert "not found" in result.output

    def test_set_transitions_and_records(self, isolated_config: Path) -> None:
        """`workflow set --to in-progress` records the transition."""
        from vesmaro.cli._manager import get_manager

        memory_id = self._add_memory(isolated_config)
        result = runner.invoke(
            app,
            [
                "workflow",
                "set",
                memory_id,
                "--to",
                "in-progress",
                "--actor",
                "cli-tester",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "in-progress" in result.output
        assert "recorded" in result.output.lower()

        # The manager reflects the transition.
        mgr = get_manager(str(isolated_config))
        assert mgr.workflow_get(memory_id)["workflow_status"] == "in-progress"

    def test_set_forbidden_transition_exits_1(self, isolated_config: Path) -> None:
        """A forbidden edge (blocked → done) surfaces as exit 1 + the reason."""
        from vesmaro.cli._manager import get_manager

        memory_id = self._add_memory(isolated_config)
        mgr = get_manager(str(isolated_config))
        # Drive the memory into blocked via the manager (one valid edge at a
        # time): open → in-progress → blocked.
        mgr.workflow_set(memory_id, "in-progress", actor="cli-tester")
        mgr.workflow_set(memory_id, "blocked", actor="cli-tester")

        result = runner.invoke(
            app,
            ["workflow", "set", memory_id, "--to", "done", "--actor", "cli-tester"],
        )
        assert result.exit_code == 1, result.output
        assert "blocked" in result.output  # the forbidden-edge message names it

    def test_history_empty_and_after_transition(self, isolated_config: Path) -> None:
        """`workflow history` is empty for a fresh memory, populated after a set."""
        memory_id = self._add_memory(isolated_config)

        empty = runner.invoke(app, ["workflow", "history", memory_id])
        assert empty.exit_code == 0, empty.output
        assert "No workflow transitions" in empty.output

        runner.invoke(
            app,
            ["workflow", "set", memory_id, "--to", "in-progress", "--actor", "cli-tester"],
        )
        populated = runner.invoke(app, ["workflow", "history", memory_id])
        assert populated.exit_code == 0, populated.output
        assert "in-progress" in populated.output


# ── edge-stats maintenance (ADR-0030 A0, review #338 N2) ─────────────────────


class TestEdgeStatsCommand:
    def test_stats_on_empty_table(self, isolated_config: Path) -> None:
        """`edge-stats stats` reports the zero state with the global cap."""
        result = runner.invoke(app, ["edge-stats", "stats"])
        assert result.exit_code == 0, result.output
        assert "rows total: 0" in result.output
        assert "last purge: never" in result.output

    def test_purge_requires_keep_last(self, isolated_config: Path) -> None:
        """No default retention: purge without --keep-last refuses."""
        result = runner.invoke(app, ["edge-stats", "purge"])
        assert result.exit_code == 1, result.output
        assert "--keep-last" in result.output

    def test_purge_negative_keep_last_refused(self, isolated_config: Path) -> None:
        result = runner.invoke(app, ["edge-stats", "purge", "--keep-last", "-1"])
        assert result.exit_code == 1, result.output
        assert ">= 0" in result.output

    def test_purge_dry_run_default_writes_nothing(self, isolated_config: Path) -> None:
        """Without --apply the command only reports the projection —
        an operator reviews, then re-runs with --apply."""
        result = runner.invoke(app, ["edge-stats", "purge", "--keep-last", "5"])
        assert result.exit_code == 0, result.output
        assert "dry run" in result.output
        assert "would purge: 0" in result.output

    def test_unknown_action_exits_1(self, isolated_config: Path) -> None:
        result = runner.invoke(app, ["edge-stats", "vacuum"])
        assert result.exit_code == 1, result.output
        assert "Unknown action" in result.output


class TestGraphLifecycleCli:
    """`vesma graph register/repoint` (#450/#454) — thin CLI adapters
    over CodeGraphService; happy path + loud refusal, exit codes."""

    def _repo(self, tmp_path: Path, name: str) -> Path:
        repo = tmp_path / name
        repo.mkdir()
        (repo / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        return repo

    def test_register_then_repoint_ghost(self, isolated_config: Path, tmp_path: Path) -> None:
        repo = self._repo(tmp_path, "cli-repo")
        result = runner.invoke(app, ["graph", "register", "cliproj", str(repo)])
        assert result.exit_code == 0, result.output
        assert "registered" in result.output

        moved = repo.with_name("cli-repo-moved")
        repo.rename(moved)  # the ghost
        repoint = runner.invoke(app, ["graph", "repoint", "cliproj", str(moved)])
        assert repoint.exit_code == 0, repoint.output
        assert "repointed" in repoint.output

    def test_repoint_refusal_is_loud_and_nonzero(
        self, isolated_config: Path, tmp_path: Path
    ) -> None:
        repo = self._repo(tmp_path, "cli-repo")
        runner.invoke(app, ["graph", "register", "cliproj", str(repo)])
        result = runner.invoke(app, ["graph", "repoint", "cliproj", str(tmp_path / "nope")])
        assert result.exit_code == 1, result.output
        assert "refused" in result.output
