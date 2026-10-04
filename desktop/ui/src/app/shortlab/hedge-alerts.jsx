/* ============================================================================
   short-lab — Desktop · Hedge Alerts page (H09 · design B26/B27/B32.11-12)

   Reads ONLY window.DIVE.hedgeAlerts / ackHedgeAlert (data.js). Sorted
   CRITICAL > WARN > INFO. ACK moves OPEN → ACKNOWLEDGED and NEVER to
   RESOLVED (hedge_ack_note copy pins this). Browser Notification is
   user-granted only; denial and app-stopped states render honest copy and
   never claim protection continues. Software-stop / offline / stale copy is
   explicit. AbortController + sequence rejects late responses; 503 never
   mocks. `initial` bypasses fetch for deterministic tests.
   ========================================================================== */

const HEDGE_SEV_ORDER = { CRITICAL: 0, WARN: 1, INFO: 2 };

function HedgeAlerts({ filters, initial }) {
  const [body, setBody] = React.useState(initial && initial.data ? initial.data : null);
  const [err, setErr] = React.useState(initial && initial.error ? initial.error : null);
  const [loading, setLoading] = React.useState(!(initial && (initial.data || initial.error)));
  const [keepErr, setKeepErr] = React.useState(null);
  const [ackMsg, setAckMsg] = React.useState(null);
  const [notifState, setNotifState] = React.useState(
    (typeof Notification !== "undefined" && Notification.permission) ? Notification.permission : "unknown");
  const seqRef = React.useRef(0);
  const abortRef = React.useRef(null);

  const load = React.useCallback(() => {
    if (initial && (initial.data || initial.error)) return;
    const seq = ++seqRef.current;
    if (abortRef.current) { try { abortRef.current.abort(); } catch (e) {} }
    const ctrl = (typeof AbortController !== "undefined") ? new AbortController() : null;
    abortRef.current = ctrl;
    setLoading(true);
    if (!body) setErr(null);
    const opts = ctrl ? { signal: ctrl.signal } : {};
    window.DIVE.hedgeAlerts(filters || {}, opts)
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
  }, [JSON.stringify(filters || {}), body ? "has-body" : "no-body"]);

  React.useEffect(() => { load(); }, [load]);
  React.useEffect(() => () => { if (abortRef.current) { try { abortRef.current.abort(); } catch (e) {} } }, []);

  const doAck = (alertId) => {
    const seq = ++seqRef.current;
    setAckMsg(null);
    window.DIVE.ackHedgeAlert(alertId, {})
      .then((res) => {
        if (seq !== seqRef.current) return;
        setAckMsg(`ACKNOWLEDGED · ${String(alertId).slice(0, 18)}`);
        const updated = res && (res.alert || res);
        setBody((b) => {
          if (!b) return b;
          const items = (b.alerts || b.items || []).map((a) => {
            const aid = a.alertId || a.alert_id;
            if (String(aid) === String(alertId)) {
              return { ...a, state: (updated && (updated.state || updated.State)) || "ACKNOWLEDGED" };
            }
            return a;
          });
          return { ...b, alerts: items, items };
        });
      })
      .catch((e) => { if (seq === seqRef.current) setAckMsg(String((e && e.message) || e)); });
  };

  const askNotif = () => {
    try {
      if (typeof Notification === "undefined" || !Notification.requestPermission) {
        setNotifState("unsupported");
        return;
      }
      Notification.requestPermission().then((p) => setNotifState(p));
    } catch (e) {
      setNotifState("denied");
    }
  };

  if (loading) {
    return <div className="state" style={{ height: 160 }} data-testid="hedge-alerts-loading">
      <div className="spin" /><div>{L("hedge_alerts_loading")}</div></div>;
  }
  if (err && !body) {
    const txt = String(err);
    const unavailable = /503|unavailable/i.test(txt);
    const offline = /Failed to fetch|NetworkError|network|offline/i.test(txt);
    return (
      <div className="state src-down" role="alert" data-testid={unavailable ? "hedge-alerts-unavailable" : "hedge-alerts-error"}>
        <div className="sd-title">ALERTS {unavailable ? L("sl_unavailable_title") : L("sl_error_title")}</div>
        <div className="sd-body">{offline ? L("hedge_offline") : (unavailable ? L("hedge_alerts_unavailable_body") : L("hedge_alerts_error_body"))}</div>
        <div className="sd-err">{txt}</div>
        <button className="cta" onClick={load}>{L("sl_retry")}</button>
      </div>
    );
  }

  const items = ((body && (body.alerts || body.items)) || []).slice().sort((a, b) => {
    const sa = HEDGE_SEV_ORDER[String(a.severity || "").toUpperCase()] != null ? HEDGE_SEV_ORDER[String(a.severity || "").toUpperCase()] : 9;
    const sb = HEDGE_SEV_ORDER[String(b.severity || "").toUpperCase()] != null ? HEDGE_SEV_ORDER[String(b.severity || "").toUpperCase()] : 9;
    return sa - sb;
  });

  return (
    <div data-testid="hedge-alerts">
      <div className="vhead"><span className="kicker">{L("hedge_alerts_kicker")}</span><h1>{L("hedge_alerts_title")}</h1>
        <div className="meta">CRITICAL &gt; WARN &gt; INFO · {items.length}</div></div>
      <div className="scanbar sl-filters">
        <button className="cta" onClick={load}>{L("sl_retry")}</button>
        <button className="chip" onClick={askNotif}>{L("hedge_notif_enable")}</button>
        <span className="sbcap" data-testid="hedge-notif-state">{L("hedge_notif_state")} {notifState}</span>
      </div>
      {(notifState === "denied" || notifState === "default") && (
        <div className="reason" data-testid="hedge-notif-denied">{L("hedge_notif_denied")}</div>
      )}
      <div className="reason" data-testid="hedge-ack-note">{L("hedge_ack_note")}</div>
      <div className="reason">{L("hedge_app_stopped_note")}</div>
      {keepErr && (
        <div className="provline" data-testid="hedge-alerts-kept-stale" role="alert">
          <span className="tag hot">STALE</span><span>{L("hedge_stale_kept")}</span>
          <span className="sd-err">{String(keepErr).slice(0, 200)}</span>
        </div>
      )}
      {ackMsg && <div className="reason" data-testid="hedge-ack-msg">{String(ackMsg).slice(0, 300)}</div>}
      {items.length === 0
        ? <div className="reason" data-testid="hedge-alerts-empty">{L("hedge_alerts_empty")}</div>
        : items.map((a) => {
          const aid = a.alertId || a.alert_id || a.id || JSON.stringify(a).slice(0, 12);
          const code = a.code || a.Code || "—";
          const sev = String(a.severity || "").toUpperCase() || "—";
          const state = String(a.state || "").toUpperCase() || "—";
          const action = a.recommendedAction || a.recommended_action;
          return (
            <div key={String(aid)} className="panel" data-testid={`hedge-alert-${aid}`}>
              <div className="ph"><span className="tick">▸</span>{code}
                <span className="rt"><span className={"tag " + window.HEDGE_FORMAT.alertSeverityClass(sev)}>{sev}</span> · {window.HEDGE_FORMAT.alertStateLabel(state)}</span></div>
              <div className="pb">
                <div className="reason">plan {a.planId || a.plan_id || "—"} · {action ? `${L("hedge_recommended_action")} ${action}` : L("hedge_no_action")}</div>
                {state === "OPEN" && <button className="chip" onClick={() => doAck(aid)}>{L("hedge_ack_btn")}</button>}
                {state === "ACKNOWLEDGED" && <span className="tag" data-testid={`hedge-acked-${aid}`}>ACKNOWLEDGED</span>}
                {state === "RESOLVED" && <span className="tag" data-testid={`hedge-resolved-${aid}`}>RESOLVED</span>}
              </div>
            </div>
          );
        })}
    </div>
  );
}
