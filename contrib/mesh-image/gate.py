# mesh#44 field-presence gate — run INSIDE the built image, pre-push.
# Required (fail build if absent): ListMemoriesRequest.resume_cursor,
# ListMemoriesResponse.cursor, federation PullRequest.cursor; W3 (ADR-0018-T
# §3) — the MnemosCore.ValidateAgentToken RPC in the gencode and the
# VesmaCoreServicer._agent_data_gate method on the shipped src.
# Informational: MetadataRecord.origin_peer (S2, not on main yet),
# agent_gateway_pb2 stubs (shipped since the W3 protoc set).
import importlib.util
import sys

sys.path.insert(0, "/usr/local/lib/python3.12/federation/gen/python")
import federation_pb2 as fed
import mnemos_core_api_pb2 as core


def fields(msg_cls):
    return [f.name for f in msg_cls.DESCRIPTOR.fields]


def service_rpcs(pb2_module, service_name):
    service = pb2_module.DESCRIPTOR.services_by_name.get(service_name)
    return list(service.methods_by_name) if service else []


checks = {
    "ListMemoriesRequest.resume_cursor": "resume_cursor" in fields(core.ListMemoriesRequest),
    "ListMemoriesResponse.cursor": "cursor" in fields(core.ListMemoriesResponse),
    "federation.PullRequest.cursor": "cursor" in fields(fed.PullRequest),
    "MnemosCore.ValidateAgentToken (W3)": "ValidateAgentToken" in service_rpcs(core, "MnemosCore"),
    "federation.MetadataRecord.origin_peer (S2, optional)": "origin_peer"
    in fields(fed.MetadataRecord),
    "federation.MetadataRecord.source_peer (main analog)": "source_peer"
    in fields(fed.MetadataRecord),
    "agent_gateway_pb2 stubs shipped (W3 protoc set)": importlib.util.find_spec("agent_gateway_pb2")
    is not None,
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'absent'}  {name}")

# Exercise the crash path itself: mesh_server must import against the
# shipped stubs and the servicer module must resolve the descriptors.
import vesma.mesh_server as vesma_mesh  # noqa: E402  (runtime import path; AttributeError = stale stubs)

print("PASS  import vesma.mesh_server (site-packages/vesma/mesh_server.py)")

# W3 src check (#398): the agent data gate must be ON the shipped servicer.
# An image whose gencode has ValidateAgentToken but whose src predates the
# data gate would pass every field check yet serve ungated data RPCs.
gate_check = "VesmaCoreServicer._agent_data_gate (W3)"
checks[gate_check] = hasattr(vesma_mesh.mesh_server.MnemosCoreServicer, "_agent_data_gate")
print(f"{'PASS' if checks[gate_check] else 'absent'}  {gate_check}")

required = [
    "ListMemoriesRequest.resume_cursor",
    "ListMemoriesResponse.cursor",
    "federation.PullRequest.cursor",
    "MnemosCore.ValidateAgentToken (W3)",
    gate_check,
]
missing = [n for n in required if not checks[n]]
if missing:
    print("GATE-FAIL:", ", ".join(missing))
    sys.exit(1)
print("GATE-OK")
