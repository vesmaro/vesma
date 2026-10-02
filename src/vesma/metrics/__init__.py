"""Vitals — passive assemble metrics (ADR-0026 phase A).

The metrics.sqlite sidecar: one ``assemble_metrics`` row per assemble
call plus its ``injection_blocks``, written at the boundaries (MCP
``mnemos_assemble_context`` + the ``pre_llm_call`` hook) AFTER the
result exists. The assemble pipeline itself stays write-free — S2
measures that verb directly and the corridor headroom is not eaten.

Guest contract: this package is a convenience for the server, never a
dependency of it — every entry point degrades to a no-op on failure
and the server runs identically with the plane disabled.
"""
