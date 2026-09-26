"""
gui.py — pełnoekranowy interfejs Jarvisa w stylu HUD-a z filmów o Iron Manie.

Koncentryczne pierścienie z podziałką, segmentowy wieniec, bursztynowe łuki
i napis J.A.R.V.I.S. w środku, na tle technicznego rysunku. Każdy pierścień
obraca się z własną prędkością, a kolory i tempo zmieniają się ze stanem
Jarvisa (czuwa, słucha, myśli, mówi, błąd).

Moduł wystawia jedną metodę, set_state(), którą reszta programu woła,
żeby powiedzieć "teraz słucham" albo "teraz myślę".


JAK DZIAŁA KOMUNIKACJA MIĘDZY WĄTKAMI
=====================================

Problem: biblioteki graficzne — Qt, ale też każda inna — pozwalają dotykać
okien TYLKO z tego wątku, w którym powstały (wątek GUI, ten z pętlą zdarzeń).
Jeśli inny wątek, np. ten nasłuchujący mikrofonu, zmieni coś w oknie
bezpośrednio, program albo się wysypie, albo — gorzej — będzie działał
losowo: raz dobrze, raz nie. To klasyczny wyścig (race condition).

Rozwiązanie Qt: sygnały i sloty.

  - SYGNAŁ (Signal) to "ogłoszenie" — coś się stało.
  - SLOT to zwykła metoda, która na to ogłoszenie reaguje.
  - .connect() łączy jedno z drugim.

Gdy sygnał wyemituje INNY wątek niż ten, w którym żyje odbiorca, Qt pakuje
wywołanie razem z argumentami i wrzuca je do kolejki zdarzeń wątku GUI.
Wątek GUI wykona je sam, w bezpiecznym momencie. Nikt niczego nie dotyka
jednocześnie, więc nie ma wyścigu.

Dlatego set_state() poniżej NIE ustawia stanu wprost. Emituje sygnał,
a odbiera go scena hud.qml — zawsze w wątku GUI.


DLACZEGO HUD RYSUJE KARTA GRAFICZNA, A NIE PYTHON
=================================================

Poprzednia wersja rysowała każdą klatkę w Pythonie (QPainter, na procesorze).
Miało to dwie wady, które było widać gołym okiem:

  1. MAŁO KLATEK. Jedna klatka kosztowała ok. 12 ms procesora, więc żeby
     nie zjeść komputera, HUD rysował tylko 20-30 klatek na sekundę.
     Na monitorze 240 Hz wygląda to jak pokaz slajdów.

  2. PRZYCIĘCIA. Python ma tzw. GIL: w danej chwili kod Pythona może
     wykonywać tylko JEDEN wątek. Kiedy wątek nasłuchu przetwarzał dźwięk,
     rysowanie musiało czekać na swoją kolej — i klatka przychodziła za późno.

Teraz praca jest podzielona tak:

  - gui.py (ten plik, Python) — raz rysuje nieruchome tło i napis, a potem
    tylko mówi scenie, jaki jest stan. Kilka razy na minutę, nie co klatkę.
  - hud.qml (Qt Quick) — opisuje scenę i jej ruch. Klatki odmierza i rysuje
    Qt (C++), a w wątku GUI nie wykonuje się przy tym ani linijka Pythona,
    więc nic nie czeka na Whispera (szczegóły w hud.qml).
  - hud.frag (shader) — rysuje pierścienie na karcie graficznej, która liczy
    wszystkie piksele jednocześnie.

Qt odmierza klatki w rytmie odświeżania monitora — na 240 Hz to 240 równych
klatek na sekundę (co 4,17 ms), przy ok. 1% procesora.
"""

import logging
import math
import os
import random
import sys

# Pętla renderowania "basic": Qt sam odmierza klatki zegarem zgodnym
# z odświeżaniem monitora (240 Hz -> co 4,17 ms).
#
# Dlaczego nie domyślna "threaded": ta czeka, aż karta graficzna zgłosi
# gotowość monitora (vsync). Na tym komputerze synchronizacja pionowa jest
# wyłączona w sterowniku NVIDIA, więc karta nie czeka — i HUD renderował
# ok. 3000 klatek na sekundę, z czego monitor pokazywał 240. Zmierzone:
# "basic" daje równe 240 klatek przy ok. 1% procesora.
#
# Qt czyta to ustawienie przy tworzeniu QApplication, a main.py importuje
# gui.py wcześniej.
os.environ.setdefault("QSG_RENDER_LOOP", "basic")

import threading  # noqa: E402

import psutil  # noqa: E402
from PySide6.QtCore import QLineF, QObject, QPointF, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QAction, QColor, QFont, QFontDatabase, QIcon, QImage, QPainter,
    QPainterPath, QPen, QPixmap, QRadialGradient,
)
from PySide6.QtQml import QQmlImageProviderBase
from PySide6.QtQuick import QQuickImageProvider, QQuickView
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

logger = logging.getLogger(__name__)

KATALOG = os.path.dirname(os.path.abspath(__file__))

