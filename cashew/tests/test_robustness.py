"""v5.1 robustness regressions — found by reading the first two days of
production digests.

1. A baseline frozen AFTER the month started (fresh install, rollover caught
   late) must still be a true before-the-month plan (retro-freeze), including
   invoices that settled between the month's start and the freeze moment.
2. The daily digest alarms only on NEW events, not the whole month's shocks
   every morning.
3. status / daily / weekly / reconcile share ONE verdict-escalation rule.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from engine import compute as cp
from engine import config_sync
from engine import reconcile as rc
from engine.cli import main as cli_main
from engine.client import Clients

ORG = "jam-scn-1"
BASE = "http://127.0.0.1:8900"


@pytest.fixture()
def c():
    cl = Clients(BASE)
    cl.set_org(ORG)
    cl.reset()
    yield cl
    cl.set_org(ORG)
    cl.reset()


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("CASHEW_ENGINE_DB", str(tmp_path / "t.db"))
    from engine.store import Store
    s = Store(tmp_path / "t.db")
    yield s
    s.close()


def _seed(c, store):
    cfg = c.config()
    month = cfg["scenario_month"] if cfg["now"] <= cfg["anchor"] else cfg["now"][:7]
    config_sync.sync(c, store, ORG, month, cfg["now"])


def test_retro_freeze_is_a_true_before_the_month_plan(c, store):
    """Freezing July on Jul-3 (after the Jul-1 shocks) must equal a plan made
    on Jun-30: pre-shock opening cash, and the invoices that settled on
    Jul-1 still inside the plan."""
    c.set_date("2026-07-03")
    _seed(c, store)
    fv = cp.freeze_month(c, store, ORG, month="2026-07")
    assert fv.anchor == "2026-06-30"
    # opening cash is the PRE-shock balance (Jul-1 dropped it by ~£110k)
    assert fv.opening_balance > 100_000
    # the Jul-1 settled invoices are plan occurrences, not future surprises
    early = [o for o in fv.occurrences if o.expected_date <= "2026-07-03"
             and o.source in ("invoice_ar", "invoice_ap")]
    assert early, "invoices settled between month start and freeze must be in the plan"


def test_retro_frozen_plan_reconciles_without_phantom_surprises(c, store):
    """Against a retro-frozen plan, the only surprises on Jul-2 are the
    genuinely unplanned events (dividend, early VAT) — settled invoices
    match plan lines instead of masquerading as surprises."""
    c.set_date("2026-07-02")
    _seed(c, store)
    fv = cp.freeze_month(c, store, ORG, month="2026-07")
    recon = rc.run(c, fv, owner_rules=[], materiality=6000)
    cats = {s["category"] for s in recon["surprises"]}
    assert "directors_drawings" in cats          # the £80k dividend
    assert "suppliers_cogs" not in cats, \
        "settled supplier bill must match its plan line, not be a surprise"
    # expected-to-date includes the settled invoice flows
    assert recon["expected_to_date"] != 0.0


def test_daily_alarms_only_on_new_events(c, store, capsys, monkeypatch):
    monkeypatch.setattr("engine.store.DB_PATH",
                        Path(tempfile.mkdtemp()) / "cli.db")
    c.set_date("2026-07-02")
    rcode = cli_main(["daily"])
    out = capsys.readouterr().out
    assert rcode == 0
    assert "New since yesterday" in out
    assert "Director drawings" in out
    # days later, the Jul-1 shocks must NOT re-alarm
    c.set_date("2026-07-06")
    rcode = cli_main(["daily"])
    out = capsys.readouterr().out
    assert rcode == 0
    assert "Director drawings" not in out.split("month so far")[-1] \
        or "Nothing new" in out


def test_verdict_escalation_parity_across_commands(c, store, capsys, monkeypatch):
    """When materially behind plan, daily/weekly/reconcile/status all open 🔴."""
    monkeypatch.setattr("engine.store.DB_PATH",
                        Path(tempfile.mkdtemp()) / "cli.db")
    c.set_date("2026-07-02")
    firsts = {}
    for cmd in ("status", "daily", "weekly", "reconcile"):
        assert cli_main([cmd]) == 0
        out = capsys.readouterr().out.strip().splitlines()
        # verdict is the first line carrying a traffic light
        light = next((l for l in out if any(i in l for i in "🔴🟡🟢")), "")
        firsts[cmd] = "🔴" if "🔴" in light else ("🟡" if "🟡" in light else "🟢")
    assert len(set(firsts.values())) == 1, f"verdicts disagree: {firsts}"
    assert firsts["status"] == "🔴"


def test_rollover_freeze_is_anchored_to_month_eve(c, store):
    """An August baseline first created on Aug-5 anchors at Jul-31 and still
    plans the early-August occurrences that have already happened."""
    c.set_date("2026-08-05")
    _seed(c, store)
    fv = cp.freeze_month(c, store, ORG, month="2026-08")
    assert fv.anchor == "2026-07-31"
    assert fv.month == "2026-08"
    early = [o for o in fv.occurrences if o.expected_date < "2026-08-05"]
    assert early, "already-elapsed August plan days must still be in the plan"
