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
from bot.utils import (
    FifoTracker,
    extract_min_notional,
    extract_min_qty,
    extract_max_qty,
    log,
    floor_to_precision,
    seconds_until_next_six_hour,
    load_env,
)
from bot.messages import t

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
TELEGRAM_ENABLED = os.getenv("TELEGRAM_ENABLED", "true").lower() == "true"
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
MAX_ATR = 200  # Yeni RSI-Keltner stratejisi icin ATR ust limiti
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
    if not TELEGRAM_ENABLED:
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


def _keltner(highs, lows, closes, period_ema=20, period_atr=10, mult=1.5):
    ema = _ema(closes, period_ema)
    atr = _atr(highs, lows, closes, period_atr)
    upper = ema + atr * mult
    lower = ema - atr * mult
    return upper, lower




def meets_rsi_keltner(highs, lows, closes):
    """RSI < 50, alt Keltner kesisi ve ATR < MAX_ATR kosullarini kontrol et."""
    if len(closes) < 20:
        return False
    rsi = _rsi(closes)
    if rsi.size == 0 or np.isnan(rsi[-1]) or rsi[-1] >= 50:
        return False
    upper, lower = _keltner(highs, lows, closes)
    if len(lower) < 2:
        return False
    cross = closes[-2] < lower[-2] and closes[-1] > lower[-1]
    if not cross:
        return False
    atr = _atr(highs, lows, closes)[-1]
    return atr < MAX_ATR


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
        if not self.start_notified:
            send_start_message(mode, self.current_ip, 0)
            self.start_notified = True
        asyncio.create_task(self.monitor_api())
        while True:
            now = datetime.now(timezone.utc)
            wait = (15 - (now.minute % 15)) * 60 - now.second
            await asyncio.sleep(max(wait, 0))
            await self.run()

    async def fetch_symbols(self):
        if not self.top_symbols:
            await self.update_top_symbols()
        return list(self.top_symbols)

    async def select_rsi_keltner(self):
        """Yeni RSI-Keltner stratejisini saglayan ilk sembol."""
        symbols = await self.fetch_symbols()
        for symbol in symbols:
            try:
                limit = 60
                klines = await self.client.get_klines(symbol=symbol, interval="15m", limit=limit)
            except Exception:
                continue
            if len(klines) < limit:
                continue
            highs = [float(k[2]) for k in klines[:-1]]
            lows = [float(k[3]) for k in klines[:-1]]
            closes = [float(k[4]) for k in klines[:-1]]
            if meets_rsi_keltner(highs, lows, closes):
                return symbol, closes[-1]
        return None

    async def is_btc_above_sma25(self) -> bool:
        """BTC 15m SMA25 üzerindeyse True döndür."""
        try:
            m15 = await self.client.get_klines(
                symbol="BTCUSDT", interval="15m", limit=26
            )
            if len(m15) < 26:
                return False
            closes = [float(k[4]) for k in m15[-26:-1]]
            sma = sum(closes[-25:]) / 25
            last_close = float(m15[-2][4])
            return last_close > sma
        except Exception:
            return False

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

    async def select_losers(self):
        """Zarardaki tüm pozisyonları kayıp miktarına göre sırala."""
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
                        #log(f"{symbol} bakiyesi {qty:.8f} minQty {min_qty:.8f} altında, atlandı")
                        return None
                    last_price = float(ticker["price"])
                    if qty * last_price < MIN_LOSER_USDT:
                        #log(f"{symbol} degeri {qty * last_price:.2f} USDT altinda, atlandi")
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

        losers = [r for r in results if r is not None]
        losers.sort(key=lambda x: x[0], reverse=True)
        return [(sym, price, loss) for loss, _percent, sym, price in losers]

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
            #log(f"USDT bakiyesi {MIN_LOSER_USDT} USDT altinda, bekleniyor")
            return
        while candidate:
            symbol, _ = candidate
            now = datetime.now(timezone.utc)
            if not self.loss_check_enabled:
                last = self.last_buy_times.get(symbol)
                if last and now - last < timedelta(hours=2):
                    #log(f"{symbol} son iki saat icinde alindi, atlandi")
                    if symbol in self.top_symbols:
                        self.top_symbols.remove(symbol)
                    candidate = await self.select_rsi_keltner()
                    continue
                last_sell = self.last_sell_times.get(symbol)
                if last_sell and now - last_sell < timedelta(hours=2):
                    #log(f"{symbol} son iki saat icinde satildi, atlandi")
                    if symbol in self.top_symbols:
                        self.top_symbols.remove(symbol)
                    candidate = await self.select_rsi_keltner()
                    continue
            await self.execute_buy(symbol, usdt_amount, check_loss=self.loss_check_enabled)
            break
        if not candidate:
            log("Uygun sembol bulunamadi")

    async def _execute_weighted_losers(self, candidates, usdt_amount: float):
        """Birden fazla zarardaki sembolde orantili alım yap."""
        if not candidates:
            log("Uygun sembol bulunamadi")
            return
        self._cleanup_recent_buys()
        self._cleanup_recent_sells()
        log(f"Serbest USDT bakiyesi: {usdt_amount:.8f}")
        if usdt_amount < MIN_LOSER_USDT:
            #log(f"USDT bakiyesi {MIN_LOSER_USDT} USDT altinda, bekleniyor")
            return
        total_loss = sum(loss for _sym, _price, loss in candidates)
        if total_loss <= 0:
            log("Uygun sembol bulunamadi")
            return
        for symbol, _price, loss in candidates:
            portion = usdt_amount * (loss / total_loss)
            await self.execute_buy(symbol, portion, check_loss=True)

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
            #log(f"USDT bakiyesi {MIN_LOSER_USDT} USDT altinda, tarama atlandi")
            return
        log("Sembol taramasi basladi")
        log("Zarar stratejisi kontrol ediliyor")
        candidates = await self.select_losers()
        self.loss_check_enabled = True
        if not candidates:
            log("BTC SMA25 kontrol ediliyor")
            if await self.is_btc_above_sma25():
                log("RSI-Keltner stratejisi kontrol ediliyor")
                candidate = await self.select_rsi_keltner()
                self.loss_check_enabled = False
            else:
                #log("BTC SMA25 altinda, alım yok")
                candidate = None
            await self._execute_cycle(candidate, usdt * USDT_USAGE_RATIO)
        else:
            await self._execute_weighted_losers(candidates, usdt * USDT_USAGE_RATIO)


async def main():
    client = await AsyncClient.create(API_KEY, API_SECRET, testnet=TESTNET)
    bot = BuyBot(client)
    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())
