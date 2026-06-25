"""Application wiring for MCP server."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from yggdrasil.adapters.embed_views import get_embed_view
from yggdrasil.adapters.openai_compat_embedder import OpenAICompatEmbedder
from yggdrasil.adapters.qdrant_index import QdrantIndex
from yggdrasil.adapters.sqlite_store import SqliteTrajectoryStore
from yggdrasil.config import YggConfig, load_config, redact_config_for_log
from yggdrasil.services.embed_service import EmbedService
from yggdrasil.services.search_service import SearchService
from yggdrasil.services.session_service import SessionService

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    config: YggConfig
    store: SqliteTrajectoryStore
    embedder: OpenAICompatEmbedder
    index: QdrantIndex
    embed_service: EmbedService
    session_service: SessionService
    search_service: SearchService

    @classmethod
    def from_config(cls, config: YggConfig | None = None) -> AppContext:
        cfg = config or load_config()
        logger.info("yggdrasil config: %s", redact_config_for_log(cfg))

        store = SqliteTrajectoryStore(cfg.sqlite_path)
        embedder = OpenAICompatEmbedder(
            base_url=cfg.embed_base_url,
            model=cfg.embed_model,
            dimensions=cfg.embed_dim,
            api_key=cfg.embed_api_key,
        )
        index = QdrantIndex(
            url=cfg.qdrant_url,
            collection=cfg.qdrant_collection,
            api_key=cfg.qdrant_api_key,
            fusion=cfg.fusion,
            effort_filter_mode=cfg.effort_filter_mode,
        )
        index.ensure_collection(vector_size=cfg.embed_dim)
        view = get_embed_view(cfg.embed_view_version)
        embed_service = EmbedService(embedder, index, view, cfg)
        session_service = SessionService(store, embed_service)
        search_service = SearchService(store, embedder, index, view, cfg)
        return cls(
            config=cfg,
            store=store,
            embedder=embedder,
            index=index,
            embed_service=embed_service,
            session_service=session_service,
            search_service=search_service,
        )
