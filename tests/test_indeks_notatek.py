"""
Przeszukiwanie notatek po znaczeniu (indeks_notatek.py, narzędzie search_notes).

Model embeddingów zastępuje atrapa OsadzaczSlow (wektor ze słów) — testy nie
pobierają modelu i niczego nie wysyłają. Jeden test na końcu sprawdza prawdziwy
model (multilingual-e5-small), jeśli jest już pobrany na tym komputerze.
"""

import datetime
import importlib.util
import os
import time

import numpy as np
import pytest

import agent
import conftest
import indeks_notatek
import modele
import notes
import pliki
from atrapy import KlientClaude, OsadzaczSlow, przebieg, tura
from indeks_notatek import Indeks, fragmenty_pliku

ZMIENIONO = datetime.datetime(2026, 10, 3, 12, 0).timestamp()


def zapisz(sciezka, tresc, czas=None):
    os.makedirs(os.path.dirname(sciezka), exist_ok=True)
    with open(sciezka, "w", encoding="utf-8") as plik:
        plik.write(tresc)
    if czas is not None:
        os.utime(sciezka, (czas, czas))


def notatka_glosowa(dzien, *linie):
    """Plik dnia w formacie notes.py: "- HH:MM — treść"."""
    zapisz(os.path.join(notes.KATALOG_NOTATEK, f"{dzien}.md"),
           f"# Notatki — {dzien}\n\n" + "".join(f"- {linia}\n" for linia in linie))


@pytest.fixture
def vault(tmp_path, monkeypatch):
    sciezka = tmp_path / "vault"
    sciezka.mkdir()
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(sciezka))
    return str(sciezka)


@pytest.fixture
def indeks(tmp_path):
    return Indeks(str(tmp_path / "indeks.sqlite"), OsadzaczSlow())


# --- dzielenie na fragmenty ----------------------------------------------

def test_notatka_glosowa_to_fragment_z_data_i_godzina():
    tekst = "# Notatki — 09.10.2026\n\n- 18:45 — oddać książkę\n- 19:02 — kupić mleko\nmoja uwaga\n"
    assert fragmenty_pliku(tekst, "2026-10-09", ZMIENIONO, notatki_jarvisa=True) == [
        ("notatka głosowa z 09.10.2026, 18:45", "oddać książkę", "oddać książkę"),
        ("notatka głosowa z 09.10.2026, 19:02", "kupić mleko", "kupić mleko"),
    ]


def test_markdown_dzieli_po_naglowkach_i_pomija_metadane():
    tekst = ("---\ntags: [projekt]\n---\n# Jarvis\nWstęp.\n\n## Pomysły\nRAG na notatkach.\n"
             "## Błędy\nPoprawić Telegram.\n# Inne\nCoś jeszcze.\n")
    fragmenty = fragmenty_pliku(tekst, "Projekty/Jarvis", ZMIENIONO)
    assert [(e, t) for e, t, _ in fragmenty] == [
        ("notatka „Projekty/Jarvis” › Jarvis, zmieniona 03.10.2026", "Wstęp."),
        ("notatka „Projekty/Jarvis” › Jarvis › Pomysły, zmieniona 03.10.2026", "RAG na notatkach."),
        ("notatka „Projekty/Jarvis” › Jarvis › Błędy, zmieniona 03.10.2026", "Poprawić Telegram."),
        ("notatka „Projekty/Jarvis” › Inne, zmieniona 03.10.2026", "Coś jeszcze."),
    ]
    assert "tags" not in str(fragmenty)
    # Do wektora idzie też tytuł i nagłówek — więcej sensu niż sam akapit.
    assert fragmenty[1][2] == "„Projekty/Jarvis” › Jarvis › Pomysły: RAG na notatkach."


