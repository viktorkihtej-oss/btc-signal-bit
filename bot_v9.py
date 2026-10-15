#!/usr/bin/env python3
"""BTC Signal V7 — Ukrainian alerts-only research bot. No exchange credentials/orders.
Python 3.10+, requests. Offline tests: python -m unittest test_bot_v7.py -v
Public market endpoints; never represents unobserved news or data as verified.
"""
from __future__ import annotations
import hashlib
import hmac
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
import os
import signal
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

VERSION = '9.0'
API = 'https://api.bitget.com'
TF = {'1m': 60, '5m': 300, '15m': 900, '1H': 3600, '4H': 14400}
LOG = logging.getLogger('btc_v9')


def env_float(key, default):
    try: return float(os.getenv(key, str(default)))
    except ValueError: raise ValueError(f'Невірне число у {key}')


def env_int(key, default):
    try: return int(os.getenv(key, str(default)))
    except ValueError: raise ValueError(f'Невірне ціле число у {key}')


@dataclass(frozen=True)
class Config:
    symbol: str = os.getenv('SYMBOL', 'BTCUSDT').upper()
    interval: int = env_int('CHECK_SECONDS', 60)
    equity: float = env_float('REFERENCE_EQUITY', 1000)
    risk_fraction: float = env_float('RISK_PCT', 0.005)
    max_notional: float = env_float('MAX_NOTIONAL_MULT', 1.5)
    min_rr: float = env_float('MIN_RR', 2.0)
    min_score: int = env_int('MIN_SCORE', 68)
    cooldown_minutes: int = env_int('COOLDOWN_MINUTES', 45)
    max_daily_alerts: int = env_int('MAX_DAILY_ALERTS', 6)
    heartbeat_hours: int = env_int('HEARTBEAT_HOURS', 6)
    db_path: str = os.getenv('DB_PATH', '/tmp/btc_signal_v9.sqlite3')
    token: str = os.getenv('BOT_TOKEN', '')
    chat_id: str = os.getenv('CHAT_ID', '')
    # unknown means there is NO live news monitoring. Don't claim otherwise.
    event_risk: str = os.getenv('EVENT_RISK', 'unknown').lower()
    require_news_clearance: bool = os.getenv('REQUIRE_NEWS_CLEARANCE', '0') == '1'
    # If true, no trade alerts while news verification is unavailable.
    enable_eth: bool = os.getenv('ENABLE_ETH', '0') == '1'
    # 0: send useful setup alerts even if risk is unknown; label uncertainty clearly.
    max_spread_bps: float = env_float('MAX_SPREAD_BPS', 12)
    fee_rate_side: float = env_float('TAKER_FEE_RATE', 0.0006)
    slippage_bps_side: float = env_float('SLIPPAGE_BPS', 3)
    min_net_rr: float = env_float('MIN_NET_RR', 1.3)
    min_notional_usdt: float = env_float('MIN_NOTIONAL_USDT', 5)
    paper_enabled: bool = os.getenv('PAPER_TRADING', '1') == '1'
    max_open_paper: int = env_int('MAX_OPEN_PAPER', 1)
    max_hold_hours: int = env_int('MAX_HOLD_HOURS', 24)
    daily_loss_r: float = env_float('DAILY_LOSS_R', 3.0)
    leverage: float = env_float('REFERENCE_LEVERAGE', 5.0)
    tv_secret: str = os.getenv('TV_WEBHOOK_SECRET', '')
    tv_port: int = env_int('TV_WEBHOOK_PORT', 0)
    tv_required: bool = os.getenv('TV_REQUIRED', '0') == '1'
    tv_ttl_minutes: int = env_int('TV_TTL_MINUTES', 30)
    dry_run: bool = os.getenv('DRY_RUN', '1') == '1'
    paper_bar_tf: str = os.getenv('PAPER_BAR_TF', '1m')

    def validate(self):
        if self.symbol not in ('BTCUSDT', 'ETHUSDT'): raise ValueError('SYMBOL: BTCUSDT або ETHUSDT')
        if self.interval < 60: raise ValueError('CHECK_SECONDS >= 60')
        if not (self.equity > 0 and 0 < self.risk_fraction <= .01 and 0 < self.max_notional <= 3):
            raise ValueError('Некоректні параметри ризику')
        if not (1.5 <= self.min_rr <= 5 and 50 <= self.min_score <= 95):
            raise ValueError('Некоректні пороги')
        if self.event_risk not in ('unknown', 'normal', 'elevated', 'critical'):
            raise ValueError('EVENT_RISK має бути unknown/normal/elevated/critical')
        if self.cooldown_minutes < 15 or self.max_daily_alerts < 1 or self.heartbeat_hours < 1:
            raise ValueError('Некоректні обмеження повідомлень')
        if not (1 <= self.leverage <= 5) or self.max_notional > self.leverage:
            raise ValueError('Плече/номінал перевищують обмеження')
        if self.tv_port and (not self.tv_secret or len(self.tv_secret) < 16):
            raise ValueError('Для webhook потрібен TV_WEBHOOK_SECRET (16+ символів)')
        if self.tv_required and not self.tv_port:
            raise ValueError('TV_REQUIRED=1 потребує TV_WEBHOOK_PORT')
        if self.tv_ttl_minutes < 1 or self.tv_ttl_minutes > 240:
            raise ValueError('TV_TTL_MINUTES 1..240')
        if self.fee_rate_side < 0 or self.slippage_bps_side < 0 or self.min_net_rr < 1:
            raise ValueError('Некоректні витрати/пороги')


