"""Pure effort merge helpers (no I/O)."""

from __future__ import annotations

from yggdrasil.domain.enums import TERMINAL_STATUSES, WRITABLE_STATUSES, TrajectoryStatus
from yggdrasil.domain.models import EffortLedger, EffortTotals

NUMERIC_TOTAL_FIELDS: tuple[str, ...] = (
    "wall_clock_seconds",
    "cpu_seconds",
    "gpu_seconds",
    "gpu_memory_gb_hours",
    "network_bytes",
    "disk_bytes_read",
    "disk_bytes_write",
    "llm_tokens_in",
    "llm_tokens_out",
    "llm_api_usd",
    "cloud_usd",
    "human_wait_seconds",
    "failure_waste_seconds",
)


def merge_effort_totals(base: EffortTotals, delta: EffortTotals) -> EffortTotals:
    data: dict = {}
    for field in NUMERIC_TOTAL_FIELDS:
        b = getattr(base, field)
        d = getattr(delta, field)
        if b is None and d is None:
            data[field] = None
        elif b is None:
            data[field] = d
        elif d is None:
            data[field] = b
        else:
            data[field] = b + d
    custom = {**base.custom, **delta.custom}
    for k, v in delta.custom.items():
        if k in base.custom and isinstance(base.custom[k], (int, float)) and isinstance(v, (int, float)):
            custom[k] = base.custom[k] + v
    return EffortTotals(**data, custom=custom)


def merge_effort_ledgers(base: EffortLedger, delta: EffortLedger) -> EffortLedger:
    notes_parts = [n for n in (base.notes, delta.notes) if n]
    notes = "\n".join(notes_parts) if notes_parts else None
    return EffortLedger(
        totals=merge_effort_totals(base.totals, delta.totals),
        line_items=[*base.line_items, *delta.line_items],
        started_at=base.started_at if base.started_at is not None else delta.started_at,
        ended_at=delta.ended_at if delta.ended_at is not None else base.ended_at,
        notes=notes,
    )


def is_terminal_status(status: TrajectoryStatus) -> bool:
    return status in TERMINAL_STATUSES


def is_writable_status(status: TrajectoryStatus) -> bool:
    return status in WRITABLE_STATUSES
