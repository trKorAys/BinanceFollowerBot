import os
import sys

# Proje kök dizinini test path'ine ekle
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import importlib
import asyncio
import pytest
from datetime import datetime, timezone, timedelta
import bot.buy_bot as buy_bot
from bot.buy_bot import calculate_sma, meets_buy_conditions

buy_bot.SMA_PERIOD = 7


def test_meets_buy_conditions_bool():
    opens = [1] * 60
    highs = [1.1] * 60
    lows = [0.9] * 60
    closes = [1 + i * 0.01 for i in range(60)]
    volumes = [100] * 60
    assert isinstance(
        meets_buy_conditions(opens[:-1], highs[:-1], lows[:-1], closes[:-1], volumes[:-1]),
        bool,
    )


def test_testnet_env_overrides(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "X")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "Y")
    module = importlib.reload(buy_bot)
    module.SMA_PERIOD = 7
    assert module.TESTNET is True
    assert module.API_KEY == "X"
    assert module.API_SECRET == "Y"


def test_top_symbols_env_override(monkeypatch):
    monkeypatch.setenv("TOP_SYMBOLS_COUNT", "25")
    module = importlib.reload(buy_bot)
    assert module.TOP_SYMBOLS_COUNT == 25


def test_select_symbols_fallback(monkeypatch):
    class DummyClient:
        async def get_exchange_info(self):
            return {
                "symbols": [
                    {
                        "symbol": f"S{i}USDT",
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                    }
                    for i in range(80)
                ]
            }

        async def get_ticker(self, symbol):
            return {"volume": "100", "takerBuyBaseAssetVolume": "50"}

        async def get_klines(self, symbol, interval="15m", limit=60):
            data = [
                [0, "1", "1", "1", "1", "1", 0, 0, 0, 0, "0", "0"]
            ] * 60
            return data

    dummy = DummyClient()
    bot_instance = buy_bot.BuyBot(dummy)
    monkeypatch.setattr(buy_bot, "meets_rsi_keltner", lambda *a: True)
    bot_instance.top_symbols = [f"S{i}USDT" for i in range(80)]
    monkeypatch.setattr(buy_bot.BuyBot, "update_top_symbols", lambda self: None)
    result = asyncio.run(bot_instance.select_rsi_keltner())
    assert result == ("S0USDT", 1.0)


def test_ensure_testnet_balance(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.setenv("TESTNET_INITIAL_USDT", "5")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "X")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "Y")
    module = importlib.reload(buy_bot)

    class DummyClient:
        API_TESTNET_URL = "https://testnet.binance.vision/api"

        def __init__(self):
            self.added = 0.0
            self.balance = 0.0

        async def get_asset_balance(self, asset="USDT"):
            return {"free": str(self.balance)}

        async def _request(self, method, url, signed=False, params=None, **_):
            assert method == "post"
            assert "testnet-funds" in url
            self.added += float(params.get("amount", 0))
            return {}

    dummy = DummyClient()
    bot_instance = module.BuyBot(dummy)
    asyncio.run(bot_instance.ensure_testnet_balance())
    assert dummy.added == 5.0


def test_select_symbols_ignores_open_candle(monkeypatch):
    recorded = {}

    def fake_meets(highs, lows, closes):
        recorded["last"] = closes[-1]
        return True

    monkeypatch.setattr(buy_bot, "meets_rsi_keltner", fake_meets)

    class DummyClient:
        async def get_exchange_info(self):
            return {
                "symbols": [
                    {"symbol": "ABCUSDT", "quoteAsset": "USDT", "status": "TRADING"}
                ]
            }

        async def get_ticker(self, symbol):
            return {"volume": "100", "takerBuyBaseAssetVolume": "60"}

        async def get_klines(self, symbol, interval="15m", limit=60):
            data = [
                [0, "1", "1", "1", "1", "1", 0, 0, 0, 0, "0", "0"]
            ] * 59
            data.append([0, "1", "1", "1", "99", "1", 0, 0, 0, 0, "0", "0"])
            return data

    dummy = DummyClient()
    bot_instance = buy_bot.BuyBot(dummy)
    bot_instance.top_symbols = ["ABCUSDT"]
    monkeypatch.setattr(buy_bot.BuyBot, "update_top_symbols", lambda self: None)
    result = asyncio.run(bot_instance.select_rsi_keltner())
    assert recorded["last"] != 99
    assert result == ("ABCUSDT", 1.0)


