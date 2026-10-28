"""The employee support page (``/support-agent``): outside the console, no desk account.

Two ways in, both only for people the desk knows (directory or the clients table):
* **start without signing in** — name + phone/e-mail; recognised → a chat that opens tickets like WhatsApp;
* **sign in with a one-time code** sent over WhatsApp or e-mail — then the employee also sees *their own*
  tickets (list, status, conversation, reply, close, rating). Nobody else's.

Identity is the phone number (or e-mail), the same requester WhatsApp and mail use, so every channel shares
the employee's tickets. The cookie lasts the browser session.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import timedelta

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app import systems
from app.agents import handle_message
from app.channels import portal_config
from app.db import Client, ClientCode, Event, Outbound, Ticket, _now, log, session
from app.security import cookie_secure, rate_limit

router = APIRouter()
COOKIE = "muse_support"
BASE = "/support-agent"
CODE_MINUTES = 10


# ---- identity ------------------------------------------------------------------------------------

def normalise(contact: str) -> str:
    """'0555 111 00 01' → '+905551110001'; e-mail lower-cased."""
    c = (contact or "").strip()
    if "@" in c:
        return c.lower()
    digits = re.sub(r"[^0-9+]", "", c)
    if not digits:
        return ""
    if not digits.startswith("+"):
        cc = str(portal_config().get("country", "+90"))
        digits = cc + digits[1:] if digits.startswith("0") and len(digits) == 11 else cc + digits if len(digits) == 10 else "+" + digits
    return digits if re.fullmatch(r"\+[0-9]{8,15}", digits) else ""


def recognise(db, contact: str) -> dict | None:
    """Who is this? A client record wins (admins may add people the directory lacks); then the directory."""
    if not contact:
        return None
    col = Client.email if "@" in contact else Client.phone
    c = db.query(Client).filter(col == contact).first()
    if c is not None:
        if not c.enabled:
            return None
        return {"client_id": c.id, "name": c.name, "phone": c.phone, "email": c.email, "employee_no": c.employee_no}
    hit = systems.user_by_contact(contact)
    if hit is None:
        return None
    emp, u = hit
    return {"client_id": None, "name": u["name"], "phone": u.get("phone") or "", "email": systems.user_email(emp), "employee_no": emp}


def _ensure_client(db, who: dict) -> Client:
    """First sign-in of a directory employee creates their client record."""
    if who.get("client_id"):
        return db.get(Client, who["client_id"])
    c = Client(employee_no=who["employee_no"], name=who["name"], phone=who["phone"], email=who["email"])
    db.add(c)
    db.flush()
    return c


def requester_of(who: dict) -> str:
    return who.get("phone") or who.get("email") or ""


def _token(payload: dict) -> str:
    from app import vault
    return vault._fernet().encrypt(json.dumps(payload).encode()).decode()


def _ident(request: Request) -> dict | None:
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    from app import vault
    try:
        return json.loads(vault._fernet().decrypt(raw.encode()).decode())
    except Exception:  # noqa: BLE001
        return None


def _require(request: Request, signed_in: bool = False) -> dict:
    who = _ident(request)
    if not who or not portal_config()["enabled"] or (signed_in and who.get("mode") != "client"):
        raise HTTPException(401)
    return who


def _set(resp, payload: dict):
    resp.set_cookie(COOKIE, _token(payload), httponly=True, samesite="lax", secure=cookie_secure())
    return resp


def _esc(x: str) -> str:
    return (x or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# ---- pages ----------------------------------------------------------------------------------------

@router.get("/portal")
def old_link():
    return RedirectResponse(BASE, 301)


@router.get(BASE, response_class=HTMLResponse)
def page(request: Request, error: str = "", info: str = "", code_for: str = "") -> str:
    cfg = portal_config()
    if not cfg["enabled"]:
        return _shell(cfg, '<p class="lead">The support page is switched off. Please write to the service desk over WhatsApp or e-mail.</p>')
    who = _ident(request)
    if who is not None:
        return _shell(cfg, APP.replace("{NAME}", _esc(who["name"])).replace("{MODE}", who.get("mode", "guest")), app=True)
    msg = ('<div class="alert bad">' + _esc(error) + "</div>") if error else (('<div class="alert ok">' + _esc(info) + "</div>") if info else "")
    if code_for:
        body = f"""
