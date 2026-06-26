"""Control-plane UI tests (FastAPI TestClient)."""
from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from yggdrasil.web.app import create_app  # noqa: E402
from yggdrasil.web.auth_stub import AuthStub  # noqa: E402


TEST_KEY = "sk-test-ui-key-alice"
TEST_MAP = {TEST_KEY: "alice"}


@pytest.fixture
def client() -> TestClient:
    auth = AuthStub(secret="test-secret", key_name_map=TEST_MAP)
    app = create_app(
        auth=auth,
        key_name_map=TEST_MAP,
        env={"YGG_UI_SECRET": "test-secret", "YGG_PUBLIC_BASE_URL": "http://test.example:8080"},
        public_base_url="http://test.example:8080",
    )
    return TestClient(app)


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_home(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert b"Lab" in r.content
    assert b"Demo" in r.content


def test_exchange_unknown_key_json(client: TestClient) -> None:
    r = client.post("/api/v1/tokens/exchange", json={"api_key": "sk-unknown-not-in-map"})
    assert r.status_code == 401


def test_exchange_known_key_json(client: TestClient) -> None:
    r = client.post("/api/v1/tokens/exchange", json={"api_key": TEST_KEY})
    assert r.status_code == 200
    body = r.json()
    assert body["owner"] == "alice"
    assert body["tenant_id"] == "lab"
    assert body["token"]
    assert "sk-" not in body["token"]  # issued token, not raw key


def test_lab_login_unknown_key_form(client: TestClient) -> None:
    r = client.post("/lab/login", data={"api_key": "sk-bad"}, follow_redirects=False)
    assert r.status_code == 200
    assert b"unknown" in r.content.lower() or b"error" in r.content.lower() or b"api" in r.content.lower()


def test_lab_login_and_skill_download(client: TestClient) -> None:
    r = client.post("/lab/login", data={"api_key": TEST_KEY}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/lab/home"

    home = client.get("/lab/home")
    assert home.status_code == 200
    assert b"alice" in home.content

    skill = client.get("/lab/skill.md")
    assert skill.status_code == 200
    text = skill.text
    assert "http://test.example:8080" in text
    assert "Bearer " in text or "bearer" in text.lower()
    assert "alice" in text
    assert "lab" in text
    assert TEST_KEY not in text  # never embed sk- key

    mcp = client.get("/lab/mcp.json")
    assert mcp.status_code == 200
    assert "http://test.example:8080/mcp" in mcp.text
    assert "Authorization" in mcp.text
    assert TEST_KEY not in mcp.text


def test_lab_home_requires_session(client: TestClient) -> None:
    r = client.get("/lab/home", follow_redirects=False)
    assert r.status_code == 303
    assert "/lab/login" in r.headers["location"]


def test_demo_issues_token(client: TestClient) -> None:
    r = client.post("/demo", follow_redirects=True)
    assert r.status_code == 200
    assert b"demo" in r.content.lower()

    skill = client.get("/demo/skill.md")
    assert skill.status_code == 200
    assert "demo" in skill.text
    assert "http://test.example:8080" in skill.text


def test_mcp_stub_501(client: TestClient) -> None:
    r = client.get("/mcp")
    assert r.status_code == 501


def test_auth_stub_resolve_roundtrip() -> None:
    auth = AuthStub(secret="s", key_name_map=TEST_MAP)
    result = auth.exchange_api_key(TEST_KEY)
    principal = auth.resolve_token(result["token"])
    assert principal.owner == "alice"
    assert principal.tenant_id == "lab"
    assert "read" in principal.scopes


def test_monkeypatch_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    """If mapping has a test key via env/monkeypatch, exchange succeeds."""
    monkeypatch.setenv("YGG_UI_SECRET", "mp-secret")
    # Inject map via create_app key_name_map (simulates loaded mapping)
    auth = AuthStub(secret="mp-secret", key_name_map={"sk-mp-key": "bob"})
    app = create_app(auth=auth, public_base_url="http://127.0.0.1:8080")
    c = TestClient(app)
    r = c.post("/api/v1/tokens/exchange", json={"api_key": "sk-mp-key"})
    assert r.status_code == 200
    assert r.json()["owner"] == "bob"
    bad = c.post("/api/v1/tokens/exchange", json={"api_key": "sk-nope"})
    assert bad.status_code == 401
