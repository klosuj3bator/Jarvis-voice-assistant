"""
conftest.py — wspólne przygotowanie testów. pytest wczytuje ten plik, zanim
zaimportuje jakikolwiek test, więc to, co tu ustawimy, obowiązuje wszędzie.


TRZY ZASADY
===========

  1. ŻADNYCH TWOICH PLIKÓW. Moduły Jarvisa liczą swoje ścieżki (memory.json,
     reminders.json, notatki/, email_watches.json...) przy imporcie, przez
     sciezki.dane(). Zanim zaimportuje się którykolwiek z nich, przestawiamy
     sciezki.DANE i sciezki.UZYTKOWNIK na folder tymczasowy — więc wszystkie
     ścieżki lądują tam. Przed każdym testem ten folder jest czyszczony.
     Pilnuje tego test_izolacja.py.

  2. ŻADNYCH KLUCZY. Ustawienia (config.env) i .env też wskazują na folder
     tymczasowy, a zmienne z kluczami usuwamy ze środowiska. Test, który
     potrzebuje np. konta Gmail, ustawia zmyślone wartości sam
     (monkeypatch.setenv) — znikają po teście.

  3. ŻADNEJ SIECI. Każda próba połączenia poza ten komputer kończy się
     błędem SiecZablokowana. Claude API, Spotify, Gmail i Telegram zastępują
     atrapy (atrapy.py), więc testy nie zużywają tokenów, nie puszczają
     muzyki i niczego nie wysyłają. Gdyby jakiś test przez pomyłkę sięgnął
     po prawdziwą usługę, wywali się zamiast po cichu wysłać zapytanie.
"""

import os
import shutil
import socket
import sys
import tempfile

import pytest

KOD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if KOD not in sys.path:
    sys.path.insert(0, KOD)

FOLDER_TESTOW = tempfile.mkdtemp(prefix="jarvis_testy_")

# --- 1. Pliki: wszystko do folderu tymczasowego --------------------------
import sciezki  # noqa: E402

# Prawdziwe położenie danych — tylko do porównania w test_izolacja.py.
PRAWDZIWE_FOLDERY = (sciezki.KOD, os.path.join(os.environ.get("APPDATA") or "~", "Jarvis"))
# Prawdziwe modele — test indeksu notatek CZYTA stąd pobrany model, jeśli
# jest (nic tam nie zapisuje). Bez niego ten jeden test jest pomijany.
PRAWDZIWY_FOLDER_MODELI = sciezki.MODELE

sciezki.DANE = os.path.join(FOLDER_TESTOW, "dane")
sciezki.UZYTKOWNIK = os.path.join(FOLDER_TESTOW, "appdata")
sciezki.MODELE = os.path.join(sciezki.UZYTKOWNIK, "modele")

# --- 2. Klucze: ani config.env, ani .env, ani zmienne środowiskowe -------
import konfiguracja  # noqa: E402  (KATALOG i PLIK liczy już z podmienionego folderu)

konfiguracja.PLIK_PROJEKTU = os.path.join(FOLDER_TESTOW, "brak.env")
for _zmienna in list(os.environ):
    if konfiguracja._tajna(_zmienna) or _zmienna.startswith(
            ("ANTHROPIC_", "TELEGRAM_", "GMAIL_", "SPOTIPY_", "GOOGLE_CALENDAR", "JARVIS_")):
        del os.environ[_zmienna]


# --- 3. Sieć: tylko ten komputer ----------------------------------------
class SiecZablokowana(RuntimeError):
    """Test próbował połączyć się z internetem — a ma używać atrap."""


_LOKALNE = {"127.0.0.1", "::1", "localhost"}
_polacz = socket.socket.connect
_polacz_ex = socket.socket.connect_ex


def _tylko_lokalnie(prawdziwa):
    def polacz(gniazdo, adres, *argumenty):
        host = adres[0] if isinstance(adres, tuple) else adres
        if host not in _LOKALNE:
            raise SiecZablokowana(f"Testy nie łączą się z siecią (próba połączenia z {host}). "
                                  "Użyj atrapy z tests/atrapy.py.")
        return prawdziwa(gniazdo, adres, *argumenty)
    return polacz


# SSL, requests, imaplib i biblioteka anthropic łączą się przez socket.socket.
socket.socket.connect = _tylko_lokalnie(_polacz)
socket.socket.connect_ex = _tylko_lokalnie(_polacz_ex)


# --- Przed każdym testem ------------------------------------------------
@pytest.fixture(autouse=True)
def czyste_dane():
    """Pusty folder danych przed każdym testem — testy nie widzą swoich plików."""
    for folder in (sciezki.DANE, sciezki.UZYTKOWNIK):
        shutil.rmtree(folder, ignore_errors=True)
        os.makedirs(folder)
    # Pamięć trzyma tekst faktów w pamięci podręcznej (opis w pamiec.py).
    if "pamiec" in sys.modules:
        sys.modules["pamiec"]._prompt_pamieci["zmieniono"] = object()
    yield
    # Atrapa Claude z jednego testu nie może przeciec do następnego.
    if "agent" in sys.modules:
        sys.modules["agent"]._klient = None


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(FOLDER_TESTOW, ignore_errors=True)
