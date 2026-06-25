"""Embed view registry."""
from __future__ import annotations

from yggdrasil.adapters.embed_views.coding_v1 import CodingEmbedViewV1
from yggdrasil.ports.embed_view import EmbedView


def get_embed_view(version: str) -> EmbedView:
    if version == CodingEmbedViewV1.VERSION or version == "coding_v1":
        return CodingEmbedViewV1()
    raise ValueError(f"unknown embed view version: {version!r}")


__all__ = ["CodingEmbedViewV1", "get_embed_view"]
