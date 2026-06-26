# Mongo multi-lane (B′) and within-lane subtask decomposition

**Date:** 2026-06-26  
**Status:** Direction approved (B′); within-lane slicing design for implementation follow-on  
**Related:** `data_auditing/`, prior single-canonical failure audit

## 1. Lane identity (refined B′)

**Problem:** One `session_id` mixes orchestrator + subagents. Max `message_count` keeps one snapshot (often longest Haiku list). Family-only lanes miss **two Haikus with different systems**. `user_id` is **not** a subagent id (stem is unique per session; suffix is `__session_<session_id>`).

**Lane key:**

```text
lane_key = sha256(normalize(system))[:16] + "|" + full_model_name
```

- `normalize(system)`: flatten Anthropic system blocks to text, light whitespace collapse (see `data_auditing/lib_session_metrics.normalize_system_text`).
- **Full model string** (e.g. `claude-haiku-4-5-20251001`), not family only — distinguishes model SKUs if needed.
- **Parent** remains `session_id` / `mongo-session-{id}` for org ownership and lineage.
- **Per lane maximizer:** among proxy docs with that `lane_key`, pick max `(len(messages), message_chars, recency)`.

**Hydration shape (target):**

| Artifact | id pattern | Embed? |
|----------|------------|--------|
| Session parent | `mongo-session-{sid}` | optional / metadata only |
| Lane child | `mongo-session-{sid}-lane-{sys16}-{model_slug}` | **yes** (task/scaffold from that snapshot) |

Tags: `lane`, `model:<name>`, `system_hash:<16>`. `raw_external.lane_key` stores the full key.

**Retrieval:** Lab still filters `tenant_id=lab` only; more lane points ⇒ more strategies, not tighter owner isolation.

## 2. Within-lane subtask / outcome slices

Even **one** lane maximizer (e.g. long Opus parent trajectory) can contain **multiple goals** completed in sequence (implement A → fix B → refactor C). Lanes solve **parallel agents**; **slices** solve **serial goals on one agent timeline**.

### 2.1 What we are decomposing

Input: **one** `ConversationIR` (messages list) for a lane maximizer — a single growing (or mostly growing) transcript.

Output: ordered **slices** `{start_msg_idx, end_msg_idx, task_text, outcome_hint?}` suitable for:

- `session_segment` children **under the lane** (or under parent with `lane_key` tag), and/or  
- embeddable trajectories `…-lane-…-slice-0001`.

### 2.2 Decomposition approaches (choose / combine)

| Approach | Mechanism | Pros | Cons |
|----------|-----------|------|------|
| **A. Goalish user-turn windows** | Split on substantial `user` turns matching goal language (implement/fix/add/…) | Simple; matches chat UX | Misses tool-only progress; multilingual |
| **B. Existing `mongo_segment` heuristics** | Reuse window_split / goal segmenter on **lane** messages only | Already in codebase | Tuned for single canonical, not multi-lane parents |
| **C. Assistant outcome markers** | Boundaries after assistant turns that look “done” (tests pass, committed, “fixed”) | Captures outcomes | Noisy; model-dependent |
| **D. Embedding topic shift** | Chunk by user turns; cluster or cosine-drop between consecutive user embeds | Catches semantic goal changes | Needs embed calls; cost |
| **E. LLM slice labeler** | One Haiku pass: “list goal segments with message indices” | High quality | Cost/latency; use offline hydration only |
| **F. Prefix-stable windows** | Along **time-ordered proxy docs in the same lane_key**, treat each maximizer growth step; slice when **new user turn appears** that isn’t prefix continuation | Aligns with proxy log nature | Requires all lane docs, not only maximizer |

**Recommended pipeline (practical):**

1. **Lane maximizer** (B′) — mandatory.  
2. **Offline segment** with **B** (`mongo_segment` / goal windows) on maximizer messages — default.  
3. If `suggested_slices` heuristic (A) **> 1** and segmenter returns 1 chunk only, optionally **E** for that lane only (expensive sessions).  
4. Do **not** slice on every user turn; require min chars / goalish signal to avoid tool-noise shards.

### 2.3 Hierarchy

```text
mongo-session-{sid}                    # parent, owner, tenant, request_ids
  mongo-session-{sid}-lane-{sys}-{mod} # embeddable strategy (lane maximizer)
    …-slice-0000                       # optional subtask embed (if decomposed)
    …-slice-0001
```

Search primarily on **lane** and **slice** children; parent for handoff / get_trajectory lineage.

### 2.4 Outcomes

Per slice (best effort):

- `terminal_status`: unknown | success | fail | partial from keyword heuristics on last assistant text in window (existing importer patterns).  
- Prefer **honest unknown** over fake success.

## 3. Validation

Use `data_auditing/`:

- `audit_session_canonical.py` — B′ lane list + per-lane `suggested_slices`.  
- `audit_fleet_multi_model.py` — fleet rates for `bprime_splits_same_family_by_system`, unit multipliers.  
- Future: A/B embed of lane-only vs lane+slice on held-out queries.

## 4. Implementation order

1. Audit harness (done / extended for B′).  
2. Importer: emit lane children with B′ keys; keep parent.  
3. Wire segmenter onto **each lane maximizer** (not only global canonical).  
4. Re-hydrate multi-lane sessions; measure retrieval.

## 5. Non-goals

- Using `user_id` as lane key (session-scoped account id only).  
- One vector per proxy document (too many intermediates).  
- Real-time LLM slicing in the MCP hot path.
