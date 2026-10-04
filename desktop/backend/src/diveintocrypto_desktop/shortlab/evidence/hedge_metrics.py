"""H10 hedge evidence aggregation (design B39.2/B39.3 + B32.13 filters).

Pure domain module: reads only archived FCS snapshots plus H01
``sl_hedge_outcome`` rows (never the directional outcome table, never the
live manual ledger). ``H08`` lazy-imports :func:`hedge_summary` and maps
the single-bucket :class:`HedgeEvidenceSummary` into the
``GET /api/short/hedge/evidence/summary`` response; this module never
touches the router.

Contract::

    async hedge_summary(filters, repository, policy=None) -> HedgeEvidenceSummary

``filters`` follows B32.13 (all optional): ``strategy``
(``ABSOLUTE_100``/``RELATIVE_75``/``RELATIVE_50``/``RELATIVE_25``),
``horizon`` (``7D``/``30D``/``90D`` or ``7``/``30``/``90``),
``symbol``, ``venue``, ``history_class``/``historyClass``,
``fcs_version``/``fcsVersion``, ``hedge_evidence_version``,
``cost_config_hash``/``costConfigHash``, ``start_ms``/``startMs``,
``end_ms``/``endMs`` (FCS ``as_of`` window), ``now_ms`` (grading clock
override for tests). Unknown enums never raise here -- they simply match
nothing (H08 maps them to 422 at the HTTP layer).

Semantics:

- default window is unbounded when no explicit ``start_ms``/``end_ms``
  is given (legacy offline fixtures stay visible); an explicit window
  filters FCS ``as_of_ms`` inclusively.
- default versions are the current ones (FCS ``fcs_v1`` is informational;
  cost/evidence default to the current hashes); an explicit old
  cost/version selects that bucket alone and is never mixed.
- ``sample_count`` counts FCS snapshots in the window; ``complete_count``
  / ``censored_count`` count the matching terminal outcomes;
  ``PENDING`` (not due, no row) and ``UNAVAILABLE`` (due, no row or
  explicit row) stay in the sample but outside the two counts.
- ``avg_net_return`` / ``median_net_return`` average only ``COMPLETE``
  outcomes with a non-null decimal ``net_return`` (capital denominator).
- user-ledger fills (``sl_hedge_fill_event`` via ``Ledger``) are never
  merged into the fixed-strategy buckets.
"""

from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Mapping

