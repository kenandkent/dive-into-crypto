"""H10 independent hedge Evidence domain (design B39, plan H10).

Pure domain module: no router, no Scheduler registration (H08 wires
``run_due`` as a sub-stage of the existing forward grader cycle and lazy
imports this module). Writes only ``sl_hedge_outcome`` (never the legacy
directional ``sl_forward_outcome``).

Contract::

    async grade_hedge(snapshot_id, strategy, horizon, as_of_ms,
                      repository, market_provider, policy=None) -> HedgeOutcome
    async run_due(context) -> JobStatus

``hedge_summary`` lives in :mod:`hedge_metrics` to keep grading and
aggregation independently importable.

Semantics (B39.1.1/B39.2/B39.3):

- four fixed strategies ``ABSOLUTE_100`` (1.0) / ``RELATIVE_75`` (0.75) /
  ``RELATIVE_50`` (0.5) / ``RELATIVE_25`` (0.25); horizons 7/30/90D.
- entry/exit venue quotes only from frozen snapshots via the H01 frozen
  ``HistoricalMarketProvider`` (``read_frozen_quote`` /
  ``find_frozen_quote`` / ``read_price_bars`` / ``read_settled_funding`` /
  ``read_lifecycle``); never derived from a current price. Future quotes
  are never backfilled.
- frozen entry qty/price/capital/quote/cost/FX/source versions are stored
  in ``outcome_json``.
- missing historical quote/mark/FX -> ``UNAVAILABLE`` (never 0);
  delisted/expired -> ``CENSORED`` (retained, never dropped).
- ``PENDING`` is virtual until maturity and is never persisted (same
  write-once reason as the directional grader); terminal rows are
  persisted via H01 ``save_hedge_outcome`` with ``OUTCOME->FCS`` plus
  entry/exit ``VENUE_QUOTE`` pins in the same worker transaction.
- same snapshot with a different cost hash / evidence version mints a new
  row (the table key includes both); identical keys are idempotent.
- net-return denominator is frozen ``capital_at_risk`` (spot cash +
  futures margin + cost reserve), never a single margin leg.
- hedged drawdown stays unknown without synchronized spot/perp bars;
  async spot/perp extremes and a single futures path are never substituted.
- simulated fixed-strategy outcomes never merge user-ledger fills; the
  ledger stays a separate calibre (see ``hedge_metrics``).
"""

from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Mapping

DAY_MS = 86_400_000
HOUR_MS = 3_600_000

STRATEGY_RATIOS: dict[str, Decimal] = {
    "ABSOLUTE_100": Decimal("1"),
    "RELATIVE_75": Decimal("0.75"),
    "RELATIVE_50": Decimal("0.5"),
    "RELATIVE_25": Decimal("0.25"),
    # CR15 (D14.1): UNHEDGED_0 baseline (spot 0, futures-only) participates
    # in production capture/grading alongside the four fixed ratios.
    "UNHEDGED_0": Decimal("0"),
}

# CR15: SYSTEM_POLICY is not a fixed ratio (it copies the frozen selected
# proposal of the same USER_DECISION). It is a valid grading strategy when
# an Entry exists; the ratio is resolved from the Entry (never re-selected).
_SYSTEM_POLICY = "SYSTEM_POLICY"

HORIZON_DAYS = (7, 30, 90)

# Reason codes (B33 hedge subset + forward-compatible funding codes).
PENDING_NOT_DUE = "PENDING_NOT_DUE"
NO_ENTRY_BAR = "NO_ENTRY_BAR"
NO_EXIT_BAR = "NO_EXIT_BAR"
EXIT_BAR_INCOMPLETE = "EXIT_BAR_INCOMPLETE"
NO_ENTRY_QUOTE = "NO_ENTRY_QUOTE"
NO_EXIT_QUOTE = "NO_EXIT_QUOTE"
FUTURE_QUOTE_NOT_USED = "FUTURE_QUOTE_NOT_USED"
QUOTE_FX_MISSING = "QUOTE_FX_MISSING"
BAR_FX_MISSING = "BAR_FX_MISSING"
FUNDING_MARK_MISSING = "FUNDING_MARK_MISSING"
FUNDING_FX_MISSING = "FUNDING_FX_MISSING"
FUNDING_HISTORY_INCOMPLETE = "FUNDING_HISTORY_INCOMPLETE"
CONTRACT_DELISTED = "CONTRACT_DELISTED"
NO_FCS_SNAPSHOT = "NO_FCS_SNAPSHOT"
NO_MARKET_PROVIDER = "NO_MARKET_PROVIDER"
BAD_ENTRY_PRICE = "BAD_ENTRY_PRICE"
NO_EXIT_PRICE = "NO_EXIT_PRICE"

_DELISTED_STATUSES = frozenset(
    {
        "DELISTED",
        "DELISTING",
        "SETTLED",
        "SETTLING",
        "EXPIRED",
        "DELIVERED",
        "CLOSED",
        "TERMINATED",
    }
)

_DEFAULT_COST_HASH = (
    "9915b1468e5d0e4fc02ee71b4883c5445351928b0e7509f0dc4e6727fcca37dc"
)

__all__ = [
    "DAY_MS",
    "STRATEGY_RATIOS",
    "HORIZON_DAYS",
    "grade_hedge",
    "run_due",
    "normalize_horizon_days",
    "normalize_strategy",
    "resolve_cost_hash",
    "outcome_id_for",
]


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def normalize_strategy(strategy: Any) -> str:
    """Validate a hedge strategy (B39.2 + CR15 D14.1).

    Fixed ratios (ABSOLUTE_100/RELATIVE_*/UNHEDGED_0) plus SYSTEM_POLICY
    (frozen selected-proposal copy, graded via its Entry pairing).
    """
    text = str(strategy).strip().upper() if strategy is not None else ""
    # Accept "100"/"75" shorthands defensively, canonicalise to frozen names.
    aliases = {
        "100": "ABSOLUTE_100",
        "ABSOLUTE": "ABSOLUTE_100",
        "ABSOLUTE100": "ABSOLUTE_100",
        "75": "RELATIVE_75",
        "50": "RELATIVE_50",
        "25": "RELATIVE_25",
        "0": "UNHEDGED_0",
        "UNHEDGED": "UNHEDGED_0",
        "H0": "UNHEDGED_0",
    }
    if text in aliases:
        text = aliases[text]
    if text in STRATEGY_RATIOS or text == _SYSTEM_POLICY:
        return text
    raise ValueError(
        f"strategy={strategy!r} must be one of {sorted(list(STRATEGY_RATIOS) + [_SYSTEM_POLICY])}"
    )


def normalize_horizon_days(horizon: Any) -> int:
    """Accept ``7``/``30``/``90`` or ``7D``/``30D``/``90D`` (any case)."""
    if isinstance(horizon, bool):
        raise ValueError(f"horizon={horizon!r} must be 7, 30 or 90")
    if isinstance(horizon, int):
        days = horizon
    else:
        text = str(horizon).strip().upper()
        if text.endswith("D"):
            text = text[:-1]
        try:
            days = int(text)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"horizon={horizon!r} must be 7, 30 or 90"
            ) from exc
    if days not in HORIZON_DAYS:
        raise ValueError(f"horizon={horizon!r} must be 7, 30 or 90")
    return days


def resolve_cost_hash(policy: Any = None) -> str:
    """Resolve the hedge cost-config hash for one grading call.

    Priority: explicit ``cost_config_hash`` mapping entry, H01
    ``hedge_cost_config_hash(config)`` for a config object, otherwise the
    default-config golden (B附录F.2). A changed cost mints a distinct hash
    so the new outcome coexists with -- never overwrites -- history.
    """
    if isinstance(policy, str) and len(policy.strip()) == 64:
        return policy.strip()
    if isinstance(policy, Mapping):
        for key in (
            "cost_config_hash",
            "costConfigHash",
            "hedge_cost_config_hash",
            "hedgeCostConfigHash",
        ):
            value = policy.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    if policy is not None and not isinstance(policy, Mapping):
        try:
            from diveintocrypto_desktop.shortlab.config import (
                hedge_cost_config_hash as _cost_hash,
            )

            return str(_cost_hash(policy))
        except Exception:
            pass
    try:
        from diveintocrypto_desktop.shortlab.config import (
            hedge_cost_config_hash as _cost_hash2,
        )
        from diveintocrypto_desktop.shortlab.config import (
            load_shortlab_config as _load,
        )

        return str(_cost_hash2(_load()))
    except Exception:
        return _DEFAULT_COST_HASH


def _resolve_evidence_version(policy: Any = None) -> str:
    # R14b/D15: R00 current only (no local fallback literal). Import failure
    # surfaces as DEPENDENCY_UNAVAILABLE, never a silent old-version compute.
    from diveintocrypto_desktop.shortlab.hedge import (
        HEDGE_EVIDENCE_VERSION_CURRENT as _CURRENT,
    )

    if isinstance(policy, Mapping):
        for key in ("evidence_version", "evidenceVersion",
                    "hedge_evidence_version", "hedgeEvidenceVersion"):
            value = policy.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    candidate = getattr(policy, "evidence_version", None)
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    return str(_CURRENT)


def outcome_id_for(
    snapshot_id: str,
    strategy: str,
    horizon_days: int,
    evidence_version: str,
    cost_hash: str,
) -> str:
    """Deterministic outcome id (one row per cost/version key)."""
    return (
        f"{snapshot_id}#{strategy}#{int(horizon_days)}#"
        f"{evidence_version}#{str(cost_hash)[:12]}"
    )


# ---------------------------------------------------------------------------
# Decimal helpers (ledger boundary: never float for qty/price).
# ---------------------------------------------------------------------------


