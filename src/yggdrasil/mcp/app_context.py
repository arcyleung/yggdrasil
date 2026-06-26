"""Application wiring for MCP server."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from yggdrasil.adapters.embed_views import get_embed_view
from yggdrasil.adapters.openai_compat_embedder import OpenAICompatEmbedder
from yggdrasil.adapters.qdrant_index import QdrantIndex
from yggdrasil.adapters.sqlite_store import SqliteTrajectoryStore
from yggdrasil.adapters.token_store import SqliteTokenStore
from yggdrasil.config import YggConfig, load_config, redact_config_for_log
from yggdrasil.domain.principal import Principal
from yggdrasil.services.auth_service import AuthError, AuthService
from yggdrasil.services.embed_service import EmbedService
from yggdrasil.services.principal_context import set_principal
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
    token_store: SqliteTokenStore | None = None
    auth_service: AuthService | None = None
    principal: Principal | None = None

    @property
    def tenancy_enforced(self) -> bool:
        return self.config.tenancy_mode == "enforced"

    @classmethod
    def from_config(cls, config: YggConfig | None = None) -> AppContext:
        cfg = config or load_config()
        logger.info("yggdrasil config: %s", redact_config_for_log(cfg))

        store = SqliteTrajectoryStore(cfg.sqlite_path)
        tenancy_enforced = cfg.tenancy_mode == "enforced"

        token_store = SqliteTokenStore(store.connection)
        auth_service = AuthService(
            token_store,
            user_mapping_path=cfg.user_mapping_path,
            default_tenant=cfg.default_tenant,
            demo_owner=cfg.demo_owner,
            demo_enabled=cfg.demo_enabled,
            token_ttl_days=cfg.token_ttl_days,
        )
        # Seed well-known demo token if configured and not already present
        if cfg.demo_token and cfg.demo_enabled:
            existing = token_store.resolve_token(cfg.demo_token)
            if existing is None:
                try:
                    auth_service.issue_demo_token(
                        owner=cfg.demo_owner,
                        label="env:YGG_DEMO_TOKEN",
                        raw_token=cfg.demo_token,
                    )
                except Exception as exc:
                    logger.warning("failed to seed YGG_DEMO_TOKEN: %s", exc)

        principal: Principal | None = None
        mcp_token = cfg.mcp_token or os.environ.get("YGG_MCP_TOKEN")
        if mcp_token:
            principal = token_store.resolve_token(mcp_token)
            if principal is None and tenancy_enforced:
                raise AuthError("YGG_MCP_TOKEN is set but not valid (revoked/unknown)")
            if principal is not None:
                set_principal(principal)
                logger.info(
                    "bound MCP principal tenant=%s owner=%s token_id=%s",
                    principal.tenant_id,
                    principal.owner,
                    principal.token_id,
                )
        elif tenancy_enforced:
            logger.warning(
                "YGG_TENANCY_MODE=enforced but YGG_MCP_TOKEN not set; tools will reject unauthenticated calls"
            )

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
            w_task=cfg.w_task,
            w_scaffold=cfg.w_scaffold,
        )
        index.ensure_collection(vector_size=cfg.embed_dim)
        view = get_embed_view(cfg.embed_view_version)
        embed_service = EmbedService(embedder, index, view, cfg)
        scrubber = None
        if cfg.scrub_content:
            from yggdrasil.adapters.regex_scrubber import RegexContentScrubber

            scrubber = RegexContentScrubber()
        session_service = SessionService(
            store,
            embed_service,
            scrubber=scrubber,
            tenancy_enforced=tenancy_enforced,
            default_tenant=cfg.default_tenant,
        )
        search_service = SearchService(
            store,
            embedder,
            index,
            view,
            cfg,
            tenancy_enforced=tenancy_enforced,
        )
        return cls(
            config=cfg,
            store=store,
            embedder=embedder,
            index=index,
            embed_service=embed_service,
            session_service=session_service,
            search_service=search_service,
            token_store=token_store,
            auth_service=auth_service,
            principal=principal,
        )
