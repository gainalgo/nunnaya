/* ============================================================
   dashboard_v3.js — 봇 v4 진입점 (multi-select view + engine buttons + WS real-time)
   좌측 스위치 = 다중 선택(메인 표시) / 엔진 on·off = 거래창 헤더 Engine 버튼 / WS 실시간
   ※ ribbon.js, tab_entry.js 가 window.V3 사용 — ribbon.js 로드 후 이 파일 초기화
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
    ladder: { items: null, t: 0, steps: null, stepsMarket: null, orders: null },   // 📐 steps=계산 plan / orders=실주문(grid_state, uuid)
    pcommon: { data: null, t: 0 },
    home: { pos: null, reco: null, t: 0, show: { status: true, quick: true, positions: true, reco: true } } };   // 🏠 종합 현황 (show=우측 위젯 토글)

  // 🗂️ 레이어(위젯 가시성) 영속 복원 — 화면 이동/재시작 후 직전 설정 유지 [2026-06-19 부모 "매번 리셋"]
  //   widgets(v3-w-*)·hpwidgets(v3-hw-*)·home.show(v3-hm-*) 를 localStorage('v3-layers')에서 복원.
  try {
    var _L = JSON.parse(localStorage.getItem('v3-layers') || 'null');
    if (_L) {
      if (_L.widgets) Object.assign(V3.state.widgets, _L.widgets);
      if (_L.hpwidgets) Object.assign(V3.state.hpwidgets, _L.hpwidgets);
      if (_L.homeShow && V3.state.home && V3.state.home.show) Object.assign(V3.state.home.show, _L.homeShow);
    }
  } catch (_e) { /* noop */ }

  // ── 공유 헬퍼 ──
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
      if (r.status === 401) V3.toast('세션 만료 — 다시 로그인하세요', 'err', 6000);
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
    // ★ [2026-06-23 감사 low] 거래소별 URL — Binance 창(?ex=binance_futures, shim 이 html[data-ex] 세팅)에서
    //   클릭 시 엉뚱하게 bybit.com 이 열려 수동 오발주하던 것 방지.
    const _isBnc = document.documentElement.getAttribute('data-ex') === 'binance_futures';
    const url = _isBnc ? ('https://www.binance.com/en/futures/' + symbol)
                       : ('https://www.bybit.com/trade/usdt/' + symbol);
    try { const w = window.open(url, 'ex_trade_window'); if (w) w.focus(); } catch (e) { /* noop */ }
  };

  const LABEL = { focus: 'FOCUS', harpoon: 'HARPOON', lightning: 'LIGHTNING', sniper: 'SNIPER', gazua: 'GAZUA', contrarian: 'CONTRARIAN', ladder: 'LADDER', pingpong: 'PINGPONG', autoloop: 'AUTOLOOP', whale: 'WHALE', env: 'Settings (Common)' };
  const ICON = { focus: '◎', harpoon: '🐟', lightning: '⚡', sniper: '🎯', gazua: '🚀', contrarian: '🔄', ladder: '📐', pingpong: '🏓', autoloop: '🔁', whale: '🐋' };
  const TREE_ORDER = ['focus', 'harpoon', 'lightning', 'sniper', 'gazua', 'contrarian', 'ladder', 'pingpong', 'autoloop', 'whale'];
  const TIER2 = ['pingpong', 'autoloop', 'whale'];   // autopilot 슬롯형 — 공유 'tier2' 리본/위젯 컨테이너
  // 🎛️ Tier-2 전략별 고유 튜닝 (slot-fill 시 적용 · /api/reserved/plugin-params 저장) [param, 라벨, 기본값]
  const TIER2_TUNE = {
    pingpong: [['rsi_buy', 'RSI 매수', 30], ['rsi_sell', 'RSI 매도', 70], ['pp_tp_pct', 'TP %', 3.0], ['pp_sl_pct', 'SL %', -2.0], ['pp_entry_gap_pct', '진입 갭 %', 0.35]],
    autoloop: [['rsi_buy', 'RSI 매수', 28], ['rsi_sell', 'RSI 매도', 58], ['tp_pct', 'TP %', 2.5], ['sl_pct', 'SL %', -2.5], ['trailing_pct', '트레일 %', 1.2]],
    whale: [['rsi_entry_max', 'RSI 진입상한', 30], ['rsi_exit_min', 'RSI 청산하한', 65], ['cloud_min_thickness_pct', '구름 최소두께 %', 1.5], ['vol_spike_ratio', '거래량 스파이크배수', 2.0], ['vol_lookback', '거래량 lookback', 20], ['tp_pct', 'TP %', 2.0], ['sl_pct', 'SL %', 3.0]],
  };
  // active 전략 → 리본/우측 컨테이너 키 (focus / harpoon / plugin / common)
  function engineOf(name) {
    if (TIER2.includes(name)) return 'tier2';   // 슬롯형 3개 공유 컨테이너
    if (name === 'focus' || name === 'harpoon' || name === 'lightning' || name === 'sniper' || name === 'ladder' || (typeof PLUG !== 'undefined' && PLUG[name])) return name;   // 자체 리본/위젯 컨테이너 보유 (붙인 전략)
    return name === 'env' ? 'common' : 'plugin';
  }
  // active 전략 전환: 상단 리본 + 우측 위젯패널을 그 전략 것으로 fold 전환 (이전 active 는 접힘)
  function applyActivePanels() {
    const key = V3.state.homeView ? 'home' : (V3.state.envView || V3.state.pcommonView) ? 'common' : engineOf(V3.state.active);
    if (V3.ribbonSetActive) V3.ribbonSetActive(key);
    document.querySelectorAll('.v3-wgroup').forEach((g) => {
      const st = g.dataset.wpanelStrat;
      // 'shared'(FOCUS 결과: Trade Journal·Peer Scanner) = home + FOCUS 양쪽 노출. 나머지는 해당 뷰만. (2026-06-07 부모)
      g.classList.toggle('show', st === key || (st === 'shared' && (V3.state.homeView || key === 'focus')));
    });
    if (key === 'harpoon') loadHarpoon(true);   // 리본 채우기 (메인 stack 에 없어도)
    if (key === 'lightning') { loadLightning(true); loadLightningGuards(); loadLightningRecos(true); }
    if (key === 'sniper') { loadSniper(true); loadSniperRecos(true); }
    if (PLUG[key]) { loadPlug(key, true); loadPlugRecos(key, true); }   // generic 플러그인(GAZUA 등)
    if (key === 'ladder') loadLadder(true);   // 📐 읽기 전용
    if (key === 'tier2') { const rb = $('v3-tier2-ribbon-body'); if (rb) rb.innerHTML = tier2RibbonHtml(V3.state.active); loadPluginsCommon(); loadTier2Tune(); loadTier2Work(); loadTier2Reco(V3.state.active); }   // 🤖 리본=active 플러그인 슬롯·튜닝 주입 + 메인 작동현황 + 추천
  }
  V3.applyActivePanels = applyActivePanels;

  function chip(k, v, cls) { return '<div class="v3-chip"><span class="k">' + k + '</span><span class="v ' + (cls || '') + '">' + v + '</span></div>'; }
  function _fp(v) { if (!v || v === 0) return '-'; v = Number(v); return v >= 100 ? '$' + v.toFixed(2) : v >= 1 ? '$' + v.toFixed(4) : '$' + v.toFixed(6); }

  // 요약 = v2 카드(중복 제거: 엔진상태=블록헤더, 총PnL/슬롯=Positions헤더에 이미 있음)
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

  // 📋 Positions 위젯 — v2 거래창 표 그대로 (11컬럼 + Net + 진행바 + ✕/Exit/CloseAll)
  // 포지션 행 공통 셀 (Market·Dir·Margin·Entry·Current·PnL+Net·TP1·SL·Progress·Hold) — FOCUS 표 + 🏠홈 공유
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
      '<td class="v3-neg">' + _fp(p.sl) + (p.breakeven_locked ? ((isLong ? p.sl >= p.entry_price : p.sl <= p.entry_price) ? ' <small style="color:var(--v3-warn)">BE</small>' : ' <small style="color:var(--v3-neg)" title="BE락 플래그는 켜졌는데 SL이 본전 아래 = 가짜 BE(보호 안 됨)">BE✗</small>') : '') + '</td>' +
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

  // ── Phase 3 위젯 (analytics, KST) — 우측 토글로 표시, 짧은 표는 옆으로 나란히 ──
  function _wkv(label, val, cls) { return '<div class="rib-row"><span>' + label + '</span><span class="' + (cls || '') + '">' + val + '</span></div>'; }
  function _dirBadge(x) { const c = x === 'LONG' ? 'long' : x === 'SHORT' ? 'short' : 'mute'; return '<span class="v3-badge ' + c + '">' + (x || '—') + '</span>'; }
  function _grade(g) { const c = (g === 'S' || g === 'A') ? 'v3-pos' : (g === 'D' || g === 'F') ? 'v3-neg' : ''; return '<b class="' + c + '">' + (g || '?') + '</b>'; }
  function _money(v) { v = Number(v) || 0; return '<span class="' + (v >= 0 ? 'v3-pos' : 'v3-neg') + '">' + (v >= 0 ? '+' : '') + '$' + v.toFixed(2) + '</span>'; }

  function renderDow(d) {
    const days = (d && d.days) || [];
    if (!days.length) return '<div class="v3-widget-h">📅 Day of Week <small>(KST)</small></div><div class="v3-placeholder">데이터 없음 (청산 기록 필요)</div>';
    const rows = days.map((x) => '<tr><td>' + x.day + '</td><td class="' + V3.pnlCls(x.pnl) + '">' + (x.pnl >= 0 ? '+' : '') + '$' + x.pnl.toFixed(2) + '</td><td>' + x.trades + '</td><td>' + x.win_rate + '%</td></tr>').join('');
    return '<div class="v3-widget-h">📅 Day of Week <small>(KST · net PnL 순)</small></div>' +
      '<table class="v3-wtable"><thead><tr><th>Day</th><th>PnL</th><th>Trades</th><th>Win%</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  function renderSlot(d) {
    const slots = (d && d.slots) || [];
    if (!slots.length) return '<div class="v3-widget-h">⏱️ 4H Slot <small>(KST)</small></div><div class="v3-placeholder">데이터 없음 (청산 기록 필요)</div>';
    const rows = slots.map((x) => '<tr><td>' + x.slot + '</td><td class="' + V3.pnlCls(x.pnl) + '">' + (x.pnl >= 0 ? '+' : '') + '$' + x.pnl.toFixed(2) + '</td><td>' + x.trades + '</td><td>' + x.win_rate + '%</td></tr>').join('');
    return '<div class="v3-widget-h">⏱️ 4H Slot <small>(KST 07:00 기준)</small></div>' +
      '<table class="v3-wtable"><thead><tr><th>Slot</th><th>PnL</th><th>Trades</th><th>Win%</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  function renderRegime(d) {
    if (!d || !d.ok) return '<div class="v3-widget-h">🔭 Day Direction</div><div class="v3-placeholder">로딩 실패</div>';
    return '<div class="v3-widget-h">🔭 Day Direction <small>(09:00 KST 기준선)</small></div>' +
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
      _wkv('FOCUS 연동', (d && d.config && d.config.focus_enabled) ? 'ON' : 'OFF') +
      (heads || '<div class="v3-wnote" style="opacity:.6">헤드라인 없음</div>');
  }
  function renderReport(d) {
    const rk = (d && d.rankings) || [], coins = (d && d.coins) || {};
    if (!rk.length) return '<div class="v3-widget-h">🏅 Coin Report Card</div><div class="v3-placeholder">데이터 없음</div>';
    const rows = rk.map((r) => {
      const c = coins[r.coin] || {}, tr = c.trades || 0, wr = c.win_rate || 0, wins = Math.round(tr * wr / 100), pf = c.profit_factor;
      const pfStr = (pf == null) ? '-' : (!isFinite(pf) || pf >= 99) ? '∞' : Number(pf).toFixed(1);
      const ap = Number(c.avg_pnl || 0), tp = Number(r.pnl != null ? r.pnl : (c.total_pnl || 0));
      return '<tr><td>' + r.rank + '</td><td>' + String(r.coin || '').replace('USDT', '') + '</td><td>' + _grade(r.grade) + '</td><td>' + Number(r.score || 0).toFixed(1) + '</td>' +
        '<td>' + tr + ' <small style="color:var(--v3-fg-mute)">(' + wins + 'W/' + (tr - wins) + 'L)</small></td><td>' + wr + '%</td>' +
        '<td class="' + V3.pnlCls(tp) + '">' + (tp >= 0 ? '+' : '') + '$' + tp.toFixed(2) + '</td>' +
        '<td class="' + V3.pnlCls(ap) + '">' + (ap >= 0 ? '+' : '') + '$' + ap.toFixed(2) + '</td><td>' + pfStr + '</td></tr>';
    }).join('');
    return '<div class="v3-widget-h">🏅 Coin Report Card <small>(' + (d.total_coins || rk.length) + '개)</small></div>' +
      '<table class="v3-ltable"><thead><tr><th>#</th><th>Coin</th><th>Grade</th><th>Score</th><th>Trades</th><th>Win%</th><th>PnL</th><th>Avg</th><th>PF</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  // ── 📓 Trade Journal — v2 verbatim 포팅 (필터 All/All Coins/Rows + 페이지 + Daily PnL 차트 + 요약 5종 + 12컬럼) ──
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
  // 청산 Reason → 읽기 좋은 라벨 + BE 발동 표시 (부모님 2026-06-08: 트레일링/BE/SL 구분)
  function _journalReason(t) {
    const raw = String(t.exit_reason || '');
    if (!raw || raw === '-') return '-';
    const tip = raw.replace(/"/g, "'");
    // BE(본전) 락 발동 후 청산이면 prefix (muted — 글씨 힘 빼기, 부모님 미감)
    const be = (t.event === 'EXIT' && t.breakeven_locked) ? '<span title="BE(본전) 락 발동 후 청산" style="color:var(--v3-fg-mute)">BE·</span>' : '';
    let lab;
    const _pnl = Number(t.pnl_net || 0);
    if (raw.indexOf('auto_tp_trail') === 0) lab = '🏆 트레일링 거두기';
    else if (raw.indexOf('auto_sl_pct') === 0) lab = '🛑 손실 자동컷';
    // SL 체결 = SL 이 어디 있었는지(손절/본전/이익보호)를 PnL 로 구분
    else if (raw.indexOf('SERVER_SL') >= 0 || raw === 'SL') {
      lab = _pnl > 0.02 ? '🛡️ SL 이익보호' : (_pnl >= -0.05 ? '🛡️ SL 본전' : '🛑 SL 손절');
    }
    else if (raw.indexOf('SERVER_SIDE') >= 0) {
      lab = (_pnl >= 0 ? '🛡️' : '🛑') + ' 거래소 청산' + (_pnl > 0.02 ? '(이익)' : _pnl < -0.05 ? '(손실)' : '(본전)');
    }
    else if (raw.indexOf('manual') === 0) lab = '✋ 수동';
    else if (raw.indexOf('reverse_drift') === 0) lab = '↩️ 역행 컷';
    else if (raw.indexOf('charge') === 0) lab = '⚡ Charge';
    else if (raw.indexOf('erosion') >= 0) lab = '🩹 침식→BE';
    else if (raw.indexOf('morning_shield') === 0) lab = '🌅 아침방패';
    else if (raw.indexOf('event_shield') === 0) lab = '⏰ 이벤트방패';
    else if (raw.indexOf('macro') >= 0) lab = '🧭 레짐역행';
    else if (raw.indexOf('caution') >= 0) lab = '↔️ 횡보 이익확보';
    else if (raw.indexOf('stall') >= 0) lab = '⏸️ BE 정체컷';
    else if (raw.indexOf('5m_emergency') >= 0 || raw.indexOf('5M') >= 0) lab = '🚨 5M 긴급';
    else if (raw.indexOf('take_profit') >= 0 || raw === 'TP' || raw.indexOf('TP_') >= 0) lab = '🎯 TP';
    else lab = raw.split(':')[0].split('(')[0];   // 그 외 = prefix만 (값/숫자 잘라 깔끔)
    return be + '<span title="' + tip + '">' + lab + '</span>';
  }

  function renderJournal() {
    const j = V3.state.journal;
    const tr = j.data || {}, trades = tr.trades || [];
    const sums = (j.summary && j.summary.summary) || {};
    // All 필터면 FOCUS+HARPOON 합산 (v2 refreshJournal combined 로직 그대로)
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
    // 헤더 + 필터 (All / All Coins / Rows)
    const optS = ['', 'FOCUS', 'HARPOON'].map((v) => '<option value="' + v + '"' + (j.strategy === v ? ' selected' : '') + '>' + (v || 'All') + '</option>').join('');
    const optM = '<option value="">All Coins</option>' + (j.markets || []).map((m) => '<option value="' + m + '"' + (j.market === m ? ' selected' : '') + '>' + m.replace('USDT', '') + '</option>').join('');
    let h = '<div class="v3-jhead"><span class="v3-widget-h" style="margin:0">📓 Trade Journal</span></div>';
    // 필터바 = 요약 카드 줄 우측(차트 바로 위, DT 숫자 높이)으로 내림 (부모님 "이질감")
    const filterBar = '<span class="v3-jfilter">' +
      '<button id="v3-daily-refresh" class="v3-btn sm ghost" title="Daily PnL 차트 새로고침">↻ Daily PnL</button>' +
      '<select id="v3-journal-filter" class="v3-mini">' + optS + '</select>' +
      '<select id="v3-journal-market" class="v3-mini">' + optM + '</select>' +
      '<span class="v3-jrows">Rows <input id="v3-journal-limit" class="v3-mini" type="number" min="5" max="500" value="' + j.limit + '" style="width:52px;text-align:right"></span>' +
      '<button id="v3-journal-refresh" class="v3-btn sm ghost" title="새로고침">🔄</button>' +
      '</span>';
    // 요약 카드 5종 — Total PnL / Today PnL / Trades / Win Rate(횟수%+금액%amt) / DT vs No-DT (v2 _renderSummaryCards 그대로)
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
    // Daily PnL History 차트
    const haveSnaps = j.snaps && j.snaps.length;
    h += '<div class="v3-daily-wrap">' +
      '<div id="v3-daily-chart" class="v3-daily-chart"><span class="v3-daily-cap">Daily PnL History</span>' +
      '<div id="v3-daily-ph" class="v3-daily-ph">' + (j.snaps == null ? 'Loading daily data…' : (haveSnaps ? '' : 'No daily data yet')) + '</div>' +
      '<canvas id="v3-daily-canvas" style="display:' + (haveSnaps ? 'block' : 'none') + ';width:100%;height:100%"></canvas></div></div>';
    // 거래 표 (12컬럼, All 이면 Strategy 컬럼 표시)
    const showStrat = !j.strategy;
    if (!trades.length) {
      return h + '<table class="v3-ltable v3-jtable"><thead><tr>' + _jCols(showStrat) + '</tr></thead><tbody><tr><td colspan="' + (showStrat ? 12 : 11) + '" class="v3-jempty">거래 기록 없음</td></tr></tbody></table>' +
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

  // ── 📊 BTC 분석 보조표 (Positions 우측, 보유 코인 자동 순환) — v2 focusScan(20221) 포팅 ──
  function renderGp() {
    const g = V3.state.gp, d = g.data;
    const title = (g.market || '').replace('USDT', '') || 'Analysis';
    let body;
    if (!d || !d.ok) body = '<div class="v3-gp-loading">분석 로딩 중…</div>';
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
      '<span class="v3-gp-actions"><input id="v3-gp-market" class="v3-mini" type="text" value="' + (g.market || '') + '" title="코인 입력 후 🔄" style="width:74px;text-align:left"><button id="v3-gp-refresh" class="v3-btn sm ghost" title="이 코인 분석">🔄</button></span></div>' + body;
  }
  async function loadGp(force) {
    const g = V3.state.gp, now = Date.now();
    if (!force && g.data && now - g.t < 30000) { const e0 = $('v3-wg-gp'); if (e0) e0.innerHTML = renderGp(); return; }
    let market = (force && g.market) ? g.market : null;
    if (!market) {
      const fs = V3.lastStatus || {};
      const held = (fs.positions || (fs.position ? [fs.position] : [])).map((p) => p.market).filter(Boolean);
      if (held.length) { market = held[g.rotateIdx % held.length]; g.rotateIdx++; }   // 보유 코인 자동 순환 (부모님 "랜덤으로 돌아갔다")
      else market = g.market || 'BTCUSDT';
    }
    g.market = market;
    const dd = await V3.getJSON('/api/strategy/focus/analysis/' + market + '?tf=240');
    g.data = dd; g.t = now;
    const e = $('v3-wg-gp'); if (e) e.innerHTML = renderGp();
  }

  // ── 🔭 Regime Transition Watch (Phase K) — v2 renderPhaseKWatch(21696) 포팅, 상단 full-width ──
  function renderPhaseK() {
    const pk = V3.state.phaseK, data = pk.data;
    const upd = pk.t ? new Date(pk.t).toLocaleTimeString('ko-KR', { hour12: false }) : '—';
    let badge = '<span class="v3-badge mute">대기 중</span>', body;
    if (!data || !data.ok) body = '<div class="v3-pk-loading">Phase K 데이터 로딩 중…</div>';
    else {
      const ks = data.k_status || {}, dets = data.recent_detections || [], now = Math.floor(Date.now() / 1000);
      badge = !ks.enabled ? '<span class="v3-badge mute">감지 OFF</span>' : ks.paper_mode ? '<span class="v3-badge" style="color:var(--v3-accent);border-color:var(--v3-accent)">Paper 감지 중</span>' : '<span class="v3-badge warn">Live 감지 중</span>';
      if (!dets.length) {
        const gap = ks.btc_ema_gap_pct, gapThr = ks.ema_gap_threshold_pct || 0.3;
        const gapStr = (gap != null) ? gap.toFixed(2) + '%' : '—';
        const age = ks.btc_regime_age_hours || 0, minAgeMin = ks.min_regime_age_min || 180;
        const ageStr = age >= 1 ? age.toFixed(1) + 'h' : (age * 60).toFixed(0) + 'min';
        const rc = ks.btc_regime === 'BULL' ? 'var(--v3-long)' : ks.btc_regime === 'BEAR' ? 'var(--v3-short)' : 'var(--v3-fg-mute)';
        const gapClose = gap != null && gap < gapThr;
        body = '<div class="v3-pk-empty"><div class="v3-pk-big">⚪ 현재 전환 감지 없음</div>' +
          '<div class="v3-pk-sub">BTC <b style="color:' + rc + '">' + ks.btc_regime + '</b> ' + ageStr + ' 안정 · EMA gap <b>' + gapStr + '</b> ' + (gapClose ? '✓ 근접' : '(&lt; ' + gapThr + '% 대기 중)') + ' · 최소 regime age ' + minAgeMin + '분 ' + (age * 60 > minAgeMin ? '✓' : '대기') + '</div>' +
          '<div class="v3-pk-note">최근 6시간 내 Phase K 감지 없음</div></div>';
      } else {
        body = dets.map((dd) => {
          const ageSec = now - (dd.ts || now);
          const ageStr = ageSec < 60 ? ageSec + '초 전' : ageSec < 3600 ? Math.floor(ageSec / 60) + '분 전' : (ageSec / 3600).toFixed(1) + '시간 전';
          const icon = dd.scanner_dir === 'LONG' ? '🔴' : '🟢';
          const label = dd.scanner_dir === 'LONG' ? '상향 끝나감' : '하향 끝나감';
          const color = dd.scanner_dir === 'LONG' ? 'var(--v3-short)' : 'var(--v3-long)';
          const adxStr = (dd.adx_past && dd.adx_now) ? 'ADX ' + dd.adx_past + '→' + dd.adx_now + ' (' + ((dd.adx_past - dd.adx_now) / dd.adx_past * 100).toFixed(1) + '% 하락)' : '';
          const gapStr = (dd.btc_ema_gap_pct != null) ? 'EMA gap ' + dd.btc_ema_gap_pct.toFixed(2) + '%' : '';
          const count = dd.count_today > 1 ? ' <span class="v3-badge mute">오늘 ' + dd.count_today + '회</span>' : '';
          return '<div class="v3-pk-det"><div class="v3-pk-ic">' + icon + '</div><div class="v3-pk-detbody">' +
            '<div><b style="color:' + color + '">' + dd.market + '</b> <small class="text-muted">' + label + '</small>' + count + '</div>' +
            '<small class="text-muted">' + adxStr + ' · ' + gapStr + ' · conv=' + (dd.conviction || '?') + '</small>' +
            '<div class="v3-pk-note">scanner wants <b>' + dd.scanner_dir + '</b> · ' + ageStr + '</div></div></div>';
        }).join('');
      }
    }
    return '<div class="v3-pk-head"><span class="v3-pk-title">🔭 Regime Transition Watch <small class="text-muted">Phase K · 전환 임박 감지기</small></span>' +
      '<span class="v3-pk-meta">' + badge + ' <small class="text-muted">' + upd + '</small></span></div>' +
      '<div class="v3-pk-body">' + body + '</div>' +
      '<div class="v3-pk-foot"><small class="text-muted">─── 실험적 신호 · 진입 권고 아님 · 1주 paper 후 정확도 공개 ───</small></div>';
  }
  async function loadPhaseK(force) {
    const pk = V3.state.phaseK, now = Date.now();
    if (!force && pk.data && now - pk.t < 60000) return;
    const dd = await V3.getJSON('/api/strategy/focus/phase-k/recent?hours=6');
    pk.data = dd; pk.t = now;
    const e = $('v3-phasek'); if (e) e.innerHTML = renderPhaseK();
  }

  // 📊 TF Progress 표 (7 TF 봉흐름) — 사이드 위젯(renderManual)·중앙 모달(showTfModal) 공용. v2 _tfp_render 로직
  function _tfpTable(tfp) {
    return '<table class="v3-tfp-table"><thead><tr><th>TF</th><th>진행(시간)</th><th style="text-align:right">시가→현재</th><th style="text-align:right" title="윗꼬리%">↑W</th><th style="text-align:right" title="아랫꼬리%">↓W</th></tr></thead><tbody>' +
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
  // 📊 Scanner 행 📊 클릭 → 중앙 모달 (L/S 결정 *전* 미리보기, v2 _focusShowTfModal 포팅). 데이터=동일 /tf-progress
  async function showTfModal(market, sig) {
    const back = $('v3-tfm-back'); if (!back || !market) return;
    const m = String(market).toUpperCase().replace(/[^A-Z0-9]/g, '');
    const mEl = $('v3-tfm-market'); if (mEl) mEl.textContent = m.replace('USDT', '');
    const lb = $('v3-tfm-long'), sb = $('v3-tfm-short');   // L/S 진입 버튼에 이 코인 주입
    if (lb) lb.dataset.mkt = m;
    if (sb) sb.dataset.mkt = m;
    const cons = $('v3-tfm-consensus'); if (cons) { cons.textContent = '합의: 조회중…'; cons.style.color = 'var(--v3-fg-dim)'; }
    const sum = $('v3-tfm-summary'); if (sum) sum.innerHTML = '';
    const rows = $('v3-tfm-rows'); if (rows) rows.innerHTML = '<div class="text-muted" style="padding:14px;text-align:center">조회중…</div>';
    const ts = $('v3-tfm-ts'); if (ts) ts.textContent = '';
    back.classList.add('show');
    try {
      const d = await V3.getJSON('/api/strategy/focus/tf-progress?market=' + encodeURIComponent(m));
      renderTfModal(d, sig);
    } catch (e) {
      if (rows) rows.innerHTML = '<div class="v3-neg" style="padding:12px">조회 실패: ' + (e.message || e) + '</div>';
    }
  }
  function renderTfModal(d, sig) {
    const rows = $('v3-tfm-rows'), cons = $('v3-tfm-consensus'), sum = $('v3-tfm-summary'), ts = $('v3-tfm-ts');
    if (!rows) return;
    if (!d || !d.ok) {
      rows.innerHTML = '<div class="v3-neg" style="padding:12px">조회 실패: ' + ((d && d.error) || '?') + '</div>';
      if (cons) { cons.textContent = '합의: 실패'; cons.style.color = 'var(--v3-short)'; }
      return;
    }
    if (cons) {
      const c = d.consensus || '-';
      cons.textContent = '합의: ' + c;
      cons.style.color = c.indexOf('BULL') >= 0 ? 'var(--v3-long)' : c.indexOf('BEAR') >= 0 ? 'var(--v3-short)' : 'var(--v3-fg-dim)';
    }
    if (sum) {
      const cp = d.current_price || 0;
      const cpStr = cp >= 100 ? cp.toFixed(2) : cp >= 1 ? cp.toFixed(4) : cp.toFixed(6);
      sum.innerHTML = '<span class="v3-pos">▲' + (d.n_bull || 0) + '</span> <span class="v3-neg">▼' + (d.n_bear || 0) + '</span> <span class="text-muted">·' + (d.n_flat || 0) + '</span>' +
        '<span class="text-muted" style="margin-left:12px">현재가 $' + cpStr + '</span>' +
        (sig ? '<span class="text-muted" style="margin-left:12px">봇: ' + sig + '</span>' : '');
    }
    if (ts) { try { ts.textContent = '@ ' + new Date(d.ts || Date.now()).toTimeString().slice(0, 8); } catch (e) { /* noop */ } }
    rows.innerHTML = _tfpTable(d);
  }
  // 수동 진입 (Scanner 행 L/L⏳/S/S⏳ · TF 모달 L/S 공용) — confirm → manual-entry POST. 방향 그대로(자동 FLIP 없음)
  async function scanManualEntry(mkt, dir, smart) {
    if (!mkt || !dir) return;
    const badge = '<span class="v3-badge ' + (dir === 'LONG' ? 'long' : 'short') + '">' + dir + '</span>';
    const ok = await V3.confirm('수동 진입', '<div style="line-height:1.8"><b>' + mkt + '</b> ' + badge +
      '<br><small>' + (smart ? '신호 확인 후 진입 (1h 대기)' : '즉시 진입') + '</small>' +
      '<br><small style="color:var(--v3-warn)">⚠️ 실거래 — 게이트 우회(안전가드 유지)·방향 그대로(자동 FLIP 없음)</small></div>');
    if (!ok) return;
    let url = '/api/strategy/focus/manual-entry?market=' + encodeURIComponent(mkt) + '&direction=' + dir;
    if (smart) url += '&wait_for_signal=true&timeout_sec=3600';
    V3.toast(mkt + ' ' + dir + ' 요청…', 'info');
    const r = await V3.getJSON(url, { method: 'POST' });
    V3.toast((r && r.ok) ? ('✓ ' + mkt + ' ' + dir + (smart ? ' 신호대기 등록' : ' 진입')) : ('✗ 실패: ' + ((r && r.error) || '알 수 없음')), (r && r.ok) ? 'ok' : 'err', 5000);
    pollStatus();
  }
  // ── 🖐 Manual Control — v2 포팅 (Force Select / TF Progress 봉흐름 / Pin·Selected·H1 SIG / Recent Skips). 금 거래 등 수동 점검용 ──
  function renderManual() {
    const m = V3.state.manual, st = V3.lastStatus || {};
    const lockMarket = (st.config && st.config.lock_market) || st.lock_market || '';
    const pin = lockMarket ? '<span class="v3-badge warn">📌 ' + lockMarket + '</span>' : '<span class="text-muted">Auto Scan</span>';
    const sel = st.selected_market || '-';
    const sig = st.primary_sig ? (st.primary_sig.pattern + ' ' + st.primary_sig.direction) : '-';
    // 📊 TF Progress (진행 중 봉 흐름) — v2 _tfp_render 포팅
    const tfp = m.tfp;
    let tfpHead = '<span class="text-muted">대기 중…</span>', tfpRows = '';
    if (tfp && tfp.ok) {
      const c = tfp.consensus || '-';
      const ccol = c.indexOf('BULL') >= 0 ? 'var(--v3-long)' : c.indexOf('BEAR') >= 0 ? 'var(--v3-short)' : 'var(--v3-fg-dim)';
      let ts = '';
      try { ts = '@ ' + new Date(tfp.ts || Date.now()).toTimeString().slice(0, 8); } catch (e) { /* noop */ }
      tfpHead = '합의: <b style="color:' + ccol + '">' + c + ' (' + (tfp.market || '?') + ')</b> ' +
        '<span class="v3-pos">▲' + (tfp.n_bull || 0) + '</span> <span class="v3-neg">▼' + (tfp.n_bear || 0) + '</span> <span class="text-muted">·' + (tfp.n_flat || 0) + '</span> <small class="text-muted">' + ts + '</small>';
      tfpRows = _tfpTable(tfp);
    } else if (tfp && !tfp.ok) {
      tfpHead = '<span class="text-muted">조회 실패: ' + (tfp.error || '?') + '</span>';
    }
    // 🚫 Recent Skips (신호 떴는데 진입 안 된 사유) — v2 포팅
    const skips = st.recent_skips || [];
    let skipsHtml;
    if (!skips.length) skipsHtml = '<span class="text-muted">최근 skip 없음</span>';
    else {
      const _t = (ts) => { try { return new Date(ts * 1000).toTimeString().slice(0, 8); } catch (e) { return '-'; } };
      const _dc = (x) => x === 'LONG' ? 'var(--v3-long)' : x === 'SHORT' ? 'var(--v3-short)' : 'var(--v3-fg-mute)';
      const _cc = (x) => x >= 75 ? 'var(--v3-long)' : x >= 50 ? 'var(--v3-warn)' : 'var(--v3-short)';
      skipsHtml = '<table class="v3-skips-table"><thead><tr><th>시각</th><th>코인</th><th style="text-align:center">방향</th><th style="text-align:center">conv</th><th>PA</th><th>사유</th></tr></thead><tbody>' +
        skips.slice(0, 20).map((s) => '<tr>' +
          '<td class="text-muted">' + _t(s.ts || 0) + '</td>' +
          '<td><b class="v3-mkt" data-bybit="' + (s.market || '') + '">' + (s.market || '').replace('USDT', '') + '</b></td>' +
          '<td style="text-align:center;color:' + _dc(s.direction) + '">' + (s.direction || '-').slice(0, 1) + '</td>' +
          '<td style="text-align:center;color:' + _cc(s.conviction || 0) + '">' + (s.conviction || 0) + '</td>' +
          '<td style="color:#9cf">' + (s.pa || '-') + '</td>' +
          '<td style="color:#fa8">' + (s.reason || '-') + '</td></tr>').join('') + '</tbody></table>';
    }
    // 📊 GateLedger — "오늘 왜 침묵했나"(게이트별 pass/reject). gate_ledger_enabled ON 일 때만 st.gate_stats 채워짐.
    const gstats = st.gate_stats;
    let gateHtml;
    if (!gstats || !gstats.gates || !Object.keys(gstats.gates).length) {
      gateHtml = '<span class="text-muted">' + (gstats ? '아직 집계 없음' : '게이트 집계 OFF (설정에서 켜기)') + '</span>';
    } else {
      const rows = Object.keys(gstats.gates).map((g) => { const v = gstats.gates[g]; return { g: g, pass: v.pass || 0, reject: v.reject || 0, mk: (v.top_markets || []).join(',') }; });
      rows.sort((a, b) => b.reject - a.reject);   // 병목(reject 많은 순)
      gateHtml = '<table class="v3-skips-table"><thead><tr><th>게이트</th><th style="text-align:center">pass</th><th style="text-align:center">reject</th><th>top</th></tr></thead><tbody>' +
        rows.slice(0, 15).map((r) => '<tr>' +
          '<td style="color:#fa8">' + r.g + '</td>' +
          '<td style="text-align:center;color:var(--v3-long)">' + r.pass + '</td>' +
          '<td style="text-align:center;color:var(--v3-short)">' + r.reject + '</td>' +
          '<td class="text-muted" style="font-size:11px">' + (r.mk || '-').replace(/USDT/g, '') + '</td></tr>').join('') +
        '</tbody></table><div class="v3-wnote">' + (gstats.date || '') + ' · 총 ' + (gstats.total_scanned || 0) + '건 평가</div>';
    }
    return '<div class="v3-widget-h">🖐 Manual Control <small>(금 거래 등 수동 점검)</small></div>' +
      '<div class="v3-manual">' +
      '<div class="v3-manual-form">' +
      '<div><label class="v3-label">Market</label><input id="v3-manual-market" class="v3-input" type="text" value="' + (m.market || 'XAUTUSDT') + '" placeholder="XAUTUSDT"></div>' +
      '<div><label class="v3-label">Direction</label><select id="v3-manual-dir" class="v3-input"><option value="LONG"' + (m.dir === 'SHORT' ? '' : ' selected') + '>LONG</option><option value="SHORT"' + (m.dir === 'SHORT' ? ' selected' : '') + '>SHORT</option></select></div>' +
      '</div>' +
      '<button id="v3-manual-force" class="v3-btn v3-btn-long" style="width:100%;margin-top:8px">🎯 Force Select</button>' +
      '<div class="v3-wnote">Scan List에서 📌 클릭으로 코인 고정/해제</div>' +
      '<button id="v3-manual-disable" class="v3-btn v3-btn-outline-danger" style="width:100%;margin-top:4px">■ Disable + Close</button>' +
      '<hr class="v3-manual-hr">' +
      '<div class="v3-manual-sec"><b>📊 TF Progress</b> <small class="text-muted">— 진행 중 봉 흐름 (수동 참고)</small></div>' +
      '<div class="v3-tfp-head">' + tfpHead + '</div>' +
      '<div class="v3-tfp-rows">' + tfpRows + '</div>' +
      '<div class="v3-wnote">※ 봉 진행률(시간)·시가→현재가 ±%·윗꼬리/아랫꼬리. 5초마다 갱신.</div>' +
      '<hr class="v3-manual-hr">' +
      '<div class="v3-manual-kv"><b>📌 Pin:</b> ' + pin + '</div>' +
      '<div class="v3-manual-kv"><b>Selected:</b> <span class="text-muted">' + sel + '</span></div>' +
      '<div class="v3-manual-kv"><b>H1 SIG:</b> <span class="text-muted">' + sig + '</span></div>' +
      '<div class="v3-manual-kv" style="margin-top:6px"><b>🚫 Recent Skips:</b></div>' +
      '<div class="v3-skips">' + skipsHtml + '</div>' +
      '<div class="v3-manual-kv" style="margin-top:6px"><b>📊 왜 침묵했나 (GateLedger):</b></div>' +
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
  // v2 _refreshFocusScanList(dashboard_v2.js:22244) verbatim 포팅 — 가격/경고/바/배지/penalty/Manual
  function renderScan(d) {
    const list = (d && (d.items || d.results || d.scan_list || d.list)) || [];
    if (!list.length) return '<div class="v3-widget-h">🟢 GreenPen Scanner</div><div class="v3-placeholder">스캔 데이터 없음 (Refresh 또는 켤 때 자동 · 수 초)</div>';
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
      const totalBadge = '<span class="badge" style="background:' + tBg + ';color:' + tClr + ';font-size:0.95rem;padding:5px 9px;font-weight:700;" title="Total = Base + Deduction. ≥ threshold(' + (item.guard_threshold || 65) + ') 통과 시 진입">' + totalDisp + '</span>';
      const penalty = item.guard_breakdown || item.status || '-';
      let botOp = '';
      if (item.bot_opinion && item.bot_opinion.text) {
        const lvl = item.bot_opinion.level || 'info';
        const bg = lvl === 'warn' ? 'rgba(239,83,80,0.18)' : 'rgba(255,193,7,0.15)', clr = lvl === 'warn' ? '#ef5350' : '#f9a825';
        botOp = '<br><span style="display:inline-block;margin-top:2px;padding:1px 6px;font-size:0.7rem;background:' + bg + ';color:' + clr + ';border:1px solid ' + clr + ';border-radius:3px;font-weight:600;">' + item.bot_opinion.text + '</span>';
      }
      const mk = item.market || '', chg = Number(item.change_pct || 0);
      const meBtns =
        '<button class="v3-scan-me v3-scan-tfm" data-mkt="' + mk + '" data-sig="' + (item.signal || '') + '" title="📊 TF Progress 미리보기 — 7 TF (D/H4/H1/30M/15M/5M/3M) 봉흐름. L/S 결정 전 한눈에">📊</button>' +
        '<button class="v3-scan-me" data-mkt="' + mk + '" data-dir="LONG" title="수동 강제 LONG (게이트 우회)">L</button>' +
        '<button class="v3-scan-me" data-mkt="' + mk + '" data-dir="LONG" data-smart="1" title="Smart LONG (신호 확인 후 진입, 1h 대기)">L⏳</button>' +
        '<button class="v3-scan-me s" data-mkt="' + mk + '" data-dir="SHORT" title="수동 강제 SHORT (게이트 우회)">S</button>' +
        '<button class="v3-scan-me s" data-mkt="' + mk + '" data-dir="SHORT" data-smart="1" title="Smart SHORT (신호 확인 후 진입, 1h 대기)">S⏳</button>';
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
    if (!d || !d.servers) return '<div class="v3-widget-h">🛰️ Peer Brief Scanner</div><div class="v3-placeholder">' + ((d && d.note) ? String(d.note) : '데이터 없음') + '</div>';
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
    var srvCell = function (srv) { return esc(srv); };   // 서버명 = 평문 (부모님: 위 탭으로 확인, 네비 불필요 2026-06-07)
    var coinCell = function (sym) {   // 코인 → Bybit 차트 (bybit_trade_window 재사용, 기존 v3-mkt 핸들러)
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
      if (num(n.age, 0) < min) return '<span class="text-muted">관찰중</span>';
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
      if (!label) label = v === 'good_block' ? '좋은 차단' : v === 'missed_entry' ? '아쉬운 차단' : v === 'neutral' ? '중립' : v === 'watching' ? '관찰중' : '판정대기';
      var cls = v === 'good_block' ? 'long' : v === 'missed_entry' ? 'short' : v === 'neutral' ? 'warn' : 'mute';
      return '<span class="v3-badge ' + cls + '">' + esc(label) + '</span>';
    };
    // ① 서버 상태 띠
    var strip = servers.map(function (s) {
      var ok = s.stale ? '🔴' : '🟢';
      var age = s.self ? '나' : (s.ok_age_sec >= 0 ? s.ok_age_sec + 's' : '-');
      return '<span style="font-size:11px;padding:2px 7px;background:var(--v3-bg,#1a1a2e);border:1px solid var(--v3-bd,#334);border-radius:4px;white-space:nowrap">' +
        ok + ' ' + srvCell(s.server_id || '?', s.url) + ' · ' + age + ' · pos ' + ((s.positions || []).length) +
        ' · SL ' + ((s.losses || []).length) + ' · WIN ' + ((s.wins || []).length) + '</span>';
    }).join('');
    // ② 차단 사후평가 (near-miss → 이후 가격 확인)
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
        '<td>' + verdictBadge(n) + '</td><td><small>' + esc(n.gate || n.reason) + '</small></td><td>' + num(n.age, 0).toFixed(0) + '분전</td></tr>';
    }).join('') || '<tr><td colspan="13" class="text-muted">차단 사후판정 대기 중 (near-miss 없음)</td></tr>';
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
      if (!keys.length) return '<tr><td colspan="6" class="text-muted">집계 대기</td></tr>';
      return keys.map(function (k) {
        var c = stats[k], judged = c.good + c.missed + c.neutral;
        var missRate = judged ? (c.missed / judged * 100) : 0;
        var note = judged < 3 ? '관찰중' : missRate >= 50 ? '보수 과함 의심' : (c.good / judged * 100) >= 70 ? '차단 양호' : '혼합';
        var cls = note === '보수 과함 의심' ? S : note === '차단 양호' ? L : 'color:#ffc107';
        return '<tr><td><small>' + esc(k) + '</small></td><td>' + c.total + '</td><td style="' + L + '">' + c.good + '</td><td style="' + S + '">' + c.missed +
          '</td><td>' + missRate.toFixed(0) + '%</td><td style="' + cls + '"><small>' + nameTitle + ' ' + note + '</small></td></tr>';
      }).join('');
    };
    var gateRows = statRows(gateStats, '게이트');
    var srvRows = statRows(srvStats, '서버');
    // ③ 함대 보유 포지션 + flags
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
      if (dirs.indexOf(opp) >= 0) fl.push('<span title="자가충돌" style="color:#ff9800">⚔️</span>');
      if (dirs.filter(function (x) { return x === p.direction; }).length >= 2) fl.push('<span title="겹침">🔁</span>');
      if (Number(p.peak) <= 0.1) fl.push('<span title="헤맴">🐢</span>');
      return fl.join(' ');
    };
    allPos.sort(function (a, b) { return (flagsFor(b) ? 1 : 0) - (flagsFor(a) ? 1 : 0); });
    var posRows = allPos.map(function (p) {
      return '<tr><td>' + srvCell(p.srv, p.url) + '</td><td>' + coinCell(p.symbol) + '</td><td style="' + dcol(p.direction) + '">' + esc(p.direction) +
        '</td><td>' + Number(p.age_min).toFixed(0) + '분</td><td style="' + (Number(p.peak) <= 0.1 ? S : '') + '">' + sg(p.peak) + '%</td>' +
        '<td style="' + (Number(p.pnl) >= 0 ? L : S) + '">' + sg(p.pnl) + '% <small style="opacity:.7">(' + sg(p.usdt) + ')</small></td><td>' + flagsFor(p) + '</td></tr>';
    }).join('') || '<tr><td colspan="7" class="text-muted">함대 보유 없음</td></tr>';
    // ④ near-miss 원본 (점수 합격인데 막힘)
    var nmRows = allNm.slice(0, 12).map(function (n) {
      return '<tr><td>' + srvCell(n.srv, n.url) + '</td><td>' + coinCell(n.symbol) + '</td><td style="' + dcol(n.direction) + '">' + esc(n.direction) +
        '</td><td>' + num(n.score, 0).toFixed(0) + '✓</td><td><small>' + esc(n.reason) + '</small></td><td>' + num(n.age, 0).toFixed(0) + '분전</td></tr>';
    }).join('') || '<tr><td colspan="6" class="text-muted">없음 (점수 합격 후 막힌 진입 없음)</td></tr>';
    // ⑤ 최근 청산
    var allEx = [];
    servers.forEach(function (s) {
      (s.losses || []).forEach(function (x) { allEx.push({ srv: s.server_id, url: s.url, res: 'SL', symbol: x.symbol, direction: x.direction, pnl: x.pnl_net, age: x.age_min }); });
      (s.wins || []).forEach(function (x) { allEx.push({ srv: s.server_id, url: s.url, res: 'WIN', symbol: x.symbol, direction: x.direction, pnl: x.pnl_net, age: x.age_min }); });
    });
    allEx.sort(function (a, b) { return (a.age || 0) - (b.age || 0); });
    var exRows = allEx.slice(0, 12).map(function (x) {
      var rc = x.res === 'WIN' ? L : S;
      return '<tr><td>' + srvCell(x.srv, x.url) + '</td><td>' + coinCell(x.symbol) + '</td><td style="' + dcol(x.direction) + '">' + esc(x.direction) +
        '</td><td style="' + rc + '">' + x.res + '</td><td style="' + rc + '">' + sg(x.pnl) + '</td><td>' + Number(x.age).toFixed(0) + '분전</td></tr>';
    }).join('') || '<tr><td colspan="6" class="text-muted">최근 청산 없음</td></tr>';
    var sub = function (t) { return '<div style="margin-top:8px;margin-bottom:2px;font-size:12px;color:var(--v3-accent,#6cf)">' + t + '</div>'; };
    return '<div class="v3-widget-h">🛰️ Peer Brief Scanner <small>(SL창 ' + esc(d.sl_window_min) + '분 / WIN창 ' + esc(d.peer_win_window_min) + '분 · 자가충돌감점 ' + esc(d.peer_conflict_penalty) + ')</small></div>' +
      '<div style="display:flex;flex-wrap:wrap;gap:6px;margin:4px 0">' + strip + '</div>' +
      sub('🧭 차단 판단 관제 — 방향수익 +면 놓친 자리, 0/음수면 좋은 차단') +
      '<table class="v3-ltable"><thead><tr><th>서버</th><th>코인</th><th>방향</th><th>점수</th><th>차단가</th><th>현재</th><th>5m</th><th>15m</th><th>30m</th><th>60m</th><th>판정</th><th>게이트</th><th>경과</th></tr></thead><tbody>' + auditRows + '</tbody></table>' +
      '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:10px">' +
      '<div>' + sub('게이트별 차단 품질') + '<table class="v3-ltable"><thead><tr><th>게이트</th><th>총</th><th>좋음</th><th>아쉬움</th><th>아쉬움%</th><th>판단</th></tr></thead><tbody>' + gateRows + '</tbody></table></div>' +
      '<div>' + sub('서버별 보수성 비교') + '<table class="v3-ltable"><thead><tr><th>서버</th><th>총</th><th>좋음</th><th>아쉬움</th><th>아쉬움%</th><th>판단</th></tr></thead><tbody>' + srvRows + '</tbody></table></div>' +
      '</div>' +
      sub('⚔️자가충돌 🔁겹침 🐢헤맴 — 함대 보유 포지션') +
      '<table class="v3-ltable"><thead><tr><th>서버</th><th>코인</th><th>방향</th><th>보유</th><th>peak</th><th>현재손익 %(USDT)</th><th>플래그</th></tr></thead><tbody>' + posRows + '</tbody></table>' +
      sub('🚧 점수 합격인데 진입 막힘 (near-miss)') +
      '<table class="v3-ltable"><thead><tr><th>서버</th><th>코인</th><th>방향</th><th>점수</th><th>막은 게이트</th><th>경과</th></tr></thead><tbody>' + nmRows + '</tbody></table>' +
      sub('📋 최근 옆 청산 (conviction 가감 근거)') +
      '<table class="v3-ltable"><thead><tr><th>서버</th><th>코인</th><th>방향</th><th>결과</th><th>손익</th><th>경과</th></tr></thead><tbody>' + exRows + '</tbody></table>';
  }
  const _wreg = {
    dow: { url: '/api/strategy/focus/analytics/by-dow', render: renderDow, el: 'v3-wg-dow', ttl: 30000, loading: '📅 Day of Week 로딩…' },
    slot: { url: '/api/strategy/focus/analytics/by-slot', render: renderSlot, el: 'v3-wg-slot', ttl: 30000, loading: '⏱️ 4H Slot 로딩…' },
    regime: { url: '/api/strategy/focus/day-direction', render: renderRegime, el: 'v3-wg-regime', ttl: 30000, loading: '🔭 Day Direction 로딩…' },
    news: { url: '/api/news-sentiment/status', render: renderNews, el: 'v3-wg-news', ttl: 30000, loading: '📰 News 로딩…' },
    report: { url: '/api/strategy/focus/coin-grades', render: renderReport, el: 'v3-wg-report', ttl: 60000, loading: '🏅 Report Card 로딩…', wide: true },
    journal: { url: '/api/strategy/focus/journal?limit=25&include_blocked=false', render: renderJournal, el: 'v3-wg-journal', ttl: 30000, loading: '📓 Journal 로딩…', wide: true },
    scan: { url: '/api/strategy/focus/scan-list?top_n=10', render: renderScan, el: 'v3-wg-scan', ttl: 120000, timeoutMs: 60000, loading: '🟢 GreenPen 스캔 중… (최초·재시작 후 수십 초 걸릴 수 있음)', wide: true },
    peer: { url: '/api/strategy/focus/peer-cache', render: renderPeerScan, el: 'v3-wg-peer', ttl: 20000, loading: '🛰️ 옆 서버 현황 로딩…', wide: true },
    manual: { render: renderManual, el: 'v3-wg-manual' },   // 🖐 Manual Control (자체 로더 loadTfp + status 데이터)
  };
  const _wcache = {};
  V3._wcache = _wcache; V3._wreg = _wreg;
  async function loadEnabledWidgets() {
    if (V3._widgetsLoading) return;
    V3._widgetsLoading = true;
    try {
    const w = V3.state.widgets || {};
    if (w.phasek) loadPhaseK();                // 🔭 Regime Transition Watch (우측 토글)
    if (w.positions !== false) loadGp();       // 📊 BTC 보조표 (보유 코인 순환)
    if (w.journal) loadJournal();              // 📓 Journal (자체 로더 — 필터/페이지/차트)
    if (w.manual) loadTfp();                   // 🖐 Manual Control — TF Progress 5s polling
    for (const key of Object.keys(_wreg)) {
      if (!w[key] || key === 'journal' || key === 'manual') continue;
      const cfg = _wreg[key]; if (!$(cfg.el)) continue;
      const c = _wcache[key], now = Date.now();
      if (c && now - c.t < (cfg.ttl || 30000)) { const e0 = $(cfg.el); if (e0) e0.innerHTML = cfg.render(c.data); continue; }
      const d = await V3.getJSON(cfg.url, { timeoutMs: cfg.timeoutMs || 8000 });
      if (d && d.ok !== false) _wcache[key] = { data: d, t: now };   // 실패·timeout(null/ok:false)은 캐시 안 함 → TTL 동안 빈 화면 고착 방지, 다음 렌더에 재시도
      const e1 = $(cfg.el); if (e1) e1.innerHTML = cfg.render(d);
    }
    } finally {
      V3._widgetsLoading = false;
    }
  }
  V3.loadEnabledWidgets = loadEnabledWidgets;

  // 전략 블록 헤더 (이름·상태 + Engine Start/Stop 버튼)
  function engineHeader(name, d) {
    if (name === 'focus' || name === 'harpoon') {
      const ready = d && d.ok;
      const on = ready && d.enabled;
      const st = ready ? ((d.enabled ? 'ON' : 'OFF') + (d.state ? ' · ' + d.state : '')) : '…';
      // 🧊 COOLDOWN 수동 해제 — v2 focus-skip-cooldown-btn 포팅. FOCUS COOLDOWN 일 때만 노출
      const skipCd = (name === 'focus' && ready && d.state === 'COOLDOWN')
        ? ' <button class="v3-btn sm v3-skip-cd" style="border-color:#f9a825;color:#f9a825" title="쿨다운 건너뛰기 — 수동으로 COOLDOWN 즉시 해제 (v2 Skip 버튼)">⏭ Skip Cooldown</button>' : '';
      // 🏆 승자거두기 (Auto Take-Profit) 작동중 표시 — auto_tp_enabled ON 일 때 거둘 금액까지
      const _cfg = (d && d.config) || {};
      const atpBadge = (name === 'focus' && _cfg.auto_tp_enabled)
        ? ' <span style="font-size:11px;font-weight:400;color:var(--v3-fg-mute);white-space:nowrap" title="트레일링 거두기 — 순익 $' + (Number(_cfg.auto_tp_usdt) || 0).toFixed(2) + ' 넘으면 무장 → 최고점에서 ' + Math.round((Number(_cfg.auto_tp_peak_giveback_pct) || 0.4) * 100) + '% 반납 시 거둠 (무장 후 견딤보다 우선)' + (_cfg.auto_sl_pct_enabled ? ' · 손실컷 ' + (Number(_cfg.auto_sl_pct) || 0) + '% ON' : '') + '">🏆 트레일링 거두기 (무장 $' + (Number(_cfg.auto_tp_usdt) || 0).toFixed(1) + ')</span>'
        : '';
      return '<div class="v3-block-head"><span class="v3-block-title">' + (ICON[name] || '') + ' ' + (LABEL[name] || name) + ' <small class="' + (on ? 'v3-pos' : 'v3-neg') + '">' + st + '</small>' + skipCd + atpBadge + '</span>' +
        '<span class="v3-eng-sw' + (on ? ' on' : '') + '" data-engine-sw="' + name + '" title="켜진 쪽 = 현재 상태 · 반대쪽 클릭해 전환">' +
        '<button class="es-seg es-start" data-act="start">▶ Start</button>' +
        '<button class="es-seg es-stop" data-act="stop">■ Stop</button></span></div>';
    }
    return '<div class="v3-block-head"><span class="v3-block-title">' + (ICON[name] || '') + ' ' + (LABEL[name] || name) + '</span>' +
      '<span class="v3-eng-phase">Engine — Phase 6</span></div>';
  }

  // 🖐 수동 진입 — 가로 100% 표 (FOCUS 진입 실행. 설정 리본에서 분리 → 우측 토글로 본문 표시). 게이트 우회·방향 그대로.
  function manualEntryHtml() {
    return '<div class="v3-widget-h">🖐 Manual Entry <small class="text-muted">(FOCUS · 게이트 우회 · 방향 그대로 · 실거래)</small></div>' +
      '<table class="v3-postable" style="width:100%"><tbody><tr>' +
      '<td style="text-align:left;width:36%"><input id="v3-me-market" class="v3-input" type="text" placeholder="BTCUSDT (코인 입력)" list="v3-market-list" autocomplete="off" style="max-width:240px"></td>' +
      '<td style="text-align:center"><button class="v3-btn v3-btn-long v3-me-go" data-dir="LONG">📈 LONG</button></td>' +
      '<td style="text-align:center"><button class="v3-btn v3-btn-long v3-me-go" data-dir="LONG" data-smart="1" title="신호 확인 후 진입(대기)">📈 LONG ⏳</button></td>' +
      '<td style="text-align:center"><button class="v3-btn v3-btn-short v3-me-go" data-dir="SHORT">📉 SHORT</button></td>' +
      '<td style="text-align:center"><button class="v3-btn v3-btn-short v3-me-go" data-dir="SHORT" data-smart="1" title="신호 확인 후 진입(대기)">📉 SHORT ⏳</button></td>' +
      '<td style="text-align:right;white-space:nowrap"><small class="text-muted">⏳ 대기</small> <input id="v3-me-timeout" class="v3-mini" type="number" value="60" min="1" max="240" step="5" style="width:58px"> <small class="text-muted">분</small></td>' +
      '</tr></tbody></table>' +
      '<small class="hint">⚠️ 실거래 — 즉시 진입(L/S) 또는 ⏳ 신호 대기(timeout 내 신호 확인 후). 안전가드 유지·자동 FLIP 없음. 후보에서 바로 쏘려면 GreenPen Scanner 위젯의 L/S 버튼.</small>';
  }
  function focusBlock(d) {
    let html = '<section class="v3-block">' + engineHeader('focus', d);
    if (!d || !d.ok) return html + '<div class="v3-placeholder">상태 로딩 중…</div></section>';
    const w = V3.state.widgets || {};
    // 🔭 Regime Transition Watch (Phase K) — 우측 패널 토글 (선택 시 상단 full-width)
    if (w.phasek) html += '<div id="v3-phasek" class="v3-phasek">' + renderPhaseK() + '</div>';
    if (w.summary !== false) html += summaryHtml(d);
    // 📋 Positions + 📊 BTC 분석 보조표 (우측, 보유 코인 순환) — 나란히
    if (w.positions !== false) {
      html += '<div class="v3-pos-row"><div class="v3-pos-main">' + positionsHtml(d) + '</div>' +
        '<div id="v3-wg-gp" class="v3-gp">' + renderGp() + '</div></div>';
    }
    html += buildWidgetsRow();   // 짧은 표 옆으로 나란히 (flex-wrap) — home 뷰와 공용
    // 🖐 Manual Entry — 맨 아래 (위 지표 다 보고 아래에서 진입). 우측 토글(기본 ON) → 가로 100% 표
    if (w.mentry !== false) html += '<div id="v3-wg-mentry" class="v3-widget v3-widget-wide" style="margin-top:10px">' + manualEntryHtml() + '</div>';
    return html + '</section>';
  }
  function stubBlock(name) {
    return '<section class="v3-block">' + engineHeader(name, null) +
      '<div class="v3-placeholder">' + (LABEL[name] || name) + ' 거래창 + 조건은 다음 Phase에서 연결 (현재 FOCUS·HARPOON 가동)</div></section>';
  }

  // ── 🐟 HARPOON 거래창 — v2 loadHarpoonStatus/History(22555) 포팅: state·stats·Current Scalp·FOCUS Link·Recent Scalps. (35-param Settings 는 Entry/Guards 분류 단계) ──
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
    if (!st || !st.ok) return html + '<div class="v3-placeholder">HARPOON 상태 로딩 중… (엔진 OFF 면 ▶ Start)</div></section>';
    const c = st.config || {};
    const losses = st.consecutive_losses || 0;
    const hw = V3.state.hpwidgets || {};   // 우측 패널 섹션 토글 (Stats/Scalp/Link/History)
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
    if (V3.syncHarpoonConfig && st && st.config) V3.syncHarpoonConfig(st.config);   // HARPOON 리본 [data-cfg] 채우기
    if (!V3.state.envView && V3.state.selected.has('harpoon')) { const el = $('v3-harpoon-block'); if (el) el.outerHTML = harpoonBlock(h); }
  }
  V3.loadHarpoon = loadHarpoon;

  // ── ⚡ LIGHTNING (Tier-1 플러그인, 마켓별 배포형) — 거래창 = 활성 배포 목록 / 배포 폼은 리본(정적) ──
  // FOCUS·HARPOON 과 달리 단일 엔진 config 가 없음: setup(배포)/list(조회)/stop(정지·청산·삭제) 모델.
  function pluginHeader(name, count) {
    return '<div class="v3-block-head"><span class="v3-block-title">' + (ICON[name] || '') + ' ' + (LABEL[name] || name) +
      ' <small class="v3-badge mute">' + count + ' 배포</small></span>' +
      '<span class="v3-pos-actions"><button class="v3-btn sm ghost" data-plugin-refresh="' + name + '" title="새로고침">🔄</button></span></div>';
  }
  function ltgListHtml(items) {
    if (!items.length) return '<div class="v3-placeholder">활성 LIGHTNING 배포 없음 — 아래 배포 폼에서 마켓 배포</div>';
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
          '<button class="v3-btn sm ghost v3-ltg-stop" data-mkt="' + it.market + '" data-act="stop" title="정지 (WATCH)">정지</button>' +
          '<button class="v3-btn sm ghost v3-ltg-stop" data-mkt="' + it.market + '" data-act="liquidate" title="청산 (포지션 매도)">청산</button>' +
          '<button class="v3-btn sm v3-btn-outline-danger v3-ltg-stop" data-mkt="' + it.market + '" data-act="delete" title="삭제 (DISABLED)">삭제</button>' +
        '</td></tr>';
    }).join('');
    return '<table class="v3-postable v3-postable-pos"><thead><tr><th>Market</th><th>State</th><th>A/M</th><th>Budget</th><th>Entry</th><th>Current</th><th>TP%</th><th>SL%</th><th>PnL</th><th>Actions</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  function lightningBlock(d) {
    const items = (d && d.items) || [];
    const sr = V3.state.lightning.showRecos;
    let h = '<section class="v3-block" id="v3-lightning-block">' + pluginHeader('lightning', items.length);
    // 🔍 추천 코인 (우측 토글 ON 시) — 필터바(정적·제목+필터+Rows 한 줄) + 목록(#v3-ltg-recos)
    if (sr) {
      const nrows = V3.state.lightning.recoRows || 5;
      const rowsSel = '<select id="v3-ltg-recorows" class="v3-mini" style="width:auto">' + [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 20].map((n) => '<option value="' + n + '"' + (n === nrows ? ' selected' : '') + '>' + n + '개</option>').join('') + '</select>';
      h += '<div class="v3-ltg-recobar"><span class="v3-reco-h" style="margin:0">🔍 추천 코인</span>' +
        '<span class="v3-ltg-recofilter">min$ <input id="v3-ltg-rmin" class="v3-mini" type="number" value="0" min="0" step="any"> ' +
        'max$ <input id="v3-ltg-rmax" class="v3-mini" type="number" value="0" min="0" step="any"> ' +
        '<button class="v3-btn sm ghost" id="v3-ltg-recos-refresh">🔍 불러오기</button> Rows ' + rowsSel + '</span></div>' +
        '<div id="v3-ltg-recos">' + renderRecos(V3.state.lightning.recos) + '</div>';
    }
    // ⚡ 수동 진입 폼 (본문 · 한 줄: 필드 + 버튼) — 추천 클릭→채움→예산 확인/수정→배포
    h += '<div class="v3-ltg-form"><div class="v3-ltg-form-row">' +
      '<div class="fld"><label class="v3-label">Market</label><input id="v3-ltg-market" class="v3-input" type="text" placeholder="BTCUSDT" list="v3-market-list" autocomplete="off"></div>' +
      '<div class="fld"><label class="v3-label">Budget</label><input id="v3-ltg-budget" class="v3-input" type="number" value="100" min="0" step="5"></div>' +
      '<div class="fld"><label class="v3-label">TP %</label><input id="v3-ltg-tp" class="v3-input" type="number" value="5.0" step="0.1"></div>' +
      '<div class="fld"><label class="v3-label">SL %</label><input id="v3-ltg-sl" class="v3-input" type="number" value="-3.0" step="0.1"></div>' +
      '<div class="fld"><label class="v3-label">Est. 이익</label><input id="v3-ltg-est" class="v3-input v3-pos" type="text" readonly placeholder="—" style="text-align:right"></div>' +
      '<button class="v3-btn v3-btn-long" id="v3-ltg-deploy">⚡ 배포</button>' +
      '<button class="v3-btn" id="v3-ltg-update" title="기존 배포 마켓 파라미터 갱신 (재설정)">업데이트</button>' +
      '<button class="v3-btn ghost" id="v3-ltg-recommend" title="현재 Market 추천 TP/SL 적용">권장값</button>' +
      '<label class="v3-recbud" title="권장값 적용 시 예산도 추천값으로"><input type="checkbox" class="v3-cfg-chk" id="v3-ltg-recbudget"> 예산도 변경</label>' +
      '</div>' +
      '<small class="hint">추천 클릭 → 폼 채움 → 예산 확인/수정 → ⚡배포(수동). 업데이트=기존 마켓 재설정. 자동 진입은 공통 Slots(슬롯 수만큼).</small></div>';
    // 📋 활성 배포 목록 (#v3-ltg-list, loader 가 갱신)
    h += '<div id="v3-ltg-list">' + ltgListHtml(items) + '</div>';
    return h + '</section>';
  }
  async function loadLightning(force) {
    const l = V3.state.lightning, now = Date.now();
    if (!force && l.items && now - l.t < 5000) { const e0 = $('v3-ltg-list'); if (e0) e0.innerHTML = ltgListHtml(l.items); return; }
    const d = await V3.getJSON('/api/strategy/lightning/list');
    l.items = (d && d.items) || []; l.t = now;
    const lst = $('v3-ltg-list'); if (lst) lst.innerHTML = ltgListHtml(l.items);   // 활성목록만 갱신 (배포 폼·추천 입력 보존 — 통째 재렌더 X)
    const blk = $('v3-lightning-block'); if (blk) { const b = blk.querySelector('.v3-badge.mute'); if (b) b.textContent = l.items.length + ' 배포'; }
  }
  V3.loadLightning = loadLightning;
  // 🔍 추천 코인 (특성 맞춤 = /api/strategy/recommendations profile 랭킹) — 점유 코인 배포 차단(중복 방지), 배포 예산=리본 Budget(자금 사정)
  // v2 loadCandidates verbatim 방식: rich row + 점유=안내 배지/흐리게만(차단 X) + 행 클릭→배포 폼 채움
  function renderRecos(d) {
    if (!V3.state.lightning.showRecos) return '';   // 우측 패널 토글 OFF → 숨김
    if (!d) return '<div class="v3-placeholder">[🔍 불러오기] 클릭 — 특성 맞춤 추천</div>';
    const all = d.items || [];
    if (!all.length) return '<div class="v3-placeholder">' + (d.computing ? '추천 계산 중… (자동 갱신 중, 잠시만 🔄)' : '추천 없음 — 가격 필터(min/max) 조정') + '</div>';
    const nrows = V3.state.lightning.recoRows || 5, total = all.length, pages = Math.max(1, Math.ceil(total / nrows));
    const pg = Math.min(Math.max(1, V3.state.lightning.recoPage || 1), pages);
    V3.state.lightning.recoPage = pg;
    const formBudget = parseFloat(($('v3-ltg-budget') && $('v3-ltg-budget').value) || '') || 100;   // 예상이익 = 실제 배포 예산(리본) 기준 (suggested_budget 은 자본 무시 → 표시·채움 안 함)
    const rows = all.slice((pg - 1) * nrows, pg * nrows).map((it) => {
      const mk = it.market || '', base = mk.replace('USDT', '');
      const active = it.active_strategy || null;   // 점유 전략명 (있으면 안내 배지+흐리게만, 클릭/배포 가능)
      const rp = it.recommended_params || {}, tp = rp.tp_pct, sl = rp.sl_pct;
      const budget = Number(it.suggested_budget_usdt || it.budget || 0), budOk = budget > 0 && budget <= 10000;   // 자본연동 추천 예산 (>$10k=옛 KRW 잔재 방어)
      const chg = Number(it.change_rate || 0), rsi = Math.round(Number(it.rsi || 50));
      const rsiCls = rsi <= 30 ? 'v3-pos' : rsi >= 70 ? 'v3-neg' : 'text-muted';
      const mom = Number(it.momentum || 0), macd = mom > 0.05 ? '▲' : mom < -0.05 ? '▼' : '→';
      const macdCls = mom > 0.05 ? 'v3-pos' : mom < -0.05 ? 'v3-neg' : 'text-muted';
      const aiAdj = it.ai_adjusted_score != null ? Math.round(it.ai_adjusted_score * 100) : (it.ai_score != null ? Math.round(it.ai_score * 100) : '-');
      const shouldBuy = it.ai_should_buy !== false;
      const regime = it.regime || '', rfit = it.regime_fit != null ? Math.round(it.regime_fit * 100) : '-';
      const est = (tp != null) ? Math.round((budOk ? budget : formBudget) * tp / 100) : 0;
      const badge = active ? ' <span class="v3-badge warn" title="다른 전략 점유 중 — 안내일 뿐, 배포 가능">⚠️ ' + active + '</span>' : '';
      return '<div class="v3-reco-row' + (active ? ' v3-reco-held' : '') + '" data-mkt="' + mk + '" data-budget="' + (budOk ? Math.round(budget) : '') + '" data-tp="' + (tp != null ? tp : '') + '" data-sl="' + (sl != null ? sl : '') + '" title="클릭 → 폼에 채움 (코인·예산·TP·SL)' + (active ? ' · 점유 안내일 뿐 배포 가능' : '') + '">' +
        '<div class="v3-reco-l1"><span><b>' + base + '</b> <small class="text-muted">' + _fp(it.price) + '</small> <small class="' + (chg >= 0 ? 'v3-pos' : 'v3-neg') + '">' + (chg >= 0 ? '+' : '') + chg.toFixed(1) + '%</small>' + badge + '</span>' +
        '<span>' + (budOk ? '<small class="text-muted">$' + Math.round(budget) + '</small> ' : '') + (tp != null ? '<span class="v3-badge long">TP ' + tp + '%</span> ' : '') + (sl != null ? '<span class="v3-badge short">SL ' + sl + '%</span>' : '') + '</span></div>' +
        '<div class="v3-reco-l2"><span><span class="' + rsiCls + '">RSI ' + rsi + '</span> <span class="' + macdCls + '">MACD ' + macd + '</span>' + (est > 0 ? ' <span class="text-muted">|</span> <span class="v3-pos">+$' + est + ' 예상</span>' : '') + '</span>' +
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
      '<div class="v3-recometa"><small class="text-muted">전체 ' + total + ' · 행 클릭→폼 · ⚠️=점유 안내</small>' + pager + '</div>';
  }
  async function loadLightningRecos(force) {
    const l = V3.state.lightning, now = Date.now();
    if (!force && l.recos && l.recos.items && l.recos.items.length && now - l.recosT < 180000) { const e0 = $('v3-ltg-recos'); if (e0) e0.innerHTML = renderRecos(l.recos); return; }
    const mn = ($('v3-ltg-rmin') && $('v3-ltg-rmin').value) || '0';
    const mx = ($('v3-ltg-rmax') && $('v3-ltg-rmax').value) || '0';
    const e0 = $('v3-ltg-recos'); if (e0 && !(l.recos && l.recos.items && l.recos.items.length)) e0.innerHTML = '<div class="v3-placeholder">추천 불러오는 중… (수 초)</div>';
    const d = await V3.getJSON('/api/strategy/recommendations?strategy=LIGHTNING&n=20&min_price=' + encodeURIComponent(mn) + '&max_price=' + encodeURIComponent(mx));
    l.recos = d; l.recoPage = 1; l.recosT = now;
    const e1 = $('v3-ltg-recos'); if (e1) e1.innerHTML = renderRecos(d);
    if (d && d.computing && !(d.items && d.items.length)) { l._retry = (l._retry || 0) + 1; if (l._retry <= 8 && V3.state.active === 'lightning') setTimeout(() => loadLightningRecos(true), 4000); } else { l._retry = 0; }
  }
  V3.loadLightningRecos = loadLightningRecos;
  // Est. 이익 = Budget × TP% / 100 (배포 폼 자동 계산 — v2 updateEstProfit)
  function updateLtgEst() {
    const b = parseFloat(($('v3-ltg-budget') && $('v3-ltg-budget').value) || '0') || 0;
    const tp = parseFloat(($('v3-ltg-tp') && $('v3-ltg-tp').value) || '0') || 0;
    const e = $('v3-ltg-est'); if (e) e.value = (b > 0 && tp) ? ('+$' + Math.round(b * tp / 100)) : '';
  }
  V3.updateLtgEst = updateLtgEst;
  // 🛡️ LIGHTNING Guards — 리본 Guards 패널 채우기 (정적 패널 → active 진입 시 1회 + 저장 후)
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
  // 🎯 SNIPER (Tier-1) — LIGHTNING 패턴 미러. 차이: setup JSON body 큼(필수=market)·
  //   stop=query string(sniper_id 우선)·sniper_id 다중(한 마켓 여러 instance)·guards 엔드포인트 없음·side(L/S).
  //   CSS 클래스(.v3-ltg-*/.v3-reco-*) 재사용, id=v3-snp-*. 추천은 strategy=SNIPER 프로필(특성 맞춤).
  // ════════════════════════════════════════════════════════════
  function snpListHtml(items) {
    if (!items.length) return '<div class="v3-placeholder">활성 SNIPER 배포 없음 — 아래 배포 폼에서 마켓 배포 (진입 신호 대기)</div>';
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
          '<button class="v3-btn sm ghost v3-snp-stop" data-sid="' + (it.sniper_id || '') + '" data-mkt="' + it.market + '" data-act="stop" title="정지 (WATCH)">정지</button>' +
          '<button class="v3-btn sm v3-btn-outline-danger v3-snp-stop" data-sid="' + (it.sniper_id || '') + '" data-mkt="' + it.market + '" data-act="delete" title="삭제 (DISABLED)">삭제</button>' +
        '</td></tr>';
    }).join('');
    return '<table class="v3-postable v3-postable-pos"><thead><tr><th>Market</th><th>Side</th><th>State</th><th>Budget</th><th>Entry</th><th>Current</th><th>TP%</th><th>SL%</th><th>PnL</th><th>Actions</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  function sniperBlock(d) {
    const items = (d && d.items) || [];
    let h = '<section class="v3-block" id="v3-sniper-block">' + pluginHeader('sniper', items.length);
    if (V3.state.sniper.showRecos) {
      const nrows = V3.state.sniper.recoRows || 5;
      const rowsSel = '<select id="v3-snp-recorows" class="v3-mini" style="width:auto">' + [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 20].map((n) => '<option value="' + n + '"' + (n === nrows ? ' selected' : '') + '>' + n + '개</option>').join('') + '</select>';
      h += '<div class="v3-ltg-recobar"><span class="v3-reco-h" style="margin:0">🎯 추천 코인 (SNIPER 특성)</span>' +
        '<span class="v3-ltg-recofilter">min$ <input id="v3-snp-rmin" class="v3-mini" type="number" value="0" min="0" step="any"> ' +
        'max$ <input id="v3-snp-rmax" class="v3-mini" type="number" value="0" min="0" step="any"> ' +
        '<button class="v3-btn sm ghost" id="v3-snp-recos-refresh">🔍 불러오기</button> Rows ' + rowsSel + '</span></div>' +
        '<div id="v3-snp-recos">' + renderSnpRecos(V3.state.sniper.recos) + '</div>';
    }
    h += '<div class="v3-ltg-form"><div class="v3-ltg-form-row">' +
      '<div class="fld"><label class="v3-label">Market</label><input id="v3-snp-market" class="v3-input" type="text" placeholder="BTCUSDT" list="v3-market-list" autocomplete="off"></div>' +
      '<div class="fld"><label class="v3-label">Side</label><select id="v3-snp-side" class="v3-input"><option value="LONG">LONG</option><option value="SHORT">SHORT</option></select></div>' +
      '<div class="fld"><label class="v3-label">Budget</label><input id="v3-snp-budget" class="v3-input" type="number" value="100" min="0" step="5"></div>' +
      '<div class="fld"><label class="v3-label">TP %</label><input id="v3-snp-tp" class="v3-input" type="number" value="2.0" step="0.1"></div>' +
      '<div class="fld"><label class="v3-label">SL %</label><input id="v3-snp-sl" class="v3-input" type="number" value="-2.5" step="0.1"></div>' +
      '<div class="fld"><label class="v3-label">Est. 이익</label><input id="v3-snp-est" class="v3-input v3-pos" type="text" readonly placeholder="—" style="text-align:right"></div>' +
      '<button class="v3-btn v3-btn-long" id="v3-snp-deploy">🎯 배포</button>' +
      '<button class="v3-btn" id="v3-snp-update" title="기존 마켓 재설정(upsert)">업데이트</button>' +
      '<button class="v3-btn ghost" id="v3-snp-recommend" title="현재 Market 추천 TP/SL 적용">권장값</button>' +
      '<label class="v3-recbud" title="권장값 적용 시 예산도 추천값으로"><input type="checkbox" class="v3-cfg-chk" id="v3-snp-recbudget"> 예산도 변경</label>' +
      '</div>' +
      '<small class="hint">추천 클릭 → 폼 채움 → 확인 → 🎯배포. SNIPER 는 진입 신호 대기(WATCH) 후 자동 진입. 업데이트=재설정(upsert).</small></div>';
    h += '<div id="v3-snp-list">' + snpListHtml(items) + '</div>';
    return h + '</section>';
  }
  async function loadSniper(force) {
    const l = V3.state.sniper, now = Date.now();
    if (!force && l.items && now - l.t < 5000) { const e0 = $('v3-snp-list'); if (e0) e0.innerHTML = snpListHtml(l.items); return; }
    const d = await V3.getJSON('/api/strategy/sniper/list');
    l.items = (d && d.items) || []; l.t = now;
    const lst = $('v3-snp-list'); if (lst) lst.innerHTML = snpListHtml(l.items);   // 활성목록만 갱신 (폼·추천 입력 보존)
    const blk = $('v3-sniper-block'); if (blk) { const b = blk.querySelector('.v3-badge.mute'); if (b) b.textContent = l.items.length + ' 배포'; }
  }
  V3.loadSniper = loadSniper;
  function renderSnpRecos(d) {
    if (!V3.state.sniper.showRecos) return '';
    if (!d) return '<div class="v3-placeholder">[🔍 불러오기] 클릭 — SNIPER 특성 맞춤 추천</div>';
    const all = d.items || [];
    if (!all.length) return '<div class="v3-placeholder">' + (d.computing ? '추천 계산 중… (자동 갱신 중, 잠시만 🔄)' : '추천 없음 — 가격 필터(min/max) 조정') + '</div>';
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
      const badge = active ? ' <span class="v3-badge warn" title="다른 전략 점유 중 — 안내일 뿐, 배포 가능">⚠️ ' + active + '</span>' : '';
      return '<div class="v3-reco-row' + (active ? ' v3-reco-held' : '') + '" data-mkt="' + mk + '" data-budget="' + (budOk ? Math.round(budget) : '') + '" data-tp="' + (tp != null ? tp : '') + '" data-sl="' + (sl != null ? sl : '') + '" title="클릭 → 폼에 채움' + (active ? ' · 점유 안내일 뿐 배포 가능' : '') + '">' +
        '<div class="v3-reco-l1"><span><b>' + base + '</b> <small class="text-muted">' + _fp(it.price) + '</small> <small class="' + (chg >= 0 ? 'v3-pos' : 'v3-neg') + '">' + (chg >= 0 ? '+' : '') + chg.toFixed(1) + '%</small>' + badge + '</span>' +
        '<span>' + (budOk ? '<small class="text-muted">$' + Math.round(budget) + '</small> ' : '') + (tp != null ? '<span class="v3-badge long">TP ' + tp + '%</span> ' : '') + (sl != null ? '<span class="v3-badge short">SL ' + sl + '%</span>' : '') + '</span></div>' +
        '<div class="v3-reco-l2"><span><span class="' + rsiCls + '">RSI ' + rsi + '</span> <span class="' + macdCls + '">MACD ' + macd + '</span>' + (est > 0 ? ' <span class="text-muted">|</span> <span class="v3-pos">+$' + est + ' 예상</span>' : '') + '</span>' +
        '<span><span class="' + (shouldBuy ? 'v3-pos' : 'v3-warn') + '">AI ' + aiAdj + '%' + (shouldBuy ? '' : ' ⚠️') + '</span> <small class="text-muted"> ' + regime + ' ' + rfit + '%</small></span></div>' +
        '</div>';
    }).join('');
    let pager = '';
    if (pages > 1) { const btns = []; for (let i = 1; i <= pages; i++) btns.push(i === pg ? '<span class="v3-jpg cur">' + i + '</span>' : '<button class="v3-jpg v3-recopage" data-page="' + i + '">' + i + '</button>'); pager = '<div class="v3-jpager">' + btns.join('') + '</div>'; }
    return '<div class="v3-reco-list">' + rows + '</div>' +
      '<div class="v3-recometa"><small class="text-muted">전체 ' + total + ' · 행 클릭→폼 · ⚠️=점유 안내</small>' + pager + '</div>';
  }
  async function loadSniperRecos(force) {
    const l = V3.state.sniper, now = Date.now();
    if (!force && l.recos && l.recos.items && l.recos.items.length && now - l.recosT < 180000) { const e0 = $('v3-snp-recos'); if (e0) e0.innerHTML = renderSnpRecos(l.recos); return; }
    const mn = ($('v3-snp-rmin') && $('v3-snp-rmin').value) || '0';
    const mx = ($('v3-snp-rmax') && $('v3-snp-rmax').value) || '0';
    const e0 = $('v3-snp-recos'); if (e0 && !(l.recos && l.recos.items && l.recos.items.length)) e0.innerHTML = '<div class="v3-placeholder">추천 불러오는 중… (수 초)</div>';
    const d = await V3.getJSON('/api/strategy/recommendations?strategy=SNIPER&n=20&min_price=' + encodeURIComponent(mn) + '&max_price=' + encodeURIComponent(mx));
    l.recos = d; l.recoPage = 1; l.recosT = now;
    const e1 = $('v3-snp-recos'); if (e1) e1.innerHTML = renderSnpRecos(d);
    // 계산 중(cold/semaphore busy)이면 자동 재시도 — 준비되면 사용자 조작 없이 뜸 (SNIPER 는 prewarm 6번째라 늦게 데워짐)
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
  // 🔌 Generic Tier-1 plugin 패널 (config-driven) — GAZUA/CONTRARIAN/LADDER 공용 (중복 제거).
  //   LIGHTNING/SNIPER 는 먼저 만든 bespoke (추후 이리로 이관). 신규 = PLUG 에 config 추가만.
  //   요소 id = v3-{key}-* / 스코핑 = .v3-plug-recos[data-plug] + .v3-plug-* (LIGHTNING/SNIPER 와 분리).
  // ════════════════════════════════════════════════════════════
  const PLUG = {
    gazua: {
      label: 'GAZUA', strategy: 'GAZUA', api: 'gazua', stopMode: 'body',
      actions: ['stop', 'liquidate', 'delete'], hasSide: false, defTp: '15.0', defSl: '-10.0',
      recoTitle: '🚀 추천 코인 (GAZUA 특성)',
      hint: '추천 클릭 → 폼 채움 → 확인 → 배포. GAZUA 는 고변동 급등 노림(높은 TP·딥 SL). 업데이트=재설정.',
      setupBody: (f) => ({ market: f.market, budget_usdt: f.budget, tp_pct: f.tp, sl_pct: f.sl }),
    },
    contrarian: {
      label: 'CONTRARIAN', strategy: 'CONTRARIAN', api: 'contrarian', stopMode: 'query',
      actions: ['stop', 'delete'], hasSide: false, defTp: '15.0', defSl: '-50.0',   // stop=query(liquidate 없음) · 역추세 깊은 SL
      recoTitle: '🔄 역행 코인 (기준점 대비)',
      hint: '추천 클릭 → 폼 채움 → 확인 → 배포. CONTRARIAN 은 역추세 반등 노림(깊은 SL=물속 인내). 업데이트=재설정.',
      setupBody: (f) => ({ market: f.market, budget_usdt: f.budget, tp_pct: f.tp, sl_pct: f.sl }),
      // ★ 역행 전용 scanner — recommendations 대신 /contrarian/scan(benchmark=역행 기준점) 사용 (부모님 "역행 기준점 옵션")
      scan: { path: '/api/strategy/contrarian/scan', defBenchmark: 'BTC', benchmarks: [['BTC', 'BTC'], ['ETH', 'ETH'], ['MARKET_AVG', '시장평균'], ['FEAR_GREED', '공포·탐욕']] },
    },
  };
  function plugState(key) { if (!V3.state[key]) V3.state[key] = { items: null, t: 0, recos: null, recosT: 0, showRecos: true, recoRows: 5, recoPage: 1 }; return V3.state[key]; }
  function plugListHtml(key, items) {
    const c = PLUG[key];
    if (!items.length) return '<div class="v3-placeholder">활성 ' + c.label + ' 배포 없음 — 아래 배포 폼에서 마켓 배포</div>';
    const rows = items.map((it) => {
      const p = it.position || {}, pn = it.pnl || {}, pr = it.params || {};
      // ★ [2026-06-02 부모] 진입 근접도 게이지 (selector score) — WATCH 코인만 (ACTIVE=이미 진입)
      const _es = Number(it.entry_score || 0), _isWatch = String(it.state || '').toUpperCase() === 'WATCH';
      const _esCell = !_isWatch ? '<span class="text-muted">—</span>' : (_es <= 0 ? '<small class="text-muted">평가 대기</small>' : ('<div class="v3-escore" title="selector score ' + _es + ' — 높을수록 진입 임박"><b>' + _es.toFixed(0) + '</b><span class="v3-escore-bar"><i style="width:' + Math.min(100, _es) + '%"></i></span></div>'));
      const qty = Number(p.qty || 0), entry = Number(p.entry || 0), val = Number(pn.value || 0);
      const cur = qty > 0 ? val / qty : 0, amt = Number(pn.amount || 0), pct = Number(pn.pct || 0);
      const tp = pr.tp_pct != null ? pr.tp_pct : (pr.tp != null ? pr.tp : 0);
      const sl = pr.sl_pct != null ? pr.sl_pct : (pr.sl != null ? pr.sl : 0);
      const acts = c.actions.map((a) => {
        const lab = a === 'liquidate' ? '청산' : a === 'delete' ? '삭제' : '정지';
        const cls = a === 'delete' ? 'v3-btn sm v3-btn-outline-danger' : 'v3-btn sm ghost';
        const ttl = a === 'liquidate' ? '청산 (포지션 매도)' : a === 'delete' ? '삭제 (DISABLED)' : '정지 (WATCH)';
        return '<button class="' + cls + ' v3-plug-stop" data-plug="' + key + '" data-mkt="' + it.market + '" data-act="' + a + '" title="' + ttl + '">' + lab + '</button>';
      }).join('');
      return '<tr><td><b class="v3-mkt" data-bybit="' + it.market + '">' + (it.market || '').replace('USDT', '') + '</b></td>' +
        '<td>' + (it.state || '-') + '</td><td>' + _esCell + '</td><td>$' + Number(it.budget || 0).toFixed(0) + '</td>' +
        '<td>' + (entry ? _fp(entry) : '-') + '</td><td>' + (cur ? _fp(cur) : '-') + '</td>' +
        '<td>' + Number(tp).toFixed(1) + '%</td><td class="v3-neg">' + Number(sl).toFixed(1) + '%</td>' +
        '<td class="' + V3.pnlCls(amt) + '">' + (amt >= 0 ? '+' : '') + '$' + amt.toFixed(2) + ' <small>(' + (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%)</small></td>' +
        '<td class="v3-plug-actions">' + acts + '</td></tr>';
    }).join('');
    return '<table class="v3-postable v3-postable-pos"><thead><tr><th>Market</th><th>State</th><th>🎯 진입도</th><th>Budget</th><th>Entry</th><th>Current</th><th>TP%</th><th>SL%</th><th>PnL</th><th>Actions</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  function renderPlugRecos(key, d) {
    const st = plugState(key), c = PLUG[key];
    if (!st.showRecos) return '';
    if (!d) return '<div class="v3-placeholder">[🔍 불러오기] 클릭 — ' + (c.scan ? '역행 기준점 선택 후 스캔' : c.label + ' 특성 맞춤 추천') + '</div>';
    const all = (c.scan ? d.candidates : d.items) || [];
    if (!all.length) return '<div class="v3-placeholder">' + (c.scan ? '역행 코인 없음 — 기준점(benchmark) 바꿔보세요' : (d.computing ? '추천 계산 중… (자동 갱신 중, 잠시만 🔄)' : '추천 없음 — 가격 필터(min/max) 조정')) + '</div>';
    const nrows = st.recoRows || 5, total = all.length, pages = Math.max(1, Math.ceil(total / nrows));
    const pg = Math.min(Math.max(1, st.recoPage || 1), pages); st.recoPage = pg;
    const formBudget = parseFloat(($('v3-' + key + '-budget') && $('v3-' + key + '-budget').value) || '') || 100;
    const rows = all.slice((pg - 1) * nrows, pg * nrows).map((it) => {
      if (c.scan) {   // 🔄 역행 스캔 행 — 코인 vs 기준점 수익률·역행점수·RS·Corr·AI (price/tp/sl 없음 → 클릭 시 기본 TP/SL)
        const mk = it.market || '', base = mk.replace('USDT', '');
        const cret = Number(it.coin_ret_pct || 0), bret = Number(it.benchmark_ret_pct || 0), score = it.score || 0, rsd = Number(it.rs_diff || 0), corr = it.corr;
        const ai = it.ai_score != null ? Math.round(it.ai_score * 100) : '-';
        return '<div class="v3-reco-row" data-mkt="' + mk + '" data-budget="" data-tp="' + c.defTp + '" data-sl="' + c.defSl + '" title="클릭 → 폼 채움 (CONTRARIAN 기본 TP/SL)">' +
          '<div class="v3-reco-l1"><span><b>' + base + '</b> <small class="' + (cret >= 0 ? 'v3-pos' : 'v3-neg') + '">' + (cret >= 0 ? '+' : '') + cret.toFixed(1) + '%</small> <small class="text-muted">vs 기준 ' + (bret >= 0 ? '+' : '') + bret.toFixed(1) + '%</small>' + (it.early_signal ? ' <span class="v3-badge warn">조기</span>' : '') + '</span>' +
          '<span><span class="v3-badge ' + (score >= 2 ? 'long' : 'mute') + '">역행 ' + score + '/3</span></span></div>' +
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
      const badge = active ? ' <span class="v3-badge warn" title="다른 전략 점유 중 — 안내일 뿐, 배포 가능">⚠️ ' + active + '</span>' : '';
      return '<div class="v3-reco-row' + (active ? ' v3-reco-held' : '') + '" data-mkt="' + mk + '" data-budget="' + (budOk ? Math.round(budget) : '') + '" data-tp="' + (tp != null ? tp : '') + '" data-sl="' + (sl != null ? sl : '') + '" title="클릭 → 폼에 채움' + (active ? ' · 점유 안내일 뿐 배포 가능' : '') + '">' +
        '<div class="v3-reco-l1"><span><b>' + base + '</b> <small class="text-muted">' + _fp(it.price) + '</small> <small class="' + (chg >= 0 ? 'v3-pos' : 'v3-neg') + '">' + (chg >= 0 ? '+' : '') + chg.toFixed(1) + '%</small>' + badge + '</span>' +
        '<span>' + (budOk ? '<small class="text-muted">$' + Math.round(budget) + '</small> ' : '') + (tp != null ? '<span class="v3-badge long">TP ' + tp + '%</span> ' : '') + (sl != null ? '<span class="v3-badge short">SL ' + sl + '%</span>' : '') + '</span></div>' +
        '<div class="v3-reco-l2"><span><span class="' + rsiCls + '">RSI ' + rsi + '</span> <span class="' + macdCls + '">MACD ' + macd + '</span>' + (est > 0 ? ' <span class="text-muted">|</span> <span class="v3-pos">+$' + est + ' 예상</span>' : '') + '</span>' +
        '<span><span class="' + (shouldBuy ? 'v3-pos' : 'v3-warn') + '">AI ' + aiAdj + '%' + (shouldBuy ? '' : ' ⚠️') + '</span> <small class="text-muted"> ' + regime + ' ' + rfit + '%</small></span></div></div>';
    }).join('');
    let pager = '';
    if (pages > 1) { const btns = []; for (let i = 1; i <= pages; i++) btns.push(i === pg ? '<span class="v3-jpg cur">' + i + '</span>' : '<button class="v3-jpg v3-recopage" data-page="' + i + '">' + i + '</button>'); pager = '<div class="v3-jpager">' + btns.join('') + '</div>'; }
    const meta = c.scan ? ('기준: ' + (d.benchmark_label || d.benchmark_type || '') + ' ' + (d.benchmark_ret_pct != null ? ((Number(d.benchmark_ret_pct) >= 0 ? '+' : '') + Number(d.benchmark_ret_pct).toFixed(1) + '%') : '') + (d.market_down ? ' · 시장 하락' : '') + ' · 전체 ' + total + ' · 행 클릭→폼') : ('전체 ' + total + ' · 행 클릭→폼 · ⚠️=점유 안내');
    return '<div class="v3-reco-list">' + rows + '</div><div class="v3-recometa"><small class="text-muted">' + meta + '</small>' + pager + '</div>';
  }
  function plugBlock(key, d) {
    const c = PLUG[key], st = plugState(key), items = (d && d.items) || [];
    let h = '<section class="v3-block" id="v3-' + key + '-block">' + pluginHeader(key, items.length);
    if (st.showRecos) {
      const nrows = st.recoRows || 5;
      const rowsSel = '<select id="v3-' + key + '-recorows" class="v3-mini" style="width:auto">' + [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 15, 20].map((n) => '<option value="' + n + '"' + (n === nrows ? ' selected' : '') + '>' + n + '개</option>').join('') + '</select>';
      const filt = c.scan
        ? ('역행 기준점 <select id="v3-' + key + '-benchmark" class="v3-mini" style="width:auto">' + c.scan.benchmarks.map((bm) => '<option value="' + bm[0] + '"' + ((st.benchmark || c.scan.defBenchmark) === bm[0] ? ' selected' : '') + '>' + bm[1] + '</option>').join('') + '</select> ')
        : ('min$ <input id="v3-' + key + '-rmin" class="v3-mini" type="number" value="0" min="0" step="any"> max$ <input id="v3-' + key + '-rmax" class="v3-mini" type="number" value="0" min="0" step="any"> ');
      h += '<div class="v3-ltg-recobar"><span class="v3-reco-h" style="margin:0">' + c.recoTitle + '</span>' +
        '<span class="v3-ltg-recofilter">' + filt +
        '<button class="v3-btn sm ghost v3-plug-recos-refresh" data-plug="' + key + '">🔍 불러오기</button> Rows ' + rowsSel + '</span></div>' +
        '<div class="v3-plug-recos" data-plug="' + key + '" id="v3-' + key + '-recos">' + renderPlugRecos(key, st.recos) + '</div>';
    }
    h += '<div class="v3-ltg-form"><div class="v3-ltg-form-row">' +
      '<div class="fld"><label class="v3-label">Market</label><input id="v3-' + key + '-market" class="v3-input" type="text" placeholder="BTCUSDT" list="v3-market-list" autocomplete="off"></div>' +
      (c.hasSide ? '<div class="fld"><label class="v3-label">Side</label><select id="v3-' + key + '-side" class="v3-input"><option value="LONG">LONG</option><option value="SHORT">SHORT</option></select></div>' : '') +
      '<div class="fld"><label class="v3-label">Budget</label><input id="v3-' + key + '-budget" class="v3-input" type="number" value="100" min="0" step="5"></div>' +
      '<div class="fld"><label class="v3-label">TP %</label><input id="v3-' + key + '-tp" class="v3-input" type="number" value="' + c.defTp + '" step="0.1"></div>' +
      '<div class="fld"><label class="v3-label">SL %</label><input id="v3-' + key + '-sl" class="v3-input" type="number" value="' + c.defSl + '" step="0.1"></div>' +
      '<div class="fld"><label class="v3-label">Est. 이익</label><input id="v3-' + key + '-est" class="v3-input v3-pos" type="text" readonly placeholder="—" style="text-align:right"></div>' +
      '<button class="v3-btn v3-btn-long v3-plug-deploy" data-plug="' + key + '">' + (ICON[key] || '🚀') + ' 배포</button>' +
      '<button class="v3-btn v3-plug-deploy" data-plug="' + key + '" data-upd="1" title="기존 마켓 재설정">업데이트</button>' +
      '<button class="v3-btn ghost v3-plug-recommend" data-plug="' + key + '" title="현재 Market 추천 TP/SL 적용">권장값</button>' +
      '<label class="v3-recbud" title="권장값 적용 시 예산도 추천값으로"><input type="checkbox" class="v3-cfg-chk" id="v3-' + key + '-recbudget"> 예산도 변경</label>' +
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
    const blk = $('v3-' + key + '-block'); if (blk) { const b = blk.querySelector('.v3-badge.mute'); if (b) b.textContent = st.items.length + ' 배포'; }
  }
  V3.loadPlug = loadPlug;
  async function loadPlugRecos(key, force) {
    const c = PLUG[key], st = plugState(key), now = Date.now();
    const cur = st.recos && (c.scan ? st.recos.candidates : st.recos.items);
    if (!force && cur && cur.length && now - st.recosT < (c.scan ? 30000 : 180000)) { const e0 = $('v3-' + key + '-recos'); if (e0) e0.innerHTML = renderPlugRecos(key, st.recos); return; }
    const e0 = $('v3-' + key + '-recos'); if (e0 && !(cur && cur.length)) e0.innerHTML = '<div class="v3-placeholder">' + (c.scan ? '역행 코인 스캔 중…' : '추천 불러오는 중… (수 초)') + '</div>';
    let d;
    if (c.scan) {   // 🔄 역행 전용 scanner (benchmark=기준점)
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
  // 📐 LADDER — 읽기 전용 (그리드 구조 보기만, 주문 X). 부모님 "거래소 선 긋는 거라 조심스러워" → 배포/주문은 다음 단계.
  // ════════════════════════════════════════════════════════════
  function ladderStepsHtml(d) {
    if (!d || !d.ok) return '<div class="v3-placeholder">단 정보 없음 — ' + ((d && d.error) || '마켓 선택') + '</div>';
    const pos = d.position || {}, pn = d.pnl || {}, amt = Number(pn.amount || 0), pct = Number(pn.pct || 0);
    const head = '<div style="margin:8px 0 4px;font-size:12px"><b class="v3-mkt" data-bybit="' + d.market + '">' + (d.market || '').replace('USDT', '') + '</b> ' +
      '<small class="text-muted">기준 ' + _fp(d.base_price) + ' · 현재 ' + _fp(d.current_price) + ' · 단계 ' + d.next_step + '/' + d.max_steps + ' · 간격 ' + d.step_pct + '% · TP ' + d.tp_pct + '% · 예산 $' + Number(d.budget || 0).toFixed(0) + '</small> · 보유 ' + Number(pos.qty || 0).toFixed(4) + ' @ ' + _fp(pos.avg_buy) + ' <span class="' + V3.pnlCls(amt) + '">(' + (amt >= 0 ? '+' : '') + '$' + amt.toFixed(0) + ' ' + (pct >= 0 ? '+' : '') + pct.toFixed(1) + '%)</span></div>';
    const rows = (d.steps || []).map((s) => {
      const lab = s.status === 'filled' ? '<span class="v3-pos">● 체결</span>' : s.status === 'next' ? '<span class="v3-badge warn">◀ 다음</span>' : '<span class="text-muted">○ 대기</span>';
      return '<tr><td>' + s.step + '</td><td>' + _fp(s.price) + '</td><td>$' + Number(s.budget || 0).toFixed(0) + '</td><td>' + lab + '</td></tr>';
    }).join('');
    return head + '<table class="v3-postable"><thead><tr><th>단</th><th>가격(선)</th><th>금액</th><th>상태</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  // 🔧 실주문(grid_state, uuid 有) — 개별 단 가격/수량 수정·일시정지·삭제. 계산 plan(위)과 별개: 거래소에 실제 깔린 주문.
  function ladderOrdersHtml(g) {
    if (!g || !g.ok) return '';
    const mkt = g.market, all = (g.steps || []);
    if (!all.length) return '<div class="v3-placeholder" style="margin-top:8px">🔧 활성 실주문 없음 — [🌱 깔기] 또는 🟢 자동 ON 저장 시 거래소에 주문 생성됨</div>';
    const rows = all.map((s) => {
      const sideLab = s.side === 'buy' ? '<span class="v3-pos">매수</span>' : '<span class="v3-neg">매도</span>';
      const stLab = s.filled ? '<span class="text-muted">● 체결</span>' : s.status === 'paused' ? '<span class="v3-badge warn">⏸ 정지</span>' : '<span class="v3-pos">○ 활성</span>';
      const acts = s.filled ? '<small class="text-muted">—</small>' :
        '<button class="v3-btn sm ghost v3-lad-step-pause" data-mkt="' + mkt + '" data-uuid="' + s.uuid + '" data-st="' + (s.status === 'paused' ? 'active' : 'paused') + '">' + (s.status === 'paused' ? '▶ 재개' : '⏸ 정지') + '</button>' +
        '<button class="v3-btn sm ghost v3-lad-step-edit" data-mkt="' + mkt + '" data-uuid="' + s.uuid + '" data-price="' + s.price + '" data-amount="' + (s.amount || 0) + '">✏️ 수정</button>' +
        '<button class="v3-btn sm v3-btn-outline-danger v3-lad-step-del" data-mkt="' + mkt + '" data-uuid="' + s.uuid + '">🗑 삭제</button>';
      return '<tr><td>' + sideLab + '</td><td>' + _fp(s.price) + '</td><td>$' + Number(s.amount || 0).toFixed(0) + '</td><td>' + stLab + '</td><td class="v3-plug-actions">' + acts + '</td></tr>';
    }).join('');
    return '<div style="margin:10px 0 4px;font-size:12px"><b>🔧 활성 실주문</b> <small class="text-muted">(' + all.length + '개 · 현재가 ' + _fp(g.current_price) + ') — 개별 단 수정·정지·삭제 <b style="color:var(--v3-warn)">(실주문!)</b></small></div>' +
      '<table class="v3-postable"><thead><tr><th>방향</th><th>가격(선)</th><th>금액</th><th>상태</th><th>편집</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  function ladderListHtml(items) {
    if (!items.length) return '<div class="v3-placeholder">활성 LADDER 없음</div>';
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
        '<td><small class="text-muted">매수' + (it.buy_count || 0) + '·매도' + (it.sell_count || 0) + '</small></td>' +
        '<td class="v3-plug-actions"><button class="v3-btn sm ghost v3-ladder-steps-btn" data-mkt="' + it.market + '">📊 단</button>' +
          '<button class="v3-btn sm v3-ladder-seed" data-mkt="' + it.market + '" title="그리드 지정가 매수 주문 깔기 (실주문!)">🌱 깔기</button>' +
          '<button class="v3-btn sm v3-btn-outline-danger v3-ladder-cancel" data-mkt="' + it.market + '" title="이 마켓 LADDER 주문 전체 취소">🗑️ 취소</button></td>' +
        '</tr>';
    }).join('');
    return '<table class="v3-postable v3-postable-pos"><thead><tr><th>Market</th><th>State</th><th>단계</th><th>Budget</th><th>Position</th><th>Current</th><th>PnL</th><th>B/S</th><th>주문</th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  function ladderBlock(d) {
    const items = (d && d.items) || [];
    let h = '<section class="v3-block" id="v3-ladder-block">' + pluginHeader('ladder', items.length);
    h += '<div class="v3-placeholder" style="text-align:left;margin:8px 0;border-left:3px solid var(--v3-warn);padding-left:10px">📐 그리드 사다리 — 구성 후 <b>🟢 자동 ON</b> 으로 저장하면 슬롯 열려 <b>자동 주문·거래(설정 끝, 알아서 굴러감)</b>. <b>OFF</b> 로 저장하면 구성만(주문 X)·[📊 단 보기]로 테스트. 운영자 "슬롯 열고 저장+ON 이면 끝".</div>';
    // 📐 그리드 구성 폼 (주문 X — buy_now=false·grid_auto_sync=false)
    h += '<div class="v3-ltg-form"><div class="v3-ltg-form-row">' +
      '<div class="fld"><label class="v3-label">Market</label><input id="v3-ladder-market" class="v3-input" type="text" placeholder="BTCUSDT" list="v3-market-list" autocomplete="off"></div>' +
      '<div class="fld"><label class="v3-label">Budget</label><input id="v3-ladder-budget" class="v3-input" type="number" value="100" min="0" step="5"></div>' +
      '<div class="fld"><label class="v3-label">단수 <small>(max_steps)</small></label><input id="v3-ladder-maxsteps" class="v3-input" type="number" value="10" min="1" max="40" step="1"></div>' +
      '<div class="fld"><label class="v3-label">간격 % <small>(step)</small></label><input id="v3-ladder-steppct" class="v3-input" type="number" value="1.0" min="0.1" step="0.1"></div>' +
      '<div class="fld"><label class="v3-label">단별금액 <small>(0=자동)</small></label><input id="v3-ladder-order" class="v3-input" type="number" value="0" min="0" step="1"></div>' +
      '<div class="fld"><label class="v3-label">마틴 <small>(1=균등)</small></label><input id="v3-ladder-mart" class="v3-input" type="number" value="1.0" min="1" step="0.05"></div>' +
      '<div class="fld"><label class="v3-label">TP %</label><input id="v3-ladder-tp" class="v3-input" type="number" value="2.0" step="0.1"></div>' +
      '<label class="v3-recbud" title="ON=슬롯 열고 자동거래 시작(실주문) / OFF=구성만(주문 X·테스트)"><input type="checkbox" class="v3-cfg-chk" id="v3-ladder-autosync"> 🟢 자동 ON</label>' +
      '<button class="v3-btn v3-btn-long" id="v3-ladder-deploy">📐 저장</button>' +
      '</div><small class="hint">🟢 자동 ON = 슬롯 열림 → 그리드 자동 주문·거래(설정 끝, 알아서 굴러감). OFF = 구성만 저장(주문 X)·[📊 단 보기]로 테스트 후 다시 ON. 단별금액 0=예산÷단수.</small></div>';
    h += '<div id="v3-ladder-list">' + ladderListHtml(items) + '</div>';
    h += '<div id="v3-ladder-steps">' + (V3.state.ladder.stepsMarket ? (ladderStepsHtml(V3.state.ladder.steps) + ladderOrdersHtml(V3.state.ladder.orders)) : '<div class="v3-placeholder">행의 [📊 단 보기] 클릭 → 그리드 단(계산 plan) + 활성 실주문(개별 수정·정지·삭제) 표시</div>') + '</div>';
    return h + '</section>';
  }
  async function loadLadder(force) {
    const l = V3.state.ladder, now = Date.now();
    if (!force && l.items && now - l.t < 5000) { const e0 = $('v3-ladder-list'); if (e0) e0.innerHTML = ladderListHtml(l.items); return; }
    const d = await V3.getJSON('/api/strategy/ladder/list');
    l.items = (d && d.items) || []; l.t = now;
    const lst = $('v3-ladder-list'); if (lst) lst.innerHTML = ladderListHtml(l.items);
    const blk = $('v3-ladder-block'); if (blk) { const b = blk.querySelector('.v3-badge.mute'); if (b) b.textContent = l.items.length + ' 사다리'; }
    if (l.stepsMarket) loadLadderSteps(l.stepsMarket);   // 열려있는 단 갱신
  }
  V3.loadLadder = loadLadder;
  async function loadLadderSteps(market) {
    const l = V3.state.ladder; l.stepsMarket = market;
    const mq = encodeURIComponent(market);
    const [d, g] = await Promise.all([
      V3.getJSON('/api/strategy/ladder/steps?market=' + mq),       // 계산 plan
      V3.getJSON('/api/ladder/grid/state?market=' + mq),           // 실주문(uuid)
    ]);
    l.steps = d; l.orders = g;
    const e = $('v3-ladder-steps'); if (e) e.innerHTML = ladderStepsHtml(d) + ladderOrdersHtml(g);
  }
  V3.loadLadderSteps = loadLadderSteps;

  // ════════════════════════════════════════════════════════════
  // 🔌 플러그인 공통 설정 (Reserved /api/reserved/settings) — 한 곳에서 관리 (부모님)
  // 각 입력: data-rk = POST 파라미터명 / data-rget = GET snapshot 점경로 (GET 중첩 ↔ POST flat)
  // 백엔드 무변경 (기존 엔드포인트). Strategy TP/SL·Autopilot·Triage·Guard Matrix 는 다음 단계.
  // ════════════════════════════════════════════════════════════
  const PLUGS8 = ['pingpong', 'autoloop', 'ladder', 'lightning', 'gazua', 'contrarian', 'sniper', 'whale'];
  const PLUGS7 = ['pingpong', 'autoloop', 'ladder', 'lightning', 'gazua', 'contrarian', 'sniper'];
  const TPSL_STRATS = ['PINGPONG', 'AUTOLOOP', 'LADDER', 'LIGHTNING', 'GAZUA', 'CONTRARIAN', 'SNIPER'];
  function _rget(snap, path) { return String(path).split('.').reduce((o, k) => (o == null ? undefined : o[k]), snap); }
  // src = reserved|guards|triage|tpsl · rk = 저장 key(또는 tpsl 점경로) · g = GET 점경로 · kind = chk|num|text
  function _R(label, src, rk, g, kind, attrs) {
    const a = 'data-src="' + src + '" data-rk="' + rk + '" data-rget="' + g + '" data-kind="' + kind + '"';
    if (kind === 'chk') return '<label class="rib-row"><span>' + label + '</span><input type="checkbox" class="v3-cfg-chk v3-rin" ' + a + '></label>';
    const t = kind === 'text' ? 'text' : 'number';
    const cls = kind === 'text' ? 'v3-mini v3-rin' : 'v3-mini v3-rin';
    return '<div class="rib-row"><span>' + label + '</span><input class="' + cls + '" type="' + t + '" ' + (attrs || '') + ' ' + a + '></div>';
  }
  function _sub(t) { return '<div class="v3-cset-sub">' + t + '</div>'; }
  function _prettyLabel(k) { return String(k).replace(/_/g, ' '); }
  // 자동 렌더 제외 — GET 엔 뜨지만 (a)POST/PATCH 가 안 받아 저장 안 됨 (b)내부 상태/경로값 (c)위험 토글 (d)다른 곳과 중복
  const _GUARDS_SKIP = new Set(['emergency_stop', 'ui_settings_loaded', 'btc_guard_mode', 'btc_guard_enabled']);  // btc_guard_enabled=Demotion 과 중복 / emergency_stop=E-STOP 상태 / 나머지=POST 미수용
  const _TRIAGE_SKIP = new Set(['state_path']);   // 내부 파일경로 (exempt_strategies 등 리스트는 typeof object 로 자동 제외)
  // GET 응답 dict 를 통째로 자동 렌더 (flat) — 하드코딩 누락 원천 차단 + 백엔드에 새 키 생겨도 자동 노출 (부모님). skip=비-settable 제외
  function _autoRows(src, obj, skip) {
    if (!obj || typeof obj !== 'object') return '';
    return Object.keys(obj).sort().map((k) => {
      if (skip && skip.has(k)) return '';
      const v = obj[k];
      if (v !== null && typeof v === 'object') return '';   // 리스트/중첩 dict 건너뜀 (exempt_strategies 등)
      const kind = (typeof v === 'boolean') ? 'chk' : (typeof v === 'number') ? 'num' : 'text';
      return _R(_prettyLabel(k), src, k, k, kind, kind === 'num' ? 'step="any"' : '');
    }).join('');
  }
  function _csec(title, body, csave, hint) {
    return '<section class="v3-cset" data-csave="' + csave + '"><div class="v3-cset-h">' + title + '</div>' + body +
      '<button class="v3-btn v3-btn-long rsave">✓ ' + title + ' 저장</button>' +
      (hint ? '<small class="hint" style="display:block;color:var(--v3-fg-mute);margin-top:6px;">' + hint + '</small>' : '') + '</section>';
  }
  function pluginsCommonBlock() {
    // 🎰 Slots & Budget (reserved)
    let slots = _R('Capital-based Auto Allocation', 'reserved', 'auto_slot_enabled', 'auto_slot_enabled', 'chk') +
      _R('candidate price min usdt', 'reserved', 'candidate_price_min_usdt', 'candidate_price_min_usdt', 'num', 'step="any" min="0"') +
      _R('candidate price max usdt', 'reserved', 'candidate_price_max_usdt', 'candidate_price_max_usdt', 'num', 'step="any" min="0"') +
      _R('apply suggested budget', 'reserved', 'apply_suggested_budget', 'apply_suggested_budget', 'chk') +
      _R('promote to active', 'reserved', 'promote_to_active', 'promote_to_active', 'chk');
    slots += PLUGS8.filter((p) => !TIER2.includes(p)).map((p) => _sub(p.toUpperCase()) +   // Tier-2(PINGPONG/AUTOLOOP/WHALE)는 각 플러그인 리본서 — 중복 편집·덮어쓰기 방지
      _R('enabled', 'reserved', p + '_enabled', p + '_enabled', 'chk') +
      _R('slots (0~20)', 'reserved', p + '_n', p + '_n', 'num', 'step="1" min="0" max="20"') +
      _R('budget usdt (0=auto)', 'reserved', p + '_budget_usdt', p + '_budget_usdt', 'num', 'step="any" min="0"')).join('');
    slots += _sub('SNIPER(s) SCOPE') + _R('snipers slots (0~20)', 'reserved', 'snipers_n', 'snipers_n', 'num', 'step="1" min="0" max="20"');
    slots += '<div class="rib-row" style="margin-top:10px;align-items:flex-start"><small class="text-muted">🤖 <b>PINGPONG · AUTOLOOP · WHALE</b> 의 ON/OFF · 슬롯 · 예산은 <b>각 플러그인 리본 ⚙️ 슬롯·튜닝</b> 에서 (중복 편집 · 덮어쓰기 방지). 여긴 Tier-1 5종만.</small></div>';
    // ⬇️ Demotion Rules (reserved)
    const demo = _R('BTC Guard Mode', 'reserved', 'btc_guard_mode', 'autopilot.btc_guard_mode', 'chk') +
      _R('No Fills demote', 'reserved', 'autopilot_idle_demote_enabled', 'autopilot.idle_demote_enabled', 'chk') +
      _R('Idle min', 'reserved', 'autopilot_idle_demote_min', 'autopilot.idle_demote_min', 'num', 'step="1" min="0"') +
      _R('→ LongHold (idle)', 'reserved', 'autopilot_idle_to_longhold_enabled', 'autopilot.idle_to_longhold_enabled', 'chk') +
      _R('→ LongHold hours', 'reserved', 'autopilot_idle_to_longhold_hours', 'autopilot.idle_to_longhold_hours', 'num', 'step="1" min="1" max="168"') +
      _R('LongHold Auto Sell', 'reserved', 'longhold_auto_sell', 'autopilot.longhold_auto_sell', 'chk') +
      _R('LongHold 목표 수익률 %', 'reserved', 'longhold_target_pct', 'autopilot.longhold_target_pct', 'num', 'step="any"') +
      _R('LongHold 체크 주기 min', 'reserved', 'longhold_check_interval_min', 'autopilot.longhold_check_interval_min', 'num', 'step="any"') +
      _R('LongHold 손절 기준 %', 'reserved', 'longhold_stop_loss_pct', 'autopilot.longhold_stop_loss_pct', 'num', 'step="any"') +
      _R('Global Profit Take', 'reserved', 'global_profit_take', 'autopilot.global_profit_take', 'chk') +
      _R('기준 목표수익률 %', 'reserved', 'global_profit_pct', 'autopilot.global_profit_pct', 'num', 'step="any"') +
      _R('체크 주기 min', 'reserved', 'global_profit_interval_min', 'autopilot.global_profit_interval_min', 'num', 'step="any"') +
      _R('공통 안전 SL 하한 %', 'reserved', 'global_min_sl_pct', 'autopilot.global_min_sl_pct', 'num', 'step="any"') +
      _R('수익 자동 락인 (부분매도)', 'reserved', 'profit_lock_enabled', 'autopilot.profit_lock_enabled', 'chk') +
      _R('Profit Lock 트리거 %', 'reserved', 'profit_lock_trigger_pct', 'autopilot.profit_lock_trigger_pct', 'num', 'step="any"') +
      _R('Profit Lock 부분매도 비율', 'reserved', 'profit_lock_sell_ratio', 'autopilot.profit_lock_sell_ratio', 'num', 'step="any" min="0.05" max="0.95"') +
      _R('Profit Lock 쿨다운 h', 'reserved', 'profit_lock_cooldown_h', 'autopilot.profit_lock_cooldown_h', 'num', 'step="any"');
    // 🎯 Strategy TP/SL 공통 (reserved → strategy_tp_sl JSON). rk = policy 점경로
    let tpsl = _R('가드 사용', 'tpsl', 'enabled', 'strategy_tp_sl.enabled', 'chk') +
      _R('TP 하한 %', 'tpsl', 'tp_floor_pct', 'strategy_tp_sl.tp_floor_pct', 'num', 'step="any"') +
      _R('SL 하한 %', 'tpsl', 'sl_floor_pct', 'strategy_tp_sl.sl_floor_pct', 'num', 'step="any"') +
      _R('시간완화 사용', 'tpsl', 'time_relax_enabled', 'strategy_tp_sl.time_relax_enabled', 'chk') +
      _R('N 시간 간격', 'tpsl', 'time_relax_step_hours', 'strategy_tp_sl.time_relax_step_hours', 'num', 'step="any"') +
      _R('단계 수', 'tpsl', 'time_relax_steps', 'strategy_tp_sl.time_relax_steps', 'num', 'step="1" min="1" max="24"') +
      _R('TP 단계 감소', 'tpsl', 'time_relax_tp_step', 'strategy_tp_sl.time_relax_tp_step', 'num', 'step="any"') +
      _R('SL 단계 감소', 'tpsl', 'time_relax_sl_step', 'strategy_tp_sl.time_relax_sl_step', 'num', 'step="any"') +
      _R('최저 TP %', 'tpsl', 'time_relax_min_tp_pct', 'strategy_tp_sl.time_relax_min_tp_pct', 'num', 'step="any"') +
      _R('최저 SL %', 'tpsl', 'time_relax_min_sl_pct', 'strategy_tp_sl.time_relax_min_sl_pct', 'num', 'step="any"');
    tpsl += TPSL_STRATS.map((s) => _sub(s + ' (TP / SL)') +
      _R('TP %', 'tpsl', 'per_strategy.' + s + '.tp_pct', 'strategy_tp_sl.per_strategy.' + s + '.tp_pct', 'num', 'step="any"') +
      _R('SL %', 'tpsl', 'per_strategy.' + s + '.sl_pct', 'strategy_tp_sl.per_strategy.' + s + '.sl_pct', 'num', 'step="any"')).join('');
    // 🔫 SNIPER DCA (reserved)
    const dca = _R('추가매수 간격 %', 'reserved', 'sniper_dca_step_pct', 'sniper_dca.dca_step_pct', 'num', 'step="any" min="0.1" max="5"') +
      _R('추가매수 비율', 'reserved', 'sniper_dca_add_ratio', 'sniper_dca.dca_add_ratio', 'num', 'step="any" min="0.1" max="2"') +
      _R('최대 깊이 %', 'reserved', 'sniper_dca_max_depth_pct', 'sniper_dca.dca_max_depth_pct', 'num', 'step="any" min="0.2" max="10"');
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
    ap += _sub('AutoApprove (전략별 + 최소 신뢰도 %)') + PLUGS8.map((p) =>
      _R(p.toUpperCase() + ' approve', 'reserved', 'auto_approve_' + p, 'autopilot.auto_approve_' + p, 'chk') +
      _R(p.toUpperCase() + ' min conf %', 'reserved', 'auto_approve_min_confidence_' + p, 'autopilot.auto_approve_min_confidence_' + p, 'num', 'step="any" min="0" max="100"')).join('');
    // 🚑 Triage / 🛡️ Guard Matrix 섹션 본문 = GET 응답 전 키 자동 렌더 (loadPluginsCommon 가 placeholder 에 주입).
    // ★ 하드코딩 필드 리스트 대신 GET 통째로 펼침 → 누락·중복·잊힘 원천 차단 (부모님 통찰).
    return '<section class="v3-block"><div class="v3-block-head"><span class="v3-block-title">🔌 플러그인 공통 설정 <small class="v3-badge mute">전 설정</small></span>' +
      '<span class="v3-pos-actions"><button class="v3-btn sm ghost" id="v3-pcommon-refresh" title="새로고침">🔄</button></span></div>' +
      '<div class="v3-cset-grid">' +
      _csec('🎰 Slots & Budget', slots, 'reserved', '플러그인별 ON/OFF·슬롯·예산 + 자동배분 + SNIPER(s) scope. 한 마켓=한 전략(중복 선점 자동 방지).') +
      _csec('⬇️ Demotion Rules', demo, 'reserved', '무거래 강등·LongHold 전환·Global Profit Take·수익 자동 락인(profit-lock 연결).') +
      _csec('🎯 Strategy TP/SL 공통', tpsl, 'tpsl', '공통 TP/SL 하한 + 시간완화 + 전략별 TP/SL. 저장 = strategy_tp_sl JSON.') +
      _csec('🔫 SNIPER DCA', dca, 'reserved', 'SNIPER/SNIPER(s) 물타기 간격·비율·깊이.') +
      _csec('📊 Backtest Weights', bt, 'reserved', '실거래 데이터 부족 시 백테스트 반영 비율 (0~1).') +
      _csec('🤖 Autopilot', ap, 'reserved', '자동 운용·승인·강등·전략별 AutoApprove + 최소 신뢰도.') +
      _csec('🚑 Triage Mode', '<div id="v3-auto-triage"></div>', 'triage', '손실 집중 복구 — GET /api/triage/status 전 키 자동 노출 (PATCH 저장).') +
      _csec('🛡️ Guard Matrix (Global)', '<div id="v3-auto-guards"></div>', 'guards', '글로벌 가드 전수 — GET /api/system/guards 전 키 자동 노출. (scope/dust 등은 별도 영역, 추후)') +
      '</div>' +
      '<small class="hint" style="display:block;color:var(--v3-fg-mute);margin-top:10px;">★ 공통 설정 전수 — Reserved + Triage + Guard Matrix 한 곳. Guard/Triage 는 GET 응답의 모든 키를 자동 노출(하드코딩 누락·중복·잊힘 방지). 저장은 섹션별 해당 엔드포인트로.</small></section>';
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
    // 🛡️ Guard Matrix / 🚑 Triage = GET 응답의 모든 키를 placeholder 에 자동 주입 (하드코딩 누락·잊힘 차단)
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

  // 🤖 Tier-2 autopilot 슬롯형 (PINGPONG/AUTOLOOP/WHALE) — 기존 reserved 슬롯 설정만 조립(새 가드 X·새 코드 X).
  //   _R+_csec 재사용 → 저장은 기존 .rsave 핸들러, populate 는 loadPluginsCommon 이 .v3-rin 자동 채움.
  // 🤖 Tier-2 설정 = 리본(슬롯 enable/n/예산 + 고유 튜닝). 다른 플러그인 진입/청산처럼 설정은 리본에. applyActivePanels 가 active 플러그인 것으로 주입.
  function tier2RibbonHtml(key) {
    const cfg = _R('전략 ON/OFF (enable)', 'reserved', key + '_enabled', key + '_enabled', 'chk') +
      _R('슬롯 수 (0=정지·자연 OFF)', 'reserved', key + '_n', key + '_n', 'num', 'step="1" min="0" max="20"') +
      _R('예산 usdt (0=auto)', 'reserved', key + '_budget_usdt', key + '_budget_usdt', 'num', 'step="any" min="0"');
    const hint = '슬롯 켜고(enable) + 슬롯수>0 + 예산 → autopilot 이 알아서 진입·거래(설정 끝). 슬롯 0 = 자연 정지. 작동 코인은 메인에 표시.';
    return '<div class="v3-cset-grid">' + _csec('🔌 ' + (LABEL[key] || key) + ' 슬롯', cfg, 'reserved', hint) +
      '<section class="v3-cset"><div class="v3-cset-h">🎛️ ' + (LABEL[key] || key) + ' 고유 튜닝</div>' +
      (TIER2_TUNE[key] || []).map((f) => '<div class="rib-row"><span>' + f[1] + ' <small>(' + f[0] + ')</small></span><input class="v3-mini" type="number" step="any" id="v3-tune-' + key + '-' + f[0] + '" value="' + f[2] + '"></div>').join('') +
      '<button class="v3-btn v3-btn-long v3-tune-save" data-plug="' + key + '">✓ 튜닝 저장 (슬롯 진입 시 적용)</button>' +
      '<small class="hint" style="display:block;color:var(--v3-fg-mute);margin-top:6px;">autopilot 이 슬롯 잡을 때 이 값으로 진입(빈칸=기본). ★ 튜닝은 서버 재시작 후 적용.</small></section>' +
      '</div>';
  }
  V3.tier2RibbonHtml = tier2RibbonHtml;
  // 🤖 Tier-2 메인 = 작동 현황 (autopilot 이 슬롯 잡아 진입한 코인). 설정은 리본.
  function tier2WorkBlock(key) {
    const w = (V3.state.tier2work && V3.state.tier2work[key]) || null;
    let body;
    if (!w) body = '<div class="v3-placeholder">작동 현황 로딩 중…</div>';
    else if (!w.length) body = '<div class="v3-placeholder">작동 중인 코인 없음 — 상단 리본 <b>⚙️ 슬롯·튜닝</b> 에서 슬롯 켜고(enable)+예산 저장하면 autopilot 이 코인 잡아 여기 띄움.</div>';
    else body = '<table class="v3-postable"><thead><tr><th>Market</th><th>State</th><th>현재가</th><th>예산</th></tr></thead><tbody>' +
      w.map((m) => '<tr><td><b class="v3-mkt" data-bybit="' + m.market + '">' + (m.market || '').replace('USDT', '') + '</b></td><td>' + (m.state || '-') + '</td><td>' + (m.price ? _fp(m.price) : '-') + '</td><td>' + Number(m.budget_usdt || 0).toFixed(0) + ' USDT</td></tr>').join('') + '</tbody></table>';
    return '<section class="v3-block" id="v3-' + key + '-block">' +
      '<div class="v3-block-head"><span class="v3-block-title">' + (ICON[key] || '🤖') + ' ' + (LABEL[key] || key) + ' <small class="v3-badge mute">슬롯형(autopilot)</small></span></div>' +
      '<div style="font-size:11.5px;color:var(--v3-fg-mute);margin:4px 0 8px">🤖 설정은 상단 리본 <b>⚙️ 슬롯·튜닝</b> 에서 / 아래는 autopilot 이 슬롯 잡아 진입한 <b>작동 코인</b>. (전체 포지션은 🏠 Overall Status 에서도)</div>' +
      '<h3 style="font-size:12px;font-weight:normal;color:var(--v3-fg-mute);margin:6px 0 3px">📊 작동 중인 코인</h3>' + body +
      '<h3 style="font-size:12px;font-weight:normal;color:var(--v3-fg-mute);margin:14px 0 3px">🎯 추천 코인 <small>(' + (key === 'pingpong' ? '박스권/횡보' : key === 'autoloop' ? '분할매수·유동성' : '특성') + ' 적합) — 🤖 등록 = autopilot 우선 검토·진입(반자동)</small></h3>' +
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
  // 🎯 Tier-2 추천 코인 (특성 맞춤) + autopilot 우선 등록(반자동)
  function tier2RecoHtml(key) {
    const r = (V3.state.tier2reco && V3.state.tier2reco[key]) || null;
    if (!r) return '<div class="v3-placeholder">추천 로딩 중…</div>';
    if (r.computing) return '<div class="v3-placeholder">추천 계산 중… (수 초 — 자동 갱신)</div>';
    const items = r.items || [];
    if (!items.length) return '<div class="v3-placeholder">추천 후보 없음 — ' + ((r.err) || '잠시 후 다시') + '</div>';
    const rows = items.map((it) => {
      const chg = Number(it.change_rate || 0), score = Number(it.ai_adjusted_score != null ? it.ai_adjusted_score : (it.ai_score || 0)), bud = Number(it.suggested_budget_usdt || 0);
      return '<tr><td><b class="v3-mkt" data-bybit="' + it.market + '">' + (it.market || '').replace('USDT', '') + '</b></td>' +
        '<td>' + _fp(it.price) + '</td>' +
        '<td class="' + (chg >= 0 ? 'v3-pos' : 'v3-neg') + '">' + (chg >= 0 ? '+' : '') + chg.toFixed(2) + '%</td>' +
        '<td>' + (score ? (score * 100).toFixed(0) : '-') + '</td>' +
        '<td>' + (it.rsi != null ? Number(it.rsi).toFixed(0) : '-') + '</td>' +
        '<td>' + (bud ? bud.toFixed(0) + ' USDT' : '-') + '</td>' +
        '<td><button class="v3-btn sm v3-t2-enq" data-key="' + key + '" data-mkt="' + it.market + '" title="autopilot 우선 등록 — 검토(AI·conviction) 후 통과하면 진입">🤖 등록</button></td></tr>';
    }).join('');
    return '<table class="v3-postable"><thead><tr><th>Market</th><th>가격</th><th>변화</th><th>점수</th><th>RSI</th><th>추천예산</th><th></th></tr></thead><tbody>' + rows + '</tbody></table>';
  }
  async function loadTier2Reco(key, force) {
    V3.state.tier2reco = V3.state.tier2reco || {};
    const cur = V3.state.tier2reco[key], now = Date.now();
    if (!force && cur && cur.items && cur.items.length && now - (cur.t || 0) < 600000) { const e0 = $('v3-' + key + '-reco'); if (e0) e0.innerHTML = tier2RecoHtml(key); return; }
    const d = await V3.getJSON('/api/strategy/recommendations?strategy=' + key.toUpperCase() + '&n=10');
    V3.state.tier2reco[key] = { items: (d && d.items) || [], computing: !!(d && d.computing), err: d && (d.detail || d.error), t: now };
    const e1 = $('v3-' + key + '-reco'); if (e1) e1.innerHTML = tier2RecoHtml(key);
    if (d && d.computing && V3.state.selected.has(key)) setTimeout(() => { if (V3.state.selected.has(key)) loadTier2Reco(key, true); }, 4000);   // computing 자동 재시도
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
  // ⚙️ Settings (Common) — Phase 4: 🔌 연결 상태 뷰어 + ✈️ 텔레그램 (전부 기존 /api/system/* 엔드포인트·비밀 표시 X)
  // ════════════════════════════════════════════════════════════
  function settingsBlock() {
    return '<section class="v3-block"><div class="v3-block-head"><span class="v3-block-title">⚙️ Settings (Common)</span>' +
      '<span class="v3-pos-actions"><button class="v3-btn sm ghost" id="v3-set-refresh" title="새로고침">🔄</button></span></div>' +
      '<div class="v3-cset-grid">' +
      '<section class="v3-cset"><div class="v3-cset-h">🔌 연결 상태</div>' +
        '<div class="rib-row"><span>거래소</span><b id="v3-set-exch">' + (new URLSearchParams(location.search).get('ex') === 'binance_futures' ? 'Binance Linear' : 'Bybit Linear') + '</b></div>' +
        '<div class="rib-row"><span>모드</span><span id="v3-set-mode" class="v3-badge mute">…</span></div>' +
        '<div class="rib-row"><span>거래소 API</span><span id="v3-set-api">…</span></div>' +
        '<div class="rib-row"><span>WS / 가격피드</span><span id="v3-set-ws">…</span></div>' +
        '<div class="rib-row"><span>잔고 (equity)</span><b id="v3-set-equity">…</b></div>' +
        '<div class="rib-row"><span>≈ 원화</span><b id="v3-set-equity-krw" class="v3-pos">…</b></div>' +
        '<div class="rib-row"><span>USD/KRW 환율 <small>(자동 30분·수정가능)</small></span><span style="display:flex;align-items:center;gap:6px"><label style="font-size:11px;white-space:nowrap"><input type="checkbox" id="v3-krw-auto" checked> 🔄 자동</label><input class="v3-mini" type="number" step="1" min="0" id="v3-krw-rate" value="1380"></span></div>' +
        '<div class="rib-row"><span>현금 / 투입</span><span id="v3-set-cash">…</span></div>' +
        '<div class="rib-row"><span>Tick</span><span id="v3-set-tick">…</span></div>' +
        '<div class="rib-row"><span>시스템</span><span id="v3-set-health">…</span></div>' +
        '<div class="rib-row"><span>E-STOP</span><span id="v3-set-estop">…</span></div>' +
        '<small class="hint" style="display:block;color:var(--v3-fg-mute);margin-top:6px;">읽기 전용. API키 등 비밀은 표시 안 함.</small></section>' +
      '<section class="v3-cset"><div class="v3-cset-h">✈️ 텔레그램</div>' +
        '<div class="rib-row"><span>연결 상태</span><span id="v3-tg-status">…</span></div>' +
        '<div class="rib-row"><span>새 토큰 <small>(입력만·표시 X)</small></span><input class="v3-mini" type="password" id="v3-tg-token" placeholder="bot 토큰" autocomplete="off"></div>' +
        '<div class="rib-row"><span>chat id</span><input class="v3-mini" type="text" id="v3-tg-chat" placeholder="123456789" autocomplete="off"></div>' +
        '<div class="rib-row"><span>admin 비밀번호 <small>(저장 시)</small></span><input class="v3-mini" type="password" id="v3-tg-admin" placeholder="admin pw" autocomplete="off"></div>' +
        '<div style="display:flex;gap:8px;margin-top:8px"><button class="v3-btn sm ghost" id="v3-tg-test">✈️ 테스트</button><button class="v3-btn v3-btn-long" id="v3-tg-save">✓ 저장</button></div>' +
        '<small class="hint" style="display:block;color:var(--v3-fg-mute);margin-top:6px;">봇은 <b>.env</b>(TELEGRAM_TOKEN/CHAT_ID)에서 읽음. 연결상태=현재 .env 값(마스킹). 테스트=입력값으로 즉시 전송(저장 X). 저장=.env 기록 + 즉시 적용(<b>재시작 불필요</b>, admin 인증).</small></section>' +
      '<section class="v3-cset"><div class="v3-cset-h">🔔 알림 종류 (텔레그램)</div>' +
        '<label class="rib-row"><span>LongHold 알림</span><input type="checkbox" class="v3-cfg-chk" id="v3-alert-longhold"></label>' +
        '<label class="rib-row"><span>Drawdown 알림</span><input type="checkbox" class="v3-cfg-chk" id="v3-alert-drawdown"></label>' +
        '<label class="rib-row"><span>Exit Profit Streak</span><input type="checkbox" class="v3-cfg-chk" id="v3-alert-exit_profit_streak"></label>' +
        '<label class="rib-row"><span>일일 리포트</span><input type="checkbox" class="v3-cfg-chk" id="v3-alert-daily"></label>' +
        '<label class="rib-row"><span>🔱 HARPOON 거래</span><input type="checkbox" class="v3-cfg-chk" id="v3-alert-harpoon"></label>' +
        '<button class="v3-btn v3-btn-long" id="v3-alert-save">✓ 알림 저장</button>' +
        '<small class="hint" style="display:block;color:var(--v3-fg-mute);margin-top:6px;">거래 체결 알림은 <b>모든 전략</b>(FOCUS·LADDER·CONTRARIAN·SNIPER·HARPOON 등) 기본 ON — FOCUS 전용 아님. 위 토글은 <b>추가 알림 종류</b>(LongHold·Drawdown·연속수익·일일리포트·HARPOON요약)만 켜고 끔. 즉시 적용 + .env 저장. 플러그인 <b>신호</b>(미체결) 알림만 OMA_TELEGRAM_SIGNAL_ENABLED(.env, 기본 OFF). Triage 알림은 🔌플러그인 공통 ▸ Triage.</small></section>' +
      '<section class="v3-cset"><div class="v3-cset-h">🛠️ 시스템 액션</div>' +
        '<div style="display:flex;flex-wrap:wrap;gap:6px;margin:4px 0">' +
          '<button class="v3-btn sm ghost" id="v3-sa-reconcile">🔄 잔고 동기화</button>' +
          '<button class="v3-btn sm ghost" id="v3-sa-dust">🧹 Dust 정리</button>' +
          '<button class="v3-btn sm ghost" id="v3-sa-retrain">🧠 AI 재학습</button>' +
          '<button class="v3-btn sm ghost" id="v3-sa-dd-reset">🔧 드로다운 리셋</button>' +
        '</div>' +
        '<div class="rib-row" style="gap:6px;align-items:center"><span>긴급정지</span><span style="display:flex;gap:6px"><button class="v3-btn sm v3-btn-outline-danger" id="v3-sa-estop">🛑 발동</button><button class="v3-btn sm ghost" id="v3-sa-resume">▶️ 해제</button></span></div>' +
        '<div class="rib-row" style="gap:6px;align-items:center;flex-wrap:wrap"><span>서버 <small>(정리 후 실행)</small></span><span style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">' +
          '<label style="font-size:11px;white-space:nowrap"><input type="checkbox" id="v3-srv-cleanup" checked> 정리</label>' +
          '<select id="v3-srv-delay" class="v3-mini" style="width:auto"><option value="5">5초</option><option value="10">10초</option><option value="15" selected>15초</option><option value="30">30초</option><option value="60">60초</option></select>' +
          '<button class="v3-btn sm v3-btn-outline-danger" id="v3-sa-restart">🔁 재시작</button>' +
          '<button class="v3-btn sm v3-btn-outline-danger" id="v3-sa-stop">⏹️ 정지</button>' +
        '</span></div>' +
        '<small class="hint" style="display:block;color:var(--v3-fg-mute);margin-top:6px;">🔄동기화=거래소 잔고↔OMA / 🧹Dust=소액 자투리 정리 / 🧠재학습=AI 모델(수분). 🔧드로다운 리셋=자본 입출금/장기정지 후 생긴 가짜 CRISIS(-30) 해소(다음 tick 적용). 정리=종료 전 포지션·주문 정리 대기(5~60초). 서버 재시작·정지는 run.ps1 필요(미설정 시 정지만).</small></section>' +
      '<section class="v3-cset"><div class="v3-cset-h">👥 옆 서버 (Peer Brief)</div>' +
        '<label class="rib-row"><span>활성화</span><input type="checkbox" id="v3-peer-enabled"></label>' +
        '<label class="rib-row"><span>Paper 모드 <small>(reject 대신 로그만)</small></span><input type="checkbox" id="v3-peer-paper"></label>' +
        '<div class="rib-row"><span>자기 서버 ID</span><input class="v3-mini" type="text" id="v3-peer-server-id" placeholder="ByBit_ServerB"></div>' +
        '<div class="rib-row"><span>검색 주기 <small>(초, 2~3600)</small></span><input class="v3-mini" type="number" min="2" max="3600" step="1" id="v3-peer-poll-sec"></div>' +
        '<div class="rib-row"><span>경계 윈도우 <small>(분, 1~1440 / 옆 SL 차단 지속)</small></span><input class="v3-mini" type="number" min="1" max="1440" step="1" id="v3-peer-sl-min"></div>' +
        '<div class="rib-row"><span>참조 윈도우 <small>(분, 1~1440 / 옆 TP·BE 가점 지속)</small></span><input class="v3-mini" type="number" min="1" max="1440" step="1" id="v3-peer-win-min"></div>' +
        '<div class="rib-row"><span>가점 강도 <small>(점, 0~50 / 옆 win 시 conviction +N)</small></span><input class="v3-mini" type="number" min="0" max="50" step="1" id="v3-peer-win-bonus"></div>' +
        '<div class="rib-row"><span>SL 감점 <small>(점, 0~50 / 옆 같은방향 최근 SL 시 conviction −N)</small></span><input class="v3-mini" type="number" min="0" max="50" step="1" id="v3-peer-sl-pen"></div>' +
        '<div class="rib-row"><span>고전 감점 <small>(점, 0~50 / 옆 같은방향 고전 보유 시 −N)</small></span><input class="v3-mini" type="number" min="0" max="50" step="1" id="v3-peer-struggle-pen"></div>' +
        '<div class="rib-row"><span>고전기준·보유 <small>(분, 1~120 / 이 이상 들고 미해결이면 고전 후보)</small></span><input class="v3-mini" type="number" min="1" max="120" step="1" id="v3-peer-struggle-age"></div>' +
        '<div class="rib-row"><span>고전기준·최고수익 <small>(%, 이 미만 못 오르면 고전)</small></span><input class="v3-mini" type="number" min="0" max="5" step="0.1" id="v3-peer-struggle-peak"></div>' +
        '<div class="rib-row"><span>자가충돌 감점 <small>(점, 0~50 / 옆 반대방향 건강보유 시 −N · 상대가 헤매면 통과=전환포착)</small></span><input class="v3-mini" type="number" min="0" max="50" step="1" id="v3-peer-conflict-pen"></div>' +
        '<div class="rib-row"><span>🌊 몰림 감점 <small>(점, 0~50 / 옆 같은방향 보유 1서버당 −N · soft · default 0=OFF)</small></span><input class="v3-mini" type="number" min="0" max="50" step="1" id="v3-peer-crowding-pen"></div>' +
        '<div class="rib-row"><span>몰림 감점 상한 <small>(점, 1~50 / 여러 서버 몰려도 이 이상 안 깎음 · 기본 12)</small></span><input class="v3-mini" type="number" min="1" max="50" step="1" id="v3-peer-crowding-cap"></div>' +
        '<label class="rib-row"><span>🛡️ 함대 dir_fail <small>(옆+나 같은 코인·방향 누적 손실 N회 → 하드 차단 · default OFF)</small></span><input type="checkbox" id="v3-peer-fleet-dirfail-en"></label>' +
        '<div class="rib-row"><span>함대 차단 N회 <small>(1~10, 옆+나 합산 · 기본 2=2번째부터 막음)</small></span><input class="v3-mini" type="number" min="1" max="10" step="1" id="v3-peer-fleet-dirfail-max"></div>' +
        '<div class="rib-row"><span>함대 윈도우 <small>(분, 1~1440 · 기본 240=4h, 시간차 손실 커버)</small></span><input class="v3-mini" type="number" min="1" max="1440" step="10" id="v3-peer-fleet-dirfail-win"></div>' +
        '<div class="rib-row" style="margin-top:4px"><span>옆 서버 URL <small>(한 줄 1개)</small></span></div>' +
        '<textarea id="v3-peer-urls" rows="3" cols="20" style="width:100%;font-family:monospace;font-size:11px;padding:4px;background:var(--v3-bg);color:var(--v3-fg);border:1px solid var(--v3-bd);border-radius:4px;box-sizing:border-box;resize:vertical" placeholder="http://server-a:8010&#10;http://server-b:8010&#10;http://server-office:8010"></textarea>' +
        '<button type="button" class="v3-btn v3-btn-long" id="v3-peer-save" style="margin-top:6px">✓ 저장 + 즉시 적용</button>' +
        '<div class="rib-row" style="margin-top:8px"><span>현재 polling 상태</span></div>' +
        '<div id="v3-peer-status" style="font-family:monospace;font-size:10px;background:var(--v3-bg);padding:6px;border-radius:4px;border:1px solid var(--v3-bd);min-height:24px;word-break:break-all">…</div>' +
        '<small class="hint" style="display:block;color:var(--v3-fg-mute);margin-top:6px;">5-18 운영자 통찰 — 옆 서버 같은 코인+방향 최근 N분 SL/보유 = 차단, TP·BE·흑자청산 = conviction +N 가점 (본체 가드 그대로). 모든 서버 같은 URL 풀 입력 OK (자기 자동 skip). 저장 = 영속 + polling 재시작. token 은 .env (PEER_BRIEF_TOKEN).</small></section>' +
      '</div></section>';
  }
  async function loadSettings() {
    const [st, hl, tg, al] = await Promise.all([V3.getJSON('/api/system/status'), V3.getJSON('/api/system/health'), V3.getJSON('/api/system/telegram/status'), V3.getJSON('/api/system/alerts')]);
    const setT = (id, html) => { const el = $(id); if (el) el.innerHTML = html; };
    const mode = (st && st.trading_mode) || '?';
    setT('v3-set-mode', '<span class="v3-badge ' + (mode === 'LIVE' ? 'short' : 'mute') + '">' + mode + (mode === 'LIVE' ? ' 🔴' : '') + '</span>');
    const eq = (st && st.equity) || {}, perf = (st && st.performance) || {};
    setT('v3-set-equity', '$' + Number(eq.equity_usdt || 0).toFixed(2));
    if (V3.fetchKrwRate) V3.fetchKrwRate();   // 💱 환율 자동(30분 캐시)
    const _ac = $('v3-krw-auto'); if (_ac) _ac.checked = localStorage.getItem('v3_krw_auto') !== '0';
    const _sr = $('v3-krw-rate'), _saved = localStorage.getItem('v3_krw_rate');
    if (_sr && _saved && document.activeElement !== _sr) _sr.value = _saved;   // 기억된 환율 복원
    const _rate = parseFloat((_sr && _sr.value) || _saved || '1380') || 1380;
    setT('v3-set-equity-krw', '≈ ₩' + Math.round(Number(eq.equity_usdt || 0) * _rate).toLocaleString());
    setT('v3-set-cash', '$' + Number(eq.cash_usdt || 0).toFixed(0) + ' / 투입 $' + Number(eq.deployed_usdt || 0).toFixed(0));
    setT('v3-set-tick', (perf.tick_count != null ? perf.tick_count + ' tick' : '-') + (perf.tick_duration != null ? ' · ' + Math.round(Number(perf.tick_duration) * 1000) + 'ms' : ''));
    setT('v3-set-estop', (st && st.emergency_stop) ? '<span class="v3-badge short">🛑 STOP</span>' : '<span class="v3-pos">정상</span>');
    const ch = (hl && hl.checks) || {};
    const apiOk = ch.exchange_api === 'ok';
    setT('v3-set-api', '<span class="' + (apiOk ? 'v3-pos' : 'v3-neg') + '">' + (apiOk ? '연결됨 ✓' : (ch.exchange_api || '?')) + '</span>');
    const pfAge = ch.price_feed && ch.price_feed.age_sec;
    setT('v3-set-ws', '<span class="' + (ch.websocket === 'ok' ? 'v3-pos' : 'v3-neg') + '">' + (ch.websocket || '?') + '</span>' + (pfAge != null ? ' <small class="text-muted">가격 ' + Math.round(pfAge) + 's 전</small>' : ''));
    const hs = (hl && hl.status) || '?';
    setT('v3-set-health', '<span class="v3-badge ' + (hs === 'healthy' ? 'long' : (hs === 'critical' ? 'short' : 'warn')) + '">' + hs + '</span>');
    setT('v3-tg-status', (tg && tg.has_config) ? ('<span class="v3-pos">연결됨 ✓</span> <small class="text-muted">' + (tg.token_masked || '') + ' · chat ' + (tg.chat_id || '') + '</small>') : '<span class="v3-neg">미설정</span>');
    const av = (al && al.alerts) || {};   // 🔔 알림 종류 토글 채움 (편집 중이면 건너뜀)
    ['longhold', 'drawdown', 'exit_profit_streak', 'daily', 'harpoon'].forEach((k) => { const el = $('v3-alert-' + k); if (el && document.activeElement !== el && av[k] != null) el.checked = !!av[k]; });
    loadPeerSettings();   // 👥 옆 서버 (Peer Brief) 패널 같이 갱신
  }
  V3.loadSettings = loadSettings;

  // ════════════════════════════════════════════════════════════
  // 👥 Peer Brief — 옆 서버 가드 (Settings 패널 안 섹션)
  // ════════════════════════════════════════════════════════════
  async function loadPeerSettings() {
    const s = await V3.getJSON('/peer/settings');
    if (!s || s.ok === false) {
      const st = $('v3-peer-status'); if (st) st.textContent = '/peer/settings 응답 없음';
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
    // 상태 라인
    const cache = s.cache || {};
    const peers = cache.peers || [];
    const lines = [];
    lines.push('나: <b>' + (s.server_id || '?') + '</b> · enabled=' + (s.enabled ? '✓' : '✗') + ' · paper=' + (s.paper ? '✓' : '✗') + ' · SL창 ' + (s.sl_window_min || 30) + '분 · WIN창 ' + (s.peer_win_window_min || 15) + '분/+' + (s.peer_win_bonus || 0) + 'pt · token ' + (s.token_set ? '✓' : '✗'));
    if (peers.length === 0) {
      lines.push('<span style="color:var(--v3-fg-mute)">옆 서버 URL 없음 (단독 모드)</span>');
    } else {
      peers.forEach((p) => {
        const sid = p.server_id || p.url;
        const fresh = p.ok_age_sec >= 0 && p.ok_age_sec < (s.poll_interval_sec || 20) * 4;
        const tag = p.stale ? '<span style="color:var(--v3-warn)">⚠ stale</span>' : (fresh ? '<span style="color:var(--v3-pos)">✓ ' + p.ok_age_sec + 's</span>' : '<span style="color:var(--v3-fg-mute)">대기</span>');
        lines.push(sid + ' — ' + tag + ' · SL=' + (p.recent_losses || 0) + ' · WIN=' + (p.recent_wins || 0) + ' · pos=' + (p.active_positions || 0));
      });
    }
    const st = $('v3-peer-status'); if (st) st.innerHTML = lines.join('<br>');
  }
  V3.loadPeerSettings = loadPeerSettings;

  // 💱 USD/KRW 환율 자동 fetch (30분 캐시 · open.er-api.com 무료·CORS 허용). 실패=수동값 유지. v3_krw_auto='0'=수동.
  V3.krwRate = function () { return parseFloat(localStorage.getItem('v3_krw_rate') || '1380') || 1380; };
  async function fetchKrwRate(force) {
    if (localStorage.getItem('v3_krw_auto') === '0') return;   // 수동 모드 = 자동 갱신 안 함
    const now = Date.now(), lastT = parseInt(localStorage.getItem('v3_krw_rate_t') || '0', 10) || 0;
    if (!force && now - lastT < 1800000) return;   // 30분 캐시
    try {
      const r = await fetch('https://open.er-api.com/v6/latest/USD', { cache: 'no-store' });
      const d = await r.json();
      const krw = d && d.rates && Number(d.rates.KRW);
      if (krw && krw > 500 && krw < 3000) {   // sanity (원/달러 정상범위)
        localStorage.setItem('v3_krw_rate', String(Math.round(krw)));
        localStorage.setItem('v3_krw_rate_t', String(now));
        const inp = $('v3-krw-rate'); if (inp && document.activeElement !== inp) inp.value = Math.round(krw);
        if (V3.state.envView && V3.loadSettings) V3.loadSettings();   // 설정 화면이면 ≈원화 재계산
        const he = $('v3-home-equity'); if (he && V3.state.homeView && V3.state.home.pos) he.innerHTML = homeEquityHtml(V3.state.home.pos.sys);
      }
    } catch (e) { /* offline/CORS 실패 → 수동값 유지 */ }
  }
  V3.fetchKrwRate = fetchKrwRate;

  // ── 🏠 홈 = 종합 현황 (포지션·후보, 이후 퀵트레이드·김프·환율 추가) ──
  // ⏰ Event Shield 카운트다운 (헤더 Tick·API·🧭 우측, 부모님 2026-06-08) — 매초 똑딱
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
    let evTs = null;
    for (const lab of es.events) {
      const t = Date.parse(String(lab).replace(' ', 'T') + ':00+09:00');   // KST 명시 → 타임존 무관 정확
      if (isNaN(t)) continue;
      if (t + winMs >= now) { evTs = t; break; }   // 後 윈도우 안 지난 가장 이른 이벤트
    }
    if (evTs == null) return '';
    const startMs = evTs - winMs - leadMs, endMs = evTs + winMs;   // 前 = window+lead(슬리피지 리드)
    if (now >= startMs && now <= endMs) {   // SHIELD ON
      return '<span title="Event Shield 작동 중 — 신규진입 차단·SL 조임" style="color:#dc3545;font-weight:600">⏰ SHIELD ON · -' + _esFmt(endMs - now) + '</span>';
    }
    const toStart = startMs - now;
    const col = toStart < 3600000 ? '#ffc107' : 'var(--v3-fg-mute)';   // 차단 시작 1h 내 = 노랑
    return '<span title="다음 경제이벤트 — 前 ' + ((es.window_min || 0) + (es.lead_min || 0)) + '분부터 차단 시작 (군중보다 먼저)" style="color:' + col + '">⏰ 이벤트 ' + _esFmt(evTs - now) + '</span>';
  };
  try {
    setInterval(function () {
      const inner = V3.evShieldInner((V3.lastStatus && V3.lastStatus.event_shield) || null);
      document.querySelectorAll('.v3-evshield').forEach(function (el) { el.innerHTML = inner; });
    }, 1000);
  } catch (e) { /* noop */ }

  // v2 Overview식 얇은 status line (용어=영어 유지, 메모리 원칙) — Engine·Active·Ready·Total·Free·Avail·PnL·Tick·API
  function homeStatusHtml(d) {
    const s = (d && d.system) || {}, eq = s.equity || {}, perf = s.performance || {}, api = s.api_stats || {};
    const oma = s.oma || {}, active = (oma.active || []).length, ready = (oma.watch || []).length;
    const total = Number(eq.equity_usdt || 0), free = Number(eq.cash_usdt || 0), avail = free * Number(eq.deploy_ratio || 1);
    const spnl = Number(s.session_pnl || 0), base = Number(s.pnl_baseline || 0);
    const tickN = perf.tick_count != null ? perf.tick_count : '-', tickMs = perf.tick_duration != null ? (Number(perf.tick_duration) * 1000).toFixed(1) : '-';
    const apiN = api.calls_per_min != null ? api.calls_per_min : '-', estop = s.emergency_stop, mode = s.trading_mode || '?';
    const tMs = Number(perf.tick_duration || 0) * 1000, alive = perf.tick_count != null;   // alive=엔진 tick 존재 (×1000: 초→ms)
    const tCol = !alive ? '#8a8f99' : tMs > 1000 ? '#dc3545' : tMs > 500 ? '#ffc107' : '#28a745';   // 회색=멈춤 / >1000 빨강 / >500 노랑 / else 초록
    const dot = (col, blink) => '<span class="' + (blink ? 'v3-blink' : '') + '" style="display:inline-block;width:8px;height:8px;border-radius:50%;background:' + col + ';box-shadow:0 0 6px 1px ' + col + '88;margin-right:5px;vertical-align:middle"></span>';
    const tickDot = '<span class="' + (alive ? 'v3-tick-beat' : '') + '" style="display:inline-block;width:12px;height:12px;border-radius:50%;background:' + tCol + ';box-shadow:0 0 9px 2px ' + tCol + '99;margin-right:6px;vertical-align:middle"></span>';
    const sep = '<span style="color:var(--v3-fg-mute);margin:0 9px">·</span>';
    const mut = (k) => '<span style="color:var(--v3-fg-mute)">' + k + '</span> ';
    const _mc = (V3.lastStatus && V3.lastStatus.macro_compass) || null;
    const _mcCol = _mc ? ({ RISK_OFF: '#dc3545', RECOVERING: '#ffc107', RISK_ON: '#28a745', NEUTRAL: 'var(--v3-fg-mute)' }[_mc.state] || 'var(--v3-fg-mute)') : '';
    const _mcHtml = _mc ? (sep + '<span title="거시 레짐 나침반 — 대표10 급락/회복 전환 (표시전용·진입무영향)" style="color:' + _mcCol + '">🧭 ' + _mc.label + '</span>') : '';
    const _esInner = V3.evShieldInner ? V3.evShieldInner((V3.lastStatus && V3.lastStatus.event_shield) || null) : '';
    const _evHtml = _esInner ? (sep + '<span class="v3-evshield">' + _esInner + '</span>') : '';
    return '<div style="font-size:12.5px;padding:4px 0 6px;line-height:2.1">' +
      dot(estop ? '#dc3545' : '#28a745', true) + (estop ? 'Stopped' : 'Running') + ' ' + (s.engine || 'NUNNAYA') + sep +
      '<span class="v3-badge ' + (mode === 'LIVE' ? 'short' : 'mute') + '">' + mode + '</span>' + sep +
      mut('Active') + active + sep + mut('Ready') + ready + sep +
      mut('Total') + total.toFixed(0) + ' USDT' + sep + mut('Free') + free.toFixed(0) + ' USDT' + sep + mut('Avail') + avail.toFixed(0) + ' USDT' + sep +
      mut('PnL') + '<span class="' + V3.pnlCls(spnl) + '">' + (spnl >= 0 ? '+' : '') + spnl.toFixed(2) + ' USDT</span> <span style="color:var(--v3-fg-mute)">(기준 ' + base.toFixed(0) + ' USDT)</span>' + sep +
      tickDot + mut('Tick') + '<span style="color:' + tCol + ';font-size:13.5px">' + tickN + '·' + tickMs + 'ms</span>' + sep + mut('API') + apiN + _mcHtml + _evHtml +
      (estop ? sep + '<span style="color:var(--v3-neg)">🛑 E-STOP</span>' : '') + '</div>';
  }
  function homeCard(label, value, sub, valCls) {
    return '<div style="flex:1;min-width:130px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);border-radius:8px;padding:9px 14px">' +
      '<div style="font-size:10px;color:var(--v3-fg-mute);text-transform:uppercase;letter-spacing:.5px">' + label + '</div>' +
      '<div style="font-size:21px;margin-top:3px" class="' + (valCls || '') + '">' + value + '</div>' +
      (sub ? '<div style="font-size:11px;color:var(--v3-fg-mute);margin-top:1px">' + sub + '</div>' : '') + '</div>';
  }
  function homeCardsHtml(d) {   // v2식 큰 숫자 카드 4개 (총자산·Session PnL·Active·Reserved)
    const s = (d && d.system) || {}, eq = s.equity || {};
    const total = Number(eq.equity_usdt || 0), spnl = Number(s.session_pnl || 0);
    const active = ((s.oma || {}).active || []).length;
    const reco = (V3.state.home.reco && V3.state.home.reco.items) ? V3.state.home.reco.items.length : null;
    return '<div style="display:flex;flex-wrap:wrap;gap:10px;margin:8px 0 4px">' +
      homeCard('총 자산', total.toFixed(2) + ' USDT', '≈ ₩' + Math.round(total * V3.krwRate()).toLocaleString()) +
      homeCard('Session PnL', (spnl >= 0 ? '+' : '') + spnl.toFixed(2) + ' USDT', '기준 ' + Number(s.pnl_baseline || 0).toFixed(0) + ' USDT <button class="v3-btn ghost" id="v3-home-baseline" style="padding:0 6px;font-size:10px;margin-left:4px;vertical-align:middle" title="현재 자산을 PnL 기준점으로 리셋">💾 리셋</button>', V3.pnlCls(spnl)) +
      homeCard('Active 마켓', String(active)) +
      homeCard('Reserved 후보', reco != null ? String(reco) : '…') +
      '</div>';
  }
  function homeEquityHtml(d) { return homeStatusHtml(d) + homeCardsHtml(d); }   // 호출부 호환 래퍼
  function homePosHtml(d) {
    const fpos = (d && d.focus && d.focus.positions) || [];
    const sys = (d && d.sys && d.sys.system) || {}, oma = sys.oma || {}, prices = sys.active_prices || {};
    const omaAll = [].concat(oma.active || [], oma.recovery || []);
    const fmkts = new Set(fpos.map((p) => p.market));
    const rows = [];
    fpos.forEach((p) => rows.push('<tr>' + posCells(p, 'FOCUS') + '</tr>'));   // FOCUS = 풀 필드 (posCells 공유 → FOCUS 표와 동일 모양)
    omaAll.filter((m) => m.market && !fmkts.has(m.market)).forEach((m) => {     // 타 전략 관리마켓 = 슬롯 정보만(margin=예산), 나머지 —
      const pr = prices[m.market], mShort = (m.market || '').replace('USDT', '');
      rows.push('<tr style="opacity:.7">' +
        '<td><b class="v3-mkt" data-bybit="' + m.market + '">' + mShort + '</b> <small style="color:var(--v3-fg-mute)">' + (m.strategy || '?') + '</small></td>' +
        '<td><small class="text-muted">관리</small></td>' +
        '<td><small class="text-muted">' + Number(m.budget_usdt || 0).toFixed(0) + ' USDT</small></td>' +
        '<td>—</td><td>' + (pr ? _fp(pr) : '—') + '</td>' +
        '<td><small class="text-muted">—</small></td><td>—</td><td>—</td>' +
        '<td><small class="text-muted">—</small></td><td>—</td></tr>');
    });
    if (!rows.length) return '<div class="v3-placeholder">보유 포지션 없음 · 관리 중인 마켓 없음</div>';
    return '<table class="v3-postable v3-postable-pos"><thead><tr><th>Market</th><th>Dir</th><th>Margin</th><th>Entry</th><th>Current</th><th>PnL</th><th>TP1</th><th>SL</th><th>Progress</th><th>Hold</th></tr></thead><tbody>' + rows.join('') + '</tbody></table>';
  }
  function homeRecoHtml(r) {
    const items = (r && r.items) || [];
    if (!items.length) return '<div class="v3-placeholder">추천 후보 없음 — ' + ((r && (r.detail || r.error)) || '스냅샷 비어있음 (07:00 KST 기준 갱신)') + '</div>';
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
    return '<table class="v3-postable"><thead><tr><th>Market</th><th>전략</th><th>점수</th><th>RSI</th><th>변화</th><th>추천예산</th></tr></thead><tbody>' + rows + '</tbody></table>' +
      '<small class="hint">' + (r.cached ? '캐시' : '갱신') + ' · 기준 ' + (r.basis_kst || '07:00') + ' KST · ' + (r.created_at_kst || '') + ' — 코인 클릭=Bybit · 배포는 각 전략 진입 탭에서</small>';
  }
  function homeQuickHtml() {   // ⚡ 즉시 시장가 퀵트레이드 (POST /api/trade/quick) — 실거래!
    return '<div class="v3-ltg-form" style="margin:6px 0"><div class="v3-ltg-form-row">' +
      '<div class="fld"><label class="v3-label">Market</label><input id="v3-qt-market" class="v3-input" type="text" placeholder="BTCUSDT" list="v3-market-list" autocomplete="off"></div>' +
      '<div class="fld"><label class="v3-label">금액 기준</label><select id="v3-qt-mode" class="v3-input"><option value="quote">USDT</option><option value="percent">비율 (%)</option></select></div>' +
      '<div class="fld"><label class="v3-label">값</label><input id="v3-qt-amount" class="v3-input" type="number" value="10" min="0" step="1"></div>' +
      '<div class="fld"><label class="v3-label">가드</label><select id="v3-qt-guard" class="v3-input"><option value="global">가드 적용</option><option value="entry_limit_only">진입제한만</option><option value="force">강제(무시)</option></select></div>' +
      '<button class="v3-btn v3-btn-long v3-qt-side" data-side="buy">🟢 매수</button>' +
      '<button class="v3-btn v3-btn-outline-danger v3-qt-side" data-side="sell">🔴 매도</button>' +
      '</div><small class="hint">⚡ 즉시 시장가 주문 (실거래!). 금액기준 USDT=절대금액 / 비율=가용현금 %. 가드 적용=글로벌 진입가드 / 강제=무시.</small></div>';
  }
  function homeBlock() {
    const sh = V3.state.home.show || {};
    const h3 = (t, sub) => '<h3 style="font-size:12px;font-weight:normal;color:var(--v3-fg-mute);margin:14px 0 3px">' + t + ' <small>' + sub + '</small></h3>';
    let h = '<section class="v3-block" id="v3-home-block">';
    h += '<div class="v3-block-head" style="display:flex;align-items:center;justify-content:space-between"><h2 style="margin:0;font-size:15px;font-weight:normal">🏠 Overall Status</h2><button class="v3-btn sm ghost" id="v3-home-refresh">↻ 새로고침</button></div>';
    if (sh.status !== false) h += '<div id="v3-home-equity">' + (V3.state.home.pos ? homeEquityHtml(V3.state.home.pos.sys) : '') + '</div>';
    if (sh.quick !== false) { h += h3('⚡ 퀵 트레이드', '(즉시 시장가 · 실거래)'); h += homeQuickHtml(); }
    if (sh.positions !== false) { h += h3('📊 현재 포지션 현황', '(FOCUS 상세 + 타 전략 관리마켓)'); h += '<div id="v3-home-pos">' + (V3.state.home.pos ? homePosHtml(V3.state.home.pos) : '<div class="v3-placeholder">불러오는 중…</div>') + '</div>'; }
    if (sh.reco !== false) { h += h3('🎯 전략별 추천 후보', '(전 전략 종합 스냅샷)'); h += '<div id="v3-home-reco">' + (V3.state.home.reco ? homeRecoHtml(V3.state.home.reco) : '<div class="v3-placeholder">불러오는 중…</div>') + '</div>'; }
    if (sh.status === false && sh.quick === false && sh.positions === false && sh.reco === false) h += '<div class="v3-placeholder">우측 Widgets 에서 표시할 항목을 선택하세요</div>';
    return h + '</section>';
  }
  async function loadHome(force) {
    const hm = V3.state.home, now = Date.now();
    if (hm.loading && !force) return;
    hm.loading = true;
    try {
    fetchKrwRate(force);   // 💱 환율 자동(30분 캐시) — non-blocking
    const sys = await V3.getJSON('/api/system/status', { timeoutMs: 5000 });
    hm.pos = { focus: V3.lastStatus, sys: sys };
    const pe = $('v3-home-pos'); if (pe) pe.innerHTML = homePosHtml(hm.pos);
    const eqEl = $('v3-home-equity'); if (eqEl) eqEl.innerHTML = homeEquityHtml(sys);
    if (force || !hm.reco || now - hm.t > 300000) {   // 추천 스냅샷은 5분 캐시(매 폴링마다 안 부름)
      const r = await V3.getJSON('/api/recommend/snapshot?n=12', { timeoutMs: 8000 });
      hm.reco = r; hm.t = now;
      const re = $('v3-home-reco'); if (re) re.innerHTML = homeRecoHtml(r);
    }
    } finally {
      hm.loading = false;
    }
  }
  V3.loadHome = loadHome;

  // 우측 토글된 위젯 row (전략 뷰 + 🏠 Overall Status 공용 — 2026-06-07 부모)
  function buildWidgetsRow(homeOnly) {
    const w = V3.state.widgets || {};
    let ord = (V3.state.widgetOrder && V3.state.widgetOrder.length) ? V3.state.widgetOrder.slice() : Object.keys(_wreg);
    Object.keys(_wreg).forEach((k) => { if (ord.indexOf(k) < 0) ord.push(k); });   // 새 위젯 누락 방지
    if (homeOnly) ord = ['peer', 'journal'].filter((k) => ord.indexOf(k) >= 0);   // Overall Status 기본 = 차단 관제(Peer) 먼저
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

  // 메인 = 선택된 전략 블록 스택 (체크 상태 보존하며 재렌더)
  function renderMain() {
    const el = $('v3-trade'); if (!el) return;
    if (V3.state.homeView) { el.innerHTML = homeBlock() + buildWidgetsRow(true); loadHome(); if (V3.loadEnabledWidgets) V3.loadEnabledWidgets(); return; }   // 🏠 Overall Status = FOCUS 결과(Journal·Peer)만 표시 (2026-06-07 부모)
    if (V3.state.pcommonView) { el.innerHTML = pluginsCommonBlock(); return; }
    if (V3.state.envView) { el.innerHTML = settingsBlock(); loadSettings(); return; }
    // ★ [2026-06-02 부모] 레이어 순서 = 본문 표 위치 — order(사용자 정렬) 우선 + selected 필터 + 방어(order에 없는 selected append)
    const sel = V3.state.order.filter((n) => V3.state.selected.has(n));
    V3.state.selected.forEach((n) => { if (!sel.includes(n)) sel.push(n); });
    if (!sel.length) { el.innerHTML = '<div class="v3-placeholder">좌측 스위치로 전략을 선택하세요 (다중 선택 = 메인에 같이 표시)</div>'; return; }
    const checked = new Set(Array.from(el.querySelectorAll('.focus-pos-chk:checked')).map((c) => c.dataset.market));
    const _meMkt = ($('v3-me-market') || {}).value, _meTo = ($('v3-me-timeout') || {}).value;   // 🖐 수동진입 입력 보존(5s 재렌더)
    const _meFocus = document.activeElement && document.activeElement.id === 'v3-me-market';
    // ★ [2026-06-02 부모] 전략 화면 상단 status bar — 자산/PnL/Tick "쓔웅" (home은 기존에 있어 중복이라 제외)
    const _sbar = V3.lastSys ? ('<div class="v3-strat-statusbar">' + homeStatusHtml(V3.lastSys) + '</div>') : '';
    el.innerHTML = _sbar + sel.map((n, _i) => {
      const _blk = n === 'focus' ? focusBlock(V3.lastStatus) : n === 'harpoon' ? harpoonBlock(V3.state.harpoon) : n === 'lightning' ? lightningBlock({ items: V3.state.lightning.items }) : n === 'sniper' ? sniperBlock({ items: V3.state.sniper.items }) : PLUG[n] ? plugBlock(n, { items: plugState(n).items }) : n === 'ladder' ? ladderBlock({ items: V3.state.ladder.items }) : TIER2.includes(n) ? tier2WorkBlock(n) : stubBlock(n);
      const _mv = sel.length > 1 ? ('<div class="v3-layer-mv"><button class="v3-lmv" data-layer="' + n + '" data-dir="-1"' + (_i === 0 ? ' disabled' : '') + ' title="위로">▲</button><button class="v3-lmv" data-layer="' + n + '" data-dir="1"' + (_i === sel.length - 1 ? ' disabled' : '') + ' title="아래로">▼</button></div>') : '';
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
    sel.forEach((n) => { if (PLUG[n]) loadPlug(n); });   // generic 플러그인(GAZUA 등) 활성목록 갱신
    if (V3.state.selected.has('ladder')) loadLadder();   // 📐 읽기 전용
    if (sel.some((n) => TIER2.includes(n))) { loadTier2Work(); sel.forEach((n) => { if (TIER2.includes(n)) loadTier2Reco(n); }); }   // 🤖 메인=작동 현황 + 추천 코인 (설정 리본은 applyActivePanels 가 주입)
  }
  V3.renderMain = renderMain;

  // ── 상태 폴링 (fallback) + WS 가 즉시 트리거 ──
  async function pollStatus() {
    if (V3._pollingStatus) return;
    V3._pollingStatus = true;
    try {
    const d = await V3.getJSON('/api/strategy/focus/status', { timeoutMs: 4500 });
    // ★ [2026-06-19 부모 "10초마다 전체 암흑"] 성공(ok)일 때만 갱신 — getJSON 은 timeout/실패 시
    //   {ok:false} 를 반환하는데, 그걸로 직전 좋은 status 를 덮어쓰면 renderMain 이 '상태 로딩 중'으로
    //   패널을 통째 비운다. 실패 시 직전 데이터를 유지(stale-while-revalidate)해 깜빡임 자체를 없앤다.
    if (d && d.ok) V3.lastStatus = d;
    else if (!V3.lastStatus) V3.lastStatus = d;   // 최초 로드(아직 성공 이력 없음)일 때만 로딩 표시 허용
    if (d && d.ok && d.config && V3.syncEntryConfig) V3.syncEntryConfig(d.config);
    // ★ [2026-06-02 부모] 좌트리 가동표시 — 전략 enabled(초록 노브) 매 폴링 갱신 (system status, 2s 캐시)
    try {
      const _ss = await V3.getJSON('/api/system/status', { timeoutMs: 4500 });
      if (_ss && _ss.system) V3.lastSys = _ss;   // ★ [2026-06-02 부모] 전략 화면 상단 status bar 용
      const _se = _ss && _ss.system && _ss.system.strategies;
      if (_se) { V3.state.stratEnabled = _se; updateTreeUI(); }
    } catch (_e) { /* status 일시 실패 무시 — 다음 폴링서 복구 */ }
    if (!V3.state.envView && !V3.state.pcommonView) {
      // LIGHTNING/SNIPER active = 본문에 배포 폼/추천 입력 있음 → poll 통째 재렌더 스킵(입력 보존), 활성목록만 갱신
      if (V3.state.homeView) loadHome();   // 🏠 홈은 in-place 갱신(포지션/자산 span 만)
      else if (V3.state.active === 'lightning' && V3.state.selected.has('lightning')) loadLightning();
      else if (V3.state.active === 'sniper' && V3.state.selected.has('sniper')) loadSniper();
      else if (PLUG[V3.state.active] && V3.state.selected.has(V3.state.active)) loadPlug(V3.state.active);
      else if (V3.state.active === 'ladder' && V3.state.selected.has('ladder')) loadLadder();
      else if (TIER2.includes(V3.state.active) && V3.state.selected.has(V3.state.active)) loadTier2Work();   // 🤖 작동 현황 갱신(설정 리본은 그대로)
      else renderMain();
    }
    else if (V3.state.envView) loadSettings();   // ⚙️ Settings 연결상태 갱신(입력 보존·status span 만 갱신)
    } finally {
      V3._pollingStatus = false;
    }
  }
  V3.pollStatus = pollStatus;

  // ── 좌 트리: 다중 선택 (스위치 = 메인 표시) ──
  function updateTreeUI() {
    const _en = V3.state.stratEnabled || {};   // ★ [2026-06-02 부모] 전략 가동(enabled) — 토글 노브 초록
    document.querySelectorAll('.v3-strat').forEach((row) => {
      const name = row.dataset.strat;
      const sel = !V3.state.homeView && !V3.state.envView && !V3.state.pcommonView && V3.state.selected.has(name);
      const isCommon = name === 'env' ? V3.state.envView : name === 'plugins-common' ? V3.state.pcommonView : name === 'home' ? V3.state.homeView : false;
      row.classList.toggle('active', (name === 'env' || name === 'plugins-common' || name === 'home') ? isCommon : sel);
      row.classList.toggle('is-active', !V3.state.homeView && !V3.state.envView && !V3.state.pcommonView && name === V3.state.active);   // 상단 리본+우측이 따라오는 전략
      row.classList.toggle('live', !!_en[name]);   // ★ 가동중 = 토글 노브 초록 (메인표시 선택과 별개)
      const inp = row.querySelector('.v3-toggle input');
      if (inp) inp.checked = sel;
    });
    // 상단 레일 칩 상태: is-active = 상단 리본+우측이 이 전략 / selected = 메인 스택에 표시 중
    document.querySelectorAll('.v3-srail').forEach((c) => {
      const n = c.dataset.strat;
      c.classList.toggle('is-active', n === 'env' ? V3.state.envView : (!V3.state.envView && n === V3.state.active));
      c.classList.toggle('selected', !V3.state.envView && n !== 'env' && V3.state.selected.has(n));
    });
    const cur = $('v3-cur-strat');
    if (cur) cur.textContent = V3.state.homeView ? 'Overall Status' : V3.state.envView ? 'Settings' : (TREE_ORDER.filter((n) => V3.state.selected.has(n)).map((n) => LABEL[n]).join(', ') || '—');
  }
  // 전략 선택 = 트리 행 + 상단 레일 칩 공통 (클릭한 전략 → active = 상단 리본 + 우측 위젯이 따라옴)
  // ★ [2026-06-21 부모] GAZUA = 현물 엔진. 클릭 시 거래소별 현물 대시보드를 각각 1탭(named window
  //   = 중복 X·현재 탭 덮어쓰기 X)으로 연다. 새 거래소(바이낸스 등) 추가 = 이 배열에 한 줄.
  //   상대경로라 어느 서버의 futures 대시보드든 *그 서버*의 현물 UI 를 엶.
  const GAZUA_SPOT_DASHBOARDS = [
    { name: 'gz_upbit',      url: '/ui/dashboard_upbit_v3.html' },
    { name: 'gz_bithumb',    url: '/ui/dashboard_bithumb_v3.html' },
    { name: 'gz_bybit_spot', url: '/ui/dashboard_bybit_spot_v3.html' },
    { name: 'gz_binance', url: '/ui/dashboard_binance_spot_v3.html' },   // 2026-06-23 연결
  ];
  function openGazuaDashboards() {
    GAZUA_SPOT_DASHBOARDS.forEach((d) => { try { window.open(d.url, d.name); } catch (e) { /* 팝업 차단 등 무시 */ } });
  }
  // ★ [2026-06-21 부모] GAZUA·CONTRARIAN rail 점등 — 현물 작동중 거래소를 favicon급 아이콘으로(토글 대체).
  //   읽기전용·거래무관. /spot_gazua_cross/control(서버 15s 캐시) 1콜. gazua=가동이면 점등 / 역행=가동+역행ON.
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
    if (name === 'gazua') { openGazuaDashboards(); return; }   // ★ GAZUA = 현물 대시보드 런처 (선물 플러그인 패널 대신)
    if (name === 'contrarian') { openGazuaDashboards(); return; }   // ★ [2026-06-21] 역행=현물 통일 — 현물 대시보드 런처(거기 역행 메뉴)
    if (name === 'home') { V3.state.homeView = true; V3.state.envView = false; V3.state.pcommonView = false; updateTreeUI(); applyActivePanels(); renderMain(); return; }
    if (name === 'env') { V3.state.envView = true; V3.state.pcommonView = false; V3.state.homeView = false; updateTreeUI(); applyActivePanels(); renderMain(); return; }
    if (name === 'plugins-common') { V3.state.pcommonView = true; V3.state.envView = false; V3.state.homeView = false; updateTreeUI(); applyActivePanels(); renderMain(); loadPluginsCommon(true); return; }
    V3.state.envView = false; V3.state.pcommonView = false; V3.state.homeView = false;
    if (V3.state.selected.has(name)) { V3.state.selected.delete(name); V3.state.order = V3.state.order.filter((x) => x !== name); }
    else { V3.state.selected.add(name); if (!V3.state.order.includes(name)) V3.state.order.push(name); }   // ★ [2026-06-02 부모] order 동기화 (레이어 순서)
    if (V3.state.selected.size === 0) { V3.state.selected.add('focus'); if (!V3.state.order.includes('focus')) V3.state.order.push('focus'); }
    // active = 방금 클릭한 전략(스택에 남으면) / 빠졌으면 남은 마지막 선택 전략
    V3.state.active = V3.state.selected.has(name) ? name : (TREE_ORDER.filter((n) => V3.state.selected.has(n)).slice(-1)[0] || 'focus');
    updateTreeUI(); applyActivePanels(); renderMain();
  }
  V3.selectStrat = selectStrat;
  document.querySelectorAll('.v3-strat').forEach((row) => row.addEventListener('click', () => selectStrat(row.dataset.strat)));
  // 개별 거래소 점등 클릭 = 그 거래소 현물 대시보드만 (행 클릭 전파 차단)
  document.querySelectorAll('.gz-ex .gzx').forEach((dot) => dot.addEventListener('click', (ev) => {
    ev.stopPropagation();
    const url = { upbit: '/ui/dashboard_upbit_v3.html', bithumb: '/ui/dashboard_bithumb_v3.html', bybit_spot: '/ui/dashboard_bybit_spot_v3.html', binance: '/ui/dashboard_binance_spot_v3.html' }[dot.dataset.ex];
    if (url) { try { window.open(url, 'gz_' + dot.dataset.ex); } catch (e) { /* 무시 */ } }
  }));

  // 상단 전략 레일 — 모든 전략 아이콘+첫글자 칩 (hover=풀네임 · 클릭=전환). active 아닌 전략도 흔적으로 남음 (부모님).
  function buildStratRail() {
    const rail = $('v3-strat-rail'); if (!rail) return;
    rail.innerHTML = TREE_ORDER.concat(['env']).map((n) => {
      const ic = n === 'env' ? '⚙️' : (ICON[n] || '•');
      const ab = n === 'env' ? '' : (LABEL[n] || n).charAt(0);
      return '<button class="v3-srail" data-strat="' + n + '" title="' + (LABEL[n] || n) + '"><span class="ic">' + ic + '</span>' + (ab ? '<span class="ab">' + ab + '</span>' : '') + '</button>';
    }).join('');
    rail.querySelectorAll('.v3-srail').forEach((c) => c.addEventListener('click', () => selectStrat(c.dataset.strat)));
  }

  // ── 위임 클릭: Engine 토글 / Positions 액션 / 마켓→Bybit ──
  document.addEventListener('click', async (e) => {
    // ★ [2026-06-02 부모] 레이어 순서 ▲▼ — order swap → 본문 표 위치 이동 (포토샵 레이어식)
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
    // ₿ 로고 클릭 = 대시보드 자기자신 새로고침 (다시 불러오기)
    if (e.target.closest('#v3-logo')) { location.reload(); return; }
    // 📓 Journal 페이지 넘김 / 새로고침 · 📊 BTC 보조표 새로고침
    const jp = e.target.closest('.v3-journal-page');
    if (jp) { V3.state.journal.page = parseInt(jp.dataset.page, 10) || 1; loadJournal(true); return; }
    if (e.target.closest('#v3-journal-refresh') || e.target.closest('#v3-daily-refresh')) { loadJournal(true); return; }
    if (e.target.closest('#v3-home-refresh')) { loadHome(true); return; }   // 🏠 홈 새로고침(추천 스냅샷 강제 갱신)
    if (e.target.closest('#v3-home-baseline')) {   // 💾 PnL 기준점 리셋 (입금 반영 — 금액 직접 입력 / 비우면 현재 자산)
      const inp = prompt('💾 PnL 기준 금액 (USDT)\n\n· 입금했으면 입금 포함 금액을 입력\n· 비우면 현재 자산을 기준점으로 리셋', '');
      if (inp === null) return;   // 취소
      let _q = '';
      const _s = String(inp).trim();
      if (_s !== '') {
        const _v = parseFloat(_s);
        if (isNaN(_v) || _v <= 0) { V3.toast('✗ 올바른 금액을 입력하세요 (또는 비우면 현재 자산)', 'err', 4000); return; }
        _q = '?baseline=' + _v;
      }
      const r = await V3.getJSON('/api/system/pnl-baseline/reset' + _q, { method: 'POST' });
      V3.toast((r && r.ok) ? ('✓ PnL 기준점 → ' + Number(r.baseline || 0).toFixed(2) + ' USDT' + (r.source === 'manual_input' ? ' (입력값)' : ' (현재자산)')) : ('✗ 실패: ' + ((r && (r.detail || r.error)) || '알 수 없음')), (r && r.ok) ? 'ok' : 'err', 5000);
      if (r && r.ok) loadHome(true);
      return;
    }
    // ⚡ 퀵 트레이드 — 즉시 시장가 매수/매도 (POST /api/trade/quick, 실거래!)
    const qtSide = e.target.closest('.v3-qt-side');
    if (qtSide) {
      const side = qtSide.dataset.side;
      const market = (($('v3-qt-market') && $('v3-qt-market').value) || '').trim().toUpperCase();
      if (!market) { V3.toast('마켓 입력 (예: BTCUSDT)', 'warn'); return; }
      const mode = ($('v3-qt-mode') && $('v3-qt-mode').value) || 'quote';
      const val = parseFloat(($('v3-qt-amount') && $('v3-qt-amount').value) || '0') || 0;
      if (val <= 0) { V3.toast('값 > 0 필요', 'warn'); return; }
      const guard = ($('v3-qt-guard') && $('v3-qt-guard').value) || 'global';
      const sideLab = side === 'buy' ? '🟢 매수' : '🔴 매도';
      const amtLab = mode === 'percent' ? (val + '% (가용현금)') : (val + ' USDT');
      const ok = await V3.confirm('⚡ 퀵 트레이드 (실거래!)', '<div style="line-height:1.7"><b>' + market + '</b> ' + sideLab + ' ' + amtLab + '<br><small style="color:var(--v3-warn)">⚠️ 즉시 시장가 주문이 거래소에 실제로 들어갑니다.' + (guard === 'force' ? ' <b>강제(가드 무시)</b>' : guard === 'entry_limit_only' ? ' 진입제한만 적용' : ' 글로벌 가드 적용') + '</small></div>');
      if (!ok) return;
      V3.toast(market + ' ' + sideLab + '…', 'info');
      const body = { exchange: 'bybit', market_input: market, side: side, amount_mode: mode, amount_value: val, mode: 'immediate', guard_policy: guard };
      const r = await V3.getJSON('/api/trade/quick', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      V3.toast((r && r.ok) ? ('✓ ' + market + ' ' + sideLab + ' 주문 ' + (r.quick_id ? '#' + String(r.quick_id).slice(0, 8) : '제출됨')) : ('✗ 실패: ' + ((r && (r.message || r.detail || r.error)) || '알 수 없음')), (r && r.ok) ? 'ok' : 'err', 6000);
      if (r && r.ok) loadHome(true);
      return;
    }
    if (e.target.closest('#v3-gp-refresh')) { const inp = $('v3-gp-market'); if (inp && inp.value.trim()) V3.state.gp.market = inp.value.trim().toUpperCase(); loadGp(true); return; }
    // 🔌 플러그인 공통 설정 저장 (섹션별, /api/reserved/settings query POST) / 새로고침
    const rsv = e.target.closest('.rsave');
    if (rsv) {
      const sec = rsv.closest('.v3-cset'); if (!sec) return;
      const csave = sec.dataset.csave || 'reserved';
      const inputs = Array.from(sec.querySelectorAll('.v3-rin'));
      V3.toast('공통 설정 저장…', 'info');
      let okAll = true, err = '', cnt = 0;
      if (csave === 'triage') {                                   // PATCH /api/triage/settings (JSON body)
        const body = {};
        inputs.forEach((el) => {
          const k = el.dataset.rk, kind = el.dataset.kind;
          if (kind === 'chk') { body[k] = el.checked; cnt++; }
          else if (el.value !== '') { body[k] = (kind === 'text' ? el.value : parseFloat(el.value)); cnt++; }
        });
        const r = await V3.getJSON('/api/triage/settings', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        if (!r || r.ok === false || r.detail) { okAll = false; err = (r && (r.error || r.detail)) || '알 수 없음'; }
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
        if (!(r && r.ok !== false)) { okAll = false; err = (r && r.error) || '알 수 없음'; }
      } else {                                                    // reserved | guards → flat query POST (chunk 40, URL 길이 회피)
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
          if (!(r && r.ok !== false)) { okAll = false; err = (r && r.error) || '알 수 없음'; }
        }
      }
      V3.toast(okAll ? ('✓ 공통 설정 저장 (' + cnt + '개)') : ('✗ 저장 실패: ' + err), okAll ? 'ok' : 'err', okAll ? 3500 : 6000);
      loadPluginsCommon(true); return;
    }
    if (e.target.closest('#v3-pcommon-refresh')) { loadPluginsCommon(true); return; }
    // ⚙️ Settings — 연결상태 새로고침 / 텔레그램 테스트·저장
    if (e.target.closest('#v3-set-refresh')) { loadSettings(); return; }
    // 🛠️ 시스템 액션 (Reconcile/Dust/Retrain/E-STOP/Restart/Stop) — 세션 인증, 위험군 강한 확인
    const saBtn = e.target.closest('button[id^="v3-sa-"]');
    if (saBtn) {
      const cleanup = ($('v3-srv-cleanup') && $('v3-srv-cleanup').checked) ? 1 : 0;   // 서버 종료 전 정리 여부
      const delay = parseInt(($('v3-srv-delay') && $('v3-srv-delay').value) || '15', 10) || 15;   // 정리 대기 5~60초
      const cleanLab = cleanup ? (delay + '초 정리 후') : '정리 없이 즉시';
      const SA = {
        'v3-sa-reconcile': { url: '/api/system/reconcile?reason=manual_ui', t: '🔄 잔고 동기화', msg: '거래소 잔고 ↔ OMA 상태를 동기화할까?', danger: false },
        'v3-sa-dust': { url: '/api/engine/clear_dust?threshold=1000', t: '🧹 Dust 정리', msg: '소액 자투리(dust) 잔고를 정리할까?', danger: false },
        'v3-sa-retrain': { url: '/api/ai/train', t: '🧠 AI 재학습', msg: 'AI 모델을 재학습할까? (수 분 소요 · 백그라운드 진행)', danger: false },
        'v3-sa-dd-reset': { url: '/api/strategy/focus/drawdown/reset-cumulative', t: '🔧 드로다운 리셋', msg: '드로다운 워터마크(peak)를 현재 equity로 리셋할까?<br><small>자본 입출금·장기 정지 후 생긴 가짜 CRISIS(-30 conviction) 해소. 다음 tick에 페널티 풀림. 진입 차단 아님.</small>', danger: false },
        'v3-sa-resume': { url: '/api/system/emergency/resume?reason=manual_ui', t: '▶️ E-STOP 해제', msg: 'Emergency Stop 을 해제하고 거래를 재개할까?', danger: false },
        'v3-sa-estop': { url: '/api/system/emergency/stop?reason=manual_ui', t: '🛑 E-STOP 발동', msg: '<b style="color:var(--v3-warn)">전 거래를 즉시 중지(Emergency Stop)</b>할까?<br><small>새 진입·자동매매 모두 정지. (포지션은 유지)</small>', danger: true },
        'v3-sa-restart': { url: '/api/system/restart?delay_sec=' + delay + '&cleanup=' + cleanup, t: '🔁 서버 재시작', msg: '<b style="color:var(--v3-warn)">서버를 재시작</b>할까? (' + cleanLab + ')<br><small>run.ps1 필요 — 미설정 시 정지만 됨.</small>', danger: true },
        'v3-sa-stop': { url: '/api/system/stop?delay_sec=' + delay + '&cleanup=' + cleanup, t: '⏹️ 서버 정지', msg: '<b style="color:var(--v3-warn)">서버를 완전히 정지</b>할까? (' + cleanLab + ')<br><small>다시 켜려면 수동 시작 필요.</small>', danger: true },
      };
      const a = SA[saBtn.id]; if (!a) return;
      const ok = await V3.confirm(a.t + (a.danger ? ' (주의!)' : ''), a.msg);
      if (!ok) return;
      V3.toast(a.t + '…', 'info');
      const r = await V3.getJSON(a.url, { method: 'POST' });
      V3.toast((r && r.ok) ? ('✓ ' + a.t + ' 완료') : ('✗ 실패: ' + ((r && (r.message || r.detail || r.error)) || '알 수 없음')), (r && r.ok) ? 'ok' : 'err', 6000);
      return;
    }
    if (e.target.closest('#v3-alert-save')) {
      const cv = (k) => { const el = $('v3-alert-' + k); return el ? (el.checked ? 'true' : 'false') : ''; };
      const qs = ['longhold', 'drawdown', 'exit_profit_streak', 'daily', 'harpoon'].map((k) => k + '=' + cv(k)).join('&');
      V3.toast('알림 설정 저장…', 'info');
      const r = await V3.getJSON('/api/system/alerts?' + qs, { method: 'POST' });
      V3.toast((r && r.ok) ? '✓ 알림 종류 저장 (즉시 적용 + .env)' : ('✗ ' + ((r && r.error) || '실패')), (r && r.ok) ? 'ok' : 'err', 5000);
      if (r && r.ok) loadSettings();
      return;
    }
    if (e.target.closest('#v3-tg-test')) {
      const tk = (($('v3-tg-token') && $('v3-tg-token').value) || '').trim(), cid = (($('v3-tg-chat') && $('v3-tg-chat').value) || '').trim();
      if (!tk || !cid) { V3.toast('토큰 + chat id 입력 후 테스트', 'warn'); return; }
      V3.toast('✈️ 테스트 전송…', 'info');
      const r = await V3.getJSON('/api/system/telegram/test?token=' + encodeURIComponent(tk) + '&chat_id=' + encodeURIComponent(cid), { method: 'POST' });
      V3.toast((r && r.ok) ? '✈️ 테스트 성공 — 텔레그램 확인하세요' : ('✗ ' + ((r && r.error) || '실패')), (r && r.ok) ? 'ok' : 'err', 6000);
      return;
    }
    if (e.target.closest('#v3-peer-save')) {
      e.preventDefault();   // form submit default 차단 (안전망)
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
        V3.toast('👥 옆 서버 저장 → POST /peer/settings (urls=' + urls.length + ')', 'info', 4000);
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
          V3.toast('✓ 저장 OK · polling 재시작', 'ok', 5000);
          setTimeout(loadPeerSettings, 800);
        } else {
          V3.toast('✗ HTTP ' + raw.status + ' · ' + ((r && (r.error || r.detail)) || txt.slice(0, 80)), 'err', 8000);
        }
      } catch (ex) {
        console.error('[PEER] save exception:', ex);
        V3.toast('✗ 저장 예외: ' + String(ex), 'err', 8000);
      }
      return;
    }
    if (e.target.closest('#v3-tg-save')) {
      const tk = (($('v3-tg-token') && $('v3-tg-token').value) || '').trim(), cid = (($('v3-tg-chat') && $('v3-tg-chat').value) || '').trim(), pw = (($('v3-tg-admin') && $('v3-tg-admin').value) || '').trim();
      if (!tk || !cid) { V3.toast('토큰 + chat id 입력', 'warn'); return; }
      if (!pw) { V3.toast('admin 비밀번호 입력 (저장 인증)', 'warn'); return; }
      V3.toast('admin 인증…', 'info');
      const lg = await V3.getJSON('/api/system/admin/login?password=' + encodeURIComponent(pw), { method: 'POST' });
      if (!(lg && lg.ok && lg.token)) { V3.toast('✗ admin 인증 실패: ' + ((lg && lg.error) || ''), 'err', 6000); return; }
      const r = await V3.getJSON('/api/system/telegram/save?token=' + encodeURIComponent(tk) + '&chat_id=' + encodeURIComponent(cid) + '&admin_token=' + encodeURIComponent(lg.token), { method: 'POST' });
      V3.toast((r && r.ok) ? '✓ 텔레그램 저장됨 (.env)' : ('✗ ' + ((r && r.error) || '실패')), (r && r.ok) ? 'ok' : 'err', 6000);
      if (r && r.ok) { if ($('v3-tg-token')) $('v3-tg-token').value = ''; if ($('v3-tg-admin')) $('v3-tg-admin').value = ''; loadSettings(); }
      return;
    }
    // ⚡ LIGHTNING — 플러그인 새로고침 / 배포(setup) / 정지·청산·삭제(stop)
    const plr = e.target.closest('[data-plugin-refresh]');
    if (plr) { const pn = plr.dataset.pluginRefresh; if (pn === 'lightning') loadLightning(true); else if (pn === 'sniper') loadSniper(true); else if (pn === 'ladder') loadLadder(true); else if (PLUG[pn]) loadPlug(pn, true); return; }
    const ladStep = e.target.closest('.v3-ladder-steps-btn');   // 📐 단 보기 (읽기 전용)
    if (ladStep) { loadLadderSteps(ladStep.dataset.mkt); return; }
    // 🔧 LADDER 개별 단(실주문) — 정지/재개 (step/status)
    const lsPause = e.target.closest('.v3-lad-step-pause');
    if (lsPause) {
      const mkt = lsPause.dataset.mkt, uuid = lsPause.dataset.uuid, st = lsPause.dataset.st, verb = st === 'paused' ? '일시정지' : '재개';
      const ok = await V3.confirm('⏸ 단 ' + verb + ' (실주문!)', '<b>' + mkt + '</b> 이 단의 거래소 주문을 <b>' + verb + '</b>할까?<br><small style="color:var(--v3-warn)">' + (st === 'paused' ? '정지 시 거래소 지정가 주문이 취소됨 (재개하면 재주문)' : '재개 시 거래소에 지정가 주문 다시 깖') + '</small>');
      if (!ok) return;
      const r = await V3.getJSON('/api/ladder/step/status?market=' + encodeURIComponent(mkt) + '&step_uuid=' + encodeURIComponent(uuid) + '&status=' + st, { method: 'POST' });
      V3.toast((r && r.ok) ? ('✓ 단 ' + verb + ' 완료') : ('✗ 실패: ' + ((r && (r.detail || r.error)) || '')), (r && r.ok) ? 'ok' : 'err', 5000);
      if (r && r.ok) loadLadderSteps(mkt);
      return;
    }
    // 🔧 LADDER 개별 단(실주문) — 가격/수량 수정 (step/edit)
    const lsEdit = e.target.closest('.v3-lad-step-edit');
    if (lsEdit) {
      const mkt = lsEdit.dataset.mkt, uuid = lsEdit.dataset.uuid;
      const np = prompt(mkt + ' 단 — 새 가격(선) 입력 (빈칸 = 유지):', lsEdit.dataset.price);
      if (np === null) return;
      const na = prompt(mkt + ' 단 — 새 금액(USDT) 입력 (빈칸 = 유지):', lsEdit.dataset.amount);
      if (na === null) return;
      let qs = '/api/ladder/step/edit?market=' + encodeURIComponent(mkt) + '&step_uuid=' + encodeURIComponent(uuid);
      const npN = Number(np), naN = Number(na);
      if (np.trim() && npN > 0) qs += '&price=' + encodeURIComponent(npN);
      if (na.trim() && naN > 0) qs += '&amount=' + encodeURIComponent(naN);
      if (qs.indexOf('&price=') < 0 && qs.indexOf('&amount=') < 0) { V3.toast('변경 없음 (가격·금액 모두 빈칸)', 'warn'); return; }
      const ok = await V3.confirm('✏️ 단 수정 (실주문!)', '<b>' + mkt + '</b> 이 단을 수정할까?<br><small>가격: ' + (np.trim() && npN > 0 ? _fp(npN) : '유지') + ' · 금액: ' + (na.trim() && naN > 0 ? '$' + naN : '유지') + '</small><br><small style="color:var(--v3-warn)">거래소 주문을 취소 후 새 가격/수량으로 재주문합니다.</small>');
      if (!ok) return;
      const r = await V3.getJSON(qs, { method: 'POST' });
      V3.toast((r && r.ok) ? '✓ 단 수정 완료' : ('✗ 실패: ' + ((r && (r.detail || r.error)) || '')), (r && r.ok) ? 'ok' : 'err', 5000);
      if (r && r.ok) loadLadderSteps(mkt);
      return;
    }
    // 🔧 LADDER 개별 단(실주문) — 삭제 (step/delete)
    const lsDel = e.target.closest('.v3-lad-step-del');
    if (lsDel) {
      const mkt = lsDel.dataset.mkt, uuid = lsDel.dataset.uuid;
      const ok = await V3.confirm('🗑 단 삭제 (실주문!)', '<b>' + mkt + '</b> 이 단을 <b>삭제</b>할까?<br><small style="color:var(--v3-warn)">⚠️ 거래소의 해당 지정가 주문이 취소되고 그리드에서 제거됩니다. 되돌릴 수 없음.</small>');
      if (!ok) return;
      const r = await V3.getJSON('/api/ladder/step/delete?market=' + encodeURIComponent(mkt) + '&step_uuid=' + encodeURIComponent(uuid), { method: 'POST' });
      V3.toast((r && r.ok) ? '✓ 단 삭제 완료' : ('✗ 실패: ' + ((r && (r.detail || r.error)) || '')), (r && r.ok) ? 'ok' : 'err', 5000);
      if (r && r.ok) loadLadderSteps(mkt);
      return;
    }
    const ladSeed = e.target.closest('.v3-ladder-seed');   // 🌱 그리드 주문 깔기 (실주문)
    if (ladSeed) {
      const mkt = ladSeed.dataset.mkt;
      const ok = await V3.confirm('🌱 LADDER 주문 깔기 (실주문!)', '<div style="line-height:1.7"><b>' + mkt + '</b> 그리드 지정가 <b>매수 주문</b>을 거래소에 실제로 깝니다.<br><small style="color:var(--v3-warn)">⚠️ 실거래 — 저장된 구성(단수·간격·예산)대로 N개 지정가 주문. 슬롯 잠겨있어도 이 버튼은 즉시 깖.</small></div>');
      if (!ok) return;
      V3.toast(mkt + ' 주문 깔기…', 'info');
      const r = await V3.getJSON('/api/ladder/seed?market=' + encodeURIComponent(mkt), { method: 'POST' });
      const s = (r && r.summary) || {};
      V3.toast((r && r.ok) ? ('🌱 ' + mkt + ' 주문 ' + (s.created_buy != null ? s.created_buy + '개 깔림' : '완료') + (s.failed && s.failed.length ? ' (실패 ' + s.failed.length + ')' : '')) : ('✗ 실패: ' + ((r && (r.detail || r.error)) || '')), (r && r.ok) ? 'ok' : 'err', 6000);
      loadLadder(true); if (V3.state.ladder.stepsMarket === mkt) loadLadderSteps(mkt);
      return;
    }
    const ladCancel = e.target.closest('.v3-ladder-cancel');   // 🗑️ 주문 전체 취소
    if (ladCancel) {
      const mkt = ladCancel.dataset.mkt;
      const ok = await V3.confirm('🗑️ LADDER 주문 취소', '<b>' + mkt + '</b> — 이 마켓의 LADDER 지정가 <b>대기 주문을 전부 취소</b>할까? (보유 포지션은 유지, 미체결 주문만 취소)');
      if (!ok) return;
      V3.toast(mkt + ' 주문 취소…', 'info');
      const r = await V3.getJSON('/api/ladder/cancel?market=' + encodeURIComponent(mkt), { method: 'POST' });
      const s = (r && r.summary) || {};
      V3.toast((r && r.ok) ? ('🗑️ ' + mkt + ' 주문 ' + (s.canceled != null ? s.canceled + '개 취소' : '취소 완료')) : ('✗ 실패: ' + ((r && (r.detail || r.error)) || '')), (r && r.ok) ? 'ok' : 'err', 6000);
      loadLadder(true); if (V3.state.ladder.stepsMarket === mkt) loadLadderSteps(mkt);
      return;
    }
    const tsv = e.target.closest('.v3-tune-save');   // 🎛️ Tier-2 고유 튜닝 저장 (slot-fill 시 적용)
    if (tsv) {
      const key = tsv.dataset.plug, obj = {};
      (TIER2_TUNE[key] || []).forEach((f) => { const el = $('v3-tune-' + key + '-' + f[0]); if (el && el.value !== '') { const n = parseFloat(el.value); if (!isNaN(n)) obj[f[0]] = n; } });
      V3.toast((LABEL[key] || key) + ' 튜닝 저장…', 'info');
      const payload = {}; payload[key.toUpperCase()] = obj;
      const r = await V3.getJSON('/api/reserved/plugin-params?data=' + encodeURIComponent(JSON.stringify(payload)), { method: 'POST' });
      V3.toast((r && r.ok) ? ('✓ ' + (LABEL[key] || key) + ' 튜닝 저장 — 다음 슬롯 진입 시 적용 (재시작 후)') : ('✗ 실패: ' + ((r && (r.detail || r.error)) || '')), (r && r.ok) ? 'ok' : 'err', 5000);
      return;
    }
    // 🤖 Tier-2 추천 코인 → autopilot 우선 등록 (반자동 — autopilot 이 검토 후 그 전략 로직으로 진입)
    const t2e = e.target.closest('.v3-t2-enq');
    if (t2e) {
      const key = t2e.dataset.key, mkt = t2e.dataset.mkt;
      const items = (V3.state.tier2reco && V3.state.tier2reco[key] && V3.state.tier2reco[key].items) || [];
      const it = items.find((x) => x.market === mkt);
      if (!it) { V3.toast('추천 항목 없음 — 새로고침', 'warn'); return; }
      const ok = await V3.confirm('🤖 autopilot 우선 등록', '<div style="line-height:1.7"><b>' + mkt + '</b> 을 <b>' + (LABEL[key] || key) + '</b> autopilot 우선 후보로 등록할까?<br><small>autopilot 이 AI·conviction 게이트 검토 후 통과하면 ' + (LABEL[key] || key) + ' 로직(' + (key === 'autoloop' ? '분할매수·LongHold' : key === 'pingpong' ? '박스권 핑퐁' : '특성') + ')으로 진입. <b>무조건 진입 아님</b> — 줄 앞에 세우는 것.</small></div>');
      if (!ok) return;
      const body = Object.assign({}, it, { strategy: key.toUpperCase() });
      V3.toast(mkt + ' 등록…', 'info');
      const r = await V3.getJSON('/api/reserved/enqueue', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      V3.toast((r && r.ok) ? ('✓ ' + mkt + ' → ' + (LABEL[key] || key) + ' 우선 등록 (autopilot 검토 대기)') : ('✗ 실패: ' + ((r && (r.detail || r.error)) || '알 수 없음')), (r && r.ok) ? 'ok' : 'err', 6000);
      return;
    }
    // 📐 LADDER 구성 저장 — 주문 안 깖(buy_now·grid_auto_sync=false 강제). 부모님 "만들어놓고 슬롯 잠글꺼야"
    if (e.target.closest('#v3-ladder-deploy')) {
      const market = (($('v3-ladder-market') && $('v3-ladder-market').value) || '').trim().toUpperCase();
      if (!market) { V3.toast('마켓 입력 (예: BTCUSDT)', 'warn'); return; }
      const budget = parseFloat(($('v3-ladder-budget') && $('v3-ladder-budget').value) || '0') || 0;
      const maxSteps = parseInt(($('v3-ladder-maxsteps') && $('v3-ladder-maxsteps').value) || '10', 10) || 10;
      const stepPct = parseFloat(($('v3-ladder-steppct') && $('v3-ladder-steppct').value) || '1') || 1;
      const orderUsdt = parseFloat(($('v3-ladder-order') && $('v3-ladder-order').value) || '0') || 0;
      const mart = parseFloat(($('v3-ladder-mart') && $('v3-ladder-mart').value) || '1') || 1;
      const tp = parseFloat(($('v3-ladder-tp') && $('v3-ladder-tp').value) || '2') || 2;
      if (budget <= 0) { V3.toast('예산 > 0 필요', 'warn'); return; }
      if (maxSteps < 1) { V3.toast('단수 ≥ 1 (좀비 사다리 방지)', 'warn'); return; }
      const autoOn = !!($('v3-ladder-autosync') && $('v3-ladder-autosync').checked);   // 🟢 자동 ON = grid_auto_sync (슬롯 열고 자동거래)
      const ok = await V3.confirm('📐 LADDER ' + (autoOn ? '자동 ON (실거래 시작)' : '구성 저장 (주문 X)'), '<div style="line-height:1.7"><b>' + market + '</b> 그리드<br><small>예산 $' + budget + ' · ' + maxSteps + '단 · 간격 ' + stepPct + '% · 마틴 ' + mart + ' · TP ' + tp + '%</small><br>' + (autoOn ? '<b style="color:var(--v3-warn)">🟢 자동 ON — 슬롯 열고 그리드 지정가 주문 깔고 자동거래 시작 (실거래!)</b>' : '<b style="color:var(--v3-pos)">구성만 저장 (주문 X) — 슬롯 OFF. [📊 단 보기]로 확인 후 다시 🟢 ON 으로 저장하면 자동거래.</b>') + '</div>');
      if (!ok) return;
      V3.toast(market + (autoOn ? ' 자동 ON…' : ' 구성 저장…'), 'info');
      const lgb = {};   // 🪜 리본 그리드 고급(v3-ladder-g-*) 수집 → setup body merge (spacing/atr/emergency/auto_center 등)
      document.querySelectorAll('[id^="v3-ladder-g-"]').forEach((el) => { const p = el.id.slice('v3-ladder-g-'.length); if (el.type === 'checkbox') lgb[p] = el.checked; else if (el.value !== '') { const n = parseFloat(el.value); lgb[p] = (el.tagName === 'SELECT' || isNaN(n)) ? el.value : n; } });
      const body = Object.assign({ market: market, budget_usdt: budget, max_steps: maxSteps, step_pct: stepPct, martingale: mart, tp: tp, buy_now: false, grid_auto_sync: autoOn, tune_mode: 'MANUAL' }, lgb);
      if (orderUsdt > 0) body.order_usdt = orderUsdt;
      const r = await V3.getJSON('/api/strategy/ladder/setup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      V3.toast((r && r.ok) ? ('📐 ' + market + (autoOn ? ' 자동 ON 완료 — 그리드 자동거래 시작' : ' 구성 저장 완료 (주문 X) — [📊 단 보기]로 확인')) : ('✗ 실패: ' + ((r && (r.detail || r.error)) || '알 수 없음')), (r && r.ok) ? 'ok' : 'err', 6000);
      if (r && r.ok) { loadLadder(true); loadLadderSteps(market); }
      return;
    }
    const ltgDep = e.target.closest('#v3-ltg-deploy') || e.target.closest('#v3-ltg-update');
    if (ltgDep) {
      const isUpd = ltgDep.id === 'v3-ltg-update', lbl = isUpd ? '업데이트(재설정)' : '배포';
      const market = (($('v3-ltg-market') && $('v3-ltg-market').value) || '').trim().toUpperCase();
      if (!market) { V3.toast('마켓을 입력하세요 (예: BTCUSDT)', 'warn'); return; }
      const budget = parseFloat(($('v3-ltg-budget') && $('v3-ltg-budget').value) || '0') || 0;
      const tp = parseFloat(($('v3-ltg-tp') && $('v3-ltg-tp').value) || '5') || 5;
      const sl = parseFloat(($('v3-ltg-sl') && $('v3-ltg-sl').value) || '-3') || -3;
      const ok = await V3.confirm('LIGHTNING ' + lbl, '<div style="line-height:1.7"><b>' + market + '</b> ' + lbl + '<br><small>Budget $' + budget + ' · TP ' + tp + '% / SL ' + sl + '%</small><br><small style="color:var(--v3-warn)">⚠️ 실거래 — LIGHTNING 전략에 ' + (isUpd ? '재설정' : '배포') + '</small></div>');
      if (!ok) return;
      V3.toast(market + ' ' + lbl + '…', 'info');
      const r = await V3.getJSON('/api/strategy/lightning/setup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ market: market, budget_usdt: budget, tp_pct: tp, sl_pct: sl }) });
      V3.toast((r && r.ok) ? ('⚡ ' + market + ' ' + lbl + ' 완료') : ('✗ ' + lbl + ' 실패: ' + ((r && (r.detail || r.error)) || '알 수 없음')), (r && r.ok) ? 'ok' : 'err', 5000);
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
      V3.toast('LIGHTNING Guards 저장…', 'info');
      const r = await V3.getJSON('/api/strategy/lightning/guards', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const n = (r && r.applied_markets && r.applied_markets.length) || 0;
      V3.toast((r && r.ok) ? ('✓ Guards 저장 · ' + n + '개 마켓 적용') : ('✗ 실패: ' + ((r && r.error) || '')), (r && r.ok) ? 'ok' : 'err', 5000);
      return;
    }
    if (e.target.closest('#v3-ltg-recos-refresh')) { loadLightningRecos(true); return; }
    // 추천 행 클릭 → 배포 폼(리본)에 채움 (v2 fillCandidateToForm · 점유돼도 채움 가능 — 차단 X)
    const rpg = e.target.closest('#v3-ltg-recos .v3-recopage');   // 추천 페이지 넘김 (LIGHTNING 컨테이너 한정 — SNIPER 행과 분리)
    if (rpg) { V3.state.lightning.recoPage = parseInt(rpg.dataset.page, 10) || 1; const e0 = $('v3-ltg-recos'); if (e0) e0.innerHTML = renderRecos(V3.state.lightning.recos); return; }
    const rr = e.target.closest('#v3-ltg-recos .v3-reco-row');
    if (rr) {
      const m = $('v3-ltg-market'), b = $('v3-ltg-budget'), tpe = $('v3-ltg-tp'), sle = $('v3-ltg-sl');
      if (m) m.value = rr.dataset.mkt || '';
      if (b && rr.dataset.budget) b.value = rr.dataset.budget;   // 자본연동 추천 예산 (잔재 방어로 빈값이면 폼 값 유지)
      if (tpe && rr.dataset.tp !== '') tpe.value = rr.dataset.tp;
      if (sle && rr.dataset.sl !== '') sle.value = rr.dataset.sl;
      updateLtgEst();
      V3.toast((rr.dataset.mkt || '') + ' → 폼 채움 (예산/TP/SL 확인 후 ⚡배포)', 'info');
      return;
    }
    // 권장값 — 현재 입력 마켓을 추천 캐시에서 찾아 TP/SL(+'예산도' 체크 시 예산) 적용 (v2 applyRecommended)
    if (e.target.closest('#v3-ltg-recommend')) {
      const mkt = (($('v3-ltg-market') && $('v3-ltg-market').value) || '').trim().toUpperCase();
      if (!mkt) { V3.toast('Market 입력 후 권장값', 'warn'); return; }
      const recos = V3.state.lightning.recos;
      const it = recos && recos.items && recos.items.find((x) => String(x.market || '').toUpperCase() === mkt);
      if (!it) { V3.toast('추천 목록에 ' + mkt + ' 없음 — 🔍 추천 불러오기 또는 행 클릭', 'warn'); return; }
      const rp = it.recommended_params || {};
      if (rp.tp_pct != null && $('v3-ltg-tp')) $('v3-ltg-tp').value = rp.tp_pct;
      if (rp.sl_pct != null && $('v3-ltg-sl')) $('v3-ltg-sl').value = rp.sl_pct;
      const recBud = $('v3-ltg-recbudget') && $('v3-ltg-recbudget').checked;
      let budApplied = false;
      if (recBud) {
        const bud = Number(it.suggested_budget_usdt || it.budget || 0);
        if (bud > 10000) { V3.toast('추천 예산 $' + Math.round(bud) + ' = 비정상(옛 KRW 단위 잔재) — 예산은 직접 입력하세요. 서버 재시작 후 자본 기준으로 정상화됩니다', 'warn', 7000); }
        else if (bud > 0 && $('v3-ltg-budget')) { $('v3-ltg-budget').value = Math.round(bud); budApplied = true; }
      }
      updateLtgEst();
      V3.toast('권장값 적용: ' + mkt + ' (TP/SL' + (budApplied ? ' + 예산' : '') + ')', 'ok');
      return;
    }
    const lstop = e.target.closest('.v3-ltg-stop');
    if (lstop) {
      const mkt = lstop.dataset.mkt, act = lstop.dataset.act;
      const lab = act === 'liquidate' ? '청산 (포지션 매도)' : act === 'delete' ? '삭제 (DISABLED)' : '정지 (WATCH)';
      if (!(await V3.confirm('LIGHTNING ' + lab, '<b>' + mkt + '</b> — ' + lab + ' 할까?'))) return;
      const r = await V3.getJSON('/api/strategy/lightning/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ market: mkt, liquidate: act === 'liquidate', delete: act === 'delete' }) });
      V3.toast((r && r.ok) ? ('✓ ' + mkt + ' ' + lab) : ('✗ 실패: ' + ((r && r.error) || '')), (r && r.ok) ? 'ok' : 'err');
      loadLightning(true); return;
    }
    // 🎯 SNIPER — 배포(setup JSON body·side·source) / 추천 / 정지·삭제(stop = query string·sniper_id 우선)
    const snpDep = e.target.closest('#v3-snp-deploy') || e.target.closest('#v3-snp-update');
    if (snpDep) {
      const isUpd = snpDep.id === 'v3-snp-update', lbl = isUpd ? '업데이트(재설정)' : '배포';
      const market = (($('v3-snp-market') && $('v3-snp-market').value) || '').trim().toUpperCase();
      if (!market) { V3.toast('마켓을 입력하세요 (예: BTCUSDT)', 'warn'); return; }
      const side = ($('v3-snp-side') && $('v3-snp-side').value) || 'LONG';
      const budget = parseFloat(($('v3-snp-budget') && $('v3-snp-budget').value) || '0') || 0;
      const tp = parseFloat(($('v3-snp-tp') && $('v3-snp-tp').value) || '2') || 2;
      const sl = parseFloat(($('v3-snp-sl') && $('v3-snp-sl').value) || '-2.5') || -2.5;
      const ok = await V3.confirm('SNIPER ' + lbl, '<div style="line-height:1.7"><b>' + market + '</b> <span class="v3-badge ' + (side === 'SHORT' ? 'short' : 'long') + '">' + side + '</span> ' + lbl + '<br><small>Budget $' + budget + ' · TP ' + tp + '% / SL ' + sl + '%</small><br><small style="color:var(--v3-warn)">⚠️ 실거래 — SNIPER 진입 신호 대기(WATCH) 후 자동 진입</small></div>');
      if (!ok) return;
      V3.toast(market + ' ' + lbl + '…', 'info');
      // 🎯 리본 저격 조건(v3-snp-g-{param}) 수집 → setup body merge (select=cycle_mode 문자열)
      const sgb = {};
      document.querySelectorAll('[id^="v3-snp-g-"]').forEach((el) => { const p = el.id.slice('v3-snp-g-'.length); if (el.type === 'checkbox') sgb[p] = el.checked; else if (el.value !== '') { const n = parseFloat(el.value); sgb[p] = (el.tagName === 'SELECT' || el.type === 'time' || el.type === 'text' || isNaN(n)) ? el.value : n; } });   // time/text=문자열 유지(time_start "09:00"→parseFloat 9 방지)
      const r = await V3.getJSON('/api/strategy/sniper/setup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(Object.assign({ market: market, profile: 'SNIPER', side: side, budget_usdt: budget, tp_pct: tp, sl_pct: sl, source: 'manual' }, sgb)) });
      V3.toast((r && r.ok) ? ('🎯 ' + market + ' ' + lbl + ' 완료') : ('✗ ' + lbl + ' 실패: ' + ((r && (r.detail || r.error)) || '알 수 없음')), (r && r.ok) ? 'ok' : 'err', 5000);
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
      V3.toast((snpRr.dataset.mkt || '') + ' → 폼 채움 (예산/TP/SL 확인 후 🎯배포)', 'info');
      return;
    }
    if (e.target.closest('#v3-snp-recommend')) {
      const mkt = (($('v3-snp-market') && $('v3-snp-market').value) || '').trim().toUpperCase();
      if (!mkt) { V3.toast('Market 입력 후 권장값', 'warn'); return; }
      const recos = V3.state.sniper.recos;
      const it = recos && recos.items && recos.items.find((x) => String(x.market || '').toUpperCase() === mkt);
      if (!it) { V3.toast('추천 목록에 ' + mkt + ' 없음 — 🔍 추천 불러오기 또는 행 클릭', 'warn'); return; }
      const rp = it.recommended_params || {};
      if (rp.tp_pct != null && $('v3-snp-tp')) $('v3-snp-tp').value = rp.tp_pct;
      if (rp.sl_pct != null && $('v3-snp-sl')) $('v3-snp-sl').value = rp.sl_pct;
      const recBud = $('v3-snp-recbudget') && $('v3-snp-recbudget').checked;
      let budApplied = false;
      if (recBud) { const bud = Number(it.suggested_budget_usdt || it.budget || 0); if (bud > 10000) { V3.toast('추천 예산 $' + Math.round(bud) + ' = 비정상(옛 KRW 잔재) — 직접 입력하세요', 'warn', 6000); } else if (bud > 0 && $('v3-snp-budget')) { $('v3-snp-budget').value = Math.round(bud); budApplied = true; } }
      updateSnpEst();
      V3.toast('권장값 적용: ' + mkt + ' (TP/SL' + (budApplied ? ' + 예산' : '') + ')', 'ok');
      return;
    }
    const snpStop = e.target.closest('.v3-snp-stop');
    if (snpStop) {
      const sid = snpStop.dataset.sid, mkt = snpStop.dataset.mkt, del = snpStop.dataset.act === 'delete';
      const lab = del ? '삭제 (DISABLED)' : '정지 (WATCH)';
      if (!(await V3.confirm('SNIPER ' + lab, '<b>' + mkt + '</b> — ' + lab + ' 할까?'))) return;
      const qs = (sid ? ('sniper_id=' + encodeURIComponent(sid)) : ('market=' + encodeURIComponent(mkt))) + '&delete=' + (del ? 'true' : 'false');
      const r = await V3.getJSON('/api/strategy/sniper/stop?' + qs, { method: 'POST' });
      V3.toast((r && r.ok) ? ('✓ ' + mkt + ' ' + lab) : ('✗ 실패: ' + ((r && (r.detail || r.error)) || '')), (r && r.ok) ? 'ok' : 'err');
      loadSniper(true); return;
    }
    // 🔌 Generic plugin 패널 (GAZUA 등) — data-plug 기반 (LIGHTNING/SNIPER 이후라 그들 행은 이미 처리됨)
    const pgDep = e.target.closest('.v3-plug-deploy');
    if (pgDep) {
      const key = pgDep.dataset.plug, c = PLUG[key]; if (!c) return;
      const isUpd = pgDep.dataset.upd === '1', lbl = isUpd ? '업데이트(재설정)' : '배포';
      const market = (($('v3-' + key + '-market') && $('v3-' + key + '-market').value) || '').trim().toUpperCase();
      if (!market) { V3.toast('마켓을 입력하세요 (예: BTCUSDT)', 'warn'); return; }
      const f = { market: market, budget: parseFloat(($('v3-' + key + '-budget') && $('v3-' + key + '-budget').value) || '0') || 0, tp: parseFloat(($('v3-' + key + '-tp') && $('v3-' + key + '-tp').value) || c.defTp) || parseFloat(c.defTp), sl: parseFloat(($('v3-' + key + '-sl') && $('v3-' + key + '-sl').value) || c.defSl) || parseFloat(c.defSl) };
      if (c.hasSide) f.side = ($('v3-' + key + '-side') && $('v3-' + key + '-side').value) || 'LONG';
      const ok = await V3.confirm(c.label + ' ' + lbl, '<div style="line-height:1.7"><b>' + market + '</b>' + (c.hasSide ? ' <span class="v3-badge ' + (f.side === 'SHORT' ? 'short' : 'long') + '">' + f.side + '</span>' : '') + ' ' + lbl + '<br><small>Budget $' + f.budget + ' · TP ' + f.tp + '% / SL ' + f.sl + '%</small><br><small style="color:var(--v3-warn)">⚠️ 실거래 — ' + c.label + ' 전략에 ' + (isUpd ? '재설정' : '배포') + '</small></div>');
      if (!ok) return;
      V3.toast(market + ' ' + lbl + '…', 'info');
      // 🛡️ 리본 Entry/Exit 가드(v3-{key}-g-{param}) 수집 → setup body 에 합침 (부모님 "배포 시 적용")
      const gbody = {};
      document.querySelectorAll('[id^="v3-' + key + '-g-"]').forEach((el) => { const p = el.id.slice(('v3-' + key + '-g-').length); if (el.type === 'checkbox') gbody[p] = el.checked; else if (el.value !== '') { const n = parseFloat(el.value); gbody[p] = (el.tagName === 'SELECT' || isNaN(n)) ? el.value : n; } });
      const r = await V3.getJSON('/api/strategy/' + c.api + '/setup', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(Object.assign({}, c.setupBody(f), gbody)) });
      V3.toast((r && r.ok) ? ('✓ ' + market + ' ' + lbl + ' 완료') : ('✗ ' + lbl + ' 실패: ' + ((r && (r.detail || r.error)) || '알 수 없음')), (r && r.ok) ? 'ok' : 'err', 5000);
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
      V3.toast((pgRow.dataset.mkt || '') + ' → 폼 채움 (예산/TP/SL 확인 후 배포)', 'info'); return;
    }
    const pgRec = e.target.closest('.v3-plug-recommend');
    if (pgRec) {
      const key = pgRec.dataset.plug, st = plugState(key);
      const mkt = (($('v3-' + key + '-market') && $('v3-' + key + '-market').value) || '').trim().toUpperCase();
      if (!mkt) { V3.toast('Market 입력 후 권장값', 'warn'); return; }
      const it = st.recos && st.recos.items && st.recos.items.find((x) => String(x.market || '').toUpperCase() === mkt);
      if (!it) { V3.toast('추천 목록에 ' + mkt + ' 없음 — 🔍 추천 불러오기 또는 행 클릭', 'warn'); return; }
      const rp = it.recommended_params || {};
      if (rp.tp_pct != null && $('v3-' + key + '-tp')) $('v3-' + key + '-tp').value = rp.tp_pct;
      if (rp.sl_pct != null && $('v3-' + key + '-sl')) $('v3-' + key + '-sl').value = rp.sl_pct;
      const recBud = $('v3-' + key + '-recbudget') && $('v3-' + key + '-recbudget').checked;
      let budApplied = false;
      if (recBud) { const bud = Number(it.suggested_budget_usdt || it.budget || 0); if (bud > 10000) { V3.toast('추천 예산 비정상(옛 KRW 잔재) — 직접 입력하세요', 'warn', 6000); } else if (bud > 0 && $('v3-' + key + '-budget')) { $('v3-' + key + '-budget').value = Math.round(bud); budApplied = true; } }
      updatePlugEst(key);
      V3.toast('권장값 적용: ' + mkt + ' (TP/SL' + (budApplied ? ' + 예산' : '') + ')', 'ok'); return;
    }
    const pgStop = e.target.closest('.v3-plug-stop');
    if (pgStop) {
      const key = pgStop.dataset.plug, c = PLUG[key], mkt = pgStop.dataset.mkt, act = pgStop.dataset.act;
      const lab = act === 'liquidate' ? '청산 (포지션 매도)' : act === 'delete' ? '삭제 (DISABLED)' : '정지 (WATCH)';
      if (!(await V3.confirm(c.label + ' ' + lab, '<b>' + mkt + '</b> — ' + lab + ' 할까?'))) return;
      let r;
      if (c.stopMode === 'query') { r = await V3.getJSON('/api/strategy/' + c.api + '/stop?market=' + encodeURIComponent(mkt) + '&delete=' + (act === 'delete' ? 'true' : 'false'), { method: 'POST' }); }
      else { r = await V3.getJSON('/api/strategy/' + c.api + '/stop', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ market: mkt, liquidate: act === 'liquidate', delete: act === 'delete' }) }); }
      V3.toast((r && r.ok) ? ('✓ ' + mkt + ' ' + lab) : ('✗ 실패: ' + ((r && (r.detail || r.error)) || '')), (r && r.ok) ? 'ok' : 'err');
      loadPlug(key, true); return;
    }
    // 🖐 Manual Control — Force Select / Disable+Close (focusApi 동일: query param POST)
    if (e.target.closest('#v3-manual-force')) {
      const mkt = ($('v3-manual-market') && $('v3-manual-market').value.trim().toUpperCase()) || 'XAUTUSDT';
      const dir = ($('v3-manual-dir') && $('v3-manual-dir').value) || 'LONG';
      const ok = await V3.confirm('Force Select', '<b>' + mkt + '</b> <span class="v3-badge ' + (dir === 'LONG' ? 'long' : 'short') + '">' + dir + '</span> 강제 선택(고정)할까?');
      if (!ok) return;
      const r = await V3.getJSON('/api/strategy/focus/force-select?market=' + encodeURIComponent(mkt) + '&direction=' + dir, { method: 'POST' });
      V3.toast((r && r.ok !== false) ? ('🎯 ' + mkt + ' ' + dir + ' 고정') : ('Force Select 실패: ' + ((r && r.error) || '')), (r && r.ok !== false) ? 'ok' : 'err');
      pollStatus(); return;
    }
    if (e.target.closest('#v3-manual-disable')) {
      if (!(await V3.confirm('Disable + Close', 'FOCUS 정지 + 포지션 청산할까?'))) return;
      const r = await V3.getJSON('/api/strategy/focus/disable?close_position=1', { method: 'POST' });
      V3.toast((r && r.ok !== false) ? '■ FOCUS 정지 + 청산' : '실패', (r && r.ok !== false) ? 'ok' : 'err');
      pollStatus(); return;
    }
    // 📊 Scanner 행 📊 → TF Progress 중앙 모달 (v3-scan-me 분기보다 먼저: 같은 버튼에 두 클래스)
    const tfmBtn = e.target.closest('.v3-scan-tfm');
    if (tfmBtn) { showTfModal(tfmBtn.dataset.mkt, tfmBtn.dataset.sig || ''); return; }
    // 📊 TF 모달 닫기 (백드롭 클릭 / 닫기 버튼)
    if (e.target.id === 'v3-tfm-back' || e.target.closest('#v3-tfm-close')) { const b = $('v3-tfm-back'); if (b) b.classList.remove('show'); return; }
    // 📊 TF 모달 L/S 진입 → 모달 닫고 동일 confirm 흐름 (z-index 같음 → 먼저 닫음)
    const tfmGo = e.target.closest('#v3-tfm-long, #v3-tfm-short');
    if (tfmGo) { const b = $('v3-tfm-back'); if (b) b.classList.remove('show'); await scanManualEntry(tfmGo.dataset.mkt, tfmGo.dataset.dir, false); return; }
    // Scanner Manual 버튼 (L / L⏳ / S / S⏳) → 수동 진입 (게이트 우회, 방향 그대로)
    const me = e.target.closest('.v3-scan-me');
    if (me) { await scanManualEntry(me.dataset.mkt, me.dataset.dir, me.dataset.smart === '1'); return; }
    // 🖐 수동 진입 위젯 (가로 100% 표) — 입력 폼 버전. 마켓=입력란 / timeout=분
    const meGo = e.target.closest('.v3-me-go');
    if (meGo) {
      if (V3.state.active !== 'focus') { V3.toast('현재 FOCUS만 연결됨 (다른 전략은 Phase 6)', 'warn'); return; }
      const mkt = (($('v3-me-market') && $('v3-me-market').value) || '').trim().toUpperCase();
      if (!mkt) { V3.toast('코인을 입력하세요 (예: BTCUSDT)', 'warn'); return; }
      const dir = meGo.dataset.dir, smart = meGo.dataset.smart === '1';
      const tmin = parseInt(($('v3-me-timeout') && $('v3-me-timeout').value) || '60', 10) || 60;
      const badge = '<span class="v3-badge ' + (dir === 'LONG' ? 'long' : 'short') + '">' + dir + '</span>';
      const ok = await V3.confirm('수동 진입 확인', '<div style="line-height:1.8"><b>' + mkt + '</b> ' + badge +
        '<br><small>모드: ' + (smart ? ('신호 ' + tmin + '분 대기') : '즉시 진입') + '</small>' +
        '<br><small style="color:var(--v3-warn)">⚠️ 실거래 — 게이트 우회(안전가드 유지)·방향 그대로(자동 FLIP 없음).</small></div>');
      if (!ok) return;
      let url = '/api/strategy/focus/manual-entry?market=' + encodeURIComponent(mkt) + '&direction=' + dir;
      if (smart) url += '&wait_for_signal=true&timeout_sec=' + (tmin * 60);
      V3.toast(mkt + ' ' + dir + ' 요청…', 'info');
      const r = await V3.getJSON(url, { method: 'POST' });
      V3.toast((r && r.ok) ? ('✓ ' + mkt + ' ' + dir + (smart ? ' 신호대기 등록' : ' 진입') + ' 완료') : ('✗ 진입 실패: ' + ((r && r.error) || '알 수 없음')), (r && r.ok) ? 'ok' : 'err', 6000);
      pollStatus(); return;
    }
    // 🧊 FOCUS COOLDOWN 수동 해제 (v2 Skip 버튼 포팅) → /skip-cooldown POST → 즉시 재스캔
    if (e.target.closest('.v3-skip-cd')) {
      const r = await V3.getJSON('/api/strategy/focus/skip-cooldown', { method: 'POST' });
      V3.toast((r && r.ok) ? '⏭ FOCUS 쿨다운 해제됨' : ('실패: ' + ((r && r.error) || '알 수 없음')), (r && r.ok) ? 'ok' : 'err');
      pollStatus(); return;
    }
    const seg = e.target.closest('.es-seg');
    if (seg) {
      const sw = seg.closest('.v3-eng-sw'); const name = sw && sw.dataset.engineSw;
      if (name !== 'focus' && name !== 'harpoon') { V3.toast('이 엔진은 다음 Phase 연결', 'warn'); return; }
      const act = seg.dataset.act, running = sw.classList.contains('on');
      if ((act === 'start' && running) || (act === 'stop' && !running)) return;  // 이미 그 상태
      const lbl = LABEL[name] || name;
      if (act === 'stop' && !(await V3.confirm('엔진 정지', lbl + ' 엔진을 정지할까요? (열린 포지션 유지)'))) return;
      const r = await V3.getJSON('/api/strategy/' + name + '/' + (act === 'start' ? 'enable' : 'disable'), { method: 'POST' });
      V3.toast((r && r.ok !== false) ? (lbl + ' ' + (act === 'start' ? 'ON' : 'OFF')) : '엔진 전환 실패', (r && r.ok !== false) ? 'ok' : 'err');
      pollStatus(); return;
    }
    const one = e.target.closest('.focus-btn-close-one');
    if (one) {
      const mkt = one.dataset.market;
      if (!(await V3.confirm('포지션 청산', 'Close <b>' + mkt + '</b>?'))) return;
      const r = await V3.getJSON('/api/strategy/focus/close-one?market=' + encodeURIComponent(mkt), { method: 'POST' });
      V3.toast((r && r.ok) ? ('✅ ' + mkt + ' 청산') : ('❌ 청산 실패: ' + ((r && r.error) || '')), (r && r.ok) ? 'ok' : 'err');
      pollStatus(); return;
    }
    const mk = e.target.closest('.v3-mkt');
    if (mk && mk.dataset.bybit) { V3.openBybitTrade(mk.dataset.bybit); return; }
    if (e.target.closest('#focus-btn-close-all')) {
      if (!(await V3.confirm('전체 청산', '모든 FOCUS 포지션을 청산할까요?'))) return;
      await V3.getJSON('/api/strategy/focus/close-all', { method: 'POST' });
      V3.toast('🛑 전체 청산 요청', 'ok'); pollStatus(); return;
    }
    if (e.target.closest('#focus-btn-exit-selected')) {
      const chk = Array.from(document.querySelectorAll('.focus-pos-chk:checked')).map((c) => c.dataset.market);
      if (!chk.length) return;
      if (!(await V3.confirm('선택 청산', 'Close ' + chk.join(', ') + '?'))) return;
      await V3.getJSON('/api/strategy/focus/close-selected?markets=' + encodeURIComponent(chk.join(',')), { method: 'POST' });
      V3.toast('선택 청산 요청', 'ok'); pollStatus(); return;
    }
  });
  document.addEventListener('change', (e) => {
    // 📓 Journal 필터 (전략 All/FOCUS/HARPOON · 코인 All Coins · Rows) → 페이지 리셋 + 재조회
    if (e.target.id === 'v3-journal-filter') { V3.state.journal.strategy = e.target.value; V3.state.journal.page = 1; loadJournal(true); return; }
    if (e.target.id === 'v3-journal-market') { V3.state.journal.market = e.target.value; V3.state.journal.page = 1; loadJournal(true); return; }
    if (e.target.id === 'v3-journal-limit') { V3.state.journal.limit = Math.max(5, Math.min(500, parseInt(e.target.value, 10) || 20)); V3.state.journal.page = 1; loadJournal(true); return; }
    // ⚡ LIGHTNING 추천 Rows 변경(페이지 리셋) / Budget·TP 변경 → Est.이익 재계산
    if (e.target.id === 'v3-ltg-recorows') { V3.state.lightning.recoRows = parseInt(e.target.value, 10) || 10; V3.state.lightning.recoPage = 1; const e0 = $('v3-ltg-recos'); if (e0) e0.innerHTML = renderRecos(V3.state.lightning.recos); return; }
    if (e.target.id === 'v3-ltg-budget' || e.target.id === 'v3-ltg-tp') { if (V3.updateLtgEst) V3.updateLtgEst(); return; }
    // 🎯 SNIPER 추천 Rows / Budget·TP → Est 재계산
    if (e.target.id === 'v3-snp-recorows') { V3.state.sniper.recoRows = parseInt(e.target.value, 10) || 5; V3.state.sniper.recoPage = 1; const e0 = $('v3-snp-recos'); if (e0) e0.innerHTML = renderSnpRecos(V3.state.sniper.recos); return; }
    if (e.target.id === 'v3-snp-budget' || e.target.id === 'v3-snp-tp') { if (V3.updateSnpEst) V3.updateSnpEst(); return; }
    if (e.target.id === 'v3-krw-rate') { localStorage.setItem('v3_krw_rate', e.target.value); localStorage.setItem('v3_krw_auto', '0'); const _ac = $('v3-krw-auto'); if (_ac) _ac.checked = false; if (V3.loadSettings) V3.loadSettings(); return; }   // 💱 수동 입력 = 자동 OFF + 기억 + 재계산
    if (e.target.id === 'v3-krw-auto') { localStorage.setItem('v3_krw_auto', e.target.checked ? '1' : '0'); if (e.target.checked && V3.fetchKrwRate) V3.fetchKrwRate(true); else if (V3.loadSettings) V3.loadSettings(); return; }   // 🔄 자동 ON=즉시 fetch / OFF=수동값 유지
    // 🔌 Generic plugin (GAZUA 등) Rows / Budget·TP / 추천토글 — PLUG 키만 매칭
    { const m = e.target.id && e.target.id.match(/^v3-([a-z]+)-recorows$/); if (m && PLUG[m[1]]) { const st = plugState(m[1]); st.recoRows = parseInt(e.target.value, 10) || 5; st.recoPage = 1; const e0 = $('v3-' + m[1] + '-recos'); if (e0) e0.innerHTML = renderPlugRecos(m[1], st.recos); return; } }
    { const m = e.target.id && e.target.id.match(/^v3-([a-z]+)-(budget|tp)$/); if (m && PLUG[m[1]]) { updatePlugEst(m[1]); return; } }
    { const m = e.target.id && e.target.id.match(/^v3-([a-z]+)-w-recos$/); if (m && PLUG[m[1]]) { plugState(m[1]).showRecos = e.target.checked; renderMain(); return; } }
    { const m = e.target.id && e.target.id.match(/^v3-([a-z]+)-benchmark$/); if (m && PLUG[m[1]]) { plugState(m[1]).benchmark = e.target.value; loadPlugRecos(m[1], true); return; } }   // 🔄 역행 기준점 변경 → 재스캔
    // 🖐 Manual Control — 마켓 변경 시 TF Progress 즉시 갱신 / 방향 기억
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

  // ── 우측 위젯 토글 ──
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
  $('v3-w-mentry')?.addEventListener('change', (e) => { V3.state.widgets.mentry = e.target.checked; renderMain(); });   // 🖐 수동 진입 표
  $('v3-w-phasek')?.addEventListener('change', (e) => { V3.state.widgets.phasek = e.target.checked; renderMain(); });
  // 🏠 홈 우측 위젯 토글 (종합현황·퀵트레이드·포지션·추천후보 — 표시할 표 선택)
  ['status', 'quick', 'positions', 'reco'].forEach((k) => $('v3-hm-' + k)?.addEventListener('change', (e) => { V3.state.home.show[k] = e.target.checked; renderMain(); }));
  // 🐟 HARPOON 우측 위젯 토글 (Stats / Current Scalp / FOCUS Link / Recent Scalps)
  $('v3-hw-stats')?.addEventListener('change', (e) => { V3.state.hpwidgets.stats = e.target.checked; renderMain(); });
  $('v3-hw-scalp')?.addEventListener('change', (e) => { V3.state.hpwidgets.scalp = e.target.checked; renderMain(); });
  $('v3-hw-link')?.addEventListener('change', (e) => { V3.state.hpwidgets.link = e.target.checked; renderMain(); });
  $('v3-hw-history')?.addEventListener('change', (e) => { V3.state.hpwidgets.history = e.target.checked; renderMain(); });
  // 🗂️ 레이어 영속 [2026-06-19 부모 "이전 설정으로 뜨면 좋은데 매번 리셋?"] — 토글 변경 시 저장 +
  //   로드 시 정적 HTML 체크박스 .checked 를 복원된 state 에 동기화. v3-w-*·v3-hm-*·v3-hw-* 공통.
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
  syncLayerChecks();   // 로드 시 1회 — 복원된 state(or 기본값)를 정적 HTML 체크박스에 반영
  document.addEventListener('change', function (e) {
    var id = e.target && e.target.id;
    if (typeof id === 'string' && (id.indexOf('v3-w-') === 0 || id.indexOf('v3-hw-') === 0 || id.indexOf('v3-hm-') === 0)) _saveLayers();
  });
  // ⚡ LIGHTNING 우측 위젯 토글 (추천 코인 show/hide — 부모님 "놀고있는 오른쪽 활용·추천 보일지 말지")
  $('v3-lw-recos')?.addEventListener('change', (e) => { V3.state.lightning.showRecos = e.target.checked; renderMain(); });
  // 🎯 SNIPER 우측 위젯 토글 (추천 코인 show/hide)
  $('v3-sw-recos')?.addEventListener('change', (e) => { V3.state.sniper.showRecos = e.target.checked; renderMain(); });

  // ── E-STOP ──
  $('v3-estop')?.addEventListener('click', async () => {
    const ok = await V3.confirm('🛑 E-STOP',
      '<div style="line-height:1.7"><b style="color:var(--v3-warn)">전 거래를 즉시 중지(Emergency Stop)</b>할까?<br>' +
      '<small>새 진입·자동매매 모두 정지. 포지션은 유지됨.</small></div>');
    if (!ok) return;
    const r = await V3.getJSON('/api/system/emergency/stop?reason=v3_top_estop', { method: 'POST' });
    V3.toast((r && r.ok) ? '🛑 E-STOP 발동됨' : ('E-STOP 실패: ' + ((r && (r.message || r.detail || r.error)) || '')), (r && r.ok) ? 'ok' : 'err', 6000);
    pollStatus();
  });

  // ── 위젯 순서 drag reorder (2026-06-07 부모) — 우측 분석위젯 행 끌어서 순서 변경, localStorage 영속 ──
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
      if (!k || !_wreg[k]) return;   // _wreg 분석위젯만 reorder (summary/positions/mentry 제외)
      row.setAttribute('draggable', 'true');
      row.style.cursor = 'grab';
      row.addEventListener('dragstart', (e) => { dragged = row; row.style.opacity = '0.4'; try { e.dataTransfer.effectAllowed = 'move'; } catch (x) { /* noop */ } });
      row.addEventListener('dragend', () => { row.style.opacity = ''; dragged = null; });
      row.addEventListener('dragover', (e) => { e.preventDefault(); });
      row.addEventListener('drop', (e) => {
        e.preventDefault();
        if (!dragged || dragged === row || dragged.parentNode !== row.parentNode) return;   // 같은 그룹 내에서만
        row.parentNode.insertBefore(dragged, row);
        rebuild();
      });
    });
  }

  // ── 패널 폭 리사이즈 (거터 드래그 + localStorage) ──
  function setupResize() {
    const body = document.querySelector('.v3-body'); if (!body) return;
    try {
      const s = JSON.parse(localStorage.getItem('v3-panel-w') || '{}');
      if (s.tree) body.style.setProperty('--v3-tree', s.tree + 'px');
      if (s.wpanel) body.style.setProperty('--v3-wpanel', s.wpanel + 'px');
      const wo = JSON.parse(localStorage.getItem('v3-widget-order') || 'null'); if (Array.isArray(wo)) V3.state.widgetOrder = wo;
    } catch (e) { /* noop */ }
    setupWidgetReorder();   // 위젯 순서 drag
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

  // ── WS 실시간: /ws/state (POSITION_OPEN/CLOSE/CONFIG → 즉시 갱신) ──
  function connectStateWS() {
    try {
      const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws = new WebSocket(proto + '//' + location.host + '/ws/state');
      ws.onmessage = () => { pollStatus(); };           // 모든 state 이벤트 = 즉시 새로고침
      ws.onclose = () => { setTimeout(connectStateWS, 3000); };
      ws.onerror = () => { try { ws.close(); } catch (e) { /* noop */ } };
      V3._stateWS = ws;
    } catch (e) { setTimeout(connectStateWS, 5000); }
  }

  // ── 시작 ──
  setupResize();
  buildStratRail();      // 상단 전략 레일 (흔적 + 전환)
  updateTreeUI();
  applyActivePanels();   // active 전략(=focus) 리본/우측 초기 표시
  renderMain();
  pollStatus();
  connectStateWS();
  setInterval(pollStatus, 8000);   // fallback (WS 가 이벤트 즉시 push, 중복 폴링 방지)
  updateSpotLights();              // ★ GAZUA·CONTRARIAN 현물 거래소 점등 (토글 대체)
  setInterval(updateSpotLights, 15000);   // cross 엔드포인트 15s 캐시와 정합
  // 🟢 GreenPen Scanner 자동 갱신 — 화면에 떠 있을 때만 (탭 백그라운드·다른 뷰·위젯 OFF 면 스킵 → 서버 부담 0)
  async function refreshScanIfVisible() {
    if (document.visibilityState !== 'visible') return;             // 탭 가려져 있으면 스킵 (배경 스캔 안 함)
    const w = V3.state.widgets || {}; if (!w.scan) return;           // GreenPen 위젯 OFF → 스킵
    const cfg = _wreg.scan; if (!cfg || !$(cfg.el)) return;          // 현재 뷰에 위젯 DOM 없으면 스킵
    const c = _wcache.scan, now = Date.now();
    if (c && now - c.t < (cfg.ttl || 120000)) return;               // TTL(120s) 내면 재스캔 안 함 (무거운 scan-list 절약)
    const d = await V3.getJSON(cfg.url, { timeoutMs: cfg.timeoutMs || 8000 });
    if (d && d.ok !== false) _wcache.scan = { data: d, t: now };
    const e1 = $(cfg.el); if (e1) e1.innerHTML = cfg.render(d);
  }
  setInterval(refreshScanIfVisible, 30000);   // 30s 체크 → TTL 덕에 실제 스캔은 ~2분마다, 화면 보일 때만
})();
