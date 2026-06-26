# Yggdrasil Production Control Plane — Architecture Spec

**Date:** 2026-06-25  
**Status:** Ready for implementation on branches `feat/ui` and `feat/multi-tenant`  
**Supersedes PoC threat model for shared deployment:** still read [`2026-06-25-threat-model-poc.md`](2026-06-25-threat-model-poc.md) for residual risks; this doc defines the **prod path**.

## 1. Goals

1. **UI** reachable via **Tailscale Funnel** and/or **DDNS + reverse proxy** (HTTPS), for humans to sign in, download personalized **Skill** + **MCP client config**, and manage tokens.
2. **Multi-principal auth** for real lab users listed in `user_mapping.yaml` (API key → owner name). **Same org (`lab`)**: all mapped owners **share one retrieval scope** (org-wide experience memory). Writes still bind **server-side `owner`** from the principal (no client spoofing).
3. **Demo tenant** for external customers: isolated data plane, **demo key/account**, retrieval against **demo-seeded** trajectories only; limited or audited writes.

Non-goals (v1): full SSO/OIDC (optional later); multi-org SaaS billing; Clio-grade PII pipeline (keep optional scrub + defaults).

## 2. Tenancy model

| Tenant ID | Who | Retrieval scope | Writes | Source of principals |
|-----------|-----|-----------------|--------|----------------------|
| **`lab`** | Internal users from `user_mapping.yaml` / `KEY_NAME_MAP` | **All `lab` trajectories** (any owner in the map) | Allowed; `owner` forced from principal | Static map + issued API tokens |
| **`demo`** | External trial users | **Only `tenant_id=demo`** trajectories | Optional limited write to demo only; rate-limited | Demo account + issued demo tokens |

**Critical product rule (user request):** Lab users are **one org**. Do **not** filter search results to “only my owner” for lab. Filter **must** enforce `tenant_id == principal.tenant_id`. Owner is for **attribution and handoff**, not isolation within lab.

Demo is a **separate tenant** — never returns lab trajectories.

## 3. High-level architecture

```text
                    Tailscale Funnel / DDNS (HTTPS)
                              |
                    +---------v----------+
                    |  Control plane UI  |  FastAPI + Jinja/HTMX (or static)
                    |  :8080 (internal)  |
                    +----+---------+-----+
                         |         |
           issue tokens  |         |  serve skill + mcp.json templates
                         |         |
                    +----v---------v-----+
                    |  Auth + Token API  |  (same FastAPI app)
                    |  POST /auth/token  |
                    |  GET  /me          |
                    +----+---------------+
                         |
                         | Bearer / X-Ygg-Token
                         v
                    +--------------------+
                    |  MCP gateway       |  Streamable HTTP MCP (remote)
                    |  /mcp              |  + legacy stdio for local dev
                    +----+---------------+
                         |
              PrincipalContext (tenant_id, owner, scopes)
                         |
         +---------------+----------------+
         v                                v
  SQLite (+ tenant_id)            Qdrant payload tenant_id
  forced filters on read          mandatory filter on search
```

**Deploy topology (recommended single host):**

- Process A: `uvicorn yggdrasil.web.app:app` (UI + token API + MCP HTTP).
- Process B (optional split): embed worker / Qdrant stays internal, **not** on Funnel.
- Tailscale **Funnel** exposes only **443 → control plane** (UI + MCP path). Qdrant **never** on Funnel.
- DDNS alternative: Caddy/nginx TLS to same app; firewall Qdrant to localhost.

## 4. Identity and tokens

### 4.1 Lab principals

1. Load `user_mapping.yaml` / `KEY_NAME_MAP` at startup → `owner` set + **fingerprints** (existing `api_key_owners` helpers). **Never** serve full API keys via UI.
2. **Bootstrap auth options (pick for v1):**
   - **A (simplest):** Operator pre-issues **long-lived org tokens** in UI admin (or env `YGG_LAB_TOKENS_FILE`) mapping `token_id → owner`. Users never paste Anthropic keys into the browser.
   - **B (key prove-once):** User pastes **lab API key once** over HTTPS; server fingerprints it, matches map, issues **opaque bearer** `ygg_…` stored hashed (sha256) in SQLite `api_tokens` table; browser never stores the sk- key.