# Ikona zasobnika i okna. Generujemy ją przy pierwszym uruchomieniu — podmień plik
# na własną grafikę, kiedy będziesz miał lepszą (32x32 albo 64x64 PNG).
SCIEZKA_IKONY = os.path.join(KATALOG, "jarvis_icon.png")

# Scena i shader. hud.frag to źródło shadera, a Qt wczytuje jego
# skompilowaną wersję hud.frag.qsb (opis kompilacji na górze hud.frag).
SCIEZKA_QML = os.path.join(KATALOG, "hud.qml")

CZAS_BLYSKU_BLEDU_MS = 900   # jak długo trwa czerwony błysk, zanim wrócimy do idle

# --- Paleta ---

BIALY = (220, 242, 255)
BURSZTYN = (255, 186, 64)
TLO_SRODEK = (9, 30, 42)
TLO_KRAWEDZ = (2, 6, 10)
SIATKA = (60, 150, 190)

# --- Stany ---
#
# Te wartości czyta hud.qml w każdej klatce.
#
#   kolor     — główna barwa pierścieni
#   akcent    — barwa łuków wyróżniających (w filmie bursztynowych)
#   etykieta  — napis pod J.A.R.V.I.S.
#   tempo     — szybkość pulsowania jasności
#   min/max   — zakres jasności pulsu (0.0-1.0)
#   hud       — obrót zewnętrznej podziałki (stopni/s)
#   seg       — obrót wieńca segmentów (stopni/s)
#   wewn      — obrót łuków wewnętrznych (stopni/s, minus = w drugą stronę)
#   bieg      — prędkość światła biegnącego po segmentach (obrotów/s)
#   smuga     — czy świetlna smuga obiega wieniec (stan "myślę")
#   fale_co   — co ile sekund z tarczy wylatuje fala energii (0 = wcale)
#   migot     — siła nerwowego migotania (0 = spokojnie)
#
# Wzór zapalonych segmentów wieńca (które z 72 świecą pełnym światłem)
# siedzi w hud.frag, bo to shader decyduje o każdym pikselu segmentu.
STANY = {
    "idle": {
        "kolor": (58, 168, 222),
        "akcent": BURSZTYN,
        "etykieta": "CZUWAM",
        "tempo": 0.9,
        "min": 0.58,
        "max": 0.74,
        "hud": 3.0,
        "seg": 2.0,
        "wewn": -6.0,
        "bieg": 0.07,
        "smuga": False,
        "fale_co": 4.5,
        "migot": 0.0,
    },
    "listening": {
        "kolor": (86, 214, 255),
        "akcent": BURSZTYN,
        "etykieta": "SŁUCHAM",
        "tempo": 3.6,
        "min": 0.80,
        "max": 1.00,
        "hud": 11.0,
        "seg": 9.0,
        "wewn": -22.0,
        "bieg": 0.55,
        "smuga": False,
        "fale_co": 1.1,
        "migot": 0.0,
    },
    # Myślenie przestawia cały HUD na bursztyn — w filmie ten kolor oznacza,
    # że system coś liczy. Akcent odwrotnie: robi się niebieski, żeby łuki
    # wyróżniające nadal odcinały się od reszty.
    "processing": {
        "kolor": (255, 176, 58),
        "akcent": (130, 222, 255),
        "etykieta": "MYŚLĘ",
        "tempo": 2.2,
        "min": 0.78,
        "max": 0.96,
        "hud": -18.0,
        "seg": 24.0,
        "wewn": 46.0,
        "bieg": 0.0,        # smuga i tak biegnie — dwa ruchy naraz męczą oko
        "smuga": True,
        "fale_co": 0.0,
        "migot": 0.0,
    },
    # Mówienie: jaśniejszy, bielszy cyjan i gęste fale — jakby słowa
    # wylatywały z tarczy.
    "speaking": {
        "kolor": (140, 232, 255),
        "akcent": BURSZTYN,
        "etykieta": "MÓWIĘ",
        "tempo": 5.0,
        "min": 0.80,
        "max": 1.00,
        "hud": 8.0,
        "seg": 6.0,
        "wewn": -16.0,
        "bieg": 1.10,
        "smuga": False,
        "fale_co": 0.45,
        "migot": 0.0,
    },
    "error": {
        "kolor": (255, 64, 64),
        "akcent": (255, 190, 190),
        "etykieta": "BŁĄD",
        "tempo": 8.0,
        "min": 0.55,
        "max": 1.00,
        "hud": 0.0,
        "seg": 0.0,
        "wewn": 0.0,
        "bieg": 0.0,
        "smuga": False,
        "fale_co": 0.0,
        "migot": 0.40,       # awaria ma migotać nerwowo, nie oddychać
    },
    # Brak mikrofonu — np. słuchawki bezprzewodowe jeszcze się nie połączyły.
    # To stan TRWAŁY, nie chwilowy błysk jak "error": trwa, dopóki mikrofon
    # się nie pojawi. Pierścienie stają w miejscu — żywy, obracający się HUD
    # sugerowałby, że Jarvis słucha, a on w tej chwili nie może.
    "no_mic": {
        "kolor": (255, 128, 64),
        "akcent": (255, 205, 170),
        "etykieta": "BRAK MIKROFONU",
        "tempo": 1.4,
        "min": 0.38,
        "max": 0.68,
        "hud": 0.0,
        "seg": 0.0,
        "wewn": 0.0,
        "bieg": 0.0,
        "smuga": False,
        "fale_co": 0.0,
        "migot": 0.0,
    },
    # Wątek nasłuchu padł na błędzie, z którego sam się nie podniesie.
    # Szczegóły są w jarvis.log. Bez tego stanu HUD dalej by się animował
    # jak gdyby nigdy nic — dokładnie tak wyglądała cicha awaria, przez którą
    # powstał ten stan.
    "offline": {
        "kolor": (205, 60, 60),
        "akcent": (255, 170, 170),
        "etykieta": "NASŁUCH NIEAKTYWNY",
        "tempo": 0.7,
        "min": 0.30,
        "max": 0.48,
        "hud": 0.0,
        "seg": 0.0,
        "wewn": 0.0,
        "bieg": 0.0,
        "smuga": False,
        "fale_co": 0.0,
        "migot": 0.0,
    },
}


