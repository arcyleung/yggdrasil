"""Minimal config loader tests."""
from yggdrasil.config import ConfigError, load_config, redact_config_for_log
from yggdrasil.domain.enums import EffortFilterMode, FusionMode


def test_load_config_defaults():
    cfg = load_config(environ={}, load_dotenv_file=False)
    assert cfg.embed_dim == 1536
    assert cfg.fusion == FusionMode.RRF
    assert cfg.effort_filter_mode == EffortFilterMode.INCLUSIVE_NULL
    assert cfg.search_include_open is True
    assert cfg.default_domain == "coding"
    assert cfg.embed_view_version == "coding_v1"
    assert cfg.qdrant_api_key is None
    assert cfg.embed_api_key is None


def test_load_config_overrides_and_redaction():
    env = {
        "EMBED_DIM": "768",
        "YGG_FUSION": "weighted",
        "YGG_W_TASK": "0.7",
        "YGG_W_SCAFFOLD": "0.3",
        "YGG_SEARCH_INCLUDE_OPEN": "false",
        "EMBED_API_KEY": "secret-key",
        "QDRANT_API_KEY": "q-secret",
        "MONGO_URI": "mongodb://user:pass@localhost/db",
    }
    cfg = load_config(environ=env, load_dotenv_file=False)
    assert cfg.embed_dim == 768
    assert cfg.fusion == FusionMode.WEIGHTED
    assert cfg.search_include_open is False
    redacted = redact_config_for_log(cfg)
    assert redacted["embed_api_key"] == "***"
    assert redacted["qdrant_api_key"] == "***"
    assert redacted["mongo_uri"] == "***"


def test_invalid_embed_dim():
    try:
        load_config(environ={"EMBED_DIM": "0"}, load_dotenv_file=False)
        assert False, "expected ConfigError"
    except ConfigError:
        pass


def test_invalid_fusion():
    try:
        load_config(environ={"YGG_FUSION": "bogus"}, load_dotenv_file=False)
        assert False, "expected ConfigError"
    except ConfigError:
        pass
