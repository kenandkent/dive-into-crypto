"""R00 config: D15 subtrees, fcs dual-accept, decision_policy_hash goldens."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml


def _write_user_yaml(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "repair.override.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return path


def test_optimization_defaults_match_d15():
    from diveintocrypto_desktop.shortlab.config import load_shortlab_config

    config = load_shortlab_config()
    assert config.funding_capture.fcs_version == 'fcs_v2'
    assert config.funding_capture.reference_hold_days == 30
    assert config.optimization.schema_version == 'repair-contract-v1'
    decision = dict(config.optimization.decision)
    assert decision['version'] == 'hedge-decision-v1'
    assert decision['validation_level'] == 'RULE_BASED_UNVALIDATED'
    assert list(decision['ratios']) == ['0', '0.25', '0.5', '0.75', '1']
    assert decision['ratio_tolerance'] == '0.02'
    assert decision['min_net_carry_usd'] == '0'
    assert decision['quote_valid_sec'] == 20
    assert decision['exit_stress_bps'] == 100
    assert decision['capital_reserve_fraction'] == '0.05'
    assert set(dict(decision['scenarios'])) == {
        'UP_50', 'UP_100', 'DOWN_50', 'BASIS_UP', 'BASIS_DOWN', 'FX_DOWN',
    }
    assert dict(config.optimization.protection)['confirmation_ttl_sec'] == 86400
    evidence = dict(config.optimization.evidence)
    assert evidence['version'] == 'hedge_evidence_v3'
    assert list(evidence['horizons_days']) == [7, 30, 90]
    assert evidence['bootstrap_seed'] == 20261008
    providers = dict(dict(config.optimization.providers)['coingecko'])
    assert providers['account_monthly_limit'] == 10000
    assert providers['local_requests_per_minute'] == 30
    assert providers['reserve_fraction'] == '0.1'


def test_old_user_config_gets_defaults_and_fcs_dual_accept(tmp_path):
    from diveintocrypto_desktop.shortlab.config import load_shortlab_config

    # Old file without new keys still loads via deep-merge defaults.
    path = _write_user_yaml(tmp_path, {"shortlab": {"universe": {"limit": 100}}})
    config = load_shortlab_config(path)
    assert config.optimization.schema_version == 'repair-contract-v1'
    assert config.funding_capture.reference_hold_days == 30
    # fcs_v1 accepted and normalized to current fcs_v2.
    v1_path = _write_user_yaml(
        tmp_path, {"shortlab": {"funding_capture": {"fcs_version": "fcs_v1"}}}
    )
    assert load_shortlab_config(v1_path).funding_capture.fcs_version == 'fcs_v2'
    v2_path = _write_user_yaml(
        tmp_path, {"shortlab": {"funding_capture": {"fcs_version": "fcs_v2"}}}
    )
    assert load_shortlab_config(v2_path).funding_capture.fcs_version == 'fcs_v2'
    with pytest.raises(Exception):
        bad = _write_user_yaml(
            tmp_path, {"shortlab": {"funding_capture": {"fcs_version": "fcs_v9"}}}
        )
        load_shortlab_config(bad)


def test_optimization_rejections_ratio_hold_capital(tmp_path):
    from diveintocrypto_desktop.shortlab.config import ShortLabConfigError, load_shortlab_config

    # Ratio out of range.
    with pytest.raises(ShortLabConfigError):
        bad = _write_user_yaml(
            tmp_path, {"shortlab": {"optimization": {"decision": {"ratios": ["0", "2"]}}}}
        )
        load_shortlab_config(bad)
    # Missing full-hedge ratio.
    with pytest.raises(ShortLabConfigError):
        bad = _write_user_yaml(
            tmp_path, {"shortlab": {"optimization": {"decision": {"ratios": ["0", "0.5"]}}}}
        )
        load_shortlab_config(bad)
    # Negative carry floor.
    with pytest.raises(ShortLabConfigError):
        bad = _write_user_yaml(
            tmp_path,
            {"shortlab": {"optimization": {"decision": {"min_net_carry_usd": "-1"}}}},
        )
        load_shortlab_config(bad)
    # Unknown key.
    with pytest.raises(ShortLabConfigError):
        bad = _write_user_yaml(
            tmp_path, {"shortlab": {"optimization": {"nope": 1}}}
        )
        load_shortlab_config(bad)


def test_decision_policy_hash_golden_canonical_independent_of_layout(tmp_path):
    from diveintocrypto_desktop.shortlab.config import (
        decision_policy_canonical_json,
        decision_policy_hash,
        load_shortlab_config,
    )

    config = load_shortlab_config()
    first = decision_policy_hash(config)
    assert len(first) == 64
    assert decision_policy_hash(load_shortlab_config()) == first
    # Canonical: single line, no trailing newline, sorted keys.
    blob = decision_policy_canonical_json(config)
    assert "\n" not in blob
    assert not blob.endswith("\n")
    reparsed = json.loads(blob)
    recanon = json.dumps(reparsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    assert hashlib.sha256(recanon.encode("utf-8")).hexdigest() == first
    # CRLF/pretty re-serialization does not change the hash.
    pretty = json.dumps(reparsed, indent=2, sort_keys=True, ensure_ascii=False).replace("\n", "\r\n")
    recanon2 = json.dumps(json.loads(pretty), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    assert hashlib.sha256(recanon2.encode("utf-8")).hexdigest() == first


def test_decision_policy_hash_moves_with_policy_not_paths(tmp_path):
    from diveintocrypto_desktop.shortlab.config import decision_policy_hash, load_shortlab_config

    base = decision_policy_hash(load_shortlab_config())
    # Decision threshold moves the hash.
    changed = _write_user_yaml(
        tmp_path, {"shortlab": {"optimization": {"decision": {"quote_valid_sec": 30}}}}
    )
    assert decision_policy_hash(load_shortlab_config(changed)) != base
    # Candidate direction threshold moves it.
    changed2 = _write_user_yaml(
        tmp_path, {"shortlab": {"candidate": {"ready_entry": 71}}}
    )
    assert decision_policy_hash(load_shortlab_config(changed2)) != base
    # Entry gate moves it.
    changed3 = _write_user_yaml(
        tmp_path,
        {"shortlab": {"funding_capture": {"entry_gate": {"min_30d_coverage": 0.5}}}},
    )
    assert decision_policy_hash(load_shortlab_config(changed3)) != base
    # Providers/refresh/paths/secrets never do.
    for payload in (
        {"shortlab": {"providers": {"coingecko": {"enabled": False}}}},
        {"shortlab": {"refresh": {"jitter_sec": 0}}},
        {"shortlab": {"hedge": {"refresh": {"opportunity_sec": 60}}}},
        {"shortlab": {"hedge": {"providers": {"binance_alpha": {"enabled": True}}}}},
    ):
        path = _write_user_yaml(tmp_path, payload)
        assert decision_policy_hash(load_shortlab_config(path)) == base


def test_decision_policy_hash_accepts_mapping_and_uses_current_versions():
    from diveintocrypto_desktop.shortlab.config import (
        decision_policy_canonical_json,
        decision_policy_hash,
        load_shortlab_config,
    )

    config = load_shortlab_config()
    assert decision_policy_hash(config.to_dict()) == decision_policy_hash(config)
    blob = decision_policy_canonical_json(config)
    obj = json.loads(blob)
    assert obj["repair_schema_version"] == "repair-contract-v1"
    assert obj["versions"]["fcs"] == "fcs_v2"
    assert obj["versions"]["hedge"] == "hedge_v2"
    assert obj["versions"]["evidence"] == "hedge_evidence_v3"
    assert obj["versions"]["features"] == "features-v3"
    assert obj["versions"]["entry"] == "entry-v3"
    # Providers/refresh never enter the projection.
    assert "providers" not in obj or "coingecko" not in str(obj.get("providers", ""))
    assert "refresh" not in json.dumps(obj) or "opportunity_sec" not in json.dumps(obj)


def test_yaml_duplicate_keys_rejected(tmp_path):
    from diveintocrypto_desktop.shortlab.config import ShortLabConfigError, load_shortlab_config

    dup = tmp_path / "dup.yaml"
    dup.write_text("shortlab:\n  universe:\n    limit: 100\n    limit: 200\n", encoding="utf-8")
    with pytest.raises(ShortLabConfigError):
        load_shortlab_config(dup)


def test_scoring_versions_added_not_modified():
    from diveintocrypto_desktop.shortlab.scoring import versions as v

    assert v.FEATURE_VERSION == "features-v2"
    assert v.SCORE_VERSION_LITE == "ltss-lite-v1"
    assert v.SCORE_VERSION_FULL == "ltss-full-v1"
    assert v.ENTRY_VERSION == "entry-v2"
    assert v.FEATURE_VERSION_V3 == "features-v3"
    assert v.ENTRY_VERSION_V3 == "entry-v3"


def test_hedge_versions_bump_with_legacy_decode():
    from diveintocrypto_desktop.shortlab.hedge import (
        COST_FORMULA_VERSION,
        COST_FORMULA_VERSION_V2,
        FCS_VERSION,
        FCS_VERSION_V2,
        HEDGE_EVIDENCE_VERSION,
        HEDGE_EVIDENCE_VERSION_V3,
        HEDGE_FORMULA_VERSION,
        HEDGE_FORMULA_VERSION_V2,
    )
    from diveintocrypto_desktop.shortlab.hedge.models import (
        FCSResult,
        HedgeOutcome,
        HistoricalPriceBar,
    )

    assert FCS_VERSION == "fcs_v1"
    assert FCS_VERSION_V2 == "fcs_v2"
    assert HEDGE_FORMULA_VERSION == "hedge_v1"
    assert HEDGE_FORMULA_VERSION_V2 == "hedge_v2"
    assert HEDGE_EVIDENCE_VERSION == "hedge_evidence_v2"
    assert HEDGE_EVIDENCE_VERSION_V3 == "hedge_evidence_v3"
    assert COST_FORMULA_VERSION == "NATIVE_NOTIONAL_VWAP_INCLUDED_ONCE_V1"
    assert COST_FORMULA_VERSION_V2 == "NATIVE_NOTIONAL_VWAP_INCLUDED_ONCE_V2"
    # Old decode still works; new versions also accepted.
    for ver in ("fcs_v1", "fcs_v2"):
        FCSResult(
            snapshot_id="s", symbol="BTCUSDT", canonical_id="bitcoin", as_of_ms=1,
            fcs_version=ver, fcs_config_hash="x" * 64,
            reference_notional_usd="10000",
        )
    for ver in ("hedge_evidence_v2", "hedge_evidence_v3"):
        HedgeOutcome(
            outcome_id="o", fcs_snapshot_id="s", strategy="ABSOLUTE_100",
            horizon_days=30, outcome_status="PENDING", reason_code=None,
            evidence_version=ver, cost_config_hash="y" * 64,
        )
    # price_basis defaults to TRADE for legacy bars.
    bar = HistoricalPriceBar(
        symbol="BTCUSDT", open_ms=1, close_ms=2, native_open="1",
        native_high="2", native_low="0.5", native_close="1.5",
        quote_asset="USDT",
    )
    assert bar.price_basis == "TRADE"
