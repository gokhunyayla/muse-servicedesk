# VPN bağlantı sorunu

Belirti: "VPN bağlanmıyor", "evden sisteme giremiyorum", VPN istemcisi hata veriyor.

1. Kullanıcının cihazını bul (find_user) ve vpn_status ile profil durumuna bak.
2. Profil `expired` ise renew_vpn_profile ile yenile; cihaz online olmalı.
3. Profil geçerli ama bağlanmıyorsa run_endpoint_script ile `vpn-reset` çalıştır (istemciyi yeniden başlatır, DNS önbelleğini temizler).
4. Hâlâ bağlanmıyorsa L2'ye devret: ağ teşhisi (network_diagnostics) gerekir.
5. Kullanıcıdan "bağlandı" teyidi al; teyit gelmeden bileti kapatma (request_confirmation).
