/* Depends on: utils.js (window.AutocoinUtils) */
"use strict";

(function() {
  const { IntervalManager, formatUSDT } = window.AutocoinUtils || {};
  const i18n = window.AutocoinEmbedI18n || {};
  const t = i18n.t || ((_, fallback = "") => fallback || "");
  const qs = (s) => document.querySelector(s);
  let lastMarkets = [];

  if (typeof i18n.initLanguage === "function") {
    i18n.initLanguage("ko");
  }

  function applyHeaderTexts() {
    document.title = t("ladder.doc_title", "Ladder Strategy View - Autocoin OS");
  }
  applyHeaderTexts();

  function fmtNum(n, d = 2) {
    if (formatUSDT) return formatUSDT(n, d);
    if (n === null || n === undefined) return "—";
    return Number(n).toLocaleString(undefined, { minimumFractionDigits: d, maximumFractionDigits: d });
  }

  async function fetchJson(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  }

  async function fetchAllLadderStatus() {
    const listRes = await fetchJson("/api/ladder/list");
    if (!listRes.ok || !Array.isArray(listRes.items)) return [];

    const markets = listRes.items;
    const statuses = await Promise.all(
      markets.map((cfg) =>
        fetchJson(`/api/ladder/status?market=${cfg.market}`)
          .then((statusRes) => ({ ...cfg, status: statusRes }))
          .catch(() => ({ ...cfg, status: { error: "fetch_failed" } }))
      )
    );
    return statuses;
  }

  function render(markets) {
    lastMarkets = Array.isArray(markets) ? markets : [];
    const grid = document.getElementById("marketGrid");
    if (!markets || markets.length === 0) {
      grid.innerHTML = `<div class="empty">${t("ladder.no_markets_configured", "No markets configured for Ladder strategy.")}</div>`;
      qs("#stCount").textContent = "0";
      qs("#stOrders").textContent = "0";
      return;
    }

    let totalOrders = 0;

    grid.innerHTML = markets.map((item) => {
      const m = item.market;
      const cfg = item;
      const status = item.status || {};
      const openOrders = status.open_orders || {};
      const total = openOrders.total || 0;

      totalOrders += total;
      const enabled = !!cfg.enabled;

      return `
        <div class="strat-card" onclick="window.location.href='/ui/market_detail.html?market=${m}'" style="cursor:pointer" title="${t("common.click_for_details", "Click for details")}">
          <div class="sc-head">
            <div class="sc-title">${m}</div>
            <div class="sc-badge ${enabled ? "active" : ""}">${enabled ? t("ladder.badge_on", "ON") : t("ladder.badge_off", "OFF")}</div>
          </div>
          <div class="sc-row">
            <div class="sc-label">${t("ladder.open_orders", "Open Orders")}</div>
            <div class="sc-val">${total}</div>
          </div>
          <div class="sc-row">
            <div class="sc-label">${t("ladder.range", "Range")}</div>
            <div class="sc-val">${fmtNum(cfg.lower_bound, 0)} - ${fmtNum(cfg.upper_bound, 0)}</div>
          </div>
          <div class="sc-divider"></div>
          <div class="sc-details">
             <div>${t("ladder.spacing", "Spacing")}: ${fmtNum(cfg.spacing_value, 4)} ${cfg.spacing_mode}</div>
             <div>${t("ladder.order_usdt", "Order USDT")}: ${fmtNum(cfg.order_usdt, 0)}</div>
          </div>
        </div>
      `;
    }).join("");

    qs("#stCount").textContent = markets.length;
    qs("#stOrders").textContent = totalOrders;
  }

  async function loop() {
    try {
      const markets = await fetchAllLadderStatus();
      render(markets);
    } catch (e) {
      console.error(t("ladder.update_failed", "Failed to update ladder view"), e);
    }
  }

  document.addEventListener("autocoin:lang-changed", () => {
    applyHeaderTexts();
    render(lastMarkets);
  });

  loop();
  if (IntervalManager) {
    IntervalManager.set("ladder_poll", loop, 5000);
  } else {
    setInterval(loop, 5000);
  }
})();
