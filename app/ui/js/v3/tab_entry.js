/* ============================================================
   tab_entry.js — 리본 조건 패널 로직 (진입 / 모드 / 사이즈 / 시장락)
   진입=manual-entry(확인 후) / 모드·사이즈·락=/config 저장. 현재 FOCUS 전략 대상.
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
      if (cnt) cnt.textContent = m.length + '개 USDT 마켓';
      V3.state.markets = m;
    } else if (cnt) { cnt.textContent = '마켓 조회 실패'; }
  }

  // 폴링 시 config 입력 동기화 — ★ 전략 컨테이너 단위로 격리.
  // (FOCUS·HARPOON 가 leverage/risk_pct/cooldown_sec/min_adx 등 같은 data-cfg 키를 쓰므로
  //  컨테이너 밖 전역 selector 로 쓰면 서로의 값을 덮어씀 → 반드시 .rib-strat 안에서만)
  function syncCfgInto(rootSel, cfg) {
    const root = document.querySelector(rootSel);
    if (!root || !cfg) return;
    const expand = root.querySelector('.ribbon-expand');
    if (expand && !expand.hidden) return;   // 그 전략 펼침 패널 편집 중이면 동결
    root.querySelectorAll('[data-cfg]').forEach((el) => {
      if (document.activeElement === el) return;
      const k = el.dataset.cfg; if (cfg[k] == null) return;
      if (el.type === 'checkbox') el.checked = !!cfg[k];
      else el.value = Array.isArray(cfg[k]) ? cfg[k].join(',') : cfg[k];   // whitelist/blacklist = array
    });
  }
  V3.syncCfgInto = syncCfgInto;

  // FOCUS config → FOCUS 리본만 (data-cfg + id 전용 필드 mode/lock)
  V3.syncEntryConfig = (cfg) => {
    const root = document.querySelector('.rib-strat[data-rib-strat="focus"]');
    if (!root || !cfg) return;
    const expand = root.querySelector('.ribbon-expand');
    if (expand && !expand.hidden) return;
    const md = $('v3-entry-mode'); if (md && document.activeElement !== md && cfg.entry_mode) md.value = cfg.entry_mode;
    const lk = $('v3-cfg-lock'); if (lk && document.activeElement !== lk && !lk._touched) lk.value = cfg.lock_market || '';
    syncCfgInto('.rib-strat[data-rib-strat="focus"]', cfg);
  };
  // HARPOON config → HARPOON 리본만 (loadHarpoon 이 status.config 로 호출)
  V3.syncHarpoonConfig = (cfg) => syncCfgInto('.rib-strat[data-rib-strat="harpoon"]', cfg);

  function focusOnly() {
    if (V3.state.active !== 'focus') { V3.toast('현재 FOCUS만 연결됨 (다른 전략은 Phase 6)', 'warn'); return false; }
    return true;
  }

  async function saveConfig(params, label) {
    if (!focusOnly()) return;
    const qs = Object.entries(params).map(([k, v]) => k + '=' + encodeURIComponent(v)).join('&');
    const d = await V3.getJSON('/api/strategy/focus/config?' + qs, { method: 'POST', timeoutMs: 40000 });
    if (d && d.ok !== false) V3.toast('✓ ' + label + ' 저장', 'ok');
    else V3.toast('✗ ' + label + ' 저장 실패: ' + ((d && d.error) || ''), 'err', 6000);
    if (V3.pollStatus) V3.pollStatus();
  }

  // 🖐 수동 진입(LONG/SHORT)은 우측 위젯 → 본문 가로 100% 표로 이동 (dashboard_v3.js .v3-me-go). 리본=설정만.
  $('v3-mode-apply')?.addEventListener('click', () => { const m = $('v3-entry-mode').value; if (m) saveConfig({ entry_mode: m }, '모드'); });
  $('v3-size-apply')?.addEventListener('click', () => {
    const p = {}; const b = $('v3-cfg-budget').value, l = $('v3-cfg-leverage').value, r = $('v3-cfg-risk').value;
    if (b !== '') p.budget_usdt = b; if (l !== '') p.leverage = l; if (r !== '') p.risk_pct = r;
    if (!Object.keys(p).length) { V3.toast('저장할 값이 없습니다', 'warn'); return; }
    saveConfig(p, '사이즈');
  });
  $('v3-cfg-lock')?.addEventListener('input', (e) => { e.target._touched = true; });
  $('v3-lock-apply')?.addEventListener('click', () => {
    const v = ($('v3-cfg-lock').value || '').trim().toUpperCase();
    saveConfig({ lock_market: v }, v ? ('락 ' + v) : '락 해제');
    const lk = $('v3-cfg-lock'); if (lk) lk._touched = false;
  });
  $('v3-lock-clear')?.addEventListener('click', () => {
    const lk = $('v3-cfg-lock'); if (lk) { lk.value = ''; lk._touched = false; }
    saveConfig({ lock_market: '' }, '락 해제');
  });

  // generic data-cfg 패널 저장 (Exit/Guards 등 .rib-save 버튼 공용)
  // Guards 236개 등 대량 → query URL 길이 한계 회피 위해 청크(50)로 분할 POST (부분 업데이트 안전)
  document.addEventListener('click', async (e) => {
    const btn = e.target.closest('.rib-save'); if (!btn) return;
    // ★ 저장 대상 = 그 버튼이 속한 전략 컨테이너 (focus → /focus/config · harpoon → /harpoon/config)
    const cont = btn.closest('.rib-strat');
    const strat = (cont && cont.dataset.ribStrat) || 'focus';
    if (strat !== 'focus' && strat !== 'harpoon') { V3.toast('이 전략 저장은 다음 Phase 연결', 'warn'); return; }
    const panel = btn.closest('.rib-panel'); if (!panel) return;
    const entries = [];
    panel.querySelectorAll('[data-cfg]').forEach((el) => {
      const k = el.dataset.cfg;
      if (el.type === 'checkbox') entries.push([k, el.checked ? 'true' : 'false']);
      else { const v = (el.value || '').trim(); if (v !== '') entries.push([k, v]); }
    });
    if (!entries.length) { V3.toast('저장할 값이 없습니다', 'warn'); return; }
    const label = btn.dataset.saveLabel || '설정';
    const CHUNK = 50;
    V3.toast(label + ' 저장 중… (' + entries.length + '개)', 'info');
    let okAll = true, err = '';
    for (let i = 0; i < entries.length; i += CHUNK) {
      const qs = entries.slice(i, i + CHUNK).map(([k, v]) => k + '=' + encodeURIComponent(v)).join('&');
      const d = await V3.getJSON('/api/strategy/' + strat + '/config?' + qs, { method: 'POST', timeoutMs: 40000 });   // 느린 서버(부하 시 config POST 12s 초과 → AbortError) 대비 — 저장은 사용자 액션이라 길게 허용
      if (!(d && d.ok !== false)) { okAll = false; err = (d && d.error) || '알 수 없음'; break; }
    }
    V3.toast(okAll ? ('✓ ' + label + ' 저장 (' + entries.length + '개)') : ('✗ ' + label + ' 저장 실패: ' + err),
      okAll ? 'ok' : 'err', okAll ? 3500 : 6000);
    if (strat === 'harpoon' && V3.loadHarpoon) V3.loadHarpoon(true);
    if (V3.pollStatus) V3.pollStatus();
  });

  loadMarkets();
})();
