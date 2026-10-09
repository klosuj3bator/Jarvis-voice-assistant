"""
pasek_boczny.py — "Tryb 2 ekranów": panel boczny jako wąski pasek przy
krawędzi drugiego monitora, i sterownik, który decyduje, gdzie panel widać.

Wygląd panelu to Panel.qml, dane — panel.py. Ten moduł robi tylko okno
i pilnuje monitorów.


GDZIE JEST PANEL
================

  włączony "Tryb 2 ekranów" i wybrany monitor jest podłączony
      -> pasek przy jego krawędzi (to okno),
  w przeciwnym razie
      -> panel wysuwany z boku HUD-a / tapety (hud.qml).

Odłączysz drugi monitor — Windows zgłasza to Qt, Qt nam (screenRemoved),
pasek znika, a panel przechodzi na tapetę. Podłączysz z powrotem — wraca.


PASEK JAK PASEK ZADAŃ
=====================

Windows ma na to gotowy mechanizm: pasek aplikacji (AppBar, SHAppBarMessage).
Pasek zgłasza krawędź i szerokość, a system zmniejsza "obszar roboczy"
monitora — zmaksymalizowane okna kończą się tuż przed paskiem, dokładnie
jak przy pasku zadań. Dlatego pasek nie zasłania reszty ekranu, a reszta
nie zasłania jego.

Pasek NIE jest "zawsze na wierzchu": gra na pełnym ekranie tego monitora
go przykryje (i dobrze), a okno przeciągnięte ręcznie może na niego wejść.
Nie ma go na pasku zadań ani w Alt+Tab i nie przejmuje focusu.

Rezerwację miejsca trzeba zwolnić (ABM_REMOVE). Gdyby Jarvis zginął
w trakcie, zostałby pusty pas przy krawędzi — dlatego uchwyt okna leży
w pliku ZNACZNIK, a usun_stary_pasek() zwalnia rezerwację przy następnym
starcie i w deinstalatorze (Jarvis.exe --przywroc-pulpit).
"""

import ctypes
import logging
import os
from ctypes import wintypes

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QGuiApplication
from PySide6.QtQuick import QQuickView

import gui
import konfiguracja
import panel
import sciezki

logger = logging.getLogger(__name__)

# Szerokość paska w pikselach "logicznych" (przy 100% = zwykłych).
SZEROKOSC = 320

SCIEZKA_QML = sciezki.zasob("Panel.qml")
ZNACZNIK = sciezki.dane("panel.pasek")

ABM_NEW, ABM_REMOVE, ABM_QUERYPOS, ABM_SETPOS = 0, 1, 2, 3
ABE_LEFT, ABE_RIGHT = 0, 2


class _APPBARDATA(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uCallbackMessage", wintypes.UINT), ("uEdge", wintypes.UINT),
                ("rc", wintypes.RECT), ("lParam", wintypes.LPARAM)]


def _appbar(komunikat, hwnd, krawedz=0, prostokat=None):
    dane = _APPBARDATA(cbSize=ctypes.sizeof(_APPBARDATA), hWnd=hwnd, uEdge=krawedz)
    # Komunikat zwrotny jest wymagany przy ABM_NEW; jego powiadomień nie
    # obsługujemy (pasek nie jest "na wierzchu", więc nie musi ustępować grom).
    dane.uCallbackMessage = ctypes.windll.user32.RegisterWindowMessageW("JarvisPasekBoczny")
    if prostokat:
        dane.rc = wintypes.RECT(*prostokat)
    ctypes.windll.shell32.SHAppBarMessage(komunikat, ctypes.byref(dane))
    return (dane.rc.left, dane.rc.top, dane.rc.right, dane.rc.bottom)


def usun_stary_pasek():
    """
    Zwalnia rezerwację miejsca po pasku, którego nikt nie zamknął (awaria,
    zabity proces, deinstalacja). Działa tylko, gdy jest ZNACZNIK.
    """
    if not os.path.exists(ZNACZNIK):
        return False
    try:
        with open(ZNACZNIK, encoding="utf-8") as plik:
            hwnd = int(plik.read().strip() or 0)
        if hwnd:
            _appbar(ABM_REMOVE, hwnd)
        logger.info("[PANEL] Zwolniłem miejsce po poprzednim pasku bocznym.")
    except (OSError, ValueError):
        pass
    try:
        os.remove(ZNACZNIK)
    except OSError:
        pass
    return True


