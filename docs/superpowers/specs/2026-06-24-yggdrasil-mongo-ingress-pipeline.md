# Yggdrasil MongoDB Trajectory Ingress Pipeline

**Date:** 2026-06-24  
**Status:** Design + live schema reconnaissance  
**Related:** design spec §9 (`2026-06-24-yggdrasil-mcp-trajectory-memory-design.md`), plan T14 (`2026-06-24-yggdrasil-mcp-trajectory-memory.md`)  
**Scope:** Convert `claude_conversations.conversations` into Yggdrasil `Trajectory` / `Step` / `Outcome` / `EffortLedger`, embed multi-aspect views, upsert Qdrant.

---

## 0. Executive summary

Live Mongo is reachable (`claude_conversations.conversations`, ~649k docs). **Actual documents are per-API-request proxy logs**, not the normalized conversation shape assumed by the current T14 mapper fixture (`metadata` + top-level `messages`).

| Assumption (T14 v1 / fixture) | Live reality (sampled) |
|------------------------------|------------------------|
| One doc = one full conversation | One doc = one Anthropic `/v1/messages` request+response |
| Top-level `messages[]` | Messages under `request_body.messages[]` |
| `metadata.title/project/tags` | No top-level title/project; `request_body.metadata.user_id` only |
| Top-level `usage.{input,output}_tokens` | Usage inside `response.streaming_events[].data` (`message_start` / `message_delta`) |
| Roles include `tool` | Anthropic-style: `user`/`assistant` with content blocks `text` / `tool_use` / `tool_result` |
| Idempotency on `_id` only | Need **session-level** collapse: many docs share `session_id`; each carries growing full history |

**Implication:** Ingress needs a **normalize → session-aggregate → segment → map** pipeline. Whole-session embed is weak for multi-step agent sessions (see §B). Recommended strategy: **parent session trajectory + child sub-trajectories** for goal/tool/outcome segments (see §C).

**Connectivity note:** Ping and small projections succeeded; full-document scans and unindexed `count_documents` over 649k rows often timed out. Design below is grounded in 5+ projected samples + session fan-out probes + checked-in fixture.

---

## A. Sample schema observations

### A.1 Collection stats (live)

| Item | Value |
|------|-------|
| Host | `192.168.2.15:27017` (URI from local `mongo_creds.txt`; **do not commit / log**) |
| Database | `claude_conversations` |
| Collection | `conversations` |
| Est. size | ~649,018 documents |
| Connectivity | OK (`admin.ping`) |

### A.2 Top-level keys (live shape — dominant)

Every sampled document had this key set (no top-level `messages` / `metadata` / `usage`):

```
_id, created_at, file_id, filename, model, request_body,
request_headers, request_info, response, session_id, timestamp, user_id
```

| Field | Type / notes (structure only) |
|-------|-------------------------------|
| `_id` | ObjectId — unique per **API request**, not per session |
| `file_id` / `filename` | Log capture id, e.g. `v1-messages-<iso>-<hash>.log` |
| `session_id` | UUID — **session/conversation lineage key** |
| `user_id` | Opaque account/session-scoped string |
| `model` | e.g. `claude-haiku-4-5-*`, `claude-sonnet-4-5-*` |
| `timestamp` | Request time (ISO string with ns precision) |
| `created_at` | Ingest/import time into Mongo |
| `request_info` | `{ method, url, version, timestamp }` |
| `request_headers` | Full HTTP headers including **`Authorization`** (secret risk — see §G) |
| `request_body` | Anthropic Messages API payload (see A.3) |
| `response` | `{ status, headers, is_streaming, streaming_events[] }` (see A.4) |

**Alternate shape (fixture / T14 v1 target):** may exist in tests only (`tests/fixtures/mongo/conversation_sample.json`). Mapper should accept **both** via a normalizer, but live ingress must prioritize the proxy-log shape.

### A.3 `request_body` structure

| Field | Observed |
|-------|----------|
| `model` | Mirrors top-level `model` |
| `max_tokens` | e.g. `32000` |
| `stream` | `true` (all samples streaming) |
| `metadata` | `{ user_id: "user_<hash>_account__session_<session_uuid>" }` — embeds session in string |
| `system` | **array** of blocks: `{ type: "text", text: <system prompt>, cache_control? }` |
| `tools` | array of tool defs; names include agent tools: `Bash`, `Glob`, `Grep`, `Read`, `WebFetch`, `TodoWrite`, `WebSearch`, `Skill`, … (8–20 tools/sample) |
| `thinking` | optional object (present on some Sonnet requests) |
| `messages` | array of `{ role, content }`; **content is always array of blocks** in samples |

**Message roles (live):** only `user` | `assistant` alternating. No separate `tool` role.

**Content block types (live counts on one 33-msg request):**

| Block `type` | Role typically | Mapper step kind |
|--------------|----------------|------------------|
| `text` | user / assistant | `note` / `thought` |
| `tool_use` | assistant | `tool_call` (name in block, not OpenAI `tool_calls[]`) |
| `tool_result` | user (tool results returned as user turn) | `tool_result` |

