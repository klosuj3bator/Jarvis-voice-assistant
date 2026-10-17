"""
Okienko powitania po starcie: co jest powitaniem, ile trwa okienko, co
Jarvis robi z powitaniem (jedna odpowiedź i zwykły tryb), znacznik dla
agenta i zakładka Nasłuch w Ustawieniach.

Mikrofon, czas i Claude to atrapy. Testy main.py i okna Ustawień potrzebują
PySide6 — bez niego (lekkie zależności testów) się pomijają.
"""

import importlib.util
from types import SimpleNamespace as NS

import pytest

import agent
import konfiguracja
import powitanie
import powitanie_startowe as ps
from atrapy import KlientClaude, przebieg, tura

z_qt = pytest.mark.skipif(importlib.util.find_spec("PySide6") is None,
                          reason="bez PySide6 (lekkie zależności testów)")


# --- co jest powitaniem -------------------------------------------------

@pytest.mark.parametrize("tekst", [
    "Hej, wróciłem!", "Cześć Jarvis.", "Jestem.", "No to jestem", "Już jestem z powrotem",
    "Wróciłam", "Dzień dobry, Dżarwis", "Siema Jarvis, co słychać?", "to ja", "Jestem w domu",
    "Hej Jarvis",
])
def test_powitania(tekst):
    assert ps.rodzaj(tekst) == "powitanie"


@pytest.mark.parametrize("tekst", [
    "cześć",                           # samo "cześć" — telefon albo ktoś inny w pokoju
    "Dzień dobry państwu",             # telewizja
    "Jestem głodny",
    "Cześć mamo, co tam?",
    "Halo?",
    "Oglądaliśmy wczoraj film o Jarvisie i było super",
    "", "   ",
])
def test_inne_wypowiedzi_sa_pomijane(tekst):
    assert ps.rodzaj(tekst) is None


@pytest.mark.parametrize("tekst", ["Hej Jarvis, puść muzykę", "Jarvis, jaka jutro pogoda?"])
def test_wolanie_jarvisa_z_poleceniem(tekst):
    assert ps.rodzaj(tekst) == "do_jarvisa"


# --- okienko ------------------------------------------------------------

class Zegar:
    def __init__(self):
        self.teraz = 1000.0

    def __call__(self):
        return self.teraz


def okienko(wypowiedzi, minuty=10, zatrzymaj_po=None):
    """Uruchamia czekaj_na_powitanie z atrapami; zwraca (wynik, zapis wywołań)."""
    zegar, zapis = Zegar(), {"limity": [], "przywitano": [], "polecenia": []}
    wypowiedzi = list(wypowiedzi)

    def sluchaj(limit_s):
        zapis["limity"].append(limit_s)
        if not wypowiedzi:                 # cisza do końca okienka
            zegar.teraz += limit_s
            return None
        zegar.teraz += 20
        return wypowiedzi.pop(0)

    wynik = ps.czekaj_na_powitanie(
        sluchaj=sluchaj, przywitaj=zapis["przywitano"].append, polecenie=zapis["polecenia"].append,
        czy_zatrzymano=lambda: zatrzymaj_po is not None and len(zapis["limity"]) >= zatrzymaj_po,
        minuty=minuty, zegar=zegar)
    return wynik, zapis


def test_powitanie_konczy_okienko(caplog):
    caplog.set_level("INFO")
    wynik, zapis = okienko(["", None, "Cześć mamo, co tam?", "Hej, wróciłem"])
    assert wynik == "powitanie"
    assert zapis["przywitano"] == ["Hej, wróciłem"] and zapis["polecenia"] == []
    assert "mamo" not in caplog.text                 # cudzych rozmów nie zapisujemy


def test_inne_wypowiedzi_ignoruje_a_po_czasie_zwykly_tryb():
    wynik, zapis = okienko(["Cześć mamo", "Jestem głodny"], minuty=10)
    assert wynik == "czas" and zapis["przywitano"] == zapis["polecenia"] == []
    # Nasłuch dostaje tylko tyle czasu, ile zostało z okienka.
    assert zapis["limity"][0] == 600 and zapis["limity"][-1] == 560


def test_polecenie_z_imieniem_dziala_od_razu():
    wynik, zapis = okienko(["Hej Jarvis, puść muzykę"])
    assert wynik == "polecenie" and zapis["polecenia"] == ["Hej Jarvis, puść muzykę"]
    assert zapis["przywitano"] == []


