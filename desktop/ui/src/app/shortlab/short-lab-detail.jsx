/* ============================================================================
   short-lab — Desktop · detail view (Task 15 · design §26.4)

   Fixed blocks: Summary / Structure / Carry / Valuation / Tokenomics /
   Narrative / Existing Dive / Risks / Data Sources. Reads the
   /api/short/symbol/{symbol} body VERBATIM — never recomputes score/status.
   Missing values: real value, else "N/A" (confirmed NOT_APPLICABLE), else "—"
   plus an UNAVAILABLE note. `initial` bypasses fetch for deterministic tests.
   ========================================================================== */

function slDig(root, paths) {
  for (const p of (paths || [])) {
    let cur = root, ok = true;
    for (const k of p) {
      if (cur == null || typeof cur !== "object" || !(k in cur)) { ok = false; break; }
      cur = cur[k];
    }
    if (!ok || cur == null) continue;
    if (typeof cur === "object" && !Array.isArray(cur) && cur.value != null) return cur.value;
    if (typeof cur !== "object") return cur;
  }
  return null;
}

function SlStat({ k, v, sub }) {
  return (
    <div className="stat"><span className="k">{k}</span>
      <span className="v">{v == null ? "—" : v}{sub ? <small className="sub"> · {sub}</small> : null}</span>
    </div>
  );
}

