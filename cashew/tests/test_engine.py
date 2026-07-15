"""Engine v5 tests — against the live mock server on :8900.

v5 has ONE forecast path: seed line-item config with config_sync.sync(), then
cp.compute() (forecast.build was deleted). Reconciliation runs against a
frozen ForecastVersion from cp.freeze_month().
"""
from __future__ import annotations

import pytest

from engine import compute as cp
from engine import config_sync
from engine import lineitems as li
from engine import reconcile as rc
from engine.actuals import build_enriched
from engine.client import Clients
from engine.mapping import norm
from engine.store import Store

BASE = "http://127.0.0.1:8900"
ORG = "jam-scn-1"


@pytest.fixture(scope="module")
def c():
    cl = Clients(BASE)
    cl.set_org(ORG)
    cl.reset()
    return cl


@pytest.fixture()
def anchored(c):
    """Sim pinned to the canonical anchor (2026-06-30) before AND after each
    test — reconcile tests move the clock and must not leak that state."""
    c.set_org(ORG)
    c.reset()
    yield c
    c.set_org(ORG)
    c.reset()


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


@pytest.fixture()
def synced(anchored, store):
    """A throwaway config store seeded from actuals at the anchor."""
    cfg = anchored.config()
    config_sync.sync(anchored, store, ORG, cfg["scenario_month"], cfg["anchor"])
    return store


def _detected(anchored):
    cfg = anchored.config()
    enriched, _, _, _ = build_enriched(anchored, cfg["anchor"])
    return li.detect(enriched, cfg["scenario_month"], residual_floor=250.0)


# --- mapping / actuals analysis ------------------------------------------------

def test_mapping_accuracy_without_labels(anchored):
    """The engine must classify from GL join + learned rules alone (no leak)."""
    cfg = anchored.config()
    enriched, _, _, _ = build_enriched(anchored, cfg["anchor"])
    truth = anchored._get(f"{anchored.sim}/truth/labels")["labels"]
    scored = [(t.category, truth[t.txn_id]) for t in enriched if t.txn_id in truth]
    acc = sum(1 for got, want in scored if got == want) / len(scored)
    assert acc >= 0.95, f"mapping accuracy {acc:.1%} below 95%"


def test_bank_feed_has_no_labels(anchored):
    accts = anchored.accounts()
    txns = anchored.transactions(accts[0]["account_id"])
    assert all("cashew_category" not in t.get("meta", {}) for t in txns)


def test_owner_rule_takes_priority(anchored):
    cfg = anchored.config()
    rule = [{"pattern": "hmrc", "category": "tax_paye"}]
    enriched, _, _, _ = build_enriched(anchored, cfg["anchor"], owner_rules=rule)
    hmrc = [t for t in enriched if "hmrc" in t.counterparty.lower()]
    assert hmrc and all(t.category == "tax_paye" and t.map_source == "owner" for t in hmrc)


# --- line items -----------------------------------------------------------------

def test_line_items_detect_known_cadences(anchored):
    items, _, _ = _detected(anchored)
    by_key = {i.key: i for i in items}
    # NatWest loan: perfectly regular on the 17th
    loan = by_key.get("loan_repayment|nat west bounce ba")
    assert loan and loan.kind == "regular_monthly" and abs(loan.day_of_month - 17) <= 1
    # payroll people: regular at month end
    payroll = [i for i in items if i.category == "payroll" and i.kind == "regular_monthly"]
    assert len(payroll) >= 3


def test_residuals_never_oppose_category_direction(anchored):
    """v5 drops a residual outright when it would oppose the category's own
    run-rate direction; the kept ones must close the gap to the run-rate."""
    items, run_rate, _ = _detected(anchored)
    resids = [i for i in items if i.kind == "residual"]
    assert resids
    for it in resids:
        rate = run_rate[it.category]
        named = sum(i.monthly_amount for i in items
                    if i.category == it.category and i.kind != "residual")
        assert it.monthly_amount * rate >= 0     # never flips the category's sign
        assert round(named + it.monthly_amount, 2) == pytest.approx(rate, abs=0.05)


# --- compute (the single forecast path) -------------------------------------------

def test_forecast_math_consistent(anchored, synced):
    r = cp.compute(anchored, synced, ORG)
    assert round(r["opening_balance"] + r["projected_net"], 2) == r["close"]
    assert round(sum(o.amount for o in r["occurrences"]), 2) == r["projected_net"]
    assert r["buckets"] and r["buckets"][-1]["close"] == r["close"]
    assert r["low_point"]["balance"] <= r["opening_balance"]


def test_forecast_deterministic(anchored, synced):
    a = cp.compute(anchored, synced, ORG)
    b = cp.compute(anchored, synced, ORG)
    assert a["projected_net"] == b["projected_net"]
    assert [o.occ_id for o in a["occurrences"]] == [o.occ_id for o in b["occurrences"]]


def test_invoices_supersede_line_items(anchored, synced):
    """An open invoice beats the historical pattern for its (month, category,
    counterparty) — the same cash must never be forecast twice."""
    r = cp.compute(anchored, synced, ORG)
    inv_keys = {(o.expected_date[:7], o.category, norm(o.counterparty))
                for o in r["occurrences"] if o.source.startswith("invoice")}
    li_keys = {(o.expected_date[:7], o.category, norm(o.counterparty))
               for o in r["occurrences"]
               if o.source == "lineitem" and o.counterparty}
    assert inv_keys and not (inv_keys & li_keys)


