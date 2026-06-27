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
Codex/Cursor lives). It starts a **local** MCP process or opens an HTTP client to a remote MCP
server. Remote Codex should use Streamable HTTP at `https://arcyleung-ubuntu.tailb940e6.ts.net/ygg/mcp` with Bearer `ygg_…` (this control plane).
Stdio (`python -m yggdrasil.mcp`) remains available for local/dev agents on the data-plane host.

Use the **issued bearer token** only — never paste lab `sk-` API keys.

### Codex: remote HTTP MCP (Tailscale) — preferred

```bash
codex mcp remove yggdrasil 2>/dev/null || true
codex mcp add yggdrasil --url 'https://arcyleung-ubuntu.tailb940e6.ts.net/ygg/mcp' --\
  header "Authorization: Bearer ygg_YOUR_TOKEN_FROM_LAB_LOGIN"
# if your Codex build uses env for headers, set CODEX_* / config equivalently
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

### Codex fallback: stdio MCP on a host with DB access

From a checkout of Yggdrasil (or an install where `python -m yggdrasil.mcp` works), with access to
the lab SQLite/Qdrant/embed stack **or** a remote API if you proxy later:

```bash
# Client-side (your laptop / agent host) — NOT something the Funnel /mcp URL does yet
export YGG_MCP_TOKEN='ygg_YOUR_TOKEN_FROM_LAB_LOGIN'
# Also load normal Yggdrasil .env on that host (SQLITE, QDRANT, EMBED_*), or point at shared infra
cd /path/to/yggdrasil && export PYTHONPATH=src

codex mcp remove yggdrasil 2>/dev/null || true
codex mcp add yggdrasil --env YGG_MCP_TOKEN="ygg_YOUR_TOKEN_FROM_LAB_LOGIN" -- \
  python -m yggdrasil.mcp
```

Equivalent `~/.codex/config.toml` / MCP servers entry (shape may vary by Codex version):

```json
{
  "mcpServers": {
    "yggdrasil": {
      "command": "python",
      "args": ["-m", "yggdrasil.mcp"],
      "env": {
        "YGG_MCP_TOKEN": "ygg_YOUR_TOKEN_FROM_LAB_LOGIN",
        "PYTHONPATH": "src"
      },
      "cwd": "/path/to/yggdrasil"
    }
  }
}
```

**Preferred for remote Codex (Tailscale Streamable HTTP):**

```json
{ "url": "https://arcyleung-ubuntu.tailb940e6.ts.net/ygg/mcp", "headers": { "Authorization": "Bearer …" } }
```

Requires `Authorization: Bearer` with your issued `ygg_…` token. If initialize fails, fall back to stdio below.

### Cursor / VS Code (`mcpServers`) — same stdio pattern

```json
{
  "mcpServers": {
    "yggdrasil": {
      "command": "python",
      "args": ["-m", "yggdrasil.mcp"],
      "env": {
        "YGG_MCP_TOKEN": "ygg_YOUR_TOKEN_FROM_LAB_LOGIN",
        "PYTHONPATH": "src"
      }
    }
  }
}
```

### Connection facts

| Field | Value |
|-------|--------|
| **Public base URL** (UI / skill download) | `https://arcyleung-ubuntu.tailb940e6.ts.net/ygg` |
| **MCP HTTP URL** (Streamable HTTP) | `https://arcyleung-ubuntu.tailb940e6.ts.net/ygg/mcp` — requires `Authorization: Bearer ygg_…` |
| **Bearer token** (`YGG_MCP_TOKEN`) | `ygg_YOUR_TOKEN_FROM_LAB_LOGIN` |
| **Tenant** | `lab` (org-wide shared retrieval) |
| **Owner (writes bind here)** | `arthur` |
| **Where `codex mcp add` runs** | **Client** (user’s Codex), spawning stdio server locally |

After tools appear (`search_strategies`, `start_trajectory`, …), follow the policies below.

---

## 2. What Yggdrasil is

Org-wide **agent experience memory** (not document RAG). Trajectories store task + scaffold + steps +
progress + outcome + effort. Think Q/K/V strategy memory: query = current task/scaffold; keys = past
experiences; values = outcomes, effort, artifacts; full traces via `get_trajectory`.

Lab users share **one retrieval scope** (`tenant_id=lab`). Search returns experiences from **all lab owners**.
Writes still bind **your** owner (`arthur`) server-side — clients cannot spoof owner or tenant.

---

## 3. Org-wide experience discovery

**Default reflex:** Before work that is **uncertain to succeed** or has **large research / setup / infra
overhead**, call `search_strategies` first. Other agents (and their human owners) may already have tried
similar goals.

