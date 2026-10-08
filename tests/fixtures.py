"""Syntetyczne odpowiedzi źródeł danych do testów offline (format jak w prawdziwych API)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import numpy as np

CIK = 1234567
ACCN_10Q = "0001234567-26-000020"
ACCN_10K = "0001234567-26-000005"


def _fact(start, end, val, accn=ACCN_10Q, form="10-Q", filed="2026-08-05"):
    d = {"end": end, "val": val, "accn": accn, "form": form, "filed": filed}
    if start:
        d["start"] = start
    return d


def companyfacts() -> dict:
    """Rok obrotowy = kalendarzowy. Przychody rosną 20% r/r, CFO raportowane narastająco."""
    rev = []
    base = {2024: 100.0, 2025: 120.0, 2026: 144.0}
    q_ranges = [("01-01", "03-31"), ("04-01", "06-30"), ("07-01", "09-30"), ("10-01", "12-31")]
    for y, b in base.items():
        for qi, (s, e) in enumerate(q_ranges):
            if y == 2026 and qi > 1:
                break
            if qi == 3:
                continue  # Q4 tylko w 10-K jako rok - 9M
            rev.append(_fact(f"{y}-{s}", f"{y}-{e}", (b + qi) * 1e6))
        if y < 2026:
            rev.append(_fact(f"{y}-01-01", f"{y}-09-30", (3 * b + 3) * 1e6))
            rev.append(_fact(f"{y}-01-01", f"{y}-12-31", (4 * b + 6) * 1e6, ACCN_10K, "10-K", f"{y + 1}-02-20"))
    ocf = [
        _fact("2024-01-01", "2024-06-30", 15e6, filed="2024-08-05"),
        _fact("2024-01-01", "2024-12-31", 40e6, ACCN_10K, "10-K", "2025-02-20"),
        _fact("2025-01-01", "2025-06-30", 18e6),
        _fact("2025-01-01", "2025-12-31", 50e6, ACCN_10K, "10-K", "2026-02-20"),
        _fact("2026-01-01", "2026-06-30", 30e6),
    ]
    capex = [
        _fact("2024-01-01", "2024-06-30", 4e6, filed="2024-08-05"),
        _fact("2024-01-01", "2024-12-31", 10e6, ACCN_10K, "10-K", "2025-02-20"),
        _fact("2025-01-01", "2025-06-30", 5e6),
        _fact("2025-01-01", "2025-12-31", 12e6, ACCN_10K, "10-K", "2026-02-20"),
        _fact("2026-01-01", "2026-06-30", 6e6),
    ]
    op = [
        _fact("2025-01-01", "2025-06-30", 20e6),
        _fact("2025-01-01", "2025-12-31", 45e6, ACCN_10K, "10-K", "2026-02-20"),
        _fact("2026-01-01", "2026-06-30", 28e6),
    ]
    da = [
        _fact("2025-01-01", "2025-06-30", 4e6),
        _fact("2025-01-01", "2025-12-31", 8e6, ACCN_10K, "10-K", "2026-02-20"),
        _fact("2026-01-01", "2026-06-30", 5e6),
    ]
    ni = [
        _fact("2025-01-01", "2025-06-30", 15e6),
        _fact("2025-01-01", "2025-12-31", 33e6, ACCN_10K, "10-K", "2026-02-20"),
        _fact("2026-01-01", "2026-06-30", 21e6),
    ]
    return {
        "cik": CIK, "entityName": "Test Corp",
        "facts": {
            "RevenueFromContractWithCustomerExcludingAssessedTax": {"USD": rev},
            "NetCashProvidedByUsedInOperatingActivities": {"USD": ocf},
            "PaymentsToAcquirePropertyPlantAndEquipment": {"USD": capex},
            "OperatingIncomeLoss": {"USD": op},
            "DepreciationDepletionAndAmortization": {"USD": da},
            "NetIncomeLoss": {"USD": ni},
            "CashAndCashEquivalentsAtCarryingValue": {"USD": [_fact(None, "2026-06-30", 80e6)]},
            "LongTermDebt": {"USD": [_fact(None, "2026-06-30", 30e6)]},
            "EntityCommonStockSharesOutstanding": {"shares": [_fact(None, "2026-07-31", 50e6)]},
        },
    }


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


def submissions(today: date) -> dict:
    f = lambda d: (today - timedelta(days=d)).isoformat()
    rows = [
        ("0001234567-26-000020", f(64), "2026-06-30", "10-Q", "", "test-20260630.htm"),
        ("0001234567-26-000030", f(20), "", "4", "", "xslF345X05/wk-form4_1.xml"),
        ("0001234567-26-000031", f(30), "", "8-K", "2.02,9.01", "ex.htm"),
        ("0001234567-24-000040", f(900), "", "S-8", "", "s8.htm"),
    ]
    keys = ["accessionNumber", "filingDate", "reportDate", "form", "items", "primaryDocument"]
    return {"cik": str(CIK), "name": "Test Corp", "sic": "3674", "sicDescription": "Semiconductors",
            "addresses": {"business": {"stateOrCountry": "CA", "stateOrCountryDescription": "CA"}},
            "filings": {"recent": {k: [r[i] for r in rows] for i, k in enumerate(keys)}, "files": []}}


FORM4_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer><issuerCik>0001234567</issuerCik><issuerTradingSymbol>TEST</issuerTradingSymbol></issuer>
  <reportingOwner>
    <reportingOwnerId><rptOwnerName>Doe Jane</rptOwnerName></reportingOwnerId>
    <reportingOwnerRelationship><isDirector>0</isDirector><isOfficer>1</isOfficer><officerTitle>CEO</officerTitle></reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>{d}</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>12.50</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>"""

FINVIZ_HTML = """<html><body><table class="js-snapshot-table snapshot-table2 screener_snapshot-table-body">
<tr><td class="snapshot-td2">Market Cap</td><td class="snapshot-td2"><b>1.05B</b></td>
<td class="snapshot-td2">Short Float</td><td class="snapshot-td2"><b><span>18.40%</span></b></td></tr>
<tr><td class="snapshot-td2">Short Ratio</td><td class="snapshot-td2"><b>6.10</b></td>
<td class="snapshot-td2">Beta</td><td class="snapshot-td2"><b>1.75</b></td></tr>
<tr><td class="snapshot-td2">Earnings</td><td class="snapshot-td2"><b>Nov 05 AMC</b></td>
<td class="snapshot-td2">Shs Float</td><td class="snapshot-td2"><b>45.20M</b></td></tr>
</table></body></html>"""

NASDAQ_SHORT = {"data": {"symbol": "TEST", "shortInterestTable": {"rows": [
    {"settlementDate": "09/15/2026", "interest": "8,300,000", "avgDailyShareVolume": "1,200,000", "daysToCover": "6.92"},
    {"settlementDate": "08/29/2026", "interest": "7,900,000", "avgDailyShareVolume": "1,100,000", "daysToCover": "7.18"},
]}}}

NASDAQ_EARNINGS = {"data": {"announcement": "Earnings announcement* for TEST: Nov 05, 2026",
                            "reportText": "Test Corp is expected to report earnings on 11/05/2026 after market close."}}


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


SEC_TICKERS = {"fields": ["cik", "name", "ticker", "exchange"],
               "data": [[CIK, "Test Corp", "TEST", "Nasdaq"], [7654321, "China Co", "CHN", "Nasdaq"]]}
