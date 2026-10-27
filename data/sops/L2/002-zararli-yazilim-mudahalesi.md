# Zararlı yazılım şüphesi

Belirti: garip pencereler, fidye notu, EDR uyarısı, şüpheli linke tıklama.

1. Önce izole et: isolate_device (ağdan ayırır, sadece yönetim kanalı açık kalır). Kullanıcıya bilgi ver, izin bekleme — hız önemli.
2. collect_evidence ile çalışan süreçler, son indirilenler, ağ bağlantılarını topla; bilete ekle.
3. run_malware_scan: temiz ise release_device; bulgu varsa karantinaya al (tarama bunu yapar) ve tekrar tara.
4. Kimlik bilgisi sızmış olabilir: L3'e devret → şifre + oturum sıfırlama.
5. Temiz raporuyla cihazı serbest bırak, kullanıcıya ne olduğunu kısa anlat.
