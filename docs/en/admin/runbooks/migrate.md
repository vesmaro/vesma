# Runbook: Migrate from ai-brain

**🌐 Language / Язык:** English · [Русский](../../../ru/admin/runbooks/migrate.md)

## Overview

Migrate your existing ai-brain data (SQLite DB + vault) into Vesma format.

## Before you start

1. **Backup your ai-brain data**:
   ```bash
   cp -r ~/.ai-brain ~/.ai-brain.backup-$(date +%Y%m%d)
   cp -r ~/brain-vault ~/brain-vault.backup-$(date +%Y%m%d)
   ```

2. **Install Vesma** (see `install.md`).

## Dry run

Always run dry-run first to see what will be migrated:

```bash
vesma migrate from-ai-brain --dry-run
```

Output shows:
- Number of memories to migrate
- Number of vault files to copy
- Any anticipated errors

## Full migration

```bash
vesma migrate from-ai-brain
```

This will:
1. Back up existing Vesma DB (if any)
2. Migrate all memories with Vesma tag contract applied in **lax mode**
3. Copy vault files preserving directory structure
4. Map ai-brain sources → Vesma sources (telegram → mcp)

## Tag contract handling

Legacy ai-brain entries without `project:` / `agent:` / `vesma:` tags get:
- `project:legacy`
- `agent:unknown`
- `mnemos:legacy`

After migration, review and retag important entries:

```bash
vesma search legacy --tags project:legacy --limit 50
```

## Migrating `gcw:` tags → `vesma:` tags

If your store contains memories with the legacy `gcw:<subtype>` tag prefix
(from the pre-2.7.8 GCW agent family), rename them in bulk to the canonical
`vesma:<subtype>` prefix using the safe `tags rename` command:

```bash
# Dry-run first — preview the change, nothing written (default)
vesma tags rename --from gcw: --to mnemos: --dry-run

# Apply the rename
vesma tags rename --from gcw: --to mnemos: --no-dry-run
```

Notes:
- `validate_tag_contract()` already auto-migrates valid `gcw:<subtype>` →
  `vesma:<subtype>` on read, so `gcw:` tags are accepted as an alias. The
  bulk rename is a one-time housekeeping step to canonicalise the stored tags.
- Invalid `gcw:` subtypes (not in the whitelist) are skipped by default and
  counted in `skipped_invalid`. Pass `--invalid-to-legacy` to rename them to
  `mnemos:legacy` instead of skipping.
- The operation is **idempotent** — a second run reports `renamed=0`.
- The deprecated `vesma migrate tags` command now delegates to this safe path
  and emits a deprecation warning. Prefer `vesma tags rename` directly.

## Post-migration checklist

- [ ] `vesma stats` shows expected memory count
- [ ] `vesma search "hello"` returns results
- [ ] Vault files visible in `~/.mnemos/vault/`
- [ ] MCP server `mnemos_recall_context` works

## Upgrading across an embedder weights change

When an upgrade ships new bundled embedder weights (e.g. the round-3
`vesma-embed-v1` swap, `weights_sha256 3b752e06…`), your existing vectors
were produced by the OLD geometry. Nothing needs to be done manually:

1. On upgrade, the background heal sweeper detects every vector whose
   stored embedder fingerprint no longer matches the current one and
   re-embeds those rows in bounded batches — the migration is gradual
   and automatic (start the processor: `vesma processor start`).
2. `vesma doctor` shows the progress in the **Vector store** row
   ("N cut by another embedder"); the count drops to zero as the
   sweeper drains the `refined` rows. Orphan vector rows of deleted or
   never-refined memories may keep the count above zero — they are
   diagnostics-only (doctor reports them; `vesma reindex` does not
   clear orphans).
3. To rebuild in one pass instead of waiting for the background cycles:

   ```bash
   vesma reindex
   ```

Until the migration drains, vector search mixes two embedding spaces,
which can slightly degrade semantic ranking; full-text search is
unaffected.

## Rollback

If something goes wrong:

```bash
# Restore from Vesma backup
ls ~/.mnemos/data/*.backup-*
cp ~/.mnemos/data/mnemos.db.backup-YYYYMMDD-HHMMSS ~/.mnemos/data/mnemos.db

# Or start fresh
rm -rf ~/.mnemos/data ~/.mnemos/vault
```
