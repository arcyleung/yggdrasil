"""Ports / protocols for adapters (no implementations)."""
from yggdrasil.ports.embed_view import AspectTexts, EmbedView
from yggdrasil.ports.embedder import Embedder
from yggdrasil.ports.store import (
    AppendStepInput, CreateTrajectoryInput, FinalizeTrajectoryInput,
    TrajectoryClosedError, TrajectoryNotFoundError, TrajectoryStore, UpdateTrajectoryMetaInput,
)
from yggdrasil.ports.vector_index import (
    NamedVectors, UpsertVectorPoint, VectorIndex, VectorPointPayload,
    VectorSearchHit, VectorSearchQuery, payload_from_trajectory,
)
__all__ = [
    "AppendStepInput", "AspectTexts", "CreateTrajectoryInput", "EmbedView", "Embedder",
    "FinalizeTrajectoryInput", "NamedVectors", "TrajectoryClosedError", "TrajectoryNotFoundError",
    "TrajectoryStore", "UpdateTrajectoryMetaInput", "UpsertVectorPoint", "VectorIndex",
    "VectorPointPayload", "VectorSearchHit", "VectorSearchQuery", "payload_from_trajectory",
]
