# Cashew on WhatsApp — pricing scope (July 2026)

**Integration:** Meta Cloud API direct (free hosting, no BSP, no markup). One-off setup:
business verification, display-name number, digest template approval, webhook (Hermes gateway).

## Policy context
Meta banned **general-purpose AI assistants** (ChatGPT/Perplexity-class) from the WhatsApp
Business API (Jan 2026); the EU ordered access restored and Meta's fee-based fix
(~$0.0625/msg, later "free up to a cap") is still under EC review. **Business-focused bots
are unaffected** and pay standard rates. Cashew positioned as a feature of the business
account (digests + account Q&A) sits on the safe side; a standalone "AI assistant" framing
is the contested category. ⚠ Positioning needs Compliance/Legal sign-off before build.

## How Meta charges (per delivered message, since Jul 2025)
| Type | UK rate | What it covers for us |
|---|---|---|
| Service (inside 24h window opened by a user message) | **Free**, uncapped, free-form | All interactive Q&A — the bulk of volume |
| Utility template | **£0.0159** (free if a window is open) | The 08:00 daily / Monday weekly digest |
| Marketing template | £0.0382 (charged even in-window) | Avoid — keep promo out of digest templates |

Business-initiated messages with no open window **must** be templates (free-form is rejected,
error 131047). Every user reply re-opens the window for 24h → an engaged owner's digests are
free; only fully passive users pay the utility rate.

## Cost estimate (per business: ~34 digests + ~20 queries/month)
| Scale | Meta cost/month | Notes |
|---|---|---|
| Pilot (3 recipients) | ~£1.60 | worst case, all digests billed |
| 100 SMEs | ~£54 | ~£6.50/business/year |
| 1,000 SMEs | ~£540 | scales linearly, no volume tiers |

**Worst case £0.54/business/month; ~£0 when engaged.** Meta is not the cost driver — LLM
inference for the chat layer (~£1–3/business/month) is 2–5× the entire Meta bill.

*Rates move (UK marketing rose Jul 2026) — verify against
[Meta's rate card](https://developers.facebook.com/docs/whatsapp/pricing) before budgeting.
Messenger/Instagram DM APIs are per-message free, if ever relevant. Customer financial data
over WhatsApp also needs a UK GDPR/DPA review.*
