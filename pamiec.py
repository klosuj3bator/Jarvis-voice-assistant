"""
pamiec.py — trwała pamięć Jarvisa.

Do tej pory Jarvis zapominał wszystko po ośmiu sekundach ciszy, a po restarcie
komputera nie wiedział nawet, jak masz na imię. Ten moduł to zmienia.


CO JARVIS PAMIĘTA
=================

Dwie różne rzeczy, bo mają różny okres przydatności:

  FAKTY        — pamięć długoterminowa: "pracuję do 16", "mów do mnie
  (memory.json)  mistrzu". Zapisywane WYŁĄCZNIE na Twoją wyraźną prośbę
                 ("zapamiętaj, że…") — narzędziami remember_fact,
                 forget_fact i list_memories w agent.py. Trafiają do
                 instrukcji Jarvisa przy każdym zapytaniu.

  OSTATNIA     — dosłowny zapis końcówki poprzedniej rozmowy. Dzięki temu
  ROZMOWA        możesz wrócić po godzinie i powiedzieć "wróćmy do tego,
  (pamiec.json)  o czym mówiliśmy", a on wie, o co chodzi.

Wszystko ląduje w zwykłych plikach JSON obok programu. To celowe — możesz je
otworzyć notatnikiem i zobaczyć dokładnie, co Jarvis o Tobie wie, poprawić
to albo skasować. Żadnej magii, żadnej bazy danych. memory.json jest
w .gitignore — to Twoje prywatne dane, nie część kodu.


HASŁA, KLUCZE, KODY — NIGDY
===========================

Pamięć długoterminowa trafia do instrukcji przy KAŻDYM zapytaniu i leży
na dysku zwykłym tekstem. Hasło zapisane tam raz wysyłałoby się dalej przy
każdym "puść muzykę". Dlatego wyglada_na_sekret() pilnuje tego w kodzie,
niezależnie od modelu: zapamietaj_fakt() odmawia zapisu wszystkiego, co
wygląda na hasło, PIN, klucz, token, kod dostępu albo numer karty czy konta.

Ta sama funkcja chroni też zapis końcówki rozmowy (zapisz_rozmowe) i dziennik
jarvis.log (ukryj_jesli_sekret) — jeśli powiesz hasło na głos, nie zostanie
na dysku.

Sprawdzanie jest celowo przewrażliwione: lepiej odmówić zapisania "hasła
reklamowego Allegro" niż przepuścić prawdziwe hasło.
"""

import json
import logging
import os
import re
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

KATALOG = os.path.dirname(os.path.abspath(__file__))
SCIEZKA_PAMIECI = os.path.join(KATALOG, "pamiec.json")    # końcówka rozmowy
SCIEZKA_FAKTOW = os.path.join(KATALOG, "memory.json")     # pamięć długoterminowa

# Ile ostatnich wiadomości z poprzedniej rozmowy odtwarzamy przy starcie.
# Za mało — Jarvis gubi wątek. Za dużo — każde zapytanie niesie ze sobą
# wielką porcję starego tekstu, co kosztuje i czasem myli.
ILE_ODTWARZAMY = 12

# Górny limit faktów. Cała lista jedzie z każdym zapytaniem, więc bez limitu
# rosłaby w nieskończoność. Po osiągnięciu limitu NIE kasujemy starych po
# cichu — Jarvis odmówi i poprosi, żebyś coś zapomniał.
MAX_FAKTOW = 60

# Fakt to jedno krótkie zdanie. Dłuższe odrzucamy — pamięć ma być zwięzła,
# bo jedzie z każdym zapytaniem.
MAKS_ZNAKOW_FAKTU = 200

# Zapis faktów z dwóch wątków naraz (głos i Telegram) mógłby zgubić jeden
# z nich — ten sam plik czytany i nadpisywany równocześnie.
_blokada_faktow = threading.Lock()


# ---------------------------------------------------------------
# Hasła, klucze, kody — patrz opis modułu
# ---------------------------------------------------------------

