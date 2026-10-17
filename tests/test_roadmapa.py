"""
Tryb roadmapy (roadmapa.py): mapa zadań (MapaZadan.md) — ponumerowane linijki
z trudnością, części A i B — kolejka, modele, limity Claude Code, propozycje
z przyciskami, zadanie na gałęzi roadmap, /scal, /odrzuc, raport.

Git działa naprawdę — na tymczasowym repozytorium z gałęzią main i kopią
roboczą gałęzi roadmap. Claude Code, testy, Telegram i restart to atrapy:
nic nie zużywa limitów, nic nie wychodzi z komputera.
"""

import collections
import datetime
import importlib.util
import os
import re
import shutil
import subprocess
from types import SimpleNamespace as NS

import pytest

import agent
import konfiguracja
import roadmapa
import sciezki
import telegram_bridge
import zdalny_kod
from atrapy import TelegramAPI

W = roadmapa.STREFA
TUTAJ = os.path.dirname(os.path.abspath(__file__))

MAPA = """# Mapa zadań

Oznaczenia: [Ł] łatwe · [Ś] średnie · [T] trudne

```
1. [Ł] Przykład w bloku kodu — to nie zadanie
```

## CZĘŚĆ A — zadania samodzielne

### Etap 1. Start

1. [Ł] Pierwsze zadanie. (5)
2. [T] Drugie zadanie. (6)

### Etap 2. Dalej

3. [Ś] Trzecie zadanie.
"""


# --- MapaZadan.md ---------------------------------------------------------

def test_zadaniem_jest_tylko_ponumerowana_linijka():
    zadania = roadmapa.wczytaj_zadania(MAPA + "4. ✅ [Ł] Zrobione. (7)\n5. [x] [T] Też zrobione.\n"
                                       "6. Bez trudności\n")
    assert [z.numer for z in zadania] == [1, 2, 3, 4, 5, 6]          # nagłówki ## i ### to nie zadania
    pierwsze, drugie, trzecie, zrobione, tez_zrobione, bez = zadania
    assert (pierwsze.tytul, pierwsze.trudnosc, pierwsze.pomysl, pierwsze.etap) == (
        "Pierwsze zadanie.", "Ł", 5, "Etap 1. Start")
    assert (drugie.trudnosc, drugie.pomysl, trzecie.trudnosc, trzecie.pomysl) == ("T", 6, "Ś", None)
    assert zrobione.zrobione and tez_zrobione.zrobione and not pierwsze.zrobione
    assert (zrobione.tytul, zrobione.trudnosc) == ("Zrobione.", "Ł")
    assert bez.trudnosc is None                                      # brak — Jarvis zapyta
    assert all(z.czesc == "A" for z in zadania)


def test_oznacz_zrobione_zostawia_konce_linii():
    tekst = MAPA.replace("\n", "\r\n")
    nowy, znaleziono = roadmapa.oznacz_zrobione(tekst, "Drugie zadanie.", 2)
    assert znaleziono and "2. ✅ [T] Drugie zadanie. (6)\r\n" in nowy
    assert nowy.count("\r\n") == tekst.count("\r\n")
    assert roadmapa.wczytaj_zadania(nowy)[1].zrobione
    assert not roadmapa.oznacz_zrobione(tekst, "Przykład w bloku kodu — to nie zadanie", 1)[1]
    # Ta sama treść dwa razy — oznaczamy tę z właściwym numerem.
    podwojna = "1. [Ł] To samo.\n2. [Ł] To samo.\n"
    assert roadmapa.oznacz_zrobione(podwojna, "To samo.", 2)[0] == "1. [Ł] To samo.\n2. ✅ [Ł] To samo.\n"


def test_kolejka_pominiete_na_koncu():
    zadania = roadmapa.wczytaj_zadania(MAPA)
    stan = dict(roadmapa._nowy_stan(), pominiete=["Pierwsze zadanie."], zrobione=["Drugie zadanie."])
    assert [z.numer for z in roadmapa.kolejka(zadania, stan)] == [3, 1]


def test_model_wedlug_trudnosci(monkeypatch):
    latwe, trudne, srednie = roadmapa.wczytaj_zadania(MAPA)
    bez = roadmapa.wczytaj_zadania("7. Bez trudności\n")[0]
    # [Ł] i [Ś] — Sonnet 5.5, [T] — Opus 5.5, bez trudności — None (pytamy).
    assert [roadmapa.model_dla(z) for z in (latwe, srednie, trudne, bez)] == [
        "claude-sonnet-5-5", "claude-sonnet-5-5", "claude-opus-5-5", None]
    monkeypatch.setenv(konfiguracja.CLAUDE_MODEL_LATWE, "claude-haiku-5-5")
    monkeypatch.setenv(konfiguracja.CLAUDE_MODEL_TRUDNE, "nie model; rm -rf")      # śmieci — domyślny
    assert (roadmapa.model_dla(srednie), roadmapa.model_dla(trudne)) == ("claude-haiku-5-5", "claude-opus-5-5")
    assert konfiguracja.nazwa_modelu("claude-haiku-5-5") == "Haiku 5.5"


def test_licznik_kolejki():
    assert roadmapa.licznik(1, 100) == "zadanie 1 ze 100"
    assert roadmapa.licznik(2, 99) == "zadanie 2 z 99"
    assert roadmapa.licznik(1, 3) == "zadanie 1 z 3"


def test_kod_bierze_model_do_zadan_trudnych(monkeypatch):
    monkeypatch.setenv(konfiguracja.CLAUDE_MODEL_TRUDNE, "claude-opus-5")
    polecenie = zdalny_kod.polecenie_claude()
    assert polecenie[polecenie.index("--model") + 1] == "claude-opus-5" and "--continue" in polecenie


def test_godzina_raportu(monkeypatch):
    assert konfiguracja.raport_roadmapy() == (12, 0)
    for zapisana, godzina in (("8:30", (8, 30)), ("25:00", (12, 0)), ("abc", (12, 0))):
        monkeypatch.setenv(konfiguracja.ROADMAPA_RAPORT, zapisana)
        assert konfiguracja.raport_roadmapy() == godzina


def test_agent_nie_zna_roadmapy():
    assert not any(getattr(v, "__name__", "") == "roadmapa" for v in vars(agent).values())


