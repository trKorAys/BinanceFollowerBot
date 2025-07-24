import os
from importlib import import_module

LANG_MAP = {
    'en': 'messages_en',
    'de': 'messages_de',
    'tr': 'messages_tr',
    'fr': 'messages_fr',
    'ar': 'messages_ar',
    'zh': 'messages_zh',
    'ru': 'messages_ru',
    'ja': 'messages_ja',
    'ko': 'messages_ko',
}

_cache = {}

def _load(lang: str):
    if lang not in _cache:
        mod = import_module(f'.{LANG_MAP.get(lang, "messages_en")}', package=__name__)
        _cache[lang] = mod.MESSAGES
    return _cache[lang]

def t(key: str, **kwargs) -> str:
    lang = os.getenv('TELEGRAM_LANG', os.getenv('LANGUAGE', 'en')).lower()
    messages = _load(lang)
    if key not in messages:
        messages = _load('en')
    template = messages.get(key, key)
    return template.format(**kwargs)
