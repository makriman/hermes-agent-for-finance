"""Cashew engine CLI — the deterministic surface the Hermes agent drives.

Forecasting: outlook · forecast · status · reconcile · vat|tax · weekly · daily
Config:      sync · items · item add|set|end|rm|undo · assume · changes · settings
Analysis:    lessons · compare · whatif · scenario save|list|run|compare · export · import
Mapping:     map list|unmapped|preview|add|score
Sim:         now · orgs · advance · set · reset

v5 invariants: ONE compute path behind every view (no two commands can
disagree on a number) · verdict-first on every reporting command, scanned over
the full risk horizon · every edit prints its before→after impact + undo hint
· errors come out as one friendly line, never a traceback.
"""
from __future__ import annotations

import argparse
import json
import statistics

from . import compute as cp
from . import config_sync
from . import reconcile as rc
from . import report
from .actuals import build_enriched
from .client import DEFAULT_BASE, Clients
from .store import Store

# set by _ctx / commands so main() can maintain the "since you last looked"
# cursor without each command carrying the plumbing
_LAST = {"org": None, "clock": None, "live": None}

READ_CMDS = {"outlook", "forecast", "status", "reconcile", "vat", "tax",
             "weekly", "daily", "debtors", "creditors", "commitments",
             "items", "lineitems", "lessons", "compare", "changes", "map",
             "whatif", "scenario", "export", "assume", "settings"}


def _ctx(c: Clients, store: Store, args):
    cfg = c.config()
    if getattr(args, "org", None) and args.org != cfg["org"]:
        c.set_org(args.org)
        cfg = c.config()
    _LAST["org"], _LAST["clock"] = cfg["org"], cfg["now"]
    return cfg["org"], cfg


def _current_month(cfg) -> str:
    """The month the owner is living in: the scenario month until the clock
    moves past it, then whatever month the clock is in (rollover-aware)."""
    return cfg["scenario_month"] if cfg["now"] <= cfg["anchor"] else cfg["now"][:7]


def _detail(store, org, args) -> str:
    """Persona: explicit --detail flag beats the persisted org setting."""
    return args.detail or store.get_setting(org, "persona", "owner")


def _floor(store, org, args) -> float:
    """Owner cash floor: persisted org setting, overridable per run."""
    explicit = getattr(args, "floor", None)
    if explicit is not None and explicit > 0:
        store.set_setting(org, "cash_floor", explicit)
        return explicit
    return float(store.get_setting(org, "cash_floor", 0.0))


def _materiality(c, store, org, cfg) -> float:
    """The single materiality knob, scaled to THIS business: 2% of its median
    monthly gross cash volume (min £500). Set once from data, owner-tunable
    via `settings --materiality`."""
    v = store.get_setting(org, "materiality")
    if v is not None:
        return float(v)
    from . import taxonomy as tax
    enriched, _, _, _ = build_enriched(c, cfg["now"], store.rules(org))
    vol: dict[str, float] = {}
    for t in enriched:
        if tax.is_transfer(t.category):
            continue                       # internal moves aren't volume
        ym = t.date.strftime("%Y-%m")
        vol[ym] = vol.get(ym, 0.0) + abs(t.amount)
    full = [vol[m] for m in sorted(vol) if m < cfg["now"][:7]][-3:]
    mat = max(500.0, round(statistics.median(full) * 0.02, -1)) if full else 2000.0
    store.set_setting(org, "materiality", mat)
    return mat


def _params(c, store, org, cfg, args, months=None) -> dict:
    mat = _materiality(c, store, org, cfg)
    return {
        "horizon_months": months or getattr(args, "months", None) or 3,
        "grain": getattr(args, "grain", None) or "week",
        "materiality": mat,
        "cash_floor": _floor(store, org, args),
        "vat_pot_account": store.get_setting(org, "vat_pot_account"),
    }


def _live(c, store, org, cfg, args, months=None, scenario=None) -> dict:
    _ensure_synced(c, store, org, cfg)
    r = cp.compute(c, store, org, params=_params(c, store, org, cfg, args, months),
                   scenario=scenario)
    _LAST["live"] = r
    return r


def _verdict_line(r: dict) -> str:
    icon, why = r["verdict"]
    return f"{icon} *{why}*"


def _since_block(store, org, live, mat) -> str | None:
    prev = store.get_setting(org, "view_snapshot")
    if not prev:
        return None
    prev = json.loads(prev)
    last_viewed = store.get_setting(org, "last_viewed_at", "1970-01-01")
    cur = {"verdict_icon": live["verdict"][0], "cash": live["opening_balance"],
           "low_date": live["low_point"]["date"]}
    edits = [ch for ch in store.changes_since(org, last_viewed)
             if ch["action"] in ("create", "update", "end", "deactivate",
                                 "recategorize")
             and not (ch["note"] or "").startswith("sync as of")]
    return report.fmt_since(prev, cur, edits, mat)


def _ensure_synced(c, store, org, cfg):
    if not store.items(org):
        mat = _materiality(c, store, org, cfg)
        config_sync.sync(c, store, org, _current_month(cfg), cfg["now"],
                         materiality=mat)


def _freeze(c, store, org, cfg, month=None):
    _ensure_synced(c, store, org, cfg)
    fv = cp.freeze_month(c, store, org, month=month)
    store.save_forecast(fv)
    return fv


def _latest_or_freeze(c, store, org, cfg, month=None):
    month = month or _current_month(cfg)
    fv = store.latest_forecast(org, month)
    return fv if fv else _freeze(c, store, org, cfg, month=month)


def _reconcile(c, store, org, fv, mat, as_of=None):
    recon = rc.run(c, fv, as_of=as_of, owner_rules=store.rules(org),
                   materiality=mat)
    store.save_reconciliation(org, fv.month, recon["as_of"], recon)
    store.update_lateness(org, recon["lateness_observations"], month=fv.month)
    store.add_lessons(org, fv.month, recon["lessons"])
    return recon


