import unittest, tempfile, time
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
import bot_v9 as b

class V9Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=b.database(str(Path(self.tmp.name)/'test.sqlite'))
        self.cfg=replace(b.Config(),db_path=str(Path(self.tmp.name)/'test.sqlite'),dry_run=True)
    def tearDown(self):
        self.db.close(); self.tmp.cleanup()
    def test_config(self):
        self.cfg.validate()
        with self.assertRaises(ValueError): replace(self.cfg,leverage=20).validate()
    def test_macd(self):
        self.assertTrue(b.macd_hist(list(range(1,201)))>0)
    def test_tv(self):
        now=int(time.time())
        self.db.execute('INSERT INTO tv_signals VALUES (?,?,?,?,?)',('abc',now,'BTCUSDT','LONG','TradingView'))
        self.db.commit()
        self.assertTrue(b.tv_recent(self.db,'BTCUSDT','LONG',now,self.cfg))
        self.assertFalse(b.tv_recent(self.db,'BTCUSDT','SHORT',now,self.cfg))
    def test_paper_stop_first(self):
        now=int(time.time()); opened=now-120
        self.db.execute("INSERT INTO paper_trades (id,opened,symbol,side,strategy,entry,stop,target,score,qty,risk,status,regime,meta) VALUES (?,?,?,?,?,?,?,?,?,?,?,'OPEN',?,?)",
            ('a',opened,'BTCUSDT','LONG','test',100,95,110,80,1,5,'RANGE','{}'))
        self.db.commit()
        bars=[dict(t=(now-120)*1000,o=100,h=111,l=94,c=108)]
        closed=b.paper_update_bars(self.db,'BTCUSDT',bars,now,self.cfg)
        self.assertEqual(closed[0][2],'BOTH_STOP_FIRST')
        self.assertLess(closed[0][3],0)
    def test_dedupe(self):
        self.db.execute('INSERT INTO tv_signals VALUES (?,?,?,?,?)',('x',100,'BTCUSDT','SHORT','TradingView'))
        with self.assertRaises(Exception):
            self.db.execute('INSERT INTO tv_signals VALUES (?,?,?,?,?)',('x',100,'BTCUSDT','SHORT','TradingView'))

if __name__=='__main__': unittest.main()