class MarketError(RuntimeError): pass


def finite(value, name='value'):
    try: v = float(value)
    except (TypeError, ValueError): raise MarketError(f'Некоректне число {name}')
    if not math.isfinite(v): raise MarketError(f'Не скінченне число {name}')
    return v


class Bitget:
    def __init__(self, session=None):
        self.session = session or requests.Session()
        self.session.headers.update({'User-Agent': f'BTCSignalResearch/{VERSION}', 'Accept': 'application/json'})
        # GET retry only, not Telegram POST (to avoid duplicate messages).
        adapter = HTTPAdapter(max_retries=Retry(total=2, backoff_factor=.6,
            status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset(['GET'])))
        self.session.mount('https://api.bitget.com/', adapter)

    def get(self, path, params):
        try:
            r = self.session.get(API + path, params=params, timeout=(5, 12))
            r.raise_for_status()
            obj = r.json()
        except (requests.RequestException, ValueError) as e:
            raise MarketError(f'API HTTP/JSON {path}: {type(e).__name__}') from e
        if obj.get('code') != '00000' or obj.get('data') is None:
            raise MarketError(f'Bitget {path}: code={obj.get("code")}')
        return obj['data']

    def candles(self, symbol, tf):
        # Bitget v2 documented API. Only fully CLOSED candles are used.
        raw = self.get('/api/v2/mix/market/candles', {
            'symbol': symbol, 'productType': 'USDT-FUTURES', 'granularity': tf, 'limit': '200'})
        now_ms = int(time.time() * 1000)
        out = {}
        for x in raw:
            try:
                t = int(x[0]); o,h,l,c,v = [finite(z) for z in x[1:6]]
                if t + TF[tf]*1000 > now_ms - 2000: continue
                if min(o,h,l,c) <= 0 or l > min(o,c) or h < max(o,c) or v < 0: continue
                out[t] = dict(t=t,o=o,h=h,l=l,c=c,v=v)
            except (ValueError, IndexError, TypeError, MarketError): continue
        rows = [out[t] for t in sorted(out)]
        if len(rows) < (2 if tf=='1m' else 110): raise MarketError(f'Мало закритих свічок {symbol}/{tf}: {len(rows)}')
        if now_ms - rows[-1]['t'] > 3 * TF[tf] * 1000:
            raise MarketError(f'Застарілі свічки {symbol}/{tf}')
        return rows

    def book(self, symbol, kind):
        if kind == 'spot':
            path = '/api/v2/spot/market/orderbook'
            params = {'symbol': symbol, 'limit': '50'}
        else:
            path = '/api/v2/mix/market/merge-depth'
            params = {'symbol': symbol, 'productType': 'USDT-FUTURES', 'limit': '50'}
        data = self.get(path, params)
        return parse_book(data)

    def ticker(self, symbol):
        data = self.get('/api/v2/mix/market/ticker', {
            'symbol': symbol, 'productType': 'USDT-FUTURES'})
        if not isinstance(data, list) or not data: raise MarketError('Немає ticker')
        x = data[0]
        p = finite(x.get('lastPr'), 'ticker')
        if p <= 0: raise MarketError('Некоректна ціна')
        ts = int(x.get('ts', 0))
        if abs(time.time()*1000-ts) > 120000: raise MarketError('Застарілий ticker')
        return p

    def recent_trades(self, symbol, kind):
        if kind == 'spot':
            path = '/api/v2/spot/market/fills'; params = {'symbol': symbol, 'limit': '100'}
        else:
            path = '/api/v2/mix/market/fills'; params = {
                'symbol': symbol, 'productType': 'USDT-FUTURES', 'limit': '100'}
        rows = self.get(path, params)
        return parse_trades(rows)

    def oi(self, symbol):
        data = self.get('/api/v2/mix/market/open-interest', {
            'symbol': symbol, 'productType': 'USDT-FUTURES'})
        values = data.get('openInterestList', [])
        if not values: raise MarketError('Open Interest порожній')
        return finite(values[0].get('size'), 'OI')

    def funding(self, symbol):
        data = self.get('/api/v2/mix/market/current-fund-rate', {
            'symbol': symbol, 'productType': 'USDT-FUTURES'})
        if not isinstance(data, list) or not data: raise MarketError('Funding порожній')
        return finite(data[0].get('fundingRate'), 'funding')


