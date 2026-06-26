# Code Review Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. After each task, run superpowers:requesting-code-review (or `/review --local`) before starting the next wave. Use superpowers:check-work / verification-before-completion before claiming a wave done. Use superpowers:test-driven-development for every code task (failing test first). Optional: superpowers:using-git-worktrees for isolated execution.

**Goal:** Close P0/P1 findings from the 2026-06-25 comprehensive code review so Yggdrasil is safe to grow as a **single-operator trusted PoC** with honest privacy claims, stable dual-store indexing, real integration tests, and a simplified Mongo ingress—without pretending multi-tenant production readiness yet.

**Architecture:** Keep Approach 2 (SQLite SoT + Qdrant named vectors). Make Qdrant point IDs deterministic; track index health on trajectories; fail closed on unimplemented fusion; tighten lab trust gates; collapse legacy import; document threat model; optionally add a **content scrubber port** (regex first, LLM later) with owner exception. Defer full MCP authz/RLS to a follow-on plan.

**Tech Stack:** Existing Python 3.11+, pydantic v2, pytest, qdrant-client, SQLite, MCP FastMCP. Tests: unit + in-process integration (tmp SQLite; Qdrant optional via env or mocked `VectorIndex`).

**Inputs / review artifacts:**
- [`surveys/code_review_comprehensive_2026-06-25.md`](../../../surveys/code_review_comprehensive_2026-06-25.md)
- [`surveys/code_review_thermonuclear.md`](../../../surveys/code_review_thermonuclear.md)
- [`surveys/code_review_architecture.md`](../../../surveys/code_review_architecture.md)
- [`surveys/code_review_security.md`](../../../surveys/code_review_security.md)
- Original design: [`docs/superpowers/specs/2026-06-24-yggdrasil-mcp-trajectory-memory-design.md`](../specs/2026-06-24-yggdrasil-mcp-trajectory-memory-design.md)

## Global Constraints

- Package under `src/yggdrasil/`; tests under `tests/`; `requires-python >=3.11`.
- Layering: `mcp/tools` → `services` → `ports` → `adapters`; domain has no I/O.
- TDD: failing test → minimal impl → pass → commit per task.
- Never commit `.env`, `mongo_creds.txt`, `user_mapping.yaml`, or secrets.
- PoC threat model remains **trusted local operator + trusted agents** until Wave F (explicit multi-user plan).
- Do not implement full multi-tenant authz in this plan—only document blockers and add minimal knobs that do not lie.
- Prefer deleting complexity (legacy import path) over adding flags.
- After each **Wave**, run: `PYTHONPATH=src pytest -q` (all must pass).
- Point ID rule (restored from original design intent): **stable deterministic id from `trajectory_id`**, never Python `hash()`.

## Execution waves (dependency order)

| Wave | Name | Closes review items |
|------|------|---------------------|
| **A** | Stable index identity + config honesty | P0 #1, #4; embed dim validation |
| **B** | Integration test belt | P0 #3 |
| **C** | Dual-store write policy + index status | P0 #2; SQLite concurrency partial |
| **D** | Trust gates / experience_grade | P0 #8 |
| **E** | Mongo ingress collapse | P0 #7; thermonuclear gravity well |
| **F** | Privacy honesty + scrubber skeleton | P0 #5 (honesty + optional enforcement) |
| **G** | PoC security hygiene docs + compose | P0 #6 (document + optional Qdrant API key) |
| **H** | Layer cleanup (P2, optional in same PR train) | adapter→service imports |

Implement **A→D** before more features. **E–G** can parallelize after B if staffing allows (E depends on hierarchical path tests from B).

## File structure (touch map)

