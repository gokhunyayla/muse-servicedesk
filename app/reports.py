"""Daily / weekly / monthly desk reports: numbers from the ticket store, a narrative written by Muse Wonder,
delivered as an HTML e-mail to the people set under Settings → Reports. SMTP comes from the same settings
(falling back to .env)."""
from __future__ import annotations

import html
import logging
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from app.db import Report, Ticket, _now, session
from app.llm import ContentFiltered, get_llm
from app.sla import get_setting, put_setting, timing

logger = logging.getLogger(__name__)

DEFAULT_REPORTS = {
    "channel": "email",          # email | whatsapp | both
    "recipients": "",            # comma-separated e-mails
    "whatsapp_recipients": "",   # comma-separated phone numbers (+90...)
    "daily": False, "daily_at": "18:00",
    "weekly": False, "weekly_day": 1, "weekly_at": "09:00",     # 1 = Monday
    "monthly": False, "monthly_day": 1, "monthly_at": "09:00",
    "language": "en",            # en | tr
}
DEFAULT_SMTP = {"host": "", "port": 587, "user": "", "password": "", "from": "", "tls": True}


def config() -> dict:
    return get_setting("reports", DEFAULT_REPORTS)


def smtp_config() -> dict:
    from app.config import auth_settings
    env = {"host": auth_settings.smtp_host, "port": auth_settings.smtp_port, "user": auth_settings.smtp_user,
           "password": auth_settings.smtp_password, "from": auth_settings.smtp_from, "tls": True}
    saved = get_setting("smtp", {})
    return {**DEFAULT_SMTP, **{k: v for k, v in env.items() if v}, **{k: v for k, v in saved.items() if v not in ("", None)}}


# ---- numbers ---------------------------------------------------------------------------------

def _window(kind: str, now: datetime | None = None) -> tuple[datetime, datetime, str]:
    from app.dashboard import period_start
    now = now or _now()
    if kind == "daily":
        start = period_start("today")
        return start, now, "today"
    if kind == "weekly":
        return now - timedelta(days=7), now, "the last 7 days"
    return now - timedelta(days=30), now, "the last 30 days"


