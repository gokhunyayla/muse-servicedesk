"""Microsoft Entra ID / Microsoft 365 behind the desk: the same directory tools and the same e-mail channel,
now acting on a real tenant through Graph. Graph itself is faked at the HTTP boundary (graph._call)."""
import pytest
from fastapi.testclient import TestClient

from app import channels, graph, systems, vault
from app.db import Event, Ticket, init_db, session
from app.sla import put_setting
from app.tools import Ctx, run_tool

ME = {"id": "u-1", "displayName": "Emma Johnson", "userPrincipalName": "emma.johnson@cloudrium.com", "mail": "emma.johnson@cloudrium.com",
      "mobilePhone": "+15551110001", "businessPhones": [], "employeeId": "E1001", "department": "Sales", "jobTitle": "Rep",
      "accountEnabled": True, "onPremisesExtensionAttributes": {}}
BOSS = {**ME, "id": "u-2", "displayName": "Liam Carter", "userPrincipalName": "liam.carter@cloudrium.com", "mail": "liam.carter@cloudrium.com",
        "mobilePhone": "+15551110002", "employeeId": "E1002"}


class FakeGraph:
    """Answers the handful of Graph calls the desk makes and records every write."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.mail: list[dict] = []
        self.groups = {"u-1": ["sales-tr"]}
        self.licenses = {"u-1": ["O365_BUSINESS_ESSENTIALS"]}

    def __call__(self, method, path, json=None, params=None, headers=None):
        self.calls.append((method, path, json))
        f = (params or {}).get("$filter", "")
        if path == "/users" and method == "GET":
            for u in (ME, BOSS):
                if u["employeeId"] in f or u["mobilePhone"] in f or u["mail"] in f:
                    return {"value": [u]}
            return {"value": []}
        if path.endswith("/manager"):
            return BOSS if path.startswith("/users/u-1") else {}
        if path.endswith("/memberOf/microsoft.graph.group"):
            uid = path.split("/")[2]
            return {"value": [{"id": f"g-{n}", "displayName": n} for n in self.groups.get(uid, [])]}
        if path.endswith("/licenseDetails"):
            return {"value": [{"skuPartNumber": s} for s in self.licenses.get(path.split("/")[2], [])]}
        if path == "/subscribedSkus":
            return {"value": [{"skuId": "sku-1", "skuPartNumber": "O365_BUSINESS_ESSENTIALS", "prepaidUnits": {"enabled": 25}, "consumedUnits": 3},
                              {"skuId": "sku-2", "skuPartNumber": "VISIO_PLAN1", "prepaidUnits": {"enabled": 5}, "consumedUnits": 5}]}
        if path.endswith("/assignLicense"):
            uid = path.split("/")[2]
            for a in json["addLicenses"]:
                self.licenses.setdefault(uid, []).append({"sku-1": "O365_BUSINESS_ESSENTIALS", "sku-2": "VISIO_PLAN1"}[a["skuId"]])
            return {}
        if path == "/groups":
            return {"value": [{"id": "g-finance", "displayName": "finance"}]} if "finance" in f else {"value": []}
        if path.endswith("/members/$ref"):
            self.groups.setdefault("u-1", []).append("finance")
            return {}
        if path.endswith("/revokeSignInSessions"):
            return {"value": True}
        if method == "PATCH":
            return {}
        if path.endswith("/sendMail"):
            self.mail.append(json["message"])
            return {}
        if path.endswith("/mailFolders/inbox/messages"):
            return {"value": self.inbox}
        if path == "/organization":
            return {"value": [{"displayName": "Cloudrium", "verifiedDomains": [{"name": "cloudrium.com", "isVerified": True}]}]}
        if path == "/users/$count":
            return 2
        if path.endswith("/mailFolders/inbox"):
            return {"totalItemCount": 1}
        raise AssertionError(f"unexpected Graph call {method} {path}")

    inbox: list = []


@pytest.fixture
def fake(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(vault, "KEY_FILE", tmp_path / "k")
    from app import security
    security._hits.clear()
    fg = FakeGraph()
    monkeypatch.setattr(graph, "_call", fg)
    put_setting("graph", {"enabled": True, "tenant": "t", "client_id": "c", "mailbox": "support@cloudrium.com", "domain": "cloudrium.com"})
    vault.set_many({"GRAPH_CLIENT_SECRET": "s3cret"})
    assert graph.enabled()
    return fg


def _ctx(db, requester="+15551110001", verified=False):
    t = Ticket(requester=requester, title="x", verified=verified)
    db.add(t)
    db.flush()
    return Ctx(db=db, ticket=t, agent="L1")


def test_directory_reads_come_from_the_tenant(fake):
    assert systems.user_by_contact("+15551110001")[0] == "E1001"
    rec = systems.get_user("E1001")
    assert rec["name"] == "Emma Johnson" and rec["manager"] == "E1002" and rec["groups"] == ["sales-tr"] and rec["source"] == "entra"
    assert systems.user_email("E1001") == "emma.johnson@cloudrium.com" and systems.get_user("E9999") is None
    with session() as db:
        r = run_tool(_ctx(db), "find_user", {"phone": "+15551110001"})
        assert r["ok"] and r["employee_no"] == "E1001"
        r = run_tool(_ctx(db), "get_employee", {"employee_no": "E1001"})
        assert r["ok"] and r["department"] == "Sales" and r["licenses"] == ["O365_BUSINESS_ESSENTIALS"]


def test_identity_is_the_sender_binding_when_the_tenant_has_no_birth_year(fake):
    with session() as db:
        # the real directory has no birth year: the number the employee writes from must be theirs
        assert run_tool(_ctx(db), "verify_identity", {"employee_no": "E1001", "birth_year": 1990})["ok"] is True
        r = run_tool(_ctx(db, requester="+15551110002"), "verify_identity", {"employee_no": "E1001", "birth_year": 1990})
        assert r["ok"] is False and "Liam Carter" in r["error"]


def test_password_reset_and_sessions_hit_graph_only_after_strong_verification(fake):
    with session() as db:
        ctx = _ctx(db, verified=True)
        assert run_tool(ctx, "reset_password", {"employee_no": "E1001"})["ok"] is False
        db.add(Event(ticket_id=ctx.ticket.id, actor="L1", kind="note", content="strong verification passed (code)"))
        db.flush()
        r = run_tool(ctx, "reset_password", {"employee_no": "E1001"})
        assert r["ok"] and r["temporary_password"].startswith("Tmp-") and r["must_change"]
        patch = [c for c in fake.calls if c[0] == "PATCH" and c[1] == "/users/u-1"]
        assert patch and patch[-1][2]["passwordProfile"]["forceChangePasswordNextSignIn"] is True
        assert patch[-1][2]["passwordProfile"]["password"] == r["temporary_password"]
        assert run_tool(ctx, "revoke_sessions", {"employee_no": "E1001"})["revoked"] is True
        assert ("POST", "/users/u-1/revokeSignInSessions", None) in fake.calls


def test_licences_and_groups_change_in_the_tenant(fake):
    with session() as db:
        ctx = _ctx(db)
        cat = run_tool(ctx, "list_licenses", {})["catalog"]
        assert set(cat) == {"O365_BUSINESS_ESSENTIALS", "VISIO_PLAN1"}        # the tenant's SKUs, desk approval policy
        r = run_tool(ctx, "assign_license", {"employee_no": "E1001", "license": "VISIO_PLAN1"})
        assert r["ok"] and r["licenses"] == ["O365_BUSINESS_ESSENTIALS", "VISIO_PLAN1"]
        assert run_tool(ctx, "assign_license", {"employee_no": "E1001", "license": "Nope"})["ok"] is False
        r = run_tool(ctx, "add_to_group", {"employee_no": "E1001", "group": "finance"})
        assert r["ok"] and "finance" in r["groups"]
        assert ("POST", "/groups/g-finance/members/$ref", {"@odata.id": f"{graph.BASE}/directoryObjects/u-1"}) in fake.calls
        assert run_tool(ctx, "disable_account", {"employee_no": "E1001"})["ok"]
        assert ("PATCH", "/users/u-1", {"accountEnabled": False}) in fake.calls


def test_email_channel_through_the_microsoft_365_mailbox(fake, desk, monkeypatch):
    from app.llm import Reply, ScriptedLLM, set_llm
    put_setting("email", {**channels.DEFAULT_EMAIL, "enabled": True, "provider": "graph", "poll_minutes": 1})
    fake.inbox = [{"id": "m1", "subject": "VPN bağlanmıyor", "from": {"emailAddress": {"address": "emma.johnson@cloudrium.com", "name": "Emma"}},
                   "receivedDateTime": "2026-10-28T12:00:05Z", "body": {"contentType": "html", "content": "<p>VPN açılmıyor</p><br><div>> eski alıntı</div>"},
                   "hasAttachments": False, "internetMessageId": "<m1@x>"}]
    assert channels.poll_email(force=True) == 0                    # first run only records the position, never the backlog
    assert graph.config()["last_received"]
    put_setting("graph", {**graph.config(), "last_received": "2026-10-28T12:00:00Z"})
    set_llm(ScriptedLLM([Reply(text="VPN için ağ bağlantınızı kontrol edelim.")]))
    assert channels.poll_email(force=True) == 1
    with session() as db:
        t = db.query(Ticket).one()
        assert t.requester == "emma.johnson@cloudrium.com" and t.channel == "email"
        first = db.query(Event).filter(Event.ticket_id == t.id).order_by(Event.id).first()
        assert "eski alıntı" not in (first.content or "")
    assert fake.mail and fake.mail[-1]["subject"].startswith("[#1] Re: VPN") and fake.mail[-1]["toRecipients"][0]["emailAddress"]["address"] == "emma.johnson@cloudrium.com"
    assert graph.config()["last_received"] == "2026-10-28T12:00:06Z"      # one second past it: Graph compares in ms
    fake.inbox[0]["receivedDateTime"] = "2026-10-28T12:00:06Z"           # even if the same message came back …
    assert channels.poll_email(force=True) == 0                            # … its id is remembered: never twice
    # outbound desk messages to that employee go the same way
    ok, err = channels.send_email("emma.johnson@cloudrium.com", "[#1] Re: VPN", "Merhaba")
    assert ok and len(fake.mail) == 2


def test_settings_page_saves_tenant_and_tests_the_connection(fake, desk, tmp_path):
    r = desk.post("/settings/graph", data={"enabled": "1", "tenant": "t2", "client_id": "c2", "mailbox": "", "domain": "",
                                           "GRAPH_CLIENT_SECRET": "new-secret", "action": "test"}, follow_redirects=False)
    assert r.status_code == 303 and "Connected" in r.headers["location"] and "Cloudrium" in r.headers["location"]
    assert graph.config()["tenant"] == "t2" and vault.get("GRAPH_CLIENT_SECRET") == "new-secret"
    page = desk.get("/settings?tab=connectors").text
    assert "Microsoft Entra ID" in page and "configured" in page and "new-secret" not in page
    page = desk.get("/settings?tab=email").text
    assert 'name="provider"' in page


def test_simulated_directory_stays_when_graph_is_off(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(vault, "KEY_FILE", tmp_path / "k")
    assert not graph.enabled()
    assert systems.get_user("E1001")["name"] == "Emma Johnson" and systems.license_catalog() is systems.LICENSE_CATALOG


def test_attribute_mapping_and_tenant_country_for_new_accounts(fake, desk):
    # the admin maps the employee number and birth year to extension attributes; nothing assumes a country
    desk.post("/settings/graph", data={"enabled": "1", "tenant": "t", "client_id": "c", "mailbox": "support@cloudrium.com",
                                       "domain": "cloudrium.com", "usage_location": "", "employee_attr": "extensionAttribute2",
                                       "birth_year_attr": "extensionAttribute3"}, follow_redirects=False)
    assert graph.config()["employee_attr"] == "extensionAttribute2" and graph.config()["usage_location"] == ""
    u = {**ME, "employeeId": None, "onPremisesExtensionAttributes": {"extensionAttribute2": "HR-77", "extensionAttribute3": "1990"}}
    assert graph._emp_no(u) == "HR-77" and graph._record(u)["birth_year"] == 1990
    created = {}

    def fake_call(method, path, json=None, params=None, headers=None):
        if path == "/organization":
            return {"value": [{"countryLetterCode": "GB"}]}
        if method == "POST" and path == "/users":
            created.update(json)
            return {"id": "u-new"}
        return fake(method, path, json, params, headers)
    graph._call = fake_call
    emp, upn, pw = graph.create_user("Noah Smith", "Sales", "", "")
    assert upn == "noah.smith@cloudrium.com" and emp == upn and "employeeId" not in created
    assert created["usageLocation"] == "GB"                       # the tenant's own country, not a fixed one
