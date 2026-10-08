"""Filtry twarde, punktacja 0-12 i kary według specyfikacji screeningu."""
from __future__ import annotations

import math

CAP_MIN, CAP_MAX = 300e6, 3e9
MIN_DOLLAR_VOLUME = 2e6


def _ok(x) -> bool:
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def hard_filter_failures(m: dict) -> list[str]:
    """Lista niespełnionych filtrów twardych (pusta = spółka przechodzi).
    Brak danych traktujemy jak niespełnienie, bo filtra nie da się potwierdzić."""
    fails = []
    cap = m.get("market_cap")
    if not _ok(cap) or not CAP_MIN <= cap <= CAP_MAX:
        fails.append("kapitalizacja poza 300 mln - 3 mld USD")
    dv = m.get("avg_dollar_volume")
    if not _ok(dv) or dv < MIN_DOLLAR_VOLUME:
        fails.append("średni obrót < 2 mln USD")
    if not _ok(m.get("fcf_ttm")) or m["fcf_ttm"] <= 0:
        fails.append("F1: FCF TTM <= 0 lub brak danych")
    if not _ok(m.get("rev_growth")) or m["rev_growth"] <= 0:
        fails.append("F2: przychody kw. r/r nie rosną lub brak danych")
    if not _ok(m.get("beta")) or m["beta"] <= 1.2:
        fails.append("F3: beta <= 1,2 lub brak danych")
    if not _ok(m.get("dist_from_high")) or m["dist_from_high"] > -0.25:
        fails.append("F4: kurs mniej niż 25% pod 52-tyg. szczytem")
    if not m.get("above_sma50"):
        fails.append("F5: kurs pod SMA50")
    bf = m.get("beta_finviz")
    if _ok(bf) and bf <= 1.2:
        fails.append(f"F3: beta wg Finviz {bf:.2f} <= 1,2")
    dff = m.get("dist_from_high_finviz")
    if _ok(dff) and dff > -0.25:
        fails.append(f"F4: wg Finviz tylko {dff * 100:.1f}% pod 52-tyg. szczytem")
    if m.get("going_concern"):
        fails.append("F6: ostrzeżenie going concern")
    if m.get("delisting"):
        fails.append("F6: groźba delistingu")
    if m.get("exclude"):
        fails.append(f"weryfikacja ręczna: {m['exclude']}")
    return fails


def score(m: dict) -> dict:
    f = {
        "fcf_rosnie": bool(_ok(m.get("fcf_ttm")) and _ok(m.get("fcf_ttm_prev")) and m["fcf_ttm"] > m["fcf_ttm_prev"]),
        "przychody_>15%": bool(_ok(m.get("rev_growth")) and m["rev_growth"] > 0.15),
        "gotowka_netto_lub_dlug/ebitda<1,5": bool(
            m.get("net_cash") or (_ok(m.get("net_debt_ebitda")) and 0 <= m["net_debt_ebitda"] < 1.5)),
        "p/fcf<20": bool(_ok(m.get("p_fcf")) and 0 < m["p_fcf"] < 20),
    }
    t = {
        "sma50_zawraca": bool(m.get("sma50_turning_up")),
        "wyzszy_dolek": bool(m.get("higher_low")),
        "wolumen_na_wybiciu": bool(m.get("breakout_volume")),
    }
    s = {
        "short_float>15%": bool(_ok(m.get("short_float")) and m["short_float"] > 0.15),
        "days_to_cover>5": bool(_ok(m.get("days_to_cover")) and m["days_to_cover"] > 5),
    }
    catalysts = list(m.get("catalysts") or [])
    pts_f, pts_t, pts_s = sum(f.values()), sum(t.values()), sum(s.values())
    pts_c = min(2, len(catalysts))

    penalties = []
    if m.get("one_off"):
        penalties.append(("jednorazówka w wyniku", -2))
    if m.get("dilution"):
        penalties.append(("rozwodnienie (S-3/ATM/emisja/rejestracja)", -2))
    if m.get("legal"):
        penalties.append(("pozew zbiorowy / raport short sellera / SEC", -2))
    if _ok(m.get("insider_sell_usd")) and m["insider_sell_usd"] > 1e6:
        penalties.append(("sprzedaż insiderów > 1 mln USD / 90 dni", -1))
    if _ok(m.get("top_customer_share")) and m["top_customer_share"] > 0.30:
        penalties.append(("klient > 30% przychodów", -1))
    pen = sum(p for _, p in penalties)

    # Rozwodnienie dyskwalifikuje kandydatów na squeeze (spełniony choć jeden warunek paliwa).
    disqualified = bool(m.get("dilution") and pts_s > 0)
    return {
        "fundamenty": f, "technika": t, "squeeze": s, "katalizatory": catalysts,
        "pkt_fundamenty": pts_f, "pkt_technika": pts_t, "pkt_squeeze": pts_s, "pkt_katalizator": pts_c,
        "kary": penalties, "pkt_kary": pen,
        "wynik": pts_f + pts_t + pts_s + pts_c + pen,
        "dyskwalifikacja": "rozwodnienie przy kandydacie na squeeze" if disqualified else "",
    }
