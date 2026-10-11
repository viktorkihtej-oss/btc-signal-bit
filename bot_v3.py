import os
import time
import logging
import requests
import math

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

BASE = "https://api.bitget.com"
SYMBOL = "BTCUSDT"
PRODUCT = "USDT-FUTURES"

CHECK_SECONDS = 60
MIN_SCORE = 85
RR = 2.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

session = requests.Session()
session.headers.update({
    "User-Agent": "BTCSignalBot/3.1",
    "Accept": "application/json"
})

last_signal = None


def telegram(text):
    try:
        r = session.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={
                "chat_id": CHAT_ID,
                "text": text
            },
            timeout=15
        )
        r.raise_for_status()
        result = r.json()

        if not result.get("ok"):
            raise ValueError(result)

        return True

    except Exception as e:
        logging.error("Telegram error: %s", e)
        return False


def api(path, extra=None):
    params = {
        "symbol": SYMBOL,
        "productType": PRODUCT
    }

    if extra:
        params.update(extra)

    r = session.get(
        BASE + path,
        params=params,
        timeout=15
    )

    r.raise_for_status()
    result = r.json()

    if result.get("code") != "00000":
        raise ValueError(result)

    if result.get("data") is None:
        raise ValueError("Empty API data")

    return result["data"]


def candles(tf, seconds):
    data = api(
        "/api/v2/mix/market/candles",
        {
            "granularity": tf,
            "limit": "250"
        }
    )

    result = []

    now = int(time.time())

    for x in data:
        stamp = int(x[0]) // 1000

        if stamp + seconds > now:
            continue

        result.append({
            "time": stamp,
            "open": float(x[1]),
            "high": float(x[2]),
            "low": float(x[3]),
            "close": float(x[4]),
            "volume": float(x[5])
        })

    result.sort(key=lambda x: x["time"])

    if len(result) < 205:
        raise ValueError(
            f"Not enough {tf} candles"
        )

    if now - result[-1]["time"] > seconds * 3:
        raise ValueError(
            f"Stale {tf} candles"
        )

    return result


def ema(values, period):
    result = sum(values[:period]) / period
    k = 2 / (period + 1)

    for value in values[period:]:
        result = value * k + result * (1 - k)

    return result


def rsi(values, period=14):
    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    gain = sum(gains[:period]) / period
    loss = sum(losses[:period]) / period

    for i in range(period, len(changes)):
        gain = (
            gain * (period - 1) + gains[i]
        ) / period

        loss = (
            loss * (period - 1) + losses[i]
        ) / period

    if loss == 0:
        return 100.0 if gain > 0 else 50.0

    rs = gain / loss

    return 100 - 100 / (1 + rs)


def atr(data, period=14):
    ranges = []

    for i in range(
        len(data) - period,
        len(data)
    ):
        c = data[i]
        p = data[i - 1]

        ranges.append(max(
            c["high"] - c["low"],
            abs(c["high"] - p["close"]),
            abs(c["low"] - p["close"])
        ))

    return sum(ranges) / len(ranges)


def trend(data):
    closes = [x["close"] for x in data]

    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    e200 = ema(closes, 200)

    if e20 > e50 > e200:
        return "LONG"

    if e20 < e50 < e200:
        return "SHORT"

    return "WAIT"


def market_data():
    ticker = api(
        "/api/v2/mix/market/ticker"
    )[0]

    price = float(ticker["lastPr"])
    ticker_time = int(ticker["ts"]) / 1000

    if time.time() - ticker_time > 90:
        raise ValueError("Stale ticker")

    depth = api(
        "/api/v2/mix/market/merge-depth",
        {"limit": "50"}
    )

    if time.time() - int(depth["ts"]) / 1000 > 90:
        raise ValueError("Stale orderbook")

    bids = depth["bids"]
    asks = depth["asks"]

    if not bids or not asks:
        raise ValueError("Empty orderbook")

    bid_volume = sum(float(x[1]) for x in bids)
    ask_volume = sum(float(x[1]) for x in asks)

    if bid_volume <= 0 or ask_volume <= 0:
        raise ValueError("Invalid orderbook")

    book_ratio = bid_volume / ask_volume

    funding_data = api(
        "/api/v2/mix/market/current-fund-rate"
    )

    funding = float(
        funding_data[0]["fundingRate"]
    )

    oi_data = api(
        "/api/v2/mix/market/open-interest"
    )

    oi = float(
        oi_data["openInterestList"][0]["size"]
    )

    trades = api(
        "/api/v2/mix/market/fills",
        {"limit": "100"}
    )

    if not trades:
        raise ValueError("No recent trades")

    recent = [
        t for t in trades
        if time.time() - int(t["ts"]) / 1000 < 120
    ]

    if len(recent) < 10:
        raise ValueError("Not enough recent trades")

    buy = sum(
        float(t["size"]) * float(t["price"])
        for t in recent
        if t["side"].lower() == "buy"
    )

    sell = sum(
        float(t["size"]) * float(t["price"])
        for t in recent
        if t["side"].lower() == "sell"
    )

    if buy + sell <= 0:
        raise ValueError("Invalid trade volume")

    buy_share = buy / (buy + sell)

    return {
        "price": price,
        "book": book_ratio,
        "funding": funding,
        "oi": oi,
        "buy_share": buy_share
    }