def test_meets_rsi_keltner_bool():
    highs = [1.1] * 60
    lows = [0.9] * 60
    closes = [1 - i * 0.01 for i in range(60)]
    assert isinstance(buy_bot.meets_rsi_keltner(highs, lows, closes), bool)


def test_is_btc_above_sma99_bool():
    class DummyClient:
        async def get_klines(self, symbol, interval="1d", limit=99):
            if interval == "1d":
                return [[0, 0, 0, 0, "2", 0, 0, 0, 0, 0, "0", "0"]] * 99
            return [
                [0, 0, 0, 0, "1", 0, 0, 0, 0, 0, "0", "0"],
                [0, 0, 0, 0, "3", 0, 0, 0, 0, 0, "0", "0"],
            ]

    bot = buy_bot.BuyBot(DummyClient())
    assert isinstance(asyncio.run(bot.is_btc_above_sma99()), bool)


def test_notify_buy_utf8(monkeypatch):
    messages = []
    monkeypatch.setattr(buy_bot, "send_telegram", lambda *a, **k: messages.append(a[0]))
    buy_bot.notify_buy("ABCUSDT", 1.0, 10.0)
    assert messages
    messages[0].encode("utf-8")


def test_check_api_ip_change(monkeypatch):
    ip_iter = iter(["1.1.1.1", "2.2.2.2"])
    monkeypatch.setattr(buy_bot, "get_public_ip", lambda: next(ip_iter))

    class DummyClient:
        async def ping(self):
            return {}

    messages = []
    monkeypatch.setattr(buy_bot, "send_telegram", lambda *a, **k: messages.append(a[0]))
    monkeypatch.setenv("TELEGRAM_LANG", "tr")

    bot = buy_bot.BuyBot(DummyClient())
    bot.current_ip = "1.1.1.1"
    asyncio.run(bot.check_api())  # first call, same IP
    asyncio.run(bot.check_api())  # second call, new IP
    assert any("Yeni IP" in m for m in messages)


def test_check_api_ip_change_en(monkeypatch):
    ip_iter = iter(["3.3.3.3", "4.4.4.4"])
    monkeypatch.setattr(buy_bot, "get_public_ip", lambda: next(ip_iter))

    class DummyClient:
        async def ping(self):
            return {}

    messages = []
    monkeypatch.setattr(buy_bot, "send_telegram", lambda *a, **k: messages.append(a[0]))
    monkeypatch.setenv("TELEGRAM_LANG", "en")
    bot = buy_bot.BuyBot(DummyClient())
    bot.current_ip = "3.3.3.3"
    asyncio.run(bot.check_api())
    asyncio.run(bot.check_api())
    assert any("New IP" in m for m in messages)


def test_sync_time(monkeypatch):
    class DummyClient:
        def __init__(self):
            self.timestamp_offset = 0

        async def get_server_time(self):
            return {"serverTime": 10_000}

    bot = buy_bot.BuyBot(DummyClient())
    monkeypatch.setattr(buy_bot.time, "time", lambda: 8)
    asyncio.run(bot.sync_time())
    assert bot.client.timestamp_offset == 10_000 - 8_000


def test_run_skips_when_usdt_low(monkeypatch):
    class DummyClient:
        async def get_asset_balance(self, asset="USDT"):
            return {"free": "4"}

    async def fake_sync(self):
        pass

    called = {"select": False}

    async def fake_select(self):
        called["select"] = True
        return None

    bot = buy_bot.BuyBot(DummyClient())
    monkeypatch.setattr(buy_bot.BuyBot, "sync_time", fake_sync)
    monkeypatch.setattr(buy_bot.BuyBot, "select_rsi_keltner", fake_select)
    asyncio.run(bot.run())
    assert called["select"] is False


