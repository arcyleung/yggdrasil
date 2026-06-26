import pytest

from yggdrasil.adapters.qdrant_index import QdrantIndex
from yggdrasil.config import ConfigError


class _FakeCollections:
    def __init__(self, names):
        self.collections = [type("C", (), {"name": n})() for n in names]


class _FakeClient:
    def __init__(self, *, existing=True, task_size=1536):
        self._existing = existing
        self._task_size = task_size
        self.created = False

    def get_collections(self):
        names = ["traj"] if self._existing else []
        return _FakeCollections(names)

    def get_collection(self, name):
        # Mimic qdrant-client collection info shape for named vectors
        task_params = type("VP", (), {"size": self._task_size})()
        vectors = {"task": task_params, "scaffold": task_params}
        config = type("Cfg", (), {"params": type("P", (), {"vectors": vectors})()})()
        return type("Info", (), {"config": config})()

    def create_collection(self, **kwargs):
        self.created = True


def test_ensure_collection_creates_when_missing():
    client = _FakeClient(existing=False)
    idx = QdrantIndex(url="http://x", collection="traj", client=client)
    idx.ensure_collection(vector_size=8)
    assert client.created is True


def test_ensure_collection_ok_when_size_matches():
    client = _FakeClient(existing=True, task_size=8)
    idx = QdrantIndex(url="http://x", collection="traj", client=client)
    idx.ensure_collection(vector_size=8)
    assert client.created is False


def test_ensure_collection_raises_on_size_mismatch():
    client = _FakeClient(existing=True, task_size=1536)
    idx = QdrantIndex(url="http://x", collection="traj", client=client)
    with pytest.raises((ConfigError, ValueError)) as exc:
        idx.ensure_collection(vector_size=8)
    msg = str(exc.value).lower()
    assert "1536" in msg or "size" in msg or "dimension" in msg or "vector" in msg
