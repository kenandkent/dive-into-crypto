"""Task 1: Short-Lab shared DTO contracts.

Freezes `ProviderResult` / `CandidateState` exactly as the cross-task DTO
contract in `ShortLab_Implementation_Plan_CN.md` (consumed by Tasks 2/9/14),
plus identity/feature DTOs from design section 4. Covers construction,
allowed status literals, `reason_code` discipline and `error_message`
redaction (secrets must never reach logs or the public API).
"""

from __future__ import annotations

import dataclasses
from typing import get_args

import pytest

from diveintocrypto_desktop.shortlab import models
from diveintocrypto_desktop.shortlab.models import (
    AssetIdentity,
    CandidateState,
    FeatureSnapshot,
    ProviderResult,
    sanitize_error_message,
)


def _ok_result(**overrides):
    fields = {
        "status": "OK",
        "source": "coingecko",
        "fetched_at_ms": 1760000000000,
        "as_of_ms": 1760000000000,
        "data": {"market_cap_usd": 1.0},
        "stale": False,
        "reason_code": None,
        "error_message": None,
    }
    fields.update(overrides)
    return ProviderResult(**fields)


# ---------------------------------------------------------------------------
# ProviderResult: contract shape + status literals
# ---------------------------------------------------------------------------


def test_provider_result_field_names_and_order_match_contract():
    names = [f.name for f in dataclasses.fields(ProviderResult)]
    assert names == [
        "status",
        "source",
        "fetched_at_ms",
        "as_of_ms",
        "data",
        "stale",
        "reason_code",
        "error_message",
    ]


def test_provider_result_is_frozen():
    result = _ok_result()
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.stale = True  # type: ignore[misc]


def test_provider_result_accepts_all_contract_statuses():
    for status in ("OK", "PARTIAL", "NOT_APPLICABLE", "UNAVAILABLE", "ERROR"):
        result = _ok_result(
            status=status,
            reason_code=None if status == "OK" else "SOME_REASON",
            data=None,
        )
        assert result.status == status


def test_provider_result_rejects_unknown_status():
    with pytest.raises(ValueError):
        _ok_result(status="STALE")


def test_provider_result_ok_requires_null_reason_code():
    with pytest.raises(ValueError):
        _ok_result(status="OK", reason_code="SOME_REASON")


def test_provider_result_carries_reason_code_for_not_applicable():
    result = _ok_result(
        status="NOT_APPLICABLE",
        data=None,
        reason_code="no_spot_market",
    )
    assert result.reason_code == "no_spot_market"
    assert result.data is None


def test_provider_result_data_none_is_not_zero():
    result = _ok_result(status="UNAVAILABLE", data=None, reason_code="HTTP_429")
    assert result.data is None
    assert result.data != 0


def test_provider_result_generic_data_payload():
    result = ProviderResult[str](
        status="OK",
        source="universe",
        fetched_at_ms=1,
        as_of_ms=None,
        data="payload",
        stale=False,
        reason_code=None,
        error_message=None,
    )
    assert result.data == "payload"
    assert result.as_of_ms is None


# ---------------------------------------------------------------------------
# error_message redaction
# ---------------------------------------------------------------------------


def test_sanitize_error_message_redacts_api_key_assignment():
    raw = "coingecko fetch failed, api_key=SECRET123&retry"
    cleaned = sanitize_error_message(raw)
    assert cleaned is not None
    assert "SECRET123" not in cleaned
    assert "api_key=" in cleaned


def test_sanitize_error_message_redacts_bearer_token():
    raw = "HTTP 401 with Bearer SECRET123"
    assert "SECRET123" not in sanitize_error_message(raw)


def test_sanitize_error_message_redacts_query_param_secret():
    raw = "GET https://api.example.com/x?api_key=SECRET123&symbol=BTC"
    cleaned = sanitize_error_message(raw)
    assert "SECRET123" not in cleaned
    assert "symbol=BTC" in cleaned


def test_sanitize_error_message_none_stays_none():
    assert sanitize_error_message(None) is None


def test_provider_result_error_message_stored_sanitized():
    result = _ok_result(
        status="ERROR",
        data=None,
        reason_code="HTTP_500",
        error_message="boom api_key=SECRET123",
    )
    assert result.error_message is not None
    assert "SECRET123" not in result.error_message


# ---------------------------------------------------------------------------
# CandidateState: dual status + reasons/vetoes/pauses/warnings
# ---------------------------------------------------------------------------


def test_candidate_state_field_names_and_order_match_contract():
    names = [f.name for f in dataclasses.fields(CandidateState)]
    assert names == [
        "candidate_status",
        "execution_status",
        "status",
        "reasons",
        "vetoes",
        "pauses",
        "warnings",
    ]


def test_candidate_state_construction_with_four_tuple():
    state = CandidateState(
        candidate_status="CANDIDATE",
        execution_status="NOT_READY",
        status="CANDIDATE",
        reasons=("ENTRY_BELOW_READY_THRESHOLD",),
        vetoes=(),
        pauses=(),
        warnings=("WARN_FUNDING_WEAKENING",),
    )
    assert state.reasons == ("ENTRY_BELOW_READY_THRESHOLD",)
    assert state.vetoes == ()
    assert state.warnings == ("WARN_FUNDING_WEAKENING",)


