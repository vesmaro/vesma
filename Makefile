.PHONY: help install bootstrap check-venv test lint lint-shell format typecheck security coverage clean verify doctor security-reminder update-deps build-dist build-image push-image check-version pypi-publish bench-s1 bench-s1-record bench-s4 bench-s4-record bench-s2-smoke bench-s2-nightly bench-s3 bench-s3-record bench-report

# Read version from pyproject.toml — keeps local build targets in sync with the package version.
VERSION := $(shell grep -m1 '^version' pyproject.toml | cut -d'"' -f2)

# Interpreter for benchmark stand targets. Defaults to the repo venv so the
# verify chain is self-contained (#337 review: bare python3 resolves a
# foreign interpreter when the venv is not activated — #335 class).
# Override stays available: make bench-s1 PYTHON=/usr/bin/python3.12
PYTHON ?= $(VENV)/python

help:
	@echo "Mnemos development commands"
	@echo "  make bootstrap  - [DEV] Create .venv and install project (editable) + dev extras"
	@echo "  make check-venv - [DEV] Verify .venv editable install resolves to ./src"
	@echo "  make install    - Install with dev dependencies"
	@echo "  make test       - Run pytest suite"
	@echo "  make lint       - Run ruff linter"
	@echo "  make lint-shell - Run shellcheck on shell scripts"
	@echo "  make format     - Run ruff formatter"
	@echo "  make typecheck  - Run mypy"
	@echo "  make security   - Run bandit + pip-audit"
	@echo "  make security-reminder - Show pinned CVE reminder for manual dependency review"
	@echo "  make update-deps - Upgrade all deps and re-run audit"
	@echo "  make coverage   - Run pytest with coverage"
	@echo "  make bench-s1   - Run the S1 benchmark stand (gate mode, ADR-0020)"
	@echo "  make bench-s1-record - Re-record the S1 baseline + regenerate BASELINE.md"
	@echo "  make bench-s4   - Run the S4 availability stand (BF-2, nightly contour)"
	@echo "  make bench-s4-record - Write the S4 baseline (first record / re-baseline)"
	@echo "  make bench-s2-smoke - Run the S2 timing smoke (informational, never blocks)"
	@echo "  make bench-s2-nightly - Full S2 nightly: R repeats + S1 gate with S1m required (ADR-0020 §5)"
	@echo "  make bench-report - Regenerate the one-page owner report (benchmarks/reports/latest.md)"
	@echo "  make bench-s3   - Run the S3 session stand (BF-3, nightly contour)"
	@echo "  make bench-s3-record - Write the S3 baseline (first record / re-baseline)"
	@echo "  make verify     - Run all checks (lint + typecheck + security + test + bench-s1 + doctor)"
	@echo "  make doctor     - Run mnemos doctor health checks (config, storage, MCP, integration)"
	@echo "  make clean      - Remove build artifacts"
	@echo "  make build-dist - Build wheel + sdist into dist/ (requires: pip install build)"
	@echo "  make build-image - Build container image locally with podman"
	@echo "  make push-image - Tag and push local image to ghcr.io/vesmaro/vesma (requires: podman login ghcr.io)"
	@echo "  make pypi-publish - PyPI pipeline: name+version gates, build, twine check, smoke (upload needs scripts/pypi-publish.sh --publish)"

# Gate targets invoke the repo venv EXPLICITLY (.venv/bin/*), never bare
# tool names through PATH (#337 / #335): a bare `pytest`/`ruff` resolves a
# foreign interpreter when the venv is not activated — green locally, wrong
# build. tests/test_repo_hygiene.py::test_pytest_runs_from_repo_venv is the
# runtime tripwire for the same invariant.
VENV := .venv/bin

install:
	uv pip install -e ".[dev]"

test:
	$(VENV)/pytest tests/ -v --tb=short

lint:
	$(VENV)/ruff check .

