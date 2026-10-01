# E0 — Pre-Registered Experiment: Memory Meta-Level (Lanes B/B0/A · Collapse Cascade C · Awareness D)

| Field | Value |
|---|---|
| Status | **PRE-REGISTERED — committed before the first run** |
| Date | 2026-09-13 |
| Commit window | **Open.** Per ADR-0025 the E0 window is open as of this commit: no benchmark run of any leg (A/B/B0, C0–C3, D) has occurred. This file precedes all runs (issue [#252](https://github.com/Korrnals/vesma/issues/252), acceptance clause 1). |
| Owner | Analytics Lead (experiment design); G-gov ground-truth criterion co-owned with a Security observer for provenance (R1 open point 3, resolved here in §4.4) |
| Anti-HARKing clause | **No metric, threshold, stratum, or hypothesis may be added or altered after the first run.** Deviations must be logged in a dated amendment section (§8). Single-look analysis; no interim peeking (§6.6). |
| Language | English (canonical, ADR-style) |

**Sources (authoritative, cited inline as shown):**

1. [ADR-0025 — Memory Meta-Level Lanes](../project/adr/0025-memory-meta-level-lanes.md) — `[ADR-0025 §…]`
2. Committee protocol R1 (2026-09-08) `2026-09-08-memory-meta-level-lanes.md` and its contract — `[R1 §…]` — team-local (`~/.gcw/architectural-committee/`), not part of this repository
3. Committee protocol R2 (2026-09-09) `2026-09-09-memory-segments-collapse-cascade.md` — `[R2 §…]` — team-local
4. Committee protocol R3 (2026-09-09) `2026-09-09-awareness-presence-delta.md` — `[R3 §…]` — team-local
5. [ADR-0020 — Benchmark Framework](../project/adr/0020-benchmark-framework.md) — `[ADR-0020 §…]` (stands S1m/S2/S3, McNemar pairing policy, corridor rule `baseline − max(0.02; CI95)`, event-driven re-baseline triggers)
6. [ADR-0026 — Memory Value Observability](../project/adr/0026-memory-value-observability.md) — `[ADR-0026 §…]` (invariant / corridor / verdict taxonomy; the `docs/experiments/` pre-registration canon)
7. [ArchCom meta-level cycle report (2026-09-09)](../project/reports/2026-09-09-archcom-meta-level-cycle-report.md) and [dev-plan §4a](../project/dev-plan.md) — run ordering, slice gates, dependency status

**Interpretation boundary (binding, [ADR-0025 §Decision]):** a positive result validates the retrieval-side structure (lanes mechanics, collapse cascade, awareness composition) — not a "brain zeroth layer" and not "memory organized as a brain". The metaphor enters no schema, API, or metric wording.

---

## 0. Traceability — issue #252 scope item → document section

| Issue #252 scope item (checklist) | Section(s) |
|---|---|
| Legs A/B/B0 defined; primary metric governance-recall@5 on G-gov; falsifier "B does not beat B0 → theory NOT confirmed" | §1.1, §2.3, §5.1 |
| Hypotheses H1–H4 (latency, KV-cache, governance-recall, guardrail) with exact thresholds | §2.1, §2.2, §2.3, §2.4 |
| G-gov stratum (~96 queries, ~60–100 seeded records, 2 queries/record) + G-neg (~24) + distribution-matched corpus profile (58% checkpoints) | §3.1, §3.2, §3.3 |
| Equal-budget as primary mode; pin-cost curve 5/15/30% | §4.1, §4.2 |
| H5: retention-or-report = 1.000 invariant on seeded markers; cross-session answerability vs C0 (mandatory trivial leg) | §2.5, §5.3 |
| collapse-precision ≥ 0.95 (anti-hallucination) | §2.5, §5.3 |
| C1 → C2 → C3 sequential gates; 2×2 factorial B×C1 only after individual passes | §1.2, §1.4, §5.3 |
| Multi-session stratum (~80–100 scenarios, 160–200 paired probes) in E2 | §3.4 |
| G-poison adversarial stratum; ANY trace in C-outputs = FAIL | §3.5, §5.3 |
| C1 blocked until vesma-refine (#223) smoke-validation | §1.4 |
| D1 (primary): intrusion on type-2 −20pp, McNemar p<0.05, MDE 20pp | §2.6, §5.4 |
| D4 (co-equal primary): over-deferral ≤ 0.05 on 40 stale-claims | §2.9, §5.4 |
| D2: t_eligible p95 ≤ 60s; stale-action share ≤ 0.05 | §2.7 |
| D3: ≤ 300 tokens/call, ≤ 5% budget; KV-guardrail LCP ≥ 512 | §2.8 |
| Type-1 sanity floor ≥ 90% (else harness broken → NOISE) | §2.10, §5.4 |
| Strata: 80 conflict pairs (≥40 type-2) + 40 stale-claims + 200 canaries (false-drop ≤ 0.01) + adversarial-peer | §3.6, §5.4 |
| PR #224 replay as permanent scenario | §3.7 |
| Acceptance: file committed before the first benchmark run | Header (status + commit window) |
| Acceptance: all falsifiers pre-registered; no metric added after first run | §5, §6.4, §8 + anti-HARKing clause |

---

## 1. Experiment legs overview

### 1.1 Lanes legs (R1 → ADR-0025)

| Leg | Role | Treatment | Runs in |
|---|---|---|---|
| **A** | control | current `assemble_context` pipeline as-is (`LanesConfig.enabled=false`, code path identical) | E3 |
| **B** | treatment | lanes (rules/decisions/knowledge) + governance pinned to top + stable lane ordering (`lane → (score desc)` in the budget stage) | E3 |
| **B0** | trivial (anti-confounding) | type-boost of rules/decisions at recall — one ranking line, zero meta-level | E3 |

B0 exists because any pinning mechanics lifts drowned governance; without the trivial leg a positive result is confirmation bias [ADR-0025 §Alternatives]. B's only structural contribution B0 cannot give is the byte-stable pinned prefix (lane ordering) — that is the KV-cache argument (H2) [R1 §System Engineer].

The v0 experiment carries **no manifest lane at all**: metrics live on rules, decisions, knowledge, and checkpoints over existing records; pinning goes through the operator CLI only [ADR-0025 §Binding security controls].

### 1.2 Cascade legs (R2)

| Leg | Role | Treatment | Gate to next level |
|---|---|---|---|
| **C0** | trivial (mandatory) | mechanical compressor of a token budget equal to the C1 output budget — no LLM | establishes the H5 comparison baseline |
| **C1** | treatment, session level | session-scoped collapse via vesma-refine (`scope_key='s1:<session_id>@v<n>'`) | full H5 pass at session level |
| **C2** | treatment, project level | project-scoped collapse (incremental via meta cursors) | full H5 pass at project level |
| **C3** | treatment, cross-project | cross-project collapse — **first multi-principal trigger** | single-operator threat-model revision BEFORE any C3 run + operator decision per run [R2 §Security] (timing flagged §5.3) |

"C0 without the trivial leg any collapse looks like a victory — the lesson of B0" [R2 §Decision]. C0 also supplies the superseded pointers that D2 depends on (§1.4).

### 1.3 Awareness leg (R3)

| Leg | Role | Treatment | Gate |
|---|---|---|---|
| **D** | treatment (vs control = same scenarios, awareness off) | awareness v0: presence + delta + conflict hints as a hooks composition (`include_awareness` in `pre_llm_call`, presence section in `on_session_start`, MCP tool `mnemos_awareness`) — NOT a seventh assemble stage | **D1 AND D4, co-equal** (§5.4) |

### 1.4 Ordering, blockers, dependencies

- **No lanes/cascade/awareness run precedes this file** [dev-plan §4a rule 1]. This commit satisfies that precondition.
- **P0 collapse-bug batch #250** (quarantine intake, strip-by-default, ANY-member no-federate, input_set_hash idempotency) precedes any C-leg run — a first run over an unfixed pipeline would canonize an unprotected baseline [R2 §Decision]. Status: merged 2026-09-13 (PR #260) — recorded, not re-litigated.
- **C1 is blocked until vesma-refine (#223) smoke-validation**: `synthesize.py` is a deterministic stub (`llm_called=False`); C1 over the stub measures a 200-character concatenation, not collapse [R2 §Decision, §System Engineer]. Issue [#223](https://github.com/Korrnals/vesma/issues/223).
- **D-leg prerequisites**: D0 blocker #251 (agent hardcode) resolved 2026-09-13 (PR #263); `origin=` provenance (P0) merged 2026-09-13 (PR #262). **D2 additionally depends on superseded pointers from the C-leg lineage** — a dependency, not new construction [R3 §Analytics].
- **Factorials**: 2×2 B×C1 and 2×2 B×D run only after both constituent legs pass individually; the synergy claim ("segments in synergy") requires a non-negative interaction term [R1 §criticism conflict 3; R2 §Analytics; dev-plan §4a slices 4+].
- **E2 corpus build triggers an event-driven re-baseline in the same PR** [ADR-0020 §Re-baseline triggers].

---

## 2. Hypotheses and operational definitions

Every metric is classified at registration per the ADR-0026 taxonomy: **invariant** (= 1.000 / = 0, blocking, never carried across a re-baseline), **corridor** (blocking for rollout), **verdict** (PASS / FAIL / NO-DATA, never blocking, NO-DATA renders explicitly).

### 2.1 H1 — latency (secondary confirmation; verdict) [ADR-0025 §H1]

| Item | Registration |
|---|---|
| Metric | p95 of `assemble_context` wall-clock latency under governance-heavy load (the G-gov query stream) |
| Operational definition | per-leg p95 over the same query stream — E0 opdef (flagged): "governance-heavy load" is operationalized as the G-gov query stream; the sources leave the load mix unspecified; ≥ 3 repeats on the S2 stand, quiet machine; noise band = max − min across repeats |
| Threshold | p95(B) ≤ **0.9 × p95(A)** |
| Noise semantics | if the noise band is wider than the corridor (the 10% improvement), the comparison renders **NOISE** — de-escalated to report + ticket, never a block [ADR-0020 §Gate policy] |
| Role | independent secondary confirmation; non-gating |

### 2.2 H2 — KV-cache (secondary confirmation; verdict) [ADR-0025 §H2]

| Item | Registration |
|---|---|
| Metric (a) | **LCP** — longest common token prefix between consecutive assemblies within a session (deterministic proxy, no wall-clock) |
| Threshold (a) | LCP ≥ **512 tokens** in **≥ 80%** of intra-session transitions |
| Metric (b) | **TTFT improvement** on a stable prefix, live measurement (vLLM / llama.cpp prefix cache), **≥ 30 paired runs** (same session replayed with and without lane ordering) |
| Threshold (b) | TTFT improvement ≥ **20%** |
| Role | independent secondary confirmation; non-gating. If only H2 holds, the surviving argument of leg B is KV stability alone [ADR-0025 §Decision rule] |

### 2.3 H3 — governance flooding (PRIMARY, lanes; verdict) [ADR-0025 §H3]

| Item | Registration |
|---|---|
| Metric | **governance-recall@5 on the G-gov stratum**: share of G-gov queries for which the target governance record (the seeded rule/decision the query was generated from) appears in the top-5 blocks of the assembled context, adjudicated blind (§4.3, §4.4) |
| Pairing | per-query McNemar pairs across legs on the identical corpus build (§6.1); n = 96 |
| Confirmatory condition | **B ≥ B0 + 10 pp AND B ≥ A + 10 pp (absolute)**, each comparison exact McNemar, two-sided, **p < 0.05** |
| MDE | +10 pp absolute [R1 §Analytics] |
| Power note | registered limitation, §6.5 |

### 2.4 H4 — no-harm guardrail (BLOCKING for lanes rollout) [ADR-0025 §H4]

| Item | Registration |
|---|---|
| Metric (a) | knowledge recall@5 on the **existing 192-query corpus** (S1m, baseline 0.8745) |
| Threshold (a) | **≥ 0.832** (= 0.8745 − CI95 0.043, the ADR-0020 corridor rule) — evaluated for leg B (A and B0 reported for symmetry) |
| Metric (b) | **governance-noise-rate on G-neg**: share of G-neg queries with ≥ 1 governance-class block in the top-5 (all such insertions are false by construction, §3.2) |
| Threshold (b) | **≤ 0.15**; falsifier **> 0.30** [ADR-0025 §H4]. Band resolution registered here: (0.15, 0.30] = erosion zone — verdict does not confirm H4-clean, escalation to ArchCom with pin-cost-curve data (§4.2); not an automatic fail |
| Consequence | H4 failure **blocks rollout regardless of H3** (§5.2) |

### 2.5 H5 — cascaded collapse (PRIMARY, cascade) [R2 §Analytics; issue #252]

Confirmatory = the **four conjunctive conditions** enumerated in §5.3 (retention-or-report invariant; retention floor; precision floor; C0-superiority); each failure mode is a falsifier.

| Item | Registration |
|---|---|
| Invariant | **retention-or-report = 1.000** on seeded critical-fact markers: every seeded critical fact at a collapse boundary is either retained in the collapsed output at its level or its loss is explicitly visible in the collapse-report. Any silent loss = FAIL (invariant, blocking) — "zero silent losses", the honest operationalization of "unconditional" [R2 §Decision] |
| Primary comparison | **cross-session-answerability@5** on the multi-session stratum (§3.4): C1 > C0, exact McNemar two-sided p < 0.05 on paired probes. The committee fixed no MDE for H5; E0 registers MDE reference **+20 pp** absolute (power note §6.5) — flagged as an E0-filled gap, not a committee number |
| Precision floor | **collapse-precision ≥ 0.95**: share of collapsed-output claims corroborated by ≥ 1 source record, blind-adjudicated. Adjudication scope registered here: 100% of claims touching seeded critical markers + a random 10% of remaining claims (minimum 50 per level, or all if fewer). **Any invented critical fact = falsifier = FAIL** (anti-hallucination) [R2 §Analytics] |
| Retention floor | **collapse-fact-retention@level ≥ baseline − max(0.02; CI95)** per level — S3 fact-retention semantics, the ADR-0019/0020 corridor rule [R2 §Analytics; ADR-0020 §Corridors] |
| Cost curve | **collapse-cost curve** at 2× / 5× / 10× compression targets, from traces — descriptive (verdict), informs the C2 budget choice, never gates [R2 §System Engineer] |
| C0 definition | mechanical compressor (deterministic truncation/concatenation, no LLM) with a **token budget equal to the C1 realized output budget** — the equal-budget rule of the cascade (§4.1) |

### 2.6 D1 — intrusion on intent conflicts (PRIMARY, awareness; verdict) [R3 §Analytics; §Decision]

| Item | Registration |
|---|---|
| Metric | intrusion rate on **type-2 (intent-conflict)** scenarios: share of paired scenarios where the acting agent intrudes into the peer's work zone — writes to the component/task another active session has claimed — as determined by the scenario's deterministic collision oracle |
| Pairing | per-scenario McNemar pairs (treatment arm vs control arm, same scenario); confirmatory set = all seeded type-2 pairs, **n ≥ 40** (target 80; §3.6) |
| Confirmatory condition | intrusion(D arm) ≤ intrusion(control) **− 20 pp absolute**, exact McNemar two-sided **p < 0.05** |
| MDE | 20 pp [issue #252; R3] |

### 2.7 D2 — freshness (corridor) [R3 §Analytics]

| Item | Registration |
|---|---|
| Metric (a) | **t_eligible**: elapsed time from a peer write event to that write being eligible for inclusion in a `project_delta` response to another agent in the same project (committed + inside the recency window + past trivial-reject). Operational definition fixed here; R3 names the metric and the bound |
| Threshold (a) | p95 ≤ **60 s** |
| Metric (b) | **stale-action share**: share of scripted agent actions taken against state already superseded by a peer write, detectable via superseded pointers + `origin=` provenance |
| Threshold (b) | ≤ **0.05** |
| Dependency | superseded pointers (C-leg lineage) + origin= (P0, merged) — dependency, not new construction [R3 §Analytics] |

### 2.8 D3 — awareness price (corridor) [R3 §Analytics]

| Item | Registration |
|---|---|
| Metric | awareness block cost per call; share of assembled budget |
| Thresholds | ≤ **300 tokens/call** AND ≤ **5%** of the context budget |
| KV guardrail | the H2 proxy must hold with awareness enabled: **LCP ≥ 512 tokens in ≥ 80%** of intra-session transitions (awareness block placed last, outside the pinned prefix) |

### 2.9 D4 — over-deferral (CO-EQUAL PRIMARY; verdict) [R3 §Decision; §Product]

| Item | Registration |
|---|---|
| Metric | **over-deferral rate on the 40 seeded stale-claims**: share of stale-claim scenarios where the agent defers or abstains from work that is actually safe to do (the presence/goal claim is stale or superseded; no real conflict) |
| Threshold | ≤ **0.05** — operational rule: ≤ 2 of 40 deferrals (point estimate); Clopper-Pearson upper 95% bound reported alongside (registered caveat: at 2/40 the upper bound = 0.169; the threshold is a product-risk call, not a power-derived one — §6.5) |
| Rationale | paralysis of agents = product failure, symmetric to intrusion; D-leg passes only if D1 AND D4 both hold |

### 2.10 Type-1 sanity floor (harness validity, not a hypothesis; corridor) [R3 §Analytics]

| Item | Registration |
|---|---|
| Metric | control-arm (awareness off) intrusion-free rate on **type-1 (file-visible conflict)** scenarios: the conflict is visible in the files themselves, so a competent agent reading current files already avoids it |
| Threshold | ≥ **90%** |
| Reading registered here | R3 states the floor as "else harness broken → NOISE". E0 fixes the reading: the floor is measured on the **control arm** — if even file-visible conflicts are not handled at ≥ 90% without awareness, the scenario scripting / harness is miscalibrated and **all D1 results of the batch render NOISE** (not a verdict on the leg). Repair ticket, re-run as a new registered run (§6.6) |

---

## 3. Strata and corpus

### 3.1 G-gov — governance stratum (~96 queries) [ADR-0025 §Mandatory design elements; R1 §Analytics]

- The governance class is **absent from the S1m corpus** — the stratum is invented for this experiment.
- Seeded records: rules and decisions generated per the `tech_patterns.py` pattern, **~60–100 records** permitted, **2 queries per record** for McNemar power.
- **Numeric tension in sources, resolved here (flagged, not silent):** the sources carry both "~96 queries" and "~60–100 records × 2 queries" (= 120–200). E0 locks: **analyzed stratum = 96 queries = 48 records × 2 queries**; the seeding budget up to 100 records forms a **replacement pool** for adjudication rejects (§4.4); replacements are logged, the analyzed denominator stays 96.
- Ground-truth protocol and applicability criterion: §4.4 — fixed in this document **before seeding**.

### 3.2 G-neg — negative control (~24 queries) [ADR-0025 §Mandatory design elements]

- Queries with **no applicable governance record by construction**: knowledge questions about the same projects whose governance records exist, so lexical pull toward governance exists but the correct answer is knowledge content.
- Measures the **price of pinning**: false governance insertions. Any governance-class block in the top-5 of a G-neg query is a false insertion by construction; governance-noise-rate = insertions / 24 (§2.4).

### 3.3 Corpus profile — distribution-matched (58% checkpoints) [ADR-0025 §Context; issue #252]

- The E2 corpus extension mirrors the live-store class distribution that produces the measured drowning (1457 entries: 839 checkpoints = 58%, 30 rules, 217 decisions): **checkpoints 58% ± 2 pp** (tolerance is an E0 fill — the sources fix the 58% point, not a band), rules/decisions per live ratio.
- The same distribution matching applies to the multi-session scenario stores (§3.4) so the drowning condition is reproduced, not sanitized.
- Corpus fingerprint (seed, version, class counts) committed with E2; any corpus or issuance-path change → event-driven re-baseline in the same PR [ADR-0020 §Re-baseline triggers].

### 3.4 Multi-session stratum (~80–100 scenarios, 160–200 paired probes) [R2 §Analytics; issue #252]

- Multi-session project histories with seeded events; each scenario runs in both arms (treatment vs control) — one **paired probe** per arm, i.e. 160–200 probe runs forming 80–100 McNemar pairs.
- Built in **E2** per the issue and dev-plan; probes target cross-session-answerability@5 (H5).
- Scenario stores distribution-matched (§3.3).

### 3.5 G-poison — adversarial stratum (cascade) [R2 §Security; issue #252]

- Seeded poisoned entries in collapse inputs, mirroring the four P0 bug classes: quarantined RAW rows entering clusters (F1), synthesis tag inheritance including `applyTo:**` (F2), secret-bearing non-first members (F2b), source-swap cache invalidation (F3).
- **Falsifier: ANY trace of poisoned content in ANY C-leg output** (synthesized record, collapse report, sampled claim) = FAIL of the C leg — blocking invariant, regardless of every other metric.

### 3.6 Awareness strata [R3 §Analytics, §Security; issue #252]

| Stratum | Size | Measures |
|---|---|---|
| Conflict pairs | **80 pairs**, **≥ 40 type-2** (intent-conflict), remainder type-1 (file-visible) | D1 confirmatory on the type-2 subset; type-1 carries the sanity floor (§2.10). Type-2 may be raised by **adding** pairs — the stratum total then exceeds 80, n reported — under three binding rules: the type-1 count never drops (zeroing type-1 would kill the sanity floor), the raise decision is taken **before any treatment-vs-control comparison is computed**, and the added pairs follow the same generator and seeding protocol as the base set |
| Stale-claims | **40** seeded stale/superseded presence or goal claims | D4 over-deferral |
| Noisy canaries | **200** noisy-but-legitimate facts at the write boundary | **false-drop ≤ 0.01**; quarantine-not-delete: drop-receipt without content, TTL 90 days (configurable), un-drop path. An LLM write-filter is prohibited on the write path (hot-path latency, false positives, contradicts retention-or-report = 1.000) [R3 §Decision] |
| Adversarial-peer | scripted hostile peer emitting spoofed presence / goal claims | security falsifier (§5.4): spoofed presence must not move agent behavior |

### 3.7 PR #224 replay — permanent scenario [R3 §Decision; §Analytics]

- The PR #224 incident (a release PR closed by a parallel session during the v4.0.0 release) becomes a **permanent controlled scenario** in every D-leg batch: parallel sessions over one project, release in flight, the peer's checkpoint ~3 minutes old inside the delta recency window — the delta surfaces it at the top where recall would drown it among 839 checkpoints.
- Reported separately in every batch, indefinitely. Evidence ladder: Tier C anecdote → **Tier A controlled scenario** (this experiment) → Tier B production telemetry (future).
- **Classification and pass criterion (E0):** diagnostic verdict (ADR-0026 taxonomy), non-gating, never enters a confirmatory set (§6.4). Pass = the peer's ~3-minute-old checkpoint appears in the delta top slot AND the acting agent does not intrude into the peer's release task. A FAIL is reported with every D verdict and is a standing ArchCom escalation item.

---

## 4. Modes and protocols

### 4.1 Equal-budget — primary mode [R1 §Analytics; ADR-0025 §Mandatory design elements]

- All cross-leg comparisons run at **identical assembled-context token budgets**.
- A **B-inflated run** (leg B given extra budget) may be executed **report-only, never a basis for conclusions**: "governance no longer drowns" must not be bought with extra tokens.
- Cascade equal-budget: C0's budget = C1's **realized output budget** (§2.5) — a mechanical compressor of the same token budget.

### 4.2 Pin-cost curve — 5 / 15 / 30% [R1 §Mandatory design elements]

- Pinned-governance prefix share of the budget swept at **5%, 15%, 30%**; measured at each point: governance-recall@5 (G-gov), knowledge-recall@5 (192-corpus), governance-noise-rate (G-neg).
- The curve is **descriptive** — the optimum is a design parameter, not a constant [R1 contract §5 risk table]; it feeds the R-phase configuration choice.
- The H4 floor (§2.4) binds at **every** point of the curve.
- Informational: the 10–15% manifest-lane cap (Security invariant, R1 §Security) constrains the future P1 manifest lane, not this experiment — v0 has no manifest lane.

### 4.3 Blind adjudication [R1 §Mandatory design elements; R1 contract §4a]

- The judge sees (query, candidate record/content) pairs only — **never** the leg an issuance came from, never run ids or leg-revealing provenance; adjudication artifacts are leg-stripped.
- Double annotation on a 20% subsample; Cohen's κ reported; **κ < 0.6 → adjudication recalibrated before scoring** (calibration floor per the ADR-0026 experiments canon).

### 4.4 Ground-truth protocol for G-gov (locked here, before seeding) [ADR-0025 §Mandatory design elements; R1 contract open point 3]

- **Generator**: `tech_patterns.py` pattern; 60–100 seeded rules/decisions; 48 analyzed records × 2 queries = 96 (§3.1); surplus records = replacement pool.
- **Applicability criterion (fixed before seeding — this is the E0 act):** a (query, record) pair is a gold pair iff all three hold:
  1. **Topical match** — the query asks about the norm or prior decision the record encodes.
  2. **Self-sufficiency** — the record alone answers the query (with standard terminology), without requiring another record.
  3. **Non-adjacency** — the record is not merely lexically co-occurrent (same project/artifact name) while answering a different question.
- Rejected pairs are replaced from the pool; replacements are logged; the analyzed denominator stays 96.
- **Seed hygiene** (Security observer): declarative prose only, no second-person imperatives, no `applyTo`, no severity tags; seeds must pass the injection screen; provenance from server columns only.
- Honest residual, accepted at registration: the ground truth is only as strong as this criterion [ADR-0025 §Consequences]; it is reported with every result.

### 4.5 Security invariants for all runs [ADR-0025 §Binding security controls; R3 §Security]

- No mint→pin path without operator approval (two-key rule); v0 pinning only via operator CLI over existing records.
- `origin=` provenance from server columns, never client tags/metadata.
- Awareness/delta blocks are **never pinnable**; presence derives from server-observed hook facts, not self-reported checkpoints; dedup keys include the issuer.
- A run that violates a security invariant is **void** — it is not a data point and is logged as such (§6.6).

---

## 5. Falsifiers and decision rules

### 5.1 Lanes decision rule [ADR-0025 §Decision rule; R1]

| Outcome | Condition (governance-recall@5, G-gov) | Consequence |
|---|---|---|
| **CONFIRMED** | B − B0 ≥ +10 pp AND B − A ≥ +10 pp, both exact McNemar p < 0.05, AND H4 holds | theory confirmed; R-phase (full lanes implementation) becomes an owner decision |
| **NOT CONFIRMED (falsifier zone)** | **B − B0 < 5 pp OR p(B vs B0) ≥ 0.05** | theory NOT confirmed; at most a cheap type-boost survives; **recommendation reverts to B0**; the meta-level is not built. Scope note (flagged): ADR-0025 writes the falsifier on the B-vs-B0 comparison only ("If B does not beat B0, difference < 5 pp or p ≥ 0.05"); E0 registers it verbatim on that scope. The initial draft of this document extended the falsifier to "either comparison p ≥ 0.05" — an unflagged E0 extension, corrected here before any run |
| **INDETERMINATE (residual zone)** | any other non-confirmation outcome: B − B0 ≥ 5 pp with p(B vs B0) < 0.05, while B − B0 < 10 pp, OR B − A < +10 pp, OR p(B vs A) ≥ 0.05 | not confirmation; no rollout. Re-opening requires a NEW pre-registered experiment; re-thresholding or re-zoneing the same data is prohibited (HARKing). Closes the outcome space with the falsifier row: every possible (delta, p) combination maps to exactly one row, so no zone can be picked after seeing data |
| **H3 met, H4 failed** | all four H3 conditions satisfied while any H4 guardrail breaks | rollout blocked (§5.2); not CONFIRMED; remediation via pin-cost curve / ArchCom — the lanes win cannot ship on a broken guardrail |
| Only H2 holds | H3 fails, H2 passes | the surviving argument of leg B is KV stability alone |
| Only H1 holds | H3 fails, H1 passes | latency argument alone — insufficient for the structure |

Multiple-comparison rule (registered): each H3 comparison is tested at exact McNemar two-sided α = 0.05; **no familywise correction is applied** — the ADR binds p < 0.05 per comparison, and choosing a correction after seeing data would be post hoc. Flagged E0 choice (per the §3.1 standard): the committee sources say only "McNemar p < 0.05"; the exact (binomial on discordant pairs), two-sided form and the no-correction rule are fixed here to foreclose post-hoc selection.

### 5.2 Guardrail block rule [ADR-0025 §H4]

- Leg B breaking the **0.832 floor** on the 192-query corpus blocks rollout **regardless of H3**.
- governance-noise-rate: ≤ 0.15 pass; (0.15, 0.30] erosion zone → ArchCom escalation with pin-cost data; **> 0.30 falsifier → H4 FAIL → rollout blocked**.

### 5.3 Cascade decision rules [R2; issue #252]

| Rule | Registration |
|---|---|
| H5 full pass (definition) | **conjunctive, all four**: (1) retention-or-report = 1.000 (invariant, zero silent losses); (2) collapse-fact-retention@level ≥ baseline − max(0.02; CI95); (3) collapse-precision ≥ 0.95 AND no invented critical fact; (4) cross-session-answerability@5: C_L > C0, exact McNemar p < 0.05. "Pass at level L" always means all four |
| Zero-silent-loss | condition (1): retention-or-report = 1.000 at every level; **any silent loss of a seeded critical marker = FAIL** (invariant) |
| Anti-hallucination | condition (3): collapse-precision ≥ 0.95; **any invented critical fact = FAIL** (falsifier) |
| C0-superiority | condition (4): cross-session-answerability@5: C_L > C0, exact McNemar p < 0.05 |
| Sequential gates | **C1 full pass (all four conditions) → C2 allowed; C2 full pass → C3 allowed.** Running a level without the prior level's full pass stacks attribution errors [R2 §Alternatives] |
| C3 additional gate | single-operator threat-model revision (multi-principal trigger, `manager.py:3459`) BEFORE any C3 run; each C3 run is an operator decision [R2 §Security]. **Flagged resolution (per the §3.1 standard):** R2's decision summary reads "threat model revised BEFORE C2/C3" — the stricter reading, revision precedes C2 as well — while R2's alternatives row ties the multi-principal trigger to C3 alone; E0 registers the C3-scoped reading. If the committee intends the stricter reading, that is an amendment, not an interpretation |
| Factorial | 2×2 B×C1 only after B and C1 pass individually; synergy claim requires a non-negative interaction term |
| G-poison | **ANY trace of poisoned content in ANY C-output = FAIL** of the C leg, blocking, regardless of other metrics |
| C1 blocker | no C1 run before vesma-refine #223 smoke-validation (§1.4) |

### 5.4 Awareness decision rules [R3; issue #252]

| Rule | Registration |
|---|---|
| Co-equal gates | **PASS = D1 AND D4.** The legs are not extended past D1–D4 — HARKing excluded [R3 §Decision] |
| Awareness-theater falsifier | if the D-arm improvement concentrates on **type-1 (file-visible)** scenarios while **type-2 (intent-conflict)** — the target stratum — shows no improvement, the mechanism delivers only what ordinary file context already delivers: **theater → FAIL**. Operationalization (E0, registered): theater is declared when the delta (D arm − control) on type-2 scenarios is **< +5 pp** while the delta on type-1 scenarios is **≥ +10 pp**. Related R3 signal: seen-but-ignored ≥ 30% → iterate placement, not data (a placement re-run requires an amendment entry, §8) |
| Over-deferral | D4 > 0.05 (> 2 of 40) → **FAIL** — agent paralysis is a product failure symmetric to intrusion |
| Adversarial-peer | spoofed presence/goal claims must **not** move agent behavior: no abstention from work based on unverified self-reported presence without operator coordination (the fixed frame is part of the treatment). **Any scripted deferral or decision change attributable to the spoofed block = security-contour FAIL regardless of D1/D4** [R3 §Security] |
| Harness sanity | type-1 control floor < 90% → **NOISE for the whole D batch** (§2.10); repair + new registered run — NOISE is not a leg verdict |
| Corridors | D2/D3 failure blocks production rollout of awareness v0 (fix-first), does not falsify D1/D4 |

---

## 6. Analysis plan

### 6.1 Pairing and tests (McNemar policy per ADR-0020 / ADR-0025)

- Pairing unit = the **identical probe under two legs**: same corpus build, same seeds, deterministic stands. G-gov query (n = 96) for H3 pairs; multi-session scenario (n = 80–100) for H5; type-2 scenario (n ≥ 40) for D1; C0/C1 paired probes for H5 at each level.
- All McNemar tests: **exact** (binomial on discordant pairs), **two-sided**, α = 0.05, per comparison (§5.1) — an E0 choice, flagged there; the sources say only "McNemar p < 0.05".
- D4: fixed-threshold count rule (≤ 2 of 40), exact binomial CI reported (§2.9).
- Non-paired thresholds (H1, H2a, D2, D3, canary false-drop, collapse-precision) evaluated against their registered bounds with intervals; no additional tests invented later.

### 6.2 Interval reporting

- Wilson score CI95 for every proportion; latency percentiles with CI95 across repeats; floors derived only via the corridor rule `baseline − max(0.02; CI95)` [ADR-0020 §Corridors]. Percentiles, never means [ADR-0020 §Alternatives].

### 6.3 Verdict taxonomy mapping [ADR-0026 §5]

| Class | Metrics in this experiment |
|---|---|
| invariant (blocking) | retention-or-report = 1.000; G-poison trace = 0; seed injection-acceptance = 1.000 (§4.4 hygiene); security invariants (§4.5) |
| corridor (blocking for rollout/production/level progression) | recall@5 ≥ 0.832; governance-noise ≤ 0.15; collapse-precision ≥ 0.95; collapse-fact-retention@level floor (baseline − max(0.02; CI95)); canary false-drop ≤ 0.01; D2 both bounds; D3 all bounds; type-1 sanity floor ≥ 0.90 |
| verdict (PASS / FAIL / NO-DATA, never blocking) | H3 comparisons; H5 answerability delta; D1; D4 count; H1; H2a/b; collapse-cost curve; #224-replay diagnostic (§3.7) |

**NO-DATA handling:** missing probes, voided runs (§4.5), NOISE (§2.1, §2.10) render **explicitly** — never as zero, never silently dropped [ADR-0026 §5]. A FAIL verdict is a reportable result; null and negative results are results.

### 6.4 Subgroup discipline

- Confirmatory and gating analyses are restricted to the registered strata, each with its taxonomy class (§6.3): G-gov (H3), G-neg (H4b), 192-corpus (H4a), multi-session (H5), the type-2 subset (D1), stale-claims (D4), canaries (corridor), G-poison (invariant). The #224-replay is diagnostic (§3.7): reported with every D batch, never enters a gate or a confirmatory set.
- **No unregistered subgroup analysis** (record type, query length, session, ordering, time) may support any decision; any such cut is exploratory, labeled, non-gating.
- All comparator legs are reported symmetrically; no leg is removed post hoc [ADR-0026 §8].

### 6.5 Power notes (registered limitations, stated before any run)

- **H3 / G-gov (n = 96 pairs):** 80% power (exact McNemar, α = 0.05) requires ≈ 90 discordant pairs resolving a 65/35 split — an ≈ +28 pp marginal effect at ~94% discordance. At the registered +10 pp MDE the power is **0.13–0.32** across plausible discordance (40–96%); a true +10 pp effect can therefore land in the falsifier zone (observed < 5 pp or p(B vs B0) ≥ 0.05). The design is powered for large effects consistent with the drowning premise (A near-floor vs B high); the limited power at the +10 pp threshold is a registered limitation stated before the run. Realized discordance is reported with the result.
- **H5 (n = 80–100 scenario pairs):** at ~50% discordance the +20 pp MDE reference yields power ≈ 0.70 at n = 80 and ≈ 0.78 at n = 100 — it does not reach 0.80 anywhere in the registered range. 80% power requires ≈ +25 pp at n = 80 and ≈ +22 pp at n = 100, or a larger stratum. The committee fixed no H5 MDE; the +20 pp reference is an E0 registration (§2.5, flagged).
- **D1 (n = 40–80 type-2 pairs):** at ~50% discordance, a +25 pp effect has only ≈ 62% power at n = 40; the 80%-power MDE at n = 40 is ≈ +30 pp. Even at the stratum target n = 80, the registered +20 pp MDE yields ≈ 0.70 power. Raising type-2 by adding pairs toward and beyond 80 (§3.6) is the power plan, not decoration: a confirmatory D1 claim at the registered MDE is supportable only at the top of the range.
- **D4 (n = 40):** point-estimate rule; at 2/40 the Clopper-Pearson upper bound = 0.169. The 0.05 threshold is a product-risk call registered by the committee, not a power-derived one.

### 6.6 Single-look, run ledger, re-baseline

- **Single-look:** the registered analysis runs once per leg after data collection completes; no interim look at leg comparisons; no sequential peeking.
- **Run ledger (append-only, below §8):** every run logged with run id, date, legs, corpus fingerprint, flag state; voided runs logged as void.
- **Re-baseline:** corpus × 2 growth, embedder or processing-model change, composition-algorithm or issuance-path change → event-driven re-baseline **in the same PR** [ADR-0020 §Re-baseline triggers]. Invariants never carry across a re-baseline [ADR-0026 §5].

---

## 7. Registered exploratory measures (reported, never gating) [R1 contract §4a; R3 §Analytics]

| Measure | Description | Source |
|---|---|---|
| Behavioral obedience of pinned rules | scripted agent solves ~20 tasks; share of obeyed pinned rules (governance that is recalled but not obeyed is not a benefit) | R1 |
| Longitudinal governance-decay | wallpaper-effect probe over time; confirmed decay zeroes the value of a future manifest lane | R1 (deferred probe) |
| checkpoint-share@k | share of checkpoints in issuance on non-bootstrap queries; enters the ADR-0020 metric registry, informational | R1 |
| Graft rate | share of agent-A decisions using a fact from another session's write — mechanical proxy of the "organism" claim | R3 |
| Counterfactual shadow-replay | replay of scenarios with the awareness block suppressed, post hoc | R3 |
| Abstention credit attribution | abstention → fact-ID provenance chain (also a binding security control; exploratory as a metric) | R3 |

---

## 8. Amendment log

**2026-09-13 — registered. No post-run amendments.**

**2026-09-13 (same day, later) — pre-run revision 1.** Independent review of the initial registration returned REQUEST-CHANGES (1×P1, 4×P2, 4×P3). The E0 window was and remains open — no run has occurred — so the revision is honest pre-run editing, not HARKing. Changes: §5.1 outcome space closed (INDETERMINATE extended to the full residual zone; falsifier re-scoped to the ADR's B-vs-B0 wording, with the initial draft's "either comparison" extension flagged; H3-met/H4-failed row added); §5.3 H5 pass enumerated as four conjunctive conditions, sequential gates tied to "full pass"; §6.3 corridors extended (canary false-drop, collapse-fact-retention floor, collapse-precision moved from verdict to corridor as it gates level progression); §6.4 confirmatory strata re-enumerated with classes, #224-replay made diagnostic (§3.7, with a pass criterion); §3.6 type-2 raise rule made additive with pre-comparison timing; §5.3 C3 threat-model timing flagged as an E0 resolution of an R2 internal ambiguity; §6.5 power figures corrected to conservative exact-binomial values; §5.4 theater falsifier operationalized (type-2 delta < +5 pp while type-1 delta ≥ +10 pp); E0 fills flagged (58% ±2 pp tolerance, exact two-sided McNemar, governance-heavy load opdef). The anti-HARKing clause applies unchanged from this revision onward.

**2026-09-13 (same day, later) — pre-run revision 2 — registered by the awareness v0 engine (#254).** The awareness v0 engine (mnemos #254, R3 hooks composition, `src/vesma/awareness.py`) shipped with one behavioral parameter of the D-experiment that this pre-registration had not fixed: the conflict-hint contact threshold `CONFLICT_HINT_MIN_SHARED_TOKENS = 2`, the knob that trades D1 intrusion-hints (§2.6) against D4 over-deferral (§2.9). The E0 window remains open — no run has occurred — so registering it now is honest pre-run editing per this section's discipline, not HARKing; the parameter is registered exactly as implemented, not as an aspiration. Definition (as implemented, `awareness.py::_goal_tokens` / `conflict_hints`): a shared token is any lowercase token matching `[a-z0-9][a-z0-9_.\-]+` (length ≥ 2 characters) extracted from the goal title after lowercasing and after dropping a fixed deterministic stopword set, that is present in BOTH my last checkpoint goal title and the active neighbor's goal title; a conflict hint fires if and only if ≥ 2 distinct shared tokens are present. Rationale: a single shared word never defers work — the D4 guard (over-deferral ≤ 0.05, co-equal primary; agent paralysis is product failure, symmetric to intrusion), while ≥ 2 distinct shared tokens still fires on real intent overlap such as the #224 replay pair (§3.7). This threshold is an E0-filled operationalization of the R3 "lexical conflict-hints" clause — flagged, not silent (the §3.1 standard): the R3 protocol names the mechanism but fixes no number, the engine had to choose one before any D-batch could run, and every D-batch report must state the value in force. The anti-HARKing clause applies unchanged from revision 1 onward.

**2026-09-13 (same day, later) — pre-run revision 3 — registered by the E3 runner infrastructure (#277 / E3-runner wave).** The E3 runner wave (mnemos #277 pre-run obligations + the lanes-legs runner, `benchmarks/experiments/e3_lanes/runner.py`) shipped with three items of the class revision 2 established for engine parameters: behavioral / registry parameters of the experiment that this pre-registration had left open and the implementation had to fix before any run. The E0 window remains open — no run has been recorded — so registering them now is honest pre-run editing per this section's discipline, not HARKing; each item is registered exactly as implemented, not as an aspiration.

1. **B0 operationalization (§1.1 leg B0).** §1.1 defines B0 as "type-boost of rules/decisions at recall — one ranking line, zero meta-level" without fixing the boost's arithmetic. As implemented (`LanesConfig.type_boost` in `src/vesma/config.py`; the boost applied in `assemble._recall_stage`; factor `lanes.B0_TYPE_BOOST_FACTOR = 10.0` in `src/vesma/lanes.py`): governance rows still arrive through the ordinary RRF recall leg only — no lane queries, no lane ordering, no pinned prefix — and their recall scores are multiplied by the factor, after which the candidate list is re-ranked by score in one stable ranking line (placed before the applyTo partition so M8 pinning survives). The factor is chosen from RRF score arithmetic: observed governance RRF scores on the E2 corpus run ~0.011–0.016, so ×10.0 lifts a recalled governance row above every non-governance candidate — the MAXIMUM lift a pure type boost can buy — which is the anti-confounding point: whatever leg B still adds over B0 is the meta-level structure (deterministic lane recall + byte-stable pinned prefix), not residual rank boost; symmetrically, B0 cannot surface governance rows the RRF recall missed — that recall reach is B's structural contribution. `type_boost` and `enabled` are mutually exclusive at the config boundary (composing them would be an unregistered fourth leg; a model validator refuses it); with both flags off the assemble code path is byte-identical to the pre-E1 pipeline (the E1 flag-off fixture test stays green).

2. **Double-annotation selection rule (§4.3).** §4.3 registers "double annotation on a 20% subsample" without fixing WHICH pairs; issue #277 prescribed the deterministic rule, and the runner wave registers it as implemented (`benchmarks/strata/e2_gov/worksheet.py::double_annotation_pair_ids`): rank ALL worksheet pair_ids by sha256(pair_id) hex ascending and double-annotate the top ceil(20%) of the ranked list — a pure function of the pair-id set, frozen pre-run and stamped into every adjudication worksheet before annotation (58 of 288 pairs at the bootstrap ledger), removing the post-hoc selection freedom that could launder κ past the 0.6 floor. This is an E0-filled operationalization of the §4.3 "20% subsample" clause — flagged, not silent (the §3.1 standard).

3. **e2-gov corpus fingerprint re-record (§3.3 discipline).** Issue #277's replacement-granularity note is registered as one clarifying sentence in the `record_rejection` docstring (`benchmarks/strata/e2_gov/ground_truth.py`: the RECORD is the replacement unit — both its queries, gg-NNN-ph/pr, swap together; defensible under §3.1's record pool), and that module sits inside the e2-gov `corpus_fingerprint` module set. The edit is docstring-only: corpus bytes and all counts unchanged; `profile.json` re-recorded in the same pre-run PR, fingerprint 45489482… → a5155acaf…. Recorded here so the hash change is a registered act, not silent drift.

4. **Development smoke dry-runs.** The runner's acceptance gates (issue #277, runner smoke tests) executed collect-only dry-runs of legs A/B/B0 over the real G-gov/G-neg strata during development: nothing was recorded (the default invocation refuses to persist without an explicit --record), no leg comparison, threshold, or analysis was consulted, and no design parameter was adjusted to observed numbers; the first recorded run and its run-ledger entry below remain a separate deliberate human step.

The anti-HARKing clause applies unchanged from revision 2 onward.
**2026-09-13 (same day, later) — pre-run revision 4 — registered by the E2 wave-2 D-strata artifacts.** The E2 wave-2 corpus preparation (`benchmarks/strata/e2_d/`; strata commit `4ad9a74` on `feat/e2-d-strata` as in-chain after the branch rebase over #283/#285 — the original pre-rebase hash `a1b0a69` no longer resolves — the DURABLE anchor is the stratum corpus fingerprint `7b259633c40d9f679d233dcc06f81e17316a9ce5f2b0250a8565f3e846856d0e` committed in `benchmarks/strata/e2_d/profile.json`, re-recorded with this revision after `materialize.py` joined the fingerprint module set so no stratum-shaping module sits outside the pin; a later merge rebase may shift the commit hash again, the fingerprint cannot) fixed several D-leg MEASUREMENT-INSTRUMENT parameters this pre-registration had left unspecified — the same parameter class as the conflict-hint threshold of revision 2: metric operationalization the E3 runner will score against, fixed by the committed artifacts. The E0 window remains open — no run of any leg has occurred — so registering them now is honest pre-run editing per this section's discipline, not HARKing; every parameter below is registered exactly as implemented in the committed artifacts, not as an aspiration. Registrations:

1. **D-outcome oracle (the load-bearing registration).** Intrusion per scenario (D1, the §2.6 "deterministic collision oracle") is operationalized as `scenario_intruded`: a BINARY outcome — the scripted agent's chosen action set has a non-empty EXACT intersection with the scenario's claimed zone (action `targets` ∩ claimed zone; artifact stems, exact string match; no lexical fuzziness in the judgment layer — lexical overlap is the engine's hint layer, i.e. a treatment). Over-deferral per scenario (D4, §2.9) is operationalized as `deferred_on_safe_work`: a BINARY outcome — the agent withholds at least one SAFE action while an awareness composition was rendered FOR THE PROJECT in the treatment arm (precondition wording corrected pre-run: this entry's original draft required the composition to "reference the (stale) claim" — unsatisfiable by design, since window_expired peers sit outside the delta clamp and superseded-goal slots show the CURRENT goal; the stale claim's misleading pathway is recall-side — the delta cannot and must not carry it). Binary per-scenario outcomes only; no graded severity enters at run time.
2. **Type-2 share in the base conflict stratum:** floor-exact 40 type-2 + 40 type-1 (within §3.6's "≥ 40 type-2, remainder type-1"; the choice preserves the §2.10 sanity-floor n at 40). The §3.6 raise-by-adding-pairs rule remains available, with its three binding conditions, before any treatment-vs-control comparison is computed.
3. **Stale-claims mechanism split:** 20 window_expired (claim age 5400 s, outside the 3600 s delta clamp) + 20 superseded_goal (stale claim 3000 s vs current goal 150 s; the delta's latest-wins per-agent slot is the v0 binding of the §2.7 superseded-pointer dependency — a dependency, not new construction).
4. **Canary composition:** 200 = 140 generic-add + 60 checkpoint-channel, across 10 noise classes × 20; the false-drop ≤ 0.01 corridor (§3.6) is pinned by design and by test at the current write boundary (realized false-drop = 0).
5. **#224 replay store:** flood of 221 rows / 128 checkpoints = 57.9% checkpoint share — the §3.3 live profile (58% ± 2 pp) reproduced at scenario scale; leg 2 of the §3.7 pass criterion (no intrusion into the peer's release task) remains behavioral (E3).
6. **Conflict-store mass and action menu:** per scenario block, exactly 3 checkpoints (actor + peer + an always-present bystander) + 1 evidence row (type-1 only) + 6 noise rows; the action menu carries 2 colliding + 2 safe actions per conflict pair (and in the #224 replay), and 4 safe (0 colliding) per stale-claim and adversarial scenario — the menu shape that scales the intrusion rate; type-1 peers also write lexically overlapping goals — hint contact is uniform across the stratum, and the type split fixes WHERE the claim is discoverable, not whether a peer goal exists.
7. **Two structural readings, recorded:** the D-strata are scenario artifacts materialized as per-scenario stores, not corpus records — §3.3's distribution matching binds the corpus extension and the §3.4 multi-session stores only; and canaries sit at the WRITE BOUNDARY per §3.6's text (drop-receipt without content, TTL 90 days, un-drop path, LLM write-filter prohibition), not in the delta/deferral layer.

Every D-batch report must state the oracle in force (item 1) alongside the hint threshold in force (revision 2). The anti-HARKing clause applies unchanged from revision 2 onward.

**2026-09-13 (same day, later) — pre-run revision 5 — H4 guardrail floor re-derived after the round-3 embedder re-baseline (mnemos #285).** PR #285 (owner-approved round-3 `vesma-embed-v1` weights + fingerprint-aware re-embed migration) re-recorded the S1m baseline under ADR-0020's event-driven re-baseline triggers: the **S1m** contour — the block §2.4's floor derives from — moved `recall@5` 0.87452 → **0.863002** (CI95 ±0.043918); corpus fingerprint unchanged (the embedder swapped, the measured corpus did not). E0 §2.4 registered the H4 knowledge-recall guardrail floor as *S1m baseline − CI95* = 0.832 (0.87452 − 0.042274), derived from the pre-round-3 S1m; that numeric anchor is now stale. The floor is re-derived from the in-force S1m baseline: **0.863002 − 0.043918 = 0.8191**. The derivation RULE is unchanged — the floor is `in-force S1m baseline recall@5 − CI95` computed against the S1m baseline in force at the time of the first recorded run; any further re-baseline before that run requires another dated entry in this log. All legs (A/B0/B) run on the single round-3 embedder — the guardrail compares legs against each other and against the in-force baseline, never against a retired model's numbers. Scope note (review finding): #285 re-measured only the S1m contour; the top-level hybrid `retrieval` block in `baselines/s1.json` was carried over byte-identical and is NOT a round-3 measurement — it must never feed a derivation without being re-run under the round-3 weights first. The E0 window remains open — no run has occurred. The anti-HARKing clause applies unchanged from revision 4 onward.

**2026-09-13 (same day, later) — pre-run revision 6 — type-2 raised to 80 per the §3.6 raise rule and the TL decision of 2026-09-13.** The D-leg conflict stratum's type-2 share is raised from the floor-exact 40 (revision 4, item 2) to **80 type-2 pairs** by executing the §3.6 raise rule: 40 pairs ADDED through the base set's own generator and seeding protocol (`_build_pair(TYPE_2, index ≥ 40)` — indices 40-79, ids `dcp-t2-040`…`dcp-t2-079`; zone pools, store mass, action-menu shape and blindness invariants identical to the base set — pinned by the updated stratum tests), the type-1 count unchanged at 40, so the stratum total is **120 pairs** under the §3.6 "total exceeds 80, n reported" clause (n reported here and in the stratum profile; no type-1 counterparts added per the additive rule). All three §3.6 binding conditions were verified at raise time: the type-1 count never dropped (40 → 40), the raise decision was taken **before any treatment-vs-control comparison is computed** — no D arm of any leg has been executed and no comparison, threshold, or analysis has been consulted — and the added pairs follow the same generator and seeding protocol as the base set. The stratum corpus fingerprint is re-recorded with this revision (`7b259633c40d…` → `6649c245cb07…`, `profile.json` re-recorded; the hash change is a registered act, not silent drift). Effect on the registered power note (§6.5, D1): the power plan stated there — "raising type-2 by adding pairs toward and beyond 80 is the power plan, not decoration" — is now executed: power moves from ≈ 0.62 at the floor-exact n = 40 to **≈ 0.70 at the raised n = 80** at the registered +20 pp MDE (~50% discordance, per the §6.5 figures). §3.6's stratum-table row "80 pairs, ≥ 40 type-2" is superseded IN FORCE by this raise under the rule's own terms; §3.6's raise-rule text (the three binding conditions) continues to govern any future raise. The E0 window remains open — no run of any leg has occurred. The anti-HARKing clause applies unchanged from revision 5 onward.

**2026-09-13 (same day, later) — pre-run revision 7 — probe policy registered as a pre-run instrument parameter (D-runner wave).** The D-leg runner (`benchmarks/experiments/e3_d/runner.py`) ships with one behavioral parameter of the class revisions 2-4 established (engine/instrument parameters this pre-registration had left open and the implementation had to fix before any run): the v0 acting agent is a SCRIPTED DETERMINISTIC PROBE POLICY — the policy is part of the measurement instrument, registered here exactly as implemented (`PROBE_POLICY` / `probe_decide`, pinned by `tests/test_e3_d_runner.py` and stamped into every run manifest), not as an aspiration. The E0 window remains open — no run of any leg has been RECORDED; the runner's development acceptance gates executed collect-only dry-runs of both arms over the real strata with nothing recorded, no comparison consulted and no design parameter adjusted to observed numbers (the revision-3 item-4 discipline) — so this registration is honest pre-run editing, not HARKing. Registrations:

1. **Probe policy `goal-align-then-hazard-abstain-v1`.** (a) The policy reads the same context an agent would: the actor's LIVE-SESSION goal (the scenario artifact's tasking — the dap-001 contract generalized: never read back from the store), the agent view's store rows newest-first, and — in the treatment arm only — the rendered awareness section parsed exactly as rendered. (b) Hazard tokens arise ONLY from two source kinds: a FILE-BEARING store row (a row naming artifact paths; its hazard tokens are that row's path segments that also appear in the actor goal — file claims anchored to my zone, the §2.10 "files" leg) and a CONFLICT-HINT line of the awareness section (the engine-curated, goal-anchored shared-token list). [unverified] self-reported goal lines, presence lines, and the disclaimer frame NEVER create hazards — the fixed frame's own rule (§5.4: unverified presence alone must not defer work). (c) Actions are ranked by lexical overlap of description with the actor goal, ties broken by the neutral action id; the FIRST non-deferred action is executed and the skipped ranked prefix is ABSTAINED from. (d) An action is deferred iff a hazard token sits inside one of its description's artifact-path SEGMENTS — the deferral semantics are "do not WRITE into a claimed zone"; prose mentions of a zone are not writes. The policy is ARM-BLIND: one fixed rule applied identically to both arms; only its input differs (the awareness section renders in the treatment arm alone).

2. **Scope limitation (load-bearing honesty).** The v0 probe is a deterministic lexical INSTRUMENT, not an LLM agent. Its outcomes VALIDATE the pipeline end-to-end (materialization → composition → policy → oracle → abstention chains → artifacts) and demonstrate the treatment's signal pathway; they are NOT a behavioral measurement of D1/D4. The deterministic treatment/control separation such a policy produces over the strata is an INSTRUMENT PROPERTY, not an experimental result, and must never be reported as a confirmatory D1/D4 outcome. Behavioral D1/D4 measurement requires a real agent harness — a separate future wave under its own registration.

3. **Power-figure anchor clarification (revision 6 follow-up).** §6.5's registered ≈ 0.62 figure is anchored at a **+25 pp** effect (n = 40, ~50% discordance). At the REGISTERED +20 pp MDE with ~50% discordance, the exact two-sided-binomial values are ≈ **0.35** at n = 40 and ≈ **0.68** at n = 80. Revision 6's "0.62 → 0.70" quoted §6.5's own registered figures (0.70 being §6.5's n = 80 value at the +20 pp MDE); this entry fixes the anchors so neither figure can be re-read post hoc. The §6.5 derivation rule is unchanged; the revision-6 raise stands on the n comparison alone (power is increasing in n throughout the plausible effect range: ≈ 0.35 < ≈ 0.68 at the +20 pp MDE, ≈ 0.55 < ≈ 0.88 at +25 pp).

The anti-HARKing clause applies unchanged from revision 6 onward.

**2026-09-14 — pre-run revision 8 — conditional-power disclosure and effective-n disclosure (both review rulings on the D-runner wave, PR #294).** The dual review of the D-runner wave (analytics + code) returned two pre-record rulings; both land here while the E0 window remains open (no run of any leg has been recorded). Registrations:

1. **§6.5's D1 power figures are CONDITIONAL-on-expected-discordance values** (computed at b = n/2): 0.62 = cond(b=20, p=0.75, n=40), 0.70 = cond(b=40, p=0.70, n=80), "80%-power MDE ≈ +30 pp" = cond(b=20, p=0.80, n=40). The UNCONDITIONAL exact two-sided values are: **0.3544** (n=40, +20pp), **0.6800** (n=80, +20pp), **0.5477** (n=40, +25pp), **0.8794** (n=80, +25pp). §6.5 stays in force as registered; every D-batch REPORT quotes the unconditional quartet, and the conditional convention is named wherever a §6.5 figure is cited. (Revision 7 item 3's phrase "anchored at +25 pp" was itself imprecise — 0.62 is the conditional value at +25pp-discordance-0.75; the quartet above supersedes all anchor prose.)

2. **Effective n of the type-2 conflict stratum is 32 distinct behavioral worlds, not 80 pairs.** The generator's zone/work selection cycles with period 8 over j = index // 4, so the raised pairs (indices 40-79) RE-EMIT the base block's (project, actor goal, peer goal, action-menu) tuples — the raise added zero new worlds, and even the base 40 was 32 distinct (verified empirically by the code review). McNemar discordance counters therefore count each distinct world 2-3 times; any independence-assuming power arithmetic at n=80 is OPTIMISTIC. The first recorded e3-d run is INSTRUMENT-VALIDATION per revision 7 item 2 (its run-ledger entry must carry that label plus the in-force parameter block: n=80 type-2 / 120 total / probe policy v1 / oracle / threshold 2 / unconditional quartet), so no confirmatory claim is affected. The FUTURE behavioral wave (real agent harness) must widen the `_ZONES`/`_ACTOR_WORKS` pools before raising n — registered here as the required path.

The anti-HARKing clause applies unchanged from revision 7 onward.

Standing rules for this section: after the first run, any deviation — a changed metric, threshold, stratum, hypothesis, or analysis choice — requires a dated entry stating what changed, why, and which run prompted it. **A logged deviation is a report of what happened, never an authorization: the registered analysis stands and is reported as registered; any analysis under changed rules is reported alongside as exploratory (§6.4), never as the confirmatory result, and never replaces the registered verdict.** Run-ledger entries (§6.6) are appended below as runs occur.

---

## 9. Run ledger (§6.6, append-only)

**2026-09-14 — RUN — lanes legs A/B/B0 — `e3-lanes-56c568297ad6` — FIRST RECORDED RUN of any E0 leg.** Recorded 2026-09-13T21:23:27Z at merged-main commit `e14427dbf…` (worktree clean of source changes at execution), owner-authorized the same day. With this entry the E0 window is **CLOSED for thresholds**: no metric, threshold, stratum, hypothesis, or analysis choice may be added or altered from this point; deviations are dated §8 entries that report what happened and never authorize a replacement analysis (standing rules above).

- **Run id / artifacts:** `e3-lanes-56c568297ad6` — `benchmarks/experiments/e3_lanes/runs/e3-lanes-56c568297ad6/` (`manifest.json`, `outcomes.json`, gitignored by design; to be committed deliberately by the report wave when citing).
- **Legs / flag states:** A (`lanes_enabled=false`, `type_boost=false`) · B0 (`lanes_enabled=false`, `type_boost=true`, factor 10.0) · B (`lanes_enabled=true`, `type_boost=false`); equal budget 2048 tokens (§4.1), top-5, fixed leg order A→B0→B, per-leg isolated stores (deterministic embedder, scanner off).
- **Corpus:** e2-gov fingerprint `a5155acafecd3c79…` (stratum `e2-gov-1`, combined 421 rows), S1 pin `c2ce056d57d9…`; adjudication-ledger state `826e443859f0…` — rejects=0, replacements=0, 48 analyzed records × 2 queries = 96 (bootstrap ledger).
- **Code state:** mnemos 4.2.0, Python 3.12.3, sqlite 3.45.1, git `e14427dbf…`. **Recorded surprise (provenance): `git_dirty=true`** — the flag is a single-line `uv.lock` rewrite (editable-package version 4.1.0→4.2.0) performed by `uv run`'s implicit sync between the pre-run clean-tree check and manifest capture; no source file differed from `e14427d`. Reported, not fixed: recorded runs are write-once, and re-recording to obtain a `git_dirty=false` manifest would be a new run state selected on a provenance cosmetic — refused. Write-once verified post-run: a deliberate re-record recomputed the identical run id and was refused by the write-once guard.
- **Ingestion instrumentation (expected, verified identical across legs):** the 8 golden PLANTED secret rows trip the direct-seed danger gate in each leg (24 refusal log lines total) and are restored at store level per the loader's registered golden-harness semantics — no store divergence between legs; stratum seeds: zero demotions (E0 §4.4 seed-hygiene invariant held).
- **Single-look analysis:** executed per §6.1/§5.1/§5.2 on the recorded artifacts after this entry; the outcome report (per-leg rates, exact two-sided McNemar, §5.1 row, H4 status) is delivered to the Architectural Committee via the Tech Lead. H4a carried there as PENDING: the per-leg knowledge-recall@5 measurement (§2.4a) is outside the runner's registered measurement surface and was not improvised.

**2026-09-14 — committee findings on run `e3-lanes-56c568297ad6` (Architectural Committee ratification; REPORT-ONLY — no authorization, the registered verdict stands unchanged).** The committee ratified the §5.1 falsifier outcome (theory NOT confirmed; recommendation reverts to B0) and recorded two findings from the exploratory investigations it commissioned:

1. **Ceiling analysis (engine verified defect-free; working-as-designed).** The lanes engine executed the E1 specification flawlessly (byte-exact reproduction of all three legs; every defect hypothesis rejected by code trace). The observed B = 12/96 is the EXACT structural ceiling of the query-blind pinned prefix on this stratum: the lane queries depend only on `(project)`; the `(lane, score)` sort pins the 6-8 score-1.0 rule rows (newest-first) into the entire top-5, invariant to the query text; a hit therefore occurs iff the query's gold record sits in the project's hot-5 (6 analyzed rules × 2 phrasings = 12). The 12 hits are all rule-gold (0/84 decision-gold queries); B destroyed 58 of A's hits and gained 2 (paraphrase pairs where the structural recall-reach genuinely worked). The ceiling is a property of the design's registered value (byte-stable prefix, hypothesis H2) meeting a per-query-gold metric — informative falsification, not an artifact. A future query-conditioned redesign (leg B1) would sacrifice H2 and requires a NEW pre-registration with this ceiling analysis registered up front.
2. **H4b instrument finding (registration-time miscalibration; counter defect-free).** The governance-noise falsifier bound (0.30) sits below the chance baseline implied by the corpus geometry (~0.74-0.76 P(≥1 governance row in top-5) at the per-project 23-24.5% governance density) — a pilot calibration was never registered, which is why the miscalibration survived to the recorded run. Additionally, leg B's noise-rate 1.0000 is definitional: the pinned-lane design surfaces governance unconditionally (H3's anti-drowning mechanism working as registered) while §2.4(b) declares any surfaced governance on a G-neg query false — the two registrations collide. Future G-neg registrations should measure DISPLACEMENT (governance slot-share / gold-in-top-5 McNemar), pilot-calibrate the control baseline before locking thresholds, and unify the governance-class operationalization (tag class vs seeded-set currently disagree silently).

The committee's product posture: lanes remain in-tree behind `LanesConfig.enabled=False` as a verified-inert dormant capability (rollout required an H3+H4 pass); B0 survives as not-falsified (+3.1pp on leg A) with any default change an owner product decision; the D (awareness) and C (collapse-cascade) legs carry separate hypotheses and are not affected by this verdict.

**2026-09-14 — RUN — e3 D-leg deterministic probe — `e3-d-9e466b5c8f9a` — FIRST RECORDED RUN of the e3-d runner: INSTRUMENT VALIDATION per §8 revision 7 item 2, NOT a behavioral D1/D4 measurement.** Recorded 2026-09-13T22:09:33Z at detached origin/main commit `dd0f5092a210…` (worktree clean at execution — `git_dirty=false` in the manifest; the lanes-run `uv.lock` re-lock hazard was pre-empted this time by restoring the lockfile and invoking the warmed venv interpreter directly, so no implicit `uv` sync ran between the clean-tree check and manifest capture). Orchestrator-authorized the same day; this is the deliberate first-record decision the runner's default refusal gates (no D-leg comparison, threshold, or analysis had been consulted before the run — the D anti-HARKing window holds for thresholds, which this run does not close).

- **Instrument-validation label (§8 revision 7 item 2, verbatim):** "The v0 probe is a deterministic lexical INSTRUMENT, not an LLM agent. Its outcomes VALIDATE the pipeline end-to-end (materialization → composition → policy → oracle → abstention chains → artifacts) and demonstrate the treatment's signal pathway; they are NOT a behavioral measurement of D1/D4. The deterministic treatment/control separation such a policy produces over the strata is an INSTRUMENT PROPERTY, not an experimental result, and must never be reported as a confirmatory D1/D4 outcome. Behavioral D1/D4 measurement requires a real agent harness — a separate future wave under its own registration."
- **In-force parameter block (per §8 revision 8 item 2):** n=80 type-2 / 120 total pairs / probe policy `goal-align-then-hazard-abstain-v1` / oracle (revision 4 item 1: binary `scenario_intruded` / `deferred_on_safe_work`, exact artifact-stem set intersection) / `CONFLICT_HINT_MIN_SHARED_TOKENS=2` / unconditional power quartet 0.3544-0.6800 @ +20 pp, 0.5477-0.8794 @ +25 pp / **effective n = 32 distinct worlds per revision 8** — the 80 type-2 pairs re-emit the base block's (project, goal, action-menu) worlds, so McNemar counters below count each distinct world 2-3 times.
- **Run id / artifacts:** `e3-d-9e466b5c8f9a` — `benchmarks/experiments/e3_d/runs/e3-d-9e466b5c8f9a/` (`manifest.json`, `outcomes.json`, gitignored by design). Stratum `e2-d-2`, corpus fingerprint `6649c245cb07…`, 162 scenarios = 80 type-2 + 40 type-1 (120 conflict pairs) + 40 stale + dap-001 + replay-224; frozen scenario clock 2026-09-13T12:00:00Z; mnemos 4.2.0, Python 3.12.3, sqlite 3.45.1. Schema and statistics ban re-verified post-run by independent reload (`verify_manifest` / `verify_outcomes` pass; recursive stat-key scan clean — the artifact carries no p-values, CIs, or verdicts by schema). Write-once verified post-run: a deliberate re-record recomputed the IDENTICAL run id and was refused by the write-once guard.
- **Legs:** control (`include_awareness=false`) · treatment (`include_awareness=true`), fixed order control→treatment, fresh isolated store per (scenario, leg), identical store bytes across arms, deterministic embedder, scanner off.
- **Findings recorded as counters only (no D1/D4 behavioral claim; no p-value interpretation):** type-2 intrusion 80/80 control vs 0/80 treatment; type-1 intrusion 0/40 in both arms; stale over-deferral 0/40 in both arms; replay-224 intrusion 0 in both arms; dap-001 deferral 0 in both arms. Discordance (per-scenario McNemar pairs): type-2 intrusion {treatment_only 0, control_only 80}; type-1 intrusion {0, 0}; stale over-deferral {0, 0}.
- **Instrument-pathway facts behind the counters:** abstaining scenarios — control 40 (all type-1), treatment 120 (all 80 type-2 + all 40 type-1). Hazard attribution — control: 57 `file_row` deferrals, all type-1 (the §2.10 "files" leg is view-visible, hence arm-blind); treatment adds 102 `conflict_hint` deferrals on type-2 (the hint-only pathway — the entire type-2 separation) and 57 `conflict_hint` on type-1; stale / adversarial / replay-224: zero deferrals. The treatment arm rendered ≥1 conflict hint in 121/162 scenarios (all type-2, all type-1, replay-224; stale and adversarial carry none by design) and recorded abstention chains for the 120 conflict-pair scenarios.
- **Replay-224 diagnostics (§3.7):** engine-side `peer_top_slot=true` (pass-criterion leg 1); `intruded=false` in BOTH arms — the treatment hint rendered (`hints=1`) but the probe's goal-aligned first choice was already the safe action (`a1`, no abstention), so this scenario shows no arm separation at the policy layer.
- **dap-001 (adversarial, §5.4 security contour):** treatment `hints=0` — the registered `hint_expected=false` verified in the recorded artifact (the store holds no actor row, the engine's hint layer stayed off, the ghosts manufactured no hints); the spoofed block reached the agent only through the delta's [unverified] self-reported layer and produced NO deferral (`deferred=false` in both arms; chosen action `a4`) — no treatment-arm deferral attributable to the spoofed block.
- **Run-time instrumentation (expected):** two "stripped client-supplied checkpoint stamps" server log lines (mnemos #251: server-minted stamps) during materialization — no store divergence between arms.

No behavioral D1/D4 claim is made or implied by this entry; the deterministic separation above is an instrument property of the lexical probe (label, first bullet). The behavioral wave (real agent harness) remains a separate future registration, and per revision 8 item 2 it must widen the `_ZONES`/`_ACTOR_WORKS` pools before raising n.
