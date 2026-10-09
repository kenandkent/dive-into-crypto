"""H05 Ethereum/0x read-only price adapter (design B12.2, plan H05.1-H05.3).

Fixed surface (never generalised to other chains in V1):

- chain ``1`` (Ethereum mainnet), host ``https://api.0x.org``, read-only
  ``/swap/allowance-holder/price/v2``; header ``0x-version: v2`` plus
  ``0x-api-key: <SHORTLAB_0X_API_KEY>`` (key travels as a header only,
  never in the URL, logs or error messages).
- No key -> zero network: ``health`` and both quote legs return
  ``UNAVAILABLE``/``UNCONFIGURED`` with ``enabled=false`` and no sends.
- 1 request/second, concurrency 1 (asyncio lock + start-to-start throttle).
- ``buyAmount`` and ``sellAmount`` are mutually exclusive atomic-unit
  integer strings for the *same* net token quantity (``Decimal(qty) *
  10**decimals`` with 80-digit Decimal context, never float). The sell leg
  quotes the identical net token amount via ``sellAmount``.
- Quote asset is the verified Ethereum USDC mapping (address + 6
  decimals); FX stays ``None`` (observed-only, never ``0``).
- Gas/network fee may be ``null``: missing gas keeps
  ``gas_units/gas_price/estimated_gas_usd`` as ``None`` (PARTIAL, never
  ``"0"``).
- Provider expiry, when present, is honoured; when absent the adapter
  stores ``expires_at_ms = fetched_at_ms + quote_ttl_sec * 1000``
  (default 30s) with an explicit local-fallback source. Already-expired
  provider timestamps are served as ``UNAVAILABLE``/``QUOTE_EXPIRED``.
- Never requests ``getQuote``/calldata/approve/swap payloads, never sends
  ``taker``/``recipient``/``txOrigin`` or wallet/balance/allowance reads.
  A response that smuggles ``transaction``/``calldata`` is rejected with
  ``PRICE_SCHEMA_REJECTED`` and never stored.
- Health never hits an undisclosed health endpoint: it reports local
  configuration plus the last legal price receipt statistics.

Address/identity gating (F04 ``normalize_chain_address`` is the only
place that interprets address case):

- chain label must be Ethereum (``ethereum``/``eth``/``mainnet``/``1``);
  anything else is ``UNAVAILABLE``/``CHAIN_PROVIDER_UNCONFIGURED``.
- ``contract_address`` must normalise to ``CHECKSUM_VERIFIED``. Uniform
  case (``NO_CHECKSUM``) carries no checksum proof and is not quotable;
  ``BAD_CHECKSUM``/``INVALID_ADDRESS``/``ADDRESS_CHAIN_UNSUPPORTED``
  never send.
- ``mapping_confidence`` must be ``VERIFIED``/``HIGH`` and token
  ``decimals`` must be explicitly known (injected ``decimals_by_address``
  or ``token_decimals``); both are never guessed from the symbol.
"""

from __future__ import annotations

import asyncio
import os
import time
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Awaitable, Callable, Mapping

from diveintocrypto_desktop.shortlab.hedge.models import OnchainQuote
from diveintocrypto_desktop.shortlab.hedge.venues.onchain import (
    ONCHAIN_QUOTE_KIND,
)
from diveintocrypto_desktop.shortlab.models import (
    ProviderResult,
    sanitize_error_message,
)

try:  # F04 checksum source (H05 depends on H01+F04).
    from diveintocrypto_desktop.shortlab.identity import resolver as _resolver
except Exception:  # pragma: no cover -- tooling fallback, never used live
    _resolver = None  # type: ignore[assignment]

__all__ = [
    "PROVIDER_ID",
    "CHAIN_ID",
    "BASE_URL",
    "PRICE_PATH",
    "PRICE_URL",
    "API_VERSION",
    "API_KEY_ENV",
    "QUOTE_ASSET",
    "QUOTE_ASSET_ADDRESS",
    "QUOTE_ASSET_DECIMALS",
    "DEFAULT_QUOTE_TTL_SEC",
    "REQUESTS_PER_SEC",
    "CONCURRENCY",
    "DOC_PRICE_URL",
    "DOC_AUTH_URL",
    "UNCONFIGURED",
    "CHAIN_PROVIDER_UNCONFIGURED",
    "VENUE_REGION_UNAVAILABLE",
    "RATE_LIMITED",
    "NO_ROUTE",
    "QUOTE_EXPIRED",
    "PRICE_SCHEMA_REJECTED",
    "ONCHAIN_FEES_INCLUDED",
    "ONCHAIN_EXECUTION_KIND",
    "Ethereum0xPriceVenue",
    "is_onchain_quote_expired",
    "build_poc_report",
]

