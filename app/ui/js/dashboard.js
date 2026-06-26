/* ============================================================
 * Autocoin OS v3-H — OMA Command Center
 * Dashboard JS (Admin + AI/Risk minimal)
 * ============================================================
 * 목표
 * - OMA Admin Apply가 실제 POST를 발생시키고(서버 로그 확인)
 * - Active Prices ↔ OMA Managed Markets 선택이 연동되며
 * - AI/Risk 카드가 선택 마켓 기준으로 항상 표시되도록 한다.
 *
 * 설계 원칙
 * - UI는 "확인용". 과도한 기능 확장은 금지.
 * - 백엔드 계약이 흔들려도 버티도록(키 이름) 유연 파싱.
 * 
 * Depends on: utils.js (window.AutocoinUtils)
 * 
 * NOTE: Currency is USDT (Bybit). All monetary values in USDT.
 * ============================================================ */

"use strict";

(function() {
const IntervalManager = (window.AutocoinUtils || {}).IntervalManager;

/* =========================
 * Quote currency config (loaded from server)
 * ========================= */
let QUOTE_CONFIG = { symbol: 'USDT' };

async function loadQuoteCurrencyConfig() {
  try {
    const resp = await fetch('/api/ui/config');
    if (resp.ok) {
      const data = await resp.json();
      if (data.quote_currency) {
        QUOTE_CONFIG = data.quote_currency;
      }
    }
  } catch (e) {
    console.warn('Failed to load quote currency config:', e);
  }
}

function extractBase(market) {
  if (!market) return '';
  // Bybit format: BTCUSDT -> BTC
  return market.replace(/USDT$/i, '');
}

/* =========================
 * DOM helpers
 * ========================= */
function qs(id) {
  return document.getElementById(id) || null;
}
function clear(el) {
  if (!el) return;
  while (el.firstChild) el.removeChild(el.firstChild);
}

/**
 * 상승 여력 계산 (0~100%)
 * RSI, AI Score, Momentum 종합
 * 0% = 과매수(고점) → 하락 임박
 * 100% = 과매도(저점) → 상승 여력 큼
 */
function calcUpsidePotential(brain) {
  if (!brain) return null;
  
  let score = 0;
  let factors = 0;
  
  // RSI (30 이하 = 과매도 = 상승여력, 70 이상 = 과매수 = 하락임박)
  if (brain.rsi !== undefined && Number.isFinite(brain.rsi)) {
    const rsiPotential = Math.max(0, Math.min(100, (70 - brain.rsi) * 2.5));
    score += rsiPotential;
    factors++;
  }
  
  // AI Prediction (0~1, 높을수록 상승 가능성)
  if (brain.ai_prediction !== undefined && Number.isFinite(brain.ai_prediction)) {
    const aiPotential = brain.ai_prediction * 100;
    score += aiPotential;
    factors++;
  }
  
  // Momentum (음수 = 하락중 = 반등 여력, 양수 = 상승중 = 추가 상승 제한적)
  if (brain.momentum !== undefined && Number.isFinite(brain.momentum)) {
    const momPotential = Math.max(0, Math.min(100, 50 - brain.momentum * 5));
    score += momPotential;
    factors++;
  }
  
  if (factors === 0) return null;
  return Math.max(0, Math.min(100, score / factors));
}

/**
 * 위치 게이지 HTML 렌더링
 */
function renderPositionGauge(potential) {
  if (potential === null) return '';
  
  const pct = Math.round(potential);
  const arrow = pct >= 50 ? '↑' : '↓';
  const cls = pct >= 60 ? 'bullish' : (pct <= 40 ? 'bearish' : 'neutral');
  const label = pct >= 60 ? '상승여력' : (pct <= 40 ? '하락위험' : '중립');
  
  return `
    <div class="position-gauge">
      <span class="gauge-label sell">SELL</span>
      <div class="gauge-track">
        <div class="gauge-marker" style="left: ${pct}%"></div>
      </div>
      <span class="gauge-label buy">BUY</span>
      <span class="gauge-value ${cls}">${pct}%${arrow} ${label}</span>
    </div>
  `;
}

/* =========================
 * API endpoints
 * ========================= */
const API = {
  systemStatus: "/api/system/status",
  systemInfo: "/api/system/info",
  systemGuardsGet: "/api/system/guards",
  systemGuardsSet: "/api/system/guards",
  managerSet: (m, s, r, budgetUsdt) => {
    const qs = new URLSearchParams();
    qs.set("market", m);
    qs.set("state", s);
    if (r) qs.set("reason", r);
    if (budgetUsdt !== undefined && budgetUsdt !== null && String(budgetUsdt).trim() !== "") {
      qs.set("budget_usdt", String(budgetUsdt).trim());
    }
    return `/api/manager/markets/set?${qs.toString()}`;
  },
  engineStart: (m) => `/api/engine/start?market=${encodeURIComponent(m)}`,
  engineStop: "/api/engine/stop",
  engineControls: (m) => `/api/engine/controls?market=${encodeURIComponent(m)}`,
  emergencyStop: (reason) =>
    `/api/system/emergency/stop?reason=${encodeURIComponent(reason || "UI")}`,
  emergencyResume: (reason) =>
    `/api/system/emergency/resume?reason=${encodeURIComponent(reason || "UI")}`,    
  systemReconcile: "/api/system/reconcile",

  // AI Gate
  aiGateGet: "/api/ai/gate",
  aiGateSet: "/api/ai/gate",
  
  engineManualOrder: "/api/engine/manual/order",
  engineManualBatch: "/api/engine/manual/batch",

  // Reserved (candidate proposals)
  reservedList: "/api/reserved/list",
  reservedClear: "/api/reserved/clear",
  reservedHistoryClear: "/api/reserved/history/clear",
  reservedRefresh: (ppN, alN, ldN, ltN, gzN, ctN, snN, forceFill = false) => {
    const qs = new URLSearchParams();
    qs.set("pingpong_n", String(ppN ?? 3));
    qs.set("autoloop_n", String(alN ?? 3));
    qs.set("ladder_n", String(ldN ?? 0));
    qs.set("lightning_n", String(ltN ?? 0));
    qs.set("gazua_n", String(gzN ?? 0));
    qs.set("contrarian_n", String(ctN ?? 0));
    qs.set("sniper_n", String(snN ?? 0));
    if (forceFill) qs.set("force_fill", "1");
    return `/api/reserved/refresh?${qs.toString()}`;
  },
  reservedSettingsGet: "/api/reserved/settings",
  reservedSettingsSet: (opts) => {
    const o = (opts && typeof opts === "object") ? opts : {};
    const qs = new URLSearchParams();

    if (o.pingpong_n !== undefined) qs.set("pingpong_n", String(o.pingpong_n));
    if (o.autoloop_n !== undefined) qs.set("autoloop_n", String(o.autoloop_n));
    if (o.ladder_n !== undefined) qs.set("ladder_n", String(o.ladder_n));
    if (o.lightning_n !== undefined) qs.set("lightning_n", String(o.lightning_n));
    if (o.gazua_n !== undefined) qs.set("gazua_n", String(o.gazua_n));
    if (o.contrarian_n !== undefined) qs.set("contrarian_n", String(o.contrarian_n));
    if (o.sniper_n !== undefined) qs.set("sniper_n", String(o.sniper_n));

    if (o.autopilot_enabled !== undefined) qs.set("autopilot_enabled", o.autopilot_enabled ? "1" : "0");
    if (o.auto_approve !== undefined) qs.set("autopilot_auto_approve", o.auto_approve ? "1" : "0");

    if (o.promote_to_active !== undefined) qs.set("promote_to_active", o.promote_to_active ? "1" : "0");
    if (o.apply_suggested_budget !== undefined) qs.set("apply_suggested_budget", o.apply_suggested_budget ? "1" : "0");

    // time window
    if (o.window_enabled !== undefined) qs.set("autopilot_window_enabled", o.window_enabled ? "1" : "0");
    if (o.window_start !== undefined) qs.set("autopilot_window_start", String(o.window_start ?? ""));
    if (o.window_end !== undefined) qs.set("autopilot_window_end", String(o.window_end ?? ""));

    // demotion rules
    if (o.idle_demote_enabled !== undefined) qs.set("autopilot_idle_demote_enabled", o.idle_demote_enabled ? "1" : "0");
    if (o.idle_demote_min !== undefined) qs.set("autopilot_idle_demote_min", String(o.idle_demote_min));

    if (o.guard_demote_enabled !== undefined) qs.set("autopilot_guard_demote_enabled", o.guard_demote_enabled ? "1" : "0");
    if (o.guard_demote_window_min !== undefined) qs.set("autopilot_guard_demote_window_min", String(o.guard_demote_window_min));
    if (o.guard_demote_n !== undefined) qs.set("autopilot_guard_demote_n", String(o.guard_demote_n));

    if (o.signal_miss_enabled !== undefined) qs.set("autopilot_signal_miss_enabled", o.signal_miss_enabled ? "1" : "0");
    if (o.signal_miss_window_min !== undefined) qs.set("autopilot_signal_miss_window_min", String(o.signal_miss_window_min));
    if (o.signal_miss_min_attempts !== undefined) qs.set("autopilot_signal_miss_min_attempts", String(o.signal_miss_min_attempts));

    if (o.eval_interval_sec !== undefined) qs.set("autopilot_eval_interval_sec", String(o.eval_interval_sec));
    if (o.grace_sec !== undefined) qs.set("autopilot_grace_sec", String(o.grace_sec));
    if (o.demote_max_total !== undefined) qs.set("autopilot_demote_max_total", String(o.demote_max_total));
    if (o.demote_max_per_strategy !== undefined) qs.set("autopilot_demote_max_per_strategy", String(o.demote_max_per_strategy));

    return `/api/reserved/settings?${qs.toString()}`;
  },
  reservedAutopilotRun: (scanOnly) => {
    const qs = new URLSearchParams();
    qs.set("scan_only", scanOnly ? "1" : "0");
    return `/api/reserved/autopilot/run?${qs.toString()}`;
  },
  reservedApprove: (rid, toState, applyBudget) => {
    const qs = new URLSearchParams();
    qs.set("rid", String(rid || ""));
    if (toState) qs.set("to_state", String(toState));
    if (applyBudget !== undefined && applyBudget !== null) qs.set("apply_budget", applyBudget ? "1" : "0");
    return `/api/reserved/approve?${qs.toString()}`;
  },
  reservedReject: (rid) => `/api/reserved/reject?rid=${encodeURIComponent(rid)}`,

  // Market Status Check
  marketStatusCheck: "/api/system/market-status-check",
  
  // Holdings Upside Ranking
  holdingsUpside: "/api/strategy/holdings/upside",
  
  // Market Upside Ranking (전체 마켓)
  marketUpside: "/api/strategy/market/upside",
  
  // Rebound Opportunity (급락 후 반등 - 전체 마켓)
  marketRebound: "/api/strategy/market/rebound",
  
  // RSI Ranking (과매도 코인)
  marketRsi: "/api/strategy/market/rsi",
  
  // Technical Aggregate Score (종합 기술 점수)
  marketTechScore: "/api/strategy/market/tech-score",
  
  // Unified Rankings API (통합 랭킹)
  marketRankings: "/api/strategy/market/rankings",
};

const POLL_MS = 3000;
const POLL_MS_WS_FALLBACK = 60000;  // WebSocket 연결 시 폴링 간격 늘림

/* =========================
 * STATE
 * ========================= */
const state = {
  system: {},
  guards: null,
  coordinator: {},
  oma: { active: [], watch: [], recovery: [] },
  prices: {},

  // UI derived
  managedMarkets: [], // ACTIVE(+RECOVERY) 중심
  selectedMarket: null,

    // for autocomplete
  allKnownMarkets: [],

  // Reserved queue
  reserved: { meta: null, items: [], history: [] },
  reservedSettings: null,
  
  // WebSocket state
  wsConnected: false,
  
  // Binance prices (for cross-exchange comparison)
  binancePrices: {},  // { "BTC": 96500, "ETH": 3200, ... } USDT prices
  binanceLastFetch: 0,
};


/* =========================
 * UTIL
 * ========================= */

// localStorage helpers (PnL baseline / pinned manual markets)
function lsGetJson(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return fallback;
    return JSON.parse(raw);
  } catch (e) {
    return fallback;
  }
}
function lsSetJson(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch (e) {}
}

const LS_PNL_BASELINE = "nunnaya_pnl_baseline_v1"; // { [market]: { equity: number, ts: string } }
const LS_MANUAL_PIN = "nunnaya_manual_pins_v1";    // string[] of markets
const LS_RESERVED_HISTORY_SHOW_MAX = "nunnaya_reserved_history_show_max_v1"; // number (1-200)
const LS_AUTO_LIQUIDATE_DELISTING = "nunnaya_auto_liquidate_delisting_v1"; // boolean

function clampInt(v, minV, maxV, fallback) {
  const n = Number(v);
  if (!Number.isFinite(n)) return fallback;
  const x = Math.trunc(n);
  return Math.max(minV, Math.min(maxV, x));
}

// Reserved History: UI-only display count (persists in localStorage)
function getReservedHistoryShowMax() {
  const stored = lsGetJson(LS_RESERVED_HISTORY_SHOW_MAX, null);
  if (stored !== null && stored !== undefined) {
    return clampInt(stored, 1, 200, 5);
  }
  const el = document.getElementById("reservedHistoryShowMax");
  return clampInt(el ? el.value : null, 1, 200, 5);
}

function setReservedHistoryShowMax(v) {
  const n = clampInt(v, 1, 200, 5);
  lsSetJson(LS_RESERVED_HISTORY_SHOW_MAX, n);
  const el = document.getElementById("reservedHistoryShowMax");
  if (el) el.value = String(n);
  return n;
}

function getManualPins() {
  const arr = lsGetJson(LS_MANUAL_PIN, []);
  return Array.isArray(arr) ? arr.map(normMarket).filter(Boolean) : [];
}
function setManualPins(markets) {
  lsSetJson(LS_MANUAL_PIN, Array.from(new Set((markets || []).map(normMarket).filter(Boolean))));
}
function pinManualMarket(market, pinned) {
  const m = normMarket(market);
  if (!m) return;
  const pins = new Set(getManualPins());
  if (pinned) pins.add(m);
  else pins.delete(m);
  setManualPins(Array.from(pins));
}

// --- Market Alerts (Delisting / New Listing / Preview) ---
async function loadMarketAlerts() {
  try {
    const res = await fetch(API.marketStatusCheck);
    const data = await res.json();
    if (!data.ok) return;
    
    // Delisting alerts
    const delistingList = qs("delistingAlertList");
    if (delistingList) {
      if (data.delisting_alerts && data.delisting_alerts.length > 0) {
        delistingList.innerHTML = data.delisting_alerts.map(a => {
          const sev = a.severity === 'critical' ? 'style="color:#ff5252; font-weight:bold;"' : 'style="color:#ff9800;"';
          return `<div ${sev}>${a.korean_name || a.market} (${a.market}) - 종료일: ${a.delisting_date || '미정'}</div>`;
        }).join('');
      } else {
        delistingList.innerHTML = '<div class="empty" style="color:#666;">없음</div>';
      }
    }
    
    // New listings
    const newList = qs("newListingList");
    if (newList) {
      if (data.new_listings && data.new_listings.length > 0) {
        newList.innerHTML = data.new_listings.map(a => 
          `<div style="color:#4caf50;">${a.korean_name || a.market} (${a.market})</div>`
        ).join('');
      } else {
        newList.innerHTML = '<div class="empty" style="color:#666;">없음</div>';
      }
    }
    
    // Preview markets
    const previewList = qs("previewList");
    const previewCount = qs("previewCount");
    if (previewCount) {
      previewCount.textContent = data.preview_markets?.length || 0;
    }
    if (previewList) {
      if (data.preview_markets && data.preview_markets.length > 0) {
        previewList.innerHTML = data.preview_markets.map(a => 
          `<div style="color:#ff9800;">${a.korean_name || a.market} (${a.market})</div>`
        ).join('');
      } else {
        previewList.innerHTML = '<div class="empty" style="color:#666;">없음</div>';
      }
    }
  } catch (e) {
    console.error("loadMarketAlerts error:", e);
  }
}

function bindMarketAlertsControls() {
  const btnRefresh = qs("btnRefreshMarketAlerts");
  if (btnRefresh) {
    btnRefresh.addEventListener("click", () => loadMarketAlerts());
  }
  
  const chkAutoLiquidate = qs("autoLiquidateDelisting");
  if (chkAutoLiquidate) {
    // Restore from localStorage
    const saved = lsGetJson(LS_AUTO_LIQUIDATE_DELISTING, false);
    chkAutoLiquidate.checked = !!saved;
    
    chkAutoLiquidate.addEventListener("change", () => {
      lsSetJson(LS_AUTO_LIQUIDATE_DELISTING, chkAutoLiquidate.checked);
    });
  }
}

// ============================================================
// Holdings Upside Ranking
// ============================================================
async function loadHoldingsUpside() {
  try {
    const res = await fetch(API.holdingsUpside + "?top_n=3");
    const data = await res.json();
    
    const listEl = qs("upsideRankingList");
    const allEl = qs("upsideAllRankings");
    
    // 에러 응답 처리
    if (!data.ok && data.error) {
      if (listEl) listEl.innerHTML = `<div class="empty" style="color:#ff5252; text-align:center; padding:10px; font-size:11px;">API 에러: ${data.error}</div>`;
      console.error("loadHoldingsUpside API error:", data.error);
      return;
    }
    
    if (!data.rankings || data.rankings.length === 0) {
      const msg = data.message || "보유 코인 없음";
      if (listEl) listEl.innerHTML = `<div class="empty" style="color:#666; text-align:center; padding:10px;">${msg}</div>`;
      if (allEl) allEl.innerHTML = '<div class="empty" style="color:#666;">없음</div>';
      return;
    }
    
    // TOP 3 렌더링
    if (listEl) {
      listEl.innerHTML = data.rankings.map((r, i) => {
        const medal = i === 0 ? "🥇" : (i === 1 ? "🥈" : "🥉");
        const pnlColor = r.pnl_pct >= 0 ? "#4caf50" : "#ff5252";
        const scoreColor = r.upside_score >= 50 ? "#4caf50" : (r.upside_score >= 30 ? "#ff9800" : "#888");
        
        return `
          <div style="display:flex; justify-content:space-between; align-items:center; padding:8px; border-bottom:1px solid #333; cursor:pointer;" onclick="window.open('/ui/market_detail.html?market=${encodeURIComponent(r.market)}', '_blank')">
            <div>
              <span style="font-size:16px;">${medal}</span>
              <span style="font-weight:600; margin-left:4px;">${r.currency}</span>
              <span style="color:#888; font-size:10px; margin-left:6px;">점수 <span style="color:${scoreColor}; font-weight:bold;">${r.upside_score}</span></span>
            </div>
            <div style="text-align:right;">
              <div style="font-size:11px; color:${pnlColor};">${r.pnl_pct >= 0 ? "+" : ""}${r.pnl_pct}%</div>
              <div style="font-size:10px; color:#888;">${r.reason || "분석 중"}</div>
            </div>
          </div>
        `;
      }).join("");
    }
    
    // 전체 순위 렌더링
    if (allEl && data.all_rankings) {
      allEl.innerHTML = data.all_rankings.map(r => {
        const pnlColor = r.pnl_pct >= 0 ? "#4caf50" : "#ff5252";
        return `
          <div style="display:flex; justify-content:space-between; padding:4px 0; border-bottom:1px solid #222; cursor:pointer;" onclick="window.open('/ui/market_detail.html?market=${encodeURIComponent(r.market)}', '_blank')">
            <span><span style="color:#888; font-size:10px;">#${r.rank}</span> ${r.currency}</span>
            <span>
              <span style="color:#888;">점수 ${r.upside_score}</span>
              <span style="color:${pnlColor}; margin-left:8px;">${r.pnl_pct >= 0 ? "+" : ""}${r.pnl_pct}%</span>
            </span>
          </div>
        `;
      }).join("");
    }
  } catch (e) {
    console.error("loadHoldingsUpside error:", e);
  }
}

function bindHoldingsUpsideControls() {
  const btnRefresh = qs("btnRefreshUpside");
  if (btnRefresh) {
    btnRefresh.addEventListener("click", () => loadHoldingsUpside());
  }
}

// ============================================================
// Market Upside Ranking (전체 마켓)
// ============================================================
async function loadMarketUpside() {
  try {
    const res = await fetch(API.marketUpside + "?top_n=20&min_volume_usdt=500000");
    const data = await res.json();

    const listEl = qs("marketUpsideList");
    const moreEl = qs("marketUpsideMore");

    if (!data.ok || !data.rankings || data.rankings.length === 0) {
      if (listEl) listEl.innerHTML = '<div class="empty" style="color:#666; text-align:center; padding:10px;">No data</div>';
      if (moreEl) moreEl.innerHTML = '<div class="empty" style="color:#666;">None</div>';
      return;
    }

    const formatVol = (v) => {
      if (v >= 1e9) return "$" + (v / 1e9).toFixed(1) + "B";
      if (v >= 1e6) return "$" + (v / 1e6).toFixed(1) + "M";
      if (v >= 1e3) return "$" + (v / 1e3).toFixed(0) + "K";
      return "$" + v.toFixed(0);
    };

    const formatPrice = (p) => {
      if (!p || p <= 0) return "";
      if (p >= 1000) return "$" + p.toLocaleString();
      if (p >= 1) return "$" + p.toFixed(2);
      if (p >= 0.01) return "$" + p.toFixed(4);
      return "$" + p.toFixed(6);
    };
    
    // TOP 5 렌더링
    if (listEl) {
      listEl.innerHTML = data.rankings.slice(0, 5).map((r, i) => {
        const medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][i];
        const changeColor = r.change_pct >= 0 ? "#4caf50" : "#ff5252";
        const scoreColor = r.upside_score >= 50 ? "#4caf50" : (r.upside_score >= 30 ? "#ff9800" : "#888");
        const priceStr = formatPrice(r.current_price);
        const a = r.analysis || {};
        
        return `
          <div style="display:flex; justify-content:space-between; align-items:center; padding:6px 4px; border-bottom:1px solid #333; cursor:pointer;" onclick="window.open('/ui/market_detail.html?market=${encodeURIComponent(r.market)}', '_blank')">
            <div style="flex:1;">
              <span style="font-size:14px;">${medal}</span>
              <span style="font-weight:600; margin-left:2px;">${r.currency}</span>
              <span style="color:#4caf50; font-size:10px; margin-left:4px;">${priceStr}</span>
              <div style="font-size:9px; color:#888; margin-top:1px;">
                RSI:${a.rsi || '-'} BB:${Math.round((a.bb_position || 0) * 100)}% 변동:${a.daily_range_pct || '-'}%
              </div>
            </div>
            <div style="text-align:right; min-width:80px;">
              <div style="font-size:10px;">
                <span style="color:${scoreColor}; font-weight:bold;">점수 ${r.upside_score}</span>
                <span style="color:${changeColor}; margin-left:6px;">${r.change_pct >= 0 ? "+" : ""}${r.change_pct}%</span>
              </div>
              <div style="font-size:9px; color:#888;">${r.reason || "분석 중"}</div>
            </div>
          </div>
        `;
      }).join("");
    }
    
    // TOP 6~20 렌더링
    if (moreEl && data.rankings.length > 5) {
      moreEl.innerHTML = data.rankings.slice(5, 20).map(r => {
        const changeColor = r.change_pct >= 0 ? "#4caf50" : "#ff5252";
        return `
          <div style="display:flex; justify-content:space-between; padding:3px 0; border-bottom:1px solid #222; cursor:pointer;" onclick="window.open('/ui/market_detail.html?market=${encodeURIComponent(r.market)}', '_blank')">
            <span><span style="color:#666; font-size:9px;">#${r.rank}</span> ${r.currency} <span style="color:#4caf50; font-size:9px;">${formatPrice(r.current_price)}</span></span>
            <span>
              <span style="color:#888; font-size:10px;">점수 ${r.upside_score}</span>
              <span style="color:${changeColor}; margin-left:6px; font-size:10px;">${r.change_pct >= 0 ? "+" : ""}${r.change_pct}%</span>
            </span>
          </div>
        `;
      }).join("");
    } else if (moreEl) {
      moreEl.innerHTML = '<div class="empty" style="color:#666;">5개 이하</div>';
    }
  } catch (e) {
    console.error("loadMarketUpside error:", e);
  }
}

function bindMarketUpsideControls() {
  const btnRefresh = qs("btnRefreshMarketUpside");
  if (btnRefresh) {
    btnRefresh.addEventListener("click", () => loadMarketUpside());
  }
}

// ============================================================
// 김프 비교 (바이낸스 가격 비교) - 전역 데이터
// ============================================================

// 바이낸스 가격 전역 조회 (60초 캐시, 백그라운드에서 조용히 업데이트)
let _binanceFetchInFlight = false;
async function fetchBinancePrices(forceRefresh = false) {
  const now = Date.now();
  // 60초 캐시 - API 호출 최소화
  if (!forceRefresh && now - state.binanceLastFetch < 60000 && Object.keys(state.binancePrices).length > 0) {
    return state.binancePrices;
  }
  
  // 중복 호출 방지
  if (_binanceFetchInFlight) return state.binancePrices;
  _binanceFetchInFlight = true;
  
  try {
    const res = await fetch("/api/system/cross-compare-disabled?markets=");
    const data = await res.json();
    
    if (data.ok && data.data) {
      const prices = {};
      for (const item of data.data) {
        if (item.coin && item.binance_usdt > 0) {
          prices[item.coin] = item.binance_usdt;
        }
      }
      state.binancePrices = prices;
      state.binanceLastFetch = now;
    }
    return state.binancePrices;
  } catch (e) {
    console.warn("fetchBinancePrices error:", e);
    return state.binancePrices;
  } finally {
    _binanceFetchInFlight = false;
  }
}

// Cross-exchange price comparison helper
function getKimchiPremium(market) {
  const coin = extractBase(market);
  const bybitPrice = state.prices[market] || 0;
  const binanceUsdt = state.binancePrices[coin] || 0;

  if (!bybitPrice || !binanceUsdt || binanceUsdt <= 0) {
    return null;
  }

  const premiumPct = ((bybitPrice - binanceUsdt) / binanceUsdt) * 100;

  return {
    coin,
    bybitPrice,
    binanceUsdt,
    premiumPct: Math.round(premiumPct * 100) / 100,
  };
}

// Cross-exchange badge HTML
function renderKimchiBadge(market, compact = false) {
  const kimchi = getKimchiPremium(market);
  if (!kimchi) return "";

  const pct = kimchi.premiumPct;
  const color = pct > 0 ? "#f0b90b" : (pct < 0 ? "#4caf50" : "#888");

  const binanceFmt = kimchi.binanceUsdt >= 1000
    ? "$" + kimchi.binanceUsdt.toLocaleString()
    : "$" + kimchi.binanceUsdt.toFixed(2);

  const sign = pct > 0 ? "+" : "";
  const tooltip = `Binance: $${kimchi.binanceUsdt.toLocaleString()} (diff ${sign}${pct}%)`;

  if (compact) {
    return `<span style="color:${color}; font-size:9px; margin-left:3px;" title="${tooltip}">B${binanceFmt}</span>`;
  }

  return `<span style="color:${color}; font-weight:bold;" title="${tooltip}">B${binanceFmt}</span>`;
}

async function loadKimchiPremium() {
  const listEl = qs("kimchiCompareList");
  const marketInput = qs("kimchiMarketInput");
  const rateLabel = qs("kimchiRateLabel");
  
  if (!listEl) return;
  
  listEl.innerHTML = '<div class="empty" style="color:#888; text-align:center;">Loading...</div>';

  try {
    const markets = marketInput ? marketInput.value.trim() : "";

    const url = `/api/system/cross-compare-disabled?markets=${encodeURIComponent(markets)}`;
    const res = await fetch(url);
    const data = await res.json();

    if (!data.ok || !data.data || data.data.length === 0) {
      listEl.innerHTML = '<div class="empty" style="color:#666; text-align:center; padding:10px;">No data</div>';
      return;
    }

    const formatPrice = (p) => {
      if (!p || p <= 0) return "-";
      if (p >= 1000) return "$" + p.toLocaleString();
      if (p >= 1) return "$" + p.toFixed(2);
      return "$" + p.toFixed(4);
    };

    const formatUsdt = (p) => {
      if (!p || p <= 0) return "-";
      if (p >= 1) return "$" + p.toLocaleString(undefined, {maximumFractionDigits: 2});
      return "$" + p.toFixed(6);
    };

    listEl.innerHTML = `
      <table style="width:100%; border-collapse:collapse; font-size:10px;">
        <thead>
          <tr style="border-bottom:1px solid #444; color:#888;">
            <th style="text-align:left; padding:4px;">Coin</th>
            <th style="text-align:right; padding:4px;">Bybit</th>
            <th style="text-align:right; padding:4px;">Binance</th>
            <th style="text-align:right; padding:4px;">Diff</th>
          </tr>
        </thead>
        <tbody>
          ${data.data.map(item => {
            const premiumColor = item.premium_pct > 0 ? "#ff5252" : (item.premium_pct < 0 ? "#4caf50" : "#888");
            const premiumSign = item.premium_pct > 0 ? "+" : "";
            const hasBinance = item.has_binance && item.binance_usdt > 0;
            const bybitPrice = item.bybit_usdt || item.bybit_usdt || 0;

            return `
              <tr style="border-bottom:1px solid #333;">
                <td style="padding:4px; font-weight:600;">${item.coin}</td>
                <td style="text-align:right; padding:4px;">${formatPrice(bybitPrice)}</td>
                <td style="text-align:right; padding:4px; color:#f0b90b;">${hasBinance ? formatUsdt(item.binance_usdt) : '<span style="color:#666;">-</span>'}</td>
                <td style="text-align:right; padding:4px; color:${premiumColor}; font-weight:bold;">${hasBinance ? premiumSign + item.premium_pct + "%" : '-'}</td>
              </tr>
            `;
          }).join("")}
        </tbody>
      </table>
    `;
  } catch (e) {
    console.error("loadKimchiPremium error:", e);
    listEl.innerHTML = `<div class="empty" style="color:#ff5252; text-align:center; padding:10px;">에러: ${e.message}</div>`;
  }
}

function bindKimchiControls() {
  const btnRefresh = qs("btnRefreshKimchi");
  if (btnRefresh) {
    btnRefresh.addEventListener("click", () => loadKimchiPremium());
  }
  
  
  // 코인 검색 자동완성
  const marketInput = qs("kimchiMarketInput");
  const autocompleteEl = qs("kimchiAutocomplete");
  
  if (marketInput && autocompleteEl) {
    let debounceTimer = null;
    
    marketInput.addEventListener("input", () => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => {
        const query = marketInput.value.trim().toUpperCase();
        if (query.length < 1) {
          autocompleteEl.style.display = "none";
          return;
        }
        
        // 모든 알려진 마켓에서 검색
        const allMarkets = Object.keys(state.prices || {});
        const matches = allMarkets
          .filter(m => m.toUpperCase().includes(query))
          .slice(0, 10);
        
        if (matches.length === 0) {
          autocompleteEl.style.display = "none";
          return;
        }
        
        autocompleteEl.innerHTML = matches.map(m => {
          const coin = m.split("-")[1] || m;
          const price = state.prices[m] || 0;
          const kimchi = getKimchiPremium(m);
          const kimchiStr = kimchi ? ` <span style="color:${kimchi.premiumPct > 0 ? '#ff5252' : '#4caf50'}">${kimchi.premiumPct > 0 ? '+' : ''}${kimchi.premiumPct}%</span>` : "";
          
          return `<div class="kimchi-autocomplete-item" data-market="${m}" style="padding:6px 8px; cursor:pointer; border-bottom:1px solid #333; font-size:11px;">
            <b>${coin}</b> <span style="color:#888;">${fmtPrice(price)}</span>${kimchiStr}
          </div>`;
        }).join("");
        
        autocompleteEl.style.display = "block";
      }, 200);
    });
    
    // 자동완성 항목 클릭
    autocompleteEl.addEventListener("click", (e) => {
      const item = e.target.closest(".kimchi-autocomplete-item");
      if (item) {
        const market = item.dataset.market;
        const coin = market.split("-")[1] || market;
        marketInput.value = coin;
        autocompleteEl.style.display = "none";
        loadKimchiPremium();
      }
    });
    
    // 외부 클릭 시 닫기
    document.addEventListener("click", (e) => {
      if (!marketInput.contains(e.target) && !autocompleteEl.contains(e.target)) {
        autocompleteEl.style.display = "none";
      }
    });
    
    // Enter 키로 조회
    marketInput.addEventListener("keypress", (e) => {
      if (e.key === "Enter") {
        autocompleteEl.style.display = "none";
        loadKimchiPremium();
      }
    });
  }
}

// ============================================================
// Rebound Opportunity - market-wide dip & bounce scanner
// ============================================================
async function loadReboundOpportunity() {
  try {
    const timeframeEl = qs("reboundTimeframe");
    const timeframe = timeframeEl ? timeframeEl.value : "24h";
    const res = await fetch(API.marketRebound + `?top_n=5&min_volume_usdt=500000&max_decline_pct=-3&timeframe=${timeframe}`);
    const data = await res.json();

    const listEl = qs("reboundRankingList");
    const allEl = qs("reboundAllRankings");

    if (!data.ok || !data.rankings || data.rankings.length === 0) {
      if (listEl) listEl.innerHTML = '<div class="empty" style="color:#666; text-align:center; padding:10px;">No dipped coins (market stable) </div>';
      if (allEl) allEl.innerHTML = '<div class="empty" style="color:#666;">None</div>';
      return;
    }

    const formatVol = (v) => {
      if (v >= 1e9) return "$" + (v / 1e9).toFixed(1) + "B";
      if (v >= 1e6) return "$" + (v / 1e6).toFixed(1) + "M";
      if (v >= 1e3) return "$" + (v / 1e3).toFixed(0) + "K";
      return "$" + v.toFixed(0);
    };

    const formatPrice = (p) => {
      if (!p || p <= 0) return "";
      if (p >= 1000) return "$" + p.toLocaleString();
      if (p >= 1) return "$" + p.toFixed(2);
      if (p >= 0.01) return "$" + p.toFixed(4);
      return "$" + p.toFixed(6);
    };
    
    // TOP 5 렌더링
    if (listEl) {
      listEl.innerHTML = data.rankings.map((r, i) => {
        const medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][i] || `${i + 1}`;
        const scoreColor = r.rebound_score >= 60 ? "#4caf50" : (r.rebound_score >= 40 ? "#2196f3" : "#888");
        const severityIcon = {
          "폭락": "🔴",
          "급락": "🟠",
          "하락": "🟡",
          "조정": "🟢"
        }[r.decline_severity] || "⚪";
        
        // 반등 상태 표시
        const posText = r.intraday_position < 0.3 ? "저점" : (r.intraday_position < 0.6 ? "반등중" : "회복중");
        const priceStr = formatPrice(r.current_price);
        
        return `
          <div style="display:flex; justify-content:space-between; align-items:center; padding:8px 4px; border-bottom:1px solid #333; cursor:pointer;" onclick="window.open('/ui/market_detail.html?market=${encodeURIComponent(r.market)}', '_blank')">
            <div style="flex:1;">
              <span style="font-size:14px;">${medal}</span>
              <span style="font-weight:600; margin-left:4px;">${r.currency}</span>
              <span style="color:#4caf50; font-size:10px; margin-left:2px;">${priceStr}</span>
              <div style="font-size:10px; margin-top:2px;">
                <span style="color:#ff5252;">${r.change_rate}%</span>
                <span style="margin-left:4px;">${severityIcon}</span>
                <span style="color:#888; margin-left:4px;">${posText}</span>
                <span style="color:#666; margin-left:4px; font-size:9px;">거래량 ${formatVol(r.volume_24h_usdt)}</span>
              </div>
            </div>
            <div style="text-align:right; min-width:80px;">
              <div style="font-size:12px;">
                <span style="color:${scoreColor}; font-weight:bold;">${r.rebound_score}점</span>
              </div>
              <div style="font-size:9px; color:#888; max-width:100px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${r.reason}">${r.reason || "분석 중"}</div>
            </div>
          </div>
        `;
      }).join("");
    }
    
    // 전체 급락 코인 렌더링
    if (allEl && data.all_rankings) {
      allEl.innerHTML = data.all_rankings.map(r => {
        const scoreColor = r.rebound_score >= 60 ? "#4caf50" : (r.rebound_score >= 40 ? "#2196f3" : "#888");
        return `
          <div style="display:flex; justify-content:space-between; padding:4px 0; border-bottom:1px solid #222; cursor:pointer;" onclick="window.open('/ui/market_detail.html?market=${encodeURIComponent(r.market)}', '_blank')">
            <span>
              <span style="color:#666; font-size:9px;">#${r.rank}</span>
              <span style="font-weight:500;">${r.currency}</span>
              <span style="color:#4caf50; font-size:9px;">${formatPrice(r.current_price)}</span>
              <span style="color:#ff5252; font-size:10px; margin-left:4px;">${r.change_rate}%</span>
            </span>
            <span style="color:${scoreColor}; font-size:10px;">${r.rebound_score}점</span>
          </div>
        `;
      }).join("");
    }
  } catch (e) {
    console.error("loadReboundOpportunity error:", e);
  }
}

