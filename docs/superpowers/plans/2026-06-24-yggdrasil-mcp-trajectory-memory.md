# Yggdrasil MCP Trajectory Memory — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python MCP server (Yggdrasil) that stores session-oriented agent trajectories in SQLite, indexes multi-aspect embeddings (`task` + `scaffold`) in Qdrant, exposes six MCP tools for record/recall with concrete effort totals, and includes a MongoDB conversation import shim for testing.

**Architecture:** Approach 2 split-brain — SQLite is source of truth (trajectories, steps, outcomes, full EffortLedger); Qdrant holds named vectors + filter payload (concrete effort scalars only, no cost bands). Services orchestrate ports; MCP tools are thin. PoC domain `coding`; multi-domain via `domain` + `EmbedView` later.

**Tech Stack:** Python 3.11+, pydantic v2, pytest, python-dotenv, httpx, qdrant-client, mcp (FastMCP stdio), Docker Compose (Qdrant only). Optional: pymongo for live import.

**Spec:** [`docs/superpowers/specs/2026-06-24-yggdrasil-mcp-trajectory-memory-design.md`](../specs/2026-06-24-yggdrasil-mcp-trajectory-memory-design.md)

## Global Constraints

- Package under `src/yggdrasil/`; tests under `tests/`; requires-python `>=3.11`.
- PoC embed aspects: **`task` + `scaffold` only**; never embed attempt history by default (ablation later).
- Effort: **concrete multi-dimensional totals/line items only** — no server-side cheap/expensive bands; calling model interprets numbers in user context.
- Partial/open trajectories are first-class and searchable by default (`include_open=true`).
- Layering: `mcp/tools` → `services` → `ports` → `adapters`; domain has no I/O.
- Never commit `.env`, `mongo_creds.txt`, or secrets; ship `.env.example` only.
- TDD: failing test first, minimal impl, pass, commit per task.
- Point id in Qdrant == trajectory UUID string in SQLite.
- RRF fusion default; optional scalar effort predicates only for explicit constraints.
- Mongo: `claude_conversations.conversations` via local `mongo_creds.txt` / `MONGO_URI` — import shim for testing only (unit tests use fixtures, no live Mongo required).
- Second Postgres trajectories instance = future `TrajectoryStore` adapter (out of PoC code; note only).

---

## File Structure (target tree)

```text
yggdrasil/
  pyproject.toml
  .gitignore
  .env.example
  docker-compose.yml
  README.md
  mongo_creds.txt                 # local only, gitignored
  docs/superpowers/specs/...design.md
  docs/superpowers/plans/...plan.md   # this file
  scripts/import_mongo_for_testing.py
  src/yggdrasil/
    __init__.py
    config.py
    domain/
      __init__.py
      enums.py
      models.py
      effort.py
    ports/
      __init__.py
      embedder.py
      embed_view.py
      store.py
      vector_index.py
    adapters/
      __init__.py
      sqlite_store.py
      openai_compat_embedder.py
      qdrant_filters.py
      qdrant_index.py
      embed_views/
        __init__.py
        coding_v1.py
      importers/
        __init__.py
        mongo_mapping.py
        mongo_conversation_importer.py
    services/
      __init__.py
      errors.py
      effort_projection.py      # Qdrant payload builder (if not only in ports)
      embed_service.py
      session_service.py
      search_service.py
      rrf.py
    mcp/
      __init__.py
      __main__.py
      app_context.py
      serialization.py
      tools.py
      server.py
  tests/
    conftest.py
    test_scaffold_import.py
    test_config.py
    domain/
    ports/
    adapters/
    services/
    mcp/
    fixtures/mongo/conversation_sample.json
```

---

## Canonical interfaces (locked — implement Tasks 1–4 first)

### Domain (Task 2)

