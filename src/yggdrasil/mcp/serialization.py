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


def search_result(hits: list[SearchHit]) -> dict[str, Any]:
    return {"hits": [search_hit_to_dict(h) for h in hits], "count": len(hits)}
