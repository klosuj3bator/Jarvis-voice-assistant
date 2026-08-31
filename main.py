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
import chat
import gui
import router
import spotify_controller
import tts
import wake_word_listener
from logging_setup import PLIK_LOGU, skonfiguruj_logowanie

logger = logging.getLogger(__name__)

# Ile sekund ciszy kończy rozmowę i odsyła Jarvisa z powrotem do czuwania.
# Za mało — rozmowa urywa się, gdy zastanawiasz się nad pytaniem.
# Za dużo — mikrofon zostaje otwarty na długo po tym, jak skończyłeś.
LIMIT_CISZY_ROZMOWY_S = 8

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

    if akcja == "play_album":
        # Osobna akcja, bo Spotify gra album inaczej niż pojedynczy utwór —
        # przez context_uri zamiast listy uris. Szczegóły w odtworz_album().
        return spotify_controller.zagraj_album(
            decyzja.get("album"),
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


def powiedz(orb, komunikat):
    """
    Wypowiada tekst na głos, trzymając kulę w stanie "speaking".

    mow() jest blokujące, więc przez cały czas mówienia mikrofon nie nasłuchuje
    — to nie przypadek, tylko warunek działania całości. Gdyby nasłuch trwał
    w tle, Jarvis usłyszałby własny głos i albo wykryłby w nim wake word,
    albo nagrał samego siebie jako kolejną wypowiedź.
    """
    if not komunikat:
        return

    orb.set_state("speaking")
    tts.mow(komunikat)


def rozmowa(orb, pierwszy_tekst):
    """
    PĘTLA WEWNĘTRZNA — obsługuje jedną sesję rozmowy.

    Zaczyna się od tekstu wypowiedzianego zaraz po "Hey Jarvis" i toczy się
    dalej BEZ wake worda: po każdej odpowiedzi Jarvis sam nasłuchuje przez
    chwilę, czy chcesz coś dodać. Kończy się, gdy przez LIMIT_CISZY_ROZMOWY_S
    nikt się nie odezwie.

    Historia rozmowy żyje TYLKO tutaj, jako zmienna lokalna. To celowe:
    wyjście z tej funkcji jest równoznaczne z zapomnieniem kontekstu,
    więc nowa rozmowa nigdy nie odziedziczy strzępów poprzedniej.
    """
    historia_rozmowy = []
    tekst = pierwszy_tekst

    while tekst is not None and not wake_word_listener.czy_zatrzymano():
        # MÓZG — czy to komenda do wykonania, czy zwykła rozmowa?
        decyzja = router.rozpoznaj_komende(tekst)
        logger.info("[DECYZJA] %s", decyzja)
        akcja = decyzja.get("action")

        if akcja == "chat":
            # Rozmowa: do Sonneta idzie ORYGINALNY tekst z transkrypcji razem
            # z historią. Router celowo nie przepisuje treści — jego zadaniem
            # było tylko stwierdzić, że to rozmowa, a nie polecenie.
            #
            # Wersja strumieniowa: Jarvis zaczyna mówić pierwsze zdanie, gdy
            # model dopiero układa drugie. Generator nie może zwrócić historii
            # (w chwili oddania pierwszego zdania reszty jeszcze nie ma),
            # więc składamy ją tutaj — z tego, co FAKTYCZNIE zostało wypowiedziane.
            generator = chat.odpowiedz_rozmowa_stream(tekst, historia_rozmowy)

            # Kula przełącza się na "speaking" dopiero przy pierwszym dźwięku,
            # a nie już teraz — inaczej świeciłaby "mówię" przez te sekundę
            # czy dwie, w których model jeszcze myśli, a z głośników nic nie leci.
            pelna_odpowiedz = tts.mow_strumieniowo(
                generator, na_start=lambda: orb.set_state("speaking")
            )

            if pelna_odpowiedz:
                historia_rozmowy = historia_rozmowy + [
                    {"role": "user", "content": tekst},
                    {"role": "assistant", "content": pelna_odpowiedz},
                ]
                logger.info("[JARVIS] %s", pelna_odpowiedz)
            else:
                # Nic nie padło (błąd API albo syntezy) — nie zapisujemy do historii
                # pytania bez odpowiedzi, bo zaburzyłoby to przeplot ról.
                logger.warning("Rozmowa nie zwróciła żadnej treści.")

            orb.set_state("idle")

        elif akcja == "unknown":
            logger.info("[JARVIS] Nie zrozumiałem polecenia.")
            powiedz(orb, "Nie zrozumiałem polecenia.")
            orb.set_state("idle")

        else:
            # RĘCE — konkretna akcja: muzyka albo aplikacja.
            # Błąd łapiemy tutaj, żeby jedna nieudana komenda nie zabiła
            # całej sesji — Jarvis ma powiedzieć, co poszło źle, i słuchać dalej.
            try:
                komunikat, sukces = wykonaj_akcje(decyzja)
            except Exception:
                logger.exception("Błąd podczas wykonywania komendy")
                komunikat, sukces = "Coś poszło nie tak przy wykonywaniu komendy.", False

            logger.info("[JARVIS] %s", komunikat)
            powiedz(orb, komunikat)

            # Czerwony błysk sam wraca do idle po chwili (obsługuje to gui.py).
            orb.set_state("idle" if sukces else "error")

        if wake_word_listener.czy_zatrzymano():
            break

        # Nasłuch bez wake worda. None znaczy "cisza — koniec rozmowy".
        tekst = wake_word_listener.sluchaj_bez_wake_worda(
            orb.set_state, limit_ciszy_s=LIMIT_CISZY_ROZMOWY_S
        )


def petla_jarvisa(orb):
    """
    PĘTLA ZEWNĘTRZNA — czeka na "Hey Jarvis" i oddaje sterowanie rozmowie.

    orb — okienko z animacją; wołamy na nim wyłącznie set_state(),
          bo tylko ta metoda jest bezpieczna międzywątkowo

    Dwa poziomy zamiast jednego, bo Jarvis ma dwa wyraźnie różne tryby:

        CZUWANIE — mikrofon słucha wyłącznie wake worda. Tanio (openWakeWord),
                   lokalnie, bez wysyłania czegokolwiek do sieci.
        ROZMOWA  — po wykryciu wake worda; kolejne wypowiedzi już go nie
                   wymagają, bo powtarzanie "Hey Jarvis" przed każdym zdaniem
                   zabijałoby naturalność rozmowy.

    Działa w wątku roboczym, nie w wątku GUI.
    """
    # Cały korpus w try/except, bo w wątku roboczym nieobsłużony wyjątek
    # zabiłby ten wątek po cichu: kula dalej by pulsowała, ikona wisiałaby
    # w zasobniku, a Jarvis po prostu przestałby słuchać, bez śladu na ekranie.
    try:
        while not wake_word_listener.czy_zatrzymano():
            logger.info("=== TRYB: CZUWANIE (czekam na 'Hey Jarvis') ===")

            # Blokuje aż do wykrycia wake worda, potem nagrywa i transkrybuje.
            # Przekazujemy orb.set_state jako callback, więc to sam moduł nasłuchu
            # przełącza animację na "listening" i "processing" — stan kuli zgadza
            # się z tym, co program faktycznie robi, co do sekundy.
            tekst = wake_word_listener.sluchaj_komendy(orb.set_state)

            if wake_word_listener.czy_zatrzymano():
                break

            if not tekst:
                orb.set_state("idle")
                continue

            logger.info("=== TRYB: ROZMOWA (wake word już niepotrzebny) ===")
            rozmowa(orb, tekst)
            logger.info("=== TRYB: KONIEC ROZMOWY — wracam do czuwania ===")

            orb.set_state("idle")

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
