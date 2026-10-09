"""F02: observation wrapper, units and UTC decision windows (frozen owner).

Design A4.1/A4.2/A4.3, B16.1; plan F02 (AC02/AC03). This module owns
``ObservationMeta`` / ``Observed[T]``; consumers only import them.

- ``Observed.value`` is the legacy return object (``to_legacy`` returns the
  identical object, preserving type). ``meta`` follows A4.1; an unknown
  source time stays ``None`` and is never filled with the local clock.
- ``known_at_ms`` is the response-completion time. Cache hits return the
  identical ``Observed`` (original ``known_at``/``source_as_of``), never a
  copy stamped with the hit time.
- ``validate_observation`` rejects a ``known_at`` later than the decision
  cutoff (``CLOCK_SKEW``). Negative age (``cutoff - known_at < 0``) is
  reported as-is and rejected -- never clamped to zero to fake freshness.
- ``canonical_price`` / ``canonical_qty`` use ``Decimal`` only for scaling:
  price ``native / multiplier * fx``, quantity ``native * multiplier``.
  Nominal OI / quote-volume legs must never be routed through them; an
  unknown multiplier/FX yields ``None`` (callers attach ``UNIT_UNKNOWN``).
- Daily legs cover closed UTC days (``qv`` reads Binance raw index 7; a
  missing day is never zero-filled). OI-window ends must agree with the
  price window within one 5-minute period (``WINDOW_MISALIGNED``).

R03 (D03.2/D05.1/D18.2): receipt persistence bridge. ``observation_to_record``
writes the raw compatible value + full ``observations-v1`` meta (+
``repair-contract-v1`` marker) without restamping; ``observation_from_record``
restores the identical receipt (never "now fetched"). A legacy record without
a receipt (``known_at_ms`` None) is restored with ``reason_code=UNVERIFIED``
and can never prove freshness via :func:`validate_observation`.

Record dataclasses stay frozen in ``repository.py``; this module adds no
storage types.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Generic, TypeVar

T = TypeVar("T")

DAY_MS = 86_400_000

#: Source schema version stamped on F02 observations.
OBSERVATION_SCHEMA_VERSION = "observations-v1"

#: Reason codes owned by this module.
CLOCK_SKEW = "CLOCK_SKEW"
LEGACY_PROVENANCE_UNKNOWN = "LEGACY_PROVENANCE_UNKNOWN"
UNIT_UNKNOWN = "UNIT_UNKNOWN"
WINDOW_MISALIGNED = "WINDOW_MISALIGNED"

#: R03 (D03.2/D05.1/D18.2): receipt / archive reason codes.
#: ``UNVERIFIED`` marks a legacy record without a receipt (no known_at);
#: ``RECEIPT_ONLY`` marks an observation whose source carries no time (usable
#: only where explicitly allowed, never as realtime market time);
#: ``HISTORY_BOOTSTRAPPING`` marks a window without a verifiable old-regime
#: archive (history shown, no complete entry Gate).
UNVERIFIED = "UNVERIFIED"
RECEIPT_ONLY = "RECEIPT_ONLY"
HISTORY_BOOTSTRAPPING = "HISTORY_BOOTSTRAPPING"
FUNDING_SCHEDULE_UNKNOWN = "FUNDING_SCHEDULE_UNKNOWN"

#: Repair schema marker stamped on persisted market records (D18.2).
REPAIR_SCHEMA_VERSION = "repair-contract-v1"

#: Market observation kinds persisted via :func:`observation_to_record`
#: (D18.2 value_json/meta_json contract).
MARKET_OBSERVATION_KINDS = frozenset(
    {
        "TICKER",
        "MARK",
        "MARK_BAR_1H",
        "OI",
        "BOOK",
        "RULES",
        "FUNDING_INFO",
        "ACTIVATION_CHECK",
        "EVENT_FX",
        "BUDGET_COUNTER",
    }
)

#: OI-window / price-window agreement tolerance (one 5m period, A5.1/F02.1).
OI_PRICE_SYNC_TOLERANCE_MS = 5 * 60 * 1000

_OK_STATUSES = frozenset({"OK", "PARTIAL", "NOT_APPLICABLE", "UNAVAILABLE", "ERROR"})


class FutureKnownAtError(ValueError):
    """``known_at_ms`` is later than the decision cutoff (CLOCK_SKEW)."""


@dataclass(frozen=True)
class ObservationUnits:
    """Dimension record for an observation (A4.1 ``units``).

    ``price_unit`` / ``qty_unit`` name the native legs (e.g. ``"USDT"``,
    ``"BTC"``); ``quote_asset`` names the settlement quote; ``fx_source``
    and ``multiplier_source`` record provenance (``None`` = unverified).
    """

    price_unit: str | None = None
    qty_unit: str | None = None
    quote_asset: str | None = None
    fx_source: str | None = None
    multiplier_source: str | None = None


@dataclass(frozen=True)
class ObservationMeta:
    """Point-in-time envelope for one observation (design A4.1)."""

    status: str = "OK"
    source: str = ""
    source_schema_version: str | None = None
    reason_code: str | None = None
    source_as_of_ms: int | None = None
    fetched_at_ms: int | None = None
    known_at_ms: int | None = None
    window_start_ms: int | None = None
    window_end_ms: int | None = None
    complete: bool | None = None
    coverage_fraction: float | None = None
    units: ObservationUnits | None = None
    identity_snapshot_id: str | None = None
    raw_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _OK_STATUSES:
            raise ValueError(f"unknown observation status: {self.status!r}")


@dataclass(frozen=True)
class Observed(Generic[T]):
    """Legacy value plus its PIT envelope.

    ``value`` is the original return object (never a copy); ``meta``
    carries the A4.1 envelope.
    """

    value: T
    meta: ObservationMeta


def to_legacy(observed: Observed[T]) -> T:
    """Return the wrapped legacy value (identical object and type)."""
    return observed.value


def observation_age_ms(observed: Observed[Any], cutoff_ms: int) -> int | None:
    """``cutoff_ms - known_at_ms`` (may be negative; never clamped).

    A negative age means the observation arrived after the decision cutoff
    and must be rejected by :func:`validate_observation`, not rounded to
    zero to fake freshness. ``None`` when ``known_at_ms`` is unknown.
    """
    known = observed.meta.known_at_ms
    if known is None:
        return None
    return int(cutoff_ms) - int(known)


def validate_observation(
    observed: Observed[T], cutoff_ms: int, max_future_skew_sec: int = 2
) -> Observed[T]:
    """Check an observation against a decision cutoff (A4.3).

    Returns the identical ``Observed`` when usable; raises otherwise:

    - ``known_at_ms`` missing -> ``ValueError`` (``LEGACY_PROVENANCE_UNKNOWN``):
      untraceable provenance can never prove point-in-time freshness.
    - ``known_at_ms > cutoff_ms`` -> :class:`FutureKnownAtError`
      (``CLOCK_SKEW``): data from after the decision moment must not score.
    - ``source_as_of_ms`` beyond ``fetched_at_ms`` plus ``max_future_skew_sec``
      -> ``ValueError`` (``CLOCK_SKEW``): the source clock is implausible.
    """
    cutoff = int(cutoff_ms)
    meta = observed.meta
    known = meta.known_at_ms
    if known is None:
        raise ValueError(
            f"{LEGACY_PROVENANCE_UNKNOWN}: known_at_ms is None; "
            "provenance cannot prove point-in-time freshness"
        )
    if int(known) > cutoff:
        raise FutureKnownAtError(
            f"{CLOCK_SKEW}: known_at_ms={known} is after cutoff_ms={cutoff}"
        )
    source_as_of = meta.source_as_of_ms
    fetched = meta.fetched_at_ms
    if source_as_of is not None and fetched is not None:
        skew_ms = int(max_future_skew_sec) * 1000
        if int(source_as_of) > int(fetched) + skew_ms:
            raise ValueError(
                f"{CLOCK_SKEW}: source_as_of_ms={source_as_of} exceeds "
                f"fetched_at_ms={fetched} by more than {max_future_skew_sec}s"
            )
    return observed


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None


def canonical_price(
    native_price: Decimal | str | int | float | None,
    multiplier: Decimal | str | int | float | None,
    fx: Decimal | str | int | float | None,
) -> Decimal | None:
    """Canonical per-coin USD price: ``native / multiplier * fx`` (Decimal).

    ``None`` when the multiplier or FX is unknown/unverified (``<= 0`` or
    unparseable) -- callers surface ``UNIT_UNKNOWN`` instead of guessing.
    Nominal OI / quote-volume legs must never be passed through here.
    """
    native = _to_decimal(native_price)
    mult = _to_decimal(multiplier)
    rate = _to_decimal(fx)
    if native is None or mult is None or rate is None:
        return None
    if mult <= 0 or rate <= 0:
        return None
    try:
        return native / mult * rate
    except (InvalidOperation, ZeroDivisionError):
        return None


def canonical_qty(
    native_qty: Decimal | str | int | float | None,
    multiplier: Decimal | str | int | float | None,
) -> Decimal | None:
    """Canonical coin quantity: ``native * multiplier`` (Decimal).

    ``None`` when the multiplier is unknown/unverified. Quote-notional
    amounts (USD nominals) must never be passed through here.
    """
    qty = _to_decimal(native_qty)
    mult = _to_decimal(multiplier)
    if qty is None or mult is None:
        return None
    if mult <= 0:
        return None
    try:
        return qty * mult
    except InvalidOperation:
        return None


def resolve_unit_or_null(
    value: Any, *, expected: str | None, actual: str | None = None
) -> dict[str, Any]:
    """Return ``{value, reason_code}``: null + ``UNIT_UNKNOWN`` on mismatch.

    ``actual`` defaults to ``expected`` (verified leg); a missing/blank
    unit or a unit that does not match ``expected`` yields ``None`` with
    the machine reason instead of a guessed conversion.
    """
    want = str(expected).strip().upper() if expected is not None else ""
    got_raw = expected if actual is None else actual
    got = str(got_raw).strip().upper() if got_raw is not None else ""
    if value is None or not want or not got or got != want:
        return {"value": None, "reason_code": UNIT_UNKNOWN}
    return {"value": value, "reason_code": None}


def utc_closed_day_window(as_of_ms: int, days: int) -> tuple[int, int]:
    """``[start, end)`` of the ``days`` closed UTC days ending at ``as_of``.

    ``end`` is the UTC midnight at/before ``as_of_ms``; the still-open
    current UTC day is always excluded. Both ends derive from the same
    frozen cutoff so a cross-day freeze realigns them together.
    """
    end = (int(as_of_ms) // DAY_MS) * DAY_MS
    return (end - int(days) * DAY_MS, end)


def oi_price_window_usable(
    oi_start_ms: int | None,
    oi_end_ms: int | None,
    price_start_ms: int | None,
    price_end_ms: int | None,
    tolerance_ms: int = OI_PRICE_SYNC_TOLERANCE_MS,
) -> tuple[bool, str | None]:
    """Check OI-window ends against the price window (A5.1/F02.1).

    Both ends must agree within ``tolerance_ms`` (default one 5m period).
    Unknown ends are unusable. Returns ``(True, None)`` or
    ``(False, WINDOW_MISALIGNED)`` -- never a partial silent reuse.
    """
    try:
        oi_s = None if oi_start_ms is None else int(oi_start_ms)
        oi_e = None if oi_end_ms is None else int(oi_end_ms)
        px_s = None if price_start_ms is None else int(price_start_ms)
        px_e = None if price_end_ms is None else int(price_end_ms)
        tol = int(tolerance_ms)
    except (TypeError, ValueError):
        return False, WINDOW_MISALIGNED
    if oi_s is None or oi_e is None or px_s is None or px_e is None:
        return False, WINDOW_MISALIGNED
    if abs(oi_s - px_s) > tol or abs(oi_e - px_e) > tol:
        return False, WINDOW_MISALIGNED
    return True, None


def make_observation(
    value: T,
    *,
    source: str,
    source_as_of_ms: int | None = None,
    fetched_at_ms: int | None = None,
    known_at_ms: int | None = None,
    status: str = "OK",
    source_schema_version: str | None = OBSERVATION_SCHEMA_VERSION,
    reason_code: str | None = None,
    window_start_ms: int | None = None,
    window_end_ms: int | None = None,
    complete: bool | None = None,
    coverage_fraction: float | None = None,
    units: ObservationUnits | None = None,
    identity_snapshot_id: str | None = None,
    raw_snapshot_id: str | None = None,
) -> Observed[T]:
    """Build an ``Observed``; unknown source time stays ``None``.

    Callers pass the response-completion time as both ``fetched_at_ms``
    and ``known_at_ms`` for fresh fetches; cache-hit paths must reuse the
    stored ``Observed`` instead of calling this again.
    """
    return Observed(
        value=value,
        meta=ObservationMeta(
            status=status,
            source=source,
            source_schema_version=source_schema_version,
            reason_code=reason_code,
            source_as_of_ms=None if source_as_of_ms is None else int(source_as_of_ms),
            fetched_at_ms=None if fetched_at_ms is None else int(fetched_at_ms),
            known_at_ms=None if known_at_ms is None else int(known_at_ms),
            window_start_ms=window_start_ms,
            window_end_ms=window_end_ms,
            complete=complete,
            coverage_fraction=coverage_fraction,
            units=units,
            identity_snapshot_id=identity_snapshot_id,
            raw_snapshot_id=raw_snapshot_id,
        ),
    )


# ---------------------------------------------------------------------------
# R03 receipt persistence bridge (D03.2/D18.2).
#
# ``value_json`` is the original compatible value (never a re-parsed copy);
# ``meta_json`` is the full ``observations-v1`` meta plus
# ``repair_schema_version=repair-contract-v1``. Restart restores the identical
# receipt (``known_at``/``source_as_of``/``fetched_at``); a complete archive is
# never patched with "now fetched". A legacy record without a receipt keeps
# ``known_at_ms=None`` and is explicitly marked ``reason_code=UNVERIFIED``.
# ---------------------------------------------------------------------------


def _canonical_bytes(value: Any) -> bytes:
    try:
        text = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
            default=str,
        )
    except (TypeError, ValueError):
        text = json.dumps(str(value), ensure_ascii=False, allow_nan=False)
    return text.encode("utf-8")


def _meta_to_dict(meta: ObservationMeta) -> dict[str, Any]:
    units = meta.units
    if isinstance(units, ObservationUnits):
        units_dict: dict[str, Any] | None = {
            "price_unit": units.price_unit,
            "qty_unit": units.qty_unit,
            "quote_asset": units.quote_asset,
            "fx_source": units.fx_source,
            "multiplier_source": units.multiplier_source,
        }
    elif isinstance(units, dict):
        units_dict = dict(units)
    else:
        units_dict = None
    return {
        "status": meta.status,
        "source": meta.source,
        "source_schema_version": meta.source_schema_version,
        "reason_code": meta.reason_code,
        "source_as_of_ms": meta.source_as_of_ms,
        "fetched_at_ms": meta.fetched_at_ms,
        "known_at_ms": meta.known_at_ms,
        "window_start_ms": meta.window_start_ms,
        "window_end_ms": meta.window_end_ms,
        "complete": meta.complete,
        "coverage_fraction": meta.coverage_fraction,
        "units": units_dict,
        "identity_snapshot_id": meta.identity_snapshot_id,
        "raw_snapshot_id": meta.raw_snapshot_id,
        "repair_schema_version": REPAIR_SCHEMA_VERSION,
    }


def _meta_from_dict(data: Any) -> ObservationMeta:
    if not isinstance(data, dict):
        data = {}
    units_raw = data.get("units")
    units: ObservationUnits | None = None
    if isinstance(units_raw, dict):
        try:
            units = ObservationUnits(
                price_unit=units_raw.get("price_unit"),
                qty_unit=units_raw.get("qty_unit"),
                quote_asset=units_raw.get("quote_asset"),
                fx_source=units_raw.get("fx_source"),
                multiplier_source=units_raw.get("multiplier_source"),
            )
        except Exception:
            units = None
    status = data.get("status", "OK")
    if status not in _OK_STATUSES:
        status = "OK"
    return ObservationMeta(
        status=status,
        source=str(data.get("source", "")),
        source_schema_version=data.get("source_schema_version", OBSERVATION_SCHEMA_VERSION),
        reason_code=data.get("reason_code"),
        source_as_of_ms=data.get("source_as_of_ms"),
        fetched_at_ms=data.get("fetched_at_ms"),
        known_at_ms=data.get("known_at_ms"),
        window_start_ms=data.get("window_start_ms"),
        window_end_ms=data.get("window_end_ms"),
        complete=data.get("complete"),
        coverage_fraction=data.get("coverage_fraction"),
        units=units,
        identity_snapshot_id=data.get("identity_snapshot_id"),
        raw_snapshot_id=data.get("raw_snapshot_id"),
    )


def observation_to_record(
    observed: Observed[Any],
    *,
    kind: str,
    symbol: str,
    observation_id: str | None = None,
) -> dict[str, Any]:
    """Persist an ``Observed`` without restamping its receipt (R03/D18.2).

    ``value_json`` keeps the original compatible value; ``meta_json`` keeps
    the full ``observations-v1`` envelope plus ``repair_schema_version``.
    ``raw_sha256`` is the canonical-JSON SHA256 of the value (content
    integrity, not an exchange signature).
    """
    meta = observed.meta
    value = observed.value
    raw_sha256 = hashlib.sha256(_canonical_bytes(value)).hexdigest()
    known = meta.known_at_ms
    source_as_of = meta.source_as_of_ms
    if observation_id is None:
        observation_id = f"{symbol}:{kind}:{known}:{source_as_of}"
    return {
        "observation_id": str(observation_id),
        "symbol": str(symbol),
        "kind": str(kind),
        "source_as_of_ms": source_as_of,
        "known_at_ms": known,
        "value_json": value,
        "meta_json": _meta_to_dict(meta),
        "raw_sha256": raw_sha256,
    }


def observation_from_record(record: Any) -> Observed[Any]:
    """Restore an ``Observed`` with its original receipt (R03/D03.2).

    The stored ``known_at``/``source_as_of``/``fetched_at`` are returned
    verbatim -- never replaced with the restore (now) time. A legacy record
    without a receipt (``known_at_ms`` None) is marked
    ``reason_code=UNVERIFIED`` instead of being silently trusted.
    """
    if not isinstance(record, dict):
        raise ValueError("observation record must be a mapping")
    value = record.get("value_json", record.get("value"))
    meta_json = record.get("meta_json", {})
    if not isinstance(meta_json, dict):
        meta_json = {}
    # Top-level columns win when meta_json omits them (legacy rows).
    for key in ("source_as_of_ms", "known_at_ms"):
        if meta_json.get(key) is None and record.get(key) is not None:
            meta_json = dict(meta_json)
            meta_json[key] = record.get(key)
    if meta_json.get("fetched_at_ms") is None and record.get("known_at_ms") is not None:
        # Very old rows only carry known_at; fetched_at mirrors it (receipt).
        meta_json = dict(meta_json)
        meta_json["fetched_at_ms"] = record.get("known_at_ms")
    meta = _meta_from_dict(meta_json)
    if meta.known_at_ms is None and meta.reason_code is None:
        # Legacy without receipt: explicit UNVERIFIED, never a fresh receipt.
        try:
            meta = ObservationMeta(
                status=meta.status,
                source=meta.source,
                source_schema_version=meta.source_schema_version,
                reason_code=UNVERIFIED,
                source_as_of_ms=meta.source_as_of_ms,
                fetched_at_ms=meta.fetched_at_ms,
                known_at_ms=None,
                window_start_ms=meta.window_start_ms,
                window_end_ms=meta.window_end_ms,
                complete=meta.complete,
                coverage_fraction=meta.coverage_fraction,
                units=meta.units,
                identity_snapshot_id=meta.identity_snapshot_id,
                raw_snapshot_id=meta.raw_snapshot_id,
            )
        except Exception:
            pass
    return Observed(value=value, meta=meta)


def is_legacy_without_receipt(record_or_observed: Any) -> bool:
    """True when a record/observation carries no receipt (``known_at`` None)."""
    if isinstance(record_or_observed, Observed):
        return record_or_observed.meta.known_at_ms is None
    if isinstance(record_or_observed, dict):
        if record_or_observed.get("known_at_ms") is not None:
            return False
        meta = record_or_observed.get("meta_json")
        if isinstance(meta, dict) and meta.get("known_at_ms") is not None:
            return False
        return True
    return False
