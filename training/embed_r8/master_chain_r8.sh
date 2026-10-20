#!/usr/bin/env bash
# Round-8 DETACHED master chain — FULL PIPELINE (continuation executor,
# 2026-10-20; supersedes the "export+gates not chained" note of 2421ce3):
#   stage-0 inputs check
#   A  stage-1 FT base (harrier-oss-v1-0.6b, resume loop)
#   B  stage-1 selection (select_ft_r8 — rule fixed in run_config BEFORE run)
#   C  teacher precompute (FT'd harrier -> teacher_vectors.npz, resume-capable)
#   D  stage-2 student KD (train_r8, NO contrastive, resume loop)
#   E  stage-2 selection (r6 §3 rule, r8 recalibrated proxy)
#   F  export int8-dynamic (prereg-fixed form; stale-partial guard)
#   G  single-shot gates: gate_r8 (main gate, ONE pass) + g1 S1m
#      (gate_s1m_candidate --artifact, judged c2ce056d — read ONLY here)
#
# Runs as a systemd-run --user transient unit (MemoryMax=14G, MemorySwapMax=
# 16G, CPUQuota=800%) — detached from any agent process tree; the unit's
# OOM-kill hits ONLY this unit and the chain RESUMES from per-stage markers:
#   A/D: metrics.jsonl + epoch<N>/{model.safetensors,optimizer.pt} (+START
#        epoch recomputed); B: ft_selection.json; C: teacher_vectors.npz;
#        E: selection.json; F: export/measure_forms.json; G: gate_pass.json
#        + g1_s1m_report.json. A mid-stage kill leaves at most a stale
#        partial (guards delete it before the stage re-runs).
# OOM contingency markers (executor-created, logged before use):
#   ft-stage1/BATCH8   -> stage A relaunch with --batch-pairs 8
#   .                  -> stage D relaunch with --batch-size 16 (BATCH16)
# Every heavy stage honours the launch gate (RAM) and the disk floor (2 GB).
set -u
RUN_DIR=/var/home/abyss/LABs/Projects/Project-Vesma/vesma/wt/embed-r8-train/training/runs/embed-r8
S1_DIR="$RUN_DIR/ft-stage1"   # stage-1 artifacts SEPARATE from stage-2 (metrics collision guard)
PY_S1=/var/home/abyss/.venvs/nm-eg-r8/bin/python
PY_S2=/var/home/abyss/.venvs/nm-train-host/bin/python   # host-context clone of nm-train (same pins; torch 2.13.0+cpu) — deviation logged in runlog
PY_GATE=/var/home/abyss/LABs/Projects/Project-Vesma/vesma/.venv/bin/python
CORPUS=/var/home/abyss/LABs/Projects/Project-Vesma/vesma-cortex/wt/embed-r5-corpus/data/embed-r7/corpus
WT=/var/home/abyss/LABs/Projects/Project-Vesma/vesma/wt/embed-r8-train
# host-context unit: the HF cache lives in the box home (harrier @ f9b9dc8d
# verified there 2026-10-20) — pin it explicitly, HOME differs on the host
export HF_HOME=/var/home/abyss/.distrobox/ubuntu/home/.cache/huggingface
export HF_HUB_DISABLE_TELEMETRY=1
LOG="$RUN_DIR/chain.log"
mkdir -p "$S1_DIR"
cd "$WT"
exec >> "$LOG" 2>&1
echo "[master] $(date -Is) master chain start (pid $$) host-context unit"

launch_gate() {
  while true; do
    AVAIL_MB=$(free -m | awk '/^Mem:/{print $7}')
    echo "[master] $(date -Is) launch gate: available ${AVAIL_MB} MB (need >= 4096)"
    if [ "$AVAIL_MB" -ge 4096 ]; then break; fi
    sleep 120
  done
}

disk_gate() {
  FREE_KB=$(df -k /var/home | tail -1 | awk '{print $4}')
  if [ "$FREE_KB" -lt 2097152 ]; then
    echo "[master] $(date -Is) FATAL: disk floor 2 GB violated (${FREE_KB} KB free)"
    exit 3
  fi
}

