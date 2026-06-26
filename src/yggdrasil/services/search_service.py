"""Search strategies via embed + vector index + store hydration."""
from __future__ import annotations

from typing import Any

from yggdrasil.config import YggConfig
from yggdrasil.domain.artifacts import team_identity_from_refs
from yggdrasil.domain.enums import IndexStatus, TrajectoryStatus
from yggdrasil.domain.models import EffortPredicate, SearchHit, SearchScores, Trajectory
from yggdrasil.domain.principal import Principal
from yggdrasil.ports.embed_view import EmbedView
from yggdrasil.ports.embedder import Embedder
from yggdrasil.ports.store import TrajectoryStore
from yggdrasil.ports.vector_index import VectorIndex, VectorSearchHit, VectorSearchQuery
from yggdrasil.services.errors import EmbedFailedError, InvalidQueryError, NotFoundError
from yggdrasil.services.principal_context import get_principal
from yggdrasil.services.retrieval_gates import GateConfig, GatedSearchResult, apply_retrieval_gates

# Default search excludes trajectories that never reached a usable index.
_DEFAULT_EXCLUDED_INDEX_STATUSES: frozenset[IndexStatus] = frozenset(
    {IndexStatus.PENDING, IndexStatus.FAILED}
)


class SearchService:
    """Embeds query aspects, searches Qdrant, hydrates trajectories from SQLite."""

    def __init__(
        self,
        store: TrajectoryStore,
        embedder: Embedder,
        index: VectorIndex,
        view: EmbedView,
        config: YggConfig,
        *,
        gate_config: GateConfig | None = None,
        apply_gates_default: bool = True,
        tenancy_enforced: bool = False,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._index = index
        self._view = view
        self._config = config
        self._gate_config = gate_config or GateConfig()
        self._apply_gates_default = apply_gates_default
        self._tenancy_enforced = tenancy_enforced
        self._last_gate_result: GatedSearchResult | None = None

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
        ident = team_identity_from_refs(traj.external_refs)
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
            artifacts=list(traj.artifacts or []),
            owner=ident["owner"],
            agent_id=ident["agent_id"],
            team=ident["team"],
            workspace=ident["workspace"],
            tenant_id=getattr(traj, "tenant_id", None) or "lab",
            scores=SearchScores(fused=score, fusion="rrf") if score is not None else None,
            score=score,
            index_status=traj.index_status,
            embed_view_version=traj.embed_view_version,
        )

    @property
    def last_gate_result(self) -> GatedSearchResult | None:
        """Diagnostics from the most recent gated search (dropped reasons, warnings)."""
        return self._last_gate_result

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
        apply_gates: bool | None = None,
        include_archive: bool = False,
        fetch_limit_multiplier: int = 4,
        # Lab / team forensics (Alice finding Bob's work without knowing who)
        owner: str | None = None,
        agent_id: str | None = None,
        team: str | None = None,
        workspace: str | None = None,
        require_artifacts: bool | None = None,
        experience_grade_only: bool | None = None,
        # agent = strict gates; lab = include experience_grade team memory, weaker lexical gate
        search_mode: str = "agent",
        # When False (default), drop pending/failed index_status trajectories from results.
        include_unindexed: bool = False,
        principal: Principal | None = None,
        tenant_id: str | None = None,
    ) -> list[SearchHit]:
        if include_attempt_history_in_embed:
            # PoC: ignored but validate embed view does not support it if explicitly wired elsewhere
            pass

        principal = principal if principal is not None else get_principal()
        if self._tenancy_enforced and principal is None:
            from yggdrasil.services.auth_service import AuthError

            raise AuthError("principal required when YGG_TENANCY_MODE=enforced")
        # Lab shares org scope: filter by tenant only, never force owner from principal.
        effective_tenant = tenant_id
        if principal is not None:
            effective_tenant = principal.tenant_id
        elif self._tenancy_enforced:
            effective_tenant = getattr(self._config, "default_tenant", "lab")

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

        mode = (search_mode or "agent").strip().lower()
        if mode == "lab":
            # Management / report agent: prefer authored experience, include team scope
            if experience_grade_only is None:
                experience_grade_only = True
            if apply_gates is None:
                apply_gates = True
            include_archive = include_archive or False

        use_gates = self._apply_gates_default if apply_gates is None else apply_gates
        # Over-fetch so gates can drop noisy/archive hits and still fill limit
        fetch_limit = limit * max(1, fetch_limit_multiplier) if use_gates else limit

        vquery = VectorSearchQuery(
            task_vector=task_vector,
            scaffold_vector=scaffold_vector,
            domain=domain,
            status_in=self._coerce_status_in(status_in),
            include_open=include_open,
            tags_any=list(tags_any) if tags_any else None,
            owner=owner,
            agent_id=agent_id,
            team=team,
            workspace=workspace,
            require_artifacts=require_artifacts,
            experience_grade_only=experience_grade_only,
            tenant_id=effective_tenant,
            effort_predicates=self._coerce_effort_predicates(effort_predicates),
            runtime_filters=dict(runtime_filters or {}),
            limit=fetch_limit,
        )
        vector_hits = self._index.search(vquery)
        if not vector_hits:
            self._last_gate_result = GatedSearchResult(hits=[], dropped=[], warnings=[])
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
            if effective_tenant is not None and getattr(traj, "tenant_id", "lab") != effective_tenant:
                continue
            # Dual-store gate: exclude pending/failed unless caller opts in
            if not include_unindexed and traj.index_status in _DEFAULT_EXCLUDED_INDEX_STATUSES:
                continue
            results.append(
                self._to_search_hit(
                    traj,
                    score=score_by_id.get(tid),
                    vector_hit=hit_by_id.get(tid),
                )
            )

        if use_gates:
            if mode == "lab":
                # Lab forensics: don't exclude experience_grade; relax lexical gate slightly
                gcfg = GateConfig(
                    exclude_tags_enabled=not include_archive,
                    exclude_tags=self._gate_config.exclude_tags,
                    reject_noisy_task=True,
                    min_token_overlap=0.0,
                    require_overlap_if_no_shared_tokens=False,
                    respect_explicit_tags_any=self._gate_config.respect_explicit_tags_any,
                    pass_through_if_all_filtered=False,
                )
            else:
                gcfg = GateConfig(
                    exclude_tags_enabled=not include_archive,
                    exclude_tags=self._gate_config.exclude_tags,
                    reject_noisy_task=self._gate_config.reject_noisy_task,
                    min_token_overlap=self._gate_config.min_token_overlap,
                    require_overlap_if_no_shared_tokens=self._gate_config.require_overlap_if_no_shared_tokens,
                    respect_explicit_tags_any=self._gate_config.respect_explicit_tags_any,
                    pass_through_if_all_filtered=False,
                )
            gated = apply_retrieval_gates(
                results,
                query_task=task_q,
                query_scaffold=scaffold_q,
                tags_any=tags_any,
                config=gcfg,
            )
            self._last_gate_result = gated
            results = gated.hits
        else:
            self._last_gate_result = None

        # Management / anti-duplication: surface low-waste successful paths first
        if prefer_low_waste:
            results = sorted(
                results,
                key=lambda h: (
                    h.effort_totals.failure_waste_seconds
                    if h.effort_totals.failure_waste_seconds is not None
                    else float("inf"),
                    h.effort_totals.wall_clock_seconds
                    if h.effort_totals.wall_clock_seconds is not None
                    else float("inf"),
                    -(h.score or 0.0),
                ),
            )

        return results[:limit]
