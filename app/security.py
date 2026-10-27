"""Hardening shared by the console and the integration API: login rate limiting, CSRF origin checks,
API keys for machine endpoints, and an audit log of everything a desk user does."""
from __future__ import annotations

import hmac
import os
import time
from collections import defaultdict, deque
from urllib.parse import urlparse

from fastapi import HTTPException, Request

from app.config import auth_settings

# ---- rate limiting (per client IP, in memory; enough for one node) ---------------------------

_hits: dict[str, deque] = defaultdict(deque)


def rate_limit(request: Request, bucket: str, limit: int = 10, window_sec: int = 300) -> None:
    """Allow `limit` calls per `window_sec` for this bucket + client; 429 beyond that."""
    key = f"{bucket}:{request.client.host if request.client else 'unknown'}"
    now = time.monotonic()
    q = _hits[key]
    while q and now - q[0] > window_sec:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(429, "too many attempts, try again later")
    q.append(now)


# ---- CSRF: browsers send Origin on form posts; it must match where the console is served ------

def check_origin(request: Request) -> None:
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return                     # non-browser clients (tests, curl) carry no Origin
    allowed = {urlparse(auth_settings.public_url).netloc, request.headers.get("host", "")}
    if urlparse(origin).netloc not in allowed:
        raise HTTPException(403, "cross-site request blocked")


def cookie_secure() -> bool:
    return auth_settings.public_url.startswith("https://")


# ---- API key for the integration endpoints (/messages, /voice, ...) ---------------------------

API_KEY = os.getenv("API_KEY", "")


def api_key(request: Request) -> None:
    """When API_KEY is set, machine endpoints require `X-API-Key`; unset means open (local dev)."""
    if not API_KEY:
        return
    given = request.headers.get("x-api-key", "")
    if not hmac.compare_digest(given, API_KEY):
        raise HTTPException(401, "invalid API key")
