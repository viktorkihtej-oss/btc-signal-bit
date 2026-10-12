"""BTC Signal Bot V4.2: alerts only, no trading credentials or orders."""
import json
import logging
import math
import os
import statistics
import time
from pathlib import Path

import requests

VERSION = '4.2'
BASE = 'https://api.bitget.com'
SYMBOL = 'BTCUSDT'
PRODUCT = 'USDT-FUTURES'
INTERVAL = 60
MIN_SCORE = 80
STATE = Path('/tmp/btc_signal_v4_2.json')
LOG = logging.getLogger('btc_v4_2')
HTTP = requests.Session()
HTTP.headers.update({'User-Agent': 'BTCSignalBot/4.2', 'Accept': 'application/json'})


def num(value):
    x = float(value)
    if not math.isfinite(x):
        raise ValueError('Non-finite numeric value')
    return x


def get(path, extra=None):
    params = {'symbol': SYMBOL, 'productType': PRODUCT}
    if extra:
        params.update(extra)
    response = HTTP.get(BASE + path, params=params, timeout=15)
    response.raise_for_status()
    payload = response.json()
    if payload.get('code') != '00000' or payload.get('data') is None:
        raise ValueError(f'Bitget {path}: {str(payload)[:180]}')
    return payload['data']


def telegram(message):
    token, chat = os.environ['BOT_TOKEN'], os.environ['CHAT_ID']
    response = HTTP.post(
        f'https://api.telegram.org/bot{token}/sendMessage',
        data={'chat_id': chat, 'text': message},
        timeout=15,
    )
    response.raise_for_status()
    if not response.json().get('ok'):
        raise ValueError('Telegram rejected message')


def candles(tf, seconds):
    rows = get('/api/v2/mix/market/candles', {'granularity': tf, 'limit': '260'})
    now = time.time()
    found = {}
    for row in rows:
        stamp = int(row[0]) // 1000
        if stamp + seconds > now - 3:
            continue
        o, h, l, c, v = (num(x) for x in row[1:6])
        if not (0 < l <= min(o, c) <= max(o, c) <= h and v >= 0):
            raise ValueError(f'Invalid {tf} OHLCV')
        found[stamp] = {'t': stamp, 'o': o, 'h': h, 'l': l, 'c': c, 'v': v}
    data = [found[t] for t in sorted(found)]
    if len(data) < 205:
        raise ValueError(f'Insufficient {tf} candles: {len(data)}')
    if now - (data[-1]['t'] + seconds) > seconds * 2:
        raise ValueError(f'Stale {tf} candles')
    if any(data[i]['t'] - data[i - 1]['t'] != seconds for i in range(1, len(data))):
        raise ValueError(f'Gaps in {tf} candles')
    return data


def ema(values, period):
    if len(values) < period:
        raise ValueError('EMA insufficient data')
    result = sum(values[:period]) / period
    for value in values[period:]:
        result += (value - result) * 2 / (period + 1)
    return result


def rsi(values, period=14):
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gain = sum(max(x, 0) for x in changes[:period]) / period
    loss = sum(max(-x, 0) for x in changes[:period]) / period
    for change in changes[period:]:
        gain = (gain * (period - 1) + max(change, 0)) / period
        loss = (loss * (period - 1) + max(-change, 0)) / period
    if loss == 0:
        return 100.0 if gain else 50.0
    return 100 - 100 / (1 + gain / loss)


def atr(data, period=14):
    ranges = []
    for i in range(len(data) - period, len(data)):
        c, p = data[i], data[i - 1]
        ranges.append(max(c['h'] - c['l'], abs(c['h'] - p['c']), abs(c['l'] - p['c'])))
    return statistics.mean(ranges)


def macd_hist(values):
    def series(period):
        k = 2 / (period + 1)
        result = values[0]
        out = []
        for x in values:
            result += k * (x - result)
            out.append(result)
        return out
    fast, slow = series(12), series(26)
    line = [a - b for a, b in zip(fast, slow)]
    signal = line[0]
    hist = []
    for x in line:
        signal += 0.2 * (x - signal)
        hist.append(x - signal)
    return hist[-1], hist[-2]


def trend(data):
    closes = [x['c'] for x in data]
    e20, e50, e200 = (ema(closes, n) for n in (20, 50, 200))
    if e20 > e50 > e200:
        return 'LONG', e20
    if e20 < e50 < e200:
        return 'SHORT', e20
    return 'WAIT', e20


