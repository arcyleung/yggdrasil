# Yggdrasil

**MCP-native org-wide trajectory experience memory for autonomous agents.**

Agents record real work (steps, progress, outcomes, effort, artifacts) and retrieve similar *strategies* via multi-aspect embeddings (`task` + `scaffold`), with full traces in SQLite and vectors in Qdrant. Humans regain **context** over fleets of agents on long-horizon tasks—without maintaining a parallel documentation culture.

**Status:** PoC. Spec: [`docs/superpowers/specs/2026-06-24-yggdrasil-mcp-trajectory-memory-design.md`](docs/superpowers/specs/2026-06-24-yggdrasil-mcp-trajectory-memory-design.md). Deeper literature: [org-level agent experience memory survey](surveys/org_level_agent_experience_memory_literature_survey.md).

---

## Install for lab users (skill + remote MCP)

You do **not** need to run Yggdrasil yourself. An operator hosts the control plane and data plane; you only need a **secret API key** they mapped to your name (e.g. in `user_mapping.yaml`).

1. Open the **Yggdrasil host** URL your org provides (control plane UI).
2. **Lab login** with your **secret key** (`sk-…` style org key—not a password you invent).
3. Download **`skill.md` (MCP + policies)** — one document that embeds:
   - how to connect MCP (Streamable HTTP to the host’s `/mcp` with your issued `ygg_…` bearer), and
   - search/write policies (search before uncertain work, surface owners, segment long runs, …).
4. **Paste that skill** into your favorite agent (Codex, Claude Code, Cursor, Grok skills, …) as the Yggdrasil skill.
5. Point the agent’s MCP client at the URL + bearer from the skill (or use `mcp.json` from the same login). Tools such as `search_strategies` / `start_trajectory` should appear once MCP handshakes succeed.

Re-download `skill.md` if your token is revoked or you sign in again (bearer is session-bound).

### Example prompt (after skill + MCP are live)

Try something that forces **org experience search** and **human handoff**, not a greenfield invent:

> Who has set up the Kubernetes infra in the lab, configured it the most, and might be the most familiar? Search Yggdrasil for prior trajectories; name the **owners** I should talk to, summarize what worked or failed, and link any artifacts.

A good agent will call `search_strategies` (often `search_mode=lab`), report **owner / agent_id / team**, optionally `get_trajectory` on the best hits, and only then suggest next steps or people to ping.

---

## Value proposition

### 1) No documentation maintenance burden

Trajectories are the **experiential source of truth**. Agents and scaffolds act as a **self-verifier / attestation** mechanism: what was tried is what was recorded (task + scaffold for retrieval, steps and outcomes for proof)—not a wiki someone was supposed to update after the fact. Humans rarely read AI-generated docs; agents do. Indexing execution traces beats maintaining stale human-facing writeups as **context debt** grows.

### 2) Memoization via “tombstones”

Success and failure outcomes of prior agents on **complex / long-horizon** tasks are durable **tombstones**: cheap to consult, expensive to rediscover. They memoize “we already paid this cost” and give a **preview of likely outcomes** for similar tasks and strategies (including high `failure_waste_seconds` as a warning, not a template).

### 3) Strategy-level understanding (not just the 20% humans see live)

At test time a human may understand only a **fraction** (~20%) of the context of what agents are doing on the task at hand. For **auditing** and **long-term strategy** understanding, you need the agent’s **whole trajectory**—paths, retries, tools, artifacts—not only the final answer in chat. Yggdrasil keeps that lineage searchable at strategy grain (embed current task + scaffold only; full trace on demand).

### 4) Security and management leverage

- **Security / compliance auditing:** quick semantic search over “what did agents actually run?” for suspicious or policy-sensitive activity by org members (attribution via `owner` / `agent_id`).
- **Management / efficiency:** see which human–agent interaction patterns burn tokens, loop, or succeed with low waste; improve playbooks and who to staff by **who has done this before** (handoff is first-class).

---

## Differentiation (vs Mem0, MemClaw, doc RAG, …)

Yggdrasil is **not** another fact store, chat memory, or document RAG layer (Mem0, MemClaw-style fleet memory, Zep/Graphiti, Letta, A-MEM, Byterover, generic MCP memory servers, LangMem, …). Those optimize for “what does the user/agent know?” or “retrieve snippets.” Yggdrasil optimizes for **“what strategies did we execute, what did they cost, what did they produce, and who owns the human follow-up?”**

| Theme | Typical memory / RAG | Yggdrasil |
|-------|----------------------|-----------|
| Unit of memory | Facts, chunks, messages | **Trajectories** (strategy + steps + outcome + effort + artifacts) |
| Search key | Text / passage embed | **Task + scaffold** aspects (not full retry transcripts) |
| Org handoff | Often missing | **`owner` / `agent_id` / `team` on every useful hit** |
| Integration | App-specific SDKs | **MCP-native** (remote HTTP on the host and/or stdio) |
| Agent reflex | Passive or always-on write | **Search before uncertain / high-overhead work**, then selective write |

