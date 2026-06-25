"""MCP tool registration."""
from __future__ import annotations

from typing import Any

from yggdrasil.mcp.app_context import AppContext
from yggdrasil.mcp.serialization import (
    append_step_result,
    error_payload,
    get_trajectory_result,
    search_result,
    trajectory_result,
)
from yggdrasil.services.errors import YggdrasilError

AGENT_GUIDANCE = (
    "Yggdrasil trajectory memory: record agent work as trajectories with concrete effort totals "
    "(no cheap/expensive bands). Use start_trajectory at session start, append_step for progress "
    "(re-embeds only on task/scaffold/checkpoint changes), finalize_trajectory on terminal outcome, "
    "search_strategies before repeating hard work (include_open=true by default for partials), "
    "get_trajectory for full detail, update_trajectory_meta for tags/task/scaffold patches. "
    "Interpret effort numbers in user context; prefer low failure_waste_seconds when relevant."
)


def register_tools(mcp: Any, ctx: AppContext) -> None:
    """Register all six MCP tools on a FastMCP instance."""

    @mcp.tool(description=f"{AGENT_GUIDANCE} Start a new trajectory session.")
    def start_trajectory(
        task_text: str,
        scaffold_text: str,
        domain: str = "coding",
        tags: list[str] | None = None,
        runtime_fingerprint: dict[str, Any] | None = None,
        external_refs: dict[str, Any] | None = None,
        embed_view_version: str = "coding_v1",
    ) -> dict[str, Any]:
        try:
            traj = ctx.session_service.start_trajectory(
                task_text=task_text,
                scaffold_text=scaffold_text,
                domain=domain,
                tags=tags,
                runtime_fingerprint=runtime_fingerprint,
                external_refs=external_refs,
                embed_view_version=embed_view_version,
            )
            return trajectory_result(traj)
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(description=f"{AGENT_GUIDANCE} Append a step to an open/partial trajectory.")
    def append_step(
        trajectory_id: str,
        kind: str,
        summary: str,
        payload: dict[str, Any] | None = None,
        scaffold_update: str | None = None,
        task_update: str | None = None,
        is_checkpoint: bool = False,
        progress: dict[str, Any] | None = None,
        mark_partial: bool = False,
        effort_delta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            result = ctx.session_service.append_step(
                trajectory_id=trajectory_id,
                kind=kind,
                summary=summary,
                payload=payload,
                scaffold_update=scaffold_update,
                task_update=task_update,
                is_checkpoint=is_checkpoint,
                progress=progress,
                mark_partial=mark_partial,
                effort_delta=effort_delta,
            )
            return append_step_result(result["trajectory"], result["step"])
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(description=f"{AGENT_GUIDANCE} Finalize a trajectory with terminal outcome.")
    def finalize_trajectory(
        trajectory_id: str,
        outcome: dict[str, Any],
        effort: dict[str, Any] | None = None,
        runtime_fingerprint: dict[str, Any] | None = None,
        progress: dict[str, Any] | None = None,
        task_text: str | None = None,
        scaffold_text: str | None = None,
    ) -> dict[str, Any]:
        try:
            traj = ctx.session_service.finalize_trajectory(
                trajectory_id=trajectory_id,
                outcome=outcome,
                effort=effort,
                runtime_fingerprint=runtime_fingerprint,
                progress=progress,
                task_text=task_text,
                scaffold_text=scaffold_text,
            )
            return trajectory_result(traj)
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(description=f"{AGENT_GUIDANCE} Search prior strategies by task/scaffold similarity.")
    def search_strategies(
        task: str | None = None,
        scaffold: str | None = None,
        domain: str | None = "coding",
        status_in: list[str] | None = None,
        include_open: bool | None = None,
        tags_any: list[str] | None = None,
        limit: int = 10,
        effort_predicates: list[dict[str, Any]] | None = None,
        runtime_filters: dict[str, Any] | None = None,
        prefer_low_waste: bool = False,
    ) -> dict[str, Any]:
        try:
            hits = ctx.search_service.search_strategies(
                task=task,
                scaffold=scaffold,
                domain=domain,
                status_in=status_in,
                include_open=include_open,
                tags_any=tags_any,
                limit=limit,
                effort_predicates=effort_predicates,
                runtime_filters=runtime_filters,
                prefer_low_waste=prefer_low_waste,
            )
            return search_result(hits)
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(description=f"{AGENT_GUIDANCE} Get a trajectory and its steps.")
    def get_trajectory(trajectory_id: str, include_steps: bool = True) -> dict[str, Any]:
        try:
            result = ctx.session_service.get_trajectory(
                trajectory_id, include_steps=include_steps
            )
            return get_trajectory_result(result["trajectory"], result["steps"])
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(description=f"{AGENT_GUIDANCE} Update trajectory metadata (tags/task/scaffold/refs).")
    def update_trajectory_meta(
        trajectory_id: str,
        tags: list[str] | None = None,
        task_text: str | None = None,
        scaffold_text: str | None = None,
        runtime_fingerprint: dict[str, Any] | None = None,
        external_refs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            traj = ctx.session_service.update_trajectory_meta(
                trajectory_id=trajectory_id,
                tags=tags,
                task_text=task_text,
                scaffold_text=scaffold_text,
                runtime_fingerprint=runtime_fingerprint,
                external_refs=external_refs,
            )
            return trajectory_result(traj)
        except Exception as exc:
            return error_payload(exc)
