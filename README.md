# Screening small-capów USA

Szuka spółek z NYSE, Nasdaq i NYSE American (kapitalizacja 300 mln - 3 mld USD) z potencjałem
na mocny ruch w górę w horyzoncie 1-3 miesięcy. Pracuje wyłącznie na danych pobranych w dniu
uruchomienia. Każda liczba trafia do `sources.csv` ze źródłem (URL) i datą danych.

## Źródła danych (wymagany dostęp do sieci)

| Host | Do czego |
|---|---|
| `api.nasdaq.com` | uniwersum (screener), short interest z datą rozliczenia, daty wyników, lista zgłoszeń do SEC (ok. 6 mies.), transakcje insiderów |
| `query1.finance.yahoo.com` | notowania dzienne (SMA50, 52-tyg. szczyt, beta, obroty), dane ze sprawozdań (przychody, FCF TTM, dług, gotówka, EBITDA, liczba akcji) |
| `finviz.com` | short float, kontrola krzyżowa (beta, P/FCF, sprzedaż r/r, odległość od szczytu), branża, nagłówki wiadomości |
| `stooq.com` | notowania zapasowo, gdy Yahoo nie odpowiada |

SEC EDGAR nie jest używany (decyzja z 2026-10-08: SEC wymaga w zapytaniach prawdziwego adresu e-mail).
Wersja czytająca bezpośrednio 10-Q/10-K, Form 4 i S-3 jest w historii gita (commit `f239216`).

## Uruchomienie

```bash
pip install -r requirements.txt
python -m screener.run                      # pełne uniwersum (ok. 1700 spółek, ok. 30 min), wyniki w output/
python -m screener.run --offline            # ponownie z cache (data/cache), np. po uzupełnieniu weryfikacji
python -m screener.run --tickers AAA,BBB    # wybrane spółki
python -m unittest discover -s tests -t .   # testy offline
```

## Etapy

1. **Uniwersum** (Nasdaq screener): akcje zwykłe w przedziale kapitalizacji. Bez warrantów, jednostek, akcji
   uprzywilejowanych, SPAC-ów, REIT-ów i spółek z Chin, Hongkongu i Makau.
2. **Kurs i technika** (Yahoo, 2 lata dziennie): obrót > 2 mln USD, beta > 1,2 (2 lata, tygodniowo vs SPY),
   kurs co najmniej 25% pod 52-tyg. szczytem, kurs nad SMA50.
3. **Finanse** (Yahoo fundamentals-timeseries): FCF TTM > 0, przychody ostatniego kwartału r/r > 0,
   przychody > 0, kapitalizacja = liczba akcji ze sprawozdania x kurs.
4. **Szczegóły** (Finviz, Nasdaq): short float, days to cover, data wyników, ślady rozwodnienia (S-3/424B/S-1,
   wzrost liczby akcji, wiadomości o emisji/ATM), ostrzeżenia z nagłówków (delisting, going concern, pozwy,
   short sellerzy, SEC), insiderzy z 90 dni, kontrola krzyżowa z Finviz.
5. **Punktacja 0-12 i kary** (`screener/scoring.py`), samokontrola TOP 10 (odległość od szczytu, beta, ATM)
   i raport (`screener/report.py`).

## Weryfikacja ręczna

Wyniki weryfikacji (wyszukiwanie w sieci, komunikaty spółek) wpisuje się do `overrides.csv`:

```csv
ticker,metric,value,source_url,as_of,note
ABC,top_customer_share,0.34,https://...,2025-12-31,10-K: największy klient
ABC,atm,true,https://...,2026-03-01,umowa ATM z bankiem X
ABC,catalyst,Decyzja FDA (PDUFA) 2026-12-15,https://...,2026-09-01,
ABC,exclude,raport short sellera z 2026-09,https://...,2026-09-12,
```

Teza, ryzyko i katalizator dla TOP 10 są w `analysis/notes.json`. Raport generuje się z tych plików
i danych w cache, więc `--offline` odtwarza go bez ponownego pobierania.

## Wyniki (`output/`)

- `results.csv`: spółki po filtrach twardych z punktacją, posortowane według wyniku.
- `raport.md`: TOP 10 (teza, ryzyko, katalizator, poziom unieważnienia), 5 kandydatów na short squeeze,
  odrzucone w ostatniej chwili, metodologia i ograniczenia.
- `sources.csv`: każda liczba z URL-em źródła, datą danych i datą pobrania.
- `excluded.csv`: spółki odrzucone, z etapem i powodem.
- `discrepancies.csv`: rozbieżności między Finviz a danymi ze sprawozdań/wyliczeniami.
