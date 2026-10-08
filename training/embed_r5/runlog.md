# embed-r5 decisive run log (APPEND-ONLY)

Format: ISO-timestamped append-only events. Never rewrite earlier lines.
Executor: gcw-ml-engineer (decisive round-5 run: train -> select -> export -> single-shot gates).

## 2026-10-08 — session start

- (01:20) Mission accepted. Inputs verified BEFORE anything: r5 corpus `cat train val | sha256sum` = e64fdbea… (matches), train 25420 / val 1261 / total 26681; gate manifest read (cb367577…, 180 pairs); characterization floors read (/tmp/r5-charact, prod mono 0.8982, REJECT-candidate floors: seed-dup 0.8201/0.2727, llm 0.9051/0.7941, real 0.9344/0.9673, twin 0.8621/0.9357, cross R@5 0.9556/0.9444/0.9667, sibling anchor prod 0.6889 n=80 from build_gate_baseline.json).
- (01:29) Worktree wt/embed-r5-train created, branch feat/embed-r5-train from feat/embed-r4-train @ 507bfda. Prereg + phase-0.5 runlog read in full; pilot mechanics adopted (in-batch InfoNCE, pair-adjacent 16-pair batches, partner=i^1, lambda=1.0, tau=0.05).
- (01:33) build_pair_pool: TRAIN POOL = 2732 pairs {llm 1400, real 268, tatoeba 750, twin 262, seed-dup 52}. Non-tatoeba counts EXACTLY match the phase-0.5 pilot pool (1982) — machinery parity verified. Gate-sha invariant asserted (0 hits). Sibling monitor population = 80 pairs (r4-baseline parity asserted).
- (01:33) COMMIT aba00c4: run_config.json (committed BEFORE any run) + build_pair_pool + precompute_teacher_r5 (resumable).
- (01:34-01:38) INCIDENT (logged, resolved): first precompute launch raced with a relaunch — TWO precompute processes ran concurrently for ~2-4 min (nohup redirect failed on missing dir, then a second launch). Sequential-heavy-jobs rule violated transiently. Both killed, partial state erased, relaunched as ONE process (zcode background exec_4d7e2383). Correctness unaffected (frozen teacher, fixed batches, deterministic per-text vectors; nothing was resumed from the raced partials — clean restart). Cost: ~5 min wall.
- (01:38) Precompute running single-stream: 26681 texts, batch 32, bf16, threads 8/interop 1, partial npz every 64 batches (atomic rename). Estimate ~2.2 h.
- (01:55) train_r5.py written + committed BEFORE precompute finishes: partition stream design (KD-only batches over non-pair texts, then joint KD+InfoNCE pair-adjacent batches over the pool; every train text exactly once per epoch), per-epoch S1m-val per-slice R@5 (min-over-slices = selection + early-stop criterion), optimizer state in checkpoints (resume discipline), sqlite+JSON tracking, per-epoch gate-proxy monitoring (§4.3).
