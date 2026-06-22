/* ============================================================
 * Autocoin OS v3-H — OMA Command Center
 * Dashboard JS (Admin + AI/Risk minimal)
 * ============================================================
 * 목표
 * - OMA Admin Apply가 실제 POST를 발생시키고(서버 로그 확인)
 * - Active Prices ↔ OMA Managed Markets 선택이 연동되며
 * - AI/Risk 카드가 선택 마켓 기준으로 항상 표시되도록 한다.
 *console.log("[DASHBOARD] version=2026-01-06-fix16");

 * 설계 원칙
 * - UI는 "확인용". 과도한 기능 확장은 금지.
 * - 백엔드 계약이 흔들려도 버티도록(키 이름) 유연 파싱.
 * ============================================================ */

"use strict";

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
    // RSI 30 → 100%, RSI 70 → 0%, RSI 50 → 50%
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
    // momentum -5% → 75%, momentum +5% → 25%
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
  
  engineManualOrder: "/api/engine/manual/order",
  engineManualBatch: "/api/engine/manual/batch",

  // Reserved (candidate proposals)
  reservedList: "/api/reserved/list",
  reservedClear: "/api/reserved/clear",
  reservedHistoryClear: "/api/reserved/history/clear",
  reservedRefresh: (ppN, alN) => {
    const qs = new URLSearchParams();
    qs.set("pingpong_n", String(ppN ?? 3));
    qs.set("autoloop_n", String(alN ?? 3));
    return `/api/reserved/refresh?${qs.toString()}`;
  },
  reservedSettingsGet: "/api/reserved/settings",
  reservedSettingsSet: (opts) => {
    const o = (opts && typeof opts === "object") ? opts : {};
    const qs = new URLSearchParams();

    if (o.pingpong_n !== undefined) qs.set("pingpong_n", String(o.pingpong_n));
    if (o.autoloop_n !== undefined) qs.set("autoloop_n", String(o.autoloop_n));

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

  // LongHold (GAZUA/LADDER advisory)
  longholdSnapshot: "/api/ladder/longhold/snapshot",
  longholdList: "/api/ladder/longhold/list",
  longholdConfigGet: (mkt) => `/api/ladder/longhold/config?market=${encodeURIComponent(mkt)}`,
  longholdConfigSet: "/api/ladder/longhold/config",
  longholdRemove: (mkt) => `/api/ladder/longhold/remove?market=${encodeURIComponent(mkt)}`,
  longholdPoll: (mkt) => {
    const qs = new URLSearchParams();
    if (mkt) qs.set("market", String(mkt));
    return `/api/ladder/longhold/poll?${qs.toString()}`;
  },
  longholdCandidates: (strategy, n, method) => {
    const qs = new URLSearchParams();
    qs.set("strategy", String(strategy || "LADDER"));
    qs.set("n", String(n ?? 3));
    qs.set("method", String(method || "candles"));
    return `/api/ladder/longhold/candidates?${qs.toString()}`;
  },
};

const POLL_MS = 3000;

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
  // LongHold (GAZUA/LADDER)
  longhold: { snapshot: null, candidates: null, lastFetchMs: 0 },
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
          const b = (it && typeof it === 'object') ? (it.budget_usdt ?? it.budgetUsdt ?? it.budget) : null;
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

  const engineLabel = sys.engine_state || sys.engine || sys.name || "—";
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

    // -------------------------------------------------
    // UPSIDE POTENTIAL (상승 여력 게이지)
    // RSI + AI + Momentum 종합 → 0~100%
    // 0% = 과매수(고점) → 하락 임박
    // 100% = 과매도(저점) → 상승 여력 큼
    // -------------------------------------------------
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
        <span>Price <b>${fmtPrice(state.prices[mkt])}</b></span>
        <span>
          Warm-up <b>${r.ticks ?? 0}/${r.min_ticks ?? "—"}</b>
          <small>(min ${r.min_seconds ?? "—"}s)</small>
        </span>
      </div>

      <div class="meta">
        <span>Strategy <b>${stratName}</b></span>
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

    box.innerHTML = `
      <div class="price-mkt">${m}</div>
      <div class="price-val">${fmtPrice(p)}</div>
    `;

    box.addEventListener("click", () => selectMarket(m));
    grid.appendChild(box);
  });
}

/* =========================
 * RENDER: AI / Risk (selected market)
 * ========================= */
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

  const mkChk = (id, label, checked) => {
    return `<label class="gc-check"><input type="checkbox" id="${id}" ${checked ? "checked" : ""}/> <span>${escHtml(label)}</span></label>`;
  };

  const mkNum = (id, label, value, step = "0.01") => {
    return `<div class="gc-field"><label for="${id}">${escHtml(label)}</label><input id="${id}" type="number" step="${step}" value="${escHtml(valNum(value, ""))}" /></div>`;
  };

  
  const mkSel = (id, label, value, options) => {
    const v = (value === undefined || value === null) ? "" : String(value);
    const opts = (options || []).map((o) => {
      const ov = String(o.value);
      const sel = (ov === v) ? " selected" : "";
      return `<option value="${escHtml(ov)}"${sel}>${escHtml(o.label)}</option>`;
    }).join("");
    return `<div class="gc-field"><label for="${id}">${escHtml(label)}</label><select id="${id}">${opts}</select></div>`;
  };

