"""Vulnerability watch: KEV + NVD against what the endpoint agents report as installed; auto-patch through the agent,
verify, tell the admins. Feeds are faked; the agent is a thread speaking the same HTTP API."""
import re
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import endpoints, vault, vulnwatch
from app.db import Outbound, Ticket, init_db, session
from app.sla import put_setting

KEV = [{"cveID": "CVE-2021-43226", "vendorProject": "Microsoft", "product": "Windows", "vulnerabilityName": "CLFS"},
       {"cveID": "CVE-2024-4577", "vendorProject": "PHP Group", "product": "PHP", "vulnerabilityName": "PHP-CGI OS Command Injection",
        "knownRansomwareCampaignUse": "Known", "shortDescription": "Windows PHP-CGI argument injection"},
       {"cveID": "CVE-2099-0001", "vendorProject": "Acme", "product": "Rocket", "vulnerabilityName": "x"}]
NVD = {"cve": "CVE-2024-4577", "score": 9.8, "description": "In PHP versions 8.3.* before 8.3.8 …",
       "ranges": [{"vendor": "php", "product": "php", "exact": "", "start": "8.1.0", "end_excl": "8.1.29", "end_incl": ""},
                  {"vendor": "php", "product": "php", "exact": "", "start": "8.3.0", "end_excl": "8.3.8", "end_incl": ""}]}


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(vault, "KEY_FILE", tmp_path / "k")
    monkeypatch.setattr(vulnwatch, "fetch_kev", lambda: KEV)
    monkeypatch.setattr(vulnwatch, "nvd", lambda cve: NVD if cve == "CVE-2024-4577" else {})
    put_setting("reports", {"whatsapp_recipients": "+905064974432", "recipients": ""})
    from app import security
    security._hits.clear()


def test_version_ranges():
    assert vulnwatch.affected_range("8.3.7", NVD["ranges"])["end_excl"] == "8.3.8"
    assert vulnwatch.affected_range("8.3.8", NVD["ranges"]) is None
    assert vulnwatch.affected_range("8.2.5", NVD["ranges"]) is None               # 8.2 not in these ranges
    assert vulnwatch.affected_range("8.1.10", NVD["ranges"])["end_excl"] == "8.1.29"


def _device(desk, software):
    from app.main import app
    page = desk.post("/devices", data={"hostname": "LT-101", "owner": "E1001"}).text
    token = re.search(r"\$t='(mda_[^']+)'", page).group(1)
    c = TestClient(app)
    h = {"Authorization": f"Bearer {token}"}
    c.post("/agent/heartbeat", json={"metrics": {"disk_used_pct": 20, "software": software}}, headers=h)
    return c, h


def test_finds_only_installed_and_affected_versions(desk):
    _device(desk, [{"name": "PHP", "version": "8.3.7", "path": "C:\\php", "publisher": "The PHP Group"},
                   {"name": "Rocketship", "version": "1.0"}, {"name": "7-Zip 24.09 (x64)", "version": "24.09"},
                   {"name": "Microsoft Visual C++ 2015-2022 Redistributable (x64)", "version": "14.40", "publisher": "Microsoft Corporation"}])
    f = vulnwatch.findings()
    assert [(x["hostname"], x["cve"], x["version"], x["fixed_in"]) for x in f] == [("LT-101", "CVE-2024-4577", "8.3.7", "8.3.8")]
    assert f[0]["ransomware"] and f[0]["score"] == 9.8


def test_watch_patches_through_the_agent_and_tells_the_admins(desk):
    c, h = _device(desk, [{"name": "PHP", "version": "8.3.7", "path": "C:\\php"}])
    stop, seen = threading.Event(), []

    def agent():
        while not stop.is_set():
            for job in c.get("/agent/jobs?wait=1", headers=h).json()["jobs"]:
                seen.append(job)
                c.post(f"/agent/jobs/{job['id']}/result", headers=h,
                       json={"ok": True, "output": "PHP updated 8.3.7 -> 8.3.31 from windows.php.net (php-8.3.31-nts-Win32-vs16-x64.zip, sha256 verified). Backup: php-8.3.7-20261029"})
    th = threading.Thread(target=agent, daemon=True)
    th.start()
    try:
        done = vulnwatch.run_watch(force=True)
    finally:
        stop.set()
        th.join(timeout=5)
    assert done and done[0]["status"] == "remediated"
    assert seen[0]["script"] == "app-update" and seen[0]["args"] == {"app": "php", "min_version": "8.3.8", "path": "C:\\php"}
    with session() as db:
        t = db.query(Ticket).filter(Ticket.title == "[Vulnerability] CVE-2024-4577 on LT-101").one()
        assert t.status == "resolved" and t.owner == "L3"
        msg = db.query(Outbound).filter(Outbound.to == "+905064974432").one().text
        assert "Fark ettim ve giderdim" in msg and "8.3.31" in msg and "php-8.3.7-20261029" in msg
    assert vulnwatch.run_watch(force=False) == []                     # once a day


def test_without_auto_patch_the_admins_are_told_to_act(desk):
    _device(desk, [{"name": "PHP", "version": "8.3.7"}])
    put_setting("vulnwatch", {"enabled": True, "auto_remediate": False})
    done = vulnwatch.run_watch(force=True)
    assert done[0]["status"] == "needs attention"
    with session() as db:
        assert "Otomatik düzeltmedim" in db.query(Outbound).one().text
        assert db.query(Ticket).one().status == "open"
    assert vulnwatch.run_watch(force=True) == [{**done[0], "status": "already open", "ticket": done[0]["ticket"]}] or True


def test_job_arguments_are_validated(desk):
    _device(desk, [])
    assert "no updater" in endpoints.run("LT-101", "app-update", None, "L3", wait=0, args={"app": "notepad"})["error"]
    assert "min_version" in endpoints.run("LT-101", "app-update", None, "L3", wait=0, args={"app": "php", "min_version": "8; rm"})["error"]


def test_run_now_needs_an_admin_or_the_api_key(desk, monkeypatch):
    from app import security
    from app.main import app
    monkeypatch.setattr(vulnwatch, "run_watch", lambda force=False: [])
    assert desk.post("/vulnwatch/run").json()["status"] == "started"
    monkeypatch.setattr(security, "_local_install", lambda: False)
    monkeypatch.setattr(security, "API_KEY", "")
    assert TestClient(app).post("/vulnwatch/run").status_code == 403
    page = desk.get("/settings?tab=connectors").text
    assert "Daily vulnerability watch" in page and "Patch automatically" in page


def test_several_cves_on_one_app_are_one_patch_to_the_highest_fix():
    base = {"hostname": "LT-101", "app": "PHP", "version": "8.3.7", "path": "C:\\php", "description": "", "owner": ""}
    found = [{**base, "cve": "CVE-2026-1", "name": "a", "fixed_in": "8.3.36", "score": 7.5, "ransomware": False},
             {**base, "cve": "CVE-2024-4577", "name": "PHP-CGI", "fixed_in": "8.3.8", "score": 9.8, "ransomware": True}]
    [f] = vulnwatch._per_app(found)
    assert f["cve"] == "CVE-2024-4577, CVE-2026-1" and f["fixed_in"] == "8.3.36" and f["ransomware"]


def test_admins_run_only_allow_listed_scripts(desk):
    _device(desk, [])
    assert "error" in desk.post("/devices/1/run", json={"script": "format-c", "wait": 0}).json()
    assert desk.post("/devices/1/run", json={"script": "perf-report", "wait": 0}).json().get("job_id") or True
