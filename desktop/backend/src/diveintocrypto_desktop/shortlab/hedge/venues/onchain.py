"""H05 on-chain quote protocol (design B12.2, plan H05).

The frozen :class:`OnchainQuote` DTO lives in ``hedge.models`` (H01); this
module only freezes the provider *interface* so H05/H08/H10 share one
shape without copying the DTO.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = [
    "ONCHAIN_QUOTE_KIND",
    "OnchainQuoteProvider",
]

#: The only quote kind V1 may emit (B12.2 indicative-only).
ONCHAIN_QUOTE_KIND = "INDICATIVE"


@runtime_checkable
class OnchainQuoteProvider(Protocol):
    """Read-only on-chain indicative quote surface (B12.2, H05 contract).

    ``quote_buy`` spends the quote asset (USDC) to buy the requested net
    token quantity; ``quote_sell`` sells the same net token quantity for
    the quote asset. Both carry ``quote_kind="INDICATIVE"`` and never
    produce trade payloads. ``health`` reports local configuration/readiness
    without touching an undisclosed health endpoint.
    """

    async def quote_buy(
        self,
        identity: Any,
        requested_canonical_qty: str,
        quote_asset: Any | None = None,
        as_of_ms: int | None = None,
        request_context: Any | None = None,
    ) -> Any: ...

    async def quote_sell(
        self,
        identity: Any,
        requested_canonical_qty: str,
        quote_asset: Any | None = None,
        as_of_ms: int | None = None,
        request_context: Any | None = None,
    ) -> Any: ...

    async def health(self, request_context: Any | None = None) -> Any: ...
