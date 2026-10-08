"""Nasdaq (uniwersum, short interest, daty wyników, lista zgłoszeń do SEC, insiderzy) i Finviz
(migawka wskaźników, branża, nagłówki wiadomości)."""
from __future__ import annotations

import html
import re
from datetime import date, datetime

NASDAQ_SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true&exchange={ex}"
STOOQ_URL = "https://stooq.com/q/d/l/?s={sym}.us&i=d"
FINVIZ_QUOTE_URL ="https://finviz.com/quote.ashx?t={sym}&p=d"
NASDAQ_SHORT_URL = "https://api.nasdaq.com/api/quote/{sym}/short-interest?assetClass=stocks"
NASDAQ_EARNINGS_URL = "https://api.nasdaq.com/api/analyst/{sym}/earnings-date"
NASDAQ_FILINGS_URL = ("https://api.nasdaq.com/api/company/{sym}/sec-filings?limit=200&sortColumn=filed"
                      "&sortOrder=desc&IsQuoteMedia=true")
NASDAQ_INSIDER_URL = ("https://api.nasdaq.com/api/company/{sym}/insider-trades?limit=200&type=ALL"
                      "&sortColumn=lastDate&sortOrder=DESC")

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


def finviz_symbol(sym: str) -> str:
    return sym.replace("/", "-").replace(".", "-")


def nasdaq_symbol(sym: str) -> str:
    return sym.replace("/", ".").replace("-", ".")


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


def _clean(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def finviz_snapshot(page: str) -> dict[str, str]:
    """Pary etykieta -> wartość z tabel 'snapshot-table2' na stronie spółki w Finviz.
    Nowy układ: etykiety i wartości w div.snapshot-td-label / div.snapshot-td-content;
    stary: naprzemienne komórki <td>."""
    pairs = re.findall(r'class="snapshot-td-label"[^>]*>(.*?)</div>.*?class="snapshot-td-content"[^>]*>(.*?)</div>',
                       page, re.S | re.I)
    if pairs:
        return {_clean(k): _clean(v) for k, v in pairs}
    out: dict[str, str] = {}
    for table in re.findall(r'<table[^>]*snapshot-table2[^>]*>(.*?)</table>', page, re.S | re.I):
        texts = [_clean(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", table, re.S | re.I)]
        out.update({texts[i]: texts[i + 1] for i in range(0, len(texts) - 1, 2)})
    return out


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
_US_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


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


def nasdaq_earnings_date(js: dict) -> tuple[date, bool] | None:
    """(data, czy szacunek). Nasdaq podaje datę potwierdzoną albo szacunek Zacks."""
    data = js.get("data") or {}
    for field in ("announcement", "reportText"):
        text = data.get(field) or ""
        estimated = "estimated" in text.lower() or "derived from an algorithm" in text.lower()
        m = _DATE_RE.search(text)
        if m:
            try:
                return datetime.strptime(" ".join(m.groups()), "%b %d %Y").date(), estimated
            except ValueError:
                pass
        m = _US_DATE_RE.search(text)
        if m:
            mo, d, y = map(int, m.groups())
            try:
                return date(y, mo, d), estimated
            except ValueError:
                pass
    return None


def _mdy(s: str) -> date | None:
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date()
    except (ValueError, AttributeError):
        return None


def nasdaq_filings(js: dict) -> list[dict]:
    """Lista zgłoszeń do SEC (ok. 6 ostatnich miesięcy) z Nasdaq/QuoteMedia."""
    out = []
    for r in ((js.get("data") or {}).get("rows")) or []:
        filed = _mdy(r.get("filed", ""))
        if filed:
            out.append({"form": (r.get("formType") or "").strip(), "filed": filed,
                        "owner": r.get("reportingOwner") or "",
                        "url": ((r.get("view") or {}).get("htmlLink")) or ""})
    return out


def nasdaq_insider_trades(js: dict) -> list[dict]:
    rows = ((((js.get("data") or {}).get("transactionTable")) or {}).get("table") or {}).get("rows") or []
    out = []
    for r in rows:
        d = _mdy(r.get("lastDate", ""))
        shares, price = num(r.get("sharesTraded")), num(r.get("lastPrice"))
        if d is None or shares is None:
            continue
        out.append({"insider": r.get("insider", ""), "relation": r.get("relation", ""), "date": d,
                    "type": (r.get("transactionType") or "").strip(), "shares": shares, "price": price or 0.0,
                    "value": shares * (price or 0.0)})
    return out


def finviz_categories(page: str) -> dict[str, str]:
    """Sektor, branża i kraj z nagłówka strony spółki w Finviz."""
    out = {}
    for key, code in (("sector", "sec_"), ("industry", "ind_"), ("country", "geo_")):
        m = re.search(r'<a href="screener[^"]*f=' + code + r'[^"]*"[^>]*>(.*?)</a>', page, re.S)
        if m:
            out[key] = _clean(m.group(1))
    return out


def finviz_news(page: str, today: date) -> list[dict]:
    """Nagłówki wiadomości z datami. Finviz podaje datę tylko w pierwszym wierszu danego dnia."""
    m = re.search(r'id="news-table"(.*?)</table>', page, re.S)
    if not m:
        return []
    out, current = [], None
    for row in m.group(1).split("<tr")[1:]:
        td = re.search(r"<td[^>]*>(.*?)</td>", row, re.S)
        link = re.search(r'class="tab-link-news"\s+href="([^"]*)"[^>]*>(.*?)</a>', row, re.S)
        if not td or not link:
            continue
        stamp = _clean(td.group(1))
        if stamp.startswith("Today"):
            current = today
        else:
            dm = re.match(r"([A-Z][a-z]{2}-\d{2}-\d{2})", stamp)
            if dm:
                current = datetime.strptime(dm.group(1), "%b-%d-%y").date()
        if current is None:
            continue
        src = re.search(r"news-link-right.*?<span>\(?([^<)]*)\)?</span>", row, re.S)
        href = link.group(1)
        out.append({"date": current, "title": _clean(link.group(2)), "source": src.group(1).strip() if src else "",
                    "url": href if href.startswith("http") else "https://finviz.com" + href})
    return out
