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
    }
