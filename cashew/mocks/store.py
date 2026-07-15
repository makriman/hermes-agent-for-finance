"""Assembles a fully-built, cached OrgStore for a given slug."""
from __future__ import annotations

from pathlib import Path

from . import config, extend, loader, mutations, synth
from .models import OrgStore
from .orgs import get_org

_CACHE: dict[str, OrgStore] = {}


def build_store(slug: str) -> OrgStore:
    meta = get_org(slug)
    folder: Path = config.DATA_ROOT / meta["folder"]

    bank = loader.load_bank(slug, folder)
    xero = loader.load_xero(slug, folder)
    # one historical corporation-tax payment (the CSVs carry none; deterministic)
    ct_bank, ct_xero, corp_tax_truth = synth.synth_corp_tax(slug, bank, config.SEED)
    bank = sorted(bank + ct_bank, key=lambda t: (t.date, t.txn_id))
    xero = sorted(xero + ct_xero, key=lambda t: (t.date, t.txn_id))
    # synthetic continuation past the end of the CSVs (deterministic, seeded)
    extra_bank, extra_xero, emerging_truth = extend.extend_org(slug, bank, xero)
    bank = sorted(bank + extra_bank, key=lambda t: (t.date, t.txn_id))
    xero = sorted(xero + extra_xero, key=lambda t: (t.date, t.txn_id))
    xero = mutations.apply(slug, xero)       # re-apply bookkeeper recategorizations
    bank = loader.reconcile_balances(bank, config.ANCHOR)   # make balances txn-consistent
    store = OrgStore(
        slug=slug,
        meta=meta,
        bank_txns=bank,
        xero_txns=xero,
        accounts=loader.derive_accounts(slug, xero),
        bank_accounts=loader.derive_bank_accounts(slug, bank),
        invoices=synth.synth_invoices(slug, bank, config.SEED),
    )

    pos = synth.cash_position(bank, config.ANCHOR)
    store.operating_balance_anchor = pos["operating"]
    store.total_cash_anchor = pos["total"]
    store.opening_balance_anchor = pos["total"]

    due_date, due_amt = synth.next_vat_due(bank, config.ANCHOR)
    store.vat_next_due_date = due_date
    store.vat_next_due_amount = round(due_amt, 2)
    store.stated_coverage = float(meta["vat_coverage"])

    if pos["has_pot_account"]:
        # Prefer the real ring-fenced VAT-pot account balance from the data.
        store.vat_pot_balance = pos["vat_pot"]
        store.vat_pot_source = "account"
    else:
        # Fall back to the README stated coverage if no pot account exists.
        store.vat_pot_balance = round(due_amt * store.stated_coverage, 2)
        store.vat_pot_source = "synthesized"

    store.vat_coverage = round(store.vat_pot_balance / due_amt, 4) if due_amt else 0.0
    store.corp_tax_truth = corp_tax_truth
    store.emerging_truth = emerging_truth
    return store


def get_store(slug: str) -> OrgStore:
    if slug not in _CACHE:
        _CACHE[slug] = build_store(slug)
    return _CACHE[slug]


def invalidate(slug: str) -> None:
    """Drop one org from the cache (next read rebuilds it, re-applying any
    registered mutations)."""
    _CACHE.pop(slug, None)


def clear_cache() -> None:
    _CACHE.clear()
