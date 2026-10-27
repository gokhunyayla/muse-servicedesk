"""Model access. One interface, two implementations:

- MetaLLM talks to Meta Model API (OpenAI-compatible chat completions).
- ScriptedLLM replays fixed responses so tests run offline and deterministically.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

from app.config import settings


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict


@dataclass
class Reply:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)


class LLM(Protocol):
    def chat(self, messages: list[dict], tools: list[dict], heavy: bool = False) -> Reply: ...


class ContentFiltered(Exception):
    """The provider refused the request on content-policy grounds (HTTP 400, no model output)."""


class MetaLLM:
    def __init__(self) -> None:
        from openai import OpenAI

        if not settings.meta_api_key:
            raise RuntimeError("META_API_KEY is empty; add it to .env")
        self.client = OpenAI(api_key=settings.meta_api_key, base_url=settings.meta_base_url)

    def chat(self, messages: list[dict], tools: list[dict], heavy: bool = False) -> Reply:
        from openai import BadRequestError

        try:
            res = self.client.chat.completions.create(
                model=settings.meta_model_heavy if heavy else settings.meta_model,
                messages=messages,
                tools=tools or None,
                # reasoning tokens count against this budget, so keep it generous
                max_tokens=4000,
                extra_body={"reasoning_effort": settings.reasoning_effort},
            )
        except BadRequestError as e:
            if "content management policy" in str(e):
                raise ContentFiltered(str(e)) from e
            raise
        msg = res.choices[0].message
        calls = [
            ToolCall(id=c.id, name=c.function.name, arguments=json.loads(c.function.arguments or "{}"))
            for c in (msg.tool_calls or [])
        ]
        return Reply(text=msg.content or "", tool_calls=calls)


class ScriptedLLM:
    """Returns the queued replies in order; records every request for assertions."""

    def __init__(self, replies: list[Reply]) -> None:
        self.replies = list(replies)
        self.requests: list[list[dict]] = []
        self.heavy_calls = 0

    def chat(self, messages: list[dict], tools: list[dict], heavy: bool = False) -> Reply:
        self.requests.append([dict(m) for m in messages])
        self.heavy_calls += heavy
        if not self.replies:
            return Reply(text="(script exhausted)")
        return self.replies.pop(0)


def image_message(text: str, image: bytes, mime: str = "image/jpeg") -> list[dict]:
    """A user message that carries a photo alongside its caption (OpenAI content-parts format)."""
    import base64
    data = base64.b64encode(image).decode()
    return [{"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}}]


_llm: LLM | None = None


def get_llm() -> LLM:
    global _llm
    if _llm is None:
        _llm = MetaLLM()
    return _llm


def set_llm(llm: LLM) -> None:
    global _llm
    _llm = llm
