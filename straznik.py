"""
straznik.py — pilnuje /restart z Telegrama.

Jarvis nie sprawdzi sam, czy jego nowa wersja wstała — w chwili restartu
już go nie ma. Dlatego przed zamknięciem uruchamia osobny, mały proces:
strażnika (`main.py --straznik`, bez okna i bez Qt). Strażnik:

  1. czeka, aż stary Jarvis się zamknie,
  2. uruchamia nowego i daje mu minutę (LIMIT_STARTU_S) na "Systemy gotowe"
     (znacznik z zdalny_kod.gotowy), a potem jeszcze STABILNOSC_S sekund
     — wersja, która pada zaraz po starcie, też nie jest działająca,
  3. wstał — pisze Ci na Telegram i kończy pracę,
     nie wstał — zatrzymuje go, sprząta pulpit (tapeta, pasek boczny),
     wraca do ostatniej działającej wersji (zdalny_kod.przywroc_dzialajaca),
     uruchamia ją i pisze Ci, co się stało i dlaczego.

Swój dziennik prowadzi osobno (zdalny/straznik.log w %APPDATA%\\Jarvis):
jarvis.log w tym samym czasie pisze nowy Jarvis, a dwa procesy obracające
ten sam plik dziennika potrafią go pomieszać.
"""

import json
import logging
import os
import subprocess
import time
import uuid

import konfiguracja
import zdalny_kod

logger = logging.getLogger("straznik")

LIMIT_STARTU_S = 60
STABILNOSC_S = zdalny_kod.STABILNOSC_S
LIMIT_ZAMKNIECIA_S = 30      # tyle czekamy, aż stary Jarvis się zamknie


