"""Deterministic OHLC replay of PRECOMPUTED signals. NOT a full strategy backtest.
Signals CSV: id,side,entry_time,entry,stop,target,qty
Bars CSV: ts,open,high,low,close (UTC unix seconds, start of bar)
Conservative: stop before target if both touched. No live market access.
"""
import argparse, csv, json
from datetime import datetime, timezone

def read_rows(path):
    with open(path,encoding='utf-8-sig',newline='') as f: return list(csv.DictReader(f))

def replay(signals,bars,fee=.0006,slip_bps=3,bar_seconds=60):
    bars=sorted(bars,key=lambda b:int(b['ts']))
    results=[]
    for s in signals:
        sign=1 if s['side'].upper()=='LONG' else -1
        entry=float(s['entry']); stop=float(s['stop']); target=float(s['target']); qty=float(s['qty']); entered=int(s['entry_time'])
        if not (entry>0 and stop>0 and target>0 and qty>0 and sign*(entry-stop)>0 and sign*(target-entry)>0):
            raise ValueError('Invalid signal '+str(s.get('id')))
        relevant=[b for b in bars if int(b['ts'])>=entered]
        if not relevant: results.append({'id':s['id'],'status':'UNRESOLVED'});continue
        # No inferred fills when bar coverage is missing.
        last_ts=None; outcome=None
        for bar in relevant:
            ts=int(bar['ts']); o,h,l,c=(float(bar[k]) for k in ('open','high','low','close'))
            if last_ts is not None and ts-last_ts!=bar_seconds:
                outcome={'id':s['id'],'status':'DATA_GAP'};break
            last_ts=ts
            if not (0<l<=min(o,c)<=max(o,c)<=h): raise ValueError('Invalid OHLC')
            hit_stop=l<=stop if sign==1 else h>=stop
            hit_target=h>=target if sign==1 else l<=target
            if not (hit_stop or hit_target):continue
            if hit_stop:
                exit_price=min(o,stop) if sign==1 else max(o,stop)
                reason='BOTH_STOP_FIRST' if hit_target else 'STOP'
            else:
                exit_price=target; reason='TARGET'
            entry_fill=entry*(1+sign*slip_bps/10000)
            exit_fill=exit_price*(1-sign*slip_bps/10000)
            pnl=sign*(exit_fill-entry_fill)*qty-fee*(entry_fill+exit_fill)*qty
            outcome={'id':s['id'],'status':reason,'exit':exit_fill,'pnl':round(pnl,8)}
            break
        results.append(outcome or {'id':s['id'],'status':'UNRESOLVED'})
    closed=[r for r in results if 'pnl' in r]
    wins=sum(r['pnl']>0 for r in closed)
    gp=sum(max(0,r['pnl']) for r in closed); gl=-sum(min(0,r['pnl']) for r in closed)
    return {'n_closed':len(closed),'n_total':len(results),'pnl':round(sum(r['pnl'] for r in closed),8),
            'winrate':wins/len(closed) if closed else None,'profit_factor':gp/gl if gl else None,'trades':results}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--signals',required=True);p.add_argument('--bars',required=True)
    p.add_argument('--fee',type=float,default=.0006);p.add_argument('--slippage-bps',type=float,default=3)
    a=p.parse_args()
    print(json.dumps(replay(read_rows(a.signals),read_rows(a.bars),a.fee,a.slippage_bps),ensure_ascii=False,indent=2))
