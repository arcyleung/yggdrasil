#!/usr/bin/env python3
"""Seed a few demo-tenant trajectories for external trial isolation tests.

Usage:
  PYTHONPATH=src python scripts/seed_demo_org.py [--sqlite PATH] [--no-embed]

Writes trajectories with tenant_id=demo and tags including demo_seed.
Uses FakeEmbedder when --no-embed (default) so it runs offline.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running from repo root
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from yggdrasil.adapters.embed_views import get_embed_view
from yggdrasil.adapters.sqlite_store import SqliteTrajectoryStore
from yggdrasil.config import YggConfig, load_config
from yggdrasil.domain.enums import EffortFilterMode, FusionMode, TrajectoryStatus
from yggdrasil.domain.models import Outcome
from yggdrasil.services.embed_service import EmbedService
from yggdrasil.services.session_service import SessionService


def _minimal_config(sqlite_path: Path) -> YggConfig:
    return YggConfig(
        sqlite_path=sqlite_path,
        qdrant_url="http://localhost:6333",
        qdrant_collection="yggdrasil_trajectories",
        qdrant_api_key=None,
        embed_base_url="http://127.0.0.1:9/v1",
        embed_api_key=None,
        embed_model="fake",
        embed_dim=8,
        default_domain="coding",
        search_include_open=True,
        fusion=FusionMode.RRF,
        w_task=1.0,
        w_scaffold=1.0,
        effort_filter_mode=EffortFilterMode.INCLUSIVE_NULL,
        embed_view_version="coding_v1",
        mongo_uri=None,
        mongo_creds_file=Path("mongo_creds.txt"),
        scrub_content=False,
        tenancy_mode="off",
        default_tenant="lab",
        demo_enabled=True,
        demo_owner="demo",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed demo tenant trajectories")
    parser.add_argument("--sqlite", type=Path, default=None, help="SQLite path (default from config)")
    parser.add_argument(
        "--no-embed",
        action="store_true",
        default=True,
        help="Use in-memory fake embed/index (default)",
    )
    parser.add_argument("--live-embed", action="store_true", help="Use real embedder + Qdrant from env")
    args = parser.parse_args()

    if args.live_embed:
        cfg = load_config()
        sqlite_path = args.sqlite or cfg.sqlite_path
        from yggdrasil.mcp.app_context import AppContext

        ctx = AppContext.from_config(cfg)
        session = ctx.session_service
        store = ctx.store
    else:
        sqlite_path = args.sqlite or Path("./data/yggdrasil.db")
        cfg = _minimal_config(sqlite_path)
        # Offline fakes
        from tests.fakes import FakeEmbedder, FakeVectorIndex  # type: ignore

        # Prefer in-tree imports without tests on path
        class _FakeEmbedder:
            def __init__(self, dimensions: int = 8) -> None:
                self._dimensions = dimensions

            @property
            def model_name(self) -> str:
                return "fake-embed"

            @property
            def dimensions(self) -> int:
                return self._dimensions

            def embed_texts(self, texts):
                return [[0.1] * self._dimensions for _ in texts]

            def embed_one(self, text: str):
                return self.embed_texts([text])[0]

        class _FakeIndex:
            def __init__(self) -> None:
                self.points = {}

            def ensure_collection(self, *, vector_size: int) -> None:
                return None

            def upsert(self, point) -> None:
                self.points[point.trajectory_id] = point

            def upsert_many(self, points) -> None:
                for p in points:
                    self.upsert(p)

            def delete(self, trajectory_id: str) -> None:
                self.points.pop(trajectory_id, None)

            def search(self, query):
                return []

        store = SqliteTrajectoryStore(sqlite_path)
        embedder = _FakeEmbedder(cfg.embed_dim)
        index = _FakeIndex()
        view = get_embed_view(cfg.embed_view_version)
        embed_service = EmbedService(embedder, index, view, cfg)
        session = SessionService(store, embed_service, tenancy_enforced=False, default_tenant="lab")

    seeds = [
        {
            "task_text": "Demo: set up a minimal FastAPI hello world",
            "scaffold_text": "Python 3.11, FastAPI, uvicorn; return {\"hello\": \"world\"}",
            "tags": ["demo_seed", "fastapi", "experience_grade"],
        },
        {
            "task_text": "Demo: write a pytest fixture for temporary SQLite",
            "scaffold_text": "tmp_path fixture, sqlite3.connect, teardown close",
            "tags": ["demo_seed", "pytest", "experience_grade"],
        },
        {
            "task_text": "Demo: docker-compose with postgres and redis",
            "scaffold_text": "compose v2, healthchecks, named volumes",
            "tags": ["demo_seed", "docker", "experience_grade"],
        },
    ]

    created = []
    for item in seeds:
        traj = session.start_trajectory(
            task_text=item["task_text"],
            scaffold_text=item["scaffold_text"],
            tags=item["tags"],
            external_refs={
                "owner": "demo",
                "agent_id": "demo-seed-agent",
                "team": "demo",
                "experience_grade": True,
            },
            tenant_id="demo",
        )
        # Force tenant_id=demo even without principal (seed script sets explicitly)
        if traj.tenant_id != "demo":
            # Re-read and patch via direct SQL if default was lab
            store.connection.execute(
                "UPDATE trajectories SET tenant_id = 'demo' WHERE id = ?",
                (traj.id,),
            )
            store.connection.commit()
            traj = store.get(traj.id)
        traj = session.finalize_trajectory(
            trajectory_id=traj.id,
            outcome=Outcome(
                terminal_status=TrajectoryStatus.SUCCESS,
                summary="demo seed success",
                goal_satisfied=True,
            ),
        )
        # Ensure tenant after finalize path
        store.connection.execute(
            "UPDATE trajectories SET tenant_id = 'demo' WHERE id = ?",
            (traj.id,),
        )
        store.connection.commit()
        traj = store.get(traj.id)
        created.append(traj)
        print(f"seeded demo trajectory {traj.id} tenant={traj.tenant_id} tags={traj.tags}")

    print(f"done: {len(created)} demo trajectories in {sqlite_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
