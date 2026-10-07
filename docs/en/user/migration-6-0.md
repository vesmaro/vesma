# Migrating to Vesma 6.0

**🌐 Language / Язык:** English · [Русский](../../ru/user/migration-6-0.md)

> The 5.6.x → 6.0 migration guide: what breaks, how to move your store with
> `vesma migrate-store`, and how to update configs, environments, MCP wiring
> and Python imports. Read sections 1–2 before touching anything; the mover
> itself enforces a snapshot and a dry-run by default.

Vesma 6.0 is the contract release: the rebrand that 5.0 carried as a
compatibility layer (legacy `mnemos` spellings kept working alongside the new
`vesma` names) is now completed end to end. One command — `vesma migrate-store`
— moves your data store to the new identity; everything else is config and
wiring you update once. The semantics of the move are ratified in
[ADR-0044](../../project/adr/0044-store-migration.md); this page is the
operator walkthrough.

---

## 1. What changes and why

The 5.0 rebrand renamed the product but deliberately kept every legacy
spelling alive: tools answered both `mnemos_*` and `vesma_*` names, env vars
were read under three prefixes, the Python package imported under two names,
and the on-disk store kept its original home. 6.0 ends every one of those
dual spellings — 6.0 is "vesma end to end" at every surface you can touch.

What does **not** change: your records. Content, timestamps, ids and vectors
are preserved byte-for-byte; the mover rewrites only the tag prefix, the
project silo slug, the file names and the config pins — with a verified
snapshot behind it (section 2).

### Breaking changes at a glance

| # | What | 5.6.x | 6.0 | What you do |
|---|------|-------|-----|-------------|
| 1 | Store home | `~/.mnemos/`, `mnemos.db` | `~/.vesma/`, `vesma.db` | Run `vesma migrate-store` (section 2) |
| 2 | Canonical tag prefix | `mnemos:*` stored, `vesma:*` alias | `vesma:*` stored, `mnemos:*` accepted as input | The mover rewrites tags; typing stays free (section 7) |
| 3 | Config YAML section | `mnemos:` | `vesma:` (a leftover `mnemos:` section fails the load loudly) | The mover rewrites it; or rename by hand (section 4) |
| 4 | Environment variables | `MNEMOS_*` / `VESMARO_*` / `VESMA_*` all read | `VESMA_*` only | Rename exports; regenerate completion (section 4) |
| 5 | MCP tool names | `mnemos_*` canonical, `vesma_*` behind a brand flag | `vesma_*` only; `mnemos_*` answers `Unknown tool` | Re-run `vesma integration setup`; update tool allowlists (section 5) |
| 6 | Python package | `import vesmaro` / `import mnemos` | `import vesma` only | Replace imports, reinstall (section 6) |
| 7 | Export schema field | `mnemos_version` | `vesma_version` (import accepts both) | Read the new key; old exports still import (section 6) |
| 8 | Trust marker | `mnemos:no-federate` | **Unchanged** — byte-stable by design | Nothing; do not rename it (section 7, FAQ 3) |

One command per row: rows 1–3 are handled by the store mover, rows 4–7 are
one-time edits on your side, row 8 is intentionally frozen.

---

## 2. Step-by-step store update

The mover is an explicit operator command. It is **never** run automatically
on boot or on upgrade — you invoke it, on a quiet machine, one device at a
time (stagger further machines after the first one proves a full run).

Safety contract, enforced by the command itself:

- **Explicit paths only** — `--from` and `--to` are required; nothing is
  guessed. Omitting `--from` prints suggested candidates and does nothing.
- **Dry-run is the default** — without `--apply` the command only reads and
  reports; the source store stays unchanged.
- **Snapshot gate** — on `--apply`, a SQLite backup-API snapshot is taken
  into `<to>/../migrate-snapshot-<timestamp>/`, verified readable, and kept
  behind as the rollback artifact.
- **Quiesce gate** — a live service socket or a held database lock is a loud
  refusal, never a concurrent migration.
- **No silent fork** — a fresh zero-config install refuses to create a new
  `~/.vesma` while a legacy `~/.mnemos` store still exists.
- **Privacy** — the report prints paths and counters, never record values.

