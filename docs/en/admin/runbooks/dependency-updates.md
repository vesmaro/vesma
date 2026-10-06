# Runbook: Dependency Updates & CVE Reminder

**🌐 Language / Язык:** English · [Русский](../../../ru/admin/runbooks/dependency-updates.md)

## Why this exists

Historically this runbook tracked the ignored `CVE-2026-45829` in `chromadb` (no upstream fix).
Since NM-1c (ADR-0021) chromadb is **removed from the runtime** — replaced by the bundled
`vesma-embed-v1` local model — and its CVEs left with it. The runbook stays for what is still
true: the direct-pin policy for vulnerable transitives and the weekly audit check.

## Pinning policy (M15.5.1)

Vesma uses **direct pins** for vulnerable transitives rather than bumping parent packages:

- **`aiohttp>=3.14.3,<4.0`** — direct pin. Originally pulled in transitively by
  `chromadb → kubernetes` (historical: chromadb is gone); the floor was raised
  to 3.14.3 (#267): PYSEC-2026-3546/3547 are fixed in 3.14.2, PYSEC-2026-3545
  in 3.14.3, so 3.14.3 covers all three on top of the older CVE pack
  (34993, 47265, 50269, 54273-54280). The pin stays because vulnerable
  aiohttp versions are still reachable via `fastapi`/`uvicorn`. Pinning the
  safe minor directly is smaller-blast-radius than bumping a parent package.

- **`starlette>=1.3.0,<2.0`** — direct pin, fixes CVE-2026-48817, 48818, 54282, 54283.
  Pulled in transitively by `fastapi`. Same rationale.

- **M15.5.2 pin family** (`pyproject.toml`): `pyjwt>=2.15.0`
  (PYSEC-2026-120/175-179 fixed in 2.13.0, 4140-4152 in 2.14.0, 4141 in 2.15.0;
  PYSEC-2026-4146 has no upstream fix — ignored in `make security`, tracked in #476),
  `urllib3>=2.8.0`, `python-dotenv>=1.2.2`, `idna>=3.15`, `pygments>=2.20.0`.

- **`pip`** — a tool, not a project dep (not in `pyproject.toml`): upgrade via
  `pip install --upgrade pip` after venv recreate. The current graph ships
  26.2.x, which covers PYSEC-2026-196 and later.

- **`chromadb`** — removed from the runtime in NM-1c (ADR-0021): the bundled `vesma-embed-v1`
  model runs on `onnxruntime` directly. Nothing to bump anymore; kept here as decision history.

When adding a new pin: include a one-line comment in `pyproject.toml` with the CVE id and
the fix version, as in the entries above. Pins must use a range with an upper bound
(`<4.0`, `<2.0`) to prevent accidental major-version drift.

## Weekly quick check

```bash
cd /path/to/vesma   # repo root
source .venv/bin/activate
make security
```

Expected outcomes:
- Clean `pip-audit` → nothing to do.
- A new CVE in a transitive → add a direct pin per the policy above (or bump the parent
  when the fixed release is the parent itself).

## Full dependency refresh

```bash
cd /path/to/vesma   # repo root
source .venv/bin/activate
make update-deps
```

Then run full project checks:

```bash
make verify
```

## Remove temporary CVE ignore when fixed

The `security` target in [Makefile](../../../../Makefile) still carries two
ignore flags: `--ignore-vuln CVE-2026-45829` (the former chromadb exception —
inert since chromadb left the runtime) and `--ignore-vuln PYSEC-2026-4146`
(pyjwt advisory with no upstream fix yet, tracked in #476 — DO NOT remove
until the fix lands). When chromadb is gone from every deployed environment:

1. Edit [Makefile](../../../../Makefile)
2. In target `security`, remove `--ignore-vuln CVE-2026-45829` (keep the
   PYSEC-2026-4146 ignore while #476 is open)
3. In target `security-reminder`, drop the stale-ignore note lines
4. Run:

```bash
make verify
```

## Operational policy

- Keep an ignore only while upstream has no fix.
- Keep the reminder enabled while an ignore exists.
- Remove both ignore + reminder in one commit after upgrade.