def test_dzienna_notatka_obsidiana_ma_date_z_nazwy():
    (etykieta, _, _), = fragmenty_pliku("Dziś byłem na basenie.", "Dziennik/2026-09-28", ZMIENIONO)
    assert etykieta == "notatka „Dziennik/2026-09-28”, z 28.09.2026"


def test_dlugi_akapit_jest_ciety_po_zdaniach():
    zdanie = "To jest zdanie numer sto, które ma sporo znaków w środku. "
    fragmenty = fragmenty_pliku(zdanie * 40, "Długa", ZMIENIONO)
    assert len(fragmenty) > 1
    assert all(len(t) <= indeks_notatek.MAKS_ZNAKOW_FRAGMENTU for _, t, _ in fragmenty)
    assert all(t.endswith(".") for _, t, _ in fragmenty)


def test_pusty_plik_nie_daje_fragmentow():
    assert fragmenty_pliku("\n\n   \n", "Pusta", ZMIENIONO) == []


# --- skąd notatki ---------------------------------------------------------

def test_bez_vaulta_tylko_notatki_glosowe(monkeypatch):
    monkeypatch.delenv("OBSIDIAN_VAULT_PATH", raising=False)
    assert indeks_notatek.zrodla() == [(notes.KATALOG_NOTATEK, True)]


def test_nieistniejacy_vault_jest_pomijany(monkeypatch, tmp_path):
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", str(tmp_path / "nie-ma"))
    assert indeks_notatek.zrodla() == [(notes.KATALOG_NOTATEK, True)]


def test_vault_same_pliki_md_bez_folderow_obsidiana(vault):
    zapisz(os.path.join(vault, "Pomysły.md"), "Pomysł na grę.")
    zapisz(os.path.join(vault, "Projekty", "Jarvis.md"), "Asystent głosowy.")
    zapisz(os.path.join(vault, ".obsidian", "workspace.md"), "ustawienia edytora")
    zapisz(os.path.join(vault, ".trash", "stara.md"), "usunięta notatka")
    zapisz(os.path.join(vault, "obrazek.png"), "nie tekst")
    nazwy = sorted(nazwa for _, nazwa, *_ in indeks_notatek._pliki_notatek().values())
    assert nazwy == ["Pomysły", "Projekty/Jarvis"]


def test_notatki_glosowe_w_srodku_vaulta_licza_sie_raz(monkeypatch):
    monkeypatch.setenv("OBSIDIAN_VAULT_PATH", os.path.dirname(notes.KATALOG_NOTATEK))
    notatka_glosowa("2026-10-09", "18:45 — oddać książkę")
    pliki = indeks_notatek._pliki_notatek()
    assert len(pliki) == 1
    assert list(pliki.values())[0][2] is True          # jako notatka głosowa, z datą i godziną


# --- aktualizacja tylko zmienionych plików --------------------------------

def test_pierwsza_aktualizacja_indeksuje_wszystko(indeks, vault):
    notatka_glosowa("2026-10-09", "18:45 — oddać książkę", "19:00 — kupić mleko")
    zapisz(os.path.join(vault, "Auto.md"), "Wymiana opon w listopadzie.")
    assert indeks.aktualizuj() == {"nowe": 2, "zmienione": 0, "usuniete": 0, "bez_zmian": 0}
    assert indeks.liczba_fragmentow() == 3


def test_bez_zmian_nic_nie_jest_liczone_od_nowa(indeks, vault):
    zapisz(os.path.join(vault, "Auto.md"), "Wymiana opon w listopadzie.")
    indeks.aktualizuj()
    indeks._osadzacz.policzone.clear()
    assert indeks.aktualizuj()["bez_zmian"] == 1
    assert indeks._osadzacz.policzone == []


