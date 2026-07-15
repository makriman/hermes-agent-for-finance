"""Test suite for the Cashew mock/simulation harness."""
from __future__ import annotations

import pytest

from mocks.orgs import ORGS


def _main_account(client):
    accts = client.get("/openbanking/data/v1/accounts").json()["results"]
    return next((a for a in accts if "pot" not in a["display_name"].lower()), accts[0])["account_id"]


def _visible_count(client, aid):
    return len(client.get(f"/openbanking/data/v1/accounts/{aid}/transactions").json()["results"])


# --- loading -----------------------------------------------------------------

@pytest.mark.parametrize("slug", list(ORGS))
def test_every_org_loads(client, slug):
    client.post("/sim/org", json={"slug": slug})
    cfg = client.get("/sim/config").json()
    assert cfg["org"] == slug
    assert cfg["counts"]["bank_txns"] > 0
    assert cfg["counts"]["xero_txns"] == cfg["counts"]["bank_txns"]  # reconcile 1:1
    assert cfg["counts"]["accounts"] > 0


def test_unknown_org_404(client):
    assert client.post("/sim/org", json={"slug": "nope"}).status_code == 404


# --- open banking + time travel ---------------------------------------------

def test_accounts_and_balance(client):
    accts = client.get("/openbanking/data/v1/accounts").json()["results"]
    assert accts and accts[0]["provider"]["provider_id"] == "monzo"
    aid = accts[0]["account_id"]
    bal = client.get(f"/openbanking/data/v1/accounts/{aid}/balance").json()["results"][0]
    assert bal["currency"] == "GBP" and "current" in bal


def test_time_travel_monotonic(client):
    aid = _main_account(client)
    client.post("/sim/set", json={"date": "2026-06-30"})
    n0 = _visible_count(client, aid)
    client.post("/sim/set", json={"date": "2026-07-01"})
    n1 = _visible_count(client, aid)
    client.post("/sim/set", json={"date": "2026-07-31"})
    n2 = _visible_count(client, aid)
    assert n0 <= n1 <= n2          # never fewer as the clock advances
    assert n1 > n0                 # July shock reveals new actuals


def test_future_is_hidden(client):
    aid = _main_account(client)
    client.post("/sim/set", json={"date": "2024-03-13"})
    txns = client.get(f"/openbanking/data/v1/accounts/{aid}/transactions").json()["results"]
    assert all(t["timestamp"][:10] <= "2024-03-13" for t in txns)


def test_transaction_shape(client):
    aid = _main_account(client)
    client.post("/sim/set", json={"date": "2026-07-31"})
    t = client.get(f"/openbanking/data/v1/accounts/{aid}/transactions").json()["results"][0]
    for k in ("transaction_id", "timestamp", "amount", "transaction_type", "running_balance"):
        assert k in t
    assert t["transaction_type"] in ("CREDIT", "DEBIT")
    # realism: the bank feed must NOT leak ground-truth labels
    assert "cashew_category" not in t.get("meta", {})


def test_truth_labels_endpoint(client):
    client.post("/sim/set", json={"date": "2026-07-31"})
    labels = client.get("/sim/truth/labels").json()["labels"]
    assert labels and all(isinstance(v, str) for v in labels.values())


def test_xero_reconciliation_lag(client):
    """Xero rows lag the bank by XERO_LAG_DAYS — recent txns are unbooked."""
    client.post("/sim/set", json={"date": "2026-07-01"})
    aid = _main_account(client)
    bank_n = _visible_count(client, aid)
    xero_n = len(client.get("/xero/api.xro/2.0/BankTransactions?page=all").json()["BankTransactions"])
    assert xero_n < bank_n            # ledger runs behind the bank feed


def test_xero_contacts(client):
    client.post("/sim/set", json={"date": "2026-07-31"})
    contacts = client.get("/xero/api.xro/2.0/Contacts").json()["Contacts"]
    assert contacts and {"Name", "IsSupplier", "IsCustomer"} <= set(contacts[0].keys())


