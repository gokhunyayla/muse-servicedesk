"""Console sign-in: a small pool of desk users (admins and support agents) with password sessions,
e-mail password reset and an admin-only user list. Employees never sign in here; they use WhatsApp."""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import smtplib
from datetime import timedelta
from email.message import EmailMessage

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import auth_settings
from app.db import Session, User, _now, audit, session
from app.security import check_origin, cookie_secure, rate_limit

logger = logging.getLogger(__name__)
router = APIRouter()
COOKIE = "sd_session"
SESSION_DAYS = 7
RESET_MINUTES = 30


# ---- passwords & sessions --------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return f"pbkdf2${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return hmac.compare_digest(candidate, digest)


def needs_setup() -> bool:
    with session() as db:
        return db.query(User).count() == 0


def _user_from_request(request: Request) -> User | None:
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    with session() as db:
        s = db.get(Session, token)
        if s is None or s.expires_at < _now():
            return None
        u = db.get(User, s.user_id)
        return u if u and u.active else None


def current_user(request: Request) -> User:
    """Dependency for console routes: 401 for API calls, redirect for pages is handled in main."""
    u = _user_from_request(request)
    if u is None:
        raise HTTPException(401, "sign in required")
    return u


def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "admin only")
    return user


def _sign_in(db, user: User, response: Response) -> None:
    token = secrets.token_urlsafe(32)
    db.add(Session(token=token, user_id=user.id, expires_at=_now() + timedelta(days=SESSION_DAYS)))
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=cookie_secure(),
                        max_age=SESSION_DAYS * 86400)


# ---- e-mail ----------------------------------------------------------------------------------

def send_mail(to: str, subject: str, body: str) -> bool:
    """SMTP when configured; otherwise the message goes to the log so development still works."""
    if not auth_settings.smtp_host:
        logger.warning("SMTP not configured; mail to %s: %s\n%s", to, subject, body)
        return False
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = auth_settings.smtp_from, to, subject
    msg.set_content(body)
    with smtplib.SMTP(auth_settings.smtp_host, auth_settings.smtp_port, timeout=20) as s:
        s.starttls()
        if auth_settings.smtp_user:
            s.login(auth_settings.smtp_user, auth_settings.smtp_password)
        s.send_message(msg)
    return True


# ---- pages -----------------------------------------------------------------------------------

@router.get("/setup", response_class=HTMLResponse)
def setup_page(error: str = "") -> str:
    """First run only: create the first admin. Credentials live in the users table, nowhere else."""
    if not needs_setup():
        return RedirectResponse("/login", 303)   # type: ignore[return-value]
    return _page("Create the first admin", f"""
<form method="post" action="/setup">
<p class="muted">No desk users exist yet. Create the first administrator; they can add everyone else at Users.</p>
<label>Name</label><input name="name" required autofocus>
<label>E-mail</label><input name="email" type="email" required>
<label>Password</label><input name="password" type="password" minlength="8" required>
<label>Repeat</label><input name="password2" type="password" minlength="8" required>
{_alert(error, "")}
<button class="btn primary" type="submit">Create admin and sign in</button></form>""")


@router.post("/setup")
def setup(request: Request, name: str = Form(...), email: str = Form(...), password: str = Form(...), password2: str = Form(...)):
    check_origin(request)
    if not needs_setup():
        raise HTTPException(403, "setup already done")
    if password != password2:
        return RedirectResponse("/setup?error=Passwords+do+not+match", 303)
    with session() as db:
        u = User(email=email.lower().strip(), name=name.strip(), role="admin", password_hash=hash_password(password))
        db.add(u)
        db.flush()
        audit(db, u.email, "setup.first_admin", ip=_ip(request))
        r = RedirectResponse("/", 303)
        _sign_in(db, u, r)
        return r


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, error: str = "", info: str = "") -> str:
    if needs_setup():
        return RedirectResponse("/setup", 303)   # type: ignore[return-value]
    if _user_from_request(request):
        return RedirectResponse("/", 303)   # type: ignore[return-value]
    return _page("Sign in", f"""
<form method="post" action="/login">
<label>E-mail</label><input name="email" type="email" required autofocus>
<label>Password</label><input name="password" type="password" required>
{_alert(error, info)}
<button class="btn primary" type="submit">Sign in</button>
<p class="links"><a href="/forgot">Forgot your password?</a></p></form>""")


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...)):
    check_origin(request)
    rate_limit(request, "login", limit=10, window_sec=300)
    with session() as db:
        u = db.query(User).filter(User.email == email.lower().strip()).first()
        if u is None or not u.active or not u.password_hash or not verify_password(password, u.password_hash):
            audit(db, email.lower().strip(), "login.failed", ip=_ip(request))
            return RedirectResponse("/login?error=Wrong+e-mail+or+password", 303)
        audit(db, u.email, "login.ok", ip=_ip(request))
        r = RedirectResponse("/", 303)
        _sign_in(db, u, r)
        return r


