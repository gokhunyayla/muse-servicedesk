# Genel yaklaşım: SOP'ta olmayan sorunlar

Her sorun için bir SOP yok. SOP bulamadığında:
1. Belirtiyi netleştir: ne zaman başladı, hata mesajı ne, hangi cihaz/uygulama. Tek seferde en fazla 2 soru sor.
2. Kendi IT bilginle en olası nedeni belirle; elindeki araçlarla çözmeyi dene (run_endpoint_script ile uygun script, lisans, hesap).
3. Çözemiyorsan doğru seviyeye devret: cihaz/ağ/performans → L2, sunucu/uygulama/yetki/güvenlik → L3, donanım/tedarikçi → L4.
4. Çözdüğünde ve bu yeni bir yöntemse write_sop ile havuza ekle ki bir dahaki sefere hazır olsun.
5. Hiçbir durumda kullanıcıyı "bilmiyorum" ile bırakma; en azından devret ve ne zaman dönüş alacağını söyle.
