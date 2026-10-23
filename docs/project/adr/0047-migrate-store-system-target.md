# ADR 0047: `migrate-store --target=system` — the system-target contract

**Status:** Accepted — ArchCom 2026-10-22 (the layout v2 wave-5 session;
the verdict is this document: positions Product / Security / DBA — all
three ACCEPT-with-amendments, folded inline; the amendments are the C1–C15
points below). Implementation wave: `feat/layout-v2-wave5-mover-system`.
The C13 activation gate keeps production use locked until the round-trip
soak closes AND the owner gives the word (ADR-0045 Decision 9) — this ADR
ratifies the CONTRACT, not a production cutover.

**Deciders:** ArchCom 2026-10-22 — Tech Lead (chair), Product Architect,
Senior Security Engineer, Senior DBA; Senior System Engineer implementing.
The TL decision on the install-state dispute (C12) is recorded inline.

**Scope:** the target contract of `vesma migrate-store --target=system` —
how the existing B3 mover ([ADR-0044](0044-store-migration.md)) relocates a
store INTO the installed system profile of
[ADR-0045](0045-dual-profile-fhs-layout.md): target resolution, cargo
boundary, conffile discipline, ownership, piecewise publish, source
protection, target quiesce, verification, snapshot placement, secrets
invariants, the install-state non-interaction, the activation gate, the
fast path, and the refusal taxonomy. Out of scope: the production cutover
itself (locked), the v1.5 → v2 user consolidation vehicle (a different
wave), the system→user reverse relocation as a CLI surface (exists only as
an internal seam for the round-trip machinery), and any tag-namespace
change (the B3 semantics are inherited unchanged, C11).

**Preconditions:** the B3 mover merged and green (ADR-0044, 2026-10-08);
the system profile implemented through ADR-0045 rollout waves 1–3 (resolver,
`install --system`, doctor DR-14…DR-19, symlink-swap updater); the FHS
manifest as the single path authority (`vesma.service.fhs`).

## Context

ADR-0045 reserved `--target=system` and deferred its contract to its own
ratification "at activation time". The mover exists and carries proven
semantics for the 5.x → 6.0 namespace move — but relocating a store into a
root-owned FHS tree is a different risk shape: the target is resolved (not
typed), the operator runs as root against a user-owned source, the store
home IS the data root (no single atomic rename), a live system service can
race the publish, and a system conffile may already exist. The 2026-10-22
committee ratified the contract below; every amendment resolved toward the
safer or an equivalently safe variant, mirroring the ADR-0044 discipline.

## Decision

The fifteen contract points, normative, each pinned by a test in
`tests/test_store_migration_system.py`:

**C1 — Placement.** The target resolves ONLY through the system
install-state + the FHS manifest resolvers; zero hardcoded system paths in
mover code (a source-scan pin enforces the absence of the canonical
literals). Preconditions, fail-fast BEFORE any write (snapshot included):
install-state exists + parses + `profile: system`; DR-14 (source home and
the data root are distinct roots); DR-17 clean; the data root is fresh of
store content (no `vesma.db`/`mnemos.db`/`data/`/`vault/`/the mover
marker); quiesce (C7). The generic «`--to` must not exist» rule NARROWS to
«store content in the target exists» — installer scaffolding is allowed. A
missing profile is a typed refusal with the remediation
(`sudo vesma service install --system` → stop the service → re-run);
AUTO-INSTALL there is none (committee unanimous).

**C2 — Cargo.** The mover carries the STORE ONLY: the memories database,
its data siblings, `vault/`, and the config. env.d secrets, components.d
manifests, component data, venvs and unit links are never its cargo; the
honest report + the dry-run advisory list what remains, with owner
commands (resolved through the manifest, never literal paths).

**C3 — Conffile.** An existing NON-EMPTY system conffile is never
overwritten and never silently merged: the incoming config lands beside it
as `config.yaml.migrated-<ts>` with a loud operator callout. An empty or
absent conffile gets the canonical write. (Same refusal class as the B3
«both sections» case, which is inherited unchanged.)

