#!/usr/bin/env python3
"""Backfill SQLite schema v4 (owner/occurred_at) and sync Qdrant payloads without re-embed.

Optionally enriches owner from user_mapping + Mongo proxy docs (session_id / request_id).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from yggdrasil.adapters.importers.api_key_owners import (
    fingerprint_auth_value,
    load_key_name_map,
    owner_map_from_key_name_map,
)
from yggdrasil.config import load_config
from yggdrasil.mcp.app_context import AppContext
from yggdrasil.ports.vector_index import payload_from_trajectory
from yggdrasil.services.retrieval_gates import time_range_label


def _owner_from_traj(traj, fp_map: dict[str, str]) -> str | None:
    refs = traj.external_refs or {}
    o = refs.get("owner") or refs.get("user")
    if o and str(o).strip() and str(o).strip().lower() not in ("unknown", "none", ""):
        return str(o).strip()
    for t in traj.tags or []:
        if isinstance(t, str) and t.startswith("owner:"):
            return t.split(":", 1)[1].strip() or None
    for key in ("auth_fingerprint", "identity_key", "api_key_fingerprint"):
        fp = refs.get(key)
        if fp and fp in fp_map:
            return fp_map[fp]
    return None


def _occurred_from_traj(traj) -> datetime | None:
    if traj.occurred_at:
        return traj.occurred_at
    refs = traj.external_refs or {}
    for key in ("occurred_at", "event_time", "mongo_created_at", "created_at"):
        v = refs.get(key)
        if isinstance(v, str) and v:
            try:
                return datetime.fromisoformat(v.replace("Z", "+00:00"))
            except ValueError:
                pass
    return traj.finalized_at or traj.created_at


def _mongo_enrich(session_id: str | None, mongo_uri: str | None, fp_map: dict[str, str]) -> dict:
    """Best-effort: first proxy doc for session → owner fingerprint + created_at."""
    out: dict = {}
    if not session_id or not mongo_uri:
        return out
    try:
        from pymongo import MongoClient

        cl = MongoClient(mongo_uri, serverSelectionTimeoutMS=15000)
        doc = cl["claude_conversations"]["conversations"].find_one(
            {"session_id": session_id},
            {"request_headers": 1, "created_at": 1, "updated_at": 1, "model": 1},
        )
        cl.close()
        if not doc:
            return out
        headers = doc.get("request_headers") or {}
        auth = headers.get("Authorization") or headers.get("authorization")
        if auth:
            fp = fingerprint_auth_value(auth)
            if fp and fp in fp_map:
                out["owner"] = fp_map[fp]
                out["auth_fingerprint"] = fp
        for k in ("created_at", "updated_at"):
            if doc.get(k) is not None:
                out[k] = doc[k]
                break
    except Exception as exc:
        out["mongo_error"] = str(exc)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--batch", type=int, default=200)
    p.add_argument("--live-mongo", action="store_true", help="Resolve owner/time from Mongo by session_id")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--progress-every", type=int, default=50)
    args = p.parse_args()

    cfg = load_config()
    key_map = load_key_name_map(cfg.user_mapping_path or ROOT / "user_mapping.yaml")
    fp_map = owner_map_from_key_name_map(key_map)
    mongo_uri = cfg.mongo_uri
    if not mongo_uri and (ROOT / "mongo_creds.txt").is_file():
        mongo_uri = (ROOT / "mongo_creds.txt").read_text().strip().splitlines()[0].strip()

    ctx = AppContext.from_config(cfg)
    # ensure schema columns
    _ = ctx.store  # migrates on init

    conn = ctx.store.connection  # type: ignore[attr-defined]
    cur = conn.execute("SELECT id FROM trajectories ORDER BY id")
    ids = [r[0] for r in cur.fetchall()]
    ids = ids[args.offset :]
    if args.limit is not None:
        ids = ids[: args.limit]

    stats = {
        "total": len(ids),
        "updated_sqlite": 0,
        "synced_qdrant": 0,
        "owners_set": 0,
        "occurred_set": 0,
        "errors": 0,
        "skipped": 0,
    }
    t0 = time.time()
    embed_model = cfg.embed_model
    embed_dim = cfg.embed_dim

    for i, tid in enumerate(ids):
        try:
            # TrajectoryStore.get(id) — not get_trajectory (session service API)
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
        changed = False
        owner = _owner_from_traj(traj, fp_map)
        occurred = _occurred_from_traj(traj)

        if args.live_mongo and (not owner or occurred is None or occurred == traj.created_at):
            enr = _mongo_enrich(str(session_id) if session_id else None, mongo_uri, fp_map)
            if enr.get("owner") and not owner:
                owner = enr["owner"]
                refs["auth_fingerprint"] = enr.get("auth_fingerprint")
                refs.setdefault("metadata_sources", {})["owner"] = "mongo_auth_fingerprint"
            for k in ("created_at", "updated_at"):
                if enr.get(k) is not None and occurred in (None, traj.created_at):
                    v = enr[k]
                    if hasattr(v, "isoformat"):
                        occurred = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
                    elif isinstance(v, str):
                        try:
                            occurred = datetime.fromisoformat(v.replace("Z", "+00:00"))
                        except ValueError:
                            pass
                    refs.setdefault("metadata_sources", {})["occurred_at"] = f"mongo_{k}"

        if owner:
            if refs.get("owner") != owner:
                refs["owner"] = owner
                changed = True
            stats["owners_set"] += 1
        if occurred and traj.occurred_at != occurred:
            changed = True
            stats["occurred_set"] += 1

        new_tags = list(traj.tags or [])
        if owner and f"owner:{owner}" not in new_tags:
            new_tags.append(f"owner:{owner}")
            changed = True

        schema_version = max(int(getattr(traj, "schema_version", 1) or 1), 4)
        if getattr(traj, "schema_version", 1) != schema_version:
            changed = True

        if changed and not args.dry_run:
            traj = traj.model_copy(
                update={
                    "external_refs": refs,
                    "tags": new_tags,
                    "occurred_at": occurred or traj.occurred_at,
                    "schema_version": schema_version,
                    "updated_at": datetime.now(timezone.utc),
                }
            )
            # direct SQL update via store private API
            ctx.store._update_trajectory(traj)  # noqa: SLF001
            ctx.store._sync_external_refs(traj.id, traj.external_refs)  # noqa: SLF001
            conn.commit()
            stats["updated_sqlite"] += 1
        elif changed:
            stats["updated_sqlite"] += 1

        # Qdrant payload-only sync
        if not args.dry_run:
            try:
                payload = payload_from_trajectory(
                    traj,
                    embed_model=embed_model,
                    embed_dim=embed_dim,
                    aspects_present=["task", "scaffold"],
                )
                # Use qdrant client set_payload via point id
                from yggdrasil.adapters.qdrant_index import QdrantIndex, point_id_for_trajectory

                assert isinstance(ctx.index, QdrantIndex)
                pid = point_id_for_trajectory(traj.id)
                ctx.index._client.set_payload(  # noqa: SLF001
                    collection_name=ctx.index._collection,  # noqa: SLF001
                    payload=payload.model_dump(mode="json"),
                    points=[pid],
                )
                stats["synced_qdrant"] += 1
            except Exception as exc:
                # Parents / non-embedded rows have no Qdrant point — not fatal
                msg = str(exc)
                if "404" in msg or "Not found" in msg or "No point with id" in msg:
                    stats["skipped"] += 1
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
                        **{k: stats[k] for k in ("updated_sqlite", "synced_qdrant", "owners_set", "errors")},
                        "elapsed_sec": round(time.time() - t0, 1),
                    }
                ),
                flush=True,
            )

    out = {
        **stats,
        "elapsed_sec": round(time.time() - t0, 2),
        "dry_run": args.dry_run,
        "live_mongo": args.live_mongo,
    }
    print(json.dumps(out, indent=2))
    prog = ROOT / "data" / "exports" / "sync_qdrant_payload_v4_progress.json"
    prog.parent.mkdir(parents=True, exist_ok=True)
    prog.write_text(json.dumps(out, indent=2))
    return 0 if stats["errors"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
