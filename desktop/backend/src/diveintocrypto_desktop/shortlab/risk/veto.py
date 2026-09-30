"""Task 11: VETO / PAUSE / WARN risk engine and dual status (design 16/17).

Pure computation (no HTTP / SQL). ``evaluate_risks`` turns point-in-time
``features`` + ``metadata`` + ``dq`` into a deterministic :class:`RiskResult`;
:func:`derive_status` implements the section 17 state machine verbatim and
returns the frozen :class:`CandidateState` from Task 1 ``shortlab/models.py``
(never redefined here).

Section 16 codes
----------------
BLOCK (``VETO_*``): ``VETO_DATA_IDENTITY``, ``VETO_LOW_DATA_QUALITY``,
``VETO_LOW_LIQUIDITY``, ``VETO_CONTRACT_DELISTING``.
PAUSE (``PAUSE_*``): ``PAUSE_BREAKOUT_24H`` (``priceChange24h >= 0.35``),
``PAUSE_BREAKOUT_7D`` (``>= 0.70``), ``PAUSE_SQUEEZE`` (price-up + OI-up +
taker-buy/micro confirmation, see ``risk/squeeze.py``),
``PAUSE_NEGATIVE_CARRY`` (7D/30D funding negative or 30D positive-ratio
below 0.4), ``PAUSE_NEW_TOKEN`` (valid onboard age < 45d),
``PAUSE_CONTRACT_STATUS_UNVERIFIED`` (seen contract missing from the live
universe without terminal evidence), ``PAUSE_MAJOR_CATALYST``.
WARN (``WARN_*``): ``WARN_HIGH_VOLATILITY``, ``WARN_FUNDING_WEAKENING``,
``WARN_PROVIDER_PARTIAL``, ``WARN_HIGH_CONCENTRATION``. WARN never changes
the status and never enters ``reasons``.

Section 17 machine
------------------
``candidateStatus`` from LTSS (null/<60 EXCLUDED, 60-70 WATCH, >=70
CANDIDATE). ``executionStatus`` precedence: any BLOCK -> BLOCKED; else
non-CANDIDATE -> NOT_READY; else MEDIUM identity -> NOT_READY
(``IDENTITY_REVIEW_REQUIRED``, pauses still reported); else unverified
cross-source multiplier -> NOT_READY (``MULTIPLIER_UNVERIFIED``); else no
valid onboardDate -> NOT_READY (``LISTING_AGE_UNKNOWN``); else any PAUSE ->
PAUSED; else READY only when non-stale, LTSS>=80, Entry>=70, DQ>=80,
Tradeability>=7 and confidence VERIFIED/HIGH; else NOT_READY. Display
``status``: BLOCKED > EXCLUDED > WATCH > PAUSED > READY > CANDIDATE.
``reasons`` are deduped BLOCK -> PAUSE -> NOT_READY; ``warnings`` stay
independent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from diveintocrypto_desktop.shortlab.models import CandidateState
from diveintocrypto_desktop.shortlab.risk.squeeze import evaluate_squeeze

__all__ = [
    "VETO_DATA_IDENTITY",
    "VETO_LOW_DATA_QUALITY",
    "VETO_LOW_LIQUIDITY",
    "VETO_CONTRACT_DELISTING",
    "PAUSE_BREAKOUT_24H",
    "PAUSE_BREAKOUT_7D",
    "PAUSE_SQUEEZE",
    "PAUSE_NEGATIVE_CARRY",
    "PAUSE_NEW_TOKEN",
    "PAUSE_CONTRACT_STATUS_UNVERIFIED",
    "PAUSE_MAJOR_CATALYST",
    "WARN_HIGH_VOLATILITY",
    "WARN_FUNDING_WEAKENING",
    "WARN_PROVIDER_PARTIAL",
    "WARN_HIGH_CONCENTRATION",
    "IDENTITY_REVIEW_REQUIRED",
    "MULTIPLIER_UNVERIFIED",
    "LISTING_AGE_UNKNOWN",
    "READY_INPUT_STALE",
    "LTSS_BELOW_READY",
    "ENTRY_NOT_AVAILABLE",
    "ENTRY_BUDGET_EXHAUSTED",
    "ENTRY_BELOW_READY_THRESHOLD",
    "DATA_QUALITY_BELOW_READY",
    "TRADEABILITY_BELOW_READY",
    "BLOCK_ORDER",
    "PAUSE_ORDER",
    "NOT_READY_ORDER",
    "WARN_ORDER",
    "READY_LTSS",
    "READY_ENTRY",
    "READY_DQ",
    "READY_TRADEABILITY",
    "VETO_DQ_THRESHOLD",
    "RiskResult",
    "evaluate_risks",
    "derive_status",
]

# -- stable machine codes ----------------------------------------------------
VETO_DATA_IDENTITY = "VETO_DATA_IDENTITY"
VETO_LOW_DATA_QUALITY = "VETO_LOW_DATA_QUALITY"
VETO_LOW_LIQUIDITY = "VETO_LOW_LIQUIDITY"
VETO_CONTRACT_DELISTING = "VETO_CONTRACT_DELISTING"

PAUSE_BREAKOUT_24H = "PAUSE_BREAKOUT_24H"
PAUSE_BREAKOUT_7D = "PAUSE_BREAKOUT_7D"
PAUSE_SQUEEZE = "PAUSE_SQUEEZE"
PAUSE_NEGATIVE_CARRY = "PAUSE_NEGATIVE_CARRY"
PAUSE_NEW_TOKEN = "PAUSE_NEW_TOKEN"
PAUSE_CONTRACT_STATUS_UNVERIFIED = "PAUSE_CONTRACT_STATUS_UNVERIFIED"
PAUSE_MAJOR_CATALYST = "PAUSE_MAJOR_CATALYST"

WARN_HIGH_VOLATILITY = "WARN_HIGH_VOLATILITY"
WARN_FUNDING_WEAKENING = "WARN_FUNDING_WEAKENING"
WARN_PROVIDER_PARTIAL = "WARN_PROVIDER_PARTIAL"
WARN_HIGH_CONCENTRATION = "WARN_HIGH_CONCENTRATION"

IDENTITY_REVIEW_REQUIRED = "IDENTITY_REVIEW_REQUIRED"
MULTIPLIER_UNVERIFIED = "MULTIPLIER_UNVERIFIED"
LISTING_AGE_UNKNOWN = "LISTING_AGE_UNKNOWN"
READY_INPUT_STALE = "READY_INPUT_STALE"

LTSS_BELOW_READY = "LTSS_BELOW_READY"
ENTRY_NOT_AVAILABLE = "ENTRY_NOT_AVAILABLE"
ENTRY_BUDGET_EXHAUSTED = "ENTRY_BUDGET_EXHAUSTED"
ENTRY_BELOW_READY_THRESHOLD = "ENTRY_BELOW_READY_THRESHOLD"
DATA_QUALITY_BELOW_READY = "DATA_QUALITY_BELOW_READY"
TRADEABILITY_BELOW_READY = "TRADEABILITY_BELOW_READY"

BLOCK_ORDER = (
    VETO_DATA_IDENTITY,
    VETO_LOW_DATA_QUALITY,
    VETO_LOW_LIQUIDITY,
    VETO_CONTRACT_DELISTING,
)
PAUSE_ORDER = (
    PAUSE_BREAKOUT_24H,
    PAUSE_BREAKOUT_7D,
    PAUSE_SQUEEZE,
    PAUSE_NEGATIVE_CARRY,
    PAUSE_NEW_TOKEN,
    PAUSE_CONTRACT_STATUS_UNVERIFIED,
    PAUSE_MAJOR_CATALYST,
)
NOT_READY_ORDER = (
    LTSS_BELOW_READY,
    ENTRY_NOT_AVAILABLE,
    ENTRY_BUDGET_EXHAUSTED,
    ENTRY_BELOW_READY_THRESHOLD,
    DATA_QUALITY_BELOW_READY,
    TRADEABILITY_BELOW_READY,
    IDENTITY_REVIEW_REQUIRED,
    MULTIPLIER_UNVERIFIED,
    LISTING_AGE_UNKNOWN,
    READY_INPUT_STALE,
)
WARN_ORDER = (
    WARN_HIGH_VOLATILITY,
    WARN_FUNDING_WEAKENING,
    WARN_PROVIDER_PARTIAL,
    WARN_HIGH_CONCENTRATION,
)

# -- thresholds (mirror default.yaml candidate/liquidity/veto) ---------------
READY_LTSS = 80
READY_ENTRY = 70
READY_DQ = 80
READY_TRADEABILITY = 7
VETO_DQ_THRESHOLD = 60

BREAKOUT_24H = 0.35
BREAKOUT_7D = 0.70
NEW_TOKEN_DAYS = 45
HARD_MIN_FUTURES_QV = 10_000_000.0
HARD_MIN_OI_USD = 2_000_000.0
DAY_MS = 86_400_000
DELIVERY_VETO_WINDOW_MS = 7 * DAY_MS

TRADABLE_STATUS = "TRADING"
LISTING_FLOW_STATUSES = frozenset({"PENDING_TRADING", "PRE_TRADING"})
# Explicit terminal states confirming the contract is leaving/has left trading
# (design 10.2/16.1). HALT/BREAK and other non-TRADING states mean "cannot
# trade right now" (VETO_LOW_LIQUIDITY), not a confirmed delisting.
TERMINAL_STATUSES = frozenset(
    {
        "CLOSE",
        "CLOSED",
        "CLOSING",
        "DELISTED",
        "DELISTING",
        "SETTLING",
        "SETTLE",
        "DELIVERING",
        "EXPIRED",
        "TERMINATED",
    }
)


@dataclass(frozen=True)
class RiskResult:
    """Deterministic risk verdict (never API JSON)."""

    vetoes: tuple[str, ...] = ()
    pauses: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in ("vetoes", "pauses", "warnings"):
            value = getattr(self, name)
            items = tuple(value) if value is not None else ()
            for item in items:
                if not isinstance(item, str):
                    raise ValueError(f"{name} must be str items, got {item!r}")
            object.__setattr__(self, name, items)


# -- small helpers -----------------------------------------------------------


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


def _get(source: Any, *names: str) -> Any:
    if isinstance(source, Mapping):
        for name in names:
            if name in source:
                return source[name]
    else:
        for name in names:
            try:
                value = getattr(source, name)
            except AttributeError:
                continue
            if callable(value):
                continue
            return value
    return None


def _nested_features(features: Any) -> list[Any]:
    """Candidate mappings to search, metadata-first ordering handled by caller."""
    out: list[Any] = []
    if features is None:
        return out
    out.append(features)
    inner = _get(features, "features")
    if isinstance(inner, Mapping):
        out.append(inner)
        nested = inner.get("_inputs")
        if isinstance(nested, Mapping):
            out.append(nested)
    direct = _get(features, "_inputs")
    if isinstance(direct, Mapping):
        out.append(direct)
    meta = _get(features, "source_meta")
    if isinstance(meta, Mapping):
        out.append(meta)
    return out


def _pick(features: Any, metadata: Any, *names: str) -> Any:
    for source in (metadata, *(_nested_features(features))):
        value = _get(source, *names)
        if value is not None:
            return value
    return None


def _dq_value(dq: Any) -> float | None:
    if dq is None:
        return None
    value = _get(dq, "data_quality", "dq", "dataQuality")
    if value is None and isinstance(dq, (int, float)) and not isinstance(dq, bool):
        value = dq
    return _finite(value)


def _risk_lists(risk: Any) -> tuple[list[str], list[str], list[str]]:
    if risk is None:
        return [], [], []
    if isinstance(risk, RiskResult):
        return list(risk.vetoes), list(risk.pauses), list(risk.warnings)
    vetoes = _get(risk, "vetoes") or []
    pauses = _get(risk, "pauses") or []
    warnings = _get(risk, "warnings") or []
    return (
        [str(v) for v in vetoes],
        [str(p) for p in pauses],
        [str(w) for w in warnings],
    )


def _sort_unique(codes: list[str], order: tuple[str, ...]) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for code in codes:
        if code not in seen:
            seen[code] = None
    rank = {code: idx for idx, code in enumerate(order)}
    return tuple(
        sorted(seen, key=lambda c: (rank.get(c, len(order)), c))
    )


# ---------------------------------------------------------------------------
# evaluate_risks
# ---------------------------------------------------------------------------


def evaluate_risks(features: Any, metadata: Any, dq: Any) -> RiskResult:
    """Evaluate BLOCK / PAUSE / WARN from traceable contract, funding, price
    and OI inputs. Same inputs always produce the same result.

    ``features`` may be a Task 10 ``FeatureSnapshot`` (or its ``features``
    mapping); ``metadata`` carries identity, lifecycle, market and provider
    flags (see module docstring for recognised keys); ``dq`` is a float or a
    ``DQBreakdown``-like object. ``metadata`` wins over ``features`` on key
    collisions.
    """
    meta = metadata if isinstance(metadata, Mapping) else {}
    vetoes: list[str] = []
    pauses: list[str] = []
    warnings: list[str] = []

    dq_num = _dq_value(dq)
    if dq_num is None or dq_num < VETO_DQ_THRESHOLD:
        vetoes.append(VETO_LOW_DATA_QUALITY)

    confidence = _pick(features, meta, "mapping_confidence", "confidence")
    conflict = _pick(features, meta, "identity_conflict", "provider_identity_conflict")
    if (isinstance(confidence, str) and confidence in ("LOW", "UNRESOLVED")) or bool(
        conflict
    ):
        vetoes.append(VETO_DATA_IDENTITY)

    futures_qv = _finite(
        _pick(features, meta, "futures_qv_1d", "futures_quote_volume_24h", "futures_qv")
    )
    oi_usd = _finite(_pick(features, meta, "oi_value_usd", "oi_usd", "open_interest_usd"))
    contract_status = _pick(
        features, meta, "contract_status", "exchange_status", "status"
    )
    if futures_qv is not None and futures_qv < HARD_MIN_FUTURES_QV:
        vetoes.append(VETO_LOW_LIQUIDITY)
    if oi_usd is not None and oi_usd < HARD_MIN_OI_USD:
        vetoes.append(VETO_LOW_LIQUIDITY)
    if (
        isinstance(contract_status, str)
        and contract_status
        and contract_status != TRADABLE_STATUS
        and contract_status not in LISTING_FLOW_STATUSES
        and contract_status not in TERMINAL_STATUSES
    ):
        # Non-tradable right now (HALT/BREAK/...) without confirmed terminal
        # evidence: liquidity veto. Terminal states are handled as delisting
        # below; listing-flow states are never vetoes.
        vetoes.append(VETO_LOW_LIQUIDITY)

    # -- confirmed delisting -------------------------------------------------
    delisting_confirmed = _pick(
        features, meta, "delisting_confirmed", "contract_delisting_confirmed"
    )
    exchange_status = _pick(features, meta, "exchange_status", "contract_exchange_status")
    # Fall back to contract_status when the exchange-specific key is absent.
    if exchange_status is None and isinstance(contract_status, str):
        exchange_status = contract_status
    delivery_at = _pick(features, meta, "delivery_at_ms", "deliveryAtMs")
    as_of = _pick(features, meta, "as_of_ms", "asOfMs")
    as_of_num = _finite(as_of)
    if bool(delisting_confirmed):
        vetoes.append(VETO_CONTRACT_DELISTING)
    elif isinstance(exchange_status, str) and exchange_status:
        # Only explicit terminal states confirm a delisting. HALT/BREAK,
        # missing status or listing-flow states never veto here (they map to
        # LOW_LIQUIDITY or UNVERIFIED pause instead).
        if exchange_status in TERMINAL_STATUSES:
            vetoes.append(VETO_CONTRACT_DELISTING)
    if VETO_CONTRACT_DELISTING not in vetoes:
        try:
            delivery_num = None if delivery_at is None else int(delivery_at)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            delivery_num = None
        if (
            delivery_num is not None
            and delivery_num > 0
            and as_of_num is not None
            and abs(delivery_num - int(as_of_num)) <= DELIVERY_VETO_WINDOW_MS
        ):
            vetoes.append(VETO_CONTRACT_DELISTING)

    # -- breakout pauses -----------------------------------------------------
    chg_24h = _finite(_pick(features, meta, "price_change_24h", "ret_24h", "chg_24h"))
    chg_7d = _finite(
        _pick(features, meta, "price_change_7d", "ret_7d", "return_7d", "chg_7d")
    )
    if chg_24h is not None and chg_24h >= BREAKOUT_24H:
        pauses.append(PAUSE_BREAKOUT_24H)
    if chg_7d is not None and chg_7d >= BREAKOUT_7D:
        pauses.append(PAUSE_BREAKOUT_7D)

    if evaluate_squeeze(features, meta):
        pauses.append(PAUSE_SQUEEZE)

    funding_30d = _finite(_pick(features, meta, "funding_30d", "funding30d"))
    funding_7d = _finite(_pick(features, meta, "funding_7d", "funding7d"))
    pos_30d = _finite(
        _pick(
            features,
            meta,
            "funding_positive_ratio_30d",
            "positive_funding_ratio_30d",
            "positive_ratio_30d",
        )
    )
    if (
        (funding_30d is not None and funding_30d < 0)
        or (funding_7d is not None and funding_7d < 0)
        or (pos_30d is not None and pos_30d < 0.4)
    ):
        pauses.append(PAUSE_NEGATIVE_CARRY)

    # -- new-token pause (valid onboardDate only) -----------------------------
    onboard_at = _pick(features, meta, "onboard_at_ms", "onboardAtMs", "listing_at_ms")
    try:
        onboard_num = None if onboard_at is None else int(onboard_at)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        onboard_num = None
    if onboard_num is not None and onboard_num > 0 and as_of_num is not None:
        age_days = (int(as_of_num) - onboard_num) / DAY_MS
        if 0 <= age_days < NEW_TOKEN_DAYS:
            pauses.append(PAUSE_NEW_TOKEN)

    # -- unverified disappearance --------------------------------------------
    live_present = _pick(features, meta, "live_universe_present", "in_live_universe")
    previously_seen = _pick(features, meta, "previously_seen", "symbol_known", "known_symbol")
    if live_present is False and VETO_CONTRACT_DELISTING not in vetoes:
        # Explicit terminal evidence already vetoed above; a disappearance
        # with no terminal evidence pauses. Never-seen symbols (explicit
        # previously_seen False) make no claim.
        if previously_seen is not False:
            # Missing exchange status is also unverified, never TRADING.
            if not isinstance(exchange_status, str) or exchange_status in (
                TRADABLE_STATUS,
                *tuple(LISTING_FLOW_STATUSES),
                None,
            ):
                pauses.append(PAUSE_CONTRACT_STATUS_UNVERIFIED)
            else:
                # Non-terminal but unexpected status string with no veto:
                # still unverified rather than silently OK.
                pauses.append(PAUSE_CONTRACT_STATUS_UNVERIFIED)

    catalyst_major = _pick(
        features, meta, "catalyst_major_event", "major_catalyst_event"
    )
    catalyst_severity = _pick(features, meta, "catalyst_severity", "severity")
    if bool(catalyst_major) or (
        isinstance(catalyst_severity, str) and catalyst_severity.upper() == "MAJOR"
    ):
        pauses.append(PAUSE_MAJOR_CATALYST)

    # -- warnings --------------------------------------------------------------
    volatility = _finite(
        _pick(features, meta, "volatility_30d", "price_volatility_30d", "volatility")
    )
    atr_pct = _finite(_pick(features, meta, "atr_pct", "atr_percent"))
    if (volatility is not None and volatility > 0.15) or (
        atr_pct is not None and atr_pct > 0.08
    ):
        warnings.append(WARN_HIGH_VOLATILITY)

    pos_7d = _finite(
        _pick(
            features,
            meta,
            "funding_positive_ratio_7d",
            "positive_funding_ratio_7d",
            "positive_ratio_7d",
        )
    )
    if funding_30d is not None and funding_30d > 0:
        if (funding_7d is not None and funding_7d <= 0) or (
            pos_30d is not None
            and pos_30d >= 0.5
            and pos_7d is not None
            and pos_7d < 0.4
        ):
            warnings.append(WARN_FUNDING_WEAKENING)

    provider_partial = _pick(features, meta, "provider_partial", "has_partial_provider")
    unlock_status = _pick(features, meta, "unlock_status")
    social_status = _pick(features, meta, "social_status")
    if bool(provider_partial) or unlock_status == "PARTIAL" or social_status == "PARTIAL":
        warnings.append(WARN_PROVIDER_PARTIAL)

    concentration = _finite(
        _pick(features, meta, "holder_concentration", "concentration")
    )
    if concentration is not None and concentration > 0.5:
        warnings.append(WARN_HIGH_CONCENTRATION)

    return RiskResult(
        vetoes=_sort_unique(vetoes, BLOCK_ORDER),
        pauses=_sort_unique(pauses, PAUSE_ORDER),
        warnings=_sort_unique(warnings, WARN_ORDER),
    )


# ---------------------------------------------------------------------------
# derive_status
# ---------------------------------------------------------------------------


def _identity_parts(identity: Any) -> tuple[str | None, float | None, bool | None]:
    """Return (confidence, multiplier, has_valid_onboard).

    ``has_valid_onboard`` is True/False when the caller states it explicitly
    (``onboard_at_ms`` int, ``has_valid_onboard`` / ``listing_age_known`` /
    ``valid_onboard`` bools); None when the caller provides no onboard signal
    at all (treated as unknown downstream).
    """
    confidence = _get(identity, "mapping_confidence", "confidence")
    if not isinstance(confidence, str):
        confidence = None
    multiplier = _finite(_get(identity, "contract_multiplier", "multiplier"))

    onboard_raw = _get(identity, "onboard_at_ms", "onboardAtMs", "listing_at_ms")
    has_valid: bool | None = None
    if onboard_raw is not None:
        try:
            has_valid = int(onboard_raw) > 0  # type: ignore[arg-type]
        except (TypeError, ValueError):
            has_valid = False
    for key in ("has_valid_onboard", "listing_age_known", "valid_onboard", "has_valid_onboard_date"):
        flag = _get(identity, key)
        if flag is not None:
            has_valid = bool(flag)
            break
    return confidence, multiplier, has_valid


def derive_status(
    ltss: Any,
    entry: Any,
    dq: Any,
    tradeability: Any,
    identity: Any,
    risk: Any,
    stale: Any,
    *,
    entry_budget_exhausted: bool = False,
    ready_ltss: float = READY_LTSS,
    ready_entry: float = READY_ENTRY,
    ready_dq: float = READY_DQ,
    ready_tradeability: float = READY_TRADEABILITY,
) -> CandidateState:
    """Derive the dual status (design section 17, verbatim precedence).

    ``ltss``/``entry``/``tradeability`` are floats (or None); ``dq`` is a
    float or ``DQBreakdown``-like; ``identity`` exposes ``mapping_confidence``
    / ``contract_multiplier`` / onboard signal; ``risk`` is a
    :class:`RiskResult` (or mapping with ``vetoes``/``pauses``/``warnings``);
    ``stale`` is the top-level stale flag (key READY input stale or score
    snapshot expired). Extra keyword thresholds default to the frozen
    ``default.yaml`` candidate gates and exist only for explicit boundary
    tests.
    """
    ltss_num = _finite(ltss)
    entry_num = _finite(entry)
    dq_num = _dq_value(dq)
    trade_num = _finite(tradeability)
    stale_flag = bool(stale)

    confidence, multiplier, has_valid_onboard = _identity_parts(identity)
    risk_vetoes, risk_pauses, risk_warnings = _risk_lists(risk)

    # -- candidateStatus ------------------------------------------------------
    if ltss_num is None or ltss_num < 60:
        candidate: str = "EXCLUDED"
    elif ltss_num < 70:
        candidate = "WATCH"
    else:
        candidate = "CANDIDATE"

    # -- effective vetoes (identity/DQ safety net) -----------------------------
    effective_vetoes = list(risk_vetoes)
    if (isinstance(confidence, str) and confidence in ("LOW", "UNRESOLVED")) or (
        confidence is None and _get(identity, "identity_conflict") is True
    ):
        if VETO_DATA_IDENTITY not in effective_vetoes:
            effective_vetoes.append(VETO_DATA_IDENTITY)
    # An explicit provider-identity conflict blocks even with HIGH confidence.
    if bool(_get(identity, "identity_conflict", "provider_identity_conflict")):
        if VETO_DATA_IDENTITY not in effective_vetoes:
            effective_vetoes.append(VETO_DATA_IDENTITY)
    if dq_num is None or dq_num < VETO_DQ_THRESHOLD:
        if VETO_LOW_DATA_QUALITY not in effective_vetoes:
            effective_vetoes.append(VETO_LOW_DATA_QUALITY)
    vetoes = _sort_unique(effective_vetoes, BLOCK_ORDER)
    pauses = _sort_unique(list(risk_pauses), PAUSE_ORDER)
    warnings = _sort_unique(list(risk_warnings), WARN_ORDER)

    # -- executionStatus --------------------------------------------------------
    not_ready: list[str] = []
    if vetoes:
        execution = "BLOCKED"
    elif candidate != "CANDIDATE":
        execution = "NOT_READY"
    elif confidence == "MEDIUM":
        execution = "NOT_READY"
        not_ready.append(IDENTITY_REVIEW_REQUIRED)
    elif multiplier is None:
        execution = "NOT_READY"
        not_ready.append(MULTIPLIER_UNVERIFIED)
    elif has_valid_onboard is not True:
        execution = "NOT_READY"
        not_ready.append(LISTING_AGE_UNKNOWN)
    elif pauses:
        execution = "PAUSED"
    elif (
        not stale_flag
        and ltss_num is not None
        and ltss_num >= ready_ltss
        and entry_num is not None
        and entry_num >= ready_entry
        and dq_num is not None
        and dq_num >= ready_dq
        and trade_num is not None
        and trade_num >= ready_tradeability
        and confidence in ("VERIFIED", "HIGH")
    ):
        execution = "READY"
    else:
        execution = "NOT_READY"

    # -- NOT_READY sub-reasons (CANDIDATE only; EXCLUDED/WATCH already explain
    #    execution via the candidate leg, while BLOCK/PAUSE always surface) ----
    if candidate == "CANDIDATE" and execution == "NOT_READY":
        if ltss_num is None or ltss_num < ready_ltss:
            not_ready.append(LTSS_BELOW_READY)
        if entry_num is None:
            not_ready.append(
                ENTRY_BUDGET_EXHAUSTED if entry_budget_exhausted else ENTRY_NOT_AVAILABLE
            )
        elif entry_num < ready_entry:
            not_ready.append(ENTRY_BELOW_READY_THRESHOLD)
        if dq_num is None or dq_num < ready_dq:
            not_ready.append(DATA_QUALITY_BELOW_READY)
        if trade_num is None or trade_num < ready_tradeability:
            not_ready.append(TRADEABILITY_BELOW_READY)
        if confidence == "MEDIUM" and IDENTITY_REVIEW_REQUIRED not in not_ready:
            not_ready.append(IDENTITY_REVIEW_REQUIRED)
        if multiplier is None and MULTIPLIER_UNVERIFIED not in not_ready:
            not_ready.append(MULTIPLIER_UNVERIFIED)
        if has_valid_onboard is not True and LISTING_AGE_UNKNOWN not in not_ready:
            not_ready.append(LISTING_AGE_UNKNOWN)
        if stale_flag:
            not_ready.append(READY_INPUT_STALE)
    if candidate == "CANDIDATE" and execution == "PAUSED" and stale_flag:
        # A stale key input on an otherwise pausable candidate must still
        # surface; execution stays PAUSED (pause outranks the stale gate).
        not_ready.append(READY_INPUT_STALE)

    reasons = _sort_unique(
        [*vetoes, *pauses, *not_ready],
        (*BLOCK_ORDER, *PAUSE_ORDER, *NOT_READY_ORDER),
    )

    # -- display status ----------------------------------------------------------
    if execution == "BLOCKED":
        display = "BLOCKED"
    elif candidate == "EXCLUDED":
        display = "EXCLUDED"
    elif candidate == "WATCH":
        display = "WATCH"
    elif execution == "PAUSED":
        display = "PAUSED"
    elif execution == "READY":
        display = "READY"
    else:
        display = "CANDIDATE"

    return CandidateState(
        candidate_status=candidate,  # type: ignore[arg-type]
        execution_status=execution,  # type: ignore[arg-type]
        status=display,  # type: ignore[arg-type]
        reasons=reasons,
        vetoes=vetoes,
        pauses=pauses,
        warnings=warnings,
    )
