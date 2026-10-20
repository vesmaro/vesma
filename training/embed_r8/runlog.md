# Round-8 run log — BASE-SWAP (append-only)

Authoritative config: `run_config.json` (committed before the run).
Prereg: round-8 (`embed-round8-prereg-baseswap.md`, ACTIVATED, owner
ratification 10.10) — MUST be verified merged into origin/main BEFORE the
decisive stage-1 launch (commit-order discipline). Branch
`feat/embed-r8-train` FROM `feat/embed-r7-train` @ `de097a8`; worktree
`wt/embed-r8-train`; stage-1 runs at `training/runs/embed-r8/ft-stage1/`,
stage-2 at `training/runs/embed-r8/`.

## STAGE-1 SELECTION RULE (fixed BEFORE the run; authority run_config.stage1)

- pool = epoch 0 (base, pre-FT) + FT epochs 1..4;
- candidates = mono_control_median >= base_mono − 0.010 AND cross_R@5 >=
  base_cross − 0.01 (trend guard + geometry hold);
- rank = MAX (llm_median + seed_dup_median)/2; tie: higher mono, then
  lower epoch. Epoch 0 passes its own gates by construction → a selection
  always exists; a harmful FT degrades to the sweep-audited base, never to
  an undefined teacher. Executed by `select_ft_r8.py` after `train_final`.

## 2026-10-18 — setup

- (setup) Corpus verified BEFORE anything: train 24841 / val 1228 = 26069;
  `cat train val | sha256sum` = ad3d438f89ce91f47d… — EXACT match to the
  sealed fp (same sealed corpus as r7; r8 changes the TEACHER, not data).
- (setup) Worktree wt/embed-r8-train created FROM de097a8; branch
  feat/embed-r8-train. r5/r6/r7 worktrees untouched (read-only).
- (setup) ENV SPLIT (transformers version wall): harrier-oss-v1-0.6b
  (model_type qwen3) requires transformers >= 5.18 — nm-train (5.16.1, the
  r5/r6/r7-proven env) left UNTOUCHED; isolated venv
  /var/home/abyss/.venvs/nm-eg-r8 created (python 3.12, torch 2.14.1+cpu,
  transformers 5.20.1) for stage-1 + teacher precompute ONLY. Stage-2
  (student KD, npz teacher) runs under nm-train exactly as r5/r6/r7.
- (setup) /tmp/r5-teachersweep GONE (tmp wipe): the sweep loading recipe
  was re-derived from the HF model card (last-token pooling, L2 norm,
  dim 1024, bare-text documents pattern) + the prereg §0 sweep anchors
  (burned 0.9407, S_cal 0.9889, R@5 1.0) kept as the recipe-validation
  reference for the epoch-0 readout. Model re-downloaded PINNED to
  revision f9b9dc8d367d443f2479d27aa5d8d2850c0774ee (HF main @ 18.10).
- (setup) pairs_r8.json = BYTE-COPY of runs/embed-r7/pairs_r7.json —
  sha256 52b82a84b099… both sides identical (corpus unchanged in r8 → the
  r7 pool is the r8 pool by identity). sibling_pop.json = byte-copy,
  sha256 9f0d0f466ce2… . Provenance in run_config.provenance.
- (setup) encoding recipe FIXED (run_config.stage1.encoding_recipe): BARE
  text (no instruction template — the student has no instruction concept;
  symmetric embedding of all proxy texts), last-token pooling, L2 norm,
  max_length 256; fp32 for evals; bf16 compute/fp32 npz for the stage-2
  teacher precompute (r5/r6/r7 teacher convention).
- (setup) loss FIXED: in-batch MN InfoNCE over the 2183-pair pool, batch
  layout [s1,d1,…] partner=i^1 — train_r7.contrastive_infonce VERBATIM
  (pilot-proven), scale 20 = 1/tau tau 0.05 (the corpus CT operating
  point). lr 1e-5 AdamW (conservative, full FT), cosine→10% + 20-step
  warmup, grad-clip 1.0, seed 42, 4 epochs (the full sanctioned 2–4 range,
  every epoch checkpointed + evaluated).
- (setup) package written: ft_base_r8 / select_ft_r8 / precompute_teacher_r8
  / train_r8 (r7 clone minus contrastive) / select_checkpoint_r8 (r6 §3
  rule, recalibrated selection proxy 0.90/0.75) / export_r8 (r5 recipe
  verbatim) / gate_r8 (r5 mechanics verbatim + patched llm thresholds,
  asserted) / master_chain_r8 (stage A–E; export+gates NOT chained —
  single-shot stays executor-run).
- (setup) STAGE-1/STAGE-2 dirs SPLIT (runs/embed-r8/ft-stage1 vs run
  root): the two trainers' metrics.jsonl schemas and epoch numbering
  collide otherwise.
- (setup) prelaunch estimates (run_config.stage1.prelaunch_estimates):
  stage-1 wall 1.7–4 h; teacher precompute 1.2–2.4 h; stage-2 3.7–5.5 h;
  total 8–12 h. Stage-1 peak RAM ~10.5 GB (0.6B fp32 AdamW foreach=False)
  vs ~10 GB available — BORDERLINE, documented; 33 G swap backstop; USB
  swap OFF and not relied on. Launch gates: RAM >= 4 GB, disk floor 2 GB,
  sequential heavy jobs only.

