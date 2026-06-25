#!/usr/bin/env python3
"""Seed lab-team experience trajectories (Alice/Bob/Carol agents) with artifacts.

Models the management scenario:
  - Multiple engineers each run agents that update Yggdrasil after meaningful work
  - A later agent (Alice's report agent) searches experiences without knowing who
    did Ray setup — embedding finds relevant runs; artifacts/owner/workspace pin deliverables
  - Prevents token waste / toe-stepping by discovering efficient paths already tried
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from yggdrasil.domain.artifacts import ArtifactRef
from yggdrasil.domain.enums import StepKind, TrajectoryStatus
from yggdrasil.domain.models import EffortLedger, EffortTotals, Outcome, Progress
from yggdrasil.mcp.app_context import AppContext


def _refs(
    *,
    owner: str,
    agent_id: str,
    team: str = "lab-infra",
    workspace: str,
    project: str = "ray-dev-setup",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = {
        "source": "lab_team_seed",
        "experience_grade": True,
        "owner": owner,
        "agent_id": agent_id,
        "team": team,
        "workspace": workspace,
        "project": project,
        "kind": "author_experience",
    }
    if extra:
        base.update(extra)
    return base


def _seed_one(
    ctx: AppContext,
    *,
    owner: str,
    agent_id: str,
    task: str,
    scaffold: str,
    tags: list[str],
    refs: dict[str, Any],
    steps: list[tuple[StepKind, str, dict[str, Any]]],
    artifacts: list[ArtifactRef],
    outcome_summary: str,
    effort_wall: float,
    waste: float = 0.0,
    success: bool = True,
    remaining_work: str | None = None,
) -> str:
    result = ctx.session_service.start_trajectory(
        task_text=task,
        scaffold_text=scaffold,
        domain="coding",
        tags=tags,
        external_refs=refs,
        artifacts=artifacts,
    )
    tid = result.id
    for i, (kind, summary, payload) in enumerate(steps, start=1):
        is_cp = kind == StepKind.CHECKPOINT
        ctx.session_service.append_step(
            trajectory_id=tid,
            kind=kind,
            summary=summary,
            payload=payload,
            is_checkpoint=is_cp,
            progress=Progress(
                phase="setup" if not is_cp else "verify",
                summary=summary[:120],
                steps_count=i,
            )
            if is_cp
            else None,
        )
    # Ensure artifacts merged on meta (in case step harvest missed any)
    ctx.session_service.update_trajectory_meta(
        trajectory_id=tid,
        artifacts=[a.model_dump(mode="json") for a in artifacts],
        merge_artifacts=True,
    )
    ctx.session_service.finalize_trajectory(
        trajectory_id=tid,
        outcome=Outcome(
            terminal_status=TrajectoryStatus.SUCCESS if success else TrajectoryStatus.PARTIAL,
            summary=outcome_summary,
            goal_satisfied=success,
            remaining_work=remaining_work,
            signals={
                "owner": owner,
                "agent_id": agent_id,
                "team": refs.get("team"),
                "project": refs.get("project"),
            },
        ),
        effort=EffortLedger(
            totals=EffortTotals(
                wall_clock_seconds=effort_wall,
                llm_api_usd=round(effort_wall / 3600.0 * 2.0, 3),
                failure_waste_seconds=waste,
            ),
            notes=f"lab seed: {owner}/{agent_id}",
        ),
        progress=Progress(
            phase="done" if success else "blocked",
            summary=outcome_summary[:200],
            steps_count=len(steps),
            milestones=[s[1][:40] for s in steps if s[0] == StepKind.CHECKPOINT],
        ),
    )
    return tid


def seed_lab_ray_experiences(ctx: AppContext) -> list[dict[str, str]]:
    """Seed complementary/conflicting lab experiences for Ray setup discovery."""
    seeded: list[dict[str, str]] = []

    # --- Bob agent-3: efficient path (wheels) — gold for Alice's search ---
    bob_ws = "/lab/workspaces/bob/agent-3-ray-wheels"
    tid = _seed_one(
        ctx,
        owner="bob",
        agent_id="bob-agent-3",
        task=(
            "Set up ray-project/ray for local Python API development on lab Linux; "
            "avoid multi-hour source builds if possible"
        ),
        scaffold=(
            "lab Linux; Python 3.11; team=lab-infra; workspace="
            + bob_ws
            + "; ray clone at /lab/src/ray; pip available; no GPU required for import smoke"
        ),
        tags=[
            "author_segmented",
            "experience_grade",
            "lab_team",
            "ray",
            "dev-setup",
            "efficient_path",
            "owner:bob",
        ],
        refs=_refs(
            owner="bob",
            agent_id="bob-agent-3",
            workspace=bob_ws,
            extra={
                "id": "lab-bob-agent-3-ray-wheels",
                "efficient_path": "nightly_or_release_wheels",
                "naive_path": "full_bazel_build",
                "doc_url": "https://docs.ray.io/en/latest/ray-contribute/development.html#building-ray-python",
            },
        ),
        steps=[
            (
                StepKind.NOTE,
                "Read Ray contribute docs building-ray-python section",
                {
                    "url": "https://docs.ray.io/en/latest/ray-contribute/development.html#building-ray-python",
                    "artifacts": [
                        {
                            "kind": "url",
                            "path_or_url": "https://docs.ray.io/en/latest/ray-contribute/development.html#building-ray-python",
                            "label": "ray-contribute-python-build",
                        }
                    ],
                },
            ),
            (
                StepKind.THOUGHT,
                "Full Bazel only needed for C++/core changes; for Python API dev use wheels (~minutes)",
                {},
            ),
            (
                StepKind.COMMAND,
                "pip install -U 'ray[default]' from release/nightly index; skip bazel build //:ray",
                {"estimated_seconds": 120, "avoid": "bazel build //:ray"},
            ),
            (
                StepKind.COMMAND,
                "Wrote setup notes and import smoke script for teammates",
                {
                    "artifacts": [
                        {
                            "kind": "md",
                            "path_or_url": f"{bob_ws}/notes/ray_python_setup.md",
                            "label": "setup-notes",
                            "workspace": bob_ws,
                        },
                        {
                            "kind": "code",
                            "path_or_url": f"{bob_ws}/scripts/smoke_import_ray.py",
                            "label": "smoke-import",
                            "workspace": bob_ws,
                        },
                    ]
                },
            ),
            (
                StepKind.CHECKPOINT,
                "ray import works; documented wheel path; Bazel deferred",
                {
                    "path": f"{bob_ws}/notes/ray_python_setup.md",
                    "workspace": bob_ws,
                },
            ),
        ],
        artifacts=[
            ArtifactRef(
                kind="md",
                path_or_url=f"{bob_ws}/notes/ray_python_setup.md",
                label="Ray Python setup notes (wheels path)",
                step_seq=4,
                workspace=bob_ws,
            ),
            ArtifactRef(
                kind="code",
                path_or_url=f"{bob_ws}/scripts/smoke_import_ray.py",
                label="Import smoke test",
                step_seq=4,
                workspace=bob_ws,
            ),
            ArtifactRef(
                kind="url",
                path_or_url="https://docs.ray.io/en/latest/ray-contribute/development.html#building-ray-python",
                label="Upstream contribute docs",
                step_seq=1,
            ),
        ],
        outcome_summary=(
            "Bob agent-3: Ray Python via pip wheels in ~3 min. Artifacts: setup notes + smoke script "
            "in bob agent-3 workspace. Do NOT full Bazel unless changing C++/core."
        ),
        effort_wall=180.0,
        waste=0.0,
    )
    seeded.append({"owner": "bob", "agent_id": "bob-agent-3", "trajectory_id": tid, "path": "wheels"})

    # --- Carol agent-7: naive/expensive Bazel attempt (partial, high waste) — anti-pattern ---
    carol_ws = "/lab/workspaces/carol/agent-7-ray-bazel"
    tid = _seed_one(
        ctx,
        owner="carol",
        agent_id="carol-agent-7",
        task="Build ray from source with Bazel for local Python development on lab machine",
        scaffold=(
            "lab Linux 8 CPU; Python 3.11; workspace="
            + carol_ws
            + "; ray clone; attempting full source build without reading contribute wheels guidance"
        ),
        tags=[
            "author_segmented",
            "experience_grade",
            "lab_team",
            "ray",
            "dev-setup",
            "naive_path",
            "high_waste",
            "owner:carol",
        ],
        refs=_refs(
            owner="carol",
            agent_id="carol-agent-7",
            workspace=carol_ws,
            extra={
                "id": "lab-carol-agent-7-ray-bazel",
                "efficient_path": "nightly_or_release_wheels",
                "naive_path": "full_bazel_build",
                "warning": "high_token_and_wall_waste_for_python_only_dev",
            },
        ),
        steps=[
            (
                StepKind.COMMAND,
                "Started bazel build //:ray; still compiling after 45+ minutes",
                {"estimated_seconds": 2700},
            ),
            (
                StepKind.NOTE,
                "Logged partial build output; aborted due to time; did not produce importable wheel",
                {
                    "artifacts": [
                        {
                            "kind": "log",
                            "path_or_url": f"{carol_ws}/logs/bazel_build_partial.log",
                            "workspace": carol_ws,
                        }
                    ]
                },
            ),
            (
                StepKind.CHECKPOINT,
                "Partial: Bazel path expensive; recommend wheels for Python-only work",
                {"path": f"{carol_ws}/logs/bazel_build_partial.log"},
            ),
        ],
        artifacts=[
            ArtifactRef(
                kind="log",
                path_or_url=f"{carol_ws}/logs/bazel_build_partial.log",
                label="Aborted Bazel build log",
                step_seq=2,
                workspace=carol_ws,
                note="Do not copy this path for Python-only Ray setup",
            ),
        ],
        outcome_summary=(
            "Carol agent-7: full Bazel Ray build aborted after ~45m with no usable Python env. "
            "Wasteful for API-only work; prefer Bob agent-3 wheel path."
        ),
        effort_wall=2700.0,
        waste=2700.0,
        success=False,
        remaining_work="Switch to pip wheels per contribute docs; only resume Bazel if C++/core changes needed",
    )
    seeded.append({"owner": "carol", "agent_id": "carol-agent-7", "trajectory_id": tid, "path": "bazel_waste"})

    # --- Alice agent-1: unrelated ETL (noise) — should not rank for Ray setup ---
    alice_ws = "/lab/workspaces/alice/agent-1-etl-report"
    tid = _seed_one(
        ctx,
        owner="alice",
        agent_id="alice-agent-1",
        task="Build quarterly lab metrics ETL from parquet logs into a manager report markdown",
        scaffold=(
            "lab Linux; dask optional; workspace="
            + alice_ws
            + "; parquet inputs under /lab/data/metrics; no Ray dependency"
        ),
        tags=[
            "author_segmented",
            "experience_grade",
            "lab_team",
            "etl",
            "reporting",
            "owner:alice",
        ],
        refs=_refs(
            owner="alice",
            agent_id="alice-agent-1",
            workspace=alice_ws,
            project="lab-metrics-report",
            extra={"id": "lab-alice-agent-1-etl"},
        ),
        steps=[
            (
                StepKind.COMMAND,
                "Aggregated parquet metrics; wrote draft report",
                {
                    "artifacts": [
                        {
                            "kind": "md",
                            "path_or_url": f"{alice_ws}/out/quarterly_metrics_draft.md",
                            "workspace": alice_ws,
                        },
                        {
                            "kind": "data",
                            "path_or_url": f"{alice_ws}/out/metrics_summary.parquet",
                            "workspace": alice_ws,
                        },
                    ]
                },
            ),
            (
                StepKind.CHECKPOINT,
                "Draft report ready for boss review",
                {"path": f"{alice_ws}/out/quarterly_metrics_draft.md"},
            ),
        ],
        artifacts=[
            ArtifactRef(
                kind="md",
                path_or_url=f"{alice_ws}/out/quarterly_metrics_draft.md",
                label="Quarterly metrics draft",
                step_seq=1,
                workspace=alice_ws,
            ),
            ArtifactRef(
                kind="data",
                path_or_url=f"{alice_ws}/out/metrics_summary.parquet",
                label="Metrics summary table",
                step_seq=1,
                workspace=alice_ws,
            ),
        ],
        outcome_summary="Alice agent-1: ETL + draft metrics report (unrelated to Ray setup).",
        effort_wall=900.0,
    )
    seeded.append({"owner": "alice", "agent_id": "alice-agent-1", "trajectory_id": tid, "path": "etl_noise"})

    # --- Bob agent-5: partial open work on Ray cluster config (in progress) ---
    bob5_ws = "/lab/workspaces/bob/agent-5-ray-cluster"
    tid = _seed_one(
        ctx,
        owner="bob",
        agent_id="bob-agent-5",
        task="Document minimal Ray cluster bring-up notes for lab shared GPU node (follow-on to Python setup)",
        scaffold=f"lab Linux; workspace={bob5_ws}; depends on Python Ray install first",
        tags=[
            "author_segmented",
            "experience_grade",
            "lab_team",
            "ray",
            "cluster",
            "partial",
            "owner:bob",
        ],
        refs=_refs(
            owner="bob",
            agent_id="bob-agent-5",
            workspace=bob5_ws,
            project="ray-cluster-lab",
            extra={"id": "lab-bob-agent-5-cluster-partial", "depends_on": "lab-bob-agent-3-ray-wheels"},
        ),
        steps=[
            (
                StepKind.NOTE,
                "Started cluster config draft; blocked on GPU node access ticket",
                {
                    "artifacts": [
                        {
                            "kind": "md",
                            "path_or_url": f"{bob5_ws}/notes/cluster_wip.md",
                            "workspace": bob5_ws,
                        }
                    ]
                },
            ),
            (
                StepKind.CHECKPOINT,
                "Partial: cluster doc WIP; needs GPU access",
                {"path": f"{bob5_ws}/notes/cluster_wip.md"},
            ),
        ],
        artifacts=[
            ArtifactRef(
                kind="md",
                path_or_url=f"{bob5_ws}/notes/cluster_wip.md",
                label="Cluster WIP notes",
                step_seq=1,
                workspace=bob5_ws,
            ),
        ],
        outcome_summary="Bob agent-5: cluster notes partial; blocked on GPU ticket. Python install already done by agent-3.",
        effort_wall=600.0,
        success=False,
        remaining_work="Finish cluster section after GPU node access; link to agent-3 wheel setup notes",
    )
    seeded.append({"owner": "bob", "agent_id": "bob-agent-5", "trajectory_id": tid, "path": "cluster_partial"})

    return seeded


def main() -> int:
    p = argparse.ArgumentParser(description="Seed lab team experiences with artifacts")
    p.add_argument("--probe", action="store_true", help="Run Ray discovery search after seed")
    args = p.parse_args()

    ctx = AppContext.from_config()
    t0 = time.perf_counter()
    rows = seed_lab_ray_experiences(ctx)
    dt = time.perf_counter() - t0
    print(f"seeded {len(rows)} lab trajectories in {dt:.2f}s")
    for r in rows:
        print(f"  {r['owner']}/{r['agent_id']} path={r['path']} id={r['trajectory_id']}")

    if args.probe:
        print("\n--- probe: Alice's agent searches team Ray setup (search_mode=lab) ---")
        hits = ctx.search_service.search_strategies(
            task=(
                "Set up Ray for local Python development on lab Linux without full source build; "
                "find prior team experience and any setup notes or scripts"
            ),
            scaffold="lab Linux; Python 3.11; team=lab-infra; unknown owner; need efficient path",
            team="lab-infra",
            search_mode="lab",
            experience_grade_only=True,
            prefer_low_waste=True,
            limit=5,
        )
        for i, h in enumerate(hits, 1):
            arts = ", ".join(f"{a.kind}:{a.path_or_url}" for a in (h.artifacts or [])[:3])
            print(
                f"  #{i} owner={h.owner} agent={h.agent_id} score={h.score:.4f} "
                f"waste={h.effort_totals.failure_waste_seconds} arts=[{arts}]"
            )
            print(f"      task={h.task_text[:90]}...")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
