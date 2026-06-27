"""Post-retrieval gates for experience memory (skill-aligned, not doc RAG).

Applied after vector search + store hydration. Reduces false confidence from
hydration-test / system-reminder / off-domain segment hits.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from yggdrasil.domain.models import SearchHit

# Task keys that should not be trusted as strategy memory
_NOISE_TASK_RE = re.compile(
    r"(?is)(<system-reminder>|system-reminder|claudeMd\b|#\s*claudeMd|"
    r"\(from assistant plan\)|tool_result:|^\s*\[TOOL_OUTPUT)"
)
_MIN_GOAL_CHARS = 24
# Tags that mark externally segmented hydration corpus (archive unless agent opts in)
_DEFAULT_EXCLUDE_TAGS = frozenset(
    {
        "hydration_test",
        "not_author_segmented",
        "external_pre_embed",
    }
)
_STOP = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "is",
        "it",
        "this",
        "that",
        "be",
        "as",
        "at",
        "by",
        "from",
        "use",
        "using",
        "via",
        "into",
        "agent",
        "coding",
        "task",
        "tools",
        "tool",
        "model",
        "python",
        "claude",
    }
)


def clean_task_text_for_embed(text: str, *, max_len: int = 2000) -> str:
    """Strip system-reminder / assistant-plan noise from segment task keys."""
    if not text:
        return ""
    t = text.strip()
    # Drop leading system-reminder blocks (common in proxy imports)
    t = re.sub(r"(?is)<system-reminder>.*?</system-reminder>\s*", " ", t)
    t = re.sub(r"(?is)^\s*system-reminder[:\s].*?(?=\n\n|\Z)", " ", t)
    t = re.sub(r"(?is)\(from assistant plan\)\s*", "", t)
    # If still dominated by claudeMd injection, take last non-empty line as weak fallback
    if _NOISE_TASK_RE.search(t) and "claudeMd" in t:
        lines = [ln.strip() for ln in t.splitlines() if ln.strip() and "system-reminder" not in ln.lower()]
        # prefer a line that looks like a user ask
        for ln in reversed(lines):
            if len(ln) >= 20 and not ln.startswith("#") and "claudeMd" not in ln:
                t = ln
                break
    t = re.sub(r"\s+", " ", t).strip()
    return t[:max_len]


def task_is_noisy(task_text: str | None) -> bool:
    if not task_text or not task_text.strip():
        return True
    t = task_text.strip()
    if len(t) < _MIN_GOAL_CHARS:
        return True
    if _NOISE_TASK_RE.search(t):
        return True
    return False


def _tokens(text: str) -> set[str]:
    return {
        w
        for w in re.findall(r"[a-z0-9_]{3,}", (text or "").lower())
        if w not in _STOP
    }


def token_overlap_score(query_task: str | None, hit_task: str | None) -> float:
    """Jaccard-ish overlap on content tokens; 0 = none, 1 = identical token sets."""
    a, b = _tokens(query_task or ""), _tokens(hit_task or "")
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def hit_claims_experience_grade(hit: SearchHit) -> bool:
    """True if payload/tags claim authored experience_grade trust.

    This flag alone is **never** sufficient to pass lab/agent gates when
    hydration/archive provenance tags are also present.
    """
    tags = set(hit.tags or [])
    if "experience_grade" in tags or "author_segmented" in tags:
        return True
    # B′ multi-lane imports are org experience (not hydration_test archive)
    if "multilane_bprime" in tags or "session_lane_slice" in tags:
        return True
    refs = hit.external_refs or {}
    return bool(refs.get("experience_grade") or refs.get("multilane_policy"))


def _as_utc_datetime(when: Any) -> datetime | None:
    """Parse datetime or ISO string to aware UTC; None if unusable."""
    from datetime import datetime, timezone

    if when is None:
        return None
    if isinstance(when, str):
        try:
            when = datetime.fromisoformat(when.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(when, datetime):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def iso_timestamp(when: Any) -> str | None:
    """Serialize event time as ISO-8601 UTC (…Z) for agents; None if unknown."""
    dt = _as_utc_datetime(when)
    if dt is None:
        return None
    return dt.isoformat().replace("+00:00", "Z")


def time_range_label(when: Any) -> str:
    """Bucket a datetime (or ISO string) into today | week | month | older | unknown.

    Prefer client-side binning from ranked[].at; kept for tests / optional UI.
    """
    from datetime import datetime, timedelta, timezone

    dt = _as_utc_datetime(when)
    if dt is None:
        return "unknown"
    now = datetime.now(timezone.utc)
    age = now - dt
    if age <= timedelta(days=1):
        return "today"
    if age <= timedelta(days=7):
        return "week"
    if age <= timedelta(days=30):
        return "month"
    return "older"


def experience_event_time(hit: SearchHit) -> Any:
    """Best-effort *experience* timestamp (when work happened), not re-index mtime.

    Prefer finalized_at (session close / outcome), then external occurred_at / created_at,
    then trajectory created hints in refs. Avoid updated_at alone — importers often
    stamp it at embed time so everything looks like \"today\".
    """
    refs = hit.external_refs or {}
    for candidate in (
        hit.finalized_at,
        refs.get("occurred_at"),
        refs.get("finalized_at"),
        refs.get("created_at"),
        refs.get("session_ended_at"),
        refs.get("ended_at"),
    ):
        if candidate is not None:
            return candidate
    # Last resort: updated_at (index / row mtime)
    return hit.updated_at or refs.get("updated_at")


def outcome_rank(status: str | None) -> int:
    """Higher is better for ranking (success > partial > open > fail > unknown)."""
    s = (status or "").lower()
    if s == "success":
        return 4
    if s in ("partial", "open"):
        return 3
    if s == "aborted":
        return 2
    if s == "fail":
        return 1
    return 0


def rank_experience_hits(
    hits: list[SearchHit],
    *,
    query_task: str | None = None,
    prefer_low_waste: bool = True,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Rank hits for org discovery: success, event-time recency, relevance, low waste.

    Each row carries ISO datestamp ``at`` (experience event time). Agents bin into
    today / this week / this month / older / unknown using their clock — do not
    rely on a server ``range`` label (re-index ``updated_at`` used to mis-bin all hits
    as \"today\").
    """
    from datetime import datetime, timezone

    rows: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for h in hits:
        refs = h.external_refs or {}
        owner = h.owner or refs.get("owner") or "unknown"
        agent_id = h.agent_id or refs.get("agent_id")
        team = h.team or refs.get("team")
        when = experience_event_time(h)
        when_dt = _as_utc_datetime(when)
        at_iso = iso_timestamp(when)
        # Index/row mtime (often embed time) — separate from experience event time
        indexed_iso = iso_timestamp(h.updated_at) or iso_timestamp(refs.get("updated_at"))
        status = None
        if h.outcome is not None:
            ts = getattr(h.outcome, "terminal_status", None)
            status = ts.value if hasattr(ts, "value") else (str(ts) if ts else None)
        status = status or (h.status.value if hasattr(h.status, "value") else str(h.status))
        overlap = token_overlap_score(query_task, h.task_text)
        score = float(h.score or 0.0)
        waste = None
        if h.effort_totals is not None:
            waste = h.effort_totals.failure_waste_seconds
        # Recency key: epoch seconds (newer = larger) for sort; unknown → 0
        recency_epoch = when_dt.timestamp() if when_dt is not None else 0.0
        waste_key = waste if waste is not None else (0.0 if prefer_low_waste else 0.0)
        rows.append(
            {
                # Primary datestamp for agent tables / client-side range bins
                "at": at_iso,
                "occurred_at": at_iso,
                "indexed_at": indexed_iso,
                "owner": owner,
                "agent_id": agent_id,
                "team": team,
                "trajectory_id": h.trajectory_id,
                "trajectory": {
                    "id": h.trajectory_id,
                    "task_text": (h.task_text or "")[:500],
                    "scaffold_text": (h.scaffold_text or "")[:300],
                    "score": score,
                    "tags": list(h.tags or [])[:20],
                },
                "outcome": status,
                "relevance_score": score,
                "token_overlap": round(overlap, 4),
                "failure_waste_seconds": waste,
                "updated_at": indexed_iso,
                "finalized_at": iso_timestamp(h.finalized_at),
                "_sort": (
                    outcome_rank(status),
                    recency_epoch,
                    score,
                    overlap,
                    -(waste_key if prefer_low_waste and waste_key is not None else 0.0),
                ),
            }
        )
    rows.sort(key=lambda r: r["_sort"], reverse=True)
    for r in rows:
        del r["_sort"]
    if limit is not None:
        rows = rows[:limit]
    return rows