# --- Twoja mapa: 100 zadań ------------------------------------------------
#
# Na zamrożonej kopii (tests/dane/MapaZadan_100.md), a nie na żywym pliku:
# po każdej Twojej zmianie mapy (np. 101. zadanie) test na żywym pliku by
# padł, a nieprzechodzące testy zatrzymują roadmapę. Żywy plik sprawdza
# osobny test — czy czyta się spójnie, bez liczenia zadań.

# Trudności zadań 1–100 po kolei, po dziesięć — przepisane z mapy z 17.10.
TRUDNOSCI_100 = ("ŁŁŁŁŚŁŁŁŁŁ" "ŁŁŚŚŁŁŁŚŚŚ" "ŚŚŚŁŁŁŁŁŚŚ" "ŚŚŚŁŁŚŚŚŚŚ" "ŚŚŁŁŁŚŚŚŚŚ"
                 "TŁŚŚŚŚŚŚTŁ" "ŁŁŚŚŁŁŁŚŚŚ" "ŚŚŚTŁŁŁŁŚŚ" "ŚŁŚŁŚŚŚŚŚŚ" "ŚTTTŚŚŚŚŚT")


def test_mapa_ma_dokladnie_100_zadan_z_poprawnymi_trudnosciami():
    with open(os.path.join(TUTAJ, "dane", "MapaZadan_100.md"), encoding="utf-8") as plik:
        zadania = roadmapa.wczytaj_zadania(plik.read())
    assert len(zadania) == 100
    assert [z.numer for z in zadania] == list(range(1, 101))
    assert "".join(z.trudnosc for z in zadania) == TRUDNOSCI_100
    assert collections.Counter(z.trudnosc for z in zadania) == {"Ś": 56, "Ł": 37, "T": 7}
    assert [z.numer for z in zadania if z.trudnosc == "T"] == [51, 59, 74, 92, 93, 94, 100]
    # Część B od zadania 86; nagłówki części i etapów nie są zadaniami.
    assert [z.numer for z in zadania if z.czesc == "B"] == list(range(86, 101))
    assert not [z for z in zadania if "CZĘŚĆ" in z.tytul or z.tytul.startswith(("Etap", "["))]
    # Treść bez numeru, trudności i odnośnika; odnośnik osobno.
    pierwsze, ostatnie = zadania[0], zadania[-1]
    assert pierwsze.tytul == "Pakiet diagnostyczny: logi, wersje i konfiguracja (bez kluczy) w jednym ZIP."
    assert (pierwsze.pomysl, ostatnie.pomysl, zadania[14].pomysl) == (79, 100, 5)
    assert all(z.pomysl is not None for z in zadania) and not any(z.zrobione for z in zadania)
    assert zadania[14].etap == "Etap 3. Rozmowa i głos"
    # Model: [Ł] i [Ś] — Sonnet 5.5, [T] — Opus 5.5.
    modele = collections.Counter(roadmapa.model_dla(z) for z in zadania)
    assert modele == {"claude-sonnet-5-5": 93, "claude-opus-5-5": 7}


def test_zywa_mapa_czyta_sie_spojnie():
    """Prawdziwy MapaZadan.md: każda ponumerowana linijka to zadanie, nic więcej."""
    sciezka = roadmapa._plik_mapy(sciezki.KOD)
    if not os.path.exists(sciezka):
        pytest.skip("w projekcie nie ma mapy zadań")
    with open(sciezka, encoding="utf-8") as plik:
        tekst = plik.read()
    poza_kodem = re.sub(r"```.*?```", "", tekst, flags=re.S)
    numerowane = [l for l in poza_kodem.splitlines() if re.match(r"^\s*(?:[-*]\s+)?\d{1,3}[.)]\s+\S", l)]
    zadania = roadmapa.wczytaj_zadania(tekst)
    assert len(zadania) == len(numerowane)
    assert len({z.numer for z in zadania}) == len(zadania)                         # numery bez powtórek
    assert all(z.trudnosc in ("Ł", "Ś", "T", None) for z in zadania)


# --- limity Claude Code ---------------------------------------------------

TERAZ = datetime.datetime(2026, 10, 16, 10, 0, tzinfo=W)


@pytest.mark.parametrize("tekst, teraz, kiedy", [
    ("Claude AI usage limit reached|1760630400", TERAZ,
     datetime.datetime.fromtimestamp(1760630400, W)),
    ("You've hit your limit · resets 3pm (Europe/Warsaw)", TERAZ, datetime.datetime(2026, 10, 16, 15, 0, tzinfo=W)),
    ("You've hit your limit · resets 3pm (Europe/Warsaw)", TERAZ.replace(hour=16),
     datetime.datetime(2026, 10, 17, 15, 0, tzinfo=W)),                         # 15:00 minęła — jutro
    ("You've hit your limit · resets 3pm (UTC)", TERAZ, datetime.datetime(2026, 10, 16, 17, 0, tzinfo=W)),
    ("Usage limit reached · limit resets Oct 20, 9am", TERAZ, datetime.datetime(2026, 10, 20, 9, 0, tzinfo=W)),
    ("5-hour limit reached ∙ resets at 15:30", TERAZ, datetime.datetime(2026, 10, 16, 15, 30, tzinfo=W)),
    ("Usage limit reached, resets in 2h 30m", TERAZ, TERAZ + datetime.timedelta(hours=2, minutes=30)),
    ("limit reached (resets 2026-10-17 00:00 UTC)", TERAZ, datetime.datetime(2026, 10, 17, 2, 0, tzinfo=W)),
    ("Usage limit reached", TERAZ, None),                                      # bez godziny — co 30 min
])
def test_kiedy_limity_wroca(tekst, teraz, kiedy):
    assert roadmapa.kiedy_limity_wroca(tekst, teraz) == kiedy


def test_komunikat_limitu():
    limit = "You've hit your limit · resets 3pm (Europe/Warsaw)"
    assert roadmapa.komunikat_limitu({"is_error": True, "result": limit}) == limit
    assert roadmapa.komunikat_limitu(None, "Error: " + limit) == "Error: " + limit
    # Udane zadanie może pisać o limitach w podsumowaniu — to nie limit.
    assert roadmapa.komunikat_limitu({"is_error": False, "result": "Dodałem obsługę: " + limit}) is None
    assert roadmapa.komunikat_limitu({"is_error": True, "result": "Context limit reached · /compact"}) is None
    assert roadmapa.komunikat_limitu({"is_error": True, "result": "API Error: 500"}) is None


