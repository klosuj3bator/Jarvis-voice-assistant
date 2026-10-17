"""
setup_wizard.py — kreator pierwszego uruchomienia i okno Ustawień.

Uruchamia się sam, gdy brakuje klucza Anthropic (bez niego Jarvis nie ma
"mózgu"). Później to samo okno otwierasz z menu ikony przy zegarku:
Ustawienia. Podgląd bez Jarvisa: `python setup_wizard.py`.


JAK TO DZIAŁA
=============

Po jednej stronie na każdą usługę: Anthropic (wymagany), potem Spotify,
Telegram, Gmail i kalendarz — każdą można pominąć. Na każdej stronie:

  - krótka instrukcja i link, skąd wziąć klucz,
  - przycisk "Sprawdź i dalej", który NAPRAWDĘ łączy się z usługą, zanim
    puści Cię dalej. Zły klucz wychodzi od razu, a nie przy pierwszym
    "puść muzykę" tydzień później.

Nic nie jest zapisywane, dopóki nie klikniesz "Zapisz" na ostatniej stronie.
Zapis idzie do %APPDATA%\\Jarvis\\config.env (opis w konfiguracja.py) —
Twojego .env w folderze programu kreator nigdy nie rusza.


SPRAWDZANIE W TLE
=================

Sprawdzenie klucza to zapytanie przez internet — trwa sekundę albo dwie.
Gdyby szło w wątku okna, okno by w tym czasie zamarło. Dlatego sprawdzamy
w osobnym wątku, a wynik wraca do okna sygnałem Qt (tylko wątek okna może
bezpiecznie zmieniać to, co widać na ekranie).


KLUCZE NIE WYCIEKAJĄ
====================

Biblioteki sieciowe lubią wklejać adres zapytania w treść błędu — a w adresie
Telegrama siedzi token, adres kalendarza sam jest tajny. Dlatego komunikaty
błędów składamy sami, a do dziennika trafiają tylko nazwy usług i wyniki.
"""

import imaplib
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field

import requests
from PySide6.QtCore import QObject, Qt, QTime, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
                               QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QProgressBar, QPushButton, QSpinBox, QStackedWidget,
                               QTimeEdit, QVBoxLayout, QWidget)

import konfiguracja
import modele
import sciezki

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------
# Sprawdzanie kluczy — zwykłe funkcje, bez okna (da się je testować osobno).
# Każda zwraca: (czy_działa, komunikat dla człowieka).
# ---------------------------------------------------------------

def sprawdz_anthropic(w):
    """
    Jedno malutkie zapytanie do modelu, którego używa Jarvis (1 token
    odpowiedzi, koszt rzędu tysięcznej części centa). Sprawdza naraz
    trzy rzeczy: czy klucz jest prawdziwy, czy ma dostęp do modelu
    i czy na koncie są środki — to ostatnie jest najczęstszym powodem,
    dla którego "klucz nagle przestał działać".
    """
    import anthropic

    from agent import MODEL

    klucz = w["ANTHROPIC_API_KEY"]
    if not klucz.startswith("sk-ant-"):
        return False, "To nie wygląda na klucz Anthropic — powinien zaczynać się od „sk-ant-”."

    klient = anthropic.Anthropic(api_key=klucz, max_retries=0, timeout=20)
    try:
        klient.messages.create(model=MODEL, max_tokens=1, thinking={"type": "disabled"},
                               messages=[{"role": "user", "content": "ping"}])
    except anthropic.AuthenticationError:
        return False, "Ten klucz nie działa. Sprawdź, czy skopiowałeś go w całości."
    except anthropic.PermissionDeniedError:
        return False, f"Klucz działa, ale nie ma dostępu do modelu {MODEL}."
    except anthropic.RateLimitError:
        return True, "Klucz działa (serwer prosi o chwilę przerwy, ale to nie problem)."
    except anthropic.BadRequestError as e:
        if "credit balance" in str(e).lower():
            return False, ("Klucz działa, ale na koncie nie ma środków. Doładuj je "
                           "(Billing w panelu Anthropic) i sprawdź jeszcze raz.")
        return False, f"Anthropic odrzucił zapytanie ({e.status_code})."
    except anthropic.APIConnectionError:
        return False, "Nie mogę połączyć się z Anthropic — sprawdź internet."
    except anthropic.APIStatusError as e:
        return False, f"Anthropic odpowiedział błędem {e.status_code}. Spróbuj za chwilę."
    return True, f"Klucz działa — Jarvis będzie myślał modelem {MODEL}."


def sprawdz_spotify(w):
    """
    Client ID i Secret sprawdzamy, prosząc Spotify o token "aplikacji"
    (bez logowania na Twoje konto). Logowanie na konto — z kliknięciem
    "Zgadzam się" — nastąpi przy pierwszym "puść muzykę".
    """
    uri = w["SPOTIPY_REDIRECT_URI"]
    if "localhost" in uri:
        return False, ("Spotify nie przyjmuje już adresu „localhost”. "
                       "Wpisz http://127.0.0.1:8888/callback — tu i w panelu Spotify.")
    if not re.match(r"^https?://", uri):
        return False, "Redirect URI musi zaczynać się od http:// (np. http://127.0.0.1:8888/callback)."

    try:
        odp = requests.post("https://accounts.spotify.com/api/token",
                            data={"grant_type": "client_credentials"},
                            auth=(w["SPOTIPY_CLIENT_ID"], w["SPOTIPY_CLIENT_SECRET"]),
                            timeout=15)
    except requests.RequestException:
        return False, "Nie mogę połączyć się ze Spotify — sprawdź internet."
    if odp.status_code in (400, 401):
        return False, "Client ID albo Client Secret nie pasują — skopiuj je jeszcze raz."
    if odp.status_code != 200:
        return False, f"Spotify odpowiedział kodem {odp.status_code}. Spróbuj za chwilę."
    return True, (f"Klucze działają. W panelu Spotify Redirect URI musi być dokładnie: {uri}. "
                  "Przy pierwszym „puść muzykę” otworzy się logowanie do Spotify.")


def _telegram(token, metoda, **dane):
    """Zapytanie do Telegrama. Błędy BEZ adresu — w adresie jest token."""
    limit = dane.pop("_limit", 15)
    try:
        odp = requests.post(f"https://api.telegram.org/bot{token}/{metoda}", data=dane,
                            timeout=limit)
        return odp.status_code, odp.json()
    except (requests.RequestException, ValueError):
        return None, {}


def sprawdz_telegram(w):
    """
    Token sprawdza getMe. Twoje ID sprawdzamy, wysyłając Ci krótką wiadomość
    testową — to jedyny pewny sposób, a przy okazji widzisz na telefonie,
    że działa. Telegram pozwala botowi pisać tylko do tych, którzy napisali
    do niego pierwsi, więc najczęstszy błąd to "najpierw kliknij Start".
    """
    token, wlasciciel = w["TELEGRAM_JARVIS_TOKEN"], w["TELEGRAM_OWNER_ID"]
    if not re.fullmatch(r"\d+:[\w-]{20,}", token):
        return False, "To nie wygląda na token od @BotFather (liczby, dwukropek, długi ciąg znaków)."
    if not re.fullmatch(r"\d+", wlasciciel):
        return False, "Twoje ID to sama liczba — użyj przycisku „Wykryj moje ID”."

    kod, wynik = _telegram(token, "getMe")
    if kod is None:
        return False, "Nie mogę połączyć się z Telegramem — sprawdź internet."
    if kod == 401:
        return False, "Ten token nie działa. Skopiuj go jeszcze raz od @BotFather."
    if not wynik.get("ok"):
        return False, f"Telegram odpowiedział kodem {kod}."
    bot = wynik["result"].get("username", "twój bot")

    kod, wynik = _telegram(token, "sendMessage", chat_id=wlasciciel,
                           text="✅ Jarvis: połączenie z Telegramem działa. Tu będę odpisywać.")
    if kod == 400:
        return False, (f"Bot @{bot} nie może jeszcze do Ciebie pisać. Otwórz go w Telegramie, "
                       "kliknij Start (albo napisz cokolwiek) i sprawdź jeszcze raz.")
    if kod == 403:
        return False, f"Zablokowałeś bota @{bot} — odblokuj go w Telegramie i sprawdź jeszcze raz."
    if not wynik.get("ok"):
        return False, f"Nie udało się wysłać wiadomości testowej (kod {kod})."
    return True, f"Działa — bot @{bot} wysłał Ci wiadomość testową."


def wykryj_wlasciciela(token, stop, limit_s=120):
    """
    Czeka, aż ktoś napisze do bota, i oddaje jego ID — jak
    `python telegram_bridge.py kto-ja`, tylko w oknie.

    Zwraca: (id albo None, komunikat).
    """
    koniec = time.monotonic() + limit_s
    offset = None
    while time.monotonic() < koniec and not stop.is_set():
        dane = {"timeout": 5, "_limit": 15, "allowed_updates": '["message"]'}
        if offset is not None:
            dane["offset"] = offset
        kod, wynik = _telegram(token, "getUpdates", **dane)
        if kod == 409:
            return None, ("Jarvis właśnie korzysta z tego bota, więc nie mogę podsłuchać "
                          "wiadomości. Wpisz ID ręcznie (np. od @userinfobot).")
        if kod == 401:
            return None, "Ten token nie działa — popraw go i spróbuj jeszcze raz."
        if kod is None or not wynik.get("ok"):
            time.sleep(2)
            continue
        for aktualizacja in wynik["result"]:
            offset = aktualizacja["update_id"] + 1
            wiadomosc = aktualizacja.get("message") or {}
            nadawca = wiadomosc.get("from") or {}
            if nadawca.get("id") and (wiadomosc.get("chat") or {}).get("type") == "private":
                return nadawca["id"], f"Mam — to Ty, {nadawca.get('first_name', '')}."
    return None, "Nic nie przyszło. Napisz do bota i kliknij „Wykryj” jeszcze raz."


def _sprawdz_konto_gmail(adres, haslo):
    """Logowanie IMAP tylko do odczytu — dokładnie tak, jak robi to email_monitor.py."""
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", adres):
        return False, "to nie wygląda na adres e-mail."
    if len(haslo) != 16:
        return False, ("hasło aplikacji ma 16 liter (Google pokazuje je w czterech grupach "
                       "— spacje możesz zostawić). To nie jest zwykłe hasło do konta.")
    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=20)
        try:
            imap.login(adres, haslo)
            imap.select("INBOX", readonly=True)
        finally:
            try:
                imap.logout()
            except Exception:
                pass
    except imaplib.IMAP4.error:
        return False, ("Gmail odrzucił logowanie. Sprawdź adres i użyj HASŁA APLIKACJI, "
                       "nie zwykłego hasła do konta.")
    except OSError:
        return False, "nie mogę połączyć się z Gmailem — sprawdź internet."
    return True, ""


