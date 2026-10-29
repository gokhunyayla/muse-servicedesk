"""Self-enrolment: someone the desk does not know (no directory record, no client row) can get an account by
talking to the desk, on WhatsApp or the support page, with an invitation code. Built for evaluators and pilots:
they write from their own phone, prove the invitation, give their name and e-mail, and the desk creates a real
account (Entra when connected), tells them the username and first password, and lets them use every channel.

Kept apart from the ticket flow on purpose: a known sender never reaches this module, and with enrolment off
(the default) nothing here runs. State is one row per contact in the settings table, like the resume store."""
from __future__ import annotations

import logging
import re
import secrets
import time
from datetime import date, datetime, timedelta

from app.db import Client, Outbound, Setting, _now, session
from app.sla import get_setting, put_setting

logger = logging.getLogger(__name__)

DEFAULT = {
    "enabled": False,
    "code": "",                 # the invitation code; empty = nobody can enrol
    "max_accounts": 50,
    "expires_on": "",           # YYYY-MM-DD: accounts are disabled after this day (empty = never)
    "department": "Evaluation",
    "group": "jury",            # directory group the accounts join ("" = none)
    "license": "",              # licence SKU part for the accounts; "" = none; "auto" = the first one the catalog has
    "manager_employee_no": "",  # whose reports the accounts are (for approvals); empty = none
    "max_licensed": 18,         # how many enrolled accounts may take a licence (the rest get no mailbox)
}
ACCOUNTS = "enrol_accounts"    # Setting: {"list": [{employee_no, upn, name, phone, email, created, disabled}]}
MAX_TRIES = 5
LOCK_MIN = 60
STATE_TTL = 1800               # a half-finished enrolment is forgotten after 30 minutes
SKIP_RE = re.compile(r"^\s*(skip|geç|yok|no|hayır|-|none)\s*$", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")


def config() -> dict:
    return {**DEFAULT, **get_setting("enrol", {})}


def active() -> bool:
    c = config()
    return bool(c["enabled"] and c["code"])


# ---- who is unknown ----------------------------------------------------------------------------------

def is_known(contact: str) -> bool:
    """Known = a client row (enabled or not: a disabled client is a decision, not a stranger) or a directory hit."""
    from app import systems
    with session() as db:
        col = Client.email if "@" in contact else Client.phone
        if db.query(Client).filter(col == contact).first() is not None:
            return True
    try:
        return systems.user_by_contact(contact) is not None
    except Exception as e:  # noqa: BLE001  (directory unreachable: treat as known so the ticket flow handles it)
        logger.warning("directory lookup failed for enrolment: %s", e)
        return True


def wants(contact: str) -> bool:
    """Does this message belong to enrolment? Only when it is on, the sender is unknown and not locked out."""
    if not contact or not active():
        return False
    return not is_known(contact)


# ---- the conversation --------------------------------------------------------------------------------

def _key(contact: str) -> str:
    return f"enrol:{contact}"


def _state(db, contact: str) -> dict:
    row = db.get(Setting, _key(contact))
    v = dict(row.value or {}) if row else {}
    if v and time.time() - float(v.get("at", 0)) > STATE_TTL and v.get("stage") != "locked":
        v = {}
    return v


def _save(db, contact: str, v: dict | None) -> None:
    row = db.get(Setting, _key(contact))
    value = {**v, "at": time.time()} if v else None
    if row is None:
        db.add(Setting(key=_key(contact), value=value))
    else:
        row.value = value
    db.flush()


T = {
    "hello": {"tr": "Merhaba! Bu numarayı tanımıyorum. Bir davet kodunuz varsa yazın, hesabınızı birlikte açalım. Yoksa BT ekibine başvurun.",
              "en": "Hello! I don't know this number yet. If you have an invitation code, type it and we'll set up your account together. Otherwise please contact IT."},
    "bad_code": {"tr": "Bu kod eşleşmedi ({left} deneme hakkı kaldı). Davet kodunuzu tekrar yazın.",
                 "en": "That code does not match ({left} attempts left). Type your invitation code again."},
    "locked": {"tr": "Çok fazla yanlış deneme; bir saat sonra tekrar deneyebilirsiniz.",
               "en": "Too many wrong attempts; please try again in an hour."},
    "full": {"tr": "Davet kodu doğru, ama açılabilecek hesap sayısı doldu. BT ekibine başvurun.",
             "en": "The invitation code is right, but no more accounts can be opened. Please contact IT."},
    "ask_name": {"tr": "Kod doğru, hoş geldiniz! Adınızı ve soyadınızı yazar mısınız?",
                 "en": "Code accepted, welcome! What is your first and last name?"},
    "ask_email": {"tr": "Teşekkürler {name}. Hesap bilgilerinizi göndereceğim bir e-posta adresi yazın (istemezseniz \"geç\" yazın).",
                  "en": "Thanks {name}. Type an e-mail address for your account details (or \"skip\")."},
    "ask_name_again": {"tr": "Ad ve soyad olarak iki kelime bekliyorum, örneğin: Ada Lovelace",
                       "en": "I need a first and last name, e.g. Ada Lovelace"},
    "ask_email_again": {"tr": "Geçerli bir e-posta adresi yazın ya da \"geç\" deyin.",
                        "en": "Type a valid e-mail address or say \"skip\"."},
    "working": {"tr": "Hesabınızı açıyorum, birkaç saniye…", "en": "Creating your account, a few seconds…"},
    "done": {"tr": "Hesabınız hazır, {name}.\nKullanıcı adı: {upn}\nİlk şifre: {pw} (ilk girişte değiştirilecek)\n{mailbox}\n"
                   "Artık buradan ya da {support} adresinden yazabilirsiniz; şifre sıfırlama, yazıcı, VPN, lisans, oltalama… ne isterseniz. "
                   "Örnek: \"şifremi unuttum\". Hesap {until} tarihine kadar geçerli.",
             "en": "Your account is ready, {name}.\nUsername: {upn}\nFirst password: {pw} (you will change it at the first sign-in)\n{mailbox}\n"
                   "You can write here or at {support}: password reset, printer, VPN, licences, a suspicious e-mail… whatever you like. "
                   "Try: \"I forgot my password\". The account is valid until {until}."},
    "mailbox_yes": {"tr": "Posta kutunuz: https://outlook.office.com (aynı kullanıcı adı).", "en": "Your mailbox: https://outlook.office.com (same username)."},
    "mailbox_no": {"tr": "Bu hesabın posta kutusu yok; kodlar ve bildirimler WhatsApp'a gelir.", "en": "This account has no mailbox; codes and notices come to WhatsApp."},
    "failed": {"tr": "Hesap açılırken bir sorun oldu: {why}. BT ekibi bilgilendirildi.",
               "en": "Something went wrong while creating the account: {why}. IT has been told."},
    "mail_subject": {"tr": "IT Servis Masası hesabınız", "en": "Your IT Service Desk account"},
    "mail_body": {"tr": "Merhaba {name},\n\nHesabınız hazır.\nKullanıcı adı: {upn}\nİlk şifreniz WhatsApp ile gönderildi.\n\nServis masası: {support}\n",
                  "en": "Hello {name},\n\nYour account is ready.\nUsername: {upn}\nYour first password was sent over WhatsApp.\n\nService desk: {support}\n"},
    "never": {"tr": "süresiz", "en": "further notice"},
}


def _t(key: str, lang: str, **kw) -> str:
    return T[key].get(lang, T[key]["en"]).format(**kw)


def _lang(text: str, prev: str | None) -> str:
    from app.i18n import DEFAULT_LANG, detect
    return detect(text, None) or prev or DEFAULT_LANG


def step(contact: str, text: str, verified: bool = True, db=None) -> str:
    """One turn of the enrolment conversation for an unknown contact. `verified` says the contact itself is proven
    (a WhatsApp sender, or a support-page contact that passed its code). Returns the reply."""
    from contextlib import nullcontext
    with (nullcontext(db) if db is not None else session()) as s:
        st = _state(s, contact)
        lang = _lang(text, st.get("lang"))
        st["lang"] = lang
        stage = st.get("stage", "ask_code")
        cfg = config()
        if stage == "locked":
            if time.time() < float(st.get("until", 0)):
                return _t("locked", lang)
            st, stage = {"lang": lang}, "ask_code"
        if stage == "ask_code":
            given = text.strip()
            if given and secrets.compare_digest(given.upper(), cfg["code"].upper()):
                if len([a for a in accounts() if not a.get("disabled")]) >= int(cfg["max_accounts"]):
                    _save(s, contact, None)
                    return _t("full", lang)
                st.update(stage="ask_name", tries=0)
                _save(s, contact, st)
                return _t("ask_name", lang)
            if st.get("greeted"):
                st["tries"] = int(st.get("tries", 0)) + 1
                if st["tries"] >= MAX_TRIES:
                    _save(s, contact, {"stage": "locked", "until": time.time() + LOCK_MIN * 60, "lang": lang})
                    return _t("locked", lang)
                _save(s, contact, st)
                return _t("bad_code", lang, left=MAX_TRIES - st["tries"])
            st["greeted"] = True
            _save(s, contact, st)
            return _t("hello", lang)
        if stage == "ask_name":
            words = [w for w in re.split(r"\s+", text.strip()) if w]
            if len(words) < 2 or len(text.strip()) > 80 or not all(re.fullmatch(r"[^\W\d_][\w'.-]*", w) for w in words):
                return _t("ask_name_again", lang)
            st.update(stage="ask_email", name=" ".join(w[:1].upper() + w[1:] for w in words))
            _save(s, contact, st)
            return _t("ask_email", lang, name=st["name"].split()[0])
        if stage == "ask_email":
            if SKIP_RE.match(text):
                email = ""
            else:
                m = EMAIL_RE.search(text)
                if not m:
                    return _t("ask_email_again", lang)
                email = m.group(0).lower()
            phone = contact if "@" not in contact else ""
            email = email or (contact if "@" in contact else "")
            _save(s, contact, None)
            return create(s, st["name"], phone, email, lang)
        _save(s, contact, None)
        return _t("hello", lang)


# ---- the account -------------------------------------------------------------------------------------

def accounts() -> list[dict]:
    return list(get_setting(ACCOUNTS, {}).get("list", []))


def _remember_account(db, rec: dict) -> None:
    row = db.get(Setting, ACCOUNTS)
    lst = list((row.value or {}).get("list", [])) if row else []
    lst.append(rec)
    if row is None:
        db.add(Setting(key=ACCOUNTS, value={"list": lst}))
    else:
        row.value = {"list": lst}
    db.flush()


def _pick_license(cfg: dict) -> str:
    from app import systems
    want = str(cfg.get("license") or "").strip()
    if not want:
        return ""
    licensed = len([a for a in accounts() if a.get("license") and not a.get("disabled")])
    if licensed >= int(cfg.get("max_licensed") or 0):
        return ""
    g = systems._graph()
    if g:                                                    # the tenant's own catalog, with what is still free
        cat = {k: v for k, v in g.license_catalog().items() if int(v.get("available", 0)) > 0}
    else:
        cat = systems.license_catalog()
    if want.lower() == "auto":
        return next(iter(cat), "")
    return want if want in cat else ""


def create(db, name: str, phone: str, email: str, lang: str) -> str:
    """Directory account + client row + the password to the person. Problems after the account exists are
    reported, not fatal (the person can still use the desk)."""
    from app import systems
    from app.config import auth_settings
    cfg = config()
    support = f"{auth_settings.public_url.rstrip('/')}/support-agent"
    g = systems._graph()
    problems = []
    try:
        if g:
            extra = {"otherMails": [email] if email and not email.endswith("@" + (g.config().get("domain") or "")) else None}
            emp, upn, pw = g.create_user(name, cfg["department"], cfg["manager_employee_no"], phone, extra=extra)
        else:
            emp = systems.create_user(name, cfg["department"], cfg["manager_employee_no"], phone)
            upn, pw = f"{emp.lower()}@company.com", systems.reset_password(emp)
            if email:
                systems.USERS[emp]["email"] = email
    except Exception as e:  # noqa: BLE001
        logger.exception("enrolment: account creation failed for %s", phone or email)
        _notify_admins(db, f"Self-enrolment failed for {name} ({phone or email}): {e}")
        return _t("failed", lang, why=str(e)[:160])
    sku = ""
    if cfg.get("group"):
        try:
            if g:
                g.ensure_group(cfg["group"])
                g.add_to_group(upn, cfg["group"])
            else:
                systems.USERS[emp]["groups"].append(cfg["group"])
        except Exception as e:  # noqa: BLE001
            problems.append(f"group: {e}")
    try:
        sku = _pick_license(cfg)
        if sku:
            g.assign_license(upn, sku) if g else systems.USERS[emp]["licenses"].append(sku)
    except Exception as e:  # noqa: BLE001
        problems.append(f"licence: {e}")
        sku = ""
    db.add(Client(name=name, phone=phone, email=email, employee_no=emp))
    _remember_account(db, {"employee_no": emp, "upn": upn, "name": name, "phone": phone, "email": email,
                           "license": sku, "created": _now().isoformat(), "disabled": False})
    db.flush()
    until = cfg.get("expires_on") or _t("never", lang)
    reply = _t("done", lang, name=name.split()[0], upn=upn, pw=pw, support=support, until=until,
               mailbox=_t("mailbox_yes" if sku else "mailbox_no", lang))
    if email and "@" in email:
        try:
            from app.channels import send_email
            send_email(email, _t("mail_subject", lang), _t("mail_body", lang, name=name, upn=upn, support=support))
        except Exception as e:  # noqa: BLE001
            problems.append(f"mail: {e}")
    if problems:
        _notify_admins(db, f"Self-enrolment of {name} ({upn}) done with problems: " + "; ".join(problems))
    return reply


def _notify_admins(db, text: str) -> None:
    from app import vulnwatch
    for to in vulnwatch._admins():
        db.add(Outbound(to=to, text=text))


# ---- expiry ------------------------------------------------------------------------------------------

def expire_due(today: date | None = None) -> int:
    """After the expiry day: disable every enrolled account that is still on (directory and client row)."""
    from app import systems
    cfg = config()
    if not cfg.get("expires_on"):
        return 0
    try:
        last = date.fromisoformat(cfg["expires_on"])
    except ValueError:
        return 0
    if (today or date.today()) <= last:
        return 0
    import copy
    n = 0
    with session() as db:
        row = db.get(Setting, ACCOUNTS)
        lst = copy.deepcopy((row.value or {}).get("list", [])) if row else []     # a copy: in-place edits are invisible to the JSON column
        for a in lst:
            if a.get("disabled"):
                continue
            try:
                systems.set_enabled(a["employee_no"], False)
            except Exception as e:  # noqa: BLE001
                logger.warning("enrolment expiry: %s not disabled: %s", a.get("upn"), e)
                continue
            for c in db.query(Client).filter(Client.employee_no == a["employee_no"]):
                c.enabled = False
            a["disabled"] = True
            n += 1
        if row is not None and n:
            row.value = {"list": lst}
    return n


_last_tick = 0.0


def tick() -> None:
    """Called by the scheduler loop; checks once an hour, costs nothing when nothing expires."""
    global _last_tick
    if time.time() - _last_tick < 3600:
        return
    _last_tick = time.time()
    try:
        expire_due()
    except Exception as e:  # noqa: BLE001
        logger.warning("enrolment expiry failed: %s", e)
