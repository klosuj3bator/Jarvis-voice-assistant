"""
slownik.py — słownik podpowiedzi dla Whispera: nazwy własne, których używasz.

Whisper zna język, ale nie zna Twojej muzyki. "Kaz Bałagane" wychodziło mu
jako "kazba łagany", a "RIOTT od Otsochodzi" jako "Riot od co chodzi" — i potem
Spotify szukało nie tego, co trzeba. Wystarczy mu jednak PODPOWIEDZIEĆ, jakie
nazwy mogą paść, a trafia w nie od razu.


SKĄD NAZWY (w kolejności ważności)
==================================

  1. Twoje ręczne wpisy — "Jarvis, zapamiętaj słowo Tarcho Terror"
     (narzędzie add_vocabulary_word w agent.py)
  2. Nazwy własne z pamięci długoterminowej (memory.json)
  3. Spotify: 10 wykonawców, których ostatnio słuchałeś najczęściej
  4. Aplikacje z apps_config.json i apps_cache.json
  5. Spotify: obserwowani, reszta ostatnio słuchanych, albumy

Aplikacje są dopiero za czołówką wykonawców, bo to zwykle znane słowa (Steam,
Chrome, Teams) — "League of Legends" Whisper trafiał i bez słownika. Mylił się
na muzyce, więc to jej należy się miejsce na początku. Ale tylko czołówce —
inaczej przy długiej historii słuchania aplikacje nie zmieściłyby się wcale.

Automatyczną część zbieramy raz dziennie (a nie przy każdej komendzie) i trzymamy
w slownik.json. Ręczne wpisy działają od razu.


DLACZEGO SŁOWNIK JEST KRÓTKI
============================

Podpowiedź to dodatkowy tekst, który Whisper czyta przed każdym rozpoznaniem,
więc im dłuższa, tym wolniej. Zmierzone 26.09 (model small, CPU, komendy
nagrane syntezatorem Jarvisa):

    podpowiedź       czas komendy   "RIOTT od Otsochodzi"   "Kaz Bałagane"
    brak               5,5 s        "Riot od od co chodzi"  "kawałagane"
    8 nazw  (31 tok)   5,4 s        dobrze                  (nie było na liście)
    15 nazw (59 tok)   5,7 s        dobrze                  (nie było na liście)
    30 nazw (117 tok)  6,2 s        dobrze                  dobrze
    60 nazw (232 tok)  7,2 s        dobrze                  dobrze

Pomaga tylko nazwom, które są NA liście, a każde ~10 tokenów to ~0,07 s
czekania. Stąd limit MAKS_ZNAKOW i kolejność ważności powyżej. Na ciszy
i szumie Whisper z podpowiedzią niczego nie zmyślał (sprawdzone).


SPOTIFY — JEDNORAZOWA ZGODA
===========================

Historia słuchania i obserwowani wykonawcy wymagają innych uprawnień niż
sterowanie odtwarzaniem. Nie ruszamy tokenu, którym Jarvis puszcza muzykę —
słownik ma własny (.spotify_cache_slownik). Zgodę dajesz raz:

    python slownik.py spotify

Do tego czasu słownik działa bez Spotify. Odświeżanie w tle NIGDY samo nie
otwiera przeglądarki — używa wyłącznie zapisanej zgody.
"""

import datetime
import json
import logging
import os
import re
import threading
from collections import Counter

from dotenv import load_dotenv

import pamiec

load_dotenv()

logger = logging.getLogger(__name__)

KATALOG = os.path.dirname(os.path.abspath(__file__))
SCIEZKA = os.path.join(KATALOG, "slownik.json")
SCIEZKA_TOKENU_SPOTIFY = os.path.join(KATALOG, ".spotify_cache_slownik")

# Tylko odczyt: co ostatnio grało i kogo obserwujesz.
UPRAWNIENIA_SPOTIFY = "user-read-recently-played user-follow-read"

# Co ile godzin zbieramy automatyczną część od nowa.
CO_ILE_H = 24

# Długość podpowiedzi w znakach. Nazwy wychodzą średnio ~2,5 znaku na token,
# więc 250 znaków to ~100 tokenów: ok. 25 nazw i ~0,5 s dłuższe rozpoznawanie
# (tabela w opisie modułu). Więcej trafionych nazw kosztem czekania — podnieś;
# szybciej — obniż.
MAKS_ZNAKOW = 250

