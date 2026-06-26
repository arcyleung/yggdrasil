"""Control-plane UI tests — real AuthService + SQLite token store."""
from __future__ import annotations

from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from yggdrasil.web.app import create_app  # noqa: E402
from yggdrasil.web.auth_factory import WebAuthFacade, build_web_auth  # noqa: E402
from yggdrasil.adapters.token_store import SqliteTokenStore  # noqa: E402
from yggdrasil.services.auth_service import AuthService  # noqa: E402


TEST_KEY = "sk-test-ui-key-alice"
TEST_MAP = {TEST_KEY: "alice"}


@pytest.fixture
def auth_db(tmp_path: Path) -> Path:
    return tmp_path / "ui_auth.db"


@pytest.fixture
def auth_facade(auth_db: Path) -> WebAuthFacade:
    return build_web_auth(sqlite_path=auth_db, key_name_map=TEST_MAP, env={})


@pytest.fixture
def client(auth_facade: WebAuthFacade, auth_db: Path) -> TestClient:
    app = create_app(
        auth=auth_facade,
        key_name_map=TEST_MAP,
        sqlite_path=auth_db,
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


def test_exchange_unknown_key_json(client: TestClient) -> None:
    r = client.post("/api/v1/tokens/exchange", json={"api_key": "sk-unknown-not-in-map"})
    assert r.status_code == 401


def test_exchange_known_key_issues_ygg_token(client: TestClient, auth_facade: WebAuthFacade) -> None:
    r = client.post("/api/v1/tokens/exchange", json={"api_key": TEST_KEY})
    assert r.status_code == 200
    body = r.json()
    assert body["owner"] == "alice"
    assert body["tenant_id"] == "lab"
    assert body["token"].startswith("ygg_")
    assert body["token_id"]
    assert TEST_KEY not in body["token"]
    # Token resolves via multi-tenant store
    p = auth_facade.resolve_token(body["token"])
    assert p.owner == "alice"
    assert p.tenant_id == "lab"


def test_lab_login_skill_with_token_query(client: TestClient, auth_facade: WebAuthFacade) -> None:
    r = client.post("/lab/login", data={"api_key": TEST_KEY}, follow_redirects=False)
    assert r.status_code == 303

    home = client.get("/lab/home")
    assert home.status_code == 200
    assert b"alice" in home.content
    # First home consumes flash — extract token from page or re-exchange
    exch = client.post("/api/v1/tokens/exchange", json={"api_key": TEST_KEY})
    tok = exch.json()["token"]

    skill = client.get(f"/lab/skill.md?token={tok}")
    assert skill.status_code == 200
    assert "http://test.example:8080" in skill.text
    assert tok in skill.text
    assert TEST_KEY not in skill.text

    mcp = client.get(f"/lab/mcp.json?token={tok}")
    assert mcp.status_code == 200
    assert tok in mcp.text


def test_session_does_not_keep_raw_token_after_home(client: TestClient) -> None:
    client.post("/lab/login", data={"api_key": TEST_KEY}, follow_redirects=False)
    client.get("/lab/home")  # consume flash
    # skill without token should 401 (session has token_id only)
    skill = client.get("/lab/skill.md")
    assert skill.status_code == 401


def test_revoke_invalidates_token(client: TestClient, auth_facade: WebAuthFacade) -> None:
    exch = client.post("/api/v1/tokens/exchange", json={"api_key": TEST_KEY})
    tok = exch.json()["token"]
    assert auth_facade.resolve_token(tok).owner == "alice"
    # login to set session token_id
    client.post("/lab/login", data={"api_key": TEST_KEY}, follow_redirects=False)
    rev = client.post(
        "/api/v1/tokens/revoke",
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert rev.status_code == 200
    with pytest.raises(Exception):
        auth_facade.resolve_token(tok)


def test_demo_token_resolves_in_store(client: TestClient, auth_facade: WebAuthFacade) -> None:
    r = client.post("/demo", follow_redirects=True)
    assert r.status_code == 200
    # issue via API path on facade
    result = auth_facade.issue_demo_token()
    assert result["tenant_id"] == "demo"
    assert result["token"].startswith("ygg_")
    p = auth_facade.resolve_token(result["token"])
    assert p.tenant_id == "demo"


def test_mcp_stub_501(client: TestClient) -> None:
    assert client.get("/mcp").status_code == 501


def test_token_usable_as_mcp_principal(auth_db: Path) -> None:
    """Issued UI token works with same resolve path MCP uses."""
    facade = build_web_auth(sqlite_path=auth_db, key_name_map=TEST_MAP, env={})
    raw, principal, _ = facade.auth_service.exchange_api_key(TEST_KEY)
    store = SqliteTokenStore(auth_db)
    p2 = store.resolve_token(raw)
    assert p2 is not None
    assert p2.owner == principal.owner
    assert p2.token_id == principal.token_id
