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
from yggdrasil.domain.enums import IndexStatus, TrajectoryStatus, coerce_index_status
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
    index_status TEXT NOT NULL DEFAULT 'pending',
    tenant_id TEXT NOT NULL DEFAULT 'lab',
    schema_version INTEGER NOT NULL DEFAULT 4,
    occurred_at TEXT,
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

CREATE TABLE IF NOT EXISTS api_tokens (
    token_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    label TEXT
);

CREATE INDEX IF NOT EXISTS idx_steps_trajectory ON steps(trajectory_id);
CREATE INDEX IF NOT EXISTS idx_trajectories_status ON trajectories(status);
-- idx_trajectories_tenant_status created in _migrate_schema after tenant_id exists on legacy DBs
CREATE INDEX IF NOT EXISTS idx_external_ref_traj ON external_ref_index(trajectory_id);
CREATE INDEX IF NOT EXISTS idx_api_tokens_hash ON api_tokens(token_hash);
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
        # PoC multi-thread / multi-process safety knobs
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        # Create tables first; indexes that need migrated columns run in _migrate_schema
        self._conn.executescript(SCHEMA_SQL)
        self._migrate_schema()
        self._conn.commit()

    def _migrate_schema(self) -> None:
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(trajectories)").fetchall()}
        if "artifacts_json" not in cols:
            self._conn.execute(
                "ALTER TABLE trajectories ADD COLUMN artifacts_json TEXT NOT NULL DEFAULT '[]'"
            )
            cols.add("artifacts_json")

        # Wave C: index_status replaces index_state (legacy column may still exist)
        if "index_status" not in cols and "index_state" in cols:
            self._conn.execute(
                "ALTER TABLE trajectories ADD COLUMN index_status TEXT NOT NULL DEFAULT 'pending'"
            )
            self._conn.execute(
                """
                UPDATE trajectories SET index_status = CASE index_state
                    WHEN 'indexed' THEN 'ready'
                    WHEN 'error' THEN 'failed'
                    WHEN 'ready' THEN 'ready'
                    WHEN 'failed' THEN 'failed'
                    WHEN 'stale' THEN 'stale'
                    WHEN 'pending' THEN 'pending'
                    ELSE 'pending'
                END
                """
            )
            cols.add("index_status")
        elif "index_status" not in cols:
            self._conn.execute(
                "ALTER TABLE trajectories ADD COLUMN index_status TEXT NOT NULL DEFAULT 'pending'"
            )
            cols.add("index_status")
        else:
            # Normalize any legacy values still stored in index_status
            self._conn.execute(
                "UPDATE trajectories SET index_status = 'ready' WHERE index_status = 'indexed'"
            )
            self._conn.execute(
                "UPDATE trajectories SET index_status = 'failed' WHERE index_status = 'error'"
            )

        # Multi-tenant: tenant_id on trajectories (backfill existing PoC data → lab)
        if "tenant_id" not in cols:
            self._conn.execute(
                "ALTER TABLE trajectories ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'lab'"
            )
            cols.add("tenant_id")
        self._conn.execute(
            "UPDATE trajectories SET tenant_id = 'lab' WHERE tenant_id IS NULL OR tenant_id = ''"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trajectories_tenant_status ON trajectories(tenant_id, status)"
        )

        # v4: explicit schema_version + event-time occurred_at
        if "schema_version" not in cols:
            self._conn.execute(
                "ALTER TABLE trajectories ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 1"
            )
            cols.add("schema_version")
        if "occurred_at" not in cols:
            self._conn.execute("ALTER TABLE trajectories ADD COLUMN occurred_at TEXT")
            cols.add("occurred_at")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trajectories_schema_version ON trajectories(schema_version)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_trajectories_occurred_at ON trajectories(occurred_at)"
        )

        # Ensure api_tokens table exists (also created in SCHEMA_SQL for new DBs)
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS api_tokens (
                token_id TEXT PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                tenant_id TEXT NOT NULL,
                owner TEXT NOT NULL,
                scopes_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT,
                revoked_at TEXT,
                label TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_api_tokens_hash ON api_tokens(token_hash);
            """
        )

    def close(self) -> None:
        self._conn.close()

    def _row_index_status(self, row: sqlite3.Row) -> IndexStatus:
        keys = row.keys()
        if "index_status" in keys and row["index_status"] is not None:
            return coerce_index_status(row["index_status"])
        if "index_state" in keys and row["index_state"] is not None:
            return coerce_index_status(row["index_state"])
        return IndexStatus.PENDING

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
        keys = row.keys()
        tenant_id = "lab"
        if "tenant_id" in keys and row["tenant_id"]:
            tenant_id = str(row["tenant_id"])
        schema_version = 1
        if "schema_version" in keys and row["schema_version"] is not None:
            schema_version = int(row["schema_version"])
        occurred_at = None
        if "occurred_at" in keys and row["occurred_at"]:
            occurred_at = _dt_from_str(row["occurred_at"])
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
            index_status=self._row_index_status(row),
            tenant_id=tenant_id,
            schema_version=schema_version,
            occurred_at=occurred_at,
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
                embed_view_version, index_status, tenant_id, schema_version, occurred_at,
                created_at, updated_at, finalized_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                traj.index_status.value,
                traj.tenant_id or "lab",
                int(getattr(traj, "schema_version", 4) or 4),
                _dt_to_str(getattr(traj, "occurred_at", None)),
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
                embed_view_version = ?, index_status = ?, tenant_id = ?,
                schema_version = ?, occurred_at = ?,
                updated_at = ?, finalized_at = ?
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
                traj.index_status.value,
                traj.tenant_id or "lab",
                int(getattr(traj, "schema_version", 4) or 4),
                _dt_to_str(getattr(traj, "occurred_at", None)),
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
            index_status=IndexStatus.PENDING,
            tenant_id=data.tenant_id or "lab",
            created_at=now,
            updated_at=now,
            finalized_at=None,
        )
        self._insert_trajectory(traj)
        self._sync_external_refs(traj.id, traj.external_refs)
        self._conn.commit()
        return traj

    def get(self, trajectory_id: str, *, tenant_id: str | None = None) -> Trajectory:
        row = self._conn.execute(
            "SELECT * FROM trajectories WHERE id = ?", (trajectory_id,)
        ).fetchone()
        if row is None:
            raise TrajectoryNotFoundError(trajectory_id)
        traj = self._row_to_trajectory(row)
        if tenant_id is not None and traj.tenant_id != tenant_id:
            # Hide cross-tenant existence (same as not found for callers)
            raise TrajectoryNotFoundError(trajectory_id)
        return traj

    @property
    def connection(self) -> sqlite3.Connection:
        """Expose connection for co-located token store on the same DB."""
        return self._conn

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
        by_id: dict[str, Trajectory] = {}
        for r in rows:
            t = self._row_to_trajectory(r)
            by_id[t.id] = t
        return [by_id[tid] for tid in trajectory_ids if tid in by_id]

    def append_step(self, data: AppendStepInput) -> tuple[Trajectory, Step]:
        # BEGIN IMMEDIATE acquires a write lock so seq allocation is race-safe
        # under concurrent writers (WAL + busy_timeout).
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            traj = self.get(data.trajectory_id)
            self._require_writable(traj)
            now = data.recorded_at or _utcnow()
            # Allocate seq inside the write transaction (subquery avoids TOCTOU)
            cur = self._conn.execute(
                """
                INSERT INTO steps (
                    trajectory_id, seq, kind, summary, payload_json,
                    scaffold_update, is_checkpoint, recorded_at, step_effort_json
                )
                SELECT
                    ?,
                    COALESCE((SELECT MAX(seq) FROM steps WHERE trajectory_id = ?), 0) + 1,
                    ?, ?, ?, ?, ?, ?, ?
                """,
                (
                    data.trajectory_id,
                    data.trajectory_id,
                    data.kind.value if hasattr(data.kind, "value") else data.kind,
                    data.summary,
                    _json_dumps(dict(data.payload)),
                    data.scaffold_update,
                    1 if data.is_checkpoint else 0,
                    _dt_to_str(now),
                    _json_dumps(data.effort_delta) if data.effort_delta else None,
                ),
            )
            # Re-read allocated seq
            seq_row = self._conn.execute(
                "SELECT MAX(seq) AS m FROM steps WHERE trajectory_id = ?",
                (data.trajectory_id,),
            ).fetchone()
            next_seq = int(seq_row["m"])

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

            if data.task_update is not None:
                traj = traj.model_copy(update={"task_text": data.task_update})
            if data.scaffold_update is not None:
                traj = traj.model_copy(update={"scaffold_text": data.scaffold_update})
            if data.effort_delta is not None:
                traj = traj.model_copy(update={"effort": merge_effort_ledgers(traj.effort, data.effort_delta)})
            if data.mark_partial:
                traj = traj.model_copy(update={"status": TrajectoryStatus.PARTIAL})

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
        except Exception:
            self._conn.rollback()
            raise

    def finalize(self, data: FinalizeTrajectoryInput) -> Trajectory:
        traj = self.get(data.trajectory_id)
        if is_terminal_status(traj.status) and traj.status != TrajectoryStatus.PARTIAL:
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

    def set_index_status(self, trajectory_id: str, index_status: IndexStatus) -> Trajectory:
        traj = self.get(trajectory_id)
        traj = traj.model_copy(update={"index_status": index_status, "updated_at": _utcnow()})
        self._update_trajectory(traj)
        self._conn.commit()
        return traj

    def set_index_state(self, trajectory_id: str, index_state: IndexStatus) -> Trajectory:
        """Back-compat alias for set_index_status."""
        return self.set_index_status(trajectory_id, index_state)

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

    def _upsert_imported_no_commit(self, trajectory: Trajectory, steps: Sequence[Step]) -> Trajectory:
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
        return trajectory

    def upsert_imported(self, trajectory: Trajectory, steps: Sequence[Step]) -> Trajectory:
        trajectory = self._upsert_imported_no_commit(trajectory, steps)
        self._conn.commit()
        return trajectory

    def upsert_imported_many(
        self, items: Sequence[tuple[Trajectory, Sequence[Step]]]
    ) -> list[Trajectory]:
        """Persist multiple trajectories+steps in a single transaction (hierarchical import)."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            out: list[Trajectory] = []
            for trajectory, steps in items:
                out.append(self._upsert_imported_no_commit(trajectory, steps))
            self._conn.commit()
            return out
        except Exception:
            self._conn.rollback()
            raise


# Protocol satisfaction check (static typing aid)
_: type[TrajectoryStore] = SqliteTrajectoryStore  # type: ignore[misc,assignment]
