"""
spotify_controller.py — moduł sterowania Spotify dla asystenta Jarvis.

Odpowiada za: logowanie (OAuth), wyszukiwanie utworów i sterowanie odtwarzaniem.
Reszta Jarvisa (mowa, rozpoznawanie komend) będzie tylko wołać funkcje z tego pliku.
"""

import difflib
import logging
import os
import re
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


# Ile wyników pobieramy do oceny. Pierwszy z brzegu bywa przypadkowy,
# więc oglądamy kilkanaście i wybieramy sami.
LICZBA_KANDYDATOW = 10

# Minimalne podobieństwo tytułu, żeby uznać album za trafiony (0.0-1.0).
# Bez tego progu Spotify ZAWSZE coś zwraca — nawet na kompletny bełkot
# podsuwa najbliższą losową płytę, a Jarvis grałby ją bez mrugnięcia okiem.
PROG_TYTULU_ALBUMU = 0.6


def _uprosc_tytul(tytul):
    """
    Sprowadza tytuł do postaci porównywalnej.

    Usuwa dopiski wydawnicze w nawiasach ("(Remastered)", "(Deluxe Edition)"),
    rodzajnik "the" na początku i znaki przestankowe. Dzięki temu
    "The Dark Side of the Moon" i "Dark Side of the Moon (2011 Remaster)"
    są rozpoznawane jako ten sam tytuł.
    """
    tekst = tytul.lower()
    tekst = re.sub(r"[\(\[].*?[\)\]]", " ", tekst)      # nawiasy z zawartością
    tekst = re.sub(r"\s*-\s*(remaster|deluxe|edition).*$", " ", tekst)
    tekst = re.sub(r"[^\w\s]", " ", tekst)               # znaki przestankowe
    tekst = re.sub(r"\s+", " ", tekst).strip()
    if tekst.startswith("the "):
        tekst = tekst[4:]
    return tekst