def test_zamykanie_programu_konczy_okienko():
    assert okienko(["Cześć mamo"] * 5, zatrzymaj_po=2)[0] == "zatrzymano"


# --- ustawienia ---------------------------------------------------------

def test_ustawienia_okienka(monkeypatch):
    assert konfiguracja.powitanie_po_starcie() and konfiguracja.okno_powitania_min() == 10
    monkeypatch.setenv(konfiguracja.POWITANIE_PO_STARCIE, "0")
    assert not konfiguracja.powitanie_po_starcie()
    for zapisane, minuty in (("25", 25), ("500", 60), ("0", 1), ("dużo", 10)):
        monkeypatch.setenv(konfiguracja.OKNO_POWITANIA, zapisane)
        assert konfiguracja.okno_powitania_min() == minuty


def test_ustawienia_okienka_i_progu_nie_wymagaja_restartu():
    assert {konfiguracja.POWITANIE_PO_STARCIE, konfiguracja.OKNO_POWITANIA,
            konfiguracja.PROG_WAKE_WORDA} <= konfiguracja.BEZ_RESTARTU


# --- agent: znacznik powitania ------------------------------------------

def test_agent_dostaje_znacznik_i_wita_przez_narzedzie(monkeypatch):
    monkeypatch.setattr(powitanie, "zbierz", lambda: ("Powitanie: dane", True))
    klient = KlientClaude(tura(narzedzia=[("p1", "powitanie_po_powrocie", {})]),
                          tura("Dzień dobry, Macieju!"))
    agent._klient = klient
    zdania, _ = przebieg(agent.odpowiedz("Hej, wróciłem", [], powitanie_startowe=True))
    assert zdania == ["Dzień dobry, Macieju!"]
    pierwsze = klient.zapytania[0]
    assert "[powitanie po starcie] Hej, wróciłem" in str(pierwsze["messages"])
    assert "[powitanie po starcie]" in str(pierwsze["system"])         # instrukcja dla modelu
    assert "tool_choice" not in pierwsze                                # bez wymuszania narzędzia


# --- main.py: jedna odpowiedź i zwykły tryb ------------------------------

@pytest.fixture
def jarvis(monkeypatch):
    """main.py z atrapami mikrofonu, pamięci, mowy i panelu."""
    import main
    import panel

    stan = {"odpowiedzi": [], "stany": [], "nasluch": [], "zapisane": [], "rozmowy": []}
    monkeypatch.setattr(main, "pamiec", NS(poprzednia_rozmowa=lambda: [],
                                           ukryj_jesli_sekret=lambda t: t,
                                           zapisz_rozmowe=stan["zapisane"].append), raising=False)
    monkeypatch.setattr(main, "tts", NS(mow=lambda t: None), raising=False)
    monkeypatch.setattr(panel, "ty", lambda t: None)
    orb = NS(set_state=stan["stany"].append)
    return main, orb, stan


def ustaw_odpowiedzi(monkeypatch, main, stan, *wyniki):
    wyniki = list(wyniki)

    def odpowiedz(orb, tekst, historia, po_wake_wordzie=True, wszedl_w_slowo=False, powitanie=False):
        stan["odpowiedzi"].append((tekst, powitanie))
        return historia + [{"role": "user", "content": tekst}], wyniki.pop(0), False

    monkeypatch.setattr(main, "_odpowiedz", odpowiedz)


def nasluch(stan, *wypowiedzi):
    wypowiedzi = list(wypowiedzi)

    def sluchaj_bez_wake_worda(orb_callback=None, limit_ciszy_s=8, cicho=False):
        stan["nasluch"].append({"limit": limit_ciszy_s, "cicho": cicho})
        return wypowiedzi.pop(0) if wypowiedzi else None

    return NS(sluchaj_bez_wake_worda=sluchaj_bez_wake_worda, czy_zatrzymano=lambda: False)


@z_qt
def test_po_powitaniu_od_razu_zwykly_tryb(jarvis, monkeypatch):
    main, orb, stan = jarvis
    monkeypatch.setattr(main, "wake_word_listener", nasluch(stan), raising=False)
    ustaw_odpowiedzi(monkeypatch, main, stan, {"powiedzial": True})
    main.rozmowa(orb, "Hej, wróciłem", powitanie=True)
    assert stan["odpowiedzi"] == [("Hej, wróciłem", True)]
    assert stan["nasluch"] == []              # nie słucha dalej bez "Hey Jarvis"
    assert len(stan["zapisane"]) == 1         # rozmowa trafia do pamięci jak każda