### Step 0 — update the package first

The mover ships with 6.0. Update your install to the 6.0 line and confirm:

```bash
vesma update check
vesma update apply          # or: pip install --upgrade vesma / uv tool upgrade vesma
vesma --version             # expect 6.0.x
```

### Step 1 — back up

The mover makes its own verified snapshot at step 4, but a fresh user-level
backup before any data operation is cheap insurance:

```bash
vesma export -o vesma-backup-pre60.sqlite -f sqlite
```

Full procedures, including point-in-time recovery, live in
[Backup & Restore](../admin/runbooks/backup-restore.md).

### Step 2 — stop the running vesma service

The quiesce gate refuses to migrate a live store. Stop every writing
component (the embedded core and, if installed, the sidecars):

```bash
vesma service stop core
vesma service stop board     # if installed
vesma service stop metrics   # if installed
vesma service status         # confirm: everything stopped
```

`vesma service stop` is idempotent — an already-stopped component is reported
as-is, not an error. If you run `vesma serve` or `vesma mcp-server` manually
instead of the service, stop those processes too.

### Step 3 — dry-run (the default)

```bash
vesma migrate-store --from ~/.mnemos --to ~/.vesma
```

Both paths are required. The target must **not exist at all** — not even as
an empty directory; the mover creates it. Expected output (illustrative
numbers; yours will differ):

```text
migrate-store — mode: dry-run
  from: /home/user/.mnemos
  to:   /home/user/.vesma
  snapshot (on --apply): /home/user/migrate-snapshot-20261007-…

  Records
    total:                 3790
    status=published:      3610
    project=mnemos:        3790

  Planned rewrites (exact prefixes only)
    mnemos:<subtype> -> vesma:<subtype>:  8120
    project:mnemos -> project:vesma:      3790
    duplicate tags removed:               0
    trust markers kept byte-stable:       14

  Files
    vault present: True
    config present: True
```

Read the plan: the record total, the planned tag/slug rewrites and the
kept-marker count. Nothing has been written yet — the source is untouched.

> Omit `--from` on purpose once, if you like: `vesma migrate-store` prints
> the candidate store homes it found (`~/.mnemos`, `~/.local/share/vesma/core`,
> `~/.vesma`) and exits without touching anything. That is the discovery
> contract — suggestions only, never an automatic choice.

### Step 4 — apply

```bash
vesma migrate-store --from ~/.mnemos --to ~/.vesma --apply
```

The mover snapshots the source, materializes the new store **from the
snapshot** in a staging directory, rewrites tags and slugs there, verifies,
and publishes the result to `--to` with an atomic rename. The source home is
left byte-for-byte in place — never renamed, never deleted. Expected report:

```text
migrate-store: applied and verified
  target:   /home/user/.vesma
  snapshot: /home/user/migrate-snapshot-20261007-… (rollback artifact)
  records:  3790
  mnemos:<subtype> -> vesma:<subtype>:  8120
  project:mnemos -> project:vesma:      3790
  duplicate tags removed:               0
  trust markers kept byte-stable:       14

  Config
    source: /home/user/.mnemos/config.yaml
    section mnemos: -> vesma: renamed: True
    paths re-rooted: 3
    db_name pinned to vesma.db: True

  Verification
    record counts equal: True
    id-set digest equal: True
    sample field checksums: 10 checked, 0 mismatched
    FTS rebuilt + integrity ok: True
    sqlite quick_check: ok on all moved databases
```

**Check the report before moving on.** The record count must equal the
dry-run total; every verification line must read `True` / `ok` / `0
mismatched`. The mover refuses to print success on a skewed store — but read
the numbers yourself. Add `--json` to any invocation for a machine-readable
report (paths and numbers only).

After the run, the migration-window checklist (from ADR-0044, manual by
design): recall a known checkpoint, and — if you federate — make one pull
against a live 5.x peer.

### Step 5 — switch the running installation to the new home

The mover writes the canonical `config.yaml` into the new home (section
renamed `mnemos:` → `vesma:`, paths re-rooted, `db_name: vesma.db` pinned).
At this release the built-in default config path is still the legacy one, so
point your installation at the new config explicitly:

