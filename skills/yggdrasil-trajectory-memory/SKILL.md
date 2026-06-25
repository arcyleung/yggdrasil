---
name: yggdrasil-trajectory-memory
description: >
  Use the Yggdrasil MCP trajectory experience memory whenever you might repeat
  hard work, after finishing a coding session worth preserving, or mid-task when
  you need strategy/progress recall from prior similar attempts. Activate before
  searching or writing trajectories via start_trajectory, append_step,
  finalize_trajectory, search_strategies, get_trajectory, or update_trajectory_meta.
  Not for generic document RAG — only Q/K/V experience trajectories.
---

# Yggdrasil Trajectory Experience Memory

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

## When to search vs when to write

### Search (`search_strategies`) — read-only recall

Use when you want candidates **without** necessarily starting a session:

| Situation | Why search |
|-----------|------------|
| **Cold start** | Before repeating hard work on a task that might have been done before |
| **Mid-task stuck / pivot** | Recall strategies that reached a similar scaffold/progress without polluting the index with your failure log |
| **Cost / predictability** | Compare concrete effort totals and `runtime_fingerprint` to the user's hardware/budget/deadline |
| **Avoid rediscovery** | Similar `task` or `scaffold` may already encode a workable path |

Search does **not** require an open trajectory. You can search, then optionally `start_trajectory` only if you will record this session.

### Write (`start_trajectory` → `append_step` → `finalize_trajectory`)

Use when the session is worth preserving for future agents (or future you):

| Situation | Why write |
|-----------|-----------|
| **Starting a non-trivial coding session** | Open a trajectory so progress and strategy accumulate in the world tree |
| **Meaningful progress / checkpoints** | Append steps; update scaffold on real state changes; mark `partial` if stopping mid-way |
| **Terminal outcome** | Finalize with outcome + best-available concrete effort (success, fail, partial, aborted) |
| **Metadata drift** | `update_trajectory_meta` for tags/task/scaffold/refs **before** finalize |

**Partial/open sessions are valuable.** An unfinalized or `partial` trajectory still answers: *“this strategy gets at least this far”* (`progress`, optional `remaining_work`). Default search includes open/partial (`include_open` defaults true server-side). Do not treat open/partial as automatic failure.

---

## MCP tools (exact names and key params)

All tools share this agent guidance from the server:

> Record agent work as trajectories with **concrete effort totals** (no cheap/expensive bands). Use `start_trajectory` at session start, `append_step` for progress (re-embeds only on task/scaffold/checkpoint changes), `finalize_trajectory` on terminal outcome, `search_strategies` before repeating hard work (`include_open=true` by default for partials), `get_trajectory` for full detail, `update_trajectory_meta` for tags/task/scaffold patches. Interpret effort numbers in **user context**; prefer low `failure_waste_seconds` when relevant.

### 1. `start_trajectory` — open a session and index task+scaffold

| Param | Required | Notes |
|-------|----------|-------|
| `task_text` | yes | Current goal/instruction only (not full chat history) |
| `scaffold_text` | yes | Current environment/repo/state summary; may be weak but recommended |
| `domain` | no | Default `"coding"` |
| `tags` | no | Filterable labels |
| `runtime_fingerprint` | no | OS/arch, GPU/RAM, containerized, etc. — stored for comparability, **not** embedded in PoC |
| `external_refs` | no | Lineage (`mongo`, `postgres`, issue ids, …) |
| `embed_view_version` | no | Default `"coding_v1"` |

Returns trajectory id, `status=open`, index state. Embeds **task + scaffold** once at start.

### 2. `append_step` — record progress on an open/partial trajectory

