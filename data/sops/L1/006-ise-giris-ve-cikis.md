# İşe giriş (onboarding) ve işten çıkış (offboarding)

Giriş talebi genelde yöneticiden gelir: "Pazartesi yeni arkadaş başlıyor".
1. create_account (ad, departman, yönetici) → hesap ve e-posta açılır.
2. assign_license standart paket; add_to_group departman grupları.
3. Cihaz gerekiyorsa L4'e devret (loaner/yeni cihaz ve kargo).
4. İlk giriş bilgilerini yöneticiye ilet.

Çıkış talebi: "X bugün ayrıldı".
1. disable_account hemen; revoke_sessions ile tüm oturumları düşür.
2. remove_from_groups, lisansları geri al (revoke_license).
3. Cihaz iadesi için L4'e devret (kargo etiketi).
4. Yöneticiye özet rapor.
