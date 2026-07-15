"""Telegram-friendly rendering of engine output. Pure formatting — no math.

Two audiences (PRD: accountant AND owner). `detail="owner"` keeps messages
calm, plain-English and truncated-but-auditable; `detail="accountant"` uncaps
tables and exposes methods, params, sources and machine hints.
"""
from __future__ import annotations

from .models import ForecastVersion


def money(x: float) -> str:
    return ("-£{:,.0f}".format(abs(x))) if x < 0 else ("£{:,.0f}".format(x))


def _vat_lines(v: dict) -> list[str]:
    if v.get("due_estimate", 0) <= 0:
        return []
    paid = v.get("paid_this_month")
    if paid:
        out = [f"*VAT:* {money(paid['amount'])} paid to HMRC on {paid['date']} ✅"]
        out.append(f"  Pot rebuilding: {money(v['pot'])} saved toward the next return"
                   f" (est. {money(v['due_estimate'])}"
                   + (f", due {v['next_due']}" if v.get("next_due") else "") + ").")
        return out
    flag = "⚠️ UNDERFUNDED" if v["underfunded"] else "✅ funded"
    out = [f"*VAT:* est. liability {money(v['due_estimate'])} vs pot {money(v['pot'])} → {flag}"]
    if v["underfunded"]:
        out.append(f"  Fund the pot by {money(v['shortfall'])} before the return"
                   + (f" (due {v['next_due']})" if v.get("next_due") else "") + ".")
    elif v.get("next_due"):
        out.append(f"  Next return due {v['next_due']}.")
    if v.get("accrued_to_date"):
        out.append(f"  _{money(v['accrued_to_date'])} accrued so far this period "
                   f"(from the ledger's tax codes)._")
    return out


def _corp_lines(ct: dict) -> list[str]:
    if not ct or ct.get("due_estimate", 0) <= 0:
        return []
    return [f"*Corporation tax:* ~{money(ct['due_estimate'])} due {ct['next_due']} "
            f"_(est. from {ct['basis']})_"]


def fmt_forecast(fv: ForecastVersion) -> str:
    L = [f"*Cashflow forecast — {fv.org} — {fv.month}* (made {fv.anchor})",
         f"Opening cash: *{money(fv.opening_balance)}*  (operating {money(fv.operating_balance)} + VAT pot {money(fv.vat_pot)})",
         f"Projected net: *{money(fv.projected_net)}*  →  close: *{money(fv.forecast_close)}*",
         f"Projected low point: *{money(fv.low_point['balance'])}* on {fv.low_point['date']}",
         ""]
    L.append("_Week by week:_")
    for w in fv.weekly:
        L.append(f"  W{w['week']} ({w['from'][5:]}→{w['to'][5:]}): net {money(w['net'])} → {money(w['close'])}")
    ins = [o for o in fv.occurrences if o.amount > 0]
    outs = [o for o in fv.occurrences if o.amount < 0]
    L.append("")

    def _listed(rows, n):
        shown = rows[:n]
        rest = rows[n:]
        out = [f"  • {o.expected_date[5:]}  {o.label}: {money(o.amount)}" for o in shown]
        if rest:   # totals must stay auditable from the screen
            out.append(f"  … and {len(rest)} more ({money(sum(o.amount for o in rest))})")
        return out

    L.append(f"_Expected inflows ({len(ins)}):_")
    L += _listed(sorted(ins, key=lambda x: -x.amount), 8)
    L.append(f"_Expected outflows ({len(outs)}):_")
    L += _listed(sorted(outs, key=lambda x: x.amount), 10)
    L.append("")
    L.extend(_vat_lines(fv.vat))
    if fv.excluded:
        L.append("_Not forecast (lumpy/discretionary — watch):_")
        for e in fv.excluded:
            L.append(f"  • {e['label']} (recent ~{money(e['recent_monthly'])}/mo)")
    return "\n".join(L)


