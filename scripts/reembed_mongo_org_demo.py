#!/usr/bin/env python3
"""Re-embed Mongo session JSONL with real owner names for org experience PoC.

Pipeline:
  1) build_api_key_owner_map.py  (KEY_NAME_MAP + Mongo headers → fingerprint owner map + session index)
  2) this script: hydrate existing export JSONL with experience_grade + owner stamps + embed
  3) probe search_strategies (search_mode=lab) as if an agent doesn't know who set things up

Does NOT re-export full request_headers or API keys into JSONL; joins owners via session_id sidecar.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from lib.mem_limit import apply_memory_cap  # noqa: E402
from lib.mongo_session_io import iter_sessions_jsonl  # noqa: E402
from mongo_importer_pre_embed import hydrate_record  # noqa: E402
from yggdrasil.adapters.importers.api_key_owners import load_owner_map  # noqa: E402
from yggdrasil.adapters.importers.mongo_conversation_importer import (  # noqa: E402
    MongoConversationImporter,
)
from yggdrasil.mcp.app_context import AppContext  # noqa: E402


def load_session_index(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    out: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            sid = row.get("session_id")
            if sid:
                out[str(sid)] = row
    return out


def main() -> int:
    apply_memory_cap()
    p = argparse.ArgumentParser(description="Re-embed mongo sessions with org owner map")
    p.add_argument(
        "--jsonl",
        type=Path,
        default=ROOT / "data" / "exports" / "mongo_sessions_2000.jsonl",
    )
    p.add_argument(
        "--owner-map",
        type=Path,
        default=ROOT / "data" / "exports" / "api_key_owner_map.json",
    )
    p.add_argument(
        "--session-index",
        type=Path,
        default=ROOT / "data" / "exports" / "session_owner_index.jsonl",
    )
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--limit", type=int, default=200, help="Sessions to hydrate (PoC subset)")
    p.add_argument("--embed", action="store_true", default=True)
    p.add_argument("--no-embed", action="store_true")
    p.add_argument("--embed-parent", action="store_true", default=False)
    p.add_argument("--fresh-qdrant", action="store_true", help="Drop collection first (destructive)")
    p.add_argument("--keep-lab-seeds", action="store_true", help="Do not wipe sqlite (merge with seeds)")
    p.add_argument("--probe", action="store_true", default=True)
    p.add_argument("--no-probe", action="store_true")
    p.add_argument("--progress-every", type=int, default=25)
    args = p.parse_args()
    embed = args.embed and not args.no_embed
    probe = args.probe and not args.no_probe

    if not args.jsonl.is_file():
        print(f"missing jsonl: {args.jsonl}", file=sys.stderr)
        return 1

    owner_map = load_owner_map(args.owner_map)
    session_index = load_session_index(args.session_index)
    if not owner_map:
        print(
            f"WARN: no owner map at {args.owner_map}. Run:\n"
            f"  PYTHONPATH=src python scripts/build_api_key_owner_map.py",
            file=sys.stderr,
        )
    else:
        print(f"loaded {len(owner_map)} identity→owner mappings; {len(session_index)} session rows")

    if args.fresh_qdrant and not args.keep_lab_seeds:
        from yggdrasil.config import load_config
        from qdrant_client import QdrantClient
        import shutil

        cfg = load_config()
        db = Path(cfg.sqlite_path)
        if db.exists():
            bak = db.with_suffix(db.suffix + f".bak.{int(time.time())}")
            shutil.move(str(db), str(bak))
            print(f"backed up sqlite → {bak}")
        client = QdrantClient(url=cfg.qdrant_url, api_key=cfg.qdrant_api_key or None, check_compatibility=False)
        try:
            client.delete_collection(cfg.qdrant_collection)
            print(f"deleted qdrant {cfg.qdrant_collection}")
        except Exception as exc:
            print(f"qdrant delete: {exc}")

    ctx = AppContext.from_config()
    ctx.index.ensure_collection(vector_size=ctx.config.embed_dim)
    importer = MongoConversationImporter(ctx.store, ctx.embed_service)

    stats = {"sessions": 0, "embedded": 0, "errors": 0, "owners": {}}
    t0 = time.perf_counter()
    for i, record in enumerate(iter_sessions_jsonl(args.jsonl)):
        if i < args.offset:
            continue
        if args.limit is not None and stats["sessions"] >= args.limit:
            break
        sid = str(record.get("session_id") or "")
        ident_row = session_index.get(sid, {})
        session_identity = {
            "identity_key": ident_row.get("identity_key") or record.get("identity_key"),
            "api_key_fingerprint": ident_row.get("api_key_fingerprint"),
            "user_id_fingerprint": ident_row.get("user_id_fingerprint"),
            "session_id": sid,
        }
        # stamp owner onto record for hydrate stamp()
        if ident_row.get("owner"):
            record = dict(record)
            record["owner"] = ident_row["owner"]
            record["owner_identity"] = session_identity

        try:
            result = hydrate_record(
                record,
                importer,
                embed=embed,
                embed_parent=args.embed_parent,
                dry_run=False,
                max_segment_msgs=40,
                experience_grade=True,
                owner_map=owner_map,
                session_identity=session_identity,
            )
            stats["sessions"] += 1
            if result.get("embedded"):
                stats["embedded"] += 1
            owner = ident_row.get("owner") or "unknown"
            stats["owners"][owner] = stats["owners"].get(owner, 0) + 1
        except Exception as exc:
            stats["errors"] += 1
            print(f"ERR session={sid[:12]}… {exc}", file=sys.stderr)
            continue

        if stats["sessions"] % max(1, args.progress_every) == 0:
            elapsed = time.perf_counter() - t0
            print(
                f"… {stats['sessions']} sessions ({stats['embedded']} embedded) "
                f"errors={stats['errors']} elapsed={elapsed:.1f}s owners={stats['owners']}"
            )

    elapsed = time.perf_counter() - t0
    print(f"\ndone in {elapsed:.1f}s: {stats}")

    if probe:
        print("\n=== PoC probe: agent searches org experience (owner unknown) ===")
        queries = [
            (
                "Set up or fix Python/Ray/data pipeline environment on Linux",
                "lab or laptop Linux; Python; team org-proxy-logs; owner unknown",
            ),
            (
                "Debug failing tests or CI build errors",
                "repo coding agent; pytest or build tools",
            ),
            (
                "Install dependencies and configure project without wasting hours",
                "dev setup; prefer efficient paths; avoid duplicate agent work",
            ),
        ]
        for task, scaffold in queries:
            print(f"\n--- query: {task[:70]}… ---")
            hits = ctx.search_service.search_strategies(
                task=task,
                scaffold=scaffold,
                search_mode="lab",
                experience_grade_only=True,
                team="org-proxy-logs",
                prefer_low_waste=False,
                limit=5,
            )
            if not hits:
                # retry without team filter (imports may miss team stamp on older rows)
                hits = ctx.search_service.search_strategies(
                    task=task,
                    scaffold=scaffold,
                    search_mode="lab",
                    experience_grade_only=True,
                    prefer_low_waste=False,
                    limit=5,
                )
            gate = ctx.search_service.last_gate_result
            if gate and gate.warnings:
                print(f"  gate: {gate.warnings}")
            if not hits:
                print("  (no hits)")
                continue
            for j, h in enumerate(hits, 1):
                owner = h.owner or (h.external_refs or {}).get("owner") or "?"
                agent = h.agent_id or (h.external_refs or {}).get("agent_id") or "?"
                print(
                    f"  #{j} owner={owner} agent={agent} status={h.status.value} "
                    f"score={h.score:.4f}"
                )
                print(f"      task: {(h.task_text or '')[:100]}")
                print(
                    f"      → tell user: follow up in person with **{owner}** "
                    f"(their agent {agent} produced this trajectory)"
                )

        # owner distribution in store via quick unfiltered sample
        print("\n=== owner diversity check (same query, no team filter) ===")
        hits = ctx.search_service.search_strategies(
            task="coding agent session with tools and repository work",
            scaffold="linux development environment",
            search_mode="lab",
            experience_grade_only=True,
            limit=12,
        )
        owners_seen: dict[str, int] = {}
        for h in hits:
            o = h.owner or (h.external_refs or {}).get("owner") or "?"
            owners_seen[o] = owners_seen.get(o, 0) + 1
        print(f"  top hit owners: {owners_seen}")

    return 0 if stats["errors"] == 0 or stats["sessions"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
