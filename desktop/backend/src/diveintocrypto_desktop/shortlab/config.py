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
                name: {"enabled": provider.enabled, "api_key_env": provider.api_key_env}
                for name, provider in self.providers.items()
            },
        }

    def public_dict(self) -> dict[str, Any]:
        """Log/API-safe view: provider entries keep only the enabled flag."""
        data = self.to_dict()
        data["providers"] = {
            name: {"enabled": provider.enabled} for name, provider in self.providers.items()
        }
        return data


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> Any:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ShortLabConfigError(f"cannot read Short-Lab config {path}: {exc}") from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ShortLabConfigError(f"invalid YAML in Short-Lab config {path}: {exc}") from exc


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
        unknown = sorted(k for k in entry if k not in ("enabled", "api_key_env"))
        if unknown:
            raise ShortLabConfigError(f"{where} has unknown keys: {unknown}")
        providers[_as_str(f"{where} provider name", name)] = ProviderConfig(
            enabled=_as_bool(f"{where}.enabled", entry.get("enabled")),
            api_key_env=_as_optional_str(f"{where}.api_key_env", entry.get("api_key_env")),
        )
    return MappingProxyType(providers)


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
    """
    resolved = _resolve_path(path)
    if not resolved.is_file():
        raise ShortLabConfigError(f"Short-Lab config file not found: {resolved}")
    defaults = _section(_read_yaml(DEFAULT_CONFIG_PATH), where=str(DEFAULT_CONFIG_PATH))
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