def _czcionka(rodzina_preferowana, zapasowa="Segoe UI"):
    """Zwraca rodzinę czcionki, jeśli jest w systemie, albo zapasową."""
    if rodzina_preferowana in QFontDatabase.families():
        return rodzina_preferowana
    return zapasowa


def _dla_qml(nazwa_stanu):
    """
    Zamienia wpis ze STANY na postać, którą rozumie QML.

    Krotki (tuple) zamieniamy na listy — w JavaScripcie to tablice,
    z których hud.qml czyta kolor jako p.kolor[0], p.kolor[1], p.kolor[2].
    """
    return {klucz: list(wartosc) if isinstance(wartosc, tuple) else wartosc
            for klucz, wartosc in STANY[nazwa_stanu].items()}


# ---------------------------------------------------------------
# Nieruchome obrazy — rysowane raz, na procesorze
# ---------------------------------------------------------------
#
# Tło z rysunkiem technicznym i podświetlony napis nigdy się nie ruszają,
# a mają dużo drobnych szczegółów (tekst, losowe prostokąty). Taniej je raz
# narysować jako obraz, niż liczyć w shaderze co klatkę.

def _pioro(kolor, alfa, szerokosc):
    pioro = QPen(QColor(int(kolor[0]), int(kolor[1]), int(kolor[2]),
                        int(max(0, min(255, alfa)))))
    pioro.setWidthF(szerokosc)
    pioro.setCapStyle(Qt.FlatCap)
    return pioro


def _okrag(m, cx, cy, r, kolor, alfa):
    m.setBrush(Qt.NoBrush)
    m.setPen(_pioro(kolor, alfa, 1))
    m.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))