def _escalate(live: dict, recon: dict, fv, mat: float) -> tuple[str, str]:
    """One escalation rule for status AND daily: materially behind plan is
    never a quiet 🟡."""
    icon, why = live["verdict"]
    pace = recon["on_pace_delta"]
    if icon != "🔴" and pace < -max(0.15 * abs(fv.opening_balance), 5 * mat):
        icon = "🔴"
        why = (f"{report.money(pace)} behind plan this month — check the "
               f"surprises before anything else")
    return icon, why


def _impact_wrap(c, store, org, cfg, args, mutate, undo_hint=True):
    """Run one mutation between two computes and print the before→after
    effect — every edit is safe, reversible, and clearly reflected."""
    mat = _materiality(c, store, org, cfg)
    params = _params(c, store, org, cfg, args, months=6)
    base = cp.compute(c, store, org, params=params)
    msg = mutate()
    print(msg)
    if msg.startswith(("No ", "Nothing", "Give ", "⚠")):
        return
    after = cp.compute(c, store, org, params=params)
    _LAST["live"] = after
    print()
    print(report.fmt_impact(base, after, mat, undo_hint=undo_hint))


# --- forecasting ------------------------------------------------------------------

def cmd_outlook(c, store, args):
    org, cfg = _ctx(c, store, args)
    scenario = None
    if args.scenario:
        scenario = store.scenario(org, args.scenario)
        if scenario is None:
            print(f"Unknown scenario '{args.scenario}'. Saved: "
                  + (", ".join(store.scenarios_list(org)) or "(none)"))
            return
    r = _live(c, store, org, cfg, args, scenario=scenario)
    since = _since_block(store, org, r, r["materiality"])
    if since:
        print(since + "\n")
    print(report.fmt_outlook(r))


def cmd_forecast(c, store, args):
    """Show the frozen monthly baseline. A baseline is created once per month;
    viewing it never rebases it (that would zero out the variance the owner
    trusts). Use --refreeze to deliberately re-baseline."""
    org, cfg = _ctx(c, store, args)
    live = _live(c, store, org, cfg, args, months=1)
    print(_verdict_line(live) + "\n")
    existing = store.latest_forecast(org, args.month or _current_month(cfg))
    if existing and not args.refreeze:
        print(report.fmt_forecast(existing))
        print("\n_(the plan: frozen baseline from "
              f"{existing.created_at[:16]} — use `forecast --refreeze` to re-baseline, "
              "or `outlook` for the live view)_")
        return
    fv = _freeze(c, store, org, cfg, month=args.month or _current_month(cfg))
    print(report.fmt_forecast(fv))


def cmd_status(c, store, args):
    org, cfg = _ctx(c, store, args)
    mat = _materiality(c, store, org, cfg)
    month = args.month or _current_month(cfg)
    fv = store.latest_forecast(org, month)
    if fv is None:                      # month rollover: freeze the new baseline
        fv = _freeze(c, store, org, cfg, month=month)
        if month != cfg["scenario_month"]:
            print(f"_(new month — froze the {fv.month} baseline)_\n")
    recon = _reconcile(c, store, org, fv, mat)
    live = _live(c, store, org, cfg, args, months=1)
    icon, why = _escalate(live, recon, fv, mat)
    print(f"{icon} *{why}*\n")
    since = _since_block(store, org, live, mat)
    if since:
        print(since + "\n")
    print(report.fmt_status(fv, recon, live=live, materiality=mat))


def cmd_reconcile(c, store, args):
    org, cfg = _ctx(c, store, args)
    mat = _materiality(c, store, org, cfg)
    live = _live(c, store, org, cfg, args)
    fv = _latest_or_freeze(c, store, org, cfg, args.month)
    recon = _reconcile(c, store, org, fv, mat, as_of=args.as_of)
    icon, why = _escalate(live, recon, fv, mat)
    print(f"{icon} *{why}*\n")
    print(report.fmt_reconcile(recon, detail=_detail(store, org, args)))


def cmd_vat(c, store, args):
    """The tax position — VAT (quarterly, accrual-first) + corporation tax
    (annual). Always live: a payment that landed this morning shows here."""
    org, cfg = _ctx(c, store, args)
    live = _live(c, store, org, cfg, args, months=6)
    print(_verdict_line(live) + "\n")
    v = live["vat"]
    print(f"*Tax position — {org}* (as of {cfg['now']})")
    if v.get("due_estimate", 0) <= 0:
        print("No VAT liability detected (no VAT history in the data).")
    else:
        vat_block = report._vat_lines(v)
        print("\n".join(vat_block))
        print(f"_Basis: {v['basis']}_")
    ct = live.get("corp_tax", {})
    if ct.get("due_estimate", 0) > 0:
        print()
        print("\n".join(report._corp_lines(ct)))
    elif ct:
        print(f"\n_Corporation tax: {ct.get('basis', 'no history')}._")


def cmd_weekly(c, store, args):
    org, cfg = _ctx(c, store, args)
    mat = _materiality(c, store, org, cfg)
    live = _live(c, store, org, cfg, args, months=1)
    fv = _latest_or_freeze(c, store, org, cfg, args.month or _current_month(cfg))
    recon = _reconcile(c, store, org, fv, mat)
    icon, why = _escalate(live, recon, fv, mat)
    print(f"{icon} *{why}*\n")
    print(report.fmt_status(fv, recon, live=live, materiality=mat))
    print("\n_The weeks ahead (live):_")
    for b in live["buckets"]:
        bar = "▁" if b["net"] == 0 else ("▲" if b["net"] > 0 else "▼")
        print(f"  {b['from'][5:]}→{b['to'][5:]}  {bar} net {report.money(b['net'])} "
              f"→ {report.money(b['close'])}")
        for drv in b.get("drivers", []):
            print(f"      └ {drv['label']}: {report.money(drv['amount'])} "
                  f"({drv['date'][5:]})")
    lessons = store.lessons(org, limit=5)
    if lessons:
        print("\n_Recent lessons:_")
        for l in lessons:
            print(f"  💡 [{l['month']}] {l['text']}")


