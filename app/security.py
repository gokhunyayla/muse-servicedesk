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

API_KEY = ""     # tests may monkeypatch this; otherwise the console/.env value is used


def _local_install() -> bool:
    from urllib.parse import urlparse
    from app.config import auth_settings
    host = (urlparse(auth_settings.public_url).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1") or host.endswith((".localhost", ".test"))


def api_key(request: Request) -> None:
    """Machine endpoints (/messages, /voice, /image, /camera, /proactive/run …) require `X-API-Key` once a key is
    set (Settings → Channels or .env). Without a key they stay open only on a local install; a public desk
    refuses them, so an unset key never leaves a door open on the internet."""
    from app import vault
    key = API_KEY or vault.get("API_KEY")
    if not key:
        if _local_install():
            return
        raise HTTPException(403, "integration API is closed: set an API key in Settings → Channels")
    given = request.headers.get("x-api-key", "")
    if not hmac.compare_digest(given, key):
        raise HTTPException(401, "invalid API key")


def admin_or_api_key(request: Request) -> None:
    """Machine endpoints that a console admin may also call from the browser (camera frames, voice notes, images):
    a signed-in admin passes with the origin check, anyone else needs `X-API-Key` as in `api_key`."""
    from app.auth import current_user
    try:
        user = current_user(request)
    except HTTPException:
        user = None
    if user is not None:
        if user.role != "admin":
            raise HTTPException(403, "admins only")
        check_origin(request)
        return
    api_key(request)
