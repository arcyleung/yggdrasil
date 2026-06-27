#!/usr/bin/env python3
"""Backfill SQLite schema v4 + Qdrant payloads: owner from user_mapping (full API keys) + Mongo.

Mongo often stores *truncated* Authorization values. We resolve owners by:
1. Exact full-key match (sk-… from user_mapping.yaml)
2. Fingerprint of full key / Bearer+key (same as importer)
3. Unique suffix/prefix match against truncated headers (last 6–10 chars of key)
4. Precomputed session_id → owner map from a single Mongo scan (fast path)
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from yggdrasil.adapters.importers.api_key_owners import (
    extract_auth_raw,
    fingerprint_auth_value,
    load_key_name_map,
    owner_map_from_key_name_map,
)
from yggdrasil.adapters.qdrant_index import QdrantIndex, point_id_for_trajectory
from yggdrasil.config import load_config
from yggdrasil.mcp.app_context import AppContext
from yggdrasil.ports.vector_index import payload_from_trajectory


def _strip_bearer(raw: str) -> str:
    return re.sub(r"(?i)^bearer\s+", "", str(raw).strip()).strip()


def build_owner_resolvers(key_map: dict[str, str]) -> dict:
    """Indexes for resolving truncated or full auth material → owner.

    Mongo stores headers like ``Bearer sk-L...0I7u`` (first char after sk- + ... + last 4).
    Full keys live in user_mapping.yaml and are matched via that pattern or exact key.
    """
    fp_map = owner_map_from_key_name_map(key_map)
    full_key_to_owner = {str(k).strip(): str(v).strip() for k, v in key_map.items() if k and v}
    by_suffix: dict[str, set[str]] = defaultdict(set)
    # (first_char_after_sk-, last4) -> owners  — matches Mongo truncation style
    sk_trunc: dict[tuple[str, str], set[str]] = defaultdict(set)
    for key, owner in full_key_to_owner.items():
        tok = _strip_bearer(key)
        for n in (10, 8, 6, 4):
            if len(tok) >= n:
                by_suffix[tok[-n:]].add(owner)
        if tok.startswith("sk-") and len(tok) >= 8:
            sk_trunc[(tok[3], tok[-4:])].add(owner)
        for variant in (key, tok, f"Bearer {tok}", f"bearer {tok}"):
            fp = fingerprint_auth_value(variant)
            if fp:
                fp_map[fp] = owner
    return {
        "full": full_key_to_owner,
        "fp": fp_map,
        "by_suffix": dict(by_suffix),
        "sk_trunc": {k: v for k, v in sk_trunc.items()},
    }


def resolve_owner_from_auth(raw: str | None, resolvers: dict) -> tuple[str | None, str | None]:
    """Return (owner, method) using full keys from user_mapping.yaml."""
    if not raw or not str(raw).strip():
        return None, None
    s = str(raw).strip()
    tok = _strip_bearer(s)
    full = resolvers["full"]
    # 1) exact full API key from mapping
    if tok in full:
        return full[tok], "full_key"
    if s in full:
        return full[s], "full_key_raw"
    # 2) fingerprint of whatever was stored
    fp = fingerprint_auth_value(s) or fingerprint_auth_value(tok)
    if fp and fp in resolvers["fp"]:
        return resolvers["fp"][fp], "fingerprint"
    # 3) Mongo truncation: Bearer sk-X...YYYY  (ASCII or unicode ellipsis)
    norm = tok.replace("\u2026", "...").replace("…", "...")
    m = re.match(r"(?i)^sk-(.)\.\.\.(.{4})$", norm)
    if not m:
        m = re.match(r"(?i)^sk-(.)\.{3}(.{4})$", norm)
    if m:
        pair = (m.group(1), m.group(2))
        owners = resolvers["sk_trunc"].get(pair)
        if owners and len(owners) == 1:
            return next(iter(owners)), "sk_trunc_pattern"
        if owners and len(owners) > 1:
            # ambiguous — try longer unique suffix from full keys
            pass
    # 4) unique suffix only
    for n in (10, 8, 6, 4):
        if len(tok) >= n:
            owners = resolvers["by_suffix"].get(tok[-n:])
            if owners and len(owners) == 1:
                return next(iter(owners)), f"suffix_{n}"
    # 5) ellipsis forms with arbitrary head
    if "..." in norm:
        tail = norm.rsplit("...", 1)[-1]
        head = norm.split("...", 1)[0]
        if len(tail) >= 4 and head.startswith("sk-") and len(head) >= 4:
            pair = (head[3], tail[-4:])
            owners = resolvers["sk_trunc"].get(pair)
            if owners and len(owners) == 1:
                return next(iter(owners)), "sk_trunc_ellipsis"
        for n in (10, 8, 6, 4):
            if len(tail) >= n:
                owners = resolvers["by_suffix"].get(tail[-n:])
                if owners and len(owners) == 1:
                    return next(iter(owners)), f"ellipsis_suffix_{n}"
    return None, None


def build_session_owner_cache(mongo_uri: str, resolvers: dict) -> dict[str, dict]:
    """One pass over proxy docs: session_id → {owner, occurred_at, method}."""
    from pymongo import MongoClient

    cache: dict[str, dict] = {}
    cl = MongoClient(mongo_uri, serverSelectionTimeoutMS=30000)
    col = cl["claude_conversations"]["conversations"]
    # Project minimal fields; may be large collection — stream
    cursor = col.find(
        {},
        {"session_id": 1, "request_headers": 1, "created_at": 1, "updated_at": 1, "user_id": 1},
        no_cursor_timeout=True,
    )
    n = 0
    try:
        for doc in cursor:
            n += 1
            sid = doc.get("session_id")
            if not sid:
                continue
            sid = str(sid)
            if sid in cache and cache[sid].get("owner"):
                # still upgrade time if missing
                pass
            else:
                auth = extract_auth_raw(doc.get("request_headers"))
                owner, method = resolve_owner_from_auth(auth, resolvers)
                entry = cache.get(sid) or {}
                if owner:
                    entry["owner"] = owner
                    entry["method"] = method
                    if auth:
                        entry["auth_fingerprint"] = fingerprint_auth_value(auth)
                for k in ("created_at", "updated_at"):
                    if doc.get(k) is not None and "occurred_at" not in entry:
                        entry["occurred_at"] = doc[k]
                # user_id sometimes encodes key prefix
                uid = doc.get("user_id")
                if not entry.get("owner") and isinstance(uid, str) and uid.startswith("sk-"):
                    o2, m2 = resolve_owner_from_auth(uid, resolvers)
                    if o2:
                        entry["owner"] = o2
                        entry["method"] = m2 or "user_id"
                if entry:
                    cache[sid] = entry
            if n % 5000 == 0:
                print(json.dumps({"mongo_scan": n, "sessions_cached": len(cache)}), flush=True)
    finally:
        cursor.close()
        cl.close()
    print(json.dumps({"mongo_scan_done": n, "sessions_cached": len(cache)}), flush=True)
    return cache


def _owner_from_traj(traj, resolvers: dict) -> str | None:
    refs = traj.external_refs or {}
    o = refs.get("owner") or refs.get("user")
    if o and str(o).strip().lower() not in ("unknown", "none", ""):
        return str(o).strip()
    for t in traj.tags or []:
        if isinstance(t, str) and t.startswith("owner:"):
            name = t.split(":", 1)[1].strip()
            if name and name.lower() != "unknown":
                return name
    for key in ("auth_fingerprint", "identity_key", "api_key_fingerprint"):
        fp = refs.get(key)
        if fp and fp in resolvers["fp"]:
            return resolvers["fp"][fp]
    # stored full key unlikely but try
    for key in ("api_key", "authorization"):
        if refs.get(key):
            owner, _ = resolve_owner_from_auth(str(refs[key]), resolvers)
            if owner:
                return owner
    return None


def _dt_parse(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--live-mongo", action="store_true", help="Scan Mongo once for session→owner cache")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--progress-every", type=int, default=200)
    ap.add_argument("--only-missing-owner", action="store_true", help="Skip rows that already have owner")
    args = ap.parse_args()

    cfg = load_config()
    key_map = load_key_name_map(cfg.user_mapping_path or (ROOT / "user_mapping.yaml"))
    if not key_map:
        # also try KEY_NAME_MAP path
        key_map = load_key_name_map(None)
    print(json.dumps({"user_mapping_keys": len(key_map)}), flush=True)
    resolvers = build_owner_resolvers(key_map)

    mongo_uri = cfg.mongo_uri
    if not mongo_uri and (ROOT / "mongo_creds.txt").is_file():
        mongo_uri = (ROOT / "mongo_creds.txt").read_text().strip().splitlines()[0].strip()

    session_cache: dict[str, dict] = {}
    if args.live_mongo and mongo_uri:
        session_cache = build_session_owner_cache(mongo_uri, resolvers)
        cache_path = ROOT / "data" / "exports" / "session_owner_cache.json"
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        # serialize datetimes
        serial = {}
        for sid, ent in session_cache.items():
            e = dict(ent)
            if hasattr(e.get("occurred_at"), "isoformat"):
                e["occurred_at"] = e["occurred_at"].isoformat()
            serial[sid] = e
        cache_path.write_text(json.dumps(serial))
        print(json.dumps({"wrote_cache": str(cache_path), "entries": len(serial)}), flush=True)
    elif (ROOT / "data" / "exports" / "session_owner_cache.json").is_file():
        session_cache = json.loads(
            (ROOT / "data" / "exports" / "session_owner_cache.json").read_text()
        )
        print(json.dumps({"loaded_cache": len(session_cache)}), flush=True)

    ctx = AppContext.from_config(cfg)
    conn = ctx.store.connection  # type: ignore[attr-defined]
    ids = [r[0] for r in conn.execute("SELECT id FROM trajectories ORDER BY id")]
    ids = ids[args.offset :]
    if args.limit is not None:
        ids = ids[: args.limit]

    stats = {
        "total": len(ids),
        "updated_sqlite": 0,
        "synced_qdrant": 0,
        "owners_set": 0,
        "owners_new": 0,
        "occurred_set": 0,
        "errors": 0,
        "skipped": 0,
        "qdrant_missing_point": 0,
    }
    t0 = time.time()

    for i, tid in enumerate(ids):
        try:
            traj = ctx.store.get(tid)
        except Exception as exc:
            stats["errors"] += 1
            if stats["errors"] <= 3:
                print(f"get err {tid}: {exc}", file=sys.stderr)
            continue
        if traj is None:
            stats["skipped"] += 1
            continue

        refs = dict(traj.external_refs or {})
        session_id = refs.get("session_id")
        if session_id is not None:
            session_id = str(session_id)

        owner = _owner_from_traj(traj, resolvers)
        had_owner = bool(owner)
        if args.only_missing_owner and had_owner:
            # still may need qdrant sync — fall through only if schema < 4
            if int(getattr(traj, "schema_version", 1) or 1) >= 4 and traj.occurred_at:
                stats["skipped"] += 1
                continue

        cache_hit = session_cache.get(session_id or "") if session_id else None
        if cache_hit:
            if not owner and cache_hit.get("owner"):
                owner = cache_hit["owner"]
                refs["metadata_sources"] = dict(refs.get("metadata_sources") or {})
                refs["metadata_sources"]["owner"] = (
                    f"mongo_session_cache:{cache_hit.get('method')}"
                )
                if cache_hit.get("auth_fingerprint"):
                    refs["auth_fingerprint"] = cache_hit["auth_fingerprint"]
            if cache_hit.get("occurred_at") and not traj.occurred_at:
                occ = _dt_parse(cache_hit["occurred_at"])
            else:
                occ = traj.occurred_at
        else:
            occ = traj.occurred_at

        if not occ:
            occ = traj.finalized_at or traj.created_at

        changed = False
        if owner:
            stats["owners_set"] += 1
            if refs.get("owner") != owner:
                refs["owner"] = owner
                changed = True
                if not had_owner:
                    stats["owners_new"] += 1
        if occ and traj.occurred_at != occ:
            changed = True
            stats["occurred_set"] += 1

        new_tags = list(traj.tags or [])
        if owner:
            # remove owner:unknown
            new_tags = [t for t in new_tags if not (isinstance(t, str) and t.startswith("owner:") and "unknown" in t)]
            tag = f"owner:{owner}"
            if tag not in new_tags:
                new_tags.append(tag)
                changed = True

        schema_version = max(int(getattr(traj, "schema_version", 1) or 1), 4)
        if getattr(traj, "schema_version", 1) != schema_version:
            changed = True

        if changed and not args.dry_run:
            traj = traj.model_copy(
                update={
                    "external_refs": refs,
                    "tags": new_tags,
                    "occurred_at": occ or traj.occurred_at,
                    "schema_version": schema_version,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            ctx.store._update_trajectory(traj)  # noqa: SLF001
            ctx.store._sync_external_refs(traj.id, traj.external_refs)  # noqa: SLF001
            conn.commit()
            stats["updated_sqlite"] += 1
        elif changed:
            stats["updated_sqlite"] += 1
        else:
            # still refresh payload if we have owner in refs for qdrant
            traj = traj.model_copy(
                update={
                    "external_refs": refs,
                    "tags": new_tags,
                    "occurred_at": occ or traj.occurred_at,
                    "schema_version": schema_version,
                }
            )

        if not args.dry_run:
            try:
                payload = payload_from_trajectory(
                    traj,
                    embed_model=cfg.embed_model,
                    embed_dim=cfg.embed_dim,
                    aspects_present=["task", "scaffold"],
                )
                assert isinstance(ctx.index, QdrantIndex)
                pid = point_id_for_trajectory(traj.id)
                ctx.index._client.set_payload(  # noqa: SLF001
                    collection_name=ctx.index._collection,  # noqa: SLF001
                    payload=payload.model_dump(mode="json"),
                    points=[pid],
                )
                stats["synced_qdrant"] += 1
            except Exception as exc:
                msg = str(exc)
                if "404" in msg or "Not found" in msg or "No point with id" in msg:
                    stats["qdrant_missing_point"] += 1
                else:
                    stats["errors"] += 1
                    if stats["errors"] <= 5:
                        print(f"qdrant err {tid}: {exc}", file=sys.stderr)

        if args.progress_every and (i + 1) % args.progress_every == 0:
            print(
                json.dumps(
                    {
                        "progress": i + 1,
                        "of": len(ids),
                        "owners_set": stats["owners_set"],
                        "owners_new": stats["owners_new"],
                        "synced_qdrant": stats["synced_qdrant"],
                        "updated_sqlite": stats["updated_sqlite"],
                        "qdrant_missing_point": stats["qdrant_missing_point"],
                        "errors": stats["errors"],
                        "elapsed_sec": round(time.time() - t0, 1),
                    }
                ),
                flush=True,
            )

    out = {**stats, "elapsed_sec": round(time.time() - t0, 2), "dry_run": args.dry_run}
    print(json.dumps(out, indent=2))
    prog = ROOT / "data" / "exports" / "sync_qdrant_payload_v4_progress.json"
    prog.write_text(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
