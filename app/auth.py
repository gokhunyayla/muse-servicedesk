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
    """SMTP from Settings → Reports (or .env); otherwise the message goes to the log so development still works."""
    from app.reports import smtp_config
    c = smtp_config()
    if not c["host"]:
        logger.warning("SMTP not configured; mail to %s: %s\n%s", to, subject, body)
        return False
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = c["from"] or c["user"], to, subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(c["host"], int(c["port"]), timeout=20) as s:
            if c.get("tls", True):
                s.starttls()
            if c["user"]:
                s.login(c["user"], c["password"])
            s.send_message(msg)
        return True
    except Exception as e:
        logger.error("mail to %s failed: %s", to, e)
        return False


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
        rows = ""
        for u in db.query(User).order_by(User.id):
            if u.id == admin.id:
                actions = "<span class='muted'>you</span>"
            else:
                actions = f"""<div class="menu"><button class="kebab" onclick="toggleRow(this)" aria-label="Actions">⋯</button>
<div class="dropdown right"><form method="post" action="/users/{u.id}/role"><input type="hidden" name="role" value="{'agent' if u.role == 'admin' else 'admin'}"><button>{'Make support agent' if u.role == 'admin' else 'Make admin'}</button></form>
<form method="post" action="/users/{u.id}/invite"><button>Resend invitation</button></form><div class="dd-sep"></div>
<form method="post" action="/users/{u.id}/toggle"><button class="{'danger' if u.active else ''}">{'Disable account' if u.active else 'Enable account'}</button></form></div></div>"""
            state = "invited" if not u.password_hash else ("active" if u.active else "disabled")
            rows += (f"<tr><td><div class='who'><span class='avatar sm'>{_initials(u.name)}</span><div><b>{_esc(u.name)}</b>"
                     f"<small>{_esc(u.email)}</small></div></div></td><td><span class='tag'>{u.role}</span></td>"
                     f"<td><span class='tag {state}'>{state}</span></td><td class='mono muted'>{u.created_at:%d %b %Y}</td><td class='act'>{actions}</td></tr>")
    body = f"""
<div class="pagehead"><p class="muted">People who may sign in to this console. Employees never sign in; they use WhatsApp.</p>
<button class="btn primary slim" onclick="openModal('inviteModal')">+ Invite user</button></div>
{_alert(error, info)}
<div class="scroll"><table><thead><tr><th>User</th><th>Role</th><th>Status</th><th>Since</th><th></th></tr></thead><tbody>{rows}</tbody></table></div>
<div class="modal {'on' if error else ''}" id="inviteModal"><div class="box"><h3>Invite a desk user</h3>
<p class="muted">They receive a link to set their own password (valid 24 hours). You never type or e-mail a password.</p>
<form method="post" action="/users">
<label for="iname">Name</label><input id="iname" name="name" required>
<label for="imail">E-mail</label><input id="imail" name="email" type="email" required>
<label for="irole">Role</label><select id="irole" name="role"><option value="agent">Support agent</option><option value="admin">Admin</option></select>
<div class="row end"><button type="button" class="btn" onclick="closeModal('inviteModal')">Cancel</button><button class="btn primary slim" type="submit">Send invitation</button></div></form></div></div>
<div class="modal {'on' if link else ''}" id="linkModal"><div class="box"><h3>Invitation link</h3>
<p class="muted">SMTP is not configured, so the e-mail was not sent. Share this link with them yourself; it is valid 24 hours.</p>
<input value="{_esc(link)}" readonly onclick="this.select()">
<div class="row end"><button type="button" class="btn" onclick="navigator.clipboard&&navigator.clipboard.writeText('{_esc(link)}');this.textContent='Copied'">Copy</button><button type="button" class="btn primary slim" onclick="closeModal('linkModal')">Done</button></div></div></div>"""
    return _page("Desk users", body, wide=True, user=admin, active="users")


@router.post("/users/{user_id}/invite")
def reinvite(request: Request, user_id: int, admin: User = Depends(admin_user)):
    check_origin(request)
    with session() as db:
        u = db.get(User, user_id)
        if u is None:
            raise HTTPException(404)
        u.reset_token, u.reset_expires = secrets.token_urlsafe(32), _now() + timedelta(hours=24)
        link = f"{auth_settings.public_url}/reset?token={u.reset_token}"
        mailed = send_mail(u.email, "Muse ServiceDesk: set your password",
                           f"Hello {u.name},\n\nSet your password here (valid 24 hours):\n{link}\n")
        audit(db, admin.email, "user.reinvited", target=u.email, ip=_ip(request))
    return RedirectResponse("/users?info=Invitation+sent" + ("" if mailed else f"&link={link}"), 303)


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
    return _page("Profile", body, wide=True, user=user, active="")


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
                 wide=True, user=admin, active="audit")


@router.get("/audit.json")
def audit_json(admin: User = Depends(admin_user), limit: int = 200) -> list[dict]:
    from app.db import Audit
    with session() as db:
        return [{"at": r.at.isoformat(), "user": r.user, "action": r.action, "target": r.target, "detail": r.detail, "ip": r.ip}
                for r in db.query(Audit).order_by(Audit.id.desc()).limit(min(limit, 1000))]


