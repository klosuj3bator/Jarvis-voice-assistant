"""
panel.py — dane panelu bocznego: rozmowa na żywo, dzień (kalendarz,
przypomnienia, pogoda), ważne maile, Spotify i dzisiejsze notatki.

Panel ma dwa wcielenia (okno robi pasek_boczny.py, wygląd — Panel.qml):
  - "Tryb 2 ekranów": wąski pasek przy krawędzi drugiego monitora,
  - przy jednym monitorze: panel wysuwany z boku HUD-a / tapety.
Oba pokazują TE SAME dane — zbiera je ten moduł, w jednym wątku w tle.


SKĄD DANE I JAK CZĘSTO
======================

  rozmowa    — na żywo: main.py podaje Twoje zdania, tts.py zdania Jarvisa
  dzien      — przypomnienia co 20 s (plik na dysku), kalendarz co 10 min,
               pogoda co 30 min (Open-Meteo — darmowe, bez klucza i konta;
               wysyłamy tam tylko nazwę miasta z Ustawień)
  maile      — co 5 min, nagłówki nieprzeczytanych ważnych (email_monitor.wazne)
  spotify    — co 3 s, tylko z zapisanego logowania (nigdy nie otwiera przeglądarki)
  notatki    — co 30 s, dzisiejszy plik notatek

Wyłączony element nie jest w ogóle pobierany — wyłączone maile to zero
logowań do Gmaila. Wyłączony cały panel to wątek, który tylko śpi.


BEZPIECZEŃSTWO
==============

Nic stąd nie trafia do Claude — to czyste odczyty, jak czujki poczty.
Tematy maili i tytuły wydarzeń to cudze dane: Panel.qml pokazuje je jako
zwykły tekst (Text.PlainText), więc temat w rodzaju <img src=...> nie
wczyta niczego z internetu. Okładki Spotify pobieramy tu, w Pythonie,
i podajemy scenie jako gotowe obrazki — scena sama do sieci nie sięga.
"""

import collections
import copy
import datetime
import hashlib
import logging
import threading
import time

import konfiguracja

# requests importujemy dopiero w funkcjach, które idą do sieci — main.py
# importuje ten moduł na samym starcie, a każda dziesiąta sekundy się liczy.

logger = logging.getLogger(__name__)

# Co ile sekund odświeżamy każdą część (opis na górze).
CO_ILE_S = {"przypomnienia": 20, "kalendarz": 600, "pogoda": 1800,
            "maile": 300, "spotify": 3, "notatki": 30}

# Rozmowa: ile ostatnich wypowiedzi trzymamy i jak długie mogą być.
MAKS_WYPOWIEDZI = 8
MAKS_ZNAKOW_WYPOWIEDZI = 400

# Kody pogody WMO (Open-Meteo) -> (znaczek, opis po polsku).
POGODA = {0: ("☀", "bezchmurnie"), 1: ("🌤", "prawie bezchmurnie"), 2: ("⛅", "trochę chmur"),
          3: ("☁", "pochmurno"), 45: ("🌫", "mgła"), 48: ("🌫", "szadź"),
          51: ("🌦", "mżawka"), 53: ("🌦", "mżawka"), 55: ("🌦", "gęsta mżawka"),
          61: ("🌧", "lekki deszcz"), 63: ("🌧", "deszcz"), 65: ("🌧", "ulewa"),
          66: ("🌧", "marznący deszcz"), 67: ("🌧", "marznący deszcz"),
          71: ("❄", "lekki śnieg"), 73: ("❄", "śnieg"), 75: ("❄", "śnieżyca"), 77: ("❄", "śnieg"),
          80: ("🌦", "przelotny deszcz"), 81: ("🌧", "przelotne ulewy"), 82: ("🌧", "ulewy"),
          85: ("❄", "przelotny śnieg"), 86: ("❄", "przelotny śnieg"),
          95: ("⛈", "burza"), 96: ("⛈", "burza z gradem"), 99: ("⛈", "burza z gradem")}

_blokada = threading.Lock()
_dane = {"rozmowa": [], "dzien": {}, "maile": {}, "spotify": {}, "notatki": []}
_rozmowa = collections.deque(maxlen=MAKS_WYPOWIEDZI)
_sluchacze = []
_zatrzymaj = threading.Event()
_watek = None