```bash
export VESMA_CONFIG=~/.vesma/config.yaml    # persist in your service env/unit
vesma service start core
vesma service health                        # expect OK
vesma doctor                                # full pass over the install
```

`vesma doctor` checks config, database, vault, pending queue, MCP,
integration, completion and the tag contract — every check should report
PASS. Re-run `vesma integration setup` here as well; it refreshes generated
harness files to the 6.0 spellings (see section 5).

### Mover exit codes

Every failure is a typed exit code with a one-line reason on stderr
(`migrate-store: …`). Stable contract:

| Code | Meaning | What to do |
|------|---------|------------|
| 0 | Success (dry-run report or applied+verified) | Proceed to the next step |
| 2 | Usage error — missing/invalid `--from`/`--to` | Re-run with both explicit paths |
| 3 | Discovery refused — `--from` omitted | The candidates are listed in the message; re-run with an explicit `--from` |
| 4 | Quiesce violation — service live / database lock held | `vesma service stop core` (stop writers), then retry |
| 5 | Snapshot failed (created/verify/write error) | Nothing was migrated; check disk space and permissions; retry |
| 6 | Verification failed (post-checks) | Staging was removed, snapshot kept, source untouched; inspect the report, then retry — or restore from the snapshot and file an issue |
| 7 | Already migrated — polite refusal | The target is a migrated store; nothing to do |
| 8 | Invalid source — the path is not a 5.x store | Check `--from` (a `mnemos.db` under `data/` is expected) |
| 9 | Config blocked — unknown keys, schema-rejected keys, or both a `mnemos:` and a `vesma:` section | Fix the named keys in the source config by hand, then retry |

Any exit other than 0 on `--apply` means: no target was published. The
snapshot stays in place, the legacy home stays byte-identical.

---

## 3. What the mover touches — and what it never touches

Rewritten (exact-prefix rewrites only, everything else byte-for-byte):

- tags `mnemos:<subtype>` → `vesma:<subtype>` — in the tags field, the
  denormalized `project` column and the FTS index (rebuilt);
- the main silo slug `project:mnemos` → `project:vesma`;
- `data/mnemos.db` → `data/vesma.db`;
- the config: section `mnemos:` → `vesma:`, `Path`-typed values under the
  old home re-rooted under the new one, `db_name` pinned to `vesma.db`;
  unknown key names abort with exit 9 rather than being silently dropped.

Never rewritten:

- **record content** — titles, bodies, provenance text that happens to embed
  old slugs: preserved byte-for-byte;
- **vectors** — id-keyed, untouched;
- **the trust marker `mnemos:no-federate`** — kept byte-stable; the report
  shows the kept count (section 7);
- **the legacy home `~/.mnemos/`** — left in place as-is; deleting it is
  your decision, after you have confirmed the new store on real workloads.

---

## 4. Machine configs and environment

### Config file (YAML)

Rename the top-level section; every key inside it keeps its name:

```yaml
# 5.6.x                      # 6.0
mnemos:                      vesma:
  data_dir: ~/.mnemos/data     data_dir: ~/.vesma/data
  vault_path: ~/.mnemos/vault  vault_path: ~/.vesma/vault
                               db_name: vesma.db
```

A config that still carries `mnemos:` **fails to load** in 6.0 with a loud
validation error — never a silent fallback to defaults. If you let the mover
produce the config (recommended), this is already done and validated against
the real settings schema; doing it by hand, rename the section key and
re-run `vesma doctor` to confirm the load.

### Environment variables

`VESMA_*` is the only honoured prefix. The 5.x `VESMARO_*` and the 4.x
`MNEMOS_*` spellings are no longer read — silently: a stale export falls
through to the default value.

| 5.6.x | 6.0 |
|-------|-----|
| `VESMARO_API__HOST`, `VESMARO_EXPORT_PASSPHRASE`, … | `VESMA_API__HOST`, `VESMA_EXPORT_PASSPHRASE`, … |
| `VESMARO_FED_PEER_*_URL`, `VESMARO_SYNC_*` | `VESMA_FED_PEER_*_URL`, `VESMA_SYNC_*` |
| `MNEMOS_SYNC_*` (sync-peers.sh shim) | `VESMA_SYNC_*` |
| `VESMARO_CONFIG` / `MNEMOS_CONFIG` | `VESMA_CONFIG` (unchanged, canonical since 5.3) |
| `VESMARO_AUTO_COLLECT`, `VESMARO_UPDATES_CHECK`, `VESMARO_ORT_THREADS`, `VESMARO_OPENROUTER_API_KEY` | the same names under `VESMA_` |

Rules of thumb:

- Nested settings use `VESMA_<SECTION>__<FIELD>` (double underscore). The
  store section's own nested vars are `VESMA_VESMA__*` — e.g.
  `VESMA_VESMA__DATA_DIR`; the short aliases `VESMA_DATA_DIR` and
  `VESMA_VAULT__VAULT_PATH` still work and are the readable spelling.
- Regenerate shell completion after the rename — the old scripts emit the
  dead `_VESMARO_COMPLETE` variable: `vesma completion` (auto-detects the
  shell).
- Re-run `vesma integration setup`: generated env blocks now write canonical
  names.
- Update the environment files used by `sync-peers.sh` to the `VESMA_SYNC_*`
  spellings.

Verify nothing legacy is left: `vesma doctor` (it loads the real config the
way the server would) and a manual `env | grep -iE 'mnemos|vesmaro'` in the
service's environment.

---

## 5. MCP wiring

The MCP server registers the `vesma_*` tool names **only**. Legacy
`mnemos_*` names are gone: a client that allowlists them stops seeing every
tool, and a direct `mnemos_*` call answers `Unknown tool`. The brand-switch
env var is gone with them — the vesma-only surface is unconditional.

1. Re-run the deploy: `vesma integration setup` (idempotent — refreshes MCP
   registrations and agent wiring in place).
2. Update your harness config: tool allowlists and any automation that calls
   tools by name move from `mnemos_*` to `vesma_*`.
3. The full catalogue lives in [mcp-tools.md](mcp-tools.md) — same tools,
   same inputs, new prefix.

---

## 6. Python and API surfaces

If you integrate with Vesma as a library or parse its exports:

| 5.6.x | 6.0 |
|-------|-----|
| `import vesmaro` / `from vesmaro import …` | `import vesma` / `from vesma import …` |
| `import mnemos` (compatibility shim) | removed — `ModuleNotFoundError` |
| `importlib.resources.files("vesmaro")` | `importlib.resources.files("vesma")` |
| `settings.mnemos.data_dir` | `settings.vesma.data_dir` |
| export JSON key `mnemos_version` | `vesma_version` (the importer still accepts the old key) |
| console scripts `vesma` / `vesmaro` / `mnemos` | all three still installed; `vesma` is canonical, the aliases point at the same entry point |

There is no deprecation period for the import names — 6.0 is the hard cut.
Reinstall after upgrading (`uv sync` / `pip install -e .`); wheels build from
`src/vesma` only. Protected and unchanged: the `github.com/vesmaro` org, the
`ghcr.io/vesmaro/vesma` image, the npm scope, the `vesmaro.federation.v1`
wire schema string — these are federation/registry identities, not import
names.

## 7. Tags

The canonical storage prefix is now `vesma:*` — new and migrated records are
stored as `vesma:<subtype>`. You do not have to change how you type: the
legacy `mnemos:` spelling is accepted as an input alias at every surface
(CLI `--tags`, HTTP tag filters, MCP calls) and normalized to the canonical
form on write. Unknown subtypes are refused loudly, under either spelling.
The full contract: [tag-contract.md](tag-contract.md).

Two boundaries to know:

- **Do not hand-roll the store re-slag with `vesma tags rename`.** That tool
  is for other prefixes; the mover is the only path that rewrites the
  subtype namespace together with the FTS index, the silo slug and the
  config, atomically and with verification.
- **`mnemos:no-federate` stays exactly as it is.** The federation exclusion
  marker keeps its legacy spelling indefinitely — it is a security trust
  marker, and its stability across a mixed 5.x/6.0 fleet is the point.
  Typing `vesma:no-federate` is accepted and normalizes to the same stored
  tag.

---