def test_zmieniony_plik_liczony_od_nowa_tylko_on(indeks, vault):
    zapisz(os.path.join(vault, "Auto.md"), "Wymiana opon w listopadzie.", czas=ZMIENIONO)
    zapisz(os.path.join(vault, "Dom.md"), "Pomalować płot.", czas=ZMIENIONO)
    indeks.aktualizuj()
    indeks._osadzacz.policzone.clear()
    zapisz(os.path.join(vault, "Auto.md"), "Wymiana oleju w grudniu.", czas=ZMIENIONO + 60)
    assert indeks.aktualizuj()["zmienione"] == 1
    assert indeks._osadzacz.policzone == ["passage: „Auto”: Wymiana oleju w grudniu."]
    assert indeks.szukaj("opon")[0][2] != "Wymiana opon w listopadzie."   # stary fragment zniknął


def test_nowa_data_bez_zmiany_tresci_nie_liczy_od_nowa(indeks, vault):
    sciezka = os.path.join(vault, "Auto.md")
    zapisz(sciezka, "Wymiana opon w listopadzie.", czas=ZMIENIONO)
    indeks.aktualizuj()
    indeks._osadzacz.policzone.clear()
    os.utime(sciezka, (ZMIENIONO + 3600, ZMIENIONO + 3600))      # np. plik skopiowany z kopii
    assert indeks.aktualizuj()["bez_zmian"] == 1
    assert indeks._osadzacz.policzone == []


def test_usuniety_plik_znika_z_indeksu(indeks, vault):
    zapisz(os.path.join(vault, "Auto.md"), "Wymiana opon w listopadzie.")
    zapisz(os.path.join(vault, "Dom.md"), "Pomalować płot.")
    indeks.aktualizuj()
    os.remove(os.path.join(vault, "Auto.md"))
    assert indeks.aktualizuj()["usuniete"] == 1
    assert [t for _, _, t in indeks.szukaj("wymiana opon", ile=5)] == ["Pomalować płot."]


def test_po_restarcie_jarvisa_indeks_jest_na_dysku(tmp_path, vault):
    zapisz(os.path.join(vault, "Auto.md"), "Wymiana opon w listopadzie.")
    Indeks(str(tmp_path / "i.sqlite"), OsadzaczSlow()).aktualizuj()
    po_restarcie = Indeks(str(tmp_path / "i.sqlite"), osadzacz := OsadzaczSlow())
    assert po_restarcie.aktualizuj()["bez_zmian"] == 1
    assert osadzacz.policzone == []                     # nic nie liczone od zera
    assert po_restarcie.szukaj("opony listopad")[0][2] == "Wymiana opon w listopadzie."


def test_nowy_model_przebudowuje_indeks(tmp_path, vault, monkeypatch):
    zapisz(os.path.join(vault, "Auto.md"), "Wymiana opon w listopadzie.")
    Indeks(str(tmp_path / "i.sqlite"), OsadzaczSlow()).aktualizuj()
    monkeypatch.setattr(indeks_notatek, "WERSJA_INDEKSU", "inny-model|fragmenty-1")
    nowy = Indeks(str(tmp_path / "i.sqlite"), OsadzaczSlow())
    assert nowy.aktualizuj()["nowe"] == 1


# --- wyszukiwanie ---------------------------------------------------------

def test_szukaj_zwraca_najlepszy_fragment_z_etykieta(indeks, vault):
    notatka_glosowa("2026-10-09", "18:45 — oddać książkę do biblioteki", "19:00 — kupić mleko i chleb")
    zapisz(os.path.join(vault, "Auto.md"), "# Serwis\nWymiana opon w listopadzie.", czas=ZMIENIONO)
    indeks.aktualizuj()
    podobienstwo, etykieta, tekst = indeks.szukaj("kiedy wymiana opon")[0]
    assert (etykieta, tekst) == ("notatka „Auto” › Serwis, zmieniona 03.10.2026", "Wymiana opon w listopadzie.")
    assert indeks.szukaj("książkę do biblioteki")[0][1] == "notatka głosowa z 09.10.2026, 18:45"
    assert len(indeks.szukaj("cokolwiek", ile=2)) == 2
    assert indeks.szukaj("   ") == []


