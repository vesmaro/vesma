# Contributing to Vesma

Thanks for thinking about contributing. This page is the whole contract: how to set up a dev
environment, how changes flow to `main`, and the gate every change must pass. For the product
itself, start at the [README](README.md) and the [docs](docs/README.md).

**🌐 Language / Язык:** English · [Русский](CONTRIBUTING.ru.md)

---

## Development setup

```bash
git clone https://github.com/vesmaro/vesma.git
cd vesma
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"
vesma --help        # sanity check
```

- Python **3.11+** (`uv` recommended; plain `python -m venv` works too).
- `[dev]` brings the quality-gate toolchain. The MCP SDK is a core dependency (ADR-0023); the `[mcp]` extra remains as an empty compatibility alias.
- External LLM providers are separate extras (`ollama`, `openai`, `anthropic`, `gemini`) — install
  only what you exercise.

## The quality gate

```bash
make verify
```

One command, the full gate — the same composition the release pipeline trusts:

| Step | Tool | What it checks |
|------|------|----------------|
| 1 | `ruff format --check` + `ruff check` | Formatting and lint |
| 2 | `mypy --strict` | Type correctness |
| 3 | `pytest` | The test suite (2300+ tests) |
| 4 | `bandit` + `pip-audit` | Security lint + dependency CVE scan |
| 5 | `bench-s1` | The ADR-0020 quality gate (corridors + invariants vs the baseline) |
| 6 | `vesma doctor` | Health checks (warnings are non-blocking in CI-like environments) |
| 7 | version guard | `VERSION` and `pyproject.toml` agree |

If it's green, the change is good to ship. If `pip-audit` flags a pinned CVE, follow the
[dependency-updates runbook](docs/en/admin/runbooks/dependency-updates.md).

## Git workflow

```
feat/*  →  dev-<stage>  →  release/X.Y.Z  →  main
```

- `main` accepts **only** `release/*` and `hotfix/*` PRs.
- Conventional Commits are required (`feat(scope): …`, `fix: …`, `docs: …`).
- Run `make verify` before opening a PR.
- Breaking or model-footprint changes need a release-window card from the release manager
  **before** merge (see [ADR-0022](docs/project/adr/0022-licensing-foundation.md) context and
  the [release versioning policy](docs/project/dev-plan.md) in `docs/project/dev-plan.md`).

## Documentation conventions

- **Docs reflect code.** Every command, flag, config key, endpoint, and path in `docs/` must match
  the source; if the code changed, the docs change in the same PR.
- **EN and RU are synchronous.** Every user-facing page exists in `docs/en/` and `docs/ru/`; edit
  both in the same wave. `README.md` and `README.ru.md` are full mirrors — preserve the
  `<!-- version:… -->` marker blocks (the release pipeline rewrites versions inside them).
- Frozen history: `docs/project/` (ADRs, reports, milestones) is not kept "current" — do not
  restate it, reference it.

## Where things live

| Path | What |
|------|------|
| `src/vesma/` | The server: core, CLI, MCP, HTTP API, storage |
| `tests/` | The suite (unit + integration + golden baselines) |
| `integrations/` | The behavioral pack: targets, instructions, skills, prompts, presets, the pi bridge |
| `benchmarks/` | The ADR-0020 stands (S1–S4) and baselines |
| `training/` | The nano-model training stack (never ships in the wheel) |
| `docs/` | EN + RU documentation set |
| [PLAN.md](PLAN.md) | The roadmap · [docs/project/adr/](docs/project/adr/) — decision records |

## Reporting issues

Open a [GitHub issue](https://github.com/vesmaro/vesma/issues) with the command you ran, the
exact output, and your `vesma doctor` report (mask anything that looks like a secret — the
issue tracker is public).
