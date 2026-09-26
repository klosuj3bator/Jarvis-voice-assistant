"""
wake_word_listener.py — warstwa "uszu" Jarvisa.

Wystawia jedną funkcję dla reszty programu: sluchaj_komendy().
Blokuje wykonanie, czeka na "Hey Jarvis", nagrywa kilka sekund,
przepuszcza je przez Whispera i zwraca rozpoznany tekst jako string.

Główna pętla programu NIE żyje w tym pliku — jest w main.py.
Ten moduł odpowiada wyłącznie za: mikrofon -> tekst.

Podział na dwa etapy (tani detektor + drogi Whisper) jest celowy: Whisper jest
zbyt wolny, żeby puszczać przez niego cały czas wszystko, co słyszy mikrofon.
openWakeWord pełni rolę taniego "strażnika" i jest w pełni open-source —
nie wymaga konta, klucza API ani sieci (poza jednorazowym pobraniem modeli).
"""

import atexit
import collections
import contextlib
import datetime
import difflib
import logging
import os
import re
import sys
import threading
import time
import unicodedata
import wave

import numpy as np
import sounddevice as sd

import slownik
import zajetosc

# faster_whisper i openwakeword są importowane LENIWIE, w funkcjach poniżej.
#
# Powód jest mierzalny: na zimnym starcie (po restarcie komputera) sam
# `import openwakeword` trwa ok. 16 sekund, bo pociąga za sobą scikit-learn —
# a ten przychodzi wyłącznie przez moduł do trenowania własnych słów-kluczy,
# którego w ogóle nie używamy. Import na górze pliku oznaczałby, że te
# kilkanaście sekund mija, zanim cokolwiek pojawi się na ekranie.

# Logger nazwany jak moduł — w jarvis.log widać wtedy, że linia przyszła stąd.
# Konfiguracją (dokąd zapisywać) zajmuje się main.py, nie ten plik.
logger = logging.getLogger(__name__)

# --- Ustawienia, które najczęściej będziesz chciał zmieniać ---

# Wbudowany model openWakeWord. Uwaga: to fraza "HEY Jarvis", nie samo "Jarvis".
# Inne gotowe opcje: "alexa", "hey_mycroft", "hey_rhasspy".
MODEL_WAKE_WORD = "hey_jarvis"

# Próg pewności 0.0-1.0, powyżej którego od razu uznajemy słowo za wykryte.
#
# DLACZEGO TAK NISKO (wcześniej było 0.5)
# =======================================
# Model "hey_jarvis" trenowano na angielskiej wymowie. Ta sama fraza
# powiedziana po polsku dostaje znacznie niższą ocenę. Zmierzone
# na nagraniach z syntezatora mowy:
#
#     "Hey Jarvis" angielskim głosem ....... 0.999
#     "Hej Dżarwis" (polska wymowa) ........ 0.99
#     "Hey Jarvis" polskim głosem .......... 0.44    <- przy progu 0.5 nie działało
#     "Hej Dżarwis" głosem kobiecym ........ 0.29    <- też nie
#     "Hej Jarwis" (polskie "j") ........... 0.09    <- tym bardziej
#
# Czyli to nie mikrofon i nie akcent — to próg odrzucał poprawne próby.
# (Głośność nagrania prawie nic nie zmienia: te same frazy ściszone
# czterokrotnie dostawały niemal identyczne oceny.)
#
# A ile dostaje zwykła mowa BEZ słowa aktywującego? Na prawdziwym nagraniu
# z tego mikrofonu: najwyżej 0.0096 i ani jednej ramki powyżej 0.05.
# Margines jest więc ogromny — stąd 0.2.
#
# Gdyby Jarvis zaczął się odzywać sam z siebie, podnieś tę wartość.
PROG_WYKRYCIA = 0.2

# Drugi próg: za nisko, żeby reagować od razu, ale wyraźnie powyżej szumu.
# Takie "podejrzenie" sprawdzamy jeszcze raz, Whisperem — patrz
# _potwierdz_whisperem(). To ono łapie wymowę "Hej Jarwis" z ocenami ~0.09.
PROG_PODEJRZENIA = 0.04

# Ile sekund nagrywamy po usłyszeniu wake worda. Stała długość — bez wykrywania ciszy.
CZAS_NAGRANIA = 5

# Model Whispera: "small" (244 mln parametrów).
#
# DLACZEGO NIE "medium" — WYNIKI POMIARÓW
# =======================================
# Teoria mówi, że większy model lepiej radzi sobie z obcojęzycznymi nazwami
# własnymi: Whisper przewiduje kolejne słowa trochę jak autouzupełnianie, więc
# przy nagraniu po polsku spodziewa się polskich słów, a wtrącone angielskie
# "The Grind (Deluxe)" nie pasuje do niczego i bywa dopasowywane do najbliższego
# polskiego brzmienia. Większy model zna więcej angielskich tytułów, więc
# w założeniu miał zgadywać rzadziej.
#
# Pomiar na tym konkretnym tytule pokazał coś odwrotnego (5 s nagrania, CPU, int8):
#
#     medium:  54 s  ->  "The Grind The Lux"   (błędnie)
#     small:   17 s  ->  "The Grind Deluxe"    (poprawnie)
#
# Czyli trzy razy wolniej i przy tym gorzej. Większy model nie jest automatycznie
# lepszy na krótkich, kilkusekundowych komendach — ma więcej swobody, żeby
# "poprawić" to, co usłyszał, na coś, co wydaje mu się sensowniejsze.
#
# Gdybyś kiedyś wracał do medium, rób to razem z GPU — na CPU czas odpowiedzi
# rośnie do poziomu, przy którym Jarvis przestaje być używalny.
MODEL_WHISPER = "small"

# Gdzie liczyć: "cuda" = karta NVIDIA, "cpu" = procesor.
#
# Wróciliśmy na CPU po nieudanej walce z CUDA na Windows: CTranslate2 uparcie
# nie znajdował cublas64_12.dll, mimo doinstalowania bibliotek, poprawiania PATH
# i os.add_dll_directory(). Kod obsługi GPU zostaje na miejscu — wystarczy
# zmienić te dwie stałe z powrotem na "cuda"/"float16", żeby spróbować ponownie.
URZADZENIE = "cpu"

# Format liczb, na których model wykonuje obliczenia.
#
# RÓŻNICA MIĘDZY int8 (CPU) A float16 (GPU)
# =========================================
# Model to miliony liczb (wag). Można je trzymać z różną precyzją:
#
#   int8    — liczby całkowite, 8 bitów na wagę. To KWANTYZACJA: oryginalne
#             wartości zmiennoprzecinkowe są zaokrąglane do 256 poziomów.
#             Model zajmuje ok. 4x mniej pamięci i liczy szybciej, bo procesory
#             sprawnie mnożą liczby całkowite. Kosztem jest utrata precyzji —
#             przy trudnych nagraniach to właśnie ona dokłada się do błędów.
#             Na CPU to jedyny sensowny wybór, bo tam liczby zmiennoprzecinkowe
#             są zbyt wolne.
#
#   float16 — liczby zmiennoprzecinkowe połówkowej precyzji, 16 bitów na wagę.
#             Dwa razy mniej pamięci niż standardowe float32, ale karty NVIDIA
#             mają do nich sprzętowe wsparcie, więc liczą je szybciej niż float32
#             i DUŻO szybciej niż CPU cokolwiek. Precyzja jest wyraźnie wyższa
#             niż w int8, a to znaczy mniej pomyłek na niewyraźnych fragmentach.
#
# Krótko: int8 to "mniej dokładnie, ale znośnie szybko na CPU",
# a float16 to "dokładniej i znacznie szybciej, ale wymaga GPU".
#
# Skoro liczymy na procesorze, musi być int8 — float16 na CPU jest emulowany
# programowo i byłby dramatycznie wolny.
COMPUTE_TYPE = "int8"

# Ile rdzeni procesora wolno Whisperowi zająć podczas rozpoznawania mowy.
#
# Domyślnie bierze 4 — i to on odpowiadał za skoki obciążenia do 80-90%:
# przez kilka sekund po każdej wypowiedzi zajmował jedną trzecią procesora.
# Zmierzone (5-sekundowe nagranie, model small, 12 wątków logicznych):
#
#     wątki   czas    obciążenie całego CPU
#       1     9,8 s          8%
#       2     5,8 s         17%
#       3     4,9 s         25%     <- ustawione
#       4     5,1 s         33%     (domyślne)
#
# Trzy wątki są tak samo szybkie jak cztery — przy tak krótkim nagraniu
# Whisper nie umie wykorzystać czwartego rdzenia — a biorą o ćwierć mniej.
# Ustaw 2, jeśli wolisz o połowę mniejsze obciążenie kosztem ~1 s czekania.
WATKI_WHISPERA = 3