lint-shell:  ## Run shellcheck on all shell scripts
	shellcheck scripts/*.sh

format:
	$(VENV)/ruff format .

format-check:
	$(VENV)/ruff format --check .

typecheck:
	$(VENV)/mypy --strict src/vesma/

security:
	$(VENV)/bandit -r src/ -f json -o bandit-report.json || true
	$(VENV)/pip-audit --ignore-vuln CVE-2026-45829

security-reminder:
	@echo "⚠️  SECURITY REMINDER: review the pip-audit output weekly (make security / make update-deps);"
	@echo "⚠️  chromadb and its ignored CVE-2026-45829 left the runtime in NM-1c — remove the"
	@echo "⚠️  stale --ignore-vuln flag below once no environment still carries chromadb."

update-deps:
	pip install --upgrade -e ".[dev]"
	pip-audit

coverage:
	$(VENV)/pytest --cov=src/vesma --cov-report=term-missing --cov-fail-under=80 tests/ -q

check-version:
	@$(VENV)/python scripts/check_version.py

# ── Benchmark stands (ADR-0020) ──────────────────────────────────────────────
# S1 stays in the local merge gate (deterministic corridors + invariants
# vs benchmarks/baselines/s1.json). Re-record only on an event-driven
# trigger (corpus ×2, embedder/model change, issuance-path change) — never
# to make a red gate green.

bench-s1:
	$(PYTHON) benchmarks/stands/s1_quality/run.py

bench-s1-record:
	$(PYTHON) benchmarks/stands/s1_quality/run.py --record

# BF-2 stands — NOT in the local merge gate (ADR-0020: S4 rides the
# nightly contour while within budget; S2 never blocks locally). The
# VESMA_BENCH_S1M_REQUIRED=1 flag for CI nightlies is documented in
# benchmarks/README.md (S1m skip semantics); full CI wiring is BF-4.
bench-s4:
	$(PYTHON) benchmarks/stands/s4_availability/run.py

bench-s4-record:
	$(PYTHON) benchmarks/stands/s4_availability/run.py --record

bench-s2-smoke:
	$(PYTHON) benchmarks/stands/s2_timing/run.py

# BF-4 — full S2 NIGHTLY (ADR-0020 §5, epic #169). Runs on the quiet
# nightly machine ONLY: R full-workload repeats, the between-repeat
# spread is the measured noise band. A band wider than the corridor →
# status NOISE (de-escalated to report + ticket, exit 0); a tight-band
# median breach → REGRESSION (exit 1). The S2 baseline
# (benchmarks/baselines/s2.json) is born HERE ONLY (--record-nightly,
# ≥3 repeats; overwrite needs --force — event-driven re-baseline).
#
# VESMA_BENCH_S1M_REQUIRED=1 is preset for the WHOLE target (review
# N4 on #206): the nightly contour is the only place where the
# required-S1m semantics is mandatory — the S1 gate leg below fails
# red when the production embedder cannot be verified, while the local
# `make verify` posture stays soft (skip tolerated). The env var is
# exported per-invocation, never globally.
S2_REPEATS ?= 5
S2_NIGHTLY_FLAGS ?=

bench-s2-nightly:
	VESMA_BENCH_S1M_REQUIRED=1 $(PYTHON) benchmarks/stands/s1_quality/run.py --quiet
	VESMA_BENCH_S1M_REQUIRED=1 $(PYTHON) benchmarks/stands/s2_timing/run.py --repeats $(S2_REPEATS) $(S2_NIGHTLY_FLAGS)

# BF-4 — the one-page owner report (ADR-0020 §5 gate policy 5): traffic
# light per family F1–F7 from ALL baselines/*.json (bytes, not memory),
# invariants as separate lines, trend arrows vs the previous snapshot.
bench-report:
	$(PYTHON) benchmarks/report_page.py

# BF-3 stand — NOT in the local merge gate (ADR-0020: S3 is nightly
# class, 100–500 turns; the suite carries only a 20-turn determinism
# smoke). Prerequisite of the ADR-0021 nano-refiner gate (NM-2 → NM-3).
bench-s3:
	$(PYTHON) benchmarks/stands/s3_session/run.py

bench-s3-record:
	$(PYTHON) benchmarks/stands/s3_session/run.py --record

verify: format-check lint typecheck test security security-reminder bench-s1 doctor check-version
	@echo "✅ All verification checks passed"

# doctor gate: fail on actual failures (exit 1), allow warnings (exit 2).
# CI environments typically lack agent harnesses, so the integration check
# warns — that is expected and must not break the build.
# Exit codes OUTSIDE {0,1,2} (127 tool missing, 126 not executable, ...)
# are HARD failures (#337 review): the old ladder funneled them into the
# else-branch and printed "all checks passed" for a doctor that never ran.
doctor:
	@$(VENV)/mnemos doctor --json > /dev/null 2>&1; \
	code=$$?; \
	if [ $$code -eq 1 ]; then \
		echo "✗ mnemos doctor: one or more health checks FAILED"; \
		$(VENV)/mnemos doctor; \
		exit 1; \
	elif [ $$code -eq 2 ]; then \
		echo "⚠ mnemos doctor: warnings only (non-blocking)"; \
	elif [ $$code -eq 0 ]; then \
		echo "✓ mnemos doctor: all checks passed"; \
	else \
		echo "✗ mnemos doctor: tool did not run (exit $$code; 127=not found in .venv, 126=not executable) — run: uv sync / make bootstrap"; \
		exit 1; \
	fi

bootstrap:
	@echo "🔧 Creating .venv and installing mnemos (editable) + dev extras..."
	python -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -e ".[dev]"
	@echo "✅ Bootstrap complete — activate with: source .venv/bin/activate"

check-venv:
	@if [ -x .venv/bin/python ]; then \
		.venv/bin/python -c "import vesma, pathlib, sys; got=pathlib.Path(vesma.__file__).resolve(); want=(pathlib.Path.cwd()/'src/vesma/__init__.py').resolve(); sys.exit(0 if got == want else 1)" \
			&& echo "✅ .venv editable install resolves to ./src" \
			|| { echo '⚠️  .venv is stale: vesma does not import from ./src (project moved or venv built elsewhere). Run: make bootstrap'; exit 1; }; \
	else \
		echo "ℹ️  No .venv found — run: make bootstrap"; \
	fi

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache dist/ *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete

# --- Distribution & container -----------------------------------------------

build-dist:
	# Requires: pip install build  (not in dev extras).
	python -m build

build-image:
	podman build -t localhost/mnemos:$(VERSION) -t localhost/mnemos:latest -f Containerfile .

push-image:
	# Run `make build-image` first to ensure the local image exists.
	# Requires: podman login ghcr.io  (credentials are NOT embedded here).
	podman tag localhost/mnemos:$(VERSION) ghcr.io/vesmaro/vesma:$(VERSION)
	podman tag localhost/mnemos:latest ghcr.io/vesmaro/vesma:latest
	podman push ghcr.io/vesmaro/vesma:$(VERSION)
	podman push ghcr.io/vesmaro/vesma:latest

pypi-publish:
	# Check mode: name+version gates, wheel/sdist build, twine check, metadata smoke.
	# NO upload — first publish + final name are owner decisions.
	# Flags (--full-smoke, --publish, ...): scripts/pypi-publish.sh --help
	@bash scripts/pypi-publish.sh


# ── local CI / release (GitHub Actions billing-locked — memory ef56d3b5) ──
# local-ci.sh replicates .github/workflows/ci.yml verify job locally.
# local-release.sh replicates .github/workflows/release.yml (build + push image + GitHub Release).
# See scripts/local-ci.sh and scripts/local-release.sh for details.

local-ci:
	@bash scripts/local-ci.sh

local-ci-build:
	@bash scripts/local-ci.sh --build

local-release:
	@bash scripts/run-release-local.sh

local-release-dry:
	@bash scripts/run-release-local.sh --dry-run

local-release-no-image:
	@bash scripts/local-release.sh --no-image
