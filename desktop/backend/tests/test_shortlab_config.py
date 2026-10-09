"""Task 1: Short-Lab configuration contract.

Covers design doc (`ShortLab_Detailed_Design_CN.md`) section 24 / 24.1 and
`ShortLab_Implementation_Plan_CN.md` Task 1:

- default values (universe/entry/funding/refresh),
- single-key user override preserves remaining defaults (deep merge),
- unknown keys / negative TTL / weights not summing to 100 raise,
- `default.yaml` matches design section 24 key-for-key,
- `config_hash` canonical-JSON algorithm with the appendix golden value,
- hash ignores refresh/providers/paths/secrets, reacts to scoring keys,
- `engine.loader.load_config()` unaffected by Short-Lab config reads.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from diveintocrypto_desktop.engine import loader as engine_loader
from diveintocrypto_desktop.shortlab.config import (
    DEFAULT_CONFIG_PATH,
    SHORTLAB_CONFIG_PATH_ENV,
    ShortLabConfigError,
    config_hash,
    cost_config_hash,
    load_shortlab_config,
)

GOLDEN_SCORING_HASH = "4909ffe7d43c294983d313cff65c6d73125b921944e403a1e1e152b91a869786"

EXPECTED_SCORE_PROFILES = {
    "MEME_LITE",
    "GENERAL_LITE",
    "LOW_FLOAT_VC_LITE",
    "MEME_FULL",
    "GENERAL_FULL",
    "LOW_FLOAT_VC_FULL",
}

EXPECTED_FRESHNESS_GROUPS = {
    "market_futures",
    "funding_history",
    "fundamentals",
    "spot_liquidity",
    "identity_profile",
    "unlock",
    "social",
    "catalyst",
}


def _write_user_yaml(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "shortlab.override.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Defaults (design section 24)
# ---------------------------------------------------------------------------


def test_default_universe_and_entry_values():
    config = load_shortlab_config()
    assert config.universe.limit == 500
    assert config.universe.shortlist_size == 50
    assert config.universe.entry_depth_top == 10
    assert config.refresh.entry_concurrency == 2
    assert config.refresh.entry_max_upstream_calls_per_run == 240


def test_default_funding_budget():
    config = load_shortlab_config()
    assert config.funding.history_requests_per_5min == 80
    assert config.funding.backfill_symbols_per_batch == 80
    assert config.funding.lookbacks_days == (7, 30, 90)


def test_default_refresh_intervals():
    config = load_shortlab_config()
    assert config.refresh.jitter_sec == 300
    assert config.refresh.supply_sec == 21600
    assert config.refresh.fundamental_sec == 3600


def test_default_candidate_thresholds():
    config = load_shortlab_config()
    assert (config.candidate.watch_ltss, config.candidate.candidate_ltss) == (60, 70)
    assert (config.candidate.ready_ltss, config.candidate.ready_entry) == (80, 70)
    assert config.candidate.ready_data_quality == 80
    assert config.candidate.ready_tradeability_score == 7


# ---------------------------------------------------------------------------
# Deep merge of user overrides
# ---------------------------------------------------------------------------


def test_single_key_override_preserves_remaining_defaults(tmp_path):
    path = _write_user_yaml(tmp_path, {"shortlab": {"universe": {"limit": 100}}})
    config = load_shortlab_config(path)
    assert config.universe.limit == 100
    # everything else keeps package defaults
    assert config.universe.shortlist_size == 50
    assert config.universe.entry_depth_top == 10
    assert config.candidate.ready_entry == 70
    assert config.refresh.jitter_sec == 300


def test_env_var_shortlab_config_path_overrides_package_yaml(tmp_path, monkeypatch):
    path = _write_user_yaml(tmp_path, {"shortlab": {"universe": {"limit": 123}}})
    monkeypatch.setenv(SHORTLAB_CONFIG_PATH_ENV, str(path))
    assert load_shortlab_config().universe.limit == 123


def test_explicit_path_beats_env_var(tmp_path, monkeypatch):
    env_file = tmp_path / "env.yaml"
    env_file.write_text(yaml.safe_dump({"shortlab": {"universe": {"limit": 111}}}), encoding="utf-8")
    explicit = tmp_path / "explicit.yaml"
    explicit.write_text(yaml.safe_dump({"shortlab": {"universe": {"limit": 222}}}), encoding="utf-8")
    monkeypatch.setenv(SHORTLAB_CONFIG_PATH_ENV, str(env_file))
    assert load_shortlab_config(explicit).universe.limit == 222


# ---------------------------------------------------------------------------
# Rejections: unknown keys, negative TTL, bad weight sums, bad order
# ---------------------------------------------------------------------------


def test_unknown_top_level_key_raises(tmp_path):
    path = _write_user_yaml(tmp_path, {"shortlab": {"no_such_section": {}}})
    with pytest.raises(ShortLabConfigError):
        load_shortlab_config(path)


def test_unknown_nested_key_raises(tmp_path):
    path = _write_user_yaml(tmp_path, {"shortlab": {"universe": {"nope": 1}}})
    with pytest.raises(ShortLabConfigError):
        load_shortlab_config(path)


def test_unknown_score_profile_key_raises(tmp_path):
    path = _write_user_yaml(tmp_path, {"shortlab": {"score_weights": {"MADE_UP": {"carry": 100}}}})
    with pytest.raises(ShortLabConfigError):
        load_shortlab_config(path)


def test_negative_ttl_raises(tmp_path):
    path = _write_user_yaml(
        tmp_path,
        {"shortlab": {"quality_freshness_sec": {"market_futures": {"ttl": -5, "grace": 3600}}}},
    )
    with pytest.raises(ShortLabConfigError):
        load_shortlab_config(path)


def test_negative_refresh_period_raises(tmp_path):
    path = _write_user_yaml(tmp_path, {"shortlab": {"refresh": {"jitter_sec": -1}}})
    with pytest.raises(ShortLabConfigError):
        load_shortlab_config(path)


def test_grace_must_exceed_ttl(tmp_path):
    path = _write_user_yaml(
        tmp_path,
        {"shortlab": {"quality_freshness_sec": {"market_futures": {"ttl": 3600, "grace": 900}}}},
    )
    with pytest.raises(ShortLabConfigError):
        load_shortlab_config(path)


def test_score_weights_must_sum_to_100(tmp_path):
    # Override a single weight: deep merge keeps the rest, sum becomes 101.
    path = _write_user_yaml(
        tmp_path, {"shortlab": {"score_weights": {"GENERAL_LITE": {"carry": 41}}}}
    )
    with pytest.raises(ShortLabConfigError):
        load_shortlab_config(path)


def test_threshold_order_ready_gte_candidate_gte_watch(tmp_path):
    path = _write_user_yaml(tmp_path, {"shortlab": {"candidate": {"ready_ltss": 65}}})
    with pytest.raises(ShortLabConfigError):
        load_shortlab_config(path)


def test_missing_override_file_raises(tmp_path):
    with pytest.raises(ShortLabConfigError):
        load_shortlab_config(tmp_path / "does-not-exist.yaml")


# ---------------------------------------------------------------------------
# default.yaml matches design section 24
# ---------------------------------------------------------------------------


def test_default_yaml_has_six_score_profiles_each_summing_to_100():
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))["shortlab"]
    assert set(raw["score_weights"]) == EXPECTED_SCORE_PROFILES
    for profile, weights in raw["score_weights"].items():
        assert sum(weights.values()) == 100, profile


def test_default_yaml_has_eight_freshness_groups_with_grace_gt_ttl_gt_0():
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))["shortlab"]
    assert set(raw["quality_freshness_sec"]) == EXPECTED_FRESHNESS_GROUPS
    for group, window in raw["quality_freshness_sec"].items():
        assert window["grace"] > window["ttl"] > 0, group


def test_default_yaml_supply_float_override_is_6h_ttl_12h_grace():
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))["shortlab"]
    override = raw["quality_field_overrides_sec"]["supply_float"]
    assert override == {"ttl": 21600, "grace": 43200}


def test_default_yaml_top_level_keys_match_design_section_24():
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))["shortlab"]
    assert set(raw) == {
        "enabled",
        "analysis_tier",
        "universe",
        "candidate",
        "score_weights",
        "quality_freshness_sec",
        "quality_field_overrides_sec",
        "liquidity",
        "lifecycle",
        "funding",
        "evidence_cost",
        "veto",
        "refresh",
        "providers",
        # F05 A10 base keys + H01 Hedge subtrees + R00 optimization subtree.
        "identity",
        "ingestion",
        "evidence",
        "maintenance",
        "funding_capture",
        "hedge",
        "optimization",
    }


# ---------------------------------------------------------------------------
# config_hash: canonical JSON algorithm + golden value
# ---------------------------------------------------------------------------


def test_config_hash_matches_appendix_golden_value():
    assert config_hash(load_shortlab_config()) == GOLDEN_SCORING_HASH


def test_config_hash_ignores_refresh_section(tmp_path):
    base = config_hash(load_shortlab_config())
    path = _write_user_yaml(tmp_path, {"shortlab": {"refresh": {"jitter_sec": 0}}})
    assert config_hash(load_shortlab_config(path)) == base


def test_config_hash_ignores_providers_section(tmp_path):
    base = config_hash(load_shortlab_config())
    path = _write_user_yaml(
        tmp_path, {"shortlab": {"providers": {"coingecko": {"enabled": False}}}}
    )
    assert config_hash(load_shortlab_config(path)) == base


def test_config_hash_ignores_config_path_and_secret_env(tmp_path, monkeypatch):
    base = config_hash(load_shortlab_config())
    mirror = tmp_path / "mirror.yaml"
    mirror.write_bytes(DEFAULT_CONFIG_PATH.read_bytes())
    monkeypatch.setenv(SHORTLAB_CONFIG_PATH_ENV, str(mirror))
    monkeypatch.setenv("DIVE_COINGECKO_API_KEY", "super-secret-value-123")
    assert config_hash(load_shortlab_config()) == base


def test_config_hash_changes_when_ready_entry_changes(tmp_path):
    base = config_hash(load_shortlab_config())
    path = _write_user_yaml(tmp_path, {"shortlab": {"candidate": {"ready_entry": 71}}})
    assert config_hash(load_shortlab_config(path)) != base


def test_config_hash_changes_when_scoring_weight_changes(tmp_path):
    base = config_hash(load_shortlab_config())
    path = _write_user_yaml(
        tmp_path,
        {
            "shortlab": {
                "score_weights": {
                    "GENERAL_LITE": {"lifecycle": 36, "carry": 39, "valuation": 15, "tradeability": 10}
                }
            }
        },
    )
    assert config_hash(load_shortlab_config(path)) != base


def test_secret_values_never_enter_hash_log_or_api(monkeypatch):
    monkeypatch.setenv("DIVE_COINGECKO_API_KEY", "super-secret-value-123")
    config = load_shortlab_config()
    assert "super-secret-value-123" not in repr(config)
    assert "super-secret-value-123" not in config.public_dict().__repr__()
    # only the env var *name* is stored, never the value
    assert config.providers["coingecko"].api_key_env == "DIVE_COINGECKO_API_KEY"


def test_cost_config_hash_tracks_evidence_cost_only(tmp_path):
    base = cost_config_hash(load_shortlab_config())
    refresh_override = _write_user_yaml(tmp_path, {"shortlab": {"refresh": {"jitter_sec": 0}}})
    assert cost_config_hash(load_shortlab_config(refresh_override)) == base
    cost_override = _write_user_yaml(
        tmp_path, {"shortlab": {"evidence_cost": {"entry_fee": 0.001}}}
    )
    assert cost_config_hash(load_shortlab_config(cost_override)) != base


# ---------------------------------------------------------------------------
# F05: A10 base keys + frozen policy hash (legacy scoring hash untouched)
# ---------------------------------------------------------------------------


def test_f05_identity_ingestion_evidence_maintenance_defaults():
    config = load_shortlab_config()
    assert config.identity.catalog_ttl_sec == 86400
    assert config.identity.overrides_path_env == "SHORTLAB_IDENTITY_OVERRIDES_PATH"
    assert config.identity.catalog_grace_sec == 259200
    assert config.identity.catalog_include_platform is False
    assert config.identity.catalog_max_bytes == 33554432
    assert config.identity.platform_detail_batch_size == 50
    assert config.ingestion.market_concurrency == 4
    assert config.ingestion.max_source_future_skew_sec == 2
    assert config.ingestion.contract_refresh_sec == 1800
    assert config.ingestion.funding_backfill_sec == 300
    assert config.ingestion.funding_repair_overlap_intervals == 1
    assert config.ingestion.monitor_reserved_fraction == pytest.approx(0.2)
    assert config.ingestion.scanner_reserved_fraction == pytest.approx(0.3)
    assert config.ingestion.db_queue_limit == 256
    assert config.ingestion.db_persist_timeout_sec == 5
    assert config.ingestion.db_priority_aging_sec == 30
    assert config.evidence.default_history_days == 180
    assert config.evidence.grader_batch_size == 100
    assert (
        config.evidence.sample_policy
        == "first_eligible_per_symbol_profile_utc_day"
    )
    assert config.maintenance.retention_sec == 86400
    assert config.maintenance.snapshot_min_days == 180
    assert config.maintenance.batch_delete_limit == 1000
    assert config.providers["coingecko"].api_plan == "DEMO"


def test_f05_unknown_base_key_still_raises(tmp_path):
    path = _write_user_yaml(tmp_path, {"shortlab": {"identity": {"nope": 1}}})
    with pytest.raises(ShortLabConfigError):
        load_shortlab_config(path)


def test_f05_identity_grace_must_exceed_ttl(tmp_path):
    path = _write_user_yaml(
        tmp_path,
        {"shortlab": {"identity": {"catalog_ttl_sec": 259200,
                                   "catalog_grace_sec": 86400}}},
    )
    with pytest.raises(ShortLabConfigError):
        load_shortlab_config(path)


def test_f05_ingestion_reserves_capped_at_one(tmp_path):
    path = _write_user_yaml(
        tmp_path,
        {"shortlab": {"ingestion": {"monitor_reserved_fraction": 0.6,
                                    "scanner_reserved_fraction": 0.6}}},
    )
    with pytest.raises(ShortLabConfigError):
        load_shortlab_config(path)


def test_f05_evidence_sample_policy_is_fixed(tmp_path):
    path = _write_user_yaml(
        tmp_path, {"shortlab": {"evidence": {"sample_policy": "anything_goes"}}}
    )
    with pytest.raises(ShortLabConfigError):
        load_shortlab_config(path)


def test_f05_coingecko_plan_is_demo_or_pro(tmp_path):
    path = _write_user_yaml(
        tmp_path, {"shortlab": {"providers": {"coingecko": {"api_plan": "ENTERPRISE"}}}}
    )
    with pytest.raises(ShortLabConfigError):
        load_shortlab_config(path)


def test_f05_policy_hash_deterministic_and_canonical():
    from diveintocrypto_desktop.shortlab.config import (
        POLICY_VERSION,
        policy_canonical_json,
        policy_hash,
    )

    assert POLICY_VERSION == "policy-v1"
    first = policy_hash(load_shortlab_config())
    assert policy_hash(load_shortlab_config()) == first
    assert len(first) == 64
    # Canonical JSON: single line, no trailing newline, sorted keys.
    blob = policy_canonical_json(load_shortlab_config())
    assert "\n" not in blob
    import json as _json

    assert _json.loads(blob)["policy_version"] == "policy-v1"


def test_f05_policy_hash_moves_with_policy_not_cadence(tmp_path):
    from diveintocrypto_desktop.shortlab.config import policy_hash

    base = policy_hash(load_shortlab_config())
    veto_override = _write_user_yaml(
        tmp_path, {"shortlab": {"veto": {"breakout_24h": 0.40}}}
    )
    assert policy_hash(load_shortlab_config(veto_override)) != base
    identity_override = _write_user_yaml(
        tmp_path, {"shortlab": {"identity": {"catalog_ttl_sec": 3600}}}
    )
    assert policy_hash(load_shortlab_config(identity_override)) != base
    # Refresh cadence / provider capability never enter the frozen policy.
    cadence = _write_user_yaml(
        tmp_path, {"shortlab": {"refresh": {"jitter_sec": 0}}}
    )
    assert policy_hash(load_shortlab_config(cadence)) == base
    capability = _write_user_yaml(
        tmp_path, {"shortlab": {"providers": {"coingecko": {"enabled": False}}}}
    )
    assert policy_hash(load_shortlab_config(capability)) == base


def test_f05_legacy_scoring_hash_still_golden():
    assert config_hash(load_shortlab_config()) == GOLDEN_SCORING_HASH


# ---------------------------------------------------------------------------
# Engine isolation: must not modify engine/loader.py behaviour
# ---------------------------------------------------------------------------


def test_engine_loader_config_unchanged_by_shortlab_reads():
    before = engine_loader.load_config()
    load_shortlab_config()
    after = engine_loader.load_config()
    assert before == after
    assert "shortlab" not in before


def test_shortlab_package_yaml_is_not_engine_yaml():
    assert DEFAULT_CONFIG_PATH.name == "default.yaml"
    assert DEFAULT_CONFIG_PATH.parent.name == "shortlab"
    assert os.fspath(DEFAULT_CONFIG_PATH) != os.fspath(engine_loader._CONFIG_PATH)
