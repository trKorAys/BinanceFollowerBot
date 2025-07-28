import asyncio
import bot.sell_bot as bot_module
from bot.sell_bot import FifoTracker, Position


class DummyClient:
    async def get_recent_trades(self, symbol, limit=60):
        return []

    async def get_symbol_ticker(self, symbol):
        return {"price": "3.18"}


async def fake_open(self, symbol):
    return 3.25


async def fake_vol(*_args, **_kwargs):
    return 0.1


def test_targets_reset_peak(monkeypatch):
    monkeypatch.setattr(bot_module.SellBot, "get_last_open_price", fake_open)
    monkeypatch.setattr(bot_module.SellBot, "get_volatility", fake_vol)
    bot_module.FEE_BUY = 0.0
    bot_module.FEE_SELL = 0.0
    bot_module.MIN_PROFIT = 0.0
    bot_module.TARGET_STEPS = 1
    watcher = bot_module.SellBot(DummyClient())
    tracker = FifoTracker()
    tracker.add_trade(1, 3.0)
    pos = Position(tracker, 0.0, 0.0)
    pos.peak = 3.3
    watcher.positions["CAKEUSDT"] = pos
    watcher.btc_above_sma7 = True
    decision = asyncio.run(watcher.should_sell("CAKEUSDT", 3.18, 3.0))
    assert decision is False
    assert watcher.positions["CAKEUSDT"].peak == 3.18
