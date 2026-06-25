from pathlib import Path

from yggdrasil.adapters.importers.api_key_owners import (
    auth_label_from_fingerprint,
    assign_owners_for_fingerprints,
    fingerprint_auth_value,
    load_key_name_map,
    owner_label_map_from_key_name_map,
    owner_map_from_key_name_map,
)


def test_load_key_name_map_from_repo_yaml_shape(tmp_path: Path) -> None:
    mapping = tmp_path / "user_mapping.yaml"
    mapping.write_text(
        """
user_mapping:
  sk-test-key-one: alice
  sk-test-key-two: bob
""".strip(),
        encoding="utf-8",
    )

    assert load_key_name_map("user_mapping.yaml", base_dir=tmp_path) == {
        "sk-test-key-one": "alice",
        "sk-test-key-two": "bob",
    }


def test_owner_map_from_key_name_map_matches_bare_and_bearer_auth() -> None:
    owner_map = owner_map_from_key_name_map({"sk-test-key-one": "alice"})

    assert owner_map[fingerprint_auth_value("sk-test-key-one")] == "alice"
    assert owner_map[fingerprint_auth_value("Bearer sk-test-key-one")] == "alice"


def test_assign_owners_can_leave_unknowns_unmapped() -> None:
    known = fingerprint_auth_value("sk-known")
    unknown = fingerprint_auth_value("sk-unknown")

    owner_map = assign_owners_for_fingerprints(
        [known, unknown],
        existing_map={known: "alice"},
        assign_unknown=False,
    )

    assert owner_map == {known: "alice"}


def test_owner_label_map_matches_unique_redacted_auth_label() -> None:
    label_map = owner_label_map_from_key_name_map(
        {
            "sk-test-key-one-1234": "alice",
            "sk-other-key-two-5678": "bob",
        }
    )
    redacted_fp = "auth:redacteddgst:sk-…1234"

    assert label_map[auth_label_from_fingerprint(redacted_fp)] == "alice"
