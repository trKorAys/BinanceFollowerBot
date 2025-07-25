# Binance Follower Bot

A Python-based bot that monitors your Binance account and automatically sells when certain conditions are met. Purchases are tracked with the FIFO method and profit targets are determined according to current volatility. Notifications are delivered through Telegram. Existing balances are checked concurrently at startup. The number of simultaneous API calls is controlled with the `CONCURRENCY_LIMIT` value in the `.env` file.

## Features

- Tracks buy and sell operations using FIFO logic.
- Calculates dynamic profit targets based on recent market movement.
- Adds a fixed target equal to the sum of fees and minimum profit on top of three ATR-based levels.
- Sends notifications and responds to commands over Telegram.
- Supports price queries and manual buy/sell commands on Telegram.
- Contains an internal rate limiter so Binance API limits are not exceeded.
- Easy switching between testnet and mainnet.
- Telegram messages support multiple languages via the `TELEGRAM_LANG` variable.
- If no suitable symbol is found, it attempts purchases on coins where the price is above SMA‑7 while SMA‑7 is below SMA‑99.
- During the buy scan only the top `TOP_SYMBOLS_COUNT` symbols by USDT volume are considered and this list is automatically refreshed at 00, 06, 12 and 18 UTC‑0. Set `TOP_SYMBOLS_COUNT` in `.env` to change the default of 150.
- Positions worth less than **5 USDT** are ignored.
- Logs specify which buying strategy was attempted each time.
- Timestamps are kept in **UTC‑0** on the backend and shown in your browser time zone.
- Recently bought symbols are stored with a UTC timestamp and skipped for two hours.
- Recently sold symbols are also remembered and skipped for two hours.
- Target prices are printed whenever updated and instantaneous targets are shown on price changes.
- If the price drops back below any target level it is automatically sold.
- When the highest target is passed and the price stays above it, a one minute volume analysis is repeated every cycle; if sell volume exceeds buy volume or the price dips back below the target an automatic sale is triggered.
- Prices are monitored live via a websocket so sell decisions are applied without delay.

## Installation Steps

1. Ensure **Python 3.8+** is installed on your system.
2. Create and activate a virtual environment:
   ```bash
   python -m venv venv
   source venv/bin/activate
   ```
