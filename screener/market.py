"""Dane rynkowe: uniwersum (Nasdaq screener), notowania (Yahoo), short interest i daty wyników
(Nasdaq), migawka screenera Finviz."""
from __future__ import annotations

import html
import re
from datetime import date, datetime

NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true&exchange={ex}"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=2y&interval=1d&includeAdjustedClose=true"
FINVIZ_QUOTE_URL = "https://finviz.com/quote.ashx?t={sym}&p=d"
NASDAQ_SHORT_URL = "https://api.nasdaq.com/api/quote/{sym}/short-interest?assetClass=stocks"
NASDAQ_EARNINGS_URL = "https://api.nasdaq.com/api/analyst/{sym}/earnings-date"

EXCHANGES = {"nasdaq": "Nasdaq", "nyse": "NYSE", "amex": "NYSE American"}
CHINA_COUNTRIES = {"China", "Hong Kong", "Macau"}
_NON_COMMON = re.compile(r"\b(warrants?|units?|rights?|preferred|depositary shares? representing|notes due|"
                         r"debentures?|%)", re.I)
_SPAC_NAME = re.compile(r"\bacquisition (corp|corporation|company|co)\b|\bblank check\b", re.I)


def num(s) -> float | None:
    """'$1,234.5' / '12.3%' / '1.2B' -> liczba; '-' / '' -> None."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = str(s).strip().replace(",", "").replace("$", "")
    if t in ("", "-", "N/A", "NA", "--"):
        return None
    mult = 1.0
    if t.endswith("%"):
        t = t[:-1]
    elif t[-1:] in ("K", "M", "B", "T"):
        mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[t[-1]]
        t = t[:-1]
    try:
        return float(t) * mult
    except ValueError:
        return None


def yahoo_symbol(sym: str) -> str:
    return sym.replace("/", "-").replace(".", "-")


def screener_rows(js: dict, exchange_label: str) -> list[dict]:
    rows = ((js.get("data") or {}).get("rows")) or []
    out = []
    for r in rows:
        out.append({
            "symbol": (r.get("symbol") or "").strip(),
            "name": (r.get("name") or "").strip(),
            "exchange": exchange_label,
            "last": num(r.get("lastsale")),
            "market_cap": num(r.get("marketCap")),
            "volume": num(r.get("volume")),
            "country": (r.get("country") or "").strip(),
            "sector": (r.get("sector") or "").strip(),
            "industry": (r.get("industry") or "").strip(),
        })
    return out


def universe_exclusion(row: dict) -> str | None:
    """Powód wykluczenia na etapie listy (przed pobraniem fundamentów) albo None."""
    sym, name = row["symbol"], row["name"]
    if not sym or "^" in sym or len(sym.replace("/", "").replace(".", "")) > 5:
        return "nie akcja zwykła (symbol)"
    if _NON_COMMON.search(name):
        return "nie akcja zwykła (nazwa)"
    if _SPAC_NAME.search(name):
        return "SPAC (nazwa)"
    if row["country"] in CHINA_COUNTRIES:
        return f"spółka z {row['country']}"
    if "real estate investment trust" in row["industry"].lower():
        return "REIT (branża)"
    return None


def finviz_snapshot(page: str) -> dict[str, str]:
    """Pary etykieta -> wartość z tabeli 'snapshot-table2' na stronie spółki w Finviz."""
    m = re.search(r'<table[^>]*snapshot-table2[^>]*>(.*?)</table>', page, re.S | re.I)
    if not m:
        return {}
    cells = re.findall(r"<td[^>]*>(.*?)</td>", m.group(1), re.S | re.I)
    texts = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in cells]
    return {texts[i]: texts[i + 1] for i in range(0, len(texts) - 1, 2)}


def nasdaq_short_interest(js: dict) -> dict | None:
    rows = (((js.get("data") or {}).get("shortInterestTable") or {}).get("rows")) or []
    if not rows:
        return None
    parsed = []
    for r in rows:
        try:
            d = datetime.strptime(r["settlementDate"], "%m/%d/%Y").date()
        except (KeyError, ValueError):
            continue
        parsed.append({"settlement_date": d.isoformat(), "short_shares": num(r.get("interest")),
                       "avg_daily_volume": num(r.get("avgDailyShareVolume")),
                       "days_to_cover": num(r.get("daysToCover"))})
    return max(parsed, key=lambda r: r["settlement_date"]) if parsed else None


_DATE_RE = re.compile(r"([A-Z][a-z]{2}) (\d{1,2}), (\d{4})")


def finviz_earnings_date(s: str, today: date) -> date | None:
    """'Nov 05 AMC' -> data. Finviz nie podaje roku; data w przeszłości to ostatnie wyniki,
    a nie następne, więc wtedy zwracamy None."""
    m = re.match(r"([A-Z][a-z]{2}) (\d{1,2})\b", s or "")
    if not m:
        return None
    try:
        d = datetime.strptime(f"{m.group(1)} {m.group(2)} {today.year}", "%b %d %Y").date()
    except ValueError:
        return None
    if d < today and (today - d).days > 180:  # np. 'Jan 20' oglądane w grudniu
        d = d.replace(year=today.year + 1)
    return d if d >= today else None


def nasdaq_earnings_date(js: dict) -> date | None:
    data = js.get("data") or {}
    for field in ("announcement", "reportText"):
        m = _DATE_RE.search(data.get(field) or "")
        if m:
            try:
                return datetime.strptime(" ".join(m.groups()), "%b %d %Y").date()
            except ValueError:
                continue
    return None
