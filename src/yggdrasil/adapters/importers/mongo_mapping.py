"""Map MongoDB conversation documents to trajectories (testing shim)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from yggdrasil.adapters.importers.mongo_normalize import (
    ConversationIR,
    IRMessage,
    ir_message_text,
    normalize_mongo_doc,
)
from yggdrasil.domain.enums import StepKind, TrajectoryStatus
from yggdrasil.domain.models import EffortLedger, EffortTotals, Progress, Step, Trajectory


@dataclass
class MappedTrajectory:
    trajectory: Trajectory
    steps: list[Step]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _infer_status_from_messages(messages: list[IRMessage]) -> TrajectoryStatus:
    for msg in reversed(messages):
        if msg.role != "assistant":
            continue
        text = ir_message_text(msg).lower()
        if any(w in text for w in ("error", "failed", "failure", "exception")):
            return TrajectoryStatus.FAIL
        if any(w in text for w in ("done", "completed", "success", "fixed", "works")):
            return TrajectoryStatus.SUCCESS
        break
    return TrajectoryStatus.PARTIAL


def _build_effort_from_ir(ir: ConversationIR) -> EffortLedger:
    usage = ir.usage
    if usage is None:
        return EffortLedger()
    if (
        usage.input_tokens is None
        and usage.output_tokens is None
        and usage.cache_creation_input_tokens is None
        and usage.cache_read_input_tokens is None
    ):
        return EffortLedger()
    notes = usage.notes
    return EffortLedger(
        totals=EffortTotals(
            llm_tokens_in=usage.input_tokens,
            llm_tokens_out=usage.output_tokens,
        ),
        notes=notes,
    )


def _anthropic_tool_use_steps(
    traj_id: str,
    msg: IRMessage,
    *,
    seq_start: int,
    recorded: datetime,
) -> tuple[list[Step], int]:
    """Emit tool_call steps from Anthropic content blocks; return steps and next seq."""
    steps: list[Step] = []
    seq = seq_start
    content = msg.content
    if not isinstance(content, list):
        return steps, seq
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use":
            continue
        seq += 1
        name = str(block.get("name") or "tool")
        inp = block.get("input")
        input_keys: list[str] = []
        if isinstance(inp, dict):
            input_keys = list(inp.keys())[:32]
        steps.append(
            Step(
                trajectory_id=traj_id,
                seq=seq,
                kind=StepKind.TOOL_CALL,
                summary=f"tool_call: {name}",
                payload={
                    "tool_use_id": block.get("id"),
                    "name": name,
                    "input_keys": input_keys,
                    "role": "assistant",
                },
                recorded_at=recorded,
            )
        )
    return steps, seq


def _anthropic_tool_result_steps(
    traj_id: str,
    msg: IRMessage,
    *,
    seq_start: int,
    recorded: datetime,
) -> tuple[list[Step], int]:
    steps: list[Step] = []
    seq = seq_start
    content = msg.content
    if not isinstance(content, list):
        return steps, seq
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        seq += 1
        tr_content = block.get("content")
        summary = ""
        if isinstance(tr_content, str):
            summary = tr_content[:500]
        elif isinstance(tr_content, list):
            parts: list[str] = []
            for sub in tr_content:
                if isinstance(sub, dict) and sub.get("text"):
                    parts.append(str(sub["text"]))
            summary = "\n".join(parts)[:500]
        steps.append(
            Step(
                trajectory_id=traj_id,
                seq=seq,
                kind=StepKind.TOOL_RESULT,
                summary=summary or f"tool_result:{block.get('tool_use_id', '')}",
                payload={
                    "role": "user",
                    "tool_use_id": block.get("tool_use_id"),
                    "is_error": block.get("is_error"),
                },
                recorded_at=recorded,
            )
        )
    return steps, seq


def _has_block_type(msg: IRMessage, block_type: str) -> bool:
    if not isinstance(msg.content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == block_type for b in msg.content
    )


def map_conversation_ir_legacy(ir: ConversationIR) -> MappedTrajectory:
    """Legacy single-trajectory map (one IR → one trajectory). Used by Phase 1 tests/import."""
    external_id = ir.request_id
    title = (ir.title or "untitled").strip()
    project = (ir.project or "").strip()
    tags = list(ir.tags)
    if ir.source_shape == "proxy_log" and "proxy_log" not in tags:
        tags.append("proxy_log")

    messages = ir.messages
    first_user = ""
    for msg in messages:
        if msg.role == "user":
            # Prefer user text blocks; skip pure tool_result turns for task_text
            if _has_block_type(msg, "tool_result") and not _has_block_type(msg, "text"):
                continue
            first_user = ir_message_text(msg).strip()
            if first_user:
                break

    task_parts = [title] if title and title != "untitled" else []
    if first_user:
        task_parts.append(first_user[:2000])
    if not task_parts:
        task_parts.append("untitled imported conversation")
    task_text = "\n\n".join(task_parts)

    scaffold_parts: list[str] = []
    if project:
        scaffold_parts.append(f"project: {project}")
    if ir.model:
        scaffold_parts.append(f"model: {ir.model}")
    if ir.tool_names:
        scaffold_parts.append(f"tools: {', '.join(ir.tool_names[:24])}")
    if ir.system_text:
        scaffold_parts.append(f"system: {ir.system_text[:600]}")

    hint_count = 0
    for msg in messages[:12]:
        if msg.role == "assistant":
            text = ir_message_text(msg).strip()
            if text:
                scaffold_parts.append(f"assistant_hint: {text[:400]}")
                hint_count += 1
        elif msg.role == "tool" or _has_block_type(msg, "tool_result"):
            text = ir_message_text(msg).strip()
            if text:
                scaffold_parts.append(f"tool_hint: {text[:200]}")
                hint_count += 1
        if hint_count >= 4:
            break
    if not scaffold_parts:
        scaffold_parts.append("scaffold: imported conversation (no early hints)")
    scaffold_text = "\n".join(scaffold_parts)

    created_at = ir.created_at or _utcnow()
    updated_at = ir.updated_at or created_at
    status = _infer_status_from_messages(messages)
    effort = _build_effort_from_ir(ir)

    external_refs = dict(ir.raw_external)
    external_refs.setdefault("source", "mongo")
    external_refs.setdefault("db", "claude_conversations")
    external_refs.setdefault("collection", "conversations")
    external_refs["id"] = external_id
    if ir.session_id:
        external_refs["session_id"] = ir.session_id

    traj_id = f"mongo-{external_id}"
    progress = Progress(
        phase="imported",
        summary=f"imported from mongo conversation {external_id}",
        steps_count=len(messages),
        last_step_summary=ir_message_text(messages[-1])[:200] if messages else None,
    )

    trajectory = Trajectory(
        id=traj_id,
        domain="coding",
        status=status,
        task_text=task_text,
        scaffold_text=scaffold_text,
        tags=tags,
        external_refs=external_refs,
        progress=progress,
        effort=effort,
        embed_view_version="coding_v1",
        created_at=created_at,
        updated_at=updated_at,
        finalized_at=updated_at if status != TrajectoryStatus.OPEN else None,
    )

    steps: list[Step] = []
    seq = 0
    for msg in messages:
        recorded = msg.timestamp or created_at
        role = msg.role

        # Fixture OpenAI-style tool_calls on assistant
        if msg.tool_calls and role == "assistant":
            for tc in msg.tool_calls:
                seq += 1
                name = ""
                if isinstance(tc, dict):
                    fn = tc.get("function") or {}
                    name = fn.get("name") or tc.get("name") or "tool"
                steps.append(
                    Step(
                        trajectory_id=traj_id,
                        seq=seq,
                        kind=StepKind.TOOL_CALL,
                        summary=f"tool_call: {name}",
                        payload={"tool_call": tc},
                        recorded_at=recorded,
                    )
                )
            text = ir_message_text(msg).strip()
            if text:
                seq += 1
                steps.append(
                    Step(
                        trajectory_id=traj_id,
                        seq=seq,
                        kind=StepKind.THOUGHT,
                        summary=text[:500],
                        payload={"role": "assistant"},
                        recorded_at=recorded,
                    )
                )
            continue

        # Anthropic proxy: tool_use / tool_result blocks
        if role == "assistant" and _has_block_type(msg, "tool_use"):
            extra, seq = _anthropic_tool_use_steps(
                traj_id, msg, seq_start=seq, recorded=recorded
            )
            steps.extend(extra)
            text = ir_message_text(msg).strip()
            # only pure text (ir_message_text may include only text blocks)
            text_only = ""
            if isinstance(msg.content, list):
                parts = [
                    str(b.get("text"))
                    for b in msg.content
                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                ]
                text_only = "\n".join(parts).strip()
            elif isinstance(msg.content, str):
                text_only = msg.content.strip()
            if text_only:
                seq += 1
                steps.append(
                    Step(
                        trajectory_id=traj_id,
                        seq=seq,
                        kind=StepKind.THOUGHT,
                        summary=text_only[:500],
                        payload={"role": "assistant"},
                        recorded_at=recorded,
                    )
                )
            continue

        if role == "user" and _has_block_type(msg, "tool_result"):
            extra, seq = _anthropic_tool_result_steps(
                traj_id, msg, seq_start=seq, recorded=recorded
            )
            steps.extend(extra)
            # optional accompanying user text goal
            if isinstance(msg.content, list):
                text_parts = [
                    str(b.get("text"))
                    for b in msg.content
                    if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
                ]
                if text_parts:
                    seq += 1
                    steps.append(
                        Step(
                            trajectory_id=traj_id,
                            seq=seq,
                            kind=StepKind.NOTE,
                            summary="\n".join(text_parts)[:500],
                            payload={"role": "user", "block_types": ["text", "tool_result"]},
                            recorded_at=recorded,
                        )
                    )
            continue

        seq += 1
        text = ir_message_text(msg).strip()
        if role == "user":
            kind = StepKind.NOTE
        elif role == "assistant":
            kind = StepKind.THOUGHT
        elif role == "tool":
            kind = StepKind.TOOL_RESULT
        else:
            kind = StepKind.OTHER
        steps.append(
            Step(
                trajectory_id=traj_id,
                seq=seq,
                kind=kind,
                summary=(text[:500] if text else f"{role} message"),
                payload={"role": role, "raw_id": msg.msg_id},
                recorded_at=recorded,
            )
        )

    trajectory = trajectory.model_copy(
        update={"progress": progress.model_copy(update={"steps_count": len(steps)})}
    )
    return MappedTrajectory(trajectory=trajectory, steps=steps)


def map_mongo_conversation_doc(doc: dict[str, Any]) -> MappedTrajectory:
    """Map a claude_conversations.conversations-style document (dual-shape via normalizer)."""
    ir = normalize_mongo_doc(doc)
    return map_conversation_ir_legacy(ir)


