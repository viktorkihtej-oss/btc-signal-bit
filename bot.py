import os
import time
import requests

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    requests.post(
        url,
        data={"chat_id": CHAT_ID, "text": text},
        timeout=20
    )

def get_candles():
    url = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
    r = requests.get(
        url,
        params={"granularity": 900},
        headers={"User-Agent": "BTC-Signal-Bot"},
        timeout=20
    )
    r.raise_for_status()
    data = r.json()
    data.sort(key=lambda x: x[0])
    return data

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
        return 100.0

    rs = avg_gain / avg_loss
    return 100