# --- zadania na tymczasowym repozytorium ----------------------------------

class Kanal:
    """Atrapa mostu do Telegrama: zapisuje wiadomości i przyciski."""

    def __init__(self):
        self.wiadomosci = {}         # id -> {"tekst", "przyciski"}
        self.kolejnosc = []

    def wyslij(self, tekst, przyciski=None):
        numer = len(self.wiadomosci) + 1
        self.wiadomosci[numer] = {"tekst": tekst, "przyciski": przyciski or []}
        self.kolejnosc.append(numer)
        return numer

    def edytuj(self, numer, tekst, przyciski=None):
        self.wiadomosci[numer] = {"tekst": tekst, "przyciski": przyciski or []}

    def usun_przyciski(self, numer):
        self.wiadomosci[numer]["przyciski"] = []

    def teksty(self):
        return [self.wiadomosci[n]["tekst"] for n in self.kolejnosc]

    def ostatnia(self):
        return self.wiadomosci[self.kolejnosc[-1]]["tekst"]

    def napisy(self, numer=None):
        numer = numer or self.kolejnosc[-1]
        return [t for wiersz in self.wiadomosci[numer]["przyciski"] for t, _ in wiersz]

    def kliknij(self, napis, numer=None):
        """Kliknięcie przycisku zaczynającego się od `napis` (w ostatniej wiadomości z przyciskami)."""
        numery = [numer] if numer else [n for n in reversed(self.kolejnosc) if self.wiadomosci[n]["przyciski"]]
        for n in numery:
            for wiersz in self.wiadomosci[n]["przyciski"]:
                for tekst, dane in wiersz:
                    if tekst.lstrip("• ").startswith(napis) or napis in tekst:
                        roadmapa.przycisk(dane, n)
                        return n
        raise AssertionError(f"nie ma przycisku {napis!r}; wiadomości: {self.teksty()}")


@pytest.fixture
def jarvis(monkeypatch, tmp_path):
    """Repozytorium Jarvisa (main) + roadmapa z atrapami Claude Code, testów i Telegrama."""
    if shutil.which("git") is None:
        pytest.skip("brak gita")
    katalog = str(tmp_path / "jarvis")
    os.makedirs(katalog)

    def git(*argumenty, w=katalog):
        return subprocess.run(["git", *argumenty], cwd=w, capture_output=True, text=True,
                              encoding="utf-8", check=True).stdout.rstrip("\n")

    git("init", "-q", "-b", "main")
    git("config", "user.name", "Test"), git("config", "user.email", "t@t"), git("config", "core.autocrlf", "false")
    for nazwa, tresc in {"a.py": "a = 1\n", "MapaZadan.md": MAPA}.items():
        with open(os.path.join(katalog, nazwa), "w", encoding="utf-8", newline="\n") as plik:
            plik.write(tresc)
    git("add", "-A"), git("commit", "-qm", "start")

    monkeypatch.setattr(zdalny_kod, "KATALOG", katalog)
    monkeypatch.setattr(zdalny_kod.shutil, "which",
                        lambda nazwa, _which=shutil.which: _which(nazwa) if nazwa == "git" else "claude.exe")
    monkeypatch.setattr(roadmapa, "KOPIA", str(tmp_path / "kopia"))
    monkeypatch.setenv(konfiguracja.ROADMAPA, "1")

    stan = NS(claude=[], wyniki=[], testy=[], wynik_testow={"stan": "ok", "opis": "10 przeszło", "nieudane": []},
              restarty=[], katalog=katalog, kopia=roadmapa.KOPIA, git=git)

    def claude(tresc, polecenie, katalog_claude, limit_s, przy_starcie=None):
        stan.claude.append({"tresc": tresc, "polecenie": polecenie, "katalog": katalog_claude})
        wynik = stan.wyniki.pop(0) if stan.wyniki else {"zmiany": {"a.py": "a = 2\n"}}
        for nazwa, nowa in wynik.get("zmiany", {}).items():
            with open(os.path.join(katalog_claude, nazwa), "w", encoding="utf-8", newline="\n") as plik:
                plik.write(nowa)
        return wynik.get("json", {"is_error": False, "result": "Zrobione."}), wynik.get("blad"), wynik.get("bledy", "")

    def testy(commit=None, katalog=None):
        stan.testy.append(katalog)
        return dict(stan.wynik_testow)

    monkeypatch.setattr(zdalny_kod, "uruchom_claude_code", claude)
    monkeypatch.setattr(zdalny_kod, "uruchom_testy", testy)
    monkeypatch.setattr(roadmapa, "_w_tle", lambda tryb: roadmapa._praca(tryb))     # bez wątków
    monkeypatch.setattr(roadmapa, "_sprawdz_potrzeby_w_tle", lambda pid: roadmapa._sprawdz_potrzeby(pid))
    kanal = Kanal()
    roadmapa.uruchom(kanal, restart=lambda dopisek="": stan.restarty.append(dopisek), watek=False)
    stan.kanal = kanal
    yield stan
    zdalny_kod.zwolnij()
    roadmapa._kanal = roadmapa._restart = None


def plik(sciezka):
    with open(sciezka, encoding="utf-8") as f:
        return f.read()


def ustaw_mape(jarvis, tekst):
    """Twoja nowa mapa zadań — commit na main (Jarvis dociągnie ją do roadmap)."""
    with open(os.path.join(jarvis.katalog, "MapaZadan.md"), "w", encoding="utf-8", newline="\n") as f:
        f.write(tekst)
    jarvis.git("commit", "-qam", "nowa mapa zadań")


def test_propozycja_krotka_numer_trudnosc_tresc_model(jarvis):
    roadmapa.krok(TERAZ)
    tekst = jarvis.kanal.ostatnia()
    assert tekst == ("🗺 Tryb roadmapy włączony.\n\n"
                     "🗺 Zadanie 1 z 3 · nr 1 [Ł]\n\nPierwsze zadanie.\n\n🧠 Model: Sonnet 5.5")
    assert "(5)" not in tekst and "Etap" not in tekst and "CZĘŚĆ" not in tekst      # bez fragmentów pliku
    assert jarvis.kanal.napisy()[:3] == ["✅ Akceptuj", "⏭ Pomiń", "📋 Pokaż inne"]
    roadmapa.krok(TERAZ + datetime.timedelta(minutes=1))
    assert len(jarvis.kanal.kolejnosc) == 1             # tylko raz, przy włączeniu