```text
src/yggdrasil/
  adapters/qdrant_index.py          # Wave A: _point_id; fusion weighted; dim check
  adapters/sqlite_store.py          # Wave C: WAL, index_status column, seq race
  adapters/importers/
    mongo_mapping.py                # Wave E: delete/shrink legacy
    mongo_conversation_importer.py  # Wave E: hierarchical-only atomic session
    mongo_segment.py                # Wave E: messages_to_steps extract if needed
  config.py                         # Wave A: reject weighted until implemented OR implement
  domain/models.py                  # Wave C: IndexStatus enum field on trajectory meta
  domain/enums.py                   # Wave C: IndexStatus
  ports/store.py                    # Wave C: index_status on update API if needed
  ports/scrubber.py                 # Wave F: NEW protocol (optional)
  adapters/regex_scrubber.py        # Wave F: NEW minimal regex scrubber
  services/session_service.py       # Wave C: embed failure → index_status
  services/embed_service.py         # Wave C: return status; dim validation
  services/search_service.py        # Wave D: trust gates; respect index_status
  services/retrieval_gates.py       # Wave D: experience_grade / hydration tags
  mcp/tools.py                      # Wave B tests; Wave G threat notes in errors optional
tests/
  adapters/test_qdrant_point_id.py  # Wave A
  adapters/test_sqlite_store.py     # Wave B/C
  services/test_session_lifecycle.py # Wave B/C
  services/test_retrieval_gates.py  # Wave D extend
  integration/test_trajectory_roundtrip.py  # Wave B
  importers/test_hierarchical_atomic.py     # Wave B/E
  adapters/test_regex_scrubber.py   # Wave F
docs/superpowers/specs/
  2026-06-25-threat-model-poc.md    # Wave G NEW (short)
README.md                           # Wave F/G: threat model pointer; privacy honesty
docker-compose.yml                  # Wave G: optional QDRANT_API_KEY
skills/yggdrasil-trajectory-memory/SKILL.md  # Wave D: align trust language
```

---

## Wave A — Stable Qdrant identity + config honesty

### Task A1: Deterministic Qdrant point IDs

**Files:**
- Modify: `src/yggdrasil/adapters/qdrant_index.py` (`_point_id`)
- Create: `tests/adapters/test_qdrant_point_id.py`

**Interfaces:**
- Produces: `point_id_for_trajectory(trajectory_id: str) -> str` (always UUID string form Qdrant accepts)
- Consumes: `trajectory_id` strings (UUID or non-UUID segment ids like `mongo-session-…-seg-0000`)

- [ ] **Step 1: Write failing tests**

```python
# tests/adapters/test_qdrant_point_id.py
from yggdrasil.adapters.qdrant_index import point_id_for_trajectory

def test_uuid_trajectory_id_round_trips():
    tid = "550e8400-e29b-41d4-a716-446655440000"
    assert point_id_for_trajectory(tid) == tid

def test_non_uuid_is_stable_across_calls():
    tid = "mongo-session-abc-seg-0000"
    a = point_id_for_trajectory(tid)
    b = point_id_for_trajectory(tid)
    assert a == b
    # Must not depend on PYTHONHASHSEED / process
    assert isinstance(a, str)
    assert len(a) == 36  # UUID string

def test_different_ids_differ():
    assert point_id_for_trajectory("seg-a") != point_id_for_trajectory("seg-b")
```

- [ ] **Step 2: Run test — expect FAIL** (function missing or still uses `hash`)

```bash
PYTHONPATH=src pytest tests/adapters/test_qdrant_point_id.py -v
```

- [ ] **Step 3: Implement**

```python
# In qdrant_index.py — replace _point_id
import hashlib
from uuid import UUID, uuid5, NAMESPACE_URL

YGG_POINT_NAMESPACE = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # or project-fixed UUID

def point_id_for_trajectory(trajectory_id: str) -> str:
    try:
        return str(UUID(trajectory_id))
    except ValueError:
        # Stable across processes/workers (never Python hash())
        return str(uuid5(YGG_POINT_NAMESPACE, trajectory_id))

def _point_id(trajectory_id: str) -> str:
    return point_id_for_trajectory(trajectory_id)
```

Ensure all `upsert` / `delete` / search result mapping uses `point_id_for_trajectory` consistently. Payload must still store original `trajectory_id` string.

- [ ] **Step 4: Run tests — PASS**

```bash
PYTHONPATH=src pytest tests/adapters/test_qdrant_point_id.py -v
PYTHONPATH=src pytest -q
```

- [ ] **Step 5: Commit**

```bash
git add src/yggdrasil/adapters/qdrant_index.py tests/adapters/test_qdrant_point_id.py
git commit -m "fix(qdrant): stable deterministic point ids (no Python hash)"
```

