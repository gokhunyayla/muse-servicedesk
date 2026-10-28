"""Documents people attach on any channel (a CV, a form, an error log): the text is read at the channel's edge and
travels with the message, so the agents see it like anything else the employee wrote. The file itself is not
kept; what the agents need from it ends up on the ticket in their own words."""
from __future__ import annotations

MIME_EXT = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/plain": "txt",
    "text/markdown": "md",
}
DOC_EXT = {"pdf", "docx", "txt", "md"}
MAX_CHARS = 12000
MAX_BYTES = 10_000_000


def _ext(filename: str, mime: str) -> str:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    return ext if ext in DOC_EXT else MIME_EXT.get((mime or "").split(";")[0].strip().lower(), "")


def is_document(filename: str, mime: str) -> bool:
    return bool(_ext(filename, mime))


def document_text(filename: str, mime: str, data: bytes) -> str:
    """Plain text of a PDF / DOCX / text file; raises ValueError for anything else."""
    from app.references import extract
    ext = _ext(filename, mime)
    if not ext:
        raise ValueError("not a supported document (PDF, DOCX, text)")
    if len(data) > MAX_BYTES:
        raise ValueError("document larger than 10 MB")
    name = filename if (filename or "").lower().endswith("." + ext) else f"{filename or 'document'}.{ext}"
    text = "\n".join(t for _, t in extract(name, data)).strip()
    return text[:MAX_CHARS] + ("\n[… truncated]" if len(text) > MAX_CHARS else "")


def with_document(text: str, filename: str, mime: str, data: bytes) -> str:
    """The message text with the attached document's text appended (or a note that it could not be read)."""
    label = filename or "document"
    try:
        body = document_text(filename, mime, data)
        block = f"[Attached document: {label}]\n{body}" if body else f"[Attached document: {label} — no readable text]"
    except Exception as e:  # noqa: BLE001
        block = f"[Attached document: {label} — could not be read: {str(e)[:120]}]"
    return f"{(text or '').strip()}\n\n{block}".strip()
