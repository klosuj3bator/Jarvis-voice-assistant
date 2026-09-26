"""
stan_komputera.py — "jak się ma komputer?": obciążenie, procesy, pobierania
i zrzut ekranu.

Wszystkie funkcje TYLKO CZYTAJĄ. Nic tu niczego nie zamyka, nie kasuje ani
nie zmienia — do tego są app_launcher.py i system_control.py. Dzięki temu
te narzędzia można dać modelowi bez żadnych potwierdzeń: najgorsze, co może
się stać, to zbędny odczyt.

Każda funkcja zwraca gotowy tekst dla modelu, a model wybiera z niego to,
o co pytałeś. Liczby podajemy zaokrąglone — "83 procent" brzmi na głos
lepiej niż "83,47".
"""

import ctypes
import difflib
import io
import logging
import os
import re
import time
import uuid

import psutil

import czujniki

logger = logging.getLogger(__name__)

# Tyle najbardziej wymagających programów pokazujemy w zestawieniu.
ILE_NAJWIEKSZYCH = 5

# Jak długo mierzymy zużycie procesora przez programy. Procesor liczy się
# jako różnica między dwoma odczytami — pojedynczy odczyt nic nie mówi.
PROBKA_PROCESOW_S = 1.0

# Jak długo obserwujemy pobierane pliki, żeby stwierdzić, czy rosną.
PROBKA_POBIERAN_S = 1.0

# Końcówki plików, które przeglądarki dokładają w trakcie pobierania.
#   .crdownload — Chrome, Edge i Opera (wszystkie na silniku Chromium)
#   .opdownload — starsze wersje Opery
#   .part       — Firefox
#   .partial    — stary Edge
#   .tmp        — różne programy; plik tymczasowy tuż przed zapisem
KONCOWKI_W_TOKU = (".crdownload", ".opdownload", ".part", ".partial", ".tmp")

# Potoczne nazwy programów różnią się od nazw ich plików .exe.
# Tylko przypadki, których samo podobieństwo nazw nie wyłapie.
ALIASY_PROGRAMOW = {
    "word": "winword",
    "excel": "excel",
    "powerpoint": "powerpnt",
    "vs code": "code",
    "visual studio code": "code",
    "opera gx": "opera",
    "eksplorator": "explorer",
    "menedżer zadań": "taskmgr",
}


def _liczba(wartosc, miejsca=1):
    """Polski zapis liczby: przecinek zamiast kropki."""
    return f"{wartosc:.{miejsca}f}".replace(".", ",")


def _rozmiar(bajty):
    """1536 -> "1,5 KB" — rozmiar czytelny dla człowieka."""
    for jednostka in ("B", "KB", "MB", "GB"):
        if bajty < 1024 or jednostka == "GB":
            return f"{_liczba(bajty, 0 if jednostka == 'B' else 1)} {jednostka}"
        bajty /= 1024


# ---------------------------------------------------------------
# system_status
# ---------------------------------------------------------------

