# Donanım arızası, servis kaydı ve kargo

1. check_warranty: garanti içindeyse tedarikçi servisi, değilse yedek cihaz + hurda/onarım kararı için not.
2. open_service_case ile servis kaydı; özet: belirti, stop kodu/hata, denenenler.
3. assign_loaner — kullanıcı işsiz kalmasın.
4. create_shipment: yedek cihazı kullanıcıya, arızalıyı servise. Takip numarasını bilete yaz.
5. track_shipment ile teslimatı izle; schedule_followup ile 2 günde bir kontrol.
6. Arızalı cihaz döndüğünde yedek cihazı geri al (return_loaner).
