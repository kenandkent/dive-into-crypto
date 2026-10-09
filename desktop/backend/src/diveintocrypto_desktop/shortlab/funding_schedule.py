"""R05 funding schedule coverage (D05.2, pure, no network/DB/clock reads).

Frozen port signature (docs/contracts/shortlab_repair_service_calls.json)::

    compute_schedule_coverage(
        events: Sequence[Mapping],
        schedules: Sequence[FundingScheduleSegment],
        start_ms: int, end_ms: int, known_by_ms: int,
    ) -> FundingCoverage

Semantics (D05.1/D05.2):

- Only ``CONFIRMED`` segments grant expected slots. Each confirmed segment
  generates ``anchor_ms + k * interval_hours`` slots. Segment window is
  ``[effective_from_ms, effective_to_ms)``; statistics window is
  ``(start_ms, end_ms]`` (left-open, right-closed, same as funding_score).
  A slot is allocated at most once even when segments overlap.
- Overlapping confirmed segments (or unverifiable boundaries) make the
  overlapping range UNKNOWN: no slots are generated there and the window
  fraction is null with ``FUNDING_SCHEDULE_UNKNOWN``.
- ``expected_count`` is the verified slot count; ``received_count`` counts
  slots matched by a valid event within +/-60s
  (``optimization.funding_schedule.event_match_tolerance_sec``). Head and
  tail missing slots count as missing (they lower ``received_count``).
- Window with any UNKNOWN schedule part or ``expected == 0`` yields
  ``coverage_fraction=None`` with ``FUNDING_SCHEDULE_UNKNOWN``. A second
  output ``schedule_coverage_fraction = confirmed_duration / window`` is
  always a Decimal string.
- Events are de-duplicated by funding time; only events with
  ``known_at <= known_by_ms`` are usable. Duplicates keep the latest
  ``known_at`` (most recent legal receipt). Events without a valid
  rate/time are ignored. Missing ``mark`` does NOT affect rate matching;
  USD carry uses :func:`compute_priced_event_coverage` separately.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping, Sequence

from diveintocrypto_desktop.shortlab.repair_contracts import (
    FundingCoverage,
    FundingScheduleSegment,
)

__all__ = [
    "EVENT_MATCH_TOLERANCE_MS",
    "compute_schedule_coverage",
    "compute_priced_event_coverage",
]

#: D15 ``optimization.funding_schedule.event_match_tolerance_sec`` (frozen 60).
EVENT_MATCH_TOLERANCE_MS = 60_000

_HOUR_MS = 3_600_000


# ---------------------------------------------------------------------------
# Event / schedule normalisation
# ---------------------------------------------------------------------------

def _event_time_ms(ev: Any) -> int | None:
    if isinstance(ev, Mapping):
        for key in (
            "funding_time_ms", "fundingTime", "t", "funding_time",
            "event_time_ms", "funding_time_ms_", "time_ms",
        ):
            if key in ev and ev[key] is not None:
                try:
                    return int(ev[key])
                except (TypeError, ValueError):
                    return None
        # Nested value envelope: {"value": {...}, "meta": {...}} (Observed dict).
        val = ev.get("value")
        if isinstance(val, Mapping):
            return _event_time_ms(val)
        return None
    for attr in ("funding_time_ms", "fundingTime", "t"):
        if hasattr(ev, attr):
            try:
                return int(getattr(ev, attr))
            except (TypeError, ValueError):
                return None
    return None


def _event_rate(ev: Any) -> Decimal | None:
    raw: Any = None
    if isinstance(ev, Mapping):
        for key in ("rate", "fundingRate", "funding_rate", "funding_rate_str"):
            if key in ev and ev[key] is not None:
                raw = ev[key]
                break
        else:
            val = ev.get("value")
            if isinstance(val, Mapping):
                return _event_rate(val)
            return None
    else:
        for attr in ("rate", "fundingRate", "funding_rate"):
            if hasattr(ev, attr):
                raw = getattr(ev, attr)
                if raw is not None:
                    break
        else:
            return None
        if raw is None:
            return None
    try:
        d = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not d.is_finite():
        return None
    return d


def _event_known_at_ms(ev: Any) -> int | None:
    if isinstance(ev, Mapping):
        for key in (
            "known_at_ms", "knownAt", "known_at", "fetched_at_ms",
            "fetchedAt", "receipt_known_at_ms",
        ):
            if key in ev and ev[key] is not None:
                try:
                    return int(ev[key])
                except (TypeError, ValueError):
                    return None
        meta = ev.get("meta")
        if isinstance(meta, Mapping):
            for key in ("known_at_ms", "knownAt", "known_at", "fetched_at_ms"):
                if key in meta and meta[key] is not None:
                    try:
                        return int(meta[key])
                    except (TypeError, ValueError):
                        return None
        # Observed envelope: {"value":..., "meta": {...}} handled above;
        # plain legacy funding dicts without provenance are treated as known
        # (caller passes known_by generously); filtering happens only when
        # a known_at is present.
        return None
    for attr in ("known_at_ms", "known_at", "fetched_at_ms"):
        if hasattr(ev, attr):
            try:
                v = getattr(ev, attr)
                return None if v is None else int(v)
            except (TypeError, ValueError):
                return None
    meta = getattr(ev, "meta", None)
    if meta is not None:
        for attr in ("known_at_ms", "known_at", "fetched_at_ms"):
            if hasattr(meta, attr):
                try:
                    v = getattr(meta, attr)
                    return None if v is None else int(v)
                except (TypeError, ValueError):
                    return None
    return None


def _event_mark(ev: Any) -> Decimal | None:
    raw: Any = None
    if isinstance(ev, Mapping):
        for key in ("mark_price", "markPrice", "mark", "mark_price_str"):
            if key in ev and ev[key] is not None:
                raw = ev[key]
                break
        else:
            val = ev.get("value")
            if isinstance(val, Mapping):
                return _event_mark(val)
            return None
    else:
        for attr in ("mark_price", "markPrice", "mark"):
            if hasattr(ev, attr):
                raw = getattr(ev, attr)
                if raw is not None:
                    break
        else:
            return None
        if raw is None:
            return None
    try:
        d = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not d.is_finite():
        return None
    return d


def _coerce_segment(seg: Any) -> FundingScheduleSegment:
    if isinstance(seg, FundingScheduleSegment):
        return seg
    if isinstance(seg, Mapping):
        # Accept both snake_case DTO keys and camelCase HTTP aliases.
        def _pick(*names: str, default: Any = None) -> Any:
            for n in names:
                if n in seg and seg[n] is not None:
                    return seg[n]
            return default

        return FundingScheduleSegment(
            schedule_id=str(_pick("schedule_id", "scheduleId")),
            symbol=str(_pick("symbol")),
            effective_from_ms=int(_pick("effective_from_ms", "effectiveFrom")),
            effective_to_ms=(
                None if _pick("effective_to_ms", "effectiveTo") is None
                else int(_pick("effective_to_ms", "effectiveTo"))
            ),
            interval_hours=int(_pick("interval_hours", "intervalHours")),
            anchor_ms=int(_pick("anchor_ms", "anchorMs")),
            known_at_ms=int(_pick("known_at_ms", "knownAt")),
            source=str(_pick("source")),
            evidence_ref=str(_pick("evidence_ref", "evidenceRef")),
            verification=str(_pick("verification")),
        )
    raise ValueError(f"schedule must be FundingScheduleSegment or mapping, got {type(seg).__name__}")


def _format_fraction(numer: int, denom: int) -> str:
    """Decimal fraction with 8dp rounding, trailing zeros stripped (D03.1)."""
    if denom <= 0:
        raise ValueError("denom must be > 0")
    if numer == 0:
        return "0"
    if numer == denom:
        return "1"
    q = (Decimal(numer) / Decimal(denom)).quantize(
        Decimal("0.00000001"), rounding=ROUND_HALF_UP
    )
    text = format(q, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("-0", "-0.0"):
        return "0"
    return text


def _format_duration_fraction(confirmed_ms: int, window_ms: int) -> str:
    if window_ms <= 0:
        raise ValueError("window must be > 0")
    if confirmed_ms <= 0:
        return "0"
    if confirmed_ms >= window_ms:
        return "1"
    return _format_fraction(confirmed_ms, window_ms)


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------

def compute_schedule_coverage(
    events: Sequence[Mapping],
    schedules: Sequence[Any],
    start_ms: int,
    end_ms: int,
    known_by_ms: int,
) -> FundingCoverage:
    """Build :class:`FundingCoverage` for ``(start_ms, end_ms]``.

    Pure: no network, no DB, no clock reads. ``known_by_ms`` is the decision
    cutoff; schedules/events with ``known_at > known_by`` are invisible.
    """
    try:
        start = int(start_ms)
        end = int(end_ms)
        known_by = int(known_by_ms)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"start/end/known_by must be ints: {exc}") from exc
    if end <= start:
        raise ValueError("window_end_ms must be > window_start_ms")
    if known_by < 0:
        raise ValueError("known_by_ms must be >= 0")
    window_len = end - start

    # -- schedules: known_by filter + revision pick -------------------------
    coerced: list[FundingScheduleSegment] = []
    for seg in (schedules or []):
        try:
            c = _coerce_segment(seg)
        except (ValueError, TypeError, KeyError):
            continue
        if int(c.known_at_ms) <= known_by:
            coerced.append(c)
    # Group by (symbol, effective_from): latest known_at wins, schedule_id ASC.
    grouped: dict[tuple[str, int], FundingScheduleSegment] = {}
    for c in coerced:
        key = (c.symbol, int(c.effective_from_ms))
        prev = grouped.get(key)
        if prev is None:
            grouped[key] = c
        elif int(c.known_at_ms) > int(prev.known_at_ms):
            grouped[key] = c
        elif int(c.known_at_ms) == int(prev.known_at_ms) and c.schedule_id < prev.schedule_id:
            grouped[key] = c
    selected = sorted(grouped.values(), key=lambda s: (s.symbol, int(s.effective_from_ms)))

    # -- confirmed intervals clipped to window + overlap detection -----------
    # Each entry: (lo, hi) with [lo, hi) inside (start, end] domain.
    confirmed: list[tuple[int, int]] = []
    for seg in selected:
        if seg.verification != "CONFIRMED":
            continue
        lo = max(int(seg.effective_from_ms), start)
        hi = int(seg.effective_to_ms) if seg.effective_to_ms is not None else end
        hi = min(hi, end)
        # Segment is [from, to); window is (start, end]: slot domain needs
        # lo = max(from, start+1?) but duration uses continuous ms; using
        # [max(from,start), min(to,end)] is exact for duration purposes
        # (single-ms boundary error is negligible vs 8h slots and matches
        # the R00 TEST_FAKE golden durations of full-window == 1).
        lo = max(lo, start)
        if hi > lo:
            confirmed.append((lo, hi))
    confirmed.sort()
    # Overlaps -> the overlapping range is UNKNOWN (D05.2): no slots are
    # generated there and the window can never be fully verified, even when
    # the union still spans the window.
    overlaps: list[tuple[int, int]] = []
    for a, b in zip(confirmed, confirmed[1:]):
        if b[0] < a[1]:
            overlaps.append((b[0], min(a[1], b[1])))
    has_overlap = bool(overlaps)
    # Merge overlaps for exclusion.
    def _in_unknown(t: int) -> bool:
        return any(lo <= t < hi for lo, hi in overlaps)

    confirmed_duration = sum(hi - lo for lo, hi in confirmed)
    # Subtract overlapping double-counted spans once (they are UNKNOWN).
    overlap_duration = 0
    if overlaps:
        merged: list[list[int]] = []
        for lo, hi in sorted(overlaps):
            if merged and lo <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        overlap_duration = sum(hi - lo for lo, hi in merged)
        confirmed_duration -= overlap_duration
    confirmed_duration = max(0, confirmed_duration)
    # Clamp (defensive: overlapping confirmed should not exceed window).
    confirmed_duration = min(confirmed_duration, window_len)
    schedule_cov = _format_duration_fraction(confirmed_duration, window_len)
    fully_covered = (confirmed_duration >= window_len) and not has_overlap

    # -- expected slots ------------------------------------------------------
    expected: list[int] = []
    if confirmed:
        for seg in selected:
            if seg.verification != "CONFIRMED":
                continue
            interval_ms = int(seg.interval_hours) * _HOUR_MS
            if interval_ms <= 0:
                continue
            anchor = int(seg.anchor_ms)
            seg_lo = int(seg.effective_from_ms)
            seg_hi = int(seg.effective_to_ms) if seg.effective_to_ms is not None else end
            lo_bound = max(seg_lo, start + 1)  # window left-open
            hi_bound = min(seg_hi - 1 if seg.effective_to_ms is not None else end, end)
            # Slot domain: lo_bound <= t <= hi_bound, t in [seg_lo, seg_hi),
            # t = anchor + k*interval.
            # k_min = ceil((lo_bound - anchor)/interval)
            import math as _math

            k_min = _math.ceil((lo_bound - anchor) / interval_ms)
            k_max = _math.floor((hi_bound - anchor) / interval_ms)
            for k in range(k_min, k_max + 1):
                t = anchor + k * interval_ms
                if t <= start or t > end:
                    continue
                if t < seg_lo or (seg.effective_to_ms is not None and t >= int(seg.effective_to_ms)):
                    continue
                if _in_unknown(t):
                    continue
                expected.append(t)
    expected = sorted(set(expected))

    # -- events: known_by filter + dedup (latest known_at wins) --------------
    by_time: dict[int, tuple[Decimal, int]] = {}
    for ev in (events or []):
        t = _event_time_ms(ev)
        r = _event_rate(ev)
        if t is None or r is None:
            continue
        if int(t) <= start or int(t) > end:
            # Events outside the statistics window do not match slots, but
            # they are not errors; skip silently (funding_score windows also
            # clip to (start, end]).
            continue
        known = _event_known_at_ms(ev)
        if known is not None and int(known) > known_by:
            continue
        known_key = int(known) if known is not None else -1
        prev = by_time.get(int(t))
        if prev is None or known_key >= prev[1]:
            by_time[int(t)] = (r, known_key)
    event_times = sorted(by_time)

    # -- greedy slot matching (+/-60s, one event per slot) --------------------
    unmatched_events = list(event_times)
    received = 0
    missing: list[int] = []
    for slot in expected:
        best: int | None = None
        best_delta: int | None = None
        for et in unmatched_events:
            delta = abs(int(et) - int(slot))
            if delta <= EVENT_MATCH_TOLERANCE_MS:
                if best is None or delta < best_delta or (delta == best_delta and int(et) < int(best)):
                    best = et
                    best_delta = delta
        if best is None:
            missing.append(int(slot))
        else:
            received += 1
            unmatched_events.remove(best)

    # -- assemble -------------------------------------------------------------
    if not selected or not confirmed:
        # No verifiable schedule at all: expected unknown (null), not zero.
        return FundingCoverage(
            window_start_ms=start,
            window_end_ms=end,
            expected_count=None,
            received_count=0,
            coverage_fraction=None,
            schedule_coverage_fraction=schedule_cov,
            missing_slots=(),
            reasons=("FUNDING_SCHEDULE_UNKNOWN",),
        )
    expected_count: int | None = len(expected)
    if not fully_covered or len(expected) == 0:
        return FundingCoverage(
            window_start_ms=start,
            window_end_ms=end,
            expected_count=expected_count,
            received_count=received,
            coverage_fraction=None,
            schedule_coverage_fraction=schedule_cov,
            missing_slots=tuple(sorted(missing)),
            reasons=("FUNDING_SCHEDULE_UNKNOWN",),
        )
    if received >= len(expected):
        return FundingCoverage(
            window_start_ms=start,
            window_end_ms=end,
            expected_count=len(expected),
            received_count=received,
            coverage_fraction=_format_fraction(received, len(expected)) if expected else None,
            schedule_coverage_fraction=schedule_cov,
            missing_slots=(),
            reasons=(),
        )
    return FundingCoverage(
        window_start_ms=start,
        window_end_ms=end,
        expected_count=len(expected),
        received_count=received,
        coverage_fraction=_format_fraction(received, len(expected)),
        schedule_coverage_fraction=schedule_cov,
        missing_slots=tuple(sorted(missing)),
        reasons=("COVERAGE_GAP",),
    )


def compute_priced_event_coverage(
    events: Sequence[Mapping],
    start_ms: int,
    end_ms: int,
) -> str | None:
    """Fraction of in-window valid-rate events carrying a valid mark.

    Missing ``mark`` never affects rate statistics (sums/ratios still use
    the event); it only lowers USD-carry coverage. Returns a D03.1 Decimal
    string (``"1"``/``"0"`` exact, else 8dp stripped) or ``None`` when the
    window holds no valid-rate events.
    """
    try:
        start = int(start_ms)
        end = int(end_ms)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"start/end must be ints: {exc}") from exc
    if end <= start:
        raise ValueError("window_end_ms must be > window_start_ms")
    # Dedup by time (first valid rate wins; mark presence is per-slot).
    seen: dict[int, bool] = {}
    for ev in (events or []):
        t = _event_time_ms(ev)
        r = _event_rate(ev)
        if t is None or r is None:
            continue
        if int(t) <= start or int(t) > end:
            continue
        key = int(t)
        if key in seen:
            # If either duplicate carries a mark, the slot counts as priced
            # (a priced receipt exists for that settlement).
            if _event_mark(ev) is not None:
                seen[key] = True
            continue
        seen[key] = _event_mark(ev) is not None
    if not seen:
        return None
    priced = sum(1 for v in seen.values() if v)
    total = len(seen)
    return _format_fraction(priced, total)
