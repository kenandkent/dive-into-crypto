"""H02 Binance Spot venue: same-quantity dual quote over level VWAP (B9/B10).

Identity rule: the Spot instrument comes ONLY from
``identity.binance_spot_symbol`` -- the futures symbol is never stripped or
guessed and a leading ``1000`` (``1000PEPEUSDT`` etc.) is never removed.
``1000``-denomination handling is identity metadata (``contract_multiplier``
provenance), never a symbol rewrite here.

Flow per ``quote``:

1. validate ``canonical_qty`` (Decimal string ``> 0``);
2. resolve rules via :mod:`data.trading_rules` (30-min cache; precision ban;
   ``0`` means disabled);
3. validate quantity for ``LIMIT``/``MARKET`` separately (``LOT_SIZE`` /
   ``MARKET_LOT_SIZE``); price/notional checked after the mid is known;
4. fetch best bid/ask + depth (default limit 100, escalate 500/1000 while
   either side cannot cover the target);
5. level VWAP for the **same** target on both sides (buy from asks, sell to
   bids) -- never derived from 1% totals;
6. executable qty capped by ``max_price_impact_bps`` depth plus raw depth;
7. build the frozen :class:`SpotVenueQuote` (Decimal strings) with
   ``CONFIRMED``/``PARTIAL``/``NO`` exit feasibility.

``451``/``403``/``429`` (and budget denials) are ``UNAVAILABLE`` with
``VENUE_REGION_UNAVAILABLE``/``RATE_LIMITED`` -- never ``NOT_APPLICABLE``.
Only an exchangeInfo-confirmed absence is ``NOT_APPLICABLE``.
"""

from __future__ import annotations

import time
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Awaitable, Callable

from diveintocrypto_desktop.shortlab.hedge.models import SpotVenueQuote
from diveintocrypto_desktop.shortlab.hedge.venues.base import (
    build_quote_cache_key,
    mid_from_book,
    parse_depth_snapshot,
    slippage_bps,
    vwap_for_qty,
    QuoteCache,
)
from diveintocrypto_desktop.shortlab.models import ProviderResult, sanitize_error_message

try:
    from diveintocrypto_desktop.data import trading_rules as _rules
except Exception:  # pragma: no cover -- import-time fallback for tooling
    _rules = None  # type: ignore[assignment]

__all__ = [
    "BINANCE_SPOT",
    "NO_SPOT_MARKET",
    "VENUE_REGION_UNAVAILABLE",
    "SPOT_RATE_LIMITED",
    "SPOT_BOOK_THIN",
    "SPOT_BOOK_INVALID",
    "BinanceSpotVenue",
    "reset_spot_venue_cache",
]

BINANCE_SPOT = "BINANCE_SPOT"
NO_SPOT_MARKET = "no_spot_market"
VENUE_REGION_UNAVAILABLE = "VENUE_REGION_UNAVAILABLE"
SPOT_RATE_LIMITED = "RATE_LIMITED"
SPOT_BOOK_THIN = "BOOK_THIN"
SPOT_BOOK_INVALID = "BOOK_INVALID"
TRADING_RULES_UNVERIFIED = "TRADING_RULES_UNVERIFIED"
VENUE_DISABLED = "VENUE_DISABLED"

_DEPTH_ESCALATION = (100, 500, 1000)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _is_spot_enabled(config: Any) -> bool:
    if config is None:
        return True
    try:
        hedge = getattr(config, "hedge", None)
        providers = getattr(hedge, "providers", None) if hedge is not None else None
        if providers is None and isinstance(hedge, dict):
            providers = hedge.get("providers")
        if providers is None:
            return True
        entry = providers.get("binance_spot") if isinstance(providers, dict) else getattr(
            providers, "binance_spot", None)
        if entry is None:
            return True
        if isinstance(entry, dict):
            return bool(entry.get("enabled", True))
        return bool(getattr(entry, "enabled", True))
    except Exception:
        return True