**Migration note (document in commit body):** Existing Qdrant collections keyed by old `hash()` ints are **orphaned**. PoC action: delete collection or `ensure_collection` with new name via env; re-run embed/hydration. Add one line to README under Config.

---

### Task A2: Fusion config honesty (implement weighted OR reject)

**Decision for this plan:** **Implement weighted fusion** (config already has `w_task` / `w_scaffold`) — small, removes the lie.

**Files:**
- Modify: `src/yggdrasil/adapters/qdrant_index.py` (search path)
- Modify: `tests/services/test_rrf.py` or create `tests/adapters/test_qdrant_fusion.py` with unit-level fusion helper

**Interfaces:**
- When `FusionMode.WEIGHTED`, combine scores: `w_task * s_task + w_scaffold * s_scaffold` after per-aspect search (normalize weights to sum 1 if not already).

- [ ] **Step 1: Write failing test** for a pure function extracted from search:

```python
# tests/adapters/test_weighted_fusion.py
from yggdrasil.adapters.qdrant_index import fuse_weighted_scores

def test_weighted_prefers_higher_weight_aspect():
    # tid -> (task_score, scaffold_score)
    scores = {"a": (1.0, 0.0), "b": (0.0, 1.0)}
    ranked = fuse_weighted_scores(scores, w_task=0.8, w_scaffold=0.2)
    assert ranked[0][0] == "a"
```

- [ ] **Step 2: Implement `fuse_weighted_scores` + wire `search` when `self._fusion == FusionMode.WEIGHTED`**

Pass `w_task` / `w_scaffold` into `QdrantIndex.__init__` from config (today only fusion enum is passed — extend constructor).

- [ ] **Step 3: Config load already validates weights > 0 for weighted — keep that**

- [ ] **Step 4: pytest -q**

- [ ] **Step 5: Commit** `feat(qdrant): implement weighted fusion`

---

### Task A3: Embed dimension validation

**Files:**
- Modify: `src/yggdrasil/services/embed_service.py` and/or `adapters/openai_compat_embedder.py`
- Modify: `src/yggdrasil/adapters/qdrant_index.py` `ensure_collection` — if collection exists, read vector size and error on mismatch

- [ ] **Step 1: Test** — mock embedder returns wrong len → `EmbedService` raises clear `ConfigError` or domain error

- [ ] **Step 2: After embed, assert `len(vec) == cfg.embed_dim`**

- [ ] **Step 3: On `ensure_collection`, if collection exists, compare `task` vector size to `vector_size` argument; raise if mismatch**

- [ ] **Step 4: Commit** `fix(embed): validate embedding dimensions vs config and Qdrant`

---

## Wave B — Integration test belt

### Task B1: SQLite store unit tests (foundation)

**Files:**
- Create: `tests/adapters/test_sqlite_store.py`
- Uses tmp_path SQLite file

- [ ] **Step 1: Failing tests** for `create_trajectory`, `append_step` seq monotonic, `finalize`, `get_trajectory` include_steps

```python
def test_roundtrip_trajectory(tmp_path):
    from yggdrasil.adapters.sqlite_store import SqliteTrajectoryStore
    # minimal Trajectory/Step domain objects — mirror existing models
    store = SqliteTrajectoryStore(tmp_path / "t.db")
    # create → append → get → assert steps
```

- [ ] **Step 2: Implement only if tests reveal bugs; otherwise tests document current behavior**

- [ ] **Step 3: Commit** `test(sqlite): trajectory store unit coverage`

---

### Task B2: In-process session lifecycle (store + fake vector index)

**Files:**
- Create: `tests/services/test_session_lifecycle.py`
- Create: `tests/fakes.py` with `FakeVectorIndex` implementing `ports.vector_index.VectorIndex` (in-memory dict)

**Interfaces:**
- `FakeVectorIndex.upsert` records points; `search` returns by trivial string match on payload task text for tests

- [ ] **Step 1: Test** `SessionService.start` → `append_step` → `finalize` → `SearchService.search_strategies` finds trajectory with `FakeVectorIndex` + real embedder **or** inject precomputed vectors via a `FakeEmbedder` that returns fixed-dim zeros/ones