def test_licznik_ze_100_liczy_tylko_nieukonczone(jarvis):
    mapa = "## CZĘŚĆ A\n" + "".join(f"{n}. [Ł] Zadanie {n}.\n" for n in range(1, 103))
    ustaw_mape(jarvis, mapa.replace("1. [Ł] Zadanie 1.", "1. ✅ [Ł] Zadanie 1.")
               .replace("2. [Ł] Zadanie 2.", "2. ✅ [Ł] Zadanie 2."))
    roadmapa.zaproponuj()
    assert jarvis.kanal.ostatnia().startswith("🗺 Zadanie 1 ze 100 · nr 3 [Ł]")


def test_zadanie_na_galezi_roadmap_i_od_razu_kolejna_propozycja(jarvis):
    roadmapa.zaproponuj()
    jarvis.kanal.kliknij("✅ Akceptuj")

    wywolanie, = jarvis.claude
    polecenie = wywolanie["polecenie"]
    assert wywolanie["katalog"] == jarvis.kopia
    assert "Zadanie nr 1 z mapy zadań Jarvisa (MapaZadan.md): Pierwsze zadanie." in wywolanie["tresc"]
    assert "Trudność: łatwe" in wywolanie["tresc"] and "pomysł nr 5 w POMYSLY_JARVIS.md" in wywolanie["tresc"]
    assert "Etap: Etap 1. Start" in wywolanie["tresc"]
    assert polecenie[polecenie.index("--model") + 1] == "claude-sonnet-5-5"
    assert "--continue" not in polecenie and "--dangerously-skip-permissions" not in polecenie
    assert "Edit(./MapaZadan.md)" in polecenie and polecenie[polecenie.index("--permission-mode") + 1] == "acceptEdits"

    # Commit na roadmap; main i folder Jarvisa nietknięte; zadanie oznaczone w mapie.
    assert jarvis.git("log", "-1", "--format=%s", "roadmap") == "Roadmapa: nr 1 Pierwsze zadanie."
    assert jarvis.git("log", "--format=%s", "main") == "start"
    assert plik(os.path.join(jarvis.katalog, "a.py")) == "a = 1\n"
    assert plik(os.path.join(jarvis.kopia, "a.py")) == "a = 2\n"
    assert "1. ✅ [Ł] Pierwsze zadanie. (5)" in plik(os.path.join(jarvis.kopia, "MapaZadan.md"))
    assert jarvis.git("status", "--porcelain", w=jarvis.kopia) == ""

    teksty = jarvis.kanal.teksty()
    raport = next(t for t in teksty if t.startswith("✅ Zrobione"))
    assert "• a.py (+1 −1)" in raport and "🧪 Testy: ✅ 10 przeszło." in raport and "/scal" in raport
    # Kolejna propozycja: [T] — Opus 5.5, licznik liczy już tylko nieukończone.
    assert jarvis.kanal.ostatnia() == "🗺 Zadanie 1 z 2 · nr 2 [T]\n\nDrugie zadanie.\n\n🧠 Model: Opus 5.5"
    assert jarvis.restarty == []                        # sam nigdy nie restartuje


def test_bez_trudnosci_pyta_o_model_zamiast_zgadywac(jarvis):
    ustaw_mape(jarvis, "## CZĘŚĆ A\n7. Zadanie bez trudności.\n")
    roadmapa.zaproponuj()
    assert "❓ brak trudności w mapie — wybierz model" in jarvis.kanal.ostatnia()
    assert "✅ Akceptuj" not in jarvis.kanal.napisy() and "Sonnet 5.5" in jarvis.kanal.napisy()
    assert jarvis.claude == []
    jarvis.kanal.kliknij("Sonnet 5.5")
    assert jarvis.kanal.ostatnia().endswith("🧠 Model: Sonnet 5.5") and "✅ Akceptuj" in jarvis.kanal.napisy()
    jarvis.kanal.kliknij("✅ Akceptuj")
    assert jarvis.claude[0]["polecenie"][jarvis.claude[0]["polecenie"].index("--model") + 1] == "claude-sonnet-5-5"


def test_przed_startem_niezatwierdzone_zmiany_w_folderze_jarvisa(jarvis):
    with open(os.path.join(jarvis.katalog, "a.py"), "w", encoding="utf-8") as f:
        f.write("a = 5\n")
    roadmapa.zaproponuj()
    jarvis.kanal.kliknij("✅ Akceptuj")
    assert jarvis.claude == [] and "niezatwierdzone zmiany (a.py)" in jarvis.kanal.ostatnia()
    assert roadmapa._stan()["zadanie"] is None and not zdalny_kod.zajety()


def test_przed_startem_testy_musza_przechodzic(jarvis):
    jarvis.wynik_testow = {"stan": "blad", "opis": "1 nie przeszło", "nieudane": ["tests/test_a.py::test_x"]}
    roadmapa.zaproponuj()
    jarvis.kanal.kliknij("✅ Akceptuj")
    assert jarvis.claude == []
    assert "tests/test_a.py::test_x" in jarvis.kanal.ostatnia() and "🔧 Napraw testy" in jarvis.kanal.napisy()
    # Naprawa: model do trudnych, nazwy nieudanych testów w treści, mapa zadań bez zmian.
    jarvis.wynik_testow = {"stan": "ok", "opis": "10 przeszło", "nieudane": []}
    jarvis.kanal.kliknij("🔧 Napraw testy")
    naprawa, = jarvis.claude
    assert "tests/test_a.py::test_x" in naprawa["tresc"] and "claude-opus-5-5" in naprawa["polecenie"]
    assert jarvis.git("log", "-1", "--format=%s", "roadmap") == "Roadmapa: Naprawa testów"
    assert "✅" not in plik(os.path.join(jarvis.kopia, "MapaZadan.md"))


