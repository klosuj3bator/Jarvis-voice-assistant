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


# Zmienne z sekretami: klucze API, tokeny, hasła, tajny adres kalendarza.
# Po końcówce nazwy, żeby objąć też konta Gmail 1-3 bez wyliczania.
def _tajna(zmienna):
    return (zmienna.endswith(("_API_KEY", "_SECRET", "_TOKEN", "_PASSWORD"))
            or zmienna in ("SPOTIPY_CLIENT_ID", "GOOGLE_CALENDAR_ICAL_URL"))


def tajne_wartosci():
    """
    {nazwa: wartość} sekretów Jarvisa — do sprawdzania, czy żaden nie trafia
    do commita (zdalny_kod.py). Krótsze niż 8 znaków pomijamy: takie
    "trafienia" byłyby przypadkowe.
    """
    return {z: w for z, w in os.environ.items() if _tajna(z) and len(w.strip()) >= 8}


def srodowisko_bez_kluczy(tylko_z_plikow=False):
    """
    Kopia os.environ dla programu, który uruchamia Jarvis.

    tylko_z_plikow=False — bez ŻADNYCH kluczy Jarvisa. Dla Claude Code
        (zdalny_kod.py): nie potrzebuje ich, a z ANTHROPIC_API_KEY płaciłby
        kluczem Jarvisa zamiast z Twojej subskrypcji claude.ai.
    tylko_z_plikow=True — bez tego, co wczytaj() dołożyło z config.env i .env.
        Dla nowej kopii Jarvisa (restart): przeczyta je sama, jak przy zwykłym
        starcie. Inaczej uznałaby je za zmienne Windows, a te wygrywają
        z Ustawieniami — zmiana klucza w Ustawieniach przestałaby działać.
    """
    srodowisko = dict(os.environ)
    for zmienna, skad in _zrodla.items():
        if skad in ("ustawienia", ".env"):
            srodowisko.pop(zmienna, None)
    if not tylko_z_plikow:
        for zmienna in list(srodowisko):
            if _tajna(zmienna) or zmienna.startswith(("ANTHROPIC_", "TELEGRAM_", "GMAIL_",
                                                       "SPOTIPY_", "GOOGLE_CALENDAR")):
                del srodowisko[zmienna]
    return srodowisko


def zrodlo(zmienna):
    """Skąd pochodzi obecna wartość: "ustawienia", ".env", "środowisko" albo None."""
    return _zrodla.get(zmienna) if wartosc(zmienna) else None


def skonfigurowana(funkcja):
    """Czy funkcja ma komplet niepustych kluczy."""
    if funkcja == "gmail":
        return bool(konta_gmail())          # wystarczy jedno konto (opis niżej)
    return all(wartosc(z).strip() for z in FUNKCJE[funkcja].zmienne)


# ---------------------------------------------------------------
# Konta Gmail — do trzech (opis w email_monitor.py, KILKA KONT)
# ---------------------------------------------------------------
#
#   GMAIL_1_NAME=prywatny             nazwy WŁASNE, po przecinku (opcjonalnie)
#   GMAIL_1_ADDRESS=ty@gmail.com
#   GMAIL_1_APP_PASSWORD=...          hasło aplikacji, nie hasło konta
#   ...analogicznie GMAIL_2_* i GMAIL_3_*
#   GMAIL_DOMYSLNE=1                  numer albo nazwa konta domyślnego
#
# Nazwą konta jest ZAWSZE część adresu przed "@" (ty@gmail.com -> "ty");
# GMAIL_n_NAME dokłada do niej własne nazwy, a pierwsza z nich jest tą,
# którą Jarvis mówi ("na koncie praca przyszedł mail…").
#
# Stary zapis z jednym kontem (GMAIL_ADDRESS, GMAIL_APP_PASSWORD) dalej
# działa: zajmuje miejsce konta 1, dopóki nie ma GMAIL_1_ADDRESS.

MAKS_KONT_GMAIL = 3
GMAIL_DOMYSLNE = "GMAIL_DOMYSLNE"
STARE_KONTO_GMAIL = ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD")


def zmienne_konta(numer):
    """(nazwa, adres, hasło) — nazwy zmiennych konta o danym numerze."""
    return f"GMAIL_{numer}_NAME", f"GMAIL_{numer}_ADDRESS", f"GMAIL_{numer}_APP_PASSWORD"


def konta_gmail():
    """
    Skonfigurowane konta, domyślne pierwsze. Każde to słownik:
        numer, adres, haslo, nazwa (ta, którą Jarvis mówi), nazwy (wszystkie),
        domyslne (True dla jednego konta)
    """
    konta = []
    for numer in range(1, MAKS_KONT_GMAIL + 1):
        zm_nazwa, zm_adres, zm_haslo = zmienne_konta(numer)
        adres, haslo = wartosc(zm_adres).strip(), wartosc(zm_haslo).replace(" ", "")
        if numer == 1 and not adres:
            adres = wartosc(STARE_KONTO_GMAIL[0]).strip()
            haslo = wartosc(STARE_KONTO_GMAIL[1]).replace(" ", "")
        if not (adres and haslo):
            continue
        wlasne = [n.strip() for n in wartosc(zm_nazwa).split(",") if n.strip()]
        nazwy = list(dict.fromkeys(wlasne + [adres.split("@")[0]]))
        konta.append({"numer": numer, "adres": adres, "haslo": haslo,
                      "nazwa": nazwy[0], "nazwy": nazwy, "domyslne": False})

    wybor = wartosc(GMAIL_DOMYSLNE).strip().lower()
    domyslne = next((k for k in konta if wybor and (wybor == str(k["numer"])
                     or wybor in (n.lower() for n in k["nazwy"]))), konta[0] if konta else None)
    for konto in konta:
        konto["domyslne"] = konto is domyslne
    konta.sort(key=lambda k: not k["domyslne"])
    return konta


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