Prefer **FakeEmbedder** returning ` [0.1] * dim` and `FakeVectorIndex` that ignores vectors and filters on payload — keeps tests offline.

- [ ] **Step 2: Minimal wiring in test fixture** — build services like `mcp/app_context.py` does

- [ ] **Step 3: Commit** `test(services): session lifecycle with fake embedder/index`

---

### Task B3: MCP tools smoke (optional but recommended)

**Files:**
- Create: `tests/mcp/test_tools_register.py` — import tools module, assert tool names exist
- Or call service layer only if FastMCP hard to invoke in unit tests

- [ ] Prefer service-level lifecycle over full stdio MCP for PoC speed; add MCP registration smoke only.

- [ ] **Commit** `test(mcp): tool module smoke`

---

## Wave C — Dual-store write policy + index status

### Task C1: `IndexStatus` on trajectories

**Files:**
- Modify: `src/yggdrasil/domain/enums.py` — add `IndexStatus = open | pending | ready | stale | failed`
- Modify: `src/yggdrasil/domain/models.py` / SQLite schema in `sqlite_store.py` — column `index_status TEXT NOT NULL DEFAULT 'pending'`
- Migration: `CREATE TABLE IF NOT EXISTS` already — use `ALTER TABLE` best-effort in `_migrate` or recreate note for PoC (`unlink db` acceptable for PoC; document)

- [ ] **Step 1: Test** new trajectories default `pending`; after successful embed → `ready`; embed error → `failed` or `stale`

- [ ] **Step 2: Schema + model field**

- [ ] **Step 3: Commit** `feat(store): trajectory index_status column`

---

### Task C2: SessionService embed outcome updates status

**Files:**
- Modify: `src/yggdrasil/services/session_service.py`
- Modify: `src/yggdrasil/services/embed_service.py` — return success/failure explicitly (no silent pass)

**Policy (document in code comment):**
1. Persist SQLite first (SoT).
2. Attempt Qdrant upsert.
3. On success → `index_status=ready`.
4. On failure → `index_status=failed` (or `stale` if previous ready); surface error to MCP response dict (`ok: false` already pattern).
5. Search default: exclude `index_status in (pending, failed)` unless `include_unindexed=true` (new optional flag default false).

- [ ] **Step 1: Failing lifecycle test** — FakeVectorIndex that raises on upsert → trajectory exists in SQLite with `failed`, search does not return it by default

- [ ] **Step 2: Implement**

- [ ] **Step 3: Commit** `feat(session): index_status after embed success/failure`

---

### Task C3: SQLite hardening (PoC multi-thread safety)

**Files:**
- Modify: `src/yggdrasil/adapters/sqlite_store.py`

- [ ] **Step 1:** After connect: `PRAGMA journal_mode=WAL;` `PRAGMA busy_timeout=5000;`
- [ ] **Step 2:** Replace `MAX(seq)+1` race with single-statement insert using subquery **inside a write transaction** (`BEGIN IMMEDIATE`)
- [ ] **Step 3:** Test concurrent appends optional (threading); at least sequential seq test remains
- [ ] **Step 4: Commit** `fix(sqlite): WAL and transactional step seq allocation`

---

### Task C4: Hierarchical import atomicity (session unit)

**Files:**
- Modify: `src/yggdrasil/adapters/importers/mongo_conversation_importer.py`

**Policy:** For one logical session, either all children + parent metadata commit, or none. Prefer: write all SQLite rows in one transaction; embed children after commit; mark each child’s `index_status`. On embed partial failure, children remain queryable via SQLite `get` but not search (status failed) — acceptable PoC; log clearly.

- [ ] **Step 1: Test** with fake store that fails mid-child → no partial parent published **or** document compensating status (pick one and test it)

Recommended PoC: **SQLite transaction for all trajectory rows**; embeds best-effort after.

- [ ] **Step 2: Implement transaction boundary**

- [ ] **Step 3: Commit** `fix(import): transactional hierarchical session write`

---

## Wave D — Trust gates / experience_grade

### Task D1: Lab mode excludes hydration provenance

**Files:**
- Modify: `src/yggdrasil/services/retrieval_gates.py`
- Modify: `tests/services/test_retrieval_gates.py`
- Modify: `skills/yggdrasil-trajectory-memory/SKILL.md` (one paragraph)