function bindReboundControls() {
  const btnRefresh = qs("btnRefreshRebound");
  if (btnRefresh) {
    btnRefresh.addEventListener("click", () => loadReboundOpportunity());
  }
  
  // Timeframe 변경 시 자동 새로고침
  const timeframeEl = qs("reboundTimeframe");
  if (timeframeEl) {
    timeframeEl.addEventListener("change", () => loadReboundOpportunity());
  }
}

// ============================================================
// RSI Ranking - oversold coins
// ============================================================
async function loadRsiRanking() {
  try {
    const res = await fetch(API.marketRsi + "?top_n=10&min_volume_usdt=500000&rsi_max=40");
    const data = await res.json();

    const listEl = qs("rsiRankingList");
    const allEl = qs("rsiAllRankings");

    if (!data.ok || !data.rankings || data.rankings.length === 0) {
      if (listEl) listEl.innerHTML = '<div class="empty" style="color:#666; text-align:center; padding:10px;">No oversold coins</div>';
      if (allEl) allEl.innerHTML = '<div class="empty" style="color:#666;">None</div>';
      return;
    }

    const formatVol = (v) => {
      if (v >= 1e9) return "$" + (v / 1e9).toFixed(1) + "B";
      if (v >= 1e6) return "$" + (v / 1e6).toFixed(1) + "M";
      if (v >= 1e3) return "$" + (v / 1e3).toFixed(0) + "K";
      return "$" + v.toFixed(0);
    };

    const formatPrice = (p) => {
      if (!p || p <= 0) return "";
      if (p >= 1000) return "$" + p.toLocaleString();
      if (p >= 1) return "$" + p.toFixed(2);
      if (p >= 0.01) return "$" + p.toFixed(4);
      return "$" + p.toFixed(6);
    };
    
    // TOP 5 렌더링
    if (listEl) {
      listEl.innerHTML = data.rankings.slice(0, 5).map((r, i) => {
        const medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][i] || `${i + 1}`;
        const changeColor = r.change_rate >= 0 ? "#4caf50" : "#ff5252";
        const priceStr = formatPrice(r.current_price);
        
        const d = r.details || {};
        
        return `
          <div style="display:flex; justify-content:space-between; align-items:center; padding:6px 4px; border-bottom:1px solid #333; cursor:pointer;" onclick="window.open('/ui/market_detail.html?market=${encodeURIComponent(r.market)}', '_blank')">
            <div style="flex:1;">
              <span style="font-size:14px;">${medal}</span>
              <span style="font-weight:600; margin-left:4px;">${r.currency}</span>
              <span style="color:#4caf50; font-size:10px; margin-left:2px;">${priceStr}</span>
              <div style="font-size:9px; color:#888; margin-top:2px;">
                Vol:${d.volume_score || 0} RSI:${d.rsi_score || 0} 일목:${d.ichimoku_score || 0} MACD:${d.macd_score || 0}
              </div>
            </div>
            <div style="text-align:right; min-width:90px;">
              <div style="font-size:12px;">
                <span>${r.rsi_emoji}</span>
                <span style="font-weight:bold; color:#2196f3;">RSI ${r.rsi}</span>
              </div>
              <div style="font-size:10px;">
                <span style="color:#888;">${r.rsi_status}</span>
                <span style="color:${changeColor}; margin-left:4px;">${r.change_rate >= 0 ? "+" : ""}${r.change_rate}%</span>
              </div>
            </div>
          </div>
        `;
      }).join("");
    }
    
    // 전체 순위 렌더링
    if (allEl && data.all_rankings) {
      allEl.innerHTML = data.all_rankings.map(r => {
        return `
          <div style="display:flex; justify-content:space-between; padding:3px 0; border-bottom:1px solid #222; cursor:pointer;" onclick="window.open('/ui/market_detail.html?market=${encodeURIComponent(r.market)}', '_blank')">
            <span>
              <span style="color:#666; font-size:9px;">#${r.rank}</span>
              <span style="font-weight:500;">${r.currency}</span>
              <span style="color:#4caf50; font-size:9px;">${formatPrice(r.current_price)}</span>
            </span>
            <span>
              <span>${r.rsi_emoji}</span>
              <span style="color:#2196f3; font-size:10px;">RSI ${r.rsi}</span>
            </span>
          </div>
        `;
      }).join("");
    }
  } catch (e) {
    console.error("loadRsiRanking error:", e);
  }
}

function bindRsiControls() {
  const btnRefresh = qs("btnRefreshRsi");
  if (btnRefresh) {
    btnRefresh.addEventListener("click", () => loadRsiRanking());
  }
}

// ============================================================
// Technical Aggregate Score
// ============================================================
async function loadTechScore() {
  try {
    const res = await fetch(API.marketTechScore + "?top_n=10&min_volume_usdt=500000&min_score=50");
    const data = await res.json();

    const listEl = qs("techScoreList");
    const allEl = qs("techScoreAllRankings");

    if (!data.ok || !data.rankings || data.rankings.length === 0) {
      if (listEl) listEl.innerHTML = '<div class="empty" style="color:#666; text-align:center; padding:10px;">No buy signals</div>';
      if (allEl) allEl.innerHTML = '<div class="empty" style="color:#666;">None</div>';
      return;
    }

    const formatPrice = (p) => {
      if (!p || p <= 0) return "";
      if (p >= 1000) return "$" + p.toLocaleString();
      if (p >= 1) return "$" + p.toFixed(2);
      if (p >= 0.01) return "$" + p.toFixed(4);
      return "$" + p.toFixed(6);
    };
    
    // TOP 5 렌더링
    if (listEl) {
      listEl.innerHTML = data.rankings.slice(0, 5).map((r, i) => {
        const medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][i] || `${i + 1}`;
        const changeColor = r.change_rate >= 0 ? "#4caf50" : "#ff5252";
        const d = r.details || {};
        const priceStr = formatPrice(r.current_price);
        
        return `
          <div style="display:flex; justify-content:space-between; align-items:center; padding:6px 4px; border-bottom:1px solid #333; cursor:pointer;" onclick="window.open('/ui/market_detail.html?market=${encodeURIComponent(r.market)}', '_blank')">
            <div style="flex:1;">
              <span style="font-size:14px;">${medal}</span>
              <span style="font-weight:600; margin-left:4px;">${r.currency}</span>
              <span style="color:#4caf50; font-size:10px; margin-left:2px;">${priceStr}</span>
              <div style="font-size:9px; color:#888; margin-top:2px;">
                Vol:${d.volume_score} RSI:${d.rsi_score} 일목:${d.ichimoku_score} MACD:${d.macd_score}
              </div>
            </div>
            <div style="text-align:right; min-width:80px;">
              <div style="font-size:12px;">
                <span>${r.signal_emoji}</span>
                <span style="font-weight:bold;">${r.total_score}점</span>
              </div>
              <div style="font-size:10px;">
                <span style="color:#888;">${r.signal}</span>
                <span style="color:${changeColor}; margin-left:4px;">${r.change_rate >= 0 ? "+" : ""}${r.change_rate}%</span>
              </div>
            </div>
          </div>
        `;
      }).join("");
    }
    
    // 전체 순위 렌더링
    if (allEl && data.all_rankings) {
      allEl.innerHTML = data.all_rankings.map(r => {
        return `
          <div style="display:flex; justify-content:space-between; padding:3px 0; border-bottom:1px solid #222; cursor:pointer;" onclick="window.open('/ui/market_detail.html?market=${encodeURIComponent(r.market)}', '_blank')">
            <span>
              <span style="color:#666; font-size:9px;">#${r.rank}</span>
              <span style="font-weight:500;">${r.currency}</span>
              <span style="color:#4caf50; font-size:9px;">${formatPrice(r.current_price)}</span>
            </span>
            <span>
              <span>${r.signal_emoji}</span>
              <span style="font-size:10px; font-weight:bold;">${r.total_score}점</span>
              <span style="color:#888; font-size:9px; margin-left:4px;">${r.signal}</span>
            </span>
          </div>
        `;
      }).join("");
    }
  } catch (e) {
    console.error("loadTechScore error:", e);
  }
}

function bindTechScoreControls() {
  const btnRefresh = qs("btnRefreshTechScore");
  if (btnRefresh) {
    btnRefresh.addEventListener("click", () => loadTechScore());
  }
}

// ============================================================
// Unified Rankings API - 통합 랭킹 로드
// ============================================================
async function loadAllRankings() {
  try {
    const res = await fetch(API.marketRankings + "?top_n=5&min_volume_usdt=1000000");
    const data = await res.json();
    
    if (!data.ok || !data.rankings) {
      console.warn("loadAllRankings: no data", data);
      return;
    }
    
    const r = data.rankings;
    
    // 각 섹션 렌더링
    if (r.rebound) renderReboundSection(r.rebound);
    if (r.rsi_oversold) renderRsiSection(r.rsi_oversold);
    if (r.tech_score) renderTechScoreSection(r.tech_score);
    if (r.upside) renderUpsideSection(r.upside);
    
  } catch (e) {
    console.error("loadAllRankings error:", e);
  }
}

// --- Unified Rendering Helpers ---

function renderStrategyBadge(strategy) {
  const colors = {
    "GAZUA": "#f44336",
    "LIGHTNING": "#ff9800",
    "LADDER": "#2196f3",
    "AUTOLOOP": "#9c27b0",
    "PINGPONG": "#4caf50",
  };
  const color = colors[strategy] || "#666";
  return `<span style="font-size:9px; padding:1px 4px; background:${color}; color:#fff; border-radius:3px; margin-left:6px;">🎯 ${strategy}</span>`;
}

function formatPriceUnified(p) {
  if (!p || p <= 0) return "";
  if (p >= 1000) return "$" + p.toFixed(0);
  if (p >= 1) return "$" + p.toFixed(2);
  if (p >= 0.01) return "$" + p.toFixed(4);
  return "$" + p.toFixed(6);
}

function renderReboundSection(section) {
  const listEl = qs("reboundRankingList");
  const headerEl = qs("reboundSectionHeader");
  
  if (headerEl && section.recommended_strategy) {
    const badge = renderStrategyBadge(section.recommended_strategy);
    if (!headerEl.innerHTML.includes("🎯")) {
      headerEl.innerHTML = headerEl.textContent + badge;
    }
  }
  
  const items = section.items || [];
  if (!items.length) {
    if (listEl) listEl.innerHTML = '<div class="empty" style="color:#666; text-align:center; padding:10px;">급락 코인 없음 (시장 안정) 👍</div>';
    return;
  }
  
  if (listEl) {
    listEl.innerHTML = items.slice(0, 5).map((r, i) => {
      const medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][i] || `${i + 1}`;
      const scoreColor = r.score >= 60 ? "#4caf50" : (r.score >= 40 ? "#2196f3" : "#888");
      const priceStr = formatPriceUnified(r.price);
      const changeColor = r.change_pct >= 0 ? "#4caf50" : "#ff5252";
      
      return `
        <div style="display:flex; justify-content:space-between; align-items:center; padding:6px 4px; border-bottom:1px solid #333; cursor:pointer;" onclick="window.open('/ui/market_detail.html?market=${encodeURIComponent(r.market)}', '_blank')">
          <div style="flex:1;">
            <span style="font-size:14px;">${medal}</span>
            <span style="font-weight:600; margin-left:4px;">${extractBase(r.market || r.symbol || "")}</span>
            <span style="color:#4caf50; font-size:10px; margin-left:2px;">${priceStr}</span>
            <div style="font-size:10px; margin-top:2px;">
              <span style="color:${changeColor};">${r.change_pct >= 0 ? "+" : ""}${r.change_pct}%</span>
              <span style="color:#888; margin-left:4px;">RSI ${r.rsi}</span>
            </div>
          </div>
          <div style="text-align:right; min-width:60px;">
            <span style="color:${scoreColor}; font-weight:bold; font-size:12px;">${r.score}점</span>
          </div>
        </div>
      `;
    }).join("");
  }
}

function renderRsiSection(section) {
  const listEl = qs("rsiRankingList");
  const headerEl = qs("rsiSectionHeader");
  
  if (headerEl && section.recommended_strategy) {
    const badge = renderStrategyBadge(section.recommended_strategy);
    if (!headerEl.innerHTML.includes("🎯")) {
      headerEl.innerHTML = headerEl.textContent + badge;
    }
  }
  
  const items = section.items || [];
  if (!items.length) {
    if (listEl) listEl.innerHTML = '<div class="empty" style="color:#666; text-align:center; padding:10px;">과매도 코인 없음 (시장 과열?) 🔥</div>';
    return;
  }
  
  if (listEl) {
    listEl.innerHTML = items.slice(0, 5).map((r, i) => {
      const medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][i] || `${i + 1}`;
      const priceStr = formatPriceUnified(r.price);
      const changeColor = r.change_pct >= 0 ? "#4caf50" : "#ff5252";
      const rsiEmoji = r.rsi < 20 ? "🔴" : (r.rsi < 30 ? "🟠" : "🟡");
      
      return `
        <div style="display:flex; justify-content:space-between; align-items:center; padding:6px 4px; border-bottom:1px solid #333; cursor:pointer;" onclick="window.open('/ui/market_detail.html?market=${encodeURIComponent(r.market)}', '_blank')">
          <div style="flex:1;">
            <span style="font-size:14px;">${medal}</span>
            <span style="font-weight:600; margin-left:4px;">${extractBase(r.market || r.symbol || "")}</span>
            <span style="color:#4caf50; font-size:10px; margin-left:2px;">${priceStr}</span>
            <div style="font-size:10px; margin-top:2px;">
              <span style="color:${changeColor};">${r.change_pct >= 0 ? "+" : ""}${r.change_pct}%</span>
              <span style="color:#888; margin-left:4px;">${r.rsi_status}</span>
            </div>
          </div>
          <div style="text-align:right; min-width:70px;">
            <span>${rsiEmoji}</span>
            <span style="color:#2196f3; font-weight:bold; font-size:12px;">RSI ${r.rsi}</span>
          </div>
        </div>
      `;
    }).join("");
  }
}

function renderTechScoreSection(section) {
  const listEl = qs("techScoreList");
  const headerEl = qs("techScoreSectionHeader");
  
  if (headerEl && section.recommended_strategy) {
    const badge = renderStrategyBadge(section.recommended_strategy);
    if (!headerEl.innerHTML.includes("🎯")) {
      headerEl.innerHTML = headerEl.textContent + badge;
    }
  }
  
  const items = section.items || [];
  if (!items.length) {
    if (listEl) listEl.innerHTML = '<div class="empty" style="color:#666; text-align:center; padding:10px;">매수 신호 없음</div>';
    return;
  }
  
  if (listEl) {
    listEl.innerHTML = items.slice(0, 5).map((r, i) => {
      const medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][i] || `${i + 1}`;
      const priceStr = formatPriceUnified(r.price);
      const changeColor = r.change_pct >= 0 ? "#4caf50" : "#ff5252";
      const signalEmoji = r.signal === "강력 매수" ? "🚀" : (r.signal === "매수" ? "📈" : "➡️");
      
      return `
        <div style="display:flex; justify-content:space-between; align-items:center; padding:6px 4px; border-bottom:1px solid #333; cursor:pointer;" onclick="window.open('/ui/market_detail.html?market=${encodeURIComponent(r.market)}', '_blank')">
          <div style="flex:1;">
            <span style="font-size:14px;">${medal}</span>
            <span style="font-weight:600; margin-left:4px;">${extractBase(r.market || r.symbol || "")}</span>
            <span style="color:#4caf50; font-size:10px; margin-left:2px;">${priceStr}</span>
            <div style="font-size:10px; margin-top:2px;">
              <span style="color:${changeColor};">${r.change_pct >= 0 ? "+" : ""}${r.change_pct}%</span>
              <span style="color:#888; margin-left:4px;">${r.signal}</span>
            </div>
          </div>
          <div style="text-align:right; min-width:60px;">
            <span>${signalEmoji}</span>
            <span style="font-weight:bold; font-size:12px;">${r.score}점</span>
          </div>
        </div>
      `;
    }).join("");
  }
}

function renderUpsideSection(section) {
  const listEl = qs("marketUpsideList");
  const headerEl = qs("upsideSectionHeader");
  
  if (headerEl && section.recommended_strategy) {
    const badge = renderStrategyBadge(section.recommended_strategy);
    if (!headerEl.innerHTML.includes("🎯")) {
      headerEl.innerHTML = headerEl.textContent + badge;
    }
  }
  
  const items = section.items || [];
  if (!items.length) {
    if (listEl) listEl.innerHTML = '<div class="empty" style="color:#666; text-align:center; padding:10px;">데이터 없음</div>';
    return;
  }
  
  if (listEl) {
    listEl.innerHTML = items.slice(0, 5).map((r, i) => {
      const medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][i] || `${i + 1}`;
      const priceStr = formatPriceUnified(r.price);
      const changeColor = r.change_pct >= 0 ? "#4caf50" : "#ff5252";
      const scoreColor = r.score >= 50 ? "#4caf50" : (r.score >= 30 ? "#ff9800" : "#888");
      
      return `
        <div style="display:flex; justify-content:space-between; align-items:center; padding:6px 4px; border-bottom:1px solid #333; cursor:pointer;" onclick="window.open('/ui/market_detail.html?market=${encodeURIComponent(r.market)}', '_blank')">
          <div style="flex:1;">
            <span style="font-size:14px;">${medal}</span>
            <span style="font-weight:600; margin-left:4px;">${extractBase(r.market || r.symbol || "")}</span>
            <span style="color:#4caf50; font-size:10px; margin-left:2px;">${priceStr}</span>
            <div style="font-size:10px; margin-top:2px;">
              <span style="color:${changeColor};">${r.change_pct >= 0 ? "+" : ""}${r.change_pct}%</span>
              <span style="color:#888; margin-left:4px;">AI ${(r.ai_score * 100).toFixed(0)}%</span>
            </div>
          </div>
          <div style="text-align:right; min-width:60px;">
            <span style="color:${scoreColor}; font-weight:bold; font-size:12px;">점수 ${r.score}</span>
          </div>
        </div>
      `;
    }).join("");
  }
}

// ============================================================
// Strategy Tab UI - Lazy Loading & Sparkline
// ============================================================

const strategyTabState = {
  currentTab: null,
  cache: {},
  selectedTimeframe: '1h',
};

