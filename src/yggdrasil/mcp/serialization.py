"""MCP response serialization helpers."""
from __future__ import annotations

from typing import Any

from yggdrasil.domain.models import SearchHit, Step, Trajectory
from yggdrasil.services.errors import YggdrasilError


def error_payload(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, YggdrasilError):
        return {"error": {"code": exc.code, "message": exc.message}}
    return {"error": {"code": "error", "message": str(exc)}}


def trajectory_to_dict(traj: Trajectory) -> dict[str, Any]:
    return traj.model_dump(mode="json")


def step_to_dict(step: Step) -> dict[str, Any]:
    return step.model_dump(mode="json")


def search_hit_to_dict(hit: SearchHit) -> dict[str, Any]:
    return hit.model_dump(mode="json")


def trajectory_result(traj: Trajectory) -> dict[str, Any]:
    return {"trajectory": trajectory_to_dict(traj)}


def get_trajectory_result(traj: Trajectory, steps: list[Step]) -> dict[str, Any]:
    return {
        "trajectory": trajectory_to_dict(traj),
        "steps": [step_to_dict(s) for s in steps],
    }


def append_step_result(traj: Trajectory, step: Step) -> dict[str, Any]:
    return {
        "trajectory": trajectory_to_dict(traj),
        "step": step_to_dict(step),
    }


def search_result(
    hits: list[SearchHit],
    *,
    query_task: str | None = None,
    prefer_low_waste: bool = True,
) -> dict[str, Any]:
    from yggdrasil.services.retrieval_gates import group_ranked_by_range, rank_experience_hits

    ranked = rank_experience_hits(
        hits, query_task=query_task, prefer_low_waste=prefer_low_waste
    )
    return {
        "hits": [search_hit_to_dict(h) for h in hits],
        "count": len(hits),
        # Agent-facing structured table (recency × success × relevance)
        "ranked": ranked,
        "by_range": group_ranked_by_range(ranked),
        "presentation": {
            "format": "experience_table_v1",
            "rank_order": ["outcome_success", "recency", "relevance_score", "low_waste"],
            "ranges": ["today", "week", "month", "older", "unknown"],
            "agent_instruction": (
                "Present ranked[] as a markdown table (range | owner | agent_id | outcome | "
                "trajectory_id | task preview | score). Prefer by_range buckets when the user "
                "asks time-based questions. Always name owners for human follow-up."
            ),
        },
    }