# Okładki Spotify: klucz -> bajty obrazka (dla gui.DostawcaObrazow).
_okladki = collections.OrderedDict()
# Miasto -> (szerokość, długość, nazwa) — współrzędnych nie pytamy co pół godziny.
_miejsca = {}


# ---------------------------------------------------------------
# Odbiorcy i stan
# ---------------------------------------------------------------

def sluchaj(funkcja):
    """Rejestruje funkcję(dane), wołaną po każdej zmianie (z wątku w tle)."""
    with _blokada:
        _sluchacze.append(funkcja)


def stan():
    """Kopia wszystkich danych panelu — dla okna, które dopiero powstaje."""
    with _blokada:
        return copy.deepcopy(_dane)


def _ustaw(czesc, wartosc):
    """Zapisuje część danych i powiadamia odbiorców — tylko gdy coś się zmieniło."""
    with _blokada:
        if _dane.get(czesc) == wartosc:
            return
        _dane[czesc] = wartosc
        migawka = copy.deepcopy(_dane)
        sluchacze = list(_sluchacze)
    for sluchacz in sluchacze:
        try:
            sluchacz(migawka)
        except Exception:
            logger.exception("[PANEL] Odbiorca danych zawiódł")


def okladka(klucz):
    """Bajty okładki o danym kluczu albo None."""
    with _blokada:
        return _okladki.get(klucz)


def _wlaczony(element):
    return konfiguracja.panel_wlaczony() and element in konfiguracja.panel_elementy()


# ---------------------------------------------------------------
# Rozmowa na żywo — wołają ją main.py i tts.py
# ---------------------------------------------------------------

def _czas():
    return datetime.datetime.now().strftime("%H:%M")


def _publikuj_rozmowe():
    _ustaw("rozmowa", [dict(w) for w in _rozmowa])


def ty(tekst):
    """Twoja wypowiedź (już po ukryciu haseł — robi to main.py)."""
    if not _wlaczony("rozmowa") or not (tekst or "").strip():
        return
    with _blokada:
        _rozmowa.append({"kto": "ty", "czas": _czas(), "przerwane": False,
                         "tekst": tekst.strip()[:MAKS_ZNAKOW_WYPOWIEDZI]})
    _publikuj_rozmowe()


def jarvis(zdanie):
    """
    Zdanie Jarvisa w chwili, gdy zaczyna brzmieć. Kolejne zdania jednej
    odpowiedzi doklejamy do tego samego wpisu — to jedna wypowiedź.
    """
    if not _wlaczony("rozmowa") or not (zdanie or "").strip():
        return
    with _blokada:
        ostatni = _rozmowa[-1] if _rozmowa else None
        if ostatni and ostatni["kto"] == "jarvis" and not ostatni["przerwane"]:
            ostatni["tekst"] = (ostatni["tekst"] + " " + zdanie.strip())[-MAKS_ZNAKOW_WYPOWIEDZI:]
        else:
            _rozmowa.append({"kto": "jarvis", "czas": _czas(), "przerwane": False,
                             "tekst": zdanie.strip()[:MAKS_ZNAKOW_WYPOWIEDZI]})
    _publikuj_rozmowe()


def przerwano():
    """Wszedłeś Jarvisowi w słowo — ostatnia jego wypowiedź dostaje znaczek."""
    if not _wlaczony("rozmowa"):
        return
    with _blokada:
        for wpis in reversed(_rozmowa):
            if wpis["kto"] == "jarvis":
                wpis["przerwane"] = True
                break
    _publikuj_rozmowe()


# ---------------------------------------------------------------
# Dzień: kalendarz, przypomnienia, pogoda
# ---------------------------------------------------------------

_dzien = {"wydarzenia": [], "przypomnienia": [], "pogoda": None, "uwagi": {}}


def _kalendarz():
    import kalendarz

    if not kalendarz.skonfigurowany():
        return [], "Kalendarz nie jest skonfigurowany (Ustawienia)."
    dzis = datetime.datetime.now(kalendarz.STREFA).date()
    try:
        wydarzenia = kalendarz.wydarzenia_dnia(dzis)
    except kalendarz.BladKalendarza as e:
        return None, str(e)
    wynik = []
    for w in wydarzenia:
        godzina = "cały dzień" if w["caly_dzien"] else (
            w["od"].strftime("%H:%M") if w["od"].date() == dzis else "wcześniej")
        wynik.append({"godzina": godzina, "tytul": w["tytul"], "zaproszenie": w["zaproszenie"]})
    return wynik, ""


