"""C-locale boundary pin (wave61 portability slice, audit F1-F3).

On a C-locale unix (``LC_ALL=C``/``LANG=C`` — minimal containers, ssh
hosts without locales) an encoding-less ``open()``/``read_text()`` decodes
with the ANSI_X3.4-1968 (ASCII) preferred encoding: a cyrillic memory file
crashes ``vesma add --file`` with :class:`UnicodeDecodeError`, and a
cyrillic comment in ``config.yaml`` crashes EVERY command at config load.
Under a legacy locale (KOI8-R/CP1251) the same code silently mojibakes
the store instead.

The subprocess forces the worst case deterministically:

* ``PYTHONCOERCECLOCALE=0`` + ``PYTHONUTF8=0`` — PEP 538 locale coercion
  would silently repair ``LC_ALL=C`` to C.UTF-8 on most distros and the
  pin would not pin anything;
* ``PYTHONIOENCODING=utf-8`` — the bug under test is the READ path
  (locale decoding of file/config input); stdout stays UTF-8 so the
  child can still render cyrillic output.

Byte-for-byte: the saved memory is read back via ``vesma export`` JSON
and compared against the original file bytes — a mojibake path fails the
exact-match assertion even where no exception fires.
"""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - fixed-argv CLI probes under a pinned env
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"

#: The canonical tag triple the rest of the CLI suite uses (strict
#: tag contract is the default; empty tag lists are not contract rows).
_TAGS = "project:cli-smoke,agent:cli,mnemos:checkpoint"

_PHRASE = (
    "Привет, мир! Проверка кодировки: ё, й, Ж — "
    "плюс ASCII-якорь locale-pin-42 для точного сравнения."
)


def _vesma_bin() -> Path:
    # No resolve(): the venv python is usually a symlink into the system
    # interpreter, and resolving it would leave the venv bin/ directory.
    script = Path(sys.executable).parent / "vesma"
    if not script.exists():
        raise RuntimeError(f"vesma console script not found next to {sys.executable}")
    return script


def _c_locale_env(home: Path, config: Path) -> dict[str, str]:
    """Child env: worst-case C locale, readable stdout, this checkout."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "VESMA_CONFIG": str(config),
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONCOERCECLOCALE": "0",
        "PYTHONUTF8": "0",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(SRC_ROOT),
    }


def _run(env: dict[str, str], cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 - fixed argv, no shell
        [str(_vesma_bin()), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        env=env,
        cwd=str(cwd),
        check=False,
    )


def _assert_clean(proc: subprocess.CompletedProcess[str], what: str) -> None:
    assert proc.returncode == 0, (
        f"{what}: exit={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "UnicodeDecodeError" not in proc.stderr, f"{what}: decode crash\n{proc.stderr}"
    assert "Traceback" not in proc.stderr, f"{what}: traceback\n{proc.stderr}"


def _export_contents(env: dict[str, str], home: Path, config: Path, out: Path) -> str:
    """Export the store to JSON and return every stored string concatenated."""
    proc = _run(env, home, "export", "--output", str(out), "--format", "json")
    _assert_clean(proc, "export")
    payload = json.loads(out.read_text(encoding="utf-8"))
    chunks: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, str):
            chunks.append(node)
        elif isinstance(node, dict):
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(payload)
    return "\n".join(chunks)


class TestCLocaleBoundary:
    """Core read paths survive ANSI_X3.4-1968 preferred encoding."""

    def test_add_file_cyrillic_round_trips_byte_for_byte(self, tmp_path: Path) -> None:
        """`add --file` on a cyrillic note: no decode crash, store keeps the exact bytes."""
        home = tmp_path / "home"
        home.mkdir()
        cfg = tmp_path / "config.yaml"
        cfg.write_text("# пусто — дефолты\n", encoding="utf-8")
        note = tmp_path / "note.md"
        note.write_text(_PHRASE + "\n", encoding="utf-8")

        env = _c_locale_env(home, cfg)
        proc = _run(env, home, "add", "--file", str(note), "--tags", _TAGS)
        _assert_clean(proc, "add --file")
        assert "Saved" in proc.stdout, proc.stdout

        stored = _export_contents(env, home, cfg, tmp_path / "export.json")
        assert _PHRASE in stored, "stored content diverged from the source file (mojibake or loss)"

    def test_cyrillic_config_comment_loads_without_decode_error(self, tmp_path: Path) -> None:
        """A cyrillic comment in config.yaml must not crash every command."""
        home = tmp_path / "home"
        home.mkdir()
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "# Комментарий по-русски: конфиг читается при любой локали\n"
            "# Ёлки, йод, Жюли — не-ASCII для декодера\n",
            encoding="utf-8",
        )

        env = _c_locale_env(home, cfg)
        proc = _run(env, home, "export", "--output", str(tmp_path / "export.json"))
        _assert_clean(proc, "export with cyrillic config")