function renderSparkline(prices) {
  if (!prices || !Array.isArray(prices) || prices.length < 2) return '';
  
  const validPrices = prices.filter(p => typeof p === 'number' && isFinite(p));
  if (validPrices.length < 2) return '';
  
  const min = Math.min(...validPrices);
  const max = Math.max(...validPrices);
  const range = max - min || 1;
  
  const points = validPrices.map((p, i) => {
    const x = (i / (validPrices.length - 1)) * 50;
    const y = 12 - ((p - min) / range) * 10;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  
  const color = validPrices[validPrices.length - 1] >= validPrices[0] ? "#4caf50" : "#ff5252";
  
  return `
    <span class="sparkline-container">
      <svg width="50" height="14" style="vertical-align:middle;">
        <polyline points="${points}" fill="none" stroke="${color}" stroke-width="1.2"/>
      </svg>
    </span>
  `;
}

/**
 * 멀티 타임프레임 미니 스파크라인 렌더링
 * @param {Object} priceHistory - {"5m": [...], "15m": [...], "1h": [...], "4h": [...], "1d": [...]}
 * @returns {string} HTML string
 */
function renderMultiSparkline(priceHistory) {
  if (!priceHistory || typeof priceHistory !== 'object') return '';
  
  const timeframes = ['5m', '15m', '1h', '4h', '1d'];
  const labels = { '5m': '5분', '15m': '15분', '1h': '1시간', '4h': '4시간', '1d': '1일' };
  
  const sparkHtml = timeframes.map(tf => {
    const prices = priceHistory[tf];
    if (!prices || !Array.isArray(prices) || prices.length < 2) {
      return `<span class="spark-empty" title="${labels[tf]}">—</span>`;
    }
    return renderSparklineSvg(prices, tf, labels[tf]);
  }).join('');
  
  return `<span class="spark-container">${sparkHtml}</span>`;
}

/**
 * 개별 타임프레임 SVG 스파크라인 렌더링 (작은 버전, 멀티 타임프레임용)
 */
function renderSparklineSvg(prices, tf, label) {
  const validPrices = prices.filter(p => typeof p === 'number' && isFinite(p));
  if (validPrices.length < 2) return `<span class="spark-empty" title="${label}">—</span>`;
  
  const min = Math.min(...validPrices);
  const max = Math.max(...validPrices);
  const range = max - min || 1;
  
  const points = validPrices.map((p, i) => {
    const x = (i / (validPrices.length - 1)) * 28;
    const y = 10 - ((p - min) / range) * 8;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  
  const isUp = validPrices[validPrices.length - 1] >= validPrices[0];
  const color = isUp ? "#4caf5080" : "#ff525280";  // 50% 투명도
  
  const changePct = ((validPrices[validPrices.length - 1] - validPrices[0]) / validPrices[0] * 100).toFixed(2);
  const tooltip = `${label}: ${isUp ? '+' : ''}${changePct}%`;
  
  return `
    <span class="spark-item" title="${tooltip}">
      <svg width="30" height="12">
        <polyline points="${points}" fill="none" stroke="${color}" stroke-width="1.5"/>
      </svg>
    </span>
  `;
}

/**
 * 3등분 스파크라인 (5분, 4시간, 1일)
 */
function renderTripleSparkline(priceHistory) {
  if (!priceHistory || typeof priceHistory !== 'object') return '';
  
  const timeframes = ['5m', '4h', '1d'];
  const labels = { '5m': '5분', '4h': '4시간', '1d': '1일' };
  
  const charts = timeframes.map(tf => {
    const prices = priceHistory[tf];
    if (!prices || !Array.isArray(prices) || prices.length < 2) {
      return `<div style="flex:1; text-align:center; color:#444; font-size:8px;">${labels[tf]}:—</div>`;
    }
    
    const validPrices = prices.filter(p => typeof p === 'number' && isFinite(p));
    if (validPrices.length < 2) {
      return `<div style="flex:1; text-align:center; color:#444; font-size:8px;">${labels[tf]}:—</div>`;
    }
    
    const min = Math.min(...validPrices);
    const max = Math.max(...validPrices);
    const range = max - min || 1;
    const change = ((validPrices[validPrices.length - 1] - validPrices[0]) / validPrices[0] * 100).toFixed(1);
    
    const points = validPrices.map((p, i) => {
      const x = (i / (validPrices.length - 1)) * 100;
      const y = 16 - ((p - min) / range) * 14;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    }).join(' ');
    
    const isUp = validPrices[validPrices.length - 1] >= validPrices[0];
    const color = isUp ? "#4caf50" : "#ff5252";
    
    return `
      <div style="flex:1; text-align:center;" title="${labels[tf]}: ${isUp ? '+' : ''}${change}%">
        <svg viewBox="0 0 100 18" preserveAspectRatio="none" style="width:100%; height:16px; opacity:0.7;">
          <polyline points="${points}" fill="none" stroke="${color}" stroke-width="1.5"/>
        </svg>
        <div style="font-size:7px; color:#666;">${labels[tf]}</div>
      </div>
    `;
  });
  
  return `<div style="display:flex; gap:2px;">${charts.join('')}</div>`;
}

/**
 * 선택된 타임프레임의 큰 스파크라인 렌더링 (120x24px, 끝에서 끝까지)
 */
function renderSingleSparkline(priceHistory, timeframe) {
  const labels = { '5m': '5분', '15m': '15분', '1h': '1시간', '4h': '4시간', '1d': '1일' };
  const prices = priceHistory?.[timeframe];
  
  if (!prices || !Array.isArray(prices) || prices.length < 2) {
    return `<span class="spark-empty" title="${labels[timeframe] || timeframe}">—</span>`;
  }
  
  const validPrices = prices.filter(p => typeof p === 'number' && isFinite(p));
  if (validPrices.length < 2) return `<span class="spark-empty">—</span>`;
  
  const W = 120, H = 24, PAD = 1;
  const min = Math.min(...validPrices);
  const max = Math.max(...validPrices);
  const range = max - min || 1;
  const change = ((validPrices[validPrices.length - 1] - validPrices[0]) / validPrices[0] * 100).toFixed(1);
  
  const points = validPrices.map((p, i) => {
    const x = PAD + (i / (validPrices.length - 1)) * (W - 2 * PAD);
    const y = PAD + (H - 2 * PAD) - ((p - min) / range) * (H - 2 * PAD);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  
  const isUp = validPrices[validPrices.length - 1] >= validPrices[0];
  const color = isUp ? "#4caf50" : "#ff5252";
  const tooltip = `${labels[timeframe] || timeframe}: ${isUp ? '+' : ''}${change}%`;
  
  return `
    <span class="spark-single" title="${tooltip}">
      <svg width="${W}" height="${H}" style="background:rgba(255,255,255,0.02); border-radius:3px;">
        <polyline points="${points}" fill="none" stroke="${color}" stroke-width="1.5" opacity="0.8"/>
      </svg>
    </span>
  `;
}

/**
 * 기술 지표 배지 렌더링 (RSI, MACD, BB)
 */
function renderIndicatorBadges(item) {
  const badges = [];
  
  // RSI
  const rsi = item.rsi || item.details?.rsi_score;
  if (rsi !== undefined) {
    const rsiColor = rsi < 30 ? "#ff5252" : (rsi > 70 ? "#4caf50" : "#888");
    const rsiLabel = rsi < 30 ? "과매도" : (rsi > 70 ? "과매수" : "");
    badges.push(`<span style="font-size:9px; color:${rsiColor};" title="RSI ${rsi}">RSI:${Math.round(rsi)}${rsiLabel ? ' ' + rsiLabel : ''}</span>`);
  }
  
  // MACD
  const macd = item.macd || item.details?.macd_score;
  if (macd !== undefined) {
    const macdColor = macd > 0 ? "#4caf50" : (macd < 0 ? "#ff5252" : "#888");
    const macdLabel = macd > 0 ? "↑" : (macd < 0 ? "↓" : "");
    badges.push(`<span style="font-size:9px; color:${macdColor};" title="MACD ${macd}">MACD:${macdLabel}</span>`);
  }
  
  // Bollinger Bands (BB position)
  const bb = item.bb_position || item.details?.bb_position;
  if (bb !== undefined) {
    const bbPct = Math.round(bb * 100);
    const bbColor = bb < 0.2 ? "#ff5252" : (bb > 0.8 ? "#4caf50" : "#888");
    const bbLabel = bb < 0.2 ? "하단" : (bb > 0.8 ? "상단" : "중간");
    badges.push(`<span style="font-size:9px; color:${bbColor};" title="BB ${bbPct}%">BB:${bbLabel}</span>`);
  }
  
  // Ichimoku
  const ichimoku = item.details?.ichimoku_score;
  if (ichimoku !== undefined) {
    const ichiColor = ichimoku >= 50 ? "#4caf50" : "#888";
    badges.push(`<span style="font-size:9px; color:${ichiColor};" title="일목 ${ichimoku}">일목:${ichimoku}</span>`);
  }
  
  if (badges.length === 0) return '';
  return `<div style="display:flex; gap:6px; margin-top:2px;">${badges.join('')}</div>`;
}

/**
 * 지표 배지 항상 표시 (RSI, MACD, BB - 데이터 없으면 기본값)
 */
function renderIndicatorBadgesAlways(item) {
  const parts = [];
  
  // RSI (항상 표시)
  const rsi = item.rsi ?? item.details?.rsi_score ?? item.analysis?.rsi ?? null;
  if (rsi !== null) {
    const c = rsi < 30 ? "#ff5252" : (rsi > 70 ? "#4caf50" : "#888");
    const label = rsi < 30 ? " 과매도" : (rsi > 70 ? " 과매수" : "");
    parts.push(`<span style="color:${c};">RSI:${Math.round(rsi)}${label}</span>`);
  } else {
    parts.push(`<span style="color:#555;">RSI:—</span>`);
  }
  
  // MACD (항상 표시)
  const macd = item.macd ?? item.details?.macd_score ?? null;
  if (macd !== null) {
    const c = macd > 20 ? "#4caf50" : "#ff5252";
    const arrow = macd > 20 ? "↑" : "↓";
    parts.push(`<span style="color:${c};">MACD:${arrow}</span>`);
  } else {
    parts.push(`<span style="color:#555;">MACD:—</span>`);
  }
  
  // BB (항상 표시)
  const bb = item.bb_position ?? item.details?.bb_position ?? item.analysis?.bb_position ?? null;
  if (bb !== null) {
    const pct = Math.round(bb * 100);
    const c = bb < 0.2 ? "#ff5252" : (bb > 0.8 ? "#4caf50" : "#888");
    const label = bb < 0.2 ? "하단" : (bb > 0.8 ? "상단" : `${pct}%`);
    parts.push(`<span style="color:${c};">BB:${label}</span>`);
  } else {
    parts.push(`<span style="color:#555;">BB:—</span>`);
  }
  
  return parts.join(' <span style="color:#333;">|</span> ');
}

function initStrategyTabs() {
  const tabs = document.querySelectorAll('.strategy-tab');
  if (!tabs.length) return;
  
  tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      tabs.forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      loadStrategyTab(tab.dataset.tab, tab.dataset.api);
    });
  });
  
  // 타임프레임 선택 이벤트
  const tfSelect = document.getElementById('sparkTimeframe');
  if (tfSelect) {
    tfSelect.addEventListener('change', () => {
      strategyTabState.selectedTimeframe = tfSelect.value;
      // 현재 탭 다시 렌더링 (캐시 사용)
      if (strategyTabState.currentTab && strategyTabState.cache[strategyTabState.currentTab]) {
        const contentEl = qs("strategyTabContent");
        const allEl = qs("strategyTabAllRankings");
        if (contentEl) {
          renderStrategyTabContent(strategyTabState.currentTab, strategyTabState.cache[strategyTabState.currentTab], contentEl, allEl);
        }
      }
    });
  }
  
  if (tabs[0]) tabs[0].click();
}

async function loadStrategyTab(tabName, apiUrl) {
  const contentEl = qs("strategyTabContent");
  const allEl = qs("strategyTabAllRankings");
  
  if (!contentEl) return;
  
  strategyTabState.currentTab = tabName;
  
  if (strategyTabState.cache[tabName]) {
    renderStrategyTabContent(tabName, strategyTabState.cache[tabName], contentEl, allEl);
    return;
  }
  
  contentEl.innerHTML = '<div class="strategy-tab-loading">Loading...</div>';
  if (allEl) allEl.innerHTML = '<div class="empty" style="color:#666;">로딩 중...</div>';
  
  try {
    const res = await fetch(apiUrl + "?top_n=20&min_volume_usdt=1000000");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    
    strategyTabState.cache[tabName] = data;
    
    if (strategyTabState.currentTab === tabName) {
      renderStrategyTabContent(tabName, data, contentEl, allEl);
    }
  } catch (e) {
    console.error(`loadStrategyTab(${tabName}) error:`, e);
    if (strategyTabState.currentTab === tabName) {
      contentEl.innerHTML = `<div class="strategy-tab-error">로드 실패: ${e.message}</div>`;
      if (allEl) allEl.innerHTML = '<div class="empty" style="color:#ff5252;">오류 발생</div>';
    }
  }
}

function renderStrategyTabContent(tabName, data, contentEl, allEl) {
  if (!data) {
    contentEl.innerHTML = '<div class="empty" style="color:#666; text-align:center; padding:20px;">데이터 없음</div>';
    if (allEl) allEl.innerHTML = '<div class="empty" style="color:#666;">없음</div>';
    return;
  }
  
  const rankings = data.rankings || data.items || [];
  if (!rankings.length) {
    const emptyMsg = getTabEmptyMessage(tabName);
    contentEl.innerHTML = `<div class="empty" style="color:#666; text-align:center; padding:20px;">${emptyMsg}</div>`;
    if (allEl) allEl.innerHTML = '<div class="empty" style="color:#666;">없음</div>';
    return;
  }
  
  const formatPrice = (p) => {
    if (!p || p <= 0) return "";
    if (p >= 1000) return "$" + p.toFixed(0);
    if (p >= 1) return "$" + p.toFixed(2);
    if (p >= 0.01) return "$" + p.toFixed(4);
    return "$" + p.toFixed(6);
  };
  
  const formatVol = (v) => {
    if (v >= 1e9) return "$" + (v / 1e9).toFixed(1) + "B";
    if (v >= 1e6) return "$" + (v / 1e6).toFixed(0) + "M";
    if (v >= 1e3) return "$" + (v / 1e3).toFixed(0) + "K";
    return "$" + v.toFixed(0);
  };
  
  const top5 = rankings.slice(0, 5);
  const rest = rankings.slice(5, 20);
  
  contentEl.innerHTML = top5.map((r, i) => {
    const medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][i] || `${i + 1}`;
    const market = r.market || r.symbol || '';
    const currency = r.currency || extractBase(market);
    const price = r.current_price || r.price || 0;
    const priceStr = formatPrice(price);
    
    const changeRate = r.change_rate ?? r.change_pct ?? 0;
    const changeColor = changeRate >= 0 ? "#4caf50" : "#ff5252";
    
    const score = r.rebound_score ?? r.total_score ?? r.upside_score ?? r.score ?? 0;
    const scoreColor = score >= 60 ? "#4caf50" : (score >= 40 ? "#ff9800" : "#888");
    
    // 3등분 스파크라인 (5분, 4시간, 1일)
    const sparklineHtml = r.price_history && typeof r.price_history === 'object' && Object.keys(r.price_history).length > 0
      ? renderTripleSparkline(r.price_history)
      : '';
    
    const activeStrat = r.active_strategy || null;
    const activeTag = activeStrat
      ? `<span style="color:#ff9800; font-size:9px;">⚠️${activeStrat}</span>`
      : '';
    
    // 지표 배지 (항상 표시)
    const indicatorHtml = renderIndicatorBadgesAlways(r);
    
    return `
      <div style="padding:6px 4px; border-bottom:1px solid #333; cursor:pointer;" data-market="${escHtml(market)}" data-active-strategy="${escHtml(activeStrat || '')}" onclick="handleStrategyTabClick(this, '${escHtml(market)}', '${escHtml(activeStrat || '')}')">
        <div style="display:flex; align-items:center; gap:4px;">
          <span style="font-size:12px; min-width:18px;">${medal}</span>
          <span style="font-weight:600; font-size:11px; min-width:50px;">${currency}</span>
          <span style="color:#4caf50; font-size:9px; min-width:55px;">${priceStr}</span>
          ${activeTag}
          <div style="flex:1; margin:0 4px;">${sparklineHtml}</div>
          <span style="color:${scoreColor}; font-weight:bold; font-size:11px; min-width:35px; text-align:right;">${score}점</span>
          <span style="color:${changeColor}; font-size:10px; min-width:40px; text-align:right;">${changeRate >= 0 ? "+" : ""}${changeRate.toFixed(1)}%</span>
        </div>
        <div style="font-size:9px; margin-top:3px; margin-left:20px;">
          ${indicatorHtml}
        </div>
      </div>
    `;
  }).join("");
  
  if (allEl) {
    if (rest.length > 0) {
      const selectedTf = strategyTabState.selectedTimeframe || '1h';
      allEl.innerHTML = rest.map((r, i) => {
        const rank = i + 6;
        const market = r.market || r.symbol || '';
        const currency = r.currency || extractBase(market);
        const price = r.current_price || r.price || 0;
        const priceStr = formatPrice(price);
        
        const changeRate = r.change_rate ?? r.change_pct ?? 0;
        const changeColor = changeRate >= 0 ? "#4caf50" : "#ff5252";
        
        const score = r.rebound_score ?? r.total_score ?? r.upside_score ?? r.score ?? 0;
        
        const activeStrat = r.active_strategy || null;
        const activeTag = activeStrat
          ? `<span class="badge-active-strategy">📌 ${activeStrat}</span>`
          : '';
        
        // 스파크라인 (더보기는 작게: 80x16px)
        const sparkHtml = r.price_history && typeof r.price_history === 'object' 
          ? renderSmallSparkline(r.price_history, selectedTf)
          : '';
        
        // 지표 배지 (간략 버전)
        const indicators = renderIndicatorBadgesCompact(r);
        
        return `
          <div style="display:flex; justify-content:space-between; align-items:center; padding:4px 0; border-bottom:1px solid #222; cursor:pointer;" data-market="${escHtml(market)}" data-active-strategy="${escHtml(activeStrat || '')}" onclick="handleStrategyTabClick(this, '${escHtml(market)}', '${escHtml(activeStrat || '')}')">
            <div style="flex:1;">
              <span style="color:#666; font-size:9px;">#${rank}</span>
              <span style="font-weight:500;">${currency}</span>
              <span style="color:#4caf50; font-size:9px;">${priceStr}</span>
              ${activeTag}
              ${sparkHtml}
              <div style="font-size:8px; color:#666;">${indicators}</div>
            </div>
            <div style="text-align:right;">
              <span style="font-size:10px;">${score}점</span>
              <span style="color:${changeColor}; margin-left:4px; font-size:10px;">${changeRate >= 0 ? "+" : ""}${changeRate.toFixed(1)}%</span>
            </div>
          </div>
        `;
      }).join("");
    } else {
      allEl.innerHTML = '<div class="empty" style="color:#666;">5개 이하</div>';
    }
  }
}

/**
 * 더보기용 작은 스파크라인 (80x16px)
 */
function renderSmallSparkline(priceHistory, timeframe) {
  const prices = priceHistory?.[timeframe];
  if (!prices || !Array.isArray(prices) || prices.length < 2) return '';
  
  const validPrices = prices.filter(p => typeof p === 'number' && isFinite(p));
  if (validPrices.length < 2) return '';
  
  const W = 80, H = 16, PAD = 1;
  const min = Math.min(...validPrices);
  const max = Math.max(...validPrices);
  const range = max - min || 1;
  
  const points = validPrices.map((p, i) => {
    const x = PAD + (i / (validPrices.length - 1)) * (W - 2 * PAD);
    const y = PAD + (H - 2 * PAD) - ((p - min) / range) * (H - 2 * PAD);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  
  const isUp = validPrices[validPrices.length - 1] >= validPrices[0];
  const color = isUp ? "#4caf50" : "#ff5252";
  
  return `<svg width="${W}" height="${H}" style="vertical-align:middle; margin-left:4px;"><polyline points="${points}" fill="none" stroke="${color}" stroke-width="1" opacity="0.6"/></svg>`;
}

/**
 * 더보기용 간략 지표 배지
 */
function renderIndicatorBadgesCompact(item) {
  const parts = [];
  
  const rsi = item.rsi || item.details?.rsi_score;
  if (rsi !== undefined) {
    const c = rsi < 30 ? "#ff5252" : (rsi > 70 ? "#4caf50" : "#666");
    parts.push(`<span style="color:${c};">R:${Math.round(rsi)}</span>`);
  }
  
  const macd = item.macd || item.details?.macd_score;
  if (macd !== undefined) {
    const c = macd > 0 ? "#4caf50" : "#ff5252";
    parts.push(`<span style="color:${c};">M:${macd > 0 ? '↑' : '↓'}</span>`);
  }
  
  const bb = item.bb_position || item.details?.bb_position;
  if (bb !== undefined) {
    const c = bb < 0.2 ? "#ff5252" : (bb > 0.8 ? "#4caf50" : "#666");
    parts.push(`<span style="color:${c};">B:${Math.round(bb * 100)}%</span>`);
  }
  
  return parts.join(' ');
}

function getTabEmptyMessage(tabName) {
  const messages = {
    'rebound': '급락 코인 없음 (시장 안정) 👍',
    'rsi': '과매도 코인 없음 (시장 과열?) 🔥',
    'tech': '매수 신호 없음',
    'upside': '데이터 없음',
  };
  return messages[tabName] || '데이터 없음';
}

function invalidateStrategyTabCache() {
  strategyTabState.cache = {};
}

/**
 * WebSocket rankings 데이터를 전략 탭에 적용
 */
function updateStrategyTabWithWsData(rankings) {
  if (!rankings) return;
  
  const tabMapping = {
    'rebound': rankings.rebound,
    'rsi': rankings.rsi_oversold,
    'tech': rankings.tech_score,
    'upside': rankings.upside
  };
  
  const currentTab = strategyTabState.currentTab;
  if (!currentTab || !tabMapping[currentTab]) return;
  
  const sectionData = tabMapping[currentTab];
  if (!sectionData || !sectionData.items) return;
  
  // API 응답 형식으로 변환하여 캐시 업데이트
  const data = { rankings: sectionData.items };
  strategyTabState.cache[currentTab] = data;
  
  // 현재 탭 다시 렌더링
  const contentEl = qs("strategyTabContent");
  const allEl = qs("strategyTabAllRankings");
  if (contentEl) {
    renderStrategyTabContent(currentTab, data, contentEl, allEl);
  }
}


function setPnlBaselineMap(mapObj) {
  try { localStorage.setItem(LS_PNL_BASELINE, JSON.stringify(mapObj || {})); } catch(e) {}
}
function getPnlBaselineMap() {
  const obj = lsGetJson(LS_PNL_BASELINE, {});
  return (obj && typeof obj === "object") ? obj : {};
}
function setPnlBaseline(market, equity) {
  const m = normMarket(market);
  if (!m) return;
  const base = getPnlBaselineMap();
  base[m] = { equity: Number(equity) || 0, ts: new Date().toISOString() };
  lsSetJson(LS_PNL_BASELINE, base);
}
function clearPnlBaseline(market) {
  const m = normMarket(market);
  if (!m) return;
  const base = getPnlBaselineMap();
  delete base[m];
  lsSetJson(LS_PNL_BASELINE, base);
}

const uniq = (arr) => Array.from(new Set(arr));

function normMarket(x) {
  if (!x) return null;
  if (typeof x === "string") return x.trim();
  if (typeof x === "object") {
    return (x.market || x.code || x.symbol || "").toString().trim();
  }
  return null;
}

function upperOrEmpty(s) {
  return (s || "").toString().trim().toUpperCase();
}

// --- OMA per-market budget helper (manual override) ---
function getOmaBudgetUsdt(state, market) {
  try {
    const mkt = upperOrEmpty(market);
    const lists = [state?.oma?.active || [], state?.oma?.watch || [], state?.oma?.recovery || []];
    for (const lst of lists) {
      for (const it of lst) {
        const nm = upperOrEmpty(normMarket(it));
        if (nm && nm === mkt) {
          const b = (it && typeof it === 'object') ? (it.budget_usdt ?? it.budgetUsdt ?? it.budget_usdt ?? it.budgetUsdt ?? it.budget) : null;
          const n = Number(b);
          return Number.isFinite(n) && n > 0 ? n : null;
        }
      }
    }
  } catch {
    // ignore
  }
  return null;
}

function fmtPrice(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return n.toLocaleString();
}

function nowSec() { return Date.now() / 1000; }

function coolRemainSec(untilTs) {
  const t = Number(untilTs);
  if (!Number.isFinite(t) || t <= 0) return 0;
  return Math.max(0, Math.round((t - nowSec()) * 10) / 10);
}

function gateLabel(state, reason, remainSec) {
  const st = (state || "").toString().toUpperCase();
  if (!st) return "—";
  if (st === "BLOCKED") {
    const rs = (reason || "unknown").toString();
    const cd = remainSec > 0 ? ` (${remainSec}s)` : "";
    return `BLOCKED: ${rs}${cd}`;
  }
  return st;
}

function fmtNum(v, digits = 2) {
  const n = Number(v);
  if (!Number.isFinite(n)) return "—";
  return n.toFixed(digits);
}

async function fetchJson(url, opts) {
  const res = await fetch(url, opts);
  const text = await res.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch {
    data = null;
  }
  if (!res.ok) {
    const msg = (data && (data.detail || data.error)) || text || res.statusText;
    throw new Error(`HTTP ${res.status}: ${msg}`);
  }
  return data;
}

function setAdminMsg(msg, ok = true) {
  const el = qs("adminMsg");
  if (!el) return;
  el.textContent = msg;
  el.style.color = ok ? "var(--ok)" : "var(--danger)";
}

function setGuardMsg(msg) {
  const el = qs("guardMsg");
  if (el) el.textContent = msg;
}

/* =========================
 * Contract-flex parsing
 * ========================= */
function getCtx(market) {
  return (state.coordinator && state.coordinator[market]) || {};
}

function getReadiness(ctx) {
  return ctx.readiness || ctx.readiness_status || ctx.readinessStatus || {};
}

function getStrategy(ctx) {
  // 다양한 이전/실험 버전 키 흡수
  return (
    ctx.strategy ||
    ctx.strategy_state ||
    ctx.strategyState ||
    ctx.strategy_snapshot ||
    {}
  );
}

function getRisk(ctx) {
  return ctx.risk || ctx.risk_state || ctx.riskState || ctx.risk_snapshot || {};
}

function deriveManagedMarkets(sys) {
  const oma = sys.oma || {};

  const active = (oma.active || []).map(normMarket).filter(Boolean);
  const recovery = (oma.recovery || []).map(normMarket).filter(Boolean);

  let managed = uniq([...active, ...recovery]);

  // fallback: 만약 OMA가 비었는데도 active_prices가 존재하면 그것으로 보정
  if (!managed.length) {
    const prices = sys.active_prices || sys.activePrices || {};
    managed = uniq(Object.keys(prices || {}));
  }

  return managed;
}

function deriveAllKnownMarkets(sys) {
  const oma = sys.oma || {};
  const watch = (oma.watch || []).map(normMarket).filter(Boolean);
  const active = (oma.active || []).map(normMarket).filter(Boolean);
  const recovery = (oma.recovery || []).map(normMarket).filter(Boolean);

  const coord = Object.keys(sys.coordinator || {});
  const prices = Object.keys(sys.active_prices || sys.activePrices || {});

  return uniq([...watch, ...active, ...recovery, ...coord, ...prices]).sort();
}

/* =========================
 * RENDER: Header
 * ========================= */
function renderHeaderStats() {
  const sys = state.system || {};

  const engineLabelRaw = sys.engine_state || sys.engine || sys.name || "—";
  const engineLabel = typeof engineLabelRaw === "string" ? engineLabelRaw.toUpperCase() : engineLabelRaw;
  const mode = sys.trading_mode || sys.mode || "—";
  const emergency =
    sys.emergency_stop === true || sys.emergency === true
      ? "ON"
      : sys.emergency_stop === false || sys.emergency === false
      ? "OFF"
      : "—";

  if (qs("engineState")) qs("engineState").textContent = engineLabel;

  const readyCount = state.managedMarkets.filter((m) =>
    Boolean(getReadiness(getCtx(m)).ready)
  ).length;

  const stratCount = state.managedMarkets.filter((m) => {
    const s = getStrategy(getCtx(m));
    return Boolean(s.selected || s.bias);
  }).length;

  if (qs("activeMarkets")) qs("activeMarkets").textContent = state.managedMarkets.length;
  if (qs("readyMarkets")) qs("readyMarkets").textContent = readyCount;
  if (qs("activeStrategies")) qs("activeStrategies").textContent = stratCount || "—";
  if (qs("activePricesCount")) {
    const n = Object.values(state.prices || {}).filter(
      (v) => v !== null && v !== undefined
    ).length;
    qs("activePricesCount").textContent = n || "—";
  }

  if (qs("tradingMode")) qs("tradingMode").textContent = mode;
  if (qs("emergencyStop")) qs("emergencyStop").textContent = emergency;

  // -------------------------------------------------
  // ENV / DEFENSIVE badge (single source of truth)
  // -------------------------------------------------
  const badge = qs("envBadge");
  const def = sys._oma_defensive;

  if (!badge) return;

  // DEFENSIVE MODE가 최우선
  if (def && def.enabled === true) {
    badge.textContent = "DEFENSIVE";
    badge.style.background = "var(--danger)";
    badge.style.color = "#fff";
  } else {
    // 정상 ENV 표시
    badge.textContent = mode;
    badge.style.background = "";
    badge.style.color = mode.toUpperCase().includes("LIVE")
      ? "var(--ok)"
      : "var(--warn)";
  }

  // [NEW] Tick Load Visualization (Heartbeat)
  const perf = sys.performance || {};
  const tickDur = perf.tick_duration || 0;
  const tickMs = Math.round(tickDur * 1000);
  const tickEl = qs("systemTickLoad"); // UI에 이 ID가 있다면 업데이트
  if (tickEl) {
    tickEl.textContent = `${tickMs}ms`;
    // 50ms 미만: 아주 좋음(녹색), 200ms 이상: 부하 있음(노란색), 500ms 이상: 느림(빨간색)
    tickEl.style.color = tickMs < 50 ? "var(--ok)" : (tickMs < 200 ? "var(--warn)" : "var(--danger)");
    tickEl.title = `Engine Cycle: ${tickMs}ms (Lower is better)`;
  }
}


/* =========================
 * RENDER: Managed Markets
 * ========================= */
function renderMarketRegistry() {
  const wrap = qs("marketList");
  if (!wrap) return;
  clear(wrap);

  if (!state.managedMarkets.length) {
    wrap.innerHTML = `<div class="empty">ACTIVE 없음</div>`;
    return;
  }

  state.managedMarkets.forEach((mkt) => {
    const ctx  = getCtx(mkt);
    const r    = getReadiness(ctx);
    const s    = getStrategy(ctx);
    const risk = getRisk(ctx);
    const attempts = ctx.attempt_count ?? 0;
    const suspicionLevel = risk.reason?.suspicion_level || "—";
    // -------------------------------------------------
    // INDICATORS (cheap): reuse engine snapshot in ctx.strategy.reason.engine_ai
    // - no extra API call, no extra indicator computation on UI
    // -------------------------------------------------
    const strat = getStrategy(ctx);
    const sReason = strat?.reason || {};
    const brain = sReason.engine_ai || sReason.engineAi || null;

    const aiScore = (brain && Number.isFinite(brain.ai_prediction)) ? brain.ai_prediction : null;

    // AI Trend Tracking (이전 점수와 비교하여 화살표 표시)
    if (!state._aiPrev) state._aiPrev = {};
    const prevAi = state._aiPrev[mkt];
    let aiTrend = "";
    if (aiScore !== null && prevAi !== null && prevAi !== undefined) {
      if (aiScore > prevAi) aiTrend = "↑";
      else if (aiScore < prevAi) aiTrend = "↓";
    }
    if (aiScore !== null) state._aiPrev[mkt] = aiScore;

    const rsi = (brain && Number.isFinite(brain.rsi)) ? brain.rsi : null;

    // prefer histogram if present
    const macd =
      (brain && Number.isFinite(brain.macd_histogram)) ? brain.macd_histogram :
      (brain && Number.isFinite(brain.macdHistogram)) ? brain.macdHistogram :
      null;

    // Optional: show configured RSI thresholds if available (still cheap)
    const sParams = ctx?.controls?.strategy?.params || {};
    const cfgBuy  = Number.isFinite(sParams.rsi_buy)  ? sParams.rsi_buy  : null;
    const cfgSell = Number.isFinite(sParams.rsi_sell) ? sParams.rsi_sell : null;

    const tunedAt = Number.isFinite(sParams.tuned_at) ? sParams.tuned_at : null;
    const nextRt  = Number.isFinite(sParams.next_retune_ts) ? sParams.next_retune_ts : null;

    // UPSIDE POTENTIAL (상승 여력 게이지)
    const upsidePotential = calcUpsidePotential(brain);

    const buySplits = Array.isArray(sParams.buy_splits) ? sParams.buy_splits : null;
    const addTrigs  = Array.isArray(sParams.add_buy_drop_pcts) ? sParams.add_buy_drop_pcts : null;

    let rtLabel = "";
    if (nextRt) {
      const secLeft = Math.max(0, Math.floor(nextRt - Date.now() / 1000));
      rtLabel = ` · Retune <b>${Math.floor(secLeft / 60)}</b>m`;
    }

    const cfgLabel =
      `CFG RSI <b>${cfgBuy ?? "—"}</b>/<b>${cfgSell ?? "—"}</b>` +
      (buySplits ? ` · Split <b>${buySplits.map(x => fmtNum(x,2)).join("/")}</b>` : "") +
      (addTrigs ? ` · Trig <b>${addTrigs.map(x => fmtNum(x,1)).join("/")}%</b>` : "") +
      rtLabel;

    let aiLabel = `AI <b>—</b>`;
    if (!Boolean(r.ready)) {
      aiLabel = `AI <small style="opacity:0.7">Wait</small>`;
    } else if (aiScore !== null) {
      const c = (aiScore >= 0.6) ? "var(--ok)" : (aiScore <= 0.4) ? "var(--danger)" : "";
      const vol = (brain.volatility !== undefined) ? fmtNum(brain.volatility, 1) : "?";
      const mom = (brain.momentum !== undefined) ? fmtNum(brain.momentum, 1) : "?";
      aiLabel = `<span style="${c ? 'color:'+c : ''}" title="Score: ${aiScore.toFixed(4)}\nVol: ${vol}%\nMom: ${mom}%">AI <b>${fmtNum(aiScore, 2)}</b>${aiTrend}</span>`;
    }

    const rsiLabel =
      (rsi !== null) ? `RSI <b>${fmtNum(rsi, 1)}</b> · ${cfgLabel}` : `RSI <b>—</b> · ${cfgLabel}`;


    const macdLabel =
      (macd !== null) ? `MACD <b>${fmtNum(macd, 3)}</b>` : `MACD <b>—</b>`;

    // -------------------------------------------------
    // LAST BUY (cheap): for PINGPONG display
    // - no extra API call, no indicator computation
    // - prefer pos.usdt, else entry*qty approximation
    // -------------------------------------------------
    const pos = ctx?.position || null;
    const qty = pos ? numOr(pos.qty, 0) : 0;
    const entry = pos ? numOr(pos.entry, 0) : 0;

    const lastBuyUsdt =
      pos && Number.isFinite(pos.usdt) ? numOr(pos.usdt, 0) :
      (qty > 0 && entry > 0) ? (qty * entry) :
      null;

    const lastBuyLabel =
      (lastBuyUsdt !== null)
        ? `Entry <b>${fmtNum(entry, 2)}</b>`
        : `Entry <b>—</b>`;

    const card = document.createElement("div");
    card.className = "market-card";
    card.dataset.market = mkt;

    if (state.selectedMarket === mkt) {
      card.classList.add("selected");
    }

    const ready = Boolean(r.ready);
    const statusLabel = ready ? "READY" : "WARMUP";
    const stratName = upperOrEmpty(pickStrategyName(ctx)) || "—";

    // Strategy badge color (통일: Guard Matrix 기준)
    const stratColors = {
      'LADDER': '#f5a623',
      'LIGHTNING': '#4fc3f7',
      'GAZUA': '#66bb6a',
      'PINGPONG': '#9fa8da',
      'AUTOLOOP': '#ef9a9a',
      'LONGHOLD': '#ffb74d',
      'AI': '#888',
      'OFF': '#555'
    };
    const stratColor = stratColors[stratName.toUpperCase()] || '#888';

    // -------------------------------------------------
    // STRATEGY META (why HOLD?)
    // - prefer engine-provided strategy_out.meta (same values used by decision)
    // - show per-condition lamps even when signal=HOLD
    // -------------------------------------------------
    const strategyOut = sReason.strategy_out || sReason.strategyOut || null;
    const soMeta = (strategyOut && typeof strategyOut === "object")
      ? (strategyOut.meta || strategyOut.Meta || null)
      : null;

    const num = (v) => {
      const n = Number(v);
      return Number.isFinite(n) ? n : null;
    };

    const mkLampHtml = (label, state, title) => {
      const st = (state === "pass" || state === "block" || state === "off") ? state : "off";
      return `
        <div class="lampbox" title="${escHtml(title || "")}">
          <span class="lamp ${st}"></span>
          <span class="lamp-label">${escHtml(label)}</span>
        </div>
      `;
    };

    let stratDetailHtml = "";
    let stratDetailTitle = "";
    let condStripHtml = "";

    if (stratName === "PINGPONG") {
      const levels = (soMeta && typeof soMeta.levels === "object") ? soMeta.levels : null;

      const anchor   = num(levels?.anchor);
      const buyPrice = num(levels?.buy_price);
      const curPrice = (num(soMeta?.price) ?? num(levels?.price) ?? num(state.prices[mkt]));

      const distUsdt = (curPrice !== null && buyPrice !== null) ? (curPrice - buyPrice) : null;
      const distPct = (distUsdt !== null && buyPrice && buyPrice > 0)
        ? ((curPrice / buyPrice - 1) * 100.0)
        : null;

      const bandOk = (curPrice !== null && buyPrice !== null) ? (curPrice <= buyPrice) : null;
      const reBlocked = !!levels?.reentry_blocked;

      stratDetailHtml =
        aiLabel + " · " +
        `${lastBuyLabel}` +
        ` · A <b>${anchor !== null ? fmtPrice(anchor) : "—"}</b>` +
        ` · Buy <b>${buyPrice !== null ? fmtPrice(buyPrice) : "—"}</b>` +
        ` · Δ <b>${distPct !== null ? (fmtNum(distPct, 2) + "%") : "—"}</b>` +
        ` (${distUsdt !== null ? fmtPrice(distUsdt) : "—"} USDT)`;

      stratDetailTitle =
        `PingPong\n` +
        `anchor=${anchor ?? "—"}\n` +
        `buy_price=${buyPrice ?? "—"}\n` +
        `price=${curPrice ?? "—"}\n` +
        `dist=${distUsdt ?? "—"} USDT (${distPct !== null ? fmtNum(distPct, 2) : "—"}%)`;

      const lamps = [];
      const bandState = (bandOk === null) ? "off" : (bandOk ? "pass" : "block");
      lamps.push(
        mkLampHtml(
          "BAND",
          bandState,
          `BUY band: price <= buy_price\nprice=${curPrice ?? "—"}\nbuy_price=${buyPrice ?? "—"}\ndist=${distUsdt ?? "—"} USDT (${distPct !== null ? fmtNum(distPct, 2) : "—"}%)`
        )
      );
      const reState = reBlocked ? "block" : "pass";
      lamps.push(
        mkLampHtml(
          "RE",
          reState,
          `Re-entry cooldown\nreentry_blocked=${reBlocked ? "TRUE" : "FALSE"}`
        )
      );
      // Exit META (PINGPONG) - best-effort visualization (engine must provide meta.exit)
      const exit = (soMeta && typeof soMeta.exit === "object") ? soMeta.exit : null;
      if (exit) {
        const trig = (exit.trigger && typeof exit.trigger === "object") ? exit.trigger
          : (exit.triggers && typeof exit.triggers === "object") ? exit.triggers
          : {};

        const trailHit = !!(exit.triggered === true || exit.hit === true || exit.fired === true ||
          trig.trail || trig.trailing || trig.TRAIL);

        const rsiHit = !!(trig.rsi_dampen || trig.rsi || trig.RSI || exit.rsi_dampen_hit);
        const macdHit = !!(trig.macd_dampen || trig.macd || trig.MACD || exit.macd_dampen_hit);
        const bandHit = !!(trig.band_reject || trig.band || trig.BAND || exit.band_reject);

        const high = num(exit.high_N ?? exit.high ?? exit.H);
        const distHighPct = num(exit.dist_from_high_pct);
        const trailPct = num(exit.trail_pct ?? exit.trailing_pct);
        const rsiNow2 = num(exit.rsi_now ?? exit.rsi);
        const rsiPeak2 = num(exit.rsi_peak);
        const macdStreak = num(exit.macd_down_streak);

        // Show EXIT lamps (hit => green). Off if meta is present but not hit.
        lamps.push(
          mkLampHtml(
            "TRAIL",
            trailHit ? "pass" : "off",
            `Trailing exit
high=${high !== null ? fmtPrice(high) : "—"}
dist_from_high=${distHighPct !== null ? fmtNum(distHighPct, 3) : "—"}%
trail=${trailPct !== null ? fmtNum(trailPct * 100, 3) : "—"}%`
          )
        );
        lamps.push(
          mkLampHtml(
            "RSI↓",
            rsiHit ? "pass" : "off",
            `RSI dampen
rsi_now=${rsiNow2 !== null ? fmtNum(rsiNow2, 2) : "—"}
rsi_peak=${rsiPeak2 !== null ? fmtNum(rsiPeak2, 2) : "—"}`
          )
        );
        lamps.push(
          mkLampHtml(
            "MACD↓",
            macdHit ? "pass" : "off",
            `MACD dampen
macd_down_streak=${macdStreak !== null ? macdStreak : "—"}`
          )
        );
        lamps.push(
          mkLampHtml(
            "BANDx",
            bandHit ? "pass" : "off",
            `Band reject / reversion
band_reject=${bandHit ? "TRUE" : "FALSE"}`
          )
        );

        const exitReason =
          (typeof exit.reason === "string" && exit.reason) ||
          (typeof exit.mode === "string" && exit.mode) ||
          null;

        if (exitReason) {
          stratDetailHtml += ` · Exit <b>${escHtml(exitReason)}</b>`;
          stratDetailTitle = `Last exit reason: ${exitReason}`;
        }
      }

      condStripHtml = `<div class=\"cond-strip\">${lamps.join("")}</div>`;

    } else if (stratName === "AUTOLOOP") {
      const m = (soMeta && typeof soMeta === "object") ? soMeta : null;
      const th = (m && typeof m.thresholds === "object") ? m.thresholds : {};
      const filters = (m && typeof m.filters === "object") ? m.filters : {};

      const rsiNow = num(m?.rsi);
      const rsiBuy = num(th?.rsi_buy);
      const zNow   = num(m?.z);
      const zBuy   = num(th?.z_buy);
      const devNow = num(m?.dev_pct);
      const devBuy = num(th?.dev_buy_pct);
      const macdNow  = num(m?.macd_hist);
      const macdPrev = num(m?.macd_hist_prev);
      const macdUp = (macdNow !== null && macdPrev !== null) ? (macdNow > macdPrev) : null;

      const condRsi = (rsiNow !== null && rsiBuy !== null) ? (rsiNow <= rsiBuy) : null;
      const zTh = (zBuy !== null) ? (-Math.abs(zBuy)) : null;
      const condZ = (zNow !== null && zTh !== null) ? (zNow <= zTh) : null;
      const devTh = (devBuy !== null) ? (-Math.abs(devBuy)) : null;
      const condDev = (devNow !== null && devTh !== null) ? (devNow <= devTh) : null;
      const condMacd = (macdUp === null) ? null : macdUp;

      const hasAnyFilterBool =
        (typeof filters?.vol_ok === "boolean") ||
        (typeof filters?.knife_ok === "boolean") ||
        (typeof filters?.momentum_ok === "boolean");

      const condFlt = hasAnyFilterBool
        ? ((filters?.vol_ok === true) && (filters?.knife_ok === true) && (filters?.momentum_ok === true))
        : null;

      stratDetailHtml =
        aiLabel + " · " +
        `RSI <b>${rsiNow !== null ? fmtNum(rsiNow, 1) : "—"}</b>/<b>${rsiBuy !== null ? fmtNum(rsiBuy, 1) : "—"}</b>` +
        ` · Z <b>${zNow !== null ? fmtNum(zNow, 2) : "—"}</b>/<b>${zTh !== null ? fmtNum(zTh, 2) : "—"}</b>` +
        ` · DEV <b>${devNow !== null ? fmtNum(devNow, 3) : "—"}%</b>/<b>${devTh !== null ? fmtNum(devTh, 3) : "—"}%</b>` +
        ` · MACD↑ <b>${macdUp === null ? "—" : (macdUp ? "YES" : "NO")}</b>`;

      stratDetailTitle =
        `Autoloop (mean reversion)\n` +
        `RSI: ${rsiNow ?? "—"} <= ${rsiBuy ?? "—"}\n` +
        `Z: ${zNow ?? "—"} <= ${zTh ?? "—"} (z_buy=${zBuy ?? "—"})\n` +
        `DEV: ${devNow ?? "—"}% <= ${devTh ?? "—"}% (dev_buy_pct=${devBuy ?? "—"}%)\n` +
        `MACD turning up: ${macdUp === null ? "—" : (macdUp ? "YES" : "NO")} (hist=${macdNow ?? "—"}, prev=${macdPrev ?? "—"})\n` +
        `filters: vol_ok=${filters?.vol_ok ?? "—"}, knife_ok=${filters?.knife_ok ?? "—"}, momentum_ok=${filters?.momentum_ok ?? "—"}`;

      const lamps = [];
      lamps.push(
        mkLampHtml(
          "RSI",
          (condRsi === null) ? "off" : (condRsi ? "pass" : "block"),
          `RSI condition: rsi <= rsi_buy\nrsi=${rsiNow ?? "—"}\nrsi_buy=${rsiBuy ?? "—"}`
        )
      );
      lamps.push(
        mkLampHtml(
          "Z",
          (condZ === null) ? "off" : (condZ ? "pass" : "block"),
          `Z condition: z <= -z_buy\nz=${zNow ?? "—"}\nz_buy=${zBuy ?? "—"}\nthreshold=${zTh ?? "—"}`
        )
      );
      lamps.push(
        mkLampHtml(
          "DEV",
          (condDev === null) ? "off" : (condDev ? "pass" : "block"),
          `DEV condition: dev_pct <= -dev_buy_pct\ndev_pct=${devNow ?? "—"}%\ndev_buy_pct=${devBuy ?? "—"}%\nthreshold=${devTh ?? "—"}%`
        )
      );
      lamps.push(
        mkLampHtml(
          "MACD",
          (condMacd === null) ? "off" : (condMacd ? "pass" : "block"),
          `MACD turning up: hist > prev\nhist=${macdNow ?? "—"}\nprev=${macdPrev ?? "—"}`
        )
      );
      lamps.push(
        mkLampHtml(
          "FLT",
          (condFlt === null) ? "off" : (condFlt ? "pass" : "block"),
          `Filters (all must be TRUE)\nvol_ok=${filters?.vol_ok ?? "—"}\nknife_ok=${filters?.knife_ok ?? "—"}\nmomentum_ok=${filters?.momentum_ok ?? "—"}`
        )
      );

      condStripHtml = `<div class="cond-strip">${lamps.join("")}</div>`;

    } else {
      stratDetailHtml = (aiLabel + " · " + rsiLabel + " · " + macdLabel);
      stratDetailTitle = "";
      condStripHtml = "";
    }

    card.innerHTML = `
      <div class="market-head">
        <div class="market-title">
          <h3>${mkt}</h3>
        </div>
        <span class="pill ${ready ? "pill-ok" : "pill-warn"}">${statusLabel}</span>
      </div>

      <div class="meta">
        <span>Price <b>${fmtPrice(state.prices[mkt])}</b>${renderKimchiBadge(mkt, true)}</span>
        <span>
          Warm-up <b>${r.ticks ?? 0}/${r.min_ticks ?? "—"}</b>
          <small>(min ${r.min_seconds ?? "—"}s)</small>
        </span>
      </div>

      <div class="meta">
        <span>Strategy <b class="strat-badge" style="background:${stratColor}22; color:${stratColor}; padding:1px 5px; border-radius:3px; font-size:9px; font-weight:600;">${stratName}</b></span>
        <span ${stratDetailTitle ? `title="${escHtml(stratDetailTitle)}"` : ""}>${stratDetailHtml}</span>
        <span>
          Risk <b>${risk.band || "—"}</b>
          ${risk.unlock ? "<small>(UNLOCK)</small>" : ""}
        </span>
      </div>

      ${condStripHtml ? `
        <div class="meta meta-cond">
          <span class="span-all">${condStripHtml}</span>
        </div>
      ` : ""}

      ${renderPositionGauge(upsidePotential)}
    `;

    // =====================================================
    // 🔥 THIS WAS MISSING — Suspicion → UI binding
    // =====================================================
    const sg = risk.reason && risk.reason.suspicion_group;
    if (sg) {
      card.dataset.signal = sg.toLowerCase(); // red / yellow / green
    }

    card.addEventListener("click", () => selectMarket(mkt));
    wrap.appendChild(card);
  });

  // Persist AI scores for trend arrows
  try {
    if (state._aiPrev) localStorage.setItem("nunnaya_ai_prev_v1", JSON.stringify(state._aiPrev));
  } catch (_) {}
}



/* =========================
 * RENDER: Active Prices
 * ========================= */
function renderPriceBoard() {
  const grid = qs("priceGrid");
  if (!grid) return;
  clear(grid);

  const entries = Object.entries(state.prices || {});
  if (!entries.length) {
    grid.innerHTML = `<div class="empty">price 없음</div>`;
    return;
  }

  entries.forEach(([m, p]) => {
    const box = document.createElement("div");
    box.className = "price-box";
    box.dataset.market = m;
    if (state.selectedMarket === m) box.classList.add("selected");

    // Get context data for strategy and PnL
    const ctx = getCtx(m);
    const stratName = pickStrategyName(ctx) || "—";
    
    // Calculate PnL (same logic as PnL panel)
    const pos = ctx.position || null;
    const qty = pos ? numOr(pos.qty, 0) : 0;
    const entryPx = pos ? numOr(pos.entry, numOr(pos.entry_price, numOr(pos.avg_price, 0))) : 0;
    let pnlPct = NaN;
    let pnlUsdt = 0;
    
    if (qty > 0 && entryPx > 0 && p > 0) {
      pnlPct = ((p - entryPx) / entryPx) * 100;
      pnlUsdt = (p - entryPx) * qty;
    }
    
    const hasPnl = !isNaN(pnlPct);
    const pnlColor = pnlPct >= 0 ? "#4caf50" : "#ff5252";
    const pnlSign = pnlPct >= 0 ? "+" : "";
    
    // Strategy badge color (통일: Guard Matrix 기준)
    const stratColors = {
      'LADDER': '#f5a623',
      'LIGHTNING': '#4fc3f7',
      'GAZUA': '#66bb6a',
      'PINGPONG': '#9fa8da',
      'AUTOLOOP': '#ef9a9a',
      'LONGHOLD': '#ffb74d',
      'AI': '#888',
      'OFF': '#555'
    };
    const stratColor = stratColors[stratName.toUpperCase()] || '#888';

    box.innerHTML = `
      <div style="display:flex; justify-content:space-between; align-items:center;">
        <div class="price-mkt">${m}</div>
        <span class="strat-badge" style="background:${stratColor}22; color:${stratColor}; padding:1px 5px; border-radius:3px; font-size:9px; font-weight:600;">${stratName}</span>
      </div>
      <div class="price-val">${fmtPrice(p)}${renderKimchiBadge(m, true)}</div>
      ${hasPnl ? `<div style="font-size:0.7rem; color:${pnlColor}; margin-top:2px;">${pnlSign}${pnlPct.toFixed(2)}% <span style="opacity:0.7;">(${fmtKrShort(pnlUsdt)})</span></div>` : '<div style="font-size:0.7rem; color:#555; margin-top:2px;">—</div>'}
    `;

    box.addEventListener("click", () => selectMarket(m));
    grid.appendChild(box);
  });
  
  // Update Quick Trade panel with selected market
  updateQuickTradePanel();
}

/* =========================
 * Quick Trade Panel (Enhanced v2.0)
 * ========================= */
function updateQuickTradePanel() {
  const panel = qs("quickTradePanel");
  if (!panel) return;
  
  // Always show panel (supports arbitrary market input)
  panel.style.display = "block";
  
  const marketInputEl = qs("quickTradeMarketInput");
  const priceEl = qs("quickTradePrice");
  
  // If market selected, prefill input
  const market = state.selectedMarket;
  if (market && marketInputEl && !marketInputEl.value) {
    marketInputEl.value = market;
  }
  
  // Update price display for current input
  const inputMarket = marketInputEl ? marketInputEl.value.trim().toUpperCase() : "";
  if (priceEl) {
    let price = null;
    if (inputMarket) {
      // Try direct match
      price = state.prices && state.prices[inputMarket];
      // Try USDT suffix (Bybit format)
      if (!price && !inputMarket.endsWith("USDT")) {
        price = state.prices && state.prices[`${inputMarket}USDT`];
      }
    }
    priceEl.textContent = price ? `$${fmtPrice(price)}` : "$--";
  }
  
  // Update pending orders list
  updateQuickPendingOrders();
}

async function updateQuickPendingOrders() {
  const container = qs("quickPendingOrders");
  const listEl = qs("quickPendingList");
  if (!container || !listEl) return;
  
  try {
    const res = await fetch("/api/trade/quick/pending/list");
    const data = await res.json();
    if (data.ok && data.orders && data.orders.length > 0) {
      container.style.display = "block";
      listEl.innerHTML = data.orders.map(o => {
        const cond = o.conditional || {};
        const trigger = cond.trigger === "near_low" ? "최저가" : "최고가";
        const thresholdUnit = cond.threshold_mode === "quote" ? "USDT" : "%";
        const thresholdVal = cond.threshold_value || 0.2;
        const sideLabel = o.side === "buy" ? "매수" : "매도";
        const sideColor = o.side === "buy" ? "#4caf50" : "#f44336";
        return `<div style="display:flex;justify-content:space-between;align-items:center;padding:3px 0;border-bottom:1px solid #222;">
          <span><b style="color:${sideColor}">[${sideLabel}]</b> ${o.market}: ${cond.lookback_min || 15}분 ${trigger} ${thresholdVal}${thresholdUnit} 이내</span>
          <button onclick="cancelQuickOrder('${o.quick_id}')" style="padding:2px 6px;background:#c62828;border:none;color:#fff;border-radius:3px;font-size:0.65rem;cursor:pointer;">취소</button>
        </div>`;
      }).join("");
    } else {
      container.style.display = "none";
      listEl.innerHTML = "";
    }
  } catch (e) {
    container.style.display = "none";
  }
}

window.cancelQuickOrder = async function(quickId) {
  try {
    const res = await fetch(`/api/trade/quick/${quickId}/cancel`, { method: "POST" });
    const data = await res.json();
    if (data.ok) {
      showQuickTradeMsg("취소 완료", true);
      updateQuickPendingOrders();
    } else {
      showQuickTradeMsg(`취소 실패: ${data.error}`, false);
    }
  } catch (e) {
    showQuickTradeMsg(`취소 오류: ${e.message}`, false);
  }
};

async function executeQuickTrade(side) {
  const marketInputEl = qs("quickTradeMarketInput");
  const marketInput = marketInputEl ? marketInputEl.value.trim() : "";
  
  if (!marketInput) {
    showQuickTradeMsg("코인을 입력하세요", false);
    return;
  }
  
  const amountModeEl = qs("quickTradeMode");
  const valueEl = qs("quickTradeValue");
  if (!amountModeEl || !valueEl) return;
  
  const amountMode = amountModeEl.value; // "quote" or "percent"
  const amountValue = parseFloat(valueEl.value);
  
  if (isNaN(amountValue) || amountValue <= 0) {
    showQuickTradeMsg("금액/비율을 입력하세요", false);
    return;
  }
  
  // Get order mode and guard policy
  const orderModeEl = qs("quickOrderMode");
  const guardPolicyEl = qs("quickGuardPolicy");
  const orderMode = orderModeEl ? orderModeEl.value : "immediate";
  const guardPolicy = guardPolicyEl ? guardPolicyEl.value : "global";
  
  // Build conditional config if needed
  let conditional = null;
  if (orderMode === "conditional") {
    const triggerEl = qs("quickCondTrigger");
    const lookbackEl = qs("quickCondLookback");
    const thresholdModeEl = qs("quickCondThresholdMode");
    const thresholdValEl = qs("quickCondThresholdVal");
    const expiryEl = qs("quickCondExpiry");
    
    conditional = {
      lookback_min: parseInt(lookbackEl?.value || "15", 10),
      trigger: triggerEl?.value || "near_low",
      threshold_mode: thresholdModeEl?.value || "pct",
      threshold_value: parseFloat(thresholdValEl?.value || "0.2"),
      expiry_sec: parseInt(expiryEl?.value || "30", 10) * 60
    };
  }
  
  // Build execution config
  const execution = {
    order_type: guardPolicy === "entry_limit_only" ? "limit" : "market",
    limit_price_mode: "best_bid"
  };
  
  const actionText = orderMode === "conditional" 
    ? "조건부 주문 등록" 
    : (side === "buy" ? "매수" : "매도");
  showQuickTradeMsg(`${actionText} 중...`, true);
  
  const payload = {
    exchange: "bybit",
    market_input: marketInput,
    side: side,
    amount_mode: amountMode,
    amount_value: amountValue,
    mode: orderMode,
    guard_policy: guardPolicy,
    conditional: conditional,
    execution: execution
  };
  console.log("[QuickTrade v2] Request:", payload);
  
  try {
    const res = await fetch("/api/trade/quick", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    
    const resp = await res.json();
    console.log("[QuickTrade v2] Response:", resp);
    
    if (resp.ok) {
      if (orderMode === "conditional") {
        showQuickTradeMsg(`✓ 조건부 주문 등록 (${resp.resolved_market})`, true);
        updateQuickPendingOrders();
      } else {
        showQuickTradeMsg(`✓ ${side === "buy" ? "매수" : "매도"} 완료 (${resp.resolved_market})`, true);
      }
      valueEl.value = "";
    } else {
      showQuickTradeMsg(`✗ ${resp.error || "실패"}`, false);
    }
  } catch (e) {
    showQuickTradeMsg(`✗ ${e.message}`, false);
    console.error("Quick Trade Exception:", e);
  }
}

function showQuickTradeMsg(msg, ok) {
  const el = qs("quickTradeMsg");
  if (!el) return;
  el.textContent = msg;
  el.style.color = ok ? "#4caf50" : "#ff5252";
  setTimeout(() => { if (el.textContent === msg) el.textContent = ""; }, 4000);
}

// Update threshold label based on mode selection (% or USDT)
window.updateThresholdLabel = function() {
  const modeEl = qs("quickCondThresholdMode");
  const labelEl = qs("quickCondThresholdLabel");
  const valueEl = qs("quickCondThresholdVal");
  if (!modeEl || !labelEl) return;

  const mode = modeEl.value;
  if (mode === "pct") {
    labelEl.textContent = "% proximity";
    if (valueEl) {
      valueEl.step = "0.1";
      valueEl.placeholder = "0.2";
    }
  } else {
    labelEl.textContent = "USDT proximity";
    if (valueEl) {
      valueEl.step = "0.01";
      valueEl.placeholder = "0.5";
    }
  }
  
  // 예상 진입가 업데이트
  updateQuickCondEstimate();
};

// 예상 진입가 조회 및 표시
let estimateDebounceTimer = null;
async function updateQuickCondEstimate() {
  const estimateEl = qs("quickCondEstimate");
  const textEl = qs("quickCondEstimateText");
  if (!estimateEl || !textEl) return;
  
  // 조건부 모드인지 확인
  const orderModeEl = qs("quickOrderMode");
  if (!orderModeEl || orderModeEl.value !== "conditional") {
    estimateEl.style.display = "none";
    return;
  }
  
  // 마켓 확인
  const marketInputEl = qs("quickTradeMarketInput");
  const marketInput = marketInputEl ? marketInputEl.value.trim().toUpperCase() : "";
  if (!marketInput) {
    estimateEl.style.display = "none";
    return;
  }
  
  // Market normalization (Bybit USDT)
  let market = marketInput;
  if (!market.endsWith("USDT")) {
    market = `${market}USDT`;
  }
  
  // 조건 파라미터
  const lookbackEl = qs("quickCondLookback");
  const triggerEl = qs("quickCondTrigger");
  const thresholdModeEl = qs("quickCondThresholdMode");
  const thresholdValEl = qs("quickCondThresholdVal");
  
  const lookback = parseInt(lookbackEl?.value || "15", 10);
  const trigger = triggerEl?.value || "near_low";
  const thresholdMode = thresholdModeEl?.value || "pct";
  const thresholdValue = parseFloat(thresholdValEl?.value || "0.2");
  
  estimateEl.style.display = "block";
  textEl.textContent = "계산 중...";
  textEl.style.color = "#888";
  
  // 디바운스
  clearTimeout(estimateDebounceTimer);
  estimateDebounceTimer = setTimeout(async () => {
    try {
      const url = `/api/trade/quick/estimate?market=${encodeURIComponent(market)}&lookback_min=${lookback}&threshold_mode=${thresholdMode}&threshold_value=${thresholdValue}&trigger=${trigger}`;
      const res = await fetch(url);
      const data = await res.json();
      
      if (data.ok) {
        const refLabel = trigger === "near_low" ? "최저" : "최고";
        const diffSign = data.diff_from_current >= 0 ? "+" : "";
        const diffColor = trigger === "near_low" 
          ? (data.diff_from_current < 0 ? "#4caf50" : "#ff9800")  // 매수: 현재보다 낮으면 좋음
          : (data.diff_from_current > 0 ? "#4caf50" : "#ff9800"); // 매도: 현재보다 높으면 좋음
        
        textEl.innerHTML = `
          <span style="color:#aaa;">${lookback}m ${refLabel}:</span> <b>$${fmtPrice(data.reference_price)}</b>
          → <span style="color:#fff;">Entry $${fmtPrice(data.entry_price)}</span>
          <span style="color:${diffColor};">(vs current ${diffSign}${data.diff_pct.toFixed(2)}%)</span>
        `;
      } else {
        textEl.textContent = data.error || "데이터 부족";
        textEl.style.color = "#666";
      }
    } catch (e) {
      textEl.textContent = "조회 실패";
      textEl.style.color = "#666";
    }
  }, 300);
}

async function fetchMarketSuggestions(query) {
  if (!query || query.length < 1) return;
  try {
    const res = await fetch(`/api/trade/markets/suggest?query=${encodeURIComponent(query)}&limit=10`);
    const data = await res.json();
    const datalist = qs("marketSuggestList");
    if (datalist && data.ok && data.markets) {
      datalist.innerHTML = data.markets.map(m => 
        `<option value="${m.market}">${m.base}/${m.quote} ${m.active ? "●" : "○"}</option>`
      ).join("");
    }
  } catch (e) {
    console.error("Market suggest error:", e);
  }
}

function initQuickTrade() {
  // Buy / Sell buttons
  const btnBuy = qs("btnQuickBuy");
  const btnSell = qs("btnQuickSell");
  if (btnBuy) btnBuy.addEventListener("click", () => executeQuickTrade("buy"));
  if (btnSell) btnSell.addEventListener("click", () => executeQuickTrade("sell"));
  
  // Order mode toggle - show/hide conditional config
  const orderModeEl = qs("quickOrderMode");
  const condConfigEl = qs("quickCondConfig");
  if (orderModeEl && condConfigEl) {
    orderModeEl.addEventListener("change", () => {
      condConfigEl.style.display = orderModeEl.value === "conditional" ? "block" : "none";
      updateQuickCondEstimate();
    });
  }
  
  // Market input autocomplete
  const marketInputEl = qs("quickTradeMarketInput");
  if (marketInputEl) {
    let debounceTimer = null;
    marketInputEl.addEventListener("input", () => {
      clearTimeout(debounceTimer);
      debounceTimer = setTimeout(() => {
        fetchMarketSuggestions(marketInputEl.value.trim());
        updateQuickTradePanel(); // Update price display
        updateQuickCondEstimate();
      }, 300);
    });
  }
  
  // Conditional config change listeners for estimate update
  ["quickCondLookback", "quickCondTrigger", "quickCondThresholdVal"].forEach(id => {
    const el = qs(id);
    if (el) {
      el.addEventListener("change", updateQuickCondEstimate);
      el.addEventListener("input", updateQuickCondEstimate);
    }
  });
  
  // Quick amount buttons
  document.querySelectorAll(".qt-quick").forEach(btn => {
    btn.addEventListener("click", () => {
      const modeEl = qs("quickTradeMode");
      const valueEl = qs("quickTradeValue");
      if (!modeEl || !valueEl) return;
      
      if (btn.dataset.val) {
        modeEl.value = "quote";
        valueEl.value = btn.dataset.val;
      } else if (btn.dataset.pct) {
        modeEl.value = "percent";
        valueEl.value = btn.dataset.pct;
      }
    });
  });
  
  // Force mode warning
  const guardPolicyEl = qs("quickGuardPolicy");
  if (guardPolicyEl) {
    guardPolicyEl.addEventListener("change", () => {
      if (guardPolicyEl.value === "force") {
        if (!confirm("⚠️ Force 모드는 모든 가드를 무시합니다.\n정말 사용하시겠습니까?")) {
          guardPolicyEl.value = "global";
        }
      }
    });
  }
}

/* =========================
 * RENDER: AI / Risk (selected market)
 * ========================= */


/* =========================
 * AI Gate Strictness (global)
 * ========================= */
async function loadAiGate() {
  try {
    if (!API.aiGateGet) return;
    const data = await fetchJson(API.aiGateGet);
    if (!data || data.ok === false) return;
    const gate = data.gate || {};
    const stats = data.stats || {};
    const s = Number(gate.strictness);
    if (qs("aiGateStrictness")) qs("aiGateStrictness").value = String(Number.isFinite(s) ? s : 60);
    if (qs("aiGateStrictnessVal")) qs("aiGateStrictnessVal").textContent = Number.isFinite(s) ? String(s) : "—";
    const eligible = stats.eligible;
    const total = stats.scoreboard_total;
    if (qs("aiGateEligible")) {
      if (Number.isFinite(eligible) && Number.isFinite(total)) qs("aiGateEligible").textContent = `${eligible}/${total}`;
      else qs("aiGateEligible").textContent = "—";
    }
  } catch (e) {
    console.error(e);
  }
}

async function setAiGateStrictness(v) {
  try {
    if (!API.aiGateSet) return;
    const s = Math.max(0, Math.min(100, Number(v)));
    const data = await fetchJson(API.aiGateSet, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ strictness: s }),
    });
    if (!data || data.ok === false) throw new Error((data && (data.error || data.detail)) || "set failed");
    await loadAiGate();
  } catch (e) {
    console.error(e);
    setAdminMsg(`AI Gate ERROR: ${e.message}`, false);
  }
}

function initAiGate() {
  const slider = qs("aiGateStrictness");
  if (!slider) return;

  // initial
  loadAiGate();

  let t = null;
  slider.addEventListener("input", () => {
    const v = Number(slider.value);
    if (qs("aiGateStrictnessVal")) qs("aiGateStrictnessVal").textContent = String(v);
    if (t) clearTimeout(t);
    t = setTimeout(() => setAiGateStrictness(v), 350);
  });
}

function renderForesight() {
  // NOTE: legacy panel replaced by Guard Matrix.
  // Keep this function as a safe no-op to avoid breaking other code paths.
  return;
  const mkt = state.selectedMarket;

  if (qs("foresightMarket")) qs("foresightMarket").textContent = mkt || "—";

  if (!mkt) {
    if (qs("foresightStatus")) qs("foresightStatus").textContent = "—";
    if (qs("foresightBias")) qs("foresightBias").textContent = "—";
    if (qs("foresightConfidence")) qs("foresightConfidence").textContent = "—";
    if (qs("foresightRisk")) qs("foresightRisk").textContent = "—";
    if (qs("foresightUnlock")) qs("foresightUnlock").textContent = "—";
    const ul = qs("foresightReasons");
    if (ul) clear(ul);
    renderRiskBar();
    return;
  }

  const ctx = getCtx(mkt);
  const r = getReadiness(ctx);
  const s = getStrategy(ctx);
  const risk = getRisk(ctx);
  const lastSignal = ctx.last_signal || "—";

  const status = Boolean(r.ready) ? "READY" : "WARMUP";

  // Bias/Confidence는 risk.reason에 있을 수도, strategy에 있을 수도 있다.
  const bias =
    (risk.reason && (risk.reason.bias || risk.reason.Bias)) || s.bias || s.selected || "—";
  const conf =
    (risk.reason && (risk.reason.confidence ?? risk.reason.Confidence)) ??
    s.confidence ??
    "—";

  if (qs("foresightStatus")) qs("foresightStatus").textContent = status;
  if (qs("foresightBias")) qs("foresightBias").textContent = upperOrEmpty(bias) || "—";
  if (qs("foresightConfidence")) {
    qs("foresightConfidence").textContent =
      conf === "—" ? "—" : fmtNum(conf, 2);
  }
  if (qs("foresightRisk")) qs("foresightRisk").textContent = risk.band || "—";
  if (qs("foresightUnlock")) {
    qs("foresightUnlock").textContent = risk.unlock ? "TRUE" : "FALSE";
  }

  // Decision Reason: risk.reason 우선, 없으면 strategy.reason
  const reason = (risk && risk.reason) || (s && s.reason) || {};
  const ul = qs("foresightReasons");
  if (ul) {
    clear(ul);

    const entries = Object.entries(reason || {});

    if (!entries.length) {
      ul.innerHTML = `<li class="li-title">(no reason)</li>`;
    } else {
      // rule/cause는 먼저 보여주고, 나머지 정렬
      const priority = ["rule", "cause", "bias", "confidence", "ema_gap"];
      entries
        .sort((a, b) => {
          const ia = priority.indexOf(a[0]);
          const ib = priority.indexOf(b[0]);
          if (ia === -1 && ib === -1) return a[0].localeCompare(b[0]);
          if (ia === -1) return 1;
          if (ib === -1) return -1;
          return ia - ib;
        })
        .forEach(([k, v]) => {
          const li = document.createElement("li");
          const vv =
            typeof v === "number" ? fmtNum(v, 4) : typeof v === "object" ? JSON.stringify(v) : String(v);
          li.innerHTML = `<span class="k">${k}</span><span class="v">${vv}</span><span class="d flat"></span>`;
          ul.appendChild(li);
        });
    }
  }

  renderRiskBar();
}

function renderRiskBar() {
  const bar = qs("risk-band-bar");
  if (!bar) return;

  const mkt = state.selectedMarket;
  const ctx = mkt ? getCtx(mkt) : {};
  const risk = getRisk(ctx);
  
  const band = risk.band || null;
  const unlock = Boolean(risk.unlock);

  bar.querySelectorAll(".risk-seg").forEach((s) => s.classList.remove("active"));
  if (band) {
    const seg = bar.querySelector(`.risk-seg.${band}`);
    if (seg) seg.classList.add("active");
  }
  bar.classList.toggle("unlock", unlock);
}


/* =========================
 * RENDER: Guard Matrix + Controls (RIGHT PANEL)
 * ========================= */
async function refreshGuards() {
  try {
    const g = await fetchJson(API.systemGuardsGet);
    state.guards = g;
  } catch (e) {
    state.guards = null;
  }
}


function renderGuardsPanel() {
  const boxMsg = qs("guardMsg");
  const boxControls = qs("guardControls");
  const boxMarket = qs("marketGuardControls");
  const boxGrid = qs("guardGrid");

  if (!boxMsg || !boxControls || !boxGrid) return;

  // -------------------------------------------------
  // IMPORTANT UX FIX
  // - Guard controls were being re-rendered every poll (3s),
  //   which interrupts typing (focus loss / value reset).
  // - While the user is editing any guard input/select, freeze
  //   the control re-render, but keep the Guard Matrix updating.
  // -------------------------------------------------
  const EDITING_MSG = "입력중… (자동 갱신 일시정지)";
  const ae = document.activeElement;
  const isEditing = !!(
    ae &&
    ((boxControls && boxControls.contains(ae)) || (boxMarket && boxMarket.contains(ae)))
  );

  if (isEditing) {
    // Keep existing text if it was an actual status.
    if (!boxMsg.textContent || boxMsg.textContent.trim() === "—") {
      boxMsg.textContent = EDITING_MSG;
    }
  } else {
    // Clear the temporary editing banner when user exits inputs.
    if ((boxMsg.textContent || "").trim() === EDITING_MSG) {
      boxMsg.textContent = "—";
    }
    if (boxMarket) boxMarket.innerHTML = "";
  }

  // Global guard config (dashboard overrides may already be applied server-side)
  const g = (state.guards && state.guards.guards) ? state.guards.guards : {};

  const valNum = (v, fallback = "") => {
    const n = Number(v);
    return Number.isFinite(n) ? String(n) : fallback;
  };

  // ========== Guard 툴팁 정의 ==========
  const GUARD_TOOLTIPS = {
    // Global Defaults - Main Guards
    "g_exit_profit_guard": "Exit Profit Guard: 수익 실현 조건 충족 시 자동 매도",
    "g_entry_ob_guard": "Orderbook Guard: 호가창 스프레드/깊이 검사로 슬리피지 방지",
    "g_entry_ceiling_guard": "Ceiling Guard: 최근 고점 대비 과열 구간 진입 차단",
    "g_entry_qty_guard": "Quantity Guard: 과도한 수량 주문 제한",
    "g_drawdown": "Drawdown Guard: 최대 하락폭 도달 시 자동 손절",
    "g_tp_limit": "Take Profit Limit: 지정가 익절 주문 활성화",
    "g_entry_limit_buy": "Entry Limit Buy: 시장가 대신 지정가 매수",
    "g_wallet_mode": "Wallet Mode: 지갑 잔고 기반 거래 (API 잔고 무시)",
    
    // Perf & Grad
    "g_perf_rebalance": "Performance Rebalance: 성과 기반 예산 재분배",
    "g_perf_apply_auto": "Auto Apply: 리밸런싱 결과 자동 적용",
    "g_graduation": "Graduation: 성과 우수 마켓 자동 승급/강등",
    "g_grad_apply_auto": "Auto Apply: 승급/강등 결과 자동 적용",
    
    // Risk & Smart
    "g_correlation_guard": "Correlation Guard: 고상관 코인 중복 진입 방지",
    "g_time_strategy": "Time Strategy: 시간대별 전략 가중치 조절",
    "g_risk_budget": "Risk Budget: 일일 손실 한도 관리",
    "g_ai_sizing": "AI Position Sizing: AI 기반 포지션 크기 조절",
    "g_dynamic_sl": "Dynamic Stoploss: 변동성 기반 동적 손절가",
    
    // Smart Allocation
    "g_smart_alloc_enabled": "Smart Allocation: 스코어 기반 예산 분배 활성화",
    "g_smart_alloc_corr_enabled": "Correlation Penalty: 고상관 코인 예산 감소",
    "g_smart_alloc_sector_enabled": "Sector Limit: 동일 섹터 집중 방지",
    "g_smart_alloc_w_profit": "Profit Weight: 수익률 스코어 가중치 (0~1)",
    "g_smart_alloc_w_ai": "AI Weight: AI 신호 스코어 가중치 (0~1)",
    "g_smart_alloc_w_risk": "Risk Weight: 리스크 스코어 가중치 (0~1)",
    "g_smart_alloc_w_momentum": "Momentum Weight: 모멘텀 스코어 가중치 (0~1)",
    "g_smart_alloc_w_kelly": "Kelly Weight: 켈리 비율 스코어 가중치 (0~1)",
    "g_smart_alloc_w_liquidity": "Liquidity Weight: 유동성 스코어 가중치 (0~1)",
    "g_smart_alloc_min_mult": "Min Multiplier: 최소 예산 배율 (기본 0.5x)",
    "g_smart_alloc_max_mult": "Max Multiplier: 최대 예산 배율 (기본 2.0x)",
    
    // Numeric fields
    "g_daily_loss_limit_pct": "Daily Loss Limit %: 일일 최대 손실률 한도",
    "g_max_same_sector": "Max Same Sector: 동일 섹터 최대 보유 코인 수",
    "g_high_corr_threshold": "Correlation Threshold: 고상관 판단 기준 (0~1)",
    "g_min_order_usdt": "Min Order USDT: minimum order amount",
    "g_entry_max_qty": "Max Quantity: 1회 최대 매수 수량",
    "g_entry_qty_cooldown_sec": "Qty Cooldown: 수량 가드 쿨다운 (초)",
    "g_entry_ob_max_spread_bps": "OB Max Spread: 호가 스프레드 한도 (bps)",
    "g_entry_ob_depth_factor": "OB Depth Factor: 호가 깊이 배율",
    "g_entry_ob_depth_bps": "OB Depth Range: 호가 깊이 검사 범위 (bps)",
    "g_entry_ob_stale_sec": "OB Stale: 호가 데이터 만료 시간 (초)",
    "g_entry_ceiling_extra_bps": "Ceiling Extra BPS: 천장 버퍼 (bps)",
    "g_entry_ceiling_cooldown_sec": "Ceiling Cooldown: 천장 감지 후 대기 (초)",
    "g_entry_ceiling_max_age_sec": "Ceiling Max Age: 고점 유효 기간 (초)",
    "g_entry_ceiling_decay_mode": "Ceiling Decay: 천장 감쇠 모드 (OFF/LIN/EXP)",
    "g_entry_ceiling_decay_half_life_sec": "Decay Half-Life: 감쇠 반감기 (초)",
    "g_exit_min_net_profit_pct": "Exit Profit %: 최소 순수익률 (%)",
    "g_exit_min_net_profit_usdt": "Exit Profit USDT: minimum net profit amount",
    "g_exit_slippage_guard_bps": "Slippage Guard: 슬리피지 보호 한도 (bps)",
    "g_exit_fee_rate": "Fee Rate: 거래 수수료율",
    "g_tp_limit_timeout_sec": "TP Timeout: 익절 주문 타임아웃 (초)",
    "g_tp_limit_max_retries": "TP Retries: 익절 주문 최대 재시도 횟수",
    "g_entry_limit_timeout_sec": "Limit Timeout: 지정가 주문 타임아웃 (초)",
    "g_entry_limit_cooldown_sec": "Limit Cooldown: 지정가 실패 후 대기 (초)",
    "g_entry_limit_price_mode": "Limit Price Mode: 지정가 기준 (Bid/Ask)",
    "g_entry_global_gap_sec": "Global Gap: 전역 진입 간격 (초)",
    "g_max_pending_orders_total": "Max Pending: 최대 대기 주문 수",
    "g_entry_cooldown_sec": "Entry Cooldown: 진입 간 최소 대기 시간 (초)",
    "g_drawdown_pct": "Drawdown %: 손절 트리거 하락률 (%)",
    "g_tp_limit_pct": "TP Limit %: 익절 지정가 수익률 (%)",
    
    // Market-level overrides (mg_ prefix)
    "mg_entry_enabled": "Entry Enabled: 해당 마켓 매수 허용",
    "mg_entry_ob_guard": "OB Guard: 호가창 검사 (마켓별 오버라이드)",
    "mg_entry_ceiling_guard": "Ceiling Guard: 천장 검사 (마켓별 오버라이드)",
    "mg_entry_qty_guard": "Qty Guard: 수량 제한 (마켓별 오버라이드)",
    "mg_exit_profit_guard": "Profit Guard: 수익 실현 (마켓별 오버라이드)",
    "mg_tp_limit": "TP Limit: 지정가 익절 (마켓별 오버라이드)",
    "mg_entry_ob_max_spread_bps": "OB Spread: 호가 스프레드 한도 (bps)",
    "mg_entry_ob_depth_factor": "OB Depth Factor: 호가 깊이 배율",
    "mg_entry_ob_depth_bps": "OB Depth Range: 호가 깊이 범위 (bps)",
    "mg_entry_ob_stale_sec": "OB Stale: 호가 만료 시간 (초)",
    "mg_entry_max_qty": "Max Qty: 1회 최대 매수 수량",
    "mg_entry_qty_cooldown_sec": "Qty Cooldown: 수량 가드 쿨다운 (초)",
    "mg_entry_ceiling_extra_bps": "Ceiling BPS: 천장 버퍼 (bps)",
    "mg_entry_ceiling_cooldown_sec": "Ceiling Cooldown: 천장 후 대기 (초)",
    "mg_entry_ceiling_max_age_sec": "Ceiling Age: 고점 유효 기간 (초)",
    "mg_entry_ceiling_decay_mode": "Decay Mode: 감쇠 모드 (inh=상속)",
    "mg_entry_ceiling_decay_half_life_sec": "Half-Life: 감쇠 반감기 (초)",
    "mg_exit_min_net_profit_pct": "Exit Profit %: 최소 순수익률",
    "mg_exit_min_net_profit_usdt": "Exit USDT: minimum net profit",
    "mg_exit_slippage_guard_bps": "Slippage: 슬리피지 한도 (bps)",
    "mg_tp_limit_timeout_sec": "TP Timeout: 익절 타임아웃 (초)",
    "mg_tp_limit_max_retries": "TP Retries: 익절 재시도 횟수",
  };

  const mkChk = (id, label, checked) => {
    const tip = GUARD_TOOLTIPS[id] || "";
    return `<label class="gc-check" title="${escHtml(tip)}"><input type="checkbox" id="${id}" ${checked ? "checked" : ""}/> <span>${escHtml(label)}</span></label>`;
  };

  const mkNum = (id, label, value, step = "0.01") => {
    const tip = GUARD_TOOLTIPS[id] || "";
    return `<div class="gc-field" title="${escHtml(tip)}"><label for="${id}">${escHtml(label)}</label><input id="${id}" type="number" step="${step}" value="${escHtml(valNum(value, ""))}" /></div>`;
  };

  
  const mkSel = (id, label, value, options) => {
    const v = (value === undefined || value === null) ? "" : String(value);
    const opts = (options || []).map((o) => {
      const ov = String(o.value);
      const sel = (ov === v) ? " selected" : "";
      return `<option value="${escHtml(ov)}"${sel}>${escHtml(o.label)}</option>`;
    }).join("");
    const tip = GUARD_TOOLTIPS[id] || "";
    return `<div class="gc-field" title="${escHtml(tip)}"><label for="${id}">${escHtml(label)}</label><select id="${id}">${opts}</select></div>`;
  };

  const mkSlider = (id, label, value, min, max, step) => {
    const v = Number.isFinite(Number(value)) ? Number(value) : min;
    const tip = GUARD_TOOLTIPS[id] || "";
    return `<div class="gc-field" style="flex:1 1 140px;" title="${escHtml(tip)}">
      <label for="${id}" style="display:flex; justify-content:space-between; font-size:9px;">
        <span>${escHtml(label)}</span><span id="${id}_val" style="color:var(--accent);">${v.toFixed(2)}</span>
      </label>
      <input id="${id}" type="range" min="${min}" max="${max}" step="${step}" value="${v}" style="width:100%;" 
        oninput="document.getElementById('${id}_val').textContent=parseFloat(this.value).toFixed(2)"/>
    </div>`;
  };

// =========================
  // Global (system-level) controls
  // =========================
  // NOTE: Do NOT rebuild control DOM while the user is typing.
  if (!isEditing) {
  boxControls.innerHTML = `
    <div class="market-guard-panel" style="border-top:none; padding-top:0; margin-top:0;">
      <div class="market-guard-head" style="margin-bottom:6px;">
        <div>
          <div class="title" style="font-size:12px;">Global Defaults</div>
          <div class="sub" style="font-size:10px;">ENV baseline · dashboard overrides persist</div>
        </div>
        <div class="market-guard-actions" style="margin-top:0;">
          <button class="btn" id="applyGuards" style="padding:4px 8px; font-size:10px;">Apply</button>
          <button class="btn btn-ghost" id="clearGlobalEntryCooldown" style="padding:4px 8px; font-size:10px;">Clear BUY CD</button>
        </div>
      </div>

      <div style="display:flex; flex-wrap:wrap; gap:6px 12px;">
        ${mkChk("g_exit_profit_guard", "Exit Profit", !!g.exit_profit_guard)}
        ${mkChk("g_entry_ob_guard", "OB Guard", !!g.entry_ob_guard_enabled)}
        ${mkChk("g_entry_ceiling_guard", "Ceiling", !!g.entry_ceiling_guard)}
        ${mkChk("g_entry_qty_guard", "Qty Guard", !!g.entry_qty_guard)}
        ${mkChk("g_drawdown", "Drawdown", !!g.drawdown_guard)}
        ${mkChk("g_tp_limit", "TP Limit", !!g.tp_limit_exit_enabled)}
        ${mkChk("g_entry_limit_buy", "Entry Limit", !!g.entry_limit_buy_enabled)}
        ${mkChk("g_wallet_mode", "Wallet", !!g.wallet_mode)}
      </div>
      
      <div style="margin-top:6px; padding-top:4px; border-top:1px solid var(--border);">
        <span style="font-size:10px; color:var(--muted); font-weight:600;">📊 Perf & Grad</span>
        <span style="display:inline-flex; flex-wrap:wrap; gap:6px 10px; margin-left:8px;">
          ${mkChk("g_perf_rebalance", "Rebalance", !!g.autopilot_perf_rebalance_enabled)}
          ${mkChk("g_perf_apply_auto", "Auto", !!g.autopilot_perf_apply_auto)}
          ${mkChk("g_graduation", "Grad", !!g.autopilot_graduation_enabled)}
          ${mkChk("g_grad_apply_auto", "Auto", !!g.autopilot_grad_apply_auto)}
        </span>
      </div>
      
      <div style="margin-top:6px; padding-top:4px; border-top:1px solid var(--border);">
        <span style="font-size:10px; color:var(--muted); font-weight:600;">🛡️ Risk & Smart</span>
        <span style="display:inline-flex; flex-wrap:wrap; gap:6px 10px; margin-left:8px;">
          ${mkChk("g_correlation_guard", "Corr", !!g.correlation_guard_enabled)}
          ${mkChk("g_time_strategy", "Time", !!g.time_strategy_enabled)}
          ${mkChk("g_risk_budget", "Budget", !!g.risk_budget_enabled)}
          ${mkChk("g_ai_sizing", "AI Size", !!g.ai_position_sizing_enabled)}
          ${mkChk("g_dynamic_sl", "DynSL", !!g.dynamic_stoploss_enabled)}
        </span>
      </div>
      
      <div style="margin-top:6px; padding-top:4px; border-top:1px solid var(--border);">
        <span style="font-size:10px; color:var(--muted); font-weight:600;">📊 Smart Allocation</span>
        <span style="display:inline-flex; flex-wrap:wrap; gap:6px 10px; margin-left:8px;">
          ${mkChk("g_smart_alloc_enabled", "Enabled", g.smart_alloc_enabled !== false)}
          ${mkChk("g_smart_alloc_corr_enabled", "Corr", g.smart_alloc_corr_enabled !== false)}
          ${mkChk("g_smart_alloc_sector_enabled", "Sector", g.smart_alloc_sector_enabled !== false)}
        </span>
        <div class="market-guard-grid" style="margin-top:4px;">
          ${mkSlider("g_smart_alloc_w_profit", "Profit W", g.smart_alloc_w_profit ?? 0.5, 0, 1, 0.05)}
          ${mkSlider("g_smart_alloc_w_ai", "AI W", g.smart_alloc_w_ai ?? 0.3, 0, 1, 0.05)}
          ${mkSlider("g_smart_alloc_w_risk", "Risk W", g.smart_alloc_w_risk ?? 0.2, 0, 1, 0.05)}
          ${mkSlider("g_smart_alloc_w_momentum", "Mom W", g.smart_alloc_w_momentum ?? 0.15, 0, 1, 0.05)}
          ${mkSlider("g_smart_alloc_w_kelly", "Kelly W", g.smart_alloc_w_kelly ?? 0.15, 0, 1, 0.05)}
          ${mkSlider("g_smart_alloc_w_liquidity", "Liq W", g.smart_alloc_w_liquidity ?? 0.15, 0, 1, 0.05)}
          ${mkSlider("g_smart_alloc_min_mult", "Min Mult", g.smart_alloc_min_mult ?? 0.5, 0.1, 3.0, 0.1)}
          ${mkSlider("g_smart_alloc_max_mult", "Max Mult", g.smart_alloc_max_mult ?? 2.0, 0.1, 3.0, 0.1)}
        </div>
        <div style="margin-top:4px;">
          <button id="openSectorMapBtn" class="btn btn-outline" style="font-size:11px; padding:2px 8px;">🏷️ Sector Map</button>
        </div>
      </div>
      <div class="market-guard-grid" style="margin-top:6px;">
        ${mkNum("g_daily_loss_limit_pct", "DailyLoss%", g.daily_loss_limit_pct || 2.0, "0.1")}
        ${mkNum("g_max_same_sector", "MaxSector", g.max_same_sector || 2, "1")}
        ${mkNum("g_high_corr_threshold", "CorrTH", g.high_correlation_threshold || 0.7, "0.01")}
        ${mkNum("g_min_order_usdt", "MinUSDT", g.min_order_usdt, "1000")}
        ${mkNum("g_entry_max_qty", "MaxQty", g.entry_max_qty, "0.0001")}
        ${mkNum("g_entry_qty_cooldown_sec", "QtyCD", g.entry_qty_cooldown_sec, "0.1")}
        ${mkNum("g_entry_ob_max_spread_bps", "OBSpread", g.entry_ob_max_spread_bps, "0.1")}
        ${mkNum("g_entry_ob_depth_factor", "OBDepthX", g.entry_ob_depth_factor, "0.01")}
        ${mkNum("g_entry_ob_depth_bps", "OBRange", g.entry_ob_depth_bps, "1")}
        ${mkNum("g_entry_ob_stale_sec", "OBStale", g.entry_ob_stale_sec, "0.1")}
        ${mkNum("g_entry_ceiling_extra_bps", "CeilBps", g.entry_ceiling_extra_bps, "0.1")}
        ${mkNum("g_entry_ceiling_cooldown_sec", "CeilCD", g.entry_ceiling_cooldown_sec, "0.1")}
        ${mkNum("g_entry_ceiling_max_age_sec", "CeilAge", g.entry_ceiling_max_age_sec, "1")}
        ${mkSel("g_entry_ceiling_decay_mode", "Decay", (g.entry_ceiling_decay_mode || "EXP"), [{value:"NONE",label:"OFF"},{value:"LINEAR",label:"LIN"},{value:"EXP",label:"EXP"}])}
        ${mkNum("g_entry_ceiling_decay_half_life_sec", "HalfLife", g.entry_ceiling_decay_half_life_sec, "1")}
        ${mkNum("g_exit_min_net_profit_pct", "ExitPft%", g.exit_min_net_profit_pct, "0.01")}
        ${mkNum("g_exit_min_net_profit_usdt", "ExitUSDT", g.exit_min_net_profit_usdt, "100")}
        ${mkNum("g_exit_slippage_guard_bps", "SlipBps", g.exit_slippage_guard_bps, "0.1")}
        ${mkNum("g_exit_fee_rate", "FeeRate", g.exit_fee_rate, "0.0001")}
        ${mkNum("g_tp_limit_timeout_sec", "TPTimeout", g.tp_limit_timeout_sec, "0.1")}
        ${mkNum("g_tp_limit_max_retries", "TPRetry", g.tp_limit_max_retries, "1")}
        ${mkNum("g_entry_limit_timeout_sec", "LimitTO", g.entry_limit_timeout_sec ?? 5, "0.1")}
        ${mkNum("g_entry_limit_cooldown_sec", "LimitCD", g.entry_limit_cooldown_sec ?? 60, "0.1")}
        ${mkSel("g_entry_limit_price_mode", "LimitMode", (g.entry_limit_price_mode || "best_bid"), [{value:"best_bid",label:"Bid"},{value:"best_ask",label:"Ask"}])}
        ${mkNum("g_entry_global_gap_sec", "GapSec", g.entry_global_gap_sec, "0.1")}
        ${mkNum("g_max_pending_orders_total", "MaxPend", g.max_pending_orders_total, "1")}
      </div>
    </div>
  `;

  const setNumQS = (qs2, param, id, isInt = false) => {
    const el = qs(id);
    if (!el) return;
    const raw = (el.value || "").toString().trim();
    if (!raw) return;
    const v = isInt ? parseInt(raw, 10) : parseFloat(raw);
    if (Number.isFinite(v)) qs2.set(param, String(v));
  };

  const doApplyGuards = async () => {
    const qs2 = new URLSearchParams();

    // toggles
    qs2.set("exit_profit_guard", qs("g_exit_profit_guard").checked ? "true" : "false");
    qs2.set("entry_ob_guard_enabled", qs("g_entry_ob_guard").checked ? "true" : "false");
    qs2.set("entry_ceiling_guard", qs("g_entry_ceiling_guard").checked ? "true" : "false");
    qs2.set("entry_qty_guard", qs("g_entry_qty_guard").checked ? "true" : "false");
    qs2.set("drawdown_guard", qs("g_drawdown").checked ? "true" : "false");
    qs2.set("tp_limit_exit_enabled", qs("g_tp_limit").checked ? "true" : "false");
    qs2.set("entry_limit_buy_enabled", qs("g_entry_limit_buy").checked ? "true" : "false");
    qs2.set("wallet_mode", qs("g_wallet_mode").checked ? "true" : "false");
    
    // Performance & Graduation
    qs2.set("autopilot_perf_rebalance_enabled", qs("g_perf_rebalance").checked ? "true" : "false");
    qs2.set("autopilot_perf_apply_auto", qs("g_perf_apply_auto").checked ? "true" : "false");
    qs2.set("autopilot_graduation_enabled", qs("g_graduation").checked ? "true" : "false");
    qs2.set("autopilot_grad_apply_auto", qs("g_grad_apply_auto").checked ? "true" : "false");
    
    // Risk & Smart Features
    qs2.set("correlation_guard_enabled", qs("g_correlation_guard").checked ? "true" : "false");
    qs2.set("time_strategy_enabled", qs("g_time_strategy").checked ? "true" : "false");
    qs2.set("risk_budget_enabled", qs("g_risk_budget").checked ? "true" : "false");
    qs2.set("ai_position_sizing_enabled", qs("g_ai_sizing").checked ? "true" : "false");
    qs2.set("dynamic_stoploss_enabled", qs("g_dynamic_sl").checked ? "true" : "false");

    // numeric
    setNumQS(qs2, "min_order_usdt", "g_min_order_usdt");
    setNumQS(qs2, "entry_max_qty", "g_entry_max_qty");
    setNumQS(qs2, "entry_qty_cooldown_sec", "g_entry_qty_cooldown_sec");

    setNumQS(qs2, "entry_ob_max_spread_bps", "g_entry_ob_max_spread_bps");
    setNumQS(qs2, "entry_ob_depth_factor", "g_entry_ob_depth_factor");
    setNumQS(qs2, "entry_ob_depth_bps", "g_entry_ob_depth_bps");
    setNumQS(qs2, "entry_ob_stale_sec", "g_entry_ob_stale_sec");

    setNumQS(qs2, "entry_ceiling_extra_bps", "g_entry_ceiling_extra_bps");
    setNumQS(qs2, "entry_ceiling_cooldown_sec", "g_entry_ceiling_cooldown_sec");

    setNumQS(qs2, "entry_ceiling_max_age_sec", "g_entry_ceiling_max_age_sec", true);
    const dm = String(qs("g_entry_ceiling_decay_mode")?.value ?? "").trim();
    if (dm !== "") qs2.set("entry_ceiling_decay_mode", dm.toUpperCase());
    setNumQS(qs2, "entry_ceiling_decay_half_life_sec", "g_entry_ceiling_decay_half_life_sec", true);

    setNumQS(qs2, "exit_min_net_profit_pct", "g_exit_min_net_profit_pct");
    setNumQS(qs2, "exit_min_net_profit_usdt", "g_exit_min_net_profit_usdt");
    setNumQS(qs2, "exit_slippage_guard_bps", "g_exit_slippage_guard_bps");
    setNumQS(qs2, "exit_fee_rate", "g_exit_fee_rate");

    setNumQS(qs2, "tp_limit_timeout_sec", "g_tp_limit_timeout_sec");
    setNumQS(qs2, "tp_limit_max_retries", "g_tp_limit_max_retries", true);

    setNumQS(qs2, "entry_limit_timeout_sec", "g_entry_limit_timeout_sec");
    setNumQS(qs2, "entry_limit_cooldown_sec", "g_entry_limit_cooldown_sec");
    const elpm = String(qs("g_entry_limit_price_mode")?.value ?? "").trim();
    if (elpm !== "") qs2.set("entry_limit_price_mode", elpm);

    setNumQS(qs2, "entry_global_gap_sec", "g_entry_global_gap_sec");
    setNumQS(qs2, "max_pending_orders_total", "g_max_pending_orders_total", true);
    
    // Risk & Smart numeric
    setNumQS(qs2, "daily_loss_limit_pct", "g_daily_loss_limit_pct");
    setNumQS(qs2, "max_same_sector", "g_max_same_sector", true);
    setNumQS(qs2, "high_correlation_threshold", "g_high_corr_threshold");
    
    // Smart Allocation
    qs2.set("smart_alloc_enabled", qs("g_smart_alloc_enabled")?.checked ? "true" : "false");
    qs2.set("smart_alloc_corr_enabled", qs("g_smart_alloc_corr_enabled")?.checked ? "true" : "false");
    qs2.set("smart_alloc_sector_enabled", qs("g_smart_alloc_sector_enabled")?.checked ? "true" : "false");
    setNumQS(qs2, "smart_alloc_w_profit", "g_smart_alloc_w_profit");
    setNumQS(qs2, "smart_alloc_w_ai", "g_smart_alloc_w_ai");
    setNumQS(qs2, "smart_alloc_w_risk", "g_smart_alloc_w_risk");
    setNumQS(qs2, "smart_alloc_w_momentum", "g_smart_alloc_w_momentum");
    setNumQS(qs2, "smart_alloc_w_kelly", "g_smart_alloc_w_kelly");
    setNumQS(qs2, "smart_alloc_w_liquidity", "g_smart_alloc_w_liquidity");
    setNumQS(qs2, "smart_alloc_min_mult", "g_smart_alloc_min_mult");
    setNumQS(qs2, "smart_alloc_max_mult", "g_smart_alloc_max_mult");

    try {
      const resp = await fetchJson(`${API.systemGuardsSet}?${qs2.toString()}`, { method: "POST" });
      if (resp.ok) {
        boxMsg.textContent = `Saved @ ${new Date().toLocaleTimeString()}`;
        await refreshGuards();
      } else {
        boxMsg.textContent = `Error: ${resp.error || "unknown"}`;
      }
    } catch (e) {
      boxMsg.textContent = `Error: ${e}`;
    }
  };

  qs("applyGuards")?.addEventListener("click", doApplyGuards);

  let autoSaveTimer = null;
  const autoSaveDebounce = () => {
    if (autoSaveTimer) clearTimeout(autoSaveTimer);
    autoSaveTimer = setTimeout(() => doApplyGuards(), 800);
  };

  boxControls.querySelectorAll('input[type="checkbox"]').forEach(el => {
    el.addEventListener("change", autoSaveDebounce);
  });
  boxControls.querySelectorAll('input[type="number"], select').forEach(el => {
    el.addEventListener("change", autoSaveDebounce);
  });
  boxControls.querySelectorAll('input[type="range"]').forEach(el => {
    el.addEventListener("change", autoSaveDebounce);
  });

  qs("clearGlobalEntryCooldown")?.addEventListener("click", async () => {
    try {
      const resp = await fetchJson(`${API.systemGuardsSet}?clear_global_entry_cooldown=true`, { method: "POST" });
      if (resp.ok) {
        boxMsg.textContent = `Cleared global BUY cooldown @ ${new Date().toLocaleTimeString()}`;
      } else {
        boxMsg.textContent = `Error: ${resp.error || "unknown"}`;
      }
    } catch (e) {
      boxMsg.textContent = `Error: ${e}`;
    }
  });

  // Sector Map Modal
  qs("openSectorMapBtn")?.addEventListener("click", async () => {
    await openSectorMapModal();
  });

  // =========================
  // Per-market overrides (selected market)
  // =========================
  if (boxMarket) {
    const m = state.selectedMarket;
    const coord = (state.system && state.system.coordinator) ? state.system.coordinator : {};
    const c = (m && coord[m]) ? coord[m] : null;

    if (!m || !c) {
      boxMarket.innerHTML = `<div class="empty">Select a market to edit per-market guard overrides.</div>`;
    } else {
      const ov = (c.controls && c.controls.guards && typeof c.controls.guards === "object") ? c.controls.guards : {};

      const effBool = (k, d) => (ov[k] !== undefined && ov[k] !== null) ? !!ov[k] : !!d;
      const effNum = (k, d) => (ov[k] !== undefined && ov[k] !== null && Number.isFinite(Number(ov[k]))) ? Number(ov[k]) : Number(d);

      const hasActiveOv = () => {
        try {
          return Object.keys(ov).some(k => k !== "__clear__" && ov[k] !== null && ov[k] !== undefined);
        } catch (_) {
          return false;
        }
      };

      const entryEnabled = effBool("entry_enabled", true);

      boxMarket.innerHTML = `
        <div class="market-guard-panel">
          <div class="market-guard-head" style="margin-bottom:6px;">
            <div>
              <div class="title" style="font-size:12px;">Market Overrides</div>
              <div class="sub" style="font-size:10px;">${escHtml(m)} · ${hasActiveOv() ? "overrides" : "default"}</div>
            </div>
            <div class="market-guard-actions" style="margin-top:0;">
              <button class="btn" id="applyMarketGuards" style="padding:4px 8px; font-size:10px;">Apply</button>
              <button class="btn btn-ghost" id="resetMarketGuards" style="padding:4px 8px; font-size:10px;">Reset</button>
            </div>
          </div>

          <div style="display:flex; flex-wrap:wrap; gap:6px 12px;">
            ${mkChk("mg_entry_enabled", "Entry", entryEnabled)}
            ${mkChk("mg_entry_ob_guard", "OB", effBool("entry_ob_guard_enabled", g.entry_ob_guard_enabled))}
            ${mkChk("mg_entry_ceiling_guard", "Ceil", effBool("entry_ceiling_guard", g.entry_ceiling_guard))}
            ${mkChk("mg_entry_qty_guard", "Qty", effBool("entry_qty_guard", g.entry_qty_guard))}
            ${mkChk("mg_exit_profit_guard", "Profit", effBool("exit_profit_guard", g.exit_profit_guard))}
            ${mkChk("mg_tp_limit", "TP", effBool("tp_limit_exit_enabled", g.tp_limit_exit_enabled))}
          </div>

          <div class="market-guard-grid" style="margin-top:6px;">
            ${mkNum("mg_entry_ob_max_spread_bps", "OBSpread", effNum("entry_ob_max_spread_bps", g.entry_ob_max_spread_bps), "0.1")}
            ${mkNum("mg_entry_ob_depth_factor", "OBDepthX", effNum("entry_ob_depth_factor", g.entry_ob_depth_factor), "0.01")}
            ${mkNum("mg_entry_ob_depth_bps", "OBRange", effNum("entry_ob_depth_bps", g.entry_ob_depth_bps), "1")}
            ${mkNum("mg_entry_ob_stale_sec", "OBStale", effNum("entry_ob_stale_sec", g.entry_ob_stale_sec), "0.1")}
            ${mkNum("mg_entry_max_qty", "MaxQty", effNum("entry_max_qty", g.entry_max_qty), "0.0001")}
            ${mkNum("mg_entry_qty_cooldown_sec", "QtyCD", effNum("entry_qty_cooldown_sec", g.entry_qty_cooldown_sec), "0.1")}
            ${mkNum("mg_entry_ceiling_extra_bps", "CeilBps", effNum("entry_ceiling_extra_bps", g.entry_ceiling_extra_bps), "0.1")}
            ${mkNum("mg_entry_ceiling_cooldown_sec", "CeilCD", effNum("entry_ceiling_cooldown_sec", g.entry_ceiling_cooldown_sec), "0.1")}
            ${mkNum("mg_entry_ceiling_max_age_sec", "CeilAge", effNum("entry_ceiling_max_age_sec", g.entry_ceiling_max_age_sec), "1")}
            ${mkSel("mg_entry_ceiling_decay_mode", "Decay", (ov.entry_ceiling_decay_mode ?? ""), [{value:"",label:"(inh)"},{value:"NONE",label:"OFF"},{value:"LINEAR",label:"LIN"},{value:"EXP",label:"EXP"}])}
            ${mkNum("mg_entry_ceiling_decay_half_life_sec", "HalfLife", effNum("entry_ceiling_decay_half_life_sec", g.entry_ceiling_decay_half_life_sec), "1")}
            ${mkNum("mg_exit_min_net_profit_pct", "ExitPft%", effNum("exit_min_net_profit_pct", g.exit_min_net_profit_pct), "0.01")}
            ${mkNum("mg_exit_min_net_profit_usdt", "ExitUSDT", effNum("exit_min_net_profit_usdt", g.exit_min_net_profit_usdt), "100")}
            ${mkNum("mg_exit_slippage_guard_bps", "SlipBps", effNum("exit_slippage_guard_bps", g.exit_slippage_guard_bps), "0.1")}
            ${mkNum("mg_tp_limit_timeout_sec", "TPTimeout", effNum("tp_limit_timeout_sec", g.tp_limit_timeout_sec), "0.1")}
            ${mkNum("mg_tp_limit_max_retries", "TPRetry", effNum("tp_limit_max_retries", g.tp_limit_max_retries), "1")}
          </div>
        </div>
      `;

      const numVal = (id, isInt = false) => {
        const el = qs(id);
        if (!el) return null;
        const raw = (el.value || "").toString().trim();
        if (!raw) return null;
        const v = isInt ? parseInt(raw, 10) : parseFloat(raw);
        return Number.isFinite(v) ? v : null;
      };

      qs("applyMarketGuards")?.addEventListener("click", async () => {
        const patch = {
          entry_enabled: qs("mg_entry_enabled")?.checked ? true : false,
          entry_ob_guard_enabled: qs("mg_entry_ob_guard")?.checked ? true : false,
          entry_ceiling_guard: qs("mg_entry_ceiling_guard")?.checked ? true : false,
          entry_qty_guard: qs("mg_entry_qty_guard")?.checked ? true : false,
          exit_profit_guard: qs("mg_exit_profit_guard")?.checked ? true : false,
          tp_limit_exit_enabled: qs("mg_tp_limit")?.checked ? true : false,
        };

        // numeric (only set if valid)
        const pairs = [
          ["entry_ob_max_spread_bps", "mg_entry_ob_max_spread_bps", false],
          ["entry_ob_depth_factor", "mg_entry_ob_depth_factor", false],
          ["entry_ob_depth_bps", "mg_entry_ob_depth_bps", false],
          ["entry_ob_stale_sec", "mg_entry_ob_stale_sec", false],

          ["entry_max_qty", "mg_entry_max_qty", false],
          ["entry_qty_cooldown_sec", "mg_entry_qty_cooldown_sec", false],

          ["entry_ceiling_extra_bps", "mg_entry_ceiling_extra_bps", false],
          ["entry_ceiling_cooldown_sec", "mg_entry_ceiling_cooldown_sec", false],

            ["entry_ceiling_max_age_sec", "mg_entry_ceiling_max_age_sec", true],
            ["entry_ceiling_decay_half_life_sec", "mg_entry_ceiling_decay_half_life_sec", true],

          ["exit_min_net_profit_pct", "mg_exit_min_net_profit_pct", false],
          ["exit_min_net_profit_usdt", "mg_exit_min_net_profit_usdt", false],
          ["exit_slippage_guard_bps", "mg_exit_slippage_guard_bps", false],

          ["tp_limit_timeout_sec", "mg_tp_limit_timeout_sec", false],
          ["tp_limit_max_retries", "mg_tp_limit_max_retries", true],
        ];

        for (const [k, id, isInt] of pairs) {
          const v = numVal(id, isInt);
          if (v !== null) patch[k] = v;
        }

        // Enum/string override: decay mode (blank = clear override)
        const dmRaw = String(qs("mg_entry_ceiling_decay_mode")?.value ?? "").trim();
        if (dmRaw !== "") patch["entry_ceiling_decay_mode"] = dmRaw.toUpperCase();
        else if (qs("mg_entry_ceiling_decay_mode")) patch["entry_ceiling_decay_mode"] = null;

        try {
          const resp = await fetchJson(API.engineControls(m), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ guards: patch })
          });
          if (resp.ok) {
            boxMsg.textContent = `Saved market overrides for ${m} @ ${new Date().toLocaleTimeString()}`;
            await refreshGlobalState();
          } else {
            boxMsg.textContent = `Error: ${resp.error || "unknown"}`;
          }
        } catch (e) {
          boxMsg.textContent = `Error: ${e}`;
        }
      });

      qs("resetMarketGuards")?.addEventListener("click", async () => {
        try {
          const resp = await fetchJson(API.engineControls(m), {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ guards: { "__clear__": true } })
          });
          if (resp.ok) {
            boxMsg.textContent = `Reset market overrides for ${m} @ ${new Date().toLocaleTimeString()}`;
            await refreshGlobalState();
          } else {
            boxMsg.textContent = `Error: ${resp.error || "unknown"}`;
          }
        } catch (e) {
          boxMsg.textContent = `Error: ${e}`;
        }
      });
    }
  }

  } // end !isEditing (guard controls DOM freeze)


  // =========================
  // Guard Matrix grid (per-market lamps)
  // =========================
  clear(boxGrid);

  const coord = (state.system && state.system.coordinator) ? state.system.coordinator : {};
  const safety = (state.system && state.system.safety) ? state.system.safety : {};

  const markets = state.managedMarkets || [];
  if (!markets.length) {
    boxGrid.innerHTML = `<div class="empty">No managed markets</div>`;
    return;
  }

  // Track previous lamp states for pulse effects
  if (!state._guardPrev) state._guardPrev = {};
  const lastLampState = state._guardPrev;

  const mkLampBox = (market, row, key, baseState, title, isCause = false) => {
    const fullKey = `${market}:${row}:${key}`;
    const prev = lastLampState[fullKey];

    let drawState = baseState;
    if (prev && prev !== baseState) {
      if (baseState === "pass") drawState = "just-pass";
      if (baseState === "block") drawState = "just-block";
    }
    lastLampState[fullKey] = baseState;

    const wrap = document.createElement("div");
    wrap.className = "lampbox";
    wrap.title = title || key;

    const lamp = document.createElement("span");
    lamp.className = `lamp ${drawState}`;
    if (isCause) lamp.classList.add("cause");

    const lab = document.createElement("span");
    lab.className = "lamp-label";
    lab.textContent = key;

    wrap.appendChild(lamp);
    wrap.appendChild(lab);
    return wrap;
  };

  const mapEntryCause = (entryState, entryReason) => {
    if (String(entryState || "").toUpperCase() !== "BLOCKED") return "";
    const r = String(entryReason || "").toLowerCase();
    if (r.includes("emergency_stop")) return "EMR";
    if (r.includes("recovery")) return "REC";
    if (r.includes("entry_disabled")) return "EN";
    if (r.includes("order_pending")) return "PND";
    if (r.includes("global_cooldown")) return "GCD";
    if (r.includes("max_pending")) return "MAXP";
    if (r.includes("entry_global_gap")) return "GAP";
    if (r.includes("entry_cooldown")) return "LCD";
    if (r.includes("min_order")) return "MIN";
    if (r.includes("already_in_position")) return "POS";
    if (r.includes("ceiling")) return "CEIL";
    if (r.includes("qty")) return "QTY";
    if (r.includes("orderbook")) return "OB";
    if (r.includes("entry_limit") || r.includes("limit_unfilled")) return "LMT";
    return "BUY";
  };

  const mapExitCause = (exitState, exitReason) => {
    if (String(exitState || "").toUpperCase() !== "BLOCKED") return "";
    const r = String(exitReason || "").toLowerCase();
    if (r.includes("order_pending")) return "PND";
    if (r.includes("exit_cooldown")) return "XCD";
    if (r.includes("no_position")) return "NCP";
    if (r.includes("profit_guard")) return "PFT";
    if (r.includes("entry_limit") || r.includes("limit_unfilled")) return "LMT";
    return "SELL";
  };

  const globalCdRemain = Number((safety.global_entry_cooldown && safety.global_entry_cooldown.remaining_sec) || 0);
  const pendingTotal = Number((safety.order_pressure && safety.order_pressure.pending_orders_total) || 0);
  const maxPending = Number((safety.order_pressure && safety.order_pressure.max_pending_orders_total) || 0);

  for (const m of markets) {
    const c = coord[m] || {};
    const marketState = (c.market_state || "WATCH").toUpperCase();
    const inRecovery = marketState === "RECOVERY" || !!c.recovery;

    const entryState = String(c.entry_state || "").toUpperCase();
    const exitState = String(c.exit_state || "").toUpperCase();
    const entryReason = (c.entry_block_reason || "").toString();
    const exitReason = (c.exit_block_reason || "").toString();

    const posQty = Number(c.position_qty || 0);
    const hasPos = posQty > 0;
    const hasOrder = !!c.order_state;

    const entryRemain = coolRemainSec(c.entry_block_until_ts);
    const exitRemain = coolRemainSec(c.exit_block_until_ts);

    const ov = (c.controls && c.controls.guards && typeof c.controls.guards === "object") ? c.controls.guards : {};
    const effBool = (k, d) => (ov[k] !== undefined && ov[k] !== null) ? !!ov[k] : !!d;

    const entryEnabled = effBool("entry_enabled", true);
    const obOn = effBool("entry_ob_guard_enabled", g.entry_ob_guard_enabled);
    const qtyOn = effBool("entry_qty_guard", g.entry_qty_guard);
    const ceilOn = effBool("entry_ceiling_guard", g.entry_ceiling_guard);
    const profitOn = effBool("exit_profit_guard", g.exit_profit_guard);
    const tpOn = effBool("tp_limit_exit_enabled", g.tp_limit_exit_enabled);
    const entryLimitOn = effBool("entry_limit_buy_enabled", g.entry_limit_buy_enabled);

    const buyOutcome = entryState === "ORDER_PLACED" ? "pass" : (entryState ? "block" : "off");
    const sellOutcome = exitState === "ORDER_PLACED" ? "pass" : (exitState ? "block" : "off");

    const entryCause = mapEntryCause(entryState, entryReason);
    const exitCause = mapExitCause(exitState, exitReason);

    const card = document.createElement("div");
    card.className = "guard-card";

    const title = document.createElement("div");
    title.className = "guard-title";

    const ctrl = (c.controls && typeof c.controls === "object") ? c.controls : {};
    const stCtrl = (ctrl.strategy && typeof ctrl.strategy === "object") ? ctrl.strategy : {};
    const aiCtrl = (ctrl.ai && typeof ctrl.ai === "object") ? ctrl.ai : {};

    const flagOn = (v) => (v === true || v === 1 || v === "1" || v === "true");
    const stratEnabled = flagOn(stCtrl.enabled) || flagOn(c.strategy_enabled);
    const aiEnabled = flagOn(aiCtrl.enabled);

    let strat = "OFF";
    if (stratEnabled) {
      strat = (stCtrl.mode || stCtrl.name || c.strategy_name || "STRATEGY").toString();
    } else if (aiEnabled) {
      strat = "AI";
    }
    strat = strat.toUpperCase();
    const stEnabled = strat !== "OFF";

    // Strategy telemetry (strategy_out.meta) for in-card diagnostics
    const stratRaw = upperOrEmpty(pickStrategyName(c)) || upperOrEmpty(strat) || "—";
    const stratMode = String(stratRaw || "").replace(/^AI[:_]/, "");
    const sSnap = getStrategy(c);
    const sReason = (sSnap && typeof sSnap === "object") ? (sSnap.reason || {}) : {};
    const sOut = sReason.strategy_out || sReason.strategyOut || null;
    const sMeta = (sOut && typeof sOut === "object") ? (sOut.meta || sOut.Meta || null) : null;

    const nVal = (v) => {
      const n = Number(v);
      return Number.isFinite(n) ? n : null;
    };

    const metaDiv = document.createElement("div");
    metaDiv.className = "guard-meta";
    const addMeta = (html) => {
      const sp = document.createElement("span");
      sp.innerHTML = html;
      metaDiv.appendChild(sp);
    };

    const sigLamps = [];
    if (stratMode === "PINGPONG") {
      const levels = (sMeta && typeof sMeta.levels === "object") ? sMeta.levels : null;
      const anchor   = nVal(levels?.anchor);
      const buyPrice = nVal(levels?.buy_price);
      const curPrice = (nVal(sMeta?.price) ?? nVal((state.prices || {})[m]));

      const distUsdt = (curPrice !== null && buyPrice !== null) ? (curPrice - buyPrice) : null;
      const distPct = (distUsdt !== null && buyPrice && buyPrice > 0)
        ? ((curPrice / buyPrice - 1) * 100.0)
        : null;

      const bandOk = (curPrice !== null && buyPrice !== null) ? (curPrice <= buyPrice) : null;
      const reBlocked = !!levels?.reentry_blocked;

      addMeta(`A <b>${anchor !== null ? fmtPrice(anchor) : "—"}</b>`);
      addMeta(`Buy <b>${buyPrice !== null ? fmtPrice(buyPrice) : "—"}</b>`);
      addMeta(`Δ <b>${distPct !== null ? (fmtNum(distPct, 2) + "%") : "—"}</b>`);
      addMeta(`(${distUsdt !== null ? fmtPrice(distUsdt) : "—"} USDT)`);

      sigLamps.push(
        mkLampBox(
          m,
          "SIG",
          "BAND",
          (bandOk === null) ? "off" : (bandOk ? "pass" : "block"),
          `PingPong BAND: price <= buy_price\nprice=${curPrice ?? "—"}\nbuy_price=${buyPrice ?? "—"}\ndist=${distUsdt ?? "—"} USDT (${distPct !== null ? fmtNum(distPct, 2) : "—"}%)`,
          false
        )
      );
      sigLamps.push(
        mkLampBox(
          m,
          "SIG",
          "RE",
          reBlocked ? "block" : "pass",
          `Re-entry cooldown\nreentry_blocked=${reBlocked ? "TRUE" : "FALSE"}`,
          false
        )
      );

    } else if (stratMode === "AUTOLOOP") {
      const mm = (sMeta && typeof sMeta === "object") ? sMeta : null;
      const th = (mm && typeof mm.thresholds === "object") ? mm.thresholds : {};
      const filters = (mm && typeof mm.filters === "object") ? mm.filters : {};

      const rsiNow = nVal(mm?.rsi);
      const rsiBuy = nVal(th?.rsi_buy);
      const zNow   = nVal(mm?.z);
      const zBuy   = nVal(th?.z_buy);
      const devNow = nVal(mm?.dev_pct);
      const devBuy = nVal(th?.dev_buy_pct);
      const macdNow  = nVal(mm?.macd_hist);
      const macdPrev = nVal(mm?.macd_hist_prev);
      const macdUp = (macdNow !== null && macdPrev !== null) ? (macdNow > macdPrev) : null;

      const condRsi = (rsiNow !== null && rsiBuy !== null) ? (rsiNow <= rsiBuy) : null;
      const zTh = (zBuy !== null) ? (-Math.abs(zBuy)) : null;
      const condZ = (zNow !== null && zTh !== null) ? (zNow <= zTh) : null;
      const devTh = (devBuy !== null) ? (-Math.abs(devBuy)) : null;
      const condDev = (devNow !== null && devTh !== null) ? (devNow <= devTh) : null;
      const condMacd = (macdUp === null) ? null : macdUp;

      const hasAnyFilterBool =
        (typeof filters?.vol_ok === "boolean") ||
        (typeof filters?.knife_ok === "boolean") ||
        (typeof filters?.momentum_ok === "boolean");

      const condFlt = hasAnyFilterBool
        ? ((filters?.vol_ok === true) && (filters?.knife_ok === true) && (filters?.momentum_ok === true))
        : null;

      addMeta(`RSI <b>${rsiNow !== null ? fmtNum(rsiNow, 1) : "—"}</b>/<b>${rsiBuy !== null ? fmtNum(rsiBuy, 1) : "—"}</b>`);
      addMeta(`Z <b>${zNow !== null ? fmtNum(zNow, 2) : "—"}</b>/<b>${zTh !== null ? fmtNum(zTh, 2) : "—"}</b>`);
      addMeta(`DEV <b>${devNow !== null ? fmtNum(devNow, 3) : "—"}%</b>/<b>${devTh !== null ? fmtNum(devTh, 3) : "—"}%</b>`);
      addMeta(`MACD↑ <b>${macdUp === null ? "—" : (macdUp ? "YES" : "NO")}</b>`);
      if (mm && mm.regime) addMeta(`Regime <b>${escHtml(mm.regime)}</b>`);

      sigLamps.push(
        mkLampBox(m, "SIG", "RSI",
          (condRsi === null) ? "off" : (condRsi ? "pass" : "block"),
          `RSI: rsi <= rsi_buy\nrsi=${rsiNow ?? "—"}\nrsi_buy=${rsiBuy ?? "—"}`,
          false
        )
      );
      sigLamps.push(
        mkLampBox(m, "SIG", "Z",
          (condZ === null) ? "off" : (condZ ? "pass" : "block"),
          `Z: z <= -z_buy\nz=${zNow ?? "—"}\nz_buy=${zBuy ?? "—"}\nthreshold=${zTh ?? "—"}`,
          false
        )
      );
      sigLamps.push(
        mkLampBox(m, "SIG", "DEV",
          (condDev === null) ? "off" : (condDev ? "pass" : "block"),
          `DEV: dev_pct <= -dev_buy_pct\ndev_pct=${devNow ?? "—"}%\ndev_buy_pct=${devBuy ?? "—"}%\nthreshold=${devTh ?? "—"}%`,
          false
        )
      );
      sigLamps.push(
        mkLampBox(m, "SIG", "MACD",
          (condMacd === null) ? "off" : (condMacd ? "pass" : "block"),
          `MACD turning up: hist > prev\nhist=${macdNow ?? "—"}\nprev=${macdPrev ?? "—"}`,
          false
        )
      );
      sigLamps.push(
        mkLampBox(m, "SIG", "FLT",
          (condFlt === null) ? "off" : (condFlt ? "pass" : "block"),
          `Filters (all must be TRUE)\nvol_ok=${filters?.vol_ok ?? "—"}\nknife_ok=${filters?.knife_ok ?? "—"}\nmomentum_ok=${filters?.momentum_ok ?? "—"}`,
          false
        )
      );
    }

    // Signal text (if available)
    if (sOut && typeof sOut === "object") {
      const sig = (sOut.signal || c.last_signal || "").toString();
      const rsn = (sOut.reason || "").toString();
      if (sig || rsn) {
        addMeta(`SIG <b>${escHtml(sig || "—")}</b>${rsn ? ` <small>${escHtml(rsn)}</small>` : ""}`);
      }
    }

    const hasOv = (() => {
      try {
        return Object.keys(ov).some(k => k !== "__clear__" && ov[k] !== null && ov[k] !== undefined);
      } catch (_) {
        return false;
      }
    })();

    // 전략별 색상
    const stratColors = {
      'LADDER': '#f5a623',
      'LIGHTNING': '#4fc3f7',
      'GAZUA': '#66bb6a',
      'PINGPONG': '#9fa8da',
      'AUTOLOOP': '#ef9a9a',
      'AI': '#888',
      'OFF': '#555'
    };
    const stratColor = stratColors[strat] || '#888';
    
    title.innerHTML = `
      <div class="title-left">
        <span class="badge">${escHtml(m)}</span>
        <span class="state ${marketState === "ACTIVE" ? "ok" : (marketState === "RECOVERY" ? "bad" : "")}">${escHtml(marketState)}</span>
        <span class="strat-badge" style="background:${stratColor}22; color:${stratColor}; padding:1px 5px; border-radius:3px; font-size:9px; font-weight:600;">${escHtml(strat)}</span>
        ${hasOv ? `<span class="guard-chip warn">OVR</span>` : ``}
      </div>
      <div class="title-right">
        ${entryState === "BLOCKED" ? `<span class="guard-chip danger">BUY:${escHtml(entryReason)}</span>` : ""}
        ${exitState === "BLOCKED" ? `<span class="guard-chip danger">SELL:${escHtml(exitReason)}</span>` : ""}
      </div>
    `;
    card.appendChild(title);

    // Per-strategy quick diagnostics (values + condition lamps)
    if (metaDiv.childNodes && metaDiv.childNodes.length > 0) {
      card.appendChild(metaDiv);
    }

    const rows = document.createElement("div");
    rows.className = "guard-rows";

    // BUY row
    const buyRow = document.createElement("div");
    buyRow.className = "guard-row";
    buyRow.innerHTML = `<div class="guard-row-label">BUY</div>`;

    const buyLights = document.createElement("div");
    buyLights.className = "guard-lights";

    // Always-evaluable gates
    buyLights.appendChild(mkLampBox(m, "BUY", "BUY", buyOutcome, "Last BUY outcome", entryCause === "BUY"));
    buyLights.appendChild(mkLampBox(m, "BUY", "EN", entryEnabled ? "pass" : "block", "Entry enabled (per-market)", entryCause === "EN"));
    buyLights.appendChild(mkLampBox(m, "BUY", "EMR", !!g.emergency_stop ? "block" : "pass", "Emergency stop blocks entry", entryCause === "EMR"));
    buyLights.appendChild(mkLampBox(m, "BUY", "REC", inRecovery ? "block" : "pass", "Recovery blocks entry", entryCause === "REC"));
    buyLights.appendChild(mkLampBox(m, "BUY", "PND", hasOrder ? "block" : "pass", "Pending order blocks entry", entryCause === "PND"));
    buyLights.appendChild(mkLampBox(m, "BUY", "GCD", globalCdRemain > 0 ? "block" : "pass", `Global cooldown remaining: ${globalCdRemain.toFixed(1)}s`, entryCause === "GCD"));

    const maxpNow = (maxPending > 0) ? (pendingTotal >= maxPending) : false;
    const maxpHit = entryCause === "MAXP";
    buyLights.appendChild(mkLampBox(m, "BUY", "MAXP", (maxPending > 0) ? ((maxpNow || maxpHit) ? "block" : "pass") : "off", `Pending orders total: ${pendingTotal} / ${maxPending || "∞"}`, entryCause === "MAXP"));

    // Global gap is not directly observable in UI; reflect last reason (or last pass)
    const gapState = (entryCause === "GAP") ? "block" : (buyOutcome === "pass" ? "pass" : "off");
    buyLights.appendChild(mkLampBox(m, "BUY", "GAP", gapState, `Entry global gap: ${valNum(g.entry_global_gap_sec, "?")}s`, entryCause === "GAP"));

    buyLights.appendChild(mkLampBox(m, "BUY", "LCD", entryRemain > 0 ? "block" : "pass", `Local cooldown remaining: ${entryRemain.toFixed(1)}s`, entryCause === "LCD"));

    // Context-dependent guards (show OFF if guard disabled)
    const minState = (entryCause === "MIN") ? "block" : (buyOutcome === "pass" ? "pass" : "off");
    buyLights.appendChild(mkLampBox(m, "BUY", "MIN", minState, `Min order USDT: ${valNum(g.min_order_usdt, "?")}`, entryCause === "MIN"));

    buyLights.appendChild(mkLampBox(m, "BUY", "POS", hasPos ? "block" : "pass", "Already in position blocks entry", entryCause === "POS"));

    const ceilState = !ceilOn ? "off" : ((entryCause === "CEIL") ? "block" : (buyOutcome === "pass" ? "pass" : "off"));
    buyLights.appendChild(mkLampBox(m, "BUY", "CEIL", ceilState, "Entry ceiling guard", entryCause === "CEIL"));

    const qtyState = !qtyOn ? "off" : ((entryCause === "QTY") ? "block" : (buyOutcome === "pass" ? "pass" : "off"));
    buyLights.appendChild(mkLampBox(m, "BUY", "QTY", qtyState, "Entry qty guard", entryCause === "QTY"));

    const obState = !obOn ? "off" : ((entryCause === "OB") ? "block" : (buyOutcome === "pass" ? "pass" : "off"));
    buyLights.appendChild(mkLampBox(m, "BUY", "OB", obState, "Orderbook spread/depth guard", entryCause === "OB"));

    const lmtBuyState = !entryLimitOn ? "off" : ((entryCause === "LMT") ? "block" : (buyOutcome === "pass" ? "pass" : "off"));
    buyLights.appendChild(mkLampBox(m, "BUY", "LMT", lmtBuyState, "Entry Limit order mode (unfilled → cooldown)", entryCause === "LMT"));

    buyRow.appendChild(buyLights);
    rows.appendChild(buyRow);

    // STRATEGY signal conditions row (why HOLD?)
    if (sigLamps.length > 0) {
      const sigRow = document.createElement("div");
      sigRow.className = "guard-row";
      sigRow.innerHTML = `<div class="guard-row-label">SIG</div>`;

      const sigLights = document.createElement("div");
      sigLights.className = "guard-lights";
      for (const lb of sigLamps) sigLights.appendChild(lb);

      sigRow.appendChild(sigLights);
      rows.appendChild(sigRow);
    }

    // SELL row
    const sellRow = document.createElement("div");
    sellRow.className = "guard-row";
    sellRow.innerHTML = `<div class="guard-row-label">SELL</div>`;

    const sellLights = document.createElement("div");
    sellLights.className = "guard-lights";

    sellLights.appendChild(mkLampBox(m, "SELL", "SELL", sellOutcome, "Last SELL outcome", exitCause === "SELL"));
    sellLights.appendChild(mkLampBox(m, "SELL", "PND", hasOrder ? "block" : "pass", "Pending order blocks exit", exitCause === "PND"));
    sellLights.appendChild(mkLampBox(m, "SELL", "XCD", exitRemain > 0 ? "block" : "pass", `Exit cooldown remaining: ${exitRemain.toFixed(1)}s`, exitCause === "XCD"));
    sellLights.appendChild(mkLampBox(m, "SELL", "NCP", hasPos ? "pass" : "block", "No coin position blocks exit", exitCause === "NCP"));

    const pftState = !profitOn ? "off" : ((exitCause === "PFT") ? "block" : (sellOutcome === "pass" ? "pass" : "off"));
    sellLights.appendChild(mkLampBox(m, "SELL", "PFT", pftState, "Profit guard", exitCause === "PFT"));

    sellLights.appendChild(mkLampBox(m, "SELL", "TP", tpOn ? "pass" : "off", "TP limit exit enabled", false));

    const lmtSellState = !entryLimitOn ? "off" : ((exitCause === "LMT") ? "block" : "pass");
    sellLights.appendChild(mkLampBox(m, "SELL", "LMT", lmtSellState, "Entry Limit unfilled cooldown active", exitCause === "LMT"));

    sellRow.appendChild(sellLights);
    rows.appendChild(sellRow);

    card.appendChild(rows);
    boxGrid.appendChild(card);
  }
}

