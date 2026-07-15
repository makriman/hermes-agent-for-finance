"""Actuals Analysis — pull bank + Xero, join them, classify every transaction.

The bank feed is ground truth for *cash* (amounts, dates, balances). The Xero
reconciled ledger is context (GL account, tax type) that arrives with a
bookkeeping lag. The join is deterministic: (date, signed amount), tie-broken
by description similarity.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from difflib import SequenceMatcher

from .client import Clients
from .mapping import Mapper
from .models import EnrichedTxn


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def fetch_bank(c: Clients, as_of: str,
               pot_name: str | None = None) -> tuple[list[dict], float, float]:
    """All bank txns <= as_of (all accounts) + (total_cash, vat_pot).

    READ-ONLY: never mutates the shared sim clock, and never sees past it.
    `as_of` is clamped to the provider's "now" (you cannot know the future),
    and balances are derived from each account's last running_balance — the
    portable approach for real TrueLayer too, where balance is always
    balance-now, not balance-as-of-date.

    The VAT pot is the account named `pot_name` (owner setting) when given;
    otherwise any account with "pot" in its display name (fallback)."""
    now = c.now()["now"]
    as_of = min(as_of, now)
    rows: list[dict] = []
    total_cash = vat_pot = 0.0
    for a in c.accounts():
        acct_rows = c.transactions(a["account_id"], to=as_of)
        for t in acct_rows:
            t["_account"] = a["display_name"]
            rows.append(t)
        bal = acct_rows[-1]["running_balance"]["amount"] if acct_rows else 0.0
        total_cash += bal
        name = a["display_name"].lower()
        if (pot_name and name == pot_name.lower()) or \
                (not pot_name and "pot" in name):
            vat_pot += bal
    return rows, round(total_cash, 2), round(vat_pot, 2)


def fetch_xero_ledger(c: Clients) -> list[dict]:
    return c.bank_transactions()


def join_bank_xero(bank: list[dict], ledger: list[dict]) -> dict[str, dict]:
    """bank txn_id -> xero row, greedy on (date, signed amount), best
    description similarity first."""
    buckets: dict[tuple, list[dict]] = defaultdict(list)
    for x in ledger:
        amt = x["Total"] if x["Type"] == "RECEIVE" else -x["Total"]
        buckets[(x["DateString"][:10], round(amt, 2))].append(x)

    out: dict[str, dict] = {}
    for t in bank:
        key = (t["timestamp"][:10], round(t["amount"], 2))
        cands = [x for x in buckets.get(key, []) if not x.get("_used")]
        if not cands:
            continue
        best = max(cands, key=lambda x: _similar(t["description"],
                                                 x["LineItems"][0]["Description"]))
        best["_used"] = True
        out[t["transaction_id"]] = best
    return out


def build_enriched(c: Clients, as_of: str,
                   owner_rules: list[dict] | None = None,
                   pot_name: str | None = None
                   ) -> tuple[list[EnrichedTxn], float, float, Mapper]:
    """The full Actuals Analysis pass. Returns (enriched txns sorted by date,
    total_cash, vat_pot, the fitted Mapper)."""
    bank, total_cash, vat_pot = fetch_bank(c, as_of, pot_name=pot_name)
    ledger = fetch_xero_ledger(c)
    joined = join_bank_xero(bank, ledger)

    mapper = Mapper(owner_rules)
    # Learn counterparty->category rules from booked history (the GL layer's
    # own output) so the Xero-lag window can still be classified.
    from .mapping import gl_to_category
    accounts = {a["Code"]: a["Name"] for a in c.xero_accounts()}
    booked: list[tuple[str, str]] = []
    for t in bank:
        x = joined.get(t["transaction_id"])
        if x:
            gl_name = accounts.get(x["LineItems"][0].get("AccountCode", ""), "")
            cat = gl_to_category(gl_name)
            if cat:
                booked.append((t.get("merchant_name", ""), cat))
    mapper.learn_from(booked)

    out: list[EnrichedTxn] = []
    for t in bank:
        x = joined.get(t["transaction_id"])
        gl_code = x["LineItems"][0].get("AccountCode", "") if x else ""
        gl_name = accounts.get(gl_code, "") if x else ""
        tax_type = x["LineItems"][0].get("TaxType", "") if x else ""
        category, source = mapper.classify(t.get("merchant_name", ""),
                                           t.get("description", ""), gl_name)
        out.append(EnrichedTxn(
            txn_id=t["transaction_id"],
            date=date.fromisoformat(t["timestamp"][:10]),
            account=t["_account"],
            amount=round(t["amount"], 2),
            description=t.get("description", ""),
            counterparty=t.get("merchant_name", ""),
            gl_code=gl_code, gl_name=gl_name, tax_type=tax_type,
            category=category, map_source=source,
        ))
    out.sort(key=lambda e: (e.date, e.txn_id))
    return out, total_cash, vat_pot, mapper
