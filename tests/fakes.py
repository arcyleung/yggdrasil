"""In-memory fakes for offline integration tests (no Qdrant / embed HTTP)."""
from __future__ import annotations

from typing import Sequence

from yggdrasil.ports.vector_index import (
    UpsertVectorPoint,
    VectorIndex,
    VectorPointPayload,
    VectorSearchHit,
    VectorSearchQuery,
)


class FakeEmbedder:
    """Returns a fixed-dimension vector for any text (offline tests)."""

    def __init__(self, dimensions: int = 8, *, model_name: str = "fake-embed") -> None:
        self._dimensions = dimensions
        self._model_name = model_name

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        # Deterministic per-text salt so different queries aren't identical zeros
        out: list[list[float]] = []
        for text in texts:
            seed = (sum(ord(c) for c in text) % 97) / 97.0
            out.append([0.1 + seed * 0.01] * self._dimensions)
        return out

    def embed_one(self, text: str) -> list[float]:
        return self.embed_texts([text])[0]


class FakeVectorIndex:
    """In-memory VectorIndex: stores payloads; search filters on task text substring."""

    def __init__(self) -> None:
        self.points: dict[str, UpsertVectorPoint] = {}
        self.fail_upsert: bool = False
        self.upsert_calls: int = 0

    def ensure_collection(self, *, vector_size: int) -> None:
        return None

    def upsert(self, point: UpsertVectorPoint) -> None:
        self.upsert_calls += 1
        if self.fail_upsert:
            raise RuntimeError("fake vector index upsert failed")
        self.points[point.trajectory_id] = point

    def upsert_many(self, points: Sequence[UpsertVectorPoint]) -> None:
        for p in points:
            self.upsert(p)

    def delete(self, trajectory_id: str) -> None:
        self.points.pop(trajectory_id, None)

    def search(self, query: VectorSearchQuery) -> list[VectorSearchHit]:
        hits: list[VectorSearchHit] = []
        for tid, point in self.points.items():
            payload = point.payload
            if query.domain is not None and payload.domain != query.domain:
                continue
            if not query.include_open and payload.status.value == "open":
                continue
            if query.status_in is not None and payload.status not in query.status_in:
                continue
            if query.tags_any:
                if not any(t in payload.tags for t in query.tags_any):
                    continue
            if query.owner is not None and payload.owner != query.owner:
                continue
            if query.agent_id is not None and payload.agent_id != query.agent_id:
                continue
            if query.team is not None and payload.team != query.team:
                continue
            if query.workspace is not None and payload.workspace != query.workspace:
                continue
            if query.require_artifacts is True and not payload.has_artifacts:
                continue
            if query.experience_grade_only is True and not payload.experience_grade:
                continue
            if query.tenant_id is not None and getattr(payload, "tenant_id", "lab") != query.tenant_id:
                continue
            # Trivial relevance: always score 1.0 (payload-filter oriented fake)
            hits.append(VectorSearchHit(trajectory_id=tid, score=1.0, payload=payload))
        hits.sort(key=lambda h: h.trajectory_id)
        return hits[: query.limit]


# Protocol satisfaction aids
_: type[VectorIndex] = FakeVectorIndex  # type: ignore[misc,assignment]
