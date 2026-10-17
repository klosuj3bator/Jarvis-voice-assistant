"""
pliki.py — praca z plikami na Twoje polecenie.

    create_folder    "zrób folder Projekty na pulpicie"
    create_file      "zrób na pulpicie notatkę zakupy z mlekiem i chlebem"
    list_files       "co mam na pulpicie?"
    open_folder      "otwórz folder Projekty"
    append_to_file   "dopisz do listy zakupów jajka"
    edit_file        "w liście zamień mleko na sok"
    move_files       "stwórz folder Faktury i wrzuć tam wszystkie PDF-y z pulpitu"
    copy_files       to samo, ale oryginały zostają

To NIE są notatki. "Zanotuj…" dalej trafia do dziennika notatek (notes.py);
tutaj powstają osobne pliki — gdy mówisz o pliku albo o miejscu ("na pulpicie").


GDZIE WOLNO
===========

Tylko w dozwolonych miejscach: domyślnie Pulpit i Dokumenty (gdzie naprawdę
leżą, pytamy Windows — bywają przeniesione do OneDrive) albo foldery
z JARVIS_DOZWOLONE_FOLDERY w .env, rozdzielone średnikiem. Model podaje
ścieżkę od nazwy miejsca: "Pulpit/Projekty/plan.md". Odrzucamy:

  - ".." w ścieżce — nawet gdyby wynik wypadał w środku; prościej i pewniej,
  - wszystko poza dozwolonymi miejscami, także przez skróty i dowiązania
    (porównujemy prawdziwe położenie, os.path.realpath),
  - folder Jarvisa i jego dane — leży na Pulpicie, a model nie może
    zmieniać własnego kodu ani .env z kluczami,
  - ukryte foldery i pliki (z kropką na początku: .git, .obsidian),
  - nazwy zastrzeżone w Windows (CON, NUL, COM1…) i niedozwolone znaki.

Tworzyć, dopisywać i zmieniać wolno tylko pliki .txt i .md. Przenosić
i kopiować — dowolne pliki (np. PDF-y), ale same pliki, nie całe foldery.
Folder otwieramy w Eksploratorze; pliku nie otwieramy nigdy — "otwarcie"
programu to jego uruchomienie.


CZEGO NIE MA
============

Usuwania. Żadne narzędzie nie kasuje plików ani folderów.

Czytania. Model nie dostaje treści plików — widzi tylko nazwy (list_files)
i przy edit_file same zmieniane linie. Plików z kluczami Jarvisa ani linii
wyglądających na hasło edit_file nie pokazuje i nie zmienia.


POTWIERDZENIA
=============

Część operacji czeka na Twoją zgodę:

  - nadpisanie istniejącego pliku (create_file, a przy przenoszeniu
    i kopiowaniu — plik o tej samej nazwie w folderze docelowym),
  - każda zmiana treści (edit_file) — najpierw podgląd, co się zmieni,
  - przeniesienie albo skopiowanie więcej niż PROG_ZGODY plików — najpierw lista.

Wtedy narzędzie niczego nie robi: zwraca opis i kod, a model pyta Cię
o zgodę. Dopiero gdy w NASTĘPNEJ wypowiedzi powiesz "tak", model woła
confirm_action z tym kodem. Jak kod pilnuje, żeby model nie "potwierdził
sam sobie" (i żeby nie zrobił tego mail) — opis w potwierdzenia.py; ten sam
mechanizm potwierdza dodawanie wydarzeń do kalendarza.
"""

import ctypes
import glob
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import unicodedata
import uuid

import akcje
import konfiguracja
import pamiec
import sciezki
from potwierdzenia import POTRZEBNE_POTWIERDZENIE, zaproponuj  # noqa: F401

logger = logging.getLogger(__name__)