def stan_systemu():
    """
    Procesor, pamięć, karta graficzna i wolne miejsce na dyskach.

    Zwraca: (komunikat, czy_się_udało).
    """
    linie = []

    # interval=0.5: procesor mierzymy przez pół sekundy. Bez tego pierwszy
    # odczyt zwraca zero, bo nie ma się do czego porównać.
    cpu = psutil.cpu_percent(interval=0.5)
    linie.append(f"Procesor: {cpu:.0f}% ({psutil.cpu_count()} wątków).")

    ram = psutil.virtual_memory()
    linie.append(f"Pamięć RAM: {ram.percent:.0f}% zajęte "
                 f"({_rozmiar(ram.used)} z {_rozmiar(ram.total)}).")

    karta = czujniki.karta_graficzna()
    if karta:
        opis = (f"Karta graficzna {karta['nazwa']}: obciążenie {karta['obciazenie']}%, "
                f"pamięć {karta['pamiec_uzyta_mb'] / 1024:.1f} z "
                f"{karta['pamiec_calkowita_mb'] / 1024:.1f} GB").replace(".", ",")
        if karta["temperatura"] is not None:
            opis += f", temperatura {karta['temperatura']:.0f}°C (limit {karta['limit']:.0f}°C)"
        linie.append(opis + ".")
    else:
        linie.append("Karta graficzna: brak odczytu (brak karty NVIDIA).")

    # Tylko dyski wbudowane ("fixed") — pendrive'ów i napędów sieciowych
    # nie liczymy, bo potrafią zawiesić odczyt na kilka sekund.
    for dysk in psutil.disk_partitions(all=False):
        if "fixed" not in dysk.opts:
            continue
        try:
            uzycie = psutil.disk_usage(dysk.mountpoint)
        except OSError:
            continue
        wolne_proc = 100 - uzycie.percent
        litera = dysk.device.rstrip("\\")        # "C:\" -> "C:"
        opis = (f"Dysk {litera} wolne {_rozmiar(uzycie.free)} "
                f"z {_rozmiar(uzycie.total)} ({wolne_proc:.0f}%)")
        # Poniżej ~10% wolnego Windows zaczyna mieć kłopoty z aktualizacjami
        # i pamięcią wirtualną — to warto powiedzieć, nawet jeśli nikt nie pytał.
        if wolne_proc < 10:
            opis += " — BARDZO MAŁO MIEJSCA"
        linie.append(opis + ".")

    return "\n".join(linie), True


# ---------------------------------------------------------------
# running_processes
# ---------------------------------------------------------------

def _zmierz_procesy():
    """
    Mierzy wszystkie programy i sumuje je po nazwie.

    Sumowanie jest ważne: przeglądarka to kilkadziesiąt osobnych procesów
    (po jednym na kartę). Pokazane osobno, każdy wyglądałby niewinnie —
    razem potrafią zajmować najwięcej pamięci w całym systemie.

    Zwraca: {nazwa: [cpu_%, ram_bajty, liczba_procesow]}
    """
    procesy = []
    for proces in psutil.process_iter(["name"]):
        try:
            proces.cpu_percent(None)        # pierwszy odczyt: punkt startowy
            procesy.append(proces)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    time.sleep(PROBKA_PROCESOW_S)

    # psutil liczy procent JEDNEGO rdzenia — program zajmujący dwa rdzenie
    # w pełni ma 200%. Dzielimy przez liczbę wątków, żeby dostać udział
    # w całym procesorze, taki jak w Menedżerze zadań.
    watki = psutil.cpu_count() or 1
    suma = {}

    for proces in procesy:
        # PID 0 to "System Idle Process" — jego "zużycie" to w rzeczywistości
        # BEZCZYNNOŚĆ procesora. Bez pominięcia zawsze wygrywałby ranking.
        if proces.pid == 0:
            continue
        try:
            nazwa = re.sub(r"\.exe$", "", proces.info["name"] or "?", flags=re.I)
            cpu = proces.cpu_percent(None) / watki
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        try:
            ram = proces.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            ram = 0     # procesy systemowe nie zdradzają pamięci — liczymy bez niej

        wpis = suma.setdefault(nazwa, [0.0, 0, 0])
        wpis[0] += cpu
        wpis[1] += ram
        wpis[2] += 1

    return suma


def _pasujace(szukane, nazwy):
    """Nazwy procesów pasujące do nazwy programu podanej przez użytkownika."""
    szukane = (szukane or "").lower().strip()
    szukane = ALIASY_PROGRAMOW.get(szukane, szukane)
    szukane = re.sub(r"\.exe$|\s+", "", szukane)
    if not szukane:
        return []

    pasujace = []
    for nazwa in nazwy:
        prosta = nazwa.lower().replace(" ", "")
        zawiera = szukane in prosta                                  # "opera" w "opera"
        zawarta = len(prosta) >= 4 and prosta in szukane             # "spotify" w "spotifymusic"
        podobna = difflib.SequenceMatcher(None, szukane, prosta).ratio() >= 0.8   # literówki
        if zawiera or zawarta or podobna:
            pasujace.append(nazwa)
    return pasujace


