# Binance Follower Bot

Bu bot, Binance API üzerinden alım satım işlemlerini takip etmenize yardımcı olur. Telegram üzerinden komut gönderebilir ve bildirim alabilirsiniz. Kod Python ile yazılmıştır.

## Kurulum

1. Python 3.8 veya üzeri bir sürüm kurulu olmalıdır.
2. Sanal ortam oluşturup etkinleştirin:
   ```bash
   python -m venv venv
   source venv/bin/activate
   ```
3. Gerekli paketleri yükleyin:
   ```bash
   pip install -r requirements.txt
   ```
   `ta-lib` kurulamazsa bot yine çalışır.
4. `cp .env.example .env` komutu ile ortam değişkenlerini tanımlayın ve kendi bilgilerinizi girin.
5. Telegram'da `@BotFather` üzerinden bir bot oluşturup token değerini `TELEGRAM_TOKEN` alanına yazın. `TELEGRAM_CHAT_ID` için botla konuşup `getUpdates` sonucundaki chat id'yi kullanın.

## Ortam Değişkenleri

`.env` dosyasında en sık kullanılan değişkenler:
- `BINANCE_API_KEY`, `BINANCE_API_SECRET`
- `BINANCE_TESTNET` (true/false)
- `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`
- `LOCAL_TIMEZONE` (örn. `Europe/Istanbul`)
- `STOP_LOSS_ENABLED` (true/false)

Tüm değişkenler için `.env.example` dosyasına bakabilirsiniz.

## Kullanım

Sanal ortam aktifleştirildikten sonra bot aşağıdaki komutlarla çalıştırılır:

```bash
python -m bot.sell_bot       # Sadece satış tarafı
python -m bot.buy_bot        # Sadece alış tarafı
python -m bot.testnet_bot    # Testnet ortamında her ikisi
python -m bot.mainnet_bot    # Gerçek ortamda her ikisi
```

Çıkmak için `deactivate` komutunu kullanabilirsiniz.

Fiyat üst Keltner bandına ulaştığında:
- Son 5 dakikalık satış hacmi alış hacminden yüksek **ve** fiyat önceden belirlenen hedefin üzerindeyse pozisyon satılır.
- Kar **negatifse** ve `STOP_LOSS_ENABLED=true` ise hacim kontrolüne bakılmadan satış yapılır.

## Zaman Dilimi

Backend tarafında bütün zaman bilgileri **UTC‑0** olarak tutulur. Loglar ve Telegram mesajları `LOCAL_TIMEZONE` değişkeni tanımlıysa bu değere, aksi halde sistem saat dilimine çevrilerek gösterilir. Tarayıcı tabanlı arayüzlerde zamanlar cihazın saat dilimine göre gösterilir.
Hacim hesaplamaları gibi süreye dayalı tüm kontroller de UTC‑0 üzerinden yapılır.

## Değişiklikler

- Üst Keltner bandı kuralı geliştirildi: fiyat bandı aşıldığında son 5 dakikalık hacim analizi yapılır; satış hacmi alış hacminden büyük ve fiyat hedefi geçiyorsa satış tetiklenir.

## Planlananlar

- Keltner bandı eşiği, hacim periyodu ve stop-loss parametreleri için daha esnek yapılandırma seçenekleri.

## Testler

Projede yer alan testleri çalıştırmak için:
```bash
pytest -q
```
