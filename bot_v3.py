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
   
