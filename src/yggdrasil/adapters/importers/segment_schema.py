"""Reusable hierarchical segment representation for experience trajectories.

Skill callers (agents) or importers produce this structure *before* MCP/Qdrant
writes so each embeddable child has a clear task/outcome slice of the parent session.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SegmentOutcome = Literal[
    "success",
    "failed",
    "partial",
    "aborted",
    "unknown",
    "in_progress",
]


@dataclass
class TrajectorySegment:
    """One sub-trajectory span within a parent session (message index range).

    Indices are inclusive on both ends over the canonical ordered message list
    (or logical turn list — importer documents which index space is used).
    """

    start_idx: int
    end_idx: int
    task: str
    outcome: str = "unknown"
    segment_kind: str = "goal"  # goal | tool_phase | checkpoint | window_split | caller
    scaffold_hint: str | None = None
    notes: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.start_idx < 0 or self.end_idx < self.start_idx:
            raise ValueError(
                f"invalid span start_idx={self.start_idx} end_idx={self.end_idx}"
            )
        self.task = (self.task or "").strip() or "untitled segment"
        self.outcome = (self.outcome or "unknown").strip().lower()

    @property
    def message_span(self) -> int:
        return self.end_idx - self.start_idx + 1

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TrajectorySegment":
        return cls(
            start_idx=int(data["start_idx"]),
            end_idx=int(data["end_idx"]),
            task=str(data.get("task") or ""),
            outcome=str(data.get("outcome") or "unknown"),
            segment_kind=str(data.get("segment_kind") or "caller"),
            scaffold_hint=data.get("scaffold_hint"),
            notes=data.get("notes"),
            extra=dict(data.get("extra") or {}),
        )


@dataclass
class SegmentedSession:
    """Parent session + ordered child segments (hierarchical experience unit)."""

    session_id: str
    segments: list[TrajectorySegment]
    parent_task: str | None = None
    parent_scaffold: str | None = None
    source: str = "caller"  # caller | mongo_auto | fixture
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "parent_task": self.parent_task,
            "parent_scaffold": self.parent_scaffold,
            "source": self.source,
            "segments": [s.to_dict() for s in self.segments],
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SegmentedSession":
        segs = [TrajectorySegment.from_dict(s) for s in (data.get("segments") or [])]
        return cls(
            session_id=str(data["session_id"]),
            segments=segs,
            parent_task=data.get("parent_task"),
            parent_scaffold=data.get("parent_scaffold"),
            source=str(data.get("source") or "caller"),
            extra=dict(data.get("extra") or {}),
        )

    def validate_coverage(self, n_messages: int) -> list[str]:
        """Return warnings (non-fatal) about gaps/overlaps/out-of-range."""
        warnings: list[str] = []
        if n_messages <= 0:
            warnings.append("empty message list")
            return warnings
        if not self.segments:
            warnings.append("no segments")
            return warnings
        covered: set[int] = set()
        for i, seg in enumerate(self.segments):
            if seg.end_idx >= n_messages:
                warnings.append(
                    f"segment[{i}] end_idx={seg.end_idx} >= n_messages={n_messages}"
                )
            for j in range(seg.start_idx, min(seg.end_idx + 1, n_messages)):
                if j in covered:
                    warnings.append(f"overlap at message index {j} (segment[{i}])")
                covered.add(j)
        missing = [j for j in range(n_messages) if j not in covered]
        if missing and len(missing) < n_messages:
            warnings.append(
                f"uncovered message indices (sample): {missing[:8]}"
                + ("…" if len(missing) > 8 else "")
            )
        return warnings
