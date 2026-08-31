"""
router.py — warstwa "mózgu" Jarvisa.

Bierze surowy tekst z mikrofonu (np. "puść mi Bohemian Rhapsody Queen")
i zamienia go na ustrukturyzowaną decyzję, którą main.py umie wykonać:

    {"action": "play_song", "song": "Bohemian Rhapsody", "artist": "Queen"}

Dlaczego model językowy zamiast zwykłych if-ów na słowach kluczowych?
Bo ludzie mówią na sto sposobów — "puść", "włącz", "zagraj", "play",
"chciałbym posłuchać" — i do tego Whisper czasem przekręca końcówki.
Lista if-ów rozsypałaby się przy pierwszej nieprzewidzianej formie.

Używamy modelu Haiku, bo to zadanie klasyfikacji: krótkie wejście,
krótkie wyjście, liczy się szybkość odpowiedzi (Jarvis ma reagować od razu).
"""

import json
import logging
import os

import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Model wybrany pod kątem szybkości — klasyfikacja komendy to proste zadanie.
MODEL = "claude-haiku-4-5-20251001"

# Odpowiedź to kilkanaście tokenów JSON-a, więc nie ma po co rezerwować więcej.
MAX_TOKENS = 256

# System prompt = stałe instrukcje dla modelu, niezależne od tego, co powiedziałeś.
# Trzy rzeczy są tu ważne:
#   1. żądanie CZYSTEGO JSON-a (bez ```json i bez komentarzy),
#   2. jawne wyliczenie dozwolonych akcji,
#   3. przykłady po polsku i po angielsku — najskuteczniejszy sposób
#      pokazania modelu, czego dokładnie oczekujemy.
SYSTEM_PROMPT = """Jesteś parserem komend dla asystenta głosowego. Otrzymujesz tekst \
rozpoznany z mowy użytkownika (po polsku lub po angielsku) i zamieniasz go na JSON.

Odpowiadasz WYŁĄCZNIE czystym obiektem JSON. Bez bloków kodu, bez ```json, \
bez wyjaśnień, bez tekstu przed ani po.

Dozwolone jest dokładnie pięć formatów:

1. Odtworzenie pojedynczej piosenki:
{"action": "play_song", "song": "tytuł utworu", "artist": "wykonawca lub null"}

2. Odtworzenie całego albumu:
{"action": "play_album", "album": "tytuł albumu", "artist": "wykonawca lub null"}

3. Otwarcie aplikacji:
{"action": "open_app", "app_name": "nazwa aplikacji"}

4. Zamknięcie aplikacji:
{"action": "close_app", "app_name": "nazwa aplikacji"}

5. Rozmowa — pytanie, stwierdzenie, prośba o informację, żart, pogawędka:
{"action": "chat"}

6. Wypowiedź, z której nic nie wynika — sam szum, urwane słowo, bełkot:
{"action": "unknown"}

Zasady:
- Jeśli użytkownik nie podał wykonawcy, ustaw "artist" na null (nie zgaduj wykonawcy).
- Nie tłumacz tytułów piosenek, albumów ani nazw aplikacji — przepisz je tak, jak padły.
- Usuń z tytułu słowa komendy ("puść", "włącz", "zagraj", "play", "odtwórz").
- ALBUM od PIOSENKI odróżniasz po słowie "album" (albo "płyta", "record", "LP").
  Jeśli użytkownik powiedział "album", użyj play_album i usuń to słowo z tytułu.
  Jeśli nie powiedział — użyj play_song, nawet jeśli wiesz, że tytuł to album.
- Nazwę aplikacji podaj małymi literami.
- OTWIERANIE rozpoznajesz po słowach: otwórz, uruchom, włącz, odpal, open, launch, start.
- ZAMYKANIE rozpoznajesz po słowach: zamknij, wyłącz, zakończ, ubij, close, quit, exit, kill.
- Uwaga na słowo "włącz": przy muzyce znaczy odtwarzanie ("włącz piosenkę"),
  a przy aplikacji otwieranie ("włącz Chrome"). "Wyłącz" zawsze znaczy zamykanie.
- Jeśli nie masz pewności, czy chodzi o otwarcie czy zamknięcie, zwróć "unknown".
  Nigdy nie zgaduj między open_app a close_app.
- CHAT to domyślna akcja dla wszystkiego, co nie jest poleceniem do wykonania:
  pytań ("ile to jest...", "kim był..."), pogawędki ("jak się masz"), próśb
  o opinię czy żart, a także zwykłych stwierdzeń. Przy "chat" NIE dodajesz
  żadnych dodatkowych pól — sam tekst weźmiemy z transkrypcji.
- Rozróżnienie chat vs komenda: komenda mówi Jarvisowi COŚ ZROBIĆ z muzyką
  albo aplikacją. Pytanie O muzykę albo aplikację to nadal chat.
  "Włącz Nevermind" to komenda, "kto nagrał Nevermind" to chat.
- "unknown" zostaw wyłącznie dla wypowiedzi bez treści: urwanych słów,
  bełkotu, przypadkowego szumu. Sensowne zdanie, którego nie umiesz
  zaklasyfikować jako komendy, jest rozmową — nie "unknown".

Przykłady:

Wejście: "puść mi Bohemian Rhapsody Queen"
Wyjście: {"action": "play_song", "song": "Bohemian Rhapsody", "artist": "Queen"}

Wejście: "włącz piosenkę Mury"
Wyjście: {"action": "play_song", "song": "Mury", "artist": null}

Wejście: "play Smells Like Teen Spirit by Nirvana"
Wyjście: {"action": "play_song", "song": "Smells Like Teen Spirit", "artist": "Nirvana"}

Wejście: "puść album Dark Side of the Moon"
Wyjście: {"action": "play_album", "album": "Dark Side of the Moon", "artist": null}

Wejście: "włącz album Abbey Road Beatlesów"
Wyjście: {"action": "play_album", "album": "Abbey Road", "artist": "The Beatles"}

Wejście: "play the album Nevermind by Nirvana"
Wyjście: {"action": "play_album", "album": "Nevermind", "artist": "Nirvana"}

Wejście: "zagraj płytę Kolysanki"
Wyjście: {"action": "play_album", "album": "Kolysanki", "artist": null}

Wejście: "otwórz przeglądarkę"
Wyjście: {"action": "open_app", "app_name": "przeglądarka"}

Wejście: "open spotify"
Wyjście: {"action": "open_app", "app_name": "spotify"}

Wejście: "wyłącz League of Legends"
Wyjście: {"action": "close_app", "app_name": "league of legends"}

Wejście: "zamknij chrome"
Wyjście: {"action": "close_app", "app_name": "chrome"}

Wejście: "zakończ discorda"
Wyjście: {"action": "close_app", "app_name": "discord"}

Wejście: "close notepad"
Wyjście: {"action": "close_app", "app_name": "notepad"}

Wejście: "jaka jest dzisiaj pogoda"
Wyjście: {"action": "chat"}

Wejście: "kto nagrał album Nevermind"
Wyjście: {"action": "chat"}

Wejście: "opowiedz mi jakiś żart"
Wyjście: {"action": "chat"}

Wejście: "dzięki, super"
Wyjście: {"action": "chat"}

Wejście: "yyy no więc eee"
Wyjście: {"action": "unknown"}"""


