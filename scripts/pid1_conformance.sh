#!/usr/bin/env bash
# SL-06 true-container leg (specs/service-lifecycle/v1, checklist SL-06).
#
# Runs src/vesma/service/pid1_probe.py as the PID 1 of a throwaway
# container, SIGTERMs the container (the signal lands on PID 1), and
# asserts the PID1 contract from the captured output + exit code:
# mandatory handlers (graceful stop happened at all), the orphaned
# grandchild reaped with no zombie, children stopped before exit, and
# exit code 0.
#
# Podman resolution order (try in-box first, ONE probe — it is
# historically broken in distroboxes with newuidmap EPERM; the WORKING
# path is the HOST podman via distrobox-host-exec, the same way the box
# itself runs). Override with PODMAN="... cmd ...".
# Image override: VESMA_PID1_IMAGE (default ghcr.io/vesmaro/vesma:latest —
# carries python3 + the supervisor's deps: jsonschema, pyyaml).
#
# The repo is bind-mounted READ-ONLY at /repo; the probe runs from the
# WORKTREE sources (PYTHONPATH=/repo/src precedes the image's installed
# vesma). Container name: vesma-pid1-conformance (always cleaned up).
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
NAME="vesma-pid1-conformance"
IMAGE="${VESMA_PID1_IMAGE:-ghcr.io/vesmaro/vesma:latest}"
PODMAN_LINE="${PODMAN:-podman}"
READY_TIMEOUT_S="${READY_TIMEOUT_S:-120}"

log() { echo "pid1-conformance: $*"; }

if ! $PODMAN_LINE info >/dev/null 2>&1; then
  log "in-box podman probe failed — trying host podman via distrobox-host-exec"
  if command -v distrobox-host-exec >/dev/null 2>&1 \
      && distrobox-host-exec podman info >/dev/null 2>&1; then
    PODMAN_LINE="distrobox-host-exec podman"
  else
    log "FAIL: no working podman (in-box refused; host fallback unavailable)"
    exit 2
  fi
fi
log "using podman: $PODMAN_LINE"

# The bind-mount source must be host-visible (distrobox shares /var/home,
# so the in-box path is normally already the host path). Verify loudly.
if [ "$PODMAN_LINE" != "podman" ] && ! distrobox-host-exec test -d "$REPO/src/vesma"; then
  log "FAIL: $REPO is not visible on the HOST (non-shared home?) — cannot bind-mount"
  exit 2
fi

cleanup() { $PODMAN_LINE rm -f "$NAME" >/dev/null 2>&1 || true; }

if ! $PODMAN_LINE inspect --format '{{.Id}}' "$IMAGE" >/dev/null 2>&1; then
  log "image $IMAGE not cached — pulling (needs network once)"
  $PODMAN_LINE pull "$IMAGE"
fi

TRANSCRIPT="$(mktemp /tmp/vesma-pid1-transcript.XXXXXX)"
trap 'cleanup; rm -f "$TRANSCRIPT"' EXIT

log "starting container (image=$IMAGE, repo mounted read-only at /repo)"
$PODMAN_LINE run -d --name "$NAME" \
  -v "$REPO:/repo:ro" \
  -e PYTHONPATH=/repo/src \
  -e PYTHONDONTWRITEBYTECODE=1 \
  "$IMAGE" python3 -m vesma.service.pid1_probe >/dev/null

log "waiting for the probe READY marker (timeout ${READY_TIMEOUT_S}s)"
ready=""
deadline=$(( $(date +%s) + READY_TIMEOUT_S ))
while [ "$(date +%s)" -lt "$deadline" ]; do
  if $PODMAN_LINE logs "$NAME" 2>/dev/null | grep -q VESMA_PID1_PROBE_READY; then
    ready=1
    break
  fi
  if [ "$($PODMAN_LINE inspect --format '{{.State.Status}}' "$NAME" 2>/dev/null)" != "running" ]; then
    break  # probe died before becoming ready — fall through to the verdict
  fi
  sleep 1
done
if [ -z "$ready" ]; then
  $PODMAN_LINE logs "$NAME" > "$TRANSCRIPT" 2>&1 || true
  log "FAIL: probe never became ready; transcript tail:"
  tail -20 "$TRANSCRIPT" >&2
  exit 3
fi

sleep 0.5  # let the READY line fully flush before the signal
log "SIGTERM the container PID 1"
if [ "$($PODMAN_LINE inspect --format '{{.State.Status}}' "$NAME" 2>/dev/null)" = "running" ]; then
  $PODMAN_LINE kill --signal=SIGTERM "$NAME"
fi
EXIT_CODE="$($PODMAN_LINE wait "$NAME")"
$PODMAN_LINE logs "$NAME" > "$TRANSCRIPT" 2>&1 || true

SUMMARY_LINE="$(grep VESMA_PID1_SUMMARY "$TRANSCRIPT" | tail -1 | sed 's/^VESMA_PID1_SUMMARY //')"
if [ -z "$SUMMARY_LINE" ]; then
  log "FAIL: no summary line in the transcript (exit code $EXIT_CODE); tail:"
  tail -20 "$TRANSCRIPT" >&2
  exit 4
fi

# The verdict itself is python-checked (no jq dependency): exit code 0,
# pid1 asserted inside the container, child stopped+gone, no zombies,
# orphan reaped before the stop, journal carries the graceful order.
if printf '%s\n' "$EXIT_CODE" "$SUMMARY_LINE" | python3 -c '
import json, sys
exit_code = int(sys.stdin.readline().strip())
summary = json.loads(sys.stdin.readline())
summary["container_exit_code"] = exit_code
ok = (
    exit_code == 0
    and summary.get("ok") is True
    and summary.get("pid1") is True
    and summary.get("child_gone") is True
    and summary.get("child_final_state") == "stopped"
    and summary.get("zombies") == []
    and summary.get("orphan_reaped_before_stop") is True
    and any("to=stopped" in line for line in summary.get("journal", []))
)
print(json.dumps(summary, indent=1))
sys.exit(0 if ok else 1)
'; then
  log "SL06_CONTAINER_LEG PASS exit_code=$EXIT_CODE"
  log "transcript tail (journal + summary):"
  tail -12 "$TRANSCRIPT"
else
  log "FAIL: PID1 contract violated (container exit code $EXIT_CODE); summary above"
  exit 5
fi
