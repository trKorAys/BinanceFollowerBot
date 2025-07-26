import os
import asyncio
from datetime import datetime, timezone, timedelta
import sqlite3
import time
import numpy as np
import requests
from typing import Optional

try:
    import talib
except Exception:  # pragma: no cover - talib is optional in tests
    talib = None

from binance import AsyncClient
from .utils import (
    FifoTracker,
    extract_min_notional,
    extract_min_qty,
    extract_max_qty,
    log,
    floor_to_precision,
    seconds_until_next_six_hour,
    load_env,
)
from .messages import t

load_env()

TESTNET = os.getenv("BINANCE_TESTNET", "false").lower() == "true"
API_KEY = (
    os.getenv("BINANCE_TESTNET_API_KEY") if TESTNET else os.getenv("BINANCE_API_KEY")
)
API_SECRET = (
    os.getenv("BINANCE_TESTNET_API_SECRET")
    if TESTNET
    else os.getenv("BINANCE_API_SECRET")
)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MIN_PROFIT_PERCENT = float(os.getenv("MIN_PROFIT_PERCENT", "0.5"))
MIN_PROFIT_RATIO = MIN_PROFIT_PERCENT / 100

# Komisyon yüzdeleri hem yüzde hem de oran olarak tutulur
FEE_BUY_PERCENT = float(os.getenv("FEE_BUY_PERCENT", "0.1"))
FEE_BUY = FEE_BUY_PERCENT / 100
FEE_SELL_PERCENT = float(os.getenv("FEE_SELL_PERCENT", "0.1"))
TESTNET_INITIAL_USDT = float(os.getenv("TESTNET_INITIAL_USDT", "5000"))
USDT_USAGE_RATIO = float(os.getenv("USDT_USAGE_RATIO", "0.99"))
LOSS_BUY_THRESHOLD_PERCENT = float(os.getenv("LOSS_BUY_THRESHOLD_PERCENT", "2"))
MIN_LOSER_USDT = 5.0  # Zarardaki bakiyeleri kontrol etmek icin alt limit
MIN_FOLLOW_NOTIONAL = float(os.getenv("MIN_FOLLOW_NOTIONAL", "5"))
SMA_PERIOD = 7 * 96  # 7 gunluk SMA icin 15 dakikalik mum sayisi
LONG_SMA_PERIOD = 25 * 96  # 25 gunluk SMA
TOP_SYMBOLS_COUNT = int(os.getenv("TOP_SYMBOLS_COUNT", "150"))
BUY_DB_PATH = os.getenv("BUY_DB_PATH", "buy.db")
EXCLUDED_BASES = [
    s.strip().upper()
    for s in os.getenv(
        "EXCLUDED_BASES", "BUSD,USDC,USDP,TUSD,DAI"
    ).split(",")
    if s.strip()
]


