# Cashew v5 — Cashflow Assistant Harness

**v5 (launch):** ONE forecast path behind every view (outlook/weekly/status/
whatif can no longer disagree — the v4 dual-engine drift is gone); verdict
scans a 6-month risk horizon on every command (a December cliff colours a
3-month view); accrual-aware **VAT module** with real UK due dates that
settles itself when the payment lands, plus a **corporation tax module**;
every edit prints its **before→after impact** with an undo hint; automatic
**"since you last looked"** diffs; inline **why-drivers** on busy weeks; one
org-scaled **materiality knob** replacing 10 scattered constants; honest
uncertainty (volatile series project flat medians, ranges shown only when
meaningful, hollow trailing months excluded from trends); **emerging-pattern**
suggestions (1-month lag) + gone-quiet end-date proposals; safe
**recategorization** (map preview → apply migrates line items, reconcile
matches across a mid-month re-code with no phantom variance); **Excel
round-trip** (export → accountant edits → `import` previews → applies,
undoable); owner/accountant **personas**; scenario **item patches** +
`scenario compare`; friendly one-line errors (no tracebacks to chat); `daily`
is read-only (the clock only moves when asked). Harness: corp-tax history,
VAT cadence into the extension window, seeded emerging-pattern fixtures,
`POST /sim/recategorize` + truth endpoints. 100+ tests.

**v4:** full month simulation (synthetic continuation to 2026-08-31),
DD/standing-orders/pending endpoints, AR/AP aging (`debtors`/`creditors`),
`commitments`, owner cash floor, delay what-ifs, VAT accrual cross-check,
audit-hardened engine. 65 tests.

A local test bed for the Cashew cashflow-forecasting engine. It serves the
synthetic design-partner data as **two provider APIs that look like the real
ones** — TrueLayer (open banking) and Xero (accounting) — behind a shared
**virtual clock**, so you can "replay time" and watch actuals arrive day by day.

It also **synthesises everything the reconciled CSVs lack** (opening balances,
the VAT pot, forward AR/AP commitments with realistic payment lateness) and
exposes a **ground-truth oracle** so tests can assert the engine caught what
each scenario planted.

> **Data is private.** `data/ailabgemini/` is synthetic but derived from real
> design-partner statements — do not share or send to any external service.
> The mock approach keeps everything local by design.

---

## What's in the box

```
cashew/
├── data/ailabgemini/        # JAM Ltd — 3 scenarios, 2 CSVs each (SASH/Tritility archived out)
├── mocks/                   # the harness (FastAPI)
│   ├── app.py               # 3 routers under one service
│   ├── config.py            # env-driven settings (org, anchor, seed…)
│   ├── orgs.py              # registry of the JAM orgs (3 scenarios)
│   ├── models.py            # canonical in-memory model
│   ├── loader.py            # CSV → canonical + chart of accounts
│   ├── clock.py             # persisted virtual clock + active org
│   ├── synth.py             # AR/AP, VAT pot, cash position, scenario detection
│   ├── store.py             # assembles + caches each org
│   └── routers/
│       ├── openbanking.py   # TrueLayer Data API v1 shape
│       ├── xero.py          # Xero Accounting API 2.0 shape
│       └── sim.py           # clock control + /truth oracle
├── sim/runner.py            # reference cadence runner + oracle (demo/smoke)
├── tests/                   # pytest suite
├── Dockerfile / docker-compose.yml / requirements.txt / run.sh / .env.example
```

Scoped to **JAM Ltd** — `jam-scn-{1,2,3}` (VAT liability / dividend surge /
supplier cost-creep, with an underfunded VAT pot). The SASH and Tritility
datasets have been archived out of the project (`~/cashew_archive_nonjam/`).

---

## Run it

**Local (venv):**
```bash
./run.sh                       # http://localhost:8900  (docs at /docs)
```

**Docker:**
```bash
mkdir -p state
docker compose up --build      # serves on :8900, data mounted read-only
```

**Point the Cashew engine at it** — set the connector base URLs; swap toward real
TrueLayer/Xero in prod (connector shapes match; real auth/pagination/error
handling still needed):
```
OPENBANKING_BASE_URL=http://localhost:8900/openbanking/data/v1
XERO_BASE_URL=http://localhost:8900/xero/api.xro/2.0
```

---

## The virtual clock (time travel)

Every read is filtered to `date <= sim_now`; balances are reported as-of the
clock. Advance the clock and tomorrow's actuals appear.

```bash
curl -s localhost:8900/sim/now
curl -s -XPOST localhost:8900/sim/reset                       # -> anchor (2026-06-30)
curl -s -XPOST localhost:8900/sim/advance -d '{"days":1}' -H 'content-type: application/json'
curl -s -XPOST localhost:8900/sim/org     -d '{"slug":"jam-scn-2"}' -H 'content-type: application/json'
```

## Endpoints

**Open banking (TrueLayer-shaped)** — `/openbanking/data/v1`
`GET /accounts` · `/accounts/{id}` · `/accounts/{id}/balance` ·
`/accounts/{id}/transactions?from=&to=` · `/accounts/{id}/transactions/pending`

**Xero (Accounting API 2.0-shaped)** — `/xero/api.xro/2.0`
`GET /Organisation` · `/Accounts` · `/BankTransactions?page=` ·
`/Invoices?type=ACCREC|ACCPAY&page=`   *(Invoices are synthesised AR/AP.)*

