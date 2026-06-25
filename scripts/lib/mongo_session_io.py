"""Shared helpers: Mongo URI, session aggregate export/import as JSONL records."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from yggdrasil.adapters.importers.mongo_normalize import (
    ConversationIR,
    IRMessage,
    IRUsage,
    SessionAggregate,
    normalize_and_aggregate_docs,
    normalize_mongo_doc,
    parse_dt,
)


def read_mongo_uri(creds_file: Path, env_uri: str | None = None) -> str | None:
    if env_uri and str(env_uri).strip():
        return str(env_uri).strip()
    if not creds_file.exists():
        return None
    for line in creds_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("MONGO_URI="):
            return stripped.split("=", 1)[1].strip().strip('"').strip("'")
        if stripped.startswith("mongodb://") or stripped.startswith("mongodb+srv://"):
            return stripped
    return None


def iter_live_mongo_docs(
    uri: str,
    *,
    limit: int | None = None,
    include_request_headers: bool = False,
) -> Iterator[dict[str, Any]]:
    """Yield proxy-log docs.

    By default strips ``request_headers`` (secrets). Set ``include_request_headers=True``
    only for in-process owner fingerprinting—callers must strip/redact before export.
    """
    from pymongo import MongoClient

    client = MongoClient(uri, serverSelectionTimeoutMS=15000)
    try:
        coll = client["claude_conversations"]["conversations"]
        proj = None if include_request_headers else {"request_headers": 0}
        cursor = coll.find({}, proj) if proj else coll.find({})
        if limit is not None:
            cursor = cursor.limit(limit)
        for doc in cursor:
            if "_id" in doc and not isinstance(doc["_id"], (str, dict)):
                doc = dict(doc)
                doc["_id"] = {"$oid": str(doc["_id"])}
            if isinstance(doc, dict) and "request_headers" in doc and not include_request_headers:
                doc = {k: v for k, v in doc.items() if k != "request_headers"}
            yield doc
    finally:
        client.close()


def iter_session_identities_from_mongo(uri: str, *, limit: int | None = None) -> Iterator[dict[str, Any]]:
    """Yield per-session safe identity (fingerprint only; headers never written out)."""
    from yggdrasil.adapters.importers.api_key_owners import identity_from_mongo_doc

    # Aggregate by session_id: first non-empty identity wins
    seen: dict[str, dict[str, Any]] = {}
    for doc in iter_live_mongo_docs(uri, limit=limit, include_request_headers=True):
        sid = doc.get("session_id") or str(doc.get("_id"))
        if sid in seen:
            continue
        ident = identity_from_mongo_doc(doc)
        ident["session_id"] = sid
        seen[sid] = ident
        yield ident


def _dt_iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _usage_to_dict(u: IRUsage | None) -> dict[str, Any] | None:
    if u is None:
        return None
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_creation_input_tokens": u.cache_creation_input_tokens,
        "cache_read_input_tokens": u.cache_read_input_tokens,
        "notes": u.notes,
    }


def _msg_to_dict(m: IRMessage) -> dict[str, Any]:
    return {
        "role": m.role,
        "content": m.content,
        "msg_id": m.msg_id,
        "timestamp": _dt_iso(m.timestamp),
        "tool_calls": m.tool_calls,
        "usage": m.usage,
    }


def ir_to_export_dict(ir: ConversationIR) -> dict[str, Any]:
    return {
        "session_id": ir.session_id,
        "request_id": ir.request_id,
        "model": ir.model,
        "created_at": _dt_iso(ir.created_at),
        "updated_at": _dt_iso(ir.updated_at),
        "title": ir.title,
        "project": ir.project,
        "tags": ir.tags,
        "system_text": ir.system_text,
        "tool_names": ir.tool_names,
        "messages": [_msg_to_dict(m) for m in ir.messages],
        "usage": _usage_to_dict(ir.usage),
        "source_shape": ir.source_shape,
        "raw_external": ir.raw_external,
    }


def ir_from_export_dict(d: dict[str, Any]) -> ConversationIR:
    msgs: list[IRMessage] = []
    for m in d.get("messages") or []:
        if not isinstance(m, dict):
            continue
        msgs.append(
            IRMessage(
                role=str(m.get("role") or "other"),
                content=m.get("content"),
                msg_id=m.get("msg_id"),
                timestamp=parse_dt(m.get("timestamp")),
                tool_calls=list(m.get("tool_calls") or []),
                usage=m.get("usage") if isinstance(m.get("usage"), dict) else None,
            )
        )
    usage_raw = d.get("usage")
    usage = None
    if isinstance(usage_raw, dict):
        usage = IRUsage(
            input_tokens=usage_raw.get("input_tokens"),
            output_tokens=usage_raw.get("output_tokens"),
            cache_creation_input_tokens=usage_raw.get("cache_creation_input_tokens"),
            cache_read_input_tokens=usage_raw.get("cache_read_input_tokens"),
            notes=usage_raw.get("notes"),
        )
    created = parse_dt(d.get("created_at")) or datetime.now(timezone.utc)
    updated = parse_dt(d.get("updated_at")) or created
    return ConversationIR(
        session_id=d.get("session_id"),
        request_id=str(d.get("request_id") or "unknown"),
        model=d.get("model"),
        created_at=created,
        updated_at=updated,
        title=d.get("title"),
        project=d.get("project"),
        tags=list(d.get("tags") or []),
        system_text=str(d.get("system_text") or ""),
        tool_names=list(d.get("tool_names") or []),
        messages=msgs,
        usage=usage,
        source_shape=d.get("source_shape") or "proxy_log",  # type: ignore[arg-type]
        raw_external=dict(d.get("raw_external") or {}),
    )


def session_record_from_aggregate(agg: SessionAggregate) -> dict[str, Any]:
    """One JSONL line: canonical session ready for external segmentation + embed."""
    return {
        "schema_version": 1,
        "record_kind": "mongo_session_canonical",
        "hydration_mode": "external_pre_embed",
        "note": (
            "Segmentation performed offline by importer/subagent, NOT by the agent that "
            "produced the trajectory. Production flow: agent uses skill → segments → MCP write."
        ),
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "session_id": agg.session_id,
        "request_count": agg.request_count,
        "request_ids": agg.request_ids,
        "canonical": ir_to_export_dict(agg.canonical),
        # Optional: caller/subagent may fill segments before embed step
        "segments": None,
        "segmentation_source": None,
    }


def write_sessions_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            n += 1
    return n


def iter_sessions_jsonl(path: Path) -> Iterator[dict[str, Any]]:
    """Yield one session record at a time (do not load full JSONL into RAM)."""
    with path.open(encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid json: {exc}") from exc


def count_jsonl_lines(path: Path) -> int:
    n = 0
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                n += 1
    return n


def export_sessions_from_mongo(
    uri: str,
    *,
    target_sessions: int = 100,
    max_docs: int | None = None,
    min_messages: int = 2,
    batch_docs: int = 500,
) -> list[dict[str, Any]]:
    """Pull docs in batches, aggregate incrementally (memory-safe vs loading all docs)."""
    doc_cap = max_docs if max_docs is not None else max(target_sessions * 8, target_sessions + 50)
    # session_id -> best SessionAggregate (keep fullest canonical only)
    best: dict[str, Any] = {}  # session_id -> SessionAggregate
    seen_docs = 0
    batch: list[dict[str, Any]] = []

    def _flush_batch() -> None:
        nonlocal batch
        if not batch:
            return
        for agg in normalize_and_aggregate_docs(batch):
            if agg.canonical.message_count < min_messages:
                continue
            sid = agg.session_id
            prev = best.get(sid)
            if prev is None or agg.canonical.message_count > prev.canonical.message_count:
                best[sid] = agg
        batch = []

    for doc in iter_live_mongo_docs(uri, limit=doc_cap):
        seen_docs += 1
        batch.append(doc)
        if len(batch) >= batch_docs:
            _flush_batch()
        if len(best) >= target_sessions * 3 and seen_docs >= target_sessions * 4:
            # enough diversity; can stop early if we have surplus candidates
            pass
    _flush_batch()

    aggs = list(best.values())
    aggs.sort(key=lambda a: (-a.canonical.message_count, a.session_id))
    aggs = aggs[:target_sessions]
    return [session_record_from_aggregate(a) for a in aggs]


def export_sessions_from_mongo_to_jsonl(
    uri: str,
    out_path: Path,
    *,
    target_sessions: int = 100,
    max_docs: int | None = None,
    min_messages: int = 2,
    batch_docs: int = 400,
) -> dict[str, Any]:
    """Stream-aggregate mongo → write only top target_sessions to disk (bounded RAM)."""
    records = export_sessions_from_mongo(
        uri,
        target_sessions=target_sessions,
        max_docs=max_docs,
        min_messages=min_messages,
        batch_docs=batch_docs,
    )
    n = write_sessions_jsonl(out_path, records)
    msg_counts = [len((r.get("canonical") or {}).get("messages") or []) for r in records]
    return {
        "sessions": n,
        "out": str(out_path),
        "msg_count_min": min(msg_counts) if msg_counts else 0,
        "msg_count_max": max(msg_counts) if msg_counts else 0,
        "msg_count_avg": round(sum(msg_counts) / len(msg_counts), 1) if msg_counts else 0,
    }


def record_to_ir(record: dict[str, Any]) -> ConversationIR:
    can = record.get("canonical")
    if not isinstance(can, dict):
        raise ValueError("session record missing canonical")
    return ir_from_export_dict(can)
