"""
roadmapa.py — ciągła praca nad mapą zadań (MapaZadan.md) przez Claude Code,
sterowana z Telegrama.

Jarvis bierze zadania po kolei z MapaZadan.md i proponuje Ci na Telegramie
następne (czego dotyczy, co zmieni, którym modelem) z przyciskami "Akceptuj",
"Pomiń" i "Pokaż inne". Po akceptacji robi je przez Claude Code, oznacza
w pliku jako zrobione, commituje na gałęzi "roadmap", przysyła podsumowanie
z wynikiem testów i od razu proponuje kolejne. I tak w kółko — ale każde
zadanie zaczyna się dopiero po Twoim kliknięciu.


OSOBNA KOPIA ROBOCZA ZAMIAST PRZEŁĄCZANIA GAŁĘZI
================================================

Claude Code pracuje w osobnej kopii roboczej gita (`git worktree`) gałęzi
"roadmap", w %APPDATA%\\Jarvis\\roadmapa. Folder, z którego działa Jarvis,
zostaje na "main" i niczego z roadmapy nie widzi.

Gdyby zamiast tego przełączać gałąź w folderze Jarvisa, pliki działającego
programu podmieniałyby się pod nim, a pierwszy restart — Twój, z /restart
albo po prostu po włączeniu komputera — uruchomiłby niescalony kod
z roadmapy. "Nic nie trafia do main bez Twojej zgody" musi też znaczyć
"nic nie uruchamia się bez Twojej zgody".

Do main zmiany trafiają tylko komendą /scal: Jarvis łączy gałęzie
i restartuje się pod okiem strażnika (straznik.py) — jeśli nowa wersja nie
wstanie, wraca do poprzedniej. Sam Jarvisa nie restartuje nigdy.


MapaZadan.md
============

Zadaniem jest POJEDYNCZA PONUMEROWANA LINIJKA — numer, trudność w nawiasie
kwadratowym i treść; numer w nawiasie na końcu to odnośnik do pomysłu
w POMYSLY_JARVIS.md:

    ## CZĘŚĆ A — zadania do wykonania samodzielnie przez Claude Code
    ### Etap 3. Rozmowa i głos
    15. [Ł] Dźwięk potwierdzenia (krótki "bip") po wykryciu słowa aktywującego. (5)

Nagłówki (#, ##, ###) tylko grupują — nigdy nie są zadaniem. Nagłówek
z "CZĘŚĆ B" zaczyna zadania wymagające Twojego udziału (opis niżej).
Zrobione Jarvis oznacza sam w commicie zadania: "15. ✅ [Ł] ..." (rozumie też
"15. [x] ..."). Gdy plik nazywa się MapaZadan.txt, czyta go tak samo (ale
MapaZadan.md ma pierwszeństwo).

Trudność wybiera model: [Ł] łatwe i [Ś] średnie — model do zadań łatwych
(domyślnie Sonnet 5.5), [T] trudne — do trudnych (domyślnie Opus 5.5); oba
w Ustawieniach (Ogólne → Claude Code). Bez trudności Jarvis nie zgaduje —
pyta Cię w propozycji, którym modelem zrobić zadanie. Model widać
w propozycji i można go tam zmienić przed akceptacją.


CZĘŚĆ B: NAJPIERW PYTANIA, POTEM NIC SAM
=======================================

Zadania z Części B potrzebują czegoś od Ciebie (decyzji, konta, pliku,
klucza). Gdy przyjdzie kolej na takie zadanie, Jarvis go NIE proponuje do
akceptacji, tylko pisze, czego potrzebuje, i czeka. Żeby to ustalić, pyta
Claude Code, który przegląda kod TYLKO DO ODCZYTU (Read, Glob, Grep; nic
nie zmienia) i wypisuje, czego potrzebuje.

Odpowiadasz na Telegramie odpowiedzią (Odpowiedz) na tę wiadomość — tyle
razy, ile trzeba; Jarvis dołączy to do treści zadania. Hasła i klucze idą
do Ustawień, nie do czatu: wiadomość, która wygląda na sekret, jest
odrzucana i nie trafia do Claude Code. Zadanie zaczyna się dopiero, gdy
klikniesz "Zaczynaj".

Kolejność kolejki to kolejność w pliku; "Pomiń" przesuwa zadanie na koniec.
Jarvis czyta mapę zadań z gałęzi roadmap, do której przed każdą propozycją
dociąga nowe commity z main — Twoje zmiany w pliku zobaczy po commicie.


ZANIM ZACZNIE
=============

Jedno zadanie Claude Code naraz (wspólna blokada z /kod). Przed startem:
żadnych niezatwierdzonych zmian ani w folderze Jarvisa, ani w kopii
roadmapy, i testy na gałęzi roadmap przechodzą. Gdy testy padną po zadaniu,
Jarvis nie proponuje kolejnego, tylko "Napraw testy" (albo /odrzuc).


LIMITY CLAUDE CODE
==================

Gdy subskrypcja się wyczerpie, Claude Code kończy się komunikatem w rodzaju
"You've hit your limit · resets 3pm". Jarvis zostawia niedokończone zmiany
w kopii roadmapy, zapisuje stan zadania i od razu pisze Ci o tym na
Telegramie. Godzinę powrotu limitów odczytuje z komunikatu
(kiedy_limity_wroca); jeśli się nie da — sprawdza co 30 minut małym
zapytaniem do Claude Code. Gdy limity wrócą: "Limity wróciły — kontynuujemy?"
i czeka na Twój przycisk. Wznowione zadanie to dalszy ciąg tej samej
rozmowy z Claude Code (--continue w kopii roadmapy).


TO SAMO, CO /kod
================

Te same ograniczenia Claude Code co przy /kod (zdalny_kod.py: lista
narzędzi, bramka na poleceniach powłoki, bez kluczy Jarvisa), a do tego
zakaz edycji mapy zadań. Ten moduł woła tylko most do Telegrama
(telegram_bridge.py) — agent.py go nie importuje, więc model nie może sam
zlecić zadania, nawet gdy ktoś podsunie mu to w mailu.
"""

import datetime
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import konfiguracja
import sciezki
import zdalny_kod
from zdalny_kod import BladKodu

logger = logging.getLogger(__name__)

# Mapa zadań — opis formatu w nagłówku. Pierwszy istniejący z tych plików.
PLIKI_MAPY = ("MapaZadan.md", "MapaZadan.txt")
GALAZ = "roadmap"
GALAZ_GLOWNA = "main"

# Kopia robocza gałęzi roadmap (git worktree) — opis w nagłówku modułu.
KOPIA = os.path.join(sciezki.UZYTKOWNIK, "roadmapa")
# Stan trybu: kolejka, propozycja, zadanie, limity. Obok stanu /kod — poza
# folderem projektu, Claude Code go nie widzi.
PLIK_STANU = os.path.join(zdalny_kod.FOLDER, "roadmapa.json")

# Zadania z roadmapy bywają większe niż /kod — dłuższy limit czasu.
LIMIT_ZADANIA_S = 45 * 60
# Sprawdzenie, czego potrzebuje zadanie z Części B (samo czytanie kodu).
LIMIT_SPRAWDZANIA_S = 10 * 60
NARZEDZIA_ODCZYTU = "Read,Glob,Grep"
# Gdy z komunikatu nie wiadomo, kiedy wrócą limity.
SPRAWDZANIE_LIMITOW_S = 30 * 60
# Sprawdzamy chwilę PO godzinie odnowienia, a nie co do sekundy.
ZAPAS_PO_ODNOWIENIU_S = 2 * 60
PRZERWA_PETLI_S = 30
MAKS_POKAZANYCH = 8

# Kopie odrzuconej pracy — git ich nie usunie, a push ich nie wyśle.
REF_PORZUCONE = "refs/jarvis/roadmapa-porzucone/"
REF_ODRZUCONE = "refs/jarvis/roadmapa-odrzucone/"

# Claude Code nie oznacza sam zadań jako zrobionych — robi to Jarvis po commicie.
ZAKAZANE = tuple(f"{narzedzie}(./{plik})" for plik in PLIKI_MAPY for narzedzie in ("Edit", "Write"))

# Strefa godzin z komunikatów o limitach, gdy komunikat jej nie podaje.
STREFA = ZoneInfo("Europe/Warsaw")


# ---------------------------------------------------------------
# MapaZadan.md
# ---------------------------------------------------------------

@dataclass
class Zadanie:
    numer: int              # numer z mapy (1–100)
    tytul: str              # treść zadania, bez numeru, trudności i odnośnika
    trudnosc: str | None    # "Ł", "Ś", "T" albo None — brak (wtedy Jarvis pyta)
    pomysl: int | None      # odnośnik do POMYSLY_JARVIS.md: "(79)" na końcu linijki
    czesc: str = "A"        # "A" albo "B" (najpierw pytania — opis w nagłówku)
    etap: str = ""          # nagłówek etapu, pod którym stoi zadanie (dla Claude Code)
    zrobione: bool = False


def _plik_mapy(katalog=None):
    """Ścieżka mapy zadań w folderze (domyślnie w kopii roadmapy)."""
    katalog = katalog or KOPIA
    for nazwa in PLIKI_MAPY:
        if os.path.exists(os.path.join(katalog, nazwa)):
            return os.path.join(katalog, nazwa)
    return os.path.join(katalog, PLIKI_MAPY[0])


# "15. [Ł] Treść. (5)" — także "15) ...", "- 15. ...", zrobione "15. ✅ ..." i "15. [x] ...".
_LINIA = re.compile(r"^(?P<poczatek>\s*(?:[-*]\s+)?(?P<numer>\d{1,3})[.)]\s+)"
                    r"(?P<zrobione>(?:✅|\[[xX]\])\s*)?"
                    r"(?:\[(?P<trudnosc>[^\]]{1,3})\]\s*)?(?P<tresc>\S.*?)\s*$")
_ODNOSNIK = re.compile(r"\s*\((?P<pomysl>\d{1,3})\)\s*\.?\s*$")
_NAGLOWEK = re.compile(r"^#{1,6}\s+(?P<tekst>.*)$")
# Część w nagłówku dowolnego poziomu: "## CZĘŚĆ B — ...", "# Część B", "### Part B".
_CZESC = re.compile(r"(?:część|czesc|cz\.|part)\s+(?P<litera>[a-z])\b", re.I)
_TRUDNOSCI = {"Ł": "Ł", "L": "Ł", "Ś": "Ś", "S": "Ś", "T": "T"}
OPIS_TRUDNOSCI = {"Ł": "łatwe", "Ś": "średnie", "T": "trudne"}


