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
SESSION_DAYS = 1          # a plain sign-in
REMEMBER_DAYS = 30        # "keep me signed in"
EYE = '<svg viewBox="0 0 24 24"><path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7S1 12 1 12z"/><circle cx="12" cy="12" r="3"/></svg>'
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


def _sign_in(db, user: User, response: Response, days: int = SESSION_DAYS) -> None:
    token = secrets.token_urlsafe(32)
    db.add(Session(token=token, user_id=user.id, expires_at=_now() + timedelta(days=days)))
    response.set_cookie(COOKIE, token, httponly=True, samesite="lax", secure=cookie_secure(), max_age=days * 86400)


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
<p class="lead">No desk users exist yet. Create the first administrator — they invite everyone else.</p>
<form method="post" action="/setup">
<label for="name">Name</label><input id="name" name="name" autocomplete="name" required autofocus>
<label for="email">E-mail</label><input id="email" name="email" type="email" autocomplete="username" required>
<label for="pw">Password <small>(at least 8 characters)</small></label><div class="pw"><input id="pw" name="password" type="password" minlength="8" autocomplete="new-password" required>
<button type="button" class="eye" onclick="togglePw('pw',this)" aria-label="Show password">{EYE}</button></div>
<label for="pw2">Repeat password</label><input id="pw2" name="password2" type="password" minlength="8" autocomplete="new-password" required>
{_alert(error, "")}
<button class="btn primary" type="submit">Create admin and sign in</button></form>""", shell="auth")


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
<p class="lead">Welcome back. Sign in to the service desk console.</p>
<form method="post" action="/login">
<label for="email">E-mail</label><input id="email" name="email" type="email" autocomplete="username" required autofocus>
<label for="pw">Password</label><div class="pw"><input id="pw" name="password" type="password" autocomplete="current-password" required>
<button type="button" class="eye" onclick="togglePw('pw',this)" aria-label="Show password">{EYE}</button></div>
<div class="row between"><label class="check"><input type="checkbox" name="remember" value="1"> Keep me signed in</label><a href="/forgot">Forgot password?</a></div>
{_alert(error, info)}
<button class="btn primary" type="submit">Sign in</button></form>""", shell="auth")


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), remember: str = Form("")):
    check_origin(request)
    rate_limit(request, "login", limit=10, window_sec=300)
    with session() as db:
        u = db.query(User).filter(User.email == email.lower().strip()).first()
        if u is None or not u.active or not u.password_hash or not verify_password(password, u.password_hash):
            audit(db, email.lower().strip(), "login.failed", ip=_ip(request))
            return RedirectResponse("/login?error=Wrong+e-mail+or+password", 303)
        audit(db, u.email, "login.ok", ip=_ip(request))
        r = RedirectResponse("/", 303)
        _sign_in(db, u, r, days=REMEMBER_DAYS if remember else SESSION_DAYS)
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
<p class="lead">Enter your e-mail. If it belongs to a desk user we send a reset link, valid {RESET_MINUTES} minutes.</p>
<form method="post" action="/forgot">
<label for="email">E-mail</label><input id="email" name="email" type="email" autocomplete="username" required autofocus>
{_alert("", info)}
<button class="btn primary" type="submit">Send reset link</button>
<p class="links"><a href="/login">← Back to sign in</a></p></form>""", shell="auth")


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
<form method="post" action="/reset"><input type="hidden" name="token" value="{_esc(token)}">
<label for="pw">New password <small>(at least 8 characters)</small></label><div class="pw"><input id="pw" name="password" type="password" minlength="8" autocomplete="new-password" required autofocus>
<button type="button" class="eye" onclick="togglePw('pw',this)" aria-label="Show password">{EYE}</button></div>
<label for="pw2">Repeat password</label><input id="pw2" name="password2" type="password" minlength="8" autocomplete="new-password" required>
{_alert(error, "")}
<button class="btn primary" type="submit">Save password</button></form>""", shell="auth")


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


# ---- profile ---------------------------------------------------------------------------------