- Enums: `TrajectoryStatus` (`open|partial|success|fail|aborted`), `StepKind`, `IndexState` (`pending|indexed|stale|error`), `EmbedAspect` (`task|scaffold`), `FusionMode` (`rrf|weighted`), `EffortFilterMode`, `EffortPredicateOp`
- Models (pydantic v2, `extra=forbid`): `RuntimeFingerprint`, `EffortTotals`, `EffortLineItem`, `EffortLedger`, `Progress`, `Outcome`, `Step`, `Trajectory`, `EffortPredicate`, `SearchScores`, `SearchHit`
- Helpers: `merge_effort_totals`, `merge_effort_ledgers`, `is_terminal_status`, `is_writable_status`
- Exceptions in `ports/store.py` or `services/errors.py`: `TrajectoryNotFoundError`, `TrajectoryClosedError`; service layer adds MCP-mapped `YggdrasilError` subclasses with `.code`

### Ports (Task 3)

```python
# Embedder
embed_texts(texts: Sequence[str]) -> list[list[float]]
embed_one(text: str) -> list[float]
model_name: str
dimensions: int

# EmbedView
version: str  # coding_v1
domain: str   # coding
build_aspect_texts(trajectory: Trajectory) -> AspectTexts  # task_text, scaffold_text
build_query_aspect_texts(*, task: str | None, scaffold: str | None) -> AspectTexts

# TrajectoryStore (input DTOs: CreateTrajectoryInput, AppendStepInput, FinalizeTrajectoryInput, UpdateTrajectoryMetaInput)
create(data) -> Trajectory
get(trajectory_id) -> Trajectory  # raises TrajectoryNotFoundError
get_steps(trajectory_id) -> list[Step]
list_by_ids(ids) -> list[Trajectory]
append_step(data) -> tuple[Trajectory, Step]
finalize(data) -> Trajectory
update_meta(data) -> Trajectory
set_index_state(trajectory_id, IndexState) -> Trajectory
find_by_external_ref(source: str, external_id: str) -> Trajectory | None
upsert_imported(trajectory, steps) -> Trajectory  # mongo shim

# VectorIndex
ensure_collection(*, vector_size: int) -> None
upsert(point: UpsertVectorPoint) -> None  # NamedVectors task/scaffold + VectorPointPayload
search(query: VectorSearchQuery) -> list[VectorSearchHit]  # RRF inside adapter when both aspects present
delete(trajectory_id) -> None
payload_from_trajectory(trajectory, *, embed_model, embed_dim, aspects_present) -> VectorPointPayload
```

### Config (Task 4)

`YggConfig` frozen dataclass + `load_config(environ=None, load_dotenv_file=True)` + `redact_config_for_log(cfg)` — env keys exactly as `.env.example` / spec §10.1.

### Service contracts (Tasks 9–12)

| Service | Key methods |
|---------|-------------|
| `EmbedService` | `should_reembed(task_changed, scaffold_changed, is_checkpoint)`, `index_trajectory(traj, *, reembed=True, vectors=None)` |
| `SessionService` | `start_trajectory`, `append_step`, `finalize_trajectory`, `update_trajectory_meta`, `get_trajectory` |
| `SearchService` | `search_strategies(*, task, scaffold, domain, status_in, include_open, tags_any, limit, effort_predicates, runtime_filters, prefer_low_waste)` |
| `reciprocal_rank_fuse` | in `services/rrf.py` — used if adapter returns per-aspect lists; else adapter implements RRF and service hydrates only |

**Re-embed policy:** re-embed only when `task_update` / `scaffold_update` / `is_checkpoint` / start / meta task|scaffold patch. Progress-only append: payload refresh; on vector cache miss, full re-embed once (PoC acceptable).

**index_state:** `indexed` on success; `stale` if SQLite ok but Qdrant/embed fails on append/finalize/meta; start may fail hard with `embed_failed`/`index_failed` after best-effort `error`/`stale` mark.

### MCP tools (Task 13)

Exactly: `start_trajectory`, `append_step`, `finalize_trajectory`, `search_strategies`, `get_trajectory`, `update_trajectory_meta`.  
Transport: FastMCP stdio (`mcp.server.fastmcp.FastMCP`, `mcp.run(transport="stdio")`).  
Tool descriptions must include agent guidance from spec §7.4.

