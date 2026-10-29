"""Language detection must not mistake an English "i" for the Turkish dotless/dotted i."""
from app.i18n import detect


def test_english_with_the_letter_i_is_english():
    assert detect("Hello, I would like to try the desk", None) == "en"
    assert detect("hi", None) == "en"
    assert detect("My printer is not working", None) == "en"


def test_turkish_still_wins_with_turkish_letters_or_words():
    assert detect("Yazıcım çalışmıyor", None) == "tr"
    assert detect("şifremi unuttum", None) == "tr"
    assert detect("MUSE-JURY-2026", None) is None
