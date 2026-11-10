import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC=Path(__file__).resolve().parents[1]/'update_vanta_options.py'
spec=importlib.util.spec_from_file_location('vanta_options_publish',SRC)
pub=importlib.util.module_from_spec(spec)
spec.loader.exec_module(pub)

class TestVantaOptions(unittest.TestCase):
    def fixture(self,ticker='NVDA'):
        return {'metadata':{'ticker':ticker,'run_utc':'2026-11-09T21:00:00Z','spot_daily_close':185.5,'provider':'TEST - NOT REAL'},'contracts':[
            {'expiration':'2026-11-20','type':t,'strike':190,'volume':250,'openInterest':750,'bid':1.5,'ask':1.6,'impliedVolatility':.45,'quote_ok':True,'trade_direction':'BUYER - UNSUPPORTED'} for t in ['Call','Put']
        ],'summary':[{'expiration':'2026-11-20','atm_iv':.45,'straddle_mid':3.1}]}
    def test_preserves_valid_source_and_refuses_invented_trade_side(self):
        d=pub.sanitize(self.fixture(),'NVDA')
        self.assertEqual(d['metadata']['ticker'],'NVDA')
        self.assertEqual(len(d['contracts']),2)
        self.assertTrue(all(x['quote_ok'] for x in d['contracts']))
        self.assertEqual(d['contracts'][0]['trade_direction'],'UNKNOWN (no aggressor-side data)')
        self.assertAlmostEqual(d['summary'][0]['straddle_mid'],3.1)
    def test_rejects_wrong_ticker(self):
        with self.assertRaisesRegex(ValueError,'mismatch'):
            pub.sanitize(self.fixture(),'AAPL')
    def test_rejects_bad_quotes_without_imputing(self):
        payload=self.fixture()
        payload['contracts'][0]['ask']=-1
        d=pub.sanitize(payload,'NVDA')
        self.assertFalse(d['contracts'][0]['quote_ok'])
        self.assertIsNone(d['contracts'][0]['mid'])
        self.assertIsNone(d['contracts'][0]['premium_notional_proxy'])
    def test_rejects_missing_stamp(self):
        payload=self.fixture();payload['metadata'].pop('run_utc')
        with self.assertRaisesRegex(ValueError,'timestamp'):
            pub.sanitize(payload,'NVDA')
    def test_off_by_default_and_no_existing_ticker_leak(self):
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory)/'data'/'options';out.mkdir(parents=True)
            (out/'tickers').mkdir();(out/'tickers'/'PRIVATE.json').write_text('secret')
            (out/'manifest.json').write_text('{}')
            with mock.patch.object(pub,'OUTPUT',out),mock.patch.object(pub,'CACHE',Path(directory)/'cache'),mock.patch.dict(os.environ,{'VANTA_OPTIONS_PUBLICATION_APPROVED':'false'}):
                pub.main()
            self.assertFalse((out/'tickers').exists())
            self.assertFalse((out/'manifest.json').exists())
            self.assertEqual(json.loads((out/'status.json').read_text())['state'],'permission_required')
    def test_requires_https_template_even_when_approved(self):
        with tempfile.TemporaryDirectory() as directory:
            out=Path(directory)/'options'
            with mock.patch.object(pub,'OUTPUT',out),mock.patch.object(pub,'CACHE',Path(directory)/'cache'),mock.patch.dict(os.environ,{'VANTA_OPTIONS_PUBLICATION_APPROVED':'true','VANTA_OPTIONS_SOURCE_URL_TEMPLATE':'http://localhost/{ticker}.json'}):
                pub.main()
            self.assertEqual(json.loads((out/'status.json').read_text())['state'],'source_not_configured')

if __name__=='__main__':unittest.main()
