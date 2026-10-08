"""Źródła pierwotne z SEC EDGAR: lista zgłoszeń, formularze 4, wyszukiwanie pełnotekstowe."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date, timedelta
from urllib.parse import quote

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBMISSIONS_PAGE_URL = "https://data.sec.gov/submissions/{name}"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
EFTS_URL = ("https://efts.sec.gov/LATEST/search-index?q={q}&ciks={cik:010d}&forms={forms}"
            "&dateRange=custom&startdt={start}&enddt={end}")

SPAC_SIC = {"6770"}
REIT_SIC = {"6798"}
# Kody EDGAR dla siedziby: F4 = Chiny, K3 = Hongkong, 1N = Makau.
CHINA_CODES = {"F4", "K3", "1N"}

SHELF_FORMS = {"S-3", "S-3ASR", "S-3/A", "F-3", "F-3ASR", "F-3/A"}
PROSPECTUS_FORMS = {"424B1", "424B2", "424B3", "424B4", "424B5", "424B7"}
RESALE_FORMS = {"S-1", "S-1/A", "F-1", "F-1/A"}


def archive_url(cik: int, accn: str, doc: str = "") -> str:
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn.replace('-', '')}"
    return f"{base}/{doc}" if doc else f"{base}/{accn}-index.htm"


def filings_table(sub: dict) -> list[dict]:
    """Spłaszcza kolumnowy format `filings.recent` do listy słowników."""
    rec = sub.get("filings", {}).get("recent", sub)
    keys = ["accessionNumber", "filingDate", "reportDate", "form", "items", "primaryDocument"]
    cols = {k: rec.get(k, []) for k in keys}
    n = len(cols["accessionNumber"])
    rows = []
    for i in range(n):
        row = {k: (cols[k][i] if i < len(cols[k]) else "") for k in keys}
        row["filingDate"] = date.fromisoformat(row["filingDate"])
        rows.append(row)
    return rows


def company_profile(sub: dict) -> dict:
    addr = (sub.get("addresses") or {}).get("business") or {}
    return {
        "name": sub.get("name", ""),
        "sic": str(sub.get("sic") or ""),
        "sic_description": sub.get("sicDescription") or "",
        "country_code": addr.get("stateOrCountry") or "",
        "country_desc": addr.get("stateOrCountryDescription") or "",
        "category": sub.get("category") or "",
    }


def dilution_events(rows: list[dict], cik: int, today: date) -> list[dict]:
    """S-3/F-3 z ostatnich 3 lat (shelf jest ważny 3 lata), prospekty 424B i rejestracje
    odsprzedaży (S-1) z ostatnich 6 miesięcy."""
    out = []
    for r in rows:
        age = (today - r["filingDate"]).days
        form = r["form"]
        kind = None
        if form in SHELF_FORMS and age <= 3 * 365:
            kind = "shelf S-3/F-3"
        elif form in PROSPECTUS_FORMS and age <= 183:
            kind = f"prospekt {form} (emisja/odsprzedaż/ATM)"
        elif form in RESALE_FORMS and age <= 183:
            kind = f"rejestracja {form}"
        if kind:
            out.append({"kind": kind, "form": form, "date": r["filingDate"].isoformat(),
                        "url": archive_url(cik, r["accessionNumber"])})
    return out


def delisting_events(rows: list[dict], cik: int, today: date) -> list[dict]:
    """8-K z pozycją 3.01 (zawiadomienie o delistingu / niespełnieniu wymogów) z 12 miesięcy.
    Pozycja 3.01 bywa też używana przy dobrowolnym przeniesieniu notowań, stąd weryfikacja."""
    out = []
    for r in rows:
        if r["form"].startswith("8-K") and "3.01" in (r.get("items") or "") \
                and (today - r["filingDate"]).days <= 365:
            out.append({"form": r["form"], "date": r["filingDate"].isoformat(),
                        "url": archive_url(cik, r["accessionNumber"])})
    return out


def latest_periodic(rows: list[dict]) -> dict | None:
    periodic = [r for r in rows if r["form"] in ("10-Q", "10-K", "10-Q/A", "10-K/A")]
    return max(periodic, key=lambda r: r["filingDate"]) if periodic else None


def form4_docs(rows: list[dict], cik: int, today: date, days: int = 90) -> list[dict]:
    out = []
    for r in rows:
        if r["form"] in ("4", "4/A") and (today - r["filingDate"]).days <= days + 5:
            doc = r["primaryDocument"]
            # primaryDocument wskazuje wersję z arkuszem XSL, np. "xslF345X05/form4.xml";
            # surowy XML leży w katalogu zgłoszenia pod samą nazwą pliku.
            raw = doc.split("/", 1)[1] if doc.startswith("xsl") and "/" in doc else doc
            out.append({"date": r["filingDate"].isoformat(), "url": archive_url(cik, r["accessionNumber"], raw),
                        "index_url": archive_url(cik, r["accessionNumber"])})
    return out


def _text(el, path: str) -> str:
    node = el.find(path)
    return (node.text or "").strip() if node is not None and node.text else ""


def parse_form4(xml_text: str) -> list[dict]:
    """Transakcje z tabeli non-derivative formularza 4."""
    root = ET.fromstring(xml_text)
    owners = [_text(o, "reportingOwnerId/rptOwnerName") for o in root.findall("reportingOwner")]
    rel = root.find("reportingOwner/reportingOwnerRelationship")
    role = []
    if rel is not None:
        if _text(rel, "isDirector") in ("1", "true"):
            role.append("director")
        if _text(rel, "isOfficer") in ("1", "true"):
            role.append(_text(rel, "officerTitle") or "officer")
        if _text(rel, "isTenPercentOwner") in ("1", "true"):
            role.append("10% owner")
    plan_10b5_1 = _text(root, "aff10b5One") in ("1", "true")
    txs = []
    for t in root.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _text(t, "transactionCoding/transactionCode")
        try:
            shares = float(_text(t, "transactionAmounts/transactionShares/value") or 0)
            price = float(_text(t, "transactionAmounts/transactionPricePerShare/value") or 0)
        except ValueError:
            continue
        txs.append({
            "owner": "; ".join(owners), "role": ", ".join(role),
            "date": _text(t, "transactionDate/value")[:10],
            "code": code,
            "acq_disp": _text(t, "transactionAmounts/transactionAcquiredDisposedCode/value"),
            "shares": shares, "price": price, "value": shares * price,
            "plan_10b5_1": plan_10b5_1,
        })
    return txs


def insider_summary(txs: list[dict], today: date, days: int = 90) -> dict:
    """Zakupy (kod P) i sprzedaże (kod S) na rynku w oknie `days` dni wg daty transakcji."""
    since = today - timedelta(days=days)
    buys = [t for t in txs if t["code"] == "P" and t["date"] and date.fromisoformat(t["date"]) >= since]
    sells = [t for t in txs if t["code"] == "S" and t["date"] and date.fromisoformat(t["date"]) >= since]
    return {
        "buy_usd": sum(t["value"] for t in buys), "sell_usd": sum(t["value"] for t in sells),
        "buys": buys, "sells": sells,
    }


def efts_url(q: str, cik: int, forms: list[str], start: date, end: date) -> str:
    return EFTS_URL.format(q=quote(q), cik=cik, forms=quote(",".join(forms)),
                           start=start.isoformat(), end=end.isoformat())


def efts_hits(js: dict, cik: int) -> list[dict]:
    out = []
    for h in (js.get("hits") or {}).get("hits") or []:
        src = h.get("_source") or {}
        accn, _, fname = (h.get("_id") or "").partition(":")
        out.append({"form": src.get("form") or src.get("file_type") or "",
                    "date": src.get("file_date") or "",
                    "url": archive_url(cik, accn, fname) if accn else ""})
    return out
