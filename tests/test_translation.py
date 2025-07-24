import os
from bot.messages import t


def test_translation_en(monkeypatch):
    monkeypatch.setenv('TELEGRAM_LANG', 'en')
    assert t('unauthorized') == 'Unauthorized access'
    monkeypatch.delenv('TELEGRAM_LANG', raising=False)

def test_translation_de(monkeypatch):
    monkeypatch.setenv('TELEGRAM_LANG', 'de')
    assert 'Unbefugter' in t('unauthorized')

