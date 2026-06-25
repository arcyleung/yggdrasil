# Yggdrasil

MCP trajectory experience memory: agents record coding sessions (steps, progress, outcomes, concrete effort) and retrieve similar strategies via multi-aspect embeddings (`task` + `scaffold`) in Qdrant, with full traces in SQLite.

**Status:** PoC / greenfield. Spec: [`docs/superpowers/specs/2026-06-24-yggdrasil-mcp-trajectory-memory-design.md`](docs/superpowers/specs/2026-06-24-yggdrasil-mcp-trajectory-memory-design.md). Plan: [`docs/superpowers/plans/2026-06-24-yggdrasil-mcp-trajectory-memory.md`](docs/superpowers/plans/2026-06-24-yggdrasil-mcp-trajectory-memory.md).

## Architecture (Approach 2)

| Layer | Role |
|-------|------|
| MCP tools (stdio) | Validate args, call services, format responses |
| Services | Session / search / embed orchestration |
| Ports | `Embedder`, `TrajectoryStore`, `VectorIndex`, `EmbedView` |
| Adapters | SQLite, Qdrant, OpenAI-compat embedder, Mongo import shim |
| Domain | Trajectory / Step / Progress / Outcome / EffortLedger models |

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Qdrant
docker compose up -d
curl -s http://localhost:6333/readyz

cp .env.example .env
# set EMBED_API_KEY (and other EMBED_* if needed)

# MCP server (after implementation tasks complete)
python -m yggdrasil.mcp.server
# or: yggdrasil-mcp

pytest -q
```

## Mongo import (testing / hydration)

Batch-import agent transcripts from `claude_conversations.conversations` into Yggdrasil (SQLite + optional Qdrant embed). This is for **offline testing and index hydration**, not the production agent path.

**Never commit** `mongo_creds.txt` or `.env`. Optional extra: `pip install -e ".[mongo]"`. Live Mongo docs are **per-request proxy logs** (not one doc = one chat); ingress normalizes, session-aggregates, then segments. Full ingress design: [`docs/superpowers/specs/2026-06-24-yggdrasil-mongo-ingress-pipeline.md`](docs/superpowers/specs/2026-06-24-yggdrasil-mongo-ingress-pipeline.md).

### Two flows (know which one you're using)

| | **Intended (production)** | **External hydration (this section)** |
|--|---------------------------|----------------------------------------|
| Who segments | Same agent that ran the work, via [`skills/yggdrasil-trajectory-memory/SKILL.md`](skills/yggdrasil-trajectory-memory/SKILL.md) | Importer / subagent (`mongo_auto` heuristics, or pre-filled `segments` in JSONL) |
| How data reaches Qdrant | MCP `start_trajectory` → `append_step` → `finalize_trajectory` **per segment** | Export JSONL → `mongo_importer_pre_embed.py` → embed API → Qdrant |
| Provenance | `session_segment`, caller `external_refs` | Also `external_pre_embed`, `hydration_test`, `not_author_segmented` |

Hierarchical shape: **parent** = full session lineage (`mongo-session-{id}`, usually not embedded); **children** = embeddable sub-trajectories (`…-seg-0000`, primary search targets). Segment schema (caller or importer):

```json
[
  {"start_idx": 0, "end_idx": 3, "task": "research flights with google flights skill", "outcome": "success"},
  {"start_idx": 4, "end_idx": 6, "task": "use chrome browser agent to book flight", "outcome": "failed, rate limits"}
]
```

### Memory safety (subagents / large exports)

Importer and export workers default to a **24 GiB process budget** so parallel subagents cannot each eat all system RAM:

| Mechanism | How |
|-----------|-----|
| `scripts/lib/mem_limit.py` | `RLIMIT_AS` + optional RSS watchdog (`YGG_MAX_RSS_GB=24`) |
| `scripts/run_with_memcap.sh` | `ulimit -v` + env for any command |
| Export batching | Mongo docs aggregated in chunks (`--batch-docs`), not one giant in-memory list |
| Importer | Streams JSONL line-by-line (`iter_sessions_jsonl`), not full-file `list()` |

```bash
# Preferred for subagents / large jobs
scripts/run_with_memcap.sh 24 -- env PYTHONPATH=src python scripts/export_mongo_sessions_jsonl.py --sessions 2000 ...
scripts/run_with_memcap.sh 24 -- env PYTHONPATH=src python scripts/mongo_importer_pre_embed.py --jsonl ... --embed --experience-grade

# Or env only (scripts call apply_memory_cap() on startup)
export YGG_MAX_RSS_GB=24 YGG_MEM_WATCHDOG=1
```

Disable watchdog only if debugging: `YGG_MEM_WATCHDOG=0`. **Do not run unbounded parallel importers** without the 24 GiB wrapper; shard with `--offset` / `--limit` / `--worker-id` instead.

### Pipeline A — JSONL export + external pre-embed (recommended for ~100-session hydration)

Scripts: [`scripts/export_mongo_sessions_jsonl.py`](scripts/export_mongo_sessions_jsonl.py), [`scripts/mongo_importer_pre_embed.py`](scripts/mongo_importer_pre_embed.py), helpers in [`scripts/lib/mongo_session_io.py`](scripts/lib/mongo_session_io.py). Output under `data/exports/` (gitignored via `data/`). **Never exports `request_headers`** (strips Authorization).

```bash
# From repo root; needs mongo_creds.txt / MONGO_URI and PYTHONPATH=src
export PYTHONPATH=src

