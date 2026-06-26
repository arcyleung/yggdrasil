"""Reciprocal Rank Fusion helper (pure ranking math)."""
from __future__ import annotations


def reciprocal_rank_fuse(rank_lists: dict[str, list[str]], *, k: int = 60) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for _aspect, ordered_ids in rank_lists.items():
        for rank, doc_id in enumerate(ordered_ids, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
