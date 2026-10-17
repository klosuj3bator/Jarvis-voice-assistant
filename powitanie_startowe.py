"""
powitanie_startowe.py — przez kilka minut po starcie: "hej, wróciłem" bez "Hey Jarvis".

Włączasz komputer, wchodzisz do pokoju i mówisz "cześć Jarvis" albo "jestem"
— Jarvis wita Cię jak po "Hej Jarvis, wróciłem" (powitanie.py: pora dnia,
nowe ważne maile, przypomnienia, najbliższe wydarzenie) i od razu przechodzi
w zwykły tryb, w którym trzeba go wołać "Hey Jarvis".


JAK TO DZIAŁA
=============

Po uruchomieniu i załadowaniu modeli main.py woła czekaj_na_powitanie().
Przez okno_powitania_min() minut (Ustawienia → Nasłuch, domyślnie 10)
mikrofon słucha BEZ wake worda, a każdą wypowiedź rozpoznaje Whisper —
lokalnie, nic nie idzie do sieci. Potem rodzaj() decyduje:

  powitanie   — Jarvis wita Cię i okienko się kończy,
  do_jarvisa  — "Hej Jarvis, puść muzykę": zwykłe polecenie, jakby padło
                "Hey Jarvis" — zawołany Jarvis nie może udawać, że nie słyszy;
                okienko też się kończy, bo już rozmawiacie,
  None        — wszystko inne (telewizor, rozmowa obok, telefon): pomijamy
                bez słowa i bez zapisywania treści w dzienniku.

Po czasie okienka bez powitania — zwykły tryb z "Hey Jarvis".


CO JEST POWITANIEM
==================

Rozpoznajemy je lokalnie, po słowach — model językowy nie słyszy niczego,
co nie było do niego. Krótka wypowiedź (najwyżej MAKS_SLOW słów), złożona
WYŁĄCZNIE ze słów powitania, i do tego:

  - "jestem", "wróciłem", "z powrotem", "to ja" — z imieniem albo bez,
  - "cześć", "hej", "dzień dobry" — tylko z imieniem ("cześć Jarvis").

Samo "cześć" nie wystarcza celowo: tak samo zaczyna się rozmowa
telefoniczna albo powitanie kogoś, kto właśnie wszedł. "Jestem głodny"
też nie przejdzie — "głodny" nie jest słowem powitania.
"""

import logging
import re
import time

import slowo_aktywujace

logger = logging.getLogger(__name__)

# Dłuższa wypowiedź to już nie powitanie, tylko rozmowa (np. w telewizji).
MAKS_SLOW = 8

# "Jestem", "wróciłem" — mówisz, że jesteś. Wystarczą bez imienia.
OBECNOSC = {"jestem", "jestesmy", "wrocilem", "wrocilam", "wrocilismy", "wrocilysmy"}
OBECNOSC_FRAZY = (("z", "powrotem"), ("to", "ja"))

# Zwykłe powitania — wymagają imienia (opis modułu).
POWITANIA = {"czesc", "hej", "hejka", "hejo", "siema", "siemka", "siemano", "elo",
             "witaj", "witam", "hello", "hi", "hey"}
POWITANIA_FRAZY = (("dzien", "dobry"), ("dobry", "wieczor"))

# Słowa, które mogą się w powitaniu pojawić, ale same nim nie są.
DOPELNIENIA = {"no", "to", "ja", "juz", "z", "powrotem", "w", "domu", "i", "a", "oto",
               "co", "tam", "slychac", "jak", "leci", "dzien", "dobry", "wieczor",
               "znowu", "znow", "wlasnie", "o", "my", "u", "ciebie"}

# Ile pierwszych słów sprawdzamy, szukając wołania "Jarvis" w poleceniu.
SLOWA_WOLANIA = 3


def _slowa(tekst):
    return re.findall(r"[a-z]+", slowo_aktywujace.bez_ogonkow(tekst or ""))


def _jest_fraza(slowa, frazy):
    pary = set(zip(slowa, slowa[1:]))
    return any(fraza in pary for fraza in frazy)


def rodzaj(tekst):
    """
    "powitanie", "do_jarvisa" albo None — opis w nagłówku modułu.

    >>> rodzaj("Hej, wróciłem!"), rodzaj("Cześć Jarvis"), rodzaj("cześć")
    ('powitanie', 'powitanie', None)
    """
    slowa = _slowa(tekst)
    if not slowa:
        return None
    imie = [s for s in slowa if slowo_aktywujace.to_jarvis(s)]
    reszta = [s for s in slowa if not slowo_aktywujace.to_jarvis(s)]

    if len(slowa) <= MAKS_SLOW and all(s in OBECNOSC | POWITANIA | DOPELNIENIA for s in reszta):
        obecnosc = any(s in OBECNOSC for s in reszta) or _jest_fraza(reszta, OBECNOSC_FRAZY)
        powitanie = any(s in POWITANIA for s in reszta) or _jest_fraza(reszta, POWITANIA_FRAZY)
        if obecnosc or (powitanie and imie):
            return "powitanie"

    # "Hej Jarvis, puść muzykę" — imię na samym początku, jak po wake wordzie.
    if any(slowo_aktywujace.to_jarvis(s) for s in slowa[:SLOWA_WOLANIA]):
        return "do_jarvisa"
    return None


def czekaj_na_powitanie(sluchaj, przywitaj, polecenie, czy_zatrzymano, minuty, zegar=time.monotonic):
    """
    GŁÓWNE WEJŚCIE — okienko powitania. Wraca, gdy można przejść w zwykły tryb.

    sluchaj(limit_s)  — nasłuch bez wake worda (wake_word_listener): tekst,
                        "" (hałas bez słów) albo None (cisza przez limit_s)
    przywitaj(tekst)  — powitanie (main.py: rozmowa z jedną odpowiedzią)
    polecenie(tekst)  — zwykłe polecenie z "Jarvis" na początku
    czy_zatrzymano()  — zamykanie programu
    minuty            — długość okienka
    zegar             — do testów; domyślnie time.monotonic

    Zwraca, czym się skończyło: "powitanie", "polecenie", "czas" albo "zatrzymano".
    """
    koniec = zegar() + minuty * 60
    logger.info("[POWITANIE] Przez %d min czekam na powitanie — bez 'Hey Jarvis'.", minuty)

    while not czy_zatrzymano():
        zostalo = koniec - zegar()
        if zostalo <= 0:
            logger.info("[POWITANIE] %d min bez powitania — przechodzę w zwykły tryb.", minuty)
            return "czas"

        tekst = sluchaj(zostalo)
        if not tekst:
            continue                # cisza albo hałas bez słów

        co = rodzaj(tekst)
        if co == "powitanie":
            logger.info("[POWITANIE] Usłyszałem powitanie — witam i przechodzę w zwykły tryb.")
            przywitaj(tekst)
            return "powitanie"
        if co == "do_jarvisa":
            logger.info("[POWITANIE] Zawołano mnie z poleceniem — zwykła rozmowa.")
            polecenie(tekst)
            return "polecenie"
        # Bez treści: to mogła być rozmowa kogoś innego (opis modułu).
        logger.info("[POWITANIE] Pomijam wypowiedź (%d słów) — to nie powitanie.", len(tekst.split()))

    return "zatrzymano"