def _przypomnienia():
    import briefing

    return [{"godzina": kiedy.strftime("%H:%M"), "tresc": tresc}
            for kiedy, tresc in briefing._przypomnienia_na_dzis(datetime.datetime.now())]


def _pogoda():
    """Pogoda z Open-Meteo dla miasta z Ustawień (opis na górze)."""
    import requests

    miasto = konfiguracja.pogoda_miasto()
    if not miasto:
        return None, "Wpisz miasto w Ustawieniach, żeby widzieć pogodę."
    if miasto not in _miejsca:
        wynik = requests.get("https://geocoding-api.open-meteo.com/v1/search",
                             params={"name": miasto, "count": 1, "language": "pl", "format": "json"},
                             timeout=10).json().get("results")
        if not wynik:
            return None, f"Nie znam miasta „{miasto}” — popraw je w Ustawieniach."
        _miejsca[miasto] = (wynik[0]["latitude"], wynik[0]["longitude"], wynik[0]["name"])
    szer, dlug, nazwa = _miejsca[miasto]
    prognoza = requests.get("https://api.open-meteo.com/v1/forecast", params={
        "latitude": szer, "longitude": dlug, "current": "temperature_2m,weather_code",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": "Europe/Warsaw", "forecast_days": 1}, timeout=10).json()
    teraz, dzis = prognoza["current"], prognoza["daily"]
    znaczek, opis = POGODA.get(teraz["weather_code"], ("·", ""))
    return {"miasto": nazwa, "znaczek": znaczek, "opis": opis,
            "teraz": f"{round(teraz['temperature_2m'])}°",
            "zakres": f"{round(dzis['temperature_2m_min'][0])}° / {round(dzis['temperature_2m_max'][0])}°",
            "opady": f"{dzis['precipitation_probability_max'][0] or 0}%"}, ""


def _publikuj_dzien():
    import briefing

    teraz = datetime.datetime.now()
    _ustaw("dzien", {
        "data": f"{briefing.DNI_TYGODNIA[teraz.weekday()]}, {teraz.day} "
                f"{briefing.MIESIACE[teraz.month - 1]}",
        "wydarzenia": _dzien["wydarzenia"], "przypomnienia": _dzien["przypomnienia"],
        "pogoda": _dzien["pogoda"], "uwaga": " ".join(u for u in _dzien["uwagi"].values() if u),
    })


# ---------------------------------------------------------------
# Maile, Spotify, notatki
# ---------------------------------------------------------------

def _maile():
    import email_monitor

    if not email_monitor.skonfigurowany():
        return {"lista": [], "uwaga": "Gmail nie jest skonfigurowany (Ustawienia)."}
    lista = email_monitor.wazne()
    kilka = len(email_monitor.konta()) > 1
    for m in lista:
        m["kiedy"] = datetime.datetime.fromtimestamp(m["kiedy"]).strftime("%H:%M") if m["kiedy"] else ""
        if not kilka:
            m["konto"] = ""              # przy jednym koncie nazwa nic nie mówi
    return {"lista": lista, "uwaga": "" if lista else "Brak nieprzeczytanych ważnych."}


def _klient_spotify():
    """Klient z ZAPISANEGO logowania — w tle nigdy nie otwieramy przeglądarki."""
    import spotipy
    from spotipy.oauth2 import SpotifyOAuth

    import spotify_controller

    logowanie = SpotifyOAuth(scope=spotify_controller.SCOPE, cache_path=spotify_controller.CACHE_PATH,
                             open_browser=False)
    token = logowanie.validate_token(logowanie.cache_handler.get_cached_token())
    return spotipy.Spotify(auth=token["access_token"], requests_timeout=5) if token else None


def _okladka(adres):
    """Pobiera okładkę (raz na adres) i zwraca jej klucz dla sceny."""
    import requests

    klucz = hashlib.sha1(adres.encode()).hexdigest()[:16]
    with _blokada:
        if klucz in _okladki:
            return klucz
    bajty = requests.get(adres, timeout=5).content
    with _blokada:
        _okladki[klucz] = bajty
        while len(_okladki) > 20:
            _okladki.popitem(last=False)
    return klucz


def _spotify():
    if not konfiguracja.skonfigurowana("spotify"):
        return {"uwaga": "Spotify nie jest skonfigurowany (Ustawienia)."}
    sp = _klient_spotify()
    if sp is None:
        return {"uwaga": "Poproś Jarvisa o muzykę — po pierwszym logowaniu tu się pojawi."}
    stan_gry = sp.current_playback()
    utwor = (stan_gry or {}).get("item")
    if not utwor:
        return {"uwaga": "Nic teraz nie gra."}
    obrazy = (utwor.get("album") or {}).get("images") or []
    # Spotify daje okładki 640, 300 i 64 px — 300 wystarczy z zapasem.
    adres = next((o["url"] for o in obrazy if o.get("width") == 300), obrazy[0]["url"] if obrazy else "")
    return {
        "tytul": utwor.get("name") or "",
        "wykonawca": ", ".join(a["name"] for a in utwor.get("artists") or []),
        "okladka": f"image://jarvis/okladka/{_okladka(adres)}" if adres else "",
        "gra": bool(stan_gry.get("is_playing")),
        "postep_ms": stan_gry.get("progress_ms") or 0,
        "dlugosc_ms": utwor.get("duration_ms") or 0,
        # Scena sama przesuwa pasek postępu między odczytami — od tej chwili.
        "odczyt_ms": int(time.time() * 1000),
    }


def _notatki():
    import notes

    return [{"godzina": g, "tresc": t} for g, t in notes.wczytaj()]


# ---------------------------------------------------------------
# Wątek w tle
# ---------------------------------------------------------------

# Część -> kiedy ostatnio odświeżona (time.monotonic). Wyczyszczenie = wszystko od nowa.
_ostatnio = collections.defaultdict(float)


def _petla():
    import requests

    ostatnio = _ostatnio
    while not _zatrzymaj.is_set():
        teraz = time.monotonic()

        def pora(czesc):
            if teraz - ostatnio[czesc] < CO_ILE_S[czesc]:
                return False
            ostatnio[czesc] = teraz
            return True

        try:
            # Najpierw to, co szybkie (dysk, Spotify), potem to, co może chwilę
            # potrwać (kalendarz, pogoda, logowanie do Gmaila) — inaczej na
            # starcie Spotify i notatki czekałyby kilka sekund na pocztę.
            if _wlaczony("spotify") and pora("spotify"):
                try:
                    _ustaw("spotify", _spotify())
                except Exception as e:
                    logger.debug("[PANEL] Spotify: %s", e)
                    _ustaw("spotify", {"uwaga": "Spotify chwilowo nie odpowiada."})
                    ostatnio["spotify"] = teraz + 27      # przy kłopotach — rzadziej
            if _wlaczony("notatki") and pora("notatki"):
                _ustaw("notatki", _notatki())
            if _wlaczony("dzien"):
                zmiana = False
                if pora("przypomnienia"):
                    _dzien["przypomnienia"] = _przypomnienia()
                    zmiana = True
                if pora("kalendarz"):
                    wydarzenia, uwaga = _kalendarz()
                    if wydarzenia is not None:
                        _dzien["wydarzenia"] = wydarzenia
                    _dzien["uwagi"]["kalendarz"] = uwaga
                    zmiana = True
                if pora("pogoda"):
                    try:
                        _dzien["pogoda"], _dzien["uwagi"]["pogoda"] = _pogoda()
                    except (requests.RequestException, ValueError, KeyError):
                        _dzien["uwagi"]["pogoda"] = "Pogoda chwilowo niedostępna."
                        ostatnio["pogoda"] = teraz - CO_ILE_S["pogoda"] + 120   # spróbuj za 2 min
                    zmiana = True
                if zmiana:
                    _publikuj_dzien()
            if _wlaczony("maile") and pora("maile"):
                try:
                    _ustaw("maile", _maile())
                except Exception as e:
                    logger.warning("[PANEL] Poczta: %s", e)
                    _ustaw("maile", {"lista": [], "uwaga": "Gmail chwilowo nie odpowiada."})
        except Exception:
            logger.exception("[PANEL] Błąd odświeżania")
        _zatrzymaj.wait(1.0)


def odswiez_teraz():
    """Po zmianie Ustawień (np. nowe miasto) — wszystko od nowa, bez czekania."""
    _miejsca.clear()
    _ostatnio.clear()


def uruchom():
    """Startuje wątek danych (raz). Wyłączony panel kosztuje tylko sen wątku."""
    global _watek
    if _watek is not None and _watek.is_alive():
        return
    _zatrzymaj.clear()
    _watek = threading.Thread(target=_petla, name="panel-dane", daemon=True)
    _watek.start()


def zatrzymaj():
    _zatrzymaj.set()
