"""Screening small-capów USA. Uruchomienie: python -m screener.run --help"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from . import checks, market, yahoo
from .net import Fetcher, FetchError
from .scoring import CAP_MAX, CAP_MIN, hard_filter_failures, score
from .tech import from_stooq_csv, from_yahoo_chart, technicals

# Wstępny filtr kapitalizacji z Nasdaq z marginesem; ostateczny: akcje ze sprawozdania x kurs.
CAP_BUFFER = 0.1
TOP_N = 10
SQUEEZE_N = 5


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def norm_ticker(sym: str) -> str:
    return sym.upper().replace("/", "-").replace(".", "-")


def iso(d) -> str:
    return d.isoformat() if hasattr(d, "isoformat") else str(d or "")


class Sources:
    """Rejestr pochodzenia: każda liczba trafia tu ze źródłem, URL-em i datą danych."""

    def __init__(self):
        self.rows: list[dict] = []

    def add(self, ticker, metric, value, source, url, as_of, retrieved="", note=""):
        if isinstance(value, float):
            value = round(value, 6)
        self.rows.append({"ticker": ticker, "metryka": metric, "wartosc": value, "zrodlo": source, "url": url,
                          "data_danych": iso(as_of), "pobrano": retrieved, "uwagi": note})


def fmt(x, pct=False, money=False, nd=2):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return ""
    if pct:
        return f"{x * 100:.1f}%"
    if money:
        return f"{x:,.0f}"
    return f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def load_prices(f: Fetcher, symbol: str):
    """Notowania dzienne: Yahoo, a gdy niedostępne - Stooq. Zwraca (DataFrame, Doc, nazwa źródła)."""
    try:
        doc = f.get(yahoo.CHART_URL.format(sym=yahoo.symbol(symbol)))
        return from_yahoo_chart(doc.data)[0], doc, "Yahoo Finance chart API"
    except (FetchError, ValueError, KeyError, TypeError) as yahoo_err:
        try:
            doc = f.get(market.STOOQ_URL.format(sym=yahoo.symbol(symbol).lower()), as_json=False)
            return from_stooq_csv(doc.data), doc, "Stooq"
        except (FetchError, ValueError, KeyError) as stooq_err:
            raise FetchError(f"Yahoo: {yahoo_err}; Stooq: {stooq_err}") from stooq_err


# ---------------------------------------------------------------- etapy

def price_stage(t: str, r: dict, f: Fetcher, bench, src: Sources) -> tuple[dict | None, str]:
    try:
        df, doc, label = load_prices(f, r["symbol"])
    except FetchError as e:
        return None, f"brak notowań: {e}"
    if len(df) < 120:
        return None, f"za krótka historia notowań ({len(df)} sesji)"
    tech = technicals(df, bench)
    m = {k: tech[k] for k in ("price", "high_52w", "dist_from_high", "sma50", "above_sma50", "sma50_turning_up",
                              "higher_low", "breakout_volume", "beta", "invalidation", "invalidation_basis")}
    m.update(avg_dollar_volume=tech["avg_dollar_volume_50d"], price_date=tech["date"], tech=tech,
             price_url=doc.url, price_src=label, market_cap=r["market_cap"])
    rows = [
        ("kurs", m["price"], ""), ("szczyt_52t", m["high_52w"], f"maksimum z 252 sesji, {tech['high_52w_date']}"),
        ("odleglosc_od_szczytu", m["dist_from_high"], ""), ("sma50", m["sma50"], ""),
        ("sredni_obrot_50d_usd", m["avg_dollar_volume"], "średnia z 50 sesji: kurs x wolumen"),
        ("beta", m["beta"], f"tygodniowe stopy zwrotu, {tech['beta_weeks']} tyg., benchmark SPY"),
        ("poziom_uniewaznienia", m["invalidation"], m["invalidation_basis"]),
    ]
    if tech["breakout"]:
        b = tech["breakout"]
        rows.append(("wybicie_3m", b["close"],
                     f"{b['date']}: zamknięcie > max 63 sesji ({b['level_3m']:.2f}), wolumen x{b['vol_ratio']:.1f}"))
    for name, val, note in rows:
        src.add(t, name, val, f"{label} (wyliczone)", doc.url, tech["date"], doc.retrieved_at, note)
    return m, ""


def fundamentals_stage(t: str, r: dict, m: dict, f: Fetcher, src: Sources, today: date) -> str:
    try:
        doc = f.get(yahoo.timeseries_url(r["symbol"], today))
    except FetchError as e:
        return f"brak danych finansowych: {e}"
    fund = yahoo.fundamentals(yahoo.parse_timeseries(doc.data))
    m.update(fund)
    m["fund_url"] = doc.url
    yl = "Yahoo Finance fundamentals-timeseries"
    for key, label, end_key, typ in (
            ("revenue_q", "przychody_kw", "revenue_q_end", "quarterlyTotalRevenue"),
            ("revenue_q_prev", "przychody_kw_rok_wczesniej", "revenue_q_prev_end", "quarterlyTotalRevenue"),
            ("fcf_ttm", "fcf_ttm", "fcf_end", "trailingFreeCashFlow"),
            ("fcf_ttm_prev", "fcf_ttm_rok_wczesniej", "fcf_ttm_prev_end", "trailingFreeCashFlow"),
            ("cash", "gotowka_i_inwestycje_kr", "bs_date", "quarterlyCashCashEquivalentsAndShortTermInvestments"),
            ("debt_gross", "dlug_z_leasingiem", "bs_date", "quarterlyTotalDebt"),
            ("leases", "leasing", "bs_date", "quarterlyCapitalLeaseObligations"),
            ("ebitda_ttm", "ebitda_ttm", "ebitda_end", "trailingEBITDA"),
            ("shares", "liczba_akcji", "shares_end", "quarterlyOrdinarySharesNumber"),
            ("net_income_ttm", "zysk_netto_ttm", "ni_end", "trailingNetIncome"),
            ("unusual_ttm", "pozycje_nadzwyczajne_ttm", "ni_end", "trailingTotalUnusualItems")):
        if m.get(key) is not None:
            src.add(t, label, m[key], f"{yl} ({typ})", doc.url, m.get(end_key, ""), doc.retrieved_at)
    for key, label, end_key in (("rev_growth", "wzrost_przychodow_rr", "revenue_q_end"),
                                ("net_debt_ebitda", "dlug_netto/ebitda", "bs_date"),
                                ("shares_chg_6m", "zmiana_liczby_akcji_6m", "shares_end")):
        if m.get(key) is not None:
            src.add(t, label, m[key], "wyliczone z danych Yahoo powyżej", doc.url, m.get(end_key, ""), doc.retrieved_at)

    if m.get("revenue_q") is None or m["revenue_q"] <= 0:
        return "spółka przed przychodami lub brak przychodów"
    if (today - m["revenue_q_end"]).days > 200:
        return f"nieaktualne dane: ostatni kwartał kończy się {m['revenue_q_end']}"
    if m.get("fcf_ttm") is None:
        return "F1: brak FCF TTM"
    if m["fcf_ttm"] <= 0:
        return f"F1: FCF TTM {fmt(m['fcf_ttm'], money=True)} USD <= 0"
    if m.get("rev_growth") is None or not m["rev_growth"] > 0:
        return f"F2: przychody kw. r/r {fmt(m.get('rev_growth'), pct=True) or 'brak danych'}"

    if m.get("shares"):
        m["_nasdaq_cap"] = m["market_cap"]
        m["market_cap"] = m["shares"] * m["price"]
        m["cap_src"] = "liczba akcji (Yahoo, sprawozdanie) x kurs"
        src.add(t, "kapitalizacja", m["market_cap"], m["cap_src"], doc.url, m["price_date"], doc.retrieved_at)
    else:
        m["cap_src"] = "Nasdaq screener"
        src.add(t, "kapitalizacja", m["market_cap"], m["cap_src"], r["_url"], m["price_date"])
    m["p_fcf"] = m["market_cap"] / m["fcf_ttm"]
    src.add(t, "p_fcf", m["p_fcf"], "kapitalizacja / FCF TTM", "", m["price_date"])
    if not CAP_MIN <= m["market_cap"] <= CAP_MAX:
        return f"kapitalizacja {fmt(m['market_cap'] / 1e6, money=True)} mln USD poza 300 mln - 3 mld"
    return ""


FINVIZ_FIELDS = {"Short Float": "short_float_finviz", "Short Ratio": "short_ratio_finviz", "Beta": "beta_finviz",
                 "P/FCF": "p_fcf_finviz", "Sales Q/Q": "rev_growth_finviz", "Market Cap": "cap_finviz",
                 "Shs Float": "float_finviz", "Insider Trans": "insider_trans_finviz"}


def details_stage(t: str, r: dict, m: dict, f: Fetcher, src: Sources, disc: list, today: date) -> None:
    sym, verify = r["symbol"], m.setdefault("verify", [])
    news: list[dict] = []
    try:
        fz = f.get(market.FINVIZ_QUOTE_URL.format(sym=market.finviz_symbol(sym)), as_json=False)
        snap = market.finviz_snapshot(fz.data)
        m["finviz_cat"] = market.finviz_categories(fz.data)
        news = market.finviz_news(fz.data, today)
        m["finviz_url"], m["earnings_finviz"], m["index_finviz"] = fz.url, snap.get("Earnings", ""), snap.get("Index", "")
        m["_finviz_date"] = fz.retrieved_at[:10]
        for label, key in FINVIZ_FIELDS.items():
            raw = snap.get(label)
            v = market.num(raw)
            if v is None:
                continue
            if raw.strip().endswith("%"):
                v /= 100
            m[key] = v
            src.add(t, key, v, f"Finviz ({label})", fz.url, fz.retrieved_at[:10], fz.retrieved_at,
                    "Finviz nie podaje daty rozliczenia short interest" if label.startswith("Short") else "")
        hi = (snap.get("52W High") or "").split()
        if len(hi) == 2 and market.num(hi[1]) is not None:
            m["dist_from_high_finviz"] = market.num(hi[1]) / 100
            src.add(t, "odleglosc_od_szczytu_finviz", m["dist_from_high_finviz"], "Finviz (52W High)", fz.url,
                    fz.retrieved_at[:10], fz.retrieved_at)
        if not snap:
            verify.append("Finviz: nie rozpoznano tabeli")
    except FetchError as e:
        verify.append(f"Finviz niedostępny: {e}")

    si = None
    try:
        sd = f.get(market.NASDAQ_SHORT_URL.format(sym=market.nasdaq_symbol(sym)))
        si = market.nasdaq_short_interest(sd.data)
        if si:
            for k, label in (("short_shares", "short_interest_akcje"), ("days_to_cover", "days_to_cover")):
                if si[k] is not None:
                    src.add(t, label, si[k], "Nasdaq short interest", sd.url, si["settlement_date"], sd.retrieved_at)
    except FetchError as e:
        verify.append(f"Nasdaq short interest niedostępny: {e}")
    if m.get("short_float_finviz") is not None:
        m["short_float"], m["short_float_src"] = m["short_float_finviz"], "Finviz"
    elif si and si["short_shares"] and m.get("float_finviz"):
        m["short_float"] = si["short_shares"] / m["float_finviz"]
        m["short_float_src"] = f"Nasdaq short interest / Finviz float, {si['settlement_date']}"
    if si and si["days_to_cover"] is not None:
        m["days_to_cover"], m["dtc_src"] = si["days_to_cover"], f"Nasdaq, rozliczenie {si['settlement_date']}"
        m["si_date"] = si["settlement_date"]
    elif m.get("short_ratio_finviz") is not None:
        m["days_to_cover"], m["dtc_src"] = m["short_ratio_finviz"], "Finviz Short Ratio"

    when, how, how_url = None, "", ""
    try:
        ed = f.get(market.NASDAQ_EARNINGS_URL.format(sym=market.nasdaq_symbol(sym)))
        got = market.nasdaq_earnings_date(ed.data)
        if got and got[0] >= today:
            when, how_url = got[0], ed.url
            how = "Nasdaq" + (" (szacunek Zacks, data niepotwierdzona)" if got[1] else " (data ogłoszona)")
    except FetchError:
        pass
    if when is None:
        d = market.finviz_earnings_date(m.get("earnings_finviz", ""), today)
        if d:
            when, how, how_url = d, "Finviz (Earnings)", m.get("finviz_url", "")
    m["catalysts"] = []
    if when and (when - today).days <= 183:
        est = "szacunek" in how
        m["next_earnings"], m["next_earnings_est"], m["next_earnings_src"] = when, est, how
        m["catalysts"].append(f"wyniki kwartalne {when.isoformat()}" + (" (data szacunkowa)" if est else ""))
        src.add(t, "data_wynikow", when.isoformat(), how, how_url, when)

    filings: list[dict] = []
    try:
        filings = market.nasdaq_filings(f.get(market.NASDAQ_FILINGS_URL.format(sym=market.nasdaq_symbol(sym))).data)
    except FetchError as e:
        verify.append(f"lista zgłoszeń Nasdaq niedostępna: {e}")
    m["filings_from"] = min((x["filed"] for x in filings), default=None)
    dil = checks.dilution(filings, m, news, today)
    m["dilution"], m["dilution_detail"] = bool(dil), dil
    m["atm"] = checks.has_atm(dil)
    for d in dil:
        src.add(t, "rozwodnienie", d["kind"], d["source"], d["url"], d["date"])

    flags = checks.red_flags(news, today)
    m["news_from"] = min((n["date"] for n in news), default=None)
    m["delisting"], m["going_concern"] = bool(flags["delisting"]), bool(flags["going_concern"])
    m["legal"] = bool(flags["legal"])
    m["legal_detail"] = [f"{n['date']}: {n['title']}" for n in flags["legal"]]
    for key in ("delisting", "going_concern", "legal"):
        for n in flags[key][:3]:
            src.add(t, key, n["title"], f"Finviz news ({n['source']})", n["url"], n["date"])
    for n in flags["verify"][:2]:
        verify.append(f"news {n['date']}: {n['title'][:90]}")

    try:
        url = market.NASDAQ_INSIDER_URL.format(sym=market.nasdaq_symbol(sym))
        ins = checks.insiders(market.nasdaq_insider_trades(f.get(url).data), today)
        m["insider_buy_usd"], m["insider_sell_usd"] = ins["buy_usd"], ins["sell_usd"]
        for tr in ins["buys"] + ins["sells"]:
            src.add(t, "insider_" + tr["type"].lower().replace(" ", "_"), tr["value"],
                    f"Nasdaq insider trades: {tr['insider']} ({tr['relation']}), {tr['shares']:,.0f} akcji po {tr['price']}",
                    url, tr["date"])
        src.add(t, "insider_zakupy_90d", ins["buy_usd"], "Nasdaq insider trades, suma Buy", url, today)
        src.add(t, "insider_sprzedaz_90d", ins["sell_usd"], "Nasdaq insider trades, suma Sell", url, today)
        if ins["buy_usd"] > 0:
            m["catalysts"].append(f"zakupy insiderów {ins['buy_usd']:,.0f} USD w 90 dni")
    except FetchError as e:
        verify.append(f"insiderzy Nasdaq niedostępni: {e}")

    # Porównanie z Finviz (screener). Rozbieżności zapisujemy; w punktacji liczą się dane ze sprawozdań.
    def cmp(metric, ours, theirs, tol, note=""):
        if ours is not None and theirs is not None and abs(ours - theirs) > tol(ours):
            disc.append({"ticker": t, "metryka": metric, "screener_finviz": round(theirs, 4),
                         "sprawozdanie_lub_wyliczenie": round(ours, 4), "uwagi": note})
    cmp("p_fcf", m.get("p_fcf"), m.get("p_fcf_finviz"), lambda a: 0.2 * abs(a), "Finviz liczy na inny dzień/definicję")
    cmp("wzrost_przychodow_rr", m.get("rev_growth"), m.get("rev_growth_finviz"), lambda a: 0.05)
    cmp("kapitalizacja", m.get("market_cap"), m.get("cap_finviz"), lambda a: 0.1 * abs(a))
    cmp("kapitalizacja", m.get("market_cap"), m.get("_nasdaq_cap"), lambda a: 0.1 * abs(a), "porównanie z Nasdaq screener")
    cmp("beta", m.get("beta"), m.get("beta_finviz"), lambda a: 0.3, "własna: 2 lata tygodniowo vs SPY")

    cat = m.get("finviz_cat") or {}
    ind = cat.get("industry", "")
    if ind == "Shell Companies":
        m["exclude"] = "SPAC (Finviz: Shell Companies)"
    elif ind.startswith("REIT"):
        m["exclude"] = f"REIT (Finviz: {ind})"
    elif cat.get("country") in ("China", "Hong Kong", "Macau"):
        m["exclude"] = f"spółka z {cat['country']} (Finviz)"


# ---------------------------------------------------------------- weryfikacja ręczna

OVERRIDE_BOOL = {"one_off", "dilution", "legal", "going_concern", "delisting", "net_cash", "atm"}


def load_overrides(path: Path) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    if path.exists():
        with path.open(newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                if r.get("ticker"):
                    out.setdefault(norm_ticker(r["ticker"]), []).append(r)
    return out


def apply_overrides(t: str, m: dict, rows: list[dict], src: Sources, disc: list[dict]) -> None:
    """Weryfikacja ręczna wygrywa z danymi automatycznymi; różnice trafiają do discrepancies.csv."""
    for r in rows:
        key, raw = r["metric"].strip(), r["value"].strip()
        note, url, as_of = r.get("note", ""), r.get("source_url", ""), r.get("as_of", "")
        if key == "catalyst":
            m.setdefault("catalysts", []).append(raw)
            src.add(t, "katalizator", raw, "weryfikacja ręczna", url, as_of, note=note)
            continue
        if key == "exclude":
            m["exclude"] = raw
            src.add(t, "wykluczenie", raw, "weryfikacja ręczna", url, as_of, note=note)
            continue
        val = raw.lower() in ("1", "true", "tak", "yes") if key in OVERRIDE_BOOL else market.num(raw)
        old = m.get(key)
        if old is not None and old != val:
            differs = True
            if isinstance(old, (int, float)) and isinstance(val, (int, float)) and not isinstance(old, bool):
                differs = abs(old - val) > 0.02 * max(abs(old), abs(val), 1e-9)
            if differs:
                disc.append({"ticker": t, "metryka": key, "screener_finviz": old, "sprawozdanie_lub_wyliczenie": val,
                             "uwagi": f"weryfikacja ręczna: {note} {url}".strip()})
        m[key] = val
        if key in ("atm", "dilution") and val:
            m["dilution"] = True
            kind = "program ATM (weryfikacja)" if key == "atm" else f"weryfikacja: {note.split(';')[0]}"
            m.setdefault("dilution_detail", []).append({"kind": kind, "date": as_of, "source": note, "url": url})
        src.add(t, key, val, "weryfikacja ręczna", url, as_of, note=note)


def self_check(m: dict) -> list[str]:
    """Kontrola końcowa TOP 10: nie blisko szczytu, beta > 1,2, brak aktywnego ATM."""
    out = []
    for key, label in (("dist_from_high", "Yahoo"), ("dist_from_high_finviz", "Finviz")):
        v = m.get(key)
        if v is not None and v > -0.25:
            out.append(f"za blisko szczytu ({label}: {v * 100:.1f}%)")
    for key, label in (("beta", "własna"), ("beta_finviz", "Finviz")):
        v = m.get(key)
        if v is not None and v <= 1.2:
            out.append(f"beta {label} {v:.2f} <= 1,2")
    if m.get("atm"):
        out.append("aktywny program ATM")
    return out


# ---------------------------------------------------------------- wynik

RESULT_FIELDS = ["ticker", "spolka", "sektor", "kapitalizacja_usd", "p_fcf", "wzrost_przychodow_rr", "beta",
                 "odleglosc_od_szczytu", "short_float", "days_to_cover", "pkt_fundamenty", "pkt_technika",
                 "pkt_squeeze", "pkt_katalizator", "kary", "wynik", "opis_kar", "dyskwalifikacja", "top10",
                 "data_kursu", "okres_sprawozdania", "zrodla"]


def result_row(t, base, m, s, in_top) -> dict:
    sector = (m.get("finviz_cat") or {}).get("sector") or base["sector"]
    return {
        "ticker": t, "spolka": base["name"], "sektor": sector,
        "kapitalizacja_usd": fmt(m.get("market_cap"), money=True), "p_fcf": fmt(m.get("p_fcf"), nd=1),
        "wzrost_przychodow_rr": fmt(m.get("rev_growth"), pct=True), "beta": fmt(m.get("beta")),
        "odleglosc_od_szczytu": fmt(m.get("dist_from_high_cons", m.get("dist_from_high")), pct=True), "short_float": fmt(m.get("short_float"), pct=True),
        "days_to_cover": fmt(m.get("days_to_cover"), nd=1), "pkt_fundamenty": s["pkt_fundamenty"],
        "pkt_technika": s["pkt_technika"], "pkt_squeeze": s["pkt_squeeze"], "pkt_katalizator": s["pkt_katalizator"],
        "kary": s["pkt_kary"], "wynik": s["wynik"], "opis_kar": " | ".join(f"{n} ({p})" for n, p in s["kary"]),
        "dyskwalifikacja": s["dyskwalifikacja"], "top10": "tak" if in_top else "",
        "data_kursu": m.get("price_date", ""),
        "okres_sprawozdania": f"przychody kw. do {iso(m.get('revenue_q_end'))}; FCF TTM do {iso(m.get('fcf_end'))}",
        "zrodla": f"sources.csv (ticker={t}); kurs: {m.get('price_src', '')}; finanse: Yahoo; short: "
                  f"{m.get('short_float_src', '')} / {m.get('dtc_src', '')}",
    }


def write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main(argv: list[str] | None = None, fetcher: Fetcher | None = None) -> int:
    ap = argparse.ArgumentParser(description="Screening small-capów USA z potencjałem wzrostu (1-3 mies.)")
    ap.add_argument("--out", default="output")
    ap.add_argument("--cache", default="data/cache")
    ap.add_argument("--overrides", default="overrides.csv")
    ap.add_argument("--notes", default="analysis/notes.json", help="teza/ryzyko/katalizator dla TOP 10")
    ap.add_argument("--limit", type=int, default=0, help="tylko N pierwszych spółek (test)")
    ap.add_argument("--tickers", default="", help="lista tickerów po przecinku zamiast pełnego uniwersum")
    ap.add_argument("--offline", action="store_true", help="tylko dane z cache")
    ap.add_argument("--max-age-h", type=float, default=20.0)
    ap.add_argument("--as-of", default="", help="data screeningu RRRR-MM-DD (domyślnie dziś)")
    args = ap.parse_args(argv)

    today = date.fromisoformat(args.as_of) if args.as_of else date.today()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    f = fetcher or Fetcher(Path(args.cache), None, args.max_age_h, args.offline)
    src, disc, excluded, late = Sources(), [], [], []
    funnel: Counter = Counter()
    overrides = load_overrides(Path(args.overrides))

    def drop(t, name, stage, reason, url=""):
        excluded.append({"ticker": t, "spolka": name, "etap": stage, "powod": reason, "zrodlo": url})

    # 1. Uniwersum (Nasdaq screener)
    rows, universe_date = [], ""
    for ex, label in market.EXCHANGES.items():
        doc = f.get(market.NASDAQ_SCREENER_URL.format(ex=ex))
        universe_date = doc.retrieved_at
        for r in market.screener_rows(doc.data, label):
            r["_url"] = doc.url
            rows.append(r)
    only = {norm_ticker(x) for x in args.tickers.split(",") if x.strip()}
    funnel["1. spółki notowane na NYSE, Nasdaq, NYSE American (Nasdaq screener)"] = len(rows)
    stage1 = []
    for r in rows:
        t = norm_ticker(r["symbol"])
        if only and t not in only:
            continue
        cap = r["market_cap"]
        if not cap or not CAP_MIN * (1 - CAP_BUFFER) <= cap <= CAP_MAX * (1 + CAP_BUFFER):
            continue
        reason = market.universe_exclusion(r)
        if reason:
            drop(t, r["name"], "uniwersum", reason, r["_url"])
            continue
        r["ticker"] = t
        stage1.append(r)
    if args.limit:
        stage1 = stage1[:args.limit]
    funnel["2. kapitalizacja ok. 300 mln - 3 mld USD, akcje zwykłe, bez SPAC/REIT/Chin"] = len(stage1)
    log(f"uniwersum: {len(stage1)} spółek")

    # 2. Kurs i technika (Yahoo)
    bench, _, _ = load_prices(f, "SPY")
    stage2 = []
    for i, r in enumerate(stage1, 1):
        if i % 100 == 0:
            log(f"  notowania {i}/{len(stage1)}, przeszło {len(stage2)}")
        t = r["ticker"]
        m, err = price_stage(t, r, f, bench, src)
        if m is None:
            drop(t, r["name"], "kurs", err)
            continue
        # Finanse i ostateczną kapitalizację (akcje ze sprawozdania x kurs) sprawdzamy w etapie 3.
        fails = hard_filter_failures({**m, "fcf_ttm": 1, "rev_growth": 1, "market_cap": CAP_MIN})
        if fails:
            drop(t, r["name"], "kurs/technika", "; ".join(fails), m["price_url"])
            continue
        r["m"] = m
        stage2.append(r)
    funnel["3. obrót > 2 mln USD, F3 beta > 1,2, F4 >= 25% pod szczytem, F5 kurs nad SMA50"] = len(stage2)
    log(f"po technice: {len(stage2)}")

    # 3. Dane finansowe (Yahoo)
    stage3 = []
    for r in stage2:
        t, m = r["ticker"], r["m"]
        err = fundamentals_stage(t, r, m, f, src, today)
        if err:
            drop(t, r["name"], "finanse", err, m.get("fund_url", ""))
            continue
        stage3.append(r)
    funnel["4. F1 FCF TTM > 0, F2 przychody kw. r/r rosną, kapitalizacja 300 mln - 3 mld (akcje x kurs)"] = len(stage3)
    log(f"po finansach: {len(stage3)}")

    # 4. Short interest, katalizatory, rozwodnienie, ostrzeżenia, insiderzy (Finviz, Nasdaq)
    scored = []
    for r in stage3:
        t, m = r["ticker"], r["m"]
        log(f"  szczegóły: {t}")
        details_stage(t, r, m, f, src, disc, today)
        apply_overrides(t, m, overrides.get(t, []), src, disc)
        # Do prezentacji bierzemy ostrożniejszą (bliższą szczytu) z odległości wg Yahoo i Finviz.
        m["dist_from_high_cons"] = max(v for v in (m.get("dist_from_high"), m.get("dist_from_high_finviz")) if v is not None)
        fails = hard_filter_failures(m)
        if fails:
            drop(t, r["name"], "kontrola końcowa", "; ".join(fails), m.get("finviz_url", ""))
            late.append({"ticker": t, "spolka": r["name"], "powod": "; ".join(fails)})
            continue
        scored.append((t, r, m, score(m)))
    funnel["5. F6 (delisting, going concern), kontrola krzyżowa z Finviz, weryfikacja ręczna"] = len(scored)

    ranked = sorted(scored, key=lambda x: (x[3]["wynik"], x[3]["pkt_fundamenty"], -(x[2].get("p_fcf") or 99)),
                    reverse=True)
    top = []
    for t, r, m, s in ranked:
        if len(top) >= TOP_N:
            break
        reasons = self_check(m)
        if s["dyskwalifikacja"]:
            kinds = "; ".join(sorted({d["kind"] for d in m.get("dilution_detail", [])}))
            reasons.append(f"{s['dyskwalifikacja']} ({kinds})" if kinds else s["dyskwalifikacja"])
        if reasons:
            late.append({"ticker": t, "spolka": r["name"], "powod": "; ".join(reasons), "wynik": s["wynik"]})
            continue
        top.append((t, r, m, s))
    top_set = {x[0] for x in top}
    squeeze = [x for x in ranked if not x[3]["dyskwalifikacja"] and not x[2].get("dilution") and x[3]["pkt_squeeze"] > 0]
    squeeze.sort(key=lambda x: (x[3]["pkt_squeeze"], x[2].get("short_float") or 0, x[2].get("days_to_cover") or 0),
                 reverse=True)

    write_csv(out / "results.csv", [result_row(t, r, m, s, t in top_set) for t, r, m, s in ranked], RESULT_FIELDS)
    write_csv(out / "sources.csv", src.rows, ["ticker", "metryka", "wartosc", "zrodlo", "url", "data_danych", "pobrano", "uwagi"])
    write_csv(out / "excluded.csv", excluded, ["ticker", "spolka", "etap", "powod", "zrodlo"])
    write_csv(out / "discrepancies.csv", disc, ["ticker", "metryka", "screener_finviz", "sprawozdanie_lub_wyliczenie", "uwagi"])
    from .report import write_report

    notes = json.loads(Path(args.notes).read_text(encoding="utf-8")) if Path(args.notes).exists() else {}
    write_report(out / "raport.md", today=today, universe_date=universe_date, top=top, squeeze=squeeze[:SQUEEZE_N],
                 late=late, funnel=funnel, excluded=excluded, disc=disc, src=src, notes=notes, n_scored=len(ranked))
    (out / "funnel.json").write_text(json.dumps(funnel, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"gotowe: {len(ranked)} spółek po filtrach, TOP {len(top)} w {out / 'raport.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
