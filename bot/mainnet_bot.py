import os

# Bu dosya çalıştırıldığında ortam zorla mainnete ayarlanır
os.environ["BINANCE_TESTNET"] = "false"

from .utils import log, setup_telegram_menu, load_env
load_env()

import asyncio
from binance import AsyncClient

from .buy_bot import BuyBot
from .sell_bot import SellBot, send_telegram, CHECK_INTERVAL
from .telegram_listener import start_listener


API_KEY = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")


async def main():
    log("Mainnet botu baslatiliyor")
    token = os.getenv("TELEGRAM_TOKEN")
    if token:
        setup_telegram_menu(token)
    client = await AsyncClient.create(API_KEY, API_SECRET)
    buy_bot = BuyBot(client)
    sell_bot = SellBot(client)
    start_listener(asyncio.get_running_loop(), sell_bot, buy_bot)

    await sell_bot.start()
    asyncio.create_task(buy_bot.start())
    log("Mainnet botu aktif")

    while True:
        try:
            await sell_bot.check_positions()
        except Exception as exc:
            log(f"API baglanti hatasi: {exc}")
            send_telegram(f"*API baglanti hatasi:* `{exc}`")
        await asyncio.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