**Rules to encode:**
- `search_mode=lab` (default agent path) **must exclude** tags: `hydration_test`, `external_pre_embed`, `not_author_segmented` (already partially there — verify and add tests for `experience_grade` alone **not** sufficient to pass lab if those tags present).
- Payload flag `experience_grade=true` **cannot override** archive/hydration excludes.
- `search_mode=agent` may be stricter or equal; document difference in skill.

- [ ] **Step 1: Failing tests** for grade-without-tags and grade-with-hydration-tags

- [ ] **Step 2: Implement gate logic**

- [ ] **Step 3: Update SKILL.md trust section**

- [ ] **Step 4: Commit** `fix(gates): experience_grade cannot launder hydration trust`

---

## Wave E — Mongo ingress collapse (code judo)

### Task E1: Inventory and mark legacy dead

**Files:**
- Read: `mongo_mapping.py`, `mongo_conversation_importer.py`, scripts calling legacy

- [ ] **Step 1:** Grep for `legacy` / non-hierarchical entrypoints in scripts
- [ ] **Step 2:** Add deprecation warnings on legacy CLI flags (`--hierarchical` becomes default; legacy path raises `SystemExit` with message to use hierarchical)
- [ ] **Step 3: Commit** `chore(import): deprecate non-hierarchical mongo import`

---

### Task E2: Extract `messages_to_steps` (if duplicated)

**Files:**
- Create: `src/yggdrasil/adapters/importers/messages_to_steps.py` **only if** duplication exists between mapping and importer
- Or extract from `mongo_mapping.py` into focused module and thin `mongo_mapping` to hierarchical-only helpers

- [ ] **Step 1:** Move shared conversion without behavior change; tests still pass
- [ ] **Step 2: Commit** `refactor(import): extract messages_to_steps`

---

### Task E3: Delete or quarantine legacy mapping surface

**Target:** Shrink `mongo_mapping.py` below ~400 lines by removing unused one-doc→one-trajectory helpers **after** scripts no longer import them.

- [ ] **Step 1:** `rg "from yggdrasil.adapters.importers.mongo_mapping"` — update call sites
- [ ] **Step 2:** Delete dead functions; keep only what hierarchical + tests need
- [ ] **Step 3: Commit** `refactor(import): remove legacy mongo_mapping paths`

---

## Wave F — Privacy honesty + scrubber skeleton

### Task F1: Align docs with code (honesty pass)

**Files:**
- Modify: `README.md` Security section — explicit **threat model** bullet list (trusted local; trajectories may contain PII/secrets from agent context; owner is attribution not proof of scrubbing)
- Modify: `surveys/scrubbing_pii_survey.md` — add banner: “Target architecture; not implemented in PoC unless Wave F2 lands”
- Optional: one line in literature survey conclusions

- [ ] **No code** — commit `docs: honest PoC privacy and threat model`

---

### Task F2: Optional regex content scrubber (owner exception)

**Scope:** Minimal **enforceable** step toward surveys—not full LLM redaction.

**Files:**
- Create: `src/yggdrasil/ports/scrubber.py` — protocol `scrub_text(text: str, *, allowed_names: Sequence[str]) -> str`
- Create: `src/yggdrasil/adapters/regex_scrubber.py` — emails, phones (simple), `sk-…` / `Bearer …` patterns; do **not** redact tokens that exactly match `allowed_names` (owner)
- Modify: `session_service.start_trajectory` / `append_step` — optional via env `YGG_SCRUB_CONTENT=1` default **off** for backward compat; when on, scrub `task_text`, `scaffold_text`, step `summary` (not binary payloads)
- Create: `tests/adapters/test_regex_scrubber.py`

- [ ] **Step 1: Tests** for email redaction; owner name preserved; API key pattern redacted

- [ ] **Step 2: Implement + wire behind flag**

- [ ] **Step 3: Document in `.env.example`** `YGG_SCRUB_CONTENT=0`

- [ ] **Step 4: Commit** `feat(privacy): optional regex content scrubber with owner allowlist`

---

## Wave G — PoC security hygiene (not full multi-tenant)

### Task G1: Threat model mini-spec