**C4 — Ownership.** Staging is created 0700 → verification → a
chown/chmod pass BY FHS MANIFEST ROLES (never a blanket recursive chown:
the data tree takes the `data_root` role, staged files the `store_db`
role, the config the `shared_config` role — modes/owner/group read from
the manifest rows, wider than the manifest never) → fsync → publish → a
post-publish stat audit (owner + mode) that is part of verification. The
mover's hard 0700/0600 floor stays the USER-target property only. A
non-root run records the intended ownership and says so; its audit checks
modes (the installer's dest-dir honesty).

**C5 — Publish.** Store home == data root, so a one-rename publish is
impossible: publish is PIECEWISE. A fresh target-side quiesce gate runs
immediately before it; the finally-path rolls published entries back in
reverse on ANY interruption (a KeyboardInterrupt keeps its type), removes
staging, and leaves the snapshot as the rollback artifact. The soak
checklist owns a mid-publish interruption drill; the pytest pin exercises
the same path.

**C6 — Source untouched.** Not one write into the source (no
`wal_checkpoint(TRUNCATE)` against it — rollback philosophy plus
`SQLITE_BUSY` risk). Every source probe on a system run goes through
read-only URIs — the ratified rule keyed on `euid != owner`, implemented
UNCONDITIONALLY for the system target because an owner-equivalent
read-write open would legally checkpoint-and-remove a WAL, which is still
a source mutation (the stricter reading of the C6 header). A stale
sidecar requiring recovery is a typed refusal — repair is the owner's job
(modern SQLite heap-memory recovery rarely surfaces it; the classifier is
pinned defensively). The pin: source tree mode/uid/gid/bytes identical
before/after.

**C7 — Target quiesce.** Triple gate: (1) `systemctl is-active
vesma.service` — active/activating is FATAL (exit 4,
`QuiesceViolationError`); a merely enabled unit is a warning
(timer/socket activation); (2) the system control socket exists — warning
only (a NEW constant resolved through the manifest, never the user-scope
resolver, never the frozen 6.0 source socket); (3) the DECISIVE leg —
`BEGIN IMMEDIATE`/`ROLLBACK` + WAL sampling over every sqlite already
under the data root (component data included) is FATAL. Double run: in
the plan and immediately before publish — symmetric to the source side's
two-phase gate.

**C8 — Verification.** «Bit-identity» means LOGICAL equivalence: byte
equality of the main db is NOT a gate (the backup API legally reshapes
page layout). Inherited: record counts, id-set digest, status/silo
breakdowns, seeded per-field samples (15 fields; 100 samples above a
million rows), FTS parity, `quick_check` on every moved database. New for
the system target: per-file sha256 parity of every non-sqlite file, per
-table `count(*)` parity of every sibling database, and the post-publish
ownership/mode stat audit. Verification runs strictly pre-publish; the
stat audit closes it post-publish.

**C9 — Snapshot.** Default placement is NEXT TO THE SOURCE (operator
owned, 0700 — inspected and deleted without sudo); explicit
`--snapshot-dir`; placement inside the source or the target trees — and
under the system roots or their parents — is refused. A foreign-source
run hands the snapshot to the source owner (best effort, loudly warned).
The path is in the report.

**C10 — Keys.** Post-migration invariants: the memory vault is readable
(plaintext markdown — sampled), agent tokens verify without a re-mint
(the signing key travels with the data siblings — presence verified). The
TOTP blobs stay decryptable ONLY with `VESMARO_API__TOTP_MASTER_KEY`
provisioned in the system env.d (0600, env-only — the key never lives in
a store home): a printed precondition in the dry-run and the report, plus
an env-dir existence advisory — never an assumption. Out-of-home secret
-path config overrides (e.g. `federation.agent_token_key_path`) are
reported as «not carried» by KEY NAME; values never print (privacy
contract).

**C11 — Inherited gates.** Record-count/id-set equality and the
`mnemos:no-federate` byte-stable trust marker are REQUIRED for the system
target too; the B3 pins continue to apply unchanged.

**C12 — Install-state.** The mover does NOT write the store into the
install-state and creates no installer artifacts (TL decision of the
committee dispute: a recorded store would make the exact-inverse
uninstall DELETE it silently — data-loss by default, against the P0
spirit; an unrecorded store must surface to the operator instead). The
safety property as implemented: the data root is recorded
preserve-by-design (chairman ratification 2026-10-20), so an uninstall
after a migration PRESERVES the migrated store — pinned by test.
*Implementation note (open with the TL):* the committee summary phrased
the mechanism as «DR-17 unrecorded-content → uninstall refuses loudly»;
in the current installer shape the data-root row is
`preserve`/`skip`, so the doctor's drift walk does not visit it and the
uninstall does not refuse — it PRESERVES. The invariant that matters
(never silently delete a migrated store) holds through preservation; the
refusal phrasing stays true only for future strict-dir slots. If the
committee wants the loud-refusal shape, that is an installer-wave
amendment, not a mover change.

**F9 — uninstall adoption of the migrated conffile (installer wave,
2026-10-23).** The ONE deletion path beyond the recorded artifacts: when
every evidence check holds — the fhs-resolved `shared_config` path, an
install-state saying `profile: system`, a marker conffile record naming
exactly that path, the mover provenance header on the file, and a
sha256 match — the exact-inverse uninstall may unlink the mover-published
config (recorded-artifact discipline, kind=file; two-phase: validated at
validate, re-hashed immediately before unlink), with a loud
`adopted-from-migrate-store` report line naming the recoverability
pointers (source home + snapshot). Scope is EXACTLY the `shared_config`
role: sidecars (`config.yaml.migrated-*`), `env.d/`, `components.d/`
content always refuse — forgeable-by-service-account evidence must never
widen to secrets zones. **The predicate asymmetry is INTENTIONAL:**
`system_state_drift` tolerates ANY operator content in the conffile zone
(the manifest says the zone is operator-provisioned), while uninstall
adopts ONLY mover-stamped bytes — tolerance is a diagnosis posture,
adoption is a deletion decision, and deletion demands stronger evidence.
Heads-up for operators: content you place in `env.d` (e.g. the TOTP
master key, C10) makes `uninstall --system` REFUSE by design — remove it
explicitly first; that is the secrets zone refusing to be swept by a
conffile-grade evidence class.

**C13 — Activation gate.** `--apply` for `--target=system` REFUSES (exit
15) until the round-trip soak closes green AND the owner gives the word
(ADR-0045 Decision 9). The dry-run — plan + the DR forecast — is fully
functional and is the soak's own tool. The DR forecast is computed by the
SAME doctor check functions in a forecast accessor (ONE implementation —
`doctor_checks.system_target_forecast()` renders DR-14/DR-15/DR-17),
marked as a forecast in the output. Attempting to weaken the gate inside
the wave is a product-position veto; the gate constant flips only through
a future ratification act.

**C14 — Fast path + progress.** Zero rewrite counters in the plan (a 6.0
source — the EXPECTED system-target case; the B3 already-migrated refusal
does NOT apply to this target) → the UPDATE sweep is skipped: a pure
backup-API copy, FTS riding in the copied pages. Bulk reads fetch in
batches everywhere (a full-table materialization used to buffer the whole
table; a source-scan pin bans the bulk fetch call). Phased banners +
streaming row/byte counters on plain stderr — no spinner (sudo+ssh+grep
safe), paths and numbers only. Stepped backup with a progress callback
for databases above ~256 MB. Preflight free space ≥ 2× the source volume
(refusal with the numbers). No wall-clock timeout anywhere near a healthy
run. The source WAL size rides the plan/report as an advisory number. A
pin asserts no `-wal`/`-shm` anywhere in staging before publish.

**C15 — Refusals.** Every new refusal is a `StoreMigrationError` subclass
with a stable documented exit code, fail-fast BEFORE the first write; the
taxonomy grows additively: 10 `SystemProfileNotInstalledError` ·
11 `TargetStoreContentError` · 12 `ProfileIsolationError` ·
13 `InstallStateDriftError` · 14 `SourceRecoveryRequiredError` ·
15 `ActivationGateError` · 16 `InsufficientSpaceError`.

## Committee verdict summary (one paragraph)

ArchCom 2026-10-22: Product, Security and DBA all returned
ACCEPT-with-amendments — Product demanded the C13 activation gate and the
C2 honest-cargo report (a silent partial move would strand half a
deployment), Security required the C6 unconditional read-only source
discipline, the C10 secrets invariants and the manifest-derived C4
ownership pass (no blanket root chown over a secrets-adjacent tree), DBA
contributed the C5 piecewise-publish analysis (store home == data root
makes one rename impossible), the C7 decisive file probes over every
sqlite under the data root, and the C8 logical-equivalence framing
(backup-API page reshaping is legal; counters/digests are the gate); the
one dispute — whether the mover records the store into the install-state
— was decided by the TL for NO (C12), with the preservation-vs-refusal
mechanism note recorded above.

## Rollout

Wave 5 lands the contract implementation + pins (`make verify` + bandit
green, suites sequential). The round-trip soak (full install → run →
update → uninstall → restore, plus a mid-publish interruption drill) is
the NEXT gate and is not started by this wave; `--apply` stays refused
until the soak closes AND the owner speaks. The ADR number 0047 was
reserved against fresh `origin/main` immediately before writing
(0046 was the head at reservation time).

## Consequences

**What becomes true:** a store can be planned (dry-run + DR forecast) and
— after the gate opens — relocated into the system profile with the same
gate discipline the 6.0 mover taught: typed failures, untouched source,
verified staging, manifest-true ownership, a preserved rollback artifact,
and an uninstall that can never silently delete a migrated store.

**Costs:** the mover grows a second publish shape (piecewise + cross-fs
config) and a second quiesce surface — both pinned; the C13 gate keeps a
fully built apply path behind a constant until the soak closes (deliberate
dead code from the product's point of view, alive as the soak's tool).

**Risks accepted:** the piecewise publish is interruptible by design and
rolls back — a rollback failure (disk full mid-rollback) leaves named
leftovers that the C1 freshness check converts into a loud refusal on the
next run; the C12 mechanism note above stays an open item for the TL.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Record the migrated store into the install-state (exact-inverse coverage) | Rejected (TL, C12): uninstall would DELETE the store by default — data-loss class; the unrecorded store must make the operator decide explicitly. |
| Auto-install the system profile on `--target=system` | Rejected (unanimous): a migration command must never mutate the installation shape it migrates INTO; typed refusal + remediation instead. |
| One atomic rename via a temp home inside the data root | Rejected (DBA): the store home IS the data root — the rename would need a move of the whole root including foreign scaffolding; piecewise + rollback guard is the honest shape. |
| Byte-identity gate on the main db | Rejected: the backup API legally reshapes page layout; logical equivalence (counts, digests, per-table counts, samples) is the ratified gate. |
| Reuse the user-scope control-socket resolver for the C7 warning | Rejected (Security): a NEW manifest-derived constant keeps the two scopes from coupling; reusing a user-path resolver inside a system gate invites drift. |
| Weaken the C13 gate to ship the wave | Rejected (Product veto): activation is the owner's word + soak, per ADR-0045 Decision 9; the wave ships the contract, not the cutover. |

## References

- [ADR-0044](0044-store-migration.md) — the B3 mover whose semantics
  (staging → verify → publish, explicit-only, privacy, trust-marker
  byte-stability) are inherited (C11).
- [ADR-0045](0045-dual-profile-fhs-layout.md) — the system profile, the
  FHS manifest authority, Decision 9 (the activation gate this ADR
  implements).
- ArchCom protocol 2026-10-22 «migrate-store --target=system» —
  positions and amendments; team-local (Tech Lead).
- `tests/test_store_migration_system.py` — the pin-per-point suite
  (C1…C15 + the round-trip equivalence test).
- CHANGELOG, [Unreleased] — this wave's entry.