@router.post("/logout")
@router.get("/logout")
def logout(request: Request):
    token = request.cookies.get(COOKIE)
    if token:
        with session() as db:
            s = db.get(Session, token)
            if s:
                u = db.get(User, s.user_id)
                audit(db, u.email if u else "?", "logout", ip=_ip(request))
                db.delete(s)
    r = RedirectResponse("/login", 303)
    r.delete_cookie(COOKIE)
    return r


@router.get("/forgot", response_class=HTMLResponse)
def forgot_page(info: str = "") -> str:
    return _page("Reset your password", f"""
<form method="post" action="/forgot">
<p class="muted">Enter your e-mail; if it belongs to a desk user we send a reset link (valid {RESET_MINUTES} minutes).</p>
<label>E-mail</label><input name="email" type="email" required autofocus>
{_alert("", info)}
<button class="btn primary" type="submit">Send reset link</button>
<p class="links"><a href="/login">Back to sign in</a></p></form>""")


@router.post("/forgot")
def forgot(request: Request, email: str = Form(...)):
    check_origin(request)
    rate_limit(request, "forgot", limit=5, window_sec=900)
    with session() as db:
        audit(db, email.lower().strip(), "password.reset_requested", ip=_ip(request))
        u = db.query(User).filter(User.email == email.lower().strip(), User.active.is_(True)).first()
        if u:
            u.reset_token = secrets.token_urlsafe(32)
            u.reset_expires = _now() + timedelta(minutes=RESET_MINUTES)
            link = f"{auth_settings.public_url}/reset?token={u.reset_token}"
            send_mail(u.email, "Muse ServiceDesk: reset your password",
                      f"Hello {u.name},\n\nReset your password here (valid {RESET_MINUTES} minutes):\n{link}\n\n"
                      f"If you did not ask for this, ignore this e-mail.")
    return RedirectResponse("/forgot?info=If+that+address+belongs+to+a+desk+user,+a+reset+link+is+on+its+way.", 303)


@router.get("/reset", response_class=HTMLResponse)
def reset_page(token: str = "", error: str = "") -> str:
    return _page("Choose a new password", f"""
<form method="post" action="/reset"><input type="hidden" name="token" value="{token}">
<label>New password</label><input name="password" type="password" minlength="8" required autofocus>
<label>Repeat</label><input name="password2" type="password" minlength="8" required>
{_alert(error, "")}
<button class="btn primary" type="submit">Save password</button></form>""")


@router.post("/reset")
def reset(request: Request, token: str = Form(...), password: str = Form(...), password2: str = Form(...)):
    check_origin(request)
    rate_limit(request, "reset", limit=10, window_sec=900)
    if password != password2:
        return RedirectResponse(f"/reset?token={token}&error=Passwords+do+not+match", 303)
    with session() as db:
        u = db.query(User).filter(User.reset_token == token).first()
        if u is None or not u.reset_expires or u.reset_expires < _now():
            return RedirectResponse("/forgot?info=That+link+is+invalid+or+expired;+request+a+new+one.", 303)
        u.password_hash, u.reset_token, u.reset_expires = hash_password(password), None, None
        audit(db, u.email, "password.set", ip=_ip(request))
        for s in db.query(Session).filter(Session.user_id == u.id):
            db.delete(s)                       # sign out everywhere after a reset
    return RedirectResponse("/login?info=Password+updated,+sign+in.", 303)


# ---- user administration (admin only) --------------------------------------------------------

