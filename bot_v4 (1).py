import os, time, json, logging, statistics
from pathlib import Path
import requests

TOKEN = os.environ['BOT_TOKEN']
CHAT = os.environ['CHAT_ID']
BASE = 'https://api.bitget.com'
SYMBOL = 'BTCUSDT'
PRODUCT = 'USDT-FUTURES'
INTERVAL = 60
STATE = Path('/tmp/btc_v4_state.json')  # temporary; no durable storage guarantee
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
s = requests.Session()
s.headers.update({'User-Agent': 'BTCSignalBot/4.0', 'Accept': 'application/json'})

def fetch(path, params=None):
    p = {'symbol': SYMBOL, 'productType': PRODUCT}
    p.update(params or {})
    r = s.get(BASE + path, params=p, timeout=15)
    r.raise_for_status()
    obj = r.json()
    if obj.get('code') != '00000' or obj.get('data') is None:
        raise ValueError(f'API {path}: {str(obj)[:180]}')
    return obj['data']

def number(value):
    x = float(value)
    if not (-1e100 < x < 1e100):
        raise ValueError('Non-finite number')
    return x

def candles(tf, duration):
    raw = fetch('/api/v2/mix/market/candles', {'granularity': tf, 'limit': '260'})
    now = time.time()
    result = []
    for a in raw:
        t = int(a[0]) / 1000
        if t + duration > now - 3:
            continue
        c = {'t': int(t), 'o': number(a[1]), 'h': number(a[2]),
             'l': number(a[3]), 'c': number(a[4]), 'v': number(a[5])}
        if c['l'] <= min(c['o'], c['c']) <= max(c['o'], c['c']) <= c['h'] and c['v'] >= 0:
            result.append(c)
    result.sort(key=lambda x: x['t'])
    result = list({x['t']: x for x in result}.values())
    if len(result) < 205 or now - (result[-1]['t'] + duration) > duration * 2:
        raise ValueError(f'Insufficient/stale {tf} candles: {len(result)}')
    return result

def ema(v, n):
    a = sum(v[:n]) / n
    for x in v[n:]:
        a += (x - a) * (2 / (n + 1))
    return a

def rsi(v, n=14):
    d = [v[i] - v[i-1] for i in range(1, len(v))]
    g = sum(max(x, 0) for x in d[:n]) / n
    l = sum(max(-x, 0) for x in d[:n]) / n
    for x in d[n:]:
        g = (g * (n-1) + max(x, 0)) / n
        l = (l * (n-1) + max(-x, 0)) / n
    return 100 - 100 / (1 + g/l) if l else (100 if g else 50)

def atr(v, n=14):
    return statistics.mean(max(v[i]['h'] - v[i]['l'],
        abs(v[i]['h'] - v[i-1]['c']), abs(v[i]['l'] - v[i-1]['c']))
        for i in range(len(v)-n, len(v)))

def direction(v):
    c = [x['c'] for x in v]
    a, b, d = ema(c, 20), ema(c, 50), ema(c, 200)
    return ('LONG' if a > b > d else 'SHORT' if a < b < d else 'WAIT'), a

def extras():
    ticker = fetch('/api/v2/mix/market/ticker')
    if not isinstance(ticker, list) or not ticker:
        raise ValueError('Ticker missing')
    t = ticker[0]
    price = number(t['lastPr'])
    if abs(time.time() - int(t['ts'])/1000) > 90:
        raise ValueError('Stale ticker')
    depth = fetch('/api/v2/mix/market/merge-depth', {'precision': 'scale0'})
    if abs(time.time() - int(depth['ts'])/1000) > 90:
        raise ValueError('Stale depth')
    bids, asks = depth['bids'][:30], depth['asks'][:30]
    if not bids or not asks:
        raise ValueError('Empty depth')
    bid = sum(number(x[1]) for x in bids)
    ask = sum(number(x[1]) for x in asks)
    if min(bid, ask) <= 0:
        raise ValueError('Invalid depth')
    fund = fetch('/api/v2/mix/market/current-fund-rate')
    if not isinstance(fund, list) or not fund:
        raise ValueError('Missing funding')
    funding = number(fund[0]['fundingRate'])
    oi = fetch('/api/v2/mix/market/open-interest')
    open_interest = number(oi['openInterestList'][0]['size'])
    if price <= 0 or open_interest <= 0:
        raise ValueError('Invalid price or OI')
    return {'price': price, 'book': bid/ask, 'funding': funding, 'oi': open_interest}

