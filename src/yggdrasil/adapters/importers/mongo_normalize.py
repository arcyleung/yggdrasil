"""Normalize Mongo conversation docs (proxy-log or fixture) into ConversationIR."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Literal


SourceShape = Literal["proxy_log", "fixture_v1"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, dict) and "$date" in raw:
        return parse_dt(raw["$date"])
    if isinstance(raw, (int, float)):
        ts = float(raw)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def oid_str(doc_id: Any) -> str:
    if isinstance(doc_id, dict) and "$oid" in doc_id:
        return str(doc_id["$oid"])
    return str(doc_id)


@dataclass
class IRUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    notes: str | None = None

    def merge_max(self, other: "IRUsage | None") -> "IRUsage":
        if other is None:
            return self
        return IRUsage(
            input_tokens=_max_int(self.input_tokens, other.input_tokens),
            output_tokens=_max_int(self.output_tokens, other.output_tokens),
            cache_creation_input_tokens=_max_int(
                self.cache_creation_input_tokens, other.cache_creation_input_tokens
            ),
            cache_read_input_tokens=_max_int(
                self.cache_read_input_tokens, other.cache_read_input_tokens
            ),
            notes=self.notes or other.notes,
        )


def _max_int(a: int | None, b: int | None) -> int | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


@dataclass
class IRMessage:
    role: str
    content: Any  # str | list[dict] — preserve blocks for mapper
    msg_id: str | None = None
    timestamp: datetime | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # fixture OpenAI style
    usage: dict[str, Any] | None = None


@dataclass
class ConversationIR:
    """Internal representation; never copy request_headers / Authorization here."""

    session_id: str | None
    request_id: str
    model: str | None
    created_at: datetime
    updated_at: datetime
    title: str | None
    project: str | None
    tags: list[str]
    system_text: str
    tool_names: list[str]
    messages: list[IRMessage]
    usage: IRUsage | None
    source_shape: SourceShape
    raw_external: dict[str, Any] = field(default_factory=dict)

    @property
    def message_count(self) -> int:
        return len(self.messages)


def detect_source_shape(doc: dict[str, Any]) -> SourceShape:
    if isinstance(doc.get("request_body"), dict) and "messages" in (doc.get("request_body") or {}):
        return "proxy_log"
    if "messages" in doc and isinstance(doc.get("messages"), list):
        return "fixture_v1"
    if isinstance(doc.get("request_body"), dict):
        return "proxy_log"
    return "fixture_v1"


def _join_system_text(system: Any, *, max_chars: int = 8000) -> str:
    if system is None:
        return ""
    if isinstance(system, str):
        return system[:max_chars]
    if isinstance(system, list):
        parts: list[str] = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                parts.append(str(block["text"]))
            elif isinstance(block, str):
                parts.append(block)
        text = "\n".join(parts)
        return text[:max_chars]
    return str(system)[:max_chars]


def _tool_names_from_body(tools: Any) -> list[str]:
    if not isinstance(tools, list):
        return []
    names: list[str] = []
    for t in tools:
        if isinstance(t, dict) and t.get("name"):
            names.append(str(t["name"]))
    return names


def _usage_from_dict(usage: Any) -> IRUsage | None:
    if not isinstance(usage, dict) or not usage:
        return None
    tin = usage.get("input_tokens") or usage.get("prompt_tokens")
    tout = usage.get("output_tokens") or usage.get("completion_tokens")
    cache_c = usage.get("cache_creation_input_tokens")
    cache_r = usage.get("cache_read_input_tokens")
    if tin is None and tout is None and cache_c is None and cache_r is None:
        return None
    notes_parts: list[str] = []
    if cache_c is not None or cache_r is not None:
        notes_parts.append(
            f"cache_creation_input_tokens={cache_c}, cache_read_input_tokens={cache_r}"
        )
    return IRUsage(
        input_tokens=int(tin) if tin is not None else None,
        output_tokens=int(tout) if tout is not None else None,
        cache_creation_input_tokens=int(cache_c) if cache_c is not None else None,
        cache_read_input_tokens=int(cache_r) if cache_r is not None else None,
        notes="; ".join(notes_parts) if notes_parts else None,
    )


def _usage_from_streaming_events(response: Any) -> IRUsage | None:
    if not isinstance(response, dict):
        return None
    events = response.get("streaming_events") or []
    if not isinstance(events, list):
        return None
    acc = IRUsage()
    found = False
    for ev in events:
        if not isinstance(ev, dict):
            continue
        data = ev.get("data") if isinstance(ev.get("data"), dict) else ev
        if not isinstance(data, dict):
            continue
        etype = data.get("type")
        usage = None
        if etype == "message_start":
            msg = data.get("message") or {}
            if isinstance(msg, dict):
                usage = msg.get("usage")
        elif etype == "message_delta":
            usage = data.get("usage")
        if isinstance(usage, dict):
            part = _usage_from_dict(usage)
            if part:
                acc = acc.merge_max(part)
                found = True
    return acc if found else None


def _normalize_messages_fixture(raw_messages: list[Any]) -> list[IRMessage]:
    out: list[IRMessage] = []
    for msg in raw_messages:
        if not isinstance(msg, dict):
            continue
        tool_calls = msg.get("tool_calls") or []
        if not isinstance(tool_calls, list):
            tool_calls = []
        out.append(
            IRMessage(
                role=str(msg.get("role") or "other"),
                content=msg.get("content"),
                msg_id=str(msg["id"]) if msg.get("id") is not None else None,
                timestamp=parse_dt(msg.get("created_at") or msg.get("timestamp")),
                tool_calls=[tc for tc in tool_calls if isinstance(tc, dict)],
                usage=msg.get("usage") if isinstance(msg.get("usage"), dict) else None,
            )
        )
    return out


def _normalize_messages_proxy(raw_messages: list[Any]) -> list[IRMessage]:
    """Anthropic-style messages: content is always list of blocks in live data."""
    out: list[IRMessage] = []
    for msg in raw_messages:
        if not isinstance(msg, dict):
            continue
        out.append(
            IRMessage(
                role=str(msg.get("role") or "other"),
                content=msg.get("content"),
                msg_id=str(msg["id"]) if msg.get("id") is not None else None,
                timestamp=parse_dt(msg.get("created_at") or msg.get("timestamp")),
                tool_calls=[],
                usage=None,
            )
        )
    return out


def _normalize_fixture_v1(doc: dict[str, Any]) -> ConversationIR:
    request_id = oid_str(doc.get("_id", "unknown"))
    metadata = doc.get("metadata") if isinstance(doc.get("metadata"), dict) else {}
    title = str(metadata.get("title") or "").strip() or None
    project = str(metadata.get("project") or "").strip() or None
    tags_raw = metadata.get("tags") or []
    if not isinstance(tags_raw, list):
        tags_raw = [tags_raw]
    tags = [str(t) for t in tags_raw if t is not None]
    if "mongo_import" not in tags:
        tags = ["mongo_import", *tags]

    created_at = parse_dt(metadata.get("created_at") or metadata.get("createdAt")) or _utcnow()
    updated_at = (
        parse_dt(metadata.get("updated_at") or metadata.get("updatedAt")) or created_at
    )

    messages = _normalize_messages_fixture(list(doc.get("messages") or []))
    usage = _usage_from_dict(doc.get("usage"))
    if usage is None:
        for msg in reversed(messages):
            if msg.usage:
                usage = _usage_from_dict(msg.usage)
                if usage:
                    break

    return ConversationIR(
        session_id=request_id,  # synthetic: one fixture doc = one logical session
        request_id=request_id,
        model=None,
        created_at=created_at,
        updated_at=updated_at,
        title=title,
        project=project,
        tags=tags,
        system_text="",
        tool_names=[],
        messages=messages,
        usage=usage,
        source_shape="fixture_v1",
        raw_external={
            "source": "mongo",
            "db": "claude_conversations",
            "collection": "conversations",
            "id": request_id,
            "kind": "request_doc",
        },
    )


def _normalize_proxy_log(doc: dict[str, Any]) -> ConversationIR:
    request_id = oid_str(doc.get("_id", "unknown"))
    session_id = doc.get("session_id")
    if session_id is not None:
        session_id = str(session_id)

    body = doc.get("request_body") if isinstance(doc.get("request_body"), dict) else {}
    model = doc.get("model") or body.get("model")
    if model is not None:
        model = str(model)

    created_at = parse_dt(doc.get("timestamp")) or parse_dt(doc.get("created_at")) or _utcnow()
    updated_at = parse_dt(doc.get("created_at")) or created_at

    system_text = _join_system_text(body.get("system"))
    tool_names = _tool_names_from_body(body.get("tools"))

    tags = ["mongo_import"]
    if model:
        # coarse model family tag without full id noise
        family = model.split("-")[0] if model else ""
        if family:
            tags.append(f"model:{family}")
        tags.append("proxy_log")

    messages = _normalize_messages_proxy(list(body.get("messages") or []))
    usage = _usage_from_streaming_events(doc.get("response"))
    if usage is None:
        usage = _usage_from_dict(doc.get("usage"))

    return ConversationIR(
        session_id=session_id,
        request_id=request_id,
        model=model,
        created_at=created_at,
        updated_at=updated_at,
        title=None,
        project=None,
        tags=tags,
        system_text=system_text,
        tool_names=tool_names,
        messages=messages,
        usage=usage,
        source_shape="proxy_log",
        raw_external={
            "source": "mongo",
            "db": "claude_conversations",
            "collection": "conversations",
            "id": request_id,
            "session_id": session_id,
            "kind": "request_doc",
            "file_id": doc.get("file_id"),
            "user_id": doc.get("user_id"),
        },
    )


def normalize_mongo_doc(doc: dict[str, Any]) -> ConversationIR:
    """Convert a raw Mongo doc to ConversationIR. Never copies request_headers."""
    # Defensive: work on a shallow copy without headers
    safe = {k: v for k, v in doc.items() if k != "request_headers"}
    shape = detect_source_shape(safe)
    if shape == "proxy_log":
        return _normalize_proxy_log(safe)
    return _normalize_fixture_v1(safe)


@dataclass
class SessionAggregate:
    """One logical session: request IRs ordered by time; canonical = fullest transcript."""

    session_id: str
    requests: list[ConversationIR]
    canonical: ConversationIR

    @property
    def request_ids(self) -> list[str]:
        return [r.request_id for r in self.requests]

    @property
    def request_count(self) -> int:
        return len(self.requests)


def _session_key(ir: ConversationIR) -> str:
    if ir.session_id:
        return ir.session_id
    return ir.request_id


def _pick_canonical(requests: list[ConversationIR]) -> ConversationIR:
    """Prefer max message_count; tie-break by latest updated_at then request_id."""
    if len(requests) == 1:
        return requests[0]
    return max(
        requests,
        key=lambda r: (r.message_count, r.updated_at or r.created_at, r.request_id),
    )


def _merge_session_usage(requests: list[ConversationIR]) -> IRUsage | None:
    acc: IRUsage | None = None
    for r in requests:
        if r.usage is None:
            continue
        acc = r.usage if acc is None else acc.merge_max(r.usage)
    return acc


def aggregate_session_irs(irs: Iterable[ConversationIR]) -> list[SessionAggregate]:
    """Group normalized request IRs by session_id; emit one aggregate per session."""
    buckets: dict[str, list[ConversationIR]] = {}
    for ir in irs:
        key = _session_key(ir)
        buckets.setdefault(key, []).append(ir)

    aggregates: list[SessionAggregate] = []
    for session_id, reqs in buckets.items():
        reqs_sorted = sorted(reqs, key=lambda r: (r.created_at, r.request_id))
        canonical = _pick_canonical(reqs_sorted)
        # Enrich canonical lineage without mutating request_headers anywhere
        merged_usage = _merge_session_usage(reqs_sorted)
        if merged_usage is not None:
            canonical = ConversationIR(
                session_id=canonical.session_id or session_id,
                request_id=canonical.request_id,
                model=canonical.model,
                created_at=min(r.created_at for r in reqs_sorted),
                updated_at=max(r.updated_at for r in reqs_sorted),
                title=canonical.title,
                project=canonical.project,
                tags=list(dict.fromkeys([*canonical.tags, "session_aggregated"])),
                system_text=canonical.system_text,
                tool_names=list(dict.fromkeys(canonical.tool_names)),
                messages=canonical.messages,
                usage=merged_usage,
                source_shape=canonical.source_shape,
                raw_external={
                    **canonical.raw_external,
                    "session_id": session_id,
                    "request_ids": [r.request_id for r in reqs_sorted],
                    "request_count": len(reqs_sorted),
                    "canonical_request_id": canonical.request_id,
                    "kind": "session_canonical",
                },
            )
        else:
            canonical = ConversationIR(
                session_id=canonical.session_id or session_id,
                request_id=canonical.request_id,
                model=canonical.model,
                created_at=min(r.created_at for r in reqs_sorted),
                updated_at=max(r.updated_at for r in reqs_sorted),
                title=canonical.title,
                project=canonical.project,
                tags=list(dict.fromkeys([*canonical.tags, "session_aggregated"])),
                system_text=canonical.system_text,
                tool_names=list(dict.fromkeys(canonical.tool_names)),
                messages=canonical.messages,
                usage=canonical.usage,
                source_shape=canonical.source_shape,
                raw_external={
                    **canonical.raw_external,
                    "session_id": session_id,
                    "request_ids": [r.request_id for r in reqs_sorted],
                    "request_count": len(reqs_sorted),
                    "canonical_request_id": canonical.request_id,
                    "kind": "session_canonical",
                },
            )
        aggregates.append(
            SessionAggregate(session_id=session_id, requests=reqs_sorted, canonical=canonical)
        )
    return aggregates


def normalize_and_aggregate_docs(docs: Iterable[dict[str, Any]]) -> list[SessionAggregate]:
    """Phase-1 pipeline entry: normalize each doc, then session-aggregate."""
    irs = [normalize_mongo_doc(d) for d in docs]
    return aggregate_session_irs(irs)


def ir_message_text(msg: IRMessage) -> str:
    """Extract display/search text from an IR message (blocks or string)."""
    content = msg.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text" and block.get("text"):
                    parts.append(str(block["text"]))
                elif block.get("type") == "tool_result":
                    tr = block.get("content")
                    if isinstance(tr, str):
                        parts.append(tr)
                    elif isinstance(tr, list):
                        for sub in tr:
                            if isinstance(sub, dict) and sub.get("text"):
                                parts.append(str(sub["text"]))
                elif "text" in block:
                    parts.append(str(block["text"]))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return str(content or "")