def test_fetch_symbols_skips_excluded(monkeypatch):
    monkeypatch.setenv("EXCLUDED_BASES", "BUSD")
    module = importlib.reload(buy_bot)

    class DummyClient:
        async def get_exchange_info(self):
            return {
                "symbols": [
                    {
                        "symbol": "BUSDUSDT",
                        "baseAsset": "BUSD",
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                    },
                    {
                        "symbol": "BTCUSDT",
                        "baseAsset": "BTC",
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                    },
                ]
            }

    dummy = DummyClient()
    bot_instance = module.BuyBot(dummy)
    bot_instance.top_symbols = ["BTCUSDT"]
    symbols = asyncio.run(bot_instance.fetch_symbols())
    assert symbols == ["BTCUSDT"]


def test_update_top_symbols(monkeypatch):
    class DummyClient:
        async def get_exchange_info(self):
            return {
                "symbols": [
                    {"symbol": "AAAUSDT", "baseAsset": "AAA", "quoteAsset": "USDT", "status": "TRADING"},
                    {"symbol": "BBBUSDT", "baseAsset": "BBB", "quoteAsset": "USDT", "status": "TRADING"},
                ]
            }

        async def get_ticker(self):
            return [
                {"symbol": "AAAUSDT", "quoteVolume": "200"},
                {"symbol": "BBBUSDT", "quoteVolume": "100"},
            ]

    bot = buy_bot.BuyBot(DummyClient())
    asyncio.run(bot.update_top_symbols())
    assert bot.top_symbols == ["AAAUSDT", "BBBUSDT"]


def test_skip_buy_if_recent(tmp_path, monkeypatch):
    class DummyClient:
        async def get_asset_balance(self, asset="USDT"):
            return {"free": "10"}

    monkeypatch.setenv("BUY_DB_PATH", str(tmp_path / "b.db"))
    bot = buy_bot.BuyBot(DummyClient())

    called = {"buy": 0}

    async def fake_execute(symbol, amount, **_):
        called["buy"] += 1

    monkeypatch.setattr(bot, "execute_buy", fake_execute)

    now = datetime.now(timezone.utc)
    bot.last_buy_times["ABCUSDT"] = now - timedelta(hours=1)
    bot.loss_check_enabled = False
    bot.top_symbols = ["ABCUSDT"]
    asyncio.run(bot._execute_cycle(("ABCUSDT", 1.0), 10))
    assert called["buy"] == 0
    assert "ABCUSDT" not in bot.top_symbols

    bot.last_buy_times["ABCUSDT"] = now - timedelta(hours=3)
    asyncio.run(bot._execute_cycle(("ABCUSDT", 1.0), 10))
    assert called["buy"] == 1

    bot.loss_check_enabled = True
    bot.last_buy_times["ABCUSDT"] = now - timedelta(hours=1)
    asyncio.run(bot._execute_cycle(("ABCUSDT", 1.0), 10))
    # loss strategy ignores the 2h rule
    assert called["buy"] == 2


def test_skip_buy_if_recent_sell(tmp_path, monkeypatch):
    class DummyClient:
        async def get_asset_balance(self, asset="USDT"):
            return {"free": "10"}

    monkeypatch.setenv("BUY_DB_PATH", str(tmp_path / "s.db"))
    bot = buy_bot.BuyBot(DummyClient())

    called = {"buy": 0}

    async def fake_execute(symbol, amount, **_):
        called["buy"] += 1

    monkeypatch.setattr(bot, "execute_buy", fake_execute)

    now = datetime.now(timezone.utc)
    bot.last_sell_times["ABCUSDT"] = now - timedelta(hours=1)
    bot.loss_check_enabled = False
    asyncio.run(bot._execute_cycle(("ABCUSDT", 1.0), 10))
    assert called["buy"] == 0

    bot.last_sell_times["ABCUSDT"] = now - timedelta(hours=3)
    asyncio.run(bot._execute_cycle(("ABCUSDT", 1.0), 10))
    assert called["buy"] == 1


def test_recent_buy_persistence(tmp_path, monkeypatch):
    class DummyClient:
        async def get_asset_balance(self, asset="USDT"):
            return {"free": "10"}

    path = tmp_path / "b.db"
    monkeypatch.setenv("BUY_DB_PATH", str(path))
    bot1 = buy_bot.BuyBot(DummyClient())
    now = datetime.now(timezone.utc)
    bot1._save_recent_buy("AAAUSDT", now)
    bot1.last_buy_times["AAAUSDT"] = now
    bot2 = buy_bot.BuyBot(DummyClient())
    assert "AAAUSDT" in bot2.last_buy_times


