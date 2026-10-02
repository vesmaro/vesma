"""Deterministic lexical embedder for the golden evaluation set.

DESIGN DECISION (reported per W4 task brief): the production default
embedder (``ChromaDefaultProvider``) downloads an ~80 MB ONNX MiniLM
model on first use — unavailable in offline/sandboxed CI and not
bit-reproducible across model versions. A golden baseline must be
reproducible byte-for-byte, so the golden harness swaps the embedder for
a fully deterministic lexical feature-hashing embedder:

- tokenise on ``[a-z0-9]+`` (lowercased),
- hash every unigram and bigram with BLAKE2b (platform-independent,
  unlike Python's salted ``hash()``) into a fixed number of buckets with
  a sign flip from a second digest byte,
- L2-normalise the resulting vector.

This gives the vector leg real, content-correlated signal (bag-of-words
cosine) while remaining identical across runs, machines and Python
versions. What the golden baseline therefore measures is the RETRIEVAL
PIPELINE (RRF fusion, A9 project predicate, status gating, ranking) —
not MiniLM embedding quality. A MiniLM-based baseline would be a
separate, machine-pinned recording exercise and is out of scope for the
D5 baseline (report it, don't hide it).
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache
from itertools import pairwise

import numpy as np

from vesma.embeddings import EmbeddingProvider

_DIM = 256
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _bucket(term: str) -> tuple[int, int]:
    """Map a term to a (bucket, sign) pair via BLAKE2b — deterministic."""
    digest = hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest()
    bucket = int.from_bytes(digest[:4], "big") % _DIM
    sign = 1 if digest[4] & 1 else -1
    return bucket, sign


@lru_cache(maxsize=8192)
def _embed_cached(text: str) -> tuple[float, ...]:
    tokens = _TOKEN_RE.findall(text.lower())
    vec: np.ndarray = np.zeros(_DIM, dtype=np.float32)
    for term in tokens:
        bucket, sign = _bucket(term)
        vec[bucket] += sign
    for a, b in pairwise(tokens):
        bucket, sign = _bucket(a + "\x1f" + b)
        vec[bucket] += 0.5 * sign
    norm = float(np.linalg.norm(vec))
    if norm > 0.0:
        vec = vec / norm
    return tuple(float(x) for x in vec)


class LexicalHashEmbedder(EmbeddingProvider):  # type: ignore[misc, unused-ignore]
    """Feature-hashing embedder — the golden set's deterministic vector leg."""

    def embed(self, text: str) -> list[float]:
        return list(_embed_cached(text[:4096]))

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    @property
    def dimension(self) -> int:
        return _DIM
