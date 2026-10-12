"""BTC Signal V6: public market-data signal bot; NEVER places orders.
Python 3.10+, pip install requests. Run: python bot_v6.py
"""
import json, logging, math, os, sqlite3, statistics, time
from datetime import datetime, timezone
from pathlib import Path
import requests

BASE='https://api.bitget.com'
SYMBOL=os.getenv('SYMBOL','BTCUSDT').upper()
PRODUCT='USDT-FUTURES'
INTERVAL=max(60,int(os.getenv('CHECK_SECONDS','60')))
EQUITY=float(os.getenv('REFERENCE_EQUITY','1000'))
RISK_PCT=float(os.getenv('RISK_PCT','0.005'))
MAX_NOTIONAL_MULT=float(os.getenv('MAX_NOTIONAL_MULT','2'))
MIN_RR=float(os.getenv('MIN_RR','2.0'))
MIN_SCORE=int(os.getenv('MIN_SCORE','65'))
COOLDOWN=int(os.getenv('COOLDOWN_MINUTES','180'))*60
DB=os.getenv('DB_PATH','/tmp/btc_v6.sqlite3')
NEWS_MODE=os.getenv('NEWS_MODE','manual').lower()  # manual: no claims of news verification
EVENT_RISK=os.getenv('EVENT_RISK','unknown').lower() # normal, elevated, critical, unknown
TOKEN=os.getenv('BOT_TOKEN','')
CHAT=os.getenv('CHAT_ID','')
LOG=logging.getLogger('btc_v6')
HTTP=requests.Session()
HTTP.headers.update({'User-Agent':'BTCSignalV6/1.0','Accept':'application/json'})
TF={'5m':300,'15m':900,'1H':3600,'4H':14400}

def api(path,params):
    for attempt in range(3):
        try:
            r=HTTP.get(BASE+path,params=params,timeout=12)
            r.raise_for_status()
            d=r.json()
            if d.get('code')!='00000' or d.get('data') is None:
                raise ValueError('Bitget response '+str(d.get('code'))+' '+str(d.get('msg'))[:100])
            return d['data']
        except (requests.RequestException,ValueError):
            if attempt==2: raise
            time.sleep(2**attempt)

def candles(interval,category='USDT-FUTURES',limit=260):
    rows=api('/api/v3/market/candles',{'category':category,'symbol':SYMBOL,'interval':interval,'limit':str(limit)})
    now=time.time()*1000
    result={}
    for r in rows:
        ts=int(r[0]); o,h,l,c,v=map(float,r[1:6])
        if ts+TF[interval]*1000>now-2000: continue  # never use incomplete candles
        if not all(math.isfinite(x) for x in (o,h,l,c,v)) or min(o,h,l,c)<=0 or l>min(o,c) or h<max(o,c) or v<0:
            continue
        result[ts]={'t':ts,'o':o,'h':h,'l':l,'c':c,'v':v}
    out=[result[k] for k in sorted(result)]
    if len(out)<110 or (now-out[-1]['t'])>TF[interval]*1000*3:
        raise ValueError('Insufficient/stale candles: '+interval)
    return out

def book(category):
    d=api('/api/v3/market/orderbook',{'category':category,'symbol':SYMBOL,'limit':'50'})
    ts=int(d.get('ts',0))
    if abs(time.time()*1000-ts)>90000: raise ValueError('Stale order book')
    bids=d.get('b',[]); asks=d.get('a',[])
    if not bids or not asks: raise ValueError('Empty order book')
    bid=float(bids[0][0]); ask=float(asks[0][0])
    if bid<=0 or ask<=bid: raise ValueError('Invalid book spread')
    b=sum(float(x[0])*float(x[1]) for x in bids[:20])
    a=sum(float(x[0])*float(x[1]) for x in asks[:20])
    return {'imbalance':(b-a)/(b+a) if b+a else 0,'spread':(ask-bid)/((ask+bid)/2),'mid':(ask+bid)/2}

def ema(vals,n):
    out=[]; value=vals[0]; alpha=2/(n+1)
    for v in vals:
        value=alpha*v+(1-alpha)*value;out.append(value)
    return out

def atr(rows,n=14):
    tr=[]
    for i in range(1,len(rows)):
        a=rows[i];p=rows[i-1]['c']
        tr.append(max(a['h']-a['l'],abs(a['h']-p),abs(a['l']-p)))
    return statistics.mean(tr[-n:])

def rsi(vals,n=14):
    dif=[vals[i]-vals[i-1] for i in range(1,len(vals))][-n:]
    gain=sum(max(0,x) for x in dif)/n
    loss=sum(max(0,-x) for x in dif)/n
    return 100 if loss==0 else 100-100/(1+gain/loss)