def _parse_dec(name: str, value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = Decimal(text)
        except (InvalidOperation, ValueError, ArithmeticError):
            return None
    else:
        return None
    if not parsed.is_finite():
        return None
    return parsed


def _dec_str(value: Decimal) -> str:
    if not isinstance(value, Decimal):
        value = Decimal(value)
    text = format(value, "f")
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


# ---------------------------------------------------------------------------
# Repository access (duck-typed; never touches directional tables).
# ---------------------------------------------------------------------------


async def _fetch_fcs(repository: Any, snapshot_id: str) -> dict[str, Any] | None:
    """Fetch one FCS snapshot row by id (supports real + fake repos).

    CR15: when no FCS row matches, fall back to a USER_DECISION Entry group
    (source_snapshot_id == snapshot_id). The Entry's frozen symbol/as_of and
    its Decision's reference notional/funding are used to build an FCS-like
    view so the same frozen market history grades the real Entry (including
    UNHEDGED_0 and its SYSTEM_POLICY pairing) instead of ignoring it.
    """
    # Direct single-row accessors first (future-proof).
    for method in ("get_funding_capture_snapshot", "get_fcs_snapshot", "get_fcs"):
        fn = getattr(repository, method, None)
        if callable(fn):
            try:
                row = await fn(snapshot_id)
            except TypeError:
                try:
                    row = await fn(snapshot_id=snapshot_id)  # type: ignore[call-arg]
                except Exception:
                    row = None
            except Exception:
                row = None
            if row is not None:
                return _normalise_fcs(row)
    # Paginated list scan (H01 only exposes list_fcs / list_funding_*).
    for method in ("list_fcs", "list_funding_opportunities", "list_funding_capture"):
        fn = getattr(repository, method, None)
        if not callable(fn):
            continue
        offset = 0
        while True:
            try:
                page = await fn(None, 200, offset)
            except TypeError:
                try:
                    page = await fn(symbol=None, limit=200, offset=offset)
                except Exception:
                    break
            except Exception:
                break
            items = list(page) if page is not None else []
            for item in items:
                norm = _normalise_fcs(item)
                if norm.get("snapshot_id") == snapshot_id:
                    return norm
            if len(items) < 200:
                break
            offset += 200
    # CR15 fallback: USER_DECISION Entry group (real Entry, not FCS).
    try:
        _entry_view = await _fetch_entry_fcs_view(repository, snapshot_id)
        if _entry_view is not None:
            return _entry_view
    except Exception:
        pass
    return None


async def _fetch_entry_fcs_view(repository: Any, snapshot_id: str) -> dict[str, Any] | None:
    """Build an FCS-like view from a USER_DECISION Entry group (CR15).

    Looks for strategy entries with ``source_snapshot_id == snapshot_id``
    across the four cohorts. Uses the first entry's symbol/as_of and the
    Decision's reference notional when available (honest, never invented).
    Returns ``None`` when no Entry group matches.
    """
    _list_fn = getattr(repository, "list_strategy_entries", None)
    if not callable(_list_fn):
        return None
    _found: dict[str, Any] | None = None
    for _cohort in ("USER_DECISION", "RESEARCH_CANDIDATE", "EXECUTABLE_DIRECTIONAL", "FUNDING_CARRY"):
        try:
            # Wide window: entries are point-in-time, filter by source id.
            rows = await _list_fn(_cohort, 0, 2**62)
        except TypeError:
            try:
                rows = await _list_fn(cohort=_cohort, start_ms=0, end_ms=2**62)  # type: ignore[call-arg]
            except Exception:
                continue
        except Exception:
            continue
        for _row in (rows or ()):
            try:
                _src = _field(_row, "source_snapshot_id", "sourceSnapshotId")
                if str(_src or "") != str(snapshot_id):
                    continue
                _sym = str(_field(_row, "symbol") or "")
                _asof = _field(_row, "decision_as_of_ms", "decisionAsOfMs", "as_of_ms")
                try:
                    _asof_i = int(_asof)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    continue
                if not _sym or _asof_i <= 0:
                    continue
                _entry_json = _field(_row, "entry_json", "entryJson")
                if isinstance(_entry_json, str):
                    try:
                        import json as _js

                        _entry_json = _js.loads(_entry_json)
                    except Exception:
                        _entry_json = {}
                if not isinstance(_entry_json, Mapping):
                    _entry_json = {}
                _found = {
                    "symbol": _sym,
                    "canonical_id": str(_field(_row, "canonical_id", "canonicalId") or _sym.lower()),
                    "as_of_ms": _asof_i,
                    "_entry_row": _row,
                    "_entry_json": dict(_entry_json),
                }
                break
            except Exception:
                continue
        if _found is not None:
            break
    if _found is None:
        return None
    # Reference notional: Decision request when available (same USER_DECISION),
    # else Entry frozen qty * entry price (honest, never invented).
    _ref: str | None = None
    try:
        _get_dec = getattr(repository, "get_hedge_decision", None)
        if callable(_get_dec):
            try:
                _dec_row = await _get_dec(str(snapshot_id))
            except Exception:
                _dec_row = None
            if isinstance(_dec_row, Mapping):
                _dj = _dec_row.get("decision_json")
                if isinstance(_dj, str):
                    try:
                        import json as _js2

                        _dj = _js2.loads(_dj)
                    except Exception:
                        _dj = {}
                if isinstance(_dj, Mapping):
                    _req = _dj.get("request")
                    if isinstance(_req, Mapping) and _req.get("futures_notional_usd") is not None:
                        try:
                            _ref = _dec_str(Decimal(str(_req.get("futures_notional_usd"))))
                        except Exception:
                            _ref = None
    except Exception:
        pass
    if _ref is None:
        try:
            _ej = _found.get("_entry_json") or {}
            _canon = _ej.get("canonical_futures_qty")
            _vwap = _ej.get("futures_entry_vwap_native")
            if _canon is not None and _vwap is not None:
                with localcontext() as _ctx:
                    _ctx.prec = 80
                    _ref = _dec_str(Decimal(str(_canon)) * Decimal(str(_vwap)))
        except Exception:
            _ref = None
    _sym_f = _found.get("symbol") or ""
    _canon_f = _found.get("canonical_id") or str(_sym_f).lower()
    return {
        "snapshot_id": str(snapshot_id),
        "symbol": str(_sym_f),
        "canonical_id": str(_canon_f),
        "as_of_ms": int(_found.get("as_of_ms") or 0),
        "fcs_version": "fcs_v1",
        "fcs_config_hash": "",
        "reference_notional_usd": _ref,
        "funding_metrics": {},
        "venue_summary": {},
        "basis": None,
        "risk": {},
        "readiness": "",
        "_raw": _found.get("_entry_row"),
        "_from_entry": True,
    }


def _normalise_fcs(row: Any) -> dict[str, Any]:
    """Normalise a DB row / DTO to a plain FCS dict."""
    get = (lambda *names: _field(row, *names))
    snapshot_id = get("snapshot_id", "snapshotId")
    symbol = get("symbol")
    canonical_id = get("canonical_id", "canonicalId")
    as_of_ms = get("as_of_ms", "asOfMs", "as_of")
    fcs_version = get("fcs_version", "fcsVersion") or "fcs_v1"
    fcs_config_hash = get("fcs_config_hash", "fcsConfigHash") or ""
    ref = get("reference_notional_usd", "referenceNotionalUsd",
              "reference_notional")
    funding_raw = get("funding_metrics_json", "funding_metrics",
                      "fundingMetrics")
    venue_raw = get("venue_summary_json", "venue_summary", "venueSummary")
    basis_raw = get("basis_json", "basis")
    risk_raw = get("risk_json", "risk")
    readiness = get("readiness")
    try:
        as_of_int = int(as_of_ms)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        as_of_int = 0
    # reference_notional may be DOUBLE in DB; keep a decimal string.
    if isinstance(ref, bool) or ref is None:
        ref_str: str | None = None
    else:
        try:
            ref_str = _dec_str(Decimal(str(ref)))
        except (InvalidOperation, ValueError, ArithmeticError):
            ref_str = None
    return {
        "snapshot_id": str(snapshot_id) if snapshot_id is not None else "",
        "symbol": str(symbol) if symbol is not None else "",
        "canonical_id": str(canonical_id) if canonical_id is not None else "",
        "as_of_ms": as_of_int,
        "fcs_version": str(fcs_version),
        "fcs_config_hash": str(fcs_config_hash),
        "reference_notional_usd": ref_str,
        "funding_metrics": _parse_json_dict(funding_raw),
        "venue_summary": _parse_json_dict(venue_raw),
        "basis": _parse_json_dict(basis_raw) or None,
        "risk": _parse_json_dict(risk_raw),
        "readiness": str(readiness) if readiness is not None else "",
        "_raw": row,
    }


async def _find_existing_outcome(
    repository: Any,
    fcs_snapshot_id: str,
    strategy: str,
    horizon_days: int,
    evidence_version: str,
    cost_hash: str,
) -> dict[str, Any] | None:
    fn = getattr(repository, "list_hedge_outcomes", None)
    if not callable(fn):
        return None
    try:
        rows = await fn(fcs_snapshot_id, 200, 0)
    except Exception:
        return None
    for row in list(rows or ()):
        if isinstance(row, Mapping):
            data = dict(row)
        else:
            data = {
                "outcome_id": _field(row, "outcome_id"),
                "fcs_snapshot_id": _field(row, "fcs_snapshot_id"),
                "strategy": _field(row, "strategy"),
                "horizon_days": _field(row, "horizon_days"),
                "outcome_status": _field(row, "outcome_status"),
                "reason_code": _field(row, "reason_code"),
                "evidence_version": _field(row, "evidence_version"),
                "cost_config_hash": _field(row, "cost_config_hash"),
                "outcome_json": _field(row, "outcome_json"),
                "updated_at_ms": _field(row, "updated_at_ms"),
            }
        try:
            same = (
                str(data.get("strategy")) == strategy
                and int(data.get("horizon_days")) == int(horizon_days)  # type: ignore[arg-type]
                and str(data.get("evidence_version")) == evidence_version
                and str(data.get("cost_config_hash")) == cost_hash
            )
        except (TypeError, ValueError):
            continue
        if same:
            return data
    return None


def _outcome_row_to_dto(row: Mapping[str, Any]) -> Any:
    from diveintocrypto_desktop.shortlab.hedge.models import HedgeOutcome

    outcome_json = row.get("outcome_json")
    if isinstance(outcome_json, str):
        import json as _json

        try:
            outcome_json = _json.loads(outcome_json)
        except (TypeError, ValueError):
            outcome_json = {}
    if not isinstance(outcome_json, Mapping):
        outcome_json = {}
    return HedgeOutcome(
        outcome_id=str(row.get("outcome_id")),
        fcs_snapshot_id=str(row.get("fcs_snapshot_id")),
        strategy=str(row.get("strategy")),
        horizon_days=int(row.get("horizon_days")),  # type: ignore[arg-type]
        outcome_status=str(row.get("outcome_status")),
        reason_code=row.get("reason_code"),
        evidence_version=str(row.get("evidence_version")),
        cost_config_hash=str(row.get("cost_config_hash")),
        outcome_json=dict(outcome_json),
        updated_at_ms=int(row.get("updated_at_ms") or 0),
    )


# ---------------------------------------------------------------------------
# Market-data normalisation (frozen DTOs or plain dicts; never live state).
# ---------------------------------------------------------------------------


def _norm_bar(bar: Any) -> dict[str, Any] | None:
    open_ms = _field(bar, "open_ms", "openMs", "open_time_ms", "openTimeMs",
                     "t", "open_time")
    close_ms = _field(bar, "close_ms", "closeMs", "close_time_ms", "candle_close_ms")
    native_open = _field(bar, "native_open", "nativeOpen", "o", "open")
    native_high = _field(bar, "native_high", "nativeHigh", "h", "high")
    native_low = _field(bar, "native_low", "nativeLow", "l", "low")
    native_close = _field(bar, "native_close", "nativeClose", "c", "close")
    fx = _field(bar, "fx_to_usd", "fxToUsd", "fx", "quote_to_usd", "quoteToUsd")
    known_at = _field(bar, "known_at_ms", "knownAtMs", "known_at", "fetched_at_ms")
    source = _field(bar, "source", "source_id", "sourceId")
    basis_raw = _field(bar, "price_basis", "priceBasis", "basis")
    if isinstance(basis_raw, str) and basis_raw.strip():
        basis = basis_raw.strip().upper()
    else:
        # Legacy bars without provenance default to TRADE (never MARK).
        basis = "TRADE"
    try:
        open_i = int(open_ms)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if close_ms is None:
        close_i = open_i + HOUR_MS
    else:
        try:
            close_i = int(close_ms)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
    if close_i <= open_i:
        return None
    try:
        known_i = int(known_at) if known_at is not None else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        known_i = None
    return {
        "open_ms": open_i,
        "close_ms": close_i,
        "native_open": _parse_dec("o", native_open),
        "native_high": _parse_dec("h", native_high),
        "native_low": _parse_dec("l", native_low),
        "native_close": _parse_dec("c", native_close),
        "fx_to_usd": _parse_dec("fx", fx),
        "known_at_ms": known_i,
        "source": str(source) if source is not None else "",
        "price_basis": basis,
        "_raw": bar,
    }


def _norm_quote(quote: Any) -> dict[str, Any] | None:
    if quote is None:
        return None
    # Repository venue row wraps the quote JSON in ``quote_json``.
    if isinstance(quote, Mapping) and "quote_json" in quote:
        row = dict(quote)
        inner = _parse_json_dict(row.get("quote_json"))
        # Merge row-level provenance with inner quote fields.
        merged: dict[str, Any] = dict(inner)
        for key in ("snapshot_id", "snapshotId", "canonical_id", "canonicalId",
                    "venue", "as_of_ms", "asOfMs", "fetched_at_ms",
                    "fetchedAtMs", "expires_at_ms", "status", "reason_code"):
            if key in row and key not in merged:
                merged[key] = row[key]
        # Snapshot id lives on the row, not inside the quote JSON.
        if "snapshot_id" not in merged and row.get("snapshot_id"):
            merged["snapshot_id"] = row["snapshot_id"]
        quote = merged
    get = (lambda *names: _field(quote, *names))
    buy = get("buy_vwap", "buyVwap")
    sell = get("sell_vwap", "sellVwap")
    mid = get("mid_price", "midPrice", "mid")
    fx = get("quote_to_usd", "quoteToUsd", "fx_to_usd", "fxToUsd")
    qty = get("requested_canonical_qty", "requestedCanonicalQty")
    gas = get("estimated_gas_usd", "estimatedGasUsd", "gas_usd", "gasUsd")
    fee = get("estimated_fee_usd", "estimatedFeeUsd", "fee_usd")
    as_of = get("as_of_ms", "asOfMs", "source_as_of_ms", "sourceTimestampMs",
                "source_timestamp_ms")
    fetched = get("fetched_at_ms", "fetchedAtMs", "known_at_ms", "knownAtMs")
    snapshot_id = get("snapshot_id", "snapshotId")
    canonical_id = get("canonical_id", "canonicalId")
    venue = get("venue")
    status = get("status")
    return {
        "snapshot_id": str(snapshot_id) if snapshot_id is not None else None,
        "canonical_id": str(canonical_id) if canonical_id is not None else None,
        "venue": str(venue) if venue is not None else None,
        "as_of_ms": int(as_of) if as_of is not None else None,  # type: ignore[arg-type]
        "fetched_at_ms": int(fetched) if fetched is not None else None,  # type: ignore[arg-type]
        "buy_vwap": _parse_dec("buy", buy),
        "sell_vwap": _parse_dec("sell", sell),
        "mid_price": _parse_dec("mid", mid),
        "quote_to_usd": _parse_dec("fx", fx),
        "requested_canonical_qty": str(qty).strip() if qty is not None else None,
        "estimated_gas_usd": _parse_dec("gas", gas),
        "estimated_fee_usd": _parse_dec("fee", fee),
        "quote_currency": get("quote_currency", "quoteCurrency") or "USDT",
        "status": str(status) if status is not None else None,
        "_raw": quote,
    }


def _norm_funding(event: Any) -> dict[str, Any] | None:
    get = (lambda *names: _field(event, *names))
    t = get("funding_time_ms", "fundingTimeMs", "t", "funding_time", "fundingTime")
    rate = get("rate", "funding_rate", "fundingRate")
    mark = get("mark_price", "markPrice", "mark")
    fx = get("fx_to_usd", "fxToUsd", "fx", "quote_to_usd")
    known_at = get("known_at_ms", "knownAtMs", "known_at")
    try:
        t_i = int(t)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    rate_d = _parse_dec("rate", rate)
    if rate_d is None:
        return None
    try:
        known_i = int(known_at) if known_at is not None else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        known_i = None
    return {
        "funding_time_ms": t_i,
        "rate": rate_d,
        "mark_price": _parse_dec("mark", mark),
        "fx_to_usd": _parse_dec("fx", fx),
        "quote_asset": get("quote_asset", "quoteAsset") or "USDT",
        "known_at_ms": known_i,
        "source": str(get("source", "source_id") or ""),
        "_raw": event,
    }


def _is_delisted_lifecycle(lifecycle: Any, cutoff_ms: int) -> bool:
    if lifecycle is None:
        return False
    status = _field(lifecycle, "exchange_status", "exchangeStatus", "status")
    if isinstance(status, str) and status.strip().upper() in _DELISTED_STATUSES:
        return True
    flag = _field(lifecycle, "is_delisted", "delisted")
    if flag is True:
        return True
    delivery = _field(lifecycle, "delivery_at_ms", "deliveryAtMs", "delist_ms",
                      "delistMs", "last_tradable_ms")
    try:
        if delivery is not None and int(delivery) < int(cutoff_ms):  # type: ignore[arg-type]
            # A dated delivery/termination before the horizon end delists the
            # contract for evidence purposes.
            contract_type = _field(lifecycle, "contract_type", "contractType")
            if contract_type is not None or status is not None:
                return True
    except (TypeError, ValueError):
        pass
    return False


def _policy_cost_rates(policy: Any) -> dict[str, Decimal]:
    """Two-leg fee rates (B13/B40 costs subtree; defaults mirror planner)."""
    defaults = {
        "futures_entry_fee_rate": Decimal("0.0005"),
        "futures_exit_fee_rate": Decimal("0.0005"),
        "spot_entry_fee_rate": Decimal("0.001"),
        "spot_exit_fee_rate": Decimal("0.001"),
    }
    costs: Any = None
    if isinstance(policy, Mapping):
        costs = policy.get("costs") or policy.get("hedge", {}).get("costs") \
            if isinstance(policy.get("hedge"), Mapping) else policy.get("costs")
        if costs is None and "futures_entry_fee_rate" in policy:
            costs = policy
    else:
        hedge = getattr(policy, "hedge", None)
        if hedge is not None:
            costs = getattr(hedge, "costs", None)
            if isinstance(costs, Mapping):
                pass
            elif costs is not None:
                try:
                    costs = dict(costs)
                except Exception:
                    costs = None
    if isinstance(costs, Mapping):
        for key in list(defaults):
            raw = costs.get(key)
            parsed = _parse_dec(key, raw) if raw is not None else None
            if parsed is not None and parsed >= 0:
                defaults[key] = parsed
    else:
        # Attribute-style costs object (config dataclass mapping).
        for key in list(defaults):
            raw = None
            if costs is not None:
                try:
                    raw = costs.get(key) if isinstance(costs, Mapping) else getattr(costs, key, None)
                except Exception:
                    raw = None
            if raw is None and not isinstance(policy, Mapping) and policy is not None:
                try:
                    hedge = getattr(policy, "hedge", None)
                    cobj = getattr(hedge, "costs", None) if hedge is not None else None
                    raw = cobj.get(key) if isinstance(cobj, Mapping) else getattr(cobj, key, None)
                except Exception:
                    raw = None
            parsed = _parse_dec(key, raw) if raw is not None else None
            if parsed is not None and parsed >= 0:
                defaults[key] = parsed
    return defaults


def _policy_leverage(policy: Any) -> Decimal:
    if isinstance(policy, Mapping):
        for key in ("futures_leverage", "leverage"):
            parsed = _parse_dec(key, policy.get(key))
            if parsed is not None and parsed >= 1:
                return parsed
        hedge = policy.get("hedge")
        if isinstance(hedge, Mapping):
            for key in ("futures_leverage", "leverage"):
                parsed = _parse_dec(key, hedge.get(key))
                if parsed is not None and parsed >= 1:
                    return parsed
        return Decimal("1")
    for key in ("futures_leverage", "leverage"):
        parsed = _parse_dec(key, getattr(policy, key, None))
        if parsed is not None and parsed >= 1:
            return parsed
    return Decimal("1")


def _venue_for_fcs(fcs: Mapping[str, Any]) -> str:
    venue_summary = dict(fcs.get("venue_summary") or {})
    for key in ("venue", "spot_venue", "spotVenue", "best_venue", "bestVenue",
                "preferred_spot_venue"):
        value = venue_summary.get(key)
        if isinstance(value, str) and value.strip().upper() in (
            "BINANCE_SPOT", "BINANCE_ALPHA", "ONCHAIN_DEX",
        ):
            return value.strip().upper()
    return "BINANCE_SPOT"


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------


async def grade_hedge(
    snapshot_id: str,
    strategy: str,
    horizon: Any,
    as_of_ms: int,
    repository: Any,
    market_provider: Any,
    policy: Any = None,
) -> Any:
    """Grade one FCS snapshot for one fixed strategy and horizon.

    Reads only the archived FCS snapshot plus frozen market history via
    ``market_provider`` -- never a live provider and never a current price
    backfill. Persists terminal rows through H01 ``save_hedge_outcome``
    (same key is idempotent; a changed cost/version mints a new row) and
    returns the frozen :class:`HedgeOutcome`.
    """
    from diveintocrypto_desktop.shortlab.hedge.models import HedgeOutcome

    strategy = normalize_strategy(strategy)
    horizon_days = normalize_horizon_days(horizon)
    as_of_ms = int(as_of_ms)  # type: ignore[arg-type]
    if not snapshot_id or not isinstance(snapshot_id, str):
        raise ValueError("snapshot_id must be a non-empty str")
    if repository is None:
        raise ValueError("repository is required")

    cost_hash = resolve_cost_hash(policy)
    evidence_version = _resolve_evidence_version(policy)
    # R14b/D15: R00 current formula only (no fallback literal).
    from diveintocrypto_desktop.shortlab.hedge import (
        HEDGE_FORMULA_VERSION_CURRENT as HEDGE_FORMULA_VERSION,
    )

    fcs = await _fetch_fcs(repository, snapshot_id)
    if fcs is None or not fcs.get("snapshot_id"):
        raise LookupError(f"FCS snapshot {snapshot_id!r} not found")
    snapshot_as_of = int(fcs["as_of_ms"])
    due_ms = snapshot_as_of + horizon_days * DAY_MS
    # CR15: SYSTEM_POLICY copies the frozen selected proposal of the same
    # USER_DECISION (never re-selected). Resolve its actual ratio from the
    # Entry; missing Entry stays UNAVAILABLE (never fabricated).
    if strategy == _SYSTEM_POLICY:
        ratio: Decimal | None = None
        try:
            _list_fn = getattr(repository, "list_strategy_entries", None)
            if callable(_list_fn):
                for _cohort in ("USER_DECISION",):
                    try:
                        _rows = await _list_fn(_cohort, 0, 2**62)
                    except Exception:
                        continue
                    for _r in (_rows or ()):
                        try:
                            if str(_field(_r, "source_snapshot_id") or "") != str(snapshot_id):
                                continue
                            if str(_field(_r, "strategy") or "").upper() != _SYSTEM_POLICY:
                                continue
                            _ej = _field(_r, "entry_json")
                            if isinstance(_ej, str):
                                try:
                                    import json as _js_e

                                    _ej = _js_e.loads(_ej)
                                except Exception:
                                    _ej = {}
                            if isinstance(_ej, Mapping) and _ej.get("actual_ratio") is not None:
                                try:
                                    ratio = Decimal(str(_ej.get("actual_ratio")))
                                except Exception:
                                    ratio = None
                            break
                        except Exception:
                            continue
                    if ratio is not None:
                        break
        except Exception:
            ratio = None
        if ratio is None:
            # No frozen SYSTEM entry to copy: honest UNAVAILABLE (never 0/1).
            return await _persist_unavailable(
                repository, HedgeOutcome, snapshot_id, strategy, horizon_days,
                evidence_version, cost_hash, outcome_id_for(
                    snapshot_id, strategy, horizon_days, evidence_version, cost_hash),
                as_of_ms, snapshot_as_of, due_ms, fcs, "SYSTEM_NO_SELECTION",
                funding_event_count=None, funding_coverage=None,
            )
    else:
        ratio = STRATEGY_RATIOS[strategy]
    outcome_id = outcome_id_for(
        snapshot_id, strategy, horizon_days, evidence_version, cost_hash
    )

    def _pending(reason: str = PENDING_NOT_DUE) -> Any:
        return HedgeOutcome(
            outcome_id=outcome_id,
            fcs_snapshot_id=snapshot_id,
            strategy=strategy,  # type: ignore[arg-type]
            horizon_days=horizon_days,  # type: ignore[arg-type]
            outcome_status="PENDING",  # type: ignore[arg-type]
            reason_code=reason,
            evidence_version=evidence_version,
            cost_config_hash=cost_hash,
            outcome_json={
                "fcs_snapshot_id": snapshot_id,
                "strategy": strategy,
                "horizon_days": horizon_days,
                "snapshot_as_of_ms": snapshot_as_of,
                "due_ms": due_ms,
                "frozen": False,
            },
            updated_at_ms=as_of_ms,
        )

    # Idempotent re-grade: same key returns the stored row as-is.
    existing = await _find_existing_outcome(
        repository, snapshot_id, strategy, horizon_days,
        evidence_version, cost_hash,
    )
    if existing is not None:
        return _outcome_row_to_dto(existing)

    if as_of_ms < due_ms:
        # Virtual until maturity: no DB write so a later terminal grade
        # never hits the write-once guard.
        return _pending()

    exit_close_ms = ((due_ms + HOUR_MS - 1) // HOUR_MS) * HOUR_MS + HOUR_MS - 1
    if as_of_ms < exit_close_ms:
        return _pending('PENDING_EXIT_BAR')

    # -- due path needs frozen market history ---------------------------
    symbol = str(fcs.get("symbol") or "")
    canonical_id = str(fcs.get("canonical_id") or fcs.get("symbol") or "")
    venue = _venue_for_fcs(fcs)
    ref_str = fcs.get("reference_notional_usd")
    futures_notional = _parse_dec("reference_notional_usd", ref_str)
    if not symbol or not canonical_id or futures_notional is None \
            or futures_notional <= 0:
        return await _persist_unavailable(
            repository, HedgeOutcome, snapshot_id, strategy, horizon_days,
            evidence_version, cost_hash, outcome_id, as_of_ms,
            snapshot_as_of, due_ms, fcs,
            NO_FCS_SNAPSHOT if futures_notional is None else BAD_ENTRY_PRICE,
            funding_event_count=None, funding_coverage=None,
        )

    identity = fcs.get('risk', {}).get('identity', {})
    multiplier = _parse_dec('contract_multiplier', identity.get('contract_multiplier'))
    if (multiplier is None or multiplier <= 0 or
            identity.get('multiplier_source') not in ('MANUAL', 'EXCHANGE')):
        return await _persist_unavailable(
            repository, HedgeOutcome, snapshot_id, strategy, horizon_days,
            evidence_version, cost_hash, outcome_id, as_of_ms,
            snapshot_as_of, due_ms, fcs, 'LEGACY_IDENTITY_UNVERIFIED',
            funding_event_count=None, funding_coverage=None,
        )
    fcs['_contract_multiplier'] = multiplier

    # Lifecycle (explicit evidence only; unknown degrades to tradable).
    lifecycle = None
    if market_provider is not None:
        try:
            lifecycle = await market_provider.read_lifecycle(symbol, due_ms)
        except Exception:
            lifecycle = None
    delisted = _is_delisted_lifecycle(lifecycle, due_ms)

    # Price bars (verified-interval adapter output; carry known_at).
    raw_bars: Any = ()
    if market_provider is not None:
        try:
            raw_bars = await market_provider.read_price_bars(
                symbol, snapshot_as_of, min(as_of_ms, due_ms + 2 * HOUR_MS), None
            )
        except Exception:
            raw_bars = ()
    if getattr(market_provider, 'retryable_history_unavailable', False):
        return _pending('PENDING_HISTORY_RETRY')
    # Network history is learned at fetch completion, not at candle close.
    # Advance a real adapter's grading clock after reads; strategy due/entry
    # anchors remain frozen. Fixture providers lacking this clock stay pure.
    completed_at = getattr(market_provider, "completed_at_ms", None)
    if isinstance(completed_at, int) and completed_at > as_of_ms:
        as_of_ms = completed_at
    bars: list[dict[str, Any]] = []
    for raw in list(raw_bars or ()):
        norm = _norm_bar(raw)
        if norm is None:
            continue
        # Future bars (known after grading) can never price the past.
        if norm["known_at_ms"] is not None and norm["known_at_ms"] > as_of_ms:
            continue
        bars.append(norm)
    bars.sort(key=lambda b: (b["open_ms"], b["close_ms"]))

    entry_bar = _first_bar_at_or_after(bars, snapshot_as_of, as_of_ms)
    exit_bar = _first_bar_at_or_after(bars, due_ms, as_of_ms)
    if entry_bar is None or entry_bar["native_close"] is None \
            or entry_bar["native_close"] <= 0:
        if delisted:
            return await _persist_censored_no_bars(
                repository, HedgeOutcome, snapshot_id, strategy,
                horizon_days, evidence_version, cost_hash, outcome_id,
                as_of_ms, snapshot_as_of, due_ms, fcs, futures_notional,
            )
        return await _persist_unavailable(
            repository, HedgeOutcome, snapshot_id, strategy, horizon_days,
            evidence_version, cost_hash, outcome_id, as_of_ms,
            snapshot_as_of, due_ms, fcs, NO_ENTRY_BAR,
            funding_event_count=None, funding_coverage=None,
        )
    if entry_bar["fx_to_usd"] is None:
        if delisted:
            return await _persist_censored_no_bars(
                repository, HedgeOutcome, snapshot_id, strategy,
                horizon_days, evidence_version, cost_hash, outcome_id,
                as_of_ms, snapshot_as_of, due_ms, fcs, futures_notional,
            )
        return await _persist_unavailable(
            repository, HedgeOutcome, snapshot_id, strategy, horizon_days,
            evidence_version, cost_hash, outcome_id, as_of_ms,
            snapshot_as_of, due_ms, fcs, BAR_FX_MISSING,
            funding_event_count=None, funding_coverage=None,
        )
    with localcontext() as ctx:
        ctx.prec = 80
        futures_entry_usd = entry_bar["native_close"] * entry_bar["fx_to_usd"] / multiplier  # type: ignore[operator]
    if futures_entry_usd is None or futures_entry_usd <= 0:
        return await _persist_unavailable(
            repository, HedgeOutcome, snapshot_id, strategy, horizon_days,
            evidence_version, cost_hash, outcome_id, as_of_ms,
            snapshot_as_of, due_ms, fcs, BAD_ENTRY_PRICE,
            funding_event_count=None, funding_coverage=None,
        )

    # Frozen quantities: futures from the frozen notional/entry, spot from
    # the fixed strategy ratio (both frozen; never re-derived from live).
    with localcontext() as ctx:
        ctx.prec = 80
        futures_qty = futures_notional / futures_entry_usd
        spot_qty = futures_qty * ratio
    futures_qty_s = _dec_str(futures_qty)
    spot_qty_s = _dec_str(spot_qty)

    # Frozen spot quotes (entry at snapshot, exit at due; future forbidden).
    entry_quote = await _find_quote(
        market_provider, canonical_id, venue, spot_qty_s, snapshot_as_of,
        as_of_ms,
    )
    exit_quote = await _find_quote(
        market_provider, canonical_id, venue, spot_qty_s, due_ms, as_of_ms,
    )
    if entry_quote is None:
        if delisted:
            return await _persist_censored_no_bars(
                repository, HedgeOutcome, snapshot_id, strategy,
                horizon_days, evidence_version, cost_hash, outcome_id,
                as_of_ms, snapshot_as_of, due_ms, fcs, futures_notional,
            )
        reason = NO_MARKET_PROVIDER if market_provider is None else NO_ENTRY_QUOTE
        # A future-only quote is a distinct reason (never backfilled).
        if await _future_quote_exists(
            market_provider, canonical_id, venue, spot_qty_s, snapshot_as_of,
            as_of_ms,
        ):
            reason = FUTURE_QUOTE_NOT_USED
        return await _persist_unavailable(
            repository, HedgeOutcome, snapshot_id, strategy, horizon_days,
            evidence_version, cost_hash, outcome_id, as_of_ms,
            snapshot_as_of, due_ms, fcs, reason,
            funding_event_count=None, funding_coverage=None,
        )
    if entry_quote["quote_to_usd"] is None:
        if delisted:
            return await _persist_censored_no_bars(
                repository, HedgeOutcome, snapshot_id, strategy,
                horizon_days, evidence_version, cost_hash, outcome_id,
                as_of_ms, snapshot_as_of, due_ms, fcs, futures_notional,
            )
        return await _persist_unavailable(
            repository, HedgeOutcome, snapshot_id, strategy, horizon_days,
            evidence_version, cost_hash, outcome_id, as_of_ms,
            snapshot_as_of, due_ms, fcs, QUOTE_FX_MISSING,
            funding_event_count=None, funding_coverage=None,
        )
    entry_spot_usd = _quote_entry_price(entry_quote)
    if entry_spot_usd is None or entry_spot_usd <= 0:
        if delisted:
            return await _persist_censored_no_bars(
                repository, HedgeOutcome, snapshot_id, strategy,
                horizon_days, evidence_version, cost_hash, outcome_id,
                as_of_ms, snapshot_as_of, due_ms, fcs, futures_notional,
            )
        return await _persist_unavailable(
            repository, HedgeOutcome, snapshot_id, strategy, horizon_days,
            evidence_version, cost_hash, outcome_id, as_of_ms,
            snapshot_as_of, due_ms, fcs, NO_EXIT_PRICE,
            funding_event_count=None, funding_coverage=None,
        )

    # Exit leg: delisted contracts keep a CENSORED row even when the exit
    # quote/bar is gone; tradable contracts need both.
    if exit_bar is None or exit_bar["native_close"] is None \
            or exit_bar["native_close"] <= 0 or exit_bar["fx_to_usd"] is None:
        if delisted:
            return await _settle_censored_early_exit(
                repository, HedgeOutcome, snapshot_id, strategy,
                horizon_days, evidence_version, cost_hash, outcome_id,
                as_of_ms, snapshot_as_of, due_ms, fcs, futures_notional,
                futures_qty, spot_qty, futures_entry_usd, entry_spot_usd,
                entry_bar, entry_quote, bars, market_provider, policy,
                venue, canonical_id, symbol, HEDGE_FORMULA_VERSION,
            )
        reason = NO_EXIT_BAR if exit_bar is None else (
            BAR_FX_MISSING if exit_bar.get("fx_to_usd") is None else NO_EXIT_PRICE
        )
        # Incomplete (wall-clock exists but not complete) is explicit.
        if exit_bar is None and any(b["open_ms"] >= due_ms for b in bars):
            reason = EXIT_BAR_INCOMPLETE
        return await _persist_unavailable(
            repository, HedgeOutcome, snapshot_id, strategy, horizon_days,
            evidence_version, cost_hash, outcome_id, as_of_ms,
            snapshot_as_of, due_ms, fcs, reason,
            funding_event_count=None, funding_coverage=None,
        )
    if exit_quote is None:
        if delisted:
            return await _settle_censored_early_exit(
                repository, HedgeOutcome, snapshot_id, strategy,
                horizon_days, evidence_version, cost_hash, outcome_id,
                as_of_ms, snapshot_as_of, due_ms, fcs, futures_notional,
                futures_qty, spot_qty, futures_entry_usd, entry_spot_usd,
                entry_bar, entry_quote, bars, market_provider, policy,
                venue, canonical_id, symbol, HEDGE_FORMULA_VERSION,
            )
        reason = NO_MARKET_PROVIDER if market_provider is None else NO_EXIT_QUOTE
        if await _future_quote_exists(
            market_provider, canonical_id, venue, spot_qty_s, due_ms, as_of_ms
        ):
            reason = FUTURE_QUOTE_NOT_USED
        return await _persist_unavailable(
            repository, HedgeOutcome, snapshot_id, strategy, horizon_days,
            evidence_version, cost_hash, outcome_id, as_of_ms,
            snapshot_as_of, due_ms, fcs, reason,
            funding_event_count=None, funding_coverage=None,
        )
    if exit_quote["quote_to_usd"] is None:
        if delisted:
            return await _settle_censored_early_exit(
                repository, HedgeOutcome, snapshot_id, strategy,
                horizon_days, evidence_version, cost_hash, outcome_id,
                as_of_ms, snapshot_as_of, due_ms, fcs, futures_notional,
                futures_qty, spot_qty, futures_entry_usd, entry_spot_usd,
                entry_bar, entry_quote, bars, market_provider, policy,
                venue, canonical_id, symbol, HEDGE_FORMULA_VERSION,
            )
        return await _persist_unavailable(
            repository, HedgeOutcome, snapshot_id, strategy, horizon_days,
            evidence_version, cost_hash, outcome_id, as_of_ms,
            snapshot_as_of, due_ms, fcs, QUOTE_FX_MISSING,
            funding_event_count=None, funding_coverage=None,
        )
    exit_spot_usd = _quote_exit_price(exit_quote)
    with localcontext() as ctx:
        ctx.prec = 80
        exit_fut_usd = exit_bar["native_close"] * exit_bar["fx_to_usd"] / multiplier  # type: ignore[operator]
    if exit_spot_usd is None or exit_spot_usd <= 0 \
            or exit_fut_usd is None or exit_fut_usd <= 0:
        if delisted:
            return await _settle_censored_early_exit(
                repository, HedgeOutcome, snapshot_id, strategy,
                horizon_days, evidence_version, cost_hash, outcome_id,
                as_of_ms, snapshot_as_of, due_ms, fcs, futures_notional,
                futures_qty, spot_qty, futures_entry_usd, entry_spot_usd,
                entry_bar, entry_quote, bars, market_provider, policy,
                venue, canonical_id, symbol, HEDGE_FORMULA_VERSION,
            )
        return await _persist_unavailable(
            repository, HedgeOutcome, snapshot_id, strategy, horizon_days,
            evidence_version, cost_hash, outcome_id, as_of_ms,
            snapshot_as_of, due_ms, fcs, NO_EXIT_PRICE,
            funding_event_count=None, funding_coverage=None,
        )

    # Settled funding over (entry, exit] (then-mark * then-FX; never 0-fill).
    raw_funding: Any = ()
    if market_provider is not None:
        try:
            raw_funding = await market_provider.read_settled_funding(
                symbol, snapshot_as_of, min(as_of_ms, due_ms + 2 * HOUR_MS), None
            )
        except Exception:
            raw_funding = ()
    completed_at = getattr(market_provider, 'completed_at_ms', None)
    if isinstance(completed_at, int) and completed_at > as_of_ms:
        as_of_ms = completed_at
    fundings: list[dict[str, Any]] = []
    for raw in list(raw_funding or ()):
        norm = _norm_funding(raw)
        if norm is None:
            continue
        if norm["known_at_ms"] is not None and norm["known_at_ms"] > as_of_ms:
            continue
        if snapshot_as_of < norm["funding_time_ms"] <= due_ms:
            fundings.append(norm)
    fundings.sort(key=lambda e: e["funding_time_ms"])
    # Funding with a missing mark/FX voids the carry (price legs kept).
    bad_marks = [e for e in fundings
                 if e["mark_price"] is None or e["mark_price"] <= 0]
    bad_fx = [e for e in fundings
              if e["fx_to_usd"] is None] if not bad_marks else []
    expected_n = max(1, horizon_days * 3)  # 8h cadence baseline
    coverage = min(1.0, len(fundings) / expected_n) if expected_n else 1.0
    gaps_ok = _funding_gaps_ok(fundings, snapshot_as_of, due_ms)
    if bad_marks or bad_fx or not gaps_ok:
        if delisted:
            return await _settle_censored_early_exit(
                repository, HedgeOutcome, snapshot_id, strategy,
                horizon_days, evidence_version, cost_hash, outcome_id,
                as_of_ms, snapshot_as_of, due_ms, fcs, futures_notional,
                futures_qty, spot_qty, futures_entry_usd, entry_spot_usd,
                entry_bar, entry_quote, bars, market_provider, policy,
                venue, canonical_id, symbol, HEDGE_FORMULA_VERSION,
                funding_event_count=len(fundings), funding_coverage=coverage,
                funding_reason=(
                    FUNDING_MARK_MISSING if bad_marks
                    else FUNDING_FX_MISSING if bad_fx
                    else FUNDING_HISTORY_INCOMPLETE
                ),
            )
        reason = (FUNDING_MARK_MISSING if bad_marks
                  else FUNDING_FX_MISSING if bad_fx
                  else FUNDING_HISTORY_INCOMPLETE)
        return await _persist_unavailable(
            repository, HedgeOutcome, snapshot_id, strategy, horizon_days,
            evidence_version, cost_hash, outcome_id, as_of_ms,
            snapshot_as_of, due_ms, fcs, reason,
            funding_event_count=len(fundings), funding_coverage=coverage,
        )

    # Full PnL settlement (complete legs only).
    return await _settle_complete(
        repository, HedgeOutcome, snapshot_id, strategy, horizon_days,
        evidence_version, cost_hash, outcome_id, as_of_ms,
        snapshot_as_of, due_ms, fcs, futures_notional, futures_qty,
        spot_qty, futures_entry_usd, entry_spot_usd, exit_fut_usd,
        exit_spot_usd, entry_bar, exit_bar, entry_quote, exit_quote,
        bars, fundings, coverage, market_provider, policy, venue,
        canonical_id, symbol, HEDGE_FORMULA_VERSION, censored=delisted,
    )


def _first_bar_at_or_after(
    bars: list[dict[str, Any]], anchor_ms: int, as_of_ms: int
) -> dict[str, Any] | None:
    """First bar with ``open_ms >= anchor`` complete at grading time."""
    for bar in bars:
        if bar["open_ms"] >= anchor_ms and bar["close_ms"] <= as_of_ms:
            return bar
    return None


async def _find_quote(
    provider: Any, canonical_id: str, venue: str, qty_s: str,
    at_ms: int, grading_ms: int, max_skew_ms: int = 5000,
) -> dict[str, Any] | None:
    if provider is None:
        return None
    fn = getattr(provider, "find_frozen_quote", None)
    if not callable(fn):
        return None
    try:
        raw = await fn(canonical_id, venue, qty_s, at_ms, max_skew_ms)
    except TypeError:
        try:
            raw = await fn(canonical_id, venue, qty_s, at_ms)
        except Exception:
            return None
    except Exception:
        return None
    norm = _norm_quote(raw)
    if norm is None:
        return None
    # Identity/venue must match; the provider already enforces the skew
    # window, but a future known_at can never price the past.
    if norm["canonical_id"] is not None and norm["canonical_id"] != canonical_id:
        return None
    if norm["venue"] is not None and norm["venue"] != venue:
        return None
    if norm["fetched_at_ms"] is not None and norm["fetched_at_ms"] > grading_ms:
        return None
    if norm["as_of_ms"] is not None:
        if norm["as_of_ms"] > at_ms or norm["as_of_ms"] < at_ms - max_skew_ms:
            return None
    # Quantity must match exactly (Decimal equality; "0.2" == "0.20").
    want = _parse_dec("want_qty", qty_s)
    got_raw = norm.get("requested_canonical_qty")
    got = _parse_dec("got_qty", got_raw) if got_raw is not None else None
    if want is None or got is None or want != got:
        return None
    # Only usable quotes (missing price handled by the caller as UNAVAILABLE
    # vs CENSORED); expired/non-OK quotes are unusable here.
    return norm


async def _future_quote_exists(
    provider: Any, canonical_id: str, venue: str, qty_s: str,
    at_ms: int, grading_ms: int,
) -> bool:
    """Whether only a *future* quote exists (must never be backfilled)."""
    if provider is None:
        return False
    # Probe with a wide skew: a hit beyond the frozen window proves the
    # archive holds a future quote the grader correctly refused to use.
    fn = getattr(provider, "find_frozen_quote", None)
    if not callable(fn):
        return False
    try:
        raw = await fn(canonical_id, venue, qty_s, at_ms + 30 * DAY_MS, 30 * DAY_MS)
    except Exception:
        return False
    norm = _norm_quote(raw)
    if norm is None or norm["as_of_ms"] is None:
        return False
    return bool(norm["as_of_ms"] > at_ms)


def _quote_entry_price(quote: Mapping[str, Any]) -> Decimal | None:
    buy = quote.get("buy_vwap")
    mid = quote.get("mid_price")
    fx = quote.get("quote_to_usd")
    if fx is None:
        return None
    raw = buy if buy is not None else mid
    if raw is None or raw <= 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 80
        return raw * fx  # type: ignore[operator]


def _quote_exit_price(quote: Mapping[str, Any]) -> Decimal | None:
    sell = quote.get("sell_vwap")
    mid = quote.get("mid_price")
    fx = quote.get("quote_to_usd")
    if fx is None:
        return None
    raw = sell if sell is not None else mid
    if raw is None or raw <= 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 80
        return raw * fx  # type: ignore[operator]


def _funding_gaps_ok(
    fundings: list[dict[str, Any]], start_ms: int, end_ms: int
) -> bool:
    """Funding timeline completeness: no >24h edge/interior gap."""
    gap = 24 * HOUR_MS
    if not fundings:
        return (end_ms - start_ms) <= gap
    times = sorted(e["funding_time_ms"] for e in fundings)
    if times[0] - start_ms > gap or end_ms - times[-1] > gap:
        return False
    return not any(b - a > gap for a, b in zip(times, times[1:]))


def _drawdown_if_complete(
    bars: list[dict[str, Any]], entry_ms: int, exit_ms: int,
    futures_qty: Decimal, spot_qty: Decimal,
    futures_entry: Decimal, spot_entry: Decimal,
    multiplier: Decimal = Decimal("1"),
) -> tuple[str | None, str | None]:
    """A futures-only series cannot establish synchronized two-leg risk.

    Until archived spot bars on the same timestamps are available, a
    hedged portfolio must report unknown risk rather than zero drawdown.
    Unhedged futures can still be measured from their own complete series.

    R14b/D14.2: only fully-contained bars (open >= entry, close <= exit)
    count; straddling head/tail hours never contribute extremes.
    """
    if spot_qty != 0:
        return None, None
    # R14b: full containment (open >= entry, close <= exit).
    window = [
        b for b in bars
        if int(b["open_ms"]) >= int(entry_ms) and int(b["close_ms"]) <= int(exit_ms)
    ]
    # Completeness: the single frozen timeline must span the holding
    # window with no gap > 25h. A lone entry/exit pair 7d apart (168h gap)
    # voids the drawdown while the outcome can still be COMPLETE; daily
    # bars (24h) keep it complete. Async spot/perp extremes are never
    # stitched -- both legs share this one timeline.
    if len(window) < 2:
        return None, None
    ordered = sorted(window, key=lambda b: (b["open_ms"], b["close_ms"]))
    closes: list[Decimal] = []
    for bar in ordered:
        if bar["native_close"] is None or bar["fx_to_usd"] is None:
            return None, None
        with localcontext() as ctx:
            ctx.prec = 80
            closes.append(bar["native_close"] * bar["fx_to_usd"] / multiplier)  # type: ignore[operator]
    times = [b["close_ms"] for b in ordered]
    if any(b - a > 25 * HOUR_MS for a, b in zip(times, times[1:])):
        return None, None
    with localcontext() as ctx:
        ctx.prec = 80
        values = [
            (spot_qty * (px - spot_entry)) + (futures_qty * (futures_entry - px))
            for px in closes
        ]
        peak = values[0]
        worst = Decimal("0")
        for value in values[1:]:
            if value > peak:
                peak = value
            draw = peak - value
            if draw > worst:
                worst = draw
        # Adverse basis: worst negative excursion of the hedged basis.
        adverse = min([min(values), Decimal("0")])
        # Normalise -0 to 0.
        if worst == 0:
            draw_s: str | None = "0"
        else:
            draw_s = _dec_str(worst)
        adv_s: str | None = _dec_str(-adverse) if adverse < 0 else "0"
    return draw_s, adv_s


async def _mark_path_coverage(
    provider: Any, symbol: str, entry_ms: int, exit_ms: int, grading_ms: int,
) -> str:
    """R14b/D14.2 MARK liquidation-path coverage (COMPLETE/PARTIAL/UNKNOWN).

    CR16: actually async-read then-known MARK bars and validate
    ``price_basis``. Binding alone never grants coverage credit; TRADE bars
    never masquerade as MARK.

    - provider without callable ``read_mark_price_bars`` (or explicitly
      unbound ``mark_price_bars_fn is None``) -> UNKNOWN (no read);
    - read error / exception -> UNKNOWN;
    - reader executed but empty / all non-MARK / window mismatch / gap ->
      PARTIAL (never COMPLETE);
    - fully-contained MARK coverage with no gap -> COMPLETE else PARTIAL.
    Purely informational (never invents drawdown); old readers ignore it.
    """
    try:
        from diveintocrypto_desktop.shortlab.evidence.evaluation import (
            path_coverage as _coverage,
        )
    except Exception:
        return "UNKNOWN"
    if provider is None:
        return "UNKNOWN"
    fn = getattr(provider, "read_mark_price_bars", None)
    if not callable(fn):
        return "UNKNOWN"
    # Explicitly unbound MARK history (RepositoryHistoricalMarketProvider
    # with mark_price_bars_fn=None) stays UNKNOWN; callers must never fall
    # back to TRADE bars. Binding alone (without a successful MARK read)
    # never grants coverage credit.
    try:
        if "mark_price_bars_fn" in dir(provider):
            if getattr(provider, "mark_price_bars_fn") is None:
                return "UNKNOWN"
    except Exception:
        pass
    try:
        entry_i = int(entry_ms)
        exit_i = int(exit_ms)
        grade_i = int(grading_ms)
    except (TypeError, ValueError):
        return "UNKNOWN"
    # Actually read then-known MARK bars (proves the reader executed; the
    # test fakes count invocations). Window is the liquidation-path window
    # [entry, exit]; the provider filters to fully-inside bars.
    try:
        try:
            raw = await fn(symbol, entry_i, exit_i, None)
        except TypeError:
            raw = await fn(symbol, entry_i, exit_i)
    except Exception:
        return "UNKNOWN"
    # Network history is learned at fetch completion (same as TRADE bars).
    try:
        completed = getattr(provider, "completed_at_ms", None)
        if isinstance(completed, int) and completed > grade_i:
            grade_i = completed
    except Exception:
        pass
    mark_bars: list[dict[str, Any]] = []
    try:
        items = list(raw or ())
    except TypeError:
        return "PARTIAL"
    for item in items:
        # CR16: validate price_basis -- only MARK feeds the liquidation
        # path; TRADE (or missing/legacy basis) is discarded, never
        # substituted.
        basis_raw = _field(item, "price_basis", "priceBasis", "basis")
        if not isinstance(basis_raw, str) or basis_raw.strip().upper() != "MARK":
            continue
        norm = _norm_bar(item)
        if norm is None:
            continue
        if norm.get("price_basis") != "MARK":
            continue
        # Only then-known bars (known after grading can never price the past).
        known = norm.get("known_at_ms")
        if known is not None:
            try:
                if int(known) > grade_i:  # type: ignore[arg-type]
                    continue
            except (TypeError, ValueError):
                continue
        mark_bars.append(norm)
    if not mark_bars:
        # Reader executed but yielded no usable MARK (empty / all TRADE /
        # window mismatch): never COMPLETE.
        return "PARTIAL"
    try:
        return str(_coverage(mark_bars, entry_i, exit_i, has_mark=True))
    except Exception:
        return "UNKNOWN"


async def _persist_unavailable(
    repository: Any, dto_cls: Any, snapshot_id: str, strategy: str,
    horizon_days: int, evidence_version: str, cost_hash: str,
    outcome_id: str, as_of_ms: int, snapshot_as_of: int, due_ms: int,
    fcs: Mapping[str, Any], reason: str,
    funding_event_count: int | None = None,
    funding_coverage: float | None = None,
) -> Any:
    outcome_json = {
        "fcs_snapshot_id": snapshot_id,
        "strategy": strategy,
        "horizon_days": horizon_days,
        "snapshot_as_of_ms": snapshot_as_of,
        "due_ms": due_ms,
        "frozen": False,
        "reason": reason,
        "funding_event_count": funding_event_count,
        "funding_coverage": funding_coverage,
        "fcs_version": fcs.get("fcs_version"),
        "fcs_config_hash": fcs.get("fcs_config_hash"),
    }
    record = dto_cls(
        outcome_id=outcome_id,
        fcs_snapshot_id=snapshot_id,
        strategy=strategy,  # type: ignore[arg-type]
        horizon_days=horizon_days,  # type: ignore[arg-type]
        outcome_status="UNAVAILABLE",  # type: ignore[arg-type]
        reason_code=reason,
        evidence_version=evidence_version,
        cost_config_hash=cost_hash,
        outcome_json=outcome_json,
        updated_at_ms=as_of_ms,
    )
    await _save_outcome_best_effort(repository, record, [])
    return record


async def _persist_censored_no_bars(
    repository: Any, dto_cls: Any, snapshot_id: str, strategy: str,
    horizon_days: int, evidence_version: str, cost_hash: str,
    outcome_id: str, as_of_ms: int, snapshot_as_of: int, due_ms: int,
    fcs: Mapping[str, Any], futures_notional: Decimal,
) -> Any:
    outcome_json = {
        "fcs_snapshot_id": snapshot_id,
        "strategy": strategy,
        "horizon_days": horizon_days,
        "snapshot_as_of_ms": snapshot_as_of,
        "due_ms": due_ms,
        "frozen": True,
        "futures_notional_usd": _dec_str(futures_notional),
        "fcs_version": fcs.get("fcs_version"),
        "fcs_config_hash": fcs.get("fcs_config_hash"),
    }
    record = dto_cls(
        outcome_id=outcome_id,
        fcs_snapshot_id=snapshot_id,
        strategy=strategy,  # type: ignore[arg-type]
        horizon_days=horizon_days,  # type: ignore[arg-type]
        outcome_status="CENSORED",  # type: ignore[arg-type]
        reason_code=CONTRACT_DELISTED,
        evidence_version=evidence_version,
        cost_config_hash=cost_hash,
        outcome_json=outcome_json,
        updated_at_ms=as_of_ms,
    )
    await _save_outcome_best_effort(repository, record, [])
    return record


async def _settle_censored_early_exit(
    repository: Any, dto_cls: Any, snapshot_id: str, strategy: str,
    horizon_days: int, evidence_version: str, cost_hash: str,
    outcome_id: str, as_of_ms: int, snapshot_as_of: int, due_ms: int,
    fcs: Mapping[str, Any], futures_notional: Decimal,
    futures_qty: Decimal, spot_qty: Decimal,
    futures_entry: Decimal, spot_entry: Decimal,
    entry_bar: Mapping[str, Any] | None, entry_quote: Mapping[str, Any] | None,
    bars: list[dict[str, Any]], provider: Any, policy: Any,
    venue: str, canonical_id: str, symbol: str, formula_version: str,
    funding_event_count: int | None = None,
    funding_coverage: float | None = None,
    funding_reason: str | None = None,
) -> Any:
    """CENSORED row retaining the frozen entry (delisted, never dropped)."""
    rates = _policy_cost_rates(policy)
    leverage = _policy_leverage(policy)
    with localcontext() as ctx:
        ctx.prec = 80
        spot_notional = spot_qty * spot_entry
        margin = futures_notional / leverage if leverage > 0 else Decimal("0")
        fees = (futures_notional * (rates["futures_entry_fee_rate"] + rates["futures_exit_fee_rate"])
                + spot_notional * (rates["spot_entry_fee_rate"] + rates["spot_exit_fee_rate"]))
        capital = spot_notional + margin + fees
    outcome_json = {
        "fcs_snapshot_id": snapshot_id,
        "strategy": strategy,
        "horizon_days": horizon_days,
        "snapshot_as_of_ms": snapshot_as_of,
        "due_ms": due_ms,
        "frozen": True,
        "entry": {
            "futures_qty": _dec_str(futures_qty / fcs["_contract_multiplier"]),
            "canonical_futures_qty": _dec_str(futures_qty),
            "contract_multiplier": _dec_str(fcs["_contract_multiplier"]),
            "spot_qty": _dec_str(spot_qty),
            "futures_entry_price_usd": _dec_str(futures_entry),
            "futures_native_entry_price_usd": _dec_str(futures_entry * fcs["_contract_multiplier"]),
            "spot_entry_price_usd": _dec_str(spot_entry),
            "futures_notional_usd": _dec_str(futures_notional),
            "spot_notional_usd": _dec_str(spot_notional),
            "capital_at_risk_usd": _dec_str(capital),
            "spot_cash_usd": _dec_str(spot_notional),
            "futures_margin_usd": _dec_str(margin),
            "venue": venue,
            "entry_quote_snapshot_id": (entry_quote or {}).get("snapshot_id"),
            "entry_bar_open_ms": (entry_bar or {}).get("open_ms"),
            "quote_to_usd": str((entry_quote or {}).get("quote_to_usd") or ""),
            "fcs_version": fcs.get("fcs_version"),
            "fcs_config_hash": fcs.get("fcs_config_hash"),
            "cost_config_hash": cost_hash,
            "evidence_version": evidence_version,
            "hedge_formula_version": formula_version,
        },
        "exit": None,
        "pnl": {
            "spot_pnl_usd": None,
            "futures_pnl_usd": None,
            "basis_pnl_usd": None,
            "funding_carry_usd": None,
            "fees_usd": _dec_str(fees),
            "slippage_usd": "0",
            "gas_usd": "0",
            "net_pnl_usd": None,
            "net_return": None,
        },
        "risk": {
            "max_portfolio_drawdown_usd": None,
            "max_adverse_basis_usd": None,
            "negative_funding_settlements": 0,
            "funding_coverage": funding_coverage,
            "funding_event_count": funding_event_count,
        },
        "funding_reason": funding_reason,
    }
    record = dto_cls(
        outcome_id=outcome_id,
        fcs_snapshot_id=snapshot_id,
        strategy=strategy,  # type: ignore[arg-type]
        horizon_days=horizon_days,  # type: ignore[arg-type]
        outcome_status="CENSORED",  # type: ignore[arg-type]
        reason_code=CONTRACT_DELISTED,
        evidence_version=evidence_version,
        cost_config_hash=cost_hash,
        outcome_json=outcome_json,
        updated_at_ms=as_of_ms,
    )
    refs = _pin_refs(entry_quote, None, repository)
    await _save_outcome_best_effort(repository, record, refs)
    return record


async def _settle_complete(
    repository: Any, dto_cls: Any, snapshot_id: str, strategy: str,
    horizon_days: int, evidence_version: str, cost_hash: str,
    outcome_id: str, as_of_ms: int, snapshot_as_of: int, due_ms: int,
    fcs: Mapping[str, Any], futures_notional: Decimal,
    futures_qty: Decimal, spot_qty: Decimal,
    futures_entry: Decimal, spot_entry: Decimal,
    exit_fut: Decimal, exit_spot: Decimal,
    entry_bar: Mapping[str, Any], exit_bar: Mapping[str, Any],
    entry_quote: Mapping[str, Any], exit_quote: Mapping[str, Any],
    bars: list[dict[str, Any]], fundings: list[dict[str, Any]],
    coverage: float, provider: Any, policy: Any, venue: str,
    canonical_id: str, symbol: str, formula_version: str,
    censored: bool = False,
) -> Any:
    rates = _policy_cost_rates(policy)
    leverage = _policy_leverage(policy)
    with localcontext() as ctx:
        ctx.prec = 80
        spot_notional = spot_qty * spot_entry
        margin = futures_notional / leverage if leverage > 0 else Decimal("0")
        # CR18/D14.2: true two-leg fees on actual entry/exit notionals
        # (FX1, no VWAP invention). Exit legs follow exit notionals; the
        # legacy entry-notional total (entry price for both legs) is removed
        # because it understated the exit leg whenever exit != entry.
        futures_exit_notional = exit_fut * futures_qty
        spot_exit_notional = exit_spot * spot_qty
        fut_entry_fee = futures_notional * rates["futures_entry_fee_rate"]
        fut_exit_fee = futures_exit_notional * rates["futures_exit_fee_rate"]
        spot_entry_fee = spot_notional * rates["spot_entry_fee_rate"]
        spot_exit_fee = spot_exit_notional * rates["spot_exit_fee_rate"]
        fees_exit_based = fut_entry_fee + fut_exit_fee + spot_entry_fee + spot_exit_fee
        fees = fees_exit_based
        gas_entry = entry_quote.get("estimated_gas_usd") or Decimal("0")
        gas_exit = exit_quote.get("estimated_gas_usd") or Decimal("0")
        gas = gas_entry + gas_exit
        # Slippage from quote bps when present, else 0 (never invented).
        slip = Decimal("0")
        # Funding carry: then-qty * then-mark(FX) * rate (short receives +).
        carry = Decimal("0")
        negatives = 0
        for event in fundings:
            with localcontext() as ctx2:
                ctx2.prec = 80
                mark_usd = event["mark_price"] * event["fx_to_usd"] / fcs["_contract_multiplier"]  # type: ignore[operator]
                carry += event["rate"] * mark_usd * futures_qty
            if event["rate"] < 0:
                negatives += 1
        futures_pnl = (futures_entry - exit_fut) * futures_qty
        spot_pnl = (exit_spot - spot_entry) * spot_qty
        basis_pnl = futures_pnl + spot_pnl
        # CR18: net consumes true per-leg entry/exit-amount fees
        # (fees_exit_based); capital denominator stays frozen
        # (spot cash + futures margin + cost reserve), never one margin leg.
        net = basis_pnl + carry - fees_exit_based - slip - gas
        capital = spot_notional + margin + fees_exit_based + gas
        net_return = (net / capital) if capital != 0 else None
        fut_return = (net / futures_notional) if futures_notional != 0 else None
    # CR16: MARK liquidation-path coverage from a real MARK read (async).
    path_coverage = await _mark_path_coverage(
        provider, symbol, int(entry_bar["open_ms"]),
        int(exit_bar["open_ms"]), int(as_of_ms),
    )
    drawdown_s, adverse_s = _drawdown_if_complete(
        bars, int(entry_bar["open_ms"]), int(exit_bar["open_ms"]),
        futures_qty, spot_qty, futures_entry, spot_entry, fcs["_contract_multiplier"],
    )
    outcome_json = {
        "fcs_snapshot_id": snapshot_id,
        "strategy": strategy,
        "horizon_days": horizon_days,
        "snapshot_as_of_ms": snapshot_as_of,
        "due_ms": due_ms,
        "frozen": True,
        "entry": {
            "futures_qty": _dec_str(futures_qty / fcs["_contract_multiplier"]),
            "canonical_futures_qty": _dec_str(futures_qty),
            "contract_multiplier": _dec_str(fcs["_contract_multiplier"]),
            "spot_qty": _dec_str(spot_qty),
            "futures_entry_price_usd": _dec_str(futures_entry),
            "futures_native_entry_price_usd": _dec_str(futures_entry * fcs["_contract_multiplier"]),
            "spot_entry_price_usd": _dec_str(spot_entry),
            "futures_notional_usd": _dec_str(futures_notional),
            "spot_notional_usd": _dec_str(spot_notional),
            "capital_at_risk_usd": _dec_str(capital),
            "spot_cash_usd": _dec_str(spot_notional),
            "futures_margin_usd": _dec_str(margin),
            "cost_reserve_usd": _dec_str(fees_exit_based + gas),
            "venue": venue,
            "canonical_id": canonical_id,
            "symbol": symbol,
            "entry_quote_snapshot_id": entry_quote.get("snapshot_id"),
            "entry_bar_open_ms": entry_bar.get("open_ms"),
            "entry_bar_close_ms": entry_bar.get("close_ms"),
            "entry_bar_source": entry_bar.get("source"),
            "entry_bar_known_at_ms": entry_bar.get("known_at_ms"),
            "entry_fx_to_usd": _dec_str(entry_bar["fx_to_usd"]) if entry_bar.get("fx_to_usd") is not None else None,  # type: ignore[arg-type]
            "entry_quote_to_usd": _dec_str(entry_quote["quote_to_usd"]) if entry_quote.get("quote_to_usd") is not None else None,  # type: ignore[arg-type]
            "fcs_version": fcs.get("fcs_version"),
            "fcs_config_hash": fcs.get("fcs_config_hash"),
            "cost_config_hash": cost_hash,
            "evidence_version": evidence_version,
            "hedge_formula_version": formula_version,
        },
        "exit": {
            "futures_exit_price_usd": _dec_str(exit_fut),
            "spot_exit_price_usd": _dec_str(exit_spot),
            "exit_quote_snapshot_id": exit_quote.get("snapshot_id"),
            "exit_bar_open_ms": exit_bar.get("open_ms"),
            "exit_bar_close_ms": exit_bar.get("close_ms"),
            "exit_bar_source": exit_bar.get("source"),
        },
        "pnl": {
            "spot_pnl_usd": _dec_str(spot_pnl),
            "futures_pnl_usd": _dec_str(futures_pnl),
            "basis_pnl_usd": _dec_str(basis_pnl),
            "funding_carry_usd": _dec_str(carry),
            # CR18: fees_usd is the true exit-based total (exit follows
            # exit notional); detailed legs below sum to it.
            "fees_usd": _dec_str(fees_exit_based),
            # R14b detailed exit-based legs (exit follows exit notional).
            "futures_entry_fee_usd": _dec_str(fut_entry_fee),
            "futures_exit_fee_usd": _dec_str(fut_exit_fee),
            "spot_entry_fee_usd": _dec_str(spot_entry_fee),
            "spot_exit_fee_usd": _dec_str(spot_exit_fee),
            "entry_fee_usd": _dec_str(fut_entry_fee + spot_entry_fee),
            "exit_fee_usd": _dec_str(fut_exit_fee + spot_exit_fee),
            "fees_usd_exit_based": _dec_str(fees_exit_based),
            "futures_exit_notional_usd": _dec_str(futures_exit_notional),
            "spot_exit_notional_usd": _dec_str(spot_exit_notional),
            "slippage_usd": _dec_str(slip),
            "gas_usd": _dec_str(gas),
            "net_pnl_usd": _dec_str(net),
            "net_return": _dec_str(net_return) if net_return is not None else None,
            "futures_notional_return": _dec_str(fut_return) if fut_return is not None else None,
        },
        "risk": {
            "max_portfolio_drawdown_usd": drawdown_s,
            "max_adverse_basis_usd": adverse_s,
            "drawdown_reason": ("SYNCHRONIZED_SPOT_PATH_UNAVAILABLE"
                                if spot_qty != 0 else
                                "INCOMPLETE_FUTURES_PATH" if drawdown_s is None else None),
            # R14b/D14.2: MARK path without finer history is PARTIAL/UNKNOWN.
            # CR16: real MARK read above; TRADE never substitutes.
            "path_coverage": path_coverage,
            "negative_funding_settlements": negatives,
            "funding_coverage": coverage,
            "funding_event_count": len(fundings),
        },
    }
    record = dto_cls(
        outcome_id=outcome_id,
        fcs_snapshot_id=snapshot_id,
        strategy=strategy,  # type: ignore[arg-type]
        horizon_days=horizon_days,  # type: ignore[arg-type]
        outcome_status="CENSORED" if censored else "COMPLETE",  # type: ignore[arg-type]
        reason_code=CONTRACT_DELISTED if censored else None,
        evidence_version=evidence_version,
        cost_config_hash=cost_hash,
        outcome_json=outcome_json,
        updated_at_ms=as_of_ms,
    )
    refs = _pin_refs(entry_quote, exit_quote, repository)
    await _save_outcome_best_effort(repository, record, refs)
    return record


def _pin_refs(
    entry_quote: Mapping[str, Any] | None,
    exit_quote: Mapping[str, Any] | None,
    repository: Any,
) -> list[tuple[str, str, str]]:
    """Collect VENUE_QUOTE pins for entry/exit evidence (best effort)."""
    refs: list[tuple[str, str, str]] = []
    for quote, purpose in ((entry_quote, "entry-quote"),
                           (exit_quote, "exit-quote")):
        if not isinstance(quote, Mapping):
            continue
        snapshot_id = quote.get("snapshot_id")
        if isinstance(snapshot_id, str) and snapshot_id:
            refs.append(("VENUE_QUOTE", snapshot_id, purpose))
    return refs


async def _save_outcome_best_effort(
    repository: Any, record: Any, refs: list[Any]
) -> None:
    """Persist outcome and provenance atomically; propagate pin/write failure.

    Repository owns immutable idempotency. Never retry by discarding refs:
    that would publish a result whose retained evidence can be deleted.

    CR15: UNHEDGED_0/SYSTEM_POLICY live in strategy entries (sl_strategy_*
    allows all six); sl_hedge_outcome only stores the four fixed ratios.
    Their grading is still computed for same-Decision pairing (in-memory),
    but never persisted to the fixed-ratio outcome table.
    """
    try:
        _strat = getattr(record, "strategy", None)
        if isinstance(_strat, str) and _strat.upper() in ("UNHEDGED_0", "SYSTEM_POLICY"):
            return
    except Exception:
        pass
    await repository.save_hedge_outcome(record, refs)


# ---------------------------------------------------------------------------
# Background callback (H08 lazy-imports; never registers a Scheduler here).
# ---------------------------------------------------------------------------


async def run_due(context: Any, **overrides: Any) -> Any:
    """Grade due-but-ungraded (snapshot, strategy, horizon) triples.

    ``context`` is the F06 :class:`JobContext` (repository, config,
    clock_ms, request_budget, trace_id, data_dir). The market provider is
    resolved from ``overrides["market_provider"]`` or
    ``context.market_provider`` / ``context.hedge_market_provider`` (H08
    injects the real HistoricalMarketProvider; tests inject a Fake).
    Returns the existing :class:`JobStatus` shape with
    ``job_type="hedge_grader"``.
    """
    from diveintocrypto_desktop.shortlab.service import JobStatus

    repository = getattr(context, "repository", None)
    config = overrides.get("policy", overrides.get("config", None))
    if config is None:
        config = getattr(context, "config", None)
    clock = getattr(context, "clock_ms", None) or _now_ms
    if not callable(clock):
        clock = _now_ms
    try:
        now_ms = int(clock())
    except Exception:
        now_ms = _now_ms()
    trace = str(getattr(context, "trace_id", "") or f"hedge_grader-{now_ms}")
    job_id = trace
    limit = overrides.get("batch_size", overrides.get("limit", None))
    try:
        limit_n = int(limit) if limit else 100
    except (TypeError, ValueError):
        limit_n = 100
    if limit_n <= 0:
        limit_n = 100

    provider = overrides.get("market_provider", overrides.get("provider", None))
    if provider is None:
        for attr in ("market_provider", "hedge_market_provider",
                     "hedgeMarketProvider", "history_provider"):
            candidate = getattr(context, attr, None)
            if candidate is not None:
                provider = candidate
                break

    if repository is None:
        return JobStatus(
            job_id=job_id, job_type="hedge_grader", status="FAILED",
            stats={"graded": 0, "error": "NO_REPOSITORY"},
            error_code="NO_REPOSITORY",
        )

    started_ms = now_ms
    try:
        await repository.create_job_run(job_id, "hedge_grader", started_ms)
        created = True
    except Exception:
        created = False

    graded = 0
    complete = 0
    censored = 0
    unavailable = 0
    skipped_have_row = 0
    errors = 0
    candidates: list[tuple[int, str, str, str, int]] = []

    # Discovery: FCS snapshots in the default 180d window lacking a stored
    # outcome for a now-due (strategy, horizon). Oldest-due first.
    window_from = now_ms - 180 * DAY_MS
    fcs_rows: list[dict[str, Any]] = []
    try:
        offset = 0
        while True:
            try:
                page = await repository.list_fcs(None, 200, offset)
            except TypeError:
                page = await repository.list_fcs(symbol=None, limit=200, offset=offset)
            items = list(page or ())
            for item in items:
                norm = _normalise_fcs(item)
                try:
                    as_of = int(norm["as_of_ms"])
                except (TypeError, ValueError):
                    continue
                if as_of < window_from or as_of > now_ms:
                    # Keep out-of-window rows out of the batch, but never
                    # fail the pass when the store is legacy-shaped.
                    continue
                fcs_rows.append(norm)
            if len(items) < 200:
                break
            offset += 200
            if len(fcs_rows) >= limit_n * 4:
                break
    except Exception:
        fcs_rows = []

    cost_hash = resolve_cost_hash(config)
    # R14b/D15: R00 current only (no fallback literal).
    from diveintocrypto_desktop.shortlab.hedge import (
        HEDGE_EVIDENCE_VERSION_CURRENT as _EV,
    )

    evidence_version = _resolve_evidence_version(config)

    for fcs in fcs_rows:
        try:
            as_of = int(fcs["as_of_ms"])
            snapshot_id = str(fcs["snapshot_id"])
            symbol = str(fcs.get("symbol") or "")
        except (TypeError, ValueError):
            continue
        for strategy in sorted(list(STRATEGY_RATIOS) + [_SYSTEM_POLICY]):
            # SYSTEM_POLICY without an Entry is skipped here (graded via the
            # Entry discovery below when its USER_DECISION group exists).
            if strategy == _SYSTEM_POLICY:
                continue
            for horizon_days in HORIZON_DAYS:
                due_ms = as_of + horizon_days * DAY_MS
                if now_ms < due_ms:
                    continue
                candidates.append((due_ms, symbol, snapshot_id, strategy, horizon_days))
    # CR15: real Entry + completed-Task consumption. Discover strategy
    # entries (all four cohorts, same 180d window) and grade their due
    # (source, strategy, horizon) triples -- including UNHEDGED_0 and the
    # same-Decision SYSTEM_POLICY pairing -- via the same frozen history.
    # Completed quote tasks are consumed indirectly: entry/exit quotes come
    # from frozen venue snapshots (future quotes never backfilled).
    try:
        _list_entries = getattr(repository, "list_strategy_entries", None)
        if callable(_list_entries):
            for _cohort in ("RESEARCH_CANDIDATE", "EXECUTABLE_DIRECTIONAL", "FUNDING_CARRY", "USER_DECISION"):
                try:
                    _rows = await _list_entries(_cohort, int(window_from), int(now_ms))
                except TypeError:
                    try:
                        _rows = await _list_entries(cohort=_cohort, start_ms=int(window_from), end_ms=int(now_ms))  # type: ignore[call-arg]
                    except Exception:
                        continue
                except Exception:
                    continue
                for _r in (_rows or ()):
                    try:
                        _src = str(_field(_r, "source_snapshot_id") or "")
                        _sym = str(_field(_r, "symbol") or "")
                        _strat = str(_field(_r, "strategy") or "").upper()
                        _asof = _field(_r, "decision_as_of_ms", "as_of_ms")
                        try:
                            _asof_i = int(_asof)  # type: ignore[arg-type]
                        except (TypeError, ValueError):
                            continue
                        if not _src or not _sym or not _strat:
                            continue
                        try:
                            _strat_n = normalize_strategy(_strat)
                        except ValueError:
                            continue
                        if _asof_i < window_from or _asof_i > now_ms:
                            continue
                        for _hz in HORIZON_DAYS:
                            _due = _asof_i + _hz * DAY_MS
                            if now_ms < _due:
                                continue
                            candidates.append((_due, _sym, _src, _strat_n, _hz))
                    except Exception:
                        continue
    except Exception:
        pass
    candidates.sort(key=lambda row: (row[0], row[1], row[2], row[3], row[4]))

    for _due_ms, _symbol, snapshot_id, strategy, horizon_days in candidates:
        if graded >= limit_n:
            break
        try:
            have = await _find_existing_outcome(
                repository, snapshot_id, strategy, horizon_days,
                str(_EV), cost_hash,
            )
        except Exception:
            have = None
        if have is not None:
            skipped_have_row += 1
            continue
        try:
            from diveintocrypto_desktop.shortlab.request_budget import make_request_context, scoped_request_context
            request_context = make_request_context(
                getattr(context, 'request_budget', None), job_type='background',
                host='fapi', trace_id=trace,
            )
            with scoped_request_context(request_context):
                outcome = await grade_hedge(
                    snapshot_id, strategy, horizon_days, now_ms,
                    repository, provider, config,
                )
        except Exception:
            errors += 1
            continue
        graded += 1
        status = str(getattr(outcome, "outcome_status", ""))
        if status == "COMPLETE":
            complete += 1
        elif status == "CENSORED":
            censored += 1
        elif status == "UNAVAILABLE":
            unavailable += 1

    finished_ms: int
    try:
        finished_ms = int(clock())
    except Exception:
        finished_ms = _now_ms()
    stats = {
        "graded": graded,
        "complete": complete,
        "censored": censored,
        "unavailable": unavailable,
        "skippedHaveRow": skipped_have_row,
        "errors": errors,
        "windowDays": 180,
    }
    try:
        if created:
            await repository.finish_job_run(
                job_id, "SUCCEEDED", finished_ms, stats=stats
            )
        else:
            try:
                await repository.finish_job_run(
                    job_id, "SUCCEEDED", finished_ms, stats=stats
                )
            except Exception:
                pass
    except Exception:
        pass
    return JobStatus(
        job_id=job_id,
        job_type="hedge_grader",
        status="SUCCEEDED",
        stats=stats,
        started_at_ms=started_ms,
        finished_at_ms=finished_ms,
    )
