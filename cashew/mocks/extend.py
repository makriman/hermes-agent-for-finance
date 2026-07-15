"""Synthetic continuation of an org's activity past the end of the CSVs.

The source data stops at the scenario shocks (2026-07-01). This module extends
each recurring (category, counterparty) pattern — detected from the org's OWN
history — from the day after the last real transaction through
config.EXTEND_UNTIL, so daily-reveal, mid-month reconciliation and multi-month
demos have real texture.

Rules:
  * deterministic: every choice is seeded from (SEED, org, counterparty, month)
  * source CSVs are never touched — extension happens at load time
  * a month already covered by the scenario authors for a counterparty is
    skipped (no double-booking the Jul-1 shocks)
  * tax_vat keeps its QUARTERLY cadence: the next return (~3 months after the
    last historical payment) is fabricated only if it lands inside the
    extension window, together with a paired VAT-pot drawdown. With the shipped
    data (last payment 2026-07-01) the next return is ~2026-10-01, outside the
    default window — nothing is fabricated unless EXTEND_UNTIL covers it.
  * one-off scenario events don't recur (n>=3 gate)
  * internal transfers are extended only as PAIRED movements (monthly VAT-pot
    sweep, quarterly pot drawdown) so cross-account cash still nets to zero
  * emerging-pattern fixtures are planted so detection can be scored: one NEW
    recurring counterparty (CloudCanvas Hosting, monthly from ~2026-07-05) and
    one same-magnitude one-off (Party Supplies Direct, 2026-07-08); truth via
    /sim/truth/emerging
  * every generated bank txn gets a matching Xero ledger row (GL coding learned
    from that category's real rows), so mapping keeps working
"""
from __future__ import annotations

import calendar
import hashlib
import statistics as stats
from collections import Counter, defaultdict
from datetime import date, timedelta

from . import config
from .loader import _sid
from .models import BankTxn, XeroTxn

MIN_OBS = 3
HISTORY_MONTHS = 4          # cadence window before the extension starts
VAT_QUARTER_MONTHS = 3      # quarterly VAT return cadence

# --- emerging-pattern fixtures (planted in the extension window) --------------
# (a) a NEW recurring counterparty the org has never paid before, monthly from
#     a known start date with +/-2 days of seeded jitter, and
# (b) a same-magnitude one-off from a counterparty never seen again.
# Ground truth is exposed via /sim/truth/emerging so detection can be scored.
EMERGING_RECURRING_CP = "CloudCanvas Hosting"
EMERGING_RECURRING_DESC = "CLOUDCANVAS HOSTING SUB"
EMERGING_RECURRING_CATEGORY = "subscription_saas"
EMERGING_START = date(2026, 7, 5)          # nominal monthly anchor day
EMERGING_DAY_JITTER = 2                    # +/- days, seeded per month
EMERGING_NOISE_CP = "Party Supplies Direct"
EMERGING_NOISE_DESC = "PARTY SUPPLIES DIRECT"
EMERGING_NOISE_CATEGORY = "office_supplies"
EMERGING_NOISE_DATE = date(2026, 7, 8)

# GL fallbacks so fixture txns always get a ledger row even if the category's
# coding could not be learned from the org's own history.
_FALLBACK_GL = {
    "subscription_saas": ("463", "IT Software and Consumables", "INPUT2"),
    "office_supplies": ("461", "Printing & Stationery", "INPUT2"),
}

_EMPTY_EMERGING = {"new_recurring": [], "noise": []}


def _h(*parts) -> int:
    return int(hashlib.md5(":".join(str(p) for p in parts).encode()).hexdigest(), 16)


def _hf(*parts) -> float:
    """Deterministic uniform [0,1)."""
    return (_h(*parts) % 10_000) / 10_000.0


def _months_between(start: date, end: date) -> list[str]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


def _clamp_day(ym: str, day: int) -> date:
    y, m = map(int, ym.split("-"))
    return date(y, m, min(max(day, 1), calendar.monthrange(y, m)[1]))


def _add_months(d: date, n: int) -> date:
    y, m = d.year, d.month + n
    y += (m - 1) // 12
    m = (m - 1) % 12 + 1
    return date(y, m, min(d.day, calendar.monthrange(y, m)[1]))


def emerging_params(slug: str) -> tuple[float, float]:
    """Deterministic per-org fixture amounts: ~GBP 89/mo for the new recurring
    counterparty (84.00-93.99) and a same-magnitude one-off (80.00-99.99)."""
    monthly = round(84.0 + (_h(config.SEED, slug, "emerging", "monthly") % 1000) / 100.0, 2)
    noise = round(80.0 + (_h(config.SEED, slug, "emerging", "noise") % 2000) / 100.0, 2)
    return monthly, noise


