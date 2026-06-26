#!/usr/bin/env python3
"""Deep audit one Mongo session_id: single-canonical vs multi-lane (policy B)."""
from __future__ import annotations

import argparse
import json
import sys
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
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--session-id", required=True)
    p.add_argument("--creds", default=str(ROOT / "mongo_creds.txt"))
    p.add_argument("--out", default=None, help="Write JSON report path")
    args = p.parse_args()

    uri = mongo_uri_from_creds(args.creds)
    if not uri:
        print("error: no mongo uri", file=sys.stderr)
        return 1
    docs = fetch_session_docs(uri, args.session_id)
    audit = analyze_session_docs(args.session_id, docs)
    payload = audit_to_dict(audit)
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    print(text)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
        print(f"wrote {out}", file=sys.stderr)
    # human summary
    print("\n# summary", file=sys.stderr)
    print(
        f"docs={audit.n_docs} multi_family={audit.multi_family} "
        f"bprime_lanes={audit.lanes_bprime} family_lanes={audit.lanes_family_only} "
        f"slice_sum={audit.total_suggested_slices_on_lane_max}",
        file=sys.stderr,
    )
    print(f"canonical={audit.canonical_family} nmsg={audit.canonical_nmsg} chars={audit.canonical_chars}", file=sys.stderr)
    print(f"extra_lanes={audit.extra_lanes_beyond_canonical} extra_chars={audit.extra_lane_chars_total}", file=sys.stderr)
    print(f"flags={audit.verdict_flags()}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
