# Mapa zadań Jarvisa

Kolejność wykonywania zadań. Najpierw wszystko, co Claude Code zrobi samodzielnie,
na końcu zadania wymagające udziału Maćka (konta, klucze, nagrania, decyzje).

Oznaczenia: [Ł] łatwe · [Ś] średnie · [T] trudne
Numer w nawiasie na końcu = numer pomysłu z `POMYSLY_JARVIS.md`.

---

## CZĘŚĆ A — zadania do wykonania samodzielnie przez Claude Code

### Etap 1. Fundament i bezpieczeństwo pracy

1. [Ł] Pakiet diagnostyczny: logi, wersje i konfiguracja (bez kluczy) w jednym ZIP. (79)
2. [Ł] Sprawdzanie przed commitem, czy w repo nie ma kluczy (pre-commit). (78)
3. [Ł] Kopia zapasowa danych Jarvisa (bez kluczy) do wybranego folderu, codziennie. (74)
4. [Ł] Licznik kosztów API z dziennym limitem i ostrzeżeniem. (82)
5. [Ś] Log audytowy wszystkich akcji zmieniających coś na komputerze. (73)
6. [Ł] Sprawdzanie przy starcie, czy wszystkie klucze działają, z raportem w panelu. (85)
7. [Ł] Automatyczne sprawdzanie aktualizacji Jarvisa z GitHuba. (89)

### Etap 2. Codzienna wygoda — Telegram

8. [Ł] Komenda /pomoc z listą wszystkich komend i przykładów. (41)
9. [Ł] Szybkie przyciski w Telegramie (status PC, briefing, notatka, zrzut ekranu). (43)
10. [Ł] Raport tygodniowy: notatki, przypomnienia, maile, zadania Claude Code, koszty API. (46)
11. [Ł] Wysyłanie plików z PC na telefon ("wyślij mi listę zakupów"). (42)
12. [Ł] Ciche godziny dla powiadomień Telegrama. (48)
13. [Ś] Kolejka zadań /kod z podglądem statusu i anulowaniem. (49)
14. [Ś] Codzienny raport z działania Jarvisa: błędy, restarty, wynik testów. (50)

### Etap 3. Rozmowa i głos

15. [Ł] Dźwięk potwierdzenia (krótki "bip") po wykryciu słowa aktywującego. (5)
16. [Ł] "Powtórz" — Jarvis powtarza ostatnią odpowiedź bez nowego zapytania do API. (9)
17. [Ł] Krótsze odpowiedzi na "krócej", dłuższe na "rozwiń" — zapamiętywane na sesję. (3)
18. [Ś] Pytania doprecyzowujące przy niejasnych poleceniach zamiast zgadywania. (6)
19. [Ś] Wykrywanie końca wypowiedzi po ciszy zamiast stałego czasu nagrania. (4)
20. [Ś] Automatyczne wykrywanie języka odpowiedzi (polski/angielski). (2)
21. [Ś] Szept/tryb nocny — ciszej i krócej po 22:00. (8)
22. [Ś] Osobowości do wyboru (rzeczowy, żartobliwy, formalny) w Ustawieniach. (10)
23. [Ś] Wybór głosu i tempa mówienia w Ustawieniach, z odsłuchem próbki. (1)

### Etap 4. Pamięć i notatki

24. [Ł] "Co pamiętasz o X?" — przeszukiwanie pamięci po temacie. (11)
25. [Ł] Tagi w notatkach dodawane głosem ("zanotuj z tagiem praca"). (20)
26. [Ł] Cytowanie źródła przy odpowiedziach z notatek (nazwa pliku i fragment). (15)
27. [Ł] Eksport całej pamięci do czytelnego pliku na komendę. (17)
28. [Ł] Szablony notatek (lista zakupów, spotkanie, pomysł). (26)
29. [Ś] Automatyczne propozycje zapamiętania ("Zapamiętać, że lubisz rock?"). (12)
30. [Ś] Wygasanie nieaktualnych wspomnień — Jarvis pyta, czy dalej są prawdziwe. (16)
31. [Ś] Podsumowanie dnia z notatek i rozmów zapisywane wieczorem. (13)
32. [Ś] Indeksowanie PDF-ów i dokumentów Word w RAG. (14)
33. [Ś] "Co robiłem w zeszły wtorek?" — oś czasu z notatek, kalendarza i akcji. (19)