def _gl_by_category(bank: list[BankTxn], xero: list[XeroTxn]) -> dict[str, tuple]:
    """category -> most common (gl_code, gl_name, tax_type), learned by joining
    the org's own bank & ledger rows on (date, amount)."""
    buckets: dict[tuple, list[XeroTxn]] = defaultdict(list)
    for x in xero:
        buckets[(x.date, round(x.amount, 2))].append(x)
    votes: dict[str, Counter] = defaultdict(Counter)
    for t in bank:
        for x in buckets.get((t.date, round(t.amount, 2)), []):
            votes[t.cashew_category][(x.gl_code, x.gl_name, x.tax_type)] += 1
    return {cat: c.most_common(1)[0][0] for cat, c in votes.items() if c}


def extend_org(slug: str, bank: list[BankTxn], xero: list[XeroTxn]
               ) -> tuple[list[BankTxn], list[XeroTxn], dict]:
    """Return (extra_bank, extra_xero, emerging_truth) continuing the org's
    patterns (plus planted emerging-pattern fixtures) past the CSVs."""
    if not config.EXTEND or not bank:
        return [], [], dict(_EMPTY_EMERGING)
    last_real = max(t.date for t in bank)
    start = last_real + timedelta(days=1)
    end = config.EXTEND_UNTIL
    if start > end:
        return [], [], dict(_EMPTY_EMERGING)

    # -- cadence stats from the months before the scenario month --------------
    scen_month = config.SCENARIO_MONTH
    hist_months = [m for m in sorted({t.date.strftime("%Y-%m") for t in bank})
                   if m < scen_month][-HISTORY_MONTHS:]
    grp: dict[tuple, list[BankTxn]] = defaultdict(list)
    covered: dict[tuple, set] = defaultdict(set)   # months a group already has
    for t in bank:
        key = (t.account_name, t.cashew_category, t.counterparty)
        covered[key].add(t.date.strftime("%Y-%m"))
        if t.date.strftime("%Y-%m") in hist_months:
            grp[key].append(t)

    ext_months = _months_between(start, end)
    extra_bank: list[BankTxn] = []
    seq = 0

    def emit(account: str, ym: str, day: int, amount: float, desc: str,
             cp: str, cat: str) -> date | None:
        nonlocal seq
        seq += 1
        d = _clamp_day(ym, day)
        if d < start or d > end:
            return None
        extra_bank.append(BankTxn(
            txn_id=hashlib.md5(f"{slug}:ext:{seq}:{cp}:{d}".encode()).hexdigest()[:24],
            date=d, account_id=_sid(slug, "acct", account),   # same id scheme as loader
            account_name=account, description=desc, counterparty=cp,
            amount=round(amount, 2), balance=0.0,   # rebuilt by reconcile_balances
            cashew_category=cat,
        ))
        return d

    for (account, cat, cp), txns in sorted(grp.items()):
        if len(txns) < MIN_OBS or cat in ("tax_vat", "transfers_internal"):
            continue
        months_present = {t.date.strftime("%Y-%m") for t in txns}
        if len(months_present) < 2:
            continue                                    # not clearly recurring
        amounts = [t.amount for t in txns]
        days = sorted(t.date.day for t in txns)
        median_amt = stats.median(amounts)
        per_month = max(1, round(len(txns) / len(months_present)))
        desc = Counter(t.description for t in txns).most_common(1)[0][0]

        for ym in ext_months:
            if ym in covered[(account, cat, cp)]:
                continue                                # scenario already wrote it
            for k in range(per_month):
                day = days[_h(config.SEED, slug, cp, ym, k, "d") % len(days)]
                jitter = (_h(config.SEED, slug, cp, ym, k, "j") % 3) - 1   # -1..+1
                noise = 1.0 + (_hf(config.SEED, slug, cp, ym, k) - 0.5) * 0.2
                emit(account, ym, day + jitter, median_amt * noise, desc, cp, cat)

    # -- paired monthly VAT-pot sweep (keeps cross-account cash netting) -------
    op_counts = Counter(t.account_name for t in bank if "pot" not in t.account_name.lower())
    main_op = op_counts.most_common(1)[0][0] if op_counts else bank[0].account_name
    sweeps = [t for t in bank if t.cashew_category == "transfers_internal"
              and t.amount < 0 and "pot" not in t.account_name.lower()]
    pot_names = {t.account_name for t in bank if "pot" in t.account_name.lower()}
    pot = sorted(pot_names)[0] if pot_names else None
    main = sweeps[-1].account_name if sweeps else main_op
    sweep_amt = abs(stats.median([t.amount for t in sweeps])) if sweeps else 0.0
    if sweeps and pot:
        for ym in ext_months:
            if any(t.date.strftime("%Y-%m") == ym for t in sweeps):
                continue
            emit(main, ym, 1, -sweep_amt, "VAT Set-Aside", "VAT Set-Aside", "transfers_internal")
            emit(pot, ym, 1, +sweep_amt, "VAT Set-Aside", "VAT Set-Aside", "transfers_internal")

    # -- quarterly VAT cadence continues if the next return lands in-window ----
    # Next payment = last historical tax_vat outflow + 3 months. Paid from the
    # operating account with a paired pot drawdown (~one quarter of sweeps) so
    # cross-account cash still nets to zero. With the shipped data the next
    # return is ~2026-10-01 — fabricated only when EXTEND_UNTIL covers it.
    vat_pays = [t for t in bank if t.cashew_category == "tax_vat" and t.amount < 0]
    if vat_pays:
        last_vat = max(vat_pays, key=lambda t: (t.date, abs(t.amount)))
        nxt = _add_months(last_vat.date, VAT_QUARTER_MONTHS)
        while nxt <= end:
            ym = nxt.strftime("%Y-%m")
            if not any(t.date.strftime("%Y-%m") == ym for t in vat_pays):
                f = 0.9 + 0.2 * _hf(config.SEED, slug, "vatq", ym)      # +/-10% seeded
                emit(main, ym, nxt.day, -abs(last_vat.amount) * f,
                     "Quarterly VAT payment", "HMRC VAT", "tax_vat")
                if pot and sweep_amt:
                    dd = round(VAT_QUARTER_MONTHS * sweep_amt, 2)
                    emit(pot, ym, nxt.day, -dd, "VAT Pot Drawdown",
                         "VAT Set-Aside", "transfers_internal")
                    emit(main, ym, nxt.day, +dd, "VAT Pot Drawdown",
                         "VAT Set-Aside", "transfers_internal")
            nxt = _add_months(nxt, VAT_QUARTER_MONTHS)

    # -- emerging-pattern fixtures ---------------------------------------------
    monthly_amt, noise_amt = emerging_params(slug)
    rec_dates: list[date] = []
    if EMERGING_START <= end:
        for ym in _months_between(max(start, EMERGING_START), end):
            jit = (_h(config.SEED, slug, "emerging", ym) % (2 * EMERGING_DAY_JITTER + 1)
                   ) - EMERGING_DAY_JITTER                              # -2..+2 days
            d = emit(main_op, ym, EMERGING_START.day + jit, -monthly_amt,
                     EMERGING_RECURRING_DESC, EMERGING_RECURRING_CP,
                     EMERGING_RECURRING_CATEGORY)
            if d:
                rec_dates.append(d)
    noise_date = emit(main_op, EMERGING_NOISE_DATE.strftime("%Y-%m"),
                      EMERGING_NOISE_DATE.day, -noise_amt,
                      EMERGING_NOISE_DESC, EMERGING_NOISE_CP, EMERGING_NOISE_CATEGORY)
    emerging_truth = {
        "new_recurring": ([{
            "counterparty": EMERGING_RECURRING_CP,
            "monthly_amount": monthly_amt,
            "start_date": rec_dates[0].isoformat(),
            "cadence": "monthly",
        }] if rec_dates else []),
        "noise": ([{
            "counterparty": EMERGING_NOISE_CP,
            "amount": noise_amt,
            "date": noise_date.isoformat(),
        }] if noise_date else []),
    }

    # -- matching Xero ledger rows (GL coding learned per category) ------------
    gl = _gl_by_category(bank, xero)
    extra_xero: list[XeroTxn] = []
    for t in extra_bank:
        triple = gl.get(t.cashew_category) or _FALLBACK_GL.get(t.cashew_category)
        if not triple:
            continue
        code, name, tax = triple
        extra_xero.append(XeroTxn(
            txn_id=hashlib.md5(("x" + t.txn_id).encode()).hexdigest()[:24],
            date=t.date, contact=t.counterparty, description=t.description,
            gl_code=code, gl_name=name, tax_type=tax,
            amount=t.amount,
            direction="MONEY_IN" if t.amount >= 0 else "MONEY_OUT",
            reconciled=True,
        ))
    return extra_bank, extra_xero, emerging_truth