@router.get("/users", response_class=HTMLResponse)
def users_page(admin: User = Depends(admin_user), info: str = "", error: str = "", link: str = "") -> str:
    with session() as db:
        rows = "".join(
            f"<tr><td>{_esc(u.name)}</td><td>{_esc(u.email)}</td><td>{u.role}</td>"
            f"<td>{'active' if u.active else 'disabled'}</td><td>"
            + ("<span class='muted'>you</span>" if u.id == admin.id else
               f"<form method='post' action='/users/{u.id}/role' style='display:inline'>"
               f"<input type='hidden' name='role' value='{'agent' if u.role == 'admin' else 'admin'}'>"
               f"<button class='btn'>{'Make agent' if u.role == 'admin' else 'Make admin'}</button></form> "
               f"<form method='post' action='/users/{u.id}/toggle' style='display:inline'>"
               f"<button class='btn'>{'Disable' if u.active else 'Enable'}</button></form>")
            + "</td></tr>"
            for u in db.query(User).order_by(User.id))
    return _page("Desk users", f"""
<p class="muted">People who may sign in to this console. Employees never sign in; they use WhatsApp.</p>
<table><thead><tr><th>Name</th><th>E-mail</th><th>Role</th><th>Status</th><th></th></tr></thead><tbody>{rows}</tbody></table>
<h3>Add a support agent or admin</h3>
<form method="post" action="/users" class="grid">
<div><label>Name</label><input name="name" required></div>
<div><label>E-mail</label><input name="email" type="email" required></div>
<div><label>Role</label><select name="role"><option value="agent">Support agent</option><option value="admin">Admin</option></select></div>
<div><label>Invitation</label><p class="muted" style="margin:6px 0 0">They receive a link to set their own password (valid 24 h). No passwords are typed or e-mailed by you.</p></div>
{_alert(error, info)}
<div><button class="btn primary" type="submit">Invite user</button></div></form>
{('<p class="alert ok">Set-password link (SMTP not configured, share it yourself): <a href="'+_esc(link)+'">'+_esc(link)+'</a></p>') if link else ''}
<p class="links"><a href="/">Back to the console</a></p>""", wide=True, user=admin)


@router.post("/users")
def add_user(request: Request, admin: User = Depends(admin_user), name: str = Form(...), email: str = Form(...),
             role: str = Form("agent")):
    """Invite a desk user: the account is created without a password and a set-password link is sent."""
    check_origin(request)
    if role not in ("admin", "agent"):
        raise HTTPException(422, "role must be admin or agent")
    with session() as db:
        if db.query(User).filter(User.email == email.lower().strip()).first():
            return RedirectResponse("/users?error=That+e-mail+already+exists", 303)
        u = User(email=email.lower().strip(), name=name.strip(), role=role, password_hash="",
                 reset_token=secrets.token_urlsafe(32), reset_expires=_now() + timedelta(hours=24))
        db.add(u)
        audit(db, admin.email, "user.invited", target=u.email, detail=role, ip=_ip(request))
        link = f"{auth_settings.public_url}/reset?token={u.reset_token}"
        mailed = send_mail(u.email, "You were invited to Muse ServiceDesk",
                           f"Hello {u.name},\n\n{admin.name} invited you to the service desk console as {role}. "
                           f"Set your password here (valid 24 hours):\n{link}\n")
    q = f"info={role.title()}+{name.strip().replace(' ', '+')}+invited"
    return RedirectResponse(f"/users?{q}" + ("" if mailed else f"&link={link}"), 303)


@router.post("/users/{user_id}/role")
def set_role(request: Request, user_id: int, role: str = Form(...), admin: User = Depends(admin_user)):
    """Any user can be made an admin (or back to agent); you cannot change your own role."""
    check_origin(request)
    if role not in ("admin", "agent"):
        raise HTTPException(422, "role must be admin or agent")
    with session() as db:
        u = db.get(User, user_id)
        if u is None or u.id == admin.id:
            raise HTTPException(400, "cannot change this user")
        u.role = role
        audit(db, admin.email, "user.role", target=u.email, detail=role, ip=_ip(request))
    return RedirectResponse("/users", 303)


@router.post("/users/{user_id}/toggle")
def toggle_user(request: Request, user_id: int, admin: User = Depends(admin_user)):
    check_origin(request)
    with session() as db:
        u = db.get(User, user_id)
        if u is None or u.id == admin.id:
            raise HTTPException(400, "cannot change this user")
        u.active = not u.active
        audit(db, admin.email, "user.enabled" if u.active else "user.disabled", target=u.email, ip=_ip(request))
        if not u.active:
            for s in db.query(Session).filter(Session.user_id == u.id):
                db.delete(s)
    return RedirectResponse("/users", 303)


@router.get("/me")
def me(user: User = Depends(current_user)) -> dict:
    return {"id": user.id, "name": user.name, "email": user.email, "role": user.role}