def _initials(name: str) -> str:
    parts = [p for p in name.split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "?"


# ---- settings (tabbed; SLA first) -----------------------------------------------------------

SLA_FIELDS = [("first_response", "First response", "Time until the employee gets the first reply"),
              ("L1", "L1 · first contact", "Time L1 may hold a ticket"), ("L2", "L2 · devices & network", ""),
              ("L3", "L3 · servers & security", ""), ("L4", "L4 · vendors & logistics", ""),
              ("human", "Human engineer", "Time an escalated ticket may wait for a person"),
              ("total", "Total resolution", "Open → resolved, excluding time waiting for the employee"),
              ("at_risk_pct", "At-risk threshold (%)", "The Supervisor acts when a ticket reaches this share of a target")]


@router.get("/settings", response_class=HTMLResponse)
def settings_page(user: User = Depends(current_user), tab: str = "sla", info: str = "") -> str:
    from app.sla import sla_targets
    admin = user.role == "admin"
    tabs = [("sla", "SLA"), ("supervisor", "Supervisor"), ("reports", "Reports"), ("connectors", "Connectors"), ("channels", "Channels"), ("models", "Models"), ("about", "About")]
    nav = "".join(f'<a class="tab {"on" if tab == k else ""}" href="/settings?tab={k}">{v}</a>' for k, v in tabs)
    if tab == "sla":
        t = sla_targets()
        rows = "".join(f'<div class="srow"><div><b>{label}</b><small>{hint}</small></div>'
                       f'<div class="min"><input name="{k}" type="number" min="1" {"max=\"100\"" if k == "at_risk_pct" else ""} step="1" value="{t.get(k, "")}" {"" if admin else "disabled"}> <span>{"%" if k == "at_risk_pct" else "min"}</span></div></div>'
                       for k, label, hint in SLA_FIELDS)
        body = f"""<p class="muted">Targets in minutes. Tickets show green/red against these on their Timing panel.</p>
<form method="post" action="/settings/sla">{rows}{_alert('', info)}
{('<div class="row end"><button class="btn primary slim" type="submit">Save SLA targets</button></div>' if admin else '<p class="muted">Only admins can change these.</p>')}</form>"""
    elif tab == "supervisor":
        from app.supervisor import config
        c = config()
        def num(k, label, hint, unit="min"):
            return (f'<div class="srow"><div><b>{label}</b><small>{hint}</small></div><div class="min">'
                    f'<input name="{k}" type="number" min="1" step="1" value="{c.get(k, "")}" {"" if admin else "disabled"}> <span>{unit}</span></div></div>')
        def chk(k, label, hint):
            return (f'<div class="srow"><div><b>{label}</b><small>{hint}</small></div><label class="check" style="margin:0">'
                    f'<input type="checkbox" name="{k}" value="1" {"checked" if c.get(k) else ""} {"" if admin else "disabled"}></label></div>')
        body = f"""<p class="muted">The Supervisor reviews every open ticket on a schedule: merges emerging problems, finds where the
agents fail, researches the gap on the web and drafts a SOP with Muse Wonder, or asks a human for exactly what is missing.</p>
<form method="post" action="/settings/supervisor">
<div class="srow"><div><b>Mode</b><small>Manual: runs only when you press Run now. Auto: runs on the cadence below.</small></div>
<select name="mode" style="width:160px" {"" if admin else "disabled"}><option value="manual" {"selected" if c.get("mode","manual")=="manual" else ""}>Manual</option><option value="auto" {"selected" if c.get("mode")=="auto" else ""}>Automatic</option></select></div>
{num('every_minutes', 'Run every', 'Cadence of the review pass (automatic mode)')}
{num('window_hours', 'Problem window', 'Look back this far for similar tickets', 'h')}
{num('cluster_min', 'Problem threshold', 'Similar tickets from different employees that make a problem record', 'tickets')}
{num('stuck_minutes', 'Stuck after', 'Open ticket with no activity for this long counts as failing')}
{num('max_research', 'Research per run', 'Web research tasks per pass', 'tasks')}
{chk('auto_publish_sops', 'Auto-publish SOPs', 'Otherwise drafts wait for an admin under Knowledge')}
<div class="srow"><div><b>Search provider</b><small>ddg needs no key; tavily uses TAVILY_API_KEY from .env</small></div>
<select name="search_provider" style="width:160px" {"" if admin else "disabled"}><option value="ddg" {"selected" if c.get("search_provider")=="ddg" else ""}>DuckDuckGo</option><option value="tavily" {"selected" if c.get("search_provider")=="tavily" else ""}>Tavily</option></select></div>
{_alert('', info)}
{('<div class="row end"><button class="btn" type="button" onclick="runNow(this)">Run now</button><button class="btn primary slim" type="submit">Save</button></div>' if admin else '<p class="muted">Only admins can change these.</p>')}</form>
<pre id="runOut" class="muted" style="white-space:pre-wrap;display:none"></pre>
<script>async function runNow(b){{b.disabled=true;b.textContent='Running…';const o=document.getElementById('runOut');o.style.display='';o.textContent='The Supervisor is reviewing the desk…';
 try{{const r=await (await fetch('/supervisor/run',{{method:'POST'}})).json();o.textContent=(r.error?'Error: '+r.error+String.fromCharCode(10):'')+r.summary+String.fromCharCode(10,10)+'Actions: '+r.actions.map(a=>a.tool+(a.ok?' ✓':' ✗')).join(', ');}}catch(e){{o.textContent='Failed: '+e}}
 b.disabled=false;b.textContent='Run now';}}</script>"""
    elif tab == "reports":
        from app.db import Report
        from app.reports import config as rcfg, smtp_config
        c, sm = rcfg(), smtp_config()
        dis = "" if admin else "disabled"
        days = "".join(f'<option value="{i}" {"selected" if int(c.get("weekly_day", 1)) == i else ""}>{d}</option>'
                       for i, d in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], 1))
        with session() as db:
            hist = "".join(f"<tr><td>{r.created_at:%d %b %H:%M}</td><td><span class='tag'>{r.kind}</span></td><td>{_esc(r.recipients) or '—'}</td>"
                           f"<td><span class='tag {'active' if r.sent else 'disabled'}'>{'sent' if r.sent else 'not sent'}</span>{(' <small class=muted>' + _esc(r.error) + '</small>') if r.error else ''}</td>"
                           f"<td><a href='/reports/{r.id}' target='_blank'>open</a></td></tr>"
                           for r in db.query(Report).order_by(Report.id.desc()).limit(10))
        body = f"""<p class="muted">Muse Wonder writes a short narrative over the desk's numbers; it goes out as an HTML e-mail on the schedule below (desk time zone).</p>
<form method="post" action="/settings/reports">
<div class="srow"><div><b>Delivery</b><small>Where the reports go</small></div><div class="seg2">
<label class="check"><input type="radio" name="channel" value="email" {"checked" if c.get("channel","email")=="email" else ""} {dis}> Email</label>
<label class="check"><input type="radio" name="channel" value="whatsapp" {"checked" if c.get("channel")=="whatsapp" else ""} {dis}> WhatsApp</label>
<label class="check"><input type="radio" name="channel" value="both" {"checked" if c.get("channel")=="both" else ""} {dis}> Both</label></div></div>
<div class="srow"><div><b>E-mail recipients</b><small>Comma-separated addresses</small></div><input name="recipients" style="width:360px" value="{_esc(c.get('recipients',''))}" placeholder="it-manager@company.com, ciso@company.com" {dis}></div>
<div class="srow"><div><b>WhatsApp recipients</b><small>Comma-separated numbers with country code. Free-form messages reach a number only within 24 h of its last message to the desk; outside that window Meta requires an approved template.</small></div><input name="whatsapp_recipients" style="width:360px" value="{_esc(c.get('whatsapp_recipients',''))}" placeholder="+905551234567" {dis}></div>
<div class="srow"><div><b>Language</b></div><select name="language" style="width:140px" {dis}><option value="en" {"selected" if c.get("language")=="en" else ""}>English</option><option value="tr" {"selected" if c.get("language")=="tr" else ""}>Türkçe</option></select></div>
<div class="srow"><div><b>Daily</b><small>End-of-day report</small></div><div class="min"><label class="check" style="margin:0"><input type="checkbox" name="daily" value="1" {"checked" if c.get("daily") else ""} {dis}></label> <input name="daily_at" type="time" value="{_esc(c.get('daily_at','18:00'))}" style="width:120px" {dis}></div></div>
<div class="srow"><div><b>Weekly</b><small>Last 7 days</small></div><div class="min"><label class="check" style="margin:0"><input type="checkbox" name="weekly" value="1" {"checked" if c.get("weekly") else ""} {dis}></label> <select name="weekly_day" style="width:90px" {dis}>{days}</select> <input name="weekly_at" type="time" value="{_esc(c.get('weekly_at','09:00'))}" style="width:120px" {dis}></div></div>
<div class="srow"><div><b>Monthly</b><small>Last 30 days</small></div><div class="min"><label class="check" style="margin:0"><input type="checkbox" name="monthly" value="1" {"checked" if c.get("monthly") else ""} {dis}></label> day <input name="monthly_day" type="number" min="1" max="28" value="{int(c.get('monthly_day',1))}" style="width:70px" {dis}> <input name="monthly_at" type="time" value="{_esc(c.get('monthly_at','09:00'))}" style="width:120px" {dis}></div></div>
<h3>SMTP</h3>
<div class="grid">
<div><label>Host</label><input name="smtp_host" value="{_esc(sm.get('host',''))}" placeholder="smtp.office365.com" {dis}></div>
<div><label>Port</label><input name="smtp_port" type="number" value="{int(sm.get('port',587))}" {dis}></div>
<div><label>User</label><input name="smtp_user" value="{_esc(sm.get('user',''))}" autocomplete="off" {dis}></div>
<div><label>Password <small>(leave blank to keep)</small></label><input name="smtp_password" type="password" autocomplete="new-password" {dis}></div>
<div><label>From</label><input name="smtp_from" value="{_esc(sm.get('from',''))}" placeholder="servicedesk@company.com" {dis}></div>
<div><label>STARTTLS</label><label class="check"><input type="checkbox" name="smtp_tls" value="1" {"checked" if sm.get("tls", True) else ""} {dis}> use STARTTLS</label></div>
</div>
{_alert('', info)}
{('<div class="row end"><button class="btn" type="button" onclick="sendTest(this)">Send test report</button><button class="btn primary slim" type="submit">Save</button></div>' if admin else '<p class="muted">Only admins can change these.</p>')}</form>
<pre id="repOut" class="muted" style="white-space:pre-wrap;display:none"></pre>
<h3>Recent reports</h3><div class="scroll"><table><thead><tr><th>When</th><th>Kind</th><th>To</th><th>Status</th><th></th></tr></thead><tbody>{hist or '<tr><td colspan=5 class=muted>none yet</td></tr>'}</tbody></table></div>
<script>async function sendTest(b){{b.disabled=true;b.textContent='Sending…';const o=document.getElementById('repOut');o.style.display='';o.textContent='Writing the daily report with Muse Wonder…';
 try{{const r=await (await fetch('/reports/send',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{kind:'daily'}})}})).json();
 o.textContent=(r.sent?'Sent: ':'Not sent ('+r.error+'): ')+r.subject+String.fromCharCode(10,10)+r.narrative;}}catch(e){{o.textContent='Failed: '+e}}b.disabled=false;b.textContent='Send test report';}}</script>"""
    elif tab == "connectors":
        from app import connectors
        rows = connectors.listing()
        def ccard(c):
            st = ('<span class="tag active">connected</span>' if c["last_test_ok"] else ('<span class="tag disabled">failed</span>' if c["last_test_ok"] is False else '<span class="tag">untested</span>'))
            tools = ", ".join(c["tools"][:12]) + (f" … +{len(c['tools']) - 12}" if len(c["tools"]) > 12 else "")
            return f"""<div class="draft" data-id="{c['id']}"><div class="dh"><span class="tag">{c['kind']}</span><b>{_esc(c['label'])}</b><code>mcp.{_esc(c['name'])}.*</code>{st}{'' if c['enabled'] else '<span class="tag disabled">disabled</span>'}
<small>{_esc(c['url'] or c['command'])} · tiers {', '.join(c['tiers']) or '—'} · {'secrets set' if c['has_secrets'] else 'no secrets'}{(' · tested ' + c['last_test_at'][:16].replace('T', ' ')) if c['last_test_at'] else ''}</small></div>
{('<p class="muted" style="margin:6px 0 0">Tools: ' + _esc(tools) + '</p>') if c['tools'] else ''}{('<p class="alert bad" style="margin-top:6px">' + _esc(c['last_error']) + '</p>') if c['last_error'] else ''}
<div class="row end" style="margin-top:8px">{('<button class="btn" onclick="testConn(' + str(c['id']) + ',this)">Test</button> <button class="btn" onclick="editConn(' + str(c['id']) + ')">Edit</button> <button class="btn" onclick="toggleConn(' + str(c['id']) + ',' + ('false' if c['enabled'] else 'true') + ')">' + ('Disable' if c['enabled'] else 'Enable') + '</button> <button class="btn" onclick="delConn(' + str(c['id']) + ')">Remove</button>') if admin else ''}</div></div>"""
        body = f"""<p class="muted">MCP servers the agents may use as tools: Microsoft 365 / Azure, Cisco, firewalls, ITSM — anything with an MCP endpoint. A connector's tools appear to the chosen tiers as <code>mcp.&lt;name&gt;.&lt;tool&gt;</code>; every call lands on the ticket trail. Credentials are stored encrypted.</p>
<div class="pagehead" style="margin-bottom:10px"><small class="muted">{len(rows)} connector(s)</small>{('<button class="btn primary slim" onclick="newConn()">+ Add connector</button>') if admin else ''}</div>
{''.join(ccard(c) for c in rows) or '<p class="muted">No connectors yet.</p>'}
<div class="modal" id="connModal"><div class="box" style="width:min(680px,100%)"><h3 id="cTitle">Add connector</h3>
<label>Start from</label><select id="cPreset" onchange="applyPreset()"></select><p class="muted" id="cHint" style="margin:4px 0 0"></p>
<div class="grid"><div><label>Name (slug)</label><input id="cName" placeholder="m365"></div><div><label>Label</label><input id="cLabel" placeholder="Microsoft 365"></div>
<div><label>Transport</label><select id="cKind" onchange="kindUI()"><option value="http">HTTP (streamable) URL</option><option value="stdio">Local command (stdio)</option></select></div>
<div><label>Allowed tiers</label><div class="seg2" id="cTiers">{''.join(f'<label class="check"><input type="checkbox" value="{t}" checked> {t}</label>' for t in ("L1", "L2", "L3", "L4"))}<label class="check"><input type="checkbox" value="DESK"> Desk Chat</label><label class="check"><input type="checkbox" value="SUP"> Supervisor</label></div></div></div>
<div id="cUrlRow"><label>Endpoint URL</label><input id="cUrl" placeholder="https://host/mcp"></div>
<div id="cCmdRow" style="display:none"><label>Command</label><input id="cCmd" placeholder="npx -y some-mcp-server"></div>
<label>Secrets <small>(one per line, KEY=value — sent as request headers for HTTP, as environment for stdio; stored encrypted, never shown again)</small></label><textarea id="cSecrets" rows="3" placeholder="Authorization=Bearer …"></textarea>
<div class="row end"><button type="button" class="btn" onclick="closeModal('connModal')">Cancel</button><button type="button" class="btn primary slim" id="cGo" onclick="saveConn()">Save &amp; test</button></div></div></div>
<script>
const CONNS={__import__("json").dumps({c["id"]: c for c in rows})};let PRESETS={{}},editingConn=null;
fetch('/connectors/presets').then(r=>r.json()).then(p=>{{PRESETS=p;const s=document.getElementById('cPreset');s.innerHTML=Object.entries(p).map(([k,v])=>`<option value="${{k}}">${{v.label}}</option>`).join('');}});
function kindUI(){{const k=document.getElementById('cKind').value;document.getElementById('cUrlRow').style.display=k==='http'?'':'none';document.getElementById('cCmdRow').style.display=k==='stdio'?'':'none';}}
function applyPreset(){{const p=PRESETS[document.getElementById('cPreset').value];if(!p)return;document.getElementById('cHint').textContent=p.hint||'';document.getElementById('cKind').value=p.kind;document.getElementById('cUrl').value=p.url||'';document.getElementById('cCmd').value=p.command||'';if(!editingConn){{document.getElementById('cLabel').value=p.label;document.getElementById('cName').value=document.getElementById('cPreset').value==='custom'?'':document.getElementById('cPreset').value;}}kindUI();}}
function newConn(){{editingConn=null;document.getElementById('cTitle').textContent='Add connector';document.getElementById('cSecrets').value='';document.getElementById('cPreset').value='custom';applyPreset();openModal('connModal');}}
function editConn(id){{const c=CONNS[id];editingConn=id;document.getElementById('cTitle').textContent='Edit '+c.label;document.getElementById('cName').value=c.name;document.getElementById('cLabel').value=c.label;document.getElementById('cKind').value=c.kind;document.getElementById('cUrl').value=c.url;document.getElementById('cCmd').value=c.command;document.getElementById('cSecrets').value='';
 document.querySelectorAll('#cTiers input').forEach(i=>i.checked=c.tiers.includes(i.value));document.getElementById('cHint').textContent=c.has_secrets?'Secrets are set; leave the box empty to keep them.':'';kindUI();openModal('connModal');}}
function tiers(){{return [...document.querySelectorAll('#cTiers input:checked')].map(i=>i.value);}}
function secrets(){{const o={{}};document.getElementById('cSecrets').value.split(String.fromCharCode(10)).forEach(l=>{{const i=l.indexOf('=');if(i>0)o[l.slice(0,i).trim()]=l.slice(i+1).trim();}});return o;}}
async function saveConn(){{const b=document.getElementById('cGo');b.disabled=true;b.textContent='Connecting…';
 const body={{name:document.getElementById('cName').value.trim(),label:document.getElementById('cLabel').value.trim(),kind:document.getElementById('cKind').value,url:document.getElementById('cUrl').value.trim(),command:document.getElementById('cCmd').value.trim(),secrets:secrets(),tiers:tiers(),enabled:true}};
 const r=editingConn?await fetch('/connectors/'+editingConn,{{method:'PUT',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}}):await fetch('/connectors',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(body)}});
 b.disabled=false;b.textContent='Save & test';if(r.ok)location.reload();else alert('Failed: '+(await r.text()));}}
async function testConn(id,b){{b.disabled=true;b.textContent='Testing…';const r=await (await fetch('/connectors/'+id+'/test',{{method:'POST'}})).json();alert(r.ok?('Connected. Tools: '+r.tools.join(', ')):('Failed: '+r.error));location.reload();}}
async function toggleConn(id,on){{const c=CONNS[id];await fetch('/connectors/'+id,{{method:'PUT',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{name:c.name,label:c.label,kind:c.kind,url:c.url,command:c.command,secrets:{{}},tiers:c.tiers,enabled:on}})}});location.reload();}}
async function delConn(id){{if(!confirm('Remove this connector?'))return;await fetch('/connectors/'+id,{{method:'DELETE'}});location.reload();}}
</script>"""
    elif tab in ("channels", "models"):
        from app import vault
        from app.config import settings as cfg
        dis = "" if admin else "disabled"
        def secret_row(name, hint=""):
            st = vault.status(name)
            masked = name in vault.SECRET
            state = (f'<span class="tag active">configured</span> <small class="muted">{"…" + _esc(st["hint"]) if masked else _esc(st["hint"])} · from {st["source"]}</small>'
                     if st["configured"] else '<span class="tag disabled">not set</span>')
            shown = ("" if not st["configured"] else ("•" * 28 + st["hint"] if masked else st["hint"]))
            return (f'<div class="srow"><div><b>{_esc(st["label"])}</b><small>{_esc(hint)}</small><div style="margin-top:4px">{state}</div></div>'
                    f'<div class="min"><input name="{name}" type="text" autocomplete="off" spellcheck="false" value="{_esc(shown)}" data-shown="{_esc(shown)}" '
                    f'onfocus="if(this.value===this.dataset.shown)this.value=\'\'" onblur="if(!this.value)this.value=this.dataset.shown" '
                    f'placeholder="{"paste a new key to replace" if masked else "value"}" style="width:260px;font-family:ui-monospace,Menlo,monospace;font-size:12px" {dis}>'
                    + (f'<label class="check" style="margin:0 0 0 8px"><input type="checkbox" name="clear_{name}" value="1" {dis}> clear</label>' if st["source"] == "console" else "")
                    + '</div></div>')
        if tab == "models":
            rows = secret_row("META_API_KEY", "From dev.meta.ai; used by every agent, voice, vision and SAM call")
            body = f"""<p class="muted">Keys live encrypted in the database and are managed here; <code>.env</code> is only a fallback for first run.</p>
<form method="post" action="/settings/secrets?tab=models">{rows}
<dl class="kv" style="margin-top:14px"><dt>Agents</dt><dd>{_esc(cfg.meta_model)}</dd><dt>Heavy (L3, Supervisor, reports)</dt><dd>{_esc(cfg.meta_model_heavy)}</dd><dt>Reasoning effort</dt><dd>{_esc(cfg.reasoning_effort)}</dd><dt>Base URL</dt><dd>{_esc(cfg.meta_base_url)}</dd></dl>
{_alert('', info)}{('<div class="row end"><button class="btn primary slim" type="submit">Save</button></div>' if admin else '')}</form>"""
        else:
            rows = (secret_row("WA_PHONE_NUMBER_ID", "Meta for Developers → WhatsApp → API Setup") + secret_row("WA_ACCESS_TOKEN", "System-user token (never expires)")
                    + secret_row("WA_VERIFY_TOKEN", "Must match the webhook configuration at Meta") + secret_row("DEMO_MANAGER_PHONE", "Seeded managers E1002/E1003 answer approvals from this number")
                    + secret_row("API_KEY", "Required as X-API-Key on /messages, /voice, /image, … when set") + secret_row("TAVILY_API_KEY", "Optional; DuckDuckGo needs no key"))
            body = f"""<p class="muted">Channel and integration secrets, encrypted in the database. Changes apply immediately, no restart.</p>
<form method="post" action="/settings/secrets?tab=channels">{rows}{_alert('', info)}{('<div class="row end"><button class="btn primary slim" type="submit">Save</button></div>' if admin else '')}</form>"""
    else:
        body = '<p class="muted">Muse ServiceDesk — autonomous multi-tier IT service desk on Meta Model API. Built for the Meta Global AI Developer Hackathon 2026.</p>'
    return _page("Settings", f'<div class="tabs">{nav}</div>{body}', wide=True, user=user, active="settings")