def test_candidate_state_is_frozen():
    state = CandidateState(
        candidate_status="WATCH",
        execution_status="NOT_READY",
        status="WATCH",
        reasons=(),
        vetoes=(),
        pauses=(),
        warnings=(),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.status = "READY"  # type: ignore[misc]


def test_candidate_state_lists_coerced_to_tuples():
    state = CandidateState(
        candidate_status="CANDIDATE",
        execution_status="PAUSED",
        status="PAUSED",
        reasons=["PAUSE_BREAKOUT_24H"],
        vetoes=[],
        pauses=["PAUSE_BREAKOUT_24H"],
        warnings=[],
    )
    assert state.reasons == ("PAUSE_BREAKOUT_24H",)
    assert state.pauses == ("PAUSE_BREAKOUT_24H",)


def test_candidate_state_rejects_unknown_status_literals():
    with pytest.raises(ValueError):
        CandidateState(
            candidate_status="READY",  # not a candidate_status value
            execution_status="NOT_READY",
            status="CANDIDATE",
            reasons=(),
            vetoes=(),
            pauses=(),
            warnings=(),
        )
    with pytest.raises(ValueError):
        CandidateState(
            candidate_status="CANDIDATE",
            execution_status="PENDING",  # not an execution_status value
            status="CANDIDATE",
            reasons=(),
            vetoes=(),
            pauses=(),
            warnings=(),
        )


def test_candidate_state_rejects_non_string_reasons():
    with pytest.raises(ValueError):
        CandidateState(
            candidate_status="CANDIDATE",
            execution_status="NOT_READY",
            status="CANDIDATE",
            reasons=(123,),  # type: ignore[list-item]
            vetoes=(),
            pauses=(),
            warnings=(),
        )


# ---------------------------------------------------------------------------
# AssetIdentity (design section 4.1)
# ---------------------------------------------------------------------------


def test_asset_identity_required_fields():
    identity = AssetIdentity(
        canonical_id="pepe",
        display_symbol="PEPE",
        binance_futures_symbol="1000PEPEUSDT",
        mapping_confidence="HIGH",
        mapping_source="UNIQUE_SYMBOL",
    )
    assert identity.canonical_id == "pepe"
    assert identity.binance_spot_symbol is None
    assert identity.contract_multiplier is None
    assert identity.categories == ()


def test_asset_identity_full_mapping():
    identity = AssetIdentity(
        canonical_id="pepe",
        display_symbol="PEPE",
        name="Pepe",
        binance_futures_symbol="1000PEPEUSDT",
        binance_spot_symbol="PEPEUSDT",
        contract_multiplier=1000.0,
        multiplier_source="EXCHANGE",
        coingecko_id="pepe",
        categories=["MEME"],
        mapping_confidence="VERIFIED",
        mapping_source="MANUAL",
    )
    assert identity.binance_spot_symbol == "PEPEUSDT"
    assert identity.contract_multiplier == 1000.0
    assert identity.categories == ("MEME",)


def test_asset_identity_is_frozen():
    identity = AssetIdentity(
        canonical_id="pepe",
        display_symbol="PEPE",
        binance_futures_symbol="1000PEPEUSDT",
        mapping_confidence="HIGH",
        mapping_source="UNIQUE_SYMBOL",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        identity.coingecko_id = "pepe"  # type: ignore[misc]


def test_asset_identity_rejects_unknown_confidence():
    with pytest.raises(ValueError):
        AssetIdentity(
            canonical_id="pepe",
            display_symbol="PEPE",
            binance_futures_symbol="1000PEPEUSDT",
            mapping_confidence="SORT_OF",  # type: ignore[arg-type]
            mapping_source="OTHER",
        )


def test_asset_identity_multiplier_without_source_rejected():
    with pytest.raises(ValueError):
        AssetIdentity(
            canonical_id="pepe",
            display_symbol="PEPE",
            binance_futures_symbol="1000PEPEUSDT",
            contract_multiplier=1000.0,
            multiplier_source=None,
            mapping_confidence="HIGH",
            mapping_source="UNIQUE_SYMBOL",
        )


# ---------------------------------------------------------------------------
# FeatureSnapshot (point-in-time feature carrier)
# ---------------------------------------------------------------------------


def test_feature_snapshot_construction():
    snapshot = FeatureSnapshot(
        snapshot_id="feat-btc-001",
        symbol="BTCUSDT",
        as_of_ms=1760000000000,
        feature_version="ltss-lite-v1",
        features={"ath_drawdown": -0.58},
        source_meta={"ath_drawdown": {"status": "OK"}},
        data_quality=94.0,
    )
    assert snapshot.features["ath_drawdown"] == -0.58
    assert snapshot.data_quality == 94.0


def test_feature_snapshot_is_frozen():
    snapshot = FeatureSnapshot(
        snapshot_id="feat-btc-001",
        symbol="BTCUSDT",
        as_of_ms=1760000000000,
        feature_version="ltss-lite-v1",
        features={},
        source_meta={},
        data_quality=None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.data_quality = 1.0  # type: ignore[misc]


def test_models_exposes_status_literal_sets():
    assert set(get_args(models.ProviderStatus)) == {
        "OK",
        "PARTIAL",
        "NOT_APPLICABLE",
        "UNAVAILABLE",
        "ERROR",
    }
    assert set(get_args(models.CandidateStatusT)) == {"EXCLUDED", "WATCH", "CANDIDATE"}
    assert set(get_args(models.ExecutionStatusT)) == {"NOT_READY", "READY", "PAUSED", "BLOCKED"}
