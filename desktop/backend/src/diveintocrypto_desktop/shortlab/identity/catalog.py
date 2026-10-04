"""F04: production identity directory (design A6.3; plan F04.1-F04.4).

Three layers, in priority order:

1. ``identity/verified_assets.yaml`` -- human-verified ordinary-1x and
   1000-denomination sets (canonical/provider IDs, chain/address,
   multiplier provenance + version). Loaded at startup; never waits on the
   network directory.
2. CoinGecko ``/coins/list?include_platform=false`` -- id/symbol/name only,
   cached 24h with 72h grace and a 32MiB expanded-response ceiling. At most
   50 bound assets per refresh gain platform/address detail through
   per-coin documents. DEMO pins ``api.coingecko.com`` +
   ``x_cg_demo_api_key``; PRO pins ``pro-api.coingecko.com`` +
   ``x_cg_pro_api_key`` -- the plan selects both together, so a key is never
   sent to the other host. Keyless: local verified set only, network layer
   reports ``UNCONFIGURED`` with zero sends.
3. User overlay from ``SHORTLAB_IDENTITY_OVERRIDES_PATH`` -- wins per
   symbol under the same schema + chain validation; syntax errors fail loud.

HTTP discipline: the directory travels on the shared CoinGecko limiter
(``COINGECKO_MAX_PER_MIN``), not on the Binance ``RequestBudget`` send
budget (whose F03 weights fixture has no CoinGecko family -- such sends
would be ``UNBUDGETED_ENDPOINT``). ``refresh`` still takes a
``RequestContext``: trace/job/host/identity-snapshot provenance travel with
every call, the budget is explicitly stripped in the derived context, and
the shared 30s session timeout applies. Tests inject a fake HTTP layer, so
no live requests ever happen in the suite; the keyless branch is executed
for real (send-count asserted, never skipped).

The directory only yields *candidates*; binding priority (manual override >
trusted chain+contract > unique exact symbol, conflicts stay UNRESOLVED,
1000-prefix never guesses a multiplier) is enforced by
``identity.resolver.resolve_identity``, whose entry point is unchanged.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import pathlib
import time
import urllib.parse
from typing import Any, Awaitable, Callable, Mapping

import yaml
from aiolimiter import AsyncLimiter

from diveintocrypto_desktop.shortlab.identity import overrides as _overrides
from diveintocrypto_desktop.shortlab.identity import resolver as _resolver
from diveintocrypto_desktop.shortlab.models import ProviderResult, sanitize_error_message
from diveintocrypto_desktop.shortlab.providers import coingecko as _cg
from diveintocrypto_desktop.shortlab.request_budget import (
    RequestContext,
    make_request_context,
)

__all__ = [
    "IDENTITY_CATALOG_VERSION",
    "DIRECTORY_SOURCE",
    "DIRECTORY_TTL_SEC",
    "DIRECTORY_GRACE_SEC",
    "MAX_PLATFORM_DETAIL_PER_REFRESH",
    "IdentityCatalog",
    "build_identity_snapshot_id",
    "load_verified_assets",
]

#: Version stamp for the catalog contract consumed by F05/F06.
IDENTITY_CATALOG_VERSION = "identity-catalog-v1"

DIRECTORY_SOURCE = "coingecko-directory"
DIRECTORY_TTL_SEC = _cg.COINGECKO_DIRECTORY_TTL_SEC
DIRECTORY_GRACE_SEC = _cg.COINGECKO_DIRECTORY_GRACE_SEC
MAX_PLATFORM_DETAIL_PER_REFRESH = _cg.COINGECKO_MAX_PLATFORM_DETAIL

VERIFIED_ASSETS_PATH = pathlib.Path(__file__).with_name("verified_assets.yaml")

_HttpGet = Callable[[str, Mapping[str, Any], Any], Awaitable[Any]]
_Clock = Callable[[], int]

_limiters: dict[asyncio.AbstractEventLoop, AsyncLimiter] = {}


def _shared_limiter() -> AsyncLimiter:
    loop = asyncio.get_running_loop()
    limiter = _limiters.get(loop)
    if limiter is None:
        limiter = AsyncLimiter(
            max_rate=_cg.COINGECKO_MAX_PER_MIN, time_period=_cg.COINGECKO_PERIOD_SEC
        )
        _limiters[loop] = limiter
    return limiter


async def _default_http_get(
    url: str, params: Mapping[str, Any], request_context: RequestContext | None
) -> Any:
    """Default transport: shared ``data.http.get_json`` (30s total timeout).

    ``request_context`` travels for traceability; its budget is always
    stripped by the caller (see :meth:`IdentityCatalog._directory_context`).
    """
    from diveintocrypto_desktop.data.http import get_json

    if request_context is not None:
        return await get_json(url, dict(params), request_context=request_context)
    return await get_json(url, dict(params))


def load_verified_assets(path: str | pathlib.Path | None = None) -> dict:
    """Load the human-verified asset set (layer 1).

    Returns ``{"version": int, "assets": {futures_symbol: entry}}``. Every
    entry passes the manual-override schema; rows overlapping
    ``asset_overrides.yaml`` must agree field-for-field (asserted by the
    catalog suite, since this file mirrors the frozen Task-5 table
    byte-for-byte on overlap -- including its known quirks -- rather than
    guessing corrections). Any violation raises ``ValueError`` (fail loud,
    never an empty set).
    """
    doc_path = pathlib.Path(path) if path is not None else VERIFIED_ASSETS_PATH
    with open(doc_path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"verified assets document must be a mapping: {doc_path}")
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError(f"verified assets 'version' must be an int: {doc_path}")
    assets = data.get("assets")
    if not isinstance(assets, dict) or not assets:
        raise ValueError(f"verified assets 'assets' must be a non-empty mapping: {doc_path}")
    for symbol, entry in assets.items():
        try:
            _overrides.validate_override_entry(symbol, entry)
        except ValueError as exc:
            raise ValueError(f"{doc_path}: {exc}") from exc
    return {"version": version, "assets": dict(assets)}


def _identity_field(identity: Any, key: str) -> Any:
    if isinstance(identity, Mapping):
        return identity.get(key)
    return getattr(identity, key, None)


def build_identity_snapshot_id(
    futures_symbol: str,
    identity: Any,
    mapping_version: str,
    observed_at_ms: int,
) -> str:
    """Content-address one identity binding (F05 handoff contract).

    ``isl-`` + 32 hex chars of SHA-256 over the canonical JSON of
    (futures symbol, canonical/provider IDs, spot symbol, multiplier +
    source, chain, address, confidence, source, mapping version,
    observed_at). Deterministic across restarts for identical content; any
    binding, version or time change yields a new ID, so overlay/directory
    changes only affect NEW snapshots and F05 can pin
    ``source_meta._identity_snapshot_id`` plus the immutable
    ``sl_identity_snapshot`` row behind it.
    """
    canonical = {
        "futures_symbol": str(futures_symbol),
        "canonical_id": _identity_field(identity, "canonical_id"),
        "coingecko_id": _identity_field(identity, "coingecko_id"),
        "binance_spot_symbol": _identity_field(identity, "binance_spot_symbol"),
        "contract_multiplier": _identity_field(identity, "contract_multiplier"),
        "multiplier_source": _identity_field(identity, "multiplier_source"),
        "chain": _identity_field(identity, "chain"),
        "contract_address": _identity_field(identity, "contract_address"),
        "mapping_confidence": _identity_field(identity, "mapping_confidence"),
        "mapping_source": _identity_field(identity, "mapping_source"),
        "mapping_version": str(mapping_version),
        "observed_at_ms": int(observed_at_ms),
    }
    blob = json.dumps(canonical, separators=(",", ":"), sort_keys=True, default=str)
    return "isl-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


class IdentityCatalog:
    """Three-layer identity directory with content-addressed snapshots.

    ``verified`` / ``overrides_doc`` accept embedded documents (tests);
    ``None`` loads the packaged YAML files. ``overlay_path`` wins over the
    ``SHORTLAB_IDENTITY_OVERRIDES_PATH`` environment variable.
    """

    def __init__(
        self,
        *,
        api_plan: str | None = "demo",
        api_key: str | None = None,
        verified: Mapping[str, Any] | None = None,
        overrides_doc: Mapping[str, Any] | None = None,
        overlay_path: str | pathlib.Path | None = None,
        clock: _Clock | None = None,
        http_get: _HttpGet | None = None,
        rate_limiter: Any | None = None,
        directory_ttl_sec: int = DIRECTORY_TTL_SEC,
        directory_grace_sec: int = DIRECTORY_GRACE_SEC,
        max_platform_detail: int = MAX_PLATFORM_DETAIL_PER_REFRESH,
        max_list_bytes: int = _cg.COINGECKO_LIST_MAX_BYTES,
    ) -> None:
        self._api_plan = str(api_plan or "demo").strip().lower()
        self._api_key = api_key.strip() if isinstance(api_key, str) else ""
        self._verified = load_verified_assets() if verified is None else _checked_verified(verified)
        self._effective = _overrides.load_effective_overrides(overlay_path, embedded=overrides_doc)
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._http_get = http_get or _default_http_get
        self._limiter_override = rate_limiter
        self._directory_ttl_ms = int(directory_ttl_sec) * 1000
        self._directory_grace_ms = int(directory_grace_sec) * 1000
        self._max_platform_detail = int(max_platform_detail)
        self._max_list_bytes = int(max_list_bytes)
        self._entries: tuple[_cg.CoinDirectoryEntry, ...] = ()
        self._by_symbol: dict[str, list[_cg.CoinDirectoryEntry]] = {}
        self._checksum: str | None = None
        self._known_at_ms: int | None = None
        self._next_allowed_at_ms: int | None = None
        self._platforms: dict[str, dict[str, str]] = {}
        self._platforms_known_at_ms: dict[str, int] = {}
        self._loaded_at_ms = int(self._clock())
        self.refreshes = 0
        self.failures = 0

    # -- introspection ------------------------------------------------------
    @property
    def known_at_ms(self) -> int | None:
        return self._known_at_ms

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    @property
    def checksum(self) -> str | None:
        return self._checksum

    @property
    def next_allowed_at_ms(self) -> int | None:
        return self._next_allowed_at_ms

    @property
    def mapping_version(self) -> str:
        """Layer-version stamp for snapshot IDs and F05 provenance."""
        short = self._checksum[:12] if self._checksum else "none"
        return (
            f"verified-v{self._verified['version']}"
            f"+overrides-v{self._effective['version']}"
            f"+dir-{short}"
        )

    def snapshot(self) -> dict[str, Any]:
        """Redacted state summary (never carries key material)."""
        return {
            "catalog_version": IDENTITY_CATALOG_VERSION,
            "mapping_version": self.mapping_version,
            "entries": len(self._entries),
            "known_at_ms": self._known_at_ms,
            "checksum": self._checksum,
            "next_allowed_at_ms": self._next_allowed_at_ms,
            "platform_assets": len(self._platforms),
            "api_plan": self._api_plan,
            "refreshes": self.refreshes,
            "failures": self.failures,
        }

    def snapshot_id_for(
        self, futures_symbol: str, identity: Any, observed_at_ms: int
    ) -> str:
        """Content-address ``identity`` for ``futures_symbol`` (F05 handoff)."""
        return build_identity_snapshot_id(
            futures_symbol, identity, self.mapping_version, observed_at_ms
        )

    # -- candidates ----------------------------------------------------------
    def _directory_state(self, now_ms: int) -> str:
        """``fresh`` / ``stale`` (inside grace) / ``expired`` / ``empty``."""
        if self._known_at_ms is None:
            return "empty"
        age = now_ms - self._known_at_ms
        if age <= self._directory_ttl_ms:
            return "fresh"
        if age <= self._directory_ttl_ms + self._directory_grace_ms:
            return "stale"
        return "expired"

    def _mapping_from_override(self, symbol: str, entry: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(entry)
        out.update(
            {
                "provider_symbol": symbol,
                "catalog_source": "override",
                "mapping_source": "MANUAL",
                "catalog_known_at_ms": self._loaded_at_ms,
                "catalog_stale": False,
            }
        )
        return out

    def _mapping_from_verified(self, symbol: str, entry: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(entry)
        out.update(
            {
                "provider_symbol": symbol,
                "catalog_source": "verified-assets",
                "mapping_source": "MANUAL",
                "catalog_known_at_ms": self._loaded_at_ms,
                "catalog_stale": False,
            }
        )
        return out

    def _mappings_from_directory(
        self, entry: _cg.CoinDirectoryEntry, *, stale: bool, known_at_ms: int
    ) -> tuple[dict[str, Any], ...]:
        platforms = self._platforms.get(entry.coin_id) or {}
        if not platforms:
            return (
                {
                    "provider_symbol": entry.symbol,
                    "coingecko_id": entry.coin_id,
                    "name": entry.name,
                    "chain": None,
                    "contract_address": None,
                    "catalog_source": "coingecko-directory",
                    "catalog_stale": stale,
                    "catalog_known_at_ms": known_at_ms,
                },
            )
        return tuple(
            {
                "provider_symbol": entry.symbol,
                "coingecko_id": entry.coin_id,
                "name": entry.name,
                "chain": platform,
                "contract_address": address,
                "catalog_source": "coingecko-directory",
                "catalog_stale": stale,
                "catalog_known_at_ms": known_at_ms,
            }
            for platform, address in sorted(platforms.items())
        )

    def candidates(self, symbol: str, cutoff_ms: int) -> tuple[Mapping[str, Any], ...]:
        """Candidate mappings for ``symbol`` usable at ``cutoff_ms``.

        Priority: exact overlay/built-in override (single MANUAL mapping) >
        exact verified-asset row (single) > exact normalized-symbol directory
        matches (all of them, so homonyms/cross-chain twins stay UNRESOLVED
        downstream). Layers known after ``cutoff_ms`` are excluded (PIT);
        an expired directory (past TTL+grace) drops its layer while the
        human-verified layers keep serving on their own verification time.
        """
        cutoff = int(cutoff_ms)
        key = symbol.strip().upper() if isinstance(symbol, str) else ""
        if not key:
            return ()
        table = self._effective["overrides"]
        entry = table.get(key) or table.get(symbol)
        if isinstance(entry, Mapping):
            if self._loaded_at_ms <= cutoff:
                return (self._mapping_from_override(key, entry),)
            return ()
        assets = self._verified["assets"]
        verified = assets.get(key) or assets.get(symbol)
        if isinstance(verified, Mapping):
            if self._loaded_at_ms <= cutoff:
                return (self._mapping_from_verified(key, verified),)
            return ()
        now = int(self._clock())
        state = self._directory_state(now)
        if state in ("empty", "expired") or self._known_at_ms is None:
            return ()
        if self._known_at_ms > cutoff:
            return ()
        matches = self._by_symbol.get(_resolver.normalize_base(key), [])
        stale = state == "stale"
        out: list[dict[str, Any]] = []
        for row in matches:
            out.extend(
                self._mappings_from_directory(
                    row, stale=stale, known_at_ms=self._known_at_ms
                )
            )
        return tuple(out)

    # -- refresh --------------------------------------------------------------
    def _route(self) -> tuple[str, str]:
        """Strict (host, key-param) pair for the configured plan (no sends)."""
        return _cg.coingecko_route(self._api_plan, self._api_key or None)

    def _directory_context(self, request_context: RequestContext | None) -> RequestContext:
        """Derived context for directory sends: provenance travels, the
        Binance send budget is explicitly stripped (CoinGecko sends are
        governed by the shared CoinGecko limiter -- the F03 weights fixture
        has no CoinGecko family, so budgeted sends would be denied as
        ``UNBUDGETED_ENDPOINT``)."""
        base, _ = self._route()
        host = urllib.parse.urlparse(base).netloc or base
        if request_context is not None:
            return dataclasses.replace(
                request_context, budget=None, host=host, endpoint_family="coingecko-directory"
            )
        return make_request_context(
            None,
            job_type="identity_catalog_refresh",
            host=host,
            endpoint_family="coingecko-directory",
        )

    @property
    def _active_limiter(self) -> Any:
        return self._limiter_override if self._limiter_override is not None else _shared_limiter()

    def _summary(self, *, stale: bool) -> dict[str, Any]:
        _, key_param = self._route()
        base, _ = self._route()
        url = f"{base}/api/v3/coins/list"
        return {
            "entries": len(self._entries),
            "known_at_ms": self._known_at_ms,
            "checksum": self._checksum,
            "stale": stale,
            "api_plan": self._api_plan,
            "host": urllib.parse.urlparse(base).netloc,
            "source_url_redacted": _cg.redact_coins_list_url(url, {"include_platform": "false", key_param: "***"}),
            "next_allowed_at_ms": self._next_allowed_at_ms,
        }

    def _rate_limited_result(
        self, now_ms: int, *, retry_after_ms: int, detail: str
    ) -> ProviderResult[dict]:
        self._next_allowed_at_ms = now_ms + retry_after_ms
        self.failures += 1
        have = self._known_at_ms is not None
        data = self._summary(stale=True) if have else None
        return ProviderResult(
            status="UNAVAILABLE",
            source=DIRECTORY_SOURCE,
            fetched_at_ms=now_ms,
            as_of_ms=self._known_at_ms,
            data=data,
            stale=have,
            reason_code=_cg.COINGECKO_RATE_LIMITED,
            error_message=sanitize_error_message(detail),
        )

    def _failure_result(
        self,
        now_ms: int,
        *,
        status: str,
        reason_code: str,
        detail: str,
    ) -> ProviderResult[dict]:
        self.failures += 1
        have = self._known_at_ms is not None
        data = self._summary(stale=True) if have else None
        return ProviderResult(
            status=status,  # type: ignore[arg-type]
            source=DIRECTORY_SOURCE,
            fetched_at_ms=now_ms,
            as_of_ms=self._known_at_ms,
            data=data,
            stale=have,
            reason_code=reason_code,
            error_message=sanitize_error_message(detail),
        )

    async def refresh(
        self, request_context: RequestContext | None = None
    ) -> ProviderResult[dict]:
        """Refresh the ``/coins/list`` directory (24h TTL, 72h grace).

        Keyless (no ``api_key``): ``UNAVAILABLE``/``UNCONFIGURED`` with zero
        sends -- the local verified set keeps serving. Inside the TTL the
        cached receipt is returned with zero sends. 429 sets
        ``next_allowed_at_ms`` (Retry-After, default 60s) without retrying
        the whole directory; any failure keeps the previous
        checksum/``known_at`` (atomic replace only after schema + size
        checks pass).
        """
        now = int(self._clock())
        if not self._api_key:
            return ProviderResult(
                status="UNAVAILABLE",
                source=DIRECTORY_SOURCE,
                fetched_at_ms=now,
                as_of_ms=self._known_at_ms,
                data=None,
                stale=False,
                reason_code=_cg.IDENTITY_DIRECTORY_UNCONFIGURED,
                error_message=(
                    "no CoinGecko API key: only the local verified set is "
                    "available; the network directory is UNCONFIGURED"
                ),
            )
        # Unknown plans raise before any send (config bug, fail loud).
        base, _ = self._route()
        if self._known_at_ms is not None and now - self._known_at_ms <= self._directory_ttl_ms:
            self.refreshes += 1
            return ProviderResult(
                status="OK",
                source=DIRECTORY_SOURCE,
                fetched_at_ms=self._known_at_ms,
                as_of_ms=self._known_at_ms,
                data=self._summary(stale=False),
                stale=False,
                reason_code=None,
                error_message=None,
            )
        if self._next_allowed_at_ms is not None and now < self._next_allowed_at_ms:
            return self._failure_result(
                now,
                status="UNAVAILABLE",
                reason_code=_cg.COINGECKO_RATE_LIMITED,
                detail=(
                    "directory backoff active until "
                    f"{self._next_allowed_at_ms}; refusing to retry the full list"
                ),
            )
        url, params = _cg.build_coins_list_request(
            base, self._api_key, self._api_plan, include_platform=False
        )
        ctx = self._directory_context(request_context)
        try:
            async with self._active_limiter:
                payload = await self._http_get(url, params, ctx)
        except Exception as exc:  # noqa: BLE001 - encapsulated, never raised
            status_hint = getattr(exc, "status", None)
            retry_after = getattr(exc, "retry_after", None)
            if status_hint == 429 or "429" in f"{type(exc).__name__} {exc}":
                wait_ms = (
                    int(float(retry_after) * 1000)
                    if retry_after is not None
                    else 60_000
                )
                return self._rate_limited_result(
                    now, retry_after_ms=wait_ms, detail=f"rate limited (status 429): {exc}"
                )
            from diveintocrypto_desktop.data.http import TransientUpstreamError

            if isinstance(exc, TransientUpstreamError):
                return self._failure_result(
                    now,
                    status="ERROR",
                    reason_code=_cg.COINGECKO_UPSTREAM_ERROR,
                    detail=f"upstream status {exc.status} on coins/list",
                )
            return self._failure_result(
                now,
                status="UNAVAILABLE",
                reason_code=_cg.COINGECKO_NETWORK_ERROR,
                detail=f"network error on coins/list: {type(exc).__name__}",
            )
        try:
            entries = _cg.parse_coins_list(payload, max_bytes=self._max_list_bytes)
        except _cg.CoinGeckoBadResponse as exc:
            return self._failure_result(
                now, status="ERROR", reason_code=_cg.COINGECKO_BAD_RESPONSE, detail=str(exc)
            )
        # Atomic replace only after schema + size checks passed.
        index: dict[str, list[_cg.CoinDirectoryEntry]] = {}
        for row in entries:
            index.setdefault(_resolver.normalize_base(row.symbol), []).append(row)
        blob = json.dumps(
            sorted((e.coin_id, e.symbol, e.name) for e in entries),
            separators=(",", ":"),
        )
        self._entries = entries
        self._by_symbol = index
        self._checksum = hashlib.sha256(blob.encode("utf-8")).hexdigest()
        self._known_at_ms = now
        self.refreshes += 1
        return ProviderResult(
            status="OK",
            source=DIRECTORY_SOURCE,
            fetched_at_ms=now,
            as_of_ms=now,
            data=self._summary(stale=False),
            stale=False,
            reason_code=None,
            error_message=None,
        )

    async def refresh_platform_details(
        self, coin_ids: Any, request_context: RequestContext | None = None
    ) -> ProviderResult[dict]:
        """Enrich at most ``max_platform_detail`` (50) bound assets with
        ``{platform: address}`` from per-coin documents, sharing the
        CoinGecko limiter. Order-preserving dedup; ids past the cap are
        reported as ``skipped_over_cap`` (never silently dropped from the
        accounting). Per-id failures keep prior data and count as errors.
        """
        now = int(self._clock())
        if not self._api_key:
            return ProviderResult(
                status="UNAVAILABLE",
                source=DIRECTORY_SOURCE,
                fetched_at_ms=now,
                as_of_ms=None,
                data=None,
                stale=False,
                reason_code=_cg.IDENTITY_DIRECTORY_UNCONFIGURED,
                error_message="no CoinGecko API key: platform enrichment is UNCONFIGURED",
            )
        base, _ = self._route()
        seen: list[str] = []
        for raw in coin_ids or []:
            text = str(raw).strip() if isinstance(raw, str) else ""
            if text and text not in seen:
                seen.append(text)
        wanted = seen[: self._max_platform_detail]
        skipped = max(0, len(seen) - len(wanted))
        ctx = self._directory_context(request_context)
        fetched = 0
        errors = 0
        for coin_id in wanted:
            url, params = _cg.build_coin_detail_request(base, self._api_key, self._api_plan, coin_id)
            try:
                async with self._active_limiter:
                    payload = await self._http_get(url, params, ctx)
            except Exception:  # noqa: BLE001 - per-id failure keeps prior data
                errors += 1
                continue
            platforms = _cg.parse_coin_platforms(payload)
            self._platforms[coin_id] = platforms
            self._platforms_known_at_ms[coin_id] = now
            fetched += 1
        data = {
            "requested": len(seen),
            "fetched": fetched,
            "skipped_over_cap": skipped,
            "errors": errors,
            "known_at_ms": now,
        }
        if fetched == 0 and errors > 0:
            return ProviderResult(
                status="UNAVAILABLE",
                source=DIRECTORY_SOURCE,
                fetched_at_ms=now,
                as_of_ms=None,
                data=data,
                stale=False,
                reason_code=_cg.COINGECKO_NETWORK_ERROR,
                error_message="all platform detail fetches failed; prior data kept",
            )
        return ProviderResult(
            status="OK" if errors == 0 else "PARTIAL",  # type: ignore[arg-type]
            source=DIRECTORY_SOURCE,
            fetched_at_ms=now,
            as_of_ms=now,
            data=data,
            stale=False,
            reason_code=None if errors == 0 else _cg.COINGECKO_UPSTREAM_ERROR,
            error_message=None,
        )


def _checked_verified(doc: Mapping[str, Any]) -> dict:
    """Validate an embedded verified-assets document (tests/F06 wiring)."""
    if not isinstance(doc, Mapping):
        raise ValueError("verified assets must be a mapping")
    version = doc.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError("verified assets 'version' must be an int")
    assets = doc.get("assets")
    if not isinstance(assets, dict):
        raise ValueError("verified assets 'assets' must be a mapping")
    for symbol, entry in assets.items():
        _overrides.validate_override_entry(symbol, entry)
    return {"version": version, "assets": dict(assets)}
