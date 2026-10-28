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
body.chat{align-items:stretch}.chat .card{width:min(1040px,100%);display:flex;flex-direction:column;height:min(900px,calc(100vh - 48px));padding:18px 20px 16px}
.chat .brand{flex-direction:row;align-items:center;justify-content:space-between;margin-bottom:8px}.chat .brand h2{font-size:18px}
.who{font-size:12px;color:var(--muted);display:flex;gap:8px;align-items:center;flex-wrap:wrap}.who form{display:inline}.who button{border:0;background:none;color:var(--accent);cursor:pointer;font:inherit;font-size:12px;padding:0}
.tag{font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--muted);white-space:nowrap}.tag.open,.tag.waiting_user,.tag.waiting_approval,.tag.escalated{color:var(--accent);border-color:var(--accent)}.tag.resolved{color:var(--ok);border-color:var(--ok)}.tag.risk{color:var(--bad);border-color:var(--bad)}
.layout{display:grid;grid-template-columns:270px 1fr;gap:14px;flex:1;min-height:0;margin-top:8px}.layout.guest{grid-template-columns:1fr}
.side{border:1px solid var(--line);border-radius:12px;display:flex;flex-direction:column;min-height:0;overflow:hidden}.side .btn{margin:10px;width:auto;padding:8px 10px;font-size:13px}
.side h4{margin:4px 12px 4px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}.side .list{overflow:auto;flex:1}
.side .it{display:block;width:100%;text-align:left;border:0;border-top:1px solid var(--line);background:none;color:var(--fg);padding:9px 12px;cursor:pointer;font:inherit}.side .it:hover,.side .it.on{background:var(--bg)}
.side .it b{display:block;font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.side .it small{color:var(--muted);font-size:11px;display:flex;gap:6px;align-items:center;margin-top:2px}
.main{display:flex;flex-direction:column;min-height:0;border:1px solid var(--line);border-radius:12px;padding:10px 12px}
.head{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px;min-height:28px}.head b{font-size:14px}.head .btn{margin:0;width:auto;padding:5px 10px;font-size:12px}.head .meta{font-size:12px;color:var(--muted)}
.log{flex:1;overflow:auto;padding:6px 2px;display:flex;flex-direction:column;gap:8px}
.m{max-width:85%;padding:9px 12px;border-radius:14px;white-space:pre-wrap;word-break:break-word;font-size:14px;line-height:1.4}
.m.you{align-self:flex-end;background:var(--accent);color:#fff;border-bottom-right-radius:4px}.m.desk{align-self:flex-start;background:var(--bg);border:1px solid var(--line);border-bottom-left-radius:4px}
.m small{display:block;font-size:10px;opacity:.7;margin-top:4px}.m img{max-width:100%;border-radius:8px;display:block;margin-bottom:6px}
.sys{align-self:center;font-size:11px;color:var(--muted);text-align:center;padding:20px 10px}
.composer{display:flex;gap:8px;align-items:flex-end;margin-top:10px;border-top:1px solid var(--line);padding-top:10px}
.composer textarea{flex:1;font:inherit;padding:9px 11px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--fg);resize:none;min-height:40px;max-height:140px}
.composer .btn{margin:0;padding:9px 14px;width:auto}.composer label.att{margin:0;cursor:pointer;color:var(--muted);display:grid;place-items:center;width:38px;height:38px;border-radius:10px;border:1px solid var(--line)}
.composer label.att svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}.composer input[type=file]{display:none}
.typing{font-size:12px;color:var(--muted);height:16px}
.stars button{font-size:20px;background:none;border:0;cursor:pointer;color:var(--muted);padding:0 2px}.stars button.on{color:#f5a623}
@media(max-width:720px){.layout{grid-template-columns:1fr;grid-template-rows:auto 1fr}.side{max-height:34vh}.chat .card{padding:12px}}
"""

APP = """
<div class="who"><span>Signed in as <b>{NAME}</b> <span class="tag">{MODE}</span></span>
<form method="post" action="/support-agent/leave"><button type="submit">not you?</button></form></div>
<div class="layout {MODE}" id="layout">
<aside class="side" id="side"><button class="btn primary" onclick="newConv()">＋ New conversation</button><h4>My requests</h4><div class="list" id="tlist"><div class="sys">Loading…</div></div></aside>
<section class="main">
<div class="head"><div><b id="hTitle">New conversation</b> <span class="tag" id="hStatus" style="display:none"></span><div class="meta" id="hMeta"></div></div>
<div id="hActions"><span class="stars" id="stars" style="display:none"></span> <button class="btn" id="btnClose" style="display:none" onclick="closeTicket()">Close request</button></div></div>
<div class="log" id="log"><div class="sys">Describe your problem or request. Photos of error screens help.<br>Each new topic becomes its own request; you will see it on the left.</div></div>
<div class="typing" id="typing"></div>
<form class="composer" id="f" onsubmit="return send(event)">
<label class="att" title="Attach a photo"><input type="file" id="img" accept="image/*" onchange="preview()"><svg viewBox="0 0 24 24"><path d="M21.4 11.1 12.3 20.2a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 0 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5"/></svg></label>
<textarea id="t" rows="1" placeholder="Describe the problem…" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();send(event)}"></textarea>
<button class="btn primary" type="submit">Send</button></form></section></div>
<script>
const B='/support-agent',MODE='{MODE}';
const log=document.getElementById('log'),T=document.getElementById('t'),TY=document.getElementById('typing'),IMG=document.getElementById('img');
const LAB={open:'Open',waiting_user:'Waiting for you',waiting_approval:'Waiting for approval',escalated:'With an engineer',resolved:'Resolved'};
let pendingImg=null,cur=null,tickets=[];
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function fmt(at){return at?at.replace('T',' '):new Date().toISOString().slice(0,16).replace('T',' ')}
function add(who,text,at,img,ticket){const d=document.createElement('div');d.className='m '+who;if(img){const i=document.createElement('img');i.src=img;d.appendChild(i);}
 d.appendChild(document.createTextNode(text||''));const s=document.createElement('small');s.textContent=fmt(at)+(ticket&&cur===null?' · #'+ticket:'');d.appendChild(s);log.appendChild(d);log.scrollTop=log.scrollHeight;}