**Sim + oracle** — `/sim`
`GET /now` · `/config` · `/orgs` · `POST /set|/advance|/reset|/org` ·
`GET /truth/vat` · `/truth/opening_balance` · `/truth/scenario` · `/truth/expected`

`/sim/truth/expected` is the oracle: opening cash (operating vs ring-fenced VAT
pot vs total), the VAT funding gap, and the scenario's material signals bucketed
**predictable / surprise / trend**, plus open AR/AP at the anchor.

---

## What is real vs synthesised

| Comes straight from the CSVs | Synthesised (deterministic, seed-controlled) |
|---|---|
| Bank transactions (Monzo feed) + running balances | Forward **AR/AP invoices** with issue/due dates |
| Xero reconciled ledger (GL code, tax type) | **Payment lateness** per counterparty (DSO signal) |
| The **VAT pot** account balance (it's a real account!) | Chart of accounts (derived from GL codes) |
| Ground-truth `Cashew Category` on every txn | Scenario signal classification for the oracle |

## Cadence runner (proof it works end-to-end)

With the server running:
```bash
python -m sim.runner --org jam-scn-1
```
It forecasts at the anchor (naive run-rate reference), flags the VAT-pot
shortfall, advances daily through the scenario month reconciling actuals, prints
a weekly owner status and a month-end post-mortem, then asserts — using **only
the public APIs** — that it caught what the scenario planted.

> This runner is a *reference consumer + oracle*, not the product engine. It
> demonstrates the cadence and validates the harness.

## Tests
```bash
pytest -q          # in-process (TestClient), no server needed
```

## Configuration (env / `.env`)
`CASHEW_ORG`, `CASHEW_ANCHOR`, `CASHEW_SCENARIO_MONTH`, `CASHEW_SEED`,
`CASHEW_DATA_ROOT`, `CASHEW_STATE_FILE`. The July scenario month is
intentionally front-loaded (shocks on the 1st); for a dense month-long
reconcile demo, anchor on an earlier full month (see `.env.example`).

## Synthetic continuation (full simulation)

The source CSVs end at the Jul-1 scenario shocks. At load time the harness
**extends each org deterministically** (seeded per counterparty/month) from the
day after the last real transaction through `CASHEW_EXTEND_UNTIL` (default
2026-08-31): regulars keep their day-of-month cadence, variable vendors get
±10% noise around their median, a paired monthly VAT-pot sweep keeps
cross-account cash netting, matching Xero ledger rows are generated (GL coding
learned from the org's own history), and months the scenario authors already
wrote are never double-booked. Disable with `CASHEW_EXTEND=0`. Source CSVs are
never modified.

## Direct debits, standing orders, pending

TrueLayer-shaped, derived live from behavior visible at the clock:
- `GET /accounts/{id}/standing_orders` — fixed-amount, fixed-day regulars
  (cv ≤ 5%), with `next_payment_date`/`next_payment_amount`
- `GET /accounts/{id}/direct_debits` — variable recurring mandates, with the
  last pull amount/date
- `GET /accounts/{id}/transactions/pending` — transactions dated within the
  last `CASHEW_PENDING_DAYS` (default 1) of the clock, `status: pending`
  (simplification: they also appear on `/transactions`)

Engine surface: `./cashew commitments`.

## Notes on fidelity
- Open-banking amounts are signed with `transaction_type` CREDIT/DEBIT
  (TrueLayer convention). **The feed does not leak ground-truth categories** —
  the engine must classify via its mapping layer (Xero GL join + learned
  rules). Truth is available for scoring only at `/sim/truth/labels`
  (set `CASHEW_LEAK_LABELS=1` to re-enable the debug leak).
- **Xero runs behind the bank** by `CASHEW_XERO_LAG_DAYS` (default 3) — like
  real bookkeeping — so the newest transactions are always unbooked and the
  mapping problem stays live.
- Xero dates are ISO-8601 strings (both `Date` and `DateString`) rather than the
  legacy `/Date(ms)/`; pagination mirrors Xero (100/page, `?page=all` to disable).
- Auth is stubbed (no OAuth) — this is a local test double.

## The engine (`engine/`)
Deterministic, line-item-based, LLM-free:
`actuals` (bank+Xero join & 5-layer mapping) → `lineitems` (cadence detection,
hollow-month-aware) → `compute` (**the single forecast path**: config items →
methods → invoice supersession → VAT + corp-tax modules → buckets with
why-drivers, risk-horizon verdict) → `reconcile` (occurrence matching incl.
cross-category recategorization safety; on_track / timing / amount / missing /
surprise; lateness learning; plain-English lessons). `forecast.py` holds the
invoice + tax modules; `xlsx_import.py` is the Excel round-trip. CLI:
`./cashew outlook|forecast|status|reconcile|vat|weekly|daily|items|item|
assume|settings|sync|changes|lessons|compare|whatif|scenario|export|import|
map|debtors|creditors|commitments` (+ sim: now|orgs|advance|set|reset;
`--detail owner|accountant`). State (immutable forecast versions, rules,
line-item history with undo, scenarios, counterparty stats, lessons, the
last-viewed cursor) lives in `engine.db`.
