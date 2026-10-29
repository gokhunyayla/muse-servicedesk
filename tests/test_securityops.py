import pytest

from app import systems
from app.agents import AGENTS, handle_message
from app.db import init_db, session
from app.llm import Reply, ScriptedLLM, ToolCall, set_llm


@pytest.fixture(autouse=True)
def fresh_state(tmp_path):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    yield
    systems._isolated.clear()
    systems._blocked_senders.clear()
    systems._quarantined.clear()


def call(_name, **args):
    return ToolCall(id=f"c-{_name}", name=_name, arguments=args)


PHISH = dict(sender="it-support@micros0ft-login.com", subject="ACİL: Hesabınız askıya alınacak",
             body="24 saat içinde şifrenizi doğrulayın: https://micros0ft-login.com/verify")


def test_email_analyzer_verdicts():
    assert systems.analyze_email(**PHISH)["verdict"] == "phishing"
    assert systems.analyze_email("hr@company.com", "Bordro", "Ekte bu ayın bordrosu.")["verdict"] == "safe"
    assert systems.analyze_email("promo@shop.example", "Acil kampanya", "Bugün son gün!")["verdict"] == "suspicious"


def test_phishing_triage_quarantines_tenant_wide_and_blocks_sender():
    set_llm(ScriptedLLM([
        Reply(tool_calls=[call("analyze_email", **PHISH)]),
        Reply(tool_calls=[call("quarantine_email", subject=PHISH["subject"], sender=PHISH["sender"]),
                          call("block_sender", sender=PHISH["sender"])]),
        Reply(text="Oltalama. Tüm kutulardan kaldırdım, göndereni engelledim. Tıkladıysanız söyleyin."),
    ]))
    with session() as db:
        t, answer = handle_message(db, "+15551110001", "bu mail gerçek mi?")
        res = dict((e.content, e.data) for e in t.events if e.kind == "tool_result")
    assert res["analyze_email"]["verdict"] == "phishing"
    assert res["quarantine_email"]["mailboxes_affected"] == 17 and res["quarantine_email"]["tenant_wide"]
    assert PHISH["sender"] in res["block_sender"].get("blocked", []), res["block_sender"]


def test_malware_response_isolates_first_then_scans_and_releases():
    set_llm(ScriptedLLM([
        Reply(tool_calls=[call("handoff_L2", summary="LT-101 fidye notu görüyor")]),
        Reply(tool_calls=[call("isolate_device", hostname="LT-101")]),                 # L2
        Reply(tool_calls=[call("collect_evidence", hostname="LT-101")]),
        Reply(tool_calls=[call("run_malware_scan", hostname="LT-101")]),
        Reply(tool_calls=[call("release_device", hostname="LT-101")]),
        Reply(text="Temizlendi, serbest bırakıldı."),
        Reply(text="Cihazınız temizlendi ve ağa geri alındı."),
    ]))
    with session() as db:
        t, _ = handle_message(db, "+15551110001", "ekranda dosyalarınız şifrelendi yazıyor!")
        l2 = [(e.content, e.data) for e in t.events if e.kind == "tool_result" and e.actor == "L2"]
    order = [c for c, _ in l2]
    assert order.index("isolate_device") < order.index("run_malware_scan") < order.index("release_device")
    d = dict(l2)
    assert d["isolate_device"]["isolated"] is True
    assert d["collect_evidence"]["suspicious_processes"][0]["name"] == "svch0st.exe"
    assert d["run_malware_scan"]["findings"][0]["action"] == "quarantined"
    assert "LT-101" not in systems._isolated


def test_l1_cannot_isolate_devices():
    assert "isolate_device" not in {s["function"]["name"] for s in AGENTS["L1"].tool_specs()}
