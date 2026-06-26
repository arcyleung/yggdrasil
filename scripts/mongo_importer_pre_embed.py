#!/usr/bin/env python3
"""Offline hydration: JSONL sessions → external segmentation → embed → SQLite + Qdrant.

Intended for **testing / subagent batch hydration**, not the production agent path.

Production (intended):
  Same agent that ran the trajectory loads yggdrasil-trajectory-memory skill →
  builds segment list (task/outcome spans) → MCP start/append/finalize per segment.

This script (test/hydration):
  External process (importer or subagent) segments transcripts with ``mongo_auto`` heuristics
  (or optional pre-filled ``segments`` in JSONL) → persists parent+children → embeds children
  via embed endpoint → upserts Qdrant. Segmentation is **not** done by the trajectory author.

Usage:
  # 1) Export once
  PYTHONPATH=src python scripts/export_mongo_sessions_jsonl.py --sessions 100 \\
    --out data/exports/mongo_sessions_100.jsonl

  # 2) Segment externally + embed + insert (subagent-friendly)
  PYTHONPATH=src python scripts/mongo_importer_pre_embed.py \\
    --jsonl data/exports/mongo_sessions_100.jsonl --embed --limit 20

  # Segment only (write augmented jsonl with segments, no embed)
  PYTHONPATH=src python scripts/mongo_importer_pre_embed.py \\
    --jsonl data/exports/mongo_sessions_100.jsonl --segment-only \\
    --out-jsonl data/exports/mongo_sessions_100_segmented.jsonl

  # Resume / shard for parallel subagents
  PYTHONPATH=src python scripts/mongo_importer_pre_embed.py \\
    --jsonl … --embed --offset 0 --limit 25 --worker-id w0
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
from lib.mongo_session_io import (  # noqa: E402
    count_jsonl_lines,
    iter_sessions_jsonl,
    record_to_ir,
    write_sessions_jsonl,
)
from yggdrasil.adapters.importers.mongo_conversation_importer import (  # noqa: E402
    MongoConversationImporter,
)
from yggdrasil.adapters.importers.mongo_mapping import map_session_hierarchy  # noqa: E402
from yggdrasil.adapters.importers.mongo_lanes import map_session_multilane  # noqa: E402
from yggdrasil.adapters.importers.mongo_normalize import (  # noqa: E402
    SessionAggregate,
    aggregate_session_irs,
)
from yggdrasil.adapters.importers.mongo_segment import segment_conversation_ir  # noqa: E402
from yggdrasil.adapters.importers.segment_schema import TrajectorySegment  # noqa: E402
from yggdrasil.adapters.importers.storage_estimate import (  # noqa: E402
    estimate_segmented_storage,
    format_estimate_report,
)
from yggdrasil.config import load_config, redact_config_for_log  # noqa: E402
from yggdrasil.mcp.app_context import AppContext  # noqa: E402


def _segments_from_record(record: dict[str, Any]) -> list[dict[str, Any]] | None:
    segs = record.get("segments")
    if not segs:
        return None
    if isinstance(segs, list) and segs:
        return segs
    return None


def segment_record_externally(
    record: dict[str, Any],
    *,
    max_segment_msgs: int = 24,
) -> dict[str, Any]:
    """Run external (non-author) segmentation; attach segments + provenance on record copy."""
    ir = record_to_ir(record)
    caller = _segments_from_record(record)
    segmented = segment_conversation_ir(
        ir,
        max_segment_msgs=max_segment_msgs,
        caller_segments=caller,
    )
    # Force provenance: even if caller pre-filled segments, this pass is external_pre_embed
    out = dict(record)
    out["segments"] = [s.to_dict() for s in segmented.segments]
    out["segmentation_source"] = (
        "external_pre_embed_caller_override"
        if caller
        else "external_pre_embed_mongo_auto"
    )
    out["hydration_mode"] = "external_pre_embed"
    out["parent_task"] = segmented.parent_task
    out["parent_scaffold"] = segmented.parent_scaffold
    out["segment_count"] = len(segmented.segments)
    out["n_messages"] = len(ir.messages)
    return out


def _session_aggregate_for_record(
    record: dict[str, Any],
    ir,
    *,
    live_mongo: bool = False,
    mongo_uri: str | None = None,
):
    """Build SessionAggregate with all proxy requests when possible (B′ lanes need this)."""
    from yggdrasil.adapters.importers.mongo_normalize import (
        ConversationIR,
        SessionAggregate,
        normalize_and_aggregate_docs,
    )

    sid = record.get("session_id") or ir.session_id
    if record.get("proxy_docs"):
        aggs = normalize_and_aggregate_docs(record["proxy_docs"])
        if aggs:
            return aggs[0]
    if live_mongo and sid and mongo_uri:
        try:
            from pymongo import MongoClient

            cl = MongoClient(mongo_uri, serverSelectionTimeoutMS=20000)
            docs = list(
                cl["claude_conversations"]["conversations"].find(
                    {"session_id": sid}, {"request_headers": 0}
                )
            )
            cl.close()
            for d in docs:
                if "_id" in d and not isinstance(d["_id"], (str, dict)):
                    d["_id"] = {"$oid": str(d["_id"])}
            if docs:
                aggs = normalize_and_aggregate_docs(docs)
                if aggs:
                    return aggs[0]
        except Exception as exc:
            print(f"live_mongo_warn session={sid}: {exc}", file=sys.stderr)

    req_ids = record.get("request_ids") or ir.raw_external.get("request_ids") or [ir.request_id]
    ir2 = ConversationIR(
        session_id=ir.session_id,
        request_id=ir.request_id,
        model=ir.model,
        created_at=ir.created_at,
        updated_at=ir.updated_at,
        title=ir.title,
        project=ir.project,
        tags=list(ir.tags),
        system_text=ir.system_text,
        tool_names=list(ir.tool_names),
        messages=list(ir.messages),
        usage=ir.usage,
        source_shape=ir.source_shape,
        raw_external={
            **ir.raw_external,
            "request_ids": req_ids,
            "request_count": record.get("request_count") or len(req_ids),
        },
    )
    return SessionAggregate(
        session_id=ir2.session_id or sid or "unknown",
        requests=[ir2],
        canonical=ir2,
    )


def hydrate_record(
    record: dict[str, Any],
    importer: MongoConversationImporter,
    *,
    embed: bool,
    embed_parent: bool,
    dry_run: bool,
    max_segment_msgs: int,
    experience_grade: bool = False,
    owner_map: dict[str, str] | None = None,
    session_identity: dict[str, Any] | None = None,
    live_mongo: bool = False,
    mongo_uri: str | None = None,
    multilane: bool = True,
) -> dict[str, Any]:
    """Segment externally → map hierarchy → SQLite (+ optional Qdrant embed children).

    With multilane=True (default), uses policy B′ (system_hash|full_model maximizers)
    plus within-lane segment slices. Pass live_mongo=True to re-fetch all proxy docs
    for the session so multiple lanes are populated (JSONL often has only the old canonical IR).
    """
    seg_record = segment_record_externally(record, max_segment_msgs=max_segment_msgs)
    ir = record_to_ir(seg_record)
    if multilane:
        agg = _session_aggregate_for_record(
            record, ir, live_mongo=live_mongo, mongo_uri=mongo_uri
        )
        hierarchy = map_session_multilane(
            agg, embed_parent=embed_parent, max_segment_msgs=max_segment_msgs
        )
    else:
        segmented = segment_conversation_ir(
            ir,
            max_segment_msgs=max_segment_msgs,
            caller_segments=seg_record.get("segments"),
        )
        hierarchy = map_session_hierarchy(ir, segmented=segmented, embed_parent=embed_parent)

    # Stamp external provenance on all trajectories' tags/refs
    def stamp(mapped):
        traj = mapped.trajectory
        tags = list(traj.tags)
        if experience_grade:
            for t in ("experience_import", "cleaned_task_keys", "external_segmented", "multilane_bprime"):
                if t not in tags:
                    tags.append(t)
            # remove archive tags if mapper added them
            tags = [
                t
                for t in tags
                if t not in ("external_pre_embed", "hydration_test", "not_author_segmented")
            ]
        else:
            for t in ("external_pre_embed", "hydration_test", "not_author_segmented", "multilane_bprime"):
                if t not in tags:
                    tags.append(t)
        refs = {
            **traj.external_refs,
            "hydration_mode": "experience_grade" if experience_grade else "external_pre_embed",
            "segmentation_source": seg_record.get("segmentation_source")
            or traj.external_refs.get("segmentation_source"),
            "author_agent_segmented": False,
            "experience_grade": experience_grade,
            "multilane_policy": "B_prime_system_hash_pipe_full_model" if multilane else "single_canonical",
        }
        # Owner / agent identity from truncated API key fingerprint (org PoC)
        ident = session_identity or seg_record.get("owner_identity") or {}
        identity_key = ident.get("identity_key") or seg_record.get("identity_key")
        if owner_map and identity_key:
            from yggdrasil.adapters.importers.api_key_owners import owner_bundle_for_identity

            bundle = owner_bundle_for_identity(identity_key, owner_map, extra=ident)
            refs.update(bundle)
            owner = bundle.get("owner")
            if owner and owner != "unknown":
                for t in (f"owner:{owner}", "org_proxy_import"):
                    if t not in tags:
                        tags.append(t)
        elif seg_record.get("owner"):
            refs["owner"] = seg_record["owner"]
            refs.setdefault("agent_id", f"{seg_record['owner']}-mongo-agent")
            refs.setdefault("team", "org-proxy-logs")
        mapped.trajectory = traj.model_copy(update={"tags": tags, "external_refs": refs})
        return mapped

    hierarchy.parent = stamp(hierarchy.parent)
    hierarchy.children = [stamp(c) for c in hierarchy.children]

    if not dry_run:
        hierarchy = importer.import_session_hierarchy(
            hierarchy,
            reembed=embed,
            dry_run=False,
            embed_parent=embed_parent,
            embed_children=True,
        )

    lane_children = [
        c
        for c in hierarchy.children
        if c.trajectory.external_refs.get("kind") == "session_lane"
    ]
    slice_children = [
        c
        for c in hierarchy.children
        if c.trajectory.external_refs.get("kind") == "session_lane_slice"
    ]
    return {
        "session_id": seg_record.get("session_id"),
        "segment_count": seg_record.get("segment_count"),
        "segmentation_source": seg_record.get("segmentation_source"),
        "parent_id": hierarchy.parent.trajectory.id,
        "child_ids": [c.trajectory.id for c in hierarchy.children],
        "lane_count": len(lane_children)
        if multilane
        else hierarchy.parent.trajectory.external_refs.get("lane_count", 0),
        "slice_count": len(slice_children)
        if multilane
        else len(hierarchy.children),
        "lane_keys": hierarchy.parent.trajectory.external_refs.get("lane_keys") or [],
        "multilane": multilane,
        "multilane_policy": "B_prime_system_hash_pipe_full_model"
        if multilane
        else "single_canonical",
        "request_count_in_agg": hierarchy.parent.trajectory.external_refs.get("request_count"),
        "embedded": embed and not dry_run,
        "dry_run": dry_run,
        "experience_grade": experience_grade,
    }


def _resolve_mongo_uri(explicit: str | None, config) -> str | None:
    """Prefer CLI --mongo-uri, then config.mongo_uri, then first line of creds file."""
    if explicit:
        return explicit.strip()
    if getattr(config, "mongo_uri", None):
        return config.mongo_uri
    creds = getattr(config, "mongo_creds_file", None)
    if creds is not None:
        path = Path(creds)
        if not path.is_absolute():
            path = ROOT / path
        if path.is_file():
            line = path.read_text(encoding="utf-8").strip().splitlines()
            if line and line[0].strip() and not line[0].strip().startswith("#"):
                return line[0].strip()
    return None


def main() -> int:
    apply_memory_cap()  # 24 GiB default for subagent/importer workers
    p = argparse.ArgumentParser(
        description="External segment + embed hydration from mongo session JSONL"
    )
    p.add_argument(
        "--jsonl",
        type=Path,
        default=ROOT / "data" / "exports" / "mongo_sessions_100.jsonl",
        help="Input session JSONL from export_mongo_sessions_jsonl.py",
    )
    p.add_argument("--offset", type=int, default=0, help="Skip first N records (subagent shard)")
    p.add_argument("--limit", type=int, default=None, help="Process at most N records")
    p.add_argument("--worker-id", default=None, help="Label for logs / progress file")
    p.add_argument(
        "--segment-only",
        action="store_true",
        help="Only run external segmentation; write --out-jsonl; no store/embed",
    )
    p.add_argument(
        "--out-jsonl",
        type=Path,
        default=None,
        help="Write segmented records (default: <jsonl>.segmented.jsonl)",
    )
    p.add_argument(
        "--embed",
        action="store_true",
        help="Call embed endpoint and upsert Qdrant for segment children",
    )
    p.add_argument("--embed-parent", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="Segment+map only; no SQLite/Qdrant writes")
    p.add_argument("--max-segment-msgs", type=int, default=24)
    p.add_argument("--search-smoke", action="store_true")
    p.add_argument("--progress-every", type=int, default=5)
    p.add_argument("--estimate-storage", action="store_true", default=True)
    p.add_argument(
        "--experience-grade",
        action="store_true",
        help="Clean task keys + tags that pass agent retrieval gates (not hydration_test archive)",
    )
    p.add_argument("--max-mem-gb", type=float, default=None, help="Process mem cap GiB (default 24)")
    p.add_argument(
        "--live-mongo",
        action="store_true",
        help="Re-fetch all proxy docs for each session_id (needed for multi-lane B′ when JSONL lacks proxy_docs)",
    )
    p.add_argument(
        "--mongo-uri",
        default=None,
        help="Mongo URI override (else MONGO_URI / mongo_creds.txt first line)",
    )
    p.add_argument(
        "--no-multilane",
        action="store_true",
        help="Use legacy single-canonical map_session_hierarchy (disable B′ multi-lane)",
    )
    p.add_argument(
        "--session-id",
        default=None,
        help="Only process this session_id (scan JSONL; useful with --live-mongo smoke)",
    )
    args = p.parse_args()
    if args.max_mem_gb is not None:
        apply_memory_cap(args.max_mem_gb)

    if not args.jsonl.exists():
        print(f"error: jsonl not found: {args.jsonl}", file=sys.stderr)
        print("run: PYTHONPATH=src python scripts/export_mongo_sessions_jsonl.py --sessions 100", file=sys.stderr)
        return 2

    multilane = not args.no_multilane

    # Stream JSONL — never load entire multi-GB export into RAM
    total_in_file = count_jsonl_lines(args.jsonl)
    records_iter = iter_sessions_jsonl(args.jsonl)
    # apply offset/limit without materializing full list
    records: list[dict[str, Any]] = []
    for i, rec in enumerate(records_iter):
        if args.session_id and rec.get("session_id") != args.session_id:
            continue
        if not args.session_id and i < args.offset:
            continue
        if args.limit is not None and len(records) >= args.limit:
            break
        records.append(rec)
        if args.session_id:
            break

    worker = args.worker_id or "main"
    config = load_config()
    mongo_uri = _resolve_mongo_uri(args.mongo_uri, config)
    if args.live_mongo and not mongo_uri:
        print(
            "error: --live-mongo requires --mongo-uri, MONGO_URI, or mongo_creds.txt",
            file=sys.stderr,
        )
        return 2

    print(
        json.dumps(
            {
                "worker_id": worker,
                "jsonl": str(args.jsonl),
                "total_in_file": total_in_file,
                "offset": args.offset,
                "processing": len(records),
                "mode": "segment_only" if args.segment_only else ("embed" if args.embed else "persist_no_embed"),
                "hydration_mode": "external_pre_embed",
                "multilane": multilane,
                "multilane_policy": "B_prime_system_hash_pipe_full_model"
                if multilane
                else "single_canonical",
                "live_mongo": args.live_mongo,
                "mongo_uri_resolved": bool(mongo_uri),
                "session_id_filter": args.session_id,
                "note": "Segmentation is external (not trajectory author / skill path)",
            },
            indent=2,
        )
    )

    if args.estimate_storage and records:
        est = estimate_segmented_storage(
            n_sessions=len(records),
            segments_per_session=6.0,
            embed_dim=config.embed_dim,
            embed_parent=args.embed_parent,
        )
        print(format_estimate_report(est))

    # --- segment-only path (subagent writes augmented jsonl) ---
    if args.segment_only:
        out_path = args.out_jsonl or args.jsonl.with_suffix(".segmented.jsonl")
        segmented_rows = [
            segment_record_externally(r, max_segment_msgs=args.max_segment_msgs) for r in records
        ]
        n = write_sessions_jsonl(out_path, segmented_rows)
        seg_total = sum(int(r.get("segment_count") or 0) for r in segmented_rows)
        print(
            json.dumps(
                {
                    "wrote": n,
                    "out_jsonl": str(out_path),
                    "total_segments": seg_total,
                    "avg_segments": round(seg_total / n, 2) if n else 0,
                },
                indent=2,
            )
        )
        return 0

    print("config:", json.dumps(redact_config_for_log(config), indent=2))

    ctx = AppContext.from_config(config)
    embed_svc = ctx.embed_service if args.embed and not args.dry_run else None
    importer = MongoConversationImporter(ctx.store, embed_service=embed_svc)

    results: list[dict[str, Any]] = []
    errors: list[str] = []
    t0 = time.time()

    for i, rec in enumerate(records):
        sid = rec.get("session_id", "?")
        try:
            summary = hydrate_record(
                rec,
                importer,
                embed=args.embed,
                embed_parent=args.embed_parent,
                dry_run=args.dry_run,
                max_segment_msgs=args.max_segment_msgs,
                experience_grade=args.experience_grade,
                live_mongo=args.live_mongo,
                mongo_uri=mongo_uri,
                multilane=multilane,
            )
            results.append(summary)
        except Exception as exc:
            errors.append(f"{sid}: {exc}")
            if args.progress_every and (i + 1) % max(args.progress_every, 1) == 0:
                pass
            continue

        if args.progress_every and (i + 1) % args.progress_every == 0:
            print(
                f"[{worker}] progress {i + 1}/{len(records)} "
                f"ok={len(results)} err={len(errors)} elapsed={time.time() - t0:.1f}s",
                flush=True,
            )

    children = sum(len(r.get("child_ids") or []) for r in results)
    report = {
        "worker_id": worker,
        "sessions_ok": len(results),
        "sessions_err": len(errors),
        "children_total": children,
        "embedded": args.embed and not args.dry_run,
        "dry_run": args.dry_run,
        "elapsed_sec": round(time.time() - t0, 2),
        "errors_sample": errors[:12],
        "sample_parents": [r.get("parent_id") for r in results[:3]],
    }
    print(json.dumps(report, indent=2))

    if args.search_smoke and results and not args.dry_run:
        task_q = "coding agent tool use fix implement"
        scaffold_q = "claude agent tools Read Bash Grep"
        # Prefer first segment task from last successful record if available
        try:
            first_seg_rec = segment_record_externally(records[0], max_segment_msgs=args.max_segment_msgs)
            segs = first_seg_rec.get("segments") or []
            if segs and segs[0].get("task"):
                task_q = str(segs[0]["task"])[:500]
            if first_seg_rec.get("parent_scaffold"):
                scaffold_q = str(first_seg_rec["parent_scaffold"])[:500]
        except Exception:
            pass
        hits = ctx.search_service.search_strategies(
            task=task_q,
            scaffold=scaffold_q,
            domain="coding",
            tags_any=["external_pre_embed", "session_segment"],
            limit=8,
        )
        print("\n--- search_smoke (tags: external_pre_embed + session_segment) ---")
        print(f"query_task_preview={task_q[:100]!r}")
        for j, h in enumerate(hits[:8], 1):
            tid = getattr(h, "trajectory_id", None)
            score = getattr(h, "score", None)
            tags = getattr(h, "tags", None) or []
            print(f"  [{j}] {str(tid)[:64]} score={score} external_pre_embed={'external_pre_embed' in tags}")

    progress_path = ROOT / "data" / "exports" / f"pre_embed_progress_{worker}.json"
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    progress_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"progress_file={progress_path}")

    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
