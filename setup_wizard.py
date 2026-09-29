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
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QDialog, QFormLayout, QFrame, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
                               QStackedWidget, QVBoxLayout, QWidget)

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


def sprawdz_gmail(w):
    """Logowanie IMAP tylko do odczytu — dokładnie tak, jak robi to email_monitor.py."""
    adres, haslo = w["GMAIL_ADDRESS"], w["GMAIL_APP_PASSWORD"]
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", adres):
        return False, "To nie wygląda na adres e-mail."
    if len(haslo) != 16:
        return False, ("Hasło aplikacji ma 16 liter (Google pokazuje je w czterech grupach "
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
        return False, "Nie mogę połączyć się z Gmailem — sprawdź internet."
    return True, ("Działa — skrzynka otwarta tylko do odczytu. Google może przysłać maila "
                  "o nowym logowaniu; to normalne.")


def sprawdz_kalendarz(w):
    """Pobiera plik kalendarza — adresu nigdy nie pokazujemy w komunikatach."""
    adres = w["GOOGLE_CALENDAR_ICAL_URL"]
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


@dataclass
class Strona:
    funkcja: str                 # klucz z konfiguracja.FUNKCJE
    tytul: str
    opis: str
    instrukcja: str              # HTML z linkami
    pola: list
    sprawdz: object              # funkcja(wartości) -> (ok, komunikat)
    popraw: dict = field(default_factory=dict)   # zmienna -> funkcja czyszcząca wpis


def _link(adres, tekst):
    return f'<a href="{adres}" style="color:#ffba40; text-decoration:none;">{tekst} ↗</a>'


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
        "Szukanie maili („czy przyszło coś od Allegro?”) i czujki na nowe maile "
        "z powiadomieniem na Telegram. Skrzynka tylko do odczytu.",
        "1. Na koncie Google włącz weryfikację dwuetapową (bez niej hasła aplikacji są niedostępne).<br>"
        f"2. Otwórz {_link('https://myaccount.google.com/apppasswords', 'Hasła aplikacji')}, "
        "utwórz hasło o nazwie „Jarvis”.<br>"
        "3. Wklej 16-literowe hasło aplikacji — <b>nie</b> zwykłe hasło do konta.",
        [Pole("GMAIL_ADDRESS", "Adres Gmail", tajne=False, podpowiedz="ty@gmail.com"),
         Pole("GMAIL_APP_PASSWORD", "Hasło aplikacji", podpowiedz="xxxx xxxx xxxx xxxx")],
        sprawdz_gmail,
        popraw={"GMAIL_APP_PASSWORD": lambda s: s.replace(" ", "")}),
    Strona(
        "kalendarz", "Kalendarz Google",
        "„Co mam jutro?”, „co mam w piątek?” — i dzisiejsze wydarzenia w porannym "
        "briefingu. Tylko do odczytu.",
        f"1. Otwórz {_link('https://calendar.google.com/calendar/r/settings', 'ustawienia Kalendarza Google')}.<br>"
        "2. Po lewej wybierz swój kalendarz → <b>Integracja kalendarza</b>.<br>"
        "3. Skopiuj <b>Tajny adres w formacie iCal</b>. Traktuj go jak hasło — "
        "gdyby wyciekł, zresetujesz go w tym samym miejscu.",
        [Pole("GOOGLE_CALENDAR_ICAL_URL", "Tajny adres iCal",
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
QPushButton {{
    background: transparent; color: {CYJAN}; border: 1px solid {NIEBIESKI};
    border-radius: 3px; padding: 8px 18px; font-family: Bahnschrift; font-size: 13px;
}}
QPushButton:hover {{ background: rgba(86, 214, 255, 0.12); }}
QPushButton#glowny {{ background: rgba(86, 214, 255, 0.20); color: {BIALY};
                      border: 1px solid {CYJAN}; }}
QPushButton#glowny:hover {{ background: rgba(86, 214, 255, 0.32); }}
QPushButton#boczny {{ color: {BURSZTYN}; border-color: #9c7330; }}
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

    def __init__(self, pierwsze=False, rodzic=None):
        super().__init__(rodzic)
        self._pierwsze = pierwsze
        self._zmiany = {}
        self._wyniki = {}          # funkcja -> "ok" / "pominieta" / "wylaczona"
        self._stop_wykrywania = threading.Event()
        self._sygnaly = _Sygnaly()
        self._sygnaly.sprawdzono.connect(self._po_sprawdzeniu)
        self._sygnaly.wykryto.connect(self._po_wykryciu)

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

        self._stos = QStackedWidget()
        uklad.addWidget(self._stos, 1)

        self._strony = []          # (Strona albo None, widget, słownik pól, status)
        self._modele = None
        if pierwsze:
            self._dodaj_powitanie()
        for strona in STRONY:
            self._dodaj_strone(strona)
        # Wersja z instalatora bez pobranych modeli: strona pobierania przed
        # podsumowaniem, a samo pobieranie rusza od razu, w tle.
        if modele.potrzebne_pobranie():
            self._dodaj_strone_modeli()
        self._dodaj_podsumowanie()

        self._stos.setCurrentIndex(0)
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
        pola = {}
        for pole in strona.pola:
            wpis = QLineEdit(konfiguracja.wartosc(pole.zmienna)
                             or konfiguracja.DOMYSLNE.get(pole.zmienna, ""))
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
            etykieta = QLabel(pole.etykieta.upper())
            etykieta.setObjectName("etykieta")
            etykieta.setFont(_czcionka_rozstrzelona(8, 1.5))
            formularz.addRow(etykieta, wiersz)
            pola[pole.zmienna] = wpis
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
            if zmienna in {p.zmienna for p in strona.pola}:
                del self._zmiany[zmienna]
        self._dalej()

    def _wylacz(self):
        strona = self._biezaca()[0]
        for pole in strona.pola:
            self._zmiany[pole.zmienna] = ""
        self._wyniki[strona.funkcja] = "wylaczona"
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
            tekst = wpis.text().strip()
            wartosci[zmienna] = strona.popraw.get(zmienna, lambda s: s)(tekst)
        puste = [p.etykieta for p in strona.pola if not wartosci[p.zmienna]]
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
        self._podsumowanie.setText("<br>".join(wiersze))

    def _ma_anthropic(self):
        return bool(self._zmiany.get("ANTHROPIC_API_KEY")
                    or (konfiguracja.skonfigurowana("anthropic")
                        and self._zmiany.get("ANTHROPIC_API_KEY", None) != ""))

    def _zakoncz(self):
        if not self._ma_anthropic():
            QMessageBox.warning(self, "Brakuje klucza",
                                "Bez klucza Anthropic Jarvis nie wystartuje — wróć do tej strony.")
            return
        if self._modele is not None and not self._modele.skonczone:
            QMessageBox.information(self, "Jeszcze chwila",
                                    "Modele mowy jeszcze się pobierają — bez nich Jarvis "
                                    "nie usłyszy „Hey Jarvis”. Postęp widać o krok wcześniej.")
            return
        konfiguracja.zapisz(self._zmiany)
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


def uruchom_kreator(pierwsze=False, rodzic=None):
    """
    Pokazuje kreator (pierwsze=True) albo okno Ustawień i czeka na zamknięcie.
    Wymaga istniejącego QApplication (main.py go ma).

    Zwraca: True, jeśli zapisano ustawienia.
    """
    okno = KreatorJarvisa(pierwsze=pierwsze, rodzic=rodzic)
    return okno.exec() == QDialog.Accepted


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
