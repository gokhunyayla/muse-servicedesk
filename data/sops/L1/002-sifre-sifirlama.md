# Şifre sıfırlama ve hesap kilidi

Belirti: "şifremi unuttum", "hesabım kilitlendi", "giriş yapamıyorum".

1. Kimlik doğrulamadan asla şifre sıfırlama: verify_identity (sicil no + doğum yılı). Sesli mesajlarda sayılar yazıyla gelebilir.
2. Doğrulama başarılıysa reset_password; geçici şifreyi ilet, ilk girişte değiştirmesi gerektiğini söyle.
3. Doğrulama 2 kez başarısız olursa işlemi durdur, kullanıcıya yöneticisine başvurmasını söyle, bilete not düş.
4. Şifre değil MFA sorunuysa (kod gelmiyor): reset_mfa — bu da kimlik doğrulama ister.
