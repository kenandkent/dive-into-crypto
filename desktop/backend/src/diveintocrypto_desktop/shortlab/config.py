"""Short-Lab configuration: typed DTOs, strict loading and scoring hash.

Implements design section 24 / 24.1 (`ShortLab_Detailed_Design_CN.md`):

- the packaged `default.yaml` next to this file is the only bundled default;
  no second copy of defaults lives in code;
- `SHORTLAB_CONFIG_PATH` may point at a user override YAML; the override is
  deep-merged over the defaults and may only touch known keys;
- unknown keys, type errors, negative TTLs/periods, broken threshold order
  (`ready_ltss >= candidate_ltss >= watch_ltss`) and score profiles not
  summing to 100 raise `ShortLabConfigError` (a `ValueError`);
- `config_hash` covers only the scoring-relevant subtree
  (`candidate`, `score_weights`, `quality_freshness_sec`,
  `quality_field_overrides_sec`, `liquidity`, `lifecycle`,
  `funding.lookbacks_days`, `veto`) serialized as canonical JSON
  (`sort_keys`, `separators=(',', ':')`, `ensure_ascii=False`,
  `allow_nan=False`, UTF-8) and SHA256-hashed. `refresh`, providers, local
  paths and secret values never enter the hash, logs or the API.
- `policy_hash` (F05, design A10) covers the frozen runtime policy bundle:
  the scoring subtree's execution-relevant keys (`candidate`,
  `quality_freshness_sec`, `quality_field_overrides_sec`, `liquidity`,
  `lifecycle`, `funding.lookbacks_days`, `veto`) plus the A10 base
  subtrees (`identity`, `ingestion`, `evidence`, `maintenance`), stamped
  with `POLICY_VERSION`. `refresh`, `providers`, secret values and local
  paths never enter it either. F06 persists it via F01
  `save_config_snapshot` (keyed by `policy_hash`); history replay without
  the stored policy must fail with `REPLAY_CONFIG_UNAVAILABLE` instead of
  recomputing with today's config.

The existing `engine/loader.py` is intentionally untouched: engine config
and Short-Lab config are independent files with independent readers.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

SHORTLAB_CONFIG_PATH_ENV = "SHORTLAB_CONFIG_PATH"
DEFAULT_CONFIG_PATH = Path(__file__).with_name("default.yaml")

ANALYSIS_TIERS = ("LITE", "FULL")

KNOWN_ENTRY_BLOCKS = ("consensus", "mtf", "micro", "regime", "failed_bounce", "funding")


class ShortLabConfigError(ValueError):
    """Raised for unknown keys, type errors and constraint violations."""


# ---------------------------------------------------------------------------
# Typed DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UniverseConfig:
    limit: int
    shortlist_size: int
    entry_depth_top: int


@dataclass(frozen=True)
class CandidateConfig:
    watch_ltss: int
    candidate_ltss: int
    ready_ltss: int
    ready_entry: int
    ready_data_quality: int
    ready_tradeability_score: int
    entry_required_blocks: tuple[str, ...]


@dataclass(frozen=True)
class TtlGrace:
    ttl: int
    grace: int


@dataclass(frozen=True)
class LiquidityConfig:
    hard_min_futures_volume_usd: int | float
    hard_min_open_interest_usd: int | float
    preferred_futures_volume_usd: int | float
    preferred_open_interest_usd: int | float


@dataclass(frozen=True)
class LifecycleConfig:
    ideal_ath_drawdown_min: float
    ideal_ath_drawdown_max: float


@dataclass(frozen=True)
class FundingConfig:
    lookbacks_days: tuple[int, ...]
    history_requests_per_5min: int
    backfill_symbols_per_batch: int


@dataclass(frozen=True)
class EvidenceCostConfig:
    entry_fee: float
    exit_fee: float
    entry_slippage: float
    exit_slippage: float


@dataclass(frozen=True)
class VetoConfig:
    breakout_24h: float
    breakout_7d: float
    new_token_days: int


@dataclass(frozen=True)
class RefreshConfig:
    fundamental_sec: int
    supply_sec: int
    score_sec: int
    entry_sec: int
    grader_sec: int
    jitter_sec: int
    entry_concurrency: int
    entry_max_upstream_calls_per_run: int
    entry_cache_ttl_sec: int


@dataclass(frozen=True)
class ProviderConfig:
    enabled: bool
    api_key_env: str | None
    api_plan: str | None = None


@dataclass(frozen=True)
class IdentityConfig:
    """A10 base keys: production identity directory (design A6.3/A10)."""

    catalog_ttl_sec: int
    overrides_path_env: str
    catalog_grace_sec: int
    catalog_include_platform: bool
    catalog_max_bytes: int
    platform_detail_batch_size: int


@dataclass(frozen=True)
class IngestionConfig:
    """A10 base keys: ingestion budgets and clocks (design A7/A10)."""

    market_concurrency: int
    max_source_future_skew_sec: int
    contract_refresh_sec: int
    funding_backfill_sec: int
    funding_repair_overlap_intervals: int
    monitor_reserved_fraction: float
    scanner_reserved_fraction: float
    db_queue_limit: int
    db_persist_timeout_sec: int
    db_priority_aging_sec: int


@dataclass(frozen=True)
class EvidenceConfig:
    """A10 base keys: evidence sampling (design A10)."""

    default_history_days: int
    grader_batch_size: int
    sample_policy: str


@dataclass(frozen=True)
class MaintenanceConfig:
    """A10 base keys: retention sweeping (design A10)."""

    retention_sec: int
    snapshot_min_days: int
    batch_delete_limit: int


@dataclass(frozen=True)
class FundingCaptureConfig:
    """B40 funding_capture subtree (H01, design B40).

    Strict keys only; ``score_rules`` keeps the frozen FCS bins verbatim
    (thresholds preserve int-vs-float for the F.1 golden hash).

    R00 (D15): adds ``reference_hold_days`` (default 30); ``fcs_version``
    accepts ``fcs_v1``/``fcs_v2`` with old ``fcs_v1`` normalized to the
    current ``fcs_v2`` for new calculations (old JSON still decodes by bucket).
    """

    enabled: bool
    fcs_version: str
    reference_notional_usd: int | float
    reference_hold_days: int
    entry_gate: Mapping[str, Any]
    exit: Mapping[str, Any]
    statistics: Mapping[str, Any]
    score_rules: Mapping[str, Any]


@dataclass(frozen=True)
class OptimizationConfig:
    """R00 repair freeze (D15): optimization subtree.

    Strict keys only; unknown keys rejected. Decimal-looking ratios and
    fractions stay as validated strings/numbers per D03 (no float ledger).
    """

    schema_version: str
    funding_schedule: Mapping[str, Any]
    decision: Mapping[str, Any]
    protection: Mapping[str, Any]
    evidence: Mapping[str, Any]
    providers: Mapping[str, Any]


@dataclass(frozen=True)
class HedgeConfig:
    """B40 hedge subtree (H01, design B40).

    ``providers.onchain.api_key_env`` must be ``SHORTLAB_0X_API_KEY``;
    key/URL/path values never enter any hash, log or API (see
    ``public_dict`` and the three ``*_config_hash`` projections).
    """

    enabled: bool
    ratio: Mapping[str, Any]
    execution: Mapping[str, Any]
    liquidation: Mapping[str, Any]
    basis: Mapping[str, Any]
    liquidity_monitor: Mapping[str, Any]
    freshness: Mapping[str, TtlGrace]
    refresh: Mapping[str, Any]
    costs: Mapping[str, Any]
    providers: Mapping[str, Any]
    runtime: Mapping[str, Any]
    fx: Mapping[str, Any]
    quality: Mapping[str, Any]


@dataclass(frozen=True)
class ShortLabConfig:
    enabled: bool
    analysis_tier: str
    universe: UniverseConfig
    candidate: CandidateConfig
    score_weights: Mapping[str, Mapping[str, int]]
    quality_freshness_sec: Mapping[str, TtlGrace]
    quality_field_overrides_sec: Mapping[str, TtlGrace]
    liquidity: LiquidityConfig
    lifecycle: LifecycleConfig
    funding: FundingConfig
    evidence_cost: EvidenceCostConfig
    veto: VetoConfig
    refresh: RefreshConfig
    providers: Mapping[str, ProviderConfig]
    identity: IdentityConfig
    ingestion: IngestionConfig
    evidence: EvidenceConfig
    maintenance: MaintenanceConfig
    funding_capture: FundingCaptureConfig
    hedge: HedgeConfig
    optimization: OptimizationConfig

    def to_dict(self) -> dict[str, Any]:
        """Full config as plain dicts (still holds provider env var *names*)."""
        return {
            "enabled": self.enabled,
            "analysis_tier": self.analysis_tier,
            "universe": {
                "limit": self.universe.limit,
                "shortlist_size": self.universe.shortlist_size,
                "entry_depth_top": self.universe.entry_depth_top,
            },
            "candidate": {
                "watch_ltss": self.candidate.watch_ltss,
                "candidate_ltss": self.candidate.candidate_ltss,
                "ready_ltss": self.candidate.ready_ltss,
                "ready_entry": self.candidate.ready_entry,
                "ready_data_quality": self.candidate.ready_data_quality,
                "ready_tradeability_score": self.candidate.ready_tradeability_score,
                "entry_required_blocks": list(self.candidate.entry_required_blocks),
            },
            "score_weights": {
                profile: dict(weights) for profile, weights in self.score_weights.items()
            },
            "quality_freshness_sec": {
                group: {"ttl": window.ttl, "grace": window.grace}
                for group, window in self.quality_freshness_sec.items()
            },
            "quality_field_overrides_sec": {
                name: {"ttl": window.ttl, "grace": window.grace}
                for name, window in self.quality_field_overrides_sec.items()
            },
            "liquidity": {
                "hard_min_futures_volume_usd": self.liquidity.hard_min_futures_volume_usd,
                "hard_min_open_interest_usd": self.liquidity.hard_min_open_interest_usd,
                "preferred_futures_volume_usd": self.liquidity.preferred_futures_volume_usd,
                "preferred_open_interest_usd": self.liquidity.preferred_open_interest_usd,
            },
            "lifecycle": {
                "ideal_ath_drawdown_min": self.lifecycle.ideal_ath_drawdown_min,
                "ideal_ath_drawdown_max": self.lifecycle.ideal_ath_drawdown_max,
            },
            "funding": {
                "lookbacks_days": list(self.funding.lookbacks_days),
                "history_requests_per_5min": self.funding.history_requests_per_5min,
                "backfill_symbols_per_batch": self.funding.backfill_symbols_per_batch,
            },
            "evidence_cost": {
                "entry_fee": self.evidence_cost.entry_fee,
                "exit_fee": self.evidence_cost.exit_fee,
                "entry_slippage": self.evidence_cost.entry_slippage,
                "exit_slippage": self.evidence_cost.exit_slippage,
            },
            "veto": {
                "breakout_24h": self.veto.breakout_24h,
                "breakout_7d": self.veto.breakout_7d,
                "new_token_days": self.veto.new_token_days,
            },
            "refresh": {
                "fundamental_sec": self.refresh.fundamental_sec,
                "supply_sec": self.refresh.supply_sec,
                "score_sec": self.refresh.score_sec,
                "entry_sec": self.refresh.entry_sec,
                "grader_sec": self.refresh.grader_sec,
                "jitter_sec": self.refresh.jitter_sec,
                "entry_concurrency": self.refresh.entry_concurrency,
                "entry_max_upstream_calls_per_run": self.refresh.entry_max_upstream_calls_per_run,
                "entry_cache_ttl_sec": self.refresh.entry_cache_ttl_sec,
            },
            "providers": {
                name: {
                    "enabled": provider.enabled,
                    "api_key_env": provider.api_key_env,
                    "api_plan": provider.api_plan,
                }
                for name, provider in self.providers.items()
            },
            "identity": {
                "catalog_ttl_sec": self.identity.catalog_ttl_sec,
                "overrides_path_env": self.identity.overrides_path_env,
                "catalog_grace_sec": self.identity.catalog_grace_sec,
                "catalog_include_platform": self.identity.catalog_include_platform,
                "catalog_max_bytes": self.identity.catalog_max_bytes,
                "platform_detail_batch_size": self.identity.platform_detail_batch_size,
            },
            "ingestion": {
                "market_concurrency": self.ingestion.market_concurrency,
                "max_source_future_skew_sec": self.ingestion.max_source_future_skew_sec,
                "contract_refresh_sec": self.ingestion.contract_refresh_sec,
                "funding_backfill_sec": self.ingestion.funding_backfill_sec,
                "funding_repair_overlap_intervals": (
                    self.ingestion.funding_repair_overlap_intervals
                ),
                "monitor_reserved_fraction": self.ingestion.monitor_reserved_fraction,
                "scanner_reserved_fraction": self.ingestion.scanner_reserved_fraction,
                "db_queue_limit": self.ingestion.db_queue_limit,
                "db_persist_timeout_sec": self.ingestion.db_persist_timeout_sec,
                "db_priority_aging_sec": self.ingestion.db_priority_aging_sec,
            },
            "evidence": {
                "default_history_days": self.evidence.default_history_days,
                "grader_batch_size": self.evidence.grader_batch_size,
                "sample_policy": self.evidence.sample_policy,
            },
            "maintenance": {
                "retention_sec": self.maintenance.retention_sec,
                "snapshot_min_days": self.maintenance.snapshot_min_days,
                "batch_delete_limit": self.maintenance.batch_delete_limit,
            },
            "funding_capture": {
                "enabled": self.funding_capture.enabled,
                "fcs_version": self.funding_capture.fcs_version,
                "reference_notional_usd": self.funding_capture.reference_notional_usd,
                "reference_hold_days": self.funding_capture.reference_hold_days,
                "entry_gate": dict(self.funding_capture.entry_gate),
                "exit": dict(self.funding_capture.exit),
                "statistics": dict(self.funding_capture.statistics),
                "score_rules": {
                    k: (dict(v) if isinstance(v, Mapping) else v)
                    for k, v in self.funding_capture.score_rules.items()
                },
            },
            "optimization": {
                "schema_version": self.optimization.schema_version,
                "funding_schedule": _plain(self.optimization.funding_schedule),
                "decision": _plain(self.optimization.decision),
                "protection": _plain(self.optimization.protection),
                "evidence": _plain(self.optimization.evidence),
                "providers": _plain(self.optimization.providers),
            },
            "hedge": {
                "enabled": self.hedge.enabled,
                "ratio": dict(self.hedge.ratio),
                "execution": dict(self.hedge.execution),
                "liquidation": dict(self.hedge.liquidation),
                "basis": dict(self.hedge.basis),
                "liquidity_monitor": dict(self.hedge.liquidity_monitor),
                "freshness": {
                    group: {"ttl": window.ttl, "grace": window.grace}
                    for group, window in self.hedge.freshness.items()
                },
                "refresh": dict(self.hedge.refresh),
                "costs": dict(self.hedge.costs),
                "providers": {
                    name: dict(entry) for name, entry in self.hedge.providers.items()
                },
                "runtime": dict(self.hedge.runtime),
                "fx": dict(self.hedge.fx),
                "quality": {
                    k: (dict(v) if isinstance(v, Mapping) else v)
                    for k, v in self.hedge.quality.items()
                },
            },
        }

    def public_dict(self) -> dict[str, Any]:
        """Log/API-safe view: provider entries keep only the enabled flag.

        Hedge ``providers`` likewise expose only ``enabled``; on-chain
        ``base_url``/``api_key_env`` (key/URL/path) never enter logs, the
        API or any hash (B40/H01.1).
        """
        data = self.to_dict()
        data["providers"] = {
            name: {"enabled": provider.enabled} for name, provider in self.providers.items()
        }
        hedge_providers = data.get("hedge", {}).get("providers", {})
        data["hedge"]["providers"] = {
            name: {"enabled": bool(entry.get("enabled"))}
            for name, entry in hedge_providers.items()
        }
        return data


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


class _StrictLoader(yaml.SafeLoader):
    """YAML loader that rejects duplicate keys (R00 D03.1)."""


def _strict_construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if key in mapping:
            raise ShortLabConfigError(f"duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _strict_construct_mapping
)


def _yaml_load_strict(text: str) -> Any:
    try:
        return yaml.load(text, Loader=_StrictLoader)
    except ShortLabConfigError:
        raise
    except yaml.YAMLError as exc:
        raise ShortLabConfigError(f"invalid YAML: {exc}") from exc


def _read_yaml(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ShortLabConfigError(f"cannot read Short-Lab config {path}: {exc}") from exc
    try:
        return _yaml_load_strict(text)
    except yaml.YAMLError as exc:
        raise ShortLabConfigError(f"invalid YAML in Short-Lab config {path}: {exc}") from exc


def _read_default_doc() -> Any:
    """Read the packaged ``shortlab/default.yaml`` via resources.

    Frozen builds resolve the same file without a source checkout; domain
    validation below is unchanged.
    """
    from diveintocrypto_desktop.resources import read_resource_text

    try:
        text = read_resource_text("shortlab/default.yaml")
    except (OSError, FileNotFoundError) as exc:
        raise ShortLabConfigError(
            f"cannot read Short-Lab config {DEFAULT_CONFIG_PATH}: {exc}"
        ) from exc
    try:
        return _yaml_load_strict(text)
    except yaml.YAMLError as exc:
        raise ShortLabConfigError(
            f"invalid YAML in Short-Lab config {DEFAULT_CONFIG_PATH}: {exc}"
        ) from exc


def _section(doc: Any, *, where: str) -> Mapping[str, Any]:
    if not isinstance(doc, Mapping):
        raise ShortLabConfigError(f"{where}: top-level YAML must be a mapping")
    if "shortlab" in doc:
        unknown = sorted(k for k in doc if k != "shortlab")
        if unknown:
            raise ShortLabConfigError(f"{where}: unknown top-level keys: {unknown}")
        section = doc["shortlab"]
        if not isinstance(section, Mapping):
            raise ShortLabConfigError(f"{where}: 'shortlab' must map to a mapping")
        return section
    return doc


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any], trail: str) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        where = f"{trail}.{key}"
        if key not in merged:
            raise ShortLabConfigError(f"unknown Short-Lab config key: {where}")
        current = merged[key]
        if isinstance(current, dict):
            if not isinstance(value, Mapping):
                raise ShortLabConfigError(f"config key {where} must be a mapping")
            merged[key] = _deep_merge(current, value, where)
        else:
            if isinstance(value, Mapping):
                raise ShortLabConfigError(f"config key {where} must not be a mapping")
            merged[key] = value
    return merged


# ---------------------------------------------------------------------------
# Scalar coercion / validation helpers
# ---------------------------------------------------------------------------


def _as_bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ShortLabConfigError(f"{name} must be a boolean, got {value!r}")
    return value


def _as_int(name: str, value: Any, *, lo: int | None = None, hi: int | None = None) -> int:
    if isinstance(value, bool):
        raise ShortLabConfigError(f"{name} must be an integer, got {value!r}")
    if isinstance(value, int):
        result = value
    elif isinstance(value, float) and value.is_integer():
        result = int(value)
    else:
        raise ShortLabConfigError(f"{name} must be an integer, got {value!r}")
    if lo is not None and result < lo:
        raise ShortLabConfigError(f"{name} must be >= {lo}, got {result}")
    if hi is not None and result > hi:
        raise ShortLabConfigError(f"{name} must be <= {hi}, got {result}")
    return result


def _as_float(name: str, value: Any, *, lo: float | None = None, hi: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ShortLabConfigError(f"{name} must be a number, got {value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise ShortLabConfigError(f"{name} must be finite, got {value!r}")
    if lo is not None and result < lo:
        raise ShortLabConfigError(f"{name} must be >= {lo}, got {result}")
    if hi is not None and result > hi:
        raise ShortLabConfigError(f"{name} must be <= {hi}, got {result}")
    return result


def _as_number(name: str, value: Any, *, lo: float | None = None) -> int | float:
    """Number that keeps integral values as int (canonical-JSON stability)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ShortLabConfigError(f"{name} must be a number, got {value!r}")
    if not math.isfinite(value):
        raise ShortLabConfigError(f"{name} must be finite, got {value!r}")
    result: int | float = int(value) if float(value).is_integer() else float(value)
    if lo is not None and result < lo:
        raise ShortLabConfigError(f"{name} must be >= {lo}, got {result}")
    return result