def procesy(nazwa=None):
    """
    Czy dany program działa + programy zużywające najwięcej zasobów.

    nazwa — opcjonalnie: o jaki program pytasz ("spotify", "discord")

    Zwraca: (komunikat, czy_się_udało).
    """
    suma = _zmierz_procesy()
    linie = []

    if nazwa:
        znalezione = _pasujace(nazwa, suma)
        if znalezione:
            for n in znalezione:
                cpu, ram, ile = suma[n]
                linie.append(f"{n} DZIAŁA: {ile} proces(ów), {_rozmiar(ram)} RAM, "
                             f"{cpu:.0f}% procesora.")
        else:
            linie.append(f"Program {nazwa!r} NIE działa.")
            podobne = difflib.get_close_matches(nazwa.lower(), [n.lower() for n in suma],
                                                n=3, cutoff=0.6)
            if podobne:
                linie.append(f"Podobne działające: {', '.join(podobne)}.")
        return "\n".join(linie), True

    wedlug_cpu = sorted(suma.items(), key=lambda x: x[1][0], reverse=True)
    wedlug_ram = sorted(suma.items(), key=lambda x: x[1][1], reverse=True)

    linie.append(f"Procesor ogółem: {psutil.cpu_percent(None):.0f}%.")
    najwiecej_cpu = [f"{n} {w[0]:.0f}%" for n, w in wedlug_cpu[:ILE_NAJWIEKSZYCH]
                     if w[0] >= 0.5]
    linie.append("Najwięcej procesora: " + (", ".join(najwiecej_cpu)
                                           or "nic wyraźnie nie obciąża procesora") + ".")
    linie.append("Najwięcej pamięci: " + ", ".join(
        f"{n} {_rozmiar(w[1])}" for n, w in wedlug_ram[:ILE_NAJWIEKSZYCH]) + ".")
    return "\n".join(linie), True


# ---------------------------------------------------------------
# downloads_status
# ---------------------------------------------------------------

def _folder_pobrane():
    """
    Prawdziwa ścieżka folderu Pobrane.

    "Pobrane" to tylko nazwa wyświetlana — folder mógł zostać przeniesiony
    (np. na inny dysk). Windows wie, gdzie jest naprawdę, i podaje to przez
    SHGetKnownFolderPath. Samo zgadywanie "C:\\Users\\...\\Downloads"
    zawiodłoby każdego, kto folder przeniósł.
    """
    try:
        class GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                        ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

        identyfikator = GUID()
        # FOLDERID_Downloads — stały identyfikator folderu Pobrane w Windows.
        ctypes.memmove(ctypes.byref(identyfikator),
                       uuid.UUID("{374DE290-123F-4565-9164-39C4925E467B}").bytes_le, 16)
        sciezka = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(
                ctypes.byref(identyfikator), 0, None, ctypes.byref(sciezka)) == 0:
            wynik = sciezka.value
            ctypes.windll.ole32.CoTaskMemFree(sciezka)
            return wynik
    except (AttributeError, OSError):
        pass
    return os.path.join(os.path.expanduser("~"), "Downloads")


def _nazwa_docelowa(nazwa_pliku):
    """
    "film.mp4.crdownload" -> "film.mp4".

    Chrome na początku pobierania nie zna jeszcze nazwy i zapisuje plik
    jako "Unconfirmed 123456.crdownload" — wtedy mówimy to wprost.
    """
    rdzen = os.path.splitext(nazwa_pliku)[0]
    if re.match(r"^(Unconfirmed|Niepotwierdzon\w*) \d+", rdzen, flags=re.I):
        return "(nazwa jeszcze nieznana)"
    return rdzen