_WZORCE_SEKRETOW = [
    # Hasła i PIN-y w różnych formach: hasło, hasła, haseł, password, PIN-u...
    r"\bhas[lł](o|a|em|u|e)?\b", r"\bhase[lł]\b", r"\bha[sś]le\b", r"pass(word|wd|code)",
    r"\bpin(u|em|y)?\b", r"\bpuk\b", r"\bcv[cv]2?\b", r"\b2fa\b", r"\botp\b",
    # Klucze i tokeny — formy słowa "klucz" (ale nie "kluczowy"), pojedynczy
    # "token" (ale nie "tokeny" z rozmów o kosztach Jarvisa).
    r"\bklucz(a|e|em|u|y|ach|ami|om)?\b", r"api[ _-]?key", r"\btoken(a|u|em)?\b",
    r"\bsecret\b", r"\bsekret(u|y|em|ów)?\b", r"dane logowania", r"credential",
    r"\bseed\b", r"fraz\w* odzyskiw",
    # Kody: "kod PIN", "kod do bramy", "kod 4521" — ale nie "piszę kod w Pythonie".
    r"\bkod\w*\s+(do\s+)?(pin|dost[eę]p|weryfik|sms|blik|autoryz|zabezp|bram|alarm|"
    r"jednoraz|puk|drzw|domofon|sejf|telefon|kart|furtk|gara[zż]|2fa|otp|odblok|reset)",
    r"\bkod\w*\W{0,3}\d{3,}",
    # Numery kart i kont: 12+ cyfr, także ze spacjami czy myślnikami.
    r"(\d[ -]?){12,}",
    # Ciągi wyglądające na klucz API: 16+ znaków, litery i cyfry wymieszane.
    r"(?=[A-Za-z0-9_\-]*\d)(?=[A-Za-z0-9_\-]*[A-Za-z])[A-Za-z0-9_\-]{16,}",
]
_SEKRET = re.compile("|".join(f"(?:{w})" for w in _WZORCE_SEKRETOW), re.IGNORECASE)


def wyglada_na_sekret(tekst):
    """Czy tekst wygląda na hasło, klucz, kod albo numer karty/konta."""
    return bool(_SEKRET.search(tekst or ""))


def ukryj_jesli_sekret(tekst):
    """Tekst do zapisania na dysku — zastąpiony opisem, jeśli wygląda na sekret."""
    if wyglada_na_sekret(tekst):
        return "[ukryte — wyglądało na hasło, klucz albo kod]"
    return tekst


# ---------------------------------------------------------------
# Pamięć długoterminowa: memory.json
# ---------------------------------------------------------------

def _przenies_stare_fakty():
    """
    Jednorazowe przeniesienie faktów z pamiec.json do memory.json.

    Wcześniej fakty leżały w pamiec.json razem z końcówką rozmowy. Ten plik
    jest śledzony przez git, a fakty o Tobie nie powinny trafiać do
    repozytorium. Kolejność jest ostrożna: najpierw zapis nowego pliku
    i sprawdzenie, że da się go odczytać — dopiero potem usunięcie ze starego.
    """
    dane = wczytaj()
    stare = dane.get("fakty")
    if not stare:
        return []

    dzien = (dane.get("zapisano") or datetime.now().isoformat())[:10]
    fakty = [{"fakt": f, "dodano": dzien} for f in stare if isinstance(f, str) and f.strip()]
    try:
        _zapisz_fakty(fakty)
        udane = _wczytaj_plik_faktow() == fakty
    except OSError:
        udane = False
    if not udane:
        logger.error("[PAMIĘĆ] Przeniesienie faktów do memory.json nie powiodło się — "
                     "zostawiam je w pamiec.json i spróbuję następnym razem.")
        return fakty

    dane.pop("fakty", None)
    _zapisz(dane)
    logger.info("[PAMIĘĆ] Przeniosłem %d faktów z pamiec.json do memory.json.", len(fakty))
    return fakty


