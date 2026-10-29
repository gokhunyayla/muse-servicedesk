"""Phishing response against a tenant: the Graph client is a fake with two mailboxes holding the same mail."""
import pytest

from app import channels, phishing, systems, vault
from app.db import Outbound, init_db, session
from app.sla import put_setting


class FakeGraph:
    def __init__(self):
        self.boxes = {"emma@cloudrium.com": {"m1": "inbox"}, "liam@cloudrium.com": {"m2": "inbox"}}

    def tenant_domains(self):
        return ["cloudrium.com"]

    def find_mail(self, sender, subject=""):
        people = {"emma@cloudrium.com": ("E1001", "Emma", "+15551110001", True), "liam@cloudrium.com": ("E1002", "Liam", "", False)}
        return [{"mailbox": b, "employee_no": people[b][0], "name": people[b][1], "phone": people[b][2], "id": i,
                 "subject": "Invoice overdue", "read": people[b][3], "received": "", "message_id": "<x>"}
                for b, msgs in self.boxes.items() for i, f in msgs.items() if f == "inbox"]

    def inspect_mail(self, box, mid):
        return {"from": "billing@cloudriurn.com", "reply_to": ["pay@evil.example"], "auth": {"spf": "fail", "dkim": "none", "dmarc": "fail"},
                "links": [{"href": "https://cloudriurn-pay.com/login", "shown": "https://cloudrium.com/invoices"}], "attachments": False,
                "text": "Your invoice is overdue. Pay within 24 hours or your account is suspended."}

    def move_mail(self, box, mid, folder):
        self.boxes[box].pop(mid)
        new = mid + "-" + folder
        self.boxes[box][new] = folder
        return new


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(vault, "KEY_FILE", tmp_path / "k")
    g = FakeGraph()
    monkeypatch.setattr(systems, "_graph", lambda: g)
    put_setting("reports", {"whatsapp_recipients": "+905064974432", "recipients": "", "language": "en"})
    return g


def test_analysis_reads_the_real_mail(fresh):
    r = phishing.analyze("billing@cloudriurn.com", "Invoice", "", reporter="+15551110001")
    assert r["verdict"] == "phishing" and r["found_in_mailboxes"] == 2 and r["opened_by"] == ["Emma"]
    s = " | ".join(r["signals"])
    assert "SPF, DMARC" in s and "imitates cloudrium.com" in s and "link shows cloudrium.com but opens cloudriurn-pay.com" in s
    assert "replies go elsewhere" in s


def test_sweep_moves_every_copy_tells_people_and_can_be_undone(fresh):
    r = phishing.sweep(7, "billing@cloudriurn.com", "Invoice", reporter="+15551110001")
    assert r["mailboxes_affected"] == 2 and r["opened_by"] == ["Emma"]
    assert all(f == "deleteditems" for b in fresh.boxes.values() for f in b.values())
    with session() as db:
        to = sorted(o.to for o in db.query(Outbound))
        assert to == ["+905064974432", "liam@cloudrium.com"]                 # admin + the other recipient, not the reporter
        assert "I noticed and fixed it" in db.query(Outbound).filter(Outbound.to == "+905064974432").one().text
    assert phishing.restore(7)["restored"] == 2
    assert all(f == "inbox" for b in fresh.boxes.values() for f in b.values())
    assert phishing.restore(7)["ok"] is False


def test_a_blocked_sender_no_longer_reaches_the_desk():
    phishing.block("@cloudriurn.com")
    assert not channels.sender_allowed("billing@cloudriurn.com") and channels.sender_allowed("emma@cloudrium.com")
    assert phishing.verdict([]) == "safe"


def test_look_alike_domains():
    ours = ["cloudrium.com", "gokhunyaylagmail.onmicrosoft.com"]
    for fake in ("cloudriurn.com", "cloudriurn-billing.com", "cloudrium-pay.net", "login.cloudrium.secure-site.com"):
        assert phishing._lookalike(fake, ours) == "cloudrium.com", fake
    for fine in ("cloudrium.com", "mail.cloudrium.com", "gmail.com", "microsoft.com", "example.org"):
        assert phishing._lookalike(fine, ours) == "", fine
    info = {"from": "gokhun.yayla@gmail.com", "auth": {"spf": "pass"}, "subject": "Invoice overdue",
            "links": [{"href": "http://cloudriurn-billing.com/login", "shown": "http://cloudriurn-billing.com/login"}],
            "text": "Pay within 24 hours or your account will be suspended. Login"}
    assert phishing.verdict(phishing.signals(info, ours)) == "phishing"
