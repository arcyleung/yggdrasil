"""SQLite-backed opaque API token store (same DB as trajectories)."""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from yggdrasil.domain.principal import Principal
from yggdrasil.ports.token_store import TokenRecord, TokenStore

API_TOKENS_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_tokens (
    token_id TEXT PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    tenant_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    scopes_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked_at TEXT,
    label TEXT
);

CREATE INDEX IF NOT EXISTS idx_api_tokens_hash ON api_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_api_tokens_tenant_owner ON api_tokens(tenant_id, owner);
"""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dt_to_str(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _dt_from_str(raw: str | None) -> datetime | None:
    if raw is None:
        return None
    return datetime.fromisoformat(raw)


def hash_token(raw_token: str) -> str:
    """SHA-256 hex digest of the opaque token (never store raw)."""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def generate_raw_token() -> str:
    """Opaque bearer token with ygg_ prefix."""
    return "ygg_" + secrets.token_urlsafe(32)


def _scopes_to_json(scopes: frozenset[str] | set[str] | list[str] | Sequence[str]) -> str:
    import json

    return json.dumps(sorted(set(scopes)))


def _scopes_from_json(raw: str) -> frozenset[str]:
    import json

    data = json.loads(raw)
    return frozenset(str(s) for s in data)


class SqliteTokenStore:
    """Token store using the same SQLite connection/path as trajectories."""

    def __init__(self, db_path: str | Path | sqlite3.Connection) -> None:
        if isinstance(db_path, sqlite3.Connection):
            self._conn = db_path
            self._owns_conn = False
        else:
            path = Path(db_path)
            if path.parent != Path("."):
                path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._owns_conn = True
        if self._conn.row_factory is None:
            self._conn.row_factory = sqlite3.Row
        self._conn.executescript(API_TOKENS_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        if self._owns_conn:
            self._conn.close()

    def _row_to_record(self, row: sqlite3.Row) -> TokenRecord:
        return TokenRecord(
            token_id=row["token_id"],
            token_hash=row["token_hash"],
            tenant_id=row["tenant_id"],
            owner=row["owner"],
            scopes=_scopes_from_json(row["scopes_json"]),
            created_at=_dt_from_str(row["created_at"]) or _utcnow(),
            expires_at=_dt_from_str(row["expires_at"]),
            revoked_at=_dt_from_str(row["revoked_at"]),
            label=row["label"],
        )

    def create_token(
        self,
        *,
        tenant_id: str,
        owner: str,
        scopes: frozenset[str] | set[str] | list[str],
        label: str | None = None,
        expires_at: datetime | None = None,
        raw_token: str | None = None,
    ) -> tuple[str, TokenRecord]:
        raw = raw_token or generate_raw_token()
        token_id = str(uuid.uuid4())
        token_hash = hash_token(raw)
        now = _utcnow()
        scope_set = frozenset(scopes)
        self._conn.execute(
            """
            INSERT INTO api_tokens (
                token_id, token_hash, tenant_id, owner, scopes_json,
                created_at, expires_at, revoked_at, label
            ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)
            """,
            (
                token_id,
                token_hash,
                tenant_id,
                owner,
                _scopes_to_json(scope_set),
                _dt_to_str(now),
                _dt_to_str(expires_at),
                label,
            ),
        )
        self._conn.commit()
        record = TokenRecord(
            token_id=token_id,
            token_hash=token_hash,
            tenant_id=tenant_id,
            owner=owner,
            scopes=scope_set,
            created_at=now,
            expires_at=expires_at,
            revoked_at=None,
            label=label,
        )
        return raw, record

    def resolve_token(self, raw_token: str) -> Principal | None:
        if not raw_token or not str(raw_token).strip():
            return None
        token_hash = hash_token(str(raw_token).strip())
        row = self._conn.execute(
            "SELECT * FROM api_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if row is None:
            return None
        record = self._row_to_record(row)
        if record.revoked_at is not None:
            return None
        if record.expires_at is not None:
            exp = record.expires_at
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=timezone.utc)
            if exp < _utcnow():
                return None
        return record.to_principal()

    def revoke(self, token_id: str) -> bool:
        now = _dt_to_str(_utcnow())
        cur = self._conn.execute(
            """
            UPDATE api_tokens SET revoked_at = ?
            WHERE token_id = ? AND revoked_at IS NULL
            """,
            (now, token_id),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def get_record(self, token_id: str) -> TokenRecord | None:
        row = self._conn.execute(
            "SELECT * FROM api_tokens WHERE token_id = ?",
            (token_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)


_: type[TokenStore] = SqliteTokenStore  # type: ignore[misc,assignment]