def _wczytaj_plik_faktow():
    """Zawartość memory.json albo None, jeśli pliku nie ma lub jest uszkodzony."""
    try:
        with open(SCIEZKA_FAKTOW, encoding="utf-8") as plik:
            return json.load(plik).get("fakty", [])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, AttributeError) as e:
        logger.error("Nie udało się wczytać %s (%s) — traktuję pamięć jako pustą.",
                     SCIEZKA_FAKTOW, e)
        return None


def _wczytaj_fakty():
    """Lista faktów: [{"fakt": "...", "dodano": "RRRR-MM-DD"}, ...]."""
    fakty = _wczytaj_plik_faktow()
    if fakty is None and not os.path.exists(SCIEZKA_FAKTOW):
        fakty = _przenies_stare_fakty()
    return fakty or []


def _zapisz_fakty(fakty):
    """
    Zapis memory.json przez plik tymczasowy: gdyby program padł w połowie
    zapisu, zostaje stary, cały plik — a nie urwany w pół zdania.
    """
    tymczasowy = SCIEZKA_FAKTOW + ".tmp"
    with open(tymczasowy, "w", encoding="utf-8") as plik:
        json.dump({"fakty": fakty}, plik, indent=2, ensure_ascii=False)
    os.replace(tymczasowy, SCIEZKA_FAKTOW)


def zapamietaj_fakt(fakt):
    """
    Dopisuje fakt do pamięci długoterminowej (narzędzie remember_fact).

    To, czy użytkownik w ogóle o to poprosił, sprawdza agent.py. Tu pilnujemy
    tego, co wolno zapisać: żadnych sekretów, krótko, bez duplikatów.

    Zwraca: (komunikat dla modelu, czy_się_udało).
    """
    fakt = " ".join((fakt or "").split())
    if not fakt:
        return "Pusty fakt — nie zapisałem.", False

    if wyglada_na_sekret(fakt):
        # Treści NIE logujemy — mogłaby trafić do jarvis.log właśnie to hasło.
        logger.warning("[PAMIĘĆ] Odmowa zapisu — fakt wygląda na hasło, klucz albo kod.")
        return ("NIE ZAPISANO: to wygląda na hasło, PIN, klucz, token, kod albo numer "
                "karty czy konta. Takich rzeczy nigdy nie przechowuję — powiedz to "
                "użytkownikowi i nie powtarzaj tej wartości na głos."), False

    if len(fakt) > MAKS_ZNAKOW_FAKTU:
        return (f"Za długie ({len(fakt)} znaków, limit {MAKS_ZNAKOW_FAKTU}). "
                "Streść to do jednego krótkiego zdania i zapisz jeszcze raz."), False

    with _blokada_faktow:
        fakty = _wczytaj_fakty()

        # Model bywa powtarzalny — porównujemy bez wielkości liter.
        if any(f["fakt"].lower() == fakt.lower() for f in fakty):
            return "To już pamiętam.", True

        if len(fakty) >= MAX_FAKTOW:
            return (f"Pamięć pełna ({MAX_FAKTOW} faktów). Nie kasuję niczego po "
                    "cichu — poproś użytkownika, żeby wskazał, co zapomnieć."), False

        fakty.append({"fakt": fakt, "dodano": datetime.now().strftime("%Y-%m-%d")})
        _zapisz_fakty(fakty)

    logger.info("[PAMIĘĆ] Zapamiętałem: %s", fakt)
    return f"Zapamiętane: {fakt}", True


def zapomnij_fakt(fragment):
    """
    Usuwa fakty zawierające podany fragment (narzędzie forget_fact).

    Zwraca: (komunikat z listą usuniętych faktów, czy_się_udało).
    """
    fragment = " ".join((fragment or "").split()).lower()
    if not fragment:
        return "Nie wiem, co mam zapomnieć.", False

    with _blokada_faktow:
        fakty = _wczytaj_fakty()
        usuniete = [f for f in fakty if fragment in f["fakt"].lower()]
        if not usuniete:
            return f"Nie znalazłem w pamięci niczego z {fragment!r}.", False
        _zapisz_fakty([f for f in fakty if f not in usuniete])

    logger.info("[PAMIĘĆ] Zapomniałem %d fakt(ów) z %r.", len(usuniete), fragment)
    return "Zapomniane: " + "; ".join(f["fakt"] for f in usuniete), True


