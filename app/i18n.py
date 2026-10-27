"""Fixed system texts in the employee's language. Agents already answer in the employee's language;
these are the few messages the system sends without a model (which-ticket question, closes, reminders)."""
from __future__ import annotations

import re

_TR_HINT = re.compile(r"[çğıöşüÇĞİÖŞÜ]|\b(ve|bir|için|değil|yok|var|ama|çok|şifre|bağlan\w*|çalışm\w*|lütfen|merhaba|teşekkür\w*|evet|hayır|tamam|kapat|iptal|oldu)\b", re.I)
_EN_HINT = re.compile(r"\b(the|and|not|can\'?t|cannot|please|hello|hi|thanks|password|connect\w*|work\w*|my|is|are|yes|no|solved|close|cancel|done|ok)\b", re.I)


def detect(text: str, default: str | None = "tr") -> str | None:
    """'tr' or 'en' from a message; Turkish wins ties. With no hints at all ("ok", "5", "close") returns
    `default`, so callers can fall back to the ticket's language."""
    tr = len(_TR_HINT.findall(text or ""))
    en = len(_EN_HINT.findall(text or ""))
    if tr == 0 and en == 0:
        return default
    return "en" if en > tr else "tr"


def ticket_lang(ticket) -> str:
    """Language of the employee's last message on the ticket."""
    for e in reversed(ticket.events or []):
        if e.kind == "message" and e.actor == "user":
            return detect(e.content)
    return detect(ticket.title)


T = {
    "which_ticket": {"tr": "Birden fazla açık talebiniz var, hangisi için?\n{choices}\n(Örn. \"#{first} çözüldü\")",
                     "en": "You have several open tickets; which one is this about?\n{choices}\n(e.g. \"#{first} solved\")"},
    "closed_on_request": {"tr": "Talebiniz (#{id}) isteğiniz üzerine kapatıldı. Yeni bir sorun olursa yazmanız yeterli.",
                          "en": "Ticket #{id} was closed at your request. Just write again if anything else comes up."},
    "closed_by_desk": {"tr": "Talebiniz (#{id}) servis masası tarafından kapatıldı: {note}",
                       "en": "Ticket #{id} was closed by the service desk: {note}"},
    "reminder": {"tr": "Merhaba, #{id} numaralı talebiniz için uyguladığımız çözüm işe yaradı mı? ({resolution}) "
                       "'Çözüldü' derseniz kaydı kapatıyoruz; {hours} saat içinde yanıt gelmezse kaydı çözüldü kabul edeceğiz.",
                 "en": "Hello, did the fix for ticket #{id} work? ({resolution}) Reply 'solved' and we close it; "
                       "without an answer within {hours} hours we will consider it resolved."},
    "auto_closed": {"tr": "#{id} numaralı talebiniz yanıt gelmediği için çözüldü olarak kapatıldı. Sorun sürüyorsa yazmanız yeterli, yeni kayıt açarız.",
                    "en": "Ticket #{id} was closed as resolved because we had no reply. If the problem persists, just write and we open a new one."},
    "escalated": {"tr": "Bu adımda ilerleyemedim; talebiniz bir destek mühendisine aktarıldı, sizinle iletişime geçilecek.",
                  "en": "I could not get further here; your ticket was passed to a support engineer who will contact you."},
    "filtered": {"tr": "Mesajınızı bu haliyle işleyemedim. Sorunu bir cümleyle, başka kelimelerle yazar mısınız? (Örn. 'X sunucusunda Y hatası alıyorum')",
                 "en": "I could not process that message as written. Could you describe the problem in one sentence, in other words? (e.g. 'I get error Y on server X')"},
    "voice_failed": {"tr": "Sesli mesajı anlayamadım, yazar mısınız?", "en": "I could not understand the voice note; could you type it?"},
}


def t(key: str, lang: str, **kw) -> str:
    return T[key].get(lang, T[key]["tr"]).format(**kw)