def parse_book(data):
    try:
        ts = int(data['ts'])
        if abs(time.time()*1000-ts) > 120000: raise MarketError('Застарілий стакан')
        bids = [(finite(p),finite(q)) for p,q,*_ in data['bids'][:20]]
        asks = [(finite(p),finite(q)) for p,q,*_ in data['asks'][:20]]
        if not bids or not asks: raise MarketError('Порожній стакан')
        if min(p for p,q in bids+asks) <= 0 or min(q for p,q in bids+asks) < 0:
            raise MarketError('Невірний стакан')
        bid, ask = bids[0][0], asks[0][0]
        if bid >= ask: raise MarketError('Перехрещений стакан')
        b = sum(p*q for p,q in bids); a = sum(p*q for p,q in asks)
        if b+a <= 0: raise MarketError('Нульова ліквідність')
        return {'imbalance': (b-a)/(b+a), 'spread_bps': 10000*(ask-bid)/((ask+bid)/2),
                'mid': (ask+bid)/2}
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise MarketError('Некоректний формат стакана') from e


def parse_trades(data):
    if not isinstance(data,list): raise MarketError('Невірні угоди')
    now_ms = int(time.time()*1000)
    buys = sells = 0.0; count = 0
    for x in data:
        try:
            if now_ms - int(x['ts']) > 180000 or int(x['ts']) > now_ms + 30000: continue
            val = finite(x['price'])*finite(x['size'])
            if val < 0: continue
            side = str(x['side']).lower()
            if side == 'buy': buys += val; count += 1
            elif side == 'sell': sells += val; count += 1
        except (KeyError, TypeError, ValueError, MarketError): continue
    if count < 5 or buys+sells <= 0: raise MarketError('Замало свіжих угод для tape')
    return {'delta': (buys-sells)/(buys+sells), 'count':count,
            'buy_share':buys/(buys+sells)}


def ema(values, period):
    k=2/(period+1); result=[values[0]]
    for v in values[1:]: result.append(k*v+(1-k)*result[-1])
    return result


def rsi(values, n=14):
    # Wilder smoothing, not a simple 14-bar average.
    d=[values[i]-values[i-1] for i in range(1,len(values))]
    up=sum(max(x,0) for x in d[:n])/n
    dn=sum(max(-x,0) for x in d[:n])/n
    for x in d[n:]:
        up=(up*(n-1)+max(x,0))/n
        dn=(dn*(n-1)+max(-x,0))/n
    if dn==0: return 100.0 if up else 50.0
    return 100-100/(1+up/dn)


def atr(rows,n=14):
    tr=[max(rows[i]['h']-rows[i]['l'], abs(rows[i]['h']-rows[i-1]['c']),
            abs(rows[i]['l']-rows[i-1]['c'])) for i in range(1,len(rows))]
    a=sum(tr[:n])/n
    for x in tr[n:]: a=(a*(n-1)+x)/n
    return a


def macd_hist(values):
    """MACD(12,26,9) histogram; use closed candles only."""
    a=ema(values,12); b=ema(values,26)
    line=[x-y for x,y in zip(a,b)]
    signal=ema(line,9)
    return line[-1]-signal[-1]


def features(rows):
    closes=[r['c'] for r in rows]; e20=ema(closes,20); e50=ema(closes,50); e200=ema(closes,200)
    a=atr(rows)
    prior=rows[-21:-1]
    return {'price':closes[-1], 'ema20':e20[-1], 'ema50':e50[-1], 'ema200':e200[-1], 'macd_hist':macd_hist(closes),
            'slope':(e20[-1]-e20[-6])/max(a,1e-9), 'atr':a,
            'rsi':rsi(closes), 'hi':max(r['h'] for r in prior),
            'lo':min(r['l'] for r in prior), 'volratio':rows[-1]['v']/max(statistics.mean(r['v'] for r in prior),1e-9),
            'last':rows[-1], 'previous':rows[-2]}


def regime(f1,f4):
    up=f1['ema20']>f1['ema50'] and f4['ema20']>f4['ema50']
    down=f1['ema20']<f1['ema50'] and f4['ema20']<f4['ema50']
    if up and f1['slope']>.08: return 'UP'
    if down and f1['slope']<-.08: return 'DOWN'
    return 'RANGE'


