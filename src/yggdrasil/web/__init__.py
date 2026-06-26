"""Yggdrasil control-plane UI (FastAPI + Jinja2)."""

__all__ = ["create_app"]


def __getattr__(name: str):
    if name == "create_app":
        from yggdrasil.web.app import create_app

        return create_app
    raise AttributeError(name)