def fmt_status(fv: ForecastVersion, recon: dict, live: dict | None = None,
               materiality: float = 2000.0) -> str:
    """Status = live truth first, frozen plan as the comparison.

    `live` is a fresh compute() as of the clock — its VAT block, pot and low
    point reflect what has actually happened (the frozen fv's don't)."""
    pace = recon["on_pace_delta"]
    band = max(materiality / 2, 500.0)
    pace_s = "on pace ✅" if abs(pace) < band else \
        (f"{money(pace)} ahead 📈" if pace > 0 else f"{money(pace)} behind 📉")
    pot_now = live["vat_pot"] if live else recon["vat_pot_now"]
    _stamp = f" · {live['txn_count']} txns" if live and live.get("txn_count") else ""
    L = [f"*Cashflow status — {fv.org}* (as of {recon['as_of']}{_stamp})",
         f"Cash now: *{money(recon['cash_now'])}*  (VAT pot {money(pot_now)})",
         f"Month so far: expected {money(recon['expected_to_date'])}, "
         f"actual *{money(recon['actual_net_to_date'])}* → {pace_s}",
         f"Projected close: *{money(recon['projected_close'])}* "
         f"(the plan, frozen {fv.anchor}: {money(recon['forecast_close'])})"]
    late_in, late_out = recon.get("late_inflows", 0), recon.get("late_outflows", 0)
    if abs(late_in) + abs(late_out) >= materiality / 8:
        parts = []
        if late_in:
            parts.append(f"+{money(late_in)} in")
        if late_out:
            parts.append(f"{money(late_out)} out")
        L.append(f"  ⏳ still expected (late, excluded from projected close): "
                 + " / ".join(parts))
    if live:
        L.append(f"Projected low from here: *{money(live['low_point']['balance'])}* "
                 f"on {live['low_point']['date']}")
        L.extend(_vat_lines(live["vat"]))
    else:
        L.append(f"Plan low point: {money(fv.low_point['balance'])} on {fv.low_point['date']}")
        L.extend(_vat_lines(fv.vat))
    hot = [l for l in recon["lines"] if l["status"] in ("missing", "amount")][:3]
    for l in hot:
        L.append(f"  ⚠ {l['occ']['label']}: {l['status']} ({money(l['delta'])})")
    if recon["surprises"]:
        s = recon["surprises"][0]
        L.append(f"  ❗ unforecast: {s['label']} {money(s['amount'])} ({s['counterparty']})")
    return "\n".join(L)


_ICON = {"on_track": "✅", "timing": "🕐", "amount": "±", "missing": "❌",
         "due_now": "⏳", "pending": "·"}


def fmt_reconcile(recon: dict, detail: str = "owner") -> str:
    L = [f"*Reconciliation — {recon['org']} — {recon['month']}* (to {recon['as_of']})",
         f"Expected to date {money(recon['expected_to_date'])} vs actual "
         f"*{money(recon['actual_net_to_date'])}* → delta {money(recon['on_pace_delta'])}",
         f"Cash now {money(recon['cash_now'])}; projected close "
         f"{money(recon['projected_close'])} (the frozen plan said "
         f"{money(recon['forecast_close'])})"]
    if recon.get("late_inflows"):
        L.append(f"⏳ Still expected (late, not in projected close): "
                 f"+{money(recon['late_inflows'])} in"
                 + (f", {money(recon['late_outflows'])} out"
                    if recon.get("late_outflows") else ""))
    L += ["", "_Forecast lines:_"]
    order = {"missing": 0, "amount": 1, "timing": 2, "due_now": 3, "on_track": 4, "pending": 5}
    rows = sorted(recon["lines"], key=lambda x: (order[x["status"]],
                                                 -abs(x["occ"]["amount"])))
    cap = len(rows) if detail == "accountant" else 14
    for l in rows[:cap]:
        o = l["occ"]
        extra = ""
        if l["status"] == "timing":
            extra = f" ({abs(l['delta_days'])}d {'late' if l['delta_days'] > 0 else 'early'})"
        elif l["status"] == "amount":
            extra = f" (off by {money(l['delta'])})"
        elif l["status"] == "on_track" and l["actual"]:
            extra = f" (landed {money(l['actual'])})"
        src = f" [{o['source']}]" if detail == "accountant" else ""
        L.append(f"  {_ICON[l['status']]} {o['expected_date'][5:]} {o['label']} "
                 f"{money(o['amount'])} — {l['status']}{extra}{src}")
    if len(rows) > cap:
        rest = rows[cap:]
        L.append(f"  … and {len(rest)} more lines "
                 f"({money(sum(x['occ']['amount'] for x in rest))} expected)")
    if recon["surprises"]:
        L.append("")
        L.append("_Not in forecast (surprises):_")
        s_cap = len(recon["surprises"]) if detail == "accountant" else 6
        for s in recon["surprises"][:s_cap]:
            L.append(f"  ❗ {s['date'][5:]} {s['label']} {money(s['amount'])} — {s['counterparty']}")
        if len(recon["surprises"]) > s_cap:
            rest = recon["surprises"][s_cap:]
            L.append(f"  … and {len(rest)} more ({money(sum(s['amount'] for s in rest))})")
    if recon["lessons"]:
        L.append("")
        L.append("_Lessons:_")
        for t in recon["lessons"]:
            if isinstance(t, dict):
                L.append(f"  💡 {t['text']}")
                if detail == "accountant" and t.get("fix"):
                    L.append(f"      ↳ fix: `{t['fix']}`")
            else:
                L.append(f"  💡 {t}")
    return "\n".join(L)


