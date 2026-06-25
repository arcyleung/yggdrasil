"""Qdrant filter compilation for trajectory search."""
from __future__ import annotations

from typing import Any, Sequence

from qdrant_client.http import models as qm

from yggdrasil.domain.enums import EffortFilterMode, EffortPredicateOp, TrajectoryStatus
from yggdrasil.domain.models import EffortPredicate
from yggdrasil.ports.vector_index import VectorSearchQuery

EFFORT_SCALAR_FIELDS: frozenset[str] = frozenset(
    {
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
    }
)

RUNTIME_FILTER_FIELDS: frozenset[str] = frozenset({"gpu_model", "ram_gb", "network_class"})


def _field_condition(key: str, match: Any) -> qm.FieldCondition:
    return qm.FieldCondition(key=key, match=match)


def _range_condition(key: str, **kwargs: Any) -> qm.FieldCondition:
    return qm.FieldCondition(key=key, range=qm.Range(**kwargs))


def _is_null_condition(key: str) -> qm.IsNullCondition:
    return qm.IsNullCondition(is_null=qm.PayloadField(key=key))


def compile_effort_predicate(
    predicate: EffortPredicate,
    *,
    effort_filter_mode: EffortFilterMode = EffortFilterMode.INCLUSIVE_NULL,
) -> qm.Condition | qm.Filter:
    field = predicate.field
    op = predicate.op

    if op == EffortPredicateOp.IS_NULL:
        return _is_null_condition(field)
    if op == EffortPredicateOp.IS_NOT_NULL:
        return qm.Filter(must_not=[_is_null_condition(field)])

    base: qm.Condition | None = None
    if op == EffortPredicateOp.EQ:
        base = _field_condition(field, qm.MatchValue(value=predicate.value))
    elif op == EffortPredicateOp.LT:
        base = _range_condition(field, lt=predicate.value)
    elif op == EffortPredicateOp.LTE:
        base = _range_condition(field, lte=predicate.value)
    elif op == EffortPredicateOp.GT:
        base = _range_condition(field, gt=predicate.value)
    elif op == EffortPredicateOp.GTE:
        base = _range_condition(field, gte=predicate.value)
    elif op == EffortPredicateOp.BETWEEN:
        base = _range_condition(field, gte=predicate.min, lte=predicate.max)
    else:
        raise ValueError(f"unsupported effort predicate op: {op}")

    if effort_filter_mode == EffortFilterMode.INCLUSIVE_NULL and field in EFFORT_SCALAR_FIELDS:
        return qm.Filter(should=[base, _is_null_condition(field)])
    return base


def compile_effort_predicates(
    predicates: Sequence[EffortPredicate],
    *,
    effort_filter_mode: EffortFilterMode = EffortFilterMode.INCLUSIVE_NULL,
) -> list[qm.Condition | qm.Filter]:
    return [
        compile_effort_predicate(p, effort_filter_mode=effort_filter_mode) for p in predicates
    ]


def compile_search_filter(
    query: VectorSearchQuery,
    *,
    effort_filter_mode: EffortFilterMode = EffortFilterMode.INCLUSIVE_NULL,
) -> qm.Filter | None:
    must: list[Any] = []

    if query.domain is not None:
        must.append(_field_condition("domain", qm.MatchValue(value=query.domain)))

    statuses: list[TrajectoryStatus] = []
    if query.status_in:
        statuses = list(query.status_in)
    elif not query.include_open:
        statuses = [
            TrajectoryStatus.SUCCESS,
            TrajectoryStatus.FAIL,
            TrajectoryStatus.PARTIAL,
            TrajectoryStatus.ABORTED,
        ]
    if statuses:
        values = [s.value if isinstance(s, TrajectoryStatus) else str(s) for s in statuses]
        must.append(qm.FieldCondition(key="status", match=qm.MatchAny(any=values)))

    if query.tags_any:
        must.append(qm.FieldCondition(key="tags", match=qm.MatchAny(any=list(query.tags_any))))

    if query.owner:
        must.append(_field_condition("owner", qm.MatchValue(value=query.owner)))
    if query.agent_id:
        must.append(_field_condition("agent_id", qm.MatchValue(value=query.agent_id)))
    if query.team:
        must.append(_field_condition("team", qm.MatchValue(value=query.team)))
    if query.workspace:
        must.append(_field_condition("workspace", qm.MatchValue(value=query.workspace)))
    if query.require_artifacts is True:
        must.append(_field_condition("has_artifacts", qm.MatchValue(value=True)))
    if query.experience_grade_only is True:
        must.append(_field_condition("experience_grade", qm.MatchValue(value=True)))

    for key, value in (query.runtime_filters or {}).items():
        if key not in RUNTIME_FILTER_FIELDS:
            continue
        must.append(_field_condition(key, qm.MatchValue(value=value)))

    for pred_filter in compile_effort_predicates(
        query.effort_predicates or [], effort_filter_mode=effort_filter_mode
    ):
        must.append(pred_filter)

    if not must:
        return None
    return qm.Filter(must=must)