function ShortLabDetail({ symbol, onBack, generationId, initial }) {
  const [detail, setDetail] = React.useState(initial && initial.data ? initial.data : null);
  const [err, setErr] = React.useState(initial && initial.error ? initial.error : null);
  const [loading, setLoading] = React.useState(!(initial && (initial.data || initial.error)));
  const seqRef = React.useRef(0);

  React.useEffect(() => {
    if (initial && (initial.data || initial.error)) return;  // deterministic test path
    const seq = ++seqRef.current;
    let live = true;
    const ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
    setLoading(true); setErr(null);
    // Read-only pinned generation: the backend serves SUCCEEDED generations only.
    window.DIVE.shortDetail(symbol, { generationId, ...(ctrl ? { signal: ctrl.signal } : {}) })
      .then((d) => { if (live && seq === seqRef.current) { setDetail(d); setLoading(false); } })
      .catch((e) => {
        if (ctrl && ctrl.signal && ctrl.signal.aborted) return;
        if (live && seq === seqRef.current) { setErr((e && e.message) || String(e)); setLoading(false); }
      });
    return () => { live = false; if (ctrl) { try { ctrl.abort(); } catch (e) {} } };
  }, [symbol, generationId]);

  if (loading) {
    return <div className="state" style={{ height: 220 }} data-testid="shortlab-detail-loading">
      <div className="spin" /><div>{L("sl_detail_loading")}</div></div>;
  }
  if (err) {
    const unavailable = /503|shortlab_unavailable/i.test(String(err));
    return (
      <div className="state src-down" role="alert"
        data-testid={unavailable ? "shortlab-detail-unavailable" : "shortlab-detail-error"}>
        <div className="sd-title">SHORT LAB {unavailable ? L("sl_unavailable_title") : L("sl_error_title")}</div>
        <div className="sd-body">{unavailable ? L("sl_detail_unavailable_body") : L("sl_detail_error_body")}</div>
        <div className="sd-err">{String(err)}</div>
        <div style={{ display: "flex", gap: 8 }}>
          {onBack && <button className="cta" style={{ background: "transparent", color: "var(--dim)", border: "1px solid var(--line2)" }} onClick={onBack}>{L("sl_back_list")}</button>}
          <button className="cta" onClick={() => window.DIVE.shortDetail(symbol, generationId ? { generationId } : {}).then(setDetail).catch((e) => setErr((e && e.message) || String(e)))}>{L("sl_retry")}</button>
        </div>
      </div>
    );
  }

  const d = detail || {};
  const m = d.metrics || {};
  const sources = d.dataSources || {};
  const avail = d.dataAvailability || {};
  const feats = (d.feature && d.feature.features) || {};
  const entry = d.entry || null;
  const entryInputs = (entry && entry.inputs) || {};
  const entryComps = (entry && entry.components) || null;
  const status = d.status || "—";
  const srcStatus = (name) => (sources[name] && sources[name].status) || "UNAVAILABLE";
  const valOrNa = (paths, fmtFn, sourceNames) => {
    const v = slDig({ metrics: m, features: feats }, paths);
    if (v != null && slFinite(v) != null) return fmtFn(slFinite(v));
    const states = (sourceNames || []).map(srcStatus);
    if (states.length && states.every((s) => s === "NOT_APPLICABLE")) return "N/A";
    return "—";
  };

  return (
    <div className="sl-detail" data-testid="shortlab-detail">
      <div className="vhead">
        <span className="kicker">{L("sl_detail_kicker")}</span><h1>{d.symbol || symbol}</h1>
        <div className="meta">{d.profile || "—"} · {slTierTag(d.analysisTier)} · {d.scoreVersion || ""}</div>
      </div>
      <div className="scanbar sl-bar">
        {onBack && <button className="chip" onClick={onBack}>{L("sl_back_list")}</button>}
        <span className={"pill " + (status === "READY" ? "b" : status === "BLOCKED" ? "s" : "n")}>
          <span className="g" />{slStatusLabel(status, d.analysisTier)}</span>
        {d.stale && <span className="tag hot" title={L("sl_stale_title")}>STALE</span>}
        <span className="sbcap">as-of {slFmtMs(d.asOfMs)} · generation {(d.generationId || "").slice(0, 18)}</span>
      </div>

      <div className="grid2">
        <div className="panel"><div className="ph"><span className="tick">▸</span>SUMMARY</div><div className="pb" style={{ padding: 0 }}>
          <SlStat k="LTSS" v={slFmtScore(d.ltss)} />
          <SlStat k="ENTRY" v={slFmtEntry(d.entryScore)} sub={entry ? ("entry " + (entry.entryVersion || "")) : L("sl_entry_not_computed_short")} />
          <SlStat k="DATA QUALITY" v={slFmtDQ(d.dataQuality)} sub={d.snapshotDataQuality != null ? ("snapshot " + slFmtDQ(d.snapshotDataQuality)) : null} />
          <SlStat k="CANDIDATE" v={d.candidateStatus || "—"} />
          <SlStat k="EXECUTION" v={d.executionStatus || "—"} />
        </div></div>
        <div className="panel"><div className="ph"><span className="tick">▸</span>RISKS · BLOCK/PAUSE/WARN</div><div className="pb" style={{ padding: 0 }}>
          {(d.vetoes || []).map((c) => <SlStat key={"v" + c} k={"⛔ " + c} v={slReasonText(c)} />)}
          {(d.pauses || []).map((c) => <SlStat key={"p" + c} k={"⏸ " + c} v={slReasonText(c)} />)}
          {(d.reasons || []).filter((c) => !(d.vetoes || []).includes(c) && !(d.pauses || []).includes(c))
            .map((c) => <SlStat key={"r" + c} k={"NOT_READY · " + c} v={slReasonText(c)} />)}
          {(d.warnings || []).map((c) => <SlStat key={"w" + c} k={"WARN · " + c} v={slReasonText(c)} />)}
          {!(d.vetoes || []).length && !(d.pauses || []).length && !(d.reasons || []).length && !(d.warnings || []).length &&
            <div className="reason">{L("sl_no_risk")}</div>}
        </div></div>
      </div>

      <div className="grid2">
        <div className="panel"><div className="ph"><span className="tick">▸</span>STRUCTURE</div><div className="pb" style={{ padding: 0 }}>
          <SlStat k="ATH DRAWDOWN" v={m.athDrawdown != null ? slFmtAthDD(m.athDrawdown) : valOrNa([["features", "lifecycle", "factors", "ath_drawdown"], ["features", "_inputs", "ath_drawdown"]], (v) => slFmtAthDD(v), ["ath"])} />
          <SlStat k="ATH AGE" v={valOrNa([["features", "_inputs", "ath_age_days"], ["features", "lifecycle", "factors", "ath_age", "value"]], (v) => slFmtRatio(v, 0) + "d", ["ath"])} />
          <SlStat k="30D PRICE" v={valOrNa([["features", "_inputs", "price_change_30d"], ["features", "lifecycle", "factors", "price_30d", "value"]], (v) => slFmtFundingDecimal(v), ["market_daily"])} />
          <SlStat k="SPOT VOLUME DECAY" v={valOrNa([["features", "_inputs", "spot_volume_decay_30d"]], (v) => slFmtRatio(v), ["spot_volume_60d"])} />
        </div></div>
        <div className="panel"><div className="ph"><span className="tick">▸</span>CARRY</div><div className="pb" style={{ padding: 0 }}>
          <SlStat k="30D FUNDING" v={m.funding30d != null ? slFmtFundingDecimal(m.funding30d) : valOrNa([["features", "_inputs", "funding_30d"]], (v) => slFmtFundingDecimal(v), ["funding_30d"])} />
          <SlStat k="30D FUNDING APR" v={m.funding30d != null ? slFmtFundingDecimal(m.funding30d * 365 / 30) : "—"} sub={L("sl_apr_sub")} />
          <SlStat k="30D POSITIVE RATIO" v={slFmtShare(m.positiveFundingRatio30d)} />
          <SlStat k="OI / MC" v={m.oiMarketCapRatio != null ? slFmtRatio(m.oiMarketCapRatio) : valOrNa([["features", "_inputs", "oi_mc"]], (v) => slFmtRatio(v), ["oi_usd", "market_cap"])} />
          <SlStat k="FUTURES / SPOT" v={slFieldText(m.futuresSpotVolumeRatio, (v) => slFmtRatio(v), avail.spot)} />
        </div></div>
      </div>

      <div className="grid2">
        <div className="panel"><div className="ph"><span className="tick">▸</span>VALUATION</div><div className="pb" style={{ padding: 0 }}>
          <SlStat k="MARKET CAP" v={valOrNa([["features", "_inputs", "market_cap_usd"]], (v) => "$" + slFmtUsd(v), ["market_cap"])} />
          <SlStat k="FDV" v={valOrNa([["features", "_inputs", "fdv_usd"]], (v) => "$" + slFmtUsd(v), ["fdv"])} />
          <SlStat k="FDV / MC" v={valOrNa([["features", "_inputs", "fdv_mc"], ["features", "valuation", "factors", "fdv_mc", "value"]], (v) => slFmtRatio(v), ["market_cap", "fdv"])} />
          <SlStat k="FLOAT RATIO" v={valOrNa([["features", "_inputs", "float_ratio"]], (v) => slFmtShare(v), ["supply_float"])} />
          <div className="gcap">{L("sl_gcap_valuation")}</div>
        </div></div>
        <div className="panel mute"><div className="ph"><span className="tick">▸</span>TOKENOMICS · NARRATIVE</div><div className="pb" style={{ padding: 0 }}>
          <div className="reason">{L("sl_tokenomics_placeholder")}</div>
        </div></div>
      </div>

      <div className="panel"><div className="ph"><span className="tick">▸</span>EXISTING DIVE · ENTRY REFERENCE</div><div className="pb" style={{ padding: 0 }}>
        {!entry && <div className="reason">{L("sl_entry_not_computed")}</div>}
        {entry && (
          <div>
            <SlStat k="ENTRY SCORE" v={slFmtEntry(entry.entryScore)} sub={entry.entryVersion || ""} />
            <SlStat k="FINAL SIGNAL" v={slDig(entryInputs, [["finalSignal"], ["final_signal"]]) || "—"} sub={slDig(entryInputs, [["confidence"]]) != null ? ("conf " + slDig(entryInputs, [["confidence"]])) : null} />
            <SlStat k="MTF" v={slDig(entryInputs, [["mtfConfluence", "label"], ["mtf", "label"]]) || "—"} />
            <SlStat k="MICRO" v={slDig(entryInputs, [["microstructure", "label"], ["micro", "label"]]) || "—"} />
            <SlStat k="REGIME" v={slDig(entryInputs, [["regime", "regime"]]) || "—"} />
            {entryComps && typeof entryComps === "object" && Object.keys(entryComps).length > 0 && (
              <table className="itbl"><tbody>
                {Object.entries(entryComps).map(([k, v]) => (
                  <tr key={k}><td className="nm">{k}</td>
                    <td className="rv">{v != null && typeof v === "object" ? JSON.stringify(v).slice(0, 120) : String(v)}</td></tr>
                ))}
              </tbody></table>
            )}
          </div>
        )}
      </div></div>

      <div className="panel"><div className="ph"><span className="tick">▸</span>DATA SOURCES · fetchedAt / stale / unavailable</div>
        <div className="pb" style={{ padding: 0 }}>
          <table className="itbl"><tbody>
            {Object.keys(sources).length === 0 && <tr><td className="nm">{L("sl_no_source_meta")}</td></tr>}
            {Object.entries(sources).map(([field, meta]) => (
              <tr key={field}>
                <td className="nm">{field}</td>
                <td className="rv">{slAvailabilityLabel(meta && meta.status)}</td>
                <td className="sg" style={{ fontVariantNumeric: "tabular-nums", fontSize: 10 }}>
                  {meta && meta.fetchedAtMs != null ? slFmtMs(meta.fetchedAtMs) : "—"}{meta && meta.reasonCode ? " · " + meta.reasonCode : ""}
                </td>
              </tr>
            ))}
          </tbody></table>
          <div className="gcap">{L("sl_gcap_na")}</div>
        </div></div>
    </div>
  );
}