ZMIENNA_MIEJSC = "JARVIS_DOZWOLONE_FOLDERY"
ROZSZERZENIA_TEKSTU = (".txt", ".md")
PROG_ZGODY = 5                       # "więcej niż kilka plików" — powyżej pytamy o zgodę
MAKS_TRESC = 100_000                 # znaków w tworzonym pliku
MAKS_PLIK_DO_ZMIANY = 1_000_000      # większych plików nie zmieniamy (to nie są notatki)
MAKS_POZYCJI = 60                    # tyle pozycji pokazuje list_files
MAKS_W_PODGLADZIE = 20               # tyle plików albo zmienionych linii wymieniamy z nazwy

_ZASTRZEZONE = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                *(f"lpt{i}" for i in range(1, 10))}
_NIEDOZWOLONE_ZNAKI = re.compile(r'[<>:"|?*\x00-\x1f]')

# Znane foldery Windows (SHGetKnownFolderPath) — Pulpit i Dokumenty.
_PULPIT = "B4BFCC3A-DB2C-424C-B029-7FE99A87C641"
_DOKUMENTY = "FDD39AD0-238F-46AF-ADB4-6C85480369C7"


class BladSciezki(Exception):
    """Ścieżka niedozwolona albo błędna — komunikat dla modelu i dla Ciebie."""


# ---------------------------------------------------------------
# Dozwolone miejsca i sprawdzanie ścieżek
# ---------------------------------------------------------------

def _znany_folder(guid, zapasowy):
    """Prawdziwe położenie Pulpitu albo Dokumentów (także po przeniesieniu do OneDrive)."""
    try:
        identyfikator = (ctypes.c_char * 16).from_buffer_copy(uuid.UUID(guid).bytes_le)
        wynik = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(identyfikator), 0, None,
                                                      ctypes.byref(wynik)) == 0:
            sciezka = wynik.value
            ctypes.windll.ole32.CoTaskMemFree(wynik)
            if sciezka:
                return sciezka
    except (AttributeError, OSError, ValueError):
        pass                                # nie Windows albo brak API — zwykła ścieżka
    return os.path.join(os.path.expanduser("~"), zapasowy)


def _normuj(tekst):
    """"Pulpit" == "pulpit" == "PULPIT"; bez polskich znaków."""
    tekst = unicodedata.normalize("NFKD", tekst.lower().replace("ł", "l"))
    return tekst.encode("ascii", "ignore").decode().strip()


def _klucz(sciezka):
    return os.path.normcase(os.path.realpath(sciezka))


def _w_srodku(sciezka, folder):
    """Czy sciezka leży w folderze (albo jest nim) — po prawdziwym położeniu."""
    sciezka, folder = _klucz(sciezka), _klucz(folder)
    try:
        return os.path.commonpath([sciezka, folder]) == folder
    except ValueError:                      # różne dyski
        return False


def miejsca():
    """
    Dozwolone miejsca: [(nazwa, pełna ścieżka, nazwy, po których model może
    je wskazać)]. Liczone przy każdym użyciu — zmiana .env działa od razu.
    """
    pulpit = _znany_folder(_PULPIT, "Desktop")
    dokumenty = _znany_folder(_DOKUMENTY, "Documents")
    znane = {_klucz(pulpit): ("Pulpit", {"pulpit", "desktop"}),
             _klucz(dokumenty): ("Dokumenty", {"dokumenty", "documents", "moje dokumenty"})}

    wpisane = [s.strip().strip('"') for s in os.getenv(ZMIENNA_MIEJSC, "").split(";") if s.strip()]
    foldery = [os.path.expandvars(os.path.expanduser(s)) for s in wpisane] or [pulpit, dokumenty]
    wynik = []
    for folder in foldery:
        if not os.path.isdir(folder):
            logger.warning("[PLIKI] %s: folder %r nie istnieje — pomijam.", ZMIENNA_MIEJSC, folder)
            continue
        nazwa, nazwy = znane.get(_klucz(folder), (os.path.basename(os.path.normpath(folder)), set()))
        wynik.append((nazwa, os.path.realpath(folder), nazwy | {_normuj(nazwa)}))
    return wynik


def _chronione():
    """Folder Jarvisa i jego dane — nawet gdy leżą w dozwolonym miejscu."""
    return {os.path.realpath(p) for p in (sciezki.KOD, sciezki.DANE, sciezki.UZYTKOWNIK)}