def _dziennik():
    os.makedirs(zdalny_kod.FOLDER, exist_ok=True)
    logging.basicConfig(filename=zdalny_kod.PLIK_DZIENNIKA_STRAZNIKA, encoding="utf-8",
                        level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")


def _wyslij(tekst):
    """Wiadomość do Ciebie na Telegram (bez Telegrama — tylko do dziennika)."""
    logger.info("Telegram: %s", tekst.replace("\n", " | "))
    try:
        import telegram_bridge

        telegram_bridge.wyslij_wlascicielowi(tekst)
    except Exception:
        logger.exception("Nie wysłałem wiadomości na Telegram")


def _czekaj_na_koniec(pid, utworzony):
    """Czeka, aż stary Jarvis się zamknie. Zawieszonego po LIMIT_ZAMKNIECIA_S zatrzymuje."""
    import psutil

    try:
        proces = psutil.Process(pid)
        # Ten sam numer mógł już dostać inny program — porównujemy chwilę startu.
        if abs(proces.create_time() - utworzony) > 1:
            return
        proces.wait(LIMIT_ZAMKNIECIA_S)
    except psutil.NoSuchProcess:
        return
    except psutil.TimeoutExpired:
        logger.warning("Stary Jarvis nie zamknął się w %d s — zatrzymuję go.", LIMIT_ZAMKNIECIA_S)
        zdalny_kod._zabij_drzewo(pid)
        time.sleep(1)


def _ostatnie_bledy(od_bajtu):
    """Ostatnie błędy z jarvis.log zapisane od chwili restartu — do wiadomości."""
    from logging_setup import PLIK_LOGU

    try:
        with open(PLIK_LOGU, "rb") as plik:
            plik.seek(od_bajtu if os.path.getsize(PLIK_LOGU) >= od_bajtu else 0)
            linie = plik.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    # Od ostatniej linii z ERROR/CRITICAL do końca — zwykle to cały ślad błędu.
    for numer in range(len(linie) - 1, -1, -1):
        if "| ERROR" in linie[numer] or "| CRITICAL" in linie[numer]:
            return "\n".join(linie[numer:numer + 12])[-1500:]
    return ""


def _sprobuj(polecenie, katalog):
    """
    Uruchamia Jarvisa i czeka na jego gotowość.

    Zwraca: (True, proces) albo (False, powód) — w drugim przypadku proces
    jest już zatrzymany.
    """
    from logging_setup import PLIK_LOGU

    identyfikator = uuid.uuid4().hex
    if os.path.exists(zdalny_kod.PLIK_GOTOWOSCI):
        os.remove(zdalny_kod.PLIK_GOTOWOSCI)
    srodowisko = konfiguracja.srodowisko_bez_kluczy(tylko_z_plikow=True)
    srodowisko[zdalny_kod.ZMIENNA_RESTARTU] = identyfikator
    od_bajtu = os.path.getsize(PLIK_LOGU) if os.path.exists(PLIK_LOGU) else 0

    logger.info("Uruchamiam: %s", " ".join(polecenie))
    proces = subprocess.Popen(polecenie, cwd=katalog, env=srodowisko, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                              | zdalny_kod.BEZ_OKNA)

    def nieudany(powod):
        if proces.poll() is None:
            zdalny_kod._zabij_drzewo(proces.pid)
        bledy = _ostatnie_bledy(od_bajtu)
        logger.warning("Nie wstał: %s", powod)
        return False, powod + (f"\n\nOstatni błąd z jarvis.log:\n{bledy}" if bledy else "")

    koniec = time.monotonic() + LIMIT_STARTU_S
    while True:
        if proces.poll() is not None:
            return nieudany(f"proces zakończył się w trakcie startu (kod {proces.returncode})")
        try:
            with open(zdalny_kod.PLIK_GOTOWOSCI, encoding="utf-8") as plik:
                if plik.read().strip() == identyfikator:
                    break
        except OSError:
            pass
        if time.monotonic() > koniec:
            return nieudany(f"nie zgłosił gotowości w {LIMIT_STARTU_S} s")
        time.sleep(0.5)

    logger.info("Zgłosił gotowość — sprawdzam, czy działa dalej (%d s).", STABILNOSC_S)
    koniec = time.monotonic() + STABILNOSC_S
    while time.monotonic() < koniec:
        if proces.poll() is not None:
            return nieudany(f"padł zaraz po starcie (kod {proces.returncode})")
        time.sleep(0.5)
    return True, proces


def _posprzataj_pulpit():
    """Zatrzymany Jarvis nie zdążył zdjąć tapety ani paska — robimy to za niego."""
    try:
        import pasek_boczny
        import tapeta

        tapeta.przywroc_pulpit()
        pasek_boczny.usun_stary_pasek()
    except Exception:
        logger.exception("Sprzątanie pulpitu nie wyszło")


def uruchom():
    """Wejście z main.py (--straznik). Zwraca kod wyjścia."""
    _dziennik()
    konfiguracja.wczytaj()
    try:
        with open(zdalny_kod.PLIK_RESTARTU, encoding="utf-8") as plik:
            dane = json.load(plik)
        os.remove(zdalny_kod.PLIK_RESTARTU)
    except (OSError, ValueError):
        logger.exception("Brak opisu restartu — nie wiem, co uruchomić")
        return 1

    logger.info("=" * 40)
    logger.info("Restart z Telegrama — czekam, aż stary Jarvis się zamknie.")
    _czekaj_na_koniec(dane["pid"], dane["utworzony"])

    polecenie, katalog = dane["polecenie"], dane["katalog"]
    udany, wynik = _sprobuj(polecenie, katalog)
    if udany:
        _wyslij(f"✅ Jarvis wstał po restarcie — działa nowa wersja ({zdalny_kod.wersja()}).")
        return 0

    powod = wynik
    _posprzataj_pulpit()
    try:
        cofniecie = zdalny_kod.przywroc_dzialajaca()
    except zdalny_kod.BladKodu as e:
        cofniecie = f"Nie cofnąłem kodu: {e}."
    except Exception:
        logger.exception("Wycofanie wersji wysypało się")
        cofniecie = "Nie cofnąłem kodu — wycofanie wysypało się (szczegóły w straznik.log)."
    if cofniecie is None:
        cofniecie = ("Kod jest taki sam jak w ostatniej działającej wersji, więc nie mam do czego "
                     "wracać — próbuję uruchomić go jeszcze raz.")

    udany, wynik = _sprobuj(polecenie, katalog)
    if udany:
        stan = "Ta wersja działa."
    else:
        _posprzataj_pulpit()
        stan = (f"Ta też nie wstała ({wynik.splitlines()[0]}) — Jarvis jest wyłączony. "
                "Zajrzyj do jarvis.log na komputerze.")
    _wyslij(f"⚠️ Nowa wersja Jarvisa nie uruchomiła się: {powod}\n\n{cofniecie}\n\n{stan}")
    return 0 if udany else 1
