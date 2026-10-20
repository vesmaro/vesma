#!/usr/bin/env bash
# Round-8 MECHANICS SMOKE — the predecessor died mid-smoke, so trainer
# mechanics are UNPROVEN on this box (runlog 2026-10-19 plan). This script
# proves the FULL chain mechanics end-to-end in THROWAWAY dirs, under the
# SAME systemd caps as the decisive unit, then deletes the heavy artifacts:
#   S1: ft_base_r8 on the REAL pairs pool, 1 epoch capped at 2 optimizer
#       steps, WITH the proxy harness (proves es5 under nm-eg-r8: numpy 2.5
#       / torch 2.14 — never ran in this env) + epoch-0/epoch-1 records +
#       checkpoint + rolling optimizer deletion.
#   S2: train_r8 on a 240-text synthetic corpus + random teacher npz
#       (VESMA_R8_SMOKE=1 — fp assert bypass is the designed smoke path):
#       lockstep, KD loss, MRL, proxy monitor, checkpoint, phase record.
#   SEL: select_checkpoint_r8 on the smoke run (pure-python rule mechanics).
#   EXP: export_r8 WITH the latency probe (fp32 + int8-dynamic forms on a
#        tiny model; stale-partial path not exercised).
#   GATE: gate_r8 on the tiny artifact — quality thresholds WILL fail
#       (garbage vectors); mechanics PASS = report written with the patched
#       threshold table + g6a subprocess report. g1 S1m NOT smoked (judged
#       corpus stays single-touch for the decisive pass; machinery unchanged
#       from r5/6/7 production runs).
# Exit 0 = all mechanics proven. Heavy dirs are removed on success.
set -u
RUNS=/var/home/abyss/LABs/Projects/Project-Vesma/vesma/wt/embed-r8-train/training/runs
SMOKE_ROOT="$RUNS/embed-r8-smoke-r8mech"
S1_DIR="$SMOKE_ROOT/ft-stage1"
S2_DIR="$SMOKE_ROOT/stage2"
PY_S1=/var/home/abyss/.venvs/nm-eg-r8/bin/python
PY_S2=/var/home/abyss/.venvs/nm-train-host/bin/python
PY_GATE=/var/home/abyss/LABs/Projects/Project-Vesma/vesma/.venv/bin/python
WT=/var/home/abyss/LABs/Projects/Project-Vesma/vesma/wt/embed-r8-train
CORPUS=/var/home/abyss/LABs/Projects/Project-Vesma/vesma-cortex/wt/embed-r5-corpus/data/embed-r7/corpus
export HF_HOME=/var/home/abyss/.distrobox/ubuntu/home/.cache/huggingface
export HF_HUB_DISABLE_TELEMETRY=1
export MALLOC_ARENA_MAX=2
export MALLOC_TRIM_THRESHOLD_=134217728
export VESMA_R8_SMOKE=1
LOG="$RUNS/embed-r8-smoke-r8mech.log"
cd "$WT"
exec >> "$LOG" 2>&1
echo "[smoke] $(date -Is) mechanics smoke start (pid $$)"

FAIL=0
step() { # step <name> <cmd...>
  local name="$1"; shift
  echo "[smoke] $(date -Is) == $name =="
  local rc=0
  if "$@"; then rc=0; else rc=$?; fi
  if [ "$rc" -eq 0 ]; then echo "[smoke] $(date -Is) == $name: EXIT 0 =="
  else echo "[smoke] $(date -Is) == $name: FAILED (exit $rc) =="; FAIL=1; return 1; fi
}

rm -rf "$SMOKE_ROOT"
mkdir -p "$S1_DIR" "$S2_DIR"

# rerun lane (SKIP_S1_S2=1): S1/S2/SEL already proven in a previous smoke —
# keep the existing dirs and prove only the tail (export + gate)
if [ "${SKIP_S1_S2:-0}" = "1" ] && [ -f "$S1_DIR/train_final.json" ] && [ -f "$S2_DIR/epoch1/model.safetensors" ]; then
  echo "[smoke] SKIP_S1_S2=1 — reusing proven S1/S2 dirs"
