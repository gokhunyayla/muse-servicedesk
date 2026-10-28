"""Secrets (API keys, tokens) managed by admins in the console and stored encrypted in the settings table.
`.env` values are only a fallback for first run / local development; anything set in the console wins.

Encryption: Fernet with a key kept in data/.secret_key (created on first use, mode 600, git-ignored).
Values are never written to logs or rendered back to the browser; the UI only sees "configured" + last 4."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.db import Setting, session

KEY_FILE = Path(__file__).resolve().parent.parent / "data" / ".secret_key"
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "values": {}}
TTL = 5.0            # seconds; keeps hot paths (every model call) off the database

SPEC = {   # name -> (env fallback, label, group)
    "META_API_KEY": ("META_API_KEY", "Meta Model API key", "models"),
    "WA_ACCESS_TOKEN": ("WA_ACCESS_TOKEN", "WhatsApp access token", "channels"),
    "WA_PHONE_NUMBER_ID": ("WA_PHONE_NUMBER_ID", "WhatsApp phone number ID", "channels"),
    "WA_VERIFY_TOKEN": ("WA_VERIFY_TOKEN", "WhatsApp webhook verify token", "channels"),
    "DEMO_MANAGER_PHONE": ("DEMO_MANAGER_PHONE", "Demo manager phone (approvals go here)", "channels"),
    "API_KEY": ("API_KEY", "Integration API key (X-API-Key)", "channels"),
    "TAVILY_API_KEY": ("TAVILY_API_KEY", "Tavily search key (Supervisor research)", "channels"),
    "IMAP_PASSWORD": ("IMAP_PASSWORD", "Mailbox password (e-mail intake)", "channels"),
    "GRAPH_CLIENT_SECRET": ("GRAPH_CLIENT_SECRET", "Microsoft Entra app client secret", "connectors"),
}
SECRET = {"META_API_KEY", "WA_ACCESS_TOKEN", "API_KEY", "TAVILY_API_KEY", "IMAP_PASSWORD", "GRAPH_CLIENT_SECRET"}   # masked in the UI


def _fernet() -> Fernet:
    env = os.getenv("SECRET_KEY")
    if env:
        return Fernet(env.encode())
    if not KEY_FILE.exists():
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_bytes(Fernet.generate_key())
        os.chmod(KEY_FILE, 0o600)
    return Fernet(KEY_FILE.read_bytes().strip())


def _load() -> dict:
    with session() as db:
        row = db.get(Setting, "secrets")
        raw = dict(row.value or {}) if row else {}
    f = _fernet()
    out = {}
    for k, v in raw.items():
        try:
            out[k] = f.decrypt(v.encode()).decode()
        except (InvalidToken, AttributeError):
            out[k] = ""
    return out


def all_values() -> dict:
    now = time.time()
    with _lock:
        if now - _cache["at"] > TTL:
            try:
                _cache["values"] = _load()
            except Exception:          # database not ready (e.g. before init): fall back to env
                _cache["values"] = {}
            _cache["at"] = now
        return dict(_cache["values"])


def get(name: str, default: str = "") -> str:
    """Console value if set, else the .env fallback, else default."""
    v = all_values().get(name, "")
    if v:
        return v
    env = SPEC.get(name, (name,))[0]
    return os.getenv(env, default) or default


def set_many(values: dict[str, str | None]) -> None:
    """Store the given names (None/empty clears). Encrypted at rest."""
    f = _fernet()
    with session() as db:
        row = db.get(Setting, "secrets")
        current = dict(row.value or {}) if row else {}
        for k, v in values.items():
            if k not in SPEC:
                continue
            if v:
                current[k] = f.encrypt(v.encode()).decode()
            else:
                current.pop(k, None)
        if row is None:
            db.add(Setting(key="secrets", value=current))
        else:
            row.value = current
    with _lock:
        _cache["at"] = 0.0
    if "DEMO_MANAGER_PHONE" in values:
        from app import systems
        systems.apply_demo_phone(get("DEMO_MANAGER_PHONE"))


def status(name: str) -> dict:
    """What the UI may know: configured where, and the last 4 characters."""
    console = all_values().get(name, "")
    env = os.getenv(SPEC[name][0], "")
    v = console or env
    return {"configured": bool(v), "source": "console" if console else ("env" if env else ""),
            "hint": (v[-4:] if name in SECRET else v) if v else "", "label": SPEC[name][1]}
