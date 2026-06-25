#!/usr/bin/env python3
"""A/B timing harness: skill lookup (setup 1) vs no-search baseline (setup 2).

Setup 1 — search_experience / search_strategies (current task+scaffold only, gates on)
Setup 2 — perform as-is: no retrieval (records baseline planning stub time only;
          full execution wall times are *estimated* from task.naive_path / efficient_path)

Does NOT run multi-hour Bazel builds. It measures recall latency and whether an efficient
path is *discoverable* in experience memory (e.g. seeded Ray wheels trajectory).

Usage:
  PYTHONPATH=src python scripts/seed_synthetic_experience.py
  PYTHONPATH=src python scripts/eval_long_horizon_ab.py
  PYTHONPATH=src python scripts/eval_long_horizon_ab.py --json-out data/exports/long_horizon_ab.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from long_horizon_tasks import LONG_HORIZON_TASKS  # noqa: E402
from yggdrasil.mcp.app_context import AppContext  # noqa: E402


def _hit_summary(h) -> dict:
    tags = list(h.tags or [])
    tt = (h.task_text or "")[:200].replace("\n", " ")
    return {
        "trajectory_id": h.trajectory_id,
        "score": h.score,
        "status": h.status.value if hasattr(h.status, "value") else str(h.status),
        "tags": tags[:10],
        "task_preview": tt,
        "has_efficient_tag": "efficient_path" in tags or "skill_demo" in tags,
        "has_author_tag": "author_segmented" in tags,
    }


def setup1_skill_lookup(ctx: AppContext, task_spec: dict, *, limit: int = 8) -> dict:
    """Timed search_strategies with agent gates (search_experience skill policy)."""
    t0 = time.perf_counter()
    hits = ctx.search_service.search_strategies(
        task=task_spec["task_query"],
        scaffold=task_spec["scaffold_query"],
        domain="coding",
        include_open=True,
        apply_gates=True,
        include_archive=False,
        limit=limit,
    )
    dt = time.perf_counter() - t0
    gate = ctx.search_service.last_gate_result
    rows = [_hit_summary(h) for h in hits]
    efficient_hit = next(
        (r for r in rows if r["has_efficient_tag"] or "ray" in " ".join(r["tags"])),
        None,
    )
    # keyword relevance for ray task
    kw = ("ray", "bazel", "wheel", "nightly", "dask", "slurm", "parquet", "qdrant", "cuda", "oom")
    relevant = []
    for r in rows:
        blob = (r["task_preview"] + " " + " ".join(r["tags"])).lower()
        if any(k in blob for k in kw):
            relevant.append(r["trajectory_id"])

    return {
        "setup": "1_skill_lookup",
        "wall_sec_search": round(dt, 4),
        "n_hits": len(rows),
        "n_dropped_by_gates": len(gate.dropped) if gate else 0,
        "gate_warnings": list(gate.warnings) if gate else [],
        "top_hits": rows[:5],
        "found_efficient_path_candidate": efficient_hit is not None
        or any("wheel" in (r["task_preview"] or "").lower() for r in rows)
        or any("bazel" in (r["task_preview"] or "").lower() and "avoid" in (r["task_preview"] or "").lower() for r in rows),
        "relevant_hit_ids_sample": relevant[:5],
        "would_choose_efficient_if_ray": bool(
            task_spec["id"] == "ray_dev_setup"
            and (
                efficient_hit
                or any("wheel" in (r["task_preview"] or "").lower() for r in rows)
                or any("efficient_path" in r["tags"] for r in rows)
            )
        ),
    }


def setup2_as_is_baseline(task_spec: dict) -> dict:
    """No skill lookup: agent would execute naive path (execution not run; estimates only)."""
    t0 = time.perf_counter()
    # Simulate minimal planning without memory (constant-time stub)
    plan = {
        "approach": "naive_without_experience_memory",
        "first_steps": [
            "Interpret task from scratch",
            "Search web/docs if allowed (not timed here)",
            "Execute default heavy path unless docs found manually",
        ],
        "naive_path": task_spec.get("naive_path"),
        "efficient_path_if_known": task_spec.get("efficient_path"),
    }
    dt = time.perf_counter() - t0
    # Documented horizon estimates (not measured execution)
    est_naive = None
    est_eff = None
    if task_spec["id"] == "ray_dev_setup":
        est_naive = 4500  # >1h bazel on 8 cpus (illustrative)
        est_eff = 180  # ~3 min wheels
    return {
        "setup": "2_perform_as_is_no_skill_lookup",
        "wall_sec_planning_stub": round(dt, 6),
        "execution_run": False,
        "note": "Full horizon execution not run in harness; times below are documented estimates",
        "estimated_execution_sec_naive": est_naive,
        "estimated_execution_sec_efficient": est_eff,
        "plan_stub": plan,
    }


def compare_setups(s1: dict, s2: dict, task_spec: dict) -> dict:
    """Judge whether setup1 saves time if efficient path is in memory."""
    search_t = s1.get("wall_sec_search") or 0
    found = s1.get("would_choose_efficient_if_ray") or s1.get("found_efficient_path_candidate")
    out = {
        "search_overhead_sec": search_t,
        "skill_found_useful_hit": bool(s1.get("n_hits", 0) > 0 and not s1.get("gate_warnings")),
        "skill_found_any_hit": s1.get("n_hits", 0) > 0,
    }
    if task_spec["id"] == "ray_dev_setup" and s2.get("estimated_execution_sec_naive"):
        naive = s2["estimated_execution_sec_naive"]
        eff = s2["estimated_execution_sec_efficient"]
        if found and eff and naive:
            # setup1 total if agent trusts hit: search + efficient exec (est)
            t1 = search_t + eff
            t2 = s2.get("wall_sec_planning_stub", 0) + naive
            out["ray_demo"] = {
                "setup1_est_total_sec": t1,
                "setup2_est_total_sec": t2,
                "saved_sec_if_follow_hit": t2 - t1,
                "saved_ratio": round((t2 - t1) / t2, 3) if t2 else None,
                "narrative": (
                    "If skill returns wheels path (~3min) vs naive Bazel (>1h), "
                    f"lookup cost (~{search_t:.2f}s) is negligible vs saved hours."
                ),
            }
        else:
            out["ray_demo"] = {
                "setup1_est_total_sec": search_t + (naive or 0),
                "setup2_est_total_sec": naive,
                "narrative": "No efficient hit found; agent may still take naive path after failed/empty recall",
            }
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Long-horizon A/B: skill lookup vs as-is")
    p.add_argument("--task-id", action="append", default=None)
    p.add_argument("--json-out", type=Path, default=ROOT / "data" / "exports" / "long_horizon_ab.json")
    p.add_argument("--seed-ray", action="store_true", help="Seed Ray wheels experience before eval")
    args = p.parse_args()

    if args.seed_ray:
        from seed_synthetic_experience import seed_ray_nightly_wheels

        ctx0 = AppContext.from_config()
        tid = seed_ray_nightly_wheels(ctx0)
        print(f"seeded ray experience: {tid}")

    ctx = AppContext.from_config()
    tasks = LONG_HORIZON_TASKS
    if args.task_id:
        want = set(args.task_id)
        tasks = [t for t in tasks if t["id"] in want]

    results = []
    for spec in tasks:
        s1 = setup1_skill_lookup(ctx, spec)
        s2 = setup2_as_is_baseline(spec)
        cmp_ = compare_setups(s1, s2, spec)
        results.append(
            {
                "task_id": spec["id"],
                "title": spec["title"],
                "persona": spec["persona"],
                "horizon": spec["horizon"],
                "efficient_path": spec["efficient_path"],
                "naive_path": spec["naive_path"],
                "setup1": s1,
                "setup2": s2,
                "comparison": cmp_,
            }
        )
        print(f"\n=== {spec['id']} ({spec['persona']}) ===")
        print(f"  setup1 search: {s1['wall_sec_search']:.3f}s  hits={s1['n_hits']} dropped={s1['n_dropped_by_gates']}")
        if s1["top_hits"]:
            print(f"  top: {s1['top_hits'][0]['task_preview'][:100]!r}")
        else:
            print("  top: (none — proceed without prior steps)")
        print(f"  setup2 plan stub: {s2['wall_sec_planning_stub']*1000:.2f}ms (exec not run)")
        if "ray_demo" in cmp_:
            rd = cmp_["ray_demo"]
            print(f"  ray_demo: {rd.get('narrative', '')}")
            if "saved_sec_if_follow_hit" in rd:
                print(f"  est totals: skill+eff={rd['setup1_est_total_sec']:.0f}s vs naive={rd['setup2_est_total_sec']:.0f}s")

    payload = {
        "protocol": {
            "setup1": "search_strategies skill policy (task+scaffold only, gates ON)",
            "setup2": "perform as-is without skill lookup (execution estimated only)",
            "subagents": "assign persona per task; setup1 must call skill before acting; setup2 must not",
        },
        "tasks": results,
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
