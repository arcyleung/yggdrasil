# CLIProxyAPI logs → ATIF → Postgres

## Current Mongo path (claude-code-transcripts)

| Script | Role |
|--------|------|
| `~/Projects/claude-code-transcripts/start_watcher.sh` | Runs `claude-log-watcher` on `cliproxyapi-logs/` with URI from `mongo_creds.txt` |
| `run_watcher_mongo.sh` | One-liner invoking watcher with hard-coded mongo URI on `localhost:27017` |
| CLIProxyAPI | Writes `v1-*.log` (and variants) under `cliproxyapi-logs/` |

Yggdrasil historically **imports from Mongo** (`mongo_importer_pre_embed.py`, multi-lane B′).

## Target path (this repo)

1. Ensure SSH tunnel: `~/Projects/claude-code-transcripts/postgresql_tunnel.sh` (maps local **9433** → remote PG **9432**). Slow is expected.
2. Creds: `~/Projects/claude-code-transcripts/postgresql_creds.txt`
3. Batch convert existing logs:

```bash
cd ~/Projects/grok_trace_analysis/yggdrasil
pip install psycopg2-binary  # if needed
PYTHONPATH=src python scripts/cliproxy_logs_to_atif_postgres.py --once --limit 50
```

4. Optional ATIF files on disk: `--atif-out-dir data/exports/atif_from_proxy`

Table: `atif_trajectories` (JSONB ATIF-v1.7 documents). Later: ETL from ATIF → Ygg SQLite/Postgres SoT with schema_version=4.