def numbers(kind: str) -> dict:
    from collections import Counter
    from app import supervisor
    from app.dashboard import CATEGORIES
    start, end, label = _window(kind)
    with session() as db:
        created = db.query(Ticket).filter(Ticket.created_at >= start).all()
        resolved = db.query(Ticket).filter(Ticket.resolved_at >= start).all()
        open_now = db.query(Ticket).filter(Ticket.status != "resolved").all()
        cats = Counter()
        for t in created:
            title = t.title.lower()
            cats[next((k for k, ws in CATEGORIES.items() if any(w in title for w in ws)), "other")] += 1
        by_owner = Counter(t.owner for t in created)
        ratings = [t.rating for t in resolved if t.rating]
        breaches, fr, totals, waits = 0, [], [], []
        for t in resolved:
            tm = timing(t, t.events)
            breaches += tm["total_breached"] or tm["first_response_breached"]
            if tm["first_response_seconds"] is not None:
                fr.append(tm["first_response_seconds"])
            totals.append(tm["total_seconds"] - tm["waiting_for_employee_seconds"])
        escalated = [t for t in open_now if t.status == "escalated"]
        stale = [t for t in open_now if t.events and (_now() - t.events[-1].at) > timedelta(hours=24)]
        sup = supervisor.stats(start)
        sup["last_run"] = sup["last_run"].isoformat() if sup["last_run"] else None
        return {
            "kind": kind, "label": label, "start": start.isoformat(), "end": end.isoformat(),
            "created": len(created), "resolved": len(resolved), "open_now": len(open_now),
            "auto_resolved": sum(1 for t in resolved if t.owner != "desk" and t.owner != "human"),
            "escalated_open": len(escalated), "stale_open": len(stale),
            "avg_rating": round(sum(ratings) / len(ratings), 2) if ratings else None, "ratings": len(ratings),
            "sla_breaches": breaches,
            "median_first_response_s": sorted(fr)[len(fr) // 2] if fr else None,
            "median_handling_s": sorted(totals)[len(totals) // 2] if totals else None,
            "top_issues": cats.most_common(5), "by_owner": dict(by_owner),
            "supervisor": sup,
            "escalated_titles": [t.title[:80] for t in escalated[:5]],
            "stale_titles": [f"#{t.id} {t.title[:60]}" for t in stale[:5]],
        }


# ---- narrative -------------------------------------------------------------------------------

NARRATIVE = """You write the {kind} report of an autonomous IT service desk for its manager. Language: {lang}.
Write 3 short paragraphs in plain text (no markdown): 1) the headline numbers and what changed, 2) what went
wrong or needs a decision (escalations, SLA breaches, stale tickets, what the Supervisor learned or asked for),
3) one or two concrete recommendations. Be specific, use the numbers, no fluff.

Numbers (JSON): {numbers}"""


def narrative(n: dict, lang: str) -> str:
    import json
    from app import usage
    with usage.scope("report", None, "report"):
        return _narrative(n, lang, json)


def _narrative(n: dict, lang: str, json) -> str:
    try:
        reply = get_llm().chat([{"role": "user", "content": NARRATIVE.format(
            kind=n["kind"], lang="Turkish" if lang == "tr" else "English", numbers=json.dumps(n, ensure_ascii=False))}],
            [], heavy=True)
        return reply.text.strip()
    except ContentFiltered:
        return "(narrative unavailable: model request filtered)"


def _dur(sec):
    if sec is None:
        return "—"
    m = int(sec // 60)
    return f"{m // 60}h {m % 60}m" if m >= 60 else f"{m}m"


def render_html(n: dict, text: str, lang: str) -> str:
    L = (lambda en, tr: tr if lang == "tr" else en)
    kpis = [(L("Created", "Açılan"), n["created"]), (L("Resolved", "Çözülen"), n["resolved"]),
            (L("Open now", "Şu an açık"), n["open_now"]), (L("Auto-resolved", "Otomatik çözülen"), n["auto_resolved"]),
            (L("Escalated (open)", "İnsana aktarılan"), n["escalated_open"]), (L("SLA breaches", "SLA aşımı"), n["sla_breaches"]),
            (L("Satisfaction", "Memnuniyet"), f'{n["avg_rating"]} / 5' if n["avg_rating"] else "—"),
            (L("Median first response", "Medyan ilk yanıt"), _dur(n["median_first_response_s"])),
            (L("Median handling", "Medyan çözüm süresi"), _dur(n["median_handling_s"]))]
    cells = "".join(f'<td style="padding:10px 14px;border:1px solid #e4e6eb;border-radius:8px"><div style="font-size:22px;font-weight:700">{html.escape(str(v))}</div><div style="font-size:12px;color:#65676b">{html.escape(k)}</div></td>' for k, v in kpis)
    rows = lambda items: "".join(f"<li>{html.escape(str(i))}</li>" for i in items) or "<li>—</li>"
    s = n["supervisor"]
    title = {"daily": L("Daily desk report", "Günlük masa raporu"), "weekly": L("Weekly desk report", "Haftalık masa raporu"),
             "monthly": L("Monthly desk report", "Aylık masa raporu")}[n["kind"]]
    paras = "".join(f"<p style='margin:0 0 10px;line-height:1.5'>{html.escape(p.strip())}</p>" for p in text.split("\n") if p.strip())
    return f"""<div style="font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;color:#1c1e21;max-width:720px">
<h2 style="margin:0 0 4px">Muse ServiceDesk · {html.escape(title)}</h2>
<div style="color:#65676b;margin-bottom:16px">{html.escape(n['label'])} · {html.escape(n['end'][:16].replace('T', ' '))} UTC</div>
<table cellspacing="6" style="border-collapse:separate;margin-bottom:16px"><tr>{cells[:len(cells)]}</tr></table>
{paras}
<h3 style="font-size:14px;margin:16px 0 6px">{L('Top issues', 'En sık konular')}</h3><ul>{rows(f'{k}: {v}' for k, v in n['top_issues'])}</ul>
<h3 style="font-size:14px;margin:16px 0 6px">{L('Needs a human', 'İnsan bekleyen')}</h3><ul>{rows(n['escalated_titles'])}</ul>
<h3 style="font-size:14px;margin:16px 0 6px">{L('Stale (no activity 24h+)', 'Hareketsiz (24 saat+)')}</h3><ul>{rows(n['stale_titles'])}</ul>
<h3 style="font-size:14px;margin:16px 0 6px">Supervisor</h3>
<p style="margin:0">{L('Runs', 'Çalışma')}: {s['runs']} · {L('problems merged', 'birleştirilen problem')}: {s['problems']} · {L('SOPs drafted/published', 'SOP taslak/yayın')}: {s['sops_drafted']}/{s['sops_published']} · {L('human asks', 'insan talebi')}: {s['human_asks']}</p>
<p style="color:#65676b;font-size:12px;margin-top:20px">Muse ServiceDesk — written by Muse Wonder from the desk's own data.</p></div>"""


# ---- sending ---------------------------------------------------------------------------------

def send_html(to: list[str], subject: str, html_body: str, text_body: str) -> tuple[bool, str]:
    c = smtp_config()
    if not c["host"]:
        return False, "SMTP not configured (Settings → Reports)"
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = c["from"] or c["user"], ", ".join(to), subject
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    try:
        with smtplib.SMTP(c["host"], int(c["port"]), timeout=25) as s:
            if c.get("tls", True):
                s.starttls()
            if c["user"]:
                s.login(c["user"], c["password"])
            s.send_message(msg)
        return True, ""
    except Exception as e:
        return False, str(e)[:300]


def render_text(n: dict, text: str, lang: str, subject: str) -> str:
    """Compact WhatsApp version: headline numbers + the narrative."""
    L = (lambda en, tr: tr if lang == "tr" else en)
    s = n["supervisor"]
    head = (f"*{subject}*\n"
            f"{L('Created', 'Açılan')} {n['created']} · {L('resolved', 'çözülen')} {n['resolved']} · {L('open', 'açık')} {n['open_now']} · "
            f"{L('escalated', 'insana aktarılan')} {n['escalated_open']} · SLA {L('breaches', 'aşımı')} {n['sla_breaches']}\n"
            f"{L('Satisfaction', 'Memnuniyet')} {n['avg_rating'] or '—'}/5 · {L('first response', 'ilk yanıt')} {_dur(n['median_first_response_s'])} · "
            f"{L('handling', 'çözüm')} {_dur(n['median_handling_s'])}\n"
            f"Supervisor: {s['runs']} {L('runs', 'tur')}, {s['problems']} {L('problems', 'problem')}, {s['sops_published']}/{s['sops_drafted']} SOP, {s['human_asks']} {L('human asks', 'insan talebi')}\n\n")
    return (head + text)[:3900]


def build_and_send(kind: str, trigger: str = "schedule", to: list[str] | None = None) -> Report:
    cfg = config()
    lang = cfg.get("language", "en")
    channel = cfg.get("channel", "email")
    emails = [x.strip() for x in cfg.get("recipients", "").split(",") if x.strip()]
    phones = [x.strip() for x in cfg.get("whatsapp_recipients", "").split(",") if x.strip()]
    if to:                                   # manual override: route by shape
        emails = [x for x in to if "@" in x]
        phones = [x for x in to if "@" not in x]
        channel = "both"
    n = numbers(kind)
    text = narrative(n, lang)
    body = render_html(n, text, lang)
    subject = {"daily": "Daily", "weekly": "Weekly", "monthly": "Monthly"}[kind] + f" desk report · {n['created']} new, {n['resolved']} resolved, {n['open_now']} open"
    results, delivered = [], []
    if channel in ("email", "both"):
        if emails:
            ok, err = send_html(emails, subject, body, text)
            results.append("" if ok else f"email: {err}")
            if ok:
                delivered += emails
        else:
            results.append("email: no recipients")
    if channel in ("whatsapp", "both"):
        from app.whatsapp import SendError, send
        msg = render_text(n, text, lang, subject)
        if not phones:
            results.append("whatsapp: no recipients")
        for p in phones:
            try:
                if send(p, msg):
                    delivered.append(p)
                else:
                    results.append(f"whatsapp {p}: channel not configured")
            except SendError as e:
                hint = " (outside the 24h window a message template is required)" if e.code in (131047, 131026) else ""
                results.append(f"whatsapp {p}: {e}{hint}")
    ok = bool(delivered)
    err = "; ".join(x for x in results if x)[:300]
    with session() as db:
        r = Report(kind=kind, trigger=trigger, recipients=", ".join(delivered or emails + phones), subject=subject, html=body,
                   narrative=text, numbers=n, sent=ok, error=err)
        db.add(r)
        db.flush()
        db.refresh(r)
        return r


# ---- schedule --------------------------------------------------------------------------------

def due_kinds(now_local: datetime | None = None) -> list[str]:
    """Which reports are due at this minute (desk time zone). The scheduler calls this every tick and
    de-duplicates with the reports table (one per kind per day)."""
    import os
    cfg = config()
    tz = ZoneInfo(os.getenv("DESK_TZ", "Europe/Istanbul"))
    now = now_local or datetime.now(tz)
    hm = now.strftime("%H:%M")
    out = []
    if cfg.get("daily") and hm == cfg.get("daily_at", "18:00"):
        out.append("daily")
    if cfg.get("weekly") and now.isoweekday() == int(cfg.get("weekly_day", 1)) and hm == cfg.get("weekly_at", "09:00"):
        out.append("weekly")
    if cfg.get("monthly") and now.day == int(cfg.get("monthly_day", 1)) and hm == cfg.get("monthly_at", "09:00"):
        out.append("monthly")
    return out


def run_due() -> list[str]:
    sent = []
    for kind in due_kinds():
        with session() as db:
            today = _now().replace(hour=0, minute=0, second=0, microsecond=0)
            if db.query(Report).filter(Report.kind == kind, Report.trigger == "schedule", Report.created_at >= today).count():
                continue
        build_and_send(kind, "schedule")
        sent.append(kind)
    return sent