def znajdz_album(sp, nazwa_albumu, wykonawca=None):
    """
    Wyszukuje album w Spotify i zwraca jego URI (np. "spotify:album:4LH4d3cOWNNsVw41Gqt2kv").

    sp           — klient zwrócony przez zaloguj()
    nazwa_albumu — tytuł albumu, np. "The Dark Side of the Moon"
    wykonawca    — opcjonalnie wykonawca; przydaje się, bo tytuły albumów
                   powtarzają się częściej niż tytuły piosenek

    Szuka tak jak znajdz_utwor() — najpierw z filtrem album:, potem luźno —
    ale wyniku NIE bierze z brzegu, tylko ocenia kilkanaście kandydatów.

    Powód jest praktyczny: na zapytanie "Dark Side of the Moon" samo API
    stawia na pierwszym miejscu obskurną płytę nieznanego wykonawcy, bo jej
    tytuł pasuje co do znaku, podczas gdy album Pink Floyd nazywa się
    "THE Dark Side of the Moon". Dlatego porównujemy tytuły po uproszczeniu,
    a wśród równie dobrze pasujących wybieramy ten popularniejszy.

    Zwraca: krotkę (uri, opis) albo (None, None) jeśli nic sensownego nie ma.
    """
    zapytanie = f"album:{nazwa_albumu}"
    if wykonawca:
        zapytanie += f" artist:{wykonawca}"

    wyniki = sp.search(q=zapytanie, type="album", limit=LICZBA_KANDYDATOW)
    albumy = wyniki["albums"]["items"]

    # Plan B: jeśli filtry nic nie dały (literówka, przekręcenie przez Whispera),
    # szukamy jeszcze raz "luźno" — samym tekstem.
    if not albumy:
        luzne_zapytanie = f"{nazwa_albumu} {wykonawca}" if wykonawca else nazwa_albumu
        wyniki = sp.search(q=luzne_zapytanie, type="album", limit=LICZBA_KANDYDATOW)
        albumy = wyniki["albums"]["items"]

    if not albumy:
        return None, None

    szukane = _uprosc_tytul(nazwa_albumu)

    # Odsiewamy wszystko, co tylko z grubsza przypomina to, o co prosiłeś.
    pasujace = []
    for album in albumy:
        podobienstwo = difflib.SequenceMatcher(
            None, szukane, _uprosc_tytul(album["name"])
        ).ratio()
        if podobienstwo >= PROG_TYTULU_ALBUMU:
            pasujace.append((podobienstwo, album))

    if not pasujace:
        logger.info("Żaden z %d wyników nie przypomina albumu %r.",
                    len(albumy), nazwa_albumu)
        return None, None

    # Zaokrąglamy podobieństwo do jednego miejsca po przecinku, żeby tytuły
    # pasujące porównywalnie dobrze wpadły do wspólnego "koszyka". Dopiero
    # wewnątrz koszyka decydują kolejne kryteria.
    #
    # Naturalnym drugim kryterium byłaby popularność, ale Spotify zwraca ją
    # tylko przez /v1/albums, a ten endpoint odpowiada 403 aplikacjom w trybie
    # deweloperskim — czyli także naszej. Sprawdziłem to; nie ma sensu wołać
    # czegoś, co zawsze zawodzi. Zamiast tego używamy pól, które PRZYCHODZĄ
    # już w wynikach wyszukiwania:
    #
    #   - album_type: pełny album bije singla i składankę. To samo w sobie
    #     rozwiązuje przypadek "Dark Side of the Moon", gdzie tuż obok albumu
    #     Pink Floyd stoi jednoutworowy singiel nieznanego wykonawcy
    #     o dokładnie takim samym tytule.
    #
    # Gdy i to nie rozstrzyga, zostawiamy KOLEJNOŚĆ Z WYSZUKIWARKI Spotify.
    # sort() w Pythonie jest stabilny, więc elementy o równych kluczach
    # zachowują pierwotną kolejność — a ta niesie ocenę trafności zrobioną
    # przez samo Spotify, które o popularności wie znacznie więcej niż my.
    #
    # Próbowałem dokładać jeszcze total_tracks (dłuższe wydanie wygrywa),
    # ale to wypychało na wierzch rozdmuchane reedycje "Super Deluxe"
    # i cudze płyty coverowe. Mniej kryteriów dało tu wyraźnie lepsze wyniki.
    waga_typu = {"album": 2, "compilation": 1, "single": 0}

    pasujace.sort(
        key=lambda para: (
            round(para[0], 1),
            waga_typu.get(para[1].get("album_type"), 0),
        ),
        reverse=True,
    )

    album = pasujace[0][1]
    # Wykonawców może być kilku (składanki, kolaboracje), więc sklejamy nazwy.
    artysci = ", ".join(a["name"] for a in album["artists"])
    opis = f"{artysci} — {album['name']}"

    return album["uri"], opis


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
        return False, _opisz_blad_odtwarzania(e)


def _opisz_blad_odtwarzania(e):
    """
    Tłumaczy wyjątek Spotify na komunikat zrozumiały dla człowieka.

    Wydzielone osobno, bo odtworz() i odtworz_album() wołają ten sam endpoint
    i wywracają się dokładnie tak samo — nie ma sensu utrzymywać dwóch kopii.
    """
    # 403 to najczęstszy problem: konto darmowe (sterowanie wymaga Premium)
    # albo odtwarzanie zablokowane w danym momencie.
    if e.http_status == 403:
        return "Spotify odmówiło odtwarzania. Sterowanie playbackiem wymaga konta Premium."
    if e.http_status == 404:
        return "Nie znalazłem urządzenia do odtwarzania. Otwórz aplikację Spotify."
    return f"Błąd Spotify: {e.msg}"