def send_telegram(text: str, chat_id: Optional[str] = None, force: bool = False) -> None:
    """Telegram'a BuyBot adıyla Markdown formatında mesaj gönder."""
    if TESTNET:
        text = f"TESTNET {text}"
        if not force:
            log(text)
            return
    chat_id = chat_id or CHAT_ID
    if not TELEGRAM_TOKEN or not chat_id:
        log(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception:
        log("Telegram API hatasi")


def get_public_ip() -> str:
    """Bulunulan ortamın genel IP adresini döndür."""
    try:
        resp = requests.get("https://api.ipify.org", timeout=5)
        return resp.text.strip()
    except Exception:
        return "0.0.0.0"


def send_start_message(mode: str, ip: str, count: int) -> None:
    """Bot başlarken mod, IP ve izlenen coin sayısını Telegram'a gönder."""
    text = (
        f"🟢 *BUY Bot Başladı* (MODE: `{mode}`)\n*IP:* `{ip}`\n*İzlenen Coin Sayısı:* `{count}`"
    )
    send_telegram(text)


def notify_buy(symbol: str, qty: float, price: float) -> None:
    """Gerçekleşen alış işlemini Telegram'a bildir."""
    total_percent = MIN_PROFIT_PERCENT + FEE_SELL_PERCENT + FEE_BUY_PERCENT
    target = price * (1 + total_percent / 100)
    min_profit = qty * price * (total_percent / 100)
    msg = (
        "💹 *ALIM Gerçekleşti*\n"
        f"🔜 *Sembol:* `{symbol.replace('USDT', '')}`\n"
        f"🔜 *Adet:* `{qty:.8f}`\n"
        f"🔜 *Alış Fiyatı:* `{price:.8f}`\n"
        f"🔜 *Hedef Fiyat:* `{target:.8f}`\n"
    )
    send_telegram(msg, force=True)


def calculate_sma(prices, period=SMA_PERIOD):
    """Verilen periyot için SMA hesapla; ta-lib varsa onu kullan."""
    arr = np.array(prices, dtype=float)
    if talib is not None:
        return talib.SMA(arr, timeperiod=period)
    sma = []
    for i in range(len(arr)):
        if i + 1 < period:
            sma.append(np.nan)
        else:
            sma.append(arr[i - period + 1 : i + 1].mean())
    return np.array(sma)


def is_cross_over(prices):
    """Kapanışın SMA-7'yi yukarı kesmesi ve SMA-7 < SMA-25 koşulu."""
    sma_short = calculate_sma(prices, SMA_PERIOD)
    sma_long = calculate_sma(prices, LONG_SMA_PERIOD)
    if (
        len(sma_short) < 2
        or np.isnan(sma_short[-1])
        or np.isnan(sma_short[-2])
        or np.isnan(sma_long[-1])
    ):
        return False
    cross = prices[-2] < sma_short[-2] and prices[-1] > sma_short[-1]
    return cross and sma_short[-1] < sma_long[-1]


def _ema(values, period):
    arr = np.array(values, dtype=float)
    if len(arr) == 0:
        return arr
    alpha = 2 / (period + 1)
    ema = [arr[0]]
    for v in arr[1:]:
        ema.append((v - ema[-1]) * alpha + ema[-1])
    return np.array(ema)


def _atr(highs, lows, closes, period=14):
    highs = np.array(highs, dtype=float)
    lows = np.array(lows, dtype=float)
    closes = np.array(closes, dtype=float)
    trs = [highs[0] - lows[0]]
    for i in range(1, len(closes)):
        high_low = highs[i] - lows[i]
        high_close = abs(highs[i] - closes[i - 1])
        low_close = abs(lows[i] - closes[i - 1])
        trs.append(max(high_low, high_close, low_close))
    return _ema(trs, period)


def _dmi(highs, lows, closes, period=14):
    highs = np.array(highs, dtype=float)
    lows = np.array(lows, dtype=float)
    closes = np.array(closes, dtype=float)
    plus_dm = np.zeros_like(highs)
    minus_dm = np.zeros_like(lows)
    for i in range(1, len(highs)):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        if up > down and up > 0:
            plus_dm[i] = up
        if down > up and down > 0:
            minus_dm[i] = down
    tr = _atr(highs, lows, closes, 1)
    atr = _ema(tr, period)
    plus_di = 100 * _ema(plus_dm, period) / (atr + 1e-8)
    minus_di = 100 * _ema(minus_dm, period) / (atr + 1e-8)
    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di + 1e-8)
    adx = _ema(dx, period)
    return adx, plus_di, minus_di


def _rsi(closes, period=14):
    closes = np.array(closes, dtype=float)
    if len(closes) < period + 1:
        return np.array([])
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.zeros_like(closes)
    avg_loss = np.zeros_like(closes)
    avg_gain[period] = gains[:period].mean()
    avg_loss[period] = losses[:period].mean()
    for i in range(period + 1, len(closes)):
        avg_gain[i] = (avg_gain[i - 1] * (period - 1) + gains[i - 1]) / period
        avg_loss[i] = (avg_loss[i - 1] * (period - 1) + losses[i - 1]) / period
    rs = avg_gain / (avg_loss + 1e-8)
    rsi = 100 - (100 / (1 + rs))
    rsi[: period] = np.nan
    return rsi


def _ichimoku(highs, lows):
    highs = np.array(highs, dtype=float)
    lows = np.array(lows, dtype=float)

    def _rolling_max(arr, p):
        return np.array([np.max(arr[i - p + 1 : i + 1]) if i >= p - 1 else np.nan for i in range(len(arr))])

    def _rolling_min(arr, p):
        return np.array([np.min(arr[i - p + 1 : i + 1]) if i >= p - 1 else np.nan for i in range(len(arr))])

    tenkan = (_rolling_max(highs, 9) + _rolling_min(lows, 9)) / 2
    kijun = (_rolling_max(highs, 26) + _rolling_min(lows, 26)) / 2
    span_a = (tenkan + kijun) / 2
    span_b = (_rolling_max(highs, 52) + _rolling_min(lows, 52)) / 2
    return tenkan, kijun, span_a, span_b


