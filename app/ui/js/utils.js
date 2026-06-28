/* ============================================================
 * Autocoin OS v3-H — Common Utilities
 * Performance-optimized helper functions
 * ============================================================ */

"use strict";

/* =========================
 * Debounce
 * ========================= */
function debounce(fn, delay = 300) {
  let timer = null;
  return function (...args) {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => {
      fn.apply(this, args);
      timer = null;
    }, delay);
  };
}

/* =========================
 * Throttle
 * ========================= */
function throttle(fn, limit = 100) {
  let inThrottle = false;
  return function (...args) {
    if (!inThrottle) {
      fn.apply(this, args);
      inThrottle = true;
      setTimeout(() => (inThrottle = false), limit);
    }
  };
}

/* =========================
 * Request Animation Frame Throttle
 * ========================= */
function rafThrottle(fn) {
  let ticking = false;
  return function (...args) {
    if (!ticking) {
      requestAnimationFrame(() => {
        fn.apply(this, args);
        ticking = false;
      });
      ticking = true;
    }
  };
}

/* =========================
 * Batch DOM Updates
 * ========================= */
function batchDOMUpdate(container, items, renderItem) {
  const fragment = document.createDocumentFragment();
  items.forEach((item, index) => {
    const el = renderItem(item, index);
    if (el) fragment.appendChild(el);
  });
  container.innerHTML = "";
  container.appendChild(fragment);
}

/* =========================
 * Safe JSON Parse
 * ========================= */
function safeJsonParse(str, defaultVal = null) {
  try {
    return JSON.parse(str);
  } catch (e) {
    return defaultVal;
  }
}

/* =========================
 * Format Number (Currency)
 * =========================
 * Currency Abstraction:
 * - formatCurrency() is the canonical function for formatting monetary values.
 * - It uses the current quote currency settings from /api/system/currency.
 * - formatUSDT() is the default formatter for USDT values.
 */

// Quote currency config (USDT default)
let _quoteCurrencyConfig = {
  symbol: "USDT",
  decimals: 2,
  locale: "en-US",
  prefix: "",
  suffix: " USDT",
};

function setQuoteCurrencyConfig(cfg) {
  if (cfg && cfg.symbol) {
    _quoteCurrencyConfig = {
      symbol: cfg.symbol || "USDT",
      decimals: cfg.decimals ?? 2,
      locale: cfg.locale || "en-US",
      prefix: cfg.prefix || "",
      suffix: cfg.suffix || " USDT",
    };
  }
}

function getQuoteCurrencyConfig() {
  return _quoteCurrencyConfig;
}

function formatCurrency(num, decimals = null) {
  if (num === null || num === undefined || isNaN(num)) return "-";
  const d = decimals !== null ? decimals : _quoteCurrencyConfig.decimals;
  const formatted = Number(num).toLocaleString(_quoteCurrencyConfig.locale, {
    minimumFractionDigits: d,
    maximumFractionDigits: d,
  });
  return _quoteCurrencyConfig.prefix + formatted + _quoteCurrencyConfig.suffix;
}

// USDT formatter
function formatUSDT(num, decimals = 2) {
  if (num === null || num === undefined || isNaN(num)) return "-";
  return Number(num).toLocaleString("en-US", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

// Backward compatibility alias
// Legacy alias removed;



/* =========================
 * Format Percent
 * ========================= */
function formatPct(num, decimals = 2) {
  if (num === null || num === undefined || isNaN(num)) return "-";
  return Number(num).toFixed(decimals) + "%";
}

/* =========================
 * Simple Cache with TTL
 * ========================= */
class SimpleCache {
  constructor(ttlMs = 5000) {
    this._cache = new Map();
    this._ttl = ttlMs;
  }

  get(key) {
    const entry = this._cache.get(key);
    if (!entry) return null;
    if (Date.now() > entry.expires) {
      this._cache.delete(key);
      return null;
    }
    return entry.value;
  }

  set(key, value) {
    this._cache.set(key, {
      value,
      expires: Date.now() + this._ttl,
    });
  }

  clear() {
    this._cache.clear();
  }
}

/* =========================
 * Authenticated Fetch (Basic Auth supported)
 * ========================= */
async function authFetch(url, options = {}) {
  // credentials: 'include' makes the browser auto-attach saved Basic Auth (cross-origin included)
  const mergedOptions = {
    credentials: 'include',
    ...options,
  };
  return fetch(url, mergedOptions);
}

/* =========================
 * API Request with Cache
 * ========================= */
const apiCache = new SimpleCache(3000); // 3 second cache

async function cachedFetch(url, options = {}) {
  const cacheKey = url + JSON.stringify(options);
  const cached = apiCache.get(cacheKey);
  if (cached) return cached;

  const response = await authFetch(url, options);
  const data = await response.json();
  apiCache.set(cacheKey, data);
  return data;
}

/* =========================
 * Interval Manager (prevent duplicates)
 * ========================= */
const IntervalManager = {
  _intervals: new Map(),

  set(name, callback, intervalMs) {
    this.clear(name);
    const id = setInterval(callback, intervalMs);
    this._intervals.set(name, id);
    return id;
  },

  clear(name) {
    const id = this._intervals.get(name);
    if (id) {
      clearInterval(id);
      this._intervals.delete(name);
    }
  },

  clearAll() {
    this._intervals.forEach((id) => clearInterval(id));
    this._intervals.clear();
  },
};

/* =========================
 * Export for module usage (optional)
 * ========================= */
if (typeof window !== "undefined") {
  window.AutocoinUtils = {
    debounce,
    throttle,
    rafThrottle,
    batchDOMUpdate,
    safeJsonParse,
    formatCurrency,
    setQuoteCurrencyConfig,
    getQuoteCurrencyConfig,
    formatUSDT,
    
    formatPct,
    SimpleCache,
    authFetch,
    cachedFetch,
    IntervalManager,
  };
}
