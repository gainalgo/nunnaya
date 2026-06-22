// ============================================================
// File: app/ui/js/i18n.js
// Autocoin OS v3-H — Frontend i18n Engine
// ============================================================

"use strict";

const I18N = (function () {
  const STORAGE_KEY = "autocoin_locale";
  const DEFAULT_LOCALE = "ko";

  let _locale = localStorage.getItem(STORAGE_KEY) || DEFAULT_LOCALE;
  let _data = {};           // { locale: { key: "translation", ... }, ... }

  /* ---------------------------------------------------------
   *  t(key, params?) — 번역 문자열 반환
   *  예: t("profit_label", { market: "BTC" })
   *      "profit_label": "{market} 수익" → "BTC 수익"
   * ------------------------------------------------------- */
  function t(key, params) {
    var dict = _data[_locale] || {};
    var text = dict[key];
    if (text === undefined) return key;
    if (!params) return text;
    return text.replace(/\{(\w+)\}/g, function (_, name) {
      return params[name] !== undefined ? params[name] : "{" + name + "}";
    });
  }

  /* ---------------------------------------------------------
   *  apply() — DOM 전체에 번역 적용
   *    data-i18n            → textContent
   *    data-i18n-title      → title
   *    data-i18n-placeholder→ placeholder
   * ------------------------------------------------------- */
  function apply() {
    var dict = _data[_locale];
    if (!dict) return;

    var els = document.querySelectorAll("[data-i18n]");
    for (var i = 0; i < els.length; i++) {
      var key = els[i].getAttribute("data-i18n");
      if (dict[key] !== undefined) els[i].textContent = dict[key];
    }

    var titles = document.querySelectorAll("[data-i18n-title]");
    for (var j = 0; j < titles.length; j++) {
      var tk = titles[j].getAttribute("data-i18n-title");
      if (dict[tk] !== undefined) titles[j].title = dict[tk];
    }

    var phs = document.querySelectorAll("[data-i18n-placeholder]");
    for (var k = 0; k < phs.length; k++) {
      var pk = phs[k].getAttribute("data-i18n-placeholder");
      if (dict[pk] !== undefined) phs[k].placeholder = dict[pk];
    }
  }

  /* ---------------------------------------------------------
   *  load(locale) — JSON 로드 → 캐시 → apply
   * ------------------------------------------------------- */
  function load(locale) {
    locale = locale || _locale;
    _locale = locale;
    localStorage.setItem(STORAGE_KEY, locale);

    // 캐시 히트 → 재사용
    if (_data[locale]) {
      apply();
      return Promise.resolve();
    }

    return fetch("/ui/i18n/" + locale + ".json")
      .then(function (res) {
        if (!res.ok) throw new Error("i18n: " + res.status);
        return res.json();
      })
      .then(function (json) {
        _data[locale] = json;
        apply();
      })
      .catch(function (err) {
        console.warn("[I18N] load failed:", locale, err.message);
      });
  }

  /* ---------------------------------------------------------
   *  locale getter
   * ------------------------------------------------------- */
  function locale() {
    return _locale;
  }

  /* ---------------------------------------------------------
   *  DOMContentLoaded — 자동 초기화
   * ------------------------------------------------------- */
  document.addEventListener("DOMContentLoaded", function () {
    var sel = document.getElementById("lang-select");
    if (sel) {
      sel.value = _locale;
      sel.addEventListener("change", function () {
        load(sel.value);
      });
    }
    load(_locale);
  });

  return { t: t, apply: apply, load: load, locale: locale };
})();

window.I18N = I18N;
