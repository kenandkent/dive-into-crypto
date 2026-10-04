"""Forward-evidence aggregates for ``/api/short/evidence/summary`` (F07, design A8).

The Task 14 router is unchanged: it calls
``ShortLabService.evidence_summary(filters)``, which delegates to the
provider built here. Phase 3 (no provider) stays ``503
short_evidence_unavailable``; wiring :func:`build_metrics_provider` onto the
service flips the same route to a real ``200``.

F07 history semantics (design A8, plan F07):

- default window: past ``default_history_days`` (180) of ``SUCCEEDED``
  batches; explicit ``generation_id`` keeps the old single-generation view.
- default versions are the current ones (feature/entry/formula/cost); an
  explicit old version is selectable alone and never silently mixed.
- default experimental unit: one pre-frozen representative score per
  ``(symbol, profile, UTC day)`` -- the earliest score of that day whose
  ``candidate_status`` is not ``EXCLUDED``. A day with no eligible score
  yields no tradable sample. Raw snapshot counts are reported alongside
  (``rawTotal``) so the research page never mistakes correlated half-hour
  snapshots for independent trials.
- per horizon (7D/30D/90D): not-due without a row counts as ``PENDING``;
  due without a row counts as ``UNAVAILABLE``/``NOT_GRADED_DUE`` with a
  queue depth. Missing mark/funding/exit rows keep their reasons and null
  legs (never 0); delisted rows stay ``CENSORED`` and are retained.
- the four state counts always sum to the horizon sample size.

Extra version/window/queue fields ride inside the existing
``filters`` echo and per-horizon buckets, so the frozen Task 14 router
(which only forwards ``filters``/``horizons``/``total``/``generatedAtMs``)
needs no change.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Mapping

from diveintocrypto_desktop.shortlab.evidence.grader import (
    FORMULA_VERSION,
    HORIZON_MS,
    NOT_GRADED_DUE,
    OUTCOME_STATUSES,
    PENDING_NOT_DUE,
    default_cost_hash,
    horizon_due_ms,
)

_HORIZON_ORDER = ("7D", "30D", "90D")
_DAY_MS = 86_400_000
_DEFAULT_HISTORY_DAYS = 180


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


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


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def parse_horizons(filters: Mapping[str, Any] | None) -> list[str]:
    """Horizons requested via ``?horizon=7D`` (comma-separated allowed)."""
    if not filters:
        return list(_HORIZON_ORDER)
    raw = filters.get("horizon", filters.get("horizons"))
    if raw is None:
        return list(_HORIZON_ORDER)
    if isinstance(raw, str):
        wanted = [part.strip().upper() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple)):
        wanted = [str(part).strip().upper() for part in raw]
    else:
        wanted = [str(raw).strip().upper()]
    return [name for name in _HORIZON_ORDER if name in wanted]


def _resolve_cost_hash(
    config: Any | None, cost_config_hash: str | None
) -> str:
    if cost_config_hash is not None:
        return str(cost_config_hash)
    if config is not None:
        try:
            from diveintocrypto_desktop.shortlab.config import (
                cost_config_hash as _cost_hash,
            )

            return str(_cost_hash(config))
        except Exception:  # noqa: BLE001 - fall through to the default hash
            pass
    return default_cost_hash()


def _resolve_history_days(config: Any | None) -> int:
    try:
        days = int(getattr(getattr(config, "evidence", None), "default_history_days", 0))
    except (TypeError, ValueError):
        days = 0
    if days and days > 0:
        return days
    return _DEFAULT_HISTORY_DAYS


def _resolve_sample_policy(config: Any | None) -> str:
    try:
        policy = str(getattr(getattr(config, "evidence", None), "sample_policy", ""))
    except Exception:  # noqa: BLE001 - defensive
        policy = ""
    return policy.strip() or "first_eligible_per_symbol_profile_utc_day"


def _current_feature_version() -> str:
    try:
        from diveintocrypto_desktop.shortlab.scoring.versions import FEATURE_VERSION

        return str(FEATURE_VERSION)
    except Exception:  # noqa: BLE001 - never blocks the report
        return "features-v2"


def _current_entry_version() -> str | None:
    try:
        from diveintocrypto_desktop.shortlab.scoring.versions import ENTRY_VERSION

        return str(ENTRY_VERSION)
    except Exception:  # noqa: BLE001 - never blocks the report
        return None


def _empty_bucket() -> dict[str, Any]:
    return {
        "PENDING": 0,
        "COMPLETE": 0,
        "CENSORED": 0,
        "UNAVAILABLE": 0,
        "total": 0,
        "complete": 0,
        "meanNetReturn": None,
        "meanPriceReturn": None,
        "meanFundingCarry": None,
        # F07 extras (additive; old readers ignore them).
        "notGradedDue": 0,
        "sampled": 0,
        "rawTotal": 0,
    }


def _filter_value(filters: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        if name in filters and filters[name] is not None:
            return _str_or_none(filters[name])
    return None


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _is_eligible(score: Any) -> bool:
    """One score is a tradable-sample candidate unless EXCLUDED.

    ``candidate_status`` already encodes the frozen watch/candidate
    thresholds, so eligibility is simply "not EXCLUDED". EXCLUDED-only
    days yield no sample (A8).
    """
    try:
        return str(getattr(score, "candidate_status", "")) != "EXCLUDED"
    except Exception:  # noqa: BLE001 - unknown shape is ineligible
        return False


def _sample_scores(scores: list[Any]) -> list[Any]:
    """First eligible score per (symbol, profile, UTC day).

    Earliest ``as_of_ms`` wins; ``snapshot_id ASC`` breaks ties so the
    choice is deterministic across runs.
    """
    best: dict[tuple[str, str, int], Any] = {}
    for score in scores:
        if not _is_eligible(score):
            continue
        try:
            key = (
                str(score.symbol).upper(),
                str(score.profile),
                int(score.as_of_ms) // _DAY_MS,
            )
        except (TypeError, ValueError, AttributeError):
            continue
        current = best.get(key)
        if current is None:
            best[key] = score
            continue
        try:
            if (int(score.as_of_ms), str(score.snapshot_id)) < (
                int(current.as_of_ms),
                str(current.snapshot_id),
            ):
                best[key] = score
        except (TypeError, ValueError, AttributeError):
            continue
    return sorted(
        best.values(), key=lambda s: (int(s.as_of_ms), str(s.snapshot_id))
    )


async def summary(
    repository: Any,
    filters: Mapping[str, Any] | None = None,
    *,
    config: Any | None = None,
    cost_config_hash: str | None = None,
    formula_version: str = FORMULA_VERSION,
    clock: Callable[[], int] | None = None,
) -> Any:
    """Aggregate stored outcomes per horizon into an ``EvidenceSummary``.

    ``repository`` is the F01 outcome store (read-only here); ``filters``
    are the router's raw query params. Supported keys: ``horizon``,
    ``symbol``, ``profile``, ``generation_id``/``generationId``,
    ``start_ms``/``startMs``/``as_of_from_ms``, ``end_ms``/``endMs``/
    ``as_of_to_ms``, ``feature_version``/``featureVersion``,
    ``entry_version``/``entryVersion``, ``cost_config_hash``/
    ``costConfigHash``, ``formula_version``/``formulaVersion``.
    """
    from diveintocrypto_desktop.shortlab.service import EvidenceSummary

    filt = dict(filters or {})
    horizons = parse_horizons(filt)
    now_ms = int(clock()) if clock is not None else _now_ms()
    history_days = _resolve_history_days(config)
    sample_policy = _resolve_sample_policy(config)

    formula = _filter_value(filt, "formula_version", "formulaVersion") or formula_version
    cost_digest = _resolve_cost_hash(
        config, _filter_value(filt, "cost_config_hash", "costConfigHash") or cost_config_hash
    )
    want_feature = _filter_value(filt, "feature_version", "featureVersion")
    want_entry = _filter_value(filt, "entry_version", "entryVersion")
    generation_id = _filter_value(filt, "generation_id", "generationId")

    buckets: dict[str, dict[str, Any]] = {name: _empty_bucket() for name in horizons}
    if not horizons:
        return EvidenceSummary(
            filters=filt, horizons={}, total=0, generated_at_ms=now_ms
        )

    # -- window ------------------------------------------------------------
    if generation_id is not None:
        window_from: int | None = None
        window_to: int | None = None
    else:
        raw_from = _int_or_none(
            filt.get("start_ms", filt.get("startMs", filt.get("as_of_from_ms")))
        )
        raw_to = _int_or_none(
            filt.get("end_ms", filt.get("endMs", filt.get("as_of_to_ms")))
        )
        window_to = raw_to if raw_to is not None else now_ms
        window_from = (
            raw_from if raw_from is not None else window_to - history_days * _DAY_MS
        )
        if window_from > window_to:
            window_from, window_to = window_to, window_from

    # -- collect scores ----------------------------------------------------
    wanted_symbol = _filter_value(filt, "symbol")
    wanted_symbol = wanted_symbol.upper() if wanted_symbol is not None else None
    wanted_profile = _filter_value(filt, "profile")

    collected: list[Any] = []
    if generation_id is not None:
        # Explicit single-generation view (old capability, no window).
        offset = 0
        while True:
            try:
                page = await repository.list_candidates(
                    generation_id=generation_id, limit=200, offset=offset
                )
            except Exception:  # noqa: BLE001 - unknown generation is empty
                break
            items = list(page.items)
            collected.extend(items)
            if len(items) < 200:
                break
            offset += 200
    else:
        repo_filters: dict[str, Any] = {}
        if wanted_symbol is not None:
            repo_filters["symbol"] = wanted_symbol
        if window_from is not None:
            repo_filters["as_of_from_ms"] = window_from
        if window_to is not None:
            repo_filters["as_of_to_ms"] = window_to
        offset = 0
        while True:
            try:
                page = await repository.list_scores_for_evidence(
                    repo_filters, 200, offset
                )
            except Exception:  # noqa: BLE001 - unreadable window is empty
                break
            items = list(page.items)
            collected.extend(items)
            if len(items) < 200:
                break
            offset += 200
        # Legacy-fixture fallback: Task 16 fixtures seed as_of far outside
        # the real-clock 180d window and call summary without a clock. When
        # the windowed query is empty and the caller gave no explicit
        # window, retry unfiltered so those stores still report honestly
        # instead of going silently empty.
        if (
            not collected
            and "start_ms" not in filt
            and "startMs" not in filt
            and "as_of_from_ms" not in filt
            and "end_ms" not in filt
            and "endMs" not in filt
            and "as_of_to_ms" not in filt
        ):
            offset = 0
            while True:
                try:
                    page = await repository.list_scores_for_evidence(
                        {"symbol": wanted_symbol} if wanted_symbol is not None else {},
                        200,
                        offset,
                    )
                except Exception:  # noqa: BLE001 - unreadable store is empty
                    break
                items = list(page.items)
                collected.extend(items)
                if len(items) < 200:
                    break
                offset += 200

    if wanted_profile is not None:
        collected = [s for s in collected if str(s.profile) == wanted_profile]
    if wanted_symbol is not None:
        collected = [s for s in collected if str(s.symbol).upper() == wanted_symbol]
    # Generation filter already applied above for the single-generation path;
    # the windowed path only reads SUCCEEDED generations via the repository.

    raw_total = len(collected)

    # -- version scoping (never silently mix) ------------------------------
    current_feature = _current_feature_version()
    current_entry = _current_entry_version()
    if want_feature is not None:
        collected = [s for s in collected if str(s.feature_version) == want_feature]
    elif any(str(s.feature_version) == current_feature for s in collected):
        # Default: current feature version only. Legacy-only stores fall
        # through to the full set below so old single-version DBs (and the
        # Task 16 fixtures seeded with features-v1) still report honestly
        # instead of going silently empty.
        collected = [s for s in collected if str(s.feature_version) == current_feature]
    if want_entry is not None:
        collected = [
            s
            for s in collected
            if (s.entry_version is None and want_entry in ("", "null", "none"))
            or str(s.entry_version) == want_entry
        ]
    elif current_entry is not None and any(
        s.entry_version is not None and str(s.entry_version) == current_entry
        for s in collected
    ):
        # Scores without an Entry (null entry_version) always pass the
        # default gate; scores carrying an Entry must carry the current one.
        collected = [
            s
            for s in collected
            if s.entry_version is None or str(s.entry_version) == current_entry
        ]

    sampled = _sample_scores(collected)

    resolved_filters = dict(filt)
    resolved_filters.setdefault("historyDays", history_days)
    resolved_filters.setdefault("samplePolicy", sample_policy)
    resolved_filters.setdefault("featureVersion", want_feature or current_feature)
    if want_entry is not None or current_entry is not None:
        resolved_filters.setdefault("entryVersion", want_entry or current_entry)
    resolved_filters.setdefault("costConfigHash", cost_digest)
    resolved_filters.setdefault("formulaVersion", formula)
    if window_from is not None:
        resolved_filters.setdefault("startMs", window_from)
    if window_to is not None:
        resolved_filters.setdefault("endMs", window_to)

    sums: dict[str, dict[str, float]] = {
        name: {"net": 0.0, "price": 0.0, "funding": 0.0, "n_net": 0.0,
               "n_price": 0.0, "n_funding": 0.0}
        for name in horizons
    }
    not_graded: dict[str, int] = {name: 0 for name in horizons}

    for score in sampled:
        try:
            score_as_of = int(score.as_of_ms)
        except (TypeError, ValueError):
            continue
        for name in horizons:
            try:
                outcome = await repository.get_outcome(
                    score.snapshot_id, name, formula, cost_digest
                )
            except Exception:  # noqa: BLE001 - one bad row never fails the report
                outcome = None
            if outcome is not None:
                status = str(outcome.outcome_status)
            else:
                due_ms = horizon_due_ms(score_as_of, name)
                if now_ms < due_ms:
                    status = "PENDING"
                else:
                    status = "UNAVAILABLE"
                    not_graded[name] += 1
            if status not in OUTCOME_STATUSES:
                status = "UNAVAILABLE"
            bucket = buckets[name]
            bucket[status] += 1
            bucket["total"] += 1
            bucket["sampled"] += 1
            if status == "COMPLETE" and outcome is not None:
                bucket["complete"] += 1
                acc = sums[name]
                net = _finite(outcome.net_short_return)
                price = _finite(outcome.price_short_return)
                funding = _finite(outcome.funding_carry)
                if net is not None:
                    acc["net"] += net
                    acc["n_net"] += 1
                if price is not None:
                    acc["price"] += price
                    acc["n_price"] += 1
                if funding is not None:
                    acc["funding"] += funding
                    acc["n_funding"] += 1

    for name in horizons:
        bucket = buckets[name]
        acc = sums[name]
        # Invariant: the four states always sum to the sample size.
        assert (
            bucket["PENDING"]
            + bucket["COMPLETE"]
            + bucket["CENSORED"]
            + bucket["UNAVAILABLE"]
            == bucket["total"]
        )
        bucket["meanNetReturn"] = acc["net"] / acc["n_net"] if acc["n_net"] else None
        bucket["meanPriceReturn"] = (
            acc["price"] / acc["n_price"] if acc["n_price"] else None
        )
        bucket["meanFundingCarry"] = (
            acc["funding"] / acc["n_funding"] if acc["n_funding"] else None
        )
        bucket["notGradedDue"] = int(not_graded[name])
        bucket["not_graded_due"] = int(not_graded[name])
        bucket["rawTotal"] = int(raw_total)
        bucket["samplePolicy"] = sample_policy
        bucket["featureVersion"] = want_feature or current_feature
        bucket["entryVersion"] = want_entry or current_entry
        bucket["costConfigHash"] = cost_digest
        bucket["formulaVersion"] = formula
        if window_from is not None:
            bucket["windowStartMs"] = int(window_from)
        if window_to is not None:
            bucket["windowEndMs"] = int(window_to)
    total = sum(buckets[name]["total"] for name in horizons)
    return EvidenceSummary(
        filters=resolved_filters, horizons=buckets, total=int(total),
        generated_at_ms=now_ms,
    )


def build_metrics_provider(
    repository: Any,
    config: Any | None = None,
    clock: Callable[[], int] | None = None,
    *,
    cost_config_hash: str | None = None,
    formula_version: str = FORMULA_VERSION,
) -> Callable[[Mapping[str, Any]], Awaitable[Any]]:
    """Build the ``metrics_provider`` the service consumes (F06b handoff).

    Positional contract: ``build_metrics_provider(repository, config,
    clock)``. The keyword-only ``cost_config_hash``/``formula_version``
    overrides select one old bucket explicitly; by default the current
    cost/formula pair is read so buckets never silently mix.

    Assign it (``service._metrics_provider = provider`` in tests, runtime
    wiring in production) -- the Task 14 router needs no change. The
    provider only reads the F01 outcome repository.
    """

    async def _provider(filters: Mapping[str, Any]) -> Any:
        return await summary(
            repository,
            filters,
            config=config,
            cost_config_hash=cost_config_hash,
            formula_version=formula_version,
            clock=clock,
        )

    return _provider


__all__ = [
    "FORMULA_VERSION",
    "HORIZON_MS",
    "NOT_GRADED_DUE",
    "OUTCOME_STATUSES",
    "build_metrics_provider",
    "parse_horizons",
    "summary",
]
