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
- (resume 2026-10-18, executor gcw-ml-engineer) Predecessor session died on quota exhaustion after ep7 (last metrics ep7 08:36 10.10; no trainer running since). Environment re-verified: branch feat/embed-r7-train @ db5e69d clean; corpus train+val sha = ad3d438f… (sealed fp, matches run_config); counts 24841/1228; r6 source npz on disk hashes d0337469585a… = reuse_proof.source_npz_sha256 (reuse proof intact); venv nm-train torch 2.13.0/transformers 5.16.1 matches prelaunch env. DISK RECLAIM (executor note): /var/home at 100% (3.7G free) vs ~4.4G needed for ep8–20 checkpoints → removed optimizer.pt of epochs 1–6 ONLY (6 × 223 MB = 1275 MB freed; 5.3G free at relaunch). Rationale: the committed resume machinery loads ONLY the latest completed epoch's optimizer state (train_r7.py: last_done = start_epoch-1) and reconstructs schedule state from the LAST metrics record — older optimizer states are unreachable by design; ep7 optimizer kept as the resume source. Rolling policy this session: after epoch N+1 checkpoint lands, epoch N's optimizer.pt is removed (keep-latest). All model.safetensors/projector.pt of every epoch retained (fallback-selection route may choose any epoch). Swap note: box swap now 39.7G (zram+internal) vs 102G pre-crash — USB swap not mounted; launch gate unaffected (available RAM 13.6G >= 4G).

## 2026-10-18 — run day (resume 13:58 → 22:24, gates later)

- (13:58) MASTER CHAIN RESUMED (detached, setsid) from ep7 checkpoint — resume asserts PASS: ep7 state loaded (backbone+projector+optimizer), schedule reconstructed phase=1 ct_enabled=False, best 0.394951 patience 0/4. Corpus fp ad3d438f re-asserted at trainer start; teacher npz lockstep OK (26069, 1024); r6 source npz hash re-verified d0337469585a… (reuse proof intact).
- (14:12-22:24) PHASE 1 ep8 [P1]: R@5@384 0.4607 — 0.50 transition NOT reached → cap transition after ep8 (TRANSITION→P2 logged). mono 0.8715 (still sinking under pure KD, same as r6 phase-1).
- (14:26) PHASE 2 ep9 [P2-ct]: 137 interleaved CT batches ran; at eval mono 0.8683 < 0.8932 → GUARD TRIPPED at the FIRST phase-2 eval (event mono_guard_trip; prereg §2 ratified behavior — the ep7 trail predicted it). Contrastive OFF for the rest of the run; CT's one-epoch effect: min-slices 0.4055→0.4143, sd share 0.7727→0.7727, llm 0.8851→0.8839, mono −0.0032.
- (14:39-22:24) PHASE 2 mono-replay (KD-only, ep10-20): criterion improved to best 0.4529 at ep20 (R@5@384 0.5635); mono kept sinking (ep20 0.8265) — r6 replication: KD-replay cannot restore what KD itself erodes. Early stop never fired (dips at ep11, ep15, ep18/19 <0.001 gains but <4 consecutive). Wall 8h26m total session (incl. 8-day gap), train-side ~3.4h for 13 epochs; per-epoch 11.6-14.4 min.
- (22:24) TRAINING COMPLETE: 20/20 (cap). SELECTION (§3 rule as ratified): phase-2 cross-proxy candidates = NONE (llm proxy max at evaluable epochs 0.8948 ep4; phase-2 max 0.8839 ep9 — never ≥0.93/0.90). Fallback pool (mono ≥0.8932) = {ep1..ep4} → **epoch 4** (min-over-slices 0.3492; proxy mono 0.9012, sd 0.863/0.7727, llm 0.8948/0.8235). Route step3_fallback, identical shape to r6.
- (22:3x) EXPORT of epoch 4 (the ONLY checkpoint exported): fp32 source + int8-dynamic (prereg-fixed; static int8/fp16 never built). fp32 val crit 0.3492 EXACTLY reproduces trainer (export exactness). int8-dynamic: crit 0.3505, monoΔ +0.0021, sd 0.8623/0.7727, llm 0.8920/0.8088, cross R@5 0.9556, burned144 0.8653, size 28.24 MB, context probe 5.08 ms; per-text cos vs fp32-torch median 0.9995/min 0.999. Artifact sha256 a186abaa500e9776… (full in measure_forms.json). EXPORT-HARNESS COMPARISON vs r6 candidate (same recipe, both int8-dynamic): llm 0.8920/0.8088 vs 0.8947/0.8529 — the 1.7B llm pairs did NOT lift the llm class (median −0.0027, share −4.4pp). Official numbers from the single-shot gate pass below.
