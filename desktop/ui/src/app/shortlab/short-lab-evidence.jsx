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