def narysuj_tlo(w, h, rodzina_mono):
    """
    Rysuje całą NIERUCHOMĄ część obrazu: gradient, siatkę, rysunek
    techniczny, wnętrze tarczy i winietę.

    w, h — rozmiar w pikselach ekranu
    Zwraca: QImage.
    """
    cx, cy, R = w / 2.0, h / 2.0, min(w, h) * 0.37

    tlo = QImage(w, h, QImage.Format_RGB32)
    m = QPainter(tlo)
    m.setRenderHint(QPainter.Antialiasing)

    # --- Gradient tła: jaśniej w środku, ciemniej przy krawędziach ---
    grad = QRadialGradient(cx, cy, max(w, h) * 0.75)
    grad.setColorAt(0.0, QColor(*TLO_SRODEK))
    grad.setColorAt(1.0, QColor(*TLO_KRAWEDZ))
    m.fillRect(0, 0, w, h, grad)

    # --- Siatka jak na kalce technicznej ---
    drobna = []
    gruba = []
    for x in range(0, w, 32):
        (gruba if x % 160 == 0 else drobna).append(QLineF(x, 0, x, h))
    for y in range(0, h, 32):
        (gruba if y % 160 == 0 else drobna).append(QLineF(0, y, w, y))
    m.setPen(_pioro(SIATKA, 11, 1))
    m.drawLines(drobna)
    m.setPen(_pioro(SIATKA, 22, 1))
    m.drawLines(gruba)

    # --- Rysunek techniczny: prostokąty, linie i drobne opisy ---
    # Ziarno losowania jest stałe, więc rysunek wygląda tak samo przy każdym
    # starcie, a nie "przetasowuje się" przy zmianie rozmiaru okna.
    los = random.Random(1978)
    czcionka_mikro = QFont(rodzina_mono)
    czcionka_mikro.setPixelSize(max(9, int(h * 0.010)))
    m.setFont(czcionka_mikro)

    for _ in range(26):
        x = los.uniform(0, w)
        y = los.uniform(0, h)
        # Omijamy środek — rysunek ma być tłem, nie ma wchodzić pod tarczę.
        if math.hypot(x - cx, y - cy) < R * 1.25:
            continue
        szer = los.uniform(40, 220)
        wys = los.uniform(20, 120)
        m.setPen(_pioro(SIATKA, los.uniform(16, 34), 1))
        m.setBrush(Qt.NoBrush)
        m.drawRect(QRectF(x, y, szer, wys))

        # Wewnętrzne podziały prostokąta, jak na schemacie.
        for _ in range(los.randint(0, 3)):
            dx = los.uniform(0, szer)
            m.drawLine(QLineF(x + dx, y, x + dx, y + wys))

        if los.random() < 0.6:
            m.setPen(_pioro(SIATKA, 45, 1))
            opis = los.choice([
                "SYS.CORE", "PWR.GRID", "NODE", "ARC-", "SEC.LAYER", "DATA.BUS",
                "MK-", "SUIT.LINK", "THRUST", "REPULSOR",
            ]) + f" {los.randint(1, 99):02d}.{los.randint(0, 9999):04d}"
            m.drawText(QPointF(x + 4, y - 4), opis)

    # Kilka długich linii prowadzących — dodają "inżynierskiej" głębi.
    for _ in range(14):
        x1, y1 = los.uniform(0, w), los.uniform(0, h)
        dl = los.uniform(80, 400)
        kat = los.choice([0, 90, 45, 135])
        x2 = x1 + dl * math.cos(math.radians(kat))
        y2 = y1 + dl * math.sin(math.radians(kat))
        m.setPen(_pioro(SIATKA, 20, 1))
        m.drawLine(QLineF(x1, y1, x2, y2))

    # --- Wielkie, blade okręgi daleko za tarczą ---
    for r_tla, alfa in ((1.45, 20), (1.75, 14), (2.1, 9)):
        _okrag(m, cx, cy, R * r_tla, SIATKA, alfa)

    # --- Wnętrze tarczy: ciemny dysk z delikatną poświatą ---
    r_tarczy = R * 0.56
    dysk = QRadialGradient(cx, cy, r_tarczy)
    dysk.setColorAt(0.0, QColor(10, 34, 48, 250))
    dysk.setColorAt(0.8, QColor(5, 18, 26, 250))
    dysk.setColorAt(1.0, QColor(12, 44, 60, 250))
    m.setPen(Qt.NoPen)
    m.setBrush(dysk)
    m.drawEllipse(QRectF(cx - r_tarczy, cy - r_tarczy, r_tarczy * 2, r_tarczy * 2))

    # Szprychy i pierścienie we wnętrzu — mechaniczny detal jak na referencji.
    szprychy = []
    for i in range(36):
        a = math.radians(i * 10)
        r1, r2 = R * 0.30, R * 0.54
        szprychy.append(QLineF(cx + r1 * math.cos(a), cy - r1 * math.sin(a),
                               cx + r2 * math.cos(a), cy - r2 * math.sin(a)))
    m.setPen(_pioro(SIATKA, 22, 1))
    m.drawLines(szprychy)
    for r_wew in (0.30, 0.38, 0.46):
        _okrag(m, cx, cy, R * r_wew, SIATKA, 26)

    # --- Winieta: przyciemnione rogi skupiają wzrok na środku ---
    winieta = QRadialGradient(cx, cy, max(w, h) * 0.72)
    winieta.setColorAt(0.55, QColor(0, 0, 0, 0))
    winieta.setColorAt(1.0, QColor(0, 0, 0, 170))
    m.fillRect(0, 0, w, h, winieta)

    m.end()
    return tlo


def narysuj_napis(R, rodzina):
    """
    Rysuje podświetlony napis J.A.R.V.I.S.

    Poświata powstaje z kilku coraz węższych i coraz mniej przezroczystych
    obrysów tego samego kształtu liter.

    R — promień HUD-a w pikselach ekranu (od niego zależy wielkość liter)
    Zwraca: QImage z przezroczystym tłem.
    """
    rozmiar = int(R * 0.17)

    czcionka = QFont(rodzina)
    czcionka.setPixelSize(max(12, rozmiar))
    czcionka.setWeight(QFont.DemiBold)
    czcionka.setLetterSpacing(QFont.AbsoluteSpacing, rozmiar * 0.06)

    sciezka = QPainterPath()
    sciezka.addText(0, 0, czcionka, "J.A.R.V.I.S.")
    ramka = sciezka.boundingRect()

    margines = rozmiar * 0.6
    obraz = QImage(int(ramka.width() + margines * 2), int(ramka.height() + margines * 2),
                   QImage.Format_ARGB32_Premultiplied)
    obraz.fill(Qt.transparent)
    m = QPainter(obraz)
    m.setRenderHint(QPainter.Antialiasing)
    m.translate(margines - ramka.left(), margines - ramka.top())

    for grubosc, alfa in ((rozmiar * 0.42, 14), (rozmiar * 0.26, 26),
                          (rozmiar * 0.14, 48), (rozmiar * 0.06, 90)):
        pioro = QPen(QColor(120, 215, 255, alfa), grubosc)
        pioro.setJoinStyle(Qt.RoundJoin)
        m.strokePath(sciezka, pioro)

    m.fillPath(sciezka, QColor(*BIALY))
    m.end()
    return obraz


