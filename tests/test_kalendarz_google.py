"""
Kalendarz Google przez API (google_kalendarz.py): logowanie OAuth, odświeżanie
tokenu, odczyt i dodawanie wydarzeń. Google zastępują atrapy — logowanie idzie
naprawdę przez lokalny serwer na 127.0.0.1, ale "przeglądarką" i "Google" jest test.
"""

import base64
import datetime
import hashlib
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace as NS

import pytest

import google_kalendarz as gk
import konfiguracja

KLIENT = {"installed": {"client_id": "id-klienta", "client_secret": "sekret-klienta",
                        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "redirect_uris": ["http://localhost"]}}


def odpowiedz(kod, dane):
    return NS(status_code=kod, json=lambda: dane)


@pytest.fixture
def klient():
    with open(konfiguracja.PLIK_GOOGLE_KLIENTA, "w", encoding="utf-8") as plik:
        json.dump(KLIENT, plik)


def token(**pola):
    dane = {"refresh_token": "odswiezajacy", "access_token": "dostep", "wygasa": 9e12} | pola
    with open(konfiguracja.PLIK_GOOGLE_TOKENU, "w", encoding="utf-8") as plik:
        json.dump(dane, plik)


def zapisany_token():
    with open(konfiguracja.PLIK_GOOGLE_TOKENU, encoding="utf-8") as plik:
        return json.load(plik)


# --- logowanie OAuth ----------------------------------------------------

def wejdz(adres):
    """GET jak z przeglądarki; odpowiedź 400 (obca odpowiedź) to też odpowiedź."""
    try:
        urllib.request.urlopen(adres, timeout=5).read()
    except urllib.error.HTTPError:
        pass


def przegladarka(parametry_odpowiedzi=None):
    """Atrapa przeglądarki: zapamiętuje adres logowania i "wraca" z Google z kodem."""
    adresy = []

    def otworz(adres):
        adresy.append(adres)
        zapytanie = urllib.parse.parse_qs(urllib.parse.urlparse(adres).query)
        powrot = {"code": "kod-z-google", "state": zapytanie["state"][0]} | (parametry_odpowiedzi or {})
        cel = zapytanie["redirect_uri"][0] + "/?" + urllib.parse.urlencode(powrot)
        # Serwer Jarvisa obsłuży to dopiero po powrocie z otworz() — stąd wątek.
        threading.Thread(target=wejdz, args=(cel,), daemon=True).start()

    return otworz, adresy


def test_logowanie_z_pkce_zapisuje_token(klient, monkeypatch):
    wyslane = {}

    def wymiana(adres, timeout, data):
        wyslane.update(data, adres=adres)
        return odpowiedz(200, {"access_token": "dostep", "refresh_token": "odswiezajacy", "expires_in": 3599})

    monkeypatch.setattr(gk.requests, "post", wymiana)
    otworz, adresy = przegladarka()
    assert gk.autoryzuj(otworz=otworz, limit_s=10) == (True, "Połączono z Kalendarzem Google.")

    parametry = urllib.parse.parse_qs(urllib.parse.urlparse(adresy[0]).query)
    assert parametry["scope"] == [gk.ZAKRES] and parametry["access_type"] == ["offline"]
    assert parametry["redirect_uri"][0].startswith("http://127.0.0.1:")
    # PKCE: Google dostaje skrót, a weryfikator dopiero przy wymianie kodu.
    skrot = base64.urlsafe_b64encode(hashlib.sha256(wyslane["code_verifier"].encode()).digest()).rstrip(b"=")
    assert parametry["code_challenge"] == [skrot.decode()] and parametry["code_challenge_method"] == ["S256"]
    assert wyslane["code"] == "kod-z-google" and wyslane["grant_type"] == "authorization_code"
    assert zapisany_token()["refresh_token"] == "odswiezajacy"
    assert gk.polaczony() and gk.stan() == "połączony"


def test_obca_odpowiedz_z_innym_state_jest_ignorowana(klient, monkeypatch):
    monkeypatch.setattr(gk.requests, "post", lambda *a, **k: pytest.fail("bez kodu nie ma wymiany"))
    otworz, _ = przegladarka({"state": "podrobiony"})
    ok, komunikat = gk.autoryzuj(otworz=otworz, limit_s=2)
    assert not ok and komunikat.startswith("Nie doczekałem się")
    assert not gk.polaczony()


def test_odmowa_w_przegladarce(klient, monkeypatch):
    otworz, _ = przegladarka({"code": "", "error": "access_denied"})
    assert gk.autoryzuj(otworz=otworz, limit_s=5) == (
        False, "Nie zgodziłeś się na dostęp w przeglądarce — nic nie połączyłem.")


def test_bez_pliku_klienta_jest_jasny_komunikat():
    ok, komunikat = gk.autoryzuj(otworz=lambda adres: pytest.fail("nie otwieramy przeglądarki"))
    assert not ok and "google_credentials.json" in komunikat
    assert gk.stan() == "brak pliku google_credentials.json"


# --- token: odświeżanie i wygaśnięcie -----------------------------------

