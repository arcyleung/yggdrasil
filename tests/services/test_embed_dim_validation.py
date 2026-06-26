from datetime import datetime, timezone

import pytest

from yggdrasil.config import YggConfig
from yggdrasil.domain.enums import EffortFilterMode, FusionMode, TrajectoryStatus
from yggdrasil.domain.models import Trajectory
from yggdrasil.ports.embed_view import AspectTexts
from yggdrasil.ports.vector_index import UpsertVectorPoint, VectorSearchHit, VectorSearchQuery
from yggdrasil.services.embed_service import EmbedService
from yggdrasil.services.errors import EmbedFailedError, ValidationError


class _WrongDimEmbedder:
    model_name = "fake"
    dimensions = 4

    def embed_texts(self, texts):
        # Deliberately wrong length vs config embed_dim=4
        return [[0.1, 0.2, 0.3] for _ in texts]

    def embed_one(self, text):
        return self.embed_texts([text])[0]


class _OkEmbedder:
    model_name = "fake"
    dimensions = 4

    def embed_texts(self, texts):
        return [[0.1] * 4 for _ in texts]

    def embed_one(self, text):
        return self.embed_texts([text])[0]


class _FakeView:
    def build_aspect_texts(self, trajectory):
        return AspectTexts(task_text="task", scaffold_text="scaffold")


class _FakeIndex:
    def __init__(self):
        self.points = []

    def ensure_collection(self, *, vector_size: int) -> None:
        pass

    def upsert(self, point: UpsertVectorPoint) -> None:
        self.points.append(point)

    def upsert_many(self, points) -> None:
        for p in points:
            self.upsert(p)

    def delete(self, trajectory_id: str) -> None:
        pass

    def search(self, query: VectorSearchQuery) -> list[VectorSearchHit]:
        return []


def _cfg(*, embed_dim: int = 4) -> YggConfig:
    return YggConfig(
        sqlite_path=":memory:",
        qdrant_url="http://localhost:6333",
        qdrant_collection="test",
        qdrant_api_key=None,
        embed_base_url="http://localhost:8080",
        embed_api_key=None,
        embed_model="m",
        embed_dim=embed_dim,
        default_domain="coding",
        search_include_open=True,
        fusion=FusionMode.RRF,
        w_task=1.0,
        w_scaffold=1.0,
        effort_filter_mode=EffortFilterMode.INCLUSIVE_NULL,
        embed_view_version="coding_v1",
        mongo_uri=None,
        mongo_creds_file=None,
    )


def _traj() -> Trajectory:
    now = datetime.now(timezone.utc)
    return Trajectory(
        id="550e8400-e29b-41d4-a716-446655440000",
        domain="coding",
        status=TrajectoryStatus.OPEN,
        task_text="t",
        scaffold_text="s",
        created_at=now,
        updated_at=now,
    )


def test_embed_service_rejects_wrong_vector_dimension():
    svc = EmbedService(_WrongDimEmbedder(), _FakeIndex(), _FakeView(), _cfg(embed_dim=4))
    with pytest.raises((ValidationError, EmbedFailedError)) as exc:
        svc.index_trajectory(_traj())
    assert "dim" in str(exc.value).lower() or "dimension" in str(exc.value).lower()


def test_embed_service_accepts_matching_dimension():
    index = _FakeIndex()
    svc = EmbedService(_OkEmbedder(), index, _FakeView(), _cfg(embed_dim=4))
    vectors = svc.index_trajectory(_traj())
    assert vectors.task is not None
    assert len(vectors.task) == 4
    assert len(index.points) == 1
