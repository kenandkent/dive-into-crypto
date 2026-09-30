/* ============================================================================
   short-lab — Desktop · scanner view (Task 15 · design §26.1–26.3, §26.5)

   Owns filter state and talks to the backend ONLY through
   window.DIVE.shortCandidates / shortDetail / shortRefresh (data.js).
   Filter controls map 1:1 onto /api/short/candidates params
   (min_funding_30d, ath_drawdown_min/max, min_data_quality, …); the funding
   %↔decimal conversion happens in short-lab-format.js, nowhere else.
   No explicit sort → no sort param → server §25.1 default order.
   API failure renders Short-Lab's own UNAVAILABLE page — never mock data,
   never the global DataSourceDown gate. `initial` bypasses fetch for tests.
   ========================================================================== */

const SL_STATUS_OPTS = ["", "READY", "CANDIDATE", "WATCH", "PAUSED", "BLOCKED", "EXCLUDED"];
const SL_EXEC_OPTS = ["", "NOT_READY", "READY", "PAUSED", "BLOCKED"];
const SL_PROFILE_OPTS = ["", "MEME_LITE", "GENERAL_LITE", "LOW_FLOAT_VC_LITE",
  "MEME_FULL", "GENERAL_FULL", "LOW_FLOAT_VC_FULL"];
const SL_SORT_OPTS = ["", "ltss", "entry", "funding30d", "dataQuality"];
const SL_PAGE_LIMIT = 50;

function slDefaultFilters() {
  return {
    status: "", executionStatus: "", profile: "", category: "",
    minLtss: "", minEntry: "", minFundingPct: "", athMinPct: "", athMaxPct: "",
    minDQ: "", sort: "", order: "desc",
  };
}

function ShortLabView({ initial }) {
  const [filters, setFilters] = React.useState(slDefaultFilters());
  const [offset, setOffset] = React.useState(0);
  const [generationId, setGenerationId] = React.useState(null);
  const [selected, setSelected] = React.useState(null);
  const [body, setBody] = React.useState(initial && initial.data ? initial.data : null);
  const [err, setErr] = React.useState(initial && initial.error ? initial.error : null);
  const [loading, setLoading] = React.useState(!(initial && (initial.data || initial.error)));
  const [formErr, setFormErr] = React.useState(null);
  const [job, setJob] = React.useState(null);
  const [refreshing, setRefreshing] = React.useState(false);

  const setF = (k, v) => {
    setFilters((f) => ({ ...f, [k]: v }));
    setOffset(0); setGenerationId(null);   // new filter → unpin pagination
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
      if (!isFinite(n) || n < 0 || n > 100) return { error: "min LTSS 0–100 olmalı" };
      q.minLtss = n;
    }
    if (filters.minEntry !== "") {
      const n = Number(filters.minEntry);
      if (!isFinite(n) || n < 0 || n > 100) return { error: "min Entry 0–100 olmalı" };
      q.minEntry = n;
    }
    if (filters.minFundingPct !== "") {
      const dec = slPctInputToDecimal(filters.minFundingPct);
      if (dec == null) return { error: "min funding % sayı olmalı (örn. 0.5)" };
      q.minFunding30d = dec;   // decimal on the wire; conversion lives in format.js
    }
    if (filters.athMinPct !== "" || filters.athMaxPct !== "") {
      const lo = filters.athMinPct === "" ? null : slPctInputToDecimal(filters.athMinPct);
      const hi = filters.athMaxPct === "" ? null : slPctInputToDecimal(filters.athMaxPct);
      if ((filters.athMinPct !== "" && lo == null) || (filters.athMaxPct !== "" && hi == null))
        return { error: "ATH DD % sayı olmalı (örn. -70 … -40)" };
      if (lo != null && (lo < -1 || lo > 0)) return { error: "ATH DD aralığı -100…0 olmalı" };
      if (hi != null && (hi < -1 || hi > 0)) return { error: "ATH DD aralığı -100…0 olmalı" };
      if (lo != null && hi != null && lo > hi) return { error: "ATH DD min ≤ max olmalı" };
      if (lo != null) q.athDrawdownMin = lo;
      if (hi != null) q.athDrawdownMax = hi;
    }
    if (filters.minDQ !== "") {
      const n = Number(filters.minDQ);
      if (!isFinite(n) || n < 0 || n > 100) return { error: "min DQ 0–100 olmalı" };
      q.minDataQuality = n;
    }
    if (filters.sort) { q.sort = filters.sort; q.order = filters.order || "desc"; }
    return { query: q };
  };

  const fetchPage = React.useCallback(() => {
    if (initial && (initial.data || initial.error)) return;
    const built = buildQuery();
    if (built.error) { setFormErr(built.error); return; }
    setFormErr(null); setLoading(true); setErr(null);
    window.DIVE.shortCandidates(built.query)
      .then((res) => {
        setBody(res); setLoading(false);
        if (res && res.generationId && !generationId) setGenerationId(res.generationId);
        try { window.__diveShortQuery = built.query; } catch (e) { /* poll hint only */ }
      })
      .catch((e) => { setErr((e && e.message) || String(e)); setLoading(false); });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(filters), offset, generationId]);

  React.useEffect(() => { fetchPage(); }, [fetchPage]);

  const doRefresh = () => {
    setRefreshing(true); setJob(null);
    window.DIVE.shortRefresh()
      .then((r) => { setJob(r); setRefreshing(false); fetchPage(); })
      .catch((e) => { setJob({ error: (e && e.message) || String(e) }); setRefreshing(false); });
  };

  if (selected) {
    return <ShortLabDetail symbol={selected} generationId={generationId} onBack={() => setSelected(null)} />;
  }

  const items = (body && body.items) || [];
  const staleCount = items.filter((it) => it && it.stale).length;
  const total = body != null && body.total != null ? body.total : null;

  const errText = err ? String(err) : "";
  if (err) {
    const isUnavailable = /503|shortlab_unavailable/i.test(errText);
    return (
      <div>
        <div className="vhead"><span className="kicker">SHORT LAB</span><h1>Short Araştırma</h1>
          <div className="meta">LTSS · ENTRY · DQ · VETO/PAUSE</div></div>
        <div className="state src-down" role="alert"
          data-testid={isUnavailable ? "shortlab-unavailable" : "shortlab-error"}>
          <div className="sd-title">SHORT LAB {isUnavailable ? "KULLANILAMIYOR · UNAVAILABLE" : "HATASI · ERROR"}</div>
          <div className="sd-body">{isUnavailable
            ? "Short-Lab servisi şu an yanıt vermiyor (DB migrate olmadı ya da runtime down). Diğer sayfalar çalışmaya devam eder — bu görünüm veri uydurmaz, mock'a düşmez."
            : "Aday listesi yüklenemedi (filtre 422 ise eşikleri kontrol et)."}</div>
          <div className="sd-err">{errText}</div>
          <button className="cta" onClick={() => { setErr(null); fetchPage(); }}>TEKRAR DENE · RETRY</button>
        </div>
      </div>
    );
  }

  return (
    <div data-testid="shortlab-view">
      <div className="vhead"><span className="kicker">SHORT LAB</span><h1>Short Araştırma</h1>
        <div className="meta">LTSS · ENTRY · DQ · VETO/PAUSE<br />
          {body ? `${slTierTag(body.analysisTier)} · ${body.scoreVersion || ""} · gen ${(body.generationId || "").slice(0, 18)}` : "—"}</div></div>

      <div className="scanbar sl-filters">
        <span className="label">STATUS</span>
        <select aria-label="status filtresi" value={filters.status} onChange={(e) => setF("status", e.target.value)}>
          {SL_STATUS_OPTS.map((o) => <option key={o} value={o}>{o === "" ? "TÜMÜ" : o}</option>)}
        </select>
        <span className="label">EXEC</span>
        <select aria-label="execution status filtresi" value={filters.executionStatus} onChange={(e) => setF("executionStatus", e.target.value)}>
          {SL_EXEC_OPTS.map((o) => <option key={o} value={o}>{o === "" ? "TÜMÜ" : o}</option>)}
        </select>
        <span className="label">PROFILE</span>
        <select aria-label="profile filtresi" value={filters.profile} onChange={(e) => setF("profile", e.target.value)}>
          {SL_PROFILE_OPTS.map((o) => <option key={o} value={o}>{o === "" ? "TÜMÜ" : o}</option>)}
        </select>
        <input className="pname" aria-label="kategori filtresi" placeholder="kategori (örn. MEME)" value={filters.category}
          onChange={(e) => setF("category", e.target.value)} />
        <input className="pname" aria-label="min LTSS" placeholder="min LTSS" inputMode="decimal" value={filters.minLtss}
          onChange={(e) => setF("minLtss", e.target.value)} />
        <input className="pname" aria-label="min Entry" placeholder="min Entry" inputMode="decimal" value={filters.minEntry}
          onChange={(e) => setF("minEntry", e.target.value)} />
        <input className="pname" aria-label="min funding %" placeholder="min fund % (örn. 0.5)" inputMode="decimal" value={filters.minFundingPct}
          title="30D kümülatif funding alt eşiği, yüzde yazılır (0.5 = %0.5); API'ye ondalık gider"
          onChange={(e) => setF("minFundingPct", e.target.value)} />
        <input className="pname" aria-label="ATH DD min %" placeholder="ATH DD min % (-70)" inputMode="decimal" value={filters.athMinPct}
          onChange={(e) => setF("athMinPct", e.target.value)} />
        <input className="pname" aria-label="ATH DD max %" placeholder="ATH DD max % (-40)" inputMode="decimal" value={filters.athMaxPct}
          onChange={(e) => setF("athMaxPct", e.target.value)} />
        <input className="pname" aria-label="min DQ" placeholder="min DQ" inputMode="decimal" value={filters.minDQ}
          onChange={(e) => setF("minDQ", e.target.value)} />
        <select aria-label="sıralama" value={filters.sort} onChange={(e) => setF("sort", e.target.value)}>
          {SL_SORT_OPTS.map((o) => <option key={o} value={o}>{o === "" ? "VARSAYILAN §25.1" : o}</option>)}
        </select>
        {filters.sort && (
          <button className="chip" aria-label="sıralama yönü" onClick={() => setF("order", filters.order === "asc" ? "desc" : "asc")}>
            {filters.order === "asc" ? "▲ ASC" : "▼ DESC"}</button>
        )}
        <button className="cta" disabled={refreshing} onClick={doRefresh} title="POST /api/short/refresh — aynı iş varsa mevcut job döner">
          {refreshing ? "YENİLENİYOR…" : "YENİLE · REFRESH"}</button>
        {job && !job.error && <span className="sbmsg">job {String(job.jobId).slice(0, 12)}{job.existing ? " · mevcut iş yeniden kullanıldı (existing)" : ""}</span>}
        {job && job.error && <span className="sbmsg bad">refresh: {job.error}</span>}
        {formErr && <span className="sbmsg bad" role="alert">{formErr}</span>}
        <span className="sbcap">{SL_DEFAULT_SORT_CAPTION}</span>
      </div>

      {(staleCount > 0 || (body && total != null)) && (
        <div className="provline" data-testid="shortlab-provline">
          <span className="pl-k">SHORT LAB</span>
          <span>{total != null ? `${total} aday · ` : ""}{items.length} satır · gen {(body.generationId || "").slice(0, 18)}</span>
          {staleCount > 0 && <span className="tag hot" data-testid="shortlab-stale" title="READY girdisi bayatladı: üst seviye STALE + NOT_READY (READY_INPUT_STALE). Kritik-olmayan bayatlık yalnızca detay › veri kaynaklarında.">STALE × {staleCount}</span>}
          <span className="pl-note">Kritik-olmayan bayatlık satırı STALE yapmaz — detay › DATA SOURCES içinde gösterilir.</span>
        </div>
      )}

      <div className="panel"><div className="ph"><span className="tick">▸</span>ADAYLAR · CANDIDATES
        <span className="rt">{loading ? "yükleniyor…" : `${offset + 1}–${offset + items.length}${total != null ? ` / ${total}` : ""}`}</span></div>
        <div className="pb" style={{ padding: 0 }}>
          {loading
            ? <div className="state" style={{ height: 220 }}><div className="spin" /><div>Adaylar yükleniyor…</div></div>
            : <ShortLabTable items={items} sort={filters.sort} order={filters.order}
              onSort={toggleSort}
              onPick={(s) => setSelected(s)} />}
        </div></div>

      {!loading && total != null && total > 0 && (
        <div className="scanbar">
          <button className="chip" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - SL_PAGE_LIMIT))}>← ÖNCEKİ</button>
          <span className="sbcap" style={{ marginLeft: 0 }}>sayfa {Math.floor(offset / SL_PAGE_LIMIT) + 1} / {Math.max(1, Math.ceil(total / SL_PAGE_LIMIT))} · generation sabitli (ikinci sayfa aynı generationId ile)</span>
          <button className="chip" disabled={offset + SL_PAGE_LIMIT >= total} onClick={() => setOffset(offset + SL_PAGE_LIMIT)}>SONRAKİ →</button>
        </div>
      )}
    </div>
  );
}
