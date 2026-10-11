import os
import time
import requests

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

SYMBOL = "BTCUSDT"
INTERVAL = "15m"

def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(
        url,
        data={"chat_id": CHAT_ID, "text": text},
        timeout=20
    )

def get_candles():
    url = "https://api.binance.com/api/v3/klines"
    r = requests.get(
        url,
        params={"symbol": SYMBOL, "interval": INTERVAL, "limit": 100},
        timeout=20
    )
    r.raise_for_status()
    return r.json()

def ema(values, period):
    k = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = value * k + result * (1 - k)
    return result

def rsi(values, period=14):
    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

last_candle = None

send_message("🤖 BTC Signal Bot: аналіз ринку запущено")

while True:
    try:
        candles = get_candles()

        # Беремо останню ЗАКРИТУ свічку
        closed = candles[:-1]
        candle = closed[-1]

        candle_time = candle[6]

        if candle_time != last_candle:
            closes = [float(x[4]) for x in closed]
            volumes = [float(x[5]) for x in closed]

            price = closes[-1]
            ema20 = ema(closes[-50:], 20)
            ema50 = ema(closes, 50)
            rsi14 = rsi(closes, 14)

            avg_volume = sum(volumes[-21:-1]) / 20
            current_volume = volumes[-1]

            signal = None

            if (
                price > ema20 > ema50
                and 52 <= rsi14 <= 70
                and current_volume > avg_volume
            ):
                signal = "🟢 LONG"

            elif (
                price < ema20 < ema50
                and 30 <= rsi14 <= 48
                and current_volume > avg_volume
            ):
                signal = "🔴 SHORT"

            if signal:
                send_message(
                    f"{signal} BTC/USDT\n"
                    f"Ціна: {price:.2f}\n"
                    f"EMA20: {ema20:.2f}\n"
                    f"EMA50: {ema50:.2f}\n"
                    f"RSI14: {rsi14:.1f}\n"
                    f"Обсяг: {current_volume:.2f}"
                )

            last_candle = candle_time

    except Exception as e:
        print("ERROR:", e, flush=True)

    time.sleep(60)
