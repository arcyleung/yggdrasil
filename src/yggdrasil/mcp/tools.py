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
from yggdrasil.services.principal_context import get_principal

AGENT_GUIDANCE = (
    "Yggdrasil org experience memory (not doc RAG). Before work that is uncertain to succeed or has "
    "large research/setup overhead, call search_strategies (search_experience) to discover whether "
    "other agents in the organization already tried similar goals—learn from their outcomes, effort, "
    "and artifacts; always surface hit owner/agent_id/team so the user can follow up in person with "
    "the people behind those trajectories. Write trajectories with external_refs "
    "owner/agent_id/team/workspace and artifacts ({kind, path_or_url}) so others can find your work. "
    "search_mode=lab for org/team discovery when owner is unknown; agent mode for strict gates. "
    "get_trajectory for shortlisted depth; prefer low failure_waste_seconds."
)


def _tool_principal(ctx: AppContext):
    """Prefer process-bound principal; fall back to contextvar."""
    return ctx.principal if ctx.principal is not None else get_principal()


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
        artifacts: list[dict[str, Any]] | None = None,
        embed_view_version: str = "coding_v1",
    ) -> dict[str, Any]:
        try:
            refs = dict(external_refs or {})
            if artifacts:
                refs.setdefault("experience_grade", True)
            traj = ctx.session_service.start_trajectory(
                task_text=task_text,
                scaffold_text=scaffold_text,
                domain=domain,
                tags=tags,
                runtime_fingerprint=runtime_fingerprint,
                external_refs=refs,
                artifacts=artifacts,
                embed_view_version=embed_view_version,
                principal=_tool_principal(ctx),
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
                principal=_tool_principal(ctx),
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
                principal=_tool_principal(ctx),
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
        owner: str | None = None,
        agent_id: str | None = None,
        team: str | None = None,
        workspace: str | None = None,
        require_artifacts: bool | None = None,
        experience_grade_only: bool | None = None,
        search_mode: str = "agent",
        include_archive: bool = False,
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
                owner=owner,
                agent_id=agent_id,
                team=team,
                workspace=workspace,
                require_artifacts=require_artifacts,
                experience_grade_only=experience_grade_only,
                search_mode=search_mode,
                include_archive=include_archive,
                principal=_tool_principal(ctx),
            )
            payload = search_result(hits)
            gate = ctx.search_service.last_gate_result
            if gate is not None:
                payload["gate_warnings"] = list(gate.warnings)
                payload["gate_dropped_count"] = len(gate.dropped)
            return payload
        except Exception as exc:
            return error_payload(exc)

    @mcp.tool(description=f"{AGENT_GUIDANCE} Get a trajectory and its steps.")
    def get_trajectory(trajectory_id: str, include_steps: bool = True) -> dict[str, Any]:
        try:
            result = ctx.session_service.get_trajectory(
                trajectory_id,
                include_steps=include_steps,
                principal=_tool_principal(ctx),
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
        artifacts: list[dict[str, Any]] | None = None,
        merge_artifacts: bool = True,
    ) -> dict[str, Any]:
        try:
            traj = ctx.session_service.update_trajectory_meta(
                trajectory_id=trajectory_id,
                tags=tags,
                task_text=task_text,
                scaffold_text=scaffold_text,
                runtime_fingerprint=runtime_fingerprint,
                external_refs=external_refs,
                artifacts=artifacts,
                merge_artifacts=merge_artifacts,
                principal=_tool_principal(ctx),
            )
            return trajectory_result(traj)
        except Exception as exc:
            return error_payload(exc)
