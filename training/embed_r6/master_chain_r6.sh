#!/usr/bin/env bash
# Round-6 DETACHED master chain: (teacher npz reused) -> train (resume loop) -> selection.
# Runs under setsid/nohup, parented away from any task supervisor — the only
# restart authority is the executor's explicit poll. Training is resumable
# (checkpoints carry optimizer state; schedule state carried by metrics.jsonl).
set -u
RUN_DIR=/var/home/abyss/LABs/Projects/Project-Vesma/vesma/wt/embed-r6-train/training/runs/embed-r6
PY=/var/home/abyss/.venvs/nm-train/bin/python
CORPUS=/var/home/abyss/LABs/Projects/Project-Vesma/vesma-cortex/wt/embed-r5-corpus/data/embed-r5/corpus
WT=/var/home/abyss/LABs/Projects/Project-Vesma/vesma/wt/embed-r6-train
LOG="$RUN_DIR/chain.log"
cd "$WT"
exec >> "$LOG" 2>&1
echo "[master] $(date -Is) master chain start (pid $$)"

# ── stage 0: inputs present ──
if [ ! -f "$RUN_DIR/teacher_vectors.npz" ]; then
  echo "[master] $(date -Is) FATAL: reused teacher npz missing from runs/embed-r6"
  exit 1
fi
if [ ! -f "$RUN_DIR/pairs_r5.json" ]; then
  echo "[master] $(date -Is) FATAL: pair pool missing (build_pair_pool_r6 must run first)"
  exit 1
fi

# ── stage 1: training (resume loop) ──
# launch gate: available RAM >= 4 GB (sequential-heavy-jobs rule; run_config
# prelaunch_estimates.launch_gate). Polls every 120 s; logged per check.
while true; do
  AVAIL_MB=$(free -m | awk '/^Mem:/{print $7}')
  echo "[master] $(date -Is) launch gate: available ${AVAIL_MB} MB (need >= 4096)"
  if [ "$AVAIL_MB" -ge 4096 ]; then break; fi
  sleep 120
done

compute_start_epoch() {
  "$PY" - <<'EOF'
import json
from pathlib import Path
run = Path("training/runs/embed-r6")
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

while true; do
  if [ -f "$RUN_DIR/train_final.json" ]; then break; fi
  if [ -f "$RUN_DIR/STOP" ]; then echo "[master] $(date -Is) STOP flag"; break; fi
  START=$(compute_start_epoch | tail -1)
  echo "[master] $(date -Is) trainer from epoch $START"
  "$PY" training/embed_r6/train_r6.py \
    --corpus-dir "$CORPUS" \
    --teacher-vectors "$RUN_DIR/teacher_vectors.npz" \
    --pairs "$RUN_DIR/pairs_r5.json" \
    --out-dir "$RUN_DIR" \
    --epochs 20 --start-epoch "$START"
  code=$?
  echo "[master] $(date -Is) trainer exited code=$code"
  if [ -f "$RUN_DIR/train_final.json" ]; then break; fi
  if [ "$code" -eq 4 ]; then echo "[master] non-finite loss — not relaunching"; exit 4; fi
  sleep 30
done

# ── stage 2: selection (prereg §3) ──
if [ -f "$RUN_DIR/train_final.json" ]; then
  "$PY" training/embed_r6/select_checkpoint_r6.py --run-dir "$RUN_DIR"
  echo "[master] $(date -Is) selection done"
fi
echo "[master] $(date -Is) chain complete"