def group_ranked_by_range(
    ranked: list[dict[str, Any]],
    *,
    now: Any = None,
) -> dict[str, list[dict[str, Any]]]:
    """Optional client-style bins from ranked[].at (for tests/UI). MCP no longer returns this.

    Prefer implementing the same bins in the agent skill from ISO ``at`` timestamps.
    """
    out: dict[str, list[dict[str, Any]]] = {
        "today": [],
        "week": [],
        "month": [],
        "older": [],
        "unknown": [],
    }
    for r in ranked:
        when = r.get("at") or r.get("occurred_at") or r.get("range")
        key = time_range_label(when)
        if key not in out:
            key = "unknown"
        out[key].append(r)
    return out


@dataclass
class GateConfig:
    """Defaults match production agent skill gates; importers/eval can relax."""

    exclude_tags: frozenset[str] = _DEFAULT_EXCLUDE_TAGS
    exclude_tags_enabled: bool = True
    # If True, exclude_tags only applied when agent did not pass tags_any including them
    respect_explicit_tags_any: bool = True
    # experience_grade tag/flag must never launder hydration/archive excludes (Wave D)
    experience_grade_cannot_override_excludes: bool = True
    reject_noisy_task: bool = True
    min_token_overlap: float = 0.02  # weak; 0 disables lexical gate
    require_overlap_if_no_shared_tokens: bool = True
    demote_noisy_instead_of_drop: bool = False
    # Report-only: attach gate_meta on hits via extra field not on model — use list filter
    pass_through_if_all_filtered: bool = False  # if True, return unfiltered when empty


