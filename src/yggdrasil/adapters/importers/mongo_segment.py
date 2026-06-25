"""Auto-segment ConversationIR into hierarchical TrajectorySegment list (importer/agent assist)."""
from __future__ import annotations

import re
from typing import Any

from yggdrasil.adapters.importers.mongo_normalize import ConversationIR, IRMessage, ir_message_text
from yggdrasil.adapters.importers.segment_schema import SegmentedSession, TrajectorySegment
from yggdrasil.services.retrieval_gates import clean_task_text_for_embed, task_is_noisy

# H1 goal-shift cues in user text
_GOAL_SHIFT_RE = re.compile(
    r"\b(also|next|instead|new task|ignore previous|now do|switch to|separately)\b",
    re.I,
)
# H3 outcome cues in assistant text
_SUCCESS_RE = re.compile(
    r"\b(done|completed|success|fixed|works|tests pass|all green|resolved)\b", re.I
)
_FAIL_RE = re.compile(
    r"\b(failed|failure|error|exception|blocked|can't|cannot|rate limit|timed out)\b", re.I
)
_PARTIAL_RE = re.compile(r"\b(partial|wip|in progress|remaining|todo|not yet)\b", re.I)

DEFAULT_MAX_SEGMENT_MSGS = 24
MIN_SEGMENT_MSGS = 2


def _user_has_goal_text(msg: IRMessage) -> bool:
    if msg.role != "user":
        return False
    if isinstance(msg.content, list):
        has_text = any(
            isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip()
            for b in msg.content
        )
        only_tool = all(
            not isinstance(b, dict) or b.get("type") in ("tool_result",)
            for b in msg.content
            if isinstance(b, dict)
        ) and not has_text
        if only_tool:
            return False
        return has_text or bool(ir_message_text(msg).strip())
    text = ir_message_text(msg).strip()
    return bool(text)


def _infer_outcome_from_text(text: str) -> str:
    if not text:
        return "unknown"
    if _FAIL_RE.search(text) and not _SUCCESS_RE.search(text):
        return "failed"
    if _SUCCESS_RE.search(text):
        return "success"
    if _PARTIAL_RE.search(text):
        return "partial"
    return "unknown"


def _segment_task_from_span(messages: list[IRMessage], start: int, end: int, fallback: str) -> str:
    candidates: list[str] = []
    for i in range(start, min(end + 1, len(messages))):
        msg = messages[i]
        if msg.role == "user" and _user_has_goal_text(msg):
            t = clean_task_text_for_embed(ir_message_text(msg).strip())
            if t and not task_is_noisy(t):
                return t[:2000]
            if t:
                candidates.append(t)
    for i in range(start, min(end + 1, len(messages))):
        if messages[i].role == "assistant":
            t = clean_task_text_for_embed(ir_message_text(messages[i]).strip())
            if t and not task_is_noisy(t) and len(t) >= 24:
                candidates.append(t[:500])
                break
    fb = clean_task_text_for_embed(fallback or "")
    if candidates:
        # pick shortest non-noisy-ish candidate (prefer concrete user goals)
        candidates.sort(key=lambda s: (task_is_noisy(s), len(s)))
        return candidates[0][:2000]
    if fb and not task_is_noisy(fb):
        return fb[:2000]
    return (fb or "untitled segment")[:2000]


def _segment_outcome_from_span(messages: list[IRMessage], start: int, end: int) -> str:
    for i in range(min(end, len(messages) - 1), start - 1, -1):
        if i < 0:
            break
        if messages[i].role == "assistant":
            return _infer_outcome_from_text(ir_message_text(messages[i]))
    return "unknown"