def odtworz_album(sp, album_uri, device_id=None):
    """
    Odtwarza cały album po kolei.

    sp        — klient z zaloguj()
    album_uri — URI albumu z znajdz_album()
    device_id — opcjonalnie konkretne urządzenie; gdy None, Spotify użyje aktywnego


    RÓŻNICA MIĘDZY uris= A context_uri=
    ===================================

    start_playback() przyjmuje dwa różne parametry i to NIE są synonimy:

      uris=["spotify:track:...", ...]
          Jawna lista konkretnych utworów. Spotify odtworzy dokładnie te
          i tylko te, po czym odtwarzanie się skończy. Używamy tego w odtworz()
          do puszczenia jednej piosenki.

      context_uri="spotify:album:..."
          Wskazanie CAŁEGO ZBIORU — albumu, playlisty albo wykonawcy.
          Spotify sam pobiera jego zawartość i gra po kolei, od pierwszego
          utworu do ostatniego, z zachowaniem kolejności z albumu.
          Działa też tryb losowy i "następny utwór" w obrębie tego zbioru.

    Można to sobie wyobrazić tak: uris to "zagraj te trzy piosenki, które
    wypisałem", a context_uri to "włącz tę płytę". W drugim przypadku nie
    musimy sami pobierać listy utworów ani ich kolejności — Spotify wie.

    Ważne: te dwa parametry wykluczają się nawzajem. Podanie obu naraz
    kończy się błędem 400 z API.

    Zwraca: (True/False, komunikat) — True gdy się udało.
    """
    try:
        sp.start_playback(device_id=device_id, context_uri=album_uri)
        return True, "Odtwarzam album."
    except spotipy.SpotifyException as e:
        return False, _opisz_blad_odtwarzania(e)


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


def _przygotuj_urzadzenie(sp):
    """
    Znajduje urządzenie gotowe do grania, w razie potrzeby otwierając Spotify.

    Wspólne dla zagraj_piosenke() i zagraj_album() — obie potrzebują dokładnie
    tego samego, a duplikowanie tej logiki znaczyłoby, że przy każdej poprawce
    trzeba pamiętać o dwóch miejscach.

    Zwraca: (device_id, komunikat_błędu). Przy powodzeniu komunikat to None.
    """
    device_id, komunikat_urzadzenia = znajdz_aktywne_urzadzenie(sp)

    # Nie ma na czym grać — próbujemy otworzyć aplikację Spotify i poczekać,
    # aż zgłosi się do API jako urządzenie.
    if device_id is None:
        logger.info(komunikat_urzadzenia)
        device_id = uruchom_i_poczekaj_na_spotify(sp)

    if device_id is None:
        return None, (
            "Nie udało mi się uruchomić Spotify. "
            "Otwórz aplikację ręcznie i spróbuj jeszcze raz."
        )

    return device_id, None


def zagraj_piosenke(tytul, wykonawca=None):
    """
    Wygodne "wszystko na raz" — funkcja, którą Jarvis woła po rozpoznaniu
    komendy głosowej: zaloguj → znajdź → sprawdź urządzenie → graj.

    Zwraca: (komunikat dla użytkownika, czy się udało).
    Flaga sukcesu jest potrzebna GUI — decyduje, czy koło wróci spokojnie
    do idle, czy błyśnie na czerwono.
    """
    sp = zaloguj()

    uri, opis = znajdz_utwor(sp, tytul, wykonawca)
    if uri is None:
        return f"Nie znalazłem utworu: {tytul}.", False

    device_id, blad = _przygotuj_urzadzenie(sp)
    if device_id is None:
        return blad, False

    sukces, komunikat = odtworz(sp, uri, device_id)
    if not sukces:
        return komunikat, False

    return f"Odtwarzam {opis}.", True


def zagraj_album(nazwa_albumu, wykonawca=None):
    """
    To samo co zagraj_piosenke(), tylko dla całego albumu.

    Różnica jest w dwóch krokach: szukamy przez znajdz_album() zamiast
    znajdz_utwor(), a gramy przez odtworz_album(), który używa context_uri
    zamiast uris — dzięki czemu Spotify puszcza całą płytę po kolei,
    a nie jeden utwór. Szczegóły w komentarzu przy odtworz_album().

    Zwraca: (komunikat dla użytkownika, czy się udało).
    """
    sp = zaloguj()

    uri, opis = znajdz_album(sp, nazwa_albumu, wykonawca)
    if uri is None:
        return f"Nie znalazłem albumu: {nazwa_albumu}.", False

    device_id, blad = _przygotuj_urzadzenie(sp)
    if device_id is None:
        return blad, False

    sukces, komunikat = odtworz_album(sp, uri, device_id)
    if not sukces:
        return komunikat, False

    return f"Odtwarzam album {opis}.", True


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