def _sprawdz_nazwe(nazwa):
    if nazwa.startswith("."):
        raise BladSciezki(f"„{nazwa}” to ukryty plik albo folder — tych nie ruszam.")
    if _NIEDOZWOLONE_ZNAKI.search(nazwa) or nazwa.endswith((" ", ".")):
        raise BladSciezki(f"„{nazwa}” zawiera znaki niedozwolone w nazwach plików.")
    if nazwa.split(".")[0].lower() in _ZASTRZEZONE:
        raise BladSciezki(f"„{nazwa}” to nazwa zastrzeżona w Windows.")


def rozwiaz(sciezka):
    """
    Ścieżka od modelu -> (pełna ścieżka, opis do komunikatów, np. "Pulpit/Projekty").
    Rzuca BladSciezki, gdy ścieżka jest niedozwolona (opis na górze modułu).
    """
    sciezka = (sciezka or "").strip().strip('"').strip()
    if not sciezka:
        raise BladSciezki("Podaj ścieżkę, np. Pulpit/Projekty.")
    czesci = [c for c in re.split(r"[\\/]+", sciezka) if c not in ("", ".")]
    if ".." in czesci:
        raise BladSciezki("Ścieżek z „..” nie przyjmuję — podaj ją od nazwy miejsca, np. Pulpit/Projekty.")
    dozwolone = miejsca()
    if not dozwolone:
        raise BladSciezki(f"Nie ma żadnego dozwolonego miejsca — sprawdź {ZMIENNA_MIEJSC} w .env.")

    if sciezka.startswith(("\\\\", "//")) or re.match(r"^[A-Za-z]:", sciezka):
        # Pełna ścieżka (C:\...) — wolno, jeśli leży w którymś z dozwolonych miejsc.
        if not re.match(r"^[A-Za-z]:[\\/]", sciezka):
            raise BladSciezki("Podaj pełną ścieżkę albo ścieżkę od nazwy miejsca, np. Pulpit/Projekty.")
        pelna = os.path.realpath(sciezka)
        miejsce = next((m for m in dozwolone if _w_srodku(pelna, m[1])), None)
        if miejsce is None:
            raise BladSciezki(f"{sciezka} leży poza dozwolonymi miejscami ({_lista_miejsc(dozwolone)}).")
        wzgledna = os.path.relpath(pelna, miejsce[1])
        czesci_wzgledne = [] if wzgledna == "." else wzgledna.split(os.sep)
    else:
        miejsce = next((m for m in dozwolone if _normuj(czesci[0]) in m[2]), None)
        if miejsce is None:
            raise BladSciezki(f"Nie znam miejsca „{czesci[0]}”. Wolno mi działać tylko tu: "
                              f"{_lista_miejsc(dozwolone)}.")
        czesci_wzgledne = czesci[1:]
        pelna = os.path.realpath(os.path.join(miejsce[1], *czesci_wzgledne))

    for nazwa in czesci_wzgledne:
        _sprawdz_nazwe(nazwa)
    # Prawdziwe położenie jeszcze raz: skrót albo dowiązanie mogłoby wyprowadzić poza miejsce.
    if not _w_srodku(pelna, miejsce[1]):
        raise BladSciezki("Ta ścieżka prowadzi poza dozwolone miejsce.")
    if any(_w_srodku(pelna, chroniony) for chroniony in _chronione()):
        raise BladSciezki("To folder Jarvisa albo jego dane — własnych plików nie ruszam.")
    return pelna, "/".join([miejsce[0], *czesci_wzgledne])


def _lista_miejsc(dozwolone=None):
    return ", ".join(nazwa for nazwa, _, _ in (dozwolone if dozwolone is not None else miejsca()))


def _plik_tekstowy(pelna, opis):
    if os.path.splitext(pelna)[1].lower() not in ROZSZERZENIA_TEKSTU:
        raise BladSciezki(f"{opis}: pracuję tylko na plikach tekstowych .txt i .md.")


# ---------------------------------------------------------------
# Operacje
# ---------------------------------------------------------------