def sprawdz_gmail(w):
    """Każde wpisane konto osobno; błąd mówi, które konto i co z nim jest nie tak."""
    dzialajace = []
    for numer in range(1, konfiguracja.MAKS_KONT_GMAIL + 1):
        zm_nazwa, zm_adres, zm_haslo = konfiguracja.zmienne_konta(numer)
        adres, haslo = w.get(zm_adres, ""), w.get(zm_haslo, "")
        if not adres and not haslo:
            if w.get(zm_nazwa):
                return False, f"Konto {numer}: jest nazwa, ale brak adresu i hasła."
            continue
        if not adres or not haslo:
            return False, f"Konto {numer}: wpisz i adres, i hasło aplikacji."
        ok, problem = _sprawdz_konto_gmail(adres, haslo)
        if not ok:
            return False, f"Konto {numer}: {problem}"
        nazwy = [n.strip() for n in w.get(zm_nazwa, "").split(",") if n.strip()]
        dzialajace.append((numer, (nazwy or [adres.split("@")[0]])[0]))

    domyslne = w.get(konfiguracja.GMAIL_DOMYSLNE) or "1"
    if int(domyslne) not in [n for n, _ in dzialajace]:
        return False, f"Domyślne konto {domyslne} nie jest wpisane — wybierz jedno z wpisanych."
    opis = ", ".join(f"„{nazwa}”" + (" (domyślne)" if str(n) == domyslne else "")
                     for n, nazwa in dzialajace)
    return True, (f"Działa: {opis} — skrzynki otwarte tylko do odczytu. Google może przysłać "
                  "maila o nowym logowaniu; to normalne.")


def sprawdz_kalendarz(w):
    """
    Kalendarz działa przez Google Calendar API ("Połącz z Google") albo przez
    adres iCal (zapas, tylko odczyt). Adresu iCal nigdy nie pokazujemy w komunikatach.
    """
    import google_kalendarz

    adres = w["GOOGLE_CALENDAR_ICAL_URL"]
    if not adres:
        if google_kalendarz.polaczony():
            return True, "Połączony przez Google Calendar API (bez zapasowego adresu iCal)."
        return False, "Kliknij „Połącz z Google” albo wpisz tajny adres iCal."
    if not adres.startswith("https://") or ".ics" not in adres:
        return False, "To nie wygląda na adres iCal — powinien zaczynać się od https:// i kończyć na .ics."
    if "/public/" in adres:
        return False, ("To adres PUBLICZNY — widać w nim tylko „zajęty”, bez nazw wydarzeń. "
                       "Skopiuj „Tajny adres w formacie iCal”.")
    try:
        odp = requests.get(adres, timeout=15)
    except requests.RequestException:
        return False, "Nie mogę pobrać kalendarza — sprawdź internet."
    if odp.status_code == 404:
        return False, "Google nie zna tego adresu — mógł zostać zresetowany. Skopiuj go jeszcze raz."
    if odp.status_code != 200 or b"BEGIN:VCALENDAR" not in odp.content[:300]:
        return False, f"Pod tym adresem nie ma kalendarza (kod {odp.status_code})."
    ile = odp.content.count(b"BEGIN:VEVENT")
    # 1 wydarzenie, 2-4 wydarzenia (ale 12-14 wydarzeń), reszta: wydarzeń.
    slowo = ("wydarzenie" if ile == 1 else "wydarzenia"
             if ile % 10 in (2, 3, 4) and ile % 100 not in (12, 13, 14) else "wydarzeń")
    return True, f"Działa — kalendarz ma {ile} {slowo}."


# ---------------------------------------------------------------
# Strony kreatora — same dane: co wpisać, skąd to wziąć, jak sprawdzić.
# ---------------------------------------------------------------

@dataclass
class Pole:
    zmienna: str
    etykieta: str
    tajne: bool = True
    podpowiedz: str = ""
    wymagane: bool = True        # puste blokuje "Sprawdź i dalej"
    zapas: str = ""              # zmienna, z której wypełnić pole, gdy własnej brak (stary zapis)
    wybor: tuple = ()            # zamiast pola tekstowego — lista do wyboru


@dataclass
class Strona:
    funkcja: str                 # klucz z konfiguracja.FUNKCJE
    tytul: str
    opis: str
    instrukcja: str              # HTML z linkami
    pola: list
    sprawdz: object              # funkcja(wartości) -> (ok, komunikat)
    popraw: dict = field(default_factory=dict)   # zmienna -> funkcja czyszcząca wpis
    # >0: pola w siatce po tyle w wierszu (np. Gmail: nazwa | adres | hasło,
    # wiersz na konto); pola ponad pełne wiersze idą pod siatkę, jak zwykle.
    kolumny: int = 0
    wiersze: tuple = ()          # podpisy wierszy siatki, np. ("KONTO 1", ...)
    # Wartości zapisywane razem ze stroną (po udanym sprawdzeniu i przy "Wyłącz").
    dodatkowe: dict = field(default_factory=dict)


def _link(adres, tekst):
    return f'<a href="{adres}" style="color:#ffba40; text-decoration:none;">{tekst} ↗</a>'


def _pola_gmail():
    """Siatka kont Gmail: nazwa | adres | hasło aplikacji, plus konto domyślne."""
    pola = []
    for numer in range(1, konfiguracja.MAKS_KONT_GMAIL + 1):
        nazwa, adres, haslo = konfiguracja.zmienne_konta(numer)
        pierwsze = numer == 1
        pola += [
            Pole(nazwa, "Nazwy (opcjonalnie)", tajne=False, wymagane=False,
                 podpowiedz="np. prywatne" if pierwsze else "np. praca"),
            # Konto 1 wypełnia się starym zapisem z .env (GMAIL_ADDRESS), jeśli jest.
            Pole(adres, "Adres Gmail", tajne=False, wymagane=pierwsze, podpowiedz="ty@gmail.com",
                 zapas=konfiguracja.STARE_KONTO_GMAIL[0] if pierwsze else ""),
            Pole(haslo, "Hasło aplikacji", wymagane=pierwsze, podpowiedz="xxxx xxxx xxxx xxxx",
                 zapas=konfiguracja.STARE_KONTO_GMAIL[1] if pierwsze else ""),
        ]
    pola.append(Pole(konfiguracja.GMAIL_DOMYSLNE, "Domyślne konto", tajne=False, wymagane=False,
                     wybor=tuple(str(n) for n in range(1, konfiguracja.MAKS_KONT_GMAIL + 1))))
    return pola


_POLA_GMAIL = _pola_gmail()


STRONY = [
    Strona(
        "anthropic", "Klucz Anthropic",
        "Mózg Jarvisa — model Claude rozumie, co mówisz, i decyduje, co zrobić. "
        "Bez tego klucza Jarvis nie wystartuje.",
        f"1. Otwórz {_link('https://platform.claude.com', 'panel Anthropic')} i zaloguj się.<br>"
        "2. Wejdź w <b>API Keys</b> → <b>Create Key</b>, nazwij go np. „Jarvis”.<br>"
        "3. Skopiuj klucz (zaczyna się od <code>sk-ant-</code>) — Anthropic pokazuje go tylko raz.<br>"
        "4. Upewnij się, że w <b>Billing</b> masz doładowane środki.",
        [Pole("ANTHROPIC_API_KEY", "Klucz API", podpowiedz="sk-ant-…")],
        sprawdz_anthropic),
    Strona(
        "spotify", "Spotify",
        "Włączanie piosenek i albumów, pauza, następny utwór. "
        "Sterowanie odtwarzaniem wymaga konta Premium.",
        f"1. Otwórz {_link('https://developer.spotify.com/dashboard', 'Spotify for Developers')} "
        "→ <b>Create app</b>.<br>"
        "2. W <b>Redirect URIs</b> wpisz dokładnie <code>http://127.0.0.1:8888/callback</code>, "
        "zaznacz <b>Web API</b>.<br>"
        "3. W ustawieniach aplikacji skopiuj <b>Client ID</b> i <b>Client Secret</b>.",
        [Pole("SPOTIPY_CLIENT_ID", "Client ID", tajne=False),
         Pole("SPOTIPY_CLIENT_SECRET", "Client Secret"),
         Pole("SPOTIPY_REDIRECT_URI", "Redirect URI", tajne=False)],
        sprawdz_spotify),
    Strona(
        "telegram", "Telegram",
        "Rozmowa z Jarvisem z telefonu — tekstem i głosówkami, przypomnienia "
        "i powiadomienia o mailach na telefon, zrzuty ekranu.",
        f"1. W Telegramie napisz do {_link('https://t.me/BotFather', '@BotFather')}, "
        "wyślij <code>/newbot</code> i wybierz nazwę.<br>"
        "2. Wklej token, który dostaniesz.<br>"
        "3. Otwórz swojego nowego bota, kliknij <b>Start</b>, potem tutaj <b>Wykryj moje ID</b>.<br>"
        "Bot będzie odpowiadał wyłącznie Tobie.",
        [Pole("TELEGRAM_JARVIS_TOKEN", "Token bota"),
         Pole("TELEGRAM_OWNER_ID", "Twoje ID", tajne=False, podpowiedz="np. 123456789")],
        sprawdz_telegram,
        popraw={"TELEGRAM_OWNER_ID": lambda s: s.strip()}),
    Strona(
        "gmail", "Gmail",
        "Szukanie maili i czujki z powiadomieniem na Telegram — do trzech kont, "
        "tylko do odczytu. Bez wskazania konta Jarvis sprawdza wszystkie.",
        "Dla każdego konta: włącz weryfikację dwuetapową, otwórz "
        f"{_link('https://myaccount.google.com/apppasswords', 'Hasła aplikacji')} i utwórz "
        "hasło „Jarvis” — wklej je, <b>nie</b> zwykłe hasło.<br>"
        "Nazwa jest opcjonalna: domyślnie to część adresu przed „@”; własne nazwy "
        "(„praca”, „prywatne”) wpisz po przecinku.",
        _POLA_GMAIL,
        sprawdz_gmail,
        popraw={zmienne[2]: (lambda s: s.replace(" ", "")) for zmienne in
                (konfiguracja.zmienne_konta(n) for n in range(1, konfiguracja.MAKS_KONT_GMAIL + 1))},
        kolumny=3,
        wiersze=tuple(f"KONTO {n}" for n in range(1, konfiguracja.MAKS_KONT_GMAIL + 1)),
        # Stary zapis z jednym kontem (GMAIL_ADDRESS w .env) — po zapisaniu tej
        # strony konto 1 siedzi już w GMAIL_1_*, a pusty stary zapis w Ustawieniach
        # wyłącza ten z .env (opis w konfiguracja.py). Sam .env zostaje nietknięty.
        dodatkowe={zmienna: "" for zmienna in konfiguracja.STARE_KONTO_GMAIL}),
    Strona(
        "kalendarz", "Kalendarz Google",
        "„Co mam jutro?”, „dodaj rozmowę w piątek o 10” i przypomnienia przed "
        "wydarzeniami. Usuwać wydarzeń Jarvis nie może.",
        f"<b>Google Calendar API</b> (czytanie i dodawanie): w {_link('https://console.cloud.google.com/apis/library/calendar-json.googleapis.com', 'Google Cloud')} "
        "włącz Calendar API, na <b>ekranie zgody OAuth</b> dodaj siebie jako testera, utwórz "
        "<b>identyfikator klienta OAuth → Aplikacja komputerowa</b> i zapisz pobrany JSON jako "
        f"<code>google_credentials.json</code> w <code>{os.path.dirname(konfiguracja.PLIK_GOOGLE_KLIENTA)}</code>. "
        "Potem <b>Połącz z Google</b>.<br>"
        f"<b>Zapas</b> (tylko odczyt): {_link('https://calendar.google.com/calendar/r/settings', 'ustawienia kalendarza')} "
        "→ Integracja kalendarza → <b>Tajny adres w formacie iCal</b> — traktuj go jak hasło.",
        [Pole("GOOGLE_CALENDAR_ICAL_URL", "Adres iCal (zapas)", wymagane=False,
              podpowiedz="https://calendar.google.com/calendar/ical/…/basic.ics")],
        sprawdz_kalendarz),
]


