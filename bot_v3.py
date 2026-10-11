import os
import time
import json
import logging
import statistics
from pathlib import Path

import requests


# ==========================================
# BTC SIGNAL BOT V4
# Сигнали, НЕ автоматична торгівля
# ==========================================

TOKEN = os.environ["BOT_TOKEN"]
CHAT = os.environ["CHAT_ID"]

BASE = "https://api.bitget.com"
SYMBOL = "BTCUSDT"
PRODUCT = "USDT-FUTURES"

# Перевірка раз на хвилину
INTERVAL = 60

# Тимчасове збереження останнього сигналу
STATE = Path("/tmp/btc_v4_state.json")


# ==========================================
# LOGGING
# ==========================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

session = requests.Session()

session.headers.update({
    "User-Agent": "BTCSignalBot/4.0",
    "Accept": "application/json"
})


# ==========================================
# BITGET API
# ==========================================

def fetch(path, params=None):

    p = {
        "symbol": SYMBOL,
        "productType": PRODUCT
    }

    if params:
        p.update(params)

    response = session.get(
        BASE + path,
        params=p,
        timeout=15
    )

    response.raise_for_status()

    obj = response.json()

    if (
        obj.get("code") != "00000"
        or obj.get("data") is None
    ):
        raise ValueError(
            f"API {path}: {str(obj)[:180]}"
        )

    return obj["data"]


def number(value):

    x = float(value)

    if not (-1e100 < x < 1e100):
        raise ValueError(
            "Invalid numeric value"
        )

    return x


# ==========================================
# CANDLES
# ==========================================

def candles(
    timeframe,
    duration
):

    raw = fetch(
        "/api/v2/mix/market/candles",
        {
            "granularity": timeframe,
            "limit": "260"
        }
    )

    now = time.time()

    result = []

    for row in raw:

        candle_time = (
            int(row[0]) / 1000
        )

        # Тільки закриті свічки
        if (
            candle_time + duration
            > now - 3
        ):
            continue

        candle = {
            "t": int(candle_time),
            "o": number(row[1]),
            "h": number(row[2]),
            "l": number(row[3]),
            "c": number(row[4]),
            "v": number(row[5])
        }

        # Перевірка коректності OHLC
        if (
            candle["l"]
            <= min(
                candle["o"],
                candle["c"]
            )
            <= max(
                candle["o"],
                candle["c"]
            )
            <= candle["h"]
            and candle["v"] >= 0
        ):
            result.append(candle)

    result.sort(
        key=lambda x: x["t"]
    )

    # Прибираємо дублікати
    unique = {}

    for candle in result:
        unique[candle["t"]] = candle

    result = list(
        unique.values()
    )

    if len(result) < 205:
        raise ValueError(
            f"Not enough {timeframe} candles: "
            f"{len(result)}"
        )

    # Захист від застарілих даних
    if (
        now
        - (
            result[-1]["t"]
            + duration
        )
        > duration * 2
    ):
        raise ValueError(
            f"Stale {timeframe} candles"
        )

    return result


# ==========================================
# EMA
# ==========================================

def ema(
    values,
    period
):

    result = (
        sum(values[:period])
        / period
    )

    multiplier = (
        2 / (period + 1)
    )

    for value in values[period:]:

        result += (
            value - result
        ) * multiplier

    return result


# ==========================================
# RSI
# ==========================================

def rsi(
    values,
    period=14
):

    changes = [
        values[i]
        - values[i - 1]
        for i in range(
            1,
            len(values)
        )
    ]

    gain = (
        sum(
            max(x, 0)
            for x in changes[:period]
        )
        / period
    )

    loss = (
        sum(
            max(-x, 0)
            for x in changes[:period]
        )
        / period
    )

    for change in changes[period:]:

        gain = (
            gain * (period - 1)
            + max(change, 0)
        ) / period

        loss = (
            loss * (period - 1)
            + max(-change, 0)
        ) / period

    if loss == 0:

        if gain > 0:
            return 100.0

        return 50.0

    rs = gain / loss

    return (
        100
        - 100 / (1 + rs)
    )


