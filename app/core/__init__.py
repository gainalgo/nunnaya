# -*- coding: utf-8 -*-
"""
Core 모듈 (Bybit Spot).
"""

from __future__ import annotations

from app.core.hyper_price_feed_bybit import BybitHyperPriceFeed, bybit_price_feed


def get_price_feed() -> BybitHyperPriceFeed:
    """PriceFeed 싱글톤 인스턴스 반환."""
    return bybit_price_feed


# 편의를 위한 alias
PriceFeed = BybitHyperPriceFeed
