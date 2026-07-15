"""Forecast modules — the pieces compute.py assembles into the single
forecast path: open-invoice occurrences (AR/AP with learned lateness) and the
dedicated tax modules (VAT, corporation tax — PRD: taxes are their own
prediction logic, not run-rate extrapolations).

Everything is deterministic given (data, params).
"""
from __future__ import annotations

import calendar
import hashlib
from collections import defaultdict
from datetime import date, timedelta

from . import taxonomy as tax
from .client import Clients
from .mapping import norm
from .models import Occurrence


def month_bounds(month: str) -> tuple[date, date]:
    y, m = map(int, month.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def add_months(d: date, n: int) -> date:
    y, m = d.year, d.month + n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def _occ_id(*parts) -> str:
    return hashlib.md5(":".join(str(p) for p in parts).encode()).hexdigest()[:12]


def _clamp_day(month: str, day: int) -> date:
    y, m = map(int, month.split("-"))
    return date(y, m, min(day, calendar.monthrange(y, m)[1]))


# --- invoice-based occurrences -------------------------------------------------

def _contact_lateness(c: Clients) -> dict[str, float]:
    """Mean payment lateness (days) per contact, from PAID invoices —
    FullyPaidOnDate vs DueDate. This is the learned DSO signal."""
    acc: dict[str, list[int]] = defaultdict(list)
    for inv in c.invoices():
        if inv.get("Status") == "PAID" and inv.get("FullyPaidOnDate"):
            due = date.fromisoformat(inv["DueDateString"][:10])
            paid = date.fromisoformat(inv["FullyPaidOnDate"][:10])
            acc[norm(inv["Contact"]["Name"])].append((paid - due).days)
    return {cp: sum(v) / len(v) for cp, v in acc.items() if v}


def invoice_occurrences(c: Clients, month: str, mapper,
                        lateness: dict[str, float],
                        include_paid_since: str | None = None) -> list[Occurrence]:
    """Open (AUTHORISED) invoices whose expected settlement falls in the month.
    Expected date = due date + that contact's learned mean lateness.

    `include_paid_since` (ISO date) supports HISTORICAL vantage points
    (retro-frozen baselines): invoices that already settled on/after that
    date were still open at the vantage point and belong in the plan —
    otherwise everything that settled since would masquerade as a surprise."""
    m_start, m_end = month_bounds(month)
    out: list[Occurrence] = []
    for inv in c.invoices():
        status = inv.get("Status")
        recently_settled = (
            include_paid_since and status == "PAID"
            and (inv.get("FullyPaidOnDate") or "")[:10] >= include_paid_since)
        if status != "AUTHORISED" and not recently_settled:
            continue
        contact = inv["Contact"]["Name"]
        due = date.fromisoformat(inv["DueDateString"][:10])
        expected = due + timedelta(days=round(lateness.get(norm(contact), 0)))
        if expected < m_start:
            expected = m_start          # overdue -> expected imminently
        if expected > m_end:
            continue
        is_ar = inv["Type"] == "ACCREC"
        # classify via the org's own learned mapping; generic fallback
        cat, _src = mapper.classify(contact, inv["LineItems"][0].get("Description", ""))
        if cat == "unmapped":
            cat = "revenue" if is_ar else "suppliers_cogs"
        gross = inv["AmountDue"] if status == "AUTHORISED" else \
            (inv.get("AmountPaid") or inv.get("Total") or 0.0)
        amount = gross if is_ar else -gross
        out.append(Occurrence(
            occ_id=_occ_id("inv", inv["InvoiceID"]),
            source="invoice_ar" if is_ar else "invoice_ap",
            category=cat, label=f"{tax.label(cat)} — {contact} ({inv['InvoiceNumber']})",
            counterparty=contact, expected_date=expected.isoformat(),
            amount=round(amount, 2), confidence=0.85,
        ))
    return out


# --- VAT module ------------------------------------------------------------------
# UK quarterly VAT: return period ends at a month end; payment due ~1 month +
# 7 days later. The liability estimate is ACCRUAL-FIRST: gross x 1/6 over the
# ledger's OUTPUT2/INPUT2 tax codes for the current period, extrapolated to a
# full quarter — with the largest historical payment as fallback only when the
# accrual signal is too thin to extrapolate honestly.

VAT_LOOKBACK_DAYS = 400
MIN_ACCRUAL_FRACTION = 0.25   # need >= this much of the quarter booked to extrapolate


def vat_module(enriched, months: list[str], anchor: date,
               vat_pot: float) -> tuple[list[Occurrence], dict]:
    lo = anchor - timedelta(days=VAT_LOOKBACK_DAYS)
    pays = [(t.date, abs(t.amount)) for t in enriched
            if t.category == "tax_vat" and t.amount < 0 and lo <= t.date <= anchor]
    vat = {"due_estimate": 0.0, "accrued_to_date": 0.0, "pot": vat_pot,
           "shortfall": 0.0, "underfunded": False, "projected_in_month": False,
           "projected_dates": [], "next_due": None, "basis": "no VAT history"}
    occs: list[Occurrence] = []

    # ---- period boundaries from the last payment (stagger-aware) -------------
    if pays:
        last_pay_date = max(d for d, _ in pays)
        last_pay_amount = max(a for d, a in pays if d == last_pay_date)
        # payment lands ~1m7d after the period end it settles
        period_end_prev = month_end(add_months(last_pay_date - timedelta(days=7), -1))
        if period_end_prev >= last_pay_date:   # early payer edge case
            period_end_prev = month_end(add_months(last_pay_date, -2))
        period_start = period_end_prev + timedelta(days=1)
    else:
        last_pay_date = last_pay_amount = None
        period_start = month_end(add_months(anchor, -4)) + timedelta(days=1)
    period_end = month_end(add_months(period_start, 2))
    next_due = min(period_end + timedelta(days=38),
                   month_end(add_months(period_end, 1)) + timedelta(days=7))

    # ---- accrue the current period from the ledger's tax codes ---------------
    booked = [t for t in enriched if t.date >= period_start and t.tax_type]
    out_vat = sum(t.amount for t in booked if t.tax_type == "OUTPUT2" and t.amount > 0) / 6
    in_vat = sum(-t.amount for t in booked if t.tax_type == "INPUT2" and t.amount < 0) / 6
    accrued = round(max(out_vat - in_vat, 0.0), 2)
    accrual_end = max((t.date for t in booked), default=period_start)
    total_days = (period_end - period_start).days + 1
    elapsed = max(min((accrual_end - period_start).days + 1, total_days), 0)
    frac = elapsed / total_days

    # Two independent estimates, take the more cautious (higher): the accrual
    # catches a growing business before its next bill does; payment history
    # catches ledgers whose tax coding is too thin to trust.
    est_accrual = round(accrued / frac, 2) \
        if accrued > 0 and frac >= MIN_ACCRUAL_FRACTION else 0.0
    est_pays = round(max(a for _, a in pays), 2) if pays else 0.0
    if est_accrual > est_pays:
        est, basis = est_accrual, (
            f"accrual: £{accrued:,.0f} VAT accrued over {elapsed}d of the "
            f"{period_start} → {period_end} period, scaled to the full quarter "
            f"(above the largest recent payment £{est_pays:,.0f} — sales growing)")
    elif est_pays > 0:
        est, basis = est_pays, (
            f"largest of {len(pays)} payments in {VAT_LOOKBACK_DAYS}d; "
            f"last paid {last_pay_date}"
            + (f"; ledger accrual cross-check £{accrued:,.0f} to date"
               if accrued else ""))
    else:
        est, basis = est_accrual, "accrual only — no VAT payment history yet"
    if est <= 0:
        return occs, vat

    vat.update({
        "due_estimate": est, "accrued_to_date": accrued,
        "shortfall": round(est - vat_pot, 2), "underfunded": est - vat_pot > 0,
        "next_due": next_due.isoformat(), "basis": basis,
        "period": {"start": period_start.isoformat(), "end": period_end.isoformat()},
    })

    # ---- settled? a payment that already landed this month closes the story --
    paid = [(t.date, abs(t.amount)) for t in enriched
            if t.category == "tax_vat" and t.amount < 0
            and t.date.strftime("%Y-%m") == months[0]]
    if paid:
        pd, pa = max(paid)
        vat["paid_this_month"] = {"date": pd.isoformat(), "amount": round(pa, 2)}
        vat["underfunded"] = False
        vat["shortfall"] = 0.0

    # ---- project the quarterly cadence across the horizon --------------------
    due = next_due
    dates = []
    while due.strftime("%Y-%m") <= months[-1]:
        if due.strftime("%Y-%m") >= months[0] and due > anchor:
            occs.append(Occurrence(
                occ_id=_occ_id("vat", due.isoformat()), source="vat",
                category="tax_vat", label="VAT — HMRC (quarterly, estimated)",
                counterparty="HMRC", expected_date=due.isoformat(),
                amount=-est, confidence=0.5))
            dates.append(due.isoformat())
        nxt_end = month_end(add_months(period_end, 3))
        due = month_end(add_months(nxt_end, 1)) + timedelta(days=7)
        period_end = nxt_end
    vat["projected_dates"] = dates
    vat["projected_in_month"] = bool(dates and dates[0][:7] == months[0])
    return occs, vat


# --- corporation tax module -------------------------------------------------------
# Annual: due 9 months + 1 day after the accounting year end. With only cash
# data, the honest deterministic estimate is last year's payment on last
# year's cadence — surfaced early so it never lands as a surprise.

def corp_tax_module(enriched, months: list[str],
                    anchor: date) -> tuple[list[Occurrence], dict]:
    pays = [(t.date, abs(t.amount)) for t in enriched
            if t.category == "tax_corp" and t.amount < 0 and t.date <= anchor]
    ct = {"due_estimate": 0.0, "next_due": None, "last_paid": None,
          "basis": "no corporation tax history"}
    occs: list[Occurrence] = []
    if not pays:
        return occs, ct
    last_d = max(d for d, _ in pays)
    last_a = max(a for d, a in pays if d == last_d)
    next_due = add_months(last_d, 12)
    while next_due <= anchor:
        next_due = add_months(next_due, 12)
    ct.update({
        "due_estimate": round(last_a, 2), "next_due": next_due.isoformat(),
        "last_paid": {"date": last_d.isoformat(), "amount": round(last_a, 2)},
        "basis": f"last year's payment (£{last_a:,.0f} on {last_d})",
    })
    if months[0] <= next_due.strftime("%Y-%m") <= months[-1]:
        occs.append(Occurrence(
            occ_id=_occ_id("ct", next_due.isoformat()), source="corp_tax",
            category="tax_corp", label="Corporation tax — HMRC (annual, estimated)",
            counterparty="HMRC", expected_date=next_due.isoformat(),
            amount=-round(last_a, 2), confidence=0.5))
    return occs, ct