class DostawcaObrazow(QQuickImageProvider):
    """
    Podaje scenie QML obrazy narysowane w Pythonie.

    W hud.qml obraz ma adres w rodzaju "image://jarvis/tlo/1920x1080".
    Qt rozpoznaje przedrostek "image://jarvis/" i pyta ten obiekt o resztę:
    "tlo/1920x1080". Rozmiar siedzi w adresie celowo — po zmianie rozmiaru
    okna zmienia się adres, więc Qt samo poprosi o nowy obraz.
    """

    def __init__(self, rodzina, rodzina_mono):
        super().__init__(QQmlImageProviderBase.ImageType.Image)
        self._rodzina = rodzina
        self._rodzina_mono = rodzina_mono

    def requestImage(self, id_obrazu, rozmiar, zadany_rozmiar):
        rodzaj, _, parametr = id_obrazu.partition("/")
        try:
            if rodzaj == "tlo":
                w, h = (int(x) for x in parametr.split("x"))
                return narysuj_tlo(w, h, self._rodzina_mono)
            if rodzaj == "napis":
                return narysuj_napis(int(parametr), self._rodzina)
        except Exception:
            # Wyjątek wewnątrz Qt zniknąłby bez śladu — logujemy go sami.
            logger.exception("Nie udało się narysować obrazu %r", id_obrazu)
        return QImage()


# ---------------------------------------------------------------
# Okno HUD-a
# ---------------------------------------------------------------

class _Most(QObject):
    """
    Most między Pythonem a sceną QML: dwa sygnały, które odbiera hud.qml.

    Sygnały MUSZĄ być zadeklarowane jako atrybuty klasy (nie w __init__) —
    Qt skanuje klasę przy jej tworzeniu i buduje na tej podstawie metaobiekt.
    Signal(str) znaczy: "to ogłoszenie niesie ze sobą jeden argument typu string".

    Odbiorcą jest kod w hud.qml (blok Connections), a NIE metoda Pythona.
    To celowe: gdy sygnał wyśle wątek nasłuchu, Qt dostarczy go w wątku GUI —
    i gdyby odbiorcą był Python, wątek GUI musiałby najpierw zaczekać na GIL.
    Pomiar pokazał, że właśnie takie czekanie przycinało animację (do 0,1 s).
    Odbiorca w QML nie czeka na nic.
    """

    stan = Signal(str)        # nazwa nowego stanu
    odczyty = Signal(list)    # wiersze do rogu ekranu (procesor, pamięć, sieć, temperatura)


