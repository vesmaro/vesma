"""Context Filter for Vesma. (M10 — mandatory v1 subsystem)

Sits between interface input and downstream pipeline/recall so the model
receives concise, semantically complete context instead of raw noise.

Key invariant: filtering never destroys data.
  raw_content  — always retained in Memory for audit/drill-down
  clean_content — default payload for retrieval and model-facing flows

Pipeline order:
  1. dedup    — exact + near-duplicate suppression
  2. noise    — ANSI/progress/timestamps/separators cleanup
  3. extract  — errors/warnings/exit-status + informative sampling
  4. compress — semantic compression of repetitive blocks
  5. tokens   — pre-tokenization estimation and budget accounting

Profiles: log | terminal | code | docs | web | default
Configuration: ~/.mnemos/filter_profiles.yaml
"""
