# Data auditing — Mongo session canonicalization vs multi-lane

Validates whether **one canonical = max `message_count`** under-represents multi-agent proxy sessions, and whether **per-model lanes (policy B)** would improve experience retrieval coverage.

## Policy B (product direction)

- Keep **one parent** trajectory per `session_id` (session lineage).
- Emit **one embeddable lane per model** (max message_count within that model; tie-break chars, then latest time).
- Lab retrieval stays **tenant-wide**; lanes are extra strategies, not extra tenants.

## Scripts

| Script | Purpose |
|--------|---------|
| `audit_session_canonical.py` | Deep dive one `session_id` (Mongo live) |
| `audit_fleet_multi_model.py` | Sample multi-model sessions; mismatch rates vs single-canonical |
| `audit_multilane_retrieval.py` | Compare embeddable **text units** (canonical-only vs multi-lane) and optional Qdrant hit overlap |

## Run (from repo root)

```bash
export PYTHONPATH=src
.venv/bin/python data_auditing/audit_session_canonical.py \
  --session-id 8efd1fed-68fd-4ca2-bea9-678b8c617ec6

.venv/bin/python data_auditing/audit_fleet_multi_model.py --sample 40 --out data_auditing/reports/fleet_report.json

.venv/bin/python data_auditing/audit_multilane_retrieval.py \
  --session-ids-file data_auditing/reports/multi_model_session_ids.txt \
  --out data_auditing/reports/multilane_retrieval.json
```

Reports land in `data_auditing/reports/` (gitignored recommended for large dumps).

## Policy B′ (current)

```text
lane_key = sha256(normalize(system))[:16] + "|" + full_model_name
```

- Not `user_id` (session-scoped account id with `__session_<uuid>` suffix).
- Not family-only (two Haikus with different systems → two lanes).
- Within each lane maximizer, **subtask slices** may still be needed (serial goals on one Opus/Haiku timeline) — see `docs/superpowers/specs/2026-06-26-mongo-multilane-and-subtask-slices.md`.