---

## Task dependency graph

```text
T1 scaffold
 └─ T2 domain
     └─ T3 ports
         └─ T4 config
             ├─ T5 sqlite_store ─────────────────────────────┐
             ├─ T6 openai_compat_embedder ──┐                │
             ├─ T7 qdrant_index + filters ──┼─ T10 embed_svc ┼─ T11 session_svc ─┐
             └─ T8 coding_v1 embed_view ────┘                │                  ├─ T13 mcp
                                                              └─ T12 search_svc ─┘
T5 + T10/T11 ─ T14 mongo shim (+ fixtures)
T13 + T14 ─ T15 README runbook
```

Execute **one task per subagent session**; do not start T5 until T1–T4 pass `pytest tests/`.

---

### Task 1: Project scaffold

**Files:** Create `pyproject.toml`, `.gitignore`, `.env.example`, `docker-compose.yml`, `README.md` (stub), `src/yggdrasil/__init__.py` (`__version__ = "0.1.0"`), empty `domain/__init__.py` + `ports/__init__.py`, `tests/conftest.py`, `tests/test_scaffold_import.py`

**Interfaces:** Produces installable package via `pip install -e ".[dev]"`; Qdrant via `docker compose up -d` on `:6333`.

**Dependencies in pyproject:** `pydantic>=2.6,<3`, `python-dotenv>=1.0,<2`; dev: `pytest>=8`, `pytest-asyncio`; later tasks add `httpx`, `qdrant-client`, `mcp>=1.2.0`; optional `[mongo]: pymongo>=4.6`.

- [ ] **Step 1:** Write `tests/test_scaffold_import.py` asserting `import yggdrasil` and `__version__`.
- [ ] **Step 2:** `pytest tests/test_scaffold_import.py -v` — expect FAIL `ModuleNotFoundError`.
- [ ] **Step 3:** Add `pyproject.toml` with setuptools `where = ["src"]`, pytest `pythonpath = ["src"]`.
- [ ] **Step 4:** Add package inits, `.gitignore` (`.env`, `mongo_creds.txt`, `*.db`, `.venv`, etc.).
- [ ] **Step 5:** Add `.env.example` with all spec §10.1 keys (empty secrets).
- [ ] **Step 6:** Add `docker-compose.yml` — service `qdrant`, image `qdrant/qdrant:v1.13.2`, ports 6333/6334, volume.
- [ ] **Step 7:** README stub pointing at spec + `pip install -e ".[dev]"` + compose.
- [ ] **Step 8:** `pip install -e ".[dev]" && pytest tests/test_scaffold_import.py -v` — PASS.
- [ ] **Step 9:** Commit: `chore: scaffold yggdrasil package, compose, and env template`.

---

### Task 2: Domain types, enums, effort merge

**Files:** `src/yggdrasil/domain/enums.py`, `models.py`, `effort.py`, `__init__.py` re-exports; tests `tests/domain/test_enums.py`, `test_models.py`, `test_effort_merge.py`

**Interfaces:** All domain types listed in Canonical interfaces; include `SearchScores` + `SearchHit` with `effort_totals: EffortTotals` (concrete only on search path).

**Key model fields (`Trajectory`):** `id`, `domain`, `status`, `task_text`, `scaffold_text`, `runtime_fingerprint`, `tags`, `external_refs`, `progress`, `outcome`, `effort`, `embed_view_version` (default `coding_v1`), `index_state`, timestamps, optional `finalized_at`.

**`merge_effort_totals`:** sum numeric fields treating None as absent; `custom` dict shallow-merge with numeric sum on key clash.

**`merge_effort_ledgers`:** merge totals; concat `line_items`; `started_at` base wins if set; `ended_at` delta wins; notes join with `\n`.