/* =========================
 * RENDER: PnL (By Coin / By Strategy)
 * ========================= */

function escHtml(s) {
  return String(s ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function numOr(v, d = 0) {
  const n = Number(v);
  return Number.isFinite(n) ? n : d;
}

function fmtKr(v) {
  const n = numOr(v, 0);
  return new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 }).format(Math.round(n));
}

function fmtKrShort(v) {
  const n = numOr(v, 0);
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "+";
  if (abs >= 1e6) return `${sign}$${(Math.abs(n) / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${sign}$${(Math.abs(n) / 1e3).toFixed(1)}K`;
  return `${sign}$${Math.abs(n).toLocaleString(undefined, {maximumFractionDigits: 2})}`;
}

function fmtPct(v) {
  const n = numOr(v, NaN);
  if (!Number.isFinite(n)) return "-";
  return `${n.toFixed(3)}%`;
}

function pnlClass(pnl) {
  const n = numOr(pnl, 0);
  if (n > 0) return "pnl-pos";
  if (n < 0) return "pnl-neg";
  return "";
}

function pickStrategyName(ctx) {
  if (!ctx) return "UNKNOWN";

  // 1) Manual strategy override (controls.strategy)
  //    - dashboard는 기본적으로 ctx.strategy(selected) 를 표시했는데,
  //      이는 StrategySelector(=AI 내부 선택)의 스냅샷일 수 있어
  //      controls.strategy.mode 와 불일치가 발생한다.
  try {
    const c = ctx.controls || {};
    const st = c.strategy || {};
    const enabled =
      st &&
      (st.enabled === true ||
        st.enabled === 1 ||
        st.enabled === "1" ||
        st.enabled === "true");
    if (enabled) {
      const mode = st.mode || st.name || st.selected || "";
      if (mode) return String(mode);
      return "STRATEGY";
    }
  } catch (e) {
    // ignore
  }

  // 2) Legacy selector snapshot (ctx.strategy_state)
  const s = ctx.strategy || {};
  return (
    s.selected ||
    s.engine ||
    s.bucket ||
    s.name ||
    "UNKNOWN"
  );
}

