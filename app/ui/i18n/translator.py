# ============================================================
# File: app/ui/i18n/translator.py
# ------------------------------------------------------------
# Translator
# - Central translation module for multi-language support
# - Loads .json files from the i18n directory to provide translations
# - Can be used across the project (FastAPI, UI, etc.)
# ============================================================

import json
import logging
import os
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class Translator:
    """
    A class that handles JSON-based multi-language translation.
    """

    def __init__(self, lang_dir: str, default_lang: str = "en"):
        """
        Loads language files (.json) from the specified directory.

        Args:
            lang_dir (str): Path to the directory containing 'ko.json', 'en.json', etc.
            default_lang (str): Default language used when a translation is not found.
        """
        self.lang_dir = lang_dir
        self.default_lang = default_lang
        self.translations: Dict[str, Dict[str, str]] = {}
        self._load_languages()

    def _load_languages(self):
        """Loads all .json files in the directory and stores them in memory."""
        if not os.path.isdir(self.lang_dir):
            logger.warning("Language directory not found at '%s'", self.lang_dir)
            return

        for filename in os.listdir(self.lang_dir):
            if filename.endswith(".json"):
                lang_code = filename.split(".")[0]
                filepath = os.path.join(self.lang_dir, filename)
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        self.translations[lang_code] = json.load(f)
                except (json.JSONDecodeError, IOError) as e:
                    logger.warning("Error loading language file '%s': %s", filepath, e, exc_info=True)

    def get(self, key: str, lang: str, fallback: Optional[str] = None) -> str:
        """
        Returns the translated string for the given key and language.

        - Priority 1: Look up the key in the requested language (lang).
        - Priority 2: Look up the key in the default language (default_lang).
        - Priority 3: Return the fallback if one is provided.
        - Priority 4: Return the key itself.

        Args:
            key (str): Translation key (e.g., "action.add").
            lang (str): Language code (e.g., "ko", "en").
            fallback (Optional[str]): Default string to return when all lookups fail.

        Returns:
            str: The translated string.
        """
        return self.translations.get(lang, {}).get(
            key,
            self.translations.get(self.default_lang, {}).get(
                key, fallback if fallback is not None else key
            ),
        )

# --- Create global instance ---
# Used like a singleton for easy access throughout the application

# Set the i18n directory path relative to this file's location
I18N_DIR = os.path.dirname(os.path.abspath(__file__))

translator = Translator(lang_dir=I18N_DIR, default_lang="en")
