---
name: yggdrasil-trajectory-memory
description: >
  Org-wide agent experience memory (not doc RAG). Self-contained skill: includes MCP
  client config patterns plus search/write policies. Before uncertain or high-overhead
  work, search_strategies; surface hit owner/agent_id for human follow-up. Write
  trajectories via start_trajectory, append_step, finalize_trajectory, get_trajectory,
  update_trajectory_meta. Prefer the personalized lab skill.md from the control plane
  (token embedded); this file uses placeholders for self-hosted setups.
---

# Yggdrasil Trajectory Experience Memory

**One-paste onboarding:** This skill is meant to be the **only** document you give an agent.
Wire MCP (section below), then follow the policies. If you downloaded a personalized
`skill.md` from the Yggdrasil lab/demo UI, prefer that file — it has your live bearer token
and URLs already filled in (no separate `mcp.json`).

## Wire MCP (self-hosted / placeholders)

Replace `YGG_PUBLIC_BASE`, `YGG_MCP_URL`, and `YGG_BEARER_TOKEN` with values from your
operator (or lab UI). Never put lab `sk-` API keys into agent MCP configs.

### Cursor / VS Code (`mcpServers`)

```json
{
  "mcpServers": {
    "yggdrasil": {
      "url": "YGG_MCP_URL",
      "headers": {
        "Authorization": "Bearer YGG_BEARER_TOKEN"
      }
    }
  }
}
```

Typical remote URL shape: `https://<host>/ygg/mcp` or `http://127.0.0.1:8080/mcp` depending on
funnel / `public_base_url`.

### HTTP remote MCP (equivalent)

```json
{
  "mcpServers": {
    "yggdrasil": {
      "type": "http",
      "url": "YGG_MCP_URL",
      "headers": {
        "Authorization": "Bearer YGG_BEARER_TOKEN"
      }
    }
  }
}
```

### Stdio MCP

```bash
export YGG_MCP_TOKEN='YGG_BEARER_TOKEN'
# also configure SQLite/Qdrant/embed via .env on the server host
python -m yggdrasil.mcp
```

```json
{
  "mcpServers": {
    "yggdrasil": {
      "command": "python",
      "args": ["-m", "yggdrasil.mcp"],
      "env": {
        "YGG_MCP_TOKEN": "YGG_BEARER_TOKEN"
      }
    }
  }
}
```

If remote HTTP MCP returns **501**, use stdio with `YGG_MCP_TOKEN`. After tools appear, continue below.

---


You are using **Yggdrasil**, an MCP server that stores and retrieves **agent trajectories**
(task + scaffold + steps + progress + outcome + concrete effort). It is **not** document
RAG. Think of it as higher-level Q/K/V strategy memory:

| Role | What it is in Yggdrasil |
|------|-------------------------|
| **Query (Q)** | Embedding of the **current** task instruction and/or scaffold state |
| **Keys (K)** | Past experiences indexed from their task + scaffold aspects (agents feed the DB) |
| **Values (V)** | Progress reached, outcome summaries, concrete effort totals; full step traces on demand via `get_trajectory` |

PoC stack: Python MCP tools → services → SQLite (canonical trajectories/steps/effort) + Qdrant (named vectors `task` + `scaffold`, filter payload). Domain default: `coding`.

**Core rule:** pass **current task + current scaffold only** into embed-facing fields. Do **not** dump full retry/failure transcripts into `task` / `scaffold` / `task_text` / `scaffold_text`. Prior attempts live as **steps** and are retrieved via `get_trajectory` after you shortlist candidates.

---

## Org-wide experience discovery (search before expensive / uncertain work)

**Default reflex:** Before starting work that is **uncertain to succeed** or that implies **large research / setup / infra / integration overhead**, consult Yggdrasil first. Other agents (and the people who ran them) elsewhere in the organization may already have tried similar goals—successes, failures, partials, and deliverables included.

Conceptually this is **`search_experience`** (via MCP `search_strategies`) plus inspecting **`artifacts`** on hits (and step payloads / `get_trajectory` for paths/URLs). PoC tool names are below; the *intent* is experience + artifact discovery, not document RAG.

| Trigger (search first) | Why |
|------------------------|-----|
| Unclear if the approach will work | Prior outcomes (success / fail / partial / `remaining_work`) de-risk the plan |
| High research or setup overhead | Reuse someone else's path, notes, scripts, and anti-patterns instead of re-deriving from scratch |
| Likely another agent already touched this area | Avoid duplicate token spend and agents stepping on the same problem |
| Need concrete deliverables from prior runs | Hits carry `artifacts` (md/code/url/log/data) and `workspace` when writers recorded them |
| Need a human to follow up with | Hits carry **`owner`** (person) and **`agent_id`** (which agent run)—surface these to the user |

### What to do with hits (agent + human)

1. **Learn from outcomes** — prefer low `failure_waste_seconds` / successful or clearly documented partials; treat high-waste fails as warnings, not templates.
2. **Learn from strategy** — `get_trajectory` on 1–3 shortlisted ids for step-level method (not blind copy-paste).
3. **Locate deliverables** — read hit `artifacts[]` and `workspace`; drill into steps if needed.
4. **Name the people** — always report **`owner`** (and `agent_id` / `team` when present) so the **user can follow up in person** (or chat/email) with the engineer whose agent produced the trajectory. Do not assume the prior agent is still running; the human owner is the handoff point.
5. **Then proceed** — only after this pass (or an empty trusted result set) start or continue your own trajectory and write back for the next agent.

