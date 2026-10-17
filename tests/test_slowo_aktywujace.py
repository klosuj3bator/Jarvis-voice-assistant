"""
Słowo aktywujące: własny model models/jarvis.onnx (a bez niego "hey_jarvis"),
próg czułości z Ustawień i "cichy" nasłuch okienka powitania.

Część z nasłuchem (wake_word_listener) potrzebuje numpy i sounddevice
(są w requirements-test.txt) — bez nich te testy się pomijają. Mikrofonu
i modeli nie używa żaden test: detektor, strumień i Whisper to atrapy.
"""

import os
import sys
import threading
import types

import pytest

import konfiguracja
import slowo_aktywujace as sa

try:
    import numpy as np

    import wake_word_listener as wwl
except Exception:           # brak sounddevice albo PortAudio nie wstał (serwer bez dźwięku)
    wwl = None

z_nasluchem = pytest.mark.skipif(wwl is None, reason="bez sounddevice (lekkie zależności testów)")


def zapisz_model(tresc=b"onnx"):
    os.makedirs(os.path.dirname(sa.PLIK_WLASNEGO_MODELU), exist_ok=True)
    with open(sa.PLIK_WLASNEGO_MODELU, "wb") as plik:
        plik.write(tresc)


# --- który model i jaki próg --------------------------------------------

def test_bez_pliku_zostaje_hey_jarvis_z_progiem_0_2():
    assert sa.wlasny_model() is None
    assert sa.prog_wykrycia(False) == 0.2
    assert sa.prog_podejrzenia(0.2) == 0.04            # jak przed zmianą
    assert sa.PLIK_WLASNEGO_MODELU.endswith(os.path.join("models", "jarvis.onnx"))


def test_wlasny_model_z_pliku_ma_domyslny_prog_0_5():
    zapisz_model()
    assert sa.wlasny_model() == sa.PLIK_WLASNEGO_MODELU
    assert sa.prog_wykrycia(True) == 0.5
    assert "jarvis.onnx" in sa.opis_modelu(True)


def test_pusty_plik_to_nie_model():
    zapisz_model(b"")
    assert sa.wlasny_model() is None


@pytest.mark.parametrize("ustawiony, wbudowany, wlasny", [
    ("0.35", 0.35, 0.35),          # Ustawienia wygrywają z domyślnym dla obu modeli
    ("0,4", 0.4, 0.4),             # przecinek jak w polskiej liczbie
    ("0.01", 0.05, 0.05),          # za czuły — przycięty do minimum
    ("0.99", 0.95, 0.95),          # prawie głuchy — przycięty do maksimum
    ("", 0.2, 0.5),                # automatycznie
    ("abc", 0.2, 0.5),             # śmieci = automatycznie
    ("1.5", 0.2, 0.5),
])
def test_prog_z_ustawien(monkeypatch, ustawiony, wbudowany, wlasny):
    monkeypatch.setenv(konfiguracja.PROG_WAKE_WORDA, ustawiony)
    assert (sa.prog_wykrycia(False), sa.prog_wykrycia(True)) == (wbudowany, wlasny)


def test_imie_jarvisa_w_tekscie():
    assert sa.brzmi_jak_jarvis("Hej, Dżarwis!") and sa.brzmi_jak_jarvis("Jarwiz, puść coś")
    assert not sa.brzmi_jak_jarvis("kupiłem jarzyny") and not sa.brzmi_jak_jarvis("")


# --- detektor: własny model albo hey_jarvis -----------------------------

@pytest.fixture
def openwakeword(monkeypatch):
    """Atrapa biblioteki openWakeWord: zapamiętuje, z jakim modelem ją stworzono."""
    stan = {"utworzone": [], "zepsute": set()}

    class Model:
        def __init__(self, wakeword_models, inference_framework, **pomocnicze):
            assert inference_framework == "onnx"
            self.model = wakeword_models[0]
            stan["utworzone"].append(self.model)

        def predict(self, audio):
            if self.model in stan["zepsute"]:
                raise ValueError("model wytrenowany pod inne wejście")
            return {"model": 0.0}

        def reset(self):
            pass

    modul = types.ModuleType("openwakeword.model")
    modul.Model = Model
    monkeypatch.setitem(sys.modules, "openwakeword", types.ModuleType("openwakeword"))
    monkeypatch.setitem(sys.modules, "openwakeword.model", modul)
    monkeypatch.setitem(sys.modules, "openwakeword.custom_verifier_model", types.ModuleType("x"))
    monkeypatch.setattr(wwl.modele, "openwakeword_z_appdata", lambda: False)
    monkeypatch.setattr(wwl, "_brakuje_modeli", lambda: False)
    monkeypatch.setattr(wwl, "_wlasny_model", False)
    return stan


