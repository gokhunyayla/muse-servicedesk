"""Fixed system texts in the employee's language. Agents already answer in the employee's language;
these are the few messages the system sends without a model (which-ticket question, closes, reminders)."""
from __future__ import annotations

import os
import re

DEFAULT_LANG = os.getenv("DESK_LANG", "en")     # used only when a message carries no language hints at all

_TR_HINT = re.compile(r"[çğıöşüÇĞİÖŞÜ]|\b(ve|bir|için|değil|yok|var|ama|çok|şifre|bağlan\w*|çalışm\w*|lütfen|merhaba|teşekkür\w*|evet|hayır|tamam|kapat|iptal|oldu)\b", re.I)
_EN_HINT = re.compile(r"\b(the|and|not|can\'?t|cannot|please|hello|hi|thanks|password|connect\w*|work\w*|my|is|are|yes|no|solved|close|cancel|done|ok)\b", re.I)


def detect(text: str, default: str | None = ...) -> str | None:  # type: ignore[assignment]
    """'tr' or 'en' from a message; Turkish wins ties. With no hints at all ("ok", "5", "close") returns
    `default`, so callers can fall back to the ticket's language."""
    tr = len(_TR_HINT.findall(text or ""))
    en = len(_EN_HINT.findall(text or ""))
    if tr == 0 and en == 0:
        return DEFAULT_LANG if default is ... else default
    return "en" if en > tr else "tr"


def ticket_lang(ticket) -> str:
    """Language of the employee's last message on the ticket."""
    for e in reversed(ticket.events or []):
        if e.kind == "message" and e.actor == "user":
            return detect(e.content)
    return detect(ticket.title)