def cmd_daily(c, store, args):
    """Read-only by default (v5): checking your cash never moves the clock.
    The simulated morning tick passes --advance explicitly.

    The digest alarms only on what's NEW (last ~2 days) — a shock from the
    1st must not re-alarm every morning for a month. The verdict and pace
    always reflect the true month state; `status` has the full breakdown."""
    from datetime import date as _date, timedelta as _td
    org, cfg = _ctx(c, store, args)
    if args.advance:
        c.advance(1)
        cfg = c.config()
        _LAST["clock"] = cfg["now"]
    mat = _materiality(c, store, org, cfg)
    fv = _latest_or_freeze(c, store, org, cfg, args.month)
    recon = _reconcile(c, store, org, fv, mat)
    live = _live(c, store, org, cfg, args, months=1)
    icon, why = _escalate(live, recon, fv, mat)
    print(f"*Daily check — {org} — {cfg['now']}*")
    print(f"{icon} *{why}*")
    since = _since_block(store, org, live, mat)
    if since:
        print(since)
    pace = recon["on_pace_delta"]
    band = max(mat / 2, 500.0)
    pace_s = "on pace ✅" if abs(pace) < band else \
        (f"{report.money(pace)} ahead of plan 📈" if pace > 0
         else f"{report.money(pace)} behind plan 📉")
    print(f"Cash *{report.money(recon['cash_now'])}* (VAT pot "
          f"{report.money(live['vat_pot'])}) · month so far: {pace_s} · "
          f"low from here {report.money(live['low_point']['balance'])} "
          f"({live['low_point']['date'][5:]})")
    cut = (_date.fromisoformat(cfg["now"]) - _td(days=1)).isoformat()
    new_surprises = [s for s in recon["surprises"] if s["date"] >= cut]
    new_hot = [l for l in recon["lines"]
               if l["status"] in ("missing", "amount", "timing")
               and l["occ"]["expected_date"] >= cut]
    if not new_surprises and not new_hot:
        print("Nothing new needs your attention since yesterday."
              + ("" if icon == "🟢" else " (`status` has the month's full picture.)"))
        return
    print("\n_New since yesterday:_")
    for s in new_surprises[:5]:
        print(f"  ❗ {s['date'][5:]} {s['label']} {report.money(s['amount'])} "
              f"— {s['counterparty']} (not in the plan)")
    for l in new_hot[:5]:
        o = l["occ"]
        print(f"  ⚠ {o['expected_date'][5:]} {o['label']} "
              f"{report.money(o['amount'])} — {l['status']}")


# --- config: sync / items / changes / settings --------------------------------------

def cmd_sync(c, store, args):
    org, cfg = _ctx(c, store, args)
    mat = _materiality(c, store, org, cfg)
    rep = config_sync.sync(c, store, org, _current_month(cfg), cfg["now"],
                           materiality=mat)
    print(report.fmt_sync(rep))


def cmd_items(c, store, args):
    org, cfg = _ctx(c, store, args)
    _ensure_synced(c, store, org, cfg)
    print(report.fmt_config_items(store.items(org), detail=_detail(store, org, args)))


def cmd_item(c, store, args):
    org, cfg = _ctx(c, store, args)
    a = args.action

    def do_add():
        params = json.loads(args.params) if args.params else {}
        if args.amount is not None:
            params["amount"] = args.amount
        if args.date:
            params["date"] = args.date
        if args.day:
            params["day_of_month"] = args.day
        if args.pct is not None:
            params["pct"] = args.pct
        if args.target:
            slug = _resolve_cat(args.target)
            if slug is None:
                return (f"⚠ linked target '{args.target}' is not a category — the item "
                        f"would contribute £0. Use a category like 'revenue'.")
            params["target"] = slug
        iid = store.upsert_item(org, args.name, args.category, args.method, params,
                                counterparty=args.counterparty or "",
                                start_date=args.start, end_date=args.end,
                                source="owner", locked=1, note=args.note or "")
        return (f"Line item #{iid} '{args.name}' saved ({args.method}). "
                f"Locked against auto-sync.")

    def do_set():
        it = store.get_item(org, args.id)
        if not it:
            return f"No item '{args.id}'."
        params = {**it["params"], **(json.loads(args.params) if args.params else {})}
        if args.amount is not None:
            params["amount"] = args.amount
        if args.day:
            params["day_of_month"] = args.day
        store.upsert_item(org, it["name"], args.category or it["category"],
                          args.method or it["method"], params,
                          counterparty=it["counterparty"],
                          start_date=args.start or it["start_date"],
                          end_date=args.end or it["end_date"],
                          source="owner", locked=1, note=args.note or "edited")
        return f"Item '{it['name']}' updated (owner-locked)."

    def do_end():
        end = args.end or args.date
        if not end:
            return "Give the end date: item end --id N --end YYYY-MM-DD"
        ok = store.end_item(org, args.id, end, note=args.note or "")
        return "Item ended." if ok else "Item not found."

    def do_rm():
        ok = store.deactivate_item(org, args.id, note=args.note or "")
        return "Item deactivated (recoverable via undo)." if ok else "Item not found."

    if a == "add":
        _impact_wrap(c, store, org, cfg, args, do_add)
    elif a == "set":
        _impact_wrap(c, store, org, cfg, args, do_set)
    elif a == "end":
        _impact_wrap(c, store, org, cfg, args, do_end)
    elif a == "rm":
        _impact_wrap(c, store, org, cfg, args, do_rm)
    elif a == "undo":
        _impact_wrap(c, store, org, cfg, args, lambda: store.undo_last(org),
                     undo_hint=False)


