#!/usr/bin/env python3
"""Scan Mongo proxy logs → auth fingerprints → real owner map.

Never writes full Authorization values—only fingerprints + session counts.
Full API key → owner mappings are loaded from KEY_NAME_MAP / user_mapping.yaml
and converted to fingerprints in memory.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from lib.mongo_session_io import read_mongo_uri  # noqa: E402
from yggdrasil.adapters.importers.api_key_owners import (  # noqa: E402
    DEFAULT_OWNER_ROSTER,
    assign_owners_for_fingerprints,
    auth_label_from_fingerprint,
    identity_from_mongo_doc,
    load_key_name_map,
    load_owner_map,
    owner_label_map_from_key_name_map,
    owner_map_from_key_name_map,
    save_owner_map,
)


def main() -> int:
    p = argparse.ArgumentParser(description="Build fingerprint→owner map from Mongo (safe)")
    p.add_argument("--creds", type=Path, default=ROOT / "mongo_creds.txt")
    p.add_argument("--out", type=Path, default=ROOT / "data" / "exports" / "api_key_owner_map.json")
    p.add_argument("--merge", type=Path, default=None, help="Existing map to merge/prefer")
    p.add_argument(
        "--key-name-map",
        default=None,
        help=(
            "Full API key → owner mapping as a path, JSON object, or key=name CSV. "
            "Defaults to KEY_NAME_MAP from .env; paths are resolved from repo root."
        ),
    )
    p.add_argument(
        "--auto-assign-mock-owners",
        action="store_true",
        help="Demo mode: assign remaining unknown fingerprints to alice/bob/... roster.",
    )
    p.add_argument("--limit", type=int, default=None, help="Max request docs to scan")
    p.add_argument("--session-sidecar", type=Path, default=ROOT / "data" / "exports" / "session_owner_index.jsonl")
    args = p.parse_args()

    uri = read_mongo_uri(args.creds)
    if not uri:
        print("No MONGO_URI in mongo_creds.txt", file=sys.stderr)
        return 1

    existing = load_owner_map(args.merge) if args.merge else {}
    key_name_map = load_key_name_map(args.key_name_map, base_dir=ROOT)
    explicit_owner_map = owner_map_from_key_name_map(key_name_map)
    explicit_label_map = owner_label_map_from_key_name_map(key_name_map)
    if explicit_owner_map:
        # Explicit real user mapping wins over old generated/demo owner aliases.
        existing.update(explicit_owner_map)
        print(f"loaded {len(key_name_map)} KEY_NAME_MAP entries → {len(explicit_owner_map)} auth fingerprints")
    else:
        print(
            "WARN: no KEY_NAME_MAP/user_mapping entries loaded; "
            "unmapped identities will be owner=unknown",
            file=sys.stderr,
        )
    fp_counts: Counter[str] = Counter()
    session_fp: dict[str, str] = {}
    session_meta: dict[str, dict] = {}

    # Lean projection: only fields needed for identity (still strips full key at fingerprint step)
    from pymongo import MongoClient

    client = MongoClient(uri, serverSelectionTimeoutMS=15000)
    coll = client["claude_conversations"]["conversations"]
    proj = {"request_headers.Authorization": 1, "request_headers.authorization": 1,
            "request_headers.X-Api-Key": 1, "request_headers.x-api-key": 1,
            "session_id": 1, "user_id": 1}
    cursor = coll.find({}, proj)
    if args.limit is not None:
        cursor = cursor.limit(args.limit)

    n_docs = 0
    try:
        for doc in cursor:
            n_docs += 1
            if "_id" in doc and not isinstance(doc["_id"], (str, dict)):
                doc = dict(doc)
                doc["_id"] = {"$oid": str(doc["_id"])}
            ident = identity_from_mongo_doc(doc)
            key = ident.get("identity_key")
            if not key:
                continue
            fp_counts[key] += 1
            sid = doc.get("session_id") or ident.get("request_id")
            if sid and sid not in session_fp:
                session_fp[sid] = key
                session_meta[sid] = {
                    "session_id": sid,
                    "identity_key": key,
                    "api_key_fingerprint": ident.get("api_key_fingerprint"),
                    "user_id_fingerprint": ident.get("user_id_fingerprint"),
                }
            if n_docs % 5000 == 0:
                print(f"  scanned {n_docs} requests, {len(session_fp)} sessions…", flush=True)
    finally:
        client.close()

    ordered = [fp for fp, _ in fp_counts.most_common()]
    label_matched = 0
    if explicit_label_map:
        for fp in ordered:
            if fp in existing:
                continue
            label = auth_label_from_fingerprint(fp)
            owner = explicit_label_map.get(label or "")
            if owner:
                existing[fp] = owner
                label_matched += 1

    owner_map = assign_owners_for_fingerprints(
        ordered,
        existing_map=existing,
        roster=DEFAULT_OWNER_ROSTER,
        assign_unknown=args.auto_assign_mock_owners,
    )

    meta = {
        "scanned_request_docs": n_docs,
        "distinct_identities": len(ordered),
        "distinct_sessions": len(session_fp),
        "explicit_key_name_entries": len(key_name_map),
        "redacted_label_matches": label_matched,
        "mapped_identities": sum(1 for fp in ordered if fp in owner_map),
        "unmapped_identities": sum(1 for fp in ordered if fp not in owner_map),
        "mock_owner_assignment": bool(args.auto_assign_mock_owners),
        "request_counts_by_owner": {},
        "identity_keys_by_owner": {},
    }
    for fp, owner in owner_map.items():
        meta["request_counts_by_owner"].setdefault(owner, 0)
        meta["request_counts_by_owner"][owner] += fp_counts.get(fp, 0)
        meta["identity_keys_by_owner"].setdefault(owner, []).append(fp)

    save_owner_map(args.out, owner_map, meta=meta)
    print(f"wrote {args.out} ({len(owner_map)} owners/identities)")
    for owner, n in sorted(meta["request_counts_by_owner"].items(), key=lambda x: -x[1])[:12]:
        print(f"  {owner}: ~{n} requests")

    args.session_sidecar.parent.mkdir(parents=True, exist_ok=True)
    with args.session_sidecar.open("w", encoding="utf-8") as f:
        for sid, ident_key in session_fp.items():
            owner = owner_map.get(ident_key, "unknown")
            row = {
                **session_meta[sid],
                "owner": owner,
                "agent_id": f"{owner}-mongo-agent",
                "team": "org-proxy-logs",
            }
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"wrote session sidecar {args.session_sidecar} ({len(session_fp)} sessions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
