import asyncio
import time
import importlib
import pytest
import sqlite3
import bot.sell_bot as bot_module
from bot.sell_bot import SellBot, FifoTracker, Position

class DummyClient:
    async def get_account(self):
        return {
            "balances": [
                {"asset": "BTC", "free": "0.5", "locked": "0"}
            ]
        }

    async def get_symbol_info(self, symbol):
        return {
            "filters": [
                {"filterType": "LOT_SIZE", "minQty": "0.0001", "stepSize": "0.0001"},
                {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
            ]
        }

    async def get_my_trades(self, symbol, limit=1000, fromId=None):
        return [{
            "qty": "0.5",
            "price": "10000",
            "isBuyer": True,
            "id": 1,
            "commission": "0.01",
            "commissionAsset": "BTC",
        }]


def test_testnet_env_overrides(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "AA")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "BB")
    module = importlib.reload(bot_module)
    assert module.TESTNET is True
    assert module.API_KEY == "AA"
    assert module.API_SECRET == "BB"


def test_check_interval_forced_in_testnet(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    monkeypatch.setenv("CHECK_INTERVAL", "5")
    module = importlib.reload(bot_module)
    assert module.CHECK_INTERVAL == 60

def test_load_balances():
    watcher = SellBot(DummyClient())
    asyncio.run(watcher.load_balances())
    assert "BTCUSDT" in watcher.positions
    pos = watcher.positions["BTCUSDT"].tracker
    assert abs(pos.total_qty() - 0.5) < 1e-8
    assert abs(watcher.positions["BTCUSDT"].min_notional - 5) < 1e-8


def test_load_balances_single_symbol_in_testnet(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    module = importlib.reload(bot_module)

    class MultiClient(DummyClient):
        async def get_account(self):
            return {
                "balances": [
                    {"asset": "BTC", "free": "0.5", "locked": "0"},
                    {"asset": "ETH", "free": "0.3", "locked": "0"},
                ]
            }

    watcher = module.SellBot(MultiClient())
    asyncio.run(watcher.load_balances())
    assert len(watcher.positions) == 2


def test_zero_avg_triggers_sell(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    module = importlib.reload(bot_module)

    class ZeroClient(DummyClient):
        async def get_account(self):
            return {"balances": [{"asset": "BTC", "free": "1", "locked": "0"}]}

        async def get_symbol_info(self, symbol):
            return {
                "filters": [
                    {"filterType": "LOT_SIZE", "minQty": "0.0001", "stepSize": "0.0001"},
                    {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                ]
            }

        async def get_my_trades(self, symbol, limit=1000, fromId=None):
            return [{"qty": "1", "price": "0", "isBuyer": True, "id": 1}]

        async def get_symbol_ticker(self, symbol):
            return {"price": "10"}

    sells = []

    async def fake_sell(self, sym, qty, notify=True):
        sells.append((sym, qty, notify))

    monkeypatch.setattr(module.SellBot, "execute_sell", fake_sell)
    watcher = module.SellBot(ZeroClient())
    asyncio.run(watcher.load_balances())
    assert sells and sells[0][0] == "BTCUSDT" and sells[0][2] is False


def test_notional_below_limit_not_tracked():
    class LowClient(DummyClient):
        async def get_account(self):
            return {"balances": [{"asset": "X", "free": "4", "locked": "0"}]}

        async def get_symbol_info(self, symbol):
            return {
                "filters": [
                    {"filterType": "LOT_SIZE", "minQty": "1", "stepSize": "1"},
                    {"filterType": "MIN_NOTIONAL", "minNotional": "0.1"},
                ]
            }

        async def get_my_trades(self, symbol, limit=1000, fromId=None):
            return []

        async def get_symbol_ticker(self, symbol):
            return {"price": "0.7"}

    watcher = SellBot(LowClient())
    asyncio.run(watcher.load_balances())
    assert "XUSDT" not in watcher.positions


def test_min_qty_below_limit_not_tracked():
    class QtyClient(DummyClient):
        async def get_account(self):
            return {"balances": [{"asset": "Y", "free": "0.4", "locked": "0"}]}

        async def get_symbol_info(self, symbol):
            return {
                "filters": [
                    {"filterType": "LOT_SIZE", "minQty": "1", "stepSize": "1"},
                    {"filterType": "MIN_NOTIONAL", "minNotional": "0.01"},
                ]
            }

        async def get_my_trades(self, symbol, limit=1000, fromId=None):
            return []

        async def get_symbol_ticker(self, symbol):
            return {"price": "10"}

    watcher = SellBot(QtyClient())
    asyncio.run(watcher.load_balances())
    assert "YUSDT" not in watcher.positions


def test_fetch_all_trades_start_from_zero():
    class Dummy:
        def __init__(self):
            self.calls = []

        async def get_my_trades(self, symbol, limit=1000, fromId=None):
            self.calls.append(fromId)
            if fromId == 0:
                return [{"id": 0, "qty": "1", "price": "1", "isBuyer": True}]
            return []

    dummy = Dummy()
    watcher = SellBot(dummy)
    trades = asyncio.run(watcher.fetch_all_trades("BTCUSDT"))
    assert dummy.calls[0] == 0
    assert len(trades) == 1


def test_should_sell_considers_volatility(monkeypatch):
    class VolClient(DummyClient):
        async def get_recent_trades(self, symbol, limit=60):
            return [
                {"qty": "2", "isBuyerMaker": True},
                {"qty": "1", "isBuyerMaker": False},
            ]

        async def get_symbol_ticker(self, symbol):
            return {"price": "105"}

        async def get_klines(self, symbol, interval, limit=2):
            return [
                [0, "100", 0, 0, "102", 0, 0, 0, 0, 0, "0", "0"],
                [0, "0", 0, 0, "0", 0, 0, 0, 0, 0, "0", "0"],
            ]

    import bot.sell_bot as bot_module
    bot_module.FEE_BUY = 0.0
    bot_module.FEE_SELL = 0.0
    bot_module.MIN_PROFIT = 0.0
    bot_module.TARGET_STEPS = 2
    watcher = bot_module.SellBot(VolClient())
    watcher.positions["BTCUSDT"] = bot_module.Position(bot_module.FifoTracker(), 0.0, 0.0)
    watcher.positions["BTCUSDT"].peak = 125.0
    async def fake_vol(*_args, **_kwargs):
        return 0.2

    monkeypatch.setattr(bot_module.SellBot, "get_volatility", fake_vol)
    watcher.btc_above_sma7 = True
    decision = asyncio.run(watcher.should_sell("BTCUSDT", 125.0, 100.0))
    assert decision is True


def test_should_sell_static_when_btc_below_sma(monkeypatch):
    class VolClient(DummyClient):
        async def get_recent_trades(self, symbol, limit=60):
            return [
                {"qty": "2", "isBuyerMaker": True},
                {"qty": "1", "isBuyerMaker": False},
            ]

        async def get_symbol_ticker(self, symbol):
            return {"price": "105"}

        async def get_klines(self, symbol, interval, limit=2):
            return [
                [0, "100", 0, 0, "110", 0, 0, 0, 0, 0, "0", "0"],
                [0, "0", 0, 0, "0", 0, 0, 0, 0, 0, "0", "0"],
            ]

    import bot.sell_bot as bot_module
    bot_module.FEE_BUY = 0.0
    bot_module.FEE_SELL = 0.0
    bot_module.MIN_PROFIT = 0.0
    bot_module.TARGET_STEPS = 3
    watcher = bot_module.SellBot(VolClient())
    watcher.positions["BTCUSDT"] = bot_module.Position(bot_module.FifoTracker(), 0.0, 0.0)
    watcher.positions["BTCUSDT"].peak = 105.0
    async def fake_vol(*_args, **_kwargs):
        return 0.5

    monkeypatch.setattr(bot_module.SellBot, "get_volatility", fake_vol)
    watcher.btc_above_sma7 = False
    decision = asyncio.run(watcher.should_sell("BTCUSDT", 99.0, 100.0))
    assert decision is False


def test_should_sell_uses_open_price_when_higher(monkeypatch):
    class OpenClient(DummyClient):
        async def get_recent_trades(self, symbol, limit=60):
            return [
                {"qty": "2", "isBuyerMaker": True},
                {"qty": "1", "isBuyerMaker": False},
            ]

        async def get_symbol_ticker(self, symbol):
            return {"price": "112"}

        async def get_klines(self, symbol, interval, limit=2):
            return [
                [0, "110", 0, 0, "105", 0, 0, 0, 0, 0, "0", "0"],
                [0, "0", 0, 0, "0", 0, 0, 0, 0, 0, "0", "0"],
            ]

    import bot.sell_bot as bot_module
    bot_module.FEE_BUY = 0.0
    bot_module.FEE_SELL = 0.0
    bot_module.MIN_PROFIT = 0.1
    bot_module.TARGET_STEPS = 1
    watcher = bot_module.SellBot(OpenClient())
    tracker = bot_module.FifoTracker()
    tracker.add_trade(1, 100.0)
    watcher.positions["BTCUSDT"] = bot_module.Position(tracker, 0.0, 0.0)
    watcher.positions["BTCUSDT"].peak = 112.0
    async def fake_vol(*_args, **_kwargs):
        return 0.0

    monkeypatch.setattr(bot_module.SellBot, "get_volatility", fake_vol)
    watcher.btc_above_sma7 = True
    decision = asyncio.run(watcher.should_sell("BTCUSDT", 112.0, 100.0))
    assert decision is False


def test_open_price_ignored_when_btc_below_sma(monkeypatch):
    class OpenClient(DummyClient):
        async def get_recent_trades(self, symbol, limit=60):
            return [
                {"qty": "2", "isBuyerMaker": True},
                {"qty": "1", "isBuyerMaker": False},
            ]

        async def get_symbol_ticker(self, symbol):
            return {"price": "112"}

        async def get_klines(self, symbol, interval, limit=2):
            return [
                [0, "110", 0, 0, "105", 0, 0, 0, 0, 0, "0", "0"],
                [0, "0", 0, 0, "0", 0, 0, 0, 0, 0, "0", "0"],
            ]

    import bot.sell_bot as bot_module
    bot_module.FEE_BUY = 0.0
    bot_module.FEE_SELL = 0.0
    bot_module.MIN_PROFIT = 0.1
    bot_module.TARGET_STEPS = 1
    watcher = bot_module.SellBot(OpenClient())
    tracker = bot_module.FifoTracker()
    tracker.add_trade(1, 100.0)
    watcher.positions["BTCUSDT"] = bot_module.Position(tracker, 0.0, 0.0)
    watcher.positions["BTCUSDT"].peak = 112.0
    async def fake_vol(*_args, **_kwargs):
        return 0.0

    monkeypatch.setattr(bot_module.SellBot, "get_volatility", fake_vol)
    watcher.btc_above_sma7 = False
    decision = asyncio.run(watcher.should_sell("BTCUSDT", 112.0, 100.0))
    assert decision is True


def test_sell_when_above_last_target(monkeypatch):
    class HighClient(DummyClient):
        async def get_recent_trades(self, symbol, limit=60):
            return [
                {"qty": "2", "isBuyerMaker": True},
                {"qty": "1", "isBuyerMaker": False},
            ]

        async def get_symbol_ticker(self, symbol):
            return {"price": "125"}

        async def get_klines(self, symbol, interval, limit=2):
            return [
                [0, "100", 0, 0, "102", 0, 0, 0, 0, 0, "0", "0"],
                [0, "0", 0, 0, "0", 0, 0, 0, 0, 0, "0", "0"],
            ]

    import bot.sell_bot as bot_module
    bot_module.FEE_BUY = 0.0
    bot_module.FEE_SELL = 0.0
    bot_module.MIN_PROFIT = 0.0
    bot_module.TARGET_STEPS = 2
    watcher = bot_module.SellBot(HighClient())
    tracker = bot_module.FifoTracker()
    tracker.add_trade(1, 100.0)
    watcher.positions["BTCUSDT"] = bot_module.Position(tracker, 0.0, 0.0)
    watcher.positions["BTCUSDT"].peak = 125.0
    async def fake_vol2(*_args, **_kwargs):
        return 0.2

    monkeypatch.setattr(bot_module.SellBot, "get_volatility", fake_vol2)
    watcher.btc_above_sma7 = True
    decision = asyncio.run(watcher.should_sell("BTCUSDT", 125.0, 100.0))
    assert decision is True


def test_sell_when_falls_below_after_top(monkeypatch):
    class DownClient(DummyClient):
        async def get_recent_trades(self, symbol, limit=60):
            return [
                {"qty": "1", "isBuyerMaker": True},
                {"qty": "2", "isBuyerMaker": False},
            ]

        async def get_symbol_ticker(self, symbol):
            return {"price": "120"}

        async def get_klines(self, symbol, interval, limit=2):
            return [
                [0, "100", 0, 0, "102", 0, 0, 0, 0, 0, "0", "0"],
                [0, "0", 0, 0, "0", 0, 0, 0, 0, 0, "0", "0"],
            ]

    import bot.sell_bot as bot_module
    bot_module.FEE_BUY = 0.0
    bot_module.FEE_SELL = 0.0
    bot_module.MIN_PROFIT = 0.0
    bot_module.TARGET_STEPS = 2
    watcher = bot_module.SellBot(DownClient())
    tracker = bot_module.FifoTracker()
    tracker.add_trade(1, 100.0)
    pos = bot_module.Position(tracker, 0.0, 0.0)
    pos.peak = 125.0
    watcher.positions["BTCUSDT"] = pos
    async def fake_vol(*_args, **_kwargs):
        return 0.2

    monkeypatch.setattr(bot_module.SellBot, "get_volatility", fake_vol)
    watcher.btc_above_sma7 = True
    # İlk olarak hedef geçildiğinde hacim kontrolü yapılır ancak satış olmaz
    first = asyncio.run(watcher.should_sell("BTCUSDT", 125.0, 100.0))
    assert first is False
    # Fiyat yeniden hedefin altına düşünce satış sinyali gelir
    second = asyncio.run(watcher.should_sell("BTCUSDT", 119.0, 100.0))
    assert second is True


def test_repeated_volume_checks(monkeypatch):
    class RepeatClient(DummyClient):
        def __init__(self):
            self.calls = 0

        async def get_recent_trades(self, symbol, limit=60):
            self.calls += 1
            if self.calls == 1:
                return [
                    {"qty": "1", "isBuyerMaker": True},
                    {"qty": "2", "isBuyerMaker": False},
                ]
            return [
                {"qty": "3", "isBuyerMaker": True},
                {"qty": "1", "isBuyerMaker": False},
            ]

        async def get_symbol_ticker(self, symbol):
            return {"price": "125"}

        async def get_klines(self, symbol, interval, limit=2):
            return [
                [0, "100", 0, 0, "102", 0, 0, 0, 0, 0, "0", "0"],
                [0, "0", 0, 0, "0", 0, 0, 0, 0, 0, "0", "0"],
            ]

    import bot.sell_bot as bot_module
    bot_module.FEE_BUY = 0.0
    bot_module.FEE_SELL = 0.0
    bot_module.MIN_PROFIT = 0.0
    bot_module.TARGET_STEPS = 2
    client = RepeatClient()
    watcher = bot_module.SellBot(client)
    tracker = bot_module.FifoTracker()
    tracker.add_trade(1, 100.0)
    pos = bot_module.Position(tracker, 0.0, 0.0)
    pos.peak = 125.0
    watcher.positions["BTCUSDT"] = pos

    async def fake_vol(*_args, **_kwargs):
        return 0.2

    monkeypatch.setattr(bot_module.SellBot, "get_volatility", fake_vol)
    watcher.btc_above_sma7 = True
    first = asyncio.run(watcher.should_sell("BTCUSDT", 125.0, 100.0))
    assert first is False
    second = asyncio.run(watcher.should_sell("BTCUSDT", 125.0, 100.0))
    assert second is True


def test_sell_when_step_lost(monkeypatch):
    class StepClient(DummyClient):
        async def get_recent_trades(self, symbol, limit=60):
            return []

        async def get_symbol_ticker(self, symbol):
            return {"price": "105"}

        async def get_klines(self, symbol, interval, limit=2):
            return [
                [0, "100", 0, 0, "102", 0, 0, 0, 0, 0, "0", "0"],
                [0, "0", 0, 0, "0", 0, 0, 0, 0, 0, "0", "0"],
            ]

    import bot.sell_bot as bot_module
    bot_module.FEE_BUY = 0.0
    bot_module.FEE_SELL = 0.0
    bot_module.MIN_PROFIT = 0.0
    bot_module.TARGET_STEPS = 2
    watcher = bot_module.SellBot(StepClient())
    tracker = bot_module.FifoTracker()
    tracker.add_trade(1, 100.0)
    pos = bot_module.Position(tracker, 0.0, 0.0)
    pos.peak = 115.0
    watcher.positions["BTCUSDT"] = pos

    async def fake_vol(*_args, **_kwargs):
        return 0.2

    monkeypatch.setattr(bot_module.SellBot, "get_volatility", fake_vol)
    watcher.btc_above_sma7 = True
    # Hedef güncellendiğinde fiyat hedef üzerine çıkmadığı için satış yapmaz
    decision = asyncio.run(watcher.should_sell("BTCUSDT", 105.0, 100.0))
    assert decision is False
    # Fiyat hedef geçildikten sonra altına düşerse satış yapar
    pos.peak = 115.0
    decision2 = asyncio.run(watcher.should_sell("BTCUSDT", 105.0, 100.0))
    assert decision2 is True


def test_sell_without_volume_when_profit_five_times(monkeypatch):
    class FiveClient(DummyClient):
        async def get_recent_trades(self, symbol, limit=60):
            return [
                {"qty": "1", "isBuyerMaker": True},
                {"qty": "1", "isBuyerMaker": False},
            ]

        async def get_symbol_ticker(self, symbol):
            return {"price": "115"}

        async def get_klines(self, symbol, interval, limit=2):
            return [[0, "100", 0, 0, "100", 0, 0, 0, 0, 0, "0", "0"]] * 2

    import bot.sell_bot as bot_module
    bot_module.FEE_BUY = 0.0
    bot_module.FEE_SELL = 0.0
    bot_module.MIN_PROFIT = 0.02
    bot_module.TARGET_STEPS = 1
    watcher = bot_module.SellBot(FiveClient())
    tracker = bot_module.FifoTracker()
    tracker.add_trade(1, 100.0)
    watcher.positions["BTCUSDT"] = bot_module.Position(tracker, 0.0, 0.0)
    watcher.positions["BTCUSDT"].peak = 115.0

    async def fake_vol(*_args, **_kwargs):
        return 0.0

    monkeypatch.setattr(bot_module.SellBot, "get_volatility", fake_vol)
    watcher.btc_above_sma7 = False
    decision = asyncio.run(watcher.should_sell("BTCUSDT", 115.0, 100.0))
    assert decision is True


def test_static_target_drop(monkeypatch):
    class StaticClient(DummyClient):
        async def get_recent_trades(self, symbol, limit=60):
            return [
                {"qty": "1", "isBuyerMaker": True},
                {"qty": "2", "isBuyerMaker": False},
            ]

        async def get_symbol_ticker(self, symbol):
            return {"price": "112"}

        async def get_klines(self, symbol, interval, limit=2):
            return [
                [0, "100", 0, 0, "102", 0, 0, 0, 0, 0, "0", "0"],
                [0, "0", 0, 0, "0", 0, 0, 0, 0, 0, "0", "0"],
            ]

    import bot.sell_bot as bot_module
    bot_module.FEE_BUY = 0.05
    bot_module.FEE_SELL = 0.05
    bot_module.MIN_PROFIT = 0.05
    bot_module.TARGET_STEPS = 3
    watcher = bot_module.SellBot(StaticClient())
    tracker = bot_module.FifoTracker()
    tracker.add_trade(1, 100.0)
    pos = bot_module.Position(tracker, 0.0, 0.0)
    pos.peak = 116.0
    watcher.positions["BTCUSDT"] = pos

    async def fake_vol(*_args, **_kwargs):
        return 0.05

    monkeypatch.setattr(bot_module.SellBot, "get_volatility", fake_vol)
    watcher.btc_above_sma7 = True
    first = asyncio.run(watcher.should_sell("BTCUSDT", 116.0, 100.0))
    assert first is False
    second = asyncio.run(watcher.should_sell("BTCUSDT", 108.0, 100.0))
    assert second is True




def test_sync_time(monkeypatch):
    class Dummy:
        def __init__(self):
            self.timestamp_offset = 0

        async def get_server_time(self):
            return {"serverTime": 5000}

    dummy = Dummy()
    watcher = SellBot(dummy)
    monkeypatch.setattr(time, "time", lambda: 3)
    asyncio.run(watcher.sync_time())
    assert dummy.timestamp_offset == 5000 - 3000


def test_execute_sell_checks_balance():
    class SellClient(DummyClient):
        def __init__(self):
            self.sent_qty = None

        async def get_asset_balance(self, asset="BTC"):
            return {"free": "0.4", "locked": "0"}

        async def create_order(self, symbol, side, type, quantity):
            self.sent_qty = quantity
            return {"fills": [{"price": "100"}]}

    client = SellClient()
    watcher = SellBot(client)
    asyncio.run(watcher.load_balances())
    qty = watcher.positions["BTCUSDT"].tracker.total_qty()
    asyncio.run(watcher.execute_sell("BTCUSDT", qty))
    assert abs(client.sent_qty - 0.4) < 1e-8


def test_execute_sell_removes_when_zero():
    class ZeroClient(DummyClient):
        def __init__(self):
            self.orders = []

        async def get_asset_balance(self, asset="BTC"):
            return {"free": "0", "locked": "0"}

        async def create_order(self, *args, **kwargs):
            self.orders.append(kwargs.get("quantity"))

    client = ZeroClient()
    watcher = SellBot(client)
    asyncio.run(watcher.load_balances())
    qty = watcher.positions["BTCUSDT"].tracker.total_qty()
    asyncio.run(watcher.execute_sell("BTCUSDT", qty))
    assert "BTCUSDT" not in watcher.positions
    assert not client.orders


def test_execute_sell_records_timestamp(tmp_path, monkeypatch):
    class SellClient(DummyClient):
        async def get_asset_balance(self, asset="BTC"):
            return {"free": "0.5", "locked": "0"}

        async def create_order(self, symbol, side, type, quantity):
            return {"fills": [{"price": "100"}]}

    monkeypatch.setenv("BUY_DB_PATH", str(tmp_path / "s.db"))
    module = importlib.reload(bot_module)
    bot = module.SellBot(SellClient())
    asyncio.run(bot.load_balances())
    qty = bot.positions["BTCUSDT"].tracker.total_qty()
    asyncio.run(bot.execute_sell("BTCUSDT", qty))
    rows = sqlite3.connect(str(tmp_path / "s.db")).execute(
        "SELECT symbol FROM recent_sells"
    ).fetchall()
    assert ("BTCUSDT",) in rows


def test_manual_sell_detected(monkeypatch):
    class BalClient(DummyClient):
        def __init__(self):
            self.qty = 0.5

        async def get_asset_balance(self, asset="BTC"):
            return {"free": str(self.qty), "locked": "0"}

        async def get_symbol_ticker(self, symbol):
            return {"price": "100"}

    client = BalClient()
    watcher = SellBot(client)
    asyncio.run(watcher.load_balances())
    client.qty = 0.0
    asyncio.run(watcher.check_positions())
    assert "BTCUSDT" not in watcher.positions


def test_total_usdt_value():
    class BalClient(DummyClient):
        async def get_account(self):
            return {
                "balances": [
                    {"asset": "BTC", "free": "1", "locked": "0"},
                    {"asset": "USDT", "free": "50", "locked": "0"},
                ]
            }

        async def get_symbol_ticker(self, symbol):
            assert symbol == "BTCUSDT"
            return {"price": "10"}

    client = BalClient()
    watcher = SellBot(client)
    total = asyncio.run(watcher.get_total_usdt_value())
    assert abs(total - 60) < 1e-8


def test_total_value_filters_min_qty():
    class SmallClient(DummyClient):
        async def get_account(self):
            return {
                "balances": [
                    {"asset": "ABC", "free": "0.001", "locked": "0"},
                    {"asset": "USDT", "free": "5", "locked": "0"},
                ]
            }

        async def get_symbol_info(self, symbol):
            return {
                "filters": [
                    {"filterType": "LOT_SIZE", "minQty": "0.01"},
                    {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                ]
            }

        async def get_symbol_ticker(self, symbol):
            assert symbol == "ABCUSDT"
            return {"price": "10"}

    client = SmallClient()
    watcher = SellBot(client)
    total = asyncio.run(watcher.get_total_usdt_value())
    assert abs(total - 5) < 1e-8


def test_check_positions_connection_error():
    class ErrClient(DummyClient):
        async def get_asset_balance(self, asset="BTC"):
            return {"free": "0.5", "locked": "0"}

        async def get_symbol_ticker(self, symbol):
            raise Exception("conn error")

    client = ErrClient()
    watcher = SellBot(client)
    tracker = FifoTracker()
    tracker.add_trade(0.5, 100)
    watcher.positions["BTCUSDT"] = Position(tracker, 0.0001, 5)
    # Should not raise even if API call fails during check
    asyncio.run(watcher.check_positions())


def test_check_positions_groups(monkeypatch):
    module = importlib.reload(bot_module)
    module.CHECK_INTERVAL = 10
    module.RATE_LIMIT_PER_MINUTE = 6
    module.GROUP_SIZE = 1
    watcher = module.SellBot(DummyClient())
    pos = module.Position(module.FifoTracker(), 0.0, 0.0)
    for i in range(3):
        watcher.positions[f"S{i}USDT"] = pos

    called = []

    async def fake_check(self, symbol, position, price=None):
        called.append(symbol)

    monkeypatch.setattr(module.SellBot, "_check_symbol", fake_check)

    asyncio.run(watcher.check_positions())
    assert called == ["S0USDT"]
    called.clear()
    asyncio.run(watcher.check_positions())
    assert called == ["S1USDT"]
    called.clear()
    asyncio.run(watcher.check_positions())
    assert called == ["S2USDT"]


def test_btc_sma25_stop(monkeypatch):
    monkeypatch.setenv("STOP_LOSS_ENABLED", "true")
    module = importlib.reload(bot_module)

    class Dummy(DummyClient):
        async def get_account(self):
            return {"balances": [{"asset": "A", "free": "1", "locked": "0"}]}

        async def get_symbol_info(self, symbol):
            return {"filters": [{"filterType": "LOT_SIZE", "minQty": "0.1"}]}

        async def get_my_trades(self, symbol, limit=1000, fromId=None):
            return [{"id": 0, "qty": "1", "price": "10", "isBuyer": True}]

        async def get_symbol_ticker(self, symbol):
            return {"price": "10"}

    watcher = module.SellBot(Dummy())
    asyncio.run(watcher.load_balances())
    sold = []

    async def fake_sell(self, symbol, qty):
        sold.append(symbol)

    monkeypatch.setattr(module.SellBot, "execute_sell", fake_sell)
    async def fake_btc(self):
        return True

    monkeypatch.setattr(module.SellBot, "is_btc_below_sma25", fake_btc)
    async def fake_sync(self):
        pass

    monkeypatch.setattr(module.SellBot, "sync_time", fake_sync)
    asyncio.run(watcher.check_positions())
    assert "AUSDT" in sold


def test_send_telegram_prefix(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "true")
    module = importlib.reload(bot_module)
    messages = []
    module.TELEGRAM_TOKEN = None
    module.CHAT_ID = None
    monkeypatch.setattr(module, "log", lambda m: messages.append(m))
    module.send_telegram("selam")
    assert messages and messages[0].startswith("TESTNET")


def test_load_balances_handles_timeout(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "false")
    module = importlib.reload(bot_module)

    class TimeoutClient(DummyClient):
        async def get_symbol_info(self, symbol):
            raise asyncio.TimeoutError()

    watcher = module.SellBot(TimeoutClient())
    # Should not raise despite the timeout
    asyncio.run(watcher.load_balances())
    assert not watcher.positions


def test_send_start_message(monkeypatch):
    module = importlib.reload(bot_module)
    messages = []
    monkeypatch.setattr(module, "send_telegram", lambda *a, **k: messages.append(a[0]))
    module.send_start_message("LIVE", "1.2.3.4", 0)
    assert messages and "1.2.3.4" in messages[0]


def test_start_message_once(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET", "false")
    module = importlib.reload(bot_module)

    class DummyClient:
        async def ping(self):
            pass

        async def get_server_time(self):
            return {"serverTime": 0}

    bot = module.SellBot(DummyClient())
    messages = []
    monkeypatch.setattr(module, "send_start_message", lambda *a: messages.append(a))

    async def fake_load_balances(self):
        pass

    async def fake_sync_time(self):
        pass

    async def fake_check_api(self):
        pass

    async def fake_is_btc(self):
        return False

    async def fake_listen(self, bsm):
        pass

    async def fake_daily(self):
        pass

    async def fake_monitor(self):
        pass

    monkeypatch.setattr(module.SellBot, "load_balances", fake_load_balances)
    monkeypatch.setattr(module.SellBot, "sync_time", fake_sync_time)
    monkeypatch.setattr(module.SellBot, "check_api", fake_check_api)
    monkeypatch.setattr(module.SellBot, "is_btc_above_sma7", fake_is_btc)
    monkeypatch.setattr(module.SellBot, "listen_user_socket", fake_listen)
    monkeypatch.setattr(module.SellBot, "daily_balance_loop", fake_daily)
    monkeypatch.setattr(module.SellBot, "monitor_api", fake_monitor)
    monkeypatch.setattr(module.SellBot, "monitor_btc_sma", fake_monitor)
    monkeypatch.setattr(module, "BinanceSocketManager", lambda c: object())

    asyncio.run(bot.start())
    assert len(messages) == 1
    asyncio.run(bot.start())
    assert len(messages) == 1


def test_check_symbol_logs_once(monkeypatch):
    module = importlib.reload(bot_module)

    class FixedClient(DummyClient):
        async def get_asset_balance(self, asset="BTC"):
            return {"free": "0.5", "locked": "0"}

        async def get_symbol_ticker(self, symbol):
            return {"price": "100"}

    watcher = module.SellBot(FixedClient())
    tracker = module.FifoTracker()
    tracker.add_trade(0.5, 90)
    pos = module.Position(tracker, 0.0001, 5)
    watcher.positions["BTCUSDT"] = pos
    messages = []
    monkeypatch.setattr(module, "log", lambda m: messages.append(m))
    async def fake_should_sell(*_args, **_kwargs):
        return False

    monkeypatch.setattr(module.SellBot, "should_sell", fake_should_sell)
    asyncio.run(watcher._check_symbol("BTCUSDT", pos, price=None))
    first_len = len(messages)
    asyncio.run(watcher._check_symbol("BTCUSDT", pos, price=None))
    assert len(messages) == first_len


def test_should_sell_logs_targets_once(monkeypatch):
    module = importlib.reload(bot_module)

    class SimpleClient(DummyClient):
        async def get_recent_trades(self, symbol, limit=60):
            return []

        async def get_symbol_ticker(self, symbol):
            return {"price": "100"}

        async def get_klines(self, symbol, interval, limit=2):
            return [[0, "100", 0, 0, "100", 0, 0, 0, 0, 0, "0", "0"]] * 2

    module.FEE_BUY = 0.0
    module.FEE_SELL = 0.0
    module.MIN_PROFIT = 0.0
    module.TARGET_STEPS = 1
    watcher = module.SellBot(SimpleClient())
    watcher.positions["BTCUSDT"] = module.Position(module.FifoTracker(), 0.0, 0.0)
    messages = []
    monkeypatch.setattr(module, "log", lambda m: messages.append(m))
    asyncio.run(watcher.should_sell("BTCUSDT", 100.0, 90.0))
    first = len(messages)
    asyncio.run(watcher.should_sell("BTCUSDT", 100.0, 90.0))
    assert len(messages) == first


def test_restart_socket_on_new_symbol(monkeypatch):
    module = importlib.reload(bot_module)

    class SimpleClient(DummyClient):
        async def get_symbol_ticker(self, symbol):
            return {"price": "100"}

    bot = module.SellBot(SimpleClient())
    bot.bsm = object()
    bot.positions["AAAUSDT"] = module.Position(module.FifoTracker(), 0.0001, 5)

    called = []

    async def fake_restart(self):
        called.append(True)

    monkeypatch.setattr(module.SellBot, "restart_price_socket", fake_restart)
    asyncio.run(bot.add_buy("BBBUSDT", 1.0, 100.0))
    assert called


def test_handle_msg_uses_cumulative_qty(monkeypatch):
    module = importlib.reload(bot_module)

    class SimpleClient(DummyClient):
        async def get_symbol_ticker(self, symbol):
            return {"price": "100"}

    bot = module.SellBot(SimpleClient())
    bot.bsm = object()

    captured = {}

    async def fake_add(self, symbol, qty, price, *_args):
        captured['qty'] = qty

    monkeypatch.setattr(module.SellBot, 'add_buy', fake_add)

    msg = {
        'e': 'executionReport',
        's': 'AAAUSDT',
        'X': 'FILLED',
        'S': 'BUY',
        'l': '0.5',
        'z': '1.5',
        'L': '100',
    }

    asyncio.run(bot.handle_msg(msg))
    assert captured['qty'] == 1.5


def test_check_new_balances_adds_only_new(monkeypatch):
    module = importlib.reload(bot_module)

    class BalClient(DummyClient):
        async def get_account(self):
            return {
                "balances": [
                    {"asset": "BTC", "free": "0.5", "locked": "0"},
                    {"asset": "ETH", "free": "0.3", "locked": "0"},
                ]
            }

        async def get_symbol_info(self, symbol):
            return {
                "filters": [
                    {"filterType": "LOT_SIZE", "minQty": "0.0001", "stepSize": "0.0001"},
                    {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                ]
            }

        async def get_my_trades(self, symbol, limit=1000, fromId=None):
            return [{"qty": "0.3", "price": "1000", "isBuyer": True, "id": 1}]

        async def get_symbol_ticker(self, symbol):
            return {"price": "1000"}

    bot = module.SellBot(BalClient())
    bot.bsm = object()
    bot.positions["BTCUSDT"] = module.Position(module.FifoTracker(), 0.0001, 5)
    called = []

    async def fake_restart(self):
        called.append(True)

    monkeypatch.setattr(module.SellBot, "restart_price_socket", fake_restart)
    asyncio.run(bot.check_new_balances())
    assert "ETHUSDT" in bot.positions
    assert "BTCUSDT" in bot.positions
    assert called


def test_check_new_balances_updates_average(monkeypatch):
    module = importlib.reload(bot_module)

    class BalClient(DummyClient):
        async def get_account(self):
            return {"balances": [{"asset": "BTC", "free": "2", "locked": "0"}]}

        async def get_symbol_info(self, symbol):
            return {
                "filters": [
                    {"filterType": "LOT_SIZE", "minQty": "0.0001", "stepSize": "0.0001"},
                    {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                ]
            }

        async def get_my_trades(self, symbol, limit=1000, fromId=None):
            return [
                {"qty": "1", "price": "1000", "isBuyer": True, "id": 1},
                {"qty": "1", "price": "2000", "isBuyer": True, "id": 2},
            ]

        async def get_symbol_ticker(self, symbol):
            return {"price": "2000"}

    bot = module.SellBot(BalClient())
    bot.bsm = object()
    tracker = module.FifoTracker()
    tracker.add_trade(1, 1000)
    bot.positions["BTCUSDT"] = module.Position(tracker, 0.0001, 5)
    asyncio.run(bot.check_new_balances())
    avg = bot.positions["BTCUSDT"].tracker.average_price()
    assert abs(avg - 1500) < 1e-8


def test_should_sell_triggers_stop_loss(monkeypatch):
    monkeypatch.setenv("STOP_LOSS_ENABLED", "true")
    import bot.sell_bot as bot_module
    module = importlib.reload(bot_module)

    class SLClient(DummyClient):
        async def get_recent_trades(self, symbol, limit=60):
            return []

        async def get_symbol_ticker(self, symbol):
            return {"price": "95"}

        async def get_klines(self, symbol, interval, limit=2):
            return [
                [0, "100", "102", "98", "100", 0, 0, 0, 0, 0, "0", "0"],
                [0, "0", "0", "0", "0", 0, 0, 0, 0, 0, "0", "0"],
            ]

    async def atr_klines(self, symbol, interval, limit):
        data = []
        base = 100
        for i in range(limit):
            o = base + i
            data.append([0, str(o), str(o + 2), str(o - 2), str(o + 1), 0, 0, 0, 0, 0, "0", "0"])
        return data

    monkeypatch.setattr(SLClient, "get_klines", atr_klines)

    module.ATR_PERIOD = 14
    module.STOP_LOSS_MULTIPLIER = 1.0
    watcher = module.SellBot(SLClient())
    tracker = module.FifoTracker()
    tracker.add_trade(1, 100.0)
    watcher.positions["BTCUSDT"] = module.Position(tracker, 0.0, 0.0)

    async def fake_vol(*_args, **_kwargs):
        return 0.0

    monkeypatch.setattr(module.SellBot, "get_volatility", fake_vol)
    decision = asyncio.run(watcher.should_sell("BTCUSDT", 95.0, 100.0))
    assert decision is True
