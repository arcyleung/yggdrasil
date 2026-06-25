"""Minimal effort merge tests."""
from yggdrasil.domain.effort import merge_effort_ledgers, merge_effort_totals
from yggdrasil.domain.models import EffortLedger, EffortLineItem, EffortTotals


def test_merge_effort_totals_sums_and_none_absorbed():
    base = EffortTotals(cpu_seconds=1.0, llm_tokens_in=10, wall_clock_seconds=None)
    delta = EffortTotals(cpu_seconds=2.5, llm_tokens_in=None, wall_clock_seconds=5.0)
    merged = merge_effort_totals(base, delta)
    assert merged.cpu_seconds == 3.5
    assert merged.llm_tokens_in == 10
    assert merged.wall_clock_seconds == 5.0


def test_merge_effort_totals_custom_numeric_sum():
    base = EffortTotals(custom={"retries": 1})
    delta = EffortTotals(custom={"retries": 2, "label": "x"})
    merged = merge_effort_totals(base, delta)
    assert merged.custom["retries"] == 3
    assert merged.custom["label"] == "x"


def test_merge_effort_ledgers_line_items_and_notes():
    base = EffortLedger(
        totals=EffortTotals(cpu_seconds=1),
        line_items=[EffortLineItem(category="cpu", label="a", quantity=1, unit="s")],
        notes="first",
        started_at=None,
    )
    delta = EffortLedger(
        totals=EffortTotals(cpu_seconds=2),
        line_items=[EffortLineItem(category="cpu", label="b", quantity=2, unit="s")],
        notes="second",
    )
    merged = merge_effort_ledgers(base, delta)
    assert merged.totals.cpu_seconds == 3
    assert len(merged.line_items) == 2
    assert merged.notes == "first\nsecond"
