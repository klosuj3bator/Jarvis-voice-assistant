"""
notes.py — notatki głosowe Jarvisa.

Mówisz "zanotuj, że trzeba kupić mleko", a Jarvis dopisuje to do pliku
z dzisiejszą datą. Później pytasz "co dziś zanotowałem?" i odczytuje listę.


DLACZEGO MARKDOWN I JEDEN PLIK NA DZIEŃ
=======================================

Notatki mają Cię przeżyć. Baza danych czy własny format oznaczałyby, że
dostęp do nich masz tylko przez Jarvisa — a on może kiedyś przestać działać.
Zwykły tekst otworzy wszystko: Notatnik, Obsidian (z którego korzystasz),
telefon, przeglądarka. Markdown dokłada do tego tyle, że lista wygląda
jak lista, a data jak nagłówek.

Jeden plik na dzień to kompromis między dwiema skrajnościami:

    plik na każdą notatkę  ->  katalog zasypany setkami plików
    jeden plik na wszystko ->  rośnie bez końca, trudno cokolwiek znaleźć

Dzień jest naturalną działką: "co dziś zanotowałem?" to jedno otwarcie pliku,
a folder z notatkami czyta się jak dziennik.

    notatki/
      2026-09-23.md
      2026-09-24.md

Nazwa pliku zaczyna się od roku, więc lista sortuje się chronologicznie sama
z siebie — inaczej niż przy formacie 24-09-2026.


CZEGO TU NIE MA
===============

Nie ma usuwania ani edycji. Notatka rzucona głosem to rzecz, którą łatwo
zapisać przez pomyłkę i trudno poprawić na głos — a plik tekstowy poprawisz
w dwie sekundy w dowolnym edytorze. Jarvis tylko dopisuje i czyta.
"""

import datetime
import logging
import os
import re

logger = logging.getLogger(__name__)

KATALOG_NOTATEK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notatki")

# Wzór linii z notatką: "- 18:45 — treść".
# Rozpoznajemy go przy czytaniu, więc zwykły tekst dopisany ręcznie
# (np. Twoje uwagi w Obsidianie) zostanie pominięty przy odczycie na głos.
WZOR_NOTATKI = re.compile(r"^-\s+(\d{2}:\d{2})\s+—\s+(.+)$")

# Ile notatek Jarvis odczyta na głos, zanim powie "i tak dalej". Przy dwudziestu
# notatkach czytanie wszystkiego trwałoby minutę i nikt by tego nie wysłuchał.
LIMIT_ODCZYTU = 10


def _sciezka_dnia(dzien=None):
    """Zwraca ścieżkę pliku z notatkami danego dnia (domyślnie dzisiejszego)."""
    dzien = dzien or datetime.date.today()
    return os.path.join(KATALOG_NOTATEK, f"{dzien.isoformat()}.md")


def _jedna_linia(tekst):
    """
    Spłaszcza tekst do jednej linii.

    Notatka jest jednym punktem listy — gdyby miała w środku znak nowej linii,
    rozpadłaby się na dwa punkty, z których drugi nie miałby godziny.
    """
    return " ".join(tekst.split())


def dopisz(tekst, teraz=None):
    """
    GŁÓWNE WEJŚCIE DO ZAPISU — dopisuje notatkę do dzisiejszego pliku.

    tekst — treść notatki
    teraz — moment zapisu; parametr istnieje dla testów, normalnie zostaw pusty

    Zwraca: (komunikat, czy_się_udało).
    """
    tekst = _jedna_linia(tekst or "")
    if not tekst:
        return "Pusta notatka — nie ma czego zapisać.", False

    teraz = teraz or datetime.datetime.now()
    sciezka = _sciezka_dnia(teraz.date())

    try:
        os.makedirs(KATALOG_NOTATEK, exist_ok=True)
        nowy = not os.path.exists(sciezka)

        # Tryb "a" (append) dopisuje na koniec i tworzy plik, jeśli go nie ma.
        # Nigdy nie nadpisuje — to ważne przy pliku z cudzymi notatkami.
        with open(sciezka, "a", encoding="utf-8") as plik:
            if nowy:
                plik.write(f"# Notatki — {teraz.strftime('%d.%m.%Y')}\n\n")
            plik.write(f"- {teraz.strftime('%H:%M')} — {tekst}\n")
    except OSError as e:
        logger.exception("Nie udało się zapisać notatki")
        return f"Nie udało się zapisać notatki: {e}", False

    logger.info("[NOTATKA] %s", tekst)
    return f"Zapisano notatkę o {teraz.strftime('%H:%M')}: {tekst}", True


def wczytaj(dzien=None):
    """
    Wczytuje notatki z danego dnia (domyślnie dzisiejszego).

    Zwraca: listę par (godzina, treść). Pusta lista = brak notatek.
    """
    sciezka = _sciezka_dnia(dzien)
    if not os.path.exists(sciezka):
        return []

    try:
        with open(sciezka, encoding="utf-8") as plik:
            tresc = plik.read()
    except OSError:
        logger.exception("Nie udało się odczytać notatek z %s", sciezka)
        return []

    notatki = []
    for linia in tresc.splitlines():
        dopasowanie = WZOR_NOTATKI.match(linia.strip())
        if dopasowanie:
            notatki.append((dopasowanie.group(1), dopasowanie.group(2)))
    return notatki


def z_dzisiaj():
    """
    GŁÓWNE WEJŚCIE DO ODCZYTU — dzisiejsze notatki jako gotowy tekst dla modelu.

    Zwraca: (komunikat, czy_się_udało). Brak notatek to NIE błąd —
    "dziś nic nie zanotowałeś" jest poprawną odpowiedzią na pytanie.
    """
    notatki = wczytaj()
    if not notatki:
        return "Brak notatek z dzisiaj.", True

    pokazane = notatki[:LIMIT_ODCZYTU]
    linie = [f"{godzina} — {tresc}" for godzina, tresc in pokazane]

    naglowek = f"Notatki z dzisiaj ({len(notatki)}):"
    if len(notatki) > len(pokazane):
        linie.append(f"(pozostałe {len(notatki) - len(pokazane)} są w pliku)")

    return naglowek + "\n" + "\n".join(linie), True


# --- Test samego modułu: `python notes.py` ---
if __name__ == "__main__":
    from logging_setup import skonfiguruj_logowanie

    skonfiguruj_logowanie()

    print(dopisz("przykładowa notatka z testu modułu"))
    komunikat, _ = z_dzisiaj()
    print(komunikat)
    print(f"\nPlik: {_sciezka_dnia()}")
