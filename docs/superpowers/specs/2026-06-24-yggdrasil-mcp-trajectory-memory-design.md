# Yggdrasil — MCP Trajectory Experience Memory

**Date:** 2026-06-24  
**Status:** Draft for implementation planning (brainstorming complete; user-approved direction)  
**Codename:** Yggdrasil (shared “world tree” of agent experiences across domains)

---

## 1. Problem & goals

### 1.1 Problem

Agents repeatedly rediscover strategies for similar tasks. Traditional RAG retrieves documents; it does not systematically store **trajectories** (scaffold state + steps + outcomes + effort) so a calling agent can, at inference time, treat:

- **Query** = embedding of current scaffold / instruction / prompt (multi-aspect)
- **Keys** = past experiences encoded from initial/current task + scaffold aspects (agents continually feed the DB)
- **Values** = outcomes, progress reached, concrete costs, and full step traces (on demand)

This is higher-level Q/K/V memory for **strategy recall and predictability**, without requiring SFT or RL post-training.

### 1.2 PoC goals

1. Record **session-oriented** coding trajectories via MCP (including **partial/open** sessions).
2. Retrieve by multi-aspect similarity: **`task` + `scaffold`** named vectors in Qdrant.
3. Return strategy/progress/outcome **summaries** + **concrete effort totals** first; full steps via explicit get.
4. Run locally: **Python MCP server** + **Qdrant Docker** + **SQLite** canonical store.
5. Keep interfaces ready for **multi-domain**, extra embed aspects, history ablation, and import from existing stores.

### 1.3 Non-goals (PoC)

- Training / fine-tuning pipelines
- Multi-tenant auth / SaaS
- Automatic agent orchestration (Yggdrasil is memory + tools only)
- Server-side “cheap vs expensive” labeling or order-of-magnitude cost bands (context-dependent; offload interpretation to calling model)
- Perfect embed quality tuning (pluggable embedder sufficient)

### 1.4 Success criteria (PoC)

- Agent can `search_strategies` cold-start and mid-task **without** embedding attempt history by default.
- Agent can record multi-step sessions, leave them unfinalized, and search still surfaces them with **progress** (“gets at least this far”).
- `finalize_trajectory` records terminal outcome + **concrete EffortLedger** (time/compute/money/waste) for predictability.
- `get_trajectory` returns full ordered steps for a chosen id.
- Embed model swappable via config without changing MCP tool names.
- **Conversion shim** can import existing MongoDB conversation trajectories for testing (see §9).

### 1.5 Product value (predictability)

Beyond “what worked,” trajectories carry **how long / how costly** it was to reach an outcome (wall time, GPU minutes, API $, wasted compute on failure, network/download cost). Agents use concrete numbers plus `runtime_fingerprint` to schedule and choose strategies in the **user’s** context—not global band labels.

---

## 2. Scope evolution

| Phase | Domain | Storage | Embed aspects | Imports |
|-------|--------|---------|---------------|---------|
| **PoC** | Coding (`domain=coding`) | SQLite + Qdrant | `task`, `scaffold` | Mongo shim for test backfill |
| **Near-term** | Still coding-first | + Postgres adapter optional | + optional `outcome` vector mode | Postgres trajectories instance |
| **Target** | Multi-domain (D) | Pluggable `TrajectoryStore` / `VectorIndex` | Per-domain `EmbedView`; optional `runtime`, step index | Full importers, dual-read |

**PoC = A (agentic coding); architecture shaped for D (multi-domain) without implementing all domains.**

---

## 3. Architectural approaches considered

### 3.1 Approach 1 — Thin MCP + Qdrant-everything

All data in Qdrant payloads. Fastest, but poor fit for large step lists and relational session integrity.

### 3.2 Approach 2 — Split brain: Qdrant vectors + relational trajectories (**chosen**)

- **Qdrant:** ANN + filter payload (ids, status, progress/effort **scalars**, runtime coarse fields).
- **SQLite (PoC) / Postgres (later):** canonical trajectories, steps, outcomes, full effort ledgers.
- **Ports:** `Embedder`, `TrajectoryStore`, `VectorIndex`, `EmbedView`.