class JarvisHUD(QQuickView):
    """
    Pełnoekranowe okno z animowanym HUD-em.

    Użycie z innego wątku:
        hud.set_state("listening")   # zawsze bezpieczne, patrz opis na górze pliku

    Klawisze:
        ESC — schowaj okno (Jarvis dalej działa w zasobniku)
        F11 albo podwójne kliknięcie — przełącz pełny ekran / okno
    """

    def __init__(self):
        super().__init__()

        # Zwykłe okno aplikacji, NIE "zawsze na wierzchu". Na pełnym ekranie
        # okno zawsze-na-wierzchu zablokowałoby cały komputer — nie dałoby się
        # przejść Alt+Tabem do innego programu.
        self.setTitle("J.A.R.V.I.S.")
        self.setIcon(QIcon(utworz_plik_ikony()))
        self.setColor(QColor(*TLO_KRAWEDZ))
        self.setMinimumSize(QSize(640, 480))
        self.resize(1280, 800)
        # Scena rozciąga się razem z oknem.
        self.setResizeMode(QQuickView.SizeRootObjectToView)

        self._pelny_ekran = True
        self._ostatni_stan = "idle"

        rodzina = _czcionka("Bahnschrift")
        rodzina_mono = _czcionka("Consolas", "Courier New")

        # Trzymamy referencje — obiekty muszą żyć tak długo jak scena.
        self._dostawca = DostawcaObrazow(rodzina, rodzina_mono)
        self.engine().addImageProvider("jarvis", self._dostawca)
        self._most = _Most()

        # Wartości startowe ustawiamy PRZED wczytaniem sceny, żeby pierwsza
        # klatka od razu była poprawna, a nie mrugnęła domyślnymi.
        # Scena dostaje od razu WSZYSTKIE stany — potem wystarczy jej nazwa.
        self.setInitialProperties({
            "rodzina": rodzina,
            "rodzinaMono": rodzina_mono,
            "stany": {nazwa: _dla_qml(nazwa) for nazwa in STANY},
            "czasBlyskuBleduMs": CZAS_BLYSKU_BLEDU_MS,
            "most": self._most,
        })
        self.setSource(QUrl.fromLocalFile(SCIEZKA_QML))

        if self.status() != QQuickView.Status.Ready:
            bledy = "; ".join(blad.toString() for blad in self.errors())
            raise RuntimeError(f"Nie udało się wczytać {SCIEZKA_QML}: {bledy}")

        # Odczyt procesora i pamięci do narożnika ekranu — w osobnym wątku,
        # z tego samego powodu co w opisie klasy _Most: w wątku GUI nie może
        # regularnie wykonywać się Python. Raz na sekundę wystarczy.
        watek = threading.Thread(target=self._petla_statystyk, name="hud-statystyki",
                                 daemon=True)
        watek.start()

    # ---------------------------------------------------------------
    # Publiczne API — to woła reszta programu
    # ---------------------------------------------------------------

    def set_state(self, nazwa_stanu):
        """
        Zmienia stan animacji. BEZPIECZNE do wywołania z dowolnego wątku.

        nazwa_stanu — "idle", "listening", "processing", "speaking", "error",
                      "no_mic" albo "offline"

        Ta metoda niczego nie zmienia sama. Emituje sygnał, a Qt dostarczy go
        scenie w wątku GUI. Resztę — płynne przejście kolorów, nowe tempo
        obrotów, powrót z błysku błędu do idle — robi hud.qml.
        """
        if nazwa_stanu not in STANY:
            raise ValueError(
                f"Nieznany stan: {nazwa_stanu!r}. Dostępne: {list(STANY)}"
            )

        # Zapamiętujemy, żeby przypomnienie mogło po sobie wrócić do tego,
        # co było wcześniej (np. "brak mikrofonu"), zamiast zawsze do "czuwam".
        # Przypisanie jednej zmiennej jest w Pythonie bezpieczne międzywątkowo.
        self._ostatni_stan = nazwa_stanu
        self._most.stan.emit(nazwa_stanu)

    def stan_teraz(self):
        """Ostatni stan zgłoszony przez set_state(). Bezpieczne z dowolnego wątku."""
        return self._ostatni_stan

    def pokaz(self):
        """
        Pokazuje okno w trybie, w którym było ostatnio (pełny ekran albo okno).

        Samo show() zawsze otworzyłoby zwykłe okno — nawet jeśli schowałeś
        Jarvisa z pełnego ekranu. Stąd osobna metoda, która pamięta tryb.
        Wołać tylko z wątku GUI (robi to zasobnik systemowy).
        """
        if self._pelny_ekran:
            self.showFullScreen()
        else:
            self.showNormal()
        self.raise_()
        self.requestActivate()

    def _petla_statystyk(self):
        """
        Wątek w tle: raz na sekundę zbiera odczyty i wysyła je scenie.

        cpu_percent(interval=1.0) sam czeka tę sekundę i zwraca średnie
        obciążenie z tego czasu — nie trzeba osobnego sleep().

        Gotowe napisy składamy TUTAJ, a nie w QML. Dzięki temu scena nie musi
        nic wiedzieć o megabitach ani o tym, skąd bierze się temperatura —
        dostaje listę wierszy: etykieta, wartość, podpis i wypełnienie paska.
        """
        import czujniki

        def liczba(wartosc, miejsca=1):
            """Polski zapis liczby: przecinek zamiast kropki."""
            return f"{wartosc:.{miejsca}f}".replace(".", ",")

        siec = czujniki.PomiarSieci()
        lacze = czujniki.predkosc_lacza() or 100.0
        szczyt_temperatury = 0.0

        while True:
            cpu = psutil.cpu_percent(interval=1.0)
            ram = psutil.virtual_memory().percent
            pobieranie, wysylanie = siec.odczyt()

            wiersze = [
                {"etykieta": "CPU", "wartosc": f"{cpu:.0f} %", "podpis": "",
                 "wypelnienie": cpu / 100.0, "alarm": cpu > 80},
                {"etykieta": "RAM", "wartosc": f"{ram:.0f} %", "podpis": "",
                 "wypelnienie": ram / 100.0, "alarm": ram > 80},
                {"etykieta": "SIEĆ",
                 "wartosc": f"↓ {liczba(pobieranie)}   ↑ {liczba(wysylanie)} Mb/s",
                 "podpis": f"szczyt ↓ {liczba(siec.szczyt_pobierania)} "
                           f"/ łącze {lacze:.0f} Mb/s",
                 # Pasek pokazuje, jaką część łącza zajmuje ruch.
                 "wypelnienie": min(1.0, pobieranie / lacze), "alarm": False},
            ]

            temperatura = czujniki.temperatura()
            if temperatura:
                szczyt_temperatury = max(szczyt_temperatury, temperatura["teraz"])
                udzial = temperatura["teraz"] / temperatura["limit"]
                wiersze.append({
                    "etykieta": temperatura["etykieta"],
                    "wartosc": f"{temperatura['teraz']:.0f} °C",
                    "podpis": f"maks {szczyt_temperatury:.0f} "
                              f"/ limit {temperatura['limit']:.0f} °C",
                    "wypelnienie": min(1.0, udzial),
                    "alarm": udzial > czujniki.PROG_ALARMU_TEMPERATURY,
                })

            try:
                self._most.odczyty.emit(wiersze)
            except RuntimeError:
                # Przy zamykaniu programu Qt kasuje obiekty, a ten wątek
                # mógł akurat skończyć pomiar. Nie ma już komu wysyłać.
                return

    # ---------------------------------------------------------------
    # Klawiatura, mysz, zamykanie
    # ---------------------------------------------------------------

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            # Chowamy, nie zamykamy — Jarvis dalej słucha w tle,
            # a z powrotem przywołasz go z ikony w zasobniku.
            self.hide()
            logger.info("HUD schowany klawiszem ESC.")
        elif event.key() == Qt.Key_F11:
            self._przelacz_pelny_ekran()
        else:
            super().keyPressEvent(event)

    def mouseDoubleClickEvent(self, event):
        """Podwójne kliknięcie przełącza pełny ekran i okno."""
        self._przelacz_pelny_ekran()

    def _przelacz_pelny_ekran(self):
        self._pelny_ekran = not self._pelny_ekran
        self.pokaz()

    def closeEvent(self, event):
        """
        Krzyżyk w trybie okienkowym CHOWA Jarvisa zamiast go zamykać.

        Program ma dalej działać w tle, a prawdziwe zamknięcie jest w menu
        zasobnika. Gdyby krzyżyk zamykał program, łatwo byłoby wyłączyć
        asystenta przez przypadek.
        """
        event.ignore()
        self.hide()
        logger.info("HUD schowany (krzyżyk okna).")