- [ ] **Step 1:** Write enum tests (status/kind/index/aspect/fusion/predicate ops + TERMINAL/WRITABLE sets; `PARTIAL` in both writable and terminal).
- [ ] **Step 2:** `pytest tests/domain/test_enums.py -v` — FAIL.
- [ ] **Step 3:** Implement `enums.py`.
- [ ] **Step 4:** PASS enums.
- [ ] **Step 5:** Write model tests (minimal Trajectory, Step/Outcome JSON round-trip, SearchHit, extra=forbid).
- [ ] **Step 6:** Implement `models.py` (pydantic BaseModel).
- [ ] **Step 7:** PASS models.
- [ ] **Step 8:** Write effort merge tests.
- [ ] **Step 9:** Implement `effort.py` + domain `__init__` exports.
- [ ] **Step 10:** `pytest tests/domain/ -v` — PASS.
- [ ] **Step 11:** Commit: `feat(domain): trajectory models, enums, effort merge helpers`.

---

### Task 3: Ports (protocols + payload_from_trajectory)

**Files:** `ports/embedder.py`, `embed_view.py`, `store.py`, `vector_index.py`, `__init__.py`; `tests/ports/test_ports_protocols.py`

**Interfaces:** Full protocols and DTOs from Canonical interfaces. Store exceptions: `TrajectoryNotFoundError(trajectory_id)`, `TrajectoryClosedError(trajectory_id, status)`.

**`payload_from_trajectory`:** maps trajectory → Qdrant payload scalars (status, phase, steps_count, has_outcome, goal_satisfied, tags, embed metadata, all effort total fields, gpu_model/ram_gb/network_class, external source).

- [ ] **Step 1:** Write structural tests with fake adapters satisfying `@runtime_checkable` protocols + `payload_from_trajectory` assertions.
- [ ] **Step 2:** FAIL on import.
- [ ] **Step 3–6:** Implement four port modules + re-exports.
- [ ] **Step 7:** `pytest tests/ports/ -v` — PASS.
- [ ] **Step 8:** `pytest tests/test_scaffold_import.py tests/domain/ tests/ports/ -v` — PASS.
- [ ] **Step 9:** Commit: `feat(ports): Embedder, TrajectoryStore, VectorIndex, EmbedView protocols`.

---

### Task 4: YggConfig loader

**Files:** `src/yggdrasil/config.py`; `tests/test_config.py`

**Interfaces:** `YggConfig` frozen dataclass fields: `sqlite_path`, `qdrant_url`, `qdrant_collection`, `qdrant_api_key`, `embed_base_url`, `embed_api_key`, `embed_model`, `embed_dim`, `default_domain`, `search_include_open`, `fusion`, `w_task`, `w_scaffold`, `effort_filter_mode`, `embed_view_version`, `mongo_uri`, `mongo_creds_file`.

**Validation:** embed_dim > 0; fusion/effort_filter_mode enums; weighted fusion requires weights > 0; empty secrets → None; bool parse true/false/1/0/yes/no.

- [ ] **Step 1:** Write tests for defaults, overrides, bool parse, invalid dim/fusion/mode, redaction masks.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** Implement `load_config` + `redact_config_for_log`.
- [ ] **Step 4:** `pytest tests/test_config.py -v` — PASS.
- [ ] **Step 5:** `pytest tests/ -v` — PASS (P0 complete).
- [ ] **Step 6:** Commit: `feat(config): YggConfig from environment with redaction`.

---

### Task 5: SQLite TrajectoryStore adapter

**Files:** `adapters/sqlite_store.py` (SCHEMA_SQL embedded); optional `adapters/sqlite_schema.sql`; `tests/adapters/test_sqlite_store.py`

**Interfaces:** Implements `TrajectoryStore` using Task 3 input DTOs. Persist JSON columns for progress/outcome/effort/runtime/tags/external_refs. Steps table `(trajectory_id, seq)` PK. `external_ref_index(source, external_id) -> trajectory_id` for mongo idempotency.

**Behaviors:**
- `append_step`: assign next seq; merge effort via `merge_effort_ledgers`; apply task/scaffold updates; `mark_partial` → status partial; reject if finalized success/fail/aborted.
- `finalize`: set outcome, terminal status, finalized_at; merge effort close-out; reject double finalize.
- `find_by_external_ref` / `upsert_imported` for importer.

