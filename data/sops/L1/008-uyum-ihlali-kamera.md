# Görsel uyum ihlali (kamera bulgusu)

Kamera izleyicisinden gelen bulgular: şifre notu, kilitsiz ekran, rozetsiz ziyaretçi.

1. Şifre notu görüldüyse kimlik bilgisi ifşa olmuş sayılır: hemen revoke_sessions, sonra kullanıcıya notu imha etmesini ve şifresini değiştirmesini söyle (şifre sıfırlama kimlik doğrulama ister, kullanıcı yazınca yap).
2. Kilitsiz ekran: run_endpoint_script `lock-screen` ile uzaktan kilitle, kullanıcıya Windows+L alışkanlığını hatırlat.
3. Rozetsiz ziyaretçi: masa sahibine sor, resepsiyona notify_user ile bilgi ver.
4. Her bulgu için ilgili politika maddesini (ISO 27001 A.5.17 / clear-screen) bilete yaz; schedule_followup ile 2 saat sonra tekrar kontrol.
5. Tekrarlayan ihlalde (aynı kişi 2+ kez) yöneticisine notify_user ile bilgi ver.
