"""Czy testy na pewno nie dotykają Twoich plików, kluczy ani sieci (zasady z conftest.py)."""

import os
import socket

import pytest

import conftest
import email_monitor
import indeks_notatek
import konfiguracja
import modele
import notes
import pamiec
import pliki
import reminders
import sciezki
import slownik
import zdalny_kod


def _w_folderze_testow(sciezka):
    return os.path.normcase(os.path.abspath(sciezka)).startswith(
        os.path.normcase(conftest.FOLDER_TESTOW))


@pytest.mark.parametrize("sciezka", [
    pamiec.SCIEZKA_FAKTOW, pamiec.SCIEZKA_PAMIECI, reminders.SCIEZKA, notes.KATALOG_NOTATEK,
    email_monitor.SCIEZKA, slownik.SCIEZKA, konfiguracja.PLIK, konfiguracja.PLIK_PROJEKTU,
    zdalny_kod.FOLDER, indeks_notatek.PLIK_BAZY, modele.katalog_modelu_notatek(),
    pliki.PLIK_REJESTRU,
], ids=["memory.json", "pamiec.json", "reminders.json", "notatki", "email_watches.json",
        "slownik.json", "config.env", ".env", "zdalny", "indeks_notatek", "model notatek",
        "pliki_jarvisa.json"])
def test_pliki_danych_leza_w_folderze_tymczasowym(sciezka):
    assert _w_folderze_testow(sciezka)
    for prawdziwy in conftest.PRAWDZIWE_FOLDERY:
        assert os.path.normcase(os.path.dirname(sciezka)) != os.path.normcase(prawdziwy)


def test_nowy_plik_danych_tez_trafia_do_folderu_tymczasowego():
    assert _w_folderze_testow(sciezki.dane("cokolwiek.json"))


def test_w_srodowisku_nie_ma_kluczy():
    assert konfiguracja.tajne_wartosci() == {}
    assert not os.environ.get("ANTHROPIC_API_KEY")
    assert not konfiguracja.skonfigurowana("telegram")
    assert not konfiguracja.skonfigurowana("gmail")


def test_siec_jest_zablokowana():
    with pytest.raises(conftest.SiecZablokowana):
        socket.create_connection(("api.anthropic.com", 443), timeout=1)
