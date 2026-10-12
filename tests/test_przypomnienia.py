"""Przypomnienia i timery (reminders.py) — także zapis i odczyt po restarcie Jarvisa."""

import datetime
import importlib
import json

import pytest

import reminders


@pytest.fixture(autouse=True)
def bez_odbiorcow(monkeypatch):
    """Lista odbiorców (Telegram) jest globalna — każdy test zaczyna z pustą."""
    monkeypatch.setattr(reminders, "_odbiorcy", [])


def _zapisz_recznie(*wpisy):
    with open(reminders.SCIEZKA, "w", encoding="utf-8") as plik:
        json.dump({"przypomnienia": list(wpisy)}, plik)


def _restart():
    """Restart Jarvisa: moduł wczytuje się od nowa, w pamięci nie zostaje nic."""
    return importlib.reload(reminders)


# --- dodawanie ----------------------------------------------------------

def test_za_ile_minut():
    komunikat, ok = reminders.dodaj("pranie", za_minut=20)
    assert ok
    assert komunikat.startswith("Ustawione: pranie")
    assert "za 20 min" in komunikat
    (wpis,) = reminders.aktywne()
    kiedy = datetime.datetime.fromisoformat(wpis["kiedy"])
    assert 19.9 * 60 < (kiedy - datetime.datetime.now()).total_seconds() < 20.1 * 60


def test_timer_bez_tresci():
    komunikat, ok = reminders.dodaj(za_minut=5)
    assert ok and komunikat.startswith("Ustawione: timer")


def test_godzina_ktora_minela_to_jutro():
    teraz = datetime.datetime(2026, 10, 9, 20, 0)
    assert reminders._o_godzinie("7:30", teraz) == datetime.datetime(2026, 10, 10, 7, 30)
    assert reminders._o_godzinie("21:15", teraz) == datetime.datetime(2026, 10, 9, 21, 15)
    assert reminders._o_godzinie("21:15", teraz, jutro=True) == datetime.datetime(2026, 10, 10, 21, 15)


@pytest.mark.parametrize("zla", ["25:00", "7:61", "siódma", ""])
def test_zla_godzina(zla):
    komunikat, ok = reminders.dodaj("x", o_godzinie=zla) if zla else reminders.dodaj("x")
    assert not ok
    assert reminders.aktywne() == []


@pytest.mark.parametrize("minuty", [0, -5, "dużo"])
def test_zly_czas(minuty):
    assert reminders.dodaj("x", za_minut=minuty)[1] is False


def test_limit_przypomnien(monkeypatch):
    monkeypatch.setattr(reminders, "MAX_PRZYPOMNIEN", 2)
    reminders.dodaj("a", za_minut=1)
    reminders.dodaj("b", za_minut=2)
    komunikat, ok = reminders.dodaj("c", za_minut=3)
    assert not ok and "Mam już 2" in komunikat


# --- lista i anulowanie -------------------------------------------------

def test_lista_od_najblizszego():
    reminders.dodaj("później", za_minut=60)
    reminders.dodaj("wcześniej", za_minut=10)
    tekst, ok = reminders.lista()
    assert ok
    assert tekst.index("wcześniej") < tekst.index("później")


def test_anuluj_po_fragmencie_i_wszystkie():
    reminders.dodaj("pranie", za_minut=10)
    reminders.dodaj("trening", za_minut=20)
    assert reminders.anuluj("PRAN") == ("Anulowane: pranie.", True)
    assert [p["tresc"] for p in reminders.aktywne()] == ["trening"]
    assert reminders.anuluj("dentysta")[1] is False
    assert reminders.anuluj("wszystkie") == ("Anulowane: trening.", True)
    assert reminders.aktywne() == []


# --- zapis i odczyt po restarcie ----------------------------------------

def test_przypomnienie_przetrwa_restart():
    reminders.dodaj("zadzwonić do mamy", za_minut=30)
    przed = reminders.aktywne()
    po_restarcie = _restart()
    assert po_restarcie.aktywne() == przed
    assert po_restarcie.lista()[0].count("zadzwonić do mamy") == 1


def test_po_restarcie_zalegle_odzywa_sie_jako_spoznione():
    teraz = datetime.datetime.now()
    _zapisz_recznie({"id": "a1", "tresc": "pranie",
                     "kiedy": (teraz - datetime.timedelta(minutes=40)).isoformat(timespec="seconds")})
    modul = _restart()
    powiedziane, telefon = [], []
    modul._mow = powiedziane.append
    modul.dodaj_odbiorce(telefon.append)          # zamiast Telegrama
    for wpis in modul._zapadle(teraz):
        modul._wypowiedz(wpis, teraz)
    assert len(powiedziane) == 1
    assert powiedziane[0].startswith("Spóźnione przypomnienie z godziny")
    assert powiedziane[0].endswith("Przypomnienie: pranie.")
    assert telefon == powiedziane


def test_przypomnienie_odzywa_sie_tylko_raz_nawet_po_restarcie():
    teraz = datetime.datetime.now()
    _zapisz_recznie({"id": "a1", "tresc": "pranie", "kiedy": teraz.isoformat(timespec="seconds")})
    assert len(reminders._zapadle(teraz)) == 1
    # Program padł zaraz po wyjęciu przypomnienia — po restarcie go już nie ma.
    assert _restart()._zapadle(teraz) == []


def test_po_restarcie_bardzo_stare_sa_kasowane_a_przyszle_zostaja():
    teraz = datetime.datetime.now()
    stare = (teraz - datetime.timedelta(hours=reminders.TOLERANCJA_SPOZNIENIA_H + 1))
    przyszle = teraz + datetime.timedelta(hours=1)
    _zapisz_recznie({"id": "s", "tresc": "wczorajsze", "kiedy": stare.isoformat(timespec="seconds")},
                    {"id": "p", "tresc": "jutrzejsze", "kiedy": przyszle.isoformat(timespec="seconds")})
    modul = _restart()
    assert modul._zapadle(teraz) == []
    assert [p["tresc"] for p in modul.aktywne()] == ["jutrzejsze"]


def test_uszkodzony_plik_nie_zatrzymuje_jarvisa():
    with open(reminders.SCIEZKA, "w", encoding="utf-8") as plik:
        plik.write("{to nie jest json")
    modul = _restart()
    assert modul.aktywne() == []
    assert modul.dodaj("nowe", za_minut=5)[1] is True
