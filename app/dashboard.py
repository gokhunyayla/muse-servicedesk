"""Manager dashboard: queues, auto-resolution rate, satisfaction, top issues, per-tier load, and a
live view of any ticket's audit trail. One HTML page that polls the JSON endpoints below."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.auth import current_user
from app.db import Event, Ticket, _now, session

router = APIRouter()


def period_start(period: str | None):
    """Start of today / this week / month / quarter / year in the desk's time zone (DESK_TZ, default
    Europe/Istanbul), returned as naive UTC to compare with stored timestamps; None = all time."""
    import os
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo
    if period in (None, "", "all"):
        return None
    tz = ZoneInfo(os.getenv("DESK_TZ", "Europe/Istanbul"))
    local = datetime.now(tz)
    day = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        return _to_utc(day)
    if period == "week":
        return _to_utc(day - timedelta(days=day.weekday()))
    if period == "month":
        return _to_utc(day.replace(day=1))
    if period == "quarter":
        return _to_utc(day.replace(month=3 * ((day.month - 1) // 3) + 1, day=1))
    if period == "year":
        return _to_utc(day.replace(month=1, day=1))
    return None


def _to_utc(local_dt):
    from datetime import UTC
    return local_dt.astimezone(UTC).replace(tzinfo=None)


@router.get("/stats")
def stats(period: str | None = None, owner: str | None = None, user=Depends(current_user)) -> dict:
    """KPIs, top issues and tier load for the selected period; `owner` narrows everything except the
    per-tier cards, which always show the whole period so the cards stay comparable."""
    with session() as db:
        since = period_start(period)
        q = db.query(Ticket)
        if since:
            q = q.filter(Ticket.created_at >= since)
        all_tickets = q.all()
        tickets = [t for t in all_tickets if not owner or t.owner == owner]
        ids = {t.id for t in tickets}
        by_status = Counter(t.status for t in tickets)
        resolved = [t for t in tickets if t.status == "resolved"]
        rated = [t.rating for t in resolved if t.rating]
        since = _now() - timedelta(days=7)
        recent = [t for t in tickets if t.created_at >= since]
        handoffs = Counter()
        for e in db.query(Event).filter(Event.kind == "handoff"):
            if e.ticket_id in ids:
                handoffs[e.content.split(":")[0].strip()] += 1
        tool_calls = Counter(e.actor for e in db.query(Event).filter(Event.kind == "tool_call") if e.ticket_id in ids)
        by_owner = Counter(t.owner for t in all_tickets)
        open_by_owner = Counter(t.owner for t in all_tickets if t.status != "resolved")
        cats = Counter()
        for t in tickets:
            title = t.title.lower()
            cat = next((k for k, words in CATEGORIES.items() if any(w in title for w in words)), "other")
            cats[cat] += 1
        return {
            "total": len(tickets), "by_status": dict(by_status), "last_7_days": len(recent),
            "auto_resolution_rate": round(len(resolved) / len(tickets), 2) if tickets else 0,
            "avg_rating": round(sum(rated) / len(rated), 2) if rated else None, "ratings": len(rated),
            "top_issues": cats.most_common(6), "handoffs": dict(handoffs),
            "tool_calls_by_tier": dict(tool_calls), "by_owner": dict(by_owner),
            "open_by_owner": dict(open_by_owner), "period": period or "all", "owner": owner or "",
            "period_total": len(all_tickets), "period_open": sum(1 for t in all_tickets if t.status != "resolved"),
        }


CATEGORIES = {   # English labels; keywords cover Turkish and English ticket titles
    "vpn / network": ["vpn", "wifi", "wi-fi", "ağ", "network", "internet", "dns"],
    "account / password": ["şifre", "sifre", "password", "hesap", "account", "mfa", "kilit", "lock"],
    "hardware": ["mavi ekran", "bsod", "blue screen", "laptop", "donanım", "hardware", "açılmıyor", "kargo"],
    "security": ["phishing", "oltalama", "fidye", "virüs", "zararlı", "malware", "cve", "açık", "compliance"],
    "license / software": ["lisans", "license", "adobe", "visio", "program", "uygulama", "software"],
    "email": ["outlook", "e-posta", "mail", "incident"],
    "printer": ["yazıcı", "printer"],
    "proactive": ["proactive"],
}


def _render(user, mode: str) -> str:
    from app.auth import RAIL_CSS, rail
    return (PAGE.replace("<!--RAIL_CSS-->", RAIL_CSS).replace("<!--RAIL-->", rail(user, "dash" if mode == "dashboard" else "tickets"))
            .replace("<!--MODE-->", mode))


@router.get("/", response_class=HTMLResponse)
def dashboard_page(user=Depends(current_user)) -> str:
    """Numbers only: period, tier cards, KPIs, top issues, tier load."""
    return _render(user, "dashboard")


@router.get("/tickets")
def tickets(request: Request, status: str | None = None, owner: str | None = None, period: str | None = None,
            q: str | None = None, user=Depends(current_user)):
    """HTML for the browser (the Tickets page), JSON for everything else."""
    if "text/html" in request.headers.get("accept", "") and not request.headers.get("x-requested-with"):
        return HTMLResponse(_render(user, "tickets"))
    with session() as db:
        qs = db.query(Ticket).order_by(Ticket.id.desc())
        if status:
            qs = qs.filter(Ticket.status == status)
        if owner:
            qs = qs.filter(Ticket.owner == owner)
        since = period_start(period)
        if since:
            qs = qs.filter(Ticket.created_at >= since)
        if q:
            qs = qs.filter(Ticket.title.contains(q) | Ticket.requester.contains(q))
        return [{"id": t.id, "title": t.title, "status": t.status, "owner": t.owner, "rating": t.rating, "incident_id": t.incident_id,
                 "created_at": t.created_at.isoformat()} for t in qs.limit(200)]


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Muse ServiceDesk</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c1e21;--muted:#65676b;--line:#e4e6eb;--accent:#0866ff;--ok:#31a24c;--warn:#f7b928;--bad:#e41e3f}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#18191a;--card:#242526;--fg:#e4e6eb;--muted:#b0b3b8;--line:#3a3b3c}}
:root[data-theme=dark]{--bg:#18191a;--card:#242526;--fg:#e4e6eb;--muted:#b0b3b8;--line:#3a3b3c}
*{box-sizing:border-box}html{color-scheme:light dark}body{margin:0;font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
h3{margin:14px 0 8px;font-size:14px}small{color:var(--muted)}
.live{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--muted)}.live i{width:8px;height:8px;border-radius:50%;background:var(--ok);display:inline-block}
.ibtn{width:34px;height:34px;border-radius:8px;border:1px solid var(--line);background:transparent;color:var(--fg);cursor:pointer;display:grid;place-items:center;padding:0}
.ibtn svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.menu{position:relative}.avatar{width:36px;height:36px;border-radius:50%;border:0;background:var(--accent);color:#fff;font:600 13px/1 inherit;font-family:inherit;cursor:pointer;display:grid;place-items:center}
.dropdown{position:absolute;right:0;top:42px;min-width:230px;background:var(--card);border:1px solid var(--line);border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,.15);padding:6px;display:none;z-index:20}
.dropdown.on{display:block}.dropdown a{display:block;padding:8px 10px;border-radius:6px;color:var(--fg);text-decoration:none;font-size:13px}.dropdown a:hover{background:var(--bg)}
.dd-head{padding:8px 10px 10px}.dd-head b{display:block;font-size:13px}.dd-head small{color:var(--muted);font-size:12px}.dd-sep{height:1px;background:var(--line);margin:6px 0}
<!--RAIL_CSS-->
main{display:grid;gap:16px;padding:12px 16px 16px}main.dash{grid-template-columns:1fr 1fr 1fr}main.tix{grid-template-columns:minmax(360px,2fr) 3fr;align-items:start}
@media(max-width:1100px){main.dash{grid-template-columns:1fr 1fr}}@media(max-width:900px){main.dash,main.tix{grid-template-columns:1fr;padding:12px;gap:12px}}
[data-mode=dashboard] .only-tickets{display:none!important}[data-mode=tickets] .only-dash{display:none!important}
.tl{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.search{font:inherit;padding:6px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);min-width:220px}
.list .scroll{max-height:72vh;overflow:auto}tr.t.on{background:rgba(8,102,255,.12)}
.dcols{grid-template-columns:1fr 260px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;min-width:0}
.kpis{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}.kpi{padding:10px;border:1px solid var(--line);border-radius:8px}

.kpi b{display:block;font-size:22px}.kpi span{color:var(--muted);font-size:12px}
.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:6px 4px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:12px}tr.t{cursor:pointer}tr.t:hover{background:var(--bg)}td.title{max-width:52vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;color:#fff;white-space:nowrap}
.open{background:var(--accent)}.waiting_user{background:var(--warn);color:#000}.waiting_approval{background:#8a2be2}.resolved{background:var(--ok)}.escalated{background:#d9480f}
.bar{height:8px;background:var(--line);border-radius:4px;overflow:hidden}.bar i{display:block;height:100%;background:var(--accent)}
.tiers{display:flex;flex-wrap:wrap;gap:6px}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--card)}
.seg button{font:inherit;font-size:12px;padding:6px 10px;border:0;background:transparent;color:var(--muted);cursor:pointer}
.seg button.on{background:var(--accent);color:#fff}@media(max-width:600px){.seg button{padding:6px 7px}}
.toolbar{display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;padding:12px 16px 0}
.cards{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;padding:12px 16px 0}@media(max-width:900px){.cards{grid-template-columns:repeat(3,1fr);padding:12px 12px 0}}
.tcard{padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:inherit;font:inherit;text-align:left;cursor:pointer}
.tcard b{display:block;font-size:20px}.tcard span{color:var(--muted);font-size:12px}.tcard em{font-style:normal;font-size:11px;color:var(--muted)}
.tcard.on{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}.tcard:hover{background:var(--bg)}
.btn{font:inherit;font-size:12px;padding:5px 10px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}.btn:disabled{opacity:.5;cursor:default}
.thead{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap}.thead h3{margin:0}
.actions{display:flex;gap:6px;align-items:center;flex-wrap:wrap}
.link{font-size:11px;color:var(--muted)}td.sel{width:24px}input[type=checkbox]{accent-color:var(--accent)}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;place-items:center;z-index:9;padding:16px}.modal.on{display:grid}
.modal .box{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;width:min(520px,100%)}
.modal label{display:block;font-size:12px;color:var(--muted);margin:10px 0 4px}.modal input,.modal textarea,.modal select{width:100%;font:inherit;padding:7px 9px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
.modal .row{display:flex;justify-content:flex-end;gap:8px;margin-top:14px}
.dcols{display:grid;grid-template-columns:1fr 300px;gap:18px}@media(max-width:900px){.dcols{grid-template-columns:1fr}}
.timeline{max-height:70vh;overflow:auto;display:flex;flex-direction:column;gap:8px;padding:4px 2px}
.msg{max-width:78%;padding:9px 12px;border-radius:14px;font-size:13px;line-height:1.45;white-space:pre-wrap;overflow-wrap:anywhere}
.msg.user{align-self:flex-end;background:var(--accent);color:#fff;border-bottom-right-radius:4px}
.msg.agent{align-self:flex-start;background:var(--bg);border:1px solid var(--line);border-bottom-left-radius:4px}
.msg small{display:block;margin-top:4px;font-size:11px;opacity:.75}.msg.user small{color:#fff}
.sys{align-self:center;font-size:12px;color:var(--muted);text-align:center;padding:2px 10px}
.sys.handoff{align-self:flex-start;text-align:left;max-width:85%;border:1px dashed var(--line);border-radius:8px;padding:6px 10px}.sys.handoff b{color:var(--accent);font-weight:600}.sys.note{background:rgba(247,185,40,.14);color:inherit;border-radius:8px;padding:6px 10px;max-width:85%;align-self:flex-start;text-align:left}
.tool{align-self:flex-start;max-width:85%}.tool summary{list-style:none;cursor:pointer;display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);padding:3px 10px;border:1px solid var(--line);border-radius:999px;background:var(--card)}
.tool summary::-webkit-details-marker{display:none}.tool summary b{color:var(--fg);font-weight:600}.tool summary i{font-style:normal}.tool .ok{color:var(--ok)}.tool .err{color:var(--bad)}
.tool pre{margin:6px 0 0 8px;padding:8px 10px;background:var(--bg);border:1px solid var(--line);border-radius:8px;font-size:12px;white-space:pre-wrap;overflow-wrap:anywhere;max-height:260px;overflow:auto}
.meta{border-left:1px solid var(--line);padding-left:18px;font-size:13px}@media(max-width:900px){.meta{border-left:0;padding-left:0;border-top:1px solid var(--line);padding-top:10px}}
.meta dl{display:grid;grid-template-columns:auto 1fr;gap:6px 12px;margin:0}.meta dt{color:var(--muted)}.meta dd{margin:0;overflow-wrap:anywhere}
.meta h4{margin:14px 0 6px;font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.04em}
.sla{display:flex;flex-direction:column;gap:4px}.srow2{display:grid;grid-template-columns:1fr auto auto;gap:8px;align-items:baseline;padding:4px 8px;border-radius:6px;border-left:3px solid var(--line)}
.srow2.ok{border-left-color:var(--ok)}.srow2.bad{border-left-color:var(--bad);background:rgba(228,30,63,.08)}.srow2 b{font-variant-numeric:tabular-nums}.srow2 small{color:var(--muted)}.tagok{color:var(--ok)}.meta a{color:var(--accent);text-decoration:none}
.composer{display:flex;gap:8px;margin-top:10px;align-items:flex-end}.composer textarea{flex:1;font:inherit;padding:8px 10px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--fg);resize:vertical}
.msg.desk{align-self:flex-start;background:rgba(49,162,76,.14);border:1px solid rgba(49,162,76,.35);border-bottom-left-radius:4px}
.meta select{width:100%;font:inherit;padding:6px 8px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
.tool summary small,.sys small{color:var(--muted);font-weight:400;margin-left:4px}
</style></head><body class="app">
<!--RAIL--><div class="page" data-mode="<!--MODE-->">
<div class="toolbar"><div class="tl"><div class="seg" id="period"><button data-p="today">Today</button><button data-p="week">Week</button><button data-p="month">Month</button><button data-p="quarter">Quarter</button><button data-p="year">Year</button><button data-p="all" class="on">All</button></div>
<div class="seg only-tickets" id="statusSeg"><button data-s="" class="on">Any status</button><button data-s="open">Open</button><button data-s="waiting_user">Waiting</button><button data-s="waiting_approval">Approval</button><button data-s="escalated">Escalated</button><button data-s="resolved">Resolved</button></div>
<div class="seg only-tickets" id="ownerSeg"><button data-o="" class="on">Any tier</button><button data-o="L1">L1</button><button data-o="L2">L2</button><button data-o="L3">L3</button><button data-o="L4">L4</button><button data-o="human">Human</button></div>
<input class="search only-tickets" id="q" type="search" placeholder="Search title or requester…"></div>
<span class="live"><small id="scope"></small> · <i></i><span id="upd">live</span></span></div>
<div class="cards only-dash" id="cards"></div>
<main class="dash only-dash">
<section class="card"><div class="kpis" id="kpis"></div></section>
<section class="card"><h3 style="margin-top:0">Top issues</h3><div id="issues"></div></section>
<section class="card"><h3 style="margin-top:0">Tier load</h3><div class="tiers" id="tiers"></div><h3>Handoffs</h3><div id="handoffs" class="muted"></div></section>
</main>
<main class="tix only-tickets">
<section class="card list"><div class="thead"><h3 id="tt">Tickets</h3><div class="actions"><small id="selinfo"></small><button class="btn primary" id="mergeBtn" disabled onclick="openMerge()">Merge into problem</button></div></div>
<div class="scroll"><table><thead><tr><th class="sel"></th><th>#</th><th>Title</th><th>Status</th><th>Owner</th></tr></thead><tbody id="tickets"></tbody></table></div></section>
<section class="card detail" id="detail"><div class="thead"><h3 id="ttitle">Ticket</h3><div class="actions" id="tactions"></div></div>
<div class="dcols"><div><div id="trail" class="timeline"><small>Select a ticket to see its conversation and what the agents did.</small></div>
<form class="composer" id="composer" style="display:none" onsubmit="sendReply(event)"><textarea id="replyText" rows="2" placeholder="Write to the employee… (sent over WhatsApp)"></textarea><button class="btn primary" type="submit">Send</button></form></div>
<aside id="meta" class="meta"></aside></div></section>
</main></div>
<div class="modal" id="mergeModal"><div class="box"><h3>Merge into a problem record</h3><small id="mergeList"></small>
<label>Problem title</label><input id="mTitle" placeholder="e.g. Exchange connectivity outage">
<label>First update to everyone affected (optional)</label><textarea id="mUpdate" rows="2" placeholder="We are aware of the issue and working on it."></textarea>
<label>Owner</label><select id="mOwner"><option>L1</option><option>L2</option><option>L3</option><option>L4</option><option value="human">Human engineer</option></select>
<div class="row"><button class="btn" onclick="closeModal('mergeModal')">Cancel</button><button class="btn primary" onclick="doMerge()">Create problem</button></div></div></div>
<div class="modal" id="closeModal"><div class="box"><h3 id="cTitle">Close ticket</h3><p class="muted" style="margin:4px 0 0">The employee is told over WhatsApp.</p>
<label>Closing note</label><textarea id="cNote" rows="2" placeholder="e.g. resolved on site, lamp replaced"></textarea>
<div class="row"><button class="btn" onclick="closeModal('closeModal')">Cancel</button><button class="btn primary" onclick="doClose()">Close ticket</button></div></div></div>
<div class="modal" id="assignModal"><div class="box"><h3 id="aTitle">Assign ticket</h3>
<label>Assign to</label><select id="aOwner"><option>L1</option><option>L2</option><option>L3</option><option>L4</option><option value="human">Human engineer</option></select>
<div id="aWho"><label>Person</label><select id="aName"><option value="">— anyone on the desk —</option></select></div>
<label>Note to the assignee</label><textarea id="aNote" rows="2" placeholder="What should they do?"></textarea>
<div class="row"><button class="btn" onclick="closeModal('assignModal')">Cancel</button><button class="btn primary" id="aGo" onclick="doAssign()">Assign</button></div></div></div>
<script>
const $=s=>document.querySelector(s);let cur=null;
const LABELS={open:'open',waiting_user:'waiting for user',waiting_approval:'waiting for approval',resolved:'resolved',escalated:'escalated'};
const pill=s=>`<span class="pill ${s}">${LABELS[s]||s}</span>`;
const esc=x=>String(x).replace(/</g,'&lt;');
function toggleMenu(b){const m=$('#userMenu'),on=!m.classList.contains('on');m.classList.toggle('on',on);b.setAttribute('aria-expanded',on);}
document.addEventListener('click',e=>{const m=$('#userMenu');if(m&&!e.target.closest('.menu'))m.classList.remove('on');});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){const m=$('#userMenu');if(m)m.classList.remove('on');document.querySelectorAll('.modal.on').forEach(x=>x.classList.remove('on'));}});
(function(){const b=$('#theme');if(!b)return;const I={system:'<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>',
 light:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
 dark:'<svg viewBox="0 0 24 24"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>'};
 const O=['system','light','dark'];let cur='system';try{cur=localStorage.getItem('theme')||'system'}catch(e){}
 function ap(v){cur=v;try{localStorage.setItem('theme',v)}catch(e){}if(v==='system')document.documentElement.removeAttribute('data-theme');else document.documentElement.setAttribute('data-theme',v);b.innerHTML=I[v];b.title='Theme: '+v;}
 ap(O.includes(cur)?cur:'system');b.onclick=()=>ap(O[(O.indexOf(cur)+1)%3]);})();
(async function(){try{const us=await (await fetch('/users.json')).json();$('#aName').innerHTML='<option value="">— anyone on the desk —</option>'+us.map(u=>`<option>${esc(u.name)}</option>`).join('');}catch(e){}})();
const MODE=document.querySelector('.page').dataset.mode;const P=new URLSearchParams(location.search);
let period=P.get('period')||'all',owner=P.get('owner')||'',status=P.get('status')||'',q='';
function seg(id,attr,val,fn){document.querySelectorAll('#'+id+' button').forEach(b=>{b.classList.toggle('on',b.dataset[attr]===val);b.onclick=()=>{fn(b.dataset[attr]);document.querySelectorAll('#'+id+' button').forEach(x=>x.classList.toggle('on',x===b));load();};});}
seg('period','p',period,v=>period=v);seg('statusSeg','s',status,v=>status=v);seg('ownerSeg','o',owner,v=>owner=v);
const qEl=$('#q');if(qEl){let tmr;qEl.oninput=()=>{clearTimeout(tmr);tmr=setTimeout(()=>{q=qEl.value.trim();load();},250);}}
function pick(o){location.href='/tickets?period='+period+(o?'&owner='+o:'');}
const TIERS=[['','All tickets'],['L1','L1 · first contact'],['L2','L2 · devices & network'],['L3','L3 · servers & security'],['L4','L4 · vendors & logistics'],['human','Human engineer']];
async function load(){
 const qp='?period='+period+(owner?'&owner='+owner:'')+(status?'&status='+status:'')+(q?'&q='+encodeURIComponent(q):'');
 $('#scope').textContent=(period==='all'?'all time':'this '+period.replace('today','day'));
 if(MODE==='dashboard'){
  const s=await (await fetch('/stats?period='+period)).json();const st=s.by_status||{}, bo=s.by_owner||{}, ob=s.open_by_owner||{};
  $('#cards').innerHTML=TIERS.map(([k,label])=>{const n=k?(bo[k]||0):s.total,o=k?(ob[k]||0):(s.total-(st.resolved||0));
   return `<button class="tcard" onclick="pick('${k}')" title="Open in Tickets"><b>${n}</b><span>${label}</span><br><em>${o} open</em></button>`}).join('');
  $('#scope').textContent+=' · '+s.total+' tickets';
  $('#kpis').innerHTML=[['Total',s.total],['Open',(st.open||0)+(st.waiting_user||0)+(st.waiting_approval||0)],['Escalated',st.escalated||0],
   ['Auto-resolved',Math.round(s.auto_resolution_rate*100)+'%'],['Satisfaction',s.avg_rating?s.avg_rating+' / 5':'—'],['Ratings',s.ratings||0]]
   .map(([k,v])=>`<div class="kpi"><b>${v}</b><span>${k}</span></div>`).join('');
  const max=Math.max(1,...s.top_issues.map(x=>x[1]));
  $('#issues').innerHTML=s.top_issues.map(([k,v])=>`<div>${esc(k)} <small>${v}</small><div class="bar"><i style="width:${v/max*100}%"></i></div></div>`).join('')||'<small>No tickets in this period.</small>';
  const tc=s.tool_calls_by_tier||{};$('#tiers').innerHTML=['L1','L2','L3','L4'].map(t=>`<span class="pill open">${t}: ${tc[t]||0} tool calls</span>`).join('');
  const h=s.handoffs||{};$('#handoffs').innerHTML=Object.keys(h).length?Object.entries(h).map(([k,v])=>`${esc(k)} <b>${v}</b>`).join(' · '):'none';
 }else{
  const ts=await (await fetch('/tickets'+qp,{headers:{'x-requested-with':'fetch'}})).json();
  $('#tt').textContent='Tickets'+(owner?' — '+(TIERS.find(t=>t[0]===owner)||[])[1]:'');$('#scope').textContent+=' · '+ts.length+' tickets';
  $('#tickets').innerHTML=ts.map(t=>`<tr class="t ${cur===t.id?'on':''}" onclick="show(${t.id})"><td class="sel" onclick="event.stopPropagation()"><input type="checkbox" ${sel.has(t.id)?'checked':''} onchange="toggle(${t.id},this.checked)"></td><td>${t.id}</td><td class="title" title="${esc(t.title)}">${esc(t.title)}${t.incident_id?` <span class="link">→ #${t.incident_id}</span>`:''}</td><td>${pill(t.status)}</td><td>${esc(t.owner)}</td></tr>`).join('')||'<tr><td colspan="5"><small>No tickets match.</small></td></tr>';
  updateSel();if(cur)show(cur,true);else if(P.get('id')){show(+P.get('id'));}
 }
 $('#upd').textContent='updated '+new Date().toLocaleTimeString();
}
const sel=new Set();
function toggle(id,on){on?sel.add(id):sel.delete(id);updateSel();}
function updateSel(){$('#mergeBtn').disabled=sel.size<2;$('#selinfo').textContent=sel.size?sel.size+' selected':'';}
function closeModal(id){$('#'+id).classList.remove('on');}
function openMerge(){$('#mergeList').textContent='Tickets: '+[...sel].sort((a,b)=>a-b).map(i=>'#'+i).join(', ');$('#mergeModal').classList.add('on');$('#mTitle').focus();}
async function doMerge(){const title=$('#mTitle').value.trim();if(!title)return $('#mTitle').focus();
 const r=await fetch('/problems',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({ticket_ids:[...sel],title,update:$('#mUpdate').value.trim(),owner:$('#mOwner').value})});
 const j=await r.json();closeModal('mergeModal');sel.clear();$('#mTitle').value='';$('#mUpdate').value='';await load();if(j.problem_id)show(j.problem_id);}
function openAssign(id){$('#aTitle').textContent='Assign #'+id;$('#assignModal').dataset.id=id;$('#assignModal').classList.add('on');
 $('#aOwner').onchange=()=>{$('#aWho').style.display=$('#aOwner').value==='human'?'':'none'};$('#aOwner').onchange();}
async function doAssign(){const id=$('#assignModal').dataset.id;$('#aGo').disabled=true;$('#aGo').textContent='Assigning…';
 await fetch('/tickets/'+id+'/assign',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({owner:$('#aOwner').value,note:$('#aNote').value.trim(),assignee:$('#aName').value.trim()})});
 $('#aGo').disabled=false;$('#aGo').textContent='Assign';closeModal('assignModal');$('#aNote').value='';await load();show(id);}
const AGENT_LABEL={L1:'L1 · first contact',L2:'L2 · devices & network',L3:'L3 · servers & security',L4:'L4 · vendors & logistics'};
function fmt(x){try{const d=new Date(x);return d.toLocaleString([], {dateStyle:'medium',timeStyle:'short'})}catch(e){return x}}
function tstamp(x){try{return new Date(x).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}catch(e){return ''}}
function renderTimeline(events){
 const out=[];const calls=new Map();
 for(const e of events){
  if(e.kind==='message'){
   const who=e.actor==='user'?'user':(e.actor.startsWith('desk:')?'desk':'agent');
   const label=who==='user'?'employee':(who==='desk'?'desk · '+e.actor.slice(5):(AGENT_LABEL[e.actor]||e.actor));
   out.push(`<div class="msg ${who}">${esc(e.content)}<small>${esc(label)} · ${tstamp(e.at)}</small></div>`);
  }else if(e.kind==='tool_call'){
   calls.set(e.content+'|'+e.actor,{args:e.data,actor:e.actor,at:e.at});
   out.push(`<details class="tool" data-k="${esc(e.content+'|'+e.actor)}"><summary><i>${esc(e.actor)}</i> · <b>${esc(e.content)}</b> <span class="pending">…</span><small>${tstamp(e.at)}</small></summary><pre>${esc(JSON.stringify(e.data,null,1))}</pre></details>`);
  }else if(e.kind==='tool_result'){
   const ok=!(e.data&&e.data.ok===false);const k=e.content+'|'+e.actor;
   for(let i=out.length-1;i>=0;i--){if(out[i].startsWith(`<details class="tool" data-k="${esc(k)}"`)&&out[i].includes('class="pending"')){
    out[i]=out[i].replace('<span class="pending">…</span>',ok?'<span class="ok">✓</span>':'<span class="err">✗</span>').replace('</pre></details>',`</pre><pre>→ ${esc(JSON.stringify(e.data,null,1).slice(0,1500))}</pre></details>`);break;}}
  }else if(e.kind==='handoff'){const m=/^([A-Za-z0-9]+)\\s*->\\s*([A-Za-z0-9]+):\\s*([^]*)$/.exec(e.content)||[];
   out.push(`<div class="sys handoff"><b>${esc(m[1]||'')} → ${esc(m[2]||'')}</b> handoff<small>${tstamp(e.at)}</small><br>${esc(m[3]||e.content)}</div>`);}
  else if(e.kind==='note'){out.push(`<div class="sys note"><b>${esc(e.actor)}</b> · ${esc(e.content)}<small>${tstamp(e.at)}</small></div>`);}
  else{out.push(`<div class="sys">${esc(e.actor)} · ${esc(e.kind)} · ${esc(e.content)}<small>${tstamp(e.at)}</small></div>`);}
 }
 return out.join('');
}
function dur(sec){if(sec==null)return '—';sec=Math.round(sec);if(sec<60)return sec+'s';const m=Math.floor(sec/60),h=Math.floor(m/60),d=Math.floor(h/24);
 if(h<1)return m+'m';if(d<1)return h+'h '+(m%60)+'m';return d+'d '+(h%24)+'h';}
function slaRow(label,sec,target,breached){return `<div class="srow2 ${breached?'bad':(target?'ok':'')}"><span>${label}</span><b>${dur(sec)}</b><small>${target?'/ '+dur(target*60):''}</small></div>`;}
function renderTiming(tm){if(!tm)return '';
 let h='<h4>Timing &amp; SLA</h4><div class="sla">';
 h+=slaRow('First response',tm.first_response_seconds,tm.first_response_target_minutes,tm.first_response_breached);
 for(const x of tm.tiers)h+=slaRow((AGENT_LABEL[x.tier]||x.tier).split(' · ')[0]+' handling',x.seconds,x.target_minutes,x.breached);
 if(tm.waiting_for_employee_seconds)h+=slaRow('Waiting for employee',tm.waiting_for_employee_seconds,null,false);
 h+=slaRow(tm.resolved?'Total (resolved)':'Total (running)',tm.total_seconds,tm.total_target_minutes,tm.total_breached);
 return h+'</div>';}
async function show(id,quiet){cur=id;const t=await (await fetch('/tickets/'+id)).json();
 $('#ttitle').innerHTML=`#${t.id} ${esc(t.title)} ${pill(t.status)}`;
 $('#tactions').innerHTML=`<button class="btn" onclick="openAssign(${t.id})">Assign</button>`+(t.status!=='resolved'?` <button class="btn" onclick="closeTicket(${t.id})">Close</button>`:'');
 $('#meta').innerHTML=`<h4>Details</h4><dl><dt>Requester</dt><dd>${esc(t.requester||'')}</dd><dt>Status</dt><dd><select onchange="setStatus(${t.id},this.value)">${['open','waiting_user','waiting_approval','escalated','resolved'].map(x=>`<option value="${x}" ${x===t.status?'selected':''}>${LABELS[x]}</option>`).join('')}</select></dd><dt>Owner</dt><dd>${esc(AGENT_LABEL[t.owner]||t.owner||'')}</dd>
  <dt>Identity</dt><dd>${t.verified?'<span class="tagok">verified</span>':'not verified'}</dd><dt>Opened</dt><dd>${fmt(t.created_at)}</dd>
  ${t.rating?`<dt>Rating</dt><dd>${'★'.repeat(t.rating)}${'☆'.repeat(5-t.rating)}</dd>`:''}
  ${t.incident_id?`<dt>Problem</dt><dd><a href="#" onclick="show(${t.incident_id});return false">#${t.incident_id}</a></dd>`:''}
  ${t.linked&&t.linked.length?`<dt>Linked</dt><dd>${t.linked.map(i=>`<a href="#" onclick="show(${i});return false">#${i}</a>`).join(', ')}</dd>`:''}</dl>
  ${renderTiming(t.timing)}
  ${t.resolution?`<h4>Resolution</h4><div>${esc(t.resolution)}</div>`:''}
  <h4>Activity</h4><div class="muted">${t.events.filter(e=>e.kind==='tool_call').length} tool calls · ${t.events.filter(e=>e.kind==='handoff').length} handoffs · ${t.events.filter(e=>e.kind==='message').length} messages</div>`;
 $('#trail').innerHTML=renderTimeline(t.events);$('#composer').style.display='';
 document.querySelectorAll('tr.t').forEach(r=>r.classList.toggle('on',r.cells[1]&&+r.cells[1].textContent===t.id));
 if(!quiet){$('#trail').scrollTop=1e9;if(window.innerWidth<900)$('#detail').scrollIntoView({behavior:'smooth'});}}
async function sendReply(ev){ev.preventDefault();const text=$('#replyText').value.trim();if(!text||!cur)return;
 await fetch('/tickets/'+cur+'/reply',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});$('#replyText').value='';show(cur);}
async function setStatus(id,status){if(status==='resolved'){closeTicket(id);return;}
 await fetch('/tickets/'+id+'/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({status})});await load();show(id,true);}
function closeTicket(id){$('#cTitle').textContent='Close #'+id;$('#closeModal').dataset.id=id;$('#closeModal').classList.add('on');$('#cNote').focus();}
async function doClose(){const id=$('#closeModal').dataset.id;await fetch('/tickets/'+id+'/close',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({resolution:$('#cNote').value.trim()||'closed by the service desk'})});
 closeModal('closeModal');await load();show(id);}
load();setInterval(load,5000);
</script></body></html>"""
