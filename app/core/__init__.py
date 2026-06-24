# -*- coding: utf-8 -*-
"""
Core module (Bybit Spot).
"""

from __future__ import annotations

from app.core.hyper_price_feed_bybit import BybitHyperPriceFeed, bybit_price_feed


def get_price_feed() -> BybitHyperPriceFeed:
    """Return the PriceFeed singleton instance."""
    return bybit_price_feed


# Convenience alias
PriceFeed = BybitHyperPriceFeed
