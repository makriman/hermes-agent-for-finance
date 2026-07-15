"""Tests for the v5 harness upgrades:

1. corporation-tax history + /sim/truth/corp_tax
2. VAT cadence (monthly pot sweep + quarterly payment) in the extension window
3. emerging-pattern fixtures + /sim/truth/emerging
4. POST /sim/recategorize + /sim/truth/recategorizations
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

import pytest
from fastapi.testclient import TestClient

from mocks import clock, config, mutations, store
from mocks.orgs import ORGS

CORP_CP = "HMRC Corporation Tax"
REC_CP = "CloudCanvas Hosting"
NOISE_CP = "Party Supplies Direct"


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """Own clock state file, clean store cache AND clean mutation registry."""
    monkeypatch.setattr(config, "STATE_FILE", tmp_path / "state.json")
    mutations.reset()
    store.clear_cache()
    clock.set_org("jam-scn-1")
    clock.reset()
    yield
    mutations.reset()
    store.clear_cache()


@pytest.fixture
def client():
    from mocks.app import app
    return TestClient(app)


def _main_account(client):
    accts = client.get("/openbanking/data/v1/accounts").json()["results"]
    return next(a for a in accts if "pot" not in a["display_name"].lower())["account_id"]


def _xero_rows(client, contact=None):
    rows = client.get("/xero/api.xro/2.0/BankTransactions?page=all").json()["BankTransactions"]
    if contact:
        rows = [r for r in rows if r["Contact"]["Name"] == contact]
    return rows


# --- 1. corporation tax --------------------------------------------------------

@pytest.mark.parametrize("slug", list(ORGS))
def test_corp_tax_payment_planted(slug):
    st = store.get_store(slug)
    ct = [t for t in st.bank_txns if t.counterparty == CORP_CP]
    assert len(ct) == 1                        # exactly one historical CT payment
    t = ct[0]
    assert t.date == date(2026, 2, 1)          # FYE 2025-04-30 + 9 months + 1 day
    assert t.amount < 0 and t.description == "HMRC Corporation Tax"
    assert t.cashew_category == "tax_corp"
    assert "pot" not in t.account_name.lower()      # paid from the operating account
    # matching Xero ledger row, GL-coded to the corp-tax account, TaxType NONE
    xr = [x for x in st.xero_txns if x.contact == CORP_CP]
    assert len(xr) == 1
    assert xr[0].date == t.date and round(xr[0].amount, 2) == round(t.amount, 2)
    assert (xr[0].gl_code, xr[0].gl_name, xr[0].tax_type) == ("830", "Corporation Tax", "NONE")


def test_corp_tax_truth_endpoint(client):
    truth = client.get("/sim/truth/corp_tax").json()
    st = store.get_store("jam-scn-1")
    paid = next(t for t in st.bank_txns if t.counterparty == CORP_CP)
    assert truth["last_payment"]["date"] == "2026-02-01"
    assert truth["last_payment"]["amount"] == round(abs(paid.amount), 2)
    assert truth["estimated_next_due"] == "2027-02-01"
    assert isinstance(truth["basis"], str) and truth["basis"]
    assert truth["source"] == "synthesized"    # the CSVs carry no CT payments


def test_corp_tax_deterministic_and_org_scaled():
    amounts = {}
    for slug in ORGS:
        a = store.build_store(slug).corp_tax_truth["last_payment"]["amount"]
        b = store.build_store(slug).corp_tax_truth["last_payment"]["amount"]
        assert a == b                          # deterministic under the seed
        assert 1000.0 < a < 200000.0           # plausible magnitude for org volume
        amounts[slug] = a
    assert len(set(amounts.values())) == len(ORGS)   # seeded per org, not one constant


@pytest.mark.parametrize("slug", list(ORGS))
def test_running_balances_consistent_through_corp_tax(slug):
    """Inserting a historical txn must keep balance == prior + amount per account."""
    st = store.get_store(slug)
    per = defaultdict(list)
    for t in st.bank_txns:
        per[t.account_name].append(t)          # store order is (date, txn_id)
    for txns in per.values():
        for a, b in zip(txns, txns[1:]):
            assert abs(b.balance - (a.balance + b.amount)) < 0.015


def test_corp_tax_does_not_move_anchor_balance(client):
    client.post("/sim/set", json={"date": "2026-06-30"})
    aid = _main_account(client)
    bal = client.get(f"/openbanking/data/v1/accounts/{aid}/balance").json()["results"][0]
    assert round(bal["current"], 2) == 123714.48    # same pin as before the upgrade


def test_corp_tax_visible_in_feeds_and_chart(client):
    client.post("/sim/set", json={"date": "2026-06-30"})
    aid = _main_account(client)
    txns = client.get(
        f"/openbanking/data/v1/accounts/{aid}/transactions?from=2026-02-01&to=2026-02-01"
    ).json()["results"]
    assert any(t["description"] == "HMRC Corporation Tax" and t["amount"] < 0 for t in txns)
    accounts = client.get("/xero/api.xro/2.0/Accounts").json()["Accounts"]
    ct = next(a for a in accounts if a["Code"] == "830")
    assert ct["Name"] == "Corporation Tax" and ct["Class"] == "LIABILITY"


# --- 2. VAT cadence in the extension window -------------------------------------

@pytest.mark.parametrize("slug", list(ORGS))
def test_monthly_vat_pot_sweep_continues_in_extension(slug):
    """August (fully synthetic) must still have the paired pot sweep, netting 0."""
    st = store.get_store(slug)
    aug = [t for t in st.bank_txns if t.counterparty == "VAT Set-Aside"
           and t.date.strftime("%Y-%m") == "2026-08"]
    assert len(aug) == 2
    assert round(sum(t.amount for t in aug), 2) == 0.0
    pot_leg = [t for t in aug if "pot" in t.account_name.lower()]
    assert len(pot_leg) == 1 and pot_leg[0].amount > 0     # money INTO the pot


@pytest.mark.parametrize("slug", list(ORGS))
def test_no_vat_payment_fabricated_inside_default_window(slug, client):
    """Last historical quarterly VAT payment is 2026-07-01, so the next return
    (~2026-10-01) falls OUTSIDE the default window — nothing may be invented."""
    st = store.get_store(slug)
    ext_vat = [t for t in st.bank_txns
               if t.cashew_category == "tax_vat" and t.date > date(2026, 7, 1)]
    assert ext_vat == []
    client.post("/sim/org", json={"slug": slug})
    vat = client.get("/sim/truth/vat").json()
    assert vat["next_due_date"] == "2026-07-01"            # oracle unchanged


def test_quarterly_vat_payment_appears_when_window_covers_it(monkeypatch):
    """Extend the window past the next return date: the quarterly payment plus
    a paired pot drawdown must appear, deterministically."""
    monkeypatch.setattr(config, "EXTEND_UNTIL", date(2026, 10, 31))
    st = store.build_store("jam-scn-1")
    vat = [t for t in st.bank_txns
           if t.cashew_category == "tax_vat" and t.date > date(2026, 7, 1)]
    assert len(vat) == 1
    pay = vat[0]
    assert pay.date == date(2026, 10, 1)                   # 2026-07-01 + 3 months
    assert pay.counterparty == "HMRC VAT" and "pot" not in pay.account_name.lower()
    assert 0.9 * 45000 <= abs(pay.amount) <= 1.1 * 45000   # seeded around last return
    # paired pot movement on the same date, cross-account netting to zero
    dd = [t for t in st.bank_txns if t.description == "VAT Pot Drawdown"]
    assert len(dd) == 2 and all(t.date == pay.date for t in dd)
    assert round(sum(t.amount for t in dd), 2) == 0.0
    assert any(t.amount < 0 and "pot" in t.account_name.lower() for t in dd)
    # matching Xero ledger row, coded like the org's other VAT payments
    xr = [x for x in st.xero_txns
          if x.date == pay.date and round(x.amount, 2) == round(pay.amount, 2)]
    assert xr and xr[0].gl_code == "820"
    # deterministic under the seed
    st2 = store.build_store("jam-scn-1")
    assert [t.txn_id for t in st.bank_txns] == [t.txn_id for t in st2.bank_txns]


# --- 3. emerging-pattern fixtures ------------------------------------------------

@pytest.mark.parametrize("slug", list(ORGS))
def test_new_recurring_counterparty_planted(slug):
    st = store.get_store(slug)
    cc = sorted((t for t in st.bank_txns if t.counterparty == REC_CP), key=lambda t: t.date)
    months = [t.date.strftime("%Y-%m") for t in cc]
    assert months == ["2026-07", "2026-08"]                # monthly, from July
    for t in cc:
        assert 3 <= t.date.day <= 7                        # 5th +/- 2 days
        assert t.amount < 0 and 84.0 <= abs(t.amount) <= 94.0   # ~GBP 89/mo
    assert cc[0].amount == cc[1].amount                    # clean recurring signal
    # never seen before the extension window
    assert cc[0].date >= date(2026, 7, 3)
    # matching Xero rows so mapping keeps working
    xr = [x for x in st.xero_txns if x.contact == REC_CP]
    assert len(xr) == 2 and all(x.gl_code for x in xr)


@pytest.mark.parametrize("slug", list(ORGS))
def test_one_off_noise_counterparty_planted(slug):
    st = store.get_store(slug)
    ns = [t for t in st.bank_txns if t.counterparty == NOISE_CP]
    assert len(ns) == 1                                    # never seen again
    t = ns[0]
    assert t.date == date(2026, 7, 8) and t.amount < 0
    assert 80.0 <= abs(t.amount) <= 100.0                  # same magnitude as recurring
    assert len([x for x in st.xero_txns if x.contact == NOISE_CP]) == 1


def test_truth_emerging_matches_feed(client):
    truth = client.get("/sim/truth/emerging").json()
    st = store.get_store("jam-scn-1")
    assert len(truth["new_recurring"]) == 1 and len(truth["noise"]) == 1
    rec, noise = truth["new_recurring"][0], truth["noise"][0]
    cc = sorted((t for t in st.bank_txns if t.counterparty == REC_CP), key=lambda t: t.date)
    assert rec["counterparty"] == REC_CP
    assert rec["start_date"] == cc[0].date.isoformat()
    assert rec["monthly_amount"] == round(abs(cc[0].amount), 2)
    assert rec["cadence"] == "monthly"
    ns = next(t for t in st.bank_txns if t.counterparty == NOISE_CP)
    assert noise == {"counterparty": NOISE_CP, "amount": round(abs(ns.amount), 2),
                     "date": ns.date.isoformat()}


def test_emerging_deterministic():
    a = store.build_store("jam-scn-1").emerging_truth
    b = store.build_store("jam-scn-1").emerging_truth
    assert a == b


# --- 4. recategorization mutations ------------------------------------------------

def test_recategorize_mutates_xero_reads(client):
    client.post("/sim/set", json={"date": "2026-08-31"})
    before = _xero_rows(client, REC_CP)
    assert len(before) == 2
    assert all(r["LineItems"][0]["AccountCode"] == "463" for r in before)

    r = client.post("/sim/recategorize", json={
        "counterparty": REC_CP, "gl_code": "461", "from_date": "2026-08-01"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["matched_rows"] == 1
    assert body["gl_code"] == "461" and body["gl_name"] == "Printing & Stationery"

    after = {r["DateString"][:7]: r["LineItems"][0]["AccountCode"]
             for r in _xero_rows(client, REC_CP)}
    assert after["2026-08"] == "461"           # re-coded from from_date onwards
    assert after["2026-07"] == "463"           # earlier rows untouched


def test_recategorize_accepts_category_target(client):
    r = client.post("/sim/recategorize", json={
        "counterparty": REC_CP, "gl_code": "office_supplies", "from_date": "2026-07-01"})
    assert r.status_code == 200
    body = r.json()
    assert body["gl_code"] == "461" and body["matched_rows"] == 2


def test_recategorize_survives_cache_rebuild(client):
    client.post("/sim/set", json={"date": "2026-08-31"})
    client.post("/sim/recategorize", json={
        "counterparty": NOISE_CP, "gl_code": "820", "from_date": "2026-07-01"})
    store.clear_cache()                        # simulate a full org-cache refresh
    rows = _xero_rows(client, NOISE_CP)
    assert rows and rows[0]["LineItems"][0]["AccountCode"] == "820"


def test_truth_recategorizations_lists_applied(client):
    assert client.get("/sim/truth/recategorizations").json()["recategorizations"] == []
    client.post("/sim/recategorize", json={
        "counterparty": REC_CP, "gl_code": "461", "from_date": "2026-08-01"})
    out = client.get("/sim/truth/recategorizations").json()
    assert out["org"] == "jam-scn-1"
    assert len(out["recategorizations"]) == 1
    m = out["recategorizations"][0]
    assert m["counterparty"] == REC_CP and m["gl_code"] == "461"
    assert m["gl_name"] == "Printing & Stationery"
    assert m["from_date"] == "2026-08-01" and m["matched_rows"] == 1
    assert "applied_at" in m


def test_recategorize_is_org_scoped(client):
    client.post("/sim/recategorize", json={
        "counterparty": REC_CP, "gl_code": "461", "from_date": "2026-07-01"})
    client.post("/sim/org", json={"slug": "jam-scn-2"})
    assert client.get("/sim/truth/recategorizations").json()["recategorizations"] == []
    rows = _xero_rows(client, REC_CP)          # other org's ledger is untouched
    client.post("/sim/set", json={"date": "2026-08-31"})
    rows = _xero_rows(client, REC_CP)
    assert all(r["LineItems"][0]["AccountCode"] == "463" for r in rows)


def test_recategorize_rejects_bad_input(client):
    bad_date = client.post("/sim/recategorize", json={
        "counterparty": REC_CP, "gl_code": "461", "from_date": "not-a-date"})
    assert bad_date.status_code == 422
    bad_target = client.post("/sim/recategorize", json={
        "counterparty": REC_CP, "gl_code": "no-such-account", "from_date": "2026-07-01"})
    assert bad_target.status_code == 422
    assert client.get("/sim/truth/recategorizations").json()["recategorizations"] == []
