"""Offline unit tests for the optional MARKET-DEX UW GEX updater."""
import unittest
from unittest.mock import patch

import update_uw_gex as gex


class TestMarketDexUwGex(unittest.TestCase):
    def test_safe_symbol(self):
        self.assertEqual(gex.safe_symbol(" brk.b "), "BRK.B")
        self.assertEqual(gex.safe_symbol("bad ticker!"), "")

    def test_unwrap_rows(self):
        self.assertEqual(gex.unwrap_rows({"result": [{"ticker": "AAPL"}]}), [{"ticker": "AAPL"}])
        self.assertEqual(gex.unwrap_rows({"data": []}), [])
        self.assertEqual(gex.unwrap_rows(None), [])

    def test_detail_ticker_list_caps_explicit_requests(self):
        rows = [
            {"ticker": "AAA", "total_open_interest": "100"},
            {"ticker": "BBB", "total_open_interest": "200"},
            {"ticker": "CCC", "total_open_interest": "300"},
        ]
        with patch.object(gex, "EXPLICIT_TICKERS", "ZZZ,YYY,XXX"), \
             patch.object(gex, "DETAIL_LIMIT", 2), \
             patch.object(gex, "DETAIL_MAX_TICKERS", 4):
            self.assertEqual(gex.detail_ticker_list(rows), ["ZZZ", "YYY", "XXX", "CCC"])

    def test_fetch_detail_uses_oi_surface_and_separate_levels(self):
        def fake_request(_session, path, params=None):
            if path.endswith("/spot-exposures/expiry-strike"):
                page = params["page"]
                if page:
                    return {"data": []}
                return {"data": [{
                    "date": "2026-11-09", "expiry": "2026-11-20",
                    "strike": "100", "price": "100",
                    "call_gex": 1200, "put_gex": -500, "dte": 11,
                }]}
            if path.endswith("/gex-levels"):
                return {"data": {"source": params["source"], "call_wall": "110"}}
            raise AssertionError(path)

        with patch.object(gex, "request_json", side_effect=fake_request), \
             patch.object(gex, "DETAIL_MAX_PAGES", 2), \
             patch.object(gex, "PAUSE_SECONDS", 0):
            result = gex.fetch_detail(object(), "TEST", {"close": "100"})
        self.assertEqual(result["metadata"]["surface_basis"], "open_interest")
        self.assertEqual(result["rows"][0]["net_gex_oi"], 700)
        self.assertEqual(result["levels"]["oi"]["source"], "oi")
        self.assertEqual(result["levels"]["vol"]["source"], "vol")


if __name__ == "__main__":
    unittest.main()
