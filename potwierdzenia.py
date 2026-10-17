"""
potwierdzenia.py — czy w wypowiedzi padło wyraźne "tak" i operacje czekające na nie.

Jedno miejsce dla wszystkich potwierdzeń: restartu i wyłączenia komputera
(main.py), operacji na plikach (pliki.py) i dodawania wydarzeń do kalendarza
(kalendarz.py). Dzięki temu "tak" znaczy wszędzie to samo.
"""

import logging
import re
import threading
import time
import uuid

logger = logging.getLogger(__name__)

SLOWA_ODMOWY = {
    "nie", "anuluj", "anuluję", "anuluje", "przerwij", "stop", "zostaw",
    "czekaj", "rezygnuję", "rezygnuje", "odwołaj", "odwolaj", "no",
}
SLOWA_ZGODY = {
    "tak", "jasne", "pewnie", "potwierdzam", "zgoda", "zgadza", "dawaj",
    "śmiało", "smialo", "rób", "rob", "zrób", "zrob", "wykonaj", "oczywiście",
    "oczywiscie", "ok", "okej", "okay", "yes", "dobra", "dobrze",
}


def to_zgoda(tekst):
    """
    Czy w odpowiedzi padło wyraźne "tak"?

    Porównujemy CAŁE słowa, a nie fragmenty — inaczej "nie" złapałoby się
    w "niebo", a "ok" w "okno". Cisza (None) i hałas ("") to odmowa:
    brak potwierdzenia nigdy nie może znaczyć zgody.
    """
    if not tekst:
        return False

    slowa = set(re.findall(r"[a-ząćęłńóśżź]+", tekst.lower()))
    if slowa & SLOWA_ODMOWY:
        return False
    return bool(slowa & SLOWA_ZGODY)


def to_odmowa(tekst):
    """Czy w odpowiedzi padło wyraźne "nie" (albo "anuluj", "stop"...)."""
    return bool(set(re.findall(r"[a-ząćęłńóśżź]+", (tekst or "").lower())) & SLOWA_ODMOWY)


# ---------------------------------------------------------------
# Operacje czekające na zgodę (pliki.py, kalendarz.py)
# ---------------------------------------------------------------
#
# Narzędzie, które coś zmienia "na poważnie" (nadpisanie pliku, zmiana
# treści, dodanie wydarzenia do kalendarza), najpierw NICZEGO nie robi:
# zapamiętuje operację tutaj i zwraca modelowi opis z kodem. Model pyta Cię
# o zgodę, a dopiero gdy w NASTĘPNEJ wypowiedzi powiesz "tak", woła
# confirm_action z tym kodem. Kod pilnuje, żeby model nie "potwierdził sam
# sobie":
#
#   - potwierdzenie musi przyjść w innej wypowiedzi niż propozycja,
#   - w tej wypowiedzi musi paść wyraźna zgoda (to_zgoda), bez "nie",
#   - propozycja wygasa po WAZNOSC_S, a naraz czeka tylko jedna.
#
# Mail tego nie obejdzie: po odczycie poczty agent blokuje wszystkie
# narzędzia do końca wypowiedzi (agent.py, BLOKADA_PO_POCZCIE), a do
# potwierdzenia potrzeba Twojego "tak" w kolejnej wypowiedzi.

WAZNOSC_S = 5 * 60                   # tyle czeka propozycja na Twoje "tak"

# Tym zaczyna się wynik narzędzia, które czeka na zgodę.
POTRZEBNE_POTWIERDZENIE = "POTRZEBNE POTWIERDZENIE"


class _Operacja:
    def __init__(self, kod, id_wypowiedzi, wykonaj):
        self.kod = kod
        self.id_wypowiedzi = id_wypowiedzi
        self.wykonaj = wykonaj            # funkcja bez argumentów -> (komunikat, ok)
        self.kiedy = time.monotonic()


_oczekujaca = None
_blokada = threading.Lock()


def zaproponuj(opis, wykonaj, id_wypowiedzi):
    """Zapamiętuje operację do potwierdzenia i mówi modelowi, co ma zrobić."""
    global _oczekujaca
    kod = uuid.uuid4().hex[:6]
    with _blokada:
        _oczekujaca = _Operacja(kod, id_wypowiedzi, wykonaj)
    return (f"{POTRZEBNE_POTWIERDZENIE} (kod {kod}). NICZEGO JESZCZE NIE ZROBIŁEM.\n{opis}\n"
            "Powiedz to użytkownikowi krótko i zapytaj, czy wykonać. Jeśli w następnej "
            f"wypowiedzi wyraźnie się zgodzi, wywołaj confirm_action z kodem {kod}. "
            "Nie potwierdzaj sam."), True


def potwierdz(kod, tekst_uzytkownika, id_wypowiedzi):
    """confirm_action — wykonuje czekającą operację tylko z wyraźnym "tak" w kolejnej wypowiedzi."""
    global _oczekujaca
    with _blokada:
        operacja = _oczekujaca
        if operacja is None or operacja.kod != (kod or "").strip():
            return ("Nie ma operacji o tym kodzie, która czeka na potwierdzenie (mogła wygasnąć "
                    "albo została zastąpiona nową). Jeśli trzeba, zaproponuj ją jeszcze raz."), False
        if time.monotonic() - operacja.kiedy > WAZNOSC_S:
            _oczekujaca = None
            return "Propozycja wygasła — zaproponuj ją jeszcze raz i zapytaj o zgodę.", False
        if operacja.id_wypowiedzi == id_wypowiedzi:
            return ("NIE WYKONANO: zgodę musi dać użytkownik w swojej kolejnej wypowiedzi. "
                    "Najpierw go zapytaj."), False
        if to_odmowa(tekst_uzytkownika):
            _oczekujaca = None
            return "Użytkownik się nie zgodził — operację anulowałem, nic nie zmieniłem.", True
        if not to_zgoda(tekst_uzytkownika):
            return ("NIE WYKONANO: w ostatniej wypowiedzi użytkownika nie ma wyraźnej zgody "
                    "(„tak”, „potwierdzam”). Zapytaj jeszcze raz."), False
        _oczekujaca = None
    logger.info("[POTWIERDZENIE] Wykonuję operację %s.", operacja.kod)
    return operacja.wykonaj()
