"""Wave W-A — custom completion engine: ``vesma __complete`` + installer + doctor.

Covers the three surfaces shipped in this wave:

* the ``__complete`` engine contract (value<TAB>description output, live
  typer/click introspection — no hardcoded tables, hidden commands excluded,
  exit 0 always, malformed input → empty output);
* the rewritten installer (prog binding for ``vesma`` + legacy aliases,
  script content pins per shell, idempotency, legacy rc migration, the
  stale-line false-positive regression);
* the doctor "Completion" check (pass + warn states).

All filesystem-touching tests run against a fake HOME (tmp_path +
monkeypatched ``Path.home``/``$HOME``); ``shutil.which`` is pinned so the
alias set does not depend on the machine running the suite.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import vesma.cli.completion as completion_mod
from vesma.cli.complete_cmd import get_completions
from vesma.cli.completion import (
    _canonical_source_line,
    _completion_file_path,
    _fish_completions_file,
    _is_installed,
    _primary_prog_name,
    _prog_names,
    _rc_path,
    _remove_old_completion_entries,
)
from vesma.cli.main import app

runner = CliRunner()

CANONICAL_BASH = "[ -f ~/.mnemos/completion/vesma.bash ] && source ~/.mnemos/completion/vesma.bash"


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Fake HOME with a pinned ``shutil.which`` (no mnemos binary — hermetic)."""
    home = tmp_path / "fakehome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    return home


def run_complete(*args: str) -> str:
    """Invoke ``vesma __complete <args>`` and assert the exit-0 contract."""
    result = runner.invoke(app, ["__complete", *args])
    assert result.exit_code == 0, f"__complete must always exit 0, got {result.exit_code}"
    return result.output


# ── __complete engine ─────────────────────────────────────────────────────────


class TestCompleteEngine:
    def test_top_level_commands_with_descriptions(self, fake_home: Path) -> None:
        out = run_complete("", "0")
        lines = out.splitlines()
        assert lines, "top-level completion must not be empty"
        assert "add\tAdd a new memory entry (quick-capture)." in lines
        assert "search\tSearch long-term memory (hybrid FTS + vector)." in lines
        # Contract shape: value<TAB>description, no headers, no Rich markup.
        for line in lines:
            assert "\x1b[" not in line
            value = line.partition("\t")[0]
            assert value and not value.startswith(" ")

    def test_hidden_command_excluded_but_exact_match_works(self, fake_home: Path) -> None:
        out = run_complete("", "0")
        assert "__complete" not in out  # hidden plumbing is never suggested
        assert run_complete("__c", "0") == ""  # not even by prefix
        # Exact-match path: the hidden name itself completes (with description).
        exact = run_complete("__complete", "0")
        assert exact.startswith("__complete\t")

    def test_nested_subcommands_two_levels(self, fake_home: Path) -> None:
        out = run_complete("auth", "token", "", "2")
        lines = out.splitlines()
        assert "create\tMint a new bearer token and print it ONCE." in lines
        assert any(line.startswith("list\t") for line in lines)
        assert any(line.startswith("revoke\t") for line in lines)

    def test_nested_subcommand_prefix_under_group(self, fake_home: Path) -> None:
        out = run_complete("tags", "va", "1")
        assert out.startswith("validate\t")

    def test_option_name_completion(self, fake_home: Path) -> None:
        out = run_complete("add", "-", "1")
        long_opts = {line.split("\t")[0] for line in out.splitlines()}
        assert {"--title", "--tags", "--type", "--source", "--dry-run"} <= long_opts
        # Prefix filtering narrows to the single match, description intact.
        assert run_complete("add", "--ti", "1") == "--title\n"

    def test_enum_option_value_completion(self, fake_home: Path) -> None:
        out = run_complete("add", "--source", "", "2")
        values = [line.split("\t")[0] for line in out.splitlines()]
        assert {"manual", "web", "cli", "obsidian"} <= set(values)
        # Fragment filters the choice values.
        assert run_complete("add", "--source", "we", "2") == "web\n"

    def test_option_value_equals_prefix_form(self, fake_home: Path) -> None:
        assert run_complete("add", "--type=fa", "1") == "fact\n"
        assert run_complete("add", "--source=", "1").startswith("manual\n")

    def test_options_at_nested_depth(self, fake_home: Path) -> None:
        out = run_complete("auth", "token", "create", "-", "3")
        long_opts = {line.split("\t")[0] for line in out.splitlines()}
        assert "--name" in long_opts
        assert "--no-totp" in long_opts

    def test_fish_shape_index_equals_word_count(self, fake_home: Path) -> None:
        # fish emits no empty-token argument: index == len(words) means an
        # empty fragment at the end.
        out = run_complete("tags", "1")
        values = [line.split("\t")[0] for line in out.splitlines()]
        assert {"validate", "normalize", "rename"} <= set(values)

    def test_malformed_input_exit_zero_empty_output(self, fake_home: Path) -> None:
        assert run_complete() == ""  # no args at all
        assert run_complete("x") == ""  # last arg is not an index
        assert run_complete("add", "-1") == ""  # negative index is not digits

    def test_no_match_empty_output(self, fake_home: Path) -> None:
        assert run_complete("zzz", "0") == ""
        assert run_complete("tags", "zz", "1") == ""

    def test_direct_engine_call_matches_cli(self, fake_home: Path) -> None:
        direct = get_completions(["tags", "va"], 1)
        assert (
            "validate",
            "Validate tag contract across an existing vault. Reports non-conformant entries.",
        ) in direct
        # The engine never raises even on nonsense input.
        assert get_completions([], 7) == []


