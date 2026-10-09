"""
rozruch.py — start Jarvisa: moduły i modele ładowane RÓWNOLEGLE, z postępem dla HUD-a.


SKĄD BYŁ DŁUGI START
====================

Dawniej wszystko szło po kolei, w jednym wątku: najpierw import modułów
(agent, mowa, Telegram…), potem przy pierwszym czuwaniu detektor "Hey Jarvis"
i Whisper, a VAD i mały Whisper dopiero przy pierwszej rozmowie. Zmierzone
01.10 na rozgrzanym komputerze — razem 8,9 s:

    import nasłuchu ............ 1,2 s   (numpy, sounddevice)
    moduły Jarvisa ............. 3,7 s   (agent: anthropic, spotipy…; mowa)
    Whisper small .............. 2,9 s
    mały Whisper (potwierdzanie) 0,7 s
    detektor + VAD ............. 0,4 s

Po włączeniu komputera (pliki jeszcze nie w pamięci podręcznej Windowsa)
same moduły trwały 31 s — wtedy Jarvis głównie czeka na dysk.


CO ROBIMY
=========

Najpierw import nasłuchu (reszta kroków go potrzebuje), a potem cztery
wątki naraz: moduły Jarvisa, Whisper small, mały Whisper, detektory.
Modele ładuje C++ (CTranslate2, ONNX), który zwalnia GIL — więc naprawdę
liczą się w tym samym czasie co importy Pythona. A gdy wszyscy czekają
na dysk, kilka odczytów naraz idzie szybciej niż jeden po drugim.

Rozruch rusza w main.py ZANIM powstanie okno HUD-a — budowanie sceny
(kilka sekund) też nakłada się na ładowanie.

Najcięższy był sam import biblioteki anthropic (2,4 s z 3,7 s modułów).
Do nasłuchu nie jest potrzebna, więc agent.py importuje ją dopiero przy
pierwszym zapytaniu, a rozgrzej() — w tle, zaraz po "Systemy gotowe".

Postęp: każdy krok ma wagę (sekundy z pomiaru wyżej); procent to suma wag
kroków skończonych. HUD pokazuje go jako "Uruchamianie systemów…".
"""

import importlib
import logging
import threading
import time

logger = logging.getLogger(__name__)

# (klucz, opis na HUD-zie, waga) — wagi to sekundy z pomiaru PO zmianach
# (01.10, równolegle, bez anthropic): całość 4,1-4,7 s zamiast 8,9 s.
KROKI = (
    ("nasluch", "nasłuch", 1.0),
    ("moduly", "moduły Jarvisa", 1.3),
    ("whisper", "rozpoznawanie mowy", 3.3),
    ("potwierdzanie", "potwierdzanie słowa", 1.5),
    ("detektory", "słowo aktywujące", 0.7),
)

# Moduły, które main.py dostaje gotowe (dawniej importował je zaladuj_moduly).
MODULY = ("email_monitor", "pamiec", "reminders", "system_control",
          "telegram_bridge", "tts", "agent")


class Rozruch:
    """
    Jedno uruchomienie: start() w tle, czekaj() blokuje do końca,
    sluchaj() podaje postęp (procent, opis trwających kroków).
    """

    def __init__(self):
        self.moduly = {}               # nazwa -> zaimportowany moduł
        self._stan = {klucz: "czeka" for klucz, _, _ in KROKI}
        self._bledy = []
        self._gotowe = threading.Event()
        self._blokada = threading.Lock()
        self._sluchacze = []
        self._start = None

    # --- postęp ---

    def postep(self):
        """(procent 0-100, opis tego, co teraz trwa)."""
        with self._blokada:
            calosc = sum(waga for _, _, waga in KROKI)
            zrobione = sum(waga for klucz, _, waga in KROKI if self._stan[klucz] == "gotowe")
            trwa = [opis for klucz, opis, _ in KROKI if self._stan[klucz] == "trwa"]
        procent = 100 if self._gotowe.is_set() else min(99, round(100 * zrobione / calosc))
        return procent, ", ".join(trwa)

    def sluchaj(self, funkcja):
        """funkcja(procent, opis) — wołana przy każdej zmianie i od razu teraz."""
        with self._blokada:
            self._sluchacze.append(funkcja)
        funkcja(*self.postep())

    def _powiadom(self):
        procent, opis = self.postep()
        with self._blokada:
            sluchacze = list(self._sluchacze)
        for sluchacz in sluchacze:
            try:
                sluchacz(procent, opis)
            except Exception:
                logger.exception("[ROZRUCH] Odbiorca postępu zawiódł")

    # --- praca ---

    def _krok(self, klucz, funkcja):
        with self._blokada:
            self._stan[klucz] = "trwa"
        self._powiadom()
        t0 = time.perf_counter()
        try:
            funkcja()
        except BaseException as e:            # także SystemExit z wnętrza biblioteki
            logger.exception("[ROZRUCH] Krok '%s' się nie udał", klucz)
            self._bledy.append(e)
        logger.info("[ROZRUCH] %s: %.1f s", klucz, time.perf_counter() - t0)
        with self._blokada:
            self._stan[klucz] = "gotowe"
        self._powiadom()

    def _moduly(self):
        for nazwa in MODULY:
            self.moduly[nazwa] = importlib.import_module(nazwa)

    def _praca(self):
        self._start = time.perf_counter()
        try:
            # Krok 1 — sam: pozostałe potrzebują modułu nasłuchu.
            self._krok("nasluch", lambda: self.moduly.__setitem__(
                "wake_word_listener", importlib.import_module("wake_word_listener")))
            if self._bledy:
                return
            nasluch = self.moduly["wake_word_listener"]

            # Krok 2 — cztery wątki naraz (opis modułu).
            watki = [threading.Thread(target=self._krok, args=(klucz, funkcja),
                                      name=f"rozruch-{klucz}", daemon=True)
                     for klucz, funkcja in (("moduly", self._moduly),
                                            ("whisper", nasluch._zapewnij_model_whispera),
                                            ("potwierdzanie", nasluch.zaladuj_potwierdzanie),
                                            ("detektory", nasluch.zaladuj_detektor))]
            for watek in watki:
                watek.start()
            for watek in watki:
                watek.join()
        finally:
            logger.info("[ROZRUCH] Gotowe w %.1f s%s.", time.perf_counter() - self._start,
                        f" — {len(self._bledy)} błędów" if self._bledy else "")
            self._gotowe.set()
            self._powiadom()

    def start(self):
        threading.Thread(target=self._praca, name="rozruch", daemon=True).start()
        return self

    def czekaj(self):
        """Blokuje do końca rozruchu. Pierwszy błąd przekazuje dalej."""
        self._gotowe.wait()
        if self._bledy:
            raise self._bledy[0]
        return self.moduly

    def rozgrzej(self):
        """
        Po starcie, w tle: biblioteki potrzebne dopiero przy pierwszym pytaniu.

        anthropic sam importuje się 2,4 s — start na niego nie czeka
        (agent.py, _anthropic), ale lepiej, żeby był gotowy, zanim zadasz
        pierwsze pytanie, niż żeby to pytanie czekało te dwie sekundy.
        """
        def praca():
            t0 = time.perf_counter()
            try:
                importlib.import_module("anthropic")
                logger.info("[ROZRUCH] Rozgrzewka (anthropic): %.1f s", time.perf_counter() - t0)
            except Exception:
                logger.exception("[ROZRUCH] Rozgrzewka się nie udała")

        threading.Thread(target=praca, name="rozruch-rozgrzewka", daemon=True).start()