def _classify_fetch_error(exc: BaseException) -> tuple[str, str | None]:
    """Map a fetch exception to ``(status, reason_code)`` (never N/A)."""
    status = getattr(exc, "status", None)
    try:
        status_int = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_int = None
    if status_int in (451, 403):
        return "UNAVAILABLE", VENUE_REGION_UNAVAILABLE
    if status_int == 429:
        return "UNAVAILABLE", SPOT_RATE_LIMITED
    reason = getattr(exc, "reason_code", None)
    if isinstance(reason, str) and reason:
        if reason == "REQUEST_BUDGET_EXHAUSTED":
            return "UNAVAILABLE", reason
        if reason in ("UNBUDGETED_ENDPOINT",):
            return "UNAVAILABLE", reason
    text = str(exc)
    if "BudgetExhausted" in type(exc).__name__ or "BUDGET_EXHAUSTED" in text:
        return "UNAVAILABLE", "REQUEST_BUDGET_EXHAUSTED"
    return "UNAVAILABLE", "spot_unreachable"


async def _default_exchange_entry(symbol: str, request_context: Any | None) -> dict[str, Any] | None:
    """Spot exchangeInfo symbol entry for ``symbol`` (None when absent)."""
    from diveintocrypto_desktop.data import spot as _spot

    return await _spot.fetch_spot_exchange_entry(symbol, request_context=request_context)


async def _default_book_ticker(symbol: str, request_context: Any | None) -> dict[str, Any]:
    from diveintocrypto_desktop.data import spot as _spot

    return await _spot.fetch_spot_book_ticker(symbol, request_context=request_context)


async def _default_depth(symbol: str, limit: int, request_context: Any | None) -> dict[str, Any]:
    from diveintocrypto_desktop.data import spot as _spot

    return await _spot.fetch_spot_depth(symbol, limit, request_context=request_context)