**Message length distribution (session fan-out sample):** within one `session_id`, many request docs; `request_body.messages` length grows as history accumulates (e.g. session A: 1, 3, 5, 7, 9, … 41 msgs; session B: interleaved 1-msg “delta-only” requests and full-history requests). **Last request per session usually holds the fullest transcript.**

### A.4 `response` structure

| Field | Observed |
|-------|----------|
| `status` | `200` (samples) |
| `is_streaming` | `true` |
| `headers` | Anthropic rate-limit / org / request-id headers (PII-adjacent: org id) |
| `streaming_events` | array of `{ data: { type, ... } }` — SSE-style event objects |

**Event `data.type` histogram (one request, 34 events):**

| type | count | notes |
|------|------:|-------|
| `content_block_delta` | 28 | incremental text/tool args |
| `message_start` | 1 | includes `message.usage` (input/cache/output tokens) |
| `message_delta` | 1 | final `usage.output_tokens` update |
| `message_stop` | 1 | terminal |
| `content_block_start` / `stop` | 1 each | block boundaries |
| `ping` | 1 | ignore |

**Usage fields (from stream, not top-level `usage`):**

```
input_tokens, output_tokens,
cache_creation_input_tokens, cache_read_input_tokens,
cache_creation.{ephemeral_5m_input_tokens, ephemeral_1h_input_tokens},
service_tier
```

Mapper should map these into `EffortTotals.llm_tokens_in` / `llm_tokens_out` (optionally store cache tokens in effort `notes` or line_items until schema extends).

### A.5 Sampled document metrics (redacted)

| Sample | session_id | model family | n_msgs (request history) | n_tools | n_stream_events | system | thinking |
|--------|------------|--------------|--------------------------:|--------:|----------------:|--------|----------|
| doc0 | `8efd1fed…` | haiku-4-5 | 33 | 8 | 34 | array | no |
| doc1 | `4a16709e…` | sonnet-4-5 | 11 | 14 | 66 | array | no |
| doc2 | `881ab071…` | haiku-4-5 | 11 | 13 | 52 | array | no |
| doc3 | `2d14d1ff…` | sonnet-4-5 | 13 | 20 | 142 | array | yes |
| doc4 | `0d9ac112…` | sonnet-4-5 | 33 | 14 | 37 | array | no |

**Role sequence pattern (all samples):** `user, assistant, user, assistant, …` ending on `user` (often latest tool_result or new goal turn awaiting assistant completion in that request snapshot).

**Content placeholders only (never store/log full text in docs/tests):**

- First user text block: `[USER_GOAL_OR_INSTRUCTION]`
- Assistant text: `[ASSISTANT_PLAN_OR_NARRATION]`
- `tool_use.name`: e.g. `Read`, `Bash`, `Grep`
- `tool_result`: `[TOOL_OUTPUT_SNIPPET]`
- System: `[AGENT_SYSTEM_PROMPT_WITH_TOOLS_POLICY]`

### A.6 Fixture shape (T14 / unit tests — still required)

`tests/fixtures/mongo/conversation_sample.json` remains the **canonical unit-test shape**:

- `_id.$oid`, `metadata.{title, project, tags, created_at, updated_at}`
- top-level `messages[]` with optional OpenAI-style `tool_calls[]` and role `tool`
- top-level `usage.{input_tokens, output_tokens}`

Ingress normalizer output should converge both shapes to one internal **ConversationIR** before mapping.

### A.7 Gap vs current `mongo_mapping.py`

| Mapper expects | Live provides | Action |
|----------------|---------------|--------|
| `doc.messages` | `doc.request_body.messages` | normalize first |
| `doc.metadata.title/project/tags` | absent; derive from first user text / system / tools | heuristic task/scaffold |
| `msg.tool_calls[]` (OpenAI) | `content[].type == tool_use` | block-aware step extraction |
| `role == tool` | `tool_result` in user content | remap |
| `doc.usage` | stream event usage | extract from `response.streaming_events` |
| traj id `mongo-{_id}` | `_id` = one request; duplicates session history | prefer `mongo-session-{session_id}` parent + segment children |
| status from last assistant keywords | weak on multi-goal agent logs | segment-level outcome + parent rollup |

---

## B. Why whole-session embed is weak for long multi-step conversations

Yggdrasil search embeds two aspects (`task_text`, `scaffold_text`) per trajectory and fuses via RRF/weighted scores. A single embed per entire agent session fails when sessions are long, multi-goal, and tool-heavy:

1. **Semantic dilution**  
   One session may contain: orient → read files → fix bug A → run tests → pivot to bug B → write docs. A single `task_text` (title + first user message only, as in current mapper) misses later goals; stuffing the full transcript into `task_text`/`scaffold_text` exceeds useful embed context and mixes unrelated intents.

