#!/usr/bin/env python3
"""Sample multi-model sessions from Mongo; quantify single-canonical failure rate vs multi-lane gain."""
from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib_session_metrics import (  # noqa: E402
    analyze_session_docs,
    audit_to_dict,
    fetch_session_docs,
    mongo_uri_from_creds,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", type=int, default=40, help="Max multi-family sessions to audit in depth")
    ap.add_argument("--scan-sessions", type=int, default=400, help="How many distinct session_ids to probe for multi-model")
    ap.add_argument("--min-docs", type=int, default=5, help="Skip sessions with fewer proxy docs")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--creds", default=str(ROOT / "mongo_creds.txt"))
    ap.add_argument("--out", default=str(ROOT / "data_auditing/reports/fleet_report.json"))
    ap.add_argument(
        "--session-ids-out",
        default=str(ROOT / "data_auditing/reports/multi_model_session_ids.txt"),
    )
    ap.add_argument("--include", action="append", default=[], help="Force-include session_id (repeatable)")
    args = ap.parse_args()

    uri = mongo_uri_from_creds(args.creds)
    if not uri:
        print("error: no mongo uri", file=sys.stderr)
        return 1

    from pymongo import MongoClient

    random.seed(args.seed)
    cl = MongoClient(uri, serverSelectionTimeoutMS=20000)
    coll = cl["claude_conversations"]["conversations"]

    # Prefer session ids we already know are multi-doc: sample from hydrated parents + forced includes
    candidates: list[str] = list(args.include)
    # From SQLite parents
    try:
        import sqlite3

        db = ROOT / "data" / "yggdrasil.db"
        if db.exists():
            c = sqlite3.connect(db)
            for (tid,) in c.execute(
                "select id from trajectories where id like 'mongo-session-%' and id not like '%-seg-%' limit 5000"
            ):
                sid = tid[len("mongo-session-") :]
                candidates.append(sid)
    except Exception as e:
        print("sqlite_warn", e, file=sys.stderr)

    # Fill from mongo distinct (limited)
    if len(candidates) < args.scan_sessions:
        print("scanning mongo for session_ids…", file=sys.stderr)
        seen = set(candidates)
        for row in coll.aggregate([{"$group": {"_id": "$session_id"}}, {"$limit": args.scan_sessions * 3}], allowDiskUse=True):
            sid = row.get("_id")
            if sid and str(sid) not in seen:
                seen.add(str(sid))
                candidates.append(str(sid))
            if len(candidates) >= args.scan_sessions * 2:
                break

    random.shuffle(candidates)
    candidates = candidates[: max(args.scan_sessions, len(args.include))]

    multi_family_ids: list[str] = []
    audits: list[dict] = []
    flag_counter: Counter[str] = Counter()
    n_scanned = 0
    n_multi = 0
    n_single = 0
    units_single = 0
    units_multi = 0
    units_family_total = 0
    units_slices_total = 0
    extra_lanes_total = 0
    extra_chars_total = 0
    bprime_gt_family = 0
    slices_gt_bprime = 0

    for sid in candidates:
        n_scanned += 1
        n_docs = coll.count_documents({"session_id": sid})
        if n_docs < args.min_docs:
            continue
        # cheap multi-model probe: sample models field via small projection fetch
        models = set()
        for d in coll.find({"session_id": sid}, {"request_body.model": 1, "model": 1}).limit(80):
            rb = d.get("request_body") if isinstance(d.get("request_body"), dict) else {}
            m = rb.get("model") or d.get("model")
            if m:
                models.add(str(m))
        # family diversity
        from lib_session_metrics import _model_family

        families = {_model_family(m) for m in models}
        if len(families) < 2 and sid not in args.include:
            n_single += 1
            continue
        n_multi += 1
        multi_family_ids.append(sid)
        if len(audits) >= args.sample and sid not in args.include:
            continue
        # deep audit
        docs = fetch_session_docs(uri, sid)
        audit = analyze_session_docs(sid, docs)
        d = audit_to_dict(audit)
        audits.append(d)
        for f in d["flags"]:
            flag_counter[f] += 1
        ru = d.get("retrieval_units") or {}
        u_s = int(ru.get("single_canonical") or d.get("retrieval_units_single") or 1)
        u_b = int(ru.get("bprime_lanes") or d.get("retrieval_units_multilane") or 1)
        u_f = int(ru.get("family_lane_B") or 1)
        u_sl = int(ru.get("bprime_x_heuristic_slices") or u_b)
        units_single += u_s
        units_multi += u_b
        units_family_total += u_f
        units_slices_total += u_sl
        if u_b > u_f:
            bprime_gt_family += 1
        if u_sl > u_b:
            slices_gt_bprime += 1
        extra_lanes_total += d["extra_lanes_beyond_canonical"]
        extra_chars_total += d["extra_lane_chars_total"]
        lc = d.get("lane_counts") or {}
        print(
            f"audited {sid[:8]}… docs={d['n_docs']} "
            f"bprime={lc.get('bprime_system_model', u_b)} "
            f"family={lc.get('family_only_B', u_f)} "
            f"slices={lc.get('suggested_subtask_slices_sum', u_sl)} "
            f"extra={d['extra_lanes_beyond_canonical']} flags={len(d['flags'])}",
            file=sys.stderr,
        )

    cl.close()

    n_audited = len(audits)
    problem = sum(1 for a in audits if a["flags"])
    report = {
        "scan": {
            "candidates_considered": n_scanned,
            "sessions_with_min_docs_single_family_skip_est": n_single,
            "multi_family_found": n_multi,
            "deep_audited": n_audited,
        },
        "hypothesis_single_canonical_is_wrong": {
            "sessions_with_any_flag": problem,
            "sessions_audited": n_audited,
            "rate": (problem / n_audited) if n_audited else None,
            "flag_histogram": dict(flag_counter),
        },
        "multilane_gain": {
            "policy": "B_prime_system_hash_pipe_full_model",
            "retrieval_units_if_single_canonical": units_single,
            "retrieval_units_if_family_lane_B": units_family_total,
            "retrieval_units_if_bprime": units_multi,
            "retrieval_units_if_bprime_x_heuristic_slices": units_slices_total,
            "unit_multiplier_bprime_vs_single": (units_multi / units_single) if units_single else None,
            "unit_multiplier_slices_vs_single": (units_slices_total / units_single) if units_single else None,
            "sessions_bprime_gt_family": bprime_gt_family,
            "sessions_slices_gt_bprime": slices_gt_bprime,
            "extra_lanes_total": extra_lanes_total,
            "extra_lane_chars_total": extra_chars_total,
            "avg_extra_lanes_per_multi_session": (extra_lanes_total / n_audited) if n_audited else None,
        },
        "sessions": audits,
    }

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    Path(args.session_ids_out).write_text("\n".join(multi_family_ids) + "\n", encoding="utf-8")

    print(json.dumps({k: report[k] for k in ("scan", "hypothesis_single_canonical_is_wrong", "multilane_gain")}, indent=2))
    print(f"wrote {out}", file=sys.stderr)
    print(f"wrote {args.session_ids_out} ({len(multi_family_ids)} ids)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
