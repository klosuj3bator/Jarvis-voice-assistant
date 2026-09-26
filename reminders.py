"""
reminders.py — timery i przypomnienia.

"Przypomnij mi za dwadzieścia minut o praniu", "timer na pięć minut",
"jakie mam przypomnienia", "anuluj to o praniu".


DWIE RZECZY, KTÓRE ODRÓŻNIAJĄ TEN MODUŁ OD POZOSTAŁYCH
======================================================

1. DZIAŁA SAM Z SIEBIE. Wszystko inne w Jarvisie jest odpowiedzią na Twoje
   słowa: mówisz, on robi. Tu jest odwrotnie — osobny wątek budzi się co dwie
   sekundy, sprawdza zegar i w pewnym momencie zaczyna mówić z własnej
   inicjatywy. To pierwsze miejsce, w którym program odzywa się niepytany.

2. MUSI SIĘ UMÓWIĆ Z RESZTĄ. Skoro odzywa się w nieprzewidywalnej chwili,
   może trafić dokładnie w moment, gdy Jarvis odpowiada na pytanie albo
   nagrywa Twoją komendę. Dlatego przed mówieniem bierze wspólną blokadę
   z zajetosc.py i czeka, aż rozmowa się skończy. Przypomnienie spóźnione
   o dwadzieścia sekund jest nieszkodliwe; dwa głosy naraz psują i wypowiedź,
   i nagranie.


PRZYPOMNIENIA PRZEŻYWAJĄ RESTART
================================

Lista leży w reminders.json obok programu. Zapisujemy ją po każdej zmianie,
a nie przy zamykaniu — bo Jarvisa zamyka się różnie, czasem przez zabicie
procesu, i wtedy nie ma okazji niczego zapisać.

Po starcie przypomnienia, których czas minął w trakcie przestoju, są
wypowiadane jako spóźnione. Te sprzed więcej niż TOLERANCJA_SPOZNIENIA_H
godzin po prostu kasujemy — przypomnienie o praniu sprzed trzech dni
nie jest już przypomnieniem, tylko wyrzutem sumienia.
"""

import datetime
import json
import logging
import os
import re
import threading
import uuid

import zajetosc

logger = logging.getLogger(__name__)

SCIEZKA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reminders.json")

# Co ile sekund wątek sprawdza zegar. Dwie sekundy to kompromis: dokładność
# wystarczająca dla przypomnień (nikt nie mierzy nimi sekund), a procesor
# w praktyce tego nie zauważa.
ODSTEP_SPRAWDZANIA_S = 2.0

# Po ilu godzinach spóźnione przypomnienie przestaje mieć sens.
TOLERANCJA_SPOZNIENIA_H = 12

# Od ilu minut spóźnienia Jarvis zaznacza, że przypomnienie jest spóźnione.
PROG_SPOZNIENIA_MIN = 5

# Bezpiecznik przed zaśmieceniem: tyle przypomnień naraz w zupełności starcza.
MAX_PRZYPOMNIEN = 50

# Blokada listy — wątek przypomnień ją czyta i kasuje z niej wpisy, a wątek
# rozmowy w tym samym czasie może dopisywać nowe. To INNA blokada niż ta
# z zajetosc.py: tamta pilnuje mówienia, ta pilnuje pliku i listy w pamięci.
_blokada_listy = threading.Lock()

_zatrzymaj = threading.Event()
_watek = None
_mow = None
_ustaw_stan = None
_stan_teraz = None

# Dodatkowe miejsca, do których trafia każde przypomnienie — np. Telegram.
# Każdy odbiorca to funkcja przyjmująca gotowy tekst przypomnienia.
_odbiorcy = []


def dodaj_odbiorce(funkcja):
    """
    Rejestruje dodatkowy kanał dla przypomnień (np. wysyłkę na telefon).

    Odbiorcy dostają przypomnienie OD RAZU, zanim Jarvis zacznie je mówić —
    nie czekają, aż skończy rozmowę. Na telefonie to i tak nikomu nie przeszkodzi.
    """
    if funkcja not in _odbiorcy:
        _odbiorcy.append(funkcja)


# ---------------------------------------------------------------
# Plik
# ---------------------------------------------------------------

