"""Notatki głosowe (notes.py): zapis do pliku dnia i odczyt."""

import datetime
import os

import notes

TERAZ = datetime.datetime(2026, 10, 9, 18, 45)


def _plik(dzien=TERAZ.date()):
    with open(os.path.join(notes.KATALOG_NOTATEK, f"{dzien.isoformat()}.md"), encoding="utf-8") as f:
        return f.read()


def test_pierwsza_notatka_zaklada_plik_z_naglowkiem():
    komunikat, ok = notes.dopisz("oddać książkę", teraz=TERAZ)
    assert ok
    assert "18:45" in komunikat
    assert _plik() == "# Notatki — 09.10.2026\n\n- 18:45 — oddać książkę\n"


def test_kolejne_notatki_sa_dopisywane_nie_nadpisywane():
    notes.dopisz("pierwsza", teraz=TERAZ)
    notes.dopisz("druga", teraz=TERAZ + datetime.timedelta(minutes=5))
    assert notes.wczytaj(TERAZ.date()) == [("18:45", "pierwsza"), ("18:50", "druga")]
    assert _plik().count("# Notatki") == 1


def test_wiele_linii_staje_sie_jedna_notatka():
    notes.dopisz("kupić\nmleko\n\n  i chleb", teraz=TERAZ)
    assert notes.wczytaj(TERAZ.date()) == [("18:45", "kupić mleko i chleb")]


def test_pusta_notatka_jest_odrzucana():
    assert notes.dopisz("   \n", teraz=TERAZ) == ("Pusta notatka — nie ma czego zapisać.", False)
    assert not os.path.exists(os.path.join(notes.KATALOG_NOTATEK, "2026-10-09.md"))


def test_odczyt_pomija_linie_dopisane_recznie():
    notes.dopisz("z Jarvisa", teraz=TERAZ)
    with open(os.path.join(notes.KATALOG_NOTATEK, "2026-10-09.md"), "a", encoding="utf-8") as f:
        f.write("Moja uwaga z Obsidiana\n- bez godziny\n")
    assert notes.wczytaj(TERAZ.date()) == [("18:45", "z Jarvisa")]


def test_kazdy_dzien_ma_osobny_plik():
    notes.dopisz("wczoraj", teraz=TERAZ - datetime.timedelta(days=1))
    notes.dopisz("dziś", teraz=TERAZ)
    assert notes.wczytaj(TERAZ.date()) == [("18:45", "dziś")]
    assert notes.wczytaj(TERAZ.date() - datetime.timedelta(days=1)) == [("18:45", "wczoraj")]


def test_z_dzisiaj_bez_notatek_to_nie_blad():
    assert notes.z_dzisiaj() == ("Brak notatek z dzisiaj.", True)


def test_z_dzisiaj_czyta_najwyzej_limit():
    teraz = datetime.datetime.now().replace(hour=8, minute=0)
    for numer in range(notes.LIMIT_ODCZYTU + 3):
        notes.dopisz(f"notatka {numer}", teraz=teraz + datetime.timedelta(minutes=numer))
    tekst, ok = notes.z_dzisiaj()
    assert ok
    assert tekst.startswith(f"Notatki z dzisiaj ({notes.LIMIT_ODCZYTU + 3}):")
    assert "(pozostałe 3 są w pliku)" in tekst
    assert f"notatka {notes.LIMIT_ODCZYTU}" not in tekst
