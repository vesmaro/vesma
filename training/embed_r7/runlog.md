# Round-7 run log — llm-class quality (append-only)

Authoritative config: `run_config.json` (committed before the run).
Preregs: round-7 (`embed-round7-prereg-llmquality.md`, merge 72ddf3f) +
round-6 schedule (`embed-round6-prereg-schedule.md`, inherited §1) +
round-5 constants. Branch `feat/embed-r7-train` FROM `feat/embed-r6-train`
@ 7f50a07; worktree `wt/embed-r7-train`; runs at `training/runs/embed-r7/`.

## 2026-10-10 — setup

- (setup) Corpus verified BEFORE anything else: train 24841 / val 1228,
  total 26069; `cat train val | sha256sum` = ad3d438f89ce91f47d279b63f… —
  EXACT match to the sealed fp. Report.json delta consistent: 937 replaced
  (891 train / 46 val), 612 dropped (589 fail / 17 ngram / 6 cos).
- (setup) Worktree wt/embed-r7-train created from 7f50a07; envs verified:
  nm-train python (torch 2.13.0 CPU-used, transformers 5.16.1) and vesma
  venv (ORT 1.27.0). RAM at check: 9.7 GB available, 102 G swap present.
- (setup) `training/embed_r7/` package written: build_pair_pool_r7 (r6
  recipe verbatim + r7 llm shards), precompute_teacher_r7 (partial reuse +
  recompute, proof obligations), train_r7/select_r7/export_r7/gate_r7/
  master_chain_r7 (r6 clones; only corpus fp, paths, kinds, smoke-env
  changed — machinery byte-verbatim). run_config.json committed before run.
- teacher reuse PROVEN: reused=25136 (raw-string identity vs r6 npz d0337469585a…), recomputed=933 (all translated-llm-r4), r6-absent=1545; npz lockstep OK ((26069, 1024)); proof -> runs/embed-r7/reuse_proof.json
- teacher reuse PROVEN: reused=25136 (raw-string identity vs r6 npz d0337469585a…), recomputed=933 (all translated-llm-r4), r6-absent=1545; npz lockstep OK ((26069, 1024)); proof -> runs/embed-r7/reuse_proof.json
- (setup) Pair pool BUILT: 2183 pairs (llm 851 / real 268 / tatoeba 750 / twin 262 /
  seed-dup 52). IDENTITY vs r6 pool: all 1332 non-llm pair_keys IDENTICAL (no
  losses, no gains); llm src units kept 851, dropped 549 (= r7 build's row
  drops on the train/pool-eligible side). Sibling monitor population n=80
  (recipe verbatim). Pool key-list sha recorded in pairs_r7.json.
- (setup) Teacher vectors PROVEN + BUILT: reused 25136 by RAW-STRING equality
  (hash narrows, string decides — a case-fold hash collision old-vs-new llm
  text was caught in flight: 'Helios-Index…' vs 'Helios-index…'; raw-string
  criterion is the only sound one), recomputed 933 (ALL translated-llm-r4,
  0 non-llm), r6 texts absent from r7 = 1545 (all r5 llm-class: 612 dropped
  rows + 933 replaced old dst texts; arithmetic closes: 25136+933=26069,
  26681−1545=25136). npz (26069,1024) fp32 finite, lockstep asserted.
  4 new llm texts are byte-identical to old r6 texts → legitimately reused.
  Proof: runs/embed-r7/reuse_proof.json; source npz sha d0337469585a….
- (setup) SMOKE VALIDATION COMPLETE (fixture wt/embed-r5-train runs/smoke,
  read-only; /tmp/r7-smoke):
  * smoke A (3 ep, PHASE1_CAP=1 + MONO_LINE=2.0): full state machine PASS —
    ep1 [P1]+TRANSITION→P2(cap), ep2 [P2-ct] 4 CT batches + GUARD-TRIP→CT-OFF,
    ep3 [P2] mono-replay; proxy monitor carries share fields.
  * smoke B (resume --start-epoch 4): backbone+projector+optimizer reload
    PASS; schedule state reconstructed (phase=2, ct_enabled=False).
  * selection routes (synthetic records): step2 max-mono PASS; mono tie →
    higher criterion PASS; step3 fallback PASS; REJECT route PASS.
  * export/gate: r6-proven mechanics byte-verbatim (only paths/kinds edited);
    gate carries the internal production plumbing assert (measures prod
    3b752e06 vs stored 0.8982/0.8722 BEFORE the single-shot) — deviation vs
    r6's separate gate smoke, rationale logged.
- LAUNCH: master_chain_r7.sh detached (setsid/nohup) — RAM launch gate 4 GB,
  trainer resume loop, selection at end. Epochs ≤ 20, seed 42.