## 8. Docker, Kubernetes, helm

> **Placeholder — lands with the artifact-identity slice of the 6.0 train.**
> The container/image and chart renames (`vesmaro-eyes` → `vesma-eyes` chart
> major v2.0.0, the k8s secret rename, `/etc/vesmaro` → `/etc/vesma`,
> `vesmaro-*` unit names → `vesma-*`) are scheduled for the coordinated
> 6.0 window and are **not** part of this branch yet. Until that lands,
> deploy exactly as documented in
> [Container Deployment](../admin/runbooks/container-deployment.md); this
> section will carry the chart v2.0.0 migration notes when the slice ships.
> The ghcr image coordinates (`ghcr.io/vesmaro/vesma`) are unchanged.

---

## 9. Rollback

Every layer of the migration has a way back:

- **Data.** The snapshot directory `<to>/../migrate-snapshot-<timestamp>/` is the
  rollback artifact, verified before anything was written. The legacy home
  `~/.mnemos/` is never modified by the mover. To go back: point the
  installation at the old home again (the old `VESMA_CONFIG` / config path
  you used before), start the service — the 5.x store is intact.
- **Package.** The 5.6.x releases stay installable from PyPI:
  `vesma update apply --to 5.6.5`, or pin explicitly
  (`pip install vesma==5.6.5` / `uv tool install vesma==5.6.5`). Roll the
  config change back together with the package (restore `VESMA_*` env
  spellings your 5.6.x expects — or simply the old config file).
- **Order.** Package and store roll back as a pair: 5.6.x code does not
  read `vesma:*` tags or `vesma.db`, and 6.0 code reads both spellings — so
  the safe rollback direction is always "old package + old home", before you
  delete anything. Keep the snapshot until the new store has survived a real
  workload window.

---

## 10. FAQ

**Will I lose anything?**
The mover is built so that a half-done migration cannot masquerade as
success: it snapshots first and verifies before publishing; your records'
content is byte-preserved (only the tag prefix, the silo column and the FTS
index are rewritten); record counts, the id-set digest and sample checksums
must match between source and target, or the run refuses to report success.
The legacy home stays untouched as a second, independent copy. The worst
case is a failed run — which leaves you exactly where you started, plus a
snapshot.

**What happens to federation with 5.x peers?**
It keeps working. 6.0 runs a dual-accept window: records tagged `mnemos:*`
arriving from peers are accepted, stored and lazily rewritten to the
canonical spelling; records you send carry canonical tags. The window closes
with the 5.x line (EOL 2026-10-20); legacy-tag acceptance is removed no
earlier than 6.1. Migrate machines one at a time — mixed fleets are exactly
what the window is for.

**Why does the no-federate marker still say `mnemos:`?**
By design, and it is not an oversight to clean up later at will. The marker
is a security boundary: it fences secret-bearing records out of every export
and federated pull. Renaming a trust marker across a fleet where old and new
spellings coexist would turn the boundary into an exfiltration amplifier —
a record could carry `vesma:no-federate` past a 5.x peer that only matches
the old spelling. So the marker is byte-stable, every enforcement site
matches exactly that one spelling, and the mover reports how many markers it
kept. Its rename, if it ever happens, rides an atomic code+data wave — not a
store migration.

**Why doesn't the migration run by itself when I upgrade?**
Deliberately. A data rewrite must be an operator action with an explicit
target, a snapshot and a readable report — never a boot side effect. 6.0
runs fine against a not-yet-migrated store via the input alias; you choose
the quiet window.

**Can I just re-create the store and re-import an export?**
You can, but you should not: the mover preserves checkpoints, vectors and
ids that an export/import round-trip does not fully guarantee. The mover is
also strictly less work — one command, one report.

---

## See also

- [ADR-0044 — store migration semantics](../../project/adr/0044-store-migration.md)
- [Tag Contract](tag-contract.md) — the `vesma:` canon and the input alias
- [CLI Reference](cli-reference.md) — every flag of every command above
- [MCP Tools Reference](mcp-tools.md) — the `vesma_*` catalogue
- [Export & Import](export-import.md) — backup formats and restore
- [Getting Started](getting-started.md) — the full install/service lifecycle
