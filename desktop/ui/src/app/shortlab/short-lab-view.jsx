/* ============================================================================
   short-lab — Desktop · scanner view (F08 · design A9.1, §26.1–26.3, §26.5)

   Owns its own request state (the App never polls the same query):
   - talks to the backend ONLY through window.DIVE.shortCandidates /
     shortDetail / shortRefresh / shortRefreshStatus / shortHealth (data.js);
   - manual POST /refresh answers 202 as a TASK; the page polls job status
     every 2s to SUCCEEDED/FAILED/timeout and only then resets offset to 0
     and switches generation;
   - normal pagination pins generationId; a periodic health probe reads
     lastSuccessfulGeneration and shows a "new data" hint without auto-switch;
   - AbortController + request sequence reject late responses; a failed fetch
     keeps the old body and marks STALE instead of clearing; 503 never mocks.
   Filter controls map 1:1 onto /api/short/candidates params
   (min_funding_30d, ath_drawdown_min/max, min_data_quality, …); the funding
   %↔decimal conversion happens in short-lab-format.js, nowhere else.
   No explicit sort → no sort param → server §25.1 default order.
   API failure with no prior body renders Short-Lab's own UNAVAILABLE page —
   never mock data, never the global DataSourceDown gate.
   `initial` bypasses fetch for tests ({data, error, newGenerationId, job}).
   ========================================================================== */

const SL_STATUS_OPTS = ["", "READY", "CANDIDATE", "WATCH", "PAUSED", "BLOCKED", "EXCLUDED"];
const SL_EXEC_OPTS = ["", "NOT_READY", "READY", "PAUSED", "BLOCKED"];
const SL_PROFILE_OPTS = ["", "MEME_LITE", "GENERAL_LITE", "LOW_FLOAT_VC_LITE",
  "MEME_FULL", "GENERAL_FULL", "LOW_FLOAT_VC_FULL"];
const SL_SORT_OPTS = ["", "ltss", "entry", "funding30d", "dataQuality"];
const SL_PAGE_LIMIT = 50;
const SL_REFRESH_POLL_MS = 2000;
const SL_REFRESH_TIMEOUT_MS = 60000;
const SL_HEALTH_CHECK_MS = 30000;

function slDefaultFilters() {
  return {
    status: "", executionStatus: "", profile: "", category: "",
    minLtss: "", minEntry: "", minFundingPct: "", athMinPct: "", athMaxPct: "",
    minDQ: "", sort: "", order: "desc",
  };
}

/* F08 pure helpers (exposed for node --test via window.SL_REFRESH). */
function slIsTerminalJobStatus(status) {
  const s = String(status || "").toUpperCase();
  return s !== "" && s !== "RUNNING";
}
function slShouldShowNewGen(pinned, latest) {
  if (!latest) return false;
  if (!pinned) return false;
  return String(pinned) !== String(latest);
}
async function slPollRefreshJob(jobId, opts) {
  const o = opts || {};
  const intervalMs = o.intervalMs != null ? o.intervalMs : SL_REFRESH_POLL_MS;
  const timeoutMs = o.timeoutMs != null ? o.timeoutMs : SL_REFRESH_TIMEOUT_MS;
  const statusFn = o.statusFn || ((id) => window.DIVE.shortRefreshStatus(id));
  const sleep = o.sleep || ((ms) => new Promise((r) => setTimeout(r, ms)));
  const deadline = Date.now() + timeoutMs;
  let last = null;
  for (;;) {
    let cur = null;
    try { cur = await statusFn(jobId); }
    catch (e) { last = { error: (e && e.message) || String(e) }; break; }
    last = cur;
    const st = cur && (cur.status || cur.Status);
    if (slIsTerminalJobStatus(st)) return { status: cur, timedOut: false };
    if (Date.now() >= deadline) return { status: cur, timedOut: true };
    await sleep(intervalMs);
    if (Date.now() >= deadline) {
      try { cur = await statusFn(jobId); last = cur;
        if (slIsTerminalJobStatus(cur && (cur.status || cur.Status))) return { status: cur, timedOut: false };
      } catch (e) { /* keep last */ }
      return { status: last, timedOut: true };
    }
  }
  return { status: last, timedOut: false };
}
if (typeof window !== "undefined") {
  window.SL_REFRESH = window.SL_REFRESH || {};
  window.SL_REFRESH.POLL_MS = SL_REFRESH_POLL_MS;
  window.SL_REFRESH.TIMEOUT_MS = SL_REFRESH_TIMEOUT_MS;
  window.SL_REFRESH.isTerminal = slIsTerminalJobStatus;
  window.SL_REFRESH.shouldShowNewGen = slShouldShowNewGen;
  window.SL_REFRESH.pollJob = slPollRefreshJob;
  window.SL_REFRESH.PAGE_LIMIT = SL_PAGE_LIMIT;
}

