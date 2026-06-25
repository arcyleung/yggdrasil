"""Phase 1: dual-shape normalizer + session aggregate."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from yggdrasil.adapters.importers.mongo_mapping import (
    map_conversation_ir_legacy,
    map_mongo_conversation_doc,
)
from yggdrasil.adapters.importers.mongo_normalize import (
    aggregate_session_irs,
    detect_source_shape,
    normalize_and_aggregate_docs,
    normalize_mongo_doc,
)
from yggdrasil.domain.enums import StepKind

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "mongo"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_fixture_shape_detected_and_normalized():
    doc = _load("conversation_sample.json")
    assert detect_source_shape(doc) == "fixture_v1"
    ir = normalize_mongo_doc(doc)
    assert ir.source_shape == "fixture_v1"
    assert ir.request_id == "65a1b2c3d4e5f67890123456"
    assert ir.session_id == ir.request_id  # synthetic
    assert ir.title == "Fix flaky auth test"
    assert ir.project == "backend-api"
    assert "python" in ir.tags
    assert len(ir.messages) == 4
    assert ir.messages[1].tool_calls  # OpenAI-style on assistant
    assert ir.usage is not None
    assert ir.usage.input_tokens == 1200
    assert ir.usage.output_tokens == 800
    assert "request_headers" not in ir.raw_external


def test_proxy_log_shape_normalized_strips_headers():
    doc = _load("conversation_proxy_log_sample.json")
    assert detect_source_shape(doc) == "proxy_log"
    ir = normalize_mongo_doc(doc)
    assert ir.source_shape == "proxy_log"
    assert ir.session_id == "8efd1fed-0000-4000-8000-000000000001"
    assert ir.model and "haiku" in ir.model
    assert "Read" in ir.tool_names and "Bash" in ir.tool_names
    assert "AGENT_SYSTEM_PROMPT" in ir.system_text
    assert len(ir.messages) == 4
    assert ir.usage is not None
    assert ir.usage.input_tokens == 1500
    assert ir.usage.output_tokens == 420
    assert ir.usage.cache_read_input_tokens == 200
    # secrets must not leak into IR
    assert "SECRET_MUST_NOT_APPEAR_IN_IR" not in json.dumps(ir.raw_external)
    assert "Authorization" not in json.dumps(ir.__dict__, default=str)


def test_map_fixture_still_works_via_normalizer():
    doc = _load("conversation_sample.json")
    mapped = map_mongo_conversation_doc(doc)
    assert mapped.trajectory.id.startswith("mongo-")
    assert "flaky auth" in mapped.trajectory.task_text.lower() or "JWT" in mapped.trajectory.task_text
    kinds = [s.kind for s in mapped.steps]
    assert StepKind.TOOL_CALL in kinds
    assert StepKind.TOOL_RESULT in kinds


def test_map_proxy_log_anthropic_blocks():
    doc = _load("conversation_proxy_log_sample.json")
    mapped = map_mongo_conversation_doc(doc)
    assert mapped.trajectory.external_refs.get("session_id") == (
        "8efd1fed-0000-4000-8000-000000000001"
    )
    assert "Fix flaky pytest" in mapped.trajectory.task_text
    assert "tools:" in mapped.trajectory.scaffold_text or "Read" in mapped.trajectory.scaffold_text
    kinds = [s.kind for s in mapped.steps]
    assert StepKind.TOOL_CALL in kinds
    assert StepKind.TOOL_RESULT in kinds
    assert mapped.trajectory.effort.totals.llm_tokens_in == 1500
    assert mapped.trajectory.effort.totals.llm_tokens_out == 420


def test_session_aggregate_picks_fullest_request():
    base = _load("conversation_proxy_log_sample.json")
    short = json.loads(json.dumps(base))
    short["_id"] = {"$oid": "aaaaaaaaaaaaaaaaaaaaaaaa"}
    short["request_body"]["messages"] = short["request_body"]["messages"][:1]
    short["timestamp"] = "2025-06-01T11:59:00.000000000Z"

    full = json.loads(json.dumps(base))
    full["_id"] = {"$oid": "bbbbbbbbbbbbbbbbbbbbbbbb"}
    full["timestamp"] = "2025-06-01T12:01:00.000000000Z"

    aggs = normalize_and_aggregate_docs([short, full])
    assert len(aggs) == 1
    agg = aggs[0]
    assert agg.session_id == base["session_id"]
    assert agg.request_count == 2
    assert agg.canonical.request_id == "bbbbbbbbbbbbbbbbbbbbbbbb"
    assert agg.canonical.message_count == 4
    assert agg.canonical.raw_external.get("kind") == "session_canonical"
    assert len(agg.canonical.raw_external.get("request_ids", [])) == 2


def test_session_aggregate_separate_sessions():
    a = _load("conversation_proxy_log_sample.json")
    b = json.loads(json.dumps(a))
    b["_id"] = {"$oid": "cccccccccccccccccccccccc"}
    b["session_id"] = "other-session-id"
    aggs = normalize_and_aggregate_docs([a, b])
    assert len(aggs) == 2
    ids = {x.session_id for x in aggs}
    assert a["session_id"] in ids
    assert "other-session-id" in ids


def test_normalize_doc_ignores_headers_even_if_present_on_copy():
    doc = _load("conversation_proxy_log_sample.json")
    ir = normalize_mongo_doc(doc)
    # re-normalize must not depend on headers for correctness
    del doc["request_headers"]
    ir2 = normalize_mongo_doc(doc)
    assert ir.session_id == ir2.session_id
    assert ir.message_count == ir2.message_count


def test_map_ir_legacy_from_aggregate_canonical():
    doc = _load("conversation_proxy_log_sample.json")
    aggs = normalize_and_aggregate_docs([doc])
    mapped = map_conversation_ir_legacy(aggs[0].canonical)
    assert mapped.trajectory.id.startswith("mongo-")
    assert mapped.steps
