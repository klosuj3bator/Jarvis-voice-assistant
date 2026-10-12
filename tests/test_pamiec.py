"""Pamięć długoterminowa (pamiec.py, memory.json) i końcówka rozmowy (pamiec.json)."""

import json
import os
from types import SimpleNamespace as NS

import pytest

import agent
import pamiec


def test_zapamietaj_i_lista():
    assert pamiec.zapamietaj_fakt("Pracuję do szesnastej") == ("Zapamiętane: Pracuję do szesnastej", True)
    tekst, ok = pamiec.lista_faktow()
    assert ok and "- Pracuję do szesnastej (od " in tekst
    assert pamiec.fakty() == ["Pracuję do szesnastej"]


def test_fakty_sa_zapisane_na_dysku():
    pamiec.zapamietaj_fakt("Mam psa Burka")
    with open(pamiec.SCIEZKA_FAKTOW, encoding="utf-8") as plik:
        assert [f["fakt"] for f in json.load(plik)["fakty"]] == ["Mam psa Burka"]


def test_duplikat_bez_wielkosci_liter():
    pamiec.zapamietaj_fakt("Lubię jazz")
    assert pamiec.zapamietaj_fakt("lubię JAZZ") == ("To już pamiętam.", True)
    assert len(pamiec.fakty()) == 1


@pytest.mark.parametrize("sekret", [
    "moje hasło do banku to kot123",
    "PIN do karty 4521",
    "numer karty 4111 1111 1111 1111",
    "klucz API sk-ant-abc123def456ghi789",
    "kod do bramy 2580",
])
def test_sekrety_nigdy_nie_trafiaja_do_pamieci(sekret):
    komunikat, ok = pamiec.zapamietaj_fakt(sekret)
    assert not ok and komunikat.startswith("NIE ZAPISANO")
    assert not os.path.exists(pamiec.SCIEZKA_FAKTOW)


def test_zwykle_zdania_nie_sa_brane_za_sekret():
    assert not pamiec.wyglada_na_sekret("piszę kod w Pythonie")
    assert not pamiec.wyglada_na_sekret("ile tokenów zużyłem dzisiaj")
    assert pamiec.zapamietaj_fakt("Uczę się programowania w Pythonie")[1]


def test_za_dlugi_fakt():
    komunikat, ok = pamiec.zapamietaj_fakt("a" * (pamiec.MAKS_ZNAKOW_FAKTU + 1))
    assert not ok and komunikat.startswith("Za długie")


def test_pelna_pamiec_nie_kasuje_po_cichu(monkeypatch):
    monkeypatch.setattr(pamiec, "MAX_FAKTOW", 2)
    pamiec.zapamietaj_fakt("pierwszy fakt")
    pamiec.zapamietaj_fakt("drugi fakt")
    komunikat, ok = pamiec.zapamietaj_fakt("trzeci fakt")
    assert not ok and komunikat.startswith("Pamięć pełna")
    assert pamiec.fakty() == ["pierwszy fakt", "drugi fakt"]


def test_zapomnij_po_fragmencie():
    pamiec.zapamietaj_fakt("Pracuję do szesnastej")
    pamiec.zapamietaj_fakt("Mam psa Burka")
    assert pamiec.zapomnij_fakt("SZESNAST") == ("Zapomniane: Pracuję do szesnastej", True)
    assert pamiec.fakty() == ["Mam psa Burka"]
    assert pamiec.zapomnij_fakt("kot")[1] is False


def test_kontekst_promptu_odswieza_sie_po_zmianie():
    assert pamiec.kontekst_do_promptu() == ""
    pamiec.zapamietaj_fakt("Mam psa Burka")
    os.utime(pamiec.SCIEZKA_FAKTOW, (1, 1))    # inny czas zmiany niż przy pierwszym odczycie
    assert pamiec.kontekst_do_promptu().endswith("- Mam psa Burka")


def test_stare_fakty_z_pamiec_json_sa_przenoszone():
    with open(pamiec.SCIEZKA_PAMIECI, "w", encoding="utf-8") as plik:
        json.dump({"fakty": ["Stary fakt"], "zapisano": "2026-09-01T10:00:00",
                   "ostatnia_rozmowa": []}, plik)
    assert pamiec.fakty() == ["Stary fakt"]
    assert "fakty" not in pamiec.wczytaj()        # po przeniesieniu znika ze starego pliku


def test_uszkodzony_memory_json_to_pusta_pamiec():
    with open(pamiec.SCIEZKA_FAKTOW, "w", encoding="utf-8") as plik:
        plik.write("{zepsute")
    assert pamiec.fakty() == []


# --- zmiana pamięci tylko na wyraźną prośbę (agent.py) -------------------

@pytest.mark.parametrize("narzedzie, tekst, wolno", [
    ("remember_fact", "zapamiętaj, że pracuję do szesnastej", True),
    ("remember_fact", "Trzebnica", False),                  # odpowiedź na pytanie o pogodę
    ("forget_fact", "zapomnij, do której pracuję", True),
    ("forget_fact", "nie zapomnij o praniu", False),
])
def test_pamiec_zmienia_sie_tylko_na_prosbe(narzedzie, tekst, wolno):
    assert agent._prosil_o_zmiane_pamieci(narzedzie, tekst, []) is wolno


def test_tak_na_pytanie_jarvisa_wystarczy():
    historia = [{"role": "user", "content": "pracuję do szesnastej"},
                {"role": "assistant", "content": "Mam to zapamiętać?"}]
    assert agent._prosil_o_zmiane_pamieci("remember_fact", "tak", historia)


# --- końcówka rozmowy (pamiec.json) --------------------------------------

def test_zapis_rozmowy_bez_narzedzi_i_sekretow():
    historia = [
        {"role": "user", "content": "puść coś"},
        {"role": "assistant", "content": [NS(type="text", text="Już włączam."),
                                          NS(type="tool_use", id="t1", name="zagraj_piosenke", input={})]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
        {"role": "assistant", "content": [NS(type="text", text="Gra.")]},
        {"role": "user", "content": "moje hasło to tajne123"},
    ]
    pamiec.zapisz_rozmowe(historia)
    assert pamiec.poprzednia_rozmowa() == [
        {"role": "user", "content": "puść coś"},
        {"role": "assistant", "content": "Już włączam."},
        {"role": "assistant", "content": "Gra."},
        {"role": "user", "content": "[ukryte — wyglądało na hasło, klucz albo kod]"},
    ]


def test_zapis_rozmowy_zostawia_koncowke_od_wypowiedzi_uzytkownika():
    historia = []
    for numer in range(20):
        historia += [{"role": "user", "content": f"pytanie {numer}"},
                     {"role": "assistant", "content": [{"type": "text", "text": f"odpowiedź {numer}"}]}]
    pamiec.zapisz_rozmowe(historia)
    zapisane = pamiec.poprzednia_rozmowa()
    assert len(zapisane) == pamiec.ILE_ODTWARZAMY
    assert zapisane[0] == {"role": "user", "content": "pytanie 14"}
    assert zapisane[-1] == {"role": "assistant", "content": "odpowiedź 19"}
