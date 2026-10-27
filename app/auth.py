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
              ("total", "Total resolution", "Open → resolved, excluding time waiting for the employee")]


@router.get("/settings", response_class=HTMLResponse)
def settings_page(user: User = Depends(current_user), tab: str = "sla", info: str = "") -> str:
    from app.sla import sla_targets
    admin = user.role == "admin"
    tabs = [("sla", "SLA"), ("channels", "Channels"), ("models", "Models"), ("about", "About")]
    nav = "".join(f'<a class="tab {"on" if tab == k else ""}" href="/settings?tab={k}">{v}</a>' for k, v in tabs)
    if tab == "sla":
        t = sla_targets()
        rows = "".join(f'<div class="srow"><div><b>{label}</b><small>{hint}</small></div>'
                       f'<div class="min"><input name="{k}" type="number" min="1" step="1" value="{t.get(k, "")}" {"" if admin else "disabled"}> <span>min</span></div></div>'
                       for k, label, hint in SLA_FIELDS)
        body = f"""<p class="muted">Targets in minutes. Tickets show green/red against these on their Timing panel.</p>
<form method="post" action="/settings/sla">{rows}{_alert('', info)}
{('<div class="row end"><button class="btn primary slim" type="submit">Save SLA targets</button></div>' if admin else '<p class="muted">Only admins can change these.</p>')}</form>"""
    elif tab == "channels":
        body = '<p class="muted">WhatsApp Cloud API settings are read from <code>.env</code> (WA_PHONE_NUMBER_ID, WA_ACCESS_TOKEN, WA_VERIFY_TOKEN). An editable form lands here next.</p>'
    elif tab == "models":
        from app.config import settings as cfg
        body = f'<p class="muted">Meta Model API</p><dl class="kv"><dt>Agents</dt><dd>{_esc(cfg.meta_model)}</dd><dt>Heavy (L3)</dt><dd>{_esc(cfg.meta_model_heavy)}</dd><dt>Reasoning effort</dt><dd>{_esc(cfg.reasoning_effort)}</dd><dt>Base URL</dt><dd>{_esc(cfg.meta_base_url)}</dd></dl>'
    else:
        body = '<p class="muted">Muse ServiceDesk — autonomous multi-tier IT service desk on Meta Model API. Built for the Meta Global AI Developer Hackathon 2026.</p>'
    return _page("Settings", f'<div class="tabs">{nav}</div>{body}', wide=True, user=user, active="settings")


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
    "dash": '<svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>',
    "tickets": '<svg viewBox="0 0 24 24"><path d="M4 4h16v16H4z"/><path d="M4 9h16M9 9v11"/></svg>',
    "users": '<svg viewBox="0 0 24 24"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"/></svg>',
    "audit": '<svg viewBox="0 0 24 24"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z"/><path d="M14 3v6h6M8 13h8M8 17h8"/></svg>',
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
.modal{position:fixed;inset:0;background:rgba(0,0,0,.45);display:none;place-items:center;z-index:30;padding:16px}.modal.on{display:grid}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--line);margin:0 0 16px}.tab{padding:8px 12px;color:var(--muted);text-decoration:none;font-size:13px;border-bottom:2px solid transparent;margin-bottom:-1px}.tab.on{color:var(--accent);border-color:var(--accent)}
.srow{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:10px 0;border-bottom:1px solid var(--line)}.srow b{display:block;font-size:13px}.srow small{color:var(--muted)}
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
    return (f'{head}<body class="app">{rail(user, active)}<div class="page"><main><div class="card"><h2>{_esc(title)}</h2>{body}</div></main></div>{JS}</body></html>')


def rail(user: User | None, active: str = "") -> str:
    """The vertical icon sidebar shared by every signed-in page: logo, navigation, theme, avatar menu."""
    if user is None:
        return ""
    admin = user.role == "admin"
    def item(key, href, label):
        return (f'<a class="nav {"on" if active == key else ""}" href="{href}" title="{label}" aria-label="{label}">'
                f'{ICONS[key]}<span>{label}</span></a>')
    nav = item("dash", "/", "Dashboard") + item("tickets", "/tickets", "Tickets")
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