def market():
    ticker = get('/api/v2/mix/market/ticker')
    if not isinstance(ticker, list) or not ticker:
        raise ValueError('Missing ticker')
    ticker = ticker[0]
    price = num(ticker['lastPr'])
    if price <= 0 or abs(time.time() - int(ticker['ts']) / 1000) > 90:
        raise ValueError('Invalid or stale ticker')

    depth = get('/api/v2/mix/market/merge-depth', {'precision': 'scale0', 'limit': '50'})
    if abs(time.time() - int(depth['ts']) / 1000) > 90:
        raise ValueError('Stale order book')
    bids, asks = depth['bids'][:30], depth['asks'][:30]
    if len(bids) < 5 or len(asks) < 5:
        raise ValueError('Insufficient order book')
    if num(bids[0][0]) >= num(asks[0][0]):
        raise ValueError('Crossed order book')
    bv = sum(num(row[1]) for row in bids)
    av = sum(num(row[1]) for row in asks)
    if bv <= 0 or av <= 0:
        raise ValueError('Invalid order book sizes')

    funding_data = get('/api/v2/mix/market/current-fund-rate')
    if not isinstance(funding_data, list) or not funding_data:
        raise ValueError('Missing funding')
    funding = num(funding_data[0]['fundingRate'])

    oi_data = get('/api/v2/mix/market/open-interest')
    oi_list = oi_data['openInterestList']
    if not oi_list:
        raise ValueError('Missing open interest')
    oi = num(oi_list[0]['size'])
    if oi <= 0:
        raise ValueError('Invalid open interest')

    trades = get('/api/v2/mix/market/fills', {'limit': '100'})
    now = time.time()
    recent = [t for t in trades if 0 <= now - int(t['ts']) / 1000 <= 120]
    if len(recent) < 10:
        raise ValueError('Too few recent trades')
    buy = sum(num(t['size']) * num(t['price']) for t in recent if t['side'] == 'buy')
    sell = sum(num(t['size']) * num(t['price']) for t in recent if t['side'] == 'sell')
    if buy + sell <= 0:
        raise ValueError('Empty trade flow')

    return {
        'price': price,
        'book': bv / av,
        'funding': funding,
        'oi': oi,
        'buy_share': buy / (buy + sell),
    }


def analyze():
    frames = {
        '5m': candles('5m', 300),
        '15m': candles('15m', 900),
        '1H': candles('1H', 3600),
        '4H': candles('4H', 14400),
    }
    trends = {name: trend(rows)[0] for name, rows in frames.items()}

    # V4.2: 15m remains the trigger. 1H must agree.
    # 4H is no longer mandatory; it may agree or be WAIT, but must not oppose.
    side = trends['15m']
    if side == 'WAIT' or trends['1H'] != side or trends['4H'] not in (side, 'WAIT'):
        LOG.info('WAIT: trend disagreement %s', trends)
        return None

    c15 = frames['15m']
    closes = [x['c'] for x in c15]
    e20 = ema(closes, 20)
    strength = rsi(closes)
    volatility = atr(c15)
    if volatility <= 0:
        return None

    mean_vol = statistics.mean(x['v'] for x in c15[-21:-1])
    if mean_vol <= 0:
        return None
    vol_ratio = c15[-1]['v'] / mean_vol
    hist, prev_hist = macd_hist(closes)

    # Less strict than V4.1: 1.20 -> 1.05; oversized candle 1.5 ATR -> 1.8 ATR.
    if vol_ratio < 1.05 or abs(c15[-1]['c'] - c15[-1]['o']) > 1.8 * volatility:
        LOG.info('WAIT: volume or oversized candle')
        return None

    m = market()
    price = m['price']

    # Less strict: 0.6% -> 0.8%, EMA distance 1.5 ATR -> 2.0 ATR.
    if abs(price - closes[-1]) / price > 0.008:
        LOG.info('WAIT: price deviated from last closed candle')
        return None
    if abs(price - e20) > 2.0 * volatility or abs(m['funding']) > 0.001:
        LOG.info('WAIT: stretched price or extreme funding')
        return None

    # Less strict RSI / order-book / aggressive-flow thresholds.
    # MACD must still point in the trade direction, but need not accelerate.
    if side == 'LONG':
        if not (
            48 <= strength <= 72
            and price > e20
            and m['book'] >= 1.05
            and m['buy_share'] >= 0.50
            and hist > 0
        ):
            LOG.info('WAIT: LONG confirmation rsi=%.1f book=%.2f buy=%.2f hist=%.2f',
                     strength, m['book'], m['buy_share'], hist)
            return None
    else:
        if not (
            28 <= strength <= 52
            and price < e20
            and m['book'] <= 0.95
            and m['buy_share'] <= 0.50
            and hist < 0
        ):
            LOG.info('WAIT: SHORT confirmation rsi=%.1f book=%.2f buy=%.2f hist=%.2f',
                     strength, m['book'], m['buy_share'], hist)
            return None

    # Keep V4.1 risk management: minimum 0.6% / 1.5 ATR stop, TP at 2R.
    distance = max(volatility * 1.5, price * 0.006)
    stop = price - distance if side == 'LONG' else price + distance
    take = price + 2 * distance if side == 'LONG' else price - 2 * distance

    # V4.2 does not reject a signal merely because an old 15m extreme lies
    # between entry and the full 2R target. It is still a test alert.
    score = 70
    score += 10 if vol_ratio >= 1.4 else 5
    score += 10 if (m['book'] >= 1.30 if side == 'LONG' else m['book'] <= 0.77) else 5
    score += 5 if trends['5m'] == side else 0
    score += 5 if (m['buy_share'] >= 0.57 if side == 'LONG' else m['buy_share'] <= 0.43) else 0
    score = min(score, 100)

    if score < MIN_SCORE:
        LOG.info('WAIT: score %d < %d', score, MIN_SCORE)
        return None

    return {
        'side': side,
        'price': price,
        'stop': stop,
        'take': take,
        'score': score,
        'rsi': strength,
        'vol': vol_ratio,
        'book': m['book'],
        'buy_share': m['buy_share'],
        'funding': m['funding'],
        'oi': m['oi'],
        'stamp': c15[-1]['t'],
    }


