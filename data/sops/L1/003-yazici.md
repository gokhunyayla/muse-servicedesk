# Yazıcı yazdırmıyor

Belirti: "yazıcı çalışmıyor", "çıktı alamıyorum", kuyrukta bekleyen işler.

1. Hangi yazıcı ve hangi cihaz? find_user ile cihazı bul.
2. run_endpoint_script `printer-fix`: yazdırma kuyruğunu temizler ve spooler servisini yeniden başlatır.
3. Düzelmediyse yazıcı ağda erişilemiyor olabilir → L2'ye devret (network_diagnostics).
4. Teyit al, kapat.