@router.get("/profile", response_class=HTMLResponse)
def profile_page(user: User = Depends(current_user), info: str = "", error: str = "") -> str:
    with session() as db:
        u = db.get(User, user.id)
        sessions = db.query(Session).filter(Session.user_id == u.id, Session.expires_at > _now()).count()
    body = f"""
<div class="cols">
<section>
<div class="avatar big">{_initials(u.name)}</div>
<h3 style="margin-top:12px">{_esc(u.name)}</h3>
<p class="muted">{_esc(u.email)} · {u.role} · member since {u.created_at:%d %b %Y} · {sessions} active session{'s' if sessions != 1 else ''}</p>
<form method="post" action="/profile">
<label for="name">Display name</label><input id="name" name="name" value="{_esc(u.name)}" required>
<label>E-mail</label><input value="{_esc(u.email)}" disabled><small class="muted">Changing the e-mail is done by an admin.</small>
{_alert('', info) if info and not info.startswith('Password') else ''}
<button class="btn" type="submit">Save</button></form>
</section>
<aside class="side">
<h3>Change password</h3>
<form method="post" action="/profile/password">
<label for="cur">Current password</label><input id="cur" name="current" type="password" autocomplete="current-password" required>
<label for="pw">New password <small>(at least 8 characters)</small></label><div class="pw"><input id="pw" name="password" type="password" minlength="8" autocomplete="new-password" required>
<button type="button" class="eye" onclick="togglePw('pw',this)" aria-label="Show password">{EYE}</button></div>
<label for="pw2">Repeat new password</label><input id="pw2" name="password2" type="password" minlength="8" autocomplete="new-password" required>
{_alert(error, info if info.startswith('Password') else '')}
<button class="btn primary" type="submit">Update password</button>
<p class="muted" style="margin-top:10px">Other sessions are signed out after a password change.</p></form>
</aside></div>"""
    return _page("Profile", body, wide=True, user=user)


@router.post("/profile")
def profile_save(request: Request, user: User = Depends(current_user), name: str = Form(...)):
    check_origin(request)
    with session() as db:
        u = db.get(User, user.id)
        u.name = name.strip()[:120] or u.name
        audit(db, u.email, "profile.name", detail=u.name, ip=_ip(request))
    return RedirectResponse("/profile?info=Saved", 303)


@router.post("/profile/password")
def profile_password(request: Request, user: User = Depends(current_user), current: str = Form(...),
                     password: str = Form(...), password2: str = Form(...)):
    check_origin(request)
    rate_limit(request, "pwchange", limit=10, window_sec=900)
    with session() as db:
        u = db.get(User, user.id)
        if not verify_password(current, u.password_hash):
            audit(db, u.email, "password.change_failed", ip=_ip(request))
            return RedirectResponse("/profile?error=Current+password+is+wrong", 303)
        if password != password2:
            return RedirectResponse("/profile?error=New+passwords+do+not+match", 303)
        u.password_hash = hash_password(password)
        audit(db, u.email, "password.changed", ip=_ip(request))
        mine = request.cookies.get(COOKIE)
        for s in db.query(Session).filter(Session.user_id == u.id):
            if s.token != mine:
                db.delete(s)
    return RedirectResponse("/profile?info=Password+updated", 303)


# ---- audit log (admin) -----------------------------------------------------------------------

@router.get("/audit", response_class=HTMLResponse)
def audit_page(admin: User = Depends(admin_user), limit: int = 200) -> str:
    from app.db import Audit
    with session() as db:
        rows = "".join(f"<tr><td class='mono'>{r.at:%Y-%m-%d %H:%M:%S}</td><td>{_esc(r.user)}</td><td>{_esc(r.action)}</td>"
                       f"<td>{_esc(r.target)}</td><td class='muted'>{_esc(r.detail[:120])}</td><td class='mono'>{_esc(r.ip)}</td></tr>"
                       for r in db.query(Audit).order_by(Audit.id.desc()).limit(min(limit, 1000)))
    return _page("Audit log", f"""<p class="muted">Everything desk users did in this console, newest first (UTC).</p>
<div class="scroll"><table><thead><tr><th>When</th><th>User</th><th>Action</th><th>Target</th><th>Detail</th><th>IP</th></tr></thead><tbody>{rows}</tbody></table></div>""",
                 wide=True, user=admin)


