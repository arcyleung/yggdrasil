"""In-process session lifecycle: store + fake embedder/index (no live Qdrant)."""
from __future__ import annotations

import pytest

from yggdrasil.adapters.embed_views import get_embed_view
from yggdrasil.adapters.sqlite_store import SqliteTrajectoryStore
from yggdrasil.config import YggConfig
from yggdrasil.domain.enums import FusionMode, EffortFilterMode, IndexStatus, TrajectoryStatus
from yggdrasil.domain.models import Outcome
from yggdrasil.services.embed_service import EmbedService
from yggdrasil.services.errors import IndexFailedError
from yggdrasil.services.search_service import SearchService
from yggdrasil.services.session_service import SessionService
from fakes import FakeEmbedder, FakeVectorIndex


def _cfg(dim: int = 8) -> YggConfig:
    from pathlib import Path

    return YggConfig(
        sqlite_path=Path(":memory:"),
        qdrant_url="http://localhost:6333",
        qdrant_collection="test",
        qdrant_api_key=None,
        embed_base_url="http://localhost/v1",
        embed_api_key=None,
        embed_model="fake",
        embed_dim=dim,
        default_domain="coding",
        search_include_open=True,
        fusion=FusionMode.RRF,
        w_task=0.5,
        w_scaffold=0.5,
        effort_filter_mode=EffortFilterMode.INCLUSIVE_NULL,
        embed_view_version="coding_v1",
        mongo_uri=None,
        mongo_creds_file=Path("mongo_creds.txt"),
    )


def _wire(tmp_path, *, fail_upsert: bool = False, dim: int = 8):
    store = SqliteTrajectoryStore(tmp_path / "life.db")
    embedder = FakeEmbedder(dimensions=dim)
    index = FakeVectorIndex()
    index.fail_upsert = fail_upsert
    cfg = _cfg(dim)
    view = get_embed_view("coding_v1")
    embed_service = EmbedService(embedder, index, view, cfg)
    session = SessionService(store, embed_service)
    search = SearchService(store, embedder, index, view, cfg, apply_gates_default=False)
    return store, session, search, index


def test_start_append_finalize_and_search(tmp_path):
    store, session, search, index = _wire(tmp_path)
    traj = session.start_trajectory(
        task_text="implement OAuth login flow",
        scaffold_text="FastAPI + authlib",
        tags=["experience_grade"],
    )
    assert traj.index_status == IndexStatus.READY
    assert traj.id in index.points

    out = session.append_step(
        trajectory_id=traj.id,
        kind="thought",
        summary="sketch token exchange",
    )
    assert out["step"].seq == 1
    assert out["trajectory"].index_status == IndexStatus.READY

    finalized = session.finalize_trajectory(
        trajectory_id=traj.id,
        outcome=Outcome(
            terminal_status=TrajectoryStatus.SUCCESS,
            summary="login works",
            goal_satisfied=True,
        ),
    )
    assert finalized.status == TrajectoryStatus.SUCCESS
    assert finalized.index_status == IndexStatus.READY

    got = session.get_trajectory(traj.id, include_steps=True)
    assert len(got["steps"]) == 1

    hits = search.search_strategies(task="OAuth login", apply_gates=False)
    assert any(h.trajectory_id == traj.id for h in hits)
    store.close()


def test_failed_upsert_marks_failed_and_excluded_from_search(tmp_path):
    store, session, search, index = _wire(tmp_path, fail_upsert=True)

    with pytest.raises(IndexFailedError):
        session.start_trajectory(
            task_text="will fail embed path",
            scaffold_text="scaffold",
        )

    # Trajectory still exists in SQLite with failed status
    # We need the id — start raises after create; find via list is hard.
    # Re-create without going through session hard-fail path: use store create + set via append path.
    from yggdrasil.ports.store import CreateTrajectoryInput

    traj = store.create(
        CreateTrajectoryInput(task_text="orphan pending", scaffold_text="s")
    )
    assert traj.index_status == IndexStatus.PENDING

    # Soft-fail path (append) marks failed
    index.fail_upsert = False
    ok = session.start_trajectory(task_text="ok trajectory", scaffold_text="scaffold ok")
    assert ok.index_status == IndexStatus.READY
    index.fail_upsert = True
    # Force reembed on append via scaffold_update so upsert is attempted
    result = session.append_step(
        trajectory_id=ok.id,
        kind="note",
        summary="update",
        scaffold_update="new scaffold that forces reembed",
    )
    # Was ready, so soft failure → stale
    assert result["trajectory"].index_status == IndexStatus.STALE

    # Start a fresh trajectory that fails before ever being ready
    index.fail_upsert = False
    pending_ready = session.start_trajectory(
        task_text="another ok",
        scaffold_text="scaffold",
    )
    # Manually set pending then fail
    store.set_index_status(pending_ready.id, IndexStatus.PENDING)
    index.fail_upsert = True
    result2 = session.append_step(
        trajectory_id=pending_ready.id,
        kind="note",
        summary="x",
        scaffold_update="force reembed again",
    )
    assert result2["trajectory"].index_status == IndexStatus.FAILED

    # Default search excludes pending/failed; stale may still appear if vector point exists
    hits_default = search.search_strategies(task="ok trajectory", apply_gates=False)
    ids_default = {h.trajectory_id for h in hits_default}
    assert pending_ready.id not in ids_default or result2["trajectory"].index_status != IndexStatus.FAILED
    # Explicit: failed traj should not be returned unless include_unindexed
    # Point may still be in fake index from the successful start before we set pending.
    # Filter by SQLite status is what SearchService enforces.
    hits_unindexed = search.search_strategies(
        task="another ok", apply_gates=False, include_unindexed=True
    )
    assert any(h.trajectory_id == pending_ready.id for h in hits_unindexed)

    hits_no = search.search_strategies(task="another ok", apply_gates=False, include_unindexed=False)
    assert not any(
        h.trajectory_id == pending_ready.id and h.index_status == IndexStatus.FAILED
        for h in hits_no
    )
    # Stronger: failed must not appear at all with default flag
    for h in hits_no:
        assert h.index_status not in (IndexStatus.PENDING, IndexStatus.FAILED)

    store.close()


def test_new_trajectory_defaults_pending_until_embed(tmp_path):
    store = SqliteTrajectoryStore(tmp_path / "pend.db")
    from yggdrasil.ports.store import CreateTrajectoryInput

    traj = store.create(CreateTrajectoryInput(task_text="t", scaffold_text="s"))
    assert traj.index_status == IndexStatus.PENDING
    store.close()
