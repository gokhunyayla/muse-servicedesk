"""Runs due follow-ups. A confirmation reminder becomes an outbound message; any other follow-up
(stability check, cargo tracking) is handed to L1 as a scheduled task so it can act with its tools."""
from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta

from app.agents import AGENTS
from app.db import Followup, Outbound, Ticket, _now, log, session

logger = logging.getLogger(__name__)
AUTO_CLOSE_HOURS = 48


def run_due_followups() -> int:
    ran = 0
    with session() as db:
        due = db.query(Followup).filter(Followup.done.is_(False), Followup.due_at <= _now()).all()
        for f in due:
            ticket = db.get(Ticket, f.ticket_id)
            f.done = True
            ran += 1
            if ticket is None or ticket.status == "resolved":
                continue
            if f.note == "confirmation reminder":
                from app.i18n import t as tr_, ticket_lang
                db.add(Outbound(to=ticket.requester, ticket_id=ticket.id,
                                text=tr_("reminder", ticket_lang(ticket), id=ticket.id, resolution=ticket.resolution,
                                         hours=AUTO_CLOSE_HOURS)))
                log(db, ticket.id, "system", "note", "confirmation reminder sent")
                db.add(Followup(ticket_id=ticket.id, due_at=_now() + timedelta(hours=AUTO_CLOSE_HOURS),
                                note="auto-close if no reply"))
                continue
            if f.note == "auto-close if no reply":
                if ticket.status == "waiting_user":     # still unanswered: assume fixed, close, tell them
                    from app.db import resolve
                    resolve(ticket)
                    ticket.resolution = f"{ticket.resolution} (auto-closed: no reply from the employee)"
                    log(db, ticket.id, "system", "note", "auto-closed: no reply after reminder")
                    from app.i18n import t as tr_, ticket_lang
                    db.add(Outbound(to=ticket.requester, ticket_id=ticket.id,
                                    text=tr_("auto_closed", ticket_lang(ticket), id=ticket.id)))
                continue
            log(db, ticket.id, "system", "note", f"scheduled follow-up: {f.note}")
            answer = AGENTS["L1"].run(db, ticket, f"[Scheduled follow-up] {f.note}. Check and act; "
                                                  f"notify the employee only if there is something to say.")
            log(db, ticket.id, "L1", "message", answer)
    return ran


def start_background(interval_sec: int = 30, proactive_every_sec: int = 6 * 3600) -> threading.Thread:
    def loop():
        from app.proactive import run_proactive_checks
        last_proactive = 0.0
        while True:
            try:
                run_due_followups()
                from app.whatsapp import drain_outbound
                drain_outbound()
                if time.time() - last_proactive >= proactive_every_sec:
                    run_proactive_checks()
                    last_proactive = time.time()
                from app import supervisor
                from app.db import SupervisorRun
                with session() as db:
                    last = db.query(SupervisorRun).order_by(SupervisorRun.id.desc()).first()
                    is_due = supervisor.due(last)
                if is_due:
                    supervisor.run("schedule")
            except Exception:   # the scheduler must survive a failing run
                logger.exception("scheduled run failed")
            time.sleep(interval_sec)
    t = threading.Thread(target=loop, daemon=True, name="followups")
    t.start()
    return t