def wczytaj_zadania(tekst):
    """
    Zadania z treści mapy zadań, w kolejności z pliku (opis formatu w nagłówku).
    Linijki w blokach kodu (```) to przykłady, nie zadania.
    """
    zadania, w_kodzie, czesc, etap = [], False, "A", ""
    for linia in tekst.splitlines():
        if linia.lstrip().startswith("```"):
            w_kodzie = not w_kodzie
            continue
        if w_kodzie:
            continue
        naglowek = _NAGLOWEK.match(linia)
        if naglowek:
            # Nagłówek tylko grupuje: zmienia część albo etap, zadaniem nie jest nigdy.
            nowa = _CZESC.search(naglowek.group("tekst"))
            if nowa:
                czesc, etap = nowa.group("litera").upper(), ""
            else:
                etap = naglowek.group("tekst").strip()
            continue
        dopasowanie = _LINIA.match(linia)
        if not dopasowanie:
            continue
        tresc = dopasowanie.group("tresc")
        zrobione = bool(dopasowanie.group("zrobione")) or (tresc.startswith("~~") and tresc.endswith("~~"))
        tresc = tresc.strip("~ ")
        odnosnik = _ODNOSNIK.search(tresc)
        if odnosnik:
            tresc = tresc[:odnosnik.start()].strip()
        trudnosc = _TRUDNOSCI.get((dopasowanie.group("trudnosc") or "").strip().upper())
        zadania.append(Zadanie(numer=int(dopasowanie.group("numer")), tytul=tresc, trudnosc=trudnosc,
                               pomysl=int(odnosnik.group("pomysl")) if odnosnik else None,
                               czesc=czesc, etap=etap, zrobione=zrobione))
    return zadania


def oznacz_zrobione(tekst, tytul, numer=None):
    """
    Treść mapy zadań z zadaniem oznaczonym "✅" zaraz po numerze.
    Szukamy po numerze i treści (sam numer mógł się zmienić). (nowa treść, czy znaleziono).
    """
    linie = tekst.splitlines(keepends=True)
    kandydaci, w_kodzie = [], False
    for indeks, linia in enumerate(linie):
        if linia.lstrip().startswith("```"):
            w_kodzie = not w_kodzie
            continue
        dopasowanie = None if w_kodzie else _LINIA.match(linia.rstrip("\r\n"))
        if not dopasowanie or dopasowanie.group("zrobione"):
            continue
        tresc = dopasowanie.group("tresc").strip("~ ")
        odnosnik = _ODNOSNIK.search(tresc)
        tresc = tresc[:odnosnik.start()].strip() if odnosnik else tresc
        if tresc == tytul:
            kandydaci.append((int(dopasowanie.group("numer")) == numer, indeks, dopasowanie.group("poczatek")))
    if not kandydaci:
        return tekst, False
    # Najpierw linijka z tym samym numerem; gdy numer się zmienił — pierwsza z tą treścią.
    _, indeks, poczatek = max(kandydaci, key=lambda k: (k[0], -k[1]))
    linie[indeks] = poczatek + "✅ " + linie[indeks][len(poczatek):]
    return "".join(linie), True


def kolejka(zadania, stan):
    """Niezrobione zadania w kolejności pliku; pominięte na końcu, w kolejności pomijania."""
    otwarte = [z for z in zadania if not z.zrobione and z.tytul not in stan["zrobione"]]
    pominiete = [t for t in stan["pominiete"] if any(z.tytul == t for z in otwarte)]
    return ([z for z in otwarte if z.tytul not in pominiete]
            + [next(z for z in otwarte if z.tytul == t) for t in pominiete])


def model_dla(zadanie):
    """[Ł] i [Ś] — model do zadań łatwych, [T] — do trudnych; bez trudności — None (pytamy)."""
    if zadanie.trudnosc is None:
        return None
    return konfiguracja.model_claude("trudne" if zadanie.trudnosc == "T" else "latwe")


def licznik(pozycja, ile):
    """"zadanie 1 ze 100", "zadanie 3 z 99" — "ze" tylko przed "sto…"."""
    return f"zadanie {pozycja} {'ze' if 100 <= ile <= 199 else 'z'} {ile}"


def _etykieta(zadanie):
    """"nr 15 [Ł]" — numer i trudność, jak w mapie."""
    return f"nr {zadanie.numer} [{zadanie.trudnosc}]" if zadanie.trudnosc else f"nr {zadanie.numer} [?]"


# ---------------------------------------------------------------
# Limity Claude Code
# ---------------------------------------------------------------

_LIMIT = re.compile(r"usage limit|hit your (?:\w+ )?limit|(?:session|weekly|daily|5-hour|opus|sonnet)"
                    r" limit|limit reached|limit resets|out of (?:extra )?usage", re.I)
# "limit" w innym sensie — to nie są limity subskrypcji.
_INNY_LIMIT = re.compile(r"context limit|budget limit|subagent|size limit|recursion limit|"
                         r"device limit|watch limit|spend limit", re.I)
_MIESIACE = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug",
                                         "sep", "oct", "nov", "dec"), 1)}


def komunikat_limitu(wynik, bledy=""):
    """
    Tekst komunikatu o wyczerpanych limitach albo None.

    Treść wyniku sprawdzamy tylko przy błędzie — udane zadanie może w swoim
    podsumowaniu mówić o limitach (np. gdy samo dotyczyło tej funkcji).
    """
    kandydaci = [bledy or ""]
    if wynik is None or wynik.get("is_error"):
        kandydaci.append(str((wynik or {}).get("result") or ""))
    for tekst in kandydaci:
        if _LIMIT.search(tekst) and not (_INNY_LIMIT.search(tekst) and "usage" not in tekst.lower()):
            return " ".join(tekst.split())[:300]
    return None


def kiedy_limity_wroca(tekst, teraz=None):
    """
    Chwila odnowienia limitów odczytana z komunikatu (datetime ze strefą) albo None.

    Formaty, które zna Claude Code:
      "Claude AI usage limit reached|1760620800"   — znacznik czasu (starsze wersje)
      "You've hit your limit · resets 3pm (Europe/Warsaw)"
      "Usage limit reached · limit resets Oct 20, 9am"
      "... resets at 15:30", "... resets in 2h 30m", "... resets 2026-10-20 08:00 UTC"
    """
    teraz = teraz or datetime.datetime.now(STREFA)
    if not tekst:
        return None

    znacznik = re.search(r"\|(\d{10})\b", tekst)
    if znacznik:
        return datetime.datetime.fromtimestamp(int(znacznik.group(1)), STREFA)

    iso = re.search(r"resets?\s+(?:at\s+)?(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})\s*(UTC|Z)?", tekst, re.I)
    if iso:
        r, m, d, g, mi = (int(x) for x in iso.groups()[:5])
        strefa = datetime.timezone.utc if iso.group(6) else STREFA
        return datetime.datetime(r, m, d, g, mi, tzinfo=strefa).astimezone(STREFA)

    za = re.search(r"resets?\s+in\s+(?:(\d+)\s*h\w*)?\s*(?:(\d+)\s*m\w*)?", tekst, re.I)
    if za and (za.group(1) or za.group(2)):
        return teraz + datetime.timedelta(hours=int(za.group(1) or 0), minutes=int(za.group(2) or 0))

    zegar = re.search(r"resets?\s+(?:at\s+)?(?:(?P<mies>[A-Za-z]{3})[a-z]*\.?\s+(?P<dzien>\d{1,2}),?\s+"
                      r"(?:at\s+)?)?(?P<godz>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ampm>[ap]\.?m\.?)?"
                      r"(?:\s*\((?P<strefa>[^)]+)\))?", tekst, re.I)
    if not zegar or not (zegar.group("ampm") or zegar.group("min") or zegar.group("mies")):
        return None
    godzina, minuta = int(zegar.group("godz")), int(zegar.group("min") or 0)
    ampm = (zegar.group("ampm") or "").lower().replace(".", "")
    if ampm == "pm" and godzina < 12:
        godzina += 12
    elif ampm == "am" and godzina == 12:
        godzina = 0
    if godzina > 23 or minuta > 59:
        return None
    try:
        strefa = ZoneInfo(zegar.group("strefa").strip()) if zegar.group("strefa") else STREFA
    except Exception:
        strefa = STREFA
    lokalnie = teraz.astimezone(strefa)
    if zegar.group("mies"):
        miesiac = _MIESIACE.get(zegar.group("mies").lower()[:3])
        if miesiac is None:
            return None
        kiedy = datetime.datetime(lokalnie.year, miesiac, int(zegar.group("dzien")), godzina, minuta,
                                  tzinfo=strefa)
        if kiedy < lokalnie - datetime.timedelta(days=1):
            kiedy = kiedy.replace(year=kiedy.year + 1)       # "Jan 2" w grudniu
    else:
        kiedy = lokalnie.replace(hour=godzina, minute=minuta, second=0, microsecond=0)
        if kiedy <= lokalnie:
            kiedy += datetime.timedelta(days=1)
    return kiedy.astimezone(STREFA)


def _sprawdz_limity(model):
    """
    Czy limity już wróciły? Małe zapytanie do Claude Code (bez narzędzi,
    poza projektem). (True, None) — wróciły; (False, kiedy wrócą albo None);
    (None, None) — nie wiadomo (inny błąd), spróbujemy później.
    """
    os.makedirs(zdalny_kod.FOLDER, exist_ok=True)
    polecenie = [zdalny_kod.shutil.which("claude") or "claude", "-p", "--model", model, "--tools", "",
                 "--output-format", "json", "--strict-mcp-config", "--setting-sources", "user"]
    wynik, blad, bledy = zdalny_kod.uruchom_claude_code("Odpowiedz jednym słowem: ok", polecenie,
                                                       zdalny_kod.FOLDER, 180)
    tekst = komunikat_limitu(wynik, bledy)
    if tekst:
        return False, kiedy_limity_wroca(tekst)
    if blad or wynik is None or wynik.get("is_error"):
        return None, None
    return True, None


# ---------------------------------------------------------------
# Stan
# ---------------------------------------------------------------

_blokada = threading.RLock()
_kanal = None           # most do Telegrama: wyslij(), edytuj(), usun_przyciski()
_restart = None         # restart Jarvisa ze strażnikiem — tylko dla /scal
_zatrzymaj = threading.Event()
_watek = None
_proces = None          # Claude Code w toku (żeby zatrzymać go przy zamykaniu Jarvisa)
_rezerwacja = None      # opis, pod którym zadanie zajęło Claude Code (zdalny_kod.zarezerwuj)


def _zarezerwuj(opis):
    """Zajmuje Claude Code dla zadania z roadmapy. False, gdy coś już trwa."""
    global _rezerwacja
    if not zdalny_kod.zarezerwuj(opis):
        return False
    _rezerwacja = opis
    return True


def _zwolnij():
    """
    Zwalnia Claude Code zajęty przez zadanie — tylko własną rezerwację. Wołane
    zaraz po Claude Code: testy i commit go nie potrzebują, a kolejna
    propozycja (sprawdzenie zadania z Części B) musi mieć wolną drogę.
    """
    global _rezerwacja
    if _rezerwacja is not None:
        zdalny_kod.zwolnij(_rezerwacja)
        _rezerwacja = None