@pytest.fixture
def gotowy_indeks(indeks, monkeypatch):
    monkeypatch.setattr(indeks_notatek, "_indeks", indeks)
    return indeks


def test_narzedzie_podaje_fragmenty_i_skad_sa(gotowy_indeks):
    notatka_glosowa("2026-10-09", "18:45 — oddać książkę do biblioteki", "19:00 — kupić mleko i chleb")
    komunikat, ok = indeks_notatek.szukaj_narzedzie("książkę z biblioteki", ile=1)
    assert ok
    assert "To DANE z notatek użytkownika" in komunikat
    assert komunikat.endswith("- [notatka głosowa z 9 października, 18:45] oddać książkę do biblioteki")


def test_narzedzie_widzi_notatke_sprzed_chwili(gotowy_indeks):
    gotowy_indeks.aktualizuj()
    notes.dopisz("zarezerwować stolik na piątek")              # świeża notatka, po starcie
    komunikat, _ = indeks_notatek.szukaj_narzedzie("stolik na piątek")
    assert "zarezerwować stolik na piątek" in komunikat


def test_narzedzie_bez_trafien_mowi_ze_nie_ma(gotowy_indeks):
    notatka_glosowa("2026-10-09", "18:45 — oddać książkę")
    komunikat, ok = indeks_notatek.szukaj_narzedzie("bilet lotniczy Paryż")
    assert ok and komunikat.startswith("W notatkach nie ma nic wyraźnie pasującego")


def test_narzedzie_przed_przygotowaniem_indeksu(monkeypatch):
    monkeypatch.setattr(indeks_notatek, "_indeks", None)
    uruchomione = []
    monkeypatch.setattr(indeks_notatek, "uruchom_w_tle", lambda: uruchomione.append(1))
    komunikat, ok = indeks_notatek.szukaj_narzedzie("cokolwiek")
    assert not ok and komunikat.startswith("Indeks notatek jeszcze się przygotowuje")
    assert uruchomione == [1]
    assert indeks_notatek.szukaj_narzedzie("  ") == ("Podaj, czego szukać w notatkach.", False)


def test_agent_odpowiada_na_podstawie_notatek(gotowy_indeks):
    notatka_glosowa("2026-10-09", "18:45 — oddać książkę do biblioteki")
    agent._klient = klient = KlientClaude(
        tura(narzedzia=[("n1", "search_notes", {"zapytanie": "co miałem oddać do biblioteki"})]),
        tura("Zanotowałeś dziewiątego października, że masz oddać książkę do biblioteki."),
    )
    zdania, _ = przebieg(agent.odpowiedz("co miałem oddać do biblioteki?", []))
    wynik = klient.zapytania[1]["messages"][-1]["content"][0]
    assert wynik["tool_use_id"] == "n1" and not wynik["is_error"]
    assert "[notatka głosowa z 9 października, 18:45] oddać książkę do biblioteki" in wynik["content"]
    assert zdania[-1].startswith("Zanotowałeś dziewiątego października")


# --- prawdziwy model (tylko odczyt, jeśli już pobrany) ---------------------

PRAWDZIWY_MODEL = os.path.join(conftest.PRAWDZIWY_FOLDER_MODELI, os.path.basename(modele.katalog_modelu_notatek()))


@pytest.mark.skipif(not all(os.path.exists(os.path.join(PRAWDZIWY_MODEL, p))
                            for p in modele.PLIKI_MODELU_NOTATEK.values()),
                    reason="model notatek nie jest pobrany na tym komputerze")
# Lekkie zależności testów (requirements-test.txt, GitHub Actions) celowo nie
# mają onnxruntime ani tokenizers — wtedy ten jeden test się pomija.
@pytest.mark.skipif(not all(importlib.util.find_spec(m) for m in ("onnxruntime", "tokenizers")),
                    reason="brak onnxruntime albo tokenizers (lekkie zależności testów)")
