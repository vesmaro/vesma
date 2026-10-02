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
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from vesmaro.cli.complete_cmd import get_completions
from vesmaro.cli.completion import (
    _canonical_source_line,
    _completion_file_path,
    _fish_completions_file,
    _is_installed,
    _primary_prog_name,
    _prog_names,
    _rc_path,
    _remove_old_completion_entries,
)
from vesmaro.cli.main import app

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
        assert "add\tAdd a new memory entry." in lines
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


# ── Doctor: Completion check ──────────────────────────────────────────────────


class TestDoctorCompletionCheck:
    def test_pass_when_installed(self, fake_home: Path) -> None:
        from vesmaro.cli.doctor import CheckStatus, _check_completion

        runner.invoke(app, ["completion", "bash"])
        result = _check_completion()
        assert result.status == CheckStatus.PASS
        assert "vesma.bash" in result.detail

    def test_warn_when_script_missing(self, fake_home: Path) -> None:
        from vesmaro.cli.doctor import CheckStatus, _check_completion

        result = _check_completion()
        assert result.status == CheckStatus.WARN
        assert "vesma completion bash" in result.detail

    def test_warn_when_source_line_missing(self, fake_home: Path) -> None:
        from vesmaro.cli.doctor import CheckStatus, _check_completion

        _completion_file_path("bash").parent.mkdir(parents=True, exist_ok=True)
        _completion_file_path("bash").write_text("complete -F _vesma vesma vesmaro\n", "utf-8")
        result = _check_completion()
        assert result.status == CheckStatus.WARN
        assert "source line missing" in result.detail

    def test_warn_when_script_does_not_bind_primary(self, fake_home: Path) -> None:
        from vesmaro.cli.doctor import CheckStatus, _check_completion

        script = _completion_file_path("bash")
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text("# broken script without the complete binding\n", "utf-8")
        (fake_home / ".bashrc").write_text(f"{CANONICAL_BASH}\n", "utf-8")
        result = _check_completion()
        assert result.status == CheckStatus.WARN
        assert "does not bind" in result.detail

    def test_canonical_line_helper_is_exact_contract(self, fake_home: Path) -> None:
        assert _canonical_source_line("bash") == CANONICAL_BASH