2. **Aspect mismatch**  
   - `task` aspect should match *what problem is being solved*.  
   - `scaffold` aspect should match *environment/approach* (repo, tools, stack, constraints).  
   Whole-session scaffolds that concatenate early assistant hints (mapper takes first ~12 messages) under-represent mid-session tool regimes (e.g. shifted from `Read`/`Grep` to `Bash` test loops).

3. **History duplication in source**  
   Live Mongo stores **many requests per session**, each with nearly full prior `messages`. Importing every doc as a trajectory duplicates the same early turns N times with different terminal states — search noise and wasted embed cost.

4. **Outcome ambiguity**  
   Keyword status inference on the last assistant message (`done`/`failed`) is unreliable across multi-outcome sessions (success on subtask A, fail on B, partial overall).

5. **Recall use-case mismatch**  
   Mid-task agents usually want: “strategies for *this subgoal* under *this scaffold*,” not “anything that happened in a 2-hour session.” Segment-level trajectories align with `search_strategies` intent.

6. **Cost/latency**  
   ~649k request docs; if collapsed naively without segmentation, still potentially tens of thousands of sessions × 2 embeddings. Segmenting increases count but improves precision; embedding every raw request doc is worst of both worlds (duplicates + wrong grain).

**Design stance:** Store **parent = session rollup** (lineage, totals, weak global task) and **children = sub-trajectories** (embeddable units). Search defaults to children; parent used for drill-down / `get_trajectory` on full session.

---

## C. Decomposition strategy: parent session + child sub-trajectories

### C.1 Entity model

```
Parent Trajectory (session)
  id: mongo-session-{session_id}
  external_refs: { source, db, collection, session_id, kind: "session_parent" }
  task_text: session-level goal summary (first user goal + optional title heuristic)
  scaffold_text: system/tool/environment summary (stable across session)
  progress/effort/outcome: rollups
  tags: ["mongo_import", "session_parent", model_family, ...]
  NOT primary embed target (optional embed with low weight / skip in v1)

Child Trajectory (sub-trajectory / segment)
  id: mongo-session-{session_id}-seg-{seg_index:04d}
  external_refs: {
    source, db, collection,
    session_id,
    parent_trajectory_id: "mongo-session-{session_id}",
    segment_index, segment_kind,
    request_ids: [<mongo _ids contributing to segment>],  # optional audit
    kind: "session_segment"
  }
  task_text / scaffold_text: segment-local (embedded)
  steps: only steps in [seg_start, seg_end)
  outcome/effort: segment-local when inferable
```

**Linking conventions (no schema migration required for PoC):**

| Mechanism | Usage |
|-----------|--------|
| `external_refs.parent_trajectory_id` | Child → parent (primary) |
| `external_refs.session_id` | Shared join key for all imports from session |
| `external_refs.kind` | `session_parent` \| `session_segment` \| `request_doc` (legacy single-doc) |
| Parent `progress.milestones[]` | Segment ids or short labels in order |
| Parent `tags` | include `has_segments` |

Optional future: first-class `parent_id` on `Trajectory` model; until then `external_refs` is sufficient and searchable via payload filters.

### C.2 Segmentation heuristics

Apply on the **canonical session transcript** (built by taking the request doc with max `len(messages)` per `session_id`, or merging by appending only new suffix turns — see §E extract).

Segments are half-open index ranges over ordered **logical turns** (a logical turn = one assistant cycle: optional user text/tool_results → assistant text/tool_uses).

| Heuristic | Trigger | Segment boundary | Rationale |
|-----------|---------|------------------|-----------|
| **H1 — User goal shift** | New `user` message with substantial `text` block (not only `tool_result`); optional embedding/sim drop vs prior goal; keyword cues (`also`, `next`, `instead`, `new task`, `ignore previous`) | Close previous segment before this user text | New intent |
| **H2 — Tool-phase boundary** | Dominant tool cluster changes (e.g. read/search phase → edit/test phase → web phase); detect via tool_use name histograms in sliding window | Boundary at phase change | Scaffold/approach shift |
| **H3 — Outcome checkpoint** | Assistant text suggests completion/failure (`done`, `fixed`, `tests pass`, `blocked`, `can't`); or explicit `TodoWrite` completing a todo; or `stop_reason` end_turn after non-tool reply | End segment; start new if more user text follows | Captures sub-outcome |
| **H4 — Max steps / window** | Segment exceeds `MAX_SEGMENT_STEPS` (default **24** logical steps) or `MAX_SEGMENT_CHARS` (default **24k** summary chars) | Hard split at nearest user-text boundary | Embed context guard |
| **H5 — Time gap** | Optional: if request timestamps available across session docs, gap > `SESSION_IDLE_SPLIT` (e.g. 45m) | Split | Distinct work blocks |
| **H6 — System/tool set change** | Rare: tools list or system prompt hash changes mid-session | Split; refresh scaffold | Environment change |

**Default v1 policy (deterministic, fixture-testable):**

