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
import logging
import threading

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
from openwakeword.model import Model as WakeWordModel
from openwakeword.utils import download_models

# Logger nazwany jak moduł — w jarvis.log widać wtedy, że linia przyszła stąd.
# Konfiguracją (dokąd zapisywać) zajmuje się main.py, nie ten plik.
logger = logging.getLogger(__name__)

# --- Ustawienia, które najczęściej będziesz chciał zmieniać ---

# Wbudowany model openWakeWord. Uwaga: to fraza "HEY Jarvis", nie samo "Jarvis".
# Inne gotowe opcje: "alexa", "hey_mycroft", "hey_rhasspy".
MODEL_WAKE_WORD = "hey_jarvis"

# Próg pewności 0.0-1.0, powyżej którego uznajemy słowo za wykryte.
# 0.5 to rozsądny start. Podnieś (np. 0.7), jeśli odpala się samo z siebie;
# obniż (np. 0.3), jeśli musisz powtarzać frazę kilka razy.
PROG_WYKRYCIA = 0.5

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

# Whisper pracuje na 16 kHz i openWakeWord też — dlatego jeden strumień obsługuje oba.
SAMPLE_RATE = 16000

# openWakeWord analizuje audio w porcjach po 80 ms = 1280 próbek przy 16 kHz.
# To wartość wymuszona przez architekturę modelu, nie dowolny wybór.
DLUGOSC_RAMKI = 1280


# --- Zasoby współdzielone między wywołaniami sluchaj_komendy() ---
#
# Detektor, model Whispera i strumień mikrofonu tworzymy RAZ i trzymamy tutaj.
# Gdyby main.py budował je przy każdej komendzie, każda wypowiedź kosztowałaby
# kilka sekund ładowania modelu i ponowne otwieranie urządzenia audio.
_detektor = None
_model_whisper = None
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


def _przygotuj():
    """
    Leniwa inicjalizacja: przy pierwszym wywołaniu tworzy detektor, model i strumień.
    Przy kolejnych nie robi nic.

    Dzięki temu main.py nie musi pamiętać o żadnym "setupie" — po prostu woła
    sluchaj_komendy(), a moduł sam się przygotowuje, kiedy jest pierwszy raz potrzebny.
    """
    global _detektor, _model_whisper, _stream

    if _stream is not None:
        return

    _detektor = _utworz_detektor()
    _model_whisper = _wczytaj_model_whisper()

    # dtype="int16", bo tego formatu oczekuje openWakeWord.
    # blocksize = DLUGOSC_RAMKI daje najniższe opóźnienie przy wykrywaniu słowa.
    _stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=DLUGOSC_RAMKI,
    )
    _stream.start()
    logger.info("Mikrofon otwarty: %s", sd.query_devices(kind="input")["name"])

    # Strumień żyje przez cały czas działania programu, więc nie ma tu bloku `with`.
    # atexit gwarantuje, że mikrofon zostanie zwolniony przy wyjściu — także po Ctrl+C.
    atexit.register(zamknij)


def _utworz_detektor():
    """
    Tworzy detektor słowa-klucza openWakeWord.

    Przy pierwszym uruchomieniu pobiera modele (~kilkanaście MB) do katalogu
    biblioteki. Kolejne uruchomienia działają już w pełni offline.

    Zwraca: obiekt Model gotowy do wywołania .predict().
    """
    logger.info("Przygotowuję detektor wake worda '%s'...", MODEL_WAKE_WORD)

    # Pobiera wskazany model + modele pomocnicze (melspectrogram, embedding, VAD).
    # Funkcja sama sprawdza, czy pliki już są — przy kolejnych startach nic nie robi.
    download_models(model_names=[MODEL_WAKE_WORD])

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
    logger.info("Wczytuję model Whisper '%s' na %s (%s)...",
                MODEL_WHISPER, URZADZENIE.upper(), COMPUTE_TYPE)

    try:
        model = WhisperModel(MODEL_WHISPER, device=URZADZENIE, compute_type=COMPUTE_TYPE)
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