- [ ] **Step 1:** Write tests: create/get, external_ref index, append effort additive merge, mark_partial, finalize blocks append, update_meta index_state, list filters.
- [ ] **Step 2:** FAIL.
- [ ] **Step 3:** Implement adapter with executescript(SCHEMA_SQL).
- [ ] **Step 4:** `pytest tests/adapters/test_sqlite_store.py -v` — PASS.
- [ ] **Step 5:** Commit: `feat(adapters): SQLite TrajectoryStore with effort merge and external_ref index`.

---

### Task 6: OpenAI-compatible embedder

**Files:** `adapters/openai_compat_embedder.py`; `tests/adapters/test_openai_compat_embedder.py`

**Interfaces:** Implements `Embedder`. POST `{base_url}/embeddings`, body `{"model","input"}`, optional Bearer auth. Sort response data by `index`. Empty input → `[]` no HTTP. Raise domain/service embed error on HTTP/JSON failure. Inject fake client for tests (no network).

Add `httpx` to pyproject dependencies.

- [ ] **Step 1:** Tests with fake client (order by index, no auth header when no key, errors).
- [ ] **Step 2–4:** Implement + PASS.
- [ ] **Step 5:** Commit: `feat(adapters): OpenAI-compatible HTTP embedder`.

---

### Task 7: Qdrant VectorIndex + filter compiler

**Files:** `adapters/qdrant_filters.py`, `adapters/qdrant_index.py`; tests `test_qdrant_filters.py`, `test_qdrant_index.py`

**Interfaces:** Implements `VectorIndex`. Collection named vectors `task`/`scaffold`, cosine, size=`embed_dim`. `compile_search_filter` / `compile_effort_predicates` from domain `EffortPredicate` + search filter fields; **inclusive_null** default wraps effort predicates so missing scalars still match.

**RRF:** implement in adapter when both aspect queries present (`fusion=rrf`); `fusion=none` uses first aspect only. Pure `rrf_fuse` unit-tested with fake ranked point lists.

Add `qdrant-client` dependency.

- [ ] **Step 1:** Filter compiler tests (lte, between, is_null, inclusive_null should-wrap, domain/status/tags/runtime).
- [ ] **Step 2:** QdrantIndex tests with fake client (ensure_collection, upsert, RRF search, delete).
- [ ] **Step 3–5:** Implement + PASS.
- [ ] **Step 6:** Commit: `feat(adapters): Qdrant index with named vectors, filters, RRF`.

---

### Task 8: CodingEmbedView `coding_v1`

**Files:** `adapters/embed_views/coding_v1.py`, `__init__.py` (`get_embed_view(version)`); `tests/domain/test_coding_embed_view.py`

**Interfaces:** Implements `EmbedView`. Deterministic templates:

**Write `task_text`:**
```text
domain: coding
task: {stripped task_text}
tags: {sorted comma tags}   # omit line if no tags
```

**Write `scaffold_text`:**
```text
domain: coding
scaffold: {stripped scaffold_text}
progress_phase: ...   # if non-empty
progress_summary: ... # if non-empty
```

**Query aspects:** same domain/task and domain/scaffold lines only (no progress, no history).

**Must not embed:** step bodies, tool payloads, runtime fingerprint, full attempt history.  
`include_attempt_history` / future flag: raise `ValueError` in PoC if ever passed.

- [ ] **Step 1:** Tests for determinism, tag sort, no runtime leakage, no step leakage, registry.
- [ ] **Step 2–4:** Implement + PASS.
- [ ] **Step 5:** Commit: `feat(adapters): coding_v1 EmbedView templates`.

---

### Task 9: Service errors + effort projection (if not only in ports)

**Files:** `services/errors.py` (`YggdrasilError` + codes: `not_found`, `trajectory_closed`, `validation_error`, `embed_failed`, `index_failed`, `store_failed`, `invalid_query`); optionally `services/effort_projection.py` re-exporting/wrapping `payload_from_trajectory` for services; tests for error codes.