# ---------------------------------------------------------------
# Wygląd — kolory i czcionki HUD-a (paleta z gui.py)
# ---------------------------------------------------------------

# Pełna ścieżka (C:\Users\…\AppData\Roaming\…) łamała się w pół słowa — pokazujemy
# krótszą, a pełna jest w dymku po najechaniu myszą.
SCIEZKA_DO_POKAZANIA = r"%APPDATA%\Jarvis\config.env"

CYJAN = "#56d6ff"
NIEBIESKI = "#3aa8de"
BIALY = "#dcf2ff"
BURSZTYN = "#ffba40"
CZERWONY = "#ff7a6b"

STYL = f"""
QDialog#kreator {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #0b2433, stop:1 #02060a);
}}
QLabel {{ color: {BIALY}; font-family: Bahnschrift; font-size: 14px; }}
QLabel#marka {{ color: {CYJAN}; font-size: 12px; }}
QLabel#krok {{ color: {NIEBIESKI}; font-size: 12px; }}
QLabel#naglowek {{ color: {BIALY}; font-size: 28px; font-weight: 300; }}
QLabel#znaczek {{ color: {BURSZTYN}; font-size: 11px; border: 1px solid #9c7330;
                  border-radius: 3px; padding: 2px 6px; }}
QLabel#opis {{ color: #b7dcee; font-size: 14px; }}
QLabel#instrukcja {{ color: #8fb9cc; font-size: 13px; background: rgba(8, 32, 46, 0.7);
                     border-left: 2px solid {NIEBIESKI}; padding: 10px 12px; }}
QLabel#etykieta {{ color: {NIEBIESKI}; font-size: 12px; }}
QLabel#status {{ font-size: 13px; }}
QLineEdit {{
    background: rgba(10, 40, 56, 0.9); color: {BIALY};
    border: 1px solid #1f6f94; border-radius: 3px; padding: 8px 10px;
    font-family: Consolas; font-size: 13px; selection-background-color: {NIEBIESKI};
}}
QLineEdit:focus {{ border: 1px solid {CYJAN}; }}
QComboBox {{
    background: rgba(10, 40, 56, 0.9); color: {BIALY};
    border: 1px solid #1f6f94; border-radius: 3px; padding: 8px 10px;
    font-family: Bahnschrift; font-size: 13px;
}}
QComboBox:focus {{ border: 1px solid {CYJAN}; }}
QSpinBox, QDoubleSpinBox, QTimeEdit {{
    background: rgba(10, 40, 56, 0.9); color: {BIALY};
    border: 1px solid #1f6f94; border-radius: 3px; padding: 6px 10px;
    font-family: Bahnschrift; font-size: 13px;
}}
QSpinBox:focus, QDoubleSpinBox:focus, QTimeEdit:focus {{ border: 1px solid {CYJAN}; }}
QSpinBox:disabled, QTimeEdit:disabled {{ color: #2d5566; border-color: #1d3a47; }}
QCheckBox {{ color: {BIALY}; font-family: Bahnschrift; font-size: 13px; spacing: 10px; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border: 1px solid #1f6f94;
                        border-radius: 2px; background: rgba(10, 40, 56, 0.9); }}
QCheckBox::indicator:checked {{ background: {CYJAN}; border-color: {CYJAN}; }}
QComboBox QAbstractItemView {{
    background: #06141d; color: {BIALY}; selection-background-color: #1f6f94;
    border: 1px solid #1f6f94;
}}
QPushButton {{
    background: transparent; color: {CYJAN}; border: 1px solid {NIEBIESKI};
    border-radius: 3px; padding: 8px 18px; font-family: Bahnschrift; font-size: 13px;
}}
QPushButton:hover {{ background: rgba(86, 214, 255, 0.12); }}
QPushButton#glowny {{ background: rgba(86, 214, 255, 0.20); color: {BIALY};
                      border: 1px solid {CYJAN}; }}
QPushButton#glowny:hover {{ background: rgba(86, 214, 255, 0.32); }}
QPushButton#boczny {{ color: {BURSZTYN}; border-color: #9c7330; }}
QPushButton#zakladka {{ padding: 5px 12px; color: #8fb3c4; border-color: #1f4a5c; }}
QPushButton#zakladka:checked {{ color: {BIALY}; background: rgba(86, 214, 255, 0.18); border-color: {CYJAN}; }}
QPushButton#boczny:hover {{ background: rgba(255, 186, 64, 0.10); }}
QPushButton#oko {{ padding: 6px 10px; font-size: 11px; }}
QPushButton:disabled {{ color: #2d5566; border-color: #1d3a47; background: transparent; }}
QProgressBar {{
    background: rgba(10, 40, 56, 0.9); border: 1px solid #1f6f94; border-radius: 3px;
    min-height: 16px; max-height: 16px; text-align: center; color: {BIALY};
    font-family: Consolas; font-size: 11px;
}}
QProgressBar::chunk {{
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {NIEBIESKI}, stop:1 {CYJAN});
}}
QFrame#linia {{ background: {NIEBIESKI}; max-height: 1px; min-height: 1px; border: none; }}
QMessageBox {{ background: #06141d; }}
QMessageBox QLabel {{ color: {BIALY}; }}
"""


def _czcionka_rozstrzelona(rozmiar, odstep):
    """Napisy w stylu HUD-a: "J.A.R.V.I.S. // KONFIGURACJA" z odstępami między literami.
    (Arkusze stylów Qt nie znają letter-spacing, więc ustawiamy to czcionką.)"""
    czcionka = QFont("Bahnschrift", rozmiar)
    czcionka.setLetterSpacing(QFont.AbsoluteSpacing, odstep)
    return czcionka


class _Pierscien(QWidget):
    """Mały, nieruchomy "reaktor" w rogu — ten sam motyw co kula HUD-a.
    Celowo bez animacji: okno Ustawień bywa otwarte przy działającym HUD-zie,
    a każda klatka animacji to Python w wątku okna (opis w gui.py)."""

    def __init__(self, rozmiar=46):
        super().__init__()
        self.setFixedSize(rozmiar, rozmiar)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(3, 3, -3, -3)
        for kolor, grubosc, margines, start, dlugosc in (
                (NIEBIESKI, 1.2, 0, 0, 360), (CYJAN, 2.4, 6, 30, 250),
                (BURSZTYN, 2.4, 6, 300, 50), (CYJAN, 1.2, 12, 200, 280)):
            pioro = QPen(QColor(kolor))
            pioro.setWidthF(grubosc)
            p.setPen(pioro)
            p.drawArc(r.adjusted(margines, margines, -margines, -margines),
                      start * 16, dlugosc * 16)
        p.setBrush(QColor(86, 214, 255, 90))
        p.setPen(Qt.NoPen)
        p.drawEllipse(r.center(), 4, 4)


class _Sygnaly(QObject):
    """Wyniki z wątków w tle wracają do okna tylko przez sygnały (opis modułu)."""
    sprawdzono = Signal(object)
    wykryto = Signal(object)
    postep = Signal(object)
    pobrano = Signal(object)
    google = Signal(object)          # (ok, komunikat) po "Połącz z Google"


class StronaModeli(QWidget):
    """
    Pobieranie modeli mowy (modele.py) z paskiem postępu.

    Używa jej kreator (jako jednej ze stron — pobieranie rusza od razu, więc
    modele ściągają się w tle, gdy wpisujesz klucze) i osobne okienko
    pobierz_modele(), gdy klucze są, a modeli brakuje.

    Sygnał `procent` (0-100) karmi mały licznik w nagłówku kreatora,
    `gotowe` — przyciski "Dalej" / "Uruchom Jarvisa".
    """

    procent = Signal(int)
    gotowe = Signal()

    def __init__(self):
        super().__init__()
        self._stop = threading.Event()
        self._skonczone = False
        self._start = None
        self._sygnaly = _Sygnaly()
        self._sygnaly.postep.connect(self._pokaz_postep)
        self._sygnaly.pobrano.connect(self._po_pobraniu)

        uklad = QVBoxLayout(self)
        uklad.setSpacing(12)
        wiersz = QHBoxLayout()
        naglowek = QLabel("Modele mowy")
        naglowek.setObjectName("naglowek")
        wiersz.addWidget(naglowek)
        znaczek = QLabel("WYMAGANE")
        znaczek.setObjectName("znaczek")
        znaczek.setFont(_czcionka_rozstrzelona(8, 2))
        wiersz.addWidget(znaczek, 0, Qt.AlignVCenter)
        wiersz.addStretch()
        uklad.addLayout(wiersz)

        opis = QLabel("Whisper zamienia Twój głos na tekst, a openWakeWord słyszy „Hey Jarvis”. "
                      "Oba działają na tym komputerze, bez wysyłania dźwięku do internetu — "
                      "trzeba je tylko raz pobrać.")
        opis.setObjectName("opis")
        opis.setWordWrap(True)
        uklad.addWidget(opis)
        miejsce = QLabel("Ok. 560 MB do <nobr><code>%APPDATA%\\Jarvis\\modele</code></nobr> "
                         "— zwykle kilka minut. Instalator ich nie zawiera, żeby był "
                         "pięć razy mniejszy.")
        miejsce.setObjectName("instrukcja")
        miejsce.setWordWrap(True)
        miejsce.setTextFormat(Qt.RichText)
        miejsce.setToolTip(sciezki.MODELE)
        uklad.addWidget(miejsce)

        self._pasek = QProgressBar()
        self._pasek.setRange(0, 1000)
        self._pasek.setFormat("%p%")
        uklad.addWidget(self._pasek)
        self._opis = QLabel("Łączę się…")
        self._opis.setObjectName("status")
        self._opis.setStyleSheet(f"color: {NIEBIESKI};")
        uklad.addWidget(self._opis)
        self._ponow = QPushButton("Spróbuj jeszcze raz")
        self._ponow.setObjectName("boczny")
        self._ponow.clicked.connect(self.start)
        self._ponow.hide()
        uklad.addWidget(self._ponow, 0, Qt.AlignLeft)
        uklad.addStretch()

    @property
    def skonczone(self):
        return self._skonczone

    def start(self):
        """Rusza pobieranie w tle (albo ponawia je po błędzie)."""
        self._ponow.hide()
        self._stop = threading.Event()
        self._start = time.monotonic()
        stop = self._stop
        self._opis.setStyleSheet(f"color: {NIEBIESKI};")
        self._opis.setText("Łączę się…")

        def praca():
            try:
                modele.pobierz(postep=lambda *p: self._sygnaly.postep.emit(p), stop=stop)
                wynik = None
            except modele.BladPobierania as e:
                wynik = str(e)
            except Exception:
                logger.exception("Pobieranie modeli zawiodło")
                wynik = "Nieoczekiwany błąd — szczegóły są w dzienniku Jarvisa."
            if not stop.is_set():
                self._sygnaly.pobrano.emit(wynik)

        threading.Thread(target=praca, name="kreator-modele", daemon=True).start()

    def zatrzymaj(self):
        self._stop.set()

    def _pokaz_postep(self, dane):
        pobrano, razem, plik = dane
        if not razem:
            return
        self._pasek.setValue(int(pobrano * 1000 / razem))
        self.procent.emit(int(pobrano * 100 / razem))
        sekundy = max(time.monotonic() - self._start, 0.1)
        predkosc = pobrano / sekundy
        zostalo = (razem - pobrano) / predkosc if predkosc else 0
        self._opis.setText(f"{plik}  —  {pobrano / 1e6:.0f} z {razem / 1e6:.0f} MB, "
                           f"{predkosc / 1e6:.1f} MB/s, zostało ok. {_czas(zostalo)}")

    def _po_pobraniu(self, blad):
        if blad:
            self._opis.setStyleSheet(f"color: {CZERWONY};")
            self._opis.setText(blad)
            self._ponow.show()
            return
        self._skonczone = True
        self._pasek.setValue(1000)
        self.procent.emit(100)
        self._opis.setStyleSheet(f"color: {CYJAN};")
        self._opis.setText("✓ Modele pobrane i sprawdzone.")
        self.gotowe.emit()


