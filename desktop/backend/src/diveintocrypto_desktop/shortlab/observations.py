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

Record dataclasses stay frozen in ``repository.py``; this module adds no
storage types.
"""

from __future__ import annotations

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
