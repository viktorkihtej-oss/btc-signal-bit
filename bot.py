import os
import time
import logging
import requests
from datetime import datetime, timezone

# ===== НАЛАШТУВАННЯ =====

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

SYMBOL = "BTC-USD"

CHECK_SECONDS = 300
RISK_REWARD = 2.0
STOP_PERCENT = 0.012

API_URL = (
    "https://api.exchange.coinbase.com"
    "/products/BTC-USD/candles"
)

TELEGRAM_URL = (
    f"https://api.telegram.org/bot{BOT_TOKEN}"
    "/sendMessage"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

session = requests.Session()

session.headers.update({
    "User-Agent": "BTCSignalBot/1.0",
    "Accept": "application/json"
})

last_signal = None


# ===== TELEGRAM =====

def send_message(text):
    try:
        response = session.post(
            TELEGRAM_URL,
            data={
                "chat_id": CHAT_ID,
                "text": text
            },
            timeout=20
        )

        response.raise_for_status()

        result = response.json()

        if not result.get("ok"):
            logging.error(
                "Telegram error: %s",
                result.get("description")
            )
            return False

        logging.info("Telegram message sent")
        return True

    except Exception as error:
        logging.error(
            "Telegram request failed: %s",
            error
        )
        return False


# ===== ОТРИМАННЯ СВІЧОК =====

def get_candles(granularity):
    response = session.get(
        API_URL,
        params={
            "granularity": granularity
        },
        timeout=20
    )

    response.raise_for_status()

    data = response.json()

    if not isinstance(data, list):
        raise ValueError(
            f"Invalid candle data: {data}"
        )

    candles = []

    for candle in data:
        if len(candle) < 6:
            continue

        candles.append({
            "time": int(candle[0]),
            "low": float(candle[1]),
            "high": float(candle[2]),
            "open": float(candle[3]),
            "close": float(candle[4]),
            "volume": float(candle[5])
        })

    candles.sort(
        key=lambda item: item["time"]
    )

    # Не використовуємо незакриту свічку
    now = int(time.time())

    candles = [
        c for c in candles
        if c["time"] + granularity <= now
    ]

    if len(candles) < 60:
        raise ValueError(
            "Not enough candle data"
        )

    return candles


# ===== EMA =====

def ema(values, period):
    if len(values) < period:
        raise ValueError("Not enough EMA data")

    multiplier = 2 / (period + 1)

    result = sum(values[:period]) / period

    for value in values[period:]:
        result = (
            value * multiplier
            + result * (1 - multiplier)
        )

    return result


# ===== RSI =====

def rsi(values, period=14):
    if len(values) <= period:
        raise ValueError("Not enough RSI data")

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [
        max(change, 0)
        for change in changes
    ]

    losses = [
        max(-change, 0)
        for change in changes
    ]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):
        avg_gain = (
            avg_gain * (period - 1)
            + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1)
            + losses[i]
        ) / period

    if avg_loss == 0:
        if avg_gain == 0:
            return 50.0
        return 100.0

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


# ===== АНАЛІЗ =====

def analyze():
    candles_15m = get_candles(900)
    candles_1h = get_candles(3600)

    closes_15m = [
        c["close"] for c in candles_15m
    ]

    closes_1h = [
        c["close"] for c in candles_1h
    ]

    price = closes_15m[-1]

    ema20 = ema(closes_15m, 20)
    ema50 = ema(closes_15m, 50)

    ema20_1h = ema(closes_1h, 20)
    ema50_1h = ema(closes_1h, 50)

    current_rsi = rsi(closes_15m)

    candle_time = candles_15m[-1]["time"]

    logging.info(
        "BTC %.2f | RSI %.2f | EMA20 %.2f | EMA50 %.2f",
        price,
        current_rsi,
        ema20,
        ema50
    )

    # LONG: напрямок на обох таймфреймах
    # і підтвердження імпульсу

    long_signal = (
        ema20 > ema50
        and ema20_1h > ema50_1h
        and price > ema20
        and 52 <= current_rsi <= 68
    )

    # SHORT: напрямок на обох таймфреймах
    # і підтвердження імпульсу

    short_signal = (
        ema20 < ema50
        and ema20_1h < ema50_1h
        and price < ema20
        and 32 <= current_rsi <= 48
    )

    if long_signal:
        direction = "LONG"

        stop = price * (1 - STOP_PERCENT)

        take = price + (
            price - stop
        ) * RISK_REWARD

    elif short_signal:
        direction = "SHORT"

        stop = price * (1 + STOP_PERCENT)

        take = price - (
            stop - price
        ) * RISK_REWARD

    else:
        return None

    return {
        "direction": direction,
        "price": price,
        "stop": stop,
        "take": take,
        "rsi": current_rsi,
        "candle_time": candle_time
    }


# ===== ФОРМУВАННЯ СИГНАЛУ =====

def format_signal(signal):
    direction = signal["direction"]

    if direction == "LONG":
        icon = "🟢"
    else:
        icon = "🔴"

    return (
        f"{icon} BTC SIGNAL\n\n"
        f"Напрямок: {direction}\n"
        f"Вхід орієнтовно: "
        f"${signal['price']:,.2f}\n"
        f"Stop Loss: "
        f"${signal['stop']:,.2f}\n"
        f"Take Profit: "
        f"${signal['take']:,.2f}\n\n"
        f"RSI: {signal['rsi']:.1f}\n"
        f"Risk/Reward: 1:2\n\n"
        f"Сигнал за закритими свічками.\n"
        f"Перевір актуальну ціну перед входом.\n"
        f"Не гарантує прибутку."
    )


# ===== ОСНОВНИЙ ЦИКЛ =====

def main():
    global last_signal

    logging.info("BTC Signal Bot starting")

    send_message(
        "✅ BTC Signal Bot запущений!\n\n"
        "Моніторинг BTC активний.\n"
        "Таймфрейми: 15m та 1h.\n"
        "Перевірка кожні 5 хвилин.\n"
        "Сигнали: LONG / SHORT."
    )

    while True:
        try:
            signal = analyze()

            if signal:
                signal_id = (
                    signal["direction"],
                    signal["candle_time"]
                )

                if signal_id != last_signal:
                    message = format_signal(signal)

                    if send_message(message):
                        last_signal = signal_id

            else:
                logging.info(
                    "No confirmed signal"
                )

        except Exception as error:
            logging.exception(
                "Analysis error: %s",
                error
            )

        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    main()
