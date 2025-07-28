import os
import asyncio
from threading import Thread
from telegram.ext import Updater, CommandHandler

from .sell_bot import send_telegram
from .utils import log
from .messages import t



def _format_position(symbol, position, price):
    avg = position.tracker.average_price()
    profit = (price - avg) * position.tracker.total_qty()
    percent = (price - avg) / avg * 100 if avg else 0.0
    return (
        f"📌 *{symbol}*\n"
        f"💸 *Ortalama:* `{avg:.8f}`\n"
        f"💱 *Fiyat:* `{price:.8f}`\n"
        f"⏳ *Kar:* `{profit:.4f}` ({percent:.2f}%)"
    )

ALLOWED_IDS = [
    cid.strip()
    for cid in os.getenv("TELEGRAM_CHAT_ID", "").split(",")
    if cid.strip()
]


def _authorized(chat_id: str) -> bool:
    return not ALLOWED_IDS or chat_id in ALLOWED_IDS


def start_listener(loop: asyncio.AbstractEventLoop, sell_bot=None, buy_bot=None) -> None:
    """Telegram bot komutlarini dinle."""
    telegram_enabled = os.getenv("TELEGRAM_ENABLED", "true").lower() == "true"
    if not telegram_enabled:
        log("Telegram devre disi, listener baslatilmadi")
        return
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        log("TELEGRAM_TOKEN tanimsiz, listener baslatilmadi")
        return

    updater = Updater(token, use_context=True)
    dispatcher = updater.dispatcher

    def cmd_start(update, context):
        chat_id = str(update.effective_chat.id)
        if not _authorized(chat_id):
            send_telegram(t("unauthorized"), chat_id=chat_id)
            return
        send_telegram(
            t("start_info"),
            chat_id=chat_id,
        )

    def cmd_help(update, context):
        chat_id = str(update.effective_chat.id)
        if not _authorized(chat_id):
            send_telegram(t("unauthorized"), chat_id=chat_id)
            return
        send_telegram(t("commands"), chat_id=chat_id)

    def cmd_summary(update, context):
        chat_id = str(update.effective_chat.id)
        if not _authorized(chat_id):
            send_telegram(t("unauthorized"), chat_id=chat_id)
            return
        if sell_bot is None:
            send_telegram(t("summary_unavailable"), chat_id=chat_id)
            return
        future = asyncio.run_coroutine_threadsafe(sell_bot.get_total_usdt_value(), loop)
        value = future.result()
        send_telegram(f"Guncel toplam deger: {value:.2f} USDT", chat_id=chat_id)

    def cmd_report(update, context):
        chat_id = str(update.effective_chat.id)
        if not _authorized(chat_id):
            send_telegram(t("unauthorized"), chat_id=chat_id)
            return
        if sell_bot is None:
            send_telegram(t("report_unavailable"), chat_id=chat_id)
            return
        history = sell_bot.get_balance_history()
        if not history:
            send_telegram(t("no_report"), chat_id=chat_id)
            return
        lines = [f"{d}: {v:.2f} USDT" for d, v in history]
        msg = "\n".join(lines)
        send_telegram(t("report_header", msg=msg), chat_id=chat_id)

    def cmd_balances(update, context):
        chat_id = str(update.effective_chat.id)
        if not _authorized(chat_id):
            send_telegram(t("unauthorized"), chat_id=chat_id)
            return
        if sell_bot is None or not sell_bot.positions:
            send_telegram(t("no_balance_symbols"), chat_id=chat_id)
            return
        symbols = "\n".join(sell_bot.positions.keys())
        send_telegram(t("balances", symbols=symbols), chat_id=chat_id)

    def cmd_positions(update, context):
        chat_id = str(update.effective_chat.id)
        if not _authorized(chat_id):
            send_telegram(t("unauthorized"), chat_id=chat_id)
            return
        if sell_bot is None or not sell_bot.positions:
            send_telegram(t("no_tracked_symbols"), chat_id=chat_id)
            return
        lines = []
        for sym, pos in sell_bot.positions.items():
            future = asyncio.run_coroutine_threadsafe(
                sell_bot.client.get_symbol_ticker(symbol=sym), loop
            )
            try:
                ticker = future.result()
                price = float(ticker["price"])
            except Exception:
                price = pos.tracker.average_price()
            lines.append(_format_position(sym, pos, price))
        send_telegram("\n\n".join(lines), chat_id=chat_id)

    def cmd_sell(update, context):
        chat_id = str(update.effective_chat.id)
        if not _authorized(chat_id):
            send_telegram(t("unauthorized"), chat_id=chat_id)
            return
        if sell_bot is None:
            send_telegram(t("sell_unavailable"), chat_id=chat_id)
            return
        args = context.args
        if not args:
            send_telegram(t("usage_sell"), chat_id=chat_id)
            return
        symbol = args[0].upper().replace("USDT", "") + "USDT"
        position = sell_bot.positions.get(symbol)
        if not position:
            send_telegram(t("no_symbol_balance", symbol=symbol), chat_id=chat_id)
            return
        qty = position.tracker.total_qty()
        future = asyncio.run_coroutine_threadsafe(sell_bot.execute_sell(symbol, qty), loop)
        try:
            future.result()
        except Exception as exc:  # pragma: no cover - ağ hatası
            send_telegram(t("sell_error", exc=exc), chat_id=chat_id)
        else:
            send_telegram(t("sell_sent", symbol=symbol), chat_id=chat_id)

    def cmd_price(update, context):
        chat_id = str(update.effective_chat.id)
        if not _authorized(chat_id):
            send_telegram(t("unauthorized"), chat_id=chat_id)
            return
        args = context.args
        if not args:
            send_telegram(t("price_usage"), chat_id=chat_id)
            return
        symbol = args[0].upper().replace("USDT", "") + "USDT"
        if sell_bot is None:
            send_telegram(t("price_unavailable"), chat_id=chat_id)
            return
        future = asyncio.run_coroutine_threadsafe(
            sell_bot.client.get_symbol_ticker(symbol=symbol), loop
        )
        try:
            ticker = future.result()
            price = float(ticker["price"])
            send_telegram(t("price_value", symbol=symbol, price=price), chat_id=chat_id)
        except Exception as exc:
            send_telegram(t("price_error", symbol=symbol, exc=exc), chat_id=chat_id)

    def cmd_free(update, context):
        chat_id = str(update.effective_chat.id)
        if not _authorized(chat_id):
            send_telegram(t("unauthorized"), chat_id=chat_id)
            return
        if sell_bot is None:
            send_telegram(t("balance_unavailable"), chat_id=chat_id)
            return
        future = asyncio.run_coroutine_threadsafe(
            sell_bot.client.get_asset_balance(asset="USDT"), loop
        )
        try:
            bal = future.result()
            free = float(bal.get("free", 0))
            send_telegram(t("free_value", free=free), chat_id=chat_id)
        except Exception as exc:
            send_telegram(t("free_error", exc=exc), chat_id=chat_id)

    def cmd_buy(update, context):
        chat_id = str(update.effective_chat.id)
        if not _authorized(chat_id):
            send_telegram(t("unauthorized"), chat_id=chat_id)
            return
        if buy_bot is None:
            send_telegram(t("buy_unavailable"), chat_id=chat_id)
            return
        args = context.args
        if not args:
            send_telegram(t("usage_buy"), chat_id=chat_id)
            return
        symbol = args[0].upper().replace("USDT", "") + "USDT"
        amount = None
        if len(args) > 1:
            try:
                amount = float(args[1])
            except ValueError:
                send_telegram(t("amount_invalid"), chat_id=chat_id)
                return
        if amount is None:
            future = asyncio.run_coroutine_threadsafe(
                buy_bot.client.get_asset_balance(asset="USDT"), loop
            )
            try:
                bal = future.result()
                amount = float(bal.get("free", 0))
            except Exception:
                amount = 0.0
        future = asyncio.run_coroutine_threadsafe(
            buy_bot.execute_buy(symbol, amount), loop
        )
        try:
            result = future.result()
        except Exception as exc:
            send_telegram(t("buy_error", exc=exc), chat_id=chat_id)
        else:
            if result:
                send_telegram(t("buy_sent", symbol=symbol, amount=amount), chat_id=chat_id)
            else:
                reason = getattr(buy_bot, "last_skip_reason", "")
                send_telegram(t("buy_skipped", reason=reason), chat_id=chat_id)

    dispatcher.add_handler(CommandHandler("start", cmd_start))
    dispatcher.add_handler(CommandHandler("help", cmd_help))
    dispatcher.add_handler(CommandHandler("summary", cmd_summary))
    dispatcher.add_handler(CommandHandler("report", cmd_report))
    dispatcher.add_handler(CommandHandler("balances", cmd_balances))
    dispatcher.add_handler(CommandHandler("positions", cmd_positions))
    dispatcher.add_handler(CommandHandler("price", cmd_price))
    dispatcher.add_handler(CommandHandler("free", cmd_free))
    dispatcher.add_handler(CommandHandler("buy", cmd_buy))
    dispatcher.add_handler(CommandHandler("sell", cmd_sell))

    try:
        updater.start_polling()
    except Exception as exc:  # pragma: no cover - network error
        msg = str(exc)
        if "terminated by other getUpdates" in msg or "Conflict" in msg or "409" in msg:
            log("Telegram listener baslatilamadi")
            return
        raise
    log("Telegram listener baslatildi")