def fakty():
    """Same teksty faktów — np. dla słownika Whispera (slownik.py)."""
    return [f["fakt"] for f in _wczytaj_fakty()]


def lista_faktow():
    """Cała pamięć długoterminowa (narzędzie list_memories). Zwraca (komunikat, ok)."""
    fakty = _wczytaj_fakty()
    if not fakty:
        return "Pamięć długoterminowa jest pusta — użytkownik nic nie kazał zapamiętać.", True
    return (f"W pamięci ({len(fakty)}):\n"
            + "\n".join(f"- {f['fakt']} (od {f['dodano']})" for f in fakty)), True


# Tekst pamięci do instrukcji, zbudowany raz i odświeżany tylko po zmianie
# pliku. Patrz kontekst_do_promptu().
_prompt_pamieci = {"zmieniono": object(), "tekst": ""}


def kontekst_do_promptu():
    """
    Fakty w postaci doklejanej do instrukcji Jarvisa — krótko, jeden na linię.

    Wczytujemy je przy starcie i potem tylko wtedy, gdy memory.json się
    zmieni (dopisanie, zapomnienie albo Twoja ręczna poprawka w notatniku).
    Tekst musi być co do bajta taki sam między zapytaniami — inaczej cache
    w agent.py musiałby zapisywać instrukcje od nowa.

    Zwraca: string (pusty, jeśli pamięć jest pusta).
    """
    try:
        zmieniono = os.path.getmtime(SCIEZKA_FAKTOW)
    except OSError:
        zmieniono = None

    if zmieniono is None or zmieniono != _prompt_pamieci["zmieniono"]:
        fakty = _wczytaj_fakty()
        _prompt_pamieci["tekst"] = (
            "CO WIESZ O UŻYTKOWNIKU (zapamiętane na jego prośbę):\n"
            + "\n".join(f"- {f['fakt']}" for f in fakty)
        ) if fakty else ""
        # Czas zmiany sprzed odczytu: jeśli plik zmieni się w trakcie,
        # następne wywołanie zauważy różnicę i wczyta go jeszcze raz.
        _prompt_pamieci["zmieniono"] = zmieniono
    return _prompt_pamieci["tekst"]


# ---------------------------------------------------------------
# Końcówka rozmowy: pamiec.json
# ---------------------------------------------------------------

def _pusta():
    return {"ostatnia_rozmowa": [], "zapisano": None}


def wczytaj():
    """
    Wczytuje pamiec.json (końcówkę rozmowy).

    Zwraca: słownik z kluczami "ostatnia_rozmowa", "zapisano".
    Przy braku pliku albo uszkodzeniu zwraca pustą pamięć — Jarvis zacznie
    od zera, ale się nie wywali.
    """
    if not os.path.exists(SCIEZKA_PAMIECI):
        return _pusta()

    try:
        with open(SCIEZKA_PAMIECI, encoding="utf-8") as plik:
            dane = json.load(plik)
    except (json.JSONDecodeError, OSError) as e:
        logger.error("Nie udało się wczytać pamięci (%s) — zaczynam od pustej.", e)
        return _pusta()

    # Uzupełniamy brakujące klucze, żeby reszta kodu nie musiała ich sprawdzać.
    pusta = _pusta()
    pusta.update(dane)
    return pusta


def _zapisz(dane):
    """Zapisuje pamiec.json. Błąd zapisu logujemy, ale nie przerywamy pracy."""
    dane["zapisano"] = datetime.now().isoformat(timespec="seconds")
    try:
        with open(SCIEZKA_PAMIECI, "w", encoding="utf-8") as plik:
            json.dump(dane, plik, indent=2, ensure_ascii=False)
    except OSError as e:
        logger.error("Nie udało się zapisać pamięci: %s", e)


