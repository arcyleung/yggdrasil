# Yggdrasil PoC Threat Model (2026-06-25)

Short security posture for the **single-operator trusted PoC**. This is not a multi-tenant production security design.

## In scope

- **Single operator** runs MCP server, SQLite, and Qdrant on a **local or private network**.
- **Trusted MCP clients and agents** — tool callers are assumed cooperative (not adversarial).
- **Local trust** for stored trajectory content: agents may write task/scaffold/steps that include operational text from their environment.
- **Attribution fields** (`owner`, `agent_id`, `team`, `workspace`) for human handoff — not cryptographic identity binding.
- Optional **best-effort** content scrubbing via `YGG_SCRUB_CONTENT=1` (regex: emails, `sk-…`, `Bearer …`; preserve allowlisted owner names). Default is **off**.

## Out of scope (explicit non-goals for PoC)

- **Hostile multi-tenant** isolation (tenant A reading tenant B trajectories).
- **Malicious agents** spoofing `owner` / `experience_grade` / tags to launder trust.
- **Qdrant (or SQLite) on the public internet** without auth, network policy, or TLS termination you control.
- Formal **compliance** (GDPR processor guarantees, SOC2 evidence, differential privacy).
- LLM-based PII redaction pipelines (Clio-style) — survey only unless separately implemented.

## Residual risks operators accept

1. Trajectories **may contain secrets and PII** from agent context; `owner` is **not** proof of scrubbing.
2. Vector index payload can leak task/scaffold snippets in search hits.
3. MCP tools have **no principal authentication** in this PoC — whoever can invoke the server can read/write memory.
4. Dual-store (SQLite SoT + Qdrant) can diverge under embed failure; `index_status` mitigates search visibility but not offline payload copies.

## Blockers before multi-user / shared-host deployment

Track implementation in [`../plans/2026-06-25-multi-tenant-authz-FOLLOWON.md`](../plans/2026-06-25-multi-tenant-authz-FOLLOWON.md):

1. MCP **authentication** (principal bound to each tool call).
2. **Authorization** — server-side `owner`/`team` from principal; prevent spoofing `experience_grade`.
3. Store + Qdrant **forced filters** / RLS-equivalent on every read path.
4. Qdrant **API key** + network policy (do not publish 6333/6334 without auth).
5. Content scrubbing **default-on** + retention policy.
6. Rate limits and **audit log** of trajectory access.
7. Artifact path allowlists.

## Operator checklist (PoC hygiene)

- [ ] Do not commit `.env`, `mongo_creds.txt`, `user_mapping.yaml`.
- [ ] Bind Qdrant to localhost or private net; set `QDRANT_API_KEY` if ports leave the host.
- [ ] Prefer `search_mode=lab` / agent gates; do not treat hydration tags as trusted experience.
- [ ] Enable `YGG_SCRUB_CONTENT=1` if trajectories leave the trusted operator boundary (still not Clio-grade).
- [ ] Re-evaluate this doc before any shared multi-user rollout.
