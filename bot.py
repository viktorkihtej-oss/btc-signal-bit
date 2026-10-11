import os
import time
import logging
import requests

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

CHECK_SECONDS = 300
RISK_REWARD = 2.0
STOP_PERCENT = 0.012

API_URL = "https://api.exchange.coinbase.com/products/BTC-USD/candles"
TELEGRAM_URL = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

session = requests.Session()
session.headers.update({
    "User-Agent": "BTCSignalBot/2.0",
    "Accept": "application/json"
})

last_signal = None


def send_message(text):
    try:
        r = session.post(
            TELEGRAM_URL,
            data={"chat_id": CHAT_ID, "text": text},
            timeout=20
        )
        r.raise_for_status()

        result = r.json()

        if not result.get("ok"):
            logging.error("Telegram error: %s", result)
            return False

        logging.info("Telegram message sent")
        return True

    except Exception as e:
        logging.error("Telegram failed: %s", e)
        return False


def get_candles(granularity):
    r = session.get(
        API_URL,
        params={"granularity": granularity},
        timeout=20
    )

    r.raise_for_status()
    data = r.json()

    candles = []

    for c in data:
        if len(c) >= 6:
            candles.append({
                "time": int(c[0]),
                "low": float(c[1]),
                "high": float(c[2]),
                "open": float(c[3]),
                "close": float(c[4]),
                "volume": float(c[5])
            })

    candles.sort(key=lambda x: x["time"])

    now = int(time.time())

    candles = [
        c for c in candles
        if c["time"] + granularity <= now
    ]

    if len(candles) < 60:
        raise ValueError("Not enough candle data")

    return candles


def ema(values, period):
    result = sum(values[:period]) / period
    multiplier = 2 / (period + 1)

    for value in values[period:]:
        result = (
            value * multiplier
            + result * (1 - multiplier)
        )

    return result


def rsi(values, period=14):
    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


def signal_score(
    direction,
    price,
    ema20,
    ema50,
    ema20_1h,
    ema50_1h,
    current_rsi,
    volume_ratio
):
    score = 50

    if direction == "LONG":
        if ema20 > ema50:
            score += 7

        if ema20_1h > ema50_1h:
            score += 10

        if price > ema20:
            score += 5

        if 55 <= current_rsi <= 65:
            score += 8

    else:
        if ema20 < ema50:
            score += 7

        if ema20_1h < ema50_1h:
            score += 10

        if price < ema20:
            score += 5

        if 35 <= current_rsi <= 45:
            score += 8

    if volume_ratio >= 1.5:
        score += 10
    elif volume_ratio >= 1.2:
        score += 6
    elif volume_ratio < 0.8:
        score -= 7

    return max(5, min(score, 90))


def analyze():
    candles15 = get_candles(900)
    candles1h = get_candles(3600)

    closes15 = [c["close"] for c in candles15]
    closes1h = [c["close"] for c in candles1h]

    price = closes15[-1]

    ema20 = ema(closes15, 20)
    ema50 = ema(closes15, 50)

    ema20_1h = ema(closes1h, 20)
    ema50_1h = ema(closes1h, 50)

    current_rsi = rsi(closes15)

    volumes = [c["volume"] for c in candles15]

    average_volume = (
        sum(volumes[-21:-1]) / 20
    )

    current_volume = volumes[-1]

    if average_volume > 0:
        volume_ratio = current_volume / average_volume
    else:
        volume_ratio = 1.0

    long_signal = (
        ema20 > ema50
        and ema20_1h > ema50_1h
        and price > ema20
        and 52 <= current_rsi <= 68
    )

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
        logging.info(
            "No signal | BTC %.2f | RSI %.1f",
            price,
            current_rsi
        )
        return None

    score = signal_score(
        direction,
        price,
        ema20,
        ema50,
        ema20_1h,
        ema50_1h,
        current_rsi,
        volume_ratio
    )

    return {
        "direction": direction,
        "price": price,
        "stop": stop,
        "take": take,
        "rsi": current_rsi,
        "volume_ratio": volume_ratio,
        "score": score,
        "candle_time": candles15[-1]["time"]
    }


def format_signal(s):
    icon = "🟢" if s["direction"] == "LONG" else "🔴"

    if s["score"] >= 80:
        strength = "ДУЖЕ СИЛЬНИЙ"
    elif s["score"] >= 70:
        strength = "СИЛЬНИЙ"
    elif s["score"] >= 60:
        strength = "СЕРЕДНІЙ"
    else:
        strength = "СЛАБКИЙ"

    return (
        f"{icon} BTC SIGNAL\n\n"
        f"Напрямок: {s['direction']}\n"
        f"Вхід: ${s['price']:,.2f}\n"
        f"Stop Loss: ${s['stop']:,.2f}\n"
        f"Take Profit: ${s['take']:,.2f}\n\n"
        f"📊 Оцінка сигналу: {s['score']}%\n"
        f"Сила: {strength}\n\n"
        f"RSI: {s['rsi']:.1f}\n"
        f"Обсяг: {s['volume_ratio']:.2f}x середнього\n"
        f"Risk/Reward: 1:2\n\n"
        f"⚠️ Відсоток — оцінка сили сигналу, "
        f"а не гарантована ймовірність прибутку."
    )


def main():
    global last_signal

    logging.info("BTC Signal Bot v2 starting")

    send_message(
        "✅ BTC Signal Bot v2 запущений!\n\n"
        "EMA + RSI + обсяг активні.\n"
        "Оцінка сили сигналу активна."
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
                    if send_message(
                        format_signal(signal)
                    ):
                        last_signal = signal_id

        except Exception as e:
            logging.exception(
                "Analysis error: %s",
                e
            )

        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    main()