def _wczytaj():
    """Zwraca listę przypomnień z pliku (pustą, gdy pliku nie ma)."""
    if not os.path.exists(SCIEZKA):
        return []
    try:
        with open(SCIEZKA, encoding="utf-8") as plik:
            dane = json.load(plik)
        return dane.get("przypomnienia", [])
    except (OSError, ValueError):
        # Uszkodzony plik nie może zatrzymać Jarvisa. Zaczynamy od pustej
        # listy — gorsze od utraty przypomnień byłoby tylko to, że przez nie
        # program w ogóle nie wstanie.
        logger.exception("Nie udało się wczytać %s — zaczynam od pustej listy", SCIEZKA)
        return []


def _zapisz(przypomnienia):
    """Zapisuje listę do pliku."""
    try:
        with open(SCIEZKA, "w", encoding="utf-8") as plik:
            json.dump({"przypomnienia": przypomnienia}, plik,
                      ensure_ascii=False, indent=2)
    except OSError:
        logger.exception("Nie udało się zapisać %s", SCIEZKA)


# ---------------------------------------------------------------
# Czas
# ---------------------------------------------------------------

def _o_godzinie(tekst, teraz, jutro=False):
    """
    Zamienia "15:30" na najbliższy taki moment.

    Jeśli ta godzina dziś już minęła, bierzemy jutro — "przypomnij o siódmej"
    powiedziane wieczorem znaczy jutro rano, nie "za minus dwanaście godzin".

    jutro — wymusza jutrzejszy dzień. Potrzebne, bo "jutro o ósmej"
            powiedziane o siódmej rano wypadłoby inaczej na dziś.

    Zwraca: datetime albo None, gdy tekst nie wygląda jak godzina.
    """
    dopasowanie = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", tekst or "")
    if not dopasowanie:
        return None

    godzina, minuta = int(dopasowanie.group(1)), int(dopasowanie.group(2))
    if not (0 <= godzina <= 23 and 0 <= minuta <= 59):
        return None

    kiedy = teraz.replace(hour=godzina, minute=minuta, second=0, microsecond=0)
    if jutro or kiedy <= teraz:
        kiedy += datetime.timedelta(days=1)
    return kiedy


def _ktory_dzien(kiedy, teraz):
    """"dziś", "jutro" albo data — żeby potwierdzenie nie było dwuznaczne."""
    roznica = (kiedy.date() - teraz.date()).days
    if roznica == 0:
        return "dziś"
    if roznica == 1:
        return "jutro"
    return kiedy.strftime("%d.%m")


def _po_polsku(minuty):
    """Zamienia liczbę minut na opis w rodzaju "za 1 godzinę 20 minut"."""
    if minuty < 1:
        return f"za {max(1, round(minuty * 60))} s"
    minuty = int(round(minuty))
    if minuty < 60:
        return f"za {minuty} min"
    godziny, reszta = divmod(minuty, 60)
    if reszta == 0:
        return f"za {godziny} godz."
    return f"za {godziny} godz. {reszta} min"


# ---------------------------------------------------------------
# Publiczne API — to woła agent.py
# ---------------------------------------------------------------

def dodaj(tresc=None, za_minut=None, o_godzinie=None, jutro=False):
    """
    Dodaje przypomnienie.

    tresc      — o czym przypomnieć (może być puste — wtedy to zwykły timer)
    za_minut   — za ile minut (może być ułamkiem, np. 0.5)
    o_godzinie — albo konkretna godzina jako "HH:MM"
    jutro      — razem z o_godzinie: na pewno jutro, nie dziś

    Zwraca: (komunikat, czy_się_udało).
    """
    teraz = datetime.datetime.now()

    if za_minut is not None:
        try:
            minuty = float(za_minut)
        except (TypeError, ValueError):
            return "Nie zrozumiałem, za ile minut przypomnieć.", False
        if minuty <= 0:
            return "Czas przypomnienia musi być w przyszłości.", False
        kiedy = teraz + datetime.timedelta(minutes=minuty)
    elif o_godzinie:
        kiedy = _o_godzinie(str(o_godzinie), teraz, jutro=bool(jutro))
        if kiedy is None:
            return f"Nie rozumiem godziny {o_godzinie!r}. Podaj ją jako HH:MM.", False
    else:
        return "Nie podano, kiedy przypomnieć.", False

    tresc = " ".join((tresc or "").split())

    with _blokada_listy:
        przypomnienia = _wczytaj()
        if len(przypomnienia) >= MAX_PRZYPOMNIEN:
            return (f"Mam już {MAX_PRZYPOMNIEN} przypomnień — anuluj któreś, "
                    "zanim dodasz kolejne.", False)

        przypomnienia.append({
            "id": uuid.uuid4().hex[:6],
            "tresc": tresc,
            "kiedy": kiedy.isoformat(timespec="seconds"),
        })
        _zapisz(przypomnienia)

    za_ile = (kiedy - teraz).total_seconds() / 60
    logger.info("[PRZYPOMNIENIE] na %s: %s", kiedy.strftime("%H:%M"), tresc or "(timer)")

    opis = tresc or "timer"
    return (f"Ustawione: {opis} — {_ktory_dzien(kiedy, teraz)} "
            f"{kiedy.strftime('%H:%M')} ({_po_polsku(za_ile)}).", True)