PROVIDER_ID = "ETHEREUM_0X_PRICE_V2"
CHAIN_ID = 1
BASE_URL = "https://api.0x.org"
PRICE_PATH = "/swap/allowance-holder/price/v2"
PRICE_URL = f"{BASE_URL}{PRICE_PATH}"
API_VERSION = "v2"
API_KEY_ENV = "SHORTLAB_0X_API_KEY"
QUOTE_ASSET = "USDC"
# Verified Ethereum USDC mapping (checksum-verified at import).
QUOTE_ASSET_ADDRESS = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
QUOTE_ASSET_DECIMALS = 6
DEFAULT_QUOTE_TTL_SEC = 30
REQUESTS_PER_SEC = 1
CONCURRENCY = 1

# Frozen doc references (B12.2): `evm-ap-is` is used verbatim.
DOC_PRICE_URL = (
    "https://docs.0x.org/api-reference/evm-ap-is/swap/allowanceholder-getprice"
)
DOC_AUTH_URL = "https://docs.0x.org/api-reference/api-overview"

UNCONFIGURED = "UNCONFIGURED"
CHAIN_PROVIDER_UNCONFIGURED = "CHAIN_PROVIDER_UNCONFIGURED"
VENUE_REGION_UNAVAILABLE = "VENUE_REGION_UNAVAILABLE"
RATE_LIMITED = "RATE_LIMITED"
NO_ROUTE = "NO_ROUTE"
QUOTE_EXPIRED = "QUOTE_EXPIRED"
PRICE_SCHEMA_REJECTED = "PRICE_SCHEMA_REJECTED"
#: R06b (D06.2/D06.3): honest fee flag (no fee leg in 0x price) + real
#: indicative-only capability projection (never an execution permission).
ONCHAIN_FEES_INCLUDED = False
ONCHAIN_EXECUTION_KIND = "INDICATIVE_ONLY"
ONCHAIN_GAS_UNAVAILABLE = "ONCHAIN_GAS_UNAVAILABLE"
ONCHAIN_DECIMALS_UNVERIFIED = "ONCHAIN_DECIMALS_UNVERIFIED"
ONCHAIN_IDENTITY_UNVERIFIED = "ONCHAIN_IDENTITY_UNVERIFIED"
VENUE_DISABLED = "VENUE_DISABLED"

_SOURCE = "0x-price-v2"

_ETHEREUM_LABELS = frozenset({"ethereum", "eth", "mainnet", "1"})
_FORBIDDEN_PARAMS = frozenset({
    "taker", "recipient", "txOrigin", "tx_origin", "takerAddress",
    "calldata", "data", "transaction", "approve", "permit2", "getQuote",
})
_TX_PAYLOAD_KEYS = frozenset({"transaction", "calldata"})


def _now_ms() -> int:
    return int(time.time() * 1000)


def _read_onchain_cfg(config: Any) -> dict[str, Any]:
    """Project the B40 on-chain subtree (defaults mirror default.yaml)."""
    out: dict[str, Any] = {
        "enabled": True,
        "provider": PROVIDER_ID,
        "chain_id": CHAIN_ID,
        "base_url": BASE_URL,
        "api_key_env": API_KEY_ENV,
        "api_version": API_VERSION,
        "quote_kind": ONCHAIN_QUOTE_KIND,
        "quote_asset": QUOTE_ASSET,
        "quote_ttl_sec": DEFAULT_QUOTE_TTL_SEC,
        "requests_per_sec": REQUESTS_PER_SEC,
        "concurrency": CONCURRENCY,
    }
    if config is None:
        return out
    try:
        hedge = getattr(config, "hedge", None)
        providers = getattr(hedge, "providers", None) if hedge is not None else None
        if isinstance(providers, Mapping):
            entry: Any = providers.get("onchain")
        elif providers is not None:
            entry = getattr(providers, "onchain", None)
            if entry is None and isinstance(hedge, dict):
                entry = hedge.get("providers", {}).get("onchain")
            elif isinstance(entry, dict):
                pass
            else:
                entry = providers.get("onchain") if hasattr(providers, "get") else None
        else:
            entry = None
        if isinstance(entry, Mapping):
            for key in ("enabled", "provider", "chain_id", "base_url",
                        "api_key_env", "api_version", "quote_kind",
                        "quote_asset", "quote_ttl_sec", "requests_per_sec",
                        "concurrency"):
                if key in entry and entry[key] is not None:
                    out[key] = entry[key]
        elif entry is not None:
            for key in ("enabled", "provider", "chain_id", "base_url",
                        "api_key_env", "api_version", "quote_kind",
                        "quote_asset", "quote_ttl_sec", "requests_per_sec",
                        "concurrency"):
                value = getattr(entry, key, None)
                if value is not None:
                    out[key] = value
    except Exception:
        pass
    return out


