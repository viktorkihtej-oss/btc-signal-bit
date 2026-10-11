import os
import time
import logging
import requests
import statistics
from datetime import datetime, timezone

# =========================
# НАЛАШТУВАННЯ
# =========================

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHAT_ID = os.environ["CHAT_ID"]

SYMBOL = "BTCUSDT"
CHECK_SECONDS = 60

# Тільки сильні сигнали
MIN_SIGNAL_SCORE = 80

# Мінімальне співвідношення прибуток/ризик
RISK_REWARD = 2.0

TELEGRAM_URL = (
    f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
)

# Coinbase залишаємо резервним джерелом свічок
COINBASE_URL = (
    "https://api.exchange.coinbase.com/"
    "products/BTC-USD/candles"
)

# Bybit — ф'ючерсні дані
BYBIT_URL = "https://api.bybit.com"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

session = requests.Session()

session.headers.update({
    "User-Agent": "BTCSignalBot/3.0",
    "Accept": "application/json"
})

last_signal = None


# =========================
# TELEGRAM
# =========================

def send_message(text):
    try:
        r = session.post(
            TELEGRAM_URL,
            data={
                "chat_id": CHAT_ID,
                "text": text
            },
            timeout=20
        )

        r.raise_for_status()

        result = r.json()

        if not result.get("ok"):
            logging.error(
                "Telegram error: %s",
                result
            )
            return False

        logging.info("Telegram message sent")
        return True

    except Exception as e:
        logging.error(
            "Telegram failed: %s",
            e
        )
        return False


# =========================
# COINBASE CANDLES
# =========================

def get_candles(granularity):
    r = session.get(
        COINBASE_URL,
        params={
            "granularity": granularity
        },
        timeout=20
    )

    r.raise_for_status()
    data = r.json()

    if not isinstance(data, list):
        raise ValueError(
            f"Invalid Coinbase data: {data}"
        )

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

    candles.sort(
        key=lambda x: x["time"]
    )

    now = int(time.time())

    # Використовуємо тільки закриті свічки
    candles = [
        c for c in candles
        if c["time"] + granularity <= now
    ]

    if len(candles) < 210:
        raise ValueError(
            "Not enough candle data"
        )

    return candles


# =========================
# ІНДИКАТОРИ
# =========================

def ema(values, period):
    if len(values) < period:
        raise ValueError("Not enough EMA data")

    result = (
        sum(values[:period]) / period
    )

    multiplier = 2 / (period + 1)

    for value in values[period:]:
        result = (
            value * multiplier
            + result * (1 - multiplier)
        )

    return result


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

    avg_gain = (
        sum(gains[:period]) / period
    )

    avg_loss = (
        sum(losses[:period]) / period
    )

    for i in range(
        period,
        len(changes)
    ):
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

    return (
        100 - (100 / (1 + rs))
    )


def atr(candles, period=14):
    if len(candles) <= period:
        raise ValueError("Not enough ATR data")

    ranges = []

    for i in range(
        len(candles) - period,
        len(candles)
    ):
        current = candles[i]
        previous = candles[i - 1]

        true_range = max(
            current["high"]
            - current["low"],

            abs(
                current["high"]
                - previous["close"]
            ),

            abs(
                current["low"]
                - previous["close"]
            )
        )

        ranges.append(true_range)

    return sum(ranges) / len(ranges)


# =========================
# BYBIT FUTURES
# =========================

def bybit_get(path, params):
    r = session.get(
        BYBIT_URL + path,
        params=params,
        timeout=15
    )

    r.raise_for_status()

    data = r.json()

    if data.get("retCode") != 0:
        raise ValueError(
            f"Bybit error: {data}"
        )

    return data