3. Install required libraries:
   ```bash
   pip install -r requirements.txt
   ```
   The bot will still run if `ta-lib` is missing; the SMA calculation will fall back to the internal method.
   If the automatic install of `ta-lib` fails you can download a wheel matching your Python version from [cgohlke/talib-build](https://github.com/cgohlke/talib-build/releases) and install it manually with `pip install <file>.whl`.
4. Copy the `.env.example` file from the repository to define environment variables:
   ```bash
   cp .env.example .env
   ```
   Fill in the fields with your own information. You can store your API keys, Telegram token and other settings here.
5. Create a new bot via `@BotFather` on Telegram and place the token in `TELEGRAM_TOKEN`. Add the bot to your chat group and after sending `/start`, record the `chat -> id` value from `https://api.telegram.org/botTOKEN/getUpdates` as `TELEGRAM_CHAT_ID`.
   When you run the `mainnet` or `testnet` bot the Telegram menu will automatically display `/start`, `/summary` and `/help`. These commands are now handled by a listener. `/summary` shows your current total balance while `/help` lists all commands.

## Environment Variables

The main variables in `.env` are:
- `BINANCE_API_KEY` and `BINANCE_API_SECRET`
- `TELEGRAM_TOKEN` and `TELEGRAM_CHAT_ID`
- `FEE_BUY_PERCENT`, `FEE_SELL_PERCENT`, `MIN_PROFIT_PERCENT`
- `BINANCE_TESTNET` enables testnet mode.
- `USDT_USAGE_RATIO` defines the portion of USDT to use for each buy cycle.
- `MIN_FOLLOW_NOTIONAL` sets the minimum USDT value required for regular buys and tracked positions.
- `LOSS_BUY_THRESHOLD_PERCENT` is the minimum loss percentage required to buy again. Default is **2**.
- `CHECK_INTERVAL` is the interval in seconds for the sell side loop. In testnet mode this value is automatically **60** seconds.
- `GROUP_SIZE` is how many symbols SellBot checks each cycle. Default is **10**.
- `CONCURRENCY_LIMIT` determines how many API calls are made concurrently during the initial balance check. Default is **5**.
- `BALANCE_DB_PATH` is the SQLite file for end-of-day reports. Default is `balances.db`.
 - `BUY_DB_PATH` stores the most recent buy and sell times in SQLite. Default is `buy.db`.
- Symbols stored here are checked before each cycle and any older than two hours are removed.
- `STOP_LOSS_ENABLED` enables ATR-based stop losses when set to `true`.
- `ATR_PERIOD` controls how many candles are used for ATR.
- `STOP_LOSS_MULTIPLIER` multiplies the ATR value to set the stop level.

### Language Selection

The Telegram language is chosen with the `TELEGRAM_LANG` variable in `.env`. Supported languages are `en`, `de`, `tr`, `fr`, `ar`, `zh`, `ru`, `ja`, `ko`. If left empty Turkish will be used.

```bash
TELEGRAM_LANG=en
```

### Creating a Binance Testnet Account

To operate in the testnet environment visit [testnet.binance.vision](https://testnet.binance.vision/), log in with your GitHub account and create an account. After logging in, generate a new *Testnet API* key from the **API Key** section of the user menu. Enter the resulting `API Key` and `Secret Key` in the `.env` file as `BINANCE_TESTNET_API_KEY` and `BINANCE_TESTNET_API_SECRET`, then enable testnet mode with `BINANCE_TESTNET=true`.

The testnet account is completely independent from your real balances and trades are made with virtual funds. Orders do not affect the real exchange but the same API rules apply. All calls use `testnet.binance.vision` instead of `api.binance.com` and commissions are approximated.

See `.env.example` for all values. Remember that percentages are specified in real values (`0.5` = 0.5%).

### Time Zone Settings

Timestamps are always stored as **UTC‑0** on the backend. The frontend uses the device time zone for display. To see output in your local time use the helper functions in `bot.utils`:

- `convert_utc_to_local(utc_str)` converts a UTC value to your system time zone.
- `convert_utc_to_timezone(utc_str, "Europe/Istanbul")` converts to any zone you choose.
- `convert_utc_to_env_timezone(utc_str)` converts according to the `LOCAL_TIMEZONE` variable in `.env`.

If `LOCAL_TIMEZONE` is defined log messages also use this time zone. Leave it empty to use the system zone. For example setting `LOCAL_TIMEZONE=Europe/Istanbul` will produce logs in Turkish time.

Common time zone names include:
- Turkey: `Europe/Istanbul`
- Germany: `Europe/Berlin`
- USA (New York): `America/New_York`
- Japan: `Asia/Tokyo`
- UK: `Europe/London`
  
### API Limits

Binance limits incoming requests using a "weight" system. The bot uses an internal counter to stay under the 6000 weight per minute limit. After each request the `X-MBX-USED-WEIGHT-1M` header is read to update the counter. If error `-1003` is returned the bot waits for the specified time. If too many symbols are tracked SellBot automatically splits them into groups and checks only part of them each cycle so the total API calls stay within safe bounds. By default each group contains **10** symbols.

## File Structure
- `bot/buy_bot.py` – Handles averaging down on positions in loss and processes new buy signals.
- `bot/sell_bot.py` – Monitors current positions and sells when profit targets are reached.
- `bot/testnet_bot.py` – Runs both bots in the Binance testnet environment.
- `bot/mainnet_bot.py` – Starts BuyBot and SellBot together on mainnet.
- `bot/utils.py` – Utility functions for time handling and more. Trades with a price of `0` are saved with a fixed price of `0.0000001` here.
- `bot/rate_limiter.py` – Helper module controlling the API weight limits.
- `bot/telegram_listener.py` – Helper module that listens for Telegram commands.

## Usage

Activate the virtual environment:
```bash
source venv/bin/activate
```
To start the sell side:
```bash
python -m bot.sell_bot
```
To start the buy side:
```bash
python -m bot.buy_bot
```
To run both on the testnet:
```bash
python -m bot.testnet_bot
```
To run both bots together in live mode:
```bash
python -m bot.mainnet_bot
```

Exit the virtual environment with:
```bash
source deactivate
```
These two files ignore the `BINANCE_TESTNET` value in `.env` and configure the required mode themselves. To test only the sell side on testnet enable `BINANCE_TESTNET=true` and run `python -m bot.sell_bot` again. Both bots report which network they are on and how many symbols are tracked via Telegram along with the IP address at startup. If the Telegram token is in use elsewhere the detected `Conflict` error prevents the chat bot from starting. All symbols in your balance are monitored even in testnet mode. Set `LOCAL_TIMEZONE` in `.env` to control log and console output times. For example `LOCAL_TIMEZONE=Europe/Istanbul` writes all logs in Turkish time. In testnet mode general notifications are no longer sent to Telegram; only buy operations and sales (if the buy price is not `0`) are reported. Other info is printed to the console. Important parts of notifications are **bold** and copyable fields such as IP or errors are wrapped in backticks. Trailing commas and spaces are cleaned up from all Telegram messages. Every message sent in testnet mode is prefixed with **TESTNET**. After an API error the next attempt waits 10 seconds. Sales of balances with a buy price of `0` are not announced via Telegram. The testnet bot no longer creates a Telegram menu and the chat bot is disabled; commands are active only in mainnet mode.

## Telegram Commands

While the bot is running you can send the following commands via Telegram:

- `/start` – Confirms the bot is active.
- `/summary` – Shows your current total balance in USDT.
- `/report` – Lists the last end-of-day balances (data is read from `balances.db`).
- `/balances` – Lists all symbols with a balance.
- `/positions` – Shows the status of tracked symbols.
- `/price <Symbol>` – Returns the current price for the given symbol.
- `/free` – Shows your free USDT balance.
- `/buy <Symbol>` – Buys using all free USDT.
- `/sell <Symbol>` – Sells the entire specified symbol.
- `/help` – Sends a summary of available commands.

### Telegram Chat Bot

`bot/telegram_listener.py` is a simple chat bot that waits for commands via Telegram. It automatically starts when `mainnet_bot.py` or `testnet_bot.py` is run and responds to `/start`, `/summary` and `/help`. Commands are listened for in a separate thread so bot operations continue uninterrupted. All messages are generated with UTC‑0 timestamps but the Telegram app shows them in your device time zone. This keeps backend times always in UTC‑0 while the chat screen uses local time.

Commands are automatically sent to the chat ID from which they originate. Commands from chats not matching the `TELEGRAM_CHAT_ID` value in `.env` are replied to as "unauthorized". You can provide multiple IDs separated by commas.

### Building an Exe on Windows

To run the bot on Windows without installing dependencies you can generate standalone executables using `PyInstaller`.

1. Install PyInstaller:
   ```bash
   pip install pyinstaller
   ```
2. Run the `build_exe.py` script in the repository root:
   ```bash
   python build_exe.py
   ```
   A small window will open allowing one‑click creation of an exe for `mainnet` or `testnet`. It also works on systems without a command line. Move the files created under `dist/` together with `.env` to any folder you like.
   The build script now calls PyInstaller with `--collect-all dateparser` so the
   required time zone cache is packaged automatically. Without this option the
   exe could fail with `dateparser_tz_cache.pkl` errors.
   Entry scripts now adjust `sys.path` when run directly so exes work without
   import errors. Frozen exe'ler için `sys.frozen` kontrolü eklenerek ve
   `sys._MEIPASS` dizini kullanılarak modül yolu otomatik ayarlanır, böylece
  `bot` paketine ait içe aktarmalar hatasız çalışır. Böylece oluşturulan exe
  çalıştırıldığında `ModuleNotFoundError: No module named 'bot'` hatası
  görülmez. Paket adının boş string olması da kontrol edilerek PyInstaller
  ile oluşturulan exe'lerin her ortamda sorunsuz başlaması sağlandı.
  Ayrıca PyInstaller derlemesinde tüm modüllerin paketlenebilmesi için
  `mainnet_bot.py` ve `testnet_bot.py` dosyalarındaki içe aktarmalar mutlak
  hale getirildi. Bu sayede `bot` paketindeki kodlar eksiksiz şekilde
  arşive eklenir.

## Running Tests

Run the unit tests in the project with:
```bash
pytest -q
```
The `test_env_timezone_conversion` test verifies that the `LOCAL_TIMEZONE` setting works correctly. The bot now reports your total balance at the end of each day at UTC‑0 via Telegram. These reports are stored in the `balances.db` SQLite file so that the `/report` command can show previous days after a restart. When no suitable symbol is found on the buy side a new strategy checking for SMA‑7 break and SMA‑7 < SMA‑99 is used. `build_exe.py` now provides a simple interface for generating an exe with one click.
Tests were also updated so `DummyDispatcher.add_handler` uses `handler.commands` when registering handlers.

Target levels are now divided into three steps from the fixed target up to the ATR target. Each target is printed when updated and a sale is executed if the price falls below that target. Once the highest target is passed and the price remains above it, a one minute volume analysis is repeated every cycle. If sell volume is higher than buy volume or the price drops back below the target, an automatic sale is made.

## Support

For donations our USDT address is: `THz1ssvnpVcmt9Kk24x4wD5XCMZBtnubnE`
