"""Canonical in-memory model of one org's data.

The two CSVs are loaded into `BankTxn` / `XeroTxn`; `Account` and `BankAccount`
are derived; `Invoice` objects are synthesised (they don't exist in the CSVs).
Everything downstream (the routers) is a time-filtered *view* over this store.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class BankTxn:
    """One open-banking (Monzo) transaction."""
    txn_id: str
    date: date
    account_id: str
    account_name: str
    description: str
    counterparty: str
    amount: float          # signed: + money in, - money out
    balance: float         # running balance after this txn
    cashew_category: str   # ground-truth label from the dataset


@dataclass(frozen=True)
class XeroTxn:
    """One reconciled Xero bank-ledger line (1:1 with a bank txn)."""
    txn_id: str
    date: date
    contact: str
    description: str
    gl_code: str
    gl_name: str
    tax_type: str
    amount: float          # signed: + money in, - money out
    direction: str         # MONEY_IN | MONEY_OUT
    reconciled: bool


@dataclass(frozen=True)
class Account:
    """A Xero chart-of-accounts entry (derived from GL codes)."""
    account_id: str
    code: str
    name: str
    type: str              # BANK | REVENUE | EXPENSE | CURRLIAB | ...
    tax_type: str
    cls: str               # ASSET | REVENUE | EXPENSE | LIABILITY | EQUITY


@dataclass(frozen=True)
class BankAccount:
    """An open-banking account (derived from the bank CSV 'Account' column)."""
    account_id: str
    display_name: str
    sort_code: str
    account_number: str


@dataclass(frozen=True)
class Invoice:
    """A synthesised AR (ACCREC) or AP (ACCPAY) commitment.

    These do not exist in the reconciled CSVs; we derive them from future cash
    movements so the forecast has forward commitments and reconciliation can
    measure payment-timing variance. `payment_date` is the actual bank date;
    the invoice reads PAID once the clock passes it.
    """
    invoice_id: str
    type: str              # ACCREC (AR) | ACCPAY (AP)
    contact: str
    description: str
    amount: float          # positive gross
    issue_date: date
    due_date: date
    payment_date: date     # when the linked bank txn actually settles
    account_code: str
    tax_type: str
    cashew_category: str


@dataclass
class OrgStore:
    slug: str
    meta: dict
    bank_txns: list[BankTxn] = field(default_factory=list)
    xero_txns: list[XeroTxn] = field(default_factory=list)
    accounts: list[Account] = field(default_factory=list)
    bank_accounts: list[BankAccount] = field(default_factory=list)
    invoices: list[Invoice] = field(default_factory=list)
    # synthesised / derived context (as of the forecast anchor)
    opening_balance_anchor: float = 0.0     # total cash across all accounts
    operating_balance_anchor: float = 0.0   # excl. ring-fenced VAT pot
    total_cash_anchor: float = 0.0
    vat_next_due_date: date | None = None
    vat_next_due_amount: float = 0.0
    vat_pot_balance: float = 0.0
    vat_pot_source: str = "account"         # "account" (real) | "synthesized"
    vat_coverage: float = 0.0               # actual = pot / next_due
    stated_coverage: float = 0.0            # README design metric
    corp_tax_truth: dict = field(default_factory=dict)   # /sim/truth/corp_tax
    emerging_truth: dict = field(default_factory=dict)   # /sim/truth/emerging
