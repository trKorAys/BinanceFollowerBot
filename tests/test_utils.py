
import os
import sys
from datetime import datetime, timezone

# Proje kök dizinini test path'ine ekle
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from bot.utils import (
    FifoTracker,
    get_current_utc_iso,
    convert_utc_to_local,
    convert_utc_to_timezone,
    convert_utc_to_env_timezone,
    extract_step_size,
    extract_min_qty,
    extract_max_qty,
    extract_min_notional,
    seconds_until_next_midnight,
    seconds_until_next_six_hour,
)


def test_fifo_average():
    tracker = FifoTracker()
    tracker.add_trade(1, 10)
    tracker.add_trade(2, 20)
    assert tracker.average_price() == (1 * 10 + 2 * 20) / 3
    assert tracker.total_qty() == 3


def test_fifo_sell():
    tracker = FifoTracker()
    tracker.add_trade(1, 10)
    tracker.add_trade(1, 20)
    tracker.sell(0.5)
    assert abs(tracker.total_qty() - 1.5) < 1e-8
    # İlk 0.5 birim 10 dolardan satıldı, kalanlar 0.5*10 + 1*20 maliyetinde
    expected_avg = (0.5 * 10 + 1 * 20) / 1.5
    assert abs(tracker.average_price() - expected_avg) < 1e-8


def test_utc_format():
    utc_str = get_current_utc_iso()
    assert utc_str.endswith("Z")
    assert len(utc_str) == 20


def test_local_conversion():
    utc_time = "2023-01-01T12:00:00Z"
    local = convert_utc_to_local(utc_time)
    assert local.startswith("2023-01-01T12:00:00")
    assert len(local) >= 21


def test_timezone_conversion():
    utc_time = "2023-01-01T12:00:00Z"
    ist = convert_utc_to_timezone(utc_time, "Europe/Istanbul")
    assert ist.endswith("+0300")
    assert ist.startswith("2023-01-01T15:00:00")


def test_extract_step_size():
    info = {"filters": [{"filterType": "LOT_SIZE", "stepSize": "0.001"}]}
    assert abs(extract_step_size(info) - 0.001) < 1e-8
    info = {"filters": [{"stepSize": "0.5"}]}
    assert abs(extract_step_size(info) - 0.5) < 1e-8
    info = {"filters": []}
    assert extract_step_size(info) == 1.0


def test_extract_min_qty():
    info = {"filters": [{"filterType": "LOT_SIZE", "minQty": "0.01"}]}
    assert abs(extract_min_qty(info) - 0.01) < 1e-8
    info = {"filters": [{"minQty": "0.5"}]}
    assert abs(extract_min_qty(info) - 0.5) < 1e-8
    info = {"filters": []}
    assert extract_min_qty(info) == 0.0


def test_extract_max_qty():
    info = {"filters": [{"filterType": "LOT_SIZE", "maxQty": "100"}]}
    assert abs(extract_max_qty(info) - 100) < 1e-8
    info = {"filters": [{"maxQty": "50"}]}
    assert abs(extract_max_qty(info) - 50) < 1e-8
    info = {"filters": []}
    assert extract_max_qty(info) == float("inf")


def test_extract_min_notional():
    info = {"filters": [{"filterType": "MIN_NOTIONAL", "minNotional": "10"}]}
    assert abs(extract_min_notional(info) - 10) < 1e-8
    info = {"filters": [{"minNotional": "1"}]}
    assert abs(extract_min_notional(info) - 1) < 1e-8
    info = {"filters": []}
    assert extract_min_notional(info) == 0.0


def test_seconds_until_midnight():
    dt = datetime(2023, 1, 1, 23, 59, 30, tzinfo=timezone.utc)
    sec = seconds_until_next_midnight(dt)
    assert abs(sec - 30) < 1e-6


def test_seconds_until_next_six_hour():
    dt = datetime(2023, 1, 1, 10, 30, tzinfo=timezone.utc)
    sec = seconds_until_next_six_hour(dt)
    assert abs(sec - 5400) < 1e-6
    dt = datetime(2023, 1, 1, 23, 59, 30, tzinfo=timezone.utc)
    sec = seconds_until_next_six_hour(dt)
    assert abs(sec - 30) < 1e-6


def test_setup_telegram_menu(monkeypatch):
    import bot.utils as utils
    calls = {}

    def fake_post(url, json=None, timeout=10):
        calls['url'] = url
        calls['data'] = json

    monkeypatch.setattr(utils, 'requests', type('R', (), {'post': fake_post}))
    utils.setup_telegram_menu('TOKEN')
    assert 'setMyCommands' in calls['url']
    assert calls['data']['commands']


def test_env_timezone_conversion(monkeypatch):
    monkeypatch.setenv('LOCAL_TIMEZONE', 'Europe/Istanbul')
    out = convert_utc_to_env_timezone('2023-01-01T12:00:00Z')
    assert out.startswith('2023-01-01T15:00:00')
    assert out.endswith('+0300')


def test_log_uses_env_timezone(monkeypatch):
    import bot.utils as utils
    monkeypatch.setenv('LOCAL_TIMEZONE', 'Europe/Istanbul')
    logs = []
    monkeypatch.setattr(utils, 'print', lambda m: logs.append(m))
    monkeypatch.setattr(utils, 'get_current_utc_iso', lambda: '2023-01-01T12:00:00Z')
    utils.log('Test')
    assert logs
    assert logs[0].startswith('[2023-01-01T15:00:00+0300] Test')


def test_load_env_warning(monkeypatch):
    import bot.utils as utils
    monkeypatch.setattr(utils.os.path, 'exists', lambda p: False)
    records = []
    monkeypatch.setattr(utils, 'load_dotenv', lambda *a, **k: records.append('loaded'))
    monkeypatch.setattr(utils, 'print', lambda m: records.append(m))
    utils.load_env()
    assert any('Uyarı' in r for r in records)
    assert 'loaded' in records
