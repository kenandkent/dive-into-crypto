"""Short-Lab shared domain DTOs (frozen by Task 1).

`ProviderResult` / `CandidateState` follow the cross-task DTO contract in
`ShortLab_Implementation_Plan_CN.md` verbatim; Tasks 2/6/9/11/14 import this
module instead of redefining them. `AssetIdentity` / `FeatureSnapshot`
implement design section 4. Any DTO change must first update the plan
contract, the design doc and every consumer task.

`error_message` carries sanitized internal diagnostics only: it must never
contain API keys or other secrets and is never exposed by the public API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Generic, Literal, Mapping, TypeVar

T = TypeVar("T")

ProviderStatus = Literal["OK", "PARTIAL", "NOT_APPLICABLE", "UNAVAILABLE", "ERROR"]
ProviderStatusT = ProviderStatus

CandidateStatusT = Literal["EXCLUDED", "WATCH", "CANDIDATE"]
ExecutionStatusT = Literal["NOT_READY", "READY", "PAUSED", "BLOCKED"]
DisplayStatusT = Literal["EXCLUDED", "WATCH", "CANDIDATE", "READY", "PAUSED", "BLOCKED"]

MappingConfidence = Literal["VERIFIED", "HIGH", "MEDIUM", "LOW", "UNRESOLVED"]
MappingSource = Literal["MANUAL", "CONTRACT", "UNIQUE_SYMBOL", "OTHER"]
MultiplierSource = Literal["EXCHANGE", "MANUAL"]

_ALLOWED_PROVIDER_STATUS = frozenset({"OK", "PARTIAL", "NOT_APPLICABLE", "UNAVAILABLE", "ERROR"})
_ALLOWED_CANDIDATE_STATUS = frozenset({"EXCLUDED", "WATCH", "CANDIDATE"})
_ALLOWED_EXECUTION_STATUS = frozenset({"NOT_READY", "READY", "PAUSED", "BLOCKED"})
_ALLOWED_DISPLAY_STATUS = frozenset({"EXCLUDED", "WATCH", "CANDIDATE", "READY", "PAUSED", "BLOCKED"})
_ALLOWED_MAPPING_CONFIDENCE = frozenset({"VERIFIED", "HIGH", "MEDIUM", "LOW", "UNRESOLVED"})
_ALLOWED_MAPPING_SOURCE = frozenset({"MANUAL", "CONTRACT", "UNIQUE_SYMBOL", "OTHER"})
_ALLOWED_MULTIPLIER_SOURCE = frozenset({"EXCHANGE", "MANUAL"})

# Patterns that may carry secret material. Replacements keep the key name
# (useful for debugging) but drop the value.
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?i)\b(api[_-]?key\s*[:=]\s*)([^\s,;&\"']+)"), r"\1***"),
    (re.compile(r"(?i)\b(secret\s*[:=]\s*)([^\s,;&\"']+)"), r"\1***"),
    (re.compile(r"(?i)\b(token\s*[:=]\s*)([^\s,;&\"']+)"), r"\1***"),
    (re.compile(r"(?i)([?&](?:api[_-]?key|key|token)=)([^&\s\"']*)"), r"\1***"),
    (re.compile(r"(?i)\b(Bearer\s+)([^\s,;&\"']+)"), r"\1***"),
)


def sanitize_error_message(message: str | None) -> str | None:
    """Redact key-like material from an internal diagnostic message."""
    if message is None:
        return None
    cleaned = message
    for pattern, replacement in _SECRET_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned


def _coerce_str_tuple(name: str, value: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    items = tuple(value)
    for item in items:
        if not isinstance(item, str):
            raise ValueError(f"{name} must be a tuple of str, got {item!r}")
    return items


@dataclass(frozen=True)
class ProviderResult(Generic[T]):
    status: ProviderStatus
    source: str
    fetched_at_ms: int
    as_of_ms: int | None
    data: T | None
    stale: bool
    reason_code: str | None
    error_message: str | None  # sanitized internal diagnostics, never in the public API

    def __post_init__(self) -> None:
        if self.status not in _ALLOWED_PROVIDER_STATUS:
            raise ValueError(f"unknown ProviderResult.status: {self.status!r}")
        if self.status == "OK" and self.reason_code is not None:
            raise ValueError("ProviderResult with status OK must have reason_code=None")
        if self.error_message is not None:
            object.__setattr__(
                self, "error_message", sanitize_error_message(self.error_message)
            )


@dataclass(frozen=True)
class CandidateState:
    candidate_status: CandidateStatusT
    execution_status: ExecutionStatusT
    status: DisplayStatusT
    reasons: tuple[str, ...]
    vetoes: tuple[str, ...]
    pauses: tuple[str, ...]
    warnings: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.candidate_status not in _ALLOWED_CANDIDATE_STATUS:
            raise ValueError(f"unknown candidate_status: {self.candidate_status!r}")
        if self.execution_status not in _ALLOWED_EXECUTION_STATUS:
            raise ValueError(f"unknown execution_status: {self.execution_status!r}")
        if self.status not in _ALLOWED_DISPLAY_STATUS:
            raise ValueError(f"unknown status: {self.status!r}")
        for name in ("reasons", "vetoes", "pauses", "warnings"):
            object.__setattr__(self, name, _coerce_str_tuple(name, getattr(self, name)))


@dataclass(frozen=True)
class AssetIdentity:
    canonical_id: str
    display_symbol: str
    name: str | None = None
    binance_futures_symbol: str = ""
    binance_spot_symbol: str | None = None
    contract_multiplier: float | None = None
    multiplier_source: MultiplierSource | None = None
    coingecko_id: str | None = None
    unlock_provider_id: str | None = None
    social_provider_id: str | None = None
    chain: str | None = None
    contract_address: str | None = None
    categories: tuple[str, ...] = ()
    mapping_confidence: MappingConfidence = "UNRESOLVED"
    mapping_source: MappingSource = "OTHER"

    def __post_init__(self) -> None:
        if self.mapping_confidence not in _ALLOWED_MAPPING_CONFIDENCE:
            raise ValueError(f"unknown mapping_confidence: {self.mapping_confidence!r}")
        if self.mapping_source not in _ALLOWED_MAPPING_SOURCE:
            raise ValueError(f"unknown mapping_source: {self.mapping_source!r}")
        if (
            self.multiplier_source is not None
            and self.multiplier_source not in _ALLOWED_MULTIPLIER_SOURCE
        ):
            raise ValueError(f"unknown multiplier_source: {self.multiplier_source!r}")
        if self.contract_multiplier is not None and self.multiplier_source is None:
            raise ValueError(
                "contract_multiplier requires a verified multiplier_source "
                "(EXCHANGE or MANUAL); never guess it from the symbol prefix"
            )
        object.__setattr__(self, "categories", _coerce_str_tuple("categories", self.categories))


@dataclass(frozen=True)
class FeatureSnapshot:
    """Point-in-time feature carrier; raw values only, never API JSON."""

    snapshot_id: str
    symbol: str
    as_of_ms: int
    feature_version: str
    features: Mapping[str, Any] = field(default_factory=dict)
    source_meta: Mapping[str, Any] = field(default_factory=dict)
    data_quality: float | None = None
