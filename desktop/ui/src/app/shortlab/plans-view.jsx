/* ============================================================================
   R13a — PlansView (pure display, D12.1).
   Renders only real props, never fabricates from localStorage, no HTTP.
   DRAFT stays distinct from ACTIVE; view/monitor are callbacks only.
   ========================================================================== */

function PlansView(props) {
  const p = props || {};
  const t = (k, fb) => {
    try { const v = (typeof L === "function") ? L(k) : null; return v || fb || k; }
    catch (e) { return fb || k; }
  };
  let plans = null;
  let total = null;
  if (Array.isArray(p.plans)) { plans = p.plans; total = (p.total != null ? p.total : plans.length); }
  else if (p.initial && Array.isArray(p.initial.plans)) { plans = p.initial.plans; total = (p.initial.total != null ? p.initial.total : plans.length); }
  else if (p.initial && p.initial.data && Array.isArray(p.initial.data.items)) { plans = p.initial.data.items; total = (p.initial.data.total != null ? p.initial.data.total : plans.length); }
  else if (p.initial && p.initial.data && Array.isArray(p.initial.data.plans)) { plans = p.initial.data.plans; total = plans.length; }
  else if (Array.isArray(p.items)) { plans = p.items; total = (p.total != null ? p.total : plans.length); }
  else { plans = []; total = 0; }

  const onView = p.onView || p.onViewPlan || null;
  const onMonitor = p.onMonitor || p.onGotoMonitor || null;
  const err = p.error || (p.initial && p.initial.error) || null;
  const loading = !!p.loading;

  if (loading) {
    return <div className="state" style={{ height: 120 }} data-testid="plans-loading"><div className="spin" /></div>;
  }
  if (err && plans.length === 0) {
    return (
      <div className="state src-down" role="alert" data-testid="plans-error">
        <div className="sd-title">PLANS {t("repair_plans_title", "计划列表")}</div>
        <div className="sd-err">{String(err).slice(0, 300)}</div>
      </div>
    );
  }

  return (
    <div data-testid="plans-view">
      <div className="vhead"><span className="kicker">{t("repair_plans_kicker", "PLANS")}</span>
        <h1>{t("repair_plans_title", "计划列表")}</h1>
        <div className="meta">{total != null ? String(total) : "0"}</div></div>
      <div className="gcap" data-testid="plans-note">{t("repair_plans_note", "只渲染真实数据")}</div>
      {plans.length === 0
        ? <div className="reason" data-testid="plans-empty">{t("repair_plans_empty", "暂无计划")}</div>
        : (
          <div className="sl-scroll">
            <table className="rank sl-table" data-testid="plans-table">
              <thead><tr>
                <th scope="col">PLAN</th>
                <th scope="col">SYMBOL</th>
                <th scope="col">STATUS</th>
                <th scope="col">ACTIONS</th>
              </tr></thead>
              <tbody>
                {plans.map((pl, i) => {
                  const pid = (pl && (pl.planId != null ? pl.planId : pl.plan_id)) || `plan-${i}`;
                  const sym = (pl && (pl.symbol || pl.Symbol)) || "—";
                  const status = (pl && (pl.status || pl.Status)) || "—";
                  const ver = (pl && (pl.planVersion != null ? pl.planVersion : pl.plan_version)) || "—";
                  return (
                    <tr key={String(pid)} data-testid={`plans-row-${String(pid)}`}>
                      <td>{String(pid)}<small> v{String(ver)}</small></td>
                      <td>{String(sym)}</td>
                      <td>
                        <span className={"pill " + (String(status).toUpperCase() === "DRAFT" ? "n" : "b")}>
                          <span className="g" />{String(status)}
                        </span>
                        {String(status).toUpperCase() === "DRAFT" && (
                          <div className="sl-reasons">{t("repair_plans_status_draft", "DRAFT草稿")}</div>
                        )}
                      </td>
                      <td>
                        <button className="chip" data-testid={`plans-view-${String(pid)}`}
                          onClick={() => { if (typeof onView === "function") onView(pl); else { try { location.hash = "#/shortlab/monitor"; } catch (e) {} } }}>
                          {t("repair_plans_view", "查看计划")}
                        </button>
                        <button className="chip" data-testid={`plans-monitor-${String(pid)}`}
                          onClick={() => { if (typeof onMonitor === "function") onMonitor(pl); else { try { location.hash = "#/shortlab/monitor"; } catch (e) {} } }}>
                          {t("repair_plans_monitor", "进入监控")}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
    </div>
  );
}