else
  SKIP_S1_S2=0
fi

# ── S1: stage-1 trainer mechanics (real pool, 2 steps, full proxy) ──
if [ "${SKIP_S1_S2:-0}" = "1" ]; then
  echo "[smoke] SKIP_S1_S2=1 — S1 already proven, skipping"
else
step S1-ft-base \
  "$PY_S1" training/embed_r8/ft_base_r8.py \
    --pairs "$RUNS/embed-r8/pairs_r8.json" \
    --out-dir "$S1_DIR" \
    --epochs 1 --max-steps 2 --threads 12 --grad-checkpoint
fi

# ── S2 fixture: 200 train + 40 val texts, random (240,1024) teacher ──
if [ "${SKIP_S1_S2:-0}" = "1" ]; then
  echo "[smoke] SKIP_S1_S2=1 — S2 already proven, skipping"
else
step S2-fixture "$PY_S1" - "$CORPUS" "$S2_DIR" <<'EOF'
import json, sys
from pathlib import Path
import numpy as np
corpus, out = Path(sys.argv[1]), Path(sys.argv[2])
train = [l for l in (corpus / "train.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()][:200]
val = [l for l in (corpus / "val.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()][:40]
(out / "corpus").mkdir(parents=True, exist_ok=True)
(out / "corpus" / "train.jsonl").write_text("\n".join(train) + "\n", encoding="utf-8")
(out / "corpus" / "val.jsonl").write_text("\n".join(val) + "\n", encoding="utf-8")
texts = [json.loads(l)["text"] for l in train + val]
rng = np.random.default_rng(42)
v = rng.standard_normal((len(texts), 1024)).astype(np.float32)
v /= np.linalg.norm(v, axis=1, keepdims=True)
np.savez(out / "teacher_vectors.npz", vectors=v, texts=np.array(texts, dtype=object))
print(f"fixture: {len(texts)} texts, npz {v.shape}")
EOF

# ── S2: stage-2 trainer mechanics ──
step S2-train-kd \
  "$PY_S2" training/embed_r8/train_r8.py \
    --corpus-dir "$S2_DIR/corpus" \
    --teacher-vectors "$S2_DIR/teacher_vectors.npz" \
    --out-dir "$S2_DIR" \
    --epochs 1 --batch-size 32 --threads 8

# ── SEL: stage-2 selection rule mechanics on the smoke metrics ──
step SEL-select-checkpoint \
  "$PY_S2" training/embed_r8/select_checkpoint_r8.py --run-dir "$S2_DIR"
# export_r8 refuses a selection whose chosen_epoch != --epoch; a garbage
# smoke artifact may legitimately select NOTHING (rule step 4 = REJECT).
# Keep the rule's true output, shim a chosen epoch for the export step only.
if [ -f "$S2_DIR/selection.json" ]; then
  mv "$S2_DIR/selection.json" "$S2_DIR/selection_rule_result.json"
  echo '{"chosen_epoch": 1, "smoke_shim": true}' > "$S2_DIR/selection.json"
  echo "[smoke] selection shim installed (rule result kept as selection_rule_result.json)"
fi
fi

# ── EXP: export mechanics (fp32 + int8-dynamic + latency probe); corpus-dir
# MUST point at the SMOKE corpus (the real-corpus default breaks npz lockstep)
step EXP-export \
  "$PY_S2" training/embed_r8/export_r8.py --run-dir "$S2_DIR" --epoch 1 \
    --corpus-dir "$S2_DIR/corpus"

# ── GATE: gate mechanics on the tiny artifact (quality FAIL expected) ──
if "$PY_GATE" training/embed_r8/gate_r8.py \
     --artifact-dir "$S2_DIR/export/int8-dynamic" \
     --out "$S2_DIR/gate_smoke.json"; then
  echo "[smoke] gate_r8 exit 0 (report written; quality flags expected FAIL)"
else
  echo "[smoke] gate_r8 nonzero exit — inspect"; FAIL=1
fi

# ── verify mechanics artifacts ──
"$PY_S1" - "$S1_DIR" "$S2_DIR" <<'EOF'
import json, sys
from pathlib import Path
s1, s2 = Path(sys.argv[1]), Path(sys.argv[2])
ok = True
def need(p, cond=True):
    global ok
    good = p.exists() and cond
    print(("  OK  " if good else "  MISS") + f" {p}")
    ok = ok and good
    return good

m1 = [json.loads(l) for l in (s1 / "metrics.jsonl").read_text().splitlines() if l.strip()]
need(s1 / "metrics.jsonl", len(m1) == 2 and m1[0]["epoch"] == 0 and m1[1]["epoch"] == 1)
need(s1 / "metrics.jsonl", "proxy_readout" in m1[0] and "proxy_readout" in m1[1])
need(s1 / "epoch1" / "model.safetensors")
need(s1 / "epoch1" / "optimizer.pt")
need(s1 / "train_final.json")
b284 = m1[0]["proxy_readout"]["full_1024"]
print(f"  epoch0 base full1024: llm={b284['llm_median']} mono={b284['mono_control_median']} cross={b284['cross_R@5']} burned={b284['burned144_median']}")

m2 = [json.loads(l) for l in (s2 / "metrics.jsonl").read_text().splitlines() if l.strip()]
need(s2 / "metrics.jsonl", len(m2) == 1 and m2[0]["epoch"] == 1)
need(s2 / "metrics.jsonl", "proxy_monitor" in m2[0] and "s1m_val_by_slice" in m2[0])
need(s2 / "epoch1" / "model.safetensors")
need(s2 / "epoch1" / "projector.pt")
rule = json.loads((s2 / "selection_rule_result.json").read_text())
need(s2 / "selection_rule_result.json",
     rule.get("chosen_epoch") in (1, None) and bool(rule.get("route")))
print(f"  selection rule route: {rule.get('route')} chosen={rule.get('chosen_epoch')}")
need(s2 / "export" / "measure_forms.json")
rec = json.loads((s2 / "export" / "measure_forms.json").read_text())
need(s2 / "export" / "measure_forms.json", rec.get("chosen_form") == "int8-dynamic")
need(s2 / "export" / "int8-dynamic" / "model.onnx")
need(s2 / "export" / "int8-dynamic" / "CHOSEN.txt")
lat = rec["results"]["int8-dynamic"].get("latency_probe", {})
print(f"  export latency probe: {lat.get('mean_ms')} ms (mechanics only)")

g = json.loads((s2 / "gate_smoke.json").read_text())
need(s2 / "gate_smoke.json", g.get("kind") == "embed-r8-single-shot-gate-pass")
need(s2 / "gate_smoke.json", g.get("thresholds", {}).get("llm_median") == 0.90
     and g.get("thresholds", {}).get("llm_share") == 0.75)
gp = {k: v.get("pass") for k, v in g.items() if isinstance(v, dict) and "pass" in v}
print(f"  gate flags (quality garbage expected): {gp}")
g6a = (g.get("g6a_report") or {}).get("g6a") or {}
need(s2 / "gate_smoke.json", bool(g6a) and g6a.get("measured_mean_ms") is not None)
print("MECHANICS", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
EOF
if [ $? -ne 0 ]; then FAIL=1; fi

if [ "$FAIL" -eq 0 ]; then
  echo "[smoke] $(date -Is) MECHANICS SMOKE PASS — deleting heavy artifacts (log kept)"
  rm -rf "$SMOKE_ROOT"
else
  echo "[smoke] $(date -Is) MECHANICS SMOKE FAIL — dirs LEFT for inspection: $SMOKE_ROOT"
fi
echo "[smoke] $(date -Is) smoke end FAIL=$FAIL"
exit $FAIL
