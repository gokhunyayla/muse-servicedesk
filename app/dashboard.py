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
            cat = next((k for k, words in CATEGORIES.items() if any(w in title for w in words)), "diğer")
            cats[cat] += 1
        return {
            "total": len(tickets), "by_status": dict(by_status), "last_7_days": len(recent),
            "auto_resolution_rate": round(len(resolved) / len(tickets), 2) if tickets else 0,
            "avg_rating": round(sum(rated) / len(rated), 2) if rated else None, "ratings": len(rated),
            "top_issues": cats.most_common(6), "handoffs": dict(handoffs),
            "tool_calls_by_tier": dict(tool_calls),
        }


CATEGORIES = {
    "vpn / ağ": ["vpn", "wifi", "wi-fi", "ağ", "internet", "dns"],
    "hesap / şifre": ["şifre", "sifre", "password", "hesap", "mfa", "kilit"],
    "donanım": ["mavi ekran", "bsod", "laptop", "donanım", "açılmıyor", "kargo"],
    "güvenlik": ["phishing", "oltalama", "fidye", "virüs", "zararlı", "cve", "açık", "compliance"],
    "lisans / yazılım": ["lisans", "license", "adobe", "visio", "program", "uygulama"],
    "e-posta": ["outlook", "e-posta", "mail", "incident"],
    "yazıcı": ["yazıcı", "printer"],
    "proaktif": ["proactive"],
}


@router.get("/", response_class=HTMLResponse)
def page() -> str:
    return PAGE


PAGE = """<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><title>Muse ServiceDesk</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c1e21;--muted:#65676b;--line:#e4e6eb;--accent:#0866ff;--ok:#31a24c;--warn:#f7b928;--bad:#e41e3f}
@media(prefers-color-scheme:dark){:root{--bg:#18191a;--card:#242526;--fg:#e4e6eb;--muted:#b0b3b8;--line:#3a3b3c}}
*{box-sizing:border-box}body{margin:0;font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
header{padding:14px 20px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center}
h1{font-size:17px;margin:0}small{color:var(--muted)}
main{display:grid;grid-template-columns:1fr 1fr;gap:16px;padding:16px 20px}
@media(max-width:900px){main{grid-template-columns:1fr}}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.kpi{padding:10px;border:1px solid var(--line);border-radius:8px}
.kpi b{display:block;font-size:22px}.kpi span{color:var(--muted);font-size:12px}
table{width:100%;border-collapse:collapse}td,th{text-align:left;padding:6px 4px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:500;font-size:12px}tr.t{cursor:pointer}tr.t:hover{background:var(--bg)}
.pill{display:inline-block;padding:1px 8px;border-radius:999px;font-size:12px;color:#fff}
.open{background:var(--accent)}.waiting_user{background:var(--warn);color:#000}.waiting_approval{background:#8a2be2}.resolved{background:var(--ok)}.escalated{background:#d9480f}
button.close{margin-left:8px;padding:2px 10px;border-radius:6px;border:1px solid var(--border);background:transparent;color:inherit;cursor:pointer;font-size:12px}
.bar{height:8px;background:var(--line);border-radius:4px;overflow:hidden}.bar i{display:block;height:100%;background:var(--accent)}
#trail{max-height:70vh;overflow:auto}.ev{padding:5px 0;border-bottom:1px dashed var(--line);font-size:13px}
.ev b{color:var(--accent)}.ev.user b{color:var(--ok)}.ev.handoff b{color:var(--bad)}.ev code{font-size:12px;color:var(--muted);white-space:pre-wrap}
</style></head><body>
<header><h1>Muse ServiceDesk <small>— yönetici panosu</small></h1><small id="upd"></small></header>
<main>
<section class="card"><div class="kpis" id="kpis"></div><h3>En sık konular</h3><div id="issues"></div>
<h3>Seviye yükü</h3><div id="tiers"></div></section>
<section class="card"><h3>Biletler</h3><table><thead><tr><th>#</th><th>Başlık</th><th>Durum</th><th>Sahip</th><th>Puan</th></tr></thead><tbody id="tickets"></tbody></table></section>
<section class="card" style="grid-column:1/-1"><h3 id="ttitle">Bilet izi</h3><div id="trail"><small>Bir bilete tıklayın.</small></div></section>
</main>
<script>
const $=s=>document.querySelector(s);let cur=null;
async function load(){
 const s=await (await fetch('/stats')).json(), ts=await (await fetch('/tickets')).json();
 const st=s.by_status||{};
 $('#kpis').innerHTML=[['Toplam',s.total],['Açık',(st.open||0)+(st.waiting_user||0)+(st.waiting_approval||0)],['İnsana aktarılan',st.escalated||0],
  ['Otomatik çözüm',Math.round(s.auto_resolution_rate*100)+'%'],['Memnuniyet',s.avg_rating?s.avg_rating+' / 5':'—']]
  .map(([k,v])=>`<div class="kpi"><b>${v}</b><span>${k}</span></div>`).join('');
 const max=Math.max(1,...s.top_issues.map(x=>x[1]));
 $('#issues').innerHTML=s.top_issues.map(([k,v])=>`<div>${k} <small>${v}</small><div class="bar"><i style="width:${v/max*100}%"></i></div></div>`).join('');
 const tc=s.tool_calls_by_tier||{};$('#tiers').innerHTML=['L1','L2','L3','L4'].map(t=>`<span class="pill open" style="margin-right:6px">${t}: ${tc[t]||0} araç çağrısı</span>`).join('');
 $('#tickets').innerHTML=ts.map(t=>`<tr class="t" onclick="show(${t.id})"><td>${t.id}</td><td>${t.title}</td><td><span class="pill ${t.status}">${t.status}</span></td><td>${t.owner}</td><td>${t.rating||''}</td></tr>`).join('');
 $('#upd').textContent='güncellendi '+new Date().toLocaleTimeString();
 if(cur)show(cur,true);
}
async function show(id,quiet){cur=id;const t=await (await fetch('/tickets/'+id)).json();
 $('#ttitle').innerHTML=`#${t.id} ${t.title.replace(/</g,'&lt;')} — <span class="pill ${t.status}">${t.status}</span>`+
  (t.status!=='resolved'?` <button class="close" onclick="closeTicket(${t.id})">Kapat</button>`:'');
 $('#trail').innerHTML=t.events.map(e=>`<div class="ev ${e.actor==='user'?'user':''} ${e.kind}"><b>${e.actor}</b> <small>${e.kind}</small> ${e.content.replace(/</g,'&lt;')}${e.data?`<br><code>${JSON.stringify(e.data).slice(0,300).replace(/</g,'&lt;')}</code>`:''}</div>`).join('');
 if(!quiet)$('#trail').scrollTop=1e9;}
async function closeTicket(id){const r=prompt('Kapanış notu (çalışana iletilir):','servis masası tarafından kapatıldı');
 if(r===null)return;await fetch('/tickets/'+id+'/close',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({resolution:r})});load();}
load();setInterval(load,5000);
</script></body></html>"""
