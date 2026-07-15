"""Sync — turn detected patterns into editable line-item config, safely.

Rules of the merge (the "recategorization risk" answer):
  * owner/agent items and anything `locked` are NEVER auto-touched
  * detected items are refreshed in place (params follow the data)
  * a detected item that stops appearing is flagged, not deleted
  * genuinely NEW patterns (too young for detection) become *suggestions*
    for the owner — the recency-bias answer: novelty needs a human yes.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date

from . import lineitems as li
from .actuals import build_enriched
from .client import Clients
from .mapping import norm
from .models import EnrichedTxn


def _item_name(det: "li.LineItem") -> str:
    return det.label


def detected_to_config(det: "li.LineItem") -> dict:
    """Translate a detected pattern into a config row (PRD method vocabulary).
    The detection evidence (basis, confidence) rides along in params so the
    items view can show WHY each number is what it is."""
    if det.kind in ("regular_monthly", "residual"):
        # residuals are flat aggregates by construction — fixed, not trended
        method = "recurring_fixed"
        params = {"amount": det.monthly_amount, "day_of_month": det.day_of_month}
    else:  # variable_monthly -> trend over its observation series
        method = "recurring_variable"
        params = {"observations": [det.monthly_amount], "lookback_months": 3,
                  "day_of_month": det.day_of_month}
    params["basis"] = det.basis
    params["confidence"] = det.confidence
    return {"name": _item_name(det), "category": det.category,
            "counterparty": det.counterparty, "method": method, "params": params}


def refresh_observations(enriched: list[EnrichedTxn], before_month: str,
                         months_back: int = 6) -> dict[tuple[str, str], list[float]]:
    """(category, counterparty_norm) -> per-month totals, oldest first —
    the observation series feeding recurring_variable items. A hollow
    trailing month (mid-month data cut) is excluded — see li.observed_months."""
    months = li.observed_months(enriched, before_month)[-months_back:]
    agg: dict[tuple[str, str], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for t in enriched:
        ym = t.date.strftime("%Y-%m")
        if ym in months:
            agg[(t.category, norm(t.counterparty))][ym] += t.amount
    return {k: [round(v.get(m, 0.0), 2) for m in months] for k, v in agg.items()}


def sync(c: Clients, store, org: str, month: str, as_of: str,
         materiality: float = 2000.0) -> dict:
    """Detect from actuals and merge into the config store. Returns a report:
    {added, refreshed, skipped_locked, gone, quiet, suggestions}."""
    enriched, _, _, _ = build_enriched(c, as_of, store.rules(org))
    detected, run_rate, base_months = li.detect(
        enriched, month, residual_floor=max(materiality / 4, 250.0))
    obs = refresh_observations(enriched, month)

    existing = {it["name"]: it for it in store.items(org, include_inactive=True)}
    seen_names = set()
    report = {"added": [], "refreshed": [], "skipped_locked": [], "gone": [],
              "quiet": [], "suggestions": []}

    for det in detected:
        cfg = detected_to_config(det)
        key = (det.category, norm(det.counterparty))
        if cfg["method"] == "recurring_variable" and key in obs:
            cfg["params"]["observations"] = obs[key]
        name = cfg["name"]
        seen_names.add(name)
        prior = existing.get(name)
        if prior and prior["locked"]:
            report["skipped_locked"].append(name)
            continue
        store.upsert_item(org, name, cfg["category"], cfg["method"], cfg["params"],
                          counterparty=cfg["counterparty"], source="detected",
                          note=f"sync as of {as_of}")
        report["refreshed" if prior else "added"].append(name)

    # detected items that vanished from the data
    for name, it in existing.items():
        if it["source"] == "detected" and it["active"] and not it["locked"] \
                and name not in seen_names:
            report["gone"].append(name)

    # quiet counterparties: an ACTIVE item (owner-locked or not) whose
    # counterparty was absent from BOTH of the last two fully-observed months
    # is probably over — propose an end date, never auto-apply one. (Judged on
    # observed full months so a hollow trailing month or a young current month
    # can't cry wolf.)
    last_seen: dict[str, str] = {}
    for t in enriched:
        cp = norm(t.counterparty)
        if cp:
            d = t.date.isoformat()
            if d > last_seen.get(cp, ""):
                last_seen[cp] = d
    full_months = li.observed_months(enriched, as_of[:7])
    quiet_cutoff = full_months[-2] + "-01" if len(full_months) >= 2 else "1970-01-01"
    for it in store.items(org):
        cp = norm(it["counterparty"])
        if not cp or it["end_date"] or it["method"] == "one_off":
            continue
        seen = last_seen.get(cp)
        if seen and seen < quiet_cutoff:
            report["quiet"].append({"name": it["name"], "id": it["id"],
                                    "last_seen": seen,
                                    "proposal": f"end it as of {seen}?"})

    # emerging patterns: too young for detection (2 similar events in the last
    # 2 months, INCLUDING the month in progress — novelty should surface with
    # a 1-month lag, not 2) — suggestions only, never auto-forecast. Novelty
    # needs a human yes (the recency-bias answer); a same-size one-off won't
    # qualify because it has no second occurrence.
    latest = sorted({t.date.strftime("%Y-%m") for t in enriched
                     if t.date.strftime("%Y-%m") <= as_of[:7]})[-2:]
    # group by counterparty ONLY: the newest occurrence often sits inside the
    # bookkeeping-lag window still unmapped, and splitting on category would
    # hide exactly the pair we're looking for
    young: dict[str, list[EnrichedTxn]] = defaultdict(list)
    for t in enriched:
        if t.date.strftime("%Y-%m") in latest:
            young[norm(t.counterparty)].append(t)
    known_cps = {norm(d.counterparty) for d in detected if d.counterparty}
    known_cps |= {norm(it["counterparty"])
                  for it in store.items(org) if it["counterparty"]}
    for cp, txns in young.items():
        if not cp or cp in known_cps or len(txns) != 2:
            continue
        a, b = abs(txns[0].amount), abs(txns[1].amount)
        similar = min(a, b) >= 0.65 * max(a, b)   # roughly the same size twice
        if len({t.date.strftime("%Y-%m") for t in txns}) == 2 and similar \
                and abs(sum(x.amount for x in txns)) >= max(150.0, materiality / 40):
            cats = [t.category for t in txns if t.category != "unmapped"]
            report["suggestions"].append({
                "category": cats[0] if cats else "unmapped",
                "counterparty": txns[-1].counterparty,
                "monthly_amount": round(sum(x.amount for x in txns) / 2, 2),
                "day_of_month": txns[-1].date.day,
                "reason": "seen in each of the last 2 months — new recurring item?"})
    return report