def fmt_outlook(r: dict) -> str:
    """Multi-period, config-driven outlook. Verdict first — the PRD's
    'I'm okay / I need to look closer' signal."""
    icon, why = r["verdict"]
    mat = r.get("materiality", 2000.0)
    L = [f"{icon} *{why}*",
         "",
         f"*Cash outlook — {r['org']}* ({r['months'][0]} → {r['months'][-1]}, "
         f"by {r['grain']})",
         f"🕐 _as of {r.get('as_of', r.get('anchor'))} · {r.get('txn_count','?')} txns_",
         f"Opening cash: *{money(r['opening_balance'])}*  (VAT pot {money(r['vat_pot'])})",
         f"Projected net: {money(r['projected_net'])}  →  close: *{money(r['close'])}*"]
    sigma = r.get("sigma", 0.0)
    lp = r["low_point"]
    if mat / 2 <= sigma < 0.35 * max(abs(r["opening_balance"]), 1):
        L.append(f"Low point: *~{money(lp['balance'] - sigma)} to "
                 f"{money(lp['balance'] + sigma)}* around {lp['date']} "
                 f"_(±{money(sigma)} from normal month-to-month variation)_")
    else:
        L.append(f"Low point: *{money(lp['balance'])}* on {lp['date']}")
        if sigma >= 0.35 * max(abs(r["opening_balance"]), 1):
            L.append(f"  _History swings ~±{money(sigma)}/month — treat "
                     f"amounts and dates as approximate._")
    if r.get("breach") and r["breach"]["date"][:7] > r["months"][-1]:
        L.append(f"⚠️ _First floor breach {r['breach']['date']} — beyond the "
                 f"{len(r['months'])} months shown. Run `outlook --months "
                 f"{len(r.get('risk_months', r['months']))}` to see it._")
    L.append("")
    show = r["buckets"][:14]
    L.append(f"_{r['grain'].title()}s:_")
    for b in show:
        bar = "▁" if b["net"] == 0 else ("▲" if b["net"] > 0 else "▼")
        L.append(f"  {b['from'][5:]}→{b['to'][5:]}  {bar} net {money(b['net'])} → {money(b['close'])}")
        for drv in b.get("drivers", []):
            L.append(f"      └ {drv['label']}: {money(drv['amount'])} ({drv['date'][5:]})")
    if len(r["buckets"]) > 14:
        L.append(f"  … +{len(r['buckets']) - 14} more")
    L.append("")
    L.extend(_vat_lines(r["vat"]))
    L.extend(_corp_lines(r.get("corp_tax", {})))
    big = [o for o in r["occurrences"] if abs(o.amount) >= mat]
    small = [o for o in r["occurrences"] if abs(o.amount) < mat]
    if big:
        L.append("_Largest expected movements:_")
        for o in sorted(big, key=lambda x: -abs(x.amount))[:8]:
            L.append(f"  • {o.expected_date[5:]}  {o.label}: {money(o.amount)}")
        if small:
            L.append(f"  • Other ({len(small)} small items): net "
                     f"{money(sum(o.amount for o in small))}")
    if r.get("excluded"):
        L.append("_Not forecast (lumpy — watch):_ " +
                 ", ".join(f"{e['label']} (~{money(e['recent_monthly'])}/mo)"
                           for e in r["excluded"][:3]))
    L.append("")
    L.append(f"_Built from {r.get('txn_count', '?')} bank transactions · "
             f"{r.get('item_count', '?')} editable line items · every number "
             f"deterministic (no AI in the maths)._")
    return "\n".join(L)


