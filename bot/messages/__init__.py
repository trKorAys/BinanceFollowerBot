import os

# PyInstaller dinamik importlari tespit edemediginden
# ceviri modullerini burada dogrudan dahil ediyoruz
from . import (
    messages_en,
    messages_de,
    messages_tr,
    messages_fr,
    messages_ar,
    messages_zh,
    messages_ru,
    messages_ja,
    messages_ko,
)

LANG_MAP = {
    'en': messages_en,
    'de': messages_de,
    'tr': messages_tr,
    'fr': messages_fr,
    'ar': messages_ar,
    'zh': messages_zh,
    'ru': messages_ru,
    'ja': messages_ja,
    'ko': messages_ko,
}

_cache = {}


def _load(lang: str):
    if lang not in _cache:
        mod = LANG_MAP.get(lang)
        if mod is None:
            mod = messages_en
        _cache[lang] = mod.MESSAGES
    return _cache[lang]

def t(key: str, **kwargs) -> str:
    lang = os.getenv('TELEGRAM_LANG', os.getenv('LANGUAGE', 'en')).lower()
    messages = _load(lang)
    if key not in messages:
        messages = _load('en')
    template = messages.get(key, key)
    return template.format(**kwargs)