def cmd_changes(c, store, args):
    org, cfg = _ctx(c, store, args)
    since = args.since or store.get_setting(org, "last_viewed_at", "1970-01-01")
    print(report.fmt_changes(store.changes_since(org, since), since))


def cmd_assume(c, store, args):
    """Sugar: an assumption is a one_off owner line item."""
    org, cfg = _ctx(c, store, args)
    if args.action == "add":
        name = f"Assumption — {args.note or args.category} ({args.date})"

        def do_add():
            iid = store.upsert_item(org, name, args.category, "one_off",
                                    {"amount": args.amount, "date": args.date},
                                    source="owner", locked=1, note=args.note or "")
            return f"Assumption saved as line item #{iid}."
        _impact_wrap(c, store, org, cfg, args, do_add)
    elif args.action == "list":
        rows = [it for it in store.items(org)
                if it["method"] == "one_off" and it["source"] == "owner"]
        if not rows:
            print("No active assumptions.")
        for it in rows:
            print(f"  #{it['id']} {it['params']['date']} {it['category']} "
                  f"{report.money(it['params']['amount'])} — {it['name']}")
    elif args.action == "rm":
        _impact_wrap(c, store, org, cfg, args,
                     lambda: ("Assumption removed."
                              if store.deactivate_item(org, args.id,
                                                       note="assumption removed")
                              else f"No assumption #{args.id}."))


def cmd_settings(c, store, args):
    org, cfg = _ctx(c, store, args)
    changed = False
    if args.floor is not None:
        store.set_setting(org, "cash_floor", args.floor)
        changed = True
    if args.materiality is not None:
        store.set_setting(org, "materiality", args.materiality)
        changed = True
    if args.pot is not None:
        store.set_setting(org, "vat_pot_account", args.pot)
        changed = True
    if args.persona is not None:
        store.set_setting(org, "persona", args.persona)
        changed = True
    mat = _materiality(c, store, org, cfg)
    print(f"*Settings — {org}*" + (" (updated)" if changed else ""))
    print(f"  cash floor: {report.money(_floor(store, org, args))} "
          f"(verdict turns 🔴 below this)")
    print(f"  materiality: {report.money(mat)} "
          f"(smaller movements are grouped, not alarmed on)")
    print(f"  VAT pot account: {store.get_setting(org, 'vat_pot_account') or '(auto: name contains \"pot\")'}")
    print(f"  persona: {store.get_setting(org, 'persona', 'owner')} "
          f"(owner = plain English; accountant = full detail)")


# --- analysis ------------------------------------------------------------------------

def _aging(c, store, org, cfg, itype: str):
    """Open invoices with aging + expected arrival (learned lateness)."""
    from datetime import date as _date
    from engine.mapping import norm as _norm
    now = _date.fromisoformat(cfg["now"])
    lateness = store.lateness(org)
    rows = []
    for inv in c.invoices(itype):
        if inv.get("Status") != "AUTHORISED":
            continue
        due = _date.fromisoformat(inv["DueDateString"][:10])
        overdue = (now - due).days
        cp_ = inv["Contact"]["Name"]
        eta = due
        late = lateness.get(_norm(cp_)) or lateness.get(cp_)
        if late:
            from datetime import timedelta as _td
            eta = due + _td(days=round(late))
        rows.append({"cp": cp_, "amount": inv["AmountDue"], "due": due,
                     "overdue": overdue, "eta": eta, "num": inv["InvoiceNumber"]})
    rows.sort(key=lambda r: -r["overdue"])
    return rows


def cmd_debtors(c, store, args):
    org, cfg = _ctx(c, store, args)
    rows = _aging(c, store, org, cfg, "ACCREC")
    if not rows:
        print("No open customer invoices — nobody owes you money right now.")
        return
    total = sum(r["amount"] for r in rows)
    overdue_total = sum(r["amount"] for r in rows if r["overdue"] > 0)
    print(f"*Who owes you — {org}*  (open: {report.money(total)}, "
          f"of which OVERDUE: {report.money(overdue_total)})")
    for r in rows[:12]:
        state = (f"⚠️ {r['overdue']}d overdue" if r["overdue"] > 0
                 else f"due in {-r['overdue']}d")
        eta = f", usually pays ~{r['eta']}" if r["eta"] != r["due"] else ""
        print(f"  • {r['cp']}: {report.money(r['amount'])} ({r['num']}) — "
              f"{state}{eta}")
    if overdue_total > 0:
        print(f"\n💡 Chase list: start with the top of this list — "
              f"{report.money(overdue_total)} is collectable now.")


def cmd_creditors(c, store, args):
    org, cfg = _ctx(c, store, args)
    rows = _aging(c, store, org, cfg, "ACCPAY")
    if not rows:
        print("No open supplier bills.")
        return
    total = sum(r["amount"] for r in rows)
    print(f"*What you owe — {org}*  (open bills: {report.money(total)})")
    for r in rows[:12]:
        state = (f"⚠️ {r['overdue']}d overdue" if r["overdue"] > 0
                 else f"due in {-r['overdue']}d")
        print(f"  • {r['cp']}: {report.money(r['amount'])} ({r['num']}) — {state}")