---

## For operators (host the control plane)

FastAPI UI: lab login, personalized `skill.md` / `mcp.json`, Streamable HTTP MCP at `/mcp` (Bearer `ygg_…`), optional chat UI.

```bash
pip install -e ".[web,dev]"
export YGG_PUBLIC_BASE_URL=https://your-yggdrasil-host.example   # public URL agents will use
export YGG_URL_PREFIX=                                          # set if UI is served under a path prefix
export YGG_USER_MAPPING_PATH=user_mapping.yaml                  # api_key -> owner (do not commit)
# Qdrant + embed endpoint via .env — see .env.example
uvicorn yggdrasil.web.app:app --host 127.0.0.1 --port 8080
# or: YGG_UI_BIND=127.0.0.1:6001 python -m yggdrasil.web
```

Map each lab user in `user_mapping.yaml` (`sk-…: alice`). Users log in with that key; the UI issues opaque `ygg_…` tokens for MCP. Reverse-proxy / private network / Funnel-style exposure is an ops choice—publish **one stable base URL** in `YGG_PUBLIC_BASE_URL` so skill downloads embed the right MCP endpoint.

Optional ops notes (path prefixes, private mesh): [`docs/superpowers/runbooks/tailscale-funnel.md`](docs/superpowers/runbooks/tailscale-funnel.md).

## Architecture (Approach 2)

| Layer | Role |
|-------|------|
| MCP tools (stdio **or** Streamable HTTP `/mcp`) | Validate args, call services, format responses; principal from Bearer / `YGG_MCP_TOKEN` |
| Control plane UI | Login, skill download, optional chat |
| Services | Session / search / embed orchestration |
| Ports | `Embedder`, `TrajectoryStore`, `VectorIndex`, `EmbedView` |
| Adapters | SQLite, Qdrant, OpenAI-compat embedder, Mongo import shim |
| Domain | Trajectory / Step / Progress / Outcome / EffortLedger models |

## Quick start (developers / local data plane)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[web,dev]"

# Qdrant
docker compose up -d
curl -s http://localhost:6333/readyz

cp .env.example .env
# set EMBED_* , YGG_USER_MAPPING_PATH / KEY_NAME_MAP, YGG_PUBLIC_BASE_URL

# Control plane + remote MCP (preferred for agents off-box)
uvicorn yggdrasil.web.app:app --host 127.0.0.1 --port 8080

# Optional: stdio MCP only (same machine as SQLite/Qdrant)
# export YGG_MCP_TOKEN=ygg_…   # issued token, or omit if tenancy off
python -m yggdrasil.mcp

pytest -q
```

## Agent skill (production write path)

End users should prefer the **personalized `skill.md` from the host UI** (token + MCP URL embedded). The repo copy [`skills/yggdrasil-trajectory-memory/SKILL.md`](skills/yggdrasil-trajectory-memory/SKILL.md) is the same policies with placeholders for self-hosted setups. Agents should **segment** multi-goal sessions, then MCP-write per segment—do not embed one giant multi-goal key.

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

### Integration smoke (non-Mongo)

```bash
# Requires Qdrant + embed endpoint (.env). Seeds mock traces + search_strategies.
PYTHONPATH=src python scripts/smoke_integration.py
PYTHONPATH=src python scripts/smoke_normalize_mongo.py   # fixture dual-shape normalizer only
```

## Security

**Threat model (trusted PoC only)** — see [`docs/superpowers/specs/2026-06-25-threat-model-poc.md`](docs/superpowers/specs/2026-06-25-threat-model-poc.md):

- **In scope:** single local operator, trusted MCP clients/agents on a private network, SQLite + Qdrant under operator control.
- **Out of scope:** hostile multi-tenant use, malicious agents, Qdrant exposed on the public internet, formal compliance/PII guarantees.
- **Trajectories may contain secrets and PII** copied from agent context (prompts, tool output, credentials in text). Storage is **not** proof of scrubbing.
- **`owner` / `agent_id` are attribution fields**, not evidence that content was redacted. Optional regex scrubbing is off by default (`YGG_SCRUB_CONTENT=0`); set to `1` for best-effort email/`sk-`/`Bearer` redaction while preserving allowlisted owner names exactly.
- Do not commit `.env`, `mongo_creds.txt`, or `user_mapping.yaml`.
- If `docker-compose` Qdrant ports are reachable beyond localhost, set `QDRANT_API_KEY` (compose passes `QDRANT__SERVICE__API_KEY` when set).
- Multi-tenant authz is **not** implemented; blockers are tracked in [`docs/superpowers/plans/2026-06-25-multi-tenant-authz-FOLLOWON.md`](docs/superpowers/plans/2026-06-25-multi-tenant-authz-FOLLOWON.md).

## Config

See `.env.example` and the design spec §10.