### 3.3 Approach 3 — Full event sourcing + projections

Best for research/replay/ablations; deferred. PoC uses append-only **steps** without a separate event bus.

**Recommendation:** Approach 2 for PoC; event-sourced projection hooks only as future evolution.

---

## 4. System context & components

### 4.1 Runtime topology

```text
Agent (MCP host)
    │ MCP stdio (SSE optional later)
    ▼
yggdrasil (Python)
    mcp/tools  →  services  →  ports
         adapters: sqlite_store, qdrant_index, openai_compat_embedder
         later: postgres_store, mongo_conversation_importer (shim)
    │                           │
    ▼                           ▼
 SQLite (canonical)         Qdrant :6333 (Docker)
 trajectories/steps         named vectors: task, scaffold
 effort/outcome full        payload: filters + concrete effort scalars
```

### 4.2 Package layout

```text
yggdrasil/
  pyproject.toml
  docker-compose.yml              # Qdrant only for PoC
  .env.example
  mongo_creds.txt                 # LOCAL ONLY — never commit; see §9
  src/yggdrasil/
    config.py
    domain/                       # types, enums
    ports/                        # Embedder, TrajectoryStore, VectorIndex, EmbedView
    services/                     # session, search, embed
    adapters/                     # sqlite, qdrant, embedder, mongo_importer (shim)
    mcp/                          # server.py, tools.py
  tests/
  docs/superpowers/specs/         # this document
  scripts/                        # import_mongo_for_testing.py (shim CLI)
```

### 4.3 Layering rules

| Layer | May do | Must not do |
|-------|--------|-------------|
| `mcp/tools` | Validate args, call one service, format response | Embed, SQL, Qdrant, business rules |
| `services` | Orchestrate store + embed + index; policies | MCP transport details |
| `ports` | Interfaces only | Implementations |
| `adapters` | One external system | Cross-call other adapters |
| `domain` | Shared models | I/O |

### 4.4 Key service flows

1. **`start_trajectory`** — Insert session (`status=open`); embed task+scaffold; upsert Qdrant; return id.
2. **`append_step`** — Append step in SQLite; update progress; merge effort; re-embed only on scaffold/task change or checkpoint; optional `mark_partial`.
3. **`finalize_trajectory`** — Terminal outcome + close effort ledger; refresh Qdrant payload; reject further appends.
4. **`search_strategies`** — Embed query aspects; Qdrant multi-vector + RRF; filter; hydrate summaries from SQLite (concrete effort, no full steps).
5. **`get_trajectory`** — Full steps + full effort line items.

**Search without write:** always allowed (cold or mid-task recall without starting a session).

---

## 5. Data model

### 5.1 Trajectory

| Field | Notes |
|-------|--------|
| `id` | UUID, MCP-facing |
| `domain` | PoC: `coding` |
| `status` | `open` \| `partial` \| `success` \| `fail` \| `aborted` |
| `task_text`, `scaffold_text` | Aspect sources for embed |
| `runtime_context` / fingerprint | Comparability; not embedded in PoC |
| `tags`, `external_refs` | Filters; lineage (`mongo`, `postgres`, …) |
| `embed_view_version`, `index_state` | `pending` \| `indexed` \| `stale` \| `error` |
| timestamps | `created_at`, `updated_at`, `finalized_at?` |

**Partial/open sessions are first-class** and included in default search: they answer “this strategy reaches at least this scaffold/progress.”

### 5.2 Step

`trajectory_id`, `seq`, `kind`, `summary`, `payload` (JSON), optional `scaffold_update`, `is_checkpoint`, `recorded_at`, optional `step_effort`.

Kinds (PoC): `thought`, `tool_call`, `tool_result`, `edit`, `command`, `checkpoint`, `note`, `other`.

### 5.3 Progress (denormalized on trajectory / search hits)

`phase`, `summary`, `steps_count`, `last_step_summary`, optional `milestones[]`, `percent_estimate`, `blockers[]`.

### 5.4 Outcome (mainly on finalize; partial finalize valid)

`terminal_status`, `summary`, `signals` (JSON: tests, error_class, artifacts, …), `goal_satisfied?`, `remaining_work?` (critical for partial).

