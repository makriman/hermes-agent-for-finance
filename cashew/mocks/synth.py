"""Synthesis of everything the reconciled CSVs do NOT contain but a real
forecasting engine needs: opening balances, the VAT pot, forward AR/AP
commitments (with realistic payment-timing lateness), and a machine-readable
description of each scenario's signals for the test oracle.

All synthesis is deterministic given the seed, so runs are reproducible.
"""
from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from datetime import date, timedelta

from .loader import _sid
from .models import BankTxn, Invoice, XeroTxn

# Cash categories that, in real life, arrive as invoices/bills (AR/AP).
AR_CATEGORIES = {"revenue"}
AP_CATEGORIES = {
    "suppliers_cogs", "rent", "subscription_saas", "marketing_advertising",
    "professional_services", "insurance", "office_supplies",
    "repairs_maintenance", "travel", "utilities",
}
# Recurring/scheduled categories the engine should forecast from patterns, not invoices.
SCHEDULED_CATEGORIES = {"payroll", "tax_vat", "tax_corp", "loan_repayment", "pension"}
# Lumpy / one-off categories that are legitimately hard to forecast (surprises).
LUMPY_CATEGORIES = {"directors_drawings", "capital_expenditure", "directors_contributions"}

_TERMS_DAYS = {"revenue": 14}          # AR default terms; AP defaults to 30
_DEFAULT_TERMS = 30


def _h(*parts) -> int:
    return int(hashlib.md5(":".join(str(p) for p in parts).encode()).hexdigest(), 16)


def counterparty_lateness(counterparty: str, seed: int) -> int:
    """Deterministic per-counterparty payment lateness in days.

    ~40% pay on time (0); the rest are 1-11 days late. This makes actual
    settlement drift from due date so reconciliation has timing variance to
    detect and learn (per-counterparty DSO)."""
    h = _h(seed, "late", counterparty)
    if h % 100 < 40:
        return 0
    return (h % 11) + 1


def is_vat_pot(account_name: str) -> bool:
    n = account_name.lower()
    return "vat" in n and ("pot" in n or "reserve" in n)


def account_latest_balances(bank_txns: list[BankTxn], as_of: date) -> dict[str, float]:
    """Latest running balance per account as-of a date (each account keeps its
    own running balance in the CSV, so they must be summed, not mixed)."""
    bal: dict[str, float] = {}
    for t in bank_txns:
        if t.date <= as_of:
            bal[t.account_name] = t.balance
    return bal


def cash_position(bank_txns: list[BankTxn], as_of: date) -> dict:
    """Split total cash into operating vs ring-fenced VAT-pot accounts."""
    bals = account_latest_balances(bank_txns, as_of)
    pot = round(sum(v for k, v in bals.items() if is_vat_pot(k)), 2)
    operating = round(sum(v for k, v in bals.items() if not is_vat_pot(k)), 2)
    return {
        "operating": operating,
        "vat_pot": pot,
        "total": round(operating + pot, 2),
        "has_pot_account": any(is_vat_pot(k) for k in bals),
        "by_account": {k: round(v, 2) for k, v in bals.items()},
    }


def opening_balance(bank_txns: list[BankTxn], as_of: date) -> float:
    """Total cash across all accounts as-of a date."""
    return cash_position(bank_txns, as_of)["total"]


def next_vat_due(bank_txns: list[BankTxn], anchor: date) -> tuple[date | None, float]:
    """Soonest VAT payment strictly after the anchor (the liability the owner
    must fund). Falls back to the largest VAT outflow if none is future."""
    future = [t for t in bank_txns if t.cashew_category == "tax_vat" and t.amount < 0 and t.date > anchor]
    if future:
        t = min(future, key=lambda x: x.date)
        return t.date, abs(t.amount)
    allvat = [t for t in bank_txns if t.cashew_category == "tax_vat" and t.amount < 0]
    if allvat:
        t = max(allvat, key=lambda x: abs(x.amount))
        return t.date, abs(t.amount)
    return None, 0.0


