"""
konfiguracja.py — skąd Jarvis bierze klucze i które funkcje są włączone.

Klucze (Anthropic, Spotify, Telegram, Gmail, kalendarz) wpisujesz w kreatorze
pierwszego uruchomienia albo później w Ustawieniach (menu ikony przy zegarku).
Wszystkie moduły czytają je przez os.getenv() — ten moduł dba o to, żeby
przed pierwszym odczytem były już na miejscu.


SKĄD KLUCZE — OD NAJWAŻNIEJSZEGO
================================

  1. Zmienne środowiskowe ustawione poza Jarvisem (np. w Windows).
  2. %APPDATA%\\Jarvis\\config.env — tu zapisują kreator i Ustawienia.
  3. .env w folderze programu — stary sposób, dalej działa.

Dlaczego Ustawienia wygrywają z .env: inaczej zmiana klucza w Ustawieniach
nic by nie dawała, dopóki ten sam klucz leży w .env. A skoro kreator nigdy
nie zapisuje do .env, Twoja dotychczasowa konfiguracja zostaje nietknięta.

Wyłączenie funkcji w Ustawieniach zapisuje PUSTY klucz w config.env ("KLUCZ=").
Pusta wartość też wygrywa z .env — więc da się wyłączyć funkcję
skonfigurowaną w .env bez ruszania tego pliku.


DLACZEGO %APPDATA%, A NIE FOLDER PROGRAMU
=========================================

Folder programu to kod: kopiujesz go, wrzucasz na GitHuba, podmieniasz przy
aktualizacji. Klucze są Twoje i nie powinny jechać razem z kodem. %APPDATA%
to miejsce, które Windows przeznacza na dane programów konkretnego
użytkownika — inni użytkownicy tego komputera do niego nie zajrzą.
"""

import logging
import os
from dataclasses import dataclass

from dotenv import dotenv_values, set_key

import sciezki

logger = logging.getLogger(__name__)

KATALOG = sciezki.UZYTKOWNIK
PLIK = os.path.join(KATALOG, "config.env")
# Stary .env — obok kodu. W wersji z instalatora nie istnieje (sprawdza to
# instalator/sprawdz_paczke.py), więc tam klucze są wyłącznie w Ustawieniach.
PLIK_PROJEKTU = os.path.join(sciezki.KOD, ".env")

# Wartości, których nie trzeba wpisywać — kreator podpowiada je sam.
DOMYSLNE = {"SPOTIPY_REDIRECT_URI": "http://127.0.0.1:8888/callback"}


@dataclass(frozen=True)
class Funkcja:
    nazwa: str              # do komunikatów: "Spotify nie jest skonfigurowany"
    zmienne: tuple          # wszystkie muszą być niepuste, żeby funkcja działała
    wymagana: bool = False  # bez niej Jarvis w ogóle nie wystartuje


