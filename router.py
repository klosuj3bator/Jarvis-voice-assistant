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
import os

import anthropic
from dotenv import load_dotenv

load_dotenv()

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

Dozwolone są dokładnie trzy formaty:

1. Odtworzenie muzyki:
{"action": "play_song", "song": "tytuł utworu", "artist": "wykonawca lub null"}

2. Otwarcie aplikacji:
{"action": "open_app", "app_name": "nazwa aplikacji"}

3. Cokolwiek innego, czego nie rozumiesz lub co nie pasuje do powyższych:
{"action": "unknown"}

Zasady:
- Jeśli użytkownik nie podał wykonawcy, ustaw "artist" na null (nie zgaduj wykonawcy).
- Nie tłumacz tytułów piosenek ani nazw aplikacji — przepisz je tak, jak padły.
- Usuń z tytułu słowa komendy ("puść", "włącz", "zagraj", "play", "odtwórz").
- Nazwę aplikacji podaj małymi literami, jednym słowem, jeśli to możliwe.

Przykłady:

Wejście: "puść mi Bohemian Rhapsody Queen"
Wyjście: {"action": "play_song", "song": "Bohemian Rhapsody", "artist": "Queen"}

Wejście: "włącz piosenkę Mury"
Wyjście: {"action": "play_song", "song": "Mury", "artist": null}

Wejście: "play Smells Like Teen Spirit by Nirvana"
Wyjście: {"action": "play_song", "song": "Smells Like Teen Spirit", "artist": "Nirvana"}

Wejście: "otwórz przeglądarkę"
Wyjście: {"action": "open_app", "app_name": "przeglądarka"}

Wejście: "open spotify"
Wyjście: {"action": "open_app", "app_name": "spotify"}

Wejście: "jaka jest dzisiaj pogoda"
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
        print(f"[ROUTER] Błąd Claude API: {e}")
        return {"action": "unknown"}

    # Odpowiedź to lista bloków treści — bierzemy tekst z pierwszego bloku typu "text".
    surowy_tekst = next(
        (blok.text for blok in odpowiedz.content if blok.type == "text"), ""
    )

    wynik = _wyciagnij_json(surowy_tekst)

    if wynik is None:
        print(f"[ROUTER] Model zwrócił coś, co nie jest JSON-em: {surowy_tekst!r}")
        return {"action": "unknown"}

    # Ostatnia linia obrony: jeśli w JSON-ie brakuje "action" albo jest nieznana wartość,
    # traktujemy to jak komendę nierozpoznaną.
    if wynik.get("action") not in ("play_song", "open_app", "unknown"):
        print(f"[ROUTER] Nieznana akcja w odpowiedzi modelu: {wynik}")
        return {"action": "unknown"}

    return wynik


# --- Test samego routera: `python router.py` (bez mikrofonu, na wpisanym tekście) ---
if __name__ == "__main__":
    PRZYKLADY = [
        "puść mi Bohemian Rhapsody Queen",
        "włącz piosenkę Mury",
        "play Smells Like Teen Spirit by Nirvana",
        "otwórz spotify",
        "jaka jest dzisiaj pogoda",
    ]

    for przyklad in PRZYKLADY:
        print(f"\nWejście: {przyklad}")
        print(f"Wyjście: {rozpoznaj_komende(przyklad)}")