def _zapisz_cale(pelna, tresc):
    """Zapis przez plik tymczasowy — przerwany zapis nie zostawi pół pliku."""
    tymczasowy = pelna + ".jarvis-tmp"
    with open(tymczasowy, "w", encoding="utf-8", newline="") as plik:
        plik.write(tresc)
    os.replace(tymczasowy, pelna)


def _czytaj(pelna):
    if os.path.getsize(pelna) > MAKS_PLIK_DO_ZMIANY:
        raise BladSciezki("Ten plik jest za duży, żebym go zmieniał.")
    with open(pelna, encoding="utf-8", errors="replace", newline="") as plik:
        return plik.read()


def _z_koncem_linii(tekst, nowa_linia="\n"):
    tekst = tekst.replace("\r\n", "\n").replace("\n", nowa_linia)
    return tekst if tekst.endswith(nowa_linia) else tekst + nowa_linia


def utworz_folder(sciezka):
    pelna, opis = rozwiaz(sciezka)
    if os.path.isdir(pelna):
        return f"Folder {opis} już jest — nic nie zmieniłem.", True
    if os.path.exists(pelna):
        return f"{opis} to plik, nie folder.", False
    os.makedirs(pelna)
    akcje.zapisz("Nowy folder")
    logger.info("[PLIKI] Nowy folder: %s", opis)
    return f"Utworzyłem folder {opis}.", True


def utworz_plik(sciezka, tresc, id_wypowiedzi):
    if not os.path.splitext(sciezka or "")[1]:
        sciezka = f"{sciezka}.txt"              # bez rozszerzenia — zwykły plik tekstowy
    pelna, opis = rozwiaz(sciezka)
    _plik_tekstowy(pelna, opis)
    tresc = tresc or ""
    if len(tresc) > MAKS_TRESC:
        return f"Za długa treść ({len(tresc)} znaków, limit {MAKS_TRESC}).", False
    if os.path.isdir(pelna):
        return f"{opis} to folder — podaj nazwę pliku.", False

    def zapisz(nadpisz):
        os.makedirs(os.path.dirname(pelna), exist_ok=True)
        if nadpisz:
            _zapisz_cale(pelna, _z_koncem_linii(tresc))
        else:
            # "x" = tylko nowy plik; gdyby w międzyczasie ktoś go utworzył — błąd, nie nadpisanie.
            with open(pelna, "x", encoding="utf-8", newline="") as plik:
                plik.write(_z_koncem_linii(tresc))
        _zarejestruj(pelna)          # do przeszukiwania notatek (opis przy PLIK_REJESTRU)
        akcje.zapisz("Nadpisałem plik" if nadpisz else "Nowy plik")
        logger.info("[PLIKI] %s: %s", "Nadpisany" if nadpisz else "Nowy plik", opis)
        return f"{'Nadpisałem' if nadpisz else 'Utworzyłem'} plik {opis}.", True

    if os.path.exists(pelna):
        return zaproponuj(f"Plik {opis} już istnieje. Nadpisanie zastąpi całą jego treść nową.",
                           lambda: zapisz(nadpisz=True), id_wypowiedzi)
    return zapisz(nadpisz=False)


def lista(sciezka=None):
    if not (sciezka or "").strip():
        return ("Dozwolone miejsca: " + "; ".join(f"{n} ({p})" for n, p, _ in miejsca())
                + ". Podaj jedno z nich, żeby zobaczyć zawartość."), True
    pelna, opis = rozwiaz(sciezka)
    if not os.path.isdir(pelna):
        return f"Nie ma folderu {opis}.", False
    chronione = _chronione()
    foldery, pliki_ = [], []
    for wpis in sorted(os.scandir(pelna), key=lambda w: w.name.lower()):
        if wpis.name.startswith(".") or any(_w_srodku(wpis.path, c) for c in chronione):
            continue
        if wpis.is_dir():
            foldery.append(f"📁 {wpis.name}/")
        else:
            stan = wpis.stat()
            kiedy = time.strftime("%d.%m.%Y", time.localtime(stan.st_mtime))
            pliki_.append(f"{wpis.name} ({_rozmiar(stan.st_size)}, {kiedy})")
    pozycje = foldery + pliki_
    akcje.zapisz("Lista plików")
    if not pozycje:
        return f"Folder {opis} jest pusty.", True
    pokazane = pozycje[:MAKS_POZYCJI]
    reszta = f"\n…i {len(pozycje) - len(pokazane)} innych" if len(pozycje) > len(pokazane) else ""
    return (f"{opis}: {_ile(len(foldery), 'folder', 'foldery', 'folderów')}, "
            f"{_ile(len(pliki_), 'plik', 'pliki', 'plików')}:\n" + "\n".join(pokazane) + reszta), True