function pickExitReason(ctx) {
  // Returns a short, human-readable exit reason for display (PINGPONG-focused).
  // Only returns a value if the engine marks an exit trigger as hit/triggered.
  try {
    const so = (ctx && (ctx.strategy_out || ctx.strategyOut)) || null;
    const meta = so && typeof so === "object" ? (so.meta || so.Meta || null) : null;
    const exit = meta && typeof meta.exit === "object" ? meta.exit : null;
    if (!exit) return null;

    const trig = (exit.trigger && typeof exit.trigger === "object") ? exit.trigger
      : (exit.triggers && typeof exit.triggers === "object") ? exit.triggers
      : null;

    const triggered =
      exit.triggered === true ||
      exit.hit === true ||
      exit.fired === true ||
      (trig ? Object.values(trig).some(Boolean) : false);

    if (!triggered) return null;

    const reason =
      (typeof exit.reason === "string" && exit.reason) ||
      (typeof exit.mode === "string" && exit.mode) ||
      null;

    return reason;
  } catch (e) {
    return null;
  }
}




function pickMarketState(sys, market, ctx) {
  const reg = (sys.oma && sys.oma.registry) ? sys.oma.registry : {};
  const item = reg ? reg[market] : null;
  return (item && item.state) || (ctx && ctx.market_state) || "";
}

