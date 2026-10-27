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
    events: Mapped[list["Event"]] = relationship(back_populates="ticket", order_by="Event.id")


class Followup(Base):
    """Scheduled work on a ticket: confirmation reminders, stability checks, cargo tracking."""
    __tablename__ = "followups"

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("tickets.id"))
    due_at: Mapped[datetime] = mapped_column(DateTime)
    note: Mapped[str] = mapped_column(Text)
    done: Mapped[bool] = mapped_column(default=False)


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
