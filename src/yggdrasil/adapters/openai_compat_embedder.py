"""OpenAI-compatible HTTP embedder adapter."""
from __future__ import annotations

from typing import Any, Protocol, Sequence

import httpx

from yggdrasil.services.errors import EmbedFailedError


class HttpClientProtocol(Protocol):
    def post(self, url: str, *, headers: dict[str, str] | None = None, json: Any = None) -> Any: ...


class OpenAICompatEmbedder:
    """Embedder using OpenAI-compatible /embeddings endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        dimensions: int,
        api_key: str | None = None,
        client: HttpClientProtocol | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._dimensions = dimensions
        self._api_key = api_key
        self._owns_client = client is None
        self._client: Any = client if client is not None else httpx.Client(timeout=timeout)

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def close(self) -> None:
        if self._owns_client and hasattr(self._client, "close"):
            self._client.close()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        url = f"{self._base_url}/embeddings"
        body = {"model": self._model, "input": list(texts)}
        try:
            resp = self._client.post(url, headers=self._headers(), json=body)
        except Exception as exc:
            raise EmbedFailedError(f"embed request failed: {exc}") from exc

        status = getattr(resp, "status_code", None)
        if status is not None and status >= 400:
            text = getattr(resp, "text", "") or str(resp)
            raise EmbedFailedError(f"embed HTTP {status}: {text}")

        try:
            if hasattr(resp, "json") and callable(resp.json):
                payload = resp.json()
            else:
                payload = resp
            data = payload.get("data", []) if isinstance(payload, dict) else []
        except Exception as exc:
            raise EmbedFailedError(f"embed response parse failed: {exc}") from exc

        try:
            sorted_data = sorted(data, key=lambda item: item.get("index", 0))
            vectors = [list(item["embedding"]) for item in sorted_data]
        except Exception as exc:
            raise EmbedFailedError(f"embed response missing embeddings: {exc}") from exc

        if len(vectors) != len(texts):
            raise EmbedFailedError(
                f"embed count mismatch: expected {len(texts)}, got {len(vectors)}"
            )
        return vectors

    def embed_one(self, text: str) -> list[float]:
        vectors = self.embed_texts([text])
        return vectors[0]
