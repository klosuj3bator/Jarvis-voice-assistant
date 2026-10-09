"""
tapeta.py — Jarvis jako żywa tapeta pulpitu i wykrywanie gier na pełnym ekranie.

Same wywołania Windowsa (ctypes), bez Qt — samym oknem HUD-a zajmuje się gui.py.


JAK ZOSTAĆ TAPETĄ
=================

Pulpit Windowsa to kilka warstw okien Explorera, od góry:

    [okna programów]
    [ikony pulpitu]    WorkerW z SHELLDLL_DefView — przezroczysty poza ikonami
    [warstwa tapety]   WorkerW — na nim Windows rysuje płynne przejścia tapet
    [Progman]          zwykła tapeta

Warstwę tapety Explorer tworzy na komunikat 0x052C — ten sam, którym sam
zaczyna płynną zmianę tapety. To nie zmienia żadnych ustawień i znika przy
restarcie Explorera; tak samo robią Wallpaper Engine i Lively. HUD wstawiony
jako DZIECKO tej warstwy jest pod wszystkimi oknami i pod ikonami, nie ma
go na pasku zadań ani w Alt+Tab, a kliknięcia trafiają w ikony pulpitu —
więc nigdy nie przejmie focusu. Win+D ("pokaż pulpit") też go nie chowa.


SPRZĄTANIE — WAŻNE
==================

Warstwa tapety NIE przerysowuje się sama, gdy zabierze się z niej okno:
zostaje na niej ostatnia klatka HUD-a. Sprawdzone 29.09 — po teście część
pulpitu była jednolicie magentowa, aż do schowania warstwy; nie pomagało
nawet RedrawWindow. Dlatego wychodząc z trybu tapety chowamy CAŁĄ warstwę
(schowaj_warstwe) — spod niej wychodzi Progman ze zwykłą tapetą.

Schować warstwy nie wolno, gdy siedzi na niej okno innego programu (opis
niżej) — wtedy po zejściu HUD-a z monitora zostałaby jego klatka. Dlatego
HUD, zanim zejdzie, zakrywa się na chwilę obrazem zwykłej tapety spod
spodu (obraz_tapety, gui.py: _zakryj_tapeta) — "ostatnią klatką" jest
wtedy sama tapeta. Działa to też przy przenoszeniu tapety na inny monitor.

Gdy Jarvis zostanie zabity (awaria, deinstalator), sprzątanie się nie
wykona. Na tę okazję jest plik-znacznik ZNACZNIK (istnieje, dopóki tapeta
działa) i przywroc_pulpit(): woła ją main.py przy następnym starcie,
a deinstalator — przez `Jarvis.exe --przywroc-pulpit`.

Warstwę chowamy tylko wtedy, gdy nie ma na niej okien innych programów —
nie psujemy tapety z Wallpaper Engine czy Lively.


PLAN AWARYJNY
=============

Nowsze Windows 11 układają te warstwy inaczej. Szukamy obu układów; jeśli
nie znajdziemy żadnego, gui.py robi z HUD-a zwykłe okno "zawsze na spodzie"
(też bez paska zadań, Alt+Tab i focusu), tyle że przykrywa ono ikony.
"""

import ctypes
import logging
import os
from ctypes import wintypes

import sciezki

logger = logging.getLogger(__name__)

user32 = ctypes.windll.user32
shell32 = ctypes.windll.shell32
for _funkcja in ("FindWindowW", "FindWindowExW", "GetParent", "GetAncestor", "GetWindow",
                 "GetForegroundWindow", "MonitorFromWindow"):
    getattr(user32, _funkcja).restype = wintypes.HANDLE

# Plik-znacznik: "HUD siedzi teraz w warstwie tapety" (opis SPRZĄTANIE).
ZNACZNIK = sciezki.dane("tapeta.aktywna")

KOMUNIKAT_WARSTWY = 0x052C
SW_HIDE, SW_SHOWNA = 0, 8
GW_HWNDNEXT, GW_CHILD = 2, 5
GA_ROOT = 2

# Okna samego pulpitu i paska zadań — nie są "programem na pełnym ekranie".
KLASY_PULPITU = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def _klasa(h):
    bufor = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(h, bufor, 256)
    return bufor.value


def _dzieci(h):
    wynik = []
    dziecko = user32.GetWindow(h, GW_CHILD)
    while dziecko:
        wynik.append(dziecko)
        dziecko = user32.GetWindow(dziecko, GW_HWNDNEXT)
    return wynik


