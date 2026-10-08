import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from screener import edgar, market, run, scoring, xbrl
from screener.net import Doc, Fetcher, NotFound
from screener.tech import from_stooq_csv, from_yahoo_chart, technicals
from tests import fixtures as fx

TODAY = date(2026, 10, 8)


def slim():
    return fx.companyfacts()


class XbrlTest(unittest.TestCase):
    def test_revenue_quarters_and_derived_q4(self):
        rq = xbrl.revenue_quarters(slim())
        self.assertAlmostEqual(rq[date(2025, 12, 31)].val, 123e6)  # 486 - 363
        cur, prev = xbrl.yoy(rq)
        self.assertEqual(cur.end, date(2026, 6, 30))
        self.assertAlmostEqual(cur.val / prev.val - 1, 145 / 121 - 1)

    def test_ttm_from_ytd(self):
        fcf, capex_found = xbrl.fcf_ttm(slim())
        self.assertTrue(capex_found)
        # CFO 50 + 30 - 18 = 62, capex 12 + 6 - 5 = 13
        self.assertAlmostEqual(fcf[date(2026, 6, 30)].val, 49e6)
        cur, prev = xbrl.yoy(fcf, tol=15)
        self.assertAlmostEqual(prev.val, (40 + 18 - 15 - (10 + 5 - 4)) * 1e6)

    def test_restated_fact_wins(self):
        s = slim()
        s["facts"]["NetIncomeLoss"]["USD"].append(
            {"start": "2025-01-01", "end": "2025-12-31", "val": 30e6, "accn": "x", "form": "10-K/A", "filed": "2026-05-01"})
        f = [x for x in xbrl.facts_for(s, "NetIncomeLoss") if x.end == date(2025, 12, 31)]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0].val, 30e6)

    def test_cash_debt_ebitda_shares(self):
        s = slim()
        cd = xbrl.cash_and_debt(s)
        self.assertEqual((cd["cash"], cd["debt"]), (80e6, 30e6))
        self.assertAlmostEqual(xbrl.ebitda_ttm(s, date(2026, 6, 30)).val, (53 + 9) * 1e6)
        self.assertEqual(xbrl.shares_outstanding(s).val, 50e6)

    def test_slim_keeps_needed_tags_only(self):
        full = {"cik": 1, "entityName": "X", "facts": {"us-gaap": {
            "Revenues": {"units": {"USD": []}}, "SomethingElse": {"units": {"USD": []}}}}}
        self.assertEqual(list(xbrl.slim_companyfacts(full)["facts"]), ["Revenues"])


class TechTest(unittest.TestCase):
    def test_indicators(self):
        bench_js, stock_js = fx.prices(TODAY)
        bench, _ = from_yahoo_chart(bench_js)
        stock, _ = from_yahoo_chart(stock_js)
        t = technicals(stock, bench)
        weekly = lambda df: df["adjclose"].resample("W-FRI").last().pct_change()
        both = pd.concat([weekly(stock), weekly(bench)], axis=1).dropna().tail(104)
        self.assertAlmostEqual(t["beta"], np.polyfit(both.iloc[:, 1], both.iloc[:, 0], 1)[0], places=9)
        self.assertGreater(t["beta"], 1.2)
        self.assertLess(t["dist_from_high"], -0.25)
        self.assertTrue(t["above_sma50"])
        self.assertTrue(t["sma50_turning_up"])
        self.assertGreater(t["avg_dollar_volume_50d"], 2e6)


class EdgarTest(unittest.TestCase):
    def test_form4_and_insider_summary(self):
        txs = edgar.parse_form4(fx.FORM4_XML.format(d="2026-09-20"))
        self.assertEqual(txs[0]["code"], "P")
        self.assertEqual(txs[0]["role"], "CEO")
        s = edgar.insider_summary(txs, TODAY)
        self.assertEqual((s["buy_usd"], s["sell_usd"]), (125000.0, 0))
        old = edgar.insider_summary(edgar.parse_form4(fx.FORM4_XML.format(d="2026-05-01")), TODAY)
        self.assertEqual(old["buy_usd"], 0)

    def test_dilution_and_delisting(self):
        rows = edgar.filings_table({"filings": {"recent": {
            "accessionNumber": ["a-1", "a-2", "a-3", "a-4", "a-5"],
            "filingDate": ["2026-09-01", "2025-01-10", "2023-01-01", "2026-03-01", "2026-02-01"],
            "reportDate": ["", "", "", "", ""],
            "form": ["424B5", "S-3", "S-3", "8-K", "424B5"],
            "items": ["", "", "", "3.01,9.01", ""],
            "primaryDocument": ["", "", "", "", ""]}}})
        kinds = [d["kind"] for d in edgar.dilution_events(rows, 1, TODAY)]
        self.assertEqual(len(kinds), 2)  # 424B5 z 1.09 i S-3 z 2025; S-3 z 2023 i 424B5 sprzed 6 mies. odpadają
        self.assertEqual(len(edgar.delisting_events(rows, 1, TODAY)), 1)

    def test_form4_raw_document_url(self):
        rows = edgar.filings_table(fx.submissions(TODAY))
        docs = edgar.form4_docs(rows, fx.CIK, TODAY)
        self.assertTrue(docs[0]["url"].endswith("/000123456726000030/wk-form4_1.xml"))


