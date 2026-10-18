"""Round-8 embed training package — BASE-SWAP (r8 prereg §1).

Stage 1: FT microsoft/harrier-oss-v1-0.6b on the sealed r7 corpus pairs
(multiple-negatives; per-epoch proxy eval; selection incl. epoch 0).
Stage 2: distill the 30M student (rubert-tiny2 + 312->384 projector) from
FT-harrier vectors via per-slice KD — NO contrastive (one variable per
round). Export int8-dynamic (r5 recipe); single-shot gates with the
ratified llm recalibration 0.90/0.75 (r8 prereg §2).

run_config.json is the committed-before-run contract.
"""