@dataclass
class GatedSearchResult:
    hits: list[SearchHit]
    dropped: list[dict[str, Any]] = field(default_factory=list)
    gate_applied: bool = True
    warnings: list[str] = field(default_factory=list)


def apply_retrieval_gates(
    hits: Iterable[SearchHit],
    *,
    query_task: str | None,
    query_scaffold: str | None = None,
    tags_any: list[str] | None = None,
    config: GateConfig | None = None,
) -> GatedSearchResult:
    """Filter/reorder hits per skill gates. Does not re-query Qdrant.

    Trust rules (lab and agent):
    - Tags ``hydration_test``, ``external_pre_embed``, ``not_author_segmented`` are
      excluded when ``exclude_tags_enabled`` (default on unless ``include_archive``).
    - ``experience_grade=true`` (tag or external_refs) **cannot** override those
      excludes — graded hydration corpus is still archive noise.
    - Only an explicit caller ``tags_any`` that intersects exclude_tags opts in
      (archive forensics), and only when ``respect_explicit_tags_any`` is True.
    """
    cfg = config or GateConfig()
    dropped: list[dict[str, Any]] = []
    kept: list[SearchHit] = []
    warnings: list[str] = []

    explicit_tags = set(tags_any or [])
    # Caller must name an exclude tag in tags_any to opt into archive — never via grade alone
    skip_exclude = cfg.respect_explicit_tags_any and bool(
        explicit_tags & set(cfg.exclude_tags)
    )

    for h in hits:
        reasons: list[str] = []
        tags = set(h.tags or [])
        task = (h.task_text or "").strip()
        graded = hit_claims_experience_grade(h)

        if cfg.exclude_tags_enabled and not skip_exclude:
            bad = tags & cfg.exclude_tags
            if bad:
                reason = f"excluded_tags:{','.join(sorted(bad))}"
                # Explicit laundering guard: grade does not clear hydration provenance
                if graded and cfg.experience_grade_cannot_override_excludes:
                    reason += "+experience_grade_no_override"
                reasons.append(reason)

        if cfg.reject_noisy_task and task_is_noisy(task):
            reasons.append("noisy_or_short_task_text")

        overlap = token_overlap_score(query_task, task)
        if cfg.min_token_overlap > 0 and cfg.require_overlap_if_no_shared_tokens:
            qtok = _tokens(query_task or "")
            if qtok and overlap < cfg.min_token_overlap and not reasons:
                # only apply overlap gate if query has content tokens
                if not (_tokens(task) & qtok):
                    reasons.append(f"no_token_overlap:{overlap:.3f}")

        if reasons:
            # Lab wide-recall: keep noisy tasks with a penalty, but never keep archive excludes
            has_exclude = any(str(r).startswith("excluded_tags") for r in reasons)
            if (
                cfg.demote_noisy_instead_of_drop
                and "noisy_or_short_task_text" in reasons
                and not has_exclude
            ):
                kept.append(h)
                dropped.append(
                    {
                        "trajectory_id": h.trajectory_id,
                        "reasons": reasons + ["demoted_kept"],
                        "score": h.score,
                    }
                )
            else:
                dropped.append(
                    {
                        "trajectory_id": h.trajectory_id,
                        "reasons": reasons,
                        "score": h.score,
                        "task_preview": task[:120],
                    }
                )
            continue
        kept.append(h)

    # Stable: prefer higher overlap then score among kept
    def sort_key(h: SearchHit) -> tuple:
        ov = token_overlap_score(query_task, h.task_text)
        noisy_pen = 1 if task_is_noisy(h.task_text) else 0
        return (noisy_pen, -ov, -(h.score or 0.0))

    kept.sort(key=sort_key)

    if not kept and list(hits):
        warnings.append(
            "all_hits_gated_out: no trusted experience memory for this query "
            "(hydration/archive noise or off-domain segments). Proceed without prior steps."
        )
        if cfg.pass_through_if_all_filtered:
            return GatedSearchResult(
                hits=list(hits),
                dropped=dropped,
                gate_applied=True,
                warnings=warnings + ["pass_through_unfiltered"],
            )

    if kept and query_task:
        top_ov = token_overlap_score(query_task, kept[0].task_text)
        if top_ov < 0.01 and _tokens(query_task):
            warnings.append(
                "top_hit_weak_lexical_match: treat as low-confidence; verify before get_trajectory"
            )

    return GatedSearchResult(hits=kept, dropped=dropped, gate_applied=True, warnings=warnings)
