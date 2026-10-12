"""
akcje.py — krótki dziennik ostatnich akcji Jarvisa, pokazywany na HUD-zie
(panel "OSTATNIE AKCJE", zwłaszcza w trybie tapety).

Zapisują tu: agent (każde udane narzędzie i szukanie w internecie),
przypomnienia i czujki poczty. Odczytuje gui.py — dostaje każdą zmianę
przez funkcję zarejestrowaną w sluchaj().


CO WOLNO POKAZAĆ
================

Tapeta jest widoczna dla każdego, kto przejdzie obok biurka. Dlatego
dziennik mówi, CO Jarvis zrobił, ale nie zdradza treści:

  - tak: tytuł włączonej muzyki, nazwa otwartego programu, domena strony,
  - nie: treść notatki, przypomnienia, maila, schowka ani zapamiętanego
    faktu, a także frazy wpisane w wyszukiwarkę — zamiast nich ogólny opis
    ("Nowa notatka", "Sprawdziłem pocztę").

Dziennik żyje tylko w pamięci — po restarcie Jarvisa zaczyna od zera
i nigdy nie trafia na dysk.
"""

import collections
import datetime
import logging
import threading
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Ile ostatnich akcji trzymamy (i pokazuje HUD).
MAKS_AKCJI = 6

# Dłuższe nazwy (tytuł albumu z dopiskiem "Deluxe Edition") ucinamy,
# żeby wiersz na HUD-zie nie wychodził poza ekran.
MAKS_ZNAKOW = 48

_ostatnie = collections.deque(maxlen=MAKS_AKCJI)
_sluchacze = []
_blokada = threading.Lock()


def _krotko(tekst):
    tekst = " ".join(str(tekst or "").split())
    return tekst if len(tekst) <= MAKS_ZNAKOW else tekst[:MAKS_ZNAKOW - 1] + "…"


def _z_wykonawca(tytul, wykonawca):
    return f"{tytul} — {wykonawca}" if wykonawca else tytul


def _domena(adres):
    adres = str(adres or "")
    return urlparse(adres if "//" in adres else "https://" + adres).netloc or adres


# Narzędzie agenta -> opis akcji na HUD-zie (patrz CO WOLNO POKAZAĆ).
# Funkcja dostaje parametry wywołania; zwykły napis = opis bez parametrów.
OPISY = {
    "zagraj_piosenke": lambda w: "Muzyka: " + _z_wykonawca(w.get("tytul"), w.get("wykonawca")),
    "zagraj_album": lambda w: "Album: " + _z_wykonawca(w.get("tytul"), w.get("wykonawca")),
    "sterowanie_spotify": lambda w: f"Spotify: {w.get('polecenie', '')}",
    "otworz_aplikacje": lambda w: f"Otworzyłem: {w.get('nazwa', '')}",
    "zamknij_aplikacje": lambda w: f"Zamknąłem: {w.get('nazwa', '')}",
    "otworz_strone": lambda w: (f"Strona: {_domena(w['adres'])}" if w.get("adres")
                                else "Wyszukiwanie w przeglądarce"),
    "zanotuj": "Nowa notatka",
    "przeczytaj_notatki": "Przeczytałem notatki",
    "search_notes": "Przeszukałem notatki",       # bez pytania — mogłoby zdradzić treść
    "search_emails": lambda w: "Sprawdziłem pocztę" + (f" ({w['konto']})" if w.get("konto") else ""),
    "add_email_watch": "Nowa czujka poczty",
    "list_email_watches": "Czujki poczty",
    "cancel_email_watch": "Usunąłem czujkę poczty",
    "read_clipboard": "Przeczytałem schowek",
    "write_clipboard": "Zapisałem do schowka",
    "system_status": "Stan komputera",
    "running_processes": "Działające programy",
    "downloads_status": "Pobierania",
    "analyze_screen": "Przeczytałem ekran",
    "screenshot": "Zrzut ekranu na Telegram",
    "morning_briefing": "Poranny briefing",
    "powitanie_po_powrocie": "Powitanie po powrocie",
    "list_events": lambda w: f"Kalendarz: {w.get('dzien') or 'dziś'}",
    "ustaw_przypomnienie": lambda w: (f"Przypomnienie na {w['o_godzinie']}" if w.get("o_godzinie")
                                      else f"Przypomnienie za {w.get('za_minut')} min"
                                      if w.get("za_minut") else "Nowe przypomnienie"),
    "lista_przypomnien": "Lista przypomnień",
    "anuluj_przypomnienie": "Anulowałem przypomnienie",
    "sterowanie_systemem": lambda w: "System: " + " ".join(
        str(x) for x in (w.get("polecenie"), w.get("wartosc")) if x not in (None, "")),
    "remember_fact": "Zapamiętałem nowy fakt",
    "forget_fact": "Usunąłem fakt z pamięci",
    "add_vocabulary_word": lambda w: f"Nowe słowo: {w.get('slowo', '')}",
    "list_memories": "Przegląd pamięci",
    # zakoncz_rozmowe celowo pomijamy — to nie akcja, tylko koniec rozmowy.
}


def zapisz(opis):
    """Dopisuje akcję (z godziną) i powiadamia HUD. Bezpieczne z każdego wątku."""
    wpis = {"czas": datetime.datetime.now().strftime("%H:%M"), "tekst": _krotko(opis)}
    with _blokada:
        _ostatnie.append(wpis)
        lista = _lista()
        sluchacze = list(_sluchacze)
    for sluchacz in sluchacze:
        try:
            sluchacz(lista)
        except Exception:
            logger.exception("Słuchacz dziennika akcji zawiódł")


def narzedzie(nazwa, wejscie, kanal="glos"):
    """Wpis dla udanego narzędzia agenta — opis według OPISY (albo nic)."""
    opis = OPISY.get(nazwa)
    if opis is None:
        return
    try:
        tekst = opis(wejscie or {}) if callable(opis) else opis
    except Exception:
        logger.debug("Nie udało się opisać akcji %s", nazwa, exc_info=True)
        return
    zapisz(("📱 " if kanal == "telegram" else "") + tekst)


def _lista():
    """Akcje od najnowszej. Woła się pod blokadą — z zewnątrz służy ostatnie()."""
    return list(reversed(_ostatnie))


def ostatnie():
    """Ostatnie akcje, od najnowszej: [{"czas": "12:03", "tekst": "..."}]."""
    with _blokada:
        return _lista()


def sluchaj(funkcja):
    """Rejestruje funkcję(lista akcji), wołaną po każdej nowej akcji."""
    with _blokada:
        _sluchacze.append(funkcja)
