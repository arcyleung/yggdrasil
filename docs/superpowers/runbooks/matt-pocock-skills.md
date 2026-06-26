# Matt Pocock skills (installed for Grok)

Source: https://github.com/mattpocock/skills.git  
Install path: `~/.grok/skills/mattpocock/<skill-name>/`

## Recommended flow for multi-replica / high-throughput architecture

1. **`/grill-with-docs` or grill-me** — align on SLOs (qps, p99 search, embed lag), replica count, HA requirements.
2. **`decision-mapping`** — tickets for: Postgres SoT, embed outbox, Qdrant HA, connection pools, cache tier.
3. **`improve-codebase-architecture`** — HTML report of deepening opportunities (store, search, embed, auth).
4. **`codebase-design`** — design-it-twice for TrajectoryStore / SearchService interfaces under load.
5. **`to-prd` → `to-issues`** — vertical slices (tracer bullets) on GitHub with `ready-for-agent`.
6. **`prototype`** — throwaway queue consumer or pg store smoke before full migrate.
7. **`implement` + `tdd`** — implement at ports/seams only.
8. **`diagnosing-bugs`** — when p99 regresses after scale changes.
9. **`handoff`** — pass state between agent sessions.

Repo config: `AGENTS.md`, `CONTEXT.md`, `docs/agents/*`, `docs/adr/0001-multi-replica-data-plane.md`.