- [ ] Implement errors used by T10–T13.
- [ ] Commit: `feat(services): error types with MCP error codes`.

---

### Task 10: EmbedService

**Files:** `services/embed_service.py`; `tests/services/test_embed_service.py`

**Interfaces:**
```python
should_reembed(*, task_changed: bool, scaffold_changed: bool, is_checkpoint: bool) -> bool
class EmbedService:
    def __init__(self, embedder: Embedder, index: VectorIndex, view: EmbedView, config: YggConfig): ...
    def index_trajectory(self, trajectory: Trajectory, *, reembed: bool = True, prior_vectors: NamedVectors | None = None) -> NamedVectors:
        # build AspectTexts via view; embed aspects if reembed or prior missing; upsert UpsertVectorPoint; return vectors used
```

- [ ] Tests with fakes: policy matrix, full embed+upsert, payload-only with prior vectors skips embed_texts.
- [ ] Implement.
- [ ] Commit: `feat(services): EmbedService and re-embed policy`.

---

### Task 11: SessionService

**Files:** `services/session_service.py`; `tests/services/test_session_service.py`

**Interfaces:** Methods map 1:1 to MCP tools (minus search). Coordinates store + embed; maintains optional in-memory vector cache per trajectory_id for payload-only updates; sets `index_state` per policy.

**start_trajectory:** create via `CreateTrajectoryInput`; index with reembed=True; on index fail after create → set error/stale and raise embed/index failed.

**append_step / finalize / update_meta:** store first; then embed with reembed per policy; on index fail → `stale`, do not rollback store (except start).

**finalize:** reject if already finalized terminal; set outcome + effort ended_at.

- [ ] Tests: start indexed; append reembed false; checkpoint reembed true; finalize blocks append; append index fail → stale; get returns steps via get_steps.
- [ ] Implement aligned to Task 3 store DTOs (not ad-hoc kwargs).
- [ ] Commit: `feat(services): SessionService lifecycle and index_state handling`.

---

### Task 12: SearchService + RRF helper

**Files:** `services/search_service.py`, `services/rrf.py`; tests `test_rrf.py`, `test_search_service.py`

**Interfaces:**
```python
def reciprocal_rank_fuse(rank_lists: dict[str, list[str]], *, k: int = 60) -> list[tuple[str, float]]: ...

class SearchService:
    def search_strategies(
        self, *, task: str | None = None, scaffold: str | None = None,
        domain: str | None = "coding", status_in: list[str] | None = None,
        include_open: bool | None = None, tags_any: list[str] | None = None,
        limit: int = 10, effort_predicates: list[EffortPredicate] | dict | None = None,
        runtime_filters: dict | None = None, prefer_low_waste: bool = False,
        include_attempt_history_in_embed: bool = False,  # ignored in PoC
    ) -> list[SearchHit]: ...
```

Flow: validate task|scaffold; embed query aspects via view+embedder; build `VectorSearchQuery`; index.search (RRF in adapter); hydrate trajectories via `store.get` / `list_by_ids`; build `SearchHit` with progress/outcome/`effort_totals` only; optional prefer_low_waste stable sort by `failure_waste_seconds` (None last).

- [ ] RRF unit tests.
- [ ] Search service tests with fakes.
- [ ] Implement.
- [ ] Commit: `feat(services): SearchService with RRF and hydration`.

---

### Task 13: MCP server + six tools (FastMCP stdio)

**Files:** `mcp/app_context.py`, `serialization.py`, `tools.py`, `server.py`, `__main__.py`; add `mcp>=1.2.0` + console script `yggdrasil-mcp`; `tests/mcp/test_tools_unit.py`

**Interfaces:** `AppContext.from_config()` wires SqliteTrajectoryStore, OpenAICompatEmbedder, QdrantIndex.ensure_collection(embed_dim), CodingEmbedViewV1 / get_embed_view(config.embed_view_version), EmbedService, SessionService, SearchService.

