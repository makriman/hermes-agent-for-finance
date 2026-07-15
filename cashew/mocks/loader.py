"""CSV -> canonical model. Reads the two CSVs for an org and derives the bank
accounts and the Xero chart of accounts."""
from __future__ import annotations

import csv
import hashlib
from collections import defaultdict
from dataclasses import replace
from datetime import date
from pathlib import Path

from . import config
from .models import Account, BankAccount, BankTxn, XeroTxn


def _sid(*parts) -> str:
    return hashlib.md5(":".join(str(p) for p in parts).encode()).hexdigest()[:24]


def _money(s: str | None) -> float:
    s = (s or "").replace(",", "").strip()
    return float(s) if s else 0.0


def _d(s: str) -> date:
    return date.fromisoformat(s.strip())


def _find(folder: Path, suffix: str) -> Path:
    matches = sorted(folder.glob(f"*{suffix}"))
    if not matches:
        raise FileNotFoundError(f"no '*{suffix}' in {folder}")
    return matches[0]


def load_bank(slug: str, folder: Path) -> list[BankTxn]:
    path = _find(folder, "bank statement.csv")
    out: list[BankTxn] = []
    with open(path, newline="") as f:
        for i, row in enumerate(csv.DictReader(f)):
            acct_name = row["Account"].strip()
            amount = _money(row["Money In (GBP)"]) - _money(row["Money Out (GBP)"])
            out.append(BankTxn(
                txn_id=_sid(slug, "bank", i),
                date=_d(row["Date"]),
                account_id=_sid(slug, "acct", acct_name),
                account_name=acct_name,
                description=row["Description"].strip(),
                counterparty=row["Counterparty"].strip(),
                amount=amount,
                balance=_money(row["Balance (GBP)"]),
                cashew_category=row["Cashew Category"].strip(),
            ))
    out.sort(key=lambda t: (t.date, t.txn_id))
    return out


def load_xero(slug: str, folder: Path) -> list[XeroTxn]:
    path = _find(folder, "Xero.csv")
    out: list[XeroTxn] = []
    with open(path, newline="") as f:
        for i, row in enumerate(csv.DictReader(f)):
            amount = _money(row["Money In (GBP)"]) - _money(row["Money Out (GBP)"])
            out.append(XeroTxn(
                txn_id=_sid(slug, "xero", i),
                date=_d(row["Date"]),
                contact=row["Contact"].strip(),
                description=row["Description"].strip(),
                gl_code=row["GL Account Code"].strip(),
                gl_name=row["GL Account Name"].strip(),
                tax_type=row["Tax Type"].strip(),
                amount=amount,
                direction=row["Type"].strip(),
                reconciled=row["Reconciled"].strip().lower() == "true",
            ))
    out.sort(key=lambda t: (t.date, t.txn_id))
    return out


def reconcile_balances(bank_txns: list[BankTxn], anchor: date) -> list[BankTxn]:
    """Make each account's running balance transaction-consistent.

    The synthetic CSV balance column can drift from the transaction amounts; a
    real open-banking feed never does (balance = prior + amount). We recompute
    the running balance per account as a cumulative sum of amounts, offset so
    the balance at the anchor still matches the CSV's level there (keeping the
    absolute cash position realistic)."""
    per: dict[str, list[BankTxn]] = defaultdict(list)
    for t in bank_txns:
        per[t.account_name].append(t)

    out: list[BankTxn] = []
    for txns in per.values():
        txns = sorted(txns, key=lambda x: (x.date, x.txn_id))
        run = 0.0
        computed: list[float] = []
        for t in txns:
            run += t.amount
            computed.append(run)
        csv_anchor = None
        comp_anchor = 0.0
        for t, cval in zip(txns, computed):
            if t.date <= anchor:
                csv_anchor, comp_anchor = t.balance, cval
        offset = (csv_anchor - comp_anchor) if csv_anchor is not None else 0.0
        for t, cval in zip(txns, computed):
            out.append(replace(t, balance=round(cval + offset, 2)))
    out.sort(key=lambda t: (t.date, t.txn_id))
    return out


def derive_bank_accounts(slug: str, bank_txns: list[BankTxn]) -> list[BankAccount]:
    seen: dict[str, BankAccount] = {}
    for t in bank_txns:
        if t.account_id in seen:
            continue
        h = hashlib.md5((slug + t.account_name).encode()).hexdigest()
        seen[t.account_id] = BankAccount(
            account_id=t.account_id,
            display_name=t.account_name,
            sort_code=f"04-00-{int(h[:2], 16):02d}",           # Monzo-style 04-00-xx
            account_number=str(10_000_000 + int(h[2:8], 16) % 90_000_000),
        )
    return list(seen.values())


def _classify_gl(name: str) -> tuple[str, str]:
    """Return (Xero account Type, Class) from the GL account name."""
    n = name.lower()
    # expense keywords first — "Audit & Accountancy fees" must not match "account"
    if any(k in n for k in ("accountancy", "audit", "fees")):
        return "EXPENSE", "EXPENSE"
    if any(k in n for k in ("bank", "savings", "account", "cash")):
        return "BANK", "ASSET"
    if any(k in n for k in ("sales", "revenue", "income", "fees earned")):
        return "REVENUE", "REVENUE"
    if any(k in n for k in ("vat", "paye", "payable", "liability", "hmrc", "corporation tax")):
        return "CURRLIAB", "LIABILITY"
    if any(k in n for k in ("drawings", "dividend", "capital", "equity", "retained")):
        return "EQUITY", "EQUITY"
    return "EXPENSE", "EXPENSE"


def derive_accounts(slug: str, xero_txns: list[XeroTxn]) -> list[Account]:
    seen: dict[str, Account] = {}
    for t in xero_txns:
        key = (t.gl_code, t.gl_name)
        if key in seen:
            continue
        typ, cls = _classify_gl(t.gl_name)
        seen[key] = Account(
            account_id=_sid(slug, "gl", t.gl_code, t.gl_name),
            code=t.gl_code,
            name=t.gl_name,
            type=typ,
            tax_type=t.tax_type or "NONE",
            cls=cls,
        )
    return sorted(seen.values(), key=lambda a: a.code)
