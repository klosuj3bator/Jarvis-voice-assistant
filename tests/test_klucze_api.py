"""
Klucze API przy starcie: liczenie brakujących (linijka na HUD-zie), Jarvis
bez klucza Anthropic, żadnego kreatora przy uruchamianiu, Ustawienia na
zakładce Klucze API. Testy okien Qt pomijają się, gdy nie ma PySide6 (lekkie
zależności testów, GitHub Actions).
"""

import importlib.util
import inspect
import json
import os

import pytest

import agent
import konfiguracja
from atrapy import KlientClaude, przebieg, tura

z_qt = pytest.mark.skipif(importlib.util.find_spec("PySide6") is None,
                          reason="bez PySide6 (lekkie zależności testów)")


def zapisz_ustawienia(**wartosci):
    with open(konfiguracja.PLIK, "w", encoding="utf-8") as plik:
        plik.writelines(f'{k}="{v}"\n' for k, v in wartosci.items())


# --- które klucze brakują -------------------------------------------------

def test_bez_kluczy_brakuje_wszystkich_anthropic_pierwszy():
    assert konfiguracja.brakujace_klucze() == ["anthropic", "spotify", "telegram", "gmail", "kalendarz"]


def test_ustawione_nie_brakuja(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-atrapa")
    monkeypatch.setenv("TELEGRAM_JARVIS_TOKEN", "123:atrapa")
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "1")
    assert konfiguracja.brakujace_klucze() == ["spotify", "gmail", "kalendarz"]


def test_swiadomie_wylaczone_nie_licza_sie_jako_brak():
    zapisz_ustawienia(SPOTIPY_CLIENT_ID="", GOOGLE_CALENDAR_ICAL_URL="")      # "Wyłącz" w Ustawieniach
    assert konfiguracja.wylaczona("spotify") and konfiguracja.wylaczona("kalendarz")
    assert konfiguracja.brakujace_klucze() == ["anthropic", "telegram", "gmail"]


def test_anthropic_nigdy_nie_jest_wylaczony():
    zapisz_ustawienia(ANTHROPIC_API_KEY="")
    assert not konfiguracja.wylaczona("anthropic")
    assert "anthropic" in konfiguracja.brakujace_klucze()


def test_kalendarz_przez_api_liczy_sie_jako_ustawiony():
    with open(konfiguracja.PLIK_GOOGLE_TOKENU, "w", encoding="utf-8") as plik:
        json.dump({"refresh_token": "x"}, plik)
    assert "kalendarz" not in konfiguracja.brakujace_klucze()
    with open(konfiguracja.PLIK_GOOGLE_TOKENU, "w", encoding="utf-8") as plik:
        json.dump({"refresh_token": "x", "wygasl": True}, plik)
    assert "kalendarz" in konfiguracja.brakujace_klucze()


# --- bez klucza Anthropic -------------------------------------------------

def test_bez_klucza_anthropic_jarvis_mowi_gdzie_go_dodac(monkeypatch):
    monkeypatch.setattr(agent, "_klient", None)
    monkeypatch.setattr(agent, "_utworz_klienta", lambda: pytest.fail("bez klucza nie tworzymy klienta"))
    zdania, stan = przebieg(agent.odpowiedz("jaka jutro pogoda?", []))
    assert zdania == [agent.BRAK_KLUCZA_ANTHROPIC]
    assert "Klucze API" in zdania[0] and stan["historia"] == []
    _, stan = przebieg(agent.odpowiedz("cześć", [], True, "telegram"))   # z Telegrama tak samo
    assert stan["historia"] == []


def test_po_dodaniu_klucza_rozmowa_dziala_bez_restartu(monkeypatch):
    monkeypatch.setattr(agent, "_klient", None)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-atrapa")
    utworzone = []
    monkeypatch.setattr(agent, "_utworz_klienta",
                        lambda: utworzone.append(1) or KlientClaude(tura("Cześć!")))
    zdania, _ = przebieg(agent.odpowiedz("cześć", []))
    assert utworzone == [1] and zdania == ["Cześć!"]


# --- start i Ustawienia ----------------------------------------------------

@z_qt
def test_start_nie_otwiera_kreatora_ani_okien_z_kluczami():
    import main

    kod_startu = inspect.getsource(main.main)
    assert "uruchom_kreator" not in kod_startu.split("def ustawienia")[0]
    assert "brakujace_klucze()" in kod_startu


@z_qt
def test_ustawienia_otwieraja_sie_na_zakladce_klucze_api(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    import setup_wizard

    app = QApplication.instance() or QApplication([])
    okno = setup_wizard.KreatorJarvisa(pierwsze=False, strona="klucze")
    try:
        assert okno._stos.currentIndex() == 0 and okno._zakladki["Klucze API"].isChecked()
        stan_anthropic, _ = okno._przeglad["anthropic"]
        assert stan_anthropic.text().startswith("BRAK")
        okno._otworz_zakladke("Tapeta")
        assert okno._zakladki["Tapeta"].isChecked() and not okno._zakladki["Klucze API"].isChecked()
        okno._idz_do("kalendarz")
        assert okno._strony[okno._stos.currentIndex()][0].funkcja == "kalendarz"
        assert okno._zakladki["Klucze API"].isChecked()
    finally:
        okno.close()
        app.processEvents()


def test_kalendarz_w_ustawieniach_przyjmuje_api_albo_ical(monkeypatch):
    if importlib.util.find_spec("PySide6") is None:
        pytest.skip("bez PySide6")
    import setup_wizard

    ok, komunikat = setup_wizard.sprawdz_kalendarz({"GOOGLE_CALENDAR_ICAL_URL": ""})
    assert not ok and "Połącz z Google" in komunikat
    with open(konfiguracja.PLIK_GOOGLE_TOKENU, "w", encoding="utf-8") as plik:
        json.dump({"refresh_token": "x"}, plik)
    assert setup_wizard.sprawdz_kalendarz({"GOOGLE_CALENDAR_ICAL_URL": ""})[0]
    assert os.path.exists(konfiguracja.PLIK_GOOGLE_TOKENU)