@router.get("/audit.json")
def audit_json(admin: User = Depends(admin_user), limit: int = 200) -> list[dict]:
    from app.db import Audit
    with session() as db:
        return [{"at": r.at.isoformat(), "user": r.user, "action": r.action, "target": r.target, "detail": r.detail, "ip": r.ip}
                for r in db.query(Audit).order_by(Audit.id.desc()).limit(min(limit, 1000))]


def _initials(name: str) -> str:
    parts = [p for p in name.split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


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


LOGO = '<svg viewBox="0 0 24 24"><path d="M4 5h16v10H9l-5 4z"/><path d="M8 9h8M8 12h5"/></svg>'

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c1e21;--muted:#65676b;--line:#e4e6eb;--accent:#0866ff;--ok:#31a24c;--bad:#e41e3f}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#18191a;--card:#242526;--fg:#e4e6eb;--muted:#b0b3b8;--line:#3a3b3c}}
:root[data-theme=dark]{--bg:#18191a;--card:#242526;--fg:#e4e6eb;--muted:#b0b3b8;--line:#3a3b3c}
*{box-sizing:border-box}html{color-scheme:light dark}body{margin:0;font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
header{background:var(--card);padding:10px 16px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;gap:10px}
.brand{display:flex;align-items:center;gap:10px}.logo{width:32px;height:32px;border-radius:9px;background:var(--accent);display:grid;place-items:center;flex:none}
.logo svg{width:20px;height:20px;stroke:#fff;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
h1{font-size:16px;margin:0;line-height:1.1}h1 small{display:block;font-size:11px;font-weight:400;color:var(--muted)}
main{max-width:960px;margin:32px auto;padding:0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px}h2{margin:0 0 12px;font-size:18px}h3{font-size:14px;margin:20px 0 8px}
label{display:block;font-size:12px;color:var(--muted);margin:12px 0 4px}label small{color:var(--muted)}
input,select{width:100%;font:inherit;padding:9px 11px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
input:focus,select:focus{outline:2px solid var(--accent);outline-offset:-1px;border-color:var(--accent)}
.btn{font:inherit;font-size:13px;padding:8px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer;text-decoration:none;display:inline-block;margin-top:14px}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff;width:100%;padding:10px 14px;font-weight:600}.grid .btn.primary{width:auto}td .btn{margin-top:0;padding:4px 9px;font-size:12px}
.links{margin:14px 0 0;font-size:13px}.links a,.row a{color:var(--accent);text-decoration:none;font-size:13px}.muted{color:var(--muted);font-size:13px}
.alert{padding:9px 11px;border-radius:8px;font-size:13px;margin-top:12px}.alert.bad{background:rgba(228,30,63,.12);color:var(--bad)}.alert.ok{background:rgba(49,162,76,.12);color:var(--ok)}
table{width:100%;border-collapse:collapse;margin-top:8px}td,th{text-align:left;padding:7px 6px;border-bottom:1px solid var(--line)}th{color:var(--muted);font-weight:500;font-size:12px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:0 14px}@media(max-width:700px){.grid{grid-template-columns:1fr}}
/* auth shell: no header, centred card */
body.auth{min-height:100vh;display:grid;place-items:center;padding:24px;background:radial-gradient(1200px 600px at 20% -10%,rgba(8,102,255,.12),transparent 60%),var(--bg)}
.auth .card{width:min(420px,100%);padding:28px 28px 24px;box-shadow:0 10px 40px rgba(0,0,0,.08)}
.auth .brand{flex-direction:column;align-items:flex-start;gap:12px;margin-bottom:18px}.auth .logo{width:44px;height:44px;border-radius:12px}.auth .logo svg{width:26px;height:26px}
.auth h2{font-size:22px;margin:0}.auth .lead{color:var(--muted);margin:0 0 6px}
.pw{position:relative}.pw input{padding-right:42px}.eye{position:absolute;right:6px;top:50%;transform:translateY(-50%);border:0;background:transparent;color:var(--muted);cursor:pointer;width:32px;height:32px;border-radius:6px;display:grid;place-items:center}
.eye svg{width:18px;height:18px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}.eye:hover{background:var(--bg)}
.row{display:flex;align-items:center;gap:10px;margin-top:12px}.row.between{justify-content:space-between}
.check{display:flex;align-items:center;gap:8px;margin:0;font-size:13px;color:var(--fg)}.check input{width:auto;accent-color:var(--accent)}
.foot{margin-top:18px;text-align:center;font-size:12px;color:var(--muted)}
/* user menu */
.menu{position:relative}.avatar{width:34px;height:34px;border-radius:50%;border:0;background:var(--accent);color:#fff;font:600 13px/1 inherit;cursor:pointer;display:grid;place-items:center;font-family:inherit}
.avatar.big{width:64px;height:64px;font-size:22px}
.dropdown{position:absolute;right:0;top:42px;min-width:230px;background:var(--card);border:1px solid var(--line);border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,.15);padding:6px;display:none;z-index:20}
.dropdown.on{display:block}.dropdown a{display:block;padding:8px 10px;border-radius:6px;color:var(--fg);text-decoration:none;font-size:13px}.dropdown a:hover{background:var(--bg)}
.dd-head{padding:8px 10px 10px}.dd-head b{display:block;font-size:13px}.dd-head small{color:var(--muted);font-size:12px}.dd-sep{height:1px;background:var(--line);margin:6px 0}
.cols{display:grid;grid-template-columns:1fr 340px;gap:24px}@media(max-width:800px){.cols{grid-template-columns:1fr}}
.side{border-left:1px solid var(--line);padding-left:24px}@media(max-width:800px){.side{border-left:0;padding-left:0;border-top:1px solid var(--line);padding-top:8px}}
.mono{font-family:ui-monospace,Menlo,monospace;font-size:12px;white-space:nowrap}.scroll{overflow-x:auto}a.logo{text-decoration:none}
"""

JS = """<script>
try{var t=localStorage.getItem('theme');if(t&&t!=='system')document.documentElement.setAttribute('data-theme',t)}catch(e){}
function togglePw(id,b){var i=document.getElementById(id);i.type=i.type==='password'?'text':'password';b.setAttribute('aria-label',i.type==='password'?'Show password':'Hide password');}
function toggleMenu(b){var m=document.getElementById('userMenu');var on=!m.classList.contains('on');m.classList.toggle('on',on);b.setAttribute('aria-expanded',on);}
document.addEventListener('click',function(e){var m=document.getElementById('userMenu');if(m&&!e.target.closest('.menu'))m.classList.remove('on');});
document.addEventListener('keydown',function(e){if(e.key==='Escape'){var m=document.getElementById('userMenu');if(m)m.classList.remove('on');}});
</script>"""


def _page(title: str, body: str, wide: bool = False, user: User | None = None, shell: str = "app") -> str:
    head = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{_esc(title)} · Muse ServiceDesk</title>'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><style>{CSS}</style></head>')
    if shell == "auth":
        return (f'{head}<body class="auth"><div class="card"><div class="brand"><div class="logo">{LOGO}</div>'
                f'<h2>{_esc(title)}</h2></div>{body}'
                f'<p class="foot">Muse ServiceDesk · service desk console</p></div>{JS}</body></html>')
    return (f'{head}<body><header><div class="brand"><a class="logo" href="/" title="Console">{LOGO}</a>'
            f'<h1>Muse ServiceDesk<small>Service desk console</small></h1></div>{user_menu(user)}</header>'
            f'<main><div class="card"><h2>{_esc(title)}</h2>{body}</div></main>{JS}</body></html>')


def user_menu(user: User | None) -> str:
    """Avatar + dropdown shared by the console and the app pages."""
    if user is None:
        return ""
    admin = user.role == "admin"
    return (f'<div class="menu"><button class="avatar" id="avatarBtn" aria-haspopup="menu" aria-expanded="false" '
            f'onclick="toggleMenu(this)">{_initials(user.name)}</button>'
            f'<div class="dropdown" id="userMenu" role="menu">'
            f'<div class="dd-head"><b>{_esc(user.name)}</b><small>{_esc(user.email)} · {user.role}</small></div>'
            f'<a href="/" role="menuitem">Console</a><a href="/profile" role="menuitem">Profile &amp; password</a>'
            + (f'<a href="/users" role="menuitem">Desk users</a><a href="/audit" role="menuitem">Audit log</a>' if admin else "")
            + f'<div class="dd-sep"></div><a href="/logout" role="menuitem">Sign out</a></div></div>')
