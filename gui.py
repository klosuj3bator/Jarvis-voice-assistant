"""
gui.py — wizualny interfejs Jarvisa: świecące, pulsujące koło w stylu Iron Mana.

Na razie to samodzielny moduł — nie wie nic o mikrofonie ani o Spotify.
Wystawia jedną metodę, set_state(), którą reszta programu będzie wołać,
żeby powiedzieć "teraz słucham" albo "teraz myślę".


JAK DZIAŁA KOMUNIKACJA MIĘDZY WĄTKAMI (to jest tu najważniejsze)
================================================================

Problem: biblioteki graficzne — Qt, ale też każda inna — pozwalają dotykać
widgetów TYLKO z tego wątku, w którym powstały (wątek GUI, ten z pętlą zdarzeń).
Jeśli inny wątek, np. ten nasłuchujący mikrofonu, wywoła bezpośrednio
`okno.kolor = czerwony`, program albo się wysypie, albo — gorzej — będzie
działał losowo: raz dobrze, raz nie, zależnie od tego, co akurat robiło Qt
w tym samym momencie. To klasyczny wyścig (race condition).

Rozwiązanie Qt: sygnały i sloty.

  - SYGNAŁ (Signal) to "ogłoszenie" — coś się stało.
  - SLOT to zwykła metoda, która na to ogłoszenie reaguje.
  - .connect() łączy jedno z drugim.

Sztuczka jest w tym, CO Qt robi, gdy sygnał jest wyemitowany z innego wątku
niż ten, w którym żyje odbiorca. Domyślne połączenie (Qt.AutoConnection)
sprawdza to w momencie emisji:

  - ten sam wątek  -> slot wywoływany od razu, jak zwykła funkcja;
  - inny wątek     -> Qt PAKUJE wywołanie razem z argumentami i wrzuca je
                      do kolejki zdarzeń wątku GUI. Wątek GUI wykona je
                      sam, w bezpiecznym momencie, między klatkami animacji.

Czyli: emit() z obcego wątku nie zmienia niczego natychmiast — zostawia
"liścik" dla wątku GUI. Nikt niczego nie dotyka jednocześnie, więc nie ma wyścigu.

Dlatego set_state() poniżej NIE ustawia stanu wprost. Robi jedną rzecz:
emituje sygnał. Faktyczna zmiana dzieje się w slocie _zastosuj_stan(),
który zawsze wykonuje się w wątku GUI — niezależnie od tego, kto zawołał.
"""

import logging
import math
import os
import sys

from PySide6.QtCore import QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPen, QPixmap, QRadialGradient
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

logger = logging.getLogger(__name__)

# Ikona zasobnika. Generujemy ją przy pierwszym uruchomieniu — podmień plik
# na własną grafikę, kiedy będziesz miał lepszą (32x32 albo 64x64 PNG).
SCIEZKA_IKONY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis_icon.png")

# --- Wygląd okienka ---

ROZMIAR_OKNA = 220          # okno jest kwadratowe, w pikselach
KLATKA_MS = 16              # ~60 klatek na sekundę
CZAS_BLYSKU_BLEDU_MS = 900  # jak długo trwa czerwony błysk, zanim wrócimy do idle

# Parametry każdego stanu w jednym miejscu — łatwo podkręcić wygląd
# bez grzebania w kodzie rysowania.
#
#   kolor     — barwa poświaty (R, G, B)
#   tempo     — prędkość pulsowania (wyżej = szybciej)
#   min/max   — jak mocno koło "oddycha" (0.0-1.0 promienia)
#   jasnosc   — mnożnik przezroczystości poświaty
#   pierscien — czy rysować obracający się łuk (stan processing)
STANY = {
    "idle": {
        "kolor": (0, 180, 255),
        "tempo": 1.2,
        "min": 0.62,
        "max": 0.72,
        "jasnosc": 0.75,
        "pierscien": False,
    },
    "listening": {
        "kolor": (0, 220, 255),
        "tempo": 4.5,
        "min": 0.58,
        "max": 0.85,
        "jasnosc": 1.0,
        "pierscien": False,
    },
    "processing": {
        "kolor": (255, 140, 0),
        "tempo": 2.5,
        "min": 0.60,
        "max": 0.70,
        "jasnosc": 0.95,
        "pierscien": True,
    },
    "error": {
        "kolor": (255, 40, 40),
        "tempo": 9.0,
        "min": 0.55,
        "max": 0.90,
        "jasnosc": 1.0,
        "pierscien": False,
    },
}


