"""
pamiec.py — trwała pamięć Jarvisa.

Do tej pory Jarvis zapominał wszystko po ośmiu sekundach ciszy, a po restarcie
komputera nie wiedział nawet, jak masz na imię. Ten moduł to zmienia.


CO JARVIS PAMIĘTA
=================

Dwie różne rzeczy, bo mają różny okres przydatności:

  FAKTY        — trwałe informacje o Tobie: imię, gust muzyczny, czym się
                 zajmujesz, jak lubisz być traktowany. Zapisuje je sam,
                 w trakcie rozmowy, gdy uzna coś za warte zapamiętania.

  OSTATNIA     — dosłowny zapis końcówki poprzedniej rozmowy. Dzięki temu
  ROZMOWA        możesz wrócić po godzinie i powiedzieć "wróćmy do tego,
                 o czym mówiliśmy", a on wie, o co chodzi.

Wszystko ląduje w zwykłym pliku JSON obok programu. To celowe — możesz go
otworzyć notatnikiem i zobaczyć dokładnie, co Jarvis o Tobie wie, poprawić
to albo skasować. Żadnej magii, żadnej bazy danych.
"""

import json
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

KATALOG = os.path.dirname(os.path.abspath(__file__))
SCIEZKA_PAMIECI = os.path.join(KATALOG, "pamiec.json")

# Ile ostatnich wiadomości z poprzedniej rozmowy odtwarzamy przy starcie.
# Za mało — Jarvis gubi wątek. Za dużo — każde zapytanie niesie ze sobą
# wielką porcję starego tekstu, co kosztuje i czasem myli.
ILE_ODTWARZAMY = 12

# Górny limit zapamiętanych faktów. Bez niego lista rosłaby w nieskończoność,
# a cała trafia do system promptu przy każdym zapytaniu.
MAX_FAKTOW = 60


def _pusta():
    return {"fakty": [], "ostatnia_rozmowa": [], "zapisano": None}


def wczytaj():
    """
    Wczytuje pamięć z dysku.

    Zwraca: słownik z kluczami "fakty", "ostatnia_rozmowa", "zapisano".
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
    """Zapisuje pamięć na dysk. Błąd zapisu logujemy, ale nie przerywamy pracy."""
    dane["zapisano"] = datetime.now().isoformat(timespec="seconds")
    try:
        with open(SCIEZKA_PAMIECI, "w", encoding="utf-8") as plik:
            json.dump(dane, plik, indent=2, ensure_ascii=False)
    except OSError as e:
        logger.error("Nie udało się zapisać pamięci: %s", e)


def zapamietaj_fakt(fakt):
    """
    Dopisuje trwały fakt o użytkowniku. To wywołuje sam Jarvis, jako narzędzie.

    fakt — jedno zdanie, np. "Klaudiusz uczy się programowania w Pythonie"

    Zwraca: komunikat potwierdzający (trafia z powrotem do modelu).
    """
    fakt = (fakt or "").strip()
    if not fakt:
        return "Pusty fakt — nie zapisałem."

    dane = wczytaj()

    # Prosta ochrona przed duplikatami: porównujemy bez wielkości liter.
    # Model bywa powtarzalny i bez tego zapisałby "ma na imię Klaudiusz"
    # przy co drugiej rozmowie.
    if any(f.lower() == fakt.lower() for f in dane["fakty"]):
        return "To już pamiętam."

    dane["fakty"].append(fakt)

    # Gdy lista urośnie ponad limit, kasujemy najstarsze wpisy. Nowsze
    # informacje o człowieku są zwykle aktualniejsze niż te sprzed miesięcy.
    if len(dane["fakty"]) > MAX_FAKTOW:
        dane["fakty"] = dane["fakty"][-MAX_FAKTOW:]

    _zapisz(dane)
    logger.info("[PAMIĘĆ] zapamiętałem: %s", fakt)
    return f"Zapamiętane: {fakt}"


def zapomnij_fakt(fragment):
    """
    Usuwa fakty zawierające podany fragment tekstu. Też jest narzędziem —
    Jarvis może tego użyć, gdy powiesz mu, że coś się zdezaktualizowało.

    Zwraca: komunikat o tym, co zostało usunięte.
    """
    fragment = (fragment or "").strip().lower()
    if not fragment:
        return "Nie wiem, co mam zapomnieć."

    dane = wczytaj()
    zostaje = [f for f in dane["fakty"] if fragment not in f.lower()]
    usuniete = len(dane["fakty"]) - len(zostaje)

    if not usuniete:
        return "Nie znalazłem takiego wpisu w pamięci."

    dane["fakty"] = zostaje
    _zapisz(dane)
    logger.info("[PAMIĘĆ] zapomniałem %d wpis(ów) pasujących do %r", usuniete, fragment)
    return f"Zapomniane ({usuniete})."


def zapisz_rozmowe(historia):
    """
    Zachowuje końcówkę rozmowy, żeby następnym razem dało się do niej wrócić.

    historia — lista wiadomości w formacie API

    Zapisujemy TYLKO zwykłe wypowiedzi (tekst użytkownika i Jarvisa).
    Wywołania narzędzi i ich wyniki pomijamy — po restarcie są bezwartościowe
    (odwołują się do nieistniejących już identyfikatorów), a zajmują mnóstwo
    miejsca i mogą wprawić model w zakłopotanie.

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
                czyste.append({"role": rola, "content": tresc})
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
            czyste.append({"role": "assistant", "content": " ".join(fragmenty)})

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


def kontekst_do_promptu():
    """
    Składa to, co Jarvis pamięta, w kawałek tekstu doklejany do system promptu.

    Zwraca: string (pusty, jeśli pamięć jest pusta).
    """
    dane = wczytaj()
    fakty = dane.get("fakty") or []

    if not fakty:
        return ""

    linie = "\n".join(f"- {f}" for f in fakty)
    return (
        "\n\nCO WIESZ O UŻYTKOWNIKU (z poprzednich rozmów):\n"
        f"{linie}\n"
        "Korzystaj z tego naturalnie, tak jak człowiek pamiętający znajomego. "
        "Nie recytuj tej listy i nie chwal się, że coś pamiętasz — po prostu wiedz."
    )


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

    dane = wczytaj()

    print(f"Plik pamięci: {SCIEZKA_PAMIECI}")
    print(f"Ostatni zapis: {dane.get('zapisano') or '(nigdy)'}")
    print()

    fakty = dane.get("fakty") or []
    print(f"FAKTY ({len(fakty)}):")
    for f in fakty:
        print(f"  - {f}")
    if not fakty:
        print("  (pusto — Jarvis jeszcze Cię nie poznał)")

    rozmowa = dane.get("ostatnia_rozmowa") or []
    print(f"\nKOŃCÓWKA OSTATNIEJ ROZMOWY ({len(rozmowa)} wiadomości):")
    for w in rozmowa:
        kto = "TY    " if w["role"] == "user" else "JARVIS"
        print(f"  {kto}: {w['content'][:100]}")
    if not rozmowa:
        print("  (pusto)")
