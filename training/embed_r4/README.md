# training/embed_r4 — round-4 F-leg (30M student) train + gates

Executor: gcw-ml-engineer. Authority: `embed-round4-prereg-DRAFT.md`
(ACTIVATED) + ADDENDUM-6/7/8 (ratified); see `run_config.json` — the
committed pre-launch contract (thresholds are NOT tunable here).

## Layout

- `train_r4.py` — KD trainer, adapted verbatim from the proven
  `training/probe/probe_train.py` engine (probe leg a-30m IS the
  ratified F config). Teacher precompute (`--precompute`) produces the
  lockstep npz; training consumes it (corpus lockstep asserted).
- `select_checkpoint.py` — val-only selection (rule committed in
  `run_config.json` BEFORE training).
- `gate_s1m_candidate.py` — g1/g2 single-shot through production S1m
  mechanics (`--self-check` validates plumbing against the recorded
  BASELINE §10 with the production artifact, BEFORE the single-shot).
- `gate_mrl.py` — g3 slices (256/128/64 + L2 renorm vs same-harness
  full-384; tolerances 0.02/0.03/0.05).
- `gate_latency.py` — g6a per addendum-6 §A.2 (mean ≤ 45 ms, 4 threads).

## Protocol (short form)

1. Corpus fingerprint check (`cat train val | sha256sum` vs
   `7a3bf00b…8b645`) BEFORE anything; corpus read-only forever.
2. Teacher lockstep precompute over all 25496 texts.
3. Train 6 epochs, checkpoint + S1m-val (1275 val queries, frozen
   teacher-top5) per epoch; metrics.jsonl append-only.
4. Select checkpoint on val ONLY. The sealed judged corpus c2ce056d is
   not read before this point (single-shot honesty, prereg §4).
5. Export ONNX 1×256 int8 (opset 15) from the selected checkpoint.
6. Harness self-check (production artifact must reproduce BASELINE
   recall@5 = 0.9484 exactly — deterministic), then exactly ONE
   measured pass of g1/g2 + g3 on the exported candidate, plus g6a.
7. Gate numbers are reported as measured. FAIL is reported plainly:
   no threshold tuning, no re-rolls, no post-hoc checkpoint swaps on
   judged data.

## Run artifacts (gitignored area)

`training/runs/embed-r4/`: teacher_vectors.npz, a-30m/ (checkpoints +
metrics.jsonl + selection.json), export/ (model.onnx + tokenizer.json +
manifest.json), gate_*.json, runlog.md (append-only).
