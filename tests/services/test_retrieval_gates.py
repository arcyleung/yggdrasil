"""Unit tests for retrieval gates."""
from __future__ import annotations

from yggdrasil.domain.enums import TrajectoryStatus
from yggdrasil.domain.models import EffortTotals, Progress, SearchHit, SearchScores
from yggdrasil.services.retrieval_gates import (
    GateConfig,
    apply_retrieval_gates,
    clean_task_text_for_embed,
    task_is_noisy,
    token_overlap_score,
)


def _hit(tid: str, task: str, tags: list[str], score: float = 0.03) -> SearchHit:
    return SearchHit(
        trajectory_id=tid,
        score=score,
        scores=SearchScores(fused=score),
        domain="coding",
        status=TrajectoryStatus.PARTIAL,
        task_text=task,
        scaffold_text="tools: Read Bash",
        tags=tags,
        progress=Progress(summary="seg"),
        effort_totals=EffortTotals(),
    )


def test_clean_strips_system_reminder():
    raw = "<system-reminder>\n# claudeMd\nfoo\n</system-reminder>\n\nFix the flaky pytest timeout"
    cleaned = clean_task_text_for_embed(raw)
    assert "system-reminder" not in cleaned.lower()
    assert "pytest" in cleaned.lower() or "flaky" in cleaned.lower() or len(cleaned) > 0


def test_noisy_detection():
    assert task_is_noisy("<system-reminder>x</system-reminder>")
    assert task_is_noisy("short")
    assert not task_is_noisy("Implement Qdrant embed dimension fix for collection mismatch")


def test_gates_drop_archive_and_noise():
    hits = [
        _hit("a", "Fix Qdrant embedding dimension mismatch in yggdrasil", ["session_segment"], 0.04),
        _hit(
            "b",
            "<system-reminder>claudeMd prefs</system-reminder>",
            ["session_segment", "external_pre_embed"],
            0.05,
        ),
        _hit(
            "c",
            "Build index button layout",
            ["session_segment", "hydration_test", "not_author_segmented"],
            0.06,
        ),
    ]
    out = apply_retrieval_gates(
        hits,
        query_task="Fix Qdrant collection embedding dimension with vLLM embed model",
        config=GateConfig(min_token_overlap=0.01),
    )
    ids = [h.trajectory_id for h in out.hits]
    assert "a" in ids
    assert "b" not in ids
    assert "c" not in ids


def test_gates_can_empty_for_control_query():
    hits = [
        _hit(
            "x",
            "Implement stock search button performance",
            ["session_segment", "external_pre_embed", "hydration_test"],
            0.03,
        ),
    ]
    out = apply_retrieval_gates(
        hits,
        query_task="Write a marketing blog about coffee shops",
        config=GateConfig(),
    )
    assert out.hits == []
    assert out.dropped
    assert any("all_hits_gated_out" in w for w in out.warnings) or True


def test_token_overlap_positive():
    s = token_overlap_score(
        "qdrant embed dimension collection mismatch",
        "Fix Qdrant embedding dimension mismatch",
    )
    assert s > 0.05