def test_lumpy_categories_excluded(anchored, synced):
    r = cp.compute(anchored, synced, ORG)
    cats = {o.category for o in r["occurrences"]}
    assert "directors_drawings" not in cats
    assert "transfers_internal" not in cats
    assert any(e["category"] == "directors_drawings" for e in r["excluded"])


def test_vat_underfunding_flagged(anchored, synced):
    """jam-scn-1 at the 2026-06-30 anchor: est. liability well above the pot."""
    r = cp.compute(anchored, synced, ORG)
    vat = r["vat"]
    assert vat["underfunded"] and vat["shortfall"] > 0
    assert vat["pot"] > 0                          # from the real VAT Pot account
    assert vat["due_estimate"] == pytest.approx(vat["pot"] + vat["shortfall"], abs=0.02)
    # UK rule: period end + 1 month + 7 days (last paid 2026-05-11 -> period
    # ends 2026-07-31 -> due 2026-09-07)
    assert vat["next_due"] == "2026-09-07"


def test_assumption_changes_forecast(anchored, synced):
    """An assumption is a one_off owner line item; compute reflects it 1:1."""
    base = cp.compute(anchored, synced, ORG)
    synced.upsert_item(ORG, "Planned dividend", "directors_drawings", "one_off",
                       {"amount": -80000, "date": "2026-07-01"},
                       source="owner", locked=1, note="planned dividend")
    with_div = cp.compute(anchored, synced, ORG)
    assert with_div["projected_net"] == round(base["projected_net"] - 80000, 2)
    assert with_div["low_point"]["balance"] < base["low_point"]["balance"]


def test_whatif_scale(anchored, synced):
    base = cp.compute(anchored, synced, ORG)
    halved = cp.compute(anchored, synced, ORG, scenario={"scale": {"revenue": 0.5}})
    base_rev = sum(o.amount for o in base["occurrences"]
                   if o.category == "revenue" and o.source == "lineitem")
    alt_rev = sum(o.amount for o in halved["occurrences"]
                  if o.category == "revenue" and o.source == "lineitem")
    assert base_rev > 0
    assert round(alt_rev, 2) == round(base_rev * 0.5, 2)


# --- config sync: emerging patterns ---------------------------------------------------

def test_sync_surfaces_emerging_pattern_as_suggestion(anchored, synced):
    """CloudCanvas Hosting (~£89.93/mo from 2026-07-05) is too young for
    detection — once the clock passes its second occurrence it must surface as
    a *suggestion* (advice only, never auto-forecast), while the same-sized
    one-off noise counterparty must not."""
    anchored.set_date("2026-08-10")
    cfg = anchored.config()
    rep = config_sync.sync(anchored, synced, ORG, cfg["now"][:7], cfg["now"])
    truth = anchored._get(f"{anchored.sim}/truth/emerging")
    rec = truth["new_recurring"][0]
    noise_cp = truth["noise"][0]["counterparty"]
    by_cp = {s["counterparty"]: s for s in rep["suggestions"]}
    assert rec["counterparty"] in by_cp
    assert abs(by_cp[rec["counterparty"]]["monthly_amount"]) == pytest.approx(
        rec["monthly_amount"], abs=0.01)
    assert noise_cp not in by_cp
    # a suggestion is not a forecast: no line item may have been created for it
    assert not any(rec["counterparty"].lower() in (it["counterparty"] or "").lower()
                   for it in synced.items(ORG))


# --- reconciliation ----------------------------------------------------------------

@pytest.fixture()
def frozen(anchored, synced):
    """The 2026-07 baseline, frozen at the anchor BEFORE the clock moves."""
    fv = cp.freeze_month(anchored, synced, ORG)
    assert fv.month == "2026-07"
    return fv


def _run_recon(anchored, synced, fv, as_of):
    anchored.set_date(as_of)          # engine never peeks past the clock
    return rc.run(anchored, fv, as_of=as_of, owner_rules=synced.rules(ORG))


def test_reconcile_finds_dividend_surprise(anchored, synced, frozen):
    recon = _run_recon(anchored, synced, frozen, "2026-07-31")
    # multiple drawings surprises can exist (recurring small draws + the big
    # dividend) — the BIG one must be among them
    draws = [s["amount"] for s in recon["surprises"]
             if s["category"] == "directors_drawings"]
    assert draws and min(draws) <= -50000


def test_reconcile_matches_invoices_on_track(anchored, synced, frozen):
    recon = _run_recon(anchored, synced, frozen, "2026-07-31")
    ar = [l for l in recon["lines"] if l["occ"]["source"] == "invoice_ar"]
    assert ar and any(l["status"] == "on_track" for l in ar)


def test_reconcile_midmonth_is_honest(anchored, synced, frozen):
    """Mid-month, expected-to-date must exclude future occurrences."""
    recon = _run_recon(anchored, synced, frozen, "2026-07-10")
    future = sum(o.amount for o in frozen.occurrences
                 if o.expected_date > "2026-07-10")
    assert abs(recon["expected_to_date"] - (frozen.projected_net - future)) < 1500


def test_reconcile_emits_lessons_and_lateness(anchored, synced, frozen):
    recon = _run_recon(anchored, synced, frozen, "2026-07-31")
    assert recon["lessons"]                        # dividend + VAT lessons
    assert isinstance(recon["lateness_observations"], list)


def test_cash_consistency(anchored, synced, frozen):
    """Cash reported at month end equals opening + all month movements."""
    recon = _run_recon(anchored, synced, frozen, "2026-07-31")
    assert round(recon["cash_now"], 0) == round(
        frozen.opening_balance + recon["actual_net_to_date"], 0)
