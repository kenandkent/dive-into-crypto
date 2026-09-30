"""Forward-evidence aggregates for ``/api/short/evidence/summary`` (Task 16).

The Task 14 router is unchanged: it calls
``ShortLabService.evidence_summary(filters)``, which delegates to the
provider built here. Phase 3 (no provider) stays ``503
short_evidence_unavailable``; wiring :func:`build_metrics_provider` onto the
service flips the same route to a real ``200``.

Every per-horizon bucket reports the four outcome states
``PENDING / COMPLETE / CENSORED / UNAVAILABLE`` and the four counts always
sum to that horizon's sample size (scores times horizons -- nothing is
silently dropped, so there is no survivorship bias). Scores with no stored
outcome row yet count as ``PENDING`` (``NOT_GRADED``).
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Mapping

from diveintocrypto_desktop.shortlab.evidence.grader import (
    FORMULA_VERSION,
    HORIZON_MS,
    OUTCOME_STATUSES,
    default_cost_hash,
)

_HORIZON_ORDER = ("7D", "30D", "90D")


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
    }


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

    ``repository`` is Task 2's outcome store (read-only here); ``filters``
    are the router's raw query params (``horizon``/``symbol``/``profile``/
    ``generation_id``). Only the latest ``SUCCEEDED`` score generation is
    read, so half-written refresh batches never leak into the report.
    """
    from diveintocrypto_desktop.shortlab.service import EvidenceSummary

    filt = dict(filters or {})
    horizons = parse_horizons(filt)
    now_ms = int(clock()) if clock is not None else _now_ms()
    digest = _resolve_cost_hash(config, cost_config_hash)

    buckets: dict[str, dict[str, Any]] = {name: _empty_bucket() for name in horizons}
    if not horizons:
        return EvidenceSummary(
            filters=filt, horizons={}, total=0, generated_at_ms=now_ms
        )

    generation_id = filt.get("generation_id", filt.get("generationId"))
    if generation_id is not None:
        generation_id = str(generation_id)
    else:
        try:
            generation_id = await repository.latest_completed_generation()
        except Exception:  # noqa: BLE001 - no generation is an empty report
            generation_id = None
    if generation_id is None:
        return EvidenceSummary(
            filters=filt, horizons=buckets, total=0, generated_at_ms=now_ms
        )

    wanted_symbol = filt.get("symbol")
    wanted_symbol = str(wanted_symbol).upper() if wanted_symbol is not None else None
    wanted_profile = filt.get("profile")
    wanted_profile = str(wanted_profile) if wanted_profile is not None else None

    sums: dict[str, dict[str, float]] = {
        name: {"net": 0.0, "price": 0.0, "funding": 0.0, "n_net": 0.0,
               "n_price": 0.0, "n_funding": 0.0}
        for name in horizons
    }

    offset = 0
    while True:
        try:
            page = await repository.list_candidates(
                generation_id=generation_id, limit=200, offset=offset
            )
        except Exception:  # noqa: BLE001 - unreadable generation is empty
            break
        items = list(page.items)
        for score in items:
            if wanted_symbol is not None and str(score.symbol).upper() != wanted_symbol:
                continue
            if wanted_profile is not None and str(score.profile) != wanted_profile:
                continue
            for name in horizons:
                try:
                    outcome = await repository.get_outcome(
                        score.snapshot_id, name, formula_version, digest
                    )
                except Exception:  # noqa: BLE001 - one bad row never fails the report
                    outcome = None
                status = str(
                    outcome.outcome_status
                    if outcome is not None
                    else "PENDING"
                )
                if status not in OUTCOME_STATUSES:
                    status = "UNAVAILABLE"
                bucket = buckets[name]
                bucket[status] += 1
                bucket["total"] += 1
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
        if len(items) < 200:
            break
        offset += 200

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
    total = sum(buckets[name]["total"] for name in horizons)
    return EvidenceSummary(
        filters=filt, horizons=buckets, total=int(total), generated_at_ms=now_ms
    )


def build_metrics_provider(
    repository: Any,
    *,
    config: Any | None = None,
    cost_config_hash: str | None = None,
    formula_version: str = FORMULA_VERSION,
    clock: Callable[[], int] | None = None,
) -> Callable[[Mapping[str, Any]], Awaitable[Any]]:
    """Build the ``metrics_provider`` the service consumes.

    Assign it (``service._metrics_provider = provider`` in tests, runtime
    wiring in production) -- the Task 14 router needs no change. The
    provider only reads Task 2's outcome repository.
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
    "OUTCOME_STATUSES",
    "build_metrics_provider",
    "parse_horizons",
    "summary",
]
