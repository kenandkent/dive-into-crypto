"""F05: frozen production input factory (design A5.1; plan F05.2-F05.4).

Pure computation (no HTTP / SQL). :func:`build_feature_inputs` turns one
frozen decision instant -- ``Observed`` legs (F02), a resolved identity
(F04) and a validated config used as the frozen ``policy`` -- into an
immutable :class:`FeatureInputs`; :func:`build_field_states` turns those
inputs into real DQ :class:`FieldState` objects (no ``NOT_WIRED``
placeholders).

Contract fixes vs the legacy ``service._build_inputs`` path (A5.1):

- ``current_price`` / ``ath_price`` go through the same canonical-USD path
  (``observations.canonical_price`` in Decimal, narrowed to float):
  ``native / multiplier * fx`` with a verified multiplier
  (``EXCHANGE``/``MANUAL``) and quote FX. Nominal OI / quote-volume legs
  are never scaled; an unknown multiplier/FX yields ``None`` with
  ``UNIT_UNKNOWN`` instead of a guessed conversion. Daily closes/highs
  stay native (trends are ratios and need no scaling).
- ``funding_rates_30d`` is the complete in-window settled sequence in
  ascending time order (never ``None`` when the producer attests the
  window complete). Completeness/coverage ride the F02 envelope
  (``meta.complete`` / ``meta.coverage_fraction``); the factory only
  enforces the canonical window and the ascending order.
- ``oi_change_7d`` is ``last/first - 1`` over the usable in-window OI
  history, gated on price-window alignment
  (:func:`observations.oi_price_window_usable`, one 5m tolerance); a
  misaligned or gappy window is ``None`` with ``WINDOW_MISALIGNED``,
  never a zero-filled ratio. ``price_change_7d`` is computed once from
  the same closed-day package and :meth:`FeatureInputs.risk_meta`
  reuses the identical values for the risk layer.
- ``build_field_states`` emits real states: a successful book leg is
  ``OK`` with its real source (never ``NOT_WIRED``); a single-sided book
  is ``PARTIAL`` (never a faked double-sided ``OK``); basis without a
  confirmed spot market is ``NOT_APPLICABLE`` only with the
  exchangeInfo-confirmed absence as proof.
- Every threshold/window is read from ``policy`` exactly once per build
  (watch/candidate/veto/TTL). ``policy=None`` raises ``ValueError``
  (``REPLAY_CONFIG_UNAVAILABLE``): a missing legacy policy must never be
  silently replaced with today's config to recompute history.

LTSS/Entry bin math and rounding are untouched: :meth:`to_ltss_inputs`
emits exactly the keys ``scoring.ltss.extract_features`` reads, and the
math versions stay ``ltss-lite-v1`` / ``ltss-full-v1`` while the input
contract itself is ``features-v3`` (CR10/D15: new production bucket;
legacy ``features-v2`` rows remain readable and are never rewritten).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

from diveintocrypto_desktop.shortlab import observations as _obs
from diveintocrypto_desktop.shortlab.observations import (
    UNIT_UNKNOWN,
    WINDOW_MISALIGNED,
    Observed,
    validate_observation,
)
from diveintocrypto_desktop.shortlab.quality import FieldState, QualityPolicy
from diveintocrypto_desktop.shortlab.scoring.versions import (
    FEATURE_VERSION,
    FEATURE_VERSION_CURRENT,
)

__all__ = [
    "FEATURE_INPUTS_VERSION",
    "FEATURE_INPUTS_VERSION_V2",
    "FEATURE_INPUTS_VERSIONS_ALL",
    "REPLAY_CONFIG_UNAVAILABLE",
    "FUNDING_HISTORY_INCOMPLETE",
    "DATA_MISSING",
    "IDENTITY_UNVERIFIED",
    "SourceRef",
    "FeatureInputs",
    "build_feature_inputs",
    "build_field_states",
]

#: Input-contract version stamped on every new :class:`FeatureInputs`
#: (CR10/D15: CURRENT/v3 production bucket).
FEATURE_INPUTS_VERSION = FEATURE_VERSION_CURRENT

#: Legacy input-contract bucket, frozen for history decode/replay only.
#: Old ``features-v2`` JSON stays readable and is never rewritten.
FEATURE_INPUTS_VERSION_V2 = FEATURE_VERSION

#: All readable input-contract buckets (new writes use CURRENT/v3).
FEATURE_INPUTS_VERSIONS_ALL = frozenset({FEATURE_VERSION, FEATURE_VERSION_CURRENT})

#: Raised (as ``ValueError``) when history is replayed without its policy.
REPLAY_CONFIG_UNAVAILABLE = "REPLAY_CONFIG_UNAVAILABLE"

#: Reason codes owned by this module (producer reasons pass through as-is).
FUNDING_HISTORY_INCOMPLETE = "FUNDING_HISTORY_INCOMPLETE"
DATA_MISSING = "DATA_MISSING"
IDENTITY_UNVERIFIED = "IDENTITY_UNVERIFIED"
PARTIAL_COVERAGE = "PARTIAL_COVERAGE"

DAY_MS = 86_400_000
FUNDING_WINDOW_DAYS = 30
SEVEN_DAYS_MS = 7 * DAY_MS

USD_QUOTES = frozenset({"USD", "USDT", "USDC", "FDUSD", "TUSD", "USDP", "DAI"})

VERIFIED_MULTIPLIER_SOURCES = frozenset({"EXCHANGE", "MANUAL"})
READY_IDENTITY_CONFIDENCE = frozenset({"VERIFIED", "HIGH", "MEDIUM"})


# ---------------------------------------------------------------------------
# Small parsing helpers (tolerant readers, strict honesty)
# ---------------------------------------------------------------------------


def _finite(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    return None


def _field(data: Any, *names: str) -> Any:
    mapping = _as_mapping(data)
    if mapping is not None:
        for name in names:
            if name in mapping:
                return mapping[name]
        return None
    for name in names:
        try:
            value = getattr(data, name)
        except AttributeError:
            continue
        if callable(value):
            continue
        return value
    return None


def _candle_open_ms(candle: Any) -> int | None:
    raw = _field(candle, "t", "open_time", "open_ms", "openTime")
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    # Futures klines carry ``t`` in nanoseconds; spot-style rows in ms.
    if value > 10**14:
        value //= 1_000_000
    return value


def _candle_close(candle: Any) -> float | None:
    return _finite(_field(candle, "c", "close"))


def _candle_high(candle: Any) -> float | None:
    return _finite(_field(candle, "h", "high"))


def _candle_qv(candle: Any) -> float | None:
    return _finite(_field(candle, "qv", "quote_volume", "quoteVolume"))


def _event_time_ms(event: Any) -> int | None:
    raw = _field(event, "t", "fundingTime", "funding_time_ms", "time_ms")
    try:
        return int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _event_rate(event: Any) -> float | None:
    return _finite(_field(event, "funding_rate", "fundingRate", "rate"))


def _oi_time_ms(point: Any) -> int | None:
    raw = _field(point, "t", "time_ms", "timestamp")
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if value > 10**14:  # OI history carries ``t`` in nanoseconds
        value //= 1_000_000
    return value


def _reason_of(exc: BaseException) -> str:
    text = str(exc)
    head = text.split(":", 1)[0].strip()
    return head or "OBSERVATION_INVALID"


@dataclass(frozen=True)
class _Leg:
    """One validated observation leg: value + its real envelope."""

    value: Any
    source: str
    known_at_ms: int | None
    source_as_of_ms: int | None
    status: str
    reason_code: str | None
    complete: bool | None
    coverage_fraction: float | None
    window_start_ms: int | None
    window_end_ms: int | None
    quote_asset: str | None
    usable: bool
    failure_reason: str | None = None


def _validate_leg(
    observations: Mapping[str, Any],
    key: str,
    cutoff_ms: int,
    max_future_skew_sec: int,
) -> _Leg:
    """Fetch ``observations[key]`` and validate it against the cutoff.

    A missing key is an absent leg (``usable=False``); a present
    non-``Observed`` value is legacy provenance and is rejected, never
    scored. Validation failures degrade the leg (with the machine reason),
    never the whole symbol.
    """
    raw = observations.get(key) if isinstance(observations, Mapping) else None
    if raw is None:
        return _Leg(
            value=None,
            source="",
            known_at_ms=None,
            source_as_of_ms=None,
            status="UNAVAILABLE",
            reason_code=DATA_MISSING,
            complete=None,
            coverage_fraction=None,
            window_start_ms=None,
            window_end_ms=None,
            quote_asset=None,
            usable=False,
            failure_reason=DATA_MISSING,
        )
    if not isinstance(raw, Observed):
        return _Leg(
            value=None,
            source="",
            known_at_ms=None,
            source_as_of_ms=None,
            status="UNAVAILABLE",
            reason_code=_obs.LEGACY_PROVENANCE_UNKNOWN,
            complete=None,
            coverage_fraction=None,
            window_start_ms=None,
            window_end_ms=None,
            quote_asset=None,
            usable=False,
            failure_reason=_obs.LEGACY_PROVENANCE_UNKNOWN,
        )
    try:
        validate_observation(
            raw, cutoff_ms, max_future_skew_sec=int(max_future_skew_sec)
        )
    except Exception as exc:  # noqa: BLE001 - per-leg degradation with reason
        return _Leg(
            value=None,
            source=str(raw.meta.source or ""),
            known_at_ms=raw.meta.known_at_ms,
            source_as_of_ms=raw.meta.source_as_of_ms,
            status="UNAVAILABLE",
            reason_code=_reason_of(exc),
            complete=raw.meta.complete,
            coverage_fraction=raw.meta.coverage_fraction,
            window_start_ms=raw.meta.window_start_ms,
            window_end_ms=raw.meta.window_end_ms,
            quote_asset=(
                raw.meta.units.quote_asset if raw.meta.units is not None else None
            ),
            usable=False,
            failure_reason=_reason_of(exc),
        )
    meta = raw.meta
    return _Leg(
        value=raw.value,
        source=str(meta.source or ""),
        known_at_ms=meta.known_at_ms,
        source_as_of_ms=meta.source_as_of_ms,
        status=str(meta.status or "UNAVAILABLE"),
        reason_code=meta.reason_code,
        complete=meta.complete,
        coverage_fraction=meta.coverage_fraction,
        window_start_ms=meta.window_start_ms,
        window_end_ms=meta.window_end_ms,
        quote_asset=meta.units.quote_asset if meta.units is not None else None,
        usable=True,
    )


# ---------------------------------------------------------------------------
# Frozen DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRef:
    """Point-in-time provenance of one observation leg (never API JSON)."""

    name: str
    source: str
    known_at_ms: int | None
    source_as_of_ms: int | None
    status: str
    reason_code: str | None


@dataclass(frozen=True)
class FeatureInputs:
    """Frozen production inputs for one symbol at one decision cutoff.

    Prices that enter scoring (``current_price`` / ``ath_price``) are
    canonical USD (same multiplier + FX path); daily closes/highs stay
    native (trends are ratios). ``funding_rates_30d`` is the complete
    in-window settled sequence, ascending. ``price_change_7d`` and
    ``oi_change_7d`` are computed once over the same cutoff-aligned
    package; :meth:`risk_meta` hands the identical values to the risk
    layer. OI / quote-volume legs are nominals, never unit-scaled.
    """

    version: str
    symbol: str
    decision_as_of_ms: int
    policy_hash: str | None
    identity_snapshot_id: str | None
    # Canonical-USD point prices (None with unit_reason on UNIT_UNKNOWN).
    current_price: float | None
    current_price_source: str | None
    ath_price: float | None
    ath_date_ms: int | None
    unit_reason: str | None
    price_change_24h: float | None
    # Native daily legs (closed days only, ascending).
    daily_closes: tuple[float, ...]
    daily_highs: tuple[float, ...]
    daily_open_ms: tuple[int, ...]
    futures_qv_1d: float | None
    # Funding windows (sums are None unless the window is complete).
    funding_rates_30d: tuple[float, ...]
    funding_window_start_ms: int
    funding_window_end_ms: int
    funding_30d: float | None
    funding_7d: float | None
    funding_positive_ratio_30d: float | None
    funding_30d_complete: bool
    funding_30d_coverage: float
    funding_90d: float | None
    funding_positive_ratio_90d: float | None
    funding_90d_complete: bool
    funding_90d_coverage: float
    # 7D package (single computation, shared with risk).
    price_change_7d: float | None
    price_window_start_ms: int | None
    price_window_end_ms: int | None
    oi_change_7d: float | None
    oi_window_start_ms: int | None
    oi_window_end_ms: int | None
    oi_window_reason: str | None
    oi_value_usd: float | None
    # Spot leg (None when N/A or unknown -- never 0).
    spot_status: str
    spot_applicable: bool
    spot_reason: str | None
    spot_volume_30d: float | None
    spot_volume_prev_30d: float | None
    spot_quote_volume_24h: float | None
    spot_price: float | None
    spot_daily_bars: int | None
    futures_spot_volume_ratio: float | None
    premium: float | None
    # Fundamentals (None when unknown -- never 0).
    market_cap_usd: float | None
    fdv_usd: float | None
    circulating_supply: float | None
    total_supply: float | None
    # Microstructure / contract legs.
    spread: float | None
    best_bid: float | None
    best_ask: float | None
    book_bid_notional_1pct: float | None
    book_ask_notional_1pct: float | None
    book_status: str
    book_reason: str | None
    contract_status: str | None
    onboard_at_ms: int | None
    delivery_at_ms: int | None
    live_universe_present: bool | None
    mapping_confidence: str | None
    contract_multiplier: float | None
    # Derived 30D trend (same rule as the legacy service path).
    return_30d: float | None
    close_30d_ago: float | None
    sources: tuple[SourceRef, ...] = ()

    def to_ltss_inputs(self) -> dict[str, Any]:
        """Mapping with exactly the keys ``extract_features`` reads."""
        return {
            "symbol": self.symbol,
            "ath_price": self.ath_price,
            "current_price": self.current_price,
            "ath_date_ms": self.ath_date_ms,
            "daily_closes": list(self.daily_closes),
            "daily_highs": list(self.daily_highs),
            "close_30d_ago": self.close_30d_ago,
            "return_30d": self.return_30d,
            "spot_volume_30d": self.spot_volume_30d,
            "spot_volume_prev_30d": self.spot_volume_prev_30d,
            "spot_applicable": self.spot_applicable,
            "spot_status": self.spot_status,
            "funding_30d": self.funding_30d,
            "funding_30d_complete": self.funding_30d_complete,
            "funding_positive_ratio_30d": self.funding_positive_ratio_30d,
            "funding_30d_ratio_complete": self.funding_30d_complete,
            "funding_positive_ratio_90d": self.funding_positive_ratio_90d,
            "funding_90d_complete": self.funding_90d_complete,
            "funding_rates_30d": list(self.funding_rates_30d),
            "market_cap_usd": self.market_cap_usd,
            "fdv_usd": self.fdv_usd,
            "circulating_supply": self.circulating_supply,
            "total_supply": self.total_supply,
            "oi_value_usd": self.oi_value_usd,
            "price_change_7d": self.price_change_7d,
            "oi_change_7d": self.oi_change_7d,
            "futures_spot_volume_ratio": self.futures_spot_volume_ratio,
            "futures_qv_1d": self.futures_qv_1d,
            "spread": self.spread,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "book_bid_notional_1pct": self.book_bid_notional_1pct,
            "book_ask_notional_1pct": self.book_ask_notional_1pct,
            "contract_status": self.contract_status,
            "mapping_confidence": self.mapping_confidence,
            "contract_multiplier": self.contract_multiplier,
            "onboard_at_ms": self.onboard_at_ms,
        }

    def risk_meta(self) -> dict[str, Any]:
        """Risk-layer view reusing the single-computed 7D values.

        Returns the stored attributes (identical objects, not recomputed
        copies) so the risk engine can never drift from the scored inputs.
        """
        return {
            "symbol": self.symbol,
            "as_of_ms": self.decision_as_of_ms,
            "mapping_confidence": self.mapping_confidence,
            "contract_multiplier": self.contract_multiplier,
            "onboard_at_ms": self.onboard_at_ms,
            "delivery_at_ms": self.delivery_at_ms,
            "exchange_status": self.contract_status,
            "contract_status": self.contract_status,
            "live_universe_present": self.live_universe_present,
            "price_change_24h": self.price_change_24h,
            "price_change_7d": self.price_change_7d,
            "oi_change_7d": self.oi_change_7d,
            "funding_30d": self.funding_30d,
            "funding_7d": self.funding_7d,
            "funding_positive_ratio_30d": self.funding_positive_ratio_30d,
            "oi_value_usd": self.oi_value_usd,
            "futures_qv_1d": self.futures_qv_1d,
        }


# ---------------------------------------------------------------------------
# Policy access (single frozen read)
# ---------------------------------------------------------------------------


def _require_policy(policy: Any) -> Any:
    if policy is None:
        raise ValueError(
            f"{REPLAY_CONFIG_UNAVAILABLE}: build_feature_inputs requires the "
            "frozen policy the snapshot was taken with; a missing legacy "
            "policy must never be replaced with today's config"
        )
    return policy


def _policy_value(policy: Any, *path: str, default: Any = None) -> Any:
    current = policy
    for name in path:
        current = _field(current, name)
        if current is None:
            return default
    return current


def _policy_hash_of(policy: Any) -> str | None:
    to_dict = getattr(policy, "to_dict", None)
    if not callable(to_dict):
        return None
    try:
        from diveintocrypto_desktop.shortlab.config import policy_hash as _hash

        return _hash(policy)
    except Exception:  # noqa: BLE001 - duck-typed doubles carry no hash
        return None


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _canonical_usd(
    native: Any,
    multiplier: Any,
    multiplier_source: Any,
    fx: Any,
    quote_asset: str | None,
) -> tuple[float | None, str | None]:
    """Canonical-USD price via Decimal; ``None`` + reason when unverified."""
    source = str(multiplier_source).upper() if multiplier_source is not None else ""
    mult = _finite(multiplier)
    if mult is None or mult <= 0 or source not in VERIFIED_MULTIPLIER_SOURCES:
        return None, UNIT_UNKNOWN
    rate = _finite(fx)
    if rate is None:
        quote = str(quote_asset or "USDT").strip().upper()
        if quote == "USD":
            rate = 1.0
        else:
            return None, UNIT_UNKNOWN
    if rate <= 0:
        return None, UNIT_UNKNOWN
    try:
        value = _obs.canonical_price(native, mult, rate)
    except Exception:  # noqa: BLE001 - unparseable native leg
        return None, UNIT_UNKNOWN
    if value is None:
        return None, UNIT_UNKNOWN
    try:
        return float(value), None
    except (TypeError, ValueError):
        return None, UNIT_UNKNOWN


def _funding_series(
    leg: _Leg, window_start_ms: int, window_end_ms: int
) -> tuple[tuple[float, ...], tuple[int, ...], bool, float]:
    """In-window settled ``(rates, times)`` ascending + envelope completeness.

    Completeness/coverage are producer-attested (F02 envelope); the factory
    only enforces the canonical window and the ascending order. Anything
    outside ``[start, end)`` is dropped, never scored.
    """
    if not leg.usable or not isinstance(leg.value, (list, tuple)):
        return (), (), False, 0.0
    pairs: list[tuple[int, float]] = []
    for event in leg.value:
        moment = _event_time_ms(event)
        rate = _event_rate(event)
        if moment is None or rate is None:
            continue
        if window_start_ms <= moment < window_end_ms:
            pairs.append((moment, rate))
    pairs.sort(key=lambda item: item[0])
    complete = bool(leg.complete) and bool(pairs)
    coverage = leg.coverage_fraction
    try:
        coverage_f = float(coverage) if coverage is not None else (1.0 if complete else 0.0)
    except (TypeError, ValueError):
        coverage_f = 1.0 if complete else 0.0
    if coverage_f != coverage_f:  # NaN guard: never trust a NaN attestation
        coverage_f = 1.0 if complete else 0.0
    coverage_f = min(1.0, max(0.0, coverage_f))
    return (
        tuple(rate for _, rate in pairs),
        tuple(moment for moment, _ in pairs),
        complete,
        coverage_f,
    )


def build_feature_inputs(
    symbol: str,
    observations: Mapping[str, Any],
    identity: Any,
    decision_as_of_ms: int,
    policy: Any,
) -> FeatureInputs:
    """Build the frozen :class:`FeatureInputs` for one decision instant.

    ``observations`` maps leg names to F02 ``Observed`` values (see the
    module docstring); ``identity`` is the F04-resolved identity;
    ``decision_as_of_ms`` is the frozen cutoff every leg is validated
    against; ``policy`` is the validated config acting as the frozen
    policy (read exactly once -- skew tolerance, funding lookbacks and the
    policy hash). ``policy=None`` raises ``ValueError``
    (``REPLAY_CONFIG_UNAVAILABLE``).
    """
    _require_policy(policy)
    cutoff = int(decision_as_of_ms)
    name = str(symbol)
    skew = _policy_value(policy, "ingestion", "max_source_future_skew_sec", default=2)
    try:
        skew_sec = int(skew)
    except (TypeError, ValueError):
        skew_sec = 2

    legs = {
        key: _validate_leg(observations, key, cutoff, skew_sec)
        for key in (
            "klines_daily",
            "ticker_24h",
            "funding_history",
            "funding_history_90d",
            "oi_history",
            "ath",
            "fundamentals",
            "spot_history",
            "book",
            "contract",
            "fx_rate",
        )
    }
    sources = tuple(
        SourceRef(
            name=key,
            source=leg.source,
            known_at_ms=leg.known_at_ms,
            source_as_of_ms=leg.source_as_of_ms,
            status=leg.status,
            reason_code=leg.reason_code,
        )
        for key, leg in legs.items()
    )

    multiplier = _field(identity, "contract_multiplier")
    multiplier_source = _field(identity, "multiplier_source")
    confidence = _field(identity, "mapping_confidence")
    identity_snapshot_id = _field(identity, "identity_snapshot_id")

    quote_asset = _field(observations, "quote_asset")
    if not isinstance(quote_asset, str) or not quote_asset.strip():
        quote_asset = "USDT"
    fx_leg = legs["fx_rate"]
    fx = _finite(fx_leg.value) if fx_leg.usable else None

    # -- daily legs (closed days only, ascending) ---------------------------
    klines = legs["klines_daily"]
    closes: list[float] = []
    highs: list[float] = []
    opens: list[int] = []
    if klines.usable and isinstance(klines.value, (list, tuple)):
        rows: list[tuple[int, float, float | None, float | None]] = []
        for candle in klines.value:
            moment = _candle_open_ms(candle)
            close = _candle_close(candle)
            if moment is None or close is None or moment >= cutoff:
                continue  # unclosed as of the cutoff is never scored
            rows.append((moment, close, _candle_high(candle), _candle_qv(candle)))
        rows.sort(key=lambda row: row[0])
        seen: set[int] = set()
        for moment, close, high, _ in rows:
            if moment in seen:
                continue  # repeated open time is ambiguous; keep first
            seen.add(moment)
            opens.append(moment)
            closes.append(close)
            highs.append(high if high is not None else close)
    # Last closed-day quote volume (never the rolling ticker, never 0).
    futures_qv_1d: float | None = None
    if klines.usable and isinstance(klines.value, (list, tuple)) and opens:
        last_open = opens[-1]
        for candle in klines.value:
            if _candle_open_ms(candle) == last_open:
                futures_qv_1d = _candle_qv(candle)
                break

    # -- canonical-USD point prices (same multiplier + FX path) -------------
    ticker = legs["ticker_24h"]
    ticker_map = _as_mapping(ticker.value) if ticker.usable else None
    ticker_price = _finite(_field(ticker_map, "current_price", "price", "last_price"))
    ticker_valid = ticker.usable and ticker_price is not None
    native_price = ticker_price if ticker_valid else (closes[-1] if closes else None)
    current_price, unit_reason = _canonical_usd(
        native_price, multiplier, multiplier_source, fx, quote_asset
    )
    if native_price is None:
        unit_reason = unit_reason or DATA_MISSING
    price_source = (
        "ticker_24h" if ticker_valid else ("daily_close" if closes else None)
    )
    price_change_24h = _finite(_field(ticker_map, "price_change_24h", "chg_24h"))

    ath_leg = legs["ath"]
    ath_map = _as_mapping(ath_leg.value) if ath_leg.usable else None
    if ath_map is not None:
        ath_native = _field(ath_map, "ath_price", "ath_usd", "price", "ath")
        ath_date_ms = _field(ath_map, "ath_date_ms", "ath_date", "date_ms")
    else:
        ath_native = _finite(ath_leg.value) if ath_leg.usable else None
        ath_date_ms = None
    try:
        ath_date = int(ath_date_ms) if ath_date_ms is not None else None
    except (TypeError, ValueError):
        ath_date = None
    # CoinGecko ATH is already per base coin in USD; never scale twice.
    if ath_map is not None and ath_map.get("ath_usd") is not None:
        ath_price = _finite(ath_map["ath_usd"])
        ath_reason = None if ath_price is not None and ath_price > 0 else UNIT_UNKNOWN
    else:
        ath_price, ath_reason = _canonical_usd(
            ath_native, multiplier, multiplier_source, fx, quote_asset
        )
    if ath_price is None and unit_reason is None:
        unit_reason = ath_reason if ath_native is not None else DATA_MISSING

    # -- funding windows (canonical 30D window, ascending) -------------------
    midnight = (cutoff // DAY_MS) * DAY_MS
    funding_end = midnight
    funding_start = midnight - FUNDING_WINDOW_DAYS * DAY_MS
    rates_30d, _times_30d, complete_30d, coverage_30d = _funding_series(
        legs["funding_history"], funding_start, funding_end
    )
    funding_30d = float(sum(rates_30d)) if complete_30d else None
    slice_7d = [
        rate
        for moment, rate in zip(_times_30d, rates_30d)
        if moment >= funding_end - SEVEN_DAYS_MS
    ]
    # The 7D slice inherits the attested 30D package: same events, same
    # cutoff, no second fetch and no independent completeness claim.
    funding_7d = float(sum(slice_7d)) if complete_30d and slice_7d else None
    positive_ratio_30d = (
        sum(1 for rate in rates_30d if rate > 0) / len(rates_30d)
        if complete_30d and rates_30d
        else None
    )

    rates_90d, _times_90d, complete_90d, coverage_90d = _funding_series(
        legs["funding_history_90d"],
        funding_end - 90 * DAY_MS,
        funding_end,
    )
    funding_90d = float(sum(rates_90d)) if complete_90d else None
    positive_ratio_90d = (
        sum(1 for rate in rates_90d if rate > 0) / len(rates_90d)
        if complete_90d and rates_90d
        else None
    )

    # -- 7D package: one price move, one OI move, same cutoff (R04/D04.3) --
    # Price compares the last closed UTC day D vs D-7 close (7x24h, never
    # expanded from D-7 open to 8D). OI compares the two corresponding close
    # moments; each end looks back at most 5min for the nearest known sample
    # (never future samples). Range is 7*DAY_MS; an 8D span is rejected.
    price_change_7d: float | None = None
    price_window_start: int | None = None
    price_window_end: int | None = None
    if len(closes) >= 8 and len(opens) >= 8:
        prev, last = closes[-8], closes[-1]
        open_d7, open_d = opens[-8], opens[-1]
        # Strict consecutive-day guard: D open minus D-7 open must be 7 days.
        # Gappy/missing days never fabricate a 7D return; an 8D expansion
        # (open D-7 to close D) is rejected by construction below.
        if prev is not None and last is not None and prev > 0:
            try:
                span_ok = (int(open_d) - int(open_d7)) == 7 * DAY_MS
            except (TypeError, ValueError):
                span_ok = False
            if span_ok:
                price_change_7d = last / prev - 1.0
                # Close moments (not opens): 7x24h window.
                price_window_start = int(open_d7) + DAY_MS
                price_window_end = int(open_d) + DAY_MS

    oi_leg = legs["oi_history"]
    oi_points: list[tuple[int, float | None]] = []
    if oi_leg.usable and isinstance(oi_leg.value, (list, tuple)):
        for point in oi_leg.value:
            moment = _oi_time_ms(point)
            if moment is None or moment > cutoff:
                continue  # PIT: never score a point from after the cutoff
            oi_points.append((moment, _finite(_field(point, "oi_value", "oiValue"))))
    oi_points.sort(key=lambda item: item[0])
    # R04: per-end backward 5min lookup at the two close moments (same frozen
    # package, same cutoff). Each end takes the nearest known sample at or
    # before the close within 5min; future samples are never used; an 8D
    # span (e.g. D-7 open) is outside the 5min lookback and is rejected.
    def _latest_at_or_before(close_ms: int) -> tuple[int, float | None] | None:
        best: tuple[int, float | None] | None = None
        lo = close_ms - _obs.OI_PRICE_SYNC_TOLERANCE_MS
        for moment, value in oi_points:
            if moment <= close_ms and moment >= lo:
                best = (moment, value)
        return best

    oi_change_7d: float | None = None
    oi_window_start: int | None = None
    oi_window_end: int | None = None
    oi_window_reason: str | None = None
    oi_value_usd: float | None = None
    # OI latest value (for oi_value_usd) is the nearest at-or-before the D
    # close within 5min when the 7D window exists, else the latest known
    # at-or-before cutoff (nominal, never unit-scaled).
    latest_oi_ms: int | None = None
    latest_oi_val: float | None = None
    if oi_points:
        # Latest at-or-before cutoff (already filtered) for nominal display.
        latest_oi_ms, latest_oi_val = oi_points[-1]
    if price_window_start is not None and price_window_end is not None:
        start_hit = _latest_at_or_before(price_window_start)
        end_hit = _latest_at_or_before(price_window_end)
        quote = (oi_leg.quote_asset or "USDT").strip().upper()
        if quote not in USD_QUOTES:
            oi_window_reason = UNIT_UNKNOWN
        elif start_hit is None or end_hit is None:
            oi_window_reason = WINDOW_MISALIGNED
        else:
            first_ms, first_oi = start_hit
            last_ms, last_oi = end_hit
            # Both ends must be within 5min backward (enforced by lookup);
            # an 8D-separated pair can never both hit, so it is rejected.
            if first_oi is None or last_oi is None or not (first_oi > 0 and last_oi > 0):
                oi_window_reason = "OI_UNIT_UNVERIFIED"
            else:
                oi_change_7d = last_oi / first_oi - 1.0
                oi_window_start, oi_window_end = first_ms, last_ms
        # Nominal OI value uses the D-close sample when available (same
        # frozen instant as price D), else falls back to latest known.
        display_ms: int | None = None
        display_val: float | None = None
        if end_hit is not None:
            display_ms, display_val = end_hit
        else:
            display_ms, display_val = latest_oi_ms, latest_oi_val
        if display_val is not None and display_val > 0 and quote in USD_QUOTES:
            # sumOpenInterestValue is quote nominal, not contract quantity.
            # Convert quote→USD only; never apply the contract multiplier.
            oi_value_usd = (display_val if quote == "USD" else
                            display_val * fx if fx is not None and fx > 0 else None)
            # If the D-close sample itself is missing, the 7D leg stays
            # misaligned even though a nominal value may still display.
            if end_hit is None and oi_window_reason is None:
                oi_window_reason = WINDOW_MISALIGNED
        elif oi_window_reason is None:
            oi_window_reason = "OI_UNIT_UNVERIFIED"
    elif oi_leg.usable:
        oi_window_reason = WINDOW_MISALIGNED
        # Still surface a nominal when the price window is absent but OI is known.
        if latest_oi_val is not None and latest_oi_val > 0:
            quote2 = (oi_leg.quote_asset or "USDT").strip().upper()
            if quote2 in USD_QUOTES:
                oi_value_usd = (latest_oi_val if quote2 == "USD" else
                                latest_oi_val * fx if fx is not None and fx > 0 else None)
    else:
        oi_window_reason = oi_leg.failure_reason or DATA_MISSING

    # -- spot leg (exchangeInfo-confirmed absence is the only N/A proof) -----
    # The F02 wrapper carries the legacy ProviderResult as its value; both
    # mappings and duck-typed objects are read via _field (a ProviderResult
    # is a dataclass, not a Mapping).
    spot_leg = legs["spot_history"]
    spot_result = spot_leg.value if spot_leg.usable else None
    spot_status = str(_field(spot_result, "status") or "UNAVAILABLE")
    if spot_status not in ("OK", "PARTIAL", "NOT_APPLICABLE", "UNAVAILABLE", "ERROR"):
        spot_status = "UNAVAILABLE"
    spot_applicable = spot_status != "NOT_APPLICABLE"
    spot_reason = _field(spot_result, "reason_code")
    spot_data = _field(spot_result, "data")
    spot_volume_30d = _finite(_field(spot_data, "spot_volume_30d"))
    spot_volume_prev_30d = _finite(_field(spot_data, "spot_volume_prev_30d"))
    spot_quote_volume_24h = _finite(_field(spot_data, "spot_quote_volume_24h"))
    spot_price = _finite(_field(spot_data, "spot_price"))
    spot_bars = _field(spot_data, "spot_daily_bars")
    try:
        spot_daily_bars: int | None = int(spot_bars) if spot_bars is not None else None
    except (TypeError, ValueError):
        spot_daily_bars = None
    futures_spot_volume_ratio = _finite(
        _field(spot_data, "futures_spot_volume_ratio_30d", "futures_spot_volume_ratio")
    )
    premium = _finite(_field(spot_data, "premium"))

    # -- fundamentals ---------------------------------------------------------
    fund_leg = legs["fundamentals"]
    fund_map = _as_mapping(fund_leg.value) if fund_leg.usable else None
    market_cap_usd = _finite(_field(fund_map, "market_cap_usd"))
    fdv_usd = _finite(_field(fund_map, "fdv_usd"))
    circulating_supply = _finite(_field(fund_map, "circulating_supply"))
    total_supply = _finite(_field(fund_map, "total_supply"))

    # -- book + contract legs --------------------------------------------------
    book_leg = legs["book"]
    book_map = _as_mapping(book_leg.value) if book_leg.usable else None
    bid_n = _finite(_field(book_map, "bid_notional_1pct", "bid_notional"))
    ask_n = _finite(_field(book_map, "ask_notional_1pct", "ask_notional"))
    spread = _finite(_field(book_map, "spread"))
    best_bid = _finite(_field(book_map, "best_bid"))
    best_ask = _finite(_field(book_map, "best_ask"))
    if spread is None and best_bid and best_ask and best_bid > 0 and best_ask > 0:
        mid = (best_bid + best_ask) / 2.0
        if mid > 0:
            spread = (best_ask - best_bid) / mid
    book_status = str(book_leg.status or "UNAVAILABLE")
    book_reason = book_leg.reason_code

    contract_leg = legs["contract"]
    contract_map = _as_mapping(contract_leg.value) if contract_leg.usable else None
    contract_status = _field(contract_map, "status", "contract_status", "exchange_status")
    if contract_status is not None:
        contract_status = str(contract_status)
    onboard_raw = _field(contract_map, "onboard_at_ms", "onboardAtMs", "listing_at_ms")
    delivery_raw = _field(contract_map, "delivery_at_ms", "deliveryAtMs")
    try:
        onboard_at_ms: int | None = int(onboard_raw) if onboard_raw is not None else None
    except (TypeError, ValueError):
        onboard_at_ms = None
    try:
        delivery_at_ms: int | None = (
            int(delivery_raw) if delivery_raw is not None else None
        )
    except (TypeError, ValueError):
        delivery_at_ms = None
    live_present = _field(contract_map, "live_universe_present", "in_live_universe")
    live_universe_present = bool(live_present) if live_present is not None else None

    # -- 30D trend (same rule as the legacy service path) ----------------------
    return_30d: float | None = None
    close_30d_ago: float | None = None
    if len(closes) >= 31 and closes[-31] is not None and closes[-31] > 0:
        close_30d_ago = closes[-31]
        return_30d = closes[-1] / closes[-31] - 1.0

    try:
        mult_f = float(multiplier) if multiplier is not None else None
    except (TypeError, ValueError):
        mult_f = None

    return FeatureInputs(
        version=FEATURE_INPUTS_VERSION,
        symbol=name,
        decision_as_of_ms=cutoff,
        policy_hash=_policy_hash_of(policy),
        identity_snapshot_id=(
            str(identity_snapshot_id) if identity_snapshot_id is not None else None
        ),
        current_price=current_price,
        current_price_source=price_source,
        ath_price=ath_price,
        ath_date_ms=ath_date,
        unit_reason=unit_reason,
        price_change_24h=price_change_24h,
        daily_closes=tuple(closes),
        daily_highs=tuple(highs),
        daily_open_ms=tuple(opens),
        futures_qv_1d=futures_qv_1d,
        funding_rates_30d=tuple(rates_30d),
        funding_window_start_ms=funding_start,
        funding_window_end_ms=funding_end,
        funding_30d=funding_30d,
        funding_7d=funding_7d,
        funding_positive_ratio_30d=positive_ratio_30d,
        funding_30d_complete=complete_30d,
        funding_30d_coverage=coverage_30d,
        funding_90d=funding_90d,
        funding_positive_ratio_90d=positive_ratio_90d,
        funding_90d_complete=complete_90d,
        funding_90d_coverage=coverage_90d,
        price_change_7d=price_change_7d,
        price_window_start_ms=price_window_start,
        price_window_end_ms=price_window_end,
        oi_change_7d=oi_change_7d,
        oi_window_start_ms=oi_window_start,
        oi_window_end_ms=oi_window_end,
        oi_window_reason=oi_window_reason,
        oi_value_usd=oi_value_usd,
        spot_status=spot_status,
        spot_applicable=spot_applicable,
        spot_reason=str(spot_reason) if spot_reason is not None else None,
        spot_volume_30d=spot_volume_30d,
        spot_volume_prev_30d=spot_volume_prev_30d,
        spot_quote_volume_24h=spot_quote_volume_24h,
        spot_price=spot_price,
        spot_daily_bars=spot_daily_bars,
        futures_spot_volume_ratio=futures_spot_volume_ratio,
        premium=premium,
        market_cap_usd=market_cap_usd,
        fdv_usd=fdv_usd,
        circulating_supply=circulating_supply,
        total_supply=total_supply,
        spread=spread,
        best_bid=best_bid,
        best_ask=best_ask,
        book_bid_notional_1pct=bid_n,
        book_ask_notional_1pct=ask_n,
        book_status=book_status,
        book_reason=str(book_reason) if book_reason is not None else None,
        contract_status=contract_status,
        onboard_at_ms=onboard_at_ms,
        delivery_at_ms=delivery_at_ms,
        live_universe_present=live_universe_present,
        mapping_confidence=str(confidence) if confidence is not None else None,
        contract_multiplier=mult_f,
        return_30d=return_30d,
        close_30d_ago=close_30d_ago,
        sources=sources,
    )


# ---------------------------------------------------------------------------
# Field states (real DQ states, never NOT_WIRED placeholders)
# ---------------------------------------------------------------------------


def _source_of(inputs: FeatureInputs, *names: str, default: str = "shortlab") -> str:
    for ref in inputs.sources:
        if ref.name in names and ref.source:
            return ref.source
    return default


def _known_of(inputs: FeatureInputs, *names: str) -> int | None:
    for ref in inputs.sources:
        if ref.name in names and ref.known_at_ms is not None:
            return int(ref.known_at_ms)
    return None


def _count_state(
    field_id: str,
    valid: float | None,
    required: int,
    fetched_at_ms: int | None,
    source: str,
    reason: str | None = None,
) -> FieldState:
    try:
        n = float(valid) if valid is not None else 0.0
    except (TypeError, ValueError):
        n = 0.0
    if n >= required:
        return FieldState(
            field_id=field_id, status="OK", fetched_at_ms=fetched_at_ms, source=source
        )
    if n > 0:
        return FieldState(
            field_id=field_id,
            status="PARTIAL",
            valid_count=n,
            fetched_at_ms=fetched_at_ms,
            reason_code=reason or PARTIAL_COVERAGE,
            source=source,
        )
    return FieldState(
        field_id=field_id,
        status="UNAVAILABLE",
        fetched_at_ms=fetched_at_ms,
        reason_code=reason or DATA_MISSING,
        source=source,
    )


def _ok_state(
    field_id: str,
    ok: bool,
    fetched_at_ms: int | None,
    source: str,
    reason: str | None = None,
) -> FieldState:
    if ok:
        return FieldState(
            field_id=field_id, status="OK", fetched_at_ms=fetched_at_ms, source=source
        )
    return FieldState(
        field_id=field_id,
        status="UNAVAILABLE",
        fetched_at_ms=fetched_at_ms,
        reason_code=reason or DATA_MISSING,
        source=source,
    )


def build_field_states(
    inputs: FeatureInputs, policy: Any
) -> tuple[FieldState, ...]:
    """Real DQ field states for ``inputs`` (LITE-complete, F07 extends).

    Every state carries the real source observation's ``known_at`` and the
    real producer reason; nothing is socketed to ``now_ms`` and no
    ``*_NOT_WIRED`` placeholder is ever emitted. ``policy`` supplies the
    PARTIAL denominators (a :class:`QualityPolicy` or the validated config;
    ``None`` falls back to the frozen defaults). FULL unlock/social/
    catalyst groups need live provider results and are appended downstream
    via ``quality.full_tier_field_states``.
    """
    counts: Mapping[str, int] = {}
    if isinstance(policy, QualityPolicy):
        counts = policy.field_required_counts
    else:
        from diveintocrypto_desktop.shortlab.quality import FIELD_REQUIRED_COUNTS

        counts = FIELD_REQUIRED_COUNTS

    def required(field_id: str, default: int) -> int:
        try:
            return int(counts.get(field_id, default))
        except (TypeError, ValueError):
            return default

    states: list[FieldState] = []
    klines_known = _known_of(inputs, "klines_daily")
    klines_source = _source_of(inputs, "klines_daily", default="binance-klines")

    # -- market legs ---------------------------------------------------------
    states.append(
        _count_state(
            "market_daily_price",
            float(len(inputs.daily_closes)),
            required("market_daily_price", 70),
            klines_known,
            klines_source,
            PARTIAL_COVERAGE,
        )
    )
    states.append(
        _ok_state(
            "futures_qv_1d",
            inputs.futures_qv_1d is not None,
            klines_known,
            klines_source,
        )
    )
    oi_known = _known_of(inputs, "oi_history")
    oi_source = _source_of(inputs, "oi_history", default="binance-oi")
    oi_reason = inputs.oi_window_reason
    if inputs.oi_value_usd is None and oi_reason is None:
        for ref in inputs.sources:
            if ref.name == "oi_history" and ref.reason_code:
                oi_reason = ref.reason_code
                break
    states.append(
        _ok_state("oi_usd", inputs.oi_value_usd is not None, oi_known, oi_source, oi_reason)
    )
    contract_known = _known_of(inputs, "contract")
    contract_source = _source_of(inputs, "contract", default="binance-exchangeInfo")
    states.append(
        _ok_state(
            "contract_status",
            inputs.contract_status is not None,
            contract_known,
            contract_source,
        )
    )

    # -- funding legs (producer-attested completeness, same package) ---------
    funding_known = _known_of(inputs, "funding_history")
    funding_source = _source_of(inputs, "funding_history", default="binance-funding")
    funding_reason: str | None = None
    for ref in inputs.sources:
        if ref.name == "funding_history" and ref.reason_code:
            funding_reason = ref.reason_code
            break
    if inputs.funding_30d_complete:
        states.append(
            FieldState(
                field_id="funding_7d",
                status="OK",
                fetched_at_ms=funding_known,
                source=funding_source,
            )
        )
        states.append(
            FieldState(
                field_id="funding_30d",
                status="OK",
                fetched_at_ms=funding_known,
                source=funding_source,
            )
        )
    elif inputs.funding_rates_30d:
        for field_id in ("funding_7d", "funding_30d"):
            states.append(
                FieldState(
                    field_id=field_id,
                    status="PARTIAL",
                    coverage=inputs.funding_30d_coverage,
                    fetched_at_ms=funding_known,
                    reason_code=funding_reason or FUNDING_HISTORY_INCOMPLETE,
                    source=funding_source,
                )
            )
    else:
        for field_id in ("funding_7d", "funding_30d"):
            states.append(
                FieldState(
                    field_id=field_id,
                    status="UNAVAILABLE",
                    fetched_at_ms=funding_known,
                    reason_code=funding_reason or FUNDING_HISTORY_INCOMPLETE,
                    source=funding_source,
                )
            )
    funding_90d_known = _known_of(inputs, "funding_history_90d") or funding_known
    if inputs.funding_90d_complete:
        states.append(
            FieldState(
                field_id="funding_90d",
                status="OK",
                fetched_at_ms=funding_90d_known,
                source=funding_source,
            )
        )
    elif inputs.funding_90d_coverage > 0:
        states.append(
            FieldState(
                field_id="funding_90d",
                status="PARTIAL",
                coverage=inputs.funding_90d_coverage,
                fetched_at_ms=funding_90d_known,
                reason_code=funding_reason or FUNDING_HISTORY_INCOMPLETE,
                source=funding_source,
            )
        )
    else:
        reason_90d: str | None = None
        for ref in inputs.sources:
            if ref.name == "funding_history_90d" and ref.reason_code:
                reason_90d = ref.reason_code
                break
        states.append(
            FieldState(
                field_id="funding_90d",
                status="UNAVAILABLE",
                fetched_at_ms=funding_90d_known,
                reason_code=reason_90d or FUNDING_HISTORY_INCOMPLETE,
                source=funding_source,
            )
        )

    # -- fundamentals ---------------------------------------------------------
    fund_known = _known_of(inputs, "fundamentals", "ath")
    fund_source = _source_of(inputs, "fundamentals", "ath", default="coingecko")
    states.append(
        _ok_state("mc", inputs.market_cap_usd is not None, fund_known, fund_source)
    )
    states.append(_ok_state("fdv", inputs.fdv_usd is not None, fund_known, fund_source))
    have_circ = inputs.circulating_supply is not None
    have_total = inputs.total_supply is not None
    if have_circ and have_total:
        states.append(
            FieldState(
                field_id="supply_float",
                status="OK",
                fetched_at_ms=fund_known,
                source=fund_source,
            )
        )
    elif have_circ or have_total:
        states.append(
            FieldState(
                field_id="supply_float",
                status="PARTIAL",
                valid_count=1,
                fetched_at_ms=fund_known,
                source=fund_source,
            )
        )
    else:
        states.append(
            FieldState(
                field_id="supply_float",
                status="UNAVAILABLE",
                fetched_at_ms=fund_known,
                reason_code=DATA_MISSING,
                source=fund_source,
            )
        )
    ath_n = (1 if inputs.ath_price is not None else 0) + (
        1 if inputs.ath_date_ms is not None else 0
    )
    states.append(
        _count_state(
            "ath",
            float(ath_n),
            required("ath", 2),
            _known_of(inputs, "ath", "fundamentals"),
            fund_source,
        )
    )

    # -- spot legs (NOT_APPLICABLE only with confirmed absence) ---------------
    spot_known: int | None = None
    for ref in inputs.sources:
        if ref.name == "spot_history" and ref.known_at_ms is not None:
            spot_known = int(ref.known_at_ms)
            break
    spot_source = _source_of(inputs, "spot_history", default="binance-spot")
    if not inputs.spot_applicable:
        for field_id in ("spot_60d_qv", "spot_24h_qv"):
            states.append(
                FieldState(
                    field_id=field_id,
                    status="NOT_APPLICABLE",
                    fetched_at_ms=spot_known,
                    reason_code=inputs.spot_reason,
                    source=spot_source,
                )
            )
    else:
        have_30 = inputs.spot_volume_30d is not None
        have_prev = inputs.spot_volume_prev_30d is not None
        if have_30 and have_prev:
            states.append(
                FieldState(
                    field_id="spot_60d_qv",
                    status="OK",
                    fetched_at_ms=spot_known,
                    source=spot_source,
                )
            )
        elif (inputs.spot_daily_bars or 0) > 0:
            states.append(
                FieldState(
                    field_id="spot_60d_qv",
                    status="PARTIAL",
                    valid_count=float(inputs.spot_daily_bars or 0),
                    fetched_at_ms=spot_known,
                    reason_code=inputs.spot_reason or "SPOT_HISTORY_INCOMPLETE",
                    source=spot_source,
                )
            )
        else:
            states.append(
                FieldState(
                    field_id="spot_60d_qv",
                    status="UNAVAILABLE",
                    fetched_at_ms=spot_known,
                    reason_code=inputs.spot_reason or DATA_MISSING,
                    source=spot_source,
                )
            )
        states.append(
            _ok_state(
                "spot_24h_qv",
                inputs.spot_quote_volume_24h is not None,
                spot_known,
                spot_source,
                inputs.spot_reason,
            )
        )
    # Basis is real: OK from the verified premium, N/A with the confirmed
    # absence as proof, otherwise UNAVAILABLE -- never a NOT_WIRED fake.
    if not inputs.spot_applicable:
        states.append(
            FieldState(
                field_id="basis",
                status="NOT_APPLICABLE",
                fetched_at_ms=spot_known,
                reason_code=inputs.spot_reason,
                source=spot_source,
            )
        )
    elif inputs.premium is not None:
        states.append(
            FieldState(
                field_id="basis",
                status="OK",
                fetched_at_ms=spot_known,
                source=spot_source,
            )
        )
    else:
        states.append(
            FieldState(
                field_id="basis",
                status="UNAVAILABLE",
                fetched_at_ms=spot_known,
                reason_code=inputs.spot_reason or DATA_MISSING,
                source=spot_source,
            )
        )

    # -- book depth: two-sided honesty ----------------------------------------
    book_known = _known_of(inputs, "book")
    book_source = _source_of(inputs, "book", default="binance-book")
    have_bid = inputs.book_bid_notional_1pct is not None
    have_ask = inputs.book_ask_notional_1pct is not None
    if have_bid and have_ask:
        states.append(
            FieldState(
                field_id="book_depth",
                status="OK",
                fetched_at_ms=book_known,
                source=book_source,
            )
        )
    elif have_bid or have_ask:
        states.append(
            FieldState(
                field_id="book_depth",
                status="PARTIAL",
                valid_count=1,
                fetched_at_ms=book_known,
                reason_code=inputs.book_reason or PARTIAL_COVERAGE,
                source=book_source,
            )
        )
    else:
        states.append(
            FieldState(
                field_id="book_depth",
                status="UNAVAILABLE",
                fetched_at_ms=book_known,
                reason_code=inputs.book_reason or DATA_MISSING,
                source=book_source,
            )
        )

    # -- identity legs (binding resolved at the decision instant) -------------
    identity_ok = inputs.mapping_confidence in READY_IDENTITY_CONFIDENCE
    for field_id in ("canonical_mapping", "profile_basis"):
        states.append(
            FieldState(
                field_id=field_id,
                status="OK" if identity_ok else "UNAVAILABLE",
                fetched_at_ms=inputs.decision_as_of_ms,
                reason_code=None if identity_ok else IDENTITY_UNVERIFIED,
                source="shortlab-identity",
            )
        )
    return tuple(states)
