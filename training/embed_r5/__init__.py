"""Round-5 decisive vesma-embed training (prereg embed-round5-prereg.md, ACTIVATED).

Per-slice KD (round-4 mechanics, unchanged) + translation-contrastive term
(lambda=1.0, tau=0.05, in-batch InfoNCE — phase-0.5 pilot selection,
feat/embed-r5-corpus @ 6dcb4a7). Single-shot gates on the sealed gate set
cb367577 after export-form choice; judged corpus c2ce056d is read only by
the g1 gate pass, never before selection+export are final.
"""
