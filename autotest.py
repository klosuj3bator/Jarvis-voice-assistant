"""
autotest.py — sprawdzenie zbudowanej paczki: `Jarvis.exe --autotest=wynik.json`.

PyInstaller pakuje to, co znajdzie w kodzie, i łatwo coś przeoczyć: bibliotekę
DLL silnika Whispera, dane stref czasowych, wtyczkę QML. Taki brak wychodzi
dopiero po uruchomieniu — często u kogoś innego, na innym komputerze.

Ten tryb uruchamia z paczki każdą "ciężką" część Jarvisa po kolei, BEZ
mikrofonu, HUD-a na ekranie, Telegrama i zapytań do Claude, i zapisuje wynik
do pliku JSON. Woła go instalator/zbuduj.py zaraz po zbudowaniu.
"""

import json
import sys
import time
import traceback


def _proba(wyniki, nazwa, funkcja):
    start = time.monotonic()
    try:
        opis = funkcja()
        wyniki[nazwa] = {"ok": True, "opis": opis or "", "s": round(time.monotonic() - start, 2)}
    except Exception as e:
        wyniki[nazwa] = {"ok": False, "opis": f"{type(e).__name__}: {e}",
                         "slad": traceback.format_exc()[-1500:]}


def uruchom(plik_wyniku):
    wyniki = {}

    def moduly():
        import agent, app_launcher, briefing, czujniki, email_monitor, kalendarz  # noqa: F401,E401
        import konfiguracja, modele, notes, pamiec, przegladarka, reminders  # noqa: F401,E401
        import schowek, setup_wizard, slownik, spotify_controller, stan_komputera  # noqa: F401,E401
        import system_control, telegram_bridge, tts, wake_word_listener  # noqa: F401,E401
        return "wszystkie moduły Jarvisa się importują"

    def sciezki():
        import os

        import sciezki as s
        brak = [p for p in ("hud.qml", "hud.frag.qsb", "jarvis_icon.png")
                if not os.path.exists(s.zasob(p))]
        if brak:
            raise FileNotFoundError(f"brak w paczce: {brak}")
        return f"spakowany={s.SPAKOWANY}, dane={s.DANE}"

    def strefa():
        from zoneinfo import ZoneInfo
        return str(ZoneInfo("Europe/Warsaw"))

    def dzwiek():
        import av  # noqa: F401
        import sounddevice as sd
        return f"PortAudio: {sd.get_portaudio_version()[1]}"

    def okno_qt():
        from PySide6.QtWidgets import QApplication

        import gui
        import setup_wizard
        app = QApplication.instance() or QApplication(sys.argv)
        hud = gui.JarvisHUD()          # wczytuje hud.qml — bez pokazywania
        kreator = setup_wizard.KreatorJarvisa(pierwsze=False)
        hud.close(), kreator.close()
        app.processEvents()
        return "HUD (QML) i okno Ustawień powstają"

    def modele_mowy():
        import numpy as np

        import modele
        # Wystarczy mały "tiny" i openWakeWord — ścieżka ładowania jest ta sama
        # co dla "small", a nie trzeba do testu 480 MB.
        if not (modele._whisper_komplet("tiny") and modele._openwakeword_komplet()):
            return "modeli nie ma w %APPDATA% — pominięte (pobierze je kreator)"
        from faster_whisper import WhisperModel
        m = WhisperModel(modele.whisper("tiny"), device="cpu", compute_type="int8")
        segmenty, _ = m.transcribe(np.zeros(16000, np.float32), language="pl")
        list(segmenty)
        import wake_word_listener as w
        detektor = w._utworz_detektor()
        detektor.predict(np.zeros(1280, np.int16))
        w._przygotuj_vad().predict(np.zeros(1280, np.int16), frame_size=640)
        return "Whisper (CTranslate2), openWakeWord i VAD działają"

    def siec():
        import requests
        requests.head("https://huggingface.co", timeout=15)
        return "HTTPS działa (certyfikaty w paczce)"

    for nazwa, funkcja in (("moduły", moduly), ("zasoby", sciezki), ("strefa czasowa", strefa),
                           ("dźwięk", dzwiek), ("Qt", okno_qt), ("modele", modele_mowy),
                           ("sieć", siec)):
        _proba(wyniki, nazwa, funkcja)

    with open(plik_wyniku, "w", encoding="utf-8") as plik:
        json.dump(wyniki, plik, ensure_ascii=False, indent=2)
    return 0 if all(w["ok"] for w in wyniki.values()) else 1