__all__ = ["hedge_summary", "parse_hedge_filters"]


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_hedge_filters(filters: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalise B32.13 query params to a plain selection dict."""
    filt = dict(filters or {})

    def _first(*names: str) -> Any:
        for name in names:
            if name in filt and filt[name] is not None:
                return filt[name]
        return None

    raw_strategy = _first("strategy", "strategyName")
    strategy: str | None = None
    if raw_strategy is not None:
        strategy = str(raw_strategy).strip().upper()
        if strategy not in (
            "ABSOLUTE_100", "RELATIVE_75", "RELATIVE_50", "RELATIVE_25",
        ):
            strategy = None  # unknown enum matches nothing downstream

    raw_horizon = _first("horizon", "horizons", "horizon_days", "horizonDays")
    horizon_days: int | None = None
    if raw_horizon is not None:
        if isinstance(raw_horizon, (list, tuple)) and raw_horizon:
            raw_horizon = raw_horizon[0]
        text = str(raw_horizon).strip().upper()
        if text.endswith("D"):
            text = text[:-1]
        try:
            days = int(text)
            if days in (7, 30, 90):
                horizon_days = days
        except (TypeError, ValueError):
            horizon_days = None

    return {
        "strategy": strategy or "ABSOLUTE_100",
        "horizon_days": horizon_days if horizon_days is not None else 30,
        "symbol": _str_or_none(_first("symbol", "Symbol")),
        "venue": _str_or_none(_first("venue", "Venue")),
        "history_class": _str_or_none(
            _first("history_class", "historyClass", "historyClassName")
        ),
        "fcs_version": _str_or_none(_first("fcs_version", "fcsVersion")),
        "cost_config_hash": _str_or_none(
            _first("cost_config_hash", "costConfigHash")
        ),
        "evidence_version": _str_or_none(
            _first("evidence_version", "evidenceVersion",
                   "hedge_evidence_version", "hedgeEvidenceVersion")
        ),
        "start_ms": _int_or_none(
            _first("start_ms", "startMs", "as_of_from_ms", "asOfFromMs")
        ),
        "end_ms": _int_or_none(
            _first("end_ms", "endMs", "as_of_to_ms", "asOfToMs")
        ),
        "now_ms": _int_or_none(_first("now_ms", "as_of_ms", "asOfMs")),
        "_explicit_strategy": raw_strategy is not None,
        "_explicit_horizon": raw_horizon is not None,
        "_unknown_strategy": (
            raw_strategy is not None and strategy is None
            and str(raw_strategy).strip() != ""
        ),
    }


def _parse_json_dict(value: Any) -> dict[str, Any]:
    import json as _json

    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = _json.loads(value)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def _field(obj: Any, *names: str) -> Any:
    for name in names:
        if isinstance(obj, Mapping) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            try:
                return getattr(obj, name)
            except Exception:
                continue
    return None


def _normalise_fcs(row: Any) -> dict[str, Any]:
    get = (lambda *names: _field(row, *names))
    try:
        as_of = int(get("as_of_ms", "asOfMs"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        as_of = 0
    venue_summary = _parse_json_dict(
        get("venue_summary_json", "venue_summary", "venueSummary")
    )
    funding_metrics = _parse_json_dict(
        get("funding_metrics_json", "funding_metrics", "fundingMetrics")
    )
    risk = _parse_json_dict(get("risk_json", "risk"))
    history_class = (
        funding_metrics.get("history_class") or risk.get("history_class")
    )
    venue = None
    for key in ("venue", "spot_venue", "spotVenue", "best_venue", "bestVenue"):
        value = venue_summary.get(key)
        if isinstance(value, str) and value.strip():
            venue = value.strip().upper()
            break
    return {
        "snapshot_id": str(get("snapshot_id", "snapshotId") or ""),
        "symbol": str(get("symbol") or ""),
        "canonical_id": str(get("canonical_id", "canonicalId") or ""),
        "as_of_ms": as_of,
        "fcs_version": str(get("fcs_version", "fcsVersion") or ""),
        "fcs_config_hash": str(get("fcs_config_hash", "fcsConfigHash") or ""),
        "history_class": str(history_class) if history_class else None,
        "venue": venue,
        "_raw": row,
    }


def _dec_or_none(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value).strip()) if isinstance(value, str) \
            else Decimal(value)
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


def _dec_str(value: Decimal) -> str:
    text = format(Decimal(value), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
        if text in ("", "-"):
            text = "0"
        elif text.startswith("."):
            text = "0" + text
        elif text.startswith("-."):
            text = "-0." + text[2:]
    if text == "-0":
        text = "0"
    return text


async def hedge_summary(
    filters: Mapping[str, Any] | None,
    repository: Any,
    policy: Any = None,
) -> Any:
    """Aggregate frozen hedge outcomes to one :class:`HedgeEvidenceSummary`.

    Only ``sl_hedge_outcome`` rows are read; the directional outcome store
    and the user-ledger fill table are never consulted, so simulated and
    live-manual statistics stay separate by construction.
    """
    from diveintocrypto_desktop.shortlab.hedge.models import HedgeEvidenceSummary

    sel = parse_hedge_filters(filters)
    # Unknown strategy enum matches nothing (H08 surfaces 422); the DTO
    # still requires a valid strategy, so short-circuit to an empty bucket.
    if sel["_unknown_strategy"]:
        from diveintocrypto_desktop.shortlab.hedge import HEDGE_EVIDENCE_VERSION as _EV
        return HedgeEvidenceSummary(
            strategy="ABSOLUTE_100",  # type: ignore[arg-type]
            horizon_days=sel["horizon_days"],  # type: ignore[arg-type]
            sample_count=0,
            complete_count=0,
            censored_count=0,
            avg_net_return=None,
            median_net_return=None,
            evidence_version=str(_EV),
        )

    strategy = sel["strategy"]
    horizon_days = sel["horizon_days"]

    # Cost/evidence selection: explicit bucket alone, else current.
    cost_hash = sel["cost_config_hash"]
    if cost_hash is None:
        try:
            from diveintocrypto_desktop.shortlab.evidence.hedge_grader import (
                resolve_cost_hash as _resolve,
            )
        except Exception:
            _resolve = None  # type: ignore[assignment]
        if _resolve is not None:
            try:
                cost_hash = _resolve(policy)
            except Exception:
                cost_hash = None
    evidence_version = sel["evidence_version"]
    if evidence_version is None:
        try:
            from diveintocrypto_desktop.shortlab.hedge import (
                HEDGE_EVIDENCE_VERSION as _EV2,
            )
            evidence_version = str(_EV2)
        except Exception:
            evidence_version = "hedge_evidence_v2"

    now_ms = sel["now_ms"] if sel["now_ms"] is not None else _now_ms()

    # -- collect FCS snapshots -------------------------------------------
    fcs_rows: list[dict[str, Any]] = []
    list_fn = getattr(repository, "list_fcs", None) or getattr(
        repository, "list_funding_opportunities", None
    )
    if callable(list_fn):
        offset = 0
        while True:
            try:
                page = await list_fn(None, 200, offset)
            except TypeError:
                try:
                    page = await list_fn(symbol=None, limit=200, offset=offset)
                except Exception:
                    break
            except Exception:
                break
            items = list(page or ())
            for item in items:
                fcs_rows.append(_normalise_fcs(item))
            if len(items) < 200:
                break
            offset += 200

    # -- window + attribute filters (never silently mix versions) ----------
    wanted_symbol = sel["symbol"].upper() if sel["symbol"] else None
    kept: list[dict[str, Any]] = []
    for row in fcs_rows:
        if wanted_symbol and row["symbol"].upper() != wanted_symbol:
            continue
        if sel["fcs_version"] and row["fcs_version"] != sel["fcs_version"]:
            continue
        if sel["history_class"] and row["history_class"] != sel["history_class"]:
            continue
        if sel["venue"] and (row["venue"] or "") != sel["venue"].upper():
            continue
        if sel["start_ms"] is not None and row["as_of_ms"] < sel["start_ms"]:
            continue
        if sel["end_ms"] is not None and row["as_of_ms"] > sel["end_ms"]:
            continue
        kept.append(row)

    # -- join outcomes ------------------------------------------------------
    complete_returns: list[Decimal] = []
    complete_count = 0
    censored_count = 0
    for fcs in kept:
        outcome = await _matching_outcome(
            repository, fcs["snapshot_id"], strategy, horizon_days,
            str(evidence_version), str(cost_hash) if cost_hash else None,
        )
        if outcome is not None:
            status = str(outcome.get("outcome_status") or "")
            if status == "COMPLETE":
                complete_count += 1
                ret = _outcome_net_return(outcome)
                if ret is not None:
                    complete_returns.append(ret)
            elif status == "CENSORED":
                censored_count += 1
            # UNAVAILABLE rows stay in the sample only.
            continue
        # No row: PENDING (not due) vs UNAVAILABLE (due, not graded) both
        # stay in the sample but outside complete/censored counts.
        _ = fcs  # explicit: virtual states need no extra bookkeeping here.

    sample_count = len(kept)
    avg_s: str | None = None
    median_s: str | None = None
    if complete_returns:
        with localcontext() as ctx:
            ctx.prec = 80
            total = sum(complete_returns, Decimal("0"))
            avg_s = _dec_str(total / Decimal(len(complete_returns)))
        ordered = sorted(complete_returns)
        mid = len(ordered) // 2
        if len(ordered) % 2 == 1:
            median_s = _dec_str(ordered[mid])
        else:
            with localcontext() as ctx:
                ctx.prec = 80
                median_s = _dec_str((ordered[mid - 1] + ordered[mid]) / Decimal("2"))

    return HedgeEvidenceSummary(
        strategy=strategy,  # type: ignore[arg-type]
        horizon_days=int(horizon_days),  # type: ignore[arg-type]
        sample_count=int(sample_count),
        complete_count=int(complete_count),
        censored_count=int(censored_count),
        avg_net_return=avg_s,
        median_net_return=median_s,
        evidence_version=str(evidence_version),
    )


async def _matching_outcome(
    repository: Any,
    fcs_snapshot_id: str,
    strategy: str,
    horizon_days: int,
    evidence_version: str,
    cost_hash: str | None,
) -> dict[str, Any] | None:
    fn = getattr(repository, "list_hedge_outcomes", None)
    if not callable(fn):
        return None
    try:
        rows = await fn(fcs_snapshot_id, 200, 0)
    except Exception:
        return None
    for row in list(rows or ()):
        data = dict(row) if isinstance(row, Mapping) else {
            "outcome_status": _field(row, "outcome_status"),
            "strategy": _field(row, "strategy"),
            "horizon_days": _field(row, "horizon_days"),
            "evidence_version": _field(row, "evidence_version"),
            "cost_config_hash": _field(row, "cost_config_hash"),
            "outcome_json": _field(row, "outcome_json"),
        }
        try:
            same_strategy = str(data.get("strategy")) == strategy
            same_horizon = int(data.get("horizon_days")) == int(horizon_days)  # type: ignore[arg-type]
            same_ev = str(data.get("evidence_version")) == evidence_version
            same_cost = (cost_hash is None) or (
                str(data.get("cost_config_hash")) == cost_hash
            )
        except (TypeError, ValueError):
            continue
        if same_strategy and same_horizon and same_ev and same_cost:
            # Cost-filtered view selects one bucket alone; the unfiltered
            # default already scoped cost_hash to the current one, so the
            # first match is the bucket row.
            if isinstance(row, Mapping):
                return dict(row)
            return dict(data)
    return None


def _outcome_net_return(outcome: Mapping[str, Any]) -> Decimal | None:
    raw_json = outcome.get("outcome_json")
    parsed = _parse_json_dict(raw_json)
    pnl = parsed.get("pnl") if isinstance(parsed.get("pnl"), Mapping) else {}
    for key in ("net_return", "netReturn", "net_return_str"):
        candidate = pnl.get(key) if isinstance(pnl, Mapping) else None
        if candidate is None:
            candidate = parsed.get(key)
        parsed_dec = _dec_or_none(candidate)
        if parsed_dec is not None:
            return parsed_dec
    return None
