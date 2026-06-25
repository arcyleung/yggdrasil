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


@dataclass
class GateConfig:
    """Defaults match production agent skill gates; importers/eval can relax."""

    exclude_tags: frozenset[str] = _DEFAULT_EXCLUDE_TAGS
    exclude_tags_enabled: bool = True
    # If True, exclude_tags only applied when agent did not pass tags_any including them
    respect_explicit_tags_any: bool = True
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
    """Filter/reorder hits per skill gates. Does not re-query Qdrant."""
    cfg = config or GateConfig()
    dropped: list[dict[str, Any]] = []
    kept: list[SearchHit] = []
    warnings: list[str] = []

    explicit_tags = set(tags_any or [])
    skip_exclude = cfg.respect_explicit_tags_any and bool(
        explicit_tags & set(cfg.exclude_tags)
    )

    for h in hits:
        reasons: list[str] = []
        tags = set(h.tags or [])
        task = (h.task_text or "").strip()

        if cfg.exclude_tags_enabled and not skip_exclude:
            bad = tags & cfg.exclude_tags
            if bad:
                reasons.append(f"excluded_tags:{','.join(sorted(bad))}")

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
            if cfg.demote_noisy_instead_of_drop and "noisy_or_short_task_text" in reasons:
                # keep but sort later with penalty — append with note in dropped only
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
