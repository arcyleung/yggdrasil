"""Multi-lane (B′) session mapping: system_hash|full_model maximizers + within-lane segments."""
from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from yggdrasil.adapters.importers.mongo_mapping import (
    MappedSessionHierarchy,
    MappedTrajectory,
    _build_effort_from_ir,
    _build_steps_for_messages,
    _outcome_str_to_status,
)
from yggdrasil.adapters.importers.mongo_normalize import ConversationIR, SessionAggregate
from yggdrasil.adapters.importers.mongo_segment import segment_conversation_ir
from yggdrasil.adapters.importers.segment_schema import SegmentedSession
from yggdrasil.domain.enums import TrajectoryStatus
from yggdrasil.domain.models import Outcome, Progress, Trajectory
from yggdrasil.services.retrieval_gates import clean_task_text_for_embed

_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def normalize_system_text(system_text: str | None, *, max_chars: int = 120_000) -> str:
    s = system_text or ""
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s[:max_chars]


def system_hash16(system_text: str | None) -> str:
    norm = normalize_system_text(system_text)
    return hashlib.sha256(norm.encode("utf-8", errors="replace")).hexdigest()[:16]


def lane_key_for_ir(ir: ConversationIR) -> str:
    """B′: sha256(normalize(system))[:16] + '|' + full model name."""
    model = (ir.model or "unknown").strip() or "unknown"
    return f"{system_hash16(ir.system_text)}|{model}"


def model_slug(model: str | None) -> str:
    m = (model or "unknown").strip() or "unknown"
    m = m.replace("/", "-")
    m = _SLUG_RE.sub("-", m)
    return m[:80]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _pick_lane_maximizer(requests: list[ConversationIR]) -> ConversationIR:
    """Max message_count, then chars proxy via joined message texts length, then updated_at."""

    def score(r: ConversationIR) -> tuple:
        # approximate chars from message texts
        chars = 0
        for msg in r.messages:
            from yggdrasil.adapters.importers.mongo_normalize import ir_message_text

            chars += len(ir_message_text(msg))
        ts = r.updated_at or r.created_at
        return (r.message_count, chars, ts.timestamp() if hasattr(ts, "timestamp") else 0.0, r.request_id)

    return max(requests, key=score)


def group_lane_maximizers(agg: SessionAggregate) -> dict[str, ConversationIR]:
    """lane_key → maximizer ConversationIR among requests in the session."""
    buckets: dict[str, list[ConversationIR]] = defaultdict(list)
    for ir in agg.requests:
        buckets[lane_key_for_ir(ir)].append(ir)
    return {lk: _pick_lane_maximizer(reqs) for lk, reqs in buckets.items()}


