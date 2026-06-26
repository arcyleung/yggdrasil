"""Shared metrics for proxy-log sessions — multi-agent lanes (refined B′)."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def normalize_system_text(system: Any, *, max_chars: int = 120_000) -> str:
    """Stable-ish system prompt text for lane hashing."""
    if system is None:
        return ""
    if isinstance(system, str):
        s = system
    elif isinstance(system, list):
        parts: list[str] = []
        for block in system:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                parts.append(str(block["text"]))
            elif isinstance(block, str):
                parts.append(block)
            else:
                parts.append(json.dumps(block, sort_keys=True, default=str))
        s = "\n".join(parts)
    else:
        s = json.dumps(system, sort_keys=True, default=str)
    # light normalize: collapse runs of whitespace for hash stability
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s[:max_chars]


def system_hash(system: Any) -> str:
    return _sha16(normalize_system_text(system))


def lane_key(system: Any, model: str) -> str:
    """Refined B′: hash(normalized_system) + '|' + full model name."""
    return f"{system_hash(system)}|{model or 'unknown'}"


def _msg_fingerprint(messages: list[Any] | None) -> list[tuple[str | None, str]]:
    out: list[tuple[str | None, str]] = []
    if not isinstance(messages, list):
        return out
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        c = m.get("content")
        if isinstance(c, str):
            raw = c[:400]
        else:
            raw = str(c)[:400]
        h = hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()[:16]
        out.append((role if isinstance(role, str) else None, h))
    return out


def _messages_chars(messages: list[Any] | None) -> int:
    if not isinstance(messages, list):
        return 0
    n = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            n += len(c)
        elif isinstance(c, list):
            n += sum(len(str(x)) for x in c)
        else:
            n += len(str(c))
    return n


def _first_user_snippet(messages: list[Any] | None, n: int = 160) -> str:
    if not isinstance(messages, list):
        return ""
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c[:n].replace("\n", " ")
            return str(c)[:n]
    return ""


def _user_turn_texts(messages: list[Any] | None) -> list[str]:
    """User-visible turns (string content only) for subtask slicing heuristics."""
    out: list[str] = []
    if not isinstance(messages, list):
        return out
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        c = m.get("content")
        if isinstance(c, str) and c.strip():
            # skip obvious system-reminder-only blobs for slicing
            if c.strip().startswith("<system-reminder>") and len(c) < 500:
                continue
            out.append(c.strip())
        elif isinstance(c, list):
            texts = [
                str(b.get("text", ""))
                for b in c
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
            ]
            joined = "\n".join(texts).strip()
            if joined and not (
                joined.startswith("<system-reminder>") and len(joined) < 500
            ):
                out.append(joined)
    return out


def _model_from_doc(doc: dict[str, Any]) -> str:
    rb = doc.get("request_body") if isinstance(doc.get("request_body"), dict) else {}
    m = rb.get("model") or doc.get("model")
    if not m and isinstance(doc.get("response_body"), dict):
        m = doc["response_body"].get("model")
    return str(m) if m else "unknown"


def _model_family(model: str) -> str:
    ml = model.lower()
    if "haiku" in ml:
        return "haiku"
    if "opus" in ml:
        return "opus"
    if "sonnet" in ml:
        return "sonnet"
    if "claude" in ml:
        return "claude_other"
    return "other"


def _doc_ts(doc: dict[str, Any]) -> datetime | None:
    for k in ("created_at", "timestamp", "updated_at"):
        v = doc.get(k)
        if isinstance(v, datetime):
            return v
        if isinstance(v, str) and v:
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except Exception:
                pass
    return None


@dataclass
class DocView:
    index: int
    oid: str
    model: str
    family: str
    system_hash: str
    lane_key: str
    nmsg: int
    chars: int
    fp: list[tuple[str | None, str]]
    first_user: str
    user_turns: list[str]
    ts: str | None


@dataclass
class LanePick:
    lane_key: str
    system_hash: str
    model: str
    family: str
    nmsg: int
    chars: int
    oid: str
    index: int
    first_user: str
    n_user_turns: int
    # within-lane decomposition hints (on maximizer snapshot only)
    suggested_slices: int = 1
    slice_method: str = "single_snapshot"


@dataclass
class SessionAudit:
    session_id: str
    n_docs: int
    models: dict[str, int]
    families: dict[str, int]
    multi_model: bool
    multi_family: bool
    multi_lane_bprime: bool
    max_nmsg: int
    max_chars: int
    canonical_index: int
    canonical_model: str
    canonical_family: str
    canonical_lane_key: str
    canonical_nmsg: int
    canonical_chars: int
    canonical_first_user: str
    last_index: int
    last_model: str
    last_nmsg: int
    prefixes_of_canonical: int
    canonical_is_prefix_of_last: bool
    last_is_prefix_of_canonical: bool
    canonical_model_ne_last_model: bool
    canonical_not_global_max_chars: bool
    lanes_family_only: int
    lanes_bprime: int
    lanes: list[LanePick] = field(default_factory=list)
    extra_lanes_beyond_canonical: int = 0
    extra_lane_chars_total: int = 0
    retrieval_units_single: int = 1
    retrieval_units_family_lane: int = 1
    retrieval_units_bprime: int = 1
    # within maximizer lanes: sum of suggested slices
    total_suggested_slices_on_lane_max: int = 0
    distinct_lane_first_users: int = 0

    def verdict_flags(self) -> list[str]:
        flags: list[str] = []
        if self.multi_lane_bprime and self.lanes_bprime > 1:
            flags.append("bprime_multiple_lanes")
        if self.lanes_bprime > self.lanes_family_only:
            flags.append("bprime_splits_same_family_by_system")
        if self.canonical_model_ne_last_model:
            flags.append("canonical_model_differs_from_last_doc")
        if self.extra_lanes_beyond_canonical > 0:
            flags.append("multi_lane_would_add_beyond_max_msg_doc")
        if self.canonical_not_global_max_chars:
            flags.append("another_doc_has_more_chars_than_canonical")
        if not self.canonical_is_prefix_of_last and not self.last_is_prefix_of_canonical:
            flags.append("canonical_and_last_not_prefix_related")
        if self.distinct_lane_first_users > 1:
            flags.append("lanes_have_distinct_user_intents")
        if self.total_suggested_slices_on_lane_max > self.lanes_bprime:
            flags.append("within_lane_subtask_slices_detected")
        return flags


_GOAL_HINT = re.compile(
    r"\b(implement|fix|add|remove|refactor|investigate|debug|write|create|update|migrate|test|deploy|please\b)",
    re.I,
)


def suggest_subtask_slices(user_turns: list[str], *, min_turn_chars: int = 40) -> tuple[int, str]:
    """Heuristic: how many subtask/outcome slices might live inside one lane maximizer.

    Not a full segmenter — signals whether further decomposition is warranted.
    Methods (best effort):
    - count substantial user turns with goal-like language
    - if only 0–1, return 1 (single snapshot is enough for that lane)
    """
    substantial = [t for t in user_turns if len(t) >= min_turn_chars]
    if len(substantial) <= 1:
        return 1, "single_or_trivial_user_turns"
    goalish = [t for t in substantial if _GOAL_HINT.search(t)]
    if len(goalish) >= 2:
        # consecutive goalish turns → candidate slice boundaries
        return min(len(goalish), 12), "goalish_user_turns"
    if len(substantial) >= 3:
        return min(len(substantial) // 2, 8), "many_user_turns_half"
    return 1, "single_snapshot"


def analyze_session_docs(session_id: str, docs: list[dict[str, Any]]) -> SessionAudit:
    views: list[DocView] = []
    for i, d in enumerate(docs):
        rb = d.get("request_body") if isinstance(d.get("request_body"), dict) else {}
        msgs = rb.get("messages") if isinstance(rb, dict) else None
        if not isinstance(msgs, list):
            msgs = []
        model = _model_from_doc(d)
        sys_obj = rb.get("system") if isinstance(rb, dict) else None
        sh = system_hash(sys_obj)
        lk = lane_key(sys_obj, model)
        ts = _doc_ts(d)
        oid = d.get("_id")
        if isinstance(oid, dict) and "$oid" in oid:
            oid_s = str(oid["$oid"])
        else:
            oid_s = str(oid)
        views.append(
            DocView(
                index=i,
                oid=oid_s,
                model=model,
                family=_model_family(model),
                system_hash=sh,
                lane_key=lk,
                nmsg=len(msgs),
                chars=_messages_chars(msgs),
                fp=_msg_fingerprint(msgs),
                first_user=_first_user_snippet(msgs),
                user_turns=_user_turn_texts(msgs),
                ts=ts.isoformat() if ts else None,
            )
        )

    empty = SessionAudit(
        session_id=session_id,
        n_docs=0,
        models={},
        families={},
        multi_model=False,
        multi_family=False,
        multi_lane_bprime=False,
        max_nmsg=0,
        max_chars=0,
        canonical_index=-1,
        canonical_model="unknown",
        canonical_family="other",
        canonical_lane_key="",
        canonical_nmsg=0,
        canonical_chars=0,
        canonical_first_user="",
        last_index=-1,
        last_model="unknown",
        last_nmsg=0,
        prefixes_of_canonical=0,
        canonical_is_prefix_of_last=False,
        last_is_prefix_of_canonical=False,
        canonical_model_ne_last_model=False,
        canonical_not_global_max_chars=False,
        lanes_family_only=0,
        lanes_bprime=0,
    )
    if not views:
        return empty

    canonical = max(views, key=lambda v: (v.nmsg, v.chars, v.index))
    last = views[-1]

    def is_prefix(a: list, b: list) -> bool:
        return len(a) <= len(b) and b[: len(a)] == a

    pref_can = sum(1 for v in views if v.fp and is_prefix(v.fp, canonical.fp) and v.index != canonical.index)

    # Family-only lanes (old B)
    by_fam: dict[str, list[DocView]] = defaultdict(list)
    for v in views:
        by_fam[v.family].append(v)
    n_fam_lanes = len(by_fam)

    # B′ lanes: system_hash|full_model
    by_lane: dict[str, list[DocView]] = defaultdict(list)
    for v in views:
        by_lane[v.lane_key].append(v)

    lanes: list[LanePick] = []
    total_slices = 0
    for lk, lst in by_lane.items():
        best = max(lst, key=lambda v: (v.nmsg, v.chars, v.index))
        ns, method = suggest_subtask_slices(best.user_turns)
        total_slices += ns
        lanes.append(
            LanePick(
                lane_key=lk,
                system_hash=best.system_hash,
                model=best.model,
                family=best.family,
                nmsg=best.nmsg,
                chars=best.chars,
                oid=best.oid,
                index=best.index,
                first_user=best.first_user,
                n_user_turns=len(best.user_turns),
                suggested_slices=ns,
                slice_method=method,
            )
        )
    lanes.sort(key=lambda L: (-L.nmsg, -L.chars))

    can_key = canonical.lane_key
    extra = [L for L in lanes if L.lane_key != can_key]
    first_users = {L.first_user[:100] for L in lanes if L.first_user}

    models = Counter(v.model for v in views)
    families = Counter(v.family for v in views)
    max_chars = max(v.chars for v in views)

    return SessionAudit(
        session_id=session_id,
        n_docs=len(views),
        models=dict(models),
        families=dict(families),
        multi_model=len(models) > 1,
        multi_family=len(families) > 1,
        multi_lane_bprime=len(by_lane) > 1,
        max_nmsg=max(v.nmsg for v in views),
        max_chars=max_chars,
        canonical_index=canonical.index,
        canonical_model=canonical.model,
        canonical_family=canonical.family,
        canonical_lane_key=canonical.lane_key,
        canonical_nmsg=canonical.nmsg,
        canonical_chars=canonical.chars,
        canonical_first_user=canonical.first_user,
        last_index=last.index,
        last_model=last.model,
        last_nmsg=last.nmsg,
        prefixes_of_canonical=pref_can,
        canonical_is_prefix_of_last=bool(canonical.fp and last.fp and is_prefix(canonical.fp, last.fp)),
        last_is_prefix_of_canonical=bool(canonical.fp and last.fp and is_prefix(last.fp, canonical.fp)),
        canonical_model_ne_last_model=canonical.model != last.model,
        canonical_not_global_max_chars=canonical.chars < max_chars,
        lanes_family_only=n_fam_lanes,
        lanes_bprime=len(lanes),
        lanes=lanes,
        extra_lanes_beyond_canonical=len(extra),
        extra_lane_chars_total=sum(L.chars for L in extra),
        retrieval_units_single=1,
        retrieval_units_family_lane=n_fam_lanes,
        retrieval_units_bprime=len(lanes),
        total_suggested_slices_on_lane_max=total_slices,
        distinct_lane_first_users=len(first_users),
    )


def audit_to_dict(a: SessionAudit) -> dict[str, Any]:
    return {
        "session_id": a.session_id,
        "n_docs": a.n_docs,
        "models": a.models,
        "families": a.families,
        "multi_model": a.multi_model,
        "multi_family": a.multi_family,
        "multi_lane_bprime": a.multi_lane_bprime,
        "policy": "B_prime_system_hash_pipe_full_model",
        "canonical": {
            "index": a.canonical_index,
            "model": a.canonical_model,
            "family": a.canonical_family,
            "lane_key": a.canonical_lane_key,
            "nmsg": a.canonical_nmsg,
            "chars": a.canonical_chars,
            "first_user": a.canonical_first_user,
        },
        "last_doc": {"index": a.last_index, "model": a.last_model, "nmsg": a.last_nmsg},
        "prefix": {
            "of_canonical": a.prefixes_of_canonical,
            "canonical_is_prefix_of_last": a.canonical_is_prefix_of_last,
            "last_is_prefix_of_canonical": a.last_is_prefix_of_canonical,
        },
        "lane_counts": {
            "family_only_B": a.lanes_family_only,
            "bprime_system_model": a.lanes_bprime,
            "suggested_subtask_slices_sum": a.total_suggested_slices_on_lane_max,
        },
        "lanes": [asdict(L) for L in a.lanes],
        "extra_lanes_beyond_canonical": a.extra_lanes_beyond_canonical,
        "extra_lane_chars_total": a.extra_lane_chars_total,
        "retrieval_units": {
            "single_canonical": a.retrieval_units_single,
            "family_lane_B": a.retrieval_units_family_lane,
            "bprime_lanes": a.retrieval_units_bprime,
            "bprime_x_heuristic_slices": a.total_suggested_slices_on_lane_max,
        },
        "distinct_lane_first_users": a.distinct_lane_first_users,
        "flags": a.verdict_flags(),
        "problem_score": len(a.verdict_flags()),
    }


def mongo_uri_from_creds(path: str = "mongo_creds.txt") -> str | None:
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return None
    for line in p.read_text().splitlines():
        line = line.strip()
        if line.startswith("mongodb"):
            return line
        if line.startswith("MONGO_URI="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def fetch_session_docs(uri: str, session_id: str) -> list[dict[str, Any]]:
    from pymongo import MongoClient

    cl = MongoClient(uri, serverSelectionTimeoutMS=20000)
    try:
        coll = cl["claude_conversations"]["conversations"]
        docs = list(coll.find({"session_id": session_id}, {"request_headers": 0}).sort([("_id", 1)]))
        for d in docs:
            if "_id" in d and not isinstance(d["_id"], (str, dict)):
                d["_id"] = {"$oid": str(d["_id"])}
        return docs
    finally:
        cl.close()
