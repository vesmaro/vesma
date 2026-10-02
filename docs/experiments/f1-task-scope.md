# F1 — Pre-Registered Experiment: Task-Scoped Composition vs Context-Free Retrieval (Multicontext Epic #308 · ADR-0027 Phase 1)

| Field | Value |
|---|---|
| Status | **PRE-REGISTERED — committed before the first run** |
| Date | 2026-09-20 |
| Commit window | **Open.** No F1 leg of any arm (A0/C/B/A) has been executed or recorded — no `benchmarks/experiments/f1_task_scope/` runner exists yet (this file precedes the runner wave, exactly as E0 preceded the E3-runner wave). This file precedes all runs (epic [#308](https://github.com/Korrnals/vesma/issues/308), dev-plan §4b Ф1 row; ADR-0027 Phase 1 acceptance). |
| Owner | Analytics Lead (experiment design); corpus ground-truth criterion per the E0 §4.4 pattern, blind-auditκ co-owned with the adjudicator of the runner wave |
| Anti-HARKing clause | **No metric, threshold, stratum, arm, or hypothesis may be added or altered after the first recorded run.** Deviations require a dated §8 amendment; single-look analysis; no interim peeking (§6.6). Engine/instrument parameters the implementation must fix before a run are registered as pre-run revisions exactly as implemented (the E0 §8 revisions 2–7 pattern) — never as aspirations. |
| Language | English (canonical, ADR-style) |

**Sources (authoritative, cited inline as shown):**

1. [ADR-0027 — Multi-Context Memory](../project/adr/0027-multi-context-memory.md) — `[ADR-0027 §…]` (Phase-1 gate design, arms, corridors, the 11 binding invariants, the honest-risk clause)
2. [ADR-0026 — Memory Value Observability](../project/adr/0026-memory-value-observability.md) — `[ADR-0026 §…]` (the `S5` stand lineage, PASS / FAIL / NO-DATA verdict taxonomy, "value never blocks, invariants do", value-claim rule)
3. [ADR-0025 — Memory Meta-Level Lanes](../project/adr/0025-memory-meta-level-lanes.md) — `[ADR-0025 §…]` (the E0 pre-registration canon, the `LanesConfig` default-off precedent, the E3 falsification)
4. [E0 pre-registration](e0-meta-level.md) — `[E0 §…]` (file structure, McNemar pairing policy, power-note arithmetic and the revision-8 unconditional quartet this file's power table is anchored to)
5. [ADR-0020 — Benchmark Framework](../project/adr/0020-benchmark-framework.md) — `[ADR-0020 §…]` (corridor rule `baseline − max(0.02; CI95)`, exact-McNemar policy, event-driven re-baseline triggers)
6. [dev-plan §4b](../project/dev-plan.md) — the Ф1 checklist row (arms A/B/C with C an honest tag-filter; primary per-context recall@k on per-query-gold; the three corridors; per-stratum-only reporting)
7. Architectural Committee session of 2026-09-14 — protocol `2026-09-14-multi-context-memory.md`, team-local, not part of this repository
8. Ф0 implementation surface (as merged: PR #356 slice 1, PR #360 slice 2): `src/vesma/assemble.py` (`task` / `lens` parameters), `src/vesma/lens.py` (`Lens.CODE`, `lens_active`, `lens_admits`), `src/vesma/models.py` (the `task:` tag contract, `DOC_GROUPING_METADATA_FIELDS`)

**Interpretation boundary (binding, [ADR-0027 §Decision]):** a PASS validates retrieval-side task-scoped composition **on this corpus profile and the registered switching policy** — it is not "the agent understands tasks", not a schema justification by itself (Ф2 form is routed by §5.2), and not a user-value claim: value claims ride only an `S5` PASS [ADR-0027 invariant 11]. A negative or tie outcome is a legitimate result, not an experiment failure [ADR-0027 §Consequences].

---

## 0. Traceability — dev-plan §4b Ф1 row / ADR-0027 Phase 1 → document section

| Registered requirement (source) | Section(s) |
|---|---|
| Arms A/B/C, C = honest tag-filter, not a strawman [ADR-0027 §Phase 1; dev-plan §4b] | §1.2, §1.3 |
| Primary = per-context recall@k on per-query-gold [ADR-0027 §Phase 1] | §2.1 (k frozen at 5, §6.1) |
| Corridor: cross-context recall ≥ baseline [ADR-0027 §Phase 1] | §2.4 (G1) |
| Corridor: foreign-context noise ≤ threshold [ADR-0027 §Phase 1] | §2.5 (G2) |
| Corridor: tokens-per-completed-task ≤ baseline [ADR-0027 §Phase 1] | §2.6 (G3) |
| Lens: only narrows, never widens; never pinned [ADR-0027 invariants 6, 9] | §2.7 (G4), §4.5 |
| S5-stand lineage, versioned corpus, corpus hash in manifest before the run [ADR-0027 §Phase 1; ADR-0026 §6] | §3, §4.2 |
| E3-runner canon: run-ledger, `--record` refusal, write-once [ADR-0027 §Phase 1; E0 §6.6] | §4.3 |
| Per-stratum-only reporting; all arms symmetric; no post-hoc lens picking [ADR-0027 §Phase 1] | §6.4 |
| Exact thresholds and full decision rules incl. INDETERMINATE zones registered before data exists [ADR-0027 §Phase 1] | §5 |
| Honest negative allowed: "task-context = tag-filter with a different label" → primitive not built [ADR-0027 §Consequences] | §5.1, §5.2 |
| Binding invariants 1–11 hold during every run [ADR-0027] | §4.5 (V1–V6 operationalized; V3/V4/V5/V8 are structural — exercised by construction per §2.9 and not re-derived per run; the full #→section map lives in §2.8–§2.9) |

---

## 1. Research question, arms, ordering

### 1.1 Research question (falsifiable form)

**Does query-conditioned task-scoped composition — the Ф0 feature set: `task:` tag intersection at recall + doc-metadata grouping corpus-wide + the CODE lens, driven through the `assemble_context` `task`/`lens` parameters — improve per-task recall over context-free retrieval on a mixed multi-task corpus, WITHOUT losing the cross-task and shared knowledge access the agent needs, and does it deliver more than an honest tag-filter and more than a manual query-prefixing emulation?**

Falsifiers, in the ADR's own words: "task-context = an honest tag-filter with a different label" (→ A vs C tie, §5.2) and "query-blind composition" (already falsified as a class by E3: −61.5 pp, `e3-lanes-56c568297ad6`; every F1 arm is query-conditioned by construction — §1.3).

### 1.2 Arms — and the label-tension resolution (flagged, not silent)

**Numeric/label tension in sources, resolved here per the E0 §3.1 standard:** ADR-0027's mermaid and dev-plan §4b label the arms **A = canonical baseline (B0+type_boost), B = emulated task-context (zero production code), C = honest tag-filter**. This commission (the Ф1-prep wave, item 6) labels **A = the shipped Ф0 composition with flags on, B = emulated task context without the primitives, C = honest tag-filter**. The delta is real and structural: the ADR was written before Phase 0 landed, when the treatment could only be emulated; Ф0 has since shipped the real surface behind optional parameters (PR #356/#360), so the treatment is now executable as code rather than emulation. Resolution registered here, before any run:

| E-file arm | Role | ADR-0027 letter | Content (exact kwargs, §1.3) |
|---|---|---|---|
| **A0** | reference / baseline — context-free retrieval | **A** ("canonical baseline B0 + type_boost") | plain `assemble_context`, no task knowledge, no lens |
| **A** | treatment — task-scoped composition (the Ф0 feature set, flags on) | *(the ADR's arm-B role, realized as shipped code — new in this registration)* | `task=<slug>` + `lens=CODE` on task-class queries; `lens=CODE` on cross-class |
| **B** | honest approximation — emulated task context WITHOUT the primitives | **B** ("emulated task-context … zero production code") | query text prefixed with the task's display label; no `task`, no `lens` |
| **C** | honest tag-filter — the same task information, no composition | **C** ("an honest tag-filter carrying the same information") | pre-Ф0 client-side pattern: `mgr.search(..., tags=["task:<slug>"])` + standard block formatting |

A0 is ADDED by this E-file (the ADR's own arm A, renamed to free the letter A for the treatment per the commission). Without A0 the research question's "beats context-free retrieval" clause and two of the three ADR corridors ("≥ baseline", "≤ baseline") have no reference arm; registering it is a closure of the outcome space, not a new hypothesis. **All four arms run symmetrically on the identical corpus build and query set, are reported symmetrically, and no arm may be removed post hoc** [E0 §6.4].

### 1.3 Arm configuration (registered exactly as implemented; the runner wave re-registers any drift as a pre-run §8 revision)

Common block, every arm: `lanes_enabled=false` [ADR-0027 invariant 2 — the E3 verdict stands], `type_boost=true` (the E3 survivor is canonical per ADR-0027's own "B0 + type_boost" baseline wording; applied identically in all arms, hence controlled), `expand_ccr=false`, `mode=sync`, `budget=2048` tokens (the E3 equal-budget precedent [E0 §4.1]), top-5 blocks (k=5, §6.1), deterministic embedder, scanner off, isolated byte-identical store copy per arm, fixed arm order **A0 → C → B → A**.

Per query class (the class is a birth property of the query, part of the corpus, declared by the query generator — never derived from gold or outcomes; §3.1):

| Query class | A0 | C | B | A |
|---|---|---|---|---|
| `task` (gold inside the current task) | plain assembly | `mgr.search(query=q, project=…, limit=RECALL_DEPTH, tags=["task:<slug>"])`, top-5 formatted through the standard block formatter at the same budget | `assemble_context(query=f"{TASK_DISPLAY[slug]}: {q}", …)` — the task's human display label prefixed to the query text; zero Ф0 parameters | `assemble_context(query=q, task=<slug>, lens=CODE, …)` |
| `cross` (gold in shared scope or a foreign task) | plain assembly | plain assembly (filter dropped — the pre-Ф0 user's honest switch) | plain assembly (prefix dropped — the honest switch) | `assemble_context(query=q, lens=CODE, …)` — task dropped, lens retained (flags-on semantics) |

Implementation anchors (as merged): the `task` parameter narrows recall to rows carrying `task:<slug>` via the existing search `tags` filter, suppresses the project soft-fallback for task-scoped queries, and composes the per-call tail only [assemble.py, PR #360]; `Lens.CODE` activates only on queries matching the code-signal regexes and then admits only `content_type="code"` candidates — a pure order-preserving filter [lens.py]; the `task:` tag contract allows at most one `task:` tag per record, `^task:[a-z0-9_-]{1,64}$` [models.py]; doc grouping rides write-validated metadata `{doc_id, chunk_idx, heading_path}` [models.py `DOC_GROUPING_METADATA_FIELDS`]. `TASK_DISPLAY` = the fixed generator-side slug→display map, committed with the corpus (part of the fingerprint, §4.2).

**Scope-class visibility is symmetric:** every arm receives the same (query, class, current-task) triple and differs only in its mechanism. In real usage the class is knowable at query birth (the question text itself declares whether it is about the current task or about shared/other knowledge); no arm receives gold information.

### 1.4 Ordering, blockers, dependencies

- **No F1 run precedes this file** [dev-plan §4b; ADR-0027 §Phase 1]. This commit satisfies the precondition; the runner does not exist yet.
- The runner wave (`benchmarks/experiments/f1_task_scope/runner.py`, E3-runner canon) and the stratum builder (`benchmarks/strata/f1_mixed/`, the e2-gov/e2-d precedent) land AFTER this file; every implementation parameter they fix that this file left open is registered as a pre-run §8 revision exactly as implemented (the E0 revisions 2–7 pattern). Development smoke dry-runs follow the E0 revision-3 item-4 discipline: collect-only, nothing recorded, no comparison or threshold consulted.
- **No dependency on #248** (pinned projections): no F1 arm pins anything into the top-of-context; the lens only narrows [ADR-0027 invariant 9].
- The E3-lanes falsification is NOT re-litigated here: `lanes_enabled=false` in every arm [ADR-0027 invariant 2].

---

## 2. Hypotheses and operational definitions

Every metric is classified at registration per the ADR-0026 taxonomy: **invariant** (blocking, never carried across a re-baseline), **corridor** (blocking for rollout/unlock), **verdict** (PASS / FAIL / NO-DATA, never blocking, NO-DATA renders explicitly). Primary metric family: per-context recall@5 on per-query-gold [ADR-0027 §Phase 1; dev-plan §4b].

**Registered numeric fills (flagged, not silent — the E0 §3.1 standard):** the sources fix the corridor NAMES and the primary metric but no numbers for: the MDE (+15 pp), the H2 premium margin (+5 pp), the sanity band (0.35 / 0.90), the leakage threshold (0.05), lens-gold-retention (0.95), the G3b sign rule (6 of 8), the budget (2048) and k (5) — each is an E-file choice fixed here before data exists, each traceable to a cited precedent (E0/ADR-0020/ADR-0026 anchors inline); none may be re-derived after the first run.

### 2.1 H1 — scoping value (PRIMARY; verdict) — "beats context-free retrieval"

| Item | Registration |
|---|---|
| Metric | **task-recall@5 on the T-gold stratum**: share of T-gold queries (n = 192, §3.1) for which the query's gold record appears in the top-5 blocks of the arm's assembly, per-query binary outcome, adjudicated per §4.4 |
| Pairing | per-query exact McNemar pairs across arms on the identical corpus build (§6.1); n = 192 |
| Confirmatory comparison | **A vs A0** |
| Confirmatory condition | **A − A0 ≥ +15 pp (absolute)**, exact McNemar two-sided **p < 0.05** |
| MDE | **+15 pp absolute** (power §6.5: 0.82 at the design point D = 0.5, n = 192) |
| Sanity band on A0 (registered, §2.10) | A0 task-recall@5 must land in **[0.35, 0.90]** — below 0.35 the corpus is too hostile (NOISE), at/above 0.90 the corpus cannot discriminate (CEILING INDETERMINATE, §5.1) |

### 2.2 H2 — composition premium (ROUTING verdict; never unlocks alone) — "more than an honest tag-filter"

| Item | Registration |
|---|---|
| Metric | task-recall@5 on T-gold, **A vs C**, exact McNemar pairs, n = 192 |
| Premium condition | **A − C ≥ +5 pp AND p < 0.05** |
| Tie condition (the ADR's honest-risk clause) | A − C < +5 pp OR p ≥ 0.05 → "task-context = honest tag-filter with a different label" — the retrieval value is delivered by the tag filter itself |
| Role | routes the Ф2 FORM (§5.2), never the unlock. Mechanically, A's task condition IS the tags filter [assemble.py] — the premium isolates the lens + parameter semantics + packaging; the tie outcome is the expected-by-design null the ADR pre-accepted |

### 2.3 H3 — primitive vs manual practice (verdict; descriptive routing) — "more than query-prefixing"

| Item | Registration |
|---|---|
| Metric | task-recall@5 on T-gold, **A vs B** and **C vs B**, exact McNemar pairs, n = 192 |
| Reading registered here | B ≈ A (and B ≈ C) means a disciplined user obtains the same retrieval without any primitive — the surviving Ф2 argument is ergonomics (no prefixing discipline, no meta-knowledge), not retrieval; B < C isolates what a structural filter buys over lexical self-scoping. Reported symmetrically; never gating |

### 2.4 G1 — cross-context no-loss (CORRIDOR; blocking) — "without losing cross-task knowledge access"

| Item | Registration |
|---|---|
| Metric | **cross-recall@5 on the X-gold stratum** (n = 48: 24 shared-gold + 24 foreign-task-gold, §3.2), per-query binary, same adjudication |
| Corridor | **cross-recall@5(A) ≥ cross-recall@5(A0) − max(0.02; CI95(A0))** — the ADR-0020 corridor rule, self-comparison against the run's own baseline |
| Scope note (flagged) | under the registered switching policy (§1.3) arms A0/B/C issue identical plain assemblies on X-gold; arm A differs only by the lens. G1 therefore verifies the switching semantics + lens cost nothing cross-context — the always-on alternative is registered as EXPLORATORY (§7, A-naive), not as an arm |
| Power honesty | n = 48 is corridor-grade: unconditional power 0.30–0.53 at +15/+20 pp (D = 0.4, §6.5). G1 is a non-inferiority corridor, not a win test; a G1 breach with a CI that straddles the corridor boundary renders INDETERMINATE-correctable (§5.1), not an automatic kill |

### 2.5 G2 — foreign-context leakage (CORRIDOR; blocking for the treatment)

| Item | Registration |
|---|
| Metric | **foreign-leakage rate on T-gold**: share of issued top-5 blocks (over all T-gold queries of the arm) carrying a `task:` tag **≠ the current task's slug**. Task-less shared rows are NOT leakage (the enclosing scope is legitimately admissible everywhere) — registered definition |
| Corridor (treatment) | **leakage(A) ≤ 0.05**. Structural expectation under the strict tag intersection: 0 — any nonzero leakage in A is a defect signal (filter bypass), > 0.05 = corridor FAIL |
| Descriptive contrast (reported regardless, §6.4) | leakage(A0), leakage(B), leakage(C) — the noise profile context-free retrieval actually lives with; this contrast is the evidence that scoping removes real noise, and is reported even when A = 0 |

### 2.6 G3 — context economy (CORRIDOR; blocking)

| Item | Registration |
|---|---|
| Metric (a) | **median assembled tokens per assembly** on T-gold (n = 192 per arm), from the ContextBlock `tokens` stat — medians and percentiles, never means [ADR-0020 §Alternatives] |
| Corridor (a) | **median(A) ≤ median(A0)**, point rule; bootstrap CI95 of the median difference reported alongside |
| Metric (b) | **tokens-per-completed-task**: summed assembled tokens over one task's full session (its 24 T-gold + 6 X-gold queries), per task, n = 8 per arm |
| Corridor (b) | **A ≤ A0 in ≥ 6 of 8 tasks** (sign rule; exact binomial reported — registered caveat: at 6/8 one-sided p = 0.145, the rule is a consistency check, not a powered test) |
| E-file fills (flagged) | the ADR names "tokens-per-completed-task"; with 8 synthetic tasks the task-level unit is weak by construction — (a) is the statistical unit, (b) the ADR-faithful report. Both registered, both reported |

### 2.7 G4 — lens no-harm and gold retention (CORRIDOR; blocking)

| Item | Registration |
|---|---|
| Metric (a) | **lens-gold-retention**: on T-gold queries where the lens ACTIVATED (query matched `_QUERY_CODE_SIGNALS` — the runner logs activation per query) AND recall placed the gold in the top-5 pre-lens, the share where the gold SURVIVES the lens projection. The lens only narrows [ADR-0027 invariant 6]; dropping recalled gold is its only harm pathway |
| Corridor (a) | **≥ 0.95**; any gold dropped by the lens is itemized in the report |
| Metric (b) | **prose-no-harm on L-neg** (n = 24, §3.3): recall@5(A) vs recall@5(A0) on prose-gold queries carrying incidental code-like tokens — the lens's false-activation trap (the `"class attendance was low"` class [lens.py]) |
| Corridor (b) | **recall@5(A) ≥ recall@5(A0) − max(0.02; CI95(A0))** on L-neg |
| Metric (c) | **lens-precision on code-signal queries**: share of issued top-5 blocks with `content_type="code"` on queries where the lens activated — **descriptive only** (uncalibrated zone: no external reference standard exists [ADR-0027 §Consequences]; internal wording "uncalibrated", never "differentiator") |

### 2.8 Run invariants (blocking, void-on-breach; [ADR-0027 invariants 1–11])

| # | Invariant (operationalized for F1) |
|---|---|
| V1 | Byte-stability: `task`/`lens` compose the per-call tail only — verified per run on a fixed probe set: assembled prefix bytes identical between arm A (cross-class) and A0 on the same query |
| V2 | `lanes_enabled=false` in every arm (E3 verdict not revisited) |
| V3 | The lens never reorders, never pins, never widens (structural; V1 probe + lens-gold-retention is its metric shadow) |
| V4 | Repeat secret scan at issuance: the 8 planted secret-shaped canary rows (§3.4) — **ANY canary content in ANY issued block of ANY arm = run VOID** (invariant, blocking, regardless of every other metric) |
| V5 | Tag contract: every corpus row carries at most one `task:` tag matching `^task:[a-z0-9_-]{1,64}$` (generator-pinned, loader-verified) |
| V6 | Corpus fingerprint + arm flag-state + code version in the manifest BEFORE the run; recorded runs write-once; no statistics computed at run time (§4.3) |

### 2.9 Cross-principal / security posture

Single-agent synthetic corpus; no federation path exercised. Awareness/payload surfaces untouched by any arm (nothing passes `include_awareness`); `awr:*` cursors not exercised. A run violating any §2.8 invariant is **void** — logged as such, never a data point [E0 §4.5].

### 2.10 Sanity floor and ceiling (harness validity; corridor)

Registered reading (the E0 §2.10 pattern): the floor/ceiling is measured on **A0** — the arm with no task mechanism. **A0 task-recall@5 < 0.35** → the corpus is too hostile (task-gold unreachable even unscoped — generator defect, not an arm difference) → **the whole batch renders NOISE**; repair + new registered run. **A0 ≥ 0.90** → the corpus cannot discriminate (no headroom for +15 pp) → H1 renders **CEILING INDETERMINATE** (§5.1) — the E3 committee ceiling-analysis lesson institutionalized: a structural ceiling is informative falsification of the CORPUS, not of the arms.

---

## 3. Strata and corpus

### 3.1 T-gold — task stratum (n = 192; the confirmatory set)

- **8 tasks** for one agent across 2 projects (concurrent-task premise of ADR-0027 §Phase 0 readiness), realistic slugs committed with the corpus (e.g. `payment-webhook-refactor`, `docs-site-search`, `mobile-push-migration`, `q3-capacity-audit`, and 4 more of the same generator family).
- Per task: **12 gold records × 2 query phrasings = 24 queries**; 8 × 24 = 192. Phrasing pairs are the McNemar pairing granularity (the E0 G-gov `gg-NNN-ph/pr` pattern).
- Axis split per task (fixes the doc-axis and lens-axis content): **6 prose records** (knowledge/decisions), **3 code records** (`content_type="code"`; their queries carry code signals so the lens activates — lens-axis gold), **3 doc chunks** (each from a different chunked document; doc-axis gold targeted through `heading_path`).
- Sizing derivation (cited, as commissioned): E0's G-gov confirmatory set = 96 pairs; the S1m corpus = 192 queries; the S5 scripted leg ≈ 100 tasks + 20 negatives [ADR-0026 §6]. F1 takes **192 pairs** — the S1m size — because power at the E0 H3 MDE (+10 pp) was 0.13–0.32 at n = 96 (a registered E0 limitation); at n = 192 the +15 pp MDE reaches 0.82 at the design discordance D = 0.5 (§6.5). 8 tasks (not 4) so foreign-leakage and per-task medians have a base wider than the E0 revision-8 effective-n trap.

### 3.2 X-gold — cross-context stratum (n = 48)

- **24 shared-gold**: gold = task-less shared rows (project/agent scope — the knowledge every task legitimately draws on).
- **24 foreign-task-gold**: gold = rows of a DIFFERENT task (the cross-task knowledge the agent legitimately needs while working on its own task — "what did the docs-site task decide about X").
- Split 6 prose + 6 code records per half × 2 phrasings; issued `cross`-class under every arm's policy (§1.3).
- Feeds G1 (§2.4). No task-class query ever has out-of-task gold (that configuration would be structurally unfair to A/C by the strict intersection — registered exclusion; the "no in-task answer exists" world belongs to X-gold and to the A-naive exploratory arm, §7).

### 3.3 L-neg — lens negative control (n = 24)

Prose-gold queries containing incidental code-like tokens (the `"class attendance"`, `"CSV data"` trap family [lens.py `_QUERY_CODE_SIGNALS` rationale]); gold = prose records. Measures the lens's false-activation cost (G4b). The F1 analogue of E0's G-neg: a negative control for a narrowing mechanism, not for governance.

### 3.4 Corpus shape (versioned, fingerprinted; totals locked at registration)

| Segment | Rows | Notes |
|---|---|---|
| Task-tagged | **360** (8 × 45) | per task: 18 prose, 12 code, 15 doc chunks (3 docs × 5 chunks, `{doc_id, chunk_idx, heading_path}`); 12 of 45 are gold (§3.1) |
| Shared (task-less) | **120** | project/agent-scope knowledge + 2 shared chunked docs; 12 gold (§3.2) |
| Distractor mass | **480** | **278 checkpoints** (58% of the distractor mass — the live-store drowning echo, E0 §3.3) + 194 session/misc noise + **8 secret-shaped canaries** (V4) |
| **Total** | **960** | task mass 37.5% — task-tagged rows are a minority by design (the realistic pain the epic answers); **no live multicontext store exists to distribution-match — the profile is a registered design choice, flagged, not a measurement** (S5 v1 is synthetic-only [ADR-0026 §6]) |

Competition structure (the mechanism under test): any task's 12 gold records compete unscoped against 948 other rows (A0/B) vs 33 in-task rows scoped (A/C). Replacement pool: +25% surplus gold records generated with the same protocol (analyzed denominators stay 192/48/24; replacements logged — the E0 §3.1 discipline).

### 3.5 Ground-truth protocol (locked here, before generation — the E0 §4.4 pattern)

A (query, record) pair is gold iff all three hold: (1) **topical match** — the query asks about exactly the fact/decision/artifact the record encodes; (2) **self-sufficiency** — the record alone answers, no companion record required; (3) **non-adjacency** — not merely lexically co-occurrent (same task/doc/project name) while answering a different question. The generator declares gold at birth; the **blind applicability audit** runs on a deterministic 20% subsample (all pair-ids ranked by sha256 hex ascending, top ceil(20%) double-annotated — the E0 revision-3 rule, frozen pre-run), **κ ≥ 0.6** else recalibrate before scoring; rejected pairs replaced from the pool, logged. Seed hygiene: declarative prose, no `applyTo`, no severity tags, no second-person imperatives; canaries are the ONLY secret-shaped content and are inert by the quarantine semantics. Honest residual, accepted at registration: ground truth is only as strong as this criterion; it is reported with every result.

---

## 4. Modes and protocols

### 4.1 Equal-budget (primary mode)

All cross-arm comparisons at **identical assembled-context token budgets (2048)** [E0 §4.1]. A budget-inflated run of any arm is report-only, never a basis for conclusions: scoping must not buy recall with extra tokens. Arm C's search-level results are formatted through the standard block formatter under the same budget — comparability of the top-5 surface is a runner requirement registered here.

### 4.2 Determinism and corpus fingerprint

Single corpus build, committed with its generator, seed, `TASK_DISPLAY` map, and per-segment counts; **BLAKE2b-lexical fingerprint** stamped into the run manifest BEFORE the run (anti-cherry-picking [ADR-0027 §Phase 1]). Isolated byte-identical store copy per arm (SQLite backup API — the S5/E3 precedent); deterministic embedder, scanner off, frozen clock; wall-clock quantities never enter metrics [ADR-0026 §6]. Any corpus or issuance-path change before the first recorded run → the fingerprint change is a dated §8 act, not silent drift; after it → event-driven re-baseline in the same PR [ADR-0020 §Re-baseline triggers].

### 4.3 Runner canon (binding requirements for the future `benchmarks/experiments/f1_task_scope/runner.py`)

The E3-runner contract, inherited verbatim [E0 §6.6; ADR-0027 §Phase 1]: default invocation **refuses to record** (collect-only executes every arm, prints the manifest, persists nothing); `--record` is the explicit opt-in; recorded runs are **write-once** (content-addressed run id over the deterministic core: corpus fingerprint + analyzed-qid ledger state + code version + arm flag block + budget; a second `--record` of the same id fails loud). Artifacts under `benchmarks/experiments/f1_task_scope/runs/<run_id>/` (gitignored; committed deliberately by the report wave). **NO statistics computed at run time** — no p-values, no CIs, no verdicts in artifacts; per-query outcome tuples per arm + discordance tallies only; single-look analysis after collection (§6.6). Appending the run-ledger entry (§9) is part of the deliberate first-run step.

### 4.4 Adjudication and blindness

Outcomes are deterministic (gold-id ∈ top-5 issued ids — mechanical, no judge in the loop). The human judgment layer is the §3.5 applicability audit alone: annotators see (query, record) pairs only, never arm ids, never run provenance; annotation artifacts are arm-stripped. κ < 0.6 → recalibrate before scoring (the ADR-0026 calibration floor).

### 4.5 Invariant checks per run

V1 prefix-stability probe, V4 canary scan at issuance (every block of every arm), V5 tag-contract verification at load, V6 manifest completeness — executed by the runner, logged in the manifest; any breach = void run (§2.8, §6.3). Lenses never touch the pinned prefix and only narrow [ADR-0027 invariants 6/9]; no arm pins anything into the top-of-context (#248 stays open and untouched).

---

## 5. Falsifiers and decision rules

### 5.1 The H1 lattice (every (delta, p, guardrail) combination maps to exactly one row; no zone can be picked after seeing data)

Zone priority (explicit): NOISE > NO-DATA > CEILING INDETERMINATE > the §5.1 rows — a batch classified NOISE or NO-DATA never renders an H1 verdict; CEILING is evaluated before FALSIFIER/INDETERMINATE (an A0 ≥ 0.90 batch is CEILING even if its p ≥ 0.05).

| Outcome | Condition (task-recall@5, T-gold n = 192) | Consequence |
|---|---|---|
| **PASS — Ф2 UNLOCK** | A − A0 ≥ +15 pp AND p(A vs A0) < 0.05 AND G1–G4 all hold | Phase 2 opens; the primitive's form routed by §5.2 |
| **H1 met, guardrail failed** | the H1 condition holds while any of G1–G4 breaks | rollout blocked (the E0 "H3 met, H4 failed" rule); not PASS; remediation or re-cut via a NEW pre-registered experiment — the scoping win cannot ship on a broken guardrail |
| **FALSIFIER zone** | **A − A0 < +5 pp OR p(A vs A0) ≥ 0.05** | theory NOT confirmed: task-scoped composition does not beat context-free retrieval on this corpus — **the task primitive is NOT built**; the outcome is recorded in ADR-0027 [§Consequences]; the Ф0 surface stays as shipped (optional parameters, inert by default — no rollback needed, nothing is default-on) |
| **INDETERMINATE (residual)** | A − A0 ∈ [+5, +15) pp with p < 0.05 | no unlock, no kill; re-opening requires a NEW pre-registered experiment (re-thresholding or re-zoneing the same data is prohibited — HARKing) |
| **CEILING INDETERMINATE** | A0 ≥ 0.90 (§2.10) | the corpus cannot discriminate; informative falsification OF THE CORPUS, not of the arms (the E3 ceiling lesson); re-cut the corpus, new registration |
| **NOISE** | A0 < 0.35, or harness/runner defect, or V1–V6 breach | batch renders NOISE — not a verdict on any arm; repair ticket + new registered run; voided runs logged as void |
| **NO-DATA** | missing probes / adjudication collapse (κ unfixable) | renders explicitly — never as zero, never silently dropped [ADR-0026 §5] |

**Interpretation note for the FALSIFIER row (registered):** under the strict tag intersection, A's task mechanism is mechanically the tags filter; the falsifier primarily tests whether SCOPING (any mechanism) beats unscoped retrieval on this corpus. The decomposition of "which mechanism" is §5.2's job, not the falsifier's.

### 5.2 Ф2 form routing (H2/H3 verdicts; routing never unlocks — only §5.1 PASS unlocks)

| H2 outcome (A vs C) | H3 outcome (A vs B) | Ф2 form routed |
|---|---|---|
| premium: A − C ≥ +5 pp, p < 0.05 | any | composition delivers beyond the bare filter (the lens + parameter semantics): the richer forms are eligible — `task` parameter hardening + lens preset family; `task_id` column / `tasks`+`task_members` table only per migration-pain data [ADR-0027 §Phase 2] |
| tie: A − C < +5 pp OR p ≥ 0.05 | B < C (p < 0.05) | "task-context = honest tag-filter with a different label" — no NEW primitive; Ф2 reduces to reinforced tag-infrastructure hardening (an owner decision, not a schema migration); recorded in ADR-0027 as the pre-accepted honest outcome |
| tie (as above) | B ≈ C and B ≈ A | even the emulation matches: the surviving argument is ergonomics alone (no prefixing discipline) — weakest Ф2; owner arbitrates whether ergonomics justify hardening |
| premium (as above) | B ≈ A | primitive beats manual practice AND the filter: full §5.1 routing stands |
| any other combination (e.g. tie ∧ B > C) | — | default row: no premium claim is available — treat as the tie row's tag-infrastructure hardening; the owner arbitrates any residual disagreement before Ф2 planning |

### 5.3 Multiple-comparison rule (registered, flagged — the E0 §5.1 precedent)

Three McNemar comparisons are registered (A vs A0 primary; A vs C, A vs B routing/verdict): each at exact two-sided α = 0.05, **no familywise correction** — choosing a correction after seeing data would be post hoc. Flagged consequence: the family-wise α across the three is inflated (~0.14 under independence); only the A-vs-A0 comparison can unlock Ф2, and the routing verdicts (§5.2) never gate — the inflation is disclosed, not laundered.

### 5.4 Guardrail precedence

G1–G4 are **blocking for the unlock** regardless of H1 (a scoping win that loses cross-task access, leaks foreign context, inflates tokens, or drops lens gold cannot ship). G2 applies to the treatment (A); leakage of A0/B/C is the descriptive contrast (§2.5). Corridor failures with CIs straddling the boundary render INDETERMINATE-correctable (a NEW registration), never silently passed. **Value never blocks, invariants do** [ADR-0026 §5]: a red verdict is a reportable result; a broken invariant voids the run.

---

## 6. Analysis plan (frozen)

### 6.1 Pairing, tests, k

Pairing unit = the identical query under two arms (same corpus build, same store bytes, deterministic stand). Exact (binomial on discordant pairs) two-sided McNemar, α = 0.05, per comparison (§5.3) — an E-file choice flagged per the E0 §5.1 standard. **Primary k = 5, frozen.** recall@3 and recall@10 are registered EXPLORATORY (§7) — descriptive only, never gating, reported for all arms symmetrically.

### 6.2 Interval reporting

Wilson score CI95 for every proportion; medians + bootstrap CI95 for token metrics; percentile reporting, never means [ADR-0020 §Alternatives]. Realized discordance reported with every McNemar result (the E0 §6.5 discipline).

### 6.3 Verdict taxonomy mapping [ADR-0026 §5]

| Class | Metrics in this experiment |
|---|---|
| invariant (blocking, void-on-breach) | V1–V6 (§2.8): canary trace = 0; prefix byte-stability; lanes-off; lens-narrows-only; tag contract; manifest completeness/write-once |
| corridor (blocking for unlock) | G1 cross-recall floor; G2 leakage ≤ 0.05 (treatment); G3 token medians + sign rule; G4a lens-gold-retention ≥ 0.95; G4b prose-no-harm; sanity band [0.35, 0.90] |
| verdict (PASS / FAIL / NO-DATA, never blocking) | H1 (A vs A0); H2 (A vs C); H3 (A vs B, C vs B); lens-precision (descriptive); leakage contrast (descriptive); tokens-per-task per-task totals |

NO-DATA / NOISE / void render explicitly — never as zero, never dropped. Null and negative results are results.

### 6.4 Subgroup discipline and symmetric reporting

Confirmatory and gating analyses are restricted to the registered strata: T-gold (H1/H2/H3, G2, G3a), X-gold (G1, G3b), L-neg (G4b), lens-activation subset of T-gold (G4a). Per-stratum only — no pooled cross-stratum aggregate enters any gate [ADR-0027 §Phase 1]. Per-task breakdowns (8 tasks) are reported descriptively, never gating. **No unregistered subgroup cut** (query length, phrasing index, project, task identity, ordering) may support any decision; any such cut is exploratory, labeled, non-gating. All four arms reported symmetrically; no arm removed post hoc; no lens or parameter chosen after seeing outcomes (the E0 G-neg lesson: thresholds are pilot-free by design here — the sanity band §2.10 replaces pilot calibration and is registered before data exists).

### 6.5 Power notes (registered before any run; unconditional exact two-sided values, the E0 revision-8 convention)

Method: exact binomial on discordant pairs, unconditional (discordance b ~ Bin(n, D) marginalized; the runner MUST reproduce the E0 revision-8 anchor quartet 0.3544 / 0.6800 / 0.5477 / 0.8794 exactly (same arithmetic) — a pre-run requirement; the §8 amendment ledger freezes any drift).

| n = 192 (T-gold) | D = 0.3 | D = 0.5 (design point) | D = 0.7 |
|---|---|---|---|
| +10 pp | 0.684 | 0.463 | 0.351 |
| **+15 pp (MDE)** | 0.969 | **0.820** | 0.676 |
| +20 pp | 1.000 | 0.975 | 0.906 |

- The design point is D = 0.5 (plausible: scoped and unscoped arms disagree on roughly half the queries when scoping matters); at D = 0.7 the MDE power drops to 0.68 — a registered limitation, stated before the run. Realized discordance reported with the result; a confirmatory claim at the MDE is supportable only if realized power is disclosed alongside.
- **X-gold (n = 48, D = 0.4):** +15 pp → 0.299, +20 pp → 0.528 — corridor-grade only (G1 is non-inferiority, §2.4); no win claim is registered on X-gold.
- **L-neg (n = 24):** G4b is a corridor with the ADR-0020 rule; powered only for large lens harms — small harms will render as INDETERMINATE-correctable, disclosed as such.
- **G3b sign rule (≥ 6 of 8):** consistency check, one-sided p = 0.145 at the boundary — a product-risk rule, not a power-derived one (the E0 D4 precedent for point-rule honesty).

### 6.6 Single-look, run ledger, re-baseline

Single-look: the registered analysis runs once per arm-set after collection completes; no interim comparison, no peeking. Run ledger (§9, append-only): every run logged with run id, date, arms, corpus fingerprint, flag block, ledger state; voided runs logged as void. Re-baseline triggers [ADR-0020]: corpus × 2 growth, embedder/processing-model change, composition-algorithm or issuance-path change → event-driven re-baseline in the same PR; invariants never carry across a re-baseline [ADR-0026 §5].

---

## 7. Registered exploratory measures (reported, never gating)

| Measure | Description | Rationale |
|---|---|---|
| **A-naive (always-on policy)** | cross-class queries issued WITH `task=<slug>` (no switching): expected structural collapse on X-gold under the strict intersection | quantifies the cost of the naive policy and documents WHY the switching semantics are load-bearing — the honest complement to G1; an arm-shaped measurement registered as exploratory precisely so it cannot become a post-hoc arm |
| recall@3 / recall@10 | same strata, k swept | rank-sensitivity of the H1 picture; descriptive, all arms |
| Leakage contrast | leakage(A0), leakage(B), leakage(C) with Wilson CIs | the noise profile context-free retrieval lives with — the value evidence behind G2 |
| Lens-precision | share of code-content blocks on lens-activated queries, per arm | uncalibrated zone [ADR-0027 §Consequences]; baseline for future calibration, never a differentiator claim |
| Token distributions | full per-assembly token histograms per arm/stratum | economy picture beyond the G3 medians |
| Doc-chunk neighborhood | whether parent-doc sibling chunks co-occur in issues (doc-metadata grouping signal) | Phase-3 (docs-as-memory) design input; no gate exists for it in F1 |

---

## 8. Amendment log

**2026-09-20 — registered. No amendments.**

Standing rules: before the first recorded run, implementation-parameter registrations (runner, stratum builder, corpus generator) enter as pre-run revisions exactly as implemented — honest pre-run editing, not HARKing (the E0 revisions 2–7 pattern; the E-file window stays open until the first `--record`). After the first recorded run, any deviation — a changed metric, threshold, stratum, arm, or analysis choice — requires a dated entry stating what changed, why, and which run prompted it. **A logged deviation is a report of what happened, never an authorization: the registered analysis stands and is reported as registered; any analysis under changed rules is reported alongside as exploratory (§6.4), never as the confirmatory result, and never replaces the registered verdict.**

**2026-09-20 — pre-run revisions (14), all before any run; attribution: F1-runner wave (review round 1 + TL decision). Sections 1–7 stand as registered; these entries register the implementation exactly as shipped.**

1. **Stratum generator location/version** — the corpus generator lives INSIDE the experiment package (`benchmarks/experiments/f1_task_scope/corpus.py`), not under `benchmarks/strata/`: §1.4 anticipated a separate stratum-builder wave; the runner wave ships corpus and runner together (§1.4 dependency note). Generator version `f1-mixed-1` (superseded by entry 13's `f1-mixed-2`).
2. **Corpus content re-cut, pre-record (ceiling repair)** — the first generator cut reached A0 task-recall@5 = 192/192 (vacuous ceiling), the second 190/192; the shipped §3.5 competition implementation (adjacent rows: same noun, different question; test-wrapper twins; drowning-echo checkpoints speaking the gold vocabulary) brought A0 to 158/192 — inside the registered sanity band [0.35, 0.90]. Frozen before any run; no outcome threshold was consulted; the mix is the registered three-family difficulty split (a birth property), untouched since.
3. **X-gold foreign gold = same-project rows** — the 24 foreign-task gold rows are rows of a different task of the SAME project (the A9 project-predicate reachability premise): the honest cross-task world the X-gold stratum models (§3.2). A cross-project gold would be structurally unreachable for every arm under the project predicate — registered exclusion.
4. **L-neg registered cross-class** — every L-neg query issues `cross`-class (the §1.3 switching policy applies; there is no L-neg task-shaped issuance). This entry is superseded/folded into entry 13: the original all-trap composition made the G4b corridor structurally unfalsifiable; the mixed-stratum repair re-cut it (conservative direction — see entry 13).
5. **Arm C formatter** — arm C's search-level results are formatted through the standard block formatter running the assembled pipeline's per-block stages (context filter, issuance scan, CacheAligner, provenance wrap, greedy whole-block budget inclusion) at the same 2048 budget — the §4.1 "comparability of the top-5 surface" requirement, registered as implemented.
6. **One session id + frozen retrieval stamp** — a single session id (`f1-task-scope`) across all arms, with the session's retrieval stamp pre-seeded at the scenario RUN_NOW (2026-09-20T12:00:00Z): required for V1 byte-comparability of provenance bytes across arms; wall-clock never enters metrics.
7. **Seeded uuid5 ids + clone per arm** — the corpus build runs under a harness-only seeded uuid5 draw (the tests/_seeded_ids technique, #280) so the committed store reproduces byte-identically, and each arm executes over its own byte-identical clone of the single build (SQLite backup API; content digest verified equal across arms).
8. **V1 probe set / V3 shadow level** — V1 (prefix byte-stability) probes exactly the lens-INACTIVE cross-class queries (arm A's identity projection must be byte-identical to A0); V3's metric shadow runs at CANDIDATE level (issued ⊆ pre-lens candidate order, code-only) because the budget stage's reflow makes issued-set comparisons across arms illegitimate — budget-reflow justification, registered as implemented.
9. **Stat-ban regex `ci\d+`** — the run-time statistics ban is schema (exact key allowlists + a recursive key scan over artifacts); the interval-shaped key pattern is `ci\d+`, not the e3 spelling `ci\d*`: the latter false-fires on the committed task slug `q3-capacity-audit` (ordinary English keys containing "ci").
10. **§9 append in code under `--record`** — the run-ledger entry is appended by the runner itself, only as part of the deliberate `--record` step, with guards: duplicate run id refused; §9 must be the doc's last section (so an EOF append is a §9 append; sections 1–7 are never touched — the frozen-doc property the tests pin).
11. **G4a probe implementation** — the pre-lens gold probe is the tags-filtered `mgr.search` (exactly what `_recall_stage` issues on the task leg), the post-lens probe its code-only order-preserving subsequence via the stored `content_type` — the retention metric reads the pipeline's own orders, not a re-implementation.
12. **Manifest retrieval pins** — the manifest records `hybrid_alpha=0.5` (the config default of the shared store-settings shape; probe finding 6, #300) and `recall_depth=10` — the recall surface every arm search ran under, pinned for the re-baseline triggers (§6.6).
13. **G4b falsifiability repair (this wave; review round 1, TL-decided P2)** — defect: the original L-neg composition asserted every query NON-activating ⇒ arm A's cross-class call was the identity projection ⇒ byte-identical to A0 (V1) ⇒ recall(A) == recall(A0) on L-neg always ⇒ the §2.7b corridor could never fail. Repair: the stratum is MIXED — 16 trap queries (the registered must-not-activate majority, unchanged) + 8 activating mixed-phrasing queries (4 pairs × 2 phrasings: prose-framed questions carrying code-shaped call tokens — `retry(backoff=5)`, `refresh_token(grace=72)`, `index_page(doc.url)`, `autoscale(pool, warm)` — whose gold stays a task-less PROSE row the active lens's code-only narrowing can drop). Conservative direction only: the change can hurt arm A, never help it; T-gold/X-gold composition and the difficulty mix untouched; no threshold consulted; no new rows (the 960-row totals stand; the 4 converted pairs re-cut existing gold rows in place). Generator `f1-mixed-1` → `f1-mixed-2`; corpus fingerprint `efd1a0cfed53f113eb6a642b754d2cb16fef82cd9f3ff508c2bd20adce9ad338` → `debd849ab02ad02ef8581573e44f6855be5fbb06501acbdad5ead7d6872ae112`; L-neg surplus pairs redrawn from trap pairs (1/3/5) so replacements keep the trap shape. V1 probes 48 → 40, V3 shadow probes 24 → 32 (the 8 activating L-neg queries join the lens-active probe class).
14. **Repair-wave machinery registrations (review round 1, P3 × 4)** — (a) arm C dual token basis: every per-query tuple carries `tokens` (the registered basis: C = top-5 formatted blocks, A0/B/A = the full assembled estimate) AND `tokens_full` (C's full formatted-assembly estimate over every budget-included block — the basis comparable with the assembled arms; equal to `tokens` on A0/B/A); (b) `--record` rollback semantics: §9 appendability is pre-validated (duplicate id + §9-last) BEFORE any artifact is written, and an append failure after artifacts exist quarantines the run dir as `<run_id>.UNLEDGERED` and exits loud — no silently half-recorded state; (c) manifest exactness: `verify_manifest` enforces the exact top-level key set (unexpected == missing == broken) with the recursive stat-key scan already applied to the manifest too, and the stat-ban additionally covers the bare spellings `p` / `pval(s)` / `p…value` / `power` / `ci[\W_]?\d+` (keeping the entry-9 `ci\d+` semantics — no `q3-capacity-audit` false fire); (d) generator assert: no corpus row carries a governance-selecting tag (`mnemos:rule`/`mnemos:decision`) — a governance row would be re-ranked by `type_boost` in `_recall_stage` but not in the raw `mgr.search` probes, breaking the G4a/V3 probe-order equivalence (entry 11).

**2026-09-21 — pre-run revision (15); attribution: TL decision, review round 2 — G4b evaluation scope. Amendments stop here.**

15. **G4b corridor scope + the mixed-phrasing activation-cost measure.** The honest three-stage history, all pre-run: (i) the ORIGINAL corpus pinned every L-neg query non-activating ⇒ arm A's cross-class call was the identity projection, byte-identical to A0 ⇒ recall(A) == recall(A0) on L-neg tautologically ⇒ the §2.7b corridor was vacuous-GREEN (could never fail — the round-1 review defect); (ii) the round-2 mixed stratum (entry 13) fixed falsifiability but over-corrected: on TRUE activation the lens hard-excludes prose rows, so the 8 activating pairs lose their prose gold DETERMINISTICALLY ⇒ the corridor was guaranteed-RED (could never pass) — the mirror defect, and per §5.1 a predetermined guardrail failure would void any Ф2 unlock by corpus construction rather than by data, a HARKing-class sin that must not reach the first `--record`. FINAL DESIGN (this entry): the §2.7b corridor (recall@5(A) ≥ recall@5(A0) − max(0.02; CI95(A0))) evaluates on the TRAP subset ONLY — the 16 must-not-activate queries, §3.3's original false-activation control family — and is falsifiable BOTH ways (lens regexes broaden onto a trap phrasing ⇒ A loses trap gold ⇒ corridor FAILS; lens well-calibrated ⇒ corridor passes). The 8 activating mixed-phrasing queries become a NEW registered DESCRIPTIVE measure — "mixed-phrasing activation cost" (per-query tuples, all arms symmetric, reported descriptively per the §7 discipline; NEVER gating): the known deterministic −33 pp shape on this subset (A drops all 8 golds A0 reaches) is a REPORTED result for the owner/Architectural Committee to weigh — exactly the #368 risk class — not an automatic corridor verdict. Data shape (the split rides IN the artifacts, not in analysis-wave prose): every L-neg per-query outcome row carries the committed `substratum` flag (`trap` | `mixed`), derived from the fingerprinted `mixed` birth flag of the committed corpus; the exact row-key schema is extended accordingly (L-neg rows: +`substratum`; non-L-neg rows unchanged) and `verify_outcomes` enforces it; the manifest's corpus block pins the run's own `l_neg_composition` (bootstrap: 16 trap + 8 mixed; a §3.5 adjudication replacing a rejected mixed pair with trap-shaped surplus shifts it — recorded, reported, never silent). Corpus CONTENT is unchanged by this entry (fingerprint stands at `debd849ab02ad02ef8581573e44f6855be5fbb06501acbdad5ead7d6872ae112`, generator `f1-mixed-2`); the runner version bumps to `f1-task-scope-runner-2` (outcome-row schema + manifest extension) and the run id changes with it. V1/V3 probe sets are UNCHANGED by this entry (V1 = lens-inactive cross queries: 24 X-gold prose + 16 L-neg trap = 40; V3 shadow = lens-active cross queries: 24 X-gold code + 8 L-neg mixed = 32 — the corridor re-scope does not touch probe definitions). **Amendments stop here: no further evaluation-scope, stratum-composition, or schema changes before the first `--record`.**

**2026-09-21 — pre-run revision (16); attribution: TL correction to entry 15 — trap-corridor data path. The amendment window closes again with this entry.**

16. **Lens-dependent expectations moved from build-assert to recorded observation.** Entry 15's both-ways mechanism was NOT literally true as implemented: the corpus generator hard-asserted every query's lens activation equal to its birth property (code-axis or L-neg mixed activate; traps inert), and the runner builds the corpus at RUN START — so a future broadened lens would crash the corpus build BEFORE any arm executed, preempting the trap corridor's red path: it would surface as a build crash, never as run DATA. That repeats the round-1 principle in a new costume — a corridor that cannot produce red data is not a measurement. Correction (this entry): the per-query lens-activation assert is REMOVED from the corpus build; lens activation is designed at birth and OBSERVED at run time — the manifest's corpus block stamps `lens_observed: {trap_activations, mixed_activations, code_axis_activations}` (counts over the run's active analyzed set — run-time facts of the run's own code version; manifest content, not statistics), schema-enforced by `verify_manifest` and echoed in the collect summary. The red path now flows through data: under a broadened lens, `trap_activations > 0` ⇒ the activating traps lose their prose gold in arm A ⇒ the §2.7b trap corridor FAILS in the analysis, visibly, from the recorded tuples. ALL STRUCTURAL asserts stand unchanged (strata 192/48/24; L-neg composition 8 mixed + 16 trap; mixed-gold cross-class prose via the ingest-side classifier; governance-free; fingerprint determinism; locked segment totals) — they pin corpus SHAPE, independent of lens code. Current lens behavior is pinned at the TEST layer, the right one (a lens change that flips any activation fails CI, not the run): trap_activations == 0, mixed_activations == 8, code_axis_activations == 72. Runner version bumps to `f1-task-scope-runner-3` (manifest extension); corpus CONTENT unchanged (fingerprint stands at `debd849ab02ad02ef8581573e44f6855be5fbb06501acbdad5ead7d6872ae112`, generator `f1-mixed-2`); the run id changes with the manifest. **The amendment window, re-opened for this correction alone, closes again here: no further evaluation-scope, stratum-composition, or schema changes before the first `--record`.**

---

## 9. Run ledger (§6.6, append-only)

*(empty — no run recorded; the window is open)*

**2026-09-27 — RUN — arms A0/C/B/A — `f1-task-scope-9c08f24268dc` — recorded by explicit `--record`.** Corpus fingerprint `debd849ab02ad02ef8581573e44f6855be5fbb06501acbdad5ead7d6872ae112` (generator f1-mixed-2, seed 20260920, 960 rows); ledger state `f02ee67a2304…` (rejects=0, replacements=0, analyzed 192/48/24 — L-neg split 16 trap + 8 mixed, §8/15); budget 2048, top-5, lanes off, type-boost on, frozen clock 2026-09-20T12:00:00+00:00; code 6bc6265fbb70… (mnemos 4.3.0, python 3.12.3). Artifacts: `benchmarks/experiments/f1_task_scope/runs/f1-task-scope-9c08f24268dc/` (gitignored by design; committed deliberately by the report wave when citing). Invariants V1-V6 verified in the manifest; no statistics at run time — single-look analysis follows collection (§6.6).
