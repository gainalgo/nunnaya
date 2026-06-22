/* ============================================================
   ribbon.js — 리본 탭 = 아래로 펼침 패널 (Excel 리본 collapse)
   ★ per-strategy: 탭/패널/펼침은 active 전략 컨테이너(.rib-strat.show) 안에서만 동작.
   탭 클릭 → 그 패널 펼침 / 같은 탭 재클릭 or Esc → 접힘 / Ctrl+1~9 (active 컨테이너 탭 순서)
   ============================================================ */
(function () {
  'use strict';
  const V3 = window.V3 = window.V3 || {};
  let openTab = null;   // 현재 펼친 탭 (active 컨테이너 기준)

  function activeContainer() { return document.querySelector('.rib-strat.show'); }

  function show(rib) {
    const c = activeContainer(); if (!c) return;
    if (openTab === rib) { collapse(); return; }
    openTab = rib;
    c.querySelectorAll('.ribbon-tab').forEach((t) => t.classList.toggle('active', t.dataset.rib === rib));
    c.querySelectorAll('.rib-panel').forEach((p) => p.classList.toggle('active', p.dataset.panel === rib));
    const expand = c.querySelector('.ribbon-expand');
    if (expand) expand.hidden = false;
  }
  function collapse() {
    openTab = null;
    document.querySelectorAll('.rib-strat .ribbon-tab').forEach((t) => t.classList.remove('active'));
    document.querySelectorAll('.rib-strat .ribbon-expand').forEach((x) => { x.hidden = true; });
  }
  V3.ribbonShow = show;
  V3.ribbonCollapse = collapse;

  // active 전략 전환: 리본 컨테이너 show/hide (fold) + 열린 탭 접기
  V3.ribbonSetActive = (key) => {
    collapse();
    document.querySelectorAll('.rib-strat').forEach((c) => c.classList.toggle('show', c.dataset.ribStrat === key));
  };

  // 탭 클릭 = 위임 (탭이 여러 컨테이너에 존재) — 숨은 컨테이너 탭은 무시
  document.addEventListener('click', (e) => {
    const tab = e.target.closest('.ribbon-tab'); if (!tab) return;
    if (!tab.closest('.rib-strat.show')) return;
    show(tab.dataset.rib);
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { collapse(); return; }
    if (!e.ctrlKey || e.altKey || e.metaKey) return;
    if (e.key >= '1' && e.key <= '9') {
      const c = activeContainer(); if (!c) return;
      const tabs = Array.from(c.querySelectorAll('.ribbon-tab'));
      const t = tabs[(+e.key) - 1];
      if (t) { e.preventDefault(); show(t.dataset.rib); }
    }
  });
})();
