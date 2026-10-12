"""
Zdalne programowanie (zdalny_kod.py, /kod /zatwierdz /cofnij /restart).
Claude Code i Telegram to atrapy; git działa naprawdę, ale na repozytorium
tymczasowym — nie na repozytorium Jarvisa.
"""

import os
import shutil
import subprocess
import time
from types import SimpleNamespace as NS

import pytest

import agent
import telegram_bridge
import zdalny_kod
from atrapy import TelegramAPI

# --- agent nie ma drogi do Claude Code ----------------------------------

def test_agent_nie_ma_narzedzia_do_claude_code():
    nazwy = [n["name"] for n in agent.NARZEDZIA]
    assert not [n for n in nazwy if "claude" in n.lower() or "kod" in n.lower()]
    assert not any(getattr(v, "__name__", "") == "zdalny_kod" for v in vars(agent).values())


def test_polecenie_claude_code_jest_ograniczone():
    polecenie = zdalny_kod.polecenie_claude()
    assert "--dangerously-skip-permissions" not in polecenie
    assert polecenie[polecenie.index("--permission-mode") + 1] == "acceptEdits"
    assert polecenie[polecenie.index("--tools") + 1] == "Read,Glob,Grep,Edit,Write,Bash"
    # Bramka leży poza folderem projektu (tu: w folderze tymczasowym testów).
    assert not os.path.normcase(zdalny_kod.PLIK_BRAMKI).startswith(os.path.normcase(zdalny_kod.KATALOG))


# --- wynik pytesta -------------------------------------------------------

@pytest.mark.parametrize("linia, opis", [
    ("102 passed in 1.87s", "102 przeszło (1,9 s)"),
    ("2 failed, 100 passed in 2.10s", "2 nie przeszło, 100 przeszło (2,1 s)"),
    ("1 failed, 3 passed, 1 error in 0.50s", "1 nie przeszło, 3 przeszło, 1 z błędem (0,5 s)"),
])
def test_podsumowanie_pytesta_po_polsku(linia, opis):
    assert zdalny_kod._podsumowanie_pytesta(f"....F\n=== short test summary ===\n{linia}\n") == opis


class Proces:
    """Atrapa subprocess.Popen dla pytesta — bez uruchamiania czegokolwiek."""

    def __init__(self, wyjscie, kod):
        self.wyjscie, self.returncode, self.pid = wyjscie.encode("utf-8"), kod, 0

    def communicate(self, timeout=None):
        return self.wyjscie, b""


@pytest.fixture
def pytest_zwraca(monkeypatch, tmp_path):
    os.makedirs(tmp_path / "tests")
    monkeypatch.setattr(zdalny_kod, "KATALOG", str(tmp_path))

    def ustaw(wyjscie, kod):
        monkeypatch.setattr(zdalny_kod.subprocess, "Popen", lambda *a, **k: Proces(wyjscie, kod))
    return ustaw


def test_testy_przechodza(pytest_zwraca):
    pytest_zwraca("....\n4 passed in 0.10s\n", 0)
    assert zdalny_kod.uruchom_testy() == {"stan": "ok", "opis": "4 przeszło (0,1 s)", "nieudane": []}


def test_testy_nie_przechodza(pytest_zwraca):
    pytest_zwraca("..F\nFAILED tests/test_a.py::test_x - AssertionError: 1 != 2\n"
                  "1 failed, 2 passed in 0.30s\n", 1)
    wynik = zdalny_kod.uruchom_testy()
    assert wynik["stan"] == "blad" and wynik["nieudane"] == ["tests/test_a.py::test_x"]
    assert "❌" in zdalny_kod.opis_testow(wynik) and "• tests/test_a.py::test_x" in zdalny_kod.opis_testow(wynik)


def test_bez_pytesta_to_brak_a_nie_blad(pytest_zwraca):
    pytest_zwraca("C:\\python.exe: No module named pytest\n", 1)
    assert zdalny_kod.uruchom_testy()["stan"] == "brak"


# --- /restart a testy ----------------------------------------------------

@pytest.fixture
def most(monkeypatch):
    api = TelegramAPI()
    restarty = []
    most = telegram_bridge.MostTelegram("123:atrapa", 111, odpowiedz=None, restart=lambda: restarty.append(1))
    most._api = api
    most._offset = 5
    monkeypatch.setattr(zdalny_kod, "trwa", lambda: False)
    return most, api, restarty


def ustaw_testy(monkeypatch, stan):
    wynik = {"stan": stan, "opis": "1 nie przeszło, 9 przeszło (0,5 s)" if stan == "blad" else "10 przeszło",
             "nieudane": ["tests/test_a.py::test_x"] if stan == "blad" else []}
    monkeypatch.setattr(zdalny_kod, "testy_przed_restartem", lambda powiadom=None: wynik)


def test_restart_przy_przechodzacych_testach_od_razu(most, monkeypatch):
    most, api, restarty = most
    ustaw_testy(monkeypatch, "ok")
    most._obsluz_tekst("/restart", None, kiedy=int(time.time()) + 1)
    assert restarty == [1] and api.wyslane()[0].startswith("🔄 Restartuję się.")


def test_restart_przy_nieprzechodzacych_testach_czeka_na_tak(most, monkeypatch):
    most, api, restarty = most
    ustaw_testy(monkeypatch, "blad")
    most._obsluz_tekst("/restart", None, kiedy=int(time.time()) + 1)
    assert restarty == []
    ostrzezenie = api.wyslane()[-1]
    assert ostrzezenie.startswith("⚠️ Nie restartuję od razu") and "tests/test_a.py::test_x" in ostrzezenie
    most._obsluz_tekst("tak", None)
    assert restarty == [1] and "Mimo nieprzechodzących testów" in api.wyslane()[-1]


