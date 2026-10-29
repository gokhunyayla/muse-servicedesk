"""WhatsApp Cloud API channel: webhook in, Graph API out. Text, voice notes, photos and short
videos all land in the same handle_message flow. Outbound rows (reminders, approvals, incident
updates) are drained by the scheduler through send()."""
from __future__ import annotations

import logging
import time
import os

import httpx
from fastapi import APIRouter, HTTPException, Query, Request

from app.agents import handle_message
from app.db import Outbound, session
from app.vision import sample_frames
from app.voice import transcribe

logger = logging.getLogger(__name__)
router = APIRouter()

GRAPH = "https://graph.facebook.com/v21.0"
def _cfg() -> tuple[str, str, str]:
    """Console-managed (Settings → Channels) with .env fallback; read per call so changes apply at once."""
    from app import vault
    return (vault.get("WA_ACCESS_TOKEN"), vault.get("WA_PHONE_NUMBER_ID"), vault.get("WA_VERIFY_TOKEN", "muse-servicedesk"))


def _headers() -> dict:
    return {"Authorization": f"Bearer {_cfg()[0]}"}


PERMANENT = {131030, 131026, 131021, 100}   # not in test allow-list, not a WhatsApp user, same number, bad param
OUTSIDE_WINDOW = {131047}                    # more than 24 h since the person last wrote: only a template may go
TEMPLATE = "it_desk_update"                  # one utility template carries any desk message outside the window
TEMPLATE_BODY = "IT Service Desk update: {{1}} Reply to this message if you need anything else."   # Meta: no variable at the end


class SendError(Exception):
    def __init__(self, code: int, message: str, permanent: bool):
        super().__init__(message)
        self.code, self.permanent = code, permanent


def _post(body: dict) -> None:
    _, phone_id, _ = _cfg()
    r = httpx.post(f"{GRAPH}/{phone_id}/messages", headers=_headers(), timeout=30, json={"messaging_product": "whatsapp", **body})
    if r.status_code >= 400:
        try:
            err = r.json().get("error", {})
        except ValueError:
            err = {}
        code = int(err.get("code", r.status_code))
        msg = err.get("message", r.text[:200])
        raise SendError(code, msg, permanent=code in PERMANENT)


def template_param(text: str) -> str:
    """Template variables may not hold line breaks, tabs or long runs of spaces, and stay under 1024 characters."""
    import re
    flat = re.sub(r"\s*\n+\s*", " · ", text.strip())
    flat = re.sub(r"[\t ]+", " ", flat)
    return flat[:1000]