compute_stage1_start_epoch() {
  "$PY_S1" - <<'EOF'
import json
from pathlib import Path
run = Path("training/runs/embed-r8/ft-stage1")
final = run / "train_final.json"
sel = run / "ft_selection.json"
if sel.exists() and final.exists():
    print("DONE"); raise SystemExit
if not (run / "metrics.jsonl").exists():
    print(1); raise SystemExit
recs = [json.loads(l) for l in (run / "metrics.jsonl").read_text().splitlines() if l.strip()]
start = 1
for r in sorted(recs, key=lambda r: -r["epoch"]):
    if r["epoch"] == 0:
        continue
    ck = run / f"epoch{r['epoch']}"
    if (ck / "optimizer.pt").exists() and ((ck / "model.safetensors").exists() or (ck / "config.json").exists()):
        start = r["epoch"] + 1
        break
print(start)
EOF
}

compute_stage2_start_epoch() {
  "$PY_S2" - <<'EOF'
import json
from pathlib import Path
run = Path("training/runs/embed-r8")
if not (run / "metrics.jsonl").exists():
    print(1); raise SystemExit
recs = [json.loads(l) for l in (run / "metrics.jsonl").read_text().splitlines() if l.strip()]
start = 1
for r in sorted(recs, key=lambda r: -r["epoch"]):
    ck = run / f"epoch{r['epoch']}"
    if (ck / "optimizer.pt").exists() and (ck / "model.safetensors").exists():
        start = r["epoch"] + 1
        break
print(start)
EOF
}

# ── stage 0: inputs present ──
if [ ! -f "$RUN_DIR/pairs_r8.json" ]; then
  echo "[master] $(date -Is) FATAL: pairs_r8.json missing"; exit 1
fi
disk_gate

# ── stage A: FT base (resume loop) ──
while true; do
  if [ -f "$S1_DIR/train_final.json" ]; then break; fi
  if [ -f "$S1_DIR/STOP" ]; then echo "[master] $(date -Is) STOP flag (stage A)"; break; fi
  launch_gate
  START=$(compute_stage1_start_epoch | tail -1)
  if [ "$START" = "DONE" ]; then break; fi
  BATCH_ARGS=()
  if [ -f "$S1_DIR/BATCH8" ]; then BATCH_ARGS=(--batch-pairs 8); fi
  echo "[master] $(date -Is) stage-1 FT from epoch $START ${BATCH_ARGS[*]:-}"
  "$PY_S1" training/embed_r8/ft_base_r8.py \
    --pairs "$RUN_DIR/pairs_r8.json" \
    --out-dir "$S1_DIR" \
    --epochs 4 --start-epoch "$START" ${BATCH_ARGS+"${BATCH_ARGS[@]}"}
  code=$?
  echo "[master] $(date -Is) stage-1 trainer exited code=$code"
  if [ "$code" -eq 4 ]; then echo "[master] non-finite loss — not relaunching"; exit 4; fi
  sleep 30
done

# ── stage B: stage-1 selection ──
if [ -f "$S1_DIR/train_final.json" ] && [ ! -f "$S1_DIR/ft_selection.json" ]; then
  "$PY_S1" training/embed_r8/select_ft_r8.py --run-dir "$S1_DIR"
  echo "[master] $(date -Is) stage-1 selection done (code=$?)"
fi

# ── stage C: teacher precompute (FT'd harrier -> npz) ──
if [ -f "$S1_DIR/ft_selection.json" ] && [ ! -f "$RUN_DIR/teacher_vectors.npz" ]; then
  launch_gate
  disk_gate
  echo "[master] $(date -Is) teacher precompute start"
  "$PY_S1" training/embed_r8/precompute_teacher_r8.py
  echo "[master] $(date -Is) teacher precompute code=$?"
fi
if [ ! -f "$RUN_DIR/teacher_vectors.npz" ]; then
  echo "[master] $(date -Is) FATAL: teacher npz missing after stage C"; exit 1
fi