async function renderPnlPanels() {
  const boxCoin = qs("pnlCoin");
  const boxStrat = qs("pnlStrategy");
  if (!boxCoin || !boxStrat) return;

  const sys = state.system || {};
  const coord = sys.coordinator || {};
  const prices = sys.active_prices || {};
  // PnL panels are bound to currently OPEN markets (ACTIVE + RECOVERY).
  // This ensures PnL appears/disappears together with the running markets.
  const oma = sys.oma || {};
  const activeArr = Array.isArray(oma.active) ? oma.active.map(x => (typeof x === 'string' ? x : x.market)).filter(Boolean) : [];
  const recoveryArr = Array.isArray(oma.recovery) ? oma.recovery.map(x => (typeof x === 'string' ? x : x.market)).filter(Boolean) : [];
  const markets = uniq([...activeArr, ...recoveryArr]).filter(Boolean);

  const rows = [];

  const baselineMap = getPnlBaselineMap();

  // If a market was (re)activated, automatically reset baseline to that moment.
  const activeSinceMap = {};
  try {
    const snapActive = Array.isArray(oma.active) ? oma.active : [];
    const snapRecovery = Array.isArray(oma.recovery) ? oma.recovery : [];
    for (const it of [...snapActive, ...snapRecovery]) {
      if (it && typeof it === 'object') {
        const m = String(it.market || '').toUpperCase();
        if (!m) continue;
        const ts = numOr(it.active_since_ts, numOr(it.pnl_since_ts, 0));
        if (ts > 0) activeSinceMap[m] = ts;
      }
    }
  } catch(e) {}

  // purge baselines for markets that are no longer open
  try {
    const bm = {...baselineMap};
    let changed = false;
    for (const k of Object.keys(bm)) {
      if (!markets.includes(k)) { delete bm[k]; changed = true; }
    }
    if (changed) setPnlBaselineMap(bm);
  } catch(e) {}


  for (const market of markets) {
    const ctx = coord[market] || {};
    let allocated = numOr(ctx.allocated_capital, 0);

    // Fallback: OMA registry budget (when coordinator hasn't materialized yet)
    if (!allocated) {
      const b = getOmaBudgetUsdt(sys, market);
      if (b != null) allocated = numOr(b, 0);
    }

    const cash = numOr(ctx.usable_capital, 0);

    const pos = ctx.position || null;
    const qty = pos ? numOr(pos.qty, 0) : 0;
    const entryPx = pos ? numOr(pos.entry, numOr(pos.entry_price, numOr(pos.avg_price, 0))) : 0;

    const px = numOr(prices[market], 0);
    const posValue = qty > 0 && px > 0 ? qty * px : 0;

    // equity = 포지션 가치 (코인 보유 시) 또는 현금 (미보유 시)
    // usable_capital과 position은 중복되므로 둘 중 하나만 사용
    const equity = posValue > 0 ? posValue : cash;

    const hasAnything =
      allocated > 0 ||
      cash > 0 ||
      qty > 0 ||
      (state.managedMarkets || []).includes(market) || getManualPins().includes(market) ||
      (state.recoveryMarkets || []).includes(market);

    if (!hasAnything) continue;

    let base = baselineMap[market] || null;
    const aSince = numOr(activeSinceMap[market], 0);
    if (aSince > 0) {
      const bts = base && base.ts ? (new Date(base.ts)).getTime() / 1000.0 : 0;
      if (!base || (bts > 0 && bts < aSince - 1)) {
        // reset baseline on (re)activation
        base = { equity: equity, ts: new Date().toISOString() };
        baselineMap[market] = base;
        setPnlBaselineMap(baselineMap);
      }
    }
    const denom = base && numOr(base.equity, 0) > 0 ? numOr(base.equity, 0) : allocated;
    const pnl = (base && numOr(base.equity, 0) > 0) ? (equity - numOr(base.equity, 0)) : (equity - allocated);
    const pnlPct = denom > 0 ? (pnl / denom) * 100.0 : NaN;

    rows.push({
      market,
      market_state: pickMarketState(sys, market, ctx),
      strategy: pickStrategyName(ctx),
      manual_enabled: !!((((ctx || {}).controls || {}).manual || {}).enabled),
      allocated_usdt: allocated,
      equity_usdt: equity,
      pnl_usdt: pnl,
      pnl_pct: pnlPct,
      pnl_denom: denom,
      position: qty > 0,
      entry_price: entryPx > 0 ? entryPx : null,
      trade_count: numOr(ctx.trade_count, 0),
      exit_reason: pickExitReason(ctx),
    });
  }

  if (!rows.length) {
    boxCoin.textContent = "PnL 데이터가 아직 없습니다.";
    boxStrat.textContent = "PnL 데이터가 아직 없습니다.";
    return;
  }

  // Sort: worst first (most negative PnL)
  rows.sort((a, b) => a.pnl_usdt - b.pnl_usdt);

  const totalAlloc = rows.reduce((s, r) => s + (numOr(r.pnl_denom, r.allocated_usdt)), 0);
  const totalEq = rows.reduce((s, r) => s + r.equity_usdt, 0);
  const totalPnl = totalEq - totalAlloc;
  const totalPct = totalAlloc > 0 ? (totalPnl / totalAlloc) * 100.0 : NaN;

  boxCoin.innerHTML = `
    <div class="pnl-subtitle">
      <div>Mark-to-market (wallet + position) vs allocated capital <span class="muted">(PnL: since last reset if baseline set)</span></div>
      <div class="pnl-sub-actions">
        <div>Total: <span class="${pnlClass(totalPnl)}">${fmtKr(totalPnl)} USDT</span> (${fmtPct(totalPct)})
        <button class="btn btn-ghost" id="pnlResetAllBtn" title="Set current equity as baseline for all shown markets">Reset PnL</button>
      </div></div>
    </div>
    <table class="tbl">
      <thead>
        <tr>
          <th>Market</th>
          <th>State</th>
          <th>Strategy</th>
          <th>Allocated</th>
          <th>Equity</th>
          <th>PnL</th>
          <th>PnL%</th>
          <th>Pos</th>
          <th>Entry</th>
          <th>Trades</th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody>
        ${rows
          .map((r) => {
            const cls = pnlClass(r.pnl_usdt);
            const ms = (r.market_state || "").toUpperCase();
            const msCls =
              ms === "ACTIVE"
                ? "pill-ok"
                : ms === "WATCH" || ms === "RECOVERY"
                ? "pill-warn"
                : ms === "DISABLED"
                ? "pill-bad"
                : "";
            return `
              <tr>
                <td class="cell-market"><span class="market-link" data-market="${escHtml(r.market)}" title="${escHtml(r.market)}">${escHtml(r.market)}</span></td>
                <td title="OMA: ${escHtml(ms)}"><span class="pill ${msCls}">${escHtml(ms || "—")}</span></td>
                <td>${escHtml(r.strategy)}${r.exit_reason ? `<div style="font-size:11px; opacity:0.75; line-height:1.2; margin-top:2px;" title="${escHtml(r.exit_reason)}">${escHtml(r.exit_reason)}</div>` : ""}</td>
                <td>${fmtKr(r.allocated_usdt)}</td>
                <td>${fmtKr(r.equity_usdt)}</td>
                <td class="${cls}">${fmtKr(r.pnl_usdt)}</td>
                <td class="${cls}">${fmtPct(r.pnl_pct)}</td>
                <td>${r.position ? "Y" : "N"}</td>
                <td>${r.entry_price ? fmtPrice(r.entry_price) : "—"}</td>
          <td>${(r.trade_count || r.trade_count === 0) ? r.trade_count : "—"}</td>
                <td class="cell-actions">
              <div class="row-actions">
                <button class="btn ${r.manual_enabled ? "btn-warn" : ""}" data-mode="${escHtml(r.market)}" data-target="${r.manual_enabled ? "auto" : "manual"}">${escHtml(r.manual_enabled ? "MANUAL" : "AUTO")}</button>
                <button class="btn btn-ghost" data-resetpnl="${escHtml(r.market)}" title="Set current equity as baseline for this market">Reset</button>
                <button class="btn btn-ghost" data-eject="${escHtml(r.market)}">Disable</button>
              </div>
            </td>
              </tr>
            `;
          })
          .join("")}
      </tbody>
    </table>
  `;

  // Bind quick actions: click market -> fill Admin Market
  boxCoin.querySelectorAll(".market-link").forEach((el) => {
    el.addEventListener("click", () => {
      const m = el.getAttribute("data-market");
      const input = qs("adminMarket");
      if (input && m) input.value = m;
      const panel = qs("oma-admin-panel");
      if (panel) panel.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  });

  // Quick eject (disable)
  boxCoin.querySelectorAll("button[data-eject]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const m = btn.getAttribute("data-eject");
      if (!m) return;
      try {
        await fetchJson(API.managerSet(m, "DISABLED", "UI_PNL_EJECT", null), { method: "POST" });
        await refreshGlobalState();
        setAdminMsg(`Disabled ${m}`, true);
      } catch (e) {
        console.error(e);
        setAdminMsg(`Disable failed: ${e.message}`, false);
      }
    });
  });

  // PnL baseline reset (per market)
  boxCoin.querySelectorAll("button[data-resetpnl]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const m = btn.getAttribute("data-resetpnl");
      if (!m) return;
      // baseline uses current equity from rendered rows -> recompute from latest snapshot
      try {
        // Recompute from current state (no network) using latest system snapshot in memory
        const sys = state.system || {};
        const coord = (state.coordinator || {});
        const prices = state.prices || (sys.active_prices || sys.activePrices || {});
        const ctx = coord[m] || {};
        const cash = numOr(ctx.cash_usdt, numOr(ctx.cash, 0));
        const pos = ctx.position || null;
        const qty = pos ? numOr(pos.qty, 0) : 0;
        const px = numOr(prices[m], 0);
        const equity = cash + (qty > 0 && px > 0 ? qty * px : 0);
        setPnlBaseline(m, equity);
        setAdminMsg(`PnL baseline reset: ${m}`, true);
        renderPnlPanels();
      } catch (e) {
        console.error(e);
        setAdminMsg(`PnL reset failed: ${e.message}`, false);
      }
    });
  });

  // PnL baseline reset (all shown markets)
  const resetAllBtn = boxCoin.querySelector("#pnlResetAllBtn");
  if (resetAllBtn) {
    resetAllBtn.addEventListener("click", () => {
      try {
        const sys = state.system || {};
        const coord = (state.coordinator || {});
        const prices = state.prices || (sys.active_prices || sys.activePrices || {});
        const markets = uniq([...(state.managedMarkets || []), ...getManualPins(), state.selectedMarket].filter(Boolean));
        markets.forEach((m) => {
          const ctx = coord[m] || {};
          const cash = numOr(ctx.cash_usdt, numOr(ctx.cash, 0));
          const pos = ctx.position || null;
          const qty = pos ? numOr(pos.qty, 0) : 0;
          const px = numOr(prices[m], 0);
          const equity = cash + (qty > 0 && px > 0 ? qty * px : 0);
          setPnlBaseline(m, equity);
        });
        setAdminMsg("PnL baseline reset for shown markets", true);
        renderPnlPanels();
      } catch (e) {
        console.error(e);
        setAdminMsg(`PnL reset failed: ${e.message}`, false);
      }
    });
  }

  // Mode toggle (AUTO/MANUAL) - coin view
  boxCoin.querySelectorAll("button[data-mode]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const market = btn.getAttribute("data-mode");
      const target = (btn.getAttribute("data-target") || "").toLowerCase();
      const enabled = (target === "manual");
      try {
        await fetchJson(API.engineControls(market), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ manual: { enabled } }),
        });
        setAdminMsg(`${market}: mode -> ${enabled ? "MANUAL" : "AUTO"}`, true);
        pinManualMarket(market, enabled);
        await refreshGlobalState();
      } catch (e) {
        console.error(e);
        setAdminMsg(`mode toggle failed: ${e.message}`, false);
      }
    });
  });



  // By Strategy aggregation
  const groups = {};
  for (const r of rows) {
    const key = r.strategy || "UNKNOWN";
    if (!groups[key]) {
      groups[key] = { strategy: key, markets: 0, allocated_usdt: 0, equity_usdt: 0, pnl_usdt: 0 };
    }
    groups[key].markets += 1;
    groups[key].allocated_usdt += r.allocated_usdt;
    groups[key].equity_usdt += r.equity_usdt;
    groups[key].pnl_usdt += r.pnl_usdt;
  }

  const srows = Object.values(groups).map((g) => ({
    ...g,
    pnl_pct: g.allocated_usdt > 0 ? (g.pnl_usdt / g.allocated_usdt) * 100.0 : NaN,
  }));

  srows.sort((a, b) => a.pnl_usdt - b.pnl_usdt);

  boxStrat.innerHTML = `
    <div class="pnl-subtitle">
      <div>Aggregated PnL by current selected strategy</div>
      <div>Strategies: ${srows.length}</div>
    </div>
    <table class="tbl">
      <thead>
        <tr>
          <th>Strategy</th>
          <th>Markets</th>
          <th>Allocated</th>
          <th>Equity</th>
          <th>PnL</th>
          <th>PnL%</th>
        </tr>
      </thead>
      <tbody>
        ${srows
          .map((r) => {
            const cls = pnlClass(r.pnl_usdt);
            return `
              <tr>
                <td>${escHtml(r.strategy)}${r.exit_reason ? `<div style="font-size:11px; opacity:0.75; line-height:1.2; margin-top:2px;" title="${escHtml(r.exit_reason)}">${escHtml(r.exit_reason)}</div>` : ""}</td>
                <td>${r.markets}</td>
                <td>${fmtKr(r.allocated_usdt)}</td>
                <td>${fmtKr(r.equity_usdt)}</td>
                <td class="${cls}">${fmtKr(r.pnl_usdt)}</td>
                <td class="${cls}">${fmtPct(r.pnl_pct)}</td>
              </tr>
            `;
          })
          .join("")}
      </tbody>
    </table>
  `;
}


/* =========================
 * RENDER: Engine control dropdown
 * ========================= */
function renderEngineMarketSelect() {
  const sel = qs("marketSelect");
  if (!sel) return;

  const prev = sel.value;
  clear(sel);

  const opt0 = document.createElement("option");
  opt0.value = "";
  opt0.textContent = "Select Market";
  sel.appendChild(opt0);

  state.managedMarkets.forEach((m) => {
    const o = document.createElement("option");
    o.value = m;
    o.textContent = m;
    sel.appendChild(o);
  });

  // restore selection if possible
  if (state.selectedMarket) sel.value = state.selectedMarket;
  else if (prev) sel.value = prev;
}

/* =========================
 * UI selection
 * ========================= */
function selectMarket(market) {
  const m = upperOrEmpty(market);
  if (!m) return;

  state.selectedMarket = m;

  // Admin input sync
  const adminMarket = qs("adminMarket");
  if (adminMarket) adminMarket.value = m;

  // Sync budget from OMA snapshot (if configured)
  const adminBudget = qs("adminBudget");
  if (adminBudget) {
    const b = getOmaBudgetUsdt(state.system || {}, m);
    adminBudget.value = b != null ? String(b) : "";
  }

  // Sync strategy mode (best-effort)
  const adminStrategy = qs("adminStrategy");
  if (adminStrategy) {
    const c = (state.coordinator && state.coordinator[m]) || null;
    const mode =
      (c && c.controls && c.controls.strategy && c.controls.strategy.mode) ||
      "";
    adminStrategy.value = mode ? String(mode).toUpperCase() : "AI";
  }

  // dropdown sync
  const sel = qs("marketSelect");
  if (sel) sel.value = m;

  // re-render minimal
  renderMarketRegistry();
  renderPriceBoard();
  renderForesight();
  updateQuickTradePanel();
  refreshGuards()
  .then(() => renderGuardsPanel())
  .catch(() => renderGuardsPanel());
}

/* =========================
 * Admin actions
 * ========================= */
async function setMarketState(market, targetState, reason, budgetUsdt) {
  const m = upperOrEmpty(market);
  const s = upperOrEmpty(targetState);
  if (!m) {
    setAdminMsg("Market is empty", false);
    return;
  }
  if (!s) {
    setAdminMsg("State is empty", false);
    return;
  }

  try {
    setAdminMsg(`Applying ${m} -> ${s} ...`, true);
    const url = API.managerSet(m, s, reason || "UI", budgetUsdt);

    // FastAPI contract: POST
    const data = await fetchJson(url, { method: "POST" });
    if (!data || data.ok === false) {
      throw new Error((data && (data.error || data.detail)) || "unknown error");
    }

    // Delisting warning
    if (data.delisting_warning) {
      alert(data.delisting_warning);
      setAdminMsg(`⚠️ ${m} -> ${s} (거래지원 종료 예정)`, true);
    } else {
      setAdminMsg(`OK: ${m} -> ${s}`, true);
    }

    // refresh to reflect immediately
    await refreshGlobalState();
    selectMarket(m);
  } catch (e) {
    console.error(e);
    setAdminMsg(`ERROR: ${e.message}`, false);
  }
}


async function setMarketStrategyMode(market, strategyMode) {
  const m = upperOrEmpty(market);
  const modeRaw = (strategyMode || "").trim();
  const mode = modeRaw ? modeRaw.toUpperCase() : "AI";

  if (!m) {
    setAdminMsg("Market is empty", false);
    return;
  }

  // Default: AI only
  const payload = {
    baseline: { enabled: false, level: 10 },
    ai: { enabled: true, level: 10 },
    strategy: { enabled: false, level: 5, mode: "" },
  };

  if (mode && mode !== "AI" && mode !== "NONE") {
    payload.ai.enabled = false;
    payload.strategy.enabled = true;
    payload.strategy.mode = mode;

    // Reasonable per-mode defaults (user can override via /api/engine/controls)
    if (mode === "AUTOLOOP") {
      payload.strategy.params = {
        bootstrap: true,
        bar_sec: 180,
        max_bars: 600,
        rsi_len: 14,
        rsi_buy: 28,
        rsi_sell: 58,
        macd_fast: 12,
        macd_slow: 26,
        macd_signal: 9,
        anchor_len: 50,
        z_len: 20,
        z_buy: 1.5,
        max_vol_pct: 1.8,
        repeat_cooldown_sec: 3.0,
        // Trend pullback tactic (BULL regime)
        pb_enabled: true,
        pb_rsi_min: 38,
        pb_rsi_max: 55,
        pb_dev_min_pct: 0.15,
        pb_dev_max_pct: 0.8,
        pb_slope_bars: 5,
        pb_min_slope_pct: 0.05,
        pb_macd_floor: 0.0,
        pb_z_buy: 0.6,
        pb_require_bounce: true,
        // Telemetry snapshot throttling (trade_ledger)
        telemetry_interval_sec: 60.0,
      };
    }
  }

  try {
    await fetchJson(API.engineControls(m), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    setAdminMsg(`Strategy updated: ${m} -> ${mode}`, true);
  } catch (e) {
    setAdminMsg(`Strategy update failed: ${e.message}`, false);
  }
}

function ensureMarketDatalist() {
  const input = qs("adminMarket");
  if (!input) return;

  let dl = qs("marketDatalist");
  if (!dl) {
    dl = document.createElement("datalist");
    dl.id = "marketDatalist";
    document.body.appendChild(dl);
  }

  input.setAttribute("list", "marketDatalist");

  clear(dl);
  state.allKnownMarkets.forEach((m) => {
    const opt = document.createElement("option");
    opt.value = m;
    dl.appendChild(opt);
  });
}

function bindAdminControls() {
  const btnApply = qs("adminApply");
  const btnAct = qs("adminActivateSelected");
  const btnDeact = qs("adminDeactivateSelected");
  const btnPause = qs("adminPause");
  const btnResume = qs("adminResume");

  const inputMarket = qs("adminMarket");
  const selState = qs("adminState");
  const selStrategy = qs("adminStrategy");
  const inputReason = qs("adminReason");
  const inputBudget = qs("adminBudget");

  if (btnApply) {
    btnApply.addEventListener("click", async () => {
      const m = inputMarket ? inputMarket.value : "";
      const s = selState ? selState.value : "ACTIVE";
      const strat = selStrategy ? selStrategy.value : "AI";
      const r = inputReason ? inputReason.value : "UI";
      let budgetUsdt = undefined;
      if (inputBudget) {
        const raw = (inputBudget.value || "").trim();
        if (raw.length > 0) {
          const n = Number(raw);
          if (!Number.isFinite(n) || n < 0) {
            alert("Budget USDT must be a number >= 0 (0 clears to AUTO)." );
            return;
          }
          budgetUsdt = n;
        }
      }
      await setMarketState(m, s, r, budgetUsdt);
      await setMarketStrategyMode(m, strat);
    });
  }

  // Enter on market input = apply
  if (inputMarket) {
    inputMarket.addEventListener("keydown", async (ev) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        if (btnApply) {
          btnApply.click();
        }
      }
    });
  }

  if (btnAct) {
    btnAct.addEventListener("click", async () => {
      if (!state.selectedMarket) {
        setAdminMsg("No selected market", false);
        return;
      }
      const r = inputReason ? inputReason.value : "UI";
      await setMarketState(state.selectedMarket, "ACTIVE", r || "UI");
      const strat = selStrategy ? selStrategy.value : "AI";
      await setMarketStrategyMode(state.selectedMarket, strat);
    });
  }

  if (btnDeact) {
    btnDeact.addEventListener("click", async () => {
      if (!state.selectedMarket) {
        setAdminMsg("No selected market", false);
        return;
      }
      const r = inputReason ? inputReason.value : "UI";
      // 정책상 "De-activate"는 WATCH로 내리는 것을 기본으로 한다.
      await setMarketState(state.selectedMarket, "WATCH", r || "UI");
    });
  }
  if (btnPause) {
    btnPause.addEventListener("click", async () => {
      const r = inputReason ? inputReason.value : "UI";
      try {
        setAdminMsg("Emergency stop set (pause)", true);
        await fetchJson(API.emergencyStop(r || "UI"), { method: "POST" });
        await refreshGlobalState();
      } catch (e) {
        console.error(e);
        setAdminMsg(`Emergency stop error: ${e.message}`, false);
      }
    });
  }

  if (btnResume) {
    btnResume.addEventListener("click", async () => {
      const r = inputReason ? inputReason.value : "UI";
      try {
        setAdminMsg("Emergency stop cleared (resume)", true);
        await fetchJson(API.emergencyResume(r || "UI"), { method: "POST" });
        await refreshGlobalState();
      } catch (e) {
        console.error(e);
        setAdminMsg(`Emergency resume error: ${e.message}`, false);
      }
    });
  }
}

function bindEngineControls() {
  const btnStart = qs("engineStart");
  const btnStop = qs("engineStop");
  const sel = qs("marketSelect");

  if (btnStart) {
    btnStart.addEventListener("click", async () => {
      const m = upperOrEmpty((sel && sel.value) || state.selectedMarket);
      if (!m) {
        setAdminMsg("Select a market to start engine", false);
        return;
      }
      try {
        setAdminMsg(`Engine start: ${m}`, true);
        await fetchJson(API.engineStart(m), { method: "POST" });
        await refreshGlobalState();
        selectMarket(m);
      } catch (e) {
        console.error(e);
        setAdminMsg(`Engine start error: ${e.message}`, false);
      }
    });
  }

  if (btnStop) {
    btnStop.addEventListener("click", async () => {
      try {
        setAdminMsg("Engine stop", true);
        await fetchJson(API.engineStop, { method: "POST" });
        await refreshGlobalState();
      } catch (e) {
        console.error(e);
        setAdminMsg(`Engine stop error: ${e.message}`, false);
      }
    });
  }
}


/* =========================
 * BIND: PnL tabs
 * ========================= */
function bindPnlTabs() {
  const tabCoin = qs("pnlTabCoin") || qs("tabPnlCoin");
  const tabStrat = qs("pnlTabStrategy") || qs("tabPnlStrategy");
  const boxCoin = qs("pnlCoin");
  const boxStrat = qs("pnlStrategy");
  if (!tabCoin || !tabStrat || !boxCoin || !boxStrat) return;

  const showCoin = () => {
    tabCoin.classList.add("active");
    tabStrat.classList.remove("active");
    boxCoin.classList.remove("hidden");
    boxStrat.classList.add("hidden");
  };

  const showStrat = () => {
    tabStrat.classList.add("active");
    tabCoin.classList.remove("active");
    boxStrat.classList.remove("hidden");
    boxCoin.classList.add("hidden");
  };

  tabCoin.addEventListener("click", showCoin);
  tabStrat.addEventListener("click", showStrat);

  // default
  showCoin();
}


/* =========================
 * MAIN LOOP
 * ========================= */

// -------------------------
// Manual Trade (Emergency)
// -------------------------
function renderManualTradePanel() {
  const sel = qs("manualMarketSelect");
  if (!sel) return;

  const markets = (state.managedMarkets && state.managedMarkets.length)
    ? [...state.managedMarkets]
    : (state.allKnownMarkets ? [...state.allKnownMarkets] : []);

  markets.sort();

  const cur = sel.value || state.selectedMarket || "";
  sel.innerHTML = markets.map((m) => `<option value="${m}">${m}</option>`).join("");

  const next = (markets.includes(cur) ? cur : (state.selectedMarket && markets.includes(state.selectedMarket) ? state.selectedMarket : (markets[0] || "")));
  if (next) sel.value = next;
}

function pickBatchMarkets() {
  const activeOnly = !!qs("batchActiveOnly")?.checked;
  const skipManual = !!qs("batchSkipManual")?.checked;

  let markets = [];
  if (activeOnly) {
    const active = (state.oma && Array.isArray(state.oma.active)) ? state.oma.active : [];
    markets = active.map((x) => x.market).filter(Boolean);
  } else {
    markets = (state.managedMarkets && state.managedMarkets.length) ? [...state.managedMarkets] : [];
  }

  if (skipManual) {
    markets = markets.filter((m) => {
      const c = state.coordinator ? state.coordinator[m] : null;
      const man = (((c || {}).controls || {}).manual || {}).enabled;
      return !man;
    });
  }

  // de-dupe
  return [...new Set(markets)];
}

async function doReconcile(reason = "ui_manual") {
  const res = await fetchJson(`${API.systemReconcile}?reason=${encodeURIComponent(reason)}`, { method: "POST" });
  return res;
}

function setManualMsg(msg, ok) {
  const el = qs("manualTradeMsg");
  if (!el) return;
  el.textContent = msg;
  el.classList.toggle("ok", !!ok);
}

