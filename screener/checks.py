"""Reguły kar i filtrów jakościowych na podstawie listy zgłoszeń (Nasdaq), liczby akcji (Yahoo),
transakcji insiderów (Nasdaq) i nagłówków wiadomości (Finviz)."""
from __future__ import annotations

import re
from datetime import date, timedelta

SHELF_FORMS = {"S-3", "S-3ASR", "S-3/A", "F-3", "F-3ASR", "F-3/A"}
PROSPECTUS_PREFIX = ("424B1", "424B2", "424B3", "424B4", "424B5", "424B7")
RESALE_FORMS = {"S-1", "S-1/A", "S-1A", "F-1", "F-1/A"}

# Wzrost liczby akcji w 6 miesięcy powyżej tego progu traktujemy jako ślad emisji
# (samo wynagrodzenie w akcjach rzadko przekracza 2-3% rocznie).
SHARES_6M_THRESHOLD = 0.04

_OFFERING = re.compile(r"at[- ]the[- ]market|\bATM\b|public offering|registered direct|private placement|"
                       r"proposed offering|prices? (?:\$[\d.,]+ (?:million|billion) )?(?:underwritten )?offering|"
                       r"equity offering|offering of (?:common|shares)|shelf registration", re.I)
_DEBT_ONLY = re.compile(r"senior (?:secured |unsecured )?notes|notes due|term loan|credit facility|debt offering", re.I)
_ATM = re.compile(r"at[- ]the[- ]market|\bATM\b", re.I)
_DELIST = re.compile(r"delist|deficiency|non-?compliance|minimum bid|continued listing|notice from (?:the )?(?:nasdaq|nyse)", re.I)
_REGAIN = re.compile(r"regain(?:s|ed)? compliance", re.I)
_GOING_CONCERN = re.compile(r"going concern", re.I)
_CLASS_ACTION = re.compile(r"class action", re.I)
_FILED = re.compile(r"lawsuit|filed|deadline|lead plaintiff|sued", re.I)
# Ogólne "short sellers" pomijamy: kanał Finviz zawiera artykuły o innych spółkach.
_SHORT_SELLERS = re.compile(r"short[- ]seller report|short report|Hindenburg|Muddy Waters|Culper|Spruce Point|Grizzly|"
                            r"Wolfpack|Fuzzy Panda|Kerrisdale|Blue Orca|Bleecker Street|Citron|Viceroy|Iceberg Research|"
                            r"Morpheus|Night Market|Scorpion Capital|J Capital|Gotham City", re.I)
_SEC_PROBE = re.compile(r"Wells notice|\bSEC\b.{0,40}(?:subpoena|investigation|probe|charges|inquiry)|"
                        r"(?:subpoena|investigation|probe|inquiry).{0,40}\bSEC\b", re.I)
_LAW_FIRM_PROBE = re.compile(r"investigat", re.I)


def _recent(items: list[dict], key: str, today: date, days: int) -> list[dict]:
    return [x for x in items if (today - x[key]).days <= days]


def dilution(filings: list[dict], fund: dict, news: list[dict], today: date) -> list[dict]:
    """Ślady rozwodnienia: shelf S-3/F-3, prospekty 424B i S-1 z 6 miesięcy (lista Nasdaq sięga ok. 6 mies.),
    wzrost liczby akcji o > 4% w 6 mies., wiadomości o emisji lub programie ATM."""
    out = []
    for f in filings:
        age = (today - f["filed"]).days
        if f["form"] in SHELF_FORMS and age <= 3 * 365:
            out.append({"kind": f"shelf {f['form']}", "date": f["filed"], "source": "Nasdaq: lista zgłoszeń SEC", "url": f["url"]})
        elif f["form"].startswith(PROSPECTUS_PREFIX) and age <= 183:
            out.append({"kind": f"prospekt {f['form']}", "date": f["filed"], "source": "Nasdaq: lista zgłoszeń SEC", "url": f["url"]})
        elif f["form"] in RESALE_FORMS and age <= 183:
            out.append({"kind": f"rejestracja {f['form']}", "date": f["filed"], "source": "Nasdaq: lista zgłoszeń SEC", "url": f["url"]})
    chg = fund.get("shares_chg_6m")
    if chg is not None and chg > SHARES_6M_THRESHOLD:
        out.append({"kind": f"liczba akcji +{chg * 100:.1f}% w 6 mies.", "date": fund.get("shares_end"),
                    "source": "Yahoo: quarterlyOrdinarySharesNumber", "url": ""})
    for n in _recent(news, "date", today, 183):
        t = n["title"]
        if _OFFERING.search(t) and not (_DEBT_ONLY.search(t) and "convertible" not in t.lower()):
            kind = "program ATM (wiadomość)" if _ATM.search(t) else "emisja (wiadomość)"
            out.append({"kind": kind, "date": n["date"], "source": f"Finviz news: {t}", "url": n["url"]})
    return out


def has_atm(events: list[dict]) -> bool:
    return any("ATM" in e["kind"] for e in events)


def red_flags(news: list[dict], today: date) -> dict:
    """Ostrzeżenia z nagłówków z 12 miesięcy: delisting, going concern, sprawy prawne, raporty short sellerów."""
    year = sorted(_recent(news, "date", today, 365), key=lambda n: n["date"])
    out = {"delisting": [], "going_concern": [], "legal": [], "verify": []}
    for n in year:
        t = n["title"]
        if _DELIST.search(t) and not _REGAIN.search(t):
            out["delisting"].append(n)
        if _REGAIN.search(t):
            out["delisting"] = []  # późniejsze odzyskanie zgodności znosi wcześniejsze zawiadomienie
        if _GOING_CONCERN.search(t):
            out["going_concern"].append(n)
        if (_CLASS_ACTION.search(t) and _FILED.search(t)) or _SHORT_SELLERS.search(t) or _SEC_PROBE.search(t):
            out["legal"].append(n)
        elif _LAW_FIRM_PROBE.search(t) and re.search(r"investor|shareholder|securities", t, re.I):
            out["verify"].append(n)
    return out


def insiders(trades: list[dict], today: date, days: int = 90) -> dict:
    """Zakupy i sprzedaże na rynku (Buy / Sell, Automatic Sell) w oknie `days` dni."""
    since = today - timedelta(days=days)
    win = [t for t in trades if t["date"] >= since]
    buys = [t for t in win if t["type"].lower() == "buy"]
    sells = [t for t in win if t["type"].lower() in ("sell", "automatic sell")]
    return {"buy_usd": sum(t["value"] for t in buys), "sell_usd": sum(t["value"] for t in sells),
            "buys": buys, "sells": sells}
