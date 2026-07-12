from q15_upgrade.lineage import build_sha, lineage_stamp, stable_config_hash


def test_stable_config_hash_is_order_independent_and_value_sensitive():
    first = stable_config_hash({"b": {2, 1}, "a": {"x": True}})
    second = stable_config_hash({"a": {"x": True}, "b": {1, 2}})
    changed = stable_config_hash({"a": {"x": False}, "b": {1, 2}})
    assert first == second
    assert first != changed


def test_lineage_stamp_contains_only_allowlisted_values(monkeypatch):
    monkeypatch.setenv("Q15_BUILD_SHA", "abc123")
    build_sha.cache_clear()
    stamp = lineage_stamp(feature_schema_version="drift-v1", config={"distance": 3e-5})
    assert stamp["build_sha"] == "abc123"
    assert stamp["feature_schema_version"] == "drift-v1"
    assert len(stamp["config_hash"]) == 16
    build_sha.cache_clear()
