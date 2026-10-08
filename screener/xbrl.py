"""Fundamenty z SEC XBRL (companyfacts): przychody kwartalne, TTM przepływów, dług, gotówka.

10-Q raportuje przepływy pieniężne narastająco (3M/6M/9M), więc TTM liczymy jako
rok obrotowy + bieżący okres narastający - ten sam okres rok wcześniej."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

REVENUE_TAGS = [
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
    "SalesRevenueServicesNet",
    "RevenuesNetOfInterestExpense",
]
OCF_TAGS = [
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
]
CAPEX_TAGS = [
    "PaymentsToAcquirePropertyPlantAndEquipment",
    "PaymentsToAcquireProductiveAssets",
    "PaymentsForCapitalImprovements",
]
# Kapitalizowane oprogramowanie dodajemy tylko do PaymentsToAcquirePropertyPlantAndEquipment;
# PaymentsToAcquireProductiveAssets zwykle już je obejmuje.
SOFTWARE_CAPEX_TAGS = ["PaymentsToDevelopSoftware", "PaymentsForSoftware"]
OPERATING_INCOME_TAGS = ["OperatingIncomeLoss"]
DA_TAGS = [
    "DepreciationDepletionAndAmortization",
    "DepreciationAmortizationAndAccretionNet",
    "DepreciationAndAmortization",
    "Depreciation",
]
NET_INCOME_TAGS = ["NetIncomeLoss", "ProfitLoss"]
CASH_TAGS = [
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    "Cash",
]
SHORT_INVEST_TAGS = [
    "ShortTermInvestments",
    "MarketableSecuritiesCurrent",
    "AvailableForSaleSecuritiesDebtSecuritiesCurrent",
]
DEBT_TOTAL_TAGS = ["LongTermDebt", "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities", "DebtInstrumentCarryingAmount"]
DEBT_NONCURRENT_TAGS = ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations", "ConvertibleNotesPayableNoncurrent", "LongTermLineOfCredit"]
DEBT_CURRENT_TAGS = ["LongTermDebtCurrent", "DebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent", "ConvertibleNotesPayableCurrent"]
SHORT_BORROW_TAGS = ["ShortTermBorrowings", "LinesOfCreditCurrent", "CommercialPaper"]
# Pozycje jednorazowe. Zyski ze sprzedaży aktywów są w CFO korygowane jako niegotówkowe,
# więc nie zawyżają FCF; ugody, odszkodowania i zwroty (np. ceł) przechodzą przez CFO.
ONE_OFF_NONCASH_TAGS = [
    "GainLossOnSaleOfPropertyPlantEquipment",
    "GainLossOnDispositionOfAssets",
    "GainLossOnDispositionOfAssets1",
    "DisposalGroupNotDiscontinuedOperationGainLossOnDisposal",
    "GainLossOnSaleOfOtherAssets",
    "GainsLossesOnExtinguishmentOfDebt",
    "BusinessCombinationBargainPurchaseGainRecognizedAmount",
    "GainLossOnInvestments",
]
ONE_OFF_CASH_TAGS = [
    "GainLossRelatedToLitigationSettlement",
    "InsuranceRecoveries",
    "ProceedsFromInsuranceSettlementOperatingActivities",
    "GainOnBusinessInterruptionInsuranceRecovery",
]
SHARES_TAGS_DEI = ["EntityCommonStockSharesOutstanding"]
SHARES_TAGS_GAAP = ["CommonStockSharesOutstanding", "WeightedAverageNumberOfDilutedSharesOutstanding"]

NEEDED_TAGS = set(
    REVENUE_TAGS + OCF_TAGS + CAPEX_TAGS + SOFTWARE_CAPEX_TAGS + OPERATING_INCOME_TAGS + DA_TAGS
    + ["AmortizationOfIntangibleAssets"] + NET_INCOME_TAGS + CASH_TAGS + SHORT_INVEST_TAGS
    + DEBT_TOTAL_TAGS + DEBT_NONCURRENT_TAGS + DEBT_CURRENT_TAGS + SHORT_BORROW_TAGS
    + ONE_OFF_NONCASH_TAGS + ONE_OFF_CASH_TAGS + SHARES_TAGS_DEI + SHARES_TAGS_GAAP
)


def slim_companyfacts(cf: dict) -> dict:
    """Zostawia tylko potrzebne tagi (pełny companyfacts ma kilka MB)."""
    out = {"cik": cf.get("cik"), "entityName": cf.get("entityName"), "facts": {}}
    for taxonomy in ("us-gaap", "dei"):
        for tag, body in cf.get("facts", {}).get(taxonomy, {}).items():
            if tag in NEEDED_TAGS:
                out["facts"][tag] = body.get("units", {})
    return out


@dataclass(frozen=True)
class Fact:
    tag: str
    start: date | None
    end: date
    val: float
    accn: str
    form: str
    filed: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1 if self.start else 0


@dataclass(frozen=True)
class Value:
    """Wartość wyliczona z jednego lub kilku faktów XBRL (proweniencja w `parts`)."""
    val: float
    end: date
    parts: tuple[Fact, ...]

    @property
    def accns(self) -> list[str]:
        return sorted({p.accn for p in self.parts})


def _d(s: str) -> date:
    return date.fromisoformat(s)


def facts_for(slim: dict, tag: str, unit: str = "USD") -> list[Fact]:
    """Fakty dla tagu; przy wielu wartościach dla tego samego okresu bierze najpóźniej złożoną
    (uwzględnia korekty wcześniejszych okresów)."""
    raw = slim.get("facts", {}).get(tag, {}).get(unit, [])
    best: dict[tuple, Fact] = {}
    for r in raw:
        if "end" not in r or "val" not in r:
            continue
        f = Fact(tag, _d(r["start"]) if r.get("start") else None, _d(r["end"]), float(r["val"]),
                 r.get("accn", ""), r.get("form", ""), _d(r["filed"]) if r.get("filed") else date.min)
        key = (f.start, f.end)
        if key not in best or f.filed > best[key].filed:
            best[key] = f
    return sorted(best.values(), key=lambda f: (f.end, f.days))


def _is_annual(f: Fact) -> bool:
    return f.start is not None and 350 <= f.days <= 380


def _near(a: date, b: date, tol: int) -> bool:
    return abs((a - b).days) <= tol


def ttm_points(facts: list[Fact]) -> dict[date, Value]:
    """TTM dla każdej daty końca okresu, dla której da się go policzyć."""
    dur = [f for f in facts if f.start is not None]
    annual = [f for f in dur if _is_annual(f)]
    by_end: dict[date, list[Fact]] = defaultdict(list)
    for f in dur:
        by_end[f.end].append(f)
    out: dict[date, Value] = {}
    for end, fs in by_end.items():
        a = [f for f in fs if _is_annual(f)]
        if a:
            out[end] = Value(a[0].val, end, (a[0],))
            continue
        partial = [f for f in fs if 80 <= f.days < 350]
        if not partial:
            continue
        ytd = max(partial, key=lambda f: f.days)
        prior = [f for f in dur if _near(f.end, end - timedelta(days=365), 10)
                 and abs(f.days - ytd.days) <= 10]
        fy = [f for f in annual if _near(f.end, ytd.start - timedelta(days=1), 10)]
        if prior and fy:
            p = min(prior, key=lambda f: abs(f.days - ytd.days))
            out[end] = Value(fy[-1].val + ytd.val - p.val, end, (fy[-1], ytd, p))
    return out


def quarterly_points(facts: list[Fact]) -> dict[date, Value]:
    """Wartości kwartalne; brakujący Q4 wyliczany jako rok - 9 miesięcy."""
    dur = [f for f in facts if f.start is not None]
    q: dict[date, Value] = {}
    for f in dur:
        if 80 <= f.days <= 100:
            cur = q.get(f.end)
            if cur is None or abs(f.days - 91) < abs(cur.parts[0].days - 91):
                q[f.end] = Value(f.val, f.end, (f,))
    for a in (f for f in dur if _is_annual(f)):
        if a.end in q:
            continue
        nine = [f for f in dur if _near(f.start, a.start, 5) and 255 <= f.days <= 290 and f.end < a.end]
        if nine:
            n = max(nine, key=lambda f: f.end)
            q[a.end] = Value(a.val - n.val, a.end, (a, n))
    return q


def merged(slim: dict, tags: list[str], fn) -> dict[date, Value]:
    """Łączy punkty z kilku tagów: dla każdej daty pierwszeństwo ma tag wyżej na liście."""
    out: dict[date, Value] = {}
    for tag in tags:
        for end, v in fn(facts_for(slim, tag)).items():
            out.setdefault(end, v)
    return out


def revenue_quarters(slim: dict) -> dict[date, Value]:
    """Kwartalne przychody. Spółki często tagują jednocześnie sumę i składnik przychodów,
    więc dla każdego kwartału bierzemy największą wartość spośród tagów przychodowych."""
    out: dict[date, Value] = {}
    for tag in REVENUE_TAGS:
        for end, v in quarterly_points(facts_for(slim, tag)).items():
            if end not in out or v.val > out[end].val:
                out[end] = v
    return out


def yoy(points: dict[date, Value], tol: int = 10) -> tuple[Value, Value] | None:
    """Ostatni punkt i punkt sprzed roku."""
    if not points:
        return None
    end = max(points)
    target = end - timedelta(days=365)
    prior = [e for e in points if _near(e, target, tol)]
    if not prior:
        return None
    return points[end], points[min(prior, key=lambda e: abs((e - target).days))]


def capex_ttm(slim: dict) -> dict[date, Value]:
    out: dict[date, Value] = {}
    for tag in CAPEX_TAGS:
        for end, v in ttm_points(facts_for(slim, tag)).items():
            if end in out:
                continue
            if tag == "PaymentsToAcquirePropertyPlantAndEquipment":
                for sw in SOFTWARE_CAPEX_TAGS:
                    extra = ttm_points(facts_for(slim, sw)).get(end)
                    if extra is not None:
                        v = Value(v.val + extra.val, end, v.parts + extra.parts)
            out[end] = v
    return out


def fcf_ttm(slim: dict) -> tuple[dict[date, Value], bool]:
    """FCF = CFO - capex (TTM). Drugi element: czy capex znaleziono w XBRL."""
    ocf = merged(slim, OCF_TAGS, ttm_points)
    capex = capex_ttm(slim)
    out = {}
    for end, o in ocf.items():
        c = capex.get(end)
        if c is None:
            continue
        out[end] = Value(o.val - c.val, end, o.parts + c.parts)
    if out:
        return out, True
    # Brak capexu w XBRL (spółki asset-light): FCF = CFO, do ręcznej weryfikacji.
    return ocf, False


def ttm_at(slim: dict, tags: list[str], end: date, tol: int = 5) -> Value | None:
    pts = merged(slim, tags, ttm_points)
    match = [e for e in pts if _near(e, end, tol)]
    return pts[match[0]] if match else None


def instant_at(slim: dict, tag: str, when: date, unit: str = "USD") -> Fact | None:
    for f in facts_for(slim, tag, unit):
        if f.start is None and f.end == when:
            return f
    return None


def latest_instant_date(slim: dict, tags: list[str]) -> date | None:
    ends = [f.end for t in tags for f in facts_for(slim, t) if f.start is None]
    return max(ends) if ends else None


def _first_instant(slim: dict, tags: list[str], when: date) -> Fact | None:
    for t in tags:
        f = instant_at(slim, t, when)
        if f is not None:
            return f
    return None


def cash_and_debt(slim: dict) -> dict | None:
    """Gotówka (z krótkoterminowymi inwestycjami) i dług finansowy na ostatni dzień bilansowy.
    Leasing operacyjny pomijamy."""
    when = latest_instant_date(slim, CASH_TAGS)
    if when is None:
        return None
    cash_f = _first_instant(slim, CASH_TAGS, when)
    inv_f = _first_instant(slim, SHORT_INVEST_TAGS, when)
    parts = [p for p in (cash_f, inv_f) if p]
    cash = sum(p.val for p in parts)

    debt_parts: list[Fact] = []
    total = _first_instant(slim, DEBT_TOTAL_TAGS, when)
    if total is not None:
        debt_parts.append(total)
    else:
        for tags in (DEBT_NONCURRENT_TAGS, DEBT_CURRENT_TAGS):
            f = _first_instant(slim, tags, when)
            if f is not None:
                debt_parts.append(f)
    sb = _first_instant(slim, SHORT_BORROW_TAGS, when)
    if sb is not None:
        debt_parts.append(sb)
    debt = sum(p.val for p in debt_parts)
    return {"date": when, "cash": cash, "debt": debt, "cash_parts": parts, "debt_parts": debt_parts,
            "debt_found": bool(debt_parts)}


def shares_outstanding(slim: dict) -> Fact | None:
    for tag in SHARES_TAGS_DEI + SHARES_TAGS_GAAP:
        fs = facts_for(slim, tag, "shares")
        if tag == "WeightedAverageNumberOfDilutedSharesOutstanding":
            fs = [f for f in fs if f.start and 80 <= f.days <= 100]
        else:
            fs = [f for f in fs if f.start is None]
        if fs:
            latest = max(f.end for f in fs)
            same = [f for f in fs if f.end == latest]
            if len(same) == 1:
                return same[0]
            # Kilka wartości na ten sam dzień bez wymiaru (klasy akcji): sumujemy.
            return Fact(tag, None, latest, sum(f.val for f in same), same[0].accn, same[0].form,
                        max(f.filed for f in same))
    return None


def ebitda_ttm(slim: dict, end: date) -> Value | None:
    op = ttm_at(slim, OPERATING_INCOME_TAGS, end)
    if op is None:
        return None
    da = ttm_at(slim, DA_TAGS, end)
    if da is None:
        dep = ttm_at(slim, ["Depreciation"], end)
        amort = ttm_at(slim, ["AmortizationOfIntangibleAssets"], end)
        parts = [p for p in (dep, amort) if p]
        if not parts:
            return None
        da = Value(sum(p.val for p in parts), end, tuple(f for p in parts for f in p.parts))
    return Value(op.val + da.val, end, op.parts + da.parts)


def one_offs_ttm(slim: dict, end: date) -> dict:
    """Dodatnie (zyskowe) pozycje jednorazowe TTM, rozdzielone na gotówkowe i niegotówkowe."""
    def collect(tags):
        found = []
        for t in tags:
            v = ttm_at(slim, [t], end)
            if v is not None and v.val > 0:
                found.append((t, v))
        return found
    return {"noncash": collect(ONE_OFF_NONCASH_TAGS), "cash": collect(ONE_OFF_CASH_TAGS)}