def test_pomin_pokaz_inne_i_zmiana_modelu(jarvis):
    roadmapa.zaproponuj()
    jarvis.kanal.kliknij("⏭ Pomiń")
    assert "Pominięte: „Pierwsze zadanie.”" in jarvis.kanal.wiadomosci[1]["tekst"]
    assert "nr 2 [T]" in jarvis.kanal.ostatnia()
    numer = jarvis.kanal.kliknij("📋 Pokaż inne")
    assert jarvis.kanal.napisy(numer) == [
        "nr 3 [Ś] Trzecie zadanie.", "nr 1 [Ł] Pierwsze zadanie.", "↩️ Wróć"]     # pominięte na końcu
    jarvis.kanal.kliknij("nr 3 [Ś]", numer)
    assert "nr 3 [Ś]\n\nTrzecie zadanie." in jarvis.kanal.wiadomosci[numer]["tekst"]
    jarvis.kanal.kliknij("🧠 Zmień model", numer)
    jarvis.kanal.kliknij("Haiku 5.5", numer)
    assert "Haiku 5.5 (zmieniony; domyślnie Sonnet 5.5)" in jarvis.kanal.wiadomosci[numer]["tekst"]
    jarvis.kanal.kliknij("✅ Akceptuj", numer)
    assert "claude-haiku-5-5" in jarvis.claude[0]["polecenie"]
    assert jarvis.git("log", "-1", "--format=%s", "roadmap") == "Roadmapa: nr 3 Trzecie zadanie."


def test_stary_przycisk_nic_nie_robi(jarvis):
    roadmapa.zaproponuj()
    stara = jarvis.kanal.kolejnosc[-1]
    roadmapa.dalej()                                    # nowa propozycja, stara nieaktualna
    jarvis.kanal.wiadomosci[stara]["przyciski"] = [[("✅ Akceptuj", f"rm|a|{'0' * 6}")]]
    jarvis.kanal.kliknij("✅ Akceptuj", stara)
    assert jarvis.claude == [] and "nieaktualna" in jarvis.kanal.wiadomosci[stara]["tekst"]


def test_jedno_zadanie_claude_code_naraz(jarvis):
    assert zdalny_kod.zarezerwuj("zadanie /kod „inne”")
    roadmapa.zaproponuj()
    jarvis.kanal.kliknij("✅ Akceptuj")
    assert jarvis.claude == [] and jarvis.kanal.ostatnia().startswith("Trwa zadanie /kod „inne”")
    zdalny_kod.zwolnij()
    # I odwrotnie: gdy trwa roadmapa, /kod czeka.
    assert zdalny_kod.zarezerwuj("zadanie z roadmapy „x”")
    assert zdalny_kod.zlec("zmień coś", lambda t: None).startswith("Trwa zadanie z roadmapy „x”")


def test_limity_przerywaja_i_czekaja_na_zgode(jarvis, monkeypatch):
    jarvis.wyniki.append({"zmiany": {"a.py": "a = 'w połowie'\n"},
                          "json": {"is_error": True, "result": "You've hit your limit · resets 3pm (Europe/Warsaw)"}})
    roadmapa.zaproponuj()
    jarvis.kanal.kliknij("✅ Akceptuj")
    komunikat = jarvis.kanal.ostatnia()
    assert komunikat.startswith("⏸ Skończyły się limity") and "Limity wrócą ok. 15:00" in komunikat
    assert jarvis.git("log", "-1", "--format=%s", "roadmap") == "start"              # bez commita
    assert plik(os.path.join(jarvis.kopia, "a.py")) == "a = 'w połowie'\n"          # miejsce przerwania
    assert roadmapa._stan()["zadanie"]["stan"] == "limit" and not zdalny_kod.zajety()

    sprawdzenia = []
    monkeypatch.setattr(roadmapa, "_sprawdz_limity", lambda model: sprawdzenia.append(model) or (True, None))
    roadmapa.krok(datetime.datetime.now(W).replace(hour=14, minute=0) if datetime.datetime.now(W).hour < 14
                  else datetime.datetime.now(W) - datetime.timedelta(days=1))
    assert sprawdzenia == []                             # przed 15:00 nie pyta
    roadmapa.krok(datetime.datetime.now(W) + datetime.timedelta(days=1, hours=1))
    assert sprawdzenia == ["claude-sonnet-5-5"]
    assert jarvis.kanal.ostatnia() == "✅ Limity Claude Code wróciły — kontynuujemy zadanie „Pierwsze zadanie.”?"
    assert jarvis.claude[1:] == []                       # czeka na Twoją zgodę
    jarvis.kanal.kliknij("▶️ Kontynuuj")
    wznowienie = jarvis.claude[1]
    assert "--continue" in wznowienie["polecenie"] and "Wracamy do przerwanego zadania" in wznowienie["tresc"]
    assert jarvis.git("log", "-1", "--format=%s", "roadmap") == "Roadmapa: nr 1 Pierwsze zadanie."


def test_wygasle_logowanie_claude_code(jarvis):
    # Tak kończy się Claude Code, gdy logowanie wygaśnie (sprawdzone 16.10 na żywo).
    jarvis.wyniki.append({"zmiany": {}, "json": {
        "is_error": True, "result": "Failed to authenticate: OAuth session expired and could not be refreshed"}})
    roadmapa.zaproponuj()
    jarvis.kanal.kliknij("✅ Akceptuj")
    blad = next(t for t in jarvis.kanal.teksty() if t.startswith("⚠️ Claude Code zakończył się błędem"))
    assert "🔑 Claude Code nie jest zalogowany" in blad and "/login" in blad
    assert roadmapa._stan()["zadanie"] is None and "nr 1 [Ł]" in jarvis.kanal.ostatnia()
    assert "limity" not in blad.lower()                  # to nie limit — nie czekamy na odnowienie


def test_limity_bez_godziny_sprawdzane_co_30_minut(jarvis):
    jarvis.wyniki.append({"json": None, "blad": "zakończył się kodem 1", "bledy": "Claude AI usage limit reached"})
    roadmapa.zaproponuj()
    jarvis.kanal.kliknij("✅ Akceptuj")
    assert "sprawdzam co 30 minut" in jarvis.kanal.ostatnia()
    za = roadmapa._stan()["limit"]["sprawdz"] - datetime.datetime.now(W).timestamp()
    assert 29 * 60 < za <= 30 * 60


