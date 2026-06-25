"""Map MongoDB conversation documents to trajectories (testing shim)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from yggdrasil.domain.enums import StepKind, TrajectoryStatus
from yggdrasil.domain.models import EffortLedger, EffortTotals, Progress, Step, Trajectory


@dataclass
class MappedTrajectory:
    trajectory: Trajectory
    steps: list[Step]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, dict) and "$date" in raw:
        return _parse_dt(raw["$date"])
    if isinstance(raw, (int, float)):
        # ms or seconds heuristic
        ts = float(raw)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _oid_str(doc_id: Any) -> str:
    if isinstance(doc_id, dict) and "$oid" in doc_id:
        return str(doc_id["$oid"])
    return str(doc_id)


def _message_text(msg: dict[str, Any]) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and block.get("text"):
                    parts.append(str(block["text"]))
                elif "text" in block:
                    parts.append(str(block["text"]))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content or "")


def _infer_status(messages: list[dict[str, Any]]) -> TrajectoryStatus:
    for msg in reversed(messages):
        if msg.get("role") != "assistant":
            continue
        text = _message_text(msg).lower()
        if any(w in text for w in ("error", "failed", "failure", "exception")):
            return TrajectoryStatus.FAIL
        if any(w in text for w in ("done", "completed", "success", "fixed", "works")):
            return TrajectoryStatus.SUCCESS
        break
    return TrajectoryStatus.PARTIAL


def _build_effort(doc: dict[str, Any]) -> EffortLedger:
    usage = doc.get("usage") or {}
    if not usage:
        # try last message usage
        for msg in reversed(doc.get("messages") or []):
            if isinstance(msg, dict) and msg.get("usage"):
                usage = msg["usage"]
                break
    if not usage:
        return EffortLedger()
    tin = usage.get("input_tokens") or usage.get("prompt_tokens")
    tout = usage.get("output_tokens") or usage.get("completion_tokens")
    if tin is None and tout is None:
        return EffortLedger()
    return EffortLedger(
        totals=EffortTotals(
            llm_tokens_in=int(tin) if tin is not None else None,
            llm_tokens_out=int(tout) if tout is not None else None,
        )
    )


def map_mongo_conversation_doc(doc: dict[str, Any]) -> MappedTrajectory:
    """Map a claude_conversations.conversations-style document."""
    external_id = _oid_str(doc.get("_id", "unknown"))
    metadata = doc.get("metadata") or {}
    title = str(metadata.get("title") or "untitled").strip()
    project = str(metadata.get("project") or "").strip()
    tags = metadata.get("tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    tags = [str(t) for t in tags]

    messages: list[dict[str, Any]] = list(doc.get("messages") or [])
    first_user = ""
    for msg in messages:
        if msg.get("role") == "user":
            first_user = _message_text(msg).strip()
            break

    task_parts = [title]
    if first_user:
        task_parts.append(first_user[:2000])
    task_text = "\n\n".join(task_parts)

    # scaffold: project + early assistant/tool hints (not full transcript)
    scaffold_parts: list[str] = []
    if project:
        scaffold_parts.append(f"project: {project}")
    hint_count = 0
    for msg in messages[:12]:
        role = msg.get("role")
        if role == "assistant":
            text = _message_text(msg).strip()
            if text:
                scaffold_parts.append(f"assistant_hint: {text[:400]}")
                hint_count += 1
        elif role == "tool":
            text = _message_text(msg).strip()
            if text:
                scaffold_parts.append(f"tool_hint: {text[:200]}")
                hint_count += 1
        if hint_count >= 4:
            break
    if not scaffold_parts:
        scaffold_parts.append("scaffold: imported conversation (no early hints)")
    scaffold_text = "\n".join(scaffold_parts)

    created_at = _parse_dt(metadata.get("created_at") or metadata.get("createdAt")) or _utcnow()
    updated_at = _parse_dt(metadata.get("updated_at") or metadata.get("updatedAt")) or created_at
    status = _infer_status(messages)
    effort = _build_effort(doc)

    external_refs = {
        "source": "mongo",
        "db": "claude_conversations",
        "collection": "conversations",
        "id": external_id,
    }

    traj_id = f"mongo-{external_id}"
    progress = Progress(
        phase="imported",
        summary=f"imported from mongo conversation {external_id}",
        steps_count=len(messages),
        last_step_summary=_message_text(messages[-1])[:200] if messages else None,
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
        role = msg.get("role", "other")
        text = _message_text(msg).strip()
        recorded = _parse_dt(msg.get("created_at") or msg.get("timestamp")) or created_at

        tool_calls = msg.get("tool_calls") or []
        if tool_calls and role == "assistant":
            for tc in tool_calls:
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

        seq += 1
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
                payload={"role": role, "raw_id": msg.get("id")},
                recorded_at=recorded,
            )
        )

    trajectory = trajectory.model_copy(
        update={"progress": progress.model_copy(update={"steps_count": len(steps)})}
    )
    return MappedTrajectory(trajectory=trajectory, steps=steps)
