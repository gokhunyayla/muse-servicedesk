# SAP ve dizin yetkileri

Belirti: "SAP'de X işlemini yapamıyorum, yetki hatası", "klasöre erişemiyorum".

1. Yetki değişikliği yönetici onayı ister: request_manager_approval (onay bilette bekler).
2. Onay gelince grant_sap_role (rol adı + geçerlilik) veya add_to_group.
3. Kullanıcıya SAP oturumunu kapatıp açmasını söyle; teyit al.
4. Geçici yetkiler için schedule_followup ile bitiş tarihinde geri alma.
