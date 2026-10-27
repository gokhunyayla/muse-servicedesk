# Toplu kesinti

Aynı belirtiyle kısa sürede birden fazla bilet geliyorsa (ör. "e-posta açılmıyor" x5):
1. find_similar_tickets ile açık benzer biletleri bul; hepsini tek incident'a bağla (link_tickets).
2. Kök nedeni servis tarafında ara (sunucu sağlığı, son değişiklik).
3. Tüm bağlı biletlerin sahiplerine tek bir durum mesajı gönder (notify_user); her 30 dakikada güncelle.
4. Çözüldüğünde tek seferde kapat, herkesten teyit iste.
