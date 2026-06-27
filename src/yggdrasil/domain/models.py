"""Pydantic domain models (no I/O)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from yggdrasil.domain.artifacts import ArtifactRef
from yggdrasil.domain.enums import EffortPredicateOp, IndexStatus, StepKind, TrajectoryStatus


class RuntimeFingerprint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    os: str | None = None
    arch: str | None = None
    cpu_model: str | None = None
    cpu_cores: int | None = None
    gpu_model: str | None = None
    gpu_count: int | None = None
    gpu_vram_gb: float | None = None
    ram_gb: float | None = None
    disk_type: str | None = None
    network_class: str | None = None
    region: str | None = None
    cloud_instance_type: str | None = None
    containerized: bool | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class EffortTotals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    wall_clock_seconds: float | None = None
    cpu_seconds: float | None = None
    gpu_seconds: float | None = None
    gpu_memory_gb_hours: float | None = None
    network_bytes: int | None = None
    disk_bytes_read: int | None = None
    disk_bytes_write: int | None = None
    llm_tokens_in: int | None = None
    llm_tokens_out: int | None = None
    llm_api_usd: float | None = None
    cloud_usd: float | None = None
    human_wait_seconds: float | None = None
    failure_waste_seconds: float | None = None
    custom: dict[str, Any] = Field(default_factory=dict)


class EffortLineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: str
    label: str
    quantity: float
    unit: str
    amount_usd: float | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    step_seq_start: int | None = None
    step_seq_end: int | None = None
    was_wasted: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class EffortLedger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    totals: EffortTotals = Field(default_factory=EffortTotals)
    line_items: list[EffortLineItem] = Field(default_factory=list)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    notes: str | None = None


class Progress(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: str | None = None
    summary: str | None = None
    steps_count: int = 0
    last_step_summary: str | None = None
    milestones: list[str] = Field(default_factory=list)
    percent_estimate: float | None = None
    blockers: list[str] = Field(default_factory=list)


class Outcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    terminal_status: TrajectoryStatus
    summary: str
    signals: dict[str, Any] = Field(default_factory=dict)
    goal_satisfied: bool | None = None
    remaining_work: str | None = None


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trajectory_id: str
    seq: int
    kind: StepKind
    summary: str
    payload: dict[str, Any] = Field(default_factory=dict)
    scaffold_update: str | None = None
    is_checkpoint: bool = False
    recorded_at: datetime
    step_effort: EffortLedger | None = None


class Trajectory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    domain: str = "coding"
    status: TrajectoryStatus = TrajectoryStatus.OPEN
    task_text: str
    scaffold_text: str
    runtime_fingerprint: RuntimeFingerprint | None = None
    tags: list[str] = Field(default_factory=list)
    external_refs: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    progress: Progress = Field(default_factory=Progress)
    outcome: Outcome | None = None
    effort: EffortLedger = Field(default_factory=EffortLedger)
    embed_view_version: str = "coding_v1"
    index_status: IndexStatus = IndexStatus.PENDING
    tenant_id: str = "lab"
    schema_version: int = 4
    occurred_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    finalized_at: datetime | None = None

    @property
    def index_state(self) -> IndexStatus:
        """Back-compat alias for index_status."""
        return self.index_status


class EffortPredicate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    op: EffortPredicateOp
    value: float | int | None = None
    min: float | int | None = None
    max: float | int | None = None


class SearchScores(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task: float | None = None
    scaffold: float | None = None
    fused: float | None = None
    fusion: str = "rrf"


class SearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trajectory_id: str
    domain: str
    status: TrajectoryStatus
    task_text: str = ""
    scaffold_text: str = ""
    progress: Progress
    outcome: Outcome | None = None
    effort_totals: EffortTotals
    runtime_fingerprint: RuntimeFingerprint | None = None
    tags: list[str] = Field(default_factory=list)
    external_refs: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    owner: str | None = None
    agent_id: str | None = None
    team: str | None = None
    workspace: str | None = None
    tenant_id: str | None = None
    scores: SearchScores | None = None
    score: float | None = None
    index_status: IndexStatus | None = None
    embed_view_version: str | None = None
    updated_at: datetime | None = None
    finalized_at: datetime | None = None

    @property
    def index_state(self) -> IndexStatus | None:
        """Back-compat alias for index_status."""
        return self.index_status