def pobierania(folder=None):
    """
    Niedokończone pobierania: nazwa, rozmiar i czy nadal rosną.

    Docelowego rozmiaru nie znamy — zna go tylko przeglądarka — więc nie
    podajemy "ile zostało". Podajemy za to, czy plik rośnie i jak szybko:
    to odpowiada na prawdziwe pytanie, czyli "czy to jeszcze się ściąga?".

    Zwraca: (komunikat, czy_się_udało).
    """
    folder = folder or _folder_pobrane()
    try:
        pliki = [wpis.path for wpis in os.scandir(folder)
                 if wpis.is_file() and wpis.name.lower().endswith(KONCOWKI_W_TOKU)]
    except OSError as e:
        return f"Nie mogę zajrzeć do folderu Pobrane: {e}", False

    if not pliki:
        return "W folderze Pobrane nie ma niedokończonych pobierań.", True

    def rozmiary():
        wynik = {}
        for sciezka in pliki:
            try:
                wynik[sciezka] = os.path.getsize(sciezka)
            except OSError:
                pass    # plik zniknął — pobieranie właśnie się skończyło
        return wynik

    przed = rozmiary()
    time.sleep(PROBKA_POBIERAN_S)
    po = rozmiary()

    linie = [f"Niedokończone pobierania ({len(po)}):"]
    for sciezka, rozmiar in sorted(po.items(), key=lambda x: -x[1]):
        nazwa = _nazwa_docelowa(os.path.basename(sciezka))
        przyrost = (rozmiar - przed.get(sciezka, rozmiar)) / PROBKA_POBIERAN_S
        if przyrost > 0:
            stan = f"pobiera się, {_rozmiar(przyrost)}/s"
        else:
            minuty = (time.time() - os.path.getmtime(sciezka)) / 60
            stan = ("chwilowo stoi" if minuty < 2
                    else f"stoi od {minuty:.0f} min — pewnie przerwane")
        linie.append(f"- {nazwa}: {_rozmiar(rozmiar)}, {stan}.")

    zakonczone = len(przed) - len(po)
    if zakonczone > 0:
        linie.append(f"W trakcie sprawdzania {zakonczone} pobieranie się zakończyło.")
    return "\n".join(linie), True


# ---------------------------------------------------------------
# screenshot
# ---------------------------------------------------------------

def zrzut_ekranu(jakosc=85):
    """
    Zrzut WSZYSTKICH monitorów naraz, jako JPEG.

    JPEG, a nie PNG: zrzut dwóch ekranów Full HD w PNG waży kilka megabajtów,
    w JPEG kilkaset kilobajtów, a Telegram i tak przekompresowuje zdjęcia.

    Zwraca: (bajty_jpeg, opis) albo (None, opis_problemu).
    """
    try:
        from PIL import ImageGrab

        obraz = ImageGrab.grab(all_screens=True)
    except Exception as e:
        logger.exception("Nie udało się zrobić zrzutu ekranu")
        return None, f"Nie udało się zrobić zrzutu ekranu: {e}"

    # Zablokowany komputer daje zupełnie czarny obraz — system nie pozwala
    # podglądać ekranu logowania. Mówimy o tym zamiast wysyłać czarny prostokąt.
    if obraz.convert("L").getextrema() == (0, 0):
        return None, "Ekran jest czarny — komputer jest pewnie zablokowany."

    bufor = io.BytesIO()
    obraz.convert("RGB").save(bufor, "JPEG", quality=jakosc, optimize=True)
    return bufor.getvalue(), f"Zrzut ekranu {obraz.width}×{obraz.height}."


# ---------------------------------------------------------------
# analyze_screen — zrzut jednego monitora dla modelu
# ---------------------------------------------------------------
#
# Model płaci za obraz według jego WYMIARÓW, nie wagi pliku: mniej więcej
# (szerokość × wysokość) / 750 tokenów. Pełny ekran Full HD to ~2800 tokenów,
# a przeskalowany do MAKS_BOK_ANALIZY — patrz wartość i pomiar niżej.
#
# Zmniejszamy więc sam obraz, a nie jakość JPEG-a: kompresja zmniejsza plik,
# ale nie koszt, za to rozmywa drobny tekst, który model ma przeczytać.

