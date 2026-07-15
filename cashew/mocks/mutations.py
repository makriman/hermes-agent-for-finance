"""In-memory bookkeeper mutations: Xero recategorizations.

`POST /sim/recategorize` registers a mutation here; `store.build_store`
re-applies the whole registry on every (re)build, so mutated GL coding
survives org-cache invalidation and rebuilds. The registry is process-local
and intentionally resets on server restart (documented, acceptable for the
harness — the source CSVs are never touched).
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date

from .models import XeroTxn

# slug -> [mutation dicts], in application order (later mutations win on overlap)
_REGISTRY: dict[str, list[dict]] = {}


def add(slug: str, mutation: dict) -> dict:
    """Register a mutation: {counterparty, gl_code, gl_name, from_date(ISO),
    applied_at(ISO sim date), matched_rows}."""
    _REGISTRY.setdefault(slug, []).append(mutation)
    return mutation


def for_org(slug: str) -> list[dict]:
    return [dict(m) for m in _REGISTRY.get(slug, ())]


def reset() -> None:
    _REGISTRY.clear()


def apply(slug: str, xero: list[XeroTxn]) -> list[XeroTxn]:
    """Re-code the ledger rows of each mutated counterparty (case-insensitive
    contact match) dated >= from_date. Also refreshes each mutation's
    matched_rows count so the truth endpoint reports real coverage."""
    muts = _REGISTRY.get(slug)
    if not muts:
        return xero
    for m in muts:
        m["matched_rows"] = 0
    out: list[XeroTxn] = []
    for x in xero:
        for m in muts:                                   # later mutations win
            if (x.contact.strip().lower() == m["counterparty"].strip().lower()
                    and x.date >= date.fromisoformat(m["from_date"])):
                x = replace(x, gl_code=m["gl_code"], gl_name=m["gl_name"])
                m["matched_rows"] += 1
        out.append(x)
    return out
