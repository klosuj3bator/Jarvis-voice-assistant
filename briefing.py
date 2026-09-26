"""
briefing.py — dane do porannego briefingu.

"Co dziś?", "poranny briefing", "good morning" — i Jarvis mówi jednym ciągiem:
jaki jest dzień, jaka pogoda, co masz dziś w kalendarzu i w przypomnieniach
i ile wczoraj zanotowałeś.


PODZIAŁ PRACY
=============

Ten moduł NIE układa zdań. Zbiera suche fakty i oddaje je modelowi,
a model robi z nich naturalną wypowiedź. To ten sam podział, co przy
pozostałych narzędziach, i ma tu dwie konkretne zalety:

  1. Model nie zna daty ani godziny — nie ma zegara. Bez tego narzędzia
     zgadywałby dzień tygodnia albo mówił "nie wiem, która godzina".
     Fakty z komputera są pewne; model dokłada do nich tylko język.

  2. Pogody tu nie ma, choć jest częścią briefingu. Model dociąga ją sam,
     wyszukiwarką internetową, której i tak używa do innych pytań.
     Gdyby briefing.py sam odpytywał serwis pogodowy, potrzebowałby
     osobnego klucza API i osobnej obsługi błędów — dla jednego zdania.
"""

import datetime
import logging

import kalendarz
import notes
import reminders

logger = logging.getLogger(__name__)

# Własne nazwy zamiast strftime("%A"): ten zwraca nazwy w języku ustawionym
# w systemie, więc na angielskim Windowsie Jarvis mówiłby "Friday".
DNI_TYGODNIA = ("poniedziałek", "wtorek", "środa", "czwartek",
                "piątek", "sobota", "niedziela")

# Miesiące w dopełniaczu — "26 września", nie "26 wrzesień".
MIESIACE = ("stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca",
            "lipca", "sierpnia", "września", "października", "listopada", "grudnia")


def _data_po_polsku(teraz):
    """"piątek, 26 września 2026, godzina 08:15"."""
    return (f"{DNI_TYGODNIA[teraz.weekday()]}, {teraz.day} "
            f"{MIESIACE[teraz.month - 1]} {teraz.year}, "
            f"godzina {teraz.strftime('%H:%M')}")


def _przypomnienia_na_dzis(teraz):
    """
    Przypomnienia, które jeszcze dziś się odezwą.

    Jutrzejszych nie pokazujemy — "co dziś" to pytanie o dzisiaj, a pełną
    listę da się usłyszeć osobno ("jakie mam przypomnienia").
    """
    dzisiejsze = []
    for wpis in reminders.aktywne():
        try:
            kiedy = datetime.datetime.fromisoformat(wpis["kiedy"])
        except (KeyError, ValueError):
            continue
        if kiedy.date() == teraz.date() and kiedy > teraz:
            dzisiejsze.append((kiedy, wpis.get("tresc") or "timer"))
    return dzisiejsze


def zbierz(teraz=None):
    """
    GŁÓWNE WEJŚCIE — fakty do briefingu jako tekst dla modelu.

    teraz — moment briefingu; parametr istnieje dla testów, normalnie zostaw pusty

    Zwraca: (komunikat, czy_się_udało).
    """
    teraz = teraz or datetime.datetime.now()
    linie = [f"Teraz: {_data_po_polsku(teraz)}."]

    # Każde źródło osobno w try — awaria jednego (np. uszkodzony plik
    # przypomnień) nie może zabrać całego briefingu.
    try:
        dzisiejsze = _przypomnienia_na_dzis(teraz)
        if dzisiejsze:
            opisy = "; ".join(f"{kiedy.strftime('%H:%M')} {tresc}"
                              for kiedy, tresc in dzisiejsze)
            linie.append(f"Przypomnienia na dziś ({len(dzisiejsze)}): {opisy}.")
        else:
            linie.append("Przypomnienia na dziś: brak.")
    except Exception:
        logger.exception("Briefing: nie udało się odczytać przypomnień")
        linie.append("Przypomnienia: nie udało się odczytać.")

    # Kalendarz Google — tylko gdy jest skonfigurowany (inaczej None i cisza).
    try:
        wpis = kalendarz.na_briefing(teraz)
        if wpis:
            linie.append(wpis)
    except Exception:
        logger.exception("Briefing: nie udało się odczytać kalendarza")
        linie.append("Kalendarz: nie udało się pobrać.")

    try:
        wczoraj = teraz.date() - datetime.timedelta(days=1)
        linie.append(f"Notatki zapisane wczoraj: {len(notes.wczytaj(wczoraj))}.")
    except Exception:
        logger.exception("Briefing: nie udało się odczytać notatek")
        linie.append("Notatki z wczoraj: nie udało się odczytać.")

    # Przypomnienie o pogodzie siedzi w WYNIKU, a nie tylko w opisie narzędzia.
    # Zmierzone na modelu: z samym opisem przy krótkim "co dziś?" pogodę
    # pomijał mniej więcej co drugi raz. Wynik czyta w chwili, gdy układa
    # odpowiedź — i wtedy o niej nie zapomina.
    linie.append(
        "Pogoda: nie ma jej w tych danych — sprawdź ją teraz wyszukiwarką dla "
        "miasta użytkownika z pamięci. Jeśli miasta nie znasz, pomiń pogodę "
        "i na końcu zapytaj, gdzie mieszka i czy to zapamiętać."
    )

    logger.info("[BRIEFING] %s", " | ".join(linie))
    return "\n".join(linie), True


# --- Test: `python briefing.py` ---
if __name__ == "__main__":
    from logging_setup import skonfiguruj_logowanie

    skonfiguruj_logowanie()
    print(zbierz()[0])