class MarketTest(unittest.TestCase):
    def test_parsers(self):
        snap = market.finviz_snapshot(fx.FINVIZ_HTML)
        self.assertEqual(snap["Short Float"], "18.40%")
        self.assertEqual(market.num(snap["Shs Float"]), 45.2e6)
        self.assertEqual(market.finviz_earnings_date(snap["Earnings"], TODAY), date(2026, 11, 5))
        self.assertIsNone(market.finviz_earnings_date("Aug 05 AMC", TODAY))
        si = market.nasdaq_short_interest(fx.NASDAQ_SHORT)
        self.assertEqual((si["settlement_date"], si["days_to_cover"]), ("2026-09-15", 6.92))
        self.assertEqual(market.nasdaq_earnings_date(fx.NASDAQ_EARNINGS), date(2026, 11, 5))

    def test_universe_exclusion(self):
        rows = market.screener_rows(fx.screener("nasdaq"), "Nasdaq")
        self.assertEqual([market.universe_exclusion(r) is None for r in rows], [True, False, False])
        self.assertEqual(market.universe_exclusion(
            {"symbol": "ABCU", "name": "Alpha Acquisition Corp", "country": "United States", "industry": ""}),
            "SPAC (nazwa)")


class ScoringTest(unittest.TestCase):
    base = {"market_cap": 1e9, "avg_dollar_volume": 5e6, "fcf_ttm": 1e7, "rev_growth": 0.2, "beta": 1.5,
            "dist_from_high": -0.3, "above_sma50": True}

    def test_hard_filters(self):
        self.assertEqual(scoring.hard_filter_failures(self.base), [])
        self.assertEqual(len(scoring.hard_filter_failures({**self.base, "beta": 1.1, "going_concern": True})), 2)
        self.assertTrue(scoring.hard_filter_failures({**self.base, "rev_growth": None}))

    def test_dilution_disqualifies_squeeze(self):
        s = scoring.score({**self.base, "dilution": True, "short_float": 0.2})
        self.assertEqual(s["pkt_kary"], -2)
        self.assertTrue(s["dyskwalifikacja"])
        self.assertFalse(scoring.score({**self.base, "dilution": True})["dyskwalifikacja"])

    def test_points(self):
        s = scoring.score({**self.base, "fcf_ttm_prev": 5e6, "net_cash": True, "p_fcf": 15, "sma50_turning_up": True,
                           "short_float": 0.2, "days_to_cover": 6, "catalysts": ["a", "b", "c"],
                           "insider_sell_usd": 2e6, "top_customer_share": 0.4})
        self.assertEqual((s["pkt_fundamenty"], s["pkt_technika"], s["pkt_squeeze"], s["pkt_katalizator"]), (4, 1, 2, 2))
        self.assertEqual(s["wynik"], 9 - 2)


class FakeFetcher(Fetcher):
    """Serwuje odpowiedzi z fixture'ów zamiast sieci."""

    def __init__(self):
        self.calls = []
        bench, stock = fx.prices(TODAY)
        cf = fx.companyfacts()
        full = {"cik": fx.CIK, "entityName": "Test Corp", "facts": {
            "us-gaap": {k: {"units": v} for k, v in cf["facts"].items() if not k.startswith("Entity")},
            "dei": {k: {"units": v} for k, v in cf["facts"].items() if k.startswith("Entity")}}}
        self.routes = [
            ("api/screener/stocks", lambda u: fx.screener(u.rsplit("=", 1)[1])),
            ("company_tickers_exchange", lambda u: fx.SEC_TICKERS),
            ("submissions/CIK0001234567", lambda u: fx.submissions(TODAY)),
            ("companyfacts/CIK0001234567", lambda u: full),
            ("chart/SPY", lambda u: bench),
            ("chart/TEST", lambda u: stock),
            ("efts.sec.gov", lambda u: {"hits": {"hits": []}}),
            ("wk-form4_1.xml", lambda u: fx.FORM4_XML.format(d="2026-09-18")),
            ("finviz.com", lambda u: fx.FINVIZ_HTML),
            ("short-interest", lambda u: fx.NASDAQ_SHORT),
            ("earnings-date", lambda u: fx.NASDAQ_EARNINGS),
        ]

    def get(self, url, as_json=True, transform=None):
        self.calls.append(url)
        for pat, fn in self.routes:
            if pat in url:
                data = fn(url)
                return Doc(url, "2026-10-08T12:00:00+00:00", transform(data) if transform else data)
        raise NotFound(url)


