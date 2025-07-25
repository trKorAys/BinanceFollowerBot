from binance import AsyncClient, BinanceSocketManager
import asyncio
import os
import time
import math
import sqlite3
from .utils import (
    FifoTracker,
    get_current_utc_iso,
    log,
    extract_step_size,
    extract_min_qty,
    extract_min_notional,
    floor_to_step,
    seconds_until_next_midnight,
    load_env,
)
from dataclasses import dataclass, field
from typing import Dict, Optional
from datetime import datetime, timezone

import requests
from binance.exceptions import BinanceAPIException
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
FEE_BUY = float(os.getenv("FEE_BUY_PERCENT", "0.1")) / 100
FEE_SELL = float(os.getenv("FEE_SELL_PERCENT", "0.1")) / 100
MIN_PROFIT = float(os.getenv("MIN_PROFIT_PERCENT", "0.5")) / 100
CHECK_INTERVAL = 60 if TESTNET else int(os.getenv("CHECK_INTERVAL", "30"))
RATE_LIMIT_PER_MINUTE = 6000
MIN_FOLLOW_NOTIONAL = float(os.getenv("MIN_FOLLOW_NOTIONAL", "5"))
CANDLE_INTERVAL = os.getenv("CANDLE_INTERVAL", "1m")
TARGET_STEPS = int(os.getenv("TARGET_STEPS", "3"))
GROUP_SIZE = int(os.getenv("GROUP_SIZE", "10"))
CONCURRENCY_LIMIT = int(os.getenv("CONCURRENCY_LIMIT", "5"))
BUY_DB_PATH = os.getenv("BUY_DB_PATH", "buy.db")
STOP_LOSS_ENABLED = os.getenv("STOP_LOSS_ENABLED", "false").lower() == "true"
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
STOP_LOSS_MULTIPLIER = float(os.getenv("STOP_LOSS_MULTIPLIER", "1.0"))

def send_telegram(text: str, chat_id: Optional[str] = None, force: bool = False) -> None:
    """Telegram'a SellBot adıyla Markdown formatında mesaj gönder."""
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
    """Başlangıçta modu, IP'yi ve takip edilen sayıyı Telegram'a gönder."""
    text = (
        f"🟢 *SELL Bot Başladı* (MODE: `{mode}`)\n*IP:* `{ip}`\n*Takip Edilen Sembol Sayısı:* `{count}`"
    )
    send_telegram(text)

@dataclass
class Position:
    tracker: FifoTracker
    min_qty: float
    min_notional: float
    peak: float = 0.0
    passed_steps: list = field(default_factory=list)
    last_price: float = 0.0
    targets_str: str = ""
    extreme_logged: bool = False
    hit_top_target: bool = False