def propose(m, spot_book, fut_book, spot_tape, fut_tape, fund, oi_delta, cfg):
    f5,f15,f1,f4=[m[k] for k in ('5m','15m','1H','4H')]
    mode=regime(f1,f4)
    price=f5['price']; a=f15['atr']
    if a<=0 or spot_book['spread_bps']>cfg.max_spread_bps or fut_book['spread_bps']>cfg.max_spread_bps:
        return None, 'spread/ATR'
    proposals=[]
    for side,sign in [('LONG',1),('SHORT',-1)]:
        trend=(mode=='UP' and sign==1) or (mode=='DOWN' and sign==-1)
        aligned5=sign*(f5['ema20']-f5['ema50'])>0
        breakout=(price>f15['hi']+a*.04 if sign==1 else price<f15['lo']-a*.04)
        pullback=abs(price-f15['ema20'])<.65*a and sign*(f5['last']['c']-f5['last']['o'])>0 and aligned5
        # Reversal requires a wick rejection of range edge on CLOSED 15m candle.
        candle=f15['last']
        rejection=(candle['l']<=f15['lo']+.2*a and candle['c']>f15['lo']+.35*a) if sign==1 else (
            candle['h']>=f15['hi']-.2*a and candle['c']<f15['hi']-.35*a)
        if trend and breakout: strategy='Пробій за трендом'
        elif trend and pullback: strategy='Відкат за трендом'
        elif mode=='RANGE' and rejection and ((sign==1 and f15['rsi']<48) or (sign==-1 and f15['rsi']>52)):
            strategy='Відскок у діапазоні'
        else: continue
        score=52 if trend else 48; why=[strategy]
        if trend: score+=10;why.append('1H+4H')
        if sign*(f15['price']-f15['ema200'])>0: score+=3;why.append('EMA200')
        if sign*f15['macd_hist']>0: score+=3;why.append('MACD')
        if f15['volratio']>=1.15: score+=8;why.append('обсяг')
        if sign*spot_book['imbalance']>.08: score+=7;why.append('спот-стакан')
        if sign*fut_book['imbalance']>.08: score+=5;why.append('фʼючерсний стакан')
        if spot_tape and sign*spot_tape['delta']>.12: score+=10;why.append('спот-принти')
        if fut_tape and sign*fut_tape['delta']>.12: score+=5;why.append('фʼючерсні принти')
        if oi_delta is not None and oi_delta>.01 and trend: score+=3;why.append('OI 1г зростає')
        if fund is not None and sign*fund>.0008: score-=8;why.append('перегрітий funding')
        if (sign==1 and f15['rsi']>76) or (sign==-1 and f15['rsi']<24): score-=12
        # Refuse contradictions: strongly opposite spot tape + spot depth.
        if spot_tape and sign*spot_tape['delta']<-.3 and sign*spot_book['imbalance']<-.15:
            continue
        if score<cfg.min_score: continue
        distance=max(a*1.5,price*.006)
        stop=price-sign*distance
        target=price+sign*distance*cfg.min_rr
        proposals.append(dict(side=side,strategy=strategy,score=min(score,100),entry=price,
            stop=stop,target=target,why=why,regime=mode,bar=f5['last']['t'],
            rsi=f15['rsi'],vol=f15['volratio']))
    if not proposals: return None, 'немає підтвердженого сетапу'
    return max(proposals,key=lambda x:x['score']), 'ok'


def sizing(s,cfg):
    dist=abs(s['entry']-s['stop'])
    if dist<=0: return None
    # Per-unit round-trip estimated taker fees and slippage.
    cost_per_unit=s['entry']*(2*cfg.fee_rate_side+2*cfg.slippage_bps_side/10000)
    risk_unit=dist+cost_per_unit
    qty=min(cfg.equity*cfg.risk_fraction/risk_unit,
            cfg.equity*cfg.max_notional/s['entry'])
    if qty*s['entry']<cfg.min_notional_usdt: return None
    gross_reward=qty*abs(s['target']-s['entry'])
    estimated_cost=qty*cost_per_unit
    net_reward=gross_reward-estimated_cost
    risk=qty*risk_unit
    if net_reward<=0 or net_reward/risk<cfg.min_net_rr: return None
    return dict(qty=qty,risk=risk,net_reward=net_reward,net_rr=net_reward/risk,
                notional=qty*s['entry'],cost=estimated_cost)


def database(path):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=20)
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS alerts (id TEXT PRIMARY KEY, created INTEGER, symbol TEXT, side TEXT, strategy TEXT, entry REAL, stop REAL, target REAL, score INTEGER, state TEXT, message TEXT, sent INTEGER DEFAULT 0)')
    db.execute('CREATE TABLE IF NOT EXISTS observations (ts INTEGER, symbol TEXT, price REAL, oi REAL, funding REAL, spot_delta REAL, fut_delta REAL, PRIMARY KEY(ts,symbol))')
    db.execute('CREATE TABLE IF NOT EXISTS events (key TEXT PRIMARY KEY, value TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS tv_signals (id TEXT PRIMARY KEY, ts INTEGER NOT NULL, symbol TEXT NOT NULL, side TEXT NOT NULL, source TEXT NOT NULL)')
    db.execute("CREATE TABLE IF NOT EXISTS paper_trades (id TEXT PRIMARY KEY, opened INTEGER, closed INTEGER, symbol TEXT, side TEXT, strategy TEXT, entry REAL, stop REAL, target REAL, score INTEGER, qty REAL, risk REAL, status TEXT, exit_price REAL, exit_reason TEXT, pnl REAL, r_multiple REAL, mfe_r REAL DEFAULT 0, mae_r REAL DEFAULT 0, regime TEXT, meta TEXT)")
    db.commit()
    return db


def tv_recent(db, symbol, side, now, cfg):
    row=db.execute('SELECT ts FROM tv_signals WHERE symbol=? AND side=? AND ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 1',
                   (symbol,side,now-cfg.tv_ttl_minutes*60,now+30)).fetchone()
    return bool(row)


