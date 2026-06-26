#!/usr/bin/env python3
"""Estimate retrieval enhancement from multi-lane vs single-canonical.

Without re-embedding everything, we measure:
1) How many distinct embeddable **text units** (task-like strings) multi-lane adds.
2) Lexical diversity (unique token sets) — proxy for whether search could hit different queries.
3) Optional: for sessions already in SQLite, whether parent task_text matches only one lane's first_user.

True vector recall needs a follow-up embed experiment; this validates *information* gain.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib_session_metrics import (  # noqa: E402
    analyze_session_docs,
    fetch_session_docs,
    mongo_uri_from_creds,
)

_TOKEN = re.compile(r"[a-z0-9_]{3,}", re.I)


def tokens(s: str) -> set[str]:
    return set(_TOKEN.findall((s or "").lower()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--session-ids-file", default=str(ROOT / "data_auditing/reports/multi_model_session_ids.txt"))
    ap.add_argument("--limit", type=int, default=30)
    ap.add_argument("--creds", default=str(ROOT / "mongo_creds.txt"))
    ap.add_argument("--out", default=str(ROOT / "data_auditing/reports/multilane_retrieval.json"))
    ap.add_argument("--sqlite", default=str(ROOT / "data" / "yggdrasil.db"))
    args = ap.parse_args()

    uri = mongo_uri_from_creds(args.creds)
    if not uri:
        print("error: no mongo uri", file=sys.stderr)
        return 1

    ids_path = Path(args.session_ids_file)
    if not ids_path.exists():
        print("error: run audit_fleet_multi_model.py first", file=sys.stderr)
        return 1
    session_ids = [ln.strip() for ln in ids_path.read_text().splitlines() if ln.strip()][: args.limit]

    import sqlite3

    db_path = Path(args.sqlite)
    hydrated_task: dict[str, str] = {}
    if db_path.exists():
        c = sqlite3.connect(db_path)
        for sid in session_ids:
            tid = f"mongo-session-{sid}"
            row = c.execute("select task_text from trajectories where id=?", (tid,)).fetchone()
            if row:
                hydrated_task[sid] = row[0] or ""

    rows = []
    total_single_units = 0
    total_multi_units = 0
    total_unique_tokens_single = 0
    total_unique_tokens_multi = 0
    lanes_add_novel_tokens = 0
    hydrated_only_matches_canonical_lane = 0
    hydrated_checked = 0

    for sid in session_ids:
        docs = fetch_session_docs(uri, sid)
        audit = analyze_session_docs(sid, docs)
        can_text = audit.canonical_first_user
        lane_texts = [L.first_user for L in audit.lanes if L.first_user]
        # units
        single_units = 1 if can_text else 0
        multi_units = len({t[:200] for t in lane_texts})
        tok_s = tokens(can_text)
        tok_m: set[str] = set()
        for t in lane_texts:
            tok_m |= tokens(t)
        novel = tok_m - tok_s
        if novel:
            lanes_add_novel_tokens += 1

        total_single_units += max(single_units, 1)
        total_multi_units += max(multi_units, 1)
        total_unique_tokens_single += len(tok_s)
        total_unique_tokens_multi += len(tok_m)

        ht = hydrated_task.get(sid)
        if ht is not None:
            hydrated_checked += 1
            # crude: hydrated task closer to canonical first_user than other lanes?
            def overlap(a: str, b: str) -> int:
                return len(tokens(a) & tokens(b))

            o_can = overlap(ht, can_text)
            o_other = max((overlap(ht, L.first_user) for L in audit.lanes if L.family != audit.canonical_family), default=0)
            if o_can >= o_other:
                hydrated_only_matches_canonical_lane += 1

        rows.append(
            {
                "session_id": sid,
                "lane_count": audit.lane_count,
                "extra_lanes": audit.extra_lanes_beyond_canonical,
                "canonical_family": audit.canonical_family,
                "families": audit.families,
                "single_unit_tokens": len(tok_s),
                "multi_unit_tokens": len(tok_m),
                "novel_tokens_vs_canonical": len(novel),
                "flags": audit.verdict_flags(),
            }
        )

    report = {
        "n_sessions": len(rows),
        "embeddable_units": {
            "single_canonical_total": total_single_units,
            "multilane_total": total_multi_units,
            "multiplier": (total_multi_units / total_single_units) if total_single_units else None,
        },
        "lexical_coverage_proxy": {
            "sum_unique_tokens_single_lane_text": total_unique_tokens_single,
            "sum_unique_tokens_all_lanes": total_unique_tokens_multi,
            "sessions_where_extra_lanes_add_novel_tokens": lanes_add_novel_tokens,
            "rate_novel": (lanes_add_novel_tokens / len(rows)) if rows else None,
        },
        "hydrated_db_alignment": {
            "sessions_with_parent_in_sqlite": hydrated_checked,
            "task_text_closer_to_canonical_than_other_lanes": hydrated_only_matches_canonical_lane,
            "note": "If high, current DB task_text mostly reflects the max-msg lane only — other lanes never indexed.",
        },
        "sessions": rows,
        "interpretation": {
            "real_problem_if": "multiplier >> 1 and rate_novel high and multi_family sessions common",
            "retrieval_enhancement_expected": "Extra lanes add distinct user-intent tokens not present in canonical-only embed text",
        },
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in report if k != "sessions"}, indent=2))
    print(f"wrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