FUNKCJE = {
    "anthropic": Funkcja("Klucz Anthropic", ("ANTHROPIC_API_KEY",), wymagana=True),
    "spotify": Funkcja("Spotify", ("SPOTIPY_CLIENT_ID", "SPOTIPY_CLIENT_SECRET",
                                   "SPOTIPY_REDIRECT_URI")),
    "telegram": Funkcja("Telegram", ("TELEGRAM_JARVIS_TOKEN", "TELEGRAM_OWNER_ID")),
    "gmail": Funkcja("Gmail", ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")),
    "kalendarz": Funkcja("Kalendarz Google", ("GOOGLE_CALENDAR_ICAL_URL",)),
}

_wczytano = False
_zrodla = {}     # zmienna -> "środowisko" / "ustawienia" / ".env" (dla okna Ustawień)


def wczytaj():
    """
    Ustawia klucze w os.environ według kolejności z opisu modułu.

    Woła to main.py na samym starcie i każdy moduł, który czyta klucze —
    dzięki temu `python kalendarz.py` z terminala też widzi Ustawienia.
    Działa raz; kolejne wywołania nic nie robią.
    """
    global _wczytano
    if _wczytano:
        return
    _wczytano = True

    ustawienia = dotenv_values(PLIK) if os.path.exists(PLIK) else {}
    projekt = dotenv_values(PLIK_PROJEKTU) if os.path.exists(PLIK_PROJEKTU) else {}

    for zmienna in sorted(set(ustawienia) | set(projekt)):
        if zmienna in os.environ:
            _zrodla[zmienna] = "środowisko"
            continue
        if zmienna in ustawienia:
            wartosc, zrodlo = ustawienia[zmienna], "ustawienia"
        else:
            wartosc, zrodlo = projekt[zmienna], ".env"
        if wartosc:
            os.environ[zmienna] = wartosc
            _zrodla[zmienna] = zrodlo

    for zmienna, wartosc in DOMYSLNE.items():
        os.environ.setdefault(zmienna, wartosc)

    logger.info("Konfiguracja: %s", ", ".join(
        f"{n} {'tak' if skonfigurowana(k) else 'nie'}" for k, n in
        (("anthropic", "Claude"), ("spotify", "Spotify"), ("telegram", "Telegram"),
         ("gmail", "Gmail"), ("kalendarz", "kalendarz"))))


def wartosc(zmienna):
    return os.environ.get(zmienna, "")


def zrodlo(zmienna):
    """Skąd pochodzi obecna wartość: "ustawienia", ".env", "środowisko" albo None."""
    return _zrodla.get(zmienna) if wartosc(zmienna) else None


def skonfigurowana(funkcja):
    """Czy funkcja ma komplet niepustych kluczy."""
    return all(wartosc(z).strip() for z in FUNKCJE[funkcja].zmienne)


def komunikat_braku(funkcja):
    """
    Wynik narzędzia, gdy funkcja nie jest skonfigurowana. Mówi modelowi wprost,
    co powiedzieć — zamiast zostawić go z ogólnym błędem do zgadywania.
    """
    nazwa = FUNKCJE[funkcja].nazwa
    return (f"NIE SKONFIGUROWANE: {nazwa} nie jest skonfigurowany, więc ta funkcja jest "
            "wyłączona. Powiedz to użytkownikowi wprost, jednym zdaniem, i dodaj, że klucze "
            "wpisze w Ustawieniach Jarvisa (prawy przycisk na ikonie przy zegarku, "
            "potem Ustawienia).")


# TRYB SŁUCHAWEK — przełącznik w menu ikony przy zegarku ("Tryb słuchawek").
#
# Normalnie przerwać Jarvisowi można tylko słowami "Hey Jarvis": przez
# głośniki mikrofon słyszy też jego głos, więc reakcja na KAŻDĄ mowę
# kończyłaby się tym, że Jarvis ucisza sam siebie. Na słuchawkach mikrofon
# go nie słyszy — wtedy wystarczy zacząć mówić, jak w rozmowie z człowiekiem.
# Włączony przy głośnikach: Jarvis będzie milkł po pierwszych słowach.
#
# Zapisywany w config.env jak klucze; w .env działa też JARVIS_TRYB_SLUCHAWEK=1.
TRYB_SLUCHAWEK = "JARVIS_TRYB_SLUCHAWEK"


def tryb_sluchawek():
    """Czy przerywa każda mowa (True), czy tylko "Hey Jarvis" (False)."""
    return wartosc(TRYB_SLUCHAWEK).strip().lower() in ("1", "tak", "true", "on")


def ustaw_tryb_sluchawek(wlaczony):
    """Włącza albo wyłącza tryb słuchawek — działa od następnej odpowiedzi."""
    zapisz({TRYB_SLUCHAWEK: "1" if wlaczony else "0"})
    logger.info("Tryb słuchawek: %s.", "włączony" if wlaczony else "wyłączony")


def zapisz(zmiany):
    """
    Zapisuje zmiany w %APPDATA%\\Jarvis\\config.env i od razu ustawia je
    w działającym programie. Pusta wartość wyłącza funkcję (opis modułu).

    zmiany — słownik {"ZMIENNA": "wartość"}
    """
    os.makedirs(KATALOG, exist_ok=True)
    if not os.path.exists(PLIK):
        with open(PLIK, "w", encoding="utf-8") as plik:
            plik.write("# Klucze i ustawienia Jarvisa — zapisuje je kreator, okno Ustawień\n"
                       "# i menu ikony przy zegarku.\n")

    for zmienna, nowa in zmiany.items():
        nowa = (nowa or "").strip()
        set_key(PLIK, zmienna, nowa, quote_mode="always", encoding="utf-8")
        if nowa:
            os.environ[zmienna] = nowa
            _zrodla[zmienna] = "ustawienia"
        else:
            os.environ.pop(zmienna, None)
            _zrodla.pop(zmienna, None)
    # Same nazwy — wartości to klucze, nie mają czego szukać w dzienniku.
    logger.info("Zapisano ustawienia: %s", ", ".join(sorted(zmiany)) or "(bez zmian)")