T = {
    "approval_request": {"tr": "Onay talebi #{id}: {name} için \"{request}\". Cevap: ONAY {id} veya RET {id}",
                         "en": "Approval request #{id}: \"{request}\" for {name}. Reply: APPROVE {id} or DENY {id}"},
    "which_ticket": {"tr": "Birden fazla açık talebiniz var, hangisi için?\n{choices}\n(Örn. \"#{first} çözüldü\")",
                     "en": "You have several open tickets; which one is this about?\n{choices}\n(e.g. \"#{first} solved\")"},
    "resume_offer": {"tr": "Az önce {chan} üzerinden #{id} (\"{title}\") konusunda kalmıştık. Oradan devam edelim mi, yoksa bu yeni bir konu mu? (evet / yeni konu)",
                     "en": "A moment ago we were on #{id} (\"{title}\") over {chan}. Shall we continue from there, or is this a new issue? (yes / new issue)"},
    "resume_offer_hi": {"tr": "Merhaba! Az önce {chan} üzerinden #{id} (\"{title}\") konusunda kalmıştık. Oradan devam edelim mi, yoksa yeni bir konu mu var? (evet / yeni konu)",
                        "en": "Hello! A moment ago we were on #{id} (\"{title}\") over {chan}. Shall we continue from there, or is there a new issue? (yes / new issue)"},
    "resume_ok": {"tr": "Tamam, #{id} üzerinden devam ediyoruz. Son durum: {last}",
                  "en": "Alright, continuing on #{id}. Where we were: {last}"},
    "resume_new": {"tr": "Anladım, yeni bir konu. Sorununuzu yazın, hemen bakayım.",
                   "en": "Understood, a new issue. Tell me the problem and I will get on it."},
    "closed_all": {"tr": "Tamam, {n} açık talebinizi kapattım: {ids}. Yeni bir sorun olursa yazmanız yeterli.",
                   "en": "Done, I closed your {n} open requests: {ids}. Just write again if anything else comes up."},
    "nothing_open": {"tr": "Şu an açık talebiniz yok.", "en": "You have no open requests right now."},
    "open_list": {"tr": "{n} açık talebiniz var:\n{lines}", "en": "You have {n} open requests:\n{lines}"},
    "st_open": {"tr": "açık", "en": "open"}, "st_waiting_user": {"tr": "sizden cevap bekliyor", "en": "waiting for you"},
    "st_waiting_approval": {"tr": "onay bekliyor", "en": "waiting for approval"}, "st_escalated": {"tr": "mühendiste", "en": "with an engineer"},
    "st_resolved": {"tr": "çözüldü", "en": "resolved"},
    "greeting": {"tr": "Merhaba! Ben IT servis masası. Sorununuzu veya talebinizi yazın, hemen bakayım.",
                 "en": "Hello! This is the IT service desk. Tell me the problem or request and I will get on it."},
    "greeting_open": {"tr": "Merhaba! #{id} numaralı talebiniz (\"{title}\") hâlâ açık. Onunla ilgili bir gelişme varsa yazın; yeni bir konu için de doğrudan anlatabilirsiniz.",
                      "en": "Hello! Your ticket #{id} (\"{title}\") is still open. Tell me if there is news on it, or just describe a new issue."},
    "same_or_new": {"tr": "Tam anlayamadım: bu, açık talebiniz #{id} (\"{title}\") ile mi ilgili, yoksa yeni bir konu mu? Sorunu bir cümleyle anlatır mısınız?",
                    "en": "I could not quite place this: is it about your open ticket #{id} (\"{title}\") or a new issue? Could you describe the problem in a sentence?"},
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
    "vuln_fixed": {"tr": "🔒 {cve} ({name}, CVSS {score}, CISA'nın aktif istismar listesinde{ransom}) {host} üzerinde {app} {version} sürümünü etkiliyordu. Fark ettim ve giderdim: {app} {change}. Sürüm tekrar okunup doğrulandı; yedek {backup}. Geri almak gerekirse bilet #{id} üzerinden 'geri al' yazmanız yeterli.",
                   "en": "🔒 {cve} ({name}, CVSS {score}, on CISA's known-exploited list{ransom}) affected {app} {version} on {host}. I noticed and fixed it: {app} {change}. The version was read back and verified; backup {backup}. To undo, reply 'roll back' on ticket #{id}."},
    "vuln_open": {"tr": "⚠️ {cve} ({name}, CVSS {score}, CISA KEV) {host} üzerindeki {app} {version} sürümünü etkiliyor; düzeltme {fixed}. Otomatik düzeltmedim: {why}. Bilet #{id} L3'te.",
                  "en": "⚠️ {cve} ({name}, CVSS {score}, CISA KEV) affects {app} {version} on {host}; fixed in {fixed}. I did not fix it automatically: {why}. Ticket #{id} is with L3."},
    "ransomware": {"tr": ", fidye yazılımlarında kullanılıyor", "en": ", used in ransomware"},
    "malware_fixed": {"tr": "🦠 {host} üzerinde Microsoft Defender {names} tespit etti ({files}; Defender: {defender}). Fark ettim ve giderdim: tehdit kaldırıldı, imzalar güncellendi, hızlı tarama temiz çıktı. Bilet #{id} kapatıldı.",
                      "en": "🦠 Microsoft Defender detected {names} on {host} ({files}; Defender: {defender}). I noticed and fixed it: the threat was removed, signatures updated and a quick scan came back clean. Ticket #{id} is closed."},
    "malware_open": {"tr": "⚠️ {host} üzerinde Microsoft Defender {names} tespit etti ({files}). Otomatik temizleyemedim: {why}. Bilet #{id} L3'te; cihazı izole etmeyi düşünün.",
                     "en": "⚠️ Microsoft Defender detected {names} on {host} ({files}). I could not clean it automatically: {why}. Ticket #{id} is with L3; consider isolating the device."},
    "auto_off": {"tr": "otomatik düzeltme kapalı", "en": "automatic remediation is off"},
    "voice_failed": {"tr": "Sesli mesajı anlayamadım, yazar mısınız?", "en": "I could not understand the voice note; could you type it?"},
}


def t(key: str, lang: str, **kw) -> str:
    return T[key].get(lang, T[key]["tr"]).format(**kw)
