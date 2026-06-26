"""Session lifecycle orchestration over store + embed."""
from __future__ import annotations

import logging
from typing import Any, Sequence

from yggdrasil.domain.artifacts import ArtifactRef, normalize_artifacts
from yggdrasil.domain.enums import IndexStatus, StepKind, TrajectoryStatus
from yggdrasil.domain.models import EffortLedger, Outcome, Progress, RuntimeFingerprint, Step, Trajectory
from yggdrasil.domain.principal import Principal
from yggdrasil.ports.scrubber import ContentScrubber
from yggdrasil.ports.store import (
    AppendStepInput,
    CreateTrajectoryInput,
    FinalizeTrajectoryInput,
    TrajectoryClosedError as StoreTrajectoryClosedError,
    TrajectoryNotFoundError as StoreTrajectoryNotFoundError,
    TrajectoryStore,
    UpdateTrajectoryMetaInput,
)
from yggdrasil.ports.vector_index import NamedVectors
from yggdrasil.services.embed_service import EmbedService, should_reembed
from yggdrasil.services.errors import (
    EmbedFailedError,
    IndexFailedError,
    NotFoundError,
    StoreFailedError,
    TrajectoryClosedError,
    ValidationError,
)
from yggdrasil.services.principal_context import get_principal

logger = logging.getLogger(__name__)


