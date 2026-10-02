"""Cluster worker — M4: group raw memories by embedding similarity.

Algorithm:
  1. Fetch raw memories (optionally filtered by project/agent).
  2. Embed each memory (or reuse cached embeddings from vectors table).
  3. Build similarity matrix; greedy merge above threshold.
  4. Assign cluster_id to each member; update status → processing.
  5. Return ClusterResult per cluster.

Idempotency: re-running on the same set of raw ids returns the same
cluster_id because the centroid hash seeds the UUID deterministically.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from typing import TYPE_CHECKING

import numpy as np

from vesma.models import ClusterResult, Memory, MemoryStatus, is_quarantined

if TYPE_CHECKING:
    from vesma.manager import MemoryManager

logger = logging.getLogger(__name__)


def _seed_uuid(seed: str) -> str:
    """Deterministic UUID v5 in the Vesma namespace."""
    ns = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # UUID namespace OID
    return str(uuid.uuid5(ns, seed))


def _centroid_hash(centroid: np.ndarray) -> str:
    """Stable hash of a float centroid for deterministic cluster IDs."""
    # Round to 4 decimals for stability across tiny float jitter
    rounded = np.round(centroid, decimals=4)
    payload = rounded.tobytes()
    return hashlib.sha256(payload).hexdigest()[:16]


def cluster_raw_memories(
    mgr: MemoryManager,
    *,
    project: str | None = None,
    agent: str | None = None,
    limit: int = 100,
    similarity_threshold: float = 0.82,
    min_cluster_size: int = 2,
) -> list[ClusterResult]:
    """Group recent raw memories into clusters by embedding similarity.

    Args:
        mgr: MemoryManager instance (provides sqlite, vectors, embedder).
        project: Optional project filter.
        agent: Optional agent filter.
        limit: Max raw memories to consider in one run.
        similarity_threshold: Cosine similarity cutoff for merging (0-1).
        min_cluster_size: Clusters smaller than this are discarded.

    Returns:
        List of ClusterResult objects (may be empty).
    """
    # 1. Fetch raw memories
    raw_rows = mgr.sqlite.list_all(
        limit=limit,
        status=MemoryStatus.RAW,
        project=project,
        agent=agent,
    )
    # ADR-0019 §5: quarantined rows are excluded from every issuance path
    # by the single ``is_quarantined`` predicate; the collapse intake
    # composes the same predicate so a quarantined RAW row can never enter
    # a synthesis cluster (#250 F1). Filtered BEFORE the min_cluster_size
    # check so an excluded row cannot silently satisfy the minimum.
    raw_memories = [m for m in raw_rows if not is_quarantined(m)]
    if len(raw_memories) < min_cluster_size:
        logger.info(
            "cluster: only %s raw memories (< min=%s), skipping",
            len(raw_memories),
            min_cluster_size,
        )
        return []

    # 2. Embed each memory
    embeddings: list[np.ndarray] = []
    valid_memories: list[Memory] = []
    for mem in raw_memories:
        try:
            emb = mgr.embedder.embed(mgr._embedding_text(mem))
            embeddings.append(np.asarray(emb, dtype=np.float32))
            valid_memories.append(mem)
        except Exception as exc:
            logger.warning("cluster: embed failed for %s: %s", mem.id[:8], exc)

    n = len(valid_memories)
    if n < min_cluster_size:
        logger.info("cluster: only %s embeddings valid, skipping", n)
        return []

    # 3. Greedy clustering by cosine similarity
    #    Simple O(n²) greedy merge — sufficient for n ≤ 100.
    unassigned = set(range(n))
    clusters: list[list[int]] = []  # indices into valid_memories

    while unassigned:
        seed = unassigned.pop()
        cluster = [seed]
        seed_vec = embeddings[seed]
        # Normalise once. np.linalg.norm returns np.floating[Any], so we
        # explicitly annotate as float — mypy --strict does not narrow
        # np.floating in equality branches.
        seed_norm: float = float(np.linalg.norm(seed_vec))
        if seed_norm == 0:
            seed_norm = 1.0

        to_remove: set[int] = set()
        for idx in unassigned:
            vec = embeddings[idx]
            vec_norm: float = float(np.linalg.norm(vec))
            if vec_norm == 0:
                vec_norm = 1.0
            sim = float(np.dot(seed_vec, vec) / (seed_norm * vec_norm))
            if sim >= similarity_threshold:
                cluster.append(idx)
                to_remove.add(idx)
        unassigned -= to_remove
        clusters.append(cluster)

    # 4. Build results + update DB
    results: list[ClusterResult] = []
    for cluster in clusters:
        if len(cluster) < min_cluster_size:
            continue

        mems = [valid_memories[i] for i in cluster]
        mem_ids = [m.id for m in mems]
        vecs = [embeddings[i] for i in cluster]
        centroid = np.mean(vecs, axis=0)
        c_hash = _centroid_hash(centroid)
        cluster_id = _seed_uuid(c_hash)

        # Pick representative = closest to centroid
        centroid_norm: float = float(np.linalg.norm(centroid))
        if centroid_norm == 0:
            centroid_norm = 1.0
        closest_idx = max(
            cluster,
            key=lambda idx: float(
                np.dot(embeddings[idx], centroid)
                / (np.linalg.norm(embeddings[idx]) * centroid_norm)
            ),
        )
        rep_id = valid_memories[closest_idx].id

        # Update status → processing
        for mem in mems:
            mem.status = MemoryStatus.PROCESSING
            mem.cluster_id = cluster_id
            mgr.sqlite.save(mem)

        results.append(
            ClusterResult(
                cluster_id=cluster_id,
                memory_ids=mem_ids,
                centroid=centroid.tolist(),
                representative_id=rep_id,
            )
        )
        logger.info(
            "cluster: id=%s size=%s project=%s agent=%s",
            cluster_id[:8],
            len(mem_ids),
            project or "*",
            agent or "*",
        )

    return results
