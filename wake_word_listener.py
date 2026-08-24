"""
wake_word_listener.py — warstwa "uszu" Jarvisa.

Wystawia jedną funkcję dla reszty programu: sluchaj_komendy().
Blokuje wykonanie, czeka na "Hey Jarvis", nagrywa kilka sekund,
przepuszcza je przez Whispera i zwraca rozpoznany tekst jako string.

Główna pętla programu NIE żyje już w tym pliku — jest w main.py.
Ten moduł odpowiada wyłącznie za: mikrofon -> tekst.

Podział na dwa etapy (tani detektor + drogi Whisper) jest celowy: Whisper jest
zbyt wolny, żeby puszczać przez niego cały czas wszystko, co słyszy mikrofon.
openWakeWord pełni rolę taniego "strażnika" i jest w pełni open-source —
nie wymaga konta, klucza API ani sieci (poza jednorazowym pobraniem modeli).
"""

import atexit

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel
from openwakeword.model import Model as WakeWordModel
from openwakeword.utils import download_models

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

# Model Whispera. "small" to dobry kompromis: wyraźnie lepiej radzi sobie z polskim
# niż "base", a nadal działa na CPU w kilka sekund. Jeśli będzie za wolno — wpisz "base".
MODEL_WHISPER = "small"

# int8 = kwantyzacja, czyli model liczy na liczbach całkowitych zamiast zmiennoprzecinkowych.
# Kilkukrotnie szybciej na CPU, kosztem minimalnej utraty dokładności.
COMPUTE_TYPE = "int8"

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
    print(f"Przygotowuję detektor wake worda '{MODEL_WAKE_WORD}'...")

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

    print("Detektor gotowy.")
    return detektor


def _wczytaj_model_whisper():
    """
    Wczytuje model faster-whisper do pamięci.

    Przy pierwszym uruchomieniu model (~500 MB dla "small") pobierze się z internetu.

    Zwraca: obiekt WhisperModel.
    """
    print(f"Wczytuję model Whisper '{MODEL_WHISPER}' (przy pierwszym razie może się pobierać)...")
    model = WhisperModel(MODEL_WHISPER, device="cpu", compute_type=COMPUTE_TYPE)
    print("Model gotowy.")
    return model


def _czekaj_na_wake_word():
    """
    Blokuje działanie programu, dopóki nie usłyszy "Hey Jarvis".

    Dla każdej 80-milisekundowej porcji audio detektor zwraca słownik
    {nazwa_modelu: pewność 0.0-1.0}. Czekamy, aż pewność przekroczy próg.
    """
    print("\n[NASŁUCH] Czekam na 'Hey Jarvis'... (Ctrl+C żeby zakończyć)")

    while True:
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
            return


def _nagraj(sekundy=CZAS_NAGRANIA):
    """
    Nagrywa zadaną liczbę sekund audio z otwartego strumienia mikrofonu.

    Zwraca: tablicę numpy float32 w zakresie -1.0..1.0 — czyli w formacie,
    którego oczekuje Whisper.
    """
    print(f"[NAGRYWAM] Mów teraz ({sekundy} s)...")

    liczba_probek = int(SAMPLE_RATE * sekundy)
    dane, _ = _stream.read(liczba_probek)

    print("[NAGRYWAM] Koniec nagrania, rozpoznaję...")

    # Mikrofon daje int16 (-32768..32767), a Whisper chce float32 (-1.0..1.0).
    return dane.flatten().astype(np.float32) / 32768.0


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
    — main.py może wtedy po prostu wrócić do nasłuchu.
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
    _czekaj_na_wake_word()
    print("[WYKRYTO] Usłyszałem 'Hey Jarvis'!")

    zglos("listening")
    audio = _nagraj()

    zglos("processing")
    tekst, jezyk, pewnosc = _rozpoznaj_mowe(audio)

    # Czyścimy bufor dopiero teraz, po transkrypcji.
    _oproznij_bufor()

    if tekst:
        print(f"[TEKST] ({jezyk}, {pewnosc:.0%}) {tekst}")
    else:
        print("[TEKST] Nic nie usłyszałem.")

    return tekst


def zamknij():
    """
    Zamyka strumień mikrofonu. Wołane automatycznie przy końcu programu (atexit),
    ale możesz je wywołać ręcznie, jeśli chcesz zwolnić mikrofon wcześniej.
    """
    global _stream

    if _stream is not None:
        _stream.stop()
        _stream.close()
        _stream = None


# --- Test samego modułu: `python wake_word_listener.py` ---
# Pełnego Jarvisa uruchamiasz przez `python main.py` — tutaj sprawdzasz tylko,
# czy mikrofon, wake word i transkrypcja działają.
if __name__ == "__main__":
    print("Dostępne urządzenia wejściowe:")
    for i, urzadzenie in enumerate(sd.query_devices()):
        if urzadzenie["max_input_channels"] > 0:
            print(f"  [{i}] {urzadzenie['name']}")
    print(f"Używam domyślnego: {sd.query_devices(kind='input')['name']}\n")

    try:
        while True:
            sluchaj_komendy()
    except KeyboardInterrupt:
        print("\nZatrzymuję nasłuch. Do zobaczenia!")
