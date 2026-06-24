/* ============================================================
   ribbon.js — ribbon tabs = drop-down expanding panels (Excel ribbon collapse)
   ★ per-strategy: tabs/panels/expansion only work inside the active strategy container (.rib-strat.show).
   click tab → expand its panel / re-click same tab or Esc → collapse / Ctrl+1~9 (active container tab order)
   ============================================================ */
(function () {
  'use strict';
  const V3 = window.V3 = window.V3 || {};
  let openTab = null;   // currently expanded tab (relative to active container)

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

  // switch active strategy: show/hide ribbon container (fold) + collapse open tab
  V3.ribbonSetActive = (key) => {
    collapse();
    document.querySelectorAll('.rib-strat').forEach((c) => c.classList.toggle('show', c.dataset.ribStrat === key));
  };

  // tab click = delegated (tabs exist in multiple containers) — ignore tabs in hidden containers
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
