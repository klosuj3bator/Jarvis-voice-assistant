"""
main.py — punkt wejścia asystenta Jarvis.

Spina wszystkie moduły w jedną aplikację:

    gui                 <-  sygnały o stanie
    wake_word_listener  ->  router          ->  spotify_controller / app_launcher
    (uszy: mowa->tekst)     (mózg: co robić)    (ręce: wykonaj)


DLACZEGO NASŁUCH DZIAŁA W OSOBNYM WĄTKU
=======================================

Qt wymaga, żeby jego pętla zdarzeń (app.exec()) działała w wątku głównym —
to ona odbiera kliknięcia, odrysowuje okno i napędza animację. app.exec()
blokuje aż do zamknięcia programu.

Nasza pętla nasłuchu też blokuje: sluchaj_komendy() potrafi wisieć minutami,
czekając na "Hey Jarvis". Dwie blokujące pętle nie zmieszczą się w jednym wątku
— jedna zawsze zagłodziłaby drugą. Gdybyśmy nasłuchiwali w wątku głównym,
okno zamarłoby: żadnej animacji, brak reakcji na mysz, Windows po chwili
uznałby aplikację za zawieszoną.

Dlatego dzielimy pracę:
    wątek główny    -> Qt: okno i animacja
    wątek roboczy   -> mikrofon, Whisper, Claude, Spotify

Wątek roboczy nie dotyka okna bezpośrednio — woła orb.set_state(), które
zamienia wywołanie na sygnał Qt i bezpiecznie przekazuje je wątkowi GUI.
Cały mechanizm jest opisany na górze gui.py.

Wątek jest oznaczony jako daemon, czyli "nie wstrzymuj zamykania programu".
Bez tego zamknięcie okna zostawiłoby Pythona wiszącego w oczekiwaniu na wątek,
który właśnie blokuje się na mikrofonie.

Uruchomienie: python main.py
"""

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

    # akcja == "unknown" albo cokolwiek nieprzewidzianego.
    # Traktujemy to jak błąd, żeby koło błysnęło — inaczej nie wiedziałbyś,
    # czy Jarvis Cię nie zrozumiał, czy w ogóle nie usłyszał.
    return "Nie zrozumiałem komendy.", False


def petla_jarvisa(orb):
    """
    Główna pętla asystenta. Działa w wątku roboczym, nie w wątku GUI.

    orb — okienko z animacją; wołamy na nim wyłącznie set_state(),
          bo tylko ta metoda jest bezpieczna międzywątkowo

    W kółko: słuchaj -> zrozum -> wykonaj -> wróć do słuchania.
    """
    while True:
        # 1. USZY — blokuje aż do wykrycia wake worda, potem nagrywa i transkrybuje.
        #
        # Przekazujemy orb.set_state jako callback, więc to sam moduł nasłuchu
        # przełącza animację na "listening" w chwili, gdy zaczyna nagrywać,
        # i na "processing", gdy oddaje nagranie Whisperowi. Stąd stan koła
        # zgadza się z tym, co program faktycznie robi, co do sekundy.
        tekst = wake_word_listener.sluchaj_komendy(orb.set_state)

        # Cisza albo szum — nie ma sensu pytać modelu, wracamy do nasłuchu.
        if not tekst:
            orb.set_state("idle")
            continue

        # 2. MÓZG — zamienia zdanie na ustrukturyzowaną decyzję.
        # Koło jest już w stanie "processing", ustawionym przez nasłuch.
        decyzja = router.rozpoznaj_komende(tekst)
        print(f"[DECYZJA] {decyzja}")

        # 3. RĘCE — wykonuje decyzję.
        # Każdy błąd łapiemy tutaj, żeby jedna nieudana komenda nie zabiła
        # całego asystenta — Jarvis ma błysnąć, powiedzieć co poszło źle
        # i słuchać dalej. Wyjątek w wątku roboczym ubiłby cichaczem
        # tylko ten wątek, zostawiając okno działające, ale głuche.
        try:
            komunikat, sukces = wykonaj_akcje(decyzja)
        except Exception as e:
            komunikat, sukces = f"Coś poszło nie tak: {e}", False

        print(f"[JARVIS] {komunikat}")

        # Błysk czerwienią sam wraca do idle po chwili (obsługuje to gui.py),
        # więc tutaj ustawiamy idle tylko przy sukcesie.
        orb.set_state("idle" if sukces else "error")


def main():
    print("=" * 55)
    print("  JARVIS — asystent głosowy")
    print("  Powiedz 'Hey Jarvis', poczekaj na sygnał, potem komendę.")
    print("  Ctrl+C w konsoli albo podwójne kliknięcie w koło kończy program.")
    print("=" * 55)

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
    # modelu Whispera odbywało się już przy widocznym, animowanym kole
    # — inaczej przez pierwszą minutę wyglądałoby to jak zawieszony program.
    watek = threading.Thread(
        target=petla_jarvisa,
        args=(orb,),
        name="watek-nasluchu",
        daemon=True,
    )
    watek.start()

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

    # Zwalniamy mikrofon jawnie. Wątek roboczy jest daemonem, więc zniknie sam,
    # ale strumień audio warto zamknąć porządnie.
    wake_word_listener.zamknij()

    print("\nDo zobaczenia!")
    return kod


if __name__ == "__main__":
    sys.exit(main())
