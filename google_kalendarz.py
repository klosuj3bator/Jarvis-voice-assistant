"""
google_kalendarz.py — Kalendarz Google przez API: odczyt i DODAWANIE wydarzeń.

kalendarz.py woła ten moduł, a gdy API nie działa (brak połączenia, awaria
Google), wraca do starego adresu iCal — tylko do odczytu.


JAK JARVIS DOSTAJE DOSTĘP — OAuth
=================================

  1. W Google Cloud tworzysz "klienta OAuth" typu aplikacja desktopowa
     i zapisujesz jego plik jako google_credentials.json obok Jarvisa
     (instrukcja w Ustawieniach, strona Kalendarz Google).
  2. W Ustawieniach klikasz "Połącz z Google". Jarvis otwiera przeglądarkę
     na stronie logowania Google — logujesz się TAM, Jarvis Twojego hasła
     nigdy nie widzi — i zgadzasz się na dostęp do wydarzeń.
  3. Google przekierowuje przeglądarkę na adres http://127.0.0.1:<port> —
     to mały serwer Jarvisa, czynny tylko na czas logowania i tylko na tym
     komputerze. Dostaje jednorazowy kod i wymienia go na token.
  4. Token ląduje w google_token.json. Ważny ~godzinę, potem Jarvis sam go
     odświeża "tokenem odświeżania" — bez Twojego udziału.

Dwa zabezpieczenia logowania: losowy "state" (odpowiedź musi pochodzić
z TEGO logowania) i PKCE (kod bez tajnego "weryfikatora", który zna tylko
Jarvis, jest bezużyteczny — nawet gdyby ktoś go podejrzał).

Logowanie startuje WYŁĄCZNIE z Ustawień, na Twoje kliknięcie — nigdy przy
uruchamianiu Jarvisa.

Zakres dostępu: calendar.events — odczyt i dodawanie wydarzeń. Usuwania
w kodzie nie ma i nie będzie.


GDY TOKEN WYGAŚNIE
==================

Google potrafi unieważnić token odświeżania (zmiana hasła, odebranie
dostępu, a dla aplikacji w trybie "testowym" — po 7 dniach). Wtedy
odświeżenie kończy się błędem "invalid_grant": rzucamy TokenWygasl, a plik
tokenu dostaje znacznik "wygasl" — kalendarz.py mówi Ci o tym raz (głos
i Telegram) i do czasu ponownego połączenia korzysta z adresu iCal.


SEKRETY
=======

Token i plik klienta to klucze: nigdy nie trafiają do dziennika ani do
modelu, a komunikaty błędów składamy sami (wyjątki requests potrafią
zawierać adres z parametrami).
"""

import base64
import datetime
import hashlib
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

import requests

import konfiguracja

logger = logging.getLogger(__name__)

STREFA = ZoneInfo("Europe/Warsaw")
ZAKRES = "https://www.googleapis.com/auth/calendar.events"
API = "https://www.googleapis.com/calendar/v3"
KALENDARZ = "primary"                 # Twój główny kalendarz
LIMIT_LOGOWANIA_S = 180               # tyle czekamy na zgodę w przeglądarce

_blokada = threading.Lock()


class BladApi(Exception):
    """Błąd API z komunikatem bez tokenów i adresów."""


class TokenWygasl(BladApi):
    """Google unieważnił dostęp — trzeba połączyć się ponownie w Ustawieniach."""


# ---------------------------------------------------------------
# Pliki: klient OAuth i token
# ---------------------------------------------------------------

def _klient():
    """Dane klienta OAuth z google_credentials.json (typ "installed" albo "web")."""
    try:
        with open(konfiguracja.PLIK_GOOGLE_KLIENTA, encoding="utf-8") as plik:
            dane = json.load(plik)
        klient = dane.get("installed") or dane.get("web")
        return {k: klient[k] for k in ("client_id", "client_secret", "auth_uri", "token_uri")}
    except FileNotFoundError:
        raise BladApi("Brak pliku google_credentials.json — instrukcja jest w Ustawieniach "
                      "(Kalendarz Google).") from None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise BladApi("Plik google_credentials.json jest niepełny albo uszkodzony — pobierz "
                      "go jeszcze raz z Google Cloud.") from None


def _wczytaj_token():
    try:
        with open(konfiguracja.PLIK_GOOGLE_TOKENU, encoding="utf-8") as plik:
            return json.load(plik)
    except (OSError, ValueError):
        return None


def _zapisz_token(dane):
    tymczasowy = konfiguracja.PLIK_GOOGLE_TOKENU + ".tmp"
    with open(tymczasowy, "w", encoding="utf-8") as plik:
        json.dump(dane, plik)
    os.replace(tymczasowy, konfiguracja.PLIK_GOOGLE_TOKENU)


def ma_klienta():
    return os.path.exists(konfiguracja.PLIK_GOOGLE_KLIENTA)


def polaczony():
    """Czy jest ważny (nieunieważniony) token odświeżania."""
    token = _wczytaj_token()
    return bool(token and token.get("refresh_token") and not token.get("wygasl"))