class SessionService:
    """Coordinates trajectory CRUD with embedding/indexing policy.

    Dual-store write policy (SQLite SoT + vector index):
    1. Persist SQLite first (source of truth).
    2. Attempt vector-index upsert via EmbedService.
    3. On success → index_status=ready.
    4. On failure → index_status=failed (or stale if previously ready);
       start_trajectory hard-fails (raises) after marking failed.

    Optional content scrubbing (``YGG_SCRUB_CONTENT=1``): when a ContentScrubber
    is provided, scrub task/scaffold/summary text before persist; owner names in
    external_refs are allowlisted and preserved exactly.

    Multi-tenant: when a Principal is provided (or bound via contextvar),
    owner and tenant_id are forced server-side (client spoof ignored).
    """

    def __init__(
        self,
        store: TrajectoryStore,
        embed_service: EmbedService,
        *,
        scrubber: ContentScrubber | None = None,
        tenancy_enforced: bool = False,
        default_tenant: str = "lab",
    ) -> None:
        self._store = store
        self._embed = embed_service
        self._scrubber = scrubber
        self._tenancy_enforced = tenancy_enforced
        self._default_tenant = default_tenant
        self._vector_cache: dict[str, NamedVectors] = {}

    def _resolve_principal(self, principal: Principal | None) -> Principal | None:
        if principal is not None:
            return principal
        return get_principal()

    def _bind_refs_for_write(
        self,
        external_refs: dict[str, Any] | None,
        principal: Principal | None,
    ) -> tuple[dict[str, Any], str]:
        """Force owner + tenant from principal when present; return (refs, tenant_id)."""
        refs = dict(external_refs or {})
        if principal is not None:
            client_owner = refs.get("owner")
            if client_owner is not None and client_owner != principal.owner:
                logger.info(
                    "overwriting client owner %r with principal.owner %r (token_id=%s)",
                    client_owner,
                    principal.owner,
                    principal.token_id,
                )
            refs["owner"] = principal.owner
            tenant_id = principal.tenant_id
        else:
            tenant_id = self._default_tenant
        return refs, tenant_id

    def _assert_tenant_access(self, traj: Trajectory, principal: Principal | None) -> None:
        if principal is None:
            return
        if traj.tenant_id != principal.tenant_id:
            raise NotFoundError(f"trajectory not found: {traj.id}")

    def _allowed_names(self, external_refs: dict[str, Any] | None) -> list[str]:
        refs = external_refs or {}
        names: list[str] = []
        for key in ("owner", "agent_id", "team"):
            val = refs.get(key)
            if isinstance(val, str) and val.strip():
                names.append(val.strip())
        return names

    def _scrub(self, text: str | None, *, allowed_names: Sequence[str] = ()) -> str | None:
        if text is None or self._scrubber is None:
            return text
        return self._scrubber.scrub_text(text, allowed_names=allowed_names)

    def _map_store_error(self, exc: Exception) -> Exception:
        if isinstance(exc, StoreTrajectoryNotFoundError):
            return NotFoundError(f"trajectory not found: {exc.trajectory_id}")
        if isinstance(exc, StoreTrajectoryClosedError):
            return TrajectoryClosedError(
                f"trajectory closed: {exc.trajectory_id} status={exc.status}"
            )
        return exc

    def _index_and_mark(
        self,
        traj: Trajectory,
        *,
        reembed: bool,
        hard_fail: bool = False,
    ) -> Trajectory:
        prior = self._vector_cache.get(traj.id)
        prior_status = traj.index_status
        try:
            vectors = self._embed.index_trajectory(traj, reembed=reembed, prior_vectors=prior)
            self._vector_cache[traj.id] = vectors
            return self._store.set_index_status(traj.id, IndexStatus.READY)
        except (EmbedFailedError, IndexFailedError):
            # Prefer stale when we previously had a good index; otherwise failed.
            if prior_status == IndexStatus.READY and not hard_fail:
                state = IndexStatus.STALE
            else:
                state = IndexStatus.FAILED
            try:
                traj = self._store.set_index_status(traj.id, state)
            except Exception:
                pass
            if hard_fail:
                raise
            return traj

    def start_trajectory(
        self,
        *,
        task_text: str,
        scaffold_text: str,
        domain: str = "coding",
        tags: list[str] | None = None,
        runtime_fingerprint: RuntimeFingerprint | dict[str, Any] | None = None,
        external_refs: dict[str, Any] | None = None,
        artifacts: list[ArtifactRef] | list[dict[str, Any]] | None = None,
        progress: Progress | dict[str, Any] | None = None,
        effort: EffortLedger | dict[str, Any] | None = None,
        embed_view_version: str = "coding_v1",
        trajectory_id: str | None = None,
        principal: Principal | None = None,
        tenant_id: str | None = None,
    ) -> Trajectory:
        if not task_text or not task_text.strip():
            raise ValidationError("task_text is required")
        if not scaffold_text or not scaffold_text.strip():
            raise ValidationError("scaffold_text is required")

        principal = self._resolve_principal(principal)
        if self._tenancy_enforced and principal is None:
            from yggdrasil.services.auth_service import AuthError

            raise AuthError("principal required when YGG_TENANCY_MODE=enforced")

        refs, bound_tenant = self._bind_refs_for_write(external_refs, principal)
        if tenant_id is not None and principal is None:
            bound_tenant = tenant_id
        allowed = self._allowed_names(refs)
        task_text = self._scrub(task_text, allowed_names=allowed) or task_text
        scaffold_text = self._scrub(scaffold_text, allowed_names=allowed) or scaffold_text

        fp = runtime_fingerprint
        if isinstance(fp, dict):
            fp = RuntimeFingerprint.model_validate(fp)
        prog = progress
        if isinstance(prog, dict):
            prog = Progress.model_validate(prog)
        eff = effort
        if isinstance(eff, dict):
            eff = EffortLedger.model_validate(eff)

        try:
            traj = self._store.create(
                CreateTrajectoryInput(
                    id=trajectory_id,
                    domain=domain,
                    task_text=task_text,
                    scaffold_text=scaffold_text,
                    runtime_fingerprint=fp,
                    tags=list(tags or []),
                    external_refs=refs,
                    artifacts=normalize_artifacts(artifacts),
                    progress=prog,
                    effort=eff,
                    embed_view_version=embed_view_version,
                    tenant_id=bound_tenant,
                )
            )
        except Exception as exc:
            mapped = self._map_store_error(exc)
            if mapped is not exc:
                raise mapped from exc
            raise StoreFailedError(str(exc)) from exc

        return self._index_and_mark(traj, reembed=True, hard_fail=True)

    def append_step(
        self,
        *,
        trajectory_id: str,
        kind: StepKind | str,
        summary: str,
        payload: dict[str, Any] | None = None,
        scaffold_update: str | None = None,
        task_update: str | None = None,
        is_checkpoint: bool = False,
        progress: Progress | dict[str, Any] | None = None,
        mark_partial: bool = False,
        effort_delta: EffortLedger | dict[str, Any] | None = None,
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        principal = self._resolve_principal(principal)
        if isinstance(kind, str):
            kind = StepKind(kind)
        prog = progress
        if isinstance(prog, dict):
            prog = Progress.model_validate(prog)
        eff = effort_delta
        if isinstance(eff, dict):
            eff = EffortLedger.model_validate(eff)

        # Allowlist owner from existing trajectory when scrubbing is on
        allowed: list[str] = []
        if self._scrubber is not None or principal is not None:
            try:
                existing = self._store.get(trajectory_id)
                self._assert_tenant_access(existing, principal)
                allowed = self._allowed_names(existing.external_refs)
            except StoreTrajectoryNotFoundError as exc:
                raise NotFoundError(f"trajectory not found: {exc.trajectory_id}") from exc
            except NotFoundError:
                raise
            except Exception:
                allowed = []
            if self._scrubber is not None:
                summary = self._scrub(summary, allowed_names=allowed) or summary
                if scaffold_update is not None:
                    scaffold_update = self._scrub(scaffold_update, allowed_names=allowed)
                if task_update is not None:
                    task_update = self._scrub(task_update, allowed_names=allowed)

        try:
            traj, step = self._store.append_step(
                AppendStepInput(
                    trajectory_id=trajectory_id,
                    kind=kind,
                    summary=summary,
                    payload=dict(payload or {}),
                    scaffold_update=scaffold_update,
                    task_update=task_update,
                    is_checkpoint=is_checkpoint,
                    progress=prog,
                    mark_partial=mark_partial,
                    effort_delta=eff,
                )
            )
        except (NotFoundError, ValidationError) as exc:
            raise exc
        except Exception as exc:
            mapped = self._map_store_error(exc)
            if mapped is not exc:
                raise mapped from exc
            raise StoreFailedError(str(exc)) from exc

        reembed = should_reembed(
            task_changed=task_update is not None,
            scaffold_changed=scaffold_update is not None,
            is_checkpoint=is_checkpoint,
        )
        traj = self._index_and_mark(traj, reembed=reembed, hard_fail=False)
        return {"trajectory": traj, "step": step}

    def finalize_trajectory(
        self,
        *,
        trajectory_id: str,
        outcome: Outcome | dict[str, Any],
        effort: EffortLedger | dict[str, Any] | None = None,
        runtime_fingerprint: RuntimeFingerprint | dict[str, Any] | None = None,
        progress: Progress | dict[str, Any] | None = None,
        task_text: str | None = None,
        scaffold_text: str | None = None,
        principal: Principal | None = None,
    ) -> Trajectory:
        principal = self._resolve_principal(principal)
        if principal is not None:
            try:
                existing = self._store.get(trajectory_id)
                self._assert_tenant_access(existing, principal)
            except NotFoundError:
                raise
            except StoreTrajectoryNotFoundError as exc:
                raise NotFoundError(f"trajectory not found: {exc.trajectory_id}") from exc

        if isinstance(outcome, dict):
            outcome = Outcome.model_validate(outcome)
        eff = effort
        if isinstance(eff, dict):
            eff = EffortLedger.model_validate(eff)
        fp = runtime_fingerprint
        if isinstance(fp, dict):
            fp = RuntimeFingerprint.model_validate(fp)
        prog = progress
        if isinstance(prog, dict):
            prog = Progress.model_validate(prog)

        try:
            traj = self._store.finalize(
                FinalizeTrajectoryInput(
                    trajectory_id=trajectory_id,
                    outcome=outcome,
                    effort=eff,
                    runtime_fingerprint=fp,
                    progress=prog,
                    task_text=task_text,
                    scaffold_text=scaffold_text,
                )
            )
        except (NotFoundError, ValidationError) as exc:
            raise exc
        except Exception as exc:
            mapped = self._map_store_error(exc)
            if mapped is not exc:
                raise mapped from exc
            raise StoreFailedError(str(exc)) from exc

        reembed = should_reembed(
            task_changed=task_text is not None,
            scaffold_changed=scaffold_text is not None,
            is_checkpoint=False,
        )
        # always refresh payload on finalize (status/outcome changed)
        return self._index_and_mark(traj, reembed=reembed or True, hard_fail=False)

    def update_trajectory_meta(
        self,
        *,
        trajectory_id: str,
        tags: list[str] | None = None,
        task_text: str | None = None,
        scaffold_text: str | None = None,
        runtime_fingerprint: RuntimeFingerprint | dict[str, Any] | None = None,
        external_refs: dict[str, Any] | None = None,
        artifacts: list[ArtifactRef] | list[dict[str, Any]] | None = None,
        merge_artifacts: bool = True,
        principal: Principal | None = None,
    ) -> Trajectory:
        principal = self._resolve_principal(principal)
        if principal is not None:
            try:
                existing = self._store.get(trajectory_id)
                self._assert_tenant_access(existing, principal)
            except NotFoundError:
                raise
            except StoreTrajectoryNotFoundError as exc:
                raise NotFoundError(f"trajectory not found: {exc.trajectory_id}") from exc

        # Never let client spoof owner when principal is bound
        refs = external_refs
        if refs is not None and principal is not None:
            refs = dict(refs)
            refs["owner"] = principal.owner

        fp = runtime_fingerprint
        if isinstance(fp, dict):
            fp = RuntimeFingerprint.model_validate(fp)
        arts = normalize_artifacts(artifacts) if artifacts is not None else None
        try:
            traj = self._store.update_meta(
                UpdateTrajectoryMetaInput(
                    trajectory_id=trajectory_id,
                    tags=tags,
                    task_text=task_text,
                    scaffold_text=scaffold_text,
                    runtime_fingerprint=fp,
                    external_refs=refs,
                    artifacts=arts,
                    merge_artifacts=merge_artifacts,
                )
            )
        except (NotFoundError, ValidationError) as exc:
            raise exc
        except Exception as exc:
            mapped = self._map_store_error(exc)
            if mapped is not exc:
                raise mapped from exc
            raise StoreFailedError(str(exc)) from exc

        reembed = should_reembed(
            task_changed=task_text is not None,
            scaffold_changed=scaffold_text is not None,
            is_checkpoint=False,
        )
        return self._index_and_mark(traj, reembed=reembed, hard_fail=False)

    def get_trajectory(
        self,
        trajectory_id: str,
        *,
        include_steps: bool = True,
        principal: Principal | None = None,
    ) -> dict[str, Any]:
        principal = self._resolve_principal(principal)
        try:
            traj = self._store.get(trajectory_id)
            self._assert_tenant_access(traj, principal)
            steps: list[Step] = []
            if include_steps:
                steps = self._store.get_steps(trajectory_id)
        except (NotFoundError, ValidationError) as exc:
            raise exc
        except Exception as exc:
            mapped = self._map_store_error(exc)
            if mapped is not exc:
                raise mapped from exc
            raise StoreFailedError(str(exc)) from exc
        return {"trajectory": traj, "steps": steps}