class JarvisOrb(QWidget):
    """
    Bezramkowe okienko z pulsującym kołem.

    Użycie z innego wątku:
        orb.set_state("listening")   # zawsze bezpieczne, patrz opis na górze pliku
    """

    # Sygnał MUSI być zadeklarowany jako atrybut klasy (nie w __init__) —
    # Qt skanuje klasę przy jej tworzeniu i buduje na tej podstawie metaobiekt.
    # Signal(str) znaczy: "to ogłoszenie niesie ze sobą jeden argument typu string".
    stan_zmieniony = Signal(str)

    def __init__(self):
        super().__init__()

        # frameless   — bez paska tytułu i obramowania systemowego
        # always-on-top — okno zostaje nad innymi
        # Tool        — nie pokazuje się na pasku zadań (to widget pomocniczy, nie aplikacja)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )

        # Bez tego tło byłoby szarym prostokątem zamiast przezroczystości.
        self.setAttribute(Qt.WA_TranslucentBackground)

        self.setFixedSize(ROZMIAR_OKNA, ROZMIAR_OKNA)

        # --- Stan animacji ---
        self._stan = "idle"
        self._faza = 0.0   # rośnie w nieskończoność; sinus z niej robi puls
        self._obrot = 0.0  # kąt obracającego się pierścienia (stan processing)

        # Tu spinamy sygnał ze slotem. Od tego momentu każde wywołanie
        # self.stan_zmieniony.emit("cos") skończy się wywołaniem _zastosuj_stan("cos")
        # — natychmiast, jeśli emitował wątek GUI, albo przez kolejkę zdarzeń,
        # jeśli emitował ktokolwiek inny.
        self.stan_zmieniony.connect(self._zastosuj_stan)

        # Timer animacji. QTimer należy do wątku GUI, więc _tick() zawsze
        # wykonuje się bezpiecznie — to on napędza cały ruch na ekranie.
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(KLATKA_MS)

        # Do przeciągania okna myszą (bezramkowego okna nie da się ruszyć inaczej).
        self._punkt_chwytu = None

    # ---------------------------------------------------------------
    # Publiczne API — to woła reszta programu
    # ---------------------------------------------------------------

    def set_state(self, nazwa_stanu):
        """
        Zmienia stan animacji. BEZPIECZNE do wywołania z dowolnego wątku.

        nazwa_stanu — "idle", "listening", "processing" albo "error"

        Zwróć uwagę, że ta metoda nie przypisuje niczego do self.
        Robi jedną rzecz: emituje sygnał. Całą resztą zajmie się Qt,
        wykonując slot _zastosuj_stan() w wątku GUI.
        """
        if nazwa_stanu not in STANY:
            raise ValueError(
                f"Nieznany stan: {nazwa_stanu!r}. Dostępne: {list(STANY)}"
            )

        self.stan_zmieniony.emit(nazwa_stanu)

    # ---------------------------------------------------------------
    # Sloty — zawsze wykonywane w wątku GUI
    # ---------------------------------------------------------------

    @Slot(str)
    def _zastosuj_stan(self, nazwa_stanu):
        """
        Faktycznie zmienia stan. Dekorator @Slot(str) mówi Qt, jakiego typu
        argument przyjmuje ta metoda — dzięki temu Qt umie ją poprawnie
        zapakować i wywołać przez kolejkę zdarzeń przy połączeniu międzywątkowym.

        Ta metoda jest prywatna (podkreślnik) właśnie dlatego, że nikt
        z zewnątrz nie powinien jej wołać wprost — od tego jest set_state().
        """
        self._stan = nazwa_stanu
        self._faza = 0.0  # reset, żeby każdy stan zaczynał się od tego samego miejsca

        # Błąd jest chwilowy: po chwili sam wraca do idle.
        # singleShot to "zrób to raz, za N milisekund" — również w wątku GUI.
        if nazwa_stanu == "error":
            QTimer.singleShot(CZAS_BLYSKU_BLEDU_MS, self._powrot_do_idle)

    @Slot()
    def _powrot_do_idle(self):
        """Wraca do idle po błysku błędu — chyba że w międzyczasie stan już się zmienił."""
        if self._stan == "error":
            self._zastosuj_stan("idle")

    def _tick(self):
        """
        Jedna klatka animacji: przesuwa fazę pulsu i kąt obrotu,
        po czym prosi Qt o przerysowanie okna.

        update() nie rysuje od razu — zgłasza tylko, że okno jest nieaktualne.
        Qt zawoła paintEvent() w dogodnym momencie.
        """
        parametry = STANY[self._stan]

        self._faza += parametry["tempo"] * (KLATKA_MS / 1000.0)
        self._obrot = (self._obrot + 4.0) % 360.0

        self.update()

    # ---------------------------------------------------------------
    # Rysowanie
    # ---------------------------------------------------------------

    def paintEvent(self, event):
        """
        Rysuje całą grafikę. Qt woła tę metodę samo, po każdym update().

        Warstwy, od dołu do góry:
          1. rozmyta poświata (gradient radialny) — to daje efekt świecenia
          2. jasny rdzeń w środku
          3. obracający się łuk (tylko w stanie processing)
        """
        parametry = STANY[self._stan]
        r, g, b = parametry["kolor"]

        # sin() daje wartość -1..1; przesuwamy ją do 0..1, żeby użyć jako suwaka
        # między "min" a "max" promienia. To jest cały mechanizm pulsowania.
        oddech = (math.sin(self._faza) + 1.0) / 2.0
        skala = parametry["min"] + (parametry["max"] - parametry["min"]) * oddech

        srodek = self.width() / 2.0
        promien = srodek * skala

        malarz = QPainter(self)
        # Antyaliasing wygładza krawędzie — bez niego koło ma "schodki".
        malarz.setRenderHint(QPainter.Antialiasing)

        # --- 1. Poświata ---
        # Gradient radialny: od środka na zewnątrz. Kilka przystanków (setColorAt)
        # z malejącą nieprzezroczystością daje miękkie, świecące wybrzuszenie
        # zamiast twardego koła.
        jasnosc = parametry["jasnosc"]
        gradient = QRadialGradient(srodek, srodek, promien)
        gradient.setColorAt(0.00, QColor(r, g, b, int(230 * jasnosc)))
        gradient.setColorAt(0.35, QColor(r, g, b, int(150 * jasnosc)))
        gradient.setColorAt(0.70, QColor(r, g, b, int(50 * jasnosc)))
        gradient.setColorAt(1.00, QColor(r, g, b, 0))  # całkiem przezroczysty brzeg

        malarz.setBrush(gradient)
        malarz.setPen(Qt.NoPen)  # bez obrysu — sam gradient
        # QRectF (a nie cztery luźne liczby), bo metody rysujące Qt mają
        # przeciążenia na int — przekazanie floatów wprost gubiłoby ułamki pikseli.
        malarz.drawEllipse(
            QRectF(srodek - promien, srodek - promien, promien * 2, promien * 2)
        )

        # --- 2. Jasny rdzeń ---
        # Drugi, mniejszy gradient rozjaśniony do bieli daje wrażenie,
        # że światło bije ze środka.
        promien_rdzenia = promien * 0.35
        rdzen = QRadialGradient(srodek, srodek, promien_rdzenia)
        rdzen.setColorAt(0.0, QColor(255, 255, 255, int(220 * jasnosc)))
        rdzen.setColorAt(1.0, QColor(r, g, b, 0))

        malarz.setBrush(rdzen)
        malarz.drawEllipse(
            QRectF(
                srodek - promien_rdzenia, srodek - promien_rdzenia,
                promien_rdzenia * 2, promien_rdzenia * 2,
            )
        )

        # --- 3. Obracający się pierścień (tylko processing) ---
        if parametry["pierscien"]:
            # Tworzymy NOWE pióro zamiast modyfikować malarz.pen().
            # Wyżej ustawiliśmy Qt.NoPen dla gradientów, a to pióro ma styl
            # "nie rysuj" — samo podmienienie mu koloru nic by nie dało
            # i pierścień pozostałby niewidoczny.
            pioro = QPen(QColor(r, g, b, 230))
            pioro.setWidth(4)
            pioro.setCapStyle(Qt.RoundCap)
            malarz.setPen(pioro)
            malarz.setBrush(Qt.NoBrush)

            promien_pierscienia = srodek * 0.80
            prostokat = QRectF(
                srodek - promien_pierscienia,
                srodek - promien_pierscienia,
                promien_pierscienia * 2,
                promien_pierscienia * 2,
            )

            # Qt podaje kąty w 1/16 stopnia — stąd mnożenie przez 16.
            # Rysujemy dwa łuki po 100 stopni, po przeciwnych stronach.
            for przesuniecie in (0, 180):
                malarz.drawArc(
                    prostokat,
                    int((self._obrot + przesuniecie) * 16),
                    int(100 * 16),
                )

        malarz.end()

    # ---------------------------------------------------------------
    # Przeciąganie okna myszą
    # ---------------------------------------------------------------

    def mousePressEvent(self, event):
        """Zapamiętuje, w którym miejscu okna złapaliśmy je myszą."""
        if event.button() == Qt.LeftButton:
            self._punkt_chwytu = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        """Przesuwa okno za kursorem, zachowując miejsce chwytu."""
        if self._punkt_chwytu is not None:
            self.move(event.globalPosition().toPoint() - self._punkt_chwytu)

    def mouseReleaseEvent(self, event):
        self._punkt_chwytu = None

    def mouseDoubleClickEvent(self, event):
        """
        Podwójne kliknięcie CHOWA kulę (bezramkowe okno nie ma krzyżyka).

        Celowo hide(), a nie close() — program ma dalej działać w tle.
        Z powrotem przywołasz kulę z menu ikony w zasobniku, tam też
        znajdziesz opcję rzeczywistego zamknięcia Jarvisa.
        """
        self.hide()
        logger.info("Kula ukryta podwójnym kliknięciem.")


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
        program po zamknięciu ostatniego okna — czyli ukrycie kuli przez menu
        zabiłoby całą aplikację, mimo że ikona w zasobniku dalej by tam była.
    """

    def __init__(self, app, orb, przy_zamknieciu=None):
        """
        app             — obiekt QApplication
        orb             — okienko z pulsującym kołem (HUD na pulpicie)
        przy_zamknieciu — opcjonalna funkcja sprzątająca, wołana przed wyjściem
                          (main.py przekazuje tu zatrzymanie wątku nasłuchu)
        """
        self._app = app
        self._orb = orb
        self._przy_zamknieciu = przy_zamknieciu

        # Bez tego ukrycie kuli ("Pokaż/Ukryj") zamknęłoby cały program.
        app.setQuitOnLastWindowClosed(False)

        ikona = QIcon(utworz_plik_ikony())

        # Menu MUSI zostać polem obiektu — patrz uwaga o referencjach powyżej.
        self._menu = QMenu()

        self._akcja_widocznosc = QAction("Ukryj kulę", self._menu)
        self._akcja_widocznosc.triggered.connect(self._przelacz_widocznosc)
        self._menu.addAction(self._akcja_widocznosc)

        # Kulę można ukryć także podwójnym kliknięciem, z pominięciem menu.
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

        # Lewym przyciskiem też wygodnie przełączać widoczność kuli.
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
        """Dopasowuje napis w menu do tego, czy kula jest aktualnie widoczna."""
        self._akcja_widocznosc.setText(
            "Ukryj kulę" if self._orb.isVisible() else "Pokaż kulę"
        )

    def _przelacz_widocznosc(self):
        """Pokazuje albo ukrywa kulę i aktualizuje napis w menu."""
        if self._orb.isVisible():
            self._orb.hide()
            logger.info("Kula ukryta.")
        else:
            self._orb.show()
            logger.info("Kula pokazana.")

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


# --- Test: `python gui.py` — cyklicznie przełącza stany co 2 sekundy ---
if __name__ == "__main__":
    from logging_setup import skonfiguruj_logowanie

    skonfiguruj_logowanie()

    app = QApplication(sys.argv)

    orb = JarvisOrb()

    # Ustawiamy okno w prawym dolnym rogu ekranu, nad zegarkiem.
    ekran = app.primaryScreen().availableGeometry()
    orb.move(ekran.right() - ROZMIAR_OKNA - 40, ekran.bottom() - ROZMIAR_OKNA - 40)

    orb.show()

    # Referencję do tray trzeba przechować — inaczej ikona zniknie po chwili.
    tray = TrayJarvisa(app, orb)

    KOLEJNOSC = ["idle", "listening", "processing", "error"]
    indeks = 0

    def nastepny_stan():
        """Przełącza na kolejny stan z listy i zapisuje go w dzienniku."""
        global indeks
        stan = KOLEJNOSC[indeks % len(KOLEJNOSC)]
        indeks += 1
        logger.info("[GUI] stan: %s", stan)
        orb.set_state(stan)

    # Uwaga: ten timer żyje w wątku GUI, więc to NIE jest test międzywątkowy —
    # sprawdza tylko, jak stany wyglądają.
    timer_demo = QTimer()
    timer_demo.timeout.connect(nastepny_stan)
    timer_demo.start(2000)

    nastepny_stan()

    logger.info("Przeciągnij koło myszą, żeby je przesunąć. "
                "Prawy klik w ikonę zasobnika otwiera menu.")
    sys.exit(app.exec())
