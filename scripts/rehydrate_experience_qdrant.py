#!/usr/bin/env python3
"""Clean prior noisy retrieval corpus and rehydrate Qdrant with experience+artifact structure.

Steps:
  1) Optional: reset SQLite + drop/recreate Qdrant collection (fresh PoC)
  2) Seed lab team experiences (Alice/Bob/Carol with artifacts, owner/agent/team)
  3) Seed classic Ray wheels author experience (A/B demo)
  4) Optional: import mongo JSONL with experience_grade cleaning only (no external_pre_embed noise tags as trusted)
  5) Probe lab Ray discovery search

This deliberately prioritizes author/lab experience over bulk hydration noise that failed
agent retrieval gates previously.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from yggdrasil.config import YggConfig, load_config
from yggdrasil.mcp.app_context import AppContext


def _reset_sqlite(cfg: YggConfig) -> None:
    path = Path(cfg.sqlite_path)
    if path.exists():
        bak = path.with_suffix(path.suffix + f".bak.{int(time.time())}")
        shutil.move(str(path), str(bak))
        print(f"backed up sqlite -> {bak}")
    path.parent.mkdir(parents=True, exist_ok=True)


def _reset_qdrant(cfg: YggConfig) -> None:
    from qdrant_client import QdrantClient

    client = QdrantClient(url=cfg.qdrant_url, api_key=cfg.qdrant_api_key or None)
    name = cfg.qdrant_collection
    try:
        client.delete_collection(name)
        print(f"deleted qdrant collection {name}")
    except Exception as exc:
        print(f"qdrant delete skipped/failed (ok if missing): {exc}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--fresh",
        action="store_true",
        help="Backup+reset SQLite and drop Qdrant collection before seed",
    )
    p.add_argument("--skip-lab", action="store_true", help="Skip lab Alice/Bob/Carol seed")
    p.add_argument("--skip-ray-wheels", action="store_true", help="Skip synthetic ray wheels seed")
    p.add_argument(
        "--mongo-jsonl",
        type=str,
        default="",
        help="Optional path to mongo sessions JSONL for experience_grade import (small sample recommended)",
    )
    p.add_argument("--mongo-limit", type=int, default=50, help="Max parent sessions from mongo jsonl")
    p.add_argument("--probe", action="store_true", default=True)
    p.add_argument("--no-probe", action="store_true")
    args = p.parse_args()
    probe = args.probe and not args.no_probe

    cfg = load_config()
    if args.fresh:
        print("=== fresh reset ===")
        _reset_sqlite(cfg)
        _reset_qdrant(cfg)

    ctx = AppContext.from_config()
    ctx.index.ensure_collection(vector_size=ctx.config.embed_dim)

    seeded_ids: list[str] = []

    if not args.skip_lab:
        print("=== seed lab team experiences ===")
        from seed_lab_team_experiences import seed_lab_ray_experiences

        t0 = time.perf_counter()
        rows = seed_lab_ray_experiences(ctx)
        print(f"lab seeded {len(rows)} in {time.perf_counter() - t0:.2f}s")
        for r in rows:
            seeded_ids.append(r["trajectory_id"])
            print(f"  {r['owner']}/{r['agent_id']} {r['path']} -> {r['trajectory_id']}")

    if not args.skip_ray_wheels:
        print("=== seed ray wheels author experience ===")
        from seed_synthetic_experience import seed_ray_nightly_wheels

        t0 = time.perf_counter()
        tid = seed_ray_nightly_wheels(ctx)
        seeded_ids.append(tid)
        print(f"ray wheels id={tid} in {time.perf_counter() - t0:.2f}s")

    if args.mongo_jsonl:
        jsonl = Path(args.mongo_jsonl)
        if not jsonl.is_file():
            print(f"WARN: mongo jsonl not found: {jsonl}")
        else:
            print(
                f"=== mongo jsonl noted ({jsonl}, limit={args.mongo_limit}) ===\n"
                "Bulk mongo pre-embed is intentionally NOT re-run here: it polluted retrieval\n"
                "with external_pre_embed / noisy tasks. Use scripts/mongo_importer_pre_embed.py\n"
                "--experience-grade only if you explicitly want archive corpus (gated out by default)."
            )

    if probe:
        print("\n=== probe: team Ray setup (search_mode=lab, team=lab-infra) ===")
        hits = ctx.search_service.search_strategies(
            task=(
                "I need to set up Ray for Python development on the lab machine. "
                "Someone on the team may have done this already — find their experience, "
                "setup notes, scripts, and whether wheels or full Bazel worked."
            ),
            scaffold="lab Linux; Python 3.11; team=lab-infra; owner unknown; avoid duplicating work",
            team="lab-infra",
            search_mode="lab",
            experience_grade_only=True,
            prefer_low_waste=True,
            limit=6,
        )
        gate = ctx.search_service.last_gate_result
        if gate and gate.warnings:
            print(f"gate warnings: {gate.warnings}")
        if not hits:
            print("NO HITS — check embed service / qdrant / seeds")
        for i, h in enumerate(hits, 1):
            arts = [
                f"{a.kind}:{a.path_or_url.split('/')[-1] if '/' in a.path_or_url else a.path_or_url}"
                for a in (h.artifacts or [])[:4]
            ]
            print(
                f"#{i} owner={h.owner or '?'} agent={h.agent_id or '?'} "
                f"score={h.score:.4f} waste={h.effort_totals.failure_waste_seconds} "
                f"status={h.status.value}"
            )
            print(f"    task: {h.task_text[:100]}")
            if arts:
                print(f"    artifacts: {arts}")
            if h.workspace:
                print(f"    workspace: {h.workspace}")

        print("\n=== probe: management anti-duplication (prefer low waste) ===")
        hits2 = ctx.search_service.search_strategies(
            task="Ray local Python setup; avoid full Bazel if wheels suffice",
            scaffold="lab; team=lab-infra",
            team="lab-infra",
            search_mode="lab",
            prefer_low_waste=True,
            limit=3,
        )
        for i, h in enumerate(hits2, 1):
            print(
                f"#{i} owner={h.owner} waste={h.effort_totals.failure_waste_seconds} "
                f"wall={h.effort_totals.wall_clock_seconds} id={h.trajectory_id[:8]}..."
            )

    print(f"\ndone. seeded_core_ids={len(seeded_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