def test_recent_sell_persistence(tmp_path, monkeypatch):
    class DummyClient:
        async def get_asset_balance(self, asset="USDT"):
            return {"free": "10"}

    path = tmp_path / "s.db"
    monkeypatch.setenv("BUY_DB_PATH", str(path))
    bot1 = buy_bot.BuyBot(DummyClient())
    now = datetime.now(timezone.utc)
    bot1._save_recent_sell("AAAUSDT", now)
    bot1.last_sell_times["AAAUSDT"] = now
    bot2 = buy_bot.BuyBot(DummyClient())
    assert "AAAUSDT" in bot2.last_sell_times


def test_cleanup_recent_buys(tmp_path, monkeypatch):
    class DummyClient:
        async def get_asset_balance(self, asset="USDT"):
            return {"free": "10"}

    path = tmp_path / "c.db"
    monkeypatch.setenv("BUY_DB_PATH", str(path))
    bot = buy_bot.BuyBot(DummyClient())
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=3)
    bot._save_recent_buy("OLDUSDT", old)
    bot.last_buy_times["OLDUSDT"] = old
    bot._save_recent_buy("NEWUSDT", now)
    bot.last_buy_times["NEWUSDT"] = now
    bot._cleanup_recent_buys()
    assert "OLDUSDT" not in bot.last_buy_times
    assert "NEWUSDT" in bot.last_buy_times
    rows = bot.db.execute("SELECT symbol FROM recent_buys").fetchall()
    assert ("NEWUSDT",) in rows
    assert ("OLDUSDT",) not in rows


def test_cleanup_recent_sells(tmp_path, monkeypatch):
    class DummyClient:
        async def get_asset_balance(self, asset="USDT"):
            return {"free": "10"}

    path = tmp_path / "d.db"
    monkeypatch.setenv("BUY_DB_PATH", str(path))
    bot = buy_bot.BuyBot(DummyClient())
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=3)
    bot._save_recent_sell("OLDUSDT", old)
    bot.last_sell_times["OLDUSDT"] = old
    bot._save_recent_sell("NEWUSDT", now)
    bot.last_sell_times["NEWUSDT"] = now
    bot._cleanup_recent_sells()
    assert "OLDUSDT" not in bot.last_sell_times
    assert "NEWUSDT" in bot.last_sell_times
    rows = bot.db.execute("SELECT symbol FROM recent_sells").fetchall()
    assert ("NEWUSDT",) in rows
    assert ("OLDUSDT",) not in rows


def test_execute_buy_checks_balance():
    class DummyClient:
        def __init__(self):
            self.orders = []

        async def get_symbol_info(self, symbol):
            return {
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                ]
            }

        async def get_symbol_ticker(self, symbol):
            return {"price": "10"}

        async def get_asset_balance(self, asset="USDT"):
            return {"free": "9"}

        async def create_order(self, **kwargs):
            self.orders.append(kwargs.get("quoteOrderQty"))

    client = DummyClient()
    bot = buy_bot.BuyBot(client)
    result = asyncio.run(bot.execute_buy("ABCUSDT", 10))
    assert result is False
    assert not client.orders


def test_execute_buy_respects_follow_notional(monkeypatch):
    class DummyClient:
        def __init__(self):
            self.orders = []

        async def get_symbol_info(self, symbol):
            return {
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "minNotional": "1"},
                ]
            }

        async def get_symbol_ticker(self, symbol):
            return {"price": "1"}

        async def get_asset_balance(self, asset="USDT"):
            return {"free": "100"}

        async def create_order(self, **kwargs):
            self.orders.append(kwargs.get("quoteOrderQty"))

    client = DummyClient()
    monkeypatch.setattr(buy_bot, "MIN_FOLLOW_NOTIONAL", 10)
    bot = buy_bot.BuyBot(client)
    result = asyncio.run(bot.execute_buy("AAAUSDT", 5, check_loss=False))
    assert result is False
    assert not client.orders