# --- ZAKOMENTOWANY KOD POD GPU (do ewentualnego powrotu) ---------------------
#
# Poniższy blok był próbą wskazania Pythonowi, gdzie leżą biblioteki CUDA,
# gdy CTranslate2 zgłaszał "cublas64_12.dll is not found". NIE zadziałał
# i przy pracy na CPU jest niepotrzebny — zostaje jako punkt wyjścia,
# gdyby kiedyś wracać do tematu.
#
# Uwaga na przyszłość: os.add_dll_directory() działa tylko wtedy, gdy zostanie
# wywołane PRZED pierwszym importem ctranslate2/faster_whisper. Po imporcie
# biblioteka ma już wczytane DLL-e i dokładanie ścieżek niczego nie zmienia.
# Dlatego ten blok musiałby stać na samej górze pliku, nad importami.
#
# import os, sys
# _SCIEZKI_CUDA = [
#     os.path.join(sys.prefix, "Lib", "site-packages", "nvidia", "cublas", "bin"),
#     os.path.join(sys.prefix, "Lib", "site-packages", "nvidia", "cudnn", "bin"),
#     r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA\v12.4\bin",
# ]
# for _sciezka in _SCIEZKI_CUDA:
#     if os.path.isdir(_sciezka):
#         os.add_dll_directory(_sciezka)
# -----------------------------------------------------------------------------

# Język, w którym mówisz do Jarvisa.
#
# To NAJWAŻNIEJSZE ustawienie dla szybkości — wymuszenie języka skraca
# rozpoznawanie mniej więcej o połowę, bo Whisper nie musi go najpierw zgadywać.
#
# Cena: całe zdania po angielsku wychodzą przekręcone. Zmierzone:
#     "Play the album Dark Side of the Moon"
#       z language="pl"     -> "Plaję album Dark Side of the Moon"
#       z autowykrywaniem   -> "Play the album Dark Side of the Moon"
#
# Angielskie TYTUŁY wplecione w polskie zdanie przechodzą bez szwanku
# ("włącz album The Grind Deluxe" działa), więc przy polskiej rozmowie
# to dobry interes. Ustaw None, jeśli chcesz mówić do Jarvisa także
# całymi zdaniami po angielsku — kosztem dwukrotnie dłuższego czekania.
JEZYK = "pl"

# Whisper pracuje na 16 kHz i openWakeWord też — dlatego jeden strumień obsługuje oba.
SAMPLE_RATE = 16000

# openWakeWord analizuje audio w porcjach po 80 ms = 1280 próbek przy 16 kHz.
# To wartość wymuszona przez architekturę modelu, nie dowolny wybór.
DLUGOSC_RAMKI = 1280


# --- Druga droga wykrywania: potwierdzanie Whisperem ---
#
# Model wake worda ocenia BRZMIENIE i po polsku bywa niepewny. Whisper
# rozumie MOWĘ i tę samą frazę zapisuje poprawnie nawet wtedy, gdy detektor
# dał jej 0.09. Łączymy więc oba: detektor pracuje cały czas (jest tani),
# a przy niepewnym wyniku pytamy Whispera, czy naprawdę padło "Jarvis".
#
# Model celowo "tiny", nie "small". Zmierzone na tym komputerze:
#
#     small ... 5,5 s  <- nie do przyjęcia dla słowa aktywującego
#     base .... 1,8 s  i przy tym mylił warianty wymowy
#     tiny .... 0,8 s  i trafił we wszystkie
#
# (Whisper zawsze liczy 30-sekundowe okno, więc krótszy fragment nie jest
# szybszy — o czasie decyduje wyłącznie rozmiar modelu.)
MODEL_POTWIERDZENIA = "tiny"

# Ile ostatnich sekund dźwięku trzymamy pod ręką na potrzeby potwierdzenia.
# Przy 1,5 s Whisper gubił początek frazy ("i Arwiz"), przy 2 s było dobrze.
BUFOR_POTWIERDZENIA_S = 2.0

# Najkrótszy odstęp między dwoma potwierdzeniami. Bez niego hałas wpadający
# w okolice progu podejrzenia potrafiłby odpalać Whispera kilka razy na sekundę.
ODSTEP_POTWIERDZEN_S = 1.5

# Ile czekamy od pierwszego podejrzenia, zanim uruchomimy Whispera.
#
# Ocena detektora narasta w trakcie wymawiania frazy: zaczyna od ułamków,
# a szczyt osiąga dopiero na jej końcu. Bez tej zwłoki Whisper ruszałby już
# przy 0.05, choć pół sekundy później wynik i tak przekroczyłby próg
# i odpowiedź byłaby natychmiastowa.
#
# 0.8 s to mniej więcej długość wypowiedzenia "Hey Jarvis". Zwłoka NIE opóźnia
# trudnych przypadków: gdy podejrzany dźwięk ucichnie wcześniej, Whisper
# rusza od razu. Liczy się tylko wtedy, gdy dźwięk trwa dłużej.
OPOZNIENIE_POTWIERDZENIA_S = 0.8

# Przy wejściu w słowo: ile dźwięku SPRZED wykrycia dokładamy na początek
# nagrania polecenia. Detektor zgłasza "Hey Jarvis" kilka ramek po jego końcu,
# a te ramki to już początek polecenia. Bez tego "Hej Jarvis, puść album…"
# wychodziło jako "Duct album…" — pierwsze słowo zjadał detektor.
OGON_PO_WYKRYCIU_S = 0.4

# Jak Whisper zapisuje usłyszane "Jarvis". Nie trzeba przewidzieć wszystkich
# form — porównujemy z nimi PODOBIEŃSTWO słowa, nie równość.
WZORCE_JARVIS = ("jarvis", "jarwis", "dzarvis", "dzarwis", "dziarwis",
                 "czarvis", "charvis", "arwis")

# Jak bardzo słowo musi być podobne do jednego z wzorców (0.0-1.0).
# 0.8 przepuszcza "jarwiz" i "dżarvisie", a odrzuca "jarzyny" (0.61)
# i "jarosław" — sprawdzone na liście zwykłych polskich zdań.
PROG_PODOBIENSTWA = 0.8


# --- Zasoby współdzielone między wywołaniami sluchaj_komendy() ---
#
# Detektor, model Whispera i strumień mikrofonu tworzymy RAZ i trzymamy tutaj.
# Gdyby main.py budował je przy każdej komendzie, każda wypowiedź kosztowałaby
# kilka sekund ładowania modelu i ponowne otwieranie urządzenia audio.
_detektor = None
_model_whisper = None
_model_potwierdzenia = None
_stream = None

# Sygnał "kończymy". Event to bezpieczny międzywątkowo przełącznik:
# wątek GUI go ustawia przy zamykaniu programu, a wątek nasłuchu regularnie
# sprawdza jego stan i grzecznie wychodzi z pętli.
#
# To jest właśnie powód, dla którego pętle poniżej czytają mikrofon
# małymi porcjami zamiast jednym wielkim blokiem: między porcjami mamy
# okazję zajrzeć na ten przełącznik. Gdybyśmy czytali 5 sekund naraz,
# zamknięcie programu musiałoby czekać na koniec tego odczytu.
_zatrzymaj_sie = threading.Event()


def zatrzymaj():
    """
    Prosi pętlę nasłuchu, żeby się zakończyła.

    BEZPIECZNE do wywołania z dowolnego wątku — to woła main.py przy zamykaniu
    programu z menu w zasobniku systemowym.
    """
    logger.info("Otrzymano prośbę o zatrzymanie nasłuchu.")
    _zatrzymaj_sie.set()


def czy_zatrzymano():
    """Zwraca True, jeśli poproszono o zakończenie pracy."""
    return _zatrzymaj_sie.is_set()


# Co ile sekund szukamy mikrofonu, gdy go nie ma.
CO_ILE_SZUKAC_MIKROFONU_S = 3

_zarejestrowano_atexit = False


def _przygotuj(callback_stanu=None):
    """
    Leniwa inicjalizacja: tworzy detektor, model i strumień mikrofonu —
    każde z nich dopiero wtedy, gdy go jeszcze nie ma.

    Dzięki temu main.py nie musi pamiętać o żadnym "setupie" — po prostu woła
    sluchaj_komendy(), a moduł sam się przygotowuje, kiedy jest potrzebny.

    Każdą z trzech rzeczy sprawdzamy OSOBNO, bo mikrofon może zniknąć
    w trakcie pracy (rozładowane słuchawki), a wtedy trzeba otworzyć go
    na nowo — bez ponownego, kilkusekundowego wczytywania modeli.
    """
    global _detektor, _model_whisper, _zarejestrowano_atexit

    if _detektor is None:
        _detektor = _utworz_detektor()
    _zapewnij_model_whispera()
    if _stream is None:
        _otworz_mikrofon(callback_stanu)

    # Strumień żyje przez cały czas działania programu, więc nie ma tu bloku `with`.
    # atexit gwarantuje, że mikrofon zostanie zwolniony przy wyjściu — także po Ctrl+C.
    if not _zarejestrowano_atexit:
        atexit.register(zamknij)
        _zarejestrowano_atexit = True


