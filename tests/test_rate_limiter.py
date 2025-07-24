import asyncio
import time
from bot.rate_limiter import RequestLimiter


def test_limiter_waits():
    limiter = RequestLimiter(limit=2, interval=0.5)

    start = time.monotonic()
    asyncio.run(limiter.acquire())
    asyncio.run(limiter.acquire())
    asyncio.run(limiter.acquire())  # üçüncü çağrı sınırı aşar
    elapsed = time.monotonic() - start
    assert elapsed >= 0.5


def test_limiter_ban(monkeypatch):
    limiter = RequestLimiter(limit=2, interval=0.5)
    waits = []

    async def fake_sleep(secs):
        waits.append(secs)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    limiter.ban_until = time.monotonic() + 0.2
    asyncio.run(limiter.acquire())
    assert waits and waits[0] >= 0.19


def test_update_used():
    limiter = RequestLimiter()
    asyncio.run(limiter.update_used(5))
    assert limiter.used == 5
