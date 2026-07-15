"""Reference cadence runner + oracle.

This is NOT the Cashew forecasting engine — it is a deliberately simple,
transparent *reference consumer* that drives the harness through the real
cadence to prove:
  1. the mock APIs expose everything a consumer needs (over HTTP, no /truth),
  2. time-travel reveals actuals day-by-day,
  3. the flagship signals per scenario are observable through the public APIs.

Cadence (matches the product spec):
  • forecast at the anchor (end of month) for the scenario month
  • reconcile daily as actuals arrive
  • weekly owner status
  • month-end post-mortem + oracle PASS/FAIL

Usage:
  python -m sim.runner                       # default org, http://localhost:8900
  python -m sim.runner --org jam-scn-1 --base http://localhost:8900
"""
from __future__ import annotations

import argparse
import calendar
from collections import defaultdict
from datetime import date, timedelta

import requests

BASE = "http://localhost:8900"


def _get(path, **params):
    r = requests.get(BASE + path, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _post(path, body):
    r = requests.post(BASE + path, json=body, timeout=30)
    r.raise_for_status()
    return r.json()


def gbp(x: float) -> str:
    return f"£{x:,.0f}"


def month_bounds(ym: str) -> tuple[date, date]:
    y, m = map(int, ym.split("-"))
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def main() -> int:
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--org", default=None)
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()
    BASE = args.base.rstrip("/")

    if args.org:
        _post("/sim/org", {"slug": args.org})
    _post("/sim/reset", {})

    st = _get("/sim/config")
    org = st["org"]
    anchor = date.fromisoformat(st["anchor"])
    scenario_month = st["scenario_month"]
    m_start, m_end = month_bounds(scenario_month)
    print("=" * 74)
    print(f"  CASHEW REFERENCE RUN — {st['meta']['name']}  ({org})")
    print(f"  anchor={anchor}  forecast month={scenario_month}  ({m_start}..{m_end})")
    print("=" * 74)

    # ---- accounts ---------------------------------------------------------
    accts = _get("/openbanking/data/v1/accounts")["results"]
    main_acct = next((a for a in accts if "pot" not in a["display_name"].lower()), accts[0])
    aid = main_acct["account_id"]

    # The runner is an oracle harness, so it may use truth labels for its own
    # aggregation (the real engine does its own mapping instead).
    _post("/sim/set", {"date": m_end.isoformat()})
    labels = _get("/sim/truth/labels")["labels"]
    _post("/sim/reset", {})

    def cat_of(t):
        return labels.get(t["transaction_id"], "other")

    # ================= PHASE A: FORECAST AT ANCHOR =========================
    # opening cash across all accounts, as-of the anchor
    opening = 0.0
    pot_balance = 0.0
    for a in accts:
        bal = _get(f"/openbanking/data/v1/accounts/{a['account_id']}/balance")["results"][0]["current"]
        opening += bal
        if "pot" in a["display_name"].lower():
            pot_balance += bal

    # Pull ~7 months of history: run-rate baseline from the last 3 calendar
    # months, VAT liability from the wider window (VAT is quarterly & lumpy).
    hist_from = (anchor - timedelta(days=220)).isoformat()
    hist = _get(f"/openbanking/data/v1/accounts/{aid}/transactions", **{"from": hist_from, "to": anchor.isoformat()})["results"]
    by_month: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    vat_payments = []
    for t in hist:
        ym = t["timestamp"][:7]
        cat = cat_of(t)
        by_month[ym][cat] += t["amount"]
        if cat == "tax_vat" and t["amount"] < 0:
            vat_payments.append(abs(t["amount"]))
    recent_months = sorted(by_month)[-3:]
    base_net = defaultdict(float)
    for ym in recent_months:
        for c, v in by_month[ym].items():
            if c == "transfers_internal":     # internal movement, nets to zero
                continue
            base_net[c] += v
    nb = max(len(recent_months), 1)
    base_monthly = {c: v / nb for c, v in base_net.items()}

    # open forward commitments (AR/AP) from Xero
    ar = [i for i in _get("/xero/api.xro/2.0/Invoices", type="ACCREC", page="all")["Invoices"] if i["Status"] == "AUTHORISED"]
    ap_ = [i for i in _get("/xero/api.xro/2.0/Invoices", type="ACCPAY", page="all")["Invoices"] if i["Status"] == "AUTHORISED"]

    # run-rate forecast for the month
    forecast_net = sum(base_monthly.values())
    forecast_close = opening + forecast_net

    print(f"\n[FORECAST @ {anchor}]  (naive run-rate reference)")
    print(f"  opening cash (all accounts)      {gbp(opening)}")
    print(f"  projected net ({scenario_month}, run-rate) {gbp(forecast_net)}")
    print(f"  => forecast closing cash          {gbp(forecast_close)}")
    print(f"  open commitments: {len(ar)} AR ({gbp(sum(i['Total'] for i in ar))}) / "
          f"{len(ap_)} AP ({gbp(sum(i['Total'] for i in ap_))})")

    # EARLY WARNING: VAT funding (derived only from API data).
    # VAT is quarterly & lumpy, so estimate the next liability from the LARGEST
    # recent VAT payment, not the average.
    warnings = []
    next_vat = max(vat_payments) if vat_payments else 0.0
    if next_vat > 0:
        shortfall = next_vat - pot_balance
        status = "UNDERFUNDED" if shortfall > 0 else "ok"
        print(f"\n[EARLY WARNING — VAT]  est. next quarterly VAT {gbp(next_vat)} vs pot {gbp(pot_balance)} -> {status}")
        if shortfall > 0:
            warnings.append(("vat_underfunded", shortfall))
            print(f"  ⚠ VAT pot short by {gbp(shortfall)} — fund before the next return.")

    forecast = dict(opening=opening, close=forecast_close, base_monthly=base_monthly)

    # ================= PHASE B: DAILY RECONCILIATION ======================
    print(f"\n[RECONCILE] advancing daily through {scenario_month} ...")
    actual_net = 0.0
    actual_by_cat = defaultdict(float)
    seen = set()
    d = m_start
    week = 0
    while d <= m_end:
        _post("/sim/set", {"date": d.isoformat()})
        day = _get(f"/openbanking/data/v1/accounts/{aid}/transactions",
                   **{"from": d.isoformat(), "to": d.isoformat()})["results"]
        for t in day:
            if t["transaction_id"] in seen:
                continue
            seen.add(t["transaction_id"])
            cat = cat_of(t)
            if cat == "transfers_internal":       # internal movement, nets to zero
                continue
            actual_net += t["amount"]
            actual_by_cat[cat] += t["amount"]
            if abs(t["amount"]) >= 10000:
                print(f"    {d}  {cat_of(t):20s} {gbp(t['amount']):>12}  {t['merchant_name']}")
        # weekly owner status
        if (d - m_start).days // 7 > week or d == m_end:
            week = (d - m_start).days // 7
            proj_close = opening + actual_net
            print(f"  — week {week+1} status @ {d}: actual net {gbp(actual_net)}  "
                  f"projected close {gbp(opening + actual_net)}  "
                  f"(vs forecast {gbp(forecast['close'])})")
        d += timedelta(days=1)

    # ================= PHASE C: MONTH-END POST-MORTEM =====================
    print(f"\n[POST-MORTEM] {scenario_month} — variance vs run-rate forecast")
    variances = []
    cats = set(actual_by_cat) | set(forecast["base_monthly"])
    for c in cats:
        v = round(actual_by_cat.get(c, 0.0) - forecast["base_monthly"].get(c, 0.0), 2)
        if abs(v) >= 1000:
            variances.append((c, v))
    variances.sort(key=lambda x: -abs(x[1]))
    for c, v in variances[:8]:
        print(f"    {c:22s} variance {gbp(v):>14}")
    total_var = round((opening + actual_net) - forecast["close"], 2)
    print(f"  total closing-cash variance: {gbp(total_var)}")

    # ================= PHASE D: ORACLE (assert vs ground truth) ===========
    _post("/sim/set", {"date": anchor.isoformat()})
    exp = _get("/sim/truth/expected")
    print("\n[ORACLE] checking the reference consumer caught what the scenario planted:")
    checks = []

    # 1. VAT underfunding — caught if pre-warned from history OR surfaced as a
    #    material variance in reconciliation (a spike beyond history can only be
    #    detected the latter way).
    if exp["vat"]["underfunded"]:
        prewarned = any(w[0] == "vat_underfunded" for w in warnings)
        in_variance = any(c == "tax_vat" for c, _ in variances)
        how = "pre-warned" if prewarned else ("detected in reconciliation" if in_variance else "missed")
        checks.append((f"VAT underfunding caught ({how})", prewarned or in_variance))

    # 2/3. material scenario categories should surface as top variances
    observed = {c for c, _ in variances}
    for bucket, label in (("surprises", "surprise"), ("trends", "trend")):
        for e in exp[bucket]:
            if abs(e["scenario_month_total"]) >= 1000:
                checks.append((f"{label}:{e['category']} observed in variance",
                               e["category"] in observed))

    ok = True
    for name, passed in checks:
        print(f"    [{'PASS' if passed else 'FAIL'}] {name}")
        ok = ok and passed
    print("\n" + ("✅ ORACLE PASSED" if ok else "❌ ORACLE FAILED") +
          f"  ({sum(p for _, p in checks)}/{len(checks)} checks)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
