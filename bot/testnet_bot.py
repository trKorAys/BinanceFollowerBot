import os

# Testnet botu çalışırken ortam değişkeni daima true olmalı
os.environ["BINANCE_TESTNET"] = "true"

from dotenv import load_dotenv
load_dotenv()

import asyncio
from binance import AsyncClient

from .buy_bot import BuyBot
from .sell_bot import SellBot, send_telegram, CHECK_INTERVAL
from .telegram_listener import start_listener  # noqa: F401 - testler için içe aktarılıyor
from .utils import log, setup_telegram_menu  # noqa: F401 - testler için içe aktarılıyor

API_KEY = os.getenv("BINANCE_TESTNET_API_KEY")
API_SECRET = os.getenv("BINANCE_TESTNET_API_SECRET")


async def main():
    log("Testnet botu baslatiliyor")
    # Testnet modunda Telegram menüsü ve sohbet botu devre dışı
    token = os.getenv("TELEGRAM_TOKEN")
    # if token:
    #     setup_telegram_menu(token)
    client = await AsyncClient.create(API_KEY, API_SECRET, testnet=True)
    buy_bot = BuyBot(client)
    sell_bot = SellBot(client)
    # start_listener(asyncio.get_running_loop(), sell_bot)

    await sell_bot.start()
    asyncio.create_task(buy_bot.start())
    log("Testnet botu aktif")

    while True:
        wait = CHECK_INTERVAL
        try:
            await sell_bot.check_positions()
        except Exception as exc:
            log(f"API baglanti hatasi: {exc}")
            send_telegram(f"*API baglanti hatasi:* `{exc}`")
            wait = 10
        await asyncio.sleep(wait)


if __name__ == "__main__":
    asyncio.run(main())