def stooq_csv(chart_js):
    df, _ = from_yahoo_chart(chart_js)
    lines = ["Date,Open,High,Low,Close,Volume"]
    lines += [f"{i.date()},{r.open},{r.high},{r.low},{r.close},{r.volume:.0f}" for i, r in df.iterrows()]
    return "\n".join(lines) + "\n"


class EndToEndTest(unittest.TestCase):
    def test_stooq_fallback_when_yahoo_fails(self):
        f = FakeFetcher()
        stock = fx.prices(TODAY)[1]
        f.routes = [r for r in f.routes if r[0] != "chart/TEST"] + [("stooq.com", lambda u: stooq_csv(stock))]
        with tempfile.TemporaryDirectory() as tmp:
            run.main(["--out", tmp, "--as-of", TODAY.isoformat(), "--overrides", f"{tmp}/none.csv"], fetcher=f)
            self.assertIn("https://stooq.com/q/d/l/?s=test.us&i=d", f.calls)
            src = list(csv.DictReader(open(Path(tmp) / "sources.csv", encoding="utf-8")))
            self.assertEqual({s["zrodlo"] for s in src if s["metryka"] == "kurs"}, {"Stooq (wyliczone)"})
            rows = list(csv.DictReader(open(Path(tmp) / "results.csv", encoding="utf-8")))
            self.assertEqual([r["ticker"] for r in rows], ["TEST"])

    def test_stooq_error_page_is_rejected(self):
        with self.assertRaises(ValueError):
            from_stooq_csv("Exceeded the daily hits limit")

    def test_full_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = FakeFetcher()
            rc = run.main(["--out", tmp, "--as-of", TODAY.isoformat(), "--overrides", f"{tmp}/none.csv"], fetcher=f)
            self.assertEqual(rc, 0)
            rows = list(csv.DictReader(open(Path(tmp) / "results.csv", encoding="utf-8")))
            self.assertEqual([r["ticker"] for r in rows], ["TEST"])
            r = rows[0]
            self.assertEqual(r["wzrost_przychodow_rr"], "19.8%")
            self.assertEqual(r["short_float"], "18.4%")
            self.assertEqual(r["days_to_cover"], "6.9")
            self.assertEqual(r["pkt_squeeze"], "2")
            self.assertEqual(r["pkt_katalizator"], "2")
            self.assertIn("wyniki 2026-11-05", r["katalizatory"])
            self.assertEqual(r["gotowka_netto"], "tak")
            self.assertEqual(r["fcf_ttm_usd"], "49,000,000")
            src = list(csv.DictReader(open(Path(tmp) / "sources.csv", encoding="utf-8")))
            self.assertTrue(all(s["data_danych"] for s in src))
            excl = list(csv.DictReader(open(Path(tmp) / "excluded.csv", encoding="utf-8")))
            self.assertEqual({e["ticker"] for e in excl}, {"TESTW", "CHN"})
            self.assertIn("TEST", (Path(tmp) / "raport.md").read_text(encoding="utf-8"))

    def test_override_wins_and_logs_discrepancy(self):
        with tempfile.TemporaryDirectory() as tmp:
            ov = Path(tmp) / "ov.csv"
            ov.write_text("ticker,metric,value,source_url,as_of,note\n"
                          "TEST,rev_growth,0.12,https://www.sec.gov/x,2026-06-30,10-Q str. 4\n"
                          "TEST,top_customer_share,0.35,https://www.sec.gov/x,2026-06-30,\n", encoding="utf-8")
            run.main(["--out", tmp, "--as-of", TODAY.isoformat(), "--overrides", str(ov)], fetcher=FakeFetcher())
            r = next(csv.DictReader(open(Path(tmp) / "results.csv", encoding="utf-8")))
            self.assertEqual(r["wzrost_przychodow_rr"], "12.0%")
            self.assertIn("klient > 30%", r["kary"])
            disc = {d["metryka"]: d for d in csv.DictReader(open(Path(tmp) / "discrepancies.csv", encoding="utf-8"))}
            self.assertEqual(disc["rev_growth"]["raport"], "0.12")
            self.assertEqual(disc["rev_growth"]["zrodlo_raportu"], "https://www.sec.gov/x")
            # top_customer_share nie miał wartości ze screenera, więc to nie rozbieżność
            self.assertNotIn("top_customer_share", disc)


if __name__ == "__main__":
    unittest.main()