1. Build canonical transcript turns.  
2. Run H1 + H3 on each user/assistant boundary.  
3. Enforce H4 max window.  
4. H2 as soft hint only (log `segment_kind=tool_phase` when triggered).  
5. H5/H6 optional flags in CLI.

**Minimum segment size:** merge segments with < 3 steps into neighbor unless H1 goal shift with clear new user text.

### C.3 What gets embedded per segment

| Aspect | Parent (session) | Child (segment) |
|--------|------------------|-----------------|
| **`task_text`** | First user goal text (truncated) + optional derived title from first 80 chars; maybe segment count summary | Segment goal: triggering user `text` blocks in segment (concat, max ~2k chars); fallback to parent goal if segment starts mid-tool-loop |
| **`scaffold_text`** | System prompt snippet (redacted/truncated), tool name list, model, agent markers from system (`coding agent`, cwd hints if present in system only as labels) | **Snapshot at segment start:** same stable scaffold base + segment-local approach hints (first assistant plan text in segment, up to 4 tool names used in segment prefix) |
| **Not embedded** | Full tool outputs, headers, auth, full system if huge | Same; steps store summaries/payload refs only |

**`embed_view_version`:** keep `coding_v1` for segments; parent may use `coding_v1_session` later if parent embedded.

**Re-embed triggers on import update:** segment `task_text`/`scaffold_text` change, or `--reembed`.

### C.4 Parent links & idempotency keys

| Entity | Idempotency key (`external_refs`) |
|--------|-----------------------------------|
| Parent | `source=mongo`, `kind=session_parent`, `session_id=<uuid>` |
| Child | `source=mongo`, `kind=session_segment`, `session_id=<uuid>`, `segment_index=<int>` |
| Legacy single-doc (T14 shim only) | `source=mongo`, `id=<_id>`, `db`, `collection` |

Store lookup: extend `find_by_external_ref` usage patterns:

- Parent: `find_by_external_ref("mongo", f"session:{session_id}")` **or** query `external_refs.session_id` + `kind` (may need store helper `find_by_external_refs_match`).
- Practical PoC: encode composite external id:  
  - parent external_id = `session:{session_id}`  
  - child external_id = `session:{session_id}:seg:{index}`  
  Keep `external_refs` rich for filters; use composite string for existing `find_by_external_ref(source, external_id)` index.

Trajectory ids (stable, human-grepable):

- Parent: `mongo-session-{session_id}`
- Child: `mongo-session-{session_id}-seg-{index:04d}`

---

## D. Mapping table: Mongo → Yggdrasil domain

### D.1 Normalizer: live doc / fixture → `ConversationIR`

Internal IR (not persisted):

```
ConversationIR:
  session_id: str | None
  request_id: str          # str(_id)
  model: str | None
  created_at, updated_at: datetime
  title: str | None        # fixture metadata.title only unless derived
  project: str | None
  tags: list[str]
  system_text: str         # joined system text blocks (truncated for scaffold)
  tool_names: list[str]
  messages: list[IRMessage]
  usage: IRUsage | None    # aggregated tokens
  source_shape: "proxy_log" | "fixture_v1"
  raw_external: dict       # lineage fields only
```

| Source (live proxy_log) | Source (fixture_v1) | → IR field |
|-------------------------|---------------------|------------|
| `session_id` | — (synthetic from `_id`) | `session_id` |
| `str(_id)` | `str(_id)` / `$oid` | `request_id` |
| `model` / `request_body.model` | — | `model` |
| `timestamp` / `created_at` | `metadata.created_at` | `created_at` |
| `created_at` (ingest) | `metadata.updated_at` | `updated_at` |
| — | `metadata.title` | `title` |
| — | `metadata.project` | `project` |
| derived `["mongo_import", model]` | `metadata.tags` | `tags` |
| `request_body.system[].text` | — | `system_text` |
| `request_body.tools[].name` | — | `tool_names` |
| `request_body.messages` | `messages` | `messages` (after block flatten) |
| stream `message_delta.usage` / `message_start` | `usage` / last msg `usage` | `usage` |
| always strip `request_headers` | n/a | **never copy secrets into IR/payload** |

### D.2 `IRMessage` / content blocks → `Step`

| IR / Mongo message feature | `Step.kind` | `summary` (max ~500) | `payload` keys (no secrets) |
|----------------------------|-------------|----------------------|------------------------------|
| `role=user`, text blocks only | `note` | user text | `role, block_types` |
| `role=user`, has `tool_result` blocks | `tool_result` (one step per block or merged) | tool_result snippet / tool_use_id | `role, tool_use_id, is_error?` |
| `role=assistant`, text blocks | `thought` | assistant text | `role` |
| `role=assistant`, `tool_use` blocks | `tool_call` | `tool_call: {name}` | `tool_use_id, name, input_keys_only` (avoid full args if huge/sensitive) |
| fixture `tool_calls[]` | `tool_call` | function name | existing mapper behavior |
| fixture `role=tool` | `tool_result` | content | existing mapper behavior |
| optional checkpoint heuristic (H3) | step with `is_checkpoint=true` | outcome phrase | `checkpoint_reason` |

