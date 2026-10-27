"""Manager dashboard: queues, auto-resolution rate, satisfaction, top issues, per-tier load, and a
live view of any ticket's audit trail. One HTML page that polls the JSON endpoints below."""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.db import Event, Ticket, _now, session

router = APIRouter()


@router.get("/stats")
def stats() -> dict:
    with session() as db:
        tickets = db.query(Ticket).all()
        by_status = Counter(t.status for t in tickets)
        resolved = [t for t in tickets if t.status == "resolved"]
        rated = [t.rating for t in resolved if t.rating]
        since = _now() - timedelta(days=7)
        recent = [t for t in tickets if t.created_at >= since]
        handoffs = Counter()
        for e in db.query(Event).filter(Event.kind == "handoff"):
            handoffs[e.content.split(":")[0].strip()] += 1
        tool_calls = Counter(e.actor for e in db.query(Event).filter(Event.kind == "tool_call"))
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
            "tool_calls_by_tier": dict(tool_calls),
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


@router.get("/", response_class=HTMLResponse)
def page() -> str:
    return PAGE


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Muse ServiceDesk</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c1e21;--muted:#65676b;--line:#e4e6eb;--accent:#0866ff;--ok:#31a24c;--warn:#f7b928;--bad:#e41e3f}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#18191a;--card:#242526;--fg:#e4e6eb;--muted:#b0b3b8;--line:#3a3b3c}}
:root[data-theme=dark]{--bg:#18191a;--card:#242526;--fg:#e4e6eb;--muted:#b0b3b8;--line:#3a3b3c}
*{box-sizing:border-box}html{color-scheme:light dark}body{margin:0;font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
header{position:sticky;top:0;z-index:5;background:var(--card);padding:10px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:10px}
.brand{display:flex;align-items:center;gap:10px;min-width:0}.logo{width:32px;height:32px;border-radius:9px;background:var(--accent);display:grid;place-items:center;flex:none}
.logo svg{width:20px;height:20px;stroke:#fff;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
h1{font-size:16px;margin:0;line-height:1.1;white-space:nowrap}h1 small{display:block;font-size:11px;font-weight:400}h3{margin:14px 0 8px;font-size:14px}small{color:var(--muted)}
.right{display:flex;align-items:center;gap:8px}.live{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--muted)}
.live i{width:8px;height:8px;border-radius:50%;background:var(--ok);display:inline-block}
@media(max-width:600px){.live span{display:none}}
.ibtn{width:34px;height:34px;border-radius:8px;border:1px solid var(--line);background:transparent;color:var(--fg);cursor:pointer;display:grid;place-items:center;padding:0}
.ibtn:hover{background:var(--bg)}.ibtn svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
main{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:16px}
@media(max-width:900px){main{grid-template-columns:1fr;padding:12px;gap:12px}}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;min-width:0}
.kpis{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}.kpi{padding:10px;border:1px solid var(--line);border-radius:8px}
@media(max-width:900px){.kpis{grid-template-columns:repeat(2,1fr)}}
.kpi b{display:block;font-size:22px}.kpi span{color:var(--muted);font-size:12px}
.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:6px 4px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:12px}tr.t{cursor:pointer}tr.t:hover{background:var(--bg)}td.title{max-width:52vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;color:#fff;white-space:nowrap}
.open{background:var(--accent)}.waiting_user{background:var(--warn);color:#000}.waiting_approval{background:#8a2be2}.resolved{background:var(--ok)}.escalated{background:#d9480f}
button.close{margin-left:8px;padding:2px 10px;border-radius:6px;border:1px solid var(--line);background:transparent;color:inherit;cursor:pointer;font-size:12px}
.bar{height:8px;background:var(--line);border-radius:4px;overflow:hidden}.bar i{display:block;height:100%;background:var(--accent)}
.tiers{display:flex;flex-wrap:wrap;gap:6px}
#trail{max-height:70vh;overflow:auto}.ev{padding:5px 0;border-bottom:1px dashed var(--line);font-size:13px;overflow-wrap:anywhere}
.ev b{color:var(--accent)}.ev.user b{color:var(--ok)}.ev.handoff b{color:var(--bad)}.ev code{font-size:12px;color:var(--muted);white-space:pre-wrap}
</style></head><body>
<header>
<div class="brand"><div class="logo"><svg viewBox="0 0 24 24"><path d="M4 5h16v10H9l-5 4z"/><path d="M8 9h8M8 12h5"/></svg></div>
<h1>Muse ServiceDesk<small>Service desk console</small></h1></div>
<div class="right"><span class="live"><i></i><span id="upd">live</span></span>
<button class="ibtn" id="theme" title="Theme: system" aria-label="Toggle theme"></button></div></header>
<main>
<section class="card"><div class="kpis" id="kpis"></div><h3>Top issues</h3><div id="issues"></div>
<h3>Tier load</h3><div class="tiers" id="tiers"></div></section>
<section class="card"><h3>Tickets</h3><div class="scroll"><table><thead><tr><th>#</th><th>Title</th><th>Status</th><th>Owner</th><th>Rating</th></tr></thead><tbody id="tickets"></tbody></table></div></section>
<section class="card" style="grid-column:1/-1"><h3 id="ttitle">Ticket trail</h3><div id="trail"><small>Select a ticket.</small></div></section>
</main>
<script>
const $=s=>document.querySelector(s);let cur=null;
const LABELS={open:'open',waiting_user:'waiting for user',waiting_approval:'waiting for approval',resolved:'resolved',escalated:'escalated'};
const pill=s=>`<span class="pill ${s}">${LABELS[s]||s}</span>`;
const esc=x=>String(x).replace(/</g,'&lt;');
const ICONS={system:'<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>',
 light:'<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
 dark:'<svg viewBox="0 0 24 24"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>'};
const ORDER=['system','light','dark'];let theme='system';
function applyTheme(v){theme=v;try{localStorage.setItem('theme',v)}catch(e){}
 if(v==='system')document.documentElement.removeAttribute('data-theme');else document.documentElement.setAttribute('data-theme',v);
 $('#theme').innerHTML=ICONS[v];$('#theme').title='Theme: '+v+' (click to change)';}
(function(){let v='system';try{v=localStorage.getItem('theme')||'system'}catch(e){}applyTheme(ORDER.includes(v)?v:'system');
 $('#theme').onclick=()=>applyTheme(ORDER[(ORDER.indexOf(theme)+1)%3]);})();
async function load(){
 const s=await (await fetch('/stats')).json(), ts=await (await fetch('/tickets')).json();
 const st=s.by_status||{};
 $('#kpis').innerHTML=[['Total',s.total],['Open',(st.open||0)+(st.waiting_user||0)+(st.waiting_approval||0)],['Escalated',st.escalated||0],
  ['Auto-resolved',Math.round(s.auto_resolution_rate*100)+'%'],['Satisfaction',s.avg_rating?s.avg_rating+' / 5':'—']]
  .map(([k,v])=>`<div class="kpi"><b>${v}</b><span>${k}</span></div>`).join('');
 const max=Math.max(1,...s.top_issues.map(x=>x[1]));
 $('#issues').innerHTML=s.top_issues.map(([k,v])=>`<div>${esc(k)} <small>${v}</small><div class="bar"><i style="width:${v/max*100}%"></i></div></div>`).join('');
 const tc=s.tool_calls_by_tier||{};$('#tiers').innerHTML=['L1','L2','L3','L4'].map(t=>`<span class="pill open">${t}: ${tc[t]||0} tool calls</span>`).join('');
 $('#tickets').innerHTML=ts.map(t=>`<tr class="t" onclick="show(${t.id})"><td>${t.id}</td><td class="title" title="${esc(t.title)}">${esc(t.title)}</td><td>${pill(t.status)}</td><td>${esc(t.owner)}</td><td>${t.rating||''}</td></tr>`).join('');
 $('#upd').textContent='updated '+new Date().toLocaleTimeString();
 if(cur)show(cur,true);
}
async function show(id,quiet){cur=id;const t=await (await fetch('/tickets/'+id)).json();
 $('#ttitle').innerHTML=`#${t.id} ${esc(t.title)} — ${pill(t.status)}`+
  (t.status!=='resolved'?` <button class="close" onclick="closeTicket(${t.id})">Close</button>`:'');
 $('#trail').innerHTML=t.events.map(e=>`<div class="ev ${e.actor==='user'?'user':''} ${e.kind}"><b>${esc(e.actor)}</b> <small>${e.kind}</small> ${esc(e.content)}${e.data?`<br><code>${esc(JSON.stringify(e.data).slice(0,300))}</code>`:''}</div>`).join('');
 if(!quiet){$('#trail').scrollTop=1e9;if(window.innerWidth<900)$('#ttitle').scrollIntoView({behavior:'smooth'});}}
async function closeTicket(id){const r=prompt('Closing note (sent to the employee):','closed by the service desk');
 if(r===null)return;await fetch('/tickets/'+id+'/close',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({resolution:r})});load();}
load();setInterval(load,5000);
</script></body></html>"""