def _keltner(highs, lows, closes, period_ema=20, period_atr=10, mult=1.5):
    ema = _ema(closes, period_ema)
    atr = _atr(highs, lows, closes, period_atr)
    upper = ema + atr * mult
    lower = ema - atr * mult
    return upper, lower


def _chaikin_money_flow(highs, lows, closes, volumes, period=20):
    highs = np.array(highs, dtype=float)
    lows = np.array(lows, dtype=float)
    closes = np.array(closes, dtype=float)
    volumes = np.array(volumes, dtype=float)
    mfm = ((closes - lows) - (highs - closes)) / (highs - lows + 1e-8)
    mfv = mfm * volumes
    cmf = []
    for i in range(len(closes)):
        if i + 1 < period:
            cmf.append(np.nan)
        else:
            mfv_sum = mfv[i - period + 1 : i + 1].sum()
            vol_sum = volumes[i - period + 1 : i + 1].sum()
            cmf.append(mfv_sum / (vol_sum + 1e-8))
    return np.array(cmf)


def meets_buy_conditions(opens, highs, lows, closes, volumes):
    """Tüm indikatör koşullarını değerlendir."""
    if len(closes) < 52:
        return False
    tenkan, kijun, span_a, span_b = _ichimoku(highs, lows)
    cond_a = closes[-1] > span_a[-1] and closes[-1] > span_b[-1]
    cond_b = tenkan[-1] > kijun[-1]
    adx, plus_di, minus_di = _dmi(highs, lows, closes)
    cond_c = adx[-1] >= 25 and plus_di[-1] > minus_di[-1]
    upper, _ = _keltner(highs, lows, closes)
    cond_d = closes[-2] <= upper[-2] and closes[-1] > upper[-1]
    cmf = _chaikin_money_flow(highs, lows, closes, volumes)
    cond_e = cmf[-1] > 0
    rsi = _rsi(closes)
    cond_f = rsi.size > 0 and rsi[-1] < 70
    return all([cond_a, cond_b, cond_c, cond_d, cond_e, cond_f])