def test_prawdziwy_model_rozumie_polskie_znaczenie():
    osadzacz = indeks_notatek.Osadzacz(PRAWDZIWY_MODEL)
    notatki = ["Rachunek za prąd zapłacić do 15 października",
               "Samochód: wymiana opon na zimowe w listopadzie",
               "Trening: poniedziałek siłownia, środa basen"]
    wektory = osadzacz.wektory([indeks_notatek.PREFIKS_FRAGMENTU + x for x in notatki])
    assert wektory.shape == (3, 384)
    assert np.allclose(np.linalg.norm(wektory, axis=1), 1, atol=1e-4)
    # Bez wspólnych słów — sam sens: elektryczność ~ prąd, auto ~ samochód, ćwiczenia ~ trening.
    for pytanie, numer in (("kiedy opłata za elektryczność", 0), ("co z autem", 1), ("plan ćwiczeń", 2)):
        wektor = osadzacz.wektory([indeks_notatek.PREFIKS_PYTANIA + pytanie])[0]
        assert int(np.argmax(wektory @ wektor)) == numer, pytanie


# --- model wczytywany na żądanie i zwalniany z pamięci --------------------

class _TokenizerAtrapa:
    def encode_batch(self, teksty):
        from types import SimpleNamespace as NS
        return [NS(ids=[5, 6, 1], attention_mask=[1, 1, 0]) for _ in teksty]


class _SesjaAtrapa:
    def run(self, _, wejscie):
        return [np.ones((len(wejscie["input_ids"]), 3, 384), dtype=np.float32)]


def test_model_wczytuje_sie_przy_potrzebie_i_zwalnia_po_bezczynnosci(monkeypatch):
    wczytania = []
    monkeypatch.setattr(indeks_notatek.Osadzacz, "_wczytaj",
                        lambda self: wczytania.append(1) or (_TokenizerAtrapa(), _SesjaAtrapa(),
                                                             {"input_ids", "attention_mask"}))
    monkeypatch.setattr(indeks_notatek, "ZWOLNIJ_PO_S", 0.3)
    osadzacz = indeks_notatek.Osadzacz("bez-znaczenia")
    assert not osadzacz.wczytany() and wczytania == []          # nic na zapas przy starcie
    wektory = osadzacz.wektory(["a", "b"])
    assert wektory.shape == (2, 384) and np.allclose(np.linalg.norm(wektory, axis=1), 1)
    osadzacz.wektory(["c"])
    assert wczytania == [1]                                      # drugi raz już z pamięci
    for _ in range(40):
        if not osadzacz.wczytany():
            break
        time.sleep(0.05)
    assert not osadzacz.wczytany()                               # zwolniony po bezczynności
    osadzacz.wektory(["d"])
    assert wczytania == [1, 1]                                   # i wczytany ponownie, gdy trzeba


# --- premia za wspólne słowa ----------------------------------------------

def test_rdzenie_lacza_odmiane_i_pomijaja_puste_slowa():
    assert indeks_notatek.rdzenie("Co zanotowałem o samochodzie?") == {"samoc"}
    assert indeks_notatek.rdzenie("Samochód: wymiana opon") == {"samoc", "wymia", "opon"}
    assert indeks_notatek.rdzenie("Łódź") == {"lodz"}


def test_wspolne_slowa_przesuwaja_wynik_w_gore(indeks, vault, monkeypatch):
    zapisz(os.path.join(vault, "A.md"), "Opony zimowe trzeba wymienić.")
    zapisz(os.path.join(vault, "B.md"), "Kupić nowe opony do roweru.")
    indeks.aktualizuj()
    bez = {tekst: wynik for wynik, _, tekst in indeks.szukaj("zimowe opony", ile=2)}
    monkeypatch.setattr(indeks_notatek, "WAGA_SLOW", 0.0)
    surowe = {tekst: wynik for wynik, _, tekst in indeks.szukaj("zimowe opony", ile=2)}
    assert bez["Opony zimowe trzeba wymienić."] - surowe["Opony zimowe trzeba wymienić."] == pytest.approx(0.1)
    assert bez["Kupić nowe opony do roweru."] - surowe["Kupić nowe opony do roweru."] == pytest.approx(0.05)


