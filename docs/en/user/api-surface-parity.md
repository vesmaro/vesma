# API Surface Parity — CLI / REST / MCP

**Ratified by the Architecture Committee, 2026-10-06 (consensus; Analytics
Lead and Product Architect signed every disposition). This record exists so
the question stays closed:** future surface proposals are judged by it, not
re-litigated. The parity verdict of the same session that produced
[ADR-0038](../../project/adr/0038-pg1-graph-walk.md).

## The principle

**Parity means a named user per surface — not a verb count.** A gap is a
real user left without their surface, not a missing checkmark in a
three-way diff.

| Surface | Who it is for |
|---|---|
| CLI (`vesma ...`) | Humans and shell scripts |
| REST (`/api/v1/...`) | Non-Python integrations |
| MCP (`mnemos_*` tools) | Agents |

A capability may legitimately live on one surface only. A twin is added
when its named user exists — and then only under the versioning canon
below (new REST twins go to `/api/v1`).

## Matrix (verified against main `408836f`)

Re-enumerated from code, not from older tables: 10 top-level CLI commands
plus 21 CLI groups (`src/vesmaro/cli/`), 66 REST routes
(`src/vesmaro/api/` — main, auth, federation, A2A sessions), 40 MCP tools
(`src/vesmaro/mcp_server.py`). `✓` = present, `—` = absent,
`— *(wontfix)*` = a ratified non-goal (see dispositions below).

| Operation | CLI | REST | MCP |
|---|---|---|---|
| Add / search / re-filter a memory | ✓ `add` `search` `filter` | ✓ `POST /memories` · `/search` · `/filter/{id}` | ✓ `add` `search` `filter` |
| List recent memories | — | ✓ `GET /memories` | ✓ `list_recent` |
| Agent recall (named agent) | ✓ `recall agent` | ✓ `GET /recall/agent/{name}` | ✓ `agent_recall` |
| Session-context recall (checkpoints) | — | ✓ `POST /context/recall` | ✓ `recall_context` |
| Context save / assemble / rewrite | — | ✓ `/context/save` · `/context/assemble` · `/context/rewrite` | ✓ `save_context` · `assemble_context` · `context_rewrite` |
| Compress → retrieve original (CCR) | — | ✓ `POST /compress` + `POST /retrieve` | ✓ `compress` + `retrieve` |
| Tags (list / add / remove / rename / validate) | ✓ `tags validate·normalize·rename·audit` | ✓ `GET /tags` · `POST /tags/rename` · `POST /api/v1/tags/add` · `/remove` | ✓ `tags` · `tags_rename` · `list_tags` |
| Pipeline process | ✓ `processor run·start·stop·status` | ✓ `POST /process` | ✓ `reprocess` |
| Pipeline ops (synthesize / publish / DLQ / quarantine release) | — | ✓ `/synthesize` · `/publish/{id}` · `/dlq*` · `/memories/{id}/quarantine/release` | — |
| Pipeline traces | ✓ `logs` | ✓ `GET /traces` | — |
| Lifecycle hooks | — | ✓ `POST /hooks/{action}` | ✓ `hooks` |
| Path-scoped rules ingest | — | ✓ `POST /rules/ingest` · `DELETE /rules/ingest` | — |
| Reindex | ✓ `reindex` | ✓ `POST /reindex` | — *(wontfix)* |
| URL ingest | ✓ `add --url` | ✓ `POST /ingest-url` | ✓ `ingest_url` |
| Document ingest (quarantined pipeline) | — | ✓ `POST /ingest-document` | ✓ `ingest_document` |
| Watch / auto-collect | — | ✓ `/watch/start·stop·status` · `GET /auto-collect` | ✓ `watch_start·stop·status` · `auto_collect_status` |
| Stats / metrics | ✓ `stats` | ✓ `GET /api/v1/stats` (+ `/timeseries`) · `GET /api/v1/metrics` (Prometheus); `GET /metrics` = legacy JSON | ✓ `stats` |
| Export / import | ✓ `export` · `import` | ✓ `POST /api/v1/export` · `POST /api/v1/import` | ✓ `export` · `import` |
| Workflow lifecycle | ✓ `workflow get·set·history` | ✓ `GET·POST·DELETE /memories/{id}/workflow` | ✓ `workflow` |
| Graph read ops (index / status / search / trace / outline / snippet / coverage / schema / list / delete) | — | ✓ `/graph/*` | ✓ `index_project` … `delete_graph_project` |
| Graph register | ✓ `graph register` | ✓ `POST /api/v1/graph/register` | ✓ `register_project` |
| Graph repoint | ✓ `graph repoint` | ✓ `POST /api/v1/graph/repoint` | — *(wontfix)* |
| Sync payload (offline export/import file) | ✓ `sync export·import` | — *(wontfix)* | — |
| Federation pull | ✓ `fetch` | ✓ `POST /api/v1/federation/pull` | — |
| Scanner (quarantine dashboard) | ✓ `scanner run·status` | — *(wontfix)* | — |
| Cache alignment / awareness / usage report | — | — *(wontfix)* | ✓ `align_prefix` · `awareness` · `usage_report` |
| Ops (auth, sessions, service, update, doctor, migrate, fts, edge-stats, integration, agent-token, serve) | ✓ | ✓ `/auth/*` only; A2A sessions at `/v1` are their own versioned surface | — |

