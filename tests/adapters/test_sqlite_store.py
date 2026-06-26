"""SQLite TrajectoryStore unit/integration tests (tmp_path)."""
from __future__ import annotations

from yggdrasil.adapters.sqlite_store import SqliteTrajectoryStore
from yggdrasil.domain.enums import IndexStatus, StepKind, TrajectoryStatus
from yggdrasil.domain.models import Outcome
from yggdrasil.ports.store import (
    AppendStepInput,
    CreateTrajectoryInput,
    FinalizeTrajectoryInput,
)


def test_roundtrip_trajectory(tmp_path):
    store = SqliteTrajectoryStore(tmp_path / "t.db")
    traj = store.create(
        CreateTrajectoryInput(
            task_text="fix the flaky test",
            scaffold_text="pytest -q",
            domain="coding",
            tags=["lab"],
        )
    )
    assert traj.id
    assert traj.status == TrajectoryStatus.OPEN
    assert traj.index_status == IndexStatus.PENDING
    # Back-compat property
    assert traj.index_state == IndexStatus.PENDING

    traj2, step1 = store.append_step(
        AppendStepInput(
            trajectory_id=traj.id,
            kind=StepKind.THOUGHT,
            summary="investigate flake",
        )
    )
    assert step1.seq == 1
    traj2, step2 = store.append_step(
        AppendStepInput(
            trajectory_id=traj.id,
            kind=StepKind.COMMAND,
            summary="rerun failing test",
        )
    )
    assert step2.seq == 2
    assert step2.seq > step1.seq

    finalized = store.finalize(
        FinalizeTrajectoryInput(
            trajectory_id=traj.id,
            outcome=Outcome(
                terminal_status=TrajectoryStatus.SUCCESS,
                summary="flake fixed",
                goal_satisfied=True,
            ),
        )
    )
    assert finalized.status == TrajectoryStatus.SUCCESS
    assert finalized.finalized_at is not None

    got = store.get(traj.id)
    steps = store.get_steps(traj.id)
    assert got.task_text == "fix the flaky test"
    assert len(steps) == 2
    assert [s.seq for s in steps] == [1, 2]
    store.close()


def test_seq_monotonic_across_appends(tmp_path):
    store = SqliteTrajectoryStore(tmp_path / "seq.db")
    traj = store.create(
        CreateTrajectoryInput(task_text="t", scaffold_text="s")
    )
    seqs = []
    for i in range(5):
        _, step = store.append_step(
            AppendStepInput(
                trajectory_id=traj.id,
                kind=StepKind.NOTE,
                summary=f"step {i}",
            )
        )
        seqs.append(step.seq)
    assert seqs == [1, 2, 3, 4, 5]
    store.close()


def test_set_index_status(tmp_path):
    store = SqliteTrajectoryStore(tmp_path / "idx.db")
    traj = store.create(
        CreateTrajectoryInput(task_text="t", scaffold_text="s")
    )
    assert traj.index_status == IndexStatus.PENDING
    ready = store.set_index_status(traj.id, IndexStatus.READY)
    assert ready.index_status == IndexStatus.READY
    # alias
    failed = store.set_index_state(traj.id, IndexStatus.FAILED)
    assert failed.index_status == IndexStatus.FAILED
    store.close()


def test_wal_mode_enabled(tmp_path):
    store = SqliteTrajectoryStore(tmp_path / "wal.db")
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    store.close()


def test_upsert_imported_many_transactional(tmp_path):
    from datetime import datetime, timezone

    from yggdrasil.domain.models import Progress, Trajectory, Step

    store = SqliteTrajectoryStore(tmp_path / "many.db")
    now = datetime.now(timezone.utc)
    parent = Trajectory(
        id="parent-1",
        domain="coding",
        status=TrajectoryStatus.SUCCESS,
        task_text="parent task",
        scaffold_text="parent scaffold",
        progress=Progress(),
        embed_view_version="coding_v1",
        index_status=IndexStatus.PENDING,
        created_at=now,
        updated_at=now,
        external_refs={"source": "mongo", "id": "sess-parent"},
    )
    child = Trajectory(
        id="child-1",
        domain="coding",
        status=TrajectoryStatus.SUCCESS,
        task_text="child task",
        scaffold_text="child scaffold",
        progress=Progress(),
        embed_view_version="coding_v1",
        index_status=IndexStatus.PENDING,
        created_at=now,
        updated_at=now,
        external_refs={"source": "mongo", "id": "sess-child-0"},
    )
    step = Step(
        trajectory_id="child-1",
        seq=1,
        kind=StepKind.NOTE,
        summary="hi",
        recorded_at=now,
    )
    out = store.upsert_imported_many([(parent, []), (child, [step])])
    assert len(out) == 2
    assert store.get("parent-1").task_text == "parent task"
    assert len(store.get_steps("child-1")) == 1
    store.close()