def _as_str(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ShortLabConfigError(f"{name} must be a string, got {value!r}")
    return value


def _as_optional_str(name: str, value: Any) -> str | None:
    if value is None:
        return None
    return _as_str(name, value)


def _as_str_list(name: str, value: Any) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise ShortLabConfigError(f"{name} must be a list of strings, got {value!r}")
    items = tuple(value)
    for item in items:
        if not isinstance(item, str):
            raise ShortLabConfigError(f"{name} must be a list of strings, got {item!r}")
    return items


def _as_ttl_grace(name: str, value: Any) -> TtlGrace:
    if not isinstance(value, Mapping):
        raise ShortLabConfigError(f"{name} must be a mapping with ttl/grace, got {value!r}")
    unknown = sorted(k for k in value if k not in ("ttl", "grace"))
    if unknown:
        raise ShortLabConfigError(f"{name} has unknown keys: {unknown}")
    if "ttl" not in value or "grace" not in value:
        raise ShortLabConfigError(f"{name} requires both ttl and grace")
    ttl = _as_int(f"{name}.ttl", value["ttl"], lo=1)
    grace = _as_int(f"{name}.grace", value["grace"], lo=1)
    if not grace > ttl:
        raise ShortLabConfigError(f"{name} requires grace > ttl > 0, got ttl={ttl} grace={grace}")
    return TtlGrace(ttl=ttl, grace=grace)


# ---------------------------------------------------------------------------
# Section builders
# ---------------------------------------------------------------------------


def _build_universe(value: Any) -> UniverseConfig:
    if not isinstance(value, Mapping):
        raise ShortLabConfigError("universe must be a mapping")
    return UniverseConfig(
        limit=_as_int("universe.limit", value.get("limit"), lo=1),
        shortlist_size=_as_int("universe.shortlist_size", value.get("shortlist_size"), lo=1),
        entry_depth_top=_as_int("universe.entry_depth_top", value.get("entry_depth_top"), lo=1),
    )


def _build_candidate(value: Any) -> CandidateConfig:
    if not isinstance(value, Mapping):
        raise ShortLabConfigError("candidate must be a mapping")
    watch = _as_int("candidate.watch_ltss", value.get("watch_ltss"), lo=0, hi=100)
    candidate = _as_int("candidate.candidate_ltss", value.get("candidate_ltss"), lo=0, hi=100)
    ready = _as_int("candidate.ready_ltss", value.get("ready_ltss"), lo=0, hi=100)
    if not ready >= candidate >= watch:
        raise ShortLabConfigError(
            "candidate thresholds require ready_ltss >= candidate_ltss >= watch_ltss, "
            f"got ready={ready} candidate={candidate} watch={watch}"
        )
    blocks = _as_str_list("candidate.entry_required_blocks", value.get("entry_required_blocks"))
    if not blocks:
        raise ShortLabConfigError("candidate.entry_required_blocks must be non-empty")
    for block in blocks:
        if block not in KNOWN_ENTRY_BLOCKS:
            raise ShortLabConfigError(
                f"candidate.entry_required_blocks has unknown block {block!r}; "
                f"known blocks: {list(KNOWN_ENTRY_BLOCKS)}"
            )
    return CandidateConfig(
        watch_ltss=watch,
        candidate_ltss=candidate,
        ready_ltss=ready,
        ready_entry=_as_int("candidate.ready_entry", value.get("ready_entry"), lo=0, hi=100),
        ready_data_quality=_as_int(
            "candidate.ready_data_quality", value.get("ready_data_quality"), lo=0, hi=100
        ),
        ready_tradeability_score=_as_int(
            "candidate.ready_tradeability_score",
            value.get("ready_tradeability_score"),
            lo=0,
            hi=10,
        ),
        entry_required_blocks=blocks,
    )


def _build_score_weights(value: Any) -> Mapping[str, Mapping[str, int]]:
    if not isinstance(value, Mapping) or not value:
        raise ShortLabConfigError("score_weights must be a non-empty mapping")
    profiles: dict[str, Mapping[str, int]] = {}
    for profile, weights in value.items():
        where = f"score_weights.{profile}"
        if not isinstance(weights, Mapping) or not weights:
            raise ShortLabConfigError(f"{where} must be a non-empty mapping of module weights")
        clean: dict[str, int] = {}
        for module, weight in weights.items():
            if not isinstance(module, str) or not module:
                raise ShortLabConfigError(f"{where} has invalid module name {module!r}")
            clean[module] = _as_int(f"{where}.{module}", weight, lo=0, hi=100)
        total = sum(clean.values())
        if total != 100:
            raise ShortLabConfigError(f"{where} weights must sum to 100, got {total}")
        profiles[_as_str(f"{where} profile name", profile)] = MappingProxyType(clean)
    return MappingProxyType(profiles)


def _build_windows(name: str, value: Any) -> Mapping[str, TtlGrace]:
    if not isinstance(value, Mapping) or not value:
        raise ShortLabConfigError(f"{name} must be a non-empty mapping")
    return MappingProxyType(
        {str(group): _as_ttl_grace(f"{name}.{group}", window) for group, window in value.items()}
    )


def _build_liquidity(value: Any) -> LiquidityConfig:
    if not isinstance(value, Mapping):
        raise ShortLabConfigError("liquidity must be a mapping")
    return LiquidityConfig(
        hard_min_futures_volume_usd=_as_number(
            "liquidity.hard_min_futures_volume_usd",
            value.get("hard_min_futures_volume_usd"),
            lo=0,
        ),
        hard_min_open_interest_usd=_as_number(
            "liquidity.hard_min_open_interest_usd",
            value.get("hard_min_open_interest_usd"),
            lo=0,
        ),
        preferred_futures_volume_usd=_as_number(
            "liquidity.preferred_futures_volume_usd",
            value.get("preferred_futures_volume_usd"),
            lo=0,
        ),
        preferred_open_interest_usd=_as_number(
            "liquidity.preferred_open_interest_usd",
            value.get("preferred_open_interest_usd"),
            lo=0,
        ),
    )


def _build_lifecycle(value: Any) -> LifecycleConfig:
    if not isinstance(value, Mapping):
        raise ShortLabConfigError("lifecycle must be a mapping")
    lo = _as_float(
        "lifecycle.ideal_ath_drawdown_min", value.get("ideal_ath_drawdown_min"), lo=0, hi=1
    )
    hi = _as_float(
        "lifecycle.ideal_ath_drawdown_max", value.get("ideal_ath_drawdown_max"), lo=0, hi=1
    )
    if not hi >= lo:
        raise ShortLabConfigError(
            f"lifecycle requires ideal_ath_drawdown_max >= ideal_ath_drawdown_min, got {hi} < {lo}"
        )
    return LifecycleConfig(ideal_ath_drawdown_min=lo, ideal_ath_drawdown_max=hi)


def _build_funding(value: Any) -> FundingConfig:
    if not isinstance(value, Mapping):
        raise ShortLabConfigError("funding must be a mapping")
    raw_lookbacks = value.get("lookbacks_days")
    if isinstance(raw_lookbacks, str) or not isinstance(raw_lookbacks, (list, tuple)):
        raise ShortLabConfigError(f"funding.lookbacks_days must be a list, got {raw_lookbacks!r}")
    lookbacks = tuple(_as_int(f"funding.lookbacks_days[{i}]", day, lo=1) for i, day in enumerate(raw_lookbacks))
    if not lookbacks:
        raise ShortLabConfigError("funding.lookbacks_days must be non-empty")
    return FundingConfig(
        lookbacks_days=lookbacks,
        history_requests_per_5min=_as_int(
            "funding.history_requests_per_5min", value.get("history_requests_per_5min"), lo=1
        ),
        backfill_symbols_per_batch=_as_int(
            "funding.backfill_symbols_per_batch", value.get("backfill_symbols_per_batch"), lo=1
        ),
    )


def _build_evidence_cost(value: Any) -> EvidenceCostConfig:
    if not isinstance(value, Mapping):
        raise ShortLabConfigError("evidence_cost must be a mapping")
    return EvidenceCostConfig(
        entry_fee=_as_float("evidence_cost.entry_fee", value.get("entry_fee"), lo=0),
        exit_fee=_as_float("evidence_cost.exit_fee", value.get("exit_fee"), lo=0),
        entry_slippage=_as_float(
            "evidence_cost.entry_slippage", value.get("entry_slippage"), lo=0
        ),
        exit_slippage=_as_float("evidence_cost.exit_slippage", value.get("exit_slippage"), lo=0),
    )


def _build_veto(value: Any) -> VetoConfig:
    if not isinstance(value, Mapping):
        raise ShortLabConfigError("veto must be a mapping")
    return VetoConfig(
        breakout_24h=_as_float("veto.breakout_24h", value.get("breakout_24h"), lo=0),
        breakout_7d=_as_float("veto.breakout_7d", value.get("breakout_7d"), lo=0),
        new_token_days=_as_int("veto.new_token_days", value.get("new_token_days"), lo=0),
    )


def _build_refresh(value: Any) -> RefreshConfig:
    if not isinstance(value, Mapping):
        raise ShortLabConfigError("refresh must be a mapping")
    return RefreshConfig(
        fundamental_sec=_as_int("refresh.fundamental_sec", value.get("fundamental_sec"), lo=0),
        supply_sec=_as_int("refresh.supply_sec", value.get("supply_sec"), lo=0),
        score_sec=_as_int("refresh.score_sec", value.get("score_sec"), lo=0),
        entry_sec=_as_int("refresh.entry_sec", value.get("entry_sec"), lo=0),
        grader_sec=_as_int("refresh.grader_sec", value.get("grader_sec"), lo=0),
        jitter_sec=_as_int("refresh.jitter_sec", value.get("jitter_sec"), lo=0),
        entry_concurrency=_as_int(
            "refresh.entry_concurrency", value.get("entry_concurrency"), lo=1
        ),
        entry_max_upstream_calls_per_run=_as_int(
            "refresh.entry_max_upstream_calls_per_run",
            value.get("entry_max_upstream_calls_per_run"),
            lo=1,
        ),
        entry_cache_ttl_sec=_as_int(
            "refresh.entry_cache_ttl_sec", value.get("entry_cache_ttl_sec"), lo=0
        ),
    )


def _build_providers(value: Any) -> Mapping[str, ProviderConfig]:
    if not isinstance(value, Mapping) or not value:
        raise ShortLabConfigError("providers must be a non-empty mapping")
    providers: dict[str, ProviderConfig] = {}
    for name, entry in value.items():
        where = f"providers.{name}"
        if not isinstance(entry, Mapping):
            raise ShortLabConfigError(f"{where} must be a mapping")
        unknown = sorted(k for k in entry if k not in ("enabled", "api_key_env", "api_plan"))
        if unknown:
            raise ShortLabConfigError(f"{where} has unknown keys: {unknown}")
        raw_plan = entry.get("api_plan")
        if raw_plan is None:
            plan: str | None = None
        else:
            text = _as_str(f"{where}.api_plan", raw_plan).strip().upper()
            if text not in ("DEMO", "PRO"):
                raise ShortLabConfigError(
                    f"{where}.api_plan must be DEMO or PRO, got {raw_plan!r}"
                )
            plan = text
        providers[_as_str(f"{where} provider name", name)] = ProviderConfig(
            enabled=_as_bool(f"{where}.enabled", entry.get("enabled")),
            api_key_env=_as_optional_str(f"{where}.api_key_env", entry.get("api_key_env")),
            api_plan=plan,
        )
    return MappingProxyType(providers)


EVIDENCE_SAMPLE_POLICY = "first_eligible_per_symbol_profile_utc_day"


def _build_identity(value: Any) -> IdentityConfig:
    if not isinstance(value, Mapping):
        raise ShortLabConfigError("identity must be a mapping")
    ttl = _as_int("identity.catalog_ttl_sec", value.get("catalog_ttl_sec"), lo=1)
    grace = _as_int("identity.catalog_grace_sec", value.get("catalog_grace_sec"), lo=1)
    if not grace > ttl:
        raise ShortLabConfigError(
            f"identity requires catalog_grace_sec > catalog_ttl_sec > 0, "
            f"got ttl={ttl} grace={grace}"
        )
    path_env = _as_str(
        "identity.overrides_path_env", value.get("overrides_path_env")
    )
    if not path_env.strip():
        raise ShortLabConfigError("identity.overrides_path_env must be non-empty")
    return IdentityConfig(
        catalog_ttl_sec=ttl,
        overrides_path_env=path_env,
        catalog_grace_sec=grace,
        catalog_include_platform=_as_bool(
            "identity.catalog_include_platform",
            value.get("catalog_include_platform"),
        ),
        catalog_max_bytes=_as_int(
            "identity.catalog_max_bytes", value.get("catalog_max_bytes"), lo=1
        ),
        platform_detail_batch_size=_as_int(
            "identity.platform_detail_batch_size",
            value.get("platform_detail_batch_size"),
            lo=1,
        ),
    )


def _build_ingestion(value: Any) -> IngestionConfig:
    if not isinstance(value, Mapping):
        raise ShortLabConfigError("ingestion must be a mapping")
    monitor = _as_float(
        "ingestion.monitor_reserved_fraction",
        value.get("monitor_reserved_fraction"),
        lo=0,
        hi=1,
    )
    scanner = _as_float(
        "ingestion.scanner_reserved_fraction",
        value.get("scanner_reserved_fraction"),
        lo=0,
        hi=1,
    )
    if monitor + scanner > 1:
        raise ShortLabConfigError(
            "ingestion reserves must satisfy monitor + scanner <= 1, "
            f"got {monitor} + {scanner}"
        )
    return IngestionConfig(
        market_concurrency=_as_int(
            "ingestion.market_concurrency", value.get("market_concurrency"), lo=1
        ),
        max_source_future_skew_sec=_as_int(
            "ingestion.max_source_future_skew_sec",
            value.get("max_source_future_skew_sec"),
            lo=0,
        ),
        contract_refresh_sec=_as_int(
            "ingestion.contract_refresh_sec",
            value.get("contract_refresh_sec"),
            lo=0,
        ),
        funding_backfill_sec=_as_int(
            "ingestion.funding_backfill_sec",
            value.get("funding_backfill_sec"),
            lo=1,
        ),
        funding_repair_overlap_intervals=_as_int(
            "ingestion.funding_repair_overlap_intervals",
            value.get("funding_repair_overlap_intervals"),
            lo=0,
        ),
        monitor_reserved_fraction=monitor,
        scanner_reserved_fraction=scanner,
        db_queue_limit=_as_int(
            "ingestion.db_queue_limit", value.get("db_queue_limit"), lo=1
        ),
        db_persist_timeout_sec=_as_int(
            "ingestion.db_persist_timeout_sec",
            value.get("db_persist_timeout_sec"),
            lo=1,
        ),
        db_priority_aging_sec=_as_int(
            "ingestion.db_priority_aging_sec",
            value.get("db_priority_aging_sec"),
            lo=0,
        ),
    )


def _build_evidence(value: Any) -> EvidenceConfig:
    if not isinstance(value, Mapping):
        raise ShortLabConfigError("evidence must be a mapping")
    sample = _as_str("evidence.sample_policy", value.get("sample_policy"))
    if sample != EVIDENCE_SAMPLE_POLICY:
        raise ShortLabConfigError(
            f"evidence.sample_policy must be {EVIDENCE_SAMPLE_POLICY!r}, got {sample!r}"
        )
    return EvidenceConfig(
        default_history_days=_as_int(
            "evidence.default_history_days",
            value.get("default_history_days"),
            lo=1,
        ),
        grader_batch_size=_as_int(
            "evidence.grader_batch_size", value.get("grader_batch_size"), lo=1
        ),
        sample_policy=sample,
    )


def _build_maintenance(value: Any) -> MaintenanceConfig:
    if not isinstance(value, Mapping):
        raise ShortLabConfigError("maintenance must be a mapping")
    return MaintenanceConfig(
        retention_sec=_as_int(
            "maintenance.retention_sec", value.get("retention_sec"), lo=0
        ),
        snapshot_min_days=_as_int(
            "maintenance.snapshot_min_days",
            value.get("snapshot_min_days"),
            lo=1,
        ),
        batch_delete_limit=_as_int(
            "maintenance.batch_delete_limit",
            value.get("batch_delete_limit"),
            lo=1,
        ),
    )


def _require_mapping(name: str, value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ShortLabConfigError(f"{name} must be a mapping")
    return value


def _check_no_unknown(name: str, value: Mapping[str, Any], allowed: frozenset[str] | set[str]) -> None:
    unknown = sorted(k for k in value if k not in allowed)
    if unknown:
        raise ShortLabConfigError(f"{name} has unknown keys: {unknown}")


def _as_bin_pairs(name: str, value: Any, *, threshold_kind: str) -> list[list[Any]]:
    """Validate ``[[threshold, score], ...]`` bins preserving int-vs-float."""
    if not isinstance(value, (list, tuple)) or not value:
        raise ShortLabConfigError(f"{name} must be a non-empty list of [threshold, score]")
    out: list[list[Any]] = []
    for i, pair in enumerate(value):
        where = f"{name}[{i}]"
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise ShortLabConfigError(f"{where} must be a [threshold, score] pair")
        raw_thr, raw_score = pair[0], pair[1]
        if threshold_kind == "float":
            thr = _as_float(f"{where}[0]", raw_thr)
        elif threshold_kind == "int":
            thr = _as_int(f"{where}[0]", raw_thr)
        else:
            # number preserving original int-vs-float (used only where both
            # spellings are legal but defaults already fix the spelling).
            if isinstance(raw_thr, bool) or not isinstance(raw_thr, (int, float)):
                raise ShortLabConfigError(f"{where}[0] must be a number, got {raw_thr!r}")
            if not math.isfinite(float(raw_thr)):
                raise ShortLabConfigError(f"{where}[0] must be finite")
            thr = raw_thr
        score = _as_int(f"{where}[1]", raw_score)
        out.append([thr, score])
    return out


def _build_funding_capture(value: Any) -> FundingCaptureConfig:
    """Validate the B40 ``funding_capture`` subtree (H01, strict keys).

    R00 (D15): accepts ``fcs_v1``/``fcs_v2``; old ``fcs_v1`` input is
    normalized to the current ``fcs_v2`` for new calculations. Adds
    ``reference_hold_days`` (1..365, default filled via deep-merge).
    """
    _require_mapping("funding_capture", value)
    _check_no_unknown(
        "funding_capture", value,
        {"enabled", "fcs_version", "reference_notional_usd", "reference_hold_days",
         "entry_gate", "exit", "statistics", "score_rules"},
    )
    enabled = _as_bool("funding_capture.enabled", value.get("enabled"))
    fcs_version_raw = _as_str("funding_capture.fcs_version", value.get("fcs_version"))
    if fcs_version_raw not in ("fcs_v1", "fcs_v2"):
        raise ShortLabConfigError(
            "funding_capture.fcs_version must be 'fcs_v1' or 'fcs_v2', "
            f"got {fcs_version_raw!r}"
        )
    # Old user files stay loadable; current computation uses fcs_v2.
    fcs_version = "fcs_v2"
    reference_notional = _as_number(
        "funding_capture.reference_notional_usd",
        value.get("reference_notional_usd"), lo=0,
    )
    if not reference_notional > 0:
        raise ShortLabConfigError("funding_capture.reference_notional_usd must be > 0")
    reference_hold_days = _as_int(
        "funding_capture.reference_hold_days",
        value.get("reference_hold_days"), lo=1, hi=365,
    )
    # -- entry_gate (8 keys) --
    gate = _require_mapping("funding_capture.entry_gate", value.get("entry_gate"))
    _check_no_unknown(
        "funding_capture.entry_gate", gate,
        {"require_current_positive", "require_last_settled_positive",
         "min_funding_7d", "min_funding_30d", "min_positive_ratio_30d",
         "min_positive_ratio_90d", "min_30d_coverage", "min_90d_coverage"},
    )
    entry_gate = {
        "require_current_positive": _as_bool(
            "funding_capture.entry_gate.require_current_positive",
            gate.get("require_current_positive")),
        "require_last_settled_positive": _as_bool(
            "funding_capture.entry_gate.require_last_settled_positive",
            gate.get("require_last_settled_positive")),
        "min_funding_7d": _as_float(
            "funding_capture.entry_gate.min_funding_7d", gate.get("min_funding_7d")),
        "min_funding_30d": _as_float(
            "funding_capture.entry_gate.min_funding_30d", gate.get("min_funding_30d")),
        "min_positive_ratio_30d": _as_float(
            "funding_capture.entry_gate.min_positive_ratio_30d",
            gate.get("min_positive_ratio_30d"), lo=0, hi=1),
        "min_positive_ratio_90d": _as_float(
            "funding_capture.entry_gate.min_positive_ratio_90d",
            gate.get("min_positive_ratio_90d"), lo=0, hi=1),
        "min_30d_coverage": _as_float(
            "funding_capture.entry_gate.min_30d_coverage",
            gate.get("min_30d_coverage"), lo=0, hi=1),
        "min_90d_coverage": _as_float(
            "funding_capture.entry_gate.min_90d_coverage",
            gate.get("min_90d_coverage"), lo=0, hi=1),
    }
    # -- exit (2 keys, goes to hedge_policy_hash, not FCS) --
    exit_cfg = _require_mapping("funding_capture.exit", value.get("exit"))
    _check_no_unknown(
        "funding_capture.exit", exit_cfg,
        {"negative_settlement_streak", "weakening_7d_vs_30d_ratio"},
    )
    exit_map = {
        "negative_settlement_streak": _as_int(
            "funding_capture.exit.negative_settlement_streak",
            exit_cfg.get("negative_settlement_streak"), lo=0),
        "weakening_7d_vs_30d_ratio": _as_float(
            "funding_capture.exit.weakening_7d_vs_30d_ratio",
            exit_cfg.get("weakening_7d_vs_30d_ratio"), lo=0),
    }
    # -- statistics (7 keys) --
    stats = _require_mapping("funding_capture.statistics", value.get("statistics"))
    _check_no_unknown(
        "funding_capture.statistics", stats,
        {"std_reference_hours", "std_ddof", "rolling_window_days", "p25_method",
         "min_windows_30d", "min_windows_90d", "window_boundary"},
    )
    p25 = _as_str("funding_capture.statistics.p25_method", stats.get("p25_method"))
    if p25 != "LINEAR_N_MINUS_1":
        raise ShortLabConfigError(
            f"funding_capture.statistics.p25_method must be LINEAR_N_MINUS_1, got {p25!r}"
        )
    boundary = _as_str(
        "funding_capture.statistics.window_boundary", stats.get("window_boundary"))
    if boundary != "LEFT_OPEN_RIGHT_CLOSED":
        raise ShortLabConfigError(
            f"funding_capture.statistics.window_boundary must be LEFT_OPEN_RIGHT_CLOSED, got {boundary!r}"
        )
    statistics = {
        "std_reference_hours": _as_int(
            "funding_capture.statistics.std_reference_hours",
            stats.get("std_reference_hours"), lo=1),
        "std_ddof": _as_int(
            "funding_capture.statistics.std_ddof", stats.get("std_ddof"), lo=0),
        "rolling_window_days": _as_int(
            "funding_capture.statistics.rolling_window_days",
            stats.get("rolling_window_days"), lo=1),
        "p25_method": p25,
        "min_windows_30d": _as_int(
            "funding_capture.statistics.min_windows_30d",
            stats.get("min_windows_30d"), lo=1),
        "min_windows_90d": _as_int(
            "funding_capture.statistics.min_windows_90d",
            stats.get("min_windows_90d"), lo=1),
        "window_boundary": boundary,
    }
    # -- score_rules (frozen FCS bins) --
    rules = _require_mapping("funding_capture.score_rules", value.get("score_rules"))
    _check_no_unknown(
        "funding_capture.score_rules", rules,
        {"module_weights", "yield_30d", "positive_ratio_30d", "positive_ratio_90d",
         "std_30d", "negative_streak", "venue", "basis", "operational"},
    )
    mw = _require_mapping("funding_capture.score_rules.module_weights",
                           rules.get("module_weights"))
    _check_no_unknown(
        "funding_capture.score_rules.module_weights", mw,
        {"funding_yield", "funding_persistence", "funding_stability",
         "hedge_venue_quality", "basis_quality", "operational_safety"},
    )
    module_weights = {
        k: _as_int(f"funding_capture.score_rules.module_weights.{k}", v, lo=0)
        for k, v in mw.items()
    }
    if sum(module_weights.values()) != 100:
        raise ShortLabConfigError(
            "funding_capture.score_rules.module_weights must sum to 100, "
            f"got {sum(module_weights.values())}"
        )

    def _bins_block(block_name: str, block: Any, spec: dict[str, Any]) -> dict[str, Any]:
        _require_mapping(block_name, block)
        _check_no_unknown(block_name, block, set(spec))
        out: dict[str, Any] = {}
        for key, kind in spec.items():
            if key in ("positive_fallback", "non_positive", "fallback",
                       "not_applicable_score", "positive_above", "negative_floor",
                       "negative_at_or_above", "negative_below", "coverage_fallback",
                       "cost_fallback", "trading", "no_delisting", "identity",
                       "multiplier", "quote_fresh", "funding_time"):
                out[key] = _as_int(f"{block_name}.{key}", block.get(key))
            elif key in ("min_inclusive_bins", "max_inclusive_bins", "exact_bins",
                         "min_coverage_bins", "roundtrip_cost_max_bins",
                         "nonnegative_max_bins"):
                thr_kind = "int" if block_name.endswith("negative_streak") else "float"
                out[key] = _as_bin_pairs(f"{block_name}.{key}", block.get(key),
                                         threshold_kind=thr_kind)
            elif key == "exit_scores":
                scores = _require_mapping(f"{block_name}.exit_scores", block.get(key))
                _check_no_unknown(f"{block_name}.exit_scores", scores,
                                  {"CONFIRMED", "PARTIAL", "UNKNOWN", "NO"})
                out[key] = {
                    k: _as_int(f"{block_name}.exit_scores.{k}", v)
                    for k, v in scores.items()
                }
            else:
                raise ShortLabConfigError(f"{block_name} has unsupported key {key!r}")
        # required-key presence is enforced by _as_* (None raises); re-check
        # that every spec key was present.
        missing = [k for k in spec if k not in block]
        if missing:
            raise ShortLabConfigError(f"{block_name} is missing keys: {missing}")
        return out

    y30 = _require_mapping("funding_capture.score_rules.yield_30d", rules.get("yield_30d"))
    yield_30d = _bins_block(
        "funding_capture.score_rules.yield_30d", y30,
        {"positive_fallback": None, "min_inclusive_bins": None, "non_positive": None},
    )
    pr30 = _require_mapping("funding_capture.score_rules.positive_ratio_30d",
                             rules.get("positive_ratio_30d"))
    positive_ratio_30d = _bins_block(
        "funding_capture.score_rules.positive_ratio_30d", pr30,
        {"min_inclusive_bins": None, "fallback": None},
    )
    pr90 = _require_mapping("funding_capture.score_rules.positive_ratio_90d",
                             rules.get("positive_ratio_90d"))
    positive_ratio_90d = _bins_block(
        "funding_capture.score_rules.positive_ratio_90d", pr90,
        {"min_inclusive_bins": None, "fallback": None, "not_applicable_score": None},
    )
    std30 = _require_mapping("funding_capture.score_rules.std_30d", rules.get("std_30d"))
    std_30d = _bins_block(
        "funding_capture.score_rules.std_30d", std30,
        {"max_inclusive_bins": None, "fallback": None},
    )
    neg = _require_mapping("funding_capture.score_rules.negative_streak",
                            rules.get("negative_streak"))
    negative_streak = _bins_block(
        "funding_capture.score_rules.negative_streak", neg,
        {"exact_bins": None, "fallback": None},
    )
    venue = _require_mapping("funding_capture.score_rules.venue", rules.get("venue"))
    _check_no_unknown(
        "funding_capture.score_rules.venue", venue,
        {"min_coverage_bins", "coverage_fallback", "roundtrip_cost_max_bins",
         "cost_fallback", "exit_scores"},
    )
    venue_block = {
        "min_coverage_bins": _as_bin_pairs(
            "funding_capture.score_rules.venue.min_coverage_bins",
            venue.get("min_coverage_bins"), threshold_kind="float"),
        "coverage_fallback": _as_int(
            "funding_capture.score_rules.venue.coverage_fallback",
            venue.get("coverage_fallback")),
        "roundtrip_cost_max_bins": _as_bin_pairs(
            "funding_capture.score_rules.venue.roundtrip_cost_max_bins",
            venue.get("roundtrip_cost_max_bins"), threshold_kind="float"),
        "cost_fallback": _as_int(
            "funding_capture.score_rules.venue.cost_fallback",
            venue.get("cost_fallback")),
        "exit_scores": _bins_block(
            "funding_capture.score_rules.venue.exit_scores_helper",
            {"exit_scores": venue.get("exit_scores")},
            {"exit_scores": None},
        )["exit_scores"] if False else None,
    }
    # exit_scores validated with its own key set (CONFIRMED/PARTIAL/...).
    _exit_scores = _require_mapping("funding_capture.score_rules.venue.exit_scores",
                                    venue.get("exit_scores"))
    _check_no_unknown("funding_capture.score_rules.venue.exit_scores", _exit_scores,
                      {"CONFIRMED", "PARTIAL", "UNKNOWN", "NO"})
    venue_block["exit_scores"] = {
        k: _as_int(f"funding_capture.score_rules.venue.exit_scores.{k}", v)
        for k, v in _exit_scores.items()
    }
    for _k in ("min_coverage_bins", "coverage_fallback", "roundtrip_cost_max_bins",
               "cost_fallback", "exit_scores"):
        if _k not in venue:
            raise ShortLabConfigError(
                f"funding_capture.score_rules.venue is missing keys: {[_k]}")
    basis = _require_mapping("funding_capture.score_rules.basis", rules.get("basis"))
    _check_no_unknown(
        "funding_capture.score_rules.basis", basis,
        {"nonnegative_max_bins", "positive_above", "negative_floor",
         "negative_at_or_above", "negative_below"},
    )
    basis_block = {
        "nonnegative_max_bins": _as_bin_pairs(
            "funding_capture.score_rules.basis.nonnegative_max_bins",
            basis.get("nonnegative_max_bins"), threshold_kind="float"),
        "positive_above": _as_int(
            "funding_capture.score_rules.basis.positive_above",
            basis.get("positive_above")),
        "negative_floor": _as_float(
            "funding_capture.score_rules.basis.negative_floor",
            basis.get("negative_floor")),
        "negative_at_or_above": _as_int(
            "funding_capture.score_rules.basis.negative_at_or_above",
            basis.get("negative_at_or_above")),
        "negative_below": _as_int(
            "funding_capture.score_rules.basis.negative_below",
            basis.get("negative_below")),
    }
    operational = _require_mapping("funding_capture.score_rules.operational",
                                    rules.get("operational"))
    _check_no_unknown(
        "funding_capture.score_rules.operational", operational,
        {"trading", "no_delisting", "identity", "multiplier", "quote_fresh",
         "funding_time"},
    )
    operational_block = {
        k: _as_int(f"funding_capture.score_rules.operational.{k}", v)
        for k, v in operational.items()
    }
    score_rules = {
        "module_weights": module_weights,
        "yield_30d": yield_30d,
        "positive_ratio_30d": positive_ratio_30d,
        "positive_ratio_90d": positive_ratio_90d,
        "std_30d": std_30d,
        "negative_streak": negative_streak,
        "venue": venue_block,
        "basis": basis_block,
        "operational": operational_block,
    }
    from types import MappingProxyType as _MPT
    return FundingCaptureConfig(
        enabled=enabled,
        fcs_version=fcs_version,
        reference_notional_usd=reference_notional,
        reference_hold_days=reference_hold_days,
        entry_gate=_MPT(entry_gate),
        exit=_MPT(exit_map),
        statistics=_MPT(statistics),
        score_rules=_MPT(score_rules),
    )


def _as_decimal_str(name: str, value: Any) -> str:
    """Decimal string that stays fixed-point (no exponent, no float)."""
    from decimal import Decimal, InvalidOperation

    if not isinstance(value, str) or not value.strip():
        raise ShortLabConfigError(f"{name} must be a decimal string, got {value!r}")
    text = value.strip()
    try:
        parsed = Decimal(text)
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise ShortLabConfigError(f"{name} is not a decimal string: {value!r}") from exc
    if not parsed.is_finite():
        raise ShortLabConfigError(f"{name} must be finite, got {value!r}")
    if isinstance(value, bool):
        raise ShortLabConfigError(f"{name} must be a decimal string, got {value!r}")
    return text


def _build_optimization(value: Any) -> OptimizationConfig:
    """Validate the R00 ``optimization`` subtree (D15, strict keys).

    Unknown keys, ratio out of range, illegal horizons/capital rejected.
    """
    from decimal import Decimal
    from types import MappingProxyType as _MPT

    _require_mapping("optimization", value)
    _check_no_unknown(
        "optimization", value,
        {"schema_version", "funding_schedule", "decision", "protection",
         "evidence", "providers"},
    )
    schema_version = _as_str("optimization.schema_version", value.get("schema_version"))
    if schema_version != "repair-contract-v1":
        raise ShortLabConfigError(
            "optimization.schema_version must be 'repair-contract-v1', "
            f"got {schema_version!r}"
        )
    # -- funding_schedule --
    fs = _require_mapping("optimization.funding_schedule", value.get("funding_schedule"))
    _check_no_unknown(
        "optimization.funding_schedule", fs, {"event_match_tolerance_sec"}
    )
    funding_schedule = {
        "event_match_tolerance_sec": _as_int(
            "optimization.funding_schedule.event_match_tolerance_sec",
            fs.get("event_match_tolerance_sec"), lo=1,
        ),
    }
    # -- decision --
    dec = _require_mapping("optimization.decision", value.get("decision"))
    _check_no_unknown(
        "optimization.decision", dec,
        {"enabled", "version", "validation_level", "ratios", "ratio_tolerance",
         "min_net_carry_usd", "quote_valid_sec", "exit_stress_bps",
         "capital_reserve_fraction", "scenarios"},
    )
    enabled = _as_bool("optimization.decision.enabled", dec.get("enabled"))
    version = _as_str("optimization.decision.version", dec.get("version"))
    if version != "hedge-decision-v1":
        raise ShortLabConfigError(
            "optimization.decision.version must be 'hedge-decision-v1', "
            f"got {version!r}"
        )
    validation_level = _as_str(
        "optimization.decision.validation_level", dec.get("validation_level")
    )
    if validation_level != "RULE_BASED_UNVALIDATED":
        raise ShortLabConfigError(
            "optimization.decision.validation_level must be "
            f"'RULE_BASED_UNVALIDATED', got {validation_level!r}"
        )
    raw_ratios = dec.get("ratios")
    if isinstance(raw_ratios, str) or not isinstance(raw_ratios, (list, tuple)):
        raise ShortLabConfigError("optimization.decision.ratios must be a list")
    ratios: list[str] = []
    for i, item in enumerate(raw_ratios):
        text = _as_decimal_str(f"optimization.decision.ratios[{i}]", item)
        try:
            parsed = Decimal(text)
        except Exception as exc:
            raise ShortLabConfigError(
                f"optimization.decision.ratios[{i}] is not decimal: {item!r}"
            ) from exc
        if not (Decimal("0") <= parsed <= Decimal("1")):
            raise ShortLabConfigError(
                f"optimization.decision.ratios[{i}] must be in 0..1, got {item!r}"
            )
        ratios.append(text)
    if not ratios or "1" not in {str(Decimal(r).normalize()) if "e" not in str(Decimal(r)).lower() else r for r in ratios}:
        # Require full-hedge ratio present (canonical '1' variants allowed).
        normalized = set()
        for r in ratios:
            try:
                d = Decimal(r)
                # Canonical: strip trailing zeros for comparison.
                s = format(d.normalize(), "f") if d != 0 else "0"
                normalized.add(s)
            except Exception:
                pass
        if "1" not in normalized:
            raise ShortLabConfigError(
                "optimization.decision.ratios must include '1' (full hedge)"
            )
    ratio_tolerance = _as_decimal_str(
        "optimization.decision.ratio_tolerance", dec.get("ratio_tolerance")
    )
    try:
        tol = Decimal(ratio_tolerance)
    except Exception as exc:
        raise ShortLabConfigError("optimization.decision.ratio_tolerance invalid") from exc
    if not (Decimal("0") <= tol <= Decimal("1")):
        raise ShortLabConfigError(
            "optimization.decision.ratio_tolerance must be in 0..1"
        )
    min_net_carry = _as_decimal_str(
        "optimization.decision.min_net_carry_usd", dec.get("min_net_carry_usd")
    )
    try:
        if Decimal(min_net_carry) < 0:
            raise ShortLabConfigError(
                "optimization.decision.min_net_carry_usd must be >= 0"
            )
    except ShortLabConfigError:
        raise
    except Exception as exc:
        raise ShortLabConfigError("optimization.decision.min_net_carry_usd invalid") from exc
    quote_valid_sec = _as_int(
        "optimization.decision.quote_valid_sec", dec.get("quote_valid_sec"), lo=1
    )
    exit_stress_bps = _as_int(
        "optimization.decision.exit_stress_bps", dec.get("exit_stress_bps"), lo=0
    )
    capital_reserve = _as_decimal_str(
        "optimization.decision.capital_reserve_fraction",
        dec.get("capital_reserve_fraction"),
    )
    try:
        cr = Decimal(capital_reserve)
    except Exception as exc:
        raise ShortLabConfigError(
            "optimization.decision.capital_reserve_fraction invalid"
        ) from exc
    if not (Decimal("0") <= cr <= Decimal("1")):
        raise ShortLabConfigError(
            "optimization.decision.capital_reserve_fraction must be in 0..1"
        )
    scenarios_raw = _require_mapping(
        "optimization.decision.scenarios", dec.get("scenarios")
    )
    expected_scenarios = {"UP_50", "UP_100", "DOWN_50", "BASIS_UP", "BASIS_DOWN", "FX_DOWN"}
    if set(scenarios_raw) != expected_scenarios:
        raise ShortLabConfigError(
            "optimization.decision.scenarios must hold exactly "
            f"{sorted(expected_scenarios)}, got {sorted(scenarios_raw)}"
        )
    scenarios: dict[str, Any] = {}
    for name, leg in scenarios_raw.items():
        block = _require_mapping(
            f"optimization.decision.scenarios.{name}", leg
        )
        _check_no_unknown(
            f"optimization.decision.scenarios.{name}", block,
            {"futures_move", "spot_move", "fx_shock"},
        )
        scenarios[name] = {
            "futures_move": _as_decimal_str(
                f"optimization.decision.scenarios.{name}.futures_move",
                block.get("futures_move"),
            ),
            "spot_move": _as_decimal_str(
                f"optimization.decision.scenarios.{name}.spot_move",
                block.get("spot_move"),
            ),
            "fx_shock": _as_decimal_str(
                f"optimization.decision.scenarios.{name}.fx_shock",
                block.get("fx_shock"),
            ),
        }
    decision = {
        "enabled": enabled,
        "version": version,
        "validation_level": validation_level,
        "ratios": ratios,
        "ratio_tolerance": ratio_tolerance,
        "min_net_carry_usd": min_net_carry,
        "quote_valid_sec": quote_valid_sec,
        "exit_stress_bps": exit_stress_bps,
        "capital_reserve_fraction": capital_reserve,
        "scenarios": scenarios,
    }
    # -- protection --
    prot = _require_mapping("optimization.protection", value.get("protection"))
    _check_no_unknown("optimization.protection", prot, {"confirmation_ttl_sec"})
    protection = {
        "confirmation_ttl_sec": _as_int(
            "optimization.protection.confirmation_ttl_sec",
            prot.get("confirmation_ttl_sec"), lo=1,
        ),
    }
    # -- evidence --
    ev = _require_mapping("optimization.evidence", value.get("evidence"))
    _check_no_unknown(
        "optimization.evidence", ev,
        {"version", "horizons_days", "quote_group_skew_sec", "exit_delay_sec",
         "quote_task_batch", "retention_days", "bootstrap_samples",
         "bootstrap_seed", "min_assets", "min_comparable_samples"},
    )
    ev_version = _as_str("optimization.evidence.version", ev.get("version"))
    if ev_version != "hedge_evidence_v3":
        raise ShortLabConfigError(
            "optimization.evidence.version must be 'hedge_evidence_v3', "
            f"got {ev_version!r}"
        )
    raw_horizons = ev.get("horizons_days")
    if not isinstance(raw_horizons, (list, tuple)) or list(raw_horizons) != [7, 30, 90]:
        raise ShortLabConfigError(
            "optimization.evidence.horizons_days must be [7, 30, 90], "
            f"got {raw_horizons!r}"
        )
    evidence = {
        "version": ev_version,
        "horizons_days": [7, 30, 90],
        "quote_group_skew_sec": _as_int(
            "optimization.evidence.quote_group_skew_sec",
            ev.get("quote_group_skew_sec"), lo=1,
        ),
        "exit_delay_sec": _as_int(
            "optimization.evidence.exit_delay_sec", ev.get("exit_delay_sec"), lo=0
        ),
        "quote_task_batch": _as_int(
            "optimization.evidence.quote_task_batch",
            ev.get("quote_task_batch"), lo=1,
        ),
        "retention_days": _as_int(
            "optimization.evidence.retention_days", ev.get("retention_days"), lo=1
        ),
        "bootstrap_samples": _as_int(
            "optimization.evidence.bootstrap_samples",
            ev.get("bootstrap_samples"), lo=1,
        ),
        "bootstrap_seed": _as_int(
            "optimization.evidence.bootstrap_seed", ev.get("bootstrap_seed")
        ),
        "min_assets": _as_int(
            "optimization.evidence.min_assets", ev.get("min_assets"), lo=1
        ),
        "min_comparable_samples": _as_int(
            "optimization.evidence.min_comparable_samples",
            ev.get("min_comparable_samples"), lo=1,
        ),
    }
    if evidence["bootstrap_seed"] != 20261008:
        raise ShortLabConfigError(
            "optimization.evidence.bootstrap_seed must be 20261008"
        )
    # -- providers.coingecko --
    prov = _require_mapping("optimization.providers", value.get("providers"))
    _check_no_unknown("optimization.providers", prov, {"coingecko"})
    cg = _require_mapping("optimization.providers.coingecko", prov.get("coingecko"))
    _check_no_unknown(
        "optimization.providers.coingecko", cg,
        {"account_monthly_limit", "local_requests_per_minute", "reserve_fraction"},
    )
    cg_limit = _as_int(
        "optimization.providers.coingecko.account_monthly_limit",
        cg.get("account_monthly_limit"), lo=1,
    )
    cg_rpm = _as_int(
        "optimization.providers.coingecko.local_requests_per_minute",
        cg.get("local_requests_per_minute"), lo=1,
    )
    cg_reserve = _as_decimal_str(
        "optimization.providers.coingecko.reserve_fraction",
        cg.get("reserve_fraction"),
    )
    try:
        rf = Decimal(cg_reserve)
    except Exception as exc:
        raise ShortLabConfigError(
            "optimization.providers.coingecko.reserve_fraction invalid"
        ) from exc
    if not (Decimal("0") <= rf < Decimal("1")):
        raise ShortLabConfigError(
            "optimization.providers.coingecko.reserve_fraction must be in 0..1 (exclusive 1)"
        )
    providers = {
        "coingecko": {
            "account_monthly_limit": cg_limit,
            "local_requests_per_minute": cg_rpm,
            "reserve_fraction": cg_reserve,
        },
    }
    return OptimizationConfig(
        schema_version=schema_version,
        funding_schedule=_MPT(funding_schedule),
        decision=_MPT({k: (_MPT(v) if isinstance(v, dict) else v) for k, v in decision.items()}),
        protection=_MPT(protection),
        evidence=_MPT(evidence),
        providers=_MPT(providers),
    )


def _build_hedge(value: Any) -> HedgeConfig:
    """Validate the B40 ``hedge`` subtree (H01, strict keys)."""
    from types import MappingProxyType as _MPT
    _require_mapping("hedge", value)
    _check_no_unknown(
        "hedge", value,
        {"enabled", "ratio", "execution", "liquidation", "basis",
         "liquidity_monitor", "freshness", "refresh", "costs", "providers",
         "runtime", "fx", "quality"},
    )
    enabled = _as_bool("hedge.enabled", value.get("enabled"))
    ratio = _require_mapping("hedge.ratio", value.get("ratio"))
    _check_no_unknown("hedge.ratio", ratio, {"drift_warn_pct", "drift_critical_pct"})
    ratio_map = {
        "drift_warn_pct": _as_float("hedge.ratio.drift_warn_pct",
                                    ratio.get("drift_warn_pct"), lo=0, hi=1),
        "drift_critical_pct": _as_float("hedge.ratio.drift_critical_pct",
                                        ratio.get("drift_critical_pct"), lo=0, hi=1),
    }
    if not ratio_map["drift_critical_pct"] >= ratio_map["drift_warn_pct"]:
        raise ShortLabConfigError(
            "hedge.ratio requires drift_critical_pct >= drift_warn_pct")
    execution = _require_mapping("hedge.execution", value.get("execution"))
    _check_no_unknown("hedge.execution", execution,
                      {"max_price_impact_bps", "max_manual_chunk_usd"})
    execution_map = {
        "max_price_impact_bps": _as_int("hedge.execution.max_price_impact_bps",
                                        execution.get("max_price_impact_bps"), lo=0),
        "max_manual_chunk_usd": _as_int("hedge.execution.max_manual_chunk_usd",
                                        execution.get("max_manual_chunk_usd"), lo=0),
    }
    liquidation = _require_mapping("hedge.liquidation", value.get("liquidation"))
    _check_no_unknown(
        "hedge.liquidation", liquidation,
        {"warning_distance", "critical_distance", "emergency_distance",
         "user_price_max_age_sec"},
    )
    liquidation_map = {
        "warning_distance": _as_float("hedge.liquidation.warning_distance",
                                      liquidation.get("warning_distance"), lo=0, hi=1),
        "critical_distance": _as_float("hedge.liquidation.critical_distance",
                                       liquidation.get("critical_distance"), lo=0, hi=1),
        "emergency_distance": _as_float("hedge.liquidation.emergency_distance",
                                        liquidation.get("emergency_distance"), lo=0, hi=1),
        "user_price_max_age_sec": _as_int("hedge.liquidation.user_price_max_age_sec",
                                          liquidation.get("user_price_max_age_sec"), lo=1),
    }
    if not (liquidation_map["warning_distance"] >= liquidation_map["critical_distance"]
            >= liquidation_map["emergency_distance"]):
        raise ShortLabConfigError(
            "hedge.liquidation requires warning >= critical >= emergency")
    basis = _require_mapping("hedge.basis", value.get("basis"))
    _check_no_unknown(
        "hedge.basis", basis,
        {"warning_vs_accrued_funding", "exit_vs_accrued_funding", "warning_min_usd",
         "warning_min_notional_ratio", "exit_min_usd", "exit_min_notional_ratio",
         "fresh_max_skew_sec", "reference_max_skew_sec"},
    )
    basis_map = {
        "warning_vs_accrued_funding": _as_float("hedge.basis.warning_vs_accrued_funding",
                                               basis.get("warning_vs_accrued_funding"), lo=0),
        "exit_vs_accrued_funding": _as_float("hedge.basis.exit_vs_accrued_funding",
                                             basis.get("exit_vs_accrued_funding"), lo=0),
        "warning_min_usd": _as_int("hedge.basis.warning_min_usd",
                                   basis.get("warning_min_usd"), lo=0),
        "warning_min_notional_ratio": _as_float("hedge.basis.warning_min_notional_ratio",
                                                basis.get("warning_min_notional_ratio"), lo=0),
        "exit_min_usd": _as_int("hedge.basis.exit_min_usd",
                                basis.get("exit_min_usd"), lo=0),
        "exit_min_notional_ratio": _as_float("hedge.basis.exit_min_notional_ratio",
                                             basis.get("exit_min_notional_ratio"), lo=0),
        "fresh_max_skew_sec": _as_int("hedge.basis.fresh_max_skew_sec",
                                      basis.get("fresh_max_skew_sec"), lo=0),
        "reference_max_skew_sec": _as_int("hedge.basis.reference_max_skew_sec",
                                          basis.get("reference_max_skew_sec"), lo=0),
    }
    liq = _require_mapping("hedge.liquidity_monitor", value.get("liquidity_monitor"))
    _check_no_unknown("hedge.liquidity_monitor", liq,
                      {"max_exit_slippage_bps", "min_exit_coverage_ratio"})
    liq_map = {
        "max_exit_slippage_bps": _as_int("hedge.liquidity_monitor.max_exit_slippage_bps",
                                         liq.get("max_exit_slippage_bps"), lo=0),
        "min_exit_coverage_ratio": _as_float("hedge.liquidity_monitor.min_exit_coverage_ratio",
                                             liq.get("min_exit_coverage_ratio"), lo=0),
    }
    freshness = _require_mapping("hedge.freshness", value.get("freshness"))
    _check_no_unknown(
        "hedge.freshness", freshness,
        {"futures_mark", "spot_quote", "spot_depth", "alpha_quote",
         "funding_current", "onchain_quote", "contract_status"},
    )
    freshness_map = {
        str(group): _as_ttl_grace(f"hedge.freshness.{group}", window)
        for group, window in freshness.items()
    }
    refresh = _require_mapping("hedge.refresh", value.get("refresh"))
    _check_no_unknown(
        "hedge.refresh", refresh,
        {"opportunity_sec", "active_mark_sec", "active_basis_sec",
         "active_funding_sec", "active_depth_sec", "active_onchain_quote_sec",
         "active_alpha_quote_sec", "contract_status_sec",
         "funding_settlement_check_sec", "monitor_persist_sec"},
    )
    refresh_map = {
        k: _as_int(f"hedge.refresh.{k}", v, lo=0) for k, v in refresh.items()
    }
    costs = _require_mapping("hedge.costs", value.get("costs"))
    _check_no_unknown(
        "hedge.costs", costs,
        {"futures_entry_fee_rate", "futures_exit_fee_rate", "spot_entry_fee_rate",
         "spot_exit_fee_rate", "alpha_entry_fee_rate", "alpha_exit_fee_rate",
         "onchain_extra_buffer_bps"},
    )
    costs_map = {
        "futures_entry_fee_rate": _as_float("hedge.costs.futures_entry_fee_rate",
                                            costs.get("futures_entry_fee_rate"), lo=0, hi=1),
        "futures_exit_fee_rate": _as_float("hedge.costs.futures_exit_fee_rate",
                                           costs.get("futures_exit_fee_rate"), lo=0, hi=1),
        "spot_entry_fee_rate": _as_float("hedge.costs.spot_entry_fee_rate",
                                         costs.get("spot_entry_fee_rate"), lo=0, hi=1),
        "spot_exit_fee_rate": _as_float("hedge.costs.spot_exit_fee_rate",
                                        costs.get("spot_exit_fee_rate"), lo=0, hi=1),
        "alpha_entry_fee_rate": _as_float("hedge.costs.alpha_entry_fee_rate",
                                          costs.get("alpha_entry_fee_rate"), lo=0, hi=1),
        "alpha_exit_fee_rate": _as_float("hedge.costs.alpha_exit_fee_rate",
                                         costs.get("alpha_exit_fee_rate"), lo=0, hi=1),
        "onchain_extra_buffer_bps": _as_int("hedge.costs.onchain_extra_buffer_bps",
                                            costs.get("onchain_extra_buffer_bps"), lo=0),
    }
    providers = _require_mapping("hedge.providers", value.get("providers"))
    _check_no_unknown("hedge.providers", providers,
                      {"binance_spot", "binance_alpha", "onchain"})
    spot = _require_mapping("hedge.providers.binance_spot", providers.get("binance_spot"))
    _check_no_unknown("hedge.providers.binance_spot", spot, {"enabled"})
    alpha = _require_mapping("hedge.providers.binance_alpha", providers.get("binance_alpha"))
    _check_no_unknown("hedge.providers.binance_alpha", alpha, {"enabled"})
    onchain = _require_mapping("hedge.providers.onchain", providers.get("onchain"))
    _check_no_unknown(
        "hedge.providers.onchain", onchain,
        {"enabled", "provider", "chain_id", "base_url", "api_key_env",
         "api_version", "quote_kind", "quote_asset", "quote_ttl_sec",
         "requests_per_sec", "concurrency"},
    )
    onchain_enabled = _as_bool("hedge.providers.onchain.enabled", onchain.get("enabled"))
    onchain_provider = _as_str("hedge.providers.onchain.provider", onchain.get("provider"))
    if onchain_provider != "ETHEREUM_0X_PRICE_V2":
        raise ShortLabConfigError(
            "hedge.providers.onchain.provider must be ETHEREUM_0X_PRICE_V2, "
            f"got {onchain_provider!r}")
    onchain_chain = _as_int("hedge.providers.onchain.chain_id", onchain.get("chain_id"))
    if onchain_chain != 1:
        raise ShortLabConfigError(
            f"hedge.providers.onchain.chain_id must be 1, got {onchain_chain}")
    onchain_base = _as_str("hedge.providers.onchain.base_url", onchain.get("base_url"))
    if onchain_base != "https://api.0x.org":
        raise ShortLabConfigError(
            f"hedge.providers.onchain.base_url must be https://api.0x.org, got {onchain_base!r}")
    onchain_env = _as_str("hedge.providers.onchain.api_key_env", onchain.get("api_key_env"))
    if onchain_env != "SHORTLAB_0X_API_KEY":
        raise ShortLabConfigError(
            "hedge.providers.onchain.api_key_env must be SHORTLAB_0X_API_KEY, "
            f"got {onchain_env!r}")
    onchain_version = _as_str("hedge.providers.onchain.api_version", onchain.get("api_version"))
    if onchain_version != "v2":
        raise ShortLabConfigError(
            f"hedge.providers.onchain.api_version must be v2, got {onchain_version!r}")
    onchain_kind = _as_str("hedge.providers.onchain.quote_kind", onchain.get("quote_kind"))
    if onchain_kind != "INDICATIVE":
        raise ShortLabConfigError(
            f"hedge.providers.onchain.quote_kind must be INDICATIVE, got {onchain_kind!r}")
    onchain_asset = _as_str("hedge.providers.onchain.quote_asset", onchain.get("quote_asset"))
    if onchain_asset != "USDC":
        raise ShortLabConfigError(
            f"hedge.providers.onchain.quote_asset must be USDC, got {onchain_asset!r}")
    providers_map = {
        "binance_spot": {"enabled": _as_bool(
            "hedge.providers.binance_spot.enabled", spot.get("enabled"))},
        "binance_alpha": {"enabled": _as_bool(
            "hedge.providers.binance_alpha.enabled", alpha.get("enabled"))},
        "onchain": {
            "enabled": onchain_enabled,
            "provider": onchain_provider,
            "chain_id": onchain_chain,
            "base_url": onchain_base,
            "api_key_env": onchain_env,
            "api_version": onchain_version,
            "quote_kind": onchain_kind,
            "quote_asset": onchain_asset,
            "quote_ttl_sec": _as_int("hedge.providers.onchain.quote_ttl_sec",
                                     onchain.get("quote_ttl_sec"), lo=1),
            "requests_per_sec": _as_int("hedge.providers.onchain.requests_per_sec",
                                        onchain.get("requests_per_sec"), lo=1),
            "concurrency": _as_int("hedge.providers.onchain.concurrency",
                                   onchain.get("concurrency"), lo=1),
        },
    }
    runtime = _require_mapping("hedge.runtime", value.get("runtime"))
    _check_no_unknown(
        "hedge.runtime", runtime,
        {"max_active_symbols", "db_queue_limit", "db_persist_timeout_sec",
         "monitor_priority", "background_priority", "retention_priority"},
    )
    runtime_map = {
        "max_active_symbols": _as_int("hedge.runtime.max_active_symbols",
                                      runtime.get("max_active_symbols"), lo=1),
        "db_queue_limit": _as_int("hedge.runtime.db_queue_limit",
                                   runtime.get("db_queue_limit"), lo=1),
        "db_persist_timeout_sec": _as_int("hedge.runtime.db_persist_timeout_sec",
                                          runtime.get("db_persist_timeout_sec"), lo=1),
        "monitor_priority": _as_int("hedge.runtime.monitor_priority",
                                    runtime.get("monitor_priority"), lo=0),
        "background_priority": _as_int("hedge.runtime.background_priority",
                                       runtime.get("background_priority"), lo=0),
        "retention_priority": _as_int("hedge.runtime.retention_priority",
                                      runtime.get("retention_priority"), lo=0),
    }
    fx = _require_mapping("hedge.fx", value.get("fx"))
    _check_no_unknown("hedge.fx", fx, {"policy", "max_age_sec", "historical_policy"})
    fx_policy = _as_str("hedge.fx.policy", fx.get("policy"))
    if fx_policy != "OBSERVED_QUOTE_TO_USD":
        raise ShortLabConfigError(
            f"hedge.fx.policy must be OBSERVED_QUOTE_TO_USD, got {fx_policy!r}")
    fx_hist = _as_str("hedge.fx.historical_policy", fx.get("historical_policy"))
    if fx_hist != "AT_EVENT_NO_INTERPOLATION":
        raise ShortLabConfigError(
            "hedge.fx.historical_policy must be AT_EVENT_NO_INTERPOLATION, "
            f"got {fx_hist!r}")
    fx_map = {
        "policy": fx_policy,
        "max_age_sec": _as_int("hedge.fx.max_age_sec", fx.get("max_age_sec"), lo=1),
        "historical_policy": fx_hist,
    }
    quality = _require_mapping("hedge.quality", value.get("quality"))
    _check_no_unknown("hedge.quality", quality, {"group_weights", "field_shares"})
    gw = _require_mapping("hedge.quality.group_weights", quality.get("group_weights"))
    _check_no_unknown("hedge.quality.group_weights", gw,
                      {"funding", "futures", "spot_venue", "identity_units",
                       "contract_time"})
    group_weights = {
        k: _as_int(f"hedge.quality.group_weights.{k}", v, lo=0) for k, v in gw.items()
    }
    if sum(group_weights.values()) != 100:
        raise ShortLabConfigError(
            "hedge.quality.group_weights must sum to 100, "
            f"got {sum(group_weights.values())}")
    shares = _require_mapping("hedge.quality.field_shares", quality.get("field_shares"))
    _check_no_unknown("hedge.quality.field_shares", shares,
                      {"funding", "futures", "spot_venue", "identity_units",
                       "contract_time"})
    expected_shares = {
        "funding": {"current_rate_time", "next_time_interval", "history_7d",
                    "history_30d", "history_90d", "std_rolling"},
        "futures": {"mark_time", "exit_depth_vwap", "trading_rules", "symbol_status"},
        "spot_venue": {"buy_quote", "sell_quote", "rules_decimals", "cost_fee_gas"},
        "identity_units": {"asset_identity", "multiplier", "quote_usd_fx"},
        "contract_time": {"lifecycle", "source_time", "time_alignment", "heartbeat"},
    }
    field_shares: dict[str, Any] = {}
    for group, keys in expected_shares.items():
        block = _require_mapping(f"hedge.quality.field_shares.{group}", shares.get(group))
        _check_no_unknown(f"hedge.quality.field_shares.{group}", block, keys)
        cleaned = {
            k: _as_int(f"hedge.quality.field_shares.{group}.{k}", v, lo=0)
            for k, v in block.items()
        }
        if sum(cleaned.values()) != 100:
            raise ShortLabConfigError(
                f"hedge.quality.field_shares.{group} must sum to 100, "
                f"got {sum(cleaned.values())}")
        field_shares[group] = cleaned
    quality_map = {"group_weights": group_weights, "field_shares": field_shares}
    return HedgeConfig(
        enabled=enabled,
        ratio=_MPT(ratio_map),
        execution=_MPT(execution_map),
        liquidation=_MPT(liquidation_map),
        basis=_MPT(basis_map),
        liquidity_monitor=_MPT(liq_map),
        freshness=_MPT(freshness_map),
        refresh=_MPT(refresh_map),
        costs=_MPT(costs_map),
        providers=_MPT({k: _MPT(v) if isinstance(v, dict) else v
                        for k, v in providers_map.items()}),
        runtime=_MPT(runtime_map),
        fx=_MPT(fx_map),
        quality=_MPT(quality_map),
    )


def _build_config(merged: Mapping[str, Any]) -> ShortLabConfig:
    required = (
        "enabled",
        "analysis_tier",
        "universe",
        "candidate",
        "score_weights",
        "quality_freshness_sec",
        "quality_field_overrides_sec",
        "liquidity",
        "lifecycle",
        "funding",
        "evidence_cost",
        "veto",
        "refresh",
        "providers",
        "identity",
        "ingestion",
        "evidence",
        "maintenance",
        "funding_capture",
        "hedge",
        "optimization",
    )
    missing = [key for key in required if key not in merged]
    if missing:
        raise ShortLabConfigError(f"Short-Lab config is missing required keys: {missing}")
    tier = _as_str("analysis_tier", merged["analysis_tier"])
    if tier not in ANALYSIS_TIERS:
        raise ShortLabConfigError(
            f"analysis_tier must be one of {list(ANALYSIS_TIERS)}, got {tier!r}"
        )
    return ShortLabConfig(
        enabled=_as_bool("enabled", merged["enabled"]),
        analysis_tier=tier,
        universe=_build_universe(merged["universe"]),
        candidate=_build_candidate(merged["candidate"]),
        score_weights=_build_score_weights(merged["score_weights"]),
        quality_freshness_sec=_build_windows(
            "quality_freshness_sec", merged["quality_freshness_sec"]
        ),
        quality_field_overrides_sec=_build_windows(
            "quality_field_overrides_sec", merged["quality_field_overrides_sec"]
        ),
        liquidity=_build_liquidity(merged["liquidity"]),
        lifecycle=_build_lifecycle(merged["lifecycle"]),
        funding=_build_funding(merged["funding"]),
        evidence_cost=_build_evidence_cost(merged["evidence_cost"]),
        veto=_build_veto(merged["veto"]),
        refresh=_build_refresh(merged["refresh"]),
        providers=_build_providers(merged["providers"]),
        identity=_build_identity(merged["identity"]),
        ingestion=_build_ingestion(merged["ingestion"]),
        evidence=_build_evidence(merged["evidence"]),
        maintenance=_build_maintenance(merged["maintenance"]),
        funding_capture=_build_funding_capture(merged["funding_capture"]),
        hedge=_build_hedge(merged["hedge"]),
        optimization=_build_optimization(merged["optimization"]),
    )


def _resolve_path(explicit: Path | None) -> Path:
    if explicit is not None:
        return Path(explicit)
    override = os.environ.get(SHORTLAB_CONFIG_PATH_ENV)
    if override:
        return Path(override)
    return DEFAULT_CONFIG_PATH


def load_shortlab_config(path: Path | None = None) -> ShortLabConfig:
    """Load and validate the Short-Lab config.

    Without `path`, `SHORTLAB_CONFIG_PATH` wins over the packaged
    `shortlab/default.yaml`. Secret *values* are never read here — only the
    env var *names* from `providers.*.api_key_env` are stored.

    The packaged default is read via :mod:`resources` so frozen builds work
    without a source checkout; user overrides still read from the filesystem
    and domain validation is unchanged.
    """
    resolved = _resolve_path(path)
    defaults = _section(_read_default_doc(), where=str(DEFAULT_CONFIG_PATH))
    try:
        is_default = Path(resolved) == DEFAULT_CONFIG_PATH
    except Exception:
        is_default = False
    if is_default:
        return _build_config(dict(defaults))
    if not resolved.is_file():
        raise ShortLabConfigError(f"Short-Lab config file not found: {resolved}")
    user = _section(_read_yaml(resolved), where=str(resolved))
    merged = _deep_merge(dict(defaults), user, "shortlab")
    return _build_config(merged)


# ---------------------------------------------------------------------------
# Hashing (design section 24 canonical JSON algorithm)
# ---------------------------------------------------------------------------


def _scoring_subtree(config: ShortLabConfig) -> dict[str, Any]:
    candidate = config.candidate
    return {
        "candidate": {
            "candidate_ltss": candidate.candidate_ltss,
            "entry_required_blocks": list(candidate.entry_required_blocks),
            "ready_data_quality": candidate.ready_data_quality,
            "ready_entry": candidate.ready_entry,
            "ready_ltss": candidate.ready_ltss,
            "ready_tradeability_score": candidate.ready_tradeability_score,
            "watch_ltss": candidate.watch_ltss,
        },
        "funding": {"lookbacks_days": list(config.funding.lookbacks_days)},
        "lifecycle": {
            "ideal_ath_drawdown_max": config.lifecycle.ideal_ath_drawdown_max,
            "ideal_ath_drawdown_min": config.lifecycle.ideal_ath_drawdown_min,
        },
        "liquidity": {
            "hard_min_futures_volume_usd": config.liquidity.hard_min_futures_volume_usd,
            "hard_min_open_interest_usd": config.liquidity.hard_min_open_interest_usd,
            "preferred_futures_volume_usd": config.liquidity.preferred_futures_volume_usd,
            "preferred_open_interest_usd": config.liquidity.preferred_open_interest_usd,
        },
        "quality_field_overrides_sec": {
            name: {"grace": window.grace, "ttl": window.ttl}
            for name, window in config.quality_field_overrides_sec.items()
        },
        "quality_freshness_sec": {
            group: {"grace": window.grace, "ttl": window.ttl}
            for group, window in config.quality_freshness_sec.items()
        },
        "score_weights": {
            profile: dict(weights) for profile, weights in config.score_weights.items()
        },
        "veto": {
            "breakout_24h": config.veto.breakout_24h,
            "breakout_7d": config.veto.breakout_7d,
            "new_token_days": config.veto.new_token_days,
        },
    }


def scoring_canonical_json(config: ShortLabConfig) -> str:
    """Canonical JSON (single line, no trailing newline) feeding `config_hash`."""
    return json.dumps(
        _scoring_subtree(config),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def config_hash(config: ShortLabConfig) -> str:
    """SHA256 hex of the canonical scoring subtree (UTF-8, no trailing newline)."""
    return hashlib.sha256(scoring_canonical_json(config).encode("utf-8")).hexdigest()


def cost_config_hash(config: ShortLabConfig) -> str:
    """Separate hash for `evidence_cost` (forward-grader cost assumptions)."""
    payload = json.dumps(
        {"evidence_cost": config.to_dict()["evidence_cost"]},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Frozen runtime policy bundle (F05, design A10)
# ---------------------------------------------------------------------------

#: Version stamp for the frozen runtime policy bundle. F01 stores it as
#: `sl_config_snapshot.policy_version` next to `policy_hash`; F06 refuses to
#: replay history when the stored row is missing (`REPLAY_CONFIG_UNAVAILABLE`
#: in `shortlab/inputs.py`) instead of recomputing with today's config.
POLICY_VERSION = "policy-v1"


def _policy_subtree(config: ShortLabConfig) -> dict[str, Any]:
    """Execution-relevant policy keys (excludes cadence/capability/env)."""
    candidate = config.candidate
    data = config.to_dict()
    return {
        "policy_version": POLICY_VERSION,
        "candidate": {
            "candidate_ltss": candidate.candidate_ltss,
            "entry_required_blocks": list(candidate.entry_required_blocks),
            "ready_data_quality": candidate.ready_data_quality,
            "ready_entry": candidate.ready_entry,
            "ready_ltss": candidate.ready_ltss,
            "ready_tradeability_score": candidate.ready_tradeability_score,
            "watch_ltss": candidate.watch_ltss,
        },
        "quality_freshness_sec": data["quality_freshness_sec"],
        "quality_field_overrides_sec": data["quality_field_overrides_sec"],
        "liquidity": data["liquidity"],
        "lifecycle": data["lifecycle"],
        "funding": {"lookbacks_days": list(config.funding.lookbacks_days)},
        "veto": data["veto"],
        "identity": data["identity"],
        "ingestion": data["ingestion"],
        "evidence": data["evidence"],
        "maintenance": data["maintenance"],
    }


def policy_canonical_json(config: ShortLabConfig) -> str:
    """Canonical JSON (single line, no trailing newline) feeding `policy_hash`.

    Same canonical-JSON algorithm as `scoring_canonical_json` (sorted keys,
    compact separators, UTF-8, NaN rejected, no trailing newline).
    """
    return json.dumps(
        _policy_subtree(config),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def policy_hash(config: ShortLabConfig) -> str:
    """SHA256 hex of the frozen runtime policy bundle (UTF-8, no newline)."""
    return hashlib.sha256(policy_canonical_json(config).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# H01 Hedge hashes (B40/B附录F, same canonical-JSON algorithm)
# ---------------------------------------------------------------------------
#
# - `fcs_config_hash` covers the FCS scoring version, Funding gate,
#   reference notional, DQ field rules, quote costs and freshness;
# - `hedge_cost_config_hash` covers cost + FX valuation rules;
# - `hedge_policy_hash` covers Hedge formula, risk/stop/ratio/basis exit
#   thresholds (independent of FCS/cost).
# Key/URL/path values, provider enablement, refresh cadence, runtime queue
# limits and local paths never enter any of the three (B附录F.4); the legacy
# LTSS `config_hash`/`policy_hash` inputs are unchanged.

#: Frozen version stamps for the three H01 projections.
FCS_VERSION_H01 = "fcs_v1"
COST_FORMULA_VERSION_H01 = "NATIVE_NOTIONAL_VWAP_INCLUDED_ONCE_V1"
HEDGE_FORMULA_VERSION_H01 = "hedge_v1"
PLAN_SAFETY_RULES_VERSION_H01 = "PLAN_SAFETY_V1"
VENUE_SELECTION_VERSION_H01 = "VERIFIED_TWO_SIDED_COST_V1"


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def _fcs_subtree(config: ShortLabConfig) -> dict[str, Any]:
    fc = config.funding_capture
    hedge = config.hedge
    return {
        "funding_capture": {
            "entry_gate": _plain(fc.entry_gate),
            "fcs_version": fc.fcs_version,
            "reference_notional_usd": fc.reference_notional_usd,
            "score_rules": _plain(fc.score_rules),
            "statistics": _plain(fc.statistics),
        },
        "hedge": {
            "basis_fresh_max_skew_sec": dict(hedge.basis)["fresh_max_skew_sec"],
            "costs": _plain(hedge.costs),
            "freshness": {
                group: {"grace": window.grace, "ttl": window.ttl}
                for group, window in hedge.freshness.items()
            },
            "fx": _plain(hedge.fx),
            "max_price_impact_bps": dict(hedge.execution)["max_price_impact_bps"],
            "quality": _plain(hedge.quality),
            "venue_selection_version": VENUE_SELECTION_VERSION_H01,
        },
    }


def fcs_canonical_json(config: ShortLabConfig) -> str:
    """Canonical JSON feeding `fcs_config_hash` (no trailing newline)."""
    return json.dumps(
        _fcs_subtree(config),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def fcs_config_hash(config: ShortLabConfig) -> str:
    """SHA256 hex of the FCS projection (B附录F.1 golden)."""
    return hashlib.sha256(fcs_canonical_json(config).encode("utf-8")).hexdigest()


def _hedge_cost_subtree(config: ShortLabConfig) -> dict[str, Any]:
    return {
        "cost_formula_version": COST_FORMULA_VERSION_H01,
        "costs": _plain(config.hedge.costs),
        "fx": _plain(config.hedge.fx),
    }


def hedge_cost_canonical_json(config: ShortLabConfig) -> str:
    """Canonical JSON feeding `hedge_cost_config_hash` (no newline)."""
    return json.dumps(
        _hedge_cost_subtree(config),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def hedge_cost_config_hash(config: ShortLabConfig) -> str:
    """SHA256 hex of the cost/FX projection (B附录F.2 golden)."""
    return hashlib.sha256(
        hedge_cost_canonical_json(config).encode("utf-8")).hexdigest()


def _hedge_policy_subtree(config: ShortLabConfig) -> dict[str, Any]:
    hedge = config.hedge
    return {
        "funding_exit": _plain(config.funding_capture.exit),
        "hedge": {
            "basis": _plain(hedge.basis),
            "execution": _plain(hedge.execution),
            "freshness": {
                group: {"grace": window.grace, "ttl": window.ttl}
                for group, window in hedge.freshness.items()
            },
            "fx": _plain(hedge.fx),
            "liquidation": _plain(hedge.liquidation),
            "liquidity_monitor": _plain(hedge.liquidity_monitor),
            "quality": _plain(hedge.quality),
            "ratio": _plain(hedge.ratio),
        },
        "hedge_formula_version": HEDGE_FORMULA_VERSION_H01,
        "plan_safety_rules_version": PLAN_SAFETY_RULES_VERSION_H01,
    }


def hedge_policy_canonical_json(config: ShortLabConfig) -> str:
    """Canonical JSON feeding `hedge_policy_hash` (no trailing newline)."""
    return json.dumps(
        _hedge_policy_subtree(config),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def hedge_policy_hash(config: ShortLabConfig) -> str:
    """SHA256 hex of the Hedge policy projection (B附录F.3 golden)."""
    return hashlib.sha256(
        hedge_policy_canonical_json(config).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# R00 decision policy hash (D15, repair freeze)
# ---------------------------------------------------------------------------

#: Version stamp for the R00 decision policy bundle (separate from POLICY_VERSION).
DECISION_POLICY_VERSION = "decision-policy-v1"


def _decision_policy_subtree(config: ShortLabConfig) -> dict[str, Any]:
    """Execution-relevant decision bundle (excludes providers/refresh/paths).

    Covers decision strategy + candidate direction thresholds + funding
    entry_gate + hedge cost/quantity/liquidation capability + related repair
    versions. Providers/refresh/paths/secrets/runtime budget never enter.
    Version imports reference R00 current constants (never legacy H01 aliases
    for new computation).
    """
    from diveintocrypto_desktop.shortlab.hedge import (
        COST_FORMULA_VERSION_V2,
        FCS_VERSION_V2,
        HEDGE_EVIDENCE_VERSION_V3,
        HEDGE_FORMULA_VERSION_V2,
        PLAN_SAFETY_RULES_VERSION_V2,
        VENUE_SELECTION_VERSION_V2,
    )
    from diveintocrypto_desktop.shortlab.scoring.versions import (
        ENTRY_VERSION_V3,
        FEATURE_VERSION_V3,
    )

    candidate = config.candidate
    fc = config.funding_capture
    hedge = config.hedge
    opt = config.optimization
    return {
        "candidate": {
            "candidate_ltss": candidate.candidate_ltss,
            "entry_required_blocks": list(candidate.entry_required_blocks),
            "ready_data_quality": candidate.ready_data_quality,
            "ready_entry": candidate.ready_entry,
            "ready_ltss": candidate.ready_ltss,
            "ready_tradeability_score": candidate.ready_tradeability_score,
            "watch_ltss": candidate.watch_ltss,
        },
        "decision": _plain(opt.decision),
        "decision_policy_version": DECISION_POLICY_VERSION,
        "funding_entry_gate": _plain(fc.entry_gate),
        "funding_capture": {
            "fcs_version": FCS_VERSION_V2,
            "reference_hold_days": fc.reference_hold_days,
            "reference_notional_usd": fc.reference_notional_usd,
        },
        "hedge": {
            "basis": _plain(hedge.basis),
            "costs": _plain(hedge.costs),
            "execution": _plain(hedge.execution),
            "fx": _plain(hedge.fx),
            "liquidation": _plain(hedge.liquidation),
            "liquidity_monitor": _plain(hedge.liquidity_monitor),
            "quality": _plain(hedge.quality),
            "ratio": _plain(hedge.ratio),
        },
        "optimization_evidence": _plain(opt.evidence),
        "optimization_funding_schedule": _plain(opt.funding_schedule),
        "optimization_protection": _plain(opt.protection),
        "repair_schema_version": "repair-contract-v1",
        "versions": {
            "cost": COST_FORMULA_VERSION_V2,
            "decision": "hedge-decision-v1",
            "entry": ENTRY_VERSION_V3,
            "evidence": HEDGE_EVIDENCE_VERSION_V3,
            "fcs": FCS_VERSION_V2,
            "features": FEATURE_VERSION_V3,
            "hedge": HEDGE_FORMULA_VERSION_V2,
            "safety": PLAN_SAFETY_RULES_VERSION_V2,
            "venue": VENUE_SELECTION_VERSION_V2,
        },
    }


def decision_policy_canonical_json(config: ShortLabConfig | Mapping[str, Any]) -> str:
    """Canonical JSON feeding ``decision_policy_hash`` (no trailing newline)."""
    cfg = _coerce_to_config(config)
    return json.dumps(
        _decision_policy_subtree(cfg),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def decision_policy_hash(config: ShortLabConfig | Mapping[str, Any]) -> str:
    """SHA256 hex of the R00 decision policy bundle (UTF-8, no newline).

    Accepts a built :class:`ShortLabConfig` or a raw mapping (normalized via
    the standard merge+build path). Providers/refresh/paths/secrets never
    enter the hash.
    """
    return hashlib.sha256(
        decision_policy_canonical_json(config).encode("utf-8")
    ).hexdigest()


def _coerce_to_config(config: ShortLabConfig | Mapping[str, Any]) -> ShortLabConfig:
    if isinstance(config, ShortLabConfig):
        return config
    if isinstance(config, Mapping):
        defaults = _section(_read_default_doc(), where=str(DEFAULT_CONFIG_PATH))
        # Allow both {shortlab: {...}} and bare {...} shapes.
        if "shortlab" in config and isinstance(config["shortlab"], Mapping):
            user_section = config["shortlab"]
        else:
            # Heuristic: if mapping looks like a full shortlab section (has
            # 'candidate' etc.), treat it as the section; else treat as override.
            user_section = config
        merged = _deep_merge(dict(defaults), dict(user_section), "shortlab")
        return _build_config(merged)
    raise ShortLabConfigError(
        f"decision_policy_hash requires ShortLabConfig or Mapping, got {type(config).__name__}"
    )
