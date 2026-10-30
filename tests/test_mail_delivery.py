"""Outgoing mail must never lose a sign-in code silently: Graph failures fall back to SMTP and stay visible."""
import pytest

from app import channels
from app.db import init_db, session


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    from app import vault
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(vault, "KEY_FILE", tmp_path / "k")


def test_graph_send_failure_falls_back_to_smtp_and_keeps_the_error(monkeypatch):
    """A tenant that refuses sendMail must not swallow a sign-in code: SMTP carries it, the Graph error stays in the log."""
    import smtplib
    from app import graph
    from app.sla import put_setting
    put_setting("email", {**channels.email_config(), "provider": "graph"})
    monkeypatch.setattr(graph, "send_mail", lambda to, subject, text: (_ for _ in ()).throw(RuntimeError("403 ErrorAccessDenied")))
    sent = []

    class Smtp:
        def __init__(self, host, port, timeout=0): sent.append(("connect", host, port))
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): sent.append(("starttls",))
        def login(self, u, p): sent.append(("login", u))
        def send_message(self, msg): sent.append(("mail", msg["To"], msg["Subject"]))
    monkeypatch.setattr(smtplib, "SMTP", Smtp)
    from app import reports
    monkeypatch.setattr(reports, "smtp_config", lambda: {"host": "smtp.test", "port": 587, "user": "desk@test", "password": "x", "from": "desk@test", "tls": True})
    ok, err = channels.send_email("grace@example.com", "Your sign-in code", "123456")
    assert ok and err == "" and ("mail", "grace@example.com", "Your sign-in code") in sent
    # without SMTP the failure is reported, not hidden
    monkeypatch.setattr(reports, "smtp_config", lambda: {"host": "", "port": 587, "user": "", "password": "", "from": "", "tls": True})
    ok, err = channels.send_email("grace@example.com", "Your sign-in code", "123456")
    assert not ok and "Microsoft 365: 403 ErrorAccessDenied" in err


def test_support_page_says_when_the_code_could_not_be_sent(monkeypatch):
    from app import support
    from app.db import Audit
    from app import whatsapp
    monkeypatch.setattr(whatsapp, "try_send", lambda to, text: False)
    reply, who = support.identify({"mode": "anon"}, "+1 555 111 0001")
    assert "could not send the code" in reply and "work e-mail" in reply and who.get("stage") != "ask_code"
    with session() as db:
        a = db.query(Audit).filter(Audit.action == "support.code_failed").one()
        assert a.target == "+15551110001" and "WhatsApp" in a.detail
