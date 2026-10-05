#!/usr/bin/env bash
# image-publish.sh — THE container image phase of the vesma release train.
#
# Owner directive 2026-10-05 (card vesma-ghcr-5x-parity): the container
# image must appear SYNCHRONOUSLY with every release. There is NO skip
# path here — no --no-image, no silent step-down on a missing builder.
# A failed image build, smoke, push, or verification is a HARD failure
# of whatever invoked this script (the canonical train entry is
# `scripts/pypi-publish.sh --publish`, which orders this phase around
# the PyPI upload; see the runbook for the full shape).
#
# Subcommands:
#   preflight [--strict]  cheap gates only: builder present, GHCR_TOKEN
#                         present (--strict), version three-way gate
#                         (tag == pyproject == CHANGELOG section).
#                         Non-strict exits 3 on warn-class gaps
#                         (builder/token missing, HEAD off-tag) so
#                         check-mode callers can WARN instead of die;
#                         a real version mismatch always exits 2.
#   build                 version gates -> regenerate gRPC stubs ->
#                         image build (VERSION + latest tags) ->
#                         smoke `vesma --version` inside the container.
#   push                  login (fresh authfile, shredded after) ->
#                         skopeo explicit-transport push of :VERSION and
#                         :latest (podman/docker fallback) -> anonymous
#                         pull-flow verification (tags/list + REMOTE
#                         digest equality :VERSION == :latest as the
#                         BINDING check; a local skopeo --digestfile,
#                         when present, must match too — on the fallback
#                         legs there is no local digestfile and equality
#                         is verified remote-only with a loud WARN).
#                         Any failure after a successful PyPI upload
#                         prints the RELEASE INCOMPLETE banner with the
#                         exact catch-up command and exits 1.
#   catch-up              alias of push — the recovery entry printed in
#                         the banner; pushes the already-built image and
#                         re-verifies, touching nothing else.
#   help                  this text.
#
# No-skip contract: every failure below is terminal. The ONLY accepted
# "not pushed" state is a non-zero exit — never a green summary with a
# missing image.
#
# Version gates (all three must hold or exit 2):
#   VG1 HEAD is exactly on a tag vX.Y.Z
#   VG2 tag version == pyproject.toml version
#   VG3 CHANGELOG.md contains a `## [X.Y.Z]` section for that version
#       (the changelog freeze is part of the release, so an image may
#       not exist for a version the changelog does not vouch for)
#
# Smoke gate: `vesma --version` run INSIDE the built container must
# print exactly `vesma $VERSION` on its first line.
#
# Credentials: GHCR_TOKEN env var — a classic PAT with repo+write:packages
# (read it on demand; never commit or log it). GHCR_USER defaults to
# "vesmaro" (the registry identifies the token, not the username).
#
# Builder/runner/skopeo overrides (each a full command prefix, needed in
# distrobox setups where podman lives on the host):
#   VESMA_IMAGE_BUILDER (default: podman -> buildah -> docker)
#   VESMA_IMAGE_RUNNER  (default: podman -> docker; smoke is mandatory)
#   VESMA_IMAGE_SKOPEO  (default: skopeo — must share storage with the
#   builder, so override it together with VESMA_IMAGE_BUILDER)
#
# Transport/auth coupling: the skopeo source transport follows the
# builder (containers-storage: for podman/buildah, docker-daemon: for
# docker — containers-storage cannot see docker-daemon images). docker
# has no --authfile: its credentials are redirected into the ephemeral
# auth dir via DOCKER_CONFIG, which stays exported for every docker
# call in this script (an unexported DOCKER_CONFIG makes `docker push`
# read ~/.docker/config.json and die unauthenticated).
#
# Field note (2026-10-05, v5.5.0 push): `podman push` to ghcr.io failed
# with "Requesting bearer token: 403 Forbidden" on the first blob-reuse
# check while the SAME authfile worked through
# `skopeo copy containers-storage:... docker://...`. Push therefore goes
# through skopeo explicit transports first, podman/docker push second.
#
# See: docs/en/admin/runbooks/pypi-publish.md (canonical train shape),
# scripts/local-release.sh (deprecated fallback, delegates here).

set -euo pipefail

IMAGE="ghcr.io/vesmaro/vesma"
REPO_PATH="${IMAGE#ghcr.io/}"

die() { echo "ERROR: $*" >&2; exit 2; }

# --- arguments ----------------------------------------------------------------

