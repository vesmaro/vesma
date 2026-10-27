# ADR 0048: Installer-lane code plane — `/usr/local`

**Status:** Accepted — owner ratification 2026-10-27 (the
«/usr/local = canonical installer system profile» wave). Senior System
Engineer implementing (`feat/usrlocal-code-plane`). The EROFS typed
refusal with the atomic-distro remediation (card `151a55ff`) ships in
the same wave and is ratified as part of this decision's consequences.

**Deciders:** the owner (ratified 2026-10-27); Senior System Engineer
implementing. ADR-0045's FHS table remains the shape authority — this
ADR amends its INSTALLER-LANE table only.

**Scope:** the code-plane `system_path` values of the shipped FHS
manifest (`src/vesma/service/fhs_manifest.yaml`) for the installer lane
(`vesma system install` / `system update` / `system uninstall`, doctor
DR-14…DR-19, `migrate-store --target=system`). Out of scope: the rpm
lane's own future manifest (it keeps `/usr/lib/vesma` — see Decision),
the container build's artifact derivation (it follows the same shipped
manifest), the data/config/log/cache planes (unchanged), and any
override mechanism (ADR-0045 Decision 5 governs).

## Context

ADR-0045 ratified the dual-profile FHS layout with the system code plane
under `/usr/lib/vesma` + `/usr/share/vesma`, explicitly deferring deb/rpm
to a future "thin wrapper over the same manifest". Two facts surfaced
since, and one line of the layout has to move accordingly:

