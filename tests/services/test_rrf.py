"""RRF helper tests."""
from yggdrasil.services.rrf import reciprocal_rank_fuse


def test_rrf_prefers_shared_high_ranks():
    fused = reciprocal_rank_fuse(
        {
            "task": ["a", "b", "c"],
            "scaffold": ["b", "a", "d"],
        },
        k=60,
    )
    ids = [doc_id for doc_id, _ in fused]
    assert ids[0] in {"a", "b"}
    assert set(ids) == {"a", "b", "c", "d"}


def test_rrf_scores_decrease_with_rank():
    fused = reciprocal_rank_fuse({"only": ["x", "y"]}, k=60)
    assert fused[0][0] == "x"
    assert fused[0][1] > fused[1][1]


def test_rrf_empty_lists():
    assert reciprocal_rank_fuse({}) == []
    assert reciprocal_rank_fuse({"task": []}) == []