def test_execute_buy_allows_under_follow_notional_for_loss(monkeypatch):
    class DummyClient:
        def __init__(self):
            self.orders = []

        async def get_symbol_info(self, symbol):
            return {
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "minNotional": "1"},
                ]
            }

        async def get_symbol_ticker(self, symbol):
            return {"price": "1"}

        async def get_asset_balance(self, asset="USDT"):
            return {"free": "100"}

        async def create_order(self, **kwargs):
            self.orders.append(kwargs.get("quoteOrderQty"))

    client = DummyClient()
    monkeypatch.setattr(buy_bot, "MIN_FOLLOW_NOTIONAL", 10)
    bot = buy_bot.BuyBot(client)
    result = asyncio.run(bot.execute_buy("AAAUSDT", 5, check_loss=True))
    assert result is True
    assert client.orders


def test_execute_buy_uses_quote_order_qty():
    class DummyClient:
        def __init__(self):
            self.orders = []

        async def get_symbol_info(self, symbol):
            return {
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                ]
            }

        async def get_symbol_ticker(self, symbol):
            return {"price": "3.333333"}

        async def get_asset_balance(self, asset="USDT"):
            return {"free": "20"}

        async def create_order(self, **kwargs):
            self.orders.append(kwargs.get("quoteOrderQty"))

    client = DummyClient()
    bot = buy_bot.BuyBot(client)
    result = asyncio.run(bot.execute_buy("AAAUSDT", 10))
    assert result is True
    assert abs(client.orders[0] - 10) < 1e-8


def test_execute_buy_respects_precision():
    class DummyClient:
        def __init__(self):
            self.amount = None

        async def get_symbol_info(self, symbol):
            return {
                "quoteAssetPrecision": 2,
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
                    {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                ],
            }

        async def get_symbol_ticker(self, symbol):
            return {"price": "1"}

        async def get_asset_balance(self, asset="USDT"):
            return {"free": "100"}

        async def create_order(self, **kwargs):
            self.amount = kwargs.get("quoteOrderQty")

    client = DummyClient()
    bot = buy_bot.BuyBot(client)
    result = asyncio.run(bot.execute_buy("PRECISUSDT", 10.123456))
    assert result is True
    assert abs(client.amount - 10.12) < 1e-8


def test_execute_cycle_uses_passed_balance(monkeypatch):
    class DummyClient:
        pass

    recorded = {}

    async def fake_execute(self, symbol, amount, **_):
        recorded["amount"] = amount

    bot = buy_bot.BuyBot(DummyClient())
    monkeypatch.setattr(buy_bot.BuyBot, "execute_buy", fake_execute)
    asyncio.run(bot._execute_cycle(("XYZUSDT", 1.0), 12))
    assert abs(recorded["amount"] - 12) < 1e-8


def test_run_logs_balance(monkeypatch):
    class DummyClient:
        async def get_asset_balance(self, asset="USDT"):
            return {"free": "6"}

    async def fake_sync(self):
        pass

    async def fake_select(self):
        return None

    logs = []

    async def fake_cycle(self, candidate, usdt):
        logs.append(("cycle", usdt))

    monkeypatch.setattr(buy_bot, "log", lambda msg: logs.append(msg))
    monkeypatch.setattr(buy_bot.BuyBot, "sync_time", fake_sync)
    monkeypatch.setattr(buy_bot.BuyBot, "select_rsi_keltner", fake_select)
    monkeypatch.setattr(buy_bot.BuyBot, "_execute_cycle", fake_cycle)

    bot = buy_bot.BuyBot(DummyClient())
    asyncio.run(bot.run())
    assert any("Serbest USDT bakiyesi" in m for m in logs)
    expected = 6.0 * buy_bot.USDT_USAGE_RATIO
    assert any(
        isinstance(item, tuple) and item[0] == "cycle" and abs(item[1] - expected) < 1e-8
        for item in logs
    )


def test_select_loser(monkeypatch):
    class DummyClient:
        def __init__(self):
            self.trades = {
                "AUSDT": [{"id": 0, "qty": "1", "price": "10", "isBuyer": True}],
                "BUSDT": [{"id": 0, "qty": "1", "price": "10", "isBuyer": True}],
            }

        async def get_account(self):
            return {
                "balances": [
                    {"asset": "A", "free": "1", "locked": "0"},
                    {"asset": "B", "free": "1", "locked": "0"},
                    {"asset": "USDT", "free": "5", "locked": "0"},
                ]
            }

        async def get_symbol_ticker(self, symbol):
            return {"price": "9" if symbol == "AUSDT" else "9.5"}

        async def get_symbol_info(self, symbol):
            return {}

        async def get_my_trades(self, symbol, limit=1000, fromId=None):
            if fromId in (0, None):
                return self.trades[symbol]
            return []

    bot = buy_bot.BuyBot(DummyClient())
    result = asyncio.run(bot.select_loser())
    assert result[0] == "AUSDT"


