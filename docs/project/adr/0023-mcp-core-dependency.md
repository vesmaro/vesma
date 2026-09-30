# ADR 0023: Move the MCP SDK into Core Dependencies (`mcp>=2.0,<3.0`)

**Status:** Accepted (Architectural Committee, 2026-09-06) — lands in 4.1.0
**Deciders:** Tech Lead (chair), Product Architect, Analytics Lead,
Senior Security Engineer, Senior System Engineer
**Scope:** dependency placement of the `mcp` SDK (core vs optional extra),
version floor, backward-compat alias, migration into 4.1.0

## Context

The owner directed (2026-09-06): is a memory server for AI agents meaningful
if the base install cannot wire it into an agent — MCP belongs in the base
install. The committee examined the funnel and confirmed the gap sits on the
primary product surface.

State at decision time: `mcp[cli]>=2.0,<3.0` lives in
`[project.optional-dependencies]`, so `pip install mnemos-memory-server`
cannot run `vesma mcp-server` — the primary agent-harness surface. The
first-run sequence for a harness user is: install → `vesma doctor` FAIL
(100% of harness users) → re-install with extra syntax → doctor again. With
switching cost ≈ 0 (mem0, letta, engram are one pip command away), this is a
churn event at the most sensitive point of the funnel.

The "thin base" argument is dead on the facts: core already pulls
`onnxruntime`, `grpcio`, `huggingface_hub`, `trafilatura`. One more SDK does
not change the weight class of the install.

The security verdict removed the last objection: the stdio transport adds no
network perimeter — the HTTP API and auth are already core. Net-new
transitive dependencies were predicted as `httpx-sse`, `jsonschema` (+ `referencing`,
`rpds-py`), `sse-starlette` — roughly 1 MiB total; the wheel itself does not
grow. Post-resolve erratum (actual `uv.lock`): the mcp 2.1.1 tree pulls
`httpx2`, `httpcore2`, `truststore`, `mcp-types`, `jsonschema`, `sse-starlette`
— same order of magnitude. Verified: `mcp` is imported only in `src/vesma/mcp_server.py`, so
lazy-import isolation is preservable by construction.

### Committee positions

| Role | Position | Key argument / condition |
|---|---|---|
| Tech Lead (chair) | Approved | sequenced the phases; adopted the security controls as binding |
| Product Architect | Recommend | a base install that cannot connect an agent contradicts the product |
| Analytics Lead | Conditional recommend | install → doctor FAIL → reinstall is a churn event; fix before any connection-layer work |
| Senior Security Engineer | Conditional recommend | stdio adds no perimeter; keep the import isolated; audit the unified profile |
| Senior System Engineer | Recommend | bare `mcp` without `[cli]`; one-PR migration; `uv.lock` regeneration |

## Decision

`mcp>=2.0,<3.0` moves from the optional extra into `dependencies` as **bare
`mcp`** — not `mcp[cli]`. The `cli` extra adds only `typer` and
`python-dotenv`, both already core dependencies, so it would buy nothing.
The extras keep `mcp = []` as an empty backward-compat alias:
`pip install mnemos-memory-server[mcp]` keeps working and resolves to the
same state as the base install. Lands in 4.1.0 (the release is already
pending on `main`).

Binding conditions:

- **Lazy-import isolation.** `mnemos.mcp_server` is imported only by the
  CLI subcommand handler; a guard test pins this so the SDK stays out of
  every other import path (server startup, REST, A2A). This preserves lazy
  initialization and keeps the attack surface narrow.
- **Unified audit.** pip-audit and SBOM run over the unified profile after
  the extra dissolves — one dependency set, one audit surface.

### Migration (one PR)

| Step | What |
|---|---|
| deps | `mcp>=2.0,<3.0` into `dependencies`; extras keep `mcp = []` as the compat alias |
| lockfile | regenerate `uv.lock` — it pins a stale `mcp 1.28.1` against the `>=2.0` floor |
| guard | import-isolation guard test for `mnemos.mcp_server` |
| docs | sweep ~15 `[mcp]` mentions across EN+RU docs — the extra syntax is no longer needed |
| untouched | the `mypy` override for the SDK stays as-is |

Gate: guard test green plus a full `make verify`.

## Consequences

- **Positive:** a fresh install runs the primary agent scenario out of the
  box; `vesma doctor` is green on a fresh base install; the funnel loses
  the reinstall step; artisanal installs with drifting SDK versions end;
  pip-audit and SBOM cover a single profile.
- **Negative / costs:** the install footprint grows by ~1 MiB of net-new
  transitive dependencies; the supply-chain surface adds `httpx2`/`httpcore2`/
  `truststore`/`mcp-types` (post-resolve actuals), `jsonschema`/`referencing`/
  `rpds-py`, `sse-starlette`, and their
  transitive CVE noise now lands on the core dependency-updates process.
- **Deferred / accepted residuals:** transitive CVE triage volume is
  accepted under the existing pin-and-audit policy; a deliberately
  MCP-free install is no longer available — the committee found no such
  user (the CLI/REST surfaces are secondary to the harness surface).

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| Keep `[mcp]` as an extra | The docs already recommend `[mcp]` everywhere, so the shipped default is broken; it adds a step at the most fragile point of the funnel |
| "Lite" install profile without MCP | No user of memory without a harness-agent exists — CLI and REST are secondary surfaces |
| Lazy import with an interactive install prompt | Adds a step instead of removing one; the prompt cannot complete without network and pip anyway |
| `mcp[cli]` in core | The `cli` extra adds only `typer` + `python-dotenv`, both already core — zero benefit for a longer requirement line |
| Vendor the MCP SDK | Supply-chain and update burden moves onto the project with no benefit over normal dependency management |

## References

- ADR-0021 — wheel-composition context: what already rides in the main
  wheel; the boundary that made the "thin base" argument untenable.
- mnemos decision id `8bc9cb7f-184b-409a-b422-568493802005`.
- Architectural Committee session of 2026-09-06 (protocol and contract) —
  archived with the committee records, team-local, not part of this
  repository; see the mnemos entries above.
