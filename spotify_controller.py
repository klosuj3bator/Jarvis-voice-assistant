"""
spotify_controller.py — moduł sterowania Spotify dla asystenta Jarvis.

Odpowiada za: logowanie (OAuth), wyszukiwanie utworów i sterowanie odtwarzaniem.
Reszta Jarvisa (mowa, rozpoznawanie komend) będzie tylko wołać funkcje z tego pliku.
"""

import logging
import os
import time

import spotipy
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth

# Wczytuje zmienne z pliku .env do zmiennych środowiskowych procesu.
# Dzięki temu klucze API nie siedzą na sztywno w kodzie (i nie trafią na GitHuba).
load_dotenv()

logger = logging.getLogger(__name__)

# Zakresy uprawnień, o które prosimy użytkownika przy logowaniu.
# To minimum potrzebne do sterowania odtwarzaniem:
#   user-read-playback-state   -> odczyt: jakie urządzenia, co aktualnie gra
#   user-modify-playback-state -> zapis: play / pause / next / zmiana urządzenia
SCOPE = "user-read-playback-state user-modify-playback-state"

# Plik, w którym spotipy zapisze token po pierwszym zalogowaniu.
# Dzięki niemu przeglądarka otworzy się tylko raz — potem token jest odświeżany automatycznie.
CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".spotify_cache")


def zaloguj():
    """
    Loguje do Spotify przez Authorization Code Flow i zwraca gotowego klienta API.

    Authorization Code Flow (a nie Client Credentials) — bo tylko ten działa
    "w imieniu użytkownika", czyli pozwala sterować odtwarzaniem na jego koncie.

    Przy pierwszym uruchomieniu spotipy otworzy przeglądarkę z prośbą o zgodę.
    Po kliknięciu "Agree" Spotify przekieruje na Twój redirect URI — skopiuj wtedy
    CAŁY adres z paska przeglądarki i wklej go do terminala.

    Zwraca: obiekt spotipy.Spotify (klient API).
    """
    client_id = os.getenv("SPOTIPY_CLIENT_ID")
    client_secret = os.getenv("SPOTIPY_CLIENT_SECRET")
    redirect_uri = os.getenv("SPOTIPY_REDIRECT_URI")

    # Prosta walidacja — lepiej dostać czytelny błąd tutaj niż kryptyczny później.
    if not client_id or not client_secret or not redirect_uri:
        raise RuntimeError(
            "Brakuje danych logowania. Sprawdź, czy plik .env leży obok tego skryptu "
            "i zawiera SPOTIPY_CLIENT_ID, SPOTIPY_CLIENT_SECRET oraz SPOTIPY_REDIRECT_URI."
        )

    auth_manager = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=SCOPE,
        cache_path=CACHE_PATH,
        open_browser=True,
    )

    return spotipy.Spotify(auth_manager=auth_manager)


def znajdz_utwor(sp, tytul, wykonawca=None):
    """
    Wyszukuje utwór w Spotify i zwraca jego URI (np. "spotify:track:4cOdK2wGLETKBW3PvgPWqT").

    sp        — klient zwrócony przez zaloguj()
    tytul     — tytuł piosenki, np. "Bohemian Rhapsody"
    wykonawca — opcjonalnie wykonawca; mocno zawęża wyniki, gdy tytuł jest popularny

    Zwraca: krotkę (uri, opis) albo (None, None) jeśli nic nie znaleziono.
    """
    # Spotify rozumie w zapytaniu filtry pól: track: i artist:.
    # Dają dużo trafniejsze wyniki niż wrzucenie wszystkiego jako zwykły tekst.
    zapytanie = f"track:{tytul}"
    if wykonawca:
        zapytanie += f" artist:{wykonawca}"

    wyniki = sp.search(q=zapytanie, type="track", limit=1)
    utwory = wyniki["tracks"]["items"]

    # Plan B: jeśli filtry nic nie dały (literówka, tytuł po polsku, remiks),
    # szukamy jeszcze raz "luźno" — samym tekstem.
    if not utwory:
        luzne_zapytanie = f"{tytul} {wykonawca}" if wykonawca else tytul
        wyniki = sp.search(q=luzne_zapytanie, type="track", limit=1)
        utwory = wyniki["tracks"]["items"]

    if not utwory:
        return None, None

    utwor = utwory[0]
    # Wykonawców może być kilku (feat.), więc sklejamy ich nazwy przecinkami.
    artysci = ", ".join(a["name"] for a in utwor["artists"])
    opis = f"{artysci} — {utwor['name']}"

    return utwor["uri"], opis


