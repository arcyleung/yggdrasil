"""Multi-tenant isolation and principal binding tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from yggdrasil.adapters.embed_views import get_embed_view
from yggdrasil.adapters.sqlite_store import SqliteTrajectoryStore
from yggdrasil.adapters.token_store import SqliteTokenStore, hash_token
from yggdrasil.config import YggConfig
from yggdrasil.domain.enums import EffortFilterMode, FusionMode, TrajectoryStatus
from yggdrasil.domain.models import Outcome
from yggdrasil.domain.principal import Principal
from yggdrasil.services.auth_service import AuthError, AuthService
from yggdrasil.services.embed_service import EmbedService
from yggdrasil.services.errors import NotFoundError
from yggdrasil.services.principal_context import principal_scope
from yggdrasil.services.search_service import SearchService
from yggdrasil.services.session_service import SessionService
from fakes import FakeEmbedder, FakeVectorIndex


def _cfg(tmp_path: Path) -> YggConfig:
    return YggConfig(
        sqlite_path=tmp_path / "tenancy.db",
        qdrant_url="http://localhost:6333",
        qdrant_collection="test",
        qdrant_api_key=None,
        embed_base_url="http://127.0.0.1:9/v1",
        embed_api_key=None,
        embed_model="fake",
        embed_dim=8,
        default_domain="coding",
        search_include_open=True,
        fusion=FusionMode.RRF,
        w_task=1.0,
        w_scaffold=1.0,
        effort_filter_mode=EffortFilterMode.INCLUSIVE_NULL,
        embed_view_version="coding_v1",
        mongo_uri=None,
        mongo_creds_file=Path("mongo_creds.txt"),
        scrub_content=False,
        tenancy_mode="enforced",
        default_tenant="lab",
        demo_enabled=True,
        demo_owner="demo",
        token_ttl_days=90,
    )


def _services(tmp_path: Path, *, tenancy_enforced: bool = True):
    cfg = _cfg(tmp_path)
    store = SqliteTrajectoryStore(cfg.sqlite_path)
    embedder = FakeEmbedder(cfg.embed_dim)
    index = FakeVectorIndex()
    view = get_embed_view(cfg.embed_view_version)
    embed_service = EmbedService(embedder, index, view, cfg)
    session = SessionService(
        store,
        embed_service,
        tenancy_enforced=tenancy_enforced,
        default_tenant=cfg.default_tenant,
    )
    search = SearchService(
        store,
        embedder,
        index,
        view,
        cfg,
        tenancy_enforced=tenancy_enforced,
        apply_gates_default=False,
    )
    tokens = SqliteTokenStore(store.connection)
    auth = AuthService(
        tokens,
        key_name_map={"sk-lab-alice-key-0001": "alice", "sk-lab-bob-key-0002": "bob"},
        default_tenant="lab",
        demo_owner="demo",
        demo_enabled=True,
        token_ttl_days=90,
    )
    return store, session, search, tokens, auth, index


def _lab_principal(owner: str = "alice", token_id: str = "tok-alice") -> Principal:
    return Principal(
        tenant_id="lab",
        owner=owner,
        scopes=frozenset({"read", "write"}),
        token_id=token_id,
    )


def _demo_principal(token_id: str = "tok-demo") -> Principal:
    return Principal(
        tenant_id="demo",
        owner="demo",
        scopes=frozenset({"read", "write"}),
        token_id=token_id,
    )


def test_token_hash_and_resolve(tmp_path: Path) -> None:
    _, _, _, tokens, auth, _ = _services(tmp_path)
    raw, principal, record = auth.exchange_api_key("sk-lab-alice-key-0001")
    assert principal.owner == "alice"
    assert principal.tenant_id == "lab"
    assert record.token_hash == hash_token(raw)
    assert tokens.resolve_token(raw) == principal
    assert tokens.resolve_token("ygg_not_a_real_token") is None
    assert tokens.revoke(record.token_id) is True
    assert tokens.resolve_token(raw) is None


def test_exchange_unknown_key_raises(tmp_path: Path) -> None:
    _, _, _, _, auth, _ = _services(tmp_path)
    with pytest.raises(AuthError):
        auth.exchange_api_key("sk-unknown")


def test_lab_cannot_read_demo(tmp_path: Path) -> None:
    store, session, search, _, _, _ = _services(tmp_path)
    demo_p = _demo_principal()
    lab_p = _lab_principal()

    demo_traj = session.start_trajectory(
        task_text="demo only task about widgets",
        scaffold_text="demo scaffold widgets",
        external_refs={"owner": "demo"},
        principal=demo_p,
    )
    assert demo_traj.tenant_id == "demo"

    with pytest.raises(NotFoundError):
        session.get_trajectory(demo_traj.id, principal=lab_p)

    # Lab search must not return demo trajectories
    hits = search.search_strategies(
        task="demo only task about widgets",
        principal=lab_p,
        apply_gates=False,
    )
    assert all(h.trajectory_id != demo_traj.id for h in hits)
    assert all((h.tenant_id or "lab") == "lab" for h in hits)


def test_demo_cannot_read_lab(tmp_path: Path) -> None:
    store, session, search, _, _, _ = _services(tmp_path)
    demo_p = _demo_principal()
    lab_p = _lab_principal("bob", "tok-bob")

    lab_traj = session.start_trajectory(
        task_text="lab secret strategy for internal migration",
        scaffold_text="lab scaffold migration",
        external_refs={"owner": "spoof-me"},
        principal=lab_p,
    )
    assert lab_traj.tenant_id == "lab"
    # Owner forced from principal (spoof overwritten)
    assert lab_traj.external_refs.get("owner") == "bob"

    with pytest.raises(NotFoundError):
        session.get_trajectory(lab_traj.id, principal=demo_p)

    hits = search.search_strategies(
        task="lab secret strategy for internal migration",
        principal=demo_p,
        apply_gates=False,
    )
    assert all(h.trajectory_id != lab_traj.id for h in hits)
    assert all((h.tenant_id or "demo") == "demo" for h in hits)


def test_lab_search_not_filtered_by_owner(tmp_path: Path) -> None:
    """Lab users share org memory: alice sees bob's lab trajectories without owner filter."""
    _, session, search, _, _, _ = _services(tmp_path)
    alice = _lab_principal("alice", "tok-a")
    bob = _lab_principal("bob", "tok-b")

    bob_traj = session.start_trajectory(
        task_text="shared org memory about kubernetesization",
        scaffold_text="vector index and hybrid search",
        external_refs={"owner": "bob", "experience_grade": True},
        tags=["experience_grade"],
        principal=bob,
    )
    assert bob_traj.external_refs["owner"] == "bob"

    # Alice searches without passing owner= — should still find bob's lab work
    hits = search.search_strategies(
        task="shared org memory about vectorization",
        principal=alice,
        apply_gates=False,
    )
    ids = {h.trajectory_id for h in hits}
    assert bob_traj.id in ids
    # Ensure we did not force owner=alice on the vector query (would exclude bob)
    hit = next(h for h in hits if h.trajectory_id == bob_traj.id)
    assert hit.owner == "bob"
    assert hit.tenant_id == "lab"