def aktywne():
    """Zwraca listę przypomnień posortowaną po czasie (najbliższe pierwsze)."""
    with _blokada_listy:
        przypomnienia = _wczytaj()
    return sorted(przypomnienia, key=lambda p: p.get("kiedy", ""))


def lista():
    """
    Spis przypomnień w formie gotowej dla modelu.

    Brak przypomnień to NIE błąd — "nic nie masz" jest poprawną odpowiedzią.

    Zwraca: (komunikat, czy_się_udało).
    """
    przypomnienia = aktywne()
    if not przypomnienia:
        return "Brak aktywnych przypomnień.", True

    teraz = datetime.datetime.now()
    linie = []
    for wpis in przypomnienia:
        kiedy = datetime.datetime.fromisoformat(wpis["kiedy"])
        minuty = (kiedy - teraz).total_seconds() / 60
        linie.append(f"{_ktory_dzien(kiedy, teraz)} {kiedy.strftime('%H:%M')} "
                     f"({_po_polsku(minuty)}): {wpis['tresc'] or 'timer'}")

    return f"Aktywne przypomnienia ({len(linie)}):\n" + "\n".join(linie), True


def anuluj(fragment=None):
    """
    Anuluje przypomnienia pasujące do fragmentu treści.

    fragment — kawałek treści ("pranie") albo "wszystkie"

    Zwraca: (komunikat, czy_się_udało).
    """
    fragment = (fragment or "").strip().lower()

    with _blokada_listy:
        przypomnienia = _wczytaj()
        if not przypomnienia:
            return "Nie ma żadnych przypomnień do anulowania.", True

        if fragment in ("", "wszystkie", "all"):
            zostaja, usuniete = [], przypomnienia
        else:
            usuniete = [p for p in przypomnienia if fragment in p["tresc"].lower()]
            zostaja = [p for p in przypomnienia if p not in usuniete]

        if not usuniete:
            return (f"Nie mam przypomnienia o {fragment!r}. "
                    "Powiedz 'jakie mam przypomnienia', żeby usłyszeć listę.", False)

        _zapisz(zostaja)

    logger.info("[PRZYPOMNIENIE] anulowano %d", len(usuniete))
    if len(usuniete) == 1:
        opis = usuniete[0]["tresc"] or "timer"
        return f"Anulowane: {opis}.", True
    return f"Anulowane wszystkie ({len(usuniete)}).", True


# ---------------------------------------------------------------
# Wątek pilnujący zegara
# ---------------------------------------------------------------

def _zapadle(teraz):
    """
    Wyjmuje z listy przypomnienia, których czas już minął.

    Wyjmowanie i zapis robimy pod blokadą i OD RAZU — gdyby program padł
    w trakcie mówienia, przypomnienie nie odezwie się po restarcie po raz drugi.

    Zwraca: listę wpisów do wypowiedzenia.
    """
    with _blokada_listy:
        przypomnienia = _wczytaj()
        zapadle, zostaja = [], []

        for wpis in przypomnienia:
            try:
                kiedy = datetime.datetime.fromisoformat(wpis["kiedy"])
            except (KeyError, ValueError):
                logger.warning("Pomijam uszkodzony wpis: %r", wpis)
                continue

            if kiedy > teraz:
                zostaja.append(wpis)
                continue

            spoznienie_h = (teraz - kiedy).total_seconds() / 3600
            if spoznienie_h > TOLERANCJA_SPOZNIENIA_H:
                logger.info("Kasuję przypomnienie sprzed %.0f godzin: %s",
                            spoznienie_h, wpis.get("tresc"))
                continue

            zapadle.append(wpis)

        if zapadle or len(zostaja) != len(przypomnienia):
            _zapisz(zostaja)

    return zapadle


