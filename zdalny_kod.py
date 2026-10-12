"""
zdalny_kod.py — Claude Code sterowany z telefonu: /kod, /zatwierdz, /cofnij
oraz "ostatnia działająca wersja" dla /restart (straznik.py).


JAK TO DZIAŁA
=============

/kod <polecenie> w Telegramie uruchamia w folderze Jarvisa Claude Code bez
okna (`claude -p --continue`), czyli dalszy ciąg tej samej sesji — kolejne
/kod pamiętają poprzednie. Zadanie trwa w tle, Jarvis w tym czasie normalnie
rozmawia, a po zakończeniu przysyła podsumowanie i listę zmienionych plików.
Naraz najwyżej jedno zadanie.


TYLKO KOMENDA OD CIEBIE
=======================

Ten moduł woła wyłącznie telegram_bridge.py, po sprawdzeniu, że wiadomość
przyszła od właściciela (TELEGRAM_OWNER_ID). agent.py go nie importuje i nie
ma narzędzia do Claude Code, więc model nie może sam zlecić zadania — także
wtedy, gdy ktoś podsunie mu to w mailu albo na stronie (pilnuje tego autotest).


CO WOLNO CLAUDE CODE — TRZY WARSTWY
===================================

  1. --tools: widzi tylko Read, Glob, Grep, Edit, Write i Bash. Bez sieci
     (WebFetch), bez PowerShella, bez serwerów MCP (--strict-mcp-config).

  2. --permission-mode acceptEdits + --allowedTools: edycje plików w folderze
     projektu bez pytania, z powłoki `git status` i `git diff`. W trybie -p
     nikt nie kliknie "zgoda", więc wszystko spoza listy jest odrzucane.
     --disallowedTools zabrania czytać .env i pliki z tokenami Spotify.

  3. BRAMKA (hook PreToolUse). Test z 05.10 pokazał, że warstwa 2 nie
     wystarcza: Claude Code sam przepuszcza polecenia, które uznaje za "tylko
     do odczytu" (`python --version` przeszło), a tryb acceptEdits także
     mkdir, cp i mv. Bramka to mały skrypt wołany przed KAŻDYM poleceniem
     powłoki; wszystko poza samym `git status` / `git diff` kończy kodem 2,
     czyli "zablokowane". Leży w %APPDATA%\\Jarvis, poza folderem projektu —
     Claude Code edytuje pliki projektu, więc bramka w projekcie mogłaby
     zostać "poprawiona" przez model, którego ma pilnować.

Nigdy --dangerously-skip-permissions. --setting-sources user pomija ustawienia
z folderu projektu (.claude/settings.json), gdzie dałoby się dopisać własne
hooki. Klucze Jarvisa (Anthropic, Telegram, Gmail...) nie trafiają do
środowiska Claude Code — bez ANTHROPIC_API_KEY płaci Twoja subskrypcja
claude.ai, a nie płatny klucz Jarvisa.


MIGAWKI ZAMIAST "git checkout ."
================================

/cofnij nie może zrobić zwykłego `git checkout . && git clean` — zmiotłoby
też Twoje własne niezatwierdzone zmiany. Dlatego przed pierwszym zadaniem
Jarvis robi MIGAWKĘ folderu: commit (git commit-tree) ze wszystkim, co nie
jest w .gitignore, także z nowymi plikami — bez ruszania gałęzi, indeksu
i plików. Migawki leżą pod refs/jarvis/...: git ich nie usunie przy
sprzątaniu, a `git push` ich nie wyśle.

  - /cofnij przywraca stan sprzed pierwszego niezatwierdzonego zadania
    (Twoje wcześniejsze zmiany zostają),
  - /zatwierdz commituje tylko pliki zmienione od tamtej chwili,
  - lista zmian po zadaniu to różnica migawek sprzed i po zadaniu — widać
    w niej nowe pliki, a nie widać Twoich wcześniejszych zmian.

Przed każdym cofnięciem bieżący stan też trafia do migawki
(refs/jarvis/cofniete/...), więc nic nie przepada na dobre.


TESTY PO KAŻDYM /kod I PRZED /restart
=====================================

Po każdym zadaniu Jarvis uruchamia testy (`python -m pytest`, folder tests/)
i dopisuje wynik do raportu. /restart sprawdza testy jeszcze raz — chyba że
kod się od tamtej chwili nie zmienił (wynik pamiętamy razem z drzewem gita
kodu, którego dotyczy). Gdy testy nie przechodzą, most do Telegrama ostrzega
i czeka na odpisane "tak".

Uwaga: uruchomienie testów to uruchomienie kodu, który właśnie napisał
Claude Code — tak samo jak /restart. Dlatego testy dostają środowisko bez
kluczy Jarvisa i limit czasu, a raport pokazuje, gdy zadanie zmieniło testy.


OSTATNIA DZIAŁAJĄCA WERSJA (dla /restart)
=========================================

Na starcie Jarvis robi migawkę kodu, z którym wystartował (zapamietaj_start).
Gdy przeżyje STABILNOSC_S sekund po "Systemy gotowe" (gotowy), ta migawka
staje się refs/jarvis/dzialajaca. Strażnik restartu (straznik.py) wraca do
niej, gdy nowa wersja nie wstanie.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import konfiguracja
import sciezki

logger = logging.getLogger(__name__)

KATALOG = sciezki.KOD

# Stan zdalnego programowania leży w %APPDATA%\Jarvis\zdalny — POZA folderem
# projektu, żeby Claude Code nie mógł go ani czytać, ani zmieniać.
FOLDER = os.path.join(sciezki.UZYTKOWNIK, "zdalny")
PLIK_ZADAN = os.path.join(FOLDER, "zadania.json")       # polecenia od ostatniego /zatwierdz
PLIK_BRAMKI = os.path.join(FOLDER, "bramka.py")
PLIK_RESTARTU = os.path.join(FOLDER, "restart.json")    # dla strażnika: co i jak uruchomić
PLIK_GOTOWOSCI = os.path.join(FOLDER, "restart.gotowy")  # nowa wersja: "wstałam"
PLIK_DZIENNIKA_STRAZNIKA = os.path.join(FOLDER, "straznik.log")
PLIK_TESTOW = os.path.join(FOLDER, "testy.json")          # ostatni wynik testów i czego dotyczy

# Najdłuższe uruchomienie testów — zwykle trwają kilka sekund.
LIMIT_TESTOW_S = 5 * 60

# Pliki testów: zmianę któregoś przez /kod wyraźnie zaznaczamy w raporcie.
PLIKI_TESTOW = ("tests/", "pytest.ini", "conftest.py")

# Strażnik przekazuje nowej wersji ten identyfikator; ona odpisuje nim
# w PLIK_GOTOWOSCI — tak strażnik wie, że "gotowa" jest właśnie ta kopia.
ZMIENNA_RESTARTU = "JARVIS_RESTART_ID"

REF_PRZED = "refs/jarvis/przed-kodem"     # migawka sprzed pierwszego niezatwierdzonego /kod
REF_DZIALAJACA = "refs/jarvis/dzialajaca"
REF_COFNIETE = "refs/jarvis/cofniete/"
REF_NIEUDANE = "refs/jarvis/nieudane/"

# Najdłuższe zadanie — potem Claude Code jest zatrzymywany.
LIMIT_ZADANIA_S = 20 * 60

# Ile sekund po "Systemy gotowe" Jarvis musi przeżyć, żeby jego kod uznać
# za działający. Strażnik czeka tyle samo, zanim ogłosi udany restart.
STABILNOSC_S = 15

# Model i wysiłek Claude Code — te same co w sesji Claude Code, w której
# powstawał Jarvis. Bez nich Claude Code brał, co miał domyślnie (05.10:
# Opus 5 z wznowionej starej rozmowy, w innym folderze Sonnet 5).
# Opus 5.5 na "xhigh" szybciej zużywa limit subskrypcji niż Sonnet.
MODEL = "claude-opus-5-5"
WYSILEK = "xhigh"

# Warstwy 1 i 2 z opisu modułu.
NARZEDZIA = "Read,Glob,Grep,Edit,Write,Bash"
DOZWOLONE = ("Read", "Glob", "Grep", "Edit", "Write", "Bash(git status *)", "Bash(git diff *)")
ZAKAZANE = ("Bash(rm *)", "Bash(rmdir *)", "Bash(del *)",
            "Read(./.env)", "Edit(./.env)", "Read(./.spotify_cache*)")

# Warstwa 3: bramka. Zapisywana przy każdym zadaniu (gdyby ktoś ją zmienił,
# wraca do tej wersji). Przepuszcza jedno polecenie `git status` albo
# `git diff` bez łączenia (; && |), przekierowań i podstawień. --output
# zapisałby plik, --no-index czytałby spoza repozytorium, --ext-diff
# i --textconv uruchamiają zewnętrzne programy.
KOD_BRAMKI = r'''"""Bramka Jarvisa dla Claude Code (opis w zdalny_kod.py). Kod 2 = zablokowane."""
import json
import re
import sys

DOZWOLONE = re.compile(r"^\s*git\s+(status|diff)(\s+[^;&|<>`$\r\n()]*)?\s*$")
ZAKAZANE_OPCJE = re.compile(r"--(output|ext-diff|no-index|textconv)\b")

dane = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
polecenie = (dane.get("tool_input") or {}).get("command", "")
if dane.get("tool_name") == "Bash" and (not DOZWOLONE.match(polecenie)
                                        or ZAKAZANE_OPCJE.search(polecenie)):
    # Bez polskich znaków — kodowanie stderr na Windowsie bywa różne.
    sys.stderr.write("Zablokowane przez Jarvisa: z powloki wolno tylko `git status` i `git diff`, "
                     "kazde osobno. Pliki czytaj i edytuj narzedziami Read, Edit i Write.")
    sys.exit(2)
'''

# Okna konsoli dla gita i Claude Code — Jarvis działa w tle, bez konsoli,
# więc bez tej flagi każde polecenie mignęłoby czarnym okienkiem.
BEZ_OKNA = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class BladKodu(Exception):
    """Błąd z czytelnym komunikatem — trafia prosto na Telegram."""


# ---------------------------------------------------------------
# Git
# ---------------------------------------------------------------

def _git(*argumenty, indeks=None, wejscie=None, jako_jarvis=False, sprawdz=True):
    """
    Jedno polecenie gita w folderze Jarvisa. Zwraca wyjście (tekst).

    indeks      — osobny plik indeksu (GIT_INDEX_FILE): migawki nie ruszają
                  Twojego prawdziwego indeksu
    jako_jarvis — autor "Jarvis" (migawki; Twoje commity idą jako Ty)
    sprawdz     — False: przy błędzie zwraca None zamiast rzucać BladKodu
    """
    srodowisko = None
    if indeks or jako_jarvis:
        srodowisko = dict(os.environ)
        if indeks:
            srodowisko["GIT_INDEX_FILE"] = indeks
        if jako_jarvis:
            for rola in ("AUTHOR", "COMMITTER"):
                srodowisko[f"GIT_{rola}_NAME"] = "Jarvis"
                srodowisko[f"GIT_{rola}_EMAIL"] = "jarvis@localhost"
    try:
        wynik = subprocess.run(["git", "-c", "core.quotepath=off", *argumenty], cwd=KATALOG,
                               env=srodowisko, input=wejscie, capture_output=True,
                               timeout=120, creationflags=BEZ_OKNA)
    except (OSError, subprocess.TimeoutExpired) as e:
        if not sprawdz:
            return None
        raise BladKodu(f"git {argumenty[0]}: {e}") from None
    if wynik.returncode != 0:
        if not sprawdz:
            return None
        blad = wynik.stderr.decode("utf-8", "replace").strip().splitlines()
        raise BladKodu(f"git {argumenty[0]}: {blad[-1] if blad else f'kod {wynik.returncode}'}")
    return wynik.stdout.decode("utf-8", "replace").strip()


def _head():
    """Bieżący commit albo None (repozytorium bez commitów)."""
    return _git("rev-parse", "-q", "--verify", "HEAD^{commit}", sprawdz=False)


def _ref(nazwa):
    return _git("rev-parse", "-q", "--verify", f"{nazwa}^{{commit}}", sprawdz=False)


def _rodzic(commit):
    return _git("rev-parse", "-q", "--verify", f"{commit}^", sprawdz=False)


def _ustaw_ref(nazwa, commit):
    _git("update-ref", nazwa, commit)


def _usun_ref(nazwa):
    _git("update-ref", "-d", nazwa, sprawdz=False)


def _znacznik_czasu():
    return time.strftime("%Y-%m-%d_%H-%M-%S")


def zrzut(opis):
    """
    Migawka całego folderu (bez plików z .gitignore) jako commit poza gałęziami.

    Zaczynamy od PUSTEGO indeksu, więc git czyta treść każdego pliku.
    Kopia prawdziwego indeksu byłaby szybsza, ale git ocenia w nim "plik się
    nie zmienił" po dacie i rozmiarze — test z 05.10 złapał, że zmiana
    "b = 1" -> "b = 2" w tej samej sekundzie umykała i /cofnij jej nie
    cofał. Dla folderu Jarvisa to i tak ułamek sekundy.
    """
    with tempfile.TemporaryDirectory() as tymczasowy:
        indeks = os.path.join(tymczasowy, "index")
        _git("add", "-A", "--", ":/", indeks=indeks)
        drzewo = _git("write-tree", indeks=indeks)
    head = _head()
    return _git("commit-tree", drzewo, *(["-p", head] if head else []), "-m", opis,
                jako_jarvis=True)


def _zmiany(od, do):
    """[(status, ścieżka)] między dwiema migawkami. Status: A, M, D albo T."""
    wyjscie = _git("diff", "--name-status", "-z", "--no-renames", od, do)
    czesci = [c for c in wyjscie.split("\0") if c]
    return list(zip(czesci[0::2], czesci[1::2]))


def _lista_zmian(od, do, maks=25):
    """Zmienione pliki do wiadomości na Telegram: "• agent.py (+12 −3)"."""
    zmiany = _zmiany(od, do)
    if not zmiany:
        return "Pliki bez zmian."
    liczby = {}
    for linia in _git("diff", "--numstat", "--no-renames", od, do).splitlines():
        dodane, usuniete, sciezka = linia.split("\t", 2)
        liczby[sciezka] = (dodane, usuniete)
    wiersze, suma_plus, suma_minus = [], 0, 0
    for status, sciezka in zmiany:
        dodane, usuniete = liczby.get(sciezka, ("-", "-"))
        if dodane.isdigit():
            suma_plus, suma_minus = suma_plus + int(dodane), suma_minus + int(usuniete)
            opis = f"+{dodane} −{usuniete}"
        else:
            opis = "plik binarny"
        if status == "A":
            opis = f"nowy, {opis}"
        elif status == "D":
            opis = "usunięty"
        wiersze.append(f"• {sciezka} ({opis})")
    if len(wiersze) > maks:
        wiersze = wiersze[:maks] + [f"• …i {len(wiersze) - maks} innych"]
    naglowek = f"Zmienione pliki ({len(zmiany)}, +{suma_plus} −{suma_minus}):"
    return naglowek + "\n" + "\n".join(wiersze)


def _przywroc(cel, obecny):
    """
    Ustawia pliki folderu tak, jak są w migawce `cel`. HEAD i indeks zostają.

    Pliki dodane od tamtej chwili usuwamy, a zmienione i usunięte wypisujemy
    z migawki przez osobny indeks (checkout-index). Pliki z .gitignore
    (pamięć, notatki, .env) w ogóle nie biorą w tym udziału.
    """
    do_usuniecia, do_zapisu = [], []
    for status, sciezka in _zmiany(cel, obecny):
        (do_usuniecia if status == "A" else do_zapisu).append(sciezka)

    for sciezka in do_usuniecia:
        pelna = os.path.join(KATALOG, sciezka)
        if os.path.lexists(pelna):
            os.remove(pelna)
        # Puste foldery po usuniętych plikach też sprzątamy (git ich nie śledzi).
        folder = os.path.dirname(pelna)
        while os.path.normcase(folder) != os.path.normcase(KATALOG):
            try:
                os.rmdir(folder)        # tylko pusty — inaczej OSError i koniec
            except OSError:
                break
            folder = os.path.dirname(folder)

    if do_zapisu:
        with tempfile.TemporaryDirectory() as tymczasowy:
            indeks = os.path.join(tymczasowy, "index")
            _git("read-tree", cel, indeks=indeks)
            _git("checkout-index", "-f", "-z", "--stdin", indeks=indeks,
                 wejscie="\0".join(do_zapisu).encode("utf-8"))


def dostepny(z_claude=False):
    """(True, "") albo (False, powód dla Ciebie)."""
    if getattr(sys, "frozen", False):
        return False, ("Zdalne programowanie działa tylko w Jarvisie uruchamianym z kodu "
                       "(Jarvis.pyw albo python main.py) — wersja z instalatora nie ma czego edytować.")
    if shutil.which("git") is None:
        return False, "Nie widzę gita — zainstaluj Git for Windows."
    if _git("rev-parse", "--is-inside-work-tree", sprawdz=False) != "true":
        return False, "Folder Jarvisa nie jest repozytorium gita."
    if z_claude and shutil.which("claude") is None:
        return False, ("Nie widzę Claude Code (polecenia claude). Zainstaluj go i zaloguj się "
                       "raz w terminalu: claude")
    return True, ""


# ---------------------------------------------------------------
# Zadania /kod
# ---------------------------------------------------------------

_blokada = threading.Lock()
_zadanie = None     # {"polecenie": str, "od": monotonic, "proces": Popen albo None}


def trwa():
    """Czy właśnie trwa zadanie /kod."""
    with _blokada:
        return _zadanie is not None


def _wczytaj_zadania():
    try:
        with open(PLIK_ZADAN, encoding="utf-8") as plik:
            return json.load(plik)
    except (OSError, ValueError):
        return {"polecenia": [], "uwaga": ""}


def _zapisz_zadania(dane):
    os.makedirs(FOLDER, exist_ok=True)
    with open(PLIK_ZADAN, "w", encoding="utf-8") as plik:
        json.dump(dane, plik, ensure_ascii=False, indent=1)


def _wyczysc(uwaga=""):
    """Koniec serii zadań (po /zatwierdz, /cofnij, wycofaniu przez strażnika)."""
    _usun_ref(REF_PRZED)
    _zapisz_zadania({"polecenia": [], "uwaga": uwaga})


def _skrot(tekst, dlugosc=60):
    tekst = " ".join(tekst.split())
    return tekst if len(tekst) <= dlugosc else tekst[:dlugosc - 1] + "…"


def _czas(sekundy):
    minuty, sekundy = divmod(int(sekundy), 60)
    return f"{minuty} min {sekundy} s" if minuty else f"{sekundy} s"


def zlec(polecenie, wyslij):
    """
    /kod <polecenie>. Uruchamia zadanie w tle i od razu zwraca odpowiedź
    do wysłania. Wynik przyjdzie później przez wyslij(tekst).
    """
    global _zadanie

    polecenie = polecenie.strip()
    if not polecenie:
        return "Napisz, co mam zrobić, np.: /kod dodaj komentarze do funkcji w tts.py"
    gotowe, powod = dostepny(z_claude=True)
    if not gotowe:
        return powod

    with _blokada:
        if _zadanie is not None:
            return (f"Już pracuję nad: „{_skrot(_zadanie['polecenie'])}” (od "
                    f"{_czas(time.monotonic() - _zadanie['od'])}). Naraz robię jedno zadanie "
                    "— poczekaj na wynik.")
        _zadanie = {"polecenie": polecenie, "od": time.monotonic(), "proces": None}

    try:
        przed = zrzut("Jarvis: przed zadaniem /kod")
        if _ref(REF_PRZED) is None:
            _ustaw_ref(REF_PRZED, przed)
        zadania = _wczytaj_zadania()
        # Uwaga od Jarvisa (np. "zmiany cofnięto") — Claude Code pamięta
        # poprzednią sesję i bez niej myślałby, że jego zmiany dalej są.
        uwaga = zadania.get("uwaga") or ""
        zadania["polecenia"] = zadania.get("polecenia", []) + [polecenie]
        zadania["uwaga"] = ""
        _zapisz_zadania(zadania)
    except (BladKodu, OSError) as e:
        with _blokada:
            _zadanie = None
        return f"Nie mogę zacząć: {e}"

    tresc = f"[Uwaga od Jarvisa: {uwaga}]\n\n{polecenie}" if uwaga else polecenie
    logger.info("[KOD] Zadanie: %s", _skrot(polecenie, 200))
    threading.Thread(target=_wykonaj, args=(tresc, przed, wyslij), name="claude-code",
                     daemon=True).start()
    return (f"🛠 Zaczynam: „{_skrot(polecenie)}”. Dam znać, jak skończę "
            f"(najdłużej {LIMIT_ZADANIA_S // 60} min).")


def _ustawienia_claude():
    """--settings: bramka jako hook przed każdym poleceniem powłoki."""
    os.makedirs(FOLDER, exist_ok=True)
    with open(PLIK_BRAMKI, "w", encoding="utf-8") as plik:
        plik.write(KOD_BRAMKI)
    return {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": f'"{_python_konsolowy()}" "{PLIK_BRAMKI}"'}]}]}}


def _python_konsolowy():
    """
    python.exe zamiast pythonw.exe (Jarvis.pyw). Bramka i testy potrzebują
    wejścia i wyjścia, a pythonw ich nie ma — bierzemy python.exe z tego
    samego folderu.
    """
    python = sys.executable
    konsolowy = os.path.join(os.path.dirname(python), "python.exe")
    if os.path.basename(python).lower() == "pythonw.exe" and os.path.exists(konsolowy):
        return konsolowy
    return python


def polecenie_claude():
    """Pełne polecenie Claude Code — osobno, żeby autotest mógł je sprawdzić."""
    return [shutil.which("claude") or "claude", "-p", "--continue",
            "--model", MODEL, "--effort", WYSILEK,
            "--permission-mode", "acceptEdits", "--output-format", "json",
            "--strict-mcp-config", "--setting-sources", "user",
            "--settings", json.dumps(_ustawienia_claude()),
            "--tools", NARZEDZIA,
            "--allowedTools", *DOZWOLONE,
            "--disallowedTools", *ZAKAZANE]


def _zabij_drzewo(pid):
    """Zatrzymuje Claude Code razem z tym, co sam uruchomił."""
    try:
        import psutil

        proces = psutil.Process(pid)
        for dziecko in proces.children(recursive=True):
            dziecko.kill()
        proces.kill()
    except Exception:
        logger.warning("[KOD] Nie udało się zatrzymać Claude Code.", exc_info=True)


def _uruchom_claude(tresc):
    """Zwraca (wynik z JSON albo None, opis błędu albo None)."""
    # Polecenie idzie przez wejście, nie jako argument — tekst zaczynający
    # się od "--" nie zostanie wzięty za opcję.
    proces = subprocess.Popen(polecenie_claude(), cwd=KATALOG, stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              env=konfiguracja.srodowisko_bez_kluczy(), creationflags=BEZ_OKNA)
    with _blokada:
        if _zadanie is not None:
            _zadanie["proces"] = proces
    try:
        wyjscie, bledy = proces.communicate(tresc.encode("utf-8"), timeout=LIMIT_ZADANIA_S)
    except subprocess.TimeoutExpired:
        _zabij_drzewo(proces.pid)
        proces.communicate()
        return None, f"przekroczył limit {LIMIT_ZADANIA_S // 60} min, więc go zatrzymałem"

    try:
        return json.loads(wyjscie.decode("utf-8", "replace")), None
    except ValueError:
        linie = bledy.decode("utf-8", "replace").strip().splitlines()
        return None, f"zakończył się kodem {proces.returncode}" + (f": {linie[-1]}" if linie else "")


def _raport(wynik, blad, przed, po, czas, testy=None):
    """Wiadomość na Telegram po zadaniu."""
    if blad:
        czesci = [f"⚠️ Claude Code {blad} ({_czas(czas)})."]
    elif wynik.get("is_error"):
        czesci = [f"⚠️ Claude Code zakończył się błędem ({_czas(czas)}):\n"
                  f"{_skrot(str(wynik.get('result') or wynik.get('subtype')), 1500)}"]
    else:
        czesci = [f"✅ Claude Code skończył ({_czas(czas)}).",
                  _skrot(str(wynik.get("result") or "(bez podsumowania)"), 3000)]

    # Próby, które zatrzymały uprawnienia albo bramka — żebyś wiedział,
    # czego mu brakowało (np. chciał uruchomić testy).
    odmowy = []
    for odmowa in (wynik or {}).get("permission_denials") or []:
        wejscie = odmowa.get("tool_input") or {}
        szczegol = wejscie.get("command") or wejscie.get("file_path") or ""
        odmowy.append(f"• {odmowa.get('tool_name')}: {_skrot(str(szczegol), 80)}")
    if odmowy:
        czesci.append("Zablokowane próby:\n" + "\n".join(dict.fromkeys(odmowy[:6])))

    czesci.append(_lista_zmian(przed, po))
    if testy:
        czesci.append(opis_testow(testy))
    # Testy, które zadanie samo zmieniło, mogły zostać "dopasowane" do błędu.
    zmienione_testy = [s for _, s in _zmiany(przed, po)
                       if s.startswith(PLIKI_TESTOW[0]) or os.path.basename(s) in PLIKI_TESTOW[1:]]
    if zmienione_testy:
        czesci.append("⚠️ To zadanie zmieniło też testy (" + ", ".join(zmienione_testy[:5])
                      + ") — sprawdź, czy ich nie poluzowało.")
    if _zmiany(_ref(REF_PRZED) or przed, po):
        czesci.append("/zatwierdz — commit · /cofnij — wycofanie · /restart — uruchom nową wersję"
                      + (" (zapyta o potwierdzenie, bo testy nie przechodzą)"
                         if testy and testy["stan"] == "blad" else ""))
    return "\n\n".join(czesci)


def _wykonaj(tresc, przed, wyslij):
    """Wątek zadania: Claude Code, migawka po nim, raport."""
    global _zadanie

    start = time.monotonic()
    try:
        wynik, blad = _uruchom_claude(tresc)
        # Migawka PO zadaniu także wtedy, gdy Claude Code się wysypał —
        # mógł zdążyć coś zmienić, a Ty musisz o tym wiedzieć.
        po = zrzut("Jarvis: po zadaniu /kod")
        czas = time.monotonic() - start
        testy = uruchom_testy(po)
        tekst = _raport(wynik, blad, przed, po, czas, testy)
    except BladKodu as e:
        tekst = f"⚠️ Zadanie /kod: {e}"
    except Exception:
        logger.exception("[KOD] Zadanie wysypało się")
        tekst = "⚠️ Zadanie /kod wysypało się po mojej stronie — szczegóły są w jarvis.log."
    finally:
        with _blokada:
            _zadanie = None
    logger.info("[KOD] Koniec zadania po %s.", _czas(time.monotonic() - start))
    try:
        wyslij(tekst)
    except Exception:
        logger.exception("[KOD] Nie wysłałem wyniku zadania")


# ---------------------------------------------------------------
# Testy (opis w nagłówku: TESTY PO KAŻDYM /kod I PRZED /restart)
# ---------------------------------------------------------------

# pytest podsumowuje po angielsku ("2 failed, 100 passed in 2.10s").
_SLOWA_PYTESTA = {"passed": "przeszło", "failed": "nie przeszło", "error": "z błędem",
                  "errors": "z błędem", "skipped": "pominięto", "xfailed": "oczekiwanie nie przeszło",
                  "xpassed": "nieoczekiwanie przeszło", "deselected": "odfiltrowano",
                  "warning": "z ostrzeżeniem", "warnings": "z ostrzeżeniem"}


def _brak_testow(powod):
    return {"stan": "brak", "opis": powod, "nieudane": []}


def _podsumowanie_pytesta(wyjscie):
    """Ostatnia linia pytesta po polsku: "2 nie przeszło, 100 przeszło (2,1 s)"."""
    for linia in reversed(wyjscie.splitlines()):
        if not re.search(r"\d+ (passed|failed|errors?|skipped)\b", linia):
            continue
        liczby = re.findall(r"(\d+) (\w+)", linia.split(" in ")[0])
        czas = re.search(r" in ([\d.]+)s", linia)
        opis = ", ".join(f"{liczba} {_SLOWA_PYTESTA.get(slowo, slowo)}" for liczba, slowo in liczby)
        return opis + (f" ({float(czas.group(1)):.1f} s)".replace(".", ",") if czas else "")
    return ""


def _drzewo(commit):
    """Drzewo gita migawki — ten sam kod daje to samo drzewo, niezależnie od chwili."""
    return _git("rev-parse", f"{commit}^{{tree}}")


def uruchom_testy(commit=None):
    """
    Testy Jarvisa (python -m pytest) na kodzie, który jest teraz w folderze.

    commit — migawka tego kodu (zrzut); wynik zapamiętujemy razem z jej
             drzewem, żeby /restart nie puszczał testów drugi raz bez potrzeby.

    Zwraca słownik:
      "stan"     — "ok" (wszystkie przeszły), "blad" (któryś nie przeszedł albo
                   testy w ogóle się nie uruchomiły) albo "brak" (nie ma
                   czym albo czego testować)
      "opis"     — podsumowanie po polsku, np. "102 przeszło (1,9 s)"
      "nieudane" — identyfikatory testów, które nie przeszły
    """
    if not os.path.isdir(os.path.join(KATALOG, "tests")):
        return _brak_testow("w projekcie nie ma folderu tests")
    srodowisko = konfiguracja.srodowisko_bez_kluczy()
    srodowisko["PYTHONIOENCODING"] = "utf-8"
    try:
        # Bez "-q": pytest.ini już go dodaje, a podwójne -q wycina linię
        # podsumowania ("102 passed in 1.87s"), z której bierzemy opis.
        proces = subprocess.Popen(
            [_python_konsolowy(), "-m", "pytest", "-rfE", "--color=no", "-p", "no:cacheprovider"],
            cwd=KATALOG, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, env=srodowisko, creationflags=BEZ_OKNA)
    except OSError as e:
        return _brak_testow(f"nie udało się uruchomić Pythona ({e})")

    try:
        wyjscie, _ = proces.communicate(timeout=LIMIT_TESTOW_S)
    except subprocess.TimeoutExpired:
        _zabij_drzewo(proces.pid)
        proces.communicate()
        wynik = {"stan": "blad", "nieudane": [],
                 "opis": f"testy przekroczyły limit {LIMIT_TESTOW_S // 60} min, więc je zatrzymałem"}
    else:
        wyjscie = wyjscie.decode("utf-8", "replace")
        if "No module named pytest" in wyjscie:
            return _brak_testow("pytest nie jest zainstalowany (pip install -r requirements-dev.txt)")
        if proces.returncode == 5:
            return _brak_testow("pytest nie znalazł żadnych testów")
        # "-rfE" dopisuje na końcu linie "FAILED plik::test - powód".
        nieudane = [linia.split(" - ")[0].split(" ", 1)[1] for linia in wyjscie.splitlines()
                    if linia.startswith(("FAILED ", "ERROR "))]
        wynik = {"stan": "ok" if proces.returncode == 0 else "blad", "nieudane": nieudane,
                 "opis": _podsumowanie_pytesta(wyjscie) or f"pytest zakończył się kodem {proces.returncode}"}

    if commit:
        try:
            os.makedirs(FOLDER, exist_ok=True)
            with open(PLIK_TESTOW, "w", encoding="utf-8") as plik:
                json.dump({"drzewo": _drzewo(commit), "wynik": wynik}, plik, ensure_ascii=False)
        except (OSError, BladKodu):
            logger.warning("[KOD] Nie zapamiętałem wyniku testów.", exc_info=True)
    logger.info("[KOD] Testy: %s — %s", wynik["stan"], wynik["opis"])
    return wynik


def testy_przed_restartem(powiadom=None):
    """
    /restart: wynik testów dla kodu, który zaraz wystartuje.

    Jeśli testy szły już na dokładnie tym kodzie (np. zaraz po /kod), bierzemy
    zapamiętany wynik. Jeśli nie — uruchamiamy je teraz, a powiadom(tekst)
    uprzedza, że to chwilę potrwa.
    """
    if getattr(sys, "frozen", False):
        return _brak_testow("wersja z instalatora nie ma testów")
    commit = None
    if dostepny()[0]:
        commit = zrzut("Jarvis: kod przed /restart")
        try:
            with open(PLIK_TESTOW, encoding="utf-8") as plik:
                zapamietany = json.load(plik)
            if zapamietany.get("drzewo") == _drzewo(commit):
                return zapamietany["wynik"]
        except (OSError, ValueError, KeyError):
            pass
    if powiadom:
        powiadom("🧪 Sprawdzam testy przed restartem…")
    return uruchom_testy(commit)


def opis_testow(testy, maks=8):
    """Wynik testów jako fragment wiadomości na Telegram."""
    if testy["stan"] == "ok":
        return f"🧪 Testy: ✅ {testy['opis']}."
    if testy["stan"] == "brak":
        return f"🧪 Testy: nie uruchomiłem — {testy['opis']}."
    linie = [f"🧪 Testy: ❌ {testy['opis']}."] + [f"• {n}" for n in testy["nieudane"][:maks]]
    if len(testy["nieudane"]) > maks:
        linie.append(f"• …i {len(testy['nieudane']) - maks} innych")
    return "\n".join(linie)


# ---------------------------------------------------------------
# /zatwierdz i /cofnij
# ---------------------------------------------------------------

def _sekret_w_plikach(sciezki_plikow):
    """(plik, nazwa klucza) pierwszego klucza Jarvisa znalezionego w plikach albo None."""
    tajne = konfiguracja.tajne_wartosci()
    for sciezka in sciezki_plikow:
        pelna = os.path.join(KATALOG, sciezka)
        try:
            if os.path.getsize(pelna) > 5_000_000:
                continue
            with open(pelna, encoding="utf-8", errors="ignore") as plik:
                tresc = plik.read()
        except OSError:
            continue        # plik usunięty — nie ma w nim czego szukać
        for nazwa, wartosc in tajne.items():
            if wartosc in tresc:
                return sciezka, nazwa
    return None


def zatwierdz(opis=""):
    """/zatwierdz [opis] — commit plików zmienionych przez zadania /kod."""
    gotowe, powod = dostepny()
    if not gotowe:
        return powod
    if trwa():
        return "Poczekaj — trwa zadanie /kod. Zatwierdzę, gdy skończy."
    przed = _ref(REF_PRZED)
    if przed is None:
        return ("Nie ma czego zatwierdzać — od ostatniego /zatwierdz albo /cofnij nie było "
                "zadań /kod. (Twoich ręcznych zmian /zatwierdz nie rusza.)")

    obecny = zrzut("Jarvis: przed /zatwierdz")
    zmiany = _zmiany(przed, obecny)
    if not zmiany:
        _wyczysc()
        return "Zadania /kod niczego nie zmieniły — nie ma czego zatwierdzać."

    # Plik, którego nie było w gicie, a zadanie go usunęło — git o nim nie
    # wie, więc nie ma czego zatwierdzać (i nie wolno go podawać git add).
    znane = set(_git("ls-files", "-z").split("\0"))
    sciezki_plikow = [s for status, s in zmiany if status != "D" or s in znane]
    znaleziony = _sekret_w_plikach(sciezki_plikow)
    if znaleziony:
        return (f"Nie zatwierdzam: w pliku {znaleziony[0]} jest wartość klucza {znaleziony[1]}. "
                "Usuń ją (np. przez /kod) i spróbuj jeszcze raz.")

    polecenia = _wczytaj_zadania().get("polecenia") or []
    if opis.strip():
        wiadomosc = opis.strip()
    else:
        wiadomosc = "Claude Code: " + _skrot(polecenia[0] if polecenia else "zmiany z /kod", 60)
        if len(polecenia) > 1:
            wiadomosc += f" (+{len(polecenia) - 1})"
    if polecenia:
        wiadomosc += "\n\nZadania /kod z Telegrama:\n" + "\n".join(
            f"- {_skrot(p, 200)}" for p in polecenia)

    lista = "\0".join(sciezki_plikow).encode("utf-8")
    _git("add", "-A", "--pathspec-from-file=-", "--pathspec-file-nul", wejscie=lista)
    # Ścieżki przy commicie: wchodzą TYLKO te pliki, nawet gdybyś miał
    # w indeksie coś innego przygotowanego do commita.
    _git("commit", "-q", "-m", wiadomosc, "--pathspec-from-file=-", "--pathspec-file-nul",
         wejscie=lista)
    commit = _head()
    lista_zmian = _lista_zmian(przed, obecny)
    _wyczysc()
    logger.info("[KOD] Zatwierdzone: %s", commit[:7])
    return (f"✅ Zatwierdzone: {commit[:7]} „{wiadomosc.splitlines()[0]}”\n\n{lista_zmian}\n\n"
            "Działający Jarvis to dalej stara wersja — /restart uruchomi nową.")


def cofnij():
    """/cofnij — wraca do stanu sprzed pierwszego niezatwierdzonego zadania /kod."""
    gotowe, powod = dostepny()
    if not gotowe:
        return powod
    if trwa():
        return "Poczekaj — trwa zadanie /kod. Cofnę, gdy skończy."
    przed = _ref(REF_PRZED)
    if przed is None:
        return ("Nie ma czego cofać — od ostatniego /zatwierdz albo /cofnij nie było zadań /kod. "
                "(Twoich ręcznych zmian /cofnij nie rusza.)")
    if _rodzic(przed) != _head():
        return ("Od pierwszego zadania /kod pojawił się nowy commit — nie cofam automatycznie, "
                "żeby po cichu go nie wycofać. Zrób to ręcznie w gicie.")

    obecny = zrzut("Jarvis: stan cofnięty przez /cofnij")
    zmiany = _lista_zmian(przed, obecny)
    if not _zmiany(przed, obecny):
        _wyczysc()
        return "Zadania /kod niczego nie zmieniły — nie ma czego cofać."
    kopia = REF_COFNIETE + _znacznik_czasu()
    _ustaw_ref(kopia, obecny)
    _przywroc(przed, obecny)
    liczba = len(_wczytaj_zadania().get("polecenia") or []) or 1
    _wyczysc(uwaga="Użytkownik cofnął komendą /cofnij wszystkie zmiany z poprzednich zadań "
                   "— pliki są znowu w stanie sprzed nich.")
    logger.info("[KOD] Cofnięte; kopia w %s", kopia)
    return (f"↩️ Cofnąłem zmiany z zadań /kod ({liczba}).\n\n{zmiany}\n\n"
            f"Gdybyś jednak chciał je odzyskać, są w migawce:\ngit checkout {kopia} -- <plik>")


# ---------------------------------------------------------------
# Ostatnia działająca wersja (/restart, straznik.py)
# ---------------------------------------------------------------

_kandydat = {"commit": None, "watek": None}


def zapamietaj_start():
    """
    main.py na starcie: migawka kodu, z którym Jarvis właśnie wstaje.
    W tle, żeby nie opóźniać startu. Bez gita po prostu nic nie robi.
    """
    def zrob():
        try:
            if dostepny()[0]:
                _kandydat["commit"] = zrzut("Jarvis: kod, z którym wystartował")
        except Exception:
            logger.warning("[KOD] Nie zrobiłem migawki startowej.", exc_info=True)

    _kandydat["watek"] = threading.Thread(target=zrob, name="migawka-startu", daemon=True)
    _kandydat["watek"].start()


def gotowy():
    """
    main.py po "Systemy gotowe": znacznik dla strażnika, a po STABILNOSC_S
    sekundach — migawka startowa staje się "ostatnią działającą wersją".
    """
    identyfikator = os.environ.pop(ZMIENNA_RESTARTU, None)
    if identyfikator:
        try:
            os.makedirs(FOLDER, exist_ok=True)
            with open(PLIK_GOTOWOSCI, "w", encoding="utf-8") as plik:
                plik.write(identyfikator)
        except OSError:
            logger.warning("[KOD] Nie zapisałem znacznika gotowości dla strażnika.", exc_info=True)

    def potwierdz():
        time.sleep(STABILNOSC_S)
        if _kandydat["watek"] is not None:
            _kandydat["watek"].join()
        if _kandydat["commit"]:
            try:
                _ustaw_ref(REF_DZIALAJACA, _kandydat["commit"])
                logger.info("[KOD] Ten kod działa — zapamiętany jako ostatnia działająca wersja.")
            except BladKodu as e:
                logger.warning("[KOD] Nie zapamiętałem działającej wersji: %s", e)

    threading.Thread(target=potwierdz, name="dzialajaca-wersja", daemon=True).start()


def wersja():
    """Krótki opis bieżącego kodu: "a1b2c3d „opis commita”"."""
    opis = _git("log", "-1", "--format=%h „%s”", sprawdz=False)
    return opis or "bez commita"


def przywroc_dzialajaca():
    """
    Strażnik: nowa wersja nie wstała — wracamy do ostatniej działającej.

    Zwraca opis dla Telegrama albo None, gdy kod się nie zmienił (wtedy
    przyczyna leży gdzie indziej i cofanie nic by nie dało).
    BladKodu, gdy wrócić się nie da.
    """
    gotowe, powod = dostepny()
    if not gotowe:
        raise BladKodu(powod)
    dzialajaca = _ref(REF_DZIALAJACA)
    if dzialajaca is None:
        raise BladKodu("nie mam zapisanej działającej wersji (Jarvis jeszcze ani razu "
                       "nie wystartował poprawnie z tym mechanizmem)")
    baza, head = _rodzic(dzialajaca), _head()
    if baza != head and _git("merge-base", "--is-ancestor", baza, head, sprawdz=False) is None:
        raise BladKodu("ostatnia działająca wersja jest z innej gałęzi — nie cofam automatycznie")

    obecny = zrzut("Jarvis: wersja, która nie wstała po /restart")
    if baza == head and not _zmiany(dzialajaca, obecny):
        return None

    kopia = REF_NIEUDANE + _znacznik_czasu()
    _ustaw_ref(kopia, obecny)       # nieudana wersja razem z jej commitami
    if baza != head:
        # Gałąź wraca do commita działającej wersji; pliki ustawia _przywroc.
        _git("reset", "-q", "--mixed", baza)
    _przywroc(dzialajaca, obecny)
    _wyczysc(uwaga="Nowa wersja Jarvisa nie uruchomiła się po /restart, więc przywróciłem "
                   "ostatnią działającą — zmiany z poprzednich zadań zostały wycofane "
                   f"(są w migawce {kopia}).")
    z_niezatwierdzonymi = _zmiany(baza, dzialajaca) if baza else []
    return (f"Wróciłem do ostatniej działającej wersji: {wersja()}"
            + (" (razem z niezatwierdzonymi zmianami z tamtej chwili)" if z_niezatwierdzonymi else "")
            + f".\nNieudaną wersję zachowałem w migawce {kopia} — podgląd: git diff HEAD {kopia}")
