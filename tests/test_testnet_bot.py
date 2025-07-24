import asyncio
import importlib
import pytest

import bot.testnet_bot as testnet


def test_wait_on_api_error(monkeypatch):
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "A")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "B")
    module = importlib.reload(testnet)

    class DummyClient:
        pass

    async def fake_create(*a, **kw):
        return DummyClient()

    monkeypatch.setattr(module.AsyncClient, "create", fake_create)

    class FakeBuy:
        async def start(self):
            pass

    class FakeSell:
        async def start(self):
            pass
        async def check_positions(self):
            raise Exception("oops")

    monkeypatch.setattr(module, "BuyBot", lambda c: FakeBuy())
    monkeypatch.setattr(module, "SellBot", lambda c: FakeSell())
    monkeypatch.setattr(module, "start_listener", lambda *a, **k: None)
    monkeypatch.setattr(module, "send_telegram", lambda *a, **k: None)
    monkeypatch.setattr(module, "setup_telegram_menu", lambda *a: None)

    sleeps = []

    async def fake_sleep(secs):
        sleeps.append(secs)
        raise RuntimeError

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)

    with pytest.raises(RuntimeError):
        asyncio.run(module.main())

    assert 10 in sleeps