def _ile(liczba, jeden, kilka, wiele):
    """Polska odmiana: 1 plik, 2 pliki, 5 plików, 22 pliki, 12 plików."""
    if liczba == 1:
        return f"1 {jeden}"
    if liczba % 10 in (2, 3, 4) and liczba % 100 not in (12, 13, 14):
        return f"{liczba} {kilka}"
    return f"{liczba} {wiele}"


def _rozmiar(bajty):
    for jednostka in ("B", "KB", "MB", "GB"):
        if bajty < 1024 or jednostka == "GB":
            return f"{bajty:.0f} {jednostka}" if jednostka == "B" else f"{bajty:.1f} {jednostka}"
        bajty /= 1024


def _otworz(pelna):
    os.startfile(pelna)                      # folder -> Eksplorator


def otworz_folder(sciezka):
    pelna, opis = rozwiaz(sciezka)
    if not os.path.isdir(pelna):
        # Plików celowo nie otwieramy: "otwarcie" programu to jego uruchomienie.
        return f"{opis} to nie folder — otwieram tylko foldery.", False
    _otworz(pelna)
    akcje.zapisz("Otworzyłem folder")
    return f"Otworzyłem {opis} w Eksploratorze.", True


def dopisz(sciezka, tekst):
    pelna, opis = rozwiaz(sciezka)
    _plik_tekstowy(pelna, opis)
    if not os.path.isfile(pelna):
        return f"Nie ma pliku {opis}. Jeśli ma powstać, użyj create_file.", False
    tekst = (tekst or "").strip("\n")
    if not tekst.strip():
        return "Nie ma czego dopisać.", False
    tresc = _czytaj(pelna)
    nowa_linia = "\r\n" if "\r\n" in tresc else "\n"
    poczatek = nowa_linia if tresc and not tresc.endswith(("\n", "\r")) else ""
    with open(pelna, "a", encoding="utf-8", newline="") as plik:
        plik.write(poczatek + _z_koncem_linii(tekst, nowa_linia))
    akcje.zapisz("Dopisałem do pliku")
    logger.info("[PLIKI] Dopisane do %s", opis)
    return f"Dopisałem do {opis}.", True


