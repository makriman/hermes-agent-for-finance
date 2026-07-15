"""The Cash Flow Engine — THE single forecast path (v5).

Reads line-item config, applies methods, overlays open invoices and the tax
modules, and recomputes the forecast on the fly. Deterministic; nothing but
config + data goes in. Every user-facing view (outlook, weekly, status, vat,
whatif, scenario, export, freeze) goes through compute() — one implementation
of opening balance, invoice supersession, tax scheduling and bucketing, so no
two commands can disagree about the same number.

Engine-owned parameters:
  horizon_months (display, default 3) · risk_horizon_months (verdict scan,
  default 6) · grain ('week'|'month') · materiality (display bucketing) ·
  cash_floor (owner's minimum) · vat_pot_account (explicit pot name) ·
  scenario overlay {scale, extra, drop, delay, items}.

`items` overlay = per-item param patches over the SAME base line items
("the hire starts in September instead") — scenarios as flavors, not forks.
"""
from __future__ import annotations

import statistics as stats
from collections import defaultdict
from datetime import date, timedelta

from . import methods as mt
from . import taxonomy as tax
from .actuals import build_enriched
from .client import Clients
from .forecast import (_contact_lateness, corp_tax_module, invoice_occurrences,
                       vat_module)
from .mapping import norm
from .models import Occurrence

DEFAULTS = {"horizon_months": 3, "risk_horizon_months": 6, "grain": "week",
            "materiality": 500.0, "cash_floor": 0.0, "vat_pot_account": None}


def _occ(source, category, label, counterparty, d: date, amount, conf) -> Occurrence:
    import hashlib
    oid = hashlib.md5(f"{source}:{label}:{d}:{amount}".encode()).hexdigest()[:12]
    return Occurrence(occ_id=oid, source=source, category=category, label=label,
                      counterparty=counterparty, expected_date=d.isoformat(),
                      amount=round(amount, 2), confidence=conf)


def _patched(it: dict, patches: dict) -> dict:
    """Apply a scenario per-item param patch (match by id or name)."""
    patch = patches.get(str(it["id"])) or patches.get(it["name"])
    if not patch:
        return it
    out = {**it, "params": dict(it["params"])}
    for k, v in patch.items():
        if k in ("start_date", "end_date", "method"):
            out[k] = v
        else:
            out["params"][k] = v
    return out