def test_write_spoof_owner_overwritten(tmp_path: Path) -> None:
    _, session, _, _, _, _ = _services(tmp_path)
    alice = _lab_principal("alice")
    traj = session.start_trajectory(
        task_text="attempt to claim bob ownership",
        scaffold_text="spoof test",
        external_refs={"owner": "bob", "team": "evil"},
        principal=alice,
    )
    assert traj.external_refs["owner"] == "alice"
    assert traj.tenant_id == "lab"

    # update_meta also forces owner
    updated = session.update_trajectory_meta(
        trajectory_id=traj.id,
        external_refs={"owner": "mallory", "team": "still-evil"},
        principal=alice,
    )
    assert updated.external_refs["owner"] == "alice"


def test_tenancy_off_legacy_no_principal(tmp_path: Path) -> None:
    """Default off mode allows writes without principal (existing tests)."""
    _, session, search, _, _, _ = _services(tmp_path, tenancy_enforced=False)
    traj = session.start_trajectory(
        task_text="legacy path",
        scaffold_text="no principal",
        external_refs={"owner": "carol"},
    )
    assert traj.tenant_id == "lab"
    assert traj.external_refs["owner"] == "carol"
    got = session.get_trajectory(traj.id)
    assert got["trajectory"].id == traj.id
    hits = search.search_strategies(task="legacy path", apply_gates=False)
    assert any(h.trajectory_id == traj.id for h in hits)


def test_enforced_requires_principal(tmp_path: Path) -> None:
    _, session, search, _, _, _ = _services(tmp_path, tenancy_enforced=True)
    with pytest.raises(AuthError):
        session.start_trajectory(task_text="x", scaffold_text="y")
    with pytest.raises(AuthError):
        search.search_strategies(task="x")


def test_contextvar_principal_scope(tmp_path: Path) -> None:
    _, session, _, _, _, _ = _services(tmp_path, tenancy_enforced=True)
    alice = _lab_principal("alice")
    with principal_scope(alice):
        traj = session.start_trajectory(
            task_text="via contextvar",
            scaffold_text="ctx",
            external_refs={"owner": "nobody"},
        )
    assert traj.external_refs["owner"] == "alice"


def test_issue_demo_token(tmp_path: Path) -> None:
    _, _, _, tokens, auth, _ = _services(tmp_path)
    raw, principal, _ = auth.issue_demo_token()
    assert principal.tenant_id == "demo"
    assert principal.owner == "demo"
    assert tokens.resolve_token(raw).tenant_id == "demo"


def test_payload_carries_tenant_id(tmp_path: Path) -> None:
    _, session, _, _, _, index = _services(tmp_path)
    demo_p = _demo_principal()
    traj = session.start_trajectory(
        task_text="payload tenant check",
        scaffold_text="scaffold",
        principal=demo_p,
    )
    point = index.points[traj.id]
    assert point.payload.tenant_id == "demo"
