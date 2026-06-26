# feat/multi-tenant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development or executing-plans. Worktree: `.worktrees/feat-multi-tenant` on branch `feat/multi-tenant`.

**Goal:** Enforce `tenant_id` + server-side `Principal` on all reads/writes; lab shares org scope; demo isolated; opaque API tokens; HTTP-ready auth hooks.

**Architecture:** See `docs/superpowers/specs/2026-06-25-yggdrasil-prod-control-plane.md` §§2–5,7.

**Tech Stack:** Existing Python package; SQLite for tokens; pydantic; optional httpx.

## Global Constraints

- Lab retrieval: filter `tenant_id=lab` only — **not** by owner.
- Demo retrieval: `tenant_id=demo` only.
- Never trust client `owner` / `tenant_id` when tenancy enforced.
- TDD; commits on `feat/multi-tenant` only.
- Do not implement full UI (that is `feat/ui`); expose functions UI will call.

## Tasks

### Task MT1: Domain tenant_id + Principal types

**Files:** Create `src/yggdrasil/domain/principal.py`; modify `domain/models.py`, `domain/enums.py` if needed; `ports/store.py` payloads.

- Add `tenant_id: str = "lab"` on Trajectory / VectorPointPayload.
- `Principal` dataclass frozen.

### Task MT2: Token store (SQLite)

**Files:** `adapters/token_store.py`, `ports/token_store.py`, migration in sqlite or separate `auth.db` — prefer **same SQLite** `api_tokens` table.

- `create_token(tenant_id, owner, scopes) -> (raw_token, record)`
- `resolve_token(raw) -> Principal | None` (sha256 lookup)
- `revoke(token_id)`

### Task MT3: Key exchange service

**Files:** `services/auth_service.py`

- Load mapping via existing `load_key_name_map` / fingerprints.
- `exchange_api_key(api_key: str) -> Principal+token` or raise AuthError.
- Demo: `issue_demo_token()`.

### Task MT4: Wire tenancy into sqlite_store + search + session

- All inserts set tenant_id from principal.
- `get_trajectory` checks tenant.
- Search builds filter tenant_id=principal.tenant_id (lab: all owners in tenant).
- Qdrant payload + filter.

### Task MT5: MCP tools principal injection

- `mcp/app_context.py` / tools: resolve principal from contextvar or request state.
- stdio: `YGG_MCP_TOKEN` env.
- When `YGG_TENANCY_MODE=off`, legacy behavior (principal=None, tenant lab default) for tests.

### Task MT6: Demo seed script + tests

- `scripts/seed_demo_org.py`
- Tests: isolation lab vs demo; owner spoof rejected; lab search returns other owners' lab trajectories.

### Task MT7: Config + .env.example

Document all `YGG_*` tenancy keys.

**Done when:** `pytest -q` green; new tests for tenancy; no UI required.