def zmien(sciezka, stary, nowy, id_wypowiedzi):
    pelna, opis = rozwiaz(sciezka)
    _plik_tekstowy(pelna, opis)
    if not os.path.isfile(pelna):
        return f"Nie ma pliku {opis}.", False
    if not (stary or "").strip():
        return "Podaj, jaki fragment zmienić.", False
    tresc = _czytaj(pelna)
    # Treści plików modelowi NIE pokazujemy (poza podglądem samych zmienianych
    # linii). Model nie ma narzędzia do czytania plików — i dobrze: podsunięte
    # mu polecenie (np. z notatki) nie wyciągnie z dysku czyjegoś klucza.
    if any(wartosc in tresc for wartosc in konfiguracja.tajne_wartosci().values()):
        return f"{opis} zawiera klucz Jarvisa — tego pliku nie zmieniam.", False
    wzorzec = re.compile(re.escape(stary), re.IGNORECASE)     # "Mleko" też pasuje do "mleko"
    trafienia = len(wzorzec.findall(tresc))
    if not trafienia:
        return (f"W {opis} nie ma fragmentu „{stary}” (wielkość liter bez znaczenia). "
                "Zapytaj użytkownika, jak dokładnie brzmi to, co chce zmienić."), False
    nowa_tresc = wzorzec.sub(lambda _: nowy or "", tresc)
    skrot = hashlib.sha1(tresc.encode("utf-8")).hexdigest()

    # Podgląd: zmienione linie przed i po.
    przed, po = tresc.splitlines(), nowa_tresc.splitlines()
    zmiany = [(s, n) for s, n in zip(przed, po) if s != n]
    if len(przed) != len(po):                 # zmiana dodała albo usunęła linie
        zmiany = [("\n".join(przed), "\n".join(po))] if len(tresc) < 2000 else zmiany
    if any(pamiec.wyglada_na_sekret(linia) for para in zmiany for linia in para):
        return (f"Zmieniane linie w {opis} wyglądają na hasło, klucz albo kod — takich nie "
                "pokazuję ani nie zmieniam. Użytkownik może to zrobić sam."), False
    podglad = "\n".join(f"  było: {s}\n  będzie: {n}" for s, n in zmiany[:MAKS_W_PODGLADZIE])
    if len(zmiany) > MAKS_W_PODGLADZIE:
        podglad += f"\n  …i {len(zmiany) - MAKS_W_PODGLADZIE} innych linii"

    def zapisz():
        if hashlib.sha1(_czytaj(pelna).encode("utf-8")).hexdigest() != skrot:
            return ("Plik zmienił się od podglądu — nie zapisuję. Zaproponuj zmianę jeszcze "
                    "raz, na aktualnej treści."), False
        _zapisz_cale(pelna, nowa_tresc)
        akcje.zapisz("Zmieniłem plik")
        logger.info("[PLIKI] Zmieniony plik %s (%d miejsc).", opis, trafienia)
        return f"Zapisałem zmiany w {opis} ({trafienia} {'miejsce' if trafienia == 1 else 'miejsca'}).", True

    return zaproponuj(f"Zmiana w {opis} — {trafienia}× „{stary}” → „{nowy or ''}”:\n{podglad}",
                       zapisz, id_wypowiedzi)


def _wybierz_pliki(wzorce):
    """Lista ścieżek albo wzorców ("Pulpit/*.pdf") -> pełne ścieżki samych plików."""
    if isinstance(wzorce, str):
        wzorce = [wzorce]
    wybrane = {}
    for wzorzec in wzorce or []:
        folder, nazwa = os.path.split(re.sub(r"[\\/]+", "/", (wzorzec or "").strip()))
        if any(znak in nazwa for znak in "*?["):
            pelny_folder, opis_folderu = rozwiaz(folder)
            if not os.path.isdir(pelny_folder):
                raise BladSciezki(f"Nie ma folderu {opis_folderu}.")
            for sciezka in sorted(glob.glob(os.path.join(glob.escape(pelny_folder), nazwa))):
                if os.path.isfile(sciezka) and not os.path.basename(sciezka).startswith("."):
                    pelna, _ = rozwiaz(sciezka)            # ta sama kontrola dla każdego pliku
                    wybrane[_klucz(pelna)] = pelna
        else:
            pelna, opis = rozwiaz(wzorzec)
            if os.path.isdir(pelna):
                raise BladSciezki(f"{opis} to folder — przenoszę i kopiuję tylko pliki "
                                  "(np. „{opis}/*” to wszystkie pliki z niego).")
            if not os.path.isfile(pelna):
                raise BladSciezki(f"Nie ma pliku {opis}.")
            wybrane[_klucz(pelna)] = pelna
    return list(wybrane.values())


