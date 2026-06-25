#!/usr/bin/env python3
"""Skill-aligned segmented trajectory retrieval eval (search_strategies, not doc RAG).

Simulates the yggdrasil-trajectory-memory skill cold-start / mid-task search policy:
  - query with current task + scaffold ONLY (no full retry history)
  - prefer session_segment / external_pre_embed hits for hydration corpus
  - optional get_trajectory on top hit

Usage:
  PYTHONPATH=src python scripts/eval_segmented_retrieval_skill.py
  PYTHONPATH=src python scripts/eval_segmented_retrieval_skill.py --json-out data/exports/retrieval_eval.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from yggdrasil.mcp.app_context import AppContext

# Skill-style scenarios: concrete subgoals + scaffolds (not "coding agent tools" meta-probes)
SCENARIOS = [
    {
        "id": "debug_traceback",
        "task": "Debug Python traceback and fix exception in agent tool handler",
        "scaffold": "Python 3.12 coding agent; tools Read Grep Bash; pytest locally",
        "tags_any": ["session_segment"],
    },
    {
        "id": "mongo_import_segment",
        "task": "Import long Claude agent sessions from MongoDB and segment into sub-trajectories for embedding",
        "scaffold": "Yggdrasil PoC; SQLite + Qdrant; proxy_log request_body.messages; external_pre_embed hydration",
        "tags_any": ["session_segment", "external_pre_embed"],
    },
    {
        "id": "qdrant_embed_dim",
        "task": "Fix Qdrant collection embedding dimension mismatch with local vLLM embed model",
        "scaffold": "Qdrant docker; EMBED_DIM=1024; Qwen3-Embedding-0.6B; yggdrasil_trajectories collection",
        "tags_any": ["session_segment"],
    },
    {
        "id": "mcp_tool_wire",
        "task": "Wire MCP server tools for start_trajectory append_step finalize_trajectory search_strategies",
        "scaffold": "Python FastMCP stdio; session_service embed_service; trajectory experience memory",
        "tags_any": None,
    },
    {
        "id": "unrelated_control",
        "task": "Write a marketing blog post about artisanal coffee shops",
        "scaffold": "WordPress blog; no coding agent tools",
        "tags_any": ["session_segment"],
    },
]


def hit_row(h) -> dict:
    tid = getattr(h, "trajectory_id", None)
    tags = list(getattr(h, "tags", None) or [])
    status = getattr(h, "status", None)
    if hasattr(status, "value"):
        status = status.value
    task_preview = ""
    traj = getattr(h, "trajectory", None)
    if traj is not None and getattr(traj, "task_text", None):
        task_preview = traj.task_text[:160].replace("\n", " ")
    progress = getattr(h, "progress", None)
    prog_sum = ""
    if progress is not None and getattr(progress, "summary", None):
        prog_sum = progress.summary[:120]
    return {
        "trajectory_id": tid,
        "score": getattr(h, "score", None),
        "status": status,
        "tags": tags,
        "is_mongo_seg": bool(tid and "mongo-session-" in str(tid) and "-seg-" in str(tid)),
        "is_external_pre_embed": "external_pre_embed" in tags,
        "is_session_segment": "session_segment" in tags,
        "task_preview": task_preview,
        "progress_summary": prog_sum,
    }


def run_scenario(
    ctx: AppContext,
    sc: dict,
    *,
    limit: int,
    deep_read: bool,
    apply_gates: bool,
    include_archive: bool,
) -> dict:
    kwargs = dict(
        task=sc["task"],
        scaffold=sc["scaffold"],
        domain="coding",
        limit=limit,
        include_open=True,
        apply_gates=apply_gates,
        include_archive=include_archive,
    )
    # Default agent mode: do not force session_segment-only (hides empty relevance)
    if sc.get("tags_any") and include_archive:
        kwargs["tags_any"] = sc["tags_any"]
    hits = ctx.search_service.search_strategies(**kwargs)
    gate = ctx.search_service.last_gate_result
    rows = [hit_row(h) for h in hits]
    mongo_seg_n = sum(1 for r in rows if r["is_mongo_seg"])
    external_n = sum(1 for r in rows if r["is_external_pre_embed"])
    deep = None
    if deep_read and rows and rows[0].get("trajectory_id"):
        tid = rows[0]["trajectory_id"]
        try:
            traj = ctx.session_service.get_trajectory(tid, include_steps=True)
            steps = getattr(traj, "steps", None) or []
            deep = {
                "trajectory_id": tid,
                "n_steps": len(steps),
                "task_text_preview": (traj.task_text or "")[:200],
                "scaffold_preview": (traj.scaffold_text or "")[:200],
                "tags": list(traj.tags or []),
            }
        except Exception as exc:
            deep = {"error": str(exc), "trajectory_id": tid}
    return {
        "scenario_id": sc["id"],
        "query_task": sc["task"],
        "query_scaffold": sc["scaffold"],
        "tags_any": sc.get("tags_any"),
        "n_hits": len(rows),
        "mongo_seg_hits": mongo_seg_n,
        "external_pre_embed_hits": external_n,
        "top_hits": rows[:8],
        "deep_read_top": deep,
        "gate": {
            "applied": apply_gates,
            "include_archive": include_archive,
            "n_kept": len(rows),
            "n_dropped": len(gate.dropped) if gate else 0,
            "warnings": list(gate.warnings) if gate else [],
            "dropped_sample": (gate.dropped[:5] if gate else []),
        },
        "helpful_signal": {
            "has_mongo_seg_in_top3": any(r["is_mongo_seg"] for r in rows[:3]),
            "top_is_mongo_seg": bool(rows and rows[0]["is_mongo_seg"]),
            "control_should_be_weak": sc["id"] == "unrelated_control",
            "control_empty_or_weak": sc["id"] == "unrelated_control"
            and (len(rows) == 0 or (gate is not None and len(gate.dropped) > 0)),
            "empty_is_ok_for_agent": len(rows) == 0 and apply_gates and not include_archive,
        },
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Skill-aligned segmented retrieval eval")
    p.add_argument("--limit", type=int, default=8)
    p.add_argument("--deep-read", action="store_true", default=True)
    p.add_argument("--no-deep-read", action="store_true")
    p.add_argument("--json-out", type=Path, default=ROOT / "data" / "exports" / "retrieval_eval_skill.json")
    p.add_argument("--scenario", action="append", default=None, help="Run only scenario id(s)")
    p.add_argument("--no-gates", action="store_true", help="Disable agent retrieval gates")
    p.add_argument(
        "--include-archive",
        action="store_true",
        help="Include hydration_test/external_pre_embed (disables archive exclude gate)",
    )
    args = p.parse_args()
    deep = args.deep_read and not args.no_deep_read
    apply_gates = not args.no_gates

    ctx = AppContext.from_config()
    scenarios = SCENARIOS
    if args.scenario:
        want = set(args.scenario)
        scenarios = [s for s in SCENARIOS if s["id"] in want]

    results = [
        run_scenario(
            ctx,
            sc,
            limit=args.limit,
            deep_read=deep,
            apply_gates=apply_gates,
            include_archive=args.include_archive,
        )
        for sc in scenarios
    ]
    payload = {
        "skill": "yggdrasil-trajectory-memory / search_strategies",
        "policy": "current task+scaffold only; segment children preferred for experience recall",
        "scenarios": results,
        "summary": {
            "scenarios_run": len(results),
            "scenarios_with_mongo_seg_top3": sum(
                1 for r in results if r["helpful_signal"]["has_mongo_seg_in_top3"]
            ),
            "control_scenario": next((r for r in results if r["scenario_id"] == "unrelated_control"), None),
        },
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(json.dumps(payload["summary"], indent=2))
    for r in results:
        hs = r["helpful_signal"]
        g = r.get("gate") or {}
        print(
            f"  [{r['scenario_id']}] hits={r['n_hits']} dropped={g.get('n_dropped')} "
            f"mongo_seg={r['mongo_seg_hits']} top3_seg={hs['has_mongo_seg_in_top3']} "
            f"empty_ok={hs.get('empty_is_ok_for_agent')}"
        )
        if g.get("warnings"):
            print(f"      warnings={g['warnings'][:2]}")
        if r["top_hits"]:
            t0 = r["top_hits"][0]
            print(f"      top_id={str(t0.get('trajectory_id'))[:56]}… score={t0.get('score')}")
    print(f"wrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