MODE="${1:-help}"
STRICT=false
case "$MODE" in
  preflight)
    case "${2:-}" in
      "") ;;
      --strict) STRICT=true ;;
      *) die "preflight takes at most one argument: --strict (got: ${2:-})" ;;
    esac
    [[ $# -le 2 ]] || die "preflight takes at most one argument: --strict (got extra: ${*:3})"
    ;;
  build|push|catch-up)
    [[ $# -eq 1 ]] || die "subcommand $MODE takes no arguments (got: ${*:2})"
    ;;
  help|-h)
    awk 'NR>1 && /^set -/{exit} NR>1 {sub(/^#( |$)/,""); print}' "$0"
    exit 0
    ;;
  *) die "unknown subcommand: $MODE (use: preflight|build|push|catch-up|help)" ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT_DIR"

# --- builder / runner / skopeo discovery ---------------------------------------

BUILDER_KIND=""; declare -a BUILDER_CMD=() RUNNER_CMD=() SKOPEO_CMD=()

discover_tools() {
  if [[ -n "${VESMA_IMAGE_BUILDER:-}" ]]; then
    read -r -a BUILDER_CMD <<< "$VESMA_IMAGE_BUILDER"
  else
    if command -v podman >/dev/null 2>&1; then BUILDER_CMD=(podman)
    elif command -v buildah >/dev/null 2>&1; then BUILDER_CMD=(buildah)
    elif command -v docker >/dev/null 2>&1; then BUILDER_CMD=(docker)
    else BUILDER_CMD=()
    fi
  fi
  BUILDER_KIND="${BUILDER_CMD[0]:-}"
  [[ -n "$BUILDER_KIND" ]] || return 1

  if [[ -n "${VESMA_IMAGE_RUNNER:-}" ]]; then
    read -r -a RUNNER_CMD <<< "$VESMA_IMAGE_RUNNER"
  else
    if command -v podman >/dev/null 2>&1; then RUNNER_CMD=(podman)
    elif command -v docker >/dev/null 2>&1; then RUNNER_CMD=(docker)
    else RUNNER_CMD=()
    fi
  fi

  if [[ -n "${VESMA_IMAGE_SKOPEO:-}" ]]; then
    read -r -a SKOPEO_CMD <<< "$VESMA_IMAGE_SKOPEO"
  else
    if command -v skopeo >/dev/null 2>&1; then SKOPEO_CMD=(skopeo); else SKOPEO_CMD=(); fi
  fi
  return 0
}

# --- version gates --------------------------------------------------------------

# usage: version_gates <hard|preflight>
#   hard      : no-tag is terminal (build/push)
#   preflight : no-tag exits 3 (warn-class); a real mismatch is terminal
version_gates() {
  local mode="$1"
  if git describe --tags --exact-match HEAD >/dev/null 2>&1; then
    TAG="$(git describe --tags --exact-match HEAD)"
  else
    # no-tag is fatal for build/push and for preflight --strict; plain
    # check-mode preflight degrades to the warn-class exit 3
    local no_tag_fatal=false
    [[ "$mode" == "hard" ]] && no_tag_fatal=true
    $STRICT && no_tag_fatal=true
    if $no_tag_fatal; then
      die "VG1: HEAD is not exactly on a tag vX.Y.Z — checkout the release tag first"
    fi
    echo "WARN [VG1]: HEAD not on a tag — image version gates not evaluable here"
    return 3
  fi
  VERSION="${TAG#v}"
  PYV=$(grep -m1 '^version' pyproject.toml | cut -d'"' -f2)
  [[ "$VERSION" == "$PYV" ]] || die "VG2: tag $TAG != pyproject.toml $PYV — fix the version before any image exists"
  if ! grep -qE "^## \[$VERSION\]" CHANGELOG.md; then
    die "VG3: CHANGELOG.md has no '## [$VERSION]' section — freeze the changelog before the image build"
  fi
  echo "VG ok: tag:$TAG == pyproject:$PYV == CHANGELOG:[$VERSION]"
  return 0
}

# --- shared: registry auth (fresh authfile, always shredded) --------------------

AUTH_DIR=""

auth_cleanup() {
  if [[ -n "$AUTH_DIR" && -d "$AUTH_DIR" ]]; then
    shred -u "$AUTH_DIR"/* 2>/dev/null || rm -f "$AUTH_DIR"/*
    rmdir "$AUTH_DIR" 2>/dev/null || true
    AUTH_DIR=""
  fi
}
trap auth_cleanup EXIT
trap 'auth_cleanup; exit 130' INT
trap 'auth_cleanup; exit 143' TERM

registry_login() {
  [[ -n "${GHCR_TOKEN:-}" ]] || die "GHCR_TOKEN is not set — the image cannot be pushed without it (classic PAT, repo+write:packages; runbook: docs/en/admin/runbooks/pypi-publish.md)"
  AUTH_DIR="$(mktemp -d "${TMPDIR:-/tmp}/vesma-image-auth.XXXXXX")"
  chmod 700 "$AUTH_DIR"
  local AUTHFILE="$AUTH_DIR/auth.json"
  printf '{}' > "$AUTHFILE"   # pre-seeded: podman login rejects an EMPTY existing authfile
  chmod 600 "$AUTHFILE"
  local user="${GHCR_USER:-vesmaro}"
  case "$BUILDER_KIND" in
    docker)
      # docker has no --authfile: point DOCKER_CONFIG at AUTH_DIR so the
      # login writes config.json there. The file is duplicated to auth.json
      # (same format) so the skopeo --dest-authfile leg is authenticated
      # too, and DOCKER_CONFIG stays set for every docker call below —
      # without it `docker push` reads ~/.docker/config.json and 401s.
      printf '%s' "$GHCR_TOKEN" | DOCKER_CONFIG="$AUTH_DIR" "${BUILDER_CMD[@]}" login ghcr.io -u "$user" --password-stdin >/dev/null
      cp "$AUTH_DIR/config.json" "$AUTH_DIR/auth.json"
      chmod 600 "$AUTH_DIR/config.json" "$AUTH_DIR/auth.json"
      AUTHFILE="$AUTH_DIR/auth.json"
      export DOCKER_CONFIG="$AUTH_DIR"
      ;;
    *)  # podman / buildah: --authfile (pre-seed lesson: {} not empty)
      printf '%s' "$GHCR_TOKEN" | "${BUILDER_CMD[@]}" login --authfile "$AUTHFILE" --password-stdin -u "$user" ghcr.io >/dev/null
      ;;
  esac
  echo "→ ghcr.io login ok (authfile: $AUTHFILE, shredded on exit)"
}

# push one tag; on success write the manifest digest into $2 (skopeo only —
# the podman/docker fallback legs have no digestfile equivalent, so the
# verify step below treats the remote as the source of truth)
push_tag() {
  local src="$1" digfile="$2" rc
  # the skopeo source transport must match where the builder put the image:
  # podman/buildah build into containers-storage, docker into its daemon —
  # containers-storage: cannot see docker-daemon images
  local src_transport="containers-storage"
  if [[ "$BUILDER_KIND" == "docker" ]]; then src_transport="docker-daemon"; fi
  if [[ "${#SKOPEO_CMD[@]}" -gt 0 ]]; then
    set +e
    "${SKOPEO_CMD[@]}" copy --all --dest-authfile "$AUTH_DIR/auth.json" \
      --digestfile "$digfile" \
      "$src_transport:$src" "docker://$src"
    rc=$?
    set -e
    if [[ $rc -ne 0 ]]; then
      echo "→ skopeo copy failed (rc=$rc) — falling back to a direct push" >&2
    fi
  else
    rc=1
  fi
  if [[ $rc -ne 0 ]]; then
    case "$BUILDER_KIND" in
      # DOCKER_CONFIG is exported by registry_login (docker branch) — the
      # push must read the ephemeral config.json, not ~/.docker/config.json
      docker) set +e; "${BUILDER_CMD[@]}" push "$src"; rc=$?; set -e ;;
      *)      # podman/buildah push: source is the bare image name (a
              # docker:// SOURCE here names a nonexistent local image)
              set +e; "${BUILDER_CMD[@]}" push --authfile "$AUTH_DIR/auth.json" "$src"; rc=$?; set -e ;;
    esac
  fi
  return $rc
}

release_incomplete() {
  local where="$1"
  cat <<BANNER

══════════════════════════════════════════════════════════════════
 RELEASE INCOMPLETE — PyPI carries $VERSION but the container
 image does NOT (failed at: $where).

 The image $IMAGE:$VERSION exists locally, built and smoke-tested
 by this train. Catch up with EXACTLY this once the cause is fixed:

   GHCR_TOKEN=<token> scripts/image-publish.sh catch-up

 (catch-up pushes :$VERSION + :latest and re-runs the anonymous
  pull-flow verification; it touches nothing else. Run it from the
  release-tag checkout — the version gates hard-refuse a moved HEAD)
══════════════════════════════════════════════════════════════════
BANNER
  exit 1
}

# --- preflight ------------------------------------------------------------------

if [[ "$MODE" == "preflight" ]]; then
  STRICT_LABEL=""
  [[ "$STRICT" == true ]] && STRICT_LABEL=" (strict)"
  echo "=== image-publish preflight$STRICT_LABEL ==="
  if ! discover_tools; then
    if $STRICT; then die "no container builder (podman/buildah/docker) — the image phase cannot run; override with VESMA_IMAGE_BUILDER"
    else echo "WARN: no container builder (podman/buildah/docker) — image gates not evaluable here"; exit 3; fi
  fi
  echo "→ builder: ${BUILDER_CMD[*]}  runner: ${RUNNER_CMD[*]:-<none>}  skopeo: ${SKOPEO_CMD[*]:-<none>}"
  [[ "${#RUNNER_CMD[@]}" -gt 0 ]] || { $STRICT && die "no container runner for the mandatory smoke (podman/docker) — set VESMA_IMAGE_RUNNER"; echo "WARN: no runner for the smoke gate"; exit 3; }
  if [[ -z "${GHCR_TOKEN:-}" ]]; then
    if $STRICT; then die "GHCR_TOKEN is not set — publish-time push would fail; export it before the train"
    else echo "WARN: GHCR_TOKEN not set — push-time gate would fail (check mode continues)"; fi
  fi
  version_gates preflight || exit $?    # 0 ok / 3 warn-class no-tag / 2 terminal
  echo "✓ image preflight passed"
  exit 0
fi

# --- build ----------------------------------------------------------------------

if [[ "$MODE" == "build" ]]; then
  version_gates hard
  discover_tools || die "no container builder (podman/buildah/docker) — install one or override with VESMA_IMAGE_BUILDER"
  [[ "${#RUNNER_CMD[@]}" -gt 0 ]] || die "no container runner (podman/docker) for the MANDATORY smoke — install one or set VESMA_IMAGE_RUNNER"

  # gRPC stubs are gitignored and COPYed by the Containerfile — regenerate
  # from the checked-out proto with the release venv (uv.lock pins hold).
  if [[ -d federation/proto ]]; then
    echo "→ regenerating gRPC stubs (scripts/gen-proto.sh)"
    GENPY="${PYTHON:-$ROOT_DIR/.venv/bin/python}"
    [[ -x "$GENPY" ]] || GENPY="$(command -v python3)"
    if ! PYTHON="$GENPY" bash "$SCRIPT_DIR/gen-proto.sh"; then
      die "gen-proto.sh failed — the Containerfile COPYs federation/gen/python; bootstrap the venv with 'uv sync --extra dev' and retry"
    fi
  fi

  echo "→ building $IMAGE:$VERSION (+ :latest) with ${BUILDER_CMD[*]}"
  set +e
  "${BUILDER_CMD[@]}" build -t "$IMAGE:$VERSION" -t "$IMAGE:latest" -f Containerfile .
  rc=$?
  set -e
  [[ $rc -eq 0 ]] || die "image build failed (rc=$rc) — the train refuses to publish anything"

  echo "→ smoke: ${RUNNER_CMD[*]} run --rm $IMAGE:$VERSION vesma --version"
  set +e
  OUT="$("${RUNNER_CMD[@]}" run --rm "$IMAGE:$VERSION" vesma --version 2>&1)"
  rc=$?
  set -e
  [[ $rc -eq 0 ]] || die "smoke failed: container 'vesma --version' exited rc=$rc — output: $OUT"
  FIRST_LINE="$(printf '%s' "$OUT" | head -n1 | tr -d '[:space:]')"
  [[ "$FIRST_LINE" == "vesma$VERSION" ]] || die "smoke mismatch: expected first line 'vesma $VERSION', got '$(printf '%s' "$OUT" | head -n1)'"
  echo "✓ smoke: $(printf '%s' "$OUT" | head -n1)"
  exit 0
fi

# --- push / catch-up --------------------------------------------------------------

# MODE is push|catch-up here
version_gates hard
discover_tools || die "no container builder (podman/buildah/docker) — cannot push without the toolchain that built the image"
registry_login

WHERE=""
declare -a FAILED_STEPS=()

echo "→ pushing $IMAGE:$VERSION"
push_tag "$IMAGE:$VERSION" "$AUTH_DIR/digest-version.txt" || { FAILED_STEPS+=("push :$VERSION"); WHERE="push :$VERSION"; }
if [[ "${#FAILED_STEPS[@]}" -eq 0 ]]; then
  echo "→ pushing $IMAGE:latest"
  push_tag "$IMAGE:latest" "$AUTH_DIR/digest-latest.txt" || { FAILED_STEPS+=("push :latest"); WHERE="push :latest"; }
fi
if [[ "${#FAILED_STEPS[@]}" -gt 0 ]]; then
  release_incomplete "$WHERE"
fi
# anonymous pull-flow verification (no credentials — a user's-eye check).
# The remote is the source of truth: both tags are HEADed anonymously and
# their digests must be EQUAL — that is the binding equality check. A
# local skopeo --digestfile, when present, must ALSO match (strict); on
# the podman/docker fallback legs no digestfile is written and equality
# is verified remote-only (loud WARN below). Every registry request is
# wrapped: a transport failure must produce the RELEASE INCOMPLETE
# banner, not a silent set -e death after a successful PyPI upload.
echo "→ anonymous pull verification of $REPO_PATH"
ANON="$(curl -sf -m 20 "https://ghcr.io/token?scope=repository:$REPO_PATH:pull" | sed -n 's/.*"token": *"\([^"]*\)".*/\1/p')" \
  || release_incomplete "anonymous ghcr token request failed (curl rc=$?)"
[[ -n "$ANON" ]] || release_incomplete "anonymous ghcr token request: empty token"
TAGS="$(curl -sf -m 20 -H "Authorization: Bearer $ANON" "https://ghcr.io/v2/$REPO_PATH/tags/list")" \
  || release_incomplete "tags/list fetch failed (curl rc=$?)"
echo "$TAGS" | grep -q "\"$VERSION\"" || release_incomplete "tags/list lacks \"$VERSION\" (got: $TAGS)"
ACCEPT='application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json,application/vnd.docker.distribution.manifest.list.v2+json,application/vnd.oci.image.index.v1+json'
REMOTE_VERSION="$(curl -sfI -m 20 -H "Authorization: Bearer $ANON" -H "Accept: $ACCEPT" "https://ghcr.io/v2/$REPO_PATH/manifests/$VERSION" | tr -d '\r' | awk 'tolower($1)=="docker-content-digest:"{print $2}')" \
  || release_incomplete "manifest HEAD :$VERSION failed (curl rc=$?)"
REMOTE_LATEST="$(curl -sfI -m 20 -H "Authorization: Bearer $ANON" -H "Accept: $ACCEPT" "https://ghcr.io/v2/$REPO_PATH/manifests/latest" | tr -d '\r' | awk 'tolower($1)=="docker-content-digest:"{print $2}')" \
  || release_incomplete "manifest HEAD :latest failed (curl rc=$?)"
[[ -n "$REMOTE_VERSION" ]] || release_incomplete "remote digest :$VERSION is empty (no docker-content-digest header)"
[[ -n "$REMOTE_LATEST" ]] || release_incomplete "remote digest :latest is empty (no docker-content-digest header)"
[[ "$REMOTE_VERSION" == "$REMOTE_LATEST" ]] || release_incomplete "remote digest equality (:VERSION=$REMOTE_VERSION != :latest=$REMOTE_LATEST)"
D1="$(cat "$AUTH_DIR/digest-version.txt" 2>/dev/null || true)"
D2="$(cat "$AUTH_DIR/digest-latest.txt" 2>/dev/null || true)"
if [[ -n "$D1" && "$D1" != "$REMOTE_VERSION" ]]; then
  release_incomplete "local skopeo digest :$VERSION=$D1 != remote $REMOTE_VERSION"
fi
if [[ -n "$D2" && "$D2" != "$REMOTE_LATEST" ]]; then
  release_incomplete "local skopeo digest :latest=$D2 != remote $REMOTE_LATEST"
fi
if [[ -z "$D1" || -z "$D2" ]]; then
  echo "WARN: local skopeo digestfile absent (non-skopeo push leg) — digest equality was verified REMOTE-ONLY"
fi
echo "✓ anonymous verify: tags/list has $VERSION; digest(:$VERSION) == digest(:latest) == $REMOTE_VERSION${D1:+ (matches local skopeo digestfile)}"
exit 0