# Ilu najczęściej słuchanych wykonawców idzie PRZED aplikacjami (opis modułu).
CZOLOWKA_WYKONAWCOW = 10

# Ręczne wpisy: górny limit liczby i długości jednego wpisu.
MAKS_RECZNYCH = 100
MAKS_ZNAKOW_SLOWA = 60

# Słowa, które w faktach z pamięci zaczynają się wielką literą, ale nazwami
# własnymi nie są (początek zdania, "Użytkownik chce...").
NIE_NAZWY = {
    "użytkownik", "użytkownika", "użytkownikowi", "gdy", "kiedy", "jarvis",
    "jarvisa", "nie", "to", "ma", "jest", "lubi", "chce", "pracuje", "mieszka",
    "jego", "jej", "ulubiony", "ulubiona", "ulubione", "mistrzu",
}

_blokada = threading.Lock()
_dane = None                # zawartość slownik.json, wczytana raz
_podpowiedz = None          # gotowy tekst dla Whispera
_watek_odswiezania = None


# ---------------------------------------------------------------
# Plik
# ---------------------------------------------------------------

def _pusty():
    return {"reczne": [], "automatyczne": {}, "odswiezono": None, "spotify": None}


def _wczytaj():
    try:
        with open(SCIEZKA, encoding="utf-8") as plik:
            dane = _pusty() | json.load(plik)
        return dane
    except FileNotFoundError:
        return _pusty()
    except (OSError, ValueError, TypeError) as e:
        logger.error("Nie udało się wczytać %s (%s) — zaczynam od pustego słownika.", SCIEZKA, e)
        return _pusty()


def _zapisz(dane):
    tymczasowy = SCIEZKA + ".tmp"
    with open(tymczasowy, "w", encoding="utf-8") as plik:
        json.dump(dane, plik, indent=2, ensure_ascii=False)
    os.replace(tymczasowy, SCIEZKA)


def _stan(z_pliku=False):
    """
    Dane słownika — z pliku przy pierwszym użyciu, potem z pamięci.

    z_pliku=True czyta plik od nowa. Tak robi każdy ZAPIS — gdybyś poprawił
    slownik.json w notatniku, gdy Jarvis działa, zapis ze starej kopii
    w pamięci skasowałby Twoje poprawki.
    """
    global _dane
    if _dane is None or z_pliku:
        _dane = _wczytaj()
    return _dane


# ---------------------------------------------------------------
# Źródła nazw
# ---------------------------------------------------------------

LACZNIKI = {"of", "the", "and", "for", "to", "a", "do", "i", "w", "z", "na", "od"}


def _ladnie(nazwa):
    """
    "league of legends" -> "League of Legends", "opera gx" -> "Opera GX".

    Aplikacje w plikach Jarvisa są zapisane małymi literami, a Whisper
    przepisuje nazwę tak, jak ją widzi w podpowiedzi.
    """
    wyrazy = nazwa.split()
    wynik = []
    for i, w in enumerate(wyrazy):
        if i and w in LACZNIKI:
            wynik.append(w)
        elif len(w) <= 3 and w not in LACZNIKI:
            wynik.append(w.upper())
        else:
            wynik.append(w[:1].upper() + w[1:])
    return " ".join(wynik)


def _z_aplikacji():
    nazwy = []
    for plik in ("apps_config.json", "apps_cache.json"):
        try:
            with open(os.path.join(KATALOG, plik), encoding="utf-8") as f:
                dane = json.load(f)
        except (OSError, ValueError):
            continue
        nazwy += [_ladnie(k) for k in dane if isinstance(k, str) and not k.startswith("_")]
    return nazwy


def _z_pamieci():
    """
    Nazwy własne z faktów: ciągi słów pisanych wielką literą.
    "chodzi mu o album RIOTT Otsochodzi" -> "RIOTT Otsochodzi".
    """
    nazwy = []
    for fakt in pamiec.fakty():
        ciag = []
        for wyraz in re.findall(r"[\w\-]+", fakt) + [""]:
            if wyraz[:1].isupper() and wyraz.lower() not in NIE_NAZWY:
                ciag.append(wyraz)
            elif ciag and wyraz.isdigit():
                ciag.append(wyraz)      # "White 2115" to jedna nazwa
            elif ciag:
                nazwy.append(" ".join(ciag))
                ciag = []
    return nazwy