def prostokat(h):
    """(lewo, góra, prawo, dół) okna w pikselach ekranu."""
    r = wintypes.RECT()
    user32.GetWindowRect(h, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def istnieje(h):
    return bool(h) and bool(user32.IsWindow(h))


def rodzic(h):
    return user32.GetParent(h)


def znajdz_warstwe(utworz=True):
    """
    Uchwyt warstwy tapety (WorkerW pod ikonami) albo None.

    utworz — wysłać najpierw 0x052C, żeby Explorer ją utworzył (jeśli jej nie
             ma, nic się nie dzieje). False przy sprzątaniu: tylko szukamy.
    """
    progman = user32.FindWindowW("Progman", None)
    if not progman:
        return None
    if utworz:
        wynik = ctypes.c_size_t()
        user32.SendMessageTimeoutW(progman, KOMUNIKAT_WARSTWY, 0, 0, 0, 1000, ctypes.byref(wynik))

    # Układ z Windows 10 i starszych 11: warstwa tapety to WorkerW leżący
    # tuż pod oknem, w którym siedzą ikony (SHELLDLL_DefView).
    znalezione = []
    PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def szukaj(h, _):
        if user32.FindWindowExW(h, None, "SHELLDLL_DefView", None):
            znalezione.append(user32.FindWindowExW(None, h, "WorkerW", None))
        return True

    user32.EnumWindows(PROC(szukaj), 0)
    warstwa = next((h for h in znalezione if h), None)
    # Układ z Windows 11 24H2: warstwa jest dzieckiem Progmana.
    return warstwa or user32.FindWindowExW(progman, None, "WorkerW", None) or None


def pokaz_warstwe(h):
    """Pokazuje warstwę tapety (mogliśmy ją wcześniej schować) — bez aktywowania."""
    user32.ShowWindow(h, SW_SHOWNA)


def schowaj_warstwe(h, nasze=()):
    """
    Chowa warstwę tapety — spod niej wychodzi zwykła tapeta (opis SPRZĄTANIE).
    Tylko gdy nie ma na niej cudzych okien; `nasze` to uchwyty, które wolno
    pominąć (np. nasz HUD przy zamykaniu programu).

    Zwraca: True, jeśli schowana.
    """
    if not istnieje(h):
        return False
    cudze = [d for d in _dzieci(h) if d not in set(nasze)]
    if cudze:
        logger.info("[TAPETA] Na warstwie tapety są okna innego programu — zostawiam ją.")
        return False
    user32.ShowWindow(h, SW_HIDE)
    return True


def obraz_tapety(prostokat_okna):
    """
    Zwykła tapeta Windowsa pod danym prostokątem ekranu — do "zasłony"
    (gui.py, JarvisHUD._zakryj_tapeta).

    Bierzemy ją od Progmana (opis warstw na górze): PrintWindow każe mu
    narysować tapetę do naszego bufora, nawet gdy przykrywa go warstwa
    tapety i okna. Sprawdzone 01.10 na obu monitorach.

    Zwraca: (szerokość, wysokość, bajty BGRA) albo None.
    """
    gdi32 = ctypes.windll.gdi32
    progman = user32.FindWindowW("Progman", None)
    if not progman:
        return None
    pl, pg, pp, pd = prostokat(progman)
    lewo, gora, prawo, dol = prostokat_okna
    szer, wys = prawo - lewo, dol - gora
    if szer <= 0 or wys <= 0:
        return None

    dc_okna = user32.GetWindowDC(progman)
    dc = gdi32.CreateCompatibleDC(dc_okna)
    calosc = gdi32.CreateCompatibleBitmap(dc_okna, pp - pl, pd - pg)
    gdi32.SelectObject(dc, calosc)
    try:
        if not user32.PrintWindow(progman, dc, 2):          # PW_RENDERFULLCONTENT
            return None
        # Wycinek naszego okna — do drugiej bitmapy, z niej bajty.
        dc_wycinka = gdi32.CreateCompatibleDC(dc_okna)
        wycinek = gdi32.CreateCompatibleBitmap(dc_okna, szer, wys)
        gdi32.SelectObject(dc_wycinka, wycinek)
        gdi32.BitBlt(dc_wycinka, 0, 0, szer, wys, dc, lewo - pl, gora - pg, 0x00CC0020)   # SRCCOPY

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]

        naglowek = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=szer,
                                    biHeight=-wys, biPlanes=1, biBitCount=32)   # minus = od góry
        bufor = ctypes.create_string_buffer(szer * wys * 4)
        gdi32.GetDIBits(dc_wycinka, wycinek, 0, wys, bufor, ctypes.byref(naglowek), 0)
        gdi32.DeleteObject(wycinek)
        gdi32.DeleteDC(dc_wycinka)
        return szer, wys, bufor.raw
    finally:
        gdi32.DeleteObject(calosc)
        gdi32.DeleteDC(dc)
        user32.ReleaseDC(progman, dc_okna)