| Trigger (search first) | Why |
|------------------------|-----|
| Unclear if the approach will work | Prior outcomes de-risk the plan |
| High research or setup overhead | Reuse paths, scripts, anti-patterns |
| Likely another agent touched this area | Avoid duplicate spend |
| Need deliverables from prior runs | Hits carry `artifacts` / `workspace` |
| Need a human to follow up with | Hits carry **`owner`** and **`agent_id`** |

### What to do with hits

1. Prefer low `failure_waste_seconds` / successful or documented partials.
2. `get_trajectory` on 1–3 shortlisted ids for step-level method.
3. Read `artifacts[]` and `workspace`.
4. **Always name `owner` (and `agent_id` / `team`)** so the user can follow up in person.
5. Then proceed; write back non-trivial runs for the next agent.

### Trust gates

| Mode | Intent |
|------|--------|
| **`agent`** (default) | Strict strategy recall; excludes hydration/archive provenance tags |
| **`lab`** | Org / team forensics; prefers `experience_grade` authored memory; **same** archive excludes |

`experience_grade` does **not** launder archive/hydration trajectories. Empty gated results mean “no trusted prior experience”.

---

## 4. Core policies (critical)

- **Embed only current task + scaffold.** Never dump full retry/failure transcripts into `task` /
  `scaffold` / `task_text` / `scaffold_text`. History lives in `append_step`; retrieve via `get_trajectory`.
- **Always surface owners** on useful search hits.
- **Segment long sessions** — multi-goal work → one trajectory per segment (tag `session_segment`).
- **Prefer low waste** — `prefer_low_waste: true`; inspect `failure_waste_seconds`.
- **Partial is valuable** — open/partial trajectories are searchable.

**Re-embed triggers** on append/meta: only `scaffold_update`, `task_update`, or `is_checkpoint`.

---

## 5. MCP tools (essentials)

- **`search_strategies`**: `task`, `scaffold`, `search_mode` (`lab`|`agent`), `limit`, `prefer_low_waste`, filters.
- **`start_trajectory`**: `task_text`, `scaffold_text`, `external_refs` (owner forced server-side to `arthur`), `artifacts?`.
- **`append_step`**: `trajectory_id`, `kind`, `summary`, `payload?`, `scaffold_update?`, `effort_delta?`.
- **`finalize_trajectory`**: `trajectory_id`, `outcome{terminal_status, summary, …}`, `effort?`.
- **`get_trajectory`**: after shortlist only (`include_steps=true`).
- **`update_trajectory_meta`**: tags/refs/artifacts/task/scaffold (pre-finalize).

### Minimal patterns

**Search first:**
```json
search_strategies({
  "task": "slurm setup problems on gpu nodes",
  "scaffold": "lab cluster, sbatch, current python env",
  "search_mode": "lab",
  "prefer_low_waste": true,
  "limit": 25
})
// Present ranked[] with ISO `at`; bin client_range (today/week/month/older) yourself.
// Table: at | client_range | owner | outcome | trajectory | score.
```

**Record a session:**
```json
tid = start_trajectory({
  "task_text": "...",
  "scaffold_text": "...",
  "external_refs": {"agent_id": "arthur-agent", "team": "lab"}
}).id
append_step({ "trajectory_id": tid, "kind": "command", "summary": "..." })
finalize_trajectory({
  "trajectory_id": tid,
  "outcome": {"terminal_status": "success", "summary": "..."},
  "effort": {"totals": {"wall_clock_seconds": 1234, "failure_waste_seconds": 90}}
})
```

---


## Structured search results (agents must present this way)

Org questions (“who set up X / who got Y working?”):

1. `search_strategies` with `search_mode="lab"`, `limit` ≥ 20, focused `task` + `scaffold` keywords.
2. Read **`ranked`** (success → **event-time** recency → relevance → low waste). Each row has ISO **`at`** (experience time: prefer finalized/occurred — **not** re-index `updated_at` / `indexed_at`). Do **not** expect server `range` or `by_range`.
3. **Client-side bins** from `ranked[].at` vs agent clock (UTC): `today` ≤1d, `week` ≤7d, `month` ≤30d, else `older`; missing `at` → `unknown`.
4. Show a **markdown table**: at (ISO) | client_range | owner | agent_id | outcome | trajectory_id | task preview | score.
5. Example API row: `{"at":"2026-02-27T14:45:47.977000Z","owner":"fenglin","outcome":"partial","trajectory_id":"…"}` → you add `client_range` e.g. `older`.
6. Name **owners** for human follow-up; `get_trajectory` only on 1–3 chosen rows.

## 6. Quick checklist

1. MCP wired from section 1? Tools visible?
2. Uncertain/high-overhead? → `search_strategies` (current task/scaffold only, prefer `search_mode=lab, limit≥20`).
3. Good hits? → table with **`at`** + client_range; name the **`owner`**.
4. Worth preserving? → write trajectory (owner bound to **`arthur`**).
5. Long session? → segment before writing.
6. Done? → finalize with real `outcome` + concrete effort.
