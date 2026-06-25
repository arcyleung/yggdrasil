#!/usr/bin/env python3
"""Seed high-quality author-style experiences so skill lookup can beat naive paths.

Example: Ray contribute docs — install nightly wheels (~3 min) vs full Bazel build (>1h).
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from yggdrasil.domain.enums import StepKind, TrajectoryStatus
from yggdrasil.domain.models import EffortLedger, EffortTotals, Outcome, Progress
from yggdrasil.mcp.app_context import AppContext


def seed_ray_nightly_wheels(ctx: AppContext, *, reembed: bool = True) -> str:
    """Author-style segment: efficient Ray Python setup via wheels, not full Bazel."""
    task = (
        "Prepare ray-project/ray for local Python development without full source build; "
        "follow Ray contribute docs building-ray-python section"
    )
    scaffold = (
        "Linux; Python 3.10+; ray-project/ray clone optional for code read; "
        "docs.ray.io/en/latest/ray-contribute/development.html#building-ray-python; "
        "pip install nightly or release wheels; avoid Bazel unless changing C++/core"
    )
    result = ctx.session_service.start_trajectory(
        task_text=task,
        scaffold_text=scaffold,
        domain="coding",
        tags=[
            "author_segmented",
            "experience_grade",
            "experience_import",
            "ray",
            "dev-setup",
            "efficient_path",
            "skill_demo",
        ],
        external_refs={
            "kind": "synthetic_author_experience",
            "source": "seed_synthetic_experience",
            "experience_grade": True,
            "owner": "docs",
            "agent_id": "seed-ray-wheels",
            "team": "lab-infra",
            "workspace": "/synthetic/ray-wheels",
            "project": "ray-dev-setup",
            "doc_url": "https://docs.ray.io/en/latest/ray-contribute/development.html#building-ray-python",
            "efficient_path": "nightly_or_release_wheels",
            "naive_path": "full_bazel_build",
            "estimated_wall_efficient_seconds": 180,
            "estimated_wall_naive_seconds": 4500,
        },
        artifacts=[
            {
                "kind": "url",
                "path_or_url": "https://docs.ray.io/en/latest/ray-contribute/development.html#building-ray-python",
                "label": "ray-contribute-python-build",
            }
        ],
    )
    trajectory_id = result.id

    steps = [
        (
            StepKind.NOTE,
            "Read Ray contribute development docs: building Ray Python section",
            {"url": "https://docs.ray.io/en/latest/ray-contribute/development.html#building-ray-python"},
        ),
        (
            StepKind.THOUGHT,
            "Full Bazel build of Ray is only required when changing C++/core; for Python API "
            "dev, docs recommend installing prebuilt/nightly wheels (~minutes) vs multi-hour source build",
            {},
        ),
        (
            StepKind.COMMAND,
            "pip install -U ray[default] or documented nightly wheel index (environment-specific)",
            {"estimated_seconds": 120, "avoid": "bazel build //:ray"},
        ),
        (
            StepKind.CHECKPOINT,
            "Ray import works; skip full repo Bazel unless modifying native code",
            {},
        ),
    ]
    for kind, summary, payload in steps:
        is_cp = kind == StepKind.CHECKPOINT
        ctx.session_service.append_step(
            trajectory_id=trajectory_id,
            kind=kind,
            summary=summary,
            payload=payload,
            is_checkpoint=is_cp,
            progress=Progress(
                phase="setup",
                summary="Ray Python via wheels; Bazel deferred",
                steps_count=1,
            )
            if is_cp
            else None,
        )

    ctx.session_service.finalize_trajectory(
        trajectory_id=trajectory_id,
        outcome=Outcome(
            terminal_status=TrajectoryStatus.SUCCESS,
            summary=(
                "Used Ray docs efficient path: install nightly/release wheels for Python dev "
                "(~3 min) instead of full Bazel build (>1h on 8 CPUs). Only build from source "
                "when changing C++/core."
            ),
            goal_satisfied=True,
            signals={
                "doc": "ray-contribute-development-building-ray-python",
                "efficient_path": "wheels",
                "naive_path": "bazel_full_build",
            },
        ),
        effort=EffortLedger(
            totals=EffortTotals(
                wall_clock_seconds=180.0,
                llm_api_usd=0.05,
                failure_waste_seconds=0.0,
            ),
            notes="Synthetic author experience for A/B skill demo; estimates illustrative",
        ),
        progress=Progress(
            phase="done",
            summary="Ray Python ready via wheels; documented avoid full Bazel for pure-Python work",
            steps_count=4,
            milestones=["read_docs", "choose_wheels", "verify_import"],
        ),
    )
    return trajectory_id


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--ray-wheels", action="store_true", default=True)
    args = p.parse_args()
    ctx = AppContext.from_config()
    t0 = time.perf_counter()
    tid = seed_ray_nightly_wheels(ctx)
    dt = time.perf_counter() - t0
    print(f"seeded trajectory_id={tid} in {dt:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