def test_przerwane_przy_zamknieciu_jarvisa(jarvis):
    stan = roadmapa._stan()
    stan["zadanie"] = {"pid": "a1", "tytul": "Pierwsze zadanie.", "numer": 1, "model": "claude-opus-5-5",
                       "stan": "trwa", "od": 0, "naprawa": False, "powod": ""}
    roadmapa._zapisz(stan)
    roadmapa._przygotuj_kopie()
    with open(os.path.join(jarvis.kopia, "a.py"), "w", encoding="utf-8") as f:
        f.write("a = 'niedokończone'\n")
    roadmapa.uruchom(jarvis.kanal, watek=False)          # start Jarvisa
    assert "Jarvis został zamknięty w trakcie zadania" in jarvis.kanal.ostatnia()
    assert "nr 1 „Pierwsze zadanie.”" in jarvis.kanal.ostatnia()
    jarvis.kanal.kliknij("🗑 Porzuć zadanie")
    assert plik(os.path.join(jarvis.kopia, "a.py")) == "a = 1\n"
    assert jarvis.git("for-each-ref", "--format=%(refname)", "refs/jarvis/roadmapa-porzucone/")
    assert roadmapa._stan()["pominiete"] == ["Pierwsze zadanie."]                   # na koniec kolejki
    assert "nr 2 [T]" in jarvis.kanal.ostatnia()


def test_odrzuc_wraca_do_main_a_zadania_do_kolejki(jarvis):
    roadmapa.zaproponuj()
    jarvis.kanal.kliknij("✅ Akceptuj")
    roadmapa.komenda("odrzuc")
    assert jarvis.kanal.ostatnia().startswith("🗑 Odrzucić z gałęzi roadmap")
    jarvis.kanal.kliknij("🗑 Tak, odrzuć")
    assert jarvis.git("rev-parse", "roadmap") == jarvis.git("rev-parse", "main")
    kopia = jarvis.git("for-each-ref", "--format=%(refname)", "refs/jarvis/roadmapa-odrzucone/")
    assert kopia and "a = 2" in jarvis.git("show", f"{kopia}:a.py")
    assert "Zadanie 1 z 3 · nr 1 [Ł]" in jarvis.kanal.ostatnia()          # wróciło do kolejki


def test_scal_laczy_z_main_i_restartuje_ze_straznikiem(jarvis):
    roadmapa.zaproponuj()
    jarvis.kanal.kliknij("✅ Akceptuj")
    roadmapa.komenda("scal")
    assert jarvis.git("log", "-1", "--format=%s", "main").startswith("Roadmapa: scalenie 1 commitów")
    assert plik(os.path.join(jarvis.katalog, "a.py")) == "a = 2\n"
    assert "1. ✅ [Ł] Pierwsze zadanie." in plik(os.path.join(jarvis.katalog, "MapaZadan.md"))  # i na main
    assert jarvis.restarty == [" Po /scal: nowa wersja z roadmapy."]
    assert "nie ma czego scalać" in (roadmapa.komenda("scal") or jarvis.kanal.ostatnia())


def test_scal_odmawia_przy_niezatwierdzonych_zmianach(jarvis):
    roadmapa.zaproponuj()
    jarvis.kanal.kliknij("✅ Akceptuj")
    with open(os.path.join(jarvis.katalog, "nowy.py"), "w", encoding="utf-8") as f:
        f.write("x = 1\n")
    roadmapa.komenda("scal")
    assert "niezatwierdzone zmiany (nowy.py)" in jarvis.kanal.ostatnia() and jarvis.restarty == []
    assert jarvis.git("log", "--format=%s", "main") == "start"


def test_scal_przy_nieprzechodzacych_testach_pyta(jarvis):
    jarvis.wyniki.append({"zmiany": {"a.py": "a = 3\n"}})
    roadmapa.zaproponuj()
    jarvis.wynik_testow = {"stan": "ok", "opis": "10 przeszło", "nieudane": []}
    jarvis.kanal.kliknij("✅ Akceptuj")
    stan = roadmapa._stan()
    for wynik in stan["testy"].values():                     # testy padły po zadaniu
        wynik.update(stan="blad", opis="1 nie przeszło")
    roadmapa._zapisz(stan)
    roadmapa.komenda("scal")
    assert jarvis.kanal.ostatnia().startswith("⚠️ Nie scalam od razu") and jarvis.restarty == []
    jarvis.kanal.kliknij("⚠️ Scal mimo to")
    assert jarvis.restarty and plik(os.path.join(jarvis.katalog, "a.py")) == "a = 3\n"


def test_raport_dzienny_raz_dziennie(jarvis, monkeypatch):
    monkeypatch.setenv(konfiguracja.ROADMAPA_RAPORT, "12:00")
    # Jutro — zadanie poniżej dzieje się "teraz", czyli przed jutrzejszymi raportami.
    jutro = (datetime.datetime.now(W) + datetime.timedelta(days=1)).replace(hour=11, minute=0)
    roadmapa.krok(jutro)                                 # włączenie: propozycja, bez raportu
    jarvis.kanal.kliknij("✅ Akceptuj")
    przed = len(jarvis.kanal.kolejnosc)
    roadmapa.krok(jutro.replace(hour=12))
    raport = jarvis.kanal.ostatnia()
    assert raport.startswith(f"📋 Raport roadmapy — {jutro.strftime('%d.%m.%Y')}")
    assert "• nr 1 Pierwsze zadanie. — testy ✅" in raport
    assert "Do scalenia z main (1)" in raport and "W kolejce (2 nieukończonych)" in raport
    assert "1. nr 2 [T] Drugie zadanie. — Opus 5.5" in raport
    roadmapa.krok(jutro.replace(hour=12, minute=30))
    assert len(jarvis.kanal.kolejnosc) == przed + 1      # drugi raz tego dnia — nie
    roadmapa.krok(jutro.replace(hour=12, minute=5) + datetime.timedelta(days=1))
    assert "Od ostatniego raportu nic nie zrobiłem." in jarvis.kanal.ostatnia()


def test_wylaczony_tryb_nie_proponuje(jarvis, monkeypatch):
    monkeypatch.setenv(konfiguracja.ROADMAPA, "0")
    roadmapa.krok(TERAZ.replace(hour=13))
    assert jarvis.kanal.kolejnosc == []
    roadmapa.komenda("roadmapa", "status")
    assert "tryb wyłączony" in jarvis.kanal.ostatnia()


