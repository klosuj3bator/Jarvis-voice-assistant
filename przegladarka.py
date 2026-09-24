"""
przegladarka.py — otwieranie stron i wyszukiwanie w Operze GX.

"Odpal Operę i włącz Gmaila", "wpisz w operze przepis na pizzę",
"pokaż mi na YouTube lofi" — to wszystko sprowadza się do jednego:
uruchomić opera.exe z adresem strony jako argumentem.


DLACZEGO ADRES W ARGUMENCIE, A NIE "PISANIE" PO KLAWIATURZE
===========================================================

Kusi, żeby Jarvis naprawdę "pisał": kliknął w pasek adresu, wystukał
tekst i nacisnął Enter. To jednak kruche — jeśli w tej sekundzie okno
straci fokus (wyskoczy powiadomienie, ruszysz myszką), tekst wpadnie
w zupełnie inny program, a Enter może tam coś wysłać.

Przeglądarka rozumie adres podany w linii poleceń:

    opera.exe https://mail.google.com

Jeśli Opera jest zamknięta — uruchomi się od razu na tej stronie.
Jeśli jest otwarta — dostanie nową kartę. Efekt ten sam, co przy
wpisaniu adresu ręcznie, tylko bez ryzyka pisania nie tam, gdzie trzeba.
Wyszukiwanie frazy to po prostu adres Google z tą frazą w środku.


BEZPIECZEŃSTWO
==============

Adres wybiera model językowy, a model czyta też wyniki wyszukiwania
z internetu — czyli tekst, który mógł napisać ktokolwiek. Dlatego nie
ufamy mu na słowo:

  1. TYLKO http:// I https://. Windows ma dziesiątki innych "protokołów"
     (file:, ms-settings:, ms-msdt: i podobne), które zamiast strony
     otwierają pliki albo programy systemowe. Przez część z nich były
     w przeszłości ataki. Przeglądarka do przeglądania — nic więcej.

  2. PORZĄDNA NAZWA SERWERA. Odrzucamy adresy typu
     https://google.com@zla-strona.pl — wyglądają jak Google, a prowadzą
     gdzie indziej (część przed @ to "login", nie adres).

  3. BEZ POWŁOKI. Operę uruchamiamy listą argumentów, a nie przez cmd,
     więc żaden znak w adresie nie zostanie potraktowany jak polecenie.
"""

import ctypes
import logging
import os
import re
import subprocess
import sys
import urllib.parse

import app_launcher

logger = logging.getLogger(__name__)

# Typowe miejsca instalacji. Instalator Opery GX domyślnie wrzuca ją do
# AppData użytkownika — i tam jest na tym komputerze.
SCIEZKI_OPERY = [
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Opera GX\opera.exe"),
    os.path.expandvars(r"%ProgramFiles%\Opera GX\opera.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Opera GX\opera.exe"),
]

WYSZUKIWARKA = "https://www.google.com/search?q="

# Znaki, które w adresie mają znaczenie i muszą zostać nietknięte.
# Wszystko inne (spacje, cudzysłowy, polskie litery) zamieniamy na %XX.
ZNAKI_ADRESU = "/:?#[]@!$&'()*+,;=%~-._"

# Nazwa serwera: litery (także polskie), cyfry, kropki, myślniki, opcjonalny port.
# Celowo BEZ "@", spacji i ukośników — patrz punkt 2 w opisie modułu.
POPRAWNY_SERWER = re.compile(r"[\w.-]+(:\d{1,5})?")

# Pozwala oknu, które uruchamiamy, wyjść na pierwszy plan.
# Wyjaśnienie niżej, w _pozwol_na_pierwszy_plan().
ASFW_ANY = -1

_sciezka_opery = None


def _znajdz_opere():
    """
    Zwraca ścieżkę do opera.exe albo None.

    Najpierw typowe miejsca instalacji (natychmiast), potem Menu Start
    (wolniej, ale znajdzie Operę zainstalowaną gdziekolwiek). Wynik
    zapamiętujemy, żeby nie szukać przy każdej stronie.
    """
    global _sciezka_opery

    if _sciezka_opery and os.path.isfile(_sciezka_opery):
        return _sciezka_opery

    for sciezka in SCIEZKI_OPERY:
        if os.path.isfile(sciezka):
            _sciezka_opery = sciezka
            return sciezka

    _, sciezka = app_launcher.szukaj_w_menu_start("opera gx")
    if sciezka:
        _sciezka_opery = sciezka
    return sciezka