**Tools:** register on FastMCP with descriptions starting with agent guidance preamble (spec §7.4). Return dicts via serializers; errors as `{"error": {"code", "message"}}`.

**server.main:** build context (log redact_config), register_tools, `mcp.run(transport="stdio")`.

- [ ] Unit tests for error_payload + trajectory_to_dict keys.
- [ ] Implement tools/server (fix any typos; use TrajectoryStatus enum coercion for outcome).
- [ ] `pytest tests/mcp/ -v` PASS.
- [ ] Smoke: `python -c "from yggdrasil.mcp.server import build_server"` with env/fakes as available.
- [ ] Commit: `feat(mcp): FastMCP stdio server with trajectory tools`.

---

### Task 14: Mongo conversion shim (testing)

**Files:**
- `adapters/importers/mongo_mapping.py` — `map_mongo_conversation_doc(doc) -> MappedTrajectory`
- `adapters/importers/mongo_conversation_importer.py` — idempotent import via `find_by_external_ref` + session/store
- `scripts/import_mongo_for_testing.py` — `--limit`, `--dry-run`, `--reembed`, `--mongo-uri`, `--creds-file`
- `tests/fixtures/mongo/conversation_sample.json` — redacted plausible `claude_conversations.conversations` doc
- `tests/adapters/importers/test_mongo_mapping.py`, `test_mongo_conversation_importer.py`
- pyproject optional extra `[mongo]`

**Mapping rules (v1 heuristics, fixture-tested):**
- `external_refs = {source: mongo, db: claude_conversations, collection: conversations, id: str(_id)}`
- `task_text` = metadata.title + first user message
- `scaffold_text` = project + early assistant/tool hints (not full transcript)
- steps from messages (user→note, assistant→thought, tool_calls→tool_call, tool→tool_result)
- status default `partial`; weak success/fail inference from last assistant text
- effort only if usage tokens present; else empty totals

**CLI:** never print URI; read `MONGO_URI` or first mongodb line / MONGO*= from creds file.

**Unit tests:** fixture only — no live Mongo.

- [ ] Write fixture JSON + mapper tests.
- [ ] Implement mapper.
- [ ] Implement importer + mem-store idempotency test.
- [ ] Implement CLI script.
- [ ] Ensure store `find_by_external_ref` works (Task 5).
- [ ] `pytest tests/adapters/importers/ -v` PASS.
- [ ] Commit: `feat(importers): mongo conversation shim with fixtures and CLI`.

---

### Task 15: README runbook

**Files:** Replace/expand `README.md`; verify `.env.example` + `docker-compose.yml` match.

**Content required:** architecture blurb; prerequisites; venv install; copy `.env`; `docker compose up -d`; `curl :6333/readyz`; run `python -m yggdrasil.mcp.server` / `yggdrasil-mcp`; conceptual smoke tool order; mongo import dry-run/live commands; config table; security notes; link to spec.

- [ ] Write README.
- [ ] Commit: `docs: PoC runbook for Qdrant, MCP, and mongo import`.

---

## Implementation order checklist (agentic execution)

| Order | Task | Verify command |
|------:|------|----------------|
| 1 | T1 scaffold | `pytest tests/test_scaffold_import.py -v` |
| 2 | T2 domain | `pytest tests/domain/ -v` |
| 3 | T3 ports | `pytest tests/ports/ -v` |
| 4 | T4 config | `pytest tests/ -v` |
| 5 | T5 sqlite | `pytest tests/adapters/test_sqlite_store.py -v` |
| 6 | T6 embedder | `pytest tests/adapters/test_openai_compat_embedder.py -v` |
| 7 | T7 qdrant | `pytest tests/adapters/test_qdrant_*.py -v` |
| 8 | T8 embed_view | `pytest tests/domain/test_coding_embed_view.py -v` |
| 9 | T9 errors | `pytest tests/services/test_errors.py -v` (or inline in T10) |
| 10 | T10 embed_svc | `pytest tests/services/test_embed_service.py -v` |
| 11 | T11 session | `pytest tests/services/test_session_service.py -v` |
| 12 | T12 search | `pytest tests/services/test_rrf.py tests/services/test_search_service.py -v` |
| 13 | T13 mcp | `pytest tests/mcp/ -v` |
| 14 | T14 mongo | `pytest tests/adapters/importers/ -v` |
| 15 | T15 readme | manual review |

