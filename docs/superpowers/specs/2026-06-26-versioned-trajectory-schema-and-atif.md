# Versioned trajectory schema, retroactive Qdrant metadata, and ATIF alignment

**Date:** 2026-06-26  
**Status:** Design / plan (not implemented)  
**Triggers:** Narrow/inconsistent retrieval (missing `owner` on Qdrant filters & ranked tables); need rehydration that **backfills** metadata without full re-embed; evaluate [Harbor **ATIF**](https://www.harborframework.com/docs/agents/trajectory-format) (Agent Trajectory Interchange Format; often misremembered as “AIFT”).

---

## 1. Problem statement

Today Yggdrasil uses **Approach 2** (SQLite = system of record, Qdrant = vectors + **filter payload**). Rehydration (Mongo multi-lane B′) writes trajectories and embeds once. After that:

1. **Metadata evolves** — e.g. API-key → owner map, better timestamps, `experience_grade`, lane keys — but **Qdrant payloads stay stale** unless we re-upsert points.
2. **Retrieval / ranking depends on payload fields** (`owner`, `tags`, `experience_grade`, timestamps for `today|week|month`) that were often **missing** on import → `owner: unknown`, weak forensics tables.
3. **No explicit schema version** on stored rows or points → importers cannot say “this row is schema v3; migrate to v4”.
4. Full re-embed of 300k+ slices is **expensive**; we need **payload-only backfill** and optional **lazy embed** paths.

We want an **extensible, versioned** model so **every rehydration / migration** can retroactively add metadata to SQLite **and** Qdrant lookup filters **when fields are missing or outdated**, without requiring a vector recompute unless embed text changed.

---

## 2. Research: Harbor ATIF (not “AIFT”)

| Item | Detail |
|------|--------|
| Name | **ATIF** — Agent Trajectory Interchange Format |
| Home | https://www.harborframework.com/docs/agents/trajectory-format |
| Normative RFC | https://github.com/harbor-framework/harbor/blob/main/rfcs/0001-trajectory-format.md |
| Purpose | Interchange for **eval / RL / SFT / replay** of agent runs (tool calls, observations, metrics, multimodal, subagents) |
| Versioning | Root `schema_version` e.g. `"ATIF-v1.7"`; additive evolution (multimodal v1.6, subagents/context mgmt v1.7) |
| Core shape | `agent{name,version,model_name}` + `steps[]` (`step_id`, `source`, `message`, `tool_calls`, `observation`, `metrics`) + optional `final_metrics`, `subagent_trajectories` |
| Tooling | Harbor Pydantic models + trajectory validator CLI |

### Does ATIF “work” for Yggdrasil?

| Dimension | ATIF fit | Yggdrasil need |
|-----------|----------|----------------|
| Full step fidelity for training/eval | **Strong** | Nice for export/import; not required for ANN strategy search |
| Strategy retrieval (task+scaffold vectors) | **Weak** — no first-class dual-aspect embed keys | Core product |
| Org attribution (`owner` human handoff) | Only via `extra` / agent fields | **First-class** |
| Effort ledger / failure waste | Partial via metrics/cost | First-class `EffortLedger` |
| Multi-lane Mongo ingress (B′) | Subagent_trajectories is related but not the same | Lane maximizers + slices |
| Filterable vector DB payload | Out of scope for ATIF | Critical for Qdrant |

**Conclusion:** Treat ATIF as an **interchange / export-import dialect**, not a replacement for Yggdrasil’s internal SoT. Adopt ATIF ideas:

1. Explicit **`schema_version`** string on every trajectory document.
2. **Additive** version bumps with validators.
3. **`extra`** / extension bags for unknown future fields.
4. Optional **ATIF export** of steps for Harbor/eval ecosystems.
5. Optional **ATIF import** adapter (lossy → our domain models).

Do **not** force ATIF’s step-centric training schema as Qdrant payload (too large, wrong grain).

---

## 3. Design principles (superpowers-aligned)

1. **SQLite remains SoT** — Qdrant payloads are a **projection** of SoT + denormalized filter fields.
2. **Schema version on SoT** — every trajectory row carries `schema_version` (integer or semver string).
3. **Projection version on Qdrant** — payload carries `payload_schema_version` so we know if filters are stale vs SoT.
4. **Migrations are functions** `migrate_vN_to_vN+1(row) -> row` applied in order; **idempotent**.
5. **Backfill ≠ re-embed** — default migration updates SQLite + **Qdrant `set_payload` / overwrite payload** only; re-embed only if `task_text` / `scaffold_text` / `embed_view_version` changed.
6. **Stable point IDs** — keep deterministic Qdrant IDs from trajectory id (Wave A); migrations never orphan points.
7. **Provenance of metadata** — record `metadata_sources` (e.g. `api_key_map@2026-06-26`, `mongo_doc.created_at`) for audit.
8. **Extensibility** — new filter fields register in a **PayloadFieldRegistry** with type, qdrant index hint, migrate-from rules.

---

## 4. Proposed versioned schemas

### 4.1 Internal: `YggTrajectorySchema` (SoT)

| Version | Name | Additions (illustrative) |
|---------|------|---------------------------|
| **v1** | PoC baseline | Trajectory + steps; tags; external_refs; effort; no explicit version field (infer as v1) |
| **v2** | Tenancy + index | `tenant_id`, `index_status`, principal-bound writes |
| **v3** | Multi-lane ingress | `multilane_bprime`, lane/slice ids, `system_hash`, `lane_key` in refs |
| **v4** | **Attribution + time (this plan)** | Required-or-nullable **`owner`**, **`agent_id`**, **`team`** columns or canonical refs; **`occurred_at`** (event time) vs `created_at`/`updated_at` (ingest time); `schema_version=4`; `metadata_sources` JSON |
| **v5** | Optional ATIF bridge | `atif_export_ref` / stored ATIF blob path; `atif_schema_version` |

Store `schema_version INTEGER NOT NULL DEFAULT 1` on `trajectories` (and optionally `steps`).

### 4.2 Qdrant: `YggPayloadSchema`

Minimal **filter payload** (denormalized; keep small):

```text
payload_schema_version: int
trajectory_id: str
domain, status, tenant_id
owner, agent_id, team, workspace      # attribution — may be null
tags: string[]
experience_grade: bool
multilane_bprime: bool
occurred_at_ts: int | null            # unix seconds for range filters
updated_at_ts: int | null
index_status: str
embed_view_version: str
schema_version: int                   # mirrors SoT
```

Vectors unchanged: named `task` + `scaffold` unless `embed_view_version` migration requires re-embed.

### 4.3 Interchange: ATIF profile `Ygg-ATIF-1`

Export mapping (lossy but useful):

| Ygg | ATIF |
|-----|------|
| `id` | `trajectory_id` |
| `session_id` from refs | `session_id` |
| steps (kind/summary/payload) | `steps[]` with `source` derived from kind; tool payloads → `tool_calls` / `observation` best-effort |
| effort totals | `final_metrics.extra.ygg_effort` |
| owner/agent | `agent.extra.ygg_owner` / `agent.name` |
| `schema_version` | `extra.ygg_schema_version` + document `schema_version: "ATIF-v1.7"` for Harbor validators |

Import: ATIF file → normalize steps → `Trajectory` v4 + optional embed.

---

## 5. Retroactive metadata pipeline (“rehydrate without re-embed”)

```
                    ┌─────────────────────┐
  Mongo / MCP / ATIF │  Ingress adapters   │
                    └──────────┬──────────┘
                               ▼
                    ┌─────────────────────┐
                    │ SQLite SoT apply    │
                    │ migrations → vN     │
                    │ fill owner, times   │
                    └──────────┬──────────┘
                               ▼
              ┌────────────────┴────────────────┐
              │ payload_schema_version          │
              │ < registry.current ?            │
              └────────────┬────────────────────┘
                     yes   │   no
                           ▼
              ┌────────────────────────────┐
              │ Qdrant set_payload only    │  ◄── default path
              │ (no vector rewrite)        │
              └────────────────────────────┘
                     if task/scaffold changed
                           ▼
              ┌────────────────────────────┐
              │ Re-embed + upsert point    │
              └────────────────────────────┘
```

### 5.1 Enrichment sources (priority order for `owner`)

1. Existing `external_refs.owner` / column if non-empty and not `unknown`.
2. `user_mapping.yaml` / `KEY_NAME_MAP` via **auth fingerprint** of Mongo `Authorization` / API key (never store full key in SoT long-term; store fingerprint + resolved owner).
3. Heuristic from tags `owner:alice` if present.
4. Leave null + `metadata_sources.owner = "unresolved"` (better than lying with `"unknown"` string in filters — prefer **null** + ranked table shows `—`).

### 5.2 Timestamps

| Field | Meaning |
|-------|---------|
| `created_at` | First insert into Yggdrasil |
| `updated_at` | Last SoT mutation |
| `occurred_at` | **Event time** from source (Mongo `created_at` / last message ts / ATIF step max ts) — used for `today|week|month` ranking |
| `finalized_at` | Terminal outcome time if any |

Rehydration must set **`occurred_at`** from Mongo when available so ranking is not “all today” after a mass import.

### 5.3 Job CLI (planned)

```bash
# Dry-run: count rows needing owner/time/payload sync
python scripts/migrate_trajectory_schema.py --to 4 --dry-run

# Apply SoT migrations + Qdrant payload backfill (no embed)
python scripts/migrate_trajectory_schema.py --to 4 --sync-qdrant-payload

# Only points where payload_schema_version < 4
python scripts/sync_qdrant_payload.py --min-payload-version 4 --batch 500
```

Idempotent: re-running does not duplicate points; only patches missing/outdated fields.

### 5.4 Online path (search-time optional)

Short-term mitigation already partially done (join SQLite on hit ids). Long-term still **prefer correct Qdrant filters** for `owner=…` prefilters. Optional **read-time repair**: if hit has null owner, hydrate from SQLite and async enqueue payload patch (eventual consistency).

---

## 6. Extensibility mechanics

### 6.1 PayloadFieldRegistry

```python
@dataclass
class PayloadField:
    name: str
    py_type: type
    introduced_in_payload_version: int
    qdrant_index: Literal["keyword", "integer", "float", "bool"] | None
    extract: Callable[[Trajectory], Any]  # from SoT
```

New field in v5 → register → migration bumps `PAYLOAD_SCHEMA_VERSION` → `sync_qdrant_payload` fills it for all points.

### 6.2 Migration registry

```python
MIGRATIONS: dict[int, Callable[[Trajectory, MigrationCtx], Trajectory]] = {
    2: migrate_to_v2_tenant,
    3: migrate_to_v3_multilane_tags,
    4: migrate_to_v4_attribution_time,
}
```

`MigrationCtx` includes key→owner map, clock, dry_run flag.

### 6.3 Compatibility matrix

| Client / writer | Min schema | Notes |
|-----------------|------------|-------|
| MCP tools today | v2+ | Accept missing owner |
| Ranked search tables | v4 preferred | Falls back to refs |
| ATIF export | maps from ≥v1 | Always possible lossy |
| Harbor validator | export only | We emit ATIF-v1.7 + `extra` |

---

## 7. Relation to current pain (owner unknown, all “today”)

| Symptom | Root cause | Plan fix |
|---------|------------|----------|
| `owner: unknown` | Import didn’t resolve API key; not on Qdrant payload | v4 migration + key map backfill + payload sync |
| All ranges `today` | Used ingest `updated_at` from mass rehydrate | Populate **`occurred_at`** from Mongo event time |
| Narrow lab search | Was `experience_grade_only` (mitigated) | Keep wide lab recall; payload flags still help filters |
| Rehydrate expensive | Full embed every time | Payload-only sync by default |

---

## 8. Implementation plan (phased; orchestrate later)

### Phase 0 — Spec lock (0.5 d)
- [ ] Ratify `YggTrajectorySchema` v4 field list + nullability.
- [ ] Ratify Qdrant payload field list + `PAYLOAD_SCHEMA_VERSION = 4`.
- [ ] Document ATIF export mapping table (this doc §4.3).
- [ ] ADR: “ATIF is interchange; Ygg schema is SoT”.

### Phase 1 — Schema plumbing (1–2 d)
- [ ] Alembic-less SQLite: `ALTER` add `schema_version`, `occurred_at` (nullable), optional generated columns.
- [ ] Domain `Trajectory` model fields; default `schema_version=4` on new writes.
- [ ] `VectorPointPayload` + `qdrant_filters` for `owner`, `occurred_at_ts`, `payload_schema_version`.
- [ ] PayloadFieldRegistry module + unit tests.

### Phase 2 — Migrations + backfill job (2–3 d)
- [ ] `migrate_trajectory_schema.py` apply v1→…→v4 on SQLite batch.
- [ ] Owner resolution from `user_mapping` + stored key fingerprints (extend importer to persist `auth_fingerprint` in refs at ingress).
- [ ] `sync_qdrant_payload.py` using Qdrant `set_payload` / scroll by trajectory_id.
- [ ] Metrics: `% with owner`, `% with occurred_at`, payload version histogram.
- [ ] Dry-run + progress JSONL (reuse pre_embed progress patterns; **single writer** to avoid SQLite locks).

### Phase 3 — Ingress always writes v4 (1 d)
- [ ] Mongo multilane + MCP `start_trajectory` set owner/time/schema_version.
- [ ] Stop writing string `"unknown"`; use null + sources.

### Phase 4 — ATIF bridge (optional, 2–4 d)
- [ ] `scripts/export_atif.py` — trajectory id(s) → ATIF-v1.7 JSON.
- [ ] `scripts/import_atif.py` — ATIF → Ygg v4 (no Harbor dependency required; implement mapping ourselves; optional `harbor` extra for validation only).
- [ ] Golden test: round-trip subset fields.

### Phase 5 — Search / skill UX (0.5–1 d)
- [ ] Ranked tables prefer `occurred_at` for `range`.
- [ ] Skill text: mention schema v4 / re-download skill after deploy.
- [ ] Optional filter `owner` in `search_strategies` once payload reliable.

### Phase 6 — Hardening
- [ ] Integration test: insert v1-shaped row → migrate → payload has owner → filter query returns it.
- [ ] Runbook: “after changing user_mapping.yaml, run payload sync”.
- [ ] Do **not** run multi-worker SQLite migrators (lesson from multilane rehydrate locks).

---

## 9. Non-goals

- Replacing SQLite with ATIF files on disk as primary store.
- Training pipeline / Harbor runtime dependency in core install.
- Automatic PII redaction (separate privacy track).
- Graph migration of all historical Mongo in one mandatory job (batch + resume is enough).

---

## 10. Success criteria

1. After one `migrate --to 4 --sync-qdrant-payload`, **≥95%** of embedded points have `payload_schema_version >= 4`.
2. **≥80%** of lab trajectories have non-null `owner` **or** explicit `metadata_sources.owner=unresolved` (measurable).
3. Ranked search `range` distribution is **not** 100% `today` on a corpus with old Mongo event times.
4. Re-running migration is **idempotent** (no payload oscillation).
5. ATIF export validates against Harbor docs’ required fields for a minimal fixture (manual or optional dep).
6. New MCP writes are schema v4 without migration.

---

## 11. Open questions (resolve before coding Phase 1)

1. **Integer vs string schema versions** — recommend **int** internally + ATIF string only on export.
2. **Null owner vs `"unknown"`** — recommend **null** in DB/payload; UI/skill display `—` / `unknown`.
3. **Store auth fingerprints** on import forever? — yes, hashed only, for future map updates.
4. **Qdrant payload indexes** — create keyword index on `owner` when present (cluster ops).
5. **Adopt Harbor package** — optional `[atif]` extra; core stays dependency-light.

---

## 12. References

- Harbor ATIF docs: https://www.harborframework.com/docs/agents/trajectory-format  
- Harbor RFC 0001: https://github.com/harbor-framework/harbor/blob/main/rfcs/0001-trajectory-format.md  
- Yggdrasil PoC design (Approach 2): `docs/superpowers/specs/2026-06-24-yggdrasil-mcp-trajectory-memory-design.md`  
- Multi-lane ingress: `docs/superpowers/specs/2026-06-26-mongo-multilane-and-subtask-slices.md`  
- Multi-replica SoT note: `docs/adr/0001-multi-replica-data-plane.md`

---

## 13. Recommendation

**Yes — versioned schema + payload projection sync is the right extensible solution.**  
**ATIF is useful as interchange and as inspiration for `schema_version` + additive evolution, not as the ANN/payload core.**  

Next step when you want execution: implement **Phase 0–2** only (v4 attribution/time + migrate + Qdrant payload sync), measure owner/range quality, then consider ATIF export.
