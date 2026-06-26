/* Depends on: utils.js (window.AutocoinUtils) */
"use strict";

(function() {
  const { IntervalManager, formatUSDT, formatPct } = window.AutocoinUtils || {};
  const i18n = window.AutocoinEmbedI18n || {};
  const t = i18n.t || ((_, fallback = "") => fallback || "");
  const tf = i18n.tf || ((_, __, fallback = "") => fallback || "");
  const qs = (s) => document.querySelector(s);
  const urlParams = new URLSearchParams(window.location.search);
  const STRATEGY = (urlParams.get("strategy") || "UNKNOWN").toUpperCase();
  let lastSystem = null;

  if (typeof i18n.initLanguage === "function") {
    i18n.initLanguage("ko");
  }

  function applyHeaderTexts() {
    const titleEl = document.getElementById("stratTitle");
    if (titleEl) titleEl.textContent = STRATEGY;

    const runningEl = document.getElementById("runningWithText");
    if (runningEl) {
      runningEl.innerHTML = tf(
        "strategy.running_with",
        { strategy: `<b id="stratName">${STRATEGY}</b>` },
        `Markets running <b id="stratName">${STRATEGY}</b> strategy`
      );
    }

    document.title = tf("strategy.doc_title", { strategy: STRATEGY }, `${STRATEGY} Strategy Detail - Autocoin OS`);
  }

  applyHeaderTexts();

  const navId = `nav${STRATEGY}`;
  const navEl = document.getElementById(navId);
  if (navEl) navEl.classList.add("active");

  function fmtNum(n, d = 2) {
    if (formatUSDT) return formatUSDT(n, d);
    if (n === null || n === undefined) return "—";
    return Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
  }

  function fmtPct(n) {
    if (formatPct) return formatPct(n, 2);
    if (n === null || n === undefined) return "—";
    return Number(n).toFixed(2) + "%";
  }

  async function fetchStatus() {
    try {
      const res = await fetch("/api/system/status");
      const data = await res.json();
      if (!data.ok) throw new Error(data.error);
      return data.system;
    } catch (e) {
      console.error(e);
      return null;
    }
  }

  function render(system) {
    lastSystem = system;
    if (!system) return;

    const contexts = system.coordinator || {};
    const markets = [];

    for (const [m, ctx] of Object.entries(contexts)) {
      const ctrls = ctx.controls || {};
      const stratCtrl = ctrls.strategy || {};
      let mode = stratCtrl.enabled ? (stratCtrl.mode || stratCtrl.name) : null;
      if (!mode) mode = (ctx.strategy || {}).selected;
      if (String(mode).toUpperCase() === STRATEGY) markets.push({ market: m, ctx });
    }

    let totalPnl = 0;
    let totalAi = 0;
    let aiCount = 0;

    const grid = document.getElementById("marketGrid");
    if (markets.length === 0) {
      grid.innerHTML = `<div class="empty">${tf("strategy.no_markets_running", { strategy: STRATEGY }, `No markets currently running ${STRATEGY}`)}</div>`;
      qs("#stCount").textContent = "0";
      qs("#stPnl").textContent = "0";
      qs("#stAi").textContent = t("common.not_available", "N/A");
      return;
    }

    grid.innerHTML = markets.map((item) => {
      const m = item.market;
      const ctx = item.ctx;
      const pos = ctx.position || {};
      const qty = Number(pos.qty || 0);
      const upnl = Number(ctx.unrealized_profit || 0);
      totalPnl += upnl;

      const stratState = ctx.strategy || {};
      const reason = stratState.reason || {};
      const ai = reason.engine_ai || {};
      const aiScore = ai.ai_prediction;

      if (aiScore !== undefined) {
        totalAi += Number(aiScore);
        aiCount++;
      }

      const pnlClass = upnl > 0 ? "pnl-pos" : (upnl < 0 ? "pnl-neg" : "");

      let details = "";
      if (STRATEGY === "PINGPONG") {
        const lv = (((reason.strategy_out || {}).meta || {}).levels || {});
        if (lv.buy_price) details += `<div>${t("strategy.detail_buy", "Buy")}: ${fmtNum(lv.buy_price)}</div>`;
        if (lv.sell_price) details += `<div>${t("strategy.detail_sell", "Sell")}: ${fmtNum(lv.sell_price)}</div>`;
      } else if (STRATEGY === "AUTOLOOP") {
        const stage = ctx.strategy_vars?.autoloop_entry_stage || 0;
        details += `<div>${t("strategy.detail_stage", "Stage")}: ${stage}</div>`;
      } else if (STRATEGY === "LIGHTNING") {
        const mom = (ai.brain || ai).momentum_pct || 0;
        details += `<div>${t("strategy.detail_mom", "Mom")}: ${fmtNum(mom)}%</div>`;
      }

      return `
        <div class="strat-card" data-market="${m}" style="cursor:pointer" title="${t("common.click_for_details", "Click for details")}">
          <div class="sc-head">
            <div class="sc-title">${m}</div>
            <div class="sc-badge ${qty > 0 ? "active" : ""}">${qty > 0 ? t("strategy.badge_pos", "POS") : t("strategy.badge_wait", "WAIT")}</div>
          </div>
          <div class="sc-row">
            <div class="sc-label">${t("strategy.card_pnl", "PnL")}</div>
            <div class="sc-val ${pnlClass}">${fmtNum(upnl, 0)}</div>
          </div>
          <div class="sc-row">
            <div class="sc-label">${t("strategy.card_ai_score", "AI Score")}</div>
            <div class="sc-val">${aiScore !== undefined ? fmtNum(aiScore, 4) : "—"}</div>
          </div>
          <div class="sc-divider"></div>
          <div class="sc-details">
            ${details || `<div class="muted">${t("strategy.card_no_details", "No details")}</div>`}
          </div>
          <div class="sc-ai-feat">
            <span title="${t("strategy.metric_rsi", "RSI")}">${t("strategy.metric_rsi", "RSI")}: ${fmtNum(ai.rsi, 0)}</span>
            <span title="${t("strategy.metric_vol", "Vol")}">${t("strategy.metric_vol", "Vol")}: ${fmtNum(ai.volatility, 2)}%</span>
          </div>
        </div>
      `;
    }).join("");

    grid.querySelectorAll(".strat-card").forEach((card) => {
      card.addEventListener("click", () => {
        const market = card.dataset.market;
        if (market) window.location.href = `/ui/market_detail.html?market=${market}`;
      });
    });

    qs("#stCount").textContent = markets.length;
    qs("#stPnl").textContent = fmtNum(totalPnl, 0);
    qs("#stPnl").className = totalPnl >= 0 ? "pnl-pos" : "pnl-neg";
    qs("#stAi").textContent = aiCount ? fmtNum(totalAi / aiCount, 4) : "—";
  }

  async function loop() {
    const sys = await fetchStatus();
    render(sys);
  }

  document.addEventListener("autocoin:lang-changed", () => {
    applyHeaderTexts();
    if (lastSystem) render(lastSystem);
  });

  loop();
  if (IntervalManager) {
    IntervalManager.set("strategy_poll", loop, 2000);
  } else {
    setInterval(loop, 2000);
  }
})();
