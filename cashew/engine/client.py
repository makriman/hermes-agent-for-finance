"""HTTP clients for the provider APIs (mock harness, or real TrueLayer/Xero).

Only the base URLs change between test and prod; the shapes are identical.
"""
from __future__ import annotations

import os

import requests

DEFAULT_BASE = os.getenv("CASHEW_MOCK_BASE", "http://localhost:8900")


class Clients:
    """Thin wrapper over the three mock surfaces sharing one base URL."""

    def __init__(self, base: str = DEFAULT_BASE, timeout: float = 30.0):
        self.base = base.rstrip("/")
        self.ob = f"{self.base}/openbanking/data/v1"
        self.xero = f"{self.base}/xero/api.xro/2.0"
        self.sim = f"{self.base}/sim"
        self.timeout = timeout

    def _get(self, url, **params):
        r = requests.get(url, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def _post(self, url, body):
        r = requests.post(url, json=body, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    # --- open banking ---
    def accounts(self) -> list[dict]:
        return self._get(f"{self.ob}/accounts")["results"]

    def balance(self, account_id: str) -> float:
        return self._get(f"{self.ob}/accounts/{account_id}/balance")["results"][0]["current"]

    def transactions(self, account_id: str, frm: str | None = None, to: str | None = None) -> list[dict]:
        params = {}
        if frm:
            params["from"] = frm
        if to:
            params["to"] = to
        return self._get(f"{self.ob}/accounts/{account_id}/transactions", **params)["results"]

    def pending(self, account_id: str) -> list[dict]:
        return self._get(f"{self.ob}/accounts/{account_id}/transactions/pending")["results"]

    def standing_orders(self, account_id: str) -> list[dict]:
        return self._get(f"{self.ob}/accounts/{account_id}/standing_orders")["results"]

    def direct_debits(self, account_id: str) -> list[dict]:
        return self._get(f"{self.ob}/accounts/{account_id}/direct_debits")["results"]

    # --- xero ---
    def invoices(self, itype: str | None = None) -> list[dict]:
        params = {"page": "all"}
        if itype:
            params["type"] = itype
        return self._get(f"{self.xero}/Invoices", **params)["Invoices"]

    def bank_transactions(self) -> list[dict]:
        return self._get(f"{self.xero}/BankTransactions", page="all")["BankTransactions"]

    def xero_accounts(self) -> list[dict]:
        return self._get(f"{self.xero}/Accounts")["Accounts"]

    def contacts(self) -> list[dict]:
        return self._get(f"{self.xero}/Contacts")["Contacts"]

    # --- sim control ---
    def now(self) -> dict:
        return self._get(f"{self.sim}/now")

    def config(self) -> dict:
        return self._get(f"{self.sim}/config")

    def set_date(self, d: str) -> dict:
        return self._post(f"{self.sim}/set", {"date": d})

    def advance(self, days: int = 1) -> dict:
        return self._post(f"{self.sim}/advance", {"days": days})

    def reset(self) -> dict:
        return self._post(f"{self.sim}/reset", {})

    def set_org(self, slug: str) -> dict:
        return self._post(f"{self.sim}/org", {"slug": slug})

    def truth_expected(self) -> dict:
        return self._get(f"{self.sim}/truth/expected")
