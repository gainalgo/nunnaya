# ============================================================
# File: app/i18n/__init__.py
# Autocoin OS v3-H — Backend i18n Module
# ============================================================
from __future__ import annotations

import json
import os
from typing import Optional

_locale: str = os.getenv("DEFAULT_LOCALE", "ko")
_translations: dict[str, dict[str, str]] = {}
_BASE_DIR = os.path.dirname(__file__)


def load_locale(locale: str) -> None:
    """Load translation file for given locale."""
    path = os.path.join(_BASE_DIR, f"{locale}.json")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        _translations[locale] = json.load(f)


def set_locale(locale: str) -> None:
    """Set the default locale."""
    global _locale
    _locale = locale


def t(key: str, locale: Optional[str] = None, **kwargs) -> str:
    """Translate a key to the given locale (or default locale).

    Supports parameter substitution with {param} syntax.
    Falls back to key itself if translation not found.
    """
    loc = locale or _locale
    if loc not in _translations:
        load_locale(loc)
    template = _translations.get(loc, {}).get(key, key)
    return template.format(**kwargs) if kwargs else template
