"""B′ multi-lane mapping: system_hash|full_model maximizers + within-lane slices."""
from __future__ import annotations

from datetime import datetime, timezone

from yggdrasil.adapters.importers.mongo_lanes import (
    group_lane_maximizers,
    lane_key_for_ir,
    map_session_multilane,
    model_slug,
    system_hash16,
)
from yggdrasil.adapters.importers.mongo_normalize import (
    ConversationIR,
    IRMessage,
    SessionAggregate,
)


def _now() -> datetime:
    return datetime(2026, 6, 26, 12, 0, 0, tzinfo=timezone.utc)


def _ir(
    *,
    rid: str,
    model: str,
    system: str,
    n_msgs: int,
    session_id: str = "sess-ml",
) -> ConversationIR:
    msgs = [
        IRMessage(role="user" if i % 2 == 0 else "assistant", content=f"msg{i} {model}")
        for i in range(n_msgs)
    ]
    return ConversationIR(
        session_id=session_id,
        request_id=rid,
        model=model,
        created_at=_now(),
        updated_at=_now(),
        title="multi lane session",
        project=None,
        tags=[],
        system_text=system,
        tool_names=[],
        messages=msgs,
        usage=None,
        source_shape="proxy_log",
        raw_external={"request_ids": [rid]},
    )


def test_system_hash16_stable_and_normalized():
    a = system_hash16("hello   world\n\n\nfoo")
    b = system_hash16("hello world\n\nfoo")
    assert a == b
    assert len(a) == 16


def test_lane_key_includes_full_model_not_family_only():
    ir_a = _ir(rid="a", model="claude-haiku-4-5-20251001", system="sys-a", n_msgs=2)
    ir_b = _ir(rid="b", model="claude-haiku-4-5-20251001-fast", system="sys-a", n_msgs=2)
    assert lane_key_for_ir(ir_a) != lane_key_for_ir(ir_b)
    assert lane_key_for_ir(ir_a).endswith("|claude-haiku-4-5-20251001")
    assert system_hash16("sys-a") in lane_key_for_ir(ir_a)


def test_group_lane_maximizers_picks_longest_per_lane():
    short = _ir(rid="s1", model="claude-opus-4-6", system="orchestrator", n_msgs=4)
    long = _ir(rid="s2", model="claude-opus-4-6", system="orchestrator", n_msgs=20)
    other = _ir(rid="s3", model="claude-haiku-4-5", system="subagent tools", n_msgs=10)
    agg = SessionAggregate(session_id="sess-ml", requests=[short, long, other], canonical=long)
    maxes = group_lane_maximizers(agg)
    assert len(maxes) == 2
    opus_key = lane_key_for_ir(long)
    haiku_key = lane_key_for_ir(other)
    assert maxes[opus_key].request_id == "s2"
    assert maxes[opus_key].message_count == 20
    assert maxes[haiku_key].request_id == "s3"


def test_map_session_multilane_emits_lane_and_slice_children():
    opus = _ir(rid="o1", model="claude-opus-4-6", system="main agent system", n_msgs=30)
    haiku = _ir(rid="h1", model="claude-haiku-4-5-20251001", system="worker system", n_msgs=12)
    # second opus request shorter — maximizer should be opus
    opus_short = _ir(rid="o0", model="claude-opus-4-6", system="main agent system", n_msgs=5)
    agg = SessionAggregate(
        session_id="sess-ml",
        requests=[opus_short, opus, haiku],
        canonical=opus,
    )
    h = map_session_multilane(agg, max_segment_msgs=8, tenant_id="lab")
    assert h.parent.trajectory.id == "mongo-session-sess-ml"
    assert h.parent.trajectory.tenant_id == "lab"
    assert "multilane_bprime" in h.parent.trajectory.tags
    assert h.parent.trajectory.external_refs.get("lane_count") == 2
    assert h.parent.trajectory.external_refs.get("multilane_policy") == (
        "B_prime_system_hash_pipe_full_model"
    )

    lanes = [c for c in h.children if c.trajectory.external_refs.get("kind") == "session_lane"]
    slices = [
        c for c in h.children if c.trajectory.external_refs.get("kind") == "session_lane_slice"
    ]
    assert len(lanes) == 2
    assert slices, "expected within-lane slices from segmenter"
    assert all(c.trajectory.external_refs.get("embed_target") is True for c in slices)
    # lane nodes not embed targets when slices exist
    for lane in lanes:
        assert lane.trajectory.external_refs.get("embed_target") is False
        assert "session_lane" in lane.trajectory.tags
        assert lane.trajectory.id.startswith("mongo-session-sess-ml-lane-")
        assert model_slug(lane.trajectory.external_refs["model"]) in lane.trajectory.id
    # slice ids nest under lane id
    for sl in slices:
        lane_id = sl.trajectory.external_refs["lane_trajectory_id"]
        assert sl.trajectory.id.startswith(lane_id + "-slice-")
        assert sl.trajectory.external_refs.get("parent_trajectory_id") == h.parent.trajectory.id
        assert "session_lane_slice" in sl.trajectory.tags


def test_same_system_different_models_are_separate_lanes():
    a = _ir(rid="a", model="claude-sonnet-4-5", system="same sys", n_msgs=6)
    b = _ir(rid="b", model="claude-haiku-4-5", system="same sys", n_msgs=6)
    agg = SessionAggregate(session_id="s2", requests=[a, b], canonical=a)
    h = map_session_multilane(agg, max_segment_msgs=24)
    assert h.parent.trajectory.external_refs["lane_count"] == 2
    models = {
        c.trajectory.external_refs.get("model")
        for c in h.children
        if c.trajectory.external_refs.get("kind") == "session_lane"
    }
    assert models == {"claude-sonnet-4-5", "claude-haiku-4-5"}