def format_signal(s):
    icon = '🟢' if s['side'] == 'LONG' else '🔴'
    return (
        f"{icon} BTC Signal Bot V{VERSION} — {s['side']} (ТЕСТ)\n\n"
        f"Вхід ≈ ${s['price']:,.2f}\n"
        f"Стоп: ${s['stop']:,.2f}\n"
        f"Тейк: ${s['take']:,.2f}\n"
        f"Ризик/прибуток: 1:2 (до комісій)\n\n"
        f"Сила: {s['score']}/100 балів (НЕ ймовірність прибутку)\n"
        f"RSI: {s['rsi']:.1f}; обсяг: {s['vol']:.2f}x\n"
        f"Стакан bid/ask: {s['book']:.2f}\n"
        f"Агресивні покупки: {s['buy_share'] * 100:.1f}%\n"
        f"Funding: {s['funding'] * 100:.4f}%\n"
        f"Open Interest: {s['oi']:,.2f}\n\n"
        "Перевір актуальну ціну на Binance. Сигнал не гарантує прибуток."
    )


def load_state():
    try:
        obj = json.loads(STATE.read_text(encoding='utf-8'))
        return obj.get('last_id')
    except (OSError, ValueError, TypeError):
        return None


def save_state(signal_id):
    try:
        STATE.write_text(json.dumps({'last_id': signal_id}), encoding='utf-8')
    except OSError as error:
        LOG.warning('Could not save ephemeral state: %s', error)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
    )
    LOG.info('BTC Signal Bot V%s starting (alerts only)', VERSION)
    last_id = load_state()

    try:
        telegram(
            f'✅ BTC Signal Bot V{VERSION} запущено. '
            'Умови входу м’якші за V4.1. Тестові сигнали, без автоторгівлі.'
        )
    except Exception:
        LOG.exception('Startup Telegram notification failed')

    while True:
        try:
            signal = analyze()
            if signal:
                signal_id = f"{signal['side']}:{signal['stamp']}"
                if signal_id != last_id:
                    telegram(format_signal(signal))
                    last_id = signal_id
                    save_state(last_id)
                    LOG.info('SIGNAL SENT %s', signal_id)
                else:
                    LOG.info('WAIT: duplicate signal %s', signal_id)
            else:
                LOG.info('WAIT: no qualified setup')
        except Exception:
            LOG.exception('DATA/ANALYSIS ERROR — NO SIGNAL')

        time.sleep(INTERVAL)


if __name__ == '__main__':
    main()
