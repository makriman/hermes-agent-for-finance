"""Mock Accounting API — modelled on the Xero Accounting API 2.0.

Serves the reconciled bank ledger (`/BankTransactions`), the chart of accounts
(`/Accounts`) and the *synthesised* forward AR/AP commitments (`/Invoices`),
all time-travelled to the virtual clock.

Note: dates are emitted as ISO-8601 strings (both `Date` and `DateString`)
rather than Xero's legacy `/Date(ms)/` form — cleaner for connectors, and real
SDKs normalise both. Pagination mirrors Xero (100 rows/page via `?page=`); pass
`?page=all` to disable it.
"""
from __future__ import annotations

from fastapi import APIRouter, Query

from .. import config
from ..common import current

router = APIRouter(prefix="/xero/api.xro/2.0", tags=["xero"])

_PAGE_SIZE = 100


def _paginate(rows: list, page: str | None) -> list:
    if page in (None, "", "all"):
        return rows
    try:
        p = max(int(page), 1)
    except ValueError:
        p = 1
    return rows[(p - 1) * _PAGE_SIZE: p * _PAGE_SIZE]


def _dt(d) -> dict:
    return {"Date": f"{d.isoformat()}T00:00:00", "DateString": f"{d.isoformat()}T00:00:00"}


def _xero_visible(now):
    """Bookkeeping lag: a Xero row is visible only once the clock is
    XERO_LAG_DAYS past its bank date (real ledgers run behind the bank)."""
    from datetime import timedelta
    return now - timedelta(days=config.XERO_LAG_DAYS)


@router.get("/Organisation")
def organisation():
    store, _ = current()
    return {"Organisations": [{
        "OrganisationID": store.slug,
        "Name": store.meta["name"],
        "LegalName": store.meta["name"],
        "BaseCurrency": config.CURRENCY,
        "CountryCode": "GB",
        "OrganisationType": "COMPANY",
    }]}


@router.get("/Accounts")
def accounts():
    store, _ = current()
    return {"Accounts": [{
        "AccountID": a.account_id,
        "Code": a.code,
        "Name": a.name,
        "Type": a.type,
        "Class": a.cls,
        "TaxType": a.tax_type,
        "Status": "ACTIVE",
        "EnablePaymentsToAccount": False,
    } for a in store.accounts]}


@router.get("/Contacts")
def contacts():
    store, now = current()
    cutoff = _xero_visible(now)
    seen: dict[str, dict] = {}
    for t in store.xero_txns:
        if t.date > cutoff:
            break
        key = t.contact.strip().lower()
        if key and key not in seen:
            import hashlib
            seen[key] = {
                "ContactID": hashlib.md5((store.slug + key).encode()).hexdigest()[:24],
                "Name": t.contact,
                "ContactStatus": "ACTIVE",
                "IsSupplier": t.direction == "MONEY_OUT",
                "IsCustomer": t.direction == "MONEY_IN",
            }
    return {"Contacts": sorted(seen.values(), key=lambda c: c["Name"].lower())}


@router.get("/BankTransactions")
def bank_transactions(page: str | None = Query(None)):
    store, now = current()
    cutoff = _xero_visible(now)
    rows = []
    for t in store.xero_txns:
        if t.date > cutoff:
            break
        total = round(abs(t.amount), 2)
        rows.append({
            "BankTransactionID": t.txn_id,
            "Type": "RECEIVE" if t.direction == "MONEY_IN" else "SPEND",
            "Contact": {"Name": t.contact},
            **_dt(t.date),
            "Status": "AUTHORISED",
            "IsReconciled": t.reconciled,
            "LineItems": [{
                "Description": t.description,
                "AccountCode": t.gl_code,
                "TaxType": t.tax_type,
                "LineAmount": total,
            }],
            "SubTotal": total,
            "TotalTax": 0.0,
            "Total": total,
            "CurrencyCode": config.CURRENCY,
        })
    return {"BankTransactions": _paginate(rows, page)}


def _invoice_json(inv, now) -> dict:
    paid = inv.payment_date <= now
    total = round(inv.amount, 2)
    return {
        "InvoiceID": inv.invoice_id,
        "InvoiceNumber": ("INV-" if inv.type == "ACCREC" else "BILL-") + inv.invoice_id[:8].upper(),
        "Type": inv.type,
        "Contact": {"Name": inv.contact},
        **{"Date": _dt(inv.issue_date)["Date"], "DateString": _dt(inv.issue_date)["DateString"]},
        "DueDate": _dt(inv.due_date)["Date"],
        "DueDateString": _dt(inv.due_date)["DateString"],
        "Status": "PAID" if paid else "AUTHORISED",
        "LineItems": [{
            "Description": inv.description,
            "AccountCode": inv.account_code,
            "TaxType": inv.tax_type,
            "LineAmount": total,
        }],
        "SubTotal": total,
        "TotalTax": 0.0,
        "Total": total,
        "AmountDue": 0.0 if paid else total,
        "AmountPaid": total if paid else 0.0,
        "AmountCredited": 0.0,
        "FullyPaidOnDate": _dt(inv.payment_date)["Date"] if paid else None,
        "CurrencyCode": config.CURRENCY,
        # neutral reference — must NOT leak the ground-truth category
        "Reference": f"REF-{inv.invoice_id[:6].upper()}",
    }


@router.get("/Invoices")
def invoices(
    page: str | None = Query(None),
    type: str | None = Query(None),
    where: str | None = Query(None),
    statuses: str | None = Query(None),
):
    store, now = current()
    # resolve requested type from ?type= or a Xero-style ?where=Type=="ACCREC"
    want = None
    src = (type or "") + " " + (where or "")
    if "ACCREC" in src.upper():
        want = "ACCREC"
    elif "ACCPAY" in src.upper():
        want = "ACCPAY"
    status_filter = {s.strip().upper() for s in statuses.split(",")} if statuses else None

    rows = []
    for inv in store.invoices:
        if inv.issue_date > now:          # not yet issued
            continue
        if want and inv.type != want:
            continue
        j = _invoice_json(inv, now)
        if status_filter and j["Status"] not in status_filter:
            continue
        rows.append(j)
    rows.sort(key=lambda r: r["DueDate"])
    return {"Invoices": _paginate(rows, page)}
