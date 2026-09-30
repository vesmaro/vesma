"""Recall layer for Vesma — hybrid FTS5 + vector + RRF.

Submodules:
  fts          — SQLite FTS5 full-text search
  vector       — ChromaDB semantic search (published-only)
  rrf          — Reciprocal Rank Fusion of FTS + vector result lists
  agent_recall — M3: per-agent pre-filter before search

File-context boost (M8): when current_file_path is provided, rules with
matching applyTo: glob are pinned to the top of recall results.
"""