### Etap 5. Produktywność

34. [Ł] Lista zadań (todo) z priorytetami, osobna od notatek. (22)
35. [Ł] Liczenie, przeliczanie walut i jednostek głosem bez zapytania do API. (29)
36. [Ś] Pomodoro z przerwami i statystyką dnia. (23)
37. [Ś] Śledzenie czasu pracy nad projektami. (24)
38. [Ś] Śledzenie aplikacji o pracę: firma, data, status, przypomnienie o follow-upie. (30)
39. [Ś] Makra głosowe: "tryb pracy" otwiera aplikacje, puszcza playlistę, wycisza powiadomienia. (21)
40. [Ś] Tryb skupienia: wyciszenie powiadomień, blokada stron i aplikacji na czas. (25)
41. [Ś] Planowanie dnia: Jarvis rozkłada zadania w wolnych miejscach kalendarza. (28)
42. [Ś] Szkice odpowiedzi na maile do mojej akceptacji (bez wysyłania samodzielnie). (27)

### Etap 6. Komputer i system

43. [Ł] Zrzut ekranu zapisany do wybranego folderu na komendę. (35)
44. [Ł] Przełączanie urządzenia audio (słuchawki/głośniki) głosem. (33)
45. [Ł] Czyszczenie folderu Pobrane: propozycja uporządkowania plików (z potwierdzeniem). (31)
46. [Ś] Alerty: mało miejsca na dysku, wysoka temperatura GPU, duże zużycie RAM. (32)
47. [Ś] Zarządzanie oknami: "ułóż Chrome po lewej, terminal po prawej". (34)
48. [Ś] Nagrywanie ekranu na komendę. (36)
49. [Ś] Tryb gry: wstrzymanie animacji, zamknięcie zbędnych programów, przywrócenie po grze. (39)
50. [Ś] Sprawdzanie aktualizacji Windowsa i sterowników z raportem. (37)
51. [T] Automatyczne wykrywanie i opisywanie przyczyny zawieszania się programów. (40)

### Etap 7. Wydajność, koszty, niezawodność

52. [Ł] Pamięć podręczna odpowiedzi na powtarzalne pytania (pogoda, data). (86)
53. [Ś] Automatyczne ponawianie zapytań przy chwilowych błędach sieci. (84)
54. [Ś] Szybszy start: ładowanie modeli w tle, wczesna gotowość wake worda. (80)
55. [Ś] Wybór modelu zależnie od zadania: tańszy do komend, mocniejszy do rozmowy. (81)
56. [Ś] Monitor wydajności Jarvisa: czas reakcji każdego etapu (STT, agent, TTS). (87)
57. [Ś] Tryb offline: podstawowe komendy działają bez internetu. (83)
58. [Ś] Testy integracyjne całego przepływu z nagranym audio. (90)
59. [T] Rozpoznawanie mowy na GPU z automatycznym przełączeniem na CPU przy problemach. (88)

### Etap 8. Bezpieczeństwo i prywatność

60. [Ł] Tryb prywatny: Jarvis nie zapisuje rozmowy ani notatek do odwołania. (72)
61. [Ł] Szyfrowanie `memory.json`, notatek i przypomnień na dysku. (71)
62. [Ł] Blokada wrażliwych komend PIN-em przy użyciu przez Telegram. (76)
63. [Ś] Automatyczne czyszczenie zrzutów ekranu i nagrań po wysłaniu. (77)
64. [Ś] Wykrywanie podejrzanych maili (phishing) i ostrzeżenie na Telegramie. (75)

### Etap 9. Internet i informacje

