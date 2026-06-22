"use strict";

(function () {
  const LS_LANG = "autocoin_lang";

  const I18N = {
    ko: {
      "common.language": "언어",
      "common.live_view": "LIVE View",
      "common.loading": "로딩 중...",
      "common.none": "없음",
      "common.not_available": "N/A",
      "common.no_data": "데이터 없음",
      "common.click_for_details": "상세 보기",
      "common.market_override": "마켓 오버라이드",
      "common.global_default": "글로벌 기본값",
      "common.applied": "적용됨!",
      "common.network_error": "네트워크 오류",
      "common.error": "오류",
      "common.buy": "매수",
      "common.sell": "매도",
      "common.neutral": "중립",

      "nav.dashboard": "대시보드",
      "nav.autoloop": "Autoloop",
      "nav.pingpong": "PingPong",
      "nav.gazua": "Gazua",
      "nav.ladder": "Ladder",
      "nav.lightning": "Lightning",
      "nav.current_window": "현재 창",
      "nav.new_window": "새 창",

      "market_detail.doc_title": "{market} 상세 - Autocoin OS",
      "market_detail.title_detail": "상세",
      "market_detail.go_back": "뒤로 가기",
      "market_detail.overview": "개요",
      "market_detail.current_price": "현재가",
      "market_detail.position": "포지션",
      "market_detail.unrealized_pnl": "미실현 손익",
      "market_detail.active_strategy": "활성 전략",
      "market_detail.open_exchange_chart": "Bybit Chart",
      "market_detail.ai_brain_analysis": "AI / 브레인 분석",
      "market_detail.loading_analysis": "분석 로딩 중...",
      "market_detail.strategy_guard_controls": "전략 & 가드 제어",
      "market_detail.strategy_override": "전략 오버라이드",
      "market_detail.enabled": "활성화",
      "market_detail.ai_default": "AI (기본)",
      "market_detail.apply_strategy": "전략 적용",
      "market_detail.strategy_select_hint": "전략을 선택하면 파라미터가 표시됩니다.",
      "market_detail.guard_overrides": "가드 오버라이드",
      "market_detail.apply_guards": "가드 적용",
      "market_detail.recent_activity": "최근 활동 (원장)",
      "market_detail.last_50_events": "이 마켓 최근 50개 이벤트",
      "market_detail.time": "시간",
      "market_detail.event": "이벤트",
      "market_detail.side": "사이드",
      "market_detail.price_info": "가격 / 정보",
      "market_detail.raw_state": "원시 전략 상태 (JSON)",
      "market_detail.no_market_specified": "마켓이 지정되지 않았습니다",
      "market_detail.no_params_for_mode": "{mode} 파라미터가 없습니다.",
      "market_detail.strategy_saved": "전략 설정이 저장되었습니다.",
      "market_detail.error_saving_strategy": "전략 저장 오류: {error}",
      "market_detail.error_saving_guards": "가드 저장 오류: {error}",
      "market_detail.no_brain_data": "브레인 데이터가 없습니다",
      "market_detail.no_recent_activity": "최근 활동이 없습니다",
      "market_detail.ai_score": "AI 점수",
      "market_detail.volatility": "변동성",
      "market_detail.momentum": "모멘텀",
      "market_detail.trend": "추세",
      "market_detail.vol_change": "거래량 변화",
      "market_detail.strategy_auto_off": "AUTO/OFF",
      "market_detail.load_failed": "로드 실패",
      "market_detail.param.entry_gap_pct": "진입 갭 %",
      "market_detail.param.exit_gap_pct": "청산 갭 %",
      "market_detail.param.ai_influence": "AI 영향도",
      "market_detail.param.rsi_buy": "RSI 매수",
      "market_detail.param.rsi_sell": "RSI 매도",
      "market_detail.param.burst_pct": "급등 %",
      "market_detail.param.window_ticks": "윈도우 (ticks)",
      "market_detail.param.tp_pct": "TP %",
      "market_detail.param.sl_pct": "SL %",
      "market_detail.param.trailing_tp": "트레일링 TP",
      "market_detail.param.trail_dist_pct": "트레일 거리 %",
      "market_detail.param.buy_now": "즉시 매수",
      "market_detail.param.hold_no_sell": "홀드 (매도 금지)",
      "market_detail.param.ai_buy_threshold": "AI 매수 임계값",

      "guard.entry_enabled": "진입 활성",
      "guard.profit_guard": "수익 가드",
      "guard.ceiling_guard": "천장 가드",
      "guard.qty_guard": "수량 가드",
      "guard.ob_guard": "호가 가드",
      "guard.tp_limit_exit": "TP 지정가 청산",

      "strategy.doc_title": "{strategy} 전략 상세 - Autocoin OS",
      "strategy.title_detail": "상세",
      "strategy.stat_markets": "마켓 수",
      "strategy.stat_total_pnl": "총 손익",
      "strategy.stat_avg_ai_score": "평균 AI 점수",
      "strategy.active_markets": "활성 마켓",
      "strategy.running_with": "{strategy} 전략으로 동작 중인 마켓",
      "strategy.ai_insight": "AI 인사이트",
      "strategy.select_market_hint": "위 마켓을 선택하면 상세가 표시됩니다.",
      "strategy.no_markets_running": "{strategy} 실행 중인 마켓이 없습니다",
      "strategy.card_pnl": "손익",
      "strategy.card_ai_score": "AI 점수",
      "strategy.card_no_details": "상세 정보 없음",
      "strategy.badge_pos": "포지션",
      "strategy.badge_wait": "대기",
      "strategy.detail_buy": "매수",
      "strategy.detail_sell": "매도",
      "strategy.detail_stage": "단계",
      "strategy.detail_mom": "모멘텀",
      "strategy.metric_rsi": "RSI",
      "strategy.metric_vol": "변동성",

      "ladder.doc_title": "Ladder 전략 뷰 - Autocoin OS",
      "ladder.title_strategy": "Ladder 전략",
      "ladder.stat_configured_markets": "설정된 마켓",
      "ladder.stat_total_open_orders": "총 오픈 주문",
      "ladder.markets_title": "Ladder 마켓",
      "ladder.markets_sub": "Ladder 설정이 활성화된 마켓입니다. 클릭하면 상세를 봅니다.",
      "ladder.no_markets_configured": "Ladder 전략에 설정된 마켓이 없습니다.",
      "ladder.badge_on": "ON",
      "ladder.badge_off": "OFF",
      "ladder.open_orders": "오픈 주문",
      "ladder.range": "범위",
      "ladder.spacing": "간격",
      "ladder.order_usdt": "Order USDT",
      "ladder.update_failed": "Ladder 뷰 업데이트 실패"
    },
    en: {
      "common.language": "Language",
      "common.live_view": "LIVE View",
      "common.loading": "Loading...",
      "common.none": "None",
      "common.not_available": "N/A",
      "common.no_data": "No data",
      "common.click_for_details": "Click for details",
      "common.market_override": "Market Override",
      "common.global_default": "Global Default",
      "common.applied": "Applied!",
      "common.network_error": "Network error",
      "common.error": "Error",
      "common.buy": "Buy",
      "common.sell": "Sell",
      "common.neutral": "Neutral",

      "nav.dashboard": "Dashboard",
      "nav.autoloop": "Autoloop",
      "nav.pingpong": "PingPong",
      "nav.gazua": "Gazua",
      "nav.ladder": "Ladder",
      "nav.lightning": "Lightning",
      "nav.current_window": "Current Window",
      "nav.new_window": "New Window",

      "market_detail.doc_title": "{market} Detail - Autocoin OS",
      "market_detail.title_detail": "Detail",
      "market_detail.go_back": "Go Back",
      "market_detail.overview": "Overview",
      "market_detail.current_price": "Current Price",
      "market_detail.position": "Position",
      "market_detail.unrealized_pnl": "Unrealized PnL",
      "market_detail.active_strategy": "Active Strategy",
      "market_detail.open_exchange_chart": "Open Bybit Chart",
      "market_detail.ai_brain_analysis": "AI / Brain Analysis",
      "market_detail.loading_analysis": "Loading analysis...",
      "market_detail.strategy_guard_controls": "Strategy & Guard Controls",
      "market_detail.strategy_override": "Strategy Override",
      "market_detail.enabled": "Enabled",
      "market_detail.ai_default": "AI (Default)",
      "market_detail.apply_strategy": "Apply Strategy",
      "market_detail.strategy_select_hint": "Select a strategy to see its parameters.",
      "market_detail.guard_overrides": "Guard Overrides",
      "market_detail.apply_guards": "Apply Guards",
      "market_detail.recent_activity": "Recent Activity (Ledger)",
      "market_detail.last_50_events": "Last 50 events for this market",
      "market_detail.time": "Time",
      "market_detail.event": "Event",
      "market_detail.side": "Side",
      "market_detail.price_info": "Price / Info",
      "market_detail.raw_state": "Raw Strategy State (JSON)",
      "market_detail.no_market_specified": "No market specified",
      "market_detail.no_params_for_mode": "No parameters for {mode}.",
      "market_detail.strategy_saved": "Strategy settings saved.",
      "market_detail.error_saving_strategy": "Error saving strategy: {error}",
      "market_detail.error_saving_guards": "Error saving guards: {error}",
      "market_detail.no_brain_data": "No brain data available",
      "market_detail.no_recent_activity": "No recent activity found",
      "market_detail.ai_score": "AI Score",
      "market_detail.volatility": "Volatility",
      "market_detail.momentum": "Momentum",
      "market_detail.trend": "Trend",
      "market_detail.vol_change": "Vol Change",
      "market_detail.strategy_auto_off": "AUTO/OFF",
      "market_detail.load_failed": "Load failed",
      "market_detail.param.entry_gap_pct": "Entry Gap %",
      "market_detail.param.exit_gap_pct": "Exit Gap %",
      "market_detail.param.ai_influence": "AI Influence",
      "market_detail.param.rsi_buy": "RSI Buy",
      "market_detail.param.rsi_sell": "RSI Sell",
      "market_detail.param.burst_pct": "Burst %",
      "market_detail.param.window_ticks": "Window (ticks)",
      "market_detail.param.tp_pct": "TP %",
      "market_detail.param.sl_pct": "SL %",
      "market_detail.param.trailing_tp": "Trailing TP",
      "market_detail.param.trail_dist_pct": "Trail Dist %",
      "market_detail.param.buy_now": "Buy Now",
      "market_detail.param.hold_no_sell": "Hold (No Sell)",
      "market_detail.param.ai_buy_threshold": "AI Buy Threshold",

      "guard.entry_enabled": "Entry Enabled",
      "guard.profit_guard": "Profit Guard",
      "guard.ceiling_guard": "Ceiling Guard",
      "guard.qty_guard": "Qty Guard",
      "guard.ob_guard": "OB Guard",
      "guard.tp_limit_exit": "TP Limit Exit",

      "strategy.doc_title": "{strategy} Strategy Detail - Autocoin OS",
      "strategy.title_detail": "Detail",
      "strategy.stat_markets": "Markets",
      "strategy.stat_total_pnl": "Total PnL",
      "strategy.stat_avg_ai_score": "Avg AI Score",
      "strategy.active_markets": "Active Markets",
      "strategy.running_with": "Markets running {strategy} strategy",
      "strategy.ai_insight": "AI Insight",
      "strategy.select_market_hint": "Select a market above to see details.",
      "strategy.no_markets_running": "No markets currently running {strategy}",
      "strategy.card_pnl": "PnL",
      "strategy.card_ai_score": "AI Score",
      "strategy.card_no_details": "No details",
      "strategy.badge_pos": "POS",
      "strategy.badge_wait": "WAIT",
      "strategy.detail_buy": "Buy",
      "strategy.detail_sell": "Sell",
      "strategy.detail_stage": "Stage",
      "strategy.detail_mom": "Mom",
      "strategy.metric_rsi": "RSI",
      "strategy.metric_vol": "Vol",

      "ladder.doc_title": "Ladder Strategy View - Autocoin OS",
      "ladder.title_strategy": "Ladder Strategy",
      "ladder.stat_configured_markets": "Configured Markets",
      "ladder.stat_total_open_orders": "Total Open Orders",
      "ladder.markets_title": "Ladder Markets",
      "ladder.markets_sub": "Markets with active Ladder configurations. Click to see details.",
      "ladder.no_markets_configured": "No markets configured for Ladder strategy.",
      "ladder.badge_on": "ON",
      "ladder.badge_off": "OFF",
      "ladder.open_orders": "Open Orders",
      "ladder.range": "Range",
      "ladder.spacing": "Spacing",
      "ladder.order_usdt": "Order USDT",
      "ladder.update_failed": "Failed to update ladder view"
    },
    th: {
      "common.language": "ภาษา",
      "common.live_view": "มุมมอง LIVE",
      "common.loading": "กำลังโหลด...",
      "common.none": "ไม่มี",
      "common.not_available": "N/A",
      "common.no_data": "ไม่มีข้อมูล",
      "common.click_for_details": "คลิกเพื่อดูรายละเอียด",
      "common.market_override": "Override รายมาร์เก็ต",
      "common.global_default": "ค่าเริ่มต้นระบบ",
      "common.applied": "ใช้แล้ว!",
      "common.network_error": "เครือข่ายขัดข้อง",
      "common.error": "ข้อผิดพลาด",
      "common.buy": "ซื้อ",
      "common.sell": "ขาย",
      "common.neutral": "เป็นกลาง",

      "nav.dashboard": "แดชบอร์ด",
      "nav.autoloop": "Autoloop",
      "nav.pingpong": "PingPong",
      "nav.gazua": "Gazua",
      "nav.ladder": "Ladder",
      "nav.lightning": "Lightning",
      "nav.current_window": "หน้าต่างปัจจุบัน",
      "nav.new_window": "หน้าต่างใหม่",

      "market_detail.doc_title": "รายละเอียด {market} - Autocoin OS",
      "market_detail.title_detail": "รายละเอียด",
      "market_detail.go_back": "ย้อนกลับ",
      "market_detail.overview": "ภาพรวม",
      "market_detail.current_price": "ราคาปัจจุบัน",
      "market_detail.position": "โพสิชัน",
      "market_detail.unrealized_pnl": "กำไร/ขาดทุนที่ยังไม่ปิด",
      "market_detail.active_strategy": "กลยุทธ์ที่ใช้งาน",
      "market_detail.open_exchange_chart": "Open Bybit Chart",
      "market_detail.ai_brain_analysis": "วิเคราะห์ AI / Brain",
      "market_detail.loading_analysis": "กำลังโหลดการวิเคราะห์...",
      "market_detail.strategy_guard_controls": "ควบคุม Strategy & Guard",
      "market_detail.strategy_override": "Strategy Override",
      "market_detail.enabled": "เปิดใช้งาน",
      "market_detail.ai_default": "AI (ค่าเริ่มต้น)",
      "market_detail.apply_strategy": "ใช้กลยุทธ์",
      "market_detail.strategy_select_hint": "เลือกกลยุทธ์เพื่อดูพารามิเตอร์",
      "market_detail.guard_overrides": "Guard Overrides",
      "market_detail.apply_guards": "ใช้ Guards",
      "market_detail.recent_activity": "กิจกรรมล่าสุด (Ledger)",
      "market_detail.last_50_events": "50 เหตุการณ์ล่าสุดของมาร์เก็ตนี้",
      "market_detail.time": "เวลา",
      "market_detail.event": "เหตุการณ์",
      "market_detail.side": "ฝั่ง",
      "market_detail.price_info": "ราคา / ข้อมูล",
      "market_detail.raw_state": "สถานะกลยุทธ์ดิบ (JSON)",
      "market_detail.no_market_specified": "ไม่ได้ระบุมาร์เก็ต",
      "market_detail.no_params_for_mode": "ไม่มีพารามิเตอร์สำหรับ {mode}",
      "market_detail.strategy_saved": "บันทึกการตั้งค่ากลยุทธ์แล้ว",
      "market_detail.error_saving_strategy": "บันทึกกลยุทธ์ไม่สำเร็จ: {error}",
      "market_detail.error_saving_guards": "บันทึก Guards ไม่สำเร็จ: {error}",
      "market_detail.no_brain_data": "ไม่มีข้อมูล brain",
      "market_detail.no_recent_activity": "ไม่พบกิจกรรมล่าสุด",
      "market_detail.ai_score": "คะแนน AI",
      "market_detail.volatility": "ความผันผวน",
      "market_detail.momentum": "โมเมนตัม",
      "market_detail.trend": "แนวโน้ม",
      "market_detail.vol_change": "การเปลี่ยนแปลงวอลุ่ม",
      "market_detail.strategy_auto_off": "AUTO/OFF",
      "market_detail.load_failed": "โหลดไม่สำเร็จ",
      "market_detail.param.entry_gap_pct": "ช่องว่างเข้า %",
      "market_detail.param.exit_gap_pct": "ช่องว่างออก %",
      "market_detail.param.ai_influence": "อิทธิพล AI",
      "market_detail.param.rsi_buy": "RSI ซื้อ",
      "market_detail.param.rsi_sell": "RSI ขาย",
      "market_detail.param.burst_pct": "Burst %",
      "market_detail.param.window_ticks": "ช่วง (ticks)",
      "market_detail.param.tp_pct": "TP %",
      "market_detail.param.sl_pct": "SL %",
      "market_detail.param.trailing_tp": "Trailing TP",
      "market_detail.param.trail_dist_pct": "ระยะ Trail %",
      "market_detail.param.buy_now": "ซื้อทันที",
      "market_detail.param.hold_no_sell": "ถือ (ไม่ขาย)",
      "market_detail.param.ai_buy_threshold": "เกณฑ์ AI Buy",

      "guard.entry_enabled": "เปิดการเข้า",
      "guard.profit_guard": "การ์ดกำไร",
      "guard.ceiling_guard": "การ์ดเพดาน",
      "guard.qty_guard": "การ์ดจำนวน",
      "guard.ob_guard": "การ์ดออเดอร์บุ๊ก",
      "guard.tp_limit_exit": "TP Limit Exit",

      "strategy.doc_title": "รายละเอียดกลยุทธ์ {strategy} - Autocoin OS",
      "strategy.title_detail": "รายละเอียด",
      "strategy.stat_markets": "มาร์เก็ต",
      "strategy.stat_total_pnl": "กำไร/ขาดทุนรวม",
      "strategy.stat_avg_ai_score": "คะแนน AI เฉลี่ย",
      "strategy.active_markets": "มาร์เก็ตที่ใช้งาน",
      "strategy.running_with": "มาร์เก็ตที่รันกลยุทธ์ {strategy}",
      "strategy.ai_insight": "AI Insight",
      "strategy.select_market_hint": "เลือกมาร์เก็ตด้านบนเพื่อดูรายละเอียด",
      "strategy.no_markets_running": "ไม่มีมาร์เก็ตที่กำลังรัน {strategy}",
      "strategy.card_pnl": "กำไร/ขาดทุน",
      "strategy.card_ai_score": "คะแนน AI",
      "strategy.card_no_details": "ไม่มีรายละเอียด",
      "strategy.badge_pos": "POS",
      "strategy.badge_wait": "WAIT",
      "strategy.detail_buy": "ซื้อ",
      "strategy.detail_sell": "ขาย",
      "strategy.detail_stage": "ขั้น",
      "strategy.detail_mom": "โมเมนตัม",
      "strategy.metric_rsi": "RSI",
      "strategy.metric_vol": "Vol",

      "ladder.doc_title": "มุมมองกลยุทธ์ Ladder - Autocoin OS",
      "ladder.title_strategy": "กลยุทธ์ Ladder",
      "ladder.stat_configured_markets": "มาร์เก็ตที่ตั้งค่าไว้",
      "ladder.stat_total_open_orders": "ออเดอร์เปิดรวม",
      "ladder.markets_title": "มาร์เก็ต Ladder",
      "ladder.markets_sub": "มาร์เก็ตที่มีการตั้งค่า Ladder อยู่ คลิกเพื่อดูรายละเอียด",
      "ladder.no_markets_configured": "ไม่มีมาร์เก็ตที่ตั้งค่าสำหรับกลยุทธ์ Ladder",
      "ladder.badge_on": "ON",
      "ladder.badge_off": "OFF",
      "ladder.open_orders": "ออเดอร์เปิด",
      "ladder.range": "ช่วงราคา",
      "ladder.spacing": "ระยะห่าง",
      "ladder.order_usdt": "Order USDT",
      "ladder.update_failed": "อัปเดตมุมมอง Ladder ไม่สำเร็จ"
    }
  };

  let currentLang = "ko";

  function normalizeLanguage(lang) {
    const l = String(lang || "").trim().toLowerCase();
    if (l === "en" || l === "th") return l;
    return "ko";
  }

  function detectLanguage() {
    const browserLang = String(navigator.language || "").toLowerCase();
    if (browserLang.startsWith("th")) return "th";
    if (browserLang.startsWith("en")) return "en";
    return "ko";
  }

  function t(key, fallback = "") {
    const lang = normalizeLanguage(currentLang);
    return I18N?.[lang]?.[key] ?? I18N?.ko?.[key] ?? fallback ?? key;
  }

  function tf(key, vars = {}, fallback = "") {
    const template = String(t(key, fallback));
    return template.replace(/\{([a-zA-Z0-9_]+)\}/g, (_, name) => {
      if (Object.prototype.hasOwnProperty.call(vars, name)) return String(vars[name]);
      return `{${name}}`;
    });
  }

  function applyDataI18n(root = document) {
    const nodes = root.querySelectorAll("[data-i18n]");
    nodes.forEach((el) => {
      const key = el.getAttribute("data-i18n");
      if (!key) return;
      const fallback = el.getAttribute("data-i18n-fallback") || el.textContent || "";
      el.textContent = t(key, fallback);
    });

    const titleNodes = root.querySelectorAll("[data-i18n-title]");
    titleNodes.forEach((el) => {
      const key = el.getAttribute("data-i18n-title");
      if (!key) return;
      const fallback = el.getAttribute("title") || "";
      el.setAttribute("title", t(key, fallback));
    });

    const placeholderNodes = root.querySelectorAll("[data-i18n-placeholder]");
    placeholderNodes.forEach((el) => {
      const key = el.getAttribute("data-i18n-placeholder");
      if (!key) return;
      const fallback = el.getAttribute("placeholder") || "";
      el.setAttribute("placeholder", t(key, fallback));
    });
  }

  function setLanguage(lang, persist = true) {
    currentLang = normalizeLanguage(lang);
    if (persist) {
      try { localStorage.setItem(LS_LANG, currentLang); } catch (_) {}
    }
    document.documentElement.setAttribute("lang", currentLang);

    const sel = document.getElementById("lang-select");
    if (sel) sel.value = currentLang;

    applyDataI18n(document);

    document.dispatchEvent(new CustomEvent("autocoin:lang-changed", {
      detail: { lang: currentLang }
    }));
    return currentLang;
  }

  function initLanguage(defaultLang = "ko") {
    let stored = "";
    try { stored = localStorage.getItem(LS_LANG) || ""; } catch (_) {}
    const selected = normalizeLanguage(stored || defaultLang || detectLanguage());

    const sel = document.getElementById("lang-select");
    if (sel && sel.dataset.i18nBound !== "1") {
      sel.addEventListener("change", (e) => setLanguage(e.target.value, true));
      sel.dataset.i18nBound = "1";
    }

    setLanguage(selected, false);
    return selected;
  }

  function getLanguage() {
    return normalizeLanguage(currentLang);
  }

  window.AutocoinEmbedI18n = {
    I18N,
    LS_LANG,
    normalizeLanguage,
    t,
    tf,
    applyDataI18n,
    setLanguage,
    initLanguage,
    getLanguage
  };
})();