`Step.trajectory_id` / `seq`: assigned per target trajectory (parent optional full trace vs children segment-local seq starting at 1).

### D.3 Trajectory fields

| Yggdrasil `Trajectory` | Parent session | Child segment | Legacy single-request (debug) |
|------------------------|----------------|---------------|-------------------------------|
| `id` | `mongo-session-{session_id}` | `mongo-session-{session_id}-seg-{i:04d}` | `mongo-{request_id}` |
| `domain` | `coding` (default; override if system implies otherwise) | same | same |
| `status` | rollup: all children success→success; any fail→fail/partial; else partial | segment inference (H3 + last assistant text) | current `_infer_status` |
| `task_text` | §C.3 parent | §C.3 child | title + first user (mapper today) |
| `scaffold_text` | system/tools/model | segment-start snapshot | project + early hints |
| `tags` | session tags + `session_parent` | segment tags + `session_segment` + `seg_kind:*` | `mongo_import` |
| `external_refs` | §C.4 parent | §C.4 child | T14 v1 refs + `id=_id` |
| `progress` | phase=`imported_session`, steps_count=total, milestones=seg labels | phase=`imported_segment`, steps_count=seg steps | phase=`imported` |
| `outcome` | optional rollup summary | segment outcome if terminal-ish | rarely |
| `effort` | sum child efforts / sum request usages in session | segment usage if attributable else empty | request usage |
| `embed_view_version` | `coding_v1` or skip embed | `coding_v1` | `coding_v1` |
| `index_state` | `pending`/`indexed` if embedded | primary index target | as importer sets |
| `created_at` / `updated_at` | min/max request timestamps | segment span | request timestamp |
| `finalized_at` | if parent status terminal | if segment status terminal | if inferred terminal |

### D.4 `Outcome`

| Field | Mapping heuristic |
|-------|-------------------|
| `terminal_status` | mirror segment/parent `status` when not `open` |
| `summary` | last assistant text in scope (truncated) or template `"imported segment {i} of session {sid}"` |
| `signals` | `{ source: mongo, model, tool_names_sample, stop_reason? }` |
| `goal_satisfied` | true if success keywords + no trailing error tool_result; else null/false |
| `remaining_work` | if partial: last user text or blocker phrase |

### D.5 `EffortLedger` / `EffortTotals`

| Effort field | Live proxy_log | Fixture |
|--------------|----------------|---------|
| `totals.llm_tokens_in` | `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` (document formula in notes) **or** map only `input_tokens` in v1 and put cache in `notes` | `usage.input_tokens` / `prompt_tokens` |
| `totals.llm_tokens_out` | `message_delta.usage.output_tokens` (prefer final) | `usage.output_tokens` / `completion_tokens` |
| `line_items` | optional per-request usage when building parent | single line |
| `started_at` / `ended_at` | min/max timestamps in scope | metadata times |
| `notes` | `cache_read=…; cache_creation=…; service_tier=…` | — |

**Wall-clock / GPU / $ fields:** leave null unless derived later from timestamps + model price table (out of scope for shim v1).

### D.6 Status inference (reuse + extend)

Keep `_infer_status` keyword pass on last assistant **text** in scope; extend:

- If last blocks are only `tool_use` without completion text → `partial` / treat as `open` segment.
- If tool_result `is_error` dominant at end → lean `fail`.
- Parent rollup: conservative `partial` unless all segments terminal and agree.

---

## E. Ingress pipeline stages

**Primary interface:** batch CLI (`scripts/import_mongo_for_testing.py`) — already sketched in T14.  
**Secondary:** optional MCP `import_mongo_conversations` admin tool (post-CLI).

```
┌─────────┐   ┌──────────┐   ┌─────────┐   ┌────────────┐   ┌──────────────────┐   ┌─────────────┐
│ extract │ → │ segment  │ → │  map    │ → │ store      │ → │ embed task/      │ → │ upsert      │
│ Mongo   │   │ session  │   │ IR→dom  │   │ SQLite     │   │ scaffold        │   │ Qdrant      │
└─────────┘   └──────────┘   └─────────┘   └────────────┘   └──────────────────┘   └─────────────┘
     │              │              │              │                    │                    │
     └──────── idempotency keys / dry-run / limit / session filter ───┴────────────────────┘
```

### E.1 Stage 1 — Extract

1. Resolve URI: `MONGO_URI` env **or** first `mongodb://…` line in `mongo_creds.txt` / `YGG_MONGO_CREDS_FILE` (never print URI).  
2. Read `claude_conversations.conversations` with **projections** excluding `request_headers.Authorization` (or entire `request_headers` if not needed).  
3. Filters (CLI): `--limit`, `--session-id`, `--since`, `--model`, `--request-id`.  
4. **Session collapse:**  
   - Group by `session_id`.  
   - For each session, select **canonical doc** = max `len(request_body.messages)` (tie-break latest `timestamp`).  
   - Optionally verify by scanning other docs only for usage sum / timestamps (projected fields).  
