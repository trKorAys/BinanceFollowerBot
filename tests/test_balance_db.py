import os
import sys

# Proje kök dizinini test path'ine ekle
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import importlib
import bot.sell_bot as sell_bot

class Dummy:
    async def get_account(self):
        return {"balances": []}


def test_balance_history_persistence(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("BALANCE_DB_PATH", str(db))
    module = importlib.reload(sell_bot)
    bot1 = module.SellBot(Dummy())
    bot1.save_daily_balance("2023-01-01", 10.0)
    bot1.save_daily_balance("2023-01-02", 12.0)
    hist1 = bot1.get_balance_history()
    bot2 = module.SellBot(Dummy())
    hist2 = bot2.get_balance_history()
    assert hist1 == hist2
