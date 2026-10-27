from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.agents import handle_message
from app.db import Ticket, session

app = FastAPI(title="Muse ServiceDesk")


class Incoming(BaseModel):
    requester: str
    text: str


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.post("/messages")
def post_message(msg: Incoming) -> dict:
    with session() as db:
        ticket, answer = handle_message(db, msg.requester, msg.text)
        return {"ticket_id": ticket.id, "reply": answer}


@app.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: int) -> dict:
    with session() as db:
        t = db.get(Ticket, ticket_id)
        if t is None:
            raise HTTPException(404)
        return {"id": t.id, "title": t.title, "status": t.status, "verified": t.verified,
                "events": [{"at": e.at.isoformat(), "actor": e.actor, "kind": e.kind,
                            "content": e.content, "data": e.data} for e in t.events]}
