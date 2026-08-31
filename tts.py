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
import threading

import av
import edge_tts
import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)

# Polski głos męski. Alternatywa: "pl-PL-ZofiaNeural" (żeński).
# Pełną listę wypisze: python -c "import asyncio,edge_tts; print(asyncio.run(edge_tts.list_voices()))"
GLOS = "pl-PL-MarekNeural"

# Tempo i wysokość mowy. Format wymagany przez edge-tts: znak + wartość + jednostka.
# "+0%" to naturalne tempo; "+15%" brzmi bardziej energicznie i skraca oczekiwanie.
TEMPO = "+8%"
WYSOKOSC = "+0Hz"


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
    if not tekst or not tekst.strip():
        return False

    try:
        dane_mp3 = _syntezuj(tekst)
    except Exception:
        # Najczęstsza przyczyna to brak internetu — synteza dzieje się po stronie
        # Microsoftu. Jarvis ma wtedy zamilknąć, a nie przewrócić się.
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


def mow_strumieniowo(generator_zdan, na_start=None):
    """
    Wypowiada zdania z generatora, przygotowując kolejne w tle.

    generator_zdan — generator oddający całe zdania (np. z chat.odpowiedz_rozmowa_stream)
    na_start       — opcjonalna funkcja wołana tuż przed pierwszym dźwiękiem
                     (main.py przełącza tym kulę na stan "speaking")


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
    zdania. Mikrofon przez cały ten czas nie nasłuchuje, więc Jarvis
    nie usłyszy samego siebie.

    Zwraca: pełny tekst wszystkich WYPOWIEDZIANYCH zdań, sklejony spacjami.
    """
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
                if not zdanie or not zdanie.strip():
                    continue
                audio, czestotliwosc = _przygotuj_audio(zdanie)
                if audio is None:
                    # Jedno zdanie się nie udało — pomijamy je i mówimy dalej.
                    # Lepiej zgubić zdanie niż uciąć całą odpowiedź.
                    logger.warning("Pomijam zdanie, którego nie udało się zsyntezować.")
                    continue
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

    while True:
        element = kolejka.get()
        if element is KONIEC:
            break

        zdanie, audio, czestotliwosc = element

        if pierwsze:
            if na_start is not None:
                na_start()
            pierwsze = False

        sd.play(audio, czestotliwosc)
        sd.wait()  # to tutaj funkcja pozostaje blokująca

        wypowiedziane.append(zdanie)

    watek.join(timeout=5)

    pelny_tekst = " ".join(wypowiedziane)
    if pelny_tekst:
        logger.info("Powiedziałem (%d zdań): %s", len(wypowiedziane), pelny_tekst)

    return pelny_tekst


def przerwij():
    """
    Natychmiast przerywa mówienie.

    Przyda się później, gdy będziesz chciał móc wejść Jarvisowi w słowo —
    na razie nic tego nie woła.
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