# Dłuższy bok zrzutu wysyłanego do analizy, w pikselach. Dobrany pomiarem na
# sztucznym ekranie Full HD (okno błędu 12 px, ślad błędu w edytorze 13 px,
# pasek stanu 10 px), model Sonnet 5:
#
#     bok   tokeny   wynik
#     768     583    przekręcił nazwę klucza w błędzie ('serwer' -> 'server')
#     1024    912    pomylił cyfrę w drobnym tekście (2.7.19 -> 2.7.18)
#     1280   1331    wszystko poprawnie                         <- wybrane
#     1568   1920    wszystko poprawnie, ale o połowę drożej
#
# Przy "przeczytaj ten błąd" liczą się dokładne kody i nazwy, więc oszczędność
# 400 tokenów (ułamek centa) nie jest warta pomylonej cyfry.
MAKS_BOK_ANALIZY = 1280


def monitory():
    """
    Prostokąty wszystkich monitorów, GŁÓWNY PIERWSZY.

    Pytamy o to Windowsa bezpośrednio (EnumDisplayMonitors), bo tylko on wie,
    który monitor jest główny i gdzie leży drugi — u Ciebie drugi jest po
    LEWEJ stronie, więc ma ujemne współrzędne (od -1920 do 0).

    Zwraca: listę krotek (lewo, góra, prawo, dół, czy_główny).
    """
    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                    ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]

    user32 = ctypes.windll.user32
    wynik = []

    # Windows woła tę funkcję raz dla każdego monitora.
    def dla_monitora(uchwyt, _hdc, _prostokat, _dane):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        user32.GetMonitorInfoW(ctypes.c_void_p(uchwyt), ctypes.byref(info))
        r = info.rcMonitor
        wynik.append((r.left, r.top, r.right, r.bottom, bool(info.dwFlags & 1)))
        return 1    # "szukaj dalej"

    TypWywolania = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                      ctypes.POINTER(RECT), ctypes.c_ssize_t)
    user32.EnumDisplayMonitors(None, None, TypWywolania(dla_monitora), 0)
    return sorted(wynik, key=lambda m: not m[4])


def zrzut_do_analizy(monitor="glowny", maks_bok=None):
    """
    Zrzut jednego monitora, zmniejszony i gotowy do wysłania modelowi.

    monitor — "glowny" (domyślnie) albo "drugi"

    Obraz powstaje i zostaje wyłącznie w pamięci — nic nie trafia na dysk.

    Zwraca: (bajty_jpeg, opis) albo (None, opis_problemu).
    """
    maks_bok = maks_bok or MAKS_BOK_ANALIZY
    lista = monitory()
    if not lista:
        return None, "Nie widzę żadnego monitora."

    if monitor == "drugi":
        if len(lista) < 2:
            return None, "Jest podłączony tylko jeden monitor."
        wybrany = lista[1]
    else:
        wybrany = lista[0]

    lewo, gora, prawo, dol, _glowny = wybrany
    try:
        from PIL import Image, ImageGrab

        # all_screens=True, bo drugi monitor leży na ujemnych współrzędnych,
        # a bez tej flagi Pillow widzi tylko główny.
        obraz = ImageGrab.grab(bbox=(lewo, gora, prawo, dol), all_screens=True)
    except Exception as e:
        logger.exception("Nie udało się zrobić zrzutu do analizy")
        return None, f"Nie udało się zrobić zrzutu ekranu: {e}"

    if obraz.convert("L").getextrema() == (0, 0):
        return None, "Ekran jest czarny — komputer jest pewnie zablokowany."

    oryginal = obraz.size
    # thumbnail() zachowuje proporcje: 1920×1080 przy boku 1280 -> 1280×720.
    obraz.thumbnail((maks_bok, maks_bok), Image.LANCZOS)

    bufor = io.BytesIO()
    obraz.convert("RGB").save(bufor, "JPEG", quality=90)
    opis = (f"Zrzut {'głównego' if monitor != 'drugi' else 'drugiego'} monitora "
            f"({oryginal[0]}×{oryginal[1]}, wysłany jako {obraz.width}×{obraz.height}).")
    return bufor.getvalue(), opis


# --- Test: `python stan_komputera.py` — wszystko poza zrzutem ekranu ---
if __name__ == "__main__":
    from logging_setup import skonfiguruj_logowanie

    skonfiguruj_logowanie()
    print(stan_systemu()[0], "\n")
    print(procesy()[0], "\n")
    print(procesy("spotify")[0], "\n")
    print(pobierania()[0])
