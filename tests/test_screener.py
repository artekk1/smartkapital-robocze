import csv
import tempfile
import unittest
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from screener import checks, market, run, scoring, yahoo
from screener.net import Doc, Fetcher, NotFound
from screener.tech import from_stooq_csv, from_yahoo_chart, technicals
from tests import fixtures as fx

TODAY = date(2026, 10, 8)


def read_csv(path):
    with open(path, encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


class YahooTest(unittest.TestCase):
    def test_fundamentals(self):
        m = yahoo.fundamentals(yahoo.parse_timeseries(fx.yahoo_timeseries()))
        self.assertAlmostEqual(m["rev_growth"], 145 / 121 - 1)
        self.assertEqual((m["fcf_ttm"], m["fcf_ttm_prev"]), (49e6, 32e6))
        self.assertEqual(m["debt"], 30e6)  # 45 mln długu minus 15 mln leasingu
        self.assertTrue(m["net_cash"])
        self.assertAlmostEqual(m["net_debt_ebitda"], -50 / 62)
        self.assertAlmostEqual(m["shares_chg_6m"], 0.0)
        self.assertFalse(m["one_off"])
        self.assertEqual(m["missing"], [])

    def test_one_off_and_dilution_signal(self):
        m = yahoo.fundamentals(yahoo.parse_timeseries(fx.yahoo_timeseries(shares_6m_ago=45e6, unusual=15e6)))
        self.assertTrue(m["one_off"])  # 15 mln > 25% z 39 mln zysku
        self.assertAlmostEqual(m["shares_chg_6m"], 50 / 45 - 1)

    def test_url_has_all_types(self):
        url = yahoo.timeseries_url("BRK/B", TODAY)
        self.assertIn("/BRK-B?type=quarterlyTotalRevenue,", url)


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
        self.assertLess(t["invalidation"], t["price"])
        self.assertTrue(t["invalidation_basis"])

    def test_stooq_error_page_is_rejected(self):
        with self.assertRaises(ValueError):
            from_stooq_csv("Exceeded the daily hits limit")


class MarketTest(unittest.TestCase):
    def test_finviz(self):
        page = fx.finviz_page()
        snap = market.finviz_snapshot(page)
        self.assertEqual((snap["Short Float"], snap["Beta"], snap["52W High"]), ("18.40%", "1.75", "23.37 -39.00%"))
        self.assertEqual(market.finviz_categories(page),
                         {"sector": "Technology", "industry": "Semiconductors", "country": "USA"})
        news = market.finviz_news(page, TODAY)
        self.assertEqual([n["date"] for n in news], [date(2026, 10, 1)] * 2)  # wiersz z samą godziną dziedziczy datę
        self.assertEqual(news[0]["source"], "Business Wire")
        self.assertEqual(market.finviz_news(fx.finviz_page([("Today 07:30AM", "X")]), TODAY)[0]["date"], TODAY)
        self.assertEqual(market.finviz_earnings_date("Nov 05 AMC", TODAY), date(2026, 11, 5))
        self.assertIsNone(market.finviz_earnings_date("Aug 05 AMC", TODAY))

    def test_nasdaq(self):
        si = market.nasdaq_short_interest(fx.NASDAQ_SHORT)
        self.assertEqual((si["settlement_date"], si["days_to_cover"]), ("2026-09-15", 6.92))
        self.assertEqual(market.nasdaq_earnings_date(fx.NASDAQ_EARNINGS), (date(2026, 11, 5), False))
        self.assertEqual(market.nasdaq_earnings_date(fx.NASDAQ_EARNINGS_EST), (date(2026, 11, 5), True))
        self.assertEqual(market.nasdaq_filings(fx.nasdaq_filings())[0]["filed"], date(2026, 9, 20))
        tr = market.nasdaq_insider_trades(fx.nasdaq_insider())
        self.assertEqual((tr[0]["type"], tr[0]["value"]), ("Buy", 125000.0))

    def test_universe_exclusion(self):
        rows = market.screener_rows(fx.screener("nasdaq"), "Nasdaq")
        self.assertEqual([market.universe_exclusion(r) is None for r in rows], [True, False, False])
        self.assertEqual(market.universe_exclusion(
            {"symbol": "ABCU", "name": "Alpha Acquisition Corp", "country": "United States", "industry": ""}),
            "SPAC (nazwa)")


class ChecksTest(unittest.TestCase):
    def test_dilution(self):
        filings = market.nasdaq_filings(fx.nasdaq_filings([("S-3", "06/01/2026"), ("424B5", "09/01/2026"),
                                                           ("424B5", "01/02/2026"), ("S-8", "09/01/2026")]))
        news = [{"date": date(2026, 9, 2), "title": "Test Corp Establishes $50 Million At-The-Market Program", "url": "u"},
                {"date": date(2026, 9, 3), "title": "Test Corp Prices $300 Million Senior Notes Offering", "url": "u"}]
        ev = checks.dilution(filings, {"shares_chg_6m": 0.06, "shares_end": date(2026, 6, 30)}, news, TODAY)
        kinds = [e["kind"] for e in ev]
        self.assertEqual(kinds, ["shelf S-3", "prospekt 424B5", "liczba akcji +6.0% w 6 mies.", "program ATM (wiadomość)"])
        self.assertTrue(checks.has_atm(ev))
        self.assertEqual(checks.dilution([], {"shares_chg_6m": 0.02}, [], TODAY), [])

    def test_red_flags(self):
        news = [{"date": date(2026, 3, 1), "title": "Test Corp Receives Nasdaq Minimum Bid Price Deficiency Notice"},
                {"date": date(2026, 5, 1), "title": "Test Corp Regains Compliance with Nasdaq Listing Rule"},
                {"date": date(2026, 9, 1), "title": "ROSEN LAW FIRM Files Securities Class Action Lawsuit Against Test Corp"},
                {"date": date(2026, 9, 2), "title": "Pomerantz Law Firm Investigates Claims On Behalf of Investors of Test Corp"}]
        f = checks.red_flags(news, TODAY)
        self.assertEqual((f["delisting"], f["going_concern"]), ([], []))
        self.assertEqual(len(f["legal"]), 1)
        self.assertEqual(len(f["verify"]), 1)
        self.assertTrue(checks.red_flags(news[:1], TODAY)["delisting"])

    def test_insiders_window(self):
        ins = checks.insiders(market.nasdaq_insider_trades(fx.nasdaq_insider()), TODAY)
        self.assertEqual((ins["buy_usd"], ins["sell_usd"]), (125000.0, 0))


class ScoringTest(unittest.TestCase):
    base = {"market_cap": 1e9, "avg_dollar_volume": 5e6, "fcf_ttm": 1e7, "rev_growth": 0.2, "beta": 1.5,
            "dist_from_high": -0.3, "above_sma50": True}

    def test_hard_filters(self):
        self.assertEqual(scoring.hard_filter_failures(self.base), [])
        self.assertEqual(len(scoring.hard_filter_failures({**self.base, "beta": 1.1, "going_concern": True})), 2)
        self.assertTrue(scoring.hard_filter_failures({**self.base, "rev_growth": None}))
        self.assertTrue(scoring.hard_filter_failures({**self.base, "beta_finviz": 1.1}))
        self.assertTrue(scoring.hard_filter_failures({**self.base, "dist_from_high_finviz": -0.2}))

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

    def __init__(self, finviz=None, filings=None, timeseries=None):
        self.calls = []
        bench, stock = fx.prices(TODAY)
        self.routes = [
            ("api/screener/stocks", lambda u: fx.screener(u.rsplit("=", 1)[1])),
            ("chart/SPY", lambda u: bench),
            ("chart/TEST", lambda u: stock),
            ("timeseries/TEST", lambda u: timeseries or fx.yahoo_timeseries()),
            ("finviz.com", lambda u: finviz or fx.finviz_page()),
            ("short-interest", lambda u: fx.NASDAQ_SHORT),
            ("earnings-date", lambda u: fx.NASDAQ_EARNINGS),
            ("sec-filings", lambda u: filings or fx.nasdaq_filings()),
            ("insider-trades", lambda u: fx.nasdaq_insider()),
        ]

    def get(self, url, as_json=True, transform=None):
        self.calls.append(url)
        for pat, fn in self.routes:
            if pat in url:
                data = fn(url)
                return Doc(url, "2026-10-08T12:00:00+00:00", transform(data) if transform else data)
        raise NotFound(url)


def run_fake(tmp, *extra, **kw):
    args = ["--out", tmp, "--as-of", TODAY.isoformat(), "--overrides", f"{tmp}/none.csv", "--notes", f"{tmp}/none.json"]
    return run.main(args + list(extra), fetcher=FakeFetcher(**kw))


class EndToEndTest(unittest.TestCase):
    def test_full_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(run_fake(tmp), 0)
            rows = read_csv(Path(tmp) / "results.csv")
            self.assertEqual([r["ticker"] for r in rows], ["TEST"])
            r = rows[0]
            self.assertEqual(list(r)[:16], ["ticker", "spolka", "sektor", "kapitalizacja_usd", "p_fcf",
                                           "wzrost_przychodow_rr", "beta", "odleglosc_od_szczytu", "short_float",
                                           "days_to_cover", "pkt_fundamenty", "pkt_technika", "pkt_squeeze",
                                           "pkt_katalizator", "kary", "wynik"])
            self.assertEqual((r["wzrost_przychodow_rr"], r["short_float"], r["days_to_cover"]), ("19.8%", "18.4%", "6.9"))
            self.assertEqual((r["pkt_squeeze"], r["pkt_katalizator"], r["top10"]), ("2", "2", "tak"))
            self.assertEqual(r["sektor"], "Technology")
            self.assertTrue(all(s["data_danych"] for s in read_csv(Path(tmp) / "sources.csv")))
            self.assertEqual({e["ticker"] for e in read_csv(Path(tmp) / "excluded.csv")}, {"TESTW", "CHN"})
            rep = (Path(tmp) / "raport.md").read_text(encoding="utf-8")
            for section in ("## TOP 1", "### 1. TEST", "**Teza.**", "**Ryzyko.**", "**Najbliższy katalizator.** wyniki",
                            "**Poziom unieważnienia.**", "najlepszych kandydatów na short squeeze", "| 1 | TEST |",
                            "## Odrzucone w ostatniej chwili", "## Metodologia i ograniczenia", "**Data danych.**"):
                self.assertIn(section, rep)

    def test_self_check_removes_atm_from_top(self):
        page = fx.finviz_page([("Sep-10-26 08:00AM", "Test Corp Enters Into At-The-Market Equity Offering Program")])
        with tempfile.TemporaryDirectory() as tmp:
            run_fake(tmp, finviz=page)
            r = read_csv(Path(tmp) / "results.csv")[0]
            self.assertEqual(r["top10"], "")
            self.assertTrue(r["dyskwalifikacja"])  # ATM przy kandydacie na squeeze
            rep = (Path(tmp) / "raport.md").read_text(encoding="utf-8")
            late = rep.split("## Odrzucone w ostatniej chwili")[1].split("## Metodologia")[0]
            self.assertIn("aktywny program ATM", late)
            self.assertIn("## TOP 0", rep)

    def test_red_flag_drops_company_late(self):
        page = fx.finviz_page([("Sep-10-26 08:00AM", "Test Corp Receives Nasdaq Notice of Delinquency and Deficiency")])
        with tempfile.TemporaryDirectory() as tmp:
            run_fake(tmp, finviz=page)
            self.assertEqual(read_csv(Path(tmp) / "results.csv"), [])
            rep = (Path(tmp) / "raport.md").read_text(encoding="utf-8")
            self.assertIn("F6: groźba delistingu", rep)

    def test_override_wins_and_logs_discrepancy(self):
        with tempfile.TemporaryDirectory() as tmp:
            ov = Path(tmp) / "ov.csv"
            ov.write_text("ticker,metric,value,source_url,as_of,note\n"
                          "TEST,rev_growth,0.12,https://example.org/q2,2026-06-30,komunikat wynikowy\n"
                          "TEST,top_customer_share,0.35,https://example.org/10k,2025-12-31,\n"
                          "TEST,catalyst,Kontrakt rządowy,https://example.org/pr,2026-10-01,\n", encoding="utf-8")
            run.main(["--out", tmp, "--as-of", TODAY.isoformat(), "--overrides", str(ov), "--notes", f"{tmp}/x.json"],
                     fetcher=FakeFetcher())
            r = read_csv(Path(tmp) / "results.csv")[0]
            self.assertEqual(r["wzrost_przychodow_rr"], "12.0%")
            self.assertIn("klient > 30%", r["opis_kar"])
            disc = [d for d in read_csv(Path(tmp) / "discrepancies.csv") if d["metryka"] == "rev_growth"]
            self.assertEqual(disc[0]["sprawozdanie_lub_wyliczenie"], "0.12")

    def test_notes_render_in_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            notes = Path(tmp) / "notes.json"
            notes.write_text('{"TEST": {"teza": "Teza testowa.", "ryzyko": "Ryzyko testowe.", '
                             '"katalizator": "Wyniki Q3 2026-11-05", "zrodla": [{"url": "https://example.org", "opis": "PR"}]}}',
                             encoding="utf-8")
            run.main(["--out", tmp, "--as-of", TODAY.isoformat(), "--overrides", f"{tmp}/none.csv", "--notes", str(notes)],
                     fetcher=FakeFetcher())
            rep = (Path(tmp) / "raport.md").read_text(encoding="utf-8")
            self.assertIn("**Teza.** Teza testowa.", rep)
            self.assertIn("**Najbliższy katalizator.** Wyniki Q3 2026-11-05", rep)
            self.assertIn("[PR](https://example.org)", rep)


if __name__ == "__main__":
    unittest.main()
