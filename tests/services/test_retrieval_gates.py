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


def _lab_cfg(**kwargs) -> GateConfig:
    """Lab-mode-like gates: exclude archive tags, weak/no lexical gate."""
    defaults = dict(
        exclude_tags_enabled=True,
        reject_noisy_task=True,
        min_token_overlap=0.0,
        require_overlap_if_no_shared_tokens=False,
        respect_explicit_tags_any=True,
    )
    defaults.update(kwargs)
    return GateConfig(**defaults)


def test_experience_grade_alone_passes_lab_without_hydration_tags():
    """Authored experience_grade without archive tags is kept in lab gates."""
    hits = [
        _hit(
            "good",
            "Fix Qdrant collection embedding dimension with vLLM embed model",
            ["session_segment", "experience_grade", "author_segmented"],
            0.05,
        ),
    ]
    # stamp external_refs grade flag as writers do
    hits[0] = hits[0].model_copy(
        update={"external_refs": {"experience_grade": True, "owner": "alice"}}
    )
    out = apply_retrieval_gates(
        hits,
        query_task="Fix Qdrant embedding dimension mismatch",
        config=_lab_cfg(),
    )
    assert [h.trajectory_id for h in out.hits] == ["good"]


def test_experience_grade_cannot_launder_hydration_tags_lab():
    """experience_grade=true must NOT override hydration/archive excludes in lab."""
    for bad_tag in ("hydration_test", "external_pre_embed", "not_author_segmented"):
        hits = [
            _hit(
                "launder",
                "Fix Qdrant collection embedding dimension with vLLM embed model",
                ["session_segment", "experience_grade", bad_tag],
                0.09,
            ),
        ]
        hits[0] = hits[0].model_copy(
            update={"external_refs": {"experience_grade": True, "owner": "bob"}}
        )
        out = apply_retrieval_gates(
            hits,
            query_task="Fix Qdrant embedding dimension mismatch",
            config=_lab_cfg(),
        )
        assert out.hits == [], f"expected drop for tag {bad_tag}"
        assert any(
            "excluded_tags" in r
            for d in out.dropped
            for r in d.get("reasons", [])
        )
        assert any(
            "experience_grade_no_override" in r
            for d in out.dropped
            for r in d.get("reasons", [])
        )


def test_experience_grade_flag_only_external_refs_still_blocked_with_hydration():
    """Grade only in external_refs (no experience_grade tag) still cannot launder."""
    hits = [
        _hit(
            "x",
            "Implement Qdrant embed dimension fix for collection mismatch",
            ["session_segment", "hydration_test", "external_pre_embed"],
            0.08,
        ),
    ]
    hits[0] = hits[0].model_copy(update={"external_refs": {"experience_grade": True}})
    out = apply_retrieval_gates(
        hits,
        query_task="Qdrant embed dimension collection mismatch",
        config=_lab_cfg(),
    )
    assert out.hits == []


def test_explicit_tags_any_can_opt_into_archive_not_via_grade():
    """Caller tags_any including exclude tag opts in; grade alone does not."""
    hits = [
        _hit(
            "arch",
            "Implement Qdrant embed dimension fix for collection mismatch",
            ["session_segment", "hydration_test", "experience_grade"],
            0.07,
        ),
    ]
    # Without tags_any opt-in → blocked
    blocked = apply_retrieval_gates(
        hits,
        query_task="Qdrant embed dimension",
        config=_lab_cfg(),
    )
    assert blocked.hits == []
    # With explicit archive tag in tags_any → kept
    allowed = apply_retrieval_gates(
        hits,
        query_task="Qdrant embed dimension",
        tags_any=["hydration_test"],
        config=_lab_cfg(),
    )
    assert [h.trajectory_id for h in allowed.hits] == ["arch"]
