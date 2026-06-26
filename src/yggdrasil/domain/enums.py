"""Domain enums for trajectories, steps, index state, and search config."""

from __future__ import annotations

from enum import Enum


class TrajectoryStatus(str, Enum):
    OPEN = "open"
    PARTIAL = "partial"
    SUCCESS = "success"
    FAIL = "fail"
    ABORTED = "aborted"


class StepKind(str, Enum):
    THOUGHT = "thought"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    EDIT = "edit"
    COMMAND = "command"
    CHECKPOINT = "checkpoint"
    NOTE = "note"
    OTHER = "other"


class IndexStatus(str, Enum):
    """Dual-store index health for a trajectory (SQLite SoT + vector index).

    pending — not yet successfully upserted to the vector index
    ready   — vector index reflects current SQLite trajectory payload
    stale   — was ready; subsequent embed/index attempt failed (payload may be outdated)
    failed  — embed/index never succeeded (or hard failure on start)
    """

    PENDING = "pending"
    READY = "ready"
    STALE = "stale"
    FAILED = "failed"


# Back-compat alias (pre-Wave-C name/values). Prefer IndexStatus in new code.
IndexState = IndexStatus

# Legacy string values that may exist in older SQLite DBs / payloads.
_INDEX_STATUS_LEGACY: dict[str, IndexStatus] = {
    "indexed": IndexStatus.READY,
    "error": IndexStatus.FAILED,
}


def coerce_index_status(raw: str | IndexStatus | None) -> IndexStatus:
    """Parse index status, mapping legacy indexed/error values."""
    if raw is None:
        return IndexStatus.PENDING
    if isinstance(raw, IndexStatus):
        return raw
    key = str(raw).strip().lower()
    if key in _INDEX_STATUS_LEGACY:
        return _INDEX_STATUS_LEGACY[key]
    return IndexStatus(key)


class EmbedAspect(str, Enum):
    """PoC embed aspects only. runtime/outcome reserved for later."""

    TASK = "task"
    SCAFFOLD = "scaffold"


class FusionMode(str, Enum):
    RRF = "rrf"
    WEIGHTED = "weighted"


class EffortFilterMode(str, Enum):
    INCLUSIVE_NULL = "inclusive_null"
    STRICT = "strict"


class EffortPredicateOp(str, Enum):
    EQ = "eq"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    BETWEEN = "between"
    IS_NULL = "is_null"
    IS_NOT_NULL = "is_not_null"


TERMINAL_STATUSES: frozenset[TrajectoryStatus] = frozenset(
    {
        TrajectoryStatus.SUCCESS,
        TrajectoryStatus.FAIL,
        TrajectoryStatus.PARTIAL,
        TrajectoryStatus.ABORTED,
    }
)

WRITABLE_STATUSES: frozenset[TrajectoryStatus] = frozenset(
    {
        TrajectoryStatus.OPEN,
        TrajectoryStatus.PARTIAL,
    }
)