## 2026-10-19 — executor continuation (predecessor died in OOM crash before launch)

- (continuation) Fresh executor session (gcw-ml-engineer) took over per the
  standing continue-don't-reset rule. Predecessor completed ALL setup
  (corpus fp verified, pairs/sibling provenance, package written, selection
  rule fixed) and died in a zcode OOM crash during the stage-1 SMOKE
  (smoke runs.db carries only the 22:14:42Z launch event; no epoch_done).
  No decisive-run epochs were ever trained — nothing to unwind.
- (verify) ALL pre-flight re-verified by the continuation executor:
  prereg merged into origin/main (PR #38, merge 5598251 — commit-order
  discipline satisfied BEFORE any training); swap now 102 Gi TOTAL (usb 64 G
  pri10 ON + internal 8 G + zram 30.8 G), 97 G free — the crash-causing
  swap shortfall is FIXED; RAM available 13–14 G; worktree @ de097a8
  clean; pairs_r8/sibling_pop sha256 EXACT match to run_config provenance
  (52b82a84b099… / 9f0d0f466ce2…); corpus fp RE-ASSERTED independently:
  cat train.jsonl val.jsonl | sha256sum = ad3d438f89ce91f4… (24841+1228
  = 26069); venvs nm-eg-r8 (torch 2.14.1+cpu / transformers 5.20.1) and
  nm-train (torch 2.13.0 / 5.16.1 / onnx 1.22.0 / ORT 1.29.0) import OK;
  gate-set sha cb36757769fb… asserted in es5.load_gate_set (line 37);
  harrier @ f9b9dc8d… snapshot complete in distrobox HF cache (1 192 133 232
  bytes safetensors, no .incomplete), offline tokenizer load verified;
  gate_r5.THRESHOLDS carries the superseded 0.93/0.90 — the r8 wrapper
  patch REPLACES (not adds) and its sanity assert is meaningful.
- (deviation-note, favourable) run_config wrote "USB swapfile stays OFF"
  at setup; the coordinator turned the USB swap ON (total 102 Gi) after the
  crash. Launch/stop conditions UNCHANGED (never relied on USB swap);
  this only widens the backstop. Disk: 18 G free on /var/home (floor 2 G);
  rolling-optimizer policy keeps stage-1 peak ≈ 14.4 G (4 model ckpts +
  1 optimizer) — fits with margin.
- (plan) THIS commit = the package (commit-order: config before decisive
  run). Then: mechanics smoke in a THROWAWAY dir (--skip-proxy, 2 steps,
  deleted after) — the predecessor's smoke never completed, so trainer
  mechanics are unproven on this box. Then the decisive master chain
  detached (setsid nohup), poll: epoch-0 base readout vs sweep anchors
  (burned 0.9407 / S_cal 0.9889 / R@5 1.0 — wild mismatch = recipe
  re-derivation note, not a silent proceed), then per-epoch watch.

## 2026-10-20 — continuation attempt 5 (resource-isolated host-context chain)