1. **FHS: `/usr` is the package manager's territory.** The `vesma
   system install` lane is an ADMIN TOOL, not a distro package. FHS
   reserves `/usr` for distribution packages — Fedora packaging
   guidelines and Debian policy both forbid non-package writes into
   `/usr`. The canonical location for admin-installed software is
   `/usr/local`. An installer that lays venv trees into `/usr/lib/vesma`
   fights every distro policy at once.
2. **Atomic/immutable distros make `/usr` read-only.** On ostree-based
   systems (Silverblue, CoreOS, Bluefin) `/usr` is an immutable mount;
   installer legs writing `/usr/lib/vesma` fail with `EROFS`
   (Read-only file system). Crucially, on those systems `/usr/local` is
   a SYMLINK to `../var/usrlocal` — a persistent, writable location
   (empirically verified 2026-10-27 on Fedora Silverblue 44: the symlink
   survives reboots by design, ostree's `var/` is persistent state).
   The installer lane's code plane therefore works natively on atomic
   distros once it lives under `/usr/local`.
3. **The rpm lane needs the opposite table.** A future rpm package
   (line C) MUST ship files into `/usr/lib/vesma` + `/usr/share/vesma`:
   for a PACKAGE that is exactly the right place — distro policy, system
   image ownership, clean rpm payload. One manifest cannot serve both
   lanes without an override dance; per-lane SHIPPED manifests can: the
   resolver code (`vesma.service.fhs`) is ONE, each distribution channel
   ships its own table.

The `sysusers.d` fragment moves with the code plane: systemd-sysusers(8)
reads `/usr/local/lib/sysusers.d/` alongside `/usr/lib/sysusers.d`
(the fragment stays applied at install time through the explicit-path
invocation `systemd-sysusers <path>` — `_apply_sysusers` — and is picked
up again on boot).

## Decision

1. **The installer lane's shipped manifest puts the code plane under
   `/usr/local`** (ADR-0045 §3.4 table, installer-lane amendment):

   | role | system_path |
   |---|---|
   | `code_root` | `/usr/local/lib/vesma` |
   | `toolchain_pythons` | `/usr/local/lib/vesma/pythons` |
   | `venv_engine_revision` | `/usr/local/lib/vesma/venv-<rev>` |
   | `venv_engine` | `/usr/local/lib/vesma/venv` |
   | `venv_component_root` | `/usr/local/lib/vesma/venvs` |
   | `venv_component_revision` | `/usr/local/lib/vesma/venvs/<name>-<rev>` |
   | `venv_component` | `/usr/local/lib/vesma/venvs/<name>` |
   | `distro_static` | `/usr/local/share/vesma` |
   | `sysusers_fragment` | `/usr/local/lib/sysusers.d/vesma.conf` |

   Config (`/etc/vesma`), data (`/var/lib/vesma`), cache, logs, IPC
   (`/run/vesma`) and the systemd unit (`/etc/systemd/system`) are
   UNCHANGED — their canonical FHS places are not contested territory.

2. **Per-lane shipped manifests — one resolver, one table per
   distribution channel.** The engine resolves system paths ONLY through
   `vesma.service.fhs` (SY-02, unchanged). A future rpm lane ships its
   own manifest with `/usr/lib/vesma` forms; the installer lane ships
   the `/usr/local` forms ratified here. No code fork: resolvers,
   installer, doctor and uninstall are manifest-driven already
   (ADR-0045 Decision 5) — a lane change is a data change.

3. **unitgen and the updater stay manifest-resolved.** `ReadOnlyPaths`
   in the generated system unit and the updater's boundary check render
   from the manifest rows (`code_root`, `distro_static`) — no path
   literals in `unitgen.py` or `system_update.py`; the grep audit for
   stray `/usr/lib/vesma` literals outside docstrings must stay empty.

4. **The installer applies the sysusers fragment as an EXPLICIT file**
   (`systemd-sysusers <fragment-path>` — the existing `_apply_sysusers`
   seam, pinned by test). `/usr/local/lib/sysusers.d` is a
   systemd-sysusers search directory, so boot-time pickup is preserved.

5. **An `EROFS` failure on a code-plane leg is a TYPED refusal** (card
   `151a55ff`): a new `SystemInstallError` subclass with a stable exit
   code (the ADR-0047 C15 taxonomy discipline, additive window — the
   mover occupies 1…16, the installer's read-only-target refusal takes
   17). The refusal carries the remediation text for atomic/immutable
   systems: for an experiment `sudo rpm-ostree usroverlay` (lives until
   reboot); for a permanent install use the rpm package. Detection of an
   atomic system (probe write in the target directory by the failing
   leg + os-release hints: `VARIANT_ID` containing `silverblue`/
   `coreos`/`bluefin`, the `/run/ostree-booted` marker) informs ONLY the
   remediation wording — the refusal itself is always by the EROFS fact,
   never by a distro guess.

## Alternatives considered

- **Keep `/usr/lib/vesma` for the installer lane** — rejected: it
  violates the FHS admin-software canon on every distro, fights package
  policy, and hard-fails with `EROFS` on atomic distros (the install
  lane's most hostile environment gets no install path at all).
- **`/opt/vesma` monolith** — rejected already in ADR-0045 for the same
  shape reason: it abandons the FHS split (`/etc`, `/var/lib`, …) the
  rest of the layout earns its discipline from, and `/opt` does not
  receive the ostree `/usr/local → var/usrlocal` persistence either.
- **One manifest + a prefix-override config knob** — rejected: two
  configuration systems (the manifest IS the table, ADR-0045 Decision 5)
  and a knob whose every consumer must handle "which lane am I" — the
  per-lane shipped manifest answers that at packaging time, where the
  answer is known.
- **Detect atomic distros and switch paths at runtime** — rejected:
  runtime distro sniffing as a PATH decision is a fork generator (the
  installed tree would disagree with the shipped table) and a silent
  divergence class; the manifest must be the truth the operator can
  read.

## Consequences

**Positive.** The installer lane is FHS-clean by the book (admin
software in `/usr/local`); atomic/immutable distros work natively — the
code plane lands in persistent `var/usrlocal` through the symlink; the
future rpm lane gets a clean, non-overlapping table (`/usr/lib/vesma`)
without touching resolver code; doctor DR-14…DR-19 and the exact-inverse
uninstall needed NO code changes — they read the manifest (the
"self-configuration without a code fork" property ADR-0045 bought with
the manifest decision).

**Costs / risks.**

- *Legacy coexistence:* a tree installed by pre-0048 code
  (`/usr/lib/vesma` + a state file recorded there) is not understood by
  the new manifest. The installer overwrites the install-state with the
  new `/usr/local` rows, so the old tree becomes unrecorded residue that
  uninstall will NOT remove (exact-inverse = recorded artifacts only).
  The residue is inert (DR-16 walks only manifest roots) but occupies
  disk. Deliberately out of this wave's scope: a legacy-tree detection
  warning leg (compare the old roles against the state) — candidate for
  a follow-up card. Manual removal for the rare affected operator:
  `sudo rm -rf /usr/lib/vesma /usr/share/vesma` after `vesma doctor`.
- *`/usr/local` hygiene:* the lane now shares the directory every other
  admin-installed product uses. The namespace discipline stays with the
  manifest (`/usr/local/lib/vesma`, `/usr/local/share/vesma`,
  `/usr/local/lib/sysusers.d/vesma.conf` — vesma-owned leaves only, no
  writes into shared parents' content).
- *Exit-code wiring:* the typed EROFS refusal declares the stable exit
  code (17) on the exception class per the C15 discipline; the system
  CLI verbs currently render every `SystemInstallError` as exit 1 —
  surfacing 17 through `typer.Exit` is a `cli/system_cmd.py` change,
  reserved for the CLI wave (additive, non-breaking).

## References

- [ADR-0045](0045-dual-profile-fhs-layout.md) — the dual-profile FHS
  layout, the ONE declarative manifest (Decision 5), the §3.4 table this
  ADR amends for the installer lane.
- [ADR-0047](0047-migrate-store-system-target.md) — the C15 refusal
  taxonomy discipline (typed subclass + stable exit code) inherited here.
- `specs/layout/v2` §3.4 (system-profile table), §3.10 (symlink
  revisions), §3.12 (manifest form), §3.13 (install/uninstall/update).
- Card `151a55ff` — the EROFS/atomic typed refusal (leg 2 of this wave).
- Empirical note 2026-10-27: `/usr/local → ../var/usrlocal` persistence
  verified on Fedora Silverblue 44 (ostree `/var` = persistent state).
