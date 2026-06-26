# Multi-Tenant MCP AuthZ — Follow-on Plan (STUB)

> **Not for implementation in the code-review remediation plan.** Spawn a full superpowers:writing-plans pass when promoting Yggdrasil beyond trusted single-operator PoC.

**Goal:** Make org-wide trajectory memory safe for multiple humans/agents with non-spoofable ownership and scoped retrieval.

## Blockers from security review (must address)

1. **MCP authentication** — bind tool calls to a principal (mTLS, gateway token, or host-injected identity); reject anonymous stdio on shared hosts.
2. **Authorization** — `owner` / `team` from principal, not client-supplied `external_refs` alone; prevent spoofing `experience_grade`.
3. **Data isolation** — SQLite RLS-equivalent filters on every read path; Qdrant payload filters mandatory + API key; no global collection for hostile tenants.
4. **Network** — Qdrant not published without auth; bind localhost or private network.
5. **Content policy** — `YGG_SCRUB_CONTENT` default on; audit retention; secret scanning on finalize.
6. **Rate limits / audit log** — who read which trajectory.
7. **Artifact path policy** — allowlist workspaces; no arbitrary path disclosure.

## Suggested future waves

- Wave 1: Principal injection at MCP host + server-side owner binding
- Wave 2: Store + search forced filters
- Wave 3: Qdrant multi-collection or tenant payload + API keys
- Wave 4: Default-on scrubbing + secrets CI checks
- Wave 5: Compliance / retention

## Entry criteria

Remediation plan waves A–D and F1 complete; threat model doc published.