# ==========================================
# ATR
# ==========================================

def atr(
    data,
    period=14
):

    ranges = []

    for i in range(
        len(data) - period,
        len(data)
    ):

        current = data[i]
        previous = data[i - 1]

        true_range = max(

            current["h"]
            - current["l"],

            abs(
                current["h"]
                - previous["c"]
            ),

            abs(
                current["l"]
                - previous["c"]
            )
        )

        ranges.append(
            true_range
        )

    return statistics.mean(
        ranges
    )


# ==========================================
# TREND
# ==========================================

def direction(data):

    closes = [
        x["c"]
        for x in data
    ]

    ema20 = ema(
        closes,
        20
    )

    ema50 = ema(
        closes,
        50
    )

    ema200 = ema(
        closes,
        200
    )

    if (
        ema20
        > ema50
        > ema200
    ):
        return (
            "LONG",
            ema20
        )

    if (
        ema20
        < ema50
        < ema200
    ):
        return (
            "SHORT",
            ema20
        )

    return (
        "WAIT",
        ema20
    )


# ==========================================
# FUTURES DATA
# ==========================================

def futures_data():

    # ---------- PRICE ----------

    ticker = fetch(
        "/api/v2/mix/market/ticker"
    )

    if (
        not isinstance(
            ticker,
            list
        )
        or not ticker
    ):
        raise ValueError(
            "Ticker missing"
        )

    ticker = ticker[0]

    price = number(
        ticker["lastPr"]
    )

    ticker_time = (
        int(ticker["ts"])
        / 1000
    )

    if (
        abs(
            time.time()
            - ticker_time
        )
        > 90
    ):
        raise ValueError(
            "Stale ticker"
        )

    # ---------- ORDER BOOK ----------

    depth = fetch(
        "/api/v2/mix/market/merge-depth",
        {
            "precision": "scale0"
        }
    )

    depth_time = (
        int(depth["ts"])
        / 1000
    )

    if (
        abs(
            time.time()
            - depth_time
        )
        > 90
    ):
        raise ValueError(
            "Stale orderbook"
        )

    bids = depth["bids"][:30]
    asks = depth["asks"][:30]

    if (
        not bids
        or not asks
    ):
        raise ValueError(
            "Empty orderbook"
        )

    bid_volume = sum(
        number(x[1])
        for x in bids
    )

    ask_volume = sum(
        number(x[1])
        for x in asks
    )

    if (
        bid_volume <= 0
        or ask_volume <= 0
    ):
        raise ValueError(
            "Invalid orderbook"
        )

    book_ratio = (
        bid_volume
        / ask_volume
    )

    # ---------- FUNDING ----------

    funding_data = fetch(
        "/api/v2/mix/market/current-fund-rate"
    )

    if (
        not isinstance(
            funding_data,
            list
        )
        or not funding_data
    ):
        raise ValueError(
            "Funding missing"
        )

    funding = number(
        funding_data[0][
            "fundingRate"
        ]
    )

    # ---------- OPEN INTEREST ----------

    oi_data = fetch(
        "/api/v2/mix/market/open-interest"
    )

    oi_list = (
        oi_data.get(
            "openInterestList",
            []
        )
    )

    if not oi_list:
        raise ValueError(
            "Open Interest missing"
        )

    open_interest = number(
        oi_list[0]["size"]
    )

    if (
        price <= 0
        or open_interest <= 0
    ):
        raise ValueError(
            "Invalid price or OI"
        )

    return {
        "price": price,
        "book": book_ratio,
        "funding": funding,
        "oi": open_interest
    }


# ==========================================
# MAIN MARKET ANALYSIS
# ==========================================