def _klient_spotify(interaktywnie=False):
    """
    Klient Spotify z uprawnieniami słownika.

    Bez interaktywnie=True korzystamy WYŁĄCZNIE z zapisanej zgody. Gdy jej nie
    ma, oddajemy None — wątek w tle nie może nagle otworzyć przeglądarki
    i czekać, aż ktoś kliknie "Zgadzam się".
    """
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth

    logowanie = SpotifyOAuth(
        client_id=os.getenv("SPOTIPY_CLIENT_ID"),
        client_secret=os.getenv("SPOTIPY_CLIENT_SECRET"),
        redirect_uri=os.getenv("SPOTIPY_REDIRECT_URI"),
        scope=UPRAWNIENIA_SPOTIFY,
        cache_path=SCIEZKA_TOKENU_SPOTIFY,
        open_browser=interaktywnie,
    )
    if interaktywnie:
        return spotipy.Spotify(auth_manager=logowanie)

    # validate_token sam odświeża przeterminowany token; None = brak zgody.
    token = logowanie.validate_token(logowanie.cache_handler.get_cached_token())
    return spotipy.Spotify(auth=token["access_token"]) if token else None


def _ze_spotify():
    """
    Zwraca: (wykonawcy_ostatnio, obserwowani, albumy, opis_stanu).
    Najczęściej słuchani pierwsi.
    """
    if not os.getenv("SPOTIPY_CLIENT_ID"):
        return [], [], [], "brak kluczy Spotify w .env"
    try:
        sp = _klient_spotify()
        if sp is None:
            return [], [], [], "brak zgody — uruchom: python slownik.py spotify"

        wykonawcy, albumy = Counter(), Counter()
        for pozycja in sp.current_user_recently_played(limit=50).get("items", []):
            utwor = pozycja.get("track") or {}
            for artysta in utwor.get("artists", []):
                wykonawcy[artysta["name"]] += 1
            if (utwor.get("album") or {}).get("name"):
                albumy[utwor["album"]["name"]] += 1

        obserwowani = [a["name"] for a in
                       sp.current_user_followed_artists(limit=50)["artists"]["items"]]
        return ([n for n, _ in wykonawcy.most_common()], obserwowani,
                [n for n, _ in albumy.most_common()], "ok")
    except Exception as e:
        logger.warning("[SŁOWNIK] Spotify: %s", e)
        return [], [], [], f"błąd: {e}"


# ---------------------------------------------------------------
# Odświeżanie i podpowiedź
# ---------------------------------------------------------------

def odswiez():
    """Zbiera automatyczną część słownika od nowa. Zwraca opis dla dziennika."""
    global _podpowiedz
    wykonawcy, obserwowani, albumy, stan_spotify = _ze_spotify()
    automatyczne = {
        "aplikacje": _z_aplikacji(),
        "pamiec": _z_pamieci(),
        "spotify_wykonawcy": wykonawcy,
        "spotify_obserwowani": obserwowani,
        "spotify_albumy": albumy,
    }
    with _blokada:
        dane = _stan(z_pliku=True)
        dane["automatyczne"] = automatyczne
        dane["odswiezono"] = datetime.datetime.now().isoformat(timespec="seconds")
        dane["spotify"] = stan_spotify
        _zapisz(dane)
        _podpowiedz = None
    opis = ", ".join(f"{k}: {len(v)}" for k, v in automatyczne.items())
    logger.info("[SŁOWNIK] Odświeżony (%s; Spotify: %s).", opis, stan_spotify)
    return opis


def _nieaktualny(dane):
    try:
        wiek = datetime.datetime.now() - datetime.datetime.fromisoformat(dane["odswiezono"])
        return wiek > datetime.timedelta(hours=CO_ILE_H)
    except (TypeError, ValueError):
        return True


def _odswiez_w_tle():
    """Odświeżenie w osobnym wątku — rozpoznawanie mowy nie czeka na Spotify."""
    global _watek_odswiezania
    if _watek_odswiezania is not None and _watek_odswiezania.is_alive():
        return

    def praca():
        try:
            odswiez()
        except Exception:
            logger.exception("[SŁOWNIK] Odświeżanie nie powiodło się")

    _watek_odswiezania = threading.Thread(target=praca, name="watek-slownika", daemon=True)
    _watek_odswiezania.start()