# --- corporation tax -----------------------------------------------------------
# The CSVs contain VAT and P11D payments to HMRC but no corporation tax. One
# historical CT payment is synthesised per org: the notional accounting year
# ends 2025-04-30, so payment falls due 9 months + 1 day later (2026-02-01) —
# comfortably inside the historical window (CSVs cover 2024-03 .. 2026-07).
CORP_TAX_FYE = date(2025, 4, 30)          # notional financial year end
CORP_TAX_PAY_DATE = date(2026, 2, 1)      # FYE + 9 months + 1 day
CORP_TAX_COUNTERPARTY = "HMRC Corporation Tax"
CORP_TAX_GL = ("830", "Corporation Tax", "NONE")   # (code, name, tax_type)


def main_operating_account(bank_txns: list[BankTxn]) -> str:
    """The busiest non-pot account (the org's operating current account)."""
    counts = Counter(t.account_name for t in bank_txns if not is_vat_pot(t.account_name))
    return counts.most_common(1)[0][0] if counts else bank_txns[0].account_name


def find_corp_tax_payments(bank_txns: list[BankTxn]) -> list[BankTxn]:
    """Corporation-tax payments already present in the data (there are none in
    the shipped CSVs, but we always check rather than assume)."""
    return [t for t in bank_txns if t.amount < 0
            and "corporation tax" in f"{t.description} {t.counterparty}".lower()]


def corp_tax_amount(slug: str, bank_txns: list[BankTxn], seed: int) -> float:
    """Deterministic per-org CT charge: 19% (small-profits rate) of a seeded
    11-15% net margin applied to the org's own FY revenue."""
    fy_start = CORP_TAX_FYE.replace(year=CORP_TAX_FYE.year - 1) + timedelta(days=1)
    revenue = sum(t.amount for t in bank_txns
                  if t.cashew_category == "revenue" and t.amount > 0
                  and fy_start <= t.date <= CORP_TAX_FYE)
    margin = 0.11 + (_h(seed, "corptax", slug) % 400) / 10_000.0   # 11.00%..14.99%
    return round(revenue * margin * 0.19, 2)


def synth_corp_tax(slug: str, bank_txns: list[BankTxn], seed: int
                   ) -> tuple[list[BankTxn], list[XeroTxn], dict]:
    """One historical corporation-tax payment (bank txn + Xero ledger row) plus
    the machine-readable truth for /sim/truth/corp_tax.

    Returns (extra_bank, extra_xero, truth). If the CSVs already contain a CT
    payment nothing is synthesised and the truth reflects the real one."""
    next_fye = CORP_TAX_FYE.replace(year=CORP_TAX_FYE.year + 1)
    next_due = CORP_TAX_PAY_DATE.replace(year=CORP_TAX_PAY_DATE.year + 1)
    existing = find_corp_tax_payments(bank_txns)
    if existing:
        last = max(existing, key=lambda t: t.date)
        return [], [], {
            "last_payment": {"date": last.date.isoformat(),
                             "amount": round(abs(last.amount), 2)},
            "estimated_next_due": next_due.isoformat(),
            "basis": (f"observed in source data; next due assumed 9 months + 1 day "
                      f"after the notional year end {next_fye.isoformat()}"),
            "source": "csv",
        }

    amount = corp_tax_amount(slug, bank_txns, seed)
    account = main_operating_account(bank_txns)
    bt = BankTxn(
        txn_id=hashlib.md5(f"{slug}:corptax:{CORP_TAX_PAY_DATE}".encode()).hexdigest()[:24],
        date=CORP_TAX_PAY_DATE,
        account_id=_sid(slug, "acct", account),   # same id scheme as the loader
        account_name=account,
        description="HMRC Corporation Tax",
        counterparty=CORP_TAX_COUNTERPARTY,
        amount=-amount,
        balance=0.0,                              # rebuilt by reconcile_balances
        cashew_category="tax_corp",
    )
    code, name, tax = CORP_TAX_GL
    xt = XeroTxn(
        txn_id=hashlib.md5(("x" + bt.txn_id).encode()).hexdigest()[:24],
        date=bt.date, contact=CORP_TAX_COUNTERPARTY, description=bt.description,
        gl_code=code, gl_name=name, tax_type=tax,
        amount=bt.amount, direction="MONEY_OUT", reconciled=True,
    )
    truth = {
        "last_payment": {"date": bt.date.isoformat(), "amount": round(amount, 2)},
        "estimated_next_due": next_due.isoformat(),
        "basis": (f"synthesized: payment 9 months + 1 day after the notional year end "
                  f"{CORP_TAX_FYE.isoformat()}; 19% CT on a seeded 11-15% margin over "
                  f"FY revenue; next due {next_due.isoformat()} for FYE {next_fye.isoformat()}"),
        "source": "synthesized",
    }
    return [bt], [xt], truth