def _wypowiedz(wpis, teraz):
    """Mówi jedno przypomnienie, czekając na swoją kolej."""
    kiedy = datetime.datetime.fromisoformat(wpis["kiedy"])
    spoznienie_min = (teraz - kiedy).total_seconds() / 60
    tresc = wpis.get("tresc") or ""

    if tresc:
        tekst = f"Przypomnienie: {tresc}."
    else:
        tekst = "Minął ustawiony czas."

    if spoznienie_min > PROG_SPOZNIENIA_MIN:
        tekst = (f"Spóźnione przypomnienie z godziny {kiedy.strftime('%H:%M')}. "
                 + tekst)

    # Najpierw pozostałe kanały (Telegram). Awaria jednego z nich nie może
    # zatrzymać przypomnienia na głos.
    for odbiorca in list(_odbiorcy):
        try:
            odbiorca(tekst)
        except Exception:
            logger.exception("Odbiorca przypomnienia zawiódł")

    # Czekamy, aż Jarvis skończy mówić i słuchać. Bez tego przypomnienie
    # weszłoby mu w zdanie — opis w zajetosc.py.
    if not zajetosc.czy_wolny():
        logger.info("[PRZYPOMNIENIE] czekam, trwa: %s", zajetosc.czym_zajety())

    with zajetosc.zajmij("przypomnienie"):
        poprzedni_stan = _stan_teraz() if _stan_teraz else None
        if _ustaw_stan:
            _ustaw_stan("speaking")

        logger.info("[PRZYPOMNIENIE] mówię: %s", tekst)
        _mow(tekst)

        if _ustaw_stan:
            # Wracamy do stanu sprzed przypomnienia. "error" pomijamy —
            # to chwilowy błysk, który zdążył już zgasnąć.
            _ustaw_stan(poprzedni_stan if poprzedni_stan in ("no_mic", "offline")
                        else "idle")


def _petla():
    """Wątek w tle: co dwie sekundy sprawdza, czy coś zapadło."""
    logger.info("Pilnuję przypomnień (%d aktywnych).", len(aktywne()))

    while not _zatrzymaj.is_set():
        try:
            teraz = datetime.datetime.now()
            for wpis in _zapadle(teraz):
                if _zatrzymaj.is_set():
                    break
                _wypowiedz(wpis, teraz)
        except Exception:
            # Wyjątek nie może zabić wątku — bez niego przypomnienia
            # przestałyby działać po cichu, aż do restartu programu.
            logger.exception("Błąd w pętli przypomnień")

        _zatrzymaj.wait(ODSTEP_SPRAWDZANIA_S)

    logger.info("Pętla przypomnień zakończona.")


def uruchom(mow, ustaw_stan=None, stan_teraz=None):
    """
    Startuje wątek pilnujący przypomnień. Woła to main.py po wczytaniu modułów.

    mow        — funkcja wypowiadająca tekst (tts.mow)
    ustaw_stan — funkcja zmieniająca stan HUD-a (orb.set_state)
    stan_teraz — funkcja zwracająca bieżący stan HUD-a

    Funkcje wstrzykujemy zamiast importować tts i gui, bo dzięki temu ten
    moduł da się przetestować bez głośnika, mikrofonu i okna.
    """
    global _watek, _mow, _ustaw_stan, _stan_teraz

    if _watek is not None and _watek.is_alive():
        return _watek

    _mow, _ustaw_stan, _stan_teraz = mow, ustaw_stan, stan_teraz
    _zatrzymaj.clear()
    _watek = threading.Thread(target=_petla, name="watek-przypomnien", daemon=True)
    _watek.start()
    return _watek


def zatrzymaj():
    """Prosi wątek o zakończenie. BEZPIECZNE z dowolnego wątku."""
    _zatrzymaj.set()


# --- Test: `python reminders.py` — przypomnienie za 5 sekund, bez głosu ---
if __name__ == "__main__":
    import time

    from logging_setup import skonfiguruj_logowanie

    skonfiguruj_logowanie()

    SCIEZKA = os.path.join(os.path.dirname(SCIEZKA), "reminders_test.json")

    print(dodaj("test modułu", za_minut=5 / 60)[0])
    print(dodaj("pranie", o_godzinie="07:30")[0])
    print(lista()[0])

    uruchom(mow=lambda tekst: print(f"  [GŁOŚNIK] {tekst}"))
    time.sleep(8)
    zatrzymaj()

    print(lista()[0])
    print(anuluj("wszystkie")[0])
    os.remove(SCIEZKA)
