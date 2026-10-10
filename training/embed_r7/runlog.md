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
