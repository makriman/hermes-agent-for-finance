"""Simulation control + ground-truth ("oracle") endpoints.

`/sim/*` drives the virtual clock and switches org; `/sim/truth/*` exposes the
synthesised context and the machine-readable expectations a forecast engine
should reproduce (used by the sim runner as a pass/fail oracle).
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from .. import clock, config, extend, mutations, store, synth
from ..common import current
from ..orgs import all_orgs, get_org

router = APIRouter(prefix="/sim", tags=["sim"])


class SetDate(BaseModel):
    date: str


class Advance(BaseModel):
    days: int = 1


class SetOrg(BaseModel):
    slug: str


def _state() -> dict:
    return {
        "org": clock.get_org(),
        "now": clock.get_now().isoformat(),
        "anchor": config.ANCHOR.isoformat(),
        "scenario_month": config.SCENARIO_MONTH,
    }


@router.get("/now")
def now():
    return _state()


@router.post("/set")
def set_date(body: SetDate):
    try:
        d = date.fromisoformat(body.date)
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    clock.set_now(d)
    return _state()


@router.post("/advance")
def advance(body: Advance):
    clock.advance(body.days)
    return _state()


@router.post("/reset")
def reset():
    clock.reset()
    return _state()


@router.post("/org")
def set_org(body: SetOrg):
    try:
        get_org(body.slug)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    clock.set_org(body.slug)
    store.get_store(body.slug)   # warm the cache
    return _state()


@router.get("/orgs")
def orgs():
    return {"orgs": all_orgs()}


@router.get("/config")
def cfg():
    st, now_ = current()
    return {
        **_state(),
        "meta": st.meta,
        "counts": {
            "bank_txns": len(st.bank_txns),
            "xero_txns": len(st.xero_txns),
            "accounts": len(st.accounts),
            "bank_accounts": len(st.bank_accounts),
            "invoices": len(st.invoices),
        },
        "date_range": {
            "min": st.bank_txns[0].date.isoformat() if st.bank_txns else None,
            "max": st.bank_txns[-1].date.isoformat() if st.bank_txns else None,
        },
    }


# --- ground truth / oracle ---------------------------------------------------

def _vat_block(st) -> dict:
    shortfall = round(st.vat_next_due_amount - st.vat_pot_balance, 2)
    return {
        "next_due_date": st.vat_next_due_date.isoformat() if st.vat_next_due_date else None,
        "next_due_amount": st.vat_next_due_amount,
        "pot_balance": st.vat_pot_balance,
        "pot_source": st.vat_pot_source,
        "coverage": st.vat_coverage,
        "stated_coverage": st.stated_coverage,
        "shortfall": shortfall,
        "underfunded": shortfall > 0,
    }


@router.get("/truth/vat")
def truth_vat():
    st, _ = current()
    return _vat_block(st)


@router.get("/truth/opening_balance")
def truth_opening(as_of: str | None = Query(None)):
    st, now_ = current()
    d = date.fromisoformat(as_of) if as_of else config.ANCHOR
    return {"as_of": d.isoformat(), "balance": round(synth.opening_balance(st.bank_txns, d), 2)}


@router.get("/truth/labels")
def truth_labels():
    """Ground-truth category per bank transaction id — for SCORING an engine's
    mapping only. The open-banking feed itself does not leak labels."""
    st, now_ = current()
    return {"labels": {t.txn_id: t.cashew_category for t in st.bank_txns if t.date <= now_}}


@router.get("/truth/scenario")
def truth_scenario():
    st, _ = current()
    events = synth.scenario_events(st.bank_txns, config.SCENARIO_MONTH)
    return {"scenario_month": config.SCENARIO_MONTH, "events": events}


@router.get("/truth/corp_tax")
def truth_corp_tax():
    """Corporation-tax ground truth: the (synthesised) historical payment and
    when the next one is estimated to fall due."""
    st, _ = current()
    return st.corp_tax_truth


@router.get("/truth/emerging")
def truth_emerging():
    """Planted emerging-pattern fixtures: the NEW recurring counterparty the
    engine should detect, and the same-magnitude one-off it should ignore."""
    st, _ = current()
    return st.emerging_truth


# --- bookkeeper mutations ------------------------------------------------------

class Recategorize(BaseModel):
    counterparty: str
    gl_code: str       # a GL code, a GL account name, or a cashew category
    from_date: str     # ISO YYYY-MM-DD


def _resolve_gl_target(st, target: str) -> tuple[str, str] | None:
    """Map a code / account-name / category-ish target onto a real GL account."""
    t = target.strip().lower()
    for a in st.accounts:
        if a.code.lower() == t:
            return a.code, a.name
    for a in st.accounts:
        if a.name.lower() == t:
            return a.code, a.name
    learned = extend._gl_by_category(st.bank_txns, st.xero_txns)
    if t in learned:
        code, name, _tax = learned[t]
        return code, name
    return None


@router.post("/recategorize")
def recategorize(body: Recategorize):
    """Simulate a bookkeeper re-coding a counterparty in Xero: all of that
    counterparty's ledger rows dated >= from_date move to the target account.
    In-memory only (resets on server restart); survives org-cache rebuilds."""
    try:
        fd = date.fromisoformat(body.from_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="from_date must be YYYY-MM-DD")
    st, now_ = current()
    target = _resolve_gl_target(st, body.gl_code)
    if target is None:
        raise HTTPException(
            status_code=422,
            detail=f"cannot map '{body.gl_code}' to a GL account "
                   f"(use a chart code, an account name, or a cashew category)")
    code, name = target
    m = mutations.add(st.slug, {
        "counterparty": body.counterparty,
        "gl_code": code,
        "gl_name": name,
        "from_date": fd.isoformat(),
        "applied_at": now_.isoformat(),
        "matched_rows": 0,
    })
    store.invalidate(st.slug)
    st2 = store.get_store(st.slug)          # rebuild re-applies the registry
    return {
        "ok": True,
        "org": st2.slug,
        "counterparty": body.counterparty,
        "gl_code": code,
        "gl_name": name,
        "from_date": fd.isoformat(),
        "matched_rows": m["matched_rows"],
    }


@router.get("/truth/recategorizations")
def truth_recategorizations():
    """All bookkeeper recategorizations applied to the active org (in order)."""
    st, _ = current()
    return {"org": st.slug, "recategorizations": mutations.for_org(st.slug)}


@router.get("/truth/expected")
def truth_expected():
    """Everything the engine should reproduce for the scenario month."""
    st, _ = current()
    anchor = config.ANCHOR
    events = synth.scenario_events(st.bank_txns, config.SCENARIO_MONTH)
    material = [e for e in events if e["material"]]

    open_ar = [i for i in st.invoices
               if i.type == "ACCREC" and i.issue_date <= anchor and i.payment_date > anchor]
    open_ap = [i for i in st.invoices
               if i.type == "ACCPAY" and i.issue_date <= anchor and i.payment_date > anchor]

    return {
        "org": st.slug,
        "anchor": anchor.isoformat(),
        "scenario_month": config.SCENARIO_MONTH,
        "cash": {
            "operating": st.operating_balance_anchor,
            "vat_pot": st.vat_pot_balance,
            "total": st.total_cash_anchor,
        },
        "opening_balance_anchor": st.opening_balance_anchor,
        "vat": _vat_block(st),
        "predictable": [e for e in material if e["kind"] == "predictable"],
        "surprises": [e for e in material if e["kind"] == "surprise"],
        "trends": [e for e in material if e["kind"] == "trend"],
        "open_ar_ap_at_anchor": {
            "ar_count": len(open_ar), "ar_total": round(sum(i.amount for i in open_ar), 2),
            "ap_count": len(open_ap), "ap_total": round(sum(i.amount for i in open_ap), 2),
        },
    }