def ustaw_pozycje(h, x, y, szerokosc, wysokosc):
    """Położenie okna-dziecka w pikselach, względem rodzica (bez zmiany kolejności)."""
    SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
    user32.SetWindowPos(h, None, x, y, szerokosc, wysokosc, SWP_NOZORDER | SWP_NOACTIVATE)


def oznacz(aktywna):
    """Zakłada albo usuwa plik-znacznik ZNACZNIK."""
    try:
        if aktywna:
            os.makedirs(os.path.dirname(ZNACZNIK), exist_ok=True)
            with open(ZNACZNIK, "w", encoding="utf-8") as plik:
                plik.write("HUD Jarvisa jest tapetą — opis w tapeta.py\n")
        elif os.path.exists(ZNACZNIK):
            os.remove(ZNACZNIK)
    except OSError:
        logger.warning("[TAPETA] Nie udało się zapisać znacznika tapety.", exc_info=True)


def przywroc_pulpit(nasze=()):
    """
    Sprzątanie po tapecie, której nikt nie zdjął (awaria, zabity proces,
    deinstalacja) — opis SPRZĄTANIE. Działa tylko, gdy jest ZNACZNIK.

    Zwraca: True, jeśli było co sprzątać.
    """
    if not os.path.exists(ZNACZNIK):
        return False
    warstwa = znajdz_warstwe(utworz=False)
    if warstwa and user32.IsWindowVisible(warstwa):
        schowaj_warstwe(warstwa, nasze)
    oznacz(False)
    logger.info("[TAPETA] Posprzątałem pulpit po poprzedniej tapecie.")
    return True


# ---------------------------------------------------------------
# Gra albo film na pełnym ekranie — wtedy HUD wstrzymuje animacje
# ---------------------------------------------------------------

# Stany z SHQueryUserNotificationState — pytania, o które Windows sam dba,
# żeby nie pokazywać powiadomień w trakcie gry albo prezentacji.
QUNS_NOT_PRESENT = 1              # ekran zablokowany albo wygaszacz
QUNS_BUSY = 2                     # program na pełnym ekranie
QUNS_RUNNING_D3D_FULL_SCREEN = 3  # gra w wyłącznym pełnym ekranie
QUNS_PRESENTATION_MODE = 4        # tryb prezentacji


def _okno_na_calym_monitorze(pomijane):
    """Nazwa programu, którego okno na pierwszym planie zakrywa cały monitor, albo None."""
    h = user32.GetForegroundWindow()
    if not h:
        return None
    korzen = user32.GetAncestor(h, GA_ROOT) or h
    if korzen in pomijane or h in pomijane or _klasa(korzen) in KLASY_PULPITU:
        return None
    if not user32.IsWindowVisible(korzen) or user32.IsIconic(korzen):
        return None

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
    if not user32.GetMonitorInfoW(user32.MonitorFromWindow(korzen, 2), ctypes.byref(info)):
        return None
    lewo, gora, prawo, dol = prostokat(korzen)
    m = info.rcMonitor
    if lewo > m.left or gora > m.top or prawo < m.right or dol < m.bottom:
        return None   # zwykłe albo zmaksymalizowane okno — widać pasek zadań

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(korzen, ctypes.byref(pid))
    try:
        import psutil

        return psutil.Process(pid.value).name()
    except Exception:
        return "program"


def pelny_ekran(pomijane=()):
    """
    Czy coś zajmuje teraz cały ekran — gra, film, prezentacja?

    pomijane — uchwyty naszych okien (HUD na pełnym ekranie to nie gra)

    Dwa źródła, bo żadne nie łapie wszystkiego:
      - Windows sam wie o grach w wyłącznym pełnym ekranie i prezentacjach
        (SHQueryUserNotificationState),
      - gry "w oknie bez ramki" łapiemy sami: okno na pierwszym planie
        zakrywa cały monitor, łącznie z miejscem na pasek zadań.
    Zablokowany ekran też liczy się jako powód do pauzy — nikt nie patrzy.

    Zwraca: (True/False, krótki opis powodu do dziennika).
    """
    stan = ctypes.c_int(0)
    if shell32.SHQueryUserNotificationState(ctypes.byref(stan)) == 0:
        if stan.value == QUNS_RUNNING_D3D_FULL_SCREEN:
            return True, "gra w wyłącznym pełnym ekranie"
        if stan.value == QUNS_PRESENTATION_MODE:
            return True, "tryb prezentacji"
        if stan.value == QUNS_NOT_PRESENT:
            return True, "ekran zablokowany"
    program = _okno_na_calym_monitorze(set(pomijane))
    if program:
        return True, program
    return False, ""
