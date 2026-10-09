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