def compute(c: Clients, store, org: str, params: dict | None = None,
            scenario: dict | None = None) -> dict:
    """Recompute the multi-period forecast from config. Returns a dict with
    occurrences (display window), buckets+drivers, curve, low point/breach
    (scanned over the full risk horizon), tax blocks, verdict."""
    p = {**DEFAULTS, **(params or {})}
    ov = scenario or {}
    cfg = c.config()
    # `as_of` lets a caller compute from a HISTORICAL vantage point (never a
    # future one) — how freeze_month builds a true before-the-month baseline
    # even when the month is first viewed mid-flight.
    as_of = min(p.get("as_of") or cfg["now"], cfg["now"])
    anchor = date.fromisoformat(as_of)
    start_month = p.get("start_month") or (
        cfg["scenario_month"] if as_of <= cfg["anchor"]
        else anchor.strftime("%Y-%m"))
    horizon = int(p["horizon_months"])
    risk_n = max(horizon, int(p["risk_horizon_months"]))
    months = mt.month_iter(date.fromisoformat(start_month + "-01"), risk_n)
    display_months = months[:horizon]

    enriched, total_cash, vat_pot, mapper = build_enriched(
        c, anchor.isoformat(), store.rules(org), pot_name=p["vat_pot_account"])

    items = [it for it in store.items(org) if it["name"] not in set(ov.get("drop", []))]
    patches = ov.get("items", {})
    scale = ov.get("scale", {})

    # pass 1: non-linked items -> occurrences + per-category monthly series
    occs: list[Occurrence] = []
    cat_series: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    linked_items, sigmas = [], []
    for it in items:
        it = _patched(it, patches)
        if it["method"] == "linked":
            linked_items.append(it)
            continue
        if it["method"] == "recurring_variable":
            obs = (it["params"].get("observations") or [])[-int(
                it["params"].get("lookback_months", 3)):]
            if len(obs) >= 2:
                # spread AROUND the trend, not including it — the trend is the
                # forecast; only the residual is uncertainty
                icpt, slope = mt.trend_fit(obs)
                resid = [o - (icpt + slope * i) for i, o in enumerate(obs)]
                sigmas.append(stats.pstdev(resid))
        for d, amount in mt.project(it, months):
            amount *= scale.get(it["category"], 1.0)
            if abs(amount) < 1:
                continue
            occs.append(_occ("lineitem", it["category"], it["name"],
                             it["counterparty"], d, amount,
                             0.9 if it["method"] == "recurring_fixed" else 0.6))
            cat_series[it["category"]][d.strftime("%Y-%m")] += amount

    # pass 2: linked items (COGS = -30% of revenue, etc.)
    for it in linked_items:
        target = it["params"].get("target", "revenue")
        series = {ym: cat_series.get(target, {}).get(ym, 0.0) for ym in months}
        for d, amount in mt.project_linked(it, months, series):
            amount *= scale.get(it["category"], 1.0)
            if abs(amount) < 1:
                continue
            occs.append(_occ("linked", it["category"], it["name"],
                             it["counterparty"], d, amount, 0.5))

    # near-term overlays: open invoices supersede config items for their
    # (category, counterparty) in EVERY horizon month where an invoice is
    # expected to settle — a raised invoice beats a historical pattern.
    lateness = _contact_lateness(c)
    for cp, days in store.lateness(org).items():
        lateness[norm(cp)] = days
    # historical vantage point: invoices that settled after `as_of` were
    # still open then — include them so a retro-frozen plan stays honest
    paid_since = (anchor + timedelta(days=1)).isoformat() \
        if as_of < cfg["now"] else None
    inv: list[Occurrence] = []
    for ym in months:
        inv.extend(invoice_occurrences(c, ym, mapper, lateness,
                                       include_paid_since=paid_since))
    # dedupe: an invoice can only settle once — keep first expected occurrence
    seen_inv = set()
    inv = [o for o in inv if not (o.occ_id in seen_inv or seen_inv.add(o.occ_id))]
    inv_month_keys = {(o.expected_date[:7], o.category, norm(o.counterparty)) for o in inv}
    occs = [o for o in occs
            if (o.expected_date[:7], o.category, norm(o.counterparty)) not in inv_month_keys]
    for o in inv:   # scenario scales apply to invoice-backed flows too
        o.amount = round(o.amount * scale.get(o.category, 1.0), 2)
    occs.extend(inv)

    # tax modules: VAT (quarterly, accrual-first) + corporation tax (annual)
    vat_occs, vat = vat_module(enriched, months, anchor, vat_pot)
    occs.extend(vat_occs)
    ct_occs, corp = corp_tax_module(enriched, months, anchor)
    occs.extend(ct_occs)

    # what-if extras riding the scenario overlay
    for x in ov.get("extra", []):
        occs.append(_occ("assumption", x["category"],
                         f"{tax.label(x['category'])} — scenario", "",
                         date.fromisoformat(x["date"]), float(x["amount"]), 1.0))

    # what-if delays: shift matching occurrences by N days ("what if that
    # customer pays 30 days late?" / "what if I pay this bill next month?")
    delays = ov.get("delay", {})   # {label substring or category: days}
    if delays:
        shifted = []
        for o in occs:
            for key, days in delays.items():
                if key.lower() in o.label.lower() or key == o.category:
                    d_new = date.fromisoformat(o.expected_date) + timedelta(days=int(days))
                    o = _occ(o.source, o.category, o.label + f" (delayed {days}d)",
                             o.counterparty, d_new, o.amount, o.confidence)
                    break
            shifted.append(o)
        occs = shifted

    # Only FUTURE events belong in an outlook: when the clock sits inside the
    # first month, occurrences whose date has already passed are history — the
    # as-of balance already contains them (or their absence is a variance for
    # `reconcile`, not a projection).
    occs = [o for o in occs if o.expected_date > anchor.isoformat()]
    occs.sort(key=lambda o: (o.expected_date, o.occ_id))

    # buckets + curve over the DISPLAY window; low point + floor breach scan
    # continues across the FULL risk horizon (a December cliff must colour the
    # verdict even in a 3-month view).
    display_end = mt.clamp_day(display_months[-1], 31)
    risk_end = mt.clamp_day(months[-1], 31)
    by_day: dict[str, float] = defaultdict(float)
    for o in occs:
        by_day[o.expected_date] += o.amount
    curve, buckets = [], []
    bal = total_cash
    low = {"date": anchor.isoformat(), "balance": round(bal, 2)}
    breach = None            # FIRST day the balance crosses the cash floor
    floor = float(p["cash_floor"])
    net_all: list[float] = []       # per-week nets across the risk window (burn)
    week_net = 0.0
    bucket_net, bucket_start = 0.0, None
    display_net = display_close = None
    d = date.fromisoformat(months[0] + "-01")
    while d <= risk_end:
        day_amt = by_day.get(d.isoformat(), 0.0)
        bal += day_amt
        week_net += day_amt
        if breach is None and bal < floor:
            breach = {"date": d.isoformat(), "balance": round(bal, 2)}
        if bal < low["balance"]:
            low = {"date": d.isoformat(), "balance": round(bal, 2)}
        if d.weekday() == 6 or d == risk_end:
            net_all.append(week_net)
            week_net = 0.0
        if d <= display_end:
            if bucket_start is None:
                bucket_start = d
            bucket_net += day_amt
            curve.append({"date": d.isoformat(), "balance": round(bal, 2)})
            boundary = (p["grain"] == "week" and (d.weekday() == 6 or d == display_end)) or \
                       (p["grain"] == "month" and (d + timedelta(days=1)).day == 1) or \
                       d == display_end
            if boundary:
                buckets.append({"from": bucket_start.isoformat(), "to": d.isoformat(),
                                "net": round(bucket_net, 2), "close": round(bal, 2)})
                bucket_net, bucket_start = 0.0, None
            if d == display_end:
                display_close = round(bal, 2)
        d += timedelta(days=1)

    display_occs = [o for o in occs if o.expected_date[:7] <= display_months[-1]]
    projected_net = round(sum(o.amount for o in display_occs), 2)
    display_close = display_close if display_close is not None \
        else round(total_cash + projected_net, 2)

    # per-bucket drivers: the 1-2 largest movements inside a materially busy
    # bucket — the "why does this week look different" answer, inline.
    mat = float(p["materiality"])
    for b in buckets:
        if abs(b["net"]) < 2 * mat:
            b["drivers"] = []
            continue
        inside = [o for o in display_occs if b["from"] <= o.expected_date <= b["to"]]
        inside.sort(key=lambda o: -abs(o.amount))
        b["drivers"] = [{"label": o.label, "amount": o.amount,
                         "date": o.expected_date}
                        for o in inside[:2] if abs(o.amount) >= mat / 2]

    burn = [n for n in net_all if n < 0]
    avg_weekly_burn = abs(sum(burn) / max(len(burn), 1))

    # forecast uncertainty from the observed spread of the variable items —
    # communicated, never used to change the numbers.
    sigma = round(sum(s ** 2 for s in sigmas) ** 0.5, 2) if sigmas else 0.0

    # the "am I okay?" verdict — scanned over the full risk horizon; the
    # danger date is the FIRST breach of the owner's floor, not the trough.
    floor_txt = f"your £{floor:,.0f} floor" if floor > 0 else "zero"
    trough = ("-£{:,.0f}".format(abs(low["balance"])) if low["balance"] < 0
              else "£{:,.0f}".format(low["balance"]))
    beyond = ""
    if breach and breach["date"][:7] > display_months[-1]:
        shown = "month" if horizon == 1 else f"{horizon} months"
        beyond = f" (beyond the {shown} shown)"
    if breach:
        verdict = ("🔴", f"projected to drop below {floor_txt} on {breach['date']}"
                          f"{beyond} — trough {trough} on {low['date']}")
    elif vat["underfunded"] and vat.get("projected_in_month"):
        verdict = ("🔴", f"VAT of £{vat['due_estimate']:,.0f} due with pot "
                          f"£{vat['shortfall']:,.0f} short")
    elif low["balance"] < floor + 2 * avg_weekly_burn:
        verdict = ("🟡", "cash stays above the floor but the buffer is thin — look closer")
    elif vat["underfunded"]:
        verdict = ("🟡", f"cash is fine but the VAT pot is £{vat['shortfall']:,.0f} "
                          f"short of the next bill (~£{vat['due_estimate']:,.0f}"
                          + (f" due {vat['next_due']}" if vat.get("next_due") else "")
                          + ")")
    else:
        verdict = ("🟢", "you're okay — cash stays comfortably positive"
                          f" for the next {risk_n} months")

    from . import lineitems as li
    watch = li.watch_list(enriched, months[0], threshold=max(mat / 8, 250.0))

    return {
        "org": org, "months": display_months, "risk_months": months,
        "grain": p["grain"], "anchor": anchor.isoformat(),
        "opening_balance": round(total_cash, 2), "vat_pot": vat_pot,
        "occurrences": display_occs, "projected_net": projected_net,
        "close": display_close,
        "low_point": low, "breach": breach, "cash_floor": floor,
        "buckets": buckets, "curve": curve, "sigma": sigma,
        "vat": vat, "corp_tax": corp, "verdict": verdict, "materiality": mat,
        "excluded": watch, "txn_count": len(enriched),
        "item_count": len(items),
    }


