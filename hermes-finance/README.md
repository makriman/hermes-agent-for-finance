# hermes-finance — wiring Cashew into the Hermes gateway

This is the thin layer that turns a stock
[hermes-agent](https://github.com/NousResearch/hermes-agent) into the finance assistant.

## Important: the WhatsApp Cloud adapter is upstream now

Earlier versions of this project shipped a **custom** `whatsapp_cloud.py` adapter. It has
been **retired** — hermes-agent (**≥ v0.18**, pinned in `UPSTREAM_BASE_COMMIT.txt`) now
ships a native, more complete WhatsApp Cloud API platform *and* a native Telegram platform
out of the box (inbound media incl. voice/docs, native `/media` outbound attachments,
quoted replies with quoted-text resolution, Opus voice bubbles, interactive buttons/lists,
typing). There is nothing to patch or drop in — you just enable the platforms with env vars.

Our custom adapter reached "Telegram parity" for the core messaging flows, but upstream's is
a strict superset with cleaner architecture (a shared `WhatsAppBehaviorMixin`, the platform
registry). Adopting theirs and deleting ours is the right call; the retired code remains in
this repo's git history if you want to see it.

## What's actually in this layer

| Path | What it is |
|---|---|
| `../skills/cashew/SKILL.md` | The Hermes skill that drives the Cashew engine from chat (the real integration). |
| `config.yaml.example` | The finance-relevant additions to merge into `~/.hermes/config.yaml` (model, toolset, STT/TTS). |
| `env.example` | Env template — model, **native** Telegram + WhatsApp Cloud, STT, Langfuse. |
| `UPSTREAM_BASE_COMMIT.txt` | The hermes-agent commit this was validated against (v0.18.2). |

## Setup

```bash
# 1. install hermes-agent (>= v0.18) with the platform/voice extras
cd /path/to/hermes-agent
uv pip install -e ".[messaging,voice,edge-tts]"

# 2. drop in the finance skill + config
cp -r /path/to/this-repo/skills/cashew ~/.hermes/skills/
#    merge config.yaml.example into ~/.hermes/config.yaml
#    fill ~/.hermes/.env from env.example  (NEVER commit it)

# 3. run the gateway; enable whatsapp_cloud + telegram via the env vars above
hermes gateway run
```

Put a public HTTPS tunnel (e.g. `cloudflared`) in front of the WhatsApp webhook port and
point your Meta webhook at `https://<tunnel>/wa/webhook`.

## Runtime notes

- **Enablement is env-driven.** WhatsApp Cloud turns on when `WHATSAPP_CLOUD_ACCESS_TOKEN`
  + `WHATSAPP_CLOUD_PHONE_NUMBER_ID` are set; Telegram when `TELEGRAM_BOT_TOKEN` is set.
- **STT**: inbound voice notes transcribe via the gateway's STT path — `stt.provider: local`
  (faster-whisper, no key) or a `GROQ_API_KEY` for cloud.
- **Vision**: the Copilot CLI transport is text-only (can't see image pixels). Documents are
  read via local text extraction (no key). Configure a vision-capable provider for photos.
