# Vesma — project rules for coding agents

Operational rules proven by incidents and wave practice. Each rule carries its origin —
read it as a constraint, not a suggestion. Product canon lives in [CONTRIBUTING.md](CONTRIBUTING.md)
(setup, quality gate, flow to main); architecture in [docs/project/adr/](docs/project/adr/README.md).

## Branches, merges, parallel sessions

- Multiple sessions work the same `main` daily. Before ANY merge: `git fetch`, then
  `merge --ff-only origin/main`, then merge your branch `--no-ff`. Resolve CHANGELOG
  conflicts by KEEPING both parties' [Unreleased] entries — entries are additive, never
  re-edited.
- A checkout may sit on a foreign branch (release prep). Before any merge operation,
  check `git branch --show-current`; merge in a dedicated worktree (`wt/tmp-*`), delete
  it after. Never reset a foreign branch (2026-10-06: 5.5.1 prep held the main checkout
  for hours).
- Reserve doc/ADR/spec numbers against fresh `origin/main` right before writing —
  parallel sessions take the same number at the same time (2026-10-06: two ADR-0037s
  landed hours apart; the incoming renumber `0037→0038` cost a re-link pass).
- Delete a merged branch only after `git merge-base --is-ancestor <branch> origin/main`.
- A release train may run DIRECTLY on main (2026-10-06: 5.6.3 rode main with in-flight
  red gates — version guard 5.6.2≠5.6.3, mypy on the hotfix). If `origin/main` has an
  OPEN train (a release commit without its tag on origin, or red gate tests), DO NOT
  push your stack into the middle of it: hold your merged worktree, wait for the train
  to close (tag + channels), then merge the closed main and push (2026-10-06 precedent:
  the wait cost minutes and avoided publishing the neighbors' red WIP).

## Test gates (TL-verified, 2026-10-06)

- Full suites run SEQUENTIALLY, one merge-gate at a time. Parallel suites produce
  load flakes — case in point: `tests/test_service_pid1_container.py`
  (`test_pid1_true_container_leg_sigterm_graceful_stop_exit_zero`) fails under a
  concurrently running suite and passes solo; numbers must add up exactly
  (5981/8 baseline on merged main after the 2026-10-06 wave).
- `make verify` (ruff check + format + mypy strict + suite) is the minimum gate for
  any code wave; the TL re-runs the suite personally and compares numbers byte-for-byte
  with the executor report — a mismatch is a finding, not a rounding error.
- Worktrees: own `.venv` via `uv sync --extra dev` ONLY (`uv pip install -e` silently
  drifts locked pins and broke the B1 benchmark once); proto stubs are gitignored —
  run `PYTHON="$(uv run which python)" bash scripts/gen-proto.sh` before the first suite.
- Use `-p no:cacheprovider` when disk pressure is suspected; pytest cache writes have
  killed suite runs on a full disk.

## Release ritual (owner-visible surfaces)

- All four channels are released synchronously (PyPI + npm + GitHub Release + ghcr
  image) — the image phase is MANDATORY in the canonical train entry
  (`scripts/pypi-publish.sh --publish`); a post-PyPI push failure = loud
  RELEASE INCOMPLETE + catch-up, never a silent skip (2026-10-05 directive).
- Local post-release install steps target the canonical path EXPLICITLY
  (`~/.local/bin/vesma update apply`), never PATH-resolved binaries: agent-shell
  PATH used to resolve into a foreign product's venv and a train step landed
  vesma 5.6.0 inside it, breaking that product (2026-10-06 bathys incident).
- Environment isolation is a hard rule (owner directive 2026-10-06, global rule 8
  in the owner's zcode AGENTS.md): products never share virtualenvs; installers
  APPEND to PATH; config MCP entries point at canonical paths; check a foreign
  venv's dependency pins before writing into it (bathys `mcp<2` vs vesma `mcp>=2`
  are incompatible by definition).

## Security-class surfaces (cascade review trigger)

- Memory rows and metadata: server-minted keys (`checkpoint_*`, `pipeline_retry_*`,
  `canon`, `canon_warnings`, doc-sweep stamps) are stripped from client create/update
  paths BEFORE merge-back, and import surfaces strip them too — the mint-protection
  class (CVE-class CWE-346) is owned by `MemoryManager.add/update` +
  `cli/import_.py` (issue #432 closed 2026-10-06). Any new server-minted metadata key
  joins the strip class in the same wave, with a pin test per surface.
- Graph tools: secrets-redacted output, project-scoped resolvers, poisoned-file
  refusal; the walk verdict (ADR-0038) adds a to_id project-prefix check as a
  REQUIRED invariant of any new walk code.
- A security spot-check review is mandatory before merge for: metadata/stamp paths,
  trust boundaries, lifecycle/availability code — per the owner's cascade-review
  directive; executor refutations of review findings are verified personally (read
  the code, not the summary) before acceptance.

## Reporting and coordination

- Reports to the owner: Russian, plain language, internal codes expanded at first use;
  the checkpoint board (per line: status/next) opens the report, «ждут владельца» closes
  it — decisions only, one per line, with a recommendation.
- Findings become tracked cards/issues in the same cycle; a closed wave with an open
  finding is not done.
- Change only your wave's zone; zone conflicts (`src/vesmaro/service/**` was the
  service session's zone, `src/vesmaro/cli/**` the CLI zone) are coordinated via the
  task board, not by silence.