from yggdrasil.adapters.qdrant_index import point_id_for_trajectory


def test_uuid_trajectory_id_round_trips():
    tid = "550e8400-e29b-41d4-a716-446655440000"
    assert point_id_for_trajectory(tid) == tid


def test_non_uuid_is_stable_across_calls():
    tid = "mongo-session-abc-seg-0000"
    a = point_id_for_trajectory(tid)
    b = point_id_for_trajectory(tid)
    assert a == b
    # Must not depend on PYTHONHASHSEED / process
    assert isinstance(a, str)
    assert len(a) == 36  # UUID string


def test_different_ids_differ():
    assert point_id_for_trajectory("seg-a") != point_id_for_trajectory("seg-b")