def _zbuduj(dane):
    """Nazwy w kolejności ważności, bez powtórzeń, przycięte do MAKS_ZNAKOW."""
    auto = dane.get("automatyczne") or {}
    wykonawcy = auto.get("spotify_wykonawcy", [])
    kolejnosc = ([r["slowo"] for r in dane.get("reczne", [])]
                 + auto.get("pamiec", []) + wykonawcy[:CZOLOWKA_WYKONAWCOW]
                 + auto.get("aplikacje", []) + auto.get("spotify_obserwowani", [])
                 + wykonawcy[CZOLOWKA_WYKONAWCOW:] + auto.get("spotify_albumy", []))
    widziane, wybrane, dlugosc = set(), [], 0
    for nazwa in kolejnosc:
        nazwa = " ".join(str(nazwa).split())
        if not nazwa or nazwa.lower() in widziane:
            continue
        if dlugosc + len(nazwa) + 2 > MAKS_ZNAKOW:
            break
        widziane.add(nazwa.lower())
        wybrane.append(nazwa)
        dlugosc += len(nazwa) + 2
    return ", ".join(wybrane)


def podpowiedzi():
    """
    GŁÓWNE WEJŚCIE — tekst podpowiedzi dla Whispera (hotwords). Woła to
    wake_word_listener.py przy każdym rozpoznaniu, więc musi być natychmiastowe:
    oddaje gotowy tekst z pamięci, a raz na dobę zleca odświeżenie w tle.

    Zwraca: "Nazwa, Nazwa, ..." albo pusty napis.
    """
    global _podpowiedz
    with _blokada:
        dane = _stan()
        if _nieaktualny(dane):
            _odswiez_w_tle()
        if _podpowiedz is None:
            _podpowiedz = _zbuduj(dane)
        return _podpowiedz


def dodaj_slowo(slowo):
    """
    Dopisuje nazwę do słownika (narzędzie add_vocabulary_word). Działa od
    następnej komendy — bez czekania na dobowe odświeżenie.

    Zwraca: (komunikat dla modelu, czy_się_udało).
    """
    global _podpowiedz
    slowo = " ".join((slowo or "").strip(" \t\n\"'„”.,!?").split())
    if not slowo:
        return "Nie usłyszałem, jakie słowo dopisać.", False
    if len(slowo) > MAKS_ZNAKOW_SLOWA:
        return f"To za długie na jedną nazwę (limit {MAKS_ZNAKOW_SLOWA} znaków).", False
    if pamiec.wyglada_na_sekret(slowo):
        return "NIE ZAPISANO: to wygląda na hasło, klucz albo kod — takich rzeczy nie przechowuję.", False

    with _blokada:
        dane = _stan(z_pliku=True)
        if any(r["slowo"].lower() == slowo.lower() for r in dane["reczne"]):
            return f"„{slowo}” już jest w słowniku.", True
        if len(dane["reczne"]) >= MAKS_RECZNYCH:
            return f"Słownik ręczny jest pełny ({MAKS_RECZNYCH} słów).", False
        # Najnowsze na początek — mają pierwszeństwo, gdy brakuje miejsca.
        dane["reczne"].insert(0, {"slowo": slowo,
                                  "dodano": datetime.date.today().isoformat()})
        _zapisz(dane)
        _podpowiedz = None

    logger.info("[SŁOWNIK] Dopisane ręcznie: %s", slowo)
    return (f"Dopisane do słownika dokładnie jako: „{slowo}”. Działa od następnej "
            "komendy."), True


# --- `python slownik.py` — podgląd; `python slownik.py spotify` — zgoda Spotify ---
if __name__ == "__main__":
    import sys

    from logging_setup import skonfiguruj_logowanie

    skonfiguruj_logowanie()

    if sys.argv[1:] == ["spotify"]:
        print("Otwieram przeglądarkę — zaakceptuj dostęp do historii słuchania "
              "i obserwowanych wykonawców (tylko odczyt).")
        uzytkownik = _klient_spotify(interaktywnie=True).current_user()
        print(f"Gotowe, zalogowano jako {uzytkownik.get('display_name')}.")

    print("Odświeżam:", odswiez())
    dane = _stan()
    print(f"Spotify: {dane['spotify']}")
    print(f"Ręczne ({len(dane['reczne'])}):", ", ".join(r["slowo"] for r in dane["reczne"]) or "-")
    for zrodlo, nazwy in dane["automatyczne"].items():
        print(f"{zrodlo} ({len(nazwy)}):", ", ".join(nazwy[:15]) or "-")
    tekst = podpowiedzi()
    print(f"\nPODPOWIEDŹ DLA WHISPERA ({len(tekst)} znaków, limit {MAKS_ZNAKOW}):\n{tekst}")