# ── Prog binding ──────────────────────────────────────────────────────────────


class TestProgBinding:
    def test_primary_defaults_to_vesma_outside_real_binary(self) -> None:
        # Under pytest, argv[0] is not a known Vesma binary → brand default.
        assert _primary_prog_name() == "vesma"

    def test_primary_from_argv0_when_real_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["/usr/local/bin/vesmaro", "completion", "bash"])
        assert _primary_prog_name() == "vesmaro"

    def test_prog_names_vesmaro_always_mnemos_only_when_on_path(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: None)
        assert _prog_names() == ["vesma", "vesmaro"]
        monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
        assert _prog_names() == ["vesma", "vesmaro", "mnemos"]


# ── Installer: script content pins ────────────────────────────────────────────


class TestScriptContent:
    def test_bash_script_binds_primary_and_alias_and_engine(self, fake_home: Path) -> None:
        result = runner.invoke(app, ["completion", "bash"])
        assert result.exit_code == 0
        script = _completion_file_path("bash").read_text(encoding="utf-8")
        assert "_vesma()" in script
        assert "__complete" in script
        assert "COMP_WORDS" in script and "COMP_CWORD" in script
        # Both the primary binary and the legacy alias are bound.
        assert "complete -F _vesma vesma vesmaro" in script

    def test_zsh_script_uses_describe_and_binds_aliases(self, fake_home: Path) -> None:
        result = runner.invoke(app, ["completion", "zsh"])
        assert result.exit_code == 0
        script = _completion_file_path("zsh").read_text(encoding="utf-8")
        assert script.startswith("#compdef vesma vesmaro")
        assert "_describe" in script  # zsh SHOWS the descriptions
        assert "__complete" in script
        assert "compdef _vesma vesma vesmaro" in script

    def test_fish_script_per_name_with_native_pairs(self, fake_home: Path) -> None:
        result = runner.invoke(app, ["completion", "fish"])
        assert result.exit_code == 0
        primary = _fish_completions_file("vesma")
        assert primary.exists()
        text = primary.read_text(encoding="utf-8")
        assert "complete -c vesma -f -a '(__vesma_complete)'" in text
        assert "complete -c vesmaro -f -a '(__vesma_complete)'" in text
        assert "__complete" in text
        # Legacy alias gets its own auto-sourced file.
        assert _fish_completions_file("vesmaro").exists()

    def test_scripts_rewritten_every_run(self, fake_home: Path) -> None:
        runner.invoke(app, ["completion", "bash"])
        script = _completion_file_path("bash")
        script.write_text("# stale", encoding="utf-8")
        runner.invoke(app, ["completion", "bash"])
        assert "# stale" not in script.read_text(encoding="utf-8")


# ── Installer: rc-file semantics ──────────────────────────────────────────────