class SellBot:
    def __init__(self, client: AsyncClient):
        self.client = client
        self.positions: Dict[str, Position] = {}
        self.balance_history = []  # list of (date, usdt_value)
        db_path = os.getenv("BALANCE_DB_PATH", "balances.db")
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self._init_db()
        self.buy_db = sqlite3.connect(BUY_DB_PATH, check_same_thread=False)
        self._init_buy_db()
        self.balance_history = self.get_balance_history()
        self.api_down = False
        self.current_ip = None
        self.btc_above_sma7 = False
        self.group_index = 0
        self.start_notified = False
        self.price_socket_task = None

    def _save_recent_sell(self, symbol: str, dt: datetime) -> None:
        self.buy_db.execute(
            "INSERT OR REPLACE INTO recent_sells (symbol, time) VALUES (?, ?)",
            (symbol, dt.isoformat()),
        )
        self.buy_db.commit()

    async def sync_time(self) -> None:
        """Sunucu saat farkını hesaplayıp istemciye uygula."""
        try:
            res = await self.client.get_server_time()
            self.client.timestamp_offset = res["serverTime"] - int(time.time() * 1000)
        except Exception as exc:  # pragma: no cover - API hatası
            log(f"Sunucu saati senkronize edilemedi: {exc}")

    async def check_api(self) -> bool:
        """API bağlantısını kontrol et."""
        try:
            await self.client.ping()
            if self.api_down:
                send_telegram(t("api_recovered"))
                self.api_down = False
            return True
        except Exception as exc:
            log(f"API bağlantısı hatası: {exc}")
            if not self.api_down:
                send_telegram(t("api_error", exc=exc))
                self.api_down = True
            return False

    async def monitor_api(self):
        """API durumunu dakikada bir izle."""
        while True:
            await self.check_api()
            await asyncio.sleep(60)

    async def restart_price_socket(self) -> None:
        """Takip edilen semboller değiştiğinde fiyat akışını yenile."""
        if self.price_socket_task:
            self.price_socket_task.cancel()
            try:
                await self.price_socket_task
            except asyncio.CancelledError:
                pass
        if self.positions:
            self.price_socket_task = asyncio.create_task(
                self.listen_price_socket(self.bsm)
            )
        else:
            self.price_socket_task = None

    async def listen_price_socket(self, bsm: BinanceSocketManager):
        """Tüm semboller için anlık fiyat güncellemelerini dinle."""
        if not self.positions:
            return
        streams = [s.lower() + "@ticker" for s in self.positions.keys()]
        path = "/".join(streams)
        async with bsm._get_socket(path) as stream:
            log("Fiyat websocket bağlandı")
            while True:
                msg = await stream.recv()
                if isinstance(msg, dict):
                    msg = [msg]
                for item in msg:
                    symbol = item.get("s")
                    if symbol in self.positions:
                        price = float(item.get("c", 0))
                        position = self.positions[symbol]
                        await self._check_symbol(symbol, position, price=price)

    async def _check_symbol(
        self, symbol: str, position: Position, price: Optional[float] = None
    ) -> None:
        qty = position.tracker.total_qty()
        if qty < position.min_qty:
            return
        avg_price = position.tracker.average_price()
        try:
            bal = await self.client.get_asset_balance(asset=symbol.replace("USDT", ""))
            wallet_qty = float(bal.get("free", 0)) + float(bal.get("locked", 0))
        except Exception:
            wallet_qty = qty
        if wallet_qty + 1e-8 < qty:
            position.tracker.sell(qty - wallet_qty)
            qty = wallet_qty
            if qty < position.min_qty:
                self.positions.pop(symbol, None)
                log(f"{symbol} manuel satış tespit edildi, takipten çıkarıldı")
                await self.restart_price_socket()
                return
        if price is None:
            try:
                ticker = await self.client.get_symbol_ticker(symbol=symbol)
                last_price = float(ticker["price"])
            except Exception as exc:
                log(f"API bağlantı hatası: {exc}")
                send_telegram(t("api_error", exc=exc))
                return
        else:
            last_price = price
        if last_price > position.peak:
            position.peak = last_price
        profit = (last_price - avg_price) * qty
        percent = (last_price - avg_price) / avg_price * 100 if avg_price else 0
        if abs(last_price - position.last_price) > 1e-8:
            log(
                f"{symbol}: Fiyat={last_price:.8f}, Hedefler={position.targets_str}, Ortalama Alış Fiyatı={avg_price:.8f}, Kar={profit:.4f} ({percent:.2f}%)"
            )
        position.last_price = last_price
        if qty * last_price < position.min_notional:
            return
        try:
            should = await self.should_sell(symbol, last_price, avg_price)
        except Exception as exc:
            log(f"API bağlantı hatası: {exc}")
            send_telegram(t("api_error", exc=exc))
            return
        if should:
            try:
                await self.execute_sell(symbol, qty)
            except Exception as exc:
                log(f"API bağlantı hatası: {exc}")
                send_telegram(t("api_error", exc=exc))

    async def monitor_btc_sma(self):
        """BTC fiyatının SMA7 üzerindeki durumunu 15 dakikada bir güncelle."""
        while True:
            self.btc_above_sma7 = await self.is_btc_above_sma7()
            now = datetime.now(timezone.utc)
            wait = (15 - (now.minute % 15)) * 60 - now.second
            await asyncio.sleep(max(wait, 0))

    async def check_new_balances(self) -> None:
        """Cüzdanda bulunan yeni sembolleri tarayıp takibe ekle."""
        try:
            account = await self.client.get_account()
        except BinanceAPIException as exc:
            log(f"Yeni bakiye alınamadı: {exc}")
            return

        balances = account.get("balances", [])
        old = set(self.positions.keys())

        sem = asyncio.Semaphore(CONCURRENCY_LIMIT)

        async def scan_symbol(bal):
            async with sem:
                free = float(bal.get("free", 0))
                locked = float(bal.get("locked", 0))
                qty = free + locked
                if qty <= 0:
                    return
                asset = bal.get("asset")
                if asset in ("USDT", "BUSD"):
                    return
                symbol = f"{asset}USDT"
                if symbol in self.positions:
                    return
                try:
                    info = await self.client.get_symbol_info(symbol)
                except BinanceAPIException:
                    return
                if not info:
                    return
                min_qty = extract_min_qty(info)
                if qty < min_qty:
                    return

                trades = await self.fetch_all_trades(symbol)
                tracker = FifoTracker()
                trades = sorted(trades, key=lambda x: x.get("time", 0))
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

                try:
                    ticker = await self.client.get_symbol_ticker(symbol=symbol)
                    last_price = float(ticker["price"])
                except (BinanceAPIException, AttributeError):
                    last_price = tracker.average_price()

                current_qty = tracker.total_qty()
                if abs(current_qty - qty) > 1e-8:
                    if current_qty > qty:
                        tracker.sell(current_qty - qty)
                    else:
                        add_price = (
                            tracker.average_price() if tracker.total_qty() > 0 else 1e-7
                        )
                        tracker.add_trade(qty - current_qty, add_price)

                min_notional = max(extract_min_notional(info), MIN_FOLLOW_NOTIONAL)

                if tracker.total_qty() < min_qty or tracker.total_qty() * last_price < min_notional:
                    return

                self.positions[symbol] = Position(tracker, min_qty, min_notional)
                avg = tracker.average_price()
                log(
                    f"{symbol} bakiyesi bulundu: miktar={tracker.total_qty():.8f}, ortalama={avg:.8f}"
                )
                profit = (last_price - avg) * tracker.total_qty()
                percent = (last_price - avg) / avg * 100 if avg else 0.0
                send_telegram(
                    f"📌 *{symbol} takibe alindi.*\n"
                    f"💲 *Ortalama Fiyat:* `{avg:.8f}`\n"
                    f"💱 *Mevcut Fiyat:* `{last_price:.8f}`\n"
                    f"⏳ *Kar:* `{profit:.4f}` ({percent:.2f}%)"
                )

        tasks = [scan_symbol(b) for b in balances]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for err in results:
            if isinstance(err, Exception):
                log(f"check_new_balances hatası: {err}")
                send_telegram(t("api_error", exc=err))
                self.api_down = True

        if set(self.positions.keys()) != old:
            await self.restart_price_socket()


    async def fetch_all_trades(self, symbol: str):
        """Tüm geçmiş işlemleri baştan sona sayfalayarak getir."""
        all_trades = []
        # fromId belirtilmezse API sadece son 1000 işlemi döndürür.
        # Bu yüzden 0'dan başlayarak tüm işlemleri çekiyoruz.
        from_id = 0
        while True:
            params = {"symbol": symbol, "limit": 1000}
            # İlk çağrı da dahil olmak üzere fromId parametresini gönder
            if from_id is not None:
                params["fromId"] = from_id
            try:
                trades = await self.client.get_my_trades(**params)
            except BinanceAPIException:
                break
            if not trades:
                break
            all_trades.extend(trades)
            if len(trades) < 1000:
                break
            from_id = trades[-1].get("id", 0) + 1
            await asyncio.sleep(0.2)
        return all_trades

    async def get_total_usdt_value(self) -> float:
        """Tüm bakiyenin USDT karşılığını hesapla."""
        try:
            account = await self.client.get_account()
        except BinanceAPIException as exc:
            log(f"Bakiye alınamadı: {exc}")
            return 0.0
        total = 0.0
        for bal in account.get("balances", []):
            free = float(bal.get("free", 0))
            locked = float(bal.get("locked", 0))
            qty = free + locked
            if qty <= 0:
                continue
            asset = bal.get("asset")
            if asset.upper() in ("USDT", "BUSD", "USDC", "TUSD", "USDP", "DAI"):
                total += qty
                continue
            symbol = f"{asset}USDT"
            try:
                info = await self.client.get_symbol_info(symbol)
                min_qty = extract_min_qty(info)
                if qty < min_qty:
                    continue
                ticker = await self.client.get_symbol_ticker(symbol=symbol)
                price = float(ticker["price"])
                total += qty * price
            except Exception:
                continue
        return total

    def _init_db(self) -> None:
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS balance_history (date TEXT PRIMARY KEY, value REAL)"
        )
        self.db.commit()

    def _init_buy_db(self) -> None:
        self.buy_db.execute(
            "CREATE TABLE IF NOT EXISTS recent_sells (symbol TEXT PRIMARY KEY, time TEXT)"
        )
        self.buy_db.commit()

    def save_daily_balance(self, date_str: str, value: float) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO balance_history (date, value) VALUES (?, ?)",
            (date_str, value),
        )
        self.db.commit()

    def get_balance_history(self, limit: int = 5):
        cur = self.db.execute(
            "SELECT date, value FROM balance_history ORDER BY date DESC LIMIT ?",
            (limit,),
        )
        rows = cur.fetchall()
        return rows[::-1]

    async def daily_balance_loop(self):
        """Her gün UTC-0'a göre gün sonunda bakiye raporu gönder."""
        while True:
            wait = seconds_until_next_midnight()
            await asyncio.sleep(wait)
            value = await self.get_total_usdt_value()
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            self.save_daily_balance(date_str, value)
            self.balance_history = self.get_balance_history()
            lines = [f"💹 {d}: {v:.2f} USDT" for d, v in self.balance_history]
            diff = None
            if len(self.balance_history) >= 2:
                diff = self.balance_history[-1][1] - self.balance_history[-2][1]
                lines.append(f"Bugün Değişim: {diff:+.2f} USDT")
            msg = "❤️ *Gun Sonu Bakiyeleri:*\n" + "\n".join(lines)
            send_telegram(msg)
            if diff is not None and diff > 0:
                donation = diff * 0.1
                send_telegram(
                    "❤️ *Kazançlı olduğuna sevindik, eğer bize destek olmak* "
                    f"*isterseniz kazancınızın %10'u olan* 💹`{donation:.2f}`💲 *USDT'yi* "
                    "*USDT adresimiz:* `THz1ssvnpVcmt9Kk24x4wD5XCMZBtnubnE` *adresine* "
                    "*gönderebilirsiniz. *‼* Lütfen daha fazlasını göndermeyin*‼"
                )

    async def start(self):
        mode = "TESTNET" if TESTNET else "LIVE"
        log(f"SellBot baslatiliyor. MODE: {mode}")
        self.current_ip = get_public_ip()
        await self.load_balances()
        await self.sync_time()
        await self.check_api()
        self.btc_above_sma7 = await self.is_btc_above_sma7()
        if self.positions:
            symbols = "\n🔜 ".join(self.positions.keys())
            send_telegram(f"📌 *Takip edilen semboller:*\n🔜 {symbols}")
        if not self.start_notified:
            send_start_message(mode, self.current_ip, len(self.positions))
            self.start_notified = True
        log("Websocket bağlantısı kuruluyor")
        bsm = BinanceSocketManager(self.client)
        self.bsm = bsm  # Bunu sınıf içinde tutabilirsin istersen
        asyncio.create_task(self.listen_user_socket(bsm))
        asyncio.create_task(self.daily_balance_loop())
        asyncio.create_task(self.monitor_api())
        asyncio.create_task(self.monitor_btc_sma())
        self.price_socket_task = asyncio.create_task(
            self.listen_price_socket(bsm)
        )
        log("Kullanıcı websocket dinlemesi başladı")

    async def load_balances(self):
        """Başlangıçta mevcut bakiyeleri pozisyonlara ekle."""
        try:
            account = await self.client.get_account()
        except BinanceAPIException as exc:
            log(f"Bakiye alınamadı: {exc}")
            return

        balances = account.get("balances", [])

        sem = asyncio.Semaphore(CONCURRENCY_LIMIT)

        async def load_symbol(bal):
            async with sem:
                free = float(bal.get("free", 0))
                locked = float(bal.get("locked", 0))
                qty = free + locked
                if qty <= 0:
                    return
                asset = bal.get("asset")
                if asset in ("USDT", "BUSD"):
                    return
                symbol = f"{asset}USDT"
                try:
                    info = await self.client.get_symbol_info(symbol)
                except BinanceAPIException:
                    return
                if not info:
                    return
                min_qty = extract_min_qty(info)
                if qty < min_qty:
                    log(
                        f"{symbol} bakiyesi {qty:.8f} minQty {min_qty:.8f} altinda"
                    )
                    return

                trades = await self.fetch_all_trades(symbol)
                tracker = FifoTracker()
                trades = sorted(trades, key=lambda x: x.get("time", 0))
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

                try:
                    ticker = await self.client.get_symbol_ticker(symbol=symbol)
                    last_price = float(ticker["price"])
                except (BinanceAPIException, AttributeError):
                    last_price = tracker.average_price()

                current_qty = tracker.total_qty()
                # Cüzdandaki gerçek miktar ile işlem geçmişinden hesaplanan miktar uyuşmuyorsa
                if abs(current_qty - qty) > 1e-8:
                    if current_qty > qty:
                        # Eksik satışlar varsa fazla miktarı düş
                        tracker.sell(current_qty - qty)
                    else:
                        # İşlem geçmişinde olmayan bir alım varsa yaklaşık son fiyattan ekle
                        add_price = tracker.average_price() if tracker.total_qty() > 0 else 1e-7
                        tracker.add_trade(qty - current_qty, add_price)

                min_notional = max(extract_min_notional(info), MIN_FOLLOW_NOTIONAL)

                if tracker.total_qty() < min_qty or tracker.total_qty() * last_price < min_notional:
                    log(
                        f"{symbol} miktar {tracker.total_qty():.8f} veya notional {tracker.total_qty() * last_price:.8f} takip sınırının altında"
                    )
                    return

                self.positions[symbol] = Position(tracker, min_qty, min_notional)
                avg = tracker.average_price()
                log(f"{symbol} bakiyesi yüklendi: miktar={tracker.total_qty():.8f}, ortalama={avg:.8f}")
                profit = (last_price - avg) * tracker.total_qty()
                percent = (last_price - avg) / avg * 100 if avg else 0.0
                send_telegram(
                    f"📌 *{symbol} takibe alindi.*\n"
                    f"💲 *Ortalama Fiyat:* `{avg:.8f}`\n"
                    f"💱 *Mevcut Fiyat:* `{last_price:.8f}`\n"
                    f"⏳ *Kar:* `{profit:.4f}` ({percent:.2f}%)"
                )
                if TESTNET and avg <= 1e-7:
                    log(f"{symbol} alış fiyatı 0, testnet satış başlatılıyor")
                    await self.execute_sell(symbol, tracker.total_qty(), notify=False)
                    return

        if TESTNET:
            tasks = [load_symbol(b) for b in balances]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for err in results:
                if isinstance(err, Exception):
                    log(f"load_symbol hatası: {err}")
                    send_telegram(t("api_error", exc=err))
                    self.api_down = True
        else:
            tasks = [load_symbol(b) for b in balances]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for err in results:
                if isinstance(err, Exception):
                    log(f"load_symbol hatası: {err}")
                    send_telegram(t("api_error", exc=err))
                    self.api_down = True

    async def listen_user_socket(self, bsm: BinanceSocketManager):
        async with bsm.user_socket() as stream:
            log("Kullanıcı websocket bağlandı")
            while True:
                msg = await stream.recv()
                await self.handle_msg(msg)

    async def handle_msg(self, msg):
        if msg.get("e") != "executionReport":
            return
        symbol = msg["s"]
        status = msg["X"]
        side = msg["S"]
        qty = float(msg.get("z", msg.get("l", 0)))
        price = float(msg.get("L", 0))
        commission = float(msg.get("n", 0))
        comm_asset = msg.get("N")
        log(f"Mesaj alindi: {side} {symbol} {qty} {status}")
        if side == "BUY" and status == "FILLED":
            await self.add_buy(symbol, qty, price, commission, comm_asset)
        elif side == "SELL" and status == "FILLED":
            before = symbol in self.positions
            await self.remove_qty(symbol, qty, commission, comm_asset)
            if before and symbol not in self.positions:
                await self.restart_price_socket()

    async def add_buy(self, symbol: str, qty: float, price: float, commission: float = 0.0, commission_asset: Optional[str] = None):
        info = await self.client.get_symbol_info(symbol)
        min_qty = extract_min_qty(info)
        min_notional = max(extract_min_notional(info), MIN_FOLLOW_NOTIONAL)
        is_new = symbol not in self.positions
        if is_new:
            self.positions[symbol] = Position(FifoTracker(), min_qty, min_notional)
        else:
            self.positions[symbol].min_qty = min_qty
            self.positions[symbol].min_notional = min_notional
        if commission_asset and commission_asset == symbol.replace("USDT", ""):
            qty -= commission
        tracker = self.positions[symbol].tracker
        prev_qty = tracker.total_qty()
        tracker.add_trade(qty, price)
        avg = tracker.average_price()
        log(f"{symbol} alımı: miktar={qty:.8f}, fiyat={price:.8f}, ortalama={avg:.8f}")
        send_telegram(
            f"✔ *{symbol} ALINDI*\n"
            f"💹 *Miktar:* `{qty:.8f}`\n"
            f"💸 *Ortalama Fiyat:* `{avg:.8f}`"
        )
        total_qty = tracker.total_qty()
        try:
            ticker = await self.client.get_symbol_ticker(symbol=symbol)
            last_price = float(ticker["price"])
        except BinanceAPIException:
            last_price = price
        if total_qty < min_qty or total_qty * last_price < min_notional:
            self.positions.pop(symbol, None)
            return
        if prev_qty < min_qty or prev_qty * last_price < min_notional:
            profit = (last_price - avg) * total_qty
            percent = (last_price - avg) / avg * 100 if avg else 0.0
            send_telegram(
                f"📌 *{symbol} takibe alindi.*\n"
                f"💸 *Ortalama Alis Fiyati:* `{avg:.8f}`\n"
                f"💸 *Mevcut Fiyat:* `{last_price:.8f}`\n"
                f"⏳ *Kar:* `{profit:.4f}` ({percent:.2f}%)"
            )
        if is_new:
            await self.restart_price_socket()

    async def remove_qty(
        self, symbol: str, qty: float, commission: float = 0.0, commission_asset: Optional[str] = None
    ):
        if symbol not in self.positions:
            return
        tracker = self.positions[symbol].tracker
        sell_qty = qty
        if commission_asset and commission_asset == symbol.replace("USDT", ""):
            sell_qty += commission
        tracker.sell(sell_qty)
        total = tracker.total_qty()
        if total < self.positions[symbol].min_qty:
            self.positions.pop(symbol, None)
            await self.restart_price_socket()

    async def check_positions(self):
        if self.api_down:
            log("API hatası devam ediyor, kontrol atlandı")
            return
        await self.sync_time()
        items = list(self.positions.items())
        if not items:
            return

        group_size = GROUP_SIZE
        if group_size <= 0:
            ops_per_min = len(items) * (60 / CHECK_INTERVAL)
            if ops_per_min > RATE_LIMIT_PER_MINUTE:
                group_size = max(1, int(RATE_LIMIT_PER_MINUTE * CHECK_INTERVAL / 60))
        if group_size > 0:
            total_groups = math.ceil(len(items) / group_size)
            start = self.group_index * group_size
            items = items[start : start + group_size]
            self.group_index = (self.group_index + 1) % total_groups
        tasks = [self._check_symbol(sym, pos) for sym, pos in items]
        if tasks:
            await asyncio.gather(*tasks)

    async def get_volatility(self, symbol: str) -> float:
        """Son kapanmis mumun yuzde degisimini pozitif olarak dondur."""
        try:
            klines = await self.client.get_klines(
                symbol=symbol, interval=CANDLE_INTERVAL, limit=2
            )
            if len(klines) < 2:
                return 0.0
            kline = klines[-2]
            open_p = float(kline[1])
            close_p = float(kline[4])
            return abs(close_p - open_p) / open_p
        except Exception:
            return 0.0

    async def calculate_atr(self, symbol: str) -> float:
        """Verilen sembol icin ATR (Average True Range) hesapla."""
        try:
            limit = ATR_PERIOD + 1
            klines = await self.client.get_klines(
                symbol=symbol, interval=CANDLE_INTERVAL, limit=limit
            )
            if len(klines) < limit:
                return 0.0
            prev_close = float(klines[0][4])
            trs = []
            for k in klines[1:]:
                high = float(k[2])
                low = float(k[3])
                close = float(k[4])
                tr = max(high - low, abs(high - prev_close), abs(prev_close - low))
                trs.append(tr)
                prev_close = close
            return sum(trs) / len(trs) if trs else 0.0
        except Exception:
            return 0.0

    async def is_btc_above_sma7(self) -> bool:
        """BTC fiyatinin 7 gunluk SMA uzerinde olup olmadigini kontrol et."""
        try:
            klines = await self.client.get_klines(symbol="BTCUSDT", interval="1d", limit=7)
            if len(klines) < 7:
                return False
            closes = [float(k[4]) for k in klines]
            sma = sum(closes) / 7
            ticker = await self.client.get_symbol_ticker(symbol="BTCUSDT")
            price = float(ticker["price"])
            return price > sma
        except Exception:
            return False

    async def get_last_open_price(self, symbol: str) -> Optional[float]:
        """Son kapanmış mumun açılış fiyatını döndür."""
        try:
            klines = await self.client.get_klines(symbol=symbol, interval=CANDLE_INTERVAL, limit=2)
            if len(klines) < 2:
                return None
            return float(klines[-2][1])
        except Exception:
            return None

    async def should_sell(self, symbol: str, last_price: float, avg_price: float) -> bool:
        vol = await self.get_volatility(symbol)
        base = FEE_BUY + FEE_SELL + MIN_PROFIT
        pos = self.positions.get(symbol)
        if pos is None:
            return False
        if avg_price == 0:
            log(f"{symbol} ortalama fiyat sifir, satis onceligi")
            return True
        if STOP_LOSS_ENABLED:
            atr = await self.calculate_atr(symbol)
            stop_price = avg_price - atr * STOP_LOSS_MULTIPLIER
            if atr > 0 and last_price <= stop_price:
                log(
                    f"{symbol} stop-loss seviyesi {stop_price:.8f} altinda, satis yapilacak"
                )
                return True
        steps = [base]
        if self.btc_above_sma7 and vol > base:
            steps.extend(
                base + (vol - base) * i / TARGET_STEPS for i in range(1, TARGET_STEPS + 1)
            )
        steps = sorted(set(steps))
        open_price = await self.get_last_open_price(symbol)
        base_price = avg_price
        if open_price and open_price > avg_price:
            base_price = open_price
        targets = [base_price * (1 + s) for s in steps]
        target_str = ", ".join(f"{t:.8f}" for t in targets)
        if target_str != pos.targets_str:
            log(f"{symbol} hedef fiyatlar güncellendi: {target_str}")
            pos.targets_str = target_str
        profit_ratio = (last_price - base_price) / base_price if base_price else 0
        extreme_step = steps[-1]
        if profit_ratio >= extreme_step * 5:
            if not pos.extreme_logged:
                log(
                    f"{symbol} kar hedefin 5 katini asti, hacim onaysiz satilacak"
                )
                pos.extreme_logged = True
            return True
        # Ara hedefler gecilip geri donulurse direkt satis yap
        for step, target in zip(steps[:-1], targets[:-1]):
            if pos.peak >= target > last_price and step not in pos.passed_steps:
                log(f"{symbol} {target:.8f} seviyesinin altina dustu, satis yapilacak")
                return True
            if pos.peak < target:
                break

        last_target = targets[-1]
        if last_price >= last_target:
            trades = await self.client.get_recent_trades(symbol=symbol, limit=60)
            buy_vol = sum(float(t["qty"]) for t in trades if not t["isBuyerMaker"])
            sell_vol = sum(float(t["qty"]) for t in trades if t["isBuyerMaker"])
            decision = sell_vol > buy_vol
            log(
                f"{symbol} hacim kontrolu: satis={sell_vol:.4f}, alim={buy_vol:.4f}, karar={decision}"
            )
            pos.hit_top_target = True
            if decision:
                return True
        elif pos.hit_top_target and last_price < last_target:
            log(f"{symbol} en yuksek hedef altina dustu, satis yapilacak")
            return True
        return False

    async def execute_sell(self, symbol: str, qty: float, notify: bool = True):
        info = await self.client.get_symbol_info(symbol)
        step = extract_step_size(info)
        min_q = extract_min_qty(info)
        asset = symbol.replace("USDT", "")
        try:
            bal = await self.client.get_asset_balance(asset=asset)
            wallet_qty = float(bal.get("free", 0)) + float(bal.get("locked", 0))
        except Exception:  # pragma: no cover - API hatası
            wallet_qty = qty
        qty = floor_to_step(wallet_qty, min_q)
        qty = floor_to_step(qty, step)
        if qty < step or qty < self.positions[symbol].min_qty:
            self.positions.pop(symbol, None)
            await self.restart_price_socket()
            log(f"{symbol} bakiyesi yetersiz, takipten çıkarıldı")
            return
        try:
            order = await self.client.create_order(
                symbol=symbol, side="SELL", type="MARKET", quantity=qty
            )
            price = float(order["fills"][0]["price"])
            avg_price = self.positions[symbol].tracker.average_price()
            profit = (price - avg_price) * qty
            percent = (price - avg_price) / avg_price * 100
            log(
                f"{symbol} satildi: Fiyat={price:.8f}, Kar={profit:.4f} ({percent:.2f}%)"
            )
            if notify:
                text = (
                    f"💰 *{symbol} SATILDI*\n"
                    f"💹 *Alış:* `{avg_price:.8f}`\n"
                    f"💹 *Satış:* `{price:.8f}`\n"
                    f"❤️ *Kar:* `{profit:.4f}` ({percent:.2f}%)"
                )
                force_send = avg_price > 1e-7
                send_telegram(text, force=force_send)
            now_dt = datetime.now(timezone.utc)
            self._save_recent_sell(symbol, now_dt)
            self.positions.pop(symbol, None)
            await self.restart_price_socket()
        except BinanceAPIException as exc:
            log(f"{symbol} satış hatası: {exc}")
            send_telegram(t("sell_error", exc=exc))

async def main():
    log("Bot başlatılıyor")
    client = await AsyncClient.create(API_KEY, API_SECRET, testnet=TESTNET)
    log("Binance istemcisi oluşturuldu")
    watcher = SellBot(client)
    await watcher.start()
    log("SellBot aktif")
    while True:
        try:
            await watcher.check_positions()
        except Exception as exc:
            log(f"API bağlantı hatası: {exc}")
            send_telegram(t("api_error", exc=exc))
        await asyncio.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    asyncio.run(main())
