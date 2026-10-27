# Ağ ve cihaz teşhisi

L1'den gelen devirlerde:
1. network_diagnostics ile erişilebilirlik ve DNS durumuna bak.
2. DNS bozuksa run_endpoint_script `dns-reset`; cihaz offline ise kullanıcıya fiziksel bağlantı/Wi-Fi sorularını ilet (L1 üzerinden).
3. Performans/yavaşlık: run_endpoint_script `perf-report` (CPU, bellek, disk, başlangıç programları), rapora göre öneri.
4. Mavi ekran: fotoğraftaki stop koduna göre sürücü sorunu ise run_endpoint_script `driver-rollback`; donanım şüphesi varsa L4'e devret.
5. Sonucu yapılandırılmış döndür: bulgu, uygulanan işlem, kalan risk.
