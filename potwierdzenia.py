"""
potwierdzenia.py — czy w wypowiedzi padło wyraźne "tak".

Jedno miejsce dla wszystkich potwierdzeń: restartu i wyłączenia komputera
(main.py) i operacji na plikach (pliki.py). Dzięki temu "tak" znaczy
wszędzie to samo.
"""

import re

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