def znajdz_aktywne_urzadzenie(sp):
    """
    Sprawdza, na jakich urządzeniach user jest zalogowany do Spotify
    i zwraca ID tego aktywnego (czyli tego, na którym można coś odtworzyć).

    Zwraca: (device_id, komunikat) — device_id to None, jeśli nie ma aktywnego urządzenia.
    Komunikat jest zawsze tekstem gotowym do wypowiedzenia/wypisania przez Jarvisa.
    """
    urzadzenia = sp.devices()["devices"]

    if not urzadzenia:
        return None, (
            "Nie widzę żadnego urządzenia Spotify. "
            "Otwórz aplikację Spotify na komputerze lub telefonie i spróbuj ponownie."
        )

    # Spotify oznacza jako "is_active" to urządzenie, które aktualnie odtwarza
    # lub było ostatnio używane w tej sesji.
    for urzadzenie in urzadzenia:
        if urzadzenie["is_active"]:
            return urzadzenie["id"], f"Aktywne urządzenie: {urzadzenie['name']}"

    # Aplikacja jest otwarta, ale "uśpiona" — Spotify nie uzna jej za aktywną,
    # dopóki coś na niej nie zagra. Mówimy o tym wprost, zamiast po cichu zgadywać.
    nazwy = ", ".join(u["name"] for u in urzadzenia)
    return None, (
        f"Znalazłem urządzenia ({nazwy}), ale żadne nie jest aktywne. "
        "Kliknij play w aplikacji Spotify, żeby ją obudzić, i spróbuj ponownie."
    )


def odtworz(sp, uri, device_id=None):
    """
    Odtwarza podany utwór.

    sp        — klient z zaloguj()
    uri       — URI utworu z znajdz_utwor()
    device_id — opcjonalnie konkretne urządzenie; gdy None, Spotify użyje aktywnego

    Zwraca: (True/False, komunikat) — True gdy się udało.
    """
    try:
        # uris= to lista, bo ten endpoint umie też przyjąć całą kolejkę utworów.
        sp.start_playback(device_id=device_id, uris=[uri])
        return True, "Odtwarzam."
    except spotipy.SpotifyException as e:
        # 403 to najczęstszy problem: konto darmowe (sterowanie wymaga Premium)
        # albo odtwarzanie zablokowane w danym momencie.
        if e.http_status == 403:
            return False, "Spotify odmówiło odtwarzania. Sterowanie playbackiem wymaga konta Premium."
        if e.http_status == 404:
            return False, "Nie znalazłem urządzenia do odtwarzania. Otwórz aplikację Spotify."
        return False, f"Błąd Spotify: {e.msg}"


# Ile łącznie sekund czekamy, aż świeżo otwarte Spotify zgłosi się do API.
CZAS_OCZEKIWANIA_NA_SPOTIFY = 15

# Co ile sekund odpytujemy API w trakcie czekania.
ODSTEP_SPRAWDZANIA = 2