def get_futures_data():
    result = {
        "funding": None,
        "open_interest": None,
        "bid_volume": None,
        "ask_volume": None,
        "orderbook_ratio": None
    }

    # Funding + OI
    try:
        data = bybit_get(
            "/v5/market/tickers",
            {
                "category": "linear",
                "symbol": SYMBOL
            }
        )

        ticker = (
            data["result"]["list"][0]
        )

        result["funding"] = float(
            ticker.get(
                "fundingRate",
                0
            )
        )

        result["open_interest"] = float(
            ticker.get(
                "openInterest",
                0
            )
        )

    except Exception as e:
        logging.warning(
            "Bybit ticker unavailable: %s",
            e
        )

    # Стакан
    try:
        data = bybit_get(
            "/v5/market/orderbook",
            {
                "category": "linear",
                "symbol": SYMBOL,
                "limit": 50
            }
        )

        bids = data["result"]["b"]
        asks = data["result"]["a"]

        bid_volume = sum(
            float(x[1])
            for x in bids
        )

        ask_volume = sum(
            float(x[1])
            for x in asks
        )

        result["bid_volume"] = bid_volume
        result["ask_volume"] = ask_volume

        if ask_volume > 0:
            result[
                "orderbook_ratio"
            ] = (
                bid_volume
                / ask_volume
            )

    except Exception as e:
        logging.warning(
            "Bybit orderbook unavailable: %s",
            e
        )

    return result


# =========================
# АНАЛІЗ РИНКУ
# =========================

def analyze():
    # 15 хвилин
    candles15 = get_candles(900)

    # 1 година
    candles1h = get_candles(3600)

    closes15 = [
        c["close"]
        for c in candles15
    ]

    closes1h = [
        c["close"]
        for c in candles1h
    ]

    price = closes15[-1]

    ema20 = ema(
        closes15,
        20
    )

    ema50 = ema(
        closes15,
        50
    )

    ema200 = ema(
        closes15,
        200
    )

    ema20_1h = ema(
        closes1h,
        20
    )

    ema50_1h = ema(
        closes1h,
        50
    )

    ema200_1h = ema(
        closes1h,
        200
    )

    current_rsi = rsi(
        closes15
    )

    current_atr = atr(
        candles15
    )

    # Обсяг
    volumes = [
        c["volume"]
        for c in candles15
    ]

    average_volume = (
        sum(volumes[-21:-1]) / 20
    )

    if average_volume > 0:
        volume_ratio = (
            volumes[-1]
            / average_volume
        )
    else:
        volume_ratio = 1.0

    # Рух останньої свічки
    last_candle = candles15[-1]

    candle_move = (
        abs(
            last_candle["close"]
            - last_candle["open"]
        )
    )

    # Не переслідуємо занадто
    # різкий рух
    overextended = (
        current_atr > 0
        and candle_move
        > current_atr * 1.8
    )

    futures = get_futures_data()

    book_ratio = futures[
        "orderbook_ratio"
    ]

    funding = futures[
        "funding"
    ]

    # =====================
    # LONG SCORE
    # =====================

    long_score = 0
    long_reasons = []

    if ema20 > ema50:
        long_score += 10
        long_reasons.append(
            "EMA20 > EMA50"
        )

    if ema50 > ema200:
        long_score += 15
        long_reasons.append(
            "15m тренд вгору"
        )

    if ema20_1h > ema50_1h:
        long_score += 15
        long_reasons.append(
            "1h імпульс вгору"
        )

    if ema50_1h > ema200_1h:
        long_score += 20
        long_reasons.append(
            "1h тренд вгору"
        )

    if price > ema20:
        long_score += 5

    if 52 <= current_rsi <= 64:
        long_score += 10
        long_reasons.append(
            "RSI підтримує LONG"
        )

    if volume_ratio >= 1.2:
        long_score += 10
        long_reasons.append(
            "обсяг підтверджує"
        )

    if (
        book_ratio is not None
        and book_ratio >= 1.15
    ):
        long_score += 10
        long_reasons.append(
            "стакан за покупців"
        )

    if (
        funding is not None
        and funding < 0.0005
    ):
        long_score += 5

    if volume_ratio < 0.8:
        long_score -= 10

    if overextended:
        long_score -= 20

    # =====================
    # SHORT SCORE
    # =====================

    short_score = 0
    short_reasons = []

    if ema20 < ema50:
        short_score += 10
        short_reasons.append(
            "EMA20 < EMA50"
        )

    if ema50 < ema200:
        short_score += 15
        short_reasons.append(
            "15m тренд вниз"
        )

    if ema20_1h < ema50_1h:
        short_score += 15
        short_reasons.append(
            "1h імпульс вниз"
        )

    if ema50_1h < ema200_1h:
        short_score += 20
        short_reasons.append(
            "1h тренд вниз"
        )

    if price < ema20:
        short_score += 5

    if 36 <= current_rsi <= 48:
        short_score += 10
        short_reasons.append(
            "RSI підтримує SHORT"
        )

    if volume_ratio >= 1.2:
        short_score += 10
        short_reasons.append(
            "обсяг підтверджує"
        )

    if (
        book_ratio is not None
        and book_ratio <= 0.87
    ):
        short_score += 10
        short_reasons.append(
            "стакан за продавців"
        )

    if (
        funding is not None
        and funding > -0.0005
    ):
        short_score += 5

    if volume_ratio < 0.8:
        short_score -= 10

    if overextended:
        short_score -= 20

    long_score = max(
        0,
        min(long_score, 100)
    )

    short_score = max(
        0,
        min(short_score, 100)
    )

    logging.info(
        (
            "BTC %.2f | RSI %.1f | "
            "VOL %.2fx | LONG %s | "
            "SHORT %s | BOOK %s"
        ),
        price,
        current_rsi,
        volume_ratio,
        long_score,
        short_score,
        book_ratio
    )

    # Сигнал тільки якщо
    # перевага очевидна
    if (
        long_score >= MIN_SIGNAL_SCORE
        and long_score
        >= short_score + 20
        and not overextended
    ):
        direction = "LONG"
        score = long_score
        reasons = long_reasons

    elif (
        short_score >= MIN_SIGNAL_SCORE
        and short_score
        >= long_score + 20
        and not overextended
    ):
        direction = "SHORT"
        score = short_score
        reasons = short_reasons

    else:
        return None

    # ATR-стоп
    stop_distance = max(
        current_atr * 1.5,
        price * 0.006
    )

    if direction == "LONG":
        stop = (
            price - stop_distance
        )

        take = (
            price
            + stop_distance
            * RISK_REWARD
        )

    else:
        stop = (
            price + stop_distance
        )

        take = (
            price
            - stop_distance
            * RISK_REWARD
        )

    return {
        "direction": direction,
        "price": price,
        "stop": stop,
        "take": take,
        "score": score,
        "rsi": current_rsi,
        "atr": current_atr,
        "volume_ratio": volume_ratio,
        "book_ratio": book_ratio,
        "funding": funding,
        "open_interest":
            futures["open_interest"],
        "reasons": reasons,
        "candle_time":
            candles15[-1]["time"]
    }