def _otworz_mikrofon(callback_stanu=None):
    """
    Otwiera strumień mikrofonu, a jeśli mikrofonu nie ma — CZEKA, aż się pojawi.


    SKĄD TA FUNKCJA
    ===============
    Wcześniej brak mikrofonu przy starcie kończył się wyjątkiem, który zabijał
    wątek nasłuchu na zawsze. Okno dalej się animowało, więc wyglądało na to,
    że Jarvis słucha — a był kompletnie głuchy. W dzienniku wyglądało to tak:

        22:29:16  Whisper działa na CPU
        22:29:16  PortAudioError: Error querying device -1

    Przyczyna była prozaiczna: słuchawki bezprzewodowe (JBL Quantum 360)
    łączą się kilka sekund po starcie systemu, a Jarvis był szybszy.

    Teraz: brak mikrofonu to stan przejściowy, nie awaria. Pokazujemy go
    w HUD-zie i co kilka sekund próbujemy ponownie.


    DLACZEGO PONOWNA INICJALIZACJA PORTAUDIO
    ========================================
    Samo ponawianie prób nic by nie dało. Biblioteka PortAudio, na której stoi
    sounddevice, odczytuje listę urządzeń audio RAZ, przy starcie programu,
    i potem korzysta z tej zapamiętanej listy. Słuchawki sparowane później
    po prostu by na niej nie istniały — próbowalibyśmy w nieskończoność.
    Dlatego przed każdą kolejną próbą każemy PortAudio odczytać listę od nowa.

    Zwraca: True, gdy mikrofon otwarty; False, gdy program jest zamykany.
    """
    global _stream

    zgloszono_brak = False

    while not _zatrzymaj_sie.is_set():
        try:
            # dtype="int16", bo tego formatu oczekuje openWakeWord.
            # blocksize = DLUGOSC_RAMKI daje najniższe opóźnienie przy wykrywaniu słowa.
            strumien = sd.InputStream(
                samplerate=SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=DLUGOSC_RAMKI,
            )
            strumien.start()
        except sd.PortAudioError as e:
            # Zgłaszamy tylko pierwszy raz — bez tego dziennik zapełniałby się
            # tym samym ostrzeżeniem co trzy sekundy.
            if not zgloszono_brak:
                logger.warning("Nie widzę mikrofonu (%s). Czekam, aż się pojawi...", e)
                if callback_stanu is not None:
                    callback_stanu("no_mic")
                zgloszono_brak = True

            # Czekamy małymi krokami, żeby zamknięcie programu nie musiało
            # czekać na koniec całej przerwy.
            for _ in range(int(CO_ILE_SZUKAC_MIKROFONU_S * 10)):
                if _zatrzymaj_sie.is_set():
                    return False
                time.sleep(0.1)

            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                logger.debug("Nie udało się odświeżyć listy urządzeń audio", exc_info=True)
            continue

        _stream = strumien
        logger.info("Mikrofon otwarty: %s", sd.query_devices(kind="input")["name"])

        if zgloszono_brak and callback_stanu is not None:
            logger.info("Mikrofon się pojawił — wracam do nasłuchu.")
            callback_stanu("idle")
        return True

    return False


def _mikrofon_utracony(blad):
    """
    Sprząta po mikrofonie, który zniknął w trakcie pracy — np. słuchawki
    się rozładowały albo wyszły poza zasięg.

    Zamykamy martwy strumień i zerujemy go. Następne wywołanie _przygotuj()
    zobaczy, że mikrofonu brak, i zacznie go szukać — z komunikatem w HUD-zie.
    """
    global _stream

    logger.warning("Utracono mikrofon (%s) — zacznę go szukać od nowa.", blad)
    try:
        if _stream is not None:
            _stream.close()
    except Exception:
        pass
    _stream = None


def _odetnij_scikit_learn():
    """
    Powstrzymuje openWakeWord przed wciągnięciem scikit-learn.

    NA CZYM TO POLEGA
    =================
    Plik openwakeword/__init__.py robi między innymi to:

        from openwakeword.custom_verifier_model import train_custom_verifier

    Ten moduł służy do TRENOWANIA własnych słów-kluczy i jako jedyny w całej
    bibliotece potrzebuje scikit-learn. My używamy wyłącznie gotowego modelu
    "hey_jarvis", więc nigdy go nie wołamy — a mimo to płacilibyśmy za niego
    ok. 13 sekund przy każdym zimnym starcie.

    Podstawiamy więc pod tę nazwę pustą atrapę, ZANIM openWakeWord zdąży
    zaimportować oryginał. Python sprawdza sys.modules przed sięgnięciem
    na dysk, więc widzi naszą atrapę i nie rusza scikit-learn.

    CZY TO BEZPIECZNE
    =================
    Sprawdziłem, że po takiej podmianie Model.predict() i VAD działają
    normalnie — one nie korzystają ze scikit-learn.

    Gdyby przyszła wersja openWakeWord zmieniła układ modułów, ten kod
    NIE zepsuje się po cichu: import wywali się głośnym błędem przy starcie.
    Żeby wrócić do zachowania domyślnego, wystarczy przestać wołać tę funkcję —
    kosztem kilkunastu sekund przy uruchamianiu.
    """
    import sys
    import types

    if "openwakeword.custom_verifier_model" in sys.modules:
        return

    atrapa = types.ModuleType("openwakeword.custom_verifier_model")
    # Nazwa musi istnieć, bo __init__.py ją stamtąd importuje.
    atrapa.train_custom_verifier = None
    sys.modules["openwakeword.custom_verifier_model"] = atrapa


def _brakuje_modeli():
    """
    Sprawdza, czy komplet plików openWakeWord leży już na dysku.

    Modele mieszkają w katalogu zainstalowanej biblioteki. Pytamy o nie
    bezpośrednio, zamiast ufać, że download_models() zrobi to tanio.

    Zwraca: True, jeśli czegokolwiek brakuje (czyli trzeba pobierać).
    """
    _odetnij_scikit_learn()

    import openwakeword

    katalog = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")

    wymagane = [
        f"{MODEL_WAKE_WORD}_v0.1.onnx",
        "melspectrogram.onnx",
        "embedding_model.onnx",
        "silero_vad.onnx",
    ]

    return any(not os.path.exists(os.path.join(katalog, p)) for p in wymagane)


def _utworz_detektor():
    """
    Tworzy detektor słowa-klucza openWakeWord.

    Przy pierwszym uruchomieniu pobiera modele (~kilkanaście MB) do katalogu
    biblioteki. Kolejne uruchomienia działają już w pełni offline.

    Zwraca: obiekt Model gotowy do wywołania .predict().
    """
    _odetnij_scikit_learn()

    from openwakeword.model import Model as WakeWordModel

    logger.info("Przygotowuję detektor wake worda '%s'...", MODEL_WAKE_WORD)

    # download_models() pobiera model + pliki pomocnicze (melspectrogram,
    # embedding, VAD). Wołamy ją TYLKO wtedy, gdy czegoś brakuje.
    #
    # Zmierzone: nawet przy komplecie plików funkcja traci 2,6 s na odpytywanie
    # sieci. Po restarcie komputera bywa gorzej, bo karta sieciowa może jeszcze
    # nie mieć połączenia i zapytanie czeka na timeout.
    if _brakuje_modeli():
        logger.info("Brakuje plików modeli — pobieram (jednorazowo)...")
        from openwakeword.utils import download_models

        download_models(model_names=[MODEL_WAKE_WORD])
    else:
        logger.info("Pliki modeli są na miejscu — pomijam sprawdzanie sieci.")

    # inference_framework="onnx" jest tu KONIECZNE.
    # Domyślną wartością biblioteki jest "tflite", ale tflite-runtime nie ma
    # wersji na Windows — bez tego argumentu dostaniesz błąd importu.
    detektor = WakeWordModel(
        wakeword_models=[MODEL_WAKE_WORD],
        inference_framework="onnx",
    )

    logger.info("Detektor gotowy.")
    return detektor


# Fragmenty komunikatów błędów, po których poznajemy problem ze środowiskiem
# CUDA. Sprawdzamy je, żeby zamiast surowego stack trace'u pokazać wskazówkę,
# co konkretnie jest nie tak.
SLOWA_KLUCZE_CUDA = ("cuda", "cudnn", "cublas", "cudart", "gpu", "nvidia", "device")


def _komunikat_bledu_gpu(blad):
    """
    Tłumaczy błąd inicjalizacji modelu na wskazówkę dla człowieka.

    Rozróżniamy dwa najczęstsze przypadki, bo prowadzą do zupełnie różnych
    rozwiązań: brak bibliotek CUDA to problem instalacyjny, a brak pamięci
    na karcie to problem z doborem modelu.
    """
    tresc = str(blad).lower()

    # Brak pamięci ma inne rozwiązanie niż brak bibliotek — nie ma sensu
    # wysyłać po sterowniki kogoś, komu po prostu nie zmieścił się model.
    if "out of memory" in tresc or "cuda_error_out_of_memory" in tresc:
        return (
            f"Za mało pamięci na karcie graficznej dla modelu '{MODEL_WHISPER}'.\n"
            "  Co zrobić (od najprostszego):\n"
            "   1. Zamknij gry, przeglądarkę i inne programy obciążające GPU.\n"
            "   2. Zmień MODEL_WHISPER na 'small' w wake_word_listener.py.\n"
            "   3. Albo wróć na procesor: URZADZENIE = 'cpu', COMPUTE_TYPE = 'int8'."
        )

    if any(slowo in tresc for slowo in SLOWA_KLUCZE_CUDA):
        return (
            "Nie udało się uruchomić Whispera na karcie graficznej.\n"
            "  Najczęstsza przyczyna to brakujące biblioteki CUDA/cuDNN,\n"
            "  których wymaga CTranslate2 (silnik pod spodem faster-whisper).\n"
            "  Co zrobić:\n"
            "   1. Sprawdź, czy karta jest widoczna: uruchom 'nvidia-smi' w konsoli.\n"
            "   2. Doinstaluj biblioteki CUDA 12 i cuDNN 9:\n"
            "      pip install nvidia-cublas-cu12 nvidia-cudnn-cu12\n"
            "   3. Zaktualizuj sterownik NVIDIA, jeśli masz starszy niż 525.\n"
            "   4. Jeśli nie chcesz walczyć z GPU, wróć na procesor:\n"
            "      URZADZENIE = 'cpu' oraz COMPUTE_TYPE = 'int8'.\n"
            f"  Oryginalny błąd: {blad}"
        )

    return f"Nie udało się wczytać modelu Whisper '{MODEL_WHISPER}': {blad}"