def uruchom_i_poczekaj_na_spotify(sp):
    """
    Otwiera aplikację Spotify i czeka, aż pojawi się jako urządzenie w API.

    "spotify:" to protokół URI zarejestrowany przez aplikację w systemie —
    `start spotify:` mówi Windowsowi "otwórz to czymkolwiek, co obsługuje spotify:",
    więc działa niezależnie od tego, gdzie aplikacja jest zainstalowana
    (a Spotify z Microsoft Store nie ma normalnej ścieżki do .exe).

    Zwraca: device_id gotowe do grania, albo None jeśli się nie doczekaliśmy.
    """
    logger.info("Otwieram aplikację Spotify...")
    os.system("start spotify:")

    # Odpytujemy w pętli zamiast jednego długiego sleep(): jeśli Spotify wstanie
    # po 3 sekundach, nie ma powodu czekać pełnych 15.
    for _ in range(CZAS_OCZEKIWANIA_NA_SPOTIFY // ODSTEP_SPRAWDZANIA):
        time.sleep(ODSTEP_SPRAWDZANIA)

        urzadzenia = sp.devices()["devices"]
        if not urzadzenia:
            continue

        # Świeżo uruchomione Spotify JEST widoczne w API, ale ma is_active=False,
        # bo jeszcze nic nie gra. Dlatego nie szukamy tu aktywnego urządzenia —
        # bierzemy pierwsze z brzegu. start_playback() z podanym device_id
        # sam przełączy na nie odtwarzanie.
        aktywne = next((u for u in urzadzenia if u["is_active"]), None)
        urzadzenie = aktywne or urzadzenia[0]

        logger.info("Spotify gotowe: %s", urzadzenie["name"])
        return urzadzenie["id"]

    return None


def zagraj_piosenke(tytul, wykonawca=None):
    """
    Wygodne "wszystko na raz" — funkcja, którą docelowo zawoła Jarvis
    po rozpoznaniu komendy głosowej: zaloguj → znajdź → sprawdź urządzenie → graj.

    Zwraca: (komunikat dla użytkownika, czy się udało).
    Flaga sukcesu jest potrzebna GUI — decyduje, czy koło wróci spokojnie
    do idle, czy błyśnie na czerwono.
    """
    sp = zaloguj()

    uri, opis = znajdz_utwor(sp, tytul, wykonawca)
    if uri is None:
        return f"Nie znalazłem utworu: {tytul}.", False

    device_id, komunikat_urzadzenia = znajdz_aktywne_urzadzenie(sp)

    # Nie ma na czym grać — próbujemy otworzyć aplikację Spotify i poczekać,
    # aż zgłosi się do API jako urządzenie.
    if device_id is None:
        logger.info(komunikat_urzadzenia)
        device_id = uruchom_i_poczekaj_na_spotify(sp)

    if device_id is None:
        return (
            "Nie udało mi się uruchomić Spotify. "
            "Otwórz aplikację ręcznie i spróbuj jeszcze raz.",
            False,
        )

    sukces, komunikat = odtworz(sp, uri, device_id)
    if not sukces:
        return komunikat, False

    return f"Odtwarzam {opis}.", True


# --- Blok testowy: uruchom `python spotify_controller.py`, żeby sprawdzić cały flow ---
if __name__ == "__main__":
    from logging_setup import skonfiguruj_logowanie

    skonfiguruj_logowanie()
    TESTOWY_TYTUL = "Bohemian Rhapsody"
    TESTOWY_WYKONAWCA = "Queen"

    logger.info("[1/4] Logowanie do Spotify...")
    sp = zaloguj()
    ja = sp.current_user()
    logger.info("      Zalogowano jako: %s", ja["display_name"])

    logger.info("[2/4] Szukam utworu: %s - %s", TESTOWY_TYTUL, TESTOWY_WYKONAWCA)
    uri, opis = znajdz_utwor(sp, TESTOWY_TYTUL, TESTOWY_WYKONAWCA)
    if uri is None:
        logger.error("      Nie znaleziono utworu. Koniec testu.")
        raise SystemExit(1)
    logger.info("      Znaleziono: %s", opis)
    logger.info("      URI: %s", uri)

    logger.info("[3/4] Sprawdzam urządzenia...")
    device_id, komunikat = znajdz_aktywne_urzadzenie(sp)
    logger.info("      %s", komunikat)
    if device_id is None:
        raise SystemExit(1)

    logger.info("[4/4] Odtwarzam...")
    sukces, komunikat = odtworz(sp, uri, device_id)
    logger.info("      %s", komunikat)
