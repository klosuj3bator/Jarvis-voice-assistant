"""
main.py — punkt wejścia asystenta Jarvis.

Spina wszystkie moduły w jedną aplikację działającą w tle:

    gui (kula + zasobnik)  <-  sygnały o stanie
    wake_word_listener     ->  router          ->  spotify_controller / app_launcher
    (uszy: mowa->tekst)        (mózg: co robić)    (ręce: wykonaj)


DLACZEGO NASŁUCH DZIAŁA W OSOBNYM WĄTKU
=======================================

Qt wymaga, żeby jego pętla zdarzeń (app.exec()) działała w wątku głównym —
to ona odbiera kliknięcia, obsługuje menu zasobnika i napędza animację.
app.exec() blokuje aż do zamknięcia programu.

Nasza pętla nasłuchu też blokuje: sluchaj_komendy() potrafi wisieć minutami,
czekając na "Hey Jarvis". Dwie blokujące pętle nie zmieszczą się w jednym wątku
— jedna zawsze zagłodziłaby drugą. Gdybyśmy nasłuchiwali w wątku głównym,
kula zamarłaby, menu zasobnika przestałoby się otwierać, a Windows po chwili
uznałby aplikację za zawieszoną.

Dlatego dzielimy pracę:
    wątek główny    -> Qt: kula, ikona w zasobniku, menu
    wątek roboczy   -> mikrofon, Whisper, Claude, Spotify

Wątek roboczy nie dotyka okna bezpośrednio — woła orb.set_state(), które
zamienia wywołanie na sygnał Qt i bezpiecznie przekazuje je wątkowi GUI.
Cały mechanizm jest opisany na górze gui.py.


JAK KOŃCZY SIĘ PROGRAM
======================

Wątek nasłuchu blokuje się na mikrofonie, więc nie wystarczy "zamknąć okno" —
trzeba mu powiedzieć, żeby przestał, i poczekać, aż faktycznie skończy.
Kolejność w posprzataj() ma znaczenie i wygląda tak:

    1. zatrzymaj()  — ustawia przełącznik, który pętla nasłuchu sprawdza co 80 ms
    2. join()       — czekamy, aż wątek naprawdę wyjdzie z pętli
    3. zamknij()    — dopiero teraz zwalniamy mikrofon

Zamknięcie strumienia audio w punkcie 3 przed zakończeniem wątku z punktu 2
potrafi wysypać program: jeden wątek zamykałby urządzenie, z którego drugi
w tej samej chwili czyta.

Uruchomienie z konsolą (widać dziennik na bieżąco):  python main.py
Uruchomienie w tle, bez konsoli:                     pythonw main.py
                                                     albo uruchom_jarvis.vbs
"""

import logging
import signal
import sys
import threading

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

import app_launcher
import gui
import router
import spotify_controller
import wake_word_listener
from logging_setup import PLIK_LOGU, skonfiguruj_logowanie

logger = logging.getLogger(__name__)

# Ile sekund czekamy na grzeczne zakończenie wątku nasłuchu, zanim odpuścimy.
# Wątek sprawdza przełącznik co ~80 ms, więc w praktyce kończy się od razu;
# limit chroni tylko przed zawieszeniem się na sterowniku audio.
LIMIT_ZAMYKANIA_S = 5