### 5.5 EffortLedger (concrete values only in PoC)

**Philosophy:** Multi-dimensional cost (wall, GPU, network, LLM $, cloud $, human wait, failure waste). No server-side “cheap/expensive” or mandatory bands—**calling model interprets numbers in user context**.

**EffortTotals (canonical scalars):**

- `wall_clock_seconds`, `cpu_seconds`, `gpu_seconds`, `gpu_memory_gb_hours`
- `network_bytes`, `disk_bytes_read`, `disk_bytes_write`
- `llm_tokens_in`, `llm_tokens_out`, `llm_api_usd`, `cloud_usd`
- `human_wait_seconds`, `failure_waste_seconds`
- `custom` (JSON, e.g. `hf_dataset_gb`)

**EffortLineItem:** `category`, `label`, `quantity`, `unit`, optional `amount_usd`, time bounds, step seq range, `was_wasted`, `metadata`.

**When written:**

| Moment | Behavior |
|--------|----------|
| `start_trajectory` | Optional runtime fingerprint, `started_at`, empty/partial ledger |
| `append_step` | Optional incremental effort; recompute totals |
| `finalize_trajectory` | Close-out recommended; set `ended_at`; outcome + effort together |

**Search/get:** hits always expose concrete `effort.totals`; full `line_items` on `get_trajectory` (or flag).

### 5.6 Runtime fingerprint

OS/arch, CPU/GPU/RAM, disk type, network_class, region/instance, containerized, extra. Stored and returned for agent-side comparability; optional coarse filters (`gpu_model`, `min_ram_gb`). **Not an embed aspect in PoC** (reserved for later multi-aspect slice).

### 5.7 Status lifecycle

```text
start → open
append (± mark_partial) → open | partial
finalize(terminal_status, outcome, effort) → success | fail | partial | aborted
append after finalize → error trajectory_closed
```

### 5.8 Embed aspects (PoC slice 1 vs target)

| Aspect | PoC embed? | Notes |
|--------|------------|-------|
| `task` | yes | Named vector |
| `scaffold` | yes | Named vector |
| `runtime` | no | Fingerprint payload only |
| `outcome` | no | Outcome in relational + search hydrate |
| attempt history | no (default) | Future ablation: optional include in embed text / extra aspect |

**Default query policy:** embed **current task + current scaffold only**. Prior attempts are steps/metadata, not default embed input. Optional history-in-embed is an **ablation study** later.

---

## 6. Qdrant layout & retrieval

### 6.1 Responsibilities

| System | Holds |
|--------|--------|
| SQLite | Source of truth: sessions, steps, outcomes, full effort |
| Qdrant | `task` + `scaffold` vectors; small filter/sort payload |

**Point id** = trajectory UUID. One point per trajectory in PoC.

### 6.2 Collection

- Name: `yggdrasil_trajectories` (config)
- Named vectors: `task`, `scaffold` — **same embed model/dim**, cosine distance
- Single collection for PoC; multi-domain via `domain` payload filter (collection-per-domain only if later required)

### 6.3 Payload (projection; concrete effort scalars, no bands)

Include: `trajectory_id`, `domain`, `status`, `phase`, `steps_count`, `has_outcome`, `goal_satisfied`, `tags`, timestamps, `embed_model`, `embed_dim`, `embed_view_version`, `aspects_present`, effort totals scalars (`wall_clock_seconds`, `gpu_seconds`, `llm_api_usd`, `failure_waste_seconds`, `network_bytes`, …), coarse runtime (`gpu_model`, `ram_gb`, `network_class`, …), `source` / `external_source`.

**Payload indexes:** keyword/float fields used in filters (`domain`, `status`, `tags`, effort floats, `gpu_model`, …).

### 6.4 Write / re-embed policy

| Event | Vectors | Payload |
|-------|---------|---------|
| start | embed task + scaffold | initial |
| append (no scaffold/task/checkpoint) | no re-embed | progress + effort scalars |
| append (scaffold_update / checkpoint / task_update) | re-embed affected aspects | yes |
| finalize | re-embed only if task/scaffold passed | terminal + final effort scalars |