3. Prefer **B for “download skill and connect”** UX; prefer **A for air-gapped**. Implement **B** as primary; support **A** via admin CLI.

Token record:

```text
token_id, token_hash, tenant_id ('lab'|'demo'), owner, scopes (read|write|admin),
created_at, expires_at, revoked_at, label
```

### 4.2 Demo principal

- Env: `YGG_DEMO_TOKEN` or seed row `owner=demo`, `tenant_id=demo`.
- UI “Try demo” creates/rotates a **demo user token** (or shares a well-known demo token for hackathon simplicity — document rotation).
- Seed script: `scripts/seed_demo_org.py` writes a few trajectories with `tenant_id=demo`, tags `demo_seed`.

### 4.3 Principal on every MCP call

```python
@dataclass(frozen=True)
class Principal:
    tenant_id: str          # "lab" | "demo"
    owner: str              # human name from map or "demo"
    scopes: frozenset[str]  # {"read","write"}
    token_id: str
```

- **stdio MCP (local):** inject via env `YGG_MCP_TOKEN` read at server start (single principal per process) — good for power users.
- **HTTP MCP:** `Authorization: Bearer <token>` per request; resolve principal or 401.

**Server-side binding:**

- `start_trajectory` / `append` / `finalize`: set `external_refs.owner = principal.owner`, `tenant_id = principal.tenant_id`. Ignore client-supplied owner/tenant for authz (log if mismatch).
- `search_strategies`: force Qdrant/SQLite filter `tenant_id == principal.tenant_id`. Lab sees all lab owners’ experiences.
- `get_trajectory`: allow only if trajectory.tenant_id == principal.tenant_id.
- `experience_grade` / tags: only principals with `write` may set; server may strip client attempts to set hydration trust tags unless `admin`.

## 5. Data model changes

### SQLite

- `trajectories.tenant_id TEXT NOT NULL DEFAULT 'lab'`
- Index `(tenant_id, status)`, `(tenant_id, owner)`
- New table `api_tokens` (above)
- Optional `audit_events` (token_id, action, trajectory_id, ts)

### Qdrant payload

- Add `tenant_id: str` on every point (required filter on all searches).
- Re-embed/backfill: existing PoC data → `tenant_id=lab`.

### Config

```text
YGG_TENANCY_MODE=enforced   # or off for legacy PoC
YGG_DEFAULT_TENANT=lab
YGG_USER_MAPPING_PATH=user_mapping.yaml
YGG_PUBLIC_BASE_URL=https://ygg.example.ts.net   # Funnel / DDNS URL for skill templates
YGG_UI_BIND=127.0.0.1:8080
YGG_DEMO_ENABLED=1
YGG_DEMO_OWNER=demo
YGG_TOKEN_TTL_DAYS=90
YGG_SCRUB_CONTENT=1          # default on in prod profile
```

## 6. UI (branch `feat/ui`)

**Stack:** FastAPI + Jinja2 templates + minimal CSS (no heavy SPA required for v1). Static assets under `src/yggdrasil/web/static/`.

**Routes:**

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Landing: Lab vs Demo |
| GET/POST | `/lab/login` | Prove key (B) or paste issued token (A) |
| GET | `/lab/home` | Session cookie; show owner, download buttons |
| GET | `/lab/skill.md` | Personalized `SKILL.md` (token + public URL embedded) |
| GET | `/lab/mcp.json` | Cursor/Claude MCP config snippet |
| GET/POST | `/demo` | Demo entry; issue/show demo token |
| GET | `/demo/skill.md` | Demo skill (tenant demo, read-heavy copy) |
| GET | `/healthz` | Liveness |
| POST | `/api/v1/tokens/exchange` | JSON: `{ "api_key": "sk-..." }` → `{ "token", "owner", "tenant_id" }` |
| POST | `/api/v1/tokens/revoke` | Auth required |

