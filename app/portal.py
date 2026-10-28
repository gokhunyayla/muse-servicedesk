"""Employee web chat: a public page outside the console where an employee raises and follows requests
the same way they would over WhatsApp. Same agents, same tickets; the two channels share the
employee's tickets because both use the phone number as the requester."""
from __future__ import annotations

import json
import re

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.agents import handle_message
from app.channels import portal_config
from app.db import Event, Outbound, Ticket, session
from app.security import cookie_secure, rate_limit

router = APIRouter()
COOKIE = "muse_portal"
PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()-]{6,19}$")


def _ident(request: Request) -> dict | None:
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    from app import vault
    try:
        return json.loads(vault._fernet().decrypt(raw.encode()).decode())
    except Exception:  # noqa: BLE001
        return None


def _require(request: Request) -> dict:
    who = _ident(request)
    if not who or not portal_config()["enabled"]:
        raise HTTPException(401)
    return who


def _esc(x: str) -> str:
    return (x or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


@router.get("/portal", response_class=HTMLResponse)
def portal_page(request: Request) -> str:
    cfg = portal_config()
    if not cfg["enabled"]:
        return _shell(cfg, '<p class="lead">The web chat is switched off. Please write to the service desk over WhatsApp or e-mail.</p>')
    who = _ident(request)
    if who is None:
        return _shell(cfg, f"""
<p class="lead">Tell us who you are, then write your request as you would on WhatsApp.</p>
<form method="post" action="/portal/start">
<label for="name">Your name</label><input id="name" name="name" required autofocus maxlength="80">
<label for="phone">Mobile number <small>(the one IT knows you by)</small></label><input id="phone" name="phone" required inputmode="tel" placeholder="+90 5xx xxx xx xx">
<button class="btn primary" type="submit">Start chat</button></form>
{('<p class="muted" style="margin-top:14px">' + _esc(cfg["note"]) + '</p>') if cfg.get("note") else ''}""")
    return _shell(cfg, CHAT.replace("{NAME}", _esc(who["name"])), chat=True)


@router.post("/portal/start")
def portal_start(request: Request, name: str = Form(...), phone: str = Form(...)):
    rate_limit(request, "portal-start", 20, 600)
    if not portal_config()["enabled"]:
        raise HTTPException(403)
    digits = re.sub(r"[^0-9+]", "", phone)
    if not PHONE_RE.match(digits):
        return RedirectResponse("/portal?error=phone", 303)
    if not digits.startswith("+"):                      # local formats: 05xx…, 5xx… → +<country>5xx…
        cc = str(portal_config().get("country", "+90"))
        digits = cc + digits[1:] if digits.startswith("0") and len(digits) == 11 else cc + digits if len(digits) == 10 else "+" + digits
    from app import vault
    token = vault._fernet().encrypt(json.dumps({"name": name.strip()[:80], "phone": digits}).encode()).decode()
    r = RedirectResponse("/portal", 303)
    r.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=cookie_secure())   # session cookie: gone when the browser closes
    return r


@router.post("/portal/leave")
def portal_leave():
    r = RedirectResponse("/portal", 303)
    r.delete_cookie(COOKIE)
    return r


@router.get("/portal/history")
def portal_history(request: Request) -> list[dict]:
    """Everything said on this employee's tickets, oldest first, plus the open ticket list."""
    who = _require(request)
    with session() as db:
        tickets = db.query(Ticket).filter(Ticket.requester == who["phone"]).order_by(Ticket.id.desc()).limit(10).all()
        ids = [t.id for t in tickets]
        rows = (db.query(Event).filter(Event.ticket_id.in_(ids), Event.kind == "message").order_by(Event.id).all()
                if ids else [])
        # queued desk messages that have not been picked up yet count as delivered now
        pending = db.query(Outbound).filter(Outbound.to == who["phone"], Outbound.sent.is_(False), Outbound.error == "").all()
        out = [{"ticket": e.ticket_id, "who": "you" if e.actor == "user" else "desk", "text": e.content,
                "at": e.at.isoformat(timespec="minutes")} for e in rows]
        for o in pending:
            t = db.get(Ticket, o.ticket_id) if o.ticket_id else None
            if t is None or t.channel == "web":
                out.append({"ticket": o.ticket_id, "who": "desk", "text": o.text, "at": o.at.isoformat(timespec="minutes")})
                o.sent = True
        return out


@router.get("/portal/updates")
def portal_updates(request: Request) -> list[dict]:
    """Messages the desk sent on its own since the last poll (reminders, approvals, replies from people)."""
    who = _require(request)
    out = []
    with session() as db:
        for o in db.query(Outbound).filter(Outbound.to == who["phone"], Outbound.sent.is_(False), Outbound.error == "").order_by(Outbound.id):
            t = db.get(Ticket, o.ticket_id) if o.ticket_id else None
            if t is not None and t.channel != "web":
                continue
            o.sent = True
            out.append({"ticket": o.ticket_id, "who": "desk", "text": o.text, "at": o.at.isoformat(timespec="minutes")})
    return out


@router.post("/portal/say")
async def portal_say(request: Request, text: str = Form(""), image: UploadFile | None = File(None)):
    who = _require(request)
    rate_limit(request, "portal-say", 60, 300)
    data, mime = None, "image/jpeg"
    if image is not None and image.filename:
        data = await image.read()
        mime = image.content_type or mime
        if not mime.startswith("image/") or len(data) > 8_000_000:
            return JSONResponse({"error": "only images up to 8 MB"}, 422)
    text = text.strip()
    if not text and data is None:
        return JSONResponse({"error": "empty"}, 422)
    with session() as db:
        t, answer = handle_message(db, who["phone"], text or "(photo)", data, mime, channel="web")
        return {"ticket": t.id if t else None, "status": t.status if t else None, "text": answer}


