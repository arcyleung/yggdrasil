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
from yggdrasil.adapters.importers.mongo_segment import segment_conversation_ir
from yggdrasil.adapters.importers.segment_schema import SegmentedSession, TrajectorySegment
from yggdrasil.domain.enums import StepKind, TrajectoryStatus
from yggdrasil.domain.models import EffortLedger, EffortTotals, Outcome, Progress, Step, Trajectory
from yggdrasil.services.retrieval_gates import clean_task_text_for_embed


@dataclass
class MappedTrajectory:
    trajectory: Trajectory
    steps: list[Step]


@dataclass
class MappedSessionHierarchy:
    """Parent session trajectory + embeddable child segments."""

    parent: MappedTrajectory
    children: list[MappedTrajectory]
    segmented: SegmentedSession


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


def _outcome_str_to_status(outcome: str) -> TrajectoryStatus:
    o = (outcome or "unknown").lower()
    if o in ("success", "ok", "done"):
        return TrajectoryStatus.SUCCESS
    if o in ("failed", "fail", "error"):
        return TrajectoryStatus.FAIL
    if o in ("aborted", "abort", "cancelled"):
        return TrajectoryStatus.ABORTED
    if o in ("partial", "in_progress", "wip"):
        return TrajectoryStatus.PARTIAL
    return TrajectoryStatus.PARTIAL


def _build_steps_for_messages(
    traj_id: str,
    messages: list[IRMessage],
    *,
    created_at: datetime,
    seq_offset: int = 0,
) -> list[Step]:
    """Reuse legacy step extraction on a message slice by temporarily mapping via IR."""
    # Lightweight: call through pseudo-IR legacy for slice by building temporary steps
    pseudo = ConversationIR(
        session_id=None,
        request_id=traj_id,
        model=None,
        created_at=created_at,
        updated_at=created_at,
        title=None,
        project=None,
        tags=[],
        system_text="",
        tool_names=[],
        messages=messages,
        usage=None,
        source_shape="fixture_v1",
        raw_external={},
    )
    mapped = map_conversation_ir_legacy(pseudo)
    steps: list[Step] = []
    for s in mapped.steps:
        steps.append(
            s.model_copy(
                update={
                    "trajectory_id": traj_id,
                    "seq": s.seq + seq_offset,
                }
            )
        )
    return steps


