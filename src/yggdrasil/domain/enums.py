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


class IndexState(str, Enum):
    PENDING = "pending"
    INDEXED = "indexed"
    STALE = "stale"
    ERROR = "error"


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
