"""Environment-backed configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from dotenv import load_dotenv

from yggdrasil.domain.enums import EffortFilterMode, FusionMode


class ConfigError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class YggConfig:
    sqlite_path: Path
    qdrant_url: str
    qdrant_collection: str
    qdrant_api_key: str | None
    embed_base_url: str
    embed_api_key: str | None
    embed_model: str
    embed_dim: int
    default_domain: str
    search_include_open: bool
    fusion: FusionMode
    w_task: float
    w_scaffold: float
    effort_filter_mode: EffortFilterMode
    embed_view_version: str
    mongo_uri: str | None
    mongo_creds_file: Path
    # Wave F: optional regex content scrub on write (default off for backward compat)
    scrub_content: bool = False
    # Multi-tenant control plane
    tenancy_mode: str = "off"  # "off" | "enforced"
    default_tenant: str = "lab"
    user_mapping_path: Path | None = None
    public_base_url: str | None = None
    ui_bind: str = "127.0.0.1:8080"
    demo_enabled: bool = True
    demo_owner: str = "demo"
    demo_token: str | None = None  # optional pre-shared demo token seed
    token_ttl_days: int = 90
    mcp_token: str | None = None  # YGG_MCP_TOKEN for stdio principal


def _env_get(env: Mapping[str, str], key: str, default: str | None = None) -> str | None:
    if key in env:
        return env[key]
    return default


def _optional_secret(env: Mapping[str, str], key: str) -> str | None:
    raw = _env_get(env, key)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def _parse_bool(raw: str, *, field_name: str) -> bool:
    v = raw.strip().lower()
    if v in {"true", "1", "yes"}:
        return True
    if v in {"false", "0", "no"}:
        return False
    raise ConfigError(f"invalid boolean for {field_name}: {raw!r}")


def _parse_positive_int(raw: str, *, field_name: str) -> int:
    try:
        n = int(raw)
    except ValueError as exc:
        raise ConfigError(f"invalid integer for {field_name}: {raw!r}") from exc
    if n <= 0:
        raise ConfigError(f"{field_name} must be > 0, got {n}")
    return n


def _parse_float(raw: str, *, field_name: str) -> float:
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"invalid float for {field_name}: {raw!r}") from exc


def _parse_fusion(raw: str) -> FusionMode:
    v = raw.strip().lower()
    try:
        return FusionMode(v)
    except ValueError as exc:
        raise ConfigError(f"invalid YGG_FUSION: {raw!r}") from exc


def _parse_effort_filter_mode(raw: str) -> EffortFilterMode:
    v = raw.strip().lower()
    try:
        return EffortFilterMode(v)
    except ValueError as exc:
        raise ConfigError(f"invalid YGG_EFFORT_FILTER_MODE: {raw!r}") from exc


def _parse_tenancy_mode(raw: str) -> str:
    v = raw.strip().lower()
    if v in {"off", "0", "false", "no", "legacy"}:
        return "off"
    if v in {"enforced", "on", "1", "true", "yes"}:
        return "enforced"
    raise ConfigError(f"invalid YGG_TENANCY_MODE: {raw!r} (use off|enforced)")


def load_config(
    environ: Mapping[str, str] | None = None,
    *,
    dotenv_path: str | Path | None = None,
    load_dotenv_file: bool = True,
) -> YggConfig:
    if load_dotenv_file:
        if dotenv_path is not None:
            load_dotenv(dotenv_path=str(dotenv_path), override=False)
        else:
            load_dotenv(override=False)

    env: Mapping[str, str] = environ if environ is not None else os.environ

    embed_dim_raw = _env_get(env, "EMBED_DIM", "1536") or "1536"
    embed_dim = _parse_positive_int(embed_dim_raw, field_name="EMBED_DIM")

    fusion_raw = _env_get(env, "YGG_FUSION", "rrf") or "rrf"
    fusion = _parse_fusion(fusion_raw)

    w_task = _parse_float(_env_get(env, "YGG_W_TASK", "1.0") or "1.0", field_name="YGG_W_TASK")
    w_scaffold = _parse_float(
        _env_get(env, "YGG_W_SCAFFOLD", "1.0") or "1.0", field_name="YGG_W_SCAFFOLD"
    )
    if fusion == FusionMode.WEIGHTED and (w_task <= 0 or w_scaffold <= 0):
        raise ConfigError("YGG_W_TASK and YGG_W_SCAFFOLD must be > 0 when YGG_FUSION=weighted")

    effort_mode_raw = _env_get(env, "YGG_EFFORT_FILTER_MODE", "inclusive_null") or "inclusive_null"
    effort_filter_mode = _parse_effort_filter_mode(effort_mode_raw)

    include_open_raw = _env_get(env, "YGG_SEARCH_INCLUDE_OPEN", "true") or "true"
    search_include_open = _parse_bool(include_open_raw, field_name="YGG_SEARCH_INCLUDE_OPEN")

    scrub_raw = _env_get(env, "YGG_SCRUB_CONTENT", "0") or "0"
    scrub_content = _parse_bool(scrub_raw, field_name="YGG_SCRUB_CONTENT")

    qdrant_url = (_env_get(env, "QDRANT_URL", "http://localhost:6333") or "").strip()
    if not qdrant_url:
        raise ConfigError("QDRANT_URL must be non-empty")

    embed_base_url = (_env_get(env, "EMBED_BASE_URL", "https://api.openai.com/v1") or "").strip()
    if not embed_base_url:
        raise ConfigError("EMBED_BASE_URL must be non-empty")

    embed_model = (_env_get(env, "EMBED_MODEL", "text-embedding-3-small") or "").strip()
    if not embed_model:
        raise ConfigError("EMBED_MODEL must be non-empty")

    default_domain = (_env_get(env, "YGG_DEFAULT_DOMAIN", "coding") or "").strip()
    if not default_domain:
        raise ConfigError("YGG_DEFAULT_DOMAIN must be non-empty")

    tenancy_mode = _parse_tenancy_mode(_env_get(env, "YGG_TENANCY_MODE", "off") or "off")
    default_tenant = (_env_get(env, "YGG_DEFAULT_TENANT", "lab") or "lab").strip() or "lab"
    mapping_raw = (_env_get(env, "YGG_USER_MAPPING_PATH", "") or "").strip()
    user_mapping_path = Path(mapping_raw) if mapping_raw else None
    # Fall back to KEY_NAME_MAP path if set as a file path (handled by auth via KEY_NAME_MAP env)
    public_base_url = _optional_secret(env, "YGG_PUBLIC_BASE_URL")
    ui_bind = (_env_get(env, "YGG_UI_BIND", "127.0.0.1:8080") or "127.0.0.1:8080").strip()
    demo_enabled = _parse_bool(_env_get(env, "YGG_DEMO_ENABLED", "1") or "1", field_name="YGG_DEMO_ENABLED")
    demo_owner = (_env_get(env, "YGG_DEMO_OWNER", "demo") or "demo").strip() or "demo"
    demo_token = _optional_secret(env, "YGG_DEMO_TOKEN")
    token_ttl_days = _parse_positive_int(
        _env_get(env, "YGG_TOKEN_TTL_DAYS", "90") or "90", field_name="YGG_TOKEN_TTL_DAYS"
    )
    mcp_token = _optional_secret(env, "YGG_MCP_TOKEN")

    return YggConfig(
        sqlite_path=Path(_env_get(env, "YGG_SQLITE_PATH", "./data/yggdrasil.db") or "./data/yggdrasil.db"),
        qdrant_url=qdrant_url,
        qdrant_collection=_env_get(env, "QDRANT_COLLECTION", "yggdrasil_trajectories")
        or "yggdrasil_trajectories",
        qdrant_api_key=_optional_secret(env, "QDRANT_API_KEY"),
        embed_base_url=embed_base_url,
        embed_api_key=_optional_secret(env, "EMBED_API_KEY"),
        embed_model=embed_model,
        embed_dim=embed_dim,
        default_domain=default_domain,
        search_include_open=search_include_open,
        fusion=fusion,
        w_task=w_task,
        w_scaffold=w_scaffold,
        effort_filter_mode=effort_filter_mode,
        embed_view_version=_env_get(env, "YGG_EMBED_VIEW_VERSION", "coding_v1") or "coding_v1",
        mongo_uri=_optional_secret(env, "MONGO_URI"),
        mongo_creds_file=Path(
            _env_get(env, "YGG_MONGO_CREDS_FILE", "mongo_creds.txt") or "mongo_creds.txt"
        ),
        scrub_content=scrub_content,
        tenancy_mode=tenancy_mode,
        default_tenant=default_tenant,
        user_mapping_path=user_mapping_path,
        public_base_url=public_base_url,
        ui_bind=ui_bind,
        demo_enabled=demo_enabled,
        demo_owner=demo_owner,
        demo_token=demo_token,
        token_ttl_days=token_ttl_days,
        mcp_token=mcp_token,
    )


def redact_config_for_log(config: YggConfig) -> dict[str, Any]:
    def mask(value: str | None) -> str | None:
        return None if value is None else "***"

    return {
        "sqlite_path": str(config.sqlite_path),
        "qdrant_url": config.qdrant_url,
        "qdrant_collection": config.qdrant_collection,
        "qdrant_api_key": mask(config.qdrant_api_key),
        "embed_base_url": config.embed_base_url,
        "embed_api_key": mask(config.embed_api_key),
        "embed_model": config.embed_model,
        "embed_dim": config.embed_dim,
        "default_domain": config.default_domain,
        "search_include_open": config.search_include_open,
        "fusion": config.fusion.value,
        "scrub_content": config.scrub_content,
        "w_task": config.w_task,
        "w_scaffold": config.w_scaffold,
        "effort_filter_mode": config.effort_filter_mode.value,
        "embed_view_version": config.embed_view_version,
        "mongo_uri": mask(config.mongo_uri),
        "mongo_creds_file": str(config.mongo_creds_file),
        "tenancy_mode": config.tenancy_mode,
        "default_tenant": config.default_tenant,
        "user_mapping_path": str(config.user_mapping_path) if config.user_mapping_path else None,
        "public_base_url": config.public_base_url,
        "ui_bind": config.ui_bind,
        "demo_enabled": config.demo_enabled,
        "demo_owner": config.demo_owner,
        "demo_token": mask(config.demo_token),
        "token_ttl_days": config.token_ttl_days,
        "mcp_token": mask(config.mcp_token),
    }