# ── stage D: student KD training (resume loop; NO contrastive in r8) ──
# RUN_DIR/train_final.json can only be stage-2's (stage-1 lives in ft-stage1/)
while true; do
  if [ -f "$RUN_DIR/train_final.json" ]; then break; fi
  if [ -f "$RUN_DIR/STOP" ]; then echo "[master] $(date -Is) STOP flag (stage D)"; break; fi
  launch_gate
  disk_gate
  START=$(compute_stage2_start_epoch | tail -1)
  BATCH_ARGS=()
  if [ -f "$RUN_DIR/BATCH16" ]; then BATCH_ARGS=(--batch-size 16); fi
  echo "[master] $(date -Is) stage-2 trainer from epoch $START ${BATCH_ARGS[*]:-}"
  "$PY_S2" training/embed_r8/train_r8.py \
    --corpus-dir "$CORPUS" \
    --teacher-vectors "$RUN_DIR/teacher_vectors.npz" \
    --out-dir "$RUN_DIR" \
    --epochs 20 --start-epoch "$START" ${BATCH_ARGS+"${BATCH_ARGS[@]}"}
  code=$?
  echo "[master] $(date -Is) trainer exited code=$code"
  if [ -f "$RUN_DIR/train_final.json" ]; then break; fi
  if [ "$code" -eq 4 ]; then echo "[master] non-finite loss — not relaunching"; exit 4; fi
  sleep 30
done

# ── stage E: stage-2 selection (r6 §3 rule, r8 recalibrated proxy) ──
if [ -f "$RUN_DIR/train_final.json" ] && [ ! -f "$RUN_DIR/selection.json" ]; then
  "$PY_S2" training/embed_r8/select_checkpoint_r8.py --run-dir "$RUN_DIR"
  echo "[master] $(date -Is) stage-2 selection done (code=$?)"
fi
if [ ! -f "$RUN_DIR/selection.json" ]; then
  echo "[master] $(date -Is) FATAL: selection.json missing after stage E"; exit 1
fi

# ── stage F: export int8-dynamic (prereg-fixed form; r5 recipe verbatim) ──
if [ ! -f "$RUN_DIR/export/measure_forms.json" ]; then
  # stale-partial guard: a previous unit killed mid-export may have left a
  # partial onnx tree without the final marker — rebuild from scratch
  if [ -d "$RUN_DIR/export" ]; then
    echo "[master] $(date -Is) removing stale partial export tree (no measure_forms.json)"
    rm -rf "$RUN_DIR/export"
  fi
  EPOCH=$("$PY_S2" - "$RUN_DIR" <<'EOF'
import json, sys
sel = json.loads((sys.argv[1] + "/selection.json").read_text())
print(int(sel["chosen_epoch"]))
EOF
)
  launch_gate
  disk_gate
  echo "[master] $(date -Is) export int8-dynamic from epoch $EPOCH"
  "$PY_S2" training/embed_r8/export_r8.py --run-dir "$RUN_DIR" --epoch "$EPOCH"
  echo "[master] $(date -Is) export code=$?"
fi
if [ ! -f "$RUN_DIR/export/measure_forms.json" ]; then
  echo "[master] $(date -Is) FATAL: export measure_forms.json missing after stage F"; exit 1
fi

# ── stage G: single-shot gates on the exported artifact ──
# order matters: the main gate reads the OPEN gate set; the g1 S1m runner
# reads the JUDGED corpus c2ce056d — both only AFTER the export exists.
ART="$RUN_DIR/export/int8-dynamic"
if [ ! -f "$RUN_DIR/gate_pass.json" ]; then
  if [ -f "$RUN_DIR/gate_pass.r5-mechanics.json" ]; then
    echo "[master] $(date -Is) removing stale partial r5-mechanics report"
    rm -f "$RUN_DIR/gate_pass.r5-mechanics.json"
  fi
  launch_gate
  echo "[master] $(date -Is) SINGLE-SHOT main gate pass (llm thresholds = recalibrated 0.90/0.75)"
  "$PY_GATE" training/embed_r8/gate_r8.py --artifact-dir "$ART" --out "$RUN_DIR/gate_pass.json"
  echo "[master] $(date -Is) gate_r8 code=$?"
fi
if [ ! -f "$RUN_DIR/g1_s1m_report.json" ]; then
  launch_gate
  echo "[master] $(date -Is) SINGLE-SHOT g1 S1m pass (judged c2ce056d, production mechanics)"
  "$PY_GATE" training/embed_r4/gate_s1m_candidate.py \
    --artifact "$ART/model.onnx" --out "$RUN_DIR/g1_s1m_report.json"
  echo "[master] $(date -Is) g1 S1m code=$?"
fi
echo "[master] $(date -Is) chain complete — executor reads gate_pass.json + g1_s1m_report.json, no re-rolls"