def fmt_config_items(items: list[dict], detail: str = "owner") -> str:
    if not items:
        return "No line items configured yet — run `sync` to seed from actuals."
    conf_word = {"recurring_fixed": "steady", "recurring_variable": "trend",
                 "one_off": "planned", "linked": "linked"}
    L = [f"*Forecast line items* ({len(items)})"]
    for it in items:
        p = it["params"]
        if it["method"] == "recurring_fixed":
            desc = f"{money(p['amount'])}/mo on day {p.get('day_of_month', 15)}"
        elif it["method"] == "recurring_variable":
            obs = p.get("observations", [])
            desc = f"trend of last {min(len(obs), p.get('lookback_months', 3))} months"
        elif it["method"] == "one_off":
            desc = f"{money(p['amount'])} on {p['date']}"
        else:
            desc = f"{p['pct']}% of {p.get('target', 'revenue')}"
        window = ""
        if it["start_date"] or it["end_date"]:
            window = f" [{it['start_date'] or '…'} → {it['end_date'] or '…'}]"
        lock = " 🔒" if it["locked"] else ""
        if detail == "accountant":
            basis = f" — {p['basis']}" if p.get("basis") else ""
            L.append(f"  #{it['id']} {it['name']} — {it['method']}: {desc}{window} "
                     f"({it['source']}{lock}{basis})")
        else:
            hint = conf_word.get(it["method"], "")
            basis = ""
            if p.get("basis") and "events" in str(p.get("basis", "")):
                n = str(p["basis"]).split(" ")[0]
                basis = f", seen {n}×"
            L.append(f"  #{it['id']} {it['name']}: {desc}{window} "
                     f"({hint}{basis}{lock})")
    return "\n".join(L)


def fmt_sync(rep: dict) -> str:
    L = ["*Sync — line items refreshed from actuals*",
         f"  added {len(rep['added'])} · refreshed {len(rep['refreshed'])} · "
         f"owner-locked skipped {len(rep['skipped_locked'])} · vanished {len(rep['gone'])}"]
    for name in rep["gone"][:5]:
        L.append(f"  ⚠ '{name}' no longer appears in the data — end or remove it?")
    for q in rep.get("quiet", [])[:5]:
        L.append(f"  ⚠ '{q['name']}' has gone quiet (last seen {q['last_seen']}) — "
                 f"{q['proposal']}")
    for s in rep["suggestions"][:5]:
        L.append(f"  💡 emerging: {s['counterparty']} ({s['category']}) "
                 f"~{money(s['monthly_amount'])}/mo around day "
                 f"{s.get('day_of_month', 15)} — {s['reason']} "
                 f"(not forecast until you confirm)")
    return "\n".join(L)


def fmt_changes(changes: list[dict], since: str) -> str:
    if not changes:
        return f"No forecast changes since {since}."
    L = [f"*What changed since {since}:*"]
    for ch in changes[-15:]:
        L.append(f"  • {ch['created_at'][5:16]}  {ch['action']}: {ch['name'] or '?'}"
                 + (f" — {ch['note']}" if ch["note"] else ""))
    return "\n".join(L)


