/* ============================================================================
   R13a — DecisionPanel (pure display / status model, D07/D12.1).
   Props/callbacks only, no HTTP, no R12 symbols, no localStorage.
   Consumes R00 DTO + D18.3 shape (camel or snake), real wiring is R13b.
   - goal/hold/budget/native liq/rules +淘汰原因 visible
   - h0 read-only guidance, no save button; h>0 can simulate/save DRAFT
   - canSavePairedPlan: only PARTIAL_HEDGE/FULL_HEDGE true
   - inputs change => old decision stale, save disabled
   - capability missing/UNKNOWN => config explanation, no paired save
   ========================================================================== */

function canSavePairedPlan(decision) {
  if (!decision || typeof decision !== "object") return false;
  const r = decision.recommendation || decision.recommendationCode || decision.rec;
  return r === "PARTIAL_HEDGE" || r === "FULL_HEDGE";
}

function getDecisionActualRatio(decision) {
  if (!decision || typeof decision !== "object") return null;
  const p = decision.selectedProposal != null ? decision.selectedProposal : decision.selected_proposal;
  if (!p || typeof p !== "object") return null;
  const raw = (p.actual_ratio != null ? p.actual_ratio : p.actualRatio);
  if (raw == null || raw === "") return null;
  return raw;
}

function isDecisionExpired(decision, nowMs) {
  if (!decision || typeof decision !== "object") return false;
  if (decision.expired === true) return true;
  const cands = [decision.expiresAtMs, decision.expires_at_ms, decision.expiresAt, decision.expires_at];
  let exp = null;
  for (const c of cands) {
    if (c != null && c !== "" && isFinite(Number(c))) { exp = Number(c); break; }
  }
  if (exp == null) return false;
  const now = (nowMs != null && isFinite(Number(nowMs))) ? Number(nowMs) : Date.now();
  return now >= exp;
}

function _reqField(obj, camel, snake) {
  if (!obj || typeof obj !== "object") return undefined;
  if (obj[camel] !== undefined) return obj[camel];
  if (obj[snake] !== undefined) return obj[snake];
  return undefined;
}

function isDecisionStaleForInputs(decision, currentInputs) {
  if (!decision || typeof decision !== "object") return false;
  if (!currentInputs || typeof currentInputs !== "object") return false;
  const req = decision.request || decision.decisionRequest || decision.decision_request || {};
  const keys = [
    ["symbol", "symbol"],
    ["goal", "goal"],
    ["futuresNotionalUsd", "futures_notional_usd"],
    ["plannedHoldDays", "planned_hold_days"],
    ["availableCapitalUsd", "available_capital_usd"],
    ["maxScenarioLossUsd", "max_scenario_loss_usd"],
    ["marginUsd", "margin_usd"],
    ["liquidationPrice", "liquidation_price"],
    ["preferredSpotVenue", "preferred_spot_venue"],
  ];
  for (const [camel, snake] of keys) {
    const a = _reqField(req, camel, snake);
    const b = _reqField(currentInputs, camel, snake);
    const sa = (a == null ? "" : String(a).trim());
    const sb = (b == null ? "" : String(b).trim());
    if (camel === "symbol" || camel === "goal" || camel === "preferredSpotVenue") {
      if (sa.toUpperCase() !== sb.toUpperCase()) return true;
    } else {
      if (sa !== sb) return true;
    }
  }
  return false;
}

function isDecisionCapabilityBlocked(capability) {
  if (!capability || typeof capability !== "object") return false;
  if (capability.hedgeEnabled === false) return true;
  if (capability.hedge_enabled === false) return true;
  if (capability.enabled === false) return true;
  if (capability.available === false) return true;
  const mc = capability.monitoringCapability != null
    ? capability.monitoringCapability
    : capability.monitoring_capability;
  if (mc != null && String(mc).toUpperCase() === "UNKNOWN") return true;
  const cap2 = capability.capability != null ? capability.capability : capability.status;
  if (cap2 != null && String(cap2).toUpperCase() === "UNKNOWN") return true;
  return false;
}

