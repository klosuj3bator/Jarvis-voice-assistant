# Mapa zadań Jarvisa

Kolejka zadań dla trybu roadmapy (`roadmapa.py`). Jarvis bierze je po kolei
i proponuje na Telegramie; po Twoim **Akceptuj** robi je przez Claude Code na
gałęzi `roadmap`, oznacza tu jako zrobione (`## [x]`), a do `main` trafiają
dopiero po `/scal`.

Jak dopisać zadanie — nagłówek `## ` w odpowiedniej części i opcjonalne pola:

```
## Tytuł zadania
- Trudność: łatwe            (albo: trudne; bez oznaczenia — jak trudne)
- Dotyczy: którego modułu albo funkcji dotyczy
- Zmieni: co się zmieni dla Ciebie
- Potrzebne: (tylko Część B) czego Jarvis potrzebuje od Ciebie

Dowolny opis — trafia do Claude Code jako treść zadania. Im konkretniej,
tym lepiej: czego oczekujesz, czego nie ruszać, jak sprawdzić efekt.
```

- **Część A** — Jarvis proponuje zadanie i robi je po Twoim „Akceptuj”.
- **Część B** — zadania, które potrzebują czegoś od Ciebie (decyzji, konta,
  pliku, klucza). Jarvis ich nie zaczyna sam: najpierw pisze na Telegramie,
  czego potrzebuje (z pola „Potrzebne:” albo sprawdza to w kodzie, tylko
  czytając), i czeka. Odpowiadasz odpowiedzią na tę wiadomość, a zadanie
  rusza dopiero po Twoim „Zaczynaj”. Klucze i hasła — w Ustawieniach, nie w czacie.

Kolejność w pliku = kolejność propozycji. Zmiany w tym pliku Jarvis zobaczy
po commicie na `main`.

# Część A

## Testy dla briefing.py
- Trudność: łatwe
- Dotyczy: briefing.py — poranny przegląd dnia („co dziś?”)
- Zmieni: błędy w briefingu wyłapią testy, zanim usłyszysz je rano

Dopisz tests/test_briefing.py: przypomnienia na dziś, wydarzenia z kalendarza
i pogoda podawane przez atrapy (bez sieci i bez Twoich plików — zobacz
tests/conftest.py i tests/atrapy.py). Sprawdź też dzień bez żadnych wydarzeń.

## Przecinek dziesiętny w Ustawieniach
- Trudność: łatwe
- Dotyczy: setup_wizard.py — zakładka Nasłuch, próg czułości
- Zmieni: próg wyświetla się po polsku („0,20” zamiast „0.20”)

Ustaw polskie formatowanie liczb w polach liczbowych okna Ustawień. Zapis do
config.env ma dalej używać kropki (konfiguracja.prog_wake_worda czyta oba).

## Koszt rozmów z Claude w raporcie dnia
- Trudność: trudne
- Dotyczy: agent.py (liczenie kosztu), briefing.py
- Zmieni: „ile dziś wydałem na Jarvisa?” — i dzienna suma w porannym przeglądzie

agent.py liczy koszt każdej wypowiedzi tylko w pamięci (_koszt_od_startu)
i gubi go po restarcie. Zapisuj sumy dzienne w pliku w folderze danych
(sciezki.dane, wpis w .gitignore i w instalator/sprawdz_paczke.py), dodaj
narzędzie agenta do pytania o koszt i wspomnij wczorajszą sumę w briefingu.
Dopisz testy.

# Część B

Tu dopisz zadania, przy których Jarvis ma najpierw zapytać Cię, czego potrzebuje.
