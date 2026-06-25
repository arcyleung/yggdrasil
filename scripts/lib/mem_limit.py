"""Process memory caps for long mongo export/import/embed jobs and subagent runners.

Default agent/importer budget: **24 GiB** virtual address space (RLIMIT_AS) so a single
Python worker cannot allocate past that even if RSS is lower. Optional soft RSS monitor
aborts cleanly before the OOM killer.

Usage (in scripts, early main):
    from lib.mem_limit import apply_memory_cap
    apply_memory_cap(max_gb=24)

CLI wrapper:
    scripts/run_with_memcap.sh 24 -- python scripts/mongo_importer_pre_embed.py ...
"""
from __future__ import annotations

import os
import resource
import sys
import threading
import time
from typing import Callable


DEFAULT_MAX_GB = 24.0
_GIB = 1024**3


def _parse_max_gb(explicit: float | None = None) -> float:
    if explicit is not None:
        return float(explicit)
    env = os.environ.get("YGG_MAX_RSS_GB") or os.environ.get("YGG_MEM_CAP_GB")
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    return DEFAULT_MAX_GB


def apply_rlimit_as(max_gb: float = DEFAULT_MAX_GB) -> tuple[int, int]:
    """Set RLIMIT_AS (address space) soft+hard to max_gb GiB. Returns (soft, hard) bytes."""
    limit = int(max_gb * _GIB)
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ValueError, resource.error) as exc:
        # Some platforms disallow lowering; try soft only
        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_AS)
            new_soft = min(limit, hard) if hard != resource.RLIM_INFINITY else limit
            resource.setrlimit(resource.RLIMIT_AS, (new_soft, hard))
            return resource.getrlimit(resource.RLIMIT_AS)
        except Exception:
            print(f"mem_limit: could not set RLIMIT_AS ({exc})", file=sys.stderr)
            return resource.getrlimit(resource.RLIMIT_AS)
    return resource.getrlimit(resource.RLIMIT_AS)


def rss_bytes() -> int:
    """Current process RSS in bytes (Linux /proc preferred)."""
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    # kB
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    # ru_maxrss is KB on Linux, bytes on macOS — use only as fallback
    usage = resource.getrusage(resource.RUSAGE_SELF)
    val = usage.ru_maxrss
    if sys.platform == "darwin":
        return int(val)
    return int(val) * 1024


def start_rss_watchdog(
    max_gb: float = DEFAULT_MAX_GB,
    *,
    interval_sec: float = 2.0,
    on_exceed: Callable[[int, int], None] | None = None,
) -> threading.Thread:
    """Background thread: exit process if RSS exceeds max_gb (soft abort)."""
    limit = int(max_gb * _GIB)

    def _loop() -> None:
        while True:
            time.sleep(interval_sec)
            try:
                cur = rss_bytes()
            except Exception:
                continue
            if cur >= limit:
                if on_exceed:
                    on_exceed(cur, limit)
                else:
                    print(
                        f"mem_limit: RSS {cur / _GIB:.2f} GiB >= cap {max_gb:.1f} GiB — aborting",
                        file=sys.stderr,
                        flush=True,
                    )
                os._exit(137)

    t = threading.Thread(target=_loop, name="ygg-rss-watchdog", daemon=True)
    t.start()
    return t


def apply_memory_cap(
    max_gb: float | None = None,
    *,
    rlimit: bool = True,
    rss_watchdog: bool = True,
    log: bool = True,
) -> float:
    """Apply default 24 GiB (or env/override) memory policy. Returns effective max_gb."""
    gb = _parse_max_gb(max_gb)
    if rlimit:
        soft, hard = apply_rlimit_as(gb)
        if log:
            def _fmt(x: int) -> str:
                if x == resource.RLIM_INFINITY or x < 0:
                    return "inf"
                return f"{x / _GIB:.1f}GiB"

            print(
                f"mem_limit: RLIMIT_AS soft={_fmt(soft)} hard={_fmt(hard)} (target={gb:.1f}GiB)",
                file=sys.stderr,
                flush=True,
            )
    if rss_watchdog and os.environ.get("YGG_MEM_WATCHDOG", "1") not in ("0", "false", "no"):
        start_rss_watchdog(gb)
        if log:
            print(
                f"mem_limit: RSS watchdog armed at {gb:.1f} GiB (disable: YGG_MEM_WATCHDOG=0)",
                file=sys.stderr,
                flush=True,
            )
    return gb