Search does **not** require an open trajectory. Empty gated results mean “no trusted prior experience”—proceed normally; do not treat unfiltered archive noise as success.

### Trust gates: `search_mode` and `experience_grade`

| Mode | Intent | Provenance excludes (`hydration_test`, `external_pre_embed`, `not_author_segmented`) | Lexical overlap |
|------|--------|--------------------------------------------------------------------------------------|-----------------|
| **`agent`** (default) | Strict strategy recall for the current agent | Always excluded unless you opt into archive via explicit `tags_any` | Weak min token overlap on |
| **`lab`** | Org-wide / team forensics; prefers `experience_grade` authored memory | **Same excludes** — lab is not a back door into hydration corpus | Overlap relaxed (often off) |

**Critical:** `experience_grade=true` (tag or `external_refs`) marks *author-segmented* team memory. It **does not** launder trust for trajectories that also carry hydration/archive tags. A graded pre-embed row is still gated out in both modes. Empty gated results are correct when only archive noise matched.

---

## Core policies (critical)

- **Embed only current task + scaffold.** Never put full retry history or logs into `task_text`/`scaffold_text`. History lives in `append_step` rows; retrieve via `get_trajectory` after search shortlisting.
- **Always surface owners.** Every useful `search_strategies` hit must report `owner` (and `agent_id`/`team`) so the user can follow up with the human.
- **Segment long sessions.** For multi-goal work, build a caller segment list first (each segment gets its own `start_trajectory` + finalize). Do not embed one giant trajectory.
- **Prefer low waste.** Use `prefer_low_waste: true` and inspect `failure_waste_seconds` + outcome.
- **Partial is valuable.** Open/partial trajectories are searchable and tell future agents "this got at least this far".

**Re-embed triggers** (on append or meta): only `scaffold_update`, `task_update`, or `is_checkpoint`. Ordinary steps do not re-embed.

## When to search vs write

**Search first** (with current task/scaffold only) before uncertain or high-overhead work:
- Use `search_strategies(..., search_mode="lab")` for org discovery.
- Skim `outcome`, `effort.totals`, `owner`, `artifacts`, `progress.remaining_work`.
- `get_trajectory` only on 1-3 shortlist candidates.

**Write** when the run is worth preserving:
- `start_trajectory` (current task + scaffold + `external_refs` with owner/agent/team + artifacts)
- `append_step` (meaningful actions; use `kind`, `summary`, optional `effort_delta`)
- `finalize_trajectory` with honest `outcome.terminal_status` (`success|fail|partial|aborted`) + concrete `effort`

## MCP tools (essentials)

All tools carry the guidance: search before uncertain work; surface owners; write owner/artifacts; prefer low waste.

- **`search_strategies`** (primary): `task` (current only), `scaffold`, `search_mode="lab"|"agent"`, `limit`, `prefer_low_waste`, `include_open`, filters. Returns hits with summaries + owner + artifacts (no full steps).
- **`start_trajectory`**: `task_text`, `scaffold_text`, `tags?`, `external_refs{owner, agent_id, team, workspace, experience_grade?}`, `artifacts?`.
- **`append_step`**: `trajectory_id`, `kind` (thought|tool_call|...|checkpoint|note), `summary`, `payload?`, `scaffold_update?` (re-embeds), `effort_delta?`, `mark_partial?`.
- **`finalize_trajectory`**: `trajectory_id`, `outcome{terminal_status, summary, goal_satisfied?, remaining_work?}`, `effort?` (totals + line items strongly recommended).
- **`get_trajectory`**: `trajectory_id`, `include_steps=true` — use only after shortlist.
- **`update_trajectory_meta`**: patch tags/refs/artifacts/task/scaffold (re-embeds on aspect change). Pre-finalize only.

Returns and errors are plain dicts.

## Minimal usage patterns

**Search first (cold start):**
```json
search_strategies({
  "task": "slurm setup problems on gpu nodes",
  "scaffold": "lab cluster, sbatch, current python env",
  "search_mode": "lab",
  "prefer_low_waste": true,
  "limit": 5
})
// Report owners from hits to user. Optionally get_trajectory on best 1-2.
```

**Record a session:**
```json
tid = start_trajectory({
  "task_text": "...",
  "scaffold_text": "...",
  "external_refs": {"owner": "alice", "agent_id": "alice-7", "team": "infra"}
}).id

append_step({ "trajectory_id": tid, "kind": "command", "summary": "sbatch ...", "effort_delta": {...} })

finalize_trajectory({
  "trajectory_id": tid,
  "outcome": {"terminal_status": "success", "summary": "..."},
  "effort": {"totals": {"wall_clock_seconds": 1234, "failure_waste_seconds": 90}}
})
```

For long work, segment first into `{start_idx, end_idx, task, outcome}` list and write one trajectory per child segment (tag `session_segment`).

## Quick checklist

1. Uncertain/high-overhead? → `search_strategies` (current task/scaffold only).
2. Good hits? → name the `owner` for the user.
3. Will this help others? → write with `owner` + `artifacts`.
4. Long session? → segment before writing.
5. Done? → finalize with real `outcome` + concrete effort numbers.

Server setup: README + `.env.example`. Personalized tokens: lab UI **Download skill.md** (MCP config is embedded). Do not commit secrets or `user_mapping.yaml`.