function repairGoalLabel(goal) {
  const g = String(goal || "").toUpperCase();
  try {
    if (g === "CARRY_CAPTURE" && typeof L === "function") return L("repair_goal_carry") || g;
    if (g === "DIRECTIONAL_SHORT" && typeof L === "function") return L("repair_goal_directional") || g;
    if (g === "BALANCED" && typeof L === "function") return L("repair_goal_balanced") || g;
  } catch (e) {}
  return goal || "—";
}

function repairRecLabel(rec) {
  const r = String(rec || "").toUpperCase();
  try {
    if (typeof L !== "function") return rec || "—";
    if (r === "DATA_INSUFFICIENT") return L("repair_rec_data_insufficient");
    if (r === "MANUAL_REVIEW") return L("repair_rec_manual_review");
    if (r === "AVOID") return L("repair_rec_avoid");
    if (r === "NO_HEDGE") return L("repair_rec_no_hedge");
    if (r === "PARTIAL_HEDGE") return L("repair_rec_partial_hedge");
    if (r === "FULL_HEDGE") return L("repair_rec_full_hedge");
  } catch (e) {}
  return rec || "—";
}

function repairReasonLabel(reason) {
  const c = String(reason || "").toUpperCase();
  try {
    if (typeof L !== "function") return String(reason || "");
    if (c === "FUNDING_SCHEDULE_UNKNOWN") return L("repair_reason_funding_schedule_unknown");
    if (c === "FUNDING_CURRENT_NON_POSITIVE") return L("repair_reason_funding_current_non_positive");
    if (c === "FUNDING_LAST_NON_POSITIVE") return L("repair_reason_funding_last_non_positive");
  } catch (e) {}
  return String(reason || "");
}