SQLite success + Qdrant failure ⇒ `index_state=stale`; reconcile on retry/admin path. Embed failure on start ⇒ fail tool (no orphan inconsistent session preferred).

### 6.5 Search: multi-aspect + RRF

1. Build Qdrant filter (`domain`, `status_in`, optional effort predicates, runtime filters).
2. Prefetch/search `task` and/or `scaffold` vectors from query texts.
3. **Fuse with RRF** (default); optional weighted fusion via config (`YGG_FUSION=rrf|weighted`).
4. Overfetch; hydrate from SQLite; return hits with **concrete** effort totals.
5. Optional `prefer_low_waste` light re-rank only (off by default).

**Null effort + filters:** default **`inclusive_null`** — trajectories missing effort still match unless predicate requires `is_not_null`. Avoids empty results when instrumentation is sparse.

### 6.6 Effort filters (optional, explicit constraints only)

Not the primary UX. Agents usually read concrete totals and reason. When user/system states a hard constraint, support **scalar predicates**:

`effort_predicates[]`: `{ field, op: eq|lt|lte|gt|gte|between|is_null|is_not_null, value?, min?, max? }`

Sugar: `max_wall_clock_seconds` etc. compile to predicates internally.

**Deferred (not PoC):** effort band enums, server-side cheap/expensive, full `EffortQuery` AST/query builder. Design hook: normalize future builder IR → Qdrant filter; PoC may only implement predicate list.

**Agent guidance (normative):** interpret seconds/USD/bytes in **user context**; use filters only for explicit bounds.

### 6.7 Future Qdrant evolution

- Named vectors: `outcome`, `runtime`
- Collection `yggdrasil_steps` for step-level retrieval
- History ablation aspect / query flag
- Collection split per domain if embed models diverge

---

## 7. MCP tool contract

Transport: **stdio** MCP (`python -m yggdrasil.mcp.server`). No auth in PoC.

### 7.1 Tools (PoC)

| Tool | Purpose |
|------|---------|
| `start_trajectory` | Open session; index task+scaffold |
| `append_step` | Steps, progress, incremental effort, optional scaffold checkpoint |
| `finalize_trajectory` | Terminal outcome + effort close-out |
| `search_strategies` | Recall; summaries + concrete effort; optional predicates |
| `get_trajectory` | Full steps + effort line items |
| `update_trajectory_meta` | Patch tags/task/scaffold/runtime (pre-finalize) |

**Documented for testing / near-term (implement shim even if not all MCP-exposed initially):**

| Tool / CLI | Purpose |
|------------|---------|
| `import_mongo_conversations` (MCP optional) or `scripts/import_mongo_for_testing.py` | **Conversion shim:** Mongo → internal Trajectory/Step → SQLite + Qdrant |
| Later: postgres store adapter, `reindex_trajectory`, `estimate_effort` | |

### 7.2 Tool summaries

**`start_trajectory`** — req: `task`, `scaffold` (scaffold may be weak but recommended); opt: `domain`, `tags`, `runtime_fingerprint`, `progress`, `effort`, `external_refs`. Returns `trajectory_id`, `status=open`, `index_state`.

**`append_step`** — req: `trajectory_id`, `kind`, `summary`; opt: `payload`, `scaffold_update`, `is_checkpoint`, `progress`, `mark_partial`, `effort`, `task_update`. Returns seq, progress, rolled-up `effort_totals`, `index_state`.

**`finalize_trajectory`** — req: `trajectory_id`, `outcome` (`terminal_status`, `summary`, …); strongly recommend `effort` with concrete totals/line_items; opt: `runtime_fingerprint`, `progress`, final `task`/`scaffold`. Sets closed; further appends → `trajectory_closed`.

**`search_strategies`** — at least one of `task`/`scaffold`; opt: `domain`, `status_in`, `include_open` (default true), `tags_any`, `limit`, `effort_predicates`, `runtime_filters`, `prefer_low_waste`. Does **not** embed attempt history in PoC (`include_attempt_history_in_embed` reserved/ignored). Returns hits without full steps.

