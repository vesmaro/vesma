"""Drift guard for the published MCP presets and adapter template (#124).

The presets (``integrations/mcp-presets.md``) and the template
(``integrations/adapter-template.md``) are copy-paste configuration — a
silent edit to the wire contract breaks every user who pastes them. These
tests parse the markdown artefacts themselves and validate every config
fragment against the canonical ADR-0017 D1 stdio entry:

    command = "mnemos", args = ["mcp-server"], type = "stdio"

One documented exception: the OpenCode preset uses OpenCode's native local
server form (`"type": "local"`, a single command argv array) and is
validated against that contract instead (#231).

Import policy: tests operate on plain files (no ``mnemos`` import), so they
run identically from any interpreter / install layout. One documented
exception — ``test_canonical_env_names_and_subtypes_steer_src`` — proves the
documented env-var names and tag subtypes against the ``src/`` tree in a
subprocess, and skips itself when the deps are unavailable.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PRESETS = REPO_ROOT / "integrations" / "mcp-presets.md"
TEMPLATE = REPO_ROOT / "integrations" / "adapter-template.md"

#: The wire contract every preset/fragment must carry (ADR-0017 D1).
CANONICAL_COMMAND = "vesma"
CANONICAL_ARGS = ["mcp-server"]
CANONICAL_TYPE = "stdio"

#: The OpenCode local-server contract (its native "mcp" map shape, #231):
#: one argv array instead of command/args, and type "local" instead of
#: "stdio".
CANONICAL_OPENCODE_COMMAND = ["vesma", "mcp-server"]
CANONICAL_OPENCODE_TYPE = "local"

#: Harness config paths the presets must keep naming correctly.
PRESET_CONFIG_PATHS = (
    "~/.cursor/mcp.json",
    "~/.codex/config.toml",
    "~/.codeium/windsurf/mcp_config.json",
)

#: README compatibility-table links down to per-harness preset anchors.
README_PRESET_ANCHORS = (
    "integrations/mcp-presets.md#claude-code",
    "integrations/mcp-presets.md#cursor",
    "integrations/mcp-presets.md#codex",
    "integrations/mcp-presets.md#windsurf",
)

#: Env names as pydantic-settings actually maps them for the nested
#: ``Settings.vesma`` section (``VESMA_`` prefix + ``__`` nesting).
CANONICAL_ENV_VARS = ("VESMA_VESMA__DATA_DIR", "VESMA_VESMA__VAULT_PATH")
#: Legacy short forms — #139 compat aliases (honoured again, but never the
#: documented form; canonical wins when both are set). Allowed in the
#: artefacts' prose notes only, never as an instruction.
LEGACY_ENV_VARS = ("VESMA_DATA_DIR", "VESMA_VAULT__VAULT_PATH")

#: Known ``mnemos:`` subtypes — a canary list; ``trace`` is NOT a subtype.
CANDIDATE_SUBTYPES = (
    "session",
    "bug-pattern",
    "learning",
    "decision",
    "rule",
    "trace",
    "open-question",
    "checkpoint",
    "legacy",
    "synthesized",
)


# ── helpers ───────────────────────────────────────────────────────────────────


def fenced_blocks(text: str, lang: str) -> list[str]:
    """Return the bodies of all ```lang fenced blocks in *text*."""
    return re.findall(rf"```{lang}\n(.*?)```", text, flags=re.DOTALL)


def assert_json_server_entry(entry: object) -> None:
    """Assert a parsed JSON server entry matches the canonical contract."""
    assert isinstance(entry, dict), f"server entry is not an object: {entry!r}"
    assert entry.get("command") == CANONICAL_COMMAND
    assert entry.get("args") == CANONICAL_ARGS
    assert entry.get("type") == CANONICAL_TYPE


def assert_opencode_server_entry(entry: object) -> None:
    """Assert a parsed OpenCode local server entry matches its contract."""
    assert isinstance(entry, dict), f"server entry is not an object: {entry!r}"
    assert entry.get("command") == CANONICAL_OPENCODE_COMMAND
    assert entry.get("type") == CANONICAL_OPENCODE_TYPE


def parse_json_fragment(block: str) -> dict[str, object]:
    """Parse a `"vesma": {…}` fragment line by wrapping it in an object."""
    payload = json.loads("{" + block.strip() + "}")
    assert isinstance(payload, dict)
    return payload


def dig_server(payload: dict[str, object]) -> object:
    """Extract the vesma entry from an mcpServers/servers config file."""
    for key in ("mcpServers", "servers"):
        servers = payload.get(key)
        if isinstance(servers, dict) and "vesma" in servers:
            return servers["vesma"]
    raise AssertionError(f"no mcpServers.vesma in payload: {payload!r}")


def assert_toml_server_table(block: str) -> None:
    """Assert a Codex-style TOML block defines the canonical entry."""
    data = tomllib.loads(block)
    entry = data.get("mcp_servers", {}).get("vesma")
    assert isinstance(entry, dict), f"no [mcp_servers.vesma] table in: {block!r}"
    assert entry.get("command") == CANONICAL_COMMAND
    assert entry.get("args") == CANONICAL_ARGS


# ── mcp-presets.md ────────────────────────────────────────────────────────────


def test_presets_file_exists() -> None:
    assert PRESETS.is_file(), f"missing published artefact: {PRESETS}"


def test_presets_cover_required_harnesses() -> None:
    text = PRESETS.read_text(encoding="utf-8")
    for harness in ("Cursor", "Claude Code", "Codex", "Windsurf"):
        assert f"## {harness}" in text, f"no preset section for {harness}"


def test_presets_json_blocks_match_wire_contract() -> None:
    """Every JSON block carries the canonical stdio entry — except the
    OpenCode preset, which is validated against the native local form."""
    text = PRESETS.read_text(encoding="utf-8")
    blocks = fenced_blocks(text, "json")
    # Cursor fragment, Claude full file, Windsurf fragment, OpenCode fragment.
    assert len(blocks) == 4, f"expected 4 JSON blocks in the presets, got {len(blocks)}"
    for block in blocks:
        stripped = block.strip()
        if stripped.startswith('"'):
            entry = parse_json_fragment(stripped)["vesma"]
        else:
            entry = dig_server(json.loads(stripped))
        if isinstance(entry, dict) and entry.get("type") == CANONICAL_OPENCODE_TYPE:
            assert_opencode_server_entry(entry)
        else:
            assert_json_server_entry(entry)


def test_presets_toml_block_matches_wire_contract() -> None:
    blocks = fenced_blocks(PRESETS.read_text(encoding="utf-8"), "toml")
    assert len(blocks) == 1, "expected exactly one TOML block (Codex preset)"
    assert_toml_server_table(blocks[0])


def test_presets_shell_one_liners_match_wire_contract() -> None:
    """echo/printf one-liners embed the same entry as the paste blocks."""
    for line in PRESETS.read_text(encoding="utf-8").splitlines():
        if "echo '" in line and "mcpServers" in line:
            payload = re.search(r"echo '(\{.*\})'", line)
            assert payload is not None, f"cannot extract JSON payload: {line}"
            assert_json_server_entry(dig_server(json.loads(payload.group(1))))
        if "printf" in line and "mcp_servers.mnemos" in line:
            assert 'command = "mnemos"' in line
            assert 'args = ["mcp-server"]' in line


def test_presets_claude_one_liner_shape() -> None:
    """`claude mcp add` blocks keep the canonical command tail."""
    blocks = fenced_blocks(PRESETS.read_text(encoding="utf-8"), "bash")
    add_blocks = [b for b in blocks if "claude mcp add" in b]
    assert add_blocks, "no `claude mcp add` one-liner found"
    for block in add_blocks:
        assert "-- vesma mcp-server" in block


def test_presets_no_secrets_in_examples() -> None:
    text = PRESETS.read_text(encoding="utf-8").lower()
    for marker in ("api_key=", "token=", "password=", "sk-"):
        assert marker not in text, f"secret-shaped marker {marker!r} in presets"


# ── adapter-template.md ───────────────────────────────────────────────────────


def test_template_file_exists_and_sized() -> None:
    assert TEMPLATE.is_file(), f"missing published artefact: {TEMPLATE}"
    line_count = len(TEMPLATE.read_text(encoding="utf-8").splitlines())
    # "~100 lines" per issue #124 — a generous band, still one screen.
    assert 80 <= line_count <= 120, f"adapter template drifted to {line_count} lines"


def test_template_has_three_sections_and_checklist() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    for section in ("Connect", "Expose", "Configure", "Acceptance checklist"):
        assert section in text, f"template missing section: {section}"
    items = re.findall(r"^- \[ \] ", text, flags=re.MULTILINE)
    assert len(items) >= 8, f"checklist too thin: {len(items)} items"


def test_template_config_blocks_match_wire_contract() -> None:
    """The template must pass its own checklist item #1: the pasted entry."""
    text = TEMPLATE.read_text(encoding="utf-8")
    json_blocks = fenced_blocks(text, "json")
    toml_blocks = fenced_blocks(text, "toml")
    assert json_blocks and toml_blocks, "template must show JSON and TOML forms"
    for block in json_blocks:
        stripped = block.strip()
        if stripped.startswith('"'):
            entry = parse_json_fragment(stripped)["vesma"]
        else:
            entry = dig_server(json.loads(stripped))
        assert_json_server_entry(entry)
    for block in toml_blocks:
        assert_toml_server_table(block)


def test_template_exposes_core_tools() -> None:
    """Checklist items 3-5 reference tools the Expose section must list."""
    text = TEMPLATE.read_text(encoding="utf-8")
    for tool in ("vesma_search", "vesma_add", "vesma_agent_recall"):
        assert tool in text, f"Expose section missing core tool: {tool}"


def test_template_states_tag_contract() -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    for tag in ("project:", "agent:", "mnemos:"):
        assert tag in text, f"Configure section missing tag kind: {tag}"


# ── README compatibility table (EN + RU) ─────────────────────────────────────


README_TABLE_HEADERS = [
    ("README.md", "One-line MCP preset"),
    ("README.ru.md", "MCP-пресет"),
]


@pytest.mark.parametrize("readme,header", README_TABLE_HEADERS)
def test_readme_compatibility_table_present(readme: str, header: str) -> None:
    text = (REPO_ROOT / readme).read_text(encoding="utf-8")
    assert header in text, f"{readme} missing the compatibility table header"
    for harness in ("Cursor", "Claude Code", "Codex", "Windsurf", "Hermes"):
        assert harness in text, f"{readme} compatibility table missing {harness}"


def test_guides_reference_artefacts() -> None:
    en = (REPO_ROOT / "docs" / "en" / "user" / "integration-guide.md").read_text(encoding="utf-8")
    ru = (REPO_ROOT / "docs" / "ru" / "user" / "integration-guide.md").read_text(encoding="utf-8")
    for text in (en, ru):
        assert "mcp-presets.md" in text
        assert "adapter-template.md" in text


# ── review pins (#124 code review) ────────────────────────────────────────────


def test_presets_pin_config_paths() -> None:
    """Each preset names its harness config file — pin the exact paths."""
    text = PRESETS.read_text(encoding="utf-8")
    for path in PRESET_CONFIG_PATHS:
        assert path in text, f"presets no longer name the config path {path}"


@pytest.mark.parametrize("readme", ["README.md", "README.ru.md"])
def test_readme_preset_anchor_links_pinned(readme: str) -> None:
    """README tables link to per-harness preset anchors — pin all four."""
    text = (REPO_ROOT / readme).read_text(encoding="utf-8")
    for link in README_PRESET_ANCHORS:
        assert link in text, f"{readme} missing preset anchor link {link}"


def test_artefacts_use_canonical_env_names_only() -> None:
    """Env names in the artefacts must be the pydantic-settings-canonical
    form; the legacy short forms are #139 compat aliases, not the
    documented form.

    The legacy names may appear ONLY in an explicit prose note (compat
    aliases, canonical preferred) — never as an instruction: not inside any
    fenced code block, not as a ``--env`` flag, not as a tuning-table row.
    """
    for artefact in (PRESETS, TEMPLATE):
        text = artefact.read_text(encoding="utf-8")
        for name in CANONICAL_ENV_VARS:
            assert name in text, f"{artefact.name} missing canonical {name}"
        code_blocks = fenced_blocks(text, "bash") + fenced_blocks(text, "json")
        code_blocks += fenced_blocks(text, "toml") + fenced_blocks(text, "text")
        for legacy in LEGACY_ENV_VARS:
            for block in code_blocks:
                assert legacy not in block, (
                    f"{artefact.name} instructs the non-canonical env var {legacy} in a code block"
                )
            assert f"| `{legacy}`" not in text, (
                f"{artefact.name} lists the non-canonical env var {legacy} as a table row"
            )


def test_canonical_env_names_and_subtypes_steer_src() -> None:
    """Subprocess proof against the src tree (the one import exempt test).

    1. The documented env names must actually move ``Settings.vesma``
       (pydantic-settings ``VESMA_`` prefix + ``__`` nesting).
    2. Every ``mnemos:`` subtype listed in the template must exist in
       ``VESMARO_TAG_SUBTYPES`` (``trace`` is a canary: not a subtype).
    Skips when this interpreter cannot import the src deps.
    """
    script = (
        "import sys, os, json\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "os.environ['VESMA_VESMA__DATA_DIR'] = '/tmp/mnemos-pin-data'\n"
        "os.environ['VESMA_VESMA__VAULT_PATH'] = '/tmp/mnemos-pin-vault'\n"
        "os.environ.pop('VESMA_DATA_DIR', None)\n"
        "os.environ.pop('VESMA_VAULT__VAULT_PATH', None)\n"
        "from vesmaro.config import Settings\n"
        "from vesmaro.models import VESMARO_TAG_SUBTYPES\n"
        "s = Settings(_env_file=None)\n"
        "print(s.vesma.data_dir, s.vesma.vault_path)\n"
        "print(json.dumps(sorted(VESMARO_TAG_SUBTYPES)))\n"
    )
    proc = subprocess.run(  # nosec B603 — fixed argv, repo-local src tree
        [sys.executable, "-c", script, str(REPO_ROOT / "src")],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0 and re.search(r"(ModuleNotFound|Import)Error", proc.stderr):
        pytest.skip(f"src deps unavailable under {sys.executable}")
    assert proc.returncode == 0, proc.stderr
    lines = proc.stdout.strip().splitlines()
    assert len(lines) == 2, f"unexpected subprocess output: {proc.stdout!r}"

    # 1. Canonical env names steer Settings; the legacy ones do not.
    assert "/tmp/mnemos-pin-data /tmp/mnemos-pin-vault" in lines[0], (
        f"canonical env names ignored by Settings: {lines[0]!r}"
    )

    # 2. Subtypes listed in the template are real subtypes.
    template_text = TEMPLATE.read_text(encoding="utf-8")
    listed = {t for t in CANDIDATE_SUBTYPES if f"`{t}`" in template_text}
    assert listed, "template no longer lists any mnemos: subtypes"
    real = set(json.loads(lines[1]))
    invalid = listed - real
    assert not invalid, f"template lists non-existent mnemos: subtypes: {sorted(invalid)}"