class BuyBot:
    def __init__(self, client: AsyncClient):
        self.client = client
        self.api_down = False
        self.current_ip = None
        self.db = sqlite3.connect(BUY_DB_PATH, check_same_thread=False)
        self._init_db()
        self.last_buy_times = self._load_recent_buys()
        self.last_sell_times = self._load_recent_sells()
        self.start_notified = False
        self.top_symbols = []
        # Zarar kontrolü sadece zarar stratejisinde yapılır.
        # Sembol veya SMA stratejilerinde bu bayrak False olarak ayarlanır.
        self.loss_check_enabled = True
        # Son başarısız alımın sebebi
        self.last_skip_reason = ""

    def _init_db(self) -> None:
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS recent_buys (symbol TEXT PRIMARY KEY, time TEXT)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS recent_sells (symbol TEXT PRIMARY KEY, time TEXT)"
        )
        self.db.commit()

    def _load_recent_buys(self):
        cur = self.db.execute("SELECT symbol, time FROM recent_buys")
        rows = cur.fetchall()
        result = {}
        now = datetime.now(timezone.utc)
        for sym, ts in rows:
            try:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if now - dt < timedelta(hours=2):
                    result[sym] = dt
                else:
                    self.db.execute(
                        "DELETE FROM recent_buys WHERE symbol=?", (sym,)
                    )
            except Exception:
                continue
        self.db.commit()
        return result

    def _load_recent_sells(self):
        cur = self.db.execute("SELECT symbol, time FROM recent_sells")
        rows = cur.fetchall()
        result = {}
        now = datetime.now(timezone.utc)
        for sym, ts in rows:
            try:
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if now - dt < timedelta(hours=2):
                    result[sym] = dt
                else:
                    self.db.execute(
                        "DELETE FROM recent_sells WHERE symbol=?", (sym,)
                    )
            except Exception:
                continue
        self.db.commit()
        return result

    def _save_recent_buy(self, symbol: str, dt: datetime) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO recent_buys (symbol, time) VALUES (?, ?)",
            (symbol, dt.isoformat()),
        )
        self.db.commit()

    def _save_recent_sell(self, symbol: str, dt: datetime) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO recent_sells (symbol, time) VALUES (?, ?)",
            (symbol, dt.isoformat()),
        )
        self.db.commit()

    def _cleanup_recent_buys(self) -> None:
        """Veritabanindaki eski alis kayitlarini sil."""
        now = datetime.now(timezone.utc)
        expired = [s for s, dt in self.last_buy_times.items() if now - dt >= timedelta(hours=2)]
        for sym in expired:
            self.last_buy_times.pop(sym, None)
            self.db.execute("DELETE FROM recent_buys WHERE symbol=?", (sym,))
        if expired:
            self.db.commit()

    def _cleanup_recent_sells(self) -> None:
        """Veritabanindaki eski satis kayitlarini sil."""
        now = datetime.now(timezone.utc)
        expired = [s for s, dt in self.last_sell_times.items() if now - dt >= timedelta(hours=2)]
        for sym in expired:
            self.last_sell_times.pop(sym, None)
            self.db.execute("DELETE FROM recent_sells WHERE symbol=?", (sym,))
        if expired:
            self.db.commit()

    async def sync_time(self) -> None:
        """Sunucu saat farkını hesaplayıp istemciye uygula."""
        try:
            res = await self.client.get_server_time()
            self.client.timestamp_offset = res["serverTime"] - int(time.time() * 1000)
        except Exception as exc:  # pragma: no cover - API hatası
            log(f"Sunucu saati senkronize edilemedi: {exc}")

    async def ensure_testnet_balance(self):
        """Testnet bakiyesini gerekirse yukle."""
        target = TESTNET_INITIAL_USDT
        try:
            bal = await self.client.get_asset_balance(asset="USDT")
            current = float(bal.get("free", 0))
        except Exception as exc:
            log(f"Testnet bakiye kontrol hatasi: {exc}")
            return
        if current >= target:
            return
        try:
            # Binance kütüphanesi testnet üzerinde `sapi` uç noktasını
            # otomatik oluşturamadığından tam URL kullanıyoruz.
            base = self.client.API_TESTNET_URL.replace("/api", "")
            url = f"{base}/sapi/v1/account/testnet-funds"
            await self.client._request(
                "post",
                url,
                signed=True,
                params={"asset": "USDT", "amount": target - current},
            )
            log(f"Testnet bakiyesi {target} USDT olarak ayarlandi")
        except Exception:
            log("Bakiye Yuklenemedi")

    async def check_api(self) -> bool:
        """API baglantisini ve IP degisimini kontrol et."""
        ip = get_public_ip()
        if self.current_ip and ip != self.current_ip:
            send_telegram(t("new_ip", ip=ip))
            self.current_ip = ip
        try:
            await self.client.ping()
            if self.api_down:
                send_telegram(t("api_recovered"))
                self.api_down = False
            return True
        except Exception as exc:
            log(f"❗API baglantisi hatasi: {exc}")
            if not self.api_down:
                send_telegram(t("api_error", exc=exc))
                self.api_down = True
            return False

    async def monitor_api(self):
        """API durumunu dakikada bir kontrol et."""
        while True:
            await self.check_api()
            await asyncio.sleep(60)

    async def update_top_symbols(self):
        """USDT hacmi en yüksek sembolleri belirle."""
        try:
            info, tickers = await asyncio.gather(
                self.client.get_exchange_info(),
                self.client.get_ticker(),
            )
        except Exception as exc:
            log(f"Sembol listesi guncellenemedi: {exc}")
            return
        allowed = {}
        for s in info.get("symbols", []):
            if s.get("quoteAsset") != "USDT" or s.get("status") != "TRADING":
                continue
            base = s.get("baseAsset") or s.get("symbol", "").replace("USDT", "")
            if base.upper() in EXCLUDED_BASES:
                continue
            allowed[s["symbol"]] = True
        pairs = []
        for t in tickers:
            sym = t.get("symbol")
            if sym in allowed:
                qvol = float(t.get("quoteVolume", 0) or 0)
                if qvol == 0:
                    vol = float(t.get("volume", 0))
                    price = float(t.get("lastPrice", 0) or t.get("weightedAvgPrice", 0))
                    qvol = vol * price
                pairs.append((sym, qvol))
        pairs.sort(key=lambda x: x[1], reverse=True)
        self.top_symbols = [s for s, _ in pairs[:TOP_SYMBOLS_COUNT]]
        log(f"Sembol listesi guncellendi: {len(self.top_symbols)} adet")

    async def symbols_update_loop(self):
        while True:
            await self.update_top_symbols()
            wait = seconds_until_next_six_hour()
            await asyncio.sleep(wait)

    async def start(self):
        mode = "TESTNET" if TESTNET else "LIVE"
        log(f"BuyBot baslatiliyor. MODE: {mode}")
        self.current_ip = get_public_ip()
        await self.check_api()
        await self.sync_time()
        if TESTNET:
            await self.ensure_testnet_balance()
        await self.update_top_symbols()
        asyncio.create_task(self.symbols_update_loop())
        first_symbol = None
        self.loss_check_enabled = True
        if not self.api_down:
            first_symbol = await self.select_loser()
            if not first_symbol:
                self.loss_check_enabled = False
                first_symbol = await self.select_symbol()
                if not first_symbol:
                    first_symbol = await self.select_sma_cross()
        if not self.start_notified:
            send_start_message(mode, self.current_ip, 1 if first_symbol else 0)
            self.start_notified = True
        asyncio.create_task(self.monitor_api())
        if not self.api_down and first_symbol:
            try:
                bal = await self.client.get_asset_balance(asset="USDT")
                usdt = float(bal.get("free", 0))
            except Exception:
                usdt = 0.0
            await self._execute_cycle(first_symbol, usdt * USDT_USAGE_RATIO)
        while True:
            now = datetime.now(timezone.utc)
            wait = (15 - (now.minute % 15)) * 60 - now.second
            await asyncio.sleep(max(wait, 0))
            await self.run()

    async def fetch_symbols(self):
        if not self.top_symbols:
            await self.update_top_symbols()
        return list(self.top_symbols)

    async def select_symbol(self):
        """Beşli Doğrulama Alım Stratejisi."""
        symbols = await self.fetch_symbols()
        candidates = []  # (symbol, close, buy_vol, sell_vol)
        for symbol in symbols:
            try:
                ticker = await self.client.get_ticker(symbol=symbol)
            except Exception:
                continue
            volume = float(ticker.get("volume", 0))
            buy_vol = float(ticker.get("takerBuyBaseAssetVolume", 0))
            sell_vol = volume - buy_vol
            try:
                limit = 60
                klines = await self.client.get_klines(symbol=symbol, interval="15m", limit=limit)
            except Exception:
                continue
            if len(klines) < limit:
                continue
            opens = [float(k[1]) for k in klines[:-1]]
            highs = [float(k[2]) for k in klines[:-1]]
            lows = [float(k[3]) for k in klines[:-1]]
            closes = [float(k[4]) for k in klines[:-1]]
            volumes = [float(k[5]) for k in klines[:-1]]
            if meets_buy_conditions(opens, highs, lows, closes, volumes):
                candidates.append((symbol, closes[-1], buy_vol, sell_vol))

        filtered = [c for c in candidates if c[2] > c[3]]

        if filtered:
            # pick the coin with highest buy/sell volume ratio
            best = max(filtered, key=lambda x: x[2] / max(x[3], 1e-8))
            return best[0], best[1]

        if not candidates:
            return None

        candidates.sort(key=lambda x: x[2], reverse=True)
        top = candidates[0]
        return top[0], top[1]

    async def select_sma_cross(self):
        """SMA-7 yukarı kırılımı ve SMA-7 < SMA-25 koşulunu sağlayan ilk sembol."""
        symbols = await self.fetch_symbols()
        for symbol in symbols:
            try:
                limit = max(SMA_PERIOD, LONG_SMA_PERIOD) + 2
                klines = await self.client.get_klines(
                    symbol=symbol, interval="15m", limit=limit
                )
            except Exception:
                continue
            if len(klines) < limit:
                continue
            closes = [float(k[4]) for k in klines[:-1]]
            if is_cross_over(closes):
                return symbol, closes[-1]
        return None

    async def fetch_all_trades(self, symbol: str):
        """Tüm geçmiş işlemleri baştan sona sayfalayarak getir."""
        all_trades = []
        from_id = 0
        while True:
            params = {"symbol": symbol, "limit": 1000}
            if from_id is not None:
                params["fromId"] = from_id
            try:
                trades = await self.client.get_my_trades(**params)
            except Exception:
                break
            if not trades:
                break
            all_trades.extend(trades)
            if len(trades) < 1000:
                break
            from_id = trades[-1].get("id", 0) + 1
            await asyncio.sleep(0.2)
        return all_trades

    async def select_loser(self):
        """Zarardaki pozisyonlar arasından en çok düşene ait sembolü döndür."""
        try:
            account = await self.client.get_account()
        except Exception:
            return None

        sem = asyncio.Semaphore(5)

        async def handle_balance(bal):
            asset = bal.get("asset")
            qty = float(bal.get("free", 0)) + float(bal.get("locked", 0))
            if asset in ("USDT", "BUSD") or qty <= 0:
                return None
            symbol = f"{asset}USDT"
            async with sem:
                try:
                    info, ticker = await asyncio.gather(
                        self.client.get_symbol_info(symbol),
                        self.client.get_symbol_ticker(symbol=symbol),
                    )
                    min_qty = extract_min_qty(info)
                    if qty < min_qty:
                        log(
                            f"{symbol} bakiyesi {qty:.8f} minQty {min_qty:.8f} altında, atlandı"
                        )
                        return None
                    last_price = float(ticker["price"])
                    if qty * last_price < MIN_LOSER_USDT:
                        log(
                            f"{symbol} degeri {qty * last_price:.2f} USDT altinda, atlandi"
                        )
                        return None
                    trades = await self.fetch_all_trades(symbol)
                    if not trades:
                        return None
                    trades = sorted(trades, key=lambda x: x.get("time", 0))
                    tracker = FifoTracker()
                    for t in trades:
                        t_qty = float(t["qty"])
                        price = float(t["price"])
                        commission = float(t.get("commission", 0))
                        comm_asset = t.get("commissionAsset")
                        if t.get("isBuyer"):
                            if comm_asset == asset:
                                t_qty -= commission
                            tracker.add_trade(t_qty, price)
                        else:
                            sell_qty = t_qty
                            if comm_asset == asset:
                                sell_qty += commission
                            tracker.sell(sell_qty)
                    if tracker.total_qty() <= 0:
                        return None
                    avg = tracker.average_price()
                    percent = (last_price - avg) / avg * 100 if avg else 0.0
                    loss_usdt = (avg - last_price) * tracker.total_qty()
                    if loss_usdt > 0 and percent <= -LOSS_BUY_THRESHOLD_PERCENT:
                        return (loss_usdt, percent, symbol, last_price)
                except Exception:
                    return None
            return None

        tasks = [handle_balance(bal) for bal in account.get("balances", [])]
        results = await asyncio.gather(*tasks)

        worst = None
        for res in results:
            if res is not None and (worst is None or res[0] > worst[0]):
                worst = res

        if worst:
            return worst[2], worst[3]
        return None

    async def execute_buy(self, symbol: str, usdt_amount: float, check_loss: bool = True) -> bool:
        """Piyasa alım emri gönder. Başarılıysa True döner."""
        self.last_skip_reason = ""
        try:
            info = await self.client.get_symbol_info(symbol)
            ticker = await self.client.get_symbol_ticker(symbol=symbol)
        except Exception as exc:
            log(f"{symbol} bilgi hatasi: {exc}")
            self.last_skip_reason = str(exc)
            return False
        price = float(ticker["price"])
        try:
            trades = await self.fetch_all_trades(symbol)
            if trades:
                trades = sorted(trades, key=lambda x: x.get("time", 0))
                tracker = FifoTracker()
                base = symbol.replace("USDT", "")
                for t in trades:
                    t_qty = float(t["qty"])
                    t_price = float(t["price"])
                    commission = float(t.get("commission", 0))
                    comm_asset = t.get("commissionAsset")
                    if t.get("isBuyer"):
                        if comm_asset == base:
                            t_qty -= commission
                        tracker.add_trade(t_qty, t_price)
                    else:
                        sell_qty = t_qty
                        if comm_asset == base:
                            sell_qty += commission
                        tracker.sell(sell_qty)
                avg = tracker.average_price()
                if check_loss and avg > 0:
                    percent = (price - avg) / avg * 100
                    if percent > -LOSS_BUY_THRESHOLD_PERCENT:
                        reason = f"{symbol} zarari %{abs(percent):.2f} esigin altinda"
                        log(f"{reason}, alım iptal")
                        self.last_skip_reason = reason
                        return False
        except Exception:
            pass
        min_notional = max(extract_min_notional(info), 5)
        if not check_loss:
            min_notional = max(min_notional, MIN_FOLLOW_NOTIONAL)
        max_qty = extract_max_qty(info)
        notional = usdt_amount
        if notional / price > max_qty:
            log(f"{symbol} maxQty {max_qty} ile sinirlandi")
            notional = max_qty * price
        if notional < min_notional:
            reason = "miktar yetersiz"
            self.last_skip_reason = reason
            log(f"{symbol} icin {reason}")
            return False
        try:
            bal = await self.client.get_asset_balance(asset="USDT")
            available = float(bal.get("free", 0))
        except Exception:
            available = 0.0
        total_cost = notional * (1 + FEE_BUY)
        if available < total_cost:
            reason = f"bakiye yetersiz: {available} < {total_cost}"
            log(f"{symbol} icin {reason}")
            self.last_skip_reason = reason
            return False
        try:
            precision = int(
                info.get("quoteAssetPrecision") or info.get("quotePrecision", 8)
            )
            amount = floor_to_precision(notional, precision)
            await self.client.create_order(
                symbol=symbol,
                side="BUY",
                type="MARKET",
                quoteOrderQty=amount,
            )
            now_dt = datetime.now(timezone.utc)
            self.last_buy_times[symbol] = now_dt
            self._save_recent_buy(symbol, now_dt)
            log(
                f"{symbol} icin {amount:.{precision}f} USDT'lik alim emri gonderildi"
            )
            qty = amount / price
            notify_buy(symbol, qty, price)
            return True
        except Exception as exc:
            log(f"{symbol} alim hatasi: {exc}")
            self.last_skip_reason = str(exc)
            return False

    async def _execute_cycle(self, candidate, usdt_amount: float):
        """Seçilen tek sembolde alım yap."""
        if not candidate:
            log("Uygun sembol bulunamadi")
            return
        self._cleanup_recent_buys()
        self._cleanup_recent_sells()
        log(f"Serbest USDT bakiyesi: {usdt_amount:.8f}")
        if usdt_amount < MIN_LOSER_USDT:
            log(f"USDT bakiyesi {MIN_LOSER_USDT} USDT altinda, bekleniyor")
            return
        symbol, _ = candidate
        now = datetime.now(timezone.utc)
        last = self.last_buy_times.get(symbol)
        if not self.loss_check_enabled and last and now - last < timedelta(hours=2):
            log(f"{symbol} son iki saat icinde alindi, atlandi")
            if symbol in self.top_symbols:
                self.top_symbols.remove(symbol)
            return
        last_sell = self.last_sell_times.get(symbol)
        if not self.loss_check_enabled and last_sell and now - last_sell < timedelta(hours=2):
            log(f"{symbol} son iki saat icinde satildi, atlandi")
            return
        await self.execute_buy(symbol, usdt_amount, check_loss=self.loss_check_enabled)

    async def run(self):
        if self.api_down:
            log("API hatasi devam ediyor, tarama atlandi")
            return
        self._cleanup_recent_buys()
        self._cleanup_recent_sells()
        await self.sync_time()
        try:
            balance = await self.client.get_asset_balance(asset="USDT")
            usdt = float(balance.get("free", 0))
        except Exception:
            usdt = 0.0
        log(f"Serbest USDT bakiyesi: {usdt:.8f}")
        if usdt < MIN_LOSER_USDT:
            log(f"USDT bakiyesi {MIN_LOSER_USDT} USDT altinda, tarama atlandi")
            return
        log("Sembol taramasi basladi")
        log("Zarar stratejisi kontrol ediliyor")
        candidate = await self.select_loser()
        self.loss_check_enabled = True
        if not candidate:
            log("Beşli Doğrulama Alım Stratejisi kontrol ediliyor")
            candidate = await self.select_symbol()
            self.loss_check_enabled = False
        if not candidate:
            log("SMA stratejisi kontrol ediliyor")
            candidate = await self.select_sma_cross()
            self.loss_check_enabled = False
        await self._execute_cycle(candidate, usdt * USDT_USAGE_RATIO)


async def main():
    client = await AsyncClient.create(API_KEY, API_SECRET, testnet=TESTNET)
    bot = BuyBot(client)
    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())
