"""Offline unit/integration tests for BTC Signal V7; no internet, no Telegram sends."""
import os
import sqlite3
import tempfile
import time
import unittest
from dataclasses import replace
from unittest.mock import patch
import bot_v7 as b

class TestV7(unittest.TestCase):
    def setUp(self):
        self.cfg=b.Config()
        self.cfg=replace(self.cfg,token='fake',chat_id='123',event_risk='unknown',min_score=65)
    def candles(self,period,mode='up'):
        now=int(time.time()//period)*period
        rows=[]
        for i in range(150):
            ts=(now-(150-i)*period)*1000
            price=80000+(i*18 if mode=='up' else -i*18 if mode=='down' else (i%12)*15)
            rows.append(dict(t=ts,o=price-5,h=price+25,l=price-25,c=price,v=10+i%5))
        return rows
    def test_rsi(self):
        self.assertAlmostEqual(b.rsi([100]*100),50)
        self.assertGreater(b.rsi(list(range(100))),99)
        self.assertLess(b.rsi(list(range(100,0,-1))),1)
    def test_book(self):
        x={'ts':str(int(time.time()*1000)),'bids':[['82000','2']], 'asks':[['82001','1']]}
        v=b.parse_book(x)
        self.assertGreater(v['imbalance'],0)
        self.assertGreater(v['spread_bps'],0)
        x['ts']='1'
        with self.assertRaises(b.MarketError):b.parse_book(x)
    def test_tape(self):
        now=int(time.time()*1000)
        t=[dict(ts=str(now),price='82000',size='0.01',side='buy') for _ in range(8)]
        self.assertEqual(b.parse_trades(t)['buy_share'],1)
        with self.assertRaises(b.MarketError):b.parse_trades([])
    def test_features(self):
        m=b.features(self.candles(300))
        self.assertGreater(m['ema20'],m['ema50'])
        self.assertGreater(m['atr'],0)
    def test_sizing_and_fees(self):
        s=dict(entry=80000,stop=79400,target=81200)
        z=b.sizing(s,self.cfg)
        self.assertIsNotNone(z)
        self.assertLessEqual(z['risk'],self.cfg.equity*self.cfg.risk_fraction+1e-8)
        self.assertLess(z['net_rr'],2)
        cfg=replace(self.cfg,fee_rate_side=.05)
        self.assertIsNone(b.sizing(s,cfg))
    def test_db(self):
        with tempfile.TemporaryDirectory() as d:
            db=b.database(os.path.join(d,'x.sqlite'))
            b.state_set(db,'hello','yes')
            self.assertEqual(b.state_get(db,'hello'),'yes')
            db.close()
    def test_telegram_missing_credentials(self):
        with self.assertRaises(RuntimeError):b.telegram(replace(self.cfg,token=''), 'hello')
    def test_news_veto_and_no_duplicate(self):
        with tempfile.TemporaryDirectory() as d:
            db=b.database(os.path.join(d,'x.sqlite'))
            market=FakeMarket(self)
            fixed=dict(side='LONG',strategy='Пробій за трендом',score=83,
                       entry=82000,stop=81300,target=83400,why=['test'],regime='UP',
                       bar=123456789,rsi=55,vol=1.2)
            with patch.object(b,'propose',return_value=(fixed,'ok')):
                with patch.object(b,'notify',return_value=True) as notify:
                    self.assertEqual(b.once(replace(self.cfg,event_risk='critical'),market,db,'BTCUSDT'),'NEWS_BLOCK')
                    self.assertEqual(b.once(self.cfg,market,db,'BTCUSDT'),'SENT')
                    self.assertEqual(b.once(self.cfg,market,db,'BTCUSDT'),'DUPLICATE')
                    self.assertEqual(notify.call_count,1)
            db.close()
    def test_no_signal_on_stale_data(self):
        with tempfile.TemporaryDirectory() as d:
            db=b.database(os.path.join(d,'x.sqlite'))
            market=FakeMarket(self)
            market.stale=True
            with self.assertRaises(b.MarketError):b.once(self.cfg,market,db,'BTCUSDT')
            db.close()

class FakeMarket:
    def __init__(self,test):self.test=test;self.stale=False
    def candles(self,symbol,tf):
        if self.stale:raise b.MarketError('stale')
        return self.test.candles(b.TF[tf])
    def book(self,symbol,kind):return {'imbalance':.2,'spread_bps':1,'mid':82000}
    def ticker(self,symbol):return 82000
    def recent_trades(self,symbol,kind):return {'delta':.3,'count':30,'buy_share':.65}
    def oi(self,symbol):return 12345
    def funding(self,symbol):return .0001

if __name__=='__main__':unittest.main()