def test_select_loser_none(monkeypatch):
    class DummyClient:
        async def get_account(self):
            return {
                "balances": [
                    {"asset": "A", "free": "1", "locked": "0"},
                    {"asset": "USDT", "free": "5", "locked": "0"},
                ]
            }

        async def get_symbol_ticker(self, symbol):
            return {"price": "9.6"}

        async def get_symbol_info(self, symbol):
            return {}

        async def get_my_trades(self, symbol, limit=1000, fromId=None):
            if fromId in (0, None):
                return [{"id": 0, "qty": "1", "price": "10", "isBuyer": True}]
            return []

    bot = buy_bot.BuyBot(DummyClient())
    result = asyncio.run(bot.select_loser())
    assert result[0] == "AUSDT"


def test_select_loser_min_qty(monkeypatch):
    class DummyClient:
        async def get_account(self):
            return {"balances": [{"asset": "A", "free": "0.5", "locked": "0"}]}

        async def get_symbol_ticker(self, symbol):
            return {"price": "5"}

        async def get_symbol_info(self, symbol):
            return {"filters": [{"filterType": "LOT_SIZE", "minQty": "1"}]}

        async def get_my_trades(self, symbol, limit=1000, fromId=None):
            if fromId in (0, None):
                return [{"id": 0, "qty": "0.5", "price": "10", "isBuyer": True}]
            return []

    bot = buy_bot.BuyBot(DummyClient())
    result = asyncio.run(bot.select_loser())
    assert result is None


def test_select_loser_value_skip(monkeypatch):
    class DummyClient:
        async def get_account(self):
            return {"balances": [{"asset": "A", "free": "1", "locked": "0"}]}

        async def get_symbol_ticker(self, symbol):
            return {"price": "4"}

        async def get_symbol_info(self, symbol):
            return {"filters": [{"filterType": "LOT_SIZE", "minQty": "0.1"}]}

        async def get_my_trades(self, symbol, limit=1000, fromId=None):
            if fromId in (0, None):
                return [{"id": 0, "qty": "1", "price": "10", "isBuyer": True}]
            return []

    bot = buy_bot.BuyBot(DummyClient())
    result = asyncio.run(bot.select_loser())
    assert result is None


def test_run_prefers_loser(monkeypatch):
    class DummyClient:
        async def get_asset_balance(self, asset="USDT"):
            return {"free": "6"}

    async def fake_sync(self):
        pass

    async def fake_loser(self):
        return ("LOSSUSDT", 1.0)

    async def fake_select(self):
        return ("NEWUSDT", 1.0)

    recorded = {}

    async def fake_cycle(self, candidate, usdt):
        recorded["symbol"] = candidate[0]

    bot = buy_bot.BuyBot(DummyClient())
    monkeypatch.setattr(buy_bot.BuyBot, "sync_time", fake_sync)
    monkeypatch.setattr(buy_bot.BuyBot, "select_loser", fake_loser)
    monkeypatch.setattr(buy_bot.BuyBot, "select_rsi_keltner", fake_select)
    monkeypatch.setattr(buy_bot.BuyBot, "_execute_cycle", fake_cycle)
    asyncio.run(bot.run())
    assert recorded.get("symbol") == "LOSSUSDT"


def test_run_logs_strategies(monkeypatch):
    class DummyClient:
        async def get_asset_balance(self, asset="USDT"):
            return {"free": "10"}

    async def fake_sync(self):
        pass

    async def fake_none(self):
        return None

    logs = []

    monkeypatch.setattr(buy_bot, "log", lambda m: logs.append(m))
    bot = buy_bot.BuyBot(DummyClient())
    monkeypatch.setattr(buy_bot.BuyBot, "sync_time", fake_sync)
    monkeypatch.setattr(buy_bot.BuyBot, "select_loser", fake_none)
    monkeypatch.setattr(buy_bot.BuyBot, "select_rsi_keltner", fake_none)
    async def fake_btc(self):
        return True

    monkeypatch.setattr(buy_bot.BuyBot, "is_btc_above_sma99", fake_btc)
    asyncio.run(bot.run())
    assert any("Zarar stratejisi" in m for m in logs)
    assert any("RSI-Keltner stratejisi" in m for m in logs)