| Param | Required | Notes |
|-------|----------|-------|
| `trajectory_id` | yes | From `start_trajectory` |
| `kind` | yes | PoC kinds: `thought`, `tool_call`, `tool_result`, `edit`, `command`, `checkpoint`, `note`, `other` |
| `summary` | yes | Short human-readable step summary |
| `payload` | no | Structured JSON detail |
| `scaffold_update` | no | New scaffold text when state actually changed — **triggers re-embed** |
| `task_update` | no | Refined task text — **triggers re-embed** |
| `is_checkpoint` | no | Checkpoint — **triggers re-embed** of affected aspects |
| `progress` | no | Denormalized progress (`phase`, `summary`, `steps_count`, `milestones`, `blockers`, …) |
| `mark_partial` | no | Mark trajectory `partial` (still searchable; “gets this far”) |
| `effort_delta` | no | Incremental concrete effort; server rolls up totals |

**Re-embed policy on append:** no re-embed for ordinary steps; re-embed only when `scaffold_update`, `task_update`, or `is_checkpoint` is set. Progress/effort scalars update without rewriting vectors.

Rejects appends after finalize (`trajectory_closed`).

### 3. `finalize_trajectory` — terminal outcome + effort close-out

| Param | Required | Notes |
|-------|----------|-------|
| `trajectory_id` | yes | |
| `outcome` | yes | Dict with at least `terminal_status` + `summary`; include `signals`, `goal_satisfied`, `remaining_work` when useful |
| `effort` | strongly recommended | Concrete totals / line items; estimates ok if noted |
| `runtime_fingerprint` | no | Final environment snapshot |
| `progress` | no | Final progress snapshot |
| `task_text` / `scaffold_text` | no | Final aspect text; re-embeds only if passed |

`terminal_status` values in practice: `success` | `fail` | `partial` | `aborted` (plus trajectory `status` mirrors session lifecycle: `open` | `partial` | `success` | `fail` | `aborted`).

Further appends are rejected after finalize.

### 4. `search_strategies` — multi-aspect recall (summaries, not full steps)

| Param | Required | Notes |
|-------|----------|-------|
| `task` | at least one of task/scaffold | **Current** task only — no attempt history dump |
| `scaffold` | at least one of task/scaffold | **Current** scaffold only |
| `domain` | no | Default `"coding"` |
| `status_in` | no | Filter statuses |
| `include_open` | no | Default includes open/partial partials (server default true) |
| `tags_any` | no | Any-of tag filter |
| `limit` | no | Default 10 |
| `effort_predicates` | no | Only for explicit constraints: `{ field, op, value? / min? / max? }` |
| `runtime_filters` | no | Coarse env filters (e.g. gpu_model, min_ram_gb) |
| `prefer_low_waste` | no | Prefer low `failure_waste_seconds` when ranking/filtering |

Returns ranked hits with progress/outcome/effort **summaries** and concrete `effort.totals` scalars — **not** full step lists. PoC does **not** embed attempt history (`include_attempt_history_in_embed` is reserved/ignored).

`effort_predicates` ops: `eq` | `lt` | `lte` | `gt` | `gte` | `between` | `is_null` | `is_not_null`. Null effort is **inclusive by default** (`inclusive_null`) so sparse instrumentation does not empty results.

### 5. `get_trajectory` — full steps + effort line items

| Param | Required | Notes |
|-------|----------|-------|
| `trajectory_id` | yes | Shortlisted id from search |
| `include_steps` | no | Default `true` |

Use only for **shortlisted** trajectories before copying detailed steps into your plan. Hits from search are candidates; this tool is the deep read.

### 6. `update_trajectory_meta` — patch meta before finalize

| Param | Required | Notes |
|-------|----------|-------|
| `trajectory_id` | yes | |
| `tags` | no | Replace/patch tags |
| `task_text` | no | Patch; **re-embeds** if changed |
| `scaffold_text` | no | Patch; **re-embeds** if changed |
| `runtime_fingerprint` | no | |
| `external_refs` | no | |

PoC: reject if trajectory already finalized.

---

## Embed policy (critical)

**Default query/write policy:** embed **current task + current scaffold only**.

