/* ============================================================================
   DIVE INTO CRYPTO — DESKTOP · TR/EN/ZH i18n catalog (LANE-7)
   Loaded BEFORE desktop-app.jsx in the bundle order (see build.mjs FILES).
   Exposes on the global object:
     - window.DIVE_I18N  { tr: {...}, en: {...}, zh: {...} } — raw catalogs
     - window.diveLang()                            — active code, "zh"|"tr"|"en"
     - window.setDiveLang(code)                     — persist + notify listeners
     - window.L(key, ...args)                       — translate; {0},{1}… replaced
   The language persists under localStorage "dive_lang"; default is "zh" and the
   TR values are the legacy on-screen strings VERBATIM, so TR rendering is
   unchanged when explicitly selected. Missing keys fall back to ZH, then the key.
   ========================================================================== */
(function () {
  const G = (typeof window !== "undefined") ? window : globalThis;
  const KEY = "dive_lang";
  const LANGS = ["zh", "tr", "en"];
  const DEFAULT_LANG = "zh";

  const TR = {
    /* ── nav rail / strip ── */
    nav_scan: "TARA", nav_panel: "PANEL", nav_flow: "OI·L/S", nav_sig: "SİNYAL",
    nav_evidence: "KANIT", nav_compare: "KIYAS", nav_map: "HARİTA", nav_portfolio: "PORTFÖY",
    nav_structure: "YAPI", nav_logs: "LOG", nav_settings: "AYAR", nav_shortlab: "SHORT LAB",
    strip_live: "CANLI", strip_indicators: "İNDİKATÖR", strip_tf: "TF",
    strip_scan: "TARAMA", search_ph: "Sembol ara (örn. SOL)…",
    a11y_refresh: "Yenile", a11y_palette: "Komut paleti (Ctrl+K)", a11y_palette_open: "Komut paleti aç (Ctrl+K)",

    /* ── TARA (scan) view ── */
    scan_kicker: "MANUEL TARAMA", scan_h1: "Piyasa Süpürmesi",
    scan_meta1: "TÜM BINANCE USDT-M · 12 TF", scan_meta2: "KONSENSÜS · 60 İNDİKATÖR + 3 OVERLAY",
    scan_universe: "EVREN", scan_deep: "DERİN TARAMA (async)", scan_compare: "KIYASLA",
    scan_presets: "ÖN AYARLAR", scan_save: "KAYDET", scan_rename: "YENİDEN ADLANDIR",
    scan_export: "DIŞA AKTAR", scan_import: "İÇE AKTAR",
    scan_results: "SIRALANMIŞ SONUÇLAR", scan_survivors: "HAYATTA KALAN", scan_scanned: "TARANDI",
    scan_col_sym: "SEMBOL", scan_col_verdict: "KARAR", scan_col_ch: "FİYAT·24S",
    scan_col_conf: "GÜVEN", scan_col_score: "PUAN",
    scan_agree: "UYUM", scan_stability: "KARARLILIK", scan_12tf: "12 ZAMAN DİLİMİ",
    scan_running: "Tarama çalışıyor…",

    /* ── PANEL view ── */
    panel_kicker: "SEMBOL DERİNLİK", panel_meta1: "1H BİRİNCİL · 12 TF KONSENSÜS", panel_risk: "RİSK:",
    panel_consensus: "KONSENSÜS KARARI", panel_indtable: "İNDİKATÖR TABLOSU",
    panel_micro: "FUTURES MİKROYAPI", panel_regime: "REJİM & KONFLUENS",

    /* ── KANIT (evidence) view ── */
    ev_kicker: "MOTOR ÖZ-DERECELENDİRME", ev_h1: "Kanıt Panosu",
    ev_meta: "RAPOR-ONLY · AĞIRLIKLAR OTOMATİK DEĞİŞMEZ",
    ev_horizon: "UFUK", ev_window: "PENCERE",
    ev_grade_now: "ŞİMDİ NOTALA", ev_grading: "NOTALANIYOR",
    ev_win_7d: "7G", ev_win_30d: "30G", ev_win_all: "TÜMÜ", ev_neutral: "NÖTR",

    /* ── stats gate label (data.js · sgsHitLabel) ── */
    sgs_gate_small: "yetersiz örnek (n={0} < {1})",

    /* ── KIYAS (compare) view ── */
    cmp_kicker: "SENKRON KIYAS", cmp_h1: "Kıyas",
    cmp_meta1: "2–4 SEMBOL · ORTAK ZAMAN EKSENİ", cmp_meta2: "HİZALAMA SONDAN İNDEKSLE",
    cmp_add: "SEMBOL EKLE", cmp_open: "PANELİ AÇ", cmp_conf: "GÜVEN",

    /* ── HARİTA (map) view ── */
    map_kicker: "PİYASA MOZAİĞİ", map_h1: "Harita", map_meta: "RENK=KARAR · YOĞUNLUK=GÜVEN",
    map_mode_scan: "TARAMA", map_mode_universe: "EVREN", map_cluster: "KÜME KONTURU",

    /* ── PORTFÖY view ── */
    pf_kicker: "YEREL DEFTER", pf_h1: "Portföy",
    pf_caption: "giriş fiyatı cihazında saklanır · borsa bağlantısı yok",
    pf_add: "KONUM EKLE", pf_edit: "KONUM DÜZENLE", pf_positions: "KONUMLAR",
    pf_add_btn: "EKLE", pf_update_btn: "GÜNCELLE", pf_cancel: "VAZGEÇ",
    pf_sym: "SEMBOL", pf_dir: "YÖN", pf_entry: "GİRİŞ", pf_size: "MİKTAR", pf_now: "ŞİMDİ",
    pf_pnl: "K/Z %", pf_verdict: "KARAR", pf_action: "İŞLEM",
    pf_rows: "SATIR", pf_live: "CANLI FİYATLI",

    /* ── YAPI (structure) view ── */
    st_kicker: "PİYASA YAPISI", st_h1: "Yapı", st_meta: "BTC BETA · KORELASYON KÜMELERİ",
    st_verified: "İNDEKS DOĞRULANDI", st_unverified: "ETİKET DOĞRULANMADI",
    st_unclustered: "kümelenmemiş", st_symbols: "SEMBOL", st_beta_avg: "β-ort",

    /* ── deep-scan progress panel ── */
    sp_title: "DERİN TARAMA · İZLEME", sp_retry: "TEKRAR DENE",
    sp_reload: "SONUCU YENİDEN YÜKLE", sp_back: "SENKRON TARAMAYA DÖN", sp_cancel: "VAZGEÇ",

    /* ── settings (AYAR) view ── */
    set_kicker: "AYARLAR", set_h1: "Görünüm", set_theme: "TEMA", set_engine: "MOTOR",
    set_lang: "DİL",

    /* ── command palette ── */
    pal_scan: "TARA · tarayıcı", pal_panel: "PANEL · sembol derinliği", pal_flow: "OI·L/S · akış",
    pal_sig: "SİNYAL · kısa liste", pal_evidence: "KANIT · öz-derecelendirme",
    pal_compare: "KIYAS · yan yana", pal_map: "HARİTA · mozaik", pal_portfolio: "PORTFÖY · yerel defter",
    pal_structure: "YAPI · kümeler", pal_logs: "LOG · bağlantı", pal_settings: "AYAR · görünüm",
    pal_shortlab: "SHORT LAB · tarama",
    pal_kind_lang: "DİL", pal_lang_tr: "dil: TR", pal_lang_en: "dil: EN", pal_lang_zh: "dil: ZH",
  };

  const EN = {
    nav_scan: "SCAN", nav_panel: "PANEL", nav_flow: "OI·L/S", nav_sig: "SIGNAL",
    nav_evidence: "EVIDENCE", nav_compare: "COMPARE", nav_map: "MAP", nav_portfolio: "PORTFOLIO",
    nav_structure: "STRUCTURE", nav_logs: "LOG", nav_settings: "SETTINGS", nav_shortlab: "SHORT LAB",
    strip_live: "LIVE", strip_indicators: "INDICATORS", strip_tf: "TF",
    strip_scan: "SCANNING", search_ph: "Search symbol (e.g. SOL)…",
    a11y_refresh: "Refresh", a11y_palette: "Command palette (Ctrl+K)", a11y_palette_open: "Open command palette (Ctrl+K)",

    scan_kicker: "MANUAL SCAN", scan_h1: "Market Sweep",
    scan_meta1: "ALL BINANCE USDT-M · 12 TF", scan_meta2: "CONSENSUS · 60 INDICATORS + 3 OVERLAYS",
    scan_universe: "UNIVERSE", scan_deep: "DEEP SCAN (async)", scan_compare: "COMPARE",
    scan_presets: "PRESETS", scan_save: "SAVE", scan_rename: "RENAME",
    scan_export: "EXPORT", scan_import: "IMPORT",
    scan_results: "RANKED RESULTS", scan_survivors: "SURVIVORS", scan_scanned: "SCANNED",
    scan_col_sym: "SYMBOL", scan_col_verdict: "VERDICT", scan_col_ch: "PRICE·24H",
    scan_col_conf: "CONF", scan_col_score: "SCORE",
    scan_agree: "AGREE", scan_stability: "STABILITY", scan_12tf: "12 TIMEFRAMES",
    scan_running: "Scan running…",

    panel_kicker: "SYMBOL DEPTH", panel_meta1: "1H PRIMARY · 12 TF CONSENSUS", panel_risk: "RISK:",
    panel_consensus: "CONSENSUS VERDICT", panel_indtable: "INDICATOR TABLE",
    panel_micro: "FUTURES MICROSTRUCTURE", panel_regime: "REGIME & CONFLUENCE",

    ev_kicker: "ENGINE SELF-GRADING", ev_h1: "Evidence Board",
    ev_meta: "REPORT-ONLY · WEIGHTS NEVER AUTO-CHANGE",
    ev_horizon: "HORIZON", ev_window: "WINDOW",
    ev_grade_now: "GRADE NOW", ev_grading: "GRADING",
    ev_win_7d: "7D", ev_win_30d: "30D", ev_win_all: "ALL", ev_neutral: "NEUTRAL",

    sgs_gate_small: "insufficient sample (n={0} < {1})",

    cmp_kicker: "SYNC COMPARE", cmp_h1: "Compare",
    cmp_meta1: "2–4 SYMBOLS · SHARED TIME AXIS", cmp_meta2: "ALIGNED FROM THE END",
    cmp_add: "ADD SYMBOL", cmp_open: "OPEN PANEL", cmp_conf: "CONF",

    map_kicker: "MARKET MOSAIC", map_h1: "Map", map_meta: "COLOR=VERDICT · INTENSITY=CONF",
    map_mode_scan: "SCAN", map_mode_universe: "UNIVERSE", map_cluster: "CLUSTER OUTLINE",

    pf_kicker: "LOCAL LEDGER", pf_h1: "Portfolio",
    pf_caption: "entries live on this device · no exchange connection",
    pf_add: "ADD POSITION", pf_edit: "EDIT POSITION", pf_positions: "POSITIONS",
    pf_add_btn: "ADD", pf_update_btn: "UPDATE", pf_cancel: "CANCEL",
    pf_sym: "SYMBOL", pf_entry: "ENTRY", pf_size: "SIZE", pf_now: "NOW",
    pf_pnl: "P&L %", pf_verdict: "VERDICT", pf_action: "ACTION",
    pf_rows: "ROWS", pf_live: "LIVE-PRICED",

    st_kicker: "MARKET STRUCTURE", st_h1: "Structure", st_meta: "BTC BETA · CORRELATION CLUSTERS",
    st_verified: "INDEX VERIFIED", st_unverified: "LABEL UNVERIFIED",
    st_unclustered: "unclustered", st_symbols: "SYMBOLS", st_beta_avg: "β-avg",

    sp_title: "DEEP SCAN · MONITOR", sp_retry: "RETRY",
    sp_reload: "RELOAD RESULT", sp_back: "BACK TO SYNC SCAN", sp_cancel: "CANCEL",

    set_kicker: "SETTINGS", set_h1: "Appearance", set_theme: "THEME", set_engine: "ENGINE",
    set_lang: "LANGUAGE",

    pal_scan: "SCAN · scanner", pal_panel: "PANEL · symbol depth", pal_flow: "OI·L/S · flow",
    pal_sig: "SIGNAL · shortlist", pal_evidence: "EVIDENCE · self-grading",
    pal_compare: "COMPARE · side by side", pal_map: "MAP · mosaic", pal_portfolio: "PORTFOLIO · local ledger",
    pal_structure: "STRUCTURE · clusters", pal_logs: "LOG · connection", pal_settings: "SETTINGS · view",
    pal_shortlab: "SHORT LAB · scanner",
    pal_kind_lang: "LANG", pal_lang_tr: "language: TR", pal_lang_en: "language: EN", pal_lang_zh: "language: ZH",
  };

  const ZH = {
    /* ── 导航栏 ── */
    nav_scan: "扫描", nav_panel: "面板", nav_flow: "OI·L/S", nav_sig: "信号",
    nav_evidence: "证据", nav_compare: "对比", nav_map: "热力图", nav_portfolio: "组合",
    nav_structure: "结构", nav_logs: "日志", nav_settings: "设置", nav_shortlab: "SHORT LAB",
    strip_live: "实时", strip_indicators: "指标", strip_tf: "周期",
    strip_scan: "扫描中", search_ph: "搜索币种（例如 SOL）…",
    a11y_refresh: "刷新", a11y_palette: "命令面板 (Ctrl+K)", a11y_palette_open: "打开命令面板 (Ctrl+K)",

    /* ── 扫描视图 ── */
    scan_kicker: "手动扫描", scan_h1: "市场扫描",
    scan_meta1: "全部 BINANCE USDT-M · 12 周期", scan_meta2: "共识 · 60 指标 + 3 叠加",
    scan_universe: "全市场", scan_deep: "深度扫描 (async)", scan_compare: "对比",
    scan_presets: "预设", scan_save: "保存", scan_rename: "重命名",
    scan_export: "导出", scan_import: "导入",
    scan_results: "排序结果", scan_survivors: "入选", scan_scanned: "已扫描",
    scan_col_sym: "币种", scan_col_verdict: "结论", scan_col_ch: "价格·24H",
    scan_col_conf: "置信度", scan_col_score: "评分",
    scan_agree: "一致性", scan_stability: "稳定性", scan_12tf: "12 个周期",
    scan_running: "扫描运行中…",

    /* ── 面板视图 ── */
    panel_kicker: "币种深度", panel_meta1: "1H 主周期 · 12 周期共识", panel_risk: "风险：",
    panel_consensus: "共识结论", panel_indtable: "指标表",
    panel_micro: "期货微结构", panel_regime: "机制与汇流",

    /* ── 证据视图 ── */
    ev_kicker: "引擎自评", ev_h1: "证据看板",
    ev_meta: "仅报告 · 权重永不自动变更",
    ev_horizon: "期限", ev_window: "窗口",
    ev_grade_now: "立即评分", ev_grading: "评分中",
    ev_win_7d: "7天", ev_win_30d: "30天", ev_win_all: "全部", ev_neutral: "中性",

    /* ── 统计门控标签 ── */
    sgs_gate_small: "样本不足 (n={0} < {1})",

    /* ── 对比视图 ── */
    cmp_kicker: "同步对比", cmp_h1: "对比",
    cmp_meta1: "2–4 个币种 · 共享时间轴", cmp_meta2: "从末端对齐",
    cmp_add: "添加币种", cmp_open: "打开面板", cmp_conf: "置信度",

    /* ── 热力图视图 ── */
    map_kicker: "市场拼图", map_h1: "热力图", map_meta: "颜色=结论 · 浓度=置信度",
    map_mode_scan: "扫描", map_mode_universe: "全市场", map_cluster: "聚类轮廓",

    /* ── 组合视图 ── */
    pf_kicker: "本地账本", pf_h1: "组合",
    pf_caption: "持仓只存本机 · 不连接交易所",
    pf_add: "添加持仓", pf_edit: "编辑持仓", pf_positions: "持仓",
    pf_add_btn: "添加", pf_update_btn: "更新", pf_cancel: "取消",
    pf_sym: "币种", pf_dir: "方向", pf_entry: "入场", pf_size: "数量", pf_now: "现价",
    pf_pnl: "盈亏 %", pf_verdict: "结论", pf_action: "操作",
    pf_rows: "行数", pf_live: "实时计价",

    /* ── 结构视图 ── */
    st_kicker: "市场结构", st_h1: "结构", st_meta: "BTC BETA · 相关性聚类",
    st_verified: "指数已验证", st_unverified: "标签未验证",
    st_unclustered: "未聚类", st_symbols: "币种", st_beta_avg: "β-均值",

    /* ── 深度扫描进度 ── */
    sp_title: "深度扫描 · 监控", sp_retry: "重试",
    sp_reload: "重新加载结果", sp_back: "返回同步扫描", sp_cancel: "取消",

    /* ── 设置视图 ── */
    set_kicker: "设置", set_h1: "外观", set_theme: "主题", set_engine: "引擎",
    set_lang: "语言",

    /* ── 命令面板 ── */
    pal_scan: "扫描 · 扫描器", pal_panel: "面板 · 币种深度", pal_flow: "OI·L/S · 资金流",
    pal_sig: "信号 · 短名单", pal_evidence: "证据 · 自评",
    pal_compare: "对比 · 并排", pal_map: "热力图 · 拼图", pal_portfolio: "组合 · 本地账本",
    pal_structure: "结构 · 聚类", pal_logs: "日志 · 连接", pal_settings: "设置 · 外观",
    pal_shortlab: "SHORT LAB · 做空扫描",
    pal_kind_lang: "语言", pal_lang_tr: "语言：土耳其语", pal_lang_en: "语言：英语", pal_lang_zh: "语言：中文",

    /* ── Short Lab 视图 ── */
    sl_title: "做空研究",
    sl_unavailable_title: "不可用 · UNAVAILABLE", sl_error_title: "出错 · ERROR",
    sl_err_unavailable_body: "Short-Lab 服务暂无响应（数据库未迁移或运行时关闭）。其他页面继续工作——本视图不编造数据，不回退 mock。",
    sl_err_body: "候选列表加载失败（过滤器 422 请检查阈值）。",
    sl_retry: "重试 · RETRY",
    sl_err_min_ltss: "LTSS 最小值须为 0–100",
    sl_err_min_entry: "Entry 最小值须为 0–100",
    sl_err_min_funding: "资金费率最小值须为数字（例如 0.5）",
    sl_err_ath_nan: "ATH 回撤须为数字（例如 -70 … -40）",
    sl_err_ath_range: "ATH 回撤区间须为 -100…0",
    sl_err_ath_order: "ATH 回撤须满足最小值 ≤ 最大值",
    sl_err_min_dq: "数据质量最小值须为 0–100",
    sl_filter_all: "全部", sl_default_sort: "默认 §25.1", sl_sort_dir: "排序方向",
    sl_refresh: "刷新 · REFRESH", sl_refreshing: "刷新中…",
    sl_job_reused: "现有任务已复用 (existing)",
    sl_stale_title: "READY 输入已过时：顶层 STALE + NOT_READY (READY_INPUT_STALE)。非关键过时仅见详情 › 数据源。",
    sl_stale_note: "非关键过时不标 STALE — 见详情 › DATA SOURCES。",
    sl_candidates: "候选 · CANDIDATES",
    sl_loading: "加载中…", sl_candidates_loading: "候选加载中…",
    sl_prev: "← 上一页", sl_next: "下一页 →",
    sl_page: "第 {0} 页 / 共 {1} 页 · generation 固定（第二页用同一 generationId）",
    sl_prov_total: "共 {0} 个候选 · ", sl_prov_rows: "{0} 行 · gen ",
    sl_aria_status: "状态筛选", sl_aria_exec: "执行状态筛选", sl_aria_profile: "画像筛选",
    sl_aria_category: "分类筛选", sl_ph_category: "分类（例如 MEME）",
    sl_aria_min_ltss: "最小 LTSS", sl_aria_min_entry: "最小 Entry",
    sl_aria_min_funding: "最小资金费率 %", sl_ph_min_funding: "最小资金费 %（例如 0.5）",
    sl_title_min_funding: "30D 累计资金费率下限，输入百分数（0.5 = 0.5%）；传给 API 的是小数",
    sl_aria_ath_min: "ATH 回撤最小 %", sl_ph_ath_min: "ATH 回撤最小 %（-70）",
    sl_aria_ath_max: "ATH 回撤最大 %", sl_ph_ath_max: "ATH 回撤最大 %（-40）",
    sl_aria_min_dq: "最小 DQ", sl_aria_sort: "排序",
    sl_detail_kicker: "SHORT LAB · 详情",
    sl_detail_loading: "Short-Lab 详情加载中…",
    sl_detail_unavailable_body: "Short-Lab 服务暂无响应。其他页面继续工作——本视图不编造数据。",
    sl_detail_error_body: "详情加载失败。",
    sl_back_list: "← 返回列表",
    sl_no_risk: "未触发规则 · 快照无风险码。",
    sl_apr_sub: "历史简单年化 · hist. simple annual.",
    sl_gcap_valuation: "不可用字段显示 N/A（无市场）或 —（无数据），永不为 0。",
    sl_tokenomics_placeholder: "Phase 5 — 解锁/社交 provider 未接入 · LITE 下不接线，不编造数值。",
    sl_entry_not_computed: "未计算 Entry — entryScore 为 null（原因见上）· entry not computed.",
    sl_entry_not_computed_short: "未计算",
    sl_no_source_meta: "快照无来源元数据 · no source meta on snapshot",
    sl_gcap_na: "N/A = 已确认缺失（例如无现货市场）· UNAVAILABLE = 暂时不可用，两者分开显示。",
    sl_col_symbol: "币种",
    sl_sort_by_column: "按{0}列排序",
    sl_open_detail: "打开{0}做空详情",
    sl_empty: "没有符合筛选的候选 — 放宽阈值 · no candidates match these filters.",
    /* ── F08 换代刷新与 Evidence 子页 ── */
    sl_tab_candidates: "候选",
    sl_tab_evidence: "证据",
    sl_refresh_polling: "任务轮询中… ({0})",
    sl_refresh_timeout: "刷新超时 — 保留旧数据显示 STALE",
    sl_refresh_failed: "刷新失败 — 保留旧数据显示 STALE",
    sl_refresh_succeeded: "刷新成功 — 已切新代",
    sl_new_gen: "有新数据 · NEW",
    sl_switch_new_gen: "切换到新代",
    sl_generation_pinned: "当前固定代",
    sl_job_running: "任务运行中",
    sl_job_failed: "任务失败",
    sl_job_timeout: "任务超时",
    sl_fetch_failed_kept: "加载失败 — 保留旧数据并标 STALE",
    sl_evidence_kicker: "SHORT LAB · 证据",
    sl_evidence_title: "远期证据",
    sl_evidence_loading: "证据加载中…",
    sl_evidence_unavailable_body: "远期证据暂无（grader 未接线或无已完成代）。原因见下 —— 本视图不编造数据，不回退 mock。",
    sl_evidence_error_body: "证据加载失败。",
    sl_evidence_empty: "暂无证据样本 — 等待 grader 产出。",
    sl_evidence_total: "共 {0} 个样本",
    sl_evidence_horizon: "期限",
  };

  const SL_TR = {
    sl_title: "Short Araştırma",
    sl_unavailable_title: "KULLANILAMIYOR · UNAVAILABLE", sl_error_title: "HATASI · ERROR",
    sl_err_unavailable_body: "Short-Lab servisi şu an yanıt vermiyor (DB migrate olmadı ya da runtime down). Diğer sayfalar çalışmaya devam eder — bu görünüm veri uydurmaz, mock'a düşmez.",
    sl_err_body: "Aday listesi yüklenemedi (filtre 422 ise eşikleri kontrol et).",
    sl_retry: "TEKRAR DENE · RETRY",
    sl_err_min_ltss: "min LTSS 0–100 olmalı",
    sl_err_min_entry: "min Entry 0–100 olmalı",
    sl_err_min_funding: "min funding % sayı olmalı (örn. 0.5)",
    sl_err_ath_nan: "ATH DD % sayı olmalı (örn. -70 … -40)",
    sl_err_ath_range: "ATH DD aralığı -100…0 olmalı",
    sl_err_ath_order: "ATH DD min ≤ max olmalı",
    sl_err_min_dq: "min DQ 0–100 olmalı",
    sl_filter_all: "TÜMÜ", sl_default_sort: "VARSAYILAN §25.1", sl_sort_dir: "sıralama yönü",
    sl_refresh: "YENİLE · REFRESH", sl_refreshing: "YENİLENİYOR…",
    sl_job_reused: "mevcut iş yeniden kullanıldı (existing)",
    sl_stale_title: "READY girdisi bayatladı: üst seviye STALE + NOT_READY (READY_INPUT_STALE). Kritik-olmayan bayatlık yalnızca detay › veri kaynaklarında.",
    sl_stale_note: "Kritik-olmayan bayatlık satırı STALE yapmaz — detay › DATA SOURCES içinde gösterilir.",
    sl_candidates: "ADAYLAR · CANDIDATES",
    sl_loading: "yükleniyor…", sl_candidates_loading: "Adaylar yükleniyor…",
    sl_prev: "← ÖNCEKİ", sl_next: "SONRAKİ →",
    sl_page: "sayfa {0} / {1} · generation sabitli (ikinci sayfa aynı generationId ile)",
    sl_prov_total: "{0} aday · ", sl_prov_rows: "{0} satır · gen ",
    sl_aria_status: "status filtresi", sl_aria_exec: "execution status filtresi", sl_aria_profile: "profile filtresi",
    sl_aria_category: "kategori filtresi", sl_ph_category: "kategori (örn. MEME)",
    sl_aria_min_ltss: "min LTSS", sl_aria_min_entry: "min Entry",
    sl_aria_min_funding: "min funding %", sl_ph_min_funding: "min fund % (örn. 0.5)",
    sl_title_min_funding: "30D kümülatif funding alt eşiği, yüzde yazılır (0.5 = %0.5); API'ye ondalık gider",
    sl_aria_ath_min: "ATH DD min %", sl_ph_ath_min: "ATH DD min % (-70)",
    sl_aria_ath_max: "ATH DD max %", sl_ph_ath_max: "ATH DD max % (-40)",
    sl_aria_min_dq: "min DQ", sl_aria_sort: "sıralama",
    sl_detail_kicker: "SHORT LAB · DETAY",
    sl_detail_loading: "Short-Lab detayı yükleniyor…",
    sl_detail_unavailable_body: "Short-Lab servisi şu an yanıt vermiyor. Başka sayfalar çalışmaya devam eder — bu görünüm veri uydurmaz.",
    sl_detail_error_body: "Detay yüklenemedi.",
    sl_back_list: "← LİSTE",
    sl_no_risk: "Kural tetiklenmedi · no risk codes on this snapshot.",
    sl_apr_sub: "tarihsel basit yıllıklandırma · hist. simple annual.",
    sl_gcap_valuation: "Kullanılamayan alan N/A (piyasa yok) ya da — (veri yok) gösterir · unavailable fields show N/A or —, never 0.",
    sl_tokenomics_placeholder: "Phase 5 — unlock/social provider bağlı değil · not wired in LITE, no fabricated values.",
    sl_entry_not_computed: "Entry hesaplanmadı — entryScore null (nedenler yukarıda) · entry not computed.",
    sl_entry_not_computed_short: "not computed",
    sl_no_source_meta: "kaynak meta yok · no source meta on snapshot",
    sl_gcap_na: "N/A = teyitli yokluk (örn. spot piyasası yok) · UNAVAILABLE = geçici erişilemezlik. İkisi ayrı gösterilir.",
    sl_col_symbol: "SEMBOL",
    sl_sort_by_column: "{0} sütununa göre sırala",
    sl_open_detail: "{0} short-lab detayını aç",
    sl_empty: "Filtreye uyan aday yok — eşikleri gevşet · no candidates match these filters.",
    sl_tab_candidates: "ADAYLAR",
    sl_tab_evidence: "KANIT",
    sl_refresh_polling: "iş sorgulanıyor… ({0})",
    sl_refresh_timeout: "yenileme zaman aşımı — eski veri STALE ile korunuyor",
    sl_refresh_failed: "yenileme başarısız — eski veri STALE ile korunuyor",
    sl_refresh_succeeded: "yenileme başarılı — yeni generation'a geçildi",
    sl_new_gen: "YENİ VERİ VAR · NEW",
    sl_switch_new_gen: "yeni generation'a geç",
    sl_generation_pinned: "sabitli generation",
    sl_job_running: "iş çalışıyor",
    sl_job_failed: "iş başarısız",
    sl_job_timeout: "iş zaman aşımı",
    sl_fetch_failed_kept: "yükleme başarısız — eski veri STALE ile korunuyor",
    sl_evidence_kicker: "SHORT LAB · KANIT",
    sl_evidence_title: "İleri kanıt",
    sl_evidence_loading: "kanıt yükleniyor…",
    sl_evidence_unavailable_body: "ileri kanıt yok (grader bağlı değil ya da tamamlanmış generation yok). Neden aşağıda — bu görünüm veri uydurmaz, mock'a düşmez.",
    sl_evidence_error_body: "kanıt yüklenemedi.",
    sl_evidence_empty: "kanıt örneği yok — grader çıktısı bekleniyor.",
    sl_evidence_total: "{0} örnek",
    sl_evidence_horizon: "vade",
  };

  const SL_EN = {
    sl_title: "Short Research",
    sl_unavailable_title: "UNAVAILABLE", sl_error_title: "ERROR",
    sl_err_unavailable_body: "Short-Lab service is not responding (DB not migrated or runtime down). Other pages keep working — this view never fabricates data or falls back to mock.",
    sl_err_body: "Candidate list failed to load (check thresholds on filter 422).",
    sl_retry: "RETRY",
    sl_err_min_ltss: "min LTSS must be 0–100",
    sl_err_min_entry: "min Entry must be 0–100",
    sl_err_min_funding: "min funding % must be a number (e.g. 0.5)",
    sl_err_ath_nan: "ATH DD % must be a number (e.g. -70 … -40)",
    sl_err_ath_range: "ATH DD range must be -100…0",
    sl_err_ath_order: "ATH DD min must be ≤ max",
    sl_err_min_dq: "min DQ must be 0–100",
    sl_filter_all: "ALL", sl_default_sort: "DEFAULT §25.1", sl_sort_dir: "sort direction",
    sl_refresh: "REFRESH", sl_refreshing: "REFRESHING…",
    sl_job_reused: "reused the in-flight job (existing)",
    sl_stale_title: "a READY input went stale: top-level STALE + NOT_READY (READY_INPUT_STALE). Non-critical staleness only in detail › data sources.",
    sl_stale_note: "Non-critical staleness never marks the row STALE — see detail › DATA SOURCES.",
    sl_candidates: "CANDIDATES",
    sl_loading: "loading…", sl_candidates_loading: "Loading candidates…",
    sl_prev: "← PREV", sl_next: "NEXT →",
    sl_page: "page {0} of {1} · pinned generation (page 2 reuses the same generationId)",
    sl_prov_total: "{0} candidates · ", sl_prov_rows: "{0} rows · gen ",
    sl_aria_status: "status filter", sl_aria_exec: "execution status filter", sl_aria_profile: "profile filter",
    sl_aria_category: "category filter", sl_ph_category: "category (e.g. MEME)",
    sl_aria_min_ltss: "min LTSS", sl_aria_min_entry: "min Entry",
    sl_aria_min_funding: "min funding %", sl_ph_min_funding: "min fund % (e.g. 0.5)",
    sl_title_min_funding: "30D cumulative funding floor, written as percent (0.5 = 0.5%); sent to the API as decimal",
    sl_aria_ath_min: "ATH DD min %", sl_ph_ath_min: "ATH DD min % (-70)",
    sl_aria_ath_max: "ATH DD max %", sl_ph_ath_max: "ATH DD max % (-40)",
    sl_aria_min_dq: "min DQ", sl_aria_sort: "sort",
    sl_detail_kicker: "SHORT LAB · DETAIL",
    sl_detail_loading: "Loading Short-Lab detail…",
    sl_detail_unavailable_body: "Short-Lab service is not responding. Other pages keep working — this view never fabricates data.",
    sl_detail_error_body: "Detail failed to load.",
    sl_back_list: "← LIST",
    sl_no_risk: "No rules triggered · no risk codes on this snapshot.",
    sl_apr_sub: "hist. simple annual.",
    sl_gcap_valuation: "Unavailable fields show N/A (no market) or — (no data), never 0.",
    sl_tokenomics_placeholder: "Phase 5 — unlock/social provider not wired in LITE, no fabricated values.",
    sl_entry_not_computed: "Entry not computed — entryScore is null (reasons above).",
    sl_entry_not_computed_short: "not computed",
    sl_no_source_meta: "no source meta on snapshot",
    sl_gcap_na: "N/A = confirmed absence (e.g. no spot market) · UNAVAILABLE = temporarily unreachable. Shown separately.",
    sl_col_symbol: "SYMBOL",
    sl_sort_by_column: "sort by {0} column",
    sl_open_detail: "open {0} short-lab detail",
    sl_empty: "No candidates match these filters — loosen the thresholds.",
    sl_tab_candidates: "CANDIDATES",
    sl_tab_evidence: "EVIDENCE",
    sl_refresh_polling: "polling job… ({0})",
    sl_refresh_timeout: "refresh timed out — old data kept as STALE",
    sl_refresh_failed: "refresh failed — old data kept as STALE",
    sl_refresh_succeeded: "refresh succeeded — switched to the new generation",
    sl_new_gen: "NEW DATA AVAILABLE · NEW",
    sl_switch_new_gen: "switch to the new generation",
    sl_generation_pinned: "pinned generation",
    sl_job_running: "job running",
    sl_job_failed: "job failed",
    sl_job_timeout: "job timed out",
    sl_fetch_failed_kept: "load failed — old data kept as STALE",
    sl_evidence_kicker: "SHORT LAB · EVIDENCE",
    sl_evidence_title: "Forward evidence",
    sl_evidence_loading: "Loading evidence…",
    sl_evidence_unavailable_body: "Forward evidence is unavailable (grader not wired or no completed generation). Reason below — this view never fabricates data or falls back to mock.",
    sl_evidence_error_body: "Evidence failed to load.",
    sl_evidence_empty: "No evidence samples yet — waiting for grader output.",
    sl_evidence_total: "{0} samples",
    sl_evidence_horizon: "horizon",
  };

  G.DIVE_I18N = { tr: TR, en: EN, zh: ZH };
  for (const k of Object.keys(SL_TR)) {
    if (TR[k] == null) TR[k] = SL_TR[k];
    if (EN[k] == null) EN[k] = SL_EN[k];
  }

  /* ── H09 hedge chrome (design B32/B34/B35): ZH default, TR verbatim legacy
     style, EN plain. Target/Actual/Estimated/Confirmed/Reference/User-entered
     stay distinct in every language; DRAFT/LIMITED vs ACTIVE never share a
     label; ACK ≠ resolve; offline/notification/expired copy is explicit. ── */
  const HEDGE_ZH = {
    hedge_tab_funding: "资金费率", hedge_tab_planner: "对冲规划", hedge_tab_monitor: "持仓监控", hedge_tab_alerts: "告警",
    hedge_funding_kicker: "SHORT LAB · 资金费率", hedge_funding_title: "资金费率机会",
    hedge_historical_note: "历史费率 · Historical funding, 非已实现收益",
    hedge_funding_loading: "资金费率加载中…",
    hedge_funding_unavailable_body: "资金费率机会暂无（后端 503 或未接线）。保留可信旧数据并标 STALE —— 本视图不编造数据，不回退 mock。",
    hedge_funding_error_body: "资金费率加载失败。",
    hedge_funding_empty: "暂无资金费率机会 — 放宽筛选或等待刷新。",
    hedge_col_funding_7d: "资金费 7D（历史）", hedge_col_funding_30d: "资金费 30D（历史）", hedge_col_funding_90d: "资金费 90D（历史）",
    hedge_aria_min_fcs: "最小 FCS", hedge_aria_min_funding: "最小资金费率 %", hedge_ph_min_funding: "最小资金费 %（例如 0.5）",
    hedge_aria_min_ratio: "最小正费率比例 %", hedge_aria_venue: "场所筛选", hedge_aria_readiness: "就绪状态筛选",
    hedge_err_min_fcs: "FCS 最小值须为 0–100", hedge_err_min_funding: "资金费率最小值须为数字（例如 0.5）", hedge_err_min_ratio: "正费率比例须为 0–100",
    hedge_offline: "网络不可达或后端离线 — 检查本地后端是否运行，旧数据已标 STALE，本视图不编造数据。",
    hedge_stale_kept: "加载失败 — 保留旧数据并标 STALE",
    hedge_planner_kicker: "SHORT LAB · 对冲规划", hedge_planner_title: "对冲规划",
    hedge_planner_meta: "只计算、不下单 · 数量为字符串 · 参考价仅参考",
    hedge_inputs_title: "头寸 / 风险输入", hedge_ph_liq: "强平价（用户录入）",
    hedge_load_venues: "加载场所报价", hedge_simulating: "模拟中…", hedge_simulate_btn: "模拟 · SIMULATE",
    hedge_reload_sim: "重读模拟", hedge_relative_hint: "RELATIVE 二选一：直接填对冲比例，或填上涨压力 + 最大方向损失反推；同时提交返回 422。",
    hedge_venues_title: "场所报价（参考）", hedge_venues_empty: "暂无可用场所 — 检查身份/深度/provider 状态。",
    hedge_venues_unavailable_body: "场所报价暂无（503）。对应场所标 UNAVAILABLE，不影响原扫描。",
    hedge_venues_error_body: "场所报价加载失败。",
    hedge_reference_buy: "参考买入", hedge_reference_sell: "参考卖出",
    hedge_sim_error_body: "模拟失败 — 检查输入合法性与报价新鲜度。",
    hedge_result_title: "模拟结果（参考）",
    hedge_target_ratio: "目标对冲比例 Target", hedge_target_futures_qty: "目标期货量 Target", hedge_target_spot_qty: "目标现货量 Target",
    hedge_readiness: "就绪状态", hedge_risk_validation: "风险校验", hedge_monitoring_capability: "监控能力",
    hedge_plan_safety: "计划安全分", hedge_estimated_cost: "估算往返成本 Estimated", hedge_break_even: "回本（估算） Estimated",
    hedge_expired_body: "模拟已过期 Expired — 冻结快照仍可读，但不可据此新建 READY 计划；请重新模拟（新 ID）。",
    hedge_resimulate_btn: "重新模拟（新 ID）", hedge_quote_generated_at: "报价生成于", hedge_quote_expires_at: "报价过期于",
    hedge_no_liq_guide: "无用户录入强平价 — 可按 DRAFT + LIMITED 规划并登记真实成交；请在交易所界面核对强平价后补录，补录前不隐藏资产。",
    hedge_indicative_guide: "链上/Alpha 报价为 indicative — 可按 DRAFT + LIMITED 规划；请刷新双向可退出报价后补项，补录前不隐藏资产。",
    hedge_draft_limited_note: "DRAFT / LIMITED 为可规划态，与 ACTIVE 真实持仓分开；LIMITED 需补项后才能 READY。",
    hedge_order_guide_title: "人工执行指引（两腿）", hedge_reference_price: "参考价 Reference",
    hedge_price_reference_only: "价格仅参考", hedge_must_place_manually: "须用户手工下单",
    hedge_no_stop: "无平台止损单支持 PLATFORM_STOP_UNSUPPORTED — 仅软件提醒，不替代平台订单；请用户在交易平台自行设置止损/止盈。",
    hedge_create_plan_btn: "创建计划（冻结模拟）", hedge_quote_expired_body: "报价已过期 QUOTE_EXPIRED — 请重新模拟（新 ID），旧模拟仍可读。",
    hedge_plan_error_body: "计划创建失败。", hedge_plan_created: "计划已创建",
    hedge_monitor_kicker: "SHORT LAB · 持仓监控", hedge_monitor_title: "持仓监控",
    hedge_monitor_loading: "监控加载中…",
    hedge_monitor_unavailable_body: "监控暂无（503）。保留旧快照并标 STALE —— 本视图不编造数据。",
    hedge_monitor_error_body: "监控加载失败。",
    hedge_monitor_degraded: "监控已降级 MONITOR_DEGRADED — 数据过期或场所不可用；持仓状态不变，请复核后再操作。",
    hedge_target_title: "目标 Target（计划）", hedge_actual_title: "实际 Actual（成交+监控）",
    hedge_actual_ratio: "实际对冲比例 Actual", hedge_residual_short: "剩余净空头 Residual",
    hedge_liq_distance: "强平距离", hedge_user_entered_liq: "用户录入强平价 User-entered",
    hedge_estimated_settled: "估算已结算 Funding Estimated", hedge_projected_next: "预计下期 Funding Projected",
    hedge_projected_note: "预计下期不计入已结算 accrued，仅参考。",
    hedge_basis_pnl: "基差 PnL", hedge_spot_pnl: "现货 PnL", hedge_futures_pnl: "期货 PnL",
    hedge_known_fees: "已知费用 Confirmed", hedge_estimated_exit: "估算退出成本 Estimated",
    hedge_net_before: "退出前净值（估算）", hedge_net_after: "退出后净值（估算）",
    hedge_recommended_action: "建议动作", hedge_recommended_note: "退出建议不改写账本持仓状态",
    hedge_orphan_warning: "孤腿警告 ORPHAN_LEG — 仅一腿成交，请成对处理剩余腿",
    hedge_pair_exit_hint: "正常退出须两腿成对关闭",
    hedge_legs_title: "实际成交腿 Actual", hedge_apply_leg_title: "登记实际成交（用户录入）",
    hedge_apply_leg_btn: "登记成交", hedge_qty_string_hint: "数量保持字符串原样提交，不转 Number；极小值如 0.000000000000000001 精确保留。",
    hedge_activate_btn: "激活", hedge_close_btn: "关闭",
    hedge_alerts_kicker: "SHORT LAB · 告警", hedge_alerts_title: "告警中心",
    hedge_alerts_loading: "告警加载中…",
    hedge_alerts_unavailable_body: "告警暂无（503）。保留旧列表并标 STALE —— 本视图不编造数据。",
    hedge_alerts_error_body: "告警加载失败。", hedge_alerts_empty: "暂无告警。",
    hedge_notif_enable: "启用浏览器通知", hedge_notif_state: "通知权限",
    hedge_notif_denied: "浏览器通知被拒绝或未授权 — 仅用户主动授予后可用；软件停止/断网期间不宣称保护仍在。",
    hedge_ack_note: "ACK 为已确认，不等于解决 RESOLVED；条件恢复需连续成功 tick 才解决。",
    hedge_app_stopped_note: "应用退出/睡眠/断网期间无法监控或可靠通知；重启后恢复账本并补抓历史，不补发实时告警。",
    hedge_ack_btn: "确认 ACK", hedge_no_action: "无建议动作",
    hedge_evidence_kicker: "SHORT LAB · 对冲证据", hedge_evidence_title: "对冲证据",
    hedge_evidence_loading: "对冲证据加载中…",
    hedge_evidence_unavailable_body: "对冲证据暂无（503 HEDGE_EVIDENCE_UNAVAILABLE）。原因见下 —— 本视图不编造数据，不回退 mock。",
    hedge_evidence_error_body: "对冲证据加载失败。", hedge_evidence_empty: "暂无对冲证据样本 — 等待 grader 产出（零样本返回空 bucket，不虚构统计）。",
    hedge_evidence_total: "共 {0} 个样本",
    hedge_evidence_tab_directional: "方向性", hedge_evidence_tab_hedge: "对冲",
  };
  const HEDGE_TR = {
    hedge_tab_funding: "FONLAMA", hedge_tab_planner: "HEDGE PLANI", hedge_tab_monitor: "İZLEME", hedge_tab_alerts: "ALARMLAR",
    hedge_funding_kicker: "SHORT LAB · FONLAMA", hedge_funding_title: "Fonlama Fırsatları",
    hedge_historical_note: "tarihsel oranlar · Historical funding, gerçekleşmiş kâr değil",
    hedge_funding_loading: "fonlama yükleniyor…",
    hedge_funding_unavailable_body: "fonlama fırsatı yok (backend 503 ya da bağlı değil). Güvenilir eski veri STALE ile korunur — bu görünüm veri uydurmaz, mock'a düşmez.",
    hedge_funding_error_body: "fonlama yüklenemedi.",
    hedge_funding_empty: "fonlama fırsatı yok — filtreyi gevşet ya da yenilemeyi bekle.",
    hedge_col_funding_7d: "fonlama 7G (tarihsel)", hedge_col_funding_30d: "fonlama 30G (tarihsel)", hedge_col_funding_90d: "fonlama 90G (tarihsel)",
    hedge_aria_min_fcs: "min FCS", hedge_aria_min_funding: "min fonlama %", hedge_ph_min_funding: "min fon % (örn. 0.5)",
    hedge_aria_min_ratio: "min pozitif oran %", hedge_aria_venue: "venue filtresi", hedge_aria_readiness: "hazırlık filtresi",
    hedge_err_min_fcs: "min FCS 0–100 olmalı", hedge_err_min_funding: "min fonlama % sayı olmalı (örn. 0.5)", hedge_err_min_ratio: "pozitif oran 0–100 olmalı",
    hedge_offline: "ağ erişilemiyor ya da backend çevrimdışı — yerel backend'i kontrol et, eski veri STALE işaretli, bu görünüm veri uydurmaz.",
    hedge_stale_kept: "yükleme başarısız — eski veri STALE ile korunuyor",
    hedge_planner_kicker: "SHORT LAB · HEDGE PLANI", hedge_planner_title: "Hedge Planı",
    hedge_planner_meta: "yalnızca hesap — emir yok · miktar string · referans fiyat yalnızca referans",
    hedge_inputs_title: "POZİSYON / RİSK GİRDİLERİ", hedge_ph_liq: "likidasyon fiyatı (kullanıcı girişi)",
    hedge_load_venues: "venue kotasyonlarını yükle", hedge_simulating: "simüle ediliyor…", hedge_simulate_btn: "SİMÜLE ET · SIMULATE",
    hedge_reload_sim: "simülasyonu yeniden oku", hedge_relative_hint: "RELATIVE ikiden birini seç: doğrudan oran ya da stres artışı + maks yönsel kayıp; ikisi birlikte 422.",
    hedge_venues_title: "venue kotasyonları (referans)", hedge_venues_empty: "kullanılabilir venue yok — kimlik/derinlik/provider durumunu kontrol et.",
    hedge_venues_unavailable_body: "venue kotasyonu yok (503). İlgili venue UNAVAILABLE işaretlenir, taramayı etkilemez.",
    hedge_venues_error_body: "venue kotasyonu yüklenemedi.",
    hedge_reference_buy: "referans alış", hedge_reference_sell: "referans satış",
    hedge_sim_error_body: "simülasyon başarısız — girdileri ve kotasyon tazeliğini kontrol et.",
    hedge_result_title: "simülasyon sonucu (referans)",
    hedge_target_ratio: "hedef oran Target", hedge_target_futures_qty: "hedef futures miktar Target", hedge_target_spot_qty: "hedef spot miktar Target",
    hedge_readiness: "hazırlık", hedge_risk_validation: "risk doğrulama", hedge_monitoring_capability: "izleme kapasitesi",
    hedge_plan_safety: "plan güvenlik skoru", hedge_estimated_cost: "tahmini tur maliyeti Estimated", hedge_break_even: "başa-baş (tahmini) Estimated",
    hedge_expired_body: "simülasyon süresi doldu Expired — dondurulmuş snapshot okunabilir ama READY plan kurulamaz; yeniden simüle et (yeni ID).",
    hedge_resimulate_btn: "yeniden simüle et (yeni ID)", hedge_quote_generated_at: "kotasyon üretildi", hedge_quote_expires_at: "kotasyon bitiyor",
    hedge_no_liq_guide: "kullanıcı likidasyon fiyatı yok — DRAFT + LIMITED olarak planlanıp gerçek fill işlenebilir; borsadan kontrol edip tamamla, tamamlanmadan varlık gizlenmez.",
    hedge_indicative_guide: "zincir/Alpha kotasyonu indicative — DRAFT + LIMITED olarak planlanabilir; çift yönlü çıkış kotasyonunu yenileyip tamamla, varlık gizlenmez.",
    hedge_draft_limited_note: "DRAFT / LIMITED planlanabilir durumdur, ACTIVE gerçek pozisyondan ayrıdır; LIMITED önce tamamlanmalıdır.",
    hedge_order_guide_title: "manuel icra kılavuzu (iki bacak)", hedge_reference_price: "referans fiyat Reference",
    hedge_price_reference_only: "fiyat yalnızca referans", hedge_must_place_manually: "emirleri kullanıcı manuel girmelidir",
    hedge_no_stop: "platform stop desteği yok PLATFORM_STOP_UNSUPPORTED — yalnızca yazılım uyarısı, platform emrinin yerini tutmaz; stop/take-profit'i platformda kur.",
    hedge_create_plan_btn: "plan oluştur (simülasyonu dondur)", hedge_quote_expired_body: "kotasyon süresi doldu QUOTE_EXPIRED — yeniden simüle et (yeni ID), eski simülasyon okunabilir.",
    hedge_plan_error_body: "plan oluşturulamadı.", hedge_plan_created: "plan oluşturuldu",
    hedge_monitor_kicker: "SHORT LAB · İZLEME", hedge_monitor_title: "Pozisyon İzleme",
    hedge_monitor_loading: "izleme yükleniyor…",
    hedge_monitor_unavailable_body: "izleme yok (503). Eski snapshot STALE ile korunur — bu görünüm veri uydurmaz.",
    hedge_monitor_error_body: "izleme yüklenemedi.",
    hedge_monitor_degraded: "izleme düştü MONITOR_DEGRADED — veri bayat ya da venue erişilemez; pozisyon durumu değişmez, kontrol edip işlem yap.",
    hedge_target_title: "hedef Target (plan)", hedge_actual_title: "gerçekleşen Actual (fill+izleme)",
    hedge_actual_ratio: "gerçekleşen oran Actual", hedge_residual_short: "kalan net short Residual",
    hedge_liq_distance: "likidasyon mesafesi", hedge_user_entered_liq: "kullanıcı likidasyon fiyatı User-entered",
    hedge_estimated_settled: "tahmini settle fonlama Estimated", hedge_projected_next: "öngörülen sonraki fonlama Projected",
    hedge_projected_note: "öngörülen değer accrued'a eklenmez, yalnızca referans.",
    hedge_basis_pnl: "basis PnL", hedge_spot_pnl: "spot PnL", hedge_futures_pnl: "futures PnL",
    hedge_known_fees: "bilinen ücretler Confirmed", hedge_estimated_exit: "tahmini çıkış maliyeti Estimated",
    hedge_net_before: "çıkış öncesi net (tahmini)", hedge_net_after: "çıkış sonrası net (tahmini)",
    hedge_recommended_action: "önerilen aksiyon", hedge_recommended_note: "çıkış önerisi defter pozisyonunu değiştirmez",
    hedge_orphan_warning: "yetim bacak uyarısı ORPHAN_LEG — tek bacak gerçekleşti, kalan bacağı eşli kapat",
    hedge_pair_exit_hint: "normal çıkış iki bacağı eşli kapatır",
    hedge_legs_title: "gerçekleşen bacaklar Actual", hedge_apply_leg_title: "gerçek fill işle (kullanıcı girişi)",
    hedge_apply_leg_btn: "fill işle", hedge_qty_string_hint: "miktar string olarak aynen gönderilir, Number'a çevrilmez; 0.000000000000000001 gibi küçük değerler korunur.",
    hedge_activate_btn: "aktive et", hedge_close_btn: "kapat",
    hedge_alerts_kicker: "SHORT LAB · ALARMLAR", hedge_alerts_title: "Alarm Merkezi",
    hedge_alerts_loading: "alarmlar yükleniyor…",
    hedge_alerts_unavailable_body: "alarm yok (503). Eski liste STALE ile korunur — bu görünüm veri uydurmaz.",
    hedge_alerts_error_body: "alarmlar yüklenemedi.", hedge_alerts_empty: "alarm yok.",
    hedge_notif_enable: "tarayıcı bildirimini aç", hedge_notif_state: "bildirim izni",
    hedge_notif_denied: "tarayıcı bildirimi reddedildi ya da yetkisiz — yalnızca kullanıcı izniyle; yazılım dururken/çevrimdışıyken koruma sürüyor denmez.",
    hedge_ack_note: "ACK teyittir, çözüm RESOLVED değildir; çözüm için art arda başarılı tick gerekir.",
    hedge_app_stopped_note: "uygulama kapalıyken/uykudayken/çevrimdışıyken izleme ve güvenilir bildirim olmaz; yeniden başlayınca defter kurtarılıp tarih tamamlanır, gerçek zamanlı alarm tekrar gönderilmez.",
    hedge_ack_btn: "TEYİT ET ACK", hedge_no_action: "önerilen aksiyon yok",
    hedge_evidence_kicker: "SHORT LAB · HEDGE KANIT", hedge_evidence_title: "Hedge Kanıtı",
    hedge_evidence_loading: "hedge kanıtı yükleniyor…",
    hedge_evidence_unavailable_body: "hedge kanıtı yok (503 HEDGE_EVIDENCE_UNAVAILABLE). Neden aşağıda — bu görünüm veri uydurmaz, mock'a düşmez.",
    hedge_evidence_error_body: "hedge kanıtı yüklenemedi.", hedge_evidence_empty: "hedge kanıt örneği yok — grader çıktısı bekleniyor (sıfır örnek boş bucket döner, istatistik uydurulmaz).",
    hedge_evidence_total: "{0} örnek",
    hedge_evidence_tab_directional: "yönlü", hedge_evidence_tab_hedge: "hedge",
  };
  const HEDGE_EN = {
    hedge_tab_funding: "FUNDING", hedge_tab_planner: "PLANNER", hedge_tab_monitor: "MONITOR", hedge_tab_alerts: "ALERTS",
    hedge_funding_kicker: "SHORT LAB · FUNDING", hedge_funding_title: "Funding Opportunities",
    hedge_historical_note: "historical rates · Historical funding, not realized profit",
    hedge_funding_loading: "Loading funding…",
    hedge_funding_unavailable_body: "Funding opportunities are unavailable (backend 503 or not wired). Trusted old data is kept as STALE — this view never fabricates data or falls back to mock.",
    hedge_funding_error_body: "Funding failed to load.",
    hedge_funding_empty: "No funding opportunities — loosen the filters or wait for a refresh.",
    hedge_col_funding_7d: "funding 7D (Historical)", hedge_col_funding_30d: "funding 30D (Historical)", hedge_col_funding_90d: "funding 90D (Historical)",
    hedge_aria_min_fcs: "min FCS", hedge_aria_min_funding: "min funding %", hedge_ph_min_funding: "min fund % (e.g. 0.5)",
    hedge_aria_min_ratio: "min positive ratio %", hedge_aria_venue: "venue filter", hedge_aria_readiness: "readiness filter",
    hedge_err_min_fcs: "min FCS must be 0–100", hedge_err_min_funding: "min funding % must be a number (e.g. 0.5)", hedge_err_min_ratio: "positive ratio must be 0–100",
    hedge_offline: "Network unreachable or backend offline — check the local backend, old data is marked STALE, this view never fabricates data.",
    hedge_stale_kept: "load failed — old data kept as STALE",
    hedge_planner_kicker: "SHORT LAB · PLANNER", hedge_planner_title: "Hedge Planner",
    hedge_planner_meta: "compute only, no orders · quantities are strings · reference prices are reference only",
    hedge_inputs_title: "POSITION / RISK INPUTS", hedge_ph_liq: "liquidation price (user-entered)",
    hedge_load_venues: "Load venue quotes", hedge_simulating: "SIMULATING…", hedge_simulate_btn: "SIMULATE",
    hedge_reload_sim: "reload simulation", hedge_relative_hint: "RELATIVE is either/or: a direct ratio, or stress-up + max directional loss; both together is 422.",
    hedge_venues_title: "Venue quotes (Reference)", hedge_venues_empty: "No usable venue — check identity/depth/provider status.",
    hedge_venues_unavailable_body: "Venue quotes are unavailable (503). The venue is marked UNAVAILABLE without affecting the scan.",
    hedge_venues_error_body: "Venue quotes failed to load.",
    hedge_reference_buy: "Reference buy", hedge_reference_sell: "Reference sell",
    hedge_sim_error_body: "Simulation failed — check inputs and quote freshness.",
    hedge_result_title: "Simulation result (Reference)",
    hedge_target_ratio: "Target hedge ratio", hedge_target_futures_qty: "Target futures qty", hedge_target_spot_qty: "Target spot qty",
    hedge_readiness: "readiness", hedge_risk_validation: "risk validation", hedge_monitoring_capability: "monitoring capability",
    hedge_plan_safety: "plan safety score", hedge_estimated_cost: "Estimated round-trip cost", hedge_break_even: "break-even (Estimated)",
    hedge_expired_body: "Simulation expired — the frozen snapshot stays readable but cannot back a new READY plan; re-simulate (new ID).",
    hedge_resimulate_btn: "Re-simulate (new ID)", hedge_quote_generated_at: "quote generated at", hedge_quote_expires_at: "quote expires at",
    hedge_no_liq_guide: "No user-entered liquidation price — planning as DRAFT + LIMITED and recording real fills is allowed; verify the price on the exchange and fill it in. The asset is never hidden before that.",
    hedge_indicative_guide: "Chain/Alpha quotes are indicative — planning as DRAFT + LIMITED is allowed; refresh two-sided exit quotes to complete. The asset is never hidden before that.",
    hedge_draft_limited_note: "DRAFT / LIMITED is plannable state, separate from ACTIVE real holdings; LIMITED needs completion before READY.",
    hedge_order_guide_title: "Manual execution guide (two legs)", hedge_reference_price: "Reference price",
    hedge_price_reference_only: "price is reference only", hedge_must_place_manually: "user must place orders manually",
    hedge_no_stop: "No platform stop support PLATFORM_STOP_UNSUPPORTED — software reminders only, not a substitute for platform orders; set stops/take-profits on the venue.",
    hedge_create_plan_btn: "Create plan (freeze simulation)", hedge_quote_expired_body: "Quote expired QUOTE_EXPIRED — re-simulate (new ID); the old simulation stays readable.",
    hedge_plan_error_body: "Plan creation failed.", hedge_plan_created: "Plan created",
    hedge_monitor_kicker: "SHORT LAB · MONITOR", hedge_monitor_title: "Position Monitor",
    hedge_monitor_loading: "Loading monitor…",
    hedge_monitor_unavailable_body: "Monitor is unavailable (503). The old snapshot is kept as STALE — this view never fabricates data.",
    hedge_monitor_error_body: "Monitor failed to load.",
    hedge_monitor_degraded: "Monitor degraded MONITOR_DEGRADED — data stale or venue unavailable; holdings state is unchanged, verify before acting.",
    hedge_target_title: "Target (plan)", hedge_actual_title: "Actual (fills + monitor)",
    hedge_actual_ratio: "Actual hedge ratio", hedge_residual_short: "Residual short",
    hedge_liq_distance: "liquidation distance", hedge_user_entered_liq: "User-entered liquidation price",
    hedge_estimated_settled: "Estimated settled funding", hedge_projected_next: "Projected next funding",
    hedge_projected_note: "Projected is never counted as accrued, reference only.",
    hedge_basis_pnl: "basis PnL", hedge_spot_pnl: "spot PnL", hedge_futures_pnl: "futures PnL",
    hedge_known_fees: "Known fees Confirmed", hedge_estimated_exit: "Estimated exit cost",
    hedge_net_before: "net before exit (Estimated)", hedge_net_after: "net after exit (Estimated)",
    hedge_recommended_action: "recommended action", hedge_recommended_note: "exit advice never rewrites ledger holdings",
    hedge_orphan_warning: "Orphan-leg warning ORPHAN_LEG — only one leg filled, close the remainder in pairs",
    hedge_pair_exit_hint: "normal exits close both legs in pairs",
    hedge_legs_title: "Actual fill legs", hedge_apply_leg_title: "Record actual fills (user-entered)",
    hedge_apply_leg_btn: "Record fill", hedge_qty_string_hint: "Quantities are submitted as strings verbatim, never via Number; tiny values like 0.000000000000000001 are preserved.",
    hedge_activate_btn: "Activate", hedge_close_btn: "Close",
    hedge_alerts_kicker: "SHORT LAB · ALERTS", hedge_alerts_title: "Alert Center",
    hedge_alerts_loading: "Loading alerts…",
    hedge_alerts_unavailable_body: "Alerts are unavailable (503). The old list is kept as STALE — this view never fabricates data.",
    hedge_alerts_error_body: "Alerts failed to load.", hedge_alerts_empty: "No alerts.",
    hedge_notif_enable: "Enable browser notifications", hedge_notif_state: "notification permission",
    hedge_notif_denied: "Browser notifications denied or not granted — user-granted only; never claim protection continues while the app is stopped/offline.",
    hedge_ack_note: "ACK is acknowledgement, not resolution RESOLVED; resolution needs consecutive successful ticks.",
    hedge_app_stopped_note: "No monitoring or reliable notifications while the app is closed/asleep/offline; the ledger recovers on restart and backfills history without re-emitting live alerts.",
    hedge_ack_btn: "ACK", hedge_no_action: "no recommended action",
    hedge_evidence_kicker: "SHORT LAB · HEDGE EVIDENCE", hedge_evidence_title: "Hedge Evidence",
    hedge_evidence_loading: "Loading hedge evidence…",
    hedge_evidence_unavailable_body: "Hedge evidence is unavailable (503 HEDGE_EVIDENCE_UNAVAILABLE). Reason below — this view never fabricates data or falls back to mock.",
    hedge_evidence_error_body: "Hedge evidence failed to load.", hedge_evidence_empty: "No hedge evidence samples yet — waiting for grader output (zero samples return empty buckets, never invented stats).",
    hedge_evidence_total: "{0} samples",
    hedge_evidence_tab_directional: "Directional", hedge_evidence_tab_hedge: "Hedge",
  };
  for (const k of Object.keys(HEDGE_ZH)) {
    if (ZH[k] == null) ZH[k] = HEDGE_ZH[k];
    if (TR[k] == null) TR[k] = HEDGE_TR[k];
    if (EN[k] == null) EN[k] = HEDGE_EN[k];
  }

  G.diveLang = function () {
    try {
      const v = G.localStorage && G.localStorage.getItem(KEY);
      if (v === "tr" || v === "en" || v === "zh") return v;
    } catch (e) { /* storage unavailable → default below */ }
    return DEFAULT_LANG;
  };

  G.setDiveLang = function (code) {
    const v = code === "tr" ? "tr" : code === "en" ? "en" : "zh";
    try { G.localStorage.setItem(KEY, v); } catch (e) { /* storage unavailable → session-only */ }
    if (typeof G.__diveOnLang === "function") G.__diveOnLang();
  };

  /** Translate `key` in the active language; {0}…{n} placeholders via extra args.
      Missing keys fall back to ZH, then to the key itself (never undefined). */
  G.L = function (key) {
    const lang = G.diveLang();
    const table = G.DIVE_I18N[lang] || G.DIVE_I18N[DEFAULT_LANG];
    let s = table[key];
    if (s == null) s = G.DIVE_I18N[DEFAULT_LANG][key];
    if (s == null) return key;
    for (let i = 1; i < arguments.length; i++) {
      s = s.split("{" + (i - 1) + "}").join(String(arguments[i]));
    }
    return s;
  };
})();
