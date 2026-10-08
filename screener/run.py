"""Screening small-capów USA. Uruchomienie: python -m screener.run --help"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from . import edgar, market, xbrl
from .net import Doc, Fetcher, FetchError, NotFound
from .scoring import CAP_MAX, CAP_MIN, hard_filter_failures, score
from .tech import from_stooq_csv, from_yahoo_chart, technicals

# Margines przy wstępnym filtrze kapitalizacji z Nasdaq; ostateczny filtr liczymy
# z liczby akcji z raportu SEC x kurs.
CAP_BUFFER = 0.2
MAX_FORM4_PER_COMPANY = 60


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def norm_ticker(sym: str) -> str:
    return sym.upper().replace("/", "-").replace(".", "-")


class Sources:
    """Rejestr pochodzenia: każda liczba trafia tu ze źródłem i datą."""

    def __init__(self):
        self.rows: list[dict] = []

    def add(self, ticker, metric, value, source, url, as_of, retrieved="", note=""):
        if isinstance(value, float):
            value = round(value, 6)
        self.rows.append({"ticker": ticker, "metryka": metric, "wartosc": value, "zrodlo": source,
                          "url": url, "data_danych": str(as_of), "pobrano": retrieved, "uwagi": note})


def xbrl_src(v: xbrl.Value) -> str:
    forms = sorted({f"{p.form} złożony {p.filed.isoformat()}" for p in v.parts})
    tags = sorted({p.tag for p in v.parts})
    return f"SEC XBRL ({', '.join(tags)}; {'; '.join(forms)})"


def xbrl_url(cik: int, v: xbrl.Value) -> str:
    latest = max(v.parts, key=lambda p: p.filed)
    return edgar.archive_url(cik, latest.accn)


def load_prices(f: Fetcher, symbol: str):
    """Notowania dzienne: Yahoo, a gdy niedostępne - Stooq. Zwraca (DataFrame, Doc, nazwa źródła)."""
    try:
        doc = f.get(market.YAHOO_CHART_URL.format(sym=market.yahoo_symbol(symbol)))
        return from_yahoo_chart(doc.data)[0], doc, "Yahoo Finance chart API"
    except (FetchError, ValueError, KeyError) as yahoo_err:
        try:
            doc = f.get(market.STOOQ_URL.format(sym=market.yahoo_symbol(symbol).lower()), as_json=False)
            return from_stooq_csv(doc.data), doc, "Stooq"
        except (FetchError, ValueError, KeyError) as stooq_err:
            raise FetchError(f"Yahoo: {yahoo_err}; Stooq: {stooq_err}") from stooq_err


# ---------------------------------------------------------------- etap 2: fundamenty

def fundamentals(t: str, cik: int, slim: dict, cf_doc: Doc, src: Sources, today: date) -> tuple[dict, list[str]]:
    m: dict = {}
    notes: list[str] = []
    rq = xbrl.revenue_quarters(slim)
    pair = xbrl.yoy(rq)
    if pair is None:
        return m, ["brak porównywalnych przychodów kwartalnych w XBRL"]
    cur, prev = pair
    m["revenue_q"], m["revenue_q_prev"], m["revenue_q_end"] = cur.val, prev.val, cur.end
    m["rev_growth"] = cur.val / prev.val - 1 if prev.val > 0 else float("nan")
    src.add(t, "przychody_kw", cur.val, xbrl_src(cur), xbrl_url(cik, cur), cur.end, cf_doc.retrieved_at)
    src.add(t, "przychody_kw_rok_wczesniej", prev.val, xbrl_src(prev), xbrl_url(cik, prev), prev.end, cf_doc.retrieved_at)
    src.add(t, "wzrost_przychodow_rr", m["rev_growth"], "wyliczone z dwóch powyższych", "", cur.end)
    if cur.val <= 0:
        notes.append("spółka przed przychodami")
    if (today - cur.end).days > 200:
        notes.append(f"nieaktualne dane: ostatni kwartał kończy się {cur.end}")

    fcf_pts, capex_found = xbrl.fcf_ttm(slim)
    if not fcf_pts:
        return m, notes + ["brak CFO/capex w XBRL"]
    fcf_end = max(fcf_pts)
    fcf = fcf_pts[fcf_end]
    m["fcf_ttm"], m["fcf_end"] = fcf.val, fcf_end
    src.add(t, "fcf_ttm", fcf.val, xbrl_src(fcf) + " CFO - capex, TTM", xbrl_url(cik, fcf), fcf_end, cf_doc.retrieved_at,
            "" if capex_found else "brak capex w XBRL: FCF = CFO, do weryfikacji")
    if not capex_found:
        m.setdefault("verify", []).append("brak capex w XBRL (FCF = CFO)")
    if fcf_end < cur.end:
        m.setdefault("verify", []).append(f"FCF TTM na {fcf_end}, przychody na {cur.end}")
    fpair = xbrl.yoy(fcf_pts, tol=15)
    if fpair:
        m["fcf_ttm_prev"] = fpair[1].val
        src.add(t, "fcf_ttm_rok_wczesniej", fpair[1].val, xbrl_src(fpair[1]), xbrl_url(cik, fpair[1]),
                fpair[1].end, cf_doc.retrieved_at)

    cd = xbrl.cash_and_debt(slim)
    if cd:
        m["cash"], m["debt"], m["bs_date"] = cd["cash"], cd["debt"], cd["date"]
        for name, parts in (("gotowka", cd["cash_parts"]), ("dlug", cd["debt_parts"])):
            val = sum(p.val for p in parts)
            desc = "; ".join(f"{p.tag} {p.form} złożony {p.filed}" for p in parts) or "brak tagów długu"
            src.add(t, name, val, f"SEC XBRL ({desc})",
                    edgar.archive_url(cik, parts[0].accn) if parts else cf_doc.url, cd["date"], cf_doc.retrieved_at)
        if not cd["debt_found"]:
            m.setdefault("verify", []).append("brak tagów długu w XBRL (przyjęto 0)")
        net_debt = cd["debt"] - cd["cash"]
        m["net_debt"] = net_debt
        m["net_cash"] = net_debt < 0
        eb = xbrl.ebitda_ttm(slim, fcf_end)
        if eb is not None:
            m["ebitda_ttm"] = eb.val
            src.add(t, "ebitda_ttm", eb.val, xbrl_src(eb) + " EBIT + D&A, TTM", xbrl_url(cik, eb), eb.end,
                    cf_doc.retrieved_at)
            if eb.val > 0:
                m["net_debt_ebitda"] = net_debt / eb.val
                src.add(t, "dlug_netto/ebitda", m["net_debt_ebitda"], "wyliczone", "", cd["date"])

    sh = xbrl.shares_outstanding(slim)
    if sh:
        m["shares"] = sh.val
        src.add(t, "liczba_akcji", sh.val, f"SEC XBRL ({sh.tag}; {sh.form} złożony {sh.filed})",
                edgar.archive_url(cik, sh.accn), sh.end, cf_doc.retrieved_at)

    ni = xbrl.ttm_at(slim, xbrl.NET_INCOME_TAGS, fcf_end)
    oo = xbrl.one_offs_ttm(slim, fcf_end)
    m["one_off_detail"] = []
    noncash = sum(v.val for _, v in oo["noncash"])
    cash = sum(v.val for _, v in oo["cash"])
    for tag, v in oo["noncash"] + oo["cash"]:
        m["one_off_detail"].append(f"{tag}={v.val:,.0f}")
        src.add(t, f"jednorazowka:{tag}", v.val, xbrl_src(v), xbrl_url(cik, v), v.end, cf_doc.retrieved_at)
    if ni is not None:
        m["net_income_ttm"] = ni.val
        src.add(t, "zysk_netto_ttm", ni.val, xbrl_src(ni), xbrl_url(cik, ni), ni.end, cf_doc.retrieved_at)
    if ni is not None and ni.val > 0 and (noncash + cash) > 0.25 * ni.val:
        m["one_off"] = True
    m["fcf_ex_one_off"] = fcf.val - cash
    if cash > 0:
        src.add(t, "fcf_bez_jednorazowek", m["fcf_ex_one_off"], "FCF TTM - gotówkowe pozycje jednorazowe", "", fcf_end)
        if cash > 0.25 * abs(fcf.val):
            m["one_off"] = True
    return m, notes


# ---------------------------------------------------------------- etap 4: EDGAR i rynek

def edgar_checks(t: str, cik: int, rows: list[dict], sub_doc: Doc, f: Fetcher, src: Sources,
                 m: dict, today: date) -> None:
    dil = edgar.dilution_events(rows, cik, today)
    try:
        atm = edgar.efts_hits(f.get(edgar.efts_url('"at-the-market"', cik, ["8-K", "424B5", "S-3", "10-Q", "10-K"],
                                                   today - timedelta(days=3 * 365), today)).data, cik)
        if atm:
            dil.append({"kind": "wzmianka o programie ATM", "form": atm[0]["form"], "date": atm[0]["date"],
                        "url": atm[0]["url"]})
    except FetchError as e:
        m.setdefault("verify", []).append(f"EFTS ATM niedostępne: {e}")
    m["dilution"] = bool(dil)
    m["dilution_detail"] = dil
    for d in dil:
        src.add(t, "rozwodnienie", d["kind"], f"SEC EDGAR {d['form']}", d["url"], d["date"], sub_doc.retrieved_at)

    dl = edgar.delisting_events(rows, cik, today)
    m["delisting"] = bool(dl)
    for d in dl:
        src.add(t, "delisting_8k_3.01", d["form"], "SEC EDGAR 8-K pozycja 3.01", d["url"], d["date"],
                sub_doc.retrieved_at, "3.01 bywa też przeniesieniem notowań: sprawdzić treść")
        m.setdefault("verify", []).append(f"8-K 3.01 z {d['date']}")

    lp = edgar.latest_periodic(rows)
    if lp:
        m["latest_report"] = f"{lp['form']} za {lp['reportDate']} złożony {lp['filingDate']}"
        m["latest_report_url"] = edgar.archive_url(cik, lp["accessionNumber"], lp["primaryDocument"])
        d0 = lp["filingDate"]
        try:
            gc = edgar.efts_hits(f.get(edgar.efts_url('"substantial doubt"', cik, ["10-Q", "10-K"], d0, d0)).data, cik)
            gc2 = edgar.efts_hits(f.get(edgar.efts_url('"going concern"', cik, ["10-Q", "10-K"], d0, d0)).data, cik)
            # Wymagamy obu fraz w tym samym ostatnim raporcie.
            urls = {h["url"] for h in gc} & {h["url"] for h in gc2}
            m["going_concern"] = bool(urls)
            for u in urls:
                src.add(t, "going_concern", "wzmianka", "SEC EDGAR full-text search", u, d0, "",
                        "fraza może być boilerplate: sprawdzić treść")
                m.setdefault("verify", []).append("going concern w ostatnim raporcie")
        except FetchError as e:
            m.setdefault("verify", []).append(f"EFTS going concern niedostępne: {e}")

    legal = []
    for q in ('"securities class action"', '"Wells notice"', '"Division of Enforcement"'):
        try:
            hits = edgar.efts_hits(f.get(edgar.efts_url(q, cik, ["10-Q", "10-K", "8-K"],
                                                        today - timedelta(days=365), today)).data, cik)
        except FetchError as e:
            m.setdefault("verify", []).append(f"EFTS {q} niedostępne: {e}")
            continue
        for h in hits[:3]:
            legal.append(f"{q} w {h['form']} {h['date']}")
            src.add(t, "prawne", q, "SEC EDGAR full-text search", h["url"], h["date"], "",
                    "sprawdzić, czy dotyczy spółki jako pozwanej")
    m["legal"] = bool(legal)
    m["legal_detail"] = legal

    txs = []
    for doc in edgar.form4_docs(rows, cik, today)[:MAX_FORM4_PER_COMPANY]:
        try:
            x = f.get(doc["url"], as_json=False)
            for tx in edgar.parse_form4(x.data):
                tx["url"] = doc["index_url"]
                txs.append(tx)
        except (FetchError, ValueError, SyntaxError) as e:  # ParseError z XML dziedziczy po SyntaxError
            m.setdefault("verify", []).append(f"Form 4 {doc['index_url']}: {e}")
    ins = edgar.insider_summary(txs, today)
    m["insider_buy_usd"], m["insider_sell_usd"] = ins["buy_usd"], ins["sell_usd"]
    for tx in ins["buys"] + ins["sells"]:
        src.add(t, "insider_" + ("zakup" if tx["code"] == "P" else "sprzedaz"), tx["value"],
                f"SEC Form 4: {tx['owner']} ({tx['role']}), {tx['shares']:,.0f} akcji po {tx['price']}"
                + (", plan 10b5-1" if tx["plan_10b5_1"] else ""), tx["url"], tx["date"])
    src.add(t, "insider_zakupy_90d", ins["buy_usd"], "suma Form 4 kod P", "", today)
    src.add(t, "insider_sprzedaz_90d", ins["sell_usd"], "suma Form 4 kod S", "", today)


def market_checks(t: str, sym: str, f: Fetcher, src: Sources, m: dict, today: date) -> None:
    fv = {}
    try:
        fz = f.get(market.FINVIZ_QUOTE_URL.format(sym=market.yahoo_symbol(sym)), as_json=False)
        fv = market.finviz_snapshot(fz.data)
        if not fv:
            m.setdefault("verify", []).append("Finviz: nie rozpoznano tabeli")
        for label, key in (("Short Float", "short_float_finviz"), ("Short Ratio", "short_ratio_finviz"),
                           ("Beta", "beta_finviz"), ("Shs Float", "float_finviz"), ("Market Cap", "cap_finviz")):
            v = market.num(fv.get(label))
            if v is not None:
                if label == "Short Float":
                    v /= 100
                m[key] = v
                src.add(t, key, v, f"Finviz ({label})", fz.url, fz.retrieved_at[:10], fz.retrieved_at,
                        "Finviz nie podaje daty rozliczenia short interest" if label.startswith("Short") else "")
    except FetchError as e:
        m.setdefault("verify", []).append(f"Finviz niedostępny: {e}")

    si = None
    try:
        sd = f.get(market.NASDAQ_SHORT_URL.format(sym=sym))
        si = market.nasdaq_short_interest(sd.data)
        if si:
            for k in ("short_shares", "days_to_cover"):
                if si[k] is not None:
                    src.add(t, f"{k}_nasdaq", si[k], "Nasdaq short interest", sd.url, si["settlement_date"],
                            sd.retrieved_at)
    except FetchError as e:
        m.setdefault("verify", []).append(f"Nasdaq short interest niedostępny: {e}")

    # Short float: Finviz (short / free float). Fallback: short z Nasdaq / liczba akcji z SEC
    # (zaniża wynik, bo free float < liczba akcji).
    if m.get("short_float_finviz") is not None:
        m["short_float"], m["short_float_src"] = m["short_float_finviz"], "Finviz"
    elif si and si["short_shares"] and m.get("shares"):
        m["short_float"] = si["short_shares"] / m["shares"]
        m["short_float_src"] = "Nasdaq / akcje SEC (% akcji, nie free float)"
        src.add(t, "short_float", m["short_float"], m["short_float_src"], "", si["settlement_date"])
    if si and si["days_to_cover"] is not None:
        m["days_to_cover"], m["dtc_src"] = si["days_to_cover"], f"Nasdaq, rozliczenie {si['settlement_date']}"
    elif m.get("short_ratio_finviz") is not None:
        m["days_to_cover"], m["dtc_src"] = m["short_ratio_finviz"], "Finviz Short Ratio"

    # Najbliższe wyniki.
    when, how = None, ""
    try:
        ed = f.get(market.NASDAQ_EARNINGS_URL.format(sym=sym))
        d = market.nasdaq_earnings_date(ed.data)
        if d and d >= today:
            when, how = d, f"Nasdaq ({ed.url})"
    except FetchError:
        pass
    if when is None:
        d = market.finviz_earnings_date(fv.get("Earnings", ""), today)
        if d:
            when, how = d, "Finviz (Earnings)"
    m["catalysts"] = m.get("catalysts", [])
    if when and (when - today).days <= 183:
        m["catalysts"].append(f"wyniki {when.isoformat()}")
        src.add(t, "katalizator_wyniki", when.isoformat(), how, "", when)
    if m.get("insider_buy_usd", 0) > 0:
        m["catalysts"].append(f"zakupy insiderów {m['insider_buy_usd']:,.0f} USD / 90 dni")


# ---------------------------------------------------------------- weryfikacja ręczna

OVERRIDE_BOOL = {"one_off", "dilution", "legal", "going_concern", "delisting", "net_cash"}


def load_overrides(path: Path) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    if not path.exists():
        return out
    with path.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r.get("ticker"):
                out.setdefault(norm_ticker(r["ticker"]), []).append(r)
    return out


def apply_overrides(t: str, m: dict, rows: list[dict], src: Sources, disc: list[dict]) -> None:
    """Dane z raportu (wpisane po weryfikacji) wygrywają ze screenerem; różnice idą do discrepancies.csv."""
    for r in rows:
        key, raw = r["metric"].strip(), r["value"].strip()
        if key == "catalyst":
            m.setdefault("catalysts", []).append(raw)
            src.add(t, "katalizator", raw, "weryfikacja ręczna", r.get("source_url", ""), r.get("as_of", ""),
                    note=r.get("note", ""))
            continue
        val = raw.lower() in ("1", "true", "tak", "yes") if key in OVERRIDE_BOOL else market.num(raw)
        old = m.get(key)
        if old is not None and old != val:
            differs = True
            if isinstance(old, (int, float)) and isinstance(val, (int, float)) and not isinstance(old, bool):
                differs = abs(old - val) > 0.02 * max(abs(old), abs(val), 1e-9)
            if differs:
                disc.append({"ticker": t, "metryka": key, "screener": old, "raport": val,
                             "zrodlo_raportu": r.get("source_url", ""), "data": r.get("as_of", ""),
                             "uwagi": r.get("note", "")})
        m[key] = val
        src.add(t, key, val, "weryfikacja ręczna (raport)", r.get("source_url", ""), r.get("as_of", ""),
                note=r.get("note", ""))


# ---------------------------------------------------------------- wynik

def fmt(x, pct=False, money=False, nd=2):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return ""
    if pct:
        return f"{x * 100:.1f}%"
    if money:
        return f"{x:,.0f}"
    return f"{x:.{nd}f}" if isinstance(x, float) else str(x)


RESULT_FIELDS = [
    "ticker", "spolka", "sektor", "branza", "gielda", "kapitalizacja_usd", "p_fcf", "wzrost_przychodow_rr",
    "beta", "odleglosc_od_szczytu", "short_float", "days_to_cover", "wynik", "pkt_fundamenty", "pkt_technika",
    "pkt_squeeze", "pkt_katalizator", "pkt_kary", "dyskwalifikacja", "katalizatory", "kary", "fcf_ttm_usd",
    "fcf_ttm_rok_wczesniej_usd", "dlug_netto_ebitda", "gotowka_netto", "sredni_obrot_50d_usd", "kurs",
    "szczyt_52t", "sma50", "data_kursu", "okres_fundamentow", "raport_zrodlowy", "zrodlo_short",
    "do_weryfikacji",
]


def result_row(t: str, base: dict, m: dict, s: dict) -> dict:
    return {
        "ticker": t, "spolka": base["name"], "sektor": base["sector"], "branza": base["industry"],
        "gielda": base["exchange"], "kapitalizacja_usd": fmt(m.get("market_cap"), money=True),
        "p_fcf": fmt(m.get("p_fcf"), nd=1), "wzrost_przychodow_rr": fmt(m.get("rev_growth"), pct=True),
        "beta": fmt(m.get("beta")), "odleglosc_od_szczytu": fmt(m.get("dist_from_high"), pct=True),
        "short_float": fmt(m.get("short_float"), pct=True), "days_to_cover": fmt(m.get("days_to_cover"), nd=1),
        "wynik": s["wynik"], "pkt_fundamenty": s["pkt_fundamenty"], "pkt_technika": s["pkt_technika"],
        "pkt_squeeze": s["pkt_squeeze"], "pkt_katalizator": s["pkt_katalizator"], "pkt_kary": s["pkt_kary"],
        "dyskwalifikacja": s["dyskwalifikacja"], "katalizatory": " | ".join(s["katalizatory"]),
        "kary": " | ".join(f"{n} ({p})" for n, p in s["kary"]),
        "fcf_ttm_usd": fmt(m.get("fcf_ttm"), money=True), "fcf_ttm_rok_wczesniej_usd": fmt(m.get("fcf_ttm_prev"), money=True),
        "dlug_netto_ebitda": fmt(m.get("net_debt_ebitda")), "gotowka_netto": "tak" if m.get("net_cash") else "nie",
        "sredni_obrot_50d_usd": fmt(m.get("avg_dollar_volume"), money=True), "kurs": fmt(m.get("price")),
        "szczyt_52t": fmt(m.get("high_52w")), "sma50": fmt(m.get("sma50")), "data_kursu": m.get("price_date", ""),
        "okres_fundamentow": f"kw. do {m.get('revenue_q_end', '')}; FCF TTM do {m.get('fcf_end', '')}",
        "raport_zrodlowy": m.get("latest_report_url", ""),
        "zrodlo_short": f"{m.get('short_float_src', '')}; DTC: {m.get('dtc_src', '')}",
        "do_weryfikacji": " | ".join(m.get("verify", [])),
    }


def write_csv(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    fields = fields or (list(rows[0].keys()) if rows else ["ticker"])
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_report(path: Path, today: date, ranked: list[tuple], funnel: Counter, excluded: list[dict],
                 disc: list[dict], src: Sources, top: int) -> None:
    by_t: dict[str, list[dict]] = {}
    for r in src.rows:
        by_t.setdefault(r["ticker"], []).append(r)
    L = [f"# Screening small-cap USA ({today.isoformat()})", "",
         "Dane: SEC EDGAR (XBRL companyfacts, submissions, full-text search, Form 4), Yahoo Finance (kursy), "
         "Nasdaq (uniwersum, short interest, daty wyników), Finviz (short float). Każda liczba ze źródłem i datą: "
         "`sources.csv`. Spółki odrzucone z powodem: `excluded.csv`.", "",
         "## Lejek", ""]
    for stage, n in funnel.items():
        L.append(f"- {stage}: {n}")
    reasons = Counter(e["powod"].split(":")[0] for e in excluded)
    L += ["", "Najczęstsze powody odrzucenia:", ""]
    L += [f"- {r}: {n}" for r, n in reasons.most_common(12)]
    L += ["", f"## Top {top}", "",
          "| # | Ticker | Spółka | Wynik | F | T | S | K | Kary | Kap. (mln USD) | P/FCF | Przych. r/r | Beta | Od szczytu | Short float | DTC |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, (t, base, m, s) in enumerate(ranked[:top], 1):
        L.append(f"| {i} | {t} | {base['name']} | **{s['wynik']}** | {s['pkt_fundamenty']} | {s['pkt_technika']} | "
                 f"{s['pkt_squeeze']} | {s['pkt_katalizator']} | {s['pkt_kary']} | {fmt((m.get('market_cap') or 0) / 1e6, money=True)} | "
                 f"{fmt(m.get('p_fcf'), nd=1)} | {fmt(m.get('rev_growth'), pct=True)} | {fmt(m.get('beta'))} | "
                 f"{fmt(m.get('dist_from_high'), pct=True)} | {fmt(m.get('short_float'), pct=True)} | {fmt(m.get('days_to_cover'), nd=1)} |")
    for i, (t, base, m, s) in enumerate(ranked[:top], 1):
        L += ["", f"### {i}. {t} — {base['name']} ({base['sector']})", ""]
        for group in ("fundamenty", "technika", "squeeze"):
            L.append(f"- {group}: " + ", ".join(f"{k} {'✔' if v else '✘'}" for k, v in s[group].items()))
        L.append(f"- katalizatory: {', '.join(s['katalizatory']) or 'brak'}")
        L.append(f"- kary: {', '.join(f'{n} ({p})' for n, p in s['kary']) or 'brak'}")
        if s["dyskwalifikacja"]:
            L.append(f"- **dyskwalifikacja: {s['dyskwalifikacja']}**")
        if m.get("verify"):
            L.append(f"- do weryfikacji: {'; '.join(m['verify'])}")
        L.append(f"- ostatni raport: [{m.get('latest_report', '')}]({m.get('latest_report_url', '')})")
        L += ["", "| Metryka | Wartość | Źródło | Data danych |", "|---|---|---|---|"]
        for r in by_t.get(t, []):
            link = f"[{r['zrodlo']}]({r['url']})" if r["url"] else r["zrodlo"]
            v = r["wartosc"]
            val = (f"{v:,.0f}" if abs(v) >= 1000 else f"{v:.4g}") if isinstance(v, float) else v
            L.append(f"| {r['metryka']} | {val} | {link} | {r['data_danych']} |")
    if disc:
        L += ["", "## Rozbieżności screener vs raport", "", "| Ticker | Metryka | Screener | Raport | Źródło |",
              "|---|---|---|---|---|"]
        L += [f"| {d['ticker']} | {d['metryka']} | {d['screener']} | {d['raport']} | {d['zrodlo_raportu']} |" for d in disc]
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- główna pętla

def main(argv: list[str] | None = None, fetcher: Fetcher | None = None) -> int:
    ap = argparse.ArgumentParser(description="Screening small-capów USA z potencjałem wzrostu (1-3 mies.)")
    ap.add_argument("--out", default="output")
    ap.add_argument("--cache", default="data/cache")
    ap.add_argument("--overrides", default="overrides.csv")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--limit", type=int, default=0, help="tylko N pierwszych spółek (test)")
    ap.add_argument("--tickers", default="", help="lista tickerów po przecinku zamiast pełnego uniwersum")
    ap.add_argument("--offline", action="store_true", help="tylko dane z cache")
    ap.add_argument("--max-age-h", type=float, default=20.0)
    ap.add_argument("--as-of", default="", help="data screeningu RRRR-MM-DD (domyślnie dziś)")
    args = ap.parse_args(argv)

    today = date.fromisoformat(args.as_of) if args.as_of else date.today()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    f = fetcher or Fetcher(Path(args.cache), os.environ.get("SEC_USER_AGENT"), args.max_age_h, args.offline)
    src = Sources()
    excluded: list[dict] = []
    funnel: Counter = Counter()
    overrides = load_overrides(Path(args.overrides))
    disc: list[dict] = []

    def drop(t, name, stage, reason, url=""):
        excluded.append({"ticker": t, "spolka": name, "etap": stage, "powod": reason, "zrodlo": url})

    # 1. Uniwersum
    rows = []
    for ex, label in market.EXCHANGES.items():
        doc = f.get(market.NASDAQ_SCREENER_URL.format(ex=ex))
        for r in market.screener_rows(doc.data, label):
            r["_doc"] = doc
            rows.append(r)
    sec_t = f.get(edgar.TICKERS_URL)
    fields = sec_t.data["fields"]
    cik_by = {}
    for rec in sec_t.data["data"]:
        d = dict(zip(fields, rec))
        cik_by.setdefault(norm_ticker(d["ticker"]), int(d["cik"]))
    only = {norm_ticker(x) for x in args.tickers.split(",") if x.strip()}
    funnel["notowane na NYSE/Nasdaq/NYSE American (Nasdaq screener)"] = len(rows)

    stage1 = []
    for r in rows:
        t = norm_ticker(r["symbol"])
        if only and t not in only:
            continue
        reason = market.universe_exclusion(r)
        cap = r["market_cap"]
        if not reason and (not cap or not CAP_MIN * (1 - CAP_BUFFER) <= cap <= CAP_MAX * (1 + CAP_BUFFER)):
            reason = "kapitalizacja (Nasdaq screener) poza zakresem"
        if not reason and t not in cik_by:
            reason = "brak CIK w SEC"
        if reason:
            if cap and CAP_MIN * (1 - CAP_BUFFER) <= cap <= CAP_MAX * (1 + CAP_BUFFER):
                drop(t, r["name"], "uniwersum", reason, r["_doc"].url)
            continue
        r["ticker"], r["cik"] = t, cik_by[t]
        stage1.append(r)
    if args.limit:
        stage1 = stage1[:args.limit]
    funnel["w przedziale kapitalizacji (z marginesem), akcje zwykłe, bez SPAC/REIT/Chin"] = len(stage1)
    log(f"uniwersum: {len(stage1)} spółek")

    # 2. Fundamenty z SEC
    stage2 = []
    for i, r in enumerate(stage1, 1):
        t, cik = r["ticker"], r["cik"]
        if i % 50 == 0:
            log(f"  SEC {i}/{len(stage1)}")
        try:
            sub = f.get(edgar.SUBMISSIONS_URL.format(cik=cik))
        except FetchError as e:
            drop(t, r["name"], "SEC", f"błąd pobrania submissions: {e}")
            continue
        prof = edgar.company_profile(sub.data)
        if prof["sic"] in edgar.SPAC_SIC:
            drop(t, r["name"], "SEC", "SPAC (SIC 6770)", sub.url)
            continue
        if prof["sic"] in edgar.REIT_SIC:
            drop(t, r["name"], "SEC", "REIT (SIC 6798)", sub.url)
            continue
        if prof["country_code"] in edgar.CHINA_CODES:
            drop(t, r["name"], "SEC", f"siedziba: {prof['country_desc']}", sub.url)
            continue
        filings = edgar.filings_table(sub.data)
        if edgar.latest_periodic(filings) is None:
            drop(t, r["name"], "SEC", "brak 10-Q/10-K (emitent 20-F/40-F: brak kwartalnych danych XBRL)", sub.url)
            continue
        try:
            cf = f.get(edgar.COMPANYFACTS_URL.format(cik=cik), transform=xbrl.slim_companyfacts)
        except NotFound:
            drop(t, r["name"], "SEC", "brak danych XBRL", sub.url)
            continue
        except FetchError as e:
            drop(t, r["name"], "SEC", f"błąd pobrania companyfacts: {e}")
            continue
        m, notes = fundamentals(t, cik, cf.data, cf, src, today)
        if notes:
            drop(t, r["name"], "fundamenty", "; ".join(notes), cf.url)
            continue
        if not m.get("fcf_ttm", 0) > 0:
            drop(t, r["name"], "fundamenty", f"F1: FCF TTM {fmt(m.get('fcf_ttm'), money=True)} <= 0", cf.url)
            continue
        if m["fcf_ex_one_off"] <= 0:
            drop(t, r["name"], "fundamenty", "F1: FCF dodatni tylko dzięki pozycjom jednorazowym", cf.url)
            continue
        if not m.get("rev_growth", 0) > 0:
            drop(t, r["name"], "fundamenty", f"F2: przychody kw. r/r {fmt(m.get('rev_growth'), pct=True)}", cf.url)
            continue
        r["sub"], r["filings"], r["m"] = sub, filings, m
        stage2.append(r)
    funnel["F1 (FCF TTM > 0) i F2 (przychody kw. r/r rosną), dane SEC"] = len(stage2)
    log(f"po fundamentach: {len(stage2)}")

    # 3. Kurs i technika
    bench_df, _, _ = load_prices(f, "SPY")
    stage3 = []
    for r in stage2:
        t, m = r["ticker"], r["m"]
        try:
            df, yd, price_src = load_prices(f, r["symbol"])
        except (FetchError, ValueError, KeyError) as e:
            drop(t, r["name"], "kurs", f"brak notowań: {e}")
            continue
        if len(df) < 120:
            drop(t, r["name"], "kurs", f"za krótka historia notowań ({len(df)} sesji)", yd.url)
            continue
        tech = technicals(df, bench_df)
        m.update({k: tech[k] for k in ("price", "high_52w", "dist_from_high", "sma50", "above_sma50",
                                        "sma50_turning_up", "higher_low", "breakout_volume", "beta")})
        m["avg_dollar_volume"], m["price_date"], m["tech"] = tech["avg_dollar_volume_50d"], tech["date"], tech
        for key, label in (("price", "kurs"), ("high_52w", "szczyt_52t"), ("sma50", "sma50"),
                           ("dist_from_high", "odleglosc_od_szczytu"), ("avg_dollar_volume", "sredni_obrot_50d"),
                           ("beta", "beta")):
            note = f"52 tyg. szczyt z {tech['high_52w_date']}" if key == "high_52w" else ""
            if key == "beta":
                note = f"tygodniowe stopy zwrotu, {tech['beta_weeks']} tyg., benchmark SPY"
            src.add(t, label, m[key], f"{price_src} (wyliczone)", yd.url, tech["date"], yd.retrieved_at, note)
        if tech["breakout"]:
            b = tech["breakout"]
            src.add(t, "wybicie_3m", b["close"], f"zamknięcie > max 63 sesji ({b['level_3m']:.2f}), wolumen x{b['vol_ratio']:.1f}",
                    yd.url, b["date"], yd.retrieved_at)
        nasdaq_cap = r["market_cap"]
        if m.get("shares"):
            m["market_cap"] = m["shares"] * m["price"]
            src.add(t, "kapitalizacja", m["market_cap"], "liczba akcji (SEC) x kurs (Yahoo)", "", tech["date"])
            if nasdaq_cap and abs(m["market_cap"] / nasdaq_cap - 1) > 0.10:
                disc.append({"ticker": t, "metryka": "kapitalizacja", "screener": nasdaq_cap, "raport": m["market_cap"],
                             "zrodlo_raportu": "akcje z raportu SEC x kurs", "data": tech["date"],
                             "uwagi": "Nasdaq screener vs SEC"})
        else:
            m["market_cap"] = nasdaq_cap
            src.add(t, "kapitalizacja", nasdaq_cap, "Nasdaq screener", r["_doc"].url, r["_doc"].retrieved_at[:10])
        m["p_fcf"] = m["market_cap"] / m["fcf_ttm"]
        src.add(t, "p_fcf", m["p_fcf"], "kapitalizacja / FCF TTM", "", tech["date"])
        fails = [x for x in hard_filter_failures(m) if not x.startswith("F6")]
        if fails:
            drop(t, r["name"], "kurs/technika", "; ".join(fails), yd.url)
            continue
        stage3.append(r)
    funnel["kapitalizacja 300 mln-3 mld, obrót > 2 mln, F3 beta > 1,2, F4 >= 25% pod szczytem, F5 nad SMA50"] = len(stage3)
    log(f"po technice: {len(stage3)}")

    # 4. EDGAR (rozwodnienie, delisting, going concern, insiderzy, sprawy prawne) i short interest
    scored = []
    for r in stage3:
        t, m = r["ticker"], r["m"]
        log(f"  szczegóły: {t}")
        filings = r["filings"]
        oldest = min((x["filingDate"] for x in filings), default=today)
        if oldest > today - timedelta(days=3 * 365):
            for page in r["sub"].data.get("filings", {}).get("files", []):
                if page.get("filingTo", "") >= (today - timedelta(days=3 * 365)).isoformat():
                    try:
                        filings += edgar.filings_table(f.get(edgar.SUBMISSIONS_PAGE_URL.format(name=page["name"])).data)
                    except FetchError as e:
                        m.setdefault("verify", []).append(f"starsze zgłoszenia niedostępne: {e}")
        edgar_checks(t, r["cik"], filings, r["sub"], f, src, m, today)
        market_checks(t, r["symbol"], f, src, m, today)
        if m.get("beta_finviz") is not None and abs(m["beta_finviz"] - m["beta"]) > 0.3:
            disc.append({"ticker": t, "metryka": "beta", "screener": m["beta_finviz"], "raport": round(m["beta"], 2),
                         "zrodlo_raportu": "wyliczona z notowań (SPY, tygodniowo, 2 lata)", "data": m["price_date"],
                         "uwagi": "Finviz liczy betę z innego okna"})
        apply_overrides(t, m, overrides.get(t, []), src, disc)
        fails = hard_filter_failures(m)
        if fails:
            drop(t, r["name"], "EDGAR", "; ".join(fails), m.get("latest_report_url", ""))
            continue
        scored.append((t, r, m, score(m)))
    funnel["F6 (bez going concern i delistingu)"] = len(scored)

    ranked = sorted(scored, key=lambda x: (x[3]["dyskwalifikacja"] == "", x[3]["wynik"],
                                           x[3]["pkt_fundamenty"]), reverse=True)
    results = [result_row(t, r, m, s) for t, r, m, s in ranked]
    write_csv(out / "results.csv", results, RESULT_FIELDS)
    write_csv(out / "sources.csv", src.rows)
    write_csv(out / "excluded.csv", excluded, ["ticker", "spolka", "etap", "powod", "zrodlo"])
    write_csv(out / "discrepancies.csv", disc, ["ticker", "metryka", "screener", "raport", "zrodlo_raportu", "data", "uwagi"])
    write_report(out / "raport.md", today, [(t, r, m, s) for t, r, m, s in ranked], funnel, excluded, disc, src, args.top)
    (out / "funnel.json").write_text(json.dumps(funnel, ensure_ascii=False, indent=1))
    log(f"gotowe: {len(results)} spółek w {out / 'results.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