| Do | Don't |
|----|-------|
| Put the live instruction in `task` / `task_text` | Paste entire retry transcripts into embed fields |
| Put current repo/env/scaffold state in `scaffold` / `scaffold_text` | Embed “logs that look like my failures” as the key |
| Record attempts as `append_step` rows | Expect search to match on step payloads by default |
| Re-embed via `scaffold_update` / `task_update` / checkpoint when state truly changes | Re-embed on every trivial step |
| Use `get_trajectory` for historical step detail | Dump history into search `task`/`scaffold` “for better recall” |

**Why:** embedding prior attempts biases retrieval toward failure-log similarity, not strategy similarity. Steps remain in SQLite for explicit get; they are not default embed input. History-in-embed is a future ablation flag, not PoC behavior.

**Aspects (PoC):** named vectors `task` and `scaffold` only. Runtime fingerprint is stored/filtered coarsely but **not** an embed aspect. `embed_view_version` (e.g. `coding_v1`) selects deterministic text templates.

---

## Effort: concrete totals only

Yggdrasil stores **multi-dimensional concrete numbers**, not server-side “cheap/expensive” or order-of-magnitude bands. **You** interpret totals in the **user’s** context (hardware, budget, deadline, tolerance for waste).

### Effort totals (canonical scalars)

Examples of fields you may pass or see on hits:

- Time/compute: `wall_clock_seconds`, `cpu_seconds`, `gpu_seconds`, `gpu_memory_gb_hours`
- I/O: `network_bytes`, `disk_bytes_read`, `disk_bytes_write`
- Money/LLM: `llm_tokens_in`, `llm_tokens_out`, `llm_api_usd`, `cloud_usd`
- Human/waste: `human_wait_seconds`, `failure_waste_seconds`
- Extensibility: `custom` (JSON, e.g. `hf_dataset_gb`)

### Line items

On finalize (and optionally incremental deltas on append), prefer line items with `category`, `label`, `quantity`, `unit`, optional `amount_usd`, time bounds, step range, `was_wasted`, `metadata`. Note estimates in `effort.notes` when exact metering is unavailable.

### When effort is written

| Moment | Behavior |
|--------|----------|
| `start_trajectory` | Optional runtime fingerprint; empty/partial ledger ok |
| `append_step` | Optional `effort_delta`; totals recompute |
| `finalize_trajectory` | Close-out strongly recommended; set ended time; outcome + effort together |

### Agent interpretation rules

1. Read concrete `effort.totals` on search hits (e.g. 5400s wall, $145 API) — do not invent global band labels.
2. Map numbers to feasible/infeasible given **this user’s** machine and constraints.
3. Compare `runtime_fingerprint` on hits to the current environment before assuming transferability.
4. Use `effort_predicates` / `prefer_low_waste` only when the user or system states an explicit constraint (budget, max GPU time, minimize wasted retries).
5. On failure finalize, set `failure_waste_seconds` / mark wasted line items when known — improves future predictability.

---

## Partial / open sessions (“gets this far”)

- **`open`:** session in progress; still indexed and searchable by default.
- **`partial`:** explicitly marked incomplete via `mark_partial` or partial terminal outcome; progress/outcome may include `remaining_work`.
- **Value:** future agents learn that a strategy reached phase X / scaffold Y even without full success.
- **Guidance:** do not ignore open/partial hits; read `progress.summary`, `phase`, `blockers`, `remaining_work`. Use `get_trajectory` if the partial path looks relevant.
- **Finalize with `partial`:** valid when stopping intentionally with useful remaining work documented.

---

## Workflows

### A. Cold-start search → work → record

1. **`search_strategies`** with current `task` + `scaffold` only (`include_open` default/true).
2. Skim hits: `progress`, `outcome`, `effort.totals`, `runtime_fingerprint`, `status` (including open/partial).
3. **`get_trajectory`** only for 1–3 shortlisted ids; extract reusable steps/strategy, not blindly copy secrets.
4. **`start_trajectory`** with the same current task/scaffold (plus tags/runtime if useful).
5. Work the task; **`append_step`** for meaningful actions; `scaffold_update` / checkpoints when state changes; optional `effort_delta`.
6. **`finalize_trajectory`** with `outcome` + best-available `effort` (success/fail/partial/aborted).