def fmt_since(prev: dict, cur: dict, config_changes: list[dict],
              materiality: float = 2000.0) -> str | None:
    """The 'since you last looked' opener: only what actually moved.
    Returns None when nothing material changed (calm by default)."""
    bits = []
    if prev.get("verdict_icon") and prev["verdict_icon"] != cur["verdict_icon"]:
        bits.append(f"verdict {prev['verdict_icon']} → {cur['verdict_icon']}")
    d_cash = cur["cash"] - prev.get("cash", cur["cash"])
    if abs(d_cash) >= materiality / 2:
        bits.append(f"cash {money(prev['cash'])} → {money(cur['cash'])} "
                    f"({'+' if d_cash > 0 else ''}{money(d_cash)})")
    if prev.get("low_date") and cur["low_date"] != prev["low_date"]:
        bits.append(f"low point moved {prev['low_date']} → {cur['low_date']}")
    n_edits = len(config_changes)
    if n_edits:
        bits.append(f"{n_edits} forecast edit{'s' if n_edits > 1 else ''}")
    if not bits:
        return None
    when = prev.get("clock", "?")
    return f"_Since you last looked ({when}):_ " + " · ".join(bits)


def fmt_impact(base: dict, after: dict, materiality: float = 2000.0,
               undo_hint: bool = True) -> str:
    """Before → after effect of one edit — the PRD's 'impact of a single
    assumption change obvious without scenarios'."""
    m = money
    L = []
    d_close = after["close"] - base["close"]
    if abs(d_close) >= 1:
        L.append(f"Close ({len(after['months'])}mo): {m(base['close'])} → "
                 f"*{m(after['close'])}* ({'+' if d_close > 0 else ''}{m(d_close)})")
    if (base["low_point"]["balance"], base["low_point"]["date"]) != \
            (after["low_point"]["balance"], after["low_point"]["date"]):
        L.append(f"Low point: {m(base['low_point']['balance'])} ({base['low_point']['date']}) → "
                 f"*{m(after['low_point']['balance'])}* ({after['low_point']['date']})")
    bi, ai = base["verdict"][0], after["verdict"][0]
    if bi != ai:
        L.append(f"Verdict: {bi} → {ai} — {after['verdict'][1]}")
    if after.get("breach") and not base.get("breach"):
        L.append(f"🔴 *This change takes you below your floor on "
                 f"{after['breach']['date']}.*")
    elif base.get("breach") and not after.get("breach"):
        L.append("✅ The projected floor breach is gone.")
    if not L:
        L.append("No material change to the outlook.")
    if undo_hint:
        L.append("_(undoable: `item undo`)_")
    return "\n".join(L)


def fmt_period_compare(a: str, b: str, series_a: dict, series_b: dict,
                       threshold: float = 500.0) -> str:
    L = [f"*Period comparison — {a} vs {b}* (actuals)"]
    cats = sorted(set(series_a) | set(series_b),
                  key=lambda c: -abs(series_a.get(c, 0) - series_b.get(c, 0)))
    total_a = total_b = 0.0
    small_a = small_b = 0.0
    n_small = 0
    for c in cats:
        va, vb = series_a.get(c, 0.0), series_b.get(c, 0.0)
        total_a += va
        total_b += vb
        if abs(va - vb) < threshold:
            small_a += va
            small_b += vb
            n_small += 1
            continue
        from . import taxonomy as tax
        L.append(f"  {tax.label(c)}: {money(va)} vs {money(vb)}  (Δ {money(va - vb)})")
    if n_small:
        L.append(f"  Other ({n_small} categories, little changed): "
                 f"{money(small_a)} vs {money(small_b)}")
    L.append(f"  *Net: {money(total_a)} vs {money(total_b)}  (Δ {money(total_a - total_b)})*")
    return "\n".join(L)


def fmt_map_impact(impact: dict) -> str:
    """Preview of what a mapping rule would change — before it's applied."""
    if not impact["moved"]:
        return ("This rule matches no existing transactions — it will only "
                "affect future ones.")
    L = [f"This rule would move *{impact['n']} transaction(s)*, "
         f"{money(impact['total'])} in total:"]
    for (old, new), amt in sorted(impact["by_move"].items(),
                                  key=lambda kv: -abs(kv[1])):
        L.append(f"  • {old} → {new}: {money(amt)}")
    if impact.get("months"):
        L.append(f"  across {impact['months'][0]} → {impact['months'][-1]}")
    if impact.get("item_moves"):
        L.append("Line items that will follow the new category:")
        for mv in impact["item_moves"]:
            L.append(f"  • {mv}")
    return "\n".join(L)