@router.post("/settings/supervisor")
async def settings_supervisor(request: Request, admin: User = Depends(admin_user)):
    from app.sla import put_setting
    from app.supervisor import DEFAULT_SUPERVISOR
    check_origin(request)
    form = await request.form()
    value = dict(DEFAULT_SUPERVISOR)
    for k in ("every_minutes", "window_hours", "cluster_min", "stuck_minutes", "max_research"):
        try:
            value[k] = max(1, int(form.get(k, DEFAULT_SUPERVISOR[k])))
        except (TypeError, ValueError):
            pass
    value["mode"] = "auto" if form.get("mode") == "auto" else "manual"
    value["auto_publish_sops"] = form.get("auto_publish_sops") == "1"
    value["search_provider"] = form.get("search_provider") if form.get("search_provider") in ("ddg", "tavily") else "ddg"
    put_setting("supervisor", value)
    with session() as db:
        audit(db, admin.email, "settings.supervisor", detail=str(value), ip=_ip(request))
    return RedirectResponse("/settings?tab=supervisor&info=Supervisor+settings+saved", 303)


@router.get("/chat", response_class=HTMLResponse)
def chat_page(user: User = Depends(current_user)) -> str:
    """Talk to the desk's brain: status, who holds what, act with your authority."""
    from app.auth import RAIL_CSS, rail
    head = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Desk Chat · Muse ServiceDesk</title>'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><style>{CSS}{CHAT_CSS}</style></head>')
    return rf"""{head}<body class="app">{rail(user, "chat")}<div class="page chatpage">
<aside class="threads"><div class="th-head"><b>Desk Chat</b><button class="btn primary slim" onclick="newThread()">+ New</button></div><div id="threads"></div></aside>
<section class="conv"><div id="msgs" class="msgs"><div class="hello"><h2>Ask the desk</h2><p class="muted">I know every ticket, who holds what, SLA risk, approvals, drafts and the Supervisor's work — and I can act for you.</p>
<div class="sugg"><button onclick="say('What is the desk working on right now?')">What is the desk working on right now?</button><button onclick="say('Which tickets are at risk of breaching SLA and what should I do?')">Which tickets are at SLA risk?</button><button onclick="say('Is there anything waiting for my decision?')">Anything waiting for me?</button><button onclick="say('Şu an açık biletleri ve sahiplerini özetle')">Açık biletleri özetle</button></div></div></div>
<form class="composer" onsubmit="send(event)"><textarea id="box" rows="2" placeholder="Ask anything about the desk, or tell me what to do… (Enter to send, Shift+Enter for a new line)" onkeydown="if(event.key==='Enter'&&!event.shiftKey){{event.preventDefault();send(event)}}"></textarea><button class="btn primary" id="sendBtn" type="submit">Send</button></form></section></div>
{JS}<script>
let cur=null;const $=s=>document.querySelector(s);const esc=x=>String(x).replace(/&/g,'&amp;').replace(/</g,'&lt;');
function md(t){{let h=esc(t);h=h.replace(/\[([^\]]+)\]\((\/[^)\s]*)\)/g,'<a href="$2">$1</a>').replace(/(^|[\s(])#(\d+)(?![\w-])/g,'$1<a href="/tickets?id=$2">#$2</a>')
 .replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>').replace(/`([^`]+)`/g,'<code>$1</code>');
 const lines=h.split('\n');let out='',inList=false;for(const l of lines){{const m=/^\s*[-*•]\s+(.*)$/.exec(l);if(m){{if(!inList){{out+='<ul>';inList=true}}out+='<li>'+m[1]+'</li>';}}else{{if(inList){{out+='</ul>';inList=false}}if(/^\s*\d+[.)]\s+/.test(l))out+='<div>'+l+'</div>';else if(l.trim()==='')out+='<div class="gap"></div>';else out+='<div>'+l+'</div>';}}}}if(inList)out+='</ul>';return out;}}
async function loadThreads(){{const ts=await (await fetch('/chat/threads')).json();$('#threads').innerHTML=ts.map(t=>`<a class="thread ${{cur===t.id?'on':''}}" href="#" onclick="openThread(${{t.id}});return false">${{esc(t.title)}}<small>${{new Date(t.updated_at).toLocaleString([], {{dateStyle:'short',timeStyle:'short'}})}}</small><span class="x" onclick="event.preventDefault();event.stopPropagation();delThread(${{t.id}})" title="Delete">×</span></a>`).join('')||'<small class="muted" style="padding:8px;display:block">No conversations yet.</small>';}}
async function newThread(){{const t=await (await fetch('/chat/threads',{{method:'POST'}})).json();cur=t.id;await loadThreads();$('#msgs').innerHTML='';$('#box').focus();}}
async function openThread(id){{cur=id;const ms=await (await fetch('/chat/threads/'+id)).json();$('#msgs').innerHTML=ms.map(render).join('');$('#msgs').scrollTop=1e9;loadThreads();}}
async function delThread(id){{if(!confirm('Delete this conversation?'))return;await fetch('/chat/threads/'+id,{{method:'DELETE'}});if(cur===id){{cur=null;$('#msgs').innerHTML='';}}loadThreads();}}
function render(m){{const acts=(m.actions||[]).length?`<div class="acts">${{m.actions.map(a=>`<span class="chip ${{a.ok?'':'err'}}">${{esc(a.tool)}}${{a.args&&a.args.ticket_id?' #'+a.args.ticket_id:''}}</span>`).join('')}}</div>`:'';
 return `<div class="m ${{m.role}}"><div class="bubble">${{m.role==='assistant'?md(m.content):esc(m.content)}}${{acts}}</div></div>`;}}
function say(t){{$('#box').value=t;send();}}
async function send(ev){{if(ev)ev.preventDefault();const text=$('#box').value.trim();if(!text)return;if(!cur)await newThread();
 $('#msgs').insertAdjacentHTML('beforeend',render({{role:'user',content:text}}));$('#box').value='';$('#sendBtn').disabled=true;
 $('#msgs').insertAdjacentHTML('beforeend','<div class="m assistant" id="typing"><div class="bubble muted">thinking with Muse Wonder…</div></div>');$('#msgs').scrollTop=1e9;
 try{{const r=await (await fetch('/chat/threads/'+cur,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{text}})}})).json();
  document.getElementById('typing').remove();$('#msgs').insertAdjacentHTML('beforeend',render({{role:'assistant',content:r.answer||r.detail||'(no answer)',actions:r.actions||[]}}));}}
 catch(e){{document.getElementById('typing').remove();$('#msgs').insertAdjacentHTML('beforeend',render({{role:'assistant',content:'Failed: '+e}}));}}
 $('#sendBtn').disabled=false;$('#msgs').scrollTop=1e9;loadThreads();}}
loadThreads();
</script></body></html>"""


