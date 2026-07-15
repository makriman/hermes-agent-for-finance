"""Line Items — the canonical forecast unit, detected from actuals.

For every (category, counterparty) with enough history we classify its cadence:

  regular_monthly   ~1 event/month, stable day-of-month, stable amount
                    -> project the median amount on its anchor day (high conf)
  variable_monthly  recurs most months but noisy in amount/day
                    -> project the mean monthly total on the median day
  residual          per-category remainder so line items sum to the category
                    run-rate -> anchored mid-month (low conf)

Lumpy/excluded categories (dividends, capex, transfers) never become line
items — they are surfaced on the watch-list instead.
"""
from __future__ import annotations

import statistics as stats
from collections import defaultdict
from datetime import date

from . import taxonomy as tax
from .mapping import norm
from .models import EnrichedTxn, LineItem

MIN_OBS = 3           # min events to consider a counterparty recurring
BASELINE_MONTHS = 3   # months used for the category run-rate


def observed_months(enriched: list[EnrichedTxn], before_month: str) -> list[str]:
    """Calendar months with data before `before_month`, excluding a hollow
    trailing month: one with far fewer transactions than typical is a
    mid-month data cut (feed gap, statement boundary), not a real collapse —
    letting it into run-rates and trends would crash every forecast."""
    from collections import Counter
    counts = Counter(t.date.strftime("%Y-%m") for t in enriched
                     if t.date.strftime("%Y-%m") < before_month)
    months = sorted(counts)
    if len(months) >= 3:
        recent = [counts[m] for m in months[-7:-1]]   # the 6 before the last
        med = stats.median(recent)
        if counts[months[-1]] < 0.4 * med:
            months = months[:-1]
    return months


def _median_day(days: list[int]) -> int:
    return min(int(stats.median(days)), 28)   # clamp so it exists every month


def _cv(amounts: list[float]) -> float:
    m = stats.mean(amounts)
    return round(stats.pstdev(amounts) / abs(m), 3) if m else 0.0


def detect(enriched: list[EnrichedTxn], before_month: str,
           history_months: int = 6,
           residual_floor: float = 500.0
           ) -> tuple[list[LineItem], dict[str, float], list[str]]:
    """Return (line_items, category_run_rate, baseline_month_list).

    `before_month` (YYYY-MM) bounds the history; the run-rate uses the last
    BASELINE_MONTHS calendar months, cadence detection the last
    `history_months`."""
    months_all = observed_months(enriched, before_month)
    hist_months = months_all[-history_months:]
    base_months = months_all[-BASELINE_MONTHS:]
    if not base_months:
        return [], {}, []

    # -- category run-rate over the baseline window (forecastable cats only).
    # MEDIAN of the monthly totals, not the mean: one-off spikes (a lump-sum
    # loan payoff, a fit-out bill) must not smear into a recurring forecast.
    # EXCEPTION — financing flows (invoice-finance drawdowns and their
    # repayments) are lumpy but structural: a median of [small, small, huge]
    # hides half the story and breaks the in/out symmetry, so they use the
    # mean.
    cat_month: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for t in enriched:
        ym = t.date.strftime("%Y-%m")
        if ym in base_months and tax.in_operating_forecast(t.category) \
                and t.category != "tax_vat":
            cat_month[t.category][ym] += t.amount
    def _rate(cat: str, m: dict) -> float:
        vals = [m.get(b, 0.0) for b in base_months]
        agg = stats.mean if tax.spec(cat).kind == tax.FINANCING else stats.median
        return round(agg(vals), 2)
    run_rate = {c: _rate(c, m) for c, m in cat_month.items()}

    # -- per-counterparty cadence detection over the history window ----------
    grp: dict[tuple[str, str], list[EnrichedTxn]] = defaultdict(list)
    for t in enriched:
        ym = t.date.strftime("%Y-%m")
        if ym in hist_months and tax.in_operating_forecast(t.category) \
                and t.category != "tax_vat":
            grp[(t.category, norm(t.counterparty))].append(t)

    items: list[LineItem] = []
    projected_per_cat: dict[str, float] = defaultdict(float)
    for (cat, cp), txns in sorted(grp.items()):
        if len(txns) < MIN_OBS or not cp:
            continue
        months_present = {t.date.strftime("%Y-%m") for t in txns}
        # only counterparties active in >= 2 of the last 3 baseline months
        recent_presence = len(months_present & set(base_months))
        if recent_presence < 2:
            continue
        amounts = [t.amount for t in txns]
        days = [t.date.day for t in txns]
        per_month = len(txns) / max(len(months_present), 1)
        cv = _cv(amounts)
        day_spread = stats.pstdev(days) if len(days) > 1 else 0.0
        monthly_total = round(sum(amounts) / len(months_present), 2)

        if 0.75 <= per_month <= 1.5 and cv <= 0.35 and day_spread <= 4:
            kind, conf = "regular_monthly", 0.9
            # median of the LAST 3 events, not all history: a step change
            # (new payroll level, renegotiated rent) converges in one cycle
            # instead of being diluted by the old level for months
            amount = round(stats.median(amounts[-3:]), 2)
            basis = f"{len(txns)} events, ~day {_median_day(days)}, cv={cv}"
        else:
            kind, conf = "variable_monthly", 0.6
            amount = monthly_total
            basis = f"{len(txns)} events over {len(months_present)}mo, cv={cv}"

        display_cp = txns[-1].counterparty
        items.append(LineItem(
            key=f"{cat}|{cp}", category=cat, label=f"{tax.label(cat)} — {display_cp}",
            counterparty=display_cp, kind=kind, day_of_month=_median_day(days),
            monthly_amount=amount, n_observed=len(txns), cv=cv,
            confidence=conf, basis=basis,
        ))
        projected_per_cat[cat] += amount

    # -- residuals: keep each category consistent with its run-rate ----------
    for cat, rate in sorted(run_rate.items()):
        resid = round(rate - projected_per_cat.get(cat, 0.0), 2)
        # A residual that opposes the category's own direction means the named
        # items already exceed the (window-diluted) run-rate — dropping it is
        # honest; letting it through would cancel real, observed line items.
        if rate != 0 and resid * rate < 0:
            continue
        if abs(resid) >= residual_floor:
            items.append(LineItem(
                key=f"{cat}|__residual__", category=cat,
                label=f"{tax.label(cat)} — other/uncategorised",
                counterparty="", kind="residual", day_of_month=15,
                monthly_amount=resid, n_observed=0, cv=0.0, confidence=0.4,
                basis=f"category run-rate {rate} minus named items "
                      f"{round(projected_per_cat.get(cat, 0.0), 2)}",
            ))
    return items, run_rate, base_months


def watch_list(enriched: list[EnrichedTxn], before_month: str,
               threshold: float = 250.0) -> list[dict]:
    """Recent monthly magnitude of the lumpy/excluded categories."""
    months_all = sorted({t.date.strftime("%Y-%m") for t in enriched
                         if t.date.strftime("%Y-%m") < before_month})
    base = set(months_all[-BASELINE_MONTHS:])
    agg: dict[str, float] = defaultdict(float)
    for t in enriched:
        if t.date.strftime("%Y-%m") in base and not tax.is_transfer(t.category) \
                and not tax.in_operating_forecast(t.category):
            agg[t.category] += t.amount
    n = max(len(base), 1)
    return [{"category": c, "label": tax.label(c), "recent_monthly": round(v / n, 2)}
            for c, v in sorted(agg.items(), key=lambda kv: abs(kv[1]), reverse=True)
            if abs(v / n) >= threshold]