def _czas(sekundy):
    if sekundy < 60:
        return f"{max(int(sekundy), 1)} s"
    return f"{int(sekundy // 60)} min {int(sekundy % 60)} s"


# ---------------------------------------------------------------
# Okno
# ---------------------------------------------------------------

class KreatorJarvisa(QDialog):
    """
    pierwsze=True  — kreator pierwszego uruchomienia (strona powitalna,
                     przycisk "Uruchom Jarvisa", zamknięcie = Jarvis nie startuje)
    pierwsze=False — okno Ustawień (pola wypełnione obecnymi kluczami)
    """

    def __init__(self, pierwsze=False, rodzic=None, strona=None):
        super().__init__(rodzic)
        self._pierwsze = pierwsze
        self._rozlacz_google = False     # "Wyłącz" na stronie kalendarza — usuń token przy zapisie
        self._zmiany = {}
        self._wyniki = {}          # funkcja -> "ok" / "pominieta" / "wylaczona"
        self._stop_wykrywania = threading.Event()
        self._sygnaly = _Sygnaly()
        self._sygnaly.sprawdzono.connect(self._po_sprawdzeniu)
        self._sygnaly.wykryto.connect(self._po_wykryciu)
        self._sygnaly.google.connect(self._po_google)

        self.setObjectName("kreator")
        self.setStyleSheet(STYL)
        self.setWindowTitle("Jarvis — pierwsze uruchomienie" if pierwsze else "Jarvis — ustawienia")
        ikona = sciezki.zasob("jarvis_icon.png")
        if os.path.exists(ikona):
            self.setWindowIcon(QIcon(ikona))
        self.setFixedSize(760, 640)

        uklad = QVBoxLayout(self)
        uklad.setContentsMargins(34, 24, 34, 24)
        uklad.setSpacing(14)

        gora = QHBoxLayout()
        gora.addWidget(_Pierscien())
        marka = QLabel("J.A.R.V.I.S.  //  KONFIGURACJA")
        marka.setObjectName("marka")
        marka.setFont(_czcionka_rozstrzelona(10, 3))
        gora.addWidget(marka)
        gora.addStretch()
        # Licznik pobierania modeli — widoczny na każdej stronie, bo modele
        # ściągają się w tle, gdy wpisujesz klucze.
        self._licznik_modeli = QLabel()
        self._licznik_modeli.setObjectName("krok")
        self._licznik_modeli.setFont(_czcionka_rozstrzelona(9, 2))
        self._licznik_modeli.setStyleSheet(f"color: {BURSZTYN};")
        self._licznik_modeli.hide()
        gora.addWidget(self._licznik_modeli)
        gora.addSpacing(18)
        self._krok = QLabel()
        self._krok.setObjectName("krok")
        self._krok.setFont(_czcionka_rozstrzelona(9, 2))
        gora.addWidget(self._krok)
        uklad.addLayout(gora)

        linia = QFrame()
        linia.setObjectName("linia")
        uklad.addWidget(linia)

        # Ustawienia mają zakładki (kreator pierwszego uruchomienia — nie):
        # Klucze API (przegląd + strony kluczy), Ogólne, Tapeta, Panel boczny, Nasłuch, Podsumowanie.
        self._zakladki = {}
        if not pierwsze:
            wiersz_zakladek = QHBoxLayout()
            wiersz_zakladek.setSpacing(6)
            for nazwa in ("Klucze API", "Ogólne", "Tapeta", "Panel boczny", "Nasłuch", "Podsumowanie"):
                zakladka = QPushButton(nazwa)
                zakladka.setObjectName("zakladka")
                zakladka.setCheckable(True)
                zakladka.clicked.connect(lambda _, n=nazwa: self._otworz_zakladke(n))
                wiersz_zakladek.addWidget(zakladka)
                self._zakladki[nazwa] = zakladka
            wiersz_zakladek.addStretch()
            uklad.addLayout(wiersz_zakladek)

        self._stos = QStackedWidget()
        uklad.addWidget(self._stos, 1)

        self._strony = []          # (Strona albo None, widget, słownik pól, status)
        self._indeks_strony = {}   # funkcja ("anthropic", "kalendarz"…) -> numer strony
        self._modele = None
        if pierwsze:
            self._dodaj_powitanie()
        else:
            self._dodaj_przeglad_kluczy()
        for strona in STRONY:
            self._indeks_strony[strona.funkcja] = self._stos.count()
            self._dodaj_strone(strona)
        # Wersja z instalatora bez pobranych modeli: strona pobierania przed
        # podsumowaniem, a samo pobieranie rusza od razu, w tle.
        if modele.potrzebne_pobranie():
            self._dodaj_strone_modeli()
        # Monitor tapety — tylko w Ustawieniach; pierwsze uruchomienie
        # dotyczy kluczy, a tapeta i tak startuje na głównym monitorze.
        self._wybor_monitora = None
        self._panel_wlaczony = None
        self._powitanie_wlaczone = None
        self._roadmapa_wlaczona = None
        self._indeks_ogolnych = self._indeks_tapety = self._indeks_panelu = self._indeks_nasluchu = \
            self._stos.count()
        if not pierwsze:
            self._indeks_ogolnych = self._stos.count()
            self._dodaj_strone_ogolna()
            self._indeks_tapety = self._stos.count()
            self._dodaj_strone_tapety()
            self._indeks_panelu = self._stos.count()
            self._dodaj_strone_panelu()
            self._indeks_nasluchu = self._stos.count()
            self._dodaj_strone_nasluchu()
        self._dodaj_podsumowanie()

        self._stos.currentChanged.connect(self._po_zmianie_strony)
        self._stos.setCurrentIndex(0)        # w Ustawieniach to przegląd kluczy
        if strona == "klucze" and "Klucze API" in self._zakladki:
            self._otworz_zakladke("Klucze API")
        self._po_zmianie_strony(self._stos.currentIndex())
        self._odswiez_krok()

    # --- zakładki (tylko Ustawienia) ---

    def _poczatki_zakladek(self):
        """Pierwsza strona każdej zakładki, w kolejności."""
        return {"Klucze API": 0, "Ogólne": self._indeks_ogolnych, "Tapeta": self._indeks_tapety,
                "Panel boczny": self._indeks_panelu, "Nasłuch": self._indeks_nasluchu,
                "Podsumowanie": self._stos.count() - 1}

    def _otworz_zakladke(self, nazwa):
        # Zmiany z opuszczanej strony zapamiętujemy już teraz — inaczej
        # przejście zakładką prosto do Podsumowania gubiłoby je (zapisywał
        # je dopiero przycisk "Dalej").
        self._zbierz_ustawienia()
        self._stop_wykrywania.set()
        self._stos.setCurrentIndex(self._poczatki_zakladek()[nazwa])
        self._odswiez_krok()

    def _po_zmianie_strony(self, indeks):
        if self._zakladki:
            zakladka = [nazwa for nazwa, poczatek in self._poczatki_zakladek().items()
                        if indeks >= poczatek][-1]
            for nazwa, przycisk in self._zakladki.items():
                przycisk.setChecked(nazwa == zakladka)
        if indeks == 0 and not self._pierwsze:
            self._odswiez_przeglad()

    def _stan_klucza(self, funkcja):
        """(opis, kolor) dla przeglądu kluczy — z tym, co zmieniono w tym oknie."""
        wynik = self._wyniki.get(funkcja)
        if wynik == "ok":
            return "ustawiony (zapisz na końcu)", CYJAN
        if wynik == "wylaczona" or konfiguracja.wylaczona(funkcja):
            return "wyłączony", BURSZTYN
        if konfiguracja.skonfigurowana(funkcja):
            return "ustawiony", CYJAN
        if konfiguracja.FUNKCJE[funkcja].wymagana:
            return "BRAK — bez niego Jarvis nie rozmawia", CZERWONY
        return "brak", CZERWONY

    def _dodaj_przeglad_kluczy(self):
        widget = QWidget()
        uklad = QVBoxLayout(widget)
        uklad.setSpacing(12)
        self._naglowek(uklad, "Klucze API")
        uklad.addWidget(self._etykieta(
            "Wymagany jest tylko klucz Anthropic — bez niego Jarvis nie rozmawia. Pozostałe "
            "włączają poszczególne funkcje. Kliknij, żeby ustawić albo zmienić.", "opis"))
        siatka = QGridLayout()
        siatka.setHorizontalSpacing(14)
        siatka.setVerticalSpacing(10)
        self._przeglad = {}
        for wiersz, strona in enumerate(STRONY):
            nazwa = QLabel(strona.tytul)
            nazwa.setObjectName("opis")
            stan = QLabel()
            stan.setObjectName("status")
            przycisk = QPushButton("Ustaw")
            przycisk.setObjectName("boczny")
            przycisk.clicked.connect(lambda _, f=strona.funkcja: self._idz_do(f))
            siatka.addWidget(nazwa, wiersz, 0)
            siatka.addWidget(stan, wiersz, 1)
            siatka.addWidget(przycisk, wiersz, 2)
            self._przeglad[strona.funkcja] = (stan, przycisk)
        siatka.setColumnStretch(1, 1)
        uklad.addLayout(siatka)
        uklad.addStretch()
        uklad.addLayout(self._przyciski(dalej=("Dalej", self._dalej)))
        self._stos.addWidget(widget)
        self._strony.append((None, widget, {}, None))

    def _odswiez_przeglad(self):
        for funkcja, (stan, przycisk) in self._przeglad.items():
            opis, kolor = self._stan_klucza(funkcja)
            stan.setText(opis)
            stan.setStyleSheet(f"color: {kolor};")
            przycisk.setText("Zmień" if kolor == CYJAN else "Ustaw")

    def _idz_do(self, funkcja):
        self._stos.setCurrentIndex(self._indeks_strony[funkcja])
        self._odswiez_krok()

    # --- budowa stron ---

    def _naglowek(self, uklad, tekst, znaczek=None):
        wiersz = QHBoxLayout()
        naglowek = QLabel(tekst)
        naglowek.setObjectName("naglowek")
        wiersz.addWidget(naglowek)
        if znaczek:
            z = QLabel(znaczek)
            z.setObjectName("znaczek")
            z.setFont(_czcionka_rozstrzelona(8, 2))
            wiersz.addWidget(z, 0, Qt.AlignVCenter)
        wiersz.addStretch()
        uklad.addLayout(wiersz)

    def _etykieta(self, tekst, nazwa, bogata=False):
        etykieta = QLabel(tekst)
        etykieta.setObjectName(nazwa)
        etykieta.setWordWrap(True)
        if bogata:
            etykieta.setTextFormat(Qt.RichText)
            etykieta.setOpenExternalLinks(True)
        return etykieta

    def _dodaj_powitanie(self):
        widget = QWidget()
        uklad = QVBoxLayout(widget)
        uklad.setSpacing(16)
        uklad.addSpacing(20)
        self._naglowek(uklad, "Witaj. Skonfigurujmy Jarvisa.")
        uklad.addWidget(self._etykieta(
            "Potrzebny jest tylko <b>klucz Anthropic</b> — to mózg Jarvisa. Spotify, Telegram, "
            "Gmail i kalendarz są opcjonalne: każdy możesz pominąć i dodać później "
            "w <b>Ustawieniach</b> (prawy przycisk na ikonie Jarvisa przy zegarku).", "opis", True))
        sciezka = self._etykieta(
            "Każdy klucz sprawdzę od razu, zanim pójdziemy dalej. Zapiszę je u Ciebie, "
            f"w <nobr><code>{SCIEZKA_DO_POKAZANIA}</code></nobr> — nie w folderze programu.", "instrukcja", True)
        sciezka.setToolTip(konfiguracja.PLIK)
        uklad.addWidget(sciezka)
        uklad.addStretch()
        uklad.addLayout(self._przyciski(dalej=("Zaczynamy", self._dalej)))
        self._stos.addWidget(widget)
        self._strony.append((None, widget, {}, None))

    def _dodaj_strone(self, strona):
        widget = QWidget()
        uklad = QVBoxLayout(widget)
        uklad.setSpacing(12)
        wymagana = konfiguracja.FUNKCJE[strona.funkcja].wymagana
        self._naglowek(uklad, strona.tytul, "WYMAGANE" if wymagana else "OPCJONALNE")
        uklad.addWidget(self._etykieta(strona.opis, "opis"))
        uklad.addWidget(self._etykieta(strona.instrukcja, "instrukcja", True))

        formularz = QFormLayout()
        formularz.setSpacing(10)
        formularz.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        # Siatka (np. konta Gmail): pierwsze kolumny*len(wiersze) pól idzie tutaj,
        # wiersz po wierszu, a nad nią nagłówki kolumn z etykiet pierwszego wiersza.
        w_siatce = strona.kolumny * len(strona.wiersze)
        siatka = None
        if w_siatce:
            siatka = QGridLayout()
            siatka.setHorizontalSpacing(8)
            siatka.setVerticalSpacing(8)
            for kolumna, pole in enumerate(strona.pola[:strona.kolumny]):
                naglowek = QLabel(pole.etykieta.upper())
                naglowek.setObjectName("etykieta")
                naglowek.setFont(_czcionka_rozstrzelona(8, 1.5))
                siatka.addWidget(naglowek, 0, kolumna + 1)
            for numer, podpis in enumerate(strona.wiersze):
                etykieta = QLabel(podpis)
                etykieta.setObjectName("etykieta")
                etykieta.setFont(_czcionka_rozstrzelona(8, 1.5))
                siatka.addWidget(etykieta, numer + 1, 0)
            siatka.setColumnStretch(1, 2)
            siatka.setColumnStretch(2, 3)
            siatka.setColumnStretch(3, 3)
        pola = {}
        for indeks, pole in enumerate(strona.pola):
            obecna = (konfiguracja.wartosc(pole.zmienna)
                      or (konfiguracja.wartosc(pole.zapas) if pole.zapas else "")
                      or konfiguracja.DOMYSLNE.get(pole.zmienna, ""))
            if pole.wybor:
                wpis = QComboBox()
                for opcja in pole.wybor:
                    wpis.addItem(opcja, opcja)
                wpis.setCurrentIndex(max(0, wpis.findData(obecna)))
            else:
                wpis = QLineEdit(obecna)
                wpis.setPlaceholderText(pole.podpowiedz)
            wiersz = QHBoxLayout()
            wiersz.addWidget(wpis, 1)
            if pole.tajne:
                wpis.setEchoMode(QLineEdit.Password)
                oko = QPushButton("POKAŻ")
                oko.setObjectName("oko")
                oko.setCheckable(True)
                oko.toggled.connect(lambda w, e=wpis, o=oko: (
                    e.setEchoMode(QLineEdit.Normal if w else QLineEdit.Password),
                    o.setText("UKRYJ" if w else "POKAŻ")))
                wiersz.addWidget(oko)
            if pole.zmienna == "TELEGRAM_OWNER_ID":
                wykryj = QPushButton("Wykryj moje ID")
                wykryj.setObjectName("boczny")
                wykryj.clicked.connect(self._wykryj_id)
                wiersz.addWidget(wykryj)
            if pole.zmienna == "GOOGLE_CALENDAR_ICAL_URL":
                self._dodaj_wiersz_google(formularz)
            pola[pole.zmienna] = wpis
            if indeks < w_siatce:
                siatka.addLayout(wiersz, indeks // strona.kolumny + 1, indeks % strona.kolumny + 1)
                continue
            etykieta = QLabel(pole.etykieta.upper())
            etykieta.setObjectName("etykieta")
            etykieta.setFont(_czcionka_rozstrzelona(8, 1.5))
            formularz.addRow(etykieta, wiersz)
        if siatka is not None:
            uklad.addLayout(siatka)
        uklad.addLayout(formularz)

        status = self._etykieta("", "status")
        uklad.addWidget(status)
        uklad.addStretch()

        pominiecie = None
        if not wymagana:
            pominiecie = ("Pomiń" if self._pierwsze or not konfiguracja.skonfigurowana(strona.funkcja)
                          else "Bez zmian", self._pomin)
        wylaczenie = None
        if not wymagana and not self._pierwsze and konfiguracja.skonfigurowana(strona.funkcja):
            wylaczenie = ("Wyłącz", self._wylacz)
        uklad.addLayout(self._przyciski(
            wstecz=bool(self._strony), pomin=pominiecie, wylacz=wylaczenie,
            dalej=("Sprawdź i dalej", self._sprawdz)))

        self._stos.addWidget(widget)
        self._strony.append((strona, widget, pola, status))

    def _dodaj_strone_modeli(self):
        widget = QWidget()
        uklad = QVBoxLayout(widget)
        uklad.setContentsMargins(0, 0, 0, 0)
        self._modele = StronaModeli()
        uklad.addWidget(self._modele, 1)
        wiersz = QHBoxLayout()
        wstecz = QPushButton("Wstecz")
        wstecz.clicked.connect(self._wstecz)
        wiersz.addWidget(wstecz)
        wiersz.addStretch()
        dalej = QPushButton("Dalej")
        dalej.setObjectName("glowny")
        dalej.clicked.connect(self._dalej)
        dalej.setEnabled(False)          # dopiero gdy modele będą na dysku
        wiersz.addWidget(dalej)
        uklad.addLayout(wiersz)

        def gotowe():
            dalej.setEnabled(True)
            self._licznik_modeli.setText("MODELE ✓")

        self._modele.gotowe.connect(gotowe)
        self._modele.procent.connect(
            lambda p: self._licznik_modeli.setText(f"MODELE {p}%") if p < 100 else None)
        self._licznik_modeli.setText("MODELE 0%")
        self._licznik_modeli.show()
        self._stos.addWidget(widget)
        self._strony.append((None, widget, {}, None))
        self._modele.start()

    def _sekcja(self, uklad, tytul):
        etykieta = QLabel(tytul)
        etykieta.setObjectName("etykieta")
        etykieta.setFont(_czcionka_rozstrzelona(9, 2))
        uklad.addSpacing(4)
        uklad.addWidget(etykieta)

    def _wybor_modelu(self, trudnosc):
        """Lista modeli Claude Code; model spoza listy (np. wpisany w .env) też się pokaże."""
        wybor = QComboBox()
        for model, nazwa in konfiguracja.MODELE_CLAUDE_CODE:
            wybor.addItem(nazwa, model)
        obecny = konfiguracja.model_claude(trudnosc)
        if wybor.findData(obecny) < 0:
            wybor.addItem(obecny, obecny)
        wybor.setCurrentIndex(wybor.findData(obecny))
        return wybor

    # Etykiety pól na stronie Ogólne — jedna szerokość dla obu sekcji.
    SZEROKOSC_ETYKIET_OGOLNYCH = 150

    def _dodaj_strone_ogolna(self):
        """Tryb roadmapy (roadmapa.py) i modele Claude Code (/kod, roadmapa)."""
        widget = QWidget()
        uklad = QVBoxLayout(widget)
        uklad.setSpacing(10)
        self._naglowek(uklad, "Ogólne")

        self._sekcja(uklad, "ROADMAPA")
        self._roadmapa_wlaczona = QCheckBox("Ciągła praca nad roadmapą — propozycje zadań z MapaZadan.md "
                                            "na Telegramie")
        self._roadmapa_wlaczona.setChecked(konfiguracja.roadmapa_wlaczona())
        uklad.addWidget(self._roadmapa_wlaczona)
        self._raport_roadmapy = QTimeEdit()
        self._raport_roadmapy.setDisplayFormat("HH:mm")
        self._raport_roadmapy.setTime(QTime(*konfiguracja.raport_roadmapy()))
        formularz = QFormLayout()
        formularz.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        etykieta = QLabel("CODZIENNY RAPORT")
        etykieta.setObjectName("etykieta")
        etykieta.setFont(_czcionka_rozstrzelona(8, 1.5))
        etykieta.setMinimumWidth(self.SZEROKOSC_ETYKIET_OGOLNYCH)       # pola obu sekcji w jednej linii
        etykieta.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        formularz.addRow(etykieta, self._raport_roadmapy)
        uklad.addLayout(formularz)
        uwaga = ("" if konfiguracja.skonfigurowana("telegram") else
                 f'<br><span style="color:{BURSZTYN};">Najpierw ustaw Telegram (Klucze API) — '
                 "bez niego roadmapa nie ma jak pytać.</span>")
        uklad.addWidget(self._etykieta(
            "Jarvis proponuje na Telegramie kolejne zadanie z <code>MapaZadan.md</code>; po Twoim "
            "<b>Akceptuj</b> robi je przez Claude Code na gałęzi <b>roadmap</b>. Do <b>main</b> "
            "zmiany trafiają dopiero po komendzie <b>/scal</b>, a Jarvisa nigdy nie restartuje sam. "
            "Potrzebny Git, Claude Code i Jarvis uruchamiany z kodu." + uwaga, "instrukcja", True))

        self._sekcja(uklad, "CLAUDE CODE")
        self._model_trudne = self._wybor_modelu("trudne")
        self._model_latwe = self._wybor_modelu("latwe")
        formularz = QFormLayout()
        formularz.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        for opis, pole in (("ZADANIA TRUDNE", self._model_trudne), ("ZADANIA ŁATWE", self._model_latwe)):
            etykieta = QLabel(opis)
            etykieta.setObjectName("etykieta")
            etykieta.setFont(_czcionka_rozstrzelona(8, 1.5))
            etykieta.setMinimumWidth(self.SZEROKOSC_ETYKIET_OGOLNYCH)
            etykieta.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            formularz.addRow(etykieta, pole)
        uklad.addLayout(formularz)
        uklad.addWidget(self._etykieta(
            "Trudność zadania jest w <code>MapaZadan.md</code>: <code>[Ł]</code> i <code>[Ś]</code> — "
            "model do zadań łatwych, <code>[T]</code> — do trudnych; bez trudności Jarvis zapyta. "
            "Model widać w propozycji na Telegramie "
            "i można go tam zmienić przed akceptacją. <b>/kod</b> zawsze bierze model do zadań trudnych.",
            "instrukcja", True))

        uklad.addStretch()
        uklad.addLayout(self._przyciski(wstecz=True, dalej=("Dalej", self._dalej_z_ogolnych)))
        self._stos.addWidget(widget)
        self._strony.append((None, widget, {}, None))

    def _dalej_z_ogolnych(self):
        self._zbierz_ogolne()
        self._dalej()

    def _zbierz_ogolne(self):
        godzina, minuta = konfiguracja.raport_roadmapy()
        self._zapamietaj({
            konfiguracja.ROADMAPA: ("1" if self._roadmapa_wlaczona.isChecked() else "0",
                                    "1" if konfiguracja.roadmapa_wlaczona() else "0"),
            konfiguracja.ROADMAPA_RAPORT: (self._raport_roadmapy.time().toString("HH:mm"),
                                           f"{godzina:02d}:{minuta:02d}"),
            konfiguracja.CLAUDE_MODEL_TRUDNE: (self._model_trudne.currentData(),
                                               konfiguracja.model_claude("trudne")),
            konfiguracja.CLAUDE_MODEL_LATWE: (self._model_latwe.currentData(),
                                              konfiguracja.model_claude("latwe")),
        })

    def _dodaj_strone_tapety(self):
        """Wybór monitora dla trybu tapety (opis trybu w gui.py i tapeta.py)."""
        widget = QWidget()
        uklad = QVBoxLayout(widget)
        uklad.setSpacing(12)
        self._naglowek(uklad, "Tapeta", "OPCJONALNE")
        uklad.addWidget(self._etykieta(
            "W trybie tapety Jarvis jest żywą tapetą pulpitu: na całym ekranie wybranego "
            "monitora, pod wszystkimi oknami i pod ikonami. Pokazuje reaktor, zegar, "
            "procesor, pamięć, kartę graficzną i ostatnie akcje. Gdy grasz albo oglądasz "
            "film na pełnym ekranie, animacje stają.", "opis"))
        uklad.addWidget(self._etykieta(
            "Włączasz go i wyłączasz w menu ikony Jarvisa przy zegarku: "
            "<b>Tryb tapety</b>. Tutaj wybierasz tylko monitor.", "instrukcja", True))

        self._wybor_monitora = QComboBox()
        self._wybor_monitora.addItem("Główny monitor (domyślnie)", "")
        glowny = QGuiApplication.primaryScreen()
        for ekran in QGuiApplication.screens():
            g = ekran.geometry()
            opis = f"{ekran.name()} — {g.width()}×{g.height()}, {ekran.refreshRate():.0f} Hz"
            if ekran == glowny:
                opis += " (teraz główny)"
            self._wybor_monitora.addItem(opis, ekran.name())
        obecny = self._wybor_monitora.findData(konfiguracja.monitor_tapety())
        self._wybor_monitora.setCurrentIndex(max(0, obecny))

        formularz = QFormLayout()
        etykieta = QLabel("MONITOR")
        etykieta.setObjectName("etykieta")
        etykieta.setFont(_czcionka_rozstrzelona(8, 1.5))
        formularz.addRow(etykieta, self._wybor_monitora)
        uklad.addLayout(formularz)
        uklad.addStretch()
        uklad.addLayout(self._przyciski(wstecz=True, dalej=("Dalej", self._dalej_z_tapety)))
        self._stos.addWidget(widget)
        self._strony.append((None, widget, {}, None))

    def _dalej_z_tapety(self):
        self._zbierz_tapete()
        self._dalej()

    def _zbierz_tapete(self):
        self._zapamietaj({konfiguracja.MONITOR_TAPETY: (self._wybor_monitora.currentData(),
                                                        konfiguracja.monitor_tapety())})

    def _zapamietaj(self, nowe):
        """
        nowe — {ZMIENNA: (wartość w oknie, wartość obecna)}. Do zmian trafia
        tylko to, co naprawdę się różni; powrót do obecnej wartości zmianę cofa.
        """
        for zmienna, (nowa, obecna) in nowe.items():
            if nowa != obecna:
                self._zmiany[zmienna] = nowa
            else:
                self._zmiany.pop(zmienna, None)

    def _zbierz_ustawienia(self):
        """Zmiany ze stron Ogólne, Tapeta, Panel boczny i Nasłuch (są tylko w Ustawieniach)."""
        if self._roadmapa_wlaczona is not None:
            self._zbierz_ogolne()
        if self._wybor_monitora is not None:
            self._zbierz_tapete()
        if self._panel_wlaczony is not None:
            self._zbierz_panel()
        if self._powitanie_wlaczone is not None:
            self._zbierz_nasluch()

    # Opisy elementów panelu na stronie Ustawień (kolejność: konfiguracja.ELEMENTY_PANELU).
    OPISY_ELEMENTOW = {
        "rozmowa": "Rozmowa na żywo — co mówisz Ty i co odpowiada Jarvis",
        "dzien": "Dzień — kalendarz, przypomnienia i pogoda",
        "maile": "Ważne maile — nadawcy i tematy nieprzeczytanych (widać je na ekranie)",
        "spotify": "Spotify — okładka, tytuł i postęp utworu",
        "notatki": "Notatki z dziś",
    }

    def _dodaj_strone_panelu(self):
        """Panel boczny: gdzie (tryb 2 ekranów) i co w nim widać (opis w panel.py)."""
        widget = QWidget()
        uklad = QVBoxLayout(widget)
        uklad.setSpacing(10)
        self._naglowek(uklad, "Panel boczny", "OPCJONALNE")
        uklad.addWidget(self._etykieta(
            "Wąski panel z rozmową na żywo, planem dnia, mailami, muzyką i notatkami.", "opis"))

        self._panel_wlaczony = QCheckBox("Pokazuj panel boczny")
        self._panel_wlaczony.setChecked(konfiguracja.panel_wlaczony())
        uklad.addWidget(self._panel_wlaczony)

        # Tryb 2 ekranów ma sens dopiero przy dwóch monitorach — przy jednym
        # nawet go nie pokazujemy (panel wysuwa się wtedy z tapety).
        self._panel_2_ekrany = self._panel_monitor = self._panel_krawedz = None
        ekrany = QGuiApplication.screens()
        if len(ekrany) >= 2:
            self._panel_2_ekrany = QCheckBox("Tryb 2 ekranów — pasek przy krawędzi drugiego monitora")
            self._panel_2_ekrany.setChecked(konfiguracja.panel_2_ekrany())
            uklad.addWidget(self._panel_2_ekrany)
            self._panel_monitor = QComboBox()
            self._panel_monitor.addItem("Drugi monitor (nie główny)", "")
            for ekran in ekrany:
                g = ekran.geometry()
                self._panel_monitor.addItem(f"{ekran.name()} — {g.width()}×{g.height()}", ekran.name())
            self._panel_monitor.setCurrentIndex(max(0, self._panel_monitor.findData(konfiguracja.panel_monitor())))
            self._panel_krawedz = QComboBox()
            self._panel_krawedz.addItem("Prawa krawędź", "prawa")
            self._panel_krawedz.addItem("Lewa krawędź", "lewa")
            self._panel_krawedz.setCurrentIndex(self._panel_krawedz.findData(konfiguracja.panel_krawedz()))
            wiersz = QHBoxLayout()
            wiersz.addWidget(self._panel_monitor, 2)
            wiersz.addWidget(self._panel_krawedz, 1)
            uklad.addLayout(wiersz)
            uklad.addWidget(self._etykieta(
                "Odłączysz drugi monitor — panel sam przejdzie na tapetę, a po podłączeniu wróci.",
                "instrukcja"))
        else:
            uklad.addWidget(self._etykieta(
                "Masz teraz jeden monitor, więc panel wysuwa się z prawej krawędzi tapety: "
                "sam w trakcie rozmowy albo z menu przy zegarku (<b>Wysuń panel boczny</b>). "
                "Tryb 2 ekranów pojawi się tutaj po podłączeniu drugiego monitora.",
                "instrukcja", True))

        self._panel_elementy = {}
        wlaczone = konfiguracja.panel_elementy()
        for element in konfiguracja.ELEMENTY_PANELU:
            pole = QCheckBox(self.OPISY_ELEMENTOW[element])
            pole.setChecked(element in wlaczone)
            uklad.addWidget(pole)
            self._panel_elementy[element] = pole

        formularz = QFormLayout()
        etykieta = QLabel("MIASTO (POGODA)")
        etykieta.setObjectName("etykieta")
        etykieta.setFont(_czcionka_rozstrzelona(8, 1.5))
        self._panel_miasto = QLineEdit(konfiguracja.pogoda_miasto())
        self._panel_miasto.setPlaceholderText("np. Kraków — pogodę podaje darmowy serwis Open-Meteo")
        formularz.addRow(etykieta, self._panel_miasto)
        uklad.addLayout(formularz)

        uklad.addStretch()
        uklad.addLayout(self._przyciski(wstecz=True, dalej=("Dalej", self._dalej_z_panelu)))
        self._stos.addWidget(widget)
        self._strony.append((None, widget, {}, None))

    def _dalej_z_panelu(self):
        self._zbierz_panel()
        self._dalej()

    def _zbierz_panel(self):
        elementy = [e for e, pole in self._panel_elementy.items() if pole.isChecked()]
        nowe = {
            konfiguracja.PANEL: ("1" if self._panel_wlaczony.isChecked() else "0",
                                 "1" if konfiguracja.panel_wlaczony() else "0"),
            # "brak" zamiast pustej wartości — pusta znaczy "domyślne" (konfiguracja.py).
            konfiguracja.PANEL_ELEMENTY: (",".join(elementy) or "brak",
                                          ",".join(konfiguracja.panel_elementy()) or "brak"),
            konfiguracja.POGODA_MIASTO: (self._panel_miasto.text().strip(), konfiguracja.pogoda_miasto()),
        }
        if self._panel_2_ekrany is not None:
            nowe[konfiguracja.PANEL_2_EKRANY] = ("1" if self._panel_2_ekrany.isChecked() else "0",
                                                 "1" if konfiguracja.panel_2_ekrany() else "0")
            nowe[konfiguracja.PANEL_MONITOR] = (self._panel_monitor.currentData(), konfiguracja.panel_monitor())
            nowe[konfiguracja.PANEL_KRAWEDZ] = (self._panel_krawedz.currentData(), konfiguracja.panel_krawedz())
        self._zapamietaj(nowe)

    def _dodaj_strone_nasluchu(self):
        """
        Powitanie po starcie (powitanie_startowe.py) i słowo aktywujące
        (slowo_aktywujace.py): własny model i próg czułości.
        """
        import slowo_aktywujace

        widget = QWidget()
        uklad = QVBoxLayout(widget)
        uklad.setSpacing(10)
        self._naglowek(uklad, "Nasłuch")

        self._powitanie_wlaczone = QCheckBox("Po uruchomieniu czekaj na powitanie — bez „Hey Jarvis”")
        self._powitanie_wlaczone.setChecked(konfiguracja.powitanie_po_starcie())
        uklad.addWidget(self._powitanie_wlaczone)
        uklad.addWidget(self._etykieta(
            "Przez kilka minut po starcie wystarczy powiedzieć „hej, wróciłem”, „cześć Jarvis” albo "
            "„jestem”: Jarvis przywita Cię z najważniejszymi informacjami i przejdzie w zwykły tryb "
            "z „Hey Jarvis”. Inne rozmowy w tym czasie pomija i nie zapisuje ich treści. "
            "Zmiana działa od następnego uruchomienia.", "instrukcja"))

        self._okno_powitania = QSpinBox()
        self._okno_powitania.setRange(1, konfiguracja.MAKS_OKNO_POWITANIA_MIN)
        self._okno_powitania.setSuffix(" min")
        self._okno_powitania.setValue(konfiguracja.okno_powitania_min())
        self._okno_powitania.setEnabled(self._powitanie_wlaczone.isChecked())
        self._powitanie_wlaczone.toggled.connect(self._okno_powitania.setEnabled)

        # Próg: 0 w polu = "automatycznie", czyli domyślny dla modelu.
        wlasny = slowo_aktywujace.wlasny_model() is not None
        domyslny = slowo_aktywujace.PROG_WLASNEGO if wlasny else slowo_aktywujace.PROG_WBUDOWANEGO
        self._prog_wake_worda = QDoubleSpinBox()
        self._prog_wake_worda.setRange(0.0, slowo_aktywujace.PROG_MAKS)
        self._prog_wake_worda.setSingleStep(0.05)
        self._prog_wake_worda.setDecimals(2)
        self._prog_wake_worda.setSpecialValueText(f"automatycznie ({domyslny:.2f})")
        self._prog_wake_worda.setValue(konfiguracja.prog_wake_worda() or 0.0)

        formularz = QFormLayout()
        formularz.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        for opis, pole in (("CZAS NA POWITANIE", self._okno_powitania),
                           ("PRÓG CZUŁOŚCI", self._prog_wake_worda)):
            etykieta = QLabel(opis)
            etykieta.setObjectName("etykieta")
            etykieta.setFont(_czcionka_rozstrzelona(8, 1.5))
            formularz.addRow(etykieta, pole)
        uklad.addLayout(formularz)

        folder = os.path.dirname(os.path.dirname(slowo_aktywujace.PLIK_WLASNEGO_MODELU))
        model = ("<b>Słowo aktywujące:</b> własny model <code>models\\jarvis.onnx</code>. "
                 "Po podmianie pliku uruchom Jarvisa ponownie." if wlasny else
                 "<b>Słowo aktywujące:</b> wbudowane „Hey Jarvis”. Własny model zapisz jako "
                 f"<code>models\\jarvis.onnx</code> w <code>{folder}</code> i uruchom Jarvisa ponownie.")
        uklad.addWidget(self._etykieta(
            model + "<br><b>Próg czułości:</b> niższy — łatwiej zawołać Jarvisa, ale częściej "
            "odezwie się sam; wyższy — rzadziej się myli, ale trzeba mówić wyraźniej. "
            "Zmiana progu działa od razu.", "instrukcja", True))

        uklad.addStretch()
        uklad.addLayout(self._przyciski(wstecz=True, dalej=("Dalej", self._dalej_z_nasluchu)))
        self._stos.addWidget(widget)
        self._strony.append((None, widget, {}, None))

    def _dalej_z_nasluchu(self):
        self._zbierz_nasluch()
        self._dalej()

    def _zbierz_nasluch(self):
        prog = self._prog_wake_worda.value()
        obecny = konfiguracja.prog_wake_worda()
        self._zapamietaj({
            konfiguracja.POWITANIE_PO_STARCIE: ("1" if self._powitanie_wlaczone.isChecked() else "0",
                                                "1" if konfiguracja.powitanie_po_starcie() else "0"),
            konfiguracja.OKNO_POWITANIA: (str(self._okno_powitania.value()),
                                          str(konfiguracja.okno_powitania_min())),
            # Pusta wartość = automatycznie (konfiguracja.prog_wake_worda).
            konfiguracja.PROG_WAKE_WORDA: (f"{prog:.2f}" if prog > 0 else "",
                                           f"{obecny:.2f}" if obecny is not None else ""),
        })

    def _dodaj_podsumowanie(self):
        widget = QWidget()
        uklad = QVBoxLayout(widget)
        uklad.setSpacing(14)
        self._naglowek(uklad, "Gotowe." if self._pierwsze else "Podsumowanie")
        self._podsumowanie = self._etykieta("", "opis", True)
        uklad.addWidget(self._podsumowanie)
        sciezka = self._etykieta(
            f"Zapiszę w <nobr><code>{SCIEZKA_DO_POKAZANIA}</code></nobr>. Twój plik .env w folderze "
            "programu zostaje nietknięty — jeśli go masz, dalej działa, a te ustawienia "
            "mają przed nim pierwszeństwo.", "instrukcja", True)
        sciezka.setToolTip(konfiguracja.PLIK)
        uklad.addWidget(sciezka)
        uklad.addStretch()
        uklad.addLayout(self._przyciski(
            wstecz=True, dalej=("Uruchom Jarvisa" if self._pierwsze else "Zapisz", self._zakoncz)))
        self._stos.addWidget(widget)
        self._strony.append((None, widget, {}, None))

    def _przyciski(self, wstecz=False, pomin=None, wylacz=None, dalej=None):
        wiersz = QHBoxLayout()
        if wstecz:
            przycisk = QPushButton("Wstecz")
            przycisk.clicked.connect(self._wstecz)
            wiersz.addWidget(przycisk)
        wiersz.addStretch()
        for opis, nazwa in ((wylacz, "boczny"), (pomin, "boczny"), (dalej, "glowny")):
            if opis:
                przycisk = QPushButton(opis[0])
                przycisk.setObjectName(nazwa)
                przycisk.clicked.connect(opis[1])
                wiersz.addWidget(przycisk)
        return wiersz

    # --- nawigacja ---

    def _biezaca(self):
        return self._strony[self._stos.currentIndex()]

    def _odswiez_krok(self):
        numer = self._stos.currentIndex() + 1
        self._krok.setText(f"KROK {numer} / {self._stos.count()}")
        if self._stos.currentIndex() == self._stos.count() - 1:
            self._odswiez_podsumowanie()

    def _dalej(self):
        self._stop_wykrywania.set()
        self._stos.setCurrentIndex(min(self._stos.currentIndex() + 1, self._stos.count() - 1))
        self._odswiez_krok()

    def _wstecz(self):
        self._stop_wykrywania.set()
        self._stos.setCurrentIndex(max(self._stos.currentIndex() - 1, 0))
        self._odswiez_krok()

    def _pomin(self):
        strona = self._biezaca()[0]
        self._wyniki[strona.funkcja] = "pominieta"
        for zmienna in list(self._zmiany):
            if zmienna in {p.zmienna for p in strona.pola} | set(strona.dodatkowe):
                del self._zmiany[zmienna]
        self._dalej()

    def _wylacz(self):
        strona = self._biezaca()[0]
        for pole in strona.pola:
            self._zmiany[pole.zmienna] = ""
        self._zmiany.update(strona.dodatkowe)
        self._wyniki[strona.funkcja] = "wylaczona"
        if strona.funkcja == "kalendarz":
            self._rozlacz_google = True
        self._dalej()

    def _status(self, status, tekst, kolor):
        status.setText(tekst)
        status.setStyleSheet(f"color: {kolor};")

    def _zablokuj(self, widget, zablokuj):
        for przycisk in widget.findChildren(QPushButton):
            if przycisk.objectName() != "oko":
                przycisk.setEnabled(not zablokuj)

    # --- sprawdzanie ---

    def _sprawdz(self):
        strona, widget, pola, status = self._biezaca()
        wartosci = {}
        for zmienna, wpis in pola.items():
            tekst = (wpis.currentData() if isinstance(wpis, QComboBox) else wpis.text()).strip()
            wartosci[zmienna] = strona.popraw.get(zmienna, lambda s: s)(tekst)
        puste = [p.etykieta for p in strona.pola if p.wymagane and not wartosci[p.zmienna]]
        if puste:
            self._status(status, f"Uzupełnij: {', '.join(puste)}.", CZERWONY)
            return

        self._status(status, "Sprawdzam…", BURSZTYN)
        self._zablokuj(widget, True)
        indeks = self._stos.currentIndex()

        def praca():
            try:
                wynik = strona.sprawdz(wartosci)
            except Exception:
                logger.exception("Kreator: sprawdzanie %s zawiodło", strona.funkcja)
                wynik = (False, "Nieoczekiwany błąd — szczegóły są w dzienniku Jarvisa.")
            self._sygnaly.sprawdzono.emit((indeks, wartosci, wynik))

        threading.Thread(target=praca, name="kreator-sprawdzanie", daemon=True).start()

    def _po_sprawdzeniu(self, dane):
        indeks, wartosci, (ok, komunikat) = dane
        strona, widget, pola, status = self._strony[indeks]
        self._zablokuj(widget, False)
        logger.info("Kreator: %s — %s", strona.funkcja, "działa" if ok else "nie działa")
        if not ok:
            self._status(status, komunikat, CZERWONY)
            return
        self._status(status, "✓ " + komunikat, CYJAN)
        self._zmiany.update(wartosci)
        self._zmiany.update(strona.dodatkowe)
        self._wyniki[strona.funkcja] = "ok"
        # Chwila, żeby zdążyć przeczytać "działa", potem dalej.
        QTimer.singleShot(1200, lambda: self._stos.currentIndex() == indeks and self._dalej())

    def _wykryj_id(self):
        strona, widget, pola, status = self._biezaca()
        token = pola["TELEGRAM_JARVIS_TOKEN"].text().strip()
        if not token:
            self._status(status, "Najpierw wklej token bota.", CZERWONY)
            return
        self._stop_wykrywania = threading.Event()
        stop = self._stop_wykrywania
        self._status(status, "Napisz teraz cokolwiek do swojego bota w Telegramie — czekam do 2 minut…",
                     BURSZTYN)

        def praca():
            wynik = wykryj_wlasciciela(token, stop)
            if not stop.is_set():
                self._sygnaly.wykryto.emit(wynik)

        threading.Thread(target=praca, name="kreator-telegram", daemon=True).start()

    def _dodaj_wiersz_google(self, formularz):
        """Stan połączenia z Google Calendar API i przycisk "Połącz z Google"."""
        import google_kalendarz

        wiersz = QHBoxLayout()
        self._stan_google = QLabel(google_kalendarz.stan())
        self._stan_google.setObjectName("opis")
        wiersz.addWidget(self._stan_google, 1)
        polacz = QPushButton("Połącz z Google")
        polacz.setObjectName("boczny")
        polacz.clicked.connect(self._polacz_google)
        wiersz.addWidget(polacz)
        etykieta = QLabel("GOOGLE CALENDAR API")
        etykieta.setObjectName("etykieta")
        etykieta.setFont(_czcionka_rozstrzelona(8, 1.5))
        formularz.addRow(etykieta, wiersz)

    def _polacz_google(self):
        """Logowanie OAuth w przeglądarce — tylko na Twoje kliknięcie (google_kalendarz.py)."""
        import google_kalendarz

        strona, widget, pola, status = self._biezaca()
        self._status(status, "Otwieram przeglądarkę — zaloguj się w Google i zezwól na dostęp "
                             "do kalendarza (czekam do 3 minut)…", BURSZTYN)
        self._zablokuj(widget, True)
        threading.Thread(target=lambda: self._sygnaly.google.emit(google_kalendarz.autoryzuj()),
                         name="kreator-google", daemon=True).start()

    def _po_google(self, wynik):
        import google_kalendarz

        ok, komunikat = wynik
        strona, widget, pola, status = self._strony[self._indeks_strony["kalendarz"]]
        self._zablokuj(widget, False)
        self._stan_google.setText(google_kalendarz.stan())
        self._status(status, ("✓ " if ok else "") + komunikat + (" Teraz „Sprawdź i dalej”." if ok else ""),
                     CYJAN if ok else CZERWONY)
        if ok:
            self._rozlacz_google = False

    def _po_wykryciu(self, wynik):
        identyfikator, komunikat = wynik
        strona, widget, pola, status = self._biezaca()
        if "TELEGRAM_OWNER_ID" not in pola:
            return
        if identyfikator:
            pola["TELEGRAM_OWNER_ID"].setText(str(identyfikator))
            self._status(status, f"✓ {komunikat} Teraz „Sprawdź i dalej”.", CYJAN)
        else:
            self._status(status, komunikat, CZERWONY)

    # --- koniec ---

    def _odswiez_podsumowanie(self):
        self._zbierz_ustawienia()
        wiersze = []
        for strona in STRONY:
            wynik = self._wyniki.get(strona.funkcja)
            if wynik == "ok":
                opis, kolor = "sprawdzone, działa", CYJAN
            elif wynik == "wylaczona":
                opis, kolor = "wyłączone", BURSZTYN
            elif konfiguracja.skonfigurowana(strona.funkcja):
                opis, kolor = "bez zmian", NIEBIESKI
            elif konfiguracja.FUNKCJE[strona.funkcja].wymagana:
                opis, kolor = "BRAK — wróć i wpisz klucz", CZERWONY
            else:
                opis, kolor = "pominięte — funkcja wyłączona", "#6f8f9f"
            wiersze.append(f'<span style="color:{kolor};">■</span>&nbsp; <b>{strona.tytul}</b> — {opis}')
        if self._modele is not None:
            opis, kolor = (("pobrane", CYJAN) if self._modele.skonczone
                           else ("jeszcze się pobierają — wróć o krok", BURSZTYN))
            wiersze.append(f'<span style="color:{kolor};">■</span>&nbsp; <b>Modele mowy</b> — {opis}')
        if self._roadmapa_wlaczona is not None:
            opis = (f"roadmapa włączona, raport o {self._raport_roadmapy.time().toString('HH:mm')}"
                    if self._roadmapa_wlaczona.isChecked() else "roadmapa wyłączona")
            opis += (f"; Claude Code: {self._model_trudne.currentText()} (trudne), "
                     f"{self._model_latwe.currentText()} (łatwe)")
            zmiana = bool({konfiguracja.ROADMAPA, konfiguracja.ROADMAPA_RAPORT, konfiguracja.CLAUDE_MODEL_TRUDNE,
                           konfiguracja.CLAUDE_MODEL_LATWE} & set(self._zmiany))
            wiersze.append(f'<span style="color:{CYJAN if zmiana else NIEBIESKI};">■</span>&nbsp; '
                           f'<b>Ogólne</b> — {opis}{"" if zmiana else " (bez zmian)"}')
        if self._wybor_monitora is not None:
            opis = self._wybor_monitora.currentText().split(" — ")[0]
            zmiana = konfiguracja.MONITOR_TAPETY in self._zmiany
            wiersze.append(f'<span style="color:{CYJAN if zmiana else NIEBIESKI};">■</span>&nbsp; '
                           f'<b>Tapeta</b> — {opis}{"" if zmiana else " (bez zmian)"}')
        if self._panel_wlaczony is not None:
            if not self._panel_wlaczony.isChecked():
                opis = "wyłączony"
            elif self._panel_2_ekrany is not None and self._panel_2_ekrany.isChecked():
                opis = (f"pasek: {self._panel_monitor.currentText().split(' — ')[0]}, "
                        f"{self._panel_krawedz.currentText().lower()}")
            else:
                opis = "wysuwany z tapety"
            zmiana = any(z.startswith("JARVIS_PANEL") or z == konfiguracja.POGODA_MIASTO
                         for z in self._zmiany)
            wiersze.append(f'<span style="color:{CYJAN if zmiana else NIEBIESKI};">■</span>&nbsp; '
                           f'<b>Panel boczny</b> — {opis}{"" if zmiana else " (bez zmian)"}')
        if self._powitanie_wlaczone is not None:
            opis = (f"powitanie po starcie przez {self._okno_powitania.value()} min"
                    if self._powitanie_wlaczone.isChecked() else "bez powitania po starcie")
            opis += f", próg {self._prog_wake_worda.text()}"
            zmiana = bool({konfiguracja.POWITANIE_PO_STARCIE, konfiguracja.OKNO_POWITANIA,
                           konfiguracja.PROG_WAKE_WORDA} & set(self._zmiany))
            wiersze.append(f'<span style="color:{CYJAN if zmiana else NIEBIESKI};">■</span>&nbsp; '
                           f'<b>Nasłuch</b> — {opis}{"" if zmiana else " (bez zmian)"}')
        self._podsumowanie.setText("<br>".join(wiersze))

    def _ma_anthropic(self):
        return bool(self._zmiany.get("ANTHROPIC_API_KEY")
                    or (konfiguracja.skonfigurowana("anthropic")
                        and self._zmiany.get("ANTHROPIC_API_KEY", None) != ""))

    def _zakoncz(self):
        # Kreator pierwszego uruchomienia wymaga klucza; w Ustawieniach można
        # zapisać resztę bez niego — Jarvis działa, tylko nie rozmawia, a na
        # HUD-zie świeci się czerwona linijka "Klucze API".
        if self._pierwsze and not self._ma_anthropic():
            QMessageBox.warning(self, "Brakuje klucza",
                                "Bez klucza Anthropic Jarvis nie rozmawia — wróć do tej strony.")
            return
        if self._modele is not None and not self._modele.skonczone:
            QMessageBox.information(self, "Jeszcze chwila",
                                    "Modele mowy jeszcze się pobierają — bez nich Jarvis "
                                    "nie usłyszy „Hey Jarvis”. Postęp widać o krok wcześniej.")
            return
        self._zbierz_ustawienia()
        konfiguracja.zapisz(self._zmiany)
        if self._rozlacz_google and os.path.exists(konfiguracja.PLIK_GOOGLE_TOKENU):
            os.remove(konfiguracja.PLIK_GOOGLE_TOKENU)      # własny token Jarvisa, nie Twoje dane
        self.accept()

    def done(self, wynik):
        self._stop_wykrywania.set()
        if self._modele is not None:
            self._modele.zatrzymaj()
        super().done(wynik)


class OknoModeli(QDialog):
    """
    Samo pobieranie modeli — gdy klucze są już wpisane (np. przeniesione
    Ustawienia albo usunięty folder modeli), a modeli brakuje.
    """

    def __init__(self, rodzic=None):
        super().__init__(rodzic)
        self.setObjectName("kreator")
        self.setStyleSheet(STYL)
        self.setWindowTitle("Jarvis — pobieranie modeli mowy")
        if os.path.exists(sciezki.zasob("jarvis_icon.png")):
            self.setWindowIcon(QIcon(sciezki.zasob("jarvis_icon.png")))
        self.setFixedSize(760, 460)
        uklad = QVBoxLayout(self)
        uklad.setContentsMargins(34, 24, 34, 24)
        gora = QHBoxLayout()
        gora.addWidget(_Pierscien())
        marka = QLabel("J.A.R.V.I.S.  //  MODELE MOWY")
        marka.setObjectName("marka")
        marka.setFont(_czcionka_rozstrzelona(10, 3))
        gora.addWidget(marka)
        gora.addStretch()
        uklad.addLayout(gora)
        linia = QFrame()
        linia.setObjectName("linia")
        uklad.addWidget(linia)
        self._strona = StronaModeli()
        uklad.addWidget(self._strona, 1)
        wiersz = QHBoxLayout()
        wiersz.addStretch()
        self._dalej = QPushButton("Uruchom Jarvisa")
        self._dalej.setObjectName("glowny")
        self._dalej.setEnabled(False)
        self._dalej.clicked.connect(self.accept)
        wiersz.addWidget(self._dalej)
        uklad.addLayout(wiersz)
        self._strona.gotowe.connect(lambda: self._dalej.setEnabled(True))
        self._strona.start()

    def done(self, wynik):
        self._strona.zatrzymaj()
        super().done(wynik)


def pobierz_modele(rodzic=None):
    """Pokazuje okno pobierania modeli. Zwraca True, gdy modele są już na dysku."""
    return OknoModeli(rodzic).exec() == QDialog.Accepted


def uruchom_kreator(pierwsze=False, rodzic=None, strona=None):
    """
    Pokazuje kreator (pierwsze=True) albo okno Ustawień i czeka na zamknięcie.
    Wymaga istniejącego QApplication (main.py go ma).

    strona="klucze" — Ustawienia od razu na zakładce Klucze API (linijka
    "Klucze API: brak N" na HUD-zie, main.py).

    Zwraca: słownik zapisanych zmian {"ZMIENNA": wartość} — pusty, gdy
    zamknięto okno bez zapisu albo niczego nie zmieniono. main.py patrzy
    po nazwach, czy potrzebny jest restart (konfiguracja.BEZ_RESTARTU).
    """
    okno = KreatorJarvisa(pierwsze=pierwsze, rodzic=rodzic, strona=strona)
    if okno.exec() != QDialog.Accepted:
        return {}
    return dict(okno._zmiany)


def zapytaj_o_restart(rodzic=None):
    """Po zapisaniu Ustawień: czy uruchomić Jarvisa ponownie teraz."""
    pytanie = QMessageBox(rodzic)
    pytanie.setStyleSheet(STYL)
    pytanie.setWindowTitle("Jarvis — ustawienia zapisane")
    pytanie.setText("Zapisane. Telegram, Spotify i czujki poczty przełączą się "
                    "dopiero po ponownym uruchomieniu Jarvisa.\n\nUruchomić ponownie teraz?")
    teraz = pytanie.addButton("Uruchom ponownie", QMessageBox.AcceptRole)
    pytanie.addButton("Później", QMessageBox.RejectRole)
    pytanie.exec()
    return pytanie.clickedButton() is teraz


# --- Podgląd: `python setup_wizard.py` (okno Ustawień), `--pierwsze` (kreator) ---
if __name__ == "__main__":
    from logging_setup import skonfiguruj_logowanie

    skonfiguruj_logowanie()
    konfiguracja.wczytaj()
    app = QApplication(sys.argv)
    print("Zapisano." if uruchom_kreator(pierwsze="--pierwsze" in sys.argv) else "Bez zmian.")