def wykonaj_akcje(decyzja):
    """
    Wykonuje decyzję zwróconą przez router.

    decyzja — słownik z kluczem "action" i parametrami zależnymi od akcji

    Zwraca: (komunikat dla użytkownika, czy się udało).
    Flaga sukcesu decyduje, czy koło wróci do idle, czy błyśnie na czerwono.
    """
    akcja = decyzja.get("action")

    # Obie funkcje wykonawcze zwracają (komunikat, sukces), więc przekazujemy
    # ich wynik wprost. Dzięki temu koło błyśnie także wtedy, gdy akcja
    # została rozpoznana, ale się nie udała — np. nie znaleziono utworu
    # albo aplikacji nie ma w konfiguracji.
    if akcja == "play_song":
        # .get() zamiast [] — gdyby model pominął pole, dostaniemy None
        # zamiast wyjątku, a zagraj_piosenke() poradzi sobie z brakiem wykonawcy.
        return spotify_controller.zagraj_piosenke(
            decyzja.get("song"),
            decyzja.get("artist"),
        )

    if akcja == "open_app":
        return app_launcher.otworz_aplikacje(decyzja.get("app_name"))

    if akcja == "close_app":
        # Zamykanie ma własne zabezpieczenia po stronie app_launcher:
        # listę procesów chronionych, ograniczenie do procesów bieżącego
        # użytkownika i wyższy próg dopasowania niż przy otwieraniu.
        # main.py nie dokłada tu nic — cała ocena ryzyka jest w jednym miejscu.
        return app_launcher.zamknij_aplikacje(decyzja.get("app_name"))

    # akcja == "unknown" albo cokolwiek nieprzewidzianego.
    # Traktujemy to jak błąd, żeby koło błysnęło — inaczej nie wiedziałbyś,
    # czy Jarvis Cię nie zrozumiał, czy w ogóle nie usłyszał.
    return "Nie zrozumiałem komendy.", False


def petla_jarvisa(orb):
    """
    Główna pętla asystenta. Działa w wątku roboczym, nie w wątku GUI.

    orb — okienko z animacją; wołamy na nim wyłącznie set_state(),
          bo tylko ta metoda jest bezpieczna międzywątkowo

    W kółko: słuchaj -> zrozum -> wykonaj -> wróć do słuchania,
    aż ktoś poprosi o zatrzymanie przez menu w zasobniku.
    """
    # Cały korpus w try/except, bo w wątku roboczym nieobsłużony wyjątek
    # zabiłby ten wątek po cichu: kula dalej by pulsowała, ikona wisiałaby
    # w zasobniku, a Jarvis po prostu przestałby słuchać, bez śladu na ekranie.
    # Dzięki temu blokowi trafi przynajmniej do dziennika.
    try:
        while not wake_word_listener.czy_zatrzymano():
            # 1. USZY — blokuje aż do wykrycia wake worda, potem nagrywa i transkrybuje.
            #
            # Przekazujemy orb.set_state jako callback, więc to sam moduł nasłuchu
            # przełącza animację na "listening" w chwili, gdy zaczyna nagrywać,
            # i na "processing", gdy oddaje nagranie Whisperowi. Stąd stan koła
            # zgadza się z tym, co program faktycznie robi, co do sekundy.
            tekst = wake_word_listener.sluchaj_komendy(orb.set_state)

            # Pusty tekst znaczy albo ciszę, albo że program jest właśnie zamykany.
            if wake_word_listener.czy_zatrzymano():
                break

            if not tekst:
                orb.set_state("idle")
                continue

            # 2. MÓZG — zamienia zdanie na ustrukturyzowaną decyzję.
            # Koło jest już w stanie "processing", ustawionym przez nasłuch.
            decyzja = router.rozpoznaj_komende(tekst)
            logger.info("[DECYZJA] %s", decyzja)

            # 3. RĘCE — wykonuje decyzję.
            # Każdy błąd łapiemy tutaj, żeby jedna nieudana komenda nie zabiła
            # całego asystenta — Jarvis ma błysnąć, zapisać co poszło źle
            # i słuchać dalej.
            try:
                komunikat, sukces = wykonaj_akcje(decyzja)
            except Exception:
                logger.exception("Błąd podczas wykonywania komendy")
                komunikat, sukces = "Coś poszło nie tak przy wykonywaniu komendy.", False

            logger.info("[JARVIS] %s", komunikat)

            # Błysk czerwienią sam wraca do idle po chwili (obsługuje to gui.py),
            # więc tutaj ustawiamy idle tylko przy sukcesie.
            orb.set_state("idle" if sukces else "error")

    except Exception:
        logger.exception("Pętla nasłuchu zakończyła się nieoczekiwanym błędem")

    logger.info("Pętla nasłuchu zakończona.")