def cmd_commitments(c, store, args):
    """Confirmed recurring commitments straight from the bank: standing orders
    (fixed) + direct debits (variable mandates) + anything pending today."""
    org, cfg = _ctx(c, store, args)
    sos, dds, pend = [], [], []
    for a in c.accounts():
        sos += [(a["display_name"], s) for s in c.standing_orders(a["account_id"])]
        dds += [(a["display_name"], d) for d in c.direct_debits(a["account_id"])]
        pend += c.pending(a["account_id"])
    if sos:
        total = sum(s["next_payment_amount"] for _, s in sos)
        print(f"*Standing orders* ({len(sos)}, ~{report.money(total)}/mo committed):")
        for acct, s in sos:
            print(f"  • {s['payee']}: {report.money(s['next_payment_amount'])} monthly — "
                  f"next {s['next_payment_date'][:10]}")
    else:
        print("*Standing orders:* none detected.")
    if dds:
        print(f"\n*Direct debits* ({len(dds)}):")
        for acct, d in dds:
            print(f"  • {d['name']}: last pulled {report.money(d['previous_payment_amount'])} "
                  f"on {d['previous_payment_timestamp'][:10]}")
    else:
        print("\n*Direct debits:* none detected.")
    if pend:
        print(f"\n*Pending (not yet settled):*")
        for t in pend[:8]:
            print(f"  • {t['timestamp'][:10]} {t['merchant_name']}: {report.money(t['amount'])}")


def cmd_lessons(c, store, args):
    org, cfg = _ctx(c, store, args)
    rows = store.lessons(org, limit=15)
    if not rows:
        print("No lessons recorded yet.")
    for l in rows:
        print(f"  💡 [{l['month']}] {l['text']}")


def cmd_compare(c, store, args):
    org, cfg = _ctx(c, store, args)
    mat = _materiality(c, store, org, cfg)
    from collections import defaultdict
    from . import taxonomy as tax
    enriched, _, _, _ = build_enriched(c, cfg["now"], store.rules(org))
    sa, sb = defaultdict(float), defaultdict(float)
    na = nb = 0
    for t in enriched:
        ym = t.date.strftime("%Y-%m")
        if tax.is_transfer(t.category):
            continue
        if ym == args.a:
            sa[t.category] += t.amount
            na += 1
        elif ym == args.b:
            sb[t.category] += t.amount
            nb += 1
    # a hollow month makes the comparison meaningless — say so
    if na and nb and (na < 0.4 * nb or nb < 0.4 * na):
        thin, fat = (args.a, args.b) if na < nb else (args.b, args.a)
        print(f"⚠ {thin} has far fewer transactions than {fat} "
              f"({min(na, nb)} vs {max(na, nb)}) — it may be incomplete; "
              f"treat this comparison with caution.\n")
    print(report.fmt_period_compare(args.a, args.b, dict(sa), dict(sb),
                                    threshold=max(mat / 4, 250.0)))


def _resolve_cat(key: str) -> str | None:
    """Accept a category slug OR its display label ('Revenue' -> 'revenue')."""
    from . import taxonomy as tax
    k = key.strip()
    if k in tax._C:
        return k
    low = k.lower()
    if low in tax._C:
        return low
    for slug, spec in tax._C.items():
        if spec.label.lower() == low:
            return slug
    return None


def _parse_item_patches(specs: list[str]) -> tuple[dict, list[str]]:
    """--item "Name:key=value,key=value" -> {name: {key: value}} patches."""
    out, desc = {}, []
    for s in specs or []:
        name, _, kvs = s.partition(":")
        if not kvs:
            raise ValueError(f'--item needs "Name:key=value[,key=value]" (got {s!r})')
        patch = {}
        for kv in kvs.split(","):
            k, _, v = kv.partition("=")
            k, v = k.strip(), v.strip()
            if k in ("start_date", "end_date", "method", "date", "target"):
                patch[k] = v
            else:
                patch[k] = float(v)
        out[name.strip()] = patch
        desc.append(f"{name.strip()}: {kvs}")
    return out, desc


def _overlay_from_args(args) -> tuple[dict, str]:
    ov, desc = {}, []
    if args.scale:
        ov["scale"] = {}
        for s in args.scale:
            cat, f = s.split("=")
            slug = _resolve_cat(cat)
            if slug is None:
                print(f"⚠ '{cat}' is not a known category — this lever will do "
                      f"nothing. Try e.g. revenue, suppliers_cogs, payroll.")
                slug = cat
            ov["scale"][slug] = float(f)
            desc.append(f"{slug}×{f}")
    if args.add:
        ov["extra"] = []
        for a in args.add:
            spec_, date_ = a.split("@")
            cat, amount = spec_.split("=")
            slug = _resolve_cat(cat)
            if slug is None:
                print(f"⚠ '{cat}' is not a known category — using it anyway, "
                      f"but it won't roll up to a known label.")
                slug = cat
            ov["extra"].append({"category": slug, "amount": float(amount), "date": date_})
            desc.append(f"{slug} {float(amount):+,.0f} on {date_}")
    if getattr(args, "delay", None):
        ov["delay"] = {}
        for d in args.delay:
            key, days = d.rsplit("=", 1)
            ov["delay"][key] = int(days)
            desc.append(f"{key} +{days}d")
    if getattr(args, "drop", None):
        ov["drop"] = args.drop
        desc.append(f"drop {', '.join(args.drop)}")
    if getattr(args, "item", None):
        patches, pdesc = _parse_item_patches(args.item)
        ov["items"] = patches
        desc.extend(pdesc)
    return ov, ", ".join(desc) or "no changes"