def wygasl():
    token = _wczytaj_token()
    return bool(token and token.get("wygasl"))


def stan():
    """Krótki opis dla Ustawień."""
    if polaczony():
        return "połączony"
    if wygasl():
        return "dostęp wygasł — połącz ponownie"
    if not ma_klienta():
        return "brak pliku google_credentials.json"
    return "niepołączony"


# ---------------------------------------------------------------
# Logowanie (z Ustawień, na kliknięcie)
# ---------------------------------------------------------------

def autoryzuj(otworz=None, limit_s=LIMIT_LOGOWANIA_S):
    """
    Logowanie OAuth z przeglądarką (opis na górze modułu). Woła je
    WYŁĄCZNIE okno Ustawień — nigdy start Jarvisa.

    otworz — funkcja otwierająca adres w przeglądarce (testy podają własną)

    Zwraca: (ok, komunikat).
    """
    import webbrowser

    otworz = otworz or webbrowser.open
    try:
        klient = _klient()
    except BladApi as e:
        return False, str(e)

    weryfikator = secrets.token_urlsafe(64)
    wyzwanie = base64.urlsafe_b64encode(hashlib.sha256(weryfikator.encode()).digest()).rstrip(b"=").decode()
    stan_logowania = secrets.token_urlsafe(24)
    wynik = {}

    class Obsluga(BaseHTTPRequestHandler):
        def do_GET(self):
            parametry = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if parametry.get("state", [None])[0] != stan_logowania:
                self._odpowiedz(400, "To nie jest odpowiedź na logowanie Jarvisa.")
                return
            wynik["kod"] = parametry.get("code", [None])[0]
            wynik["blad"] = parametry.get("error", [None])[0]
            self._odpowiedz(200, "Gotowe — Jarvis ma dostęp do Kalendarza Google. Możesz zamknąć tę kartę."
                            if wynik["kod"] else "Logowanie przerwane — nic nie zostało połączone.")

        def _odpowiedz(self, kod, tekst):
            tresc = f"<html><meta charset='utf-8'><body style='font-family:sans-serif'><h3>{tekst}</h3></body></html>"
            self.send_response(kod)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(tresc.encode("utf-8"))

        def log_message(self, *argumenty):
            pass                        # adres z kodem nie może trafić do dziennika

    serwer = HTTPServer(("127.0.0.1", 0), Obsluga)
    serwer.timeout = 1
    przekierowanie = f"http://127.0.0.1:{serwer.server_address[1]}"
    adres = klient["auth_uri"] + "?" + urllib.parse.urlencode({
        "client_id": klient["client_id"], "redirect_uri": przekierowanie, "response_type": "code",
        "scope": ZAKRES, "state": stan_logowania, "code_challenge": wyzwanie,
        "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent",
    })
    try:
        otworz(adres)
        koniec = time.monotonic() + limit_s
        while "kod" not in wynik and time.monotonic() < koniec:
            serwer.handle_request()
    finally:
        serwer.server_close()

    if not wynik.get("kod"):
        if wynik.get("blad") == "access_denied":
            return False, "Nie zgodziłeś się na dostęp w przeglądarce — nic nie połączyłem."
        return False, "Nie doczekałem się logowania w przeglądarce — spróbuj jeszcze raz."
    try:
        odpowiedz = requests.post(klient["token_uri"], timeout=20, data={
            "code": wynik["kod"], "client_id": klient["client_id"],
            "client_secret": klient["client_secret"], "redirect_uri": przekierowanie,
            "grant_type": "authorization_code", "code_verifier": weryfikator,
        })
        dane = odpowiedz.json()
    except (requests.RequestException, ValueError):
        return False, "Nie udało się połączyć z Google — sprawdź internet."
    if odpowiedz.status_code != 200 or not dane.get("refresh_token"):
        return False, f"Google odrzucił logowanie ({dane.get('error', odpowiedz.status_code)})."
    with _blokada:
        _zapisz_token({"refresh_token": dane["refresh_token"], "access_token": dane.get("access_token"),
                       "wygasa": time.time() + int(dane.get("expires_in", 0)) - 60})
    logger.info("[KALENDARZ] Połączono z Google Calendar API.")
    return True, "Połączono z Kalendarzem Google."


# ---------------------------------------------------------------
# Token dostępu i zapytania
# ---------------------------------------------------------------

