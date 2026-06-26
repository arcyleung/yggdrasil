# Yggdrasil — domain context (ubiquitous language)

Shared language for agents and humans. Prefer these terms in code, ADRs, and issues.

## Core concepts

| Term | Meaning |
|------|---------|
| **Trajectory** | One unit of agent experience: task + scaffold + steps + outcome + effort + artifacts. Parent sessions may own **segment** children. |
| **Strategy** | Search key = current **task** + **scaffold** only (not full retry history). |
| **Experience memory** | Org-wide retrieval of trajectories — **not** document RAG. |
| **Owner** | Human attribution for handoff (from API key map). In **lab** tenant, search is **org-wide** (all owners); owner is not an isolation key. |
| **Tenant** | Isolation plane: `lab` (internal org) vs `demo` (external trial). All reads/writes filtered by `tenant_id`. |
| **Principal** | Authenticated caller: `{tenant_id, owner, scopes, token_id}`. Server forces owner/tenant on write. |
| **Index status** | Dual-store health: SQLite SoT vs Qdrant vectors (`pending` / `ready` / `failed` / `stale`). |
| **Hydration** | Offline Mongo proxy logs → segment → SQLite + embed → Qdrant (external pre-embed path). |
| **Control plane** | FastAPI UI + token exchange (Funnel `/ygg`); not the data plane. |
| **Data plane** | Trajectory store + vector index + embedder used by MCP tools. |

## Scaling vocabulary (target architecture)

| Term | Meaning |
|------|---------|
| **Replica** | Stateless app process (UI and/or MCP gateway) behind a load balancer; **no local SQLite as SoT** when N>1. |
| **Write-ahead / outbox** | Durable queue of embed/index jobs so writers don't block on Qdrant/embed latency. |
| **Search fan-out** | Query path that hits Qdrant (filter+vectors) then hydrates from the SoT by id — not full table scans. |
| **SoT** | Source of truth for trajectories (today SQLite; multi-replica → **Postgres** or equivalent). |
| **Vector tier** | Qdrant cluster (or managed) with **tenant_id** payload filter mandatory on every search. |
| **Embed worker** | Async consumer that embeds task/scaffold and upserts vectors; horizontally scalable. |
| **Session affinity** | Avoid relying on it; prefer shared SoT + shared Qdrant + shared token store. |
| **Throughput** | Queries/sec and embeds/sec; measure p50/p99 at the **search** and **finalize** seams. |
| **Backpressure** | Reject or queue writes when embed lag / Qdrant lag exceeds budget. |

## Seams that matter for scale

1. **TrajectoryStore** port — swap SQLite → Postgres without rewriting MCP tools.
2. **VectorIndex** port — Qdrant client with connection pooling; replica-safe point ids (`uuid5`).
3. **Embedder** port — batch embeddings, timeout, circuit breaker.
4. **TokenStore** — must live in SoT (already SQLite-adjacent); same DB as trajectories for single-node, co-located or Postgres for multi-node.
5. **Principal resolution** — cheap, cacheable; no per-request disk map reload of full keys.

## Non-goals (for agents)

- Treating Yggdrasil as a generic document KB.
- Per-owner isolation inside `lab` (product decision: shared org memory).
- Funneling Qdrant ports publicly.

## Related docs

- `docs/superpowers/specs/2026-06-25-yggdrasil-prod-control-plane.md`
- `docs/superpowers/specs/2026-06-25-threat-model-poc.md`
- `docs/adr/` (add ADRs as we decide Postgres, queue, replica topology)
