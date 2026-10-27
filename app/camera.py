"""Visual compliance watcher. A camera frame is checked for policy violations (credentials on a
sticky note, unlocked unattended screen, unescorted visitor). Muse Spark decides what it sees, SAM 3.1
pins the object down, and a ticket goes to the desk owner through the normal L1 flow."""
from __future__ import annotations

import json

from app import systems, vision
from app.agents import handle_message
from app.db import session
from app.llm import get_llm, image_message

CAMERAS = {   # camera id -> desk -> employee
    "CAM-SALES-01": {"location": "Sales floor, desk row A", "employee_no": "E1001"},
    "CAM-FIN-01": {"location": "Finance, desk 3", "employee_no": "E1003"},
}

CHECKS = {   # SAM wants one concrete noun phrase; we try a few until something is located
    "credential_note": {"prompts": ["sticky note", "yellow sticky note", "paper note"],
                        "policy": "ISO 27001 A.5.17 — credentials must not be written down"},
    "unlocked_screen": {"prompts": ["computer monitor", "laptop screen"],
                        "policy": "clear-screen policy — lock when leaving the desk"},
    "visitor_badge": {"prompts": ["person"], "policy": "visitors must wear a badge and be escorted"},
}

QUESTION = (
    "You are a security compliance checker for an office camera frame. Answer ONLY with JSON: "
    '{"findings":[{"kind":"credential_note|unlocked_screen|visitor_badge","evidence":"<what you see, short>",'
    '"confidence":0.0-1.0}]}. credential_note = a sticky note or paper with a password/PIN visible. '
    "unlocked_screen = a monitor showing an unlocked desktop/app with no one at the desk. "
    "visitor_badge = a person without a visible badge. Empty findings if nothing applies."
)


def inspect_frame(image: bytes, mime: str = "image/jpeg") -> list[dict]:
    reply = get_llm().chat([{"role": "user", "content": image_message(QUESTION, image, mime)}], [])
    try:
        text = reply.text.strip().strip("`")
        text = text[text.find("{"):text.rfind("}") + 1]
        findings = json.loads(text).get("findings", [])
    except (ValueError, AttributeError):
        findings = []
    out = []
    for f in findings:
        kind = f.get("kind")
        if kind not in CHECKS or float(f.get("confidence", 0)) < 0.6:
            continue
        boxes = []
        for prompt in CHECKS[kind]["prompts"]:
            boxes = vision.segment(image, prompt, mime)
            if boxes:
                break
        out.append({"kind": kind, "evidence": f.get("evidence", ""), "confidence": f.get("confidence"),
                    "boxes": boxes, "policy": CHECKS[kind]["policy"]})
    return out


def process_frame(camera_id: str, image: bytes, mime: str = "image/jpeg") -> list[dict]:
    """Check one frame; open a ticket for the desk owner per finding. Returns the findings."""
    cam = CAMERAS.get(camera_id)
    if not cam:
        raise KeyError(f"unknown camera {camera_id}")
    findings = inspect_frame(image, mime)
    results = []
    for f in findings:
        owner = systems.USERS[cam["employee_no"]]
        where = f["boxes"][0] if f["boxes"] else None
        loc = f" at x={where['x1']}-{where['x2']}, y={where['y1']}-{where['y2']}" if where else ""
        text = (f"[Camera {camera_id}, {cam['location']}] Compliance finding: {f['kind']} — {f['evidence']}{loc}. "
                f"Policy: {f['policy']}. Desk owner: {cam['employee_no']} ({owner['name']}). "
                f"Notify the owner, ask them to fix it, revoke any exposed credential, and schedule a re-check.")
        with session() as db:
            ticket, answer = handle_message(db, owner["phone"], text, image, mime, force_new=True)
            ticket.title = f"[Compliance:{f['kind']}] {camera_id}"
            results.append({"ticket_id": ticket.id, "kind": f["kind"], "boxes": f["boxes"], "reply": answer})
    return results
