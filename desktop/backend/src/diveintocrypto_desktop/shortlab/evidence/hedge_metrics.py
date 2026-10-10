"""H10 hedge evidence aggregation (design B39.2/B39.3 + B32.13 filters).

Pure domain module: reads archived strategy Entries and legacy FCS snapshots
plus H01 ``sl_hedge_outcome`` rows (never the directional outcome table,
never the live manual ledger). Entry cohorts stay separate and pair only on
the identical source snapshot. ``H08`` lazy-imports :func:`hedge_summary` and maps
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
``end_ms``/``endMs`` (FCS/Entry as-of window), ``cohort`` (one Entry cohort
or ``LEGACY_FCS``), ``now_ms`` (grading clock
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

_DAY_MS = 86_400_000

__all__ = ["hedge_summary", "paired_baseline_summary", "parse_hedge_filters"]


def paired_baseline_summary(
    system: Any | None = None,
    baseline: Any | None = None,
    pairs: Any | None = None,
) -> dict[str, Any]:
    """R14b paired baseline for hedge (identical FCS snapshot only).

    SYSTEM_POLICY and fixed ratios compare only the same (decision_id /
    fcs_snapshot_id, horizon) samples; missing legs stay missing, never
    market averages or cross-day substitution.
    """
    from diveintocrypto_desktop.shortlab.evidence.evaluation import (
        paired_baseline_diff as _paired,
        paired_baseline_summary as _summary,
    )

    if pairs is not None:
        return dict(_paired(list(pairs)))
    return dict(_summary(list(system or ()), list(baseline or ())))


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
            "UNHEDGED_0", "ABSOLUTE_100", "RELATIVE_75", "RELATIVE_50",
            "RELATIVE_25", "SYSTEM_POLICY",
        ):
            strategy = None  # unknown enum matches nothing downstream

    raw_cohort = _first("cohort")
    cohort: str | None = None
    if raw_cohort is not None:
        cohort = str(raw_cohort).strip().upper()
        if cohort not in (
            "RESEARCH_CANDIDATE", "EXECUTABLE_DIRECTIONAL", "FUNDING_CARRY",
            "USER_DECISION", "LEGACY_FCS",
        ):
            cohort = None

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
        "cohort": cohort,
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
        "_unknown_cohort": raw_cohort is not None and cohort is None,
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
    """Aggregate Entry-backed and legacy FCS outcomes to one summary.

    Entry samples are grouped by one of the four capture cohorts; source-
    matched FCS rows stay in the ``LEGACY_FCS`` bucket. The directional
    outcome store and user-ledger fill table are never consulted.
    """
    from diveintocrypto_desktop.shortlab.hedge.models import HedgeEvidenceSummary

    sel = parse_hedge_filters(filters)
    # Unknown strategy enum matches nothing (H08 surfaces 422); the DTO
    # still requires a valid strategy, so short-circuit to an empty bucket.
    # R14b/D15: R00 current only (no fallback literal).
    if sel["_unknown_strategy"] or sel["_unknown_cohort"]:
        from diveintocrypto_desktop.shortlab.hedge import (
            HEDGE_EVIDENCE_VERSION_CURRENT as _EV,
        )

        return HedgeEvidenceSummary(
            strategy="ABSOLUTE_100",  # type: ignore[arg-type]
            horizon_days=sel["horizon_days"],  # type: ignore[arg-type]
            sample_count=0,
            complete_count=0,
            censored_count=0,
            avg_net_return=None,
            median_net_return=None,
            evidence_version=str(_EV),
            cohort=sel["cohort"],
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
        # R14b/D15: R00 current only (no fallback literal).
        from diveintocrypto_desktop.shortlab.hedge import (
            HEDGE_EVIDENCE_VERSION_CURRENT as _EV2,
        )

        evidence_version = str(_EV2)

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
    all_fcs_by_id = {str(row["snapshot_id"]): row for row in fcs_rows if row.get("snapshot_id")}
    kept_fcs_ids = {str(row["snapshot_id"]) for row in kept if row.get("snapshot_id")}

    # -- collect new Entry cohorts and keep legacy FCS as its own bucket -----
    entry_rows: list[dict[str, Any]] = []
    baseline_entry_rows: list[dict[str, Any]] = []
    list_entry = getattr(repository, "list_strategy_entries", None)
    if callable(list_entry):
        if sel["cohort"] == "LEGACY_FCS":
            cohorts = ()
        elif sel["cohort"]:
            cohorts = (sel["cohort"],)
        else:
            cohorts = ("RESEARCH_CANDIDATE", "EXECUTABLE_DIRECTIONAL", "FUNDING_CARRY", "USER_DECISION")
        for entry_cohort in cohorts:
            try:
                rows = await list_entry(entry_cohort, int(sel["start_ms"] or 0), int(sel["end_ms"] or 2**62))
            except Exception:
                continue
            for row in rows or ():
                data = dict(row) if isinstance(row, Mapping) else {
                    key: _field(row, key) for key in (
                        "entry_id", "cohort", "symbol", "source_snapshot_id",
                        "strategy", "decision_as_of_ms", "entry_json",
                    )
                }
                entry_strategy = str(data.get("strategy") or "").upper()
                if entry_strategy not in (strategy, "UNHEDGED_0"):
                    continue
                if wanted_symbol and str(data.get("symbol") or "").upper() != wanted_symbol:
                    continue
                source_fcs = all_fcs_by_id.get(str(data.get("source_snapshot_id") or ""))
                if source_fcs is not None and str(data.get("source_snapshot_id")) not in kept_fcs_ids:
                    continue
                if source_fcs is None and any((sel["fcs_version"], sel["history_class"], sel["venue"])):
                    continue
                if source_fcs is not None:
                    if sel["fcs_version"] and source_fcs["fcs_version"] != sel["fcs_version"]:
                        continue
                    if sel["history_class"] and source_fcs["history_class"] != sel["history_class"]:
                        continue
                    if sel["venue"] and (source_fcs["venue"] or "") != sel["venue"].upper():
                        continue
                baseline_entry_rows.append(data)
                if entry_strategy == strategy:
                    entry_rows.append(data)

    # A source that has an Entry for this strategy is represented by its
    # Entry cohort(s), not double-counted again as legacy FCS.
    entry_sources = {str(r.get("source_snapshot_id") or "") for r in entry_rows}
    legacy_samples = []
    if sel["cohort"] in (None, "LEGACY_FCS"):
        legacy_samples = [r for r in kept if r["snapshot_id"] not in entry_sources]

    def _sample(row: Mapping[str, Any], cohort: str, source_id: str, as_of_ms: int) -> dict[str, Any]:
        return {"row": row, "cohort": cohort, "source_id": source_id,
                "as_of_ms": int(as_of_ms)}

    samples = [_sample(r, "LEGACY_FCS", str(r["snapshot_id"]), int(r["as_of_ms"]))
               for r in legacy_samples]
    for r in entry_rows:
        try:
            asof = int(r.get("decision_as_of_ms"))
        except (TypeError, ValueError):
            continue
        samples.append(_sample(r, str(r.get("cohort")), str(r.get("source_snapshot_id")), asof))

    # Resolve independent-asset labels once per distinct identity snapshot.
    # FCS already carries canonical_id; decision-backed entries resolve from
    # their frozen identity reference. Unknown identity remains one UNKNOWN
    # cluster and cannot inflate the breadth gate.
    canonical_by_source = {
        key: str(row.get("canonical_id") or "").strip().upper()
        for key, row in all_fcs_by_id.items()
    }
    identity_ids = sorted({
        str(entry_json.get("identity_snapshot_id"))
        for row in entry_rows
        for entry_json in (_parse_json_dict(row.get("entry_json")),)
        if entry_json.get("identity_snapshot_id")
    })
    identity_by_id: Mapping[str, Any] = {}
    get_identities = getattr(repository, "get_identity_snapshots", None)
    if identity_ids and callable(get_identities):
        try:
            identity_by_id = await get_identities(identity_ids)
        except Exception:
            identity_by_id = {}

    # -- outcome and same-source UNHEDGED_0 pairing -------------------------
    complete_returns: list[Decimal] = []
    complete_count = censored_count = unavailable_count = pending_count = 0
    paired_strategy: list[Decimal] = []
    paired_unhedged: list[Decimal] = []
    evaluation_rows: list[dict[str, Any]] = []
    evaluation_pairs: list[dict[str, Any]] = []
    for sample in samples:
        source_id = sample["source_id"]
        sample_row = sample["row"]
        sample_entry_id = str(sample_row.get("entry_id") or "") if isinstance(sample_row, Mapping) else ""
        outcome_key = sample_entry_id if sample["cohort"] != "LEGACY_FCS" and sample_entry_id else source_id
        legacy_outcome_key = (
            source_id if sample_entry_id == f"{source_id}:{strategy}" else None
        )
        outcome = await _matching_outcome(
            repository, outcome_key, strategy, horizon_days,
            str(evidence_version), str(cost_hash) if cost_hash else None,
            legacy_source_id=legacy_outcome_key,
        )
        status = str(outcome.get("outcome_status") or "") if outcome else ""
        ret = _outcome_net_return(outcome) if outcome else None
        evaluation_status = status if status in ("COMPLETE", "CENSORED", "UNAVAILABLE", "PENDING") else ""
        if status == "COMPLETE":
            complete_count += 1
            if ret is not None:
                complete_returns.append(ret)
        elif status == "CENSORED":
            censored_count += 1
        elif status == "UNAVAILABLE":
            unavailable_count += 1
        elif int(sel["now_ms"] if sel["now_ms"] is not None else now_ms) < sample["as_of_ms"] + horizon_days * _DAY_MS:
            pending_count += 1
        else:
            unavailable_count += 1
        if not evaluation_status:
            evaluation_status = "PENDING" if int(sel["now_ms"] if sel["now_ms"] is not None else now_ms) < sample["as_of_ms"] + horizon_days * _DAY_MS else "UNAVAILABLE"
        outcome_json = _parse_json_dict(outcome.get("outcome_json")) if outcome else {}
        entry_json = _parse_json_dict(sample_row.get("entry_json")) if isinstance(sample_row, Mapping) else {}
        source_fcs = all_fcs_by_id.get(source_id)
        canonical = str(sample_row.get("canonical_id") or "").strip().upper() if isinstance(sample_row, Mapping) else ""
        canonical = canonical or canonical_by_source.get(source_id, "")
        if not canonical:
            identity_id = str(entry_json.get("identity_snapshot_id") or "")
            identity = identity_by_id.get(identity_id) if identity_id else None
            canonical = str(_field(identity, "canonical_id") or "").strip().upper()
            if not canonical:
                canonical = str(_parse_json_dict(_field(identity, "identity_json")).get("canonical_id") or "").strip().upper()
        if not canonical:
            canonical = ""
        evaluation_rows.append({
            "status": evaluation_status,
            "net_return": float(ret) if ret is not None else None,
            "asset": canonical,
            "cohort": sample["cohort"],
            "decision_id": f"{sample['cohort']}:{source_id}",
            "as_of_ms": int(sample["as_of_ms"]),
            "entry_status": entry_json.get("status") or ("LEGACY_FCS" if sample["cohort"] == "LEGACY_FCS" else "UNKNOWN"),
            "goal": entry_json.get("goal"),
            "profile": entry_json.get("profile"),
            "formula_version": entry_json.get("formula_version"),
            "fcs_version": source_fcs.get("fcs_version") if source_fcs else None,
            "fcs_config_hash": source_fcs.get("fcs_config_hash") if source_fcs else None,
            "history_class": source_fcs.get("history_class") if source_fcs else None,
            "venue": source_fcs.get("venue") if source_fcs else None,
            "funding_coverage": (
                _parse_json_dict(outcome_json.get("risk")).get("funding_coverage")
                if outcome_json else None
            ),
            "pnl": outcome_json.get("pnl") if isinstance(outcome_json.get("pnl"), Mapping) else {},
            "risk": outcome_json.get("risk") if isinstance(outcome_json.get("risk"), Mapping) else {},
        })

        if strategy == "UNHEDGED_0":
            continue
        # Pair only within the exact cohort + source snapshot + horizon.
        cohort = sample["cohort"]
        baseline_entry = next((r for r in baseline_entry_rows
                               if str(r.get("cohort")) == cohort
                               and str(r.get("source_snapshot_id")) == source_id
                               and str(r.get("strategy") or "").upper() == "UNHEDGED_0"), None)
        if cohort == "LEGACY_FCS":
            continue
        baseline = None
        baseline_entry_id = str(baseline_entry.get("entry_id") or "") if isinstance(baseline_entry, Mapping) else ""
        baseline_key = baseline_entry_id or source_id
        legacy_baseline_key = source_id if baseline_entry_id == f"{source_id}:UNHEDGED_0" else None
        if baseline_entry is not None:
            baseline = await _matching_outcome(
                repository, baseline_key, "UNHEDGED_0", horizon_days,
                str(evidence_version), str(cost_hash) if cost_hash else None,
                legacy_source_id=legacy_baseline_key,
            )
        baseline_complete = baseline is not None and str(baseline.get("outcome_status")) == "COMPLETE"
        baseline_ret = _outcome_net_return(baseline) if baseline_complete and baseline is not None else None
        evaluation_pairs.append({
            "decision_id": f"{cohort}:{source_id}",
            "asset": canonical,
            "system": float(ret) if status == "COMPLETE" and ret is not None else None,
            "baseline": float(baseline_ret) if baseline_ret is not None else None,
            "cohort": cohort,
            "formula_version": evaluation_rows[-1].get("formula_version"),
            "profile": evaluation_rows[-1].get("profile"),
            "goal": evaluation_rows[-1].get("goal"),
            "history_class": evaluation_rows[-1].get("history_class"),
            "fcs_version": evaluation_rows[-1].get("fcs_version"),
            "fcs_config_hash": evaluation_rows[-1].get("fcs_config_hash"),
            "venue": evaluation_rows[-1].get("venue"),
        })
        if status == "COMPLETE" and baseline is not None \
                and str(baseline.get("outcome_status")) == "COMPLETE":
            if ret is not None and baseline_ret is not None:
                paired_strategy.append(ret)
                paired_unhedged.append(baseline_ret)

    sample_count = len(samples)
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

    pair_mean_strategy = pair_mean_unhedged = pair_mean_diff = None
    if paired_strategy:
        with localcontext() as ctx:
            ctx.prec = 80
            n = Decimal(len(paired_strategy))
            pair_mean_strategy = _dec_str(sum(paired_strategy, Decimal("0")) / n)
            pair_mean_unhedged = _dec_str(sum(paired_unhedged, Decimal("0")) / n)
            pair_mean_diff = _dec_str(sum((a - b for a, b in zip(paired_strategy, paired_unhedged)), Decimal("0")) / n)
    paired_count = len(paired_strategy)
    paired_missing_count = max(0, sample_count - paired_count) if strategy != "UNHEDGED_0" else 0
    from diveintocrypto_desktop.shortlab.evidence.evaluation import build_evaluation_report

    evaluation_report = build_evaluation_report(
        evaluation_rows,
        paired_pairs=evaluation_pairs if strategy != "UNHEDGED_0" else None,
    )
    # An unfiltered query can contain multiple independent populations even
    # when strategy and horizon match. Keep the complete evaluation key in
    # separate reports; the top-level is then counts-only and cannot make a
    # mixed-population return or bootstrap claim.
    dimension_fields = (
        "cohort", "formula_version", "profile", "goal", "history_class",
        "fcs_version", "fcs_config_hash", "venue",
    )

    def _bucket_key(row: Mapping[str, Any]) -> tuple[str, ...]:
        return tuple(str(row.get(field) or "UNKNOWN").strip() or "UNKNOWN"
                     for field in dimension_fields)

    grouped_rows: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for item in evaluation_rows:
        grouped_rows.setdefault(_bucket_key(item), []).append(item)
    grouped_pairs: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for item in evaluation_pairs:
        grouped_pairs.setdefault(_bucket_key(item), []).append(item)
    sub_buckets: list[dict[str, Any]] = []
    for key in sorted(grouped_rows):
        rows_in_bucket = grouped_rows[key]
        pair_rows = grouped_pairs.get(key, [])
        sub_report = build_evaluation_report(
            rows_in_bucket,
            paired_pairs=pair_rows if strategy != "UNHEDGED_0" else None,
        )
        sub_report["bucket"] = {
            field: value for field, value in zip(dimension_fields, key)
        } | {"strategy": strategy, "horizon_days": int(horizon_days),
             "evidence_version": str(evidence_version),
             "cost_config_hash": str(cost_hash) if cost_hash else "UNKNOWN"}
        sub_buckets.append({
            "bucket": sub_report["bucket"],
            "sample_count": len(rows_in_bucket),
            "complete_count": int(sub_report["status_counts"].get("COMPLETE", 0)),
            "censored_count": int(sub_report["status_counts"].get("CENSORED", 0)),
            "unavailable_count": int(sub_report["status_counts"].get("UNAVAILABLE", 0)),
            "pending_count": int(sub_report["status_counts"].get("PENDING", 0)),
            "evaluation": sub_report,
        })
    mixed_paired_count = sum(
        int(item["evaluation"].get("paired", {}).get("n_paired", 0) or 0)
        for item in sub_buckets
    )
    mixed_paired_missing_count = sum(
        int(item["evaluation"].get("paired", {}).get("n_missing", 0) or 0)
        for item in sub_buckets
    )
    evaluation_report["sub_buckets"] = sub_buckets
    mixed_populations = len(sub_buckets) > 1
    if mixed_populations:
        evaluation_report["sample_status"] = "MIXED_BUCKETS"
        evaluation_report["claim_valid"] = False
        evaluation_report["model_valid"] = False
        evaluation_report["mean_net"] = None
        evaluation_report["median_net"] = None
        evaluation_report["p05_net_return"] = None
        evaluation_report["bootstrap"] = {
            "status": "MIXED_BUCKETS", "mean": None, "ci_low": None,
            "ci_high": None, "n": 0, "n_assets": 0,
        }
        evaluation_report["paired"] = {
            "n_paired": mixed_paired_count,
            "n_missing": mixed_paired_missing_count,
            "missing_ratio": None,
            "mean_diff": None, "median_diff": None,
            "mean_system": None, "mean_baseline": None,
        }
        evaluation_report["baseline"] = dict(evaluation_report["paired"])
        evaluation_report["paired_bootstrap"] = {
            "status": "MIXED_BUCKETS", "mean": None, "ci_low": None,
            "ci_high": None, "n": 0, "n_assets": 0,
        }
        evaluation_report["known_costs"] = {
            "priced_outcomes": int(evaluation_report.get("known_costs", {}).get("priced_outcomes", 0)),
            "mean_fees_usd": None, "total_fees_usd": None,
        }
        evaluation_report["funding_mark_coverage"] = {
            "priced_outcomes": int(evaluation_report.get("funding_mark_coverage", {}).get("priced_outcomes", 0)),
            "mean": None,
        }
        evaluation_report["max_adverse_basis_usd"] = None
        evaluation_report["max_portfolio_drawdown_usd"] = None
        evaluation_report["cost_after_risk"] = None
        evaluation_report["risk"] = {"max_drawdown_usd": None, "reason": "MIXED_BUCKETS"}
        evaluation_report["walk_forward"] = {"status": "MIXED_BUCKETS", "months": []}
    def _dimension_values(name: str, default: str = "UNKNOWN") -> list[str]:
        values = sorted({
            str(row.get(name)).strip()
            for row in evaluation_rows
            if row.get(name) is not None and str(row.get(name)).strip()
        })
        return values or [default]

    evaluation_report["bucket"] = {
        "strategy": strategy,
        "cohort": sel["cohort"] or "MIXED_COHORTS",
        "horizon_days": int(horizon_days),
        "evidence_version": str(evidence_version),
        "formula_version": _dimension_values("formula_version"),
        "cost_config_hash": str(cost_hash) if cost_hash else "UNKNOWN",
        "fcs_version": _dimension_values("fcs_version"),
        "fcs_config_hash": _dimension_values("fcs_config_hash"),
        "history_class": _dimension_values("history_class"),
        "profile": _dimension_values("profile"),
        "goal": _dimension_values("goal"),
        "venue": _dimension_values("venue"),
    }
    return HedgeEvidenceSummary(
        strategy=strategy,  # type: ignore[arg-type]
        horizon_days=int(horizon_days),  # type: ignore[arg-type]
        sample_count=int(sample_count),
        complete_count=int(complete_count),
        censored_count=int(censored_count),
        avg_net_return=None if mixed_populations else avg_s,
        median_net_return=None if mixed_populations else median_s,
        evidence_version=str(evidence_version),
        cohort=sel["cohort"],
        unavailable_count=unavailable_count,
        pending_count=pending_count,
        paired_count=mixed_paired_count if mixed_populations else paired_count,
        paired_missing_count=mixed_paired_missing_count if mixed_populations else paired_missing_count,
        paired_mean_strategy_return=None if mixed_populations else pair_mean_strategy,
        paired_mean_unhedged_return=None if mixed_populations else pair_mean_unhedged,
        paired_mean_diff=None if mixed_populations else pair_mean_diff,
        evaluation_report=evaluation_report,
    )


async def _matching_outcome(
    repository: Any,
    fcs_snapshot_id: str,
    strategy: str,
    horizon_days: int,
    evidence_version: str,
    cost_hash: str | None,
    *,
    legacy_source_id: str | None = None,
) -> dict[str, Any] | None:
    fn = getattr(repository, "list_hedge_outcomes", None)
    if not callable(fn):
        return None
    keys = [fcs_snapshot_id]
    if legacy_source_id and legacy_source_id != fcs_snapshot_id:
        keys.append(legacy_source_id)
    for key in keys:
        try:
            rows = await fn(key, 200, 0)
        except Exception:
            continue
        for row in list(rows or ()):
            data = dict(row) if isinstance(row, Mapping) else {
                "fcs_snapshot_id": _field(row, "fcs_snapshot_id"),
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
                # Exact Entry ids are checked first. The source-id fallback
                # is reserved for legacy rows whose Entry id used the old
                # source:strategy form.
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