def _wczytaj_model_whisper():
    """
    Wczytuje model faster-whisper do pamięci.

    Przy pierwszym uruchomieniu model (~1,5 GB dla "medium") pobierze się z internetu.

    Gdy inicjalizacja na GPU zawiedzie, zamiast surowego stack trace'u
    wypisujemy wskazówkę, co zrobić. Program i tak się zatrzymuje —
    świadomie NIE przełączamy po cichu na procesor, bo Jarvis działa w tle
    i taka podmiana byłaby niewidoczna: zastanawiałbyś się tygodniami,
    czemu rozpoznawanie trwa dziesięć razy dłużej, niż powinno.

    Zwraca: obiekt WhisperModel.
    """
    from faster_whisper import WhisperModel

    logger.info("Wczytuję model Whisper '%s' na %s (%s)...",
                MODEL_WHISPER, URZADZENIE.upper(), COMPUTE_TYPE)

    try:
        # local_files_only=True mówi: "bierz z dysku, nie pytaj internetu".
        #
        # Bez tego faster-whisper przy każdym starcie odpytuje Hugging Face,
        # czy nie ma nowszej wersji modelu. Zmierzone: 3,7 s z tym sprawdzeniem,
        # 1,4 s bez niego. Po restarcie komputera bywa znacznie gorzej —
        # jeśli sieć jeszcze nie wstała, zapytanie czeka na timeout,
        # a Jarvis stoi bezczynnie.
        try:
            model = WhisperModel(
                MODEL_WHISPER,
                device=URZADZENIE,
                compute_type=COMPUTE_TYPE,
                cpu_threads=WATKI_WHISPERA,
                local_files_only=True,
            )
        except Exception:
            # Modelu nie ma jeszcze na dysku — to normalne przy pierwszym
            # uruchomieniu po instalacji. Wtedy (i tylko wtedy) sięgamy do sieci.
            logger.info("Modelu nie ma w cache — pobieram z internetu (jednorazowo)...")
            model = WhisperModel(
                MODEL_WHISPER, device=URZADZENIE, compute_type=COMPUTE_TYPE,
                cpu_threads=WATKI_WHISPERA,
            )
    except Exception as e:
        komunikat = _komunikat_bledu_gpu(e)
        # Każdą linię osobno, żeby w dzienniku zachowały format i wcięcia.
        for linia in komunikat.split("\n"):
            logger.error(linia)
        # "from None" ucina łańcuch wyjątków, więc w konsoli widać czytelną
        # wskazówkę zamiast ściany wewnętrznych wywołań CTranslate2.
        # Pełna treść oryginalnego błędu jest już w dzienniku powyżej.
        raise RuntimeError(komunikat) from None

    # Jednoznaczna informacja o trybie pracy. Jarvis chodzi w tle bez konsoli,
    # więc bez tej linii nie miałbyś jak sprawdzić, czy liczy na procesorze
    # czy na karcie — a to różnica rzędu wielkości w czasie odpowiedzi.
    if URZADZENIE == "cpu":
        logger.info("Whisper działa na CPU (model: %s, %s)", MODEL_WHISPER, COMPUTE_TYPE)
    else:
        logger.info("Whisper działa na GPU (model: %s, %s)", MODEL_WHISPER, COMPUTE_TYPE)

    return model


def _wczytaj_model_potwierdzenia():
    """
    Wczytuje mały model Whispera, którym potwierdzamy niepewne trafienia.

    Wołane LENIWIE — dopiero przy pierwszym podejrzeniu. Jeśli wymawiasz
    wake word wyraźnie i detektor radzi sobie sam, ten model nigdy się
    nie wczyta i nie zajmie pamięci.

    Zwraca: obiekt WhisperModel.
    """
    from faster_whisper import WhisperModel

    logger.info("Wczytuję mały model '%s' do potwierdzania wake worda...",
                MODEL_POTWIERDZENIA)
    try:
        # Jak przy dużym modelu: najpierw z dysku, bez odpytywania sieci.
        return WhisperModel(MODEL_POTWIERDZENIA, device="cpu", compute_type="int8",
                            cpu_threads=2, local_files_only=True)
    except Exception:
        logger.info("Modelu '%s' nie ma w cache — pobieram (jednorazowo, ~40 MB)...",
                    MODEL_POTWIERDZENIA)
        return WhisperModel(MODEL_POTWIERDZENIA, device="cpu", compute_type="int8",
                            cpu_threads=2)


def _bez_ogonkow(tekst):
    """
    "Dżarwiś" -> "dzarwis".

    Whisper zapisuje usłyszane imię raz tak, raz tak — porównywanie liter
    ma sens dopiero wtedy, gdy ogonki i kreski nie robią różnicy.
    """
    rozlozony = unicodedata.normalize("NFD", tekst.lower().replace("ł", "l"))
    return "".join(znak for znak in rozlozony
                   if unicodedata.category(znak) != "Mn")


def _brzmi_jak_jarvis(tekst):
    """
    Czy w tekście padło słowo brzmiące jak "Jarvis"?

    Porównujemy PODOBIEŃSTWO, nie równość, bo Whisper zapisuje to imię
    na kilkanaście sposobów: "Jarwiz", "Dżarvisie", "Arwis". Gotowa lista
    nigdy nie byłaby kompletna, a miara podobieństwa łapie i te formy,
    których nikt nie przewidział.
    """
    for slowo in re.findall(r"[a-z]+", _bez_ogonkow(tekst or "")):
        for wzorzec in WZORCE_JARVIS:
            if difflib.SequenceMatcher(None, slowo, wzorzec).ratio() >= PROG_PODOBIENSTWA:
                return True
    return False


def _potwierdz_whisperem(bufor):
    """
    Sprawdza, czy w ostatnich sekundach dźwięku naprawdę padło "Jarvis".

    bufor — kolejka ramek audio (int16) z ostatnich BUFOR_POTWIERDZENIA_S sekund

    Zwraca: True, jeśli Whisper usłyszał tam imię Jarvisa.
    """
    global _model_potwierdzenia

    if _model_potwierdzenia is None:
        _model_potwierdzenia = _wczytaj_model_potwierdzenia()

    audio = np.concatenate(bufor).astype(np.float32) / 32768.0

    try:
        segmenty, _ = _model_potwierdzenia.transcribe(
            audio,
            language=JEZYK,
            # beam_size=1 to najszybszy tryb. Przy jednym słowie do rozpoznania
            # szukanie lepszych wariantów i tak niczego nie wnosi.
            beam_size=1,
            without_timestamps=True,
            condition_on_previous_text=False,
            temperature=0.0,
            vad_filter=True,
        )
        tekst = " ".join(segment.text for segment in segmenty).strip()
    except Exception:
        # Potwierdzanie to dodatek — jego awaria nie może zatrzymać nasłuchu.
        logger.exception("Potwierdzanie Whisperem zawiodło")
        return False

    trafione = _brzmi_jak_jarvis(tekst)
    logger.info("[POTWIERDZENIE] usłyszałem %r -> %s",
                tekst, "to Jarvis" if trafione else "nie o mnie")
    return trafione