def analyze():

    data = {
        "5m": candles(
            "5m",
            300
        ),
        "15m": candles(
            "15m",
            900
        ),
        "1H": candles(
            "1H",
            3600
        ),
        "4H": candles(
            "4H",
            14400
        )
    }

    trends = {}

    for timeframe, values in data.items():
        trends[timeframe] = (
            direction(values)[0]
        )

    # 15m, 1H і 4H повинні
    # показувати один напрямок

    if (
        trends["15m"] == "WAIT"
        or trends["1H"] != trends["15m"]
        or trends["4H"] != trends["15m"]
    ):
        logging.info(
            "WAIT trend %s",
            trends
        )
        return None

    side = trends["15m"]

    candles15 = data["15m"]

    closes = [
        x["c"]
        for x in candles15
    ]

    ema20 = ema(
        closes,
        20
    )

    current_rsi = rsi(
        closes
    )

    current_atr = atr(
        candles15
    )

    if current_atr <= 0:
        return None

    # ---------- VOLUME ----------

    previous_volumes = [
        x["v"]
        for x in candles15[-21:-1]
    ]

    average_volume = statistics.mean(
        previous_volumes
    )

    if average_volume <= 0:
        return None

    volume_ratio = (
        candles15[-1]["v"]
        / average_volume
    )

    # ---------- FUTURES DATA ----------

    market = futures_data()

    price = market["price"]

    # Захист від великої різниці
    # між останньою свічкою і поточною ціною

    if (
        abs(
            price
            - closes[-1]
        )
        / price
        > 0.012
    ):
        logging.info(
            "WAIT price moved too far"
        )
        return None

    # ---------- VOLUME FILTER ----------

    if volume_ratio < 1.20:
        logging.info(
            "WAIT weak volume %.2f",
            volume_ratio
        )
        return None

    # ---------- EMA DISTANCE ----------

    distance_from_ema = abs(
        price - ema20
    )

    if (
        distance_from_ema
        > current_atr * 1.5
    ):
        logging.info(
            "WAIT price too far from EMA20"
        )
        return None

    # ---------- LARGE CANDLE FILTER ----------

    last_candle = candles15[-1]

    candle_body = abs(
        last_candle["c"]
        - last_candle["o"]
    )

    if (
        candle_body
        > current_atr * 1.5
    ):
        logging.info(
            "WAIT extended candle"
        )
        return None

    # ---------- FUNDING FILTER ----------

    if (
        abs(
            market["funding"]
        )
        > 0.001
    ):
        logging.info(
            "WAIT extreme funding"
        )
        return None

    # ---------- LONG ----------

    if side == "LONG":

        if not (
            52
            <= current_rsi
            <= 65
        ):
            logging.info(
                "WAIT LONG RSI %.1f",
                current_rsi
            )
            return None

        if price <= ema20:
            logging.info(
                "WAIT LONG below EMA20"
            )
            return None

        if market["book"] < 1.15:
            logging.info(
                "WAIT LONG weak orderbook %.2f",
                market["book"]
            )
            return None

    # ---------- SHORT ----------

    else:

        if not (
            35
            <= current_rsi
            <= 48
        ):
            logging.info(
                "WAIT SHORT RSI %.1f",
                current_rsi
            )
            return None

        if price >= ema20:
            logging.info(
                "WAIT SHORT above EMA20"
            )
            return None

        if market["book"] > 0.87:
            logging.info(
                "WAIT SHORT weak orderbook %.2f",
                market["book"]
            )
            return None

    # ---------- SUPPORT / RESISTANCE ----------

    recent = candles15[-49:-1]

    resistance = max(
        x["h"]
        for x in recent
    )

    support = min(
        x["l"]
        for x in recent
    )

    # ---------- STOP DISTANCE ----------

    stop_distance = max(
        current_atr * 1.5,
        price * 0.006
    )

    # ---------- TARGET ----------

    if side == "LONG":

        target = (
            price
            + stop_distance * 2
        )

        if (
            resistance > price
            and resistance < target
        ):
            logging.info(
                "WAIT resistance before 2R target"
            )
            return None

    else:

        target = (
            price
            - stop_distance * 2
        )

        if (
            support < price
            and support > target
        ):
            logging.info(
                "WAIT support before 2R target"
            )
            return None

    # ---------- SIGNAL SCORE ----------

    score = 70

    if volume_ratio >= 1.5:
        score += 10
    else:
        score += 5

    if side == "LONG":

        if market["book"] >= 1.40:
            score += 10
        else:
            score += 5

    else:

        if market["book"] <= 0.71:
            score += 10
        else:
            score += 5

    # 5m використовується як
    # додаткове підтвердження

    if trends["5m"] == side:
        score += 5

    score = min(
        score,
        100
    )

    if score < 85:
        logging.info(
            "WAIT score %s",
            score
        )
        return None

    # ---------- STOP ----------

    if side == "LONG":
        stop = (
            price
            - stop_distance
        )
    else:
        stop = (
            price
            + stop_distance
        )

    return {
        "side": side,
        "price": price,
        "stop": stop,
        "take": target,
        "score": score,
        "rsi": current_rsi,
        "volume": volume_ratio,
        "book": market["book"],
        "funding": market["funding"],
        "oi": market["oi"],
        "stamp": candles15[-1]["t"]
    }