def zapisz_rozmowe(historia):
    """
    Zachowuje końcówkę rozmowy, żeby następnym razem dało się do niej wrócić.

    historia — lista wiadomości w formacie API

    Zapisujemy TYLKO zwykłe wypowiedzi (tekst użytkownika i Jarvisa).
    Wywołania narzędzi i ich wyniki pomijamy — po restarcie są bezwartościowe
    (odwołują się do nieistniejących już identyfikatorów), a zajmują mnóstwo
    miejsca i mogą wprawić model w zakłopotanie. Wypowiedzi wyglądające na
    hasło czy kod zastępujemy opisem (patrz opis modułu).

    Uwaga na kształt danych, bo łatwo się tu pomylić: wypowiedzi użytkownika
    mają treść w postaci zwykłego tekstu, ale odpowiedzi Jarvisa przychodzą
    jako LISTA bloków (tekst, wywołania narzędzi, rozumowanie). Pierwsza wersja
    tej funkcji przepuszczała tylko tekst i przez to gubiła WSZYSTKIE odpowiedzi
    Jarvisa — na dysk trafiał sam monolog użytkownika. Po wczytaniu wyglądało to
    tak, jakbyś przed chwilą wydał te komendy jeszcze raz, więc Jarvis
    posłusznie odpowiadał na nie od nowa.
    """
    czyste = []

    for wiadomosc in historia:
        tresc = wiadomosc.get("content")
        rola = wiadomosc.get("role")

        if isinstance(tresc, str):
            # Zwykła wypowiedź użytkownika.
            if tresc.strip():
                czyste.append({"role": rola, "content": ukryj_jesli_sekret(tresc)})
            continue

        if not isinstance(tresc, list):
            continue

        # Lista bloków. Po stronie użytkownika to wyniki narzędzi — pomijamy.
        if rola != "assistant":
            continue

        # Po stronie Jarvisa wyciągamy sam wypowiedziany tekst, pomijając
        # wywołania narzędzi i bloki rozumowania.
        fragmenty = []
        for blok in tresc:
            typ = blok.get("type") if isinstance(blok, dict) else getattr(blok, "type", None)
            if typ != "text":
                continue
            tekst = blok.get("text") if isinstance(blok, dict) else getattr(blok, "text", "")
            if tekst and tekst.strip():
                fragmenty.append(tekst.strip())

        if fragmenty:
            czyste.append({"role": "assistant",
                           "content": ukryj_jesli_sekret(" ".join(fragmenty))})

    if not czyste:
        return

    czyste = czyste[-ILE_ODTWARZAMY:]

    # Historia musi zaczynać się od wypowiedzi użytkownika — inaczej API
    # odrzuci ją przy odtworzeniu. Ścinamy początek do najbliższego "user".
    while czyste and czyste[0]["role"] != "user":
        czyste.pop(0)

    dane = wczytaj()
    dane["ostatnia_rozmowa"] = czyste
    _zapisz(dane)
    logger.info("[PAMIĘĆ] zapisałem %d wiadomości z rozmowy.", len(czyste))


def poprzednia_rozmowa():
    """
    Zwraca końcówkę poprzedniej rozmowy jako gotową listę wiadomości.

    main.py wstawia ją na początek nowej rozmowy, dzięki czemu Jarvis
    podejmuje wątek zamiast zaczynać od zera.
    """
    return list(wczytaj().get("ostatnia_rozmowa") or [])


# --- Podgląd pamięci: `python pamiec.py` ---
if __name__ == "__main__":
    from logging_setup import skonfiguruj_logowanie

    skonfiguruj_logowanie()

    print(f"Pamięć długoterminowa: {SCIEZKA_FAKTOW}")
    print(lista_faktow()[0])

    dane = wczytaj()
    print(f"\nKońcówka rozmowy: {SCIEZKA_PAMIECI}")
    print(f"Ostatni zapis: {dane.get('zapisano') or '(nigdy)'}")
    rozmowa = dane.get("ostatnia_rozmowa") or []
    print(f"KOŃCÓWKA OSTATNIEJ ROZMOWY ({len(rozmowa)} wiadomości):")
    for w in rozmowa:
        kto = "TY    " if w["role"] == "user" else "JARVIS"
        print(f"  {kto}: {w['content'][:100]}")
    if not rozmowa:
        print("  (pusto)")
