#!/usr/bin/env python3
"""Estimate extra disk for segmented experience embeddings (not source token mirror).

Example (user scenario):
  10 sessions, ~15M source tokens total, ~8 segments/session, dim=1024, task+scaffold only.

Usage:
  PYTHONPATH=src python scripts/estimate_experience_storage.py
  PYTHONPATH=src python scripts/estimate_experience_storage.py --sessions 10 --tokens 15000000 --segments-per-session 8
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from yggdrasil.adapters.importers.storage_estimate import (
    estimate_segmented_storage,
    format_estimate_report,
)


def main() -> int:
    p = argparse.ArgumentParser(description="Estimate segmented experience storage")
    p.add_argument("--sessions", type=int, default=10)
    p.add_argument("--tokens", type=int, default=15_000_000, help="Total source tokens (context only)")
    p.add_argument("--segments-per-session", type=float, default=8.0)
    p.add_argument("--dim", type=int, default=1024)
    p.add_argument("--embed-parent", action="store_true")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    est = estimate_segmented_storage(
        n_sessions=args.sessions,
        segments_per_session=args.segments_per_session,
        embed_dim=args.dim,
        embed_parent=args.embed_parent,
        source_tokens_total=args.tokens,
    )
    if args.json:
        print(json.dumps(est.to_dict(), indent=2))
    else:
        print(format_estimate_report(est))
        # Scaling table
        print("\n=== Linear scale (children only embed, same segments/session) ===")
        for n in (10, 100, 1000, 10_000):
            e = estimate_segmented_storage(
                n_sessions=n,
                segments_per_session=args.segments_per_session,
                embed_dim=args.dim,
                embed_parent=args.embed_parent,
                source_tokens_total=None,
            )
            mb = e.total_extra_bytes_est / (1024 * 1024)
            print(f"  sessions={n:>6}  children≈{e.n_child_trajectories:>7}  extra≈{mb:>10.2f} MiB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
