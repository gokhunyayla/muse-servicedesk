"""WhatsApp Cloud API channel: webhook in, Graph API out. Text, voice notes, photos and short
videos all land in the same handle_message flow. Outbound rows (reminders, approvals, incident
updates) are drained by the scheduler through send()."""
from __future__ import annotations

import logging
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
VERIFY_TOKEN = os.getenv("WA_VERIFY_TOKEN", "muse-servicedesk")
ACCESS_TOKEN = os.getenv("WA_ACCESS_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("WA_PHONE_NUMBER_ID", "")


def _headers() -> dict:
    return {"Authorization": f"Bearer {ACCESS_TOKEN}"}


def send(to: str, text: str) -> bool:
    if not ACCESS_TOKEN or not PHONE_NUMBER_ID:
        logger.warning("WhatsApp not configured; would send to %s: %s", to, text[:80])
        return False
    r = httpx.post(f"{GRAPH}/{PHONE_NUMBER_ID}/messages", headers=_headers(), timeout=30,
                   json={"messaging_product": "whatsapp", "to": to.lstrip("+"), "type": "text", "text": {"body": text}})
    if r.status_code >= 400:
        logger.error("WhatsApp send failed %s: %s", r.status_code, r.text[:200])
        return False
    return True


def download_media(media_id: str) -> tuple[bytes, str]:
    meta = httpx.get(f"{GRAPH}/{media_id}", headers=_headers(), timeout=30).json()
    data = httpx.get(meta["url"], headers=_headers(), timeout=60)
    return data.content, meta.get("mime_type", "application/octet-stream")


def drain_outbound() -> int:
    sent = 0
    with session() as db:
        for o in db.query(Outbound).filter(Outbound.sent.is_(False)).limit(50):
            if send(o.to, o.text):
                o.sent = True
                sent += 1
    return sent


@router.get("/webhook")
def verify(hub_mode: str = Query("", alias="hub.mode"), hub_token: str = Query("", alias="hub.verify_token"),
           hub_challenge: str = Query("", alias="hub.challenge")):
    if hub_mode == "subscribe" and hub_token == VERIFY_TOKEN:
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
    handled = 0
    for m in _extract(payload):
        frm = "+" + m["from"].lstrip("+")
        kind = m.get("type")
        text, image, mime, extra = "", None, "image/jpeg", None
        if kind == "text":
            text = m["text"]["body"]
        elif kind == "audio":
            data, amime = download_media(m["audio"]["id"])
            text = transcribe(data, amime)
            if not text:
                send(frm, "Sesli mesajı anlayamadım, yazar mısınız?")
                continue
        elif kind == "image":
            image, mime = download_media(m["image"]["id"])
            text = m["image"].get("caption", "") or "(fotoğraf gönderdi)"
        elif kind == "video":
            data, _ = download_media(m["video"]["id"])
            frames = sample_frames(data)
            if frames:
                image, extra = frames[0], frames[1:]
            text = (m["video"].get("caption", "") or "(video gönderdi)") + f" [video: {len(frames)} frames]"
        else:
            continue
        with session() as db:
            _, answer = handle_message(db, frm, text, image, mime, extra_images=extra)
        send(frm, answer)
        handled += 1
    return {"handled": handled}