def _nowy_stan():
    return {
        "pominiete": [],        # tytuły w kolejności pomijania (koniec kolejki)
        "zrobione": [],         # tytuły zrobione od ostatniego /odrzuc
        "historia": [],         # zadania: tytuł, commit, kiedy, testy, scalone/odrzucone
        "propozycja": None,     # {"pid", "tytul", "model", "id"} — czeka na przycisk;
                                # Część B także: "czesc", "stan", "potrzeby", "od_ciebie"
        "zadanie": None,        # {"pid", "tytul", "model", "stan", "od", "naprawa", "powod"}
        "limit": None,          # {"wroca", "sprawdz", "powiadomiono"}
        "naprawa": None,        # {"pid", "nieudane"} — testy padły, propozycja naprawy
        "potwierdzenie": None,  # {"pid", "co"} — /scal mimo testów, /odrzuc
        "testy": {},            # drzewo gita -> wynik testów (gałąź roadmap)
        "ostatni_raport": None,  # data ostatniego raportu dziennego
        "wlaczona": False,       # czy tryb był włączony przy poprzednim sprawdzeniu
    }


def _stan():
    stan = _nowy_stan()
    try:
        with open(PLIK_STANU, encoding="utf-8") as plik:
            stan.update(json.load(plik))
    except (OSError, ValueError):
        pass
    return stan


def _zapisz(stan):
    os.makedirs(os.path.dirname(PLIK_STANU), exist_ok=True)
    tymczasowy = PLIK_STANU + ".tmp"
    with open(tymczasowy, "w", encoding="utf-8") as plik:
        json.dump(stan, plik, ensure_ascii=False, indent=1)
    os.replace(tymczasowy, PLIK_STANU)


def _pid():
    return uuid.uuid4().hex[:6]


def _skrot(tekst, dlugosc=60):
    return zdalny_kod._skrot(tekst, dlugosc)


def _wyslij(tekst, przyciski=None):
    if _kanal is None:
        logger.info("[ROADMAPA] (bez Telegrama) %s", tekst[:200])
        return None
    return _kanal.wyslij(tekst, przyciski)


def _edytuj(id_wiadomosci, tekst, przyciski=None):
    if _kanal is not None and id_wiadomosci is not None:
        _kanal.edytuj(id_wiadomosci, tekst, przyciski)


def _bez_przyciskow(id_wiadomosci):
    if _kanal is not None and id_wiadomosci is not None:
        _kanal.usun_przyciski(id_wiadomosci)


def trwa():
    """Czy Claude Code pracuje teraz nad zadaniem z roadmapy."""
    with _blokada:
        zadanie = _stan()["zadanie"]
    return bool(zadanie) and zadanie.get("stan") in ("sprawdzam", "trwa")


# ---------------------------------------------------------------
# Git: kopia robocza gałęzi roadmap
# ---------------------------------------------------------------

def _git(*argumenty, kopia=False, **opcje):
    return zdalny_kod._git(*argumenty, katalog=KOPIA if kopia else None, **opcje)


def _galaz_istnieje(nazwa):
    return _git("rev-parse", "-q", "--verify", f"refs/heads/{nazwa}", sprawdz=False) is not None


def _niezatwierdzone(kopia=False):
    """Pliki ze zmianami, których nie ma w commicie (bez plików z .gitignore)."""
    wyjscie = _git("status", "--porcelain", "-z", "--untracked-files=all", kopia=kopia)
    wpisy = [w for w in wyjscie.split("\0") if w]
    # _git obcina białe znaki z początku wyjścia — pierwszy wpis " M a.py"
    # przychodzi jako "M a.py". Status ma zawsze dwa znaki i spację.
    if wpisy and not re.match(r"^.. ", wpisy[0]):
        wpisy[0] = " " + wpisy[0]
    pliki, pomin = [], False
    for wpis in wpisy:
        if pomin:                       # druga ścieżka przy zmianie nazwy
            pomin = False
            continue
        pliki.append(wpis[3:])
        pomin = wpis[0] in "RC"
    return pliki


def _lista(pliki, maks=5):
    return ", ".join(pliki[:maks]) + (f" i {len(pliki) - maks} innych" if len(pliki) > maks else "")


def _przygotuj_kopie():
    """Kopia robocza gałęzi roadmap — przy pierwszym użyciu tworzy ją (i gałąź)."""
    if os.path.isfile(os.path.join(KOPIA, ".git")) and \
            _git("rev-parse", "--abbrev-ref", "HEAD", kopia=True, sprawdz=False) == GALAZ:
        return
    if not _galaz_istnieje(GALAZ_GLOWNA):
        raise BladKodu(f"w repozytorium Jarvisa nie ma gałęzi {GALAZ_GLOWNA}")
    _git("worktree", "prune", sprawdz=False)
    if os.path.isdir(KOPIA) and os.listdir(KOPIA):
        raise BladKodu(f"folder {KOPIA} istnieje, ale nie jest kopią roadmapy — przenieś go albo usuń")
    os.makedirs(os.path.dirname(KOPIA), exist_ok=True)
    if _galaz_istnieje(GALAZ):
        _git("worktree", "add", KOPIA, GALAZ)
    else:
        _git("worktree", "add", "-b", GALAZ, KOPIA, GALAZ_GLOWNA)
    logger.info("[ROADMAPA] Kopia robocza gałęzi %s: %s", GALAZ, KOPIA)


def _dociagnij_main():
    """Nowe commity z main (np. Twoje zmiany w mapie zadań) trafiają na gałąź roadmap."""
    if _git("merge-base", "--is-ancestor", GALAZ_GLOWNA, "HEAD", kopia=True, sprawdz=False) is not None:
        return
    if _git("merge", "--no-edit", "-m", f"Roadmapa: zmiany z {GALAZ_GLOWNA}", GALAZ_GLOWNA,
            kopia=True, sprawdz=False) is None:
        _git("merge", "--abort", kopia=True, sprawdz=False)
        raise BladKodu(f"zmiany z {GALAZ_GLOWNA} gryzą się z gałęzią {GALAZ} (konflikt) — "
                       "połącz je ręcznie albo /odrzuc")
    logger.info("[ROADMAPA] Dociągnąłem zmiany z %s.", GALAZ_GLOWNA)


def _zadania(dociagnij=True):
    """Zadania z mapy zadań na gałęzi roadmap (po dociągnięciu main, gdy kopia jest czysta)."""
    _przygotuj_kopie()
    if dociagnij and not _niezatwierdzone(kopia=True):
        _dociagnij_main()
    try:
        with open(_plik_mapy(), encoding="utf-8") as plik:
            return wczytaj_zadania(plik.read())
    except FileNotFoundError:
        return []


def _head_kopii():
    return _git("rev-parse", "HEAD", kopia=True)


def _do_scalenia():
    """Commity na gałęzi roadmap, których nie ma w main: [(skrót, temat)]."""
    if not _galaz_istnieje(GALAZ):
        return []
    wyjscie = _git("log", "--format=%h\t%s", f"{GALAZ_GLOWNA}..{GALAZ}", sprawdz=False) or ""
    return [tuple(linia.split("\t", 1)) for linia in wyjscie.splitlines() if "\t" in linia]


def _testy_kopii(powiadom=True):
    """
    Wynik testów dla kodu z HEAD kopii (czystej) — z pamięci, gdy kod się nie
    zmienił. Same testy idą BEZ blokady stanu: trwają kilkanaście sekund,
    a /roadmapa status ma w tym czasie odpowiadać od razu.
    """
    drzewo = _git("rev-parse", "HEAD^{tree}", kopia=True)
    with _blokada:
        zapamietany = _stan()["testy"].get(drzewo)
    if zapamietany is not None:
        return zapamietany
    if powiadom:
        _wyslij("🧪 Sprawdzam testy na gałęzi roadmap…")
    wynik = zdalny_kod.uruchom_testy(katalog=KOPIA)
    with _blokada:
        stan = _stan()
        _zapamietaj_testy(stan, drzewo, wynik)
        _zapisz(stan)
    return wynik


def _zapamietaj_testy(stan, drzewo, wynik):
    wynik = dict(wynik, kiedy=time.time())
    stan["testy"] = dict(list(stan["testy"].items())[-19:] + [(drzewo, wynik)])
    stan["ostatnie_testy"] = wynik


# ---------------------------------------------------------------
# Propozycje
# ---------------------------------------------------------------

def _opis_modelu(model, zadanie):
    """Nazwa modelu; przy zmienionym — skąd domyślny."""
    if model is None:
        return "❓ brak trudności w mapie — wybierz model"
    domyslny = model_dla(zadanie)
    if domyslny is None or model == domyslny:
        return konfiguracja.nazwa_modelu(model)
    return f"{konfiguracja.nazwa_modelu(model)} (zmieniony; domyślnie {konfiguracja.nazwa_modelu(domyslny)})"


def _tekst_propozycji(zadanie, propozycja, numer, ile):
    """
    Krótko: licznik, numer, trudność, treść i model — bez fragmentów pliku.
    W Części B dochodzi to, czego Jarvis potrzebuje od Ciebie.
    """
    czesci = [f"🗺 {licznik(numer, ile).capitalize()} · {_etykieta(zadanie)}"
              + (" · 🧩 wymaga Twojego udziału (Część B)" if zadanie.czesc == "B" else ""),
              zadanie.tytul,
              f"🧠 Model: {_opis_modelu(propozycja['model'], zadanie)}"]
    if zadanie.czesc != "B":
        return "\n\n".join(czesci)

    if propozycja.get("stan") == "analiza":
        czesci.append("🔍 Tego zadania nie zaczynam sam. Sprawdzam w kodzie (tylko czytanie), "
                      "czego będę od Ciebie potrzebować…")
        return "\n\n".join(czesci)
    if propozycja.get("potrzeby"):
        czesci.append("Zanim zacznę, potrzebuję od Ciebie:\n" + propozycja["potrzeby"])
    else:
        czesci.append("Nie ustaliłem, czego potrzebuję (" + (propozycja.get("blad") or "nie wiem czemu")
                      + "). Napisz mi, co mam wiedzieć, albo kliknij 🔍 Sprawdź jeszcze raz.")
    od_ciebie = propozycja.get("od_ciebie") or []
    if od_ciebie:
        czesci.append(f"📝 Od Ciebie ({len(od_ciebie)}): " + _skrot(" / ".join(od_ciebie), 400))
    czesci.append("Odpowiedz na TĘ wiadomość (przytrzymaj → Odpowiedz). Hasła i klucze — w Ustawieniach, "
                  "nie tutaj. Gdy wszystko będzie, kliknij ▶️ Zaczynaj.")
    return "\n\n".join(czesci)


def _przyciski_propozycji(zadanie, propozycja):
    pid = propozycja["pid"]
    if zadanie.czesc == "B" and propozycja.get("stan") == "analiza":
        return [[("⏭ Pomiń", f"rm|p|{pid}")]]
    if propozycja["model"] is None:
        # Bez trudności nie zgadujemy — najpierw wybór modelu, dopiero potem "Akceptuj".
        return ([[(nazwa, f"rm|M|{pid}|{n}")] for n, (_, nazwa) in enumerate(konfiguracja.MODELE_CLAUDE_CODE)]
                + [[("⏭ Pomiń", f"rm|p|{pid}"), ("📋 Pokaż inne", f"rm|i|{pid}")]])
    start = "▶️ Zaczynaj" if zadanie.czesc == "B" else "✅ Akceptuj"
    drugi = [("🧠 Zmień model", f"rm|m|{pid}")]
    if zadanie.czesc == "B" and not propozycja.get("potrzeby"):
        drugi.append(("🔍 Sprawdź jeszcze raz", f"rm|r|{pid}"))
    return [[(start, f"rm|a|{pid}"), ("⏭ Pomiń", f"rm|p|{pid}"), ("📋 Pokaż inne", f"rm|i|{pid}")], drugi]