CHAT_WIDGET = r"""
<style>
.cw-btn{position:fixed;right:22px;bottom:22px;width:56px;height:56px;border-radius:50%;background:var(--accent);color:#fff;border:0;box-shadow:0 10px 30px rgba(0,0,0,.25);cursor:pointer;display:grid;place-items:center;z-index:40}
.cw-btn svg{width:26px;height:26px;stroke:#fff;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}.cw-btn .dot{position:absolute;top:4px;right:4px;width:12px;height:12px;border-radius:50%;background:var(--bad);display:none}
.cw{position:fixed;right:22px;bottom:90px;width:min(420px,calc(100vw - 32px));height:min(620px,calc(100vh - 120px));background:var(--card);border:1px solid var(--line);border-radius:16px;box-shadow:0 20px 60px rgba(0,0,0,.3);display:none;flex-direction:column;z-index:41;overflow:hidden}
.cw.on{display:flex}.cw-head{display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid var(--line)}.cw-head b{flex:1;font-size:13px}
.cw-head button,.cw-head a{font:inherit;font-size:12px;border:1px solid var(--line);background:transparent;color:var(--fg);border-radius:6px;padding:4px 8px;cursor:pointer;text-decoration:none}
.cw-msgs{flex:1;overflow:auto;padding:12px;display:flex;flex-direction:column;gap:8px}
.cw .m{display:flex}.cw .m.user{justify-content:flex-end}.cw .bubble{max-width:92%;padding:8px 11px;border-radius:12px;font-size:13px;line-height:1.45;overflow-wrap:anywhere}
.cw .m.user .bubble{background:var(--accent);color:#fff;border-bottom-right-radius:4px;white-space:pre-wrap}.cw .m.assistant .bubble{background:var(--bg);border:1px solid var(--line);border-bottom-left-radius:4px}
.cw .bubble ul{margin:4px 0 4px 16px;padding:0}.cw .bubble a{color:var(--accent);text-decoration:none;font-weight:600}.cw .bubble code{font-size:12px;background:var(--card);padding:1px 4px;border-radius:4px}
.cw .acts{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}.cw .chip{font-size:11px;padding:1px 6px;border-radius:999px;border:1px solid var(--line);color:var(--muted)}
.cw-form{display:flex;gap:6px;padding:10px;border-top:1px solid var(--line)}.cw-form textarea{flex:1;font:inherit;font-size:13px;padding:8px 10px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--fg);resize:none}
.cw-form button{font:inherit;font-size:13px;padding:0 14px;border-radius:10px;border:0;background:var(--accent);color:#fff;cursor:pointer}.cw-form button:disabled{opacity:.6}
.cw .hello{color:var(--muted);font-size:13px;padding:6px 4px}.cw .sugg{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}.cw .sugg button{font:inherit;font-size:12px;padding:5px 9px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
@media(max-width:900px){.cw-btn{bottom:76px}.cw{bottom:140px;right:16px}}
</style>
<button class="cw-btn" id="cwBtn" title="Desk Chat" onclick="cwToggle()"><svg viewBox="0 0 24 24"><path d="M21 12a8 8 0 0 1-8 8H7l-4 3v-6.5A8 8 0 0 1 13 4a8 8 0 0 1 8 8z"/><path d="M9 11h6M9 14h4"/></svg><span class="dot" id="cwDot"></span></button>
<div class="cw" id="cw"><div class="cw-head"><b>Desk Chat</b><button onclick="cwNew()" title="New conversation">+ New</button><a href="/chat" title="Open full page">Expand</a><button onclick="cwToggle()" title="Close">×</button></div>
<div class="cw-msgs" id="cwMsgs"></div>
<form class="cw-form" onsubmit="cwSend(event)"><textarea id="cwBox" rows="2" placeholder="Ask the desk… (Enter to send)" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();cwSend(event)}"></textarea><button id="cwSendBtn" type="submit">Send</button></form></div>
<script>
(function(){
const esc=x=>String(x).replace(/&/g,'&amp;').replace(/</g,'&lt;');
function md(t){let h=esc(t);h=h.replace(/\[([^\]]+)\]\((\/[^)\s]*)\)/g,'<a href="$2">$1</a>').replace(/(^|[\s(])#(\d+)(?![\w-])/g,'$1<a href="/tickets?id=$2">#$2</a>').replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>').replace(/`([^`]+)`/g,'<code>$1</code>');
 let out='',inList=false;for(const l of h.split('\n')){const m=/^\s*[-*•]\s+(.*)$/.exec(l);if(m){if(!inList){out+='<ul>';inList=true}out+='<li>'+m[1]+'</li>';}else{if(inList){out+='</ul>';inList=false}out+=l.trim()===''?'<div style="height:5px"></div>':'<div>'+l+'</div>';}}if(inList)out+='</ul>';return out;}
const $=id=>document.getElementById(id);let thread=null,busy=false;
try{thread=+localStorage.getItem('cw.thread')||null;if(localStorage.getItem('cw.open')==='1')$('cw').classList.add('on');}catch(e){}
function render(m){const acts=(m.actions||[]).length?`<div class="acts">${m.actions.map(a=>`<span class="chip">${esc(a.tool)}${a.args&&a.args.ticket_id?' #'+a.args.ticket_id:''}</span>`).join('')}</div>`:'';
 return `<div class="m ${m.role}"><div class="bubble">${m.role==='assistant'?md(m.content):esc(m.content)}${acts}</div></div>`;}
function hello(){$('cwMsgs').innerHTML=`<div class="hello">I know every ticket, who holds what, SLA risk, approvals and the Supervisor's work — and I can act for you.<div class="sugg"><button onclick="cwSay('What is the desk working on right now?')">What is the desk doing?</button><button onclick="cwSay('Is there anything waiting for my decision?')">Waiting for me?</button><button onclick="cwSay('Which tickets are at SLA risk?')">SLA risk?</button></div></div>`;}
async function load(){if(!thread){hello();return;}const r=await fetch('/chat/threads/'+thread);if(!r.ok){thread=null;try{localStorage.removeItem('cw.thread')}catch(e){}hello();return;}
 const ms=await r.json();$('cwMsgs').innerHTML=ms.length?ms.map(render).join(''):'';if(!ms.length)hello();$('cwMsgs').scrollTop=1e9;}
window.cwToggle=function(){const on=$('cw').classList.toggle('on');try{localStorage.setItem('cw.open',on?'1':'0')}catch(e){}if(on){$('cwDot').style.display='none';load();$('cwBox').focus();}};
window.cwNew=async function(){const t=await (await fetch('/chat/threads',{method:'POST'})).json();thread=t.id;try{localStorage.setItem('cw.thread',thread)}catch(e){}hello();};
window.cwSay=function(t){$('cwBox').value=t;cwSend();};
window.cwSend=async function(ev){if(ev)ev.preventDefault();if(busy)return;const text=$('cwBox').value.trim();if(!text)return;if(!thread)await cwNew();
 if($('cwMsgs').querySelector('.hello'))$('cwMsgs').innerHTML='';$('cwMsgs').insertAdjacentHTML('beforeend',render({role:'user',content:text}));$('cwBox').value='';busy=true;$('cwSendBtn').disabled=true;
 $('cwMsgs').insertAdjacentHTML('beforeend','<div class="m assistant" id="cwTyping"><div class="bubble" style="color:var(--muted)">thinking…</div></div>');$('cwMsgs').scrollTop=1e9;
 try{const r=await (await fetch('/chat/threads/'+thread,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text})})).json();
  $('cwTyping').remove();$('cwMsgs').insertAdjacentHTML('beforeend',render({role:'assistant',content:r.answer||r.detail||'(no answer)',actions:r.actions||[]}));if(!$('cw').classList.contains('on'))$('cwDot').style.display='block';}
 catch(e){$('cwTyping').remove();$('cwMsgs').insertAdjacentHTML('beforeend',render({role:'assistant',content:'Failed: '+e}));}
 busy=false;$('cwSendBtn').disabled=false;$('cwMsgs').scrollTop=1e9;};
if($('cw').classList.contains('on'))load();
})();
</script>
"""