def _utworz_klienta():
    """
    Tworzy klienta Claude API.

    Klucz czytany jest z .env (ANTHROPIC_API_KEY) — biblioteka anthropic
    znajduje go sama w zmiennych środowiskowych, które ustawił load_dotenv().

    Zwraca: obiekt anthropic.Anthropic.
    """
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "Brak ANTHROPIC_API_KEY. Dopisz go do pliku .env obok tego skryptu "
            "(klucz wygenerujesz na console.anthropic.com)."
        )

    return anthropic.Anthropic()


# Klienta tworzymy raz i trzymamy tutaj — nie ma sensu budować go przy każdej komendzie.
_klient = None


def _wyciagnij_json(odpowiedz):
    """
    Wyciąga słownik z tekstu odpowiedzi modelu.

    Mimo instrukcji w system prompcie model potrafi czasem opakować JSON
    w blok ```json ... ```. Zdejmujemy taką otoczkę, zanim spróbujemy parsować
    — to tańsze niż odrzucenie skądinąd poprawnej odpowiedzi.

    Zwraca: słownik albo None, jeśli to nie jest poprawny JSON.
    """
    tekst = odpowiedz.strip()

    # Zdejmujemy ewentualne ogrodzenie blokiem kodu.
    if tekst.startswith("```"):
        linie = tekst.split("\n")
        linie = [l for l in linie if not l.strip().startswith("```")]
        tekst = "\n".join(linie).strip()

    try:
        wynik = json.loads(tekst)
    except json.JSONDecodeError:
        return None

    # Model mógłby teoretycznie zwrócić poprawny JSON, ale np. listę zamiast obiektu.
    if not isinstance(wynik, dict):
        return None

    return wynik


