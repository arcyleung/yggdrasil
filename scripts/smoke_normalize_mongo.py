#!/usr/bin/env python3
"""Smoke Phase-1 normalizer on fixture(s) and optionally live Mongo (no secrets logged)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from yggdrasil.adapters.importers.mongo_mapping import map_conversation_ir_legacy
from yggdrasil.adapters.importers.mongo_normalize import (
    normalize_and_aggregate_docs,
    normalize_mongo_doc,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize mongo docs (Phase 1 smoke)")
    parser.add_argument(
        "--fixture",
        type=Path,
        default=ROOT / "tests/fixtures/mongo/conversation_proxy_log_sample.json",
    )
    parser.add_argument("--fixture-v1", type=Path, default=ROOT / "tests/fixtures/mongo/conversation_sample.json")
    args = parser.parse_args()

    docs = []
    for path in (args.fixture, args.fixture_v1):
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            docs.append(data if isinstance(data, dict) else data[0])

    for doc in docs:
        ir = normalize_mongo_doc(doc)
        mapped = map_conversation_ir_legacy(ir)
        print(
            json.dumps(
                {
                    "source_shape": ir.source_shape,
                    "session_id": ir.session_id,
                    "request_id": ir.request_id,
                    "n_messages": ir.message_count,
                    "tool_names": ir.tool_names[:8],
                    "usage": {
                        "in": ir.usage.input_tokens if ir.usage else None,
                        "out": ir.usage.output_tokens if ir.usage else None,
                    },
                    "traj_id": mapped.trajectory.id,
                    "n_steps": len(mapped.steps),
                    "task_preview": mapped.trajectory.task_text[:120],
                },
                indent=2,
            )
        )

    aggs = normalize_and_aggregate_docs(docs)
    print(f"\naggregates: {len(aggs)} session(s)")
    for a in aggs:
        print(f"  session={a.session_id[:36]}… requests={a.request_count} canonical_msgs={a.canonical.message_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
