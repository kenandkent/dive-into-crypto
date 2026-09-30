"""LTSS-LITE feature extraction and scoring (Task 10).

Design contract: sections 8.2 / 8.3 / 9.1 / 10.3 / 11.1 / 13 / 13.1 / 14.

- :func:`extract_features` is pure (no HTTP / SQL): it turns already-closed
  inputs into a :class:`FeatureSnapshot` holding raw values, per-factor
  scores, null reasons and the point-in-time windows.
- :func:`select_profile` lives in :mod:`profiles` (re-exported here).
- :func:`score_lite` maps ``raw / raw_max * profile_weight`` per module and
  rounds only the final LTSS to 1 decimal (ROUND_HALF_UP). Missing factors
  contribute 0; a missing critical input (funding30d, ATH, OI or MC) forces
  ``ltss=None`` with no reweighting.
- :func:`score_full` pre-buries the FULL synthesis for Task 17
  (``valuation_supply_raw = valuation_raw_10 + unlock_raw_15`` over 25 and
  ``narrative_raw_15`` over 15) so the three FULL profiles hit exactly 100
  on all-max fixtures.

``lifecycle.min_age_days`` does not exist: listing-age risk uses only
``veto.new_token_days`` (Task 11).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping

from diveintocrypto_desktop.shortlab.config import ShortLabConfig, config_hash
from diveintocrypto_desktop.shortlab.features import carry as carry_mod
from diveintocrypto_desktop.shortlab.features import lifecycle as life_mod
from diveintocrypto_desktop.shortlab.features import tradeability as trade_mod
from diveintocrypto_desktop.shortlab.features import valuation as val_mod
from diveintocrypto_desktop.shortlab.models import FeatureSnapshot
from diveintocrypto_desktop.shortlab.scoring.profiles import Profile, select_profile
from diveintocrypto_desktop.shortlab.scoring.versions import (
    FEATURE_VERSION,
    SCORE_VERSION_FULL,
    SCORE_VERSION_LITE,
)

__all__ = [
    "FEATURE_VERSION",
    "SCORE_VERSION_FULL",
    "SCORE_VERSION_LITE",
    "ScoreBreakdown",
    "extract_features",
    "score_full",
    "score_lite",
    "select_profile",
    "round_half_up_1",
]


def round_half_up_1(value: float) -> float:
    """Round to 1 decimal with ROUND_HALF_UP (design: 四舍五入)."""
    return float(Decimal(str(value)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class ScoreBreakdown:
    """Deterministic LTSS result (never API JSON)."""

    ltss: float | None
    profile: str
    score_version: str
    feature_version: str
    config_hash: str
    module_scores: Mapping[str, float]
    module_raws: Mapping[str, int]
    module_max: Mapping[str, int]
    factor_scores: Mapping[str, int | None]
    raw_values: Mapping[str, Any]
    reasons: tuple[str, ...]
    null_reason: str | None = None
    symbol: str = ""
    as_of_ms: int = 0

    @property
    def tradeability_score(self) -> float:
        return float(self.module_scores.get("tradeability", 0.0))


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    import math as _math

    if not _math.isfinite(result):
        return None
    return result


def _inputs_get(inputs: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if isinstance(inputs, Mapping) and name in inputs:
            return inputs[name]
    return None


def _spot_applicable(inputs: Mapping[str, Any]) -> bool:
    if isinstance(inputs, Mapping):
        if inputs.get("spot_applicable") is False:
            return False
        status = inputs.get("spot_status")
        if isinstance(status, str) and status == "NOT_APPLICABLE":
            return False
    return True


def extract_features(inputs: Mapping[str, Any], as_of_ms: int) -> FeatureSnapshot:
    """Build the point-in-time :class:`FeatureSnapshot` (pure).

    Only ``as_of_ms``-closed values in ``inputs`` are read; the rolling
    ``universe.quote_volume`` ticker is deliberately ignored (it may be
    present in ``inputs`` for the discriminating test but never enters the
    score, gate, DQ or snapshot field).
    """
    data: dict[str, Any] = dict(inputs) if isinstance(inputs, Mapping) else {}
    as_of = int(as_of_ms)
    symbol = str(data.get("symbol") or data.get("futures_symbol") or "UNKNOWN")

    # ---- Lifecycle -----------------------------------------------------
    ath_drawdown = _inputs_get(data, "ath_drawdown")
    if ath_drawdown is None:
        ath_price = _finite(data.get("ath_price"))
        current_price = _finite(
            data.get("current_price", data.get("last_close_price"))
        )
        if ath_price is not None and current_price is not None and ath_price > 0:
            ath_drawdown = (current_price - ath_price) / ath_price
        else:
            ath_drawdown = None
    ath_age_days: float | None = None
    ath_date_ms = _finite(data.get("ath_date_ms"))
    if ath_date_ms is not None:
        ath_age_days = (as_of - float(ath_date_ms)) / life_mod.DAY_MS

    ret_30d = _inputs_get(data, "return_30d", "ret_30d", "price_change_30d")
    if ret_30d is None:
        cur = _finite(data.get("current_price", data.get("last_close_price")))
        prev = _finite(data.get("close_30d_ago", data.get("price_30d_ago")))
        if cur is not None and prev is not None and prev > 0:
            ret_30d = (cur - prev) / prev

    closes = data.get("daily_closes")
    highs = data.get("daily_highs")
    spot_applicable = _spot_applicable(data)

    lc_ath = life_mod.score_ath_drawdown(ath_drawdown)
    lc_age = life_mod.score_ath_age(ath_age_days)
    lc_trend = life_mod.score_30d_drop(ret_30d)
    lc_ma = life_mod.score_ma_structure(closes)
    lc_lh = life_mod.score_lower_high(highs)
    lc_decay = life_mod.score_spot_decay(
        data.get("spot_volume_30d"),
        data.get("spot_volume_prev_30d"),
        applicable=spot_applicable,
    )
    lc_bounce = life_mod.score_failed_bounce(closes, highs)

    # ---- Carry ---------------------------------------------------------
    funding_30d_complete = data.get("funding_30d_complete", True)
    funding_30d_ratio_complete = data.get("funding_30d_ratio_complete", True)
    funding_90d_complete = data.get("funding_90d_complete", True)
    if funding_30d_complete is None:
        funding_30d_complete = True
    c_funding = carry_mod.score_funding_30d(
        data.get("funding_30d"), complete=bool(funding_30d_complete)
    )
    c_pos30 = carry_mod.score_positive_ratio(
        data.get("funding_positive_ratio_30d"),
        complete=bool(funding_30d_complete and funding_30d_ratio_complete),
        field="POSITIVE_RATIO_30D",
    )
    c_pos90 = carry_mod.score_positive_ratio(
        data.get("funding_positive_ratio_90d"),
        complete=bool(funding_90d_complete),
        field="POSITIVE_RATIO_90D",
    )
    if not bool(funding_30d_complete):
        c_stab: tuple[int | None, float | None, str | None] = (
            None,
            None,
            "FUNDING_HISTORY_INCOMPLETE",
        )
    else:
        c_stab = carry_mod.score_funding_stability(data.get("funding_rates_30d"))
    oi_usd = _finite(data.get("oi_value_usd"))
    market_cap = _finite(data.get("market_cap_usd"))
    c_oimc = carry_mod.score_oi_mc(oi_usd, market_cap)
    c_div = carry_mod.score_price_oi_divergence(
        data.get("price_change_7d"), data.get("oi_change_7d")
    )
    c_ratio = carry_mod.score_futures_spot_ratio(
        data.get("futures_spot_volume_ratio"), applicable=spot_applicable
    )
    c_crowd = carry_mod.score_crowding(
        _inputs_get(data, "ls_ratio", "long_short_ratio", "crowding_ls_ratio")
    )

    # ---- Valuation -----------------------------------------------------
    v_fdv = val_mod.score_fdv_mc(data.get("fdv_usd"), data.get("market_cap_usd"))
    v_float = val_mod.score_float_ratio(
        data.get("circulating_supply"), data.get("total_supply")
    )
    v_combo = val_mod.score_combo(v_fdv[1], v_float[1])

    # ---- Tradeability (daily qv ONLY; rolling ticker never read) -------
    t_qv = trade_mod.score_futures_qv(data.get("futures_qv_1d"))
    t_oi = trade_mod.score_oi(oi_usd)
    spread = _finite(data.get("spread"))
    if spread is None:
        bid = _finite(data.get("best_bid"))
        ask = _finite(data.get("best_ask"))
        if bid is not None and ask is not None and bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
            if mid > 0:
                spread = (ask - bid) / mid
    t_spread = trade_mod.score_spread(spread)
    depth_min = _finite(data.get("book_depth_min_1pct"))
    if depth_min is None:
        bid_n = _finite(data.get("book_bid_notional_1pct"))
        ask_n = _finite(data.get("book_ask_notional_1pct"))
        if bid_n is not None and ask_n is not None:
            depth_min = min(bid_n, ask_n)
    t_depth = trade_mod.score_depth(depth_min)
    t_contract = trade_mod.score_contract(
        data.get("contract_status"), data.get("has_settlement_record")
    )

    # ---- FULL pass-through (Task 17 owns supply/narrative providers) ---
    unlock_raw = _finite(data.get("unlock_raw_15"))
    if unlock_raw is not None and not 0 <= unlock_raw <= 15:
        unlock_raw = None
    narrative_raw = _finite(data.get("narrative_raw_15"))
    if narrative_raw is not None and not 0 <= narrative_raw <= 15:
        narrative_raw = None

    def _pack(
        score: int | None, value: Any, reason: str | None
    ) -> dict[str, Any]:
        return {"score": score, "value": value, "reason": reason}

    lifecycle_factors = {
        "ath_drawdown": _pack(*lc_ath),
        "ath_age": _pack(*lc_age),
        "drop_30d": _pack(*lc_trend),
        "ma_structure": _pack(*lc_ma),
        "lower_high": _pack(*lc_lh),
        "spot_decay": _pack(*lc_decay),
        "failed_bounce": _pack(*lc_bounce),
    }
    carry_factors = {
        "funding_30d": _pack(*c_funding),
        "positive_ratio_90d": _pack(*c_pos90),
        "positive_ratio_30d": _pack(*c_pos30),
        "funding_stability": _pack(*c_stab),
        "oi_mc": _pack(*c_oimc),
        "price_oi_divergence": _pack(*c_div),
        "futures_spot_ratio": _pack(*c_ratio),
        "crowding": _pack(*c_crowd),
    }
    valuation_factors = {
        "fdv_mc": _pack(*v_fdv),
        "float_ratio": _pack(*v_float),
        "combo": _pack(*v_combo),
    }
    tradeability_factors = {
        "futures_qv_1d": _pack(*t_qv),
        "oi": _pack(*t_oi),
        "spread": _pack(*t_spread),
        "depth_min": _pack(*t_depth),
        "contract": _pack(*t_contract),
    }

    lifecycle_raw = sum(
        item["score"] for item in lifecycle_factors.values() if item["score"] is not None
    )
    carry_raw = sum(
        item["score"] for item in carry_factors.values() if item["score"] is not None
    )
    valuation_raw = sum(
        item["score"] for item in valuation_factors.values() if item["score"] is not None
    )
    tradeability_raw = sum(
        item["score"]
        for item in tradeability_factors.values()
        if item["score"] is not None
    )

    critical_missing: list[str] = []
    if lifecycle_factors["ath_drawdown"]["score"] is None:
        critical_missing.append("ATH")
    if carry_factors["funding_30d"]["score"] is None:
        critical_missing.append("funding30d")
    if oi_usd is None:
        critical_missing.append("OI")
    if market_cap is None:
        critical_missing.append("MC")

    null_reasons = sorted(
        {
            str(item["reason"])
            for group in (
                lifecycle_factors,
                carry_factors,
                valuation_factors,
                tradeability_factors,
            )
            for item in group.values()
            if item["reason"] is not None
        }
    )

    features: dict[str, Any] = {
        "lifecycle": {
            "factors": lifecycle_factors,
            "raw": lifecycle_raw,
            "raw_max": life_mod.RAW_MAX,
        },
        "carry": {
            "factors": carry_factors,
            "raw": carry_raw,
            "raw_max": carry_mod.RAW_MAX,
        },
        "valuation": {
            "factors": valuation_factors,
            "raw": valuation_raw,
            "raw_max": val_mod.RAW_MAX,
        },
        "tradeability": {
            "factors": tradeability_factors,
            "raw": tradeability_raw,
            "raw_max": trade_mod.RAW_MAX,
        },
        "unlock": {"raw_15": unlock_raw, "raw_max": 15},
        "narrative": {"raw_15": narrative_raw, "raw_max": 15},
        "_inputs": {
            "oi_value_usd": oi_usd,
            "market_cap_usd": market_cap,
            "funding_30d": _finite(data.get("funding_30d")),
            "ath_drawdown": lifecycle_factors["ath_drawdown"]["value"],
        },
    }

    source_meta: dict[str, Any] = {
        "as_of_ms": as_of,
        "feature_version": FEATURE_VERSION,
        "critical_missing": sorted(critical_missing),
        "null_reasons": null_reasons,
        "spot_applicable": spot_applicable,
        "funding_30d_complete": bool(funding_30d_complete),
        "funding_90d_complete": bool(funding_90d_complete),
    }

    snapshot_id = f"{symbol}:{as_of}:{FEATURE_VERSION}"
    return FeatureSnapshot(
        snapshot_id=snapshot_id,
        symbol=symbol,
        as_of_ms=as_of,
        feature_version=FEATURE_VERSION,
        features=features,
        source_meta=source_meta,
        data_quality=None,
    )


def _lite_profile_key(profile: Profile | str) -> tuple[str, str]:
    if isinstance(profile, Profile):
        return profile.name, profile.base
    text = str(profile)
    if text.endswith("_FULL"):
        text = text[: -len("_FULL")] + "_LITE"
    return text, text


def _collect_breakdown_inputs(
    features: FeatureSnapshot,
) -> tuple[dict[str, Any], dict[str, int | None], dict[str, Any], list[str]]:
    feats = features.features
    factor_scores: dict[str, int | None] = {}
    raw_values: dict[str, Any] = {}
    reasons: list[str] = []
    for module in ("lifecycle", "carry", "valuation", "tradeability"):
        group = feats.get(module, {})
        factors = group.get("factors", {}) if isinstance(group, dict) else {}
        for name, item in factors.items():
            key = f"{module}.{name}"
            score = item.get("score") if isinstance(item, dict) else None
            value = item.get("value") if isinstance(item, dict) else None
            reason = item.get("reason") if isinstance(item, dict) else None
            factor_scores[key] = score
            raw_values[key] = value
            if isinstance(reason, str) and reason:
                # Surface every non-trivial deduction / null cause.
                reasons.append(reason)
    return feats, factor_scores, raw_values, reasons


def _critical_missing_from_features(features: FeatureSnapshot) -> list[str]:
    meta = features.source_meta
    if isinstance(meta, Mapping) and meta.get("critical_missing"):
        try:
            return sorted(str(item) for item in meta.get("critical_missing", []))
        except TypeError:
            return []
    feats = features.features if isinstance(features.features, Mapping) else {}
    missing: list[str] = []
    try:
        lc_score = feats["lifecycle"]["factors"]["ath_drawdown"]["score"]
    except (KeyError, TypeError):
        lc_score = None
    try:
        c_score = feats["carry"]["factors"]["funding_30d"]["score"]
    except (KeyError, TypeError):
        c_score = None
    try:
        t_oi = feats["tradeability"]["factors"]["oi"]["score"]
    except (KeyError, TypeError):
        t_oi = None
    try:
        echo = feats.get("_inputs", {})
        mc = echo.get("market_cap_usd") if isinstance(echo, Mapping) else None
    except (AttributeError, TypeError):
        mc = None
    if lc_score is None:
        missing.append("ATH")
    if c_score is None:
        missing.append("funding30d")
    if t_oi is None:
        missing.append("OI")
    if mc is None:
        missing.append("MC")
    return sorted(missing)


def score_lite(
    features: FeatureSnapshot, profile: Profile | str, config: ShortLabConfig
) -> ScoreBreakdown:
    """Score LITE modules (pure, deterministic).

    ``module_score = raw / raw_max * profile_weight`` with fixed maxima
    (25/25/10/10, never reweighted on missing data). A missing critical
    input forces ``ltss=None``.
    """
    profile_key, _ = _lite_profile_key(profile)
    weights = config.score_weights.get(profile_key)
    if weights is None:
        raise KeyError(f"unknown LITE profile {profile_key!r}")
    feats, factor_scores, raw_values, reasons = _collect_breakdown_inputs(features)
    module_max = {"lifecycle": 25, "carry": 25, "valuation": 10, "tradeability": 10}
    module_raws: dict[str, int] = {}
    module_scores: dict[str, float] = {}
    for module, maximum in module_max.items():
        group = feats.get(module, {}) if isinstance(feats, Mapping) else {}
        raw = group.get("raw", 0) if isinstance(group, dict) else 0
        raw_int = int(raw) if isinstance(raw, (int, float)) else 0
        module_raws[module] = raw_int
        weight = float(weights.get(module, 0))
        module_scores[module] = raw_int / maximum * weight if maximum else 0.0

    critical = _critical_missing_from_features(features)
    null_reason: str | None = None
    ltss: float | None
    if critical:
        ltss = None
        null_reason = "CRITICAL_INPUT_MISSING:" + ",".join(critical)
        reasons = sorted(set(reasons) | {null_reason})
    else:
        total = sum(module_scores.values())
        ltss = round_half_up_1(total)
        reasons = sorted(set(reasons))

    return ScoreBreakdown(
        ltss=ltss,
        profile=profile_key,
        score_version=SCORE_VERSION_LITE,
        feature_version=FEATURE_VERSION,
        config_hash=config_hash(config),
        module_scores=dict(module_scores),
        module_raws=dict(module_raws),
        module_max=dict(module_max),
        factor_scores=dict(factor_scores),
        raw_values=dict(raw_values),
        reasons=tuple(reasons),
        null_reason=null_reason,
        symbol=features.symbol,
        as_of_ms=features.as_of_ms,
    )


def score_full(
    features: FeatureSnapshot, profile: Profile | str, config: ShortLabConfig
) -> ScoreBreakdown:
    """Score FULL modules (pure; Task 17 wires providers, math is frozen here).

    ``valuation_supply_raw = valuation_raw_10 + unlock_raw_15`` over 25 and
    ``narrative_raw_15`` over 15. Missing unlock/narrative contribute 0 with
    no reweighting, so all-max fixtures still reach exactly 100.
    """
    if isinstance(profile, Profile):
        full_key = profile.full_name
    else:
        full_key = str(profile)
        if full_key.endswith("_LITE"):
            full_key = full_key[: -len("_LITE")] + "_FULL"
    weights = config.score_weights.get(full_key)
    if weights is None:
        raise KeyError(f"unknown FULL profile {full_key!r}")
    feats, factor_scores, raw_values, reasons = _collect_breakdown_inputs(features)

    valuation_raw_10 = 0
    group = feats.get("valuation", {}) if isinstance(feats, Mapping) else {}
    if isinstance(group, dict) and isinstance(group.get("raw"), (int, float)):
        valuation_raw_10 = int(group["raw"])
    unlock_raw_15 = 0
    unlock_group = feats.get("unlock", {}) if isinstance(feats, Mapping) else {}
    if isinstance(unlock_group, dict):
        echo = unlock_group.get("raw_15")
        if isinstance(echo, (int, float)):
            unlock_raw_15 = int(echo)
        if unlock_group.get("raw_15") is None:
            reasons.append("UNLOCK_MISSING")
    narrative_raw_15 = 0
    narrative_group = feats.get("narrative", {}) if isinstance(feats, Mapping) else {}
    if isinstance(narrative_group, dict):
        echo = narrative_group.get("raw_15")
        if isinstance(echo, (int, float)):
            narrative_raw_15 = int(echo)
        if narrative_group.get("raw_15") is None:
            reasons.append("NARRATIVE_MISSING")
    raw_values["valuation_supply_raw"] = valuation_raw_10 + unlock_raw_15
    raw_values["narrative_raw_15"] = narrative_group.get("raw_15") if isinstance(
        narrative_group, dict
    ) else None

    module_max = {
        "lifecycle": 25,
        "carry": 25,
        "valuation": 25,
        "narrative": 15,
        "tradeability": 10,
    }
    module_raws = {
        "lifecycle": int(feats.get("lifecycle", {}).get("raw", 0) or 0),
        "carry": int(feats.get("carry", {}).get("raw", 0) or 0),
        "valuation": valuation_raw_10 + unlock_raw_15,
        "narrative": narrative_raw_15,
        "tradeability": int(feats.get("tradeability", {}).get("raw", 0) or 0),
    }
    module_scores = {
        "lifecycle": module_raws["lifecycle"] / 25 * float(weights.get("lifecycle", 0)),
        "carry": module_raws["carry"] / 25 * float(weights.get("carry", 0)),
        "valuation": module_raws["valuation"] / 25 * float(weights.get("valuation", 0)),
        "narrative": module_raws["narrative"] / 15 * float(weights.get("narrative", 0)),
        "tradeability": module_raws["tradeability"] / 10
        * float(weights.get("tradeability", 0)),
    }

    critical = _critical_missing_from_features(features)
    null_reason: str | None = None
    if critical:
        ltss = None
        null_reason = "CRITICAL_INPUT_MISSING:" + ",".join(critical)
        reasons = sorted(set(reasons) | {null_reason})
    else:
        ltss = round_half_up_1(sum(module_scores.values()))
        reasons = sorted(set(reasons))

    return ScoreBreakdown(
        ltss=ltss,
        profile=full_key,
        score_version=SCORE_VERSION_FULL,
        feature_version=FEATURE_VERSION,
        config_hash=config_hash(config),
        module_scores=dict(module_scores),
        module_raws=dict(module_raws),
        module_max=dict(module_max),
        factor_scores=dict(factor_scores),
        raw_values=dict(raw_values),
        reasons=tuple(reasons),
        null_reason=null_reason,
        symbol=features.symbol,
        as_of_ms=features.as_of_ms,
    )
