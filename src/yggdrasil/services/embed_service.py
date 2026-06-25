"""Embed + index orchestration service."""
from __future__ import annotations

from yggdrasil.config import YggConfig
from yggdrasil.domain.models import Trajectory
from yggdrasil.ports.embed_view import EmbedView
from yggdrasil.ports.embedder import Embedder
from yggdrasil.ports.vector_index import (
    NamedVectors,
    UpsertVectorPoint,
    VectorIndex,
    payload_from_trajectory,
)
from yggdrasil.services.errors import EmbedFailedError, IndexFailedError


def should_reembed(*, task_changed: bool, scaffold_changed: bool, is_checkpoint: bool) -> bool:
    return bool(task_changed or scaffold_changed or is_checkpoint)


class EmbedService:
    """Builds aspect texts, embeds, and upserts into the vector index."""

    def __init__(
        self,
        embedder: Embedder,
        index: VectorIndex,
        view: EmbedView,
        config: YggConfig,
    ) -> None:
        self._embedder = embedder
        self._index = index
        self._view = view
        self._config = config

    def index_trajectory(
        self,
        trajectory: Trajectory,
        *,
        reembed: bool = True,
        prior_vectors: NamedVectors | None = None,
    ) -> NamedVectors:
        aspects = self._view.build_aspect_texts(trajectory)
        vectors = prior_vectors or NamedVectors()

        need_task = reembed or vectors.task is None
        need_scaffold = reembed or vectors.scaffold is None

        texts: list[str] = []
        keys: list[str] = []
        if need_task and aspects.task_text.strip():
            texts.append(aspects.task_text)
            keys.append("task")
        if need_scaffold and aspects.scaffold_text.strip():
            texts.append(aspects.scaffold_text)
            keys.append("scaffold")

        if texts:
            try:
                embedded = self._embedder.embed_texts(texts)
            except EmbedFailedError:
                raise
            except Exception as exc:
                raise EmbedFailedError(str(exc)) from exc
            updates: dict[str, list[float]] = {}
            for key, vec in zip(keys, embedded):
                updates[key] = vec
            vectors = NamedVectors(
                task=updates.get("task", vectors.task),
                scaffold=updates.get("scaffold", vectors.scaffold),
            )

        aspects_present: list[str] = []
        if vectors.task is not None:
            aspects_present.append("task")
        if vectors.scaffold is not None:
            aspects_present.append("scaffold")

        payload = payload_from_trajectory(
            trajectory,
            embed_model=self._embedder.model_name,
            embed_dim=self._embedder.dimensions,
            aspects_present=aspects_present,
        )
        point = UpsertVectorPoint(
            trajectory_id=trajectory.id,
            vectors=vectors,
            payload=payload,
        )
        try:
            self._index.upsert(point)
        except Exception as exc:
            raise IndexFailedError(str(exc)) from exc
        return vectors
