#!/usr/bin/env bash
# Round-8 DETACHED master chain: stage-1 (FT base + select + teacher
# precompute) -> stage-2 (student KD + selection). Runs under setsid/nohup,
# parented away from any task supervisor — the only restart authority is
# the executor's explicit poll. Both trainings are resumable (checkpoints
# carry optimizer state; stage-2 schedule state carried by metrics.jsonl).
#
# EXPORT AND GATES ARE NOT IN THIS CHAIN: after stage-2 selection the
# executor verifies selection.json, then runs export_r8 + the single-shot
# gate pass manually (single-shot discipline: one measured pass, no
# automation races).
set -u
RUN_DIR=/var/home/abyss/LABs/Projects/Project-Vesma/vesma/wt/embed-r8-train/training/runs/embed-r8
S1_DIR="$RUN_DIR/ft-stage1"   # stage-1 artifacts SEPARATE from stage-2 (metrics collision guard)
PY_S1=/var/home/abyss/.venvs/nm-eg-r8/bin/python
PY_S2=/var/home/abyss/.venvs/nm-train/bin/python
CORPUS=/var/home/abyss/LABs/Projects/Project-Vesma/vesma-cortex/wt/embed-r5-corpus/data/embed-r7/corpus
WT=/var/home/abyss/LABs/Projects/Project-Vesma/vesma/wt/embed-r8-train
LOG="$RUN_DIR/chain.log"
mkdir -p "$S1_DIR"
cd "$WT"
exec >> "$LOG" 2>&1
echo "[master] $(date -Is) master chain start (pid $$)"

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
  echo "[master] $(date -Is) stage-1 FT from epoch $START"
  "$PY_S1" training/embed_r8/ft_base_r8.py \
    --pairs "$RUN_DIR/pairs_r8.json" \
    --out-dir "$S1_DIR" \
    --epochs 4 --start-epoch "$START"
  code=$?
  echo "[master] $(date -Is) stage-1 trainer exited code=$code"
  if [ "$code" -eq 4 ]; then echo "[master] non-finite loss — not relaunching"; exit 4; fi
  sleep 30
done

# ── stage B: stage-1 selection ──
if [ -f "$S1_DIR/train_final.json" ] && [ ! -f "$S1_DIR/ft_selection.json" ]; then
  "$PY_S1" training/embed_r8/select_ft_r8.py --run-dir "$S1_DIR"
  echo "[master] $(date -Is) stage-1 selection done"
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
  echo "[master] $(date -Is) stage-2 trainer from epoch $START"
  "$PY_S2" training/embed_r8/train_r8.py \
    --corpus-dir "$CORPUS" \
    --teacher-vectors "$RUN_DIR/teacher_vectors.npz" \
    --out-dir "$RUN_DIR" \
    --epochs 20 --start-epoch "$START"
  code=$?
  echo "[master] $(date -Is) trainer exited code=$code"
  if [ -f "$RUN_DIR/train_final.json" ]; then break; fi
  if [ "$code" -eq 4 ]; then echo "[master] non-finite loss — not relaunching"; exit 4; fi
  sleep 30
done

# ── stage E: stage-2 selection (r6 §3 rule, r8 recalibrated proxy) ──
if [ -f "$RUN_DIR/train_final.json" ]; then
  "$PY_S2" training/embed_r8/select_checkpoint_r8.py --run-dir "$RUN_DIR"
  echo "[master] $(date -Is) stage-2 selection done"
fi
echo "[master] $(date -Is) chain complete — executor verifies selection, then export + single-shot gates"
