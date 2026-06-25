#!/usr/bin/env bash
# Run a command with a hard-ish process memory budget (default 24 GiB).
# Uses: ulimit -v (virtual KB on Linux) + YGG_MAX_RSS_GB for Python mem_limit watchdog.
#
# Usage:
#   scripts/run_with_memcap.sh 24 -- python scripts/mongo_importer_pre_embed.py --embed ...
#   scripts/run_with_memcap.sh 24 -- PYTHONPATH=src python scripts/export_mongo_sessions_jsonl.py ...
#
# Subagent guidance: always launch importer/export/eval through this wrapper (or set
# YGG_MAX_RSS_GB=24) so parallel agents cannot each balloon past the budget.

set -euo pipefail

MAX_GB="${1:-24}"
shift || true
if [[ "${1:-}" == "--" ]]; then
  shift
fi

if [[ $# -lt 1 ]]; then
  echo "usage: $0 [MAX_GB=24] -- command [args...]" >&2
  exit 2
fi

# Linux ulimit -v is in kilobytes
MAX_KB=$((MAX_GB * 1024 * 1024))
if ulimit -v "$MAX_KB" 2>/dev/null; then
  echo "run_with_memcap: ulimit -v = ${MAX_GB}GiB (${MAX_KB} KB)" >&2
else
  echo "run_with_memcap: warning: could not set ulimit -v (continuing with env only)" >&2
fi

export YGG_MAX_RSS_GB="$MAX_GB"
export YGG_MEM_CAP_GB="$MAX_GB"
export YGG_MEM_WATCHDOG="${YGG_MEM_WATCHDOG:-1}"

exec "$@"