def start_webhook(cfg):
    if not cfg.tv_port:
        return None
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != '/tradingview':
                self.send_error(404); return
            try:
                n=int(self.headers.get('Content-Length','0'))
                if n<=0 or n>4096:
                    self.send_error(413); return
                raw=self.rfile.read(n)
                secret=self.headers.get('X-TV-Secret','')
                # TradingView webhooks do not support custom headers in their alert UI.
                # Also accept secret in JSON payload, but never log it.
                obj=json.loads(raw)
                supplied=secret or str(obj.get('secret',''))
                if not hmac.compare_digest(supplied,cfg.tv_secret):
                    self.send_error(403); return
                symbol=str(obj.get('symbol','')).upper()
                side=str(obj.get('side','')).upper()
                if symbol not in ('BTCUSDT','ETHUSDT') or side not in ('LONG','SHORT'):
                    self.send_error(422); return
                now=int(time.time()); ts=int(obj.get('ts',now))
                if abs(now-ts)>cfg.tv_ttl_minutes*60:
                    self.send_error(422); return
                alert_id=hashlib.sha256(f'{symbol}:{side}:{ts}:{obj.get("id", "")}'.encode()).hexdigest()
                with database(cfg.db_path) as db:
                    db.execute('INSERT OR IGNORE INTO tv_signals(id,ts,symbol,side,source) VALUES (?,?,?,?,?)',
                               (alert_id,ts,symbol,side,'TradingView'))
                    db.execute('DELETE FROM tv_signals WHERE ts<?',(now-86400,))
                self.send_response(200); self.end_headers(); self.wfile.write(b'OK')
            except (ValueError,TypeError,KeyError,json.JSONDecodeError):
                self.send_error(400)
            except Exception:
                LOG.exception('TV_WEBHOOK_ERROR'); self.send_error(500)
        def log_message(self,fmt,*args):
            LOG.debug('TV HTTP %s',self.address_string())
    server=ThreadingHTTPServer(('0.0.0.0',cfg.tv_port),Handler)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    LOG.info('TradingView webhook listening on port %s',cfg.tv_port)
    return server


def paper_update_bars(db,symbol,bars,now,cfg):
    """Conservative OHLC simulation: if both SL/TP touched in same bar, SL wins.
    On a gap through SL, use the worse of opening price and stop.
    Only closed 1m bars AFTER the paper entry are considered.
    This is an approximation, not tick-accurate execution.
    """
    rows=db.execute("SELECT id,side,entry,stop,target,qty,risk,opened,mfe_r,mae_r FROM paper_trades WHERE symbol=? AND status='OPEN'",(symbol,)).fetchall()
    closed=[]
    for tid,side,entry,stop,target,qty,risk,opened,mfe,mae in rows:
        sign=1 if side=='LONG' else -1
        for bar in bars:
            bar_end=bar['t']//1000+60
            if bar_end<=opened or bar_end>now: continue
            favorable=(bar['h']-entry if sign==1 else entry-bar['l'])*qty/max(risk,1e-9)
            adverse=(bar['l']-entry if sign==1 else entry-bar['h'])*qty/max(risk,1e-9)
            mfe=max(float(mfe or 0),favorable); mae=min(float(mae or 0),adverse)
            stop_hit=bar['l']<=stop if sign==1 else bar['h']>=stop
            target_hit=bar['h']>=target if sign==1 else bar['l']<=target
            timeout=bar_end-opened>=cfg.max_hold_hours*3600
            if not (stop_hit or target_hit or timeout): continue
            if stop_hit:
                reason='STOP' if not target_hit else 'BOTH_STOP_FIRST'
                exit_price=min(bar['o'],stop) if sign==1 else max(bar['o'],stop)
            elif target_hit:
                reason='TARGET'; exit_price=target
            else:
                reason='TIME'; exit_price=bar['c']
            gross=sign*(exit_price-entry)*qty
            cost=qty*(entry+exit_price)*(cfg.fee_rate_side+cfg.slippage_bps_side/10000)
            pnl=gross-cost; rr=pnl/max(risk,1e-9)
            db.execute("UPDATE paper_trades SET closed=?,status='CLOSED',exit_price=?,exit_reason=?,pnl=?,r_multiple=?,mfe_r=?,mae_r=? WHERE id=?",
                       (bar_end,exit_price,reason,pnl,rr,mfe,mae,tid))
            closed.append((tid,side,reason,pnl,rr)); break
        else:
            db.execute('UPDATE paper_trades SET mfe_r=?,mae_r=? WHERE id=?',(mfe,mae,tid))
    db.commit()
    return closed


def paper_daily_r(db, symbol, now):
    row=db.execute("SELECT COALESCE(SUM(r_multiple),0) FROM paper_trades WHERE symbol=? AND status='CLOSED' AND closed>=?",(symbol,now-86400)).fetchone()
    return float(row[0] or 0.0)


