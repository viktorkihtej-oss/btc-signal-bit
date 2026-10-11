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

send_message("✅ BTC Signal Bot запущений")

while True:
    try:
        data = requests.get(
            "https://api.binance.com/api/v3/ticker/price",
            params={"symbol": "BTCUSDT"},
            timeout=20
        ).json()

        price = float(data["price"])
        print("BTC:", price)

    except Exception as e:
        print("ERROR:", e)

    time.sleep(60)