@router.get("/users.json")
def users_json(user: User = Depends(current_user)) -> list[dict]:
    """Active desk users, for the console's "assign to a human" picker."""
    with session() as db:
        return [{"id": u.id, "name": u.name, "role": u.role}
                for u in db.query(User).filter(User.active.is_(True)).order_by(User.name)]


def _ip(request: Request) -> str:
    return request.client.host if request.client else ""


# ---- tiny page shell (same look as the console) ----------------------------------------------

def _esc(x: str) -> str:
    return str(x).replace("&", "&amp;").replace("<", "&lt;")


def _alert(error: str, info: str) -> str:
    if error:
        return f'<p class="alert bad">{_esc(error)}</p>'
    if info:
        return f'<p class="alert ok">{_esc(info)}</p>'
    return ""


def _page(title: str, body: str, wide: bool = False, user: User | None = None) -> str:
    who = (f'<span class="muted">{_esc(user.name)} · {user.role}</span> <a class="btn" href="/logout">Sign out</a>'
           if user else "")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{_esc(title)} · Muse ServiceDesk</title>
<meta name="viewport" content="width=device-width,initial-scale=1"><style>
:root{{--bg:#f6f7f9;--card:#fff;--fg:#1c1e21;--muted:#65676b;--line:#e4e6eb;--accent:#0866ff;--ok:#31a24c;--bad:#e41e3f}}
@media(prefers-color-scheme:dark){{:root:not([data-theme=light]){{--bg:#18191a;--card:#242526;--fg:#e4e6eb;--muted:#b0b3b8;--line:#3a3b3c}}}}
:root[data-theme=dark]{{--bg:#18191a;--card:#242526;--fg:#e4e6eb;--muted:#b0b3b8;--line:#3a3b3c}}
*{{box-sizing:border-box}}html{{color-scheme:light dark}}body{{margin:0;font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}}
header{{background:var(--card);padding:10px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:10px}}
.brand{{display:flex;align-items:center;gap:10px}}.logo{{width:32px;height:32px;border-radius:9px;background:var(--accent);display:grid;place-items:center}}
.logo svg{{width:20px;height:20px;stroke:#fff;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}}
h1{{font-size:16px;margin:0;line-height:1.1}}h1 small{{display:block;font-size:11px;font-weight:400;color:var(--muted)}}
main{{max-width:{'960px' if wide else '400px'};margin:32px auto;padding:0 16px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px}}h2{{margin:0 0 12px;font-size:18px}}h3{{font-size:14px;margin:20px 0 8px}}
label{{display:block;font-size:12px;color:var(--muted);margin:12px 0 4px}}input,select{{width:100%;font:inherit;padding:8px 10px;border-radius:6px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}}
.btn{{font:inherit;font-size:13px;padding:7px 14px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer;text-decoration:none;display:inline-block;margin-top:14px}}
td .btn{{margin-top:0;padding:4px 9px;font-size:12px}}
.btn.primary{{background:var(--accent);border-color:var(--accent);color:#fff;width:100%}}.grid .btn.primary{{width:auto}}
.links{{margin:14px 0 0;font-size:13px}}.links a{{color:var(--accent);text-decoration:none}}.muted{{color:var(--muted);font-size:13px}}
.alert{{padding:8px 10px;border-radius:6px;font-size:13px;margin-top:12px}}.alert.bad{{background:rgba(228,30,63,.12);color:var(--bad)}}.alert.ok{{background:rgba(49,162,76,.12);color:var(--ok)}}
table{{width:100%;border-collapse:collapse;margin-top:8px}}td,th{{text-align:left;padding:7px 6px;border-bottom:1px solid var(--line)}}th{{color:var(--muted);font-weight:500;font-size:12px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:0 14px}}@media(max-width:700px){{.grid{{grid-template-columns:1fr}}}}
</style></head><body>
<header><div class="brand"><div class="logo"><svg viewBox="0 0 24 24"><path d="M4 5h16v10H9l-5 4z"/><path d="M8 9h8M8 12h5"/></svg></div>
<h1>Muse ServiceDesk<small>Service desk console</small></h1></div><div>{who}</div></header>
<main><div class="card"><h2>{_esc(title)}</h2>{body}</div></main>
<script>try{{var t=localStorage.getItem('theme');if(t&&t!=='system')document.documentElement.setAttribute('data-theme',t)}}catch(e){{}}</script>
</body></html>"""