# --- pliki utworzone przez Jarvisa (pliki.py) ----------------------------

@pytest.fixture
def pulpit(tmp_path, monkeypatch):
    sciezka = tmp_path / "Pulpit"
    sciezka.mkdir()
    monkeypatch.setenv("JARVIS_DOZWOLONE_FOLDERY", str(sciezka))
    monkeypatch.delenv("OBSIDIAN_VAULT_PATH", raising=False)
    return sciezka


def test_pliki_od_jarvisa_sa_przeszukiwane_a_reszta_pulpitu_nie(indeks, pulpit):
    zapisz(str(pulpit / "prywatne.md"), "Mój plan podróży do Japonii")
    pliki.wykonaj("create_file", {"sciezka": "Pulpit/zakupy.md", "tresc": "- mleko\n- chleb"}, "", 1)
    os.utime(pulpit / "zakupy.md", (ZMIENIONO, ZMIENIONO))
    indeks.aktualizuj()
    assert indeks.liczba_fragmentow() == 1                  # prywatne.md nie trafiło do indeksu
    (_, etykieta, tekst), = indeks.szukaj("mleko chleb", ile=5)
    assert (etykieta, tekst) == ("notatka „zakupy”, plik w folderze Pulpit, zmieniona 03.10.2026", "- mleko - chleb")


def test_przeniesiony_plik_od_jarvisa_zmienia_nazwe_w_indeksie(indeks, pulpit):
    pliki.wykonaj("create_file", {"sciezka": "Pulpit/zakupy.md", "tresc": "- mleko"}, "", 1)
    indeks.aktualizuj()
    pliki.wykonaj("move_files", {"pliki": ["Pulpit/zakupy.md"], "cel": "Pulpit/Listy"}, "", 2)
    assert indeks.aktualizuj()["usuniete"] == 1
    assert indeks.liczba_fragmentow() == 1
    assert indeks.szukaj("mleko")[0][1].startswith("notatka „zakupy”, plik w folderze Pulpit/Listy,")


def test_wynik_narzedzia_ma_nazwe_notatki_i_date(gotowy_indeks, pulpit):
    pliki.wykonaj("create_file", {"sciezka": "Pulpit/zakupy.md", "tresc": "- mleko i chleb"}, "", 1)
    os.utime(pulpit / "zakupy.md", (ZMIENIONO, ZMIENIONO))
    komunikat, ok = indeks_notatek.szukaj_narzedzie("mleko chleb")
    assert ok and "ZAWSZE podaj, skąd jest informacja — nazwę notatki i datę" in komunikat
    assert komunikat.splitlines()[-1] == "- [notatka „zakupy”, plik w folderze Pulpit, zmieniona 3 października] - mleko i chleb"


def test_daty_w_wyniku_tak_jak_sie_je_mowi():
    dzis = datetime.date(2026, 10, 13)
    assert indeks_notatek.po_ludzku("notatka głosowa z 13.10.2026, 18:45", dzis) == "notatka głosowa z dzisiaj, 18:45"
    assert indeks_notatek.po_ludzku("notatka „A”, zmieniona 12.10.2026", dzis) == "notatka „A”, zmieniona wczoraj"
    assert indeks_notatek.po_ludzku("notatka „A”, zmieniona 03.10.2026", dzis) == "notatka „A”, zmieniona 3 października"
    assert indeks_notatek.po_ludzku("notatka „A”, z 28.12.2025", dzis) == "notatka „A”, z 28 grudnia 2025"
