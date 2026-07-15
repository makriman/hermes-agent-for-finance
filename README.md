# Hermes Agent for Finance

**A chat-native cashflow-forecasting assistant for small businesses — a deterministic
finance engine wearing a conversational skin, delivered over WhatsApp and Telegram.**

Built on top of [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent),
this project adds two things the base agent doesn't have:

1. **Cashew** — a deterministic, auditable cashflow-forecasting engine (an editable
   line-item forecast, VAT/corporation-tax modules, variance reconciliation, scenarios,
   Excel round-trip) where **the language model never touches a number**.
2. **A native WhatsApp Cloud API platform** for the Hermes gateway, at full feature
   parity with the built-in Telegram adapter — voice notes, images, documents, native
   file attachments, quoted replies, Opus voice bubbles — plus the skill and glue that
   turn the agent into a finance assistant.

> ⚠️ **Prototype.** This was built as a design-partner demo with **synthetic data**. It is
> not a production banking system and gives general information, not regulated financial
> advice. See [Security & data](#security--data).

---

## Why it's different

Most "AI finance" tools let the model do arithmetic and hope it's right. Cashew does the
opposite:

| Principle | What it means |
|---|---|
| **The LLM never computes** | Every figure comes from a deterministic Python engine run as a CLI. The model translates plain English → commands, and relays the output. Same clock in → byte-identical numbers out. |
| **Editable, not magic** | The forecast is a set of **line items the owner edits by chatting** ("rent is £2k on the 1st", "hiring at £4k/mo from September"). Four methods: fixed, trend, one-off, and linked-%. This is the 80/20 product — a great first pass, then the owner tweaks. |
| **Verdict first** | Every view opens with a 🟢 / 🟡 / 🔴 scanned over a 6-month risk horizon, not a wall of numbers. |
| **Trust through variance** | Frozen baselines + reconciliation against real actuals, with the *cause* of every miss (timing / amount / missing / surprise) and a plain-English lesson. |
| **As-of everything** | Cash moves as the clock moves. Every output is stamped `🕐 as of <date> · <N> txns`, and the assistant is forbidden from reusing a stale figure. |

---

## Architecture

```
     WhatsApp  ─┐                            Telegram ─┐
 (Meta Cloud    │  webhook / long-poll                 │
  API webhook)  ▼                                       ▼
        ┌───────────────────────────────────────────────────┐
        │           Hermes Agent gateway (warm)              │
        │   Claude Sonnet (via GitHub Copilot, text-only)    │
        │   • native whatsapp_cloud adapter  • telegram      │
        │   • loads the "cashew" skill                       │
        └───────────────────────┬───────────────────────────┘
                                 │  runs the CLI, relays output
                                 ▼
        ┌───────────────────────────────────────────────────┐
        │        Cashew engine  (deterministic Python)       │
        │   line-item forecast · VAT/CT · reconcile ·        │
        │   scenarios · Excel export/import                  │
        │                   ⇅  engine.db                     │
        └───────────────────────┬───────────────────────────┘
                                 │  reads bank + ledger
                                 ▼
        ┌───────────────────────────────────────────────────┐
        │   Mock harness (:8900)  — swap for real in prod    │
        │   TrueLayer-shaped open banking + Xero-shaped API  │
        │   behind a virtual clock + ground-truth oracle     │
        └───────────────────────────────────────────────────┘
```

The engine reads its data through **provider-shaped APIs** (TrueLayer for open banking,
Xero for accounting). In this repo those are served by a **local mock harness** so you can
replay time and watch actuals arrive day by day; in production you point the same engine at
the real TrueLayer + Xero.

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
│   ├── promo/                  #   demo video + render scripts
│   ├── cashew                  #   CLI entrypoint
│   └── run.sh                  #   start the mock harness on :8900
│
├── hermes-finance/             # Our custom layer for the Hermes gateway
│   ├── platforms/
│   │   └── whatsapp_cloud.py   #   NEW: native WhatsApp Cloud API adapter (full file)
│   ├── patches/                #   git-diff patches for 5 modified upstream files
│   ├── apply.sh                #   drop the adapter in + apply the patches
│   ├── config.yaml.example     #   the finance-relevant config additions
│   ├── env.example             #   WhatsApp / Telegram / STT / Langfuse template
│   └── UPSTREAM_BASE_COMMIT.txt#   the hermes-agent commit the patches apply to
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
./cashew whatif --scale revenue=0.5      # instant what-if
./cashew export                  # → an .xlsx the accountant can edit and re-import
```

Highlights:

- **One compute path** behind every view (`outlook`/`weekly`/`status`/`whatif` can't disagree).
- **Four forecast methods** with piecewise start/end dates: `recurring_fixed`,
  `recurring_variable` (trend), `one_off`, `linked` (% of another category).
- **VAT + corporation-tax modules** — accrual-aware estimates, real UK due dates, that
  settle themselves when the payment lands.
- **Reconciliation** matches planned occurrences to actuals and classifies each variance
  (`on_track` / `timing` / `amount` / `missing` / `surprise`), learning debtor payment
  timing (DSO) as it goes.
- **Scenarios & what-ifs** as flavors of the same line items; **Excel round-trip**
  (export → accountant edits → `import` previews → applies, undoable).
- **Safe & reversible** — every edit prints its before→after impact + an undo hint; owner
  edits are locked against auto-sync.

See [`cashew/README.md`](cashew/README.md) for the full command reference and design notes.

### The mock harness

`cashew/mocks/` serves the synthetic dataset as **two APIs that look like the real ones**:

- **TrueLayer-shaped** open banking (`/openbanking/data/v1/...`, plus `/standing_orders`,
  `/direct_debits`, `/transactions/pending`) — and it does **not** leak categories, so
  mapping is a real problem.
- **Xero-shaped** accounting (`/xero/api.xro/2.0/...`, `/Contacts`) — lagged behind the
  bank feed by a few days, like reality.
- A shared **virtual clock** (`/sim`) you can advance to replay time, and a **ground-truth
  oracle** (`/sim/truth/...`) so tests can assert the engine caught what each scenario planted.

---

## The native WhatsApp Cloud adapter

The headline of `hermes-finance/`. `platforms/whatsapp_cloud.py` is a from-scratch Hermes
gateway platform that speaks the **official Meta WhatsApp Cloud API** and dispatches inbound
messages straight into the *same warm-agent path Telegram uses* (cached agent, hot prompt
cache, persistent session). That single decision is what makes it fast — an external bridge
or a rebuilt-per-request server runs ~2× slower.

What it does, at Telegram parity:

- **Inbound**: text, **voice notes** (downloaded + transcribed via the gateway's STT path),
  images & stickers, **documents** (PDF/Excel/CSV/Word — extracted and, for Excel, routable
  straight into `cashew import`), video, location, contacts, reactions — each cached so the
  gateway's existing vision/STT/doc-extraction paths pick it up. The webhook returns `200`
  immediately and downloads in the background (no Meta retries).
- **Outbound**: native file attachments via `/media` upload — so the **Excel export actually
  arrives as a file**, images/voice/video send natively, and **TTS replies arrive as Opus
  voice bubbles**.
- **Quoted replies** (WhatsApp `context.message_id`), WhatsApp-native markdown, a live
  typing indicator, and a WhatsApp-first UX (no "home channel" nag, final-answer-only —
  no tool chatter).
- **Anti-misroute guard**: a reply in a live WhatsApp chat can never leak to another
  platform (a bug we hit where voice replies double-sent to Telegram).

The 5 small upstream patches wire the adapter into the gateway (platform enum, adapter
factory, auth maps), teach the `send_message` tool + TTS to treat WhatsApp as media-capable,
and add the WhatsApp platform hint. Total custom surface: **1 new file + ~140 changed lines**.

---

## Quickstart

### 1. The engine (standalone)

```bash
cd cashew
python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
cp .env.example .env                 # defaults are fine
./run.sh &                           # mock harness on :8900
./cashew outlook                     # you should see a 🟢/🟡/🔴 verdict
pytest -q                            # ~150 tests
```

> The design-partner dataset is **not** included (see [Security & data](#security--data)).
> Drop your own CSVs into `cashew/data/<dataset>/<Org>/` — the format is documented in
> [`cashew/data/README.md`](cashew/data/README.md).

### 2. The Hermes finance layer

You need a working [hermes-agent](https://github.com/NousResearch/hermes-agent) checkout
and a model provider configured (this build used Claude Sonnet via GitHub Copilot).

```bash
# from a hermes-agent checkout at the pinned base commit
cd /path/to/hermes-agent
git checkout $(cat /path/to/this-repo/hermes-finance/UPSTREAM_BASE_COMMIT.txt)

/path/to/this-repo/hermes-finance/apply.sh /path/to/hermes-agent   # adapter + patches

# copy the skill + config, fill in secrets
cp -r /path/to/this-repo/skills/cashew ~/.hermes/skills/
#   merge hermes-finance/config.yaml.example into ~/.hermes/config.yaml
#   fill ~/.hermes/.env from hermes-finance/env.example  (never commit it)
```

Then run the gateway. Put a public HTTPS tunnel (e.g. `cloudflared`) in front of the
adapter's local port and point your Meta webhook at `https://<tunnel>/wa/webhook`.

---

## Observability

The gateway traces to **Langfuse** (native Hermes plugin). Each cashflow turn shows up as a
nested trace — the LLM generations plus each Cashew tool span — so you can see exactly which
CLI commands ran and what they returned.

---

## What's custom vs. upstream

| Piece | Origin |
|---|---|
| `cashew/` (engine, mocks, sim, tests, skill) | **100% original** |
| `hermes-finance/platforms/whatsapp_cloud.py` | **Original** (new Hermes platform) |
| `hermes-finance/patches/*` | **Original** diffs against upstream files |
| The gateway, tool framework, model plumbing they patch | [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) (MIT) |

---

## Security & data

- **No secrets in this repo.** All tokens/keys live in a git-ignored `.env`. `*.example`
  files are templates only. If you cloned this, rotate any credential you ever pasted
  anywhere.
- **The dataset is excluded on purpose.** The original fixtures were synthetic *but derived
  from real design-partner statements* and marked do-not-share; publishing them would send
  them to an external service. Only the **format** is documented, so you can supply your own.
- **Not financial advice.** This is a prototype that surfaces general cashflow information.
  Anything customer-facing should go through proper compliance review.

---

## Credits & license

The `hermes-finance/` integration modifies and redistributes parts of
[NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent), which is
MIT-licensed — see [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

The original Cashew engine and the WhatsApp Cloud adapter in this repository are a prototype;
the copyright holder should choose and add a `LICENSE` before any external distribution.