5. Run **normalizer** → `ConversationIR` (proxy_log or fixture_v1).  
6. Emit extract stats: sessions_seen, requests_seen, skipped_no_session, errors.

**Performance:** prefer indexed `session_id` if available; otherwise bounded `.find().limit()` batches. Avoid collection-wide `count_documents` without filter.

### E.2 Stage 2 — Segment

Input: one `ConversationIR` with full ordered messages.  
Output: `list[SegmentSpan(start_turn, end_turn, kind, goal_text)]` + parent metadata.

Apply §C.2 heuristics; attach `segment_index`.

**CLI flags:** `--no-segment` (legacy whole-IR → one trajectory, T14 compat), `--max-segment-steps`, `--max-segment-chars`.

### E.3 Stage 3 — Map

For each session:

1. `map_parent(ir, segments) -> Trajectory` (no or minimal steps; optional step count only).  
2. For each segment: `map_segment(ir, span) -> MappedTrajectory` (trajectory + steps).  
3. Reuse / evolve `mongo_mapping.py`:  
   - `normalize_mongo_doc(doc) -> ConversationIR`  
   - `map_conversation_ir(ir, *, mode=parent|segment|legacy) -> MappedTrajectory`  
   - Keep `map_mongo_conversation_doc(doc)` as thin wrapper: normalize + legacy single trajectory for tests.

### E.4 Stage 4 — Store SQLite

Use existing ports:

- `TrajectoryStore.upsert_imported(trajectory, steps)`  
- `find_by_external_ref("mongo", external_id)` for idempotency  

Order: upsert **parent first**, then children (so parent id exists for refs).  
On update: replace steps for that trajectory id; refresh `updated_at`, effort, progress.

**Idempotency behavior:**

| Situation | Action |
|-----------|--------|
| external_id unseen | insert trajectory + steps; `index_state=pending` |
| external_id exists, `--reembed` false | update SQLite fields/steps; leave vectors unless task/scaffold changed (optional hash compare) |
| external_id exists, `--reembed` | update + force embed/upsert Qdrant |
| `--dry-run` | map only; no writes |

### E.5 Stage 5 — Embed task / scaffold

`EmbedService.index_trajectory(traj, reembed=…)` using configured embed model/dim (`YggConfig`).  
**Default embed targets:** children only.  
**Parent embed:** off by default (`--embed-parents` to enable).

Aspect texts from trajectory fields only (already bounded in mapper).

### E.6 Stage 6 — Upsert Qdrant

Via existing vector adapter: multi-vector points for `task` + `scaffold`, payload from `payload_from_trajectory` (status, domain, effort totals, external_refs, tags).  

**Filters enabled by ingress:**

- `external_refs.source == mongo`  
- `tags` contains `session_segment`  
- `external_refs.session_id`  
- `external_refs.parent_trajectory_id` (if payload flattens external_refs)

Set `index_state=indexed` on success; `stale`/`error` on embed/index failure (importer already catches `EmbedFailedError` / `IndexFailedError`).

### E.7 Idempotency keys (summary)

```
parent_external_id  = "session:{session_id}"
child_external_id   = "session:{session_id}:seg:{segment_index}"
legacy_external_id  = "{mongo_oid}"              # request-level only
```

`external_refs` always includes `db=claude_conversations`, `collection=conversations`, plus session/segment metadata.

### E.8 CLI sketch (extends T14)

```bash
python scripts/import_mongo_for_testing.py \
  --limit 50 \
  --dry-run \
  --creds-file mongo_creds.txt

python scripts/import_mongo_for_testing.py \
  --session-id 8efd1fed-68fd-4ca2-bea9-678b8c617ec6 \
  --reembed

python scripts/import_mongo_for_testing.py \
  --limit 20 --no-segment          # T14 legacy single trajectory per canonical doc

python scripts/import_mongo_for_testing.py \
  --max-segment-steps 24 --embed-parents
```

**Safety defaults:** strip/drop auth headers at extract; redact fixture generator for tests; log counts not content.

### E.9 Optional MCP stage (later)

Tool `import_mongo_conversations`: parameters mirror CLI subset (`limit`, `session_id`, `dry_run`, `reembed`); calls same `MongoConversationImporter` service. Admin-only / testing flag in server config.

---

## F. Implementation phases / PR plan

Aligns with design P4 / plan T14 / PR6, extended for live schema + segmentation.

### Phase 0 — Recon lock-in (this doc) ✅

- Live schema notes, gap analysis, segment strategy.

### Phase 1 — Normalizer + fixtures (PR: `feat(importers): mongo normalizer dual-shape`)

| Deliverable | Detail |
|-------------|--------|
| `normalize_mongo_doc` | proxy_log + fixture_v1 → `ConversationIR` |
| Fixtures | keep `conversation_sample.json`; add `conversation_proxy_log_sample.json` (redacted, tiny) |
| Tests | block types, usage extraction, no headers in IR |
| Mapper compat | `map_mongo_conversation_doc` uses normalizer + legacy map |