def _scaffold_hint(ir: ConversationIR, messages: list[IRMessage], start: int, end: int) -> str:
    parts: list[str] = []
    if ir.model:
        parts.append(f"model: {ir.model}")
    if ir.tool_names:
        parts.append(f"tools: {', '.join(ir.tool_names[:16])}")
    tool_uses: list[str] = []
    for i in range(start, min(end + 1, len(messages))):
        msg = messages[i]
        if msg.role != "assistant" or not isinstance(msg.content, list):
            continue
        for b in msg.content:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name"):
                tool_uses.append(str(b["name"]))
        if len(tool_uses) >= 8:
            break
    if tool_uses:
        parts.append(f"segment_tools: {', '.join(dict.fromkeys(tool_uses))}")
    for i in range(start, min(start + 3, end + 1, len(messages))):
        if messages[i].role == "assistant":
            t = ir_message_text(messages[i]).strip()
            if t:
                parts.append(f"plan: {t[:300]}")
                break
    return "\n".join(parts) if parts else (ir.system_text[:400] if ir.system_text else "")


def segment_conversation_ir(
    ir: ConversationIR,
    *,
    max_segment_msgs: int = DEFAULT_MAX_SEGMENT_MSGS,
    min_segment_msgs: int = MIN_SEGMENT_MSGS,
    caller_segments: list[TrajectorySegment] | list[dict[str, Any]] | None = None,
) -> SegmentedSession:
    """Produce hierarchical segments for a normalized conversation.

    If ``caller_segments`` is provided (agent skill output), validate/normalize and use them.
    Otherwise apply deterministic H1/H3/H4 heuristics on message indices.
    """
    messages = ir.messages
    n = len(messages)
    session_id = ir.session_id or ir.request_id
    parent_task = ""
    for msg in messages:
        if msg.role == "user" and _user_has_goal_text(msg):
            parent_task = clean_task_text_for_embed(ir_message_text(msg).strip())[:2000]
            if parent_task and not task_is_noisy(parent_task):
                break
            parent_task = parent_task or ""
    if (not parent_task or task_is_noisy(parent_task)) and ir.title:
        parent_task = clean_task_text_for_embed(ir.title)

    parent_scaffold_parts: list[str] = []
    if ir.project:
        parent_scaffold_parts.append(f"project: {ir.project}")
    if ir.model:
        parent_scaffold_parts.append(f"model: {ir.model}")
    if ir.tool_names:
        parent_scaffold_parts.append(f"tools: {', '.join(ir.tool_names[:24])}")
    if ir.system_text:
        parent_scaffold_parts.append(f"system: {ir.system_text[:600]}")
    parent_scaffold = "\n".join(parent_scaffold_parts) or "imported session"

    if caller_segments is not None:
        segs: list[TrajectorySegment] = []
        for raw in caller_segments:
            if isinstance(raw, TrajectorySegment):
                segs.append(raw)
            else:
                segs.append(TrajectorySegment.from_dict(dict(raw)))
        # clamp end indices
        fixed: list[TrajectorySegment] = []
        for s in segs:
            end = min(s.end_idx, max(n - 1, 0)) if n else s.end_idx
            start = min(s.start_idx, end)
            fixed.append(
                TrajectorySegment(
                    start_idx=start,
                    end_idx=end,
                    task=s.task,
                    outcome=s.outcome,
                    segment_kind=s.segment_kind or "caller",
                    scaffold_hint=s.scaffold_hint or _scaffold_hint(ir, messages, start, end),
                    notes=s.notes,
                    extra=s.extra,
                )
            )
        if not fixed and n > 0:
            fixed = [
                TrajectorySegment(
                    start_idx=0,
                    end_idx=n - 1,
                    task=parent_task or "full session",
                    outcome="unknown",
                    segment_kind="fallback",
                    scaffold_hint=parent_scaffold,
                )
            ]
        return SegmentedSession(
            session_id=session_id,
            segments=fixed,
            parent_task=parent_task or None,
            parent_scaffold=parent_scaffold,
            source="caller",
            extra={"request_id": ir.request_id, "source_shape": ir.source_shape},
        )

    if n == 0:
        return SegmentedSession(
            session_id=session_id,
            segments=[],
            parent_task=parent_task or None,
            parent_scaffold=parent_scaffold,
            source="mongo_auto",
            extra={"request_id": ir.request_id},
        )

    # --- Auto: split points before message i (start of new segment) ---
    boundaries: list[int] = [0]
    for i in range(1, n):
        msg = messages[i]
        if not _user_has_goal_text(msg):
            continue
        text = ir_message_text(msg)
        prev_was_assistant_done = False
        if i > 0 and messages[i - 1].role == "assistant":
            prev_out = _infer_outcome_from_text(ir_message_text(messages[i - 1]))
            prev_was_assistant_done = prev_out in ("success", "failed", "partial")
        goal_shift = bool(_GOAL_SHIFT_RE.search(text)) or prev_was_assistant_done
        # Always treat a new substantial user goal after assistant as potential boundary
        if goal_shift or (i > 0 and messages[i - 1].role == "assistant" and len(text) > 40):
            if i not in boundaries:
                boundaries.append(i)

    # Enforce H4 max window: insert extra boundaries
    extra_bounds: list[int] = []
    for bi, start in enumerate(boundaries):
        end_limit = boundaries[bi + 1] if bi + 1 < len(boundaries) else n
        span = end_limit - start
        if span <= max_segment_msgs:
            continue
        pos = start + max_segment_msgs
        while pos < end_limit:
            # snap to next user goal if possible within window
            snap = pos
            for j in range(pos, min(pos + 4, end_limit)):
                if _user_has_goal_text(messages[j]):
                    snap = j
                    break
            if snap not in boundaries and snap not in extra_bounds and snap < n:
                extra_bounds.append(snap)
            pos += max_segment_msgs
    for b in extra_bounds:
        if b not in boundaries:
            boundaries.append(b)
    boundaries = sorted(set(boundaries))

    segments: list[TrajectorySegment] = []
    for bi, start in enumerate(boundaries):
        end = (boundaries[bi + 1] - 1) if bi + 1 < len(boundaries) else (n - 1)
        if end < start:
            continue
        task = _segment_task_from_span(messages, start, end, parent_task or "segment")
        outcome = _segment_outcome_from_span(messages, start, end)
        kind = "window_split" if (end - start + 1) >= max_segment_msgs else "goal"
        if bi > 0 and _user_has_goal_text(messages[start]):
            kind = "goal"
        segments.append(
            TrajectorySegment(
                start_idx=start,
                end_idx=end,
                task=task,
                outcome=outcome,
                segment_kind=kind,
                scaffold_hint=_scaffold_hint(ir, messages, start, end),
            )
        )

    # Merge tiny segments into neighbor (except first goal shift)
    merged: list[TrajectorySegment] = []
    for seg in segments:
        if (
            merged
            and seg.message_span < min_segment_msgs
            and seg.segment_kind != "goal"
        ):
            prev = merged[-1]
            merged[-1] = TrajectorySegment(
                start_idx=prev.start_idx,
                end_idx=seg.end_idx,
                task=prev.task,
                outcome=seg.outcome if seg.outcome != "unknown" else prev.outcome,
                segment_kind=prev.segment_kind,
                scaffold_hint=prev.scaffold_hint,
                notes=(prev.notes or "") + ";merged_tiny",
            )
        else:
            merged.append(seg)

    if not merged:
        merged = [
            TrajectorySegment(
                start_idx=0,
                end_idx=n - 1,
                task=parent_task or "full session",
                outcome=_segment_outcome_from_span(messages, 0, n - 1),
                segment_kind="fallback",
                scaffold_hint=parent_scaffold,
            )
        ]

    return SegmentedSession(
        session_id=session_id,
        segments=merged,
        parent_task=parent_task or None,
        parent_scaffold=parent_scaffold,
        source="mongo_auto",
        extra={
            "request_id": ir.request_id,
            "source_shape": ir.source_shape,
            "n_messages": n,
        },
    )
