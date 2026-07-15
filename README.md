# Hermes Agent for Finance

**A chat-native cashflow-forecasting assistant for small businesses — a deterministic
finance engine wearing a conversational skin, delivered over WhatsApp and Telegram.**

Built on [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent), this
project's original contribution is **Cashew**: a deterministic, auditable cashflow-forecasting
engine (an editable line-item forecast, VAT/corporation-tax modules, variance reconciliation,
scenarios, Excel round-trip) where **the language model never touches a number** — plus the
skill and config that wire it into the Hermes gateway.

> ⚠️ **Prototype.** Built as a design-partner demo with **synthetic data**. Not a production
> banking system; surfaces general information, not regulated financial advice. See
> [Security & data](#security--data).

> ℹ️ **On WhatsApp:** hermes-agent (≥ v0.18) ships a **native WhatsApp Cloud API platform**
> and a native Telegram platform out of the box. This project *uses* them — it does not
> provide them. (An earlier version shipped a custom WhatsApp Cloud adapter; once upstream's
> landed — a strict superset — ours was retired. It's in git history and
> [`hermes-finance/`](hermes-finance/) explains the transition.)

---

## Why it's different

Most "AI finance" tools let the model do arithmetic and hope it's right. Cashew does the
opposite:

| Principle | What it means |
|---|---|
| **The LLM never computes** | Every figure comes from a deterministic Python engine run as a CLI. The model translates plain English → commands and relays the output. Same clock in → byte-identical numbers out. |
| **Editable, not magic** | The forecast is a set of **line items the owner edits by chatting** ("rent is £2k on the 1st", "hiring at £4k/mo from September"). Four methods: fixed, trend, one-off, linked-%. |
| **Verdict first** | Every view opens with a 🟢 / 🟡 / 🔴 scanned over a 6-month risk horizon, not a wall of numbers. |
| **Trust through variance** | Frozen baselines + reconciliation against real actuals, with the *cause* of every miss (timing / amount / missing / surprise). |
| **As-of everything** | Cash moves as the clock moves. Every output is stamped `🕐 as of <date> · <N> txns`; the assistant may not reuse a stale figure. |

---

## Architecture

```
     WhatsApp  ─┐                            Telegram ─┐
 (Meta Cloud    │  webhook / long-poll                 │
  API webhook)  ▼                                       ▼
        ┌───────────────────────────────────────────────────┐
        │        Hermes Agent gateway (warm)  — v0.18        │
        │   Claude Sonnet (via GitHub Copilot, text-only)    │
        │   • NATIVE whatsapp_cloud + telegram platforms     │
        │   • loads the "cashew" skill                       │
        └───────────────────────┬───────────────────────────┘
                                 │  runs the CLI, relays output
                                 ▼
        ┌───────────────────────────────────────────────────┐
        │        Cashew engine  (deterministic Python)       │  ← this repo's original work
        │   line-item forecast · VAT/CT · reconcile ·        │
        │   scenarios · Excel export/import   ⇅ engine.db    │
        └───────────────────────┬───────────────────────────┘
                                 │  reads bank + ledger
                                 ▼
        ┌───────────────────────────────────────────────────┐
        │   Mock harness (:8900)  — swap for real in prod    │
        │   TrueLayer-shaped open banking + Xero-shaped API  │
        │   behind a virtual clock + ground-truth oracle     │
        └───────────────────────────────────────────────────┘
```

---

## Repository layout

```
hermes-agent-for-finance/
├── cashew/                     # The deterministic cashflow engine (100% original)
│   ├── engine/                 #   compute · methods · reconcile · mapping · vat · export · cli …
│   ├── mocks/                  #   TrueLayer + Xero + virtual-clock + truth-oracle harness
│   ├── sim/                    #   oracle test runner
│   ├── tests/                  #   ~150 tests
│   ├── data/                   #   (dataset excluded — see cashew/data/README.md)
│   ├── promo/cashew-promo.mp4
│   ├── cashew                  #   CLI entrypoint
│   └── run.sh                  #   start the mock harness on :8900
│
├── hermes-finance/             # Thin layer that wires Cashew into the gateway
│   ├── README.md               #   how to set up + the upstream-adapter transition
│   ├── config.yaml.example     #   finance-relevant config additions
│   ├── env.example             #   model / native Telegram + WhatsApp Cloud / STT / Langfuse
│   └── UPSTREAM_BASE_COMMIT.txt #   validated against hermes-agent v0.18.2
│
└── skills/
    └── cashew/SKILL.md         # The Hermes skill that drives the engine from chat
```

---

## The Cashew engine

A deterministic CLI over open-banking + accounting data. The forecast is an **editable
line-item config** recomputed on the fly, never a black box.

```bash
cd cashew
./run.sh &                       # start the mock harness on :8900
./cashew outlook                 # 🟢/🟡/🔴 + 3-month weekly cash view
./cashew vat                     # accrual-aware VAT position, real UK due dates
./cashew reconcile               # plan vs actuals, with the cause of every miss
./cashew item add --name "Payroll — new hire" --category payroll \
                  --method recurring_fixed --amount -4000 --day 28 --start 2026-09-01
./cashew whatif --scale revenue=0.5
./cashew export                  # → an .xlsx the accountant can edit and re-import
```

Highlights: one compute path behind every view; four forecast methods with piecewise
start/end dates; VAT + corporation-tax modules with real UK due dates; reconciliation that
classifies every variance and learns debtor timing (DSO); scenarios/what-ifs as flavors of
the same line items; Excel round-trip; every edit prints its before→after impact + an undo
hint. See [`cashew/README.md`](cashew/README.md) for the full command reference.

### The mock harness

`cashew/mocks/` serves the synthetic dataset as **two APIs that look like the real ones** —
TrueLayer-shaped open banking (no category leakage, so mapping is a real problem) and
Xero-shaped accounting (lagged behind the bank feed) — behind a shared **virtual clock** you
can advance to replay time, plus a **ground-truth oracle** for tests. In production you point
the same engine at real TrueLayer + Xero.

---

## Quickstart

### 1. The engine (standalone)

```bash
cd cashew
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env
./run.sh &                           # mock harness on :8900
./cashew outlook                     # 🟢/🟡/🔴 verdict
pytest -q                            # ~150 tests
```

> The design-partner dataset is **not** included (see [Security & data](#security--data)).
> Drop your own CSVs into `cashew/data/<dataset>/<Org>/` — format in
> [`cashew/data/README.md`](cashew/data/README.md).

### 2. The chat assistant

Install hermes-agent (≥ v0.18), then wire in the finance layer — see
[`hermes-finance/README.md`](hermes-finance/README.md). In short: copy `skills/cashew` into
`~/.hermes/skills/`, merge `hermes-finance/config.yaml.example` into your config, and set the
Telegram + WhatsApp Cloud env vars from `hermes-finance/env.example`. Both platforms are
native in upstream — no adapter code to install.

---

## What's original vs. upstream

| Piece | Origin |
|---|---|
| `cashew/` (engine, mocks, sim, tests) | **100% original** |
| `skills/cashew/SKILL.md` + config/env glue | **Original** |
| WhatsApp Cloud + Telegram platforms | [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) (MIT) — native |
| The gateway, tool framework, model plumbing | hermes-agent (MIT) |

---

## Security & data

- **No secrets in this repo.** All tokens/keys live in a git-ignored `.env`; `*.example`
  files are templates. Rotate any credential you ever pasted anywhere.
- **The dataset is excluded on purpose** — the original fixtures were synthetic *but derived
  from real design-partner statements* and marked do-not-share. Only the format is documented.
- **Not financial advice.** A prototype that surfaces general cashflow information; anything
  customer-facing needs proper compliance review.

---

## Credits & license

WhatsApp Cloud, Telegram, and the gateway come from
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) (MIT) — see
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). The original Cashew engine in this
repository is a prototype; the copyright holder should choose and add a `LICENSE` before any
external distribution.
