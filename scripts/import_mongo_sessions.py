#!/usr/bin/env python3
"""Import live Mongo sessions as parent + segmented children; optionally embed + search smoke.

Usage:
  PYTHONPATH=src python scripts/import_mongo_sessions.py --limit-docs 80 --limit-sessions 5 --reembed --search-smoke
  PYTHONPATH=src python scripts/import_mongo_sessions.py --fixture tests/fixtures/mongo/conversation_proxy_log_sample.json --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from yggdrasil.adapters.importers.mongo_conversation_importer import MongoConversationImporter
from yggdrasil.adapters.importers.mongo_normalize import normalize_and_aggregate_docs
from yggdrasil.adapters.importers.storage_estimate import estimate_segmented_storage, format_estimate_report
from yggdrasil.config import load_config, redact_config_for_log
from yggdrasil.mcp.app_context import AppContext


def read_mongo_uri(creds_file: Path, env_uri: str | None) -> str | None:
    if env_uri and env_uri.strip():
        return env_uri.strip()
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


def load_fixture_docs(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    return [data]


def iter_live_mongo(uri: str, *, limit: int | None):
    from pymongo import MongoClient

    client = MongoClient(uri, serverSelectionTimeoutMS=12000)
    try:
        coll = client["claude_conversations"]["conversations"]
        cursor = coll.find({}, {"request_headers": 0})
        if limit is not None:
            cursor = cursor.limit(limit)
        for doc in cursor:
            if "_id" in doc and not isinstance(doc["_id"], (str, dict)):
                doc = dict(doc)
                doc["_id"] = {"$oid": str(doc["_id"])}
            yield doc
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Hierarchical mongo session import")
    parser.add_argument("--limit-docs", type=int, default=50, help="Max raw mongo docs to fetch")
    parser.add_argument("--limit-sessions", type=int, default=5, help="Max sessions after aggregate")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reembed", action="store_true", help="Embed children into Qdrant")
    parser.add_argument("--embed-parent", action="store_true")
    parser.add_argument("--fixture", default=None)
    parser.add_argument("--mongo-uri", default=None)
    parser.add_argument("--creds-file", default=None)
    parser.add_argument("--search-smoke", action="store_true", help="Run search_strategies after import")
    parser.add_argument("--storage-estimate", action="store_true", default=True)
    args = parser.parse_args()

    config = load_config()
    print("config:", json.dumps(redact_config_for_log(config), indent=2))

    if args.fixture:
        docs = load_fixture_docs(Path(args.fixture))
        if args.limit_docs:
            docs = docs[: args.limit_docs]
    else:
        creds = Path(args.creds_file) if args.creds_file else config.mongo_creds_file
        uri = read_mongo_uri(creds, args.mongo_uri or config.mongo_uri or os.environ.get("MONGO_URI"))
        if not uri:
            print("error: need --fixture or mongo creds", file=sys.stderr)
            return 2
        docs = list(iter_live_mongo(uri, limit=args.limit_docs))

    aggs = normalize_and_aggregate_docs(docs)
    print(f"fetched_docs={len(docs)} sessions_aggregated={len(aggs)}")
    preview = []
    for a in aggs[: args.limit_sessions]:
        preview.append(
            {
                "session_id": a.session_id[:48],
                "requests": a.request_count,
                "canonical_msgs": a.canonical.message_count,
                "model": a.canonical.model,
            }
        )
    print("session_preview:", json.dumps(preview, indent=2))

    if args.storage_estimate:
        avg_segs = 6.0
        est = estimate_segmented_storage(
            n_sessions=min(args.limit_sessions, len(aggs)),
            segments_per_session=avg_segs,
            embed_dim=config.embed_dim,
            embed_parent=args.embed_parent,
            source_tokens_total=None,
        )
        print(format_estimate_report(est))

    if args.dry_run and not args.reembed:
        # still allow dry-run map-only via importer
        pass

    ctx = AppContext.from_config(config)
    embed = ctx.embed_service if args.reembed else None
    importer = MongoConversationImporter(ctx.store, embed_service=embed)
    stats = importer.import_docs_as_sessions(
        docs,
        reembed=args.reembed,
        dry_run=args.dry_run,
        limit_sessions=args.limit_sessions,
        embed_parent=args.embed_parent,
        embed_children=True,
    )
    print(
        json.dumps(
            {
                "seen_docs": stats.seen,
                "sessions": stats.sessions,
                "parents": stats.parents,
                "children": stats.children,
                "imported_rows": stats.imported,
                "embedded_ops": stats.embedded,
                "skipped": stats.skipped,
                "errors": stats.errors[:15],
                "dry_run": args.dry_run,
                "reembed": args.reembed,
            },
            indent=2,
        )
    )

    if args.search_smoke and not args.dry_run:
        # Use first session's first user-ish text as query seed
        task_q = "coding agent task tool use fix bug"
        scaffold_q = "claude agent tools Read Bash Grep"
        if aggs:
            from yggdrasil.adapters.importers.mongo_normalize import ir_message_text

            for msg in aggs[0].canonical.messages:
                if msg.role == "user":
                    t = ir_message_text(msg).strip()
                    if t and len(t) > 20:
                        task_q = t[:500]
                        break
            if aggs[0].canonical.tool_names:
                scaffold_q = f"tools: {', '.join(aggs[0].canonical.tool_names[:12])}"
        hits = ctx.search_service.search_strategies(
            task=task_q, scaffold=scaffold_q, domain="coding", limit=5
        )
        print("\n--- search_strategies smoke ---")
        print(f"query_task_preview={task_q[:120]!r}")
        for i, h in enumerate(hits[:5], 1):
            tid = getattr(h, "trajectory_id", None) or (h.get("trajectory_id") if isinstance(h, dict) else None)
            score = getattr(h, "score", None) or (h.get("score") if isinstance(h, dict) else None)
            status = getattr(h, "status", None)
            if status is None and isinstance(h, dict):
                status = h.get("status")
            tags = getattr(h, "tags", None) or (h.get("tags") if isinstance(h, dict) else [])
            print(f"  [{i}] id={str(tid)[:48]}… score={score} status={status} tags={tags}")

    return 0 if not stats.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
