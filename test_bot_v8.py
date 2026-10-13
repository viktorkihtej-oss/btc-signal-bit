import sys,tempfile,unittest,importlib.util,time
spec=importlib.util.spec_from_file_location('bot_v8','/mnt/data/bot_v8.py')
b=importlib.util.module_from_spec(spec); sys.modules['bot_v8']=b; spec.loader.exec_module(b)
class Tests(unittest.TestCase):
 def test_indicators(self): self.assertTrue(0<=b.rsi([100+i for i in range(30)])<=100)
 def test_db(self):
  with tempfile.TemporaryDirectory() as d:
   db=b.database(d+'/x.db'); self.assertEqual(b.paper_stats(db,'BTCUSDT')['n'],0); db.close()
 def test_book(self):
  x=b.parse_book({'ts':str(int(time.time()*1000)),'bids':[['100','2']],'asks':[['101','2']]}); self.assertGreater(x['spread_bps'],0)
if __name__=='__main__': unittest.main()