CHAT_CSS = """
.chatpage{display:grid;grid-template-columns:260px 1fr;height:100vh}
.threads{border-right:1px solid var(--line);background:var(--card);display:flex;flex-direction:column;min-height:0}
.th-head{display:flex;justify-content:space-between;align-items:center;padding:12px 12px 8px}.th-head .btn{margin:0}
#threads{overflow:auto;padding:0 8px 8px}.thread{display:block;padding:8px 10px;border-radius:8px;color:var(--fg);text-decoration:none;font-size:13px;position:relative;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.thread small{display:block;color:var(--muted);font-size:11px}.thread:hover{background:var(--bg)}.thread.on{background:rgba(8,102,255,.14)}
.thread .x{position:absolute;right:8px;top:8px;color:var(--muted);display:none}.thread:hover .x{display:inline}
.conv{display:flex;flex-direction:column;min-height:0}.msgs{flex:1;overflow:auto;padding:20px 24px;display:flex;flex-direction:column;gap:12px}
.hello{max-width:640px;margin:40px auto;text-align:center}.hello h2{margin:0 0 6px}.sugg{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-top:14px}
.sugg button{font:inherit;font-size:13px;padding:8px 12px;border-radius:999px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}.sugg button:hover{background:var(--bg)}
.m{display:flex}.m.user{justify-content:flex-end}.bubble{max-width:760px;padding:10px 14px;border-radius:14px;font-size:14px;line-height:1.5;overflow-wrap:anywhere}
.m.user .bubble{background:var(--accent);color:#fff;border-bottom-right-radius:4px;white-space:pre-wrap}.m.assistant .bubble{background:var(--card);border:1px solid var(--line);border-bottom-left-radius:4px}
.bubble ul{margin:4px 0 4px 18px;padding:0}.bubble .gap{height:6px}.bubble a{color:var(--accent);text-decoration:none;font-weight:600}.bubble code{font-size:12px;background:var(--bg);padding:1px 5px;border-radius:4px}
.acts{display:flex;flex-wrap:wrap;gap:4px;margin-top:8px}.chip{font-size:11px;padding:1px 7px;border-radius:999px;border:1px solid var(--line);color:var(--muted)}.chip.err{color:var(--bad);border-color:var(--bad)}
.composer{display:flex;gap:8px;padding:12px 24px 16px;border-top:1px solid var(--line);background:var(--card)}.composer textarea{flex:1;font:inherit;padding:10px 12px;border-radius:12px;border:1px solid var(--line);background:var(--bg);color:var(--fg);resize:none}
.composer .btn{margin:0;width:auto}
@media(max-width:900px){.chatpage{grid-template-columns:1fr;height:auto}.threads{max-height:160px}.msgs{min-height:50vh}}
"""


