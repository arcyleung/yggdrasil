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
) -> dict[str, Any]:
    """Segment externally → map hierarchy → SQLite (+ optional Qdrant embed children).

    experience_grade: cleaned task keys + tags that pass default agent retrieval gates
    (for gated eval / pseudo-experience corpus). Still external segmentation, but not
    tagged as hydration_test/archive so skill gates can return hits.
    """
    seg_record = segment_record_externally(record, max_segment_msgs=max_segment_msgs)
    ir = record_to_ir(seg_record)
    segmented = segment_conversation_ir(
        ir,
        max_segment_msgs=max_segment_msgs,
        caller_segments=seg_record.get("segments"),
    )
    # Tag external hydration explicitly on trajectories via extra tags in mapping path:
    # inject via segmented extra + re-map with embed_parent flag
    hierarchy = map_session_hierarchy(ir, segmented=segmented, embed_parent=embed_parent)

    # Stamp external provenance on all trajectories' tags/refs
    def stamp(mapped):
        traj = mapped.trajectory
        tags = list(traj.tags)
        if experience_grade:
            for t in ("experience_import", "cleaned_task_keys", "external_segmented"):
                if t not in tags:
                    tags.append(t)
            # remove archive tags if mapper added them
            tags = [
                t
                for t in tags
                if t not in ("external_pre_embed", "hydration_test", "not_author_segmented")
            ]
        else:
            for t in ("external_pre_embed", "hydration_test", "not_author_segmented"):
                if t not in tags:
                    tags.append(t)
        refs = {
            **traj.external_refs,
            "hydration_mode": "experience_grade" if experience_grade else "external_pre_embed",
            "segmentation_source": seg_record.get("segmentation_source"),
            "author_agent_segmented": False,
            "experience_grade": experience_grade,
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

    return {
        "session_id": seg_record.get("session_id"),
        "segment_count": seg_record.get("segment_count"),
        "segmentation_source": seg_record.get("segmentation_source"),
        "parent_id": hierarchy.parent.trajectory.id,
        "child_ids": [c.trajectory.id for c in hierarchy.children],
        "embedded": embed and not dry_run,
        "dry_run": dry_run,
        "experience_grade": experience_grade,
    }


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
    args = p.parse_args()
    if args.max_mem_gb is not None:
        apply_memory_cap(args.max_mem_gb)

    if not args.jsonl.exists():
        print(f"error: jsonl not found: {args.jsonl}", file=sys.stderr)
        print("run: PYTHONPATH=src python scripts/export_mongo_sessions_jsonl.py --sessions 100", file=sys.stderr)
        return 2

    # Stream JSONL — never load entire multi-GB export into RAM
    total_in_file = count_jsonl_lines(args.jsonl)
    records_iter = iter_sessions_jsonl(args.jsonl)
    # apply offset/limit without materializing full list
    records: list[dict[str, Any]] = []
    for i, rec in enumerate(records_iter):
        if i < args.offset:
            continue
        if args.limit is not None and len(records) >= args.limit:
            break
        records.append(rec)

    worker = args.worker_id or "main"
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
                "note": "Segmentation is external (not trajectory author / skill path)",
            },
            indent=2,
        )
    )

    if args.estimate_storage and records:
        est = estimate_segmented_storage(
            n_sessions=len(records),
            segments_per_session=6.0,
            embed_dim=load_config().embed_dim,
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

    config = load_config()
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
