#!/usr/bin/env python3
"""Import MongoDB conversations into Yggdrasil for testing (optional live Mongo)."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Allow running without install when PYTHONPATH=src
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from yggdrasil.adapters.importers.mongo_conversation_importer import MongoConversationImporter
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
        if stripped.startswith("MONGO=") or stripped.upper().startswith("MONGODB"):
            # key=value form
            if "=" in stripped:
                return stripped.split("=", 1)[1].strip().strip('"').strip("'")
        if stripped.startswith("mongodb://") or stripped.startswith("mongodb+srv://"):
            return stripped
    return None


def load_fixture_docs(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    return [data]


def iter_live_mongo(uri: str, *, limit: int | None, projection: bool = True):
    """Yield live docs. Strips request_headers by default (never needed for ingress)."""
    try:
        from pymongo import MongoClient
    except ImportError as exc:
        raise SystemExit(
            "pymongo is required for live import; install with: pip install -e '.[mongo]'"
        ) from exc
    client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    try:
        coll = client["claude_conversations"]["conversations"]
        proj = None
        if projection:
            # exclude secrets; normalizer also drops headers defensively
            proj = {"request_headers": 0}
        cursor = coll.find({}, proj) if proj else coll.find({})
        if limit is not None:
            cursor = cursor.limit(limit)
        for doc in cursor:
            # normalize ObjectId
            if "_id" in doc and not isinstance(doc["_id"], (str, dict)):
                doc = dict(doc)
                doc["_id"] = {"$oid": str(doc["_id"])}
            yield doc
    finally:
        client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import mongo conversations into yggdrasil (testing)")
    parser.add_argument("--limit", type=int, default=None, help="Max documents to import")
    parser.add_argument("--dry-run", action="store_true", help="Map only; do not write store")
    parser.add_argument("--reembed", action="store_true", help="Embed/index after import")
    parser.add_argument("--mongo-uri", default=None, help="Mongo URI (never logged)")
    parser.add_argument("--creds-file", default=None, help="Path to mongo_creds.txt")
    parser.add_argument(
        "--fixture",
        default=None,
        help="Import from JSON fixture instead of live Mongo",
    )
    args = parser.parse_args(argv)

    config = load_config()
    # Do not print URI; redact config only
    print("config:", json.dumps(redact_config_for_log(config), indent=2))

    creds_file = Path(args.creds_file) if args.creds_file else config.mongo_creds_file
    mongo_uri = read_mongo_uri(creds_file, args.mongo_uri or config.mongo_uri or os.environ.get("MONGO_URI"))

    ctx = None
    store = None
    embed_service = None
    if not args.dry_run:
        ctx = AppContext.from_config(config)
        store = ctx.store
        embed_service = ctx.embed_service if args.reembed else None
    else:
        from yggdrasil.adapters.sqlite_store import SqliteTrajectoryStore

        store = SqliteTrajectoryStore(config.sqlite_path)

    importer = MongoConversationImporter(store, embed_service=embed_service)

    if args.fixture:
        docs = load_fixture_docs(Path(args.fixture))
        if args.limit is not None:
            docs = docs[: args.limit]
        stats = importer.import_many(docs, reembed=args.reembed, dry_run=args.dry_run)
    elif mongo_uri:
        docs_iter = iter_live_mongo(mongo_uri, limit=args.limit)
        stats = importer.import_many(docs_iter, reembed=args.reembed, dry_run=args.dry_run, limit=args.limit)
    else:
        default_fixture = ROOT / "tests" / "fixtures" / "mongo" / "conversation_sample.json"
        if default_fixture.exists():
            print(f"no mongo uri; using fixture {default_fixture}")
            docs = load_fixture_docs(default_fixture)
            if args.limit is not None:
                docs = docs[: args.limit]
            stats = importer.import_many(docs, reembed=args.reembed, dry_run=args.dry_run)
        else:
            print("error: provide --mongo-uri, MONGO_URI/creds, or --fixture", file=sys.stderr)
            return 2

    print(
        json.dumps(
            {
                "seen": stats.seen,
                "imported": stats.imported,
                "updated": stats.updated,
                "skipped": stats.skipped,
                "errors": stats.errors[:10],
                "dry_run": args.dry_run,
                "reembed": args.reembed,
            },
            indent=2,
        )
    )
    return 0 if not stats.errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
