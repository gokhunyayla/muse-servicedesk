from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import reports
from app.agents import handle_message
from app.db import Report, init_db, session
from app.llm import Reply, ScriptedLLM, ToolCall, set_llm
from app.sla import put_setting


@pytest.fixture(autouse=True)
def fresh(tmp_path):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")


def test_numbers_narrative_and_send(monkeypatch):
    set_llm(ScriptedLLM([Reply(tool_calls=[ToolCall("c", "close_ticket", {"resolution": "ok"})]), Reply(text="kapandı"),
                         Reply(text="Bakıyorum.")]))
    with session() as db:
        handle_message(db, "+905551110001", "vpn bağlanmıyor")
        handle_message(db, "+905551110002", "yazıcı çalışmıyor")
    n = reports.numbers("daily")
    assert n["created"] == 2 and n["resolved"] == 1 and n["open_now"] == 1 and n["top_issues"][0][0] in ("vpn / network", "printer")
    llm = ScriptedLLM([Reply(text="Two tickets today. One VPN fix. Keep an eye on the printer.")])
    set_llm(llm)
    sent = {}
    monkeypatch.setattr(reports, "send_html", lambda to, subject, html, text: sent.update(to=to, subject=subject, html=html) or (True, ""))
    put_setting("reports", {**reports.DEFAULT_REPORTS, "recipients": "boss@test, ciso@test"})
    r = reports.build_and_send("daily", "manual")
    assert r.sent and r.kind == "daily" and sent["to"] == ["boss@test", "ciso@test"]
    assert "Daily desk report" in sent["html"] and "Two tickets today" in sent["html"] and llm.heavy_calls == 1
    with session() as db:
        assert db.query(Report).count() == 1


def test_no_smtp_is_reported_not_raised():
    set_llm(ScriptedLLM([Reply(text="narrative")]))
    put_setting("reports", {**reports.DEFAULT_REPORTS, "recipients": "x@test"})
    put_setting("smtp", {})
    import os
    for k in ("SMTP_HOST",):
        os.environ.pop(k, None)
    r = reports.build_and_send("weekly", "manual")
    assert r.sent is False and "SMTP" in r.error and "Weekly" in r.subject


def test_schedule_due_kinds():
    put_setting("reports", {**reports.DEFAULT_REPORTS, "daily": True, "daily_at": "18:00", "weekly": True, "weekly_day": 1, "weekly_at": "09:00"})
    tz = ZoneInfo("Europe/Istanbul")
    assert reports.due_kinds(datetime(2026, 10, 27, 18, 0, tzinfo=tz)) == ["daily"]           # Tuesday 18:00
    assert reports.due_kinds(datetime(2026, 10, 26, 9, 0, tzinfo=tz)) == ["weekly"]           # Monday 09:00
    assert reports.due_kinds(datetime(2026, 10, 27, 9, 0, tzinfo=tz)) == []


def test_settings_tab_and_manual_send_endpoint(desk, monkeypatch):
    monkeypatch.setattr(reports, "send_html", lambda *a, **k: (True, ""))
    set_llm(ScriptedLLM([Reply(text="ok")]))
    r = desk.post("/settings/reports", data={"recipients": "a@test, b@test", "language": "tr", "daily": "1", "daily_at": "17:30",
                                              "smtp_host": "smtp.test", "smtp_port": "587", "smtp_user": "u", "smtp_password": "p", "smtp_from": "desk@test", "smtp_tls": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert reports.config()["recipients"] == "a@test, b@test" and reports.config()["daily_at"] == "17:30"
    assert reports.smtp_config()["host"] == "smtp.test" and reports.smtp_config()["password"] == "p"
    out = desk.post("/reports/send", json={"kind": "daily", "to": "me@test"}).json()
    assert out["sent"] is True and desk.get("/reports").json()[0]["recipients"] == "me@test"
    assert "SMTP" in desk.get("/settings?tab=reports").text and desk.get(f"/reports/{out['id']}").status_code == 200