# --- xero --------------------------------------------------------------------

def test_xero_bank_transactions_time_travel(client):
    client.post("/sim/set", json={"date": "2026-06-30"})
    a = len(client.get("/xero/api.xro/2.0/BankTransactions?page=all").json()["BankTransactions"])
    client.post("/sim/set", json={"date": "2026-07-31"})
    b = len(client.get("/xero/api.xro/2.0/BankTransactions?page=all").json()["BankTransactions"])
    assert b >= a


def test_xero_pagination(client):
    client.post("/sim/set", json={"date": "2026-07-31"})
    p1 = client.get("/xero/api.xro/2.0/BankTransactions?page=1").json()["BankTransactions"]
    assert len(p1) <= 100


def test_invoice_paid_transition(client):
    """An invoice open at the anchor should flip to PAID once the clock passes
    its payment date."""
    client.post("/sim/set", json={"date": "2026-06-30"})          # anchor
    open_now = [i for i in client.get("/xero/api.xro/2.0/Invoices?page=all").json()["Invoices"]
                if i["Status"] == "AUTHORISED"]
    assert open_now, "expected some open (unpaid) commitments at the anchor"
    sample = open_now[0]
    assert sample["AmountDue"] == sample["Total"] and sample["AmountPaid"] == 0.0

    client.post("/sim/set", json={"date": "2026-07-31"})          # end of scenario month
    match = [i for i in client.get("/xero/api.xro/2.0/Invoices?page=all").json()["Invoices"]
             if i["InvoiceID"] == sample["InvoiceID"]]
    assert match and match[0]["Status"] == "PAID"
    assert match[0]["AmountPaid"] == match[0]["Total"] and match[0]["AmountDue"] == 0.0


def test_invoice_type_filter(client):
    client.post("/sim/set", json={"date": "2026-07-31"})
    ar = client.get("/xero/api.xro/2.0/Invoices?type=ACCREC&page=all").json()["Invoices"]
    assert ar and all(i["Type"] == "ACCREC" for i in ar)


# --- sim control -------------------------------------------------------------

def test_advance_and_reset(client):
    client.post("/sim/reset", json={})
    anchor = client.get("/sim/now").json()["now"]
    client.post("/sim/advance", json={"days": 5})
    assert client.get("/sim/now").json()["now"] > anchor
    client.post("/sim/reset", json={})
    assert client.get("/sim/now").json()["now"] == anchor


# --- truth / oracle ----------------------------------------------------------

def test_cash_position_sums_accounts(client):
    exp = client.get("/sim/truth/expected").json()
    c = exp["cash"]
    assert round(c["operating"] + c["vat_pot"], 2) == round(c["total"], 2)


def test_jam_vat_underfunded_from_real_pot(client):
    client.post("/sim/org", json={"slug": "jam-scn-1"})
    vat = client.get("/sim/truth/vat").json()
    assert vat["pot_source"] == "account"          # real VAT-pot account, not synthetic
    assert vat["next_due_amount"] == 45000.0
    assert vat["underfunded"] and vat["shortfall"] > 0


def test_jam_dividend_is_surprise(client):
    client.post("/sim/org", json={"slug": "jam-scn-1"})
    exp = client.get("/sim/truth/expected").json()
    assert any(e["category"] == "directors_drawings" for e in exp["surprises"])


def test_jam_scenarios_have_vat_signal(client):
    for slug in ("jam-scn-1", "jam-scn-2", "jam-scn-3"):
        client.post("/sim/org", json={"slug": slug})
        vat = client.get("/sim/truth/vat").json()
        assert vat["underfunded"] and vat["shortfall"] > 0


def test_open_ar_ap_present_at_anchor(client):
    client.post("/sim/org", json={"slug": "jam-scn-1"})
    exp = client.get("/sim/truth/expected").json()["open_ar_ap_at_anchor"]
    assert exp["ar_count"] > 0     # forward commitments exist for the engine to project