def _token_dostepu(wymus_odswiezenie=False):
    """Ważny token dostępu; odświeża go, gdy wygasł. TokenWygasl, gdy Google odebrał dostęp."""
    with _blokada:
        token = _wczytaj_token()
        if not token or not token.get("refresh_token") or token.get("wygasl"):
            raise TokenWygasl("Kalendarz Google nie jest połączony.")
        if not wymus_odswiezenie and token.get("access_token") and time.time() < token.get("wygasa", 0):
            return token["access_token"]
        klient = _klient()
        try:
            odpowiedz = requests.post(klient["token_uri"], timeout=20, data={
                "client_id": klient["client_id"], "client_secret": klient["client_secret"],
                "refresh_token": token["refresh_token"], "grant_type": "refresh_token",
            })
            dane = odpowiedz.json()
        except (requests.RequestException, ValueError):
            raise BladApi("Nie udało się połączyć z Google.") from None
        if odpowiedz.status_code in (400, 401) and dane.get("error") in ("invalid_grant", "unauthorized_client"):
            token["wygasl"] = True
            _zapisz_token(token)
            logger.warning("[KALENDARZ] Google unieważnił dostęp (%s).", dane.get("error"))
            raise TokenWygasl("Dostęp do Kalendarza Google wygasł.")
        if odpowiedz.status_code != 200 or "access_token" not in dane:
            raise BladApi(f"Google nie odświeżył dostępu (kod {odpowiedz.status_code}).")
        token.update(access_token=dane["access_token"],
                     wygasa=time.time() + int(dane.get("expires_in", 3600)) - 60)
        _zapisz_token(token)
        return token["access_token"]


def _zapytanie(metoda, sciezka, **opcje):
    """Zapytanie do Calendar API. Przy 401 odświeża token i próbuje raz jeszcze."""
    for proba in range(2):
        naglowki = {"Authorization": f"Bearer {_token_dostepu(wymus_odswiezenie=proba == 1)}"}
        try:
            odpowiedz = requests.request(metoda, API + sciezka, headers=naglowki, timeout=20, **opcje)
        except requests.Timeout:
            raise BladApi("Google nie odpowiedział w 20 sekund.") from None
        except requests.RequestException as e:
            raise BladApi(f"Nie udało się połączyć z Google ({type(e).__name__}).") from None
        if odpowiedz.status_code == 401 and proba == 0:
            continue
        if odpowiedz.status_code >= 400:
            raise BladApi(f"Google Calendar odpowiedział kodem {odpowiedz.status_code}.")
        return odpowiedz.json()
    raise BladApi("Google nie przyjął tokenu.")


# ---------------------------------------------------------------
# Wydarzenia
# ---------------------------------------------------------------

def _czas(wpis):
    """{"dateTime": ...} -> datetime w Europe/Warsaw; {"date": ...} -> date (cały dzień)."""
    if "dateTime" in wpis:
        return datetime.datetime.fromisoformat(wpis["dateTime"]).astimezone(STREFA)
    return datetime.date.fromisoformat(wpis["date"])


def wydarzenia(od, do):
    """
    Wydarzenia między od a do (datetime ze strefą), powtarzające się rozwinięte.

    Zwraca listę słowników jak kalendarz.wydarzenia_dnia (bez przycinania tytułów
    — robi to kalendarz.py): {id, od, do, caly_dzien, tytul, miejsce, zaproszenie}.
    Odwołanych i odrzuconych przez Ciebie nie ma.
    """
    elementy, strona = [], None
    while True:
        parametry = {"timeMin": od.isoformat(), "timeMax": do.isoformat(), "singleEvents": "true",
                     "orderBy": "startTime", "timeZone": "Europe/Warsaw", "maxResults": 250}
        if strona:
            parametry["pageToken"] = strona
        dane = _zapytanie("GET", f"/calendars/{KALENDARZ}/events", params=parametry)
        elementy += dane.get("items", [])
        strona = dane.get("nextPageToken")
        if not strona or len(elementy) > 1000:
            break

    wynik = []
    for e in elementy:
        if e.get("status") == "cancelled":
            continue
        moja = next((g.get("responseStatus") for g in e.get("attendees", []) if g.get("self")), None)
        if moja == "declined":
            continue
        try:
            poczatek, koniec = _czas(e["start"]), _czas(e["end"])
        except (KeyError, ValueError):
            logger.warning("[KALENDARZ] Pomijam wydarzenie z niepoprawną datą.")
            continue
        wynik.append({"id": e.get("id"), "od": poczatek, "do": koniec,
                      "caly_dzien": not isinstance(poczatek, datetime.datetime),
                      "tytul": e.get("summary") or "(bez tytułu)", "miejsce": e.get("location") or "",
                      "zaproszenie": moja == "needsAction"})
    return wynik


def dodaj(tytul, od, do, miejsce=""):
    """
    Nowe wydarzenie w Twoim głównym kalendarzu. od/do: datetime ze strefą albo
    date (cały dzień; do = dzień PO ostatnim, jak w Google). Zwraca id.
    """
    if isinstance(od, datetime.datetime):
        poczatek = {"dateTime": od.isoformat(), "timeZone": "Europe/Warsaw"}
        koniec = {"dateTime": do.isoformat(), "timeZone": "Europe/Warsaw"}
    else:
        poczatek, koniec = {"date": od.isoformat()}, {"date": do.isoformat()}
    tresc = {"summary": tytul, "start": poczatek, "end": koniec}
    if miejsce:
        tresc["location"] = miejsce
    dane = _zapytanie("POST", f"/calendars/{KALENDARZ}/events", json=tresc)
    logger.info("[KALENDARZ] Dodane wydarzenie (id %s).", dane.get("id"))
    return dane.get("id")
