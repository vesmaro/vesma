# Agent Review Protocol (agent-driven merges)

**Status:** Active (owner authorization 2026-08-22, this repository only)
**Scope:** every agent-driven merge to `main` in vesmaro/vesma
**Why it exists:** branch protection requires 1 approving review, but the
repository has a single GitHub identity — the account that opens a PR cannot
approve it. Until a GitHub App reviewer bot exists, agent merges use the
admin merge path. This protocol is the compensating control that must pass
before any admin merge.

## The gate

An agent-driven merge to `main` is allowed **only** when all three steps are
done and visible on the PR:

1. **Review.** Code slices are reviewed by the agent code reviewer
   (`gcw-code-reviewer`, quick or deep mode by slice size). Docs-only
   slices are reviewed by the orchestrating Tech Lead.
2. **Posted verdict.** The review verdict is posted on the PR as a comment
   starting with `[agent-review]`, containing: the verdict
   (approve / approve-with-notes / request-changes), key findings, and who
   independently verified the result.
3. **Independent verification.** The Tech Lead verifies the merge result
   against the target branch **by content markers** (files exist, symbols
   present, acceptance tests pass on the merged tree) — never by commit
   history or tree-SHA equality alone (see incident 2026-08-22: server-side
   merge trees passed a history/SHA check while wiping two merged slices).

If any step is missing, the merge is blocked (`blocked: agent-review gate`).

## Standing prohibitions

- **No server-side merge-tree construction** (Git Data API `trees`/`commits`
  assembly) while a working local git is available. Real three-way merges
  happen locally; the server only transports the result. GitHub-native
  squash merges via `PUT /pulls/{n}/merge` are fine — GitHub computes the
  tree.
- **No force-push** without explicit owner authorization in the current task.

## Upgrade path (optional, owner-only)

A GitHub App reviewer bot (bot identity + `CODEOWNERS` + "require review
from Code Owners") would make the branch-protection rule natively
satisfiable and remove the need for admin merges. Requires one-time owner
setup: create the App, install it on this repo, provide the key. Nothing in
this protocol blocks that upgrade; the `[agent-review]` comment then becomes
the bot's approval.