def _pokaz_propozycje(id_wiadomosci, zadanie, propozycja, numer, ile, naglowek=""):
    """Wysyła albo podmienia propozycję. Zwraca id wiadomości."""
    tekst = (naglowek + "\n\n" if naglowek else "") + _tekst_propozycji(zadanie, propozycja, numer, ile)
    przyciski = _przyciski_propozycji(zadanie, propozycja)
    if id_wiadomosci:
        _edytuj(id_wiadomosci, tekst, przyciski)
        return id_wiadomosci
    return _wyslij(tekst, przyciski)


def zaproponuj(tytul=None, id_wiadomosci=None, naglowek=""):
    """
    Propozycja następnego zadania (albo `tytul`). id_wiadomosci — podmień
    tę wiadomość zamiast wysyłać nową (np. po "Pokaż inne").
    """
    with _blokada:
        stan = _stan()
        if stan["zadanie"]:
            _wyslij(_opis_zadania(stan["zadanie"]) + "\n\nNowe zadanie zaproponuję, gdy to się skończy.")
            return
        try:
            zadania = _zadania()
        except BladKodu as e:
            _wyslij(f"⚠️ Roadmapa: {e}.")
            return
        lista = kolejka(zadania, stan)
        if not lista:
            stan["propozycja"] = None
            _zapisz(stan)
            nazwa = os.path.basename(_plik_mapy())
            tekst = (f"🎉 Wszystkie zadania z {nazwa} są zrobione. Dopisz nowe (commit na main), "
                     "a potem /roadmapa dalej." if zadania else
                     f"W {nazwa} nie ma zadań — ponumerowanych linijek w rodzaju „1. [Ł] Treść”. "
                     "Dopisz je i zrób commit na main, a potem /roadmapa dalej.")
            _edytuj(id_wiadomosci, tekst) if id_wiadomosci else _wyslij(tekst)
            return
        zadanie = next((z for z in lista if z.tytul == tytul), lista[0])
        propozycja = {"pid": _pid(), "tytul": zadanie.tytul, "numer": zadanie.numer,
                      "model": model_dla(zadanie), "id": None}
        if zadanie.czesc == "B":
            # Część B: najpierw czego potrzebuję — od Claude Code (samo czytanie,
            # w tle). Bez "Zaczynaj", dopóki tego nie wiesz.
            propozycja.update(czesc="B", od_ciebie=[], blad=None, potrzeby=None, stan="analiza")
        propozycja["id"] = _pokaz_propozycje(id_wiadomosci, zadanie, propozycja,
                                             lista.index(zadanie) + 1, len(lista), naglowek)
        stan["propozycja"] = propozycja
        _zapisz(stan)
        logger.info("[ROADMAPA] Propozycja: nr %d %s (%s, część %s).", zadanie.numer, zadanie.tytul,
                    propozycja["model"], zadanie.czesc)
        if propozycja.get("stan") == "analiza":
            _sprawdz_potrzeby_w_tle(propozycja["pid"])


def _odswiez_propozycje(stan, id_wiadomosci):
    """Podmienia treść propozycji (np. po zmianie modelu)."""
    propozycja = stan["propozycja"]
    lista = kolejka(_zadania(dociagnij=False), stan)
    zadanie = next((z for z in lista if z.tytul == propozycja["tytul"]), None)
    if zadanie is None:
        stan["propozycja"] = None
        _zapisz(stan)
        _edytuj(id_wiadomosci, "To zadanie zniknęło z kolejki. /roadmapa dalej — nowa propozycja.")
        return
    _pokaz_propozycje(id_wiadomosci, zadanie, propozycja, lista.index(zadanie) + 1, len(lista))


def _pokaz_inne(stan, id_wiadomosci):
    propozycja = stan["propozycja"]
    lista = [z for z in kolejka(_zadania(dociagnij=False), stan) if z.tytul != propozycja["tytul"]]
    if not lista:
        _edytuj(id_wiadomosci, "To jedyne zadanie w kolejce.\n\n„" + propozycja["tytul"] + "”",
                [[("↩️ Wróć", f"rm|w|{propozycja['pid']}")]])
        return
    stan["inne"] = [z.tytul for z in lista[:MAKS_POKAZANYCH]]
    _zapisz(stan)
    przyciski = [[(_skrot(f"{_etykieta(z)}{' B' if z.czesc == 'B' else ''} {z.tytul}", 60),
                   f"rm|t|{propozycja['pid']}|{n - 1}")] for n, z in enumerate(lista[:MAKS_POKAZANYCH], 1)]
    przyciski.append([("↩️ Wróć", f"rm|w|{propozycja['pid']}")])
    _edytuj(id_wiadomosci, f"📋 Inne zadania w kolejce ({len(lista)}) — wybierz jedno:", przyciski)


def _menu_modeli(stan, id_wiadomosci):
    pid = stan["propozycja"]["pid"]
    przyciski = [[(("• " if model == stan["propozycja"]["model"] else "") + nazwa, f"rm|M|{pid}|{n}")]
                 for n, (model, nazwa) in enumerate(konfiguracja.MODELE_CLAUDE_CODE)]
    przyciski.append([("↩️ Wróć", f"rm|w|{pid}")])
    _edytuj(id_wiadomosci, f"🧠 Którym modelem zrobić „{stan['propozycja']['tytul']}”?", przyciski)


# ---------------------------------------------------------------
# Część B: czego potrzebuję i Twoje odpowiedzi
# ---------------------------------------------------------------

def _tresc_sprawdzenia(zadanie):
    return ("To NIE jest jeszcze polecenie wykonania zadania — niczego nie zmieniaj (masz tylko "
            f"odczyt plików).\n\n{_opis_dla_claude(zadanie)}\n\n"
            "Zadania z Części B Jarvis zaczyna dopiero wtedy, gdy użytkownik da mu to, czego trzeba. "
            "Przejrzyj kod tyle, ile potrzeba, i napisz po polsku, czego potrzebujesz od użytkownika, "
            "żeby zrobić to zadanie dobrze: decyzje do podjęcia, informacje, konta albo usługi do "
            "założenia, pliki. Najwyżej 6 krótkich punktów, każdy od „• ”. Nie proś o wpisanie haseł "
            "ani kluczy w odpowiedzi — klucze użytkownik wpisuje w Ustawieniach Jarvisa; napisz tylko, "
            "który klucz i skąd go wziąć. Jeśli niczego nie potrzebujesz, napisz dokładnie: "
            "„Niczego — mogę zaczynać.” Bez wstępu i podsumowania.")


def _opis_dla_claude(zadanie):
    """Zadanie z mapy dla Claude Code: numer, treść, trudność, etap, odnośnik do pomysłu."""
    linie = [f"Zadanie nr {zadanie.numer} z mapy zadań Jarvisa ({os.path.basename(_plik_mapy())}): "
             f"{zadanie.tytul}",
             "Trudność: " + (OPIS_TRUDNOSCI[zadanie.trudnosc] if zadanie.trudnosc else "nieoznaczona")]
    if zadanie.etap:
        linie.append(f"Etap: {zadanie.etap}")
    if zadanie.czesc == "B":
        linie.append("Część B — zadanie wymagające udziału użytkownika.")
    if zadanie.pomysl is not None:
        linie.append(f"Szczegóły: pomysł nr {zadanie.pomysl} w POMYSLY_JARVIS.md — jeśli ten plik jest "
                     "w projekcie, przeczytaj go.")
    return "\n".join(linie)


def _sprawdz_potrzeby_w_tle(pid):
    threading.Thread(target=_sprawdz_potrzeby, args=(pid,), name="roadmapa-czesc-b", daemon=True).start()


def _sprawdz_potrzeby(pid):
    """
    Wątek: Claude Code TYLKO CZYTA kod i wypisuje, czego potrzebuje zadanie
    z Części B. Niczego nie zmienia i niczego nie zaczyna — wynik idzie do
    Ciebie, a zadanie czeka na Twoje "Zaczynaj".
    """
    global _proces
    with _blokada:
        propozycja = _stan()["propozycja"] or {}
    if propozycja.get("pid") != pid:
        return
    potrzeby, blad = None, None
    try:
        zadanie = next((z for z in _zadania(dociagnij=False) if z.tytul == propozycja["tytul"]), None)
        if zadanie is None:
            blad = "zadanie zniknęło z mapy"
        elif not zdalny_kod.zarezerwuj(sprawdzanie := f"sprawdzanie zadania z Części B „{_skrot(zadanie.tytul)}”"):
            blad = f"trwa {zdalny_kod.zajety()}"
        else:
            try:
                # Bez trudności model zadania jeszcze nie jest wybrany — samo
                # czytanie robi model do zadań łatwych.
                polecenie = zdalny_kod.polecenie_claude(
                    propozycja["model"] or konfiguracja.model_claude("latwe"), kontynuuj=False,
                    zakazane=ZAKAZANE, narzedzia=NARZEDZIA_ODCZYTU)
                wynik, blad_claude, bledy = zdalny_kod.uruchom_claude_code(
                    _tresc_sprawdzenia(zadanie), polecenie, KOPIA, LIMIT_SPRAWDZANIA_S,
                    przy_starcie=_zapamietaj_proces)
            finally:
                _proces = None
                zdalny_kod.zwolnij(sprawdzanie)
            if komunikat_limitu(wynik, bledy):
                wroca = kiedy_limity_wroca(komunikat_limitu(wynik, bledy))
                blad = "skończyły się limity Claude Code" + (
                    f", wrócą ok. {wroca.strftime('%H:%M')}" if wroca else "")
            elif blad_claude or wynik is None or wynik.get("is_error"):
                blad = _skrot(blad_claude or str((wynik or {}).get("result") or "błąd Claude Code"), 200)
                podpowiedz = zdalny_kod.wskazowka(blad)
                blad += f" — {podpowiedz}" if podpowiedz else ""
            else:
                potrzeby = _skrot(str(wynik.get("result") or "").strip(), 1500) or None
    except BladKodu as e:
        blad = str(e)
    except Exception:
        logger.exception("[ROADMAPA] Sprawdzanie zadania z Części B wysypało się")
        blad = "błąd po stronie Jarvisa (szczegóły w jarvis.log)"

    with _blokada:
        stan = _stan()
        propozycja = stan["propozycja"] or {}
        if propozycja.get("pid") != pid:
            return              # w międzyczasie pominięte albo zmienione
        propozycja.update(stan="czeka", potrzeby=potrzeby, blad=blad)
        _zapisz(stan)
        logger.info("[ROADMAPA] Część B — %s: %s", propozycja["tytul"], "potrzeby gotowe" if potrzeby else blad)
        _odswiez_propozycje(stan, propozycja["id"])