def _decimal_or_none(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


class BinanceSpotVenue:
    """BINANCE_SPOT dual-quote venue (H02)."""

    venue = BINANCE_SPOT

    def __init__(
        self,
        config: Any | None = None,
        *,
        exchange_entry_fn: Callable[..., Awaitable[Any]] | None = None,
        book_ticker_fn: Callable[..., Awaitable[Any]] | None = None,
        depth_fn: Callable[..., Awaitable[Any]] | None = None,
        now_ms_fn: Callable[[], int] | None = None,
        quote_cache: QuoteCache | None = None,
        rules_cache: Any | None = None,
        max_price_impact_bps: float | None = None,
        quote_ttl_ms: int = 20_000,
    ) -> None:
        self._config = config
        self._exchange_entry_fn = exchange_entry_fn or _default_exchange_entry
        self._book_ticker_fn = book_ticker_fn or _default_book_ticker
        self._depth_fn = depth_fn or _default_depth
        self._now_ms_fn = now_ms_fn or _now_ms
        self._quotes = quote_cache or QuoteCache(ttl_sec=quote_ttl_ms / 1000.0)
        if rules_cache is None and _rules is not None:
            rules_cache = _rules.TradingRulesCache()
        self._rules_cache = rules_cache
        if max_price_impact_bps is None:
            try:
                hedge = getattr(config, "hedge", None) if config is not None else None
                execution = getattr(hedge, "execution", None) if hedge is not None else None
                if isinstance(execution, dict):
                    max_price_impact_bps = float(execution.get("max_price_impact_bps", 30))
                elif execution is not None:
                    max_price_impact_bps = float(getattr(execution, "max_price_impact_bps", 30))
                else:
                    max_price_impact_bps = 30.0
            except (TypeError, ValueError):
                max_price_impact_bps = 30.0
        self._max_impact_bps = float(max_price_impact_bps or 30.0)
        self._quote_ttl_ms = int(quote_ttl_ms)

    def reset_cache(self) -> None:
        """Drop quote + rules caches (test hook)."""
        self._quotes.clear()
        try:
            self._rules_cache.clear()
        except AttributeError:
            pass

    # -- helpers ---------------------------------------------------------
    def _fee_rates(self) -> tuple[float, float]:
        try:
            hedge = getattr(self._config, "hedge", None) if self._config is not None else None
            costs = getattr(hedge, "costs", None) if hedge is not None else None
            if isinstance(costs, dict):
                entry = float(costs.get("spot_entry_fee_rate", 0.001))
                exit_ = float(costs.get("spot_exit_fee_rate", 0.001))
            elif costs is not None:
                entry = float(getattr(costs, "spot_entry_fee_rate", 0.001))
                exit_ = float(getattr(costs, "spot_exit_fee_rate", 0.001))
            else:
                entry, exit_ = 0.001, 0.001
        except (TypeError, ValueError):
            entry, exit_ = 0.001, 0.001
        return entry, exit_

    async def _get_rules(self, symbol: str, request_context: Any | None,
                         now_ms: int) -> Any:
        if self._rules_cache is not None:
            hit = self._rules_cache.get(BINANCE_SPOT, symbol)
            if hit is not None:
                return hit
        entry = await self._exchange_entry_fn(symbol, request_context)
        if entry is None:
            return None
        if _rules is None:
            raise RuntimeError("trading_rules module unavailable")
        snapshot = _rules.parse_trading_rules(
            entry, BINANCE_SPOT, symbol,
            {"source_as_of_ms": None, "known_at_ms": now_ms})
        if self._rules_cache is not None:
            try:
                self._rules_cache.put(snapshot)
            except Exception:
                pass
        return snapshot

    # -- main ------------------------------------------------------------
    async def quote(
        self,
        identity: Any,
        canonical_qty: str,
        request_context: Any | None = None,
    ) -> ProviderResult[SpotVenueQuote]:
        """Same-quantity buy/sell quote for ``canonical_qty`` (Decimal string)."""
        now_ms = int(self._now_ms_fn())
        fetched_ms = now_ms
        # 1. quantity shape ------------------------------------------------
        try:
            target = Decimal(str(canonical_qty).strip())
        except (InvalidOperation, ValueError, AttributeError):
            return ProviderResult(
                status="ERROR", source="binance-spot", fetched_at_ms=fetched_ms,
                as_of_ms=fetched_ms, data=None, stale=False,
                reason_code="INVALID_QUANTITY",
                error_message=sanitize_error_message(f"canonical_qty not a decimal string: {canonical_qty!r}"),
            )
        if not target.is_finite() or target <= 0:
            return ProviderResult(
                status="ERROR", source="binance-spot", fetched_at_ms=fetched_ms,
                as_of_ms=fetched_ms, data=None, stale=False,
                reason_code="INVALID_QUANTITY",
                error_message=sanitize_error_message(f"canonical_qty must be > 0, got {canonical_qty!r}"),
            )
        qty_str = str(canonical_qty).strip()
        # 2. identity (never strip 1000 prefix, never guess) ----------------
        raw_spot = getattr(identity, "binance_spot_symbol", None)
        symbol = raw_spot.strip().upper() if isinstance(raw_spot, str) and raw_spot.strip() else None
        canonical_id = getattr(identity, "canonical_id", None)
        canonical_id = str(canonical_id) if isinstance(canonical_id, str) and canonical_id else (symbol or "unknown")
        confidence = getattr(identity, "mapping_confidence", "UNRESOLVED")
        if confidence not in ("VERIFIED", "HIGH", "MEDIUM", "LOW", "UNRESOLVED"):
            confidence = "UNRESOLVED"
        if not symbol:
            return ProviderResult(
                status="NOT_APPLICABLE", source="binance-spot", fetched_at_ms=fetched_ms,
                as_of_ms=fetched_ms, data=None, stale=False,
                reason_code=NO_SPOT_MARKET,
                error_message="identity carries no verified binance_spot_symbol; "
                "the futures symbol is never guessed as a spot symbol",
            )
        if not _is_spot_enabled(self._config):
            return ProviderResult(
                status="UNAVAILABLE", source="binance-spot", fetched_at_ms=fetched_ms,
                as_of_ms=fetched_ms, data=None, stale=False,
                reason_code=VENUE_DISABLED,
                error_message=sanitize_error_message("binance_spot provider disabled"),
            )
        # 3. rules (exchangeInfo-confirmed absence -> N/A only here) --------
        try:
            rules = await self._get_rules(symbol, request_context, now_ms)
        except Exception as exc:  # noqa: BLE001 -- mapped, never N/A
            if getattr(exc, "reason_code", "") == TRADING_RULES_UNVERIFIED or "TRADING_RULES" in type(exc).__name__:
                return ProviderResult(
                    status="ERROR", source="binance-spot", fetched_at_ms=fetched_ms,
                    as_of_ms=fetched_ms, data=None, stale=False,
                    reason_code=TRADING_RULES_UNVERIFIED,
                    error_message=sanitize_error_message(f"trading rules illegal: {str(exc)[:160]}"),
                )
            status, reason = _classify_fetch_error(exc)
            return ProviderResult(
                status=status, source="binance-spot", fetched_at_ms=fetched_ms,  # type: ignore[arg-type]
                as_of_ms=fetched_ms, data=None, stale=False,
                reason_code=reason,
                error_message=sanitize_error_message(f"spot exchangeInfo failed: {str(exc)[:160]}"),
            )
        if rules is None:
            return ProviderResult(
                status="NOT_APPLICABLE", source="binance-spot", fetched_at_ms=fetched_ms,
                as_of_ms=fetched_ms, data=None, stale=False,
                reason_code=NO_SPOT_MARKET, error_message=None,
            )
        # Quantity vs LOT_SIZE/MARKET_LOT_SIZE (LIMIT/MARKET separately).
        if _rules is not None:
            for otype in ("LIMIT", "MARKET"):
                ok, reason = _rules.validate_qty(rules, qty_str, otype)
                if not ok:
                    return ProviderResult(
                        status="ERROR", source="binance-spot", fetched_at_ms=fetched_ms,
                        as_of_ms=fetched_ms, data=None, stale=False,
                        reason_code=TRADING_RULES_UNVERIFIED,
                        error_message=sanitize_error_message(f"{otype} qty rejected: {reason}"),
                    )
        # 4. quote cache (venue/identity/rules/quantity) -------------------
        try:
            cache_key = build_quote_cache_key(
                BINANCE_SPOT, canonical_id, symbol, rules.rule_version, qty_str)
        except (InvalidOperation, ValueError):
            return ProviderResult(
                status="ERROR", source="binance-spot", fetched_at_ms=fetched_ms,
                as_of_ms=fetched_ms, data=None, stale=False,
                reason_code="INVALID_QUANTITY",
                error_message=sanitize_error_message(f"canonical_qty not a decimal string: {qty_str!r}"),
            )
        cached = self._quotes.get(cache_key)
        if cached is not None:
            return cached
        # 5. book ticker + depth (escalate 100/500/1000) -------------------
        try:
            book = await self._book_ticker_fn(symbol, request_context)
        except Exception as exc:  # noqa: BLE001
            status, reason = _classify_fetch_error(exc)
            return ProviderResult(
                status=status, source="binance-spot", fetched_at_ms=fetched_ms,  # type: ignore[arg-type]
                as_of_ms=fetched_ms, data=None, stale=False,
                reason_code=reason,
                error_message=sanitize_error_message(f"spot bookTicker failed: {str(exc)[:160]}"),
            )
        try:
            bid_s = str(book.get("bidPrice", book.get("bid_price", book.get("bid", "")))).strip() or None
            ask_s = str(book.get("askPrice", book.get("ask_price", book.get("ask", "")))).strip() or None
            if bid_s == "" or bid_s == "None":
                bid_s = None
            if ask_s == "" or ask_s == "None":
                ask_s = None
        except AttributeError:
            bid_s, ask_s = None, None
        mid = mid_from_book(bid_s, ask_s)
        if mid is None:
            return ProviderResult(
                status="ERROR", source="binance-spot", fetched_at_ms=fetched_ms,
                as_of_ms=fetched_ms, data=None, stale=False,
                reason_code=SPOT_BOOK_INVALID,
                error_message=sanitize_error_message(f"spot bookTicker missing bid/ask for {symbol}"),
            )
        raw_depth: dict[str, Any] | None = None
        used_limit = _DEPTH_ESCALATION[0]
        last_snapshot: Any = None
        buy_vwap: Decimal | None = None
        sell_vwap: Decimal | None = None
        buy_exec = Decimal(0)
        sell_exec = Decimal(0)
        try:
            for limit in _DEPTH_ESCALATION:
                payload = await self._depth_fn(symbol, limit, request_context)
                if not isinstance(payload, dict) or not isinstance(payload.get("bids"), list) \
                        or not isinstance(payload.get("asks"), list):
                    return ProviderResult(
                        status="ERROR", source="binance-spot", fetched_at_ms=fetched_ms,
                        as_of_ms=fetched_ms, data=None, stale=False,
                        reason_code=SPOT_BOOK_INVALID,
                        error_message=sanitize_error_message("spot depth missing bids/asks"),
                    )
                received_ms = int(self._now_ms_fn())
                snapshot = parse_depth_snapshot(
                    venue=BINANCE_SPOT, instrument_id=symbol,
                    raw_bids=payload.get("bids"), raw_asks=payload.get("asks"),
                    source="binance-spot-depth", source_timestamp_ms=None,
                    fetched_at_ms=fetched_ms, received_at_ms=received_ms)
                last_snapshot = snapshot
                used_limit = limit
                buy_vwap, buy_exec = vwap_for_qty(snapshot.asks, target)
                sell_vwap, sell_exec = vwap_for_qty(snapshot.bids, target)
                if buy_exec >= target and sell_exec >= target:
                    break
                raw_depth = payload
            if raw_depth is None and last_snapshot is not None:
                raw_depth = {"bids": list(last_snapshot.bids), "asks": list(last_snapshot.asks)}
        except Exception as exc:  # noqa: BLE001
            status, reason = _classify_fetch_error(exc)
            return ProviderResult(
                status=status, source="binance-spot", fetched_at_ms=fetched_ms,  # type: ignore[arg-type]
                as_of_ms=fetched_ms, data=None, stale=False,
                reason_code=reason,
                error_message=sanitize_error_message(f"spot depth failed: {str(exc)[:160]}"),
            )
        # 6. price / notional rule check (after mid known) ------------------
        if _rules is not None:
            mid_s = str(mid)
            for otype in ("LIMIT", "MARKET"):
                ok, reason = _rules.validate_price(rules, mid_s, otype)
                if not ok:
                    return ProviderResult(
                        status="ERROR", source="binance-spot", fetched_at_ms=fetched_ms,
                        as_of_ms=fetched_ms, data=None, stale=False,
                        reason_code=TRADING_RULES_UNVERIFIED,
                        error_message=sanitize_error_message(f"{otype} price rejected: {reason}"),
                    )
            with localcontext() as ctx:
                ctx.prec = 80
                notional = (target * mid)
            for otype in ("LIMIT", "MARKET"):
                ok, reason = _rules.validate_notional(rules, str(notional), otype)
                if not ok:
                    return ProviderResult(
                        status="ERROR", source="binance-spot", fetched_at_ms=fetched_ms,
                        as_of_ms=fetched_ms, data=None, stale=False,
                        reason_code=TRADING_RULES_UNVERIFIED,
                        error_message=sanitize_error_message(f"{otype} notional rejected: {reason}"),
                    )
        # 7. max-impact executable + feasibility ---------------------------
        buy_cov = (buy_exec / target) if target > 0 else Decimal(0)
        sell_cov = (sell_exec / target) if target > 0 else Decimal(0)
        buy_slip = slippage_bps(buy_vwap, mid)
        sell_slip = slippage_bps(sell_vwap, mid)
        # Slippage beyond max impact caps executable to the in-tolerance prefix.
        # vwap_for_qty already walks levels; re-walk with impact cap when needed.
        if buy_slip is not None and abs(buy_slip) > self._max_impact_bps:
            buy_exec = _executable_within_impact(last_snapshot.asks, target, mid, self._max_impact_bps, True)
            buy_vwap, _ = vwap_for_qty(last_snapshot.asks, buy_exec) if buy_exec > 0 else (None, Decimal(0))
            buy_slip = slippage_bps(buy_vwap, mid)
        if sell_slip is not None and abs(sell_slip) > self._max_impact_bps:
            sell_exec = _executable_within_impact(last_snapshot.bids, target, mid, self._max_impact_bps, False)
            sell_vwap, _ = vwap_for_qty(last_snapshot.bids, sell_exec) if sell_exec > 0 else (None, Decimal(0))
            sell_slip = slippage_bps(sell_vwap, mid)
        entry_feasible = bool(buy_exec >= target)
        exit_feasible = bool(sell_exec >= target)
        if entry_feasible and exit_feasible:
            exit_feasibility = "CONFIRMED"
            status: str = "OK"
            reason_code: str | None = None
        elif buy_exec > 0 and sell_exec > 0:
            exit_feasibility = "PARTIAL"
            status = "PARTIAL"
            reason_code = SPOT_BOOK_THIN
        elif buy_exec <= 0 or sell_exec <= 0:
            exit_feasibility = "NO"
            status = "PARTIAL"
            reason_code = SPOT_BOOK_THIN
        else:
            exit_feasibility = "UNKNOWN"
            status = "PARTIAL"
            reason_code = SPOT_BOOK_THIN
        # 8. costs ----------------------------------------------------------
        entry_rate, exit_rate = self._fee_rates()
        with localcontext() as ctx:
            ctx.prec = 80
            buy_notional = (buy_vwap * buy_exec) if buy_vwap is not None else Decimal(0)
            sell_notional = (sell_vwap * sell_exec) if sell_vwap is not None else Decimal(0)
            fee = buy_notional * Decimal(str(entry_rate)) + sell_notional * Decimal(str(exit_rate))
            ref_notional = target * mid
        # 9. DTO -------------------------------------------------------------
        expires_at = fetched_ms + self._quote_ttl_ms
        quote = SpotVenueQuote(
            venue=BINANCE_SPOT, canonical_id=canonical_id, symbol=symbol,
            chain=None, contract_address=None, as_of_ms=fetched_ms,
            expires_at_ms=expires_at, reference_notional_usd=str(ref_notional),
            mid_price=str(mid),
            buy_vwap=str(buy_vwap) if buy_vwap is not None else None,
            sell_vwap=str(sell_vwap) if sell_vwap is not None else None,
            buy_executable_qty=str(buy_exec), sell_executable_qty=str(sell_exec),
            buy_slippage_bps=buy_slip, sell_slippage_bps=sell_slip,
            estimated_fee_usd=str(fee), estimated_gas_usd=None,
            direction_costs={
                "buy_fee_usd": str(buy_notional * Decimal(str(entry_rate))),
                "sell_fee_usd": str(sell_notional * Decimal(str(exit_rate))),
                "buy_price_impact_included": True, "sell_price_impact_included": True,
                "gas_included": False,
            },
            entry_feasible=entry_feasible, exit_feasible=exit_feasible,
            exit_feasibility=exit_feasibility, quote_currency="USDT",
            quote_to_usd="1", source_timestamp_ms=None,
            fetched_at_ms=fetched_ms, requested_canonical_qty=qty_str,
            trading_rules={
                "rule_version": rules.rule_version, "venue": BINANCE_SPOT,
                "instrument_id": symbol, "price_rules": dict(rules.price_rules),
                "lot_rules": dict(rules.lot_rules),
                "notional_rules": dict(rules.notional_rules),
                "order_types": dict(rules.order_types),
            },
            capabilities={
                "limit_supported": bool(rules.order_types.get("LIMIT")),
                "market_supported": bool(rules.order_types.get("MARKET")),
                "depth_limit": used_limit, "max_price_impact_bps": self._max_impact_bps,
                "buy_coverage_ratio": float(buy_cov), "sell_coverage_ratio": float(sell_cov),
            },
            identity_confidence=confidence,
            status="OK" if status == "OK" else status,
            reason_code=reason_code,
        )
        result = ProviderResult(
            status=status,  # type: ignore[arg-type]
            source="binance-spot", fetched_at_ms=fetched_ms,
            as_of_ms=fetched_ms, data=quote, stale=False,
            reason_code=reason_code, error_message=None,
        )
        # Cache only usable quotes (OK/PARTIAL with data); errors never cached.
        try:
            self._quotes.put(cache_key, result)
        except Exception:
            pass
        return result


def _executable_within_impact(
    levels: tuple[tuple[str, str], ...],
    target: Decimal,
    mid: Decimal,
    max_bps: float,
    is_ask_side: bool,
) -> Decimal:
    """Largest prefix of ``levels`` whose running VWAP stays within ``max_bps``."""
    with localcontext() as ctx:
        ctx.prec = 80
        filled = Decimal(0)
        notional = Decimal(0)
        best = Decimal(0)
        for price_s, qty_s in levels or []:
            try:
                price, qty = Decimal(price_s), Decimal(qty_s)
            except (InvalidOperation, ValueError):
                continue
            if price <= 0 or qty <= 0:
                continue
            take = qty if filled + qty <= target else target - filled
            trial_filled = filled + take
            trial_notional = notional + price * take
            trial_vwap = trial_notional / trial_filled if trial_filled > 0 else None
            slip = slippage_bps(trial_vwap, mid) if trial_vwap is not None else None
            # Sells print below mid (negative bps); tolerance is absolute.
            if slip is not None and abs(slip) > max_bps:
                break
            filled, notional, best = trial_filled, trial_notional, trial_filled
            if filled >= target:
                break
        return best


def reset_spot_venue_cache() -> None:
    """Test hook placeholder (per-instance caches are reset via ``reset_cache``)."""