def _czekaj_na_wake_word():
    """
    Blokuje działanie programu, dopóki nie usłyszy "Hey Jarvis".

    Dla każdej 80-milisekundowej porcji audio detektor zwraca słownik
    {nazwa_modelu: pewność 0.0-1.0}. Czekamy, aż pewność przekroczy próg.

    Zwraca: True gdy wykryto słowo, False gdy poproszono o zatrzymanie programu.
    """
    logger.info("[NASŁUCH] Czekam na 'Hey Jarvis'...")

    # Warunek pętli sprawdza przełącznik co ~80 ms, więc zamknięcie programu
    # jest natychmiastowe nawet wtedy, gdy Jarvis stoi bezczynnie godzinami.
    while not _zatrzymaj_sie.is_set():
        # Czytamy dokładnie tyle próbek, ile detektor oczekuje w jednej porcji.
        dane, _ = _stream.read(DLUGOSC_RAMKI)
        # Mikrofon zwraca kształt (n, 1) — spłaszczamy do zwykłej listy próbek.
        audio = dane.flatten()

        wyniki = _detektor.predict(audio)

        # Bierzemy najwyższy wynik zamiast szukać po nazwie klucza — nazwa modelu
        # w słowniku bywa wersjonowana ("hey_jarvis_v0.1"), więc tak jest odporniej.
        if max(wyniki.values()) > PROG_WYKRYCIA:
            # reset() czyści wewnętrzny bufor detektora. Bez tego przez chwilę
            # pamiętałby świeże wykrycie i po powrocie odpalałby się od razu ponownie.
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


def _rozpoznaj_mowe(audio):
    """
    Zamienia nagranie na tekst.

    Nie wymuszamy języka — Whisper sam wykrywa, czy mówisz po polsku czy angielsku.
    (Gdybyś chciał zablokować na polski, dopisz language="pl" w wywołaniu poniżej.)

    Zwraca: (tekst, kod_języka, pewność_języka).
    """
    # vad_filter odsiewa fragmenty ciszy — dzięki temu Whisper nie "zmyśla"
    # słów tam, gdzie nic nie powiedziałeś (częsty problem przy stałym czasie nagrania).
    segmenty, info = _model_whisper.transcribe(audio, beam_size=5, vad_filter=True)

    # transcribe() zwraca generator — tekst powstaje dopiero tutaj, przy łączeniu segmentów.
    tekst = " ".join(segment.text.strip() for segment in segmenty).strip()

    return tekst, info.language, info.language_probability


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

    _przygotuj()

    # Uwaga: NIE zgłaszamy tu "idle". Czekanie na wake word to stan domyślny,
    # a main.py ustawia go sam po wykonaniu komendy. Gdybyśmy zgłaszali "idle"
    # na starcie każdego cyklu, skasowalibyśmy czerwony błysk po poprzednim
    # błędzie — pętla wraca tu w kilka milisekund po jego zapaleniu,
    # więc praktycznie nigdy nie zdążyłbyś go zobaczyć.
    if not _czekaj_na_wake_word():
        return ""  # program jest zamykany

    logger.info("[WYKRYTO] Usłyszałem 'Hey Jarvis'!")

    zglos("listening")
    audio = _nagraj()

    if audio.size == 0:
        return ""  # nagrywanie przerwane przez zamykanie programu

    zglos("processing")
    tekst, jezyk, pewnosc = _rozpoznaj_mowe(audio)

    # Czyścimy bufor dopiero teraz, po transkrypcji.
    _oproznij_bufor()

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
        from openwakeword.vad import VAD

        _vad = VAD()
        logger.info("Detektor mowy (VAD) gotowy.")

    return _vad