@router.get("/knowledge", response_class=HTMLResponse)
def knowledge_page(user: User = Depends(current_user), tier: str = "", q: str = "") -> str:
    """The knowledge base: browse and edit SOPs per tier; drafts from agents and the Supervisor wait here."""
    from app import knowledge
    admin = user.role == "admin"
    rows = knowledge.listing()
    drafts = [r for r in rows if r["status"] == "draft"]
    live = [r for r in rows if r["status"] != "draft" and (not tier or r["tier"] == tier)
            and (not q or q.lower() in (r["title"] + r["content"]).lower())]
    def card(r, draft=False):
        src = {"seed": "seed", "human": "desk user", "agent": f"agent {r['author']}", "supervisor": "Supervisor"}.get(r["source"], r["source"])
        meta = f"{src} · v{r['version']} · {r['updated_at'][:16].replace('T', ' ')} · used {r['used']}×" + (f" · tickets {', '.join('#' + str(i) for i in r['ticket_ids'])}" if r["ticket_ids"] else "")
        srcs = (' · '.join(f'<a href="{_esc(u)}" target="_blank" rel="noopener">{_esc(u[:50])}</a>' for u in r["sources"])) if r["sources"] else ""
        acts = ""
        if admin or draft is False:
            acts += f'<button class="btn" onclick="editSop(\'{_esc(r["key"])}\')">Edit</button> '
        if admin and draft:
            acts += f'<button class="btn" onclick="setStatus(\'{_esc(r["key"])}\',\'retired\')">Reject</button> <button class="btn primary slim" onclick="setStatus(\'{_esc(r["key"])}\',\'published\')">Publish to {r["tier"]}</button>'
        elif admin and r["status"] == "published":
            acts += f'<button class="btn" onclick="setStatus(\'{_esc(r["key"])}\',\'retired\')">Retire</button>'
        elif admin and r["status"] == "retired":
            acts += f'<button class="btn" onclick="setStatus(\'{_esc(r["key"])}\',\'published\')">Restore</button>'
        return f"""<div class="draft" data-key="{_esc(r['key'])}"><div class="dh"><span class="tag">{r['tier']}</span><b>{_esc(r['title'])}</b>
<span class="tag {'active' if r['status']=='published' else ('invited' if r['status']=='draft' else 'disabled')}">{r['status']}</span><small>{_esc(meta)}</small></div>
<details><summary>Read</summary><pre>{_esc(r['content'])}</pre>{('<small>Sources: ' + srcs + '</small>') if srcs else ''}</details>
<div class="row end" style="margin-top:8px">{acts}</div></div>"""
    chips = "".join(f'<a class="tab {"on" if tier == t else ""}" href="/knowledge?tier={t}&q={_esc(q)}">{t}</a>' for t in ("", "L1", "L2", "L3", "L4", "shared")).replace('href="/knowledge?tier=&', 'href="/knowledge?tier=&').replace('>​</a>', '>All</a>')
    chips = chips.replace(f'<a class="tab {"on" if tier == "" else ""}" href="/knowledge?tier=&q={_esc(q)}"></a>', f'<a class="tab {"on" if tier == "" else ""}" href="/knowledge?q={_esc(q)}">All</a>')
    from app import references
    refs = references.listing()
    def refcard(d):
        kb = f"{d['size']/1024:.0f} KB"
        return f"""<div class="draft"><div class="dh"><span class="tag">{d['scope']}</span><span class="tag {'disabled' if d['enforcement']=='strict' else 'active'}">{d['kind']} · {d['enforcement']}</span><b>{_esc(d['title'])}</b><small>{_esc(d['filename'])} · {kb} · {d['chunks']} passages · {_esc(d['uploaded_by'])} · {d['created_at'][:10]}</small></div>
{('<p class="muted" style="margin:6px 0 0">' + _esc(d['description']) + '</p>') if d['description'] else ''}
<div class="row end" style="margin-top:8px"><a class="btn" href="/references/search?q=&tier={d['scope'] if d['scope']!='shared' else 'L1'}" style="display:none"></a>{('<button class="btn" onclick="removeRef(' + str(d['id']) + ')">Remove</button>') if admin else ''}</div></div>"""
    ref_section = f"""<h3>Reference policies &amp; procedures ({len(refs)})</h3>
<p class="muted">Documents people upload that the agents must follow and may quote: HR/IT policies, compliance rules, vendor procedures (PDF, DOCX, Markdown or text). <b>Strict</b> rules bind the agents even if an employee insists; <b>flexible</b> ones are the default they may deviate from with a recorded reason. Agents cite passages as [REF:doc#n] and never state a rule they cannot quote; the Supervisor checks actions against strict rules.</p>
<div class="pagehead" style="margin-bottom:10px"><div class="tl"><input class="search" id="refq" placeholder="Try a search as an agent would…" style="width:300px" onkeydown="if(event.key==='Enter')refSearch()"><button class="btn" onclick="refSearch()">Search</button></div><button class="btn primary slim" onclick="openModal('refModal')">+ Upload document</button></div>
<div id="refOut"></div>
{''.join(refcard(d) for d in refs) or '<p class="muted">No reference documents yet.</p>'}
<div class="modal" id="refModal"><div class="box"><h3>Upload a reference document</h3>
<form method="post" action="/references" enctype="multipart/form-data" onsubmit="document.getElementById('upBtn').disabled=true;document.getElementById('upBtn').textContent='Uploading…'">
<label>File (PDF, DOCX, Markdown, text — max 25 MB)</label><input type="file" name="file" accept=".pdf,.docx,.md,.markdown,.txt" required>
<label>Title</label><input name="title" placeholder="e.g. Information Security Policy v3">
<div class="grid"><div><label>Kind</label><select name="kind"><option value="policy">Policy · rules the desk must follow</option><option value="guideline">Guideline · recommended practice</option><option value="manual">Manual · vendor / product procedure</option></select></div>
<div><label>Enforcement</label><select name="enforcement"><option value="strict">Strict · binding, even if the employee insists</option><option value="flexible">Flexible · default, may deviate with a reason</option></select></div></div>
<label>Scope</label><select name="scope"><option value="shared">shared · every agent</option><option value="L1">L1</option><option value="L2">L2</option><option value="L3">L3</option><option value="L4">L4</option></select>
<label>Description (optional)</label><input name="description" placeholder="What is this for, who owns it">
<div class="row end"><button type="button" class="btn" onclick="closeModal('refModal')">Cancel</button><button class="btn primary slim" id="upBtn" type="submit">Upload</button></div></form></div></div>"""
    body = f"""<div class="pagehead"><p class="muted">Procedures the agents follow. Every agent searches its own pool and <b>shared</b>; other pools are reachable too. Agents' and the Supervisor's proposals wait here as drafts until an admin publishes them.</p>
<button class="btn primary slim" onclick="openModal('sopModal');newSop()">+ Add SOP</button></div>
{('<h3>Drafts awaiting review (' + str(len(drafts)) + ')</h3>' + ''.join(card(r, True) for r in drafts)) if drafts else ''}
<h3>Knowledge base</h3><div class="tl" style="margin-bottom:10px"><div class="tabs" style="margin:0;border:0">{chips}</div>
<form method="get" action="/knowledge" style="display:inline"><input type="hidden" name="tier" value="{_esc(tier)}"><input class="search" name="q" value="{_esc(q)}" placeholder="Search…" style="width:240px"></form></div>
{''.join(card(r) for r in live) or '<p class="muted">Nothing here yet.</p>'}
{ref_section}
<div class="modal" id="sopModal"><div class="box" style="width:min(720px,100%)"><h3 id="sopTitle">Add a SOP</h3>
<label>Tier</label><select id="sTier"><option value="L1">L1 · first contact</option><option value="L2">L2 · devices &amp; network</option><option value="L3">L3 · servers &amp; security</option><option value="L4">L4 · vendors &amp; logistics</option><option value="shared">shared · every agent</option></select>
<label>Title</label><input id="sTitle" placeholder="Symptom or request, e.g. VPN cannot connect from home">
<label>Procedure</label><textarea id="sBody" rows="12" placeholder="Symptom → checks → fix steps (name the desk's tools/scripts) → when to hand off"></textarea>
<div class="row end"><button type="button" class="btn" onclick="closeModal('sopModal')">Cancel</button><button type="button" class="btn primary slim" id="sGo" onclick="saveSop()">Save</button></div></div></div>
<script>
const SOPS={__import__("json").dumps({r["key"]: {"title": r["title"], "content": r["content"], "tier": r["tier"]} for r in rows})};let editing=null;
function newSop(){{editing=null;document.getElementById('sopTitle').textContent='Add a SOP';document.getElementById('sTier').value='L1';document.getElementById('sTitle').value='';document.getElementById('sBody').value='';}}
function editSop(k){{editing=k;const s=SOPS[k];document.getElementById('sopTitle').textContent='Edit '+k;document.getElementById('sTier').value=s.tier;document.getElementById('sTitle').value=s.title;document.getElementById('sBody').value=s.content;openModal('sopModal');}}
async function saveSop(){{const tier=document.getElementById('sTier').value,title=document.getElementById('sTitle').value.trim(),content=document.getElementById('sBody').value.trim();if(!title||!content)return;
 const r=editing?await fetch('/sops/'+editing,{{method:'PUT',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{title,content,tier}})}})
                :await fetch('/sops',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{tier,title,content}})}});
 if(r.ok)location.reload();else alert('Failed: '+(await r.text()));}}
async function removeRef(id){{if(!confirm('Remove this document from the knowledge base?'))return;const r=await fetch('/references/'+id,{{method:'DELETE'}});if(r.ok)location.reload();else alert('Failed: '+(await r.text()));}}
async function refSearch(){{const q=document.getElementById('refq').value.trim();if(!q)return;const r=await (await fetch('/references/search?q='+encodeURIComponent(q))).json();
 document.getElementById('refOut').innerHTML=r.length?r.map(x=>`<div class="draft"><div class="dh"><span class="tag">REF:${{x.ref}}</span><span class="tag">${{x.kind}} · ${{x.enforcement}}</span><b>${{esc(x.document)}}</b><small>${{x.heading?esc(x.heading)+' · ':''}}${{x.page?'page '+x.page:''}}</small></div><pre>${{esc(x.text)}}</pre></div>`).join(''):'<p class="muted">No passage matches.</p>';}}
function esc(x){{return String(x).replace(/&/g,'&amp;').replace(/</g,'&lt;');}}
async function setStatus(k,status){{const r=await fetch('/sops/'+k+'/status',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{status}})}});if(r.ok)location.reload();else alert('Failed: '+(await r.text()));}}
</script>"""
    return _page("Knowledge", body, wide=True, user=user, active="knowledge")