# Wartość sekretu w odpowiedzi do Części B. Węższe niż pamiec.wyglada_na_sekret:
# "klucz już jest w Ustawieniach" ma przejść (sami o to prosimy), a odpadają
# same wartości — ciąg jak klucz API, numer karty, "hasło: ...".
_WARTOSC_SEKRETU = re.compile(
    r"(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{20,}"
    r"|(?:\d[ -]?){12,}"
    r"|\b(?:has[lł]\w*|password|pin|token\w*|klucz\w*|api[ _-]?key|secret|sekret\w*)\s*[:=]\s*\S+", re.I)


def _wyglada_na_wartosc_sekretu(tekst):
    tajne = konfiguracja.tajne_wartosci().values()
    return bool(_WARTOSC_SEKRETU.search(tekst)) or any(w and w in tekst for w in tajne)


def odpowiedz(id_wiadomosci, tekst):
    """
    Twoja odpowiedź (Telegram: Odpowiedz) na pytania do zadania z Części B.
    Zwraca True, gdy wiadomość była dla roadmapy — wtedy nie idzie do agenta.
    """
    with _blokada:
        stan = _stan()
        propozycja = stan["propozycja"] or {}
        if propozycja.get("czesc") != "B" or id_wiadomosci is None or propozycja.get("id") != id_wiadomosci:
            return False
        tekst = (tekst or "").strip()
        if _wyglada_na_wartosc_sekretu(tekst):
            logger.info("[ROADMAPA] Odpowiedź do Części B wyglądała na sekret — odrzucona.")
            _wyslij("🔒 To wygląda na hasło albo klucz — nie zapisuję tego i nie przekażę Claude Code. "
                    "Wpisz go w Ustawieniach Jarvisa (Klucze API) i odpisz tu tylko, że już jest.")
            return True
        if not tekst:
            return True
        propozycja["od_ciebie"] = (propozycja.get("od_ciebie") or []) + [tekst[:2000]]
        _zapisz(stan)
        _odswiez_propozycje(stan, propozycja["id"])
        _wyslij(f"📝 Zapisałem ({len(propozycja['od_ciebie'])}). Gdy to już wszystko — kliknij ▶️ Zaczynaj "
                "pod pytaniem.")
        return True


def _dodatek_czesci_b(propozycja):
    """Treść dla Claude Code: czego potrzebował i co odpisałeś."""
    od_ciebie = propozycja.get("od_ciebie") or []
    return ("To zadanie z Części B mapy zadań. Zanim zacząłeś, Jarvis zapytał użytkownika, czego "
            "potrzebujesz:\n" + (propozycja.get("potrzeby") or "(nie udało się tego ustalić)")
            + "\n\nOdpowiedzi użytkownika:\n"
            + ("\n".join(f"- {o}" for o in od_ciebie) if od_ciebie else
               "(brak — użytkownik kliknął „Zaczynaj” bez odpowiedzi; zrób, co się da, a o brakujące "
               "rzeczy zapytaj w podsumowaniu)"))


# ---------------------------------------------------------------
# Zadanie: sprawdzenie, Claude Code, commit, raport
# ---------------------------------------------------------------

