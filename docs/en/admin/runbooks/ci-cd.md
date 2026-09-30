# CI/CD Runbook

**🌐 Language / Язык:** English · [Русский](../../../ru/admin/runbooks/ci-cd.md)

> **Scope**: How to operate, debug, and extend the GitHub Actions CI pipeline
> for Vesma. Source of truth: [`.github/workflows/ci.yml`](../../../../.github/workflows/ci.yml).

---

## Pipeline overview

The CI workflow (`.github/workflows/ci.yml`) runs on every push to `main`,
every pull request targeting `main`, and on a weekly drift check
(Monday 06:00 UTC). It has two jobs:

| Job | Runner | Purpose |
|---|---|---|
| `verify` | `ubuntu-latest`, Python 3.11 / 3.12 / 3.13 matrix | Lint + format + mypy + bandit + pip-audit + pytest + coverage |
| `build-container` | `ubuntu-latest` (rootless buildah) | Smoke-test the `Containerfile` builds and `vesma --help` works in-image |

The `verify` job is the **required status check** for `main` (see
[Branch protection](#branch-protection)).

---

## Local validation

Run the same gates locally before pushing to save CI minutes:

```bash
cd /path/to/vesma   # repo root
source .venv/bin/activate

ruff check src/ tests/                                # lint
ruff format --check src/ tests/                       # format
mypy --strict src/vesmaro/                             # types
bandit -r src/ -f json -o bandit-report.json          # security (static)
pip-audit --ignore-vuln CVE-2026-45829                # security (deps)
pytest tests/ -q --tb=short                           # tests
pytest --cov=src/vesma --cov-fail-under=80 tests/ -q # coverage gate
```

The single-shot equivalent:

```bash
make verify
```

If the local gate is green, the CI gate will be green. If CI is red and
local is green, the difference is almost always **environment** — Python
patch version, OS libs (e.g. sqlite), or pip resolver behavior.

---

## Reproducing CI locally with `act`

[`act`](https://github.com/nektos/act) runs GitHub Actions workflows in
Docker locally. It will not be byte-identical to GitHub-hosted runners
(it uses a smaller base image), but it catches most workflow-syntax
mistakes and dependency-resolution issues before you push.

```bash
# Install
brew install act              # macOS
sudo apt install act          # Debian/Ubuntu (often older — prefer binary)

# Default runner is a small image; use 'medium' for closer parity:
act -j verify --matrix python-version:3.12
```

If `act` fails on the `build-container` job, run the same steps
manually — `buildah` is available from `apt` on most distros and the
smoke test is just `vesma --help` inside a built image.

---

## Branch protection

> ⚠️ This is **not** enforced by the workflow — it must be set via the
> GitHub repository settings (Settings → Branches → Branch protection
> rules → `main`).

Recommended settings for `main`:

| Setting | Value |
|---|---|
| Require a pull request before merging | ✅ |
| Required approving reviews | **1** |
| Dismiss stale pull request approvals when new commits are pushed | ✅ |
| Require review from Code Owners | ❌ (no `CODEOWNERS` yet) |
| Require status checks to pass before merging | ✅ |
| Require branches to be up to date before merging | ✅ |
| Required status checks | `Lint + Test + Type + Security (Python 3.12)` |
| Require conversation resolution before merging | ✅ |
| Require signed commits | ❌ (too friction for now) |
| Require linear history | ✅ (squash-merge) |
| Include administrators | ✅ |

The required status check is the **middle** matrix entry
(`Python 3.12`) on purpose: it's the version we develop against and
the one Codecov uploads from. All other matrix entries + the
container job are informational — they fail loudly on PRs but don't
block merge on their own.

To apply via the GitHub UI: Settings → Branches → Add rule → branch
pattern `main` → enable the above. The Terraform equivalent is in
the platform repo (out of scope for this slice).

### Why we don't enforce all three Python versions

Enforcing all three matrix versions as required checks would block
merges whenever one of them breaks for a reason that doesn't affect
production (3.12 is the `Containerfile` baseline and the version we
ship). The other two matrix entries surface as red ❌ on the PR — we
treat a 3.11 or 3.13 regression as a release blocker and fix it
before the next release, but we don't block day-to-day work on it.

---

## Coverage

- Threshold: **80%** (`--cov-fail-under=80`).
- Uploaded to Codecov only on Python 3.12 to avoid three duplicate
  uploads per run.
- Codecov is optional — the action fails gracefully if
  `CODECOV_TOKEN` is unset (`fail_ci_if_error: false`).

### Why the gate is at 80% (not 100%)

The remaining gap is concentrated in:

1. `src/vesmaro/llm/*.py` — provider adapters with thin pass-through
   to vendor SDKs (anthropic / openai / gemini / ollama). High
   coupling to vendor HTTP error shapes makes a real e2e test
   expensive.
2. `src/vesmaro/watchers/` — filesystem event handlers; covered by
   unit tests but not by in-process end-to-end flows.
3. `src/vesmaro/auto_collect.py` — the auto-collect cron path is
   exercised manually, not in CI.

Each of these has a follow-up issue. Until they're closed, the
80% gate is the deliberate floor.

---

## Dependabot

Configuration: [`.github/dependabot.yml`](../../../../.github/dependabot.yml).

| Ecosystem | Schedule | PR limit | Labels |
|---|---|---|---|
| `pip` | Weekly, Monday 06:00 UTC | 5 | `dependencies`, `security` |
| `github-actions` | Weekly, Monday | 5 | `ci`, `dependencies` |

Patch + minor updates are grouped into a single PR per run to reduce
reviewer load. Major-version updates are intentionally excluded for
`aiohttp` and `starlette` — those pins exist to close transitive CVEs
([ADR-0008](../../../project/adr/0008-sql-injection-via-fstring.md) family) and
require the runbook in
[`dependency-updates.md`](dependency-updates.md) to bump safely.

If Dependabot opens a PR that violates the pinned-version policy in
`pyproject.toml` (e.g. tries to push `aiohttp` past `4.0`), close it
and re-bump manually per the dependency-updates runbook.

---

## Container build job

The `build-container` job uses `buildah` (rootless, no daemon) instead
of Docker to avoid the privileged-container requirement on GitHub-hosted
runners. Steps:

1. `apt-get install buildah`
2. `buildah bud -t mnemos:test .` — builds the `Containerfile`
3. `buildah from --name vesma-test mnemos:test` — starts a container
4. `buildah run vesma-test -- python --version` — smoke test

> The smoke step runs `vesma --help` (plus a Python version print) inside
> the built image, so it validates the CLI entrypoint, not just the base image.

If the container job fails, inspect the log for:

- **Layer-cache busts on `pip install`** — usually a transient PyPI
  issue. Re-run the job.
- **`buildah bud` permission errors** — happens on the
  `ubuntu-22.04` runner image once in a while; the `ubuntu-latest`
  pin avoids this in 99% of runs. If persistent, switch the runner
  to `ubuntu-24.04` explicitly.

---

## Debugging failed runs

1. Open the failed run on GitHub Actions.
2. Find the step that failed. Each step's log is collapsible —
   expand it.
3. The most useful steps when something flakes:
   - `Security (pip-audit)` — `pip-audit` is sensitive to advisory
     DB freshness. If the only failure is a NEW CVE, check the
     `pyproject.toml` pins and the dependency-updates runbook.
   - `Test (pytest)` — scroll up; the assertion is usually a few
     hundred lines above the summary.
   - `Coverage check` — if the only failure is the threshold, look
     at the `term-missing` report in the same step. It lists which
     lines aren't covered.

### Re-running a job

Use the **"Re-run jobs"** button in the GitHub UI. If the failure
was a flake (network, transient), this is the right button. If the
failure is real, fix the code first — never re-run as a substitute
for fixing.

### Downloading artifacts

The `bandit-report-pyX.Y` artifact is uploaded **only on failure**.
Download it from the run summary page → Artifacts section. The
artifact retention is 7 days.

---

## Adding a new step to the `verify` job

Edit `.github/workflows/ci.yml`. The new step goes after the existing
lint/format/type/security block and before the test step. The
convention:

1. Use `source .venv/bin/activate &&` so the step runs in the
   project venv (uv-installed deps live there).
2. Cache nothing — let `setup-python@v5` cache `pip` deps at the
   cache step. The workflow already pins to the right `pyproject.toml`
   extras, so adding the tool means adding it to `[project.optional-dependencies].dev`.
3. If the step produces a report (like `bandit-report.json`), upload
   it as an artifact with the `if: failure()` guard so the artifact
   only appears on failure.

After editing, validate locally:

```bash
python -c "import yaml; yaml.safe_load(open('.github/workflows/ci.yml'))"
```

Then push to a feature branch and confirm the green check on a draft PR
before merging.

---

## Out of scope (today)

- **CD / deploy** — the release pipeline lives in
  [`.github/workflows/release.yml`](../../../../.github/workflows/release.yml):
  a `v*.*.*` tag builds the wheel/sdist and attaches them to the GitHub
  Release, and pushes `ghcr.io/korrnals/vesma:$VERSION` + `:latest`.
  PyPI upload is run separately per [`pypi-publish.md`](pypi-publish.md);
  container use is covered by [`container-deployment.md`](container-deployment.md).
- **Self-hosted runner** — not needed at this scale. GitHub-hosted
  `ubuntu-latest` is fast enough and the concurrency group keeps
  costs in check.
- **Matrix on OS** — Debian/Ubuntu only. The project doesn't target
  Windows or macOS, so no `runs-on:` matrix is needed.
- **Codecov dashboard gating** — the 80% floor is enforced by
  `pytest --cov-fail-under`, not by Codecov's status check. This
  keeps the gate working even if the Codecov token is missing.
