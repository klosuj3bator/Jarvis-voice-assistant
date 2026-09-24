"""
main.py — punkt wejścia asystenta Jarvis.

Spina wszystkie moduły w jedną aplikację działającą w tle:

    gui (kula + zasobnik)  <-  sygnały o stanie
    wake_word_listener     ->  agent           ->  narzędzia
    (uszy: mowa->tekst)        (mózg: rozmowa      (Spotify, aplikacje,
                                + narzędzia)        wyszukiwarka, pamięć)
    tts (usta: tekst->mowa)    pamiec (co pamięta między rozmowami)


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
import re
import signal
import sys
import threading

from PySide6.QtWidgets import QApplication

import gui
from logging_setup import PLIK_LOGU, skonfiguruj_logowanie

logger = logging.getLogger(__name__)


# --- Ciężkie moduły ładujemy LENIWIE ---------------------------------------
#
# DLACZEGO, skoro zwykłe importy na górze pliku są czytelniejsze:
#
# Zmierzyłem czas importów na zimno (czyli tak, jak wygląda pierwsze
# uruchomienie po restarcie komputera, gdy nic nie siedzi jeszcze w cache dysku):
#
#     import main .................... 49 s
#       w tym openwakeword ........... 16 s   (z czego scikit-learn 13 s!)
#       w tym anthropic .............. kilka s
#       reszta: faster-whisper, av, psutil, spotipy, pywin32...
#
# scikit-learn to skrajny przykład marnotrawstwa: openwakeword ciągnie go
# przez moduł do TRENOWANIA własnych słów-kluczy, którego nigdy nie używamy.
#
# Gorsze od samego czekania było to, że te 49 sekund mijało, ZANIM w ogóle
# powstało okno Qt — więc przez ten czas na ekranie nie było nic. Stąd wrażenie,
# że "kula się nie pokazuje": ona jeszcze nie istniała.
#
# Teraz na górze pliku zostaje tylko Qt i gui. Kula pojawia się od razu,
# a resztę dociąga wątek roboczy w tle, już przy widocznej, pulsującej kuli.
# Łączny czas startu się nie skrócił — ale przestał być ślepy.

agent = None
pamiec = None
system_control = None
tts = None
wake_word_listener = None


def zaladuj_moduly():
    """
    Importuje ciężkie moduły. Wołane z wątku roboczego, nie z głównego.

    Zwykły `import` w Pythonie jest tani przy drugim wywołaniu (moduł ląduje
    w sys.modules), więc nie ma znaczenia, że robimy to w środku funkcji —
    koszt płacimy raz, a zyskujemy kontrolę nad tym, KIEDY go zapłacimy.
    """
    global agent, pamiec, system_control, tts, wake_word_listener

    if wake_word_listener is not None:
        return

    import time as _time
    poczatek = _time.time()
    logger.info("Ładuję moduły w tle...")

    # agent ciagnie za soba spotify_controller, app_launcher i przegladarke,
    # wiec nie importujemy ich tu osobno. system_control tez przez niego
    # przychodzi, ale main.py wola go wprost (potwierdzanie restartu),
    # wiec bierzemy do niego wlasna referencje.
    import agent as _agent
    import pamiec as _pamiec
    import system_control as _system_control
    import tts as _tts
    import wake_word_listener as _wake_word_listener

    agent = _agent
    pamiec = _pamiec
    system_control = _system_control
    tts = _tts
    wake_word_listener = _wake_word_listener

    logger.info("Moduły załadowane w %.1f s.", _time.time() - poczatek)

# Ile sekund ciszy kończy rozmowę i odsyła Jarvisa z powrotem do czuwania.
#
# Wcześniej było tu 8 sekund i cisza była GŁÓWNYM sposobem kończenia rozmowy.
# To psuło całą rzecz: wystarczyło zastanowić się chwilę nad pytaniem albo
# odejść po kubek, żeby Jarvis się rozłączył i trzeba było znowu wołać go
# po imieniu.
#
# Teraz rozmowę kończy się SŁOWEM ("dzięki, to tyle", "pa", "wystarczy"),
# a ten limit jest już tylko bezpiecznikiem na wypadek, gdybyś wyszedł
# z pokoju bez pożegnania. Dwie minuty to kompromis: wystarczająco długo,
# żeby nie przerywać naturalnych przerw w rozmowie, i wystarczająco krótko,
# żeby mikrofon nie został otwarty na resztę wieczoru.
LIMIT_CISZY_ROZMOWY_S = 120

# Ile razy z rzędu możemy usłyszeć dźwięk bez rozpoznawalnych słów, zanim
# uznamy, że to nie rozmowa, tylko hałas w tle (telewizor, muzyka, rozmowa
# w drugim pokoju). Bez tego licznika Jarvis przy włączonym telewizorze
# potrafiłby wisieć w trybie rozmowy przez całe dwie minuty.
MAX_PUSTYCH_Z_RZEDU = 4

# Ile sekund czekamy na grzeczne zakończenie wątku nasłuchu, zanim odpuścimy.
# Wątek sprawdza przełącznik co ~80 ms, więc w praktyce kończy się od razu;
# limit chroni tylko przed zawieszeniem się na sterowniku audio.
LIMIT_ZAMYKANIA_S = 5


# --- Potwierdzanie groźnych poleceń systemowych ------------------------------
#
# Restartu i wyłączenia komputera nie da się cofnąć — a przesłyszeć się jest
# łatwo (telewizor w tle, ktoś mówi w drugim pokoju). Dlatego Jarvis pyta
# "czy na pewno?" i czeka na wyraźne "tak".
#
# Krótki limit, bo to pytanie zamknięte: albo odpowiadasz od razu, albo
# widocznie nie do Ciebie było kierowane.
LIMIT_POTWIERDZENIA_S = 8

# Brak odpowiedzi, cisza i hałas znaczą "nie". Tak samo każde słowo odmowy —
# sprawdzamy je PIERWSZE, żeby "nie, nie rób tego" nie przeszło jako zgoda
# tylko dlatego, że padło gdzieś słowo "dobra".
SLOWA_ODMOWY = {
    "nie", "anuluj", "anuluję", "anuluje", "przerwij", "stop", "zostaw",
    "czekaj", "rezygnuję", "rezygnuje", "odwołaj", "odwolaj", "no",
}
SLOWA_ZGODY = {
    "tak", "jasne", "pewnie", "potwierdzam", "zgoda", "zgadza", "dawaj",
    "śmiało", "smialo", "rób", "rob", "zrób", "zrob", "wykonaj", "oczywiście",
    "oczywiscie", "ok", "okej", "okay", "yes", "dobra", "dobrze",
}

# Co Jarvis mówi tuż PRZED wykonaniem polecenia. Kolejność jest ważna:
# po uśpieniu albo wyłączeniu nie byłoby już czym mówić.
ZAPOWIEDZI_SYSTEMOWE = {
    "zablokuj": "Blokuję ekran.",
    "uspij": "Dobranoc, usypiam komputer.",
    "restart": "Restartuję komputer.",
    "wylacz": "Wyłączam komputer.",
}

ODMOWA = "Dobrze, zostawiam wszystko tak, jak jest."


def _to_zgoda(tekst):
    """
    Czy w odpowiedzi padło wyraźne "tak"?

    Porównujemy CAŁE słowa, a nie fragmenty — inaczej "nie" złapałoby się
    w "niebo", a "ok" w "okno". Cisza (None) i hałas ("") to odmowa:
    brak potwierdzenia nigdy nie może znaczyć zgody.
    """
    if not tekst:
        return False

    slowa = set(re.findall(r"[a-ząćęłńóśżź]+", tekst.lower()))
    if slowa & SLOWA_ODMOWY:
        return False
    return bool(slowa & SLOWA_ZGODY)


def _wykonaj_polecenie_systemowe(orb, polecenie):
    """
    Wykonuje polecenie, które agent tylko ZGŁOSIŁ podczas rozmowy: blokadę
    ekranu, uśpienie, restart albo wyłączenie komputera.

    Dlaczego tutaj, a nie w samym narzędziu agenta: narzędzia wykonują się
    w trakcie mówienia (szczegóły w system_control.py). Uśpienie ucięłoby
    zdanie w połowie, a pytanie o potwierdzenie nałożyłoby się na to, co
    akurat leci z głośników — i mikrofon nagrałby oba naraz.

    Zwraca: (wiadomości do dopisania do historii, czy kończyć rozmowę).
    """
    potwierdzenie = None

    if polecenie in system_control.POLECENIA_DO_POTWIERDZENIA:
        # Samo pytanie "czy na pewno?" Jarvis zadał już w swojej odpowiedzi —
        # tutaj tylko słuchamy, co odpowiesz.
        logger.info("[SYSTEM] czekam na potwierdzenie polecenia: %s", polecenie)
        potwierdzenie = wake_word_listener.sluchaj_bez_wake_worda(
            orb.set_state, limit_ciszy_s=LIMIT_POTWIERDZENIA_S
        )
        logger.info("[SYSTEM] usłyszałem: %r", potwierdzenie)

        if not _to_zgoda(potwierdzenie):
            logger.info("[SYSTEM] brak zgody — odpuszczam %s.", polecenie)
            tts.mow(ODMOWA)
            return [
                {"role": "user", "content": potwierdzenie or "(brak odpowiedzi)"},
                {"role": "assistant", "content": ODMOWA},
            ], False

    zapowiedz = ZAPOWIEDZI_SYSTEMOWE.get(polecenie, "Robi się.")
    tts.mow(zapowiedz)

    komunikat, sukces = system_control.wykonaj(polecenie)
    logger.info("[SYSTEM] %s -> %s", polecenie, komunikat)
    if not sukces:
        tts.mow(komunikat)

    dopisz = []
    if potwierdzenie is not None:
        dopisz.append({"role": "user", "content": potwierdzenie})
    dopisz.append({"role": "assistant", "content": zapowiedz if sukces else komunikat})

    # Po uśpieniu, restarcie i wyłączeniu nie ma już z kim rozmawiać.
    # Po zablokowaniu ekranu Jarvis może słuchać dalej.
    return dopisz, sukces and polecenie != "zablokuj"


# --- Tylko jedna kopia Jarvisa -----------------------------------------------
#
# Dwa uruchomienia naraz to podwójny Whisper, dwa HUD-y i dwa procesy
# walczące o ten sam mikrofon. Łatwo o to przez przypadek: chowasz HUD
# klawiszem ESC, zapominasz, że Jarvis działa w tle, i klikasz ponownie.
#
# Rozwiązanie: pierwsza kopia wystawia "skrzynkę na listy" (QLocalServer —
# nazwany kanał komunikacji między programami na tym samym komputerze).
# Każda kolejna kopia najpierw próbuje się do niej dobić. Jeśli się uda,
# wie, że Jarvis już działa: wysyła prośbę "pokaż się" i sama się zamyka.

NAZWA_KANALU = "jarvis-asystent-glosowy"


def _powiadom_dzialajaca_kopie():
    """
    Sprawdza, czy Jarvis już działa, i jeśli tak — prosi go o pokazanie okna.

    Zwraca: True, gdy inna kopia działa (czyli ta ma się zamknąć).
    """
    from PySide6.QtNetwork import QLocalSocket

    gniazdo = QLocalSocket()
    gniazdo.connectToServer(NAZWA_KANALU)
    if not gniazdo.waitForConnected(300):
        return False

    gniazdo.write(b"pokaz")
    gniazdo.flush()
    gniazdo.waitForBytesWritten(300)
    gniazdo.disconnectFromServer()
    return True


def _nasluchuj_kolejnych_kopii(orb):
    """
    Wystawia kanał, przez który kolejne uruchomienia proszą o pokazanie HUD-a.

    Zwraca: obiekt serwera — trzeba go przechować, żeby nie został posprzątany.
    """
    from PySide6.QtNetwork import QLocalServer

    serwer = QLocalServer()

    def nowe_polaczenie():
        polaczenie = serwer.nextPendingConnection()
        if polaczenie is not None:
            polaczenie.close()
        logger.info("Kolejne uruchomienie poprosiło o pokazanie HUD-a.")
        orb.pokaz()

    serwer.newConnection.connect(nowe_polaczenie)

    if not serwer.listen(NAZWA_KANALU):
        # Nie jest to powód, żeby nie działać — tracimy tylko ochronę
        # przed drugą kopią. Zapisujemy, żeby było wiadomo dlaczego.
        logger.warning("Nie udało się wystawić kanału dla kolejnych kopii: %s",
                       serwer.errorString())

    return serwer


class _BudzikCtrlC:
    """
    Pozwala przerwać Jarvisa klawiszami Ctrl+C w terminalu, nie budząc
    Pythona w wątku GUI, dopóki nikt ich nie wciśnie (opis w main()).
    """

    def __init__(self, app):
        import socket

        from PySide6.QtCore import QSocketNotifier

        # Para połączonych gniazd: Python pisze do jednego, Qt słucha drugiego.
        # Na Windowsie set_wakeup_fd przyjmuje wyłącznie gniazdo sieciowe.
        self._odbior, self._nadawanie = socket.socketpair()
        self._odbior.setblocking(False)
        self._nadawanie.setblocking(False)
        signal.set_wakeup_fd(self._nadawanie.fileno())
        signal.signal(signal.SIGINT, lambda numer, ramka: app.quit())

        self._notyfikator = QSocketNotifier(self._odbior.fileno(), QSocketNotifier.Type.Read)
        self._notyfikator.activated.connect(self._odbierz)

    def _odbierz(self):
        # Sam powrót do Pythona wystarczy, żeby wykonała się obsługa SIGINT.
        # Bajt trzeba zabrać z gniazda, inaczej Qt budziłby nas w kółko.
        try:
            self._odbior.recv(64)
        except OSError:
            pass

    def zamknij(self):
        self._notyfikator.setEnabled(False)
        signal.set_wakeup_fd(-1)
        self._odbior.close()
        self._nadawanie.close()


def rozmowa(orb, pierwszy_tekst):
    """
    PĘTLA WEWNĘTRZNA — obsługuje jedną sesję rozmowy.

    Zaczyna się od tekstu wypowiedzianego zaraz po "Hey Jarvis" i toczy się
    dalej BEZ wake worda — tak długo, jak rozmawiasz.

    KAŻDA wypowiedź — i pogawędka, i "puść Nevermind" — idzie tą samą drogą,
    do agenta. Nie ma już klasyfikowania na komendy i rozmowę, bo to właśnie
    ono psuło ciągłość: komendy nigdy nie trafiały do historii, więc pytanie
    "a kto to nagrał?" zadane po włączeniu albumu trafiało w próżnię.

    Rozmowa NIE zaczyna się od zera — na starcie wczytujemy końcówkę
    poprzedniej z pamięci na dysku. Dzięki temu można wrócić po godzinie
    (albo po restarcie komputera) i podjąć wątek.


    JAK ROZMOWA SIĘ KOŃCZY
    ======================

    Trzy drogi, w kolejności od najbardziej naturalnej:

      1. POWIESZ, ŻE KONIEC — "dzięki, to tyle", "pa", "wystarczy", "śpij".
         Decyduje o tym sam agent, przez narzędzie zakoncz_rozmowe. Celowo
         nie ma tu listy słów kluczowych: człowiek żegna się na sto sposobów,
         a model rozumie intencję zamiast dopasowywać wzorce.

      2. DŁUGA CISZA — bezpiecznik na wypadek wyjścia z pokoju.

      3. HAŁAS BEZ SŁÓW — gdy kilka razy z rzędu coś słychać, ale nie są to
         słowa (telewizor, muzyka), uznajemy, że nikt do nas nie mówi.
    """
    historia_rozmowy = pamiec.poprzednia_rozmowa()
    if historia_rozmowy:
        logger.info("Wznawiam poprzednią rozmowę (%d wiadomości z pamięci).",
                    len(historia_rozmowy))

    tekst = pierwszy_tekst
    pustych_z_rzedu = 0

    while tekst is not None and not wake_word_listener.czy_zatrzymano():
        # Pusty tekst znaczy "coś było słychać, ale bez słów". Nie odpowiadamy
        # na hałas — po prostu słuchamy dalej, jakby nic się nie stało.
        if not tekst.strip():
            pustych_z_rzedu += 1
            if pustych_z_rzedu >= MAX_PUSTYCH_Z_RZEDU:
                logger.info("Sam hałas %d razy z rzędu — kończę rozmowę.",
                            pustych_z_rzedu)
                break

            tekst = wake_word_listener.sluchaj_bez_wake_worda(
                orb.set_state, limit_ciszy_s=LIMIT_CISZY_ROZMOWY_S
            )
            continue

        pustych_z_rzedu = 0
        logger.info("[TY] %s", tekst)

        # MÓZG I RĘCE W JEDNYM. Agent sam decyduje, czy sięgnąć po narzędzie
        # (Spotify, aplikacje, wyszukiwarka, pamięć), czy po prostu odpowiedzieć.
        generator = agent.odpowiedz(tekst, historia_rozmowy)

        # Agent oddaje zdania do wypowiedzenia, a na samym końcu — słownik
        # ze stanem rozmowy. Przechwytujemy go tutaj, zanim trafi
        # do syntezatora mowy, bo słownika nie da się wypowiedzieć.
        stan = {}

        def tylko_zdania():
            for element in generator:
                if isinstance(element, dict):
                    stan.update(element)
                else:
                    yield element

        # Kula przełącza się na "speaking" dopiero przy pierwszym dźwięku,
        # a nie już teraz — inaczej świeciłaby "mówię" przez te sekundy,
        # w których model jeszcze myśli, a z głośników nic nie leci.
        wypowiedziane = tts.mow_strumieniowo(
            tylko_zdania(), na_start=lambda: orb.set_state("speaking")
        )

        if wypowiedziane:
            logger.info("[JARVIS] %s", wypowiedziane)
        else:
            logger.warning("Agent nie zwrócił żadnej treści.")

        if stan.get("historia"):
            historia_rozmowy = stan["historia"]

        orb.set_state("idle")

        # Blokada ekranu, uśpienie, restart i wyłączenie czekały na ten moment:
        # odpowiedź już wybrzmiała, więc można spytać o zgodę i wykonać.
        if stan.get("system"):
            dopisz, koniec_po_systemie = _wykonaj_polecenie_systemowe(
                orb, stan["system"]
            )
            historia_rozmowy = historia_rozmowy + dopisz
            orb.set_state("idle")
            if koniec_po_systemie:
                # Rozmowa kończy się tu, a zapis do pamięci robi wspólny
                # kawałek kodu pod pętlą — nie duplikujemy go.
                break

        # Pożegnanie już wybrzmiało (mow_strumieniowo blokuje), więc dopiero
        # teraz wychodzimy — inaczej Jarvis urwałby sobie "do zobaczenia"
        # w połowie słowa.
        if stan.get("koniec"):
            logger.info("Agent zakończył rozmowę na prośbę użytkownika.")
            break

        if wake_word_listener.czy_zatrzymano():
            break

        # Nasłuch bez wake worda. None znaczy "cisza — koniec rozmowy",
        # pusty string znaczy "hałas, słuchaj dalej".
        tekst = wake_word_listener.sluchaj_bez_wake_worda(
            orb.set_state, limit_ciszy_s=LIMIT_CISZY_ROZMOWY_S
        )

    if tekst is None:
        logger.info("Cisza przez %d s — kończę rozmowę.", LIMIT_CISZY_ROZMOWY_S)

    # Koniec sesji — zachowujemy końcówkę rozmowy na dysku, żeby następnym
    # razem (także po restarcie komputera) dało się do niej wrócić.
    try:
        pamiec.zapisz_rozmowe(historia_rozmowy)
    except Exception:
        logger.exception("Nie udało się zapisać rozmowy do pamięci")


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
        # Pierwsza rzecz w wątku roboczym: dociągnięcie ciężkich modułów.
        # Kula już wtedy pulsuje na ekranie, więc czekanie jest widoczne.
        zaladuj_moduly()

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

        # HUD MUSI to pokazać. Wcześniej wątek umierał po cichu, a okno dalej
        # animowało się, jakby Jarvis słuchał — był kompletnie głuchy, a z ekranu
        # nie dało się tego poznać. Szczegóły błędu są w jarvis.log.
        try:
            orb.set_state("offline")
        except Exception:
            pass

    logger.info("Pętla nasłuchu zakończona.")


def main():
    skonfiguruj_logowanie()

    logger.info("=" * 50)
    logger.info("JARVIS startuje. Dziennik: %s", PLIK_LOGU)
    logger.info("Powiedz 'Hey Jarvis', poczekaj na sygnał, potem komendę.")
    logger.info("Zamknięcie: prawy klik w ikonę zasobnika -> Zamknij Jarvisa.")
    logger.info("=" * 50)

    # NIŻSZY PRIORYTET PROCESU
    #
    # Jarvis to asystent w tle — nie powinien odbierać procesora temu, czym
    # akurat się zajmujesz. Priorytet "poniżej normalnego" znaczy: gdy komputer
    # jest wolny, Jarvis dostaje tyle mocy, ile chce (więc nie działa wolniej),
    # ale gdy grasz albo pracujesz, Windows najpierw obsługuje Twój program.
    #
    # To nie zmniejsza liczby widocznej w Menedżerze zadań, kiedy nic innego
    # nie działa. Zmienia to, czy komputer "przymula", gdy Whisper akurat
    # rozpoznaje mowę — a to zwykle jest prawdziwy problem.
    try:
        import psutil

        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        logger.info("Priorytet procesu: poniżej normalnego.")
    except Exception:
        logger.warning("Nie udało się obniżyć priorytetu procesu.", exc_info=True)

    app = QApplication(sys.argv)

    # Druga kopia Jarvisa niczego nie wnosi, a podwaja zużycie procesora
    # i kłóci się z pierwszą o mikrofon. Jeśli jakaś już działa, prosimy ją
    # o pokazanie HUD-a i kończymy.
    if _powiadom_dzialajaca_kopie():
        logger.info("Jarvis już działa — pokazuję jego okno zamiast startować drugi raz.")
        return 0

    # Pełnoekranowy HUD. Zmienna nazywa się dalej "orb" — reszta pliku
    # woła na niej tylko set_state(), a ta metoda się nie zmieniła.
    orb = gui.JarvisHUD()
    orb.pokaz()

    # Nasłuch próśb od kolejnych uruchomień. Referencję trzymamy w zmiennej,
    # inaczej garbage collector posprzątałby serwer razem z nasłuchem.
    serwer_kopii = _nasluchuj_kolejnych_kopii(orb)

    # Wątek roboczy startuje dopiero po pokazaniu okna, żeby długie ładowanie
    # modelu Whispera odbywało się już przy widocznym, animowanym HUD-zie
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

        # Moduł może jeszcze nie być załadowany, jeśli zamykasz Jarvisa
        # w trakcie startu — wtedy nie ma czego zatrzymywać ani zwalniać.
        if wake_word_listener is None:
            logger.info("Nasłuch nie zdążył wystartować — nic do sprzątania.")
            return

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
    #
    # Kiedyś budził go timer co 200 ms. Działało, ale każde takie budzenie to
    # Python w wątku GUI, a ten musi wtedy czekać na GIL — i animacja HUD-a
    # przycinała się pięć razy na sekundę, gdy Whisper akurat pracował.
    #
    # Teraz Python budzi się TYLKO po wciśnięciu Ctrl+C:
    #   1. set_wakeup_fd: gdy przyjdzie sygnał, Python wpisuje bajt do gniazda,
    #   2. QSocketNotifier: Qt widzi ten bajt i na moment wraca do Pythona,
    #   3. wtedy wykonuje się nasza obsługa SIGINT, która woła app.quit().
    budzik = _BudzikCtrlC(app)

    kod = app.exec()
    budzik.zamknij()

    # Zabezpieczenie na wypadek zamknięcia inną drogą niż menu zasobnika
    # (Ctrl+C, wylogowanie użytkownika). posprzataj() jest idempotentne.
    posprzataj()

    logger.info("Jarvis zakończył pracę. Do zobaczenia!")
    return kod


if __name__ == "__main__":
    sys.exit(main())
