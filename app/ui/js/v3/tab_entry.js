/* ============================================================
   tab_entry.js — ribbon condition panel logic (entry / mode / size / market lock)
   entry = manual-entry (after confirm) / mode, size, lock = /config save. Currently targets the FOCUS strategy.
   ============================================================ */
(function () {
  'use strict';
  const V3 = window.V3 = window.V3 || {};
  const $ = V3.$ || ((id) => document.getElementById(id));

  async function loadMarkets() {
    const d = await V3.getJSON('/api/system/markets?quote=USDT');
    const list = $('v3-market-list'), cnt = $('v3-market-count');
    const m = (d && (d.markets || (Array.isArray(d) ? d : null))) || [];
    if (Array.isArray(m) && m.length) {
      if (list) list.innerHTML = m.map((x) => '<option value="' + x + '"></option>').join('');
      if (cnt) cnt.textContent = m.length + ' USDT markets';
      V3.state.markets = m;
    } else if (cnt) { cnt.textContent = 'Failed to load markets'; }
  }

  // Sync config inputs during polling — ★ isolated per strategy container.
  // (FOCUS and HARPOON share the same data-cfg keys such as leverage/risk_pct/cooldown_sec/min_adx,
  //  so using a global selector outside the container would overwrite each other's values → always scope within .rib-strat)
  function syncCfgInto(rootSel, cfg) {
    const root = document.querySelector(rootSel);
    if (!root || !cfg) return;
    const expand = root.querySelector('.ribbon-expand');
    if (expand && !expand.hidden) return;   // freeze while that strategy's expanded panel is being edited
    root.querySelectorAll('[data-cfg]').forEach((el) => {
      if (document.activeElement === el) return;
      const k = el.dataset.cfg; if (cfg[k] == null) return;
      if (el.type === 'checkbox') el.checked = !!cfg[k];
      else el.value = Array.isArray(cfg[k]) ? cfg[k].join(',') : cfg[k];   // whitelist/blacklist = array
    });
  }
  V3.syncCfgInto = syncCfgInto;

  // FOCUS config → FOCUS ribbon only (data-cfg + id-specific fields mode/lock)
  V3.syncEntryConfig = (cfg) => {
    const root = document.querySelector('.rib-strat[data-rib-strat="focus"]');
    if (!root || !cfg) return;
    const expand = root.querySelector('.ribbon-expand');
    if (expand && !expand.hidden) return;
    const md = $('v3-entry-mode'); if (md && document.activeElement !== md && cfg.entry_mode) md.value = cfg.entry_mode;
    const lk = $('v3-cfg-lock'); if (lk && document.activeElement !== lk && !lk._touched) lk.value = cfg.lock_market || '';
    syncCfgInto('.rib-strat[data-rib-strat="focus"]', cfg);
  };
  // HARPOON config → HARPOON ribbon only (loadHarpoon calls this with status.config)
  V3.syncHarpoonConfig = (cfg) => syncCfgInto('.rib-strat[data-rib-strat="harpoon"]', cfg);

  function focusOnly() {
    if (V3.state.active !== 'focus') { V3.toast('Only FOCUS is connected (other strategies in Phase 6)', 'warn'); return false; }
    return true;
  }

  async function saveConfig(params, label) {
    if (!focusOnly()) return;
    const qs = Object.entries(params).map(([k, v]) => k + '=' + encodeURIComponent(v)).join('&');
    const d = await V3.getJSON('/api/strategy/focus/config?' + qs, { method: 'POST', timeoutMs: 40000 });
    if (d && d.ok !== false) V3.toast('✓ ' + label + ' saved', 'ok');
    else V3.toast('✗ ' + label + ' save failed: ' + ((d && d.error) || ''), 'err', 6000);
    if (V3.pollStatus) V3.pollStatus();
  }

  // 🖐 Manual entry (LONG/SHORT) moved from the right-side widget → full-width table in the main body (dashboard_v3.js .v3-me-go). Ribbon = settings only.
  $('v3-mode-apply')?.addEventListener('click', () => { const m = $('v3-entry-mode').value; if (m) saveConfig({ entry_mode: m }, 'Mode'); });
  $('v3-size-apply')?.addEventListener('click', () => {
    const p = {}; const b = $('v3-cfg-budget').value, l = $('v3-cfg-leverage').value, r = $('v3-cfg-risk').value;
    if (b !== '') p.budget_usdt = b; if (l !== '') p.leverage = l; if (r !== '') p.risk_pct = r;
    if (!Object.keys(p).length) { V3.toast('No values to save', 'warn'); return; }
    saveConfig(p, 'Size');
  });
  $('v3-cfg-lock')?.addEventListener('input', (e) => { e.target._touched = true; });
  $('v3-lock-apply')?.addEventListener('click', () => {
    const v = ($('v3-cfg-lock').value || '').trim().toUpperCase();
    saveConfig({ lock_market: v }, v ? ('Lock ' + v) : 'Unlock');
    const lk = $('v3-cfg-lock'); if (lk) lk._touched = false;
  });
  $('v3-lock-clear')?.addEventListener('click', () => {
    const lk = $('v3-cfg-lock'); if (lk) { lk.value = ''; lk._touched = false; }
    saveConfig({ lock_market: '' }, 'Unlock');
  });

  // generic data-cfg panel save (shared .rib-save button for Exit/Guards etc.)
  // Large sets like Guards (236 fields) → split into chunks of 50 to avoid query URL length limits (partial updates are safe)
  document.addEventListener('click', async (e) => {
    const btn = e.target.closest('.rib-save'); if (!btn) return;
    // ★ save target = the strategy container the button belongs to (focus → /focus/config · harpoon → /harpoon/config)
    const cont = btn.closest('.rib-strat');
    const strat = (cont && cont.dataset.ribStrat) || 'focus';
    if (strat !== 'focus' && strat !== 'harpoon') { V3.toast('Saving this strategy will be connected in a later Phase', 'warn'); return; }
    const panel = btn.closest('.rib-panel'); if (!panel) return;
    const entries = [];
    panel.querySelectorAll('[data-cfg]').forEach((el) => {
      const k = el.dataset.cfg;
      if (el.type === 'checkbox') entries.push([k, el.checked ? 'true' : 'false']);
      else { const v = (el.value || '').trim(); if (v !== '') entries.push([k, v]); }
    });
    if (!entries.length) { V3.toast('No values to save', 'warn'); return; }
    const label = btn.dataset.saveLabel || 'Settings';
    const CHUNK = 50;
    V3.toast(label + ' saving… (' + entries.length + ')', 'info');
    let okAll = true, err = '';
    for (let i = 0; i < entries.length; i += CHUNK) {
      const qs = entries.slice(i, i + CHUNK).map(([k, v]) => k + '=' + encodeURIComponent(v)).join('&');
      const d = await V3.getJSON('/api/strategy/' + strat + '/config?' + qs, { method: 'POST', timeoutMs: 40000 });   // guards against slow servers (config POST exceeding 12s under load → AbortError) — saving is a user action so a longer timeout is allowed
      if (!(d && d.ok !== false)) { okAll = false; err = (d && d.error) || 'unknown'; break; }
    }
    V3.toast(okAll ? ('✓ ' + label + ' saved (' + entries.length + ')') : ('✗ ' + label + ' save failed: ' + err),
      okAll ? 'ok' : 'err', okAll ? 3500 : 6000);
    if (strat === 'harpoon' && V3.loadHarpoon) V3.loadHarpoon(true);
    if (V3.pollStatus) V3.pollStatus();
  });

  loadMarkets();
})();