def synth_invoices(slug: str, bank_txns: list[BankTxn], seed: int) -> list[Invoice]:
    """Create an AR/AP invoice for every AR/AP-category cash movement.

    issue_date = due_date - terms; due_date = payment_date - lateness, so the
    ACTUAL payment (the bank txn) lands `lateness` days after the due date.
    The invoice is visible in Xero once issue_date passes and reads PAID once
    the payment_date passes."""
    out: list[Invoice] = []
    for i, t in enumerate(bank_txns):
        cat = t.cashew_category
        if cat in AR_CATEGORIES and t.amount > 0:
            itype, code, tax = "ACCREC", "200", "OUTPUT2"
        elif cat in AP_CATEGORIES and t.amount < 0:
            itype, code, tax = "ACCPAY", "400", "INPUT2"
        else:
            continue
        payment_date = t.date
        lateness = counterparty_lateness(t.counterparty, seed)
        terms = _TERMS_DAYS.get(cat, _DEFAULT_TERMS)
        due_date = payment_date - timedelta(days=lateness)
        issue_date = due_date - timedelta(days=terms)
        out.append(Invoice(
            invoice_id=hashlib.md5(f"{slug}:inv:{i}".encode()).hexdigest()[:24],
            type=itype,
            contact=t.counterparty,
            description=t.description,
            amount=round(abs(t.amount), 2),
            issue_date=issue_date,
            due_date=due_date,
            payment_date=payment_date,
            account_code=code,
            tax_type=tax,
            cashew_category=cat,
        ))
    return out


def _category_kind(cat: str) -> str:
    if cat in SCHEDULED_CATEGORIES:
        return "predictable"      # engine should forecast these
    if cat in LUMPY_CATEGORIES:
        return "surprise"         # lumpy/one-off, hard to forecast
    return "trend"                # recurring but variable (creep/dip)


def scenario_events(bank_txns: list[BankTxn], scenario_month: str,
                    baseline_months: int = 3) -> list[dict]:
    """Compare each category's scenario-month total against its recent baseline
    average, flag material deltas, and label each as predictable/surprise/trend.
    This is the ground truth the oracle checks the engine against."""
    # baseline = the `baseline_months` distinct months immediately before scenario_month
    all_months = sorted({t.date.strftime("%Y-%m") for t in bank_txns})
    if scenario_month in all_months:
        idx = all_months.index(scenario_month)
    else:
        idx = len(all_months)
    base_set = set(all_months[max(0, idx - baseline_months):idx])

    cur: dict[str, float] = defaultdict(float)
    base: dict[str, float] = defaultdict(float)
    cur_cp: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for t in bank_txns:
        m = t.date.strftime("%Y-%m")
        if m == scenario_month:
            cur[t.cashew_category] += t.amount
            cur_cp[t.cashew_category][t.counterparty] += t.amount
        elif m in base_set:
            base[t.cashew_category] += t.amount

    nb = max(len(base_set), 1)
    events: list[dict] = []
    for cat in sorted(set(cur) | set(base)):
        cur_amt = round(cur.get(cat, 0.0), 2)
        base_avg = round(base.get(cat, 0.0) / nb, 2)
        delta = round(cur_amt - base_avg, 2)
        top_cp = None
        if cur_cp.get(cat):
            top_cp = max(cur_cp[cat].items(), key=lambda kv: abs(kv[1]))[0]
        # Material = a big enough delta AND either real current-month activity or
        # a scheduled category we *expect* (so pure "no data this month" noise for
        # incidental categories doesn't get flagged, but a missing payroll does).
        material = (abs(delta) >= max(1000.0, abs(base_avg) * 0.25)
                    and (abs(cur_amt) >= 1000.0 or _category_kind(cat) == "predictable"))
        events.append({
            "category": cat,
            "kind": _category_kind(cat),
            "scenario_month_total": cur_amt,
            "baseline_avg": base_avg,
            "delta": delta,
            "top_counterparty": top_cp,
            "material": material,
        })
    # material events first, biggest absolute delta first
    events.sort(key=lambda e: (not e["material"], -abs(e["delta"])))
    return events
