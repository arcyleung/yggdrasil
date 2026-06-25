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

## Mongo import (testing only)

Never commit `mongo_creds.txt`. Optional extra: `pip install -e ".[mongo]"`.

```bash
python scripts/import_mongo_for_testing.py --limit 5 --dry-run
python scripts/import_mongo_for_testing.py --limit 20 --reembed
```

## Security

- Do not commit `.env` or `mongo_creds.txt`.
- Trajectories may contain secrets from agent context; local trust model in PoC.

## Config

See `.env.example` and the design spec §10.