**Files:**
- Create: `docs/superpowers/specs/2026-06-25-threat-model-poc.md`

Content must state:
- In scope: single operator, trusted MCP clients, local network
- Out of scope: hostile multi-tenant, malicious agents, Qdrant on public internet
- Blockers for multi-user: MCP authn/authz, per-owner RLS, Qdrant API key + network policy, content scrubbing default-on, rate limits

- [ ] **Commit** `docs: PoC threat model and multi-user blockers`

---

### Task G2: docker-compose Qdrant API key optional

**Files:**
- Modify: `docker-compose.yml` — support `QDRANT__SERVICE__API_KEY` from env if set
- Modify: `.env.example` — document `QDRANT_API_KEY`
- README Security — “set Qdrant API key if compose port is reachable beyond localhost”

- [ ] **Commit** `chore(compose): optional Qdrant API key`

---

### Task G3: Do **not** implement MCP auth in this plan

Track as **follow-on plan** `2026-06-XX-multi-tenant-mcp-authz.md` (stub file with goals only, no implementation tasks).

- [ ] **Create stub only:** `docs/superpowers/plans/2026-06-25-multi-tenant-authz-FOLLOWON.md` with goal list from security review

---

## Wave H — Layer cleanup (optional P2)

### Task H1: Move RRF helper under adapters or domain util

**Files:**
- Prefer: `src/yggdrasil/adapters/rrf.py` or `src/yggdrasil/domain/rrf.py` (pure function — domain is OK)
- Update imports in `qdrant_index.py`; delete `services/rrf.py` or re-export for compat
- Keep `tests/services/test_rrf.py` path or move to `tests/domain/test_rrf.py`

- [ ] **Commit** `refactor: RRF pure helper out of services package`

---

## Verification checklist (entire plan)

Run after all implemented waves:

```bash
cd /home/arcyleung/Projects/grok_trace_analysis/yggdrasil
PYTHONPATH=src pytest -q
# expect: prior 34 + new tests all green

# Optional live (needs Qdrant + embed):
# PYTHONPATH=src python scripts/smoke_integration.py
```

**check-work criteria for PASS:**
- No `hash(` used for Qdrant ids in `qdrant_index.py`
- `YGG_FUSION=weighted` either works in a unit test or config rejects it (this plan implements it)
- At least one integration-style lifecycle test exists and passes without live Qdrant
- `index_status` exists and failed embeds do not appear in default search
- Lab gates reject hydration-tagged graded experiences
- README Security states trusted-PoC threat model
- `pytest -q` green

**Out of scope (do not implement in this plan):**
- Full multi-tenant RLS / MCP OAuth
- LLM-based PII scrubber (only regex optional)
- Postgres TrajectoryStore adapter
- Re-embedding all production Mongo data (operator runbook only)

---

## Self-review (writing-plans checklist)

| Review item | Covered by |
|-------------|------------|
| Unstable Qdrant IDs | Task A1 |
| Weighted fusion lie | Task A2 |
| Embed dim | Task A3 |
| Integration tests | Wave B |
| Non-atomic dual store | Wave C |
| SQLite races | Task C3 |
| Hierarchical import | Task C4 + Wave E |
| experience_grade trust | Wave D |
| Ingress complexity | Wave E |
| PII claims vs code | Wave F |
| Multi-tenant / MCP authz | Wave G document + follow-on stub only |
| Layer leak RRF | Wave H optional |

No TBD placeholders in task steps; TDD and commits specified.

---

## Execution handoff

**Plan complete and saved to** `docs/superpowers/plans/2026-06-25-code-review-remediation.md`.

**Two execution options:**

1. **Subagent-Driven (recommended)** — Use superpowers:subagent-driven-development: fresh subagent per task (or per wave), review between tasks, fast iteration. Use worktrees via superpowers:using-git-worktrees if isolating from other work.

2. **Inline Execution** — Use superpowers:executing-plans in this session with checkpoints after each wave (A, B, C, …).

**Suggested order for a solo agent:** A1 → A2 → A3 → B1 → B2 → C1 → C2 → C3 → D1 → F1 (quick docs) → E1–E3 → F2 → G1–G3 → H1 → full `pytest` + requesting-code-review.

**Which approach?**