# TRYB TAPETY — HUD jako żywa tapeta pulpitu (opis w tapeta.py). Przełącznik
# w menu ikony przy zegarku, monitor w Ustawieniach. Monitor zapisujemy pod
# nazwą, którą podaje Qt (np. "G25F2A"); pusta = zawsze główny monitor,
# także po zmianie głównego w Windowsie.
TRYB_TAPETY = "JARVIS_TRYB_TAPETY"
MONITOR_TAPETY = "JARVIS_MONITOR_TAPETY"

# PANEL BOCZNY (opis w panel.py i pasek_boczny.py). Wszystko ustawia się
# w Ustawieniach, na stronie "Panel boczny".
PANEL = "JARVIS_PANEL"                        # 1 = panel włączony
PANEL_2_EKRANY = "JARVIS_PANEL_2_EKRANY"      # 1 = pasek na drugim monitorze
PANEL_MONITOR = "JARVIS_PANEL_MONITOR"        # nazwa monitora; "" = pierwszy niegłówny
PANEL_KRAWEDZ = "JARVIS_PANEL_KRAWEDZ"        # "prawa" albo "lewa"
PANEL_ELEMENTY = "JARVIS_PANEL_ELEMENTY"      # np. "rozmowa,dzien,spotify"
POGODA_MIASTO = "JARVIS_POGODA_MIASTO"        # np. "Kraków" — do pogody w panelu dnia

# Elementy panelu, w kolejności od góry. Maile i notatki są domyślnie
# wyłączone: to najbardziej prywatne rzeczy, a panel widać z drugiego
# końca pokoju — włącza się je świadomie.
ELEMENTY_PANELU = ("rozmowa", "dzien", "maile", "spotify", "notatki")
DOMYSLNE_ELEMENTY = ("rozmowa", "dzien", "spotify")

# Tych przełączników program używa od razu — po ich zmianie w Ustawieniach
# nie trzeba uruchamiać Jarvisa ponownie.
BEZ_RESTARTU = {TRYB_SLUCHAWEK, TRYB_TAPETY, MONITOR_TAPETY, PANEL, PANEL_2_EKRANY,
                PANEL_MONITOR, PANEL_KRAWEDZ, PANEL_ELEMENTY, POGODA_MIASTO}


def _wlaczone(zmienna):
    return wartosc(zmienna).strip().lower() in ("1", "tak", "true", "on")


def panel_wlaczony():
    return _wlaczone(PANEL)


def panel_2_ekrany():
    return _wlaczone(PANEL_2_EKRANY)


def panel_monitor():
    return wartosc(PANEL_MONITOR).strip()


def panel_krawedz():
    return "lewa" if wartosc(PANEL_KRAWEDZ).strip().lower() == "lewa" else "prawa"


def panel_elementy():
    """
    Włączone elementy panelu, w stałej kolejności (ELEMENTY_PANELU).
    Nigdy nieustawione = DOMYSLNE_ELEMENTY. Wszystkie wyłączone zapisujemy
    jako "brak" — pusta wartość znaczyłaby tu "domyślne" (opis funkcji zapisz).
    """
    zapisane = wartosc(PANEL_ELEMENTY).strip()
    wybrane = {e.strip() for e in zapisane.split(",")} if zapisane else set(DOMYSLNE_ELEMENTY)
    return [e for e in ELEMENTY_PANELU if e in wybrane]


def pogoda_miasto():
    return wartosc(POGODA_MIASTO).strip()


# IMIĘ — jak Jarvis ma się do Ciebie zwracać (powitanie.py). Najpierw
# JARVIS_IMIE, a gdy go nie ma — imię z końca JARVIS_POWITANIE (tego samego,
# którym zaczynają się powiadomienia o mailach, np. "Witaj Macieju").
IMIE = "JARVIS_IMIE"
POWITANIE = "JARVIS_POWITANIE"
_SLOWA_POWITAN = {"witaj", "cześć", "czesc", "hej", "siema", "dzień", "dobry", "wieczór", "elo"}


def imie():
    """Imię użytkownika (z Ustawień albo .env) albo "" — gdy go nie znamy."""
    wprost = wartosc(IMIE).strip()
    if wprost:
        return wprost
    slowa = wartosc(POWITANIE).replace(",", " ").replace("!", " ").split()
    if len(slowa) >= 2 and slowa[0].lower() in _SLOWA_POWITAN \
            and slowa[-1].lower() not in _SLOWA_POWITAN and slowa[-1][:1].isupper():
        return slowa[-1]
    return ""


def tryb_tapety():
    """Czy HUD ma być tapetą pulpitu."""
    return wartosc(TRYB_TAPETY).strip().lower() in ("1", "tak", "true", "on")


def ustaw_tryb_tapety(wlaczony):
    zapisz({TRYB_TAPETY: "1" if wlaczony else "0"})


def monitor_tapety():
    """Nazwa monitora dla tapety albo "" (główny)."""
    return wartosc(MONITOR_TAPETY).strip()


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
