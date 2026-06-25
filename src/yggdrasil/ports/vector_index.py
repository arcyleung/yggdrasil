"""VectorIndex port and Qdrant payload projection."""
from __future__ import annotations
from datetime import datetime
from typing import Any, Protocol, Sequence, runtime_checkable
from pydantic import BaseModel, ConfigDict, Field
from yggdrasil.domain.enums import TrajectoryStatus
from yggdrasil.domain.models import EffortPredicate, Trajectory

class NamedVectors(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: list[float] | None = None
    scaffold: list[float] | None = None

class VectorPointPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trajectory_id: str
    domain: str
    status: TrajectoryStatus
    phase: str | None = None
    steps_count: int = 0
    has_outcome: bool = False
    goal_satisfied: bool | None = None
    tags: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    finalized_at: datetime | None = None
    embed_model: str | None = None
    embed_dim: int | None = None
    embed_view_version: str | None = None
    aspects_present: list[str] = Field(default_factory=list)
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
    gpu_model: str | None = None
    ram_gb: float | None = None
    network_class: str | None = None
    source: str | None = None
    external_source: str | None = None

class UpsertVectorPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trajectory_id: str
    vectors: NamedVectors
    payload: VectorPointPayload

class VectorSearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_vector: list[float] | None = None
    scaffold_vector: list[float] | None = None
    domain: str | None = None
    status_in: list[TrajectoryStatus] | None = None
    include_open: bool = True
    tags_any: list[str] | None = None
    effort_predicates: list[EffortPredicate] = Field(default_factory=list)
    runtime_filters: dict[str, Any] = Field(default_factory=dict)
    limit: int = 10
    overfetch: int | None = None

class VectorSearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trajectory_id: str
    score: float
    payload: VectorPointPayload

def payload_from_trajectory(
    trajectory: Trajectory,
    *,
    embed_model: str | None,
    embed_dim: int | None,
    aspects_present: list[str],
) -> VectorPointPayload:
    totals = trajectory.effort.totals
    fp = trajectory.runtime_fingerprint
    outcome = trajectory.outcome
    source = None
    if trajectory.external_refs:
        src = trajectory.external_refs.get("source")
        if isinstance(src, str):
            source = src
    return VectorPointPayload(
        trajectory_id=trajectory.id,
        domain=trajectory.domain,
        status=trajectory.status,
        phase=trajectory.progress.phase,
        steps_count=trajectory.progress.steps_count,
        has_outcome=outcome is not None,
        goal_satisfied=outcome.goal_satisfied if outcome else None,
        tags=list(trajectory.tags),
        created_at=trajectory.created_at,
        updated_at=trajectory.updated_at,
        finalized_at=trajectory.finalized_at,
        embed_model=embed_model,
        embed_dim=embed_dim,
        embed_view_version=trajectory.embed_view_version,
        aspects_present=list(aspects_present),
        wall_clock_seconds=totals.wall_clock_seconds,
        cpu_seconds=totals.cpu_seconds,
        gpu_seconds=totals.gpu_seconds,
        gpu_memory_gb_hours=totals.gpu_memory_gb_hours,
        network_bytes=totals.network_bytes,
        disk_bytes_read=totals.disk_bytes_read,
        disk_bytes_write=totals.disk_bytes_write,
        llm_tokens_in=totals.llm_tokens_in,
        llm_tokens_out=totals.llm_tokens_out,
        llm_api_usd=totals.llm_api_usd,
        cloud_usd=totals.cloud_usd,
        human_wait_seconds=totals.human_wait_seconds,
        failure_waste_seconds=totals.failure_waste_seconds,
        gpu_model=fp.gpu_model if fp else None,
        ram_gb=fp.ram_gb if fp else None,
        network_class=fp.network_class if fp else None,
        source=source,
        external_source=source,
    )

@runtime_checkable
class VectorIndex(Protocol):
    def ensure_collection(self, *, vector_size: int) -> None: ...
    def upsert(self, point: UpsertVectorPoint) -> None: ...
    def upsert_many(self, points: Sequence[UpsertVectorPoint]) -> None: ...
    def delete(self, trajectory_id: str) -> None: ...
    def search(self, query: VectorSearchQuery) -> list[VectorSearchHit]: ...