def _przygotuj_adres(adres):
    """
    Sprawdza adres i doprowadza go do postaci, którą można bezpiecznie oddać
    przeglądarce.

    Zwraca: (adres, None) gdy wszystko w porządku,
            (None, opis_problemu) gdy adres odrzucamy.
    """
    adres = adres.strip()

    # "mail.google.com" bez https:// na początku — dopisujemy.
    if "://" not in adres:
        adres = "https://" + adres

    czesci = urllib.parse.urlsplit(adres)

    if czesci.scheme.lower() not in ("http", "https"):
        return None, (
            f"Odrzucam adres {adres!r}: otwieram tylko strony http i https."
        )

    # Przy okazji łapie to próby przemycenia innych protokołów bez "://":
    # "ms-settings:display" zamienia się w "https://ms-settings:display",
    # a "ms-settings" nie jest poprawną nazwą serwera (brak kropki).
    serwer = czesci.netloc
    if (not POPRAWNY_SERWER.fullmatch(serwer)
            or ("." not in serwer and not serwer.startswith("localhost"))):
        return None, (
            f"Odrzucam adres {adres!r}: to nie wygląda na adres strony. "
            "Podaj pełną domenę, np. mail.google.com."
        )

    adres = urllib.parse.urlunsplit((
        czesci.scheme.lower(),
        serwer,
        urllib.parse.quote(czesci.path, safe=ZNAKI_ADRESU),
        urllib.parse.quote(czesci.query, safe=ZNAKI_ADRESU),
        urllib.parse.quote(czesci.fragment, safe=ZNAKI_ADRESU),
    ))
    return adres, None


def _pozwol_na_pierwszy_plan():
    """
    Windows pilnuje, żeby programy nie wyskakiwały przed okno, w którym akurat
    piszesz. Otwarta już Opera, dostając nową kartę "z zewnątrz", zwykle nie
    może sama wyjść na wierzch — tylko mruga na pasku zadań. Przy HUD-zie na
    cały ekran wyglądałoby to tak, jakby nic się nie stało.

    AllowSetForegroundWindow oddaje to prawo dalej: skoro Jarvis jest teraz
    oknem, z którym rozmawiasz, pozwala Operze zająć jego miejsce.
    Jeśli Jarvis sam nie jest na pierwszym planie, wywołanie nic nie zmienia —
    dlatego ignorujemy jego wynik.
    """
    if sys.platform == "win32":
        ctypes.windll.user32.AllowSetForegroundWindow(ASFW_ANY)


def otworz_strone(adres=None, fraza=None):
    """
    GŁÓWNE WEJŚCIE TEGO MODUŁU — to woła agent.py.

    adres — konkretna strona, np. "https://mail.google.com" albo "youtube.com"
    fraza — tekst do wyszukania w Google, np. "przepis na pizzę"

    Wystarczy jedno z dwóch. Gdy podane oba, wygrywa adres.

    Zwraca: (komunikat, czy się udało).
    """
    if adres and adres.strip():
        adres, problem = _przygotuj_adres(adres)
        if problem:
            logger.warning(problem)
            return problem, False
    elif fraza and fraza.strip():
        # quote_plus zamienia spacje na "+" i koduje polskie litery,
        # dokładnie tak, jak robi to sama wyszukiwarka.
        adres = WYSZUKIWARKA + urllib.parse.quote_plus(fraza.strip())
    else:
        return "Nie podano ani adresu, ani frazy do wyszukania.", False

    _pozwol_na_pierwszy_plan()

    opera = _znajdz_opere()
    if opera:
        try:
            # Lista argumentów zamiast jednego napisu = bez powłoki (cmd).
            # Popen nie czeka na zamknięcie przeglądarki — od razu wracamy.
            subprocess.Popen([opera, adres])
            logger.info("Opera GX: %s", adres)
            return f"Otworzyłem w Operze GX: {adres}", True
        except OSError as e:
            logger.error("Nie udało się uruchomić Opery (%s): %s", opera, e)

    # Awaryjnie: domyślna przeglądarka systemu. Bezpieczne, bo adres
    # przeszedł już kontrolę wyżej — to na pewno zwykła strona http(s).
    logger.warning("Nie znalazłem Opery GX — otwieram w domyślnej przeglądarce.")
    try:
        os.startfile(adres)
    except OSError as e:
        return f"Nie udało się otworzyć strony: {e}", False
    return (
        f"Nie znalazłem Opery GX, więc otworzyłem w domyślnej przeglądarce: {adres}",
        True,
    )


# --- Test: `python przegladarka.py mail.google.com` albo `python przegladarka.py "coś do wyszukania"` ---
if __name__ == "__main__":
    from logging_setup import skonfiguruj_logowanie

    skonfiguruj_logowanie(logging.INFO)

    tekst = " ".join(sys.argv[1:]) or "mail.google.com"
    # Spacja w środku = raczej fraza niż adres.
    if " " in tekst.strip():
        print(otworz_strone(fraza=tekst))
    else:
        print(otworz_strone(adres=tekst))