65. [Ł] Pogoda na kilka dni i ostrzeżenia (deszcz, przymrozek) w briefingu. (51)
66. [Ł] Streszczanie strony z linku wysłanego na Telegram. (55)
67. [Ł] Kursy walut i kryptowalut w briefingu (tylko informacyjnie). (57)
68. [Ś] Codzienny przegląd wiadomości z wybranych tematów (AI, gry). (53)
69. [Ś] Monitor ofert pracy przeniesiony z n8n do Jarvisa, z filtrami. (54)
70. [Ś] Śledzenie cen produktów na wybranych stronach z alertem o obniżce. (52)
71. [Ś] Śledzenie przesyłek po numerze. (56)
72. [Ś] Aktualizacje gier, w które gram (patch notes w skrócie). (58)
73. [Ś] Tłumaczenie na żywo: mówię po polsku, Jarvis czyta po angielsku i odwrotnie. (59)
74. [T] Asystent zakupowy: porównanie kilku produktów w tabeli. (60)

### Etap 10. Rozrywka i multimedia

75. [Ł] Dodawanie aktualnego utworu do ulubionych głosem. (63)
76. [Ł] "Co to za piosenka?" przez metadane aktualnie granego utworu. (66)
77. [Ł] Losowanie i decyzje ("rzuć monetą", "wybierz film z listy"). (69)
78. [Ł] "Puść coś pod mój nastrój" — playlisty Spotify wg nastroju lub pory dnia. (61)
79. [Ś] Rekomendacje muzyki na podstawie historii słuchania. (62)
80. [Ś] Quizy i zagadki głosowe. (64)
81. [Ś] Sterowanie YouTube w przeglądarce (pauza, następny film). (65)

### Etap 11. Przygotowanie pod produkt

82. [Ł] Tryb demo z fikcyjnymi danymi do nagrań i prezentacji. (93)
83. [Ś] System wtyczek: nowe narzędzia jako osobne pliki, bez zmian w rdzeniu. (92)
84. [Ł] Pełna wersja angielska interfejsu i odpowiedzi. (97)
85. [Ś] Dokumentacja dla użytkowników nietechnicznych z obrazkami. (98)

---

## CZĘŚĆ B — zadania wymagające mojego udziału

Jarvis pyta mnie, zanim je zacznie, i mówi dokładnie, czego potrzebuje ode mnie.

### Etap 12. Potrzebne konto, klucz lub konfiguracja

86. [Ś] Statystyki gier (League of Legends) — wymagany klucz Riot API. (67)
87. [Ś] Uruchamianie gier ze Steama i Riot Clienta po nazwie — potrzebne ścieżki ode mnie. (38)
88. [Ś] Zdalne uśpienie i wybudzanie komputera (Wake-on-LAN) — konfiguracja BIOS/sieci. (45)
89. [Ś] Udostępnianie lokalizacji z telefonu do przypomnień — konfiguracja na telefonie. (47)
90. [Ś] Wspólna playlista ze znajomymi przez Spotify — uprawnienia i zaproszenia. (68)
91. [Ś] Wysyłanie zdjęć do Jarvisa: opis, odczyt tekstu, zapis do notatek — moje testy. (44)

### Etap 13. Wymagane moje nagrania lub decyzje

92. [T] Rozpoznawanie mojego głosu (weryfikacja mówcy) — potrzebne próbki głosu. (7)
93. [T] Graf powiązań między notatkami (osoby, projekty, terminy) — moje decyzje o strukturze. (18)
94. [T] Generowanie krótkich podsumowań wideo do montażu — moje materiały. (70)

### Etap 14. Produkt i zarabianie

95. [Ś] Nowa nazwa i logo pod wersję komercyjną — moja decyzja. (91)
96. [Ś] Strona produktu z opisem, filmem i instrukcją instalacji — potrzebne demo i hosting. (94)
97. [Ś] Wersja płatna z instalatorem, darmowa na GitHubie — moja decyzja o zakresie. (95)
98. [Ś] Licencje i aktywacja wersji płatnej — konto płatności. (96)
99. [Ś] Anonimowe zgłaszanie błędów od użytkowników — potrzebny serwer/usługa. (99)
100. [T] Wersja na Maca lub Linuksa — potrzebny sprzęt do testów. (100)