def _czekaj_na_wake_word(pomijaj_gdy_zajety=True, stop=None, ignoruj=None, ogon=None):
    """
    Blokuje działanie programu, dopóki nie usłyszy "Hey Jarvis".

    Parametry są dla wejścia w słowo (WejscieWSlowo niżej) — przy zwykłym
    czuwaniu zostają domyślne:
      pomijaj_gdy_zajety — False: słuchamy także wtedy, gdy Jarvis mówi
      stop               — threading.Event kończący czekanie (Jarvis skończył)
      ignoruj            — funkcja; gdy zwróci True, wykrycie pomijamy
                           (Jarvis sam właśnie wymawia "Jarvis")
      ogon               — lista; po wykryciu dopisujemy do niej ostatnie
                           OGON_PO_WYKRYCIU_S dźwięku (patrz opis stałej)

    Dla każdej 80-milisekundowej porcji audio detektor zwraca słownik
    {nazwa_modelu: pewność 0.0-1.0}. Dalej są dwie drogi:

      WYNIK > PROG_WYKRYCIA        -> reagujemy natychmiast,
      WYNIK > PROG_PODEJRZENIA     -> pytamy Whispera, czy to naprawdę było
                                      "Jarvis" (ok. 0,8 s, patrz opis stałych).

    Druga droga istnieje dlatego, że detektor ocenia brzmienie i przy polskiej
    wymowie bywa bardzo niepewny — potrafi dać poprawnej frazie 0.09.

    Zwraca: True gdy wykryto słowo, False gdy poproszono o zatrzymanie programu
    (albo gdy ustawiono `stop`).
    """
    if pomijaj_gdy_zajety:
        logger.info("[NASŁUCH] Czekam na 'Hey Jarvis'...")

    # Bufor ostatnich sekund dźwięku dla potwierdzania. deque z maxlen sam
    # wyrzuca najstarszą ramkę, więc zużycie pamięci jest stałe.
    bufor = collections.deque(
        maxlen=max(1, round(BUFOR_POTWIERDZENIA_S * SAMPLE_RATE / DLUGOSC_RAMKI))
    )
    ostatnie_potwierdzenie = 0.0
    poczatek_podejrzenia = None
    szczyt_podejrzenia = 0.0

    def sam_sie_wola():
        """Wykrycie na własnym głosie Jarvisa — pomijamy i czyścimy stan."""
        if ignoruj is None or not ignoruj():
            return False
        logger.info("[WEJŚCIE W SŁOWO] Pomijam — to ja sam mówię 'Jarvis'.")
        _detektor.reset()
        bufor.clear()
        return True

    def zapamietaj_ogon():
        if ogon is not None:
            ile = max(1, round(OGON_PO_WYKRYCIU_S * SAMPLE_RATE / DLUGOSC_RAMKI))
            ogon.extend(list(bufor)[-ile:])

    # Warunek pętli sprawdza przełącznik co ~80 ms, więc zamknięcie programu
    # jest natychmiastowe nawet wtedy, gdy Jarvis stoi bezczynnie godzinami.
    while not _zatrzymaj_sie.is_set() and not (stop is not None and stop.is_set()):
        # Czytamy dokładnie tyle próbek, ile detektor oczekuje w jednej porcji.
        dane, _ = _stream.read(DLUGOSC_RAMKI)
        # Mikrofon zwraca kształt (n, 1) — spłaszczamy do zwykłej listy próbek.
        audio = dane.flatten()

        # Gdy Jarvis sam teraz mówi (np. wypowiada przypomnienie), mikrofon
        # słyszy jego głos z głośników. Pomijamy te ramki, żeby nie obudził
        # sam siebie i nie liczył własnych słów jako Twoich.
        if pomijaj_gdy_zajety and not zajetosc.czy_wolny():
            bufor.clear()
            poczatek_podejrzenia = None
            continue

        bufor.append(audio)

        # Bierzemy najwyższy wynik zamiast szukać po nazwie klucza — nazwa modelu
        # w słowniku bywa wersjonowana ("hey_jarvis_v0.1"), więc tak jest odporniej.
        wynik = max(_detektor.predict(audio).values())

        if wynik > PROG_WYKRYCIA:
            if sam_sie_wola():
                continue
            logger.info("[WYKRYTO] pewność %.3f", wynik)
            zapamietaj_ogon()
            # reset() czyści wewnętrzny bufor detektora. Bez tego przez chwilę
            # pamiętałby świeże wykrycie i po powrocie odpalałby się od razu ponownie.
            _detektor.reset()
            return True

        teraz = time.monotonic()

        if wynik > PROG_PODEJRZENIA:
            # Podejrzenie trwa. Nie wołamy Whispera od razu: ocena detektora
            # narasta z każdą sylabą i za moment może przekroczyć próg pewności,
            # a wtedy odpowiedź będzie natychmiastowa i za darmo.
            if poczatek_podejrzenia is None:
                poczatek_podejrzenia = teraz
                szczyt_podejrzenia = wynik
            else:
                szczyt_podejrzenia = max(szczyt_podejrzenia, wynik)

            if teraz - poczatek_podejrzenia < OPOZNIENIE_POTWIERDZENIA_S:
                continue
        elif poczatek_podejrzenia is None:
            # Zwykła cisza albo mowa, w której nic nie przypomina wake worda.
            continue

        # Tu docieramy w dwóch przypadkach: podejrzany dźwięk właśnie ucichł
        # albo trwa dłużej niż zwłoka. W obu czas zapytać Whispera.
        podejrzenie = szczyt_podejrzenia
        poczatek_podejrzenia = None

        # Odstęp chroni procesor: bez niego dźwięk balansujący w okolicach
        # progu odpalałby Whispera kilka razy na sekundę.
        if (len(bufor) < bufor.maxlen
                or teraz - ostatnie_potwierdzenie < ODSTEP_POTWIERDZEN_S):
            continue
        ostatnie_potwierdzenie = teraz
        if sam_sie_wola():
            continue

        logger.info("[PODEJRZENIE] pewność %.3f — sprawdzam Whisperem...", podejrzenie)
        if _potwierdz_whisperem(bufor):
            zapamietaj_ogon()
            _detektor.reset()
            return True

    return False


def _nagraj(sekundy=CZAS_NAGRANIA):
    """
    Nagrywa zadaną liczbę sekund audio z otwartego strumienia mikrofonu.

    Czytamy porcjami po 80 ms zamiast jednym blokiem, żeby dało się przerwać
    nagrywanie w trakcie, gdy program jest zamykany.

    Zwraca: tablicę numpy float32 (-1.0..1.0) — format, którego oczekuje Whisper.
    Zwraca pustą tablicę, jeśli nagrywanie zostało przerwane.
    """
    logger.info("[NAGRYWAM] Mów teraz (%s s)...", sekundy)

    liczba_porcji = int(SAMPLE_RATE * sekundy / DLUGOSC_RAMKI)
    porcje = []

    for _ in range(liczba_porcji):
        if _zatrzymaj_sie.is_set():
            return np.array([], dtype=np.float32)
        dane, _ = _stream.read(DLUGOSC_RAMKI)
        porcje.append(dane.flatten())

    logger.info("[NAGRYWAM] Koniec nagrania, rozpoznaję...")

    # Mikrofon daje int16 (-32768..32767), a Whisper chce float32 (-1.0..1.0).
    return np.concatenate(porcje).astype(np.float32) / 32768.0


# Whisper jest jeden, a korzystać z niego mogą dwa wątki naraz: nasłuch
# mikrofonu i most do Telegrama (głosówki z telefonu). Blokada ustawia je
# w kolejce. RLock, bo _zapewnij_model_whispera() i _rozpoznaj_mowe()
# biorą ją po sobie w tym samym wątku.
_blokada_whispera = threading.RLock()


def _zapewnij_model_whispera():
    """Wczytuje model Whispera, jeśli jeszcze go nie ma — dokładnie raz."""
    global _model_whisper
    with _blokada_whispera:
        if _model_whisper is None:
            _model_whisper = _wczytaj_model_whisper()


def przepisz_nagranie(audio):
    """
    Przepisuje GOTOWE nagranie na tekst — np. głosówkę z Telegrama.

    audio — tablica float32, 16 kHz, mono (-1.0..1.0)

    W przeciwieństwie do reszty tego modułu nie dotyka mikrofonu, więc
    działa nawet wtedy, gdy mikrofonu nie ma. Bezpieczne z dowolnego wątku.

    Zwraca: rozpoznany tekst (pusty, jeśli nic nie rozpoznano).
    """
    _zapewnij_model_whispera()
    tekst, _jezyk, _pewnosc = _rozpoznaj_mowe(audio, filtruj_cisze=True)
    return tekst


def _rozpoznaj_mowe(audio, filtruj_cisze=True):
    """
    Zamienia nagranie na tekst.

    filtruj_cisze — czy Whisper ma sam odsiewać ciszę. W trybie rozmowy
                    dajemy False, bo nagranie jest już przycięte naszym
                    własnym wykrywaczem mowy, a filtr potrafi wtedy zjeść
                    początek albo koniec krótkiej wypowiedzi. Zmierzone:
                    "Dzięki." z filtrem wychodziło jako "Genki.",
                    a "Ciszej." jako "Ciszyj."; bez filtra oba poprawnie.
                    Na ciszy i szumie Whisper i tak nic nie zmyślił.

    Zwraca: (tekst, kod_języka, pewność_języka).
    """
    # vad_filter odsiewa fragmenty ciszy — dzięki temu Whisper nie "zmyśla"
    # słów tam, gdzie nic nie powiedziałeś (częsty problem przy stałym czasie nagrania).
    #
    # Pozostałe parametry to wyciskanie czasu. Zmierzone na 5-sekundowych
    # nagraniach (small, CPU, int8), średnia z trzech zdań:
    #
    #     bez nich                      10,0 s
    #     bez znaczników czasu           9,6 s
    #     + bez kontekstu, temp. stała   9,5 s
    #     + WYMUSZONY JĘZYK              4,9 s   <- tu jest cały zysk
    #
    # Reszta to drobiazgi, ale JEZYK zmienia wszystko: bez niego Whisper
    # najpierw uruchamia osobne rozpoznawanie języka, a dopiero potem
    # transkrybuje. To dosłownie podwaja pracę przy krótkiej komendzie.
    #
    # beam_size zostaje 5 — przy wymuszonym języku różnica między 1 a 5
    # mieści się w błędzie pomiaru, więc nie ma po co oddawać dokładności.
    #
    # hotwords to słownik nazw własnych, które pewnie padną: Twoi wykonawcy,
    # albumy, aplikacje (slownik.py — tam też pomiary). Bez niego "Kaz Bałagane"
    # wychodziło jako "kazba łagany". W faster-whisper hotwords i initial_prompt
    # trafiają w to samo miejsce modelu, ale hotwords działa w każdym
    # 30-sekundowym oknie nagrania, a initial_prompt tylko w pierwszym — przy
    # dłuższej głosówce z Telegrama to różnica.
    try:
        podpowiedz = slownik.podpowiedzi() or None
    except Exception:
        # Słownik to dodatek — jego awaria nie może wyłączyć rozpoznawania mowy.
        logger.exception("Słownik podpowiedzi zawiódł — rozpoznaję bez niego")
        podpowiedz = None
    with _blokada_whispera:
        segmenty, info = _model_whisper.transcribe(
            audio,
            beam_size=5,
            vad_filter=filtruj_cisze,
            language=JEZYK,
            # Znaczniki czasu to dodatkowe tokeny do wygenerowania, a my i tak
            # bierzemy sam tekst.
            without_timestamps=True,
            # Bez tego Whisper doklejał do zapytania własną poprzednią transkrypcję
            # "dla kontekstu". Przy osobnych komendach to tylko zaszumia wynik.
            condition_on_previous_text=False,
            # Domyślnie po nieudanej próbie Whisper powtarza dekodowanie z wyższą
            # temperaturą. Przy krótkich komendach te powtórki kosztują więcej,
            # niż dają.
            temperature=0.0,
            hotwords=podpowiedz,
        )

        # transcribe() zwraca generator — tekst powstaje dopiero tutaj, przy
        # łączeniu segmentów. Dlatego łączenie też musi być pod blokadą.
        tekst = " ".join(segment.text.strip() for segment in segmenty).strip()

    return tekst, info.language, info.language_probability