# 1) Export ~100 sessions (1 JSONL line = 1 canonical session transcript)
python scripts/export_mongo_sessions_jsonl.py \
  --sessions 100 --out data/exports/mongo_sessions_100.jsonl

# 2a) External segmentation only (subagent-friendly; no embed yet)
python scripts/mongo_importer_pre_embed.py \
  --jsonl data/exports/mongo_sessions_100.jsonl \
  --segment-only \
  --out-jsonl data/exports/mongo_sessions_100_segmented.jsonl

# 2b) Segment + embed children + upsert Qdrant (shard for parallel subagents)
python scripts/mongo_importer_pre_embed.py \
  --jsonl data/exports/mongo_sessions_100.jsonl \
  --embed --search-smoke \
  --offset 0 --limit 25 --worker-id w0

# Full file embed (slow; needs vLLM/embed endpoint + Qdrant up)
python scripts/mongo_importer_pre_embed.py \
  --jsonl data/exports/mongo_sessions_100.jsonl --embed --worker-id full100

# Experience-grade: cleaned task keys + tags that pass agent retrieval gates
# (still external segmentation, but not tagged hydration_test/archive)
python scripts/mongo_importer_pre_embed.py \
  --jsonl data/exports/mongo_sessions_100.jsonl --embed --experience-grade --worker-id full99eg
```

**Agent retrieval gates** (default on `search_strategies`): exclude `hydration_test` / `external_pre_embed` / `not_author_segmented`; drop noisy/short `task_text`; weak lexical overlap vs query; **empty results are OK** (no trusted prior). Eval:

```bash
PYTHONPATH=src python scripts/eval_segmented_retrieval_skill.py   # gates on
PYTHONPATH=src python scripts/eval_segmented_retrieval_skill.py --no-gates --include-archive
```

Each JSONL record includes `hydration_mode: external_pre_embed`, `canonical` (messages/tools/system/usage), optional `segments` after segment step, and a note that segmentation was **not** done by the trajectory author. Progress for sharded workers: `data/exports/pre_embed_progress_<worker_id>.json`.

**Search smoke after embed:** importer can pass `--search-smoke` (filters toward `external_pre_embed` / `session_segment` tags). For agent-quality recall, prefer queries built from real segment `task` text, not generic “coding agent tools” probes.

### Pipeline B — Direct import scripts (smaller / ad-hoc)

```bash
export PYTHONPATH=src

# Legacy: one doc → one trajectory (fixture-shaped or normalized single doc)
python scripts/import_mongo_for_testing.py --limit 5 --dry-run
python scripts/import_mongo_for_testing.py --limit 20 --reembed

# Hierarchical: session aggregate + auto-segment parent/children
python scripts/import_mongo_for_testing.py --limit 60 --hierarchical --limit-sessions 5 --reembed

# Live Mongo hierarchical + search smoke (no JSONL intermediate)
python scripts/import_mongo_sessions.py --limit-docs 60 --limit-sessions 3 --reembed --search-smoke
```

### Storage scaling (segmented experience embeds)

Source token volume (e.g. 15M tokens across 10 sessions) does **not** linearly drive Qdrant size. Each embedded child stores fixed-size vectors (`task` + `scaffold`, dim × 2 × f32) plus payload/index overhead—not full transcripts (those stay in Mongo/source).

```bash
PYTHONPATH=src python scripts/estimate_experience_storage.py \
  --sessions 10 --tokens 15000000 --segments-per-session 8 --dim 1024
```

Rough order of magnitude (children only, dim=1024, ~8 segments/session): **~2.6 MiB extra** for 10 sessions / 80 children; **~tens of MiB** for ~100 sessions depending on segment count. See script output for vector vs SQLite breakdown.

### Agent skill (production write path)

Symlink or copy [`skills/yggdrasil-trajectory-memory/SKILL.md`](skills/yggdrasil-trajectory-memory/SKILL.md) into the agent host skills dir. Agents should **segment first** (Workflow E in the skill), then MCP-write each child trajectory—do not embed entire multi-goal sessions as one key.

### Integration smoke (non-Mongo)

```bash
# Requires Qdrant + embed endpoint (.env). Seeds mock traces + search_strategies.
PYTHONPATH=src python scripts/smoke_integration.py
PYTHONPATH=src python scripts/smoke_normalize_mongo.py   # fixture dual-shape normalizer only
```

## Security

- Do not commit `.env` or `mongo_creds.txt`.
- Trajectories may contain secrets from agent context; local trust model in PoC.

## Config

See `.env.example` and the design spec §10.
