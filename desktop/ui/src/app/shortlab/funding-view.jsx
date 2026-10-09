/* ============================================================================
   short-lab — Desktop · Funding opportunities page (H09 · design B32.1/B34.1)

   Reads ONLY window.DIVE.fundingOpportunities(filters) (data.js) — B32.1
   GET /api/short/funding-opportunities. Never fetches Binance/Alpha/on-chain
   directly. Table columns mirror B34.1 exactly:
     Token | FCS | Funding 7D | 30D | 90D | Positive 30D | Best Venue |
     Cost | BE Days | State
   Filters map 1:1 onto query snake aliases (min_fcs, min_funding_30d,
   min_positive_ratio_30d, venue, readiness, sort/order); percent conversion
   lives in short-lab-format.js only. Honesty: 503 renders UNAVAILABLE with
   the backend reason, never mock; AbortController + sequence rejects late
   responses; failed refetch keeps old body as STALE. `initial` bypasses fetch
   for deterministic tests ({data, error}).
   ========================================================================== */

const HEDGE_FUNDING_SORT_OPTS = ["", "fcs", "funding30d", "breakEvenDays", "positiveRatio30d"];
const HEDGE_FUNDING_VENUE_OPTS = ["", "BINANCE_SPOT", "BINANCE_ALPHA", "ONCHAIN_DEX"];
const HEDGE_FUNDING_READINESS_OPTS = ["", "READY", "NOT_READY", "BLOCKED"];

function HedgeFundingDefaults() {
  return {
    minFcs: "", minFundingPct: "", minPositivePct: "",
    venue: "", readiness: "", sort: "", order: "desc",
  };
}

function FundingView({ filters, initial, onAnalyze }) {
  const [local, setLocal] = React.useState(HedgeFundingDefaults());
  const [body, setBody] = React.useState(initial && initial.data ? initial.data : null);
  const [err, setErr] = React.useState(initial && initial.error ? initial.error : null);
  const [loading, setLoading] = React.useState(!(initial && (initial.data || initial.error)));
  const [formErr, setFormErr] = React.useState(null);
  const [keepErr, setKeepErr] = React.useState(null);
  const seqRef = React.useRef(0);
  const abortRef = React.useRef(null);

  const buildQuery = () => {
    const q = { limit: 50, offset: 0 };
    const f = { ...(filters || {}), ...local };
    if (f.minFcs !== "" && f.minFcs != null) {
      const n = Number(f.minFcs);
      if (!isFinite(n) || n < 0 || n > 100) return { error: L("hedge_err_min_fcs") };
      q.minFcs = n;
    }
    if (f.minFundingPct !== "" && f.minFundingPct != null) {
      const dec = (typeof slPctInputToDecimal === "function") ? slPctInputToDecimal(f.minFundingPct) : null;
      if (dec == null) return { error: L("hedge_err_min_funding") };
      q.minFunding30d = dec;
    }
    if (f.minPositivePct !== "" && f.minPositivePct != null) {
      const n = Number(f.minPositivePct);
      if (!isFinite(n) || n < 0 || n > 100) return { error: L("hedge_err_min_ratio") };
      q.minPositiveRatio30d = n / 100;
    }
    if (f.venue) q.venue = f.venue;
    if (f.readiness) q.readiness = f.readiness;
    if (f.sort) { q.sort = f.sort; q.order = f.order || "desc"; }
    /* R13a D12.1: research list explicitly includes stale rows. R13b wires the
       real HTTP param; here the intent is frozen for the adapter contract. */
    q.includeStale = true;
    q.include_stale = true;
    return { query: q };
  };

  const load = React.useCallback(() => {
    if (initial && (initial.data || initial.error)) return;
    const built = buildQuery();
    if (built.error) { setFormErr(built.error); return; }
    const seq = ++seqRef.current;
    if (abortRef.current) { try { abortRef.current.abort(); } catch (e) { /* superseded */ } }
    const ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
    abortRef.current = ctrl;
    setFormErr(null); setLoading(true);
    if (!body) setErr(null);
    const opts = ctrl ? { signal: ctrl.signal } : {};
    window.DIVE.fundingOpportunities(built.query, opts)
      .then((res) => {
        if (seq !== seqRef.current) return;
        setBody(res); setLoading(false); setErr(null); setKeepErr(null);
      })
      .catch((e) => {
        if (ctrl && ctrl.signal && ctrl.signal.aborted) return;
        if (seq !== seqRef.current) return;
        setLoading(false);
        const msg = (e && e.message) || String(e);
        if (body) setKeepErr(msg);
        else setErr(msg);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(local), JSON.stringify(filters || {}), body ? "has-body" : "no-body"]);

  React.useEffect(() => { load(); }, [load]);
  React.useEffect(() => () => { if (abortRef.current) { try { abortRef.current.abort(); } catch (e) {} } }, []);

  const setF = (k, v) => setLocal((f) => ({ ...f, [k]: v }));

  /* R13b D12.1 real routing: analyze carries symbol + snapshotId into Planner
     via workflow-bindings (formatPlannerHash) when present; planner refreshes
     real Gate/quotes on arrival (no stale reuse). Pure callback; fallback sets
     the planner hash so no HTTP happens here. */
  const handleAnalyze = (it) => {
    const sym = (it && (it.symbol || it.Symbol)) || "";
    const snap = (it && (it.snapshotId != null ? it.snapshotId : it.snapshot_id)) || (it && (it.fcsSnapshotId || it.fcs_snapshot_id)) || "";
    let wfHash = null;
    try {
      if (typeof formatPlannerHash === "function") wfHash = formatPlannerHash(sym, snap);
      else if (typeof window !== "undefined" && window.WORKFLOW_BINDINGS && typeof window.WORKFLOW_BINDINGS.formatPlannerHash === "function") wfHash = window.WORKFLOW_BINDINGS.formatPlannerHash(sym, snap);
    } catch (e) { wfHash = null; }
    if (typeof onAnalyze === "function") { try { onAnalyze({ symbol: sym, snapshotId: snap, item: it, hash: wfHash }); } catch (e) {} return; }
    try { location.hash = wfHash || ("#/shortlab/planner/" + encodeURIComponent(sym) + "/" + encodeURIComponent(snap)); } catch (e) {}
  };

  if (loading) {
    return <div className="state" style={{ height: 160 }} data-testid="funding-loading">
      <div className="spin" /><div>{L("hedge_funding_loading")}</div></div>;
  }
  if (err && !body) {
    const txt = String(err);
    const unavailable = /503|unavailable|HEDGE/i.test(txt);
    const offline = /Failed to fetch|NetworkError|network|offline|TypeError/i.test(txt);
    return (
      <div className="state src-down" role="alert"
        data-testid={unavailable ? "funding-unavailable" : "funding-error"}>
        <div className="sd-title">FUNDING {unavailable ? L("sl_unavailable_title") : L("sl_error_title")}</div>
        <div className="sd-body">{offline ? L("hedge_offline") : (unavailable ? L("hedge_funding_unavailable_body") : L("hedge_funding_error_body"))}</div>
        <div className="sd-err">{txt}</div>
        <button className="cta" onClick={load}>{L("sl_retry")}</button>
      </div>
    );
  }

  const items = ((body && (body.items || body.Items)) || []);
  const asOf = (body && (body.asOf != null ? body.asOf : body.as_of)) || null;

  return (
    <div data-testid="funding-view">
      <div className="vhead"><span className="kicker">{L("hedge_funding_kicker")}</span><h1>{L("hedge_funding_title")}</h1>
        <div className="meta">{L("hedge_historical_note")}{asOf != null ? ` · ${slFmtMs(asOf)}` : ""}</div></div>
      <div className="scanbar sl-filters">
        <input className="pname" aria-label={L("hedge_aria_min_fcs")} placeholder="min FCS"
          inputMode="decimal" value={local.minFcs} onChange={(e) => setF("minFcs", e.target.value)} />
        <input className="pname" aria-label={L("hedge_aria_min_funding")} placeholder={L("hedge_ph_min_funding")}
          inputMode="decimal" value={local.minFundingPct} onChange={(e) => setF("minFundingPct", e.target.value)} />
        <input className="pname" aria-label={L("hedge_aria_min_ratio")} placeholder="min positive %"
          inputMode="decimal" value={local.minPositivePct} onChange={(e) => setF("minPositivePct", e.target.value)} />
        <select aria-label={L("hedge_aria_venue")} value={local.venue} onChange={(e) => setF("venue", e.target.value)}>
          {HEDGE_FUNDING_VENUE_OPTS.map((o) => <option key={o} value={o}>{o === "" ? L("sl_filter_all") : o}</option>)}
        </select>
        <select aria-label={L("hedge_aria_readiness")} value={local.readiness} onChange={(e) => setF("readiness", e.target.value)}>
          {HEDGE_FUNDING_READINESS_OPTS.map((o) => <option key={o} value={o}>{o === "" ? L("sl_filter_all") : o}</option>)}
        </select>
        <select aria-label={L("sl_aria_sort")} value={local.sort} onChange={(e) => setF("sort", e.target.value)}>
          {HEDGE_FUNDING_SORT_OPTS.map((o) => <option key={o} value={o}>{o === "" ? L("sl_default_sort") : o}</option>)}
        </select>
        {formErr && <span className="sbmsg bad" role="alert">{formErr}</span>}
        <button className="cta" onClick={load}>{L("sl_retry")}</button>
      </div>
      {keepErr && (
        <div className="provline" data-testid="funding-kept-stale" role="alert">
          <span className="tag hot">STALE</span>
          <span>{L("hedge_stale_kept")}</span>
          <span className="sd-err">{String(keepErr).slice(0, 200)}</span>
        </div>
      )}
      {items.length === 0
        ? <div className="reason" data-testid="funding-empty">{L("hedge_funding_empty")}</div>
        : (
          <div className="sl-scroll">
            <table className="rank sl-table" data-testid="funding-table">
              <thead><tr>
                <th scope="col">TOKEN</th>
                <th scope="col" className="r">FCS</th>
                <th scope="col" className="r">{L("hedge_col_funding_7d")}</th>
                <th scope="col" className="r">{L("hedge_col_funding_30d")}</th>
                <th scope="col" className="r">{L("hedge_col_funding_90d")}</th>
                <th scope="col" className="r">POS 30D</th>
                <th scope="col">VENUE</th>
                <th scope="col" className="r">COST</th>
                <th scope="col" className="r">BE DAYS</th>
                <th scope="col">STATE</th>
              </tr></thead>
              <tbody>
                {items.map((it) => {
                  const sym = it.symbol || it.Symbol || "—";
                  const fcs = it.fcs != null ? it.fcs : it.Fcs;
                  const f7 = it.funding7d != null ? it.funding7d : it.funding_7d;
                  const f30 = it.funding30d != null ? it.funding30d : it.funding_30d;
                  const f90 = it.funding90d != null ? it.funding90d : it.funding_90d;
                  const pr = it.positiveRatio30d != null ? it.positiveRatio30d : it.positive_ratio_30d;
                  const venue = it.bestVenue || it.best_venue || it.venue || "—";
                  const cost = it.roundTripCostPct != null ? it.roundTripCostPct : it.round_trip_cost_pct;
                  const be = it.breakEvenDays != null ? it.breakEvenDays : it.break_even_days;
                  const readiness = it.readiness || "—";
                  const reasons = Array.isArray(it.reasons) ? it.reasons : [];
                  const isStale = it.stale === true || it.STALE === true;
                  const snapId = it.snapshotId != null ? it.snapshotId : it.snapshot_id;
                  return (
                    <tr key={sym + "|" + String(fcs)} data-testid={`funding-row-${sym}`}>
                      <td><div className="sym">{sym}<small>{it.canonicalId || it.canonical_id || ""}</small></div>
                        {snapId != null && snapId !== "" && <div className="sl-reasons">{String(snapId)}</div>}
                      </td>
                      <td className="r score">{slFmtScore(fcs)}</td>
                      <td className="r">{slFmtFundingDecimal(f7)}</td>
                      <td className="r">{slFmtFundingDecimal(f30)}</td>
                      <td className="r">{slFmtFundingDecimal(f90)}</td>
                      <td className="r">{slFmtShare(pr)}</td>
                      <td>{venue}</td>
                      <td className="r">{cost != null ? slFmtFundingDecimal(cost) : "—"}</td>
                      <td className="r">{be != null && isFinite(Number(be)) ? Number(be).toFixed(1) : "—"}</td>
                      <td>
                        <span className={"pill " + (readiness === "READY" ? "b" : readiness === "BLOCKED" ? "s" : "n")}>
                          <span className="g" />{readiness}
                        </span>
                        {isStale && <span className="tag hot" data-testid={`funding-stale-${sym}`}>STALE</span>}
                        {isStale && <div className="sl-reasons">{L("repair_funding_stale_reason")}</div>}
                        {reasons.length > 0 && <div className="sl-reasons">{reasons.join(" · ")}</div>}
                        <div style={{ marginTop: 4 }}>
                          <button className="chip" data-testid={`funding-analyze-${sym}`} onClick={() => handleAnalyze(it)}>
                            {L("repair_funding_analyze_btn")}
                          </button>
                        </div>
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