# Nagrania, z których Whisper nie wydobył ani słowa, zapisujemy na dysk.
# Dzięki temu da się ich POSŁUCHAĆ i rozstrzygnąć, co zawiodło: czy mikrofon
# nagrał szept, czy Whisper nie poradził sobie ze zrozumiałą wypowiedzią.
# Bez tego oba przypadki wyglądają w dzienniku identycznie.
#
# Pliki zostają wyłącznie na tym komputerze — nic nie jest nigdzie wysyłane.
# Folder jest w .gitignore, a najstarsze nagrania kasują się same.
# Żeby wyłączyć zapisywanie, ustaw poniżej False.
ZAPISUJ_NIEROZPOZNANE = True
KATALOG_NIEROZPOZNANYCH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "nierozpoznane")
LIMIT_NIEROZPOZNANYCH = 10


def _zapisz_nierozpoznane(audio, glosnosc):
    """Zapisuje nagranie bez rozpoznanych słów do folderu diagnostycznego."""
    if not ZAPISUJ_NIEROZPOZNANE:
        return

    try:
        os.makedirs(KATALOG_NIEROZPOZNANYCH, exist_ok=True)
        # Głośność w nazwie pliku, żeby dało się jednym spojrzeniem odróżnić
        # ciche nagrania (problem z mikrofonem) od głośnych (problem z mową).
        nazwa = (datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                 + f"_{glosnosc:.0f}dBFS.wav")
        sciezka = os.path.join(KATALOG_NIEROZPOZNANYCH, nazwa)

        with wave.open(sciezka, "wb") as plik:
            plik.setnchannels(1)
            plik.setsampwidth(2)     # 16 bitów na próbkę
            plik.setframerate(SAMPLE_RATE)
            plik.writeframes((np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16).tobytes())

        # Zostawiamy tylko kilka najnowszych — folder nie może rosnąć bez końca.
        nagrania = sorted(os.listdir(KATALOG_NIEROZPOZNANYCH))
        for stare in nagrania[:-LIMIT_NIEROZPOZNANYCH]:
            os.remove(os.path.join(KATALOG_NIEROZPOZNANYCH, stare))

        logger.info("[ROZMOWA] Nagranie do sprawdzenia: %s", sciezka)
    except Exception:
        # Diagnostyka nie może przeszkadzać w działaniu.
        logger.exception("Nie udało się zapisać nierozpoznanego nagrania")


def _oproznij_bufor():
    """
    Wyrzuca audio, które nazbierało się w buforze mikrofonu podczas transkrypcji.

    Bez tego detektor po powrocie do nasłuchu przetwarzałby najpierw kilka sekund
    starego dźwięku (m.in. Twoją własną komendę) i mógłby od razu odpalić się ponownie.
    """
    dostepne = _stream.read_available
    if dostepne > 0:
        _stream.read(dostepne)


def sluchaj_komendy(callback_stanu=None):
    """
    GŁÓWNE WEJŚCIE TEGO MODUŁU — to woła main.py.

    Blokuje wykonanie do momentu, aż usłyszy "Hey Jarvis", potem nagrywa
    CZAS_NAGRANIA sekund i transkrybuje nagranie.

    callback_stanu — opcjonalna funkcja przyjmująca nazwę stanu jako string.
        Wołana, gdy zmienia się etap pracy: "listening" (nagrywam)
        i "processing" (transkrybuję). Służy do sterowania animacją w GUI.

        Zwróć uwagę, że ten moduł NIE importuje gui.py i nic o nim nie wie —
        dostaje po prostu jakąś funkcję i woła ją ze stringiem. Dzięki temu
        nasłuch działa tak samo z interfejsem graficznym i bez niego,
        a testowanie go nie wymaga uruchamiania okienka.

    Zwraca: rozpoznany tekst (string). Pusty string, jeśli nic nie usłyszał
    albo jeśli program jest zamykany.
    """

    def zglos(stan):
        """Powiadamia GUI o etapie — o ile ktokolwiek nas o to prosił."""
        if callback_stanu is not None:
            callback_stanu(stan)

    _przygotuj(callback_stanu)

    # Mikrofon mógł zniknąć w trakcie czekania — wtedy odczyt rzuca wyjątkiem.
    # Łapiemy go i oddajemy pusty tekst: pętla w main.py zawoła nas ponownie,
    # a _przygotuj() zobaczy brak strumienia i zacznie szukać mikrofonu.
    try:
        # Uwaga: NIE zgłaszamy tu "idle". Czekanie na wake word to stan domyślny,
        # a main.py ustawia go sam po wykonaniu komendy. Gdybyśmy zgłaszali "idle"
        # na starcie każdego cyklu, skasowalibyśmy czerwony błysk po poprzednim
        # błędzie — pętla wraca tu w kilka milisekund po jego zapaleniu,
        # więc praktycznie nigdy nie zdążyłbyś go zobaczyć.
        if not _czekaj_na_wake_word():
            return ""  # program jest zamykany

        logger.info("[WYKRYTO] Usłyszałem 'Hey Jarvis'!")

        # Od tej chwili mikrofon jest "nasz": przypomnienie z reminders.py
        # poczeka ze swoim głosem, zamiast nagrać się razem z Twoją komendą
        # (opis w zajetosc.py).
        with zajetosc.zajmij("nagrywanie komendy"):
            zglos("listening")
            audio = _nagraj()

            if audio.size == 0:
                return ""  # nagrywanie przerwane przez zamykanie programu

            zglos("processing")
            tekst, jezyk, pewnosc = _rozpoznaj_mowe(audio)

            # Czyścimy bufor dopiero teraz, po transkrypcji.
            _oproznij_bufor()
    except sd.PortAudioError as e:
        _mikrofon_utracony(e)
        return ""

    if tekst:
        logger.info("[TEKST] (%s, %.0f%%) %s", jezyk, pewnosc * 100, tekst)
    else:
        logger.info("[TEKST] Nic nie usłyszałem.")

    return tekst


# --- Nasłuch rozmowy (bez wake worda) -----------------------------------------

# Próg, powyżej którego VAD uznaje ramkę za mowę (0.0-1.0).
# Pomiar na nagraniu testowym: cisza dawała 0.04, mowa dochodziła do 1.00,
# więc 0.5 leży w bardzo szerokiej dolinie między jednym a drugim.
PROG_VAD = 0.5

# Ile ciszy kończy wypowiedź. Uwaga: to NIE może być zbyt mało — w środku
# normalnego zdania są naturalne pauzy. W pomiarze na zdaniu testowym pauza
# między członami trwała ok. 0.25 s, więc 1.2 s daje spory zapas i nie utnie
# Ci wypowiedzi w połowie, gdy zawahasz się nad słowem.
CISZA_KONCZACA_S = 1.2

# Bezpiecznik na wypadek, gdyby VAD zaciął się na "ciągle mowa" (np. przy
# głośnym telewizorze w tle) — po tylu sekundach kończymy nagranie tak czy owak.
MAX_WYPOWIEDZ_S = 20

# Ile dźwięku SPRZED wykrycia mowy dokładamy do nagrania. VAD potrzebuje
# ułamka sekundy, żeby się zorientować, więc bez tego bufora pierwsza głoska
# regularnie ginęła. Trzymamy stale ostatnie pół sekundy i doklejamy je z przodu.
PRE_BUFOR_S = 0.5

# VAD analizuje ramki po 640 próbek (40 ms). Nasza ramka z mikrofonu ma 1280,
# czyli dokładnie dwie takie — a to warunek konieczny, bo predict() wymaga
# długości będącej wielokrotnością frame_size.
RAMKA_VAD = 640

_vad = None


