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

import math
import sys

from PySide6.QtCore import QRectF, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QApplication, QWidget

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
        """Podwójne kliknięcie zamyka okienko — bezramkowe okno nie ma krzyżyka."""
        self.close()


# --- Test: `python gui.py` — cyklicznie przełącza stany co 2 sekundy ---
if __name__ == "__main__":
    app = QApplication(sys.argv)

    orb = JarvisOrb()

    # Ustawiamy okno w prawym dolnym rogu ekranu, nad zegarkiem.
    ekran = app.primaryScreen().availableGeometry()
    orb.move(ekran.right() - ROZMIAR_OKNA - 40, ekran.bottom() - ROZMIAR_OKNA - 40)

    orb.show()

    KOLEJNOSC = ["idle", "listening", "processing", "error"]
    indeks = 0

    def nastepny_stan():
        """Przełącza na kolejny stan z listy i wypisuje go w konsoli."""
        global indeks
        stan = KOLEJNOSC[indeks % len(KOLEJNOSC)]
        indeks += 1
        print(f"[GUI] stan: {stan}")
        orb.set_state(stan)

    # Uwaga: ten timer żyje w wątku GUI, więc to NIE jest test międzywątkowy —
    # sprawdza tylko, jak stany wyglądają. Prawdziwe wywołania z wątku
    # nasłuchującego podepniemy w kolejnym kroku i zadziałają tak samo,
    # bo set_state() i tak przechodzi przez sygnał.
    timer_demo = QTimer()
    timer_demo.timeout.connect(nastepny_stan)
    timer_demo.start(2000)

    nastepny_stan()

    print("Przeciągnij koło myszą, żeby je przesunąć. Podwójne kliknięcie zamyka.")
    sys.exit(app.exec())