def rozpoznaj_komende(tekst):
    """
    GŁÓWNE WEJŚCIE TEGO MODUŁU — to woła main.py.

    tekst — surowa transkrypcja z mikrofonu

    Zwraca: słownik z kluczem "action" ("play_song", "open_app" albo "unknown").
    Przy każdym problemie (pusty tekst, błąd sieci, niepoprawny JSON)
    zwraca {"action": "unknown"} — main.py ma wtedy jedną prostą ścieżkę obsługi
    i nigdy nie dostanie czegoś, czego się nie spodziewa.
    """
    global _klient

    if not tekst or not tekst.strip():
        return {"action": "unknown"}

    if _klient is None:
        _klient = _utworz_klienta()

    try:
        odpowiedz = _klient.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": tekst}],
        )
    except anthropic.APIError as e:
        # Brak internetu, zły klucz, limit zapytań — Jarvis ma się nie wysypać,
        # tylko powiedzieć, że nie zrozumiał, i wrócić do nasłuchu.
        # logger.exception zapisuje PEŁNY ślad wyjątku, nie samą jego treść.
        # Przy diagnozie z dziennika to różnica między "wiem co", a "wiem gdzie".
        logger.exception("Błąd Claude API")
        return {"action": "unknown"}

    # Odpowiedź to lista bloków treści — bierzemy tekst z pierwszego bloku typu "text".
    surowy_tekst = next(
        (blok.text for blok in odpowiedz.content if blok.type == "text"), ""
    )

    wynik = _wyciagnij_json(surowy_tekst)

    if wynik is None:
        logger.warning("Model zwrócił coś, co nie jest JSON-em: %r", surowy_tekst)
        return {"action": "unknown"}

    # Ostatnia linia obrony: jeśli w JSON-ie brakuje "action" albo jest nieznana wartość,
    # traktujemy to jak komendę nierozpoznaną.
    if wynik.get("action") not in (
        "play_song", "play_album", "open_app", "close_app", "chat", "unknown"
    ):
        logger.warning("Nieznana akcja w odpowiedzi modelu: %s", wynik)
        return {"action": "unknown"}

    return wynik


# --- Test samego routera: `python router.py` (bez mikrofonu, na wpisanym tekście) ---
if __name__ == "__main__":
    from logging_setup import skonfiguruj_logowanie

    skonfiguruj_logowanie()

    PRZYKLADY = [
        "puść mi Bohemian Rhapsody Queen",
        "włącz piosenkę Mury",
        "play Smells Like Teen Spirit by Nirvana",
        "puść album Dark Side of the Moon",
        "włącz album Abbey Road Beatlesów",
        "zagraj mi Wish You Were Here",
        "otwórz spotify",
        "wyłącz League of Legends",
        "zamknij chrome",
        "zakończ discorda",
        "close notepad",
        "jaka jest dzisiaj pogoda",
        "kto nagrał album Nevermind",
        "opowiedz mi jakiś żart",
        "dzięki, super robota",
        "yyy no więc eee",
    ]

    for przyklad in PRZYKLADY:
        logger.info("Wejście: %s", przyklad)
        logger.info("Wyjście: %s", rozpoznaj_komende(przyklad))
