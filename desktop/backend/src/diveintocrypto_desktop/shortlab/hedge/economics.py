"""R06a economics: term net-carry + ratio proposals (D06.2/D07.2, pure).

Pure module: no network, no DB, no clock reads. All amounts are Decimal
strings (D03.1); floats are rejected at the boundary. Maths runs under an
80-digit localcontext; rounding is display-only (``-0`` -> ``0``).

- :func:`evaluate_economics`: ``conservative_carry = N_actual * APR * days/365``,
  ``roundtrip = entry + exit + slippage + gas``, ``net = carry - roundtrip``,
  ``BE = roundtrip / (N * APR / 365)`` (``None``/``NON_POSITIVE_CARRY`` when
  ``APR <= 0``). Quantities are rounded first, then ``N`` is recomputed;
  VWAP spread is never double-deducted (``slippage`` is explicit only).
  ``fees_included`` must be an explicit bool (missing => ``UNKNOWN``, never
  default-free). ``net > min_net_carry_usd`` strictly (equal => ``FAIL``).
  ``hold_days`` 1..365 is required (missing/out-of-range => ``UNKNOWN``).
- :func:`build_ratio_proposal`: single-target proposal with real quantities,
  scenario exit amounts and read-only ``h0`` (no faked Spot leg).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Mapping, Sequence

from diveintocrypto_desktop.shortlab.hedge import COST_FORMULA_VERSION_V2
from diveintocrypto_desktop.shortlab.hedge.units import floor_to_step
from diveintocrypto_desktop.shortlab.repair_contracts import (
    DecisionContext,
    DecisionRequest,
    EconomicsResult,
    FundingContext,
    GateResult,
    RatioProposal,
    ScenarioResult,
)

__all__ = ["evaluate_economics", "evaluate_position_scenarios", "build_ratio_proposal"]

_COST_BASIS = COST_FORMULA_VERSION_V2

_DEFAULT_MIN_NET_CARRY = Decimal("0")
_DEFAULT_RESERVE_FRACTION = Decimal("0.05")
_DEFAULT_RATIO_TOLERANCE = Decimal("0.02")
_DEFAULT_EXIT_STRESS_BPS = 100
_DEFAULT_FUT_ENTRY_RATE = Decimal("0.0005")
_DEFAULT_FUT_EXIT_RATE = Decimal("0.0005")
_DEFAULT_SPOT_ENTRY_RATE = Decimal("0.001")
_DEFAULT_SPOT_EXIT_RATE = Decimal("0.001")
_DEFAULT_ALPHA_ENTRY_RATE = Decimal("0.001")
_DEFAULT_ALPHA_EXIT_RATE = Decimal("0.001")
_DEFAULT_ONCHAIN_BUFFER_BPS = Decimal("20")

_DEFAULT_SCENARIOS: dict[str, dict[str, str]] = {
    "UP_50": {"futures_move": "0.50", "spot_move": "0.50", "fx_shock": "0"},
    "UP_100": {"futures_move": "1", "spot_move": "1", "fx_shock": "0"},
    "DOWN_50": {"futures_move": "-0.50", "spot_move": "-0.50", "fx_shock": "0"},
    "BASIS_UP": {"futures_move": "0.20", "spot_move": "0.10", "fx_shock": "0"},
    "BASIS_DOWN": {"futures_move": "-0.10", "spot_move": "-0.20", "fx_shock": "0"},
    "FX_DOWN": {"futures_move": "0", "spot_move": "0", "fx_shock": "-0.03"},
}
_SCENARIO_ORDER = ("UP_50", "UP_100", "DOWN_50", "BASIS_UP", "BASIS_DOWN", "FX_DOWN")


# ---------------------------------------------------------------------------
# Decimal helpers.
# ---------------------------------------------------------------------------


def _parse_decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a decimal string, got {value!r}")
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{name} must be a non-empty decimal string")
        try:
            parsed = Decimal(text)
        except (InvalidOperation, ValueError, ArithmeticError) as exc:
            raise ValueError(f"{name} is not a decimal string: {value!r}") from exc
    else:
        raise ValueError(f"{name} must be a decimal string (float rejected), got {value!r}")
    if not parsed.is_finite():
        raise ValueError(f"{name} must be finite, got {value!r}")
    return parsed


def _parse_optional_decimal(value: Any, name: str) -> Decimal | None:
    if value is None:
        return None
    return _parse_decimal(value, name)


def _dec_str(value: Decimal) -> str:
    if not isinstance(value, Decimal):
        value = Decimal(value)
    if not value.is_finite():
        raise ValueError("non-finite decimal cannot be serialised")
    if value == 0:
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
        if text in ("", "-"):
            return "0"
        if text.startswith("."):
            text = "0" + text
        elif text.startswith("-."):
            text = "-0." + text[2:]
    if text == "-0":
        return "0"
    return text


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


# ---------------------------------------------------------------------------
# Policy extraction (ShortLabConfig or Mapping, read-only).
# ---------------------------------------------------------------------------


def _as_mapping_or_none(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    # Dataclass-like (ShortLabConfig sub-configs expose to_dict or attrs).
    if hasattr(value, "to_dict") and callable(getattr(value, "to_dict")):
        try:
            data = value.to_dict()  # type: ignore[no-any-return]
            if isinstance(data, Mapping):
                return dict(data)
        except Exception:
            pass
    return None


def _extract_decision_node(policy: Any) -> dict[str, Any]:
    """Return the ``optimization.decision`` mapping (or direct keys)."""
    if policy is None:
        return {}
    # ShortLabConfig instance.
    opt = getattr(policy, "optimization", None)
    if opt is not None:
        dec = getattr(opt, "decision", None)
        if isinstance(dec, Mapping):
            return dict(dec)
        if dec is not None:
            try:
                # OptimizationConfig.decision is already a Mapping.
                return dict(dec)  # type: ignore[arg-type]
            except Exception:
                pass
    if isinstance(policy, Mapping):
        node: Any = policy
        if "optimization" in node and isinstance(node["optimization"], Mapping):
            node = node["optimization"]
            if "decision" in node and isinstance(node["decision"], Mapping):
                return dict(node["decision"])
            # optimisation node itself carries decision keys?
            if "min_net_carry_usd" in node:
                return dict(node)
        if "decision" in node and isinstance(node["decision"], Mapping):
            return dict(node["decision"])
        if "min_net_carry_usd" in node or "capital_reserve_fraction" in node:
            return dict(node)
    return {}


def _extract_costs_node(policy: Any) -> dict[str, Any]:
    """Return the ``hedge.costs`` mapping (or direct fee keys)."""
    if policy is None:
        return {}
    hedge = getattr(policy, "hedge", None)
    if hedge is not None:
        costs = getattr(hedge, "costs", None)
        if isinstance(costs, Mapping):
            return dict(costs)
    if isinstance(policy, Mapping):
        node: Any = policy
        if "hedge" in node and isinstance(node["hedge"], Mapping):
            inner = node["hedge"]
            if "costs" in inner and isinstance(inner["costs"], Mapping):
                return dict(inner["costs"])
        if "costs" in node and isinstance(node["costs"], Mapping):
            return dict(node["costs"])
        if "futures_entry_fee_rate" in node:
            return dict(node)
    return {}


def _extract_min_net_carry(policy: Any) -> Decimal:
    node = _extract_decision_node(policy)
    # cost_policy for evaluate_economics may directly carry the key.
    if isinstance(policy, Mapping) and "min_net_carry_usd" in policy and not node:
        node = dict(policy)
    raw = node.get("min_net_carry_usd", "0")
    if raw is None:
        return Decimal("0")
    parsed = _parse_decimal(raw, "min_net_carry_usd")
    if parsed < 0:
        raise ValueError("min_net_carry_usd must be >= 0")
    return parsed


def _extract_reserve_fraction(proposal: Mapping, policy: Any) -> Decimal | None:
    raw: Any = None
    if isinstance(proposal, Mapping) and proposal.get("reserve_fraction") is not None:
        raw = proposal.get("reserve_fraction")
    else:
        node = _extract_decision_node(policy)
        if isinstance(policy, Mapping) and "capital_reserve_fraction" in policy and "capital_reserve_fraction" not in node:
            raw = policy.get("capital_reserve_fraction")
        else:
            raw = node.get("capital_reserve_fraction", "0.05")
    if raw is None:
        return None
    try:
        parsed = _parse_decimal(raw, "reserve_fraction")
    except ValueError:
        return None
    if parsed < 0 or parsed > 1:
        return None
    return parsed


def _extract_ratio_tolerance(policy: Any) -> Decimal:
    node = _extract_decision_node(policy)
    raw = node.get("ratio_tolerance", "0.02")
    try:
        parsed = _parse_decimal(raw, "ratio_tolerance")
    except ValueError:
        return _DEFAULT_RATIO_TOLERANCE
    if parsed < 0 or parsed > 1:
        return _DEFAULT_RATIO_TOLERANCE
    return parsed


def _extract_exit_stress_bps(policy: Any) -> int:
    node = _extract_decision_node(policy)
    raw = node.get("exit_stress_bps", _DEFAULT_EXIT_STRESS_BPS)
    try:
        if isinstance(raw, bool):
            raise ValueError("bool")
        out = int(Decimal(str(raw)))
    except Exception:
        return _DEFAULT_EXIT_STRESS_BPS
    return max(0, out)


def _extract_scenarios(policy: Any) -> dict[str, dict[str, str]]:
    node = _extract_decision_node(policy)
    raw = node.get("scenarios")
    if not isinstance(raw, Mapping):
        return {k: dict(v) for k, v in _DEFAULT_SCENARIOS.items()}
    out: dict[str, dict[str, str]] = {}
    for key in _SCENARIO_ORDER:
        leg = raw.get(key)
        if not isinstance(leg, Mapping):
            out[key] = dict(_DEFAULT_SCENARIOS[key])
            continue
        try:
            out[key] = {
                "futures_move": _dec_str(_parse_decimal(leg.get("futures_move"), f"scenarios.{key}.futures_move")),
                "spot_move": _dec_str(_parse_decimal(leg.get("spot_move"), f"scenarios.{key}.spot_move")),
                "fx_shock": _dec_str(_parse_decimal(leg.get("fx_shock"), f"scenarios.{key}.fx_shock")),
            }
        except ValueError:
            out[key] = dict(_DEFAULT_SCENARIOS[key])
    return out


def _fee_rate(costs: Mapping[str, Any], key: str, default: Decimal) -> Decimal:
    raw = costs.get(key, default)
    if raw is None:
        return default
    try:
        if isinstance(raw, bool):
            raise ValueError("bool")
        parsed = Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError):
        return default
    if not parsed.is_finite() or parsed < 0 or parsed >= 1:
        return default
    return parsed


# ---------------------------------------------------------------------------
# evaluate_economics (D06.2/D19.5).
# ---------------------------------------------------------------------------


def evaluate_economics(
    proposal: Mapping,
    funding_context: FundingContext,
    hold_days: int | None,
    cost_policy: Mapping | Any,
) -> EconomicsResult:
    """Term net-carry economics (pure).

    ``proposal`` must carry ``actual_futures_notional_usd`` (required),
    ``spot_cash_usd``/``margin_usd`` (``None`` => capital unknown, never
    zero-filled), ``entry_fee_usd``/``exit_fee_usd``/``slippage_usd``/
    ``gas_usd`` (``None`` => ``UNKNOWN``), ``fees_included`` (explicit
    bool, missing => ``UNKNOWN``) and ``reserve_fraction`` (fallback to
    policy ``capital_reserve_fraction``). ``hold_days`` 1..365 is required.
    ``funding_context.conservative_apr`` is the carry source.
    """
    if not isinstance(proposal, Mapping):
        raise TypeError(f"proposal must be a mapping, got {type(proposal).__name__}")
    if not isinstance(funding_context, FundingContext):
        raise TypeError(f"funding_context must be FundingContext, got {type(funding_context).__name__}")
    input_refs = dict(funding_context.input_refs) if isinstance(funding_context.input_refs, Mapping) else {}

    # -- required actual notional (structural; FX unknown must surface as
    #    an explicit UNKNOWN via costs/APR, never as a zero-filled N) -------
    raw_actual = proposal.get("actual_futures_notional_usd")
    if raw_actual is None:
        raise ValueError("proposal.actual_futures_notional_usd is required (None would zero-fill)")
    try:
        actual = _parse_decimal(raw_actual, "actual_futures_notional_usd")
    except ValueError as exc:
        raise ValueError(f"proposal.actual_futures_notional_usd invalid: {exc}") from exc
    if actual <= 0:
        raise ValueError("proposal.actual_futures_notional_usd must be > 0")

    # -- hold validation ------------------------------------------------------
    hold_ok = (
        isinstance(hold_days, int)
        and not isinstance(hold_days, bool)
        and 1 <= int(hold_days) <= 365
    )
    hold_int: int | None = int(hold_days) if hold_ok else None

    # -- APR ------------------------------------------------------------------
    apr: Decimal | None = None
    apr_unknown = False
    try:
        apr = _parse_optional_decimal(funding_context.conservative_apr, "conservative_apr")
    except ValueError:
        apr = None
    if apr is None:
        apr_unknown = True

    # -- costs ----------------------------------------------------------------
    fees_included = proposal.get("fees_included")
    fees_known = isinstance(fees_included, bool)

    def _safe(key: str) -> Decimal | None:
        if key not in proposal:
            return None
        try:
            return _parse_optional_decimal(proposal.get(key), key)
        except ValueError:
            return None

    entry = _safe("entry_fee_usd")
    exit_fee = _safe("exit_fee_usd")
    slippage = _safe("slippage_usd")
    gas = _safe("gas_usd")
    costs_known = (
        fees_known
        and entry is not None
        and exit_fee is not None
        and slippage is not None
        and gas is not None
    )

    # -- spot / margin / reserve (capital only) --------------------------------
    try:
        spot_cash: Decimal | None = _parse_optional_decimal(proposal.get("spot_cash_usd"), "spot_cash_usd")
    except ValueError:
        spot_cash = None
    try:
        margin: Decimal | None = _parse_optional_decimal(proposal.get("margin_usd"), "margin_usd")
    except ValueError:
        margin = None
    # Missing keys => None (capital unknown, not zero-filled).
    if "spot_cash_usd" not in proposal:
        spot_cash = None
    if "margin_usd" not in proposal:
        margin = None
    reserve_frac = _extract_reserve_fraction(proposal, cost_policy)

    unknown_components: list[str] = []
    if not hold_ok:
        unknown_components.append("HOLD_DAYS_UNKNOWN")
    if apr_unknown:
        unknown_components.append("UNKNOWN_APR")
    if not fees_known:
        unknown_components.append("FEES_INCLUDED_UNKNOWN")
    if entry is None or exit_fee is None or slippage is None or gas is None:
        if "UNKNOWN_COST" not in unknown_components:
            unknown_components.append("UNKNOWN_COST")
    # FX unknown surfaces as an uncomputable actual; actual is required so a
    # None actual raises above. A present-but-unverifiable FX is modelled by
    # the caller passing None costs (UNKNOWN_COST) rather than zero-filling.

    min_net = _extract_min_net_carry(cost_policy)

    # -- incomplete => UNKNOWN --------------------------------------------------
    if not hold_ok or apr_unknown or not costs_known:
        # Capital may still be computable; report it when known.
        capital: str | None = None
        if spot_cash is not None and margin is not None and entry is not None and reserve_frac is not None:
            with localcontext() as ctx:
                ctx.prec = 80
                base = spot_cash + margin
                reserve = base * reserve_frac
                capital_dec = base + entry + reserve
            capital = _dec_str(capital_dec)
        else:
            if "UNKNOWN_CAPITAL" not in unknown_components:
                unknown_components.append("UNKNOWN_CAPITAL")
        reasons: list[str] = []
        if not hold_ok:
            reasons.append("HOLD_DAYS_UNKNOWN")
        if apr_unknown:
            reasons.append("UNKNOWN_APR")
        if not costs_known:
            reasons.append("UNKNOWN_COST")
            if not fees_known and "FEES_INCLUDED_UNKNOWN" not in reasons:
                reasons.append("FEES_INCLUDED_UNKNOWN")
        if not reasons:
            reasons.append("UNKNOWN_COST")
        gate = GateResult("UNKNOWN", tuple(sorted(set(reasons))), 0, dict(input_refs))
        return EconomicsResult(
            hold_days=hold_int,
            actual_futures_notional_usd=_dec_str(actual),
            conservative_carry_usd=None,
            roundtrip_cost_usd=None,
            net_carry_usd=None,
            break_even_days=None,
            capital_required_usd=capital,
            cost_basis=_COST_BASIS,
            gate=gate,
            unknown_components=tuple(sorted(set(unknown_components))),
        )

    assert hold_int is not None and apr is not None
    assert entry is not None and exit_fee is not None and slippage is not None and gas is not None
    with localcontext() as ctx:
        ctx.prec = 80
        carry = actual * apr * Decimal(hold_int) / Decimal("365")
        roundtrip = entry + exit_fee + slippage + gas
        net = carry - roundtrip
        if apr > 0 and actual > 0:
            be: Decimal | None = roundtrip / (actual * apr / Decimal("365"))
        else:
            be = None
        # Capital: spot + margin + entry + reserve (unrealised PnL never counts).
        if spot_cash is not None and margin is not None and reserve_frac is not None:
            base2 = spot_cash + margin
            reserve2 = base2 * reserve_frac
            capital_dec2 = base2 + entry + reserve2
            capital2: str | None = _dec_str(capital_dec2)
        else:
            capital2 = None
            if "UNKNOWN_CAPITAL" not in unknown_components:
                unknown_components.append("UNKNOWN_CAPITAL")

    if be is None:
        # APR <= 0: no break-even; net is still defined (negative).
        gate2 = GateResult("FAIL", ("NON_POSITIVE_CARRY",), 0, dict(input_refs))
        return EconomicsResult(
            hold_days=hold_int,
            actual_futures_notional_usd=_dec_str(actual),
            conservative_carry_usd=_dec_str(carry),
            roundtrip_cost_usd=_dec_str(roundtrip),
            net_carry_usd=_dec_str(net),
            break_even_days=None,
            capital_required_usd=capital2,
            cost_basis=_COST_BASIS,
            gate=gate2,
            unknown_components=tuple(sorted(set(unknown_components))),
        )

    # Strictly greater (Decimal, never float): equal => FAIL.
    if net > min_net:
        gate3 = GateResult("PASS", (), 0, dict(input_refs))
    else:
        gate3 = GateResult("FAIL", ("NET_CARRY_BELOW_MIN",), 0, dict(input_refs))
    return EconomicsResult(
        hold_days=hold_int,
        actual_futures_notional_usd=_dec_str(actual),
        conservative_carry_usd=_dec_str(carry),
        roundtrip_cost_usd=_dec_str(roundtrip),
        net_carry_usd=_dec_str(net),
        break_even_days=_dec_str(be),
        capital_required_usd=capital2,
        cost_basis=_COST_BASIS,
        gate=gate3,
        unknown_components=tuple(sorted(set(unknown_components))),
    )


def evaluate_position_scenarios(
    *,
    futures_qty: Any,
    spot_qty: Any,
    futures_mark: Any,
    liquidation_price: Any,
    futures_entry_price: Any,
    futures_entry_fx: Any,
    futures_exit_price: Any,
    futures_exit_fx: Any,
    spot_entry_price: Any,
    spot_entry_fx: Any,
    spot_exit_price: Any,
    spot_exit_fx: Any,
    futures_exit_fee_rate: Any,
    spot_exit_fee_rate: Any,
    entry_cost_usd: Any,
    slippage_usd: Any,
    gas_usd: Any,
    policy: Any,
    has_spot: bool = True,
) -> tuple[ScenarioResult, ...]:
    """Evaluate the six configured shocks on exact remaining native quantities.

    This is shared by new ratio proposals and activation's re-check of the
    current ledger position. Missing FX, prices, fees, or liquidation data
    produce UNKNOWN rows; carry never offsets scenario losses.
    """
    if not isinstance(has_spot, bool):
        raise ValueError("has_spot must be bool")
    scenarios_cfg = _extract_scenarios(policy)
    stress_bps = _extract_exit_stress_bps(policy)

    def _opt(name: str, value: Any) -> Decimal | None:
        try:
            return _parse_optional_decimal(value, name)
        except ValueError:
            return None

    fqty = _opt("futures_qty", futures_qty)
    sqty = _opt("spot_qty", spot_qty)
    mark = _opt("futures_mark", futures_mark)
    liq = _opt("liquidation_price", liquidation_price)
    fent = _opt("futures_entry_price", futures_entry_price)
    fentryfx = _opt("futures_entry_fx", futures_entry_fx)
    fexit = _opt("futures_exit_price", futures_exit_price)
    fexitfx = _opt("futures_exit_fx", futures_exit_fx)
    sent = _opt("spot_entry_price", spot_entry_price) if has_spot else Decimal("0")
    sentfx = _opt("spot_entry_fx", spot_entry_fx) if has_spot else Decimal("1")
    sexit = _opt("spot_exit_price", spot_exit_price) if has_spot else Decimal("0")
    sexitfx = _opt("spot_exit_fx", spot_exit_fx) if has_spot else Decimal("1")
    f_rate = _opt("futures_exit_fee_rate", futures_exit_fee_rate)
    s_rate = _opt("spot_exit_fee_rate", spot_exit_fee_rate) if has_spot else Decimal("0")
    entry_cost = _opt("entry_cost_usd", entry_cost_usd)
    slip = _opt("slippage_usd", slippage_usd)
    gas = _opt("gas_usd", gas_usd)
    base_unknown = any(v is None for v in (
        fqty, sqty, mark, liq, fent, fentryfx, fexit, fexitfx,
        sent, sentfx, sexit, sexitfx, f_rate, s_rate,
        entry_cost, slip, gas,
    ))
    if (fqty is not None and fqty <= 0) or (sqty is not None and has_spot and sqty <= 0):
        base_unknown = True
    results: list[ScenarioResult] = []
    for sid in _SCENARIO_ORDER:
        cfg = scenarios_cfg[sid]
        fmove = _parse_decimal(cfg["futures_move"], f"{sid}.futures_move")
        smove = _parse_decimal(cfg["spot_move"], f"{sid}.spot_move")
        fshock = _parse_decimal(cfg["fx_shock"], f"{sid}.fx_shock")
        common = dict(
            scenario_id=sid, futures_move=_dec_str(fmove),
            spot_move=_dec_str(smove), fx_shock=_dec_str(fshock),
        )
        if base_unknown:
            results.append(ScenarioResult(**common, status="UNKNOWN", net_pnl_usd=None,
                                          loss_usd=None, reasons=("UNKNOWN_SCENARIO_INPUT",)))
            continue
        assert fqty is not None and sqty is not None and mark is not None and liq is not None
        assert fent is not None and fentryfx is not None and fexit is not None and fexitfx is not None
        assert sent is not None and sentfx is not None and sexit is not None and sexitfx is not None
        assert f_rate is not None and s_rate is not None and entry_cost is not None and slip is not None and gas is not None
        with localcontext() as ctx:
            ctx.prec = 80
            if mark * (Decimal("1") + fmove) >= liq:
                results.append(ScenarioResult(**common, status="INVALID_AFTER_LIQUIDATION",
                                              net_pnl_usd=None, loss_usd=None,
                                              reasons=("LIQUIDATION_CROSSED",)))
                continue
            fut_fx_s = fexitfx * (Decimal("1") + fshock)
            spot_fx_s = sexitfx * (Decimal("1") + fshock)
            stress_mult = Decimal("1") + Decimal(stress_bps) / Decimal("10000")
            spot_disc = Decimal("1") - Decimal(stress_bps) / Decimal("10000")
            fut_exit_s = fexit * (Decimal("1") + fmove) * stress_mult
            # Linear futures are quote settled: apply the shocked current FX
            # to the native-price spread, never book historical entry FX as
            # principal PnL. Entry fees/cash remain separately FX-frozen.
            fut_pnl = (fent - fut_exit_s) * fqty * fut_fx_s
            fut_fee = fut_exit_s * fqty * fut_fx_s * f_rate
            if has_spot:
                spot_exit_s = sexit * (Decimal("1") + smove) * spot_disc
                spot_pnl = (spot_exit_s * spot_fx_s - sent * sentfx) * sqty
                spot_fee = spot_exit_s * sqty * spot_fx_s * s_rate
            else:
                spot_pnl = Decimal("0")
                spot_fee = Decimal("0")
            net = fut_pnl + spot_pnl - entry_cost - fut_fee - spot_fee - slip - gas
            loss = -net if net < 0 else Decimal("0")
        results.append(ScenarioResult(**common, status="VALID", net_pnl_usd=_dec_str(net),
                                      loss_usd=_dec_str(loss), reasons=()))
    return tuple(results)


# ---------------------------------------------------------------------------
# build_ratio_proposal helpers.
# ---------------------------------------------------------------------------


def _extract_multiplier(identity: Any) -> Decimal | None:
    raw: Any = None
    if isinstance(identity, Mapping):
        raw = identity.get("contract_multiplier", identity.get("multiplier"))
    else:
        raw = getattr(identity, "contract_multiplier", getattr(identity, "multiplier", None))
    if raw is None:
        return None
    try:
        if isinstance(raw, bool):
            return None
        parsed = Decimal(str(raw).strip()) if isinstance(raw, str) else Decimal(str(raw))
    except (InvalidOperation, ValueError, ArithmeticError, TypeError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def _extract_mark_native(futures_mark: Any) -> Decimal | None:
    if futures_mark is None:
        return None
    value = getattr(futures_mark, "value", None)
    if value is None and isinstance(futures_mark, Mapping):
        value = futures_mark.get("value", futures_mark)
    if isinstance(value, Mapping):
        for key in ("price", "mark_price", "markPrice", "native_price", "futures_price", "mark"):
            if value.get(key) is not None:
                try:
                    parsed = _parse_decimal(value[key], "futures_mark.price")
                    return parsed if parsed > 0 else None
                except ValueError:
                    continue
        return None
    try:
        parsed2 = _parse_decimal(value, "futures_mark.price")
        return parsed2 if parsed2 > 0 else None
    except ValueError:
        return None


def _extract_quote_fx(quote: Any) -> Decimal | None:
    if quote is None:
        return None
    raw = _field(quote, "quote_to_usd", _field(quote, "fx_to_usd", None))
    if raw is None:
        return None
    try:
        parsed = _parse_decimal(raw, "quote_to_usd")
        return parsed if parsed > 0 else None
    except ValueError:
        return None


def _extract_exec_price(quote: Any, which: str) -> Decimal | None:
    if quote is None:
        return None
    key = "sell_vwap_native" if which == "sell" else "buy_vwap_native"
    # FuturesExecutionQuote uses sell/buy_vwap_native; SpotVenueQuote uses sell/buy_vwap.
    raw = _field(quote, key, None)
    if raw is None:
        alt = "sell_vwap" if which == "sell" else "buy_vwap"
        raw = _field(quote, alt, None)
    if raw is None and which == "sell":
        raw = _field(quote, "mid_price", None)
    if raw is None:
        return None
    try:
        parsed = _parse_decimal(raw, key)
        return parsed if parsed > 0 else None
    except ValueError:
        return None


def _extract_executable(quote: Any, which: str) -> Decimal | None:
    if quote is None:
        return None
    if which == "sell":
        raw = _field(quote, "sell_executable_qty", None)
    else:
        raw = _field(quote, "buy_executable_qty", None)
    if raw is None:
        return None
    try:
        parsed = _parse_decimal(raw, "executable_qty")
        return parsed if parsed >= 0 else None
    except ValueError:
        return None


def _get_rule_decimal(rules: Any, group: str, key: str) -> Decimal | None:
    group_map: Any = None
    if rules is None:
        return None
    if isinstance(rules, Mapping):
        group_map = rules.get(group)
        if group_map is None and key in rules:
            group_map = rules
    else:
        group_map = getattr(rules, group, None)
    if group_map is None:
        return None
    if isinstance(group_map, Mapping) and key not in group_map:
        return None
    raw: Any = group_map.get(key) if isinstance(group_map, Mapping) else None
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    try:
        text = str(raw).strip() if isinstance(raw, str) else str(raw)
        parsed = Decimal(text)
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    if not parsed.is_finite():
        return None
    return parsed


def _quote_expiry(quote: Any) -> int | None:
    if quote is None:
        return None
    raw = _field(quote, "expires_at_ms", None)
    try:
        return None if raw is None else int(raw)
    except (TypeError, ValueError):
        return None


def _evaluate_funding_gate(
    funding_context: FundingContext, policy: Any, as_of_ms: int, ports: Any | None
) -> GateResult:
    """Funding gate via ports when bound, else direct R05 import (read-only)."""
    if ports is not None:
        fn = getattr(ports, "evaluate_funding_entry_gate", None)
        if callable(fn):
            try:
                res = fn(funding_context, policy, as_of_ms)
                if isinstance(res, GateResult):
                    return res
            except Exception:
                pass
    # Read-only fallback to the frozen R05 implementation.
    try:
        from diveintocrypto_desktop.shortlab.hedge.entry_gate import (
            evaluate_funding_entry_gate as _direct,
        )

        return _direct(funding_context, policy, as_of_ms)
    except Exception:
        return GateResult("UNKNOWN", ("FUNDING_GATE_UNAVAILABLE",), int(as_of_ms), {})


# ---------------------------------------------------------------------------
# build_ratio_proposal (D07.2 subset owned by R06a).
# ---------------------------------------------------------------------------


def build_ratio_proposal(
    request: DecisionRequest,
    context: DecisionContext,
    target_ratio: str,
    policy: Mapping | Any,
    *,
    ports: Any | None = None,
) -> RatioProposal:
    """Single-target ratio proposal (pure, read-only ``h0``).

    Quantities are floored to lot steps, then ``N``/``h``/capital/risk are
    recomputed from the legal quantities (never from the theoretical
    target). ``h0`` builds a contract-only read-only proposal (no Spot
    leg, ``spot_venue=None``, empty spot refs) and never calls a two-leg
    plan-saving interface.
    """
    if not isinstance(request, DecisionRequest):
        raise TypeError(f"request must be DecisionRequest, got {type(request).__name__}")
    if not isinstance(context, DecisionContext):
        raise TypeError(f"context must be DecisionContext, got {type(context).__name__}")
    try:
        target = _parse_decimal(target_ratio, "target_ratio")
    except ValueError as exc:
        raise ValueError(f"target_ratio invalid: {exc}") from exc
    if target < 0 or target > 1:
        raise ValueError(f"target_ratio must be in 0..1, got {target_ratio!r}")

    as_of = int(getattr(context, "as_of_ms", 0) or 0)
    tolerance = _extract_ratio_tolerance(policy)
    stress_bps = _extract_exit_stress_bps(policy)
    scenarios_cfg = _extract_scenarios(policy)
    costs_node = _extract_costs_node(policy)
    fut_entry_rate = _fee_rate(costs_node, "futures_entry_fee_rate", _DEFAULT_FUT_ENTRY_RATE)
    fut_exit_rate = _fee_rate(costs_node, "futures_exit_fee_rate", _DEFAULT_FUT_EXIT_RATE)
    spot_entry_spot = _fee_rate(costs_node, "spot_entry_fee_rate", _DEFAULT_SPOT_ENTRY_RATE)
    spot_exit_spot = _fee_rate(costs_node, "spot_exit_fee_rate", _DEFAULT_SPOT_EXIT_RATE)
    alpha_entry = _fee_rate(costs_node, "alpha_entry_fee_rate", _DEFAULT_ALPHA_ENTRY_RATE)
    alpha_exit = _fee_rate(costs_node, "alpha_exit_fee_rate", _DEFAULT_ALPHA_EXIT_RATE)
    try:
        buffer_bps = _parse_decimal(costs_node.get("onchain_extra_buffer_bps", "20"), "onchain_extra_buffer_bps")
    except ValueError:
        buffer_bps = _DEFAULT_ONCHAIN_BUFFER_BPS

    multiplier = _extract_multiplier(getattr(context, "identity", None))
    mark = _extract_mark_native(getattr(context, "futures_mark", None))
    futures_quote = getattr(context, "futures_quote", None)
    futures_rules = getattr(context, "futures_rules", None)
    venue_quotes: Sequence[Any] = tuple(getattr(context, "venue_quotes", ()) or ())

    try:
        notional_req = _parse_decimal(request.futures_notional_usd, "futures_notional_usd")
        margin_req = _parse_decimal(request.margin_usd, "margin_usd")
        liq_price = _parse_decimal(request.liquidation_price, "liquidation_price")
        available = _parse_decimal(request.available_capital_usd, "available_capital_usd")
        max_loss = _parse_decimal(request.max_scenario_loss_usd, "max_scenario_loss_usd")
    except ValueError as exc:
        raise ValueError(f"request amounts invalid: {exc}") from exc

    # -- funding gate (read-only via ports) ------------------------------------
    funding_gate = _evaluate_funding_gate(context.funding_context, policy, as_of, ports)

    # -- futures leg -------------------------------------------------------------
    fx_fut = _extract_quote_fx(futures_quote)
    # Entry uses SELL VWAP (short open); fallback to mark when quote lacks it.
    fut_sell = _extract_exec_price(futures_quote, "sell")
    fut_buy = _extract_exec_price(futures_quote, "buy")
    entry_px = fut_sell if fut_sell is not None else mark
    # FX unknown => notional unknown (never default 1).
    fx_known = fx_fut is not None
    if not fx_known:
        # No FX: quantities cannot be derived; return UNKNOWN proposal.
        unknown_econ = EconomicsResult(
            hold_days=int(request.planned_hold_days),
            actual_futures_notional_usd=_dec_str(notional_req),
            conservative_carry_usd=None,
            roundtrip_cost_usd=None,
            net_carry_usd=None,
            break_even_days=None,
            capital_required_usd=None,
            cost_basis=_COST_BASIS,
            gate=GateResult("UNKNOWN", ("UNKNOWN_FX",), as_of, {}),
            unknown_components=("UNKNOWN_FX",),
        )
        return RatioProposal(
            target_ratio=_dec_str(target),
            actual_ratio="0",
            futures_contract_qty="0",
            canonical_futures_qty="0",
            spot_net_qty="0",
            spot_venue=None,
            quote_refs={"futures": _field(futures_quote, "quote_id", "unknown") if futures_quote is not None else "unknown"},
            economics=unknown_econ,
            scenarios=(),
            execution_gate=GateResult("UNKNOWN", ("UNKNOWN_FX",), as_of, {}),
            risk_gate=GateResult("UNKNOWN", ("UNKNOWN_FX",), as_of, {}),
            order_guidance=(),
        )

    assert fx_fut is not None
    if entry_px is None or mark is None or multiplier is None:
        # Missing price/multiplier => UNKNOWN (never guess 1000x).
        missing = "UNKNOWN_MULTIPLIER" if multiplier is None else "UNKNOWN_PRICE"
        unknown_econ2 = EconomicsResult(
            hold_days=int(request.planned_hold_days),
            actual_futures_notional_usd=_dec_str(notional_req),
            conservative_carry_usd=None,
            roundtrip_cost_usd=None,
            net_carry_usd=None,
            break_even_days=None,
            capital_required_usd=None,
            cost_basis=_COST_BASIS,
            gate=GateResult("UNKNOWN", (missing,), as_of, {}),
            unknown_components=(missing,),
        )
        return RatioProposal(
            target_ratio=_dec_str(target),
            actual_ratio="0",
            futures_contract_qty="0",
            canonical_futures_qty="0",
            spot_net_qty="0",
            spot_venue=None,
            quote_refs={},
            economics=unknown_econ2,
            scenarios=(),
            execution_gate=GateResult("UNKNOWN", (missing,), as_of, {}),
            risk_gate=GateResult("UNKNOWN", (missing,), as_of, {}),
            order_guidance=(),
        )

    assert entry_px is not None and mark is not None and multiplier is not None
    with localcontext() as ctx:
        ctx.prec = 80
        raw_fut_qty = notional_req / (entry_px * fx_fut)
    fut_step = _get_rule_decimal(futures_rules, "lot_rules", "step_size")
    fut_qty = floor_to_step(raw_fut_qty, fut_step)
    # Rule min/max checks (recorded, not silently clamped).
    fut_min = _get_rule_decimal(futures_rules, "lot_rules", "min_qty")
    fut_max = _get_rule_decimal(futures_rules, "lot_rules", "max_qty")
    qty_issues: list[str] = []
    if fut_qty <= 0:
        qty_issues.append("FUTURES_QTY_NON_POSITIVE_AFTER_ROUNDING")
    if fut_min is not None and fut_min != 0 and fut_qty < fut_min:
        qty_issues.append("FUTURES_BELOW_MIN_QTY")
    if fut_max is not None and fut_max != 0 and fut_qty > fut_max:
        qty_issues.append("FUTURES_ABOVE_MAX_QTY")
    with localcontext() as ctx:
        ctx.prec = 80
        canonical = fut_qty * multiplier if fut_qty > 0 else Decimal("0")
        actual_notional = fut_qty * entry_px * fx_fut if fut_qty > 0 else Decimal("0")

    # Executable capacity (SELL side for entry, BUY side for stressed exit).
    sell_exec = _extract_executable(futures_quote, "sell")
    buy_exec = _extract_executable(futures_quote, "buy")
    capacity_issues: list[str] = []
    if futures_quote is None:
        capacity_issues.append("UNKNOWN_QUOTE")
    else:
        if sell_exec is None or buy_exec is None:
            capacity_issues.append("UNKNOWN_QUOTE")
        else:
            if fut_qty > sell_exec:
                capacity_issues.append("INSUFFICIENT_SELL_CAPACITY")
            if fut_qty > buy_exec:
                capacity_issues.append("INSUFFICIENT_BUY_CAPACITY")

    # -- spot leg ------------------------------------------------------------------
    is_h0 = target == 0
    preferred = getattr(request, "preferred_spot_venue", "AUTO")
    candidates: list[Any] = []
    if not is_h0:
        for q in venue_quotes:
            venue = _field(q, "venue", None)
            if preferred is not None and str(preferred) != "AUTO" and venue != preferred:
                continue
            candidates.append(q)
    # Venue selection: cheapest roundtrip estimate, then latest expiry, then name.
    # Full costing happens per-venue below; here we shortlist by executable.
    best: Any | None = None
    best_key: Any | None = None
    per_venue: list[tuple[Any, Any]] = []
    if is_h0:
        per_venue = []
    elif not candidates:
        per_venue = []
    else:
        for q in candidates:
            venue = str(_field(q, "venue", "UNKNOWN"))
            fx_spot = _extract_quote_fx(q)
            buy_px = _field(q, "buy_vwap", _field(q, "buyVwap", None))
            try:
                buy_dec = _parse_decimal(buy_px, "spot.buy_vwap") if buy_px is not None else None
            except ValueError:
                buy_dec = None
            # Executable for entry (BUY side).
            buy_exec_spot = _field(q, "buy_executable_qty", None)
            try:
                buy_exec_dec = _parse_decimal(buy_exec_spot, "spot.buy_executable") if buy_exec_spot is not None else None
            except ValueError:
                buy_exec_dec = None
            # Raw spot from canonical * target, floored to venue step.
            with localcontext() as ctx:
                ctx.prec = 80
                raw_spot = canonical * target
            # Venue step lives in trading_rules (or flat step_size for doubles).
            trading_rules = _field(q, "trading_rules", None)
            spot_step = _get_rule_decimal(trading_rules, "lot_rules", "step_size")
            if spot_step is None and isinstance(trading_rules, Mapping) and "step_size" in trading_rules:
                try:
                    spot_step = _parse_decimal(trading_rules["step_size"], "spot.step_size")
                except ValueError:
                    spot_step = None
            spot_qty = floor_to_step(raw_spot, spot_step) if raw_spot >= 0 else Decimal("0")
            # Fee preview for tie-break (rate-based; explicit quote fees refine below).
            if venue == "BINANCE_ALPHA":
                er, xr = alpha_entry, alpha_exit
            else:
                er, xr = spot_entry_spot, spot_exit_spot
            if buy_dec is not None and fx_spot is not None:
                with localcontext() as ctx:
                    ctx.prec = 80
                    cash_preview = spot_qty * buy_dec * fx_spot
                    cost_preview = cash_preview * (er + xr)
            else:
                cash_preview = None
                cost_preview = None
            expiry = _quote_expiry(q)
            # Unknown cost never enters executable selection (D07.2).
            if cost_preview is None:
                key = (Decimal("Infinity"), -(expiry or 0), venue)
            else:
                key = (cost_preview, -(expiry or 0), venue)
            per_venue.append((q, {"spot_qty": spot_qty, "cash_preview": cash_preview, "cost_preview": cost_preview, "key": key}))
        # Sort by (cost asc, expiry desc, venue asc); unknown costs last.
        def _sort_key(item: Any) -> Any:
            _q, info = item
            cost = info["cost_preview"]
            expiry2 = _quote_expiry(_q)
            venue2 = str(_field(_q, "venue", "UNKNOWN"))
            # None costs sort after all Decimal costs.
            if cost is None:
                return (1, Decimal("0"), -(expiry2 or 0), venue2)
            return (0, cost, -(expiry2 or 0), venue2)

        per_venue.sort(key=_sort_key)
        best = per_venue[0][0] if per_venue else None
    # Resolve final spot quantities from the winner (or h0).
    if is_h0:
        spot_qty_final = Decimal("0")
        spot_cash_final = Decimal("0")
        fx_spot_final: Decimal | None = None
        spot_buy_final: Decimal | None = None
        spot_sell_final: Decimal | None = None
        spot_venue_final: str | None = None
        spot_quote_final: Any | None = None
    elif best is None:
        spot_qty_final = Decimal("0")
        spot_cash_final = Decimal("0")
        fx_spot_final = None
        spot_buy_final = None
        spot_sell_final = None
        spot_venue_final = None
        spot_quote_final = None
    else:
        spot_quote_final = best
        spot_venue_final = str(_field(best, "venue", "UNKNOWN"))
        fx_raw = _field(best, "quote_to_usd", "1")
        try:
            fx_spot_final = _parse_decimal("1" if fx_raw is None else fx_raw, "spot.quote_to_usd")
        except ValueError:
            fx_spot_final = None
        buy_raw = _field(best, "buy_vwap", None)
        sell_raw = _field(best, "sell_vwap", None)
        try:
            spot_buy_final = _parse_decimal(buy_raw, "spot.buy_vwap") if buy_raw is not None else None
        except ValueError:
            spot_buy_final = None
        try:
            spot_sell_final = _parse_decimal(sell_raw, "spot.sell_vwap") if sell_raw is not None else None
        except ValueError:
            spot_sell_final = None
        # Recompute final qty (same flooring as preview for determinism).
        with localcontext() as ctx:
            ctx.prec = 80
            raw_spot2 = canonical * target
        trading_rules2 = _field(best, "trading_rules", None)
        spot_step2 = _get_rule_decimal(trading_rules2, "lot_rules", "step_size")
        if spot_step2 is None and isinstance(trading_rules2, Mapping) and "step_size" in trading_rules2:
            try:
                spot_step2 = _parse_decimal(trading_rules2["step_size"], "spot.step_size")
            except ValueError:
                spot_step2 = None
        spot_qty_final = floor_to_step(raw_spot2, spot_step2) if raw_spot2 >= 0 else Decimal("0")
        if spot_buy_final is not None and fx_spot_final is not None:
            with localcontext() as ctx:
                ctx.prec = 80
                spot_cash_final = spot_qty_final * spot_buy_final * fx_spot_final
        else:
            spot_cash_final = Decimal("0")

    with localcontext() as ctx:
        ctx.prec = 80
        actual_ratio = (spot_qty_final / canonical) if canonical > 0 else Decimal("0")
        deviation = abs(actual_ratio - target)

    # -- fees (explicit, VWAP impact never double-deducted) -------------------------
    # Futures legs are rate-based on actual notionals (CURRENT_PRICE_REFERENCE
    # for the exit leg); spot legs prefer an explicit quote fee when the
    # venue marks fees_included True, else rate-based. Slippage is explicit
    # only (VWAP already embeds spread).
    if spot_quote_final is not None:
        venue_name = str(_field(spot_quote_final, "venue", "BINANCE_SPOT"))
        if venue_name == "BINANCE_ALPHA":
            ser, sxr = alpha_entry, alpha_exit
        else:
            ser, sxr = spot_entry_spot, spot_exit_spot
    else:
        ser, sxr = spot_entry_spot, spot_exit_spot
        venue_name = "NONE"
    with localcontext() as ctx:
        ctx.prec = 80
        fut_entry_fee = actual_notional * fut_entry_rate if actual_notional > 0 else Decimal("0")
        fut_exit_fee_est = actual_notional * fut_exit_rate if actual_notional > 0 else Decimal("0")
    # Spot fees: explicit quote fee takes precedence when fees_included True.
    spot_entry_fee = Decimal("0")
    spot_exit_fee = Decimal("0")
    gas_total = Decimal("0")
    fees_explicit_ok = True
    if not is_h0:
        if spot_quote_final is None:
            fees_explicit_ok = False
            gas_total = Decimal("0")
            buffer_total = Decimal("0")
        else:
            fees_incl = _field(spot_quote_final, "fees_included", None)
            est_fee_raw = _field(spot_quote_final, "estimated_fee_usd", None)
            est_gas_raw = _field(spot_quote_final, "estimated_gas_usd", None)
            try:
                est_gas = _parse_decimal(est_gas_raw, "spot.estimated_gas") if est_gas_raw is not None else Decimal("0")
            except ValueError:
                est_gas = None
                fees_explicit_ok = False
            if est_gas is None:
                gas_total = Decimal("0")
            else:
                # Gas is a roundtrip cost (entry + exit symmetric assumption
                # would double-count a per-side quote); the quote carries a
                # single-leg estimate, so the roundtrip counts it once per
                # side => 2x would double-count. Count it once (entry side)
                # and document CURRENT_PRICE_REFERENCE for the exit leg.
                gas_total = est_gas
            if fees_incl is True and est_fee_raw is not None:
                try:
                    spot_entry_fee = _parse_decimal(est_fee_raw, "spot.estimated_fee")
                except ValueError:
                    fees_explicit_ok = False
                    spot_entry_fee = Decimal("0")
                with localcontext() as ctx:
                    ctx.prec = 80
                    spot_exit_fee = spot_cash_final * sxr
            elif fees_incl is False:
                with localcontext() as ctx:
                    ctx.prec = 80
                    spot_entry_fee = spot_cash_final * ser
                    spot_exit_fee = spot_cash_final * sxr
            elif fees_incl is None:
                # Unknown fee inclusion => cannot default-free.
                fees_explicit_ok = False
                with localcontext() as ctx:
                    ctx.prec = 80
                    spot_entry_fee = spot_cash_final * ser
                    spot_exit_fee = spot_cash_final * sxr
            else:
                with localcontext() as ctx:
                    ctx.prec = 80
                    spot_entry_fee = spot_cash_final * ser
                    spot_exit_fee = spot_cash_final * sxr
            # ONCHAIN buffer is explicit slippage (VWAP impact stays single-counted).
            buffer_total = Decimal("0")
            if venue_name == "ONCHAIN_DEX":
                with localcontext() as ctx:
                    ctx.prec = 80
                    buffer_total = spot_cash_final * buffer_bps / Decimal("10000")
            else:
                buffer_total = Decimal("0")
        # Futures fees_included: futures_quote carries no amounts, so rates
        # apply; a True flag means VWAP pricing only (no extra double-add).
        _ = _field(futures_quote, "fees_included", None)
    else:
        buffer_total = Decimal("0")
        gas_total = Decimal("0")

    with localcontext() as ctx:
        ctx.prec = 80
        entry_fee_total = fut_entry_fee + (spot_entry_fee if not is_h0 else Decimal("0"))
        exit_fee_est_total = fut_exit_fee_est + (spot_exit_fee if not is_h0 else Decimal("0"))
        slippage_total = buffer_total if not is_h0 else Decimal("0")
    # Futures leg has no gas; spot gas is the only gas leg.
    gas_roundtrip = gas_total

    # Reserve fraction from policy (proposal carries it explicitly).
    reserve_frac_final = _extract_reserve_fraction({"reserve_fraction": None}, policy)
    if reserve_frac_final is None:
        reserve_frac_final = _DEFAULT_RESERVE_FRACTION

    proposal_map: dict[str, Any] = {
        "actual_futures_notional_usd": _dec_str(actual_notional),
        "spot_cash_usd": _dec_str(spot_cash_final),
        "margin_usd": _dec_str(margin_req),
        "entry_fee_usd": _dec_str(entry_fee_total),
        "exit_fee_usd": _dec_str(exit_fee_est_total),
        "slippage_usd": _dec_str(slippage_total),
        "gas_usd": _dec_str(gas_roundtrip),
        "fees_included": True if (is_h0 or fees_explicit_ok) else False,
        "reserve_fraction": _dec_str(reserve_frac_final),
    }
    # Unknown spot quote for h>0 forces fees unknown (never default-free).
    if not is_h0 and spot_quote_final is None:
        proposal_map["fees_included"] = False
        # Mark costs unknown by nulling the exit leg (caller sees UNKNOWN).
        # Keep entry/exit numbers but flag inclusion False so economics can
        # still compute a PASS/FAIL? No: fees_included False is still a bool,
        # so economics stays computable. To force UNKNOWN we null an amount:
        proposal_map["exit_fee_usd"] = None  # type: ignore[dict-item]

    economics = evaluate_economics(
        proposal_map,
        context.funding_context,
        int(request.planned_hold_days),
        policy,
    )

    # -- scenarios (same pure evaluator used by activation on actual holdings) --
    scenario_results = evaluate_position_scenarios(
        futures_qty=fut_qty, spot_qty=spot_qty_final,
        futures_mark=mark, liquidation_price=liq_price,
        futures_entry_price=entry_px, futures_entry_fx=fx_fut,
        futures_exit_price=fut_buy if fut_buy is not None else mark,
        futures_exit_fx=fx_fut,
        spot_entry_price=spot_buy_final, spot_entry_fx=fx_spot_final,
        spot_exit_price=spot_sell_final, spot_exit_fx=fx_spot_final,
        futures_exit_fee_rate=fut_exit_rate, spot_exit_fee_rate=sxr,
        entry_cost_usd=entry_fee_total, slippage_usd=slippage_total,
        gas_usd=gas_roundtrip, policy=policy, has_spot=not is_h0,
    )

    # -- execution / risk gates ----------------------------------------------------------
    exec_reasons: list[str] = []
    exec_unknown: list[str] = []
    if funding_gate.status == "FAIL":
        exec_reasons.append("FUNDING_GATE_FAIL")
    elif funding_gate.status == "UNKNOWN":
        exec_unknown.append("FUNDING_GATE_UNKNOWN")
    if qty_issues:
        exec_reasons.extend(qty_issues)
    if capacity_issues:
        if "UNKNOWN_QUOTE" in capacity_issues:
            exec_unknown.extend(capacity_issues)
        else:
            exec_reasons.extend(capacity_issues)
    if futures_quote is None:
        if "UNKNOWN_QUOTE" not in exec_unknown:
            exec_unknown.append("UNKNOWN_QUOTE")
    # Quote freshness: expired quotes cannot PASS.
    fq_expiry = _quote_expiry(futures_quote)
    if futures_quote is not None and fq_expiry is not None and as_of >= fq_expiry:
        exec_reasons.append("QUOTE_EXPIRED")
    if not is_h0:
        if spot_quote_final is None:
            exec_unknown.append("SPOT_QUOTE_UNKNOWN")
        else:
            sq_expiry = _quote_expiry(spot_quote_final)
            if sq_expiry is not None and as_of >= sq_expiry:
                exec_reasons.append("QUOTE_EXPIRED")
            # Spot capacity: SELL side for exit must cover the position.
            sell_exec_spot = _field(spot_quote_final, "sell_executable_qty", None)
            try:
                sell_dec = _parse_decimal(sell_exec_spot, "spot.sell_executable") if sell_exec_spot is not None else None
            except ValueError:
                sell_dec = None
            if sell_dec is None:
                exec_unknown.append("UNKNOWN_SPOT_CAPACITY")
            elif spot_qty_final > sell_dec:
                exec_reasons.append("INSUFFICIENT_SPOT_CAPACITY")
            buy_exec_spot2 = _field(spot_quote_final, "buy_executable_qty", None)
            try:
                buy_dec2 = _parse_decimal(buy_exec_spot2, "spot.buy_executable") if buy_exec_spot2 is not None else None
            except ValueError:
                buy_dec2 = None
            if buy_dec2 is None:
                exec_unknown.append("UNKNOWN_SPOT_CAPACITY")
            elif spot_qty_final > buy_dec2:
                exec_reasons.append("INSUFFICIENT_SPOT_CAPACITY")
    # Ratio deviation.
    if deviation > tolerance:
        exec_reasons.append("RATIO_DEVIATION_EXCEEDS_TOLERANCE")
    # Capital.
    if economics.capital_required_usd is None:
        exec_unknown.append("UNKNOWN_CAPITAL")
    else:
        try:
            cap_req = _parse_decimal(economics.capital_required_usd, "capital_required")
            if cap_req > available:
                exec_reasons.append("INSUFFICIENT_CAPITAL")
        except ValueError:
            exec_unknown.append("UNKNOWN_CAPITAL")
    # Economics gate feeds execution (Carry/Balanced require PASS; directional
    # reports only, but R06a marks non-PASS as execution FAIL for safety;
    # R07 refines per-goal).
    if economics.gate.status == "FAIL":
        exec_reasons.append("ECONOMIC_GATE_FAIL")
    elif economics.gate.status == "UNKNOWN":
        exec_unknown.append("ECONOMIC_GATE_UNKNOWN")

    if exec_reasons:
        execution_gate = GateResult("FAIL", tuple(sorted(set(exec_reasons))), as_of, {})
    elif exec_unknown:
        execution_gate = GateResult("UNKNOWN", tuple(sorted(set(exec_unknown))), as_of, {})
    else:
        execution_gate = GateResult("PASS", (), as_of, {})

    risk_reasons: list[str] = []
    risk_unknown: list[str] = []
    invalid_scen = [s for s in scenario_results if s.status == "INVALID_AFTER_LIQUIDATION"]
    unknown_scen = [s for s in scenario_results if s.status == "UNKNOWN"]
    if invalid_scen:
        risk_reasons.append("SCENARIO_LIQUIDATION")
    if unknown_scen:
        risk_unknown.append("SCENARIO_UNKNOWN")
    # Max scenario loss budget (real stressed losses, carry credit 0).
    worst_loss: Decimal | None = None
    for s in scenario_results:
        if s.status == "VALID" and s.loss_usd is not None:
            try:
                lv = _parse_decimal(s.loss_usd, "loss")
                worst_loss = lv if worst_loss is None or lv > worst_loss else worst_loss
            except ValueError:
                continue
    if worst_loss is not None and worst_loss > max_loss:
        risk_reasons.append("SCENARIO_LOSS_EXCEEDS_BUDGET")
    if risk_reasons:
        risk_gate = GateResult("FAIL", tuple(sorted(set(risk_reasons))), as_of, {})
    elif risk_unknown:
        risk_gate = GateResult("UNKNOWN", tuple(sorted(set(risk_unknown))), as_of, {})
    else:
        risk_gate = GateResult("PASS", (), as_of, {})

    # -- order guidance (read-only; h0 has futures only, never a faked spot) ----------------
    guidance: list[dict[str, Any]] = []
    fut_id = _field(futures_quote, "quote_id", "futures-quote") if futures_quote is not None else "futures-quote"
    guidance.append(
        {
            "leg": "FUTURES_SHORT",
            "venue": "FUTURES",
            "side": "SELL",
            "qty": _dec_str(fut_qty),
            "price": _dec_str(entry_px),
            "quote_ref": str(fut_id),
            "reduce_only_or_close_position": False,
            "capability": "UNKNOWN",
        }
    )
    quote_refs: dict[str, str] = {"futures": str(fut_id)}
    spot_venue_out: str | None = None
    if not is_h0 and spot_quote_final is not None:
        spot_venue_out = str(_field(spot_quote_final, "venue", "UNKNOWN"))
        spot_id = str(_field(spot_quote_final, "quote_id", _field(spot_quote_final, "symbol", "spot-quote")))
        # SpotVenueQuote fixtures lack quote_id; fall back to symbol/ref.
        if spot_id in ("None", ""):
            spot_id = "spot-quote"
        quote_refs["spot"] = spot_id
        guidance.append(
            {
                "leg": "SPOT_LONG",
                "venue": spot_venue_out,
                "side": "BUY",
                "qty": _dec_str(spot_qty_final),
                "price": _dec_str(spot_buy_final) if spot_buy_final is not None else None,
                "quote_ref": spot_id,
                "reduce_only_or_close_position": False,
                "capability": "UNKNOWN",
            }
        )
    elif not is_h0:
        # No venue: no faked spot leg (read-only honesty).
        spot_venue_out = None

    return RatioProposal(
        target_ratio=_dec_str(target),
        actual_ratio=_dec_str(actual_ratio),
        futures_contract_qty=_dec_str(fut_qty),
        canonical_futures_qty=_dec_str(canonical),
        spot_net_qty=_dec_str(spot_qty_final),
        spot_venue=spot_venue_out,
        quote_refs=dict(quote_refs),
        economics=economics,
        scenarios=tuple(scenario_results),
        execution_gate=execution_gate,
        risk_gate=risk_gate,
        order_guidance=tuple(guidance),
    )
