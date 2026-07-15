"""Tests for the synthetic data extension + DD/SO/pending mock endpoints."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mocks import clock, config, store
from mocks.orgs import ORGS


@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "STATE_FILE", tmp_path / "state.json")
    store.clear_cache()
    clock.set_org("jam-scn-1")
    clock.reset()
    yield


@pytest.fixture
def client():
    from mocks.app import app
    return TestClient(app)


def _main_account(client):
    accts = client.get("/openbanking/data/v1/accounts").json()["results"]
    return next(a for a in accts if "pot" not in a["display_name"].lower())["account_id"]


# --- extension ---------------------------------------------------------------

def test_data_extends_past_july_1(client):
    st = store.get_store("jam-scn-1")
    assert max(t.date for t in st.bank_txns).isoformat() >= "2026-08-25"
    july = [t for t in st.bank_txns if t.date.strftime("%Y-%m") == "2026-07"]
    assert len(july) > 20            # was 8 before the extension


def test_extension_deterministic():
    a = [t.txn_id for t in store.build_store("jam-scn-1").bank_txns]
    b = [t.txn_id for t in store.build_store("jam-scn-1").bank_txns]
    assert a == b


def test_extension_has_matching_xero_rows():
    st = store.get_store("jam-scn-1")
    ext_bank = [t for t in st.bank_txns if t.date.isoformat() > "2026-07-01"
                and t.cashew_category != "transfers_internal"]
    xero_keys = {(x.date, round(x.amount, 2)) for x in st.xero_txns}
    matched = sum(1 for t in ext_bank if (t.date, round(t.amount, 2)) in xero_keys)
    assert ext_bank and matched / len(ext_bank) > 0.95   # ledger coverage for mapping


def test_anchor_balances_unchanged_by_extension(client):
    """The extension must not rewrite history before the anchor."""
    client.post("/sim/set", json={"date": "2026-06-30"})
    aid = _main_account(client)
    bal = client.get(f"/openbanking/data/v1/accounts/{aid}/balance").json()["results"][0]
    assert round(bal["current"], 2) == 123714.48


def test_no_double_booking_scenario_events():
    """Counterparties the scenario already wrote in July are not regenerated."""
    st = store.get_store("jam-scn-1")
    vat = [t for t in st.bank_txns if t.cashew_category == "tax_vat"
           and t.date.strftime("%Y-%m") == "2026-07"]
    assert len(vat) == 1              # only the scenario's Jul-1 VAT payment
    reg = [t for t in st.bank_txns if t.counterparty == "SCN Regular Customers"
           and t.date.strftime("%Y-%m") == "2026-07"]
    assert len(reg) == 3              # exactly the scenario's three invoices


@pytest.mark.parametrize("slug", list(ORGS))
def test_all_orgs_extend(client, slug):
    client.post("/sim/org", json={"slug": slug})
    cfg = client.get("/sim/config").json()
    assert cfg["date_range"]["max"] >= "2026-08-01"


# --- DD / SO / pending ----------------------------------------------------------

def test_standing_orders_shape_and_content(client):
    client.post("/sim/set", json={"date": "2026-06-30"})
    aid = _main_account(client)
    sos = client.get(f"/openbanking/data/v1/accounts/{aid}/standing_orders").json()["results"]
    assert sos, "expected at least one standing order (e.g. the NatWest loan)"
    so = sos[0]
    for k in ("standing_order_id", "frequency", "payee", "next_payment_amount",
              "next_payment_date", "status"):
        assert k in so
    assert all(s["next_payment_date"][:10] > "2026-06-30" for s in sos)


def test_natwest_loan_is_a_standing_order(client):
    client.post("/sim/set", json={"date": "2026-06-30"})
    aid = _main_account(client)
    sos = client.get(f"/openbanking/data/v1/accounts/{aid}/standing_orders").json()["results"]
    assert any("nat west" in s["payee"].lower() for s in sos)


def test_direct_debits_exclude_standing_orders(client):
    client.post("/sim/set", json={"date": "2026-06-30"})
    aid = _main_account(client)
    sos = {s["payee"] for s in client.get(
        f"/openbanking/data/v1/accounts/{aid}/standing_orders").json()["results"]}
    dds = {d["name"] for d in client.get(
        f"/openbanking/data/v1/accounts/{aid}/direct_debits").json()["results"]}
    assert not (sos & dds)


def test_pending_returns_todays_unsettled(client):
    client.post("/sim/set", json={"date": "2026-07-01"})
    aid = _main_account(client)
    pend = client.get(f"/openbanking/data/v1/accounts/{aid}/transactions/pending").json()["results"]
    assert pend and all(p["status"] == "pending" for p in pend)
    assert all(p["timestamp"][:10] == "2026-07-01" for p in pend)


def test_commitments_time_travel(client):
    """Standing orders are derived only from history visible at the clock."""
    client.post("/sim/set", json={"date": "2024-06-01"})   # barely any history
    aid = _main_account(client)
    early = client.get(f"/openbanking/data/v1/accounts/{aid}/standing_orders").json()["results"]
    client.post("/sim/set", json={"date": "2026-06-30"})
    late = client.get(f"/openbanking/data/v1/accounts/{aid}/standing_orders").json()["results"]
    assert len(late) >= len(early)