### B. Mid-task search without history pollution

1. You are mid-session (optionally already have `trajectory_id`).
2. Build **current** task text and **current** scaffold snapshot only (latest working tree / error class / phase — not full tool log).
3. **`search_strategies(task=..., scaffold=...)`** — do not embed your retry history in these fields.
4. Optionally continue appending to your open trajectory; if scaffold advanced, pass `scaffold_update` or `update_trajectory_meta(scaffold_text=...)`.
5. If abandoning / pausing, `append_step(..., mark_partial=true)` or finalize `terminal_status=partial` with `remaining_work`.

### C. Finalize with outcome + effort

1. Decide terminal status honestly (`success` / `fail` / `partial` / `aborted`).
2. Summarize outcome; attach signals (tests passed, error_class, artifacts).
3. Attach concrete effort totals/line items; mark waste on failed/aborted paths when known.
4. Optional final `task_text` / `scaffold_text` if the goal/scaffold refined — only pass if you want a last re-embed.
5. Call **`finalize_trajectory`** once; no further appends.

### D. Meta-only correction (pre-finalize)

Use **`update_trajectory_meta`** to fix tags, refine task/scaffold without a step, or attach `external_refs` — not as a substitute for `append_step` progress.

---

## Example tool call sequences (pseudo-JSON)

### Cold start: search then maybe deep-read

```json
// 1) Recall similar strategies (current task/scaffold only)
search_strategies({
  "task": "Fix flaky pytest timeout in CI for async worker pool",
  "scaffold": "Python 3.12, pytest-asyncio, GitHub Actions ubuntu-latest, repo uses uv",
  "domain": "coding",
  "limit": 8,
  "prefer_low_waste": true
})

// 2) Deep-read one promising hit
get_trajectory({
  "trajectory_id": "<id-from-search>",
  "include_steps": true
})
```

### Full record lifecycle

```json
// 1) Open session
start_trajectory({
  "task_text": "Add MCP tool get_trajectory and wire FastMCP registration",
  "scaffold_text": "yggdrasil PoC; src/yggdrasil/mcp/tools.py; SQLite+Qdrant; tests/test_mcp_tools.py",
  "domain": "coding",
  "tags": ["mcp", "yggdrasil", "poc"],
  "runtime_fingerprint": {
    "os": "linux",
    "arch": "x86_64",
    "containerized": false,
    "ram_gb": 32
  }
})
// → trajectory_id = "traj-abc"

// 2) Progress steps (no re-embed unless scaffold/task/checkpoint)
append_step({
  "trajectory_id": "traj-abc",
  "kind": "edit",
  "summary": "Implemented get_trajectory tool handler calling session_service",
  "payload": {"files": ["src/yggdrasil/mcp/tools.py"]}
})

append_step({
  "trajectory_id": "traj-abc",
  "kind": "command",
  "summary": "pytest -q tests/test_mcp_tools.py passed",
  "payload": {"exit_code": 0},
  "progress": {
    "phase": "verify",
    "summary": "Unit tests green for get_trajectory",
    "steps_count": 2
  },
  "effort_delta": {
    "totals": {"wall_clock_seconds": 120, "llm_api_usd": 0.04}
  }
})

// 3) Scaffold advanced — checkpoint re-embeds
append_step({
  "trajectory_id": "traj-abc",
  "kind": "checkpoint",
  "summary": "Tool registered; serialization returns steps + effort line items",
  "is_checkpoint": true,
  "scaffold_update": "yggdrasil PoC; all six MCP tools registered; get_trajectory returns steps; pending finalize integration test"
})

// 4) Terminal success + effort close-out
finalize_trajectory({
  "trajectory_id": "traj-abc",
  "outcome": {
    "terminal_status": "success",
    "summary": "get_trajectory implemented and tested",
    "goal_satisfied": true,
    "signals": {"tests": "pass", "pytest_nodeids": ["tests/test_mcp_tools.py::test_get_trajectory"]}
  },
  "effort": {
    "totals": {
      "wall_clock_seconds": 2400,
      "llm_tokens_in": 45000,
      "llm_tokens_out": 12000,
      "llm_api_usd": 0.85,
      "failure_waste_seconds": 300
    },
    "notes": "failure_waste includes one wrong import path retry"
  }
})
```

