/* ============================================================
 * Autocoin OS v3-H — LongHold Panel (GAZUA / LADDER)
 * - UI-only extension (keeps dashboard.js mostly untouched)
 * - Requires backend:
 *   - GET  /api/ladder/longhold/list
 *   - POST /api/ladder/longhold/config
 *   - GET  /api/ladder/longhold/candidates
 *   - GET  /api/ladder/market_stats
 * 
 * Depends on: utils.js (window.AutocoinUtils)
 * ============================================================ */

"use strict";

(function () {
  try {
  const { IntervalManager, formatUSDT, formatPct } = window.AutocoinUtils || {};
  const API = {
    list: "/api/ladder/longhold/list",
    save: "/api/ladder/longhold/config",
    candidates: (strategy, n = 3) =>
      `/api/ladder/longhold/candidates?strategy=${encodeURIComponent(strategy)}&n=${encodeURIComponent(String(n))}`,
    marketStats: (market) => `/api/ladder/market_stats?market=${encodeURIComponent(market)}`,
    aiAuto: "/api/ai/auto_full",
    aiInfo: "/api/ai/info",
    aiHistory: "/api/ai/history",
    guards: "/api/system/guards",
  };

  const PRICE_TTL_MS = 20_000;
  const LIST_POLL_MS = 10_000;

  const lhState = {
    list: [],
    candidates: { GAZUA: null, LADDER: null },
    priceCache: new Map(), // market -> {price, ts}
    inFlight: new Set(),   // market
    lastListFetch: 0,
    lastCandidatesFetch: 0,
  };

  // ---------- small utils ----------
  function qs(id) { return document.getElementById(id) || null; }

  function escHtml(s) {
    return String(s ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll("\"", "&quot;")
      .replaceAll("'", "&#039;");
  }

  function num(v, dflt = null) {
    const n = Number(v);
    return Number.isFinite(n) ? n : dflt;
  }

  function fmtNum(v, digits = 2) {
    const n = num(v, null);
    if (n === null) return "—";
    try {
      return n.toLocaleString(undefined, { minimumFractionDigits: digits, maximumFractionDigits: digits });
    } catch (_) {
      return String(n);
    }
  }

  function fmtUsdt(v) {
    if (formatUSDT) return formatUSDT(v, 0);
    const n = num(v, null);
    if (n === null) return "—";
    try {
      return Math.round(n).toLocaleString("ko-KR");
    } catch (_) {
      return String(Math.round(n));
    }
  }

  function fmtPct(v, digits = 3) {
    if (formatPct) return formatPct(v, digits);
    const n = num(v, null);
    if (n === null) return "—";
    return `${fmtNum(n, digits)}%`;
  }

  function pill(cls, label, title = "") {
    const t = title ? ` title="${escHtml(title)}"` : "";
    return `<span class="pill ${escHtml(cls || "")}"${t}>${escHtml(label || "—")}</span>`;
  }

  async function fetchJson(url, opts) {
    const res = await fetch(url, Object.assign({ headers: { "Content-Type": "application/json" } }, (opts || {})));
    const text = await res.text();
    let data = null;
    try { data = text ? JSON.parse(text) : null; } catch (_) { data = text; }

    if (!res.ok) {
      const msg = (data && (data.detail || data.error || data.message)) ? JSON.stringify(data) : String(text || res.statusText);
      const e = new Error(`HTTP ${res.status}: ${msg}`);
      e.status = res.status;
      e.data = data;
      throw e;
    }
    return data;
  }

  function getGlobalState() {
    // dashboard.js defines `const state = {...}` in global lexical env.
    try {
      // eslint-disable-next-line no-undef
      return (typeof state !== "undefined") ? state : null;
    } catch (_) {
      return null;
    }
  }

  // ---------- price helpers ----------
  function getCachedPrice(market) {
    const item = lhState.priceCache.get(market);
    if (!item) return null;
    if ((Date.now() - item.ts) > PRICE_TTL_MS) return null;
    return item.price;
  }

  function setCachedPrice(market, price) {
    const p = num(price, null);
    if (p === null || p <= 0) return;
    lhState.priceCache.set(market, { price: p, ts: Date.now() });
  }

  function getPriceFromMainState(market) {
    const st = getGlobalState();
    if (!st) return null;

    // Prefer price_store snapshot (active prices)
    const p1 = st.prices ? num(st.prices[market], null) : null;
    if (p1 !== null && p1 > 0) return p1;

    // Fallback: coordinator context might carry a last_price (best-effort)
    const ctx = st.coordinator ? st.coordinator[market] : null;
    const p2 =
      (ctx && num(ctx.last_price, null)) ??
      (ctx && num(ctx.lastPrice, null)) ??
      (ctx && num(ctx.price, null)) ??
      null;
    if (p2 !== null && p2 > 0) return p2;

    return null;
  }

  async function fetchMarketPriceOnce(market) {
    if (!market) return null;
    if (lhState.inFlight.has(market)) return getCachedPrice(market);

    const cached = getCachedPrice(market);
    if (cached !== null) return cached;

    const main = getPriceFromMainState(market);
    if (main !== null) {
      setCachedPrice(market, main);
      return main;
    }

    lhState.inFlight.add(market);
    try {
      const data = await fetchJson(API.marketStats(market), { method: "GET" });
      const lp = (data && (data.last_price ?? data.lastPrice ?? data.price)) ?? null;
      if (lp !== null) setCachedPrice(market, lp);
      return getCachedPrice(market);
    } catch (e) {
      // Don't spam; keep a short negative cache by storing ts only
      lhState.priceCache.set(market, { price: null, ts: Date.now() });
      return null;
    } finally {
      lhState.inFlight.delete(market);
    }
  }

  async function warmPricesFor(markets) {
    // sequential (small list) to avoid rate limit issues
    for (const m of markets) {
      await fetchMarketPriceOnce(m);
    }
  }

  // ---------- position helpers ----------
  function extractPosition(market) {
    const st = getGlobalState();
    if (!st || !st.coordinator) return { qty: 0, entry: 0, has: false };
    const ctx = st.coordinator[market] || null;
    const pos = ctx ? (ctx.position || null) : null;
    if (!pos) return { qty: 0, entry: 0, has: false };

    const qty = num(pos.qty, 0) || 0;
    const entry =
      num(pos.entry, null) ??
      num(pos.entry_price, null) ??
      num(pos.entryPrice, null) ??
      num(pos.avg_price, null) ??
      num(pos.avgPrice, null) ??
      0;

    return { qty, entry: entry || 0, has: qty > 0 && (entry || 0) > 0 };
  }

  // ---------- duplicates highlight ----------
  function getOmaManagedSet() {
    const st = getGlobalState();
    const s = new Set();
    if (!st) return s;

    const oma = st.oma || {};
    const addArr = (arr) => {
      if (!Array.isArray(arr)) return;
      for (const x of arr) {
        if (x) s.add(String(x).toUpperCase());
      }
    };

    addArr(st.managedMarkets);
    addArr(st.recoveryMarkets);
    addArr(oma.active);
    addArr(oma.watch);
    addArr(oma.recovery);

    return s;
  }

  function applyDupHighlights(longholdSet) {
    // OMA market cards
    document.querySelectorAll(".market-card").forEach((card) => {
      const m = (card && card.dataset) ? String(card.dataset.market || "").toUpperCase() : "";
      if (!m) return;
      if (longholdSet.has(m)) card.classList.add("dup-longhold");
      else card.classList.remove("dup-longhold");
    });

    // Reserved queue rows (best-effort: parse .mkt span)
    const wrap = qs("reservedQueue");
    if (wrap) {
      wrap.querySelectorAll(".reserved-row").forEach((row) => {
        const mktEl = row.querySelector(".mkt");
        const m = mktEl ? String(mktEl.textContent || "").trim().toUpperCase() : "";
        if (!m) return;
        if (longholdSet.has(m)) row.classList.add("dup-longhold");
        else row.classList.remove("dup-longhold");
      });
    }
  }

  // ---------- rendering ----------
  function setMsg(text, ok = true) {
    const el = qs("lhMsg");
    if (!el) return;
    el.textContent = text || "—";
    el.style.color = ok ? "" : "var(--danger)";
  }

  function renderCandidates() {
    const box = qs("lhCandidates");
    if (!box) return;

    const cg = lhState.candidates.GAZUA;
    const cl = lhState.candidates.LADDER;

    const mkTable = (title, payload, strategy) => {
      const items = payload && Array.isArray(payload.items) ? payload.items : [];
      const meta = payload && payload.method ? `${payload.method}` : "—";

      if (!items.length) {
        return `
          <div class="lh-title">${escHtml(title)} <span class="muted">(${escHtml(meta)})</span></div>
          <div class="empty">후보가 없습니다.</div>
        `;
      }

      return `
        <div class="lh-title">${escHtml(title)} <span class="muted">(${escHtml(meta)})</span></div>
        <table class="lh-table">
          <thead>
            <tr>
              <th>Market</th>
              <th>Score</th>
              <th>Mom</th>
              <th>Vol</th>
              <th>Liq</th>
              <th>Action</th>
            </tr>
          </thead>
          <tbody>
            ${items.map((it) => {
              const m = String(it.market || "");
              const score = num(it.score, null);
              const mom = num(it.momentum, null);
              const vol = num(it.volatility, null);
              const liq = num(it.liquidity, null);
              return `
                <tr>
                  <td>${escHtml(m)}</td>
                  <td>${score !== null ? fmtNum(score, 4) : "—"}</td>
                  <td>${mom !== null ? fmtNum(mom, 4) : "—"}</td>
                  <td>${vol !== null ? fmtNum(vol, 4) : "—"}</td>
                  <td>${liq !== null ? fmtNum(liq, 2) : "—"}</td>
                  <td class="lh-row-actions">
                    <button class="btn btn-ghost" data-lh-add="${escHtml(m)}" data-lh-strategy="${escHtml(strategy)}">Add</button>
                  </td>
                </tr>
              `;
            }).join("")}
          </tbody>
        </table>
      `;
    };

    box.innerHTML = `
      <div style="display:grid; grid-template-columns: 1fr; gap: 10px;">
        <div>${mkTable("GAZUA", cg, "GAZUA")}</div>
        <div>${mkTable("LADDER", cl, "LADDER")}</div>
      </div>
    `;

    // bind add buttons
    box.querySelectorAll("button[data-lh-add]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const m = String(btn.getAttribute("data-lh-add") || "").toUpperCase();
        const strategy = String(btn.getAttribute("data-lh-strategy") || "GAZUA").toUpperCase();
        if (!m) return;
        await addOrUpdateLongHold({ market: m, strategy });
      });
    });
  }

  function renderList() {
    const box = qs("lhList");
    if (!box) return;

    const list = Array.isArray(lhState.list) ? lhState.list.slice() : [];
    const st = getGlobalState();
    const omaSet = getOmaManagedSet();

    // sort: enabled first, then by strategy then market
    list.sort((a, b) => {
      const ae = a && a.enabled ? 0 : 1;
      const be = b && b.enabled ? 0 : 1;
      if (ae !== be) return ae - be;
      const as = String(a.strategy || "").localeCompare(String(b.strategy || ""));
      if (as !== 0) return as;
      return String(a.market || "").localeCompare(String(b.market || ""));
    });

    if (!list.length) {
      box.innerHTML = `<div class="empty">등록된 LongHold 코인이 없습니다.</div>`;
      applyDupHighlights(new Set());
      return;
    }

    const now = Date.now() / 1000.0;

    const rowsHtml = list.map((cfg) => {
      const market = String(cfg.market || "").toUpperCase();
      const strat = String(cfg.strategy || "").toUpperCase();
      const enabled = !!cfg.enabled;

      const tgt = num(cfg.target_profit_pct, 0) || 0;
      const cooldown = num(cfg.notify_cooldown_sec, 3600) || 3600;
      const minPos = num(cfg.min_position_usdt, 5) || 5;

      const lastNotiTs = num(cfg.last_notified_ts, 0) || 0;
      const lastNotiAgo = lastNotiTs > 0 ? Math.max(0, Math.floor(now - lastNotiTs)) : null;
      const canNotify = lastNotiTs <= 0 ? true : ((now - lastNotiTs) >= cooldown);

      const pos = extractPosition(market);
      const px = getCachedPrice(market) ?? getPriceFromMainState(market);

      const qty = pos.qty || 0;
      const entry = pos.entry || 0;

      const equity = (qty > 0 && px !== null) ? (qty * px) : 0;
      const pnlPct = (qty > 0 && px !== null && entry > 0) ? ((px / entry - 1) * 100.0) : null;
      const pnlUsdt = (qty > 0 && px !== null && entry > 0) ? (qty * (px - entry)) : null;

      const hit = enabled && (pnlPct !== null) && (pnlPct >= tgt) && (equity >= minPos);

      let status = "—";
      let statusCls = "";
      if (!enabled) { status = "OFF"; statusCls = "pill-bad"; }
      else if (!qty || qty <= 0) { status = "NO_POS"; statusCls = "pill-warn"; }
      else if (hit && canNotify) { status = "HIT"; statusCls = "pill-ok"; }
      else if (hit && !canNotify) { status = "HIT_CD"; statusCls = "pill-warn"; }
      else { status = "HOLD"; statusCls = "pill"; }

      const conflict = omaSet.has(market);
      const conflictTag = conflict ? pill("pill-warn", "CONFLICT", "OMA와 LongHold 중복. OMA쪽은 DISABLED 권장") : "";

      const notiTxt = (lastNotiAgo === null) ? "—" : `${Math.floor(lastNotiAgo / 60)}m ago`;

      const priceTxt = (px !== null) ? fmtNum(px, 2) : "—";
      const entryTxt = (entry > 0) ? fmtNum(entry, 2) : "—";

      const pnlCls = (pnlUsdt !== null && pnlUsdt < 0) ? "pnl-neg" : (pnlUsdt !== null && pnlUsdt > 0) ? "pnl-pos" : "";

      return `
        <tr>
          <td>${escHtml(market)}</td>
          <td>${escHtml(strat)}</td>
          <td>${enabled ? pill("pill-ok", "ON") : pill("pill-bad", "OFF")}</td>
          <td>${fmtPct(tgt, 1)}</td>
          <td>${qty > 0 ? fmtUsdt(equity) : "—"}</td>
          <td class="${pnlCls}">${pnlPct !== null ? fmtPct(pnlPct, 2) : "—"}</td>
          <td class="${pnlCls}">${pnlUsdt !== null ? fmtUsdt(pnlUsdt) : "—"}</td>
          <td>${entryTxt}</td>
          <td>${priceTxt}</td>
          <td>${pill(statusCls, status, `min_pos=${minPos} cooldown=${cooldown}s`)}</td>
          <td>${conflictTag}</td>
          <td class="lh-row-actions">
            <button class="btn btn-ghost" data-lh-edit="${escHtml(market)}">Edit</button>
            <button class="btn ${enabled ? "btn-danger" : ""}" data-lh-toggle="${escHtml(market)}">${enabled ? "Disable" : "Enable"}</button>
          </td>
        </tr>
      `;
    }).join("");

    box.innerHTML = `
      <div style="overflow-x:auto;">
        <table class="lh-table">
          <thead>
            <tr>
              <th>Market</th>
              <th>Strat</th>
              <th>On</th>
              <th>Target</th>
              <th>Equity</th>
              <th>PnL%</th>
              <th>PnL</th>
              <th>Entry</th>
              <th>Price</th>
              <th>Status</th>
              <th></th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>${rowsHtml}</tbody>
        </table>
      </div>
    `;

    // bind per-row actions
    box.querySelectorAll("button[data-lh-toggle]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const m = String(btn.getAttribute("data-lh-toggle") || "").toUpperCase();
        if (!m) return;
        const cur = lhState.list.find((x) => String(x.market || "").toUpperCase() === m);
        const enabled = cur ? !cur.enabled : true;
        await addOrUpdateLongHold({ market: m, enabled });
      });
    });

    box.querySelectorAll("button[data-lh-edit]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const m = String(btn.getAttribute("data-lh-edit") || "").toUpperCase();
        if (!m) return;
        const cur = lhState.list.find((x) => String(x.market || "").toUpperCase() === m) || {};
        const curT = num(cur.target_profit_pct, 5) || 5;
        const curStr = String(cur.strategy || "GAZUA").toUpperCase();
        const curCd = num(cur.notify_cooldown_sec, 3600) || 3600;
        const curMin = num(cur.min_position_usdt, 5) || 5;

        const newT = prompt(`[${m}] target profit (%)`, String(curT));
        if (newT === null) return;
        const t = num(newT, null);
        if (t === null || t <= 0) {
          alert("Invalid target (%)");
          return;
        }

        const newMin = prompt(`[${m}] min position USDT (to avoid dust notifications)`, String(curMin));
        if (newMin === null) return;
        const mp = num(newMin, null);
        if (mp === null || mp < 0) {
          alert("Invalid min position USDT");
          return;
        }

        const newCd = prompt(`[${m}] notify cooldown (sec)`, String(curCd));
        if (newCd === null) return;
        const cd = num(newCd, null);
        if (cd === null || cd < 0) {
          alert("Invalid cooldown sec");
          return;
        }

        console.log("[LONGHOLD EDIT] Saving:", {market: m, target_profit_pct: t, cur: cur.target_profit_pct});
        await addOrUpdateLongHold({
          market: m,
          strategy: curStr,
          target_profit_pct: t,
          min_position_usdt: Math.round(mp),
          notify_cooldown_sec: Math.round(cd),
        });
      });
    });

    // apply duplicates highlight (enabled only)
    const enabledSet = new Set(list.filter((x) => x && x.enabled).map((x) => String(x.market || "").toUpperCase()));
    applyDupHighlights(enabledSet);
  }

  // ---------- backend actions ----------
  async function refreshList() {
    try {
      const data = await fetchJson(API.list, { method: "GET" });
      lhState.list = (data && Array.isArray(data.items)) ? data.items : [];
      lhState.lastListFetch = Date.now();
      setMsg(`LongHold list: ${lhState.list.length}`, true);

      const markets = lhState.list.map((x) => String(x.market || "").toUpperCase()).filter(Boolean);
      await warmPricesFor(markets);

      renderList();
      return true;
    } catch (e) {
      console.error(e);
      lhState.list = [];
      renderList();
      setMsg(`LongHold list fetch failed: ${e.message}`, false);
      return false;
    }
  }

  async function refreshCandidates() {
    const box = qs("lhCandidates");
    if (box) box.innerHTML = `<div class="empty">스캔 중…</div>`;

    try {
      const cg = await fetchJson(API.candidates("GAZUA", 3), { method: "GET" });
      lhState.candidates.GAZUA = cg;
    } catch (e) {
      console.error(e);
      lhState.candidates.GAZUA = { items: [], error: e.message, method: "—" };
    }

    try {
      const cl = await fetchJson(API.candidates("LADDER", 3), { method: "GET" });
      lhState.candidates.LADDER = cl;
    } catch (e) {
      console.error(e);
      lhState.candidates.LADDER = { items: [], error: e.message, method: "—" };
    }

    lhState.lastCandidatesFetch = Date.now();
    renderCandidates();
  }

  async function addOrUpdateLongHold(partial) {
    const market = String(partial.market || "").toUpperCase().trim();
    if (!market) {
      alert("market is required (ex: BTCUSDT)");
      return;
    }

    // duplicates warning (UI-level; backend can also enforce policy)
    const omaSet = getOmaManagedSet();
    if (omaSet.has(market)) {
      const ok = confirm(
        `[중복 경고]\n${market} 는 이미 OMA 영역(Active/Watch/Recovery)에 존재합니다.\n\nLongHold는 원칙적으로 OMA와 배타 운용을 권장합니다.\n- OMA쪽: DISABLED 권장\n- LongHold쪽: 알림/관망\n\n그래도 LongHold에 등록/수정할까요?`
      );
      if (!ok) return;
    }

    const cur = lhState.list.find((x) => String(x.market || "").toUpperCase() === market) || {};

    const payload = {
      market,
      enabled: ("enabled" in partial) ? !!partial.enabled : (!!cur.enabled || true),

      strategy: String(partial.strategy || cur.strategy || qs("lhStrategy")?.value || "GAZUA").toUpperCase(),
      target_profit_pct: num(partial.target_profit_pct, num(cur.target_profit_pct, num(qs("lhTargetPct")?.value, 5))) || 5,

      notify_cooldown_sec: Math.round(num(partial.notify_cooldown_sec, num(cur.notify_cooldown_sec, 3600)) || 3600),
      min_position_usdt: Math.round(num(partial.min_position_usdt, num(cur.min_position_usdt, 5)) || 5),

      repeat: ("repeat" in partial) ? !!partial.repeat : (("repeat" in cur) ? !!cur.repeat : true),
      note: String(partial.note || cur.note || ""),
    };
    console.log("[LONGHOLD PAYLOAD]", {partial: partial.target_profit_pct, cur: cur.target_profit_pct, input: qs("lhTargetPct")?.value, final: payload.target_profit_pct});

    try {
      await fetchJson(API.save, { method: "POST", body: JSON.stringify(payload) });
      await refreshList();
      setMsg(`Saved: ${market} (${payload.strategy}) target=${payload.target_profit_pct}%`, true);
    } catch (e) {
      console.error(e);
      setMsg(`Save failed: ${e.message}`, false);
      alert(`Save failed: ${e.message}`);
    }
  }

  async function refreshAiInfo() {
    try {
      const res = await fetchJson(API.aiInfo, { method: "GET" });
      if (res.ok && res.info) {
        const info = res.info;
        const elAcc = qs("aiAcc");
        const elTs = qs("aiTs");
        if (elAcc) elAcc.textContent = (info.accuracy !== undefined) ? fmtPct(info.accuracy * 100, 1) : "—";
        if (elTs) {
            const date = new Date(info.ts * 1000);
            elTs.textContent = date.toLocaleString();
        }
        
        // Fetch system guards for threshold
        try {
            const gRes = await fetchJson(API.guards, { method: "GET" });
            if (gRes.ok && gRes.guards) {
                const thr = gRes.guards.ai_retrain_threshold;
                const elThr = qs("aiThr");
                if (elThr) elThr.textContent = fmtPct(thr * 100, 0);
            }
        } catch (_) {}
        
        // Feature Importance Visualization
        const elFeat = qs("aiFeatList");
        if (elFeat && info.importance) {
            const sorted = Object.entries(info.importance).sort((a,b) => b[1] - a[1]);
            elFeat.innerHTML = sorted.map(([k, v]) => {
                const pct = Math.round(v * 100);
                return `
                    <div class="ai-feat-row">
                        <div class="ai-feat-name" title="${escHtml(k)}">${escHtml(k)}</div>
                        <div class="ai-feat-track"><div class="ai-feat-bar" style="width:${pct}%"></div></div>
                        <div class="ai-feat-val">${pct}%</div>
                    </div>`;
            }).join("");
        }
        
        // Accuracy History Graph
        try {
            const histRes = await fetchJson(API.aiHistory, { method: "GET" });
            const elGraph = qs("aiAccGraph");
            if (elGraph && histRes.ok && Array.isArray(histRes.history) && histRes.history.length > 0) {
                const data = histRes.history; // [{ts, acc}, ...]
                const width = elGraph.clientWidth || 280;
                const height = 60;
                const pad = 4;
                
                // Scales
                const minTs = data[0].ts;
                const maxTs = data[data.length - 1].ts;
                const timeRange = maxTs - minTs || 1;
                
                // SVG Path
                let d = "";
                data.forEach((pt, i) => {
                    const x = ((pt.ts - minTs) / timeRange) * (width - 2 * pad) + pad;
                    // y: 0.0 -> bottom, 1.0 -> top. Invert for SVG.
                    // Scale 0.3~0.8 range to view for better visibility? Or 0~1.
                    // Let's use 0~1 for honesty.
                    const y = height - (pt.acc * height) - pad; 
                    d += (i === 0 ? `M ${x} ${y}` : ` L ${x} ${y}`);
                });
                
                elGraph.innerHTML = `
                    <svg width="100%" height="100%" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
                        <line x1="0" y1="${height/2}" x2="${width}" y2="${height/2}" stroke="#333" stroke-width="1" stroke-dasharray="4"/>
                        <path d="${d}" fill="none" stroke="var(--accent)" stroke-width="2" />
                    </svg>
                    <div class="ai-graph-label">24h Accuracy Trend</div>
                `;
            }
        } catch (_) {}
      }
    } catch (_) {}
  }

  // ---------- AI Controls ----------
  async function triggerAiTraining() {
    const btn = qs("aiTrainBtn");
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Training...";
    }
    
    try {
      const res = await fetchJson(API.aiAuto, { method: "POST" });
      if (res.ok) {
        const acc = res.train ? res.train.accuracy : "N/A";
        alert(`AI Training Complete!\nAccuracy: ${acc}\nModel Reloaded.`);
        await refreshAiInfo();
      } else {
        alert(`AI Training Failed:\n${JSON.stringify(res)}`);
      }
    } catch (e) {
      alert(`Error: ${e.message}`);
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Auto Train AI";
      }
    }
  }

  // ---------- Lightning Management ----------
  function renderLightningTable() {
    const box = qs("lightningList");
    if (!box) return;

    const st = getGlobalState();
    if (!st || !st.system || !st.system.coordinator) {
        box.innerHTML = `<div class="empty">System state not ready</div>`;
        return;
    }

    const contexts = st.system.coordinator;
    const markets = Object.keys(contexts).sort();
    const lightningMarkets = [];

    for (const m of markets) {
        const ctx = contexts[m];
        const ctrls = ctx.controls || {};
        const strat = ctrls.strategy || {};
        // Check if strategy is enabled and mode is LIGHTNING
        if (strat.enabled && String(strat.mode).toUpperCase() === "LIGHTNING") {
            lightningMarkets.push({ market: m, ctx });
        }
    }

    if (lightningMarkets.length === 0) {
        box.innerHTML = `<div class="empty">No active Lightning markets</div>`;
        return;
    }

    const rows = lightningMarkets.map(item => {
        const m = item.market;
        const ctx = item.ctx;
        
        // AI Prediction Score
        let aiScore = "—";
        let aiInf = "0.5";
        try {
            // Try to find ai_prediction in strategy_reason (engine_ai)
            const reason = ctx.strategy ? ctx.strategy.reason : null;
            if (reason && reason.engine_ai && reason.engine_ai.ai_prediction !== undefined) {
                aiScore = fmtNum(reason.engine_ai.ai_prediction, 4);
            }
            
            // Get current AI influence
            const ctrls = ctx.controls || {};
            const strat = ctrls.strategy || {};
            const params = strat.params || {};
            if (params.ai_influence !== undefined) {
                aiInf = fmtNum(params.ai_influence, 2);
            }
        } catch (_) {}

        // PnL
        const upnl = num(ctx.unrealized_profit, 0);
        const pnlCls = upnl > 0 ? "pnl-pos" : (upnl < 0 ? "pnl-neg" : "");

        return `
            <tr>
                <td>${escHtml(m)}</td>
                <td>${aiScore}</td>
                <td><button class="btn btn-ghost" style="padding:2px 6px; font-size:10px;" data-ai-inf="${escHtml(m)}">${aiInf}</button></td>
                <td class="${pnlCls}">${fmtUsdt(upnl)}</td>
                <td class="lh-row-actions">
                    <button class="btn btn-danger" data-lightning-stop="${escHtml(m)}">Stop</button>
                </td>
            </tr>
        `;
    }).join("");

    box.innerHTML = `
        <table class="lh-table">
            <thead><tr><th>Market</th><th>AI Score</th><th>AI Inf</th><th>uPnL</th><th>Action</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>
    `;

    box.querySelectorAll("button[data-lightning-stop]").forEach(btn => {
        btn.addEventListener("click", async () => {
            const m = btn.getAttribute("data-lightning-stop");
            if (!m) return;
            if (!confirm(`Stop Lightning strategy for ${m}?`)) return;
            // Stop engine for this market (which sets OMA to WATCH/DISABLED)
            await fetchJson("/api/engine/stop", { method: "POST", body: JSON.stringify({ market: m }) });
        });
    });
    
    box.querySelectorAll("button[data-ai-inf]").forEach(btn => {
        btn.addEventListener("click", async () => {
            const m = btn.getAttribute("data-ai-inf");
            if (!m) return;
            const cur = btn.textContent;
            const val = prompt(`Set AI Influence for ${m} (0.0 - 1.0):`, cur);
            if (val === null) return;
            const f = parseFloat(val);
            if (isNaN(f) || f < 0 || f > 2) return alert("Invalid value");
            
            // Update strategy params via engine controls
            const payload = { strategy: { params: { ai_influence: f } } };
            await fetchJson(`/api/engine/controls?market=${encodeURIComponent(m)}`, { method: "POST", body: JSON.stringify(payload) });
            
            // Optimistic update & refresh
            btn.textContent = fmtNum(f, 2);
            setTimeout(() => renderLightningTable(), 1000);
        });
    });
    
    const btnThr = qs("aiThr");
    if (btnThr) {
        btnThr.addEventListener("click", async () => {
            const cur = btnThr.textContent.replace("%", "");
            const val = prompt("Set Auto-Retrain Threshold (%):", cur);
            if (val === null) return;
            const f = parseFloat(val) / 100.0;
            if (isNaN(f) || f < 0 || f > 1) return alert("Invalid value");
            await fetchJson(API.guards, { method: "POST", body: JSON.stringify({ ai_retrain_threshold: f }) });
            refreshAiInfo();
        });
    }
  }

  function bindControls() {
    const addBtn = qs("lhAddBtn");
    const refBtn = qs("lhRefreshBtn");
    const mIn = qs("lhMarket");
    const tIn = qs("lhTargetPct");
    const stratSel = qs("lhStrategy");

    if (addBtn) {
      addBtn.addEventListener("click", async () => {
        const market = String(mIn?.value || "").toUpperCase().trim();
        const tgt = num(tIn?.value, 5) || 5;
        const strat = String(stratSel?.value || "GAZUA").toUpperCase();

        await addOrUpdateLongHold({ market, strategy: strat, target_profit_pct: tgt, enabled: true });
      });
    }

    if (refBtn) {
      refBtn.addEventListener("click", async () => {
        await refreshList();
        await refreshCandidates();
      });
    }

    // Enter key -> Add
    if (mIn) {
      mIn.addEventListener("keydown", async (ev) => {
        if (ev.key === "Enter") {
          ev.preventDefault();
          const market = String(mIn.value || "").toUpperCase().trim();
          const tgt = num(tIn?.value, 50) || 50;
          const strat = String(stratSel?.value || "GAZUA").toUpperCase();
          await addOrUpdateLongHold({ market, strategy: strat, target_profit_pct: tgt, enabled: true });
        }
      });
    }
    
    const aiBtn = qs("aiTrainBtn");
    if (aiBtn) {
      aiBtn.addEventListener("click", triggerAiTraining);
    }

    const lnAddBtn = qs("lightningAddBtn");
    if (lnAddBtn) {
        lnAddBtn.addEventListener("click", async () => {
            const inp = qs("lightningInput");
            const m = String(inp?.value || "").toUpperCase().trim();
            if (!m) return alert("Market required");
            await fetchJson(`/api/engine/start?market=${encodeURIComponent(m)}`, { method: "POST" });
        });
    }
  }

  function bindAutocomplete() {
    const input = qs("lhMarket");
    if (!input) return;

    // Create datalist once
    let dl = qs("lhMarketDatalist");
    if (!dl) {
      dl = document.createElement("datalist");
      dl.id = "lhMarketDatalist";
      document.body.appendChild(dl);
      input.setAttribute("list", "lhMarketDatalist");
    }

    const st = getGlobalState();
    const all = st && Array.isArray(st.allKnownMarkets) ? st.allKnownMarkets : [];
    if (!all.length) return;

    // Fill at most 400 to keep DOM small
    const take = all.slice(0, 400);
    dl.innerHTML = take.map((m) => `<option value="${escHtml(String(m).toUpperCase())}"></option>`).join("");
  }

  // ---------- boot ----------
  async function boot() {
    // Only boot when panel exists (safe for older dashboards)
    if (!qs("longholdPanel")) return;
    
    // Inject AI Control UI if missing
    const lhPanel = qs("longholdPanel");
    if (!qs("aiPanel")) {
        const div = document.createElement("div");
        div.id = "aiPanel";
        div.className = "panel";
        div.innerHTML = `
            <div class="panel-head"><h3>AI & Lightning</h3></div>
            
            <!-- AI Section -->
            <div class="ai-section">
                <div class="ai-stats-row">
                    <div class="stat-item"><label>Accuracy</label><span id="aiAcc">—</span></div>
                    <div class="stat-item"><label>Last Trained</label><span id="aiTs" style="font-size:11px">—</span></div>
                    <div class="stat-item"><label>Retrain Thr</label><span id="aiThr" style="cursor:pointer; text-decoration:underline; color:var(--text-main);">—</span></div>
                </div>
                <div id="aiAccGraph" class="ai-graph-box"></div>
                <div id="aiFeatList" class="ai-feat-list" style="margin-top:10px;"></div>
                <button id="aiTrainBtn" class="btn" style="width:100%; margin-top:8px;">Auto Train AI</button>
                <div class="subtle" style="margin-top:4px; font-size:10px;">Extracts ledger -> Trains Model -> Reloads Engine</div>
            </div>

            <div class="mt-divider"></div>
        `;
        lhPanel.parentNode.insertBefore(div, lhPanel.nextSibling);
    }

    bindControls();
    refreshAiInfo();

    // initial list fetch (fast)
    await refreshList();

    // candidates: user-triggered by default (avoid heavy scans)
    renderCandidates();

    // keep list fresh
    if (IntervalManager) {
      IntervalManager.set("longhold_poll", async () => {
        try {
          bindAutocomplete();
          await refreshList();
          renderLightningTable();
        } catch (_) {}
      }, LIST_POLL_MS);
    } else {
      setInterval(async () => {
        try {
          bindAutocomplete();
          await refreshList();
          renderLightningTable();
        } catch (_) {}
      }, LIST_POLL_MS);
    }
  }

  document.addEventListener("DOMContentLoaded", boot);
} catch (e) {
    console.error('[LongHold] init failed', e);
  }
})();