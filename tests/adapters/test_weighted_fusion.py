from yggdrasil.adapters.qdrant_index import fuse_weighted_scores


def test_weighted_prefers_higher_weight_aspect():
    # tid -> (task_score, scaffold_score)
    scores = {"a": (1.0, 0.0), "b": (0.0, 1.0)}
    ranked = fuse_weighted_scores(scores, w_task=0.8, w_scaffold=0.2)
    assert ranked[0][0] == "a"


def test_weights_normalized_when_not_unit_sum():
    scores = {"a": (1.0, 0.0), "b": (0.0, 1.0)}
    ranked = fuse_weighted_scores(scores, w_task=4.0, w_scaffold=1.0)
    assert ranked[0][0] == "a"
    # 4/5 * 1.0 + 1/5 * 0.0 = 0.8
    assert ranked[0][1] == 0.8


def test_missing_aspect_treated_as_zero():
    scores = {"only_task": (0.5, None), "only_scaffold": (None, 1.0)}
    ranked = fuse_weighted_scores(scores, w_task=0.5, w_scaffold=0.5)
    assert ranked[0][0] == "only_scaffold"