def test_restart_przy_nieprzechodzacych_testach_bez_tak_nie_rusza(most, monkeypatch):
    most, api, restarty = most
    ustaw_testy(monkeypatch, "blad")
    most._obsluz_tekst("/restart", None, kiedy=int(time.time()) + 1)
    most._obsluz_tekst("nie, czekaj", None)
    assert restarty == [] and api.wyslane()[-1] == "Anulowane, nic nie robię."


def test_restart_glosowka_nie_potwierdza(most, monkeypatch):
    most, api, restarty = most
    ustaw_testy(monkeypatch, "blad")
    most._obsluz_tekst("/restart", None, kiedy=int(time.time()) + 1)
    most._obsluz_glos({"file_id": "g", "duration": 2}, None)
    assert restarty == [] and api.wyslane()[-1].startswith("Anulowane.")


# --- /kod, /cofnij, /zatwierdz na tymczasowym repozytorium --------------

@pytest.fixture
def repo(monkeypatch, tmp_path):
    if shutil.which("git") is None:
        pytest.skip("brak gita")
    katalog = str(tmp_path / "repo")
    os.makedirs(katalog)

    def git(*argumenty):
        return subprocess.run(["git", *argumenty], cwd=katalog, capture_output=True, text=True,
                              encoding="utf-8", check=True).stdout.rstrip("\n")

    git("init", "-q")
    git("config", "user.name", "Test"), git("config", "user.email", "t@t"), git("config", "core.autocrlf", "false")
    for nazwa, tresc in {"a.py": "a = 1\n", "b.py": "b = 1\n"}.items():
        with open(os.path.join(katalog, nazwa), "w", encoding="utf-8", newline="\n") as plik:
            plik.write(tresc)
    git("add", "-A"), git("commit", "-qm", "start")
    monkeypatch.setattr(zdalny_kod, "KATALOG", katalog)
    monkeypatch.setattr(zdalny_kod.shutil, "which",
                        lambda nazwa, _which=shutil.which: _which(nazwa) if nazwa == "git" else "claude.exe")
    return NS(katalog=katalog, git=git)


def zapisz(repo, nazwa, tresc):
    with open(os.path.join(repo.katalog, nazwa), "w", encoding="utf-8", newline="\n") as plik:
        plik.write(tresc)


def czytaj(repo, nazwa):
    sciezka = os.path.join(repo.katalog, nazwa)
    return open(sciezka, encoding="utf-8").read() if os.path.exists(sciezka) else None


def zadanie_kod(repo, monkeypatch, zmiany):
    """/kod z atrapą Claude Code, która wprowadza `zmiany` ({plik: treść albo None})."""
    def claude(tresc):
        for nazwa, nowa in zmiany.items():
            os.remove(os.path.join(repo.katalog, nazwa)) if nowa is None else zapisz(repo, nazwa, nowa)
        return {"is_error": False, "result": "Zrobione.", "permission_denials": []}, None

    monkeypatch.setattr(zdalny_kod, "_uruchom_claude", claude)
    wyniki = []
    zdalny_kod.zlec("zmień kod", wyniki.append)
    for _ in range(200):
        if not zdalny_kod.trwa():
            break
        time.sleep(0.05)
    return wyniki[0]


def test_kod_raportuje_zmiany_i_testy(repo, monkeypatch):
    raport = zadanie_kod(repo, monkeypatch, {"b.py": "b = 2\n", "n.py": "nowy\n"})
    assert "• b.py (+1 −1)" in raport and "• n.py (nowy, +1 −0)" in raport
    assert "🧪 Testy: nie uruchomiłem — w projekcie nie ma folderu tests." in raport


def test_cofnij_zostawia_twoje_wlasne_zmiany(repo, monkeypatch):
    zapisz(repo, "a.py", "a = 1\nmoja_zmiana = True\n")          # Twoja praca, niezatwierdzona
    zadanie_kod(repo, monkeypatch, {"a.py": "a = 1\nmoja_zmiana = True\nclaude = 1\n",
                                    "b.py": None, "n.py": "nowy\n"})
    assert zdalny_kod.cofnij().startswith("↩️ Cofnąłem")
    assert czytaj(repo, "a.py") == "a = 1\nmoja_zmiana = True\n"
    assert czytaj(repo, "b.py") == "b = 1\n" and czytaj(repo, "n.py") is None


def test_zatwierdz_commituje_tylko_pliki_z_zadan(repo, monkeypatch):
    zapisz(repo, "moj.py", "mój plik\n")
    zadanie_kod(repo, monkeypatch, {"b.py": "b = 2\n"})
    assert zdalny_kod.zatwierdz("Zmiana b").startswith("✅ Zatwierdzone")
    assert repo.git("show", "--name-only", "--format=", "HEAD") == "b.py"
    assert repo.git("status", "--short") == "?? moj.py"


def test_zatwierdz_odmawia_gdy_w_pliku_jest_klucz(repo, monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "sk-sekret-12345678")
    zadanie_kod(repo, monkeypatch, {"b.py": "klucz = 'sk-sekret-12345678'\n"})
    assert zdalny_kod.zatwierdz().startswith("Nie zatwierdzam: w pliku b.py jest wartość klucza TEST_API_KEY")
    assert repo.git("log", "--format=%s") == "start"