- (verify) Machine state post-reboot: swap REPRIORITISED (zram 30.8G pri100 →
  internal /var/home/swapvol 8G pri10 → usb 64G pri1) — the inverted-swap
  thrash root cause of the attempt-1..4 oomd kills is STRUCTURALLY FIXED;
  RAM 20 G available, light load. Pre-flight re-verified: corpus fp
  ad3d438f… EXACT (recomputed 24841+1228); pairs_r8/sibling_pop shas EXACT
  (52b82a84b099… / 9f0d0f466ce2…); gate set cb367577… EXACT (180 rows,
  re-asserted against es5.EXPECTED_GATE_SHA); prereg on origin/main
  (PR #38 merge 5598251); worktree @ 2421ce3; harrier snapshot RE-VERIFIED
  present post-reboot: snapshot_download(pinned f9b9dc8d) identity pass,
  1 208 039 868 bytes total (safetensors 1 192 133 232 as recorded).
- (DEVIATION, reboot-driven — environment) `systemd-run --user` transient
  units execute HOST-side (distrobox talks to the host user manager), and
  box venvs on box /usr are NOT host-executable. Verified importable
  host-side: nm-eg-r8 (torch 2.14.1+cpu / transformers 5.20.1) and vesma
  .venv (ORT 1.27.0). Box nm-train is NOT host-executable
  (/usr/bin/python3.12 = box overlay) → stage-2/export run on
  `/var/home/abyss/.venvs/nm-train-host` — a HOST-side clone from the same
  uv cpython-3.12.13 interpreter with EXACT version parity
  (transformers 5.16.1, onnx 1.22.0, onnxruntime 1.29.0, numpy 2.5.2,
  safetensors 0.8.0, tokenizers 0.23.1, huggingface_hub 1.29.0; torch
  2.13.0 **+cpu** wheel vs box 2.13.0+cu130 wheel-used-on-CPU — CPU-only
  numerics, intra-run determinism by seed 42). Box venvs untouched.
  HF cache pinned to the box-home path via HF_HOME (harrier verified there).
- (DEVIATION, mission-ordered — chain scope) master_chain_r8 extended with
  stage F (export int8-dynamic) + stage G (single-shot gates: gate_r8 THEN
  g1 gate_s1m_candidate --artifact) — supersedes the 2421ce3 note "export
  and gates are not chained". Single-shot discipline preserved: once-only
  markers (gate_pass.json, g1_s1m_report.json), stale-partial guards before
  export/gate, judged c2ce056d read ONLY in stage G after the export exists.
  OOM contingency per mission: marker files BATCH8 (ft-stage1) → stage-A
  relaunch --batch-pairs 8; BATCH16 (run root) → stage-D relaunch
  --batch-size 16 (KD loss is per-text — batch-independent; an InfoNCE
  batch change would be a geometry deviation, logged if ever used).
- (smoke) The predecessor's smoke never completed — trainer mechanics are
  UNPROVEN on this box, and the host-context venvs are NEW. Running a FULL
  pipeline mechanics smoke (smoke_chain_r8.sh) in throwaway dirs under the
  same caps BEFORE the decisive launch: S1 = ft_base_r8 real pool 1 epoch
  @2 steps WITH proxy harness (es5 never ran under nm-eg-r8/numpy 2.5);
  S2 = train_r8 on a 240-text synthetic corpus + random teacher npz
  (VESMA_R8_SMOKE path); SEL = select_checkpoint_r8 rule mechanics; EXP =
  export_r8 fp32+int8-dynamic WITH latency probe under torch 2.13.0+cpu;
  GATE = gate_r8 report mechanics incl. g6a subprocess under the host
  context. g1 S1m NOT smoked — the judged corpus stays single-touch for the
  decisive pass (machinery unchanged from r5/6/7 production runs).
- (plan) smoke unit (capped) → verify → cleanup → decisive unit
  `embed-r8-chain` with MemoryMax=14G MemorySwapMax=16G CPUQuota=800% →
  caps verified via `systemctl --user show` → poll per epoch. Selection
  rule, thresholds, export form: UNCHANGED from run_config (fixed
  2026-10-18, still before any decisive step).

## 2026-10-20/21 — mechanics smoke COMPLETE (PASS) — decisive launch cleared

- (smoke OOM, contained) Smoke run 2: stage-1 trainer OOM-KILLED AT THE 14G
  CAP exactly as designed — "14G memory peak, 16G memory swap peak", unit
  killed, zcode + desktop UNTOUCHED (the resource isolation worked; first
  live validation of the attempt-5 fix). Root cause: full-FT backward
  retains attention-score activations across 28 layers (32 seqs × 256 fp32)
  — the 2421ce3 estimate under-counted activations.
- (lever) `--grad-checkpoint` added to ft_base_r8.py (recompute activations
  in backward; loss semantics IDENTICAL — same batch layout, same
  in-batch negatives, same schedule; ~30-40% more compute). Wired into
  stage-A invocation. Verified: peak 12.65G / 12.07G across two runs —
  fits the non-negotiable cap with ~1.4G margin. BATCH8 marker stays the
  escalation lever. bf16 autocast REJECTED: CPU lacks amx_bf16/avx512_bf16
  (software path would slow down); fp32 per run_config stands.
- (smoke run 3 — full clean-room pass, all EXIT 0): S1 25.7 min total
  (epoch-0 readout 669 s incl. one-time oneDNN JIT; epoch-1 record 250.3 s
  covering 2 grad-checkpointed steps + readout + checkpoint → steady-state
  ~20-25 s/step → 4-epoch decisive stage-1 ≈ 4-5.5 h under CPUQuota 800%);
  S2 KD trainer 24 s (240-text synthetic corpus, random-teacher npz, proxy
  monitor + slice metrics + checkpoint + phase record all written); SEL
  honest-REJECT on garbage proxies (rule step 4) + export shim; EXP 39 s
  (fp32 + int8-dynamic forms, latency probe); GATE report written under the
  vesma runtime: thresholds RECALIBRATED 0.90/0.75 asserted, gate set sha
  cb367577 asserted in-report, g6a subprocess OK (6.41 ms mean, §A.2
  method), quality flags False as expected for a garbage model.
- (smoke fixes — smoke script only) fixture npz name → teacher_vectors.npz
  (export_r8 hardcodes); export --corpus-dir → SMOKE corpus (real-corpus
  default breaks npz lockstep); inspector key g6a_report.g6a.measured_mean_ms.
  Standalone inspector re-run vs the smoke artifacts: 16/16 OK.
- (not smoked) g1 gate_s1m_candidate — judged corpus c2ce056d stays
  single-touch for the decisive pass; machinery unchanged r5/6/7.
- (GO) Decisive chain `embed-r8-chain`: MemoryMax=14G MemorySwapMax=16G
  CPUQuota=800%, stage A→G, resumable per marker. Preflight: RAM 19.8G
  avail, corpus/pairs/sibling/gate-set shas re-verified this session,
  harrier @ f9b9dc8d in shared cache, venvs import-verified host-side.