def _shell(cfg: dict, body: str, chat: bool = False) -> str:
    from app.auth import CSS, LOGO
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{_esc(cfg["title"])}</title>'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><style>{CSS}{PORTAL_CSS}</style></head>'
            f'<body class="auth{" chat" if chat else ""}"><div class="card"><div class="brand"><div class="logo">{LOGO}</div>'
            f'<h2>{_esc(cfg["title"])}</h2></div>{body}</div></body></html>')


PORTAL_CSS = """
body.chat{align-items:stretch}.chat .card{width:min(680px,100%);display:flex;flex-direction:column;height:min(860px,calc(100vh - 48px))}
.chat .brand{flex-direction:row;align-items:center;justify-content:space-between;margin-bottom:10px}.chat .brand h2{font-size:18px}
.who{font-size:12px;color:var(--muted)}.who form{display:inline}.who > button,.who form button{border:0;background:none;color:var(--accent);cursor:pointer;font:inherit;font-size:12px;padding:0}
.log{flex:1;overflow:auto;padding:6px 2px;display:flex;flex-direction:column;gap:8px}
.m{max-width:85%;padding:9px 12px;border-radius:14px;white-space:pre-wrap;word-break:break-word;font-size:14px;line-height:1.4}
.m.you{align-self:flex-end;background:var(--accent);color:#fff;border-bottom-right-radius:4px}.m.desk{align-self:flex-start;background:var(--bg);border:1px solid var(--line);border-bottom-left-radius:4px}
.m small{display:block;font-size:10px;opacity:.7;margin-top:4px}.m img{max-width:100%;border-radius:8px;display:block;margin-bottom:6px}
.sys{align-self:center;font-size:11px;color:var(--muted)}
.composer{display:flex;gap:8px;align-items:flex-end;margin-top:10px;border-top:1px solid var(--line);padding-top:10px}
.composer textarea{flex:1;font:inherit;padding:9px 11px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--fg);resize:none;min-height:40px;max-height:140px}
.composer .btn{margin:0;padding:9px 14px;width:auto}.composer label.att{margin:0;cursor:pointer;color:var(--muted);display:grid;place-items:center;width:38px;height:38px;border-radius:10px;border:1px solid var(--line)}
.composer label.att svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}.composer input[type=file]{display:none}
.typing{font-size:12px;color:var(--muted);height:16px}
"""

CHAT = """
<div class="who">Signed in as <b>{NAME}</b> · <form method="post" action="/portal/leave"><button type="submit">not you?</button></form> · <button type="button" onclick="clearChat()">clear chat</button></div>
<div class="log" id="log"><div class="sys">Write your request below. Photos of error screens help.</div></div>
<div class="typing" id="typing"></div>
<form class="composer" id="f" onsubmit="return send(event)">
<label class="att" title="Attach a photo"><input type="file" id="img" accept="image/*" onchange="preview()"><svg viewBox="0 0 24 24"><path d="M21.4 11.1 12.3 20.2a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5"/></svg></label>
<textarea id="t" rows="1" placeholder="Describe the problem…" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();send(event)}"></textarea>
<button class="btn primary" type="submit">Send</button></form>
<script>
const log=document.getElementById('log'),T=document.getElementById('t'),TY=document.getElementById('typing'),IMG=document.getElementById('img');
let pendingImg=null;
function add(who,text,at,img){const d=document.createElement('div');d.className='m '+who;if(img){const i=document.createElement('img');i.src=img;d.appendChild(i);}
 d.appendChild(document.createTextNode(text||''));const s=document.createElement('small');s.textContent=at?at.replace('T',' '):new Date().toISOString().slice(0,16).replace('T',' ');d.appendChild(s);log.appendChild(d);log.scrollTop=log.scrollHeight;}
function preview(){const f=IMG.files[0];if(!f)return;pendingImg=URL.createObjectURL(f);TY.textContent='Photo attached: '+f.name;}
async function send(e){e.preventDefault();const text=T.value.trim();const f=IMG.files[0];if(!text&&!f)return;
 add('you',text||'(photo)',null,pendingImg);T.value='';const fd=new FormData();fd.append('text',text);if(f)fd.append('image',f);IMG.value='';pendingImg=null;
 TY.textContent='Service desk is typing…';
 try{const r=await fetch('/portal/say',{method:'POST',body:fd});const j=await r.json();TY.textContent='';
  if(!r.ok){add('desk',j.error||'Something went wrong. Please try again.');return;}add('desk',j.text);}
 catch(err){TY.textContent='';add('desk','Could not reach the desk. Please try again.');}}
function since(){try{return localStorage.getItem('portal_clear')||''}catch(e){return ''}}
function clearChat(){try{localStorage.setItem('portal_clear',new Date().toISOString().slice(0,16))}catch(e){}log.innerHTML='<div class="sys">Chat cleared. Your tickets are still on the desk.</div>';}
async function load(){try{const r=await fetch('/portal/history');if(r.status===401){location.reload();return;}const rows=await r.json();const s=since();rows.filter(m=>!s||m.at>s).forEach(m=>add(m.who,m.text,m.at));}catch(e){}}
async function poll(){try{const r=await fetch('/portal/updates');if(r.ok){(await r.json()).forEach(m=>add(m.who,m.text,m.at));}}catch(e){}}
load();setInterval(poll,5000);
</script>
"""