class TestInstallerRc:
    def test_install_is_idempotent_exactly_one_canonical_line(self, fake_home: Path) -> None:
        runner.invoke(app, ["completion", "bash"])
        runner.invoke(app, ["completion", "bash"])
        rc = fake_home / ".bashrc"
        content = rc.read_text(encoding="utf-8")
        assert content.count(CANONICAL_BASH) == 1
        assert content.count("# Added by 'vesma completion' (bash)") == 1

    def test_legacy_forms_migrated_and_canonical_added(self, fake_home: Path) -> None:
        rc = fake_home / ".bashrc"
        rc.write_text(
            "export EDITOR=vim\n"
            'eval "$(mnemos --show-completion bash)"\n'
            "# Added by `vesma completion` (bash)\n"
            "if [ -f ~/.mnemos/completion/mnemos.bash ]; then "
            "source ~/.mnemos/completion/mnemos.bash; fi\n"
            "[ -f ~/.mnemos/completion/vesmaro.bash ] "
            "&& source ~/.mnemos/completion/vesmaro.bash\n"
            "[ -f ~/.mnemos/completion/vesma.zsh ] "
            "&& source ~/.mnemos/completion/vesma.zsh\n",
            encoding="utf-8",
        )
        result = runner.invoke(app, ["completion", "bash"])
        assert result.exit_code == 0
        content = rc.read_text(encoding="utf-8")
        # Unrelated user content survives.
        assert "export EDITOR=vim" in content
        # Every legacy form is gone.
        for legacy in (
            "--show-completion",
            "mnemos.bash",
            "vesmaro.bash",
            "vesma.zsh",
            "if [ -f",
        ):
            assert legacy not in content, f"legacy form not migrated: {legacy}"
        # Exactly one canonical line.
        assert content.count(CANONICAL_BASH) == 1

    def test_duplicate_canonical_lines_collapse_to_one(self, fake_home: Path) -> None:
        rc = fake_home / ".bashrc"
        rc.write_text(f"{CANONICAL_BASH}\n{CANONICAL_BASH}\n", encoding="utf-8")
        runner.invoke(app, ["completion", "bash"])
        content = rc.read_text(encoding="utf-8")
        assert content.count(CANONICAL_BASH) == 1

    def test_stale_vesmaro_line_is_not_installed_false_positive_regression(
        self, fake_home: Path
    ) -> None:
        """Pre-rebrand `source ~/.mnemos/completion/vesmaro.bash` must NOT count
        as installed — the historical self-healing-never bug."""
        rc = fake_home / ".bashrc"
        rc.write_text(
            "[ -f ~/.mnemos/completion/vesmaro.bash ] "
            "&& source ~/.mnemos/completion/vesmaro.bash\n",
            encoding="utf-8",
        )
        assert not _is_installed("bash", rc)
        # Installing fixes the state: stale line out, canonical line in.
        result = runner.invoke(app, ["completion", "bash"])
        assert result.exit_code == 0
        content = rc.read_text(encoding="utf-8")
        assert "vesmaro.bash" not in content
        assert content.count(CANONICAL_BASH) == 1

    def test_is_installed_matches_only_exact_canonical_line(self, fake_home: Path) -> None:
        rc = fake_home / ".bashrc"
        # Commented-out canonical line: not installed.
        rc.write_text(f"# {CANONICAL_BASH}\n", encoding="utf-8")
        assert not _is_installed("bash", rc)
        # Exact canonical line: installed.
        rc.write_text(f"{CANONICAL_BASH}\n", encoding="utf-8")
        assert _is_installed("bash", rc)
        # Fish "installation" is the primary completions file's presence.
        assert not _is_installed("fish", _rc_path("fish"))
        _fish_completions_file("vesma").parent.mkdir(parents=True, exist_ok=True)
        _fish_completions_file("vesma").write_text("", encoding="utf-8")
        assert _is_installed("fish", _rc_path("fish"))

    def test_remove_old_entries_keeps_non_completion_lines(self, fake_home: Path) -> None:
        rc = fake_home / ".bashrc"
        rc.write_text(
            "export PATH=$PATH:/usr/local/bin\n"
            "# a user comment mentioning mnemos/completion/ is migrated away\n"
            "alias ll='ls -la'\n",
            encoding="utf-8",
        )
        _remove_old_completion_entries(rc, "bash")
        content = rc.read_text(encoding="utf-8")
        assert "export PATH" in content
        assert "alias ll" in content
        assert "mnemos/completion/" not in content


