# 0006. Use local ONNX embeddings (privacy + offline by default)

*Historical artifact — English only.*

- **Status**: Accepted
- **Date**: 2026-05-15
- **Deciders**: abyss, GCW Tech Lead

## Context

Vesma is a personal memory server. Embeddings are computed for every `published`
memory and every search query. The team had to choose between:

1. **Local ONNX** (e.g. all-MiniLM-L6-v2, downloaded at first run).
2. **Server-side embedding API** (OpenAI, Voyage, Cohere).
3. **Local SentenceTransformers** (heavier Python deps, includes PyTorch).

## Decision

Vesma v1 ships with **local ONNX embeddings by default**, with a swappable provider
interface (`EmbeddingProvider`) so users can switch to Ollama, SentenceTransformers,
or a server-side API later.

The default ONNX model is `sentence-transformers/all-MiniLM-L6-v2` (or
`onnx-models/all-MiniLM-L6-v2-onnx` for direct ONNX). The model is downloaded via
`huggingface_hub.hf_hub_download` at first run.

**Lockdown (M15)**: the `revision=` is pinned to a commit SHA in `config.py` to
prevent supply-chain attacks against the model file. This is bandit B615 hardening
captured in `docs/security.md`.

## Consequences

**Positive**

- **Privacy**: embeddings never leave the host. The vault stays local.
- **Offline**: no network required for synthesis or search (after the initial model
  download).
- **Cost**: no per-embedding charge.
- **Latency**: 5–20ms per embedding on a modern CPU; no API round-trip.

**Negative**

- First-run download is ~25MB. Acceptable; one-time cost.
- ONNX runtime requires a native binary (`onnxruntime`). Pre-built wheels exist for
  Linux, macOS, Windows.
- Model quality (MiniLM, 384-dim) is lower than server-side APIs (1536-dim OpenAI).
  This affects recall precision on subtle queries.

**Neutral**

- Provider abstraction in `src/vesma/embeddings/__init__.py` exposes 4 providers.
  Users can opt in to a different one in `config.yaml`.

## Alternatives considered

- **OpenAI `text-embedding-3-small` server-side.** Rejected for v1: privacy +
  cost + offline constraints. Available as opt-in provider.
- **SentenceTransformers with PyTorch.** Rejected: 800MB+ PyTorch dependency is
  too heavy for a "memory server" use case. ONNX achieves the same accuracy with
  a 50MB runtime.
- **Ollama embeddings (`nomic-embed-text`).** Accepted as opt-in provider; default
  remains ONNX because Ollama requires a running daemon.

## References

- `PLAN.md` §"Further considerations" Q1 (Lazy embeddings)
- `src/vesma/embeddings/__init__.py` — 4 providers, ONNX default
- `docs/security.md` — supply-chain hardening (B615)
- `src/vesma/config.py` — `hf_revision` pinned SHA