function DecisionPanel(props) {
  const p = props || {};
  const decision = p.decision || p.initialDecision || null;
  const currentInputs = p.currentInputs || p.currentRequest || null;
  const capability = p.capability || p.capabilities || null;
  const nowMs = p.nowMs != null ? p.nowMs : null;
  const onSimulate = p.onSimulate;
  const onSave = p.onSave;
  const onViewPlan = p.onViewPlan;
  const onGotoMonitor = p.onGotoMonitor;

  const t = (k, fb) => {
    try { const v = (typeof L === "function") ? L(k) : null; return v || fb || k; }
    catch (e) { return fb || k; }
  };

  if (!decision || typeof decision !== "object") {
    return (
      <div data-testid="decision-panel">
        <div className="vhead"><span className="kicker">{t("repair_decision_kicker", "DECISION")}</span>
          <h1>{t("repair_decision_title", "Decision")}</h1></div>
        <div className="reason" data-testid="decision-empty">{t("repair_decision_empty", "暂无决策快照")}</div>
        <div className="gcap" data-testid="decision-no-probability">{t("repair_decision_no_probability", "仅展示规则与淘汰原因")}</div>
      </div>
    );
  }

  const req = decision.request || {};
  const symbol = _reqField(req, "symbol", "symbol") || "—";
  const goal = _reqField(req, "goal", "goal") || "—";
  const hold = _reqField(req, "plannedHoldDays", "planned_hold_days");
  const notional = _reqField(req, "futuresNotionalUsd", "futures_notional_usd");
  const capital = _reqField(req, "availableCapitalUsd", "available_capital_usd");
  const maxLoss = _reqField(req, "maxScenarioLossUsd", "max_scenario_loss_usd");
  const margin = _reqField(req, "marginUsd", "margin_usd");
  const liq = _reqField(req, "liquidationPrice", "liquidation_price");
  const venue = _reqField(req, "preferredSpotVenue", "preferred_spot_venue") || "AUTO";

  const rec = decision.recommendation || decision.recommendationCode || "—";
  const reasons = Array.isArray(decision.reasons) ? decision.reasons : [];
  const assumptions = Array.isArray(decision.assumptions) ? decision.assumptions : [];
  const proposal = (decision.selectedProposal != null ? decision.selectedProposal : decision.selected_proposal) || null;
  const alternatives = Array.isArray(decision.alternatives) ? decision.alternatives : [];
  const decisionId = decision.decisionId != null ? decision.decisionId : decision.decision_id;
  const expired = isDecisionExpired(decision, nowMs != null ? nowMs : Date.now());
  const stale = isDecisionStaleForInputs(decision, currentInputs);
  const capBlocked = isDecisionCapabilityBlocked(capability);
  const capUnknown = (() => {
    if (!capability || typeof capability !== "object") return false;
    const mc = capability.monitoringCapability != null ? capability.monitoringCapability : capability.monitoring_capability;
    return String(mc || "").toUpperCase() === "UNKNOWN" || String(capability.status || "").toUpperCase() === "UNKNOWN";
  })();

  const actualRaw = getDecisionActualRatio(decision);
  let actualNum = null;
  try { if (actualRaw != null && actualRaw !== "") { const n = Number(actualRaw); if (isFinite(n)) actualNum = n; } } catch (e) {}
  const isH0 = (proposal == null) || (actualNum != null && actualNum === 0) || (String(rec).toUpperCase() === "NO_HEDGE");
  const savable = canSavePairedPlan(decision);
  const showSimulate = !capBlocked;
  const showSave = showSimulate && savable && !isH0 && !expired && !stale;

  const targetRaw = proposal ? (_reqField(proposal, "targetRatio", "target_ratio")) : null;
  const futQty = proposal ? (proposal.futures_contract_qty != null ? proposal.futures_contract_qty : (proposal.canonical_futures_qty != null ? proposal.canonical_futures_qty : (proposal.canonicalFuturesQty || "—"))) : null;
  const spotQty = proposal ? (_reqField(proposal, "spotNetQty", "spot_net_qty") || proposal.spot_net_qty) : null;
  const spotVenue = proposal ? (proposal.spot_venue || proposal.spotVenue || venue) : venue;

  return (
    <div data-testid="decision-panel">
      <div className="vhead"><span className="kicker">{t("repair_decision_kicker", "DECISION")}</span>
        <h1>{t("repair_decision_title", "Decision")}</h1>
        <div className="meta">{decisionId ? String(decisionId).slice(0, 24) : ""} · {repairRecLabel(rec)}</div></div>

      <div className="panel"><div className="ph"><span className="tick">▸</span>{t("repair_decision_title", "Decision")}</div>
        <div className="pb" style={{ padding: 0 }}>
          <div className="stat"><span className="k">symbol</span>
            <span className="v" data-testid="decision-symbol">{String(symbol)}</span></div>
          <div className="stat"><span className="k">{t("repair_decision_goal", "Goal")}</span>
            <span className="v" data-testid="decision-goal">{repairGoalLabel(goal)} · {String(goal)}</span></div>
          <div className="stat"><span className="k">{t("repair_decision_hold_days", "Hold")}</span>
            <span className="v" data-testid="decision-hold">{hold != null && hold !== "" ? String(hold) : "—"}</span></div>
          <div className="stat"><span className="k">{t("repair_decision_capital", "Capital")}</span>
            <span className="v" data-testid="decision-budget">{capital != null && capital !== "" ? String(capital) : (notional != null ? String(notional) : "—")}</span></div>
          <div className="stat"><span className="k">{t("repair_decision_max_loss", "Max loss")}</span>
            <span className="v">{maxLoss != null && maxLoss !== "" ? String(maxLoss) : "—"}</span></div>
          <div className="stat"><span className="k">{t("repair_decision_margin", "Margin")}</span>
            <span className="v">{margin != null && margin !== "" ? String(margin) : "—"}</span></div>
          <div className="stat"><span className="k">{t("repair_decision_liq", "Liq")}</span>
            <span className="v" data-testid="decision-liq">{liq != null && liq !== "" ? String(liq) : "—"}</span></div>
          <div className="stat"><span className="k">{t("repair_decision_venue", "Venue")}</span>
            <span className="v">{String(spotVenue || "—")}</span></div>
          <div className="stat"><span className="k">{t("repair_decision_reasons", "Reasons")}</span>
            <span className="v" data-testid="decision-reasons">{reasons.length > 0 ? reasons.map((r) => repairReasonLabel(r)).join(" · ") : "—"}</span></div>
          {assumptions.length > 0 && (
            <div className="stat"><span className="k">{t("repair_decision_assumptions", "Assumptions")}</span>
              <span className="v">{assumptions.join(" · ")}</span></div>
          )}
          {proposal && (
            <div className="stat"><span className="k">h</span>
              <span className="v" data-testid="decision-h">{actualRaw != null ? String(actualRaw) : "—"} · target {targetRaw != null ? String(targetRaw) : "—"} · fut {futQty != null ? String(futQty) : "—"} · spot {spotQty != null ? String(spotQty) : "—"}</span></div>
          )}
          {alternatives.length > 0 && (
            <div className="stat"><span className="k">alternatives</span>
              <span className="v">{String(alternatives.length)}</span></div>
          )}
        </div></div>

      <div className="gcap" data-testid="decision-no-probability">{t("repair_decision_no_probability", "仅展示规则与淘汰原因")}</div>

      {isH0 && (
        <div className="panel" data-testid="decision-h0-guide">
          <div className="ph"><span className="tick">▸</span>h0</div>
          <div className="pb"><div className="reason">{t("repair_decision_h0_guide", "h0只读")}</div></div>
        </div>
      )}

      {String(rec).toUpperCase() === "NO_HEDGE" && (
        <div className="reason" data-testid="decision-no-hedge">{t("repair_decision_no_hedge_guide", "NO_HEDGE只读")}</div>
      )}
      {String(rec).toUpperCase() === "DATA_INSUFFICIENT" && (
        <div className="reason" data-testid="decision-insufficient">{t("repair_decision_insufficient_guide", "数据不足")}</div>
      )}
      {String(rec).toUpperCase() === "MANUAL_REVIEW" && (
        <div className="reason" data-testid="decision-manual">{t("repair_decision_manual_guide", "人工复核")}</div>
      )}
      {String(rec).toUpperCase() === "AVOID" && (
        <div className="reason" data-testid="decision-avoid">{t("repair_decision_avoid_guide", "建议回避")}</div>
      )}

      {stale && (
        <div className="provline" data-testid="decision-stale-inputs" role="alert">
          <span className="tag hot">STALE</span>
          <span>{t("repair_decision_stale_inputs", "输入已变")}</span>
        </div>
      )}
      {expired && (
        <div className="provline" data-testid="decision-expired" role="alert">
          <span className="tag hot">EXPIRED</span>
          <span>{t("repair_decision_expired", "已过期")}</span>
        </div>
      )}
      {(capBlocked || capUnknown) && (
        <div className="state src-down" role="alert" data-testid={capUnknown && !capBlocked ? "decision-unknown" : "decision-capability"}>
          <div className="sd-title">CAPABILITY</div>
          <div className="sd-body">{t("repair_decision_capability_unavailable", "能力不可用")}{capability && capability.reason ? ` · ${String(capability.reason)}` : ""}</div>
          <div className="sd-body">{t("repair_decision_unknown_no_action", "未知不推荐")}</div>
        </div>
      )}

      <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
        {showSimulate && (
          <button className="cta" data-testid="decision-simulate" onClick={() => { if (typeof onSimulate === "function") onSimulate(decision); }}>
            {t("repair_decision_simulate_btn", "模拟")}
          </button>
        )}
        {showSave && (
          <button className="cta" data-testid="decision-save" onClick={() => { if (typeof onSave === "function") onSave(decision); }}>
            {t("repair_decision_save_btn", "保存DRAFT")}
          </button>
        )}
        {typeof onViewPlan === "function" && (
          <button className="chip" data-testid="decision-view-plan" onClick={() => onViewPlan(decision)}>
            {t("repair_decision_view_plan", "查看计划")}
          </button>
        )}
        {typeof onGotoMonitor === "function" && (
          <button className="chip" data-testid="decision-goto-monitor" onClick={() => onGotoMonitor(decision)}>
            {t("repair_decision_goto_monitor", "进入监控")}
          </button>
        )}
      </div>
    </div>
  );
}

if (typeof window !== "undefined") {
  try {
    window.canSavePairedPlan = canSavePairedPlan;
    window.isDecisionExpired = isDecisionExpired;
    window.isDecisionStaleForInputs = isDecisionStaleForInputs;
    window.getDecisionActualRatio = getDecisionActualRatio;
    window.isDecisionCapabilityBlocked = isDecisionCapabilityBlocked;
  } catch (e) {}
}
