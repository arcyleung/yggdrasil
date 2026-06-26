from yggdrasil.adapters.qdrant_filters import compile_search_filter
from yggdrasil.ports.vector_index import VectorSearchQuery


def test_lab_tenant_filter_includes_legacy_missing_tenant_payloads():
    qfilter = compile_search_filter(VectorSearchQuery(tenant_id="lab"))

    assert qfilter is not None
    assert qfilter.model_dump(mode="json", exclude_none=True) == {
        "must": [
            {
                "should": [
                    {"key": "tenant_id", "match": {"value": "lab"}},
                    {"is_empty": {"key": "tenant_id"}},
                ]
            }
        ]
    }


def test_non_lab_tenant_filter_requires_exact_tenant_payload():
    qfilter = compile_search_filter(VectorSearchQuery(tenant_id="demo"))

    assert qfilter is not None
    assert qfilter.model_dump(mode="json", exclude_none=True) == {
        "must": [{"key": "tenant_id", "match": {"value": "demo"}}]
    }