def analyze():
    data = {tf: candles(tf, sec) for tf, sec in [('5m',300),('15m',900),('1H',3600),('4H',14400)]}
    trends = {tf: direction(v)[0] for tf, v in data.items()}
    if trends['15m'] == 'WAIT' or any(trends[x] != trends['15m'] for x in ('1H','4H')):
        logging.info('WAIT trend %s', trends)
        return None
    side = trends['15m']
    c15 = data['15m']
    close = [x['c'] for x in c15]
    e20 = ema(close, 20)
    strength = rsi(close)
    volatility = atr(c15)
    volume_base = statistics.mean(x['v'] for x in c15[-21:-1])
    if volume_base <= 0 or volatility <= 0:
        return None
    volume = c15[-1]['v'] / volume_base
    m = extras()  # fail closed if any required feed unavailable
    price = m['price']
    if abs(price - close[-1]) / price > 0.012:
        logging.info('WAIT price moved too far since closed candle')
        return None
    if volume < 1.2 or abs(price - e20) > 1.5 * volatility:
        logging.info('WAIT volume/extension %.2f', volume)
        return None
    if abs(c15[-1]['c'] - c15[-1]['o']) > 1.5 * volatility:
        return None
    if abs(m['funding']) > 0.001:
        return None
    if side == 'LONG':
        if not (52 <= strength <= 65 and price > e20 and m['book'] >= 1.15):
            return None
    else:
        if not (35 <= strength <= 48 and price < e20 and m['book'] <= 0.87):
            return None
    # Avoid placing take profit beyond nearby resistance/support from recent candles.
    recent = c15[-49:-1]
    resistance = max(x['h'] for x in recent)
    support = min(x['l'] for x in recent)
    distance = max(volatility * 1.5, price * 0.006)
    target = price + 2 * distance if side == 'LONG' else price - 2 * distance
    if (side == 'LONG' and resistance > price and resistance < target) or (side == 'SHORT' and support < price and support > target):
        logging.info('WAIT nearby obstacle before 2R target')
        return None
    score = min(100, 70 + (10 if volume >= 1.5 else 5) +
                (10 if (m['book'] >= 1.4 if side == 'LONG' else m['book'] <= 0.71) else 5) +
                (5 if trends['5m'] == side else 0))
    if score < 85:
        return None
    return {'side': side, 'price': price, 'stop': price - distance if side == 'LONG' else price + distance,
            'take': target, 'score': score, 'rsi': strength, 'volume': volume,
            'book': m['book'], 'funding': m['funding'], 'oi': m['oi'], 'stamp': c15[-1]['t']}

def telegram(message):
    r = s.post(f'https://api.telegram.org/bot{TOKEN}/sendMessage',
               data={'chat_id': CHAT, 'text': message}, timeout=15)
    r.raise_for_status()
    obj = r.json()
    if not obj.get('ok'):
        raise ValueError(f'Telegram: {str(obj)[:180]}')

def message(a):
    return (f"{'🟢' if a['side']=='LONG' else '🔴'} BTC V4 {a['side']} (ТЕСТ)\n"
        f"Вхід ≈ ${a['price']:,.2f}\nСтоп ${a['stop']:,.2f}\nТейк ${a['take']:,.2f}\n"
        f"Сила {a['score']}/100 (НЕ ймовірність)\nRSI {a['rsi']:.1f} | Обсяг {a['volume']:.2f}x\n"
        f"Стакан bid/ask {a['book']:.2f} | Funding {a['funding']*100:.4f}%\n"
        f"OI {a['oi']:,.2f} (рівень, не зміна)\nR:R 1:2\n"
        'Не автоматична угода. Перевір ціну Binance; ризик збитку залишається.')

def main():
    logging.info('BTC V4 starting; alerts only, not trading')
    telegram('✅ BTC V4 запущено. Режим тестових сигналів. Дані: Bitget Futures.')
    last = None
    if STATE.exists():
        try:
            last = json.loads(STATE.read_text()).get('id')
        except Exception:
            pass
    while True:
        try:
            a = analyze()
            if a:
                key = f"{a['side']}:{a['stamp']}"
                if key != last:
                    telegram(message(a))
                    last = key
                    STATE.write_text(json.dumps({'id': key}))
                    logging.info('Signal sent %s', key)
            else:
                logging.info('WAIT - no qualified setup')
        except Exception:
            logging.exception('Data or analysis error: no signal sent')
        time.sleep(INTERVAL)

if __name__ == '__main__':
    main()
