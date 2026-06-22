/* Depends on: utils.js (window.AutocoinUtils) */
"use strict";

document.addEventListener("DOMContentLoaded", function() {
  const { IntervalManager, formatUSDT } = window.AutocoinUtils || {};
  const i18n = window.AutocoinEmbedI18n || {};
  const t = i18n.t || ((_, fallback = "") => fallback || "");
  const tf = i18n.tf || ((_, __, fallback = "") => fallback || "");
  const qs = (s) => document.querySelector(s);
  const urlParams = new URLSearchParams(window.location.search);
  const MARKET = (urlParams.get("market") || "").toUpperCase();

  if (typeof i18n.initLanguage === "function") {
    i18n.initLanguage("ko");
  }

  if (!MARKET) {
    alert(t("market_detail.no_market_specified", "No market specified"));
    window.location.href = "/ui/dashboard_v2.html";
    return;
  }

  function updatePageTitle() {
    document.title = tf("market_detail.doc_title", { market: MARKET }, `${MARKET} Detail - Autocoin OS`);
  }
  updatePageTitle();

  let globalGuards = {};
  let marketControls = {};

  qs("#mktTitle").textContent = MARKET;
  qs("#exchangeLink").href = `https://www.bybit.com/trade/spot/${MARKET}`;

  function fmtNum(n, d = 2) {
    if (formatUSDT) return formatUSDT(n, d);
    if (n === null || n === undefined) return "—";
    return Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
  }

  function fmtUsdt(v) {
    if (formatUSDT) return formatUSDT(v, 0);
    const n = Number(v);
    if (!Number.isFinite(n)) return "—";
    return Math.round(n).toLocaleString("ko-KR");
  }

  function fmtTime(ts) {
    if (!ts) return "-";
    const d = new Date(ts * 1000);
    return d.toLocaleTimeString();
  }

  function calcUpsidePotential(brain) {
    if (!brain) return null;
    let score = 0;
    let factors = 0;

    if (brain.rsi !== undefined && Number.isFinite(brain.rsi)) {
      const rsiPotential = Math.max(0, Math.min(100, (70 - brain.rsi) * 2.5));
      score += rsiPotential;
      factors++;
    }
    if (brain.ai_prediction !== undefined && Number.isFinite(brain.ai_prediction)) {
      const aiPotential = brain.ai_prediction * 100;
      score += aiPotential;
      factors++;
    }
    if (brain.momentum !== undefined && Number.isFinite(brain.momentum)) {
      const momPotential = Math.max(0, Math.min(100, 50 - brain.momentum * 5));
      score += momPotential;
      factors++;
    }

    if (factors === 0) return null;
    return Math.max(0, Math.min(100, score / factors));
  }

  function renderPositionGauge(potential) {
    if (potential === null) return "";
    const pct = Math.round(potential);
    const arrow = pct >= 50 ? "↑" : "↓";
    const cls = pct >= 60 ? "bullish" : (pct <= 40 ? "bearish" : "neutral");
    const label = pct >= 60
      ? t("common.buy", "BUY")
      : (pct <= 40 ? t("common.sell", "SELL") : t("common.neutral", "Neutral"));

    return `
      <div class="position-gauge">
        <span class="gauge-label sell">${t("common.sell", "SELL")}</span>
        <div class="gauge-track">
          <div class="gauge-marker" style="left: ${pct}%"></div>
        </div>
        <span class="gauge-label buy">${t("common.buy", "BUY")}</span>
        <span class="gauge-value ${cls}">${pct}%${arrow} ${label}</span>
      </div>
    `;
  }

  async function fetchJson(url, options = {}) {
    const headers = { ...(options.headers || {}) };
    if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";

    const res = await fetch(url, {
      credentials: "include",
      ...options,
      headers
    });

    const data = await res.json().catch(() => ({ ok: false, error: `HTTP ${res.status}` }));
    if (!res.ok && typeof data.ok === "undefined") data.ok = false;
    return data;
  }

  function initTradingView() {
    if (typeof TradingView === "undefined") {
      setTimeout(initTradingView, 500);
      return;
    }

    const parts = MARKET.split("-");
    const symbol = parts.length === 2 ? `BYBIT:${parts[1]}${parts[0]}` : `BYBIT:${MARKET.replace("-", "")}`;

    new TradingView.widget({
      autosize: true,
      symbol,
      interval: "15",
      timezone: "Etc/UTC",
      theme: "dark",
      style: "1",
      locale: "en",
      toolbar_bg: "#f1f3f6",
      enable_publishing: false,
      hide_side_toolbar: false,
      allow_symbol_change: true,
      container_id: "tv_chart_container"
    });
  }

  const STRATEGY_PARAMS = {
    PINGPONG: [
      { key: "pp_entry_gap_pct", labelKey: "market_detail.param.entry_gap_pct", label: "Entry Gap %", type: "number", step: 0.01, def: 0.35 },
      { key: "pp_exit_gap_pct", labelKey: "market_detail.param.exit_gap_pct", label: "Exit Gap %", type: "number", step: 0.01, def: 0.35 },
      { key: "ai_influence", labelKey: "market_detail.param.ai_influence", label: "AI Influence", type: "number", step: 0.1, def: 0.5 }
    ],
    AUTOLOOP: [
      { key: "rsi_buy", labelKey: "market_detail.param.rsi_buy", label: "RSI Buy", type: "number", step: 1, def: 28 },
      { key: "rsi_sell", labelKey: "market_detail.param.rsi_sell", label: "RSI Sell", type: "number", step: 1, def: 58 },
      { key: "ai_influence", labelKey: "market_detail.param.ai_influence", label: "AI Influence", type: "number", step: 0.1, def: 0.5 }
    ],
    LIGHTNING: [
      { key: "burst_threshold", labelKey: "market_detail.param.burst_pct", label: "Burst %", type: "number", step: 0.1, def: 1.5 },
      { key: "burst_window", labelKey: "market_detail.param.window_ticks", label: "Window (ticks)", type: "number", step: 1, def: 5 },
      { key: "ai_influence", labelKey: "market_detail.param.ai_influence", label: "AI Influence", type: "number", step: 0.1, def: 0.5 }
    ],
    GAZUA: [
      { key: "tp", labelKey: "market_detail.param.tp_pct", label: "TP %", type: "number", step: 0.5, def: 15.0 },
      { key: "sl", labelKey: "market_detail.param.sl_pct", label: "SL %", type: "number", step: 0.5, def: -10.0 },
      { key: "trail_tp_enabled", labelKey: "market_detail.param.trailing_tp", label: "Trailing TP", type: "checkbox", def: true },
      { key: "trail_dist_pct", labelKey: "market_detail.param.trail_dist_pct", label: "Trail Dist %", type: "number", step: 0.5, def: 4.0 },
      { key: "buy_now", labelKey: "market_detail.param.buy_now", label: "Buy Now", type: "checkbox", def: false },
      { key: "hold_sell", labelKey: "market_detail.param.hold_no_sell", label: "Hold (No Sell)", type: "checkbox", def: false },
      { key: "ai_buy_threshold", labelKey: "market_detail.param.ai_buy_threshold", label: "AI Buy Threshold", type: "number", step: 0.05, def: 0.65 }
    ]
  };

  function renderStrategyParams(mode) {
    const paramsDef = STRATEGY_PARAMS[String(mode || "").toUpperCase()];
    const paramsBox = qs("#strategyParams");
    if (!paramsBox) return;

    if (!paramsDef) {
      paramsBox.innerHTML = `<div class="muted">${tf("market_detail.no_params_for_mode", { mode }, `No parameters for ${mode}.`)}</div>`;
      return;
    }

    const currentParams = (marketControls.strategy || {}).params || {};

    paramsBox.innerHTML = paramsDef.map((p) => {
      const val = currentParams[p.key] !== undefined ? currentParams[p.key] : p.def;
      const labelText = t(p.labelKey || "", p.label);

      if (p.type === "checkbox") {
        const checked = val === true || val === "true" || val === 1;
        return `
          <div class="param-row">
            <label for="param_${p.key}">${labelText}</label>
            <input type="checkbox" id="param_${p.key}" data-param-key="${p.key}" ${checked ? "checked" : ""} class="admin-checkbox">
          </div>
        `;
      }

      return `
        <div class="param-row">
          <label for="param_${p.key}">${labelText}</label>
          <input type="number" id="param_${p.key}" data-param-key="${p.key}" value="${val}" step="${p.step || 0.1}" class="admin-input">
        </div>
      `;
    }).join("");
  }

  const GUARD_KEYS = [
    { k: "entry_enabled", labelKey: "guard.entry_enabled", label: "Entry Enabled", def: true },
    { k: "exit_profit_guard", labelKey: "guard.profit_guard", label: "Profit Guard", def: false },
    { k: "entry_ceiling_guard", labelKey: "guard.ceiling_guard", label: "Ceiling Guard", def: false },
    { k: "entry_qty_guard", labelKey: "guard.qty_guard", label: "Qty Guard", def: false },
    { k: "entry_ob_guard_enabled", labelKey: "guard.ob_guard", label: "OB Guard", def: false },
    { k: "tp_limit_exit_enabled", labelKey: "guard.tp_limit_exit", label: "TP Limit Exit", def: false }
  ];

  function renderControls() {
    const st = marketControls.strategy || {};
    const stEn = qs("#stEnabled");
    const stMode = qs("#stMode");
    if (stEn) stEn.checked = !!st.enabled;
    if (stMode) stMode.value = (st.mode || st.name || "AI").toUpperCase();
    if (stMode) renderStrategyParams(stMode.value);

    const gBox = qs("#guardChecks");
    if (gBox) {
      const mg = marketControls.guards || {};
      gBox.innerHTML = GUARD_KEYS.map((item) => {
        const globalVal = globalGuards[item.k];
        const ovVal = mg[item.k];
        const eff = (ovVal !== undefined && ovVal !== null) ? ovVal : (globalVal !== undefined ? globalVal : item.def);
        const isOv = (ovVal !== undefined && ovVal !== null);

        return `
          <label class="chk-item ${isOv ? "overridden" : ""}" title="${isOv ? t("common.market_override", "Market Override") : t("common.global_default", "Global Default")}">
            <input type="checkbox" data-key="${item.k}" ${eff ? "checked" : ""}>
            ${t(item.labelKey, item.label)}
          </label>
        `;
      }).join("");
    }
  }

  async function saveStrategy() {
    const enabled = qs("#stEnabled").checked;
    const mode = qs("#stMode").value;

    const params = {};
    const paramsBox = qs("#strategyParams");
    if (paramsBox) {
      paramsBox.querySelectorAll("input[data-param-key]").forEach((input) => {
        if (input.type === "checkbox") {
          params[input.dataset.paramKey] = input.checked;
        } else {
          params[input.dataset.paramKey] = Number(input.value);
        }
      });
    }

    const payload = { strategy: { enabled, mode, params } };
    if (enabled) payload.ai = { enabled: false };

    try {
      const res = await fetchJson(`/api/engine/controls?market=${MARKET}`, {
        method: "POST",
        body: JSON.stringify(payload)
      });
      if (!res.ok) throw new Error(res.error || t("common.error", "Error"));
      alert(t("market_detail.strategy_saved", "Strategy settings saved."));
      loadData();
    } catch (e) {
      alert(tf("market_detail.error_saving_strategy", { error: e.message }, `Error saving strategy: ${e.message}`));
    }
  }

  async function saveGuards() {
    const gBox = qs("#guardChecks");
    if (!gBox) return;

    const guards = {};
    gBox.querySelectorAll("input[type=checkbox]").forEach((el) => {
      guards[el.dataset.key] = el.checked;
    });

    try {
      const res = await fetchJson(`/api/engine/controls?market=${MARKET}`, {
        method: "POST",
        body: JSON.stringify({ guards })
      });
      if (!res.ok) throw new Error(res.error || t("common.error", "Error"));

      marketControls.guards = (res.controls && res.controls.guards) ? res.controls.guards : {};
      renderControls();

      const btn = qs("#btnSaveGuards");
      if (btn) {
        btn.textContent = t("common.applied", "Applied!");
        setTimeout(() => { btn.textContent = t("market_detail.apply_guards", "Apply Guards"); }, 1500);
      }
    } catch (e) {
      alert(tf("market_detail.error_saving_guards", { error: e.message }, `Error saving guards: ${e.message}`));
    }
  }

  async function loadData() {
    try {
      const stratRes = await fetchJson(`/api/strategy/last?market=${MARKET}`);
      if (stratRes.ok) {
        const brain = stratRes.brain || {};
        const upnl = Number(stratRes.unrealized_profit || 0);
        qs("#valPnl").textContent = `${fmtUsdt(upnl)} USDT`;
        qs("#valPnl").className = `sc-val ${upnl > 0 ? "pnl-pos" : (upnl < 0 ? "pnl-neg" : "")}`;

        let brainHtml = "";
        if (brain.ai_prediction !== undefined) {
          const score = Number(brain.ai_prediction);
          const color = score >= 0.6 ? "var(--ok)" : (score <= 0.4 ? "var(--danger)" : "var(--text-main)");
          brainHtml += `<div class="sc-row"><span class="sc-label">${t("market_detail.ai_score", "AI Score")}</span><span class="sc-val" style="color:${color}">${fmtNum(score, 4)}</span></div>`;
        }
        if (brain.rsi !== undefined) brainHtml += `<div class="sc-row"><span class="sc-label">RSI</span><span class="sc-val">${fmtNum(brain.rsi, 1)}</span></div>`;
        if (brain.volatility !== undefined) brainHtml += `<div class="sc-row"><span class="sc-label">${t("market_detail.volatility", "Volatility")}</span><span class="sc-val">${fmtNum(brain.volatility, 2)}%</span></div>`;
        if (brain.momentum !== undefined) brainHtml += `<div class="sc-row"><span class="sc-label">${t("market_detail.momentum", "Momentum")}</span><span class="sc-val">${fmtNum(brain.momentum, 2)}%</span></div>`;
        if (brain.trend !== undefined) brainHtml += `<div class="sc-row"><span class="sc-label">${t("market_detail.trend", "Trend")}</span><span class="sc-val">${fmtNum(brain.trend, 2)}%</span></div>`;
        if (brain.volume_change_pct !== undefined) brainHtml += `<div class="sc-row"><span class="sc-label">${t("market_detail.vol_change", "Vol Change")}</span><span class="sc-val">${fmtNum(brain.volume_change_pct, 2)}%</span></div>`;

        qs("#brainMetrics").innerHTML = brainHtml || `<div class="empty">${t("market_detail.no_brain_data", "No brain data available")}</div>`;
        qs("#rawState").textContent = JSON.stringify(stratRes, null, 2);
      }

      const sysRes = await fetchJson("/api/system/status");
      if (sysRes.ok && sysRes.system) {
        const sys = sysRes.system;
        const prices = sys.active_prices || {};
        const price = prices[MARKET];
        qs("#valPrice").textContent = price ? fmtNum(price, 0) : "—";

        const ctx = (sys.coordinator && sys.coordinator[MARKET]) ? sys.coordinator[MARKET] : null;
        if (ctx) {
          const pos = ctx.position;
          const qty = pos ? Number(pos.qty) : 0;
          const posUsdt = pos ? Number(pos.usdt || 0) : 0;
          qs("#valPos").textContent = qty > 0 ? `${fmtNum(qty, 4)} (${fmtUsdt(posUsdt)} USDT)` : t("common.none", "None");

          const ctrls = ctx.controls || {};
          const s = ctrls.strategy || {};
          const mode = s.enabled ? (s.mode || s.name) : t("market_detail.strategy_auto_off", "AUTO/OFF");
          qs("#valStrat").textContent = String(mode).toUpperCase();
          marketControls = ctrls;
        } else {
          qs("#valPos").textContent = t("common.none", "None");
          qs("#valStrat").textContent = t("common.not_available", "N/A");
          marketControls = {};
        }
        renderControls();
      }

      const ledRes = await fetchJson("/api/system/ledger/tail?n=1000");
      if (ledRes.ok && Array.isArray(ledRes.items)) {
        const rows = ledRes.items
          .filter((x) => x.market === MARKET || (x.data && x.data.market === MARKET))
          .reverse()
          .slice(0, 50);

        const tbody = qs("#ledgerTable tbody");
        if (rows.length === 0) {
          tbody.innerHTML = `<tr><td colspan="4" class="empty">${t("market_detail.no_recent_activity", "No recent activity found")}</td></tr>`;
        } else {
          tbody.innerHTML = rows.map((r) => {
            const d = r.data || {};
            let info = d.price || d.avg_price || d.expected_price || d.message || d.error || d.reason || "—";
            if (typeof info === "number") info = fmtNum(info);

            const side = d.side || (r.event.includes("BUY")
              ? t("common.buy", "BUY")
              : (r.event.includes("SELL") ? t("common.sell", "SELL") : "—"));

            return `
              <tr>
                <td>${fmtTime(r.ts)}</td>
                <td>${r.event}</td>
                <td>${side}</td>
                <td>${info}</td>
              </tr>
            `;
          }).join("");
        }
      }
    } catch (e) {
      console.error(t("market_detail.load_failed", "Load failed"), e);
    }
  }

  initTradingView();

  fetchJson("/api/system/guards")
    .then((res) => {
      if (res.ok && res.guards) globalGuards = res.guards;
    })
    .catch(console.error);

  const btnStrat = qs("#btnSaveStrat");
  if (btnStrat) btnStrat.addEventListener("click", saveStrategy);
  const btnGuard = qs("#btnSaveGuards");
  if (btnGuard) btnGuard.addEventListener("click", saveGuards);

  const stModeSelect = qs("#stMode");
  if (stModeSelect) stModeSelect.addEventListener("change", () => renderStrategyParams(stModeSelect.value));

  document.addEventListener("autocoin:lang-changed", () => {
    updatePageTitle();
    renderControls();
    loadData();
  });

  loadData();
  if (IntervalManager) {
    IntervalManager.set("market_detail_poll", loadData, 3000);
  } else {
    setInterval(loadData, 3000);
  }
});
