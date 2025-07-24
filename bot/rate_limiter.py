import asyncio
import time
import re
from typing import Optional
from binance import AsyncClient
from binance.exceptions import BinanceAPIException

class RequestLimiter:
    """6000 ağırlık/1 dk kuralını korumak için basit sayaç."""

    def __init__(self, limit=6000, interval=60):
        self.limit = limit
        self.interval = interval
        self.used = 0
        self.reset = time.monotonic()
        self.ban_until = 0.0
        # Kilit import sirasinda olusursa calisan event loop farkli olabilir.
        # Bu nedenle ilk kullanimda olusturmak icin None atiyoruz.
        self.lock: Optional[asyncio.Lock] = None

    def _ensure_lock(self) -> asyncio.Lock:
        """Her cagri oncesi mevcut event loop'a bagli kilidi dondur."""
        if self.lock is None:
            self.lock = asyncio.Lock()
        return self.lock

    async def update_used(self, used: int) -> None:
        lock = self._ensure_lock()
        async with lock:
            self.used = used

    async def set_ban_until(self, timestamp: float) -> None:
        lock = self._ensure_lock()
        async with lock:
            self.ban_until = timestamp

    async def acquire(self, weight=1):
        lock = self._ensure_lock()
        async with lock:
            now = time.monotonic()
            if now < self.ban_until:
                await asyncio.sleep(max(self.ban_until - now, 0))
                now = time.monotonic()
            if now - self.reset >= self.interval:
                self.used = 0
                self.reset = now
            if self.used + weight > self.limit:
                wait = self.interval - (now - self.reset)
                await asyncio.sleep(max(wait, 0))
                self.used = 0
                self.reset = time.monotonic()
            self.used += weight

limiter = RequestLimiter()
_original_request = AsyncClient._request

async def _limited_request(self, method, uri: str, signed: bool, force_params: bool=False, **kwargs):
    weight = kwargs.pop("weight", 1)
    await limiter.acquire(weight)
    try:
        result = await _original_request(self, method, uri, signed, force_params=force_params, **kwargs)
    except BinanceAPIException as exc:
        if exc.code == -1003:
            match = re.search(r"until (\d+)", str(exc))
            if match:
                ts_ms = int(match.group(1))
                await limiter.set_ban_until(time.monotonic() + max(0, ts_ms / 1000 - time.time()))
            else:
                await limiter.set_ban_until(time.monotonic() + limiter.interval)
        raise
    else:
        used = 0
        try:
            used = int(self.response.headers.get("X-MBX-USED-WEIGHT-1M", 0))
        except Exception:
            pass
        if used:
            await limiter.update_used(used)
        return result

AsyncClient._request = _limited_request