function preview(){const f=IMG.files[0];if(!f)return;pendingImg=URL.createObjectURL(f);TY.textContent='Photo attached: '+f.name;}
function setHead(t){const st=document.getElementById('hStatus'),S=document.getElementById('stars');
 if(!t){document.getElementById('hTitle').textContent='New conversation';st.style.display='none';document.getElementById('hMeta').textContent='';document.getElementById('btnClose').style.display='none';S.style.display='none';return;}
 document.getElementById('hTitle').textContent='#'+t.id+' · '+(t.title||'').split('\n')[0].slice(0,70);st.style.display='';st.textContent=LAB[t.status]||t.status;st.className='tag '+t.status;
 document.getElementById('hMeta').textContent='opened '+fmt(t.created_at)+' · via '+t.channel+(t.resolution?' · '+t.resolution:'');
 document.getElementById('btnClose').style.display=t.status==='resolved'?'none':'';
 if(t.status==='resolved'){S.style.display='';S.innerHTML='<span class="muted" style="font-size:12px">How did we do?</span> '+[1,2,3,4,5].map(n=>`<button class="${t.rating&&n<=t.rating?'on':''}" onclick="rate(${n})">★</button>`).join('');}else{S.style.display='none';}}
function newConv(){cur=null;setHead(null);log.innerHTML='<div class="sys">Describe your problem or request. Photos of error screens help.<br>Each new topic becomes its own request; you will see it on the left.</div>';markList();T.focus();}
function markList(){document.querySelectorAll('.side .it').forEach(b=>b.classList.toggle('on',Number(b.dataset.id)===cur));}
async function loadTickets(){if(MODE!=='client')return;const r=await fetch(B+'/tickets');if(!r.ok)return;tickets=await r.json();
 const L=document.getElementById('tlist');L.innerHTML=tickets.length?tickets.map(t=>`<button class="it" data-id="${t.id}" onclick="openTicket(${t.id})"><b>#${t.id} ${esc(t.title)}</b><small><span class="tag ${t.status}">${LAB[t.status]||t.status}</span>${t.risk&&t.risk.pct>=100?'<span class="tag risk">SLA</span>':''}<span>${fmt(t.last_at||t.created_at)}</span></small></button>`).join(''):'<div class="sys">No requests yet.</div>';markList();}
async function openTicket(id){const r=await fetch(B+'/tickets/'+id);if(!r.ok)return;const t=await r.json();cur=id;setHead(t);log.innerHTML='';t.messages.forEach(m=>add(m.who,m.text,m.at));markList();}
async function send(e){e.preventDefault();const text=T.value.trim();const f=IMG.files[0];if(!text&&!f)return;
 add('you',text||'(photo)',null,pendingImg);T.value='';const fd=new FormData();fd.append('text',text);if(f)fd.append('image',f);if(cur)fd.append('ticket',cur);IMG.value='';pendingImg=null;
 TY.textContent='Service desk is typing…';
 try{const r=await fetch(B+'/say',{method:'POST',body:fd});const j=await r.json();TY.textContent='';
  if(!r.ok){add('desk',j.error||'Something went wrong. Please try again.');return;}add('desk',j.text,null,null,j.ticket);
  if(cur&&j.status){const t=tickets.find(x=>x.id===cur);setHead({...(t||{}),id:cur,title:(t||{}).title,status:j.status,channel:'web'});}
  loadTickets();}
 catch(err){TY.textContent='';add('desk','Could not reach the desk. Please try again.');}}
async function poll(){try{const r=await fetch(B+'/updates');if(r.ok){const rows=await r.json();rows.forEach(m=>{if(cur===null||m.ticket===cur)add(m.who,m.text,m.at,null,m.ticket);});if(rows.length)loadTickets();}}catch(e){}}
async function closeTicket(){if(!cur||!confirm('Close this request?'))return;const fd=new FormData();fd.append('note','');await fetch(B+'/tickets/'+cur+'/close',{method:'POST',body:fd});openTicket(cur);loadTickets();}
async function rate(n){if(!cur)return;const fd=new FormData();fd.append('rating',n);await fetch(B+'/tickets/'+cur+'/rate',{method:'POST',body:fd});openTicket(cur);}
loadTickets();setInterval(poll,5000);
</script>
"""
