"""Search strategies via embed + vector index + store hydration."""
from __future__ import annotations

from typing import Any

from yggdrasil.config import YggConfig
from yggdrasil.domain.enums import TrajectoryStatus
from yggdrasil.domain.models import EffortPredicate, SearchHit, SearchScores, Trajectory
from yggdrasil.ports.embed_view import EmbedView
from yggdrasil.ports.embedder import Embedder
from yggdrasil.ports.store import TrajectoryStore
from yggdrasil.ports.vector_index import VectorIndex, VectorSearchHit, VectorSearchQuery
from yggdrasil.services.errors import EmbedFailedError, InvalidQueryError, NotFoundError


class SearchService:
    """Embeds query aspects, searches Qdrant, hydrates trajectories from SQLite."""

    def __init__(
        self,
        store: TrajectoryStore,
        embedder: Embedder,
        index: VectorIndex,
        view: EmbedView,
        config: YggConfig,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._index = index
        self._view = view
        self._config = config

    def _coerce_status_in(
        self, status_in: list[str] | list[TrajectoryStatus] | None
    ) -> list[TrajectoryStatus] | None:
        if status_in is None:
            return None
        out: list[TrajectoryStatus] = []
        for s in status_in:
            if isinstance(s, TrajectoryStatus):
                out.append(s)
            else:
                out.append(TrajectoryStatus(s))
        return out

    def _coerce_effort_predicates(
        self, effort_predicates: list[EffortPredicate] | list[dict[str, Any]] | None
    ) -> list[EffortPredicate]:
        if not effort_predicates:
            return []
        out: list[EffortPredicate] = []
        for p in effort_predicates:
            if isinstance(p, EffortPredicate):
                out.append(p)
            else:
                out.append(EffortPredicate.model_validate(p))
        return out

    def _to_search_hit(
        self,
        traj: Trajectory,
        *,
        score: float | None,
        vector_hit: VectorSearchHit | None = None,
    ) -> SearchHit:
        return SearchHit(
            trajectory_id=traj.id,
            domain=traj.domain,
            status=traj.status,
            task_text=traj.task_text,
            scaffold_text=traj.scaffold_text,
            progress=traj.progress,
            outcome=traj.outcome,
            effort_totals=traj.effort.totals,
            runtime_fingerprint=traj.runtime_fingerprint,
            tags=list(traj.tags),
            external_refs=dict(traj.external_refs),
            scores=SearchScores(fused=score, fusion="rrf") if score is not None else None,
            score=score,
            index_state=traj.index_state,
            embed_view_version=traj.embed_view_version,
        )

    def search_strategies(
        self,
        *,
        task: str | None = None,
        scaffold: str | None = None,
        domain: str | None = "coding",
        status_in: list[str] | list[TrajectoryStatus] | None = None,
        include_open: bool | None = None,
        tags_any: list[str] | None = None,
        limit: int = 10,
        effort_predicates: list[EffortPredicate] | list[dict[str, Any]] | None = None,
        runtime_filters: dict[str, Any] | None = None,
        prefer_low_waste: bool = False,
        include_attempt_history_in_embed: bool = False,
    ) -> list[SearchHit]:
        if include_attempt_history_in_embed:
            # PoC: ignored but validate embed view does not support it if explicitly wired elsewhere
            pass

        task_q = task.strip() if task and task.strip() else None
        scaffold_q = scaffold.strip() if scaffold and scaffold.strip() else None
        if not task_q and not scaffold_q:
            raise InvalidQueryError("at least one of task or scaffold is required")

        if include_open is None:
            include_open = self._config.search_include_open

        aspects = self._view.build_query_aspect_texts(task=task_q, scaffold=scaffold_q)
        task_vector = None
        scaffold_vector = None
        try:
            if aspects.task_text.strip():
                task_vector = self._embedder.embed_one(aspects.task_text)
            if aspects.scaffold_text.strip():
                scaffold_vector = self._embedder.embed_one(aspects.scaffold_text)
        except EmbedFailedError:
            raise
        except Exception as exc:
            raise EmbedFailedError(str(exc)) from exc

        if task_vector is None and scaffold_vector is None:
            raise InvalidQueryError("could not build embeddable query aspects")

        vquery = VectorSearchQuery(
            task_vector=task_vector,
            scaffold_vector=scaffold_vector,
            domain=domain,
            status_in=self._coerce_status_in(status_in),
            include_open=include_open,
            tags_any=list(tags_any) if tags_any else None,
            effort_predicates=self._coerce_effort_predicates(effort_predicates),
            runtime_filters=dict(runtime_filters or {}),
            limit=limit,
        )
        vector_hits = self._index.search(vquery)
        if not vector_hits:
            return []

        ids = [h.trajectory_id for h in vector_hits]
        trajs = self._store.list_by_ids(ids)
        by_id = {t.id: t for t in trajs}
        score_by_id = {h.trajectory_id: h.score for h in vector_hits}
        hit_by_id = {h.trajectory_id: h for h in vector_hits}

        results: list[SearchHit] = []
        for tid in ids:
            traj = by_id.get(tid)
            if traj is None:
                continue
            results.append(
                self._to_search_hit(
                    traj,
                    score=score_by_id.get(tid),
                    vector_hit=hit_by_id.get(tid),
                )
            )

        if prefer_low_waste:
            results.sort(
                key=lambda h: (
                    h.effort_totals.failure_waste_seconds is None,
                    h.effort_totals.failure_waste_seconds
                    if h.effort_totals.failure_waste_seconds is not None
                    else float("inf"),
                    -(h.score or 0.0),
                )
            )

        return results[:limit]