def _resolve_api_key(
    api_key: str | None,
    env: Mapping[str, Any] | None,
    api_key_env: str | None,
) -> str:
    name = str(api_key_env or API_KEY_ENV)
    if isinstance(api_key, str) and api_key.strip():
        return api_key.strip()
    if env is not None:
        try:
            candidate = env.get(name)
        except AttributeError:
            candidate = None
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    try:
        ambient = os.environ.get(name)
    except Exception:
        ambient = None
    if isinstance(ambient, str) and ambient.strip():
        return ambient.strip()
    return ""


def _is_ethereum_chain(chain: Any) -> bool:
    if chain is None:
        return False
    if isinstance(chain, int) and not isinstance(chain, bool):
        return int(chain) == CHAIN_ID
    text = str(chain).strip().lower()
    return text in _ETHEREUM_LABELS


def _qty_to_atomic(qty_str: str, decimals: int) -> str | None:
    """Canonical qty -> atomic integer string (Decimal only, no float)."""
    try:
        qty = Decimal(str(qty_str).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    if not qty.is_finite() or qty <= 0:
        return None
    if isinstance(decimals, bool) or not isinstance(decimals, int) or decimals < 0:
        return None
    with localcontext() as ctx:
        ctx.prec = 80
        scaled = qty * (Decimal(10) ** int(decimals))
    try:
        integral = scaled.to_integral_value()
    except (InvalidOperation, ValueError):
        return None
    if integral != scaled:
        return None  # more precision than the token allows
    if integral <= 0:
        return None
    return format(integral, "f")


def _is_integer_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value.strip().isdigit()


def _parse_expiry_ms(payload: Mapping[str, Any]) -> int | None:
    for key in ("expiration", "expiresAt", "expires_at", "expiry",
                "validUntil", "valid_until", "deadline"):
        if key not in payload:
            continue
        raw = payload.get(key)
        if raw is None:
            continue
        try:
            text = str(raw).strip()
        except Exception:
            continue
        if not text:
            continue
        # Pure integer: ms (>=1e12) or seconds (>=1e9) since epoch.
        if text.lstrip("-").isdigit():
            try:
                number = int(text)
            except ValueError:
                continue
            if number <= 0:
                continue
            if number >= 1_000_000_000_000:
                return number
            if number >= 1_000_000_000:
                return number * 1000
            # Small relative TTL seconds (e.g. 30) -> caller adds; treat
            # as seconds-since-epoch only when plausible, else ignore.
            continue
        continue
    return None


def _parse_block_number(payload: Mapping[str, Any]) -> int | None:
    for key in ("blockNumber", "block_number", "block"):
        raw = payload.get(key)
        if raw is None:
            continue
        try:
            text = str(raw).strip()
        except Exception:
            continue
        if text.lstrip("-").isdigit():
            try:
                number = int(text)
            except ValueError:
                continue
            if number >= 0:
                return number
    return None


def _classify_transport_error(exc: BaseException) -> tuple[str, str | None]:
    status = getattr(exc, "status", None)
    try:
        code = int(status) if status is not None else None
    except (TypeError, ValueError):
        code = None
    if code in (451, 403):
        return "UNAVAILABLE", VENUE_REGION_UNAVAILABLE
    if code == 429:
        return "UNAVAILABLE", RATE_LIMITED
    reason = getattr(exc, "reason_code", None)
    if isinstance(reason, str) and reason:
        return "UNAVAILABLE", reason
    text = str(exc)
    if "BudgetExhausted" in type(exc).__name__ or "BUDGET_EXHAUSTED" in text:
        return "UNAVAILABLE", "REQUEST_BUDGET_EXHAUSTED"
    return "UNAVAILABLE", "onchain_unreachable"


def is_onchain_quote_expired(quote: OnchainQuote, now_ms: int) -> bool:
    """True when ``now_ms`` has reached the quote TTL (never a price)."""
    try:
        expiry = quote.expires_at_ms
    except AttributeError:
        return True
    if expiry is None:
        return False
    try:
        return int(now_ms) >= int(expiry)
    except (TypeError, ValueError):
        return True


def build_poc_report(
    *,
    api_key_present: bool,
    live_response: Mapping[str, Any] | None = None,
    endpoint: str = PRICE_URL,
) -> dict[str, Any]:
    """Independent PoC status (offline suite never claims live completion).

    ``live_verified`` is True only when the caller hands over a real,
    controlled-network live receipt for separate review. Fixture/fake runs
    -- with or without a key -- stay ``NOT_VERIFIED`` so CI can never
    present interface completion as live proof.
    """
    verified = bool(api_key_present) and isinstance(live_response, Mapping) and bool(live_response)
    if not api_key_present:
        return {
            "live_verified": False,
            "live_status": "NOT_VERIFIED",
            "reason_code": UNCONFIGURED,
            "endpoint": endpoint,
            "docs_url": DOC_PRICE_URL,
            "auth_docs_url": DOC_AUTH_URL,
            "api_version": API_VERSION,
            "chain_id": CHAIN_ID,
            "note": (
                "no SHORTLAB_0X_API_KEY in this environment; live PoC not "
                "run -- do not claim completion; interface tests use fixed "
                "fixtures with zero live sends"
            ),
        }
    return {
        "live_verified": bool(verified),
        "live_status": "VERIFIED" if verified else "NOT_VERIFIED",
        "reason_code": None if verified else UNCONFIGURED,
        "endpoint": endpoint,
        "docs_url": DOC_PRICE_URL,
        "auth_docs_url": DOC_AUTH_URL,
        "api_version": API_VERSION,
        "chain_id": CHAIN_ID,
        "note": (
            "fixture/fake run only; live PoC requires a separate controlled "
            "network run with a key -- do not claim live completion from "
            "this report"
        ),
    }


class Ethereum0xPriceVenue:
    """Ethereum 0x Swap v2 read-only price venue (H05)."""

    provider_id = PROVIDER_ID
    venue = "ONCHAIN_DEX"

    def __init__(
        self,
        config: Any | None = None,
        *,
        api_key: str | None = None,
        env: Mapping[str, Any] | None = None,
        api_key_env: str | None = None,
        price_fn: Callable[..., Awaitable[Any]] | None = None,
        now_ms_fn: Callable[[], int] | None = None,
        decimals_by_address: Mapping[str, Any] | None = None,
        token_decimals: int | None = None,
        quote_ttl_sec: int | None = None,
        requests_per_sec: int | None = None,
        concurrency: int | None = None,
        min_interval_sec: float | None = None,
        monotonic_fn: Callable[[], float] | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
        enabled: bool | None = None,
    ) -> None:
        cfg = _read_onchain_cfg(config)
        if enabled is not None:
            cfg["enabled"] = bool(enabled)
        self._config = cfg
        self._api_key_env = str(api_key_env or cfg.get("api_key_env") or API_KEY_ENV)
        self._explicit_api_key = api_key
        self._env = env
        self._price_fn = price_fn
        self._now_ms_fn = now_ms_fn or _now_ms
        self._decimals_by_address = {
            str(k).strip().lower(): v for k, v in dict(decimals_by_address or {}).items()
        }
        self._token_decimals = token_decimals
        try:
            self._quote_ttl_sec = int(
                quote_ttl_sec if quote_ttl_sec is not None else cfg.get("quote_ttl_sec", DEFAULT_QUOTE_TTL_SEC)
            )
        except (TypeError, ValueError):
            self._quote_ttl_sec = DEFAULT_QUOTE_TTL_SEC
        if self._quote_ttl_sec < 1:
            self._quote_ttl_sec = DEFAULT_QUOTE_TTL_SEC
        try:
            self.requests_per_sec = int(
                requests_per_sec if requests_per_sec is not None else cfg.get("requests_per_sec", REQUESTS_PER_SEC)
            )
        except (TypeError, ValueError):
            self.requests_per_sec = REQUESTS_PER_SEC
        try:
            self.concurrency = int(
                concurrency if concurrency is not None else cfg.get("concurrency", CONCURRENCY)
            )
        except (TypeError, ValueError):
            self.concurrency = CONCURRENCY
        if min_interval_sec is not None:
            self._min_interval_sec = float(min_interval_sec)
        elif self.requests_per_sec and self.requests_per_sec > 0:
            self._min_interval_sec = 1.0 / float(self.requests_per_sec)
        else:
            self._min_interval_sec = 1.0
        self._monotonic = monotonic_fn or time.monotonic
        # Late-bind asyncio.sleep so tests can inject a recorder.
        import asyncio as _asyncio

        self._sleep = sleep_fn or _asyncio.sleep
        self._lock = asyncio.Lock()
        self._next_allowed_mono = 0.0
        self._last_ok_ms: int | None = None
        self._last_block: int | None = None
        self.sends = 0

    # -- introspection -------------------------------------------------
    def _key(self) -> str:
        return _resolve_api_key(self._explicit_api_key, self._env, self._api_key_env)

    def _enabled(self) -> bool:
        try:
            return bool(self._config.get("enabled", True))
        except Exception:
            return True

    def _decimals_for(self, display_address: str) -> int | None:
        key = str(display_address or "").strip().lower()
        if key in self._decimals_by_address:
            value = self._decimals_by_address[key]
            if isinstance(value, bool):
                return None
            try:
                number = int(value)
            except (TypeError, ValueError):
                return None
            return number if number >= 0 else None
        if self._token_decimals is not None and not isinstance(self._token_decimals, bool):
            try:
                return int(self._token_decimals)
            except (TypeError, ValueError):
                return None
        return None

    def _unavailable(
        self, reason: str, fetched_ms: int, message: str | None = None
    ) -> ProviderResult[None]:
        return ProviderResult(
            status="UNAVAILABLE",
            source=_SOURCE,
            fetched_at_ms=fetched_ms,
            as_of_ms=fetched_ms,
            data=None,
            stale=False,
            reason_code=reason,
            error_message=sanitize_error_message(message),
        )

    # -- health (never sends) ------------------------------------------
    async def health(self, request_context: Any | None = None) -> ProviderResult[dict]:
        _ = request_context
        fetched_ms = int(self._now_ms_fn())
        if not self._key():
            return ProviderResult(
                status="UNAVAILABLE",
                source=_SOURCE,
                fetched_at_ms=fetched_ms,
                as_of_ms=fetched_ms,
                data={
                    "enabled": False,
                    "configured": False,
                    "provider": PROVIDER_ID,
                    "chain_id": CHAIN_ID,
                    "base_url": BASE_URL,
                    "endpoint": PRICE_URL,
                    "api_version": API_VERSION,
                },
                stale=False,
                reason_code=UNCONFIGURED,
                error_message=sanitize_error_message(
                    f"{UNCONFIGURED}: no {self._api_key_env}; on-chain quotes disabled"
                ),
            )
        return ProviderResult(
            status="OK",
            source=_SOURCE,
            fetched_at_ms=fetched_ms,
            as_of_ms=fetched_ms,
            data={
                "enabled": bool(self._enabled()),
                "configured": True,
                "provider": PROVIDER_ID,
                "chain_id": CHAIN_ID,
                "base_url": BASE_URL,
                "endpoint": PRICE_URL,
                "api_version": API_VERSION,
                "quote_asset": QUOTE_ASSET,
                "last_ok_ms": self._last_ok_ms,
                "last_block_number": self._last_block,
            },
            stale=False,
            reason_code=None,
            error_message=None,
        )

    # -- quotes ----------------------------------------------------------
    async def quote_buy(
        self,
        identity: Any,
        requested_canonical_qty: str,
        quote_asset: Any | None = "USDC",
        as_of_ms: int | None = None,
        request_context: Any | None = None,
    ) -> ProviderResult[OnchainQuote]:
        return await self._quote(
            "buy", identity, requested_canonical_qty, quote_asset, as_of_ms, request_context
        )

    async def quote_sell(
        self,
        identity: Any,
        requested_canonical_qty: str,
        quote_asset: Any | None = "USDC",
        as_of_ms: int | None = None,
        request_context: Any | None = None,
    ) -> ProviderResult[OnchainQuote]:
        return await self._quote(
            "sell", identity, requested_canonical_qty, quote_asset, as_of_ms, request_context
        )

    async def _quote(
        self,
        direction: str,
        identity: Any,
        requested_canonical_qty: str,
        quote_asset: Any | None,
        as_of_ms: int | None,
        request_context: Any | None,
    ) -> ProviderResult[OnchainQuote]:
        fetched_ms = int(self._now_ms_fn())
        as_of = int(as_of_ms) if isinstance(as_of_ms, int) and not isinstance(as_of_ms, bool) else fetched_ms
        # 0. key gate first: no key is always UNCONFIGURED with zero sends.
        api_key = self._key()
        if not api_key:
            return self._unavailable(
                UNCONFIGURED, fetched_ms, f"{UNCONFIGURED}: no {self._api_key_env}"
            )
        if not self._enabled():
            return self._unavailable(VENUE_DISABLED, fetched_ms, "onchain provider disabled")
        # 1. quantity shape (Decimal string > 0).
        try:
            target = Decimal(str(requested_canonical_qty).strip())
        except (InvalidOperation, ValueError, AttributeError):
            return ProviderResult(
                status="ERROR", source=_SOURCE, fetched_at_ms=fetched_ms,
                as_of_ms=as_of, data=None, stale=False,
                reason_code="INVALID_QUANTITY",
                error_message=sanitize_error_message(
                    f"requested_canonical_qty not a decimal string: {requested_canonical_qty!r}"),
            )
        if not target.is_finite() or target <= 0:
            return ProviderResult(
                status="ERROR", source=_SOURCE, fetched_at_ms=fetched_ms,
                as_of_ms=as_of, data=None, stale=False,
                reason_code="INVALID_QUANTITY",
                error_message=sanitize_error_message(
                    f"requested_canonical_qty must be > 0, got {requested_canonical_qty!r}"),
            )
        qty_str = str(requested_canonical_qty).strip()
        # 2. quote asset is fixed to USDC in V1.
        if quote_asset is not None and str(quote_asset).strip().upper() != QUOTE_ASSET:
            return self._unavailable(
                "UNSUPPORTED_QUOTE_ASSET", fetched_ms,
                f"only {QUOTE_ASSET} is supported, got {quote_asset!r}")
        # 3. chain gate (V1 is chain-1 only; never generalised silently).
        raw_chain = getattr(identity, "chain", None)
        if not _is_ethereum_chain(raw_chain):
            return self._unavailable(
                CHAIN_PROVIDER_UNCONFIGURED, fetched_ms,
                f"unsupported chain {raw_chain!r}; V1 is Ethereum mainnet (chain 1) only")
        canonical_id = getattr(identity, "canonical_id", None)
        canonical_id = str(canonical_id) if isinstance(canonical_id, str) and canonical_id else "unknown"
        confidence = getattr(identity, "mapping_confidence", "UNRESOLVED")
        if confidence not in ("VERIFIED", "HIGH", "MEDIUM", "LOW", "UNRESOLVED"):
            confidence = "UNRESOLVED"
        raw_address = getattr(identity, "contract_address", None)
        address_text = raw_address.strip() if isinstance(raw_address, str) else ""
        if not address_text:
            return self._unavailable(
                "ONCHAIN_ADDRESS_INVALID", fetched_ms, "identity carries no contract_address")
        if _resolver is None:  # pragma: no cover
            return self._unavailable(
                ONCHAIN_IDENTITY_UNVERIFIED, fetched_ms, "address resolver unavailable")
        chain_label = str(raw_chain).strip().lower() if isinstance(raw_chain, str) else "ethereum"
        if chain_label in ("1",):
            chain_label = "ethereum"
        norm = _resolver.normalize_chain_address(chain_label or "ethereum", address_text)
        if norm.validation_status == "ADDRESS_CHAIN_UNSUPPORTED":
            return self._unavailable(
                "ADDRESS_CHAIN_UNSUPPORTED", fetched_ms,
                f"address chain unsupported for {address_text[:16]}...")
        if norm.validation_status == "BAD_CHECKSUM":
            return self._unavailable(
                "BAD_CHECKSUM", fetched_ms, "contract checksum failed EIP-55 verification")
        if norm.validation_status == "INVALID_ADDRESS":
            return self._unavailable(
                "ONCHAIN_ADDRESS_INVALID", fetched_ms, "contract address is not 20-byte hex")
        if norm.validation_status == "NO_CHECKSUM":
            # Uniform case carries no checksum proof: inspectable downstream
            # but never quotable here (B12.4 verified-contract rule).
            return self._unavailable(
                "NO_CHECKSUM", fetched_ms,
                "contract address lacks EIP-55 checksum proof; not quotable")
        if norm.validation_status not in ("CHECKSUM_VERIFIED", "OK"):
            return self._unavailable(
                ONCHAIN_IDENTITY_UNVERIFIED, fetched_ms,
                f"contract address not verified ({norm.validation_status})")
        if confidence not in ("VERIFIED", "HIGH"):
            return self._unavailable(
                ONCHAIN_IDENTITY_UNVERIFIED, fetched_ms,
                f"contract binding {confidence} is not VERIFIED/HIGH")
        token_decimals = self._decimals_for(norm.display)
        if token_decimals is None:
            return self._unavailable(
                ONCHAIN_DECIMALS_UNVERIFIED, fetched_ms,
                "token decimals unverified; refusing to scale atomic amounts")
        atomic_qty = _qty_to_atomic(qty_str, int(token_decimals))
        if atomic_qty is None:
            return ProviderResult(
                status="ERROR", source=_SOURCE, fetched_at_ms=fetched_ms,
                as_of_ms=as_of, data=None, stale=False,
                reason_code="INVALID_QUANTITY",
                error_message=sanitize_error_message(
                    f"quantity {qty_str!r} is not representable in {token_decimals} decimals"),
            )
        # 4. build read-only price params (buy/sellAmount mutually exclusive).
        display_token = norm.display
        if direction == "buy":
            params: dict[str, Any] = {
                "chainId": "1",
                "buyToken": display_token,
                "sellToken": QUOTE_ASSET_ADDRESS,
                "buyAmount": atomic_qty,
            }
        else:
            params = {
                "chainId": "1",
                "sellToken": display_token,
                "buyToken": QUOTE_ASSET_ADDRESS,
                "sellAmount": atomic_qty,
            }
        headers = {"0x-version": API_VERSION, "0x-api-key": api_key}
        if self._price_fn is None:
            return self._unavailable(
                "ONCHAIN_TRANSPORT_UNCONFIGURED", fetched_ms, "no price transport injected")
        # 5. 1 RPS / concurrency 1 (loop-bound lock + start-to-start gap).
        async with self._lock:
            try:
                now_mono = float(self._monotonic())
            except Exception:
                now_mono = 0.0
            wait = float(self._next_allowed_mono) - now_mono
            if wait > 0:
                try:
                    await self._sleep(wait)
                except Exception:
                    pass
            try:
                payload = await self._price_fn(PRICE_URL, params, headers, request_context)
            except Exception as exc:  # noqa: BLE001 -- mapped, never N/A
                try:
                    self._next_allowed_mono = float(self._monotonic()) + self._min_interval_sec
                except Exception:
                    pass
                status, reason = _classify_transport_error(exc)
                return ProviderResult(
                    status=status,  # type: ignore[arg-type]
                    source=_SOURCE, fetched_at_ms=fetched_ms, as_of_ms=as_of,
                    data=None, stale=False, reason_code=reason,
                    error_message=sanitize_error_message(f"0x price failed: {str(exc)[:160]}"),
                )
            try:
                self._next_allowed_mono = float(self._monotonic()) + self._min_interval_sec
            except Exception:
                pass
            self.sends += 1
        # 6. schema gate: price only -- transaction/calldata is rejected.
        if not isinstance(payload, Mapping):
            return ProviderResult(
                status="ERROR", source=_SOURCE, fetched_at_ms=fetched_ms,
                as_of_ms=as_of, data=None, stale=False,
                reason_code=PRICE_SCHEMA_REJECTED,
                error_message=sanitize_error_message("0x price payload is not a mapping"),
            )
        for tx_key in _TX_PAYLOAD_KEYS:
            if tx_key in payload and payload.get(tx_key) not in (None, "", {}, []):
                return ProviderResult(
                    status="ERROR", source=_SOURCE, fetched_at_ms=fetched_ms,
                    as_of_ms=as_of, data=None, stale=False,
                    reason_code=PRICE_SCHEMA_REJECTED,
                    error_message=sanitize_error_message(
                        "0x price response carries transaction payload; price-only schema rejects it"),
                )
        raw_data = payload.get("data")
        if isinstance(raw_data, str) and raw_data.startswith("0x") and len(raw_data) > 42:
            return ProviderResult(
                status="ERROR", source=_SOURCE, fetched_at_ms=fetched_ms,
                as_of_ms=as_of, data=None, stale=False,
                reason_code=PRICE_SCHEMA_REJECTED,
                error_message=sanitize_error_message(
                    "0x price response carries calldata-like data; price-only schema rejects it"),
            )
        buy_raw = payload.get("buyAmount", payload.get("buy_amount"))
        sell_raw = payload.get("sellAmount", payload.get("sell_amount"))
        if not _is_integer_string(buy_raw) or not _is_integer_string(sell_raw):
            return self._unavailable(
                NO_ROUTE, fetched_ms, "0x price returned no executable route amounts")
        buy_s = str(buy_raw).strip()
        sell_s = str(sell_raw).strip()
        # 7. gas may be null (PARTIAL, never "0").
        gas_units: str | None = None
        gas_price: str | None = None
        for key in ("gas", "gasUnits", "gas_units"):
            candidate = payload.get(key)
            if candidate is None:
                continue
            text = str(candidate).strip()
            if text.isdigit():
                gas_units = text
                break
        for key in ("gasPrice", "gas_price"):
            candidate = payload.get(key)
            if candidate is None:
                continue
            try:
                parsed = Decimal(str(candidate).strip())
            except (InvalidOperation, ValueError, AttributeError):
                continue
            if parsed.is_finite() and parsed >= 0:
                gas_price = str(candidate).strip()
                break
        block_number = _parse_block_number(payload)
        provider_expiry = _parse_expiry_ms(payload)
        if provider_expiry is not None:
            expires_at = int(provider_expiry)
            expiry_source = "provider"
            if expires_at <= fetched_ms:
                return self._unavailable(
                    QUOTE_EXPIRED, fetched_ms, "0x price expiry already passed; not served")
        else:
            expires_at = fetched_ms + int(self._quote_ttl_sec) * 1000
            expiry_source = f"local-fallback-{int(self._quote_ttl_sec)}s"
        # 8. VWAP in USDC per token (Decimal only). Direction decides which
        # atomic side is the token leg; the other side is USDC (6 decimals).
        if direction == "buy":
            token_atomic_s, usdc_atomic_s = buy_s, sell_s
        else:
            token_atomic_s, usdc_atomic_s = sell_s, buy_s
        try:
            with localcontext() as ctx:
                ctx.prec = 80
                token_human = Decimal(token_atomic_s) / (Decimal(10) ** int(token_decimals))
                usdc_human = Decimal(usdc_atomic_s) / (Decimal(10) ** int(QUOTE_ASSET_DECIMALS))
                if token_human <= 0 or usdc_human <= 0:
                    raise InvalidOperation("non-positive leg")
                vwap = usdc_human / token_human
            vwap_s = str(vwap)
            Decimal(vwap_s)  # re-parse guard
        except (InvalidOperation, ValueError, ArithmeticError, ZeroDivisionError):
            return self._unavailable(
                NO_ROUTE, fetched_ms, "0x price amounts do not form a positive price")
        has_gas = gas_units is not None and gas_price is not None
        if has_gas:
            result_status: str = "OK"
            reason: str | None = None
        else:
            result_status = "PARTIAL"
            reason = ONCHAIN_GAS_UNAVAILABLE
        if direction == "buy":
            buy_vwap, sell_vwap = vwap_s, None
            buy_exec, sell_exec = qty_str, None
        else:
            buy_vwap, sell_vwap = None, vwap_s
            buy_exec, sell_exec = None, qty_str
        quote = OnchainQuote(
            venue="ONCHAIN_DEX", canonical_id=canonical_id, chain="ethereum",
            contract_address=norm.display, as_of_ms=as_of,
            expires_at_ms=expires_at, requested_canonical_qty=qty_str,
            provider_id=PROVIDER_ID, api_version=API_VERSION, chain_id=CHAIN_ID,
            block_number=block_number,
            atomic_amounts={"buyAmount": buy_s, "sellAmount": sell_s},
            decimals=int(token_decimals), quote_kind=ONCHAIN_QUOTE_KIND,
            buy_vwap=buy_vwap, sell_vwap=sell_vwap,
            buy_executable_qty=buy_exec, sell_executable_qty=sell_exec,
            gas_units=gas_units, gas_price=gas_price, native_gas_fx=None,
            estimated_gas_usd=None, estimated_fee_usd=None,
            token_tax_status="UNKNOWN", route_complete=True,
            simulation_verified=False, quote_currency=QUOTE_ASSET,
            quote_to_usd=None, fetched_at_ms=fetched_ms,
            status=result_status, reason_code=reason,
            # R06b (D06.2/D06.3): honest fee flag + indicative-only projection.
            # 0x price carries no fee leg (fee None, gas may be null), so
            # fees_included=False; quote stays INDICATIVE with no execution
            # permission (read-only price, never a trade payload).
            fees_included=False,
        )
        # Stamp the expiry provenance without touching the frozen DTO shape:
        # consumers read `expires_at_ms`; operators read the transport log.
        _ = expiry_source
        if result_status == "OK":
            self._last_ok_ms = fetched_ms
            self._last_block = block_number
        return ProviderResult(
            status=result_status,  # type: ignore[arg-type]
            source=_SOURCE, fetched_at_ms=fetched_ms, as_of_ms=as_of,
            data=quote, stale=False, reason_code=reason, error_message=None,
        )