### Mid-task search (no history in embed fields)

```json
// Current state only — NOT the full tool/error log
search_strategies({
  "task": "Qdrant collection dim mismatch on startup",
  "scaffold": "EMBED_DIM=1024, model Qwen3-Embedding-0.6B, collection yggdrasil_trajectories exists with dim 1536",
  "domain": "coding",
  "include_open": true,
  "effort_predicates": [
    {"field": "wall_clock_seconds", "op": "lte", "value": 7200}
  ]
})

// Continue your own open trajectory separately
append_step({
  "trajectory_id": "traj-current",
  "kind": "note",
  "summary": "Searched prior strategies for embed/collection dim mismatch",
  "mark_partial": false
})
```

### Pause with partial progress preserved

```json
append_step({
  "trajectory_id": "traj-abc",
  "kind": "note",
  "summary": "Stopping: blocked on Qdrant auth in prod-like env",
  "progress": {
    "phase": "blocked",
    "summary": "Local path works; remote Qdrant needs API key wiring",
    "blockers": ["QDRANT_API_KEY not set in deploy env"]
  },
  "mark_partial": true
})

// Or finalize as partial with remaining_work
finalize_trajectory({
  "trajectory_id": "traj-abc",
  "outcome": {
    "terminal_status": "partial",
    "summary": "Feature mostly done; deploy config incomplete",
    "goal_satisfied": false,
    "remaining_work": "Wire QDRANT_API_KEY and re-run integration smoke test"
  },
  "effort": {
    "totals": {"wall_clock_seconds": 5400, "llm_api_usd": 1.2}
  }
})
```

### Meta patch before finalize

```json
update_trajectory_meta({
  "trajectory_id": "traj-abc",
  "tags": ["mcp", "qdrant", "embed-dim"],
  "scaffold_text": "Updated scaffold after aligning EMBED_DIM with collection recreate"
})
```

---

## Anti-patterns

| Anti-pattern | Why it hurts | Do instead |
|--------------|--------------|------------|
| Dump full retry/chat history into `task`/`scaffold` | Pollutes keys toward failure-log similarity | Current goal + current state only; retries as steps |
| Skip search before hard/repeated work | Rediscover strategies already in the tree | `search_strategies` cold-start or mid-task |
| Trust search hit summaries alone for copy-paste fixes | Missing step order/context/secrets hygiene | `get_trajectory` for shortlist only |
| Call `get_trajectory` on every hit | Noise + cost | Shortlist 1–3 by progress/effort/runtime fit |
| Embed every append as if it were a new task | Unnecessary re-embed; index churn | Only `scaffold_update` / `task_update` / checkpoints |
| Finalize without effort when you have rough numbers | Loses predictability signal | Best-available concrete totals + notes for estimates |
| Expect server “cheap/expensive” labels | Server deliberately does not band costs | Interpret totals in user context |
| Ignore open/partial hits | Miss “gets this far” strategies | Read progress/remaining_work; optionally get steps |
| Append after finalize | `trajectory_closed` | Start a new trajectory or only search/get |
| Use `effort_predicates` as primary UX | Over-filters sparse data | Prefer read totals; predicates for explicit constraints |
| Treat Yggdrasil as doc RAG | Wrong memory model | Strategy/progress/outcome/effort trajectories only |
| Put secrets in task/scaffold/payload unnecessarily | Trajectories may be stored locally/shared in trust model | Minimize secrets; assume local PoC trust boundary |
| `update_trajectory_meta` instead of recording work | Meta is not a progress log | `append_step` for real work; meta for tags/refs/aspect patches |