@router.post("/settings/reports")
async def settings_reports(request: Request, admin: User = Depends(admin_user)):
    from app.reports import DEFAULT_REPORTS
    from app.sla import get_setting, put_setting
    check_origin(request)
    form = await request.form()
    rep = dict(DEFAULT_REPORTS)
    rep["recipients"] = ", ".join(x.strip() for x in str(form.get("recipients", "")).split(",") if x.strip())
    rep["whatsapp_recipients"] = ", ".join(x.strip() for x in str(form.get("whatsapp_recipients", "")).split(",") if x.strip())
    rep["channel"] = form.get("channel") if form.get("channel") in ("email", "whatsapp", "both") else "email"
    rep["language"] = "tr" if form.get("language") == "tr" else "en"
    for k in ("daily", "weekly", "monthly"):
        rep[k] = form.get(k) == "1"
    for k in ("daily_at", "weekly_at", "monthly_at"):
        v = str(form.get(k, DEFAULT_REPORTS[k]))
        rep[k] = v if len(v) == 5 and v[2] == ":" else DEFAULT_REPORTS[k]
    try:
        rep["weekly_day"] = min(7, max(1, int(form.get("weekly_day", 1))))
        rep["monthly_day"] = min(28, max(1, int(form.get("monthly_day", 1))))
    except (TypeError, ValueError):
        pass
    put_setting("reports", rep)
    saved = get_setting("smtp", {})
    smtp = {"host": str(form.get("smtp_host", "")).strip(), "port": int(form.get("smtp_port") or 587),
            "user": str(form.get("smtp_user", "")).strip(), "from": str(form.get("smtp_from", "")).strip(),
            "tls": form.get("smtp_tls") == "1",
            "password": str(form.get("smtp_password") or "") or saved.get("password", "")}
    put_setting("smtp", smtp)
    with session() as db:
        audit(db, admin.email, "settings.reports", detail=f"{rep['recipients']} daily={rep['daily']} weekly={rep['weekly']} monthly={rep['monthly']} smtp={smtp['host']}", ip=_ip(request))
    return RedirectResponse("/settings?tab=reports&info=Report+settings+saved", 303)


@router.post("/settings/secrets")
async def settings_secrets(request: Request, admin: User = Depends(admin_user), tab: str = "channels"):
    from app import vault
    check_origin(request)
    form = await request.form()
    changes: dict = {}
    for name in vault.SPEC:
        val = str(form.get(name, "")).strip()
        if form.get(f"clear_{name}") == "1":
            changes[name] = None
        elif val and not val.startswith("•") and val != vault.get(name):
            changes[name] = val
    if changes:
        vault.set_many(changes)
        with session() as db:
            audit(db, admin.email, "settings.secrets", detail=", ".join(f"{k}:{'cleared' if v is None else 'set'}" for k, v in changes.items()), ip=_ip(request))
    return RedirectResponse(f"/settings?tab={tab}&info={'Saved' if changes else 'Nothing+changed'}", 303)


@router.post("/settings/sla")
async def settings_sla(request: Request, admin: User = Depends(admin_user)):
    from app.sla import DEFAULT_SLA, put_setting
    check_origin(request)
    form = await request.form()
    value = {}
    for k in DEFAULT_SLA:
        try:
            value[k] = max(1, int(form.get(k, DEFAULT_SLA[k])))
        except (TypeError, ValueError):
            value[k] = DEFAULT_SLA[k]
    value["at_risk_pct"] = min(100, value["at_risk_pct"])
    put_setting("sla", value)
    with session() as db:
        audit(db, admin.email, "settings.sla", detail=str(value), ip=_ip(request))
    return RedirectResponse("/settings?tab=sla&info=SLA+targets+saved", 303)


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


ICONS = {
    "chat": '<svg viewBox="0 0 24 24"><path d="M21 12a8 8 0 0 1-8 8H7l-4 3v-6.5A8 8 0 0 1 13 4a8 8 0 0 1 8 8z"/><path d="M9 11h6M9 14h4"/></svg>',
    "dash": '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>',
    "tickets": '<svg viewBox="0 0 24 24"><path d="M4 4h16v16H4z"/><path d="M4 9h16M9 9v11"/></svg>',
    "users": '<svg viewBox="0 0 24 24"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"/></svg>',
    "audit": '<svg viewBox="0 0 24 24"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h8"/></svg>',
    "knowledge": '<svg viewBox="0 0 24 24"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>',
    "settings": '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3h.1a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8v.1a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>',
    "system": '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>',
    "light": '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
    "dark": '<svg viewBox="0 0 24 24"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>',
}


RAIL_CSS = """
/* icon rail */
body.app{display:flex;min-height:100vh}.page{flex:1;min-width:0}
.rail .logo{width:40px;height:40px;border-radius:11px;background:var(--accent);display:grid;place-items:center;flex:none;text-decoration:none}
.rail .logo svg{width:22px;height:22px;stroke:#fff;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.rail{position:sticky;top:0;height:100vh;width:64px;flex:none;background:var(--card);border-right:1px solid var(--line);display:flex;flex-direction:column;align-items:center;padding:12px 0;gap:6px;z-index:10}
.rail nav{display:flex;flex-direction:column;gap:4px;margin-top:14px;width:100%;align-items:center}
.rail .nav{width:44px;height:44px;border-radius:10px;display:grid;place-items:center;color:var(--muted);text-decoration:none;position:relative;border:0;background:transparent;cursor:pointer;padding:0}
.rail .nav svg{width:22px;height:22px;stroke:currentColor;fill:none;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.rail .nav:hover{background:var(--bg);color:var(--fg)}.rail .nav.on{background:rgba(8,102,255,.14);color:var(--accent)}
.rail .nav span{position:absolute;left:52px;top:50%;transform:translateY(-50%);background:var(--fg);color:var(--bg);font-size:12px;padding:4px 8px;border-radius:6px;white-space:nowrap;opacity:0;pointer-events:none;transition:opacity .12s}
.rail .nav:hover span{opacity:1}
.rail-bottom{margin-top:auto;display:flex;flex-direction:column;align-items:center;gap:8px}
.rail .dropdown.up{top:auto;bottom:0;left:52px;right:auto}
@media(max-width:800px){body.app{flex-direction:column}.rail{position:fixed;top:auto;bottom:0;left:0;right:0;width:auto;height:60px;flex-direction:row;justify-content:space-around;padding:0 8px;border-right:0;border-top:1px solid var(--line)}
 .rail nav{flex-direction:row;margin:0;width:auto}.rail .logo{display:none}.rail-bottom{margin:0;flex-direction:row}.rail .nav span{display:none}
 .page{padding-bottom:64px}.rail .dropdown.up{left:auto;right:0;bottom:50px}}
"""