def analyze():
    c15 = candles("15m", 900)
    c1h = candles("1H", 3600)
    c4h = candles("4H", 14400)

    t15 = trend(c15)
    t1h = trend(c1h)
    t4h = trend(c4h)

    if not (
        t15 == t1h == t4h
        and t15 != "WAIT"
    ):
        logging.info("WAIT: trends disagree")
        return None

    direction = t15

    m = market_data()
    price = m["price"]

    closes = [x["close"] for x in c15]
    current_rsi = rsi(closes)
    current_atr = atr(c15)

    if current_atr <= 0:
        return None

    volumes = [x["volume"] for x in c15]
    avg_volume = sum(volumes[-21:-1]) / 20

    if avg_volume <= 0:
        return None

    volume_ratio = volumes[-1] / avg_volume

    # Сильний сигнал потребує обсягу
    if volume_ratio < 1.2:
        logging.info("WAIT: weak volume")
        return None

    # Не переслідуємо різку свічку
    last = c15[-1]

    move = abs(
        last["close"] - last["open"]
    )

    if move > current_atr * 1.5:
        logging.info("WAIT: extended candle")
        return None

    # Перевірка відстані від EMA20
    e20 = ema(closes, 20)

    if abs(price - e20) > current_atr * 2:
        logging.info("WAIT: price too far from EMA")
        return None

    score = 50
    reasons = ["Тренди 15m, 1h, 4h збігаються"]

    if volume_ratio >= 1.5:
        score += 15
        reasons.append("Підвищений обсяг")
    else:
        score += 10
        reasons.append("Обсяг підтверджує рух")

    if direction == "LONG":
        if not 52 <= current_rsi <= 65:
            return None

        if m["book"] < 1.15:
            return None

        if m["buy_share"] < 0.55:
            return None

        if price <= e20:
            return None

        score += 25

        reasons.append("Стакан підтримує LONG")
        reasons.append("Переважають агресивні покупки")

    else:
        if not 35 <= current_rsi <= 48:
            return None

        if m["book"] > 0.87:
            return None

        if m["buy_share"] > 0.45:
            return None

        if price >= e20:
            return None

        score += 25

        reasons.append("Стакан підтримує SHORT")
        reasons.append("Переважають агресивні продажі")

    if abs(m["funding"]) > 0.001:
        logging.info("WAIT: extreme funding")
        return None

    score += 10

    score = min(score, 100)

    if score < MIN_SCORE:
        return None

    distance = max(
        current_atr * 1.5,
        price * 0.006
    )

    if direction == "LONG":
        stop = price - distance
        take = price + distance * RR
    else:
        stop = price + distance
        take = price - distance * RR

    return {
        "direction": direction,
        "price": price,
        "stop": stop,
        "take": take,
        "score": score,
        "rsi": current_rsi,
        "volume": volume_ratio,
        "book": m["book"],
        "buy_share": m["buy_share"],
        "funding": m["funding"],
        "oi": m["oi"],
        "reasons": reasons,
        "time": c15[-1]["time"]
    }


def format_signal(s):
    icon = (
        "🟢" if s["direction"] == "LONG"
        else "🔴"
    )

    reasons = "\n".join(
        "✓ " + x for x in s["reasons"]
    )

    return (
        f"{icon} BTC V3.1 SIGNAL\n\n"
        f"Напрямок: {s['direction']}\n"
        f"Вхід орієнтовно: ${s['price']:,.2f}\n"
        f"Stop Loss: ${s['stop']:,.2f}\n"
        f"Take Profit: ${s['take']:,.2f}\n\n"
        f"Сила: {s['score']}/100\n"
        f"RSI: {s['rsi']:.1f}\n"
        f"Обсяг: {s['volume']:.2f}x\n"
        f"Стакан: {s['book']:.2f}\n"
        f"Агресивні покупки: "
        f"{s['buy_share'] * 100:.1f}%\n"
        f"Open Interest: {s['oi']:,.2f}\n"
        f"Funding: {s['funding'] * 100:.4f}%\n"
        f"Risk/Reward: 1:2\n\n"
        f"Підтвердження:\n{reasons}\n\n"
        f"⚠️ Тестовий сигнал.\n"
        f"Бал сили не є ймовірністю прибутку.\n"
        f"Перевір ціну на Binance."
    )


def main():
    global last_signal

    logging.info("BTC Bot V3.1 starting")

    telegram(
        "✅ BTC Bot V3.1 запущений!\n"
        "Джерело: Bitget Futures.\n"
        "Режим: тільки сильні сигнали.\n"
        "Тестування без автоматичних угод."
    )

    while True:
        try:
            signal = analyze()

            if signal:
                signal_id = (
                    signal["direction"],
                    signal["time"]
                )

                if signal_id != last_signal:
                    if telegram(format_signal(signal)):
                        last_signal = signal_id
                        logging.info("Signal sent")
            else:
                logging.info("WAIT - no strong signal")

        except Exception as e:
            logging.exception(
                "Market analysis error: %s",
                e
            )

        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    main()
