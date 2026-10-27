# Üretim değişikliği (yama, konfigürasyon)

Her üretim etkili işlemde sıra:
1. Kapsam: find_vulnerable_servers / envanter.
2. add_note ile değişiklik planı + geri alma planı.
3. Uygula: apply_patch (anlık görüntü otomatik). Aynı anda tek sunucu; yük dengeli çiftlerde önce ikincil.
4. verify_patch; başarısızsa rollback_patch.
5. 24 saatlik stabilite izlemesi için schedule_followup (hours=24, "stabilite kontrolü").
6. Kanıt: add_note (sürümler önce/sonra, snapshot id) ve talep edene e-posta (notify_user).