**`get_trajectory`** — full record; `include_effort_line_items` default true.

**`update_trajectory_meta`** — patch meta; re-embed if task/scaffold change; reject if finalized (PoC).

### 7.3 Error codes

`invalid_query`, `not_found`, `trajectory_closed`, `embed_failed`, `index_failed`, `store_failed`, `validation_error`.

### 7.4 Agent guidance (tool descriptions must include)

1. Pass **current** task + **current** scaffold into search/start; do not dump full retry transcripts into embed fields by default.
2. Treat search hits as candidates; read `progress`, `outcome`, and **concrete** `effort.totals` (e.g. 5400s wall, $145 USD API).
3. **Calling model** maps numbers to cheap/expensive/slow/fast given user hardware, budget, and deadline—server does not.
4. Use `effort_predicates` only for explicit user/system constraints.
5. Open/partial hits = strategy reached a state, not necessarily failure; use `remaining_work` when present.
6. `get_trajectory` only for shortlisted ids before copying detailed steps.
7. Compare `runtime_fingerprint` on hits to user’s environment when scheduling.
8. On finalize, record best-available concrete effort; note estimates in `effort.notes`; mark wasted compute on failures when known.

---

## 8. Embed pipeline & history ablation hook

### 8.1 Embedder port

OpenAI-compatible HTTP API via env: `EMBED_BASE_URL`, `EMBED_API_KEY`, `EMBED_MODEL`, inferred/configured `EMBED_DIM`. Startup validates Qdrant collection dim vs model.

### 8.2 EmbedView (domain templates)

`coding_v1` builds `task_text` / `scaffold_text` from trajectory fields (deterministic, versioned). Multi-domain = additional views, same ports.

### 8.3 Mid-task safety

Embedding prior attempts biases retrieval toward “logs that look like my failures.” Default policy excludes history from embed text. Steps remain in store for `get_trajectory`.

### 8.4 Ablation (later)

Flag `include_attempt_history_in_embed` (search and/or write): optionally append summarized attempts or last-k step summaries into task/scaffold embed input, or add third aspect. Purpose: measure whether history helps or hurts—not PoC default behavior.

---

## 9. Existing data sources & Mongo conversion shim (testing)

### 9.1 Known external stores (integration later; note paths only—no secrets in repo)

| Store | Purpose | Pointer |
|-------|---------|---------|
| **MongoDB** | Existing session/conversation trajectories | Creds file: `yggdrasil/mongo_creds.txt` (local only, **do not commit**). Database: `claude_conversations`. Collection: `conversations`. |
| **Second Postgres** | Alternate/canonical trajectory sessions | Connection via env later (`POSTGRES_TRAJECTORIES_URL` or similar); implement `PostgresTrajectoryStore` behind same port as SQLite. |

**PoC runtime does not require these online.** Default path remains SQLite + Qdrant.

### 9.2 Conversion shim — implement for testing

**Goal:** Backfill / smoke-test search & schema using real historical trajectories without blocking on perfect schema alignment.

**Deliverable (implementation phase):**

1. **`adapters/mongo_conversation_importer.py`** (or `importers/mongo_conversations.py`)
   - Connect using URI from env override `MONGO_URI` or read from `mongo_creds.txt` (gitignore pattern; never log secrets).
   - Read `claude_conversations.conversations`.
   - Map each document → domain `Trajectory` + ordered `Step[]` via an explicit **field mapping module** (`importers/mongo_mapping.py`) with clear TODOs where source fields vary.
   - Set `external_refs = { "source": "mongo", "db": "claude_conversations", "collection": "conversations", "id": "<mongo _id>" }`.
   - Best-effort: derive `task_text` / `scaffold_text` from first user message / system context / metadata; derive steps from message/tool turns; outcome/status if inferable else `partial` or `open`.
   - Effort: populate only if timestamps/token metadata exist; else leave totals empty (still searchable).
   - `domain` default `coding` unless mapped otherwise.

2. **`scripts/import_mongo_for_testing.py`**
   - CLI: `--limit N`, `--dry-run`, `--reembed`, `--trajectory-id` / filter flags.
   - Pipeline: fetch → map → `TrajectoryStore.upsert` → embed aspects → `VectorIndex.upsert`.
   - Idempotent on `external_refs` mongo id (skip or update-in-place).