def _print_diff(base: dict, alt: dict, desc: str):
    m = report.money
    print(f"*What-if — {desc}*")
    print(f"Net: {m(base['projected_net'])} → *{m(alt['projected_net'])}* "
          f"({m(alt['projected_net'] - base['projected_net'])})")
    print(f"Close: {m(base['close'])} → *{m(alt['close'])}* "
          f"({m(alt['close'] - base['close'])})")
    print(f"Low point: {m(base['low_point']['balance'])} ({base['low_point']['date']}) → "
          f"*{m(alt['low_point']['balance'])}* ({alt['low_point']['date']})")
    if alt["low_point"]["balance"] < 0 <= base["low_point"]["balance"]:
        print("🔴 *This scenario takes you below zero.*")


def cmd_whatif(c, store, args):
    org, cfg = _ctx(c, store, args)
    _ensure_synced(c, store, org, cfg)
    ov, desc = _overlay_from_args(args)
    p = _params(c, store, org, cfg, args)
    base = cp.compute(c, store, org, params=p)
    alt = cp.compute(c, store, org, params=p, scenario=ov)
    _LAST["live"] = base
    _print_diff(base, alt, desc)


def cmd_scenario(c, store, args):
    org, cfg = _ctx(c, store, args)
    if args.action == "save":
        ov, desc = _overlay_from_args(args)
        store.save_scenario(org, args.name, ov)
        print(f"Scenario '{args.name}' saved: {desc}. "
              f"Run `outlook --scenario {args.name}` or `scenario run {args.name}`.")
    elif args.action == "list":
        names = store.scenarios_list(org)
        print("Scenarios: " + (", ".join(names) if names else "(none)"))
    elif args.action == "run":
        ov = store.scenario(org, args.name)
        if ov is None:
            print(f"No scenario '{args.name}'.")
            return
        _ensure_synced(c, store, org, cfg)
        p = _params(c, store, org, cfg, args)
        base = cp.compute(c, store, org, params=p)
        alt = cp.compute(c, store, org, params=p, scenario=ov)
        _LAST["live"] = base
        _print_diff(base, alt, args.name)
    elif args.action == "compare":
        names = [n for n in (args.name, args.name2) if n]
        if len(names) < 2:
            print("Give two scenario names: scenario compare a b")
            return
        _ensure_synced(c, store, org, cfg)
        p = _params(c, store, org, cfg, args)
        base = cp.compute(c, store, org, params=p)
        _LAST["live"] = base
        m = report.money
        print(f"*Scenario comparison* (vs base close {m(base['close'])}, "
              f"low {m(base['low_point']['balance'])})")
        for n in names:
            ov = store.scenario(org, n)
            if ov is None:
                print(f"  • {n}: (not found)")
                continue
            alt = cp.compute(c, store, org, params=p, scenario=ov)
            flag = " 🔴 below zero" if alt["low_point"]["balance"] < 0 else ""
            print(f"  • {n}: close {m(alt['close'])} "
                  f"({m(alt['close'] - base['close'])}), "
                  f"low {m(alt['low_point']['balance'])} "
                  f"on {alt['low_point']['date']}{flag}")


def cmd_export(c, store, args):
    org, cfg = _ctx(c, store, args)
    r = _live(c, store, org, cfg, args)
    from .export import export_xlsx
    enriched, _, _, _ = build_enriched(c, cfg["now"], store.rules(org))
    out = args.out or f"/home/hermes/cashew/exports/{org}-forecast.xlsx"
    import os
    os.makedirs(os.path.dirname(out), exist_ok=True)
    export_xlsx(out, r, store.items(org), enriched)
    print(f"MEDIA:{out}")
    print(f"Exported forecast to {out} (Forecast / LineItems / Actuals sheets).")
    print("Edit the LineItems sheet and bring it back with "
          "`import <file>` — changes are previewed before anything is applied.")


def cmd_import(c, store, args):
    """Excel round-trip: preview (default) or apply an edited LineItems sheet."""
    org, cfg = _ctx(c, store, args)
    from .xlsx_import import apply_import, plan_import
    plan = plan_import(args.path, store, org)
    print(f"*Import preview — {args.path}*")
    print(f"  {plan['summary']}")
    for ch in plan["changes"][:10]:
        fields = ", ".join(f"{f}: {v['old']} → {v['new']}"
                           for f, v in ch["field_changes"].items())
        print(f"  ~ #{ch['id']} {ch['name']}: {fields}")
    for ad in plan["additions"][:10]:
        print(f"  + {ad['name']} ({ad['category']}, {ad['method']})")
    for en in plan["endings"][:10]:
        print(f"  ⏹ #{en['id']} {en['name']} ends {en['end_date']}")
    for rm in plan["removals"][:10]:
        print(f"  − #{rm['id']} {rm['name']} (will be deactivated, undoable)")
    for sk in plan["skipped"][:10]:
        print(f"  ⚠ row {sk['row']}: {sk['reason']}")
    if not args.apply:
        print("\n_(preview only — re-run with `import <file> --apply` to apply)_")
        return
    res = apply_import(plan, store, org)
    print(f"\nApplied: {res['applied']}")
    for e in res.get("errors", []):
        print(f"  ⚠ {e}")
    after = _live(c, store, org, cfg, args)
    print("\n" + _verdict_line(after))


# --- mapping ---------------------------------------------------------------------------

def _map_impact(c, store, org, cfg, pattern, category) -> dict:
    from . import taxonomy as tax
    from .mapping import norm
    before, _, _, _ = build_enriched(c, cfg["now"], store.rules(org))
    trial = store.rules(org) + [{"pattern": pattern, "category": category}]
    after, _, _, _ = build_enriched(c, cfg["now"], trial)
    moved = [(b, a) for b, a in zip(before, after) if b.category != a.category]
    by_move: dict[tuple[str, str], float] = {}
    months = set()
    cps: dict[str, str] = {}
    for b, a in moved:
        k = (tax.label(b.category), tax.label(a.category))
        by_move[k] = by_move.get(k, 0.0) + b.amount
        months.add(b.date.strftime("%Y-%m"))
        cps[norm(b.counterparty)] = b.category
    item_moves = []
    for it in store.items(org):
        old_cat = cps.get(norm(it["counterparty"]))
        if old_cat and it["category"] == old_cat:
            item_moves.append((it, f"#{it['id']} {it['name']} → {tax.label(category)}"))
    return {"moved": moved, "n": len(moved),
            "total": round(sum(b.amount for b, _ in moved), 2),
            "by_move": by_move, "months": sorted(months),
            "item_moves": [s for _, s in item_moves],
            "_items": [it for it, _ in item_moves]}


