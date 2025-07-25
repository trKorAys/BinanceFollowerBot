from datetime import datetime, timezone, timedelta
from typing import Optional
import os
import requests
from builtins import print as builtin_print
print = builtin_print  # testlerde kolayca yamanabilmesi icin
try:
    from zoneinfo import ZoneInfo  # Python 3.9+
except ImportError:  # pragma: no cover - geri uyumluluk
    from backports.zoneinfo import ZoneInfo


def get_current_utc_iso() -> str:
    """UTC-0 zaman bilgisini ISO 8601 formatında döndür."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def convert_utc_to_local(utc_string: str) -> str:
    """Verilen ISO 8601 UTC zamanını yerel saate dönüştürüp yine ISO 8601 döndür."""
    dt = datetime.strptime(utc_string, "%Y-%m-%dT%H:%M:%SZ")
    return dt.astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def convert_utc_to_timezone(utc_string: str, tz_name: str) -> str:
    """Verilen ISO 8601 UTC zamanını belirtilen saat dilimine çevir."""
    dt = datetime.strptime(utc_string, "%Y-%m-%dT%H:%M:%SZ")
    tz = ZoneInfo(tz_name)
    return dt.replace(tzinfo=timezone.utc).astimezone(tz).strftime("%Y-%m-%dT%H:%M:%S%z")


def convert_utc_to_env_timezone(utc_string: str) -> str:
    """`LOCAL_TIMEZONE` degiskeni ayarliysa UTC degerini o dilime cevir."""
    tz_name = os.getenv("LOCAL_TIMEZONE")
    if tz_name:
        return convert_utc_to_timezone(utc_string, tz_name)
    return convert_utc_to_local(utc_string)


def log(message: str) -> None:
    """Mesaji yerel saat dilimiyle ekrana yazdir."""
    utc = get_current_utc_iso()
    print(f"[{convert_utc_to_env_timezone(utc)}] {message}")


from decimal import Decimal, getcontext


class FifoTracker:
    """Alım işlemlerini FIFO mantığıyla izleyip ortalama maliyet hesaplar."""

    def __init__(self):
        # Yüksek hassasiyet sağlamak adına Decimal kullanıyoruz
        getcontext().prec = 28
        self.trades = []  # list of [Decimal quantity, Decimal price]

    def add_trade(self, qty: float, price: float) -> None:
        """Alım işlemini listeye ekle.

        Bazı durumlarda işlemlerin fiyatı ``0`` olarak gelebiliyor. Bu durumda
        fiyatı sabit minimum değer olan ``0.0000001`` olarak kabul ederiz.
        """
        if price == 0:
            price = 1e-7
        self.trades.append([Decimal(str(qty)), Decimal(str(price))])

    def sell(self, qty: float) -> None:
        """Satış miktarını FIFO mantığıyla düş."""
        qty = Decimal(str(qty))
        while qty > 0 and self.trades:
            first_qty, price = self.trades[0]
            if first_qty > qty:
                self.trades[0][0] = first_qty - qty
                qty = Decimal("0")
            else:
                qty -= first_qty
                self.trades.pop(0)

    def average_price(self) -> float:
        total_qty = sum(q for q, _ in self.trades)
        if total_qty == 0:
            return 0.0
        total_cost = sum(q * p for q, p in self.trades)
        return float(total_cost / total_qty)

    def total_qty(self) -> float:
        return float(sum(q for q, _ in self.trades))


def extract_step_size(info: dict) -> float:
    """Sembol bilgisinden adım miktarını güvenli şekilde çıkar."""
    filters = info.get("filters", [])
    for f in filters:
        if f.get("filterType") == "LOT_SIZE" and "stepSize" in f:
            return float(f["stepSize"])
    for f in filters:
        if "stepSize" in f:
            return float(f["stepSize"])
    return 1.0


def extract_min_qty(info: dict) -> float:
    """Sembol bilgisinden minimum miktarı güvenli şekilde çıkar."""
    filters = info.get("filters", [])
    for f in filters:
        if f.get("filterType") == "LOT_SIZE" and "minQty" in f:
            return float(f["minQty"])
    for f in filters:
        if "minQty" in f:
            return float(f["minQty"])
    return 0.0


def extract_min_notional(info: dict) -> float:
    """Sembol bilgisinden minimum notional değerini güvenli şekilde çıkar."""
    filters = info.get("filters", [])
    for f in filters:
        if f.get("filterType") == "MIN_NOTIONAL" and "minNotional" in f:
            return float(f["minNotional"])
    for f in filters:
        if "minNotional" in f:
            return float(f["minNotional"])
    return 0.0


def floor_to_step(value: float, step: float) -> float:
    """Verilen değeri adıma gore aşağı yuvarla."""
    if step == 0:
        return value
    d = Decimal(str(value))
    step_d = Decimal(str(step))
    return float((d // step_d) * step_d)


def floor_to_precision(value: float, precision: int) -> float:
    """Verilen değeri ondalık basamak sayısına göre aşağı yuvarla."""
    step = 10 ** (-precision)
    return floor_to_step(value, step)


def seconds_until_next_midnight(now: Optional[datetime] = None) -> float:
    """UTC-0 bir sonraki gün başlangıcına kadar olan saniye sayısını döndür."""
    now = now or datetime.now(timezone.utc)
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return (tomorrow - now).total_seconds()


def seconds_until_next_six_hour(now: Optional[datetime] = None) -> float:
    """UTC-0 saatine göre bir sonraki 6 saatlik dilime kadar olan saniye."""
    now = now or datetime.now(timezone.utc)
    next_hour = ((now.hour // 6) + 1) * 6
    if next_hour >= 24:
        next_time = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        next_time = now.replace(hour=next_hour, minute=0, second=0, microsecond=0)
    return (next_time - now).total_seconds()


def setup_telegram_menu(token: str) -> None:
    """Telegram botunda komut menüsünü ayarla."""
    url = f"https://api.telegram.org/bot{token}/setMyCommands"
    commands = [
        {"command": "start", "description": "Botu başlat"},
        {"command": "summary", "description": "Günlük bakiye özeti"},
        {"command": "help", "description": "Komut listesini göster"},
    ]
    try:
        requests.post(url, json={"commands": commands}, timeout=10)
    except Exception:  # pragma: no cover - ağ hatası
        log("Telegram komutlari ayarlanamadi")