Cells describe presence of the capability, not flag-for-flag argument
equality — each surface keeps its own reference
([cli-reference.md](cli-reference.md),
[http-api.md](http-api.md),
[mcp-tools.md](mcp-tools.md)).

## Ratified dispositions

Every gap below was examined by the committee and closed with a reason.
**All four gaps are `wontfix`; the question does not reopen by default.**

| Gap | Disposition | Why |
|---|---|---|
| Generic `GET /recall` | wontfix | The «fetch what's relevant» job is already covered by `POST /search` + `POST /context/recall`; a third verb blurs the surface — and a cacheable, idempotent GET carrying a heavy semantic query breaks REST semantics. |
| `/fetch` | wontfix — no gap | The twin exists: `POST /retrieve` mirrors the MCP `retrieve`; the CLI `fetch` is about federation S2 and was the source of the confusion. |
| `/sync` | wontfix | A file payload with encryption is a human at a CLI (export/import already live under `/api/v1` — `/sync` would be a third verb); the service-level bulk job is covered by `/api/v1/federation/pull`. |
| `/scanner` | wontfix (revisit if it becomes an operator dashboard) | The scan initiator is the operator (CLI/service); REST consumers are the objects of scans, and they already have quarantine/release. |

Retro lines (same verdict): `align_prefix` (KV-cache alignment is an
agent's job) / `awareness` (agent introspection; diagnostics live in
`/api/v1/stats`) / reindex without MCP / graph repoint without MCP
(operator maintenance) — **wontfix**. Reprocess — no gap
(`POST /process`).

## Versioning canon

Ratified by the ArchCom on 2026-10-04 and in force:

- **Canonical roots live only under `/api/v1`.** New routes never appear
  at the bare root.
- **Root-level paths are legacy aliases.** Until Vesma 6.0 physically
  moves them, every alias hit is decorated by the deprecation middleware
  (`Deprecation: true` + a `Link` header pointing at the canonical
  `/api/v1` template).
- **`Sunset` (RFC 8594) appears with 6.0.** It requires an absolute
  HTTP-date; the header is added as soon as the 6.0 date is fixed —
  fabricating one earlier would mislead clients.
- **Breaking unification is parked behind 6.0** — alias removal, the
  physical route move, and any request/response shape unification land
  in the same major, not piecemeal.
- **`/metrics` and `/api/v1/metrics` are a documented near-duplicate:**
  the root path serves the legacy stats JSON body, the canonical path
  serves the Prometheus text exposition.

Details and per-route tables: [http-api.md](http-api.md) («Route
versioning» in Conventions).

---

_Committee protocol 2026-10-06 (team-local:
`~/.gcw/architectural-committee/2026-10-06-vesma-pg1-walk-and-rest-parity.md`),
[ADR-0038](../../project/adr/0038-pg1-graph-walk.md). Last updated: 2026-10-06._