<p class="lead">We sent a 6-digit code to <b>{_esc(code_for)}</b>. It is valid for {CODE_MINUTES} minutes.</p>{msg}
<form method="post" action="{BASE}/verify"><input type="hidden" name="contact" value="{_esc(code_for)}">
<label for="code">Code</label><input id="code" name="code" inputmode="numeric" pattern="[0-9]{{6}}" maxlength="6" required autofocus autocomplete="one-time-code">
<button class="btn primary" type="submit">Sign in</button></form>
<p class="links"><a href="{BASE}">Start over</a></p>"""
    else:
        body = f"""
<p class="lead">Write to IT as you would on WhatsApp, or sign in to follow your requests.</p>{msg}
<div class="two">
<form method="post" action="{BASE}/start" class="pane"><h3>Start a conversation</h3>
<label for="name">Your name</label><input id="name" name="name" required maxlength="80">
<label for="contact">Mobile number or work e-mail <small>(the one IT knows you by)</small></label><input id="contact" name="contact" required placeholder="+90 5xx xxx xx xx">
<button class="btn primary" type="submit">Start chat</button></form>
<form method="post" action="{BASE}/code" class="pane"><h3>Sign in to see your tickets</h3>
<label for="contact2">Mobile number or work e-mail</label><input id="contact2" name="contact" required placeholder="+90 5xx xxx xx xx">
<p class="muted" style="margin:6px 0 0">We send a one-time code over WhatsApp or e-mail. No password.</p>
<button class="btn primary" type="submit">Send code</button></form></div>
{('<p class="muted" style="margin-top:14px">' + _esc(cfg["note"]) + '</p>') if cfg.get("note") else ''}"""
    return _shell(cfg, body, wide=True)


@router.post(BASE + "/start")
def start(request: Request, name: str = Form(...), contact: str = Form(...)):
    rate_limit(request, "support-start", 20, 600)
    if not portal_config()["enabled"]:
        raise HTTPException(403)
    c = normalise(contact)
    with session() as db:
        who = recognise(db, c)
    if who is None:
        return RedirectResponse(f"{BASE}?error=IT+does+not+know+this+number+or+address.+Use+the+one+in+the+company+directory,+or+ask+the+desk+to+add+you.", 303)
    who = {**who, "mode": "guest", "name": name.strip()[:80] or who["name"], "since": _now().isoformat(timespec="minutes")}
    return _set(RedirectResponse(BASE, 303), who)


@router.post(BASE + "/code")
def send_code(request: Request, contact: str = Form(...)):
    rate_limit(request, "support-code", 10, 600)
    if not portal_config()["enabled"]:
        raise HTTPException(403)
    c = normalise(contact)
    with session() as db:
        who = recognise(db, c)
        if who is not None:
            code = f"{secrets.randbelow(1_000_000):06d}"
            db.query(ClientCode).filter(ClientCode.contact == c, ClientCode.used.is_(False)).update({"used": True})
            db.add(ClientCode(contact=c, code_hash=_hash(c, code), expires_at=_now() + timedelta(minutes=CODE_MINUTES)))
            _deliver(c, code)
    # the same answer whether or not we know the address: no way to probe who is in the directory
    return RedirectResponse(f"{BASE}?code_for={c}", 303)


def _hash(contact: str, code: str) -> str:
    return hashlib.sha256(f"{contact}:{code}".encode()).hexdigest()


def _deliver(contact: str, code: str) -> None:
    text = f"Muse ServiceDesk sign-in code: {code} (valid {CODE_MINUTES} minutes). If you did not ask for it, ignore this message."
    if "@" in contact:
        from app.channels import send_email
        send_email(contact, "Your sign-in code", text)
    else:
        from app.whatsapp import try_send
        try_send(contact, text)


@router.post(BASE + "/verify")
def verify(request: Request, contact: str = Form(...), code: str = Form(...)):
    rate_limit(request, "support-verify", 15, 600)
    c = normalise(contact)
    with session() as db:
        row = (db.query(ClientCode).filter(ClientCode.contact == c, ClientCode.used.is_(False), ClientCode.expires_at > _now())
               .order_by(ClientCode.id.desc()).first())
        if row is None or row.attempts >= 5:
            return RedirectResponse(f"{BASE}?code_for={c}&error=Code+expired+or+not+found.+Ask+for+a+new+one.", 303)
        row.attempts += 1
        if not secrets.compare_digest(row.code_hash, _hash(c, code.strip())):
            return RedirectResponse(f"{BASE}?code_for={c}&error=Wrong+code.", 303)
        row.used = True
        who = recognise(db, c)
        if who is None:
            raise HTTPException(403)
        client = _ensure_client(db, who)
        client.last_login_at = _now()
        payload = {"mode": "client", "client_id": client.id, "name": client.name, "phone": client.phone, "email": client.email,
                   "employee_no": client.employee_no}
    return _set(RedirectResponse(BASE, 303), payload)


@router.post(BASE + "/leave")
def leave():
    r = RedirectResponse(BASE, 303)
    r.delete_cookie(COOKIE)
    return r


# ---- chat (guest and client) ------------------------------------------------------------------------

def _addresses(who: dict) -> list[str]:
    return [a for a in (who.get("phone"), who.get("email")) if a]


@router.get(BASE + "/history")
def history(request: Request) -> list[dict]:
    """Guests see only this conversation (from sign-in on); signed-in clients see every message on their tickets."""
    who = _require(request)
    addrs = _addresses(who)
    with session() as db:
        q = db.query(Ticket).filter(Ticket.requester.in_(addrs)).order_by(Ticket.id.desc())
        tickets = q.limit(10).all()
        ids = [t.id for t in tickets]
        rows = db.query(Event).filter(Event.ticket_id.in_(ids), Event.kind == "message").order_by(Event.id).all() if ids else []
        out = [{"ticket": e.ticket_id, "who": "you" if e.actor == "user" else "desk", "text": e.content,
                "at": e.at.isoformat(timespec="minutes")} for e in rows]
        for o in db.query(Outbound).filter(Outbound.to.in_(addrs), Outbound.sent.is_(False), Outbound.error == ""):
            t = db.get(Ticket, o.ticket_id) if o.ticket_id else None
            if t is None or t.channel == "web":
                out.append({"ticket": o.ticket_id, "who": "desk", "text": o.text, "at": o.at.isoformat(timespec="minutes")})
                o.sent = True
    if who.get("mode") != "client":
        out = [m for m in out if m["at"] >= who.get("since", "")]
    return out


@router.get(BASE + "/updates")
def updates(request: Request) -> list[dict]:
    who = _require(request)
    out = []
    with session() as db:
        for o in db.query(Outbound).filter(Outbound.to.in_(_addresses(who)), Outbound.sent.is_(False), Outbound.error == "").order_by(Outbound.id):
            t = db.get(Ticket, o.ticket_id) if o.ticket_id else None
            if t is not None and t.channel != "web":
                continue
            o.sent = True
            out.append({"ticket": o.ticket_id, "who": "desk", "text": o.text, "at": o.at.isoformat(timespec="minutes")})
    return out


@router.post(BASE + "/say")
async def say(request: Request, text: str = Form(""), image: UploadFile | None = File(None), ticket: int | None = Form(None)):
    who = _require(request)
    rate_limit(request, "support-say", 60, 300)
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
        pinned = _own_ticket(db, who, ticket) if ticket else None
        if ticket and pinned is None:
            raise HTTPException(404)
        t, answer = handle_message(db, requester_of(who), text or "(photo)", data, mime, channel="web", ticket=pinned)
        return {"ticket": t.id if t else None, "status": t.status if t else None, "text": answer}


# ---- my tickets (signed-in clients only) ------------------------------------------------------------

def _own_ticket(db, who: dict, ticket_id: int) -> Ticket | None:
    t = db.get(Ticket, ticket_id)
    return t if t is not None and t.requester in _addresses(who) else None


@router.get(BASE + "/tickets")
def my_tickets(request: Request) -> list[dict]:
    who = _require(request, signed_in=True)
    from app.sla import risk, timing
    with session() as db:
        out = []
        for t in db.query(Ticket).filter(Ticket.requester.in_(_addresses(who))).order_by(Ticket.id.desc()).limit(100):
            last = [e for e in t.events if e.kind == "message"]
            out.append({"id": t.id, "title": t.title.split("\n")[0][:90], "status": t.status, "owner": t.owner, "channel": t.channel,
                        "created_at": t.created_at.isoformat(timespec="minutes"), "rating": t.rating,
                        "last_at": last[-1].at.isoformat(timespec="minutes") if last else None,
                        "risk": risk(t, t.events) if t.status != "resolved" else None})
        return out


@router.get(BASE + "/tickets/{ticket_id}")
def my_ticket(request: Request, ticket_id: int) -> dict:
    who = _require(request, signed_in=True)
    from app.sla import timing
    with session() as db:
        t = _own_ticket(db, who, ticket_id)
        if t is None:
            raise HTTPException(404)
        msgs = [{"who": "you" if e.actor == "user" else "desk", "text": e.content, "at": e.at.isoformat(timespec="minutes")}
                for e in t.events if e.kind == "message"]
        return {"id": t.id, "title": t.title, "status": t.status, "owner": t.owner, "channel": t.channel, "rating": t.rating,
                "created_at": t.created_at.isoformat(timespec="minutes"), "resolution": t.resolution, "messages": msgs,
                "timing": timing(t, t.events)}


@router.post(BASE + "/tickets/{ticket_id}/close")
def close_mine(request: Request, ticket_id: int, note: str = Form("")):
    who = _require(request, signed_in=True)
    from app.i18n import t as tr_, ticket_lang
    from app.tools import Ctx, run_tool
    with session() as db:
        t = _own_ticket(db, who, ticket_id)
        if t is None:
            raise HTTPException(404)
        if t.status != "resolved":
            run_tool(Ctx(db=db, ticket=t, agent="L1"), "close_ticket", {"resolution": note.strip() or "closed by the employee from the support page"})
            log(db, t.id, "L1", "tool_call", "close_ticket", {"resolution": note.strip() or "closed by the employee"})
            log(db, t.id, "L1", "message", tr_("closed_on_request", ticket_lang(t), id=t.id))
        return {"id": t.id, "status": t.status}


@router.post(BASE + "/tickets/{ticket_id}/rate")
def rate_mine(request: Request, ticket_id: int, rating: int = Form(...)):
    who = _require(request, signed_in=True)
    with session() as db:
        t = _own_ticket(db, who, ticket_id)
        if t is None or t.status != "resolved":
            raise HTTPException(404)
        t.rating = max(1, min(5, int(rating)))
        log(db, t.id, "user", "note", f"rated {t.rating}/5 from the support page")
        return {"id": t.id, "rating": t.rating}


# ---- html -------------------------------------------------------------------------------------------

def _shell(cfg: dict, body: str, app: bool = False, wide: bool = False) -> str:
    from app.auth import CSS, LOGO
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{_esc(cfg["title"])}</title>'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><style>{CSS}{SUPPORT_CSS}</style></head>'
            f'<body class="auth{" chat" if app else ""}{" wide" if wide else ""}"><div class="card"><div class="brand"><div class="logo">{LOGO}</div>'
            f'<h2>{_esc(cfg["title"])}</h2></div>{body}</div></body></html>')


SUPPORT_CSS = """
.auth.wide .card{width:min(760px,100%)}.two{display:grid;grid-template-columns:1fr 1fr;gap:18px}@media(max-width:640px){.two{grid-template-columns:1fr}}
.pane{border:1px solid var(--line);border-radius:12px;padding:14px 16px}.pane h3{margin:0 0 4px}
body.chat{align-items:stretch}.chat .card{width:min(760px,100%);display:flex;flex-direction:column;height:min(880px,calc(100vh - 48px))}
.chat .brand{flex-direction:row;align-items:center;justify-content:space-between;margin-bottom:8px}.chat .brand h2{font-size:18px}
.who{font-size:12px;color:var(--muted);display:flex;gap:8px;align-items:center;flex-wrap:wrap}.who form{display:inline}.who button{border:0;background:none;color:var(--accent);cursor:pointer;font:inherit;font-size:12px;padding:0}
.tabs{display:flex;gap:4px;margin:8px 0}.tabs button{font:inherit;font-size:13px;padding:6px 12px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}.tabs button.on{background:var(--accent);border-color:var(--accent);color:#fff}
.view{flex:1;display:none;flex-direction:column;min-height:0}.view.on{display:flex}
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
.tl{overflow:auto;flex:1}.tl table{font-size:13px}.tl tr{cursor:pointer}.tl tr:hover td{background:var(--bg)}
.tag{font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}.tag.open,.tag.waiting_user,.tag.waiting_approval,.tag.escalated{color:var(--accent);border-color:var(--accent)}.tag.resolved{color:var(--ok);border-color:var(--ok)}.tag.risk{color:var(--bad);border-color:var(--bad)}
.detail{display:none;flex-direction:column;min-height:0;flex:1}.detail.on{display:flex}.detail .head{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px}.detail .head .btn{margin:0;width:auto;padding:6px 10px;font-size:12px}
.stars button{font-size:20px;background:none;border:0;cursor:pointer;color:var(--muted)}.stars button.on{color:#f5a623}
"""

APP = """
<div class="who"><span>Signed in as <b>{NAME}</b> <span class="tag">{MODE}</span></span>
<form method="post" action="/support-agent/leave"><button type="submit">not you?</button></form> · <button type="button" onclick="clearChat()">clear chat</button></div>
<div class="tabs" id="tabs"><button class="on" onclick="show('chat')">Chat</button><button id="tabTickets" onclick="show('tickets')" style="display:none">My tickets</button></div>
<div class="view on" id="chat">
<div class="log" id="log"><div class="sys">Write your request below. Photos of error screens help.</div></div>
<div class="typing" id="typing"></div>
<form class="composer" id="f" onsubmit="return send(event)">
<label class="att" title="Attach a photo"><input type="file" id="img" accept="image/*" onchange="preview()"><svg viewBox="0 0 24 24"><path d="M21.4 11.1 12.3 20.2a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5"/></svg></label>
<textarea id="t" rows="1" placeholder="Describe the problem…" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();send(event)}"></textarea>
<button class="btn primary" type="submit">Send</button></form></div>
<div class="view" id="tickets">
<div class="tl" id="tlist"></div>
<div class="detail" id="detail"><div class="head"><div><b id="dTitle"></b> <span class="tag" id="dStatus"></span><div class="muted" id="dMeta" style="font-size:12px"></div></div>
<div><button class="btn" onclick="backToList()">Back</button> <button class="btn" id="dClose" onclick="closeTicket()">Close ticket</button></div></div>
<div class="stars" id="stars" style="display:none"></div>
<div class="log" id="dlog"></div>
<form class="composer" onsubmit="return reply(event)"><textarea id="dt" rows="1" placeholder="Reply on this ticket…" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();reply(event)}"></textarea><button class="btn primary" type="submit">Send</button></form>
</div></div>
<script>
const B='/support-agent',MODE='{MODE}';
const log=document.getElementById('log'),T=document.getElementById('t'),TY=document.getElementById('typing'),IMG=document.getElementById('img');
let pendingImg=null,cur=null;
function fmt(at){return at?at.replace('T',' '):new Date().toISOString().slice(0,16).replace('T',' ')}
function add(who,text,at,img,target){const L=target||log;const d=document.createElement('div');d.className='m '+who;if(img){const i=document.createElement('img');i.src=img;d.appendChild(i);}
 d.appendChild(document.createTextNode(text||''));const s=document.createElement('small');s.textContent=fmt(at);d.appendChild(s);L.appendChild(d);L.scrollTop=L.scrollHeight;}