def metrics(rows):
    c=[x['c'] for x in rows]
    e20=ema(c,20);e50=ema(c,50);e100=ema(c,100)
    a=atr(rows)
    return {'price':c[-1],'e20':e20[-1],'e50':e50[-1],'e100':e100[-1],
            'slope':(e20[-1]-e20[-6])/max(a,1e-9),'atr':a,'rsi':rsi(c),
            'high20':max(x['h'] for x in rows[-21:-1]),
            'low20':min(x['l'] for x in rows[-21:-1]),
            'volratio':rows[-1]['v']/max(statistics.mean(x['v'] for x in rows[-21:-1]),1e-9),
            'last':rows[-1],'prev':rows[-2]}

def oi():
    d=api('/api/v2/mix/market/open-interest',{'symbol':SYMBOL,'productType':PRODUCT})
    xs=d.get('openInterestList',[])
    return float(xs[0]['size']) if xs else None

def funding():
    d=api('/api/v2/mix/market/current-fund-rate',{'symbol':SYMBOL,'productType':PRODUCT})
    return float(d[0]['fundingRate']) if d else None

def telegram(msg):
    if not TOKEN or not CHAT:
        LOG.warning('Telegram not configured; message: %s',msg);return False
    r=HTTP.post('https://api.telegram.org/bot'+TOKEN+'/sendMessage',data={'chat_id':CHAT,'text':msg},timeout=12)
    r.raise_for_status();d=r.json()
    if not d.get('ok'): raise ValueError('Telegram error '+str(d.get('description')))
    return True

def init_db(db):
    db.execute('CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY, ts INTEGER, side TEXT, strategy TEXT, entry REAL, stop REAL, target REAL, score INTEGER, reason TEXT, status TEXT DEFAULT "open", exit REAL, outcome_r REAL)')
    db.execute('CREATE TABLE IF NOT EXISTS snapshots (ts INTEGER PRIMARY KEY, price REAL, oi REAL, funding REAL, spot_imb REAL, futures_imb REAL)')
    db.commit()

def signal(rows, m, spot, fut, fund, oi_change):
    m5,m15,m1,m4=[m[k] for k in ('5m','15m','1H','4H')]
    price=m5['price']; a=m15['atr']
    if a<=0 or fut['spread']>0.0012 or spot['spread']>0.0012: return None
    # Trend / chop detection, volatility-adjusted; independent entry families.
    trending=abs(m1['slope'])>0.20 and abs(m4['slope'])>0.10
    candidates=[]
    for side in ('LONG','SHORT'):
        sign=1 if side=='LONG' else -1
        trend=sign*(m1['e20']-m1['e50'])>0 and sign*(m4['e20']-m4['e50'])>0
        trend5=sign*(m5['e20']-m5['e50'])>0
        breakout=(price>m15['high20'] if sign==1 else price<m15['low20'])
        pullback=(abs(price-m15['e20'])<0.8*a and sign*(m5['last']['c']-m5['last']['o'])>0)
        bounce=(m15['last']['l']<=m15['low20']+0.35*a and price>m15['low20']+0.6*a) if sign==1 else (m15['last']['h']>=m15['high20']-0.35*a and price<m15['high20']-0.6*a)
        reversal=(bounce and ((m15['rsi']<43 and sign==1) or (m15['rsi']>57 and sign==-1)))
        if trending and trend and breakout: strategy='breakout'
        elif trending and trend and pullback and trend5: strategy='trend_pullback'
        elif not trending and reversal: strategy='range_reversal'
        else: continue
        score=45
        reasons=[strategy]
        if trend:score+=12;reasons.append('4H/1H trend')
        if m15['volratio']>=1.2:score+=10;reasons.append('volume')
        if sign*fut['imbalance']>0.08:score+=8;reasons.append('futures depth')
        if sign*spot['imbalance']>0.08:score+=10;reasons.append('spot depth')
        if oi_change is not None and oi_change>0.015 and trend:score+=4;reasons.append('OI rising')
        if fund is not None and sign*fund>0.0008:score-=10;reasons.append('crowded funding')
        if (sign==1 and m15['rsi']>76) or (sign==-1 and m15['rsi']<24):score-=12
        # Fixed risk-based stop, not a prediction of liquidation price.
        distance=max(a*1.5,price*0.002)
        stop=price-sign*distance;target=price+sign*distance*MIN_RR
        if score>=MIN_SCORE:
            candidates.append({'side':side,'strategy':strategy,'score':score,'entry':price,'stop':stop,'target':target,'reasons':reasons})
    return max(candidates,key=lambda x:x['score']) if candidates else None

def update_outcomes(db,price):
    # Single-price observations cannot reconstruct which level was hit first between checks.
    # Only label when observed; not a reliable performance backtest.
    for id,side,entry,stop,target in db.execute('SELECT id,side,entry,stop,target FROM signals WHERE status="open"'):
        hit_stop=(price<=stop if side=='LONG' else price>=stop)
        hit_target=(price>=target if side=='LONG' else price<=target)
        if hit_stop or hit_target:
            result=(-1 if hit_stop else MIN_RR)
            db.execute('UPDATE signals SET status=?,exit=?,outcome_r=? WHERE id=?',('observed_stop' if hit_stop else 'observed_target',price,result,id))
    db.commit()