def utworz_plik_ikony(sciezka=SCIEZKA_IKONY, rozmiar=64):
    """
    Generuje prostą ikonę zasobnika — świecące niebieskie kółko — jeśli plik
    jeszcze nie istnieje.

    To rozwiązanie tymczasowe, żeby projekt działał bez dostarczania grafiki
    z zewnątrz. Podmień jarvis_icon.png na własny plik, kiedy będziesz miał lepszy
    — kod go po prostu wczyta, bo generuje tylko wtedy, gdy pliku brakuje.

    Zwraca: ścieżkę do pliku ikony.
    """
    if os.path.exists(sciezka):
        return sciezka

    # QPixmap z kanałem alfa — przezroczyste tło, żeby ikona wyglądała dobrze
    # zarówno na jasnym, jak i ciemnym pasku zadań.
    pixmapa = QPixmap(rozmiar, rozmiar)
    pixmapa.fill(Qt.transparent)

    malarz = QPainter(pixmapa)
    malarz.setRenderHint(QPainter.Antialiasing)

    srodek = rozmiar / 2.0
    gradient = QRadialGradient(srodek, srodek, srodek)
    gradient.setColorAt(0.0, QColor(210, 245, 255, 255))
    gradient.setColorAt(0.4, QColor(0, 190, 255, 255))
    gradient.setColorAt(1.0, QColor(0, 90, 160, 210))

    malarz.setBrush(gradient)
    malarz.setPen(Qt.NoPen)
    # Mały margines (10%), żeby kółko nie dotykało krawędzi ikony.
    malarz.drawEllipse(QRectF(rozmiar * 0.05, rozmiar * 0.05, rozmiar * 0.9, rozmiar * 0.9))
    malarz.end()

    pixmapa.save(sciezka)
    logger.info("Wygenerowałem ikonę zasobnika: %s", sciezka)

    return sciezka


