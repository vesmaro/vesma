#!/usr/bin/env bash
# Regenerate gRPC stubs from federation/proto/*.proto.
# Source of truth: federation/proto/*.proto (contract-first, vesma-mesh Phase 3).
# Output: federation/gen/python/ (gitignored — regenerate after any proto change).
#
# gencode guard (W2 stitch fixes, vesma-mesh#20): the generated *_pb2.py
# files embed the protobuf version of the protoc that produced them
# ("Protobuf Python Version: X.Y.Z" header). At import time
# google.protobuf raises VersionError when the *installed runtime* is
# older than that gencode (protobuf ≥ 5.27 cross-version guarantee). A
# protoc newer than the venv runtime therefore produces stubs that fail
# on import with a confusing VersionError instead of a clear message —
# this exact mismatch broke vesmaro.mesh_client imports on 2026-09-17
# (gencode 6.33.5 vs runtime 5.29.6).
#
# The guard below does NOT guess the protoc→gencode mapping (it is not
# linear: libprotoc 29.0 emits gencode 5.29.x). Instead it generates into
# a temp dir, reads the embedded "Protobuf Python Version" header — the
# ground truth — and compares its MAJOR against the installed runtime
# major. Only a matching generation is moved into federation/gen/python;
# on mismatch the script fails loudly and the existing stubs stay valid.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python}"
GEN_DIR="federation/gen/python"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "${TMP_DIR}"' EXIT

# --- generate into a temp dir (never leaves broken stubs in place) ------

"$PYTHON" -m grpc_tools.protoc \
  -I federation/proto \
  --python_out="${TMP_DIR}" \
  --grpc_python_out="${TMP_DIR}" \
  --pyi_out="${TMP_DIR}" \
  federation/proto/federation.proto \
  federation/proto/agent_gateway.proto \
  federation/proto/mnemos_core_api.proto

# --- gencode guard: embedded gencode major == protobuf runtime major ----

read -r GENCODE_MAJOR RUNTIME_MAJOR < <("$PYTHON" - "${TMP_DIR}" <<'PYEOF'
import re
import sys
from pathlib import Path

import google.protobuf

tmp = Path(sys.argv[1])
header_re = re.compile(r"Protobuf Python Version: (\d+)\.(\d+)\.(\d+)")

gencode = None
for pb2 in sorted(tmp.glob("*_pb2.py")):
    m = header_re.search(pb2.read_text(encoding="utf-8"))
    if m:
        gencode = int(m.group(1))
        break

if gencode is None:
    print(f"ERROR: no 'Protobuf Python Version' header found in {tmp}/*_pb2.py",
          file=sys.stderr)
    sys.exit(1)

print(gencode, int(google.protobuf.__version__.split(".")[0]))
PYEOF
)

if [ "${GENCODE_MAJOR}" -ne "${RUNTIME_MAJOR}" ]; then
  cat >&2 <<EOF
ERROR: protobuf gencode/runtime mismatch — refusing to install stubs.

  embedded gencode major (generated stubs): ${GENCODE_MAJOR}.x
  protobuf runtime major (installed):       ${RUNTIME_MAJOR}.x

Stubs with gencode ${GENCODE_MAJOR}.x raise
google.protobuf.runtime_version.VersionError at import time when the
runtime is older (protobuf ≥ 5.27 cross-version guarantee). Fix the
environment first, e.g.:

  uv sync --extra dev    # align grpc_tools with the pinned protobuf
  # or: pip install "protobuf==<gencode-major>.*" matching grpc_tools

Nothing was written to ${GEN_DIR}/ — the existing stubs remain importable.
Rationale: header of this script; vesma-mesh#20.
EOF
  exit 1
fi

echo "gencode guard OK: generated gencode ${GENCODE_MAJOR}.x == protobuf runtime ${RUNTIME_MAJOR}.x"

# --- atomically install the validated stubs ------------------------------

mkdir -p "${GEN_DIR}"
# Mirror a real regeneration: remove the previous generated set first so
# files dropped from the protos do not linger as stale stubs.
rm -f "${GEN_DIR}"/federation_pb2.py "${GEN_DIR}"/federation_pb2_grpc.py \
      "${GEN_DIR}"/federation_pb2.pyi "${GEN_DIR}"/mnemos_core_api_pb2.py \
      "${GEN_DIR}"/mnemos_core_api_pb2_grpc.py "${GEN_DIR}"/mnemos_core_api_pb2.pyi \
      "${GEN_DIR}"/agent_gateway_pb2.py "${GEN_DIR}"/agent_gateway_pb2_grpc.py \
      "${GEN_DIR}"/agent_gateway_pb2.pyi
mv "${TMP_DIR}"/federation_pb2.py "${TMP_DIR}"/federation_pb2_grpc.py \
   "${TMP_DIR}"/federation_pb2.pyi "${TMP_DIR}"/mnemos_core_api_pb2.py \
   "${TMP_DIR}"/mnemos_core_api_pb2_grpc.py "${TMP_DIR}"/mnemos_core_api_pb2.pyi \
   "${TMP_DIR}"/agent_gateway_pb2.py "${TMP_DIR}"/agent_gateway_pb2_grpc.py \
   "${TMP_DIR}"/agent_gateway_pb2.pyi \
   "${GEN_DIR}/"
echo "regenerated federation/gen/python/ from federation/proto/*.proto"