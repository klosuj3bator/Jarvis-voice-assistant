"""
tts.py — warstwa "ust" Jarvisa: zamienia tekst na mowę i odtwarza ją.

Wystawia jedną funkcję: mow(tekst). Blokuje wykonanie do końca wypowiedzi,
więc reszta programu wie, kiedy Jarvis skończył mówić — to ważne, bo inaczej
mikrofon zacząłby nasłuchiwać w trakcie mówienia i Jarvis usłyszałby sam siebie.

Na razie moduł jest samodzielny — nie wie nic o mikrofonie ani o Claude.


JAK TO DZIAŁA
=============

Trzy kroki, każdy w osobnej funkcji:

  1. edge-tts wysyła tekst do syntezatora mowy Microsoftu (tego samego, który
     napędza Edge'a) i dostaje z powrotem nagranie MP3. Wymaga internetu,
     ale głosy neuronowe brzmią nieporównanie lepiej niż lokalne SAPI.
  2. PyAV dekoduje MP3 do surowych próbek dźwięku (tablica liczb).
  3. sounddevice wypycha te próbki na głośniki i czeka na koniec.

Dlaczego nie playsound, skoro byłby krótszy? Bo biblioteka jest od lat
nierozwijana i lubi się sypać na Windows przy ścieżkach z polskimi znakami,
a jej instalacja bywa problematyczna na nowszych Pythonach. PyAV i sounddevice
są w projekcie OBECNE — PyAV przyszedł razem z faster-whisper, a sounddevice
obsługuje mikrofon. Zero nowych zależności to lepszy interes niż pięć linijek
mniej kodu.
"""

import asyncio
import io
import logging
import queue
import re
import threading
import time

import av
import edge_tts
import numpy as np
import sounddevice as sd

import zajetosc

logger = logging.getLogger(__name__)

# Polski głos męski. Alternatywa: "pl-PL-ZofiaNeural" (żeński).
# Pełną listę wypisze: python -c "import asyncio,edge_tts; print(asyncio.run(edge_tts.list_voices()))"
GLOS = "pl-PL-MarekNeural"

# Tempo i wysokość mowy. Format wymagany przez edge-tts: znak + wartość + jednostka.
# "+0%" to naturalne tempo; "+15%" brzmi bardziej energicznie i skraca oczekiwanie.
TEMPO = "+8%"
WYSOKOSC = "+0Hz"


def _da_sie_wypowiedziec(tekst):
    """
    Czy w tekście jest cokolwiek do powiedzenia?

    Syntezator Microsoftu na tekście bez liter i cyfr — "...", "!!!" — nie
    zwraca żadnego dźwięku, tylko zgłasza błąd. W dzienniku wyglądało to jak
    poważna awaria, a w głośnikach jak cisza bez wyjaśnienia. Zdarzyło się
    naprawdę: model odpowiedział samym wielokropkiem i Jarvis zaniemówił.

    Zwraca: True, jeśli jest co syntezować.
    """
    return bool(tekst) and re.search(r"\w", tekst, re.UNICODE) is not None


def _syntezuj(tekst):
    """
    Wysyła tekst do syntezatora Microsoftu i zwraca nagranie jako bajty MP3.

    edge-tts jest asynchroniczne (strumieniuje dźwięk kawałkami, w miarę jak
    serwer go generuje), ale reszta Jarvisa jest zwykłym kodem synchronicznym.
    Dlatego zamykamy asynchroniczność w środku: asyncio.run() uruchamia pętlę
    zdarzeń, czeka na komplet danych i zwraca gotowy wynik. Z zewnątrz ta
    funkcja wygląda jak każda inna — po prostu zwraca bajty.

    Zwraca: bajty pliku MP3.
    """

    async def pobierz():
        komunikat = edge_tts.Communicate(
            tekst, GLOS, rate=TEMPO, pitch=WYSOKOSC
        )
        bufor = bytearray()
        # Strumień zawiera dwa rodzaje porcji: "audio" (dźwięk) oraz
        # "WordBoundary" (znaczniki czasu słów, przydatne do napisów).
        # Nas interesuje wyłącznie dźwięk.
        async for porcja in komunikat.stream():
            if porcja["type"] == "audio":
                bufor.extend(porcja["data"])
        return bytes(bufor)

    return asyncio.run(pobierz())


def _dekoduj_mp3(dane_mp3):
    """
    Zamienia bajty MP3 na próbki dźwięku gotowe do odtworzenia.

    Zwraca: (tablica numpy float32 o kształcie [próbki, kanały], częstotliwość).
    """
    # PyAV potrafi czytać prosto z pamięci, więc nie zapisujemy pliku na dysk.
    # Mniej śmieci w katalogu i szybciej, bo nie czekamy na dysk.
    kontener = av.open(io.BytesIO(dane_mp3))
    strumien = kontener.streams.audio[0]

    ramki = []
    for ramka in kontener.decode(strumien):
        # to_ndarray() zwraca kształt (kanały, próbki) — transponujemy
        # do (próbki, kanały), bo tego oczekuje sounddevice.
        ramki.append(ramka.to_ndarray().T)

    czestotliwosc = strumien.rate
    kontener.close()

    if not ramki:
        return np.zeros((0, 1), dtype=np.float32), czestotliwosc

    audio = np.concatenate(ramki, axis=0)

    # edge-tts zwraca dźwięk 16-bitowy całkowity; sounddevice woli float32
    # w zakresie -1.0..1.0. Jeśli PyAV oddał już floaty, nie ruszamy ich.
    if audio.dtype == np.int16:
        audio = audio.astype(np.float32) / 32768.0
    else:
        audio = audio.astype(np.float32)

    return audio, czestotliwosc


def mow(tekst):
    """
    GŁÓWNE WEJŚCIE TEGO MODUŁU — wypowiada podany tekst na głos.

    tekst — to, co Jarvis ma powiedzieć

    Funkcja jest BLOKUJĄCA: wraca dopiero, gdy dźwięk skończy się odtwarzać.
    To celowe. Gdyby wracała od razu, program wznowiłby nasłuch mikrofonu
    w trakcie mówienia i Jarvis usłyszałby własny głos — a wtedy albo
    wykryłby w nim swoje słowo-klucz, albo nagrał samego siebie jako komendę.

    Zwraca: True, jeśli udało się wypowiedzieć; False przy błędzie.
    """
    if not _da_sie_wypowiedziec(tekst):
        logger.info("Nie ma czego wypowiedzieć: %r", tekst)
        return False

    # Jedno mówienie naraz — przypomnienie i odpowiedź nie wejdą sobie w słowo
    # (opis w zajetosc.py).
    with zajetosc.zajmij("mówienie"):
        try:
            dane_mp3 = _syntezuj(tekst)
        except Exception:
            # Najczęstsza przyczyna to brak internetu — synteza dzieje się po
            # stronie Microsoftu. Jarvis ma wtedy zamilknąć, a nie przewrócić się.
            logger.exception("Nie udało się zsyntezować mowy")
            return False

        if not dane_mp3:
            logger.warning("Syntezator zwrócił puste nagranie dla: %r", tekst)
            return False

        try:
            audio, czestotliwosc = _dekoduj_mp3(dane_mp3)
            sd.play(audio, czestotliwosc)
            # wait() blokuje aż do końca odtwarzania — to ono robi z tej funkcji
            # funkcję synchroniczną.
            sd.wait()
        except Exception:
            logger.exception("Nie udało się odtworzyć mowy")
            return False

    logger.info("Powiedziałem: %s", tekst)
    return True


def _przygotuj_audio(tekst):
    """
    Synteza + dekodowanie w jednym: z tekstu robi gotowe próbki dźwięku.

    To ta część pracy, którą w mow_strumieniowo() wykonujemy Z WYPRZEDZENIEM,
    w tle, podczas gdy głośniki grają poprzednie zdanie.

    Zwraca: (audio, częstotliwość) albo (None, None) przy błędzie.
    """
    try:
        dane_mp3 = _syntezuj(tekst)
        if not dane_mp3:
            return None, None
        return _dekoduj_mp3(dane_mp3)
    except Exception:
        logger.exception("Nie udało się przygotować dźwięku dla: %r", tekst)
        return None, None


# Ile gotowych zdań trzymamy w zapasie. 2 wystarczą: jedno gra, drugie czeka.
# Więcej nie przyspieszy odtwarzania (i tak gramy po kolei), a niepotrzebnie
# każe generować tekst, którego użytkownik może nigdy nie usłyszeć.
ROZMIAR_BUFORA = 2

# Po przerwaniu czekamy najwyżej tyle, aż agent domknie odpowiedź i odda
# historię. Zwykle to ułamek sekundy; dłużej tylko wtedy, gdy akurat trwa
# narzędzie, którego nie da się przerwać w połowie (np. szukanie w Spotify).
MAKS_CZEKANIA_PO_PRZERWANIU_S = 20

# Zdanie, które właśnie leci z głośników. Nasłuch "Hey Jarvis" w trakcie
# mówienia sprawdza je, żeby Jarvis nie przerwał sam siebie, gdy mówi
# "Hej, tu Jarvis" (opis w wake_word_listener.py, WejscieWSlowo).
_aktualne_zdanie = ""


def aktualne_zdanie():
    """Zdanie, które właśnie jest odtwarzane (pusty napis, gdy cisza)."""
    return _aktualne_zdanie


def mow_strumieniowo(generator_zdan, na_start=None, przerwanie=None):
    """
    Wypowiada zdania z generatora, przygotowując kolejne w tle.

    generator_zdan — generator oddający całe zdania (np. z agent.odpowiedz)
    na_start       — opcjonalna funkcja wołana tuż przed pierwszym dźwiękiem
                     (main.py przełącza tym kulę na stan "speaking")
    przerwanie     — opcjonalny threading.Event. Gdy ktoś go ustawi (i zawoła
                     przerwij()), mowa milknie w pół zdania, a funkcja wraca
                     z tym, co zdążyło zabrzmieć — patrz WEJŚCIE W SŁOWO niżej.


    PODWÓJNE BUFOROWANIE — NA CZYM TO POLEGA
    ========================================

    Wypowiedzenie zdania składa się z dwóch etapów o zupełnie różnym charakterze:

        SYNTEZA    — wysłanie tekstu do Microsoftu i odebranie MP3.
                     Trwa ok. 1,5-2,5 s i przez ten czas procesor głównie CZEKA na sieć.
        ODTWARZANIE — puszczenie próbek na głośniki.
                     Trwa tyle, ile zdanie, i przez ten czas czeka CZŁOWIEK.

    Zrobione naiwnie, po kolei, dają ciszę przed każdym zdaniem:

        [synteza 1][gra 1][synteza 2][gra 2][synteza 3][gra 3]
                          ^^^^^^^^^^         ^^^^^^^^^^
                          tu jest cisza, słychać "zacinanie się"

    Sztuczka polega na tym, że te dwa etapy nie muszą na siebie czekać —
    syntezowanie zdania 2 nie wymaga niczego od odtwarzania zdania 1.
    Puszczamy je więc RÓWNOLEGLE, w osobnym wątku:

        [synteza 1][   gra 1   ][   gra 2   ][   gra 3   ]
                   [synteza 2][synteza 3]
                    ^ dzieje się w tle, w trakcie grania

    Cisza zostaje tylko przed pierwszym zdaniem, bo tam nie ma czego nakładać.
    Kolejne wchodzą jedno po drugim, bez przerw — a to właśnie odróżnia mowę,
    która brzmi płynnie, od takiej, która brzmi jak zacinająca się płyta.

    "Podwójne" znaczy: dwa bufory. Gdy jeden gra, drugi jest napełniany.
    Realizuje to kolejka o rozmiarze ROZMIAR_BUFORA: wątek produkujący
    zatrzymuje się sam, gdy zapas jest pełny, więc nie zsyntezuje całej
    wypowiedzi na zapas, gdybyś przerwał ją w połowie.

    Funkcja pozostaje BLOKUJĄCA — wraca dopiero po wybrzmieniu ostatniego
    zdania (albo po przerwaniu).


    WEJŚCIE W SŁOWO
    ===============

    Gdy w trakcie mówienia padnie "Hey Jarvis", nasłuch z innego wątku ustawia
    `przerwanie` i woła przerwij(). Tu trzeba wtedy zadbać o trzy rzeczy:

      1. Cisza natychmiast — także gdy przerwanie trafi w chwilę MIĘDZY
         zdaniami. Dlatego sprawdzamy flagę przed i po sd.play(): jeśli
         nasłuch zatrzymał stary dźwięk ułamek sekundy przed startem nowego,
         nowy zatrzymujemy sami.
      2. Wątek przygotowujący NIE MOŻE zawisnąć na pełnej kolejce. Po
         przerwaniu dalej ją opróżniamy, aż skończy.
      3. Generator agenta trzeba dojeść do końca — na samym końcu oddaje
         historię rozmowy. Producent przestaje więc syntezować (po co, skoro
         nikt nie usłyszy), ale dalej pobiera z generatora, a agent sam
         przerywa generowanie, gdy zobaczy flagę.

    Zwraca: pełny tekst WYPOWIEDZIANYCH zdań, sklejony spacjami. Przy
    przerwaniu ostatnie z nich mogło zabrzmieć tylko częściowo.
    """
    global _aktualne_zdanie

    def przerwano():
        return przerwanie is not None and przerwanie.is_set()

    # maxsize wymusza, że wątek w tle wyprzedza odtwarzanie najwyżej
    # o ROZMIAR_BUFORA zdań, zamiast produkować bez opamiętania.
    kolejka = queue.Queue(maxsize=ROZMIAR_BUFORA)

    # Wartownik: unikalny obiekt oznaczający "koniec, nic więcej nie będzie".
    # Zwykłe None byłoby mylące, bo None może też oznaczać nieudaną syntezę.
    KONIEC = object()

    def producent():
        """Pobiera zdania z generatora, syntezuje je i wkłada do kolejki."""
        try:
            for zdanie in generator_zdan:
                # Po przerwaniu tylko dojadamy generator (punkt 3 w opisie).
                # Znaki przestankowe bez ani jednej litery pomijamy w ciszy —
                # patrz _da_sie_wypowiedziec().
                if przerwano() or not _da_sie_wypowiedziec(zdanie):
                    continue
                audio, czestotliwosc = _przygotuj_audio(zdanie)
                if audio is None:
                    # Jedno zdanie się nie udało — pomijamy je i mówimy dalej.
                    # Lepiej zgubić zdanie niż uciąć całą odpowiedź.
                    logger.warning("Pomijam zdanie, którego nie udało się zsyntezować.")
                    continue
                if not przerwano():
                    kolejka.put((zdanie, audio, czestotliwosc))
        except Exception:
            logger.exception("Błąd w wątku przygotowującym mowę")
        finally:
            # finally, żeby konsument nie zawisł na kolejce nawet wtedy,
            # gdy generator albo synteza wybuchną w połowie.
            kolejka.put(KONIEC)

    watek = threading.Thread(target=producent, name="watek-tts", daemon=True)
    watek.start()

    wypowiedziane = []
    pierwsze = True

    # Zajmujemy usta na CAŁĄ wypowiedź, a nie na każde zdanie osobno —
    # inaczej przypomnienie mogłoby wskoczyć w przerwę między zdaniami.
    with zajetosc.zajmij("mówienie"):
        while not przerwano():
            # Z krótkim limitem, żeby przerwanie w czasie, gdy agent jeszcze
            # myśli nad pierwszym zdaniem, też zadziałało od razu.
            try:
                element = kolejka.get(timeout=0.1)
            except queue.Empty:
                continue
            if element is KONIEC or przerwano():
                break

            zdanie, audio, czestotliwosc = element

            if pierwsze:
                if na_start is not None:
                    na_start()
                pierwsze = False

            _aktualne_zdanie = zdanie
            sd.play(audio, czestotliwosc)
            if przerwano():
                sd.stop()   # punkt 1 w opisie: przerwanie tuż przed startem zdania
            sd.wait()  # to tutaj funkcja pozostaje blokująca
            _aktualne_zdanie = ""

            wypowiedziane.append(zdanie)

    if przerwano():
        # Punkt 2 w opisie: opróżniamy kolejkę, aż producent skończy.
        limit = time.monotonic() + MAKS_CZEKANIA_PO_PRZERWANIU_S
        while watek.is_alive() and time.monotonic() < limit:
            try:
                kolejka.get(timeout=0.1)
            except queue.Empty:
                pass
        if watek.is_alive():
            logger.warning("Agent nie skończył w %d s od przerwania — nie czekam dłużej.",
                           MAKS_CZEKANIA_PO_PRZERWANIU_S)
    else:
        watek.join(timeout=5)

    pelny_tekst = " ".join(wypowiedziane)
    if pelny_tekst:
        logger.info("Powiedziałem (%d zdań%s): %s", len(wypowiedziane),
                    ", PRZERWANE" if przerwano() else "", pelny_tekst)

    return pelny_tekst


def synteza_ogg(tekst):
    """
    Zamienia tekst na głosówkę w formacie, który Telegram pokazuje jako
    wiadomość głosową (z falą dźwięku i przyciskiem odtwarzania).

    Telegram jest tu wybredny: MP3 od edge-tts przyjmie, ale wyświetli jako
    zwykły plik muzyczny. Jako głosówkę pokazuje wyłącznie OGG z kodekiem
    Opus — tym samym, którego używa do nagrań z telefonu. Przekodowujemy więc
    MP3 na Opus przez PyAV, który i tak jest w projekcie.

    Niczego nie odtwarza, więc NIE bierze blokady mówienia z zajetosc.py —
    głosówka dla telefonu nie wchodzi w słowo temu, co mówią głośniki.

    Zwraca: bajty pliku .ogg albo None przy błędzie.
    """
    if not _da_sie_wypowiedziec(tekst):
        return None

    try:
        mp3 = _syntezuj(tekst)
        if not mp3:
            return None

        wejscie = av.open(io.BytesIO(mp3))
        bufor = io.BytesIO()
        wyjscie = av.open(bufor, mode="w", format="ogg")

        # 48 kHz mono — natywna częstotliwość Opusa. 32 kb/s to jakość
        # głosówek z telefonu: mowa brzmi czysto, a plik jest malutki.
        strumien = wyjscie.add_stream("libopus", rate=48000, layout="mono")
        strumien.bit_rate = 32_000

        # Opus przyjmuje dźwięk porcjami o ściśle określonej długości
        # (20 ms). frame_size w resamplerze tnie próbki właśnie na takie porcje.
        resampler = av.AudioResampler(
            format="s16", layout="mono", rate=48000,
            frame_size=strumien.codec_context.frame_size or 960,
        )

        for ramka in wejscie.decode(wejscie.streams.audio[0]):
            for porcja in resampler.resample(ramka):
                wyjscie.mux(strumien.encode(porcja))
        # Dokończenie: resampler i koder trzymają jeszcze końcówkę w buforze.
        for porcja in resampler.resample(None):
            wyjscie.mux(strumien.encode(porcja))
        wyjscie.mux(strumien.encode(None))

        wyjscie.close()
        wejscie.close()
        return bufor.getvalue()
    except Exception:
        logger.exception("Nie udało się przygotować głosówki dla: %r", tekst)
        return None


def przerwij():
    """
    Natychmiast ucisza głośniki. BEZPIECZNE z dowolnego wątku — woła to
    nasłuch "Hey Jarvis" w trakcie mówienia (patrz mow_strumieniowo).
    """
    sd.stop()


# --- Test samego modułu: `python tts.py` ---
if __name__ == "__main__":
    from logging_setup import skonfiguruj_logowanie

    skonfiguruj_logowanie()

    ZDANIA = [
        "Dzień dobry. Jestem Jarvis, twój asystent głosowy.",
        "Włączam album The Grind Deluxe. Miłego słuchania.",
        "Nie znalazłem takiej aplikacji. Może chodziło ci o coś innego?",
        "Zrobione. Coś jeszcze, czy mogę wracać do drzemki?",
    ]

    logger.info("Głos: %s, tempo %s", GLOS, TEMPO)

    for zdanie in ZDANIA:
        logger.info("--- %s", zdanie)
        if not mow(zdanie):
            logger.error("Nie udało się wypowiedzieć tego zdania.")
            break