def test_run_skips_when_btc_below(monkeypatch):
    class DummyClient:
        async def get_asset_balance(self, asset="USDT"):
            return {"free": "10"}

    async def fake_sync(self):
        pass

    async def fake_none(self):
        return None

    called = {"select": False}

    async def fake_select(self):
        called["select"] = True
        return None

    async def fake_btc(self):
        return False

    bot = buy_bot.BuyBot(DummyClient())
    monkeypatch.setattr(buy_bot.BuyBot, "sync_time", fake_sync)
    monkeypatch.setattr(buy_bot.BuyBot, "select_loser", fake_none)
    monkeypatch.setattr(buy_bot.BuyBot, "select_rsi_keltner", fake_select)
    monkeypatch.setattr(buy_bot.BuyBot, "is_btc_above_sma99", fake_btc)
    asyncio.run(bot.run())
    assert called["select"] is False


def test_send_telegram_prefix(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    module = importlib.reload(buy_bot)
    messages = []
    module.TELEGRAM_TOKEN = None
    module.CHAT_ID = None
    monkeypatch.setattr(module, "log", lambda m: messages.append(m))
    module.send_telegram("merhaba")
    assert messages and messages[0].startswith("TESTNET")


def test_send_telegram_disabled(monkeypatch):
    monkeypatch.setenv("TELEGRAM_ENABLED", "false")
    module = importlib.reload(buy_bot)
    calls = []

    def fake_post(url, json=None, timeout=10):
        calls.append("sent")

    monkeypatch.setattr(module.requests, "post", fake_post)
    module.TELEGRAM_TOKEN = "T"
    module.CHAT_ID = "C"
    monkeypatch.setattr(module, "log", lambda m: calls.append(m))
    module.send_telegram("deneme")
    assert "sent" not in calls


def test_start_message_once(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "false")
    module = importlib.reload(buy_bot)

    class DummyClient:
        async def ping(self):
            pass

        async def get_server_time(self):
            return {"serverTime": 0}

    bot = module.BuyBot(DummyClient())
    messages = []
    monkeypatch.setattr(module, "send_start_message", lambda *a: messages.append(a))

    async def fake_run(self):
        raise RuntimeError

    async def fake_check(self):
        pass

    async def fake_sync(self):
        pass

    async def fake_select_loser(self):
        return None

    async def fake_select_symbol(self):
        return None

    async def fake_monitor(self):
        pass

    async def fake_update(self):
        pass

    async def fake_loop(self):
        pass

    async def fake_sleep(_t):
        return None

    monkeypatch.setattr(module.BuyBot, "run", fake_run)
    monkeypatch.setattr(module.BuyBot, "check_api", fake_check)
    monkeypatch.setattr(module.BuyBot, "sync_time", fake_sync)
    monkeypatch.setattr(module.BuyBot, "select_loser", fake_select_loser)
    monkeypatch.setattr(module.BuyBot, "select_rsi_keltner", fake_select_symbol)
    monkeypatch.setattr(module.BuyBot, "monitor_api", fake_monitor)
    monkeypatch.setattr(module.BuyBot, "update_top_symbols", fake_update)
    monkeypatch.setattr(module.BuyBot, "symbols_update_loop", fake_loop)
    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)

    with pytest.raises(RuntimeError):
        asyncio.run(bot.start())
    assert len(messages) == 1
    with pytest.raises(RuntimeError):
        asyncio.run(bot.start())
    assert len(messages) == 1


def test_send_telegram_force(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    module = importlib.reload(buy_bot)
    calls = []

    def fake_post(url, json=None, timeout=10):
        calls.append(url)

    monkeypatch.setattr(module.requests, "post", fake_post)
    module.TELEGRAM_TOKEN = "T"
    module.CHAT_ID = "C"
    module.send_telegram("msg")
    assert not calls
    module.send_telegram("msg", force=True)
    assert calls
