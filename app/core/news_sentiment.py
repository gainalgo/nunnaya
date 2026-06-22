"""
News Sentiment Module — CryptoPanic API 연동

뉴스 감성을 거래 판단에 반영 (ON/OFF 토글로 비교 가능).
fear_greed.py 패턴을 그대로 복제한 독립 모듈.

- FOCUS: conviction bonus (±2)
- Nunnaya: budget multiplier (0.70~1.30)
- 실패 시 항상 neutral 반환 (1.0x, bonus 0)
- config 파일 없으면 기본값 (둘 다 OFF)
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.core.io_utils import safe_load_json, safe_write_json

logger = logging.getLogger(__name__)

try:
    import httpx
except ImportError:
    logger.info("[NewsSentiment] httpx not available, API calls will use fallback")
    httpx = None

# ── Config path ──────────────────────────────────────────────────────
_CONFIG_PATH = os.path.join("runtime", "news_config.json")

_DEFAULT_CONFIG: Dict[str, Any] = {
    "focus_enabled": False,
    "nunnaya_enabled": False,
    "api_key": "",
    "cache_sec": 1800,       # 30분
    "max_stale_sec": 7200,   # 2시간
}


# ── Data classes ─────────────────────────────────────────────────────

@dataclass
class NewsHeadline:
    title: str
    source: str
    sentiment: str          # "bullish", "bearish", "neutral"
    url: str = ""
    ts: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "source": self.source,
            "sentiment": self.sentiment,
            "url": self.url,
            "ts": self.ts,
        }


@dataclass
class NewsSentimentResult:
    """Single coin or overall sentiment result."""
    coin: str                           # "BTC", "ETH", "ALL"
    score: float                        # -1.0 ~ +1.0
    label: str                          # "Very Bearish" ~ "Very Bullish"
    budget_multiplier: float            # 0.70 ~ 1.30
    conviction_bonus: float             # [2026-05-17 100점 ×10] -20.0 ~ +20.0
    headlines: List[Dict[str, Any]] = field(default_factory=list)
    timestamp: float = 0.0
    source: str = "fallback"            # "api", "cache", "cache_stale", "fallback"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "coin": self.coin,
            "score": round(self.score, 3),
            "label": self.label,
            "budget_multiplier": round(self.budget_multiplier, 3),
            "conviction_bonus": self.conviction_bonus,
            "headlines": self.headlines[:10],
            "timestamp": self.timestamp,
            "source": self.source,
        }


# ── Score → Label / Multiplier / Bonus 변환 ─────────────────────────

def _score_to_label(score: float) -> str:
    if score <= -0.6:
        return "Very Bearish"
    elif score <= -0.2:
        return "Bearish"
    elif score <= 0.2:
        return "Neutral"
    elif score <= 0.6:
        return "Bullish"
    else:
        return "Very Bullish"


def _score_to_multiplier(score: float) -> float:
    """score -1.0~+1.0 → budget multiplier 0.70~1.30.

    Linear mapping: -1.0→0.70, 0→1.00, +1.0→1.30
    """
    clamped = max(-1.0, min(1.0, score))
    return round(1.0 + clamped * 0.30, 3)


def _score_to_conviction_bonus(score: float) -> float:
    """[2026-05-17 100점 ×10] score -1.0~+1.0 → conviction bonus -20.0~+20.0.

    Thresholds: |score| >= 0.6 → ±20, |score| >= 0.3 → ±10, else 0
    """
    if score >= 0.6:
        return 20.0
    elif score >= 0.3:
        return 10.0
    elif score <= -0.6:
        return -20.0
    elif score <= -0.3:
        return -10.0
    return 0.0


# ── Coin mapping ─────────────────────────────────────────────────────

# CryptoPanic uses currency codes: BTC, ETH, SOL, etc.
# Gold (XAUTUSDT) needs keyword search
_MARKET_TO_COIN: Dict[str, str] = {
    "XAUTUSDT": "gold",
    "XAGUSD": "silver",
}


def _market_to_coin(market: str) -> str:
    """BTCUSDT → BTC, XAUTUSDT → gold"""
    if market in _MARKET_TO_COIN:
        return _MARKET_TO_COIN[market]
    # Strip USDT/USD/USDC suffix
    for suffix in ("USDT", "USDC", "USD"):
        if market.endswith(suffix):
            return market[: -len(suffix)].upper()
    return market.upper()


# ── CryptoPanic API ──────────────────────────────────────────────────

_CRYPTOPANIC_BASE = "https://cryptopanic.com/api/free/v1/posts/"


def _parse_cryptopanic_response(data: Dict[str, Any], coin: str) -> NewsSentimentResult:
    """Parse CryptoPanic API response into NewsSentimentResult."""
    results = data.get("results", [])
    if not results:
        return _neutral_result(coin)

    headlines: List[Dict[str, Any]] = []
    bullish_count = 0
    bearish_count = 0
    total_voted = 0

    for post in results[:20]:  # Top 20 posts
        votes = post.get("votes", {})
        pos = votes.get("positive", 0) + votes.get("liked", 0)
        neg = votes.get("negative", 0) + votes.get("disliked", 0)

        if pos > neg:
            sentiment = "bullish"
            bullish_count += 1
        elif neg > pos:
            sentiment = "bearish"
            bearish_count += 1
        else:
            sentiment = "neutral"

        total_voted += 1

        headlines.append(NewsHeadline(
            title=post.get("title", ""),
            source=post.get("source", {}).get("title", "unknown"),
            sentiment=sentiment,
            url=post.get("url", ""),
            ts=time.time(),
        ).to_dict())

    # Score: -1.0 ~ +1.0 based on bullish/bearish ratio
    if total_voted > 0:
        score = (bullish_count - bearish_count) / total_voted
    else:
        score = 0.0

    # Clamp
    score = max(-1.0, min(1.0, score))

    return NewsSentimentResult(
        coin=coin,
        score=score,
        label=_score_to_label(score),
        budget_multiplier=_score_to_multiplier(score),
        conviction_bonus=_score_to_conviction_bonus(score),
        headlines=headlines[:10],
        timestamp=time.time(),
        source="api",
    )


def _analyze_sentiment_keywords(title: str) -> str:
    """간단한 키워드 매칭 sentiment 분석 (2026-05-18 RSS 도입).

    1차 단순 모델 — 향후 AI 분석/카테고리 모델로 진화 가능.
    Returns: "bullish" / "bearish" / "neutral"
    """
    if not title:
        return "neutral"
    t = title.lower()
    bullish_kw = [
        "rally", "surge", "soar", "ath", "all-time high", "moon", "breakout",
        "bullish", "rocket", "jump", "gain", "skyrocket", "outperform",
        "approval", "approved", "adoption", "partnership", "upgrade",
        "buy", "long", "accumulate", "support holds",
    ]
    bearish_kw = [
        "crash", "dump", "plunge", "sell-off", "selloff", "bearish",
        "drop", "decline", "tumble", "fall", "slip", "collapse", "crater",
        "hack", "exploit", "rug", "scam", "fraud", "ban", "banned",
        "lawsuit", "sue", "sec ", "regulation", "fud", "liquidation",
        "short", "sell", "panic", "fear",
    ]
    b_score = sum(1 for kw in bullish_kw if kw in t)
    s_score = sum(1 for kw in bearish_kw if kw in t)
    if b_score > s_score:
        return "bullish"
    elif s_score > b_score:
        return "bearish"
    return "neutral"


def _neutral_result(coin: str = "ALL") -> NewsSentimentResult:
    """Fallback neutral result — no effect on trading."""
    return NewsSentimentResult(
        coin=coin,
        score=0.0,
        label="Neutral",
        budget_multiplier=1.0,
        conviction_bonus=0,
        headlines=[],
        timestamp=time.time(),
        source="fallback",
    )


# ── Main Class ───────────────────────────────────────────────────────

class NewsSentiment:
    """News Sentiment Engine.

    - CryptoPanic API로 뉴스 감성 수집
    - 코인별/전체 감성 점수 + 예산 배율 + conviction 보너스
    - 30분 캐시, 실패 시 neutral fallback
    """

    def __init__(self):
        self.config: Dict[str, Any] = dict(_DEFAULT_CONFIG)
        self._load_config()

        # Per-coin cache: coin → NewsSentimentResult
        self._cache: Dict[str, NewsSentimentResult] = {}

    # ── Config I/O ───────────────────────────────────────────────

    def _load_config(self) -> None:
        """Load config from disk, merge with defaults."""
        on_disk = safe_load_json(_CONFIG_PATH, default={})
        merged = dict(_DEFAULT_CONFIG)
        for k, v in on_disk.items():
            if k in merged:
                merged[k] = v
        self.config = merged

    def _save_config(self) -> None:
        """Persist current config to disk."""
        safe_write_json(_CONFIG_PATH, self.config)

    def update_config(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Update config fields and persist."""
        for k, v in patch.items():
            if k in self.config:
                self.config[k] = v
        self._save_config()
        return dict(self.config)

    # ── Public API ───────────────────────────────────────────────

    def get_sentiment(self, coin: str = "ALL") -> NewsSentimentResult:
        """Get sentiment for a coin (sync).

        coin: "BTC", "ETH", "gold", "ALL"
        Returns: NewsSentimentResult (always, never raises)
        """
        now = time.time()
        cache_sec = self.config.get("cache_sec", 1800)
        max_stale = self.config.get("max_stale_sec", 7200)

        # Check cache
        cached = self._cache.get(coin)
        if cached:
            age = now - cached.timestamp
            if age < cache_sec:
                return NewsSentimentResult(
                    coin=cached.coin,
                    score=cached.score,
                    label=cached.label,
                    budget_multiplier=cached.budget_multiplier,
                    conviction_bonus=cached.conviction_bonus,
                    headlines=cached.headlines,
                    timestamp=cached.timestamp,
                    source="cache",
                )

        # Fetch from API
        try:
            result = self._fetch_from_api(coin)
            # ★ [2026-05-18] "rss" 도 valid source (CryptoPanic 죽은 후 RSS 추가)
            if result.source in ("api", "rss"):
                self._cache[coin] = result
                return result
        except Exception as exc:
            logger.warning("[NewsSentiment] API fetch error for %s: %s", coin, exc)

        # Stale cache or neutral fallback
        if cached:
            age = now - cached.timestamp
            if age > max_stale:
                return _neutral_result(coin)
            return NewsSentimentResult(
                coin=cached.coin,
                score=cached.score,
                label=cached.label,
                budget_multiplier=cached.budget_multiplier,
                conviction_bonus=cached.conviction_bonus,
                headlines=cached.headlines,
                timestamp=cached.timestamp,
                source="cache_stale",
            )

        return _neutral_result(coin)

    def get_all_cached(self) -> Dict[str, Dict[str, Any]]:
        """Return all cached sentiments (for dashboard)."""
        return {k: v.to_dict() for k, v in self._cache.items()}

    def clear_cache(self) -> None:
        """Force clear all cached data."""
        self._cache.clear()

    # ── API Fetch ────────────────────────────────────────────────

    def _fetch_from_api(self, coin: str) -> NewsSentimentResult:
        """Fetch news from RSS feeds (primary, 2026-05-18 / 2026-05-19 정리).

        ★ [2026-05-19 부모님 정정] CryptoPanic 죽은 endpoint 호출 자체 skip.
        로그 noise (404) 발생 + 호출 시간 낭비 = 가치 X.
        옛 CryptoPanic 코드 = config flag `cryptopanic_fallback_enabled` (default False) 로 보존.
        새 endpoint 부활 시 flag ON 으로 재활성 가능.
        """
        if httpx is None:
            return _neutral_result(coin)

        # 1. RSS 우선 (무료 + 작동 보장) — 사실상 유일한 소스
        rss_result = self._fetch_rss(coin)
        if rss_result.source == "rss":
            return rss_result

        # 2. CryptoPanic 폴백 — 2026-05-19 default OFF (죽은 endpoint, 404 noise)
        if not self.config.get("cryptopanic_fallback_enabled", False):
            return rss_result  # neutral 이라도 RSS 결과 그대로

        api_key = self.config.get("api_key", "")
        if api_key:
            params: Dict[str, str] = {
                "auth_token": api_key,
                "kind": "news",
                "filter": "hot",
            }
            if coin and coin.upper() not in ("ALL", "GOLD", "SILVER"):
                params["currencies"] = coin.upper()
            elif coin.lower() == "gold":
                params["filter"] = "important"
            try:
                with httpx.Client(timeout=10.0) as client:
                    resp = client.get(_CRYPTOPANIC_BASE, params=params)
                    resp.raise_for_status()
                    data = resp.json()
                return _parse_cryptopanic_response(data, coin)
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.debug("[NewsSentiment] CryptoPanic fallback network error: %s", e)
            except Exception as exc:
                logger.debug("[NewsSentiment] CryptoPanic fallback error: %s", exc)

        return _neutral_result(coin)

    def _fetch_public(self, coin: str) -> NewsSentimentResult:
        """Fetch from CryptoPanic public endpoint (no API key).

        ★ [2026-05-18 부모님 정정] CryptoPanic 옛 v1 endpoint 죽음 (404). RSS 로 대체.
        본 함수는 호환성 유지용 — 실제로는 _fetch_rss() 호출.
        """
        return self._fetch_rss(coin)

    def _fetch_rss(self, coin: str) -> NewsSentimentResult:
        """RSS feed 기반 뉴스 sentiment (2026-05-18 부모님 결정 — CryptoPanic 죽은 후 대체).

        무료 + key X + 다중 소스 (CoinDesk + Cointelegraph + Decrypt).
        Sentiment 분석 = 키워드 매칭 (1차 단순). 향후 AI 분석으로 진화 가능.
        """
        if httpx is None:
            return _neutral_result(coin)

        sources = [
            ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss?outputType=xml"),
            ("Cointelegraph", "https://cointelegraph.com/rss"),
            ("Decrypt", "https://decrypt.co/feed"),
        ]

        headlines: List[Dict[str, Any]] = []
        bullish_count = 0
        bearish_count = 0
        coin_upper = coin.upper() if coin else "ALL"

        for source_name, url in sources:
            try:
                with httpx.Client(timeout=8.0, follow_redirects=True) as client:
                    resp = client.get(url, headers={"User-Agent": "Mozilla/5.0 Autocoin News Reader"})
                    if resp.status_code != 200:
                        continue
                    import xml.etree.ElementTree as ET
                    root = ET.fromstring(resp.content)
                    for item in root.iter("item"):
                        title_el = item.find("title")
                        title = (title_el.text or "").strip() if title_el is not None else ""
                        if not title:
                            continue
                        # Coin filter (ALL 이면 모두, 그 외엔 제목에 코인 심볼 포함된 것만)
                        title_upper = title.upper()
                        if coin_upper not in ("ALL", "GOLD", "SILVER"):
                            if coin_upper not in title_upper:
                                continue
                        elif coin_upper == "GOLD":
                            if "GOLD" not in title_upper and "XAU" not in title_upper:
                                continue
                        # Sentiment 분석
                        sentiment = _analyze_sentiment_keywords(title)
                        if sentiment == "bullish":
                            bullish_count += 1
                        elif sentiment == "bearish":
                            bearish_count += 1
                        link_el = item.find("link")
                        url_val = (link_el.text or "").strip() if link_el is not None else ""
                        headlines.append({
                            "title": title[:200],
                            "source": source_name,
                            "sentiment": sentiment,
                            "url": url_val[:200],
                            "ts": time.time(),
                        })
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.debug("[NewsSentiment] RSS %s network error: %s", source_name, e)
            except Exception as exc:
                logger.debug("[NewsSentiment] RSS %s error: %s", source_name, exc)

        total = len(headlines)
        if total == 0:
            return _neutral_result(coin)

        # Score: bullish/bearish 비율 (-1.0 ~ +1.0)
        score = (bullish_count - bearish_count) / total
        score = max(-1.0, min(1.0, score))

        return NewsSentimentResult(
            coin=coin,
            score=score,
            label=_score_to_label(score),
            budget_multiplier=_score_to_multiplier(score),
            conviction_bonus=_score_to_conviction_bonus(score),
            headlines=headlines[:10],
            timestamp=time.time(),
            source="rss",
        )

    # ── Status (for API / dashboard) ─────────────────────────────

    def get_status(self) -> Dict[str, Any]:
        """Full status for API endpoint."""
        all_cached = self.get_all_cached()

        # Overall sentiment (ALL or average of cached)
        overall = all_cached.get("ALL", _neutral_result("ALL").to_dict())

        # [2026-04-09 보안] api_key 마스킹 후 반환
        safe_config = dict(self.config)
        _ak = safe_config.get("api_key", "")
        safe_config["api_key"] = (_ak[:4] + "****") if len(_ak) > 4 else ("****" if _ak else "")

        return {
            "config": safe_config,
            "overall": overall,
            "coins": {k: v for k, v in all_cached.items() if k != "ALL"},
            "cache_count": len(self._cache),
        }


# ── Singleton ────────────────────────────────────────────────────────

_instance: Optional[NewsSentiment] = None


def get_news_sentiment() -> NewsSentiment:
    """Get singleton NewsSentiment instance."""
    global _instance
    if _instance is None:
        _instance = NewsSentiment()
    return _instance
