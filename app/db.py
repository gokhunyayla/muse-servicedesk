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
    assignee: Mapped[str] = mapped_column(String(255), default="")   # desk user (name or e-mail) when a human holds it
    channel: Mapped[str] = mapped_column(String(16), default="whatsapp")   # where the employee last wrote from: whatsapp | web | email | api
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
    error: Mapped[str] = mapped_column(String(255), default="")   # set when delivery failed for good (not retried)


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
        if "charset" not in parsed.query:
            url = parsed.update_query_dict({"charset": "utf8mb4"})
    _engine = create_engine(url, pool_pre_ping=True)
    Base.metadata.create_all(_engine)
    _add_missing_columns(_engine)
    _utf8mb4(_engine)
    _Session = sessionmaker(_engine, expire_on_commit=False)
    from app import knowledge
    knowledge.seed_from_files()          # first run: load data/sops/*.md into the knowledge base


def _utf8mb4(engine) -> None:
    """MySQL/MariaDB tables made with a 3-byte charset cannot store emoji, which people and the desk both send:
    convert this database's own tables once. Nothing outside this database is touched."""
    if engine.dialect.name != "mysql":
        return
    with engine.begin() as conn:
        old = conn.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() "
                                "AND table_collation NOT LIKE 'utf8mb4%'")).scalars().all()
        for name in old:
            conn.execute(text(f"ALTER TABLE `{name}` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))


def _add_missing_columns(engine) -> None:
    """create_all never alters existing tables; add columns that newer models declare (simple, additive)."""
    from sqlalchemy import inspect
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(engine.dialect)}"
                if col.default is not None and getattr(col.default, "is_scalar", False):
                    d = col.default.arg
                    ddl += " DEFAULT " + (f"'{d}'" if isinstance(d, str) else ("1" if d is True else "0" if d is False else str(d)))
                conn.execute(text(ddl))


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


class Client(Base):
    """An employee who may use the support page: recognised from the directory or added by an admin.
    Signs in with a one-time code sent to the phone (WhatsApp) or e-mail; no password to manage."""
    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    employee_no: Mapped[str] = mapped_column(String(32), default="")
    name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str] = mapped_column(String(32), default="")
    email: Mapped[str] = mapped_column(String(255), default="")
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def addresses(self) -> list[str]:
        return [a for a in (self.phone, self.email) if a]


class ClientCode(Base):
    """One-time sign-in code for the support page (hashed, 10 minutes, single use)."""
    __tablename__ = "client_codes"

    id: Mapped[int] = mapped_column(primary_key=True)
    contact: Mapped[str] = mapped_column(String(255))
    code_hash: Mapped[str] = mapped_column(String(128))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    used: Mapped[bool] = mapped_column(default=False)
    attempts: Mapped[int] = mapped_column(default=0)


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


class Report(Base):
    """A generated desk report (daily / weekly / monthly) and whether it was e-mailed."""
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    kind: Mapped[str] = mapped_column(String(16))
    trigger: Mapped[str] = mapped_column(String(16), default="schedule")
    recipients: Mapped[str] = mapped_column(String(500), default="")
    subject: Mapped[str] = mapped_column(String(255), default="")
    narrative: Mapped[str] = mapped_column(Text, default="")
    html: Mapped[str] = mapped_column(Text, default="")
    numbers: Mapped[dict] = mapped_column(JSON, default=dict)
    sent: Mapped[bool] = mapped_column(default=False)
    error: Mapped[str] = mapped_column(String(300), default="")


class Sop(Base):
    """Knowledge base entry: a procedure a tier (or everyone, tier='shared') follows. Drafts wait for an
    admin; published ones are what agents search; retired ones stay for history."""
    __tablename__ = "sops"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True)     # stable id used in citations, e.g. L1/001-vpn
    tier: Mapped[str] = mapped_column(String(8))                    # L1..L4 | shared
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="published")   # draft | published | retired
    author: Mapped[str] = mapped_column(String(255), default="")  # agent tier, 'supervisor', or a desk user's e-mail
    source: Mapped[str] = mapped_column(String(32), default="human")       # seed | human | agent | supervisor
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)
    updated_by: Mapped[str] = mapped_column(String(255), default="")
    ticket_ids: Mapped[list] = mapped_column(JSON, default=list)
    sources: Mapped[list] = mapped_column(JSON, default=list)      # URLs the content was drawn from
    used: Mapped[int] = mapped_column(default=0)                   # how often it came up in searches


class SopVersion(Base):
    """Previous content of a SOP, kept on every edit."""
    __tablename__ = "sop_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    sop_id: Mapped[int] = mapped_column(ForeignKey("sops.id"))
    version: Mapped[int] = mapped_column()
    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text)
    saved_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    saved_by: Mapped[str] = mapped_column(String(255), default="")


class RefDoc(Base):
    """A reference policy / procedure uploaded by a desk user (PDF, DOCX, Markdown, text)."""
    __tablename__ = "ref_docs"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(255))
    filename: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(120), default="")
    size: Mapped[int] = mapped_column(default=0)
    scope: Mapped[str] = mapped_column(String(8), default="shared")     # shared | L1..L4
    kind: Mapped[str] = mapped_column(String(16), default="policy")     # policy | guideline | manual
    enforcement: Mapped[str] = mapped_column(String(16), default="strict")   # strict | flexible
    description: Mapped[str] = mapped_column(Text, default="")
    uploaded_by: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    chunks: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(16), default="active")   # active | removed


