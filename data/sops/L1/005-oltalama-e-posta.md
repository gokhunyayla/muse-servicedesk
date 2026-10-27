# Şüpheli / oltalama e-posta

Belirti: "bu mail gerçek mi?", "şüpheli link geldi", "bir şeye tıkladım".

1. analyze_email ile gönderen, link ve ek analizini yap; karar: safe / suspicious / phishing.
2. phishing ise quarantine_email (tüm kullanıcılardan karantina) ve block_sender.
3. Kullanıcı linke tıkladıysa veya şifre girdiyse: şifreyi sıfırla (SOP 002) ve cihazı L2'ye devret (zararlı yazılım şüphesi).
4. Kullanıcıya kısa bir farkındalık notu gönder: "Aynı kaynaktan gelen mailleri açma, bize ilet."
