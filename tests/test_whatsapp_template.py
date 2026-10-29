"""Outside WhatsApp's 24-hour window the same desk text goes out inside the approved utility template."""
import httpx
import pytest

from app import vault, whatsapp
from app.db import init_db


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(vault, "KEY_FILE", tmp_path / "k")
    monkeypatch.setattr(whatsapp, "_cfg", lambda: ("tok", "123", "v"))


def test_falls_back_to_the_template_outside_the_window(monkeypatch):
    sent = []

    def post(url, headers=None, timeout=None, json=None):
        sent.append(json)
        if json["type"] == "text":
            return httpx.Response(400, json={"error": {"code": 131047, "message": "Re-engagement message"}})
        return httpx.Response(200, json={"messages": [{"id": "x"}]})
    monkeypatch.setattr(whatsapp.httpx, "post", post)
    assert whatsapp.send("+905064974432", "🔒 Patched PHP.\n\nBackup kept.\tTicket #97")
    t = sent[-1]["template"]
    assert t["name"] == "it_desk_update"
    assert t["components"][0]["parameters"][0]["text"] == "🔒 Patched PHP. · Backup kept. Ticket #97"


def test_other_errors_still_raise(monkeypatch):
    monkeypatch.setattr(whatsapp.httpx, "post", lambda *a, **k: httpx.Response(400, json={"error": {"code": 131026, "message": "not a WhatsApp user"}}))
    with pytest.raises(whatsapp.SendError) as e:
        whatsapp.send("+10000000000", "hi")
    assert e.value.permanent


def test_privacy_page_is_public():
    from fastapi.testclient import TestClient
    from app.main import app
    r = TestClient(app).get("/privacy")
    assert r.status_code == 200 and "privacy notice" in r.text