def przenies(wzorce, cel, id_wypowiedzi, kopiuj=False):
    """move_files / copy_files. Folder docelowy powstaje, jeśli go nie ma."""
    czynnosc = ("skopiować", "Skopiowałem", "Skopiowane") if kopiuj else ("przenieść", "Przeniosłem", "Przeniesione")
    pelny_cel, opis_celu = rozwiaz(cel)
    if os.path.isfile(pelny_cel):
        return f"{opis_celu} to plik — podaj folder docelowy.", False
    zrodla = _wybierz_pliki(wzorce)
    plan = [(z, os.path.join(pelny_cel, os.path.basename(z))) for z in zrodla
            if _klucz(os.path.dirname(z)) != _klucz(pelny_cel)]          # już tam leży — pomijamy
    if not plan:
        return f"Nie ma plików do {czynnosc[0]} (nic nie pasuje albo już są w {opis_celu}).", True
    nazwy = [os.path.basename(z) for z, _ in plan]
    powtorzone = sorted({n for n in nazwy if nazwy.count(n) > 1})
    if powtorzone:
        return (f"Kilka plików ma tę samą nazwę ({', '.join(powtorzone)}) — w jednym folderze "
                "nie zmieszczą się obok siebie. Wybierz je dokładniej."), False
    istniejace = [d for _, d in plan if os.path.exists(d)]

    def zrob():
        os.makedirs(pelny_cel, exist_ok=True)
        zrobione, pominiete = 0, []
        for zrodlo, docelowy in plan:
            if not os.path.isfile(zrodlo):
                pominiete.append(f"{os.path.basename(zrodlo)} (zniknął)")
                continue
            # Nadpisujemy tylko to, co było na liście w chwili Twojej zgody.
            if os.path.exists(docelowy) and docelowy not in istniejace:
                pominiete.append(f"{os.path.basename(zrodlo)} (w międzyczasie pojawił się taki w celu)")
                continue
            if kopiuj:
                shutil.copy2(zrodlo, docelowy)
            elif os.path.exists(docelowy):
                shutil.copy2(zrodlo, docelowy)      # nadpisanie potwierdzone
                os.remove(zrodlo)                    # to wciąż przeniesienie: oryginał trafił do celu
            else:
                shutil.move(zrodlo, docelowy)
            # Plik od Jarvisa zostaje "od Jarvisa" w nowym miejscu (przy kopii — w obu).
            if _w_rejestrze(zrodlo):
                _zarejestruj(docelowy, zamiast=None if kopiuj else zrodlo)
            zrobione += 1
        akcje.zapisz(f"{czynnosc[2]} pliki ({zrobione})")
        logger.info("[PLIKI] %s %d plików do %s.", czynnosc[2], zrobione, opis_celu)
        komunikat = f"{czynnosc[1]} {_ile(zrobione, 'plik', 'pliki', 'plików')} do {opis_celu}."
        if pominiete:
            komunikat += " Pominięte: " + ", ".join(pominiete) + "."
        return komunikat, True

    if len(plan) > PROG_ZGODY or istniejace:
        lista_ = "\n".join(f"  - {n}" for n in nazwy[:MAKS_W_PODGLADZIE])
        if len(nazwy) > MAKS_W_PODGLADZIE:
            lista_ += f"\n  …i {len(nazwy) - MAKS_W_PODGLADZIE} innych"
        uwaga = (f"\nW {opis_celu} są już pliki o tych nazwach — zgoda je NADPISZE: "
                 + ", ".join(os.path.basename(d) for d in istniejace) + "." if istniejace else "")
        return zaproponuj(f"Chcesz {czynnosc[0]} {_ile(len(plan), 'plik', 'pliki', 'plików')} "
                           f"do {opis_celu}:\n{lista_}{uwaga}",
                           zrob, id_wypowiedzi)
    return zrob()


# ---------------------------------------------------------------
# Pliki utworzone przez Jarvisa — dla przeszukiwania notatek
# ---------------------------------------------------------------
#
# search_notes (indeks_notatek.py) przeszukuje też pliki tekstowe, które
# Jarvis sam utworzył — "lista zakupów na pulpicie" to też notatka. Ale TYLKO
# te: reszty Twojego Pulpitu i Dokumentów indeks nie czyta. Dlatego
# zapisujemy, co Jarvis utworzył, i dokąd potem to przeniósł. Sam rejestr to
# lista ścieżek (nazwy plików są prywatne — plik leży poza gitem).

PLIK_REJESTRU = sciezki.dane("pliki_jarvisa.json")
_blokada_rejestru = threading.Lock()