function preview(){const f=IMG.files[0];if(!f)return;pendingImg=URL.createObjectURL(f);TY.textContent='Photo attached: '+f.name;}
async function send(e){e.preventDefault();const text=T.value.trim();const f=IMG.files[0];if(!text&&!f)return;
 add('you',text||'(photo)',null,pendingImg);T.value='';const fd=new FormData();fd.append('text',text);if(f)fd.append('image',f);IMG.value='';pendingImg=null;
 TY.textContent='Service desk is typing…';
 try{const r=await fetch(B+'/say',{method:'POST',body:fd});const j=await r.json();TY.textContent='';
  if(!r.ok){add('desk',j.error||'Something went wrong. Please try again.');return;}add('desk',j.text);if(MODE==='client')loadTickets();}
 catch(err){TY.textContent='';add('desk','Could not reach the desk. Please try again.');}}
function since(){try{return localStorage.getItem('support_clear')||''}catch(e){return ''}}
function clearChat(){try{localStorage.setItem('support_clear',new Date().toISOString().slice(0,16))}catch(e){}log.innerHTML='<div class="sys">Chat cleared. Your tickets are still on the desk.</div>';}
async function load(){try{const r=await fetch(B+'/history');if(r.status===401){location.reload();return;}const rows=await r.json();const s=since();rows.filter(m=>!s||m.at>s).forEach(m=>add(m.who,m.text,m.at));}catch(e){}}
async function poll(){try{const r=await fetch(B+'/updates');if(r.ok){(await r.json()).forEach(m=>{add(m.who,m.text,m.at);if(cur&&m.ticket===cur)add(m.who,m.text,m.at,null,document.getElementById('dlog'));});}}catch(e){}}
function show(v){document.querySelectorAll('.view').forEach(x=>x.classList.toggle('on',x.id===v));document.querySelectorAll('#tabs button').forEach(b=>b.classList.toggle('on',b.textContent.toLowerCase().startsWith(v==='chat'?'chat':'my')));if(v==='tickets')loadTickets();}
const LAB={open:'Open',waiting_user:'Waiting for you',waiting_approval:'Waiting for approval',escalated:'With an engineer',resolved:'Resolved'};
async function loadTickets(){const r=await fetch(B+'/tickets');if(!r.ok)return;const rows=await r.json();
 document.getElementById('tlist').innerHTML='<table><thead><tr><th>#</th><th>Request</th><th>Status</th><th>Last update</th></tr></thead><tbody>'+rows.map(t=>`<tr onclick="openTicket(${t.id})"><td class="mono">${t.id}</td><td>${esc(t.title)}</td><td><span class="tag ${t.status}">${LAB[t.status]||t.status}</span>${t.risk&&t.risk.pct>=100?' <span class="tag risk">SLA</span>':''}</td><td class="muted">${(t.last_at||t.created_at).replace('T',' ')}</td></tr>`).join('')+'</tbody></table>'+(rows.length?'':'<p class="muted">No requests yet.</p>');}
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
async function openTicket(id){const r=await fetch(B+'/tickets/'+id);if(!r.ok)return;const t=await r.json();cur=id;
 document.getElementById('dTitle').textContent='#'+t.id+' · '+t.title.split('\\n')[0].slice(0,80);const st=document.getElementById('dStatus');st.textContent=LAB[t.status]||t.status;st.className='tag '+t.status;
 document.getElementById('dMeta').textContent='opened '+t.created_at.replace('T',' ')+' · via '+t.channel+(t.resolution?' · '+t.resolution:'');
 document.getElementById('dClose').style.display=t.status==='resolved'?'none':'';
 const dl=document.getElementById('dlog');dl.innerHTML='';t.messages.forEach(m=>add(m.who,m.text,m.at,null,dl));
 const S=document.getElementById('stars');if(t.status==='resolved'){S.style.display='';S.innerHTML='<span class="muted" style="font-size:12px">How did we do?</span> '+[1,2,3,4,5].map(n=>`<button class="${t.rating&&n<=t.rating?'on':''}" onclick="rate(${n})">★</button>`).join('');}else{S.style.display='none';}
 document.getElementById('tlist').style.display='none';document.getElementById('detail').classList.add('on');}
function backToList(){cur=null;document.getElementById('detail').classList.remove('on');document.getElementById('tlist').style.display='';loadTickets();}
async function reply(e){e.preventDefault();const ta=document.getElementById('dt');const text=ta.value.trim();if(!text||!cur)return;const dl=document.getElementById('dlog');add('you',text,null,null,dl);ta.value='';
 const fd=new FormData();fd.append('text',text);fd.append('ticket',cur);const r=await fetch(B+'/say',{method:'POST',body:fd});const j=await r.json();add('desk',j.text||j.error,null,null,dl);if(j.status){const st=document.getElementById('dStatus');st.textContent=LAB[j.status]||j.status;st.className='tag '+j.status;}}
async function closeTicket(){if(!cur||!confirm('Close this request?'))return;const fd=new FormData();fd.append('note','');await fetch(B+'/tickets/'+cur+'/close',{method:'POST',body:fd});openTicket(cur);}
async function rate(n){if(!cur)return;const fd=new FormData();fd.append('rating',n);await fetch(B+'/tickets/'+cur+'/rate',{method:'POST',body:fd});openTicket(cur);}
if(MODE==='client')document.getElementById('tabTickets').style.display='';
load();setInterval(poll,5000);
</script>
"""