def test_wygasly_token_dostepu_jest_odswiezany(klient, monkeypatch):
    token(access_token="stary", wygasa=0)
    monkeypatch.setattr(gk.requests, "post", lambda adres, timeout, data: odpowiedz(
        200, {"access_token": "nowy", "expires_in": 3599}) if data["grant_type"] == "refresh_token" else None)
    assert gk._token_dostepu() == "nowy"
    assert zapisany_token()["access_token"] == "nowy"


def test_uniewazniony_dostep_to_token_wygasl(klient, monkeypatch):
    token(wygasa=0)
    monkeypatch.setattr(gk.requests, "post", lambda *a, **k: odpowiedz(400, {"error": "invalid_grant"}))
    with pytest.raises(gk.TokenWygasl):
        gk._token_dostepu()
    assert zapisany_token()["wygasl"] is True
    assert not gk.polaczony() and gk.wygasl()
    assert not konfiguracja.skonfigurowana("kalendarz")        # HUD pokaże brakujący klucz


def test_401_odswieza_token_i_ponawia(klient, monkeypatch):
    token()
    monkeypatch.setattr(gk.requests, "post", lambda *a, **k: odpowiedz(200, {"access_token": "nowy"}))
    odpowiedzi = iter([odpowiedz(401, {}), odpowiedz(200, {"items": []})])
    naglowki = []
    monkeypatch.setattr(gk.requests, "request", lambda metoda, adres, headers, timeout, **k: (
        naglowki.append(headers["Authorization"]), next(odpowiedzi))[1])
    teraz = datetime.datetime.now(gk.STREFA)
    assert gk.wydarzenia(teraz, teraz + datetime.timedelta(days=1)) == []
    assert naglowki == ["Bearer dostep", "Bearer nowy"]


# --- wydarzenia ---------------------------------------------------------

def test_odczyt_wydarzen_w_strefie_warszawskiej(klient, monkeypatch):
    token()
    strony = iter([
        {"items": [
            {"id": "a", "summary": "Rozmowa", "location": "Biuro",
             "start": {"dateTime": "2026-10-16T08:00:00Z"}, "end": {"dateTime": "2026-10-16T09:00:00Z"}},
            {"id": "b", "summary": "Urlop", "start": {"date": "2026-10-16"}, "end": {"date": "2026-10-17"}},
            {"id": "c", "status": "cancelled", "start": {"date": "2026-10-16"}, "end": {"date": "2026-10-17"}},
        ], "nextPageToken": "dalej"},
        {"items": [
            {"id": "d", "summary": "Odrzucone", "attendees": [{"self": True, "responseStatus": "declined"}],
             "start": {"dateTime": "2026-10-16T12:00:00+02:00"}, "end": {"dateTime": "2026-10-16T13:00:00+02:00"}},
            {"id": "e", "summary": "Zaproszenie", "attendees": [{"self": True, "responseStatus": "needsAction"}],
             "start": {"dateTime": "2026-10-16T15:00:00+02:00"}, "end": {"dateTime": "2026-10-16T16:00:00+02:00"}},
        ]},
    ])
    zapytania = []
    monkeypatch.setattr(gk.requests, "request", lambda metoda, adres, headers, timeout, **k: (
        zapytania.append((metoda, k["params"])), odpowiedz(200, next(strony)))[1])
    od = datetime.datetime(2026, 10, 16, tzinfo=gk.STREFA)
    wynik = gk.wydarzenia(od, od + datetime.timedelta(days=1))
    assert [w["id"] for w in wynik] == ["a", "b", "e"]
    assert wynik[0]["od"] == datetime.datetime(2026, 10, 16, 10, 0, tzinfo=gk.STREFA)    # 8:00 UTC = 10:00 w Polsce
    assert wynik[1]["caly_dzien"] and wynik[1]["od"] == datetime.date(2026, 10, 16)
    assert wynik[2]["zaproszenie"]
    assert zapytania[0][1]["singleEvents"] == "true" and zapytania[0][1]["timeZone"] == "Europe/Warsaw"
    assert zapytania[1][1]["pageToken"] == "dalej"


def test_dodanie_wydarzenia_w_strefie_warszawskiej(klient, monkeypatch):
    token()
    wyslane = []
    monkeypatch.setattr(gk.requests, "request", lambda metoda, adres, headers, timeout, **k: (
        wyslane.append((metoda, adres, k["json"])), odpowiedz(200, {"id": "nowe"}))[1])
    od = datetime.datetime(2026, 10, 16, 10, 0, tzinfo=gk.STREFA)
    assert gk.dodaj("Rozmowa rekrutacyjna", od, od + datetime.timedelta(hours=1), "Biuro") == "nowe"
    metoda, adres, tresc = wyslane[0]
    assert metoda == "POST" and adres.endswith("/calendars/primary/events")
    assert tresc == {"summary": "Rozmowa rekrutacyjna", "location": "Biuro",
                     "start": {"dateTime": "2026-10-16T10:00:00+02:00", "timeZone": "Europe/Warsaw"},
                     "end": {"dateTime": "2026-10-16T11:00:00+02:00", "timeZone": "Europe/Warsaw"}}


def test_nie_ma_usuwania():
    assert not [n for n in dir(gk) if any(s in n.lower() for s in ("usun", "delete", "remove", "skasuj"))]