class RefChunk(Base):
    """A passage of a reference document; what search returns and agents cite as [REF:doc#n]."""
    __tablename__ = "ref_chunks"

    id: Mapped[int] = mapped_column(primary_key=True)
    doc_id: Mapped[int] = mapped_column(ForeignKey("ref_docs.id"))
    idx: Mapped[int] = mapped_column()
    page: Mapped[int | None] = mapped_column(nullable=True)
    heading: Mapped[str] = mapped_column(String(255), default="")
    text: Mapped[str] = mapped_column(Text)


class ChatThread(Base):
    """A desk user's conversation with the desk brain."""
    __tablename__ = "chat_threads"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(255), default="New conversation")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    thread_id: Mapped[int] = mapped_column(ForeignKey("chat_threads.id"))
    at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    role: Mapped[str] = mapped_column(String(16))          # user | assistant
    content: Mapped[str] = mapped_column(Text)
    actions: Mapped[list] = mapped_column(JSON, default=list)   # tool calls made while answering
    usage: Mapped[dict] = mapped_column(JSON, default=dict)     # tokens / latency / context share of this turn


class Connector(Base):
    """An MCP server the agents may use (Azure/M365, Cisco, a firewall, anything with an MCP endpoint)."""
    __tablename__ = "connectors"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)     # slug used in tool names: mcp.<name>.<tool>
    label: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(16))                   # stdio | http
    command: Mapped[str] = mapped_column(Text, default="")          # stdio: command line
    url: Mapped[str] = mapped_column(String(500), default="")       # http: endpoint
    secrets: Mapped[str] = mapped_column(Text, default="")          # encrypted JSON: env (stdio) / headers (http)
    tiers: Mapped[list] = mapped_column(JSON, default=list)         # which agents may call it
    enabled: Mapped[bool] = mapped_column(default=True)
    tools: Mapped[list] = mapped_column(JSON, default=list)         # cached tool list from the last successful test
    last_test_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class PeopleProcess(Base):
    """An onboarding or offboarding started by HR: the facts HR gave, the manager's answers, and what was done."""
    __tablename__ = "people_processes"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))                       # onboarding | offboarding
    status: Mapped[str] = mapped_column(String(24), default="waiting_manager")   # waiting_manager | scheduled | done | failed
    hr_contact: Mapped[str] = mapped_column(String(255))                # who started it (phone or e-mail)
    hr_ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), nullable=True)
    manager_no: Mapped[str] = mapped_column(String(255), default="")
    manager_contact: Mapped[str] = mapped_column(String(255), default="")
    manager_ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), nullable=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)              # name, start/last day, department, personal contact …
    answers: Mapped[dict] = mapped_column(JSON, default=dict)           # question key -> the manager's answer
    result: Mapped[dict] = mapped_column(JSON, default=dict)            # what was created / removed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class Device(Base):
    """A real machine running the Muse endpoint agent. It only ever calls out to the desk (heartbeat, job poll);
    the desk never connects in. Authenticated by a per-device token, stored hashed."""
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(primary_key=True)
    hostname: Mapped[str] = mapped_column(String(64), unique=True)
    owner: Mapped[str] = mapped_column(String(255), default="")        # employee number (or UPN) of the user
    token_hash: Mapped[str] = mapped_column(String(64), default="")
    enabled: Mapped[bool] = mapped_column(default=True)
    os: Mapped[str] = mapped_column(String(255), default="")
    agent_version: Mapped[str] = mapped_column(String(32), default="")
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)            # last heartbeat: disk, cpu, patch age, services …
    last_seen: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)


class DeviceJob(Base):
    """One allow-listed script the desk asked a device to run, and what came back."""
    __tablename__ = "device_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id"))
    script: Mapped[str] = mapped_column(String(64))
    args: Mapped[dict | None] = mapped_column(JSON, nullable=True)        # validated parameters (app-update: app, min_version)
    ticket_id: Mapped[int | None] = mapped_column(ForeignKey("tickets.id"), nullable=True)
    requested_by: Mapped[str] = mapped_column(String(64), default="")    # agent tier or desk user
    status: Mapped[str] = mapped_column(String(16), default="queued")    # queued | running | done | failed | expired
    ok: Mapped[bool | None] = mapped_column(nullable=True)
    output: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LlmCall(Base):
    """One request to Meta Model API: what it cost in tokens and time, and on whose behalf."""
    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    model: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(32), default="")      # ticket | chat | supervisor | report | other
    scope_id: Mapped[int | None] = mapped_column(nullable=True)     # ticket id / thread id / run id
    actor: Mapped[str] = mapped_column(String(32), default="")      # L1..L4, SUP, DESK, report
    prompt_tokens: Mapped[int] = mapped_column(default=0)
    completion_tokens: Mapped[int] = mapped_column(default=0)
    total_tokens: Mapped[int] = mapped_column(default=0)
    latency_ms: Mapped[int] = mapped_column(default=0)
    tool_calls: Mapped[int] = mapped_column(default=0)
    ok: Mapped[bool] = mapped_column(default=True)
