# Screening small-capów USA

Szuka spółek z NYSE, Nasdaq i NYSE American (kapitalizacja 300 mln - 3 mld USD) z potencjałem
na mocny ruch w górę w horyzoncie 1-3 miesięcy. Pracuje wyłącznie na danych pobranych w dniu
uruchomienia. Każda liczba trafia do `sources.csv` ze źródłem (URL) i datą danych.

## Wymagany dostęp do sieci

Pipeline pobiera dane z tych hostów. Wszystkie muszą być dozwolone w polityce sieci środowiska:

| Host | Do czego |
|---|---|
| `api.nasdaq.com` | uniwersum spółek (screener), short interest, daty wyników |
| `www.sec.gov`, `data.sec.gov`, `efts.sec.gov` | XBRL (10-Q/10-K), lista zgłoszeń, Form 4, wyszukiwanie pełnotekstowe |
| `query1.finance.yahoo.com` | notowania dzienne (SMA50, 52-tyg. szczyt, beta, obroty) |
| `finviz.com` | short float, short ratio, beta (porównanie) |

SEC wymaga nagłówka User-Agent z danymi kontaktowymi:

```bash
export SEC_USER_AGENT="Imię Nazwisko email@domena"
```

## Uruchomienie

```bash
pip install -r requirements.txt
python -m screener.run                      # pełne uniwersum, wyniki w output/
python -m screener.run --tickers AAA,BBB    # wybrane spółki
python -m screener.run --offline            # ponownie z cache (data/cache)
python -m unittest discover -s tests -t .   # testy offline
```

## Etapy

1. **Uniwersum** (Nasdaq screener, lista tickerów SEC). Odpadają: warranty, jednostki, prawa,
   akcje uprzywilejowane, SPAC-i (nazwa lub SIC 6770), REIT-y (branża lub SIC 6798), spółki z
   Chin, Hongkongu i Makau (kraj w Nasdaq lub siedziba w EDGAR), emitenci bez 10-Q/10-K
   (20-F/40-F nie dają kwartalnych danych XBRL, więc filtra F2 nie da się potwierdzić).
2. **Fundamenty z SEC XBRL.** Przychody kwartalne r/r (Q4 = rok - 9 miesięcy) i FCF TTM = CFO - capex.
   Przepływy w 10-Q są narastające, więc TTM = rok obrotowy + bieżący YTD - YTD rok wcześniej.
   Dalej: gotówka, dług finansowy (bez leasingu operacyjnego), EBITDA TTM = EBIT + D&A, liczba akcji
   z okładki raportu, pozycje jednorazowe. Spółki przed przychodami i z danymi starszymi niż 200 dni odpadają.
3. **Kurs i technika** (Yahoo, 2 lata dziennie). Kapitalizacja = akcje z raportu SEC x kurs.
   Średni obrót z 50 sesji, beta z tygodniowych stóp zwrotu z 2 lat względem SPY,
   odległość od 52-tygodniowego szczytu, SMA50.
4. **EDGAR i rynek** (tylko spółki po filtrach F1-F5):
   - rozwodnienie: S-3/F-3 z 3 lat (shelf jest ważny 3 lata), 424B* i S-1 z 6 miesięcy, wzmianki o ATM;
   - delisting: 8-K z pozycją 3.01 z 12 miesięcy;
   - going concern: obie frazy, "substantial doubt" i "going concern", w ostatnim 10-Q/10-K;
   - sprawy prawne: "securities class action", "Wells notice", "Division of Enforcement" w 10-Q/10-K/8-K z 12 miesięcy;
   - insiderzy: Form 4 z 90 dni, kod P (zakup) i S (sprzedaż);
   - short float (Finviz) i days to cover (Nasdaq, z datą rozliczenia);
   - data wyników (Nasdaq, awaryjnie Finviz).
5. **Punktacja 0-12 i kary** zgodnie ze specyfikacją (`screener/scoring.py`).

Definicje techniczne:
- *SMA50 zaczyna rosnąć*: SMA50 wyżej niż 5 sesji temu, a w ciągu wcześniejszych 60 sesji spadała.
- *Wyższy dołek*: ostatni potwierdzony dołek wyżej od poprzedniego. Na wykresie dziennym dołek
  potwierdza 5 świec z każdej strony, na tygodniowym 2.
- *Wolumen na wybiciu*: w ostatnich 10 sesjach zamknięcie powyżej maksimum z poprzednich 63 sesji,
  przy wolumenie co najmniej 1,5x średniej z 50 sesji.

## Weryfikacja ręczna (top 15)

Część kryteriów wymaga lektury dokumentów: klient powyżej 30% przychodów, charakter
jednorazówek, raporty short sellerów, kontrakty, decyzje FDA, wejście do indeksu. Do tego
dochodzą fałszywe trafienia wyszukiwania pełnotekstowego. Wyniki weryfikacji wpisuje się do
`overrides.csv`:

```csv
ticker,metric,value,source_url,as_of,note
ABC,rev_growth,0.124,https://www.sec.gov/Archives/...,2026-06-30,10-Q str. 5
ABC,top_customer_share,0.34,https://www.sec.gov/Archives/...,2025-12-31,10-K nota 2
ABC,legal,false,https://www.sec.gov/Archives/...,2026-08-05,pozew konsumencki, nie z tytułu papierów
ABC,catalyst,Kontrakt z DoD 120 mln USD,https://...,2026-09-12,8-K
```

Wartości z raportu wygrywają ze screenerem. Każda różnica trafia do `discrepancies.csv`.
Obsługiwane metryki: dowolny klucz liczbowy (`rev_growth`, `fcf_ttm`, `short_float`,
`days_to_cover`, `top_customer_share`, `insider_sell_usd`, ...), flagi `one_off`, `dilution`,
`legal`, `going_concern`, `delisting`, `net_cash` oraz `catalyst`. Każdy wiersz `catalyst`
dodaje jeden katalizator.

## Wyniki (`output/`)

- `results.csv`: wszystkie spółki po filtrach twardych, posortowane według wyniku.
- `raport.md`: lejek, top 15 i tabela źródeł dla każdej spółki.
- `sources.csv`: każda liczba z URL-em źródła, datą danych i datą pobrania.
- `excluded.csv`: spółki odrzucone, z etapem i powodem.
- `discrepancies.csv`: rozbieżności screener vs raport.

## Ograniczenia

- Short float pochodzi z Finviz, który nie podaje daty rozliczenia. Days to cover z Nasdaq ma datę.
- Pozycje jednorazowe wykrywane są po tagach XBRL. Zwroty ceł nie mają osobnego tagu, więc trzeba je sprawdzić ręcznie.
- 8-K 3.01 obejmuje też dobrowolne przeniesienie notowań, a frazy prawne mogą dotyczyć sporów
  niezwiązanych z papierami wartościowymi. Takie trafienia trafiają do kolumny `do_weryfikacji`.
- Wyniki są co kwartał prawie u każdej spółki, więc w praktyce ten katalizator dostaje prawie każdy.
  O drugim punkcie decydują zakupy insiderów i katalizatory dopisane ręcznie.
