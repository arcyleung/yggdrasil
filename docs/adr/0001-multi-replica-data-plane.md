# ADR-0001: Multi-replica requires shared SoT (not local SQLite)

## Status

Proposed (direction for scaling work; not fully implemented).

## Context

Yggdrasil PoC uses SQLite + local Qdrant client for a single-node trusted deployment. Product goals include multiple UI/MCP replicas and high query throughput.

## Decision

When running **more than one replica**:

1. **TrajectoryStore + TokenStore** must use a **networked SoT** (Postgres recommended) — not a file SQLite on each pod.
2. **Qdrant** is a shared cluster (or single service with auth); every search **must** filter `tenant_id`.
3. **Embedding** is **async** via a queue/outbox so `finalize_trajectory` is not coupled to embed latency (backpressure on queue depth).
4. App replicas stay **stateless** aside from in-process caches with TTLs.

Single-node PoC may keep SQLite until migration.

## Consequences

- Deeper **TrajectoryStore** module (port already exists) — Postgres adapter is the main seam.
- Hydration workers become embed-queue producers/consumers.
- UI and MCP scale horizontally behind Funnel/LB.

## Alternatives considered

- SQLite on NFS — rejected (locking, corruption risk).
- Session affinity to one writer — rejected (ops complexity, uneven load).