3. **Optional MCP tool** `import_mongo_conversations` (admin/testing): thin wrapper over same importer service; can ship after CLI works.

4. **Tests:** unit tests on mapper with **fixture JSON** (checked-in redacted sample docs—not live creds); integration test marked optional if no Mongo.

**Non-goals for shim v1:** perfect fidelity of every Claude conversation shape; live sync/CDC; writing back to Mongo.

**Postgres:** separate adapter later; same internal model. No requirement to implement in first vertical slice unless needed for test data volume.

---

## 10. Configuration & operations

### 10.1 Environment (PoC)

| Variable | Purpose |
|----------|---------|
| `YGG_SQLITE_PATH` | Canonical store path |
| `QDRANT_URL` | default `http://localhost:6333` |
| `QDRANT_COLLECTION` | `yggdrasil_trajectories` |
| `EMBED_BASE_URL`, `EMBED_API_KEY`, `EMBED_MODEL`, `EMBED_DIM` | Embed provider |
| `YGG_DEFAULT_DOMAIN` | `coding` |
| `YGG_SEARCH_INCLUDE_OPEN` | default true |
| `YGG_FUSION` | `rrf` (default) \| `weighted` |
| `YGG_W_TASK`, `YGG_W_SCAFFOLD` | weighted fusion only |
| `YGG_EFFORT_FILTER_MODE` | `inclusive_null` (default) \| `strict` |
| `MONGO_URI` | optional override for import shim |
| `YGG_MONGO_CREDS_FILE` | default `mongo_creds.txt` path for shim |

### 10.2 Docker Compose

Single service: official `qdrant/qdrant` image, port 6333, persistent volume. App runs on host (or second compose service later). PoC: no Qdrant auth; production must add API key + network isolation.

### 10.3 Startup

Idempotent ensure collection + payload indexes; validate embed dim; log config (redact secrets).

### 10.4 Failure modes

Documented in §6.4 / error codes. Prefer clear tool errors over silent drift.

---

## 11. Testing strategy (planning)

| Layer | Focus |
|-------|--------|
| Unit | domain merge effort totals; EmbedView text; effort predicate → filter compile; mongo mapper fixtures |
| Service | start/append/finalize state machine; re-embed only on checkpoint; search hydrate shape |
| Adapter | sqlite CRUD; qdrant upsert/search mocked or testcontainers |
| MCP | tool schema validation; happy path with temp sqlite + mocked embed/qdrant |
| Manual / shim | import subset from Mongo; search returns imported ids with external_refs |

TDD recommended per implementation plan tasks.

---

## 12. Security & privacy (PoC note)

- Local trust model; trajectories may contain secrets from agent context—document retention risk.
- Never commit `mongo_creds.txt` or `.env` with keys; provide `.env.example` and `.gitignore` entries.
- Import shim must not print connection strings or raw creds.

---

## 13. Phased roadmap

| Phase | Deliverables |
|-------|----------------|
| **P0 — Skeleton** | package, config, docker-compose Qdrant, domain types, ports |
| **P1 — Store + index** | SQLite adapter, Qdrant adapter, embedder, session+embed services |
| **P2 — MCP tools** | six tools + agent guidance strings |
| **P3 — Search quality path** | RRF search, filters, hydrate, basic tests |
| **P4 — Mongo shim** | mapper + CLI import for testing; fixtures |
| **P5 — Harden** | reconcile stale index, .gitignore, README runbook |
| **Later** | Postgres store, outcome/runtime vectors, step index, history ablation, EffortQuery builder, multi-domain views, `estimate_effort` |

---

## 14. Key decisions

