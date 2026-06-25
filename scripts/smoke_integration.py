#!/usr/bin/env python3
"""Minimal live integration smoke: start_trajectory + search_strategies via AppContext.

Requires: Qdrant, vLLM (or other embed endpoint), .env configured.
Does not use MCP transport — same services the MCP tools call.

Usage (from repo root):
  python scripts/smoke_integration.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# repo root on path when run as script
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from yggdrasil.domain.enums import StepKind, TrajectoryStatus
from yggdrasil.domain.models import EffortLedger, EffortTotals, Outcome, Progress
from yggdrasil.mcp.app_context import AppContext


MOCK_TRACES = [
    {
        "task": "Fix flaky pytest in auth refresh endpoint returning 401 on valid refresh token",
        "scaffold": "FastAPI + SQLAlchemy service; tests under tests/test_auth.py; branch main",
        "tags": ["python", "fastapi", "auth", "pytest"],
        "steps": [
            ("thought", "Inspect refresh handler and token rotation logic"),
            ("tool_call", "read_file app/auth/refresh.py"),
            ("tool_result", "Found race on token reuse after rotation"),
            ("edit", "Serialize refresh with row lock; add regression test"),
            ("command", "pytest -q tests/test_auth.py"),
        ],
        "finalize": {
            "status": TrajectoryStatus.SUCCESS,
            "summary": "Fixed rotation race; auth tests green",
            "goal_satisfied": True,
            "effort_wall": 1800.0,
            "llm_usd": 0.42,
        },
    },
    {
        "task": "Speed up HuggingFace dataset download blocking agent loop",
        "scaffold": "ML training notebook; dataset ~20GB; machine has broadband, 64GB RAM, no GPU in this step",
        "tags": ["python", "huggingface", "io"],
        "steps": [
            ("thought", "Download was single-threaded and re-fetched on retry"),
            ("command", "huggingface-cli download ... with resume"),
            ("note", "Partial progress only; still downloading"),
        ],
        "finalize": {
            "status": TrajectoryStatus.PARTIAL,
            "summary": "Resumable download configured; transfer incomplete",
            "goal_satisfied": False,
            "remaining_work": "Wait for full dataset; then run training job",
            "effort_wall": 7200.0,
            "llm_usd": 0.15,
            "network_bytes": 8_000_000_000,
        },
    },
    {
        "task": "Debug CUDA OOM in training loop on RTX 4090 24GB",
        "scaffold": "PyTorch training script; batch_size=32; model ~7B; mixed precision off",
        "tags": ["python", "pytorch", "cuda", "oom"],
        "steps": [
            ("command", "python train.py"),
            ("tool_result", "torch.cuda.OutOfMemoryError at step 12"),
            ("edit", "Enable AMP; reduce batch_size to 8; gradient checkpointing"),
        ],
        "finalize": {
            "status": TrajectoryStatus.FAIL,
            "summary": "Still OOM after batch 8; need smaller model or CPU offload",
            "goal_satisfied": False,
            "remaining_work": "Try batch 2 + offload or smaller checkpoint",
            "effort_wall": 2400.0,
            "gpu_seconds": 900.0,
            "llm_usd": 0.55,
            "failure_waste_seconds": 600.0,
        },
    },
]


def _print_hit(hit, i: int) -> None:
    scores = hit.scores
    fused = scores.fused if scores else hit.score
    print(f"  [{i}] id={hit.trajectory_id[:8]}… status={hit.status.value} score={fused}")
    if hit.progress and hit.progress.summary:
        print(f"      progress: {hit.progress.summary[:100]}")
    if hit.outcome:
        print(f"      outcome: {hit.outcome.terminal_status.value} — {hit.outcome.summary[:80]}")
    totals = hit.effort_totals
    print(
        f"      effort: wall={totals.wall_clock_seconds}s "
        f"gpu={totals.gpu_seconds}s llm_usd={totals.llm_api_usd} "
        f"waste={totals.failure_waste_seconds}s"
    )


def main() -> int:
    print("=== Yggdrasil smoke: start_trajectory + search_strategies ===\n")
    ctx = AppContext.from_config()
    cfg = ctx.config
    print(f"sqlite={cfg.sqlite_path}")
    print(f"qdrant={cfg.qdrant_url} collection={cfg.qdrant_collection}")
    print(f"embed={cfg.embed_base_url} model={cfg.embed_model} dim={cfg.embed_dim}\n")

    created_ids: list[str] = []

    # --- start_trajectory (+ append/finalize for richer traces) ---
    print("--- start_trajectory (3 mock traces) ---")
    for i, mock in enumerate(MOCK_TRACES, 1):
        t = ctx.session_service.start_trajectory(
            task_text=mock["task"],
            scaffold_text=mock["scaffold"],
            tags=mock["tags"],
            progress=Progress(phase="init", summary="smoke test seed", steps_count=0),
        )
        created_ids.append(t.id)
        print(f"[{i}] started {t.id[:8]}… index_state={t.index_state.value} status={t.status.value}")
        print(f"    task: {mock['task'][:72]}…")

        for kind, summary in mock["steps"]:
            out = ctx.session_service.append_step(
                trajectory_id=t.id,
                kind=StepKind(kind),
                summary=summary,
                progress=Progress(
                    phase="work",
                    summary=summary,
                    steps_count=0,
                    last_step_summary=summary,
                ),
            )
            t = out["trajectory"] if isinstance(out, dict) else out

        fin = mock["finalize"]
        totals = EffortTotals(
            wall_clock_seconds=fin.get("effort_wall"),
            gpu_seconds=fin.get("gpu_seconds"),
            llm_api_usd=fin.get("llm_usd"),
            network_bytes=fin.get("network_bytes"),
            failure_waste_seconds=fin.get("failure_waste_seconds"),
        )
        t = ctx.session_service.finalize_trajectory(
            trajectory_id=t.id,
            outcome=Outcome(
                terminal_status=fin["status"],
                summary=fin["summary"],
                goal_satisfied=fin.get("goal_satisfied"),
                remaining_work=fin.get("remaining_work"),
            ),
            effort=EffortLedger(totals=totals),
            progress=Progress(
                phase="done",
                summary=fin["summary"],
                steps_count=len(mock["steps"]),
                last_step_summary=mock["steps"][-1][1],
            ),
        )
        print(f"    finalized status={t.status.value} effort_wall={totals.wall_clock_seconds}s\n")

    # --- search_strategies with query-like prompts ---
    queries = [
        {
            "label": "auth / pytest / refresh 401",
            "task": "JWT refresh returns 401 intermittently; need fix and tests",
            "scaffold": "FastAPI auth service with pytest suite",
        },
        {
            "label": "HF dataset download slow",
            "task": "Large huggingface dataset download is blocking; want faster/resumable path",
            "scaffold": "Python ML notebook, multi-GB dataset",
        },
        {
            "label": "CUDA OOM training",
            "task": "Training OOMs on 24GB GPU; reduce memory footprint",
            "scaffold": "PyTorch train.py batch training",
        },
        {
            "label": "unrelated query (control)",
            "task": "Write a marketing blog post about coffee shops",
            "scaffold": "No code repo; content writing only",
        },
    ]

    print("--- search_strategies ---")
    all_ok = True
    for q in queries:
        print(f"\nQuery: {q['label']}")
        print(f"  task={q['task'][:70]}…")
        hits = ctx.search_service.search_strategies(
            task=q["task"],
            scaffold=q["scaffold"],
            domain="coding",
            include_open=True,
            limit=5,
        )
        if not hits:
            print("  (no hits)")
            if q["label"] != "unrelated query (control)":
                all_ok = False
            continue
        for i, hit in enumerate(hits, 1):
            _print_hit(hit, i)
        top_id = hits[0].trajectory_id
        if top_id in created_ids:
            print(f"  ✓ top hit is one of our seeded trajectories")
        else:
            print(f"  · top hit from prior data: {top_id[:8]}…")

    print("\n--- get_trajectory (first seeded) ---")
    got = ctx.session_service.get_trajectory(trajectory_id=created_ids[0], include_steps=True)
    full = got["trajectory"] if isinstance(got, dict) else got
    steps = got["steps"] if isinstance(got, dict) else ctx.store.get_steps(created_ids[0])
    print(f"id={full.id} steps={len(steps)} status={full.status.value}")
    print(f"outcome={full.outcome.summary if full.outcome else None}")

    print("\n=== smoke complete ===")
    print(json.dumps({"seeded_ids": created_ids, "ok": all_ok}, indent=2))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