# --- Część B: wymaga Twojego udziału, nic samemu ---------------------------

MAPA_Z_CZESCIA_B = """# Mapa zadań

## CZĘŚĆ A — zadania samodzielne

### Etap 1. Start

1. [Ł] Zadanie z części A. (1)

---

## CZĘŚĆ B — zadania wymagające mojego udziału

Jarvis pyta mnie, zanim je zacznie.

### Etap 12. Potrzebne konto

2. [Ś] Statystyki gier — wymagany klucz Riot API. (67)
3. [T] Rozpoznawanie mojego głosu — potrzebne próbki głosu. (7)
"""


def wiadomosc_z_przyciskiem(kanal, napis):
    return next(n for n in reversed(kanal.kolejnosc)
                if any(napis in t for wiersz in kanal.wiadomosci[n]["przyciski"] for t, _ in wiersz))


def test_czesci_z_naglowkow_dowolnego_poziomu():
    zadania = roadmapa.wczytaj_zadania(MAPA_Z_CZESCIA_B + "\n# Czesc B\n4. [Ł] Bez ogonków.\n")
    assert [(z.numer, z.czesc) for z in zadania] == [(1, "A"), (2, "B"), (3, "B"), (4, "B")]
    assert zadania[1].etap == "Etap 12. Potrzebne konto" and zadania[1].pomysl == 67


def test_mapa_md_ma_pierwszenstwo_przed_txt(tmp_path):
    (tmp_path / "MapaZadan.txt").write_text("1. [Ł] Z txt\n", encoding="utf-8")
    assert roadmapa._plik_mapy(str(tmp_path)).endswith("MapaZadan.txt")
    (tmp_path / "MapaZadan.md").write_text("1. [Ł] Z md\n", encoding="utf-8")
    assert roadmapa._plik_mapy(str(tmp_path)).endswith("MapaZadan.md")


def test_czesc_b_nie_zaczyna_sama_tylko_pyta_i_czeka(jarvis):
    ustaw_mape(jarvis, MAPA_Z_CZESCIA_B)
    roadmapa.zaproponuj()
    jarvis.kanal.kliknij("✅ Akceptuj")                          # Część A — zwykły tryb
    # Po zadaniu z A od razu kolej na B: Claude Code TYLKO CZYTA i wypisuje,
    # czego potrzebuje — zadanie A zwolniło już Claude Code.
    assert len(jarvis.claude) == 2 and not zdalny_kod.zajety()
    sprawdzenie = jarvis.claude[1]
    assert sprawdzenie["polecenie"][sprawdzenie["polecenie"].index("--tools") + 1] == "Read,Glob,Grep"
    assert "niczego nie zmieniaj" in sprawdzenie["tresc"] and "Zadanie nr 2" in sprawdzenie["tresc"]
    pytanie = jarvis.kanal.ostatnia()
    assert pytanie.startswith("🗺 Zadanie 1 z 2 · nr 2 [Ś] · 🧩 wymaga Twojego udziału (Część B)")
    assert "Statystyki gier — wymagany klucz Riot API." in pytanie
    assert "Zanim zacznę, potrzebuję od Ciebie:\nZrobione." in pytanie            # wynik atrapy Claude Code
    assert "Odpowiedz na TĘ wiadomość" in pytanie
    assert "✅ Akceptuj" not in jarvis.kanal.napisy() and "▶️ Zaczynaj" in jarvis.kanal.napisy()
    roadmapa.krok(TERAZ.replace(hour=11))
    assert len(jarvis.claude) == 2                                # czeka — nic nie startuje samo
    assert jarvis.git("log", "-1", "--format=%s", "roadmap") == "Roadmapa: nr 1 Zadanie z części A."

    numer = wiadomosc_z_przyciskiem(jarvis.kanal, "▶️ Zaczynaj")
    assert roadmapa.odpowiedz(numer, "klucz Riot już jest w Ustawieniach, region EUNE")
    assert "📝 Od Ciebie (1)" in jarvis.kanal.wiadomosci[numer]["tekst"]
    # Wartość klucza nie trafia ani do zapisu, ani do Claude Code.
    assert roadmapa.odpowiedz(numer, "RGAPI-1234abcd-5678-efgh-9012-ijklmnop")
    assert jarvis.kanal.ostatnia().startswith("🔒 To wygląda na hasło albo klucz")
    assert not roadmapa.odpowiedz(numer + 100, "to do kogoś innego")   # inna wiadomość — do agenta

    jarvis.kanal.kliknij("▶️ Zaczynaj", numer)
    tresc = jarvis.claude[2]["tresc"]
    assert "To zadanie z Części B" in tresc and "- klucz Riot już jest w Ustawieniach, region EUNE" in tresc
    assert "RGAPI" not in tresc and "Część B — zadanie wymagające udziału użytkownika." in tresc
    assert jarvis.git("log", "-1", "--format=%s", "roadmap") == "Roadmapa: nr 2 Statystyki gier — wymagany klucz Riot API."
    assert "2. ✅ [Ś] Statystyki gier" in plik(os.path.join(jarvis.kopia, "MapaZadan.md"))


def test_czesc_b_sprawdzenie_tylko_czytajac(jarvis):
    ustaw_mape(jarvis, MAPA_Z_CZESCIA_B.replace("1. [Ł]", "1. ✅ [Ł]"))
    jarvis.wyniki.append({"zmiany": {}, "json": {"is_error": False, "result": "• Który region konta?"}})
    roadmapa.zaproponuj()
    sprawdzenie, = jarvis.claude
    polecenie = sprawdzenie["polecenie"]
    assert polecenie[polecenie.index("--tools") + 1] == "Read,Glob,Grep"         # tylko czytanie
    assert not [p for p in polecenie if p.startswith(("Edit", "Write", "Bash("))
                and p not in roadmapa.ZAKAZANE and p not in zdalny_kod.ZAKAZANE]
    assert "--continue" not in polecenie
    assert "Zanim zacznę, potrzebuję od Ciebie:\n• Który region konta?" in jarvis.kanal.ostatnia()
    assert jarvis.git("log", "-1", "--format=%s", "roadmap") == "nowa mapa zadań"      # nic nie zmienione
    assert not zdalny_kod.zajety()