async function submitManualOrder(payload) {
  const res = await fetchJson(API.engineManualOrder, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return res;
}

async function submitManualBatch(payload) {
  const res = await fetchJson(API.engineManualBatch, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  return res;
}

function confirmManual(text) {
  return window.confirm(text);
}

function bindManualTradeControls() {
  const sel = qs("manualMarketSelect");
  const syncBtn = qs("manualSyncNow");

  if (syncBtn) {
    syncBtn.addEventListener("click", async () => {
      try {
        setManualMsg("Syncing positions (reconcile)…", true);
        await doReconcile("ui_manual_sync");
        await refreshGlobalState();
        setManualMsg("Sync complete.", true);
      } catch (e) {
        setManualMsg(`Sync failed: ${e}`, false);
      }
    });
  }

  // External Trades Sync button
  const syncTradesBtn = qs("syncExternalTrades");
  if (syncTradesBtn) {
    syncTradesBtn.addEventListener("click", async () => {
      try {
        setManualMsg("Syncing external trades to ledger…", true);
        const res = await fetchJson("/api/manager/sync-external-trades", { method: "POST" });
        if (res.ok) {
          const synced = res.total_synced ?? res.synced ?? 0;
          const skipped = res.total_skipped ?? res.skipped ?? 0;
          setManualMsg(`Trades synced: ${synced} new, ${skipped} skipped`, true);
        } else {
          setManualMsg(`Sync failed: ${res.error || "unknown"}`, false);
        }
      } catch (e) {
        setManualMsg(`Sync failed: ${e}`, false);
      }
    });
  }

  // ============================================================
  // Daily PnL (매매일지) [CREATED 2026-01-23]
  // ============================================================
  
  // 테이블 로드 함수
  async function loadDailyPnLTable() {
    const tbody = document.getElementById("dailyPnLBody");
    if (!tbody) return;
    
    try {
      // 오늘 데이터
      const todayRes = await fetchJson("/api/system/daily-pnl/today");
      // 과거 저장된 데이터
      const summaryRes = await fetchJson("/api/system/daily-pnl/summary?days=7");
      
      let rows = [];
      
      // 오늘 데이터 추가
      if (todayRes.ok && todayRes.report) {
        const r = todayRes.report;
        rows.push({
          date: r.date,
          pnl: r.total_pnl_usdt || 0,
          trades: r.total_trades || 0,
          winRate: r.win_rate || 0,
          isToday: true
        });
      }
      
      // 과거 데이터 추가 (오늘 제외)
      if (summaryRes.ok && summaryRes.daily) {
        const today = new Date().toISOString().slice(0, 10);
        summaryRes.daily.forEach(d => {
          if (d.date !== today) {
            rows.push({
              date: d.date,
              pnl: d.pnl_usdt || 0,
              trades: d.trades || 0,
              winRate: d.win_rate || 0,
              isToday: false
            });
          }
        });
      }
      
      // 날짜순 정렬 (최신 먼저)
      rows.sort((a, b) => b.date.localeCompare(a.date));
      
      if (rows.length === 0) {
        tbody.innerHTML = '<tr><td colspan="4" style="padding:8px; color:#666; text-align:center;">데이터 없음</td></tr>';
        return;
      }
      
      tbody.innerHTML = rows.map(r => {
        const pnlColor = r.pnl >= 0 ? '#4CAF50' : '#f44336';
        const pnlSign = r.pnl >= 0 ? '+' : '';
        const winPct = (r.winRate * 100).toFixed(0);
        const dateLabel = r.isToday ? `<strong>${r.date}</strong> (오늘)` : r.date;
        return `
          <tr style="border-bottom:1px solid #333;">
            <td style="padding:4px 6px;">${dateLabel}</td>
            <td style="padding:4px 6px; text-align:right; color:${pnlColor}; font-weight:bold;">${pnlSign}${r.pnl.toLocaleString()}</td>
            <td style="padding:4px 6px; text-align:center;">${r.trades}</td>
            <td style="padding:4px 6px; text-align:center;">${winPct}%</td>
          </tr>
        `;
      }).join('');
      
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="4" style="padding:8px; color:#f66; text-align:center;">오류: ${e}</td></tr>`;
    }
  }
  
  // 초기 로드
  loadDailyPnLTable();
  
  // 새로고침 버튼
  const refreshDailyPnLBtn = document.getElementById("refreshDailyPnL");
  if (refreshDailyPnLBtn) {
    refreshDailyPnLBtn.addEventListener("click", loadDailyPnLTable);
  }
  
  // 상단 버튼 (팝업용)
  const dailyPnLBtn = qs("openDailyPnL");
  if (dailyPnLBtn) {
    dailyPnLBtn.addEventListener("click", async () => {
      try {
        const res = await fetchJson("/api/system/daily-pnl/today");
        if (res.ok && res.report) {
          const r = res.report;
          const msg = `📊 오늘 매매일지 (${r.date})\n\n` +
            `💰 총 손익: ${r.total_pnl_usdt?.toLocaleString() ?? 0} USDT\n` +
            `📈 거래 수: ${r.total_trades ?? 0}건 (매수 ${r.buy_count ?? 0} / 매도 ${r.sell_count ?? 0})\n` +
            `🎯 승률: ${((r.win_rate ?? 0) * 100).toFixed(1)}%\n` +
            `💸 수수료: ${r.total_fees_usdt?.toLocaleString() ?? 0} USDT\n\n` +
            `마켓별 상세:\n` +
            Object.entries(r.markets || {}).map(([m, d]) => 
              `  ${m}: ${d.pnl_usdt?.toLocaleString() ?? 0} USDT (${d.trades ?? 0}건)`
            ).join('\n');
          alert(msg);
        } else {
          alert(`일지 조회 실패: ${res.error || "데이터 없음"}`);
        }
      } catch (e) {
        alert(`일지 조회 오류: ${e}`);
      }
    });
  }

  // Auto Sync (periodic reconcile)
  // - 휴대폰 앱 등 외부에서 발생한 매수/매도를 주기적으로 반영( Reconcile )
  // - OFF가 기본이며, ON일 때만 동작합니다.
  const autoSyncEl = qs("manualAutoSync");
  const autoSyncIntervalEl = qs("manualAutoSyncInterval");
  const LS_MANUAL_AUTOSYNC = "nunnaya_manual_autosync_v1"; // { enabled: bool, interval: number(sec) }

  let autoSyncTimer = null;
  let autoSyncInFlight = false;

  const stopAutoSync = () => {
    if (IntervalManager) {
      IntervalManager.clear("dashboard_autoSync");
    } else if (autoSyncTimer) {
      try { clearInterval(autoSyncTimer); } catch (_) {}
    }
    autoSyncTimer = null;
  };

  const saveAutoSync = () => {
    try {
      const sec = autoSyncIntervalEl ? Number(autoSyncIntervalEl.value) : 10;
      const interval = (Number.isFinite(sec) && sec >= 5) ? sec : 10;
      lsSetJson(LS_MANUAL_AUTOSYNC, {
        enabled: !!(autoSyncEl && autoSyncEl.checked),
        interval,
      });
    } catch (_) {}
  };

  const runAutoSyncOnce = async () => {
    if (autoSyncInFlight) return;
    autoSyncInFlight = true;
    try {
      await doReconcile("ui_auto_sync");
      // 별도 refreshGlobalState() 호출은 poll이 처리 (3s)
    } catch (e) {
      console.error(e);
      setManualMsg(`Auto Sync failed: ${e?.message || e}`, false);
    } finally {
      autoSyncInFlight = false;
    }
  };

  const startAutoSync = () => {
    stopAutoSync();
    const sec = autoSyncIntervalEl ? Number(autoSyncIntervalEl.value) : 10;
    const interval = (Number.isFinite(sec) && sec >= 5) ? sec : 10;

    // 즉시 1회 반영(체감 개선)
    runAutoSyncOnce();

    if (IntervalManager) {
      autoSyncTimer = IntervalManager.set("dashboard_autoSync", () => {
        if (autoSyncEl && autoSyncEl.checked) runAutoSyncOnce();
      }, interval * 1000);
    } else {
      autoSyncTimer = setInterval(() => {
        if (autoSyncEl && autoSyncEl.checked) runAutoSyncOnce();
      }, interval * 1000);
    }

    setManualMsg(`Auto Sync ON (${interval}s)`, true);
  };

  const applyAutoSyncState = () => {
    if (!autoSyncEl) return;
    if (!autoSyncEl.checked) {
      stopAutoSync();
      setManualMsg("Auto Sync OFF", true);
      saveAutoSync();
      return;
    }
    startAutoSync();
    saveAutoSync();
  };

  // init from localStorage
  try {
    const st = lsGetJson(LS_MANUAL_AUTOSYNC, null);
    if (st && typeof st === "object") {
      if (autoSyncEl && st.enabled !== undefined) autoSyncEl.checked = !!st.enabled;
      if (autoSyncIntervalEl && st.interval !== undefined) autoSyncIntervalEl.value = String(st.interval);
    }
  } catch (_) {}

  if (autoSyncIntervalEl) {
    autoSyncIntervalEl.addEventListener("change", () => {
      if (autoSyncEl && autoSyncEl.checked) applyAutoSyncState();
      else saveAutoSync();
    });
  }

  if (autoSyncEl) {
    autoSyncEl.addEventListener("change", applyAutoSyncState);
    // apply on load
    if (autoSyncEl.checked) applyAutoSyncState();
  }


  function bindSeg(segId, dataAttr, defaultVal, onChange) {
    const box = qs(segId);
    let cur = defaultVal;

    if (!box) {
      return {
        get: () => cur,
        set: (v) => {
          cur = v;
          if (onChange) onChange();
        },
      };
    }

    const btns = Array.from(box.querySelectorAll(`[data-${dataAttr}]`));

    const set = (v) => {
      if (!v) return;
      cur = v;
      btns.forEach((b) =>
        b.classList.toggle("active", b.getAttribute(`data-${dataAttr}`) === v)
      );
      if (onChange) onChange();
    };

    btns.forEach((b) =>
      b.addEventListener("click", () => set(b.getAttribute(`data-${dataAttr}`)))
    );

    set(defaultVal);
    return { get: () => cur, set };
  }

  const manualValueEl = qs("manualValue");
  const batchValueEl = qs("batchValue");
  const reconcileAfterEl = qs("manualReconcileAfter");

  const manualAllBtn = qs("manualSellAllBtn");
  const batchAllBtn = qs("batchSellAllBtn");

  const manualSide = bindSeg("manualSideSeg", "mside", "buy", updateManualHints);
  const manualUnit = bindSeg("manualUnitSeg", "munit", "pct", updateManualHints);

  const batchSide = bindSeg("batchSideSeg", "bside", "buy", updateBatchHints);
  const batchUnit = bindSeg("batchUnitSeg", "bunit", "pct", updateBatchHints);

  function updateManualHints() {
    const side = manualSide.get();
    const unit = manualUnit.get();

    if (manualValueEl) {
      if (unit === "pct") {
        manualValueEl.placeholder =
          side === "buy" ? "% of Alloc (1-100)" : "% of Pos (1-100)";
        manualValueEl.min = "1";
        manualValueEl.max = "100";
        manualValueEl.step = "1";
      } else {
        manualValueEl.placeholder = "USDT amount";
        manualValueEl.min = "1000";
        manualValueEl.removeAttribute("max");
        manualValueEl.step = "1000";
      }
    }

    if (manualAllBtn) {
      manualAllBtn.style.display = side === "sell" ? "" : "none";
    }
  }

  function updateBatchHints() {
    const side = batchSide.get();
    const unit = batchUnit.get();

    if (batchValueEl) {
      if (unit === "pct") {
        batchValueEl.placeholder =
          side === "buy" ? "% of Alloc (1-100)" : "% of Pos (1-100)";
        batchValueEl.min = "1";
        batchValueEl.max = "100";
        batchValueEl.step = "1";
      } else {
        batchValueEl.placeholder = "USDT each";
        batchValueEl.min = "1000";
        batchValueEl.removeAttribute("max");
        batchValueEl.step = "1000";
      }
    }

    if (batchAllBtn) {
      batchAllBtn.style.display = side === "sell" ? "" : "none";
    }
  }

  // set initial UI state
  updateManualHints();
  updateBatchHints();

  const manualSubmitBtn = qs("manualSubmitBtn");
  if (manualSubmitBtn) {
    manualSubmitBtn.addEventListener("click", async () => {
      const market = sel ? sel.value : "";
      if (!market) {
        setManualMsg("Select a market first.", false);
        return;
      }

      const side = manualSide.get();
      const unit = manualUnit.get();
      const value = manualValueEl ? Number(manualValueEl.value) : NaN;

      if (!Number.isFinite(value) || value <= 0) {
        setManualMsg("Enter a positive number.", false);
        return;
      }

      const reconcileAfter = !!(reconcileAfterEl && reconcileAfterEl.checked);

      let mode = "usdt";
      if (side === "buy") {
        mode = unit === "pct" ? "pct_alloc" : "usdt";
      } else {
        mode = unit === "pct" ? "pct_pos" : "usdt";
      }

      const what = side === "buy" ? "BUY" : "SELL";
      const prettyVal =
        unit === "pct" ? `${value}%` : `${fmtKr(value)}`;

      const suffix =
        unit === "pct" ? (side === "buy" ? " (Alloc)" : " (Pos)") : "";

      if (
        !confirmManual(
          `[Manual Trade]\n${what} ${prettyVal}${suffix}\nMarket: ${market}\nProceed?`
        )
      )
        return;

      try {
        const r = await submitManualOrder({
          market,
          side,
          mode,
          value,
          reconcile_after: reconcileAfter,
        });

        if (r && r.ok) {
          setManualMsg(`${what} submitted: ${market}`, true);
          if (reconcileAfter) await doReconcile(`ui_manual_${what.toLowerCase()}`);
          await refreshGlobalState();
        } else {
          setManualMsg(
            `${what} failed: ${r?.error || r?.message || "unknown"}`,
            false
          );
        }
      } catch (e) {
        setManualMsg(`${what} failed: ${e}`, false);
      }
    });
  }

  if (manualAllBtn) {
    manualAllBtn.addEventListener("click", async () => {
      const market = sel ? sel.value : "";
      if (!market) {
        setManualMsg("Select a market first.", false);
        return;
      }

      const reconcileAfter = !!(reconcileAfterEl && reconcileAfterEl.checked);

      if (!confirmManual(`[Manual Trade]\nSELL ALL (100%)\nMarket: ${market}\nProceed?`))
        return;

      try {
        const r = await submitManualOrder({
          market,
          side: "sell",
          mode: "all",
          value: 100,
          reconcile_after: reconcileAfter,
        });

        if (r && r.ok) {
          setManualMsg(`SELL ALL submitted: ${market}`, true);
          if (reconcileAfter) await doReconcile("ui_manual_sell_all");
          await refreshGlobalState();
        } else {
          setManualMsg(
            `SELL ALL failed: ${r?.error || r?.message || "unknown"}`,
            false
          );
        }
      } catch (e) {
        setManualMsg(`SELL ALL failed: ${e}`, false);
      }
    });
  }

  const batchSubmitBtn = qs("batchSubmitBtn");
  if (batchSubmitBtn) {
    batchSubmitBtn.addEventListener("click", async () => {
      const markets = pickBatchMarkets();
      if (markets.length === 0) {
        setManualMsg("No batch markets to trade (check running markets).", false);
        return;
      }

      const side = batchSide.get();
      const unit = batchUnit.get();
      const value = batchValueEl ? Number(batchValueEl.value) : NaN;

      if (!Number.isFinite(value) || value <= 0) {
        setManualMsg("Enter a positive number for batch.", false);
        return;
      }

      const reconcileAfter = !!(reconcileAfterEl && reconcileAfterEl.checked);

      let mode = "pct_alloc";
      if (side === "buy") {
        mode = unit === "pct" ? "pct_alloc" : "usdt_each";
      } else {
        mode = unit === "pct" ? "pct_pos" : "usdt_each";
      }

      const what = side === "buy" ? "BUY" : "SELL";
      const prettyVal =
        unit === "pct" ? `${value}%` : `${fmtKr(value)}`;

      const suffix =
        unit === "pct" ? (side === "buy" ? " (Alloc)" : " (Pos)") : " (each)";

      if (
        !confirmManual(
          `[Batch Trade]\n${what} ${prettyVal}${suffix}\nMarkets: ${markets.length}\nProceed?`
        )
      )
        return;

      try {
        const r = await submitManualBatch({
          markets,
          side,
          mode,
          value,
          reconcile_after: reconcileAfter,
        });

        if (r && r.ok) {
          setManualMsg(`${what} batch submitted (${markets.length} markets).`, true);
          if (reconcileAfter) await doReconcile(`ui_batch_${what.toLowerCase()}`);
          await refreshGlobalState();
        } else {
          setManualMsg(
            `${what} batch failed: ${r?.error || r?.message || "unknown"}`,
            false
          );
        }
      } catch (e) {
        setManualMsg(`${what} batch failed: ${e}`, false);
      }
    });
  }

  if (batchAllBtn) {
    batchAllBtn.addEventListener("click", async () => {
      const markets = pickBatchMarkets();
      if (markets.length === 0) {
        setManualMsg("No batch markets to trade (check running markets).", false);
        return;
      }

      const reconcileAfter = !!(reconcileAfterEl && reconcileAfterEl.checked);

      if (
        !confirmManual(
          `[Batch Trade]\nSELL ALL (100%)\nMarkets: ${markets.length}\nProceed?`
        )
      )
        return;

      try {
        const r = await submitManualBatch({
          markets,
          side: "sell",
          mode: "all",
          value: 100,
          reconcile_after: reconcileAfter,
        });

        if (r && r.ok) {
          setManualMsg(
            `SELL ALL batch submitted (${markets.length} markets).`,
            true
          );
          if (reconcileAfter) await doReconcile("ui_batch_sell_all");
          await refreshGlobalState();
        } else {
          setManualMsg(
            `SELL ALL batch failed: ${r?.error || r?.message || "unknown"}`,
            false
          );
        }
      } catch (e) {
        setManualMsg(`SELL ALL batch failed: ${e}`, false);
      }
    });
  }
}


/* =========================
 * RESERVED QUEUE (Bybit candidates)
 * ========================= */
let _reservedRenderSig = "";
let _reservedSettingsLastFetch = 0;

function reservedUiBusy() {
  const box = qs("reservedControls");
  if (!box) return false;
  const ae = document.activeElement;
  return !!(ae && box.contains(ae));
}

function summarizeDropCounts(drop) {
  if (!drop || typeof drop !== "object") return "";
  const keys = Object.keys(drop).sort();
  if (!keys.length) return "";
  const parts = [];
  for (const k of keys) {
    const v = Number(drop[k]);
    if (!Number.isFinite(v) || v <= 0) continue;
    parts.push(`${k}:${v}`);
    if (parts.length >= 6) break;
  }
  return parts.join(" · ");
}

function fmtOnOff(b) {
  return b ? "ON" : "OFF";
}

function getReservedPromoteToActive() {
  const el = qs("reservedPromoteActive");
  if (el) return !!el.checked;
  return !!(state.reservedSettings && state.reservedSettings.promote_to_active);
}

function getReservedApplyBudget() {
  const el = qs("reservedApplyBudget");
  if (el) return !!el.checked;
  const v = state.reservedSettings ? state.reservedSettings.apply_suggested_budget : undefined;
  return v === undefined ? true : !!v;
}

function buildReservedSummaryText(meta) {
  const m = meta || {};
  const s = m.summary || {};
  const lr = m.last_refresh_ts ? new Date(m.last_refresh_ts * 1000).toLocaleString() : "—";

  const pp = Number(s.picked_pingpong ?? 0);
  const al = Number(s.picked_autoloop ?? 0);
  const uni = Number(s.universe_filtered ?? 0);
  const tick = Number(s.tickers_loaded ?? 0);
  const ob = Number(s.orderbooks_loaded ?? 0);
  const el = s.elapsed_sec !== undefined ? `${s.elapsed_sec}s` : "—";

  const dpp = summarizeDropCounts(s.dropped_pingpong);
  const dal = summarizeDropCounts(s.dropped_autoloop);

  let tail = "";
  if (dpp) tail += ` | drop(PP): ${dpp}`;
  if (dal) tail += ` | drop(AL): ${dal}`;

  let base = `Last: ${lr} | pick: PP ${pp} / AL ${al} | universe ${uni} | tickers ${tick} · orderbooks ${ob} · ${el}${tail}`;

  const rs = state.reservedSettings || {};
  const ap = rs.autopilot || {};
  const apEnabled = !!ap.enabled;
  const autoApprove = !!ap.auto_approve;
  const promoteActive = !!rs.promote_to_active;
  const applyBudget = rs.apply_suggested_budget === undefined ? true : !!rs.apply_suggested_budget;

  const wEnabled = !!ap.window_enabled;
  const wStart = ap.window_start || "—";
  const wEnd = ap.window_end || "—";

  const idleEn = ap.idle_demote_enabled === undefined ? true : !!ap.idle_demote_enabled;
  const guardEn = !!ap.guard_demote_enabled;
  const sigEn = !!ap.signal_miss_enabled;

  const evalS = ap.eval_interval_sec !== undefined ? ap.eval_interval_sec : "—";
  const graceS = ap.grace_sec !== undefined ? ap.grace_sec : "—";

  base += `\nAutopilot=${fmtOnOff(apEnabled)} · AutoApprove=${fmtOnOff(autoApprove)} · Promote=${promoteActive ? "ACTIVE" : "WATCH"} · AutoBudget=${fmtOnOff(applyBudget)} · Window=${wEnabled ? `${wStart}-${wEnd}` : "OFF"} · NoFills=${fmtOnOff(idleEn)} · Guard=${fmtOnOff(guardEn)} · SignalNoFill=${fmtOnOff(sigEn)} · Re-eval=${evalS}s · Grace=${graceS}s`;

  return base;
}

/**
 * Reserved Queue 아이템 선택 시 해당 전략의 폼에 파라미터 자동 채우기
 * @param {Object} it - Reserved 아이템 (market, strategy, recommended_params, suggested_budget_usdt 등)
 */
function fillReservedItemToForm(it) {
  if (!it) return;
  
  const market = String(it.market || "").trim();
  const strat = String(it.strategy || "").toUpperCase();
  const budget = Number(it.suggested_budget_usdt) || 100;
  const recParams = it.recommended_params || {};
  const aiFeatures = it.ai_features || {};
  const aiScore = Number(it.ai_score) || 0.5;
  const metrics = it.metrics || {};
  
  // RSI/MACD 정보
  const rsi = metrics.rsi || 50;
  const changeRate = metrics.change_24h || 0;
  const volatility = aiFeatures.volatility || 0;
  const momentum = aiFeatures.momentum || 0;
  const trend = aiFeatures.trend || 0;
  const price = metrics.price || 0;
  
  // 전략별 폼 요소 채우기
  if (strat === "LADDER") {
    const lfMarket = qs("lfMarket");
    const lfBudget = qs("lfBudget");
    const lfSteps = qs("lfSteps");
    const lfStepPct = qs("lfStepPct");
    const lfTp = qs("lfTp");
    const lfMartingale = qs("lfMartingale");
    const lfStepAtr = qs("lfStepAtr");
    const lfCalcInfo = qs("lfCalcInfo");
    
    if (lfMarket) lfMarket.value = market;
    if (lfBudget) lfBudget.value = Math.round(budget);
    if (lfSteps && recParams.steps) lfSteps.value = recParams.steps;
    if (lfStepPct && recParams.step_pct) lfStepPct.value = recParams.step_pct;
    if (lfTp && recParams.tp_pct) lfTp.value = recParams.tp_pct;
    if (lfMartingale && recParams.martingale) lfMartingale.value = recParams.martingale;
    if (lfStepAtr) lfStepAtr.checked = !!recParams.use_atr;
    
    // 정보 표시
    if (lfCalcInfo) {
      const aiPct = (aiScore * 100).toFixed(0);
      const aiColor = aiScore >= 0.7 ? '#4caf50' : (aiScore >= 0.5 ? '#ff9800' : '#f44336');
      const rsiColor = rsi <= 30 ? '#4caf50' : (rsi >= 70 ? '#f44336' : '#888');
      lfCalcInfo.innerHTML = `
        <span style="color:${aiColor}; font-weight:bold;">AI ${aiPct}%</span> · 
        <span style="color:${rsiColor};">RSI ${rsi}</span> · 
        Vol ${volatility.toFixed(1)}% · $${Number(price).toLocaleString()}
      `;
    }
    
    setAdminMsg(`✓ ${market} → LADDER 폼에 채움 (예산 $${budget.toLocaleString()}, TP ${recParams.tp_pct || '-'}%)`, true);
    
  } else if (strat === "LIGHTNING") {
    const ltgMarket = qs("ltgMarket");
    const ltgBudget = qs("ltgBudget");
    const ltgTp = qs("ltgTp");
    const ltgSl = qs("ltgSl");
    const ltgCalcInfo = qs("ltgCalcInfo");
    
    if (ltgMarket) ltgMarket.value = market;
    if (ltgBudget) ltgBudget.value = Math.round(budget / 1000);
    if (ltgTp && recParams.tp_pct) ltgTp.value = recParams.tp_pct;
    if (ltgSl && recParams.sl_pct) ltgSl.value = Math.abs(recParams.sl_pct);
    
    if (ltgCalcInfo) {
      const aiPct = (aiScore * 100).toFixed(0);
      const aiColor = aiScore >= 0.7 ? '#4caf50' : (aiScore >= 0.5 ? '#ff9800' : '#f44336');
      const momColor = momentum >= 0 ? '#4caf50' : '#f44336';
      ltgCalcInfo.innerHTML = `
        <span style="color:${aiColor}; font-weight:bold;">AI ${aiPct}%</span> · 
        <span style="color:${momColor};">Mom ${momentum.toFixed(1)}%</span> · 
        $${Number(price).toLocaleString()}
      `;
    }
    
    setAdminMsg(`✓ ${market} → LIGHTNING 폼에 채움 (TP ${recParams.tp_pct || '-'}%, SL ${recParams.sl_pct || '-'}%)`, true);
    
  } else if (strat === "GAZUA") {
    const gzMarket = qs("gzMarket");
    const gzBudget = qs("gzBudget");
    const gzTargetValue = qs("gzTargetValue");
    const gzSlValue = qs("gzSlValue");
    const gzManualExit = qs("gzManualExit");
    const gzCalcInfo = qs("gzCalcInfo");
    
    if (gzMarket) gzMarket.value = market;
    if (gzBudget) gzBudget.value = Math.round(budget / 1000);
    if (gzTargetValue && recParams.tp_pct) gzTargetValue.value = recParams.tp_pct;
    if (gzSlValue && recParams.sl_pct) gzSlValue.value = Math.abs(recParams.sl_pct);
    if (gzManualExit) gzManualExit.checked = !!recParams.manual_exit;
    
    if (gzCalcInfo) {
      const aiPct = (aiScore * 100).toFixed(0);
      const aiColor = aiScore >= 0.7 ? '#4caf50' : (aiScore >= 0.5 ? '#ff9800' : '#f44336');
      const trendColor = trend >= 0 ? '#4caf50' : '#f44336';
      gzCalcInfo.innerHTML = `
        <span style="color:${aiColor}; font-weight:bold;">AI ${aiPct}%</span> · 
        <span style="color:${trendColor};">Trend ${trend >= 0 ? '+' : ''}${(trend * 100).toFixed(1)}%</span> · 
        $${Number(price).toLocaleString()}
      `;
    }
    
    setAdminMsg(`✓ ${market} → GAZUA 폼에 채움 (TP ${recParams.tp_pct || '-'}%, SL ${recParams.sl_pct || '-'}%)`, true);
    
  } else if (strat === "PINGPONG" || strat === "AUTOLOOP") {
    // OMA Admin 폼에 채우기 (기존 market/state/strategy 폼 사용)
    const adminMarket = qs("omaSetMarket");
    const adminState = qs("omaSetState");
    const adminStrategy = qs("omaSetStrategy");
    const adminBudget = qs("omaSetBudget");
    
    if (adminMarket) adminMarket.value = market;
    if (adminState) adminState.value = "ACTIVE";
    if (adminStrategy) adminStrategy.value = strat;
    if (adminBudget) adminBudget.value = budget;
    
    setAdminMsg(`✓ ${market} → OMA Admin 폼에 채움 (${strat}, 예산 $${budget.toLocaleString()})`, true);
  }
}

async function refreshReservedList() {
  const data = await fetchJson(API.reservedList);
  const root = (data && (data.reserved || data)) || {};
  const meta = root.meta || null;
  const items = Array.isArray(root.items) ? root.items : (Array.isArray(root.queue) ? root.queue : []);
  const history = Array.isArray(root.history) ? root.history : [];
  state.reserved = { meta, items, history };

  // signature for render skip
  const sig = JSON.stringify({
    n: items.length,
    a: meta?.last_refresh_ts ?? null,
    f: items[0]?.id ?? null,
    hn: history.length,
    ht: history.length ? (history[history.length - 1]?.ts ?? null) : null,
    hk: history.length ? (history[history.length - 1]?.kind ?? null) : null,
    hm: history.length ? (history[history.length - 1]?.market ?? null) : null,
  });
  return sig;
}

function applyReservedSettingsToUi(settings) {
  if (!settings || typeof settings !== "object") return;

  const nPP = qs("reservedNPingpong");
  const nAL = qs("reservedNAutoloop");
  const nLD = qs("reservedNLadder");
  const nLT = qs("reservedNLightning");
  const nGZ = qs("reservedNGazua");
  const nCT = qs("reservedNContrarian");
  const nSN = qs("reservedNSniper");
  if (nPP && settings.pingpong_n !== undefined) nPP.value = String(settings.pingpong_n);
  if (nAL && settings.autoloop_n !== undefined) nAL.value = String(settings.autoloop_n);
  if (nLD && settings.ladder_n !== undefined) nLD.value = String(settings.ladder_n);
  if (nLT && settings.lightning_n !== undefined) nLT.value = String(settings.lightning_n);
  if (nGZ && settings.gazua_n !== undefined) nGZ.value = String(settings.gazua_n);
  if (nCT && settings.contrarian_n !== undefined) nCT.value = String(settings.contrarian_n);
  if (nSN && settings.sniper_n !== undefined) nSN.value = String(settings.sniper_n);

  const ap = settings.autopilot || {};

  const apEnabled = qs("reservedAutoPilotEnabled");
  if (apEnabled && ap.enabled !== undefined) apEnabled.checked = !!ap.enabled;

  const autoApprove = qs("reservedAutoApprove");
  if (autoApprove && ap.auto_approve !== undefined) autoApprove.checked = !!ap.auto_approve;

  const promoteActive = qs("reservedPromoteActive");
  if (promoteActive && settings.promote_to_active !== undefined) promoteActive.checked = !!settings.promote_to_active;

  const applyBudget = qs("reservedApplyBudget");
  if (applyBudget && settings.apply_suggested_budget !== undefined) applyBudget.checked = !!settings.apply_suggested_budget;

  // time window
  const wEn = qs("reservedWindowEnabled");
  const wStart = qs("reservedWindowStart");
  const wEnd = qs("reservedWindowEnd");
  if (wEn && ap.window_enabled !== undefined) wEn.checked = !!ap.window_enabled;
  if (wStart && ap.window_start !== undefined) wStart.value = String(ap.window_start || "22:00");
  if (wEnd && ap.window_end !== undefined) wEnd.value = String(ap.window_end || "08:00");
  if (wStart) wStart.disabled = !(wEn && wEn.checked);
  if (wEnd) wEnd.disabled = !(wEn && wEn.checked);

  // demotion rules
  const rNoFills = qs("reservedRuleNoFills");
  const idleMin = qs("reservedIdleMin");
  if (rNoFills && ap.idle_demote_enabled !== undefined) rNoFills.checked = !!ap.idle_demote_enabled;
  if (idleMin && ap.idle_demote_min !== undefined) idleMin.value = String(ap.idle_demote_min ?? 0);
  if (idleMin) idleMin.disabled = !(rNoFills && rNoFills.checked);
  
  const ov = ap.idle_demote_overrides || {};
  const setOv = (id, key) => {
      const el = qs(id);
      if (el) el.value = (ov[key] !== undefined) ? String(ov[key]) : "";
  };
  setOv("idleOvLightning", "LIGHTNING"); setOv("idleOvGazua", "GAZUA");
  setOv("idleOvLadder", "LADDER"); setOv("idleOvPingpong", "PINGPONG"); setOv("idleOvAutoloop", "AUTOLOOP");

  const rGuards = qs("reservedRuleGuards");
  const guardN = qs("reservedGuardN");
  const guardWin = qs("reservedGuardWindowMin");
  if (rGuards && ap.guard_demote_enabled !== undefined) rGuards.checked = !!ap.guard_demote_enabled;
  if (guardN && ap.guard_demote_n !== undefined) guardN.value = String(ap.guard_demote_n ?? 0);
  if (guardWin && ap.guard_demote_window_min !== undefined) guardWin.value = String(ap.guard_demote_window_min ?? 0);
  if (guardN) guardN.disabled = !(rGuards && rGuards.checked);
  if (guardWin) guardWin.disabled = !(rGuards && rGuards.checked);

  const rSig = qs("reservedRuleSignalMiss");
  const sigMin = qs("reservedSignalMinAttempts");
  const sigWin = qs("reservedSignalWindowMin");
  if (rSig && ap.signal_miss_enabled !== undefined) rSig.checked = !!ap.signal_miss_enabled;
  if (sigMin && ap.signal_miss_min_attempts !== undefined) sigMin.value = String(ap.signal_miss_min_attempts ?? 0);
  if (sigWin && ap.signal_miss_window_min !== undefined) sigWin.value = String(ap.signal_miss_window_min ?? 0);
  if (sigMin) sigMin.disabled = !(rSig && rSig.checked);
  if (sigWin) sigWin.disabled = !(rSig && rSig.checked);

  const evalSec = qs("reservedEvalSec");
  const graceSec = qs("reservedGraceSec");
  if (evalSec && ap.eval_interval_sec !== undefined) evalSec.value = String(ap.eval_interval_sec);
  if (graceSec && ap.grace_sec !== undefined) graceSec.value = String(ap.grace_sec);

  const dmt = qs("reservedDemoteMaxTotal");
  const dms = qs("reservedDemoteMaxPerStrategy");
  if (dmt && ap.demote_max_total !== undefined) dmt.value = String(ap.demote_max_total);
  if (dms && ap.demote_max_per_strategy !== undefined) dms.value = String(ap.demote_max_per_strategy);

  // 전략별 AutoApprove 체크박스
  const aaLd = qs("aaLadder");
  const aaLt = qs("aaLightning");
  const aaGz = qs("aaGazua");
  if (aaLd) aaLd.checked = !!ap.auto_approve_ladder;
  if (aaLt) aaLt.checked = !!ap.auto_approve_lightning;
  if (aaGz) aaGz.checked = !!ap.auto_approve_gazua;
}

async function refreshReservedSettings(force = false) {
  const now = Date.now();
  if (!force && (now - _reservedSettingsLastFetch) < 15000) return state.reservedSettings;
  if (reservedUiBusy()) return state.reservedSettings;

  _reservedSettingsLastFetch = now;
  const data = await fetchJson(API.reservedSettingsGet);
  const settings = (data && (data.settings || data.reserved || data)) || null;
  state.reservedSettings = settings;
  try {
    applyReservedSettingsToUi(settings);
  } catch (_) {}
  return settings;
}

function readReservedSettingsFromUi() {
  const nPP = qs("reservedNPingpong");
  const nAL = qs("reservedNAutoloop");
  const nLD = qs("reservedNLadder");
  const nLT = qs("reservedNLightning");
  const nGZ = qs("reservedNGazua");
  const nCT = qs("reservedNContrarian");
  const nSN = qs("reservedNSniper");

  const apEnabled = qs("reservedAutoPilotEnabled");
  const aa = qs("reservedAutoApprove");

  const prom = qs("reservedPromoteActive");
  const ab = qs("reservedApplyBudget");

  const wEn = qs("reservedWindowEnabled");
  const wStart = qs("reservedWindowStart");
  const wEnd = qs("reservedWindowEnd");

  const rNoFills = qs("reservedRuleNoFills");
  const idleMin = qs("reservedIdleMin");

  const getOv = (id, key, map) => {
      const el = qs(id);
      if (el && el.value.trim() !== "") {
          const v = parseInt(el.value);
          if (v >= 0) map[key] = v;
      }
  };
  const idleOverrides = {};
  getOv("idleOvLightning", "LIGHTNING", idleOverrides); getOv("idleOvGazua", "GAZUA", idleOverrides);
  getOv("idleOvLadder", "LADDER", idleOverrides); getOv("idleOvPingpong", "PINGPONG", idleOverrides); getOv("idleOvAutoloop", "AUTOLOOP", idleOverrides);

  const rGuards = qs("reservedRuleGuards");
  const guardN = qs("reservedGuardN");
  const guardWin = qs("reservedGuardWindowMin");

  const rSig = qs("reservedRuleSignalMiss");
  const sigMin = qs("reservedSignalMinAttempts");
  const sigWin = qs("reservedSignalWindowMin");

  const evalSec = qs("reservedEvalSec");
  const graceSec = qs("reservedGraceSec");
  const dmtEl = qs("reservedDemoteMaxTotal");
  const dmsEl = qs("reservedDemoteMaxPerStrategy");

  const pp = nPP ? Number(nPP.value) : 5;
  const al = nAL ? Number(nAL.value) : 5;
  const ld = nLD ? Number(nLD.value) : 0;
  const lt = nLT ? Number(nLT.value) : 0;
  const gz = nGZ ? Number(nGZ.value) : 0;
  const ct = nCT ? Number(nCT.value) : 0;
  const sn = nSN ? Number(nSN.value) : 0;

  const idle = idleMin ? Number(idleMin.value) : 180;
  const evalS = evalSec ? Number(evalSec.value) : 300;
  const graceS = graceSec ? Number(graceSec.value) : 900;
  const dmt = dmtEl ? Number(dmtEl.value) : 2;
  const dms = dmsEl ? Number(dmsEl.value) : 1;

  const gN = guardN ? Number(guardN.value) : 12;
  const gW = guardWin ? Number(guardWin.value) : 30;

  const sN = sigMin ? Number(sigMin.value) : 6;
  const sW = sigWin ? Number(sigWin.value) : 30;

  const autopilotEnabled = apEnabled ? !!apEnabled.checked : false;
  const autoApprove = aa ? !!aa.checked : false;
  const promoteToActive = prom ? !!prom.checked : false;
  const applySuggestedBudget = ab ? !!ab.checked : true;

  // 전략별 AutoApprove
  const aaLdEl = qs("aaLadder");
  const aaLtEl = qs("aaLightning");
  const aaGzEl = qs("aaGazua");
  const autoApproveLadder = aaLdEl ? !!aaLdEl.checked : false;
  const autoApproveLightning = aaLtEl ? !!aaLtEl.checked : false;
  const autoApproveGazua = aaGzEl ? !!aaGzEl.checked : false;

  const windowEnabled = wEn ? !!wEn.checked : false;
  const windowStart = wStart ? String(wStart.value || "") : "";
  const windowEnd = wEnd ? String(wEnd.value || "") : "";

  const idleEnabled = rNoFills ? !!rNoFills.checked : (Number.isFinite(idle) ? idle > 0 : true);
  const guardsEnabled = rGuards ? !!rGuards.checked : false;
  const sigEnabled = rSig ? !!rSig.checked : false;

  return {
    pingpong_n: Number.isFinite(pp) ? pp : 3,
    autoloop_n: Number.isFinite(al) ? al : 3,
    ladder_n: Number.isFinite(ld) ? ld : 0,
    lightning_n: Number.isFinite(lt) ? lt : 0,
    gazua_n: Number.isFinite(gz) ? gz : 0,
    contrarian_n: Number.isFinite(ct) ? ct : 0,
    sniper_n: Number.isFinite(sn) ? sn : 0,

    autopilot_enabled: autopilotEnabled,
    auto_approve: autoApprove,
    auto_approve_ladder: autoApproveLadder,
    auto_approve_lightning: autoApproveLightning,
    auto_approve_gazua: autoApproveGazua,

    promote_to_active: promoteToActive,
    apply_suggested_budget: applySuggestedBudget,

    window_enabled: windowEnabled,
    window_start: windowStart,
    window_end: windowEnd,

    idle_demote_enabled: idleEnabled,
    idle_demote_min: Number.isFinite(idle) ? idle : 180,
    idle_demote_overrides: idleOverrides,

    guard_demote_enabled: guardsEnabled,
    guard_demote_window_min: Number.isFinite(gW) ? gW : 30,
    guard_demote_n: Number.isFinite(gN) ? gN : 12,

    signal_miss_enabled: sigEnabled,
    signal_miss_window_min: Number.isFinite(sW) ? sW : 30,
    signal_miss_min_attempts: Number.isFinite(sN) ? sN : 6,

    eval_interval_sec: Number.isFinite(evalS) ? evalS : 300,
    grace_sec: Number.isFinite(graceS) ? graceS : 900,
    demote_max_total: Number.isFinite(dmt) ? dmt : 2,
    demote_max_per_strategy: Number.isFinite(dms) ? dms : 1,
  };
}

function renderReservedPanel(force = false) {
  const wrap = qs("reservedQueue");
  const empty = qs("reservedEmpty");
  const histWrap = qs("reservedHistory");
  const histEmpty = qs("reservedHistoryEmpty");
  const sumEl = qs("reservedSummary");

  if (!wrap && !histWrap) return;
  if (!force && reservedUiBusy()) return;

  const meta = state.reserved?.meta || {};
  const items = Array.isArray(state.reserved?.items) ? state.reserved.items : [];
  const history = Array.isArray(state.reserved?.history) ? state.reserved.history : [];

  // Summary
  if (sumEl) {
    sumEl.textContent = buildReservedSummaryText(meta);
  }

  // Signature check (include history, because AutoApprove can consume queue instantly)
  const sig = JSON.stringify({
    n: items.length,
    a: meta?.last_refresh_ts ?? null,
    f: items[0]?.id ?? null,
    hn: history.length,
    ht: history.length ? (history[history.length - 1]?.ts ?? null) : null,
    hk: history.length ? (history[history.length - 1]?.kind ?? null) : null,
    hm: history.length ? (history[history.length - 1]?.market ?? null) : null,
  });
  if (!force && sig === _reservedRenderSig) return;
  _reservedRenderSig = sig;

  // -----------------
  // Queue (candidates)
  // -----------------
  if (wrap) clear(wrap);
  if (!items.length) {
    if (empty) empty.style.display = "block";
  } else {
    if (empty) empty.style.display = "none";
  }

  const toState = getReservedPromoteToActive() ? "ACTIVE" : "WATCH";
  const applyBudget = getReservedApplyBudget();
  const approveText = getReservedPromoteToActive() ? "Approve→ACTIVE" : "Approve→WATCH";

  for (const it of items) {
    const rid = String(it.id || "");
    const market = String(it.market || "");
    const strat = String(it.strategy || "").toUpperCase();
    const budget = Number(it.suggested_budget_usdt);

    const names = it.names || {};
    const nameKr = names.kr ? ` (${names.kr})` : "";

    const m = it.metrics || {};
    const spread = Number(m.spread_bps);
    const vol24 = Number(m.vol24_usdt);
    const depth = Math.min(Number(m.depth_ask_usdt) || 0, Number(m.depth_bid_usdt) || 0);
    const rr = Number(m.range_ratio_24h);
    const trades = m.recent_trades;

    const row = document.createElement("div");
    row.className = "reserved-row";

    const top = document.createElement("div");
    top.className = "top";

    const left = document.createElement("div");
    left.innerHTML = `<span class="mkt">${escHtml(market)}</span><span class="strategy" data-strat="${escHtml(strat)}">${escHtml(strat)}</span>${escHtml(nameKr)}`;

    const right = document.createElement("div");
    right.className = "row-actions";

    const approveBtn = document.createElement("button");
    approveBtn.className = "btn";
    approveBtn.textContent = approveText;
    approveBtn.addEventListener("click", async () => {
      try {
        setAdminMsg(`approving ${market}...`, true);
        await fetchJson(API.reservedApprove(rid, toState, applyBudget), { method: "POST" });
        await refreshGlobalState();
        setAdminMsg(`approved: ${market} → ${toState} (${strat})`, true);
      } catch (e) {
        console.error(e);
        setAdminMsg(`approve failed: ${e?.message || e}`, false);
      }
    });

    const rejectBtn = document.createElement("button");
    rejectBtn.className = "btn btn-danger";
    rejectBtn.textContent = "Reject";
    rejectBtn.addEventListener("click", async () => {
      try {
        await fetchJson(API.reservedReject(rid), { method: "POST" });
        await refreshReservedList();
        renderReservedPanel(true);
        setAdminMsg(`rejected: ${market}`, true);
      } catch (e) {
        console.error(e);
        setAdminMsg(`reject failed: ${e?.message || e}`, false);
      }
    });

    const detailsBtn = document.createElement("button");
    detailsBtn.className = "btn btn-ghost";
    detailsBtn.textContent = "Details";
    detailsBtn.addEventListener("click", () => {
      try {
        alert(JSON.stringify(it, null, 2));
      } catch (_) {
        alert(String(it));
      }
    });

    right.appendChild(detailsBtn);
    right.appendChild(rejectBtn);
    right.appendChild(approveBtn);

    top.appendChild(left);
    top.appendChild(right);

    const metaLine = document.createElement("div");
    metaLine.className = "meta";
    metaLine.innerHTML =
      `Budget <b>${fmtKr(budget)} USDT</b>` +
      ` · Spread <b>${Number.isFinite(spread) ? fmtNum(spread, 1) : "—"} bps</b>` +
      ` · Depth <b>${fmtKr(depth)} USDT</b>` +
      ` · Vol24 <b>${fmtKr(vol24)} USDT</b>` +
      ` · Range <b>${Number.isFinite(rr) ? fmtNum(rr * 100, 2) : "—"}%</b>` +
      (trades !== null && trades !== undefined ? ` · Trades(${meta?.summary?.params?.recent_minutes ?? "N"}m) <b>${trades}</b>` : "");

    row.appendChild(top);
    row.appendChild(metaLine);

    // AI 피처 및 추천 파라미터 표시 (모든 전략)
    const aiFeatures = it.ai_features || {};
    const recParams = it.recommended_params || {};
    const aiScore = Number(it.ai_score) || 0.5;
    
    const aiLine = document.createElement("div");
    aiLine.className = "meta ai-features";
    
    const trend = Number(aiFeatures.trend) || 0;
    const momentum = Number(aiFeatures.momentum) || 0;
    const volatility = Number(aiFeatures.volatility) || 0;
    
    const trendIcon = trend > 0.1 ? "📈" : (trend < -0.1 ? "📉" : "➡️");
    const aiIcon = aiScore >= 0.8 ? "🔥" : (aiScore < 0.4 ? "⚠️" : "");
    
    // Strategy badge color (통일: Guard Matrix 기준)
    const stratColors = {
      'LADDER': '#f5a623',
      'LIGHTNING': '#4fc3f7',
      'GAZUA': '#66bb6a',
      'PINGPONG': '#9fa8da',
      'AUTOLOOP': '#ef9a9a',
      'LONGHOLD': '#ffb74d',
      'AI': '#888'
    };
    const stratColor = stratColors[strat] || '#888';
    
    const stratLabels = {
      'LADDER': 'DCA 기회',
      'LIGHTNING': '돌파 감지',
      'GAZUA': '상승 잠재',
      'PINGPONG': '박스권 매매',
      'AUTOLOOP': '분할매수'
    };
    const stratLabel = stratLabels[strat] || strat;
    
    let stratHint = `<span class="strat-badge" style="background:${stratColor}22; color:${stratColor}; padding:1px 5px; border-radius:3px; font-size:9px; font-weight:600;">${stratLabel}</span>`;
    let recHtml = "";
    
    if (strat === "LADDER") {
      const p = recParams;
      recHtml = p.tp_pct ? ` · <span style="color:#4fc3f7;">📊 추천: Steps ${p.steps || '-'} Gap ${p.step_pct || '-'}%${p.use_atr ? ' ATR' : ''} TP ${p.tp_pct}%</span>` : "";
    } else if (strat === "LIGHTNING") {
      const p = recParams;
      recHtml = p.tp_pct ? ` · <span style="color:#4fc3f7;">📊 추천: TP ${p.tp_pct}% SL ${p.sl_pct}%</span>` : "";
    } else if (strat === "GAZUA") {
      const p = recParams;
      recHtml = p.tp_pct ? ` · <span style="color:#4fc3f7;">📊 추천: TP ${p.tp_pct}% SL ${p.sl_pct}%${p.manual_exit ? ' Manual' : ''}</span>` : "";
    } else if (strat === "PINGPONG") {
      const p = recParams;
      recHtml = p.tp_pct ? ` · <span style="color:#4fc3f7;">📊 추천: TP ${p.tp_pct}% SL ${p.sl_pct}% RSI ${p.rsi_buy || 30}/${p.rsi_sell || 70}</span>` : "";
    } else if (strat === "AUTOLOOP") {
      const p = recParams;
      const tier = p.confidence_tier || "-";
      const mult = p.budget_multiplier || 1.0;
      recHtml = p.tp_pct ? ` · <span style="color:#4fc3f7;">📊 추천: TP ${p.tp_pct}% x${mult.toFixed(1)} (${tier})</span>` : "";
    }
    
    aiLine.innerHTML =
      `${stratHint}` +
      ` · AI <b>${aiIcon} ${(aiScore * 100).toFixed(0)}%</b>` +
      ` · Trend <b>${trendIcon} ${fmtNum(trend * 100, 1)}%</b>` +
      ` · Vol <b>${fmtNum(volatility, 2)}%</b>` +
      recHtml;
    
    row.appendChild(aiLine);

    // 아이템 클릭 시 해당 전략의 폼에 파라미터 자동 채우기
    row.style.cursor = "pointer";
    row.addEventListener("click", (e) => {
      // 버튼 클릭은 이벤트 전파 중단
      if (e.target.tagName === "BUTTON" || e.target.closest("button")) return;
      
      fillReservedItemToForm(it);
    });

    if (wrap) wrap.appendChild(row);
  }

  // -----------------
  // History (promote/demote/approve/reject)
  // -----------------
  const fmtTs = (ts) => {
    try {
      const t = Number(ts);
      if (!Number.isFinite(t) || t <= 0) return "—";
      return new Date(t * 1000).toLocaleString();
    } catch (_) {
      return "—";
    }
  };

  const tagForKind = (kind) => {
    const k = String(kind || "").toUpperCase();
    if (k === "PROMOTE" || k === "APPROVE") return { cls: "pill pill-ok", label: k };
    if (k === "DEMOTE") return { cls: "pill pill-warn", label: k };
    if (k === "REJECT") return { cls: "pill pill-bad", label: k };
    if (k === "AUTOPILOT_SKIP") return { cls: "pill pill-warn", label: "SKIP" };
    if (k === "AUTOPILOT_STEP") return { cls: "pill", label: "STEP" };
    if (k === "SCAN") return { cls: "pill", label: "SCAN" };
    if (k === "CLEAR") return { cls: "pill", label: "CLEAR" };
    return { cls: "pill", label: k || "EV" };
  };

  if (histWrap) {
    clear(histWrap);
    const list = Array.isArray(history) ? history.slice() : [];
    // newest first
    list.sort((a, b) => Number(b?.ts || 0) - Number(a?.ts || 0));

    const maxShow = getReservedHistoryShowMax();
    const show = list.slice(0, maxShow);

    if (!show.length) {
      if (histEmpty) histEmpty.style.display = "block";
    } else {
      if (histEmpty) histEmpty.style.display = "none";
      for (const ev of show) {
        const kind = String(ev?.kind || "EV").toUpperCase();
        const src = String(ev?.source || "").toUpperCase();

        const market = String(ev?.market || "").toUpperCase();
        const strat = String(ev?.strategy || "").toUpperCase();
        const toState = String(ev?.to_state || ev?.state || "").toUpperCase();
        const fromState = String(ev?.from_state || "").toUpperCase();
        const budget2 = ev?.budget_usdt;

        const tag = tagForKind(kind);

        const row = document.createElement("div");
        row.className = "reserved-row";

        const top = document.createElement("div");
        top.className = "top";

        const left = document.createElement("div");
        const mkt = market ? `<span class="mkt">${escHtml(market)}</span>` : `<span class="mkt">—</span>`;
        const st = strat ? `<span class="strategy">${escHtml(strat)}</span>` : "";
        const tg = `<span class="hist-tag ${escHtml(tag.cls)}">${escHtml(tag.label)}</span>`;
        const srcTxt = src ? ` <span class="panel-sub">${escHtml(src)}</span>` : "";
        left.innerHTML = `${mkt}${st}${tg}${srcTxt}`;

        const right = document.createElement("div");
        right.className = "hist-time";
        right.textContent = fmtTs(ev?.ts);

        top.appendChild(left);
        top.appendChild(right);

        const metaLine = document.createElement("div");
        metaLine.className = "meta";
        const parts = [];
        if (fromState || toState) parts.push(`state <b>${escHtml(fromState || "—")}→${escHtml(toState || "—")}</b>`);
        if (budget2 !== undefined && budget2 !== null) parts.push(`budget <b>${escHtml(fmtKr(budget2))}</b>`);
        if (kind === "AUTOPILOT_SKIP" && ev?.reason) parts.push(`reason <b>${escHtml(String(ev.reason).slice(0, 64))}</b>`);
        if (kind === "AUTOPILOT_STEP" && ev?.step) parts.push(`step <b>${escHtml(String(ev.step).slice(0, 64))}</b>`);
        if (kind === "DEMOTE" && Array.isArray(ev?.rules)) {
          const rules = ev.rules.map(r => String(r?.rule || "").trim()).filter(Boolean);
          if (rules.length) parts.push(`rules <b>${escHtml(rules.slice(0,3).join(","))}</b>`);
        }
        metaLine.innerHTML = parts.length ? parts.join(" · ") : "—";

        row.appendChild(top);
        row.appendChild(metaLine);
        histWrap.appendChild(row);
      }
    }
  }
}

function bindReservedControls() {
  const btn = qs("reservedRefresh");
  const clearBtn = qs("reservedClear");
  const histClearBtn = qs("reservedHistoryClear");
  const histShowMaxEl = qs("reservedHistoryShowMax");
  const nPP = qs("reservedNPingpong");
  const nAL = qs("reservedNAutoloop");
  const nLD = qs("reservedNLadder");
  const nLT = qs("reservedNLightning");
  const nGZ = qs("reservedNGazua");
  const nCT = qs("reservedNContrarian");
  const nSN = qs("reservedNSniper");
  const sumEl = qs("reservedSummary");

  const saveBtn = qs("reservedSaveSettings");
  const runBtn = qs("reservedRunAutopilot");

  if (histClearBtn) {
    histClearBtn.addEventListener("click", async () => {
      try {
        await fetchJson(API.reservedHistoryClear, { method: "POST" });
        await refreshReservedList();
        renderReservedPanel(true);
        setAdminMsg("reserved history cleared", true);
      } catch (e) {
        console.error(e);
        setAdminMsg(`reserved history clear failed: ${e?.message || e}`, false);
      }
    });
  }

  // Reserved History display count (UI-only; persists in localStorage)
  if (histShowMaxEl) {
    // init from localStorage (or keep default)
    try {
      setReservedHistoryShowMax(getReservedHistoryShowMax());
    } catch (_) {}

    histShowMaxEl.addEventListener("change", () => {
      try {
        setReservedHistoryShowMax(histShowMaxEl.value);
        renderReservedPanel(true);
      } catch (e) {
        console.error(e);
      }
    });

    // Allow Enter to commit
    histShowMaxEl.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        try { histShowMaxEl.blur(); } catch (_) {}
      }
    });
  }

  // update approve button labels on toggle changes
  const prom = qs("reservedPromoteActive");
  if (prom) prom.addEventListener("change", () => renderReservedPanel(true));
  const ab = qs("reservedApplyBudget");
  if (ab) ab.addEventListener("change", () => renderReservedPanel(true));

  // Enable/disable dependent inputs
  const updateDeps = () => {
    const wEn = qs("reservedWindowEnabled");
    const wStart = qs("reservedWindowStart");
    const wEnd = qs("reservedWindowEnd");
    const w = wEn ? !!wEn.checked : false;
    if (wStart) wStart.disabled = !w;
    if (wEnd) wEnd.disabled = !w;

    const rNo = qs("reservedRuleNoFills");
    const idleMin = qs("reservedIdleMin");
    const nf = rNo ? !!rNo.checked : true;
    if (idleMin) idleMin.disabled = !nf;

    const rG = qs("reservedRuleGuards");
    const guardN = qs("reservedGuardN");
    const guardWin = qs("reservedGuardWindowMin");
    const g = rG ? !!rG.checked : false;
    if (guardN) guardN.disabled = !g;
    if (guardWin) guardWin.disabled = !g;

    const rS = qs("reservedRuleSignalMiss");
    const sigMin = qs("reservedSignalMinAttempts");
    const sigWin = qs("reservedSignalWindowMin");
    const s = rS ? !!rS.checked : false;
    if (sigMin) sigMin.disabled = !s;
    if (sigWin) sigWin.disabled = !s;
  };

  // init + bind
  updateDeps();
  const wEn = qs("reservedWindowEnabled");
  if (wEn) wEn.addEventListener("change", updateDeps);
  const rNo = qs("reservedRuleNoFills");
  if (rNo) rNo.addEventListener("change", updateDeps);
  const rG = qs("reservedRuleGuards");
  if (rG) rG.addEventListener("change", updateDeps);
  const rS = qs("reservedRuleSignalMiss");
  if (rS) rS.addEventListener("change", updateDeps);

  if (btn) {
    btn.addEventListener("click", async () => {
      const pp = nPP ? Number(nPP.value) : 5;
      const al = nAL ? Number(nAL.value) : 5;
      const ld = nLD ? Number(nLD.value) : 0;
      const lt = nLT ? Number(nLT.value) : 0;
      const gz = nGZ ? Number(nGZ.value) : 0;
      const ct = nCT ? Number(nCT.value) : 0;
      const sn = nSN ? Number(nSN.value) : 0;

      try {
        if (sumEl) sumEl.textContent = "Scanning Bybit…";
        setAdminMsg("reserved scan running…", true);

        const data = await fetchJson(API.reservedRefresh(pp, al, ld, lt, gz, ct, sn), { method: "POST" });
        // Use returned snapshot immediately (avoid extra GET)
        const nowMeta = {
          last_refresh_ts: Date.now() / 1000,
          summary: (data && data.summary) || {},
        };
        const items = Array.isArray(data && data.items) ? data.items : [];
        const prevHist = Array.isArray(state.reserved?.history) ? state.reserved.history : [];
        state.reserved = { meta: nowMeta, items, history: prevHist };
        renderReservedPanel(true);

        // Pull full snapshot (incl. history) so "scan" event becomes visible immediately
        try {
          await refreshReservedList();
          renderReservedPanel(true);
        } catch (_) {}

        // keep settings in sync (persisted defaults)
        try {
          await refreshReservedSettings(true);
        } catch (_) {}

        setAdminMsg(`reserved updated: ${items.length} items`, true);
      } catch (e) {
        console.error(e);
        if (sumEl) sumEl.textContent = `scan failed: ${e?.message || e}`;
        setAdminMsg(`reserved scan failed: ${e?.message || e}`, false);
      }
    });
  }

  // [2026-02-01] RUN NOW 버튼: force_fill=true로 조건 무시하고 슬롯 강제 채우기
  const forceBtn = qs("reservedRefreshForce");
  if (forceBtn) {
    forceBtn.addEventListener("click", async () => {
      const pp = nPP ? Number(nPP.value) : 5;
      const al = nAL ? Number(nAL.value) : 5;
      const ld = nLD ? Number(nLD.value) : 0;
      const lt = nLT ? Number(nLT.value) : 0;
      const gz = nGZ ? Number(nGZ.value) : 0;
      const ct = nCT ? Number(nCT.value) : 0;
      const sn = nSN ? Number(nSN.value) : 0;

      try {
        if (sumEl) sumEl.textContent = "Force filling slots…";
        setAdminMsg("reserved force scan running…", true);

        const data = await fetchJson(API.reservedRefresh(pp, al, ld, lt, gz, ct, sn, true), { method: "POST" });
        const nowMeta = {
          last_refresh_ts: Date.now() / 1000,
          summary: (data && data.summary) || {},
        };
        const items = Array.isArray(data && data.items) ? data.items : [];
        const prevHist = Array.isArray(state.reserved?.history) ? state.reserved.history : [];
        state.reserved = { meta: nowMeta, items, history: prevHist };
        renderReservedPanel(true);

        try {
          await refreshReservedList();
          renderReservedPanel(true);
        } catch (_) {}

        try {
          await refreshReservedSettings(true);
        } catch (_) {}

        setAdminMsg(`reserved force filled: ${items.length} items`, true);
      } catch (e) {
        console.error(e);
        if (sumEl) sumEl.textContent = `force scan failed: ${e?.message || e}`;
        setAdminMsg(`reserved force scan failed: ${e?.message || e}`, false);
      }
    });
  }

  if (clearBtn) {
    clearBtn.addEventListener("click", async () => {
      try {
        await fetchJson(API.reservedClear, { method: "POST" });
        await refreshReservedList();
        renderReservedPanel(true);
        setAdminMsg("reserved cleared", true);
      } catch (e) {
        console.error(e);
        setAdminMsg(`reserved clear failed: ${e?.message || e}`, false);
      }
    });
  }

  if (saveBtn) {
    saveBtn.addEventListener("click", async () => {
      try {
        const opts = readReservedSettingsFromUi();
        setAdminMsg("saving reserved settings…", true);
        const data = await fetchJson(API.reservedSettingsSet(opts), { method: "POST" });
        const settings = (data && data.settings) || null;
        state.reservedSettings = settings;
        applyReservedSettingsToUi(settings);
        renderReservedPanel(true);
        setAdminMsg("reserved settings saved", true);
      } catch (e) {
        console.error(e);
        setAdminMsg(`reserved settings save failed: ${e?.message || e}`, false);
      }
    });
  }

  if (runBtn) {
    runBtn.addEventListener("click", async () => {
      try {
        setAdminMsg("autopilot running…", true);
        const data = await fetchJson(API.reservedAutopilotRun(false), { method: "POST" });

        // Sync settings + reserved list snapshot if provided
        if (data && data.settings) {
          state.reservedSettings = data.settings;
          applyReservedSettingsToUi(data.settings);
        }
        if (data && data.reserved) {
          state.reserved = {
            meta: data.reserved.meta || null,
            items: Array.isArray(data.reserved.items) ? data.reserved.items : [],
            history: Array.isArray(data.reserved.history)
              ? data.reserved.history
              : (Array.isArray(state.reserved?.history) ? state.reserved.history : []),
          };
          renderReservedPanel(true);
        }

        // Refresh the rest (OMA Control) because markets may have moved
        await refreshGlobalState();

        const r = (data && data.result) || {};
        const dem = Array.isArray(r.demoted) ? r.demoted.length : 0;
        const pro = Array.isArray(r.promoted) ? r.promoted.length : 0;
        setAdminMsg(`autopilot done: demoted ${dem} / promoted ${pro}`, true);
      } catch (e) {
        console.error(e);
        setAdminMsg(`autopilot failed: ${e?.message || e}`, false);
      }
    });
  }
}

async function refreshGlobalState() {
  const data = await fetchJson(API.systemStatus);
  const sys = (data && (data.system || data)) || {};

  state.system = sys;
  state.coordinator = sys.coordinator || {};
  state.oma = sys.oma || { active: [], watch: [], recovery: [] };
  state.prices = sys.active_prices || sys.activePrices || {};

  state.managedMarkets = deriveManagedMarkets(sys);
  state.allKnownMarkets = deriveAllKnownMarkets(sys);

  ensureMarketDatalist();

  // 선택 시장이 없거나, 더 이상 관리대상에 없으면 첫 시장으로 보정
  if (!state.selectedMarket || !state.managedMarkets.includes(state.selectedMarket)) {
    const fallback = state.managedMarkets[0] || Object.keys(state.prices || {})[0] || null;
    state.selectedMarket = fallback;
  }

  renderHeaderStats();
  renderMarketRegistry();
  renderPriceBoard();
  renderEngineMarketSelect();
  // Guard Matrix (replaces legacy AI/Risk panel)
  try {
    await refreshGuards();
  } catch (e) {
    console.error(e);
    setGuardMsg(`guards fetch error: ${e.message}`);
  }

  try {
    renderGuardsPanel();
  } catch (e) {
    console.error(e);
    setGuardMsg(`guards render error: ${e.message}`);
  }

  try {
    renderPnlPanels();
  } catch (e) {
    console.error(e);
  }
  try {
    renderManualTradePanel();
  } catch (e) {
    console.error(e);
  }

  // Reserved Queue (proposal / autopilot)
  try {
    await refreshReservedSettings(false);
    const sig = await refreshReservedList();
    if (sig !== _reservedRenderSig) {
      renderReservedPanel(false);
    }
  } catch (e) {
    console.error(e);
    // reserved is optional; do not spam adminMsg
  }

  // Market Alerts (delisting / new listing / preview)
  try {
    await loadMarketAlerts();
  } catch (e) {
    console.error(e);
  }
}

async function init() {
  // Load quote currency config first
  await loadQuoteCurrencyConfig();

  // If any one panel binding throws, the whole dashboard can "freeze" because
  // the polling timer never starts. Make init resilient so the UI keeps updating.
  const safe = (fn, name) => {
    try {
      fn();
    } catch (e) {
      console.error(e);
      setAdminMsg(`${name} error: ${e?.message || e}`, false);
    }
  };

  // Surface unexpected runtime errors in the UI (instead of silently killing init)
  try {
    window.addEventListener("error", (ev) => {
      const msg = ev?.error?.message || ev?.message || "unknown error";
      console.error("window.error:", ev?.error || ev);
      setAdminMsg(`ui error: ${msg}`, false);
    });
    window.addEventListener("unhandledrejection", (ev) => {
      const msg = ev?.reason?.message || String(ev?.reason || "unknown rejection");
      console.error("unhandledrejection:", ev?.reason || ev);
      setAdminMsg(`ui error: ${msg}`, false);
    });
  } catch (_) {
    // ignore
  }

  safe(bindAdminControls, "bindAdminControls");
  safe(bindEngineControls, "bindEngineControls");
  safe(bindPnlTabs, "bindPnlTabs");
  safe(bindManualTradeControls, "bindManualTradeControls");

  safe(bindReservedControls, "bindReservedControls");
  safe(bindMarketAlertsControls, "bindMarketAlertsControls");
  safe(bindHoldingsUpsideControls, "bindHoldingsUpsideControls");
  safe(bindKimchiControls, "bindKimchiControls");
  safe(initQuickTrade, "initQuickTrade");
  safe(initStrategyTabs, "initStrategyTabs");

  // Initial load of market alerts & upside rankings
  loadMarketAlerts();
  loadHoldingsUpside();
  
  // 바이낸스 가격 초기 로드 (김프 계산용)
  // 바이낸스 가격 초기 로드 (블로킹 - 김프 표시를 위해 먼저 로드)
  await fetchBinancePrices();

  // Restore AI trend memory
  try {
    const saved = localStorage.getItem("nunnaya_ai_prev_v1");
    if (saved) state._aiPrev = JSON.parse(saved);
  } catch (_) {}

  let pollInFlight = false;

  const pollTick = async (label) => {
    if (pollInFlight) return;
    pollInFlight = true;
    try {
      await refreshGlobalState();
      // 바이낸스 가격 백그라운드 갱신 (60초 캐시이므로 매번 호출해도 무방)
      fetchBinancePrices();
      setAdminMsg("ready", true);
    } catch (e) {
      console.error(e);
      setAdminMsg(`${label || "poll"} error: ${e?.message || e}`, false);
    } finally {
      pollInFlight = false;
    }
  };

  // 1st draw (don't let a one-off failure prevent polling from starting)
  await pollTick("init");

  // Periodic polling
  if (IntervalManager) {
    IntervalManager.set("dashboard_poll", () => {
      pollTick("poll");
    }, POLL_MS);
  } else {
    setInterval(() => {
      pollTick("poll");
    }, POLL_MS);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  try {
    init();
  } catch (e) {
    console.error(e);
    try { setAdminMsg(`init error: ${e?.message || e}`, false); } catch (_) {}
  }
});

// Expose functions to global scope for onclick handlers and HTML scripts
window.selectMarket = selectMarket;
window.renderKimchiBadge = renderKimchiBadge;
window.getKimchiPremium = getKimchiPremium;
window.fetchBinancePrices = fetchBinancePrices;

/* =========================
 * WebSocket Real-time Updates
 * ========================= */
let ws = null;
let wsReconnectTimer = null;
const WS_RECONNECT_DELAY = 3000;

function connectWebSocket() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  
  try {
    const protocol = location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${location.host}/ws/dashboard`;
    
    ws = new WebSocket(wsUrl);
    
    ws.onopen = () => {
      console.log("[WS] Connected to dashboard WebSocket");
      state.wsConnected = true;
      updateWsIndicator(true);
      
      // Clear any pending reconnect
      if (wsReconnectTimer) {
        clearTimeout(wsReconnectTimer);
        wsReconnectTimer = null;
      }
    };
    
    ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        handleWsMessage(msg);
      } catch (e) {
        console.error("[WS] Parse error:", e);
      }
    };
    
    ws.onclose = (event) => {
      console.log("[WS] Connection closed:", event.code, event.reason);
      state.wsConnected = false;
      updateWsIndicator(false);
      ws = null;
      
      // Schedule reconnect
      if (!wsReconnectTimer) {
        wsReconnectTimer = setTimeout(() => {
          wsReconnectTimer = null;
          connectWebSocket();
        }, WS_RECONNECT_DELAY);
      }
    };
    
    ws.onerror = (error) => {
      console.error("[WS] Error:", error);
      state.wsConnected = false;
      updateWsIndicator(false);
    };
    
  } catch (e) {
    console.error("[WS] Failed to create WebSocket:", e);
    state.wsConnected = false;
    updateWsIndicator(false);
  }
}

function handleWsMessage(msg) {
  if (!msg || !msg.type) return;
  
  switch (msg.type) {
    case "rankings":
      if (msg.data && msg.data.ok && msg.data.rankings) {
        const r = msg.data.rankings;
        if (r.rebound) renderReboundSection(r.rebound);
        if (r.rsi_oversold) renderRsiSection(r.rsi_oversold);
        if (r.tech_score) renderTechScoreSection(r.tech_score);
        if (r.upside) renderUpsideSection(r.upside);
        
        // 전략 탭에도 WebSocket 데이터 적용 (현재 선택된 탭이 있으면)
        updateStrategyTabWithWsData(r);
        
        console.log("[WS] Rankings updated via WebSocket");
      }
      break;
      
    case "pong":
      // heartbeat response, ignore
      break;
      
    case "error":
      console.error("[WS] Server error:", msg.message);
      break;
      
    default:
      console.log("[WS] Unknown message type:", msg.type);
  }
}

function updateWsIndicator(connected) {
  const indicator = qs("wsIndicator");
  if (!indicator) return;
  
  if (connected) {
    indicator.style.backgroundColor = "#4caf50";
    indicator.title = "WebSocket 연결됨 (실시간 업데이트)";
  } else {
    indicator.style.backgroundColor = "#ff5252";
    indicator.title = "WebSocket 연결 끊김 (폴링 모드)";
  }
}

function sendWsPing() {
  if (ws && ws.readyState === WebSocket.OPEN) {
    ws.send("ping");
  }
}

// Handle strategy tab item click with duplicate check
window.handleStrategyTabClick = function(el, market, activeStrategy) {
  if (activeStrategy) {
    if (!confirm(`${market}은 이미 ${activeStrategy}에서 사용 중입니다.\n다른 전략에 배치하시겠습니까?`)) {
      return;
    }
  }
  window.open('/ui/market_detail.html?market=' + encodeURIComponent(market), '_blank');
};

// Start WebSocket connection on init
setTimeout(() => {
  connectWebSocket();
  // Periodic ping to keep connection alive
  setInterval(sendWsPing, 30000);
}, 1000);

// ============================================================
// Sector Map Modal
// ============================================================
async function openSectorMapModal() {
  // 기존 모달이 있으면 제거
  const existing = document.getElementById("sectorMapModal");
  if (existing) existing.remove();

  // 섹터 데이터 로드
  let sectorData = { sectors: {}, default_cap: 0.4 };
  try {
    const resp = await fetchJson("/api/system/sector-map");
    if (resp.ok) {
      sectorData = resp;
    }
  } catch (e) {
    console.error("Failed to load sector map:", e);
  }

  const sectors = sectorData.sectors || {};
  const sectorIds = Object.keys(sectors);

  // 모달 HTML 생성
  const modal = document.createElement("div");
  modal.id = "sectorMapModal";
  modal.style.cssText = `
    position: fixed; top: 0; left: 0; width: 100%; height: 100%;
    background: rgba(0,0,0,0.7); z-index: 9999; display: flex;
    align-items: center; justify-content: center;
  `;

  modal.innerHTML = `
    <div style="background: var(--card); border-radius: 8px; padding: 20px; max-width: 800px; max-height: 80vh; overflow-y: auto; width: 90%;">
      <h3 style="margin: 0 0 16px 0;">🏷️ Sector Map 설정</h3>
      <p style="color: var(--muted); font-size: 12px; margin-bottom: 16px;">
        Smart Allocation에서 섹터별 예산 균형을 유지합니다. 각 섹터의 최대 비중(Cap)을 설정하세요.
      </p>
      
      <div id="sectorList" style="display: grid; gap: 12px;">
        ${sectorIds.map(sid => {
          const s = sectors[sid];
          const coins = (s.coins || []).join(", ");
          return `
            <div class="sector-item" style="background: var(--bg); padding: 10px; border-radius: 6px; border: 1px solid var(--border);">
              <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px;">
                <strong>${s.name || sid}</strong>
                <span style="font-size: 11px; color: var(--muted);">${sid}</span>
              </div>
              <div style="display: flex; gap: 12px; align-items: center; flex-wrap: wrap;">
                <label style="font-size: 11px;">
                  Cap: <input type="number" class="sector-cap" data-sector="${sid}" value="${(s.cap || 0.4) * 100}" min="5" max="100" step="5" style="width: 60px;"> %
                </label>
                <span style="font-size: 11px; color: var(--muted); flex: 1;">
                  ${coins || "(no coins)"}
                </span>
              </div>
            </div>
          `;
        }).join("")}
      </div>
      
      <div style="margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--border);">
        <h4 style="margin: 0 0 8px 0; font-size: 13px;">➕ 코인 섹터 변경</h4>
        <div style="display: flex; gap: 8px; flex-wrap: wrap;">
          <input type="text" id="sectorCoinInput" placeholder="BTCUSDT" style="width: 120px; padding: 4px 8px;">
          <select id="sectorSelect" style="padding: 4px 8px;">
            ${sectorIds.map(sid => `<option value="${sid}">${sectors[sid].name || sid}</option>`).join("")}
            <option value="OTHERS">OTHERS</option>
          </select>
          <button id="setSectorBtn" class="btn btn-primary" style="padding: 4px 12px;">Set</button>
        </div>
      </div>
      
      <div style="margin-top: 16px; display: flex; justify-content: flex-end; gap: 8px;">
        <button id="closeSectorModal" class="btn btn-outline">닫기</button>
        <button id="saveSectorMap" class="btn btn-primary">저장</button>
      </div>
      <div id="sectorModalMsg" style="margin-top: 8px; font-size: 11px; color: var(--muted);"></div>
    </div>
  `;

  document.body.appendChild(modal);

  // 이벤트 바인딩
  document.getElementById("closeSectorModal").addEventListener("click", () => modal.remove());
  modal.addEventListener("click", (e) => { if (e.target === modal) modal.remove(); });

  // 개별 코인 섹터 설정
  document.getElementById("setSectorBtn").addEventListener("click", async () => {
    const coin = document.getElementById("sectorCoinInput").value.trim().toUpperCase();
    const sector = document.getElementById("sectorSelect").value;
    const msgEl = document.getElementById("sectorModalMsg");

    if (!coin) {
      msgEl.textContent = "Enter symbol (e.g. BTCUSDT)";
      return;
    }

    try {
      const resp = await fetchJson(`/api/system/sector-map/coin?market=${encodeURIComponent(coin)}&sector=${encodeURIComponent(sector)}`, { method: "POST" });
      if (resp.ok) {
        msgEl.textContent = `✅ ${coin} → ${sector} 설정됨`;
        document.getElementById("sectorCoinInput").value = "";
      } else {
        msgEl.textContent = `❌ 오류: ${resp.error}`;
      }
    } catch (e) {
      msgEl.textContent = `❌ 오류: ${e}`;
    }
  });

  // 전체 저장
  document.getElementById("saveSectorMap").addEventListener("click", async () => {
    const msgEl = document.getElementById("sectorModalMsg");
    
    // Cap 값 수집
    const updatedSectors = JSON.parse(JSON.stringify(sectorData.sectors || {}));
    document.querySelectorAll(".sector-cap").forEach(el => {
      const sid = el.dataset.sector;
      const cap = parseFloat(el.value) / 100;
      if (updatedSectors[sid]) {
        updatedSectors[sid].cap = Math.max(0.05, Math.min(1.0, cap));
      }
    });

    try {
      const payload = {
        sectors: updatedSectors,
        default_sector: sectorData.default_sector || "OTHERS",
        default_cap: sectorData.default_cap || 0.4
      };
      const resp = await fetch("/api/system/sector-map", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const result = await resp.json();
      if (result.ok) {
        msgEl.textContent = `✅ 저장 완료 (${result.sectors_count} sectors)`;
      } else {
        msgEl.textContent = `❌ 오류: ${result.error}`;
      }
    } catch (e) {
      msgEl.textContent = `❌ 오류: ${e}`;
    }
  });
}

})(); // End IIFE