def sluchaj_bez_wake_worda(orb_callback=None, limit_ciszy_s=8):
    """
    Nasłuchuje BEZ wymagania "Hey Jarvis" — to tryb trwającej rozmowy.

    orb_callback  — funkcja przyjmująca nazwę stanu (jak w sluchaj_komendy)
    limit_ciszy_s — ile czekamy na to, aż zaczniesz mówić, zanim uznamy
                    rozmowę za skończoną

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

    Zwraca: rozpoznany tekst, albo None gdy przez limit_ciszy_s nikt się nie
    odezwał (koniec sesji rozmowy) lub gdy program jest zamykany.
    """

    def zglos(stan):
        if orb_callback is not None:
            orb_callback(stan)

    _przygotuj()
    vad = _przygotuj_vad()

    # Stan VAD-a jest ciągły między wywołaniami (to sieć rekurencyjna),
    # więc przed każdą nową wypowiedzią zaczynamy od czystego licznika.
    vad.reset_states()
    _oproznij_bufor()

    logger.info("[ROZMOWA] Słucham dalej, bez wake worda (max %s s ciszy)...", limit_ciszy_s)

    ramek_na_sekunde = SAMPLE_RATE / DLUGOSC_RAMKI
    limit_czekania = int(limit_ciszy_s * ramek_na_sekunde)
    limit_ciszy_konczacej = int(CISZA_KONCZACA_S * ramek_na_sekunde)
    limit_nagrania = int(MAX_WYPOWIEDZ_S * ramek_na_sekunde)
    dlugosc_pre_bufora = max(1, int(PRE_BUFOR_S * ramek_na_sekunde))

    from collections import deque
    pre_bufor = deque(maxlen=dlugosc_pre_bufora)

    porcje = []
    mowa_trwa = False
    ramek_ciszy = 0
    ramek_czekania = 0

    while not _zatrzymaj_sie.is_set():
        dane, _ = _stream.read(DLUGOSC_RAMKI)
        pcm = dane.flatten()

        wynik = float(vad.predict(pcm, frame_size=RAMKA_VAD))
        jest_mowa = wynik > PROG_VAD

        if not mowa_trwa:
            # Faza 1: czekamy, aż ktoś się odezwie.
            pre_bufor.append(pcm)

            if jest_mowa:
                mowa_trwa = True
                ramek_ciszy = 0
                # Doklejamy bufor sprzed wykrycia, żeby nie zgubić pierwszej głoski.
                porcje = list(pre_bufor)
                zglos("listening")
                logger.info("[ROZMOWA] Słyszę mowę, nagrywam...")
                continue

            ramek_czekania += 1
            if ramek_czekania >= limit_czekania:
                logger.info("[ROZMOWA] Cisza przez %s s — kończę sesję rozmowy.",
                            limit_ciszy_s)
                return None
            continue

        # Faza 2: nagrywamy, aż zapadnie cisza.
        porcje.append(pcm)

        if jest_mowa:
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
    logger.info("[ROZMOWA] Nagrałem %.1f s, rozpoznaję...", len(audio) / SAMPLE_RATE)

    tekst, jezyk, pewnosc = _rozpoznaj_mowe(audio)
    _oproznij_bufor()

    if tekst:
        logger.info("[ROZMOWA] (%s, %.0f%%) %s", jezyk, pewnosc * 100, tekst)
        return tekst

    # VAD usłyszał dźwięk, ale Whisper nie wydobył z niego słów — to najczęściej
    # kaszlnięcie, trzaśnięcie drzwiami albo muzyka w tle. Traktujemy to jak ciszę,
    # czyli koniec rozmowy, zamiast zawracać głowę pustą odpowiedzią.
    logger.info("[ROZMOWA] Dźwięk bez rozpoznanych słów — kończę sesję.")
    return None


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


# --- Test samego modułu: `python wake_word_listener.py` ---
# Pełnego Jarvisa uruchamiasz przez `python main.py` — tutaj sprawdzasz tylko,
# czy mikrofon, wake word i transkrypcja działają.
if __name__ == "__main__":
    from logging_setup import skonfiguruj_logowanie

    skonfiguruj_logowanie()

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
