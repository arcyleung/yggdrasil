#!/usr/bin/env python3
"""Convert CLIProxyAPI log files → ATIF-v1.7 JSON → Postgres (via tunnel).

Watches the same log dir as claude-code-transcripts/start_watcher.sh but writes
ATIF rows to Postgres instead of (or in addition to) Mongo.

Credentials: postgresql_creds.txt (URL or key=value). Tunnel: postgresql_tunnel.sh
(slow SSH tunnel is expected — use batch commits).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Default paths from sibling transcripts project
TRANSCRIPTS = Path.home() / "Projects" / "claude-code-transcripts"


def load_pg_dsn(creds_path: Path) -> str:
    text = creds_path.read_text(encoding="utf-8").strip()
    if text.startswith("postgres"):
        return text.splitlines()[0].strip()
    # key=value lines
    kv = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        kv[k.strip().lower()] = v.strip().strip('"').strip("'")
    user = kv.get("user") or kv.get("username") or "postgres"
    password = kv.get("password") or kv.get("pass") or ""
    host = kv.get("host") or "127.0.0.1"
    port = kv.get("port") or "9433"
    db = kv.get("database") or kv.get("dbname") or kv.get("db") or "postgres"
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


def proxy_log_to_atif(path: Path, raw_text: str) -> dict:
    """Best-effort ATIF-v1.7 document from a proxy log file (JSONL or single JSON)."""
    steps = []
    step_id = 1
    model_name = None
    session_id = path.stem
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            steps.append(
                {
                    "step_id": step_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "source": "system",
                    "message": line[:8000],
                }
            )
            step_id += 1
            continue
        # proxy shapes vary; extract messages if present
        body = obj.get("request_body") or obj.get("body") or obj
        model_name = model_name or body.get("model") or obj.get("model")
        msgs = body.get("messages") if isinstance(body, dict) else None
        if isinstance(msgs, list):
            for m in msgs[-3:]:  # tail only to limit size
                role = (m.get("role") or "user") if isinstance(m, dict) else "user"
                content = m.get("content") if isinstance(m, dict) else str(m)
                if isinstance(content, list):
                    content = json.dumps(content)[:8000]
                elif not isinstance(content, str):
                    content = str(content)[:8000]
                src = "user" if role == "user" else ("agent" if role == "assistant" else "system")
                steps.append(
                    {
                        "step_id": step_id,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "source": src,
                        "message": (content or "")[:8000],
                        "model_name": model_name,
                    }
                )
                step_id += 1
        else:
            steps.append(
                {
                    "step_id": step_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "source": "system",
                    "message": json.dumps(obj)[:8000],
                }
            )
            step_id += 1
    if not steps:
        steps = [
            {
                "step_id": 1,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "source": "system",
                "message": f"empty or unparsed log {path.name}",
            }
        ]
    return {
        "schema_version": "ATIF-v1.7",
        "session_id": session_id,
        "trajectory_id": str(uuid.uuid4()),
        "agent": {
            "name": "cliproxyapi",
            "version": "unknown",
            "model_name": model_name,
            "extra": {"ygg_source": "cliproxy_log", "path": str(path)},
        },
        "steps": steps,
        "extra": {
            "ygg_schema_version": 4,
            "source_path": str(path),
            "ingested_at": datetime.now(timezone.utc).isoformat(),
        },
    }


DDL = """
CREATE TABLE IF NOT EXISTS atif_trajectories (
    trajectory_id TEXT PRIMARY KEY,
    session_id TEXT,
    schema_version TEXT NOT NULL,
    agent_name TEXT,
    model_name TEXT,
    source_path TEXT,
    atif_json JSONB NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_atif_session ON atif_trajectories(session_id);
CREATE INDEX IF NOT EXISTS idx_atif_ingested ON atif_trajectories(ingested_at);
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--log-dir",
        type=Path,
        default=TRANSCRIPTS / "cliproxyapi-logs",
        help="CLIProxyAPI log directory (same as start_watcher.sh)",
    )
    ap.add_argument("--pattern", default="v1-*.log")
    ap.add_argument(
        "--pg-creds",
        type=Path,
        default=TRANSCRIPTS / "postgresql_creds.txt",
    )
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--once", action="store_true", help="Process existing files and exit (no watch loop)")
    ap.add_argument("--atif-out-dir", type=Path, default=None, help="Also write ATIF JSON files here")
    args = ap.parse_args()

    if not args.log_dir.is_dir():
        print(f"error: log dir missing {args.log_dir}", file=sys.stderr)
        return 2
    if not args.pg_creds.is_file():
        print(f"error: pg creds missing {args.pg_creds}", file=sys.stderr)
        print("Ensure postgresql_tunnel.sh is up (port 9433) and creds exist.", file=sys.stderr)
        return 2

    try:
        import psycopg2
    except ImportError:
        print("pip install psycopg2-binary", file=sys.stderr)
        return 2

    dsn = load_pg_dsn(args.pg_creds)
    print("connecting postgres (tunnel may be slow)…", flush=True)
    conn = psycopg2.connect(dsn, connect_timeout=60)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(DDL)
    conn.commit()
    print("schema ok", flush=True)

    files = sorted(args.log_dir.glob(args.pattern))
    if args.limit:
        files = files[: args.limit]
    print(f"files={len(files)} pattern={args.pattern}", flush=True)

    n_ok = n_err = 0
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
            atif = proxy_log_to_atif(path, text)
            if args.atif_out_dir:
                args.atif_out_dir.mkdir(parents=True, exist_ok=True)
                (args.atif_out_dir / f"{atif['trajectory_id']}.json").write_text(
                    json.dumps(atif, indent=2), encoding="utf-8"
                )
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO atif_trajectories (
                        trajectory_id, session_id, schema_version, agent_name, model_name,
                        source_path, atif_json
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)
                    ON CONFLICT (trajectory_id) DO UPDATE SET
                        atif_json = EXCLUDED.atif_json,
                        ingested_at = NOW()
                    """,
                    (
                        atif["trajectory_id"],
                        atif.get("session_id"),
                        atif.get("schema_version"),
                        (atif.get("agent") or {}).get("name"),
                        (atif.get("agent") or {}).get("model_name"),
                        str(path),
                        json.dumps(atif),
                    ),
                )
            conn.commit()
            n_ok += 1
        except Exception as exc:
            conn.rollback()
            n_err += 1
            print(f"err {path.name}: {exc}", file=sys.stderr)
        if (n_ok + n_err) % 20 == 0:
            print(f"progress ok={n_ok} err={n_err}", flush=True)

    print(json.dumps({"ok": n_ok, "err": n_err, "once": args.once}))
    conn.close()
    return 0 if n_err == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
