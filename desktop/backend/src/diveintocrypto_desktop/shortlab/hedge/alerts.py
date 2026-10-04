"""H07 alert evaluation + lifecycle manager (B26/B28.6/B28.8).

Design: severity (INFO/WARN/CRITICAL) is independent from
``recommended_action`` (NONE/REVIEW/PAIR_EXIT/URGENT_PAIR_EXIT);
``EXIT_RECOMMENDED`` is an action, never a severity. The same
``(plan_id, code, leg_or_venue)`` condition in OPEN/ACKNOWLEDGED only bumps
``last_seen``/context (H01 ``upsert_hedge_alert`` does this in one worker
transaction); a re-trigger after RESOLVED opens a new episode. Recovery
needs two consecutive fresh clear ticks; fetch failures never resolve. A
mark crossing the user liquidation price only raises
``LIQUIDATION_POSSIBLE_UNCONFIRMED`` -- never an auto LIQUIDATION and never
a position rewrite. A >5s persist-queue lag marks MONITOR_DEGRADED without
dropping real positions or critical alerts.

Pure functions + an in-memory :class:`AlertManager` for the H07 unit layer.
H08 wires the same changes to the H01 repository transaction.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Mapping

__all__ = [
    "RECOVERY_TICKS",
    "PERSIST_LAG_SEC",
    "AlertChange",
    "AlertManager",
    "dedup_key",
    "evaluate_alerts",
]

#: Consecutive fresh clear ticks required before RESOLVED (B26).
RECOVERY_TICKS = 2
#: Persist-queue lag that forces degraded without losing alerts (B30).
PERSIST_LAG_SEC = 5

_SEVERITIES = ("INFO", "WARN", "CRITICAL")
_ACTIONS = ("NONE", "REVIEW", "PAIR_EXIT", "URGENT_PAIR_EXIT")


class AlertChange(dict):
    """One evaluated condition: ``code/severity/action/active/fresh``.

    ``active=True`` means the condition currently fires on fresh data;
    ``active=False`` + ``fresh=True`` means a fresh clear tick;
    ``fresh=False`` means the inputs were stale/failed and the change must
    neither open a false alert nor resolve a real one.
    """


def dedup_key(plan_id: str, code: str, leg_or_venue: str = "") -> str:
    """Return the B26 dedup key ``(plan_id, code, leg_or_venue)``."""
    return f"{plan_id}:{code}:{leg_or_venue}"


def _opt_dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, bool):
        return None
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = Decimal(text)
        except (InvalidOperation, ValueError, ArithmeticError):
            return None
    else:
        return None
    return parsed if parsed.is_finite() else None


def _snap_field(snapshot: Any, name: str, default: Any = None) -> Any:
    if snapshot is None:
        return default
    if isinstance(snapshot, Mapping):
        if name in snapshot:
            return snapshot[name]
        # camelCase fallback
        camel = name.split("_")
        camel = camel[0] + "".join(p[:1].upper() + p[1:] for p in camel[1:])
        return snapshot.get(camel, default)
    return getattr(snapshot, name, default)


def _metrics(snapshot: Any) -> dict[str, Any]:
    raw = _snap_field(snapshot, "metrics_json", {})
    if isinstance(raw, Mapping):
        return dict(raw)
    return {}


def _hedge_subtree(policy: Any) -> dict[str, Any]:
    if policy is None:
        return {}
    if isinstance(policy, Mapping):
        if "hedge" in policy and isinstance(policy["hedge"], Mapping):
            return dict(policy["hedge"])
        return dict(policy)
    hedge = getattr(policy, "hedge", None)
    if hedge is None:
        return {}
    if isinstance(hedge, Mapping):
        return dict(hedge)
    out: dict[str, Any] = {}
    for key in ("liquidation", "basis", "liquidity_monitor", "ratio",
                "freshness", "runtime"):
        node = getattr(hedge, key, None)
        if node is not None:
            out[key] = dict(node) if isinstance(node, Mapping) else node
    return out


def _num(node: Any, key: str, default: float) -> float:
    if isinstance(node, Mapping) and key in node:
        try:
            return float(node[key])
        except (TypeError, ValueError):
            return default
    return default


def _change(
    plan_id: str,
    code: str,
    severity: str,
    action: str,
    *,
    leg_or_venue: str = "",
    active: bool,
    fresh: bool,
    context: Mapping[str, Any] | None = None,
) -> AlertChange:
    assert severity in _SEVERITIES, f"bad severity {severity!r}"
    assert action in _ACTIONS, f"bad action {action!r}"
    # EXIT_RECOMMENDED-style strings are actions, never severities.
    assert severity != "EXIT_RECOMMENDED"
    return AlertChange({
        "plan_id": plan_id,
        "code": code,
        "severity": severity,
        "recommended_action": action,
        "leg_or_venue": leg_or_venue,
        "dedup_key": dedup_key(plan_id, code, leg_or_venue),
        "active": bool(active),
        "fresh": bool(fresh),
        "context": dict(context or {}),
    })


def evaluate_alerts(
    previous: Any | None,
    current: Any,
    policy: Any = None,
) -> tuple[AlertChange, ...]:
    """Evaluate alert conditions from monitor snapshots (H07.2, B26).

    ``previous``/``current`` are :class:`HedgeMonitor` snapshots (or plain
    mappings with the same fields); ``previous`` is accepted for API
    symmetry and streak detection but the current snapshot alone decides
    every condition so a restart never invents history. Every returned
    change carries an independent ``severity`` and ``recommended_action``.
    Stale/failed inputs yield ``fresh=False`` changes that must not
    resolve open alerts (see :class:`AlertManager`).
    """
    hedge = _hedge_subtree(policy)
    basis_cfg = hedge.get("basis") if isinstance(hedge.get("basis"), Mapping) else {}
    liq_cfg = hedge.get("liquidation") if isinstance(hedge.get("liquidation"), Mapping) else {}
    exit_cfg = hedge.get("liquidity_monitor") if isinstance(hedge.get("liquidity_monitor"), Mapping) else {}
    ratio_cfg = hedge.get("ratio") if isinstance(hedge.get("ratio"), Mapping) else {}
    assert isinstance(basis_cfg, Mapping)
    assert isinstance(liq_cfg, Mapping)
    assert isinstance(exit_cfg, Mapping)
    assert isinstance(ratio_cfg, Mapping)

    plan_id = str(_snap_field(current, "plan_id", "plan-unknown") or "plan-unknown")
    metrics = _metrics(current)
    prev_metrics = _metrics(previous) if previous is not None else {}

    out: list[AlertChange] = []

    # -- freshness gate ------------------------------------------------------
    fut_expired = bool(metrics.get("futures_mark_expired", False))
    spot_expired = bool(metrics.get("spot_quote_expired", False))
    feeds_failed = bool(fut_expired or spot_expired)
    degraded = list(metrics.get("degraded_reasons", []) or [])
    persist_lag_ms = metrics.get("persist_lag_ms", 0)
    try:
        lag_ms = int(persist_lag_ms or 0)
    except (TypeError, ValueError):
        lag_ms = 0
    lag_degraded = lag_ms > PERSIST_LAG_SEC * 1000

    # -- liquidation distance (B22; over user price is unconfirmed only) -----
    liq_state = str(metrics.get("liq_state", "LIQ_UNKNOWN"))
    liq_fresh = not feeds_failed and liq_state not in (
        "LIQ_UNKNOWN", "LIQ_UNKNOWN_NO_PRICE", "LIQ_UNKNOWN_NO_MARK")
    if liq_state == "LIQUIDATION_POSSIBLE_UNCONFIRMED":
        # Mark crossed the user price: possible only, never confirmed.
        out.append(_change(
            plan_id, "LIQUIDATION_POSSIBLE_UNCONFIRMED", "CRITICAL",
            "URGENT_PAIR_EXIT", active=True, fresh=liq_fresh,
            context={"liq_state": liq_state,
                     "distance": _snap_field(current, "liquidation_distance"),
                     "note": "mark crossed user price; unconfirmed, re-verify"},
        ))
    elif liq_state == "EMERGENCY":
        out.append(_change(plan_id, "LIQ_DISTANCE_EMERGENCY", "CRITICAL",
                           "URGENT_PAIR_EXIT", active=True, fresh=liq_fresh,
                           context={"liq_state": liq_state,
                                    "distance": _snap_field(current, "liquidation_distance")}))
    elif liq_state == "CRITICAL":
        out.append(_change(plan_id, "LIQ_DISTANCE_CRITICAL", "CRITICAL",
                           "PAIR_EXIT", active=True, fresh=liq_fresh,
                           context={"liq_state": liq_state,
                                    "distance": _snap_field(current, "liquidation_distance")}))
    elif liq_state == "WARNING":
        out.append(_change(plan_id, "LIQ_DISTANCE_WARNING", "WARN",
                           "REVIEW", active=True, fresh=liq_fresh,
                           context={"liq_state": liq_state,
                                    "distance": _snap_field(current, "liquidation_distance")}))
    else:
        # Fresh NORMAL still emits a clear tick so the manager can count
        # the 2-tick recovery for any previously open liq alert.
        if liq_state == "NORMAL":
            for code in ("LIQ_DISTANCE_WARNING", "LIQ_DISTANCE_CRITICAL",
                         "LIQ_DISTANCE_EMERGENCY",
                         "LIQUIDATION_POSSIBLE_UNCONFIRMED"):
                out.append(_change(plan_id, code, "CRITICAL" if "CRITICAL" in code or "EMERGENCY" in code or "POSSIBLE" in code else "WARN",
                                   "URGENT_PAIR_EXIT" if "EMERGENCY" in code or "POSSIBLE" in code else ("PAIR_EXIT" if "CRITICAL" in code else "REVIEW"),
                                   active=False, fresh=True,
                                   context={"liq_state": liq_state}))
        elif not liq_fresh:
            for code in ("LIQ_DISTANCE_WARNING", "LIQ_DISTANCE_CRITICAL",
                         "LIQ_DISTANCE_EMERGENCY",
                         "LIQUIDATION_POSSIBLE_UNCONFIRMED"):
                out.append(_change(plan_id, code, "CRITICAL" if "CRITICAL" in code or "EMERGENCY" in code or "POSSIBLE" in code else "WARN",
                                   "URGENT_PAIR_EXIT" if "EMERGENCY" in code or "POSSIBLE" in code else ("PAIR_EXIT" if "CRITICAL" in code else "REVIEW"),
                                   active=False, fresh=False,
                                   context={"liq_state": liq_state}))

    # -- hedge ratio drift (uses monitor ratio + target when available) ------
    actual = _opt_dec(_snap_field(current, "actual_hedge_ratio"))
    # Target is not on the snapshot; callers may stash it in metrics.
    target = _opt_dec(metrics.get("target_hedge_ratio",
                                  metrics.get("targetHedgeRatio")))
    drift_warn = Decimal(str(_num(ratio_cfg, "drift_warn_pct", 0.02)))
    drift_crit = Decimal(str(_num(ratio_cfg, "drift_critical_pct", 0.05)))
    ratio_fresh = not feeds_failed and actual is not None and target is not None
    orphan_hint = str(metrics.get("orphan_hint", "") or "")
    if orphan_hint in ("CRITICAL_ORPHAN_SPOT_LEG", "CRITICAL_ORPHAN_FUTURES_LEG"):
        code = ("ORPHAN_SPOT_LEG" if orphan_hint == "CRITICAL_ORPHAN_SPOT_LEG"
                else "ORPHAN_FUTURES_LEG")
        out.append(_change(plan_id, code, "CRITICAL", "URGENT_PAIR_EXIT",
                           active=True, fresh=not feeds_failed,
                           context={"orphan_hint": orphan_hint}))
        # Clear ticks for the sibling + drift codes.
        sibling = ("ORPHAN_FUTURES_LEG" if code == "ORPHAN_SPOT_LEG"
                   else "ORPHAN_SPOT_LEG")
        out.append(_change(plan_id, sibling, "CRITICAL", "URGENT_PAIR_EXIT",
                           active=False, fresh=not feeds_failed,
                           context={"orphan_hint": orphan_hint}))
    elif orphan_hint == "ORPHAN_LEG_WARNING":
        out.append(_change(plan_id, "ORPHAN_SPOT_LEG", "WARN", "REVIEW",
                           active=True, fresh=not feeds_failed,
                           context={"orphan_hint": orphan_hint}))
    if actual is not None and target is not None and not orphan_hint.startswith("CRITICAL"):
        with localcontext() as ctx:
            ctx.prec = 80
            try:
                drift = abs(actual - target)
            except (InvalidOperation, ValueError, ArithmeticError):
                drift = None
        if drift is not None:
            metrics_ctx = {"actual": str(actual), "target": str(target),
                           "drift": str(drift)}
            if drift > drift_crit:
                out.append(_change(plan_id, "HEDGE_RATIO_CRITICAL", "CRITICAL",
                                   "PAIR_EXIT", active=True, fresh=not feeds_failed,
                                   context=metrics_ctx))
                out.append(_change(plan_id, "HEDGE_RATIO_DRIFT", "WARN",
                                   "REVIEW", active=False, fresh=not feeds_failed,
                                   context=metrics_ctx))
            elif drift > drift_warn:
                out.append(_change(plan_id, "HEDGE_RATIO_DRIFT", "WARN",
                                   "REVIEW", active=True, fresh=not feeds_failed,
                                   context=metrics_ctx))
                out.append(_change(plan_id, "HEDGE_RATIO_CRITICAL", "CRITICAL",
                                   "PAIR_EXIT", active=False, fresh=not feeds_failed,
                                   context=metrics_ctx))
            else:
                out.append(_change(plan_id, "HEDGE_RATIO_DRIFT", "WARN",
                                   "REVIEW", active=False, fresh=not feeds_failed,
                                   context=metrics_ctx))
                out.append(_change(plan_id, "HEDGE_RATIO_CRITICAL", "CRITICAL",
                                   "PAIR_EXIT", active=False, fresh=not feeds_failed,
                                   context=metrics_ctx))
    elif not ratio_fresh:
        out.append(_change(plan_id, "HEDGE_RATIO_DRIFT", "WARN", "REVIEW",
                           active=False, fresh=False, context={}))
        out.append(_change(plan_id, "HEDGE_RATIO_CRITICAL", "CRITICAL",
                           "PAIR_EXIT", active=False, fresh=False, context={}))

    # -- basis consuming carry (B23; zero-accrued uses fixed thresholds) ----
    adverse = _opt_dec(metrics.get("adverse_basis_loss_usd"))
    funding_total = _opt_dec(_snap_field(current, "estimated_settled_funding_usd"))
    funding_known = _opt_dec(metrics.get("funding_known_subtotal_usd"))
    funding_complete = bool(metrics.get("funding_complete", False))
    entry_notional = _opt_dec(metrics.get("futures_entry_notional_usd",
                                          metrics.get("entry_notional_usd")))
    warn_vs = Decimal(str(_num(basis_cfg, "warning_vs_accrued_funding", 0.5)))
    exit_vs = Decimal(str(_num(basis_cfg, "exit_vs_accrued_funding", 1.0)))
    warn_min = Decimal(str(_num(basis_cfg, "warning_min_usd", 10)))
    warn_ratio = Decimal(str(_num(basis_cfg, "warning_min_notional_ratio", 0.001)))
    exit_min = Decimal(str(_num(basis_cfg, "exit_min_usd", 20)))
    exit_ratio = Decimal(str(_num(basis_cfg, "exit_min_notional_ratio", 0.002)))
    basis_fresh = (not feeds_failed
                   and adverse is not None
                   and _snap_field(current, "basis_pnl_usd") is not None)
    if adverse is not None and adverse > 0:
        with localcontext() as ctx:
            ctx.prec = 80
            warn_thr = warn_min
            exit_thr = exit_min
            if entry_notional is not None:
                warn_thr = max(warn_thr, entry_notional * warn_ratio)
                exit_thr = max(exit_thr, entry_notional * exit_ratio)
            # Accrued component only when the complete total is known;
            # zero accrued still uses the fixed thresholds above.
            if funding_complete and funding_total is not None:
                base = funding_total if funding_total > 0 else Decimal(0)
                warn_thr = max(warn_thr, base * warn_vs)
                exit_thr = max(exit_thr, base * exit_vs)
                carry_note = "CARRY_KNOWN"
            else:
                carry_note = "CARRY_COMPARISON_UNAVAILABLE"
            ctx_map = {"adverse_loss": str(adverse),
                       "warn_threshold": str(warn_thr),
                       "exit_threshold": str(exit_thr),
                       "carry_note": carry_note}
            if adverse >= exit_thr:
                out.append(_change(plan_id, "BASIS_CONSUMED_CARRY", "CRITICAL",
                                   "PAIR_EXIT", active=True, fresh=basis_fresh,
                                   context=ctx_map))
                out.append(_change(plan_id, "BASIS_CONSUMING_CARRY", "WARN",
                                   "REVIEW", active=False, fresh=basis_fresh,
                                   context=ctx_map))
            elif adverse >= warn_thr:
                out.append(_change(plan_id, "BASIS_CONSUMING_CARRY", "WARN",
                                   "REVIEW", active=True, fresh=basis_fresh,
                                   context=ctx_map))
                out.append(_change(plan_id, "BASIS_CONSUMED_CARRY", "CRITICAL",
                                   "PAIR_EXIT", active=False, fresh=basis_fresh,
                                   context=ctx_map))
            else:
                out.append(_change(plan_id, "BASIS_CONSUMING_CARRY", "WARN",
                                   "REVIEW", active=False, fresh=basis_fresh,
                                   context=ctx_map))
                out.append(_change(plan_id, "BASIS_CONSUMED_CARRY", "CRITICAL",
                                   "PAIR_EXIT", active=False, fresh=basis_fresh,
                                   context=ctx_map))
    else:
        fresh_flag = (not feeds_failed
                      and _snap_field(current, "basis_pnl_usd") is not None)
        out.append(_change(plan_id, "BASIS_CONSUMING_CARRY", "WARN", "REVIEW",
                           active=False, fresh=fresh_flag, context={}))
        out.append(_change(plan_id, "BASIS_CONSUMED_CARRY", "CRITICAL",
                           "PAIR_EXIT", active=False, fresh=fresh_flag,
                           context={}))

    # -- funding direction (current/projected are display-only) -------------
    current_rate = _opt_dec(metrics.get("funding_current_rate",
                                        metrics.get("current_rate")))
    if current_rate is not None:
        if current_rate <= 0:
            out.append(_change(plan_id, "FUNDING_TURNED_NON_POSITIVE", "WARN",
                               "REVIEW", active=True, fresh=not feeds_failed,
                               context={"rate": str(current_rate)}))
        else:
            out.append(_change(plan_id, "FUNDING_TURNED_NON_POSITIVE", "WARN",
                               "REVIEW", active=False, fresh=not feeds_failed,
                               context={"rate": str(current_rate)}))
    # Negative streak over settled rates when the caller provides them.
    streak = metrics.get("funding_recent_rates")
    if isinstance(streak, (list, tuple)) and streak:
        try:
            negatives = sum(1 for r in streak if Decimal(str(r)) < 0)
            if negatives >= 3:
                out.append(_change(plan_id, "FUNDING_NEGATIVE_STREAK",
                                   "CRITICAL", "REVIEW", active=True,
                                   fresh=not feeds_failed,
                                   context={"streak": negatives}))
            else:
                out.append(_change(plan_id, "FUNDING_NEGATIVE_STREAK",
                                   "CRITICAL", "REVIEW", active=False,
                                   fresh=not feeds_failed,
                                   context={"streak": negatives}))
        except (InvalidOperation, ValueError, ArithmeticError):
            pass
    _ = prev_metrics

    # -- spot exit liquidity (B24) -------------------------------------------
    exit_liq = metrics.get("exit_liquidity", {})
    covers = exit_liq.get("covers_remaining") if isinstance(exit_liq, Mapping) else None
    slip = exit_liq.get("sell_slippage_bps") if isinstance(exit_liq, Mapping) else None
    try:
        slip_f = float(slip) if slip is not None else None
    except (TypeError, ValueError):
        slip_f = None
    max_slip_cfg = 100
    try:
        if isinstance(exit_cfg, Mapping):
            max_slip_cfg = int(exit_cfg.get("max_exit_slippage_bps", 100))
    except (TypeError, ValueError):
        max_slip_cfg = 100
    if covers is False:
        out.append(_change(plan_id, "SPOT_EXIT_CAPACITY", "CRITICAL",
                           "PAIR_EXIT", active=True, fresh=not feeds_failed,
                           context={"covers_remaining": False}))
    elif covers is True:
        out.append(_change(plan_id, "SPOT_EXIT_CAPACITY", "CRITICAL",
                           "PAIR_EXIT", active=False, fresh=not feeds_failed,
                           context={"covers_remaining": True}))
    if slip_f is not None and slip_f > max_slip_cfg:
        out.append(_change(plan_id, "SPOT_EXIT_SLIPPAGE", "WARN", "REVIEW",
                           active=True, fresh=not feeds_failed,
                           context={"slippage_bps": slip_f}))
    elif slip_f is not None:
        out.append(_change(plan_id, "SPOT_EXIT_SLIPPAGE", "WARN", "REVIEW",
                           active=False, fresh=not feeds_failed,
                           context={"slippage_bps": slip_f}))

    # -- stale / degraded signals (never wipe positions) ----------------------
    if bool(metrics.get("futures_mark_stale", False)) or bool(
            metrics.get("spot_quote_stale", False)):
        out.append(_change(plan_id, "QUOTE_STALE", "WARN", "REVIEW",
                           active=True, fresh=True,
                           context={"stale": True}))
    else:
        out.append(_change(plan_id, "QUOTE_STALE", "WARN", "REVIEW",
                           active=False, fresh=not feeds_failed,
                           context={"stale": False}))
    if lag_degraded or "PERSISTENCE_LAG" in degraded:
        out.append(_change(plan_id, "PERSISTENCE_LAG", "WARN", "REVIEW",
                           active=True, fresh=True,
                           context={"persist_lag_ms": lag_ms,
                                    "note": "degraded only; positions kept"}))
    else:
        out.append(_change(plan_id, "PERSISTENCE_LAG", "WARN", "REVIEW",
                           active=False, fresh=True,
                           context={"persist_lag_ms": lag_ms}))
    _ = liq_cfg
    return tuple(out)


class AlertManager:
    """In-memory alert lifecycle (dedup + episode + ACK + 2-tick recovery).

    Mirrors the H01 worker contract: the same condition while OPEN or
    ACKNOWLEDGED only bumps ``last_seen``/context (no new episode, no
    repeat notification after ACK); a re-fire after RESOLVED opens
    ``episode + 1``. Recovery needs :data:`RECOVERY_TICKS` consecutive
    fresh clear ticks; stale/failed ticks (``fresh=False``) never resolve.
    ``ack()`` moves OPEN to ACKNOWLEDGED; failed fetches never resolve;
    DB-lag degraded never drops real positions or critical alerts.
    """

    def __init__(self, policy: Any = None) -> None:
        self._policy = policy
        # dedup_key -> record dict
        self._alerts: dict[str, dict[str, Any]] = {}

    def _record(self, key: str) -> dict[str, Any] | None:
        return self._alerts.get(key)

    def open_alerts(self) -> tuple[dict[str, Any], ...]:
        """Return OPEN + ACKNOWLEDGED records (the unsolved set)."""
        return tuple(r for r in self._alerts.values()
                     if r["state"] in ("OPEN", "ACKNOWLEDGED"))

    def all_alerts(self) -> tuple[dict[str, Any], ...]:
        """Return every tracked episode (including RESOLVED)."""
        return tuple(self._alerts.values())

    def ack(self, dedup: str, now_ms: int) -> dict[str, Any] | None:
        """Acknowledge an OPEN alert (ACK never re-notifies on re-fire)."""
        record = self._alerts.get(dedup)
        if record is None or record["state"] != "OPEN":
            return record
        record["state"] = "ACKNOWLEDGED"
        record["acknowledged_at_ms"] = int(now_ms)
        record["last_seen_at_ms"] = int(now_ms)
        return record

    def apply(
        self, changes: Any, now_ms: int,
    ) -> tuple[dict[str, Any], ...]:
        """Apply evaluated changes, returning per-episode dispositions.

        Each disposition holds ``dedup_key/episode/state/severity/
        recommended_action/code/notify/db_op``. ``notify=True`` only for a
        brand-new OPEN episode or a fresh RESOLVED transition -- never for
        a repeated tick of the same OPEN/ACK episode. ``db_op`` is the
        H01-equivalent worker op (``upsert``/``resolve``/``noop``) so the
        H08 queue can persist latest-only without losing criticals.
        """
        now = int(now_ms)
        seq = list(changes) if changes is not None else []
        seen: set[str] = set()
        dispositions: list[dict[str, Any]] = []
        for raw in seq:
            if isinstance(raw, Mapping):
                code = str(raw.get("code", ""))
                plan_id = str(raw.get("plan_id", ""))
                leg = str(raw.get("leg_or_venue", raw.get("legOrVenue", "")))
                key = str(raw.get("dedup_key")
                          or dedup_key(plan_id, code, leg))
                severity = str(raw.get("severity", "WARN"))
                action = str(raw.get("recommended_action",
                                     raw.get("recommendedAction", "REVIEW")))
                active = bool(raw.get("active", False))
                fresh = bool(raw.get("fresh", True))
                context = dict(raw.get("context", {}) or {})
            else:
                continue
            if not code or not plan_id:
                continue
            if severity not in _SEVERITIES or action not in _ACTIONS:
                raise ValueError(f"bad severity/action {severity!r}/{action!r}")
            seen.add(key)
            record = self._alerts.get(key)
            if active and fresh:
                if record is None or record["state"] == "RESOLVED":
                    episode = int(record["episode"] + 1) if record else 1
                    self._alerts[key] = {
                        "dedup_key": key, "plan_id": plan_id, "code": code,
                        "severity": severity, "recommended_action": action,
                        "leg_or_venue": leg,
                        "state": "OPEN", "episode": episode,
                        "opened_at_ms": now, "last_seen_at_ms": now,
                        "acknowledged_at_ms": None, "resolved_at_ms": None,
                        "clear_ticks": 0, "context": context,
                    }
                    dispositions.append({
                        "dedup_key": key, "episode": episode,
                        "state": "OPEN", "severity": severity,
                        "recommended_action": action, "code": code,
                        "notify": True, "db_op": "upsert",
                    })
                else:
                    # Same condition still firing: bump last_seen/context
                    # only; ACK never re-notifies.
                    record["last_seen_at_ms"] = now
                    record["context"] = context
                    record["clear_ticks"] = 0
                    dispositions.append({
                        "dedup_key": key, "episode": record["episode"],
                        "state": record["state"], "severity": record["severity"],
                        "recommended_action": record["recommended_action"],
                        "code": code, "notify": False, "db_op": "upsert",
                    })
            elif not active and fresh:
                if record is None or record["state"] == "RESOLVED":
                    dispositions.append({
                        "dedup_key": key, "episode": record["episode"] if record else 0,
                        "state": record["state"] if record else "RESOLVED",
                        "severity": severity, "recommended_action": action,
                        "code": code, "notify": False, "db_op": "noop",
                    })
                else:
                    record["clear_ticks"] = int(record.get("clear_ticks", 0)) + 1
                    if record["clear_ticks"] >= RECOVERY_TICKS:
                        record["state"] = "RESOLVED"
                        record["resolved_at_ms"] = now
                        record["last_seen_at_ms"] = now
                        dispositions.append({
                            "dedup_key": key, "episode": record["episode"],
                            "state": "RESOLVED", "severity": record["severity"],
                            "recommended_action": record["recommended_action"],
                            "code": code, "notify": True, "db_op": "resolve",
                        })
                    else:
                        record["last_seen_at_ms"] = now
                        dispositions.append({
                            "dedup_key": key, "episode": record["episode"],
                            "state": record["state"],
                            "severity": record["severity"],
                            "recommended_action": record["recommended_action"],
                            "code": code, "notify": False, "db_op": "noop",
                        })
            else:
                # Stale/failed tick: never resolve, reset the clear count
                # so a single success cannot fake recovery.
                if record is not None and record["state"] in ("OPEN", "ACKNOWLEDGED"):
                    record["clear_ticks"] = 0
                    record["last_seen_at_ms"] = now
                dispositions.append({
                    "dedup_key": key,
                    "episode": record["episode"] if record else 0,
                    "state": record["state"] if record else "UNKNOWN",
                    "severity": severity, "recommended_action": action,
                    "code": code, "notify": False, "db_op": "noop",
                })
        # Conditions absent from this evaluation carry no information and
        # must not resolve or create episodes.
        _ = seen
        return tuple(dispositions)
