import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

MODULE=Path(__file__).resolve().parents[1]/'scripts'/'update_cot_positioning.py'
spec=importlib.util.spec_from_file_location('cot_update',MODULE)
cot=importlib.util.module_from_spec(spec)
sys.modules[spec.name]=cot
spec.loader.exec_module(cot)

class COTValidation(unittest.TestCase):
    def test_financial_fields_do_not_invent_missing(self):
        row={'market_and_exchange_names':'E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE',
             'cftc_contract_market_code':'13874A','report_date_as_yyyy_mm_dd':'2026-11-03T00:00:00',
             'open_interest_all':'2000000','lev_money_positions_long_all':'200000',
             'lev_money_positions_short_all':'300000','asset_mgr_positions_long':'400000',
             'asset_mgr_positions_short':'100000'}
        got=cot.normalize(row,'tff');self.assertEqual(got['categories']['leveraged_funds']['net'],-100000)
        self.assertEqual(got['categories']['asset_managers']['net'],300000)
        self.assertNotIn('dealers',got['categories'])
        self.assertEqual(got['categories']['leveraged_funds']['net_pct_oi'],-5)
        self.assertEqual(got['date'],'2026-11-03')
    def test_disaggregated_futures_are_not_tff(self):
        row={'market_and_exchange_names':'GOLD - COMMODITY EXCHANGE INC.',
             'cftc_contract_market_code':'088691','report_date_as_yyyy_mm_dd':'2026-11-03T00:00:00',
             'open_interest_all':500000,'m_money_positions_long_all':200000,
             'm_money_positions_short_all':75000,'prod_merc_positions_long_all':90000,
             'prod_merc_positions_short_all':125000}
        got=cot.normalize(row,'disaggregated')
        self.assertEqual(got['categories']['managed_money']['net'],125000)
        self.assertEqual(got['categories']['producers_merchants']['net'],-35000)
        self.assertNotIn('leveraged_funds',got['categories'])
    def test_missing_never_published_as_zero(self):
        row={'market_and_exchange_names':'XYZ','cftc_contract_market_code':'ABC','report_date_as_yyyy_mm_dd':'2026-11-03',
             'open_interest_all':100000,'lev_money_positions_long_all':9}
        self.assertIsNone(cot.normalize(row,'tff'))
    def test_historical_net_rank_and_change(self):
        data={};start=__import__('datetime').date(2026,6,2)
        for report in ('tff','disaggregated'):
            observations=[]
            for wk in range(20):
                date=(start+__import__('datetime').timedelta(weeks=wk)).isoformat()
                base={'market_and_exchange_names':'Test '+report,'cftc_contract_market_code':'101ABC',
                      'report_date_as_yyyy_mm_dd':date,'open_interest_all':'10000'}
                if report=='tff':base.update(lev_money_positions_long_all=1000+wk*10,lev_money_positions_short_all=100)
                else:base.update(m_money_positions_long_all=1000+wk*10,m_money_positions_short_all=100)
                observations.append(cot.normalize(base,report))
            data[report]=observations
        idx,details=cot.build(data,'2026-11-10T00:00:00+00:00',min_markets=2)
        self.assertEqual(len(idx['markets']),2)
        item=idx['markets'][0]
        self.assertEqual(item['change_from_prior_report'],10)
        self.assertEqual(item['net_position_percentile'],100)
        self.assertIn(item['id'],details)
        self.assertEqual(details[item['id']]['metadata']['scope'],'futures_only')
    def test_official_source_paths_and_output_isolation(self):
        self.assertEqual(cot.DATASETS['tff'],'gpe5-46if')
        self.assertEqual(cot.DATASETS['disaggregated'],'72hh-3qpy')
        self.assertEqual(cot.OUTPUT.parts[-2:],('data','positioning'))
        self.assertEqual(cot.id_for('tff','13874A'),'tff-13874a')

if __name__=='__main__':unittest.main()