def paper_update(db, symbol, price, now, cfg):
    rows=db.execute("SELECT id,side,entry,stop,target,qty,risk,opened,mfe_r,mae_r FROM paper_trades WHERE symbol=? AND status='OPEN'",(symbol,)).fetchall()
    closed=[]
    for tid,side,entry,stop,target,qty,risk,opened,mfe,mae in rows:
        sign=1 if side=='LONG' else -1
        gross=sign*(price-entry)*qty
        r=gross/max(risk,1e-9); mfe=max(float(mfe or 0),r); mae=min(float(mae or 0),r)
        reason=None
        if (side=='LONG' and price<=stop) or (side=='SHORT' and price>=stop): reason='STOP'
        elif (side=='LONG' and price>=target) or (side=='SHORT' and price<=target): reason='TARGET'
        elif now-opened>=cfg.max_hold_hours*3600: reason='TIME'
        if reason:
            cost=qty*entry*(2*cfg.fee_rate_side+2*cfg.slippage_bps_side/10000)
            pnl=gross-cost; rr=pnl/max(risk,1e-9)
            db.execute("UPDATE paper_trades SET closed=?,status='CLOSED',exit_price=?,exit_reason=?,pnl=?,r_multiple=?,mfe_r=?,mae_r=? WHERE id=?",(now,price,reason,pnl,rr,mfe,mae,tid))
            closed.append((tid,side,reason,pnl,rr))
        else: db.execute('UPDATE paper_trades SET mfe_r=?,mae_r=? WHERE id=?',(mfe,mae,tid))
    db.commit(); return closed


def paper_open(db, alert_id, symbol, s, z, now, cfg):
    if not cfg.paper_enabled or paper_daily_r(db,symbol,now)<=-abs(cfg.daily_loss_r): return False
    n=db.execute("SELECT COUNT(*) FROM paper_trades WHERE symbol=? AND status='OPEN'",(symbol,)).fetchone()[0]
    if n>=cfg.max_open_paper: return False
    meta=json.dumps({'why':s['why'],'rsi15':s['rsi'],'volratio':s['vol']},ensure_ascii=False)
    db.execute("INSERT OR IGNORE INTO paper_trades (id,opened,symbol,side,strategy,entry,stop,target,score,qty,risk,status,regime,meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,'OPEN',?,?)",(alert_id,now,symbol,s['side'],s['strategy'],s['entry'],s['stop'],s['target'],s['score'],z['qty'],z['risk'],s['regime'],meta))
    db.commit(); return True


def paper_stats(db,symbol):
    row=db.execute("SELECT COUNT(*),COALESCE(SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END),0),COALESCE(SUM(pnl),0),COALESCE(AVG(r_multiple),0),COALESCE(SUM(CASE WHEN pnl>0 THEN pnl ELSE 0 END),0),COALESCE(-SUM(CASE WHEN pnl<0 THEN pnl ELSE 0 END),0) FROM paper_trades WHERE symbol=? AND status='CLOSED'",(symbol,)).fetchone()
    n,w,pnl,avg_r,gp,gl=row; pf=(gp/gl) if gl and gl>0 else (999.0 if gp>0 else 0.0)
    return {'n':n,'winrate':(100*w/n if n else 0),'pnl':pnl,'avg_r':avg_r,'pf':pf}


def state_get(db,key,default=None):
    row=db.execute('SELECT value FROM events WHERE key=?',(key,)).fetchone()
    return row[0] if row else default


