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
    from yggdrasil.services.retrieval_gates import rank_experience_hits

    ranked = rank_experience_hits(
        hits, query_task=query_task, prefer_low_waste=prefer_low_waste
    )
    return {
        "hits": [search_hit_to_dict(h) for h in hits],
        "count": len(hits),
        # Agent-facing structured table (success × event-time recency × relevance)
        "ranked": ranked,
        "presentation": {
            "format": "experience_table_v2",
            "rank_order": [
                "outcome_success",
                "event_time_recency",
                "relevance_score",
                "low_waste",
            ],
            "timestamp_field": "at",
            "timestamp_semantics": (
                "ISO-8601 UTC experience event time (prefer finalized_at / occurred_at; "
                "not re-index updated_at). Bin client-side relative to agent clock."
            ),
            "client_range_bins": ["today", "week", "month", "older", "unknown"],
            "client_range_rules": (
                "today: age <= 1d; week: <= 7d; month: <= 30d; else older; missing at → unknown"
            ),
            "agent_instruction": (
                "Present ranked[] as a markdown table: at (ISO) | client_range | owner | "
                "agent_id | outcome | trajectory_id | task preview | score. Compute "
                "client_range from ranked[].at vs now (today/week/month/older/unknown). "
                "Do not expect server-side range or by_range. Always name owners for "
                "human follow-up."
            ),
        },
    }