| ID | Decision | Rationale |
|----|----------|-----------|
| KD-1 | Python MCP server | PoC speed; embed/MCP ecosystem |
| KD-2 | Approach 2: SQLite + Qdrant split | Full traces without payload bloat; pluggable stores |
| KD-3 | Qdrant via Docker from day one | Real ANN; aligns with hybrid backend plan |
| KD-4 | PoC domain coding; multi-domain via `domain` + EmbedView | Path to D without blocking PoC |
| KD-5 | Multi-aspect embed: `task` + `scaffold` only in PoC | Mid-task safe; matches Q/K strategy recall |
| KD-6 | Default exclude attempt history from embed | Avoid failure-log similarity pollution; ablation later |
| KD-7 | Session-oriented write; partial/open searchable | “Gets this far” strategy value |
| KD-8 | EffortLedger with concrete multi-dimensional totals | Predictability/scheduling; no PoC cost bands |
| KD-9 | Calling model interprets cheap/expensive | Bands are context-dependent |
| KD-10 | RRF default fusion | Robust multi-vector without score calibration |
| KD-11 | Optional scalar effort predicates only | Explicit constraints; not primary UX |
| KD-12 | Mongo import conversion shim for testing | Use existing `claude_conversations.conversations` via local `mongo_creds.txt` |
| KD-13 | Postgres trajectories instance = future TrajectoryStore adapter | Same port as SQLite |
| KD-14 | Search without active write session | Cold/mid-task recall |
| KD-15 | finalize closes appends; effort+outcome together | Clean terminal records for learning |

---

## 15. Open questions (non-blocking for PoC start)

1. Exact Mongo conversation document shape variance—resolve during shim mapper with fixtures.
2. Whether `update_trajectory_meta` ships in first MCP slice or CLI-only patches.
3. Embed provider choice (OpenAI vs Voyage vs local compat endpoint)—config-only, pick at implement time.
4. SSE/HTTP MCP transport timeline—stdio sufficient for first agent integration.

---

## 16. PR / implementation slice plan (high level)

| PR | Scope |
|----|--------|
| PR1 | Project scaffold, domain types, ports, config, docker-compose, gitignore |
| PR2 | SQLite store + tests |
| PR3 | Embedder + Qdrant index + collection ensure |
| PR4 | Session + embed + search services |
| PR5 | MCP server + tools + guidance copy |
| PR6 | Mongo conversion shim + CLI + mapper fixtures |
| PR7 | README runbook, import docs, end-to-end smoke |

Detailed task breakdown follows in `docs/superpowers/plans/` via writing-plans skill after spec approval.

---

## 17. Spec self-review checklist

- [x] No TBD required for PoC start (Mongo field mapping deferred to shim implementation with fixtures)
- [x] Approach 2 consistent across sections
- [x] Partial sessions + effort predictability called out end-to-end
- [x] History ablation explicitly future, not default
- [x] Mongo/Postgres noted without embedding secrets
- [x] Scope appropriate for one implementation plan with phased PRs

---

## Appendix A — Conceptual Q/K/V mapping

| IR concept | Yggdrasil |
|------------|-----------|
| Query | Embed(`task_query`), Embed(`scaffold_query`) at search time |
| Key | Stored named vectors on trajectory points (from task_text, scaffold_text) |
| Value | Progress + outcome + concrete effort on hit; full steps/line_items on get |
| Write path | Agents append experiences continuously via MCP session tools |
| Learning mode | Retrieval + agent reasoning at inference; not weight updates |

---

## Appendix B — Example search hit (illustrative)

```json
{
  "trajectory_id": "…",
  "scores": { "task": 0.81, "scaffold": 0.74, "fused": null, "fusion": "rrf" },
  "status": "fail",
  "phase": "verify",
  "steps_count": 14,
  "progress": {
    "summary": "Implemented fix; tests failed on auth edge case",
    "last_step_summary": "pytest exit 1"
  },
  "outcome": {
    "terminal_status": "fail",
    "summary": "Auth regression in refresh token path",
    "remaining_work": "Fix refresh + add regression test"
  },
  "effort": {
    "totals": {
      "wall_clock_seconds": 5400,
      "gpu_seconds": 0,
      "llm_api_usd": 1.25,
      "failure_waste_seconds": 1200
    }
  },
  "runtime_fingerprint": { "gpu_model": null, "ram_gb": 32, "network_class": "broadband" }
}
```

Calling agent decides whether 90 minutes / $1.25 is acceptable for the user’s context.
