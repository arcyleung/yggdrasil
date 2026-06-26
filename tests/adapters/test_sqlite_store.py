"""SQLite TrajectoryStore unit/integration tests (tmp_path)."""
from __future__ import annotations

import sqlite3

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


def test_opens_and_migrates_legacy_db_without_tenant_column(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE trajectories (
            id TEXT PRIMARY KEY,
            domain TEXT NOT NULL,
            status TEXT NOT NULL,
            task_text TEXT NOT NULL,
            scaffold_text TEXT NOT NULL,
            runtime_fingerprint_json TEXT,
            tags_json TEXT NOT NULL DEFAULT '[]',
            external_refs_json TEXT NOT NULL DEFAULT '{}',
            artifacts_json TEXT NOT NULL DEFAULT '[]',
            progress_json TEXT NOT NULL DEFAULT '{}',
            outcome_json TEXT,
            effort_json TEXT NOT NULL DEFAULT '{}',
            embed_view_version TEXT NOT NULL DEFAULT 'coding_v1',
            index_state TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            finalized_at TEXT
        );
        CREATE TABLE steps (
            trajectory_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            kind TEXT NOT NULL,
            summary TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}',
            scaffold_update TEXT,
            is_checkpoint INTEGER NOT NULL DEFAULT 0,
            recorded_at TEXT NOT NULL,
            step_effort_json TEXT,
            PRIMARY KEY (trajectory_id, seq)
        );
        INSERT INTO trajectories (
            id, domain, status, task_text, scaffold_text, tags_json,
            external_refs_json, progress_json, effort_json, embed_view_version,
            index_state, created_at, updated_at
        ) VALUES (
            'legacy-1', 'coding', 'success', 'legacy task', 'legacy scaffold',
            '[]', '{}', '{}', '{}', 'coding_v1', 'indexed',
            '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
        );
        """
    )
    conn.commit()
    conn.close()

    store = SqliteTrajectoryStore(db_path)
    traj = store.get("legacy-1")
    cols = {r[1] for r in store.connection.execute("PRAGMA table_info(trajectories)")}
    indexes = {r[1] for r in store.connection.execute("PRAGMA index_list(trajectories)")}

    assert "tenant_id" in cols
    assert "index_status" in cols
    assert traj.tenant_id == "lab"
    assert traj.index_status == IndexStatus.READY
    assert "idx_trajectories_tenant_status" in indexes
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
