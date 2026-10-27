import io

import pytest

from app import references
from app.db import init_db, session
from app.llm import Reply, ScriptedLLM, ToolCall, set_llm


@pytest.fixture(autouse=True)
def fresh(tmp_path, monkeypatch):
    init_db(f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(references, "REF_DIR", tmp_path / "refs")


POLICY_MD = """# Remote Access Policy

## 1. VPN entitlement
VPN access is granted to employees in Sales, Finance and Engineering by default. Contractors need manager
approval and a 90-day expiry.

## 2. Loaner devices
A loaner laptop may be issued for at most 14 days. Extensions require the IT manager's written approval.
"""


def _docx(paragraphs):
    import docx
    d = docx.Document()
    for style, text in paragraphs:
        p = d.add_paragraph(text)
        if style:
            p.style = d.styles[style]
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def _pdf(text):
    from pypdf import PdfWriter
    w = PdfWriter()
    page = w.add_blank_page(width=300, height=300)
    # pypdf cannot draw text; emulate a text PDF via a minimal content stream
    from pypdf.generic import DecodedStreamObject, NameObject, DictionaryObject
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): w._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 20 150 Td ({text}) Tj ET".encode("latin-1"))
    page[NameObject("/Contents")] = w._add_object(stream)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_markdown_and_docx_are_chunked_and_searchable():
    doc = references.add("Remote Access Policy", "policy.md", POLICY_MD.encode(), by="admin@test")
    assert doc.chunks == 2
    hits = references.search("L1", "contractor vpn approval expiry")
    assert hits and hits[0]["ref"] == f"{doc.id}#1" and hits[0]["heading"] == "1. VPN entitlement" and "90-day" in hits[0]["text"]
    d2 = references.add("Loaner rules", "loaner.docx", _docx([("Heading 1", "Loaner devices"), (None, "Loaners are limited to 14 days; extensions need the IT manager.")]), by="a@test", scope="L4")
    assert d2.chunks == 1
    assert references.search("L4", "loaner extension")[0]["document"] == "Loaner rules"
    assert all(h["document"] != "Loaner rules" for h in references.search("L2", "loaner extension"))   # L4-scoped


def test_pdf_pages_are_extracted():
    doc = references.add("Printer policy", "printer.pdf", _pdf("Color printing is reserved for Marketing and requires manager approval."), by="a@test")
    assert doc.chunks == 1
    hit = references.search("L1", "color printing approval")[0]
    assert hit["page"] == 1 and "Marketing" in hit["text"]


def test_unsupported_and_empty_files_are_rejected():
    with pytest.raises(ValueError):
        references.add("x", "image.png", b"\x89PNG", by="a@test")
    with pytest.raises(ValueError):
        references.add("x", "empty.txt", b"   ", by="a@test")


def test_agent_tool_and_console_upload(desk):
    from app.tools import Ctx, run_tool
    r = desk.post("/references", data={"title": "Remote Access Policy", "scope": "shared"},
                  files={"file": ("policy.md", POLICY_MD.encode(), "text/markdown")}).json()
    assert r["chunks"] == 2
    with session() as db:
        out = run_tool(Ctx(db=db, ticket=None, agent="L3"), "search_references", {"query": "loaner laptop days extension"})
    assert out["matched"] and out["results"][0]["ref"].endswith("#2") and "14 days" in out["results"][0]["text"]
    with session() as db:
        miss = run_tool(Ctx(db=db, ticket=None, agent="L3"), "search_references", {"query": "parking garage"})
    assert miss["matched"] is False and "do not state policy" in miss["note"]
    assert desk.get("/references").json()[0]["title"] == "Remote Access Policy"
    assert "Reference policies" in desk.get("/knowledge").text
    assert desk.delete(f"/references/{r['id']}").json()["status"] == "removed"
    assert desk.get("/references").json() == []


def test_kind_and_enforcement_travel_with_passages_and_supervisor_can_check(desk):
    import app.supervisor  # noqa: F401  (registers the Supervisor's tools)
    from app.tools import Ctx, run_tool
    desk.post("/references", data={"title": "Access Policy", "scope": "shared", "kind": "policy", "enforcement": "strict"},
              files={"file": ("policy.md", POLICY_MD.encode(), "text/markdown")})
    desk.post("/references", data={"title": "Style guide", "scope": "shared", "kind": "guideline", "enforcement": "flexible"},
              files={"file": ("style.md", b"# Tone\n\nPrefer short replies; loaner laptops may be issued for 14 days by default.", "text/markdown")})
    hits = references.search("L1", "loaner laptop 14 days")
    assert {h["enforcement"] for h in hits} == {"strict", "flexible"} and hits[0]["kind"] in ("policy", "guideline")
    set_llm(ScriptedLLM([Reply(tool_calls=[ToolCall("c1", "assign_loaner", {"employee_no": "E1001"})]), Reply(text="verildi")]))
    desk.post("/messages", json={"requester": "+905551110001", "text": "yedek laptop lazım"})
    with session() as db:
        out = run_tool(Ctx(db=db, ticket=None, agent="SUP"), "check_compliance", {"ticket_id": 1})
    assert out["ok"] and out["actions"] and out["references"][0]["enforcement"] == "strict"
    assert desk.get("/references").json()[0]["kind"] in ("policy", "guideline")
