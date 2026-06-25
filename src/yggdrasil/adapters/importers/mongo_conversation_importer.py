"""Idempotent Mongo conversation importer into TrajectoryStore (legacy + hierarchical)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from yggdrasil.adapters.importers.mongo_mapping import (
    MappedSessionHierarchy,
    MappedTrajectory,
    map_mongo_conversation_doc,
    map_session_hierarchy,
)
from yggdrasil.adapters.importers.mongo_normalize import (
    SessionAggregate,
    normalize_and_aggregate_docs,
    normalize_mongo_doc,
)
from yggdrasil.adapters.importers.mongo_segment import segment_conversation_ir
from yggdrasil.adapters.importers.segment_schema import TrajectorySegment
from yggdrasil.domain.enums import IndexState
from yggdrasil.ports.store import TrajectoryStore
from yggdrasil.services.embed_service import EmbedService
from yggdrasil.services.errors import EmbedFailedError, IndexFailedError


@dataclass
class ImportStats:
    seen: int = 0
    imported: int = 0
    updated: int = 0
    skipped: int = 0
    sessions: int = 0
    parents: int = 0
    children: int = 0
    embedded: int = 0
    errors: list[str] = field(default_factory=list)


class MongoConversationImporter:
    """Import mapped mongo conversations into the store, optionally re-embedding."""

    def __init__(
        self,
        store: TrajectoryStore,
        embed_service: EmbedService | None = None,
    ) -> None:
        self._store = store
        self._embed = embed_service

    def _persist_mapped(
        self,
        mapped: MappedTrajectory,
        *,
        reembed: bool,
        dry_run: bool,
        force_embed: bool | None = None,
    ) -> MappedTrajectory:
        if dry_run:
            return mapped
        external_id = str(mapped.trajectory.external_refs.get("id", ""))
        existing = self._store.find_by_external_ref("mongo", external_id) if external_id else None
        traj = self._store.upsert_imported(mapped.trajectory, mapped.steps)
        embed_target = mapped.trajectory.external_refs.get("embed_target", True)
        if force_embed is not None:
            embed_target = force_embed
        if reembed and embed_target and self._embed is not None:
            try:
                self._embed.index_trajectory(traj, reembed=True)
                traj = self._store.set_index_state(traj.id, IndexState.INDEXED)
            except (EmbedFailedError, IndexFailedError):
                traj = self._store.set_index_state(traj.id, IndexState.STALE)
        elif existing is None:
            try:
                state = IndexState.PENDING if embed_target else IndexState.PENDING
                traj = self._store.set_index_state(traj.id, state)
            except Exception:
                pass
        mapped.trajectory = traj
        return mapped

    def import_doc(
        self,
        doc: dict[str, Any],
        *,
        reembed: bool = False,
        dry_run: bool = False,
    ) -> MappedTrajectory:
        """Legacy: one doc → one trajectory (no hierarchy)."""
        mapped = map_mongo_conversation_doc(doc)
        return self._persist_mapped(mapped, reembed=reembed, dry_run=dry_run)

    def import_session_hierarchy(
        self,
        hierarchy: MappedSessionHierarchy,
        *,
        reembed: bool = False,
        dry_run: bool = False,
        embed_parent: bool = False,
        embed_children: bool = True,
    ) -> MappedSessionHierarchy:
        """Persist parent + children; embed children (and optionally parent)."""
        parent = self._persist_mapped(
            hierarchy.parent,
            reembed=reembed and embed_parent,
            dry_run=dry_run,
            force_embed=embed_parent,
        )
        children: list[MappedTrajectory] = []
        for child in hierarchy.children:
            children.append(
                self._persist_mapped(
                    child,
                    reembed=reembed and embed_children,
                    dry_run=dry_run,
                    force_embed=embed_children,
                )
            )
        hierarchy.parent = parent
        hierarchy.children = children
        return hierarchy

    def import_session_aggregate(
        self,
        agg: SessionAggregate,
        *,
        reembed: bool = False,
        dry_run: bool = False,
        embed_parent: bool = False,
        embed_children: bool = True,
        caller_segments: list[dict[str, Any]] | list[TrajectorySegment] | None = None,
    ) -> MappedSessionHierarchy:
        """Import one session from Phase-1 aggregate (canonical IR + optional caller segments)."""
        segmented = segment_conversation_ir(agg.canonical, caller_segments=caller_segments)
        hierarchy = map_session_hierarchy(
            agg.canonical, segmented=segmented, embed_parent=embed_parent
        )
        # stamp parent embed flag on external_refs for persistence policy
        hierarchy.parent.trajectory = hierarchy.parent.trajectory.model_copy(
            update={
                "external_refs": {
                    **hierarchy.parent.trajectory.external_refs,
                    "embed_target": embed_parent,
                }
            }
        )
        return self.import_session_hierarchy(
            hierarchy,
            reembed=reembed,
            dry_run=dry_run,
            embed_parent=embed_parent,
            embed_children=embed_children,
        )

    def import_docs_as_sessions(
        self,
        docs: Iterable[dict[str, Any]],
        *,
        reembed: bool = False,
        dry_run: bool = False,
        limit_sessions: int | None = None,
        embed_parent: bool = False,
        embed_children: bool = True,
        caller_segments_by_session: dict[str, list[dict[str, Any]]] | None = None,
    ) -> ImportStats:
        """Normalize → session-aggregate → segment → parent/child persist (+ optional embed)."""
        stats = ImportStats()
        aggs = normalize_and_aggregate_docs(docs)
        for agg in aggs:
            if limit_sessions is not None and stats.sessions >= limit_sessions:
                break
            stats.sessions += 1
            stats.seen += agg.request_count
            try:
                segs = None
                if caller_segments_by_session:
                    segs = caller_segments_by_session.get(agg.session_id)
                h = self.import_session_aggregate(
                    agg,
                    reembed=reembed,
                    dry_run=dry_run,
                    embed_parent=embed_parent,
                    embed_children=embed_children,
                    caller_segments=segs,
                )
                stats.parents += 1
                stats.children += len(h.children)
                if reembed:
                    if embed_parent:
                        stats.embedded += 1
                    if embed_children:
                        stats.embedded += len(h.children)
                stats.imported += 1 + len(h.children)
            except Exception as exc:
                stats.errors.append(f"session {agg.session_id}: {exc}")
                stats.skipped += 1
        return stats

    def import_many(
        self,
        docs: Iterable[dict[str, Any]],
        *,
        reembed: bool = False,
        dry_run: bool = False,
        limit: int | None = None,
        hierarchical: bool = False,
        limit_sessions: int | None = None,
        embed_parent: bool = False,
    ) -> ImportStats:
        if hierarchical:
            limited_docs: list[dict[str, Any]] = []
            for i, doc in enumerate(docs):
                if limit is not None and i >= limit:
                    break
                limited_docs.append(doc)
            return self.import_docs_as_sessions(
                limited_docs,
                reembed=reembed,
                dry_run=dry_run,
                limit_sessions=limit_sessions,
                embed_parent=embed_parent,
            )

        stats = ImportStats()
        for doc in docs:
            if limit is not None and stats.seen >= limit:
                break
            stats.seen += 1
            try:
                external_id = None
                doc_id = doc.get("_id")
                if isinstance(doc_id, dict) and "$oid" in doc_id:
                    external_id = str(doc_id["$oid"])
                elif doc_id is not None:
                    external_id = str(doc_id)
                existed = False
                if external_id:
                    existed = self._store.find_by_external_ref("mongo", external_id) is not None
                self.import_doc(doc, reembed=reembed, dry_run=dry_run)
                if existed:
                    stats.updated += 1
                else:
                    stats.imported += 1
            except Exception as exc:
                stats.errors.append(str(exc))
                stats.skipped += 1
        return stats