LOGO = '<svg viewBox="0 0 24 24"><path d="M4 5h16v10H9l-5 4z"/><path d="M8 9h8M8 12h5"/></svg>'

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--fg:#1c1e21;--muted:#65676b;--line:#e4e6eb;--accent:#0866ff;--ok:#31a24c;--bad:#e41e3f}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#18191a;--card:#242526;--fg:#e4e6eb;--muted:#b0b3b8;--line:#3a3b3c}}
:root[data-theme=dark]{--bg:#18191a;--card:#242526;--fg:#e4e6eb;--muted:#b0b3b8;--line:#3a3b3c}
*{box-sizing:border-box}html{color-scheme:light dark}body{margin:0;font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
.brand{display:flex;align-items:center;gap:10px}.logo{width:36px;height:36px;border-radius:10px;background:var(--accent);display:grid;place-items:center;flex:none;text-decoration:none}
.logo svg{width:22px;height:22px;stroke:#fff;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
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
.mono{font-family:ui-monospace,Menlo,monospace;font-size:12px;white-space:nowrap}.scroll{overflow-x:auto}
.pagehead{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}.pagehead p{margin:0}
.btn.slim{width:auto;margin-top:0;padding:8px 14px}.row.end{justify-content:flex-end;margin-top:16px}.row.end .btn{margin-top:0}
.who{display:flex;align-items:center;gap:10px}.who b{display:block;font-size:13px}.who small{color:var(--muted)}
.avatar.sm{width:30px;height:30px;font-size:12px}
.tag{display:inline-block;padding:2px 8px;border-radius:999px;font-size:12px;background:var(--bg);border:1px solid var(--line)}
.tag.active{color:var(--ok)}.tag.invited{color:var(--accent)}.tag.disabled{color:var(--bad)}
td.act{text-align:right;width:40px}.kebab{border:1px solid transparent;background:transparent;color:var(--muted);font-size:18px;line-height:1;width:32px;height:32px;border-radius:8px;cursor:pointer}
.kebab:hover{background:var(--bg);color:var(--fg)}.dropdown.right{right:0;top:36px;left:auto;min-width:200px}
.dropdown form{margin:0}.dropdown form button{display:block;width:100%;text-align:left;padding:8px 10px;border:0;background:transparent;color:var(--fg);font:inherit;font-size:13px;border-radius:6px;cursor:pointer}
.dropdown form button:hover{background:var(--bg)}.dropdown form button.danger{color:var(--bad)}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;place-items:center;z-index:60;padding:16px}.modal.on{display:grid}
.modal textarea{width:100%;font:inherit;padding:8px 10px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin:0 0 16px}.tab{padding:8px 12px;color:var(--muted);text-decoration:none;font-size:13px;border-bottom:2px solid transparent;margin-bottom:-1px}.tab.on{color:var(--accent);border-color:var(--accent)}
.srow{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:10px 0;border-bottom:1px solid var(--line)}.srow b{display:block;font-size:13px}.srow small{color:var(--muted)}
.draft{border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:10px}.dh{display:flex;align-items:center;gap:10px;flex-wrap:wrap}.dh small{color:var(--muted)}
.draft pre{white-space:pre-wrap;font-size:12px;background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;max-height:320px;overflow:auto}.draft summary{cursor:pointer;color:var(--accent);font-size:13px}
.pools{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}@media(max-width:800px){.pools{grid-template-columns:1fr 1fr}}.pool h4{margin:0 0 6px}.pool ul{margin:0;padding-left:18px;font-size:13px}
.seg2{display:flex;gap:14px}.seg2 .check{margin:0}
.min{display:flex;align-items:center;gap:6px}.min input{width:110px;text-align:right}.kv{display:grid;grid-template-columns:auto 1fr;gap:6px 14px}.kv dt{color:var(--muted)}.kv dd{margin:0}
.modal .box{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px;width:min(480px,100%)}.modal h3{margin:0 0 6px}
""" + RAIL_CSS

JS = "<script>" + """
try{var t=localStorage.getItem('theme');if(t&&t!=='system')document.documentElement.setAttribute('data-theme',t)}catch(e){}
function togglePw(id,b){var i=document.getElementById(id);i.type=i.type==='password'?'text':'password';b.setAttribute('aria-label',i.type==='password'?'Show password':'Hide password');}
function toggleMenu(b){var m=document.getElementById('userMenu');var on=!m.classList.contains('on');m.classList.toggle('on',on);b.setAttribute('aria-expanded',on);}
function toggleRow(b){var m=b.nextElementSibling,on=!m.classList.contains('on');document.querySelectorAll('.dropdown.right.on').forEach(function(x){x.classList.remove('on')});m.classList.toggle('on',on);}
function openModal(id){document.getElementById(id).classList.add('on');var f=document.querySelector('#'+id+' input');if(f)f.focus();}
function closeModal(id){document.getElementById(id).classList.remove('on');}
document.addEventListener('click',function(e){if(!e.target.closest('.menu'))document.querySelectorAll('.dropdown.right.on').forEach(function(x){x.classList.remove('on')});});
(function(){var b=document.getElementById('theme');if(!b)return;var I=""" + __import__("json").dumps({k: ICONS[k] for k in ("system", "light", "dark")}) + """;
 var O=['system','light','dark'],cur='system';try{cur=localStorage.getItem('theme')||'system'}catch(e){}
 function ap(v){cur=v;try{localStorage.setItem('theme',v)}catch(e){}if(v==='system')document.documentElement.removeAttribute('data-theme');else document.documentElement.setAttribute('data-theme',v);b.innerHTML=I[v];b.title='Theme: '+v;}
 ap(O.indexOf(cur)>=0?cur:'system');b.onclick=function(){ap(O[(O.indexOf(cur)+1)%3])};})();
document.addEventListener('click',function(e){var m=document.getElementById('userMenu');if(m&&!e.target.closest('.menu'))m.classList.remove('on');});
document.addEventListener('keydown',function(e){if(e.key==='Escape'){var m=document.getElementById('userMenu');if(m)m.classList.remove('on');}});
""" + "</script>"


def _page(title: str, body: str, wide: bool = False, user: User | None = None, shell: str = "app", active: str = "") -> str:
    head = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{_esc(title)} · Muse ServiceDesk</title>'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><style>{CSS}</style></head>')
    if shell == "auth":
        return (f'{head}<body class="auth"><div class="card"><div class="brand"><div class="logo">{LOGO}</div>'
                f'<h2>{_esc(title)}</h2></div>{body}'
                f'<p class="foot">Muse ServiceDesk · service desk console</p></div>{JS}</body></html>')
    return (f'{head}<body class="app">{rail(user, active)}<div class="page"><main><div class="card"><h2>{_esc(title)}</h2>{body}</div></main></div>{JS}{CHAT_WIDGET}</body></html>')


def rail(user: User | None, active: str = "") -> str:
    """The vertical icon sidebar shared by every signed-in page: logo, navigation, theme, avatar menu."""
    if user is None:
        return ""
    admin = user.role == "admin"
    def item(key, href, label):
        return (f'<a class="nav {"on" if active == key else ""}" href="{href}" title="{label}" aria-label="{label}">'
                f'{ICONS[key]}<span>{label}</span></a>')
    nav = item("chat", "/chat", "Desk Chat") + item("dash", "/", "Dashboard") + item("tickets", "/tickets", "Tickets") + item("knowledge", "/knowledge", "Knowledge")
    if admin:
        nav += item("users", "/users", "Desk users") + item("audit", "/audit", "Audit log")
    return (f'<aside class="rail"><a class="logo" href="/" title="Muse ServiceDesk">{LOGO}</a><nav>{nav}</nav>'
            f'<div class="rail-bottom">{item("settings", "/settings", "Settings")}<button class="nav ibtn" id="theme" title="Theme" aria-label="Toggle theme"></button>'
            f'<div class="menu"><button class="avatar" id="avatarBtn" aria-haspopup="menu" aria-expanded="false" '
            f'onclick="toggleMenu(this)">{_initials(user.name)}</button>'
            f'<div class="dropdown up" id="userMenu" role="menu">'
            f'<div class="dd-head"><b>{_esc(user.name)}</b><small>{_esc(user.email)} · {user.role}</small></div>'
            f'<a href="/profile" role="menuitem">Profile &amp; password</a><div class="dd-sep"></div>'
            f'<a href="/logout" role="menuitem">Sign out</a></div></div></div></aside>')