@z_qt
def test_wejscie_w_slowo_w_trakcie_powitania_to_zwykla_rozmowa(jarvis, monkeypatch):
    main, orb, stan = jarvis
    monkeypatch.setattr(main, "wake_word_listener", nasluch(stan), raising=False)
    ustaw_odpowiedzi(monkeypatch, main, stan,
                     {"przerwane": True, "polecenie": "puść muzykę", "do_jarvisa": True, "uciete": True},
                     {"powiedzial": True, "koniec": True})
    main.rozmowa(orb, "Cześć Jarvis", powitanie=True)
    assert stan["odpowiedzi"] == [("Cześć Jarvis", True), ("puść muzykę", False)]


@z_qt
def test_okienko_w_main_slucha_cicho_i_wita(jarvis, monkeypatch):
    main, orb, stan = jarvis
    monkeypatch.setattr(main, "wake_word_listener", nasluch(stan, "Telewizor gada", "Cześć Jarvis"),
                        raising=False)
    monkeypatch.setattr(main, "rozmowa", lambda orb, tekst, powitanie=False:
                        stan["rozmowy"].append((tekst, powitanie)))
    main._okno_powitania(orb)
    assert stan["rozmowy"] == [("Cześć Jarvis", True)]
    assert all(n["cicho"] for n in stan["nasluch"]) and stan["nasluch"][0]["limit"] <= 600
    assert stan["stany"][0] == "powitanie" and stan["stany"][-1] == "idle"


@z_qt
def test_wylaczone_okienko_nie_slucha(jarvis, monkeypatch):
    main, orb, stan = jarvis
    monkeypatch.setenv(konfiguracja.POWITANIE_PO_STARCIE, "0")
    monkeypatch.setattr(main, "wake_word_listener", nasluch(stan, "Cześć Jarvis"), raising=False)
    main._okno_powitania(orb)
    assert stan["nasluch"] == []


@z_qt
def test_hud_ma_stan_powitania():
    import gui

    assert gui.STANY["powitanie"]["etykieta"] == "CZEKAM NA POWITANIE"


# --- Ustawienia: zakładka Nasłuch ---------------------------------------

@pytest.fixture
def bez_sladu_w_srodowisku(monkeypatch):
    """konfiguracja.zapisz ustawia os.environ — po teście te zmienne znikają."""
    for zmienna in (konfiguracja.POWITANIE_PO_STARCIE, konfiguracja.OKNO_POWITANIA,
                    konfiguracja.PROG_WAKE_WORDA):
        monkeypatch.setenv(zmienna, "")
        monkeypatch.delenv(zmienna)


@z_qt
def test_zakladka_nasluch_zapisuje_bez_klikania_dalej(monkeypatch, bez_sladu_w_srodowisku):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    import setup_wizard

    app = QApplication.instance() or QApplication([])
    okno = setup_wizard.KreatorJarvisa(pierwsze=False)
    try:
        okno._otworz_zakladke("Nasłuch")
        assert okno._stos.currentIndex() == okno._indeks_nasluchu and okno._zakladki["Nasłuch"].isChecked()
        assert okno._prog_wake_worda.text().startswith("automatycznie (0.20")
        okno._powitanie_wlaczone.setChecked(False)
        assert not okno._okno_powitania.isEnabled()
        okno._okno_powitania.setValue(20)
        okno._prog_wake_worda.setValue(0.35)
        # Prosto zakładką do Podsumowania — zmiany nie mogą przepaść.
        okno._otworz_zakladke("Podsumowanie")
        assert "Nasłuch" in okno._podsumowanie.text()
        okno._zakoncz()
    finally:
        okno.close()
        app.processEvents()
    assert not konfiguracja.powitanie_po_starcie()
    assert konfiguracja.okno_powitania_min() == 20 and konfiguracja.prog_wake_worda() == 0.35

    okno = setup_wizard.KreatorJarvisa(pierwsze=False)
    try:
        okno._prog_wake_worda.setValue(0.0)               # z powrotem "automatycznie"
        okno._zakoncz()
    finally:
        okno.close()
        app.processEvents()
    assert konfiguracja.prog_wake_worda() is None