### Phase 2 — Mapper v2 + segmentation (PR: `feat(importers): session segment mapping`)

| Deliverable | Detail |
|-------------|--------|
| Segment heuristics H1/H3/H4 | pure functions over IR messages |
| Parent/child trajectory builders | external_refs scheme §C.4 |
| Effort/status/outcome | §D tables |
| Unit tests | synthetic IR transcripts (goal shift, max window, tool_result user turns) |

### Phase 3 — Importer + store idempotency (PR: `feat(importers): session-aware mongo importer`)

| Deliverable | Detail |
|-------------|--------|
| `MongoConversationImporter.import_session` | extract canonical → segment → parent+children upsert |
| `import_many` | group by session_id when present |
| Store | ensure `find_by_external_ref` works with composite ids (T5 dependency) |
| Stats | sessions, segments imported/updated, embed failures |

### Phase 4 — CLI productionize (PR: part of T14 commit or follow-up)

| Deliverable | Detail |
|-------------|--------|
| `scripts/import_mongo_for_testing.py` | flags §E.8; never print URI |
| pyproject optional `[mongo]` pymongo extra | documented in README |
| Dry-run pretty print | ids, segment counts, task/scaffold char lens only |

### Phase 5 — Embed/Qdrant integration smoke (PR: chore/docs or with T14)

| Deliverable | Detail |
|-------------|--------|
| Manual runbook steps | import 5 sessions → search segment by task snippet |
| Verify payload filters | `source=mongo`, parent/child refs |
| Dim/model lock check | fail fast if collection dim ≠ config |

### Phase 6 — Optional MCP tool (later PR)

| Deliverable | Detail |
|-------------|--------|
| `import_mongo_conversations` | thin wrapper; gate behind config |

### Phase 7 — Hardening (backlog)

- Session merge correctness if canonical doc not latest  
- H2 tool-phase segmentation  
- PII scrubber for step payloads  
- Incremental import watermark (`created_at` / `_id` resume)  
- Postgres trajectory source (spec §9.1) parallel adapter  

### Suggested git commits (plan language)

1. `feat(importers): mongo normalizer for proxy-log + fixture shapes`  
2. `feat(importers): parent/child session segmentation mapper`  
3. `feat(importers): session-aware mongo importer + CLI flags`  
4. `test(importers): redacted proxy-log fixture and segment cases`  
5. `docs: mongo ingress pipeline + runbook notes` (this spec linked from README)

---

## G. Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| **PII / secrets in transcripts** | Critical | Never persist `request_headers` / `Authorization`; truncate tool args/outputs; redact fixtures; local-only creds file gitignored; avoid logging message bodies |
| **System prompts & code in tool_results** | High | Embed only task/scaffold summaries; steps store short `summary`; full payload optional/off by default (`--include-step-payloads`) |
| **user_id / org headers** | Medium | Hash or drop in `external_refs`; do not index raw user_id in Qdrant payload |
| **Embed cost / latency** | High at 649k requests | Session-collapse first (>>10× reduction); embed segments not every request; `--limit` for PoC; batch embed API if available |
| **Duplicate imports** | Medium | Composite idempotency keys; upsert_imported; tests for re-run stability |
| **Duplicate semantic vectors** | Medium | Do not import every request doc; one parent + N segments per session |
| **Qdrant dim / model lock** | High | Single embed model/dim in config; refuse upsert on mismatch; document recreation procedure; `embed_view_version` for future migrations |
| **Mapper/fixture drift vs live** | High (realized) | Dual-shape normalizer; live recon in this doc; integration test optional/skip |
| **Weak status/outcome** | Medium | Segment-level inference; default `partial`; don't overclaim success in search filters unless confident |
| **Mongo timeouts / huge docs** | Medium | Projections; session-targeted queries; socket timeouts; avoid full collection scans |
| **Streaming event variance** | Low-Med | Usage extraction defensive; works if only request messages present (effort empty) |
| **Legal / retention** | Context-dependent | Ingress is testing shim; production use needs data governance review |

---

## H. Concrete next engineering tasks (checklist)

### H.1 Schema & normalizer

- [x] Implement `ConversationIR` dataclass + `normalize_mongo_doc(doc) -> ConversationIR` (`mongo_normalize.py`)
- [x] Support live fields: `request_body.messages`, system array, tools, stream usage walk
- [x] Support fixture fields: top-level `messages`, `metadata`, `usage`
- [x] Explicitly drop/ignore `request_headers` (especially `Authorization`)
- [x] Session aggregate: `aggregate_session_irs` / `normalize_and_aggregate_docs` (pick fullest request per session)
- [x] Redacted proxy-log fixture: `tests/fixtures/mongo/conversation_proxy_log_sample.json`
- [x] Tests: `tests/adapters/importers/test_mongo_normalize.py`
- [ ] Add redacted `tests/fixtures/mongo/conversation_proxy_log_sample.json`
- [ ] Unit tests: block_type → step kind; usage from `message_delta`; fixture regression

