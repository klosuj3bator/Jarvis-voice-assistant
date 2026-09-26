"""
zajetosc.py — jedna blokada na usta i uszy Jarvisa.

Malutki moduł z jednym zadaniem: pilnować, żeby dwie części programu nie
próbowały mówić (albo mówić i słuchać) w tym samym momencie.


PO CO TO JEST
=============

Do tej pory wszystko działo się po kolei w jednym wątku: usłyszał, pomyślał,
odpowiedział. Przypomnienia to zmieniają — pilnuje ich osobny wątek, który
budzi się sam i chce coś powiedzieć w chwili, której nikt nie przewidział.
Bez uzgodnienia skończyłoby się tak:

    Jarvis: "Włączam Nevermind—"
    Przypomnienie:      "—przypominam o praniu"

albo gorzej: przypomnienie odezwałoby się w trakcie nagrywania Twojej
komendy, mikrofon nagrałby oba głosy naraz i Whisper zrobiłby z tego
sieczkę.

Rozwiązaniem jest BLOKADA (ang. lock) — umowa, że pewną rzecz robi naraz
tylko jeden wątek. Kto chce mówić albo nagrywać, najpierw ją bierze;
reszta czeka w kolejce, aż ją odda.

Używa się tego przez `with`:

    with zajetosc.zajmij("przypomnienie"):
        tts.mow("Pranie!")

Po wyjściu z bloku blokada zwalnia się sama — nawet jeśli w środku
wyskoczy błąd. To ważne: zapomniana blokada zatrzymałaby Jarvisa na dobre.


DLACZEGO RLock, A NIE ZWYKŁY Lock
=================================

RLock (blokada wielowejściowa) pozwala TEMU SAMEMU wątkowi wziąć ją
ponownie, jeśli już ją trzyma. Zwykły Lock zakleszczyłby się w takiej
sytuacji na zawsze, a u nas zdarza się ona naturalnie: main.py bierze
blokadę na czas całej wymiany zdań, a w środku woła tts.mow(), które
też próbuje ją wziąć.
"""

import contextlib
import logging
import threading

logger = logging.getLogger(__name__)

# Jedna blokada dla całego programu — dlatego jest tu, a nie w tts.py
# czy wake_word_listener.py: obie strony muszą uzgadniać się z tą SAMĄ.
_blokada = threading.RLock()

# Kto ją trzyma — wyłącznie do dziennika, żeby dało się zrozumieć,
# na co czekało przypomnienie.
_powod = None


@contextlib.contextmanager
def zajmij(powod, czekaj=True):
    """
    Zajmuje usta i uszy Jarvisa na czas bloku `with`.

    powod  — krótki opis do dziennika ("rozmowa", "przypomnienie")
    czekaj — True: poczekaj w kolejce. False: odpuść, jeśli zajęte
             (wtedy blok wykona się z wartością False).

    Zwraca (przez `with ... as`): True, jeśli udało się zająć.
    """
    global _powod

    if not _blokada.acquire(blocking=czekaj):
        logger.info("[ZAJĘTOŚĆ] %r odpuszcza — trwa %r.", powod, _powod)
        yield False
        return

    poprzedni = _powod
    _powod = powod
    try:
        yield True
    finally:
        _powod = poprzedni
        _blokada.release()


def czy_wolny():
    """
    Czy Jarvis akurat nic nie mówi i niczego nie nagrywa?

    Sprawdzenie jest chwilowe: między odpowiedzią a jej użyciem stan może
    się zmienić. Służy do decyzji "spróbuję za chwilę", nie do zabezpieczeń.
    """
    if _blokada.acquire(blocking=False):
        _blokada.release()
        return True
    return False


def czym_zajety():
    """Opis tego, co teraz trzyma blokadę (albo None). Do dziennika."""
    return _powod
