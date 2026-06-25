"""Hierarchical segmentation + parent/child mapping."""
from __future__ import annotations

import json
from pathlib import Path

from yggdrasil.adapters.importers.mongo_mapping import map_mongo_session_doc, map_session_hierarchy
from yggdrasil.adapters.importers.mongo_normalize import normalize_mongo_doc
from yggdrasil.adapters.importers.mongo_segment import segment_conversation_ir
from yggdrasil.adapters.importers.segment_schema import SegmentedSession, TrajectorySegment
from yggdrasil.adapters.importers.storage_estimate import estimate_segmented_storage

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "mongo"


def test_caller_segments_schema_example():
    segs = [
        TrajectorySegment(
            start_idx=0,
            end_idx=3,
            task="research flights with google flights skill",
            outcome="success",
        ),
        TrajectorySegment(
            start_idx=4,
            end_idx=6,
            task="use chrome browser agent to book flight",
            outcome="failed, rate limits",
        ),
    ]
    ss = SegmentedSession(session_id="sess-1", segments=segs, source="caller")
    d = ss.to_dict()
    assert len(d["segments"]) == 2
    assert d["segments"][0]["task"].startswith("research flights")
    restored = SegmentedSession.from_dict(d)
    assert restored.segments[1].outcome == "failed, rate limits"


def test_auto_segment_proxy_fixture():
    doc = json.loads((FIXTURES / "conversation_proxy_log_sample.json").read_text())
    ir = normalize_mongo_doc(doc)
    seg = segment_conversation_ir(ir)
    assert seg.session_id
    assert seg.segments
    assert seg.segments[0].start_idx == 0
    assert seg.segments[-1].end_idx == len(ir.messages) - 1


def test_caller_segments_override_auto():
    doc = json.loads((FIXTURES / "conversation_proxy_log_sample.json").read_text())
    ir = normalize_mongo_doc(doc)
    caller = [
        {"start_idx": 0, "end_idx": 1, "task": "plan fix", "outcome": "partial"},
        {"start_idx": 2, "end_idx": 3, "task": "apply fix", "outcome": "success"},
    ]
    seg = segment_conversation_ir(ir, caller_segments=caller)
    assert seg.source == "caller"
    assert len(seg.segments) == 2
    assert seg.segments[0].task == "plan fix"


def test_map_session_hierarchy_parent_children():
    doc = json.loads((FIXTURES / "conversation_proxy_log_sample.json").read_text())
    h = map_mongo_session_doc(doc)
    assert h.parent.trajectory.id.startswith("mongo-session-")
    assert "session_parent" in h.parent.trajectory.tags
    assert h.children
    assert h.children[0].trajectory.id.endswith("-seg-0000")
    assert h.children[0].trajectory.external_refs.get("kind") == "session_segment"
    assert h.children[0].trajectory.external_refs.get("parent_trajectory_id") == h.parent.trajectory.id
    assert h.parent.trajectory.external_refs.get("segments_json")


def test_storage_estimate_15m_tokens_10_sessions():
    est = estimate_segmented_storage(
        n_sessions=10,
        segments_per_session=8,
        embed_dim=1024,
        source_tokens_total=15_000_000,
    )
    # ~80 children × 8KB raw vectors << 15M tokens worth of storage
    assert est.n_child_trajectories == 80
    assert est.total_extra_bytes_est < 50 * 1024 * 1024  # well under 50 MiB est
    assert est.vector_bytes_raw == 80 * 1024 * 2 * 4  # 80 traj × 2 aspects × 1024 × 4