def _przygotuj_vad():
    """
    Tworzy detektor mowy (Silero VAD) przy pierwszym użyciu.

    Model przyszedł razem z openWakeWord — download_models() pobiera go
    zawsze, niezależnie od wybranego słowa-klucza. Nie ma więc nic
    do doinstalowania ani do pobrania.
    """
    global _vad

    if _vad is None:
        _odetnij_scikit_learn()

        from openwakeword.vad import VAD

        _vad = VAD()
        logger.info("Detektor mowy (VAD) gotowy.")

    return _vad


def sluchaj_bez_wake_worda(orb_callback=None, limit_ciszy_s=8):
    """
    Nasłuch w trakcie rozmowy, bez "Hey Jarvis". Pełny opis zachowania
    i zwracanych wartości — przy _sluchaj_bez_wake_worda() poniżej.

    Ta cienka warstwa dokłada jedno: obsługę mikrofonu, który zniknął
    w trakcie rozmowy (rozładowane słuchawki, wyjście poza zasięg).
    Wtedy odczyt z mikrofonu rzuca wyjątkiem — zamiast wysypywać cały
    wątek, kończymy rozmowę, a szukaniem mikrofonu zajmie się _przygotuj()
    przy następnym czuwaniu.
    """
    try:
        return _sluchaj_bez_wake_worda(orb_callback, limit_ciszy_s)
    except sd.PortAudioError as e:
        _mikrofon_utracony(e)
        return None


def _sluchaj_bez_wake_worda(orb_callback=None, limit_ciszy_s=8, po_przerwaniu=False,
                            poczatek=()):
    """
    Nasłuchuje BEZ wymagania "Hey Jarvis" — to tryb trwającej rozmowy.

    orb_callback  — funkcja przyjmująca nazwę stanu (jak w sluchaj_komendy)
    limit_ciszy_s — ile czekamy na to, aż zaczniesz mówić, zanim uznamy
                    rozmowę za skończoną
    po_przerwaniu — True, gdy właśnie wszedłeś Jarvisowi w słowo (WejscieWSlowo).
                    Wtedy NIE wyrzucamy zaległego dźwięku z mikrofonu — to
                    początek Twojego nowego polecenia — i nie patrzymy na
                    zajetosc: usta i uszy trzyma przerywana właśnie wymiana
                    zdań, a czekanie na nią byłoby czekaniem na samych siebie.
    poczatek      — ramki sprzed startu nagrywania, doklejane na jego początek
                    (przy wejściu w słowo: OGON_PO_WYKRYCIU_S)

    Różnica wobec sluchaj_komendy() jest dwojaka:

      1. Nie ma wake worda — mikrofon jest "otwarty" od razu, bo rozmowa
         już trwa i powtarzanie "Hey Jarvis" przy każdym zdaniu byłoby męczące.
      2. Długość nagrania nie jest sztywna. Nagrywamy, dopóki mówisz,
         i kończymy po CISZA_KONCZACA_S ciszy. Przy rozmowie zdania mają
         różną długość — sztywne 5 sekund albo ucinałoby dłuższe pytania,
         albo kazałoby czekać po krótkich.

    Świadomie NIE zgłaszamy tu stanu "idle" na czas czekania. Stan sprzed
    wywołania (np. czerwony błysk po nieudanej komendzie) ma zdążyć się pokazać
    — gui.py sam wróci z niego do idle po chwili.

    Zwraca jedną z trzech rzeczy i warto je rozróżniać:
        "jakiś tekst" — usłyszał i rozpoznał wypowiedź
        ""            — coś było słychać, ale bez słów (kaszlnięcie, hałas);
                        rozmowa TRWA, po prostu słuchamy dalej
        None          — cisza przez cały limit_ciszy_s albo zamykanie programu;
                        to jedyny sygnał "koniec rozmowy"
    """

    def zglos(stan):
        if orb_callback is not None:
            orb_callback(stan)

    _przygotuj(orb_callback)
    vad = _przygotuj_vad()

    # Stan VAD-a jest ciągły między wywołaniami (to sieć rekurencyjna),
    # więc przed każdą nową wypowiedzią zaczynamy od czystego licznika.
    vad.reset_states()
    if not po_przerwaniu:
        _oproznij_bufor()

    logger.info("[ROZMOWA] Słucham dalej, bez wake worda (max %s s ciszy)...", limit_ciszy_s)

    ramek_na_sekunde = SAMPLE_RATE / DLUGOSC_RAMKI
    limit_czekania = int(limit_ciszy_s * ramek_na_sekunde)
    limit_ciszy_konczacej = int(CISZA_KONCZACA_S * ramek_na_sekunde)
    limit_nagrania = int(MAX_WYPOWIEDZ_S * ramek_na_sekunde)
    dlugosc_pre_bufora = max(1, int(PRE_BUFOR_S * ramek_na_sekunde))

    from collections import deque
    pre_bufor = deque(maxlen=dlugosc_pre_bufora)

    ramek_czekania = 0

    # Faza 1: czekamy, aż ktoś się odezwie.
    while not _zatrzymaj_sie.is_set():
        dane, _ = _stream.read(DLUGOSC_RAMKI)
        pcm = dane.flatten()

        # Gdy Jarvis sam mówi (przypomnienie), mikrofon słyszy jego głos
        # z głośników — nie bierzemy tego za Twoją wypowiedź.
        if not po_przerwaniu and not zajetosc.czy_wolny():
            pre_bufor.clear()
            vad.reset_states()
            continue

        pre_bufor.append(pcm)
        if float(vad.predict(pcm, frame_size=RAMKA_VAD)) > PROG_VAD:
            break

        ramek_czekania += 1
        if ramek_czekania >= limit_czekania:
            logger.info("[ROZMOWA] Cisza przez %s s — kończę sesję rozmowy.",
                        limit_ciszy_s)
            return None

    if _zatrzymaj_sie.is_set():
        return None

    # Faza 2: ktoś mówi — nagrywamy, aż zapadnie cisza. Na ten czas zajmujemy
    # mikrofon, żeby przypomnienie nie odezwało się w środku Twojego zdania.
    # (Po przerwaniu zajmuje go już przerywana wymiana zdań — opis wyżej.)
    with contextlib.nullcontext() if po_przerwaniu else zajetosc.zajmij("nagrywanie rozmowy"):
        zglos("listening")
        logger.info("[ROZMOWA] Słyszę mowę, nagrywam...")
        return _dokoncz_wypowiedz(list(poczatek) + list(pre_bufor), vad, zglos,
                                  limit_ciszy_konczacej, limit_nagrania)


def _dokoncz_wypowiedz(porcje, vad, zglos, limit_ciszy_konczacej, limit_nagrania):
    """
    Druga połowa _sluchaj_bez_wake_worda(): nagrywa do ciszy i rozpoznaje.

    porcje — to, co już nagraliśmy, łącznie z buforem sprzed wykrycia mowy
             (żeby nie zgubić pierwszej głoski)

    Wydzielona, bo cały ten fragment musi się zmieścić w jednym bloku
    `with zajetosc.zajmij(...)` — od pierwszego słowa do gotowego tekstu.

    Zwraca to samo co _sluchaj_bez_wake_worda(): tekst, "" albo None.
    """
    ramek_ciszy = 0

    while not _zatrzymaj_sie.is_set():
        dane, _ = _stream.read(DLUGOSC_RAMKI)
        pcm = dane.flatten()
        porcje.append(pcm)

        if float(vad.predict(pcm, frame_size=RAMKA_VAD)) > PROG_VAD:
            ramek_ciszy = 0
        else:
            ramek_ciszy += 1
            if ramek_ciszy >= limit_ciszy_konczacej:
                break

        if len(porcje) >= limit_nagrania:
            logger.warning("[ROZMOWA] Wypowiedź dłuższa niż %s s — ucinam.",
                           MAX_WYPOWIEDZ_S)
            break

    if _zatrzymaj_sie.is_set():
        return None

    if not porcje:
        return None

    zglos("processing")

    audio = np.concatenate(porcje).astype(np.float32) / 32768.0

    # Głośność nagrania w dBFS: 0 to maksimum, -60 to szept na granicy słyszalności.
    # Przy pustej transkrypcji to pierwsza rzecz, którą warto sprawdzić w dzienniku:
    # cicha wypowiedź wygląda w nim tak samo jak każda inna, dopóki nie zmierzymy.
    glosnosc = 20 * np.log10(max(float(np.sqrt(np.mean(audio ** 2))), 1e-6))
    logger.info("[ROZMOWA] Nagrałem %.1f s (głośność %.0f dBFS), rozpoznaję...",
                len(audio) / SAMPLE_RATE, glosnosc)

    # Nagranie jest już przycięte naszym wykrywaczem mowy — patrz _rozpoznaj_mowe().
    tekst, jezyk, pewnosc = _rozpoznaj_mowe(audio, filtruj_cisze=False)
    _oproznij_bufor()

    if tekst:
        logger.info("[ROZMOWA] (%s, %.0f%%) %s", jezyk, pewnosc * 100, tekst)
        return tekst

    _zapisz_nierozpoznane(audio, glosnosc)

    # VAD usłyszał dźwięk, ale Whisper nie wydobył z niego słów — to najczęściej
    # kaszlnięcie, trzaśnięcie drzwiami albo muzyka w tle.
    #
    # Zwracamy PUSTY STRING, nie None, i ta różnica jest tu istotna:
    #   None — cisza przez cały limit, czyli "nikogo nie ma, kończymy"
    #   ""   — coś było słychać, tylko bez słów, czyli "słucham dalej"
    #
    # Wcześniej oba przypadki kończyły rozmowę i przez to jedno kichnięcie
    # odsyłało Jarvisa z powrotem do czekania na "Hey Jarvis".
    logger.info("[ROZMOWA] Dźwięk bez rozpoznanych słów.")
    return ""