def cmd_map(c, store, args):
    org, cfg = _ctx(c, store, args)
    if args.action in ("preview", "add"):
        if not args.pattern or not args.category:
            print("Give --pattern and --category.")
            return
        slug = _resolve_cat(args.category)
        if slug is None:
            print(f"⚠ '{args.category}' is not a known category.")
            return
        impact = _map_impact(c, store, org, cfg, args.pattern, slug)
        print(report.fmt_map_impact(impact))
        if args.action == "preview":
            print("\n_(preview only — `map add` with the same arguments applies it)_")
            return
        store.add_rule(org, args.pattern, slug)
        from . import taxonomy as tax
        migrated = []
        for it in impact["_items"]:
            new_name = f"{tax.label(slug)} — {it['counterparty']}"
            res = store.recategorize_item(org, it["id"], slug, new_name,
                                          note=f"rule '{args.pattern}' → {slug}")
            migrated.append(f"  • {it['name']}: {res}")
        print(f"\nRule saved: '{args.pattern}' → {slug}. Applies everywhere, "
              f"including past transactions; forecasts recompute on the fly.")
        if migrated:
            print("Line items migrated with it (history kept, undoable):")
            print("\n".join(migrated))
    elif args.action == "list":
        rules = store.rules(org)
        print(f"{len(rules)} owner rule(s):")
        for r in rules:
            print(f"  '{r['pattern']}' → {r['category']}")
    elif args.action == "unmapped":
        enriched, _, _, _ = build_enriched(c, cfg["now"], store.rules(org))
        un = [t for t in enriched if t.category == "unmapped"]
        if not un:
            print("Nothing unmapped — every transaction is classified.")
            return
        print(f"{len(un)} unmapped transaction(s) — propose categories to the owner, "
              f"then `map add --pattern <text> --category <cat>`:")
        for t in un[-15:]:
            print(f"  {t.date} {report.money(t.amount):>12}  {t.counterparty}  ({t.description})")
    elif args.action == "score":
        enriched, _, _, _ = build_enriched(c, cfg["now"], store.rules(org))
        truth = c._get(f"{c.sim}/truth/labels")["labels"]
        scored = [(t.category, truth.get(t.txn_id)) for t in enriched if t.txn_id in truth]
        hit = sum(1 for got, want in scored if got == want)
        print(f"Mapping accuracy: {hit}/{len(scored)} = {hit / len(scored):.1%}")
        if args.detail == "accountant":
            by_src = {}
            for t in enriched:
                by_src[t.map_source] = by_src.get(t.map_source, 0) + 1
            print(f"Sources: {json.dumps(by_src)}")


# --- sim -------------------------------------------------------------------------------

def cmd_now(c, store, args):
    print(c.now())


def cmd_orgs(c, store, args):
    for o in c._get(f"{c.sim}/orgs")["orgs"]:
        print(f"{o['slug']:12s} {o['name']} (scenario {o['scenario']})")


def cmd_advance(c, store, args):
    print(c.advance(args.days))


def cmd_set(c, store, args):
    print(c.set_date(args.date))