def _wczytaj_rejestr():
    try:
        with open(PLIK_REJESTRU, encoding="utf-8") as plik:
            return [s for s in json.load(plik).get("pliki", []) if isinstance(s, str)]
    except (OSError, ValueError, AttributeError):
        return []


def _zapisz_rejestr(lista):
    try:
        with open(PLIK_REJESTRU, "w", encoding="utf-8") as plik:
            json.dump({"pliki": lista}, plik, ensure_ascii=False, indent=1)
    except OSError:
        logger.warning("[PLIKI] Nie zapisałem rejestru plików Jarvisa.", exc_info=True)


def _w_rejestrze(sciezka):
    klucz = _klucz(sciezka)
    with _blokada_rejestru:
        return any(_klucz(s) == klucz for s in _wczytaj_rejestr())


def _zarejestruj(sciezka, zamiast=None):
    """Dopisuje plik do rejestru; przy przeniesieniu zastępuje starą ścieżkę (zamiast)."""
    nowa = os.path.realpath(sciezka)
    with _blokada_rejestru:
        lista = _wczytaj_rejestr()
        if zamiast is not None:
            lista = [s for s in lista if _klucz(s) != _klucz(zamiast)]
        if all(_klucz(s) != _klucz(nowa) for s in lista):
            lista.append(nowa)
        _zapisz_rejestr(lista)


def utworzone_pliki():
    """
    Pliki tekstowe od Jarvisa, które nadal istnieją i leżą w dozwolonych
    miejscach: [(pełna ścieżka, opis, np. "Pulpit/zakupy.md")].

    Pliki, których już nie ma (usunąłeś je sam), wypadają z rejestru. Te,
    które wypadły z dozwolonych miejsc (zmiana JARVIS_DOZWOLONE_FOLDERY),
    zostają w rejestrze, ale nie trafiają do wyszukiwania.
    """
    with _blokada_rejestru:
        lista = _wczytaj_rejestr()
        istniejace = [s for s in lista if os.path.isfile(s)]
        if len(istniejace) != len(lista):
            _zapisz_rejestr(istniejace)
    wynik = []
    for sciezka in istniejace:
        if os.path.splitext(sciezka)[1].lower() not in ROZSZERZENIA_TEKSTU:
            continue
        try:
            wynik.append(rozwiaz(sciezka))
        except BladSciezki:
            continue
    return wynik


# ---------------------------------------------------------------
# Wejście z agent.py
# ---------------------------------------------------------------

NARZEDZIA_PLIKOW = {"create_folder", "create_file", "list_files", "open_folder", "append_to_file",
                    "edit_file", "move_files", "copy_files"}


def wykonaj(nazwa, parametry, tekst_uzytkownika, id_wypowiedzi):
    """
    Jedno narzędzie plików. Zwraca (komunikat dla modelu, ok).

    id_wypowiedzi — do potwierdzeń: zgoda musi paść w KOLEJNEJ wypowiedzi
    (potwierdzenia.py); tekst_uzytkownika zostaje dla zgodności wywołania.
    """
    p = parametry or {}
    try:
        if nazwa == "create_folder":
            return utworz_folder(p.get("sciezka"))
        if nazwa == "create_file":
            return utworz_plik(p.get("sciezka"), p.get("tresc"), id_wypowiedzi)
        if nazwa == "list_files":
            return lista(p.get("sciezka"))
        if nazwa == "open_folder":
            return otworz_folder(p.get("sciezka"))
        if nazwa == "append_to_file":
            return dopisz(p.get("sciezka"), p.get("tekst"))
        if nazwa == "edit_file":
            return zmien(p.get("sciezka"), p.get("stary_tekst"), p.get("nowy_tekst"), id_wypowiedzi)
        if nazwa in ("move_files", "copy_files"):
            return przenies(p.get("pliki"), p.get("cel"), id_wypowiedzi, kopiuj=nazwa == "copy_files")
    except BladSciezki as e:
        return str(e), False
    except OSError as e:
        logger.warning("[PLIKI] %s: %s", nazwa, e)
        return f"Nie udało się: {e.strerror or e}.", False
    return f"Nieznane narzędzie plików: {nazwa}", False