**Session:** signed cookie (`itsdangerous` / Starlette SessionMiddleware) with `token_id` only; MCP uses bearer token in downloaded config (not cookie).

**Skill template variables:** `{{ public_base_url }}`, `{{ mcp_url }}`, `{{ bearer_token }}`, `{{ owner }}`, `{{ tenant_id }}`, search-before-write policy text (existing skill).

**Tailscale Funnel runbook (doc section):**

```bash
# App listens on localhost:8080
tailscale funnel 8080
# Set YGG_PUBLIC_BASE_URL to the funnel HTTPS hostname
```

DDNS: point A/AAAA to host; Caddy `reverse_proxy 127.0.0.1:8080`.

## 7. Multi-tenant MCP (branch `feat/multi-tenant`)

1. **`Principal` + `TokenStore`** port/adapter (SQLite).
2. **`require_principal`** dependency for HTTP MCP and tools wrapper.
3. Propagate `tenant_id` through domain models, sqlite_store, vector payload, search filters.
4. **Backfill migration** for existing DBs: `UPDATE trajectories SET tenant_id='lab' WHERE tenant_id IS NULL`.
5. **stdio mode:** `YGG_MCP_TOKEN` required when `YGG_TENANCY_MODE=enforced`.
6. **Gate integration:** lab hydration excludes unchanged; demo never sees lab tags.
7. **Rate limit** demo writes (simple token bucket in memory / sqlite).

## 8. Security properties (v1 acceptance)

| Property | Mechanism |
|----------|-----------|
| Demo cannot read lab | `tenant_id` filter on all reads |
| Lab users share org memory | No owner filter on lab search |
| Owner not spoofable on write | Principal.owner overwrites client |
| Keys not in git / UI storage | Hash tokens; map file local only |
| Qdrant not public | Funnel only UI/MCP app |
| Audit | optional log table for get/search |

## 9. Branch split and merge order

| Branch | Owns | Depends on |
|--------|------|------------|
| **`feat/multi-tenant`** | Principal, tokens, tenant_id in store/index/search/MCP, demo seed | code-review-refactor |
| **`feat/ui`** | FastAPI UI, templates, skill/mcp download, funnel docs | **interfaces** from multi-tenant (can stub Principal with fake for UI-first; prefer merge multi-tenant first or define API contract in this spec) |

**Concurrent implementation:** UI may depend on **stable HTTP contracts** from §6–7. Multi-tenant implements contracts; UI consumes them. If parallel: UI uses **protocol stubs** (`TokenService` protocol) and integrates when multi-tenant merges.

**Merge order recommended:** `feat/multi-tenant` → `main` (or into `code-review-refactor`) then `feat/ui` → main. Or stack: ui branches from multi-tenant after first MT commit with `TokenStore` + `/api/v1/tokens/exchange`.

## 10. Testing

- Unit: token hash verify; principal forces owner; search filter always has tenant_id.
- Integration: two tenants in one SQLite; lab token cannot `get_trajectory` demo id.
- UI: TestClient exchange key → 200 + token; wrong key → 401.
- Demo seed + search isolation.

## 11. Open decisions (defaults chosen)

| Decision | Default |
|----------|---------|
| Key exchange vs pre-issued only | Exchange (B) + optional admin file (A) |
| Demo writes | Allowed, rate-limited, tenant=demo only |
| Cookie vs bearer for UI | Cookie for pages; bearer in MCP config |
| MCP transport for prod | Streamable HTTP on same app under `/mcp` |
| Multi-org beyond lab+demo | Out of scope v1 |

## 12. Implementation plans

- [`../plans/2026-06-25-feat-multi-tenant.md`](../plans/2026-06-25-feat-multi-tenant.md)
- [`../plans/2026-06-25-feat-ui.md`](../plans/2026-06-25-feat-ui.md)