// =========================
  // Global (system-level) controls
  // =========================
  // NOTE: Do NOT rebuild control DOM while the user is typing.
  if (!isEditing) {
  boxControls.innerHTML = `
    <div class="market-guard-panel" style="border-top:none; padding-top:0; margin-top:0;">
      <div class="market-guard-head">
        <div>
          <div class="title">Global Defaults</div>
          <div class="sub">ENV is baseline; dashboard overrides persist across restarts.</div>
        </div>
        <div class="market-guard-actions" style="margin-top:0;">
          <button class="btn" id="applyGuards">Apply</button>
          <button class="btn btn-ghost" id="clearGlobalEntryCooldown">Clear BUY cooldown</button>
        </div>
      </div>

      <div style="display:flex; flex-wrap:wrap; gap:12px; margin-top:8px;">
        ${mkChk("g_exit_profit_guard", "Exit Profit Guard", !!g.exit_profit_guard)}
        ${mkChk("g_entry_ob_guard", "Entry OB Guard", !!g.entry_ob_guard_enabled)}
        ${mkChk("g_entry_ceiling_guard", "Entry Ceiling Guard", !!g.entry_ceiling_guard)}
        ${mkChk("g_entry_qty_guard", "Entry Qty Guard", !!g.entry_qty_guard)}
        ${mkChk("g_drawdown", "Drawdown Guard", !!g.drawdown_guard)}
        ${mkChk("g_tp_limit", "TP Limit Exit", !!g.tp_limit_exit_enabled)}
        ${mkChk("g_entry_limit_buy", "Entry Limit Buy", !!g.entry_limit_buy_enabled)}
        ${mkChk("g_wallet_mode", "Wallet Mode", !!g.wallet_mode)}
      </div>

      <div class="market-guard-grid" style="margin-top:10px;">
        ${mkNum("g_min_order_usdt", "Min Order (USDT)", g.min_order_usdt, "1000")}
        ${mkNum("g_entry_max_qty", "Entry Max Qty", g.entry_max_qty, "0.0001")}
        ${mkNum("g_entry_qty_cooldown_sec", "Qty Cooldown (sec)", g.entry_qty_cooldown_sec, "0.1")}

        ${mkNum("g_entry_ob_max_spread_bps", "OB Max Spread (bps)", g.entry_ob_max_spread_bps, "0.1")}
        ${mkNum("g_entry_ob_depth_factor", "OB Depth Factor (x)", g.entry_ob_depth_factor, "0.01")}
        ${mkNum("g_entry_ob_depth_bps", "OB Depth Range (bps)", g.entry_ob_depth_bps, "1")}
        ${mkNum("g_entry_ob_stale_sec", "OB Stale (sec)", g.entry_ob_stale_sec, "0.1")}

        ${mkNum("g_entry_ceiling_extra_bps", "Ceiling Extra (bps)", g.entry_ceiling_extra_bps, "0.1")}
        ${mkNum("g_entry_ceiling_cooldown_sec", "Ceiling Cooldown (sec)", g.entry_ceiling_cooldown_sec, "0.1")}
            ${mkNum("g_entry_ceiling_max_age_sec", "Ceiling Ignore After (sec)", g.entry_ceiling_max_age_sec, "1")}
            ${mkSel("g_entry_ceiling_decay_mode", "Ceiling Decay Mode", (g.entry_ceiling_decay_mode || "EXP"), [{value:"NONE",label:"OFF (NONE)"},{value:"LINEAR",label:"LINEAR"},{value:"EXP",label:"EXP"}])}
            ${mkNum("g_entry_ceiling_decay_half_life_sec", "Ceiling Half-life (sec)", g.entry_ceiling_decay_half_life_sec, "1")}

        ${mkNum("g_exit_min_net_profit_pct", "Exit Min Profit (%)", g.exit_min_net_profit_pct, "0.01")}
        ${mkNum("g_exit_min_net_profit_usdt", "Exit Min Profit (USDT)", g.exit_min_net_profit_usdt, "100")}
        ${mkNum("g_exit_slippage_guard_bps", "Exit Slip Guard (bps)", g.exit_slippage_guard_bps, "0.1")}
        ${mkNum("g_exit_fee_rate", "Exit Fee Rate", g.exit_fee_rate, "0.0001")}

        ${mkNum("g_tp_limit_timeout_sec", "TP Timeout (sec)", g.tp_limit_timeout_sec, "0.1")}
        ${mkNum("g_tp_limit_max_retries", "TP Max Retries", g.tp_limit_max_retries, "1")}

        ${mkNum("g_entry_limit_timeout_sec", "Entry Limit Timeout (sec)", g.entry_limit_timeout_sec, "0.1")}
        ${mkNum("g_entry_limit_cooldown_sec", "Entry Limit Cooldown (sec)", g.entry_limit_cooldown_sec, "0.1")}
        ${mkSel("g_entry_limit_price_mode", "Entry Limit Price Mode", (g.entry_limit_price_mode || "best_bid"), [{value:"best_bid",label:"Best Bid"},{value:"best_ask",label:"Best Ask"}])}

        ${mkNum("g_entry_global_gap_sec", "Entry Global Gap (sec)", g.entry_global_gap_sec, "0.1")}
        ${mkNum("g_max_pending_orders_total", "Max Pending Orders", g.max_pending_orders_total, "1")}
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

  qs("applyGuards")?.addEventListener("click", async () => {
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

    try {
      const resp = await fetchJson(`${API.systemGuardsSet}?${qs2.toString()}`, { method: "POST" });
      if (resp.ok) {
        boxMsg.textContent = `Guards updated @ ${new Date().toLocaleTimeString()}`;
        await refreshGuards();
        renderGuardsPanel();
      } else {
        boxMsg.textContent = `Error: ${resp.error || "unknown"}`;
      }
    } catch (e) {
      boxMsg.textContent = `Error: ${e}`;
    }
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
          <div class="market-guard-head">
            <div>
              <div class="title">Market Overrides</div>
              <div class="sub">${escHtml(m)} · ${hasActiveOv() ? "overrides active" : "no overrides"}</div>
            </div>
            <div class="market-guard-actions" style="margin-top:0;">
              <button class="btn" id="applyMarketGuards">Apply to Market</button>
              <button class="btn btn-ghost" id="resetMarketGuards">Reset to Global</button>
            </div>
          </div>

          <div style="display:flex; flex-wrap:wrap; gap:12px; margin-top:8px;">
            ${mkChk("mg_entry_enabled", "Entry Enabled", entryEnabled)}
            ${mkChk("mg_entry_ob_guard", "Entry OB Guard", effBool("entry_ob_guard_enabled", g.entry_ob_guard_enabled))}
            ${mkChk("mg_entry_ceiling_guard", "Entry Ceiling Guard", effBool("entry_ceiling_guard", g.entry_ceiling_guard))}
            ${mkChk("mg_entry_qty_guard", "Entry Qty Guard", effBool("entry_qty_guard", g.entry_qty_guard))}
            ${mkChk("mg_exit_profit_guard", "Exit Profit Guard", effBool("exit_profit_guard", g.exit_profit_guard))}
            ${mkChk("mg_tp_limit", "TP Limit Exit", effBool("tp_limit_exit_enabled", g.tp_limit_exit_enabled))}
          </div>

          <div class="market-guard-grid" style="margin-top:10px;">
            ${mkNum("mg_entry_ob_max_spread_bps", "OB Max Spread (bps)", effNum("entry_ob_max_spread_bps", g.entry_ob_max_spread_bps), "0.1")}
            ${mkNum("mg_entry_ob_depth_factor", "OB Depth Factor (x)", effNum("entry_ob_depth_factor", g.entry_ob_depth_factor), "0.01")}
            ${mkNum("mg_entry_ob_depth_bps", "OB Depth Range (bps)", effNum("entry_ob_depth_bps", g.entry_ob_depth_bps), "1")}
            ${mkNum("mg_entry_ob_stale_sec", "OB Stale (sec)", effNum("entry_ob_stale_sec", g.entry_ob_stale_sec), "0.1")}

            ${mkNum("mg_entry_max_qty", "Entry Max Qty", effNum("entry_max_qty", g.entry_max_qty), "0.0001")}
            ${mkNum("mg_entry_qty_cooldown_sec", "Qty Cooldown (sec)", effNum("entry_qty_cooldown_sec", g.entry_qty_cooldown_sec), "0.1")}

            ${mkNum("mg_entry_ceiling_extra_bps", "Ceiling Extra (bps)", effNum("entry_ceiling_extra_bps", g.entry_ceiling_extra_bps), "0.1")}
            ${mkNum("mg_entry_ceiling_cooldown_sec", "Ceiling Cooldown (sec)", effNum("entry_ceiling_cooldown_sec", g.entry_ceiling_cooldown_sec), "0.1")}
            ${mkNum("mg_entry_ceiling_max_age_sec", "Ceiling Ignore After (sec)", effNum("entry_ceiling_max_age_sec", g.entry_ceiling_max_age_sec), "1")}
            ${mkSel("mg_entry_ceiling_decay_mode", "Ceiling Decay Mode", (ov.entry_ceiling_decay_mode ?? ""), [{value:"",label:"(inherit)"},{value:"NONE",label:"OFF (NONE)"},{value:"LINEAR",label:"LINEAR"},{value:"EXP",label:"EXP"}])}
            ${mkNum("mg_entry_ceiling_decay_half_life_sec", "Ceiling Half-life (sec)", effNum("entry_ceiling_decay_half_life_sec", g.entry_ceiling_decay_half_life_sec), "1")}

            ${mkNum("mg_exit_min_net_profit_pct", "Exit Min Profit (%)", effNum("exit_min_net_profit_pct", g.exit_min_net_profit_pct), "0.01")}
            ${mkNum("mg_exit_min_net_profit_usdt", "Exit Min Profit (USDT)", effNum("exit_min_net_profit_usdt", g.exit_min_net_profit_usdt), "100")}
            ${mkNum("mg_exit_slippage_guard_bps", "Exit Slip Guard (bps)", effNum("exit_slippage_guard_bps", g.exit_slippage_guard_bps), "0.1")}

            ${mkNum("mg_tp_limit_timeout_sec", "TP Timeout (sec)", effNum("tp_limit_timeout_sec", g.tp_limit_timeout_sec), "0.1")}
            ${mkNum("mg_tp_limit_max_retries", "TP Max Retries", effNum("tp_limit_max_retries", g.tp_limit_max_retries), "1")}
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
    return "BUY";
  };

  const mapExitCause = (exitState, exitReason) => {
    if (String(exitState || "").toUpperCase() !== "BLOCKED") return "";
    const r = String(exitReason || "").toLowerCase();
    if (r.includes("order_pending")) return "PND";
    if (r.includes("exit_cooldown")) return "XCD";
    if (r.includes("no_position")) return "NCP";
    if (r.includes("profit_guard")) return "PFT";
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
    } else if (stratMode === "GAZUA") {
      const mm = (sMeta && typeof sMeta === "object") ? sMeta : null;
      
      const aiScore = nVal(mm?.ai_score);
      const aiThreshold = nVal(mm?.ai_threshold) ?? 0.65;
      const profitPct = nVal(mm?.profit_pct);
      const tpPct = nVal(mm?.tp_pct) ?? 15;
      const slPct = nVal(mm?.sl_pct) ?? -10;
      const buyNow = mm?.buy_now === true;
      const holdSell = mm?.hold_sell === true;
      
      const hasPos = posQty > 0;
      const condAi = (aiScore !== null) ? (aiScore >= aiThreshold) : null;
      const condTp = (profitPct !== null && tpPct) ? (profitPct >= tpPct) : null;
      const condSl = (profitPct !== null && slPct) ? (profitPct <= slPct) : null;
      
      addMeta(`AI <b>${aiScore !== null ? (aiScore * 100).toFixed(0) + "%" : "—"}</b>/<b>${(aiThreshold * 100).toFixed(0)}%</b>`);
      addMeta(`PnL <b>${profitPct !== null ? fmtNum(profitPct, 2) + "%" : "—"}</b>`);
      addMeta(`TP/SL <b>${tpPct}%</b>/<b>${slPct}%</b>`);
      
      sigLamps.push(
        mkLampBox(m, "SIG", "POS",
          hasPos ? "pass" : "off",
          `Position: ${hasPos ? "YES" : "NO"}\nqty=${posQty}`,
          false
        )
      );
      sigLamps.push(
        mkLampBox(m, "SIG", "AI",
          (condAi === null) ? "off" : (condAi ? "pass" : "block"),
          `AI Score: ai_score >= threshold\nai_score=${aiScore !== null ? (aiScore * 100).toFixed(1) + "%" : "—"}\nthreshold=${(aiThreshold * 100).toFixed(0)}%`,
          false
        )
      );
      sigLamps.push(
        mkLampBox(m, "SIG", "TP",
          (condTp === null) ? "off" : (condTp ? "pass" : "block"),
          `Take Profit: profit >= tp_pct\nprofit=${profitPct !== null ? fmtNum(profitPct, 2) + "%" : "—"}\ntp_pct=${tpPct}%`,
          false
        )
      );
      sigLamps.push(
        mkLampBox(m, "SIG", "SL",
          (condSl === null) ? "off" : (condSl ? "block" : "pass"),
          `Stop Loss: profit <= sl_pct\nprofit=${profitPct !== null ? fmtNum(profitPct, 2) + "%" : "—"}\nsl_pct=${slPct}%`,
          false
        )
      );
      sigLamps.push(
        mkLampBox(m, "SIG", "BUY",
          buyNow ? "pass" : "off",
          `Buy Now signal: ${buyNow ? "YES" : "NO"}`,
          false
        )
      );
      sigLamps.push(
        mkLampBox(m, "SIG", "SELL",
          holdSell ? "pass" : "off",
          `Hold Sell signal: ${holdSell ? "YES" : "NO"}`,
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

    title.innerHTML = `
      <div class="title-left">
        <span class="badge">${escHtml(m)}</span>
        <span class="state ${marketState === "ACTIVE" ? "ok" : (marketState === "RECOVERY" ? "bad" : "")}">${escHtml(marketState)}</span>
        <span class="mini">${stEnabled ? "STRAT: " + escHtml(strat) : "STRAT: OFF"}</span>
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

function fmtUsdt(v) {
  const n = numOr(v, 0);
  return new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 0 }).format(n);
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
    for (const it of snapActive) {
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

    const equity = cash + posValue;

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
        <div>Total: <span class="${pnlClass(totalPnl)}">$${fmtUsdt(totalPnl)}</span> (${fmtPct(totalPct)})
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
                <td>$${fmtUsdt(r.allocated_usdt)}</td>
                <td>$${fmtUsdt(r.equity_usdt)}</td>
                <td class="${cls}">$${fmtUsdt(r.pnl_usdt)}</td>
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
        renderMarketPnl();
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
        renderMarketPnl();
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
                <td>$${fmtUsdt(r.allocated_usdt)}</td>
                <td>$${fmtUsdt(r.equity_usdt)}</td>
                <td class="${cls}">$${fmtUsdt(r.pnl_usdt)}</td>
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

    setAdminMsg(`OK: ${m} -> ${s}`, true);

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
        // Trend pullback tactic (BULL regime)-완만한 상승장 진입용이하게 함
        pb_enabled: true,
        pb_rsi_min: 38,
        // pb_rsi_max: 55,
        // pb_dev_min_pct: 0.15,
        pb_dev_max_pct: 0.8,
        pb_slope_bars: 5,
        pb_min_slope_pct: 0.05,
        pb_macd_floor: 0.0,
        // pb_z_buy: 0.6,
        // pb_require_bounce: true,
        pb_rsi_max: 60,
        pb_z_buy: 0.3,
        pb_dev_min_pct: 0.10,
        pb_require_bounce: false,
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

  // Auto Sync (periodic reconcile)
  // - 휴대폰 앱 등 외부에서 발생한 매수/매도를 주기적으로 반영( Reconcile )
  // - OFF가 기본이며, ON일 때만 동작합니다.
  const autoSyncEl = qs("manualAutoSync");
  const autoSyncIntervalEl = qs("manualAutoSyncInterval");
  const LS_MANUAL_AUTOSYNC = "nunnaya_manual_autosync_v1"; // { enabled: bool, interval: number(sec) }

  let autoSyncTimer = null;
  let autoSyncInFlight = false;

  const stopAutoSync = () => {
    if (autoSyncTimer) {
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

    autoSyncTimer = setInterval(() => {
      if (autoSyncEl && autoSyncEl.checked) runAutoSyncOnce();
    }, interval * 1000);

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
        manualValueEl.min = "5000";
        manualValueEl.removeAttribute("max");
        manualValueEl.step = "5000";
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
        batchValueEl.min = "5000";
        batchValueEl.removeAttribute("max");
        batchValueEl.step = "5000";
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
        unit === "pct" ? `${value}%` : `$${fmtUsdt(value)}`;

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
        unit === "pct" ? `${value}%` : `$${fmtUsdt(value)}`;

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
 * RESERVED QUEUE (Market candidates)
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
  if (nPP && settings.pingpong_n !== undefined) nPP.value = String(settings.pingpong_n);
  if (nAL && settings.autoloop_n !== undefined) nAL.value = String(settings.autoloop_n);

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

  const apEnabled = qs("reservedAutoPilotEnabled");
  const aa = qs("reservedAutoApprove");

  const prom = qs("reservedPromoteActive");
  const ab = qs("reservedApplyBudget");

  const wEn = qs("reservedWindowEnabled");
  const wStart = qs("reservedWindowStart");
  const wEnd = qs("reservedWindowEnd");

  const rNoFills = qs("reservedRuleNoFills");
  const idleMin = qs("reservedIdleMin");

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

  const pp = nPP ? Number(nPP.value) : 3;
  const al = nAL ? Number(nAL.value) : 3;

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

  const windowEnabled = wEn ? !!wEn.checked : false;
  const windowStart = wStart ? String(wStart.value || "") : "";
  const windowEnd = wEnd ? String(wEnd.value || "") : "";

  const idleEnabled = rNoFills ? !!rNoFills.checked : (Number.isFinite(idle) ? idle > 0 : true);
  const guardsEnabled = rGuards ? !!rGuards.checked : false;
  const sigEnabled = rSig ? !!rSig.checked : false;

  return {
    pingpong_n: Number.isFinite(pp) ? pp : 3,
    autoloop_n: Number.isFinite(al) ? al : 3,

    autopilot_enabled: autopilotEnabled,
    auto_approve: autoApprove,

    promote_to_active: promoteToActive,
    apply_suggested_budget: applySuggestedBudget,

    window_enabled: windowEnabled,
    window_start: windowStart,
    window_end: windowEnd,

    idle_demote_enabled: idleEnabled,
    idle_demote_min: Number.isFinite(idle) ? idle : 180,

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
    left.innerHTML = `<span class="mkt">${escHtml(market)}</span><span class="strategy">${escHtml(strat)}</span>${escHtml(nameKr)}`;

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
      `Budget <b>$${fmtUsdt(budget)}</b>` +
      ` · Spread <b>${Number.isFinite(spread) ? fmtNum(spread, 1) : "—"} bps</b>` +
      ` · Depth <b>$${fmtUsdt(depth)}</b>` +
      ` · Vol24 <b>$${fmtUsdt(vol24)}</b>` +
      ` · Range <b>${Number.isFinite(rr) ? fmtNum(rr * 100, 2) : "—"}%</b>` +
      (trades !== null && trades !== undefined ? ` · Trades(${meta?.summary?.params?.recent_minutes ?? "N"}m) <b>${trades}</b>` : "");

    row.appendChild(top);
    row.appendChild(metaLine);

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
      const pp = nPP ? Number(nPP.value) : 3;
      const al = nAL ? Number(nAL.value) : 3;

      try {
        if (sumEl) sumEl.textContent = "Scanning…";
        setAdminMsg("reserved scan running…", true);

        const data = await fetchJson(API.reservedRefresh(pp, al), { method: "POST" });
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

/* =========================
 * LONGHOLD (GAZUA / LADDER)
 * ========================= */

const LONGHOLD_REFRESH_MS = 10000; // throttle (refreshGlobalState runs every 3s)

function setLongholdMsg(msg) {
  const el = qs("lhMsg");
  if (!el) return;
  el.textContent = msg || "";
}

function fmtMaybeUsdt(n) {
  if (n === undefined || n === null || Number.isNaN(Number(n))) return "—";
  return fmtUsdt(Number(n));
}

function fmtMaybeNum(n, digits = 0) {
  if (n === undefined || n === null || Number.isNaN(Number(n))) return "—";
  return Number(n).toFixed(digits);
}

function tagForLongholdStatus(st) {
  const s = String(st || "").toUpperCase();
  if (s === "OK") return { cls: "lh-tag ok", label: "OK" };
  if (s === "DUST") return { cls: "lh-tag warn", label: "DUST" };
  if (s === "NO_POSITION") return { cls: "lh-tag warn", label: "NO_POS" };
  if (s === "NO_PRICE") return { cls: "lh-tag warn", label: "NO_PRICE" };
  if (s === "DISABLED") return { cls: "lh-tag", label: "OFF" };
  return { cls: "lh-tag", label: s || "—" };
}

function fillLongholdForm(item) {
  const mEl = qs("lhMarket");
  const sEl = qs("lhStrategy");
  const tEl = qs("lhTargetPct");
  const bEl = qs("lhBudgetUsdt");
  const enEl = qs("lhEnabled");
  const repEl = qs("lhRepeat");
  const cdEl = qs("lhCooldownSec");
  const mpEl = qs("lhMinPosUsdt");
  const noteEl = qs("lhNote");

  if (mEl) mEl.value = item?.market || "";
  if (sEl && item?.strategy) sEl.value = String(item.strategy).toUpperCase();
  if (tEl) tEl.value = item?.target_profit_pct ?? "";
  if (bEl) bEl.value = item?.budget_usdt ?? "";
  if (enEl) enEl.checked = item?.enabled !== false;
  if (repEl) repEl.checked = item?.repeat !== false;
  if (cdEl) cdEl.value = item?.notify_cooldown_sec ?? "";
  if (mpEl) mpEl.value = item?.min_position_usdt ?? "";
  if (noteEl) noteEl.value = item?.note ?? "";
}

function renderLongholdPanel() {
  const wrap = qs("lhListWrap");
  const sumEl = qs("lhSummaryText");
  if (!wrap) return;

  const snap = state.longhold?.snapshot;
  const items = Array.isArray(snap?.items) ? snap.items : [];
  const totals = snap?.totals || {};

  // summary
  if (sumEl) {
    const n = items.length;
    const pos = totals?.position_usdt;
    const pnl = totals?.profit_usdt;
    const budget = totals?.budget_usdt;
    const parts = [`items ${n}`];
    if (budget) parts.push(`budget $${fmtUsdt(budget)}`);
    if (pos) parts.push(`pos $${fmtUsdt(pos)}`);
    if (pnl !== undefined && pnl !== null) parts.push(`pnl $${fmtUsdt(pnl)}`);
    sumEl.textContent = parts.join(" · ");
  }

  clear(wrap);
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "LongHold 데이터가 아직 없습니다.";
    wrap.appendChild(empty);
    return;
  }

  const omaSet = new Set([
    ...((state.oma && Array.isArray(state.oma.active)) ? state.oma.active : []),
    ...((state.oma && Array.isArray(state.oma.watch)) ? state.oma.watch : []),
    ...((state.oma && Array.isArray(state.oma.recovery)) ? state.oma.recovery : []),
  ].map(x => String(x).toUpperCase()));

  const lhSet = new Set(items.map(it => String(it.market || "").toUpperCase()));

  const tbl = document.createElement("table");
  tbl.className = "tbl";

  const thead = document.createElement("thead");
  thead.innerHTML = `
    <tr>
      <th>Market</th>
      <th>Status</th>
      <th>Strategy</th>
      <th>Budget</th>
      <th>Pos(USDT)</th>
      <th>Entry</th>
      <th>Price</th>
      <th>PnL</th>
      <th>PnL%</th>
      <th>Target%</th>
      <th class="right">Actions</th>
    </tr>`;
  tbl.appendChild(thead);

  const tbody = document.createElement("tbody");

  for (const it of items) {
    const market = String(it.market || "").toUpperCase();
    const dup = omaSet.has(market);

    const tr = document.createElement("tr");
    if (dup) tr.classList.add("lh-dup");

    const stTag = tagForLongholdStatus(it.status);
    const statusHtml = `<span class="${stTag.cls}">${escHtml(stTag.label)}</span>` + (dup ? ` <span class="lh-tag warn">DUP</span>` : "");

    const pnlUsdt = (it.profit_usdt !== undefined && it.profit_usdt !== null) ? Number(it.profit_usdt) : null;
    const pnlPct = (it.profit_pct !== undefined && it.profit_pct !== null) ? Number(it.profit_pct) : null;
    const pnlCls = pnlUsdt === null ? "" : (pnlUsdt >= 0 ? "ok" : "bad");

    tr.innerHTML = `
      <td><b>${escHtml(market || "—")}</b></td>
      <td>${statusHtml}</td>
      <td>${escHtml(String(it.strategy || "—").toUpperCase())}</td>
      <td>${fmtMaybeUsdt(it.budget_usdt)}</td>
      <td>${fmtMaybeUsdt(it.position_usdt)}</td>
      <td>${it.entry ? escHtml(fmtUsdt(it.entry)) : "—"}</td>
      <td>${it.price ? escHtml(fmtUsdt(it.price)) : "—"}</td>
      <td><span class="${pnlCls}">${pnlUsdt === null ? "—" : escHtml(fmtUsdt(pnlUsdt))}</span></td>
      <td><span class="${pnlCls}">${pnlPct === null ? "—" : escHtml(pnlPct.toFixed(3) + "%")}</span></td>
      <td>${it.target_profit_pct !== undefined && it.target_profit_pct !== null ? escHtml(String(it.target_profit_pct)) + "%" : "—"}</td>
      <td class="right lh-row-actions"></td>
    `;

    const actionsTd = tr.querySelector("td.right");
    if (actionsTd) {
      const editBtn = document.createElement("button");
      editBtn.className = "btn btn-ghost";
      editBtn.textContent = "Edit";
      editBtn.addEventListener("click", () => {
        fillLongholdForm(it);
        setLongholdMsg(`loaded ${market}`);
      });

      const pollBtn = document.createElement("button");
      pollBtn.className = "btn btn-ghost";
      pollBtn.textContent = "Poll";
      pollBtn.addEventListener("click", async () => {
        try {
          await fetchJson(API.longholdPoll(market), { method: "POST" });
          setLongholdMsg(`poll ok: ${market}`);
        } catch (e) {
          console.error(e);
          setLongholdMsg(`poll failed: ${e?.message || e}`);
        }
      });

      const delBtn = document.createElement("button");
      delBtn.className = "btn btn-danger";
      delBtn.textContent = "Del";
      delBtn.addEventListener("click", async () => {
        if (!confirm(`Remove ${market} from LongHold?`)) return;
        try {
          await fetchJson(API.longholdRemove(market), { method: "POST" });
          setLongholdMsg(`removed: ${market}`);
          await refreshLongholdSnapshot(true);
        } catch (e) {
          console.error(e);
          setLongholdMsg(`remove failed: ${e?.message || e}`);
        }
      });

      actionsTd.appendChild(editBtn);
      actionsTd.appendChild(pollBtn);
      actionsTd.appendChild(delBtn);
    }

    tbody.appendChild(tr);
  }

  tbl.appendChild(tbody);
  wrap.appendChild(tbl);
}

function renderLongholdCandidates() {
  const wrap = qs("lhCandWrap");
  if (!wrap) return;
  clear(wrap);

  const data = state.longhold?.candidates;
  const items = Array.isArray(data?.items) ? data.items : (Array.isArray(data) ? data : []);
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "empty";
    empty.textContent = "Scan 결과가 아직 없습니다.";
    wrap.appendChild(empty);
    return;
  }

  const tbl = document.createElement("table");
  tbl.className = "tbl";
  const thead = document.createElement("thead");
  thead.innerHTML = `
    <tr>
      <th>Market</th>
      <th>Score</th>
      <th>Reason</th>
      <th class="right">Actions</th>
    </tr>`;
  tbl.appendChild(thead);

  const tbody = document.createElement("tbody");
  for (const it of items) {
    const market = String(it.market || it.mkt || "").toUpperCase();
    const score = (it.score !== undefined && it.score !== null) ? Number(it.score) : null;
    const reason = String(it.reason || it.note || "").slice(0, 80);

    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><b>${escHtml(market || "—")}</b></td>
      <td>${score === null ? "—" : escHtml(score.toFixed(3))}</td>
      <td>${escHtml(reason || "—")}</td>
      <td class="right lh-row-actions"></td>
    `;
    const actionsTd = tr.querySelector("td.right");
    if (actionsTd) {
      const useBtn = document.createElement("button");
      useBtn.className = "btn btn-ghost";
      useBtn.textContent = "Use";
      useBtn.addEventListener("click", () => {
        const mEl = qs("lhMarket");
        if (mEl) mEl.value = market;
        setLongholdMsg(`selected candidate: ${market}`);
      });
      const addBtn = document.createElement("button");
      addBtn.className = "btn";
      addBtn.textContent = "Save";
      addBtn.addEventListener("click", async () => {
        try {
          const payload = readLongholdFormPayload(market);
          await fetchJson(API.longholdConfigSet, { method: "POST", body: JSON.stringify(payload) });
          setLongholdMsg(`saved: ${market}`);
          await refreshLongholdSnapshot(true);
        } catch (e) {
          console.error(e);
          setLongholdMsg(`save failed: ${e?.message || e}`);
        }
      });
      actionsTd.appendChild(useBtn);
      actionsTd.appendChild(addBtn);
    }
    tbody.appendChild(tr);
  }
  tbl.appendChild(tbody);
  wrap.appendChild(tbl);
}

function readLongholdFormPayload(overrideMarket) {
  const market = (overrideMarket || qs("lhMarket")?.value || "").trim().toUpperCase();
  const strategy = (qs("lhStrategy")?.value || "LADDER").trim().toUpperCase();
  const enabled = qs("lhEnabled") ? !!qs("lhEnabled").checked : true;
  const repeat = qs("lhRepeat") ? !!qs("lhRepeat").checked : true;

  const tp = qs("lhTargetPct")?.value;
  const budget = qs("lhBudgetUsdt")?.value;
  const cd = qs("lhCooldownSec")?.value;
  const mp = qs("lhMinPosUsdt")?.value;
  const note = qs("lhNote")?.value;

  const payload = {
    market,
    enabled,
    strategy,
  };
  if (tp !== undefined && tp !== null && String(tp).trim() !== "") payload.target_profit_pct = Number(tp);
  if (budget !== undefined && budget !== null && String(budget).trim() !== "") payload.budget_usdt = Number(budget);
  if (cd !== undefined && cd !== null && String(cd).trim() !== "") payload.notify_cooldown_sec = Number(cd);
  if (mp !== undefined && mp !== null && String(mp).trim() !== "") payload.min_position_usdt = Number(mp);
  if (repeat !== undefined && repeat !== null) payload.repeat = repeat;
  if (note !== undefined && note !== null && String(note).trim() !== "") payload.note = String(note).trim();

  return payload;
}

async function refreshLongholdSnapshot(force) {
  const panel = qs("longholdPanel");
  if (!panel) return;

  const now = Date.now();
  if (!force && state.longhold && (now - (state.longhold.lastFetchMs || 0)) < LONGHOLD_REFRESH_MS) return;
  state.longhold.lastFetchMs = now;

  try {
    const data = await fetchJson(API.longholdSnapshot);
    state.longhold.snapshot = data && (data.snapshot || data);
    renderLongholdPanel();
  } catch (e) {
    console.error(e);
    setLongholdMsg(`snapshot error: ${e?.message || e}`);
  }
}

function bindLongholdControls() {
  const marketEl = qs("lhMarket");
  if (marketEl) {
    // attach datalist (if present)
    try { marketEl.setAttribute("list", "marketDatalist"); } catch (_) {}
  }

  const loadBtn = qs("lhLoadBtn");
  const upsertBtn = qs("lhUpsertBtn");
  const removeBtn = qs("lhRemoveBtn");
  const pollBtn = qs("lhPollBtn");
  const scanLadderBtn = qs("lhScanLadderBtn");
  const scanGazuaBtn = qs("lhScanGazuaBtn");

  if (loadBtn) {
    loadBtn.addEventListener("click", async () => {
      const mkt = (marketEl?.value || "").trim().toUpperCase();
      if (!mkt) return setLongholdMsg("market required");
      try {
        const data = await fetchJson(API.longholdConfigGet(mkt));
        const cfg = (data && (data.config || data.cfg || data)) || {};
        fillLongholdForm({ market: mkt, ...cfg });
        setLongholdMsg(`loaded: ${mkt}`);
      } catch (e) {
        console.error(e);
        setLongholdMsg(`load failed: ${e?.message || e}`);
      }
    });
  }

  if (upsertBtn) {
    upsertBtn.addEventListener("click", async () => {
      const mkt = (marketEl?.value || "").trim().toUpperCase();
      if (!mkt) return setLongholdMsg("market required");
      try {
        const payload = readLongholdFormPayload(mkt);
        await fetchJson(API.longholdConfigSet, { method: "POST", body: JSON.stringify(payload) });
        setLongholdMsg(`saved: ${mkt}`);
        await refreshLongholdSnapshot(true);
      } catch (e) {
        console.error(e);
        setLongholdMsg(`save failed: ${e?.message || e}`);
      }
    });
  }

  if (removeBtn) {
    removeBtn.addEventListener("click", async () => {
      const mkt = (marketEl?.value || "").trim().toUpperCase();
      if (!mkt) return setLongholdMsg("market required");
      if (!confirm(`Remove ${mkt} from LongHold?`)) return;
      try {
        await fetchJson(API.longholdRemove(mkt), { method: "POST" });
        setLongholdMsg(`removed: ${mkt}`);
        await refreshLongholdSnapshot(true);
      } catch (e) {
        console.error(e);
        setLongholdMsg(`remove failed: ${e?.message || e}`);
      }
    });
  }

  if (pollBtn) {
    pollBtn.addEventListener("click", async () => {
      const mkt = (marketEl?.value || "").trim().toUpperCase();
      try {
        await fetchJson(API.longholdPoll(mkt || ""), { method: "POST" });
        setLongholdMsg(mkt ? `poll ok: ${mkt}` : "poll ok");
      } catch (e) {
        console.error(e);
        setLongholdMsg(`poll failed: ${e?.message || e}`);
      }
    });
  }

  const doScan = async (strategy) => {
    const nEl = qs("lhCandN");
    const methodEl = qs("lhCandMethod");
    const n = nEl ? Number(nEl.value) : 5;
    const method = methodEl ? String(methodEl.value) : "candles";
    try {
      setLongholdMsg(`scan ${strategy}…`);
      const data = await fetchJson(API.longholdCandidates(strategy, n, method));
      state.longhold.candidates = data && (data.candidates || data);
      renderLongholdCandidates();
      setLongholdMsg(`scan done: ${strategy}`);
    } catch (e) {
      console.error(e);
      setLongholdMsg(`scan failed: ${e?.message || e}`);
    }
  };

  if (scanLadderBtn) scanLadderBtn.addEventListener("click", () => doScan("LADDER"));
  if (scanGazuaBtn) scanGazuaBtn.addEventListener("click", () => doScan("GAZUA"));

  // initial render (if any)
  try { renderLongholdPanel(); } catch (_) {}
  try { renderLongholdCandidates(); } catch (_) {}
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

  // LongHold panel (throttled)
  try {
    await refreshLongholdSnapshot(false);
  } catch (e) {
    console.error(e);
  }
}

async function init() {
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
  safe(bindLongholdControls, "bindLongholdControls");
  
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
      // Keep this simple; any error path will overwrite it.
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
  setInterval(() => {
    pollTick("poll");
  }, POLL_MS);
}

document.addEventListener("DOMContentLoaded", () => {
  try {
    init();
  } catch (e) {
    console.error(e);
    try { setAdminMsg(`init error: ${e?.message || e}`, false); } catch (_) {}
  }
});
