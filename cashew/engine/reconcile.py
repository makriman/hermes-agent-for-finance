"""Reconciliation — match actuals to the frozen forecast's occurrences.

Honest mid-month semantics: actuals-to-date are compared against what was
EXPECTED to have landed by now (not the whole month), each expected occurrence
is individually matched to real transactions, and every miss is decomposed:

  on_track   matched, right amount, within the date window
  timing     matched, right amount, but landed > TIMING_TOL days off
  amount     matched by counterparty/date, but size is off
  missing    expected window has passed, nothing landed
  pending    not expected yet (future)
  surprise   real cash movement nothing in the forecast explains

Matching also yields per-counterparty lateness observations (the DSO signal)
and plain-English "lessons" to feed the learning loop.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from difflib import SequenceMatcher

from . import taxonomy as tax
from .actuals import build_enriched
from .client import Clients
from .forecast import month_bounds
from .models import EnrichedTxn, ForecastVersion, Occurrence

MATCH_WINDOW_DAYS = 7      # occurrence can settle +/- this many days
TIMING_TOL_DAYS = 2        # matched beyond this = timing variance
AMOUNT_TOL = 0.25          # relative amount tolerance
AMOUNT_TOL_ABS = 250.0     # ... or this absolute tolerance


def _sim(a: str, b: str) -> float:
    a, b = (a or "").lower().strip(), (b or "").lower().strip()
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _amount_ok(expected: float, actual: float) -> bool:
    return abs(actual - expected) <= max(abs(expected) * AMOUNT_TOL, AMOUNT_TOL_ABS)


def _match(occs: list[Occurrence], actuals: list[EnrichedTxn], as_of: date):
    """Greedy 1:1 matching of occurrences to actual transactions."""
    # group actuals per category for cheap candidate lookup
    pool: dict[str, list] = defaultdict(list)
    for t in actuals:
        pool[t.category].append({"t": t, "used": False})

    results = []
    # Two passes: named occurrences claim their transactions FIRST; aggregate
    # occurrences (residuals, counterparty="") only get what's left — otherwise
    # a mid-month residual can swallow month-end payroll's transactions.
    ordered = sorted(occs, key=lambda o: (o.counterparty == "", o.expected_date))
    for o in ordered:
        exp_d = date.fromisoformat(o.expected_date)
        cands = []
        for idx, slot in enumerate(pool.get(o.category, [])):
            if slot["used"]:
                continue
            t = slot["t"]
            dd = abs((t.date - exp_d).days)
            if dd > MATCH_WINDOW_DAYS:
                continue
            cp_sim = _sim(o.counterparty, t.counterparty) if o.counterparty else 0.5
            if o.counterparty and cp_sim < 0.55 and not _amount_ok(o.amount, t.amount):
                continue
            cands.append((dd, -cp_sim, idx, slot))
        # recategorization safety net: a named occurrence whose counterparty
        # was re-coded mid-month now carries a different category in actuals.
        # Match it cross-category by strong counterparty similarity instead of
        # reporting a phantom missing + surprise pair.
        if not cands and o.counterparty:
            for cat_key, slots in pool.items():
                if cat_key == o.category:
                    continue
                for idx, slot in enumerate(slots):
                    if slot["used"]:
                        continue
                    t = slot["t"]
                    dd = abs((t.date - exp_d).days)
                    if dd > MATCH_WINDOW_DAYS:
                        continue
                    if _sim(o.counterparty, t.counterparty) >= 0.75 \
                            and _amount_ok(o.amount, t.amount):
                        cands.append((dd, -1.0, idx, slot))
        # residual/aggregate occurrences may match MANY txns of the category
        if o.source == "lineitem" and o.counterparty == "":
            matched = []
            for dd, _ncp, _idx, slot in sorted(cands):
                slot["used"] = True
                matched.append(slot["t"])
            results.append((o, matched))
            continue
        if cands:
            cands.sort()
            best = cands[0][3]
            best["used"] = True
            results.append((o, [best["t"]]))
        else:
            results.append((o, []))
    leftovers = [slot["t"] for slots in pool.values() for slot in slots if not slot["used"]]
    return results, leftovers


def run(c: Clients, fv: ForecastVersion, as_of: str | None = None,
        owner_rules: list[dict] | None = None,
        materiality: float = 2000.0) -> dict:
    cfg = c.config()
    m_start, m_end = month_bounds(fv.month)
    now_d = date.fromisoformat(cfg["now"])
    as_of_d = date.fromisoformat(as_of) if as_of else min(now_d, m_end)
    # Never look past the provider clock (no future-peeking), never past the
    # month end; a reconcile before the month starts is an empty comparison.
    as_of_d = min(as_of_d, now_d, m_end)

    enriched, total_cash, vat_pot, _mapper = build_enriched(
        c, as_of_d.isoformat(), owner_rules)
    # Like for like: actuals are judged from the day AFTER the plan's anchor —
    # anything earlier is already inside the plan's opening balance. For a
    # healthy baseline (frozen the day before the month) this is just the
    # month start; it only bites for legacy mid-month-frozen plans.
    plan_from = max(m_start,
                    date.fromisoformat(fv.anchor) + timedelta(days=1))
    actuals = [t for t in enriched
               if plan_from <= t.date <= as_of_d and not tax.is_transfer(t.category)]

    matched, leftovers = _match(fv.occurrences, actuals, as_of_d)

    lines, lateness_obs = [], []
    expected_to_date = landed_total = 0.0
    for o, txns in matched:
        exp_d = date.fromisoformat(o.expected_date)
        actual_amt = round(sum(t.amount for t in txns), 2)
        if txns:
            landed_total += actual_amt
            first = min(t.date for t in txns)
            delta_days = (first - exp_d).days
            if o.counterparty and o.source in ("lineitem", "invoice_ar", "invoice_ap"):
                lateness_obs.append({"counterparty": o.counterparty,
                                     "delta_days": delta_days})
            if not _amount_ok(o.amount, actual_amt):
                status = "amount"
            elif abs(delta_days) > TIMING_TOL_DAYS:
                status = "timing"
            else:
                status = "on_track"
            if exp_d <= as_of_d:
                expected_to_date += o.amount
            lines.append({"occ": o.__dict__, "status": status,
                          "actual": actual_amt, "delta": round(actual_amt - o.amount, 2),
                          "delta_days": delta_days})
        else:
            if exp_d + timedelta(days=MATCH_WINDOW_DAYS) < as_of_d:
                status = "missing"
                expected_to_date += o.amount
            elif exp_d <= as_of_d:
                status = "due_now"      # inside the window, watch it
                expected_to_date += o.amount
            else:
                status = "pending"
            lines.append({"occ": o.__dict__, "status": status,
                          "actual": 0.0, "delta": round(-o.amount, 2)
                          if status == "missing" else 0.0,
                          "delta_days": None})

    surprise_floor = max(materiality / 8, 250.0)
    surprises = [{"date": t.date.isoformat(), "category": t.category,
                  "label": tax.label(t.category), "counterparty": t.counterparty,
                  "amount": t.amount, "map_source": t.map_source}
                 for t in sorted(leftovers, key=lambda x: -abs(x.amount))
                 if abs(t.amount) >= surprise_floor]

    actual_net = round(sum(t.amount for t in actuals), 2)
    remaining = round(sum(o.amount for o, txns in matched if not txns
                          and date.fromisoformat(o.expected_date) > as_of_d), 2)
    projected_close = round(total_cash + remaining, 2)
    # Late-but-still-expected money: don't let it silently vanish — a late
    # invoice usually still arrives. Surfaced separately, not booked into the
    # (conservative) projected close.
    late_in = round(sum(l["occ"]["amount"] for l in lines
                        if l["status"] in ("missing", "due_now")
                        and l["occ"]["amount"] > 0), 2)
    late_out = round(sum(l["occ"]["amount"] for l in lines
                         if l["status"] in ("missing", "due_now")
                         and l["occ"]["amount"] < 0), 2)

    # -- lessons for the learning loop ------------------------------------------
    # Each lesson has a stable KEY so re-running a reconcile UPDATES the same
    # lesson (latest figures) instead of stacking near-duplicates. Text is
    # plain English for the owner; `fix` is the machine hint (accountant view /
    # the chat agent's suggested action) — never both in one sentence.
    lessons: list[dict] = []
    vat_paid = [t for t in actuals if t.category == "tax_vat" and t.amount < 0]
    surprise_by_cat: dict[str, float] = defaultdict(float)
    for s in surprises:
        surprise_by_cat[s["category"]] += s["amount"]
    for cat, amt in sorted(surprise_by_cat.items(), key=lambda kv: abs(kv[1]), reverse=True):
        if cat == "tax_vat" and vat_paid:
            continue        # the VAT module owns the VAT narrative below
        if abs(amt) >= 2 * materiality:
            kind = "income" if amt > 0 else "spending"
            direction = "came in" if amt > 0 else "went out"
            lessons.append({"key": f"surprise:{cat}", "text":
                f"£{abs(amt):,.0f} of {tax.label(cat)} {direction} that wasn't in the "
                f"forecast. If this is planned {kind}, say so and it can be added to "
                f"future months (undoable).",
                "fix": f"cashew assume add --category {cat} --amount {amt:.0f} "
                       f"--date <YYYY-MM-DD> --note '...'"})
    if vat_paid and fv.vat.get("underfunded"):
        actual_vat = -sum(t.amount for t in vat_paid)
        gap = round(actual_vat - fv.vat["pot"], 2)
        if gap > 0:
            lessons.append({"key": "vat:pot-gap", "text":
                f"VAT of £{actual_vat:,.0f} was paid with only £{fv.vat['pot']:,.0f} in the "
                f"pot — £{gap:,.0f} came from operating cash. A weekly sweep of "
                f"~£{gap / 13:,.0f} into the pot would close that gap by next quarter.",
                "fix": "increase the VAT pot standing order"})
    big_timing = [l for l in lines if l["status"] == "timing"
                  and abs(l["occ"]["amount"]) >= materiality / 2]
    for l in big_timing[:3]:
        lessons.append({"key": f"timing:{l['occ']['label']}", "text":
            f"{l['occ']['label']}: landed {abs(l['delta_days'])}d "
            f"{'late' if l['delta_days'] > 0 else 'early'} — payment-timing profile updated."})

    return {
        "org": fv.org, "month": fv.month, "as_of": as_of_d.isoformat(),
        "opening_balance": fv.opening_balance,
        "cash_now": total_cash, "vat_pot_now": vat_pot,
        "expected_to_date": round(expected_to_date, 2),
        "actual_net_to_date": actual_net,
        "on_pace_delta": round(actual_net - expected_to_date, 2),
        "forecast_close": fv.forecast_close,
        "projected_close": projected_close,
        "late_inflows": late_in, "late_outflows": late_out,
        "lines": lines, "surprises": surprises,
        "lateness_observations": lateness_obs,
        "lessons": lessons,
    }