def map_session_multilane(
    agg: SessionAggregate,
    *,
    embed_parent: bool = False,
    max_segment_msgs: int = 24,
    caller_segments_by_lane: dict[str, Any] | None = None,
    tenant_id: str = "lab",
) -> MappedSessionHierarchy:
    """Parent session + per-lane maximizer children (segments = slices under each lane).

    Child id patterns:
    - Lane rollup (optional embed): ``mongo-session-{sid}-lane-{sys16}-{model_slug}``
      embed_target=False if it has slice children; True if single slice only (we still
      emit slices when segmenter returns ≥1; lane node is lineage + tags).
    - Slice (primary embed): ``…-lane-…-slice-{idx:04d}`` with embed_target=True.
    """
    session_id = agg.session_id
    parent_id = f"mongo-session-{session_id}"
    maximizers = group_lane_maximizers(agg)
    # Prefer global max-msg IR for parent task text / effort rollup
    global_ir = agg.canonical
    created_at = min((r.created_at for r in agg.requests), default=_utcnow())
    updated_at = max((r.updated_at for r in agg.requests), default=created_at)

    lane_summaries: list[str] = []
    all_children: list[MappedTrajectory] = []
    any_fail = False
    all_success = True
    n_slices = 0

    for lane_key, ir in sorted(maximizers.items(), key=lambda kv: -kv[1].message_count):
        sys16 = system_hash16(ir.system_text)
        mslug = model_slug(ir.model)
        lane_id = f"mongo-session-{session_id}-lane-{sys16}-{mslug}"
        caller = None
        if caller_segments_by_lane and lane_key in caller_segments_by_lane:
            caller = caller_segments_by_lane[lane_key]
        segmented = segment_conversation_ir(
            ir,
            max_segment_msgs=max_segment_msgs,
            caller_segments=caller,
        )
        lane_summaries.append(f"{mslug}:{len(segmented.segments)}slices")

        # Lane lineage trajectory (not primary embed if we have slices)
        lane_task = clean_task_text_for_embed(
            segmented.parent_task or ir.title or f"lane {mslug}"
        ) or f"lane {mslug}"
        lane_scaffold = (
            segmented.parent_scaffold
            or f"model: {ir.model or 'unknown'}; system_hash: {sys16}"
        )
        has_slices = len(segmented.segments) >= 1
        lane_refs = {
            "source": "mongo",
            "db": "claude_conversations",
            "collection": "conversations",
            "session_id": session_id,
            "kind": "session_lane",
            "id": f"session:{session_id}:lane:{sys16}:{mslug}",
            "parent_trajectory_id": parent_id,
            "lane_key": lane_key,
            "system_hash": sys16,
            "model": ir.model,
            "canonical_request_id": ir.request_id,
            "embed_target": not has_slices,  # embed lane only if no slices
            "segment_count": len(segmented.segments),
            "segmentation_source": segmented.source,
        }
        lane_tags = list(
            dict.fromkeys(
                [
                    *ir.tags,
                    "mongo_import",
                    "session_lane",
                    "multilane_bprime",
                    f"model:{ir.model or 'unknown'}",
                    f"system_hash:{sys16}",
                    f"family:{_family(ir.model)}",
                ]
            )
        )
        lane_status = TrajectoryStatus.PARTIAL
        statuses = [_outcome_str_to_status(s.outcome) for s in segmented.segments]
        if statuses and all(s == TrajectoryStatus.SUCCESS for s in statuses):
            lane_status = TrajectoryStatus.SUCCESS
        elif any(s == TrajectoryStatus.FAIL for s in statuses):
            lane_status = TrajectoryStatus.PARTIAL
            any_fail = True
            all_success = False
        else:
            all_success = False

        lane_steps = _build_steps_for_messages(lane_id, ir.messages, created_at=created_at)
        lane_traj = Trajectory(
            id=lane_id,
            domain="coding",
            status=lane_status,
            task_text=lane_task[:4000],
            scaffold_text=lane_scaffold[:4000],
            tags=lane_tags,
            external_refs=lane_refs,
            progress=Progress(
                phase="imported_lane",
                summary=f"lane {lane_key[:32]}… {len(segmented.segments)} slices",
                steps_count=len(lane_steps),
                milestones=[f"slice-{i:04d}" for i in range(len(segmented.segments))],
            ),
            outcome=Outcome(
                terminal_status=lane_status,
                summary=f"lane maximizer ({ir.model})",
                signals={"lane_key": lane_key, "model": ir.model or "unknown"},
                goal_satisfied=lane_status == TrajectoryStatus.SUCCESS,
            ),
            effort=_build_effort_from_ir(ir),
            embed_view_version="coding_v1",
            tenant_id=tenant_id,
            created_at=ir.created_at or created_at,
            updated_at=ir.updated_at or updated_at,
            finalized_at=updated_at,
        )
        all_children.append(MappedTrajectory(trajectory=lane_traj, steps=lane_steps))

        for idx, seg in enumerate(segmented.segments):
            n_slices += 1
            slice_id = f"{lane_id}-slice-{idx:04d}"
            ext_id = f"session:{session_id}:lane:{sys16}:{mslug}:slice:{idx}"
            slice_msgs = ir.messages[seg.start_idx : seg.end_idx + 1]
            status = _outcome_str_to_status(seg.outcome)
            if status == TrajectoryStatus.FAIL:
                any_fail = True
                all_success = False
            elif status != TrajectoryStatus.SUCCESS:
                all_success = False
            scaffold = seg.scaffold_hint or lane_scaffold
            task = clean_task_text_for_embed(seg.task) or lane_task
            child_refs = {
                "source": "mongo",
                "db": "claude_conversations",
                "collection": "conversations",
                "session_id": session_id,
                "kind": "session_lane_slice",
                "id": ext_id,
                "parent_trajectory_id": parent_id,
                "lane_trajectory_id": lane_id,
                "lane_key": lane_key,
                "system_hash": sys16,
                "model": ir.model,
                "segment_index": idx,
                "start_idx": seg.start_idx,
                "end_idx": seg.end_idx,
                "embed_target": True,
                "seg_kind": getattr(seg, "kind", None) or "goal",
            }
            child_tags = list(
                dict.fromkeys(
                    [
                        *ir.tags,
                        "mongo_import",
                        "session_segment",
                        "session_lane_slice",
                        "multilane_bprime",
                        f"model:{ir.model or 'unknown'}",
                        f"system_hash:{sys16}",
                        f"seg_kind:{getattr(seg, 'kind', None) or 'goal'}",
                        f"outcome:{seg.outcome}",
                    ]
                )
            )
            child_steps = _build_steps_for_messages(
                slice_id, slice_msgs, created_at=created_at
            )
            child_traj = Trajectory(
                id=slice_id,
                domain="coding",
                status=status,
                task_text=task[:4000],
                scaffold_text=(scaffold or "")[:4000],
                tags=child_tags,
                external_refs=child_refs,
                progress=Progress(
                    phase="imported_slice",
                    summary=f"slice {idx} msgs {seg.start_idx}-{seg.end_idx}",
                    steps_count=len(child_steps),
                ),
                outcome=Outcome(
                    terminal_status=status,
                    summary=seg.outcome or "unknown",
                    signals={"lane_key": lane_key, "slice": idx},
                    goal_satisfied=status == TrajectoryStatus.SUCCESS,
                ),
                effort=_build_effort_from_ir(ir),
                embed_view_version="coding_v1",
                tenant_id=tenant_id,
                created_at=created_at,
                updated_at=updated_at,
                finalized_at=updated_at,
            )
            all_children.append(MappedTrajectory(trajectory=child_traj, steps=child_steps))

    parent_status = TrajectoryStatus.SUCCESS if all_success and all_children else TrajectoryStatus.PARTIAL
    if any_fail:
        parent_status = TrajectoryStatus.PARTIAL

    parent_task = clean_task_text_for_embed(
        global_ir.title or "imported multilane session"
    ) or "imported multilane session"
    # Prefer first user text from global canonical
    if global_ir.messages:
        from yggdrasil.adapters.importers.mongo_normalize import ir_message_text

        for msg in global_ir.messages:
            if msg.role == "user":
                t = clean_task_text_for_embed(ir_message_text(msg))
                if t:
                    parent_task = t[:4000]
                break

    parent_refs = {
        "source": "mongo",
        "db": "claude_conversations",
        "collection": "conversations",
        "session_id": session_id,
        "kind": "session_parent",
        "id": f"session:{session_id}",
        "request_ids": global_ir.raw_external.get("request_ids")
        or [r.request_id for r in agg.requests],
        "request_count": len(agg.requests),
        "lane_count": len(maximizers),
        "lane_keys": list(maximizers.keys()),
        "slice_count": n_slices,
        "embed_target": embed_parent,
        "multilane_policy": "B_prime_system_hash_pipe_full_model",
    }
    parent_tags = list(
        dict.fromkeys(
            [
                *global_ir.tags,
                "mongo_import",
                "session_parent",
                "has_segments",
                "multilane_bprime",
                f"lane_count:{len(maximizers)}",
            ]
        )
    )
    parent_steps = _build_steps_for_messages(parent_id, global_ir.messages, created_at=created_at)
    parent_traj = Trajectory(
        id=parent_id,
        domain="coding",
        status=parent_status,
        task_text=parent_task[:4000],
        scaffold_text=f"multilane session; lanes={len(maximizers)}; " + ",".join(lane_summaries)[:500],
        tags=parent_tags,
        external_refs=parent_refs,
        progress=Progress(
            phase="imported_session_multilane",
            summary=f"{len(maximizers)} lanes; {len(all_children)} children",
            steps_count=len(parent_steps),
            milestones=lane_summaries[:20],
        ),
        outcome=Outcome(
            terminal_status=parent_status,
            summary=f"multilane import ({len(maximizers)} lanes)",
            signals={"lane_count": len(maximizers), "slice_count": n_slices},
            goal_satisfied=parent_status == TrajectoryStatus.SUCCESS,
        ),
        effort=_build_effort_from_ir(global_ir),
        embed_view_version="coding_v1",
        tenant_id=tenant_id,
        created_at=created_at,
        updated_at=updated_at,
        finalized_at=updated_at,
    )
    parent_mapped = MappedTrajectory(trajectory=parent_traj, steps=parent_steps)

    # Synthetic SegmentedSession for hierarchy compatibility (empty segments ok)
    synthetic = SegmentedSession(
        session_id=session_id,
        parent_task=parent_task,
        parent_scaffold=parent_traj.scaffold_text,
        segments=[],
        source="multilane_bprime",
    )
    return MappedSessionHierarchy(
        parent=parent_mapped, children=all_children, segmented=synthetic
    )


def _family(model: str | None) -> str:
    ml = (model or "").lower()
    if "haiku" in ml:
        return "haiku"
    if "opus" in ml:
        return "opus"
    if "sonnet" in ml:
        return "sonnet"
    return "other"


def map_multilane_from_docs(docs: list[dict[str, Any]], **kwargs: Any) -> MappedSessionHierarchy:
    from yggdrasil.adapters.importers.mongo_normalize import normalize_and_aggregate_docs

    aggs = normalize_and_aggregate_docs(docs)
    if not aggs:
        raise ValueError("no session aggregates from docs")
    # one session expected when docs share session_id
    return map_session_multilane(aggs[0], **kwargs)