def main():
    skonfiguruj_logowanie()

    logger.info("=" * 50)
    logger.info("JARVIS startuje. Dziennik: %s", PLIK_LOGU)
    logger.info("Powiedz 'Hey Jarvis', poczekaj na sygnał, potem komendę.")
    logger.info("Zamknięcie: prawy klik w ikonę zasobnika -> Zamknij Jarvisa.")
    logger.info("=" * 50)

    app = QApplication(sys.argv)

    orb = gui.JarvisOrb()

    # Prawy dolny róg ekranu, z marginesem nad paskiem zadań.
    ekran = app.primaryScreen().availableGeometry()
    orb.move(
        ekran.right() - gui.ROZMIAR_OKNA - 40,
        ekran.bottom() - gui.ROZMIAR_OKNA - 40,
    )
    orb.show()

    # Wątek roboczy startuje dopiero po pokazaniu okna, żeby długie ładowanie
    # modelu Whispera odbywało się już przy widocznej, animowanej kuli
    # — inaczej przez pierwszą minutę wyglądałoby to jak zawieszony program.
    watek = threading.Thread(
        target=petla_jarvisa,
        args=(orb,),
        name="watek-nasluchu",
        daemon=True,
    )
    watek.start()

    # Flaga, żeby sprzątanie wykonało się dokładnie raz — posprzataj() może
    # zostać zawołane i z menu zasobnika, i po wyjściu z pętli zdarzeń.
    posprzatano = threading.Event()

    def posprzataj():
        """Kończy wątek nasłuchu i zwalnia mikrofon. Kolejność jest istotna."""
        if posprzatano.is_set():
            return
        posprzatano.set()

        logger.info("Sprzątanie: zatrzymuję wątek nasłuchu...")
        wake_word_listener.zatrzymaj()

        watek.join(timeout=LIMIT_ZAMYKANIA_S)
        if watek.is_alive():
            # Wątek jest daemonem, więc i tak zniknie razem z procesem —
            # ale warto wiedzieć z dziennika, że nie wyszedł grzecznie.
            logger.warning("Wątek nasłuchu nie zakończył się w %s s.", LIMIT_ZAMYKANIA_S)
        else:
            logger.info("Wątek nasłuchu zakończony poprawnie.")

        wake_word_listener.zamknij()

    # Ikona w zasobniku. Referencję trzeba przechować w zmiennej, inaczej
    # garbage collector posprząta obiekt i ikona zniknie po ułamku sekundy.
    tray = gui.TrayJarvisa(app, orb, przy_zamknieciu=posprzataj)

    # Ctrl+C w aplikacji Qt: pętla zdarzeń Qt siedzi w kodzie C++ i nie oddaje
    # sterowania Pythonowi, więc domyślnie nie zauważyłby wciśnięcia Ctrl+C.
    # Dwie rzeczy to naprawiają:
    #   1. własna obsługa sygnału SIGINT, która woła app.quit(),
    #   2. timer, który co 200 ms na moment wraca do Pythona i daje mu szansę
    #      tę obsługę wykonać. Timer celowo nic nie robi — liczy się samo
    #      to, że przerywa pobyt w kodzie Qt.
    signal.signal(signal.SIGINT, lambda numer, ramka: app.quit())
    budzik = QTimer()
    budzik.timeout.connect(lambda: None)
    budzik.start(200)

    kod = app.exec()

    # Zabezpieczenie na wypadek zamknięcia inną drogą niż menu zasobnika
    # (Ctrl+C, wylogowanie użytkownika). posprzataj() jest idempotentne.
    posprzataj()

    logger.info("Jarvis zakończył pracę. Do zobaczenia!")
    return kod


if __name__ == "__main__":
    sys.exit(main())
