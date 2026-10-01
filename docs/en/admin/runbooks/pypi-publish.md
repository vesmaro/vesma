# Runbook: PyPI Publish

**🌐 Language / Язык:** English · [Русский](../../../ru/admin/runbooks/pypi-publish.md)

First-publish pipeline for the `mnemos-memory-server` package on PyPI —
issue #122, ADR-0017 Phase 0 (Distribution). GitHub Actions is
billing-locked (#117), so the whole pipeline runs locally via
`scripts/pypi-publish.sh` (sibling of `scripts/local-release.sh`, which
owns the container image + GitHub Release half).

**First publish is an owner-executed step.** PyPI names and versions are
immutable: a published version can never be re-uploaded or replaced, and
a project name cannot be silently migrated. Everything below prepares and
verifies the artifacts — the actual `twine upload` is a deliberate,
manual step.

## Package name — DECIDED: `mnemos-memory-server` (2026-09-01)

The distribution name was decided 2026-09-01 and set in `pyproject.toml`
(`name = "mnemos-memory-server"`). The import package stays `vesma` and
the CLI stays `vesma` — only the installable/PyPI name changed. The
matrix below is kept as decision history; it was last re-checked
2026-09-01 (statuses unchanged since 2026-08-21).

| Name | PyPI status | Occupied by |
| --- | --- | --- |
| `vesma` | ❌ taken (v0.1.1) | "Memory for agentic AI" — Tyson Chan |
| `mnemos-memory` | ❌ taken (v0.6.0) | "Biomimetic memory architectures for LLMs" |
| `mnemos-memory-server` | ✅ free | — |
| `vesma-server` | ✅ free | — |
| `vesma-mcp` | ✅ free | — |
| `vesma-ai` | ✅ free | — |
| `vesma-agent-memory` | ✅ free | — |

Both taken names are **AI-memory projects in the same domain** — a third
similar name maximizes user confusion, so the fallback should be
self-descriptive rather than minimal.

**Chosen: `mnemos-memory-server`** — states exactly what the
package is ("a memory server named vesma"), matches the project
description, and is unambiguous against both taken neighbors.

How to re-check (no auth needed):

```bash
# 404 = name free, 200 = taken
curl -s -o /dev/null -w '%{http_code}\n' https://pypi.org/simple/<name>/

# what occupies a taken name
curl -s https://pypi.org/pypi/<name>/json | python3 -c \
  "import json,sys; i=json.load(sys.stdin)['info']; print(i['version'], '|', i['summary'])"
```

Caveats:

- **PEP 503 normalization** — `mnemos-memory-server`, `mnemos_memory_server`
  and `mnemos.memory.server` are the SAME PyPI name. The check must use
  the normalized form.
- **Similarity/squatting screen** — PyPI rejects new registrations
  confusable with existing popular packages. The exact threshold is
  server-side; final confirmation of any name happens only at the first
  upload. If rejected, take the next candidate from the matrix above.

**Changing the name** is a one-line edit (`name = "..."` in
`pyproject.toml`) followed by a rebuild — `scripts/pypi-publish.sh`
reads the name from `pyproject.toml` and adapts automatically (wheel
filename normalization included). Do it BEFORE the first publish;
afterwards the name is fixed forever.

> **Asset note (2026-09-01):** tag `v3.1.0` was cut with the old name and
> is NOT re-cut (its npm channel `pi-mnemos@3.1.0` is already published).
> No wheel asset with the new filename exists for v3.1.0 — the README
> `<!-- version:pip -->` marker therefore points at the NEXT release
> (v3.2.0). `scripts/sync-readme-version.sh` keeps the marker correct
> from that release on; `scripts/install.sh` builds the URL from the
> normalized filename.

## Pipeline — `scripts/pypi-publish.sh`

| Mode | What it does |
| --- | --- |
| default (check) | G0–G4 gates, wheel/sdist build, `twine check`, offline metadata smoke |
| `--full-smoke` | additionally installs the wheel WITH deps into a throwaway venv, runs `vesma --version` (needs pypi.org) |
| `--publish` | all checks, then `twine upload` (release tag + credentials required) |
| `--publish --full-smoke` | recommended pre-upload combination |
| `--i-own-name` | required to upload when the PyPI project already exists (updates of our own project only) |
| `--reuse-dist` | skip the build when `dist/` already holds matching artifacts |
| `--dry-run` | print every step, mutate nothing |

Makefile alias: `make pypi-publish` (check mode).

### Gates

| Gate | Checks | Hard fail? |
| --- | --- | --- |
| G0 | PyPI name: 404 = free (first publish); 200 + `--i-own-name` = update; target version already on PyPI = always an error | `--publish` mode |
| G1 | HEAD is exactly on a release tag `vX.Y.Z` | `--publish` mode |
| G2 | tag version == `pyproject.toml` version | `--publish` mode |
| G3 | wheel + sdist filename versions == `pyproject.toml` version | `--publish` mode |
| G4 | smoke-installed package version == `pyproject.toml` version | always (artifact proof) |

G0 stays as a hard safety net even though the name is now decided (see
the matrix above): an accidental `--publish` against a taken or renamed
name still fails cleanly BEFORE any upload attempt.

## Publish procedure (owner)

Once the name is decided and a release is cut (`release/X.Y.Z` →
`main`, tag `vX.Y.Z`, per the git-workflow runbook):

1. **Create a PyPI API token** — <https://pypi.org/manage/account/token/>.
   For the very FIRST upload the project does not exist yet, so the
   token must be account-scoped; immediately after the first publish,
   delete it and issue a project-scoped token for all future uploads.
   Never paste the token into files, chat, or shell history — export it
   in the moment:

   ```bash
   export PYPI_TOKEN=pypi-...   # from the PyPI UI, stays in this shell only
   ```

2. **Checkout the tag and run the full pipeline**:

   ```bash
   git checkout vX.Y.Z
   scripts/pypi-publish.sh --publish --full-smoke
   # if (and only if) the PyPI project already exists and is ours:
   scripts/pypi-publish.sh --publish --full-smoke --i-own-name
   ```

3. **Post-publish verification** (from a CLEAN venv, not the dev one):

   ```bash
   pip index versions <final-name>            # our version must appear
   curl -s -o /dev/null -w '%{http_code}\n' https://pypi.org/simple/<final-name>/   # 200
   pip install <final-name> && vesma --version
   ```

4. **Close the loop** — update `docs/en/admin/runbooks/install.md` (+ RU
   mirror) with the real `pip install` line, mention the package in the
   release notes, tick the "pip install works from PyPI" acceptance
   criterion of #122.

## Immutability rules (PyPI hard constraints)

- **Never re-upload a version.** A filename uploaded once is burned
  forever, even if the project is deleted. Mistake in `vX.Y.Z` →
  bump to `vX.Y.Z+1` (or `X.Y.(Z+1)`) and upload the fix.
- **Never delete-and-recreate a project** to "reset" versions.
- A broken release is **yanked** (`pypi.org/manage/project/...`), not
  removed — yanked versions still resolve for pinned installs.
- Renaming a published project is impossible — a new name means a new
  project, and the old one keeps existing.

## Related

- `scripts/pypi-publish.sh --help` — pipeline modes and gates
- `scripts/local-release.sh` — container image + GitHub Release half
- [Install runbook](install.md) — first-run operational checklist
- [CI/CD runbook](ci-cd.md) — why builds run locally (billing lock)
- Issue #122, ADR-0017 (docs/project/adr/0017-memory-system-evolution-roadmap.md)