function ShortLabView({ initial, pollIntervalMs, refreshTimeoutMs, healthCheckMs }) {
  const [filters, setFilters] = React.useState(slDefaultFilters());
  const [offset, setOffset] = React.useState(0);
  const [generationId, setGenerationId] = React.useState(null);
  const [selected, setSelected] = React.useState(null);
  const [body, setBody] = React.useState(initial && initial.data ? initial.data : null);
  const [err, setErr] = React.useState(initial && initial.error ? initial.error : null);
  const [loading, setLoading] = React.useState(!(initial && (initial.data || initial.error)));
  const [formErr, setFormErr] = React.useState(null);
  const [keepErr, setKeepErr] = React.useState(null);  // failed refetch keeps old body
  const [job, setJob] = React.useState((initial && initial.job) || null);
  const [jobStatus, setJobStatus] = React.useState((initial && initial.jobStatus) || null);
  const [refreshing, setRefreshing] = React.useState(false);
  const [newGenId, setNewGenId] = React.useState((initial && initial.newGenerationId) || null);
  const [activeTab, setActiveTab] = React.useState("candidates");
  const seqRef = React.useRef(0);
  const abortRef = React.useRef(null);
  const pollMs = pollIntervalMs != null ? pollIntervalMs : SL_REFRESH_POLL_MS;
  const timeoutMs = refreshTimeoutMs != null ? refreshTimeoutMs : SL_REFRESH_TIMEOUT_MS;
  const healthMs = healthCheckMs != null ? healthCheckMs : SL_HEALTH_CHECK_MS;

  const setF = (k, v) => {
    setFilters((f) => ({ ...f, [k]: v }));
    setOffset(0); setGenerationId(null); setNewGenId(null);  // new filter → unpin pagination
  };

  const toggleSort = (k) => {
    setFilters((f) => ({ ...f, sort: k, order: f.sort === k && f.order === "desc" ? "asc" : "desc" }));
    setOffset(0);   // same pinned generation, new server order
  };

  const buildQuery = () => {
    const q = { limit: SL_PAGE_LIMIT, offset };
    if (generationId) q.generationId = generationId;
    if (filters.status) q.status = filters.status;
    if (filters.executionStatus) q.executionStatus = filters.executionStatus;
    if (filters.profile) q.profile = filters.profile;
    if (filters.category.trim()) q.category = filters.category.trim().toUpperCase();
    if (filters.minLtss !== "") {
      const n = Number(filters.minLtss);
      if (!isFinite(n) || n < 0 || n > 100) return { error: L("sl_err_min_ltss") };
      q.minLtss = n;
    }
    if (filters.minEntry !== "") {
      const n = Number(filters.minEntry);
      if (!isFinite(n) || n < 0 || n > 100) return { error: L("sl_err_min_entry") };
      q.minEntry = n;
    }
    if (filters.minFundingPct !== "") {
      const dec = slPctInputToDecimal(filters.minFundingPct);
      if (dec == null) return { error: L("sl_err_min_funding") };
      q.minFunding30d = dec;   // decimal on the wire; conversion lives in format.js
    }
    if (filters.athMinPct !== "" || filters.athMaxPct !== "") {
      const lo = filters.athMinPct === "" ? null : slPctInputToDecimal(filters.athMinPct);
      const hi = filters.athMaxPct === "" ? null : slPctInputToDecimal(filters.athMaxPct);
      if ((filters.athMinPct !== "" && lo == null) || (filters.athMaxPct !== "" && hi == null))
        return { error: L("sl_err_ath_nan") };
      if (lo != null && (lo < -1 || lo > 0)) return { error: L("sl_err_ath_range") };
      if (hi != null && (hi < -1 || hi > 0)) return { error: L("sl_err_ath_range") };
      if (lo != null && hi != null && lo > hi) return { error: L("sl_err_ath_order") };
      if (lo != null) q.athDrawdownMin = lo;
      if (hi != null) q.athDrawdownMax = hi;
    }
    if (filters.minDQ !== "") {
      const n = Number(filters.minDQ);
      if (!isFinite(n) || n < 0 || n > 100) return { error: L("sl_err_min_dq") };
      q.minDataQuality = n;
    }
    if (filters.sort) { q.sort = filters.sort; q.order = filters.order || "desc"; }
    return { query: q };
  };

  const fetchPage = React.useCallback((override) => {
    if (initial && (initial.data || initial.error)) return;
    const built = (override && override.query) ? override : buildQuery();
    if (built.error) { setFormErr(built.error); return; }
    const seq = ++seqRef.current;
    if (abortRef.current) { try { abortRef.current.abort(); } catch (e) { /* superseded */ } }
    const ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
    abortRef.current = ctrl;
    setFormErr(null); setLoading(true);
    if (!body) setErr(null);
    const opts = ctrl ? { signal: ctrl.signal } : {};
    window.DIVE.shortCandidates(built.query, opts)
      .then((res) => {
        if (seq !== seqRef.current) return;  // late response loses
        setBody(res); setLoading(false); setErr(null); setKeepErr(null);
        if (res && res.generationId && !generationId && !(override && override.keepPin)) setGenerationId(res.generationId);
        try { window.__diveShortQuery = built.query; } catch (e) { /* page-owned hint only */ }
      })
      .catch((e) => {
        if (ctrl && ctrl.signal && ctrl.signal.aborted) return;
        if (seq !== seqRef.current) return;
        setLoading(false);
        const msg = (e && e.message) || String(e);
        if (body) setKeepErr(msg);  // keep old body, flag STALE
        else setErr(msg);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(filters), offset, generationId, body ? "has-body" : "no-body"]);

  React.useEffect(() => { fetchPage(); }, [fetchPage]);
  React.useEffect(() => () => { if (abortRef.current) { try { abortRef.current.abort(); } catch (e) {} } }, []);

  /* Periodic latest-generation probe (pinned pagination stays; hint only). */
  React.useEffect(() => {
    if (initial && (initial.data || initial.error)) return;
    let live = true;
    const check = async () => {
      try {
        const h = await window.DIVE.shortHealth();
        if (!live) return;
        const latest = h && (h.lastSuccessfulGeneration || h.generationId);
        const pinned = generationId || (body && body.generationId);
        if (latest && pinned && String(latest) !== String(pinned)) setNewGenId(latest);
        else if (!latest || (pinned && String(latest) === String(pinned))) setNewGenId(null);
      } catch (e) { /* health probe never fails the page */ }
    };
    check();
    const id = setInterval(check, healthMs);
    return () => { live = false; clearInterval(id); };
  }, [generationId, body ? body.generationId : null, healthMs]);

  const switchToNewGen = () => {
    if (!newGenId) return;
    setOffset(0); setGenerationId(newGenId); setNewGenId(null);
  };

  const doRefresh = () => {
    if (refreshing) return;
    setRefreshing(true); setJob(null); setJobStatus({ status: "RUNNING" });
    window.DIVE.shortRefresh()
      .then((r) => {
        setJob(r);  // 202 task handle (existing:true reuses the in-flight job)
        const jid = r && r.jobId;
        if (!jid) { setRefreshing(false); setJobStatus(null); fetchPage(); return; }
        slPollRefreshJob(jid, { intervalMs: pollMs, timeoutMs })
          .then(({ status, timedOut }) => {
            setRefreshing(false);
            if (timedOut) { setJobStatus({ status: "TIMEOUT", jobId: jid }); return; }
            const st = status && (status.status || "");
            setJobStatus(status);
            if (st === "SUCCEEDED") {
              setOffset(0); setGenerationId(null); setNewGenId(null);  // next fetch pins the new gen
              fetchPage({ query: { ...buildQuery().query, offset: 0, generationId: undefined } });
            }
          })
          .catch((e) => { setRefreshing(false); setJob({ error: (e && e.message) || String(e) }); });
      })
      .catch((e) => { setJob({ error: (e && e.message) || String(e) }); setRefreshing(false); setJobStatus(null); });
  };

  if (selected) {
    return <ShortLabDetail symbol={selected} generationId={generationId} onBack={() => setSelected(null)} />;
  }

  const items = (body && body.items) || [];
  const staleCount = items.filter((it) => it && it.stale).length;
  const showStaleChip = staleCount > 0 || !!keepErr;
  const total = body != null && body.total != null ? body.total : null;

  const errText = err ? String(err) : "";
  if (err && !body) {
    const isUnavailable = /503|shortlab_unavailable/i.test(errText);
    return (
      <div>
        <div className="vhead"><span className="kicker">SHORT LAB</span><h1>{L("sl_title")}</h1>
          <div className="meta">LTSS · ENTRY · DQ · VETO/PAUSE</div></div>
        <div className="state src-down" role="alert"
          data-testid={isUnavailable ? "shortlab-unavailable" : "shortlab-error"}>
          <div className="sd-title">SHORT LAB {isUnavailable ? L("sl_unavailable_title") : L("sl_error_title")}</div>
          <div className="sd-body">{isUnavailable ? L("sl_err_unavailable_body") : L("sl_err_body")}</div>
          <div className="sd-err">{errText}</div>
          <button className="cta" onClick={() => { setErr(null); fetchPage(); }}>{L("sl_retry")}</button>
        </div>
      </div>
    );
  }

  const jobText = jobStatus && jobStatus.status ? String(jobStatus.status) : null;

  return (
    <div data-testid="shortlab-view">
      <div className="vhead"><span className="kicker">SHORT LAB</span><h1>{L("sl_title")}</h1>
        <div className="meta">LTSS · ENTRY · DQ · VETO/PAUSE<br />
          {body ? `${slTierTag(body.analysisTier)} · ${body.scoreVersion || ""} · gen ${(body.generationId || "").slice(0, 18)}` : "—"}</div></div>

      <div className="scanbar" role="tablist" aria-label="Short-Lab sections">
        <button className={"chip" + (activeTab === "candidates" ? " on" : "")} role="tab" aria-selected={activeTab === "candidates"}
          onClick={() => setActiveTab("candidates")}>{L("sl_tab_candidates")}</button>
        <button className={"chip" + (activeTab === "evidence" ? " on" : "")} role="tab" aria-selected={activeTab === "evidence"}
          onClick={() => setActiveTab("evidence")}>{L("sl_tab_evidence")}</button>
        {generationId && <span className="sbcap" data-testid="shortlab-pinned-gen">{L("sl_generation_pinned")} {(generationId || "").slice(0, 18)}</span>}
      </div>

      {newGenId && activeTab === "candidates" && (
        <div className="provline" data-testid="shortlab-newgen">
          <span className="tag hot">{L("sl_new_gen")}</span>
          <span>{L("sl_generation_pinned")} {(generationId || (body && body.generationId) || "").slice(0, 12)} → {(newGenId || "").slice(0, 12)}</span>
          <button className="chip" onClick={switchToNewGen}>{L("sl_switch_new_gen")}</button>
        </div>
      )}

      {activeTab === "evidence" && (
        typeof ShortLabEvidence !== "undefined"
          ? <ShortLabEvidence filters={generationId ? { generationId } : {}} />
          : <div className="reason">Evidence · {L("sl_evidence_loading")}</div>
      )}

      {activeTab === "candidates" && (
      <div>
      <div className="scanbar sl-filters">
        <span className="label">STATUS</span>
        <select aria-label={L("sl_aria_status")} value={filters.status} onChange={(e) => setF("status", e.target.value)}>
          {SL_STATUS_OPTS.map((o) => <option key={o} value={o}>{o === "" ? L("sl_filter_all") : o}</option>)}
        </select>
        <span className="label">EXEC</span>
        <select aria-label={L("sl_aria_exec")} value={filters.executionStatus} onChange={(e) => setF("executionStatus", e.target.value)}>
          {SL_EXEC_OPTS.map((o) => <option key={o} value={o}>{o === "" ? L("sl_filter_all") : o}</option>)}
        </select>
        <span className="label">PROFILE</span>
        <select aria-label={L("sl_aria_profile")} value={filters.profile} onChange={(e) => setF("profile", e.target.value)}>
          {SL_PROFILE_OPTS.map((o) => <option key={o} value={o}>{o === "" ? L("sl_filter_all") : o}</option>)}
        </select>
        <input className="pname" aria-label={L("sl_aria_category")} placeholder={L("sl_ph_category")} value={filters.category}
          onChange={(e) => setF("category", e.target.value)} />
        <input className="pname" aria-label={L("sl_aria_min_ltss")} placeholder="min LTSS" inputMode="decimal" value={filters.minLtss}
          onChange={(e) => setF("minLtss", e.target.value)} />
        <input className="pname" aria-label={L("sl_aria_min_entry")} placeholder="min Entry" inputMode="decimal" value={filters.minEntry}
          onChange={(e) => setF("minEntry", e.target.value)} />
        <input className="pname" aria-label={L("sl_aria_min_funding")} placeholder={L("sl_ph_min_funding")} inputMode="decimal" value={filters.minFundingPct}
          title={L("sl_title_min_funding")}
          onChange={(e) => setF("minFundingPct", e.target.value)} />
        <input className="pname" aria-label={L("sl_aria_ath_min")} placeholder={L("sl_ph_ath_min")} inputMode="decimal" value={filters.athMinPct}
          onChange={(e) => setF("athMinPct", e.target.value)} />
        <input className="pname" aria-label={L("sl_aria_ath_max")} placeholder={L("sl_ph_ath_max")} inputMode="decimal" value={filters.athMaxPct}
          onChange={(e) => setF("athMaxPct", e.target.value)} />
        <input className="pname" aria-label={L("sl_aria_min_dq")} placeholder="min DQ" inputMode="decimal" value={filters.minDQ}
          onChange={(e) => setF("minDQ", e.target.value)} />
        <select aria-label={L("sl_aria_sort")} value={filters.sort} onChange={(e) => setF("sort", e.target.value)}>
          {SL_SORT_OPTS.map((o) => <option key={o} value={o}>{o === "" ? L("sl_default_sort") : o}</option>)}
        </select>
        {filters.sort && (
          <button className="chip" aria-label={L("sl_sort_dir")} onClick={() => setF("order", filters.order === "asc" ? "desc" : "asc")}>
            {filters.order === "asc" ? "▲ ASC" : "▼ DESC"}</button>
        )}
        <button className="cta" disabled={refreshing} onClick={doRefresh} title="POST /api/short/refresh">
          {refreshing ? L("sl_refreshing") : L("sl_refresh")}</button>
        {job && !job.error && <span className="sbmsg" data-testid="shortlab-job">job {String(job.jobId).slice(0, 12)}{job.existing ? " · " + L("sl_job_reused") : ""}{jobText ? ` · ${jobText}` : ""}</span>}
        {refreshing && jobText === "RUNNING" && <span className="sbmsg" data-testid="shortlab-job-running">{L("sl_refresh_polling", job && job.jobId ? String(job.jobId).slice(0, 8) : "")}</span>}
        {jobStatus && jobStatus.status === "FAILED" && <span className="sbmsg bad" data-testid="shortlab-job-failed">{L("sl_job_failed")}{jobStatus.errorCode ? ` · ${jobStatus.errorCode}` : ""}</span>}
        {jobStatus && jobStatus.status === "TIMEOUT" && <span className="sbmsg bad" data-testid="shortlab-job-timeout">{L("sl_job_timeout")}</span>}
        {job && job.error && <span className="sbmsg bad">refresh: {job.error}</span>}
        {formErr && <span className="sbmsg bad" role="alert">{formErr}</span>}
        <span className="sbcap">{SL_DEFAULT_SORT_CAPTION}</span>
      </div>

      {keepErr && (
        <div className="provline" data-testid="shortlab-kept-stale" role="alert">
          <span className="tag hot">STALE</span>
          <span>{L("sl_fetch_failed_kept")}</span>
          <span className="sd-err">{String(keepErr).slice(0, 200)}</span>
        </div>
      )}

      {(showStaleChip || (body && total != null)) && (
        <div className="provline" data-testid="shortlab-provline">
          <span className="pl-k">SHORT LAB</span>
          <span>{total != null ? L("sl_prov_total", total) : ""}{L("sl_prov_rows", items.length)}{(body.generationId || "").slice(0, 18)}</span>
          {showStaleChip && <span className="tag hot" data-testid="shortlab-stale" title={L("sl_stale_title")}>STALE × {staleCount > 0 ? staleCount : items.length}</span>}
          <span className="pl-note">{L("sl_stale_note")}</span>
        </div>
      )}

      <div className="panel"><div className="ph"><span className="tick">▸</span>{L("sl_candidates")}
        <span className="rt">{loading ? L("sl_loading") : `${offset + 1}–${offset + items.length}${total != null ? ` / ${total}` : ""}`}</span></div>
        <div className="pb" style={{ padding: 0 }}>
          {loading
            ? <div className="state" style={{ height: 220 }}><div className="spin" /><div>{L("sl_candidates_loading")}</div></div>
            : <ShortLabTable items={items} sort={filters.sort} order={filters.order}
              onSort={toggleSort}
              onPick={(s) => setSelected(s)} />}
        </div></div>

      {!loading && total != null && total > 0 && (
        <div className="scanbar">
          <button className="chip" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - SL_PAGE_LIMIT))}>{L("sl_prev")}</button>
          <span className="sbcap" style={{ marginLeft: 0 }}>{L("sl_page", Math.floor(offset / SL_PAGE_LIMIT) + 1, Math.max(1, Math.ceil(total / SL_PAGE_LIMIT)))}</span>
          <button className="chip" disabled={offset + SL_PAGE_LIMIT >= total} onClick={() => setOffset(offset + SL_PAGE_LIMIT)}>{L("sl_next")}</button>
        </div>
      )}
      </div>
      )}
    </div>
  );
}