### H.2 Segmentation

- [ ] Implement turn iterator over IR messages (user/assistant cycles, tool_use/tool_result blocks)
- [ ] Implement H1 goal-shift splitter (user text vs tool_result-only user turns)
- [ ] Implement H3 checkpoint splitter (assistant completion cues)
- [ ] Implement H4 max steps/chars enforcement
- [ ] Build parent + child `external_refs` / trajectory ids per §C.4
- [ ] Tests with multi-goal synthetic transcript (assert segment count & boundaries)

### H.3 Mapper evolution (`mongo_mapping.py`)

- [ ] Refactor `map_mongo_conversation_doc` → normalize + `map_ir_legacy`
- [ ] Add `map_ir_session_parent` / `map_ir_segment`
- [ ] Map Anthropic `tool_use` / `tool_result` blocks (not only OpenAI `tool_calls`)
- [ ] Improve effort mapping for cache token fields (notes or totals policy documented)
- [ ] Derive scaffold from `system_text` + `tool_names` + model when no `metadata.project`

### H.4 Importer & store

- [ ] Session grouping in `MongoConversationImporter.import_many`
- [ ] Canonical doc selection per `session_id`
- [ ] Upsert parent then children; stats include `segments_imported`
- [ ] Verify `SqliteTrajectoryStore.find_by_external_ref` + `upsert_imported` with composite external ids
- [ ] Idempotency test: import twice → stable ids, updated timestamps, no dup rows

### H.5 CLI & config

- [ ] Wire `scripts/import_mongo_for_testing.py`: `--limit`, `--dry-run`, `--reembed`, `--session-id`, `--no-segment`, `--max-segment-steps`, `--creds-file`, `--mongo-uri`
- [ ] Ensure optional dependency: `pymongo` via `[mongo]` extra
- [ ] Never log/print Mongo URI or Authorization material
- [ ] Exit non-zero on partial embed failures beyond threshold (configurable)

### H.6 Embed / Qdrant

- [ ] Default embed children only; flag for parents
- [ ] Smoke: 3–5 real sessions (local) → `search_strategies` returns `session_segment` hits with parent ref in payload
- [ ] Assert Qdrant collection dim matches embedder; document reset steps in README
- [ ] Confirm effort totals appear in search hits for imported segments when usage present

### H.7 Docs & safety

- [ ] Link this spec from design §9 and plan T14 (“live schema: proxy logs; segment ingress”)
- [ ] README runbook: dry-run → limited import → search example
- [ ] Confirm `.gitignore` has `mongo_creds.txt`, `.env`
- [ ] Add security note: imported corpora may contain private code/secrets in tool output — local/dev only

### H.8 Optional / backlog

- [ ] MCP tool wrapper `import_mongo_conversations`
- [ ] Resume cursor / watermark for incremental backfill
- [ ] H2 tool-phase segmentation
- [ ] Stronger PII scrubber hooks
- [ ] Parent rollup effort = sum(segment efforts) consistency job

---

## Appendix I — End-to-end example (illustrative ids only)

```
Mongo requests (session 8efd1fed…): 15+ docs, message lens [1..41]
        ↓ extract canonical (41 msgs)
ConversationIR (proxy_log)
        ↓ segment (e.g. 3 children)
Parent  mongo-session-8efd1fed-…           external_id=session:8efd1fed-…
Child0  mongo-session-8efd1fed-…-seg-0000  goal: initial user task
Child1  …-seg-0001                         goal: follow-up after checkpoint
Child2  …-seg-0002                         goal: late pivot / overflow window
        ↓ SQLite upsert_imported × 4
        ↓ embed Child0..2 (task+scaffold) → Qdrant
Search("fix flaky auth test") → Child0/1 hits; payload.external_refs.parent_trajectory_id → parent
```

## Appendix II — References

| Doc / code | Path |
|------------|------|
| Design §9 Mongo shim | `docs/superpowers/specs/2026-06-24-yggdrasil-mcp-trajectory-memory-design.md` |
| Plan T14 | `docs/superpowers/plans/2026-06-24-yggdrasil-mcp-trajectory-memory.md` |
| Mapper (current) | `src/yggdrasil/adapters/importers/mongo_mapping.py` |
| Importer (current) | `src/yggdrasil/adapters/importers/mongo_conversation_importer.py` |
| Domain models | `src/yggdrasil/domain/models.py` |
| Store port | `src/yggdrasil/ports/store.py` |
| Fixture v1 | `tests/fixtures/mongo/conversation_sample.json` |
| Creds (local only) | `mongo_creds.txt` (gitignored) |

---

*Generated from live Mongo reconnaissance (projected samples; secrets redacted) plus existing T14 mapper/importer code review. Re-run recon if collection schema migrates away from per-request proxy logs.*