def freeze_month(c: Clients, store, org: str,
                 month: str | None = None) -> "ForecastVersion":
    """Freeze `month` (default: the current one) as an immutable
    ForecastVersion — the baseline actual-vs-forecast variance is judged
    against.

    The plan is always computed AS OF the day before the month starts, even
    when the freeze happens mid-month (fresh install, month rollover caught
    late): a baseline created after the month's shocks would score itself
    against a plan that already knew the answer."""
    from .models import ForecastVersion
    from datetime import datetime, timedelta
    cfg = c.config()
    month = month or (cfg["scenario_month"] if cfg["now"] <= cfg["anchor"]
                      else cfg["now"][:7])
    eve = (date.fromisoformat(month + "-01") - timedelta(days=1)).isoformat()
    r = compute(c, store, org, params={"horizon_months": 1,
                                       "risk_horizon_months": 1, "grain": "week",
                                       "as_of": eve, "start_month": month})
    return ForecastVersion(
        org=org, month=r["months"][0], anchor=r["anchor"],
        created_at=datetime.now().isoformat(timespec="seconds"),
        params={"source": "config", "grain": "week"},
        opening_balance=r["opening_balance"],
        operating_balance=round(r["opening_balance"] - r["vat_pot"], 2),
        vat_pot=r["vat_pot"], occurrences=r["occurrences"],
        projected_net=r["projected_net"], forecast_close=r["close"],
        low_point=r["low_point"],
        weekly=[{"week": i + 1, **b} for i, b in enumerate(r["buckets"])],
        vat=r["vat"], excluded=r["excluded"],
    )