# ── Wave W-I: rc integrity — block-aware migration + syntax validation ───────


class TestRcIntegrity:
    """The 2026-10-03 field-incident class: a half-removed legacy block left
    an orphaned ``fi`` in ~/.bashrc; bash aborts parsing the ENTIRE rc at
    that line, so completion stayed dead even with the canonical source line
    present — and the line-grep ``_is_installed`` reported "already
    installed". These tests pin: whole-construct migration, orphan cleanup,
    byte-stable idempotency, and never-write-a-broken-rc validation."""

    def rc_text(self, fake_home: Path) -> str:
        return (fake_home / ".bashrc").read_text(encoding="utf-8")

    def assert_bash_n_ok(self, rc: Path) -> None:
        proc = subprocess.run(["bash", "-n", str(rc)], capture_output=True, text=True)
        assert proc.returncode == 0, f"rc does not parse: {proc.stderr}"

    def test_field_incident_orphan_fi_removed_and_rc_parses(self, fake_home: Path) -> None:
        """EXACT field incident: comment + orphaned fi + old one-liner source."""
        rc = fake_home / ".bashrc"
        rc.write_text(
            "# Mnemos (AI Agents memorize)\n"
            "fi\n"
            "[ -f ~/.mnemos/completion/vesmaro.bash ] "
            "&& source ~/.mnemos/completion/vesmaro.bash\n"
            "export PATH=$PATH:/usr/local/bin\n",
            encoding="utf-8",
        )
        result = runner.invoke(app, ["completion", "bash"])
        assert result.exit_code == 0, result.output
        content = self.rc_text(fake_home)
        # The orphaned closer is gone; the rc parses again.
        assert not any(line.strip() == "fi" for line in content.splitlines())
        self.assert_bash_n_ok(rc)
        # The old one-liner is migrated, the canonical line added exactly once.
        assert "vesmaro.bash" not in content
        assert content.count(CANONICAL_BASH) == 1
        # Unrelated content survives — including the unrelated comment.
        assert "export PATH=$PATH:/usr/local/bin" in content
        assert "# Mnemos (AI Agents memorize)" in content

    def test_multiline_legacy_if_removed_whole_no_orphan(self, fake_home: Path) -> None:
        rc = fake_home / ".bashrc"
        rc.write_text(
            "export EDITOR=vim\n"
            "# Mnemos completion\n"
            "if [ -f ~/.mnemos/completion/mnemos.bash ]; then\n"
            "    source ~/.mnemos/completion/mnemos.bash\n"
            "fi\n"
            "export PATH=$PATH:/usr/local/bin\n",
            encoding="utf-8",
        )
        result = runner.invoke(app, ["completion", "bash"])
        assert result.exit_code == 0, result.output
        content = self.rc_text(fake_home)
        assert "mnemos.bash" not in content
        assert not any(line.strip() == "fi" for line in content.splitlines())
        assert "export EDITOR=vim" in content
        assert "export PATH=$PATH:/usr/local/bin" in content
        assert content.count(CANONICAL_BASH) == 1
        self.assert_bash_n_ok(rc)

    def test_legacy_if_else_block_removed_whole(self, fake_home: Path) -> None:
        rc = fake_home / ".bashrc"
        rc.write_text(
            "if [ -f ~/.mnemos/completion/mnemos.bash ]; then\n"
            "    source ~/.mnemos/completion/mnemos.bash\n"
            "else\n"
            "    echo skip\n"
            "fi\n",
            encoding="utf-8",
        )
        result = runner.invoke(app, ["completion", "bash"])
        assert result.exit_code == 0, result.output
        content = self.rc_text(fake_home)
        # Whole-block semantics: even non-legacy inner lines go with the block.
        assert "else" not in content
        assert "echo skip" not in content
        assert not any(line.strip() in ("fi", "then") for line in content.splitlines())
        assert content.count(CANONICAL_BASH) == 1
        self.assert_bash_n_ok(rc)

    def test_nested_legacy_if_removed_to_matching_fi(self, fake_home: Path) -> None:
        rc = fake_home / ".bashrc"
        rc.write_text(
            "if [ -f ~/.mnemos/completion/mnemos.bash ]; then\n"
            "  if [ -x /usr/bin/foo ]; then\n"
            "    source ~/.mnemos/completion/mnemos.bash\n"
            "  fi\n"
            "fi\n"
            "echo kept\n",
            encoding="utf-8",
        )
        result = runner.invoke(app, ["completion", "bash"])
        assert result.exit_code == 0, result.output
        content = self.rc_text(fake_home)
        assert "mnemos.bash" not in content
        assert "/usr/bin/foo" not in content
        assert not any(line.strip() == "fi" for line in content.splitlines())
        assert "echo kept" in content
        self.assert_bash_n_ok(rc)

    def test_user_own_block_survives_legacy_removal(self, fake_home: Path) -> None:
        """Over-removal guard: a healthy user if-block keeps its fi; only the
        legacy one-liner goes."""
        rc = fake_home / ".bashrc"
        rc.write_text(
            'if [ -d "$HOME/.local/bin" ]; then\n'
            '    export PATH="$HOME/.local/bin:$PATH"\n'
            "fi\n"
            "[ -f ~/.mnemos/completion/vesmaro.bash ] "
            "&& source ~/.mnemos/completion/vesmaro.bash\n",
            encoding="utf-8",
        )
        result = runner.invoke(app, ["completion", "bash"])
        assert result.exit_code == 0, result.output
        content = self.rc_text(fake_home)
        assert 'if [ -d "$HOME/.local/bin" ]; then' in content
        assert 'export PATH="$HOME/.local/bin:$PATH"' in content
        assert "fi" in content  # the user block's closer survives
        assert "vesmaro.bash" not in content
        assert content.count(CANONICAL_BASH) == 1
        self.assert_bash_n_ok(rc)

    def test_orphaned_then_done_else_removed(self, fake_home: Path) -> None:
        """Orphaned continuations/closers whose opener is historically gone
        are cleaned up too (same damage class as the orphaned fi)."""
        rc = fake_home / ".bashrc"
        rc.write_text("then\ndone\nelse\nexport A=1\n", encoding="utf-8")
        result = runner.invoke(app, ["completion", "bash"])
        assert result.exit_code == 0, result.output
        content = self.rc_text(fake_home)
        assert "then" not in content
        assert "done" not in content
        assert "else" not in content
        assert "export A=1" in content
        assert content.count(CANONICAL_BASH) == 1
        self.assert_bash_n_ok(rc)

    def test_unbalanced_legacy_opener_removes_only_itself(self, fake_home: Path) -> None:
        """An opener whose block never closes (rc damaged before us): only
        the opener is removed — user content below is never consumed to
        EOF. The result parses, so the install succeeds."""
        rc = fake_home / ".bashrc"
        rc.write_text(
            "if [ -f ~/.mnemos/completion/mnemos.bash ]; then\necho user-stuff\n",
            encoding="utf-8",
        )
        result = runner.invoke(app, ["completion", "bash"])
        assert result.exit_code == 0, result.output
        content = self.rc_text(fake_home)
        assert "mnemos.bash" not in content
        assert "echo user-stuff" in content
        assert content.count(CANONICAL_BASH) == 1
        self.assert_bash_n_ok(rc)

    def test_healthy_rc_untouched_byte_for_byte_on_rerun(self, fake_home: Path) -> None:
        """Idempotency regression: once installed, re-runs must not touch the
        rc at all (no churn, no validation-triggered rewrites)."""
        rc = fake_home / ".bashrc"
        rc.write_text("export EDITOR=vim\nalias ll='ls -la'\n", encoding="utf-8")
        assert runner.invoke(app, ["completion", "bash"]).exit_code == 0
        first = rc.read_bytes()
        for _ in range(2):
            assert runner.invoke(app, ["completion", "bash"]).exit_code == 0
        assert rc.read_bytes() == first
        assert first.count(CANONICAL_BASH.encode()) == 1

    def test_migration_write_rejected_by_validation_restores_original(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Validation failure on the legacy-migration write: original content
        restored byte-for-byte, non-zero exit, clear error."""
        rc = fake_home / ".bashrc"
        original = (
            "export KEEP=1\n"
            "[ -f ~/.mnemos/completion/vesmaro.bash ] "
            "&& source ~/.mnemos/completion/vesmaro.bash\n"
        )
        rc.write_text(original, encoding="utf-8")
        monkeypatch.setattr(
            completion_mod,
            "_check_shell_syntax",
            lambda shell, path: (False, "bash: line 2: syntax error near unexpected token"),
        )
        result = runner.invoke(app, ["completion", "bash"])
        assert result.exit_code == 1
        assert rc.read_text(encoding="utf-8") == original
        assert "syntax validation" in result.output
        assert "restored" in result.output
        assert CANONICAL_BASH not in rc.read_text(encoding="utf-8")

    def test_append_write_rejected_by_validation_restores_original(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Validation failure on the canonical-line append: the pre-append
        content is restored, non-zero exit (migration was a no-op, so only
        the append write is on trial)."""
        rc = fake_home / ".bashrc"
        original = "export KEEP=1\n"
        rc.write_text(original, encoding="utf-8")
        monkeypatch.setattr(
            completion_mod,
            "_check_shell_syntax",
            lambda shell, path: (False, "bash: line 3: parse error"),
        )
        result = runner.invoke(app, ["completion", "bash"])
        assert result.exit_code == 1
        assert rc.read_text(encoding="utf-8") == original
        assert CANONICAL_BASH not in rc.read_text(encoding="utf-8")

    def test_real_bash_n_rejection_restores_and_exits_nonzero(self, fake_home: Path) -> None:
        """End-to-end with the REAL bash -n: a pre-damaged rc (unterminated
        quote) fails validation after the append → restore + exit 1."""
        rc = fake_home / ".bashrc"
        original = 'echo "unterminated\n'
        rc.write_text(original, encoding="utf-8")
        proc = subprocess.run(["bash", "-n", str(rc)], capture_output=True, text=True)
        assert proc.returncode != 0  # fixture is genuinely broken for bash
        result = runner.invoke(app, ["completion", "bash"])
        assert result.exit_code == 1
        assert rc.read_text(encoding="utf-8") == original
        assert "syntax validation" in result.output
        assert CANONICAL_BASH not in rc.read_text(encoding="utf-8")

    def test_validation_invokes_shell_minus_n_per_shell(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """bash -n / zsh -n run on the rc after writes; fish never validates."""
        calls: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, returncode=0, stderr="")

        monkeypatch.setattr(completion_mod.subprocess, "run", fake_run)
        assert runner.invoke(app, ["completion", "bash"]).exit_code == 0
        assert ["bash", "-n", str(fake_home / ".bashrc")] in calls
        calls.clear()
        assert runner.invoke(app, ["completion", "zsh"]).exit_code == 0
        assert ["zsh", "-n", str(fake_home / ".zshrc")] in calls
        calls.clear()
        assert runner.invoke(app, ["completion", "fish"]).exit_code == 0
        assert calls == []  # fish: rc-less, validation not applicable


# ── Doctor: Completion check ──────────────────────────────────────────────────


class TestDoctorCompletionCheck:
    def test_pass_when_installed(self, fake_home: Path) -> None:
        from vesma.cli.doctor import CheckStatus, _check_completion

        runner.invoke(app, ["completion", "bash"])
        result = _check_completion()
        assert result.status == CheckStatus.PASS
        assert "vesma.bash" in result.detail

    def test_warn_when_script_missing(self, fake_home: Path) -> None:
        from vesma.cli.doctor import CheckStatus, _check_completion

        result = _check_completion()
        assert result.status == CheckStatus.WARN
        assert "vesma completion bash" in result.detail

    def test_warn_when_source_line_missing(self, fake_home: Path) -> None:
        from vesma.cli.doctor import CheckStatus, _check_completion

        _completion_file_path("bash").parent.mkdir(parents=True, exist_ok=True)
        _completion_file_path("bash").write_text("complete -F _vesma vesma vesmaro\n", "utf-8")
        result = _check_completion()
        assert result.status == CheckStatus.WARN
        assert "source line missing" in result.detail

    def test_warn_when_script_does_not_bind_primary(self, fake_home: Path) -> None:
        from vesma.cli.doctor import CheckStatus, _check_completion

        script = _completion_file_path("bash")
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("# broken script without the complete binding\n", "utf-8")
        (fake_home / ".bashrc").write_text(f"{CANONICAL_BASH}\n", "utf-8")
        result = _check_completion()
        assert result.status == CheckStatus.WARN
        assert "does not bind" in result.detail

    def test_canonical_line_helper_is_exact_contract(self, fake_home: Path) -> None:
        assert _canonical_source_line("bash") == CANONICAL_BASH

    def test_warn_when_rc_does_not_parse_field_incident(self, fake_home: Path) -> None:
        """Wave W-I: canonical line present + script fine, but the rc aborts
        parsing (orphaned fi ABOVE the source line) → WARN with the exact
        failing line and the repair hint — not PASS."""
        from vesma.cli.doctor import CheckStatus, _check_completion

        runner.invoke(app, ["completion", "bash"])
        rc = fake_home / ".bashrc"
        rc.write_text(f"# Mnemos (AI Agents memorize)\nfi\n{CANONICAL_BASH}\n", encoding="utf-8")
        result = _check_completion()
        assert result.status == CheckStatus.WARN
        assert "does not parse" in result.detail
        assert "line 2" in result.detail
        assert "fi" in result.detail
        assert "vesma completion bash" in result.detail

    def test_pass_when_rc_parses_healthy(self, fake_home: Path) -> None:
        """Healthy rc (parses clean) keeps the PASS — the new parse gate does
        not misfire on well-formed files."""
        from vesma.cli.doctor import CheckStatus, _check_completion

        runner.invoke(app, ["completion", "bash"])
        result = _check_completion()
        assert result.status == CheckStatus.PASS
        assert "does not parse" not in result.detail


# ── UX-2 hotfix wave: version stamp, staleness, install richness, shim ────────


class TestVersionStamp:
    """Every generated script embeds the generating vesma version."""

    @pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
    def test_scripts_embed_version(self, shell: str) -> None:
        script = completion_mod._completion_script(shell)
        stamp = completion_mod._script_version_header()
        assert stamp in script, f"{shell} script missing version stamp"
        assert completion_mod._script_version(script) == package_version()

    def test_parse_round_trip_and_legacy_none(self) -> None:
        assert completion_mod._script_version("# vesma version: 5.6.1\n") == "5.6.1"
        assert completion_mod._script_version("# no stamp here\n") is None

    def test_stamp_is_a_comment_line(self) -> None:
        """The stamp must stay a comment in EVERY shell — never executed."""
        stamp = completion_mod._script_version_header()
        assert stamp.startswith("# ")


def package_version() -> str:
    from vesma import __version__

    return __version__


class TestEngineDescriptions:
    """The custom engine emits value<TAB>description candidates (the UX-2
    requirement: tab completion shows command + description)."""

    def test_l1_commands_carry_descriptions(self) -> None:
        candidates = dict(get_completions([""], 0))
        assert "service" in candidates
        assert candidates["service"], "L1 service candidate has an empty description"

    def test_l2_service_verbs_carry_descriptions(self) -> None:
        candidates = dict(get_completions(["service", ""], 1))
        for verb in ("install", "uninstall", "status", "start"):
            assert verb in candidates, f"service verb {verb} missing at L2"
            assert candidates[verb], f"service verb {verb} has an empty description"

    def test_l3_options_after_dash_carry_help(self) -> None:
        candidates = dict(get_completions(["search", "-"], 1))
        assert "--limit" in candidates
        assert candidates["--limit"], "--limit option has an empty description"

    def test_descriptions_reach_stdout_as_tab_lines(self) -> None:
        result = runner.invoke(app, ["__complete", "service", "", "1"])
        assert result.exit_code == 0
        described = [ln for ln in result.output.splitlines() if "\t" in ln]
        assert described, "engine printed no value<TAB>description lines"


class TestDoctorCompletionStaleness:
    """doctor Completion check: installed script version != running version
    (or a pre-stamp script) → WARN with the one-command fix."""

    def _install_bash(self) -> None:
        assert runner.invoke(app, ["completion", "bash"]).exit_code == 0

    def test_fresh_install_passes(self, fake_home: Path) -> None:
        from vesma.cli.doctor import CheckStatus, _check_completion

        self._install_bash()
        result = _check_completion()
        assert result.status == CheckStatus.PASS

    def test_stale_version_warns_with_fix_command(self, fake_home: Path) -> None:
        from vesma.cli.doctor import CheckStatus, _check_completion

        self._install_bash()
        script = _completion_file_path("bash")
        script.write_text(
            script.read_text(encoding="utf-8").replace(
                completion_mod._script_version_header(), "# vesma version: 5.0.0"
            ),
            encoding="utf-8",
        )
        result = _check_completion()
        assert result.status == CheckStatus.WARN
        assert "stale" in result.detail
        assert "5.0.0" in result.detail
        assert "vesma completion" in result.detail

    def test_pre_stamp_script_counts_as_stale(self, fake_home: Path) -> None:
        from vesma.cli.doctor import CheckStatus, _check_completion

        self._install_bash()
        script = _completion_file_path("bash")
        text = script.read_text(encoding="utf-8")
        script.write_text(
            "\n".join(ln for ln in text.splitlines() if not ln.startswith("# vesma version:"))
            + "\n",
            encoding="utf-8",
        )
        result = _check_completion()
        assert result.status == CheckStatus.WARN
        assert "pre-versioned" in result.detail

    def test_stale_zsh_reported_with_shell_name(self, fake_home: Path) -> None:
        from vesma.cli.doctor import CheckStatus, _check_completion

        self._install_bash()
        assert runner.invoke(app, ["completion", "zsh"]).exit_code == 0
        zsh_script = _completion_file_path("zsh")
        zsh_script.write_text(
            zsh_script.read_text(encoding="utf-8").replace(
                completion_mod._script_version_header(), "# vesma version: 5.0.0"
            ),
            encoding="utf-8",
        )
        result = _check_completion()
        assert result.status == CheckStatus.WARN
        assert "zsh" in result.detail


class TestInstallCompletionFlag:
    """`vesma --install-completion` delegates to the custom installer — the
    typer builtin (description-less scripts) can never install instead."""

    def test_installs_for_detected_shell(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SHELL", "/bin/bash")
        result = runner.invoke(app, ["--install-completion"])
        assert result.exit_code == 0
        assert _completion_file_path("bash").exists()
        assert CANONICAL_BASH in (fake_home / ".bashrc").read_text(encoding="utf-8")

    def test_stamps_installed_script(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SHELL", "/bin/bash")
        runner.invoke(app, ["--install-completion"])
        assert (
            completion_mod._script_version(
                _completion_file_path("bash").read_text(encoding="utf-8")
            )
            == package_version()
        )

    def test_undetectable_shell_exits_1(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SHELL", "/usr/bin/tcsh")
        result = runner.invoke(app, ["--install-completion"])
        assert result.exit_code == 1
        assert "auto-detect" in result.output


class TestInstallOutputRichness:
    """The installer states which richness each shell got."""

    def test_bash_states_values_only(self, fake_home: Path) -> None:
        result = runner.invoke(app, ["completion", "bash"])
        assert result.exit_code == 0
        assert "values only" in result.output

    def test_zsh_states_descriptions(self, fake_home: Path) -> None:
        result = runner.invoke(app, ["completion", "zsh"])
        assert result.exit_code == 0
        assert "descriptions" in result.output

    def test_fish_states_descriptions(self, fake_home: Path) -> None:
        result = runner.invoke(app, ["completion", "fish"])
        assert result.exit_code == 0
        assert "descriptions" in result.output


class TestShellGlueDescriptionWiring:
    """The generated glue actually renders descriptions: zsh via _describe,
    fish via the -a engine call whose output fish renders natively."""

    def test_zsh_uses_describe(self) -> None:
        script = completion_mod._completion_script("zsh")
        assert "_describe" in script
        assert "word:desc" in script or "${word}:${desc}" in script

    def test_fish_uses_engine_call(self) -> None:
        script = completion_mod._completion_script("fish")
        assert "complete -c" in script
        assert "__vesma_complete" in script