Full regression before handoff: `pytest -q`

---

## Spec coverage matrix

| Spec section | Tasks |
|--------------|-------|
| §1 goals / non-goals | All; T15 documents |
| §3 Approach 2 | T5+T7+T10–12 |
| §4 components / layering | T1–T4, T13 |
| §5 data model + EffortLedger | T2, T5, T11 |
| §5 partial/open value | T5, T11, T12 default include_open |
| §6 Qdrant / RRF / predicates / inclusive_null | T3 payload, T7, T12 |
| §7 MCP tools + agent guidance | T13 |
| §8 embed policy / history ablation hook | T8, T10, T12 ignore flag |
| §9 Mongo/Postgres notes + shim | T14; Postgres deferred |
| §10 config/ops compose | T1, T4, T15 |
| §11 testing | every task |
| §12 security gitignore | T1, T14, T15 |
| §13 roadmap P0–P5 | T1–T15 |

## Placeholder / consistency self-review

- No intentional TBD for PoC start; mongo field variance handled by explicit heuristics + fixtures in T14.
- Port DTOs (Task 3) are normative for SessionService — do not invent parallel store method signatures in T11.
- `SearchHit.effort_totals` vs full `EffortLedger` on get_trajectory only.
- Adapter RRF (T7) vs service RRF (T12): prefer **RRF in T7 adapter**; T12 hydrates only. If implementer duplicates RRF in T12, ensure single path documented — recommended delete service RRF if adapter returns fused hits only.
- Effort merge: domain `merge_effort_ledgers` is single source; store and session must not diverge.

## Deferred (explicit non-goals in this plan)

- Postgres `TrajectoryStore` adapter (design note only; wire later behind same port).
- Outcome/runtime named vectors; step-level collection; history-in-embed ablation implementation.
- EffortQuery AST / cost bands / `estimate_effort` tool.
- SSE/HTTP MCP transport; multi-tenant auth.

---

## Execution handoff

**Plan complete and saved to** [`docs/superpowers/plans/2026-06-24-yggdrasil-mcp-trajectory-memory.md`](2026-06-24-yggdrasil-mcp-trajectory-memory.md).

**Two execution options:**

1. **Subagent-Driven (recommended)** — Use superpowers:subagent-driven-development: fresh subagent per task (T1→T15), run verify command, review between tasks, commit per task.
2. **Inline Execution** — Use superpowers:executing-plans in this session with checkpoints after each task.

**Recommended start:** Task 1 (scaffold) in an isolated worktree if using git worktrees skill; otherwise implement directly in `yggdrasil/`.

**Subagent prompt template per task:**
> Implement only Task N from `docs/superpowers/plans/2026-06-24-yggdrasil-mcp-trajectory-memory.md`. Follow TDD. Do not implement later tasks. Run the verify command for Task N. Commit with the message specified. Report files changed and test output.

---

## Appendix: Mongo fixture minimal shape (T14)

Checked-in at `tests/fixtures/mongo/conversation_sample.json` — must include `_id` (oid or string), `metadata.title/project/tags/timestamps`, `messages[]` with roles `user|assistant|tool` and optional `tool_calls`, optional `usage.input_tokens/output_tokens`. Redact all real user content.

## Appendix: Effort totals scalar keys (Qdrant payload)

`wall_clock_seconds`, `cpu_seconds`, `gpu_seconds`, `gpu_memory_gb_hours`, `network_bytes`, `disk_bytes_read`, `disk_bytes_write`, `llm_tokens_in`, `llm_tokens_out`, `llm_api_usd`, `cloud_usd`, `human_wait_seconds`, `failure_waste_seconds` — plus filter metadata fields from `payload_from_trajectory`. No band_* fields in PoC.
