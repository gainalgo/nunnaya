# ============================================================
# File: app/ui/i18n/translator.py
# ------------------------------------------------------------
# Translator
# - 다국어 지원을 위한 중앙 번역기 모듈
# - i18n 디렉터리의 .json 파일들을 로드하여 번역 제공
# - FastAPI, UI 등 프로젝트 전반에서 사용될 수 있음
# ============================================================

import json
import logging
import os
from typing import Dict, Optional

logger = logging.getLogger(__name__)


class Translator:
    """
    JSON 기반의 다국어 번역을 처리하는 클래스.
    """

    def __init__(self, lang_dir: str, default_lang: str = "en"):
        """
        지정된 디렉터리에서 언어 파일(.json)을 로드합니다.

        Args:
            lang_dir (str): 'ko.json', 'en.json' 등이 위치한 디렉터리 경로.
            default_lang (str): 번역을 찾지 못했을 때 사용할 기본 언어.
        """
        self.lang_dir = lang_dir
        self.default_lang = default_lang
        self.translations: Dict[str, Dict[str, str]] = {}
        self._load_languages()

    def _load_languages(self):
        """디렉터리 내의 모든 .json 파일을 로드하여 메모리에 저장합니다."""
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
        주어진 키와 언어에 대한 번역된 문자열을 반환합니다.

        - 1순위: 요청된 언어(lang)에서 키를 찾습니다.
        - 2순위: 기본 언어(default_lang)에서 키를 찾습니다.
        - 3순위: fallback이 제공되면 그것을 반환합니다.
        - 4순위: 키 자체를 반환합니다.

        Args:
            key (str): 번역 키 (예: "action.add").
            lang (str): 언어 코드 (예: "ko", "en").
            fallback (Optional[str]): 모든 경우에 실패했을 때 반환할 기본 문자열.

        Returns:
            str: 번역된 문자열.
        """
        return self.translations.get(lang, {}).get(
            key,
            self.translations.get(self.default_lang, {}).get(
                key, fallback if fallback is not None else key
            ),
        )

# --- 전역 인스턴스 생성 ---
# 애플리케이션 전체에서 쉽게 접근할 수 있도록 싱글턴처럼 사용

# 이 파일의 위치를 기준으로 i18n 디렉터리 경로를 설정
I18N_DIR = os.path.dirname(os.path.abspath(__file__))

translator = Translator(lang_dir=I18N_DIR, default_lang="en")
