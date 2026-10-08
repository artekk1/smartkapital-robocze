"""Syntetyczne odpowiedzi źródeł danych do testów offline (format jak w prawdziwych API)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np


def prices(today: date, n: int = 520, beta: float = 1.6, seed: int = 7):
    """(benchmark, akcja) jako odpowiedzi Yahoo chart: hossa, spadek ~45%, baza, odbicie."""
    rng = np.random.default_rng(seed)
    rb = rng.normal(0.0003, 0.006, n)
    drift = np.concatenate([np.full(250, 0.002), np.full(150, -0.004), np.full(80, 0.0), np.full(n - 480, 0.004)])
    rs = beta * rb + drift
    days = []
    d = today
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    ts = [int(datetime(x.year, x.month, x.day, 14, 30, tzinfo=timezone.utc).timestamp()) for x in days]

    def chart(rets, start, vol_spike=False):
        close = start * np.exp(np.cumsum(rets))
        vol = np.full(n, 1_000_000.0)
        if vol_spike:
            vol[-3:] = 3_000_000.0
        q = {"open": close.tolist(), "high": (close * 1.01).tolist(), "low": (close * 0.99).tolist(),
             "close": close.tolist(), "volume": vol.tolist()}
        return {"chart": {"result": [{"meta": {"currency": "USD"}, "timestamp": ts,
                                      "indicators": {"quote": [q], "adjclose": [{"adjclose": close.tolist()}]}}]}}
    return chart(rb, 400.0), chart(rs, 20.0, vol_spike=True)


def _ts(type_, points, period="3M"):
    return {"meta": {"symbol": ["TEST"], "type": [type_]}, "timestamp": [0] * len(points),
            type_: [{"asOfDate": d, "periodType": period, "currencyCode": "USD",
                     "reportedValue": {"raw": v, "fmt": ""}} for d, v in points]}


QUARTERS = ["2025-06-30", "2025-09-30", "2025-12-31", "2026-03-31", "2026-06-30"]


def yahoo_timeseries(shares_6m_ago: float = 50e6, unusual: float = 1e6) -> dict:
    """Przychody +19,8% r/r, FCF TTM 49 mln (rok wcześniej 32 mln), gotówka netto, 50 mln akcji."""
    return {"timeseries": {"result": [
        _ts("quarterlyTotalRevenue", list(zip(QUARTERS, [121e6, 122e6, 123e6, 144e6, 145e6]))),
        _ts("trailingFreeCashFlow", [("2025-06-30", 32e6), ("2026-06-30", 49e6)], "TTM"),
        _ts("quarterlyTotalDebt", [("2026-06-30", 45e6)]),
        _ts("quarterlyCapitalLeaseObligations", [("2026-06-30", 15e6)]),
        _ts("quarterlyCashCashEquivalentsAndShortTermInvestments", [("2026-06-30", 80e6)]),
        _ts("trailingEBITDA", [("2026-06-30", 62e6)], "TTM"),
        _ts("quarterlyOrdinarySharesNumber", list(zip(QUARTERS, [49e6, 49.5e6, shares_6m_ago, 50e6, 50e6]))),
        _ts("trailingNetIncome", [("2026-06-30", 39e6)], "TTM"),
        _ts("trailingTotalUnusualItems", [("2026-06-30", unusual)], "TTM"),
        {"meta": {"symbol": ["TEST"], "type": ["quarterlyEBITDA"]}, "timestamp": []},
    ]}}


def finviz_page(news_rows: list[tuple[str, str]] | None = None) -> str:
    snap = [("Index", "RUT"), ("Market Cap", "712.00M"), ("P/FCF", "14.20"), ("Sales Q/Q", "19.80%"),
            ("Earnings", "Nov 05 AMC"), ("Shs Float", "45.20M"), ("Short Float", "18.40%"), ("Short Ratio", "6.10"),
            ("52W High", "23.37 -39.00%"), ("Beta", "1.75"), ("Insider Trans", "2.10%")]
    cells = "".join(f'<td class="snapshot-td2"><div class="snapshot-td-label"><a href="x">{k}</a></div></td>'
                    f'<td class="snapshot-td2"><div class="snapshot-td-content"><b>{v}</b></div></td>' for k, v in snap)
    news_rows = news_rows or [("Oct-01-26 08:00AM", "Test Corp Wins Multi-Year Contract"),
                              ("07:00AM", "Test Corp to Present at Conference")]
    news = "".join(f'<tr class="cursor-pointer"><td width="130" align="right">{d}</td><td><div class="news-link-left">'
                   f'<a class="tab-link-news" href="/news/{i}/x" target="_blank">{t}</a></div>'
                   f'<div class="news-link-right"><span>(Business Wire)</span></div></td></tr>'
                   for i, (d, t) in enumerate(news_rows))
    return (f'<div><a href="screener?v=111&f=sec_technology" class="quote-header_category">Technology</a>'
            f'<a href="screener?v=111&f=ind_semiconductors" class="quote-header_category"><span>Semiconductors</span></a>'
            f'<a href="screener?v=111&f=geo_usa" class="quote-header_category">USA</a></div>'
            f'<table class="js-snapshot-table snapshot-table2 screener_snapshot-table-body"><tr>{cells}</tr></table>'
            f'<table id="news-table" class="news-table">{news}</table>')


NASDAQ_SHORT = {"data": {"symbol": "TEST", "shortInterestTable": {"rows": [
    {"settlementDate": "09/15/2026", "interest": "8,300,000", "avgDailyShareVolume": "1,200,000", "daysToCover": "6.92"},
    {"settlementDate": "08/29/2026", "interest": "7,900,000", "avgDailyShareVolume": "1,100,000", "daysToCover": "7.18"},
]}}}

NASDAQ_EARNINGS = {"data": {"announcement": "Earnings announcement* for TEST: Nov 05, 2026",
                            "reportText": "Test Corp is expected to report earnings on 11/05/2026 after market close."}}


NASDAQ_EARNINGS = {"data": {"announcement": "Earnings announcement* for TEST: Nov 05, 2026",
                            "reportText": "Test Corp is expected to report earnings on 11/05/2026 after market close."}}

NASDAQ_EARNINGS_EST = {"data": {"announcement": "", "reportText":
    "Test Corp is estimated to report earnings on  11/05/2026. The upcoming earnings date is derived from an algorithm."}}



def nasdaq_filings(rows: list[tuple[str, str]] | None = None) -> dict:
    rows = rows or [("4", "09/20/2026"), ("8-K", "08/05/2026"), ("10-Q", "08/05/2026"), ("S-8", "05/01/2026")]
    return {"data": {"rows": [{"formType": f, "filed": d, "reportingOwner": "", "view": {"htmlLink": f"https://q/{i}"}}
                              for i, (f, d) in enumerate(rows)]}}


def nasdaq_insider(rows: list[tuple] | None = None) -> dict:
    rows = rows or [("DOE JANE", "CEO", "9/18/2026", "Buy", "10,000", "$12.50"),
                    ("ROE RICH", "Director", "4/02/2026", "Sell", "200,000", "$20.00")]
    return {"data": {"transactionTable": {"totalRecords": str(len(rows)), "table": {"rows": [
        {"insider": a, "relation": b, "lastDate": c, "transactionType": d, "ownType": "Direct",
         "sharesTraded": e, "lastPrice": f, "sharesHeld": "1"} for a, b, c, d, e, f in rows]}}}}


def screener(exchange: str) -> dict:
    rows = []
    if exchange == "nasdaq":
        rows = [
            {"symbol": "TEST", "name": "Test Corp Common Stock", "lastsale": "$21.00", "marketCap": "1050000000.00",
             "volume": "1000000", "country": "United States", "sector": "Technology", "industry": "Semiconductors"},
            {"symbol": "TESTW", "name": "Test Corp Warrants", "lastsale": "$1.00", "marketCap": "500000000",
             "volume": "100", "country": "United States", "sector": "", "industry": ""},
            {"symbol": "CHN", "name": "China Co American Depositary Shares", "lastsale": "$5.00",
             "marketCap": "800000000", "volume": "100", "country": "China", "sector": "", "industry": ""},
        ]
    return {"data": {"rows": rows}}


