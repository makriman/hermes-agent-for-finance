"""Mock Open Banking API — modelled on the TrueLayer Data API v1.

Only data with `date <= sim_now` is ever returned (time-travel), and balances
are reported as-of the virtual clock. Swap the base URL for real TrueLayer and
a connector written against this shape keeps working.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query

from .. import config
from ..common import current, iso_dt

router = APIRouter(prefix="/openbanking/data/v1", tags=["openbanking"])


def _account_or_404(store, account_id: str):
    for a in store.bank_accounts:
        if a.account_id == account_id:
            return a
    raise HTTPException(status_code=404, detail="account not found")


def _account_json(a) -> dict:
    return {
        "account_id": a.account_id,
        "account_type": "TRANSACTION",
        "display_name": a.display_name,
        "currency": config.CURRENCY,
        "account_number": {"number": a.account_number, "sort_code": a.sort_code, "iban": None},
        "provider": {
            "display_name": config.OPENBANKING_PROVIDER_NAME,
            "provider_id": config.OPENBANKING_PROVIDER,
            "logo_uri": None,
        },
    }


@router.get("/accounts")
def list_accounts():
    store, _ = current()
    return {"results": [_account_json(a) for a in store.bank_accounts], "status": "Succeeded"}


@router.get("/accounts/{account_id}")
def get_account(account_id: str):
    store, _ = current()
    return {"results": [_account_json(_account_or_404(store, account_id))], "status": "Succeeded"}


@router.get("/accounts/{account_id}/balance")
def get_balance(account_id: str):
    store, now = current()
    _account_or_404(store, account_id)
    bal = 0.0
    for t in store.bank_txns:
        if t.account_id != account_id:
            continue
        if t.date <= now:
            bal = t.balance
        else:
            break
    return {"results": [{
        "currency": config.CURRENCY,
        "available": round(bal, 2),
        "current": round(bal, 2),
        "overdraft": 0,
        "update_timestamp": iso_dt(now),
    }], "status": "Succeeded"}


def _txn_json(t) -> dict:
    j = {
        "transaction_id": t.txn_id,
        "timestamp": iso_dt(t.date),
        "description": t.description,
        "amount": round(t.amount, 2),
        "currency": config.CURRENCY,
        "transaction_type": "CREDIT" if t.amount >= 0 else "DEBIT",
        "transaction_category": "CREDIT" if t.amount >= 0 else "PURCHASE",
        "merchant_name": t.counterparty,
        "running_balance": {"amount": round(t.balance, 2), "currency": config.CURRENCY},
        "meta": {"provider_category": "CREDIT" if t.amount >= 0 else "PURCHASE"},
    }
    if config.LEAK_LABELS:   # debug mode only — real bank feeds carry no labels
        j["meta"]["cashew_category"] = t.cashew_category
    return j


@router.get("/accounts/{account_id}/transactions")
def list_transactions(
    account_id: str,
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
):
    store, now = current()
    _account_or_404(store, account_id)
    try:
        lo = date.fromisoformat(from_) if from_ else None
        hi = date.fromisoformat(to) if to else None
    except ValueError:
        raise HTTPException(status_code=422, detail="from/to must be YYYY-MM-DD")
    out = []
    for t in store.bank_txns:
        if t.account_id != account_id:
            continue
        if t.date > now:                 # time-travel: future is invisible
            break
        if lo and t.date < lo:
            continue
        if hi and t.date > hi:
            continue
        out.append(_txn_json(t))
    return {"results": out, "status": "Succeeded"}


@router.get("/accounts/{account_id}/transactions/pending")
def list_pending(account_id: str):
    """Transactions auth'd but not yet settled: dated within the last
    PENDING_DAYS of the clock (they also appear on /transactions — a
    simplification of real T+1/T+2 settlement, documented in the README)."""
    from datetime import timedelta
    store, now = current()
    _account_or_404(store, account_id)
    cutoff = now - timedelta(days=config.PENDING_DAYS)
    out = []
    for t in store.bank_txns:
        if t.account_id != account_id or t.date > now:
            continue
        if t.date > cutoff:
            j = _txn_json(t)
            j["status"] = "pending"
            out.append(j)
    return {"results": out, "status": "Succeeded"}


def _regular_outflows(store, account_id: str, now):
    """(counterparty -> stats) for recurring outflows observed by the clock."""
    import statistics as st
    from collections import defaultdict
    grp = defaultdict(list)
    for t in store.bank_txns:
        if t.account_id != account_id or t.date > now or t.amount >= 0:
            continue
        if t.cashew_category == "transfers_internal":
            continue
        grp[t.counterparty].append(t)
    out = {}
    for cp, txns in grp.items():
        if len(txns) < 3:
            continue
        months = {t.date.strftime("%Y-%m") for t in txns}
        if len(months) < 3:
            continue
        amounts = [abs(t.amount) for t in txns]
        days = [t.date.day for t in txns]
        mean = st.mean(amounts)
        out[cp] = {
            "txns": txns, "median": st.median(amounts),
            "cv": (st.pstdev(amounts) / mean) if mean else 0.0,
            "day": int(st.median(days)),
            "day_spread": st.pstdev(days) if len(days) > 1 else 0.0,
            "last": max(txns, key=lambda t: t.date),
        }
    return out


@router.get("/accounts/{account_id}/standing_orders")
def standing_orders(account_id: str):
    """Fixed-amount, fixed-day recurring payments (TrueLayer shape)."""
    import calendar as cal
    store, now = current()
    _account_or_404(store, account_id)
    results = []
    for cp, s in sorted(_regular_outflows(store, account_id, now).items()):
        if s["cv"] > 0.05 or s["day_spread"] > 3:
            continue
        day = min(s["day"], 28)
        nxt = now.replace(day=min(day, cal.monthrange(now.year, now.month)[1]))
        if nxt <= now:
            y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
            nxt = nxt.replace(year=y, month=m, day=min(day, cal.monthrange(y, m)[1]))
        results.append({
            "standing_order_id": f"so-{_sid_short(store.slug, cp)}",
            "status": "Active",
            "frequency": "Monthly",
            "payee": cp,
            "reference": s["last"].description[:35],
            "currency": config.CURRENCY,
            "next_payment_amount": round(s["median"], 2),
            "next_payment_date": iso_dt(nxt),
            "first_payment_date": iso_dt(min(t.date for t in s["txns"])),
            "meta": {"provider_account_id": account_id},
        })
    return {"results": results, "status": "Succeeded"}


@router.get("/accounts/{account_id}/direct_debits")
def direct_debits(account_id: str):
    """Variable recurring pulls (TrueLayer shape) — regulars that aren't SOs."""
    store, now = current()
    _account_or_404(store, account_id)
    results = []
    for cp, s in sorted(_regular_outflows(store, account_id, now).items()):
        if s["cv"] <= 0.05 and s["day_spread"] <= 3:
            continue          # that's a standing order
        if s["cv"] > 0.9:
            continue          # too erratic to be a mandate
        last = s["last"]
        results.append({
            "direct_debit_id": f"dd-{_sid_short(store.slug, cp)}",
            "name": cp,
            "status": "Active",
            "currency": config.CURRENCY,
            "previous_payment_amount": round(abs(last.amount), 2),
            "previous_payment_timestamp": iso_dt(last.date),
            "meta": {"provider_account_id": account_id},
        })
    return {"results": results, "status": "Succeeded"}


def _sid_short(slug: str, cp: str) -> str:
    import hashlib
    return hashlib.md5(f"{slug}:{cp}".encode()).hexdigest()[:12]
