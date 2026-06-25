"""Idempotent Mongo conversation importer into TrajectoryStore."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from yggdrasil.adapters.importers.mongo_mapping import MappedTrajectory, map_mongo_conversation_doc
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

    def import_doc(
        self,
        doc: dict[str, Any],
        *,
        reembed: bool = False,
        dry_run: bool = False,
    ) -> MappedTrajectory:
        mapped = map_mongo_conversation_doc(doc)
        if dry_run:
            return mapped

        external_id = str(mapped.trajectory.external_refs.get("id", ""))
        existing = self._store.find_by_external_ref("mongo", external_id)
        traj = self._store.upsert_imported(mapped.trajectory, mapped.steps)

        if reembed and self._embed is not None:
            try:
                self._embed.index_trajectory(traj, reembed=True)
                traj = self._store.set_index_state(traj.id, IndexState.INDEXED)
            except (EmbedFailedError, IndexFailedError):
                traj = self._store.set_index_state(traj.id, IndexState.STALE)
        elif existing is None:
            try:
                traj = self._store.set_index_state(traj.id, IndexState.PENDING)
            except Exception:
                pass

        mapped.trajectory = traj
        return mapped

    def import_many(
        self,
        docs: Iterable[dict[str, Any]],
        *,
        reembed: bool = False,
        dry_run: bool = False,
        limit: int | None = None,
    ) -> ImportStats:
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