def _opis_zadania(zadanie):
    tytul = ("naprawa testów" if zadanie.get("naprawa") else
             (f"nr {zadanie['numer']} " if zadanie.get("numer") else "") + f"„{zadanie['tytul']}”")
    model = konfiguracja.nazwa_modelu(zadanie["model"])
    if zadanie["stan"] in ("sprawdzam", "trwa"):
        minuty = int((time.time() - zadanie["od"]) // 60)
        return f"🛠 Trwa zadanie z roadmapy: {tytul} ({model}, od {minuty} min)."
    if zadanie["stan"] == "limit":
        return f"⏸ Zadanie {tytul} czeka na limity Claude Code."
    return f"⏸ Zadanie {tytul} jest przerwane: {zadanie.get('powod') or 'nie wiem czemu'}."


def _tresc_zadania(zadanie, dodatek=""):
    return _opis_dla_claude(zadanie) + (f"\n\n{dodatek}" if dodatek else "") + "\n\n" + ZASADY


ZASADY = (
    "Zasady:\n"
    f"- Pracujesz w osobnej kopii projektu, na gałęzi {GALAZ}. Commit zrobi Jarvis po zadaniu — "
    "Ty tylko zmieniasz pliki.\n"
    "- Nie zmieniaj mapy zadań (MapaZadan.md) — Jarvis sam oznaczy zadanie jako zrobione.\n"
    "- Kod komentuj po polsku, tak jak reszta projektu; do nowej logiki dopisz testy w tests/.\n"
    "- Testów nie uruchomisz (z powłoki wolno tylko git status i git diff) — Jarvis uruchomi je "
    "po zadaniu i pokaże wynik.\n"
    "- Na końcu krótko, po polsku, podsumuj, co zmieniłeś.")


def _tresc_wznowienia(zadanie):
    tytul = "naprawa testów" if zadanie.get("naprawa") else f"„{zadanie['tytul']}”"
    return (f"Wracamy do przerwanego zadania z roadmapy: {tytul} "
            f"(przerwa: {zadanie.get('powod') or 'limity Claude Code'}). Sprawdź przez git status "
            "i git diff, co już jest zrobione, i dokończ zadanie.\n\n" + ZASADY)


def _tresc_naprawy(nieudane):
    lista = "\n".join(f"- {n}" for n in nieudane[:20]) or "- (pytest nie podał nazw testów)"
    return ("Testy Jarvisa na gałęzi roadmap nie przechodzą po ostatnim zadaniu z roadmapy. "
            f"Nieudane:\n{lista}\n\nZnajdź przyczynę i napraw KOD. Testów nie poluzowuj — zmień test "
            "tylko wtedy, gdy to on jest błędny, i napisz dlaczego.\n\n" + ZASADY)


def _sprawdz_przed_startem(tytul):
    """
    Czy można zacząć zadanie `tytul`? (komunikat albo None, zadanie albo None,
    nieudane testy albo None). Po drodze: kopia gałęzi roadmap gotowa i main
    dociągnięty. Wołane bez blokady stanu — testy chwilę trwają.
    """
    gotowe, powod = zdalny_kod.dostepny(z_claude=True)
    if not gotowe:
        return powod, None, None
    brudne = _niezatwierdzone()
    if brudne:
        return ("W folderze Jarvisa są niezatwierdzone zmiany (" + _lista(brudne) + "). "
                "Zatwierdź je (/zatwierdz albo git commit) albo cofnij — zadanie z roadmapy "
                "zaczynam na czystym kodzie."), None, None
    _przygotuj_kopie()
    brudne = _niezatwierdzone(kopia=True)
    if brudne:
        return (f"W kopii roadmapy są niezatwierdzone zmiany ({_lista(brudne)}) — /odrzuc "
                "je porzuci."), None, None
    _dociagnij_main()
    with _blokada:
        lista = kolejka(_zadania(dociagnij=False), _stan())
    zadanie = next((z for z in lista if z.tytul == tytul), None)
    if zadanie is None:
        return "Tego zadania nie ma już w kolejce (zrobione albo usunięte z mapy zadań).", None, None
    testy = _testy_kopii()
    if testy["stan"] == "blad":
        return "testy na gałęzi roadmap nie przechodzą", None, testy["nieudane"]
    return None, zadanie, None


def _akceptuj(stan, id_wiadomosci):
    propozycja = stan["propozycja"]
    if not konfiguracja.roadmapa_wlaczona():
        _edytuj(id_wiadomosci, "Tryb roadmapy jest wyłączony (Ustawienia → Ogólne). Nic nie zaczynam.")
        stan["propozycja"] = None
        _zapisz(stan)
        return
    if propozycja.get("czesc") == "B" and propozycja.get("stan") == "analiza":
        _wyslij("Jeszcze sprawdzam, czego potrzebuję do tego zadania — chwila.")
        return
    if propozycja.get("model") is None:
        _wyslij("To zadanie nie ma trudności w mapie — najpierw wybierz model pod propozycją.")
        return
    opis = f"zadanie z roadmapy „{_skrot(propozycja['tytul'])}”"
    if not _zarezerwuj(opis):
        _wyslij(f"Trwa {zdalny_kod.zajety()}. Naraz robię jedno zadanie Claude Code — "
                "kliknij „Akceptuj” jeszcze raz, gdy się skończy.")
        return
    stan["propozycja"] = None
    stan["zadanie"] = {"pid": _pid(), "tytul": propozycja["tytul"], "numer": propozycja.get("numer"),
                       "model": propozycja["model"], "stan": "sprawdzam", "od": time.time(),
                       "naprawa": False, "powod": "",
                       "dodatek": _dodatek_czesci_b(propozycja) if propozycja.get("czesc") == "B" else ""}
    _zapisz(stan)
    _edytuj(id_wiadomosci, f"✅ Zaakceptowane: nr {propozycja.get('numer')} „{propozycja['tytul']}” — "
                           f"{konfiguracja.nazwa_modelu(propozycja['model'])}.\n"
                           "Sprawdzam, czy nie ma niezatwierdzonych zmian i czy testy przechodzą…")
    _w_tle("nowe")


def _napraw(stan):
    if not _zarezerwuj("naprawa testów na gałęzi roadmap"):
        _wyslij(f"Trwa {zdalny_kod.zajety()} — naprawę zacznę, gdy kliknisz jeszcze raz po jego końcu.")
        return
    nieudane = (stan["naprawa"] or {}).get("nieudane") or []
    stan["naprawa"] = None
    stan["zadanie"] = {"pid": _pid(), "tytul": "Naprawa testów", "model": konfiguracja.model_claude("trudne"),
                       "stan": "sprawdzam", "od": time.time(), "naprawa": True, "powod": "",
                       "nieudane": nieudane}
    _zapisz(stan)
    _wyslij(f"🔧 Naprawiam testy ({konfiguracja.nazwa_modelu(stan['zadanie']['model'])}).")
    _w_tle("naprawa")


def _wznow(stan):
    zadanie = stan["zadanie"]
    if not _zarezerwuj(f"zadanie z roadmapy „{_skrot(zadanie['tytul'])}”"):
        _wyslij(f"Trwa {zdalny_kod.zajety()} — wznowię, gdy klikniesz jeszcze raz po jego końcu.")
        return
    zadanie.update(stan="sprawdzam", od=time.time())
    stan["limit"] = None
    _zapisz(stan)
    _wyslij(f"▶️ Wznawiam: „{zadanie['tytul']}” ({konfiguracja.nazwa_modelu(zadanie['model'])}).")
    _w_tle("wznow")


def _w_tle(tryb):
    threading.Thread(target=_praca, args=(tryb,), name="roadmapa-zadanie", daemon=True).start()


def _zapamietaj_proces(proces):
    global _proces
    _proces = proces


def _praca(tryb):
    """
    Wątek zadania: sprawdzenie (nowe), Claude Code, testy, commit, raport.
    tryb — "nowe", "wznow" (po limitach / przerwaniu) albo "naprawa" (testów).
    """
    global _proces
    try:
        with _blokada:
            zadanie = _stan()["zadanie"]
        if tryb == "nowe":
            problem, z, nieudane = _sprawdz_przed_startem(zadanie["tytul"])
            if problem:
                with _blokada:
                    stan = _stan()
                    stan["zadanie"] = None
                    if nieudane is not None:
                        stan["naprawa"] = {"pid": _pid(), "nieudane": nieudane}
                    _zapisz(stan)
                    if nieudane is not None:
                        _proponuj_naprawe(stan, "Nie zaczynam — testy na gałęzi roadmap nie przechodzą.")
                    else:
                        _wyslij(f"⚠️ Nie zaczynam: {problem}\n\n/roadmapa dalej — nowa propozycja, "
                                "gdy to załatwisz.")
                return
            tresc = _tresc_zadania(z, zadanie.get("dodatek") or "")
        elif tryb == "naprawa":
            _przygotuj_kopie()
            tresc = _tresc_naprawy(zadanie.get("nieudane") or [])
        else:
            tresc = _tresc_wznowienia(zadanie)
        with _blokada:
            stan = _stan()
            zadanie = stan["zadanie"]
            zadanie.update(stan="trwa", od=time.time())
            _zapisz(stan)
        przed = _head_kopii()
        if tryb == "nowe":
            _wyslij(f"🛠 Zaczynam: „{zadanie['tytul']}” ({konfiguracja.nazwa_modelu(zadanie['model'])}). "
                    f"Dam znać, jak skończę (najdłużej {LIMIT_ZADANIA_S // 60} min).")

        logger.info("[ROADMAPA] Claude Code (%s, %s): %s", tryb, zadanie["model"], zadanie["tytul"])
        start = time.time()
        polecenie = zdalny_kod.polecenie_claude(zadanie["model"], kontynuuj=tryb == "wznow",
                                                zakazane=ZAKAZANE)
        wynik, blad, bledy = zdalny_kod.uruchom_claude_code(tresc, polecenie, KOPIA, LIMIT_ZADANIA_S,
                                                           przy_starcie=_zapamietaj_proces)
        _proces = None
        _zwolnij()
        if _zatrzymaj.is_set():
            return          # Jarvis się zamyka — przy starcie zadanie będzie "przerwane"
        _po_claude(zadanie, wynik, blad, bledy, przed, time.time() - start)
    except BladKodu as e:
        _przerwij(f"błąd gita: {e}")
    except Exception:
        logger.exception("[ROADMAPA] Zadanie wysypało się")
        _przerwij("błąd po stronie Jarvisa (szczegóły w jarvis.log)")
    finally:
        _proces = None
        _zwolnij()


def _przerwij(powod, przyciski=True):
    """Zadanie zostaje przerwane: zmiany czekają w kopii, decyzja należy do Ciebie."""
    with _blokada:
        stan = _stan()
        zadanie = stan["zadanie"]
        if not zadanie:
            return
        zadanie.update(stan="przerwane", powod=powod, pid=_pid())
        _zapisz(stan)
    logger.info("[ROADMAPA] Przerwane: %s — %s", zadanie["tytul"], powod)
    _wyslij(f"⏸ {_opis_zadania(zadanie)}\n\nNiedokończone zmiany czekają w kopii roadmapy.",
            _przyciski_przerwanego(zadanie["pid"]) if przyciski else None)


def _przyciski_przerwanego(pid):
    return [[("▶️ Kontynuuj", f"rm|k|{pid}"), ("🗑 Porzuć zadanie", f"rm|x|{pid}")]]


def _po_claude(zadanie, wynik, blad, bledy, przed, czas):
    tekst_limitu = komunikat_limitu(wynik, bledy)
    if tekst_limitu:
        _po_limicie(tekst_limitu)
        return
    udane = blad is None and wynik is not None and not wynik.get("is_error")
    zmiany = _niezatwierdzone(kopia=True)
    if not udane:
        powod = blad or "Claude Code zakończył się błędem: " + _skrot(
            str((wynik or {}).get("result") or (wynik or {}).get("subtype") or "?"), 300)
        if zmiany:
            _przerwij(powod)
            return
        with _blokada:
            stan = _stan()
            stan["zadanie"] = None
            _zapisz(stan)
        podpowiedz = zdalny_kod.wskazowka(powod)
        _wyslij(f"⚠️ {powod}\n\nNiczego nie zmienił — zadanie zostaje w kolejce."
                + (f"\n\n{podpowiedz}" if podpowiedz else ""))
        if konfiguracja.roadmapa_wlaczona():
            zaproponuj()
        return

    testy = zdalny_kod.uruchom_testy(katalog=KOPIA)
    try:
        commit = _zatwierdz_zadanie(zadanie, wynik, testy, czas)
    except BladKodu as e:
        _przerwij(str(e))
        return

    with _blokada:
        stan = _stan()
        po = _head_kopii()
        _zapamietaj_testy(stan, _git("rev-parse", "HEAD^{tree}", kopia=True), testy)
        if not zadanie.get("naprawa"):
            stan["zrobione"] = list(dict.fromkeys(stan["zrobione"] + [zadanie["tytul"]]))
            stan["pominiete"] = [t for t in stan["pominiete"] if t != zadanie["tytul"]]
        stan["historia"] = (stan["historia"] + [{
            "tytul": zadanie["tytul"], "numer": zadanie.get("numer"), "commit": commit, "kiedy": time.time(),
            "model": zadanie["model"],
            "testy": testy["stan"], "opis_testow": testy["opis"], "naprawa": bool(zadanie.get("naprawa")),
            "scalone": False, "odrzucone": False}])[-200:]
        stan["zadanie"] = None
        _zapisz(stan)

    _wyslij(_raport_zadania(zadanie, wynik, testy, przed, po, commit, czas))
    if testy["stan"] == "blad":
        with _blokada:
            stan = _stan()
            stan["naprawa"] = {"pid": _pid(), "nieudane": testy["nieudane"]}
            _zapisz(stan)
            _proponuj_naprawe(stan, "Kolejnego zadania nie zaczynam, dopóki testy nie przechodzą.")
    elif konfiguracja.roadmapa_wlaczona():
        zaproponuj()


def _zatwierdz_zadanie(zadanie, wynik, testy, czas):
    """Commit pracy z zadania na gałęzi roadmap (z oznaczeniem w mapie zadań). Skrót albo None."""
    if not zadanie.get("naprawa"):
        sciezka = _plik_mapy()
        try:
            with open(sciezka, encoding="utf-8", newline="") as plik:
                tekst = plik.read()
            nowy, znaleziono = oznacz_zrobione(tekst, zadanie["tytul"], zadanie.get("numer"))
            if znaleziono:
                with open(sciezka, "w", encoding="utf-8", newline="") as plik:
                    plik.write(nowy)
        except FileNotFoundError:
            pass
    zmiany = _niezatwierdzone(kopia=True)
    if not zmiany:
        return None
    znaleziony = zdalny_kod._sekret_w_plikach(
        [s for s in zmiany if os.path.exists(os.path.join(KOPIA, s))], katalog=KOPIA)
    if znaleziony:
        raise BladKodu(f"nie commituję — w pliku {znaleziony[0]} jest wartość klucza {znaleziony[1]}")
    podsumowanie = _skrot(str((wynik or {}).get("result") or ""), 1500)
    numer = f"nr {zadanie['numer']} " if zadanie.get("numer") and not zadanie.get("naprawa") else ""
    wiadomosc = (f"Roadmapa: {numer}{zadanie['tytul']}\n\nModel: {konfiguracja.nazwa_modelu(zadanie['model'])}, "
                 f"{zdalny_kod._czas(czas)}\nTesty: {testy['stan']} — {testy['opis']}"
                 + (f"\n\n{podsumowanie}" if podsumowanie else ""))
    _git("add", "-A", kopia=True)
    _git("commit", "-q", "-m", wiadomosc, kopia=True)
    return _git("rev-parse", "--short", "HEAD", kopia=True)


def _raport_zadania(zadanie, wynik, testy, przed, po, commit, czas):
    tytul = "Naprawa testów" if zadanie.get("naprawa") else f"„{zadanie['tytul']}”"
    czesci = [f"✅ Zrobione: {tytul} ({konfiguracja.nazwa_modelu(zadanie['model'])}, "
              f"{zdalny_kod._czas(czas)}).",
              _skrot(str((wynik or {}).get("result") or "(bez podsumowania)"), 2500)]
    if przed != po:
        czesci.append(zdalny_kod._lista_zmian(przed, po))
        zmienione_testy = [s for _, s in zdalny_kod._zmiany(przed, po) if s.startswith("tests/")]
        if zmienione_testy:
            czesci.append("⚠️ Zadanie zmieniło też testy (" + ", ".join(zmienione_testy[:5])
                          + ") — sprawdź, czy ich nie poluzowało.")
    czesci.append(zdalny_kod.opis_testow(testy))
    czesci.append(f"Commit {commit} na gałęzi {GALAZ}. Do {GALAZ_GLOWNA} trafi dopiero po /scal "
                  "(albo /odrzuc — porzucenie)." if commit else "Pliki bez zmian — nie było czego commitować.")
    return "\n\n".join(czesci)


def _proponuj_naprawe(stan, naglowek):
    naprawa = stan["naprawa"]
    lista = "\n".join(f"• {n}" for n in naprawa["nieudane"][:8])
    _wyslij(f"❌ {naglowek}" + (f"\n\nNie przechodzą:\n{lista}" if lista else "")
            + "\n\nMogę zlecić Claude Code naprawę (model do zadań trudnych) albo /odrzuc porzuci "
              "zmiany z roadmapy.",
            [[("🔧 Napraw testy", f"rm|n|{naprawa['pid']}")]])


def _po_limicie(tekst_limitu):
    teraz = datetime.datetime.now(STREFA)
    wroca = kiedy_limity_wroca(tekst_limitu, teraz)
    sprawdz = (wroca + datetime.timedelta(seconds=ZAPAS_PO_ODNOWIENIU_S) if wroca and wroca > teraz
               else teraz + datetime.timedelta(seconds=SPRAWDZANIE_LIMITOW_S))
    with _blokada:
        stan = _stan()
        zadanie = stan["zadanie"]
        zadanie.update(stan="limit", powod="skończyły się limity Claude Code", pid=_pid())
        stan["limit"] = {"wroca": wroca.isoformat() if wroca else None, "sprawdz": sprawdz.timestamp(),
                         "powiadomiono": False, "komunikat": tekst_limitu}
        _zapisz(stan)
    logger.warning("[ROADMAPA] Limity Claude Code: %s (wrócą: %s)", tekst_limitu, wroca)
    kiedy = (f"Limity wrócą ok. {wroca.strftime('%H:%M')}"
             + ("" if wroca.date() == teraz.date() else f" ({wroca.strftime('%d.%m')})")
             + " — wtedy sprawdzę i zapytam, czy kontynuować." if wroca else
             "Z komunikatu nie wiem, kiedy wrócą — sprawdzam co 30 minut i dam znać.")
    _wyslij(f"⏸ Skończyły się limity Claude Code przy zadaniu „{zadanie['tytul']}”. Przerwałem "
            "i zapisałem miejsce: niedokończone zmiany czekają w kopii roadmapy, a po wznowieniu "
            f"Claude Code dostanie dalszy ciąg tej samej rozmowy.\n\n{kiedy}\n\n"
            f"Komunikat Claude Code: „{tekst_limitu}”")


def _porzuc_zadanie(stan):
    """Niedokończone zmiany z kopii — do migawki, kopia wraca do ostatniego commita."""
    zadanie = stan["zadanie"]
    kopia = REF_PORZUCONE + zdalny_kod._znacznik_czasu()
    migawka = zdalny_kod.zrzut(f"Jarvis: porzucone zadanie z roadmapy — {zadanie['tytul']}", katalog=KOPIA)
    _git("update-ref", kopia, migawka)
    _git("reset", "-q", "--hard", "HEAD", kopia=True)
    _git("clean", "-q", "-fd", kopia=True)
    if not zadanie.get("naprawa"):
        stan["pominiete"] = [t for t in stan["pominiete"] if t != zadanie["tytul"]] + [zadanie["tytul"]]
    stan["zadanie"] = None
    stan["limit"] = None
    _zapisz(stan)
    return kopia


# ---------------------------------------------------------------
# Przyciski i komendy (z mostu do Telegrama)
# ---------------------------------------------------------------

def przycisk(dane, id_wiadomosci):
    """Kliknięty przycisk "rm|akcja|pid|argument" pod wiadomością id_wiadomosci."""
    czesci = (dane or "").split("|")
    if len(czesci) < 3 or czesci[0] != "rm":
        return
    akcja, pid, argument = czesci[1], czesci[2], czesci[3] if len(czesci) > 3 else ""
    # /scal i /odrzuc po potwierdzeniu — już bez blokady stanu (testy, git, restart).
    potem = None
    try:
        with _blokada:
            stan = _stan()
            propozycja = stan["propozycja"] or {}
            zadanie = stan["zadanie"] or {}
            naprawa = stan["naprawa"] or {}
            potwierdzenie = stan["potwierdzenie"] or {}

            if akcja == "r" and propozycja.get("pid") == pid and propozycja.get("czesc") == "B":
                propozycja.update(stan="analiza", blad=None)
                _zapisz(stan)
                _odswiez_propozycje(stan, id_wiadomosci)
                _sprawdz_potrzeby_w_tle(pid)
            elif akcja in "apimMtw" and propozycja.get("pid") == pid:
                if akcja == "a":
                    _akceptuj(stan, id_wiadomosci)
                elif akcja == "p":
                    tytul = propozycja["tytul"]
                    stan["pominiete"] = [t for t in stan["pominiete"] if t != tytul] + [tytul]
                    stan["propozycja"] = None
                    _zapisz(stan)
                    _edytuj(id_wiadomosci, f"⏭ Pominięte: „{tytul}” — na koniec kolejki.")
                    zaproponuj()
                elif akcja == "i":
                    _pokaz_inne(stan, id_wiadomosci)
                elif akcja == "t":
                    inne = stan.get("inne") or []
                    tytul = inne[int(argument)] if argument.isdigit() and int(argument) < len(inne) else None
                    zaproponuj(tytul, id_wiadomosci)
                elif akcja == "m":
                    _menu_modeli(stan, id_wiadomosci)
                elif akcja == "M":
                    modele = konfiguracja.MODELE_CLAUDE_CODE
                    if argument.isdigit() and int(argument) < len(modele):
                        propozycja["model"] = modele[int(argument)][0]
                        _zapisz(stan)
                    _odswiez_propozycje(stan, id_wiadomosci)
                else:   # "w" — wróć do propozycji
                    _odswiez_propozycje(stan, id_wiadomosci)
            elif akcja in "kx" and zadanie.get("pid") == pid and zadanie.get("stan") in ("limit", "przerwane"):
                _bez_przyciskow(id_wiadomosci)
                if akcja == "k":
                    _wznow(stan)
                else:
                    kopia = _porzuc_zadanie(stan)
                    _wyslij(f"🗑 Porzuciłem niedokończone zadanie „{zadanie['tytul']}” (na koniec kolejki). "
                            f"Zmiany zachowałem w migawce {kopia}.")
                    if konfiguracja.roadmapa_wlaczona():
                        zaproponuj()
            elif akcja == "l" and zadanie.get("pid") == pid:
                _edytuj(id_wiadomosci, "⏸ Dobrze, czekam. /roadmapa dalej — gdy zechcesz kontynuować.")
            elif akcja == "n" and naprawa.get("pid") == pid and not stan["zadanie"]:
                _bez_przyciskow(id_wiadomosci)
                _napraw(stan)
            elif akcja in "so" and potwierdzenie.get("pid") == pid:
                stan["potwierdzenie"] = None
                _zapisz(stan)
                _bez_przyciskow(id_wiadomosci)
                potem = scal if akcja == "s" else odrzuc
            elif akcja == "c" and potwierdzenie.get("pid") == pid:
                stan["potwierdzenie"] = None
                _zapisz(stan)
                _edytuj(id_wiadomosci, "Anulowane, nic nie robię.")
            else:
                _edytuj(id_wiadomosci, "Ta wiadomość jest już nieaktualna. /roadmapa — co teraz.")
        if potem is not None:
            potem(potwierdzone=True)
    except BladKodu as e:
        _wyslij(f"⚠️ Roadmapa: {e}.")


def komenda(nazwa, reszta=""):
    """/roadmapa [status|dalej], /scal, /odrzuc."""
    try:
        if nazwa == "scal":
            scal()
        elif nazwa == "odrzuc":
            odrzuc()
        elif reszta.strip().lower() in ("dalej", "start", "wznow", "wznów"):
            dalej()
        else:
            _wyslij(status())
    except BladKodu as e:
        _wyslij(f"⚠️ Roadmapa: {e}.")


def dalej():
    """/roadmapa dalej — to, na co teraz czeka roadmapa (albo nowa propozycja)."""
    with _blokada:
        stan = _stan()
        zadanie = stan["zadanie"]
        if zadanie and zadanie["stan"] in ("limit", "przerwane"):
            zadanie["pid"] = _pid()
            _zapisz(stan)
            dopisek = ""
            if zadanie["stan"] == "limit" and not (stan["limit"] or {}).get("powiadomiono"):
                dopisek = "\n\nLimity mogą jeszcze nie wrócić — wtedy zadanie znowu się zatrzyma."
            _wyslij(_opis_zadania(zadanie) + dopisek, _przyciski_przerwanego(zadanie["pid"]))
        elif zadanie:
            _wyslij(_opis_zadania(zadanie))
        elif stan["naprawa"]:
            _proponuj_naprawe(stan, "Testy na gałęzi roadmap nie przechodzą.")
        elif not konfiguracja.roadmapa_wlaczona():
            _wyslij("Tryb roadmapy jest wyłączony — włączysz go w Ustawieniach (Ogólne).")
        else:
            if stan["propozycja"]:
                _bez_przyciskow(stan["propozycja"].get("id"))
            zaproponuj()


def _nazwa_wpisu(wpis):
    """Zadanie z historii do raportu: "nr 15 Treść" albo "naprawa testów"."""
    if wpis.get("naprawa"):
        return "naprawa testów"
    return (f"nr {wpis['numer']} " if wpis.get("numer") else "") + wpis["tytul"]


def status(raport=False, teraz=None):
    """Tekst /roadmapa status (i raportu dziennego — z tym, co zrobione od ostatniego)."""
    teraz = teraz or datetime.datetime.now(STREFA)
    with _blokada:
        stan = _stan()
    godzina, minuta = konfiguracja.raport_roadmapy()
    czesci = []
    if raport:
        czesci.append(f"📋 Raport roadmapy — {teraz.strftime('%d.%m.%Y')}")
        od = stan.get("ostatni_raport_ts") or 0
        zrobione = [h for h in stan["historia"] if h["kiedy"] > od]
        if zrobione:
            czesci.append(f"Zrobione od ostatniego raportu ({len(zrobione)}):\n" + "\n".join(
                f"• {_nazwa_wpisu(h)} — testy "
                f"{'✅' if h['testy'] == 'ok' else '❌' if h['testy'] == 'blad' else '—'}"
                + (f", commit {h['commit']}" if h.get("commit") else "")
                + (" (scalone)" if h.get("scalone") else " (odrzucone)" if h.get("odrzucone") else "")
                for h in zrobione))
        else:
            czesci.append("Od ostatniego raportu nic nie zrobiłem.")
    else:
        czesci.append("🗺 Roadmapa — tryb " + ("włączony" if konfiguracja.roadmapa_wlaczona()
                                               else "wyłączony (Ustawienia → Ogólne)"))

    if stan["zadanie"]:
        teraz_robie = _opis_zadania(stan["zadanie"])
        limit = stan["limit"] or {}
        if stan["zadanie"]["stan"] == "limit":
            teraz_robie += (" Wrócą ok. " + datetime.datetime.fromisoformat(limit["wroca"]).strftime("%H:%M")
                            if limit.get("wroca") else " Sprawdzam co 30 minut.") \
                if not limit.get("powiadomiono") else " Limity już wróciły — /roadmapa dalej."
        czesci.append(teraz_robie)
    elif stan["naprawa"]:
        czesci.append("❌ Testy na gałęzi roadmap nie przechodzą — /roadmapa dalej (naprawa) albo /odrzuc.")
    elif stan["propozycja"] and stan["propozycja"].get("czesc") == "B":
        czesci.append(f"🧩 Czeka na Ciebie zadanie z Części B: „{stan['propozycja']['tytul']}” — odpowiedz "
                      "na pytania pod nim i kliknij ▶️ Zaczynaj.")
    elif stan["propozycja"]:
        czesci.append(f"Czeka na Twoją decyzję: „{stan['propozycja']['tytul']}”.")

    try:
        do_scalenia = _do_scalenia()
    except BladKodu:
        do_scalenia = []
    if do_scalenia:
        czesci.append(f"Do scalenia z {GALAZ_GLOWNA} ({len(do_scalenia)}):\n"
                      + "\n".join(f"• {s} {_skrot(t, 70)}" for s, t in do_scalenia[:8])
                      + "\n/scal — scal i zrestartuj · /odrzuc — porzuć")
    testy = stan.get("ostatnie_testy")
    if testy:
        kiedy = datetime.datetime.fromtimestamp(testy["kiedy"], STREFA).strftime("%d.%m %H:%M")
        czesci.append(f"{zdalny_kod.opis_testow(testy)} (gałąź {GALAZ}, {kiedy})")
    else:
        czesci.append(f"🧪 Testy na gałęzi {GALAZ}: jeszcze nie uruchamiane.")

    try:
        lista = kolejka(_zadania(dociagnij=False), stan) if os.path.isdir(KOPIA) else []
    except BladKodu:
        lista = []
    if lista:
        czesci.append(f"W kolejce ({len(lista)} nieukończonych):\n" + "\n".join(
            f"{n}. {_etykieta(z)} {_skrot(z.tytul, 60)} — "
            + (konfiguracja.nazwa_modelu(model_dla(z)) if model_dla(z) else "model do wyboru")
            + (" (część B — najpierw pytania)" if z.czesc == "B" else "")
            for n, z in enumerate(lista[:6], 1)) + (f"\n…i {len(lista) - 6} innych" if len(lista) > 6 else ""))
    else:
        czesci.append("Kolejka pusta (albo kopia roadmapy jeszcze nie powstała).")
    if not raport:
        czesci.append(f"Raport codziennie o {godzina:02d}:{minuta:02d}. /roadmapa dalej — następny krok.")
    return "\n\n".join(czesci)


def scal(potwierdzone=False):
    """/scal — gałąź roadmap do main, potem restart ze strażnikiem (powrót przy błędzie)."""
    if zdalny_kod.zajety():
        _wyslij(f"Trwa {zdalny_kod.zajety()} — scalę, gdy się skończy.")
        return
    gotowe, powod = zdalny_kod.dostepny()
    if not gotowe:
        _wyslij(powod)
        return
    do_scalenia = _do_scalenia()
    if not do_scalenia:
        _wyslij(f"Gałąź {GALAZ} nie ma nic nowego względem {GALAZ_GLOWNA} — nie ma czego scalać.")
        return
    galaz = _git("rev-parse", "--abbrev-ref", "HEAD")
    if galaz != GALAZ_GLOWNA:
        _wyslij(f"Folder Jarvisa jest na gałęzi {galaz}, a nie {GALAZ_GLOWNA} — nie scalam.")
        return
    brudne = _niezatwierdzone()
    if brudne:
        _wyslij(f"W folderze Jarvisa są niezatwierdzone zmiany ({_lista(brudne)}) — zatwierdź je "
                "(/zatwierdz) albo cofnij (/cofnij), zanim scalę.")
        return

    drzewo = _git("rev-parse", f"{GALAZ}^{{tree}}")
    with _blokada:
        testy = _stan()["testy"].get(drzewo)
    if testy is None and not potwierdzone and os.path.isfile(os.path.join(KOPIA, ".git")) \
            and not _niezatwierdzone(kopia=True) and _git("rev-parse", "HEAD^{tree}", kopia=True) == drzewo:
        testy = _testy_kopii()

    with _blokada:
        stan = _stan()
        if not potwierdzone and (testy is None or testy["stan"] == "blad"):
            pid = _pid()
            stan["potwierdzenie"] = {"pid": pid, "co": "scal"}
            _zapisz(stan)
            powod = (zdalny_kod.opis_testow(testy) if testy else
                     "🧪 Nie wiem, czy testy przechodzą (kopia roadmapy ma niezatwierdzone zmiany).")
            _wyslij(f"⚠️ Nie scalam od razu.\n\n{powod}\n\nNowa wersja może się nie uruchomić "
                    "albo działać źle — strażnik wtedy wróci do poprzedniej.",
                    [[("⚠️ Scal mimo to", f"rm|s|{pid}"), ("Anuluj", f"rm|c|{pid}")]])
            return

        wiadomosc = (f"Roadmapa: scalenie {len(do_scalenia)} commitów z gałęzi {GALAZ}\n\n"
                     + "\n".join(f"- {t}" for _, t in do_scalenia[:30]))
        if _git("merge", "--no-ff", "-m", wiadomosc, GALAZ, sprawdz=False) is None:
            _git("merge", "--abort", sprawdz=False)
            _wyslij(f"⚠️ Nie udało się scalić — {GALAZ} gryzie się z {GALAZ_GLOWNA} (konflikt). "
                    "Niczego nie zmieniłem.")
            return
        scalone = {s for s, _ in do_scalenia}
        for wpis in stan["historia"]:
            if wpis.get("commit") in scalone:
                wpis["scalone"] = True
        _zapisz(stan)
    commit = _git("rev-parse", "--short", "HEAD")
    logger.info("[ROADMAPA] Scalone: %s (%d commitów).", commit, len(do_scalenia))
    _wyslij(f"🔀 Scalone z {GALAZ_GLOWNA}: {commit} ({len(do_scalenia)} commitów z roadmapy).")
    if _restart is None:
        _wyslij("Restart z telefonu jest teraz niedostępny — uruchom Jarvisa ponownie sam.")
        return
    _restart(" Po /scal: nowa wersja z roadmapy.")


def odrzuc(potwierdzone=False):
    """/odrzuc — porzuca z gałęzi roadmap wszystko, czego nie ma w main (z kopią w migawce)."""
    if zdalny_kod.zajety():
        _wyslij(f"Trwa {zdalny_kod.zajety()} — poczekaj na jego koniec.")
        return
    gotowe, powod = zdalny_kod.dostepny()
    if not gotowe:
        _wyslij(powod)
        return
    with _blokada:
        stan = _stan()
        do_scalenia = _do_scalenia()
        brudne = _niezatwierdzone(kopia=True) if os.path.isfile(os.path.join(KOPIA, ".git")) else []
        if not do_scalenia and not brudne and not stan["zadanie"]:
            _wyslij(f"Na gałęzi {GALAZ} nie ma niczego poza {GALAZ_GLOWNA} — nie ma czego odrzucać.")
            return
        if not potwierdzone:
            pid = _pid()
            stan["potwierdzenie"] = {"pid": pid, "co": "odrzuc"}
            _zapisz(stan)
            lista = "\n".join(f"• {s} {_skrot(t, 70)}" for s, t in do_scalenia[:10])
            _wyslij(f"🗑 Odrzucić z gałęzi {GALAZ} wszystko, czego nie ma w {GALAZ_GLOWNA}?"
                    + (f"\n\nCommity ({len(do_scalenia)}):\n{lista}" if do_scalenia else "")
                    + (f"\n\nNiedokończone zmiany: {_lista(brudne)}" if brudne else "")
                    + "\n\nZadania wrócą do kolejki. Kopię zachowam w migawce.",
                    [[("🗑 Tak, odrzuć", f"rm|o|{pid}"), ("Anuluj", f"rm|c|{pid}")]])
            return

        _przygotuj_kopie()
        kopia = REF_ODRZUCONE + zdalny_kod._znacznik_czasu()
        _git("update-ref", kopia, zdalny_kod.zrzut("Jarvis: odrzucona praca z roadmapy", katalog=KOPIA))
        _git("reset", "-q", "--hard", GALAZ_GLOWNA, kopia=True)
        _git("clean", "-q", "-fd", kopia=True)
        odrzucone = {s for s, _ in do_scalenia}
        for wpis in stan["historia"]:
            if wpis.get("commit") in odrzucone:
                wpis["odrzucone"] = True
        wrocily = {w["tytul"] for w in stan["historia"] if w.get("odrzucone")}
        stan["zrobione"] = [t for t in stan["zrobione"] if t not in wrocily]
        stan.update(zadanie=None, limit=None, naprawa=None, propozycja=None)
        _zapisz(stan)
    logger.info("[ROADMAPA] Odrzucone; kopia w %s", kopia)
    _wyslij(f"🗑 Odrzucone: gałąź {GALAZ} jest znowu taka jak {GALAZ_GLOWNA}. Zadania wróciły do "
            f"kolejki.\nKopia: {kopia} (git log {kopia})")
    if konfiguracja.roadmapa_wlaczona():
        zaproponuj()


# ---------------------------------------------------------------
# Wątek w tle: włączenie trybu, raport dzienny, powrót limitów
# ---------------------------------------------------------------

def krok(teraz=None):
    """Jedno sprawdzenie (co PRZERWA_PETLI_S). Osobno, żeby testy mogły podać czas."""
    teraz = teraz or datetime.datetime.now(STREFA)
    wlaczona = konfiguracja.roadmapa_wlaczona()
    godzina, minuta = konfiguracja.raport_roadmapy()
    dzis = teraz.date().isoformat()
    with _blokada:
        stan = _stan()
        wlaczono = wlaczona and not stan["wlaczona"]
        stan["wlaczona"] = wlaczona
        # Włączony po godzinie raportu — pierwszy raport jutro, nie zaraz obok propozycji.
        if wlaczono and (teraz.hour, teraz.minute) >= (godzina, minuta):
            stan.update(ostatni_raport=dzis, ostatni_raport_ts=teraz.timestamp())
        _zapisz(stan)
    if not wlaczona:
        return
    if wlaczono and not stan["zadanie"] and not stan["propozycja"]:
        zaproponuj(naglowek="🗺 Tryb roadmapy włączony.")
    if (teraz.hour, teraz.minute) >= (godzina, minuta) and stan.get("ostatni_raport") != dzis:
        _wyslij(status(raport=True, teraz=teraz))
        with _blokada:
            stan = _stan()
            stan.update(ostatni_raport=dzis, ostatni_raport_ts=teraz.timestamp())
            _zapisz(stan)

    limit = stan.get("limit")
    zadanie = stan.get("zadanie")
    if zadanie and zadanie["stan"] == "limit" and limit and not limit["powiadomiono"] \
            and teraz.timestamp() >= limit["sprawdz"] and not zdalny_kod.zajety():
        wrocily, kiedy = _sprawdz_limity(zadanie["model"])
        with _blokada:
            stan = _stan()
            if not stan["limit"]:
                return
            if wrocily:
                stan["limit"]["powiadomiono"] = True
                stan["zadanie"]["pid"] = _pid()
                _zapisz(stan)
                logger.info("[ROADMAPA] Limity wróciły.")
                _wyslij(f"✅ Limity Claude Code wróciły — kontynuujemy zadanie „{zadanie['tytul']}”?",
                        [[("▶️ Kontynuuj", f"rm|k|{stan['zadanie']['pid']}"),
                          ("⏸ Później", f"rm|l|{stan['zadanie']['pid']}")]])
            else:
                nastepne = (kiedy + datetime.timedelta(seconds=ZAPAS_PO_ODNOWIENIU_S)
                            if kiedy and kiedy > teraz else teraz + datetime.timedelta(seconds=SPRAWDZANIE_LIMITOW_S))
                stan["limit"]["sprawdz"] = nastepne.timestamp()
                _zapisz(stan)


def _petla():
    while not _zatrzymaj.is_set():
        try:
            krok()
        except Exception:
            logger.exception("[ROADMAPA] Błąd w wątku roadmapy")
        _zatrzymaj.wait(PRZERWA_PETLI_S)


def uruchom(kanal, restart=None, watek=True):
    """
    main.py, gdy działa most do Telegrama. kanal — wyslij(tekst, przyciski) -> id,
    edytuj(id, tekst, przyciski), usun_przyciski(id); restart — tylko dla /scal.
    """
    global _kanal, _restart, _watek
    _kanal, _restart = kanal, restart
    _zatrzymaj.clear()
    # Zadanie, które trwało, gdy Jarvis się zamknął — teraz jest przerwane.
    with _blokada:
        stan = _stan()
        zadanie = stan["zadanie"]
    if zadanie and zadanie.get("stan") in ("sprawdzam", "trwa"):
        _przerwij("Jarvis został zamknięty w trakcie zadania")
    if watek:
        _watek = threading.Thread(target=_petla, name="roadmapa", daemon=True)
        _watek.start()


def zatrzymaj():
    """Przy zamykaniu Jarvisa: koniec wątku i Claude Code w toku (zadanie zostaje przerwane)."""
    _zatrzymaj.set()
    if _proces is not None:
        zdalny_kod._zabij_drzewo(_proces.pid)
