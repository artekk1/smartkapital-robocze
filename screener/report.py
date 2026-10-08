"""raport.md: TOP 10 z tezą i ryzykiem, kandydaci na short squeeze, odrzucone w ostatniej chwili,
metodologia i ograniczenia. Liczby pochodzą z danych screeningu; teza, ryzyko i katalizatory
z analysis/notes.json (weryfikacja ręczna ze źródłami)."""
from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path

from .checks import SHARES_6M_THRESHOLD


def _f(x, kind="num", nd=2) -> str:
    if x is None or (isinstance(x, float) and x != x):
        return "b.d."
    if kind == "pct":
        return f"{x * 100:.1f}%".replace(".", ",")
    if kind == "mln":
        return f"{x / 1e6:,.0f}".replace(",", " ") + " mln USD"
    if kind == "usd":
        return f"{x:,.2f}".replace(",", " ").replace(".", ",") + " USD"
    return f"{x:.{nd}f}".replace(".", ",")


def _iso(d) -> str:
    return d.isoformat() if hasattr(d, "isoformat") else str(d or "b.d.")


def _link(text: str, url: str) -> str:
    return f"[{text}]({url})" if url else text


def _catalyst(t: str, m: dict, note: dict) -> str:
    if note.get("katalizator"):
        return note["katalizator"]
    if m.get("next_earnings"):
        kind = "szacunkowa (Zacks, przez Nasdaq)" if m.get("next_earnings_est") else m.get("next_earnings_src", "")
        return f"wyniki kwartalne {_iso(m['next_earnings'])} (data {kind})"
    return "brak potwierdzonego katalizatora w horyzoncie 6 miesięcy"


def _company(i: int, t: str, base: dict, m: dict, s: dict, note: dict) -> list[str]:
    cat = m.get("finviz_cat") or {}
    sector = cat.get("sector") or base["sector"]
    pd_ = m.get("price_date")
    inval = m.get("invalidation")
    inval_pct = (inval / m["price"] - 1) if inval else None
    L = [f"### {i}. {t} – {base['name']} ({sector}{', ' + cat['industry'] if cat.get('industry') else ''})", "",
         f"**Wynik: {s['wynik']}/12** (fundamenty {s['pkt_fundamenty']}/4, technika {s['pkt_technika']}/3, "
         f"squeeze {s['pkt_squeeze']}/2, katalizatory {s['pkt_katalizator']}/2, kary {s['pkt_kary']})", "",
         f"**Teza.** {note.get('teza', '_brak – do uzupełnienia w analysis/notes.json_')}", "",
         f"**Ryzyko.** {note.get('ryzyko', '_brak – do uzupełnienia w analysis/notes.json_')}", "",
         f"**Najbliższy katalizator.** {_catalyst(t, m, note)}", ""]
    if inval:
        L += [f"**Poziom unieważnienia.** Zamknięcie dzienne poniżej **{_f(inval, 'usd')}** "
              f"({m.get('invalidation_basis')}; {_f(inval_pct, 'pct')} od kursu {_f(m['price'], 'usd')} z {pd_}). "
              "Poniżej tego poziomu znika wyższy dołek albo kurs wraca pod SMA50, więc setup przestaje działać.", ""]
    nd = (f"gotówka netto {_f(-m['net_debt'], 'mln')}" if m.get("net_cash")
          else f"dług netto/EBITDA {_f(m.get('net_debt_ebitda'))}")
    ins = f"zakupy {_f(m.get('insider_buy_usd'), 'mln')}, sprzedaż {_f(m.get('insider_sell_usd'), 'mln')}"
    L += ["Dane (źródło, data):", "",
          f"- Kurs {_f(m['price'], 'usd')}, SMA50 {_f(m['sma50'], 'usd')}, 52-tyg. szczyt {_f(m['high_52w'], 'usd')} "
          f"({m['tech']['high_52w_date']}), odległość od szczytu {_f(m['dist_from_high'], 'pct')} "
          f"(Finviz: {_f(m.get('dist_from_high_finviz'), 'pct')}) – {_link(m.get('price_src', ''), m.get('price_url', ''))}, "
          f"zamknięcie {pd_}",
          f"- Beta {_f(m.get('beta'))} (2 lata, tygodniowo vs SPY, wyliczona z notowań), Finviz {_f(m.get('beta_finviz'))}"
          f" – {_link('Finviz', m.get('finviz_url', ''))}, pobrano {_iso(m.get('_finviz_date')) if m.get('_finviz_date') else pd_}",
          f"- Kapitalizacja {_f(m.get('market_cap'), 'mln')} ({m.get('cap_src', '')}, kurs z {pd_}, liczba akcji na "
          f"{_iso(m.get('shares_end'))}); P/FCF {_f(m.get('p_fcf'), nd=1)} (Finviz {_f(m.get('p_fcf_finviz'), nd=1)})",
          f"- Przychody kw. {_f(m.get('revenue_q'), 'mln')} (kw. do {_iso(m.get('revenue_q_end'))}) vs "
          f"{_f(m.get('revenue_q_prev'), 'mln')} rok wcześniej: {_f(m.get('rev_growth'), 'pct')} r/r – "
          f"{_link('Yahoo fundamentals-timeseries', m.get('fund_url', ''))}",
          f"- FCF TTM {_f(m.get('fcf_ttm'), 'mln')} (do {_iso(m.get('fcf_end'))}) vs {_f(m.get('fcf_ttm_prev'), 'mln')} "
          f"rok wcześniej (do {_iso(m.get('fcf_ttm_prev_end'))}); {nd} (bilans {_iso(m.get('bs_date'))}, dług bez leasingu) – Yahoo",
          f"- Short float {_f(m.get('short_float'), 'pct')} ({m.get('short_float_src', 'b.d.')}), days to cover "
          f"{_f(m.get('days_to_cover'), nd=1)} ({m.get('dtc_src', 'b.d.')})",
          f"- Insiderzy (90 dni, Nasdaq): {ins}",
          f"- Kary: {', '.join(f'{n} ({p})' for n, p in s['kary']) or 'brak'}"]
    if m.get("dilution_detail"):
        L.append("- Ślady rozwodnienia: " + "; ".join(f"{d['kind']} ({_iso(d['date'])})" for d in m["dilution_detail"][:4]))
    if note.get("weryfikacja"):
        L.append(f"- Weryfikacja ręczna: {note['weryfikacja']}")
    if note.get("zrodla"):
        L.append("- Źródła weryfikacji: " + ", ".join(_link(x.get("opis", x["url"]), x["url"]) for x in note["zrodla"]))
    return L + [""]