---

## Optional: configure MCP host

Run the server from the yggdrasil project (venv + deps installed):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Vector index
docker compose up -d
curl -s http://localhost:6333/readyz

cp .env.example .env
# set embed + optional qdrant settings

python -m yggdrasil.mcp.server
# equivalent entrypoint: yggdrasil-mcp
```

### Environment variables (common)

| Variable | Role |
|----------|------|
| `YGG_SQLITE_PATH` | Canonical SQLite DB path (default `./data/yggdrasil.db`) |
| `QDRANT_URL` | Qdrant HTTP endpoint (default `http://localhost:6333`) |
| `QDRANT_COLLECTION` | Collection name (default `yggdrasil_trajectories`) |
| `QDRANT_API_KEY` | Optional Qdrant auth |
| `EMBED_BASE_URL` | OpenAI-compatible embeddings base (`.../v1`) |
| `EMBED_API_KEY` | Embed API key (required by many providers) |
| `EMBED_MODEL` | Embed model id |
| `EMBED_DIM` | Vector dim; must match Qdrant collection |
| `YGG_DEFAULT_DOMAIN` | Default domain (`coding`) |
| `YGG_SEARCH_INCLUDE_OPEN` | Include open/partial in search by default |
| `YGG_FUSION` / `YGG_W_TASK` / `YGG_W_SCAFFOLD` | Multi-vector fusion weights (RRF PoC) |
| `YGG_EFFORT_FILTER_MODE` | Default `inclusive_null` for effort predicates |
| `YGG_EMBED_VIEW_VERSION` | Default embed view (`coding_v1`) |

Example `.env` embed targets: local vLLM (`EMBED_BASE_URL=http://127.0.0.1:8000/v1`, `EMBED_DIM=1024`) or OpenAI (`text-embedding-3-small`, `EMBED_DIM=1536`). Startup validates collection dim vs model.

### MCP host registration (illustrative)

Point your agent host at the stdio server (adjust command/cwd/env to your install):

```json
{
  "mcpServers": {
    "yggdrasil": {
      "command": "python",
      "args": ["-m", "yggdrasil.mcp.server"],
      "cwd": "/path/to/yggdrasil",
      "env": {
        "YGG_SQLITE_PATH": "./data/yggdrasil.db",
        "QDRANT_URL": "http://localhost:6333",
        "QDRANT_COLLECTION": "yggdrasil_trajectories",
        "EMBED_BASE_URL": "http://127.0.0.1:8000/v1",
        "EMBED_API_KEY": "",
        "EMBED_MODEL": "Qwen/Qwen3-Embedding-0.6B",
        "EMBED_DIM": "1024"
      }
    }
  }
}
```

Security note: trajectories may contain agent context secrets; PoC assumes a **local trust model**. Do not commit `.env` or `mongo_creds.txt`.

---

## Quick decision checklist

1. **Might this have been done before, or am I stuck?** → `search_strategies` with **current** task/scaffold only.
2. **Need step-level detail from a hit?** → `get_trajectory` on shortlist only.
3. **Will this session help future agents?** → `start_trajectory` early; `append_step` as you go.
4. **Did task/scaffold/checkpoint actually change?** → pass update/checkpoint fields (re-embed); else plain append.
5. **Stopping or done?** → finalize with honest `outcome` + concrete `effort` (partial is ok).
6. **Interpreting costs?** → use numbers + `runtime_fingerprint` in **user** context; never invent global cheap/expensive tiers.

---

## References (in-repo)

- Implementation tools: `src/yggdrasil/mcp/tools.py`
- Design spec (tools, embed, effort, agent guidance): `docs/superpowers/specs/2026-06-24-yggdrasil-mcp-trajectory-memory-design.md`
- Quick start / config: `README.md`, `.env.example`
