"""TrajectoryStore port and DTOs."""
from __future__ import annotations
from datetime import datetime
from typing import Any, Protocol, Sequence, runtime_checkable
from pydantic import BaseModel, ConfigDict, Field
from yggdrasil.domain.enums import IndexState, StepKind, TrajectoryStatus
from yggdrasil.domain.artifacts import ArtifactRef
from yggdrasil.domain.models import EffortLedger, Outcome, Progress, RuntimeFingerprint, Step, Trajectory

class TrajectoryNotFoundError(Exception):
    def __init__(self, trajectory_id: str) -> None:
        self.trajectory_id = trajectory_id
        super().__init__(trajectory_id)

class TrajectoryClosedError(Exception):
    def __init__(self, trajectory_id: str, status: TrajectoryStatus | None = None) -> None:
        self.trajectory_id = trajectory_id
        self.status = status
        super().__init__(trajectory_id)

class CreateTrajectoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    domain: str = "coding"
    task_text: str
    scaffold_text: str
    runtime_fingerprint: RuntimeFingerprint | None = None
    tags: list[str] = Field(default_factory=list)
    external_refs: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    progress: Progress | None = None
    effort: EffortLedger | None = None
    embed_view_version: str = "coding_v1"

class AppendStepInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trajectory_id: str
    kind: StepKind
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)
    scaffold_update: str | None = None
    task_update: str | None = None
    is_checkpoint: bool = False
    progress: Progress | None = None
    mark_partial: bool = False
    effort_delta: EffortLedger | None = None
    recorded_at: datetime | None = None

class FinalizeTrajectoryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trajectory_id: str
    outcome: Outcome
    effort: EffortLedger | None = None
    runtime_fingerprint: RuntimeFingerprint | None = None
    progress: Progress | None = None
    task_text: str | None = None
    scaffold_text: str | None = None
    finalized_at: datetime | None = None

class UpdateTrajectoryMetaInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trajectory_id: str
    tags: list[str] | None = None
    task_text: str | None = None
    scaffold_text: str | None = None
    runtime_fingerprint: RuntimeFingerprint | None = None
    external_refs: dict[str, Any] | None = None
    artifacts: list[ArtifactRef] | None = None
    merge_artifacts: bool = True

@runtime_checkable
class TrajectoryStore(Protocol):
    def create(self, data: CreateTrajectoryInput) -> Trajectory: ...
    def get(self, trajectory_id: str) -> Trajectory: ...
    def get_steps(self, trajectory_id: str) -> list[Step]: ...
    def list_by_ids(self, trajectory_ids: Sequence[str]) -> list[Trajectory]: ...
    def append_step(self, data: AppendStepInput) -> tuple[Trajectory, Step]: ...
    def finalize(self, data: FinalizeTrajectoryInput) -> Trajectory: ...
    def update_meta(self, data: UpdateTrajectoryMetaInput) -> Trajectory: ...
    def set_index_state(self, trajectory_id: str, index_state: IndexState) -> Trajectory: ...
    def find_by_external_ref(self, source: str, external_id: str) -> Trajectory | None: ...
    def upsert_imported(self, trajectory: Trajectory, steps: Sequence[Step]) -> Trajectory: ...