def map_session_hierarchy(
    ir: ConversationIR,
    segmented: SegmentedSession | None = None,
    *,
    embed_parent: bool = False,
    caller_segments: list[TrajectorySegment] | list[dict[str, Any]] | None = None,
) -> MappedSessionHierarchy:
    """Map IR + hierarchical segments → parent trajectory + child trajectories.

    Children are primary embed targets. Parent stores lineage/milestones; embed optional.
    """
    segmented = segmented or segment_conversation_ir(ir, caller_segments=caller_segments)
    session_id = segmented.session_id
    parent_id = f"mongo-session-{session_id}"
    created_at = ir.created_at or _utcnow()
    updated_at = ir.updated_at or created_at

    parent_task = clean_task_text_for_embed(
        segmented.parent_task or ir.title or "imported session"
    ) or "imported session"
    parent_scaffold = segmented.parent_scaffold or "imported session scaffold"
    milestones = [
        f"seg-{i:04d}:{seg.task[:80]}" for i, seg in enumerate(segmented.segments)
    ]
    parent_status = TrajectoryStatus.PARTIAL
    statuses = [_outcome_str_to_status(s.outcome) for s in segmented.segments]
    if statuses and all(s == TrajectoryStatus.SUCCESS for s in statuses):
        parent_status = TrajectoryStatus.SUCCESS
    elif any(s == TrajectoryStatus.FAIL for s in statuses):
        parent_status = TrajectoryStatus.PARTIAL

    parent_refs = {
        "source": "mongo",
        "db": "claude_conversations",
        "collection": "conversations",
        "session_id": session_id,
        "kind": "session_parent",
        "id": f"session:{session_id}",
        "request_id": ir.request_id,
        "request_ids": ir.raw_external.get("request_ids"),
        "embed_target": embed_parent,
        "segment_count": len(segmented.segments),
        "segmentation_source": segmented.source,
        "segments_json": [s.to_dict() for s in segmented.segments],
    }
    parent_tags = list(
        dict.fromkeys([*ir.tags, "mongo_import", "session_parent", "has_segments"])
    )
    parent_traj = Trajectory(
        id=parent_id,
        domain="coding",
        status=parent_status,
        task_text=parent_task[:4000],
        scaffold_text=parent_scaffold[:4000],
        tags=parent_tags,
        external_refs=parent_refs,
        progress=Progress(
            phase="imported_session",
            summary=f"session {session_id} with {len(segmented.segments)} segments",
            steps_count=len(ir.messages),
            milestones=milestones,
        ),
        outcome=Outcome(
            terminal_status=parent_status,
            summary=f"imported session rollup ({len(segmented.segments)} segments)",
            signals={"source": "mongo", "model": ir.model},
            goal_satisfied=parent_status == TrajectoryStatus.SUCCESS,
        ),
        effort=_build_effort_from_ir(ir),
        embed_view_version="coding_v1",
        created_at=created_at,
        updated_at=updated_at,
        finalized_at=updated_at,
    )
    parent_steps = _build_steps_for_messages(parent_id, ir.messages, created_at=created_at)
    parent_traj = parent_traj.model_copy(
        update={
            "progress": parent_traj.progress.model_copy(
                update={"steps_count": len(parent_steps)}
            )
        }
    )
    parent_mapped = MappedTrajectory(trajectory=parent_traj, steps=parent_steps)

    children: list[MappedTrajectory] = []
    for idx, seg in enumerate(segmented.segments):
        child_id = f"mongo-session-{session_id}-seg-{idx:04d}"
        ext_id = f"session:{session_id}:seg:{idx}"
        slice_msgs = ir.messages[seg.start_idx : seg.end_idx + 1]
        status = _outcome_str_to_status(seg.outcome)
        scaffold = seg.scaffold_hint or parent_scaffold
        child_refs = {
            "source": "mongo",
            "db": "claude_conversations",
            "collection": "conversations",
            "session_id": session_id,
            "kind": "session_segment",
            "id": ext_id,
            "parent_trajectory_id": parent_id,
            "segment_index": idx,
            "segment_kind": seg.segment_kind,
            "start_idx": seg.start_idx,
            "end_idx": seg.end_idx,
            "embed_target": True,
            "segmentation_source": segmented.source,
        }
        child_tags = list(
            dict.fromkeys(
                [
                    *ir.tags,
                    "mongo_import",
                    "session_segment",
                    f"seg_kind:{seg.segment_kind}",
                    f"outcome:{seg.outcome}",
                ]
            )
        )
        child_task = clean_task_text_for_embed(seg.task) or seg.task[:4000]
        child_traj = Trajectory(
            id=child_id,
            domain="coding",
            status=status,
            task_text=child_task[:4000],
            scaffold_text=scaffold[:4000],
            tags=child_tags,
            external_refs=child_refs,
            progress=Progress(
                phase="imported_segment",
                summary=f"segment {idx} msgs [{seg.start_idx},{seg.end_idx}]",
                steps_count=len(slice_msgs),
                last_step_summary=(
                    ir_message_text(slice_msgs[-1])[:200] if slice_msgs else None
                ),
            ),
            outcome=Outcome(
                terminal_status=status,
                summary=seg.outcome if seg.outcome != "unknown" else seg.task[:200],
                signals={
                    "source": "mongo",
                    "segment_index": idx,
                    "start_idx": seg.start_idx,
                    "end_idx": seg.end_idx,
                },
                goal_satisfied=status == TrajectoryStatus.SUCCESS,
                remaining_work=seg.notes,
            ),
            effort=EffortLedger(),  # segment-local effort unknown unless caller supplies
            embed_view_version="coding_v1",
            created_at=created_at,
            updated_at=updated_at,
            finalized_at=updated_at,
        )
        child_steps = _build_steps_for_messages(child_id, slice_msgs, created_at=created_at)
        child_traj = child_traj.model_copy(
            update={
                "progress": child_traj.progress.model_copy(
                    update={"steps_count": len(child_steps)}
                )
            }
        )
        children.append(MappedTrajectory(trajectory=child_traj, steps=child_steps))

    return MappedSessionHierarchy(
        parent=parent_mapped, children=children, segmented=segmented
    )


def map_mongo_session_doc(
    doc: dict[str, Any],
    *,
    caller_segments: list[dict[str, Any]] | None = None,
    embed_parent: bool = False,
) -> MappedSessionHierarchy:
    """Normalize one doc (or canonical session doc) → hierarchical trajectories."""
    ir = normalize_mongo_doc(doc)
    return map_session_hierarchy(
        ir, caller_segments=caller_segments, embed_parent=embed_parent
    )


