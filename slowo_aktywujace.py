"""
slowo_aktywujace.py — które słowo aktywujące, z jakim progiem, i czy padło "Jarvis".

Sam nasłuch mieszka w wake_word_listener.py. Ten moduł tylko decyduje,
CZEGO nasłuch szuka — bez mikrofonu, numpy i modeli, więc testy (także
w GitHub Actions) sprawdzają go bez żadnego dźwięku.


WŁASNY MODEL
============

Wbudowany model openWakeWord "hey_jarvis" trenowano na angielskiej wymowie
(pomiary w wake_word_listener.py, przy PROG_WYKRYCIA). Jeśli wytrenujesz
własny — np. na polskim "Hej Jarvis" — zapisz go jako:

    models/jarvis.onnx          (w folderze Jarvisa; w wersji z instalatora:
                                 %APPDATA%\\Jarvis\\models\\jarvis.onnx)

i uruchom Jarvisa ponownie. Gdy pliku nie ma albo się nie wczyta, zostaje
"hey_jarvis" — Jarvis nigdy nie zostaje głuchy przez zepsuty plik.


PRÓG CZUŁOŚCI
=============

Detektor ocenia każdą porcję dźwięku od 0.0 do 1.0. Powyżej progu Jarvis
reaguje od razu. Niższy próg = czulszy (łatwiej go zawołać, ale częściej
odezwie się sam z siebie), wyższy = ostrożniejszy.

Próg ustawiasz w Ustawieniach (zakładka Nasłuch). "Automatycznie" znaczy
domyślny próg dla danego modelu:
  hey_jarvis   0.2 — zmierzony na polskiej wymowie (opis w wake_word_listener.py)
  własny       0.5 — domyślny próg samego openWakeWord dla wytrenowanych modeli
"""

import difflib
import os
import re
import unicodedata

import konfiguracja
import sciezki

# Wbudowany model openWakeWord. Uwaga: to fraza "HEY Jarvis", nie samo "Jarvis".
MODEL_WBUDOWANY = "hey_jarvis"

# Własny model — opis wyżej. Względem folderu z danymi Jarvisa (sciezki.dane).
PLIK_WLASNEGO_MODELU = sciezki.dane("models", "jarvis.onnx")

# Domyślne progi, gdy w Ustawieniach jest "automatycznie" (opis modułu).
PROG_WBUDOWANEGO = 0.2
PROG_WLASNEGO = 0.5

# Zakres progu w Ustawieniach. Poniżej 0.05 detektor reagowałby na szum,
# powyżej 0.95 — prawie nigdy.
PROG_MIN = 0.05
PROG_MAKS = 0.95

# Próg "podejrzenia" (wtedy pytamy jeszcze Whispera, czy padło "Jarvis")
# jako ułamek progu wykrycia. Dla hey_jarvis daje to dawne 0.04 przy 0.2.
UDZIAL_PODEJRZENIA = 0.2


def wlasny_model():
    """Ścieżka do models/jarvis.onnx, jeśli plik jest (i nie jest pusty); inaczej None."""
    try:
        if os.path.isfile(PLIK_WLASNEGO_MODELU) and os.path.getsize(PLIK_WLASNEGO_MODELU) > 0:
            return PLIK_WLASNEGO_MODELU
    except OSError:
        pass
    return None


def prog_wykrycia(wlasny):
    """
    Próg, powyżej którego reagujemy od razu.

    wlasny — czy nasłuch używa własnego modelu (models/jarvis.onnx)

    Ustawienia wygrywają; "automatycznie" (brak wartości) = domyślny dla modelu.
    """
    ustawiony = konfiguracja.prog_wake_worda()
    if ustawiony is not None:
        return min(PROG_MAKS, max(PROG_MIN, ustawiony))
    return PROG_WLASNEGO if wlasny else PROG_WBUDOWANEGO


def prog_podejrzenia(prog):
    """Poniżej progu, ale powyżej tego — sprawdzamy Whisperem (wake_word_listener.py)."""
    return round(prog * UDZIAL_PODEJRZENIA, 4)


def opis_modelu(wlasny):
    """Do dziennika i Ustawień: czego nasłuch szuka."""
    return "własny model models/jarvis.onnx" if wlasny else "wbudowany „Hey Jarvis”"


# --- Czy w tekście padło "Jarvis" -----------------------------------------
#
# Przeniesione z wake_word_listener.py (tam dalej działają pod starymi
# nazwami), bo potrzebuje tego też okienko powitania (powitanie_startowe.py),
# a ono ma działać bez mikrofonu i numpy.

# Jak Whisper zapisuje usłyszane "Jarvis". Nie trzeba przewidzieć wszystkich
# form — porównujemy z nimi PODOBIEŃSTWO słowa, nie równość.
WZORCE_JARVIS = ("jarvis", "jarwis", "dzarvis", "dzarwis", "dziarwis",
                 "czarvis", "charvis", "arwis")

# Jak bardzo słowo musi być podobne do jednego z wzorców (0.0-1.0).
# 0.8 przepuszcza "jarwiz" i "dżarvisie", a odrzuca "jarzyny" (0.61)
# i "jarosław" — sprawdzone na liście zwykłych polskich zdań.
PROG_PODOBIENSTWA = 0.8


def bez_ogonkow(tekst):
    """
    "Dżarwiś" -> "dzarwis".

    Whisper zapisuje usłyszane imię raz tak, raz tak — porównywanie liter
    ma sens dopiero wtedy, gdy ogonki i kreski nie robią różnicy.
    """
    rozlozony = unicodedata.normalize("NFD", tekst.lower().replace("ł", "l"))
    return "".join(znak for znak in rozlozony
                   if unicodedata.category(znak) != "Mn")


def to_jarvis(slowo):
    """Czy jedno słowo (już bez ogonków, małymi literami) brzmi jak "Jarvis"?"""
    return any(difflib.SequenceMatcher(None, slowo, wzorzec).ratio() >= PROG_PODOBIENSTWA
               for wzorzec in WZORCE_JARVIS)


def brzmi_jak_jarvis(tekst):
    """
    Czy w tekście padło słowo brzmiące jak "Jarvis"?

    Porównujemy PODOBIEŃSTWO, nie równość, bo Whisper zapisuje to imię
    na kilkanaście sposobów: "Jarwiz", "Dżarvisie", "Arwis". Gotowa lista
    nigdy nie byłaby kompletna, a miara podobieństwa łapie i te formy,
    których nikt nie przewidział.
    """
    return any(to_jarvis(slowo) for slowo in re.findall(r"[a-z]+", bez_ogonkow(tekst or "")))