def cycle(db):
    rows={k:candles(k) for k in TF}
    m={k:metrics(v) for k,v in rows.items()}
    price=m['5m']['price']
    spot=book('SPOT');fut=book('USDT-FUTURES')
    if abs(spot['mid']/price-1)>0.025 or abs(fut['mid']/price-1)>0.025:
        raise ValueError('Inconsistent cross-market prices')
    try: current_oi=oi()
    except Exception as e: LOG.warning('OI unavailable: %s',e);current_oi=None
    try: fund=funding()
    except Exception as e: LOG.warning('Funding unavailable: %s',e);fund=None
    previous=db.execute('SELECT oi FROM snapshots WHERE oi IS NOT NULL ORDER BY ts DESC LIMIT 1').fetchone()
    oi_change=(current_oi/previous[0]-1) if previous and previous[0] and current_oi else None
    now=int(time.time())
    db.execute('INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?)',(now,price,current_oi,fund,spot['imbalance'],fut['imbalance']))
    db.execute('DELETE FROM snapshots WHERE ts<?',(now-7*86400,));db.commit()
    update_outcomes(db,price)
    sig=signal(rows,m,spot,fut,fund,oi_change)
    LOG.info('price=%.2f 1H_RSI=%.1f spot_depth=%.2f futures_depth=%.2f score=%s event=%s',price,m['1H']['rsi'],spot['imbalance'],fut['imbalance'],sig['score'] if sig else '-',EVENT_RISK)
    if not sig:return
    # Missing news coverage is explicit; critical news veto; unknown halves trust, not fabricated verification.
    if EVENT_RISK in ('critical','elevated'):
        LOG.info('Signal vetoed by manually configured event risk');return
    if EVENT_RISK=='unknown' and sig['score']<MIN_SCORE+12:
        LOG.info('Signal vetoed: news risk unknown and score not high enough');return
    recent=db.execute('SELECT ts FROM signals WHERE side=? ORDER BY ts DESC LIMIT 1',(sig['side'],)).fetchone()
    if recent and now-recent[0]<COOLDOWN:return
    # No open simultaneous alerts; never pretend the stop has been placed on exchange.
    if db.execute('SELECT COUNT(*) FROM signals WHERE status="open"').fetchone()[0]:return
    risk=EQUITY*RISK_PCT
    qty=risk/abs(sig['entry']-sig['stop'])
    if qty*sig['entry']>EQUITY*MAX_NOTIONAL_MULT:
        qty=EQUITY*MAX_NOTIONAL_MULT/sig['entry']
    actual_risk=qty*abs(sig['entry']-sig['stop'])
    msg=(f'BTC SIGNAL V6 | {sig["side"]} | {sig["strategy"]}\n'
         f'Entry ~ {sig["entry"]:.2f} USDT\nStop: {sig["stop"]:.2f}\nTarget: {sig["target"]:.2f}\n'
         f'Score (experimental): {sig["score"]}/100 | RR: {MIN_RR:.1f}\n'
         f'Example quantity: {qty:.6f} BTC | planned price risk: ${actual_risk:.2f} + fees/slippage\n'
         f'Factors: {", ".join(sig["reasons"])}\n'
         f'Geopolitics/news: {NEWS_MODE}; event risk: {EVENT_RISK} (NOT independently verified)\n'
         'ALERT ONLY: no order placed; prices may change. Confirm before trading.')
    # Persist first, then send: restart cannot spam repeated alerts. Failed send is logged.
    db.execute('INSERT INTO signals(ts,side,strategy,entry,stop,target,score,reason) VALUES(?,?,?,?,?,?,?,?)',
               (now,sig['side'],sig['strategy'],sig['entry'],sig['stop'],sig['target'],sig['score'],json.dumps(sig['reasons'])))
    db.commit()
    telegram(msg)

def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    if not (0<RISK_PCT<=0.01 and EQUITY>0 and 1.5<=MIN_RR<=5 and 1<=MAX_NOTIONAL_MULT<=3):
        raise ValueError('Unsafe/invalid risk configuration')
    if EVENT_RISK not in ('normal','elevated','critical','unknown'):raise ValueError('Invalid EVENT_RISK')
    Path(DB).parent.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(DB) as db:
        init_db(db)
        LOG.info('BTC V6 started: alerts only; %s; every %ds; event risk=%s',SYMBOL,INTERVAL,EVENT_RISK)
        while True:
            start=time.monotonic()
            try:cycle(db)
            except Exception:LOG.exception('Cycle failed; no signal generated')
            time.sleep(max(1,INTERVAL-(time.monotonic()-start)))

if __name__=='__main__':main()
