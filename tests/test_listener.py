import asyncio
import bot.telegram_listener as listener


def test_start_listener_conflict(monkeypatch):
    class DummyUpdater:
        def __init__(self, token, use_context=True):
            self.dispatcher = type('D', (), {'add_handler': lambda *a, **k: None})()
        def start_polling(self):
            raise Exception('Conflict: terminated by other getUpdates request')

    monkeypatch.setattr(listener, 'Updater', DummyUpdater)
    logs = []
    monkeypatch.setattr(listener, 'log', lambda m: logs.append(m))
    monkeypatch.setenv('TELEGRAM_TOKEN', 'X')
    loop = asyncio.new_event_loop()
    try:
        listener.start_listener(loop)
    finally:
        loop.close()
    assert any('baslatilamadi' in m for m in logs)


def test_cmd_buy_handles_skip(monkeypatch):
    messages = []

    class DummyDispatcher:
        def __init__(self):
            self.handlers = {}
        def add_handler(self, handler):
            self.handlers[next(iter(handler.commands))] = handler.callback

    class DummyUpdater:
        def __init__(self, token, use_context=True):
            DummyUpdater.instance = self
            self.dispatcher = DummyDispatcher()
        def start_polling(self):
            pass

    class DummyClient:
        async def get_asset_balance(self, asset="USDT"):
            return {"free": "5"}

    class DummyBuy:
        def __init__(self):
            self.client = DummyClient()
            self.last_skip_reason = "skip"

        async def execute_buy(self, symbol, amount):
            self.last_skip_reason = "no balance"
            return False

    monkeypatch.setattr(listener, "Updater", DummyUpdater)
    monkeypatch.setattr(listener, "send_telegram", lambda msg, **_: messages.append(msg))
    monkeypatch.setenv("TELEGRAM_TOKEN", "T")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    loop = asyncio.new_event_loop()
    buy = DummyBuy()
    listener.start_listener(loop, buy_bot=buy)
    cmd = DummyUpdater.instance.dispatcher.handlers["buy"]

    def fake_run(coro, _loop):
        class F:
            def result(self_inner):
                return loop.run_until_complete(coro)
        return F()

    monkeypatch.setattr(listener.asyncio, "run_coroutine_threadsafe", fake_run)

    update = type("U", (), {"effective_chat": type("C", (), {"id": 1})()})()
    context = type("Ctx", (), {"args": ["AAA"]})()
    cmd(update, context)
    loop.close()
    assert any("skipped" in m or "atlan" in m for m in messages)
