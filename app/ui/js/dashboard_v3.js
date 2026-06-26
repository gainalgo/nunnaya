/* ============================================================
   dashboard_v3.js — bot v4 entry point (multi-select view + engine buttons + WS real-time)
   Left switches = multi-select (main display) / engine on·off = trade panel header Engine buttons / WS real-time
   ※ ribbon.js, tab_entry.js use window.V3 — this file initializes after ribbon.js loads
   ============================================================ */
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const V3 = window.V3 = window.V3 || {};
  V3.$ = $;
  V3.state = { selected: new Set(['focus']), order: ['focus'], active: 'focus', homeView: true, envView: false, pcommonView: false, markets: [], widgets: { summary: false, positions: true, dow: false, slot: false, phasek: false, regime: false, news: false, report: false, journal: true, peer: false, scan: false, manual: false, mentry: true }, widgetOrder: null,
    journal: { page: 1, strategy: '', market: '', limit: 7, markets: null, data: null, summary: null, snaps: null, t: 0 },
    gp: { market: '', data: null, t: 0, rotateIdx: 0 },
    phaseK: { data: null, t: 0 },
    manual: { market: 'XAUTUSDT', dir: 'LONG', tfp: null, tfpT: 0 },
    hpwidgets: { stats: true, scalp: true, link: true, history: true },
    harpoon: { status: null, history: [], t: 0 },
    lightning: { items: null, t: 0, recos: null, recosT: 0, showRecos: true, recoRows: 5, recoPage: 1 },
    sniper: { items: null, t: 0, recos: null, recosT: 0, showRecos: true, recoRows: 5, recoPage: 1 },
    ladder: { items: null, t: 0, steps: null, stepsMarket: null, orders: null },   // 📐 steps=computed plan / orders=live orders (grid_state, uuid)
    pcommon: { data: null, t: 0 },
    home: { pos: null, reco: null, t: 0, show: { status: true, quick: true, positions: true, reco: true } } };   // 🏠 overall status (show=right-side widget toggles)

  // 🗂️ Layer (widget visibility) persistent restore — keep last settings after view switch/restart [2026-06-19 owner "resets every time"]
  //   restore widgets(v3-w-*)·hpwidgets(v3-hw-*)·home.show(v3-hm-*) from localStorage('v3-layers').
  try {
    var _L = JSON.parse(localStorage.getItem('v3-layers') || 'null');
    if (_L) {
      if (_L.widgets) Object.assign(V3.state.widgets, _L.widgets);
      if (_L.hpwidgets) Object.assign(V3.state.hpwidgets, _L.hpwidgets);
      if (_L.homeShow && V3.state.home && V3.state.home.show) Object.assign(V3.state.home.show, _L.homeShow);
    }
  } catch (_e) { /* noop */ }

  // ── Shared helpers ──
  V3.authFetch = async (url, opts = {}) => {
    const cfg = Object.assign({}, opts);
    const timeoutMs = cfg.timeoutMs == null ? 12000 : Number(cfg.timeoutMs || 0);
    delete cfg.timeoutMs;
    let timer = null;
    if (timeoutMs > 0 && !cfg.signal && window.AbortController) {
      const ctrl = new AbortController();
      cfg.signal = ctrl.signal;
      timer = setTimeout(() => ctrl.abort(), timeoutMs);
    }
    try {
      const r = await fetch(url, Object.assign({ credentials: 'include' }, cfg));
      if (r.status === 401) V3.toast('Session expired — please log in again', 'err', 6000);
      return r;
    } finally {
      if (timer) clearTimeout(timer);
    }
  };
  V3.getJSON = async (url, opts) => {
    try { return await (await V3.authFetch(url, opts)).json(); }
    catch (e) { return { ok: false, error: String(e) }; }
  };
  V3.toast = (msg, type = 'info', ms = 3500) => {
    const box = $('v3-toasts'); if (!box) return;
    const el = document.createElement('div');
    el.className = 'v3-toast ' + type; el.textContent = msg;
    box.appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 200); }, ms);
  };
  V3.confirm = (title, bodyHtml) => new Promise((resolve) => {
    const back = $('v3-confirm-back');
    if (!back) { resolve(window.confirm(title)); return; }
    $('v3-confirm-title').textContent = title;
    $('v3-confirm-body').innerHTML = bodyHtml;
    back.classList.add('show');
    const ok = $('v3-confirm-ok'), cc = $('v3-confirm-cancel');
    const done = (v) => { back.classList.remove('show'); ok.removeEventListener('click', oo); cc.removeEventListener('click', oc); resolve(v); };
    const oo = () => done(true), oc = () => done(false);
    ok.addEventListener('click', oo); cc.addEventListener('click', oc);
  });
  V3.fmtPnl = (v) => (v == null || isNaN(v)) ? '—' : ((v >= 0 ? '+' : '') + Number(v).toFixed(2));
  V3.pnlCls = (v) => (v == null || isNaN(v)) ? '' : (v >= 0 ? 'v3-pos' : 'v3-neg');
  V3.openBybitTrade = (market) => {
    if (!market) return;
    const sym = String(market).toUpperCase();
    const symbol = sym.endsWith('USDT') ? sym : sym + 'USDT';
    // ★ [2026-06-23 audit low] Exchange-specific URL — in the Binance window (?ex=binance_futures, shim sets html[data-ex]),
    //   prevents accidentally opening bybit.com on click, which led to manual misfires.
    const _isBnc = document.documentElement.getAttribute('data-ex') === 'binance_futures';
    const url = _isBnc ? ('https://www.binance.com/en/futures/' + symbol)
                       : ('https://www.bybit.com/trade/usdt/' + symbol);
    try { const w = window.open(url, 'ex_trade_window'); if (w) w.focus(); } catch (e) { /* noop */ }
  };

  const LABEL = { focus: 'FOCUS', harpoon: 'HARPOON', lightning: 'LIGHTNING', sniper: 'SNIPER', gazua: 'GAZUA', contrarian: 'CONTRARIAN', ladder: 'LADDER', pingpong: 'PINGPONG', autoloop: 'AUTOLOOP', whale: 'WHALE', env: 'Settings (Common)' };
  const ICON = { focus: '◎', harpoon: '🐟', lightning: '⚡', sniper: '🎯', gazua: '🚀', contrarian: '🔄', ladder: '📐', pingpong: '🏓', autoloop: '🔁', whale: '🐋' };
  const TREE_ORDER = ['focus', 'harpoon', 'lightning', 'sniper', 'gazua', 'contrarian', 'ladder', 'pingpong', 'autoloop', 'whale'];
  const TIER2 = ['pingpong', 'autoloop', 'whale'];   // autopilot slot-type — shared 'tier2' ribbon/widget container
  // 🎛️ Per-Tier-2-strategy unique tuning (applied at slot-fill · saved via /api/reserved/plugin-params) [param, label, default]
  const TIER2_TUNE = {
    pingpong: [['rsi_buy', 'RSI Buy', 30], ['rsi_sell', 'RSI Sell', 70], ['pp_tp_pct', 'TP %', 3.0], ['pp_sl_pct', 'SL %', -2.0], ['pp_entry_gap_pct', 'Entry Gap %', 0.35]],
    autoloop: [['rsi_buy', 'RSI Buy', 28], ['rsi_sell', 'RSI Sell', 58], ['tp_pct', 'TP %', 2.5], ['sl_pct', 'SL %', -2.5], ['trailing_pct', 'Trail %', 1.2]],
    whale: [['rsi_entry_max', 'RSI Entry Max', 30], ['rsi_exit_min', 'RSI Exit Min', 65], ['cloud_min_thickness_pct', 'Cloud Min Thickness %', 1.5], ['vol_spike_ratio', 'Volume Spike Ratio', 2.0], ['vol_lookback', 'Volume lookback', 20], ['tp_pct', 'TP %', 2.0], ['sl_pct', 'SL %', 3.0]],
  };
  // active strategy → ribbon/right-side container key (focus / harpoon / plugin / common)
  function engineOf(name) {
    if (TIER2.includes(name)) return 'tier2';   // 3 slot-type strategies share one container
    if (name === 'focus' || name === 'harpoon' || name === 'lightning' || name === 'sniper' || name === 'ladder' || (typeof PLUG !== 'undefined' && PLUG[name])) return name;   // has its own ribbon/widget container (wired strategy)
    return name === 'env' ? 'common' : 'plugin';
  }
  // active strategy switch: fold the top ribbon + right-side widget panel to that strategy's (previous active collapses)
  function applyActivePanels() {
    const key = V3.state.homeView ? 'home' : (V3.state.envView || V3.state.pcommonView) ? 'common' : engineOf(V3.state.active);
    if (V3.ribbonSetActive) V3.ribbonSetActive(key);
    document.querySelectorAll('.v3-wgroup').forEach((g) => {
      const st = g.dataset.wpanelStrat;
      // 'shared' (FOCUS results: Trade Journal·Peer Scanner) = shown in both home + FOCUS. Others only in their own view. (2026-06-07 owner)
      g.classList.toggle('show', st === key || (st === 'shared' && (V3.state.homeView || key === 'focus')));
    });
    if (key === 'harpoon') loadHarpoon(true);   // fill ribbon (even if not in the main stack)
    if (key === 'lightning') { loadLightning(true); loadLightningGuards(); loadLightningRecos(true); }
    if (key === 'sniper') { loadSniper(true); loadSniperRecos(true); }
    if (PLUG[key]) { loadPlug(key, true); loadPlugRecos(key, true); }   // generic plugins (GAZUA etc.)
    if (key === 'ladder') loadLadder(true);   // 📐 read-only
    if (key === 'tier2') { const rb = $('v3-tier2-ribbon-body'); if (rb) rb.innerHTML = tier2RibbonHtml(V3.state.active); loadPluginsCommon(); loadTier2Tune(); loadTier2Work(); loadTier2Reco(V3.state.active); }   // 🤖 ribbon = inject active plugin slots·tuning + main work status + recommendations
  }
  V3.applyActivePanels = applyActivePanels;

  function chip(k, v, cls) { return '<div class="v3-chip"><span class="k">' + k + '</span><span class="v ' + (cls || '') + '">' + v + '</span></div>'; }
  function _fp(v) { if (!v || v === 0) return '-'; v = Number(v); return v >= 100 ? '$' + v.toFixed(2) : v >= 1 ? '$' + v.toFixed(4) : '$' + v.toFixed(6); }

  // Summary = v2 card (de-duped: engine status=block header, total PnL/slots already in Positions header)
  function summaryHtml(d) {
    const c = d.config || {};
    return '<div class="v3-trade-summary">' +
      chip('Budget', (c.budget_usdt && Number(c.budget_usdt) > 0) ? ('$' + c.budget_usdt) : '$Auto', '') +
      chip('Leverage', (c.leverage ?? '—') + 'x', '') +
      chip('Daily Plans', (d.daily_plans_used ?? 0) + ' / ' + (d.daily_plans_max ?? '—'), '') +
      chip('Daily SL', (d.daily_sl_count ?? 0) + ' / ' + (d.daily_sl_max ?? '—'), '') +
      chip('Today PnL', '$' + V3.fmtPnl(d.today_pnl), V3.pnlCls(d.today_pnl)) +
      chip('Lock', c.lock_market || '—', '') +
      '</div>';
  }

  // 📋 Positions widget — v2 trade-panel table as-is (11 columns + Net + progress bar + ✕/Exit/CloseAll)
  // Shared position-row cells (Market·Dir·Margin·Entry·Current·PnL+Net·TP1·SL·Progress·Hold) — used by FOCUS table + 🏠 home
  function posCells(p, tag) {
    const pnl = p.unrealized_pnl || 0, pnlPct = p.pnl_pct || 0, isLong = p.direction === 'LONG', cur = p.current_price || 0;
    const qty = p.qty || 0, lev = Math.max(p.leverage || 1, 1);
    const fees = ((p.entry_price || 0) * qty + cur * qty) * 0.00055;
    const net = pnl - fees;
    let prog = 0;
    if (p.tp1 && p.sl && p.entry_price) {
      const range = Math.abs(p.tp1 - p.entry_price), moved = isLong ? (cur - p.entry_price) : (p.entry_price - cur);
      prog = range > 0 ? Math.min(100, Math.max(-50, moved / range * 100)) : 0;
    }
    const pc = pnl >= 0 ? 'v3-pos' : 'v3-neg', nc = net >= 0 ? 'v3-pos' : 'v3-neg';
    const gc = prog >= 0 ? 'var(--v3-long)' : 'var(--v3-short)';
    const holdH = p.hold_hours || 0, holdStr = holdH >= 1 ? holdH.toFixed(1) + 'h' : Math.round(holdH * 60) + 'm';
    const mShort = (p.market || '').replace('USDT', '');
    return '<td><b class="v3-mkt" data-bybit="' + p.market + '">' + mShort + '</b> <small style="color:var(--v3-fg-mute)">' + (tag || '') + '</small></td>' +
      '<td><span class="v3-badge ' + (isLong ? 'long' : 'short') + '">' + p.direction + '</span></td>' +
      '<td>' + ((p.entry_price || 0) * qty / lev).toFixed(2) + ' USDT</td>' +
      '<td>' + _fp(p.entry_price) + '</td>' +
      '<td>' + _fp(cur) + '</td>' +
      '<td class="v3-pnl ' + pc + '">' + (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + ' USDT <small>(' + (pnlPct >= 0 ? '+' : '') + pnlPct.toFixed(2) + '%)</small><br><small class="' + nc + '">Net ' + (net >= 0 ? '+' : '') + net.toFixed(2) + ' USDT</small></td>' +
      '<td>' + _fp(p.tp1) + '</td>' +
      '<td class="v3-neg">' + _fp(p.sl) + (p.breakeven_locked ? ((isLong ? p.sl >= p.entry_price : p.sl <= p.entry_price) ? ' <small style="color:var(--v3-warn)">BE</small>' : ' <small style="color:var(--v3-neg)" title="BE-lock flag is set but SL is below breakeven = fake BE (not protected)">BE✗</small>') : '') + '</td>' +
      '<td><div class="v3-prog"><div style="width:' + Math.abs(prog) + '%;background:' + gc + ';"></div></div><small style="color:var(--v3-fg-mute)">' + prog.toFixed(0) + '%</small></td>' +
      '<td>' + holdStr + '</td>';
  }
  function positionsHtml(d) {
    const pos = d.positions || (d.position ? [d.position] : []);
    const tPnl = d.total_pnl || 0;
    const head = '<div class="v3-pos-head"><span class="v3-pos-title">📋 Positions ' +
      '<span class="v3-badge mute">' + pos.length + ' / ' + (d.max_positions ?? 5) + '</span> ' +
      '<span class="' + V3.pnlCls(tPnl) + '">' + (tPnl >= 0 ? '+' : '') + tPnl.toFixed(2) + ' USDT</span></span>' +
      '<span class="v3-pos-actions">' +
      '<button class="v3-btn sm ghost" id="focus-btn-exit-selected" disabled>Exit Selected</button>' +
      '<button class="v3-btn sm v3-btn-outline-danger" id="focus-btn-close-all">✕ Close All</button></span></div>';
    if (!pos.length) return head + '<div class="v3-placeholder">No active positions</div>';
    const rows = pos.map((p) => '<tr>' +
      '<td><input type="checkbox" class="focus-pos-chk" data-market="' + p.market + '"></td>' +
      posCells(p, p.entry_source === 'scanner' ? 'S' : 'P') +
      '<td><button class="focus-btn-close-one" data-market="' + p.market + '" title="Close ' + p.market + '">✕</button></td>' +
      '</tr>').join('');
    return head + '<table class="v3-postable v3-postable-pos"><thead><tr>' +
      '<th style="width:22px"><input type="checkbox" id="focus-chk-all"></th><th>Market</th><th>Dir</th><th>Margin</th><th>Entry</th><th>Current</th><th>PnL</th><th>TP1</th><th>SL</th><th>Progress</th><th>Hold</th><th></th>' +
      '</tr></thead><tbody>' + rows + '</tbody></table>';
  }

  // ── Phase 3 widgets (analytics, KST) — shown via right-side toggle, short tables laid out side by side ──
  function _wkv(label, val, cls) { return '<div class="rib-row"><span>' + label + '</span><span class="' + (cls || '') + '">' + val + '</span></div>'; }
  function _dirBadge(x) { const c = x === 'LONG' ? 'long' : x === 'SHORT' ? 'short' : 'mute'; return '<span class="v3-badge ' + c + '">' + (x || '—') + '</span>'; }
  function _grade(g) { const c = (g === 'S' || g === 'A') ? 'v3-pos' : (g === 'D' || g === 'F') ? 'v3-neg' : ''; return '<b class="' + c + '">' + (g || '?') + '</b>'; }
  function _money(v) { v = Number(v) || 0; return '<span class="' + (v >= 0 ? 'v3-pos' : 'v3-neg') + '">' + (v >= 0 ? '+' : '') + '$' + v.toFixed(2) + '</span>'; }

  function renderDow(d) {
    const days = (d && d.days) || [];
    if (!days.length) return '<div class="v3-widget-h">📅 Day of Week <small>(KST)</small></div><div class="v3-placeholder">No data (exit records needed)</div>';
    const rows = days.map((x) => '<tr><td>' + x.day + '</td><td class="' + V3.pnlCls(x.pnl) + '">' + (x.pnl >= 0 ? '+' : '') + '$' + x.pnl.toFixed(2) + '</td><td>' + x.trades + '</td><td>' + x.win_rate + '%</td></tr>').join('');
    return '<div class="v3-widget-h">📅 Day of Week <small>(KST · by net PnL)</small></div>' +
      '<table class="v3-wtable"><thead><tr><th>Day</th><th>PnL</th><th>Trades</th><th>Win%</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  function renderSlot(d) {
    const slots = (d && d.slots) || [];
    if (!slots.length) return '<div class="v3-widget-h">⏱️ 4H Slot <small>(KST)</small></div><div class="v3-placeholder">No data (exit records needed)</div>';
    const rows = slots.map((x) => '<tr><td>' + x.slot + '</td><td class="' + V3.pnlCls(x.pnl) + '">' + (x.pnl >= 0 ? '+' : '') + '$' + x.pnl.toFixed(2) + '</td><td>' + x.trades + '</td><td>' + x.win_rate + '%</td></tr>').join('');
    return '<div class="v3-widget-h">⏱️ 4H Slot <small>(KST 07:00 baseline)</small></div>' +
      '<table class="v3-wtable"><thead><tr><th>Slot</th><th>PnL</th><th>Trades</th><th>Win%</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  function renderRegime(d) {
    if (!d || !d.ok) return '<div class="v3-widget-h">🔭 Day Direction</div><div class="v3-placeholder">Load failed</div>';
    return '<div class="v3-widget-h">🔭 Day Direction <small>(09:00 KST baseline)</small></div>' +
      _wkv('Direction', _dirBadge(d.day_direction)) +
      _wkv('Date', d.date || '—') +
      _wkv('H4 ATR%', (d.h4_atr_pct != null ? Number(d.h4_atr_pct).toFixed(2) + '%' : '—')) +
      _wkv('TP1 / TP2 exp', Number(d.tp1_expected_pct || 0).toFixed(2) + '% / ' + Number(d.tp2_expected_pct || 0).toFixed(2) + '%') +
      (d.reason ? '<div class="v3-wnote">' + d.reason + '</div>' : '');
  }
  function renderNews(d) {
    const o = (d && d.overall) || {};
    const sc = o.score != null ? Number(o.score).toFixed(2) : '—';
    const scls = o.score > 0.05 ? 'v3-pos' : o.score < -0.05 ? 'v3-neg' : '';
    const cb = o.conviction_bonus;
    const heads = ((o.headlines) || []).slice(0, 4).map((h) => '<div class="v3-wnote">• ' + String(h.title || '').slice(0, 64) + '</div>').join('');
    return '<div class="v3-widget-h">📰 News Sentiment</div>' +
      _wkv('Overall', '<span class="' + scls + '">' + (o.label || '—') + ' (' + sc + ')</span>') +
      _wkv('Conv bonus', (cb != null ? (cb >= 0 ? '+' : '') + cb : '—')) +
      _wkv('FOCUS link', (d && d.config && d.config.focus_enabled) ? 'ON' : 'OFF') +
      (heads || '<div class="v3-wnote" style="opacity:.6">No headlines</div>');
  }
  function renderReport(d) {
    const rk = (d && d.rankings) || [], coins = (d && d.coins) || {};
    if (!rk.length) return '<div class="v3-widget-h">🏅 Coin Report Card</div><div class="v3-placeholder">No data</div>';
    const rows = rk.map((r) => {
      const c = coins[r.coin] || {}, tr = c.trades || 0, wr = c.win_rate || 0, wins = Math.round(tr * wr / 100), pf = c.profit_factor;
      const pfStr = (pf == null) ? '-' : (!isFinite(pf) || pf >= 99) ? '∞' : Number(pf).toFixed(1);
      const ap = Number(c.avg_pnl || 0), tp = Number(r.pnl != null ? r.pnl : (c.total_pnl || 0));
      return '<tr><td>' + r.rank + '</td><td>' + String(r.coin || '').replace('USDT', '') + '</td><td>' + _grade(r.grade) + '</td><td>' + Number(r.score || 0).toFixed(1) + '</td>' +
        '<td>' + tr + ' <small style="color:var(--v3-fg-mute)">(' + wins + 'W/' + (tr - wins) + 'L)</small></td><td>' + wr + '%</td>' +
        '<td class="' + V3.pnlCls(tp) + '">' + (tp >= 0 ? '+' : '') + '$' + tp.toFixed(2) + '</td>' +
        '<td class="' + V3.pnlCls(ap) + '">' + (ap >= 0 ? '+' : '') + '$' + ap.toFixed(2) + '</td><td>' + pfStr + '</td></tr>';
    }).join('');
    return '<div class="v3-widget-h">🏅 Coin Report Card <small>(' + (d.total_coins || rk.length) + ')</small></div>' +
      '<table class="v3-ltable"><thead><tr><th>#</th><th>Coin</th><th>Grade</th><th>Score</th><th>Trades</th><th>Win%</th><th>PnL</th><th>Avg</th><th>PF</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  // ── 📓 Trade Journal — v2 verbatim port (filters All/All Coins/Rows + paging + Daily PnL chart + 5 summary cards + 12 columns) ──
  function _jCols(showStrat) {
    return '<th>Time</th>' + (showStrat ? '<th>Strategy</th>' : '') + '<th>Event</th><th>Market</th><th>Dir</th><th>Entry</th><th>Exit</th><th>PnL</th><th>ROE%</th><th>Hold</th><th>Reason</th><th>DT</th>';
  }
  function _jPager(cur, total) {
    if (total <= 1) return '';
    const pages = [];
    if (total <= 10) { for (let i = 1; i <= total; i++) pages.push(i); }
    else {
      pages.push(1);
      if (cur > 4) pages.push('...');
      for (let i = Math.max(2, cur - 2); i <= Math.min(total - 1, cur + 2); i++) pages.push(i);
      if (cur < total - 3) pages.push('...');
      pages.push(total);
    }
    return pages.map((p) => p === '...' ? '<span class="v3-jpg dis">…</span>'
      : p === cur ? '<span class="v3-jpg cur">' + p + '</span>'
      : '<button class="v3-jpg v3-journal-page" data-page="' + p + '">' + p + '</button>').join('');
  }
  // Exit reason → readable label + BE-trigger marker (owner 2026-06-08: distinguish trailing/BE/SL)
  function _journalReason(t) {
    const raw = String(t.exit_reason || '');
    if (!raw || raw === '-') return '-';
    const tip = raw.replace(/"/g, "'");
    // If exited after BE (breakeven) lock triggered, add prefix (muted — keep text light, owner's taste)
    const be = (t.event === 'EXIT' && t.breakeven_locked) ? '<span title="Exited after BE (breakeven) lock triggered" style="color:var(--v3-fg-mute)">BE·</span>' : '';
    let lab;
    const _pnl = Number(t.pnl_net || 0);
    if (raw.indexOf('auto_tp_trail') === 0) lab = '🏆 Trailing Take';
    else if (raw.indexOf('auto_sl_pct') === 0) lab = '🛑 Auto Loss Cut';
    // SL fill = use PnL to tell where the SL was (loss / breakeven / profit-protect)
    else if (raw.indexOf('SERVER_SL') >= 0 || raw === 'SL') {
      lab = _pnl > 0.02 ? '🛡️ SL Profit-Protect' : (_pnl >= -0.05 ? '🛡️ SL Breakeven' : '🛑 SL Stop-Loss');
    }
    else if (raw.indexOf('SERVER_SIDE') >= 0) {
      lab = (_pnl >= 0 ? '🛡️' : '🛑') + ' Exchange Exit' + (_pnl > 0.02 ? '(profit)' : _pnl < -0.05 ? '(loss)' : '(breakeven)');
    }
    else if (raw.indexOf('manual') === 0) lab = '✋ Manual';
    else if (raw.indexOf('reverse_drift') === 0) lab = '↩️ Reverse Cut';
    else if (raw.indexOf('charge') === 0) lab = '⚡ Charge';
    else if (raw.indexOf('erosion') >= 0) lab = '🩹 Erosion→BE';
    else if (raw.indexOf('morning_shield') === 0) lab = '🌅 Morning Shield';
    else if (raw.indexOf('event_shield') === 0) lab = '⏰ Event Shield';
    else if (raw.indexOf('macro') >= 0) lab = '🧭 Regime Reverse';
    else if (raw.indexOf('caution') >= 0) lab = '↔️ Range Profit-Lock';
    else if (raw.indexOf('stall') >= 0) lab = '⏸️ BE Stall Cut';
    else if (raw.indexOf('5m_emergency') >= 0 || raw.indexOf('5M') >= 0) lab = '🚨 5M Emergency';
    else if (raw.indexOf('take_profit') >= 0 || raw === 'TP' || raw.indexOf('TP_') >= 0) lab = '🎯 TP';
    else lab = raw.split(':')[0].split('(')[0];   // otherwise = prefix only (trim values/numbers for cleanliness)
    return be + '<span title="' + tip + '">' + lab + '</span>';
  }

  function renderJournal() {
    const j = V3.state.journal;
    const tr = j.data || {}, trades = tr.trades || [];
    const sums = (j.summary && j.summary.summary) || {};
    // If All filter, sum FOCUS+HARPOON (v2 refreshJournal combined logic as-is)
    let s;
    if (!j.strategy) {
      const f = sums.FOCUS || {}, hp = sums.HARPOON || {}, c = Object.assign({}, sums.combined || {});
      const totEx = (f.total_trades || 0) + (hp.total_trades || 0), totW = (f.wins || 0) + (hp.wins || 0);
      c.win_rate = totEx > 0 ? (totW / totEx * 100) : 0;
      c.win_pnl = (f.win_pnl || 0) + (hp.win_pnl || 0); c.loss_pnl = (f.loss_pnl || 0) + (hp.loss_pnl || 0);
      c.dt_trades = (f.dt_trades || 0) + (hp.dt_trades || 0); c.dt_pnl = (f.dt_pnl || 0) + (hp.dt_pnl || 0);
      c.no_dt_trades = (f.no_dt_trades || 0) + (hp.no_dt_trades || 0); c.no_dt_pnl = (f.no_dt_pnl || 0) + (hp.no_dt_pnl || 0);
      c.today_pnl = (f.today_pnl || 0) + (hp.today_pnl || 0);
      s = c;
    } else s = sums[j.strategy] || {};
    // Header + filters (All / All Coins / Rows)
    const optS = ['', 'FOCUS', 'HARPOON'].map((v) => '<option value="' + v + '"' + (j.strategy === v ? ' selected' : '') + '>' + (v || 'All') + '</option>').join('');
    const optM = '<option value="">All Coins</option>' + (j.markets || []).map((m) => '<option value="' + m + '"' + (j.market === m ? ' selected' : '') + '>' + m.replace('USDT', '') + '</option>').join('');
    let h = '<div class="v3-jhead"><span class="v3-widget-h" style="margin:0">📓 Trade Journal</span></div>';
    // Filter bar = moved to the right of the summary-card row (just above the chart, at the DT-number height) (owner "felt out of place")
    const filterBar = '<span class="v3-jfilter">' +
      '<button id="v3-daily-refresh" class="v3-btn sm ghost" title="Refresh Daily PnL chart">↻ Daily PnL</button>' +
      '<select id="v3-journal-filter" class="v3-mini">' + optS + '</select>' +
      '<select id="v3-journal-market" class="v3-mini">' + optM + '</select>' +
      '<span class="v3-jrows">Rows <input id="v3-journal-limit" class="v3-mini" type="number" min="5" max="500" value="' + j.limit + '" style="width:52px;text-align:right"></span>' +
      '<button id="v3-journal-refresh" class="v3-btn sm ghost" title="Refresh">🔄</button>' +
      '</span>';
    // 5 summary cards — Total PnL / Today PnL / Trades / Win Rate (count% + amount% amt) / DT vs No-DT (v2 _renderSummaryCards as-is)
    const winRate = Number(s.win_rate || 0);
    const winPnl = s.win_pnl || 0, lossPnl = Math.abs(s.loss_pnl || 0), totWl = winPnl + lossPnl;
    const amtWr = totWl > 0 ? (winPnl / totWl * 100) : null;
    const dtN = s.dt_trades || 0, noN = s.no_dt_trades || 0;
    const dtAvg = dtN > 0 ? (s.dt_pnl / dtN) : 0, noAvg = noN > 0 ? (s.no_dt_pnl / noN) : 0;
    const dtCmp = (dtN === 0 && noN === 0) ? '-' : '<span class="' + V3.pnlCls(dtAvg) + '">DT:$' + dtAvg.toFixed(2) + '</span> <span class="text-muted">vs</span> <span class="' + V3.pnlCls(noAvg) + '">Off:$' + noAvg.toFixed(2) + '</span>';
    h += '<div class="v3-jsumrow"><div class="v3-trade-summary v3-jsummary">' +
      chip('Total PnL', _money(s.total_pnl)) +
      chip('Today PnL', _money(s.today_pnl)) +
      chip('Trades', (s.total_trades || 0)) +
      chip('Win Rate', winRate.toFixed(1) + '% ' + (amtWr != null ? '<small class="text-muted">' + amtWr.toFixed(1) + '% amt</small>' : '')) +
      chip('DT vs No-DT', dtCmp) +
      '</div>' + filterBar + '</div>';
    // Daily PnL History chart
    const haveSnaps = j.snaps && j.snaps.length;
    h += '<div class="v3-daily-wrap">' +
      '<div id="v3-daily-chart" class="v3-daily-chart"><span class="v3-daily-cap">Daily PnL History</span>' +
      '<div id="v3-daily-ph" class="v3-daily-ph">' + (j.snaps == null ? 'Loading daily data…' : (haveSnaps ? '' : 'No daily data yet')) + '</div>' +
      '<canvas id="v3-daily-canvas" style="display:' + (haveSnaps ? 'block' : 'none') + ';width:100%;height:100%"></canvas></div></div>';
    // Trade table (12 columns; show Strategy column when All)
    const showStrat = !j.strategy;
    if (!trades.length) {
      return h + '<table class="v3-ltable v3-jtable"><thead><tr>' + _jCols(showStrat) + '</tr></thead><tbody><tr><td colspan="' + (showStrat ? 12 : 11) + '" class="v3-jempty">No trade records</td></tr></tbody></table>' +
        '<div class="v3-jpager">' + _jPager(tr.page || 1, tr.total_pages || 1) + '</div>';
    }
    const rows = trades.slice().reverse().map((t) => {
      const ex = t.event === 'EXIT', pa = t.event === 'PARTIAL';
      const dt = new Date((t.ts || 0) * 1000);
      const tm = (dt.getMonth() + 1) + '/' + dt.getDate() + ' ' + String(dt.getHours()).padStart(2, '0') + ':' + String(dt.getMinutes()).padStart(2, '0');
      const evt = t.event === 'ENTRY' ? '<span class="v3-badge" style="color:var(--v3-accent);border-color:var(--v3-accent)">ENTRY</span>'
        : pa ? '<span class="v3-badge" style="color:#3bc9db;border-color:#3bc9db">PARTIAL</span>'
        : '<span class="v3-badge warn">EXIT</span>';
      const dir = '<span class="v3-badge ' + (t.direction === 'LONG' ? 'long' : 'short') + '">' + (t.direction || '') + '</span>';
      const entry = (t.entry_price && t.entry_price > 0) ? _fp(t.entry_price) : (t.event === 'ENTRY' ? _fp(t.price) : '-');
      const exit = (ex || pa) ? _fp(t.price) : '-';
      const pn = (ex || pa) ? Number(t.pnl_net || 0) : null;
      const pnStr = pn != null ? '<span class="' + V3.pnlCls(pn) + '">' + (pn >= 0 ? '+' : '') + '$' + pn.toFixed(2) + '</span>' : '-';
      const roe = ex ? Number(t.roe_pct || 0) : null;
      const roeStr = roe != null ? '<span class="' + V3.pnlCls(roe) + '">' + (roe >= 0 ? '+' : '') + roe.toFixed(1) + '%</span>' : '-';
      const hs = t.hold_sec || 0;
      const hold = ex ? (hs < 60 ? Math.round(hs) + 's' : hs < 3600 ? Math.round(hs / 60) + 'm' : Math.floor(hs / 3600) + 'h' + (Math.round(hs % 3600 / 60) > 0 ? Math.round(hs % 3600 / 60) + 'm' : '')) : '-';
      const dtIc = t.dynamic_trailing ? (t.breakeven_locked ? '🔒' : '📈') : '<span class="text-muted">-</span>';
      return '<tr><td>' + tm + '</td>' +
        (showStrat ? '<td><span class="v3-badge mute">' + (t.strategy || '') + '</span></td>' : '') +
        '<td>' + evt + '</td>' +
        '<td><b class="v3-mkt" data-bybit="' + (t.market || '') + '">' + (t.market || '-') + '</b></td>' +
        '<td>' + dir + '</td><td>' + entry + '</td><td>' + exit + '</td>' +
        '<td>' + pnStr + '</td><td>' + roeStr + '</td><td>' + hold + '</td>' +
        '<td class="v3-scan-status"><small class="text-muted">' + _journalReason(t) + '</small></td>' +
        '<td>' + dtIc + '</td></tr>';
    }).join('');
    return h + '<table class="v3-ltable v3-jtable"><thead><tr>' + _jCols(showStrat) + '</tr></thead><tbody>' + rows + '</tbody></table>' +
      '<div class="v3-jpager">' + _jPager(tr.page || 1, tr.total_pages || 1) + '</div>';
  }
  function drawDailyChart() {
    const j = V3.state.journal, canvas = $('v3-daily-canvas'), ph = $('v3-daily-ph');
    if (!canvas) return;
    if (!j.snaps || !j.snaps.length) { if (ph) { ph.textContent = (j.snaps == null ? 'Loading daily data…' : 'No daily data yet'); ph.style.display = ''; } canvas.style.display = 'none'; return; }
    const snaps = j.snaps.map((x) => ({ date: x.date, pnl: Number(x.total_pnl || 0) }));
    const sums = (j.summary && j.summary.summary) || {};
    const todayVal = !j.strategy ? (((sums.FOCUS && sums.FOCUS.today_pnl) || 0) + ((sums.HARPOON && sums.HARPOON.today_pnl) || 0)) : ((sums[j.strategy] && sums[j.strategy].today_pnl) || 0);
    const todayDate = new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Seoul' });
    if (!snaps.find((x) => x.date === todayDate)) snaps.push({ date: todayDate, pnl: todayVal });
    if (ph) ph.style.display = 'none';
    canvas.style.display = 'block';
    const ctx = canvas.getContext('2d'), dpr = window.devicePixelRatio || 1;
    const rect = canvas.parentElement.getBoundingClientRect();
    if (rect.width < 10) return;
    canvas.width = rect.width * dpr; canvas.height = rect.height * dpr; ctx.scale(dpr, dpr);
    const W = rect.width, H = rect.height, mid = H / 2, padL = 50, padR = 20;
    const maxAbs = Math.max(5, ...snaps.map((x) => Math.abs(x.pnl))) * 1.2;
    const gap = (W - padL - padR) / snaps.length, barW = Math.min(40, gap - 4);
    ctx.clearRect(0, 0, W, H);
    ctx.strokeStyle = '#555'; ctx.lineWidth = 1; ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.moveTo(padL, mid); ctx.lineTo(W - padR, mid); ctx.stroke(); ctx.setLineDash([]);
    let cum = 0; const cumPts = [];
    snaps.forEach((x, i) => {
      const cx = padL + i * gap + gap / 2, barH = (x.pnl / maxAbs) * (mid - 10), y = x.pnl >= 0 ? mid - barH : mid, hh = Math.abs(barH);
      ctx.fillStyle = x.pnl >= 0 ? 'rgba(22,199,132,0.7)' : 'rgba(234,57,67,0.7)'; ctx.fillRect(cx - barW / 2, y, barW, hh);
      ctx.fillStyle = x.pnl >= 0 ? '#16c784' : '#ea3943'; ctx.font = '10px sans-serif'; ctx.textAlign = 'center'; ctx.fillText('$' + x.pnl.toFixed(0), cx, x.pnl >= 0 ? y - 3 : y + hh + 11);
      ctx.fillStyle = '#888'; ctx.font = '9px sans-serif'; ctx.fillText(x.date.slice(5), cx, H - 3);
      cum += x.pnl; cumPts.push({ x: cx, y: mid - (cum / maxAbs) * (mid - 10) });
    });
    if (cumPts.length > 1) {
      ctx.strokeStyle = '#ffb74d'; ctx.lineWidth = 2; ctx.beginPath(); cumPts.forEach((p, i) => i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y)); ctx.stroke();
      ctx.fillStyle = '#ffb74d'; ctx.font = 'bold 10px sans-serif'; ctx.textAlign = 'left'; const last = cumPts[cumPts.length - 1]; ctx.fillText('$' + cum.toFixed(0), last.x + 5, last.y - 3);
    }
    ctx.fillStyle = '#888'; ctx.font = '9px sans-serif'; ctx.textAlign = 'right';
    ctx.fillText('+$' + maxAbs.toFixed(0), padL - 5, 14); ctx.fillText('-$' + maxAbs.toFixed(0), padL - 5, H - 14); ctx.fillText('$0', padL - 5, mid + 4);
  }
  async function loadJournal(force) {
    const j = V3.state.journal, now = Date.now();
    if (j.loading && !force) return;
    if (!force && j.data && now - j.t < 30000) return;
    j.loading = true;
    try {
    if (j.markets == null) { const mr = await V3.getJSON('/api/strategy/focus/journal/markets', { timeoutMs: 5000 }); j.markets = (mr && mr.ok && mr.markets) || []; }
    let params = 'limit=' + j.limit + '&page=' + j.page;
    if (j.strategy) params += '&strategy=' + j.strategy;
    if (j.market) params += '&market=' + j.market;
    const [trd, smy, snp] = await Promise.all([
      V3.getJSON('/api/strategy/focus/journal?' + params, { timeoutMs: 7000 }),
      V3.getJSON('/api/strategy/focus/journal/summary', { timeoutMs: 7000 }),
      V3.getJSON('/api/strategy/focus/daily-snapshots', { timeoutMs: 7000 }),
    ]);
    j.data = trd; j.summary = smy; j.snaps = (snp && snp.snapshots) || []; j.t = now;
    const e = $('v3-wg-journal'); if (e) { e.innerHTML = renderJournal(); drawDailyChart(); }
    } finally {
      j.loading = false;
    }
  }
  V3.loadJournal = loadJournal;

  // ── 📊 BTC analysis side table (right of Positions, auto-rotates through held coins) — v2 focusScan(20221) port ──
  function renderGp() {
    const g = V3.state.gp, d = g.data;
    const title = (g.market || '').replace('USDT', '') || 'Analysis';
    let body;
    if (!d || !d.ok) body = '<div class="v3-gp-loading">Loading analysis…</div>';
    else {
      const st = d.structure || {}, trend = st.trend || '-';
      const tCls = trend === 'UPTREND' ? 'long' : trend === 'DOWNTREND' ? 'short' : 'mute';
      const conf = st.confidence ? '(' + (st.confidence * 100).toFixed(0) + '%)' : '';
      const swings = (st.swings || []).map((x) => x.type + ':$' + Math.round(x.price)).join(' ') || 'None';
      const bos = st.bos ? (st.bos.direction + ' @ $' + Math.round(st.bos.price)) : 'None';
      const pa = (d.pa_signals || []).map((p) => '<span class="v3-badge ' + (p.direction === 'LONG' ? 'long' : 'short') + '">' + p.pattern + '</span>').join(' ') || '<span class="text-muted">-</span>';
      const zones = (d.zones || []).map((z) => '<div class="v3-gp-zone" style="border-left:3px solid ' + (z.type === 'SUPPORT' ? 'var(--v3-long)' : 'var(--v3-short)') + '"><b>' + z.type.charAt(0) + '</b> $' + Number(z.low || 0).toFixed(0) + '~$' + Number(z.high || 0).toFixed(0) + '</div>').join('') || '<span class="text-muted">-</span>';
      body = '<div class="v3-gp-row"><b>Trend:</b> <span class="v3-badge ' + tCls + '">' + trend + '</span> <small class="text-muted">' + conf + '</small></div>' +
        '<div class="v3-gp-row"><b>ATR:</b> $' + Number(d.atr || 0).toFixed(2) + '</div>' +
        '<div class="v3-gp-row" style="min-height:38px">' + '<b>Swings:</b> <small class="text-muted">' + swings + '</small></div>' +
        '<div class="v3-gp-row"><b>BOS:</b> <span class="text-muted">' + bos + '</span></div>' +
        '<div class="v3-gp-row"><b>PA:</b> ' + pa + '</div>' +
        '<div class="v3-gp-row"><b>Zones:</b><div class="v3-gp-zones">' + zones + '</div></div>';
    }
    return '<div class="v3-gp-head"><span class="v3-gp-title">📊 ' + title + '</span>' +
      '<span class="v3-gp-actions"><input id="v3-gp-market" class="v3-mini" type="text" value="' + (g.market || '') + '" title="Enter coin then 🔄" style="width:74px;text-align:left"><button id="v3-gp-refresh" class="v3-btn sm ghost" title="Analyze this coin">🔄</button></span></div>' + body;
  }
  async function loadGp(force) {
    const g = V3.state.gp, now = Date.now();
    if (!force && g.data && now - g.t < 30000) { const e0 = $('v3-wg-gp'); if (e0) e0.innerHTML = renderGp(); return; }
    let market = (force && g.market) ? g.market : null;
    if (!market) {
      const fs = V3.lastStatus || {};
      const held = (fs.positions || (fs.position ? [fs.position] : [])).map((p) => p.market).filter(Boolean);
      if (held.length) { market = held[g.rotateIdx % held.length]; g.rotateIdx++; }   // auto-rotate through held coins (owner "it cycled randomly")
      else market = g.market || 'BTCUSDT';
    }
    g.market = market;
    const dd = await V3.getJSON('/api/strategy/focus/analysis/' + market + '?tf=240');
    g.data = dd; g.t = now;
    const e = $('v3-wg-gp'); if (e) e.innerHTML = renderGp();
  }

  // ── 🔭 Regime Transition Watch (Phase K) — v2 renderPhaseKWatch(21696) port, top full-width ──
  function renderPhaseK() {
    const pk = V3.state.phaseK, data = pk.data;
    const upd = pk.t ? new Date(pk.t).toLocaleTimeString('en-US', { hour12: false }) : '—';
    let badge = '<span class="v3-badge mute">Waiting</span>', body;
    if (!data || !data.ok) body = '<div class="v3-pk-loading">Loading Phase K data…</div>';
    else {
      const ks = data.k_status || {}, dets = data.recent_detections || [], now = Math.floor(Date.now() / 1000);
      badge = !ks.enabled ? '<span class="v3-badge mute">Detection OFF</span>' : ks.paper_mode ? '<span class="v3-badge" style="color:var(--v3-accent);border-color:var(--v3-accent)">Paper detecting</span>' : '<span class="v3-badge warn">Live detecting</span>';
      if (!dets.length) {
        const gap = ks.btc_ema_gap_pct, gapThr = ks.ema_gap_threshold_pct || 0.3;
        const gapStr = (gap != null) ? gap.toFixed(2) + '%' : '—';
        const age = ks.btc_regime_age_hours || 0, minAgeMin = ks.min_regime_age_min || 180;
        const ageStr = age >= 1 ? age.toFixed(1) + 'h' : (age * 60).toFixed(0) + 'min';
        const rc = ks.btc_regime === 'BULL' ? 'var(--v3-long)' : ks.btc_regime === 'BEAR' ? 'var(--v3-short)' : 'var(--v3-fg-mute)';
        const gapClose = gap != null && gap < gapThr;
        body = '<div class="v3-pk-empty"><div class="v3-pk-big">⚪ No transition detected right now</div>' +
          '<div class="v3-pk-sub">BTC <b style="color:' + rc + '">' + ks.btc_regime + '</b> stable for ' + ageStr + ' · EMA gap <b>' + gapStr + '</b> ' + (gapClose ? '✓ close' : '(&lt; ' + gapThr + '% waiting)') + ' · min regime age ' + minAgeMin + 'min ' + (age * 60 > minAgeMin ? '✓' : 'waiting') + '</div>' +
          '<div class="v3-pk-note">No Phase K detection in the last 6 hours</div></div>';
      } else {
        body = dets.map((dd) => {
          const ageSec = now - (dd.ts || now);
          const ageStr = ageSec < 60 ? ageSec + 's ago' : ageSec < 3600 ? Math.floor(ageSec / 60) + 'm ago' : (ageSec / 3600).toFixed(1) + 'h ago';
          const icon = dd.scanner_dir === 'LONG' ? '🔴' : '🟢';
          const label = dd.scanner_dir === 'LONG' ? 'Uptrend ending' : 'Downtrend ending';
          const color = dd.scanner_dir === 'LONG' ? 'var(--v3-short)' : 'var(--v3-long)';
          const adxStr = (dd.adx_past && dd.adx_now) ? 'ADX ' + dd.adx_past + '→' + dd.adx_now + ' (' + ((dd.adx_past - dd.adx_now) / dd.adx_past * 100).toFixed(1) + '% drop)' : '';
          const gapStr = (dd.btc_ema_gap_pct != null) ? 'EMA gap ' + dd.btc_ema_gap_pct.toFixed(2) + '%' : '';
          const count = dd.count_today > 1 ? ' <span class="v3-badge mute">' + dd.count_today + 'x today</span>' : '';
          return '<div class="v3-pk-det"><div class="v3-pk-ic">' + icon + '</div><div class="v3-pk-detbody">' +
            '<div><b style="color:' + color + '">' + dd.market + '</b> <small class="text-muted">' + label + '</small>' + count + '</div>' +
            '<small class="text-muted">' + adxStr + ' · ' + gapStr + ' · conv=' + (dd.conviction || '?') + '</small>' +
            '<div class="v3-pk-note">scanner wants <b>' + dd.scanner_dir + '</b> · ' + ageStr + '</div></div></div>';
        }).join('');
      }
    }
    return '<div class="v3-pk-head"><span class="v3-pk-title">🔭 Regime Transition Watch <small class="text-muted">Phase K · imminent-transition detector</small></span>' +
      '<span class="v3-pk-meta">' + badge + ' <small class="text-muted">' + upd + '</small></span></div>' +
      '<div class="v3-pk-body">' + body + '</div>' +
      '<div class="v3-pk-foot"><small class="text-muted">─── experimental signal · not an entry recommendation · accuracy published after 1 week of paper ───</small></div>';
  }
  async function loadPhaseK(force) {
    const pk = V3.state.phaseK, now = Date.now();
    if (!force && pk.data && now - pk.t < 60000) return;
    const dd = await V3.getJSON('/api/strategy/focus/phase-k/recent?hours=6');
    pk.data = dd; pk.t = now;
    const e = $('v3-phasek'); if (e) e.innerHTML = renderPhaseK();
  }

  // 📊 TF Progress table (7-TF candle flow) — shared by side widget (renderManual) & center modal (showTfModal). v2 _tfp_render logic
  function _tfpTable(tfp) {
    return '<table class="v3-tfp-table"><thead><tr><th>TF</th><th>Progress (time)</th><th style="text-align:right">Open→Now</th><th style="text-align:right" title="Upper wick %">↑W</th><th style="text-align:right" title="Lower wick %">↓W</th></tr></thead><tbody>' +
      (tfp.rows || []).map((r) => {
        if (!r.ok) return '<tr><td><b style="color:#9cf">' + r.tf + '</b></td><td colspan="4" class="text-muted">' + (r.reason || '?') + '</td></tr>';
        const col = r.direction === 'bull' ? 'var(--v3-long)' : r.direction === 'bear' ? 'var(--v3-short)' : 'var(--v3-fg-dim)';
        const arrow = r.direction === 'bull' ? '▲' : r.direction === 'bear' ? '▼' : '·';
        const ep = r.elapsed_pct || 0, dp = r.delta_pct || 0, sign = dp >= 0 ? '+' : '';
        const filled = Math.max(0, Math.min(10, Math.round(ep / 10)));
        const bar = '█'.repeat(filled) + '░'.repeat(10 - filled);
        return '<tr><td><b style="color:#9cf">' + r.tf + '</b></td>' +
          '<td class="v3-tfp-bar">' + bar + ' ' + ep.toFixed(0) + '%</td>' +
          '<td style="text-align:right;color:' + col + '"><b>' + arrow + ' ' + sign + dp.toFixed(2) + '%</b></td>' +
          '<td style="text-align:right;color:#f88">' + (r.upper_wick_pct || 0).toFixed(2) + '</td>' +
          '<td style="text-align:right;color:#8f8">' + (r.lower_wick_pct || 0).toFixed(2) + '</td></tr>';
      }).join('') + '</tbody></table>';
  }
  // 📊 Scanner row 📊 click → center modal (preview *before* L/S decision, v2 _focusShowTfModal port). Data = same /tf-progress
  async function showTfModal(market, sig) {
    const back = $('v3-tfm-back'); if (!back || !market) return;
    const m = String(market).toUpperCase().replace(/[^A-Z0-9]/g, '');
    const mEl = $('v3-tfm-market'); if (mEl) mEl.textContent = m.replace('USDT', '');
    const lb = $('v3-tfm-long'), sb = $('v3-tfm-short');   // inject this coin into the L/S entry buttons
    if (lb) lb.dataset.mkt = m;
    if (sb) sb.dataset.mkt = m;
    const cons = $('v3-tfm-consensus'); if (cons) { cons.textContent = 'Consensus: loading…'; cons.style.color = 'var(--v3-fg-dim)'; }
    const sum = $('v3-tfm-summary'); if (sum) sum.innerHTML = '';
    const rows = $('v3-tfm-rows'); if (rows) rows.innerHTML = '<div class="text-muted" style="padding:14px;text-align:center">Loading…</div>';
    const ts = $('v3-tfm-ts'); if (ts) ts.textContent = '';
    back.classList.add('show');
    try {
      const d = await V3.getJSON('/api/strategy/focus/tf-progress?market=' + encodeURIComponent(m));
      renderTfModal(d, sig);
    } catch (e) {
      if (rows) rows.innerHTML = '<div class="v3-neg" style="padding:12px">Load failed: ' + (e.message || e) + '</div>';
    }
  }
  function renderTfModal(d, sig) {
    const rows = $('v3-tfm-rows'), cons = $('v3-tfm-consensus'), sum = $('v3-tfm-summary'), ts = $('v3-tfm-ts');
    if (!rows) return;
    if (!d || !d.ok) {
      rows.innerHTML = '<div class="v3-neg" style="padding:12px">Load failed: ' + ((d && d.error) || '?') + '</div>';
      if (cons) { cons.textContent = 'Consensus: failed'; cons.style.color = 'var(--v3-short)'; }
      return;
    }
    if (cons) {
      const c = d.consensus || '-';
      cons.textContent = 'Consensus: ' + c;
      cons.style.color = c.indexOf('BULL') >= 0 ? 'var(--v3-long)' : c.indexOf('BEAR') >= 0 ? 'var(--v3-short)' : 'var(--v3-fg-dim)';
    }
    if (sum) {
      const cp = d.current_price || 0;
      const cpStr = cp >= 100 ? cp.toFixed(2) : cp >= 1 ? cp.toFixed(4) : cp.toFixed(6);
      sum.innerHTML = '<span class="v3-pos">▲' + (d.n_bull || 0) + '</span> <span class="v3-neg">▼' + (d.n_bear || 0) + '</span> <span class="text-muted">·' + (d.n_flat || 0) + '</span>' +
        '<span class="text-muted" style="margin-left:12px">Price $' + cpStr + '</span>' +
        (sig ? '<span class="text-muted" style="margin-left:12px">Bot: ' + sig + '</span>' : '');
    }
    if (ts) { try { ts.textContent = '@ ' + new Date(d.ts || Date.now()).toTimeString().slice(0, 8); } catch (e) { /* noop */ } }
    rows.innerHTML = _tfpTable(d);
  }
  // Manual entry (Scanner row L/L⏳/S/S⏳ · shared with TF modal L/S) — confirm → manual-entry POST. Direction as-is (no auto FLIP)
  async function scanManualEntry(mkt, dir, smart) {
    if (!mkt || !dir) return;
    const badge = '<span class="v3-badge ' + (dir === 'LONG' ? 'long' : 'short') + '">' + dir + '</span>';
    const ok = await V3.confirm('Manual Entry', '<div style="line-height:1.8"><b>' + mkt + '</b> ' + badge +
      '<br><small>' + (smart ? 'Enter after signal confirmed (wait 1h)' : 'Enter immediately') + '</small>' +
      '<br><small style="color:var(--v3-warn)">⚠️ Live trade — gate bypass (safety guards kept) · direction as-is (no auto FLIP)</small></div>');
    if (!ok) return;
    let url = '/api/strategy/focus/manual-entry?market=' + encodeURIComponent(mkt) + '&direction=' + dir;
    if (smart) url += '&wait_for_signal=true&timeout_sec=3600';
    V3.toast(mkt + ' ' + dir + ' requesting…', 'info');
    const r = await V3.getJSON(url, { method: 'POST' });
    V3.toast((r && r.ok) ? ('✓ ' + mkt + ' ' + dir + (smart ? ' signal-wait registered' : ' entered')) : ('✗ Failed: ' + ((r && r.error) || 'unknown')), (r && r.ok) ? 'ok' : 'err', 5000);
    pollStatus();
  }
  // ── 🖐 Manual Control — v2 port (Force Select / TF Progress candle flow / Pin·Selected·H1 SIG / Recent Skips). For manual checks (gold trading etc.) ──
  function renderManual() {
    const m = V3.state.manual, st = V3.lastStatus || {};
    const lockMarket = (st.config && st.config.lock_market) || st.lock_market || '';
    const pin = lockMarket ? '<span class="v3-badge warn">📌 ' + lockMarket + '</span>' : '<span class="text-muted">Auto Scan</span>';
    const sel = st.selected_market || '-';
    const sig = st.primary_sig ? (st.primary_sig.pattern + ' ' + st.primary_sig.direction) : '-';
    // 📊 TF Progress (in-progress candle flow) — v2 _tfp_render port
    const tfp = m.tfp;
    let tfpHead = '<span class="text-muted">Waiting…</span>', tfpRows = '';
    if (tfp && tfp.ok) {
      const c = tfp.consensus || '-';
      const ccol = c.indexOf('BULL') >= 0 ? 'var(--v3-long)' : c.indexOf('BEAR') >= 0 ? 'var(--v3-short)' : 'var(--v3-fg-dim)';
      let ts = '';
      try { ts = '@ ' + new Date(tfp.ts || Date.now()).toTimeString().slice(0, 8); } catch (e) { /* noop */ }
      tfpHead = 'Consensus: <b style="color:' + ccol + '">' + c + ' (' + (tfp.market || '?') + ')</b> ' +
        '<span class="v3-pos">▲' + (tfp.n_bull || 0) + '</span> <span class="v3-neg">▼' + (tfp.n_bear || 0) + '</span> <span class="text-muted">·' + (tfp.n_flat || 0) + '</span> <small class="text-muted">' + ts + '</small>';
      tfpRows = _tfpTable(tfp);
    } else if (tfp && !tfp.ok) {
      tfpHead = '<span class="text-muted">Load failed: ' + (tfp.error || '?') + '</span>';
    }
    // 🚫 Recent Skips (why a signal fired but no entry happened) — v2 port
    const skips = st.recent_skips || [];
    let skipsHtml;
    if (!skips.length) skipsHtml = '<span class="text-muted">No recent skips</span>';
    else {
      const _t = (ts) => { try { return new Date(ts * 1000).toTimeString().slice(0, 8); } catch (e) { return '-'; } };
      const _dc = (x) => x === 'LONG' ? 'var(--v3-long)' : x === 'SHORT' ? 'var(--v3-short)' : 'var(--v3-fg-mute)';
      const _cc = (x) => x >= 75 ? 'var(--v3-long)' : x >= 50 ? 'var(--v3-warn)' : 'var(--v3-short)';
      skipsHtml = '<table class="v3-skips-table"><thead><tr><th>Time</th><th>Coin</th><th style="text-align:center">Dir</th><th style="text-align:center">conv</th><th>PA</th><th>Reason</th></tr></thead><tbody>' +
        skips.slice(0, 20).map((s) => '<tr>' +
          '<td class="text-muted">' + _t(s.ts || 0) + '</td>' +
          '<td><b class="v3-mkt" data-bybit="' + (s.market || '') + '">' + (s.market || '').replace('USDT', '') + '</b></td>' +
          '<td style="text-align:center;color:' + _dc(s.direction) + '">' + (s.direction || '-').slice(0, 1) + '</td>' +
          '<td style="text-align:center;color:' + _cc(s.conviction || 0) + '">' + (s.conviction || 0) + '</td>' +
          '<td style="color:#9cf">' + (s.pa || '-') + '</td>' +
          '<td style="color:#fa8">' + (s.reason || '-') + '</td></tr>').join('') + '</tbody></table>';
    }
    // 📊 GateLedger — "why was it quiet today" (pass/reject per gate). st.gate_stats is filled only when gate_ledger_enabled is ON.
    const gstats = st.gate_stats;
    let gateHtml;
    if (!gstats || !gstats.gates || !Object.keys(gstats.gates).length) {
      gateHtml = '<span class="text-muted">' + (gstats ? 'No tally yet' : 'Gate tally OFF (enable in settings)') + '</span>';
    } else {
      const rows = Object.keys(gstats.gates).map((g) => { const v = gstats.gates[g]; return { g: g, pass: v.pass || 0, reject: v.reject || 0, mk: (v.top_markets || []).join(',') }; });
      rows.sort((a, b) => b.reject - a.reject);   // bottleneck (most rejects first)
      gateHtml = '<table class="v3-skips-table"><thead><tr><th>Gate</th><th style="text-align:center">pass</th><th style="text-align:center">reject</th><th>top</th></tr></thead><tbody>' +
        rows.slice(0, 15).map((r) => '<tr>' +
          '<td style="color:#fa8">' + r.g + '</td>' +
          '<td style="text-align:center;color:var(--v3-long)">' + r.pass + '</td>' +
          '<td style="text-align:center;color:var(--v3-short)">' + r.reject + '</td>' +
          '<td class="text-muted" style="font-size:11px">' + (r.mk || '-').replace(/USDT/g, '') + '</td></tr>').join('') +
        '</tbody></table><div class="v3-wnote">' + (gstats.date || '') + ' · ' + (gstats.total_scanned || 0) + ' evaluated</div>';
    }
    return '<div class="v3-widget-h">🖐 Manual Control <small>(manual checks, e.g. gold trading)</small></div>' +
      '<div class="v3-manual">' +
      '<div class="v3-manual-form">' +
      '<div><label class="v3-label">Market</label><input id="v3-manual-market" class="v3-input" type="text" value="' + (m.market || 'XAUTUSDT') + '" placeholder="XAUTUSDT"></div>' +
      '<div><label class="v3-label">Direction</label><select id="v3-manual-dir" class="v3-input"><option value="LONG"' + (m.dir === 'SHORT' ? '' : ' selected') + '>LONG</option><option value="SHORT"' + (m.dir === 'SHORT' ? ' selected' : '') + '>SHORT</option></select></div>' +
      '</div>' +
      '<button id="v3-manual-force" class="v3-btn v3-btn-long" style="width:100%;margin-top:8px">🎯 Force Select</button>' +
      '<div class="v3-wnote">Click 📌 in the Scan List to pin/unpin a coin</div>' +
      '<button id="v3-manual-disable" class="v3-btn v3-btn-outline-danger" style="width:100%;margin-top:4px">■ Disable + Close</button>' +
      '<hr class="v3-manual-hr">' +
      '<div class="v3-manual-sec"><b>📊 TF Progress</b> <small class="text-muted">— in-progress candle flow (manual reference)</small></div>' +
      '<div class="v3-tfp-head">' + tfpHead + '</div>' +
      '<div class="v3-tfp-rows">' + tfpRows + '</div>' +
      '<div class="v3-wnote">※ Candle progress (time) · open→current ±% · upper/lower wick. Refreshes every 5s.</div>' +
      '<hr class="v3-manual-hr">' +
      '<div class="v3-manual-kv"><b>📌 Pin:</b> ' + pin + '</div>' +
      '<div class="v3-manual-kv"><b>Selected:</b> <span class="text-muted">' + sel + '</span></div>' +
      '<div class="v3-manual-kv"><b>H1 SIG:</b> <span class="text-muted">' + sig + '</span></div>' +
      '<div class="v3-manual-kv" style="margin-top:6px"><b>🚫 Recent Skips:</b></div>' +
      '<div class="v3-skips">' + skipsHtml + '</div>' +
      '<div class="v3-manual-kv" style="margin-top:6px"><b>📊 Why was it quiet (GateLedger):</b></div>' +
      '<div class="v3-skips">' + gateHtml + '</div>' +
      '</div>';
  }
  async function loadTfp(force) {
    const m = V3.state.manual, now = Date.now();
    if (!force && m.tfp && now - m.tfpT < 5000) return;   // 5s polling (v2 cadence)
    const market = m.market || 'XAUTUSDT';
    const dd = await V3.getJSON('/api/strategy/focus/tf-progress?market=' + encodeURIComponent(market));
    m.tfp = dd; m.tfpT = now;
    const e = $('v3-wg-manual'); if (e) e.innerHTML = renderManual();
  }
  // v2 _refreshFocusScanList(dashboard_v2.js:22244) verbatim port — price/warning/bar/badge/penalty/Manual
  function renderScan(d) {
    const list = (d && (d.items || d.results || d.scan_list || d.list)) || [];
    if (!list.length) return '<div class="v3-widget-h">🟢 GreenPen Scanner</div><div class="v3-placeholder">No scan data (auto on Refresh or when enabled · a few seconds)</div>';
    const rows = list.map((item) => {
      const sigClass = item.signal === 'BUY' ? 'bg-success' : item.signal === 'SELL' ? 'bg-danger' : 'bg-secondary';
      const trendClass = item.trend === 'UPTREND' ? 'text-success' : item.trend === 'DOWNTREND' ? 'text-danger' : 'text-muted';
      let paHtml;
      if (item.pa_pattern && item.pa_pattern !== '-') {
        if (item.pa_type === 'pa') paHtml = '<span class="badge ' + sigClass + '">' + item.pa_pattern + '</span>';
        else if (item.pa_type === 'bos') paHtml = '<span style="color:#9575cd;font-weight:600;">' + item.pa_pattern + '</span>';
        else { const c = item.trend === 'UPTREND' ? '#66bb6a' : item.trend === 'DOWNTREND' ? '#ef5350' : '#90a4ae'; paHtml = '<span style="color:' + c + ';">' + item.pa_pattern + '</span>'; }
      } else paHtml = '<span class="text-muted">-</span>';
      const confBar = item.confidence > 0 ? '<div class="d-flex align-items-center gap-1"><span class="progress" style="width:50px;height:6px;"><span class="progress-bar bg-secondary" style="width:' + item.confidence + '%"></span></span><small>' + item.confidence + '%</small></div>' : '-';
      const adxVal = item.adx || 0, adxClr = adxVal >= 20 ? '#4caf50' : adxVal >= 15 ? '#f9a825' : '#ef5350';
      const adxHtml = adxVal > 0 ? '<strong style="color:' + adxClr + '">' + adxVal.toFixed(1) + '</strong>' : '-';
      const baseRaw = item.guard_base != null ? item.guard_base : item.conviction;
      const baseDisp = baseRaw != null ? Number(baseRaw).toFixed(0) : '—';
      const dedRaw = item.guard_deduction;
      const dedDisp = dedRaw != null ? ((dedRaw >= 0 ? '+' : '') + Number(dedRaw).toFixed(0)) : '—';
      const dedColor = (dedRaw != null && dedRaw < 0) ? 'text-danger' : (dedRaw != null && dedRaw > 0 ? 'text-success' : 'text-muted');
      const totalRaw = item.guard_total != null ? item.guard_total : baseRaw;
      const totalDisp = totalRaw != null ? Number(totalRaw).toFixed(0) : '—';
      const tNum = Number(totalRaw); let tBg = '#424242', tClr = '#aaa';
      if (!isNaN(tNum)) { if (tNum >= 65) { tClr = '#fff'; tBg = '#2e7d32'; } else if (tNum >= 40) { tClr = '#fff'; tBg = '#f9a825'; } else if (tNum >= 0) { tClr = '#fff'; tBg = '#e65100'; } else { tClr = '#fff'; tBg = '#c62828'; } }
      const totalBadge = '<span class="badge" style="background:' + tBg + ';color:' + tClr + ';font-size:0.95rem;padding:5px 9px;font-weight:700;" title="Total = Base + Deduction. Enters when ≥ threshold(' + (item.guard_threshold || 65) + ')">' + totalDisp + '</span>';
      const penalty = item.guard_breakdown || item.status || '-';
      let botOp = '';
      if (item.bot_opinion && item.bot_opinion.text) {
        const lvl = item.bot_opinion.level || 'info';
        const bg = lvl === 'warn' ? 'rgba(239,83,80,0.18)' : 'rgba(255,193,7,0.15)', clr = lvl === 'warn' ? '#ef5350' : '#f9a825';
        botOp = '<br><span style="display:inline-block;margin-top:2px;padding:1px 6px;font-size:0.7rem;background:' + bg + ';color:' + clr + ';border:1px solid ' + clr + ';border-radius:3px;font-weight:600;">' + item.bot_opinion.text + '</span>';
      }
      const mk = item.market || '', chg = Number(item.change_pct || 0);
      const meBtns =
        '<button class="v3-scan-me v3-scan-tfm" data-mkt="' + mk + '" data-sig="' + (item.signal || '') + '" title="📊 TF Progress preview — 7 TF (D/H4/H1/30M/15M/5M/3M) candle flow. See it at a glance before L/S decision">📊</button>' +
        '<button class="v3-scan-me" data-mkt="' + mk + '" data-dir="LONG" title="Manual force LONG (gate bypass)">L</button>' +
        '<button class="v3-scan-me" data-mkt="' + mk + '" data-dir="LONG" data-smart="1" title="Smart LONG (enter after signal confirmed, wait 1h)">L⏳</button>' +
        '<button class="v3-scan-me s" data-mkt="' + mk + '" data-dir="SHORT" title="Manual force SHORT (gate bypass)">S</button>' +
        '<button class="v3-scan-me s" data-mkt="' + mk + '" data-dir="SHORT" data-smart="1" title="Smart SHORT (enter after signal confirmed, wait 1h)">S⏳</button>';
      return '<tr>' +
        '<td><b class="v3-mkt" data-bybit="' + mk + '">' + mk.replace('USDT', '') + '</b><br><small class="text-muted">$' + Number(item.price || 0).toFixed(2) + ' <span class="' + (chg >= 0 ? 'text-success' : 'text-danger') + '">' + (chg >= 0 ? '+' : '') + chg + '%</span></small>' + botOp + '</td>' +
        '<td><span class="badge ' + sigClass + '">' + item.signal + '</span></td>' +
        '<td>' + paHtml + '</td>' +
        '<td class="' + trendClass + '">' + item.trend + '</td>' +
        '<td>' + confBar + '</td>' +
        '<td>' + adxHtml + '</td>' +
        '<td><span class="text-muted">' + baseDisp + '</span></td>' +
        '<td><span class="' + dedColor + '">' + dedDisp + '</span></td>' +
        '<td>' + totalBadge + '</td>' +
        '<td class="v3-scan-status"><small class="text-muted">' + penalty + '</small></td>' +
        '<td class="v3-scan-manual">' + meBtns + '</td>' +
        '</tr>';
    }).join('');
    return '<div class="v3-widget-h">🟢 GreenPen Scanner <small>(Top ' + list.length + ')</small></div>' +
      '<table class="v3-ltable v3-scantable"><thead><tr><th>Market</th><th>Signal</th><th>PA Pattern</th><th>Trend</th><th>Confidence</th><th>ADX</th><th>Base</th><th>Ded</th><th>Total</th><th>Status (Penalty)</th><th>Manual</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  function renderPeerScan(d) {
    if (!d || !d.servers) return '<div class="v3-widget-h">🛰️ Peer Brief Scanner</div><div class="v3-placeholder">' + ((d && d.note) ? String(d.note) : 'No data') + '</div>';
    var servers = d.servers || [];
    var html = function (s) {
      return String(s == null ? '' : s).replace(/[&<>"']/g, function (m) {
        return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[m];
      });
    };
    var esc = function (s) { return html(String(s == null ? '' : s).replace('ByBit_', '')); };
    var num = function (v, def) { var n = Number(v); return isFinite(n) ? n : (def == null ? 0 : def); };
    var sg = function (v, p) { if (v == null) return ''; var n = Number(v); return (n >= 0 ? '+' : '') + n.toFixed(p == null ? 2 : p); };
    var L = 'color:#26a69a', S = 'color:#ef5350';
    var dcol = function (dir) { return dir === 'LONG' ? L : S; };
    var srvCell = function (srv) { return esc(srv); };   // server name = plain text (owner: check via top tabs, no nav needed 2026-06-07)
    var coinCell = function (sym) {   // coin → Bybit chart (reuses bybit_trade_window, existing v3-mkt handler)
      var u = String(sym || '').toUpperCase().replace(/[^A-Z0-9:_-]/g, '');
      return '<b class="v3-mkt" data-bybit="' + u + '" style="cursor:pointer;text-decoration:underline">' + html(u) + '</b>';
    };
    var gateOf = function (r) {
      var s = String(r || '').trim();
      return (s.split(':')[0].split('(')[0].trim() || '?');
    };
    var fmtPx = function (v) {
      var n = Number(v);
      if (!isFinite(n) || n <= 0) return '<span class="text-muted">-</span>';
      return n >= 100 ? n.toFixed(2) : n >= 1 ? n.toFixed(4) : n.toFixed(6);
    };
    var retCell = function (v) {
      if (v == null || v === '') return '<span class="text-muted">-</span>';
      var n = Number(v);
      if (!isFinite(n)) return '<span class="text-muted">-</span>';
      var c = n > 0.10 ? S : (n <= 0.05 ? L : 'color:#ffc107');
      return '<span style="' + c + '">' + (n >= 0 ? '+' : '') + n.toFixed(2) + '%</span>';
    };
    var horizonCell = function (n, key, min) {
      if (num(n.age, 0) < min) return '<span class="text-muted">watching</span>';
      return retCell(n[key]);
    };
    var verdictOf = function (n) {
      if (n.verdict) return n.verdict;
      var r = Number(n.ret_now);
      if (!isFinite(r)) return 'unknown';
      if (num(n.age, 0) < 5) return 'watching';
      if (r > 0.10) return 'missed_entry';
      if (r <= 0.05) return 'good_block';
      return 'neutral';
    };
    var verdictBadge = function (n) {
      var v = verdictOf(n), label = n.verdict_label || '';
      if (!label) label = v === 'good_block' ? 'Good Block' : v === 'missed_entry' ? 'Missed Entry' : v === 'neutral' ? 'Neutral' : v === 'watching' ? 'Watching' : 'Pending';
      var cls = v === 'good_block' ? 'long' : v === 'missed_entry' ? 'short' : v === 'neutral' ? 'warn' : 'mute';
      return '<span class="v3-badge ' + cls + '">' + esc(label) + '</span>';
    };
    // ① Server status strip
    var strip = servers.map(function (s) {
      var ok = s.stale ? '🔴' : '🟢';
      var age = s.self ? 'me' : (s.ok_age_sec >= 0 ? s.ok_age_sec + 's' : '-');
      return '<span style="font-size:11px;padding:2px 7px;background:var(--v3-bg,#1a1a2e);border:1px solid var(--v3-bd,#334);border-radius:4px;white-space:nowrap">' +
        ok + ' ' + srvCell(s.server_id || '?', s.url) + ' · ' + age + ' · pos ' + ((s.positions || []).length) +
        ' · SL ' + ((s.losses || []).length) + ' · WIN ' + ((s.wins || []).length) + '</span>';
    }).join('');
    // ② Post-block evaluation (near-miss → check subsequent price)
    var allNm = [];
    servers.forEach(function (s) {
      (s.near_miss || []).forEach(function (n) {
        var row = {
          srv: s.server_id, url: s.url, symbol: n.symbol, direction: n.direction, score: n.score,
          reason: n.reason, gate: n.gate || gateOf(n.reason), age: n.age_min,
          price: n.block_price || n.price, current: n.current_price, ret_now: n.ret_now_pct,
          r5: n.ret_5m_pct, r15: n.ret_15m_pct, r30: n.ret_30m_pct, r60: n.ret_60m_pct,
          verdict: n.verdict, verdict_label: n.verdict_label
        };
        row.verdict = verdictOf(row);
        allNm.push(row);
      });
    });
    allNm.sort(function (a, b) { return (a.age || 0) - (b.age || 0); });
    var auditRows = allNm.slice(0, 14).map(function (n) {
      return '<tr><td>' + srvCell(n.srv, n.url) + '</td><td>' + coinCell(n.symbol) + '</td><td style="' + dcol(n.direction) + '">' + esc(n.direction) +
        '</td><td>' + num(n.score, 0).toFixed(0) + '</td><td>' + fmtPx(n.price) + '</td><td>' + retCell(n.ret_now) + '</td>' +
        '<td>' + horizonCell(n, 'r5', 5) + '</td><td>' + horizonCell(n, 'r15', 15) + '</td><td>' + horizonCell(n, 'r30', 30) + '</td><td>' + horizonCell(n, 'r60', 60) + '</td>' +
        '<td>' + verdictBadge(n) + '</td><td><small>' + esc(n.gate || n.reason) + '</small></td><td>' + num(n.age, 0).toFixed(0) + 'm ago</td></tr>';
    }).join('') || '<tr><td colspan="13" class="text-muted">Waiting on post-block verdict (no near-miss)</td></tr>';
    var blankStat = function () { return { total: 0, good: 0, missed: 0, neutral: 0, watching: 0, unknown: 0 }; };
    var addStat = function (box, n) {
      box.total += 1;
      var v = verdictOf(n);
      if (v === 'good_block') box.good += 1;
      else if (v === 'missed_entry') box.missed += 1;
      else if (v === 'neutral') box.neutral += 1;
      else if (v === 'watching') box.watching += 1;
      else box.unknown += 1;
    };
    var gateStats = {}, srvStats = {};
    allNm.forEach(function (n) {
      var g = n.gate || '?', s = n.srv || '?';
      gateStats[g] = gateStats[g] || blankStat(); addStat(gateStats[g], n);
      srvStats[s] = srvStats[s] || blankStat(); addStat(srvStats[s], n);
    });
    var statRows = function (stats, nameTitle) {
      var keys = Object.keys(stats).sort(function (a, b) {
        return (stats[b].total - stats[a].total) || (stats[b].missed - stats[a].missed);
      }).slice(0, 8);
      if (!keys.length) return '<tr><td colspan="6" class="text-muted">Awaiting tally</td></tr>';
      return keys.map(function (k) {
        var c = stats[k], judged = c.good + c.missed + c.neutral;
        var missRate = judged ? (c.missed / judged * 100) : 0;
        var note = judged < 3 ? 'watching' : missRate >= 50 ? 'too conservative?' : (c.good / judged * 100) >= 70 ? 'blocks good' : 'mixed';
        var cls = note === 'too conservative?' ? S : note === 'blocks good' ? L : 'color:#ffc107';
        return '<tr><td><small>' + esc(k) + '</small></td><td>' + c.total + '</td><td style="' + L + '">' + c.good + '</td><td style="' + S + '">' + c.missed +
          '</td><td>' + missRate.toFixed(0) + '%</td><td style="' + cls + '"><small>' + nameTitle + ' ' + note + '</small></td></tr>';
      }).join('');
    };
    var gateRows = statRows(gateStats, 'Gate');
    var srvRows = statRows(srvStats, 'Server');
    // ③ Fleet-held positions + flags
    var allPos = [];
    servers.forEach(function (s) {
      (s.positions || []).forEach(function (p) {
        allPos.push({ srv: s.server_id, url: s.url, symbol: p.symbol, direction: p.direction, age_min: p.age_min, peak: p.peak_pnl_pct, pnl: p.pnl_pct, usdt: p.pnl_usdt });
      });
    });
    var bySym = {};
    allPos.forEach(function (p) { (bySym[p.symbol] = bySym[p.symbol] || []).push(p.direction); });
    var flagsFor = function (p) {
      var dirs = bySym[p.symbol] || [], opp = p.direction === 'LONG' ? 'SHORT' : 'LONG', fl = [];
      if (dirs.indexOf(opp) >= 0) fl.push('<span title="self-conflict" style="color:#ff9800">⚔️</span>');
      if (dirs.filter(function (x) { return x === p.direction; }).length >= 2) fl.push('<span title="overlap">🔁</span>');
      if (Number(p.peak) <= 0.1) fl.push('<span title="struggling">🐢</span>');
      return fl.join(' ');
    };
    allPos.sort(function (a, b) { return (flagsFor(b) ? 1 : 0) - (flagsFor(a) ? 1 : 0); });
    var posRows = allPos.map(function (p) {
      return '<tr><td>' + srvCell(p.srv, p.url) + '</td><td>' + coinCell(p.symbol) + '</td><td style="' + dcol(p.direction) + '">' + esc(p.direction) +
        '</td><td>' + Number(p.age_min).toFixed(0) + 'm</td><td style="' + (Number(p.peak) <= 0.1 ? S : '') + '">' + sg(p.peak) + '%</td>' +
        '<td style="' + (Number(p.pnl) >= 0 ? L : S) + '">' + sg(p.pnl) + '% <small style="opacity:.7">(' + sg(p.usdt) + ')</small></td><td>' + flagsFor(p) + '</td></tr>';
    }).join('') || '<tr><td colspan="7" class="text-muted">No fleet positions</td></tr>';
    // ④ near-miss raw (score passed but blocked)
    var nmRows = allNm.slice(0, 12).map(function (n) {
      return '<tr><td>' + srvCell(n.srv, n.url) + '</td><td>' + coinCell(n.symbol) + '</td><td style="' + dcol(n.direction) + '">' + esc(n.direction) +
        '</td><td>' + num(n.score, 0).toFixed(0) + '✓</td><td><small>' + esc(n.reason) + '</small></td><td>' + num(n.age, 0).toFixed(0) + 'm ago</td></tr>';
    }).join('') || '<tr><td colspan="6" class="text-muted">None (no entries blocked after passing score)</td></tr>';
    // ⑤ Recent exits
    var allEx = [];
    servers.forEach(function (s) {
      (s.losses || []).forEach(function (x) { allEx.push({ srv: s.server_id, url: s.url, res: 'SL', symbol: x.symbol, direction: x.direction, pnl: x.pnl_net, age: x.age_min }); });
      (s.wins || []).forEach(function (x) { allEx.push({ srv: s.server_id, url: s.url, res: 'WIN', symbol: x.symbol, direction: x.direction, pnl: x.pnl_net, age: x.age_min }); });
    });
    allEx.sort(function (a, b) { return (a.age || 0) - (b.age || 0); });
    var exRows = allEx.slice(0, 12).map(function (x) {
      var rc = x.res === 'WIN' ? L : S;
      return '<tr><td>' + srvCell(x.srv, x.url) + '</td><td>' + coinCell(x.symbol) + '</td><td style="' + dcol(x.direction) + '">' + esc(x.direction) +
        '</td><td style="' + rc + '">' + x.res + '</td><td style="' + rc + '">' + sg(x.pnl) + '</td><td>' + Number(x.age).toFixed(0) + 'm ago</td></tr>';
    }).join('') || '<tr><td colspan="6" class="text-muted">No recent exits</td></tr>';
    var sub = function (t) { return '<div style="margin-top:8px;margin-bottom:2px;font-size:12px;color:var(--v3-accent,#6cf)">' + t + '</div>'; };
    return '<div class="v3-widget-h">🛰️ Peer Brief Scanner <small>(SL window ' + esc(d.sl_window_min) + 'm / WIN window ' + esc(d.peer_win_window_min) + 'm · self-conflict penalty ' + esc(d.peer_conflict_penalty) + ')</small></div>' +
      '<div style="display:flex;flex-wrap:wrap;gap:6px;margin:4px 0">' + strip + '</div>' +
      sub('🧭 Block-decision monitor — directional return + means a missed spot, 0/negative means a good block') +
      '<table class="v3-ltable"><thead><tr><th>Server</th><th>Coin</th><th>Dir</th><th>Score</th><th>Block px</th><th>Now</th><th>5m</th><th>15m</th><th>30m</th><th>60m</th><th>Verdict</th><th>Gate</th><th>Elapsed</th></tr></thead><tbody>' + auditRows + '</tbody></table>' +
      '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:10px">' +
      '<div>' + sub('Block quality by gate') + '<table class="v3-ltable"><thead><tr><th>Gate</th><th>Total</th><th>Good</th><th>Missed</th><th>Missed%</th><th>Verdict</th></tr></thead><tbody>' + gateRows + '</tbody></table></div>' +
      '<div>' + sub('Conservatism by server') + '<table class="v3-ltable"><thead><tr><th>Server</th><th>Total</th><th>Good</th><th>Missed</th><th>Missed%</th><th>Verdict</th></tr></thead><tbody>' + srvRows + '</tbody></table></div>' +
      '</div>' +
      sub('⚔️ self-conflict 🔁 overlap 🐢 struggling — fleet-held positions') +
      '<table class="v3-ltable"><thead><tr><th>Server</th><th>Coin</th><th>Dir</th><th>Held</th><th>peak</th><th>Current PnL %(USDT)</th><th>Flags</th></tr></thead><tbody>' + posRows + '</tbody></table>' +
      sub('🚧 Score passed but entry blocked (near-miss)') +
      '<table class="v3-ltable"><thead><tr><th>Server</th><th>Coin</th><th>Dir</th><th>Score</th><th>Blocking gate</th><th>Elapsed</th></tr></thead><tbody>' + nmRows + '</tbody></table>' +
      sub('📋 Recent peer exits (basis for conviction +/-)') +
      '<table class="v3-ltable"><thead><tr><th>Server</th><th>Coin</th><th>Dir</th><th>Result</th><th>PnL</th><th>Elapsed</th></tr></thead><tbody>' + exRows + '</tbody></table>';
  }
  const _wreg = {
    dow: { url: '/api/strategy/focus/analytics/by-dow', render: renderDow, el: 'v3-wg-dow', ttl: 30000, loading: '📅 Loading Day of Week…' },
    slot: { url: '/api/strategy/focus/analytics/by-slot', render: renderSlot, el: 'v3-wg-slot', ttl: 30000, loading: '⏱️ Loading 4H Slot…' },
    regime: { url: '/api/strategy/focus/day-direction', render: renderRegime, el: 'v3-wg-regime', ttl: 30000, loading: '🔭 Loading Day Direction…' },
    news: { url: '/api/news-sentiment/status', render: renderNews, el: 'v3-wg-news', ttl: 30000, loading: '📰 Loading News…' },
    report: { url: '/api/strategy/focus/coin-grades', render: renderReport, el: 'v3-wg-report', ttl: 60000, loading: '🏅 Loading Report Card…', wide: true },
    journal: { url: '/api/strategy/focus/journal?limit=25&include_blocked=false', render: renderJournal, el: 'v3-wg-journal', ttl: 30000, loading: '📓 Loading Journal…', wide: true },
    scan: { url: '/api/strategy/focus/scan-list?top_n=10', render: renderScan, el: 'v3-wg-scan', ttl: 120000, timeoutMs: 60000, loading: '🟢 GreenPen scanning… (may take tens of seconds on first run / after restart)', wide: true },
    peer: { url: '/api/strategy/focus/peer-cache', render: renderPeerScan, el: 'v3-wg-peer', ttl: 20000, loading: '🛰️ Loading peer-server status…', wide: true },
    manual: { render: renderManual, el: 'v3-wg-manual' },   // 🖐 Manual Control (own loader loadTfp + status data)
  };
  const _wcache = {};
  V3._wcache = _wcache; V3._wreg = _wreg;
  async function loadEnabledWidgets() {
    if (V3._widgetsLoading) return;
    V3._widgetsLoading = true;
    try {
    const w = V3.state.widgets || {};
    if (w.phasek) loadPhaseK();                // 🔭 Regime Transition Watch (right-side toggle)
    if (w.positions !== false) loadGp();       // 📊 BTC side table (rotates through held coins)
    if (w.journal) loadJournal();              // 📓 Journal (own loader — filters/paging/chart)
    if (w.manual) loadTfp();                   // 🖐 Manual Control — TF Progress 5s polling
    for (const key of Object.keys(_wreg)) {
      if (!w[key] || key === 'journal' || key === 'manual') continue;
      const cfg = _wreg[key]; if (!$(cfg.el)) continue;
      const c = _wcache[key], now = Date.now();
      if (c && now - c.t < (cfg.ttl || 30000)) { const e0 = $(cfg.el); if (e0) e0.innerHTML = cfg.render(c.data); continue; }
      const d = await V3.getJSON(cfg.url, { timeoutMs: cfg.timeoutMs || 8000 });
      if (d && d.ok !== false) _wcache[key] = { data: d, t: now };   // don't cache failures/timeouts (null/ok:false) → avoid a stuck blank screen for the TTL, retry on next render
      const e1 = $(cfg.el); if (e1) e1.innerHTML = cfg.render(d);
    }
    } finally {
      V3._widgetsLoading = false;
    }
  }
  V3.loadEnabledWidgets = loadEnabledWidgets;

  // Strategy block header (name·status + Engine Start/Stop buttons)
  function engineHeader(name, d) {
    if (name === 'focus' || name === 'harpoon') {
      const ready = d && d.ok;
      const on = ready && d.enabled;
      const st = ready ? ((d.enabled ? 'ON' : 'OFF') + (d.state ? ' · ' + d.state : '')) : '…';
      // 🧊 Manual COOLDOWN release — v2 focus-skip-cooldown-btn port. Shown only when FOCUS is in COOLDOWN
      const skipCd = (name === 'focus' && ready && d.state === 'COOLDOWN')
        ? ' <button class="v3-btn sm v3-skip-cd" style="border-color:#f9a825;color:#f9a825" title="Skip cooldown — manually clear COOLDOWN immediately (v2 Skip button)">⏭ Skip Cooldown</button>' : '';
      // 🏆 Trailing Take (Auto Take-Profit) active indicator — shows armed amount when auto_tp_enabled is ON
      const _cfg = (d && d.config) || {};
      const atpBadge = (name === 'focus' && _cfg.auto_tp_enabled)
        ? ' <span style="font-size:11px;font-weight:400;color:var(--v3-fg-mute);white-space:nowrap" title="Trailing take — arms when net profit exceeds $' + (Number(_cfg.auto_tp_usdt) || 0).toFixed(2) + ' → takes when it gives back ' + Math.round((Number(_cfg.auto_tp_peak_giveback_pct) || 0.4) * 100) + '% from peak (takes precedence over holding once armed)' + (_cfg.auto_sl_pct_enabled ? ' · loss cut ' + (Number(_cfg.auto_sl_pct) || 0) + '% ON' : '') + '">🏆 Trailing Take (armed $' + (Number(_cfg.auto_tp_usdt) || 0).toFixed(1) + ')</span>'
        : '';
      return '<div class="v3-block-head"><span class="v3-block-title">' + (ICON[name] || '') + ' ' + (LABEL[name] || name) + ' <small class="' + (on ? 'v3-pos' : 'v3-neg') + '">' + st + '</small>' + skipCd + atpBadge + '</span>' +
        '<span class="v3-eng-sw' + (on ? ' on' : '') + '" data-engine-sw="' + name + '" title="Highlighted side = current state · click the other side to switch">' +
        '<button class="es-seg es-start" data-act="start">▶ Start</button>' +
        '<button class="es-seg es-stop" data-act="stop">■ Stop</button></span></div>';
    }
    return '<div class="v3-block-head"><span class="v3-block-title">' + (ICON[name] || '') + ' ' + (LABEL[name] || name) + '</span>' +
      '<span class="v3-eng-phase">Engine — Phase 6</span></div>';
  }

  // 🖐 Manual Entry — full-width table (executes FOCUS entry. Split out of the settings ribbon → shown in body via right-side toggle). Gate bypass · direction as-is.
  function manualEntryHtml() {
    return '<div class="v3-widget-h">🖐 Manual Entry <small class="text-muted">(FOCUS · gate bypass · direction as-is · live trade)</small></div>' +
      '<table class="v3-postable" style="width:100%"><tbody><tr>' +
      '<td style="text-align:left;width:36%"><input id="v3-me-market" class="v3-input" type="text" placeholder="BTCUSDT (enter coin)" list="v3-market-list" autocomplete="off" style="max-width:240px"></td>' +
      '<td style="text-align:center"><button class="v3-btn v3-btn-long v3-me-go" data-dir="LONG">📈 LONG</button></td>' +
      '<td style="text-align:center"><button class="v3-btn v3-btn-long v3-me-go" data-dir="LONG" data-smart="1" title="Enter after signal confirmed (wait)">📈 LONG ⏳</button></td>' +
      '<td style="text-align:center"><button class="v3-btn v3-btn-short v3-me-go" data-dir="SHORT">📉 SHORT</button></td>' +
      '<td style="text-align:center"><button class="v3-btn v3-btn-short v3-me-go" data-dir="SHORT" data-smart="1" title="Enter after signal confirmed (wait)">📉 SHORT ⏳</button></td>' +
      '<td style="text-align:right;white-space:nowrap"><small class="text-muted">⏳ Wait</small> <input id="v3-me-timeout" class="v3-mini" type="number" value="60" min="1" max="240" step="5" style="width:58px"> <small class="text-muted">min</small></td>' +
      '</tr></tbody></table>' +
      '<small class="hint">⚠️ Live trade — immediate entry (L/S) or ⏳ signal wait (after signal confirmed within timeout). Safety guards kept · no auto FLIP. To fire straight from a candidate, use the L/S buttons in the GreenPen Scanner widget.</small>';
  }
  function focusBlock(d) {
    let html = '<section class="v3-block">' + engineHeader('focus', d);
    if (!d || !d.ok) return html + '<div class="v3-placeholder">Loading status…</div></section>';
    const w = V3.state.widgets || {};
    // 🔭 Regime Transition Watch (Phase K) — right-panel toggle (full-width at top when selected)
    if (w.phasek) html += '<div id="v3-phasek" class="v3-phasek">' + renderPhaseK() + '</div>';
    if (w.summary !== false) html += summaryHtml(d);
    // 📋 Positions + 📊 BTC analysis side table (right, rotates through held coins) — side by side
    if (w.positions !== false) {
      html += '<div class="v3-pos-row"><div class="v3-pos-main">' + positionsHtml(d) + '</div>' +
        '<div id="v3-wg-gp" class="v3-gp">' + renderGp() + '</div></div>';
    }
    html += buildWidgetsRow();   // short tables side by side (flex-wrap) — shared with home view
    // 🖐 Manual Entry — at the bottom (review all indicators above, enter below). Right-side toggle (default ON) → full-width table
    if (w.mentry !== false) html += '<div id="v3-wg-mentry" class="v3-widget v3-widget-wide" style="margin-top:10px">' + manualEntryHtml() + '</div>';
    return html + '</section>';
  }
  function stubBlock(name) {
    return '<section class="v3-block">' + engineHeader(name, null) +
      '<div class="v3-placeholder">' + (LABEL[name] || name) + ' trade panel + conditions will be wired in a later Phase (currently FOCUS·HARPOON running)</div></section>';
  }

  // ── 🐟 HARPOON trade panel — v2 loadHarpoonStatus/History(22555) port: state·stats·Current Scalp·FOCUS Link·Recent Scalps. (35-param Settings is in the Entry/Guards classification stage) ──
  function harpoonScalpHtml(st) {
    const s = st.current_scalp;
    let body;
    if (s) {
      body = '<table class="v3-kv-table">' +
        '<tr><td>Market</td><td><b>' + s.market + '</b></td></tr>' +
        '<tr><td>Direction</td><td><span class="v3-badge ' + (s.direction === 'LONG' ? 'long' : 'short') + '">' + s.direction + '</span></td></tr>' +
        '<tr><td>Entry</td><td>$' + Number(s.entry_price || 0).toFixed(4) + '</td></tr>' +
        '<tr><td>TP</td><td class="v3-pos">$' + Number(s.tp || 0).toFixed(4) + '</td></tr>' +
        '<tr><td>SL</td><td class="v3-neg">$' + Number(s.sl || 0).toFixed(4) + '</td></tr>' +
        '<tr><td>Qty</td><td>' + (s.qty || 0) + '</td></tr></table>';
    } else body = '<div class="v3-placeholder" style="padding:20px">🔱 No active scalp</div>';
    return '<div class="v3-hp-sec"><div class="v3-hp-h">🎯 Current Scalp</div>' + body + '</div>';
  }
  function harpoonFocusLinkHtml(st) {
    const adxVal = st.focus_adx || 0, adxOk = st.adx_ok !== false, thr = st.adx_threshold || 0;
    const src = st.adx_source === 'harpoon' ? 'own thr' : 'FOCUS thr';
    let adxStatus;
    if (adxVal <= 0) adxStatus = '<span class="text-muted">ADX not yet calculated</span>';
    else if (adxOk) adxStatus = '<span class="v3-badge long">ADX OK</span> ' + adxVal.toFixed(1) + ' ≥ ' + thr + ' (' + src + ')';
    else adxStatus = '<span class="v3-badge short">ADX LOW</span> ' + adxVal.toFixed(1) + ' < ' + thr + ' (' + src + ') — paused';
    const zone = st.target_zone ? (st.target_zone.type + ' $' + Number(st.target_zone.price_low || 0).toFixed(2) + ' ~ $' + Number(st.target_zone.price_high || 0).toFixed(2)) : 'Waiting for zone…';
    return '<div class="v3-hp-sec"><div class="v3-hp-h">🔗 FOCUS Link</div>' +
      '<table class="v3-kv-table">' +
      '<tr><td>FOCUS State</td><td><b>' + (st.focus_state || '-') + '</b></td></tr>' +
      '<tr><td>Market</td><td><b>' + (st.focus_market || '-') + '</b></td></tr>' +
      '<tr><td>ADX</td><td><b style="color:' + (adxOk ? 'var(--v3-long)' : 'var(--v3-short)') + '">' + (adxVal > 0 ? adxVal.toFixed(1) : '-') + '</b> <small class="text-muted">/ thr ' + (thr || '-') + ' · ' + (st.adx_source === 'harpoon' ? 'own' : 'FOCUS') + '</small></td></tr>' +
      '<tr><td colspan="2">' + adxStatus + '</td></tr>' +
      '<tr><td>Target Zone</td><td>' + zone + '</td></tr>' +
      '<tr><td>Direction</td><td>' + (st.target_direction || '-') + '</td></tr>' +
      '</table></div>';
  }
  function harpoonHistoryHtml(scalps) {
    let rows;
    if (!scalps.length) rows = '<tr><td colspan="8" class="v3-jempty">No scalps yet</td></tr>';
    else rows = scalps.map((s) => {
      const pc = (s.pnl_usdt || 0) >= 0 ? 'v3-pos' : 'v3-neg';
      const rb = s.result === 'TP' ? 'long' : s.result === 'SL' ? 'short' : 'mute';
      return '<tr><td>' + (s.scalp_id || '') + '</td><td><b class="v3-mkt" data-bybit="' + (s.market || '') + '">' + String(s.market || '').replace('USDT', '') + '</b></td>' +
        '<td><span class="v3-badge ' + (s.direction === 'LONG' ? 'long' : 'short') + '">' + (s.direction || '') + '</span></td>' +
        '<td>$' + Number(s.entry_price || 0).toFixed(2) + '</td><td>$' + Number(s.exit_price || 0).toFixed(2) + '</td>' +
        '<td class="' + pc + '">$' + Number(s.pnl_usdt || 0).toFixed(4) + '</td>' +
        '<td><span class="v3-badge ' + rb + '">' + (s.result || '') + '</span></td>' +
        '<td>' + Number(s.duration_sec || 0).toFixed(1) + 's</td></tr>';
    }).join('');
    return '<div class="v3-hp-sec"><div class="v3-hp-h">🔱 Recent Scalps</div>' +
      '<table class="v3-ltable"><thead><tr><th>#</th><th>Market</th><th>Dir</th><th>Entry</th><th>Exit</th><th>PnL</th><th>Result</th><th>Duration</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
  }
  function harpoonBlock(d) {
    const st = (d && d.status) || null;
    let html = '<section class="v3-block" id="v3-harpoon-block">' + engineHeader('harpoon', st);
    if (!st || !st.ok) return html + '<div class="v3-placeholder">Loading HARPOON status… (if the engine is OFF, ▶ Start)</div></section>';
    const c = st.config || {};
    const losses = st.consecutive_losses || 0;
    const hw = V3.state.hpwidgets || {};   // right-panel section toggles (Stats/Scalp/Link/History)
    if (hw.stats !== false) html += '<div class="v3-trade-summary">' +
      chip('Budget', '$' + Number(st.budget || 0).toFixed(2)) +
      chip('Leverage', (c.leverage || 20) + 'x') +
      chip('Scalps Today', (st.scalps_today || 0) + ' / ' + (st.max_daily_scalps || 15)) +
      chip('This Hour', (st.scalps_this_hour || 0)) +
      chip('Daily PnL', _money(st.daily_pnl)) +
      chip('Total PnL', _money(st.total_pnl)) +
      chip('Consec Loss', losses + ' / ' + (c.max_consecutive_loss || 3), losses >= 2 ? 'v3-neg' : '') +
      '</div>';
    let hpRow = '';
    if (hw.scalp !== false) hpRow += harpoonScalpHtml(st);
    if (hw.link !== false) hpRow += harpoonFocusLinkHtml(st);
    if (hpRow) html += '<div class="v3-hp-row">' + hpRow + '</div>';
    if (hw.history !== false) html += harpoonHistoryHtml((d && d.history) || []);
    return html + '</section>';
  }
  async function loadHarpoon(force) {
    const h = V3.state.harpoon, now = Date.now();
    if (!force && h.status && now - h.t < 4000) return;
    const [st, hist] = await Promise.all([
      V3.getJSON('/api/strategy/harpoon/status'),
      V3.getJSON('/api/strategy/harpoon/history?limit=20'),
    ]);
    h.status = st; h.history = (hist && hist.scalps) || []; h.t = now;
    if (V3.syncHarpoonConfig && st && st.config) V3.syncHarpoonConfig(st.config);   // fill HARPOON ribbon [data-cfg]
    if (!V3.state.envView && V3.state.selected.has('harpoon')) { const el = $('v3-harpoon-block'); if (el) el.outerHTML = harpoonBlock(h); }
  }
  V3.loadHarpoon = loadHarpoon;

  // ── ⚡ LIGHTNING (Tier-1 plugin, per-market deploy type) — trade panel = active deployment list / deploy form is in the ribbon (static) ──
  // Unlike FOCUS·HARPOON it has no single engine config: setup(deploy)/list(query)/stop(stop·liquidate·delete) model.
  function pluginHeader(name, count) {
    return '<div class="v3-block-head"><span class="v3-block-title">' + (ICON[name] || '') + ' ' + (LABEL[name] || name) +
      ' <small class="v3-badge mute">' + count + ' deployed</small></span>' +
      '<span class="v3-pos-actions"><button class="v3-btn sm ghost" data-plugin-refresh="' + name + '" title="Refresh">🔄</button></span></div>';
  }
  function ltgListHtml(items) {
    if (!items.length) return '<div class="v3-placeholder">No active LIGHTNING deployments — deploy a market via the form below</div>';
    const rows = items.map((it) => {
      const p = it.position || {}, pn = it.pnl || {}, pr = it.params || {};
      const qty = Number(p.qty || 0), entry = Number(p.entry || 0), val = Number(pn.value || 0);
      const cur = qty > 0 ? val / qty : 0;
      const amt = Number(pn.amount || 0), pct = Number(pn.pct || 0);
      const lt = (it.v2 && it.v2.lt_state) || '';
      return '<tr>' +
        '<td><b class="v3-mkt" data-bybit="' + it.market + '">' + (it.market || '').replace('USDT', '') + '</b>' + (lt ? ' <small class="text-muted">' + lt + '</small>' : '') + '</td>' +
        '<td>' + (it.state || '-') + '</td>' +
        '<td><span class="v3-badge mute">' + (it.am || 'M') + '</span></td>' +
        '<td>$' + Number(it.budget || 0).toFixed(0) + '</td>' +
        '<td>' + (entry ? _fp(entry) : '-') + '</td>' +
        '<td>' + (cur ? _fp(cur) : '-') + '</td>' +
        '<td>' + Number(pr.tp != null ? pr.tp : 0).toFixed(1) + '%</td>' +
        '<td class="v3-neg">' + Number(pr.sl != null ? pr.sl : 0).toFixed(1) + '%</td>' +
        '<td class="' + V3.pnlCls(amt) + '">' + (amt >= 0 ? '+' : '') + '$' + amt.toFixed(2) + ' <small>(' + (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%)</small></td>' +
        '<td class="v3-plug-actions">' +
          '<button class="v3-btn sm ghost v3-ltg-stop" data-mkt="' + it.market + '" data-act="stop" title="Stop (WATCH)">Stop</button>' +
          '<button class="v3-btn sm ghost v3-ltg-stop" data-mkt="' + it.market + '" data-act="liquidate" title="Liquidate (sell position)">Liquidate</button>' +
          '<button class="v3-btn sm v3-btn-outline-danger v3-ltg-stop" data-mkt="' + it.market + '" data-act="delete" title="Delete (DISABLED)">Delete</button>' +
        '</td></tr>';
    }).join('');
    return '<table class="v3-postable v3-postable-pos"><thead><tr><th>Market</th><th>State</th><th>A/M</th><th>Budget</th><th>Entry</th><th>Current</th><th>TP%</th><th>SL%</th><th>PnL</th><th>Actions</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  function lightningBlock(d) {
    const items = (d && d.items) || [];
    const sr = V3.state.lightning.showRecos;
    let h = '<section class="v3-block" id="v3-lightning-block">' + pluginHeader('lightning', items.length);
    // 🔍 Recommended coins (when right-side toggle is ON) — filter bar (static · title+filters+Rows in one line) + list (#v3-ltg-recos)
    if (sr) {
      const nrows = V3.state.lightning.recoRows || 5;
      const rowsSel = '<select id="v3-ltg-recorows" class="v3-mini" style="width:auto">' + [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 20].map((n) => '<option value="' + n + '"' + (n === nrows ? ' selected' : '') + '>' + n + '</option>').join('') + '</select>';
      h += '<div class="v3-ltg-recobar"><span class="v3-reco-h" style="margin:0">🔍 Recommended Coins</span>' +
        '<span class="v3-ltg-recofilter">min$ <input id="v3-ltg-rmin" class="v3-mini" type="number" value="0" min="0" step="any"> ' +
        'max$ <input id="v3-ltg-rmax" class="v3-mini" type="number" value="0" min="0" step="any"> ' +
        '<button class="v3-btn sm ghost" id="v3-ltg-recos-refresh">🔍 Load</button> Rows ' + rowsSel + '</span></div>' +
        '<div id="v3-ltg-recos">' + renderRecos(V3.state.lightning.recos) + '</div>';
    }
    // ⚡ Manual deploy form (body · one line: fields + buttons) — click a recommendation→fill→confirm/edit budget→deploy
    h += '<div class="v3-ltg-form"><div class="v3-ltg-form-row">' +
      '<div class="fld"><label class="v3-label">Market</label><input id="v3-ltg-market" class="v3-input" type="text" placeholder="BTCUSDT" list="v3-market-list" autocomplete="off"></div>' +
      '<div class="fld"><label class="v3-label">Budget</label><input id="v3-ltg-budget" class="v3-input" type="number" value="100" min="0" step="5"></div>' +
      '<div class="fld"><label class="v3-label">TP %</label><input id="v3-ltg-tp" class="v3-input" type="number" value="5.0" step="0.1"></div>' +
      '<div class="fld"><label class="v3-label">SL %</label><input id="v3-ltg-sl" class="v3-input" type="number" value="-3.0" step="0.1"></div>' +
      '<div class="fld"><label class="v3-label">Est. Profit</label><input id="v3-ltg-est" class="v3-input v3-pos" type="text" readonly placeholder="—" style="text-align:right"></div>' +
      '<button class="v3-btn v3-btn-long" id="v3-ltg-deploy">⚡ Deploy</button>' +
      '<button class="v3-btn" id="v3-ltg-update" title="Update params of an existing deployed market (re-setup)">Update</button>' +
      '<button class="v3-btn ghost" id="v3-ltg-recommend" title="Apply recommended TP/SL for the current Market">Suggested</button>' +
      '<label class="v3-recbud" title="Also set budget to the recommended value"><input type="checkbox" class="v3-cfg-chk" id="v3-ltg-recbudget"> Also set budget</label>' +
      '</div>' +
      '<small class="hint">Click a recommendation → fill form → confirm/edit budget → ⚡Deploy (manual). Update = re-setup an existing market. Auto entry uses the shared Slots (as many as the slot count).</small></div>';
    // 📋 Active deployment list (#v3-ltg-list, refreshed by loader)
    h += '<div id="v3-ltg-list">' + ltgListHtml(items) + '</div>';
    return h + '</section>';
  }
  async function loadLightning(force) {
    const l = V3.state.lightning, now = Date.now();
    if (!force && l.items && now - l.t < 5000) { const e0 = $('v3-ltg-list'); if (e0) e0.innerHTML = ltgListHtml(l.items); return; }
    const d = await V3.getJSON('/api/strategy/lightning/list');
    l.items = (d && d.items) || []; l.t = now;
    const lst = $('v3-ltg-list'); if (lst) lst.innerHTML = ltgListHtml(l.items);   // refresh active list only (preserve deploy form·reco inputs — no full re-render)
    const blk = $('v3-lightning-block'); if (blk) { const b = blk.querySelector('.v3-badge.mute'); if (b) b.textContent = l.items.length + ' deployed'; }
  }
  V3.loadLightning = loadLightning;
  // 🔍 Recommended coins (profile-matched = /api/strategy/recommendations profile ranking) — occupied coins not blocked (dedup), deploy budget = ribbon Budget (per funds)
  // v2 loadCandidates verbatim style: rich row + occupied = info badge/dimmed only (not blocked) + click row → fill deploy form
  function renderRecos(d) {
    if (!V3.state.lightning.showRecos) return '';   // right-panel toggle OFF → hidden
    if (!d) return '<div class="v3-placeholder">Click [🔍 Load] — profile-matched recommendations</div>';
    const all = d.items || [];
    if (!all.length) return '<div class="v3-placeholder">' + (d.computing ? 'Computing recommendations… (auto-refreshing, please wait 🔄)' : 'No recommendations — adjust the price filter (min/max)') + '</div>';
    const nrows = V3.state.lightning.recoRows || 5, total = all.length, pages = Math.max(1, Math.ceil(total / nrows));
    const pg = Math.min(Math.max(1, V3.state.lightning.recoPage || 1), pages);
    V3.state.lightning.recoPage = pg;
    const formBudget = parseFloat(($('v3-ltg-budget') && $('v3-ltg-budget').value) || '') || 100;   // estimated profit = based on the actual deploy budget (ribbon) (suggested_budget ignores capital → not shown/filled)
    const rows = all.slice((pg - 1) * nrows, pg * nrows).map((it) => {
      const mk = it.market || '', base = mk.replace('USDT', '');
      const active = it.active_strategy || null;   // occupying strategy name (if present, info badge + dimmed only; still clickable/deployable)
      const rp = it.recommended_params || {}, tp = rp.tp_pct, sl = rp.sl_pct;
      const budget = Number(it.suggested_budget_usdt || it.budget || 0), budOk = budget > 0 && budget <= 10000;   // capital-linked suggested budget (>$10k = guard against old KRW leftover)
      const chg = Number(it.change_rate || 0), rsi = Math.round(Number(it.rsi || 50));
      const rsiCls = rsi <= 30 ? 'v3-pos' : rsi >= 70 ? 'v3-neg' : 'text-muted';
      const mom = Number(it.momentum || 0), macd = mom > 0.05 ? '▲' : mom < -0.05 ? '▼' : '→';
      const macdCls = mom > 0.05 ? 'v3-pos' : mom < -0.05 ? 'v3-neg' : 'text-muted';
      const aiAdj = it.ai_adjusted_score != null ? Math.round(it.ai_adjusted_score * 100) : (it.ai_score != null ? Math.round(it.ai_score * 100) : '-');
      const shouldBuy = it.ai_should_buy !== false;
      const regime = it.regime || '', rfit = it.regime_fit != null ? Math.round(it.regime_fit * 100) : '-';
      const est = (tp != null) ? Math.round((budOk ? budget : formBudget) * tp / 100) : 0;
      const badge = active ? ' <span class="v3-badge warn" title="Occupied by another strategy — info only, still deployable">⚠️ ' + active + '</span>' : '';
      return '<div class="v3-reco-row' + (active ? ' v3-reco-held' : '') + '" data-mkt="' + mk + '" data-budget="' + (budOk ? Math.round(budget) : '') + '" data-tp="' + (tp != null ? tp : '') + '" data-sl="' + (sl != null ? sl : '') + '" title="Click → fill form (coin·budget·TP·SL)' + (active ? ' · occupied info only, still deployable' : '') + '">' +
        '<div class="v3-reco-l1"><span><b>' + base + '</b> <small class="text-muted">' + _fp(it.price) + '</small> <small class="' + (chg >= 0 ? 'v3-pos' : 'v3-neg') + '">' + (chg >= 0 ? '+' : '') + chg.toFixed(1) + '%</small>' + badge + '</span>' +
        '<span>' + (budOk ? '<small class="text-muted">$' + Math.round(budget) + '</small> ' : '') + (tp != null ? '<span class="v3-badge long">TP ' + tp + '%</span> ' : '') + (sl != null ? '<span class="v3-badge short">SL ' + sl + '%</span>' : '') + '</span></div>' +
        '<div class="v3-reco-l2"><span><span class="' + rsiCls + '">RSI ' + rsi + '</span> <span class="' + macdCls + '">MACD ' + macd + '</span>' + (est > 0 ? ' <span class="text-muted">|</span> <span class="v3-pos">+$' + est + ' est.</span>' : '') + '</span>' +
        '<span><span class="' + (shouldBuy ? 'v3-pos' : 'v3-warn') + '">AI ' + aiAdj + '%' + (shouldBuy ? '' : ' ⚠️') + '</span> <small class="text-muted"> ' + regime + ' ' + rfit + '%</small></span></div>' +
        '</div>';
    }).join('');
    let pager = '';
    if (pages > 1) {
      const btns = [];
      for (let i = 1; i <= pages; i++) btns.push(i === pg ? '<span class="v3-jpg cur">' + i + '</span>' : '<button class="v3-jpg v3-recopage" data-page="' + i + '">' + i + '</button>');
      pager = '<div class="v3-jpager">' + btns.join('') + '</div>';
    }
    return '<div class="v3-reco-list">' + rows + '</div>' +
      '<div class="v3-recometa"><small class="text-muted">Total ' + total + ' · click row→form · ⚠️=occupied info</small>' + pager + '</div>';
  }
  async function loadLightningRecos(force) {
    const l = V3.state.lightning, now = Date.now();
    if (!force && l.recos && l.recos.items && l.recos.items.length && now - l.recosT < 180000) { const e0 = $('v3-ltg-recos'); if (e0) e0.innerHTML = renderRecos(l.recos); return; }
    const mn = ($('v3-ltg-rmin') && $('v3-ltg-rmin').value) || '0';
    const mx = ($('v3-ltg-rmax') && $('v3-ltg-rmax').value) || '0';
    const e0 = $('v3-ltg-recos'); if (e0 && !(l.recos && l.recos.items && l.recos.items.length)) e0.innerHTML = '<div class="v3-placeholder">Loading recommendations… (a few seconds)</div>';
    const d = await V3.getJSON('/api/strategy/recommendations?strategy=LIGHTNING&n=20&min_price=' + encodeURIComponent(mn) + '&max_price=' + encodeURIComponent(mx));
    l.recos = d; l.recoPage = 1; l.recosT = now;
    const e1 = $('v3-ltg-recos'); if (e1) e1.innerHTML = renderRecos(d);
    if (d && d.computing && !(d.items && d.items.length)) { l._retry = (l._retry || 0) + 1; if (l._retry <= 8 && V3.state.active === 'lightning') setTimeout(() => loadLightningRecos(true), 4000); } else { l._retry = 0; }
  }
  V3.loadLightningRecos = loadLightningRecos;
  // Est. Profit = Budget × TP% / 100 (auto-computed in deploy form — v2 updateEstProfit)
  function updateLtgEst() {
    const b = parseFloat(($('v3-ltg-budget') && $('v3-ltg-budget').value) || '0') || 0;
    const tp = parseFloat(($('v3-ltg-tp') && $('v3-ltg-tp').value) || '0') || 0;
    const e = $('v3-ltg-est'); if (e) e.value = (b > 0 && tp) ? ('+$' + Math.round(b * tp / 100)) : '';
  }
  V3.updateLtgEst = updateLtgEst;
  // 🛡️ LIGHTNING Guards — fill the ribbon Guards panel (static panel → once on active entry + after save)
  async function loadLightningGuards() {
    const d = await V3.getJSON('/api/strategy/lightning/guards');
    const g = (d && d.guards) || {};
    const setC = (id, v) => { const el = $(id); if (el && document.activeElement !== el && v != null) el.checked = !!v; };
    const setV = (id, v) => { const el = $(id); if (el && document.activeElement !== el && v != null) el.value = v; };
    setC('v3-ltg-g-ob', g.entry_ob_guard_enabled);
    setC('v3-ltg-g-ceiling', g.entry_ceiling_guard);
    setC('v3-ltg-g-profit', g.exit_profit_guard);
    setV('v3-ltg-g-minprofit', g.exit_min_net_profit_pct);
    setV('v3-ltg-g-slippage', g.exit_slippage_guard_bps);
    setV('v3-ltg-g-minorder', g.min_order_usdt);
    setC('v3-ltg-g-abslock', g.user_sell_only);
    setC('v3-ltg-g-holdsell', g.hold_sell);
  }
  V3.loadLightningGuards = loadLightningGuards;

  // ════════════════════════════════════════════════════════════
  // 🎯 SNIPER (Tier-1) — mirrors the LIGHTNING pattern. Differences: large setup JSON body (required=market)·
  //   stop=query string (sniper_id preferred)·multiple sniper_id (several instances per market)·no guards endpoint·side(L/S).
  //   Reuses CSS classes (.v3-ltg-*/.v3-reco-*), id=v3-snp-*. Recommendations use the strategy=SNIPER profile (profile-matched).
  // ════════════════════════════════════════════════════════════
  function snpListHtml(items) {
    if (!items.length) return '<div class="v3-placeholder">No active SNIPER deployments — deploy a market via the form below (waits for entry signal)</div>';
    const rows = items.map((it) => {
      const p = it.position || {}, pn = it.pnl || {}, pr = it.params || {};
      const entry = Number(p.entry || 0), cur = Number(it.current_price || 0), tgt = Number(it.entry_target_price || 0);
      const amt = Number(pn.amount || 0), pct = Number(pn.pct || 0);
      const side = String(pr.side || it.side || 'LONG').toUpperCase();
      const tp = pr.tp_pct != null ? pr.tp_pct : (pr.tp != null ? pr.tp : 0);
      const sl = pr.sl_pct != null ? pr.sl_pct : (pr.sl != null ? pr.sl : 0);
      return '<tr>' +
        '<td><b class="v3-mkt" data-bybit="' + it.market + '">' + (it.market || '').replace('USDT', '') + '</b></td>' +
        '<td><span class="v3-badge ' + (side === 'SHORT' ? 'short' : 'long') + '">' + side + '</span></td>' +
        '<td>' + (it.state || '-') + '</td>' +
        '<td>$' + Number(it.budget || 0).toFixed(0) + '</td>' +
        '<td>' + (entry ? _fp(entry) : (tgt ? '<small class="text-muted">→' + _fp(tgt) + '</small>' : '-')) + '</td>' +
        '<td>' + (cur ? _fp(cur) : '-') + '</td>' +
        '<td>' + Number(tp).toFixed(1) + '%</td>' +
        '<td class="v3-neg">' + Number(sl).toFixed(1) + '%</td>' +
        '<td class="' + V3.pnlCls(amt) + '">' + (amt >= 0 ? '+' : '') + '$' + amt.toFixed(2) + ' <small>(' + (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%)</small></td>' +
        '<td class="v3-plug-actions">' +
          '<button class="v3-btn sm ghost v3-snp-stop" data-sid="' + (it.sniper_id || '') + '" data-mkt="' + it.market + '" data-act="stop" title="Stop (WATCH)">Stop</button>' +
          '<button class="v3-btn sm v3-btn-outline-danger v3-snp-stop" data-sid="' + (it.sniper_id || '') + '" data-mkt="' + it.market + '" data-act="delete" title="Delete (DISABLED)">Delete</button>' +
        '</td></tr>';
    }).join('');
    return '<table class="v3-postable v3-postable-pos"><thead><tr><th>Market</th><th>Side</th><th>State</th><th>Budget</th><th>Entry</th><th>Current</th><th>TP%</th><th>SL%</th><th>PnL</th><th>Actions</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  function sniperBlock(d) {
    const items = (d && d.items) || [];
    let h = '<section class="v3-block" id="v3-sniper-block">' + pluginHeader('sniper', items.length);
    if (V3.state.sniper.showRecos) {
      const nrows = V3.state.sniper.recoRows || 5;
      const rowsSel = '<select id="v3-snp-recorows" class="v3-mini" style="width:auto">' + [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 20].map((n) => '<option value="' + n + '"' + (n === nrows ? ' selected' : '') + '>' + n + '</option>').join('') + '</select>';
      h += '<div class="v3-ltg-recobar"><span class="v3-reco-h" style="margin:0">🎯 Recommended Coins (SNIPER profile)</span>' +
        '<span class="v3-ltg-recofilter">min$ <input id="v3-snp-rmin" class="v3-mini" type="number" value="0" min="0" step="any"> ' +
        'max$ <input id="v3-snp-rmax" class="v3-mini" type="number" value="0" min="0" step="any"> ' +
        '<button class="v3-btn sm ghost" id="v3-snp-recos-refresh">🔍 Load</button> Rows ' + rowsSel + '</span></div>' +
        '<div id="v3-snp-recos">' + renderSnpRecos(V3.state.sniper.recos) + '</div>';
    }
    h += '<div class="v3-ltg-form"><div class="v3-ltg-form-row">' +
      '<div class="fld"><label class="v3-label">Market</label><input id="v3-snp-market" class="v3-input" type="text" placeholder="BTCUSDT" list="v3-market-list" autocomplete="off"></div>' +
      '<div class="fld"><label class="v3-label">Side</label><select id="v3-snp-side" class="v3-input"><option value="LONG">LONG</option><option value="SHORT">SHORT</option></select></div>' +
      '<div class="fld"><label class="v3-label">Budget</label><input id="v3-snp-budget" class="v3-input" type="number" value="100" min="0" step="5"></div>' +
      '<div class="fld"><label class="v3-label">TP %</label><input id="v3-snp-tp" class="v3-input" type="number" value="2.0" step="0.1"></div>' +
      '<div class="fld"><label class="v3-label">SL %</label><input id="v3-snp-sl" class="v3-input" type="number" value="-2.5" step="0.1"></div>' +
      '<div class="fld"><label class="v3-label">Est. Profit</label><input id="v3-snp-est" class="v3-input v3-pos" type="text" readonly placeholder="—" style="text-align:right"></div>' +
      '<button class="v3-btn v3-btn-long" id="v3-snp-deploy">🎯 Deploy</button>' +
      '<button class="v3-btn" id="v3-snp-update" title="Re-setup an existing market (upsert)">Update</button>' +
      '<button class="v3-btn ghost" id="v3-snp-recommend" title="Apply recommended TP/SL for the current Market">Suggested</button>' +
      '<label class="v3-recbud" title="Also set budget to the recommended value"><input type="checkbox" class="v3-cfg-chk" id="v3-snp-recbudget"> Also set budget</label>' +
      '</div>' +
      '<small class="hint">Click a recommendation → fill form → confirm → 🎯Deploy. SNIPER waits for an entry signal (WATCH) then auto-enters. Update = re-setup (upsert).</small></div>';
    h += '<div id="v3-snp-list">' + snpListHtml(items) + '</div>';
    return h + '</section>';
  }
  async function loadSniper(force) {
    const l = V3.state.sniper, now = Date.now();
    if (!force && l.items && now - l.t < 5000) { const e0 = $('v3-snp-list'); if (e0) e0.innerHTML = snpListHtml(l.items); return; }
    const d = await V3.getJSON('/api/strategy/sniper/list');
    l.items = (d && d.items) || []; l.t = now;
    const lst = $('v3-snp-list'); if (lst) lst.innerHTML = snpListHtml(l.items);   // refresh active list only (preserve form·reco inputs)
    const blk = $('v3-sniper-block'); if (blk) { const b = blk.querySelector('.v3-badge.mute'); if (b) b.textContent = l.items.length + ' deployed'; }
  }
  V3.loadSniper = loadSniper;
  function renderSnpRecos(d) {
    if (!V3.state.sniper.showRecos) return '';
    if (!d) return '<div class="v3-placeholder">Click [🔍 Load] — SNIPER profile-matched recommendations</div>';
    const all = d.items || [];
    if (!all.length) return '<div class="v3-placeholder">' + (d.computing ? 'Computing recommendations… (auto-refreshing, please wait 🔄)' : 'No recommendations — adjust the price filter (min/max)') + '</div>';
    const nrows = V3.state.sniper.recoRows || 5, total = all.length, pages = Math.max(1, Math.ceil(total / nrows));
    const pg = Math.min(Math.max(1, V3.state.sniper.recoPage || 1), pages);
    V3.state.sniper.recoPage = pg;
    const formBudget = parseFloat(($('v3-snp-budget') && $('v3-snp-budget').value) || '') || 100;
    const rows = all.slice((pg - 1) * nrows, pg * nrows).map((it) => {
      const mk = it.market || '', base = mk.replace('USDT', '');
      const active = it.active_strategy || null;
      const rp = it.recommended_params || {}, tp = rp.tp_pct, sl = rp.sl_pct;
      const budget = Number(it.suggested_budget_usdt || it.budget || 0), budOk = budget > 0 && budget <= 10000;
      const chg = Number(it.change_rate || 0), rsi = Math.round(Number(it.rsi || 50));
      const rsiCls = rsi <= 30 ? 'v3-pos' : rsi >= 70 ? 'v3-neg' : 'text-muted';
      const mom = Number(it.momentum || 0), macd = mom > 0.05 ? '▲' : mom < -0.05 ? '▼' : '→';
      const macdCls = mom > 0.05 ? 'v3-pos' : mom < -0.05 ? 'v3-neg' : 'text-muted';
      const aiAdj = it.ai_adjusted_score != null ? Math.round(it.ai_adjusted_score * 100) : (it.ai_score != null ? Math.round(it.ai_score * 100) : '-');
      const shouldBuy = it.ai_should_buy !== false;
      const regime = it.regime || '', rfit = it.regime_fit != null ? Math.round(it.regime_fit * 100) : '-';
      const est = (tp != null) ? Math.round((budOk ? budget : formBudget) * tp / 100) : 0;
      const badge = active ? ' <span class="v3-badge warn" title="Occupied by another strategy — info only, still deployable">⚠️ ' + active + '</span>' : '';
      return '<div class="v3-reco-row' + (active ? ' v3-reco-held' : '') + '" data-mkt="' + mk + '" data-budget="' + (budOk ? Math.round(budget) : '') + '" data-tp="' + (tp != null ? tp : '') + '" data-sl="' + (sl != null ? sl : '') + '" title="Click → fill form' + (active ? ' · occupied info only, still deployable' : '') + '">' +
        '<div class="v3-reco-l1"><span><b>' + base + '</b> <small class="text-muted">' + _fp(it.price) + '</small> <small class="' + (chg >= 0 ? 'v3-pos' : 'v3-neg') + '">' + (chg >= 0 ? '+' : '') + chg.toFixed(1) + '%</small>' + badge + '</span>' +
        '<span>' + (budOk ? '<small class="text-muted">$' + Math.round(budget) + '</small> ' : '') + (tp != null ? '<span class="v3-badge long">TP ' + tp + '%</span> ' : '') + (sl != null ? '<span class="v3-badge short">SL ' + sl + '%</span>' : '') + '</span></div>' +
        '<div class="v3-reco-l2"><span><span class="' + rsiCls + '">RSI ' + rsi + '</span> <span class="' + macdCls + '">MACD ' + macd + '</span>' + (est > 0 ? ' <span class="text-muted">|</span> <span class="v3-pos">+$' + est + ' est.</span>' : '') + '</span>' +
        '<span><span class="' + (shouldBuy ? 'v3-pos' : 'v3-warn') + '">AI ' + aiAdj + '%' + (shouldBuy ? '' : ' ⚠️') + '</span> <small class="text-muted"> ' + regime + ' ' + rfit + '%</small></span></div>' +
        '</div>';
    }).join('');
    let pager = '';
    if (pages > 1) { const btns = []; for (let i = 1; i <= pages; i++) btns.push(i === pg ? '<span class="v3-jpg cur">' + i + '</span>' : '<button class="v3-jpg v3-recopage" data-page="' + i + '">' + i + '</button>'); pager = '<div class="v3-jpager">' + btns.join('') + '</div>'; }
    return '<div class="v3-reco-list">' + rows + '</div>' +
      '<div class="v3-recometa"><small class="text-muted">Total ' + total + ' · click row→form · ⚠️=occupied info</small>' + pager + '</div>';
  }
  async function loadSniperRecos(force) {
    const l = V3.state.sniper, now = Date.now();
    if (!force && l.recos && l.recos.items && l.recos.items.length && now - l.recosT < 180000) { const e0 = $('v3-snp-recos'); if (e0) e0.innerHTML = renderSnpRecos(l.recos); return; }
    const mn = ($('v3-snp-rmin') && $('v3-snp-rmin').value) || '0';
    const mx = ($('v3-snp-rmax') && $('v3-snp-rmax').value) || '0';
    const e0 = $('v3-snp-recos'); if (e0 && !(l.recos && l.recos.items && l.recos.items.length)) e0.innerHTML = '<div class="v3-placeholder">Loading recommendations… (a few seconds)</div>';
    const d = await V3.getJSON('/api/strategy/recommendations?strategy=SNIPER&n=20&min_price=' + encodeURIComponent(mn) + '&max_price=' + encodeURIComponent(mx));
    l.recos = d; l.recoPage = 1; l.recosT = now;
    const e1 = $('v3-snp-recos'); if (e1) e1.innerHTML = renderSnpRecos(d);
    // If computing (cold/semaphore busy), auto-retry — appears without user action once ready (SNIPER is 6th in prewarm so it warms up late)
    if (d && d.computing && !(d.items && d.items.length)) { l._retry = (l._retry || 0) + 1; if (l._retry <= 8 && V3.state.active === 'sniper') setTimeout(() => loadSniperRecos(true), 4000); } else { l._retry = 0; }
  }
  V3.loadSniperRecos = loadSniperRecos;
  function updateSnpEst() {
    const b = parseFloat(($('v3-snp-budget') && $('v3-snp-budget').value) || '0') || 0;
    const tp = parseFloat(($('v3-snp-tp') && $('v3-snp-tp').value) || '0') || 0;
    const e = $('v3-snp-est'); if (e) e.value = (b > 0 && tp) ? ('+$' + Math.round(b * tp / 100)) : '';
  }
  V3.updateSnpEst = updateSnpEst;

  // ════════════════════════════════════════════════════════════
  // 🔌 Generic Tier-1 plugin panel (config-driven) — shared by GAZUA/CONTRARIAN/LADDER (de-duped).
  //   LIGHTNING/SNIPER are the earlier bespoke ones (to be migrated here later). New = just add config to PLUG.
  //   element id = v3-{key}-* / scoping = .v3-plug-recos[data-plug] + .v3-plug-* (separate from LIGHTNING/SNIPER).
  // ════════════════════════════════════════════════════════════
  const PLUG = {
    gazua: {
      label: 'GAZUA', strategy: 'GAZUA', api: 'gazua', stopMode: 'body',
      actions: ['stop', 'liquidate', 'delete'], hasSide: false, defTp: '15.0', defSl: '-10.0',
      recoTitle: '🚀 Recommended Coins (GAZUA profile)',
      hint: 'Click a recommendation -> fill form -> confirm -> deploy. GAZUA targets high-volatility surges (high TP, deep SL). Update = re-setup.',
      setupBody: (f) => ({ market: f.market, budget_usdt: f.budget, tp_pct: f.tp, sl_pct: f.sl }),
    },
    contrarian: {
      label: 'CONTRARIAN', strategy: 'CONTRARIAN', api: 'contrarian', stopMode: 'query',
      actions: ['stop', 'delete'], hasSide: false, defTp: '15.0', defSl: '-50.0',   // stop=query (no liquidate) - counter-trend deep SL
      recoTitle: '🔄 Contrarian Coins (vs benchmark)',
      hint: 'Click a recommendation -> fill form -> confirm -> deploy. CONTRARIAN targets counter-trend bounces (deep SL = patience underwater). Update = re-setup.',
      setupBody: (f) => ({ market: f.market, budget_usdt: f.budget, tp_pct: f.tp, sl_pct: f.sl }),
      // ★ Contrarian-only scanner - uses /contrarian/scan (benchmark=contrarian baseline) instead of recommendations (owner: "contrarian-baseline option")
      scan: { path: '/api/strategy/contrarian/scan', defBenchmark: 'BTC', benchmarks: [['BTC', 'BTC'], ['ETH', 'ETH'], ['MARKET_AVG', 'Market Avg'], ['FEAR_GREED', 'Fear/Greed']] },
    },
  };
  function plugState(key) { if (!V3.state[key]) V3.state[key] = { items: null, t: 0, recos: null, recosT: 0, showRecos: true, recoRows: 5, recoPage: 1 }; return V3.state[key]; }
  function plugListHtml(key, items) {
    const c = PLUG[key];
    if (!items.length) return '<div class="v3-placeholder">No active ' + c.label + ' deployments - deploy a market via the form below</div>';
    const rows = items.map((it) => {
      const p = it.position || {}, pn = it.pnl || {}, pr = it.params || {};
      // ★ [2026-06-02 owner] entry-proximity gauge (selector score) - WATCH coins only (ACTIVE=already entered)
      const _es = Number(it.entry_score || 0), _isWatch = String(it.state || '').toUpperCase() === 'WATCH';
      const _esCell = !_isWatch ? '<span class="text-muted">—</span>' : (_es <= 0 ? '<small class="text-muted">Awaiting eval</small>' : ('<div class="v3-escore" title="selector score ' + _es + ' - higher = entry imminent"><b>' + _es.toFixed(0) + '</b><span class="v3-escore-bar"><i style="width:' + Math.min(100, _es) + '%"></i></span></div>'));
      const qty = Number(p.qty || 0), entry = Number(p.entry || 0), val = Number(pn.value || 0);
      const cur = qty > 0 ? val / qty : 0, amt = Number(pn.amount || 0), pct = Number(pn.pct || 0);
      const tp = pr.tp_pct != null ? pr.tp_pct : (pr.tp != null ? pr.tp : 0);
      const sl = pr.sl_pct != null ? pr.sl_pct : (pr.sl != null ? pr.sl : 0);
      const acts = c.actions.map((a) => {
        const lab = a === 'liquidate' ? 'Liquidate' : a === 'delete' ? 'Delete' : 'Stop';
        const cls = a === 'delete' ? 'v3-btn sm v3-btn-outline-danger' : 'v3-btn sm ghost';
        const ttl = a === 'liquidate' ? 'Liquidate (sell position)' : a === 'delete' ? 'Delete (DISABLED)' : 'Stop (WATCH)';
        return '<button class="' + cls + ' v3-plug-stop" data-plug="' + key + '" data-mkt="' + it.market + '" data-act="' + a + '" title="' + ttl + '">' + lab + '</button>';
      }).join('');
      return '<tr><td><b class="v3-mkt" data-bybit="' + it.market + '">' + (it.market || '').replace('USDT', '') + '</b></td>' +
        '<td>' + (it.state || '-') + '</td><td>' + _esCell + '</td><td>$' + Number(it.budget || 0).toFixed(0) + '</td>' +
        '<td>' + (entry ? _fp(entry) : '-') + '</td><td>' + (cur ? _fp(cur) : '-') + '</td>' +
        '<td>' + Number(tp).toFixed(1) + '%</td><td class="v3-neg">' + Number(sl).toFixed(1) + '%</td>' +
        '<td class="' + V3.pnlCls(amt) + '">' + (amt >= 0 ? '+' : '') + '$' + amt.toFixed(2) + ' <small>(' + (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%)</small></td>' +
        '<td class="v3-plug-actions">' + acts + '</td></tr>';
    }).join('');
    return '<table class="v3-postable v3-postable-pos"><thead><tr><th>Market</th><th>State</th><th>🎯 Proximity</th><th>Budget</th><th>Entry</th><th>Current</th><th>TP%</th><th>SL%</th><th>PnL</th><th>Actions</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  function renderPlugRecos(key, d) {
    const st = plugState(key), c = PLUG[key];
    if (!st.showRecos) return '';
    if (!d) return '<div class="v3-placeholder">Click [🔍 Load] - ' + (c.scan ? 'select a contrarian baseline then scan' : c.label + ' profile-matched recommendations') + '</div>';
    const all = (c.scan ? d.candidates : d.items) || [];
    if (!all.length) return '<div class="v3-placeholder">' + (c.scan ? 'No contrarian coins - try a different benchmark' : (d.computing ? 'Computing recommendations… (auto-refreshing, please wait 🔄)' : 'No recommendations — adjust the price filter (min/max)')) + '</div>';
    const nrows = st.recoRows || 5, total = all.length, pages = Math.max(1, Math.ceil(total / nrows));
    const pg = Math.min(Math.max(1, st.recoPage || 1), pages); st.recoPage = pg;
    const formBudget = parseFloat(($('v3-' + key + '-budget') && $('v3-' + key + '-budget').value) || '') || 100;
    const rows = all.slice((pg - 1) * nrows, pg * nrows).map((it) => {
      if (c.scan) {   // 🔄 contrarian scan row - coin vs benchmark return / contrarian score / RS / Corr / AI (no price/tp/sl -> default TP/SL on click)
        const mk = it.market || '', base = mk.replace('USDT', '');
        const cret = Number(it.coin_ret_pct || 0), bret = Number(it.benchmark_ret_pct || 0), score = it.score || 0, rsd = Number(it.rs_diff || 0), corr = it.corr;
        const ai = it.ai_score != null ? Math.round(it.ai_score * 100) : '-';
        return '<div class="v3-reco-row" data-mkt="' + mk + '" data-budget="" data-tp="' + c.defTp + '" data-sl="' + c.defSl + '" title="Click -> fill form (CONTRARIAN default TP/SL)">' +
          '<div class="v3-reco-l1"><span><b>' + base + '</b> <small class="' + (cret >= 0 ? 'v3-pos' : 'v3-neg') + '">' + (cret >= 0 ? '+' : '') + cret.toFixed(1) + '%</small> <small class="text-muted">vs base ' + (bret >= 0 ? '+' : '') + bret.toFixed(1) + '%</small>' + (it.early_signal ? ' <span class="v3-badge warn">early</span>' : '') + '</span>' +
          '<span><span class="v3-badge ' + (score >= 2 ? 'long' : 'mute') + '">Contrarian ' + score + '/3</span></span></div>' +
          '<div class="v3-reco-l2"><span><span class="' + (rsd >= 0 ? 'v3-pos' : 'v3-neg') + '">RS ' + (rsd >= 0 ? '+' : '') + rsd.toFixed(1) + '</span> <span class="text-muted">Corr ' + (corr != null ? Number(corr).toFixed(2) : '-') + '</span></span>' +
          '<span><span class="text-muted">AI ' + ai + '%</span></span></div></div>';
      }
      const mk = it.market || '', base = mk.replace('USDT', ''), active = it.active_strategy || null;
      const rp = it.recommended_params || {}, tp = rp.tp_pct, sl = rp.sl_pct;
      const budget = Number(it.suggested_budget_usdt || it.budget || 0), budOk = budget > 0 && budget <= 10000;
      const chg = Number(it.change_rate || 0), rsi = Math.round(Number(it.rsi || 50));
      const rsiCls = rsi <= 30 ? 'v3-pos' : rsi >= 70 ? 'v3-neg' : 'text-muted';
      const mom = Number(it.momentum || 0), macd = mom > 0.05 ? '▲' : mom < -0.05 ? '▼' : '→';
      const macdCls = mom > 0.05 ? 'v3-pos' : mom < -0.05 ? 'v3-neg' : 'text-muted';
      const aiAdj = it.ai_adjusted_score != null ? Math.round(it.ai_adjusted_score * 100) : (it.ai_score != null ? Math.round(it.ai_score * 100) : '-');
      const shouldBuy = it.ai_should_buy !== false;
      const regime = it.regime || '', rfit = it.regime_fit != null ? Math.round(it.regime_fit * 100) : '-';
      const est = (tp != null) ? Math.round((budOk ? budget : formBudget) * tp / 100) : 0;
      const badge = active ? ' <span class="v3-badge warn" title="Occupied by another strategy — info only, still deployable">⚠️ ' + active + '</span>' : '';
      return '<div class="v3-reco-row' + (active ? ' v3-reco-held' : '') + '" data-mkt="' + mk + '" data-budget="' + (budOk ? Math.round(budget) : '') + '" data-tp="' + (tp != null ? tp : '') + '" data-sl="' + (sl != null ? sl : '') + '" title="Click → fill form' + (active ? ' · occupied info only, still deployable' : '') + '">' +
        '<div class="v3-reco-l1"><span><b>' + base + '</b> <small class="text-muted">' + _fp(it.price) + '</small> <small class="' + (chg >= 0 ? 'v3-pos' : 'v3-neg') + '">' + (chg >= 0 ? '+' : '') + chg.toFixed(1) + '%</small>' + badge + '</span>' +
        '<span>' + (budOk ? '<small class="text-muted">$' + Math.round(budget) + '</small> ' : '') + (tp != null ? '<span class="v3-badge long">TP ' + tp + '%</span> ' : '') + (sl != null ? '<span class="v3-badge short">SL ' + sl + '%</span>' : '') + '</span></div>' +
        '<div class="v3-reco-l2"><span><span class="' + rsiCls + '">RSI ' + rsi + '</span> <span class="' + macdCls + '">MACD ' + macd + '</span>' + (est > 0 ? ' <span class="text-muted">|</span> <span class="v3-pos">+$' + est + ' est.</span>' : '') + '</span>' +
        '<span><span class="' + (shouldBuy ? 'v3-pos' : 'v3-warn') + '">AI ' + aiAdj + '%' + (shouldBuy ? '' : ' ⚠️') + '</span> <small class="text-muted"> ' + regime + ' ' + rfit + '%</small></span></div></div>';
    }).join('');
    let pager = '';
    if (pages > 1) { const btns = []; for (let i = 1; i <= pages; i++) btns.push(i === pg ? '<span class="v3-jpg cur">' + i + '</span>' : '<button class="v3-jpg v3-recopage" data-page="' + i + '">' + i + '</button>'); pager = '<div class="v3-jpager">' + btns.join('') + '</div>'; }
    const meta = c.scan ? ('Base: ' + (d.benchmark_label || d.benchmark_type || '') + ' ' + (d.benchmark_ret_pct != null ? ((Number(d.benchmark_ret_pct) >= 0 ? '+' : '') + Number(d.benchmark_ret_pct).toFixed(1) + '%') : '') + (d.market_down ? ' · market down' : '') + ' · Total ' + total + ' · click row->form') : ('Total ' + total + ' · click row->form · ⚠️=occupied info');
    return '<div class="v3-reco-list">' + rows + '</div><div class="v3-recometa"><small class="text-muted">' + meta + '</small>' + pager + '</div>';
  }
  function plugBlock(key, d) {
    const c = PLUG[key], st = plugState(key), items = (d && d.items) || [];
    let h = '<section class="v3-block" id="v3-' + key + '-block">' + pluginHeader(key, items.length);
    if (st.showRecos) {
      const nrows = st.recoRows || 5;
      const rowsSel = '<select id="v3-' + key + '-recorows" class="v3-mini" style="width:auto">' + [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 20].map((n) => '<option value="' + n + '"' + (n === nrows ? ' selected' : '') + '>' + n + '</option>').join('') + '</select>';
      const filt = c.scan
        ? ('Contrarian benchmark <select id="v3-' + key + '-benchmark" class="v3-mini" style="width:auto">' + c.scan.benchmarks.map((bm) => '<option value="' + bm[0] + '"' + ((st.benchmark || c.scan.defBenchmark) === bm[0] ? ' selected' : '') + '>' + bm[1] + '</option>').join('') + '</select> ')
        : ('min$ <input id="v3-' + key + '-rmin" class="v3-mini" type="number" value="0" min="0" step="any"> max$ <input id="v3-' + key + '-rmax" class="v3-mini" type="number" value="0" min="0" step="any"> ');
      h += '<div class="v3-ltg-recobar"><span class="v3-reco-h" style="margin:0">' + c.recoTitle + '</span>' +
        '<span class="v3-ltg-recofilter">' + filt +
        '<button class="v3-btn sm ghost v3-plug-recos-refresh" data-plug="' + key + '">🔍 Load</button> Rows ' + rowsSel + '</span></div>' +
        '<div class="v3-plug-recos" data-plug="' + key + '" id="v3-' + key + '-recos">' + renderPlugRecos(key, st.recos) + '</div>';
    }
    h += '<div class="v3-ltg-form"><div class="v3-ltg-form-row">' +
      '<div class="fld"><label class="v3-label">Market</label><input id="v3-' + key + '-market" class="v3-input" type="text" placeholder="BTCUSDT" list="v3-market-list" autocomplete="off"></div>' +
      (c.hasSide ? '<div class="fld"><label class="v3-label">Side</label><select id="v3-' + key + '-side" class="v3-input"><option value="LONG">LONG</option><option value="SHORT">SHORT</option></select></div>' : '') +
      '<div class="fld"><label class="v3-label">Budget</label><input id="v3-' + key + '-budget" class="v3-input" type="number" value="100" min="0" step="5"></div>' +
      '<div class="fld"><label class="v3-label">TP %</label><input id="v3-' + key + '-tp" class="v3-input" type="number" value="' + c.defTp + '" step="0.1"></div>' +
      '<div class="fld"><label class="v3-label">SL %</label><input id="v3-' + key + '-sl" class="v3-input" type="number" value="' + c.defSl + '" step="0.1"></div>' +
      '<div class="fld"><label class="v3-label">Est. Profit</label><input id="v3-' + key + '-est" class="v3-input v3-pos" type="text" readonly placeholder="—" style="text-align:right"></div>' +
      '<button class="v3-btn v3-btn-long v3-plug-deploy" data-plug="' + key + '">' + (ICON[key] || '🚀') + ' Deploy</button>' +
      '<button class="v3-btn v3-plug-deploy" data-plug="' + key + '" data-upd="1" title="Re-setup an existing market">Update</button>' +
      '<button class="v3-btn ghost v3-plug-recommend" data-plug="' + key + '" title="Apply recommended TP/SL for the current Market">Suggested</button>' +
      '<label class="v3-recbud" title="Also set budget to the recommended value"><input type="checkbox" class="v3-cfg-chk" id="v3-' + key + '-recbudget"> Also set budget</label>' +
      '</div><small class="hint">' + c.hint + '</small></div>';
    h += '<div id="v3-' + key + '-list">' + plugListHtml(key, items) + '</div>';
    return h + '</section>';
  }
  async function loadPlug(key, force) {
    const c = PLUG[key], st = plugState(key), now = Date.now();
    if (!force && st.items && now - st.t < 5000) { const e0 = $('v3-' + key + '-list'); if (e0) e0.innerHTML = plugListHtml(key, st.items); return; }
    const d = await V3.getJSON('/api/strategy/' + c.api + '/list');
    st.items = (d && d.items) || []; st.t = now;
    const lst = $('v3-' + key + '-list'); if (lst) lst.innerHTML = plugListHtml(key, st.items);
    const blk = $('v3-' + key + '-block'); if (blk) { const b = blk.querySelector('.v3-badge.mute'); if (b) b.textContent = st.items.length + ' deployed'; }
  }
  V3.loadPlug = loadPlug;
  async function loadPlugRecos(key, force) {
    const c = PLUG[key], st = plugState(key), now = Date.now();
    const cur = st.recos && (c.scan ? st.recos.candidates : st.recos.items);
    if (!force && cur && cur.length && now - st.recosT < (c.scan ? 30000 : 180000)) { const e0 = $('v3-' + key + '-recos'); if (e0) e0.innerHTML = renderPlugRecos(key, st.recos); return; }
    const e0 = $('v3-' + key + '-recos'); if (e0 && !(cur && cur.length)) e0.innerHTML = '<div class="v3-placeholder">' + (c.scan ? 'Scanning contrarian coins…' : 'Loading recommendations… (a few seconds)') + '</div>';
    let d;
    if (c.scan) {   // 🔄 contrarian-only scanner (benchmark=baseline)
      const bm = st.benchmark || c.scan.defBenchmark;
      d = await V3.getJSON(c.scan.path + '?benchmark=' + encodeURIComponent(bm) + '&force=' + (force ? 'true' : 'false'));
    } else {
      const mn = ($('v3-' + key + '-rmin') && $('v3-' + key + '-rmin').value) || '0';
      const mx = ($('v3-' + key + '-rmax') && $('v3-' + key + '-rmax').value) || '0';
      d = await V3.getJSON('/api/strategy/recommendations?strategy=' + c.strategy + '&n=20&min_price=' + encodeURIComponent(mn) + '&max_price=' + encodeURIComponent(mx));
    }
    st.recos = d; st.recoPage = 1; st.recosT = now;
    const e1 = $('v3-' + key + '-recos'); if (e1) e1.innerHTML = renderPlugRecos(key, d);
    if (!c.scan && d && d.computing && !(d.items && d.items.length)) { st._retry = (st._retry || 0) + 1; if (st._retry <= 8 && V3.state.active === key) setTimeout(() => loadPlugRecos(key, true), 4000); } else { st._retry = 0; }
  }
  V3.loadPlugRecos = loadPlugRecos;
  function updatePlugEst(key) {
    const b = parseFloat(($('v3-' + key + '-budget') && $('v3-' + key + '-budget').value) || '0') || 0;
    const tp = parseFloat(($('v3-' + key + '-tp') && $('v3-' + key + '-tp').value) || '0') || 0;
    const e = $('v3-' + key + '-est'); if (e) e.value = (b > 0 && tp) ? ('+$' + Math.round(b * tp / 100)) : '';
  }
  V3.updatePlugEst = updatePlugEst;

  // ════════════════════════════════════════════════════════════
  // 📐 LADDER - read-only (view grid structure only, no orders). Owner: "drawing lines on the exchange feels risky" -> deploy/orders in a later stage.
  // ════════════════════════════════════════════════════════════
  function ladderStepsHtml(d) {
    if (!d || !d.ok) return '<div class="v3-placeholder">No step info - ' + ((d && d.error) || 'select a market') + '</div>';
    const pos = d.position || {}, pn = d.pnl || {}, amt = Number(pn.amount || 0), pct = Number(pn.pct || 0);
    const head = '<div style="margin:8px 0 4px;font-size:12px"><b class="v3-mkt" data-bybit="' + d.market + '">' + (d.market || '').replace('USDT', '') + '</b> ' +
      '<small class="text-muted">Base ' + _fp(d.base_price) + ' · Now ' + _fp(d.current_price) + ' · Step ' + d.next_step + '/' + d.max_steps + ' · Gap ' + d.step_pct + '% · TP ' + d.tp_pct + '% · Budget $' + Number(d.budget || 0).toFixed(0) + '</small> · Held ' + Number(pos.qty || 0).toFixed(4) + ' @ ' + _fp(pos.avg_buy) + ' <span class="' + V3.pnlCls(amt) + '">(' + (amt >= 0 ? '+' : '') + '$' + amt.toFixed(0) + ' ' + (pct >= 0 ? '+' : '') + pct.toFixed(1) + '%)</span></div>';
    const rows = (d.steps || []).map((s) => {
      const lab = s.status === 'filled' ? '<span class="v3-pos">● Filled</span>' : s.status === 'next' ? '<span class="v3-badge warn">◀ Next</span>' : '<span class="text-muted">○ Pending</span>';
      return '<tr><td>' + s.step + '</td><td>' + _fp(s.price) + '</td><td>$' + Number(s.budget || 0).toFixed(0) + '</td><td>' + lab + '</td></tr>';
    }).join('');
    return head + '<table class="v3-postable"><thead><tr><th>Step</th><th>Price (line)</th><th>Amount</th><th>Status</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  // 🔧 Live orders (grid_state, has uuid) - edit/pause/delete individual step price/qty. Separate from the computed plan (above): orders actually placed on the exchange.
  function ladderOrdersHtml(g) {
    if (!g || !g.ok) return '';
    const mkt = g.market, all = (g.steps || []);
    if (!all.length) return '<div class="v3-placeholder" style="margin-top:8px">🔧 No active live orders - orders are created on the exchange when you [🌱 Place] or save with 🟢 Auto ON</div>';
    const rows = all.map((s) => {
      const sideLab = s.side === 'buy' ? '<span class="v3-pos">Buy</span>' : '<span class="v3-neg">Sell</span>';
      const stLab = s.filled ? '<span class="text-muted">● Filled</span>' : s.status === 'paused' ? '<span class="v3-badge warn">⏸ Paused</span>' : '<span class="v3-pos">○ Active</span>';
      const acts = s.filled ? '<small class="text-muted">—</small>' :
        '<button class="v3-btn sm ghost v3-lad-step-pause" data-mkt="' + mkt + '" data-uuid="' + s.uuid + '" data-st="' + (s.status === 'paused' ? 'active' : 'paused') + '">' + (s.status === 'paused' ? '▶ Resume' : '⏸ Pause') + '</button>' +
        '<button class="v3-btn sm ghost v3-lad-step-edit" data-mkt="' + mkt + '" data-uuid="' + s.uuid + '" data-price="' + s.price + '" data-amount="' + (s.amount || 0) + '">✏️ Edit</button>' +
        '<button class="v3-btn sm v3-btn-outline-danger v3-lad-step-del" data-mkt="' + mkt + '" data-uuid="' + s.uuid + '">🗑 Delete</button>';
      return '<tr><td>' + sideLab + '</td><td>' + _fp(s.price) + '</td><td>$' + Number(s.amount || 0).toFixed(0) + '</td><td>' + stLab + '</td><td class="v3-plug-actions">' + acts + '</td></tr>';
    }).join('');
    return '<div style="margin:10px 0 4px;font-size:12px"><b>🔧 Active Live Orders</b> <small class="text-muted">(' + all.length + ' · price ' + _fp(g.current_price) + ') - edit/pause/delete individual steps <b style="color:var(--v3-warn)">(live orders!)</b></small></div>' +
      '<table class="v3-postable"><thead><tr><th>Side</th><th>Price (line)</th><th>Amount</th><th>Status</th><th>Edit</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  function ladderListHtml(items) {
    if (!items.length) return '<div class="v3-placeholder">No active LADDER</div>';
    const rows = items.map((it) => {
      const p = it.position || {}, pn = it.pnl || {}, pr = it.params || {};
      const qty = Number(p.qty || 0), entry = Number(p.entry || 0), amt = Number(pn.amount || 0), pct = Number(pn.pct || 0), cur = Number(pn.current_price || 0);
      return '<tr>' +
        '<td><b class="v3-mkt" data-bybit="' + it.market + '">' + (it.market || '').replace('USDT', '') + '</b></td>' +
        '<td>' + (it.state || '-') + '</td>' +
        '<td>' + (it.next_step || 1) + '/' + (pr.max_steps || 10) + ' <small class="text-muted">' + (pr.step_pct || 1) + '%' + (pr.martingale > 1 ? '·M' + pr.martingale : '') + '</small></td>' +
        '<td>$' + Number(it.budget || 0).toFixed(0) + '</td>' +
        '<td>' + (qty > 0 ? Number(qty).toFixed(4) + ' @ ' + _fp(entry) : '-') + '</td>' +
        '<td>' + (cur ? _fp(cur) : '-') + '</td>' +
        '<td class="' + V3.pnlCls(amt) + '">' + (amt >= 0 ? '+' : '') + '$' + amt.toFixed(2) + ' <small>(' + (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%)</small></td>' +
        '<td><small class="text-muted">B' + (it.buy_count || 0) + '·S' + (it.sell_count || 0) + '</small></td>' +
        '<td class="v3-plug-actions"><button class="v3-btn sm ghost v3-ladder-steps-btn" data-mkt="' + it.market + '">📊 Steps</button>' +
          '<button class="v3-btn sm v3-ladder-seed" data-mkt="' + it.market + '" title="Place grid limit buy orders (live orders!)">🌱 Place</button>' +
          '<button class="v3-btn sm v3-btn-outline-danger v3-ladder-cancel" data-mkt="' + it.market + '" title="Cancel all LADDER orders for this market">🗑️ Cancel</button></td>' +
        '</tr>';
    }).join('');
    return '<table class="v3-postable v3-postable-pos"><thead><tr><th>Market</th><th>State</th><th>Step</th><th>Budget</th><th>Position</th><th>Current</th><th>PnL</th><th>B/S</th><th>Orders</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  function ladderBlock(d) {
    const items = (d && d.items) || [];
    let h = '<section class="v3-block" id="v3-ladder-block">' + pluginHeader('ladder', items.length);
    h += '<div class="v3-placeholder" style="text-align:left;margin:8px 0;border-left:3px solid var(--v3-warn);padding-left:10px">📐 Grid ladder - after configuring, save with <b>🟢 Auto ON</b> to open a slot and <b>auto-order/trade (done, runs by itself)</b>. Save with <b>OFF</b> to configure only (no orders); test via [📊 View Steps]. Operator: "open a slot, save + ON, done."</div>';
    // 📐 Grid configuration form (no orders - buy_now=false, grid_auto_sync=false)
    h += '<div class="v3-ltg-form"><div class="v3-ltg-form-row">' +
      '<div class="fld"><label class="v3-label">Market</label><input id="v3-ladder-market" class="v3-input" type="text" placeholder="BTCUSDT" list="v3-market-list" autocomplete="off"></div>' +
      '<div class="fld"><label class="v3-label">Budget</label><input id="v3-ladder-budget" class="v3-input" type="number" value="100" min="0" step="5"></div>' +
      '<div class="fld"><label class="v3-label">Steps <small>(max_steps)</small></label><input id="v3-ladder-maxsteps" class="v3-input" type="number" value="10" min="1" max="40" step="1"></div>' +
      '<div class="fld"><label class="v3-label">Gap % <small>(step)</small></label><input id="v3-ladder-steppct" class="v3-input" type="number" value="1.0" min="0.1" step="0.1"></div>' +
      '<div class="fld"><label class="v3-label">Per-step amount <small>(0=auto)</small></label><input id="v3-ladder-order" class="v3-input" type="number" value="0" min="0" step="1"></div>' +
      '<div class="fld"><label class="v3-label">Martingale <small>(1=even)</small></label><input id="v3-ladder-mart" class="v3-input" type="number" value="1.0" min="1" step="0.05"></div>' +
      '<div class="fld"><label class="v3-label">TP %</label><input id="v3-ladder-tp" class="v3-input" type="number" value="2.0" step="0.1"></div>' +
      '<label class="v3-recbud" title="ON=open slot and start auto-trading (live orders) / OFF=configure only (no orders, test)"><input type="checkbox" class="v3-cfg-chk" id="v3-ladder-autosync"> 🟢 Auto ON</label>' +
      '<button class="v3-btn v3-btn-long" id="v3-ladder-deploy">📐 Save</button>' +
      '</div><small class="hint">🟢 Auto ON = slot opens -> grid auto-orders/trades (done, runs by itself). OFF = config saved only (no orders); test via [📊 View Steps] then turn ON again. Per-step amount 0 = budget / steps.</small></div>';
    h += '<div id="v3-ladder-list">' + ladderListHtml(items) + '</div>';
    h += '<div id="v3-ladder-steps">' + (V3.state.ladder.stepsMarket ? (ladderStepsHtml(V3.state.ladder.steps) + ladderOrdersHtml(V3.state.ladder.orders)) : '<div class="v3-placeholder">Click [📊 View Steps] on a row -> shows grid steps (computed plan) + active live orders (edit/pause/delete each)</div>') + '</div>';
    return h + '</section>';
  }
  async function loadLadder(force) {
    const l = V3.state.ladder, now = Date.now();
    if (!force && l.items && now - l.t < 5000) { const e0 = $('v3-ladder-list'); if (e0) e0.innerHTML = ladderListHtml(l.items); return; }
    const d = await V3.getJSON('/api/strategy/ladder/list');
    l.items = (d && d.items) || []; l.t = now;
    const lst = $('v3-ladder-list'); if (lst) lst.innerHTML = ladderListHtml(l.items);
    const blk = $('v3-ladder-block'); if (blk) { const b = blk.querySelector('.v3-badge.mute'); if (b) b.textContent = l.items.length + ' ladders'; }
    if (l.stepsMarket) loadLadderSteps(l.stepsMarket);   // refresh the open steps
  }
  V3.loadLadder = loadLadder;
  async function loadLadderSteps(market) {
    const l = V3.state.ladder; l.stepsMarket = market;
    const mq = encodeURIComponent(market);
    const [d, g] = await Promise.all([
      V3.getJSON('/api/strategy/ladder/steps?market=' + mq),       // computed plan
      V3.getJSON('/api/ladder/grid/state?market=' + mq),           // live orders (uuid)
    ]);
    l.steps = d; l.orders = g;
    const e = $('v3-ladder-steps'); if (e) e.innerHTML = ladderStepsHtml(d) + ladderOrdersHtml(g);
  }
  V3.loadLadderSteps = loadLadderSteps;

  // ════════════════════════════════════════════════════════════
  // 🔌 Plugin common settings (Reserved /api/reserved/settings) - managed in one place (owner)
  // Each input: data-rk = POST param name / data-rget = GET snapshot dot-path (GET nested <-> POST flat)
  // No backend changes (existing endpoints). Strategy TP/SL, Autopilot, Triage, Guard Matrix are the next stage.
  // ════════════════════════════════════════════════════════════
  const PLUGS8 = ['pingpong', 'autoloop', 'ladder', 'lightning', 'gazua', 'contrarian', 'sniper', 'whale'];
  const PLUGS7 = ['pingpong', 'autoloop', 'ladder', 'lightning', 'gazua', 'contrarian', 'sniper'];
  const TPSL_STRATS = ['PINGPONG', 'AUTOLOOP', 'LADDER', 'LIGHTNING', 'GAZUA', 'CONTRARIAN', 'SNIPER'];
  function _rget(snap, path) { return String(path).split('.').reduce((o, k) => (o == null ? undefined : o[k]), snap); }
  // src = reserved|guards|triage|tpsl · rk = save key (or tpsl dot-path) · g = GET dot-path · kind = chk|num|text
  function _R(label, src, rk, g, kind, attrs) {
    const a = 'data-src="' + src + '" data-rk="' + rk + '" data-rget="' + g + '" data-kind="' + kind + '"';
    if (kind === 'chk') return '<label class="rib-row"><span>' + label + '</span><input type="checkbox" class="v3-cfg-chk v3-rin" ' + a + '></label>';
    const t = kind === 'text' ? 'text' : 'number';
    const cls = kind === 'text' ? 'v3-mini v3-rin' : 'v3-mini v3-rin';
    return '<div class="rib-row"><span>' + label + '</span><input class="' + cls + '" type="' + t + '" ' + (attrs || '') + ' ' + a + '></div>';
  }
  function _sub(t) { return '<div class="v3-cset-sub">' + t + '</div>'; }
  function _prettyLabel(k) { return String(k).replace(/_/g, ' '); }
  // Auto-render exclusions - present in GET but (a) POST/PATCH does not accept so not saved (b) internal state/path values (c) dangerous toggles (d) duplicated elsewhere
  const _GUARDS_SKIP = new Set(['emergency_stop', 'ui_settings_loaded', 'btc_guard_mode', 'btc_guard_enabled']);  // btc_guard_enabled=duplicate of Demotion / emergency_stop=E-STOP state / rest=not accepted by POST
  const _TRIAGE_SKIP = new Set(['state_path']);   // internal file path (lists like exempt_strategies are auto-excluded via typeof object)
  // Auto-render the whole GET response dict (flat) - eliminates hardcoding gaps + auto-exposes new backend keys (owner). skip = exclude non-settable
  function _autoRows(src, obj, skip) {
    if (!obj || typeof obj !== 'object') return '';
    return Object.keys(obj).sort().map((k) => {
      if (skip && skip.has(k)) return '';
      const v = obj[k];
      if (v !== null && typeof v === 'object') return '';   // skip lists/nested dicts (exempt_strategies etc.)
      const kind = (typeof v === 'boolean') ? 'chk' : (typeof v === 'number') ? 'num' : 'text';
      return _R(_prettyLabel(k), src, k, k, kind, kind === 'num' ? 'step="any"' : '');
    }).join('');
  }
  function _csec(title, body, csave, hint) {
    return '<section class="v3-cset" data-csave="' + csave + '"><div class="v3-cset-h">' + title + '</div>' + body +
      '<button class="v3-btn v3-btn-long rsave">✓ Save ' + title + '</button>' +
      (hint ? '<small class="hint" style="display:block;color:var(--v3-fg-mute);margin-top:6px;">' + hint + '</small>' : '') + '</section>';
  }
  function pluginsCommonBlock() {
    // 🎰 Slots & Budget (reserved)
    let slots = _R('Capital-based Auto Allocation', 'reserved', 'auto_slot_enabled', 'auto_slot_enabled', 'chk') +
      _R('candidate price min usdt', 'reserved', 'candidate_price_min_usdt', 'candidate_price_min_usdt', 'num', 'step="any" min="0"') +
      _R('candidate price max usdt', 'reserved', 'candidate_price_max_usdt', 'candidate_price_max_usdt', 'num', 'step="any" min="0"') +
      _R('apply suggested budget', 'reserved', 'apply_suggested_budget', 'apply_suggested_budget', 'chk') +
      _R('promote to active', 'reserved', 'promote_to_active', 'promote_to_active', 'chk');
    slots += PLUGS8.filter((p) => !TIER2.includes(p)).map((p) => _sub(p.toUpperCase()) +   // Tier-2 (PINGPONG/AUTOLOOP/WHALE) are in each plugin ribbon - avoid duplicate edits/overwrites
      _R('enabled', 'reserved', p + '_enabled', p + '_enabled', 'chk') +
      _R('slots (0~20)', 'reserved', p + '_n', p + '_n', 'num', 'step="1" min="0" max="20"') +
      _R('budget usdt (0=auto)', 'reserved', p + '_budget_usdt', p + '_budget_usdt', 'num', 'step="any" min="0"')).join('');
    slots += _sub('SNIPER(s) SCOPE') + _R('snipers slots (0~20)', 'reserved', 'snipers_n', 'snipers_n', 'num', 'step="1" min="0" max="20"');
    slots += '<div class="rib-row" style="margin-top:10px;align-items:flex-start"><small class="text-muted">🤖 ON/OFF · slots · budget for <b>PINGPONG · AUTOLOOP · WHALE</b> are in <b>each plugin ribbon ⚙️ Slots·Tuning</b> (avoid duplicate edits / overwrites). Only the 5 Tier-1 strategies here.</small></div>';
    // ⬇️ Demotion Rules (reserved)
    const demo = _R('BTC Guard Mode', 'reserved', 'btc_guard_mode', 'autopilot.btc_guard_mode', 'chk') +
      _R('No Fills demote', 'reserved', 'autopilot_idle_demote_enabled', 'autopilot.idle_demote_enabled', 'chk') +
      _R('Idle min', 'reserved', 'autopilot_idle_demote_min', 'autopilot.idle_demote_min', 'num', 'step="1" min="0"') +
      _R('→ LongHold (idle)', 'reserved', 'autopilot_idle_to_longhold_enabled', 'autopilot.idle_to_longhold_enabled', 'chk') +
      _R('→ LongHold hours', 'reserved', 'autopilot_idle_to_longhold_hours', 'autopilot.idle_to_longhold_hours', 'num', 'step="1" min="1" max="168"') +
      _R('LongHold Auto Sell', 'reserved', 'longhold_auto_sell', 'autopilot.longhold_auto_sell', 'chk') +
      _R('LongHold target return %', 'reserved', 'longhold_target_pct', 'autopilot.longhold_target_pct', 'num', 'step="any"') +
      _R('LongHold check interval min', 'reserved', 'longhold_check_interval_min', 'autopilot.longhold_check_interval_min', 'num', 'step="any"') +
      _R('LongHold stop-loss %', 'reserved', 'longhold_stop_loss_pct', 'autopilot.longhold_stop_loss_pct', 'num', 'step="any"') +
      _R('Global Profit Take', 'reserved', 'global_profit_take', 'autopilot.global_profit_take', 'chk') +
      _R('Target return %', 'reserved', 'global_profit_pct', 'autopilot.global_profit_pct', 'num', 'step="any"') +
      _R('Check interval min', 'reserved', 'global_profit_interval_min', 'autopilot.global_profit_interval_min', 'num', 'step="any"') +
      _R('Common safety SL floor %', 'reserved', 'global_min_sl_pct', 'autopilot.global_min_sl_pct', 'num', 'step="any"') +
      _R('Auto profit lock-in (partial sell)', 'reserved', 'profit_lock_enabled', 'autopilot.profit_lock_enabled', 'chk') +
      _R('Profit Lock trigger %', 'reserved', 'profit_lock_trigger_pct', 'autopilot.profit_lock_trigger_pct', 'num', 'step="any"') +
      _R('Profit Lock partial-sell ratio', 'reserved', 'profit_lock_sell_ratio', 'autopilot.profit_lock_sell_ratio', 'num', 'step="any" min="0.05" max="0.95"') +
      _R('Profit Lock cooldown h', 'reserved', 'profit_lock_cooldown_h', 'autopilot.profit_lock_cooldown_h', 'num', 'step="any"');
    // 🎯 Strategy TP/SL common (reserved -> strategy_tp_sl JSON). rk = policy dot-path
    let tpsl = _R('Use guard', 'tpsl', 'enabled', 'strategy_tp_sl.enabled', 'chk') +
      _R('TP floor %', 'tpsl', 'tp_floor_pct', 'strategy_tp_sl.tp_floor_pct', 'num', 'step="any"') +
      _R('SL floor %', 'tpsl', 'sl_floor_pct', 'strategy_tp_sl.sl_floor_pct', 'num', 'step="any"') +
      _R('Use time-relax', 'tpsl', 'time_relax_enabled', 'strategy_tp_sl.time_relax_enabled', 'chk') +
      _R('Interval (N hours)', 'tpsl', 'time_relax_step_hours', 'strategy_tp_sl.time_relax_step_hours', 'num', 'step="any"') +
      _R('Step count', 'tpsl', 'time_relax_steps', 'strategy_tp_sl.time_relax_steps', 'num', 'step="1" min="1" max="24"') +
      _R('TP step decrease', 'tpsl', 'time_relax_tp_step', 'strategy_tp_sl.time_relax_tp_step', 'num', 'step="any"') +
      _R('SL step decrease', 'tpsl', 'time_relax_sl_step', 'strategy_tp_sl.time_relax_sl_step', 'num', 'step="any"') +
      _R('Min TP %', 'tpsl', 'time_relax_min_tp_pct', 'strategy_tp_sl.time_relax_min_tp_pct', 'num', 'step="any"') +
      _R('Min SL %', 'tpsl', 'time_relax_min_sl_pct', 'strategy_tp_sl.time_relax_min_sl_pct', 'num', 'step="any"');
    tpsl += TPSL_STRATS.map((s) => _sub(s + ' (TP / SL)') +
      _R('TP %', 'tpsl', 'per_strategy.' + s + '.tp_pct', 'strategy_tp_sl.per_strategy.' + s + '.tp_pct', 'num', 'step="any"') +
      _R('SL %', 'tpsl', 'per_strategy.' + s + '.sl_pct', 'strategy_tp_sl.per_strategy.' + s + '.sl_pct', 'num', 'step="any"')).join('');
    // 🔫 SNIPER DCA (reserved)
    const dca = _R('DCA step %', 'reserved', 'sniper_dca_step_pct', 'sniper_dca.dca_step_pct', 'num', 'step="any" min="0.1" max="5"') +
      _R('DCA add ratio', 'reserved', 'sniper_dca_add_ratio', 'sniper_dca.dca_add_ratio', 'num', 'step="any" min="0.1" max="2"') +
      _R('Max depth %', 'reserved', 'sniper_dca_max_depth_pct', 'sniper_dca.dca_max_depth_pct', 'num', 'step="any" min="0.2" max="10"');
    // 📊 Backtest Weights (reserved)
    const bt = PLUGS7.map((p) => _R(p.toUpperCase(), 'reserved', 'backtest_weight_' + p, 'backtest_weights.' + p, 'num', 'step="0.05" min="0" max="1"')).join('');
    // 🤖 Autopilot (reserved)
    let ap = _R('autopilot enabled', 'reserved', 'autopilot_enabled', 'autopilot.enabled', 'chk') +
      _R('auto approve', 'reserved', 'autopilot_auto_approve', 'autopilot.auto_approve', 'chk') +
      _R('auto engine start (boot)', 'reserved', 'auto_engine_start', 'autopilot.auto_engine_start', 'chk') +
      _R('window enabled', 'reserved', 'autopilot_window_enabled', 'autopilot.window_enabled', 'chk') +
      _R('window start (HH:MM)', 'reserved', 'autopilot_window_start', 'autopilot.window_start', 'text', 'style="width:70px"') +
      _R('window end (HH:MM)', 'reserved', 'autopilot_window_end', 'autopilot.window_end', 'text', 'style="width:70px"') +
      _R('eval interval sec', 'reserved', 'autopilot_eval_interval_sec', 'autopilot.eval_interval_sec', 'num', 'step="1" min="5"') +
      _R('grace sec', 'reserved', 'autopilot_grace_sec', 'autopilot.grace_sec', 'num', 'step="1" min="0"') +
      _R('demote max total', 'reserved', 'autopilot_demote_max_total', 'autopilot.demote_max_total', 'num', 'step="1" min="0" max="50"') +
      _R('demote max per strategy', 'reserved', 'autopilot_demote_max_per_strategy', 'autopilot.demote_max_per_strategy', 'num', 'step="1" min="0" max="50"') +
      _R('guard demote enabled', 'reserved', 'autopilot_guard_demote_enabled', 'autopilot.guard_demote_enabled', 'chk') +
      _R('guard demote window min', 'reserved', 'autopilot_guard_demote_window_min', 'autopilot.guard_demote_window_min', 'num', 'step="1" min="0"') +
      _R('guard demote n', 'reserved', 'autopilot_guard_demote_n', 'autopilot.guard_demote_n', 'num', 'step="1" min="0"') +
      _R('signal miss enabled', 'reserved', 'autopilot_signal_miss_enabled', 'autopilot.signal_miss_enabled', 'chk') +
      _R('signal miss window min', 'reserved', 'autopilot_signal_miss_window_min', 'autopilot.signal_miss_window_min', 'num', 'step="1" min="0"') +
      _R('signal miss min attempts', 'reserved', 'autopilot_signal_miss_min_attempts', 'autopilot.signal_miss_min_attempts', 'num', 'step="1" min="0"');
    ap += _sub('AutoApprove (per strategy + min confidence %)') + PLUGS8.map((p) =>
      _R(p.toUpperCase() + ' approve', 'reserved', 'auto_approve_' + p, 'autopilot.auto_approve_' + p, 'chk') +
      _R(p.toUpperCase() + ' min conf %', 'reserved', 'auto_approve_min_confidence_' + p, 'autopilot.auto_approve_min_confidence_' + p, 'num', 'step="any" min="0" max="100"')).join('');
    // 🚑 Triage / 🛡️ Guard Matrix section body = auto-render all GET response keys (loadPluginsCommon injects into the placeholder).
    // ★ Expand the whole GET instead of a hardcoded field list -> eliminates gaps/duplicates/forgetting (owner insight).
    return '<section class="v3-block"><div class="v3-block-head"><span class="v3-block-title">🔌 Plugin Common Settings <small class="v3-badge mute">all settings</small></span>' +
      '<span class="v3-pos-actions"><button class="v3-btn sm ghost" id="v3-pcommon-refresh" title="Refresh">🔄</button></span></div>' +
      '<div class="v3-cset-grid">' +
      _csec('🎰 Slots & Budget', slots, 'reserved', 'Per-plugin ON/OFF, slots, budget + auto-allocation + SNIPER(s) scope. One market = one strategy (auto-prevents double-claiming).') +
      _csec('⬇️ Demotion Rules', demo, 'reserved', 'No-trade demotion, LongHold conversion, Global Profit Take, auto profit lock-in (profit-lock link).') +
      _csec('🎯 Strategy TP/SL Common', tpsl, 'tpsl', 'Common TP/SL floors + time-relax + per-strategy TP/SL. Saves to strategy_tp_sl JSON.') +
      _csec('🔫 SNIPER DCA', dca, 'reserved', 'SNIPER/SNIPER(s) DCA step, ratio, depth.') +
      _csec('📊 Backtest Weights', bt, 'reserved', 'How much backtest weighs in when live data is scarce (0~1).') +
      _csec('🤖 Autopilot', ap, 'reserved', 'Auto operation, approval, demotion, per-strategy AutoApprove + min confidence.') +
      _csec('🚑 Triage Mode', '<div id="v3-auto-triage"></div>', 'triage', 'Focused loss recovery - auto-exposes all GET /api/triage/status keys (PATCH save).') +
      _csec('🛡️ Guard Matrix (Global)', '<div id="v3-auto-guards"></div>', 'guards', 'All global guards - auto-exposes all GET /api/system/guards keys. (scope/dust etc. are a separate area, later)') +
      '</div>' +
      '<small class="hint" style="display:block;color:var(--v3-fg-mute);margin-top:10px;">★ All common settings - Reserved + Triage + Guard Matrix in one place. Guard/Triage auto-expose every key in the GET response (no hardcoding gaps/duplicates/forgetting). Each section saves to its own endpoint.</small></section>';
  }
  async function loadPluginsCommon(force) {
    const pc = V3.state.pcommon;
    const [rs, gd, tr] = await Promise.all([
      V3.getJSON('/api/reserved/settings'),
      V3.getJSON('/api/system/guards'),
      V3.getJSON('/api/triage/status'),
    ]);
    const sources = {
      reserved: (rs && rs.settings) || {}, tpsl: (rs && rs.settings) || {},
      guards: (gd && gd.guards) || {}, triage: (tr && tr.settings) || {},
    };
    pc.sources = sources;
    // 🛡️ Guard Matrix / 🚑 Triage = auto-inject every GET-response key into the placeholder (eliminates hardcoding gaps/forgetting)
    const ga = $('v3-auto-guards'); if (ga) ga.innerHTML = _autoRows('guards', sources.guards, _GUARDS_SKIP);
    const ta = $('v3-auto-triage'); if (ta) ta.innerHTML = _autoRows('triage', sources.triage, _TRIAGE_SKIP);
    document.querySelectorAll('.v3-rin').forEach((el) => {
      if (document.activeElement === el) return;
      const src = sources[el.dataset.src]; if (!src) return;
      const v = _rget(src, el.dataset.rget);
      if (v == null) return;
      if (el.type === 'checkbox') el.checked = !!v; else el.value = v;
    });
  }
  V3.loadPluginsCommon = loadPluginsCommon;

  // 🤖 Tier-2 autopilot slot-type (PINGPONG/AUTOLOOP/WHALE) - just assembles existing reserved slot settings (no new guards, no new code).
  //   Reuses _R+_csec -> saves via the existing .rsave handler, populate is auto-filled into .v3-rin by loadPluginsCommon.
  // 🤖 Tier-2 settings = ribbon (slot enable/n/budget + unique tuning). Like other plugins, settings live in the ribbon. applyActivePanels injects the active plugin's.
  function tier2RibbonHtml(key) {
    const cfg = _R('Strategy ON/OFF (enable)', 'reserved', key + '_enabled', key + '_enabled', 'chk') +
      _R('Slot count (0=stopped, natural OFF)', 'reserved', key + '_n', key + '_n', 'num', 'step="1" min="0" max="20"') +
      _R('Budget usdt (0=auto)', 'reserved', key + '_budget_usdt', key + '_budget_usdt', 'num', 'step="any" min="0"');
    const hint = 'Enable slot + slot count > 0 + budget -> autopilot enters/trades on its own (done). Slots 0 = natural stop. Working coins shown in main.';
    return '<div class="v3-cset-grid">' + _csec('🔌 ' + (LABEL[key] || key) + ' Slots', cfg, 'reserved', hint) +
      '<section class="v3-cset"><div class="v3-cset-h">🎛️ ' + (LABEL[key] || key) + ' Unique Tuning</div>' +
      (TIER2_TUNE[key] || []).map((f) => '<div class="rib-row"><span>' + f[1] + ' <small>(' + f[0] + ')</small></span><input class="v3-mini" type="number" step="any" id="v3-tune-' + key + '-' + f[0] + '" value="' + f[2] + '"></div>').join('') +
      '<button class="v3-btn v3-btn-long v3-tune-save" data-plug="' + key + '">✓ Save Tuning (applied on slot entry)</button>' +
      '<small class="hint" style="display:block;color:var(--v3-fg-mute);margin-top:6px;">When autopilot fills a slot it enters with these values (blank=default). ★ Tuning applies after a server restart.</small></section>' +
      '</div>';
  }
  V3.tier2RibbonHtml = tier2RibbonHtml;
  // 🤖 Tier-2 main = work status (coins autopilot entered by filling a slot). Settings are in the ribbon.
  function tier2WorkBlock(key) {
    const w = (V3.state.tier2work && V3.state.tier2work[key]) || null;
    let body;
    if (!w) body = '<div class="v3-placeholder">Loading work status…</div>';
    else if (!w.length) body = '<div class="v3-placeholder">No working coins - enable a slot + save a budget in the top ribbon <b>⚙️ Slots·Tuning</b>, and autopilot will pick coins and show them here.</div>';
    else body = '<table class="v3-postable"><thead><tr><th>Market</th><th>State</th><th>Price</th><th>Budget</th></tr></thead><tbody>' +
      w.map((m) => '<tr><td><b class="v3-mkt" data-bybit="' + m.market + '">' + (m.market || '').replace('USDT', '') + '</b></td><td>' + (m.state || '-') + '</td><td>' + (m.price ? _fp(m.price) : '-') + '</td><td>' + Number(m.budget_usdt || 0).toFixed(0) + ' USDT</td></tr>').join('') + '</tbody></table>';
    return '<section class="v3-block" id="v3-' + key + '-block">' +
      '<div class="v3-block-head"><span class="v3-block-title">' + (ICON[key] || '🤖') + ' ' + (LABEL[key] || key) + ' <small class="v3-badge mute">slot-type (autopilot)</small></span></div>' +
      '<div style="font-size:11.5px;color:var(--v3-fg-mute);margin:4px 0 8px">🤖 Settings are in the top ribbon <b>⚙️ Slots·Tuning</b> / below are the <b>working coins</b> autopilot entered by filling slots. (Full positions also in 🏠 Overall Status)</div>' +
      '<h3 style="font-size:12px;font-weight:normal;color:var(--v3-fg-mute);margin:6px 0 3px">📊 Working Coins</h3>' + body +
      '<h3 style="font-size:12px;font-weight:normal;color:var(--v3-fg-mute);margin:14px 0 3px">🎯 Recommended Coins <small>(suited to ' + (key === 'pingpong' ? 'range/sideways' : key === 'autoloop' ? 'scaling-in/liquidity' : 'profile') + ') - 🤖 Enqueue = autopilot reviews first then enters (semi-auto)</small></h3>' +
      '<div id="v3-' + key + '-reco">' + tier2RecoHtml(key) + '</div>' +
      '</section>';
  }
  V3.tier2WorkBlock = tier2WorkBlock;
  async function loadTier2Work() {
    const sys = await V3.getJSON('/api/system/status');
    const s = (sys && sys.system) || {}, oma = s.oma || {}, prices = s.active_prices || {};
    const act = (oma.active || []).map((m) => Object.assign({ state: 'ACTIVE' }, m));
    const rec = (oma.recovery || []).map((m) => Object.assign({ state: 'RECOVERY' }, m));
    const all = act.concat(rec);
    V3.state.tier2work = V3.state.tier2work || {};
    TIER2.forEach((key) => {
      V3.state.tier2work[key] = all.filter((m) => ('' + (m.strategy || '')).toUpperCase() === key.toUpperCase())
        .map((m) => ({ market: m.market, state: m.state, budget_usdt: m.budget_usdt, price: prices[m.market] }));
      if (V3.state.selected.has(key) && !V3.state.envView && !V3.state.pcommonView && !V3.state.homeView) { const el = $('v3-' + key + '-block'); if (el) el.outerHTML = tier2WorkBlock(key); }
    });
  }
  V3.loadTier2Work = loadTier2Work;
  // 🎯 Tier-2 recommended coins (profile-matched) + autopilot priority enqueue (semi-auto)
  function tier2RecoHtml(key) {
    const r = (V3.state.tier2reco && V3.state.tier2reco[key]) || null;
    if (!r) return '<div class="v3-placeholder">Loading recommendations…</div>';
    if (r.computing) return '<div class="v3-placeholder">Computing recommendations… (a few seconds - auto-refreshing)</div>';
    const items = r.items || [];
    if (!items.length) return '<div class="v3-placeholder">No candidates - ' + ((r.err) || 'try again shortly') + '</div>';
    const rows = items.map((it) => {
      const chg = Number(it.change_rate || 0), score = Number(it.ai_adjusted_score != null ? it.ai_adjusted_score : (it.ai_score || 0)), bud = Number(it.suggested_budget_usdt || 0);
      return '<tr><td><b class="v3-mkt" data-bybit="' + it.market + '">' + (it.market || '').replace('USDT', '') + '</b></td>' +
        '<td>' + _fp(it.price) + '</td>' +
        '<td class="' + (chg >= 0 ? 'v3-pos' : 'v3-neg') + '">' + (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%</td>' +
        '<td>' + (score ? (score * 100).toFixed(0) : '-') + '</td>' +
        '<td>' + (it.rsi != null ? Number(it.rsi).toFixed(0) : '-') + '</td>' +
        '<td>' + (bud ? bud.toFixed(0) + ' USDT' : '-') + '</td>' +
        '<td><button class="v3-btn sm v3-t2-enq" data-key="' + key + '" data-mkt="' + it.market + '" title="autopilot priority enqueue - enters if it passes review (AI/conviction)">🤖 Enqueue</button></td></tr>';
    }).join('');
    return '<table class="v3-postable"><thead><tr><th>Market</th><th>Price</th><th>Change</th><th>Score</th><th>RSI</th><th>Sugg. Budget</th><th></th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  async function loadTier2Reco(key, force) {
    V3.state.tier2reco = V3.state.tier2reco || {};
    const cur = V3.state.tier2reco[key], now = Date.now();
    if (!force && cur && cur.items && cur.items.length && now - (cur.t || 0) < 600000) { const e0 = $('v3-' + key + '-reco'); if (e0) e0.innerHTML = tier2RecoHtml(key); return; }
    const d = await V3.getJSON('/api/strategy/recommendations?strategy=' + key.toUpperCase() + '&n=10');
    V3.state.tier2reco[key] = { items: (d && d.items) || [], computing: !!(d && d.computing), err: d && (d.detail || d.error), t: now };
    const e1 = $('v3-' + key + '-reco'); if (e1) e1.innerHTML = tier2RecoHtml(key);
    if (d && d.computing && V3.state.selected.has(key)) setTimeout(() => { if (V3.state.selected.has(key)) loadTier2Reco(key, true); }, 4000);   // auto-retry while computing
  }
  V3.loadTier2Reco = loadTier2Reco;
  async function loadTier2Tune() {
    const d = await V3.getJSON('/api/reserved/plugin-params');
    const p = (d && d.params) || {};
    TIER2.forEach((key) => {
      const kp = p[key.toUpperCase()] || {};
      (TIER2_TUNE[key] || []).forEach((f) => { const el = $('v3-tune-' + key + '-' + f[0]); if (el && document.activeElement !== el && kp[f[0]] != null) el.value = kp[f[0]]; });
    });
  }
  V3.loadTier2Tune = loadTier2Tune;

  // ════════════════════════════════════════════════════════════
  // ⚙️ Settings (Common) - Phase 4: 🔌 connection-status viewer + ✈️ Telegram (all existing /api/system/* endpoints, no secrets shown)
  // ════════════════════════════════════════════════════════════
  function settingsBlock() {
    return '<section class="v3-block"><div class="v3-block-head"><span class="v3-block-title">⚙️ Settings (Common)</span>' +
      '<span class="v3-pos-actions"><button class="v3-btn sm ghost" id="v3-set-refresh" title="Refresh">🔄</button></span></div>' +
      '<div class="v3-cset-grid">' +
      '<section class="v3-cset"><div class="v3-cset-h">🔌 Connection Status</div>' +
        '<div class="rib-row"><span>Exchange</span><b id="v3-set-exch">' + (new URLSearchParams(location.search).get('ex') === 'binance_futures' ? 'Binance Linear' : 'Bybit Linear') + '</b></div>' +
        '<div class="rib-row"><span>Mode</span><span id="v3-set-mode" class="v3-badge mute">…</span></div>' +
        '<div class="rib-row"><span>Exchange API</span><span id="v3-set-api">…</span></div>' +
        '<div class="rib-row"><span>WS / Price feed</span><span id="v3-set-ws">…</span></div>' +
        '<div class="rib-row"><span>Balance (equity)</span><b id="v3-set-equity">…</b></div>' +
        '<div class="rib-row"><span>≈ KRW</span><b id="v3-set-equity-krw" class="v3-pos">…</b></div>' +
        '<div class="rib-row"><span>USD/KRW rate <small>(auto every 30m, editable)</small></span><span style="display:flex;align-items:center;gap:6px"><label style="font-size:11px;white-space:nowrap"><input type="checkbox" id="v3-krw-auto" checked> 🔄 Auto</label><input class="v3-mini" type="number" step="1" min="0" id="v3-krw-rate" value="1380"></span></div>' +
        '<div class="rib-row"><span>Cash / Deployed</span><span id="v3-set-cash">…</span></div>' +
        '<div class="rib-row"><span>Tick</span><span id="v3-set-tick">…</span></div>' +
        '<div class="rib-row"><span>System</span><span id="v3-set-health">…</span></div>' +
        '<div class="rib-row"><span>E-STOP</span><span id="v3-set-estop">…</span></div>' +
        '<small class="hint" style="display:block;color:var(--v3-fg-mute);margin-top:6px;">Read-only. Secrets like API keys are not shown.</small></section>' +
      '<section class="v3-cset"><div class="v3-cset-h">✈️ Telegram</div>' +
        '<div class="rib-row"><span>Connection</span><span id="v3-tg-status">…</span></div>' +
        '<div class="rib-row"><span>New token <small>(input only, not shown)</small></span><input class="v3-mini" type="password" id="v3-tg-token" placeholder="bot token" autocomplete="off"></div>' +
        '<div class="rib-row"><span>chat id</span><input class="v3-mini" type="text" id="v3-tg-chat" placeholder="123456789" autocomplete="off"></div>' +
        '<div class="rib-row"><span>admin password <small>(to save)</small></span><input class="v3-mini" type="password" id="v3-tg-admin" placeholder="admin pw" autocomplete="off"></div>' +
        '<div style="display:flex;gap:8px;margin-top:8px"><button class="v3-btn sm ghost" id="v3-tg-test">✈️ Test</button><button class="v3-btn v3-btn-long" id="v3-tg-save">✓ Save</button></div>' +
        '<small class="hint" style="display:block;color:var(--v3-fg-mute);margin-top:6px;">The bot reads from <b>.env</b> (TELEGRAM_TOKEN/CHAT_ID). Connection = current .env value (masked). Test = sends immediately with the entered values (no save). Save = writes .env + applies immediately (<b>no restart needed</b>, admin auth).</small></section>' +
      '<section class="v3-cset"><div class="v3-cset-h">🔔 Alert Types (Telegram)</div>' +
        '<label class="rib-row"><span>LongHold alert</span><input type="checkbox" class="v3-cfg-chk" id="v3-alert-longhold"></label>' +
        '<label class="rib-row"><span>Drawdown alert</span><input type="checkbox" class="v3-cfg-chk" id="v3-alert-drawdown"></label>' +
        '<label class="rib-row"><span>Exit Profit Streak</span><input type="checkbox" class="v3-cfg-chk" id="v3-alert-exit_profit_streak"></label>' +
        '<label class="rib-row"><span>Daily report</span><input type="checkbox" class="v3-cfg-chk" id="v3-alert-daily"></label>' +
        '<label class="rib-row"><span>🔱 HARPOON trades</span><input type="checkbox" class="v3-cfg-chk" id="v3-alert-harpoon"></label>' +
        '<button class="v3-btn v3-btn-long" id="v3-alert-save">✓ Save Alerts</button>' +
        '<small class="hint" style="display:block;color:var(--v3-fg-mute);margin-top:6px;">Trade-fill alerts are ON by default for <b>all strategies</b> (FOCUS, LADDER, CONTRARIAN, SNIPER, HARPOON, etc.) - not FOCUS-only. The toggles above only enable/disable <b>extra alert types</b> (LongHold, Drawdown, profit streak, daily report, HARPOON summary). Applies immediately + saves to .env. Plugin <b>signal</b> (unfilled) alerts are controlled by OMA_TELEGRAM_SIGNAL_ENABLED (.env, default OFF). Triage alerts are under 🔌 Plugin Common ▸ Triage.</small></section>' +
      '<section class="v3-cset"><div class="v3-cset-h">🛠️ System Actions</div>' +
        '<div style="display:flex;flex-wrap:wrap;gap:6px;margin:4px 0">' +
          '<button class="v3-btn sm ghost" id="v3-sa-reconcile">🔄 Sync Balance</button>' +
          '<button class="v3-btn sm ghost" id="v3-sa-dust">🧹 Clear Dust</button>' +
          '<button class="v3-btn sm ghost" id="v3-sa-retrain">🧠 Retrain AI</button>' +
          '<button class="v3-btn sm ghost" id="v3-sa-dd-reset">🔧 Reset Drawdown</button>' +
        '</div>' +
        '<div class="hint" style="display:block;margin:6px 0;padding:6px 8px;border-left:2px solid var(--v3-warn,#c8860d);background:rgba(200,134,13,.06);color:var(--v3-fg-mute);font-size:11px;line-height:1.5">' +
          '<b>🔄 Reset / fresh start — run in this order:</b><br>' +
          '① <b>Close All</b> positions (Positions widget → ✕ Close All, or close each manually) — <i>any mode</i> (live closes real positions, paper closes virtual). Empties open positions the next steps can\'t.<br>' +
          '② <b>🕊️ Run amnesty</b> (FOCUS settings → General Amnesty) — <i>any mode</i>. Releases all pauses / holding pen / consecutive-loss / penalties / re-entry blocks.<br>' +
          '③ <b>🧹 Clean Slate</b> (below) — <b>paper only</b> (refuses in live to protect real records). Wipes all journals (backed up) + resets the paper balance.<br>' +
          'Then <b>Restart</b>.<br>' +
          '→ Full <b>①②③</b> = a clean paper restart. In <b>live</b>, use <b>①②</b> to flatten positions and clear all blocks while keeping your real trade records (skip ③).<br>' +
          '<b style="color:var(--v3-warn,#c8860d)">⚠️ In live, be very careful.</b> <b>Close All</b> flattens <i>every</i> position at once — <b>including underwater ones</b> — so it <b>locks in real losses</b>. To spare a position you would rather hold or wait out, do <b>not</b> use Close All: close coins <b>individually</b> (per-position ✕ / Exit) and leave the rest. Run amnesty also lifts every block at once — only do it when you really want a clean slate of pauses/penalties.' +
        '</div>' +
        '<div class="rib-row" style="gap:6px;align-items:center;flex-wrap:wrap"><span>Paper clean slate <small>(before going live)</small></span><button class="v3-btn sm v3-btn-outline-danger" id="v3-sa-clean-slate">🧹 Clean Slate</button></div>' +
        '<div class="rib-row" style="gap:6px;align-items:center"><span>Emergency Stop</span><span style="display:flex;gap:6px"><button class="v3-btn sm v3-btn-outline-danger" id="v3-sa-estop">🛑 Trigger</button><button class="v3-btn sm ghost" id="v3-sa-resume">▶️ Resume</button></span></div>' +
        '<div class="rib-row" style="gap:6px;align-items:center;flex-wrap:wrap"><span>Server <small>(run after cleanup)</small></span><span style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">' +
          '<label style="font-size:11px;white-space:nowrap"><input type="checkbox" id="v3-srv-cleanup" checked> Cleanup</label>' +
          '<select id="v3-srv-delay" class="v3-mini" style="width:auto"><option value="5">5s</option><option value="10">10s</option><option value="15" selected>15s</option><option value="30">30s</option><option value="60">60s</option></select>' +
          '<button class="v3-btn sm v3-btn-outline-danger" id="v3-sa-restart">🔁 Restart</button>' +
          '<button class="v3-btn sm v3-btn-outline-danger" id="v3-sa-stop">⏹️ Stop</button>' +
        '</span></div>' +
        '<small class="hint" style="display:block;color:var(--v3-fg-mute);margin-top:6px;">🔄 Sync = exchange balance ↔ OMA / 🧹 Dust = clear small leftovers / 🧠 Retrain = AI model (a few minutes). 🔧 Reset Drawdown = clear a fake CRISIS (-30) left by deposits/withdrawals or a long pause (applies next tick). Cleanup = wait to settle positions/orders before shutdown (5~60s). Server restart/stop needs run.ps1 (stop only if not configured). 🧹 Clean Slate (paper only) = close every position + wipe all journals (backed up first) + reset paper balance, for a fresh paper → live baseline — restart afterwards.</small></section>' +
      '<section class="v3-cset"><div class="v3-cset-h">👥 Peer Servers (Peer Brief)</div>' +
        '<label class="rib-row"><span>Enabled</span><input type="checkbox" id="v3-peer-enabled"></label>' +
        '<label class="rib-row"><span>Paper mode <small>(log only instead of reject)</small></span><input type="checkbox" id="v3-peer-paper"></label>' +
        '<div class="rib-row"><span>This server ID</span><input class="v3-mini" type="text" id="v3-peer-server-id" placeholder="ByBit_ServerB"></div>' +
        '<div class="rib-row"><span>Poll interval <small>(sec, 2~3600)</small></span><input class="v3-mini" type="number" min="2" max="3600" step="1" id="v3-peer-poll-sec"></div>' +
        '<div class="rib-row"><span>Caution window <small>(min, 1~1440 / how long a peer SL keeps blocking)</small></span><input class="v3-mini" type="number" min="1" max="1440" step="1" id="v3-peer-sl-min"></div>' +
        '<div class="rib-row"><span>Reference window <small>(min, 1~1440 / how long a peer TP/BE bonus lasts)</small></span><input class="v3-mini" type="number" min="1" max="1440" step="1" id="v3-peer-win-min"></div>' +
        '<div class="rib-row"><span>Bonus strength <small>(pts, 0~50 / conviction +N on a peer win)</small></span><input class="v3-mini" type="number" min="0" max="50" step="1" id="v3-peer-win-bonus"></div>' +
        '<div class="rib-row"><span>SL penalty <small>(pts, 0~50 / conviction −N on a recent peer SL same direction)</small></span><input class="v3-mini" type="number" min="0" max="50" step="1" id="v3-peer-sl-pen"></div>' +
        '<div class="rib-row"><span>Struggle penalty <small>(pts, 0~50 / −N when a peer holds a struggling same-direction position)</small></span><input class="v3-mini" type="number" min="0" max="50" step="1" id="v3-peer-struggle-pen"></div>' +
        '<div class="rib-row"><span>Struggle: held <small>(min, 1~120 / held longer than this & unresolved = struggle candidate)</small></span><input class="v3-mini" type="number" min="1" max="120" step="1" id="v3-peer-struggle-age"></div>' +
        '<div class="rib-row"><span>Struggle: peak profit <small>(%, struggling if it never rises above this)</small></span><input class="v3-mini" type="number" min="0" max="5" step="0.1" id="v3-peer-struggle-peak"></div>' +
        '<div class="rib-row"><span>Self-conflict penalty <small>(pts, 0~50 / −N when a peer holds a healthy opposite-direction position · passes if the peer is struggling = catch the turn)</small></span><input class="v3-mini" type="number" min="0" max="50" step="1" id="v3-peer-conflict-pen"></div>' +
        '<div class="rib-row"><span>🌊 Crowding penalty <small>(pts, 0~50 / −N per peer holding the same direction · soft · default 0=OFF)</small></span><input class="v3-mini" type="number" min="0" max="50" step="1" id="v3-peer-crowding-pen"></div>' +
        '<div class="rib-row"><span>Crowding penalty cap <small>(pts, 1~50 / never deducts more than this even with many servers · default 12)</small></span><input class="v3-mini" type="number" min="1" max="50" step="1" id="v3-peer-crowding-cap"></div>' +
        '<label class="rib-row"><span>🛡️ Fleet dir_fail <small>(peers+me, N cumulative losses on the same coin/direction → hard block · default OFF)</small></span><input type="checkbox" id="v3-peer-fleet-dirfail-en"></label>' +
        '<div class="rib-row"><span>Fleet block at N <small>(1~10, peers+me combined · default 2 = blocks from the 2nd)</small></span><input class="v3-mini" type="number" min="1" max="10" step="1" id="v3-peer-fleet-dirfail-max"></div>' +
        '<div class="rib-row"><span>Fleet window <small>(min, 1~1440 · default 240=4h, covers time-staggered losses)</small></span><input class="v3-mini" type="number" min="1" max="1440" step="10" id="v3-peer-fleet-dirfail-win"></div>' +
        '<div class="rib-row" style="margin-top:4px"><span>Peer server URLs <small>(one per line)</small></span></div>' +
        '<textarea id="v3-peer-urls" rows="3" cols="20" style="width:100%;font-family:monospace;font-size:11px;padding:4px;background:var(--v3-bg);color:var(--v3-fg);border:1px solid var(--v3-bd);border-radius:4px;box-sizing:border-box;resize:vertical" placeholder="http://server-a:8010&#10;http://server-b:8010&#10;http://server-office:8010"></textarea>' +
        '<button type="button" class="v3-btn v3-btn-long" id="v3-peer-save" style="margin-top:6px">✓ Save + Apply Now</button>' +
        '<div class="rib-row" style="margin-top:8px"><span>Current polling status</span></div>' +
        '<div id="v3-peer-status" style="font-family:monospace;font-size:10px;background:var(--v3-bg);padding:6px;border-radius:4px;border:1px solid var(--v3-bd);min-height:24px;word-break:break-all">…</div>' +
        '<small class="hint" style="display:block;color:var(--v3-fg-mute);margin-top:6px;">5-18 operator insight - a peer SL/holding on the same coin+direction in the last N minutes = block; TP/BE/profitable exit = conviction +N bonus (core guards unchanged). All servers can use the same URL pool (self is auto-skipped). Save = persist + restart polling. token is in .env (PEER_BRIEF_TOKEN).</small></section>' +
      '</div></section>';
  }
  async function loadSettings() {
    const [st, hl, tg, al] = await Promise.all([V3.getJSON('/api/system/status'), V3.getJSON('/api/system/health'), V3.getJSON('/api/system/telegram/status'), V3.getJSON('/api/system/alerts')]);
    const setT = (id, html) => { const el = $(id); if (el) el.innerHTML = html; };
    const mode = (st && st.trading_mode) || '?';
    setT('v3-set-mode', '<span class="v3-badge ' + (mode === 'LIVE' ? 'short' : 'mute') + '">' + mode + (mode === 'LIVE' ? ' 🔴' : '') + '</span>');
    const eq = (st && st.equity) || {}, perf = (st && st.performance) || {};
    setT('v3-set-equity', '$' + Number(eq.equity_usdt || 0).toFixed(2));
    if (V3.fetchKrwRate) V3.fetchKrwRate();   // 💱 auto FX rate (30-min cache)
    const _ac = $('v3-krw-auto'); if (_ac) _ac.checked = localStorage.getItem('v3_krw_auto') !== '0';
    const _sr = $('v3-krw-rate'), _saved = localStorage.getItem('v3_krw_rate');
    if (_sr && _saved && document.activeElement !== _sr) _sr.value = _saved;   // restore remembered FX rate
    const _rate = parseFloat((_sr && _sr.value) || _saved || '1380') || 1380;
    setT('v3-set-equity-krw', '≈ ₩' + Math.round(Number(eq.equity_usdt || 0) * _rate).toLocaleString());
    setT('v3-set-cash', '$' + Number(eq.cash_usdt || 0).toFixed(0) + ' / Deployed $' + Number(eq.deployed_usdt || 0).toFixed(0));
    setT('v3-set-tick', (perf.tick_count != null ? perf.tick_count + ' tick' : '-') + (perf.tick_duration != null ? ' · ' + Math.round(Number(perf.tick_duration) * 1000) + 'ms' : ''));
    setT('v3-set-estop', (st && st.emergency_stop) ? '<span class="v3-badge short">🛑 STOP</span>' : '<span class="v3-pos">Normal</span>');
    const ch = (hl && hl.checks) || {};
    const apiOk = ch.exchange_api === 'ok';
    setT('v3-set-api', '<span class="' + (apiOk ? 'v3-pos' : 'v3-neg') + '">' + (apiOk ? 'Connected ✓' : (ch.exchange_api || '?')) + '</span>');
    const pfAge = ch.price_feed && ch.price_feed.age_sec;
    setT('v3-set-ws', '<span class="' + (ch.websocket === 'ok' ? 'v3-pos' : 'v3-neg') + '">' + (ch.websocket || '?') + '</span>' + (pfAge != null ? ' <small class="text-muted">price ' + Math.round(pfAge) + 's ago</small>' : ''));
    const hs = (hl && hl.status) || '?';
    setT('v3-set-health', '<span class="v3-badge ' + (hs === 'healthy' ? 'long' : (hs === 'critical' ? 'short' : 'warn')) + '">' + hs + '</span>');
    setT('v3-tg-status', (tg && tg.has_config) ? ('<span class="v3-pos">Connected ✓</span> <small class="text-muted">' + (tg.token_masked || '') + ' · chat ' + (tg.chat_id || '') + '</small>') : '<span class="v3-neg">Not set</span>');
    const av = (al && al.alerts) || {};   // 🔔 fill alert-type toggles (skip if being edited)
    ['longhold', 'drawdown', 'exit_profit_streak', 'daily', 'harpoon'].forEach((k) => { const el = $('v3-alert-' + k); if (el && document.activeElement !== el && av[k] != null) el.checked = !!av[k]; });
    loadPeerSettings();   // 👥 also refresh the Peer Servers (Peer Brief) panel
  }
  V3.loadSettings = loadSettings;

  // ════════════════════════════════════════════════════════════
  // 👥 Peer Brief — peer-server guard (section inside the Settings panel)
  // ════════════════════════════════════════════════════════════
  async function loadPeerSettings() {
    const s = await V3.getJSON('/peer/settings');
    if (!s || s.ok === false) {
      const st = $('v3-peer-status'); if (st) st.textContent = 'No response from /peer/settings';
      return;
    }
    const setV = (id, v) => { const el = $(id); if (el && document.activeElement !== el) el.value = (v != null ? v : ''); };
    const setC = (id, v) => { const el = $(id); if (el && document.activeElement !== el) el.checked = !!v; };
    setC('v3-peer-enabled', s.enabled);
    setC('v3-peer-paper', s.paper);
    setV('v3-peer-server-id', s.server_id);
    setV('v3-peer-urls', (s.urls || []).join('\n'));
    setV('v3-peer-poll-sec', Math.round(s.poll_interval_sec || 20));
    setV('v3-peer-sl-min', s.sl_window_min || 30);
    setV('v3-peer-win-min', s.peer_win_window_min || 15);
    setV('v3-peer-win-bonus', s.peer_win_bonus != null ? s.peer_win_bonus : 5);
    setV('v3-peer-sl-pen', s.peer_sl_penalty != null ? s.peer_sl_penalty : 8);
    setV('v3-peer-struggle-pen', s.peer_struggle_penalty != null ? s.peer_struggle_penalty : 6);
    setV('v3-peer-struggle-age', s.peer_struggle_age_min != null ? s.peer_struggle_age_min : 5);
    setV('v3-peer-struggle-peak', s.peer_struggle_peak_pct != null ? s.peer_struggle_peak_pct : 0.3);
    setV('v3-peer-conflict-pen', s.peer_conflict_penalty != null ? s.peer_conflict_penalty : 8);
    setV('v3-peer-crowding-pen', s.peer_crowding_penalty != null ? s.peer_crowding_penalty : 0);
    setV('v3-peer-crowding-cap', s.peer_crowding_cap != null ? s.peer_crowding_cap : 12);
    setC('v3-peer-fleet-dirfail-en', s.fleet_dirfail_enabled);
    setV('v3-peer-fleet-dirfail-max', s.fleet_dirfail_max != null ? s.fleet_dirfail_max : 2);
    setV('v3-peer-fleet-dirfail-win', s.fleet_dirfail_window_min != null ? s.fleet_dirfail_window_min : 240);
    // Status line
    const cache = s.cache || {};
    const peers = cache.peers || [];
    const lines = [];
    lines.push('Me: <b>' + (s.server_id || '?') + '</b> · enabled=' + (s.enabled ? '✓' : '✗') + ' · paper=' + (s.paper ? '✓' : '✗') + ' · SL win ' + (s.sl_window_min || 30) + 'm · WIN win ' + (s.peer_win_window_min || 15) + 'm/+' + (s.peer_win_bonus || 0) + 'pt · token ' + (s.token_set ? '✓' : '✗'));
    if (peers.length === 0) {
      lines.push('<span style="color:var(--v3-fg-mute)">No peer URLs (standalone mode)</span>');
    } else {
      peers.forEach((p) => {
        const sid = p.server_id || p.url;
        const fresh = p.ok_age_sec >= 0 && p.ok_age_sec < (s.poll_interval_sec || 20) * 4;
        const tag = p.stale ? '<span style="color:var(--v3-warn)">⚠ stale</span>' : (fresh ? '<span style="color:var(--v3-pos)">✓ ' + p.ok_age_sec + 's</span>' : '<span style="color:var(--v3-fg-mute)">waiting</span>');
        lines.push(sid + ' — ' + tag + ' · SL=' + (p.recent_losses || 0) + ' · WIN=' + (p.recent_wins || 0) + ' · pos=' + (p.active_positions || 0));
      });
    }
    const st = $('v3-peer-status'); if (st) st.innerHTML = lines.join('<br>');
  }
  V3.loadPeerSettings = loadPeerSettings;

  // 💱 USD/KRW rate auto-fetch (30-min cache · open.er-api.com free·CORS allowed). On failure, keep the manual value. v3_krw_auto='0' = manual.
  V3.krwRate = function () { return parseFloat(localStorage.getItem('v3_krw_rate') || '1380') || 1380; };
  async function fetchKrwRate(force) {
    if (localStorage.getItem('v3_krw_auto') === '0') return;   // manual mode = no auto-refresh
    const now = Date.now(), lastT = parseInt(localStorage.getItem('v3_krw_rate_t') || '0', 10) || 0;
    if (!force && now - lastT < 1800000) return;   // 30-min cache
    try {
      const r = await fetch('https://open.er-api.com/v6/latest/USD', { cache: 'no-store' });
      const d = await r.json();
      const krw = d && d.rates && Number(d.rates.KRW);
      if (krw && krw > 500 && krw < 3000) {   // sanity (normal KRW/USD range)
        localStorage.setItem('v3_krw_rate', String(Math.round(krw)));
        localStorage.setItem('v3_krw_rate_t', String(now));
        const inp = $('v3-krw-rate'); if (inp && document.activeElement !== inp) inp.value = Math.round(krw);
        if (V3.state.envView && V3.loadSettings) V3.loadSettings();   // on the settings view, recompute ≈KRW
        const he = $('v3-home-equity'); if (he && V3.state.homeView && V3.state.home.pos) he.innerHTML = homeEquityHtml(V3.state.home.pos.sys);
      }
    } catch (e) { /* offline/CORS failure → keep manual value */ }
  }
  V3.fetchKrwRate = fetchKrwRate;

  // ── 🏠 Home = overall status (positions·candidates, plus quick trade·kimchi premium·FX later) ──
  // ⏰ Event Shield countdown (header, right of Tick·API·🧭, owner 2026-06-08) — ticks every second
  function _esFmt(ms) {
    if (ms <= 0) return '0s';
    const s = Math.floor(ms / 1000), d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60), ss = s % 60;
    if (d > 0) return d + 'd ' + h + 'h';
    if (h > 0) return h + ':' + String(m).padStart(2, '0') + ':' + String(ss).padStart(2, '0');
    return m + ':' + String(ss).padStart(2, '0');
  }
  V3.evShieldInner = function (es) {
    if (!es || !es.enabled || !(es.events && es.events.length)) return '';
    const now = Date.now();
    const winMs = (es.window_min || 0) * 60000, leadMs = (es.lead_min || 0) * 60000;
    let evTs = null, evLab = null;
    for (const lab of es.events) {
      const t = Date.parse(String(lab).replace(' ', 'T') + ':00+09:00');   // explicit KST → correct regardless of timezone
      if (isNaN(t)) continue;
      if (t + winMs >= now) { evTs = t; evLab = lab; break; }   // earliest event whose post-window hasn't passed
    }
    if (evTs == null) return '';
    // ★ [2026-06-25] show WHAT the news is (event name from ForexFactory), not just the countdown
    const evName = (es.events_detail && evLab && es.events_detail[evLab]) ? String(es.events_detail[evLab]) : '';
    const evShort = evName.length > 32 ? evName.slice(0, 30) + '…' : evName;   // keep the header tidy; full name in tooltip
    const startMs = evTs - winMs - leadMs, endMs = evTs + winMs;   // pre = window+lead (slippage lead)
    if (now >= startMs && now <= endMs) {   // SHIELD ON
      return '<span title="Event Shield active — blocks new entries·tightens SL' + (evName ? ' · ' + evName : '') + '" style="color:#dc3545;font-weight:600">⏰ SHIELD ON' + (evShort ? ' · ' + evShort : '') + ' · -' + _esFmt(endMs - now) + '</span>';
    }
    const toStart = startMs - now;
    const col = toStart < 3600000 ? '#ffc107' : 'var(--v3-fg-mute)';   // within 1h of block start = yellow
    return '<span title="Next economic event' + (evName ? ': ' + evName : '') + ' — blocking starts ' + ((es.window_min || 0) + (es.lead_min || 0)) + 'min before (ahead of the crowd)" style="color:' + col + '">⏰ Event' + (evShort ? ' · ' + evShort : '') + ' ' + _esFmt(evTs - now) + '</span>';
  };
  try {
    setInterval(function () {
      const inner = V3.evShieldInner((V3.lastStatus && V3.lastStatus.event_shield) || null);
      document.querySelectorAll('.v3-evshield').forEach(function (el) { el.innerHTML = inner; });
    }, 1000);
  } catch (e) { /* noop */ }

  // v2-Overview-style thin status line (terms kept in English, per memory) — Engine·Active·Ready·Total·Free·Avail·PnL·Tick·API
  function homeStatusHtml(d) {
    const s = (d && d.system) || {}, eq = s.equity || {}, perf = s.performance || {}, api = s.api_stats || {};
    const oma = s.oma || {}, active = (oma.active || []).length, ready = (oma.watch || []).length;
    const total = Number(eq.equity_usdt || 0), free = Number(eq.cash_usdt || 0), avail = free * Number(eq.deploy_ratio || 1);
    const spnl = Number(s.session_pnl || 0), base = Number(s.pnl_baseline || 0);
    const tickN = perf.tick_count != null ? perf.tick_count : '-', tickMs = perf.tick_duration != null ? (Number(perf.tick_duration) * 1000).toFixed(1) : '-';
    const apiN = api.calls_per_min != null ? api.calls_per_min : '-', estop = s.emergency_stop, mode = s.trading_mode || '?';
    const tMs = Number(perf.tick_duration || 0) * 1000, alive = perf.tick_count != null;   // alive = engine tick exists (×1000: sec→ms)
    const tCol = !alive ? '#8a8f99' : tMs > 1000 ? '#dc3545' : tMs > 500 ? '#ffc107' : '#28a745';   // gray=stopped / >1000 red / >500 yellow / else green
    const dot = (col, blink) => '<span class="' + (blink ? 'v3-blink' : '') + '" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' + col + ';box-shadow:0 0 6px 1px ' + col + '88;margin-right:5px;vertical-align:middle"></span>';
    const tickDot = '<span class="' + (alive ? 'v3-tick-beat' : '') + '" style="display:inline-block;width:12px;height:12px;border-radius:50%;background:' + tCol + ';box-shadow:0 0 9px 2px ' + tCol + '99;margin-right:6px;vertical-align:middle"></span>';
    const sep = '<span style="color:var(--v3-fg-mute);margin:0 9px">·</span>';
    const mut = (k) => '<span style="color:var(--v3-fg-mute)">' + k + '</span> ';
    const _mc = (V3.lastStatus && V3.lastStatus.macro_compass) || null;
    const _mcCol = _mc ? ({ RISK_OFF: '#dc3545', RECOVERING: '#ffc107', RISK_ON: '#28a745', NEUTRAL: 'var(--v3-fg-mute)' }[_mc.state] || 'var(--v3-fg-mute)') : '';
    const _mcHtml = _mc ? (sep + '<span title="Macro regime compass — top-10 crash/recovery shift (display only · no entry impact)" style="color:' + _mcCol + '">🧭 ' + _mc.label + '</span>') : '';
    const _esInner = V3.evShieldInner ? V3.evShieldInner((V3.lastStatus && V3.lastStatus.event_shield) || null) : '';
    const _evHtml = _esInner ? (sep + '<span class="v3-evshield">' + _esInner + '</span>') : '';
    return '<div style="font-size:12.5px;padding:4px 0 6px;line-height:2.1">' +
      dot(estop ? '#dc3545' : '#28a745', true) + (estop ? 'Stopped' : 'Running') + ' ' + (s.engine || 'NUNNAYA') + sep +
      '<span class="v3-badge ' + (mode === 'LIVE' ? 'short' : 'mute') + '">' + mode + '</span>' + sep +
      mut('Active') + active + sep + mut('Ready') + ready + sep +
      mut('Total') + total.toFixed(0) + ' USDT' + sep + mut('Free') + free.toFixed(0) + ' USDT' + sep + mut('Avail') + avail.toFixed(0) + ' USDT' + sep +
      mut('PnL') + '<span class="' + V3.pnlCls(spnl) + '">' + (spnl >= 0 ? '+' : '') + spnl.toFixed(2) + ' USDT</span> <span style="color:var(--v3-fg-mute)">(baseline ' + base.toFixed(0) + ' USDT)</span>' + sep +
      tickDot + mut('Tick') + '<span style="color:' + tCol + ';font-size:13.5px">' + tickN + '·' + tickMs + 'ms</span>' + sep + mut('API') + apiN + _mcHtml + _evHtml +
      (estop ? sep + '<span style="color:var(--v3-neg)">🛑 E-STOP</span>' : '') + '</div>';
  }
  function homeCard(label, value, sub, valCls) {
    return '<div style="flex:1;min-width:130px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);border-radius:8px;padding:9px 14px">' +
      '<div style="font-size:10px;color:var(--v3-fg-mute);text-transform:uppercase;letter-spacing:.5px">' + label + '</div>' +
      '<div style="font-size:21px;margin-top:3px" class="' + (valCls || '') + '">' + value + '</div>' +
      (sub ? '<div style="font-size:11px;color:var(--v3-fg-mute);margin-top:1px">' + sub + '</div>' : '') + '</div>';
  }
  function homeCardsHtml(d) {   // v2-style 4 big-number cards (Total Equity·Session PnL·Active·Reserved)
    const s = (d && d.system) || {}, eq = s.equity || {};
    const total = Number(eq.equity_usdt || 0), spnl = Number(s.session_pnl || 0);
    const active = ((s.oma || {}).active || []).length;
    const reco = (V3.state.home.reco && V3.state.home.reco.items) ? V3.state.home.reco.items.length : null;
    return '<div style="display:flex;flex-wrap:wrap;gap:10px;margin:8px 0 4px">' +
      homeCard('Total Equity', total.toFixed(2) + ' USDT', '≈ ₩' + Math.round(total * V3.krwRate()).toLocaleString()) +
      homeCard('Session PnL', (spnl >= 0 ? '+' : '') + spnl.toFixed(2) + ' USDT', 'baseline ' + Number(s.pnl_baseline || 0).toFixed(0) + ' USDT <button class="v3-btn ghost" id="v3-home-baseline" style="padding:0 6px;font-size:10px;margin-left:4px;vertical-align:middle" title="Reset PnL baseline to current equity">💾 Reset</button>', V3.pnlCls(spnl)) +
      homeCard('Active Markets', String(active)) +
      homeCard('Reserved Candidates', reco != null ? String(reco) : '…') +
      '</div>';
  }
  function homeEquityHtml(d) { return homeStatusHtml(d) + homeCardsHtml(d); }   // wrapper for caller compatibility
  function homePosHtml(d) {
    const fpos = (d && d.focus && d.focus.positions) || [];
    const sys = (d && d.sys && d.sys.system) || {}, oma = sys.oma || {}, prices = sys.active_prices || {};
    const omaAll = [].concat(oma.active || [], oma.recovery || []);
    const fmkts = new Set(fpos.map((p) => p.market));
    const rows = [];
    fpos.forEach((p) => rows.push('<tr>' + posCells(p, 'FOCUS') + '</tr>'));   // FOCUS = full fields (shares posCells → same layout as the FOCUS table)
    omaAll.filter((m) => m.market && !fmkts.has(m.market)).forEach((m) => {     // markets managed by other strategies = slot info only (margin=budget), rest —
      const pr = prices[m.market], mShort = (m.market || '').replace('USDT', '');
      rows.push('<tr style="opacity:.7">' +
        '<td><b class="v3-mkt" data-bybit="' + m.market + '">' + mShort + '</b> <small style="color:var(--v3-fg-mute)">' + (m.strategy || '?') + '</small></td>' +
        '<td><small class="text-muted">Managed</small></td>' +
        '<td><small class="text-muted">' + Number(m.budget_usdt || 0).toFixed(0) + ' USDT</small></td>' +
        '<td>—</td><td>' + (pr ? _fp(pr) : '—') + '</td>' +
        '<td><small class="text-muted">—</small></td><td>—</td><td>—</td>' +
        '<td><small class="text-muted">—</small></td><td>—</td></tr>');
    });
    if (!rows.length) return '<div class="v3-placeholder">No positions held · no managed markets</div>';
    return '<table class="v3-postable v3-postable-pos"><thead><tr><th>Market</th><th>Dir</th><th>Margin</th><th>Entry</th><th>Current</th><th>PnL</th><th>TP1</th><th>SL</th><th>Progress</th><th>Hold</th></tr></thead><tbody>' + rows.join('') + '</tbody></table>';
  }
  function homeRecoHtml(r) {
    const items = (r && r.items) || [];
    if (!items.length) return '<div class="v3-placeholder">No candidates — ' + ((r && (r.detail || r.error)) || 'snapshot empty (refreshes on the 07:00 KST baseline)') + '</div>';
    const rows = items.map((it) => {
      const score = Number(it.ai_adjusted_score != null ? it.ai_adjusted_score : (it.rank_score != null ? it.rank_score : (it.score || 0)));
      const chg = Number(it.change_rate != null ? it.change_rate : (it.change_pct || 0));
      const bud = Number(it.suggested_budget_usdt || 0);
      return '<tr><td><b class="v3-mkt" data-bybit="' + it.market + '">' + (it.market || '').replace('USDT', '') + '</b></td>' +
        '<td>' + ('' + (it.strategy || it.active_strategy || '-')).toUpperCase() + '</td>' +
        '<td>' + (score ? score.toFixed(0) : '-') + '</td>' +
        '<td>' + (it.rsi != null ? Number(it.rsi).toFixed(0) : '-') + '</td>' +
        '<td class="' + (chg >= 0 ? 'v3-pos' : 'v3-neg') + '">' + (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%</td>' +
        '<td>' + (bud ? bud.toFixed(0) + ' USDT' : '-') + '</td></tr>';
    }).join('');
    return '<table class="v3-postable"><thead><tr><th>Market</th><th>Strategy</th><th>Score</th><th>RSI</th><th>Change</th><th>Sugg. Budget</th></tr></thead><tbody>' + rows + '</tbody></table>' +
      '<small class="hint">' + (r.cached ? 'cached' : 'refreshed') + ' · baseline ' + (r.basis_kst || '07:00') + ' KST · ' + (r.created_at_kst || '') + ' — click a coin = Bybit · deploy from each strategy entry tab</small>';
  }
  function homeQuickHtml() {   // ⚡ instant market-order quick trade (POST /api/trade/quick) — live trade!
    return '<div class="v3-ltg-form" style="margin:6px 0"><div class="v3-ltg-form-row">' +
      '<div class="fld"><label class="v3-label">Market</label><input id="v3-qt-market" class="v3-input" type="text" placeholder="BTCUSDT" list="v3-market-list" autocomplete="off"></div>' +
      '<div class="fld"><label class="v3-label">Amount basis</label><select id="v3-qt-mode" class="v3-input"><option value="quote">USDT</option><option value="percent">Percent (%)</option></select></div>' +
      '<div class="fld"><label class="v3-label">Value</label><input id="v3-qt-amount" class="v3-input" type="number" value="10" min="0" step="1"></div>' +
      '<div class="fld"><label class="v3-label">Guard</label><select id="v3-qt-guard" class="v3-input"><option value="global">Apply guards</option><option value="entry_limit_only">Entry limit only</option><option value="force">Force (ignore)</option></select></div>' +
      '<button class="v3-btn v3-btn-long v3-qt-side" data-side="buy">🟢 Buy</button>' +
      '<button class="v3-btn v3-btn-outline-danger v3-qt-side" data-side="sell">🔴 Sell</button>' +
      '</div><small class="hint">⚡ Instant market order (live trade!). Amount basis USDT=absolute amount / Percent=% of available cash. Apply guards=global entry guards / Force=ignore.</small></div>';
  }
  function homeBlock() {
    const sh = V3.state.home.show || {};
    const h3 = (t, sub) => '<h3 style="font-size:12px;font-weight:normal;color:var(--v3-fg-mute);margin:14px 0 3px">' + t + ' <small>' + sub + '</small></h3>';
    let h = '<section class="v3-block" id="v3-home-block">';
    h += '<div class="v3-block-head" style="display:flex;align-items:center;justify-content:space-between"><h2 style="margin:0;font-size:15px;font-weight:normal">🏠 Overall Status</h2><button class="v3-btn sm ghost" id="v3-home-refresh">↻ Refresh</button></div>';
    if (sh.status !== false) h += '<div id="v3-home-equity">' + (V3.state.home.pos ? homeEquityHtml(V3.state.home.pos.sys) : '') + '</div>';
    if (sh.quick !== false) { h += h3('⚡ Quick Trade', '(instant market · live trade)'); h += homeQuickHtml(); }
    if (sh.positions !== false) { h += h3('📊 Current Positions', '(FOCUS detail + markets managed by other strategies)'); h += '<div id="v3-home-pos">' + (V3.state.home.pos ? homePosHtml(V3.state.home.pos) : '<div class="v3-placeholder">Loading…</div>') + '</div>'; }
    if (sh.reco !== false) { h += h3('🎯 Recommended Candidates by Strategy', '(combined snapshot of all strategies)'); h += '<div id="v3-home-reco">' + (V3.state.home.reco ? homeRecoHtml(V3.state.home.reco) : '<div class="v3-placeholder">Loading…</div>') + '</div>'; }
    if (sh.status === false && sh.quick === false && sh.positions === false && sh.reco === false) h += '<div class="v3-placeholder">Select items to show from Widgets on the right</div>';
    return h + '</section>';
  }
  async function loadHome(force) {
    const hm = V3.state.home, now = Date.now();
    if (hm.loading && !force) return;
    hm.loading = true;
    try {
    fetchKrwRate(force);   // 💱 auto FX rate (30-min cache) — non-blocking
    const sys = await V3.getJSON('/api/system/status', { timeoutMs: 5000 });
    hm.pos = { focus: V3.lastStatus, sys: sys };
    const pe = $('v3-home-pos'); if (pe) pe.innerHTML = homePosHtml(hm.pos);
    const eqEl = $('v3-home-equity'); if (eqEl) eqEl.innerHTML = homeEquityHtml(sys);
    if (force || !hm.reco || now - hm.t > 300000) {   // recommendation snapshot is cached for 5 min (not fetched every poll)
      const r = await V3.getJSON('/api/recommend/snapshot?n=12', { timeoutMs: 8000 });
      hm.reco = r; hm.t = now;
      const re = $('v3-home-reco'); if (re) re.innerHTML = homeRecoHtml(r);
    }
    } finally {
      hm.loading = false;
    }
  }
  V3.loadHome = loadHome;

  // Right-side toggled widget row (strategy view + 🏠 Overall Status shared — 2026-06-07 owner)
  function buildWidgetsRow(homeOnly) {
    const w = V3.state.widgets || {};
    let ord = (V3.state.widgetOrder && V3.state.widgetOrder.length) ? V3.state.widgetOrder.slice() : Object.keys(_wreg);
    Object.keys(_wreg).forEach((k) => { if (ord.indexOf(k) < 0) ord.push(k); });   // prevent missing new widgets
    if (homeOnly) ord = ['peer', 'journal'].filter((k) => ord.indexOf(k) >= 0);   // Overall Status default = block monitor (Peer) first
    let wrow = '';
    for (const key of ord) {
      if (!_wreg[key] || !w[key]) continue;
      if (key === 'journal') { wrow += '<div class="v3-widget v3-widget-wide" id="v3-wg-journal">' + renderJournal() + '</div>'; continue; }
      if (key === 'manual') { wrow += '<div class="v3-widget" id="v3-wg-manual">' + renderManual() + '</div>'; continue; }
      const cfg = _wreg[key], c = _wcache[key];
      wrow += '<div class="v3-widget' + (cfg.wide ? ' v3-widget-wide' : '') + '" id="' + cfg.el + '">' + (c ? cfg.render(c.data) : '<div class="v3-placeholder">' + cfg.loading + '</div>') + '</div>';
    }
    return wrow ? '<div class="v3-widgets-row">' + wrow + '</div>' : '';
  }

  // Main = stack of selected strategy blocks (re-render while preserving checkbox state)
  function renderMain() {
    const el = $('v3-trade'); if (!el) return;
    if (V3.state.homeView) { el.innerHTML = homeBlock() + buildWidgetsRow(true); loadHome(); if (V3.loadEnabledWidgets) V3.loadEnabledWidgets(); return; }   // 🏠 Overall Status = show only FOCUS results (Journal·Peer) (2026-06-07 owner)
    if (V3.state.pcommonView) { el.innerHTML = pluginsCommonBlock(); return; }
    if (V3.state.envView) { el.innerHTML = settingsBlock(); loadSettings(); return; }
    // ★ [2026-06-02 owner] layer order = body table position — order (user sort) first + selected filter + safeguard (append selected not in order)
    const sel = V3.state.order.filter((n) => V3.state.selected.has(n));
    V3.state.selected.forEach((n) => { if (!sel.includes(n)) sel.push(n); });
    if (!sel.length) { el.innerHTML = '<div class="v3-placeholder">Select a strategy with the switches on the left (multi-select = shown together in main)</div>'; return; }
    const checked = new Set(Array.from(el.querySelectorAll('.focus-pos-chk:checked')).map((c) => c.dataset.market));
    const _meMkt = ($('v3-me-market') || {}).value, _meTo = ($('v3-me-timeout') || {}).value;   // 🖐 preserve manual-entry inputs (5s re-render)
    const _meFocus = document.activeElement && document.activeElement.id === 'v3-me-market';
    // ★ [2026-06-02 owner] status bar atop the strategy view — equity/PnL/Tick "whoosh" (excluded on home since it already has one)
    const _sbar = V3.lastSys ? ('<div class="v3-strat-statusbar">' + homeStatusHtml(V3.lastSys) + '</div>') : '';
    el.innerHTML = _sbar + sel.map((n, _i) => {
      const _blk = n === 'focus' ? focusBlock(V3.lastStatus) : n === 'harpoon' ? harpoonBlock(V3.state.harpoon) : n === 'lightning' ? lightningBlock({ items: V3.state.lightning.items }) : n === 'sniper' ? sniperBlock({ items: V3.state.sniper.items }) : PLUG[n] ? plugBlock(n, { items: plugState(n).items }) : n === 'ladder' ? ladderBlock({ items: V3.state.ladder.items }) : TIER2.includes(n) ? tier2WorkBlock(n) : stubBlock(n);
      const _mv = sel.length > 1 ? ('<div class="v3-layer-mv"><button class="v3-lmv" data-layer="' + n + '" data-dir="-1"' + (_i === 0 ? ' disabled' : '') + ' title="Up">▲</button><button class="v3-lmv" data-layer="' + n + '" data-dir="1"' + (_i === sel.length - 1 ? ' disabled' : '') + ' title="Down">▼</button></div>') : '';
      return '<div class="v3-layer-wrap" data-layer="' + n + '">' + _mv + _blk + '</div>';
    }).join('');
    if (checked.size) el.querySelectorAll('.focus-pos-chk').forEach((c) => { if (checked.has(c.dataset.market)) c.checked = true; });
    if (_meMkt != null) { const i = $('v3-me-market'); if (i) { i.value = _meMkt; if (_meFocus) i.focus(); } }
    if (_meTo != null) { const i = $('v3-me-timeout'); if (i) i.value = _meTo; }
    const eb = $('focus-btn-exit-selected');
    if (eb) eb.disabled = el.querySelectorAll('.focus-pos-chk:checked').length === 0;
    if (V3.state.selected.has('focus')) { loadEnabledWidgets(); if (V3.state.widgets.journal) drawDailyChart(); }
    if (V3.state.selected.has('harpoon')) loadHarpoon();
    if (V3.state.selected.has('lightning')) loadLightning();
    if (V3.state.selected.has('sniper')) loadSniper();
    sel.forEach((n) => { if (PLUG[n]) loadPlug(n); });   // refresh active list for generic plugins (GAZUA etc.)
    if (V3.state.selected.has('ladder')) loadLadder();   // 📐 read-only
    if (sel.some((n) => TIER2.includes(n))) { loadTier2Work(); sel.forEach((n) => { if (TIER2.includes(n)) loadTier2Reco(n); }); }   // 🤖 main = work status + recommended coins (settings ribbon injected by applyActivePanels)
  }
  V3.renderMain = renderMain;

  // ── Status polling (fallback) + WS triggers immediately ──
  async function pollStatus() {
    if (V3._pollingStatus) return;
    V3._pollingStatus = true;
    try {
    const d = await V3.getJSON('/api/strategy/focus/status', { timeoutMs: 4500 });
    // ★ [2026-06-19 owner "whole screen goes dark every 10s"] update only on success (ok) — getJSON returns
    //   {ok:false} on timeout/failure; overwriting the last good status with it makes renderMain blank the
    //   whole panel as 'loading status'. On failure, keep the last data (stale-while-revalidate) to remove flicker entirely.
    if (d && d.ok) V3.lastStatus = d;
    else if (!V3.lastStatus) V3.lastStatus = d;   // allow loading display only on first load (no success yet)
    if (d && d.ok && d.config && V3.syncEntryConfig) V3.syncEntryConfig(d.config);
    // ★ [2026-06-02 owner] left-tree running indicator — strategy enabled (green knob) refreshed every poll (system status, 2s cache)
    try {
      const _ss = await V3.getJSON('/api/system/status', { timeoutMs: 4500 });
      if (_ss && _ss.system) V3.lastSys = _ss;   // ★ [2026-06-02 owner] for the status bar atop the strategy view
      const _se = _ss && _ss.system && _ss.system.strategies;
      if (_se) { V3.state.stratEnabled = _se; updateTreeUI(); }
    } catch (_e) { /* ignore transient status failure — recovers on next poll */ }
    if (!V3.state.envView && !V3.state.pcommonView) {
      // LIGHTNING/SNIPER active = body has deploy form/reco inputs → skip full poll re-render (preserve inputs), refresh active list only
      if (V3.state.homeView) loadHome();   // 🏠 home updates in place (positions/equity spans only)
      else if (V3.state.active === 'lightning' && V3.state.selected.has('lightning')) loadLightning();
      else if (V3.state.active === 'sniper' && V3.state.selected.has('sniper')) loadSniper();
      else if (PLUG[V3.state.active] && V3.state.selected.has(V3.state.active)) loadPlug(V3.state.active);
      else if (V3.state.active === 'ladder' && V3.state.selected.has('ladder')) loadLadder();
      else if (TIER2.includes(V3.state.active) && V3.state.selected.has(V3.state.active)) loadTier2Work();   // 🤖 refresh work status (settings ribbon unchanged)
      else renderMain();
    }
    else if (V3.state.envView) loadSettings();   // ⚙️ refresh Settings connection status (preserve inputs · update status spans only)
    } finally {
      V3._pollingStatus = false;
    }
  }
  V3.pollStatus = pollStatus;

  // ── Left tree: multi-select (switches = main display) ──
  function updateTreeUI() {
    const _en = V3.state.stratEnabled || {};   // ★ [2026-06-02 owner] strategy running (enabled) — green toggle knob
    document.querySelectorAll('.v3-strat').forEach((row) => {
      const name = row.dataset.strat;
      const sel = !V3.state.homeView && !V3.state.envView && !V3.state.pcommonView && V3.state.selected.has(name);
      const isCommon = name === 'env' ? V3.state.envView : name === 'plugins-common' ? V3.state.pcommonView : name === 'home' ? V3.state.homeView : false;
      row.classList.toggle('active', (name === 'env' || name === 'plugins-common' || name === 'home') ? isCommon : sel);
      row.classList.toggle('is-active', !V3.state.homeView && !V3.state.envView && !V3.state.pcommonView && name === V3.state.active);   // the strategy the top ribbon + right side follow
      row.classList.toggle('live', !!_en[name]);   // ★ running = green toggle knob (separate from main-display selection)
      const inp = row.querySelector('.v3-toggle input');
      if (inp) inp.checked = sel;
    });
    // Top rail chip state: is-active = top ribbon + right side on this strategy / selected = shown in the main stack
    document.querySelectorAll('.v3-srail').forEach((c) => {
      const n = c.dataset.strat;
      c.classList.toggle('is-active', n === 'env' ? V3.state.envView : (!V3.state.envView && n === V3.state.active));
      c.classList.toggle('selected', !V3.state.envView && n !== 'env' && V3.state.selected.has(n));
    });
    const cur = $('v3-cur-strat');
    if (cur) cur.textContent = V3.state.homeView ? 'Overall Status' : V3.state.envView ? 'Settings' : (TREE_ORDER.filter((n) => V3.state.selected.has(n)).map((n) => LABEL[n]).join(', ') || '—');
  }
  // Strategy select = shared by tree row + top rail chip (clicked strategy → active = top ribbon + right widgets follow)
  // ★ [2026-06-21 owner] GAZUA = spot engine. On click, opens each exchange's spot dashboard in its own tab (named window
  //   = no duplicates·no overwriting the current tab). Add a new exchange (Binance etc.) = one line in this array.
  //   Relative paths, so any server's futures dashboard opens *that server's* spot UI.
  const GAZUA_SPOT_DASHBOARDS = [
    { name: 'gz_upbit',      url: '/ui/dashboard_upbit_v3.html' },
    { name: 'gz_bithumb',    url: '/ui/dashboard_bithumb_v3.html' },
    { name: 'gz_bybit_spot', url: '/ui/dashboard_bybit_spot_v3.html' },
    { name: 'gz_binance', url: '/ui/dashboard_binance_spot_v3.html' },   // 2026-06-23 wired
  ];
  function openGazuaDashboards() {
    GAZUA_SPOT_DASHBOARDS.forEach((d) => { try { window.open(d.url, d.name); } catch (e) { /* ignore popup blockers etc. */ } });
  }
  // ★ [2026-06-21 owner] GAZUA·CONTRARIAN rail lights — show running spot exchanges as favicon-sized icons (replaces the toggle).
  //   read-only·no trading. One call to /spot_gazua_cross/control (server 15s cache). gazua=lit if running / contrarian=running + contrarian ON.
  async function updateSpotLights() {
    const boxes = document.querySelectorAll('.gz-ex');
    if (!boxes.length) return;
    let data = null;
    try { data = await fetch('/api/strategy/spot_gazua_cross/control', { credentials: 'include' }).then((r) => r.json()); }
    catch (e) { return; }
    const byKey = {};
    ((data && data.exchanges) || []).forEach((e) => { byKey[e.key] = e; });
    boxes.forEach((box) => {
      const mode = box.dataset.gzlights;   // 'gazua' | 'contrarian'
      box.querySelectorAll('.gzx').forEach((dot) => {
        const e = byKey[dot.dataset.ex];
        dot.classList.remove('on', 'paper');
        if (!e || !e.present || !e.enabled) return;
        const active = mode === 'contrarian' ? !!e.contrarian_enabled : true;
        if (active) dot.classList.add(e.paper ? 'paper' : 'on');
      });
    });
  }
  V3.updateSpotLights = updateSpotLights;
  function selectStrat(name) {
    if (name === 'gazua') { openGazuaDashboards(); return; }   // ★ GAZUA = spot dashboard launcher (instead of the futures plugin panel)
    if (name === 'contrarian') { openGazuaDashboards(); return; }   // ★ [2026-06-21] contrarian unified into spot — spot dashboard launcher (contrarian menu is there)
    if (name === 'home') { V3.state.homeView = true; V3.state.envView = false; V3.state.pcommonView = false; updateTreeUI(); applyActivePanels(); renderMain(); return; }
    if (name === 'env') { V3.state.envView = true; V3.state.pcommonView = false; V3.state.homeView = false; updateTreeUI(); applyActivePanels(); renderMain(); return; }
    if (name === 'plugins-common') { V3.state.pcommonView = true; V3.state.envView = false; V3.state.homeView = false; updateTreeUI(); applyActivePanels(); renderMain(); loadPluginsCommon(true); return; }
    V3.state.envView = false; V3.state.pcommonView = false; V3.state.homeView = false;
    if (V3.state.selected.has(name)) { V3.state.selected.delete(name); V3.state.order = V3.state.order.filter((x) => x !== name); }
    else { V3.state.selected.add(name); if (!V3.state.order.includes(name)) V3.state.order.push(name); }   // ★ [2026-06-02 owner] sync order (layer order)
    if (V3.state.selected.size === 0) { V3.state.selected.add('focus'); if (!V3.state.order.includes('focus')) V3.state.order.push('focus'); }
    // active = the just-clicked strategy (if it stays in the stack) / if removed, the last remaining selected strategy
    V3.state.active = V3.state.selected.has(name) ? name : (TREE_ORDER.filter((n) => V3.state.selected.has(n)).slice(-1)[0] || 'focus');
    updateTreeUI(); applyActivePanels(); renderMain();
  }
  V3.selectStrat = selectStrat;
  document.querySelectorAll('.v3-strat').forEach((row) => row.addEventListener('click', () => selectStrat(row.dataset.strat)));
  // Click an individual exchange light = only that exchange's spot dashboard (stop row-click propagation)
  document.querySelectorAll('.gz-ex .gzx').forEach((dot) => dot.addEventListener('click', (ev) => {
    ev.stopPropagation();
    const url = { upbit: '/ui/dashboard_upbit_v3.html', bithumb: '/ui/dashboard_bithumb_v3.html', bybit_spot: '/ui/dashboard_bybit_spot_v3.html', binance: '/ui/dashboard_binance_spot_v3.html' }[dot.dataset.ex];
    if (url) { try { window.open(url, 'gz_' + dot.dataset.ex); } catch (e) { /* ignore */ } }
  }));

  // Top strategy rail — icon + first-letter chip for every strategy (hover=full name · click=switch). Non-active strategies remain as breadcrumbs (owner).
  function buildStratRail() {
    const rail = $('v3-strat-rail'); if (!rail) return;
    rail.innerHTML = TREE_ORDER.concat(['env']).map((n) => {
      const ic = n === 'env' ? '⚙️' : (ICON[n] || '•');
      const ab = n === 'env' ? '' : (LABEL[n] || n).charAt(0);
      return '<button class="v3-srail" data-strat="' + n + '" title="' + (LABEL[n] || n) + '"><span class="ic">' + ic + '</span>' + (ab ? '<span class="ab">' + ab + '</span>' : '') + '</button>';
    }).join('');
    rail.querySelectorAll('.v3-srail').forEach((c) => c.addEventListener('click', () => selectStrat(c.dataset.strat)));
  }

  // ── Delegated click: Engine toggle / Positions actions / market→Bybit ──
  document.addEventListener('click', async (e) => {
    // ★ [2026-06-02 owner] layer order ▲▼ — order swap → move body table position (Photoshop-layer style)
    const _lmv = e.target.closest('.v3-lmv');
    if (_lmv) {
      const _n = _lmv.dataset.layer, _dir = parseInt(_lmv.dataset.dir, 10) || 0;
      const _ord = V3.state.order.filter((x) => V3.state.selected.has(x));
      V3.state.selected.forEach((x) => { if (!_ord.includes(x)) _ord.push(x); });
      const _idx = _ord.indexOf(_n), _j = _idx + _dir;
      if (_idx >= 0 && _j >= 0 && _j < _ord.length) {
        const _t = _ord[_idx]; _ord[_idx] = _ord[_j]; _ord[_j] = _t;
        V3.state.order = _ord;
        renderMain();
      }
      return;
    }
    // ₿ logo click = reload the dashboard itself
    if (e.target.closest('#v3-logo')) { location.reload(); return; }
    // 📓 Journal page change / refresh · 📊 BTC side table refresh
    const jp = e.target.closest('.v3-journal-page');
    if (jp) { V3.state.journal.page = parseInt(jp.dataset.page, 10) || 1; loadJournal(true); return; }
    if (e.target.closest('#v3-journal-refresh') || e.target.closest('#v3-daily-refresh')) { loadJournal(true); return; }
    if (e.target.closest('#v3-home-refresh')) { loadHome(true); return; }   // 🏠 home refresh (force-refresh recommendation snapshot)
    if (e.target.closest('#v3-home-baseline')) {   // 💾 reset PnL baseline (reflect deposits — enter an amount directly / leave blank for current equity)
      const inp = prompt('💾 PnL baseline amount (USDT)\n\n· If you deposited, enter the amount including the deposit\n· Leave blank to reset baseline to current equity', '');
      if (inp === null) return;   // cancel
      let _q = '';
      const _s = String(inp).trim();
      if (_s !== '') {
        const _v = parseFloat(_s);
        if (isNaN(_v) || _v <= 0) { V3.toast('✗ Enter a valid amount (or leave blank for current equity)', 'err', 4000); return; }
        _q = '?baseline=' + _v;
      }
      const r = await V3.getJSON('/api/system/pnl-baseline/reset' + _q, { method: 'POST' });
      V3.toast((r && r.ok) ? ('✓ PnL baseline → ' + Number(r.baseline || 0).toFixed(2) + ' USDT' + (r.source === 'manual_input' ? ' (entered)' : ' (current equity)')) : ('✗ Failed: ' + ((r && (r.detail || r.error)) || 'unknown')), (r && r.ok) ? 'ok' : 'err', 5000);
      if (r && r.ok) loadHome(true);
      return;
    }
    // ⚡ Quick trade — instant market buy/sell (POST /api/trade/quick, live trade!)
    const qtSide = e.target.closest('.v3-qt-side');
    if (qtSide) {
      const side = qtSide.dataset.side;
      const market = (($('v3-qt-market') && $('v3-qt-market').value) || '').trim().toUpperCase();
      if (!market) { V3.toast('Enter a market (e.g. BTCUSDT)', 'warn'); return; }
      const mode = ($('v3-qt-mode') && $('v3-qt-mode').value) || 'quote';
      const val = parseFloat(($('v3-qt-amount') && $('v3-qt-amount').value) || '0') || 0;
      if (val <= 0) { V3.toast('Value > 0 required', 'warn'); return; }
      const guard = ($('v3-qt-guard') && $('v3-qt-guard').value) || 'global';
      const sideLab = side === 'buy' ? '🟢 Buy' : '🔴 Sell';
      const amtLab = mode === 'percent' ? (val + '% (available cash)') : (val + ' USDT');
      const ok = await V3.confirm('⚡ Quick Trade (live!)', '<div style="line-height:1.7"><b>' + market + '</b> ' + sideLab + ' ' + amtLab + '<br><small style="color:var(--v3-warn)">⚠️ An instant market order will actually be placed on the exchange.' + (guard === 'force' ? ' <b>Force (ignore guards)</b>' : guard === 'entry_limit_only' ? ' Entry-limit only' : ' Global guards applied') + '</small></div>');
      if (!ok) return;
      V3.toast(market + ' ' + sideLab + '…', 'info');
      const body = { exchange: 'bybit', market_input: market, side: side, amount_mode: mode, amount_value: val, mode: 'immediate', guard_policy: guard };
      const r = await V3.getJSON('/api/trade/quick', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      V3.toast((r && r.ok) ? ('✓ ' + market + ' ' + sideLab + ' order ' + (r.quick_id ? '#' + String(r.quick_id).slice(0, 8) : 'submitted')) : ('✗ Failed: ' + ((r && (r.message || r.detail || r.error)) || 'unknown')), (r && r.ok) ? 'ok' : 'err', 6000);
      if (r && r.ok) loadHome(true);
      return;
    }
    if (e.target.closest('#v3-gp-refresh')) { const inp = $('v3-gp-market'); if (inp && inp.value.trim()) V3.state.gp.market = inp.value.trim().toUpperCase(); loadGp(true); return; }
    // 🔌 Save plugin common settings (per section, /api/reserved/settings query POST) / refresh
    const rsv = e.target.closest('.rsave');
    if (rsv) {
      const sec = rsv.closest('.v3-cset'); if (!sec) return;
      const csave = sec.dataset.csave || 'reserved';
      const inputs = Array.from(sec.querySelectorAll('.v3-rin'));
      V3.toast('Saving common settings…', 'info');
      let okAll = true, err = '', cnt = 0;
      if (csave === 'triage') {                                   // PATCH /api/triage/settings (JSON body)
        const body = {};
        inputs.forEach((el) => {
          const k = el.dataset.rk, kind = el.dataset.kind;
          if (kind === 'chk') { body[k] = el.checked; cnt++; }
          else if (el.value !== '') { body[k] = (kind === 'text' ? el.value : parseFloat(el.value)); cnt++; }
        });
        const r = await V3.getJSON('/api/triage/settings', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        if (!r || r.ok === false || r.detail) { okAll = false; err = (r && (r.error || r.detail)) || 'unknown'; }
      } else if (csave === 'tpsl') {                              // POST /api/reserved/settings?strategy_tp_sl=<JSON>
        const policy = {};
        inputs.forEach((el) => {
          const kind = el.dataset.kind; let val;
          if (kind === 'chk') val = el.checked;
          else { if (el.value === '') return; val = parseFloat(el.value); if (isNaN(val)) return; }
          const parts = el.dataset.rk.split('.'); let o = policy;
          for (let i = 0; i < parts.length - 1; i++) { o[parts[i]] = o[parts[i]] || {}; o = o[parts[i]]; }
          o[parts[parts.length - 1]] = val; cnt++;
        });
        const r = await V3.getJSON('/api/reserved/settings?strategy_tp_sl=' + encodeURIComponent(JSON.stringify(policy)), { method: 'POST' });
        if (!(r && r.ok !== false)) { okAll = false; err = (r && r.error) || 'unknown'; }
      } else {                                                    // reserved | guards → flat query POST (chunk 40, avoid URL length limits)
        const endpoint = csave === 'guards' ? '/api/system/guards' : '/api/reserved/settings';
        const entries = [];
        inputs.forEach((el) => {
          const k = el.dataset.rk, kind = el.dataset.kind;
          if (kind === 'chk') entries.push([k, el.checked ? 'true' : 'false']);
          else { const v = (el.value || '').trim(); if (v !== '') entries.push([k, v]); }
        });
        cnt = entries.length;
        for (let i = 0; i < entries.length && okAll; i += 40) {
          const qs = entries.slice(i, i + 40).map(([k, v]) => k + '=' + encodeURIComponent(v)).join('&');
          const r = await V3.getJSON(endpoint + '?' + qs, { method: 'POST' });
          if (!(r && r.ok !== false)) { okAll = false; err = (r && r.error) || 'unknown'; }
        }
      }
      V3.toast(okAll ? ('✓ Common settings saved (' + cnt + ')') : ('✗ Save failed: ' + err), okAll ? 'ok' : 'err', okAll ? 3500 : 6000);
      loadPluginsCommon(true); return;
    }
    if (e.target.closest('#v3-pcommon-refresh')) { loadPluginsCommon(true); return; }
    // ⚙️ Settings — refresh connection status / Telegram test·save
    if (e.target.closest('#v3-set-refresh')) { loadSettings(); return; }
    // 🛠️ System actions (Reconcile/Dust/Retrain/E-STOP/Restart/Stop) — session auth, strong confirm for dangerous ones
    const saBtn = e.target.closest('button[id^="v3-sa-"]');
    if (saBtn) {
      const cleanup = ($('v3-srv-cleanup') && $('v3-srv-cleanup').checked) ? 1 : 0;   // whether to clean up before server shutdown
      const delay = parseInt(($('v3-srv-delay') && $('v3-srv-delay').value) || '15', 10) || 15;   // cleanup wait 5~60s
      const cleanLab = cleanup ? ('after ' + delay + 's cleanup') : 'immediately without cleanup';
      const SA = {
        'v3-sa-reconcile': { url: '/api/system/reconcile?reason=manual_ui', t: '🔄 Sync Balance', msg: 'Sync exchange balance ↔ OMA state?', danger: false },
        'v3-sa-dust': { url: '/api/engine/clear_dust?threshold=1000', t: '🧹 Clear Dust', msg: 'Clear small leftover (dust) balances?', danger: false },
        'v3-sa-retrain': { url: '/api/ai/train', t: '🧠 Retrain AI', msg: 'Retrain the AI model? (takes a few minutes · runs in background)', danger: false },
        'v3-sa-dd-reset': { url: '/api/strategy/focus/drawdown/reset-cumulative', t: '🔧 Reset Drawdown', msg: 'Reset the drawdown watermark (peak) to current equity?<br><small>Clears a fake CRISIS (-30 conviction) left by deposits/withdrawals or a long pause. Penalty releases on the next tick. Not an entry block.</small>', danger: false },
        'v3-sa-clean-slate': { url: '/api/system/clean-slate', t: '🧹 Clean Slate', msg: '<b style="color:var(--v3-warn)">Close ALL positions and WIPE all trade records?</b><br><small>Paper-mode only (refuses in live). Journals are backed up first. Closes positions on every engine, clears journals, resets the paper balance — for a fresh paper → live start. Restart afterwards.</small>', danger: true },
        'v3-sa-resume': { url: '/api/system/emergency/resume?reason=manual_ui', t: '▶️ Resume E-STOP', msg: 'Release Emergency Stop and resume trading?', danger: false },
        'v3-sa-estop': { url: '/api/system/emergency/stop?reason=manual_ui', t: '🛑 Trigger E-STOP', msg: '<b style="color:var(--v3-warn)">Immediately halt all trading (Emergency Stop)</b>?<br><small>Stops new entries and auto-trading. (Positions are kept.)</small>', danger: true },
        'v3-sa-restart': { url: '/api/system/restart?delay_sec=' + delay + '&cleanup=' + cleanup, t: '🔁 Restart Server', msg: '<b style="color:var(--v3-warn)">Restart the server</b>? (' + cleanLab + ')<br><small>Needs run.ps1 — only stops if not configured.</small>', danger: true },
        'v3-sa-stop': { url: '/api/system/stop?delay_sec=' + delay + '&cleanup=' + cleanup, t: '⏹️ Stop Server', msg: '<b style="color:var(--v3-warn)">Fully stop the server</b>? (' + cleanLab + ')<br><small>You will need to start it manually again.</small>', danger: true },
      };
      const a = SA[saBtn.id]; if (!a) return;
      const ok = await V3.confirm(a.t + (a.danger ? ' (caution!)' : ''), a.msg);
      if (!ok) return;
      V3.toast(a.t + '…', 'info');
      const r = await V3.getJSON(a.url, { method: 'POST' });
      V3.toast((r && r.ok) ? ('✓ ' + a.t + ' done') : ('✗ Failed: ' + ((r && (r.message || r.detail || r.error)) || 'unknown')), (r && r.ok) ? 'ok' : 'err', 6000);
      return;
    }
    if (e.target.closest('#v3-alert-save')) {
      const cv = (k) => { const el = $('v3-alert-' + k); return el ? (el.checked ? 'true' : 'false') : ''; };
      const qs = ['longhold', 'drawdown', 'exit_profit_streak', 'daily', 'harpoon'].map((k) => k + '=' + cv(k)).join('&');
      V3.toast('Saving alert settings…', 'info');
      const r = await V3.getJSON('/api/system/alerts?' + qs, { method: 'POST' });
      V3.toast((r && r.ok) ? '✓ Alert types saved (applied now + .env)' : ('✗ ' + ((r && r.error) || 'failed')), (r && r.ok) ? 'ok' : 'err', 5000);
      if (r && r.ok) loadSettings();
      return;
    }
    if (e.target.closest('#v3-tg-test')) {
      const tk = (($('v3-tg-token') && $('v3-tg-token').value) || '').trim(), cid = (($('v3-tg-chat') && $('v3-tg-chat').value) || '').trim();
      if (!tk || !cid) { V3.toast('Enter token + chat id, then Test', 'warn'); return; }
      V3.toast('✈️ Sending test…', 'info');
      const r = await V3.getJSON('/api/system/telegram/test?token=' + encodeURIComponent(tk) + '&chat_id=' + encodeURIComponent(cid), { method: 'POST' });
      V3.toast((r && r.ok) ? '✈️ Test sent — check Telegram' : ('✗ ' + ((r && r.error) || 'failed')), (r && r.ok) ? 'ok' : 'err', 6000);
      return;
    }
    if (e.target.closest('#v3-peer-save')) {
      e.preventDefault();   // block default form submit (safety net)
      try {
        const urlsRaw = ($('v3-peer-urls') && $('v3-peer-urls').value) || '';
        const urls = urlsRaw.split(/\r?\n/).map((u) => u.trim()).filter(Boolean);
        const pollSec = parseFloat(($('v3-peer-poll-sec') && $('v3-peer-poll-sec').value) || '');
        const slMin = parseInt(($('v3-peer-sl-min') && $('v3-peer-sl-min').value) || '', 10);
        const winMin = parseInt(($('v3-peer-win-min') && $('v3-peer-win-min').value) || '', 10);
        const winBonus = parseFloat(($('v3-peer-win-bonus') && $('v3-peer-win-bonus').value) || '');
        const slPen = parseFloat(($('v3-peer-sl-pen') && $('v3-peer-sl-pen').value) || '');
        const strPen = parseFloat(($('v3-peer-struggle-pen') && $('v3-peer-struggle-pen').value) || '');
        const strAge = parseFloat(($('v3-peer-struggle-age') && $('v3-peer-struggle-age').value) || '');
        const strPeak = parseFloat(($('v3-peer-struggle-peak') && $('v3-peer-struggle-peak').value) || '');
        const confPen = parseFloat(($('v3-peer-conflict-pen') && $('v3-peer-conflict-pen').value) || '');
        const crowdPen = parseFloat(($('v3-peer-crowding-pen') && $('v3-peer-crowding-pen').value) || '');
        const crowdCap = parseFloat(($('v3-peer-crowding-cap') && $('v3-peer-crowding-cap').value) || '');
        const fleetMax = parseInt(($('v3-peer-fleet-dirfail-max') && $('v3-peer-fleet-dirfail-max').value) || '', 10);
        const fleetWin = parseInt(($('v3-peer-fleet-dirfail-win') && $('v3-peer-fleet-dirfail-win').value) || '', 10);
        const body = {
          enabled: !!($('v3-peer-enabled') && $('v3-peer-enabled').checked),
          paper: !!($('v3-peer-paper') && $('v3-peer-paper').checked),
          server_id: (($('v3-peer-server-id') && $('v3-peer-server-id').value) || '').trim(),
          urls: urls,
        };
        if (!isNaN(pollSec) && pollSec >= 2 && pollSec <= 3600) body.poll_interval_sec = pollSec;
        if (!isNaN(slMin) && slMin >= 1 && slMin <= 1440) body.sl_window_min = slMin;
        if (!isNaN(winMin) && winMin >= 1 && winMin <= 1440) body.peer_win_window_min = winMin;
        if (!isNaN(winBonus) && winBonus >= 0 && winBonus <= 50) body.peer_win_bonus = winBonus;
        if (!isNaN(slPen) && slPen >= 0 && slPen <= 50) body.peer_sl_penalty = slPen;
        if (!isNaN(strPen) && strPen >= 0 && strPen <= 50) body.peer_struggle_penalty = strPen;
        if (!isNaN(strAge) && strAge >= 1 && strAge <= 120) body.peer_struggle_age_min = strAge;
        if (!isNaN(strPeak) && strPeak >= 0 && strPeak <= 5) body.peer_struggle_peak_pct = strPeak;
        if (!isNaN(confPen) && confPen >= 0 && confPen <= 50) body.peer_conflict_penalty = confPen;
        if (!isNaN(crowdPen) && crowdPen >= 0 && crowdPen <= 50) body.peer_crowding_penalty = crowdPen;
        if (!isNaN(crowdCap) && crowdCap >= 1 && crowdCap <= 50) body.peer_crowding_cap = crowdCap;
        body.fleet_dirfail_enabled = !!($('v3-peer-fleet-dirfail-en') && $('v3-peer-fleet-dirfail-en').checked);
        if (!isNaN(fleetMax) && fleetMax >= 1 && fleetMax <= 10) body.fleet_dirfail_max = fleetMax;
        if (!isNaN(fleetWin) && fleetWin >= 1 && fleetWin <= 1440) body.fleet_dirfail_window_min = fleetWin;
        console.log('[PEER] save clicked, body=', body);
        V3.toast('👥 Saving peer servers → POST /peer/settings (urls=' + urls.length + ')', 'info', 4000);
        const raw = await fetch('/peer/settings', {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        const txt = await raw.text();
        console.log('[PEER] POST resp status=', raw.status, 'body=', txt);
        let r = null;
        try { r = JSON.parse(txt); } catch { r = { ok: false, error: 'non_json:' + txt.slice(0, 100) }; }
        if (raw.ok && r && r.ok) {
          V3.toast('✓ Saved OK · polling restarted', 'ok', 5000);
          setTimeout(loadPeerSettings, 800);
        } else {
          V3.toast('✗ HTTP ' + raw.status + ' · ' + ((r && (r.error || r.detail)) || txt.slice(0, 80)), 'err', 8000);
        }
      } catch (ex) {
        console.error('[PEER] save exception:', ex);
        V3.toast('✗ Save exception: ' + String(ex), 'err', 8000);
      }
      return;
    }
    if (e.target.closest('#v3-tg-save')) {
      const tk = (($('v3-tg-token') && $('v3-tg-token').value) || '').trim(), cid = (($('v3-tg-chat') && $('v3-tg-chat').value) || '').trim(), pw = (($('v3-tg-admin') && $('v3-tg-admin').value) || '').trim();
      if (!tk || !cid) { V3.toast('Enter token + chat id', 'warn'); return; }
      if (!pw) { V3.toast('Enter admin password (save auth)', 'warn'); return; }
      V3.toast('admin auth…', 'info');
      const lg = await V3.getJSON('/api/system/admin/login?password=' + encodeURIComponent(pw), { method: 'POST' });
      if (!(lg && lg.ok && lg.token)) { V3.toast('✗ admin auth failed: ' + ((lg && lg.error) || ''), 'err', 6000); return; }
      const r = await V3.getJSON('/api/system/telegram/save?token=' + encodeURIComponent(tk) + '&chat_id=' + encodeURIComponent(cid) + '&admin_token=' + encodeURIComponent(lg.token), { method: 'POST' });
      V3.toast((r && r.ok) ? '✓ Telegram saved (.env)' : ('✗ ' + ((r && r.error) || 'failed')), (r && r.ok) ? 'ok' : 'err', 6000);
      if (r && r.ok) { if ($('v3-tg-token')) $('v3-tg-token').value = ''; if ($('v3-tg-admin')) $('v3-tg-admin').value = ''; loadSettings(); }
      return;
    }
    // ⚡ LIGHTNING — plugin refresh / deploy (setup) / stop·liquidate·delete (stop)
    const plr = e.target.closest('[data-plugin-refresh]');
    if (plr) { const pn = plr.dataset.pluginRefresh; if (pn === 'lightning') loadLightning(true); else if (pn === 'sniper') loadSniper(true); else if (pn === 'ladder') loadLadder(true); else if (PLUG[pn]) loadPlug(pn, true); return; }
    const ladStep = e.target.closest('.v3-ladder-steps-btn');   // 📐 view steps (read-only)
    if (ladStep) { loadLadderSteps(ladStep.dataset.mkt); return; }
    // 🔧 LADDER individual step (live order) — pause/resume (step/status)
    const lsPause = e.target.closest('.v3-lad-step-pause');
    if (lsPause) {
      const mkt = lsPause.dataset.mkt, uuid = lsPause.dataset.uuid, st = lsPause.dataset.st, verb = st === 'paused' ? 'Pause' : 'Resume';
      const ok = await V3.confirm('⏸ ' + verb + ' Step (live order!)', '<b>' + mkt + '</b> <b>' + verb + '</b> this step\'s exchange order?<br><small style="color:var(--v3-warn)">' + (st === 'paused' ? 'Pausing cancels the exchange limit order (resuming re-places it)' : 'Resuming re-places the limit order on the exchange') + '</small>');
      if (!ok) return;
      const r = await V3.getJSON('/api/ladder/step/status?market=' + encodeURIComponent(mkt) + '&step_uuid=' + encodeURIComponent(uuid) + '&status=' + st, { method: 'POST' });
      V3.toast((r && r.ok) ? ('✓ Step ' + verb + ' done') : ('✗ Failed: ' + ((r && (r.detail || r.error)) || '')), (r && r.ok) ? 'ok' : 'err', 5000);
      if (r && r.ok) loadLadderSteps(mkt);
      return;
    }
    // 🔧 LADDER individual step (live order) — edit price/qty (step/edit)
    const lsEdit = e.target.closest('.v3-lad-step-edit');
    if (lsEdit) {
      const mkt = lsEdit.dataset.mkt, uuid = lsEdit.dataset.uuid;
      const np = prompt(mkt + ' step — enter new price (line) (blank = keep):', lsEdit.dataset.price);
      if (np === null) return;
      const na = prompt(mkt + ' step — enter new amount (USDT) (blank = keep):', lsEdit.dataset.amount);
      if (na === null) return;
      let qs = '/api/ladder/step/edit?market=' + encodeURIComponent(mkt) + '&step_uuid=' + encodeURIComponent(uuid);
      const npN = Number(np), naN = Number(na);
      if (np.trim() && npN > 0) qs += '&price=' + encodeURIComponent(npN);
      if (na.trim() && naN > 0) qs += '&amount=' + encodeURIComponent(naN);
      if (qs.indexOf('&price=') < 0 && qs.indexOf('&amount=') < 0) { V3.toast('No change (both price and amount blank)', 'warn'); return; }
      const ok = await V3.confirm('✏️ Edit Step (live order!)', '<b>' + mkt + '</b> Edit this step?<br><small>Price: ' + (np.trim() && npN > 0 ? _fp(npN) : 'keep') + ' · Amount: ' + (na.trim() && naN > 0 ? '$' + naN : 'keep') + '</small><br><small style="color:var(--v3-warn)">Cancels the exchange order and re-places at the new price/qty.</small>');
      if (!ok) return;
      const r = await V3.getJSON(qs, { method: 'POST' });
      V3.toast((r && r.ok) ? '✓ Step edited' : ('✗ Failed: ' + ((r && (r.detail || r.error)) || '')), (r && r.ok) ? 'ok' : 'err', 5000);
      if (r && r.ok) loadLadderSteps(mkt);
      return;
    }
    // 🔧 LADDER individual step (live order) — delete (step/delete)
    const lsDel = e.target.closest('.v3-lad-step-del');
    if (lsDel) {
      const mkt = lsDel.dataset.mkt, uuid = lsDel.dataset.uuid;
      const ok = await V3.confirm('🗑 Delete Step (live order!)', '<b>' + mkt + '</b> <b>Delete</b> this step?<br><small style="color:var(--v3-warn)">⚠️ The exchange limit order is canceled and removed from the grid. Cannot be undone.</small>');
      if (!ok) return;
      const r = await V3.getJSON('/api/ladder/step/delete?market=' + encodeURIComponent(mkt) + '&step_uuid=' + encodeURIComponent(uuid), { method: 'POST' });
      V3.toast((r && r.ok) ? '✓ Step deleted' : ('✗ Failed: ' + ((r && (r.detail || r.error)) || '')), (r && r.ok) ? 'ok' : 'err', 5000);
      if (r && r.ok) loadLadderSteps(mkt);
      return;
    }
    const ladSeed = e.target.closest('.v3-ladder-seed');   // 🌱 place grid orders (live orders)
    if (ladSeed) {
      const mkt = ladSeed.dataset.mkt;
      const ok = await V3.confirm('🌱 Place LADDER Orders (live!)', '<div style="line-height:1.7"><b>' + mkt + '</b> Actually places grid limit <b>buy orders</b> on the exchange.<br><small style="color:var(--v3-warn)">⚠️ Live trade — N limit orders per the saved config (steps·gap·budget). This button places immediately even if the slot is locked.</small></div>');
      if (!ok) return;
      V3.toast(mkt + ' placing orders…', 'info');
      const r = await V3.getJSON('/api/ladder/seed?market=' + encodeURIComponent(mkt), { method: 'POST' });
      const s = (r && r.summary) || {};
      V3.toast((r && r.ok) ? ('🌱 ' + mkt + ' ' + (s.created_buy != null ? s.created_buy + ' orders placed' : 'done') + (s.failed && s.failed.length ? ' (' + s.failed.length + ' failed)' : '')) : ('✗ Failed: ' + ((r && (r.detail || r.error)) || '')), (r && r.ok) ? 'ok' : 'err', 6000);
      loadLadder(true); if (V3.state.ladder.stepsMarket === mkt) loadLadderSteps(mkt);
      return;
    }
    const ladCancel = e.target.closest('.v3-ladder-cancel');   // 🗑️ cancel all orders
    if (ladCancel) {
      const mkt = ladCancel.dataset.mkt;
      const ok = await V3.confirm('🗑️ Cancel LADDER Orders', '<b>' + mkt + '</b> — <b>Cancel all pending</b> LADDER limit orders for this market? (positions kept, only unfilled orders canceled)');
      if (!ok) return;
      V3.toast(mkt + ' canceling orders…', 'info');
      const r = await V3.getJSON('/api/ladder/cancel?market=' + encodeURIComponent(mkt), { method: 'POST' });
      const s = (r && r.summary) || {};
      V3.toast((r && r.ok) ? ('🗑️ ' + mkt + ' ' + (s.canceled != null ? s.canceled + ' orders canceled' : 'canceled')) : ('✗ Failed: ' + ((r && (r.detail || r.error)) || '')), (r && r.ok) ? 'ok' : 'err', 6000);
      loadLadder(true); if (V3.state.ladder.stepsMarket === mkt) loadLadderSteps(mkt);
      return;
    }
    const tsv = e.target.closest('.v3-tune-save');   // 🎛️ Save Tier-2 unique tuning (applied at slot-fill)
    if (tsv) {
      const key = tsv.dataset.plug, obj = {};
      (TIER2_TUNE[key] || []).forEach((f) => { const el = $('v3-tune-' + key + '-' + f[0]); if (el && el.value !== '') { const n = parseFloat(el.value); if (!isNaN(n)) obj[f[0]] = n; } });
      V3.toast('Saving ' + (LABEL[key] || key) + ' tuning…', 'info');
      const payload = {}; payload[key.toUpperCase()] = obj;
      const r = await V3.getJSON('/api/reserved/plugin-params?data=' + encodeURIComponent(JSON.stringify(payload)), { method: 'POST' });
      V3.toast((r && r.ok) ? ('✓ ' + (LABEL[key] || key) + ' tuning saved — applies on the next slot entry (after restart)') : ('✗ Failed: ' + ((r && (r.detail || r.error)) || '')), (r && r.ok) ? 'ok' : 'err', 5000);
      return;
    }
    // 🤖 Tier-2 recommended coin → autopilot priority enqueue (semi-auto — autopilot reviews then enters with that strategy's logic)
    const t2e = e.target.closest('.v3-t2-enq');
    if (t2e) {
      const key = t2e.dataset.key, mkt = t2e.dataset.mkt;
      const items = (V3.state.tier2reco && V3.state.tier2reco[key] && V3.state.tier2reco[key].items) || [];
      const it = items.find((x) => x.market === mkt);
      if (!it) { V3.toast('No recommendation item — refresh', 'warn'); return; }
      const ok = await V3.confirm('🤖 autopilot priority enqueue', '<div style="line-height:1.7">Enqueue <b>' + mkt + '</b> as a priority candidate for <b>' + (LABEL[key] || key) + '</b> autopilot?<br><small>If it passes autopilot\'s AI·conviction gates, it enters with ' + (LABEL[key] || key) + ' logic (' + (key === 'autoloop' ? 'scale-in·LongHold' : key === 'pingpong' ? 'range ping-pong' : 'profile') + '). <b>Not a guaranteed entry</b> — just moves it to the front of the line.</small></div>');
      if (!ok) return;
      const body = Object.assign({}, it, { strategy: key.toUpperCase() });
      V3.toast(mkt + ' enqueueing…', 'info');
      const r = await V3.getJSON('/api/reserved/enqueue', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      V3.toast((r && r.ok) ? ('✓ ' + mkt + ' → ' + (LABEL[key] || key) + ' priority enqueued (awaiting autopilot review)') : ('✗ Failed: ' + ((r && (r.detail || r.error)) || 'unknown')), (r && r.ok) ? 'ok' : 'err', 6000);
      return;
    }
    // 📐 Save LADDER config — no orders placed (forces buy_now·grid_auto_sync=false). Owner: "configure it then lock the slot"
    if (e.target.closest('#v3-ladder-deploy')) {
      const market = (($('v3-ladder-market') && $('v3-ladder-market').value) || '').trim().toUpperCase();
      if (!market) { V3.toast('Enter a market (e.g. BTCUSDT)', 'warn'); return; }
      const budget = parseFloat(($('v3-ladder-budget') && $('v3-ladder-budget').value) || '0') || 0;
      const maxSteps = parseInt(($('v3-ladder-maxsteps') && $('v3-ladder-maxsteps').value) || '10', 10) || 10;
      const stepPct = parseFloat(($('v3-ladder-steppct') && $('v3-ladder-steppct').value) || '1') || 1;
      const orderUsdt = parseFloat(($('v3-ladder-order') && $('v3-ladder-order').value) || '0') || 0;
      const mart = parseFloat(($('v3-ladder-mart') && $('v3-ladder-mart').value) || '1') || 1;
      const tp = parseFloat(($('v3-ladder-tp') && $('v3-ladder-tp').value) || '2') || 2;
      if (budget <= 0) { V3.toast('Budget > 0 required', 'warn'); return; }
      if (maxSteps < 1) { V3.toast('Steps ≥ 1 (prevent a zombie ladder)', 'warn'); return; }
      const autoOn = !!($('v3-ladder-autosync') && $('v3-ladder-autosync').checked);   // 🟢 Auto ON = grid_auto_sync (opens slot and auto-trades)
      const ok = await V3.confirm('📐 LADDER ' + (autoOn ? 'Auto ON (start live trading)' : 'Save config (no orders)'), '<div style="line-height:1.7"><b>' + market + '</b> grid<br><small>Budget $' + budget + ' · ' + maxSteps + ' steps · gap ' + stepPct + '% · martingale ' + mart + ' · TP ' + tp + '%</small><br>' + (autoOn ? '<b style="color:var(--v3-warn)">🟢 Auto ON — opens slot, places grid limit orders, and starts auto-trading (live!)</b>' : '<b style="color:var(--v3-pos)">Config saved only (no orders) — slot OFF. Check via [📊 View Steps], then save again with 🟢 ON to auto-trade.</b>') + '</div>');
      if (!ok) return;
      V3.toast(market + (autoOn ? ' Auto ON…' : ' saving config…'), 'info');
      const lgb = {};   // 🪜 collect ribbon grid advanced (v3-ladder-g-*) → merge into setup body (spacing/atr/emergency/auto_center etc.)
      document.querySelectorAll('[id^="v3-ladder-g-"]').forEach((el) => { const p = el.id.slice('v3-ladder-g-'.length); if (el.type === 'checkbox') lgb[p] = el.checked; else if (el.value !== '') { const n = parseFloat(el.value); lgb[p] = (el.tagName === 'SELECT' || isNaN(n)) ? el.value : n; } });
      const body = Object.assign({ market: market, budget_usdt: budget, max_steps: maxSteps, step_pct: stepPct, martingale: mart, tp: tp, buy_now: false, grid_auto_sync: autoOn, tune_mode: 'MANUAL' }, lgb);
      if (orderUsdt > 0) body.order_usdt = orderUsdt;
      const r = await V3.getJSON('/api/strategy/ladder/setup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      V3.toast((r && r.ok) ? ('📐 ' + market + (autoOn ? ' Auto ON done — grid auto-trading started' : ' config saved (no orders) — check via [📊 View Steps]')) : ('✗ Failed: ' + ((r && (r.detail || r.error)) || 'unknown')), (r && r.ok) ? 'ok' : 'err', 6000);
      if (r && r.ok) { loadLadder(true); loadLadderSteps(market); }
      return;
    }
    const ltgDep = e.target.closest('#v3-ltg-deploy') || e.target.closest('#v3-ltg-update');
    if (ltgDep) {
      const isUpd = ltgDep.id === 'v3-ltg-update', lbl = isUpd ? 'Update (re-setup)' : 'Deploy';
      const market = (($('v3-ltg-market') && $('v3-ltg-market').value) || '').trim().toUpperCase();
      if (!market) { V3.toast('Please enter a market (e.g. BTCUSDT)', 'warn'); return; }
      const budget = parseFloat(($('v3-ltg-budget') && $('v3-ltg-budget').value) || '0') || 0;
      const tp = parseFloat(($('v3-ltg-tp') && $('v3-ltg-tp').value) || '5') || 5;
      const sl = parseFloat(($('v3-ltg-sl') && $('v3-ltg-sl').value) || '-3') || -3;
      const ok = await V3.confirm('LIGHTNING ' + lbl, '<div style="line-height:1.7"><b>' + market + '</b> ' + lbl + '<br><small>Budget $' + budget + ' · TP ' + tp + '% / SL ' + sl + '%</small><br><small style="color:var(--v3-warn)">⚠️ Live trade — ' + (isUpd ? 're-setup' : 'deploy') + ' on the LIGHTNING strategy</small></div>');
      if (!ok) return;
      V3.toast(market + ' ' + lbl + '…', 'info');
      const r = await V3.getJSON('/api/strategy/lightning/setup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ market: market, budget_usdt: budget, tp_pct: tp, sl_pct: sl }) });
      V3.toast((r && r.ok) ? ('⚡ ' + market + ' ' + lbl + ' done') : ('✗ ' + lbl + ' failed: ' + ((r && (r.detail || r.error)) || 'unknown')), (r && r.ok) ? 'ok' : 'err', 5000);
      loadLightning(true); return;
    }
    if (e.target.closest('#v3-ltg-guards-save')) {
      const cb = (id) => { const el = $(id); return el ? el.checked : null; };
      const nm = (id) => { const el = $(id); if (!el || el.value === '') return null; const n = parseFloat(el.value); return isNaN(n) ? null : n; };
      const body = {
        entry_ob_guard_enabled: cb('v3-ltg-g-ob'), entry_ceiling_guard: cb('v3-ltg-g-ceiling'),
        exit_profit_guard: cb('v3-ltg-g-profit'), exit_min_net_profit_pct: nm('v3-ltg-g-minprofit'),
        exit_slippage_guard_bps: nm('v3-ltg-g-slippage'), min_order_usdt: nm('v3-ltg-g-minorder'),
        user_sell_only: cb('v3-ltg-g-abslock'), hold_sell: cb('v3-ltg-g-holdsell'),
      };
      V3.toast('Saving LIGHTNING Guards…', 'info');
      const r = await V3.getJSON('/api/strategy/lightning/guards', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const n = (r && r.applied_markets && r.applied_markets.length) || 0;
      V3.toast((r && r.ok) ? ('✓ Guards saved · applied to ' + n + ' markets') : ('✗ Failed: ' + ((r && r.error) || '')), (r && r.ok) ? 'ok' : 'err', 5000);
      return;
    }
    if (e.target.closest('#v3-ltg-recos-refresh')) { loadLightningRecos(true); return; }
    // Click a recommendation row → fill the deploy form (ribbon) (v2 fillCandidateToForm · fills even if occupied — not blocked)
    const rpg = e.target.closest('#v3-ltg-recos .v3-recopage');   // recommendation page change (LIGHTNING container only — separate from SNIPER rows)
    if (rpg) { V3.state.lightning.recoPage = parseInt(rpg.dataset.page, 10) || 1; const e0 = $('v3-ltg-recos'); if (e0) e0.innerHTML = renderRecos(V3.state.lightning.recos); return; }
    const rr = e.target.closest('#v3-ltg-recos .v3-reco-row');
    if (rr) {
      const m = $('v3-ltg-market'), b = $('v3-ltg-budget'), tpe = $('v3-ltg-tp'), sle = $('v3-ltg-sl');
      if (m) m.value = rr.dataset.mkt || '';
      if (b && rr.dataset.budget) b.value = rr.dataset.budget;   // capital-linked suggested budget (keep form value if blank, as a leftover safeguard)
      if (tpe && rr.dataset.tp !== '') tpe.value = rr.dataset.tp;
      if (sle && rr.dataset.sl !== '') sle.value = rr.dataset.sl;
      updateLtgEst();
      V3.toast((rr.dataset.mkt || '') + ' → form filled (confirm budget/TP/SL then ⚡Deploy)', 'info');
      return;
    }
    // Suggested — find the entered market in the recommendation cache and apply TP/SL (+ budget if 'Also set budget' checked) (v2 applyRecommended)
    if (e.target.closest('#v3-ltg-recommend')) {
      const mkt = (($('v3-ltg-market') && $('v3-ltg-market').value) || '').trim().toUpperCase();
      if (!mkt) { V3.toast('Enter a Market then Suggested', 'warn'); return; }
      const recos = V3.state.lightning.recos;
      const it = recos && recos.items && recos.items.find((x) => String(x.market || '').toUpperCase() === mkt);
      if (!it) { V3.toast(mkt + ' not in the recommendation list — 🔍 Load recommendations or click a row', 'warn'); return; }
      const rp = it.recommended_params || {};
      if (rp.tp_pct != null && $('v3-ltg-tp')) $('v3-ltg-tp').value = rp.tp_pct;
      if (rp.sl_pct != null && $('v3-ltg-sl')) $('v3-ltg-sl').value = rp.sl_pct;
      const recBud = $('v3-ltg-recbudget') && $('v3-ltg-recbudget').checked;
      let budApplied = false;
      if (recBud) {
        const bud = Number(it.suggested_budget_usdt || it.budget || 0);
        if (bud > 10000) { V3.toast('Suggested budget $' + Math.round(bud) + ' = abnormal (old KRW-unit leftover) — enter the budget manually. It normalizes to a capital basis after a server restart', 'warn', 7000); }
        else if (bud > 0 && $('v3-ltg-budget')) { $('v3-ltg-budget').value = Math.round(bud); budApplied = true; }
      }
      updateLtgEst();
      V3.toast('Suggested applied: ' + mkt + ' (TP/SL' + (budApplied ? ' + budget' : '') + ')', 'ok');
      return;
    }
    const lstop = e.target.closest('.v3-ltg-stop');
    if (lstop) {
      const mkt = lstop.dataset.mkt, act = lstop.dataset.act;
      const lab = act === 'liquidate' ? 'Liquidate (sell position)' : act === 'delete' ? 'Delete (DISABLED)' : 'Stop (WATCH)';
      if (!(await V3.confirm('LIGHTNING ' + lab, '<b>' + mkt + '</b> — ' + lab + '?'))) return;
      const r = await V3.getJSON('/api/strategy/lightning/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ market: mkt, liquidate: act === 'liquidate', delete: act === 'delete' }) });
      V3.toast((r && r.ok) ? ('✓ ' + mkt + ' ' + lab) : ('✗ Failed: ' + ((r && r.error) || '')), (r && r.ok) ? 'ok' : 'err');
      loadLightning(true); return;
    }
    // 🎯 SNIPER — deploy (setup JSON body·side·source) / recommend / stop·delete (stop = query string·sniper_id preferred)
    const snpDep = e.target.closest('#v3-snp-deploy') || e.target.closest('#v3-snp-update');
    if (snpDep) {
      const isUpd = snpDep.id === 'v3-snp-update', lbl = isUpd ? 'Update (re-setup)' : 'Deploy';
      const market = (($('v3-snp-market') && $('v3-snp-market').value) || '').trim().toUpperCase();
      if (!market) { V3.toast('Please enter a market (e.g. BTCUSDT)', 'warn'); return; }
      const side = ($('v3-snp-side') && $('v3-snp-side').value) || 'LONG';
      const budget = parseFloat(($('v3-snp-budget') && $('v3-snp-budget').value) || '0') || 0;
      const tp = parseFloat(($('v3-snp-tp') && $('v3-snp-tp').value) || '2') || 2;
      const sl = parseFloat(($('v3-snp-sl') && $('v3-snp-sl').value) || '-2.5') || -2.5;
      const ok = await V3.confirm('SNIPER ' + lbl, '<div style="line-height:1.7"><b>' + market + '</b> <span class="v3-badge ' + (side === 'SHORT' ? 'short' : 'long') + '">' + side + '</span> ' + lbl + '<br><small>Budget $' + budget + ' · TP ' + tp + '% / SL ' + sl + '%</small><br><small style="color:var(--v3-warn)">⚠️ Live trade — SNIPER waits for an entry signal (WATCH) then auto-enters</small></div>');
      if (!ok) return;
      V3.toast(market + ' ' + lbl + '…', 'info');
      // 🎯 collect ribbon snipe conditions (v3-snp-g-{param}) → merge into setup body (select=cycle_mode string)
      const sgb = {};
      document.querySelectorAll('[id^="v3-snp-g-"]').forEach((el) => { const p = el.id.slice('v3-snp-g-'.length); if (el.type === 'checkbox') sgb[p] = el.checked; else if (el.value !== '') { const n = parseFloat(el.value); sgb[p] = (el.tagName === 'SELECT' || el.type === 'time' || el.type === 'text' || isNaN(n)) ? el.value : n; } });   // keep time/text as string (avoid time_start "09:00"→parseFloat 9)
      const r = await V3.getJSON('/api/strategy/sniper/setup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(Object.assign({ market: market, profile: 'SNIPER', side: side, budget_usdt: budget, tp_pct: tp, sl_pct: sl, source: 'manual' }, sgb)) });
      V3.toast((r && r.ok) ? ('🎯 ' + market + ' ' + lbl + ' done') : ('✗ ' + lbl + ' failed: ' + ((r && (r.detail || r.error)) || 'unknown')), (r && r.ok) ? 'ok' : 'err', 5000);
      loadSniper(true); return;
    }
    if (e.target.closest('#v3-snp-recos-refresh')) { loadSniperRecos(true); return; }
    const snpPg = e.target.closest('#v3-snp-recos .v3-recopage');
    if (snpPg) { V3.state.sniper.recoPage = parseInt(snpPg.dataset.page, 10) || 1; const e0 = $('v3-snp-recos'); if (e0) e0.innerHTML = renderSnpRecos(V3.state.sniper.recos); return; }
    const snpRr = e.target.closest('#v3-snp-recos .v3-reco-row');
    if (snpRr) {
      const m = $('v3-snp-market'), b = $('v3-snp-budget'), tpe = $('v3-snp-tp'), sle = $('v3-snp-sl');
      if (m) m.value = snpRr.dataset.mkt || '';
      if (b && snpRr.dataset.budget) b.value = snpRr.dataset.budget;
      if (tpe && snpRr.dataset.tp !== '') tpe.value = snpRr.dataset.tp;
      if (sle && snpRr.dataset.sl !== '') sle.value = snpRr.dataset.sl;
      updateSnpEst();
      V3.toast((snpRr.dataset.mkt || '') + ' → form filled (confirm budget/TP/SL then 🎯Deploy)', 'info');
      return;
    }
    if (e.target.closest('#v3-snp-recommend')) {
      const mkt = (($('v3-snp-market') && $('v3-snp-market').value) || '').trim().toUpperCase();
      if (!mkt) { V3.toast('Enter a Market then Suggested', 'warn'); return; }
      const recos = V3.state.sniper.recos;
      const it = recos && recos.items && recos.items.find((x) => String(x.market || '').toUpperCase() === mkt);
      if (!it) { V3.toast(mkt + ' not in the recommendation list — 🔍 Load recommendations or click a row', 'warn'); return; }
      const rp = it.recommended_params || {};
      if (rp.tp_pct != null && $('v3-snp-tp')) $('v3-snp-tp').value = rp.tp_pct;
      if (rp.sl_pct != null && $('v3-snp-sl')) $('v3-snp-sl').value = rp.sl_pct;
      const recBud = $('v3-snp-recbudget') && $('v3-snp-recbudget').checked;
      let budApplied = false;
      if (recBud) { const bud = Number(it.suggested_budget_usdt || it.budget || 0); if (bud > 10000) { V3.toast('Suggested budget $' + Math.round(bud) + ' = abnormal (old KRW leftover) — enter manually', 'warn', 6000); } else if (bud > 0 && $('v3-snp-budget')) { $('v3-snp-budget').value = Math.round(bud); budApplied = true; } }
      updateSnpEst();
      V3.toast('Suggested applied: ' + mkt + ' (TP/SL' + (budApplied ? ' + budget' : '') + ')', 'ok');
      return;
    }
    const snpStop = e.target.closest('.v3-snp-stop');
    if (snpStop) {
      const sid = snpStop.dataset.sid, mkt = snpStop.dataset.mkt, del = snpStop.dataset.act === 'delete';
      const lab = del ? 'Delete (DISABLED)' : 'Stop (WATCH)';
      if (!(await V3.confirm('SNIPER ' + lab, '<b>' + mkt + '</b> — ' + lab + '?'))) return;
      const qs = (sid ? ('sniper_id=' + encodeURIComponent(sid)) : ('market=' + encodeURIComponent(mkt))) + '&delete=' + (del ? 'true' : 'false');
      const r = await V3.getJSON('/api/strategy/sniper/stop?' + qs, { method: 'POST' });
      V3.toast((r && r.ok) ? ('✓ ' + mkt + ' ' + lab) : ('✗ Failed: ' + ((r && (r.detail || r.error)) || '')), (r && r.ok) ? 'ok' : 'err');
      loadSniper(true); return;
    }
    // 🔌 Generic plugin panel (GAZUA etc.) — data-plug based (after LIGHTNING/SNIPER so their rows are already handled)
    const pgDep = e.target.closest('.v3-plug-deploy');
    if (pgDep) {
      const key = pgDep.dataset.plug, c = PLUG[key]; if (!c) return;
      const isUpd = pgDep.dataset.upd === '1', lbl = isUpd ? 'Update (re-setup)' : 'Deploy';
      const market = (($('v3-' + key + '-market') && $('v3-' + key + '-market').value) || '').trim().toUpperCase();
      if (!market) { V3.toast('Please enter a market (e.g. BTCUSDT)', 'warn'); return; }
      const f = { market: market, budget: parseFloat(($('v3-' + key + '-budget') && $('v3-' + key + '-budget').value) || '0') || 0, tp: parseFloat(($('v3-' + key + '-tp') && $('v3-' + key + '-tp').value) || c.defTp) || parseFloat(c.defTp), sl: parseFloat(($('v3-' + key + '-sl') && $('v3-' + key + '-sl').value) || c.defSl) || parseFloat(c.defSl) };
      if (c.hasSide) f.side = ($('v3-' + key + '-side') && $('v3-' + key + '-side').value) || 'LONG';
      const ok = await V3.confirm(c.label + ' ' + lbl, '<div style="line-height:1.7"><b>' + market + '</b>' + (c.hasSide ? ' <span class="v3-badge ' + (f.side === 'SHORT' ? 'short' : 'long') + '">' + f.side + '</span>' : '') + ' ' + lbl + '<br><small>Budget $' + f.budget + ' · TP ' + f.tp + '% / SL ' + f.sl + '%</small><br><small style="color:var(--v3-warn)">⚠️ Live trade — ' + (isUpd ? 're-setup' : 'deploy') + ' on the ' + c.label + ' strategy</small></div>');
      if (!ok) return;
      V3.toast(market + ' ' + lbl + '…', 'info');
      // 🛡️ collect ribbon Entry/Exit guards (v3-{key}-g-{param}) → merge into setup body (owner: "apply on deploy")
      const gbody = {};
      document.querySelectorAll('[id^="v3-' + key + '-g-"]').forEach((el) => { const p = el.id.slice(('v3-' + key + '-g-').length); if (el.type === 'checkbox') gbody[p] = el.checked; else if (el.value !== '') { const n = parseFloat(el.value); gbody[p] = (el.tagName === 'SELECT' || isNaN(n)) ? el.value : n; } });
      const r = await V3.getJSON('/api/strategy/' + c.api + '/setup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(Object.assign({}, c.setupBody(f), gbody)) });
      V3.toast((r && r.ok) ? ('✓ ' + market + ' ' + lbl + ' done') : ('✗ ' + lbl + ' failed: ' + ((r && (r.detail || r.error)) || 'unknown')), (r && r.ok) ? 'ok' : 'err', 5000);
      loadPlug(key, true); return;
    }
    const pgRef = e.target.closest('.v3-plug-recos-refresh');
    if (pgRef) { loadPlugRecos(pgRef.dataset.plug, true); return; }
    const pgPage = e.target.closest('.v3-plug-recos .v3-recopage');
    if (pgPage) { const key = pgPage.closest('.v3-plug-recos').dataset.plug, st = plugState(key); st.recoPage = parseInt(pgPage.dataset.page, 10) || 1; const e0 = $('v3-' + key + '-recos'); if (e0) e0.innerHTML = renderPlugRecos(key, st.recos); return; }
    const pgRow = e.target.closest('.v3-plug-recos .v3-reco-row');
    if (pgRow) {
      const key = pgRow.closest('.v3-plug-recos').dataset.plug;
      const m = $('v3-' + key + '-market'), b = $('v3-' + key + '-budget'), tpe = $('v3-' + key + '-tp'), sle = $('v3-' + key + '-sl');
      if (m) m.value = pgRow.dataset.mkt || '';
      if (b && pgRow.dataset.budget) b.value = pgRow.dataset.budget;
      if (tpe && pgRow.dataset.tp !== '') tpe.value = pgRow.dataset.tp;
      if (sle && pgRow.dataset.sl !== '') sle.value = pgRow.dataset.sl;
      updatePlugEst(key);
      V3.toast((pgRow.dataset.mkt || '') + ' → form filled (confirm budget/TP/SL then deploy)', 'info'); return;
    }
    const pgRec = e.target.closest('.v3-plug-recommend');
    if (pgRec) {
      const key = pgRec.dataset.plug, st = plugState(key);
      const mkt = (($('v3-' + key + '-market') && $('v3-' + key + '-market').value) || '').trim().toUpperCase();
      if (!mkt) { V3.toast('Enter a Market then Suggested', 'warn'); return; }
      const it = st.recos && st.recos.items && st.recos.items.find((x) => String(x.market || '').toUpperCase() === mkt);
      if (!it) { V3.toast(mkt + ' not in the recommendation list — 🔍 Load recommendations or click a row', 'warn'); return; }
      const rp = it.recommended_params || {};
      if (rp.tp_pct != null && $('v3-' + key + '-tp')) $('v3-' + key + '-tp').value = rp.tp_pct;
      if (rp.sl_pct != null && $('v3-' + key + '-sl')) $('v3-' + key + '-sl').value = rp.sl_pct;
      const recBud = $('v3-' + key + '-recbudget') && $('v3-' + key + '-recbudget').checked;
      let budApplied = false;
      if (recBud) { const bud = Number(it.suggested_budget_usdt || it.budget || 0); if (bud > 10000) { V3.toast('Suggested budget abnormal (old KRW leftover) — enter manually', 'warn', 6000); } else if (bud > 0 && $('v3-' + key + '-budget')) { $('v3-' + key + '-budget').value = Math.round(bud); budApplied = true; } }
      updatePlugEst(key);
      V3.toast('Suggested applied: ' + mkt + ' (TP/SL' + (budApplied ? ' + budget' : '') + ')', 'ok'); return;
    }
    const pgStop = e.target.closest('.v3-plug-stop');
    if (pgStop) {
      const key = pgStop.dataset.plug, c = PLUG[key], mkt = pgStop.dataset.mkt, act = pgStop.dataset.act;
      const lab = act === 'liquidate' ? 'Liquidate (sell position)' : act === 'delete' ? 'Delete (DISABLED)' : 'Stop (WATCH)';
      if (!(await V3.confirm(c.label + ' ' + lab, '<b>' + mkt + '</b> — ' + lab + '?'))) return;
      let r;
      if (c.stopMode === 'query') { r = await V3.getJSON('/api/strategy/' + c.api + '/stop?market=' + encodeURIComponent(mkt) + '&delete=' + (act === 'delete' ? 'true' : 'false'), { method: 'POST' }); }
      else { r = await V3.getJSON('/api/strategy/' + c.api + '/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ market: mkt, liquidate: act === 'liquidate', delete: act === 'delete' }) }); }
      V3.toast((r && r.ok) ? ('✓ ' + mkt + ' ' + lab) : ('✗ Failed: ' + ((r && (r.detail || r.error)) || '')), (r && r.ok) ? 'ok' : 'err');
      loadPlug(key, true); return;
    }
    // 🖐 Manual Control — Force Select / Disable+Close (same focusApi: query param POST)
    if (e.target.closest('#v3-manual-force')) {
      const mkt = ($('v3-manual-market') && $('v3-manual-market').value.trim().toUpperCase()) || 'XAUTUSDT';
      const dir = ($('v3-manual-dir') && $('v3-manual-dir').value) || 'LONG';
      const ok = await V3.confirm('Force Select', '<b>' + mkt + '</b> <span class="v3-badge ' + (dir === 'LONG' ? 'long' : 'short') + '">' + dir + '</span> Force-select (pin)?');
      if (!ok) return;
      const r = await V3.getJSON('/api/strategy/focus/force-select?market=' + encodeURIComponent(mkt) + '&direction=' + dir, { method: 'POST' });
      V3.toast((r && r.ok !== false) ? ('🎯 ' + mkt + ' ' + dir + ' pinned') : ('Force Select failed: ' + ((r && r.error) || '')), (r && r.ok !== false) ? 'ok' : 'err');
      pollStatus(); return;
    }
    if (e.target.closest('#v3-manual-disable')) {
      if (!(await V3.confirm('Disable + Close', 'Stop FOCUS + close positions?'))) return;
      const r = await V3.getJSON('/api/strategy/focus/disable?close_position=1', { method: 'POST' });
      V3.toast((r && r.ok !== false) ? '■ FOCUS stopped + closed' : 'Failed', (r && r.ok !== false) ? 'ok' : 'err');
      pollStatus(); return;
    }
    // 📊 Scanner row 📊 → TF Progress center modal (before the v3-scan-me branch: same button has both classes)
    const tfmBtn = e.target.closest('.v3-scan-tfm');
    if (tfmBtn) { showTfModal(tfmBtn.dataset.mkt, tfmBtn.dataset.sig || ''); return; }
    // 📊 Close TF modal (backdrop click / close button)
    if (e.target.id === 'v3-tfm-back' || e.target.closest('#v3-tfm-close')) { const b = $('v3-tfm-back'); if (b) b.classList.remove('show'); return; }
    // 📊 TF modal L/S entry → close modal then same confirm flow (same z-index → close first)
    const tfmGo = e.target.closest('#v3-tfm-long, #v3-tfm-short');
    if (tfmGo) { const b = $('v3-tfm-back'); if (b) b.classList.remove('show'); await scanManualEntry(tfmGo.dataset.mkt, tfmGo.dataset.dir, false); return; }
    // Scanner Manual buttons (L / L⏳ / S / S⏳) → manual entry (gate bypass, direction as-is)
    const me = e.target.closest('.v3-scan-me');
    if (me) { await scanManualEntry(me.dataset.mkt, me.dataset.dir, me.dataset.smart === '1'); return; }
    // 🖐 Manual entry widget (full-width table) — input form version. market=input field / timeout=minutes
    const meGo = e.target.closest('.v3-me-go');
    if (meGo) {
      if (V3.state.active !== 'focus') { V3.toast('Only FOCUS is wired right now (other strategies in Phase 6)', 'warn'); return; }
      const mkt = (($('v3-me-market') && $('v3-me-market').value) || '').trim().toUpperCase();
      if (!mkt) { V3.toast('Please enter a coin (e.g. BTCUSDT)', 'warn'); return; }
      const dir = meGo.dataset.dir, smart = meGo.dataset.smart === '1';
      const tmin = parseInt(($('v3-me-timeout') && $('v3-me-timeout').value) || '60', 10) || 60;
      const badge = '<span class="v3-badge ' + (dir === 'LONG' ? 'long' : 'short') + '">' + dir + '</span>';
      const ok = await V3.confirm('Confirm Manual Entry', '<div style="line-height:1.8"><b>' + mkt + '</b> ' + badge +
        '<br><small>Mode: ' + (smart ? ('wait ' + tmin + 'min for signal') : 'enter immediately') + '</small>' +
        '<br><small style="color:var(--v3-warn)">⚠️ Live trade — gate bypass (safety guards kept) · direction as-is (no auto FLIP).</small></div>');
      if (!ok) return;
      let url = '/api/strategy/focus/manual-entry?market=' + encodeURIComponent(mkt) + '&direction=' + dir;
      if (smart) url += '&wait_for_signal=true&timeout_sec=' + (tmin * 60);
      V3.toast(mkt + ' ' + dir + ' requesting…', 'info');
      const r = await V3.getJSON(url, { method: 'POST' });
      V3.toast((r && r.ok) ? ('✓ ' + mkt + ' ' + dir + (smart ? ' signal-wait registered' : ' entered') + ' done') : ('✗ Entry failed: ' + ((r && r.error) || 'unknown')), (r && r.ok) ? 'ok' : 'err', 6000);
      pollStatus(); return;
    }
    // 🧊 Manual FOCUS COOLDOWN release (v2 Skip button port) → /skip-cooldown POST → re-scan immediately
    if (e.target.closest('.v3-skip-cd')) {
      const r = await V3.getJSON('/api/strategy/focus/skip-cooldown', { method: 'POST' });
      V3.toast((r && r.ok) ? '⏭ FOCUS cooldown released' : ('Failed: ' + ((r && r.error) || 'unknown')), (r && r.ok) ? 'ok' : 'err');
      pollStatus(); return;
    }
    const seg = e.target.closest('.es-seg');
    if (seg) {
      const sw = seg.closest('.v3-eng-sw'); const name = sw && sw.dataset.engineSw;
      if (name !== 'focus' && name !== 'harpoon') { V3.toast('This engine is wired in a later Phase', 'warn'); return; }
      const act = seg.dataset.act, running = sw.classList.contains('on');
      if ((act === 'start' && running) || (act === 'stop' && !running)) return;  // already in that state
      const lbl = LABEL[name] || name;
      if (act === 'stop' && !(await V3.confirm('Stop Engine', 'Stop the ' + lbl + ' engine? (open positions kept)'))) return;
      const r = await V3.getJSON('/api/strategy/' + name + '/' + (act === 'start' ? 'enable' : 'disable'), { method: 'POST' });
      V3.toast((r && r.ok !== false) ? (lbl + ' ' + (act === 'start' ? 'ON' : 'OFF')) : 'Engine switch failed', (r && r.ok !== false) ? 'ok' : 'err');
      pollStatus(); return;
    }
    const one = e.target.closest('.focus-btn-close-one');
    if (one) {
      const mkt = one.dataset.market;
      if (!(await V3.confirm('Close Position', 'Close <b>' + mkt + '</b>?'))) return;
      const r = await V3.getJSON('/api/strategy/focus/close-one?market=' + encodeURIComponent(mkt), { method: 'POST' });
      V3.toast((r && r.ok) ? ('✅ ' + mkt + ' closed') : ('❌ Close failed: ' + ((r && r.error) || '')), (r && r.ok) ? 'ok' : 'err');
      pollStatus(); return;
    }
    const mk = e.target.closest('.v3-mkt');
    if (mk && mk.dataset.bybit) { V3.openBybitTrade(mk.dataset.bybit); return; }
    if (e.target.closest('#focus-btn-close-all')) {
      if (!(await V3.confirm('Close All', 'Close all FOCUS positions?'))) return;
      await V3.getJSON('/api/strategy/focus/close-all', { method: 'POST' });
      V3.toast('🛑 Close-all requested', 'ok'); pollStatus(); return;
    }
    if (e.target.closest('#focus-btn-exit-selected')) {
      const chk = Array.from(document.querySelectorAll('.focus-pos-chk:checked')).map((c) => c.dataset.market);
      if (!chk.length) return;
      if (!(await V3.confirm('Close Selected', 'Close ' + chk.join(', ') + '?'))) return;
      await V3.getJSON('/api/strategy/focus/close-selected?markets=' + encodeURIComponent(chk.join(',')), { method: 'POST' });
      V3.toast('Close-selected requested', 'ok'); pollStatus(); return;
    }
  });
  document.addEventListener('change', (e) => {
    // 📓 Journal filters (strategy All/FOCUS/HARPOON · coin All Coins · Rows) → reset page + re-query
    if (e.target.id === 'v3-journal-filter') { V3.state.journal.strategy = e.target.value; V3.state.journal.page = 1; loadJournal(true); return; }
    if (e.target.id === 'v3-journal-market') { V3.state.journal.market = e.target.value; V3.state.journal.page = 1; loadJournal(true); return; }
    if (e.target.id === 'v3-journal-limit') { V3.state.journal.limit = Math.max(5, Math.min(500, parseInt(e.target.value, 10) || 20)); V3.state.journal.page = 1; loadJournal(true); return; }
    // ⚡ LIGHTNING reco Rows change (page reset) / Budget·TP change → recompute Est. Profit
    if (e.target.id === 'v3-ltg-recorows') { V3.state.lightning.recoRows = parseInt(e.target.value, 10) || 10; V3.state.lightning.recoPage = 1; const e0 = $('v3-ltg-recos'); if (e0) e0.innerHTML = renderRecos(V3.state.lightning.recos); return; }
    if (e.target.id === 'v3-ltg-budget' || e.target.id === 'v3-ltg-tp') { if (V3.updateLtgEst) V3.updateLtgEst(); return; }
    // 🎯 SNIPER reco Rows / Budget·TP → recompute Est
    if (e.target.id === 'v3-snp-recorows') { V3.state.sniper.recoRows = parseInt(e.target.value, 10) || 5; V3.state.sniper.recoPage = 1; const e0 = $('v3-snp-recos'); if (e0) e0.innerHTML = renderSnpRecos(V3.state.sniper.recos); return; }
    if (e.target.id === 'v3-snp-budget' || e.target.id === 'v3-snp-tp') { if (V3.updateSnpEst) V3.updateSnpEst(); return; }
    if (e.target.id === 'v3-krw-rate') { localStorage.setItem('v3_krw_rate', e.target.value); localStorage.setItem('v3_krw_auto', '0'); const _ac = $('v3-krw-auto'); if (_ac) _ac.checked = false; if (V3.loadSettings) V3.loadSettings(); return; }   // 💱 manual input = auto OFF + remember + recompute
    if (e.target.id === 'v3-krw-auto') { localStorage.setItem('v3_krw_auto', e.target.checked ? '1' : '0'); if (e.target.checked && V3.fetchKrwRate) V3.fetchKrwRate(true); else if (V3.loadSettings) V3.loadSettings(); return; }   // 🔄 auto ON=fetch now / OFF=keep manual value
    // 🔌 Generic plugin (GAZUA etc.) Rows / Budget·TP / reco toggle — match PLUG keys only
    { const m = e.target.id && e.target.id.match(/^v3-([a-z]+)-recorows$/); if (m && PLUG[m[1]]) { const st = plugState(m[1]); st.recoRows = parseInt(e.target.value, 10) || 5; st.recoPage = 1; const e0 = $('v3-' + m[1] + '-recos'); if (e0) e0.innerHTML = renderPlugRecos(m[1], st.recos); return; } }
    { const m = e.target.id && e.target.id.match(/^v3-([a-z]+)-(budget|tp)$/); if (m && PLUG[m[1]]) { updatePlugEst(m[1]); return; } }
    { const m = e.target.id && e.target.id.match(/^v3-([a-z]+)-w-recos$/); if (m && PLUG[m[1]]) { plugState(m[1]).showRecos = e.target.checked; renderMain(); return; } }
    { const m = e.target.id && e.target.id.match(/^v3-([a-z]+)-benchmark$/); if (m && PLUG[m[1]]) { plugState(m[1]).benchmark = e.target.value; loadPlugRecos(m[1], true); return; } }   // 🔄 contrarian benchmark change → re-scan
    // 🖐 Manual Control — refresh TF Progress immediately on market change / remember direction
    if (e.target.id === 'v3-manual-market') { V3.state.manual.market = e.target.value.trim().toUpperCase(); loadTfp(true); return; }
    if (e.target.id === 'v3-manual-dir') { V3.state.manual.dir = e.target.value; return; }
    if (e.target.id === 'focus-chk-all') {
      document.querySelectorAll('.focus-pos-chk').forEach((c) => { c.checked = e.target.checked; });
    }
    if (e.target.id === 'focus-chk-all' || (e.target.classList && e.target.classList.contains('focus-pos-chk'))) {
      const btn = $('focus-btn-exit-selected');
      if (btn) btn.disabled = document.querySelectorAll('.focus-pos-chk:checked').length === 0;
    }
  });

  // ── Right-side widget toggles ──
  $('v3-w-summary')?.addEventListener('change', (e) => { V3.state.widgets.summary = e.target.checked; renderMain(); });
  $('v3-w-positions')?.addEventListener('change', (e) => { V3.state.widgets.positions = e.target.checked; renderMain(); });
  $('v3-w-dow')?.addEventListener('change', (e) => { V3.state.widgets.dow = e.target.checked; renderMain(); });
  $('v3-w-slot')?.addEventListener('change', (e) => { V3.state.widgets.slot = e.target.checked; renderMain(); });
  $('v3-w-regime')?.addEventListener('change', (e) => { V3.state.widgets.regime = e.target.checked; renderMain(); });
  $('v3-w-news')?.addEventListener('change', (e) => { V3.state.widgets.news = e.target.checked; renderMain(); });
  $('v3-w-report')?.addEventListener('change', (e) => { V3.state.widgets.report = e.target.checked; renderMain(); });
  $('v3-w-journal')?.addEventListener('change', (e) => { V3.state.widgets.journal = e.target.checked; renderMain(); });
  $('v3-w-scan')?.addEventListener('change', (e) => { V3.state.widgets.scan = e.target.checked; renderMain(); });
  $('v3-w-peer')?.addEventListener('change', (e) => { V3.state.widgets.peer = e.target.checked; renderMain(); if (V3.loadEnabledWidgets) V3.loadEnabledWidgets(); });
  $('v3-w-manual')?.addEventListener('change', (e) => { V3.state.widgets.manual = e.target.checked; renderMain(); });
  $('v3-w-mentry')?.addEventListener('change', (e) => { V3.state.widgets.mentry = e.target.checked; renderMain(); });   // 🖐 manual entry table
  $('v3-w-phasek')?.addEventListener('change', (e) => { V3.state.widgets.phasek = e.target.checked; renderMain(); });
  // 🏠 Home right-side widget toggles (overall status·quick trade·positions·recommendations — pick which tables to show)
  ['status', 'quick', 'positions', 'reco'].forEach((k) => $('v3-hm-' + k)?.addEventListener('change', (e) => { V3.state.home.show[k] = e.target.checked; renderMain(); }));
  // 🐟 HARPOON right-side widget toggles (Stats / Current Scalp / FOCUS Link / Recent Scalps)
  $('v3-hw-stats')?.addEventListener('change', (e) => { V3.state.hpwidgets.stats = e.target.checked; renderMain(); });
  $('v3-hw-scalp')?.addEventListener('change', (e) => { V3.state.hpwidgets.scalp = e.target.checked; renderMain(); });
  $('v3-hw-link')?.addEventListener('change', (e) => { V3.state.hpwidgets.link = e.target.checked; renderMain(); });
  $('v3-hw-history')?.addEventListener('change', (e) => { V3.state.hpwidgets.history = e.target.checked; renderMain(); });
  // 🗂️ Layer persistence [2026-06-19 owner "I want it to come up with the previous settings, but it resets every time?"] — save on toggle change +
  //   on load, sync the static HTML checkbox .checked to the restored state. Shared for v3-w-*·v3-hm-*·v3-hw-*.
  function _saveLayers() {
    try {
      localStorage.setItem('v3-layers', JSON.stringify({
        widgets: V3.state.widgets,
        hpwidgets: V3.state.hpwidgets,
        homeShow: (V3.state.home && V3.state.home.show) || {}
      }));
    } catch (e) { /* noop */ }
  }
  function syncLayerChecks() {
    var w = V3.state.widgets || {};
    Object.keys(w).forEach(function (k) { var c = $('v3-w-' + k); if (c) c.checked = !!w[k]; });
    var hp = V3.state.hpwidgets || {};
    Object.keys(hp).forEach(function (k) { var c = $('v3-hw-' + k); if (c) c.checked = !!hp[k]; });
    var hs = (V3.state.home && V3.state.home.show) || {};
    Object.keys(hs).forEach(function (k) { var c = $('v3-hm-' + k); if (c) c.checked = hs[k] !== false; });
  }
  syncLayerChecks();   // once on load — reflect the restored state (or defaults) into the static HTML checkboxes
  document.addEventListener('change', function (e) {
    var id = e.target && e.target.id;
    if (typeof id === 'string' && (id.indexOf('v3-w-') === 0 || id.indexOf('v3-hw-') === 0 || id.indexOf('v3-hm-') === 0)) _saveLayers();
  });
  // ⚡ LIGHTNING right-side widget toggle (recommended coins show/hide — owner: "use the idle right side · whether to show recommendations")
  $('v3-lw-recos')?.addEventListener('change', (e) => { V3.state.lightning.showRecos = e.target.checked; renderMain(); });
  // 🎯 SNIPER right-side widget toggle (recommended coins show/hide)
  $('v3-sw-recos')?.addEventListener('change', (e) => { V3.state.sniper.showRecos = e.target.checked; renderMain(); });

  // ── E-STOP ──
  $('v3-estop')?.addEventListener('click', async () => {
    const ok = await V3.confirm('🛑 E-STOP',
      '<div style="line-height:1.7"><b style="color:var(--v3-warn)">Immediately halt all trading (Emergency Stop)</b>?<br>' +
      '<small>Stops new entries and auto-trading. Positions are kept.</small></div>');
    if (!ok) return;
    const r = await V3.getJSON('/api/system/emergency/stop?reason=v3_top_estop', { method: 'POST' });
    V3.toast((r && r.ok) ? '🛑 E-STOP triggered' : ('E-STOP failed: ' + ((r && (r.message || r.detail || r.error)) || '')), (r && r.ok) ? 'ok' : 'err', 6000);
    pollStatus();
  });

  // ── Widget order drag reorder (2026-06-07 owner) — drag right-side analytics widget rows to reorder, persisted in localStorage ──
  function setupWidgetReorder() {
    const keyOf = (row) => { const c = row.querySelector('input[type=checkbox]'); return (c && c.id.indexOf('v3-w-') === 0) ? c.id.slice(5) : null; };
    const rebuild = () => {
      const ord = [];
      document.querySelectorAll('.v3-wgroup .v3-wrow').forEach((r) => { const kk = keyOf(r); if (kk && _wreg[kk] && ord.indexOf(kk) < 0) ord.push(kk); });
      V3.state.widgetOrder = ord;
      try { localStorage.setItem('v3-widget-order', JSON.stringify(ord)); } catch (x) { /* noop */ }
      renderMain();
    };
    let dragged = null;
    document.querySelectorAll('.v3-wgroup .v3-wrow').forEach((row) => {
      const k = keyOf(row);
      if (!k || !_wreg[k]) return;   // reorder only _wreg analytics widgets (excludes summary/positions/mentry)
      row.setAttribute('draggable', 'true');
      row.style.cursor = 'grab';
      row.addEventListener('dragstart', (e) => { dragged = row; row.style.opacity = '0.4'; try { e.dataTransfer.effectAllowed = 'move'; } catch (x) { /* noop */ } });
      row.addEventListener('dragend', () => { row.style.opacity = ''; dragged = null; });
      row.addEventListener('dragover', (e) => { e.preventDefault(); });
      row.addEventListener('drop', (e) => {
        e.preventDefault();
        if (!dragged || dragged === row || dragged.parentNode !== row.parentNode) return;   // within the same group only
        row.parentNode.insertBefore(dragged, row);
        rebuild();
      });
    });
  }

  // ── Panel width resize (gutter drag + localStorage) ──
  function setupResize() {
    const body = document.querySelector('.v3-body'); if (!body) return;
    try {
      const s = JSON.parse(localStorage.getItem('v3-panel-w') || '{}');
      if (s.tree) body.style.setProperty('--v3-tree', s.tree + 'px');
      if (s.wpanel) body.style.setProperty('--v3-wpanel', s.wpanel + 'px');
      const wo = JSON.parse(localStorage.getItem('v3-widget-order') || 'null'); if (Array.isArray(wo)) V3.state.widgetOrder = wo;
    } catch (e) { /* noop */ }
    setupWidgetReorder();   // widget order drag
    document.querySelectorAll('.v3-gutter').forEach((g) => {
      g.addEventListener('pointerdown', (e) => {
        e.preventDefault();
        const side = g.dataset.resize, varName = side === 'left' ? '--v3-tree' : '--v3-wpanel';
        const startX = e.clientX;
        const cur = parseInt(getComputedStyle(body).getPropertyValue(varName), 10);
        const startW = isNaN(cur) ? (side === 'left' ? 230 : 214) : cur;
        g.classList.add('dragging');
        const move = (ev) => {
          let w = side === 'left' ? (startW + ev.clientX - startX) : (startW - ev.clientX + startX);
          body.style.setProperty(varName, Math.max(150, Math.min(480, w)) + 'px');
        };
        const up = () => {
          g.classList.remove('dragging');
          document.removeEventListener('pointermove', move);
          document.removeEventListener('pointerup', up);
          const tree = parseInt(getComputedStyle(body).getPropertyValue('--v3-tree'), 10) || 230;
          const wpanel = parseInt(getComputedStyle(body).getPropertyValue('--v3-wpanel'), 10) || 214;
          try { localStorage.setItem('v3-panel-w', JSON.stringify({ tree, wpanel })); } catch (e) { /* noop */ }
        };
        document.addEventListener('pointermove', move);
        document.addEventListener('pointerup', up);
      });
    });
  }

  // ── WS real-time: /ws/state (POSITION_OPEN/CLOSE/CONFIG → refresh immediately) ──
  function connectStateWS() {
    try {
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws = new WebSocket(proto + '//' + location.host + '/ws/state');
      ws.onmessage = () => { pollStatus(); };           // every state event = refresh immediately
      ws.onclose = () => { setTimeout(connectStateWS, 3000); };
      ws.onerror = () => { try { ws.close(); } catch (e) { /* noop */ } };
      V3._stateWS = ws;
    } catch (e) { setTimeout(connectStateWS, 5000); }
  }

  // ── Start ──
  setupResize();
  buildStratRail();      // top strategy rail (breadcrumbs + switching)
  updateTreeUI();
  applyActivePanels();   // initial ribbon/right-side display for the active strategy (=focus)
  renderMain();
  pollStatus();
  connectStateWS();
  setInterval(pollStatus, 8000);   // fallback (WS pushes events instantly, avoid duplicate polling)
  updateSpotLights();              // ★ GAZUA·CONTRARIAN spot exchange lights (replaces the toggle)
  setInterval(updateSpotLights, 15000);   // matches the cross endpoint's 15s cache
  // 🟢 GreenPen Scanner auto-refresh — only while on screen (skip if tab is backgrounded / different view / widget OFF → zero server load)
  async function refreshScanIfVisible() {
    if (document.visibilityState !== 'visible') return;             // skip if the tab is hidden (no background scanning)
    const w = V3.state.widgets || {}; if (!w.scan) return;           // GreenPen widget OFF → skip
    const cfg = _wreg.scan; if (!cfg || !$(cfg.el)) return;          // skip if the widget DOM isn't in the current view
    const c = _wcache.scan, now = Date.now();
    if (c && now - c.t < (cfg.ttl || 120000)) return;               // don't re-scan within the TTL (120s) (saves the heavy scan-list)
    const d = await V3.getJSON(cfg.url, { timeoutMs: cfg.timeoutMs || 8000 });
    if (d && d.ok !== false) _wcache.scan = { data: d, t: now };
    const e1 = $(cfg.el); if (e1) e1.innerHTML = cfg.render(d);
  }
  setInterval(refreshScanIfVisible, 30000);   // 30s check → thanks to the TTL, actual scans run ~every 2 min, only while visible
})();
