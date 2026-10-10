/* ============================================================================
   short-lab — Desktop · evidence subpage (F08 · design A8/A9.1; H09 adds the
   hedge sub-tab · design B32.13/B39)

   Directional and hedge are SEPARATE endpoints and NEVER mix results:
     - directional: window.DIVE.shortEvidence(filters) → /api/short/evidence/summary
     - hedge:       window.DIVE.hedgeEvidenceSummary(hedgeFilters) → /api/short/hedge/evidence/summary
   Each side owns its own body/error/loading + AbortController + sequence, so
   a late directional response can never overwrite hedge state and vice versa.

   Honesty: 503 (short_evidence_unavailable / HEDGE_EVIDENCE_UNAVAILABLE)
   renders the backend reason verbatim, never mock; `initial` (directional)
   and `initialHedge` (hedge) bypass fetch for deterministic tests.
   ========================================================================== */

function ShortLabEvidence({ filters, hedgeFilters, initial, initialHedge, defaultTab }) {
  const [activeSub, setActiveSub] = React.useState(
    (defaultTab === "hedge" || (initialHedge && (initialHedge.data || initialHedge.error))) ? "hedge" : "directional");
  const [body, setBody] = React.useState(initial && initial.data ? initial.data : null);
  const [err, setErr] = React.useState(initial && initial.error ? initial.error : null);
  const [loading, setLoading] = React.useState(!(initial && (initial.data || initial.error)));
  const seqRef = React.useRef(0);
  const abortRef = React.useRef(null);
  const [hBody, setHBody] = React.useState(initialHedge && initialHedge.data ? initialHedge.data : null);
  const [hErr, setHErr] = React.useState(initialHedge && initialHedge.error ? initialHedge.error : null);
  const [hLoading, setHLoading] = React.useState(!(initialHedge && (initialHedge.data || initialHedge.error)));
  const hSeqRef = React.useRef(0);
  const hAbortRef = React.useRef(null);

  const load = React.useCallback(() => {
    if (initial && (initial.data || initial.error)) return;
    const seq = ++seqRef.current;
    if (abortRef.current) { try { abortRef.current.abort(); } catch (e) { /* next fetch owns it */ } }
    const ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
    abortRef.current = ctrl;
    setLoading(true); setErr(null);
    const opts = ctrl ? { signal: ctrl.signal } : {};
    window.DIVE.shortEvidence(filters || {}, opts)
      .then((res) => { if (seq === seqRef.current) { setBody(res); setLoading(false); } })
      .catch((e) => {
        if (ctrl && ctrl.signal && ctrl.signal.aborted) return;
        if (seq !== seqRef.current) return;  // late response loses
        setErr((e && e.message) || String(e)); setLoading(false);
      });
  }, [JSON.stringify(filters || {})]);

  React.useEffect(() => { load(); return () => { if (abortRef.current) { try { abortRef.current.abort(); } catch (e) {} } }; }, [load]);

  const loadHedge = React.useCallback(() => {
    if (initialHedge && (initialHedge.data || initialHedge.error)) return;
    const seq = ++hSeqRef.current;
    if (hAbortRef.current) { try { hAbortRef.current.abort(); } catch (e) { /* next fetch owns it */ } }
    const ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
    hAbortRef.current = ctrl;
    setHLoading(true); setHErr(null);
    const opts = ctrl ? { signal: ctrl.signal } : {};
    window.DIVE.hedgeEvidenceSummary(hedgeFilters || {}, opts)
      .then((res) => { if (seq === hSeqRef.current) { setHBody(res); setHLoading(false); } })
      .catch((e) => {
        if (ctrl && ctrl.signal && ctrl.signal.aborted) return;
        if (seq !== hSeqRef.current) return;  // late response loses; never touches directional state
        setHErr((e && e.message) || String(e)); setHLoading(false);
      });
  }, [JSON.stringify(hedgeFilters || {})]);

  React.useEffect(() => { if (activeSub === "hedge") loadHedge(); }, [loadHedge, activeSub]);
  React.useEffect(() => () => { if (hAbortRef.current) { try { hAbortRef.current.abort(); } catch (e) {} } }, []);

  const renderDirectional = () => {
    if (loading) {
      return <div className="state" style={{ height: 160 }} data-testid="shortlab-evidence-loading">
        <div className="spin" /><div>{L("sl_evidence_loading")}</div></div>;
    }
    if (err) {
      const unavailable = /503|short_evidence_unavailable/i.test(String(err));
      return (
        <div className="state src-down" role="alert"
          data-testid={unavailable ? "shortlab-evidence-unavailable" : "shortlab-evidence-error"}>
          <div className="sd-title">SHORT LAB {unavailable ? L("sl_unavailable_title") : L("sl_error_title")}</div>
          <div className="sd-body">{unavailable ? L("sl_evidence_unavailable_body") : L("sl_evidence_error_body")}</div>
          <div className="sd-err">{String(err)}</div>
          <button className="cta" onClick={load}>{L("sl_retry")}</button>
        </div>
      );
    }

    const b = body || {};
    const horizons = (b.horizons && typeof b.horizons === "object") ? b.horizons : {};
    const names = Object.keys(horizons);
    const total = b.total != null ? b.total : null;

    return (
      <div data-testid="shortlab-evidence-directional">
        <div className="vhead"><span className="kicker">{L("sl_evidence_kicker")}</span><h1>{L("sl_evidence_title")}</h1>
          <div className="meta">{total != null ? L("sl_evidence_total", total) : "—"}{b.generatedAtMs != null ? ` · ${slFmtMs(b.generatedAtMs)}` : ""}</div></div>
        {names.length === 0 && <div className="reason">{L("sl_evidence_empty")}</div>}
        {names.map((h) => {
          const bucket = horizons[h] || {};
          const row = (k, v) => <SlStat key={k} k={k} v={v == null ? "—" : String(v)} />;
          return (
            <div key={h} className="panel" data-testid={`shortlab-evidence-${h}`}>
              <div className="ph"><span className="tick">▸</span>{L("sl_evidence_horizon")} · {h}
                <span className="rt">total {bucket.total != null ? bucket.total : "—"}</span></div>
              <div className="pb" style={{ padding: 0 }}>
                {row("PENDING", bucket.PENDING)}
                {row("COMPLETE", bucket.COMPLETE)}
                {row("CENSORED", bucket.CENSORED)}
                {row("UNAVAILABLE", bucket.UNAVAILABLE)}
                <SlStat k="meanNetReturn" v={bucket.meanNetReturn != null ? slFmtRatio(bucket.meanNetReturn, 4) : "—"} />
                <SlStat k="meanPriceReturn" v={bucket.meanPriceReturn != null ? slFmtRatio(bucket.meanPriceReturn, 4) : "—"} />
                <SlStat k="meanFundingCarry" v={bucket.meanFundingCarry != null ? slFmtRatio(bucket.meanFundingCarry, 4) : "—"} />
              </div>
            </div>
          );
        })}
      </div>
    );
  };

  const renderHedge = () => {
    if (hLoading) {
      return <div className="state" style={{ height: 160 }} data-testid="hedge-evidence-loading">
        <div className="spin" /><div>{L("hedge_evidence_loading")}</div></div>;
    }
    if (hErr) {
      const unavailable = /503|HEDGE_EVIDENCE_UNAVAILABLE/i.test(String(hErr));
      return (
        <div className="state src-down" role="alert"
          data-testid={unavailable ? "hedge-evidence-unavailable" : "hedge-evidence-error"}>
          <div className="sd-title">HEDGE {unavailable ? L("sl_unavailable_title") : L("sl_error_title")}</div>
          <div className="sd-body">{unavailable ? L("hedge_evidence_unavailable_body") : L("hedge_evidence_error_body")}</div>
          <div className="sd-err">{String(hErr)}</div>
          <button className="cta" onClick={loadHedge}>{L("sl_retry")}</button>
        </div>
      );
    }
    const hb = hBody || {};
    const buckets = Array.isArray(hb.buckets) ? hb.buckets : [];
    const total = hb.total != null ? hb.total : (buckets.length ? buckets.reduce((s, b) => s + (Number(b.total) || 0), 0) : null);
    return (
      <div data-testid="hedge-evidence">
        <div className="vhead"><span className="kicker">{L("hedge_evidence_kicker")}</span><h1>{L("hedge_evidence_title")}</h1>
          <div className="meta">{total != null ? L("hedge_evidence_total", total) : "—"}{hb.generatedAt != null ? ` · ${slFmtMs(hb.generatedAt)}` : (hb.generatedAtMs != null ? ` · ${slFmtMs(hb.generatedAtMs)}` : "")}</div></div>
        {buckets.length === 0 && <div className="reason" data-testid="hedge-evidence-empty">{L("hedge_evidence_empty")}</div>}
        {buckets.map((bk, i) => {
          const key = `${bk.strategy || bk.Strategy || "strategy"}|${bk.horizon || bk.Horizon || ""}|${bk.historyClass || bk.history_class || ""}|${i}`;
          const row = (k, v) => <SlStat key={k} k={k} v={v == null ? "—" : String(v)} />;
          const evaluation = bk.evaluation || bk.evaluationReport || {};
          const bootstrap = evaluation.bootstrap || {};
          const pairedBootstrap = evaluation.paired_bootstrap || {};
          const walkForward = evaluation.walk_forward || {};
          const renderEvaluation = (ev, testId) => {
            const evBoot = ev.bootstrap || {};
            const evPairedBoot = ev.paired_bootstrap || {};
            const evWalk = ev.walk_forward || {};
            const coverage = ev.coverage || {};
            const pathCoverage = ev.liquidation_path_coverage || {};
            const pathCounts = pathCoverage.counts || {};
            const knownCosts = ev.known_costs || {};
            const entryCounts = ev.entry_status_counts || {};
            const entryTotal = Object.values(entryCounts).reduce((sum, n) => sum + (Number(n) || 0), 0);
            const entryComplete = Number(entryCounts.ENTRY_COMPLETE) || 0;
            const markCoverage = ev.funding_mark_coverage || {};
            return <div className="reason" data-testid={testId}>
              {row("sampleStatus", ev.sample_status)}
              {row("p05NetReturn", ev.p05_net_return)}
              {row("assetBootstrap", `${evBoot.status || "UNKNOWN"} · ${evBoot.n_assets || 0} assets · ${evBoot.n || 0} samples`)}
              {row("pairedBootstrap", `${evPairedBoot.status || "UNKNOWN"} · ${evPairedBoot.n_assets || 0} assets · ${evPairedBoot.n || 0} pairs`)}
              {row("entryStatusCounts", Object.keys(entryCounts).length ? Object.entries(entryCounts).map(([k, v]) => `${k} ${v}`).join(" · ") : "—")}
              {row("tradableEntryRate", entryTotal ? `${(100 * entryComplete / entryTotal).toFixed(1)}%` : "—")}
              {row("outcomeCoverage", coverage.total ? `${coverage.complete}/${coverage.total}` : "—")}
              {row("markCoverage", markCoverage.priced_outcomes ? `${slFmtRatio(markCoverage.mean, 3)} · ${markCoverage.priced_outcomes} priced` : "—")}
              {row("pathCoverage", Object.keys(pathCounts).length ? Object.entries(pathCounts).map(([k, v]) => `${k} ${v}`).join(" · ") : "—")}
              {row("pathCompleteFraction", pathCoverage.complete_fraction)}
              {row("pricedOutcomes", knownCosts.priced_outcomes)}
              {row("meanFeesUsd", knownCosts.mean_fees_usd)}
              {row("maxAdverseBasisUsd", ev.max_adverse_basis_usd)}
              {row("maxPortfolioDrawdownUsd", ev.max_portfolio_drawdown_usd)}
              {row("walkForwardOOS", `${evWalk.oos_month || "—"} · ${evWalk.oos_label || "UNKNOWN"}`)}
            </div>;
          };
          const subBuckets = Array.isArray(bk.subBuckets) ? bk.subBuckets : [];
          return (
            <div key={key} className="panel" data-testid={`hedge-evidence-bucket-${bk.strategy || "s"}-${bk.horizon || "h"}`}>
              <div className="ph"><span className="tick">▸</span>{bk.strategy || "—"} · {bk.horizon || "—"} · {bk.historyClass || bk.history_class || "—"}
                <span className="rt">total {bk.total != null ? bk.total : "—"}</span></div>
              <div className="pb" style={{ padding: 0 }}>
                {row("PENDING", bk.PENDING != null ? bk.PENDING : bk.pending)}
                {row("COMPLETE", bk.COMPLETE != null ? bk.COMPLETE : bk.complete)}
                {row("CENSORED", bk.CENSORED != null ? bk.CENSORED : bk.censored)}
                {row("UNAVAILABLE", bk.UNAVAILABLE != null ? bk.UNAVAILABLE : bk.unavailable)}
                <SlStat k="meanNetReturn" v={bk.meanNetReturn != null ? slFmtRatio(bk.meanNetReturn, 4) : (bk.mean_net_return != null ? slFmtRatio(bk.mean_net_return, 4) : "—")} />
                {row("medianNetReturn", bk.medianNetReturn != null ? bk.medianNetReturn : bk.median_net_return)}
                {bk.pairedCount != null && <div data-testid="hedge-evidence-paired">
                  {row("paired", `${bk.pairedCount} complete · ${bk.pairedMissingCount || 0} missing`)}
                  {row("pairedMeanDiff", bk.pairedMeanDiff)}
                </div>}
                {bk.evaluation && <div className="reason" data-testid="hedge-evidence-evaluation">
                  {renderEvaluation(evaluation, "hedge-evidence-evaluation-details")}
                </div>}
                {subBuckets.length > 1 && <div className="reason" data-testid="hedge-evidence-sub-buckets">
                  <div className="kicker">Independent evaluation buckets</div>
                  {subBuckets.map((sub, si) => {
                    const dims = sub.bucket || {};
                    const subEval = sub.evaluation || {};
                    const subPaired = subEval.paired || {};
                    const subKey = `${dims.cohort || "UNKNOWN"}|${dims.profile || "UNKNOWN"}|${dims.formula_version || "UNKNOWN"}|${si}`;
                    return <div key={subKey} className="panel" data-testid="hedge-evidence-sub-bucket">
                      <div className="ph">{dims.cohort || "UNKNOWN"} · {dims.profile || "UNKNOWN"} · {dims.goal || "UNKNOWN"}</div>
                      <div className="pb">
                        {row("subBucketFormula", dims.formula_version)}
                        {row("subBucketHistory", dims.history_class)}
                        {row("subBucketVenue", dims.venue)}
                        {row("subBucketSamples", sub.sampleCount)}
                        {row("subBucketMeanNet", subEval.mean_net == null ? "—" : slFmtRatio(subEval.mean_net, 4))}
                        {row("subBucketPaired", `${subPaired.n_paired || 0} complete · ${subPaired.n_missing || 0} missing`)}
                        {row("subBucketPairedMeanDiff", subPaired.mean_diff)}
                        {renderEvaluation(subEval, `hedge-evidence-sub-evaluation-${si}`)}
                      </div>
                    </div>;
                  })}
                </div>}
              </div>
            </div>
          );
        })}
      </div>
    );
  };

  if (loading && hLoading && !body && !hBody && !err && !hErr) {
    // First paint without any initial: keep the F08 loading marker so the old
    // test (no initialHedge) still sees shortlab-evidence-loading.
    if (!initialHedge) {
      return <div className="state" style={{ height: 160 }} data-testid="shortlab-evidence-loading">
        <div className="spin" /><div>{L("sl_evidence_loading")}</div></div>;
    }
  }

  return (
    <div data-testid="shortlab-evidence">
      <div className="scanbar" role="tablist" aria-label="Evidence sections" data-testid="shortlab-evidence-tabs">
        <button className={"chip" + (activeSub === "directional" ? " on" : "")} role="tab"
          aria-selected={activeSub === "directional"} data-testid="shortlab-evidence-tab-directional"
          onClick={() => setActiveSub("directional")}>{L("hedge_evidence_tab_directional")}</button>
        <button className={"chip" + (activeSub === "hedge" ? " on" : "")} role="tab"
          aria-selected={activeSub === "hedge"} data-testid="shortlab-evidence-tab-hedge"
          onClick={() => setActiveSub("hedge")}>{L("hedge_evidence_tab_hedge")}</button>
      </div>
      {activeSub === "directional" ? renderDirectional() : renderHedge()}
    </div>
  );
}
