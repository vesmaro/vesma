# ADR 0037: lineage_marks — engine attachment points (labeling-policy v1.1 §8.3)

**Status:** Proposed — awaiting owner ratification. This ADR is documentation-
only (no code changes); it maps the ratified field schema
[vesma-canon `schemas/lineage_marks.schema.json`](https://github.com/vesmaro/vesma-canon)
(merged 2026-10-05, 6ea87fd; envelope `$ref` wiring in `envelope.schema.json`)
onto the engine (`src/vesmaro/`) and records the three divergences that must
close before the field becomes load-bearing. The SEMANTICS (the mark is an
input to merge arbitration, never the classifier's verdict; folding by mark
carries the marks to the accepting record — zero loss) were ratified with the
field name on 2026-10-05 (labeling-policy v1.1 §8.3,
[vesma-cortex PR #10](https://github.com/vesmaro/vesma-cortex)); what the owner
ratifies here is WHERE the engine writes and reads it.

**Deciders:** owner (ratification gate, pending); authored by the
agent-architect lane from the `lineage-marks-schema` card (vesma-canon board,
W6 follow-up).

**Scope:** attachment points and wiring plan for `metadata.canon` →
`lineage_marks` in the engine. Out of scope: the merge-arbiter
implementation itself (duty №3 of the cortex duty registry v2, start gate
"after B2", Choice-primitive charter gate), corpus work, and any schema text
change — the canon schema is frozen by the W6 merge (TL gate 8/8) and its
semantics edits go only through separate owner-ratified PRs.

## Context

Labeling-policy v1.1 §8.3 (vesma-cortex, ratified with the field name on
2026-10-05) splits the duplicate question into two planes:

- the model's **verdict** (is-dup) stays conservative: «спорное → NOT dup»
  (§1.5, error-cost asymmetry §2) — unchanged;
- the **system action** (folding records that carry a
  `same-message-other-envelope` mark) is consolidation territory:
  merge-arbiter (duty №3, [ADR 0002 cortex](https://github.com/vesmaro/vesma-cortex)
  duty registry) consumes the mark as an ARBITRATION INPUT.

The canon side landed W6: `lineage_marks.schema.json` (array, minItems 1;
per mark: `kind` ∈ {`same-message-other-envelope`, `merged-by-arbiter`,
`split-from`}, `at` ISO-8601 required, `ref` optional provenance pointer —
deliberately store-specific, NOT a canonical link; links live in server
edges) connected into `envelope.schema.json` by `$ref`, with an ETALON
(`examples/after/lineage-marks.json`) and a negative
(`bad-lineage-marks-kind.json`).

The engine side today knows nothing about the field — which is the expected
state at this point (the card marked it: «поле СЕЙЧАС нигде не читается»),
but the canon validator shipped BEFORE the engine validator was aligned, so
a record that honors the canon schema is currently flag-raised (warn mode)
by the engine's own gate. The three divergences are listed in the
Decision; a wiring plan closes them.

## Decision

### D1. Storage locus (no new column)

`lineage_marks` lives inside `metadata.canon` of an engine
`Memory` row (`Memory.metadata["canon"]["lineage_marks"]`), exactly as the
envelope schema places it. No SQLite column, no FTS participation, no index.
Rationale: the mark is envelope data (canon §2 — the envelope never
duplicates record fields or links), it rides the existing canon gate and the
existing JSON `metadata` column serialization
(`SQLiteStore.update_fields` JSON-serialises `metadata` wholesale), and
records without `metadata.canon` remain outside canon scope (canon §9
transitional rule) — a pre-canon row cannot carry marks, and that is correct.

### D2. Write sites (who mints a mark)

The mark is server-minted or arbitration-minted — the SAME trust class as
the checkpoint envelope, NOT client data. A client must not be able to
forge «this record is a dup of another» as an input to arbiter actions;
folding by mark is a destructive-ish consolidation performed by the engine.
Consequently the write surface is:

| Site | File | Mark kind | Trigger |
|---|---|---|---|
| W1 — arbiter fold | future `pipeline/` merge-arbiter leg (no site today) | `merged-by-arbiter` | arbiter folds record B into A by mark (or on its own grounds): ACCEPTING record A gains `{kind: merged-by-arbiter, at: <fold ts>, ref: <B.id>}`; B's rows are consolidated zero-loss per §2b discipline |
| W2 — split | future split op (none in the store today) | `split-from` | when a record is split out of a parent: child gains `{kind: split-from, at: <split ts>, ref: <parent id>}` |
| W3 — other-envelope sighting | near-dup consolidation path, `MemoryManager._mint_relates_to_edges` vicinity (manager.py) and/or the mesh-import path (`mesh_server.WriteMemory` / `SQLiteStore.find_federated_duplicate`) | `same-message-other-envelope` | a second sighting of the same message with a diff confined to §8.1 envelope-whitelist fields (dates/counters/source-of-re-encounter) — the sighting that §8.1 classifies dup-eligible |

Engine surfaces that DO NOT write marks: generic create/update
(`MemoryManager.add`/`update`), CLI/REST/MCP client paths (stripped below,
D5), benchmark/import rows.

### D3. Read sites (who consumes a mark)

The card verdict is correct for TODAY: the field is read nowhere. The
minimal wiring WHEN arbitration lands (merge-arbiter wave, duty №3):

- **R1 — arbitration input (the sole semantic reader).** The merge-arbiter
  decision path (`DecisionProvider.evaluate` over a
  `CanonState(record=…, candidate=…)`) reads marks from both
  `CanonRecordView` projections: a mark on the candidate (or record) raises
  the fold-eligibility INSIDE the arbiter's own Choice logic. This requires
  `CanonRecordView` to carry the marks:
  `from_memory` (decision_provider.py) currently extracts only
  `language`/`record_type` from the envelope — add a
  `lineage_marks` projection there (typed tuple, validated shape). This is
  the exact seam «поле СЕЙЧАС нигде не читается → где читаться ДОЛЖЕН».
- **R2 — telemetry.** A mark-driven fold emits the §3.9 attribution line
  (decision-provider telemetry, `log_decision_telemetry`) plus the fold
  audit event — the mark is evidence of WHY a fold happened, so it must be
  in the audit trail.
- **R3 — corpus/eval.** The calibration/test corpus builders
  (vesma-cortex side) read marks to construct razor-zone pairs; engine-side
  nothing to wire.

### D4. The three divergences (must close before R1 goes live)

| # | Canon says | Engine says сегодня | Closing move |
|---|---|---|---|
| Д1 | `envelope.schema.json` allows the optional `lineage_marks` key in EVERY envelope type | `ENVELOPE_ALLOWED_KEYS` (canon_validate.py) enumerates the four types WITHOUT `lineage_marks` → any record carrying the field produces `CANON-E-ENVELOPE … forbids unknown field 'lineage_marks'` | add `"lineage_marks"` to all four per-type allowed key sets in `ENVELOPE_ALLOWED_KEYS` — mirrors the canon zero-diff principle (records without the field pass unchanged) |
| Д2 | schema: `at` required; `ref` optional | same shape enforced nowhere in code — `canon_validate` has no per-item validator | a small `_lineage_violations(canon)` pass in `canon_validate.py` (shape: non-empty array, `kind` in enum, `at` ISO-8601 with the pattern's shape, `ref` non-empty string when present — `additionalProperties: false` per mark). Failure → `CANON-E-LINEAGE` codes, warn/strict via the existing gate. Reuses the canon gate; no new enforcement machinery |
| Д3 | schema: marks are provenance, links are server edges | engine edge kinds are `supersedes`/`relates_to` ONLY (`_EDGE_KINDS`, SQL CHECK constraint); a fold performed BY MARK wants a durable link to the folded row that is more than `relates_to` affinity | the §8.3 fold writes its link with `provenance='merge-arbiter'` on a `relates_to` edge (the weaker honest claim) — or, if the arbiter wave later ratifies a dedicated kind, `_EDGE_KINDS` expansion is a schema migration + whitelist + manager wrappers in one change (the lockstep rule at `_EDGE_KINDS`). ADR leaves the kind question open (OQ-2) and pins only the provenance minimum |

Д1 is a BLOCKER-quality misalignment (a canon-valid record fails the engine
gate); Д2/Д3 are required before arbitration reads/produces marks.

### D5. Trust class: strip client-supplied marks

A client-supplied `lineage_marks` on a generic create/update must be
STRIPPED like the checkpoint stamps (`CHECKPOINT_STAMP_KEYS` discipline in
`MemoryManager.add`/`update`): only W1–W3 mint marks. Without the strip the
mark becomes an attack vector onto the arbitration input («признай мою
запись непомеченной» / «пометь запись-приёмник моей меткой — консолидация
уничтожит конкурента» — input-forgery into a folding action). The mark is
the arbiter's evidence; evidence the subject can self-author is no evidence.
Concretely: join the strip set in both paths, and re-derive/protect marks
on the accepting row during a §8.3 fold (merge-back pattern of
`INTERNAL_METADATA_KEYS`).

### D6. §8.3 → engine-object map (the routing table)

| §8.3 concept | Canon object | Engine object |
|---|---|---|
| mark nature | `kind` | discriminator consumed by the arbiter Choice (R1); written by W1–W3 |
| event time | `at` | the action's timestamp (fold/split sighting) — engine has no per-mark column; serialized inside the envelope JSON |
| provenance pointer | `ref` | NOT a `memory_edges` row by itself (no canonical link contract, store-specific format); for arbiter folds the durable link is the edge with `provenance='merge-arbiter'` (Д3); for sightings the ref is advisory (e.g. `fed_id` or the sighting's record id) |
| verdict/action split | schema description | `IsDuplicateRequest` verdict (conservative, untouched) vs the arbiter's fold action (duty №3) — two code planes, §8.3's core claim |
| zero-loss carry-over | «folding by mark carries the marks over» | the accepting record's `lineage_marks` array GROWS by (a) the folded twin's own marks, then (b) the `{kind: merged-by-arbiter, at, ref: <folded id>}` entry — append-only, marks never deleted |

## Phases

1. **This PR (doc-only):** this ADR; `make verify` clean; no code touched,
   no suite impact.
2. **Wave A (engine alignment, doc-gated by owner ratification here):**
   Д1 `ENVELOPE_ALLOWED_KEYS` + Д2 `_lineage_violations` + Д5 client strips,
   with targeted `canon_validate` tests — a small engine PR, independent of
   the arbiter.
3. **Wave B (arbiter, duty №3):** merge-arbiter implementation consuming
   R1; carries Д3's provenance minimum and the D6 append-only carry-over;
   remains behind the Choice charter gate and post-B2 sequencing already
   fixed by duty registry v2.

## Consequences

- The engine gate today flags canon-valid lineage-bearing records (Д1);
  until Wave A ships, such records must not be stored with
  `canon_mode=strict` on the create path — in `warn` mode they persist with
  `CANON-E-ENVELOPE` warnings (honest noise, zero-loss neither way).
- Marks ride no index: arbitration's mark lookup is a JSON read on the
  prepared view — bounded by the pairwise scope of the arbiter, not a
  store-wide scan. If a later wave needs mark-presence as a query
  predicate, that is a deliberate schema addition (generated column / FTS
  config) — out of scope here.
- The strip (Д5) means import paths that legitimately recreate lineage
  (migrate/backfill) must mint marks through the trusted path, not raw
  metadata — same discipline as checkpoint stamps.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| First-class `lineage_marks` column on `memories` | envelope data in a column duplicates the canon §2 rule («the envelope never duplicates server-owned fields» — here inverted: the record would duplicate envelope data); no store-wide predicate need today; JSON read is sufficient at arbiter scale |
| Marks as client-authored data (canon §2 class) | forgeable input to a destructive-consolidation action; breaks the verdict/action split's trust boundary (D5) |
| New edge kind `folded_by` / `lineage_of` right now | a schema migration before the arbiter even exists locks a shape chosen without arbitrated evidence; `provenance='merge-arbiter'` on `relates_to` satisfies the provenance minimum (Д3), the dedicated kind stays an OQ-2 open question for the arbiter wave |
| Ignore Д1 until the arbiter wave | canon-valid payloads fail the engine's own gate meanwhile; every canon-rolling record with a mark would carry stale `CANON-E-ENVELOPE` warnings — a silent-degradation surface of the same class the engine always closes explicitly |

## Open questions for ratification

- **OQ-1** — confirm Wave A (Д1/Д2/Д5 engine alignment) may proceed on
  ratification of this ADR without a separate owner gate; recommendation:
  yes, it is validator parity with the already-ratified canon schema.
- **OQ-2** — dedicated edge kind for arbiter folds vs `relates_to` +
  `provenance='merge-arbiter'`; recommendation: decide at the arbiter wave
  with field data, `relates_to` minimum holds meanwhile.

## References

- [vesma-cortex](https://github.com/vesmaro/vesma-cortex)
  `docs/specs/labeling-policy-b2.md` §8.1–8.4 — the ratified rule
  (verdict/action split, whitelist classification, zero-loss).
- [vesma-canon](https://github.com/vesmaro/vesma-canon)
  `schemas/lineage_marks.schema.json` + `schemas/envelope.schema.json`
  (`lineage_marks` property), `examples/after/lineage-marks.json`,
  `examples/negative/bad-lineage-marks-kind.json` — W6, merged 2026-10-05.
- Canon board (vesma-canon `docs/status.md`) W6 row — the schema landing
  record; this ADR closes the engine-side half of the same card's follow-up.
- [ADR-0019](0019-optimistic-publication-async-refinement.md) — zero-loss
  swap/§2b discipline the carry-over must respect.
- [ADR-0004](0004-decision-provider.md) — the decision-provider contract
  (CanonState/CanonRecordView) the R1 read sites ride.
- [ADR-0027](0027-multi-context-memory.md) — the metadata-convention
  precedent (all-or-nothing validation at the boundary) the Д2 check
  mirrors; [ADR-0030](0030-memory-graph-self-fueling.md) — the edge-kind
  lockstep rule Д3 references.
- Engine sites cited: `src/vesmaro/canon_validate.py`
  (`ENVELOPE_ALLOWED_KEYS`, the envelope pass), `src/vesmaro/manager.py`
  (`_canon_gate`, `add`/`update` stamp strips, `_mint_relates_to_edges`),
  `src/vesmaro/decision_provider.py` (`CanonRecordView.from_memory`,
  `DecisionProvider.evaluate`), `src/vesmaro/storage/sqlite_store.py`
  (`_EDGE_KINDS`, `memory_edges` schema, `find_federated_duplicate`),
  `src/vesmaro/mesh_server.py` (`WriteMemory` fed-id duplicate gate).