/* ============================================================================
   short-lab — Desktop · scanner table (Task 15 · design §26.2–26.3)

   Pure presentational table: rows are /api/short/candidates items VERBATIM
   (no rescoring, no re-sorting — the server owns §25.1 order). Sorting headers
   only report the requested key upward; the parent re-queries the server.
   EXCLUDED / PAUSED / BLOCKED rows always print their rule codes; null ("—")
   and confirmed N/A ("N/A") never share a cell label.
   ========================================================================== */

function ShortLabTable({ items, sort, order, onSort, onPick }) {
  const rows = Array.isArray(items) ? items : [];
  const wantSort = (k) => { if (onSort) onSort(k); };
  const arrow = (k) => (sort === k ? (order === "asc" ? " ▲" : " ▼") : "");
  const thBtn = (label, k) => (
    <button className="thbtn" onClick={() => wantSort(k)}
      aria-label={L("sl_sort_by_column", label)}>{label}{arrow(k)}</button>
  );
  return (
    <div className="sl-scroll" data-testid="shortlab-table-wrap">
      <table className="rank sl-table" data-testid="shortlab-table">
        <thead><tr>
          <th scope="col">{L("sl_col_symbol")}</th>
          <th scope="col" className="r" aria-sort={sort === "ltss" ? (order === "asc" ? "ascending" : "descending") : undefined}>{thBtn("LTSS", "ltss")}</th>
          <th scope="col" className="r" aria-sort={sort === "entry" ? (order === "asc" ? "ascending" : "descending") : undefined}>{thBtn("ENTRY", "entry")}</th>
          <th scope="col" className="r" aria-sort={sort === "funding30d" ? (order === "asc" ? "ascending" : "descending") : undefined}>{thBtn("30D FUND", "funding30d")}</th>
          <th scope="col" className="r">FUND+</th>
          <th scope="col" className="r">ATH DD</th>
          <th scope="col" className="r">OI/MC</th>
          <th scope="col" className="r">SPOT/FUT</th>
          <th scope="col" className="r" aria-sort={sort === "dataQuality" ? (order === "asc" ? "ascending" : "descending") : undefined}>{thBtn("DATA", "dataQuality")}</th>
          <th scope="col">STATUS</th>
        </tr></thead>
        <tbody>
          {rows.map((it) => {
            const m = (it && it.metrics) || {};
            const avail = (it && it.dataAvailability) || {};
            const status = it.status || "—";
            const tier = slTierTag(it.analysisTier);
            const cats = Array.isArray(it.categories) ? it.categories : [];
            const reasons = Array.isArray(it.reasons) ? it.reasons : [];
            const warnings = Array.isArray(it.warnings) ? it.warnings : [];
            const spotCell = slFieldText(m.futuresSpotVolumeRatio, (v) => slFmtRatio(v), avail.spot);
            return (
              <tr key={(it.snapshotId || it.symbol) + "|" + it.symbol}
                className={"sl-row sl-" + String(status).toLowerCase()}
                tabIndex={0}
                onClick={() => onPick && onPick(it.symbol)}
                onKeyDown={(e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onPick && onPick(it.symbol); } }}
                aria-label={L("sl_open_detail", it.symbol)}>
                <td>
                  <div className="sym">{it.symbol}
                    <small>{it.canonicalId || ""}{it.stale ? " · STALE" : ""}</small>
                  </div>
                  {cats.length > 0 && <span className="tag">{String(cats[0]).toUpperCase()}</span>}
                </td>
                <td className="r score">{slFmtScore(it.ltss)} <span className="tag">{tier}</span></td>
                <td className="r">{slFmtEntry(it.entryScore)}</td>
                <td className="r">{slFmtFundingDecimal(m.funding30d)}</td>
                <td className="r">{slFmtShare(m.positiveFundingRatio30d)}</td>
                <td className="r">{slFmtAthDD(m.athDrawdown)}</td>
                <td className="r">{slFmtRatio(m.oiMarketCapRatio)}</td>
                <td className="r">{spotCell}</td>
                <td className="r">{slFmtDQ(it.dataQuality)}</td>
                <td>
                  <span className={"pill " + (status === "READY" ? "b" : status === "BLOCKED" ? "s" : "n")}>
                    <span className="g" />{slStatusLabel(status, it.analysisTier)}
                  </span>
                  {(status === "PAUSED" || status === "BLOCKED" || status === "EXCLUDED" || reasons.length > 0) && (
                    <div className="sl-reasons" title={reasons.map(slReasonText).join(" · ")}>
                      {reasons.join(" · ") || "—"}
                    </div>
                  )}
                  {warnings.length > 0 && (
                    <div className="sl-warns">{warnings.join(" · ")}</div>
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {rows.length === 0 && (
        <div className="reason">{L("sl_empty")}</div>
      )}
    </div>
  );
}
