# Runbook: PyPI Publish

**🌐 Language / Язык:** English · [Русский](../../../ru/admin/runbooks/pypi-publish.md)

Publish pipeline for the `vesma` package on PyPI — built in issue #122,
ADR-0017 Phase 0 (Distribution) for the first publish and now the
routine update pipeline as well. GitHub Actions is
billing-locked (#117), so the whole pipeline runs locally via
`scripts/pypi-publish.sh` (sibling of `scripts/local-release.sh`, which
owns the container image + GitHub Release half).

**Uploads are owner-executed steps.** PyPI names and versions are
immutable: a published version can never be re-uploaded or replaced, and
a project name cannot be silently migrated. Everything below prepares and
verifies the artifacts — the actual `twine upload` is a deliberate,
manual step.

## PyPI channels — LIVE: `vesma` (primary; re-verified 2026-10-05, both channels carry 5.5.0)

The distribution name decided 2026-09-01 was `mnemos-memory-server`. In
the 5.0.0 rebrand the release channels moved: the bare `vesma` slot is
ours and is the primary channel (`pyproject.toml` `name = "vesma"`),
`vesma-memory-server` is a live mirror, and the pre-rebrand
`mnemos-memory-server` stays published, frozen at 5.2.0 until deprecation.
All three carry the "(Vesmaro Project)" summary.

Since 5.0.0 the import package is `vesmaro` (a `mnemos` compat shim still
ships) and the CLIs are `vesmaro` (canonical), `vesma` (short hook) and
`mnemos` (deprecated alias). Only the installable/PyPI name and the
Python import name differ. Since the 5.5.0 train the pipeline publishes
BOTH live channels in one run: `scripts/pypi-publish.sh` reads the
primary name from `pyproject.toml`, then substitutes the mirror name
(rebuild + gates per dist) and restores `pyproject.toml` byte-identical —
the formerly manual mirror dance is codified in the script.

| Channel | PyPI status | Used by |
| --- | --- | --- |
| `vesma` | ours, live (5.0.0 → 5.5.0) | primary — `pip install vesma`, README badge |
| `vesma-memory-server` | ours, live (5.0.0 → 5.5.0) | mirror; `scripts/install.sh` probes `vesma` first and consults the mirror only as a logged fallback |
| `mnemos-memory-server` | ours, frozen at 5.2.0 | legacy pre-rebrand channel, live until deprecation |

The original 2026-09-01 decision matrix is kept as written that day
(note: its `vesma` entry does not match today's PyPI state — see the
verified table above; no `v0.1.1` exists in the release list):

| Name | PyPI status (2026-09-01, as recorded) | Occupied by |
| --- | --- | --- |
| `vesma` | ❌ recorded taken (v0.1.1) | "Memory for agentic AI" — Tyson Chan |
| `mnemos-memory` | ❌ taken (v0.6.0) | "Biomimetic memory architectures for LLMs" |
| `mnemos-memory-server` | ✅ free then | — chosen first, frozen at 5.2.0 after the rebrand |
| `vesma-server` / `vesma-mcp` / `vesma-ai` / `vesma-agent-memory` | ✅ free then | — |

How to re-check (no auth needed):

```bash
# 404 = name free, 200 = taken
curl -s -o /dev/null -w '%{http_code}\n' https://pypi.org/simple/<name>/

# what occupies a taken name
curl -s https://pypi.org/pypi/<name>/json | python3 -c \
  "import json,sys; i=json.load(sys.stdin)['info']; print(i['version'], '|', i['summary'])"
```

Caveats:

- **PEP 503 normalization** — `vesma-memory-server`, `vesma_memory_server`
  and `vesma.memory.server` are the SAME PyPI name. The check must use
  the normalized form.
- **Similarity/squatting screen** — PyPI rejects new registrations
  confusable with existing popular packages. The exact threshold is
  server-side; final confirmation of any name happens only at the first
  upload. If rejected, take the next candidate from the matrix above.

**Changing the name** was a one-line edit (`name = "..."` in
`pyproject.toml`) followed by a rebuild — `scripts/pypi-publish.sh`
reads the name from `pyproject.toml` and adapts automatically (wheel
filename normalization included). That window is closed: `vesma` is
published, so the name is fixed forever (see Immutability rules) — a new
name would mean a new, parallel project.

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
| default (check) | CL changelog preflight + G0–G4 gates, wheel/sdist build, `twine check`, offline metadata smoke — for EVERY candidate dist |
| `--dists "a b"` | restrict the candidate dist list (default: `vesma vesma-memory-server`; env override `CANDIDATE_DISTS`) |
| `--full-smoke` | additionally installs each wheel WITH deps into a throwaway venv, runs `vesma --version` (needs pypi.org) |
| `--publish` | all checks, then `twine upload` per dist (release tag + credentials required) |
| `--publish --full-smoke` | recommended pre-upload combination |
| `--i-own-name` | required to upload when the PyPI project already exists (updates of our own project only) |
| `--reuse-dist` | skip the build when `dist/<name>/` already holds matching artifacts |
| `--dry-run` | print every step, mutate nothing (works without `.venv`) |

Makefile alias: `make pypi-publish` (check mode).

### Multi-dist mechanics (codified 5.5.0 train)

For every candidate dist the script rewrites the `[project] name` in
`pyproject.toml` (backup + `EXIT`/`INT`/`TERM` traps), rebuilds into an
isolated `dist/<normalized-name>/` and re-runs the gates. The restore is
verified with `git diff` after every substitution — a failed restore is a
hard error, so `pyproject.toml` can never be left modified, crash or not.
A stale `pyproject.toml.pypi-publish.bak` from a killed run is refused at
startup. Mirror dists share the import package (`vesmaro`) and the CLI
entry points — only the distribution name differs.

### Gates

| Gate | Checks | Hard fail? |
| --- | --- | --- |
| CL | changelog preflight: `[Unreleased]` in CHANGELOG.md holds <3 content lines while commits accumulated since the last tag → `WARN` (never blocks) | never |
| G0 | PyPI name per dist: 404 = free (first publish); 200 + `--i-own-name` = update; target version already on PyPI = always an error | `--publish` mode |
| G1 | HEAD is exactly on a release tag `vX.Y.Z` (once) | `--publish` mode |
| G2 | tag version == `pyproject.toml` version (once) | `--publish` mode |
| G3 | wheel + sdist filename versions == `pyproject.toml` version (per dist) | `--publish` mode |
| G4 | smoke-installed package version == `pyproject.toml` version (per dist) | always (artifact proof) |

G4 verifies the post-rebrand wheel layout: it checks the installed
`vesmaro` package resources (`integrations/`, `scripts/`), not the
deprecated `mnemos` shim.

G0 stays as a hard safety net now that the projects are live: a rebuild of
an already-published version fails cleanly BEFORE any upload attempt
(versions are immutable — bump and rebuild instead), and `--publish`
against a name/version state that does not match our project is refused
without `--i-own-name`.

## Environment canon — bootstrap the release worktree

Bootstrap a release worktree ONLY with the lockfile-driven command:

```bash
uv sync --extra dev          # respects uv.lock — the exact pinned graph
```

Do NOT bootstrap with `uv pip install -e ".[dev]"` (or `uv pip install`
of any dependency): it re-resolves fresh and IGNORES `uv.lock`. On the
5.5.0 train this silently pulled onnxruntime 1.30 (latest) instead of the
locked 1.27.0, broke the bench pin (B1) and drifted the cosine invariant
by 5.4e-4 — an incident that cost a re-run of the quality gate. The
lockfile exists precisely so every worktree builds the same graph; a
deliberate dependency change goes through the
[dependency-updates runbook](dependency-updates.md) and re-locks `uv.lock`
in a reviewed PR.

## npm channel — `@vesmaro/vesma` (`packaging/npm/`)

The npm manifest lives in the repo at `packaging/npm/package.json` (the
legacy root `package.json` — pi-mnemos — is frozen and untouched by this
pipeline). The package is a metadata/marker dist (README + manifest, no
JS code): it feeds the npm version badge and the `vesma update` npm leg.
Publishing is owner-executed, same philosophy as PyPI:

```bash
packaging/npm/publish.sh                    # check: V1 version gate + staging + npm pack + C1 composition gate (no upload)
packaging/npm/publish.sh --compare-registry # + R1: compare local composition vs the published dist metadata (WARN)
packaging/npm/publish.sh --publish          # owner step: R0 registry gate + npm publish (NPM_TOKEN or ~/.npmrc)
```

Composition is deterministic (C1): exactly `package/package.json` +
`package/README.md`, pinned by the `files` allowlist. The published 5.5.0
verified 2026-10-05: `README.md` byte-identical to the repo README
(sha256 `7b8400dd…`), manifest fields equal — the published tarball also
carried a stray `pack-preview.tgz` forgotten from the manual `npm pack`
flow; that accident is NOT reproduced by the codified pipeline (and is
the reason the composition gate exists). Versions are immutable on npm
too: an already-published version refuses `--publish` (R0). Keep
`packaging/npm/package.json` version in lockstep with `pyproject.toml`
per release — V1 enforces it.

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

2. **Checkout the tag and run the full pipeline** (one run publishes both
   live channels — `vesma` + the `vesma-memory-server` mirror):

   ```bash
   git checkout vX.Y.Z
   scripts/pypi-publish.sh --publish --full-smoke
   # if (and only if) a PyPI project already exists and is ours:
   scripts/pypi-publish.sh --publish --full-smoke --i-own-name
   # restrict the channels (rarely needed):
   scripts/pypi-publish.sh --publish --dists "vesma"
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
- `packaging/npm/publish.sh --help` — npm twin pipeline (`@vesmaro/vesma`)
- `scripts/local-release.sh` — container image + GitHub Release half
- [Install runbook](install.md) — first-run operational checklist
- [CI/CD runbook](ci-cd.md) — why builds run locally (billing lock)
- Issue #122, ADR-0017 (docs/project/adr/0017-memory-system-evolution-roadmap.md)
