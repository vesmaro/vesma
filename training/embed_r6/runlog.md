# embed-r6 decisive run log (APPEND-ONLY)

Format: ISO-timestamped append-only events. Never rewrite earlier lines.
Executor: gcw-ml-engineer (decisive round-6 run: phased schedule -> mono-guard-aware selection -> export -> single-shot gates).

## 2026-10-10 — session start

- (12:0x) Mission accepted. Prereg round-6 read in full from main @ 8310d1f (`git show`, local main is behind origin/main; NOT merged, read-only). Round-5 prereg inherited (thresholds/population/measurement unchanged).
- INPUTS VERIFIED BEFORE ANYTHING:
  - corpus `cat train val | sha256sum` = e64fdbea65f0… — MATCHES sealed fp; counts train 25420 / val 1261 = 26681. IDENTICAL to round-5.
  - gate set sha256 = cb36757769fb88… (180 pairs) — MATCHES.
  - prod reference src/vesmaro/models/vesma-embed-v1/model.onnx sha256 = 3b752e0671a5… — MATCHES 3b752e06.
  - characterization /tmp/r5-charact/ intact (run_phase0 npz + floors present).
- TEACHER VECTORS: REUSE DECIDED. Integrity provable end-to-end: (1) precompute script embed_r5/precompute_teacher_r5.py HARD-ASSERTS the corpus fp at read time (line 57) and the run log shows it passed; (2) state json final (26681/26681); (3) DIRECT re-verify 2026-10-10: npz texts array == current corpus texts (exact list equality), vectors (26681,1024) fp32 all finite. Saves ~3-6 h. npz copied (read-only source) into runs/embed-r6/.
- WORKSPACE: worktree wt/embed-r6-train created, branch feat/embed-r6-train FROM feat/embed-r5-train @ fa95de3 (verified tip). wt/embed-r5-train strictly read-only. Runs under training/runs/embed-r6/.
- COMMIT (this commit): run_config.json — schedule (phase 1 KD-only, transition R@5@384>=0.50 OR 8-epoch cap; phase 2 interleaved [3 KD : 1 CT], mono-replay ratio 624:171 = 3.65:1 >= 1:3 floor; mono guard 0.8932 at every phase-2 eval, trip = CT off to budget end; early stop unchanged; budget <=20), selection §3 (mono-guard-aware, rule verbatim, fallback-pool interpretation logged), export form FIXED int8-dynamic (no form fallback), prelaunch estimates (wall ~3.7-5.5 h clean; RAM launch gate >= 4 GB available — box currently at ~1 GB available from neighbor tenants, training launch BLOCKED until window opens).
- MECHANICS NOTES (logged pre-run): phase-2 interleave [3 KD : 1 CT] chosen over round-5's sequential streams (KD stream then CT stream) — same per-epoch batch totals, same KD coverage; the mono anchor now refreshes DURING the contrastive pass, not only before it. Ratio floor satisfied numerically either way; interleave is the executor's discretion under prereg §2. Selection proxy extended ADDITIVELY with shares (seed-dup/llm share_ge085) — required by §3 step 1, harness unchanged.

- (12:4x) SMOKE VALIDATION COMPLETE (fixture from r5 runs/embed-r5/smoke, read-only source; runs under runs/embed-r6/smoke-run + /tmp/r6-smoke-export):
  - smoke A (3 epochs, env-gated instrumentation PHASE1_CAP=1 + MONO_LINE=2.0): full state machine PASS — ep1 [P1] KD-only + TRANSITION→P2 (cap), ep2 [P2-ct] 4 interleaved CT batches + GUARD-TRIP→CT-OFF at eval, ep3 [P2] KD-only mono-replay. Proxy monitor carries the NEW share fields (sd median/share, llm median/share).
  - BUG FOUND AND FIXED in smoke: phase transition never armed the contrastive switch (ct_enabled_next stayed False) — first smoke run showed [P2] with ct_batches=0; fixed (transition now sets ct_enabled_next=True), re-run clean. THIS is why smoke exists.
  - smoke B (resume --start-epoch 3): backbone+projector+optimizer reload PASS; schedule state reconstructed from records (phase=2, ct_enabled=False post-guard); best/patience carried.
  - selection unit routes (synthetic records): step2 max-mono PASS; mono tie → higher criterion PASS; step3 fallback (max criterion under mono line, both phases) PASS; real REJECT route PASS (smoke-run records: no proxy passer, none ≥ 0.8932).
  - export smoke: fp32 + Gemm→MatMul normalize (1 node) + int8-dynamic quantize PASS; val readout EXACTLY reproduces trainer criterion (0.1467 — export exactness, r4/r5 property); int8-dynamic 28.24 MB (same size class as round-5 artifact); CHOSEN.txt on int8-dynamic.
  - gate smoke (engine venv ORT 1.27, THROWAWAY fixture artifact — not a candidate, no candidate numbers produced for any decision): prod plumbing assert PASS (mono 0.8984 vs stored 0.8982, cross_r5 0.8722 EXACT); all binding gates computed; g6a official subprocess PASS (6.43 ms); report kind=embed-r6-single-shot-gate-pass. Full measure→gate loop proven before the real single-shot.
- COMMIT: smoke instrumentation (env-gated, real-run defaults = prereg values) + gate_r6 argv fix.
