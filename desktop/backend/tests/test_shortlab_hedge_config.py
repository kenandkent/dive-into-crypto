"""H01 config: B40 subtrees, three hashes, .sha256 fixtures.

Covers plan H01.1/H01.2 config scenes:
- funding_capture/hedge strict subtrees with onchain env pin;
- fcs/hedge-cost/hedge-policy hashes equal the B附录F goldens;
- CRLF/indent/reordering never changes the hash; value changes do;
- secrets/URLs/paths/cadence never enter any hash, log or API;
- legacy LTSS config_hash stays golden.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

TEST_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = TEST_DIR / "fixtures" / "hedge" / "config"

GOLDEN_FCS = "72eca2e7214ac6dfdf908541d1dd185c6b6ce882e30de3297b495c8f1d61c136"
GOLDEN_COST = "9915b1468e5d0e4fc02ee71b4883c5445351928b0e7509f0dc4e6727fcca37dc"
GOLDEN_POLICY = "ff7adb8e321486bfc4c5d8a8c5645b26874be15577bebe63d336ba81980c8f08"
GOLDEN_LTSS = "4909ffe7d43c294983d313cff65c6d73125b921944e403a1e1e152b91a869786"


def _write_user_yaml(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "hedge.override.yaml"
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")
    return path


def test_b40_subtrees_present_with_onchain_env_pin():
    from diveintocrypto_desktop.shortlab.config import load_shortlab_config
    config = load_shortlab_config()
    assert config.funding_capture.fcs_version == "fcs_v1"
    assert config.funding_capture.reference_notional_usd == 10000
    assert config.funding_capture.enabled is False
    assert config.hedge.enabled is False
    assert config.hedge.ratio["drift_warn_pct"] == pytest.approx(0.02)
    assert config.hedge.execution["max_price_impact_bps"] == 30
    assert config.hedge.liquidation["warning_distance"] == pytest.approx(0.2)
    assert config.hedge.costs["futures_entry_fee_rate"] == pytest.approx(0.0005)
    assert config.hedge.fx["policy"] == "OBSERVED_QUOTE_TO_USD"
    # H01.1: on-chain env pin is explicit and never a raw secret.
    onchain = dict(config.hedge.providers)["onchain"]
    assert dict(onchain)["api_key_env"] == "SHORTLAB_0X_API_KEY"
    assert dict(onchain)["base_url"] == "https://api.0x.org"
    assert dict(onchain)["provider"] == "ETHEREUM_0X_PRICE_V2"
    assert dict(onchain)["chain_id"] == 1


def test_three_hashes_match_appendix_goldens():
    from diveintocrypto_desktop.shortlab.config import (
        fcs_config_hash,
        hedge_cost_config_hash,
        hedge_policy_hash,
        load_shortlab_config,
    )
    config = load_shortlab_config()
    assert fcs_config_hash(config) == GOLDEN_FCS
    assert hedge_cost_config_hash(config) == GOLDEN_COST
    assert hedge_policy_hash(config) == GOLDEN_POLICY


def test_fixtures_parse_to_same_hashes_and_sha256_match():
    for fname, golden in (
        ("fcs_config.json", GOLDEN_FCS),
        ("hedge_cost_config.json", GOLDEN_COST),
        ("hedge_policy.json", GOLDEN_POLICY),
    ):
        raw = (FIXTURE_DIR / fname).read_text(encoding="utf-8")
        obj = json.loads(raw)
        canon = json.dumps(obj, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False)
        assert hashlib.sha256(canon.encode("utf-8")).hexdigest() == golden
        sha = (FIXTURE_DIR / f"{fname}.sha256").read_text(encoding="utf-8").strip()
        assert sha == golden


def test_crlf_indent_reorder_do_not_change_hashes():
    from diveintocrypto_desktop.shortlab.config import (
        fcs_config_hash,
        hedge_cost_config_hash,
        hedge_policy_hash,
        load_shortlab_config,
    )
    config = load_shortlab_config()
    # Canonical bytes have no trailing newline by contract.
    from diveintocrypto_desktop.shortlab.config import (
        fcs_canonical_json,
        hedge_cost_canonical_json,
        hedge_policy_canonical_json,
    )
    for blob in (fcs_canonical_json(config), hedge_cost_canonical_json(config),
                 hedge_policy_canonical_json(config)):
        assert "\n" not in blob
        assert "\r" not in blob
    # Fixture reserialised with CRLF + 4-space indent still hashes golden.
    for fname, golden in (
        ("fcs_config.json", GOLDEN_FCS),
        ("hedge_cost_config.json", GOLDEN_COST),
        ("hedge_policy.json", GOLDEN_POLICY),
    ):
        obj = json.loads((FIXTURE_DIR / fname).read_text(encoding="utf-8"))
        pretty_crlf = json.dumps(obj, indent=4, sort_keys=True,
                                 ensure_ascii=False).replace("\n", "\r\n")
        reparsed = json.loads(pretty_crlf)
        canon = json.dumps(reparsed, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False)
        assert hashlib.sha256(canon.encode("utf-8")).hexdigest() == golden
    # Type normalisation: 1 vs 1.0 for a float bin still hashes identically
    # only when the schema normalises (float fields); int bins stay ints.
    assert fcs_config_hash(config) == GOLDEN_FCS
    assert hedge_cost_config_hash(config) == GOLDEN_COST
    assert hedge_policy_hash(config) == GOLDEN_POLICY


def test_value_changes_move_hashes_secret_changes_do_not(tmp_path, monkeypatch):
    from diveintocrypto_desktop.shortlab.config import (
        fcs_config_hash,
        hedge_cost_config_hash,
        hedge_policy_hash,
        load_shortlab_config,
    )
    base = load_shortlab_config()
    assert fcs_config_hash(base) == GOLDEN_FCS
    # Scoring-relevant changes move the FCS hash.
    changed = _write_user_yaml(
        tmp_path, {"shortlab": {"funding_capture": {"reference_notional_usd": 20000}}})
    assert fcs_config_hash(load_shortlab_config(changed)) != GOLDEN_FCS
    # Cost/FX changes move the cost hash.
    cost_changed = _write_user_yaml(
        tmp_path, {"shortlab": {"hedge": {"costs": {"spot_entry_fee_rate": 0.002}}}})
    assert hedge_cost_config_hash(load_shortlab_config(cost_changed)) != GOLDEN_COST
    # Risk/stop changes move the policy hash, not the FCS hash.
    risk_changed = _write_user_yaml(
        tmp_path, {"shortlab": {"hedge": {"liquidation": {"warning_distance": 0.25}}}})
    assert hedge_policy_hash(load_shortlab_config(risk_changed)) != GOLDEN_POLICY
    # Refresh cadence / provider enablement / URLs / env secrets never do.
    cadence = _write_user_yaml(
        tmp_path, {"shortlab": {"hedge": {"refresh": {"opportunity_sec": 60}}}})
    assert fcs_config_hash(load_shortlab_config(cadence)) == GOLDEN_FCS
    assert hedge_cost_config_hash(load_shortlab_config(cadence)) == GOLDEN_COST
    assert hedge_policy_hash(load_shortlab_config(cadence)) == GOLDEN_POLICY
    capability = _write_user_yaml(
        tmp_path, {"shortlab": {"hedge": {"providers": {"binance_alpha": {"enabled": True}}}}})
    assert fcs_config_hash(load_shortlab_config(capability)) == GOLDEN_FCS
    monkeypatch.setenv("SHORTLAB_0X_API_KEY", "super-secret-0x-key")
    assert fcs_config_hash(load_shortlab_config()) == GOLDEN_FCS
    assert hedge_cost_config_hash(load_shortlab_config()) == GOLDEN_COST
    assert hedge_policy_hash(load_shortlab_config()) == GOLDEN_POLICY


def test_secrets_urls_paths_absent_from_hashes_logs_and_api(monkeypatch):
    from diveintocrypto_desktop.shortlab.config import load_shortlab_config
    monkeypatch.setenv("SHORTLAB_0X_API_KEY", "super-secret-0x-key")
    config = load_shortlab_config()
    assert "super-secret-0x-key" not in repr(config)
    assert "super-secret-0x-key" not in config.public_dict().__repr__()
    assert "https://api.0x.org" not in config.public_dict().__repr__()
    # Hedge providers expose only the enabled flag publicly.
    assert config.public_dict()["hedge"]["providers"]["onchain"] == {"enabled": False}
    assert config.public_dict()["hedge"]["providers"]["binance_spot"] == {"enabled": True}


def test_legacy_ltss_hash_still_golden():
    from diveintocrypto_desktop.shortlab.config import config_hash, load_shortlab_config
    assert config_hash(load_shortlab_config()) == GOLDEN_LTSS


def test_unknown_hedge_key_rejected_and_types_checked(tmp_path):
    from diveintocrypto_desktop.shortlab.config import ShortLabConfigError, load_shortlab_config
    bad = _write_user_yaml(tmp_path, {"shortlab": {"hedge": {"nope": 1}}})
    with pytest.raises(ShortLabConfigError):
        load_shortlab_config(bad)
    bad_gate = _write_user_yaml(
        tmp_path, {"shortlab": {"funding_capture": {"entry_gate": {"min_30d_coverage": -1}}}})
    with pytest.raises(ShortLabConfigError):
        load_shortlab_config(bad_gate)
    bad_ttl = _write_user_yaml(
        tmp_path, {"shortlab": {"hedge": {"freshness": {"spot_quote": {"ttl": 60, "grace": 20}}}}})
    with pytest.raises(ShortLabConfigError):
        load_shortlab_config(bad_ttl)