def write_report(path: Path, *, today: date, universe_date: str, top: list, squeeze: list, late: list,
                 funnel: Counter, excluded: list, disc: list, src, notes: dict, n_scored: int) -> None:
    price_dates = sorted({m.get("price_date") for _, _, m, _ in top if m.get("price_date")})
    L = [f"# Screening small-capów USA – {today.isoformat()}", "",
         f"Dane rynkowe na zamknięcie {', '.join(price_dates) or 'b.d.'}; uniwersum pobrane {universe_date[:16].replace('T', ' ')} UTC; "
         "dane finansowe z ostatnich opublikowanych kwartałów (okres podany przy każdej spółce). "
         f"Po filtrach twardych zostało {n_scored} spółek (pełna lista: `results.csv`, źródło i data każdej liczby: `sources.csv`).", "",
         "> Materiał analityczny, nie rekomendacja inwestycyjna. Small-capy z betą > 1,2 potrafią tracić 20-30% w kilka sesji.", "",
         f"## TOP {len(top)}", "",
         "| # | Ticker | Spółka | Wynik | F | T | S | K | Kary | Kurs | Od szczytu | Beta | P/FCF | Przychody r/r | Short float | DTC | Unieważnienie |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for i, (t, base, m, s) in enumerate(top, 1):
        L.append(f"| {i} | {t} | {base['name']} | **{s['wynik']}** | {s['pkt_fundamenty']} | {s['pkt_technika']} | "
                 f"{s['pkt_squeeze']} | {s['pkt_katalizator']} | {s['pkt_kary']} | {_f(m['price'])} | "
                 f"{_f(m['dist_from_high'], 'pct')} | {_f(m.get('beta'))} | {_f(m.get('p_fcf'), nd=1)} | "
                 f"{_f(m.get('rev_growth'), 'pct')} | {_f(m.get('short_float'), 'pct')} | {_f(m.get('days_to_cover'), nd=1)} | "
                 f"{_f(m.get('invalidation'))} |")
    L.append("")
    for i, (t, base, m, s) in enumerate(top, 1):
        L += _company(i, t, base, m, s, notes.get(t, {}))

    L += [f"## {len(squeeze)} najlepszych kandydatów na short squeeze", "",
          "Warunek: co najmniej jeden punkt za paliwo squeeze, brak śladów rozwodnienia (rozwodnienie dyskwalifikuje "
          "kandydata na squeeze). Kolejność: punkty squeeze, short float, days to cover.", "",
          "| # | Ticker | Spółka | Short float | Days to cover | Rozliczenie SI | Akcje w obrocie (float) | Wynik | Najbliższy katalizator |",
          "|---|---|---|---|---|---|---|---|---|"]
    for i, (t, base, m, s) in enumerate(squeeze, 1):
        L.append(f"| {i} | {t} | {base['name']} | {_f(m.get('short_float'), 'pct')} ({m.get('short_float_src', '')}) | "
                 f"{_f(m.get('days_to_cover'), nd=1)} | {m.get('si_date', 'b.d.')} (Nasdaq) | {_f(m.get('float_finviz'), 'mln')} (Finviz) | "
                 f"{s['wynik']} | {_catalyst(t, m, notes.get(t, {}))} |")
    if not squeeze:
        L.append("| – | brak spółek spełniających warunek | | | | | | | |")
    L.append("")

    L += ["## Odrzucone w ostatniej chwili", "",
          "Spółki, które przeszły filtry ilościowe (F1-F5), ale odpadły na kontroli końcowej: ostrzeżenia (F6), "
          "kontrola krzyżowa z Finviz, weryfikacja ręczna, dyskwalifikacja albo samokontrola TOP 10 "
          "(odległość od szczytu, beta, ATM).", "",
          "| Ticker | Spółka | Wynik | Powód |", "|---|---|---|---|"]
    for x in late:
        L.append(f"| {x['ticker']} | {x['spolka']} | {x.get('wynik', '–')} | {x['powod']} |")
    if not late:
        L.append("| – | brak | | |")
    L.append("")

    reasons = Counter(e["powod"].split(":")[0].split(";")[0] for e in excluded)
    L += ["## Metodologia i ograniczenia", "",
          f"**Data danych.** Screening z {today.isoformat()}. Kursy: zamknięcie {', '.join(price_dates) or 'b.d.'} "
          "(Yahoo Finance). Uniwersum: Nasdaq screener z dnia pobrania. Dane finansowe: ostatnie kwartały "
          "raportowane przez spółki, w agregacji Yahoo Finance (okres przy każdej spółce). Short interest: Nasdaq, "
          "z datą rozliczenia przy spółce; short float: Finviz, który nie podaje daty rozliczenia.", "",
          "**Źródła.** Nasdaq: uniwersum, short interest, daty wyników, lista zgłoszeń do SEC (QuoteMedia), "
          "transakcje insiderów. Yahoo Finance: notowania dzienne i dane ze sprawozdań (fundamentals-timeseries). "
          "Finviz: short float, beta, P/FCF i dynamika sprzedaży do kontroli krzyżowej, branża, nagłówki "
          "wiadomości. Wyszukiwarka internetowa: weryfikacja ręczna TOP 10 (źródła przy spółkach).", "",
          "**Lejek.**", ""]
    L += [f"- {k}: {v}" for k, v in funnel.items()]
    L += ["", "Najczęstsze powody odrzucenia: " + "; ".join(f"{r} ({n})" for r, n in reasons.most_common(8)) + ".", "",
          "**Filtry twarde.** Kapitalizacja 300 mln - 3 mld USD (liczba akcji ze sprawozdania x kurs); średni dzienny "
          "obrót z 50 sesji co najmniej 2 mln USD; bez SPAC-ów, REIT-ów, spółek z Chin, Hongkongu i Makau "
          "(nazwa, branża, kraj wg Nasdaq i Finviz) oraz spółek bez przychodów. "
          "F1: FCF TTM > 0. F2: przychody ostatniego kwartału wyższe niż rok wcześniej. "
          "F3: beta > 1,2 według własnego wyliczenia (2 lata, tygodniowe stopy zwrotu vs SPY) i według Finviz. "
          "F4: kurs co najmniej 25% pod 52-tygodniowym szczytem (maksimum intraday z 252 sesji), także według Finviz. "
          "F5: kurs nad SMA50. F6: brak nagłówków o going concern i o zawiadomieniach o niespełnieniu "
          "wymogów notowania w 12 miesięcy (wcześniejsze zawiadomienie znosi późniejsze odzyskanie zgodności).", "",
          "**Punktacja.** FCF rośnie r/r: FCF TTM wyższy niż FCF TTM rok wcześniej. Gotówka netto albo dług netto/EBITDA "
          "< 1,5: dług bez zobowiązań leasingowych, gotówka z inwestycjami krótkoterminowymi. SMA50 zaczyna rosnąć: "
          "SMA50 wyższa niż 5 sesji temu, a w ciągu wcześniejszych 60 sesji spadała. Wyższy dołek: ostatni "
          "potwierdzony dołek (5 świec z każdej strony na dziennym, 2 na tygodniowym) wyżej od poprzedniego. "
          "Wolumen na wybiciu: w ostatnich 10 sesjach zamknięcie ponad maksimum z 63 sesji przy wolumenie co najmniej "
          "1,5x średniej z 50 sesji. Katalizatory: wyniki w ciągu 6 miesięcy, zakupy insiderów w 90 dni oraz zdarzenia "
          "znalezione przy weryfikacji ręcznej (maksymalnie 2 punkty).", "",
          "**Kary.** Jednorazówka: pozycje nadzwyczajne (Yahoo: TotalUnusualItems) > 25% zysku netto TTM albo "
          "weryfikacja ręczna. Rozwodnienie: S-3/F-3, prospekt 424B lub S-1 na liście zgłoszeń, wzrost liczby akcji "
          f"o ponad {SHARES_6M_THRESHOLD * 100:.0f}% w 6 miesięcy albo wiadomość o emisji lub programie ATM w 6 miesięcy; "
          "przy kandydacie na squeeze rozwodnienie oznacza dyskwalifikację. Sprawy prawne: nagłówki o złożonym pozwie "
          "zbiorowym, raporcie short sellera lub dochodzeniu SEC. Insiderzy: sprzedaż na rynku > 1 mln USD w 90 dni "
          "(Nasdaq). Klient > 30% przychodów: tylko weryfikacja ręczna.", "",
          "**Samokontrola TOP 10.** Przed publikacją każda spółka z TOP 10 musi być co najmniej 25% pod szczytem "
          "(Yahoo i Finviz), mieć betę > 1,2 (własną i Finviz) i nie mieć śladu programu ATM. Spółki, które "
          "nie przeszły, są w tabeli „Odrzucone w ostatniej chwili”, a ich miejsce zajmują kolejne z rankingu.", "",
          "**Ograniczenia.**", "",
          "- Na Twoją prośbę screening nie korzysta z SEC EDGAR. Dane finansowe pochodzą z agregacji Yahoo, a nie "
          "bezpośrednio z 10-Q/10-K. Nie ma też odczytu treści raportów: going concern, klienci, pozwy.",
          "- Lista zgłoszeń z Nasdaq sięga ok. 6 miesięcy wstecz. Starszy shelf S-3 albo program ATM ustanowiony "
          "wcześniej widać tylko pośrednio: przez wzrost liczby akcji, wiadomości albo weryfikację ręczną TOP 10.",
          "- Nagłówki Finviz obejmują ok. 100 ostatnich wiadomości. Przy spółkach z dużą liczbą newsów to kilka tygodni.",
          "- Daty wyników oznaczone jako szacunkowe pochodzą z algorytmu Zacks i mogą się przesunąć.",
          "- Beta zależy od okna i benchmarku; dlatego wymagamy > 1,2 w obu ujęciach.",
          f"- Rozbieżności między Finviz a danymi ze sprawozdań/wyliczeniami: {len(disc)} (pełna lista: `discrepancies.csv`). "
          "W punktacji liczą się dane ze sprawozdań i własne wyliczenia.", ""]
    path.write_text("\n".join(L) + "\n", encoding="utf-8")