# =========================
# ПОВІДОМЛЕННЯ
# =========================

def format_signal(s):
    if s["direction"] == "LONG":
        icon = "🟢"
    else:
        icon = "🔴"

    book = s["book_ratio"]

    if book is None:
        book_text = "н/д"
    else:
        book_text = f"{book:.2f}"

    funding = s["funding"]

    if funding is None:
        funding_text = "н/д"
    else:
        funding_text = (
            f"{funding * 100:.4f}%"
        )

    reasons = "\n".join(
        f"✓ {x}"
        for x in s["reasons"]
    )

    return (
        f"{icon} BTC V3 СИЛЬНИЙ СИГНАЛ\n\n"
        f"Напрямок: {s['direction']}\n"
        f"Вхід орієнтовно: "
        f"${s['price']:,.2f}\n"
        f"Stop Loss: "
        f"${s['stop']:,.2f}\n"
        f"Take Profit: "
        f"${s['take']:,.2f}\n\n"
        f"Сила сигналу: "
        f"{s['score']}/100\n"
        f"RSI: {s['rsi']:.1f}\n"
        f"Обсяг: "
        f"{s['volume_ratio']:.2f}x\n"
        f"Стакан bid/ask: "
        f"{book_text}\n"
        f"Funding: "
        f"{funding_text}\n"
        f"Risk/Reward: 1:"
        f"{RISK_REWARD:.1f}\n\n"
        f"Підтвердження:\n"
        f"{reasons}\n\n"
        f"⚠️ Це сигнал алгоритму, "
        f"а не гарантія прибутку."
    )


# =========================
# MAIN
# =========================

def main():
    global last_signal

    logging.info(
        "BTC Signal Bot V3 starting"
    )

    send_message(
        "✅ BTC Signal Bot V3 запущений!\n\n"
        "Режим: тільки сильні сигнали.\n"
        "EMA20/50/200 + RSI + ATR + "
        "обсяг + стакан + funding.\n"
        "Слабкі сигнали відсіюються."
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

            else:
                logging.info(
                    "WAIT - no strong signal"
                )

        except Exception as e:
            logging.exception(
                "Analysis error: %s",
                e
            )

        time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    main()
