# Vesma — SSH Sync Hardening Checklist

Auto-cron federation bridge (#104) — host/SSH layer hardening for the
batch-sync automation between two vesma instances (A = source, B = target).
The sync installation names itself `vesma-sync` (the service user,
directories, key comments); the script's env contract is `VESMA_SYNC_*`
(the 5.x `VESMARO_SYNC_*`/`MNEMOS_SYNC_*` spellings are retired — see
`scripts/sync-peers.sh`).

## Scope, audience, related

- **Scope:** the host/SSH layer that `scripts/sync-peers.sh` and the
  `contrib/systemd/vesma-sync.{service,timer}` units run on. This is NOT
  vesma application code — vesma itself stays offline.
- **Audience:** operators deploying the Phase 0 batch sync as an automated
  cron bridge. Assumes root on both A and B, both running Linux with systemd.
- **Related:**
  - ArchCom 2026-07-20 — automated channel decision (vesma memory
    `4dc7d96e`, protocol `.archcom/sessions/2026-07-20-automated-channel.md`).
  - Federation contract 2026-07-17 §3.1 (vesma memory `c64b0c37`,
    `.archcom/sessions/2026-07-17-federation-contract.md`).
  - Senior Security Engineer assessment — 7 hardening points (vesma memory
    `ed38f162`).

## Key invariant

**vesma stays offline.** There is no inbound endpoint on vesma — no
listening port, no API exposed to A. All automation is at the host/SSH layer:
A pushes a payload over rsync+ssh and triggers an import over ssh. A stolen
SSH key only gives the attacker `command=""`-restricted operations
(rsync-push or import-trigger), never an interactive shell.

## Hardening points

### 1. Dedicated `vesma-sync` user on B

Create a system user with no shell and a home under `/var/lib`. This user
owns the `incoming/` directory and the restricted `authorized_keys`.

```bash
sudo useradd --system --shell /usr/sbin/nologin \
    --home /var/lib/vesma-sync --create-home vesma-sync
sudo install -d -o vesma-sync -g vesma-sync -m 0750 /var/lib/vesma-sync/incoming
sudo install -d -o vesma-sync -g vesma-sync -m 0700 /var/lib/vesma-sync/.ssh
```

The `incoming/` dir (`0750`) is where rsync delivers payloads. The `.ssh/`
dir (`0700`) holds `authorized_keys`. The `vesma-sync` user has no password
and no shell — login is key-only via the two restricted keys (§2).

### 2. `authorized_keys` on B with `command=""` restrictions

Two restricted keys, each pinned to one wrapper. Both carry `from=""`
allow-list, `no-pty`, and every forwarding disabled. The key alone never
yields a shell — only the single guarded command runs.

```text
# ~/.ssh/authorized_keys for vesma-sync on B

# PUSH key — rsync delivery (rsync-wrapper.sh restricts dest to incoming/)
from="192.0.2.5",no-pty,no-agent-forwarding,no-X11-forwarding,no-port-forwarding,\
command="/usr/local/sbin/rsync-wrapper.sh" \
ssh-ed25519 AAAA... vesma-sync-push@A

# TRIGGER key — import invocation (vesma-import-wrapper.sh pins passphrase-env)
from="192.0.2.5",no-pty,no-agent-forwarding,no-X11-forwarding,no-port-forwarding,\
command="/usr/local/sbin/vesma-import-wrapper.sh" \
ssh-ed25519 AAAA... vesma-sync-trigger@A
```

Concrete implementations:

- `contrib/systemd/rsync-wrapper.sh` — parses `SSH_ORIGINAL_COMMAND`, rejects
  non-rsync invocations, locks the destination to `INCOMING_DIR`, appends an
  audit line, then re-execs `rsync --server`.
- `contrib/systemd/vesma-import-wrapper.sh` — parses `SSH_ORIGINAL_COMMAND`,
  rejects anything other than `vesma sync import`, rewrites the source path
  under `INCOMING_DIR`, **pins `--passphrase-env` to the configured name**
  (even a compromised A cannot redirect the passphrase read), appends an
  audit line, then execs the import.

### 3. Ed25519 key generation on A

Generate two Ed25519 keys on A — one for push, one for trigger. Two keys
keep the two commands on independent revocation paths: if the push key is
compromised you rotate only it, leaving the trigger key intact (and vice
versa). A single shared key would force a full rotation on any compromise.

```bash
sudo install -d -o root -g root -m 0750 /etc/vesma
sudo ssh-keygen -t ed25519 -f /etc/vesma/sync-push-key    -N "" -C "vesma-sync-push@A"
sudo ssh-keygen -t ed25519 -f /etc/vesma/sync-trigger-key -N "" -C "vesma-sync-trigger@A"
```

| Option | Two keys (chosen) | One shared key |
| --- | --- | --- |
| Compromise blast radius | one command loses access | both commands lose access |
| Rotation cost | rotate one key, one `authorized_keys` line | rotate one key, two lines |
| Operational surface | two key files to provision | one key file |

Copy each `.pub` to B and add it to `authorized_keys` under its `command=""`
line (§2). The private keys stay on A at `/etc/vesma/` (§4).

### 4. Key storage on A

Private keys live at `/etc/vesma/` with `chmod 600`, owner `root:root`.
The `vesma-sync.service` unit runs as `vesma-sync` but reads the keys via
the systemd unit's `User=` — adjust if your policy requires the service
user to own the keys. Alternatively store keys in an OS keyring or a
secrets manager (Vault, systemd-creds) and reference the path in
`sync.env`.

```bash
sudo chmod 600 /etc/vesma/sync-push-key /etc/vesma/sync-trigger-key
sudo chown root:root /etc/vesma/sync-push-key /etc/vesma/sync-trigger-key
```

Never commit private keys to VCS. `sync.env.example` references the paths
only — the key material itself is out-of-band.

### 5. Key rotation

Rotate quarterly, or immediately on any suspected compromise.

```text
1. Generate a new Ed25519 key on A (§3):
     sudo ssh-keygen -t ed25519 -f /etc/vesma/sync-push-key-new -N "" -C "vesma-sync-push@A-rotN"
2. Add the new .pub to authorized_keys on B (§2) — keep the OLD line in place
   during the cutover so a failed rotation does not break the cron.
3. Test: run sync-peers.sh manually with VESMA_SYNC_DRY_RUN=1 against the
   new key, then a real run.
4. Update sync.env on A to point at the new key path.
5. Remove the old .pub line from authorized_keys on B.
6. Shred the old private key on A:  sudo shred -u /etc/vesma/sync-push-key-old
```

### 6. Audit log on B

Every rsync and import invocation appends a line to
`/var/log/vesma-sync.log` with an ISO-8601 UTC timestamp, the source IP
(from `SSH_CLIENT`), the event (`ACCEPT`/`REJECT`), and the detail. Both
wrappers write via the `_audit` helper — the audit happens inside the
`command=""` guard, so it cannot be bypassed by a stolen key.

```bash
sudo install -o vesma-sync -g vesma-sync -m 0640 /dev/null /var/log/vesma-sync.log
# Optional: logrotate entry for /var/log/vesma-sync.log
```

Log line shapes (see `rsync-wrapper.sh` and `vesma-import-wrapper.sh`):

```text
[2026-07-21T12:00:00Z] rsync-wrapper src=192.0.2.5 ACCEPT dest=/var/lib/vesma-sync/incoming/vesma-sync-20260721T120000Z.json
[2026-07-21T12:00:05Z] vesma-import-wrapper src=192.0.2.5 ACCEPT source=/var/lib/vesma-sync/incoming/vesma-sync-20260721T120000Z.json passphrase-env=VESMA_EXPORT_PASSPHRASE dry_run=0
[2026-07-21T12:01:00Z] rsync-wrapper src=192.0.2.5 REJECT destination outside INCOMING_DIR: /etc/passwd
```

Forward to a central collector via rsyslog if you aggregate logs:

```text
# /etc/rsyslog.d/vesma-sync.conf
:syslogtag, contains, "vesma-sync"  /var/log/vesma-sync.log
& stop
```

### 7. Network — `from=""` allow-list + firewall

Two layers restrict who can reach the `vesma-sync` SSH surface:

1. **`from=""` in `authorized_keys`** (§2) — only A's IP can use either key.
2. **Firewall rule** — only A's IP can reach `sshd` for the `vesma-sync`
   user at all.

```bash
# nftables — allow SSH from A only, drop everything else to port 22
sudo nft add rule inet filter input tcp dport 22 ip saddr 192.0.2.5 accept
sudo nft add rule inet filter input tcp dport 22 drop
```

`sshd_config` example — restrict the `vesma-sync` user to the wrappers
and disable every form of forwarding for that user:

```text
# /etc/ssh/sshd_config.d/vesma-sync.conf
Match User vesma-sync
    AllowUsers vesma-sync
    PermitTTY no
    AllowAgentForwarding no
    X11Forwarding no
    AllowTcpForwarding no
    PermitTunnel no
    ForceCommand /usr/local/sbin/vesma-import-wrapper.sh
```

`ForceCommand` is a second layer of defence: even if `command=""` is
missing from `authorized_keys`, sshd still invokes the wrapper. For the
push key, the `rsync-wrapper.sh` guard inside `command=""` is the primary
gate — `ForceCommand` cannot distinguish push from trigger, so it is
typically set to a script that dispatches on `$SSH_ORIGINAL_COMMAND`.

## Installation summary

Ordered steps, A → B.

```text
# ── On B (target) ──────────────────────────────────────────────────────────
1. Create the vesma-sync user (§1):
     sudo useradd --system --shell /usr/sbin/nologin --home /var/lib/vesma-sync --create-home vesma-sync
2. Create incoming/ and .ssh/ with the right modes (§1).
3. Install the wrappers:
     sudo install -m 0755 contrib/systemd/rsync-wrapper.sh         /usr/local/sbin/
     sudo install -m 0755 contrib/systemd/vesma-import-wrapper.sh /usr/local/sbin/
4. Create /var/log/vesma-sync.log owned by vesma-sync (§6).
5. Add the two restricted keys to ~/.ssh/authorized_keys (§2) — after A's
   public keys exist (step A1 below).
6. Apply the sshd_config drop-in + firewall rule (§7). Reload sshd.

# ── On A (source) ──────────────────────────────────────────────────────────
3. Generate the two Ed25519 keys (§3). chmod 600, owner root (§4).
4. Copy the two .pub files to B and add them to authorized_keys (step B5).
5. Install scripts/sync-peers.sh:
     sudo install -m 0755 scripts/sync-peers.sh /usr/local/sbin/
4. Provision /etc/vesma/sync.env from contrib/systemd/sync.env.example —
   the exact path the `EnvironmentFile=` of `vesma-sync.service` loads
   (replace every RFC-reserved dummy). Provision the passphrase via a
   systemd drop-in or LoadCredential — NOT in sync.env.
5. Install the systemd units:
     sudo install -m 0644 contrib/systemd/vesma-sync.service /etc/systemd/system/
     sudo install -m 0644 contrib/systemd/vesma-sync.timer   /etc/systemd/system/
     sudo systemctl daemon-reload
8. Dry-run first:  sudo VESMA_SYNC_DRY_RUN=1 systemctl start vesma-sync.service
   (or run sync-peers.sh by hand with the env vars exported).
7. Enable the timer:  sudo systemctl enable --now vesma-sync.timer
```

## Verification

How to confirm the hardening holds.

| Test | Expected | Failure means |
| --- | --- | --- |
| `ssh -i sync-push-key vesma-sync@B` (no command) | rejected — "no command provided — interactive shell refused." (exit 2) | `command=""` not set in authorized_keys |
| `ssh -i sync-push-key vesma-sync@B "cat /etc/passwd"` | rejected — "non-rsync command refused" (exit 2) | rsync-wrapper.sh not the `command=""` |
| `rsync -e "ssh -i sync-push-key" file B:/etc/passwd` | rejected — "destination outside INCOMING_DIR" (exit 2) | rsync-wrapper.sh path check broken |
| `ssh -i sync-trigger-key vesma-sync@B "vesma sync export ..."` | rejected — "non-import command refused" (exit 2) | vesma-import-wrapper.sh guard broken |
| `VESMA_SYNC_DRY_RUN=1 bash scripts/sync-peers.sh` (with env) | exit 0, stderr logs `vesma sync export`, `rsync`, `ssh` | script env-var contract drift |
| `tail /var/log/vesma-sync.log` after a real run | ACCEPT lines with src IP + timestamp | audit helper not writing |

Run the dry-run first on every new install — it exercises the full
env-var validation and command construction without touching the network.

## See also

- ArchCom 2026-07-20 — automated channel decision (vesma memory
  `4dc7d96e`, protocol `.archcom/sessions/2026-07-20-automated-channel.md`).
- Federation contract 2026-07-17 §3.1 (vesma memory `c64b0c37`,
  `.archcom/sessions/2026-07-17-federation-contract.md`).
- Senior Security Engineer assessment — 7 hardening points (vesma memory
  `ed38f162`).
- `contrib/systemd/rsync-wrapper.sh` — concrete rsync-push guard (§2, §6).
- `contrib/systemd/vesma-import-wrapper.sh` — concrete import-trigger guard
  (§2, §6).
- `contrib/systemd/sync.env.example` — env var template (RFC-reserved dummies).
- `scripts/sync-peers.sh` — the ExecStart script (reads `VESMA_SYNC_*`).
- `tests/test_sync_peers_script.py` — tests for the script + systemd units.