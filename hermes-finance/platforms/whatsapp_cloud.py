"""WhatsApp Cloud API — native gateway platform adapter.

Unlike the Baileys ``Platform.WHATSAPP`` bridge (whatsapp.py), this speaks the
official Meta Cloud API and — crucially — dispatches inbound messages straight
into ``BasePlatformAdapter.handle_message`` → ``GatewayRunner._handle_message``,
i.e. the SAME warm-agent path Telegram uses (cached agent, warm prompt cache,
persistent per-user session, streaming). That is what makes it fast; an
external bridge or the api_server both rebuild the agent per message and run
~2x slower.

Inbound:  Meta webhook — GET (hub.challenge verify) + POST (messages), body
          HMAC-verified with the app secret (X-Hub-Signature-256). Text, voice
          notes/audio, images, stickers, video, documents, location, contacts
          and reactions are all accepted; media is downloaded into the shared
          caches so the gateway's existing STT / vision / document-extraction
          paths pick it up (parity with Telegram's _build_message_event).
Outbound: POST graph.facebook.com/<v>/<phone_number_id>/messages (Bearer token).
          Files (e.g. the Excel export) are uploaded via /media then sent by id,
          so send_image/send_document/send_video/send_voice deliver real
          attachments instead of the base class's plain-text path fallback.
Typing:   POST /messages status=read + typing_indicator (WhatsApp's live bubble).

Runs its own aiohttp listener on a LOCAL port; put cloudflared/caddy in front
for the public HTTPS URL Meta requires.

Env: WA_ACCESS_TOKEN WA_PHONE_NUMBER_ID WA_APP_SECRET WA_VERIFY_TOKEN
     WA_GRAPH_VERSION WHATSAPP_CLOUD_HOST WHATSAPP_CLOUD_PORT
     WHATSAPP_CLOUD_ALLOWED_USERS WHATSAPP_CLOUD_ALLOW_ALL_USERS
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import mimetypes
import os
import re
from pathlib import Path


def _to_whatsapp_markdown(text: str) -> str:
    """Normalise the agent's (Telegram/GitHub-flavoured) markdown to WhatsApp's
    syntax: *bold*, _italic_, ~strike~, ```code```. Converts **bold**→*bold*,
    ATX headers → *bold*, [text](url) → text (url); protects code spans."""
    if not text:
        return text
    fences: list[str] = []
    codes: list[str] = []
    text = re.sub(r"```[\s\S]*?```",
                  lambda m: fences.append(m.group(0)) or f"\x00F{len(fences)-1}\x00", text)
    text = re.sub(r"`[^`\n]+`",
                  lambda m: codes.append(m.group(0)) or f"\x00C{len(codes)-1}\x00", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)     # **bold** -> *bold*
    text = re.sub(r"__(.+?)__", r"*\1*", text)          # __bold__ -> *bold*
    text = re.sub(r"~~(.+?)~~", r"~\1~", text)          # ~~strike~~ -> ~strike~
    text = re.sub(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*$", r"*\1*", text, flags=re.MULTILINE)  # headers
    text = re.sub(r"^(\s*)[-*]\s+", r"\1• ", text, flags=re.MULTILINE)  # bullets -> •
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)         # links
    for i, f in enumerate(fences):
        text = text.replace(f"\x00F{i}\x00", f)
    for i, c in enumerate(codes):
        text = text.replace(f"\x00C{i}\x00", c)
    return text

try:
    from aiohttp import web
    import httpx
    _WAC_OK = True
except Exception:  # pragma: no cover
    _WAC_OK = False

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter, MessageEvent, MessageType, SendResult,
    cache_audio_from_bytes, cache_image_from_bytes,
    cache_document_from_bytes, cache_video_from_bytes,
)

logger = logging.getLogger(__name__)

WEBHOOK_PATH = "/wa/webhook"          # matches the existing Meta config + tunnel


def check_whatsapp_cloud_requirements() -> bool:
    return _WAC_OK


class WhatsAppCloudAdapter(BasePlatformAdapter):
    MAX_MESSAGE_LENGTH = 4096          # WhatsApp text body cap

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WHATSAPP_CLOUD)
        x = config.extra or {}
        self._token = config.token or os.getenv("WA_ACCESS_TOKEN", "")
        self._phone_id = x.get("phone_number_id") or os.getenv("WA_PHONE_NUMBER_ID", "")
        self._app_secret = x.get("app_secret") or os.getenv("WA_APP_SECRET", "")
        self._verify_token = x.get("verify_token") or os.getenv("WA_VERIFY_TOKEN", "")
        self._graph = x.get("graph_version") or os.getenv("WA_GRAPH_VERSION", "v21.0")
        self._host = x.get("host") or os.getenv("WHATSAPP_CLOUD_HOST", "127.0.0.1")
        self._port = int(x.get("port") or os.getenv("WHATSAPP_CLOUD_PORT", "8085"))
        self._base = f"https://graph.facebook.com/{self._graph}/{self._phone_id}"
        self._graph_base = f"https://graph.facebook.com/{self._graph}"  # media lookups (by media_id)
        # Cap inbound/outbound media we buffer in memory — this box is RAM-tight
        # and WhatsApp's own limits (img 5MB, doc 100MB, video 16MB) are larger
        # than we want to hold. Overridable via env.
        self._max_media_bytes = int(
            os.getenv("WHATSAPP_CLOUD_MAX_MEDIA_MB", "25")) * 1024 * 1024
        self._runner: "web.AppRunner | None" = None
        self._http: "httpx.AsyncClient | None" = None
        self._last_inbound: dict[str, str] = {}   # chat_id -> last inbound msg id
        self._typing_sent: set[str] = set()        # msg ids we've already 'typed' for
        self._seen: dict[str, float] = {}          # dedup Meta retries

    # ---------------------------------------------------------------- required
    async def connect(self) -> bool:
        if not (self._token and self._phone_id and self._app_secret and self._verify_token):
            logger.error("[whatsapp_cloud] missing WA_ACCESS_TOKEN / WA_PHONE_NUMBER_ID "
                         "/ WA_APP_SECRET / WA_VERIFY_TOKEN")
            return False
        self._http = httpx.AsyncClient(timeout=30)
        app = web.Application()
        app.router.add_get("/wa/health", self._h_health)
        app.router.add_get(WEBHOOK_PATH, self._h_verify)
        app.router.add_post(WEBHOOK_PATH, self._h_inbound)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        try:
            await web.TCPSite(self._runner, self._host, self._port).start()
        except OSError as e:
            logger.error("[whatsapp_cloud] cannot bind %s:%d (%s) — is the old "
                         "bridge still on this port?", self._host, self._port, e)
            return False
        self._mark_connected()
        logger.info("[whatsapp_cloud] webhook listening on %s:%d%s",
                    self._host, self._port, WEBHOOK_PATH)
        return True

    async def disconnect(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        if self._http:
            await self._http.aclose()
            self._http = None
        self._mark_disconnected()

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        if not content or not content.strip():
            return SendResult(success=True)
        content = _to_whatsapp_markdown(content)     # WhatsApp-native formatting
        last_id = None
        first = True
        for chunk in self.truncate_message(content, self.MAX_MESSAGE_LENGTH):
            payload = {"messaging_product": "whatsapp", "to": chat_id,
                       "type": "text", "text": {"body": chunk, "preview_url": False}}
            # Quote the message we're replying to — WhatsApp's highlight/quoted
            # reply (parity with Telegram). Only the first chunk carries the
            # quote so a multi-message answer doesn't repeat the citation.
            if first and reply_to:
                payload["context"] = {"message_id": reply_to}
            first = False
            try:
                r = await self._http.post(
                    f"{self._base}/messages",
                    headers={"Authorization": f"Bearer {self._token}"}, json=payload)
            except Exception as e:
                logger.exception("[whatsapp_cloud] send error")
                return SendResult(success=False, error=str(e), retryable=True)
            if r.status_code >= 400:
                retryable = r.status_code in (429, 500, 502, 503, 504)
                logger.error("[whatsapp_cloud] send %s: %s", r.status_code, r.text[:300])
                return SendResult(success=False, error=r.text[:300], retryable=retryable)
            last_id = (r.json().get("messages") or [{}])[0].get("id")
        return SendResult(success=True, message_id=last_id)

    async def get_chat_info(self, chat_id) -> dict:
        return {"name": chat_id, "type": "dm", "chat_id": chat_id}

    # ---------------------------------------------------------------- typing
    async def send_typing(self, chat_id, metadata=None) -> None:
        """WhatsApp 'typing…' bubble — mark the last inbound read + typing.
        Fired once per inbound message (the API shows it for ~25s / until we
        reply), so we don't spam it on the base's 2s refresh loop."""
        mid = self._last_inbound.get(chat_id)
        if not (self._http and mid) or mid in self._typing_sent:
            return
        self._typing_sent.add(mid)
        try:
            await self._http.post(
                f"{self._base}/messages",
                headers={"Authorization": f"Bearer {self._token}"},
                json={"messaging_product": "whatsapp", "status": "read",
                      "message_id": mid, "typing_indicator": {"type": "text"}})
        except Exception:
            logger.debug("[whatsapp_cloud] typing failed", exc_info=True)

    # ------------------------------------------------------------ media (in)
    async def _download_media(self, media_id: str, kind: str,
                              mime: str = "", filename: str = ""):
        """Resolve a WhatsApp media id → URL, download the bytes (the lookaside
        CDN requires the Bearer token) and cache them into the shared media
        caches so the gateway's STT / vision / document-extraction paths pick
        them up. ``kind`` ∈ {audio, image, video, document}. Returns a local
        path or None. Mirrors Telegram's inbound caching."""
        if not (self._http and media_id):
            return None
        auth = {"Authorization": f"Bearer {self._token}"}
        try:
            meta = await self._http.get(f"{self._graph_base}/{media_id}", headers=auth)
            if meta.status_code >= 400:
                logger.error("[whatsapp_cloud] media lookup %s: %s",
                             meta.status_code, meta.text[:200])
                return None
            info = meta.json()
            url = info.get("url")
            if not url:
                return None
            mime = (mime or info.get("mime_type", "")).split(";")[0].strip()
            size = int(info.get("file_size") or 0)
            if size and size > self._max_media_bytes:
                logger.warning("[whatsapp_cloud] %s too large (%d bytes) — skipping",
                               kind, size)
                return None
            data = await self._http.get(url, headers=auth)
            if data.status_code >= 400:
                logger.error("[whatsapp_cloud] media download %s", data.status_code)
                return None
            content = data.content
            if kind == "audio":
                return cache_audio_from_bytes(content, ext=".ogg")
            if kind == "image":
                ext = mimetypes.guess_extension(mime) or ".jpg"
                if ext == ".jpe":
                    ext = ".jpg"
                return cache_image_from_bytes(content, ext=ext)
            if kind == "video":
                ext = mimetypes.guess_extension(mime) or ".mp4"
                return cache_video_from_bytes(content, ext=ext)
            # document
            if not filename:
                filename = "document" + (mimetypes.guess_extension(mime) or ".bin")
            return cache_document_from_bytes(content, filename)
        except Exception:
            logger.exception("[whatsapp_cloud] media download failed")
            return None

    # ----------------------------------------------------------- media (out)
    @staticmethod
    def _guess_mime(path: str, default: str) -> str:
        mime, _ = mimetypes.guess_type(str(path))
        return mime or default

    async def _upload_media(self, path: str, mime: str):
        """Upload a local file to WhatsApp; returns a media id or None."""
        if not (self._http and path and os.path.exists(path)):
            return None
        try:
            with open(path, "rb") as f:
                r = await self._http.post(
                    f"{self._base}/media",
                    headers={"Authorization": f"Bearer {self._token}"},
                    data={"messaging_product": "whatsapp", "type": mime},
                    files={"file": (os.path.basename(path), f, mime)})
            if r.status_code >= 400:
                logger.error("[whatsapp_cloud] media upload %s: %s",
                             r.status_code, r.text[:300])
                return None
            return r.json().get("id")
        except Exception:
            logger.exception("[whatsapp_cloud] media upload failed")
            return None

    _MIME_DEFAULT = {"image": "image/jpeg", "document": "application/octet-stream",
                     "video": "video/mp4", "audio": "audio/ogg"}

    async def _send_media(self, chat_id, kind, source, caption=None,
                          filename=None, reply_to=None) -> SendResult:
        """Deliver an image/document/video/audio to WhatsApp. ``source`` is a
        local path (uploaded via /media then sent by id) or an http(s) URL
        (sent by link). Falls back to a text notice if the upload/send fails,
        so a generated file never vanishes silently."""
        if not self._http or not source:
            return SendResult(success=False, error="no media source")
        source = str(source)
        is_url = source.lower().startswith(("http://", "https://"))
        media_obj: dict = {}
        if is_url:
            media_obj["link"] = source
        else:
            mime = self._guess_mime(source, self._MIME_DEFAULT[kind])
            media_id = await self._upload_media(source, mime)
            if not media_id:
                note = (f"{caption}\n" if caption else "") + \
                       f"📎 (I made {filename or os.path.basename(source)} but couldn't attach it)"
                return await self.send(chat_id, note, reply_to=reply_to)
            media_obj["id"] = media_id
        if kind == "document":
            media_obj["filename"] = filename or os.path.basename(source)
        if caption and kind in ("image", "document", "video"):
            media_obj["caption"] = _to_whatsapp_markdown(caption)
        payload = {"messaging_product": "whatsapp", "to": chat_id,
                   "type": kind, kind: media_obj}
        if reply_to:
            payload["context"] = {"message_id": reply_to}
        try:
            r = await self._http.post(
                f"{self._base}/messages",
                headers={"Authorization": f"Bearer {self._token}"}, json=payload)
        except Exception as e:
            logger.exception("[whatsapp_cloud] media send error")
            return SendResult(success=False, error=str(e), retryable=True)
        if r.status_code >= 400:
            logger.error("[whatsapp_cloud] media send %s: %s", r.status_code, r.text[:300])
            if caption:                       # at least deliver the words
                await self.send(chat_id, caption, reply_to=reply_to)
            return SendResult(success=False, error=r.text[:300],
                              retryable=r.status_code in (429, 500, 502, 503, 504))
        mid = (r.json().get("messages") or [{}])[0].get("id")
        return SendResult(success=True, message_id=mid)

    async def send_image(self, chat_id, image_url, caption=None,
                         reply_to=None, metadata=None) -> SendResult:
        return await self._send_media(chat_id, "image", image_url,
                                      caption=caption, reply_to=reply_to)

    async def send_image_file(self, chat_id, image_path, caption=None,
                              reply_to=None, metadata=None, **kwargs) -> SendResult:
        return await self._send_media(chat_id, "image", image_path,
                                      caption=caption, reply_to=reply_to)

    async def send_document(self, chat_id, file_path, caption=None, file_name=None,
                            reply_to=None, metadata=None, **kwargs) -> SendResult:
        return await self._send_media(chat_id, "document", file_path,
                                      caption=caption, filename=file_name,
                                      reply_to=reply_to)

    async def send_video(self, chat_id, video_path, caption=None,
                         reply_to=None, metadata=None, **kwargs) -> SendResult:
        return await self._send_media(chat_id, "video", video_path,
                                      caption=caption, reply_to=reply_to)

    async def send_voice(self, chat_id, audio_path, caption=None,
                         reply_to=None, metadata=None, **kwargs) -> SendResult:
        res = await self._send_media(chat_id, "audio", audio_path, reply_to=reply_to)
        if caption:                # WhatsApp audio has no caption — send it separately
            await self.send(chat_id, caption, reply_to=reply_to)
        return res

    async def send_animation(self, chat_id, animation_url, caption=None,
                             reply_to=None, metadata=None) -> SendResult:
        # WhatsApp has no GIF type; deliver .mp4 as video, otherwise as image.
        kind = "video" if str(animation_url).lower().split("?")[0].endswith(".mp4") else "image"
        return await self._send_media(chat_id, kind, animation_url,
                                      caption=caption, reply_to=reply_to)

    # ---------------------------------------------------------------- webhook
    async def _h_health(self, request):
        return web.json_response(
            {"status": "ok", "platform": "whatsapp_cloud",
             "configured": bool(self._token and self._phone_id and self._app_secret)})

    async def _h_verify(self, request):
        q = request.query
        if q.get("hub.mode") == "subscribe" and \
                q.get("hub.verify_token") == self._verify_token:
            return web.Response(text=q.get("hub.challenge", ""))
        return web.Response(status=403, text="forbidden")

    async def _h_inbound(self, request):
        raw = await request.read()
        sig = request.headers.get("X-Hub-Signature-256", "")
        want = "sha256=" + hmac.new(self._app_secret.encode(), raw,
                                    hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, want):
            logger.warning("[whatsapp_cloud] bad signature")
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return web.json_response({"ok": True})

        import time
        now = time.time()
        self._seen = {k: t for k, t in self._seen.items() if now - t < 3600}

        for entry in data.get("entry", []):
            for ch in entry.get("changes", []):
                val = ch.get("value", {})
                profile_name = None
                contacts = val.get("contacts") or []
                if contacts:
                    profile_name = (contacts[0].get("profile") or {}).get("name")
                for m in val.get("messages", []) or []:
                    mid = m.get("id", "")
                    if not mid or mid in self._seen:
                        continue
                    self._seen[mid] = now
                    wa_from = m.get("from", "")
                    # Build the event (incl. any media download) in the background
                    # so we return 200 to Meta fast — a slow webhook triggers retries.
                    task = asyncio.create_task(
                        self._process_message(m, wa_from, profile_name, mid))
                    self._background_tasks.add(task)
                    task.add_done_callback(self._background_tasks.discard)
        return web.json_response({"status": "received"})

    async def _process_message(self, m, wa_from, profile_name, mid) -> None:
        try:
            event = await self._build_inbound_event(m, wa_from, profile_name, mid)
            if event is not None:
                await self.handle_message(event)          # WARM PATH
        except Exception:
            logger.exception("[whatsapp_cloud] message processing failed")

    async def _build_inbound_event(self, m, wa_from, profile_name, mid):
        """Turn one inbound WhatsApp message into a MessageEvent (or None to
        skip). Handles every media type WhatsApp delivers, downloading media
        into the shared caches so the gateway's vision / STT / document paths
        pick it up. Mirrors Telegram's _build_message_event."""
        mtype = m.get("type")
        text = self._extract_text(m)     # text/button/interactive → str; else None
        msg_type = MessageType.TEXT
        media_urls: list[str] = []
        media_types: list[str] = []
        try:
            if mtype in ("audio", "voice"):
                blk = m.get(mtype) or {}
                p = await self._download_media(blk.get("id", ""), "audio")
                if not p:
                    return await self._nack(wa_from, "voice note")
                media_urls, media_types, msg_type = [p], ["audio/ogg"], MessageType.VOICE
                text = text or ""
            elif mtype in ("image", "sticker"):
                blk = m.get(mtype) or {}
                mime = (blk.get("mime_type") or "image/jpeg").split(";")[0].strip()
                p = await self._download_media(blk.get("id", ""), "image", mime)
                if not p:
                    return await self._nack(wa_from, "image")
                media_urls, media_types, msg_type = [p], [mime], MessageType.PHOTO
                text = blk.get("caption") or text or ""
            elif mtype == "document":
                blk = m.get("document") or {}
                mime = (blk.get("mime_type") or "application/octet-stream").split(";")[0].strip()
                fname = blk.get("filename") or ("document" + (mimetypes.guess_extension(mime) or ""))
                p = await self._download_media(blk.get("id", ""), "document", mime, fname)
                if not p:
                    return await self._nack(wa_from, "file")
                media_urls, media_types, msg_type = [p], [mime], MessageType.DOCUMENT
                text = blk.get("caption") or text or ""
            elif mtype == "video":
                blk = m.get("video") or {}
                mime = (blk.get("mime_type") or "video/mp4").split(";")[0].strip()
                p = await self._download_media(blk.get("id", ""), "video", mime)
                if not p:
                    return await self._nack(wa_from, "video")
                media_urls, media_types, msg_type = [p], [mime], MessageType.VIDEO
                text = blk.get("caption") or text or ""
            elif mtype == "location":
                text = self._format_location(m.get("location") or {})
            elif mtype == "contacts":
                text = self._format_contacts(m.get("contacts") or [])
            elif mtype == "reaction":
                emoji = (m.get("reaction") or {}).get("emoji") or ""
                text = f"[The user reacted {emoji} to an earlier message.]" if emoji else ""
            elif text is None:
                await self.send(
                    wa_from, "I can read text, voice notes, images, PDFs and "
                             "spreadsheets — send any of those.")
                return None
        except Exception:
            logger.exception("[whatsapp_cloud] inbound media handling failed")
            await self.send(wa_from, "⚠ Something went wrong reading that — mind trying text?")
            return None

        if msg_type == MessageType.TEXT and not (text or "").strip():
            return None

        self._last_inbound[wa_from] = mid
        source = self.build_source(
            chat_id=wa_from, chat_type="dm",
            user_id=wa_from, user_name=profile_name, message_id=mid)
        event = MessageEvent(text=(text or "").strip(), message_type=msg_type,
                             source=source, raw_message=m, message_id=mid,
                             media_urls=media_urls, media_types=media_types)
        # Inbound quoted reply: WhatsApp gives the quoted message id (not its text).
        reply_to_id = (m.get("context") or {}).get("id")
        if reply_to_id:
            event.reply_to_message_id = str(reply_to_id)
        logger.info("[whatsapp_cloud] inbound %s from %s: %r",
                    mtype, wa_from, (text or "")[:80])
        return event

    async def _nack(self, chat_id, what):
        await self.send(chat_id, f"⚠ I couldn't fetch that {what} — mind resending or typing it?")
        return None

    @staticmethod
    def _format_location(loc: dict) -> str:
        lat, lng = loc.get("latitude"), loc.get("longitude")
        if lat is None or lng is None:
            return ""
        where = ""
        if loc.get("name"):
            where = f" ({loc['name']}" + (f", {loc['address']}" if loc.get("address") else "") + ")"
        return (f"[The user shared a location: {lat}, {lng}{where} — "
                f"https://maps.google.com/?q={lat},{lng}]")

    @staticmethod
    def _format_contacts(contacts: list) -> str:
        names = []
        for c in contacts or []:
            nm = (c.get("name") or {}).get("formatted_name")
            phones = ", ".join(p.get("phone", "") for p in (c.get("phones") or []) if p.get("phone"))
            names.append(f"{nm or 'contact'}" + (f" ({phones})" if phones else ""))
        return "[The user shared a contact card: " + "; ".join(names) + "]" if names else ""

    @staticmethod
    def _extract_text(m: dict):
        t = m.get("type")
        if t == "text":
            return m.get("text", {}).get("body", "")
        if t == "button":
            return m.get("button", {}).get("text", "")
        if t == "interactive":
            inter = m.get("interactive", {})
            return ((inter.get("button_reply") or inter.get("list_reply") or {})
                    .get("title", ""))
        return None