def send(to: str, text: str) -> bool:
    """Deliver one text; outside the 24-hour window the same text goes inside the approved utility template.
    Raises SendError on a provider error (permanent ones should not be retried)."""
    token, phone_id, _ = _cfg()
    if not token or not phone_id:
        logger.warning("WhatsApp not configured; would send to %s: %s", to, text[:80])
        return False
    try:
        _post({"to": to.lstrip("+"), "type": "text", "text": {"body": text}})
    except SendError as e:
        if e.code not in OUTSIDE_WINDOW:
            raise
        _post({"to": to.lstrip("+"), "type": "template", "template": {
            "name": TEMPLATE, "language": {"code": "en"},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": template_param(text)}]}]}})
    return True


def connect(waba_id: str, phone: str = "") -> dict:
    """Wire the desk to a WhatsApp Business account with the saved token: subscribe this app to its webhooks,
    pick the phone number (the one given, else the only one), and make sure the update template exists."""
    import re
    token = _cfg()[0]
    if not token or not waba_id:
        return {"ok": False, "error": "save the access token and the WhatsApp Business account ID first"}
    def call(method, path, **kw):
        r = httpx.request(method, f"{GRAPH}/{path}", headers=_headers(), timeout=30, **kw)
        if r.status_code >= 400:
            try:
                e = r.json().get("error", {})
                why = " ".join(x for x in (e.get("message", ""), e.get("error_user_title", ""), e.get("error_user_msg", "")) if x)
            except ValueError:
                why = r.text[:200]
            raise RuntimeError(f"{method} {path.split('?')[0].split('/')[-1]}: {why}")
        return r.json() if r.content else {}
    try:
        call("POST", f"{waba_id}/subscribed_apps")
        numbers = call("GET", f"{waba_id}/phone_numbers", params={"fields": "id,display_phone_number,verified_name,quality_rating"}).get("data", [])
        digits = re.sub(r"\D", "", phone)
        pick = [n for n in numbers if digits and re.sub(r"\D", "", n["display_phone_number"]).endswith(digits[-9:])] or (numbers if len(numbers) == 1 else [])
        have = call("GET", f"{waba_id}/message_templates", params={"name": TEMPLATE, "fields": "name,status,language"}).get("data", [])
        if not any(t.get("name") == TEMPLATE for t in have):
            call("POST", f"{waba_id}/message_templates", json={
                "name": TEMPLATE, "category": "UTILITY", "language": "en",
                "components": [{"type": "BODY", "text": TEMPLATE_BODY,
                                "example": {"body_text": [["Ticket #102: your VPN is connected again; reply here if anything is still wrong."]]}}]})
            have = call("GET", f"{waba_id}/message_templates", params={"name": TEMPLATE, "fields": "name,status,language"}).get("data", [])
    except (RuntimeError, httpx.HTTPError) as e:
        return {"ok": False, "error": str(e)[:300]}
    status = next((t.get("status") for t in have if t.get("name") == TEMPLATE), "missing")
    return {"ok": bool(pick), "numbers": numbers, "phone_number_id": pick[0]["id"] if pick else "",
            "template": status, "error": "" if pick else "choose which number to use"}


def try_send(to: str, text: str) -> bool:
    """Reply path: log and swallow errors (the ticket keeps the text; the employee can re-ask)."""
    try:
        return send(to, text)
    except SendError as e:
        logger.error("WhatsApp send to %s failed (%s): %s", to, e.code, e)
        return False


def download_media(media_id: str) -> tuple[bytes, str]:
    meta = httpx.get(f"{GRAPH}/{media_id}", headers=_headers(), timeout=30).json()
    data = httpx.get(meta["url"], headers=_headers(), timeout=60)
    return data.content, meta.get("mime_type", "application/octet-stream")


def drain_outbound() -> int:
    """Deliver queued messages on every channel (WhatsApp, e-mail; the web chat polls its own)."""
    from app.channels import drain
    return drain()


STALE_SEC = 3600
_SEEN: list = []


def _remember(mid: str | None) -> None:
    if mid:
        _SEEN.append(mid)
        if len(_SEEN) > 2000:
            del _SEEN[:1000]


@router.get("/webhook")
def verify(hub_mode: str = Query("", alias="hub.mode"), hub_token: str = Query("", alias="hub.verify_token"),
           hub_challenge: str = Query("", alias="hub.challenge")):
    if hub_mode == "subscribe" and hub_token == _cfg()[2]:
        return int(hub_challenge) if hub_challenge.isdigit() else hub_challenge
    raise HTTPException(403, "verification failed")


def _extract(payload: dict) -> list[dict]:
    out = []
    for entry in payload.get("entry", []):
        for ch in entry.get("changes", []):
            for m in ch.get("value", {}).get("messages", []):
                out.append(m)
    return out


@router.post("/webhook")
async def inbound(request: Request) -> dict:
    payload = await request.json()
    # the agent loop blocks (model calls, waiting for a device): never on the event loop, or every other
    # request (the endpoint agent's poll, the console) stalls behind it
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(_handle_payload, payload)


def _handle_payload(payload: dict) -> dict:
    handled = 0
    for m in _extract(payload):
        mid, ts = m.get("id"), int(m.get("timestamp") or 0)
        if mid and mid in _SEEN:                       # Meta redelivers while it retries: never answer twice
            continue
        _remember(mid)
        if ts and time.time() - ts > STALE_SEC:        # queued while we were unreachable: too old to act on now
            logger.warning("skipping stale WhatsApp message %s from %s (%d min old)", mid, m.get("from"), (time.time() - ts) // 60)
            continue
        frm = "+" + m["from"].lstrip("+")
        kind = m.get("type")
        text, image, mime, extra, voice = "", None, "image/jpeg", None, False
        if kind == "text":
            text = m["text"]["body"]
        elif kind == "audio":
            data, amime = download_media(m["audio"]["id"])
            text = transcribe(data, amime)
            voice = True
            if not text:
                from app.i18n import t as tr_
                try_send(frm, tr_("voice_failed", "tr"))
                continue
        elif kind == "image":
            image, mime = download_media(m["image"]["id"])
            text = m["image"].get("caption", "") or "(fotoğraf gönderdi)"
        elif kind == "document":
            from app.attachments import with_document
            data, dmime = download_media(m["document"]["id"])
            text = with_document(m["document"].get("caption", ""), m["document"].get("filename", "document"), dmime, data)
        elif kind == "video":
            data, _ = download_media(m["video"]["id"])
            frames = sample_frames(data)
            if frames:
                image, extra = frames[0], frames[1:]
            text = (m["video"].get("caption", "") or "(video gönderdi)") + f" [video: {len(frames)} frames]"
        else:
            continue
        with session() as db:
            _, answer = handle_message(db, frm, text, image, mime, extra_images=extra, channel="whatsapp", voice=voice)
        try_send(frm, answer)
        handled += 1
    return {"handled": handled}