# ==========================================
# TELEGRAM
# ==========================================

def telegram(message):

    response = session.post(
        (
            "https://api.telegram.org/"
            f"bot{TOKEN}/sendMessage"
        ),
        data={
            "chat_id": CHAT,
            "text": message
        },
        timeout=15
    )

    response.raise_for_status()

    obj = response.json()

    if not obj.get("ok"):
        raise ValueError(
            f"Telegram error: "
            f"{str(obj)[:180]}"
        )


# ==========================================
# FORMAT SIGNAL
# ==========================================

def format_signal(signal):

    if signal["side"] == "LONG":
        icon = "🟢"
    else:
        icon = "🔴"

    return (
        f"{icon} BTC V4 "
        f"{signal['side']} (ТЕСТ)\n\n"

        f"Вхід ≈ "
        f"${signal['price']:,.2f}\n"

        f"Stop Loss: "
        f"${signal['stop']:,.2f}\n"

        f"Take Profit: "
        f"${signal['take']:,.2f}\n\n"

        f"Сила сигналу: "
        f"{signal['score']}/100\n"

        f"⚠️ Це НЕ відсоток "
        f"імовірності.\n\n"

        f"RSI: "
        f"{signal['rsi']:.1f}\n"

        f"Обсяг: "
        f"{signal['volume']:.2f}x\n"

        f"Стакан bid/ask: "
        f"{signal['book']:.2f}\n"

        f"Funding: "
        f"{signal['funding'] * 100:.4f}%\n"

        f"Open Interest: "
        f"{signal['oi']:,.2f}\n"

        f"R:R = 1:2\n\n"

        f"Перевір актуальну ціну "
        f"на Binance перед входом.\n"

        f"Ризик збитку залишається."
    )


# ==========================================
# MAIN
# ==========================================

def main():

    logging.info(
        "BTC V4 starting - alerts only"
    )

    telegram(
        "✅ BTC Signal Bot V4 запущений!\n\n"
        "Джерело: Bitget Futures.\n"
        "Таймфрейми: 5m / 15m / 1h / 4h.\n"
        "Режим: тільки сильні сигнали.\n"
        "Автоматичні угоди ВИМКНЕНІ."
    )

    last_signal = None

    if STATE.exists():

            try:
            saved = json.loads(
                STATE.read_text()
            )
            last_signal = saved.get(
                "id"
            )

        except Exception:
            pass

    while True:

        try:
            signal = analyze()

            if signal:

                signal_id = (
                    f"{signal['side']}:"
                    f"{signal['stamp']}"
                )

                if signal_id != last_signal:

                    telegram(
                        format_signal(
                            signal
                        )
                    )

                    last_signal = signal_id

                    STATE.write_text(
                        json.dumps({
                            "id": signal_id
                        })
                    )

                    logging.info(
                        "Signal sent %s",
                        signal_id
                    )

            else:
                logging.info(
                    "WAIT - no qualified setup"
                )

        except Exception:
            logging.exception(
                "Data/analysis error - NO SIGNAL"
            )

        time.sleep(
            INTERVAL
        )


if __name__ == "__main__":
    main()