class TrayJarvisa:
    """
    Ikona w zasobniku systemowym (na Windows: obszar przy zegarku, często
    schowany pod strzałką "Pokaż ukryte ikony").


    JAK DZIAŁA QSystemTrayIcon
    ==========================

    QSystemTrayIcon to nie okno, tylko uchwyt do ikony, którą rysuje sam system
    operacyjny w swoim pasku. Składa się z trzech rzeczy:

      1. IKONA (QIcon) — obrazek, który widać przy zegarku.
      2. MENU KONTEKSTOWE (QMenu) — pokazywane po kliknięciu prawym przyciskiem.
         Menu podpinamy przez setContextMenu(); resztą (gdzie je wyświetlić,
         jak zamknąć po kliknięciu) zajmuje się system.
      3. SYGNAŁ activated — informuje o kliknięciach lewym, podwójnych itd.

    Trzy rzeczy, które łatwo przeoczyć:

      - Trzeba wywołać .show(). Bez tego ikona istnieje w pamięci,
        ale nigdzie jej nie widać.

      - Musisz trzymać w Pythonie referencję do obiektu tray ORAZ do menu.
        Jeśli powstaną jako zmienne lokalne w funkcji, garbage collector
        posprząta je po jej zakończeniu i ikona zniknie po ułamku sekundy.
        Dlatego trzymamy je jako pola tej klasy, a main.py trzyma jej instancję.

      - Trzeba ustawić app.setQuitOnLastWindowClosed(False). Domyślnie Qt kończy
        program po zamknięciu ostatniego okna — czyli schowanie HUD-a
        zabiłoby całą aplikację, mimo że ikona w zasobniku dalej by tam była.
    """

    def __init__(self, app, okno, przy_zamknieciu=None):
        """
        app             — obiekt QApplication
        okno            — pełnoekranowy HUD Jarvisa
        przy_zamknieciu — opcjonalna funkcja sprzątająca, wołana przed wyjściem
                          (main.py przekazuje tu zatrzymanie wątku nasłuchu)
        """
        self._app = app
        self._okno = okno
        self._przy_zamknieciu = przy_zamknieciu

        # Bez tego schowanie HUD-a (ESC) zamknęłoby cały program.
        app.setQuitOnLastWindowClosed(False)

        ikona = QIcon(utworz_plik_ikony())

        # Menu MUSI zostać polem obiektu — patrz uwaga o referencjach powyżej.
        self._menu = QMenu()

        self._akcja_widocznosc = QAction("Schowaj Jarvisa", self._menu)
        self._akcja_widocznosc.triggered.connect(self._przelacz_widocznosc)
        self._menu.addAction(self._akcja_widocznosc)

        # HUD można schować także klawiszem ESC, z pominięciem menu.
        # aboutToShow odpala się tuż przed pokazaniem menu, więc to dobre miejsce,
        # żeby napis zawsze zgadzał się z rzeczywistym stanem okna.
        self._menu.aboutToShow.connect(self._odswiez_napis)

        self._menu.addSeparator()

        akcja_zamknij = QAction("Zamknij Jarvisa", self._menu)
        akcja_zamknij.triggered.connect(self._zamknij)
        self._menu.addAction(akcja_zamknij)

        self._tray = QSystemTrayIcon(ikona, app)
        self._tray.setToolTip("Jarvis — asystent głosowy")
        self._tray.setContextMenu(self._menu)

        # Lewym przyciskiem też wygodnie przełączać widoczność HUD-a.
        self._tray.activated.connect(self._klikniecie)

        self._tray.show()
        logger.info("Ikona zasobnika systemowego uruchomiona.")

    def _klikniecie(self, powod):
        """
        Reakcja na kliknięcie ikony. `powod` mówi, jakiego rodzaju było kliknięcie
        — reagujemy tylko na pojedyncze lewe (Trigger), bo prawe obsługuje menu.
        """
        if powod == QSystemTrayIcon.Trigger:
            self._przelacz_widocznosc()

    def _odswiez_napis(self):
        """Dopasowuje napis w menu do tego, czy HUD jest aktualnie widoczny."""
        self._akcja_widocznosc.setText(
            "Schowaj Jarvisa" if self._okno.isVisible() else "Pokaż Jarvisa"
        )

    def _przelacz_widocznosc(self):
        """Pokazuje albo chowa HUD i aktualizuje napis w menu."""
        if self._okno.isVisible():
            self._okno.hide()
            logger.info("HUD schowany.")
        else:
            # pokaz(), a nie show() — przywraca pełny ekran, jeśli w nim był.
            self._okno.pokaz()
            logger.info("HUD pokazany.")

        self._odswiez_napis()

    def _zamknij(self):
        """
        Kończy cały program: najpierw sprząta (wątek nasłuchu, mikrofon),
        potem chowa ikonę i zatrzymuje pętlę zdarzeń Qt.

        Kolejność ma znaczenie — ikona zasobnika potrafi zostać widoczna
        jeszcze chwilę po zamknięciu programu, jeśli nie ukryjemy jej jawnie.
        """
        logger.info("Zamykanie przez menu zasobnika.")

        if self._przy_zamknieciu is not None:
            self._przy_zamknieciu()

        self._tray.hide()
        self._app.quit()


# --- Test: `python gui.py` — HUD na pełnym ekranie, stany zmieniają się co 3 s ---
if __name__ == "__main__":
    from PySide6.QtGui import QKeySequence, QShortcut
    from PySide6.QtWidgets import QApplication

    from logging_setup import skonfiguruj_logowanie

    skonfiguruj_logowanie()

    app = QApplication(sys.argv)

    hud = JarvisHUD()
    hud.pokaz()

    # Referencję do tray trzeba przechować — inaczej ikona zniknie po chwili.
    tray = TrayJarvisa(app, hud)

    # W trybie testowym Ctrl+Q zamyka od razu — normalnie służy do tego zasobnik.
    wyjscie = QShortcut(QKeySequence("Ctrl+Q"), hud)
    wyjscie.activated.connect(app.quit)

    KOLEJNOSC = ["idle", "listening", "processing", "speaking", "error"]
    indeks = 0

    def nastepny_stan():
        """Przełącza na kolejny stan z listy."""
        global indeks
        stan = KOLEJNOSC[indeks % len(KOLEJNOSC)]
        indeks += 1
        logger.info("[GUI] stan: %s", stan)
        hud.set_state(stan)

    timer_demo = QTimer()
    timer_demo.timeout.connect(nastepny_stan)
    timer_demo.start(3000)
    nastepny_stan()

    logger.info("ESC chowa, F11 przełącza okno, Ctrl+Q zamyka test.")
    sys.exit(app.exec())
