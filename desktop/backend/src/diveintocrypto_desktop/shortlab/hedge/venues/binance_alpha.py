"""H02 Binance Alpha venue: same-quantity dual quote over fullDepth (B9/B11).

Identity rule: the Alpha instrument comes ONLY from the venue mapping
(``identity.binance_alpha_symbol`` + ``identity.binance_alpha_token_id``).
``tokenId`` and contract addresses are never derived from each other, and a
bare symbol is never guessed as a spot ticker. Multiple candidates stay
``UNRESOLVED`` and are never ``READY``.

Flow per ``quote``:

1. ``providers.binance_alpha.enabled`` must be true (fixture + live evidence
   gate, H02.3) -- otherwise ``UNAVAILABLE``/``VENUE_DISABLED`` with zero
   sends;
2. ``tokenList`` + ``exchangeInfo`` confirm the legal instrument ID, then
   rules parse through the shared :mod:`data.trading_rules` parser;
3. ``ticker`` gives the reference mid; ``fullDepth`` (default 100, escalate
   500/1000 within budget while either side cannot cover) gives level VWAP
   for the **same** canonical target on both sides;
4. HTTP 200 still checks ``code == "000000"`` (+ ``success`` when present);
   ``451``/``403`` -> ``VENUE_REGION_UNAVAILABLE``, ``429`` ->
   ``RATE_LIMITED`` (Retry-After kept) -- never ``N/A``.
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
    from diveintocrypto_desktop.data import binance_alpha as _alpha
    from diveintocrypto_desktop.data import trading_rules as _rules
except Exception:  # pragma: no cover
    _alpha = None  # type: ignore[assignment]
    _rules = None  # type: ignore[assignment]

__all__ = [
    "BINANCE_ALPHA",
    "NO_ALPHA_MARKET",
    "VENUE_REGION_UNAVAILABLE",
    "ALPHA_RATE_LIMITED",
    "ALPHA_BUSINESS_ERROR",
    "ALPHA_DISABLED",
    "ALPHA_BOOK_THIN",
    "BinanceAlphaVenue",
]

BINANCE_ALPHA = "BINANCE_ALPHA"
NO_ALPHA_MARKET = "no_alpha_market"
VENUE_REGION_UNAVAILABLE = "VENUE_REGION_UNAVAILABLE"
ALPHA_RATE_LIMITED = "RATE_LIMITED"
ALPHA_BUSINESS_ERROR = "ALPHA_BUSINESS_ERROR"
ALPHA_DISABLED = "VENUE_DISABLED"
ALPHA_BOOK_THIN = "BOOK_THIN"
TRADING_RULES_UNVERIFIED = "TRADING_RULES_UNVERIFIED"

_DEPTH_ESCALATION = (100, 500, 1000)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _classify_alpha_error(exc: BaseException) -> tuple[str, str]:
    status = getattr(exc, "status", None)
    try:
        status_int = int(status) if status is not None else None
    except (TypeError, ValueError):
        status_int = None
    if status_int in (451, 403):
        return "UNAVAILABLE", VENUE_REGION_UNAVAILABLE
    if status_int == 429:
        return "UNAVAILABLE", ALPHA_RATE_LIMITED
    name = type(exc).__name__
    if name in ("AlphaRegionError",) or "451" in str(exc) or "403" in str(exc):
        return "UNAVAILABLE", VENUE_REGION_UNAVAILABLE
    if name in ("AlphaRateLimited",):
        return "UNAVAILABLE", ALPHA_RATE_LIMITED
    if name in ("AlphaBusinessError", "AlphaHttpError", "AlphaError"):
        return "ERROR", ALPHA_BUSINESS_ERROR
    reason = getattr(exc, "reason_code", None)
    if isinstance(reason, str) and reason:
        return "UNAVAILABLE", reason
    return "UNAVAILABLE", "alpha_unreachable"


class BinanceAlphaVenue:
    """BINANCE_ALPHA dual-quote venue (H02)."""

    venue = BINANCE_ALPHA

    def __init__(
        self,
        config: Any | None = None,
        *,
        token_list_fn: Callable[..., Awaitable[Any]] | None = None,
        exchange_info_fn: Callable[..., Awaitable[Any]] | None = None,
        ticker_fn: Callable[..., Awaitable[Any]] | None = None,
        depth_fn: Callable[..., Awaitable[Any]] | None = None,
        http_get: Any | None = None,
        now_ms_fn: Callable[[], int] | None = None,
        quote_cache: QuoteCache | None = None,
        rules_cache: Any | None = None,
        max_price_impact_bps: float | None = None,
        quote_ttl_ms: int = 20_000,
    ) -> None:
        self._config = config
        self._http_get = http_get
        self._token_list_fn = token_list_fn
        self._exchange_info_fn = exchange_info_fn
        self._ticker_fn = ticker_fn
        self._depth_fn = depth_fn
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
        self._quotes.clear()
        try:
            self._rules_cache.clear()
        except AttributeError:
            pass

    # -- default fetchers (real Alpha adapter) ---------------------------
    async def _fetch_token_list(self) -> Any:
        if self._token_list_fn is not None:
            return await self._token_list_fn()
        assert _alpha is not None
        return await _alpha.fetch_token_list(http_get=self._http_get)

    async def _fetch_exchange_info(self) -> Any:
        if self._exchange_info_fn is not None:
            return await self._exchange_info_fn()
        assert _alpha is not None
        return await _alpha.fetch_exchange_info(http_get=self._http_get)

    async def _fetch_ticker(self, symbol: str) -> Any:
        if self._ticker_fn is not None:
            return await self._ticker_fn(symbol)
        assert _alpha is not None
        return await _alpha.fetch_ticker(symbol, http_get=self._http_get)

    async def _fetch_depth(self, symbol: str, limit: int) -> Any:
        if self._depth_fn is not None:
            return await self._depth_fn(symbol, limit)
        assert _alpha is not None
        return await _alpha.fetch_full_depth(symbol, limit, http_get=self._http_get)

    def _fee_rates(self) -> tuple[float, float]:
        try:
            hedge = getattr(self._config, "hedge", None) if self._config is not None else None
            costs = getattr(hedge, "costs", None) if hedge is not None else None
            if isinstance(costs, dict):
                entry = float(costs.get("alpha_entry_fee_rate", 0.001))
                exit_ = float(costs.get("alpha_exit_fee_rate", 0.001))
            elif costs is not None:
                entry = float(getattr(costs, "alpha_entry_fee_rate", 0.001))
                exit_ = float(getattr(costs, "alpha_exit_fee_rate", 0.001))
            else:
                entry, exit_ = 0.001, 0.001
        except (TypeError, ValueError):
            entry, exit_ = 0.001, 0.001
        return entry, exit_

    # -- main ------------------------------------------------------------
    async def quote(
        self,
        identity: Any,
        canonical_qty: str,
        request_context: Any | None = None,
    ) -> ProviderResult[SpotVenueQuote]:
        now_ms = int(self._now_ms_fn())
        fetched_ms = now_ms
        # 0. enabled gate (H02.3: fixture + live evidence before true) -----
        enabled = False
        try:
            if _alpha is not None:
                enabled = bool(_alpha.is_alpha_enabled(self._config))
            else:
                hedge = getattr(self._config, "hedge", None) if self._config is not None else None
                providers = getattr(hedge, "providers", None) if hedge is not None else None
                entry = providers.get("binance_alpha") if isinstance(providers, dict) else None
                enabled = bool(entry.get("enabled")) if isinstance(entry, dict) else False
        except Exception:
            enabled = False
        if not enabled:
            return ProviderResult(
                status="UNAVAILABLE", source="binance-alpha", fetched_at_ms=fetched_ms,
                as_of_ms=fetched_ms, data=None, stale=False,
                reason_code=ALPHA_DISABLED,
                error_message=sanitize_error_message(
                    "binance_alpha provider disabled (H02.3: fixture + live evidence required)"),
            )
        # 1. quantity shape ------------------------------------------------
        try:
            target = Decimal(str(canonical_qty).strip())
        except (InvalidOperation, ValueError, AttributeError):
            return ProviderResult(
                status="ERROR", source="binance-alpha", fetched_at_ms=fetched_ms,
                as_of_ms=fetched_ms, data=None, stale=False,
                reason_code="INVALID_QUANTITY",
                error_message=sanitize_error_message(f"canonical_qty not a decimal string: {canonical_qty!r}"),
            )
        if not target.is_finite() or target <= 0:
            return ProviderResult(
                status="ERROR", source="binance-alpha", fetched_at_ms=fetched_ms,
                as_of_ms=fetched_ms, data=None, stale=False,
                reason_code="INVALID_QUANTITY",
                error_message=sanitize_error_message(f"canonical_qty must be > 0, got {canonical_qty!r}"),
            )
        qty_str = str(canonical_qty).strip()
        # 2. identity (Alpha mapping only; never guess from tokenId) --------
        raw_symbol = getattr(identity, "binance_alpha_symbol", None)
        raw_token_id = getattr(identity, "binance_alpha_token_id", None)
        symbol = raw_symbol.strip() if isinstance(raw_symbol, str) and raw_symbol.strip() else None
        token_id = raw_token_id.strip() if isinstance(raw_token_id, str) and raw_token_id.strip() else None
        canonical_id = getattr(identity, "canonical_id", None)
        canonical_id = str(canonical_id) if isinstance(canonical_id, str) and canonical_id else (symbol or "unknown")
        confidence = getattr(identity, "mapping_confidence", "UNRESOLVED")
        if confidence not in ("VERIFIED", "HIGH", "MEDIUM", "LOW", "UNRESOLVED"):
            confidence = "UNRESOLVED"
        if not symbol:
            return ProviderResult(
                status="NOT_APPLICABLE", source="binance-alpha", fetched_at_ms=fetched_ms,
                as_of_ms=fetched_ms, data=None, stale=False,
                reason_code=NO_ALPHA_MARKET,
                error_message="identity carries no verified binance_alpha_symbol; "
                "tokenId is never guessed as a market symbol",
            )
        if confidence == "UNRESOLVED":
            # Multi-candidate Alpha symbols stay UNRESOLVED and are never
            # ABSOLUTE READY venues -- still quotable for inspection, but the
            # confidence travels verbatim into the quote.
            pass
        # 3. tokenList + exchangeInfo confirm the legal instrument ----------
        try:
            await self._fetch_token_list()
        except Exception as exc:  # noqa: BLE001
            status, reason = _classify_alpha_error(exc)
            if status == "ERROR":
                return ProviderResult(
                    status="ERROR", source="binance-alpha", fetched_at_ms=fetched_ms,  # type: ignore[arg-type]
                    as_of_ms=fetched_ms, data=None, stale=False, reason_code=reason,
                    error_message=sanitize_error_message(f"alpha tokenList business failure: {str(exc)[:160]}"),
                )
            return ProviderResult(
                status=status, source="binance-alpha", fetched_at_ms=fetched_ms,  # type: ignore[arg-type]
                as_of_ms=fetched_ms, data=None, stale=False, reason_code=reason,
                error_message=sanitize_error_message(f"alpha tokenList failed: {str(exc)[:160]}"),
            )
        try:
            exchange_info = await self._fetch_exchange_info()
        except Exception as exc:  # noqa: BLE001
            status, reason = _classify_alpha_error(exc)
            if status == "ERROR":
                return ProviderResult(
                    status="ERROR", source="binance-alpha", fetched_at_ms=fetched_ms,  # type: ignore[arg-type]
                    as_of_ms=fetched_ms, data=None, stale=False, reason_code=reason,
                    error_message=sanitize_error_message(f"alpha exchangeInfo business failure: {str(exc)[:160]}"),
                )
            return ProviderResult(
                status=status, source="binance-alpha", fetched_at_ms=fetched_ms,  # type: ignore[arg-type]
                as_of_ms=fetched_ms, data=None, stale=False, reason_code=reason,
                error_message=sanitize_error_message(f"alpha exchangeInfo failed: {str(exc)[:160]}"),
            )
        entry: dict[str, Any] | None = None
        try:
            rows = exchange_info.get("symbols", []) if isinstance(exchange_info, dict) else []
            for row in rows:
                if isinstance(row, dict) and str(row.get("symbol", "")) == symbol:
                    entry = row
                    break
        except AttributeError:
            entry = None
        if entry is None:
            return ProviderResult(
                status="NOT_APPLICABLE", source="binance-alpha", fetched_at_ms=fetched_ms,
                as_of_ms=fetched_ms, data=None, stale=False,
                reason_code=NO_ALPHA_MARKET, error_message=None,
            )
        # Optional tokenId cross-check: when the identity carries a tokenId,
        # the exchange entry must agree; a mismatch is UNRESOLVED, not a guess.
        if token_id is not None:
            entry_token = entry.get("tokenId", entry.get("token_id"))
            if entry_token is not None and str(entry_token) != token_id:
                return ProviderResult(
                    status="NOT_APPLICABLE", source="binance-alpha", fetched_at_ms=fetched_ms,
                    as_of_ms=fetched_ms, data=None, stale=False,
                    reason_code="ALPHA_IDENTITY_UNRESOLVED",
                    error_message=sanitize_error_message("alpha tokenId mismatch; symbol not resolved"),
                )
        # 4. rules ----------------------------------------------------------
        rules = None
        if self._rules_cache is not None:
            try:
                rules = self._rules_cache.get(BINANCE_ALPHA, symbol)
            except Exception:
                rules = None
        if rules is None:
            if _rules is None:
                raise RuntimeError("trading_rules module unavailable")
            try:
                rules = _rules.parse_trading_rules(
                    entry, BINANCE_ALPHA, symbol,
                    {"source_as_of_ms": None, "known_at_ms": now_ms})
            except Exception as exc:  # noqa: BLE001 -- illegal filters reject
                return ProviderResult(
                    status="ERROR", source="binance-alpha", fetched_at_ms=fetched_ms,
                    as_of_ms=fetched_ms, data=None, stale=False,
                    reason_code=TRADING_RULES_UNVERIFIED,
                    error_message=sanitize_error_message(f"trading rules illegal: {str(exc)[:160]}"),
                )
            try:
                self._rules_cache.put(rules)
            except Exception:
                pass
        if _rules is not None:
            for otype in ("LIMIT", "MARKET"):
                ok, reason = _rules.validate_qty(rules, qty_str, otype)
                if not ok:
                    return ProviderResult(
                        status="ERROR", source="binance-alpha", fetched_at_ms=fetched_ms,
                        as_of_ms=fetched_ms, data=None, stale=False,
                        reason_code=TRADING_RULES_UNVERIFIED,
                        error_message=sanitize_error_message(f"{otype} qty rejected: {reason}"),
                    )
        # 5. quote cache ----------------------------------------------------
        try:
            cache_key = build_quote_cache_key(
                BINANCE_ALPHA, canonical_id, symbol, rules.rule_version, qty_str)
        except (InvalidOperation, ValueError):
            return ProviderResult(
                status="ERROR", source="binance-alpha", fetched_at_ms=fetched_ms,
                as_of_ms=fetched_ms, data=None, stale=False,
                reason_code="INVALID_QUANTITY",
                error_message=sanitize_error_message(f"canonical_qty not a decimal string: {qty_str!r}"),
            )
        cached = self._quotes.get(cache_key)
        if cached is not None:
            return cached
        # 6. ticker (reference mid) -----------------------------------------
        try:
            ticker = await self._fetch_ticker(symbol)
        except Exception as exc:  # noqa: BLE001
            status, reason = _classify_alpha_error(exc)
            return ProviderResult(
                status=status, source="binance-alpha", fetched_at_ms=fetched_ms,  # type: ignore[arg-type]
                as_of_ms=fetched_ms, data=None, stale=False, reason_code=reason,
                error_message=sanitize_error_message(f"alpha ticker failed: {str(exc)[:160]}"),
            )
        bid_s, ask_s = _ticker_bid_ask(ticker)
        mid = mid_from_book(bid_s, ask_s)
        if mid is None:
            # Fall back to a single lastPrice when the venue gives no spread.
            last = _ticker_last(ticker)
            if last is not None:
                mid = last
        if mid is None:
            return ProviderResult(
                status="ERROR", source="binance-alpha", fetched_at_ms=fetched_ms,
                as_of_ms=fetched_ms, data=None, stale=False,
                reason_code=ALPHA_BUSINESS_ERROR,
                error_message=sanitize_error_message("alpha ticker missing price shape"),
            )
        if _rules is not None:
            mid_s = str(mid)
            for otype in ("LIMIT", "MARKET"):
                ok, reason = _rules.validate_price(rules, mid_s, otype)
                if not ok:
                    return ProviderResult(
                        status="ERROR", source="binance-alpha", fetched_at_ms=fetched_ms,
                        as_of_ms=fetched_ms, data=None, stale=False,
                        reason_code=TRADING_RULES_UNVERIFIED,
                        error_message=sanitize_error_message(f"{otype} price rejected: {reason}"),
                    )
            with localcontext() as ctx:
                ctx.prec = 80
                notional = target * mid
            for otype in ("LIMIT", "MARKET"):
                ok, reason = _rules.validate_notional(rules, str(notional), otype)
                if not ok:
                    return ProviderResult(
                        status="ERROR", source="binance-alpha", fetched_at_ms=fetched_ms,
                        as_of_ms=fetched_ms, data=None, stale=False,
                        reason_code=TRADING_RULES_UNVERIFIED,
                        error_message=sanitize_error_message(f"{otype} notional rejected: {reason}"),
                    )
        # 7. fullDepth with budget-bounded escalation ------------------------
        last_snapshot: Any = None
        buy_vwap: Decimal | None = None
        sell_vwap: Decimal | None = None
        buy_exec = Decimal(0)
        sell_exec = Decimal(0)
        used_limit = _DEPTH_ESCALATION[0]
        source_ts: int | None = None
        try:
            for limit in _DEPTH_ESCALATION:
                payload = await self._fetch_depth(symbol, limit)
                if not isinstance(payload, dict):
                    return ProviderResult(
                        status="ERROR", source="binance-alpha", fetched_at_ms=fetched_ms,
                        as_of_ms=fetched_ms, data=None, stale=False,
                        reason_code=ALPHA_BUSINESS_ERROR,
                        error_message=sanitize_error_message("alpha fullDepth bad shape"),
                    )
                source_ts = _depth_source_ts(payload)
                received_ms = int(self._now_ms_fn())
                snapshot = parse_depth_snapshot(
                    venue=BINANCE_ALPHA, instrument_id=symbol,
                    raw_bids=payload.get("bids"), raw_asks=payload.get("asks"),
                    source="binance-alpha-fullDepth",
                    source_timestamp_ms=source_ts,
                    fetched_at_ms=fetched_ms, received_at_ms=received_ms)
                last_snapshot = snapshot
                used_limit = limit
                buy_vwap, buy_exec = vwap_for_qty(snapshot.asks, target)
                sell_vwap, sell_exec = vwap_for_qty(snapshot.bids, target)
                if buy_exec >= target and sell_exec >= target:
                    break
                # A truncated book never proves deeper prices fillable: only
                # escalate within the 100/500/1000 budget, then report thin.
        except Exception as exc:  # noqa: BLE001
            status, reason = _classify_alpha_error(exc)
            return ProviderResult(
                status=status, source="binance-alpha", fetched_at_ms=fetched_ms,  # type: ignore[arg-type]
                as_of_ms=fetched_ms, data=None, stale=False, reason_code=reason,
                error_message=sanitize_error_message(f"alpha fullDepth failed: {str(exc)[:160]}"),
            )
        # 8. feasibility ------------------------------------------------------
        buy_slip = slippage_bps(buy_vwap, mid)
        sell_slip = slippage_bps(sell_vwap, mid)
        entry_feasible = bool(buy_exec >= target)
        exit_feasible = bool(sell_exec >= target)
        if entry_feasible and exit_feasible:
            exit_feasibility = "CONFIRMED"
            status = "OK"
            reason_code: str | None = None
        elif buy_exec > 0 and sell_exec > 0:
            exit_feasibility = "PARTIAL"
            status = "PARTIAL"
            reason_code = ALPHA_BOOK_THIN
        else:
            exit_feasibility = "NO"
            status = "PARTIAL"
            reason_code = ALPHA_BOOK_THIN
        entry_rate, exit_rate = self._fee_rates()
        with localcontext() as ctx:
            ctx.prec = 80
            buy_notional = (buy_vwap * buy_exec) if buy_vwap is not None else Decimal(0)
            sell_notional = (sell_vwap * sell_exec) if sell_vwap is not None else Decimal(0)
            fee = buy_notional * Decimal(str(entry_rate)) + sell_notional * Decimal(str(exit_rate))
            ref_notional = target * mid
        expires_at = fetched_ms + self._quote_ttl_ms
        as_of = source_ts if source_ts is not None else fetched_ms
        # R06b (D06.2/D06.3): honest fee flag + real capability projection.
        # Alpha stays indicative-only for protection purposes: no new trading
        # capability or execution permission is added here (quote-only).
        try:
            from diveintocrypto_desktop.shortlab.hedge.venues.base import (  # noqa: WPS433
                SPOT_FEES_INCLUDED as _SPOT_FEES,
                project_spot_capabilities as _project_spot,
            )

            _alpha_extra = _project_spot(
                rules, venue=BINANCE_ALPHA, depth_limit=used_limit,
                max_price_impact_bps=self._max_impact_bps,
            )
            # Alpha has no verified conditional-order execution in V1.
            _alpha_extra = dict(_alpha_extra)
            _alpha_extra["execution_kind"] = "QUOTE_ONLY_INDICATIVE"
        except Exception:
            _alpha_extra = {
                "fees_included": True,
                "spot_conditional_capability": "MANUAL_EXIT_ONLY",
                "execution_kind": "QUOTE_ONLY_INDICATIVE",
            }
            _SPOT_FEES = True  # type: ignore[assignment]
        quote = SpotVenueQuote(
            venue=BINANCE_ALPHA, canonical_id=canonical_id, symbol=symbol,
            chain=None, contract_address=None, as_of_ms=as_of,
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
            quote_to_usd="1", source_timestamp_ms=source_ts,
            fetched_at_ms=fetched_ms, requested_canonical_qty=qty_str,
            trading_rules={
                "rule_version": rules.rule_version, "venue": BINANCE_ALPHA,
                "instrument_id": symbol, "price_rules": dict(rules.price_rules),
                "lot_rules": dict(rules.lot_rules),
                "notional_rules": dict(rules.notional_rules),
                "order_types": dict(rules.order_types),
            },
            capabilities={
                "limit_supported": bool(rules.order_types.get("LIMIT")),
                "market_supported": bool(rules.order_types.get("MARKET")),
                "depth_limit": used_limit, "max_price_impact_bps": self._max_impact_bps,
                **dict(_alpha_extra),
            },
            identity_confidence=confidence,
            status="OK" if status == "OK" else status,
            reason_code=reason_code,
            fees_included=bool(_SPOT_FEES),
        )
        result = ProviderResult(
            status=status,  # type: ignore[arg-type]
            source="binance-alpha", fetched_at_ms=fetched_ms,
            as_of_ms=as_of, data=quote, stale=False,
            reason_code=reason_code, error_message=None,
        )
        try:
            self._quotes.put(cache_key, result)
        except Exception:
            pass
        return result


def _ticker_bid_ask(ticker: Any) -> tuple[str | None, str | None]:
    if not isinstance(ticker, dict):
        return None, None
    data = ticker.get("data", ticker) if isinstance(ticker.get("data"), dict) else ticker
    bid = data.get("bidPrice", data.get("bid_price", data.get("bid", data.get("bestBid"))))
    ask = data.get("askPrice", data.get("ask_price", data.get("ask", data.get("bestAsk"))))
    # Some Alpha tickers nest under data.price/bid/ask with numeric types.
    bid_s = str(bid).strip() if bid is not None and str(bid).strip() not in ("", "None") else None
    ask_s = str(ask).strip() if ask is not None and str(ask).strip() not in ("", "None") else None
    return bid_s, ask_s


def _ticker_last(ticker: Any) -> Decimal | None:
    if not isinstance(ticker, dict):
        return None
    data = ticker.get("data", ticker) if isinstance(ticker.get("data"), dict) else ticker
    for key in ("lastPrice", "price", "close", "last_price"):
        raw = data.get(key)
        if raw is None:
            continue
        try:
            parsed = Decimal(str(raw).strip())
        except (InvalidOperation, ValueError, AttributeError):
            continue
        if parsed.is_finite() and parsed > 0:
            return parsed
    return None


def _depth_source_ts(payload: dict[str, Any]) -> int | None:
    """Prefer ``E``/``T`` source millis; None when the venue gives no clock."""
    for key in ("E", "T", "eventTime", "transactionTime", "closeTime", "E_time"):
        raw = payload.get(key)
        if raw is None:
            continue
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    data = payload.get("data")
    if isinstance(data, dict):
        return _depth_source_ts(data)
    return None
