---
name: yggdrasil-trajectory-memory
description: >
  Org-wide agent experience memory for the lab tenant. Search before uncertain or
  high-overhead work; write trajectories with owner attribution. This file is
  self-contained: MCP client config + usage policies in one paste.
---

# Yggdrasil Trajectory Experience Memory (Lab)

You are using **Yggdrasil** for tenant **`lab`** as owner **`arthur`**.
This single document is enough to connect and operate — no separate `mcp.json` is required.

## 1. Wire MCP (do this first)

**Important:** `codex mcp add` / Cursor MCP config is **client-side** (runs on the machine where
Codex/Cursor lives). Remote Codex should use Streamable HTTP at `https://arcyleung-ubuntu.tailb940e6.ts.net/ygg/mcp` with Bearer `ygg_…` (this control plane).
Stdio (`python -m yggdrasil.mcp`) remains available for local/dev agents on the data-plane host.

Use the **issued bearer token** only — never paste lab `sk-` API keys.

### Codex: remote HTTP MCP (Tailscale) — preferred

```bash
codex mcp remove yggdrasil 2>/dev/null || true
codex mcp add yggdrasil --url 'https://arcyleung-ubuntu.tailb940e6.ts.net/ygg/mcp' --\
  header "Authorization: Bearer ygg_YOUR_TOKEN_FROM_LAB_LOGIN"
```

Or in MCP JSON:

```json
{
  "mcpServers": {
    "yggdrasil": {
      "url": "https://arcyleung-ubuntu.tailb940e6.ts.net/ygg/mcp",
      "headers": {
        "Authorization": "Bearer ygg_YOUR_TOKEN_FROM_LAB_LOGIN"
      }
    }
  }
}
```

### Connection facts

| Field | Value |
|-------|--------|
| **Public base URL** | `https://arcyleung-ubuntu.tailb940e6.ts.net/ygg` |
| **MCP HTTP URL** | `https://arcyleung-ubuntu.tailb940e6.ts.net/ygg/mcp` (Streamable HTTP; Bearer required) |
| **Bearer token** | `ygg_YOUR_TOKEN_FROM_LAB_LOGIN` |
| **Tenant** | `lab` (org-wide shared retrieval) |
| **Owner (writes bind here)** | `arthur` |

After tools appear (`search_strategies`, `start_trajectory`, …), follow the policies below.

---

## 2. What Yggdrasil is

Org-wide **agent experience memory** (not document RAG). Lab users share **one retrieval scope** (`tenant_id=lab`). Writes bind owner **`arthur`** server-side.

---

## 3. Org-wide experience discovery

**Default reflex:** Before uncertain or high-overhead work, call `search_strategies` first with **`search_mode=lab`**, **`limit` ≥ 20**.

### Structured search results (required)

1. Call `search_strategies` with focused `task` + `scaffold` keywords.
2. Read **`ranked`** and **`by_range`** (success → recency → relevance → low waste).
3. Show a **markdown table**: range | owner | agent_id | outcome | trajectory_id | task preview | score.
4. Name **owners** for human follow-up; `get_trajectory` on 1–3 rows only.

**Search template:**
```json
search_strategies({
  "task": "kubernetes networking CNI cluster network working",
  "scaffold": "lab k8s infra calico cilium",
  "search_mode": "lab",
  "prefer_low_waste": true,
  "limit": 25
})
```

### Trust gates

| Mode | Intent |
|------|--------|
| **`agent`** | Strict strategy recall; excludes hydration/archive tags |
| **`lab`** (prefer for org questions) | Wide org recall; archive tags still excluded |

---

## 4. Core policies

- Embed only **current** task + scaffold.
- Always surface **owners**.
- Segment long multi-goal sessions.
- Prefer low waste; partial is valuable.

## 5. MCP tools

- `search_strategies` — primary; returns `hits`, `ranked`, `by_range`
- `start_trajectory` / `append_step` / `finalize_trajectory` / `get_trajectory` / `update_trajectory_meta`

## 6. Quick checklist

1. MCP wired? Tools visible?
2. Uncertain / who-knows-X? → `search_strategies` lab, limit≥20.
3. Table from `ranked`; name owners.
4. Write non-trivial runs (owner=arthur server-side).
5. Finalize with real outcome + effort.