@z_nasluchem
def test_detektor_bez_pliku_uzywa_hey_jarvis(openwakeword):
    wwl._utworz_detektor()
    assert openwakeword["utworzone"] == ["hey_jarvis"] and wwl._wlasny_model is False


@z_nasluchem
def test_detektor_z_plikiem_uzywa_wlasnego_modelu(openwakeword):
    zapisz_model()
    wwl._utworz_detektor()
    assert openwakeword["utworzone"] == [sa.PLIK_WLASNEGO_MODELU] and wwl._wlasny_model is True


@z_nasluchem
def test_zepsuty_wlasny_model_nie_ogluszy_jarvisa(openwakeword):
    zapisz_model()
    openwakeword["zepsute"].add(sa.PLIK_WLASNEGO_MODELU)
    wwl._utworz_detektor()
    assert openwakeword["utworzone"] == [sa.PLIK_WLASNEGO_MODELU, "hey_jarvis"]
    assert wwl._wlasny_model is False


# --- próg w czekaniu na "Hey Jarvis" ------------------------------------

class Strumien:
    """Mikrofon na niby: cisza; po `ile` porcjach ustawia `stop`."""

    def __init__(self, stop=None, ile=5):
        self.stop, self.ile, self.przeczytane = stop, ile, 0
        self.read_available = 0

    def read(self, ile_probek):
        self.przeczytane += 1
        if self.stop is not None and self.przeczytane >= self.ile:
            self.stop.set()
        return np.zeros((ile_probek, 1), dtype=np.int16), False


class Detektor:
    def __init__(self, wynik):
        self.wynik = wynik

    def predict(self, audio):
        return {"jarvis": self.wynik}

    def reset(self):
        pass


@z_nasluchem
@pytest.mark.parametrize("wlasny, ustawiony, wykryto", [
    (False, "", True),             # hey_jarvis, próg 0.2: ocena 0.3 wystarcza
    (True, "", False),             # własny model, próg 0.5: już nie
    (True, "0.25", True),          # próg z Ustawień działa od razu, bez restartu
    (False, "0.45", False),
])
def test_prog_przy_czekaniu_na_slowo(monkeypatch, wlasny, ustawiony, wykryto):
    stop = threading.Event()
    monkeypatch.setattr(wwl, "_stream", Strumien(stop))
    monkeypatch.setattr(wwl, "_detektor", Detektor(0.3))
    monkeypatch.setattr(wwl, "_wlasny_model", wlasny)
    monkeypatch.setattr(wwl, "_potwierdz_whisperem", lambda bufor: pytest.fail("bez Whispera"))
    monkeypatch.setenv(konfiguracja.PROG_WAKE_WORDA, ustawiony)
    assert wwl._czekaj_na_wake_word(stop=stop) is wykryto


# --- cichy nasłuch (okienko powitania) ----------------------------------

class Cisza:
    def predict(self, pcm, frame_size):
        return 0.0


@z_nasluchem
@pytest.mark.parametrize("cicho", [True, False])
def test_cichy_nasluch_nie_zapisuje_tresci(monkeypatch, caplog, cicho):
    monkeypatch.setattr(wwl, "_stream", Strumien())
    monkeypatch.setattr(wwl, "_rozpoznaj_mowe", lambda audio, filtruj_cisze: ("sąsiad o kredycie", "pl", 0.9))
    caplog.set_level("INFO")
    tekst = wwl._dokoncz_wypowiedz([np.zeros(1280, dtype=np.int16)], Cisza(), lambda stan: None,
                                   limit_ciszy_konczacej=2, limit_nagrania=50, cicho=cicho)
    assert tekst == "sąsiad o kredycie"
    assert ("kredycie" in caplog.text) is not cicho


@z_nasluchem
def test_cichy_nasluch_nie_zapisuje_nierozpoznanych_nagran(monkeypatch):
    monkeypatch.setattr(wwl, "_stream", Strumien())
    monkeypatch.setattr(wwl, "_rozpoznaj_mowe", lambda audio, filtruj_cisze: ("", "pl", 0.0))
    monkeypatch.setattr(wwl, "_zapisz_nierozpoznane", lambda *a: pytest.fail("nagranie na dysk"))
    assert wwl._dokoncz_wypowiedz([np.zeros(1280, dtype=np.int16)], Cisza(), lambda stan: None,
                                  limit_ciszy_konczacej=2, limit_nagrania=50, cicho=True) == ""
