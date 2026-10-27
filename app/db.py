"""Ticket store: every ticket keeps a full audit trail of messages, tool calls and handoffs."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from app.config import settings


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Ticket(Base):
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    requester: Mapped[str] = mapped_column(String(64))          # phone number or employee no
    title: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), default="open")   # open | waiting_user | resolved
    owner: Mapped[str] = mapped_column(String(8), default="L1")
    verified: Mapped[bool] = mapped_column(default=False)        # identity checked in this ticket
    resolution: Mapped[str] = mapped_column(Text, default="")
    rating: Mapped[int | None] = mapped_column(nullable=True)    # 1..5 from the employee
    incident_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), nullable=True)  # parent, for mass outages
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    events: Mapped[list["Event"]] = relationship(back_populates="ticket", order_by="Event.id")


class Followup(Base):
    """Scheduled work on a ticket: confirmation reminders, stability checks, cargo tracking."""
    __tablename__ = "followups"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))
    due_at: Mapped[datetime] = mapped_column(DateTime)
    note: Mapped[str] = mapped_column(Text)
    done: Mapped[bool] = mapped_column(default=False)


class Approval(Base):
    """A decision the desk needs from a human (usually the requester's manager)."""
    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))
    approver: Mapped[str] = mapped_column(String(16))    # employee no
    request: Mapped[str] = mapped_column(Text)
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)   # approved | denied
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Outbound(Base):
    """Messages the desk sends on its own (not as a reply): reminders, approvals, incident updates."""
    __tablename__ = "outbound"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    to: Mapped[str] = mapped_column(String(64))       # phone or employee no
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    sent: Mapped[bool] = mapped_column(default=False)  # flipped by the channel (WhatsApp) once delivered


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))
    at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    actor: Mapped[str] = mapped_column(String(16))     # user | L1..L4
    kind: Mapped[str] = mapped_column(String(16))      # message | tool_call | tool_result | handoff | note
    content: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    ticket: Mapped[Ticket] = relationship(back_populates="events")


_engine = None
_Session = None


def init_db(url: str | None = None) -> None:
    global _engine, _Session
    url = url or settings.database_url
    parsed = make_url(url)
    if parsed.get_backend_name() == "mysql" and parsed.database:
        server = create_engine(parsed.set(database=""))
        with server.connect() as conn:
            conn.execute(text(f"CREATE DATABASE IF NOT EXISTS `{parsed.database}` CHARACTER SET utf8mb4"))
        server.dispose()
    _engine = create_engine(url, pool_pre_ping=True)
    Base.metadata.create_all(_engine)
    _Session = sessionmaker(_engine, expire_on_commit=False)


@contextmanager
def session():
    if _Session is None:
        init_db()
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def log(db, ticket_id: int, actor: str, kind: str, content: str = "", data: dict | None = None) -> None:
    db.add(Event(ticket_id=ticket_id, actor=actor, kind=kind, content=content, data=data))
    db.flush()


class User(Base):
    """A person who may sign in to the service desk console: an admin or a support agent."""
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="agent")     # admin | agent
    active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    reset_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reset_expires: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Session(Base):
    """A signed-in browser session (HttpOnly cookie carries the token)."""
    __tablename__ = "sessions"

    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class Audit(Base):
    """What a signed-in desk user did in the console (sign-ins, assignments, merges, user admin)."""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    user: Mapped[str] = mapped_column(String(255))       # e-mail (or "anonymous")
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(255), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(String(64), default="")


def audit(db, user: str, action: str, target: str = "", detail: str = "", ip: str = "") -> None:
    db.add(Audit(user=user, action=action, target=str(target), detail=detail[:2000], ip=ip))


class Setting(Base):
    """Console settings as JSON documents keyed by section (sla, channels, ...)."""
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


def resolve(ticket) -> None:
    """Mark a ticket resolved and stamp the time (used by every path that closes a ticket)."""
    ticket.status = "resolved"
    ticket.resolved_at = _now()


class SupervisorRun(Base):
    """One pass of the Supervisor agent over the desk."""
    __tablename__ = "supervisor_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    trigger: Mapped[str] = mapped_column(String(16), default="schedule")   # schedule | manual
    summary: Mapped[str] = mapped_column(Text, default="")
    findings: Mapped[list] = mapped_column(JSON, default=list)             # what it noticed
    actions: Mapped[list] = mapped_column(JSON, default=list)              # what it did (tool calls)
    error: Mapped[str] = mapped_column(Text, default="")


class SopDraft(Base):
    """A SOP the Supervisor wrote from research; an admin publishes or rejects it."""
    __tablename__ = "sop_drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    tier: Mapped[str] = mapped_column(String(8))
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    sources: Mapped[list] = mapped_column(JSON, default=list)
    reason: Mapped[str] = mapped_column(Text, default="")        # the gap it fills
    ticket_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="pending")   # pending | published | rejected
    decided_by: Mapped[str] = mapped_column(String(255), default="")
    sop_id: Mapped[str] = mapped_column(String(128), default="")