def test_czesc_b_gdy_sprawdzenie_sie_nie_uda(jarvis):
    ustaw_mape(jarvis, "## CZĘŚĆ B\n\n5. [Ś] Coś z kontem.\n")
    jarvis.wyniki.append({"zmiany": {}, "json": {"is_error": True, "result": "You've hit your limit · resets 3pm"}})
    roadmapa.zaproponuj()
    pytanie = jarvis.kanal.ostatnia()
    assert "Nie ustaliłem, czego potrzebuję (skończyły się limity Claude Code, wrócą ok. 15:00)" in pytanie
    jarvis.kanal.kliknij("🔍 Sprawdź jeszcze raz")
    assert len(jarvis.claude) == 2 and "Zanim zacznę, potrzebuję od Ciebie" in jarvis.kanal.ostatnia()


# --- most do Telegrama: przyciski i komendy -------------------------------

@pytest.fixture
def most(monkeypatch):
    api = TelegramAPI()
    most = telegram_bridge.MostTelegram("123:atrapa", 111, odpowiedz=None)
    most._api = api
    klikniete = []
    monkeypatch.setattr(roadmapa, "przycisk", lambda dane, numer: klikniete.append((dane, numer)))
    monkeypatch.setattr(roadmapa, "komenda", lambda nazwa, reszta="": klikniete.append((nazwa, reszta)))
    return most, api, klikniete


def klikniecie(od=111, czat=111, dane="rm|a|abc123"):
    return {"update_id": 1, "callback_query": {"id": "q1", "from": {"id": od}, "data": dane,
                                               "message": {"message_id": 7, "chat": {"id": czat}}}}


def test_klikniecie_wlasciciela_trafia_do_roadmapy(most):
    most, api, klikniete = most
    assert most._obsluz(klikniecie()) is None
    most._wykonaj(most._kolejka.get_nowait(), None)
    assert klikniete == [("rm|a|abc123", 7)]
    assert api.wywolania[0] == ("answerCallbackQuery", {"callback_query_id": "q1"})


def test_klikniecie_obcego_i_z_pierwszej_paczki_pomijane(most):
    most, api, klikniete = most
    assert most._obsluz(klikniecie(od=999)) == "obca" and api.wywolania == []
    assert most._obsluz(klikniecie(), pierwsza_paczka=True) == "stara"
    assert most._kolejka.empty() and "jeszcze raz" in api.wywolania[0][1]["text"]


def test_komendy_roadmapy(most):
    most, api, klikniete = most
    most._obsluz_tekst("/odrzuć", None)
    most._obsluz_tekst("/roadmapa status", None)
    assert klikniete == [("odrzuc", ""), ("roadmapa", "status")]


def test_odpowiedz_na_pytania_czesci_b_nie_idzie_do_agenta(most, monkeypatch):
    most, api, _ = most
    odpowiedzi, do_agenta = [], []
    monkeypatch.setattr(roadmapa, "odpowiedz", lambda numer, tekst: odpowiedzi.append((numer, tekst)) or numer == 7)
    monkeypatch.setattr(most, "_rozmawiaj", lambda tekst, przerwanie, uslyszane=None: do_agenta.append(tekst))
    most._wykonaj({"text": "konto jarvis-test", "date": 0, "reply_to_message": {"message_id": 7}}, None)
    most._wykonaj({"text": "a to do Jarvisa", "date": 0, "reply_to_message": {"message_id": 8}}, None)
    most._wykonaj({"text": "zwykła wiadomość", "date": 0}, None)
    assert odpowiedzi == [(7, "konto jarvis-test"), (8, "a to do Jarvisa")]
    assert do_agenta == ["a to do Jarvisa", "zwykła wiadomość"]


def test_przyciski_jako_inline_keyboard(most):
    most, api, _ = most
    most.wyslij("Propozycja", [[("✅ Akceptuj", "rm|a|x")]])
    metoda, dane = api.wywolania[-1]
    assert metoda == "sendMessage" and '"inline_keyboard": [[{"text": "✅ Akceptuj", "callback_data": "rm|a|x"}]]' \
        in dane["reply_markup"]


# --- Ustawienia: zakładka Ogólne ----------------------------------------

@pytest.mark.skipif(importlib.util.find_spec("PySide6") is None, reason="bez PySide6 (lekkie zależności testów)")
def test_zakladka_ogolne(monkeypatch):
    for zmienna in (konfiguracja.ROADMAPA, konfiguracja.ROADMAPA_RAPORT, konfiguracja.CLAUDE_MODEL_TRUDNE,
                    konfiguracja.CLAUDE_MODEL_LATWE):
        monkeypatch.setenv(zmienna, "")
        monkeypatch.delenv(zmienna)
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTime
    from PySide6.QtWidgets import QApplication

    import setup_wizard

    app = QApplication.instance() or QApplication([])
    okno = setup_wizard.KreatorJarvisa(pierwsze=False)
    try:
        okno._otworz_zakladke("Ogólne")
        assert okno._zakladki["Ogólne"].isChecked() and okno._stos.currentIndex() == okno._indeks_ogolnych
        assert (okno._model_trudne.currentText(), okno._model_latwe.currentText()) == ("Opus 5.5", "Sonnet 5.5")
        assert [okno._model_trudne.itemText(i) for i in range(okno._model_trudne.count())] == [
            "Opus 5.5", "Opus 5", "Sonnet 5.5", "Sonnet 5", "Haiku 5.5"]
        okno._roadmapa_wlaczona.setChecked(True)
        okno._raport_roadmapy.setTime(QTime(9, 15))
        okno._model_latwe.setCurrentIndex(okno._model_latwe.findData("claude-haiku-5-5"))
        okno._otworz_zakladke("Podsumowanie")
        assert "Ogólne</b> — roadmapa włączona, raport o 09:15" in okno._podsumowanie.text()
        okno._zakoncz()
    finally:
        okno.close()
        app.processEvents()
    assert konfiguracja.roadmapa_wlaczona() and konfiguracja.raport_roadmapy() == (9, 15)
    assert konfiguracja.model_claude("latwe") == "claude-haiku-5-5"
    assert konfiguracja.model_claude("trudne") == "claude-opus-5-5"
    assert konfiguracja.CLAUDE_MODEL_TRUDNE not in open(konfiguracja.PLIK, encoding="utf-8").read()   # bez zmian