# --- Wejście w słowo: "Hey Jarvis" w trakcie odpowiedzi -----------------------

class WejscieWSlowo:
    """
    Nasłuch "Hey Jarvis" przez cały czas, gdy Jarvis myśli i mówi.

    Dotąd mikrofon w tym czasie w ogóle nie słuchał — żeby Jarvis nie usłyszał
    sam siebie. Teraz słucha, i to w osobnym wątku, bo wątek główny jest wtedy
    zajęty mówieniem. Po wykryciu:

      1. woła `na_wykrycie` — main.py ucisza wtedy głośniki i każe agentowi
         przestać generować,
      2. OD RAZU, w tym samym wątku, nagrywa Twoje nowe polecenie i je
         rozpoznaje. Nie oddajemy mikrofonu z powrotem wątkowi głównemu,
         bo zanim ten skończy sprzątać po przerwanej odpowiedzi, minęłoby
         pół sekundy — a to akurat pierwsze słowa polecenia.

    Mikrofon czyta w danej chwili tylko jeden wątek: ten tutaj od start() do
    zakoncz() albo do końca nagrania. main.py sięga po mikrofon dopiero potem.


    CZY JARVIS NIE PRZERWIE SAM SIEBIE?
    ===================================

    Bez tłumienia echa mikrofon słyszy głośniki. Zmierzone 26.09 na głosie
    Jarvisa puszczonym przez detektor: zwykłe zdania dają 0,000 — także
    "Jestem Jarvis". Odpala dopiero "Hej, tu Jarvis" (0,397). Dlatego gdy
    właśnie odtwarzane zdanie brzmi jak "Jarvis" (`co_mowie`), wykrycia
    pomijamy. "Hej Jarvis" innym głosem na tle mówiącego Jarvisa dawało
    0,26-0,45, czyli ponad próg — wejście w słowo działa nawet bez słuchawek.
    """

    def __init__(self, na_wykrycie, co_mowie=None, callback_stanu=None, limit_ciszy_s=8):
        self._na_wykrycie = na_wykrycie
        self._co_mowie = co_mowie or (lambda: "")
        self._callback_stanu = callback_stanu
        self._limit_ciszy_s = limit_ciszy_s
        self._stop = threading.Event()
        self._wykryto = threading.Event()
        self._polecenie = None
        self._watek = None

    def start(self):
        """Zaczyna nasłuch. Bez mikrofonu po prostu nic nie robi."""
        if _stream is None or _detektor is None:
            return
        self._watek = threading.Thread(target=self._praca, name="watek-wejscia-w-slowo",
                                       daemon=True)
        self._watek.start()

    def _praca(self):
        try:
            # Detektor pamięta ostatnie ramki sprzed tej odpowiedzi — zaczynamy czysto.
            _detektor.reset()
            ogon = []
            if not _czekaj_na_wake_word(pomijaj_gdy_zajety=False, stop=self._stop,
                                        ignoruj=self._sam_sie_wolam, ogon=ogon):
                return
            self._wykryto.set()
            logger.info("[WEJŚCIE W SŁOWO] Usłyszałem 'Hey Jarvis' — milknę i słucham.")
            self._na_wykrycie()
            self._polecenie = _sluchaj_bez_wake_worda(
                self._callback_stanu, self._limit_ciszy_s, po_przerwaniu=True, poczatek=ogon)
        except sd.PortAudioError as e:
            _mikrofon_utracony(e)
        except Exception:
            logger.exception("[WEJŚCIE W SŁOWO] Nasłuch w trakcie odpowiedzi zawiódł")

    def _sam_sie_wolam(self):
        return _brzmi_jak_jarvis(self._co_mowie())

    def zakoncz(self):
        """
        Odpowiedź się skończyła — kończymy czuwanie, jeśli nikt nie przerwał.

        Zwraca: True, jeśli przerwano (wtedy nagrywanie polecenia trwa dalej
        i trzeba po nie sięgnąć przez polecenie()).
        """
        self._stop.set()
        if self._watek is not None and not self._wykryto.is_set():
            # Najwyżej jedna ramka mikrofonu albo jedno potwierdzenie Whisperem.
            self._watek.join(timeout=5)
            if self._watek.is_alive() and not self._wykryto.is_set():
                logger.warning("[WEJŚCIE W SŁOWO] Nasłuch nie zakończył się w 5 s.")
        return self._wykryto.is_set()

    def polecenie(self):
        """
        Polecenie wypowiedziane po przerwaniu — czeka, aż zostanie nagrane.

        Zwraca to samo co sluchaj_bez_wake_worda(): tekst, "" (hałas bez słów)
        albo None (po "Hey Jarvis" zapadła cisza).
        """
        if self._watek is not None:
            self._watek.join()
        return self._polecenie


def zamknij():
    """
    Zamyka strumień mikrofonu. Wołane automatycznie przy końcu programu (atexit),
    ale możesz je wywołać ręcznie, jeśli chcesz zwolnić mikrofon wcześniej.

    Uwaga na kolejność: najpierw zatrzymaj(), potem poczekaj aż wątek nasłuchu
    skończy, i dopiero wtedy zamknij(). Zamknięcie strumienia, z którego inny
    wątek właśnie czyta, potrafi wysypać program.
    """
    global _stream

    if _stream is not None:
        _stream.stop()
        _stream.close()
        _stream = None
        logger.info("Mikrofon zwolniony.")


def kalibracja(ile_prob=5):
    """
    Sprawdza, jak Twoja wymowa "Hey Jarvis" wypada na tle progów.

    Uruchom: python wake_word_listener.py kalibracja

    Po każdej próbie wypisuje najwyższą ocenę detektora i mówi, czy
    wystarczyłaby do natychmiastowej reakcji, czy Jarvis musiałby
    dopytywać Whispera. Na końcu podpowiada próg dopasowany do Ciebie.
    """
    _przygotuj()
    print("\nPowiedz 'Hey Jarvis' po każdym sygnale. Ctrl+C przerywa.\n")

    wyniki = []
    for numer in range(1, ile_prob + 1):
        print(f"  Próba {numer}/{ile_prob} — mów teraz...", end="", flush=True)

        najlepszy = 0.0
        _detektor.reset()
        # 3 sekundy na jedną próbę.
        for _ in range(int(3 * SAMPLE_RATE / DLUGOSC_RAMKI)):
            dane, _ = _stream.read(DLUGOSC_RAMKI)
            najlepszy = max(najlepszy, max(_detektor.predict(dane.flatten()).values()))

        wyniki.append(najlepszy)
        if najlepszy > PROG_WYKRYCIA:
            ocena = "reakcja natychmiastowa"
        elif najlepszy > PROG_PODEJRZENIA:
            ocena = "potwierdzenie Whisperem (ok. 1 s później)"
        else:
            ocena = "NIE WYKRYTO — spróbuj wymówić 'Hej Dżarwis'"
        print(f"  ocena {najlepszy:.3f} -> {ocena}")

    print(f"\nNajsłabsza próba: {min(wyniki):.3f}, najlepsza: {max(wyniki):.3f}")
    print(f"Progi w kodzie: wykrycie {PROG_WYKRYCIA}, podejrzenie {PROG_PODEJRZENIA}")
    if min(wyniki) < PROG_PODEJRZENIA:
        print("Twoje najsłabsze próby są poniżej progu podejrzenia — obniż "
              "PROG_PODEJRZENIA w wake_word_listener.py.")
    elif min(wyniki) < PROG_WYKRYCIA:
        print(f"Chcesz reagować od razu, bez czekania? Ustaw PROG_WYKRYCIA "
              f"na {max(0.02, min(wyniki) * 0.8):.2f}.")
    else:
        print("Wszystkie próby są powyżej progu — Jarvis reaguje od razu.")
    zamknij()


# --- Test samego modułu: `python wake_word_listener.py` ---
# Pełnego Jarvisa uruchamiasz przez `python main.py` — tutaj sprawdzasz tylko,
# czy mikrofon, wake word i transkrypcja działają.
#
# `python wake_word_listener.py kalibracja` sprawdza samo słowo aktywujące.
if __name__ == "__main__":
    from logging_setup import skonfiguruj_logowanie

    skonfiguruj_logowanie()

    if "kalibracja" in sys.argv:
        try:
            kalibracja()
        except KeyboardInterrupt:
            zatrzymaj()
            zamknij()
        raise SystemExit

    logger.info("Dostępne urządzenia wejściowe:")
    for i, urzadzenie in enumerate(sd.query_devices()):
        if urzadzenie["max_input_channels"] > 0:
            logger.info("  [%d] %s", i, urzadzenie["name"])

    try:
        while True:
            sluchaj_komendy()
    except KeyboardInterrupt:
        logger.info("Zatrzymuję nasłuch.")
        zatrzymaj()
        zamknij()
