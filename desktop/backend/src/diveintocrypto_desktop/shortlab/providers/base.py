"""Short-Lab provider registry (Task 9).

``ProviderRegistry`` is the single entry point through which the runtime and
service layers (Task 13) resolve external providers by name. Unknown names
yield a shared :class:`NullProvider` -- an explicit ``UNAVAILABLE`` result
with ``PROVIDER_NOT_CONFIGURED`` -- instead of raising, so Phase 5/6
(``unlock`` / ``social`` / ``catalyst``, Task 17) can be registered on the
same runtime without copying the scheduler or changing this module's API.

DTOs live in ``shortlab.models`` (frozen by Task 1); this module imports
``ProviderResult`` and never redefines it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from diveintocrypto_desktop.shortlab.models import ProviderResult

PROVIDER_NOT_CONFIGURED = "PROVIDER_NOT_CONFIGURED"

#: A provider without a verified real-HTTP transport (skeleton, ``.example``
#: fixture or injected fake without an explicit opt-in) must never register
#: as a READY-capable provider (F04.4 / design A6.3: FULL needs real request,
#: schema, identity, time and cost-limit contract verification).
PROVIDER_TRANSPORT_UNVERIFIED = "PROVIDER_TRANSPORT_UNVERIFIED"


@dataclass(frozen=True)
class ProviderCapability:
    """READY-eligibility of one provider instance (F04).

    ``ready`` is True only for a registered, non-``.example`` provider whose
    ``transport_verified`` flag confirms a real HTTP transport. ``reason_code``
    is ``None`` when ready, otherwise ``PROVIDER_NOT_CONFIGURED`` or
    ``PROVIDER_TRANSPORT_UNVERIFIED``.
    """

    name: str
    ready: bool
    reason_code: str | None = None
    detail: str | None = None


def provider_capability(provider: Any) -> ProviderCapability:
    """Describe whether ``provider`` may register as a verified capability.

    Never raises: unknown shapes report not-ready. ``transport_verified``
    is opt-in on each provider (only a real network fetcher sets it by
    default); test doubles stay silent unless explicitly flagged.
    """
    name = getattr(provider, "name", None)
    name = str(name) if isinstance(name, str) and name else "unknown"
    if isinstance(provider, NullProvider) or name == "null":
        return ProviderCapability(
            name=name,
            ready=False,
            reason_code=PROVIDER_NOT_CONFIGURED,
            detail="no provider registered under this name",
        )
    if name.endswith(".example"):
        return ProviderCapability(
            name=name,
            ready=False,
            reason_code=PROVIDER_TRANSPORT_UNVERIFIED,
            detail=".example skeletons never carry a verified transport",
        )
    if not bool(getattr(provider, "transport_verified", False)):
        return ProviderCapability(
            name=name,
            ready=False,
            reason_code=PROVIDER_TRANSPORT_UNVERIFIED,
            detail="provider has no verified HTTP transport (skeleton/no-fetcher)",
        )
    return ProviderCapability(name=name, ready=True)


class Provider(Protocol):
    """Structural provider contract used by the registry and the runtime.

    Concrete providers (CoinGecko now, Unlock/Social/Catalyst in Task 17)
    implement ``fetch`` structurally; they do not need to subclass anything,
    which keeps Task 17 free of import cycles with future modules.
    """

    name: str

    async def fetch(self, identity: Any) -> ProviderResult[Any]:
        """Fetch provider data bound to ``identity``; never raise for
        upstream failures -- wrap them in a ``ProviderResult``."""
        ...  # pragma: no cover - protocol stub


class NullProvider:
    """Zero-I/O placeholder for unregistered / keyless providers.

    Returns ``UNAVAILABLE`` + ``PROVIDER_NOT_CONFIGURED`` with ``data=None``.
    Used for Phase 5/6 providers before Task 17 wires the real ones, and as
    the registry fallback so the runtime never branches on ``None``.
    """

    name = "null"

    def __init__(self, clock: Callable[[], int] | None = None) -> None:
        self._clock = clock or (lambda: int(time.time() * 1000))

    async def fetch(self, identity: Any) -> ProviderResult[None]:
        _ = identity  # accepted for signature compatibility; never inspected
        return ProviderResult(
            status="UNAVAILABLE",
            source="null",
            fetched_at_ms=self._clock(),
            as_of_ms=None,
            data=None,
            stale=False,
            reason_code=PROVIDER_NOT_CONFIGURED,
            error_message=None,
        )


class ProviderRegistry:
    """Name -> provider mapping with a ``NullProvider`` fallback.

    ``register`` overwrites an existing entry on purpose: Task 17 upgrades a
    previously-null provider (e.g. ``unlock``) to its real implementation on
    the same runtime. Lookups are exact (case-sensitive); provider names are
    lowercase by convention (``coingecko``, ``unlock``, ``social``,
    ``catalyst``).
    """

    def __init__(self, clock: Callable[[], int] | None = None) -> None:
        self._providers: dict[str, Any] = {}
        self._null = NullProvider(clock=clock)

    def register(self, name: str, provider: Any) -> None:
        self._providers[name] = provider

    def get(self, name: str) -> Any:
        """Return the provider registered under ``name``, or the shared
        :class:`NullProvider` when nothing is registered."""
        return self._providers.get(name, self._null)

    def has(self, name: str) -> bool:
        return name in self._providers

    def names(self) -> tuple[str, ...]:
        return tuple(self._providers)