class PasekBoczny(QQuickView):
    """Okno paska: Panel.qml na całą wysokość, przy krawędzi monitora."""

    def __init__(self, most, rodzina, rodzina_mono):
        super().__init__()
        self.setTitle("J.A.R.V.I.S. — panel")
        # Narzędziowe (bez paska zadań i Alt+Tab), bez ramki, bez focusu.
        self.setFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus)
        self.setColor(QColor(2, 7, 11))
        self.setResizeMode(QQuickView.SizeRootObjectToView)
        self._dostawca = gui.DostawcaObrazow(rodzina, rodzina_mono)   # okładki Spotify
        self.engine().addImageProvider("jarvis", self._dostawca)
        self.setInitialProperties({
            "most": most, "dane": panel.stan(), "elementy": konfiguracja.panel_elementy(),
            "rodzina": rodzina, "rodzinaMono": rodzina_mono, "pasek": True,
        })
        self.setSource(QUrl.fromLocalFile(SCIEZKA_QML))
        if self.status() != QQuickView.Status.Ready:
            bledy = "; ".join(blad.toString() for blad in self.errors())
            raise RuntimeError(f"Nie udało się wczytać {SCIEZKA_QML}: {bledy}")
        self._hwnd = 0

    def pokaz_na(self, ekran, krawedz):
        """Rezerwuje pas przy krawędzi monitora i staje w nim (wątek GUI)."""
        g = ekran.geometry()
        dpr = ekran.devicePixelRatio()
        # Położenie monitora w pikselach Windowsa (opis przeliczenia: gui.py, tapeta).
        lewo, gora = g.x(), g.y()
        prawo, dol = lewo + round(g.width() * dpr), gora + round(g.height() * dpr)
        szer = round(SZEROKOSC * dpr)
        strona = ABE_LEFT if krawedz == "lewa" else ABE_RIGHT

        self.resize(SZEROKOSC, g.height())
        if not self._hwnd:
            self.create()
            self._hwnd = int(self.winId())
            _appbar(ABM_NEW, self._hwnd)
            with open(ZNACZNIK, "w", encoding="utf-8") as plik:
                plik.write(str(self._hwnd))

        prostokat = (lewo, gora, lewo + szer, dol) if strona == ABE_LEFT else (prawo - szer, gora, prawo, dol)
        # System może przesunąć pas (np. pasek zadań na tej samej krawędzi) —
        # pytamy, poprawiamy szerokość i dopiero wtedy rezerwujemy.
        l, t, r, b = _appbar(ABM_QUERYPOS, self._hwnd, strona, prostokat)
        prostokat = (l, t, l + szer, b) if strona == ABE_LEFT else (r - szer, t, r, b)
        l, t, r, b = _appbar(ABM_SETPOS, self._hwnd, strona, prostokat)

        self.show()
        ctypes.windll.user32.SetWindowPos(self._hwnd, None, l, t, r - l, b - t,
                                          0x0004 | 0x0010)      # NOZORDER | NOACTIVATE
        logger.info("[PANEL] Pasek na monitorze %s, %s krawędź (%d px).", ekran.name(), krawedz, r - l)

    def schowaj(self):
        """Zwalnia zarezerwowane miejsce i chowa okno."""
        if self._hwnd:
            _appbar(ABM_REMOVE, self._hwnd)
            self._hwnd = 0
            try:
                os.remove(ZNACZNIK)
            except OSError:
                pass
        self.hide()


class SterownikPanelu:
    """
    Decyduje, gdzie jest panel (opis GDZIE JEST PANEL), i reaguje na zmiany
    monitorów oraz Ustawień. Tylko z wątku GUI.
    """

    def __init__(self, hud):
        self._hud = hud
        self._most = hud.most_panelu
        self._pasek = None
        self._w_hud = False
        self._przypiety = False
        self._ekran_paska = None
        panel.sluchaj(self._most.dane.emit)       # z wątku danych prosto do scen

        # Monitory przychodzą i odchodzą — ale Windows zgłasza to w kilku
        # krokach, więc odczekujemy chwilę, aż wszystko się ułoży.
        self._opoznienie = QTimer()
        self._opoznienie.setSingleShot(True)
        self._opoznienie.setInterval(700)
        self._opoznienie.timeout.connect(self.zastosuj)
        aplikacja = QGuiApplication.instance()
        aplikacja.screenAdded.connect(lambda _: self._opoznienie.start())
        aplikacja.screenRemoved.connect(lambda _: self._opoznienie.start())
        aplikacja.primaryScreenChanged.connect(lambda _: self._opoznienie.start())
        self.zastosuj()

    def _wybrany_ekran(self):
        """Monitor paska albo None (panel wtedy na HUD-zie / tapecie)."""
        if not (konfiguracja.panel_wlaczony() and konfiguracja.panel_2_ekrany()):
            return None
        ekrany = QGuiApplication.screens()
        if len(ekrany) < 2:
            return None
        nazwa = konfiguracja.panel_monitor()
        if nazwa:
            return next((e for e in ekrany if e.name() == nazwa), None)
        glowny = QGuiApplication.primaryScreen()
        return next((e for e in ekrany if e != glowny), None)

    def zastosuj(self):
        """Ustawia panel według Ustawień i podłączonych monitorów."""
        elementy = konfiguracja.panel_elementy()
        self._most.elementy.emit(elementy)
        ekran = self._wybrany_ekran()

        if ekran is not None:
            if self._pasek is None:
                self._pasek = PasekBoczny(self._most, *self._hud.rodziny)
            self._pasek.pokaz_na(ekran, konfiguracja.panel_krawedz())
            self._w_hud = False
        else:
            if self._pasek is not None and self._pasek.isVisible():
                self._pasek.schowaj()
                if konfiguracja.panel_wlaczony() and konfiguracja.panel_2_ekrany():
                    logger.info("[PANEL] Drugi monitor odłączony — panel przechodzi na tapetę.")
            self._w_hud = konfiguracja.panel_wlaczony()
        self._ekran_paska = ekran

        self._most.wHud.emit(self._w_hud)
        self._most.przypiety.emit(self._przypiety)
        self._most.dane.emit(panel.stan())
        if konfiguracja.panel_wlaczony():
            panel.odswiez_teraz()

    # --- dla menu przy zegarku ---

    def w_hud(self):
        """Czy panel wysuwa się teraz z HUD-a (wtedy ma sens "Wysuń panel")."""
        return self._w_hud

    def przypiety(self):
        return self._przypiety

    def przypnij(self, tak):
        self._przypiety = tak
        self._most.przypiety.emit(tak)

    def zakoncz(self):
        """Przy zamykaniu programu — zwalnia miejsce paska."""
        if self._pasek is not None:
            self._pasek.schowaj()
