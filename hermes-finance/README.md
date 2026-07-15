# hermes-finance — the custom Hermes gateway layer

This directory holds everything that turns a stock
[hermes-agent](https://github.com/NousResearch/hermes-agent) into the finance assistant:
a **native WhatsApp Cloud API platform**, five small patches that wire it in, and the
config/env templates.

## Contents

| Path | What it is |
|---|---|
| `platforms/whatsapp_cloud.py` | **New** Hermes gateway platform — the native WhatsApp Cloud adapter (inbound media, outbound `/media` uploads, quoted replies, typing, anti-misroute guard). Copied to `gateway/platforms/`. |
| `patches/agent__prompt_builder.py.patch` | Adds the `whatsapp_cloud` platform hint (concise, reply-on-channel, never deflect to another platform). |
| `patches/gateway__config.py.patch` | `Platform.WHATSAPP_CLOUD` enum + connected-check + env-enable block. |
| `patches/gateway__run.py.patch` | Adapter factory branch, auth maps, home-channel-nag suppression. |
| `patches/tools__send_message_tool.py.patch` | WhatsApp media delivery + the **anti-misroute guard** (a live reply can't leak to another platform). |
| `patches/tools__tts_tool.py.patch` | Emit Opus (`.ogg`) for WhatsApp so TTS lands as a voice bubble. |
| `config.yaml.example` | The finance-relevant additions to merge into `~/.hermes/config.yaml`. |
| `env.example` | WhatsApp / Telegram / STT / Langfuse variables for `~/.hermes/.env`. |
| `UPSTREAM_BASE_COMMIT.txt` | The hermes-agent commit these patches apply cleanly to. |

## Apply

```bash
# 1. check out hermes-agent at the pinned base commit
cd /path/to/hermes-agent
git checkout $(cat /path/to/this-repo/hermes-finance/UPSTREAM_BASE_COMMIT.txt)

# 2. drop in the adapter + apply the patches
/path/to/this-repo/hermes-finance/apply.sh /path/to/hermes-agent

# 3. config + secrets
#    merge config.yaml.example into ~/.hermes/config.yaml
#    fill ~/.hermes/.env from env.example   (NEVER commit it)
#    copy the skill:  cp -r ../skills/cashew ~/.hermes/skills/
```

`apply.sh` is a thin wrapper around `git apply`; if upstream has moved and a patch no longer
applies cleanly, apply the hunks by hand — each patch is tiny (a few dozen lines).

## Runtime notes

- The adapter listens on a **local** port (default `127.0.0.1:8085`, path `/wa/webhook`).
  Put a public HTTPS tunnel in front (this build used `cloudflared`) and point the Meta
  webhook at `https://<tunnel>/wa/webhook`.
- **STT**: inbound voice notes are transcribed by the gateway's existing STT path. Set
  `stt.provider: local` (faster-whisper, no key) or a `GROQ_API_KEY` for cloud STT.
- **Vision**: the model used here (Claude Sonnet via the GitHub Copilot CLI transport) is
  **text-only** — it can't see image pixels. Documents are read via local text extraction
  (no key). To have the assistant *understand* photos, configure a vision-capable provider.
- **Voice replies** are user-requested only (`voice: auto_tts: false`); they render as Opus
  voice bubbles on both WhatsApp and Telegram.
