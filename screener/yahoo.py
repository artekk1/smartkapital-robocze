"""Yahoo Finance: notowania dzienne (chart API) i dane ze sprawozdań (fundamentals-timeseries)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=2y&interval=1d&includeAdjustedClose=true"
TIMESERIES_URL = ("https://query1.finance.yahoo.com/ws/fundamentals-timeseries/v1/finance/timeseries/{sym}"
                  "?type={types}&period1={p1}&period2={p2}")
TYPES = [
    "quarterlyTotalRevenue", "trailingTotalRevenue",
    "trailingFreeCashFlow", "quarterlyFreeCashFlow", "trailingOperatingCashFlow", "trailingCapitalExpenditure",
    "quarterlyTotalDebt", "quarterlyCapitalLeaseObligations",
    "quarterlyCashCashEquivalentsAndShortTermInvestments",
    "trailingEBITDA", "quarterlyEBITDA",
    "quarterlyOrdinarySharesNumber",
    "trailingNetIncome", "trailingTotalUnusualItems",
]


def symbol(sym: str) -> str:
    return sym.replace("/", "-").replace(".", "-")


def timeseries_url(sym: str, today: date) -> str:
    p1 = int(datetime(today.year - 3, today.month, 1, tzinfo=timezone.utc).timestamp())
    p2 = int(datetime(today.year, today.month, today.day, tzinfo=timezone.utc).timestamp()) + 86400
    return TIMESERIES_URL.format(sym=symbol(sym), types=",".join(TYPES), p1=p1, p2=p2)


def parse_timeseries(js: dict) -> dict[str, list[tuple[date, float]]]:
    """typ -> [(data okresu, wartość)] rosnąco po dacie."""
    out: dict[str, list[tuple[date, float]]] = {}
    for r in ((js.get("timeseries") or {}).get("result")) or []:
        t = ((r.get("meta") or {}).get("type") or [None])[0]
        vals = [v for v in (r.get(t) or []) if v and v.get("reportedValue")]
        if t and vals:
            out[t] = sorted((date.fromisoformat(v["asOfDate"]), float(v["reportedValue"]["raw"])) for v in vals)
    return out


def _at(series: list[tuple[date, float]], when: date, tol: int = 20) -> tuple[date, float] | None:
    near = [p for p in series if abs((p[0] - when).days) <= tol]
    return min(near, key=lambda p: abs((p[0] - when).days)) if near else None


def fundamentals(ts: dict[str, list[tuple[date, float]]]) -> dict:
    """Wskaźniki potrzebne do filtrów i punktacji. Klucz 'missing' wymienia brakujące dane."""
    m: dict = {"missing": []}
    rev = ts.get("quarterlyTotalRevenue", [])
    if rev:
        end, cur = rev[-1]
        prev = _at(rev, end - timedelta(days=365))
        m["revenue_q_end"], m["revenue_q"] = end, cur
        if prev:
            m["revenue_q_prev_end"], m["revenue_q_prev"] = prev
            m["rev_growth"] = cur / prev[1] - 1 if prev[1] > 0 else float("nan")
        else:
            m["missing"].append("przychody kwartał rok wcześniej")
    else:
        m["missing"].append("przychody kwartalne")

    fcf = ts.get("trailingFreeCashFlow", [])
    if fcf:
        m["fcf_end"], m["fcf_ttm"] = fcf[-1]
        prev = _at(fcf, fcf[-1][0] - timedelta(days=365))
        if prev:
            m["fcf_ttm_prev_end"], m["fcf_ttm_prev"] = prev
    else:
        q = ts.get("quarterlyFreeCashFlow", [])
        last4 = q[-4:]
        if len(last4) == 4 and (last4[-1][0] - last4[0][0]).days <= 290:
            m["fcf_end"], m["fcf_ttm"] = last4[-1][0], sum(v for _, v in last4)
            m["fcf_from_quarters"] = True
        else:
            m["missing"].append("FCF TTM")

    debt = ts.get("quarterlyTotalDebt", [])
    cash = ts.get("quarterlyCashCashEquivalentsAndShortTermInvestments", [])
    if cash:
        bs_end, m["cash"] = cash[-1]
        m["bs_date"] = bs_end
        d = _at(debt, bs_end, 5)
        leases = _at(ts.get("quarterlyCapitalLeaseObligations", []), bs_end, 5)
        # TotalDebt w Yahoo zawiera zobowiązania leasingowe (także operacyjne) - odejmujemy je.
        m["debt_gross"] = d[1] if d else 0.0
        m["leases"] = leases[1] if leases else 0.0
        m["debt"] = max(m["debt_gross"] - m["leases"], 0.0)
        m["net_debt"] = m["debt"] - m["cash"]
        m["net_cash"] = m["net_debt"] < 0
    eb = ts.get("trailingEBITDA", [])
    if eb:
        m["ebitda_end"], m["ebitda_ttm"] = eb[-1]
    elif len(ts.get("quarterlyEBITDA", [])) >= 4:
        q = ts["quarterlyEBITDA"][-4:]
        m["ebitda_end"], m["ebitda_ttm"] = q[-1][0], sum(v for _, v in q)
    if m.get("ebitda_ttm") and m["ebitda_ttm"] > 0 and "net_debt" in m:
        m["net_debt_ebitda"] = m["net_debt"] / m["ebitda_ttm"]

    sh = ts.get("quarterlyOrdinarySharesNumber", [])
    if sh:
        m["shares_end"], m["shares"] = sh[-1]
        half = _at(sh, sh[-1][0] - timedelta(days=182))
        year = _at(sh, sh[-1][0] - timedelta(days=365))
        if half and half[1] > 0:
            m["shares_chg_6m"] = sh[-1][1] / half[1] - 1
        if year and year[1] > 0:
            m["shares_chg_1y"] = sh[-1][1] / year[1] - 1

    ni = ts.get("trailingNetIncome", [])
    un = ts.get("trailingTotalUnusualItems", [])
    if ni:
        m["ni_end"], m["net_income_ttm"] = ni[-1]
        u = _at(un, ni[-1][0], 5)
        if u:
            m["unusual_ttm"] = u[1]
            # Jednorazówki (dodatnie) > 25% zysku netto TTM -> zysk podbity jednorazówką.
            m["one_off"] = bool(u[1] > 0 and ni[-1][1] > 0 and u[1] > 0.25 * ni[-1][1])
    return m
