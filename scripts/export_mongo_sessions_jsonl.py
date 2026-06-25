#!/usr/bin/env python3
"""Export ~N Mongo sessions (canonical transcript per session) to JSONL for offline hydration.

Does NOT embed or segment. Output is input for ``mongo_importer_pre_embed.py`` (external
segmentation + embed + Qdrant). Secrets: never writes ``request_headers``.

Usage:
  PYTHONPATH=src python scripts/export_mongo_sessions_jsonl.py \\
    --sessions 100 --out data/exports/mongo_sessions_100.jsonl

  PYTHONPATH=src python scripts/export_mongo_sessions_jsonl.py --sessions 5 --dry-run-stats
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from lib.mem_limit import apply_memory_cap  # noqa: E402
from lib.mongo_session_io import (  # noqa: E402
    export_sessions_from_mongo,
    export_sessions_from_mongo_to_jsonl,
    read_mongo_uri,
    write_sessions_jsonl,
)
from yggdrasil.config import load_config  # noqa: E402


def main() -> int:
    apply_memory_cap()  # default 24 GiB — subagent/importer safety
    p = argparse.ArgumentParser(description="Export mongo sessions to JSONL")
    p.add_argument("--sessions", type=int, default=100, help="Target number of sessions")
    p.add_argument("--max-docs", type=int, default=None, help="Cap raw mongo docs scanned")
    p.add_argument("--min-messages", type=int, default=2, help="Skip tiny sessions")
    p.add_argument(
        "--out",
        type=Path,
        default=ROOT / "data" / "exports" / "mongo_sessions_100.jsonl",
    )
    p.add_argument("--mongo-uri", default=None)
    p.add_argument("--creds-file", type=Path, default=None)
    p.add_argument("--dry-run-stats", action="store_true", help="Export in memory; print stats only")
    p.add_argument("--batch-docs", type=int, default=400, help="Mongo docs per aggregate batch (RAM)")
    p.add_argument("--max-mem-gb", type=float, default=None, help="Process mem cap GiB (default 24)")
    args = p.parse_args()
    if args.max_mem_gb is not None:
        apply_memory_cap(args.max_mem_gb)

    config = load_config()
    creds = args.creds_file or config.mongo_creds_file
    if not creds.is_absolute():
        creds = ROOT / creds
    uri = read_mongo_uri(creds, args.mongo_uri or config.mongo_uri or os.environ.get("MONGO_URI"))
    if not uri:
        print("error: need mongo URI via --mongo-uri, MONGO_URI, or mongo_creds.txt", file=sys.stderr)
        return 2

    print(
        f"exporting up to {args.sessions} sessions (max_docs={args.max_docs}, "
        f"batch_docs={args.batch_docs}, mem_cap≈24GiB)…"
    )
    if args.dry_run_stats:
        records = export_sessions_from_mongo(
            uri,
            target_sessions=args.sessions,
            max_docs=args.max_docs,
            min_messages=args.min_messages,
            batch_docs=args.batch_docs,
        )
        if not records:
            print("error: no sessions exported", file=sys.stderr)
            return 1
        msg_counts = [len((r.get("canonical") or {}).get("messages") or []) for r in records]
        stats = {
            "sessions": len(records),
            "out": str(args.out),
            "msg_count_min": min(msg_counts),
            "msg_count_max": max(msg_counts),
            "msg_count_avg": round(sum(msg_counts) / len(msg_counts), 1),
            "hydration_mode": "external_pre_embed",
            "sample_session_ids": [r["session_id"][:48] for r in records[:3]],
        }
        print(json.dumps(stats, indent=2))
        return 0

    stats = export_sessions_from_mongo_to_jsonl(
        uri,
        args.out,
        target_sessions=args.sessions,
        max_docs=args.max_docs,
        min_messages=args.min_messages,
        batch_docs=args.batch_docs,
    )
    if not stats.get("sessions"):
        print("error: no sessions exported", file=sys.stderr)
        return 1
    stats["hydration_mode"] = "external_pre_embed"
    stats["mem_cap_gb"] = 24
    print(json.dumps(stats, indent=2))
    print(f"wrote {stats['sessions']} lines → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
