"""Estimate extra disk for segmented experience embeddings (Qdrant + SQLite overhead).

Key insight: **source token volume does not linearly determine vector storage**.
Each embeddable trajectory stores a fixed-size vector per aspect (task + scaffold),
regardless of whether the source session was 1k or 1M tokens. Segmentation multiplies
the *number* of vectors, not their dimension.

Example (default PoC):
  - 2 named vectors per trajectory (task, scaffold)
  - dim = 1024, float32 → 4 KB/vector → 8 KB vectors/trajectory (+ Qdrant payload/index overhead)
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


BYTES_PER_F32 = 4
# Qdrant payload + HNSW graph overhead is workload-dependent; PoC uses a conservative multiplier.
QDRANT_OVERHEAD_FACTOR = 2.5
# SQLite trajectory row + steps (summaries only) — rough average extra per child trajectory
SQLITE_BYTES_PER_TRAJECTORY_BASE = 4_000
SQLITE_BYTES_PER_STEP = 400


@dataclass
class StorageEstimate:
    embed_dim: int
    aspects_per_traj: int
    n_sessions: int
    segments_per_session: float
    embed_parent: bool
    source_tokens_total: int | None
    n_child_trajectories: int
    n_parent_trajectories: int
    n_embedded_trajectories: int
    vector_bytes_raw: int
    vector_bytes_with_overhead: int
    sqlite_bytes_est: int
    total_extra_bytes_est: int
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "embed_dim": self.embed_dim,
            "aspects_per_traj": self.aspects_per_traj,
            "n_sessions": self.n_sessions,
            "segments_per_session": self.segments_per_session,
            "embed_parent": self.embed_parent,
            "source_tokens_total": self.source_tokens_total,
            "n_child_trajectories": self.n_child_trajectories,
            "n_parent_trajectories": self.n_parent_trajectories,
            "n_embedded_trajectories": self.n_embedded_trajectories,
            "vector_bytes_raw": self.vector_bytes_raw,
            "vector_bytes_with_overhead": self.vector_bytes_with_overhead,
            "sqlite_bytes_est": self.sqlite_bytes_est,
            "total_extra_bytes_est": self.total_extra_bytes_est,
            "total_extra_mb_est": round(self.total_extra_bytes_est / (1024 * 1024), 3),
            "total_extra_gb_est": round(self.total_extra_bytes_est / (1024**3), 6),
            "bytes_per_source_token_if_any": (
                round(self.total_extra_bytes_est / self.source_tokens_total, 6)
                if self.source_tokens_total
                else None
            ),
            "notes": self.notes,
        }


def estimate_segmented_storage(
    *,
    n_sessions: int = 10,
    segments_per_session: float = 8.0,
    embed_dim: int = 1024,
    aspects_per_traj: int = 2,
    embed_parent: bool = False,
    source_tokens_total: int | None = 15_000_000,
    avg_steps_per_child: int = 12,
    avg_steps_per_parent: int = 80,
    qdrant_overhead_factor: float = QDRANT_OVERHEAD_FACTOR,
) -> StorageEstimate:
    """Estimate extra disk for segmented experience store (not full transcript mirror)."""
    n_children = int(round(n_sessions * segments_per_session))
    n_parents = n_sessions
    n_embedded = n_children + (n_parents if embed_parent else 0)

    bytes_per_vector = embed_dim * BYTES_PER_F32
    bytes_per_traj_vectors = bytes_per_vector * aspects_per_traj
    vector_raw = n_embedded * bytes_per_traj_vectors
    vector_oh = int(vector_raw * qdrant_overhead_factor)

    sqlite_children = n_children * (
        SQLITE_BYTES_PER_TRAJECTORY_BASE + avg_steps_per_child * SQLITE_BYTES_PER_STEP
    )
    sqlite_parents = n_parents * (
        SQLITE_BYTES_PER_TRAJECTORY_BASE + avg_steps_per_parent * SQLITE_BYTES_PER_STEP
    )
    sqlite_total = sqlite_children + sqlite_parents
    total = vector_oh + sqlite_total

    notes = [
        "Source tokens (e.g. 15M) affect *LLM/API cost at session time*, not vector size directly.",
        "Each embedded trajectory stores fixed-size vectors (dim × aspects × 4 bytes) + payload overhead.",
        "Segmentation increases trajectory *count* (children), not bytes-per-token of the original chat.",
        "Full transcripts stay in Mongo/source; Yggdrasil stores summaries/steps + task/scaffold embed text only.",
        f"Qdrant overhead factor={qdrant_overhead_factor} is approximate (HNSW + payload + WAL); measure live with du.",
        "Re-import/re-embed duplicates points only if ids change; use stable mongo-session-* ids for upserts.",
    ]
    if source_tokens_total:
        notes.append(
            f"If all {source_tokens_total:,} tokens were *re-embedded as text chunks* (doc RAG), "
            "storage would be far larger; experience memory only embeds ~2 short strings per segment."
        )

    return StorageEstimate(
        embed_dim=embed_dim,
        aspects_per_traj=aspects_per_traj,
        n_sessions=n_sessions,
        segments_per_session=segments_per_session,
        embed_parent=embed_parent,
        source_tokens_total=source_tokens_total,
        n_child_trajectories=n_children,
        n_parent_trajectories=n_parents,
        n_embedded_trajectories=n_embedded,
        vector_bytes_raw=vector_raw,
        vector_bytes_with_overhead=vector_oh,
        sqlite_bytes_est=sqlite_total,
        total_extra_bytes_est=total,
        notes=notes,
    )


def format_estimate_report(est: StorageEstimate) -> str:
    d = est.to_dict()
    lines = [
        "=== Segmented experience storage estimate ===",
        f"sessions={est.n_sessions}  segments/session≈{est.segments_per_session}  "
        f"children={est.n_child_trajectories}  parents={est.n_parent_trajectories}",
        f"embed: dim={est.embed_dim} aspects={est.aspects_per_traj}  "
        f"embedded_trajs={est.n_embedded_trajectories}  embed_parent={est.embed_parent}",
        f"source_tokens_total={est.source_tokens_total}",
        "",
        f"vectors (raw f32 only):     {est.vector_bytes_raw:>12,} bytes  "
        f"({est.vector_bytes_raw / (1024**2):.3f} MiB)",
        f"vectors (+Qdrant overhead): {est.vector_bytes_with_overhead:>12,} bytes  "
        f"({est.vector_bytes_with_overhead / (1024**2):.3f} MiB)",
        f"SQLite (traj+steps est):    {est.sqlite_bytes_est:>12,} bytes  "
        f"({est.sqlite_bytes_est / (1024**2):.3f} MiB)",
        f"TOTAL extra (est):          {est.total_extra_bytes_est:>12,} bytes  "
        f"({d['total_extra_mb_est']} MiB / {d['total_extra_gb_est']} GiB)",
        "",
        "Per embedded trajectory (approx):",
        f"  vectors only: {est.embed_dim * BYTES_PER_F32 * est.aspects_per_traj} bytes raw "
        f"(+ overhead factor applied globally)",
        "",
        "Notes:",
    ]
    for n in est.notes:
        lines.append(f"  - {n}")
    return "\n".join(lines)