def state_set(db,key,val):
    db.execute('INSERT INTO events(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(val)))
    db.commit()


def telegram(cfg,text):
    if not cfg.token or not cfg.chat_id: raise RuntimeError('BOT_TOKEN або CHAT_ID не задані')
    # No retry on timeout to avoid duplicate sends.
    try:
        r=requests.post(f'https://api.telegram.org/bot{cfg.token}/sendMessage',
                        data={'chat_id':cfg.chat_id,'text':text,'disable_web_page_preview':'true'},timeout=(5,12))
        r.raise_for_status()
        data=r.json()
        if not data.get('ok'): raise RuntimeError('Telegram відхилив повідомлення')
    except (requests.RequestException,ValueError) as exc:
        raise RuntimeError(f'Telegram: {type(exc).__name__} (перевір токен/чат/мережу)') from exc
    return True


def notify(cfg,text,label):
    try:
        telegram(cfg,text)
        LOG.info('TELEGRAM_OK label=%s',label)
        return True
    except Exception as e:
        LOG.error('TELEGRAM_FAILED label=%s error=%s',label,str(e))
        return False


def build_message(symbol,s,z,cfg,spot_tape,fut_tape,oi_delta,funding):
    notes='НЕ ПЕРЕВІРЕНО автоматично' if cfg.event_risk=='unknown' else 'ручна оцінка: '+cfg.event_risk
    spot='н/д' if spot_tape is None else f'{spot_tape["delta"]:+.2f}'
    fut='н/д' if fut_tape is None else f'{fut_tape["delta"]:+.2f}'
    oi='н/д' if oi_delta is None else f'{oi_delta*100:+.2f}% за ~1 год'
    fr='н/д' if funding is None else f'{funding*100:.4f}%'
    return (f'📊 BTC Signal V{VERSION} | ТЕСТ | {symbol} {s["side"]}\n'
        f'Стратегія: {s["strategy"]} | Режим: {s["regime"]}\n'
        f'Вхід орієнтовно: {s["entry"]:,.2f} USDT\n'
        f'Стоп: {s["stop"]:,.2f} | Тейк: {s["target"]:,.2f}\n'
        f'Бал моделі: {s["score"]}/100 (НЕ ймовірність)\n'
        f'Орієнтовний обсяг: {z["qty"]:.6f} {symbol.replace("USDT","")}\n'
        f'Номінал: {z["notional"]:.2f} USDT | Ризик: до {z["risk"]:.2f} USDT*\n'
        f'Чисте R:R оцінка: 1:{z["net_rr"]:.2f}\n'
        f'RSI15: {s["rsi"]:.1f}; обсяг: {s["vol"]:.2f}x\n'
        f'Spot tape: {spot}; Futures tape: {fut}; OI: {oi}; funding: {fr}\n'
        f'Підтвердження: {", ".join(s["why"])}\n'
        f'Новини/геополітика: {notes}. Опціони/ETF/он-чейн: НЕ ПІДКЛЮЧЕНІ.\n'
        '*Оцінка з комісіями/прослизанням, не гарантія максимального збитку. '
        'Стоп на біржі НЕ виставлений. Перевір ринок вручну перед угодою.')


def fetch_optional(fn,label):
    try: return fn()
    except Exception as e:
        LOG.warning('OPTIONAL_UNAVAILABLE %s %s',label,type(e).__name__)
        return None


def once(cfg,market,db,symbol):
    # Fail closed on any mandatory source; optional missing sources are disclosed.
    rows={tf:market.candles(symbol,tf) for tf in ('5m','15m','1H','4H')}
    m={tf:features(rows[tf]) for tf in TF}
    spot=market.book(symbol,'spot')
    fut=market.book(symbol,'futures')
    live=market.ticker(symbol)
    close=m['5m']['price']
    if abs(live/close-1)>.025 or abs(spot['mid']/live-1)>.025 or abs(fut['mid']/live-1)>.025:
        raise MarketError('Непослідовні ціни різних ринків')
    # 1m OHLC replay is more conservative than single ticker polling.
    # If 1m history is unavailable, DO NOT fabricate paper exits.
    paper_bars=fetch_optional(lambda:market.candles(symbol,'1m'),'paper 1m candles') if cfg.paper_enabled else None
    for tid,side,reason,pnl,rr in (paper_update_bars(db,symbol,paper_bars,int(time.time()),cfg) if paper_bars else []):
        st=paper_stats(db,symbol)
        notify(cfg,f'🧪 PAPER {side} закрито: {reason}\nP&L: {pnl:+.2f} USDT | R: {rr:+.2f}\nСтатистика: {st["n"]} угод | Win {st["winrate"]:.1f}% | PF {st["pf"]:.2f} | AvgR {st["avg_r"]:+.2f}','paper_close')
    spot_tape=fetch_optional(lambda:market.recent_trades(symbol,'spot'),'spot tape')
    fut_tape=fetch_optional(lambda:market.recent_trades(symbol,'futures'),'futures tape')
    oi=fetch_optional(lambda:market.oi(symbol),'OI')
    funding=fetch_optional(lambda:market.funding(symbol),'funding')
    now=int(time.time())
    # Compare OI with observation near one hour ago, not the previous minute.
    old=db.execute('SELECT oi FROM observations WHERE symbol=? AND ts<=? AND ts>=? AND oi IS NOT NULL ORDER BY ts DESC LIMIT 1',
        (symbol,now-3300,now-4500)).fetchone()
    oi_delta=(oi/old[0]-1) if oi is not None and old and old[0]>0 else None
    db.execute('INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,?,?)',(
        now,symbol,live,oi,funding,spot_tape['delta'] if spot_tape else None,
        fut_tape['delta'] if fut_tape else None))
    db.execute('DELETE FROM observations WHERE ts<?',(now-7*86400,))
    db.commit()
    s,why=propose(m,spot,fut,spot_tape,fut_tape,funding,oi_delta,cfg)
    LOG.info('SCAN symbol=%s live=%.2f RSI15=%.1f regime=%s score=%s reason=%s event=%s',
             symbol,live,m['15m']['rsi'],regime(m['1H'],m['4H']),s['score'] if s else '-',why,cfg.event_risk)
    if not s: return 'WAIT'
    if cfg.tv_required and not tv_recent(db,symbol,s['side'],int(time.time()),cfg):
        return 'TV_NOT_CONFIRMED'
    if cfg.event_risk in ('elevated','critical'):
        LOG.info('BLOCK: manually elevated event risk');return 'NEWS_BLOCK'
    if cfg.require_news_clearance and cfg.event_risk=='unknown':
        LOG.info('BLOCK: news clearance required but unavailable');return 'NEWS_UNKNOWN'
    if cfg.paper_enabled and paper_daily_r(db,symbol,int(time.time()))<=-abs(cfg.daily_loss_r):
        return 'DAILY_LOSS_BLOCK'
    if cfg.paper_enabled and db.execute("SELECT COUNT(*) FROM paper_trades WHERE symbol=? AND status='OPEN'",(symbol,)).fetchone()[0]>=cfg.max_open_paper:
        return 'OPEN_PAPER_BLOCK'
    z=sizing(s,cfg)
    if not z:
        LOG.info('BLOCK: net R:R / risk sizing / notional');return 'RISK_BLOCK'
    # Deduplicate by strategy + direction + closed 5m bar. Avoid repeated alert after restart.
    alert_id=hashlib.sha256(f'{symbol}:{s["side"]}:{s["strategy"]}:{s["bar"]}'.encode()).hexdigest()[:24]
    if db.execute('SELECT 1 FROM alerts WHERE id=?',(alert_id,)).fetchone():return 'DUPLICATE'
    today=now-86400
    if db.execute('SELECT COUNT(*) FROM alerts WHERE symbol=? AND created>=?',(symbol,today)).fetchone()[0]>=cfg.max_daily_alerts:
        return 'DAILY_CAP'
    prev=db.execute('SELECT created FROM alerts WHERE symbol=? AND side=? ORDER BY created DESC LIMIT 1',
                    (symbol,s['side'])).fetchone()
    if prev and now-prev[0]<cfg.cooldown_minutes*60:return 'COOLDOWN'
    msg=build_message(symbol,s,z,cfg,spot_tape,fut_tape,oi_delta,funding)
    # Outbox: persist before send. A timeout may have delivered: no automatic retry
    # of an uncertain Telegram POST, preventing accidental duplicate trading alerts.
    db.execute('INSERT INTO alerts (id,created,symbol,side,strategy,entry,stop,target,score,state,message) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
        (alert_id,now,symbol,s['side'],s['strategy'],s['entry'],s['stop'],s['target'],s['score'],'pending',msg))
    db.commit()
    if not cfg.dry_run:
        paper_open(db,alert_id,symbol,s,z,now,cfg)
    else:
        LOG.info('DRY_RUN: no paper position opened')
    ok=True if cfg.dry_run else notify(cfg,msg,'signal')
    db.execute('UPDATE alerts SET state=?,sent=? WHERE id=?',('sent' if ok else 'uncertain',int(ok),alert_id))
    db.commit()
    return 'SENT' if ok else 'SEND_FAILED'


RUNNING=True

def stop_handler(signum,frame):
    global RUNNING
    RUNNING=False


def main():
    logging.basicConfig(level=os.getenv('LOG_LEVEL','INFO'),
        format='%(asctime)s %(levelname)s %(message)s',stream=sys.stdout)
    cfg=Config();cfg.validate()
    if not cfg.dry_run and (not cfg.token or not cfg.chat_id):
        raise RuntimeError('BOT_TOKEN/CHAT_ID відсутні')
    for sig in (signal.SIGTERM,signal.SIGINT):
        signal.signal(sig,stop_handler)
    db=database(cfg.db_path)
    webhook=start_webhook(cfg)
    market=Bitget()
    symbols=['BTCUSDT']+(['ETHUSDT'] if cfg.enable_eth else [])
    LOG.info('START V%s alerts-only symbols=%s interval=%ds db=%s event=%s',
        VERSION,','.join(symbols),cfg.interval,cfg.db_path,cfg.event_risk)
    startup_ok=cfg.dry_run or notify(cfg,f'✅ BTC Signal V{VERSION} ЗАПУЩЕНО\n'
        f'Моніторинг: {", ".join(symbols)}; кожні {cfg.interval} секунд.\n'
        f'Тестові сигнали + paper trading: {"УВІМКНЕНО" if cfg.paper_enabled else "ВИМКНЕНО"}. Реальні ордери ВИМКНЕНІ.\n'
        f'Новини/геополітика: {cfg.event_risk} (ручний статус, НЕ автоматична перевірка).', 'startup')
    if not startup_ok:
        raise RuntimeError('Стартове Telegram-повідомлення не підтверджено. Перевір BOT_TOKEN/CHAT_ID і Render Logs.')
    last_heartbeat=time.monotonic(); last_error_alert=0.0; failures=0
    while RUNNING:
        started=time.monotonic()
        for symbol in symbols:
            try:
                result=once(cfg,market,db,symbol)
                LOG.info('RESULT %s %s',symbol,result)
                failures=0
            except Exception:
                failures+=1
                LOG.exception('SCAN_FAILED symbol=%s failures=%d',symbol,failures)
                if failures>=3 and (not last_error_alert or time.monotonic()-last_error_alert>=3600):
                    notify(cfg,f'⚠️ BTC Signal V{VERSION}: {failures} помилки підряд. '
                               'Сигнали можуть не працювати. Перевір Render Logs.','error')
                    last_error_alert=time.monotonic()
        if not cfg.dry_run and time.monotonic()-last_heartbeat>=cfg.heartbeat_hours*3600:
            notify(cfg,f'💓 BTC Signal V{VERSION} працює. Останній цикл: '
                       f'{datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}. '
                       f'Помилки підряд: {failures}. Це не торговий сигнал.','heartbeat')
            last_heartbeat=time.monotonic()
        while RUNNING and time.monotonic()-started<cfg.interval:
            time.sleep(min(1,max(.1,cfg.interval-(time.monotonic()-started))))
    if webhook: webhook.shutdown()
    db.close()
    LOG.info('STOP V%s',VERSION)


if __name__=='__main__':main()
