"""SQLite TrajectoryStore adapter."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from yggdrasil.domain.artifacts import ArtifactRef, artifacts_from_step_payload, merge_artifacts
from yggdrasil.domain.effort import is_terminal_status, is_writable_status, merge_effort_ledgers
from yggdrasil.domain.enums import IndexState, TrajectoryStatus
from yggdrasil.domain.models import (
    EffortLedger,
    Outcome,
    Progress,
    RuntimeFingerprint,
    Step,
    Trajectory,
)
from yggdrasil.ports.store import (
    AppendStepInput,
    CreateTrajectoryInput,
    FinalizeTrajectoryInput,
    TrajectoryClosedError,
    TrajectoryNotFoundError,
    TrajectoryStore,
    UpdateTrajectoryMetaInput,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS trajectories (
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

CREATE TABLE IF NOT EXISTS steps (
    trajectory_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    scaffold_update TEXT,
    is_checkpoint INTEGER NOT NULL DEFAULT 0,
    recorded_at TEXT NOT NULL,
    step_effort_json TEXT,
    PRIMARY KEY (trajectory_id, seq),
    FOREIGN KEY (trajectory_id) REFERENCES trajectories(id)
);

CREATE TABLE IF NOT EXISTS external_ref_index (
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    trajectory_id TEXT NOT NULL,
    PRIMARY KEY (source, external_id),
    FOREIGN KEY (trajectory_id) REFERENCES trajectories(id)
);

CREATE INDEX IF NOT EXISTS idx_steps_trajectory ON steps(trajectory_id);
CREATE INDEX IF NOT EXISTS idx_trajectories_status ON trajectories(status);
CREATE INDEX IF NOT EXISTS idx_external_ref_traj ON external_ref_index(trajectory_id);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dt_to_str(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _dt_from_str(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    return datetime.fromisoformat(raw)


def _json_dumps(obj: Any) -> str:
    if hasattr(obj, "model_dump"):
        return json.dumps(obj.model_dump(mode="json"))
    return json.dumps(obj)


def _json_loads(raw: str | None, default: Any = None) -> Any:
    if raw is None:
        return default
    return json.loads(raw)


class SqliteTrajectoryStore:
    """SQLite-backed TrajectoryStore implementation."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        if self._db_path.parent != Path("."):
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA_SQL)
        self._migrate_schema()
        self._conn.commit()

    def _migrate_schema(self) -> None:
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(trajectories)").fetchall()}
        if "artifacts_json" not in cols:
            self._conn.execute(
                "ALTER TABLE trajectories ADD COLUMN artifacts_json TEXT NOT NULL DEFAULT '[]'"
            )

    def close(self) -> None:
        self._conn.close()

    def _row_to_trajectory(self, row: sqlite3.Row) -> Trajectory:
        runtime_raw = _json_loads(row["runtime_fingerprint_json"])
        outcome_raw = _json_loads(row["outcome_json"])
        progress_raw = _json_loads(row["progress_json"], {})
        effort_raw = _json_loads(row["effort_json"], {})
        tags_raw = _json_loads(row["tags_json"], [])
        external_refs_raw = _json_loads(row["external_refs_json"], {})
        try:
            artifacts_raw = _json_loads(row["artifacts_json"], [])
        except (KeyError, IndexError):
            artifacts_raw = []
        artifacts = [ArtifactRef.model_validate(a) for a in (artifacts_raw or [])]
        return Trajectory(
            id=row["id"],
            domain=row["domain"],
            status=TrajectoryStatus(row["status"]),
            task_text=row["task_text"],
            scaffold_text=row["scaffold_text"],
            runtime_fingerprint=RuntimeFingerprint.model_validate(runtime_raw) if runtime_raw else None,
            tags=list(tags_raw or []),
            external_refs=dict(external_refs_raw or {}),
            artifacts=artifacts,
            progress=Progress.model_validate(progress_raw or {}),
            outcome=Outcome.model_validate(outcome_raw) if outcome_raw else None,
            effort=EffortLedger.model_validate(effort_raw or {}),
            embed_view_version=row["embed_view_version"],
            index_state=IndexState(row["index_state"]),
            created_at=_dt_from_str(row["created_at"]) or _utcnow(),
            updated_at=_dt_from_str(row["updated_at"]) or _utcnow(),
            finalized_at=_dt_from_str(row["finalized_at"]),
        )

    def _row_to_step(self, row: sqlite3.Row) -> Step:
        effort_raw = _json_loads(row["step_effort_json"])
        return Step(
            trajectory_id=row["trajectory_id"],
            seq=row["seq"],
            kind=row["kind"],
            summary=row["summary"],
            payload=_json_loads(row["payload_json"], {}) or {},
            scaffold_update=row["scaffold_update"],
            is_checkpoint=bool(row["is_checkpoint"]),
            recorded_at=_dt_from_str(row["recorded_at"]) or _utcnow(),
            step_effort=EffortLedger.model_validate(effort_raw) if effort_raw else None,
        )

    def _insert_trajectory(self, traj: Trajectory) -> None:
        self._conn.execute(
            """
            INSERT INTO trajectories (
                id, domain, status, task_text, scaffold_text,
                runtime_fingerprint_json, tags_json, external_refs_json, artifacts_json,
                progress_json, outcome_json, effort_json,
                embed_view_version, index_state, created_at, updated_at, finalized_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                traj.id,
                traj.domain,
                traj.status.value,
                traj.task_text,
                traj.scaffold_text,
                _json_dumps(traj.runtime_fingerprint) if traj.runtime_fingerprint else None,
                _json_dumps(traj.tags),
                _json_dumps(traj.external_refs),
                _json_dumps([a.model_dump(mode="json") for a in traj.artifacts]),
                _json_dumps(traj.progress),
                _json_dumps(traj.outcome) if traj.outcome else None,
                _json_dumps(traj.effort),
                traj.embed_view_version,
                traj.index_state.value,
                _dt_to_str(traj.created_at),
                _dt_to_str(traj.updated_at),
                _dt_to_str(traj.finalized_at),
            ),
        )

    def _update_trajectory(self, traj: Trajectory) -> None:
        self._conn.execute(
            """
            UPDATE trajectories SET
                domain = ?, status = ?, task_text = ?, scaffold_text = ?,
                runtime_fingerprint_json = ?, tags_json = ?, external_refs_json = ?,
                artifacts_json = ?,
                progress_json = ?, outcome_json = ?, effort_json = ?,
                embed_view_version = ?, index_state = ?, updated_at = ?, finalized_at = ?
            WHERE id = ?
            """,
            (
                traj.domain,
                traj.status.value,
                traj.task_text,
                traj.scaffold_text,
                _json_dumps(traj.runtime_fingerprint) if traj.runtime_fingerprint else None,
                _json_dumps(traj.tags),
                _json_dumps(traj.external_refs),
                _json_dumps([a.model_dump(mode="json") for a in traj.artifacts]),
                _json_dumps(traj.progress),
                _json_dumps(traj.outcome) if traj.outcome else None,
                _json_dumps(traj.effort),
                traj.embed_view_version,
                traj.index_state.value,
                _dt_to_str(traj.updated_at),
                _dt_to_str(traj.finalized_at),
                traj.id,
            ),
        )

    def _sync_external_refs(self, trajectory_id: str, external_refs: dict[str, Any]) -> None:
        self._conn.execute("DELETE FROM external_ref_index WHERE trajectory_id = ?", (trajectory_id,))
        source = external_refs.get("source")
        external_id = external_refs.get("id")
        if isinstance(source, str) and external_id is not None:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO external_ref_index (source, external_id, trajectory_id)
                VALUES (?, ?, ?)
                """,
                (source, str(external_id), trajectory_id),
            )

    def _require_writable(self, traj: Trajectory) -> None:
        if not is_writable_status(traj.status):
            raise TrajectoryClosedError(traj.id, traj.status)

    def create(self, data: CreateTrajectoryInput) -> Trajectory:
        now = _utcnow()
        traj_id = data.id or str(uuid.uuid4())
        progress = data.progress or Progress()
        effort = data.effort or EffortLedger()
        traj = Trajectory(
            id=traj_id,
            domain=data.domain,
            status=TrajectoryStatus.OPEN,
            task_text=data.task_text,
            scaffold_text=data.scaffold_text,
            runtime_fingerprint=data.runtime_fingerprint,
            tags=list(data.tags),
            external_refs=dict(data.external_refs),
            artifacts=list(data.artifacts or []),
            progress=progress,
            outcome=None,
            effort=effort,
            embed_view_version=data.embed_view_version,
            index_state=IndexState.PENDING,
            created_at=now,
            updated_at=now,
            finalized_at=None,
        )
        self._insert_trajectory(traj)
        self._sync_external_refs(traj.id, traj.external_refs)
        self._conn.commit()
        return traj

    def get(self, trajectory_id: str) -> Trajectory:
        row = self._conn.execute(
            "SELECT * FROM trajectories WHERE id = ?", (trajectory_id,)
        ).fetchone()
        if row is None:
            raise TrajectoryNotFoundError(trajectory_id)
        return self._row_to_trajectory(row)

    def get_steps(self, trajectory_id: str) -> list[Step]:
        self.get(trajectory_id)  # ensure exists
        rows = self._conn.execute(
            "SELECT * FROM steps WHERE trajectory_id = ? ORDER BY seq ASC",
            (trajectory_id,),
        ).fetchall()
        return [self._row_to_step(r) for r in rows]

    def list_by_ids(self, trajectory_ids: Sequence[str]) -> list[Trajectory]:
        if not trajectory_ids:
            return []
        placeholders = ",".join("?" for _ in trajectory_ids)
        rows = self._conn.execute(
            f"SELECT * FROM trajectories WHERE id IN ({placeholders})",
            list(trajectory_ids),
        ).fetchall()
        by_id = {self._row_to_trajectory(r).id: self._row_to_trajectory(r) for r in rows}
        # re-read once properly
        by_id = {}
        for r in rows:
            t = self._row_to_trajectory(r)
            by_id[t.id] = t
        return [by_id[tid] for tid in trajectory_ids if tid in by_id]

    def append_step(self, data: AppendStepInput) -> tuple[Trajectory, Step]:
        traj = self.get(data.trajectory_id)
        self._require_writable(traj)
        now = data.recorded_at or _utcnow()
        max_seq_row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM steps WHERE trajectory_id = ?",
            (data.trajectory_id,),
        ).fetchone()
        next_seq = int(max_seq_row["m"]) + 1

        step = Step(
            trajectory_id=data.trajectory_id,
            seq=next_seq,
            kind=data.kind,
            summary=data.summary,
            payload=dict(data.payload),
            scaffold_update=data.scaffold_update,
            is_checkpoint=data.is_checkpoint,
            recorded_at=now,
            step_effort=data.effort_delta,
        )
        self._conn.execute(
            """
            INSERT INTO steps (
                trajectory_id, seq, kind, summary, payload_json,
                scaffold_update, is_checkpoint, recorded_at, step_effort_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                step.trajectory_id,
                step.seq,
                step.kind.value if hasattr(step.kind, "value") else step.kind,
                step.summary,
                _json_dumps(step.payload),
                step.scaffold_update,
                1 if step.is_checkpoint else 0,
                _dt_to_str(step.recorded_at),
                _json_dumps(step.step_effort) if step.step_effort else None,
            ),
        )

        if data.task_update is not None:
            traj = traj.model_copy(update={"task_text": data.task_update})
        if data.scaffold_update is not None:
            traj = traj.model_copy(update={"scaffold_text": data.scaffold_update})
        if data.effort_delta is not None:
            traj = traj.model_copy(update={"effort": merge_effort_ledgers(traj.effort, data.effort_delta)})
        if data.mark_partial:
            traj = traj.model_copy(update={"status": TrajectoryStatus.PARTIAL})

        # Harvest artifact refs from step payloads (skill contract)
        step_arts = artifacts_from_step_payload(step.payload, step_seq=step.seq)
        if step_arts:
            traj = traj.model_copy(update={"artifacts": merge_artifacts(traj.artifacts, step_arts)})

        progress = traj.progress
        if data.progress is not None:
            progress = data.progress
        else:
            progress = progress.model_copy(
                update={
                    "steps_count": progress.steps_count + 1,
                    "last_step_summary": data.summary,
                }
            )
        traj = traj.model_copy(update={"progress": progress, "updated_at": now})
        self._update_trajectory(traj)
        self._conn.commit()
        return traj, step

    def finalize(self, data: FinalizeTrajectoryInput) -> Trajectory:
        traj = self.get(data.trajectory_id)
        if is_terminal_status(traj.status) and traj.status != TrajectoryStatus.PARTIAL:
            # allow finalize from partial/open only; success/fail/aborted are closed
            if traj.status in (TrajectoryStatus.SUCCESS, TrajectoryStatus.FAIL, TrajectoryStatus.ABORTED):
                raise TrajectoryClosedError(traj.id, traj.status)
        if traj.finalized_at is not None and traj.status in (
            TrajectoryStatus.SUCCESS,
            TrajectoryStatus.FAIL,
            TrajectoryStatus.ABORTED,
        ):
            raise TrajectoryClosedError(traj.id, traj.status)

        now = data.finalized_at or _utcnow()
        updates: dict[str, Any] = {
            "outcome": data.outcome,
            "status": data.outcome.terminal_status,
            "finalized_at": now,
            "updated_at": now,
        }
        if data.effort is not None:
            updates["effort"] = merge_effort_ledgers(traj.effort, data.effort)
        if data.runtime_fingerprint is not None:
            updates["runtime_fingerprint"] = data.runtime_fingerprint
        if data.progress is not None:
            updates["progress"] = data.progress
        if data.task_text is not None:
            updates["task_text"] = data.task_text
        if data.scaffold_text is not None:
            updates["scaffold_text"] = data.scaffold_text
        traj = traj.model_copy(update=updates)
        self._update_trajectory(traj)
        self._conn.commit()
        return traj

    def update_meta(self, data: UpdateTrajectoryMetaInput) -> Trajectory:
        traj = self.get(data.trajectory_id)
        updates: dict[str, Any] = {"updated_at": _utcnow()}
        if data.tags is not None:
            updates["tags"] = list(data.tags)
        if data.task_text is not None:
            updates["task_text"] = data.task_text
        if data.scaffold_text is not None:
            updates["scaffold_text"] = data.scaffold_text
        if data.runtime_fingerprint is not None:
            updates["runtime_fingerprint"] = data.runtime_fingerprint
        if data.external_refs is not None:
            updates["external_refs"] = dict(data.external_refs)
        if data.artifacts is not None:
            if data.merge_artifacts:
                updates["artifacts"] = merge_artifacts(traj.artifacts, data.artifacts)
            else:
                updates["artifacts"] = merge_artifacts(None, data.artifacts, replace=True)
        traj = traj.model_copy(update=updates)
        self._update_trajectory(traj)
        if data.external_refs is not None:
            self._sync_external_refs(traj.id, traj.external_refs)
        self._conn.commit()
        return traj

    def set_index_state(self, trajectory_id: str, index_state: IndexState) -> Trajectory:
        traj = self.get(trajectory_id)
        traj = traj.model_copy(update={"index_state": index_state, "updated_at": _utcnow()})
        self._update_trajectory(traj)
        self._conn.commit()
        return traj

    def find_by_external_ref(self, source: str, external_id: str) -> Trajectory | None:
        row = self._conn.execute(
            """
            SELECT t.* FROM trajectories t
            JOIN external_ref_index e ON e.trajectory_id = t.id
            WHERE e.source = ? AND e.external_id = ?
            """,
            (source, external_id),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_trajectory(row)

    def upsert_imported(self, trajectory: Trajectory, steps: Sequence[Step]) -> Trajectory:
        existing = None
        source = trajectory.external_refs.get("source")
        external_id = trajectory.external_refs.get("id")
        if isinstance(source, str) and external_id is not None:
            existing = self.find_by_external_ref(source, str(external_id))

        if existing is not None:
            trajectory = trajectory.model_copy(
                update={"id": existing.id, "created_at": existing.created_at}
            )
            self._conn.execute("DELETE FROM steps WHERE trajectory_id = ?", (existing.id,))
            self._update_trajectory(trajectory)
        else:
            try:
                self.get(trajectory.id)
                self._conn.execute("DELETE FROM steps WHERE trajectory_id = ?", (trajectory.id,))
                self._update_trajectory(trajectory)
            except TrajectoryNotFoundError:
                self._insert_trajectory(trajectory)

        for step in steps:
            self._conn.execute(
                """
                INSERT INTO steps (
                    trajectory_id, seq, kind, summary, payload_json,
                    scaffold_update, is_checkpoint, recorded_at, step_effort_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trajectory.id,
                    step.seq,
                    step.kind.value if hasattr(step.kind, "value") else step.kind,
                    step.summary,
                    _json_dumps(step.payload),
                    step.scaffold_update,
                    1 if step.is_checkpoint else 0,
                    _dt_to_str(step.recorded_at),
                    _json_dumps(step.step_effort) if step.step_effort else None,
                ),
            )
        self._sync_external_refs(trajectory.id, trajectory.external_refs)
        self._conn.commit()
        return trajectory


# Protocol satisfaction check (static typing aid)
_: type[TrajectoryStore] = SqliteTrajectoryStore  # type: ignore[misc,assignment]