def cmd_reset(c, store, args):
    print(c.reset())


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="cashew")
    p.add_argument("--base", default=DEFAULT_BASE)
    p.add_argument("--detail", choices=["owner", "accountant"], default=None,
                   help="owner = calm plain English (default); accountant = full tables")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, org=True, month=True, setup=None):
        sp = sub.add_parser(name)
        if org:
            sp.add_argument("--org", default=None)
        if month:
            sp.add_argument("--month", default=None)
        # accepted in both positions: `cashew --detail accountant reconcile`
        # and `cashew reconcile --detail accountant` (SUPPRESS so the
        # subcommand only overrides when actually given)
        sp.add_argument("--detail", choices=["owner", "accountant"],
                        default=argparse.SUPPRESS)
        if setup:
            setup(sp)
        sp.set_defaults(func=fn)

    def outlook_setup(sp):
        sp.add_argument("--months", type=int, default=3)
        sp.add_argument("--grain", choices=["week", "month"], default="week")
        sp.add_argument("--scenario", default=None)
        sp.add_argument("--floor", type=float, default=None,
                        help="owner's minimum acceptable cash balance (persisted)")
    add("outlook", cmd_outlook, month=False, setup=outlook_setup)
    add("forecast", cmd_forecast,
        setup=lambda sp: sp.add_argument("--refreeze", action="store_true"))
    add("debtors", cmd_debtors, month=False)
    add("creditors", cmd_creditors, month=False)
    add("commitments", cmd_commitments, month=False)
    add("status", cmd_status,
        setup=lambda sp: sp.add_argument("--floor", type=float, default=None))
    add("reconcile", cmd_reconcile,
        setup=lambda sp: sp.add_argument("--as-of", dest="as_of", default=None))
    add("vat", cmd_vat)
    add("tax", cmd_vat)                            # alias: the full tax position
    add("weekly", cmd_weekly,
        setup=lambda sp: sp.add_argument("--floor", type=float, default=None))

    def daily_setup(sp):
        sp.add_argument("--advance", action="store_true",
                        help="move the sim clock 1 day first (the morning tick)")
        sp.add_argument("--no-advance", action="store_true",
                        help=argparse.SUPPRESS)   # v4 compat: now the default
        sp.add_argument("--floor", type=float, default=None)
    add("daily", cmd_daily, setup=daily_setup)
    add("sync", cmd_sync, month=False)
    add("items", cmd_items, month=False)
    add("lineitems", cmd_items, month=False)      # alias

    def item_setup(sp):
        sp.add_argument("action", choices=["add", "set", "end", "rm", "undo"])
        sp.add_argument("--id")
        sp.add_argument("--name")
        sp.add_argument("--category")
        sp.add_argument("--method",
                        choices=["recurring_fixed", "recurring_variable", "one_off", "linked"])
        sp.add_argument("--amount", type=float)
        sp.add_argument("--date")
        sp.add_argument("--day", type=int)
        sp.add_argument("--pct", type=float)
        sp.add_argument("--target")
        sp.add_argument("--start")
        sp.add_argument("--end")
        sp.add_argument("--counterparty")
        sp.add_argument("--params")
        sp.add_argument("--note")
    add("item", cmd_item, month=False, setup=item_setup)

    add("changes", cmd_changes, month=False,
        setup=lambda sp: sp.add_argument("--since", default=None))

    def assume_setup(sp):
        sp.add_argument("action", choices=["add", "list", "rm"])
        sp.add_argument("--category")
        sp.add_argument("--amount", type=float)
        sp.add_argument("--date")
        sp.add_argument("--note")
        sp.add_argument("--id", type=int)
    add("assume", cmd_assume, month=False, setup=assume_setup)

    def settings_setup(sp):
        sp.add_argument("--floor", type=float, default=None)
        sp.add_argument("--materiality", type=float, default=None)
        sp.add_argument("--pot", default=None,
                        help="exact display name of the VAT pot account")
        sp.add_argument("--persona", choices=["owner", "accountant"], default=None)
    add("settings", cmd_settings, month=False, setup=settings_setup)

    add("lessons", cmd_lessons, month=False)
    add("compare", cmd_compare, month=False, setup=lambda sp: (
        sp.add_argument("--a", required=True), sp.add_argument("--b", required=True)))

    def whatif_setup(sp):
        sp.add_argument("--scale", action="append")
        sp.add_argument("--add", action="append")
        sp.add_argument("--delay", action="append",
                        help='"<item-or-category>=<days>", e.g. "Regular Customers=30"')
        sp.add_argument("--drop", action="append")
        sp.add_argument("--item", action="append",
                        help='"Name:key=value[,key=value]" — patch one line item, '
                             'e.g. "Payroll — new hire:start_date=2026-09-01"')
        sp.add_argument("--months", type=int, default=3)
    add("whatif", cmd_whatif, month=False, setup=whatif_setup)

    def scenario_setup(sp):
        sp.add_argument("action", choices=["save", "list", "run", "compare"])
        sp.add_argument("name", nargs="?")
        sp.add_argument("name2", nargs="?")
        sp.add_argument("--scale", action="append")
        sp.add_argument("--add", action="append")
        sp.add_argument("--delay", action="append")
        sp.add_argument("--drop", action="append")
        sp.add_argument("--item", action="append")
        sp.add_argument("--months", type=int, default=3)
    add("scenario", cmd_scenario, month=False, setup=scenario_setup)

    add("export", cmd_export, month=False, setup=lambda sp: (
        sp.add_argument("--out", default=None), sp.add_argument("--months", type=int, default=3)))
    add("import", cmd_import, month=False, setup=lambda sp: (
        sp.add_argument("path"),
        sp.add_argument("--apply", action="store_true",
                        help="apply the changes (default is preview only)")))

    def map_setup(sp):
        sp.add_argument("action", choices=["list", "unmapped", "preview", "add", "score"])
        sp.add_argument("--pattern")
        sp.add_argument("--category")
    add("map", cmd_map, month=False, setup=map_setup)

    add("now", cmd_now, org=False, month=False)
    add("orgs", cmd_orgs, org=False, month=False)
    add("reset", cmd_reset, org=False, month=False)
    add("advance", cmd_advance, org=False, month=False,
        setup=lambda sp: sp.add_argument("--days", type=int, default=1))
    add("set", cmd_set, org=False, month=False,
        setup=lambda sp: sp.add_argument("--date", required=True))

    args = p.parse_args(argv)
    c = Clients(args.base)
    store = Store()
    if args.detail is None:
        args.detail = "owner"      # resolved per-org below once we know the org
    try:
        # per-org persona default (only when the flag wasn't given explicitly)
        args.func(c, store, args)
        # maintain the "since you last looked" cursor for read commands
        if args.cmd in READ_CMDS and _LAST["org"]:
            from datetime import datetime
            org = _LAST["org"]
            if _LAST["live"] is not None:
                live = _LAST["live"]
                snap = {"clock": _LAST["clock"],
                        "verdict_icon": live["verdict"][0],
                        "cash": live["opening_balance"],
                        "low_date": live["low_point"]["date"],
                        "close": live["close"]}
                store.set_setting(org, "view_snapshot", json.dumps(snap))
            store.set_setting(org, "last_viewed_at",
                              datetime.now().isoformat(timespec="seconds"))
        return 0
    except Exception as e:                                    # noqa: BLE001
        import requests as _rq
        if isinstance(e, _rq.exceptions.ConnectionError):
            print("⚠ I can't reach the data feed right now — the server on "
                  ":8900 looks down. Try again in a minute.")
        elif isinstance(e, (ValueError, KeyError, json.JSONDecodeError)):
            print(f"⚠ I couldn't read that ({e}). Dates are YYYY-MM-DD, "
                  f"amounts are plain numbers, params are JSON.")
        else:
            print(f"⚠ Something went wrong running `{args.cmd}`: {e}")
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
