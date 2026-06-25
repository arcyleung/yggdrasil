"""Qdrant VectorIndex adapter with named vectors and RRF fusion."""
from __future__ import annotations

from typing import Any, Sequence
from uuid import UUID

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from yggdrasil.domain.enums import EffortFilterMode, FusionMode
from yggdrasil.ports.vector_index import (
    UpsertVectorPoint,
    VectorIndex,
    VectorPointPayload,
    VectorSearchHit,
    VectorSearchQuery,
)
from yggdrasil.adapters.qdrant_filters import compile_search_filter
from yggdrasil.services.rrf import reciprocal_rank_fuse


def _point_id(trajectory_id: str) -> str | int:
    """Use UUID string directly when valid; otherwise hash to deterministic int."""
    try:
        return str(UUID(trajectory_id))
    except ValueError:
        return abs(hash(trajectory_id)) % (2**63)


def _payload_to_dict(payload: VectorPointPayload) -> dict[str, Any]:
    return payload.model_dump(mode="json")


def _payload_from_dict(data: dict[str, Any] | None) -> VectorPointPayload:
    return VectorPointPayload.model_validate(data or {"trajectory_id": "", "domain": "coding", "status": "open"})


class QdrantIndex:
    """Qdrant-backed vector index with task/scaffold named vectors."""

    def __init__(
        self,
        *,
        url: str,
        collection: str,
        api_key: str | None = None,
        client: QdrantClient | None = None,
        fusion: FusionMode = FusionMode.RRF,
        effort_filter_mode: EffortFilterMode = EffortFilterMode.INCLUSIVE_NULL,
        rrf_k: int = 60,
    ) -> None:
        self._collection = collection
        self._fusion = fusion
        self._effort_filter_mode = effort_filter_mode
        self._rrf_k = rrf_k
        self._owns_client = client is None
        self._client = client or QdrantClient(
            url=url, api_key=api_key, check_compatibility=False
        )

    def close(self) -> None:
        if self._owns_client and hasattr(self._client, "close"):
            self._client.close()

    def ensure_collection(self, *, vector_size: int) -> None:
        names = []
        try:
            existing = self._client.get_collections()
            names = [c.name for c in existing.collections]
        except Exception:
            pass
        if self._collection in names:
            return
        self._client.create_collection(
            collection_name=self._collection,
            vectors_config={
                "task": qm.VectorParams(size=vector_size, distance=qm.Distance.COSINE),
                "scaffold": qm.VectorParams(size=vector_size, distance=qm.Distance.COSINE),
            },
        )

    def _vectors_dict(self, point: UpsertVectorPoint) -> dict[str, list[float]]:
        vectors: dict[str, list[float]] = {}
        if point.vectors.task is not None:
            vectors["task"] = point.vectors.task
        if point.vectors.scaffold is not None:
            vectors["scaffold"] = point.vectors.scaffold
        return vectors

    def upsert(self, point: UpsertVectorPoint) -> None:
        vectors = self._vectors_dict(point)
        if not vectors:
            return
        self._client.upsert(
            collection_name=self._collection,
            points=[
                qm.PointStruct(
                    id=_point_id(point.trajectory_id),
                    vector=vectors,
                    payload=_payload_to_dict(point.payload),
                )
            ],
        )

    def upsert_many(self, points: Sequence[UpsertVectorPoint]) -> None:
        structs: list[qm.PointStruct] = []
        for point in points:
            vectors = self._vectors_dict(point)
            if not vectors:
                continue
            structs.append(
                qm.PointStruct(
                    id=_point_id(point.trajectory_id),
                    vector=vectors,
                    payload=_payload_to_dict(point.payload),
                )
            )
        if structs:
            self._client.upsert(collection_name=self._collection, points=structs)

    def delete(self, trajectory_id: str) -> None:
        self._client.delete(
            collection_name=self._collection,
            points_selector=qm.PointIdsList(points=[_point_id(trajectory_id)]),
        )

    def _search_aspect(
        self,
        *,
        vector_name: str,
        vector: list[float],
        query_filter: qm.Filter | None,
        limit: int,
    ) -> list[Any]:
        # qdrant-client >=1.14 uses query_points; older used search()
        if hasattr(self._client, "query_points"):
            result = self._client.query_points(
                collection_name=self._collection,
                query=vector,
                using=vector_name,
                query_filter=query_filter,
                limit=limit,
                with_payload=True,
            )
            return list(getattr(result, "points", None) or [])
        return self._client.search(
            collection_name=self._collection,
            query_vector=(vector_name, vector),
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )

    def _hit_from_scored(self, scored: Any, score: float | None = None) -> VectorSearchHit:
        payload_raw = scored.payload if isinstance(scored.payload, dict) else {}
        payload = _payload_from_dict(payload_raw)
        tid = payload.trajectory_id or payload_raw.get("trajectory_id", "")
        if not tid and hasattr(scored, "id"):
            tid = str(scored.id)
        return VectorSearchHit(
            trajectory_id=tid,
            score=float(score if score is not None else scored.score),
            payload=payload,
        )

    def search(self, query: VectorSearchQuery) -> list[VectorSearchHit]:
        qfilter = compile_search_filter(query, effort_filter_mode=self._effort_filter_mode)
        limit = query.limit
        overfetch = query.overfetch or max(limit * 3, limit)

        has_task = query.task_vector is not None
        has_scaffold = query.scaffold_vector is not None

        if not has_task and not has_scaffold:
            return []

        # Single aspect or non-RRF: search one vector only
        if self._fusion != FusionMode.RRF or not (has_task and has_scaffold):
            if has_task:
                results = self._search_aspect(
                    vector_name="task",
                    vector=query.task_vector,  # type: ignore[arg-type]
                    query_filter=qfilter,
                    limit=limit,
                )
            else:
                results = self._search_aspect(
                    vector_name="scaffold",
                    vector=query.scaffold_vector,  # type: ignore[arg-type]
                    query_filter=qfilter,
                    limit=limit,
                )
            return [self._hit_from_scored(r) for r in results[:limit]]

        # RRF fusion across both aspects
        task_results = self._search_aspect(
            vector_name="task",
            vector=query.task_vector,  # type: ignore[arg-type]
            query_filter=qfilter,
            limit=overfetch,
        )
        scaffold_results = self._search_aspect(
            vector_name="scaffold",
            vector=query.scaffold_vector,  # type: ignore[arg-type]
            query_filter=qfilter,
            limit=overfetch,
        )

        def _tid(scored: Any) -> str:
            if isinstance(scored.payload, dict) and scored.payload.get("trajectory_id"):
                return str(scored.payload["trajectory_id"])
            return str(scored.id)

        task_ids = [_tid(r) for r in task_results]
        scaffold_ids = [_tid(r) for r in scaffold_results]
        fused = reciprocal_rank_fuse({"task": task_ids, "scaffold": scaffold_ids}, k=self._rrf_k)

        by_id: dict[str, Any] = {}
        for r in task_results + scaffold_results:
            tid = _tid(r)
            if tid not in by_id:
                by_id[tid] = r

        hits: list[VectorSearchHit] = []
        for tid, score in fused[:limit]:
            scored = by_id.get(tid)
            if scored is None:
                continue
            hits.append(self._hit_from_scored(scored, score=score))
        return hits


_: type[VectorIndex] = QdrantIndex  # type: ignore[misc,assignment]
