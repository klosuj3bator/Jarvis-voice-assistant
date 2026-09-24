"""
agent.py — "mózg" Jarvisa. Zastępuje dawny podział na router.py i chat.py.


CO SIĘ ZMIENIŁO I DLACZEGO
==========================

Wcześniej działało to tak: router (Haiku) klasyfikował każde zdanie do jednej
z akcji, main.py ją wykonywał, a rozmowa była osobnym światem obsługiwanym
przez chat.py. Ten podział miał trzy wady, które psuły wrażenie rozmowy:

  1. JEDNA AKCJA NA ZDANIE. "Puść Nirvanę i otwórz Chrome" było nie do
     wykonania — router musiał wybrać jedno.

  2. KOMENDY NIE WCHODZIŁY DO HISTORII. Mówiłeś "puść album Nevermind",
     a potem "kto to nagrał?" — i Jarvis nie miał pojęcia, o czym mowa,
     bo czat nigdy nie zobaczył tamtej komendy.

  3. BRAK RATUNKU PO BŁĘDZIE. Gdy Spotify odmówiło, dostawałeś komunikat
     i koniec. Nikt nie próbował inaczej.

Teraz jest jeden agent. Spotify, aplikacje i pamięć to NARZĘDZIA, po które
model sięga sam, w trakcie normalnej rozmowy. Wszystko — komendy i pogawędka —
płynie jedną historią.

Efekt uboczny, który jest właściwie sednem: skoro nie ma klasyfikacji,
nie ma też przypadków "nie zrozumiałem komendy". Każde zdanie jest po prostu
kolejną wypowiedzią w rozmowie.


JAK DZIAŁA PĘTLA AGENTOWA
=========================

Model dostaje listę narzędzi i sam decyduje, czy któregoś użyć:

    Ty: "puść Nevermind"
      -> model wywołuje narzędzie zagraj_album("Nevermind")
      -> my je wykonujemy i oddajemy wynik
      -> model widzi wynik i mówi "Włączam Nevermind"

Gdy narzędzie zawiedzie, model widzi błąd i może spróbować inaczej —
na przykład otworzyć Spotify i powtórzyć próbę. Tego stara architektura
nie potrafiła w ogóle.

Pętlę piszemy ręcznie (zamiast używać gotowego tool_runnera z SDK), bo
runner nie strumieniuje tekstu, a my mamy zbudowane mówienie zdanie po zdaniu.
Utrata strumienia oznaczałaby powrót do czekania w ciszy na całą odpowiedź.
"""

import json
import logging
import os
import re

import anthropic
from dotenv import load_dotenv

import app_launcher
import pamiec
import przegladarka
import spotify_controller
import system_control

load_dotenv()

logger = logging.getLogger(__name__)

# Sonnet 5 — 2,5 raza tańszy od Opusa za każdy token ($2/$10 zamiast $5/$25
# za milion tokenów wejścia/wyjścia). Jarvis wybiera narzędzia i mówi krótkie
# zdania — do tego Sonnet w zupełności wystarcza. Opus błyszczy przy długim,
# trudnym rozumowaniu, a tego w rozmowie głosowej prawie nie ma.
# Chcesz wrócić? Wpisz "claude-opus-5".
MODEL = "claude-sonnet-5"

MAX_TOKENS = 2048

# Niski wysiłek to świadomy wybór pod rozmowę głosową. Wyższy daje lepsze
# wyniki przy trudnych problemach, ale kosztuje sekundy — a przy mówieniu
# na głos każda sekunda ciszy jest słyszalna. Podnieś na "medium", jeśli
# uznasz, że Jarvis bywa zbyt powierzchowny.
WYSILEK = "low"

# PROMPT CACHING — największa oszczędność w całym Jarvisie.
#
# Model niczego nie pamięta między zapytaniami, więc przy KAŻDYM wysyłamy
# od nowa instrukcje, opisy narzędzi i całą dotychczasową rozmowę. To tysiące
# tokenów, które za każdym razem są identyczne. Cache pozwala serwerowi
# zapamiętać ten początek na 5 minut: kolejne zapytanie, które zaczyna się
# tak samo, płaci za tę część tylko 10% ceny.
#
# Haczyk: pierwsze zapytanie płaci za zapis do cache 25% więcej. Opłaca się
# więc wtedy, gdy zapytania idą seriami — a u Jarvisa idą: rozmowa to kilka
# zdań pod rząd, a każde polecenie z narzędziem to dwa zapytania.
CACHE_PROMPTU = True

# Ile razy model może po kolei sięgnąć po narzędzia w jednej wypowiedzi.
# Bezpiecznik przed zapętleniem się (np. w kółko sprawdza urządzenia Spotify).
MAX_TUR_NARZEDZI = 6

# Wyszukiwarka internetowa. Bloki z wynikami obsługuje serwer Anthropica —
# my tylko deklarujemy, że model MOŻE jej użyć.
#
# Świadomie STARSZA wersja (20250305), nie nowsza 20260209. Nowsza sama
# filtruje wyniki, ale ciągnie za sobą narzędzie do wykonywania kodu, a jego
# opis dokłada ok. 3 tysięcy tokenów do KAŻDEGO zapytania — także "puść
# muzykę", gdzie nikt niczego nie szuka. Zmierzone na tej samej rozmowie:
# pierwsze polecenie 1,9 zamiast 2,8 centa, a odpowiedź o pogodę równie dobra.
NARZEDZIE_WYSZUKIWANIA = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": 2,
}

KOMUNIKAT_WYSZUKIWANIA = "Dobra, sprawdzam, szefie."

# Zapowiedzi na czas działania narzędzi, które trwają zauważalnie długo.
# Wyszukiwanie w sieci to kilkanaście sekund, otwieranie Spotify do 15 —
# bez słowa z głośników wygląda to na zawieszenie.
ZAPOWIEDZI = {
    "zagraj_piosenke": "Już szukam.",
    "zagraj_album": "Już szukam.",
}


SYSTEM_PROMPT = """Jesteś Jarvisem — osobistym asystentem głosowym. Rozmawiasz \
z jednym człowiekiem, na głos, i znasz go z poprzednich rozmów.

KIM JESTEŚ
Pomocny, konkretny, z nutą suchego humoru. Nie jesteś infolinią ani chatbotem — \
jesteś kimś, kto zna tego człowieka i z kim rozmawia się swobodnie. Możesz mieć \
zdanie, możesz zażartować, możesz powiedzieć "nie wiem".

JAK MÓWISZ
To będzie odczytane przez syntezator mowy, więc:
- Domyślnie 1-3 krótkie zdania. Dłużej tylko wtedy, gdy ktoś wyraźnie poprosi.
- Żadnego formatowania: bez list punktowanych, gwiazdek, emoji, linków, kodu.
- Liczby i daty zapisuj słowami tam, gdzie brzmi to naturalnie.
- Nie zaczynaj odpowiedzi od powtarzania pytania.
- Mówisz po polsku, chyba że ktoś odezwie się po angielsku.

CO POTRAFISZ
Masz narzędzia do muzyki na Spotify, uruchamiania i zamykania programów, \
otwierania stron w przeglądarce Opera GX, sterowania komputerem (głośność, \
jasność, blokada, uśpienie, restart, wyłączenie), wyszukiwania w internecie \
oraz do zapamiętywania rzeczy o użytkowniku. Używasz ich sam, gdy rozmowa tego \
wymaga — nie pytaj o pozwolenie przy zwykłych prośbach, po prostu zrób.

Wyszukiwanie w internecie a otwieranie strony to dwie różne rzeczy. Gdy \
użytkownik pyta o coś ("jaka jest pogoda"), sam szukasz i odpowiadasz. \
Gdy chce coś ZOBACZYĆ ("otwórz", "włącz stronę", "pokaż", "wpisz w operze"), \
otwierasz to w Operze i mówisz krótko, co otworzyłeś — bez czytania adresu.

Gdy narzędzie zawiedzie, nie poprzestawaj na komunikacie błędu. Zastanów się, \
czy da się inaczej: inny tytuł, otwarcie Spotify, inna nazwa aplikacji. \
Dopiero gdy naprawdę nie ma wyjścia, powiedz o tym krótko i po ludzku.

WYSZUKIWANIE
Używaj go OSZCZĘDNIE — tylko do rzeczy zmiennych w czasie: pogoda, kursy, \
wyniki, bieżące wydarzenia, premiery. NIE szukaj tego, co już wiesz: stolic, \
faktów historycznych, definicji, ogólnej wiedzy o muzyce. Gdy szukasz, zrób \
JEDNO precyzyjne zapytanie i od razu odpowiedz. Nie czytaj adresów stron \
ani nazw serwisów.

ROZMOWA JEST CIĄGŁA
Po pierwszym "Hey Jarvis" słuchasz dalej bez przerwy — użytkownik nie musi \
cię wołać przed każdym zdaniem. Traktuj to jak normalną rozmowę: pauza \
nie znaczy koniec, a "hmm", "czekaj" czy "moment" to część rozmowy, nie \
pożegnanie. Nie dopytuj "czy coś jeszcze?" ani nie podsumowuj po każdej \
odpowiedzi — to brzmi jak infolinia.

Rozmowę kończ narzędziem zakoncz_rozmowe TYLKO wtedy, gdy użytkownik \
wyraźnie daje do zrozumienia, że skończył. W razie wątpliwości nie kończ — \
lepiej zostać w rozmowie o jedno zdanie za długo niż rozłączyć się komuś \
w pół myśli.

PAMIĘĆ
Gdy dowiesz się o użytkowniku czegoś trwałego — imienia, czym się zajmuje, \
co lubi, jak chce być traktowany, nad czym pracuje — zapisz to narzędziem \
zapamietaj. Rób to dyskretnie, w tle, bez ogłaszania "zapamiętałem". \
Nie zapisuj rzeczy jednorazowych ani tego, co i tak wynika z kontekstu.
Gdy coś się zdezaktualizuje, użyj narzędzia zapomnij."""


# ---------------------------------------------------------------
# Narzędzia
# ---------------------------------------------------------------
#
# Każde narzędzie to opis (co robi, jakie ma parametry) plus funkcja, która
# je wykonuje. Opis czyta model i na jego podstawie decyduje, czy sięgnąć.
# Dlatego opisy są pisane dla NIEGO, nie dla programisty — mówią, kiedy użyć,
# a nie jak to działa w środku.

NARZEDZIA = [
    {
        "name": "zagraj_piosenke",
        "description": (
            "Odtwarza pojedynczy utwór na Spotify. Używaj, gdy użytkownik prosi "
            "o konkretną piosenkę. Jeśli nie podał wykonawcy, pomiń ten parametr."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tytul": {"type": "string", "description": "Tytuł utworu."},
                "wykonawca": {"type": "string", "description": "Wykonawca, jeśli podany."},
            },
            "required": ["tytul"],
        },
    },
    {
        "name": "zagraj_album",
        "description": (
            "Odtwarza cały album na Spotify, po kolei. Używaj, gdy użytkownik "
            "mówi o albumie lub płycie, a nie o pojedynczym utworze."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tytul": {"type": "string", "description": "Tytuł albumu."},
                "wykonawca": {"type": "string", "description": "Wykonawca, jeśli podany."},
            },
            "required": ["tytul"],
        },
    },
    {
        "name": "otworz_aplikacje",
        "description": (
            "Uruchamia program na komputerze. Podaj nazwę tak, jak powiedział "
            "ją użytkownik — program sam odnajdzie ją w Menu Start."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nazwa": {"type": "string", "description": "Nazwa aplikacji."},
            },
            "required": ["nazwa"],
        },
    },
    {
        "name": "zamknij_aplikacje",
        "description": (
            "Zamyka działający program. Procesy systemowe Windows są chronione "
            "i zostaną odrzucone — to zabezpieczenie, nie błąd."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nazwa": {"type": "string", "description": "Nazwa aplikacji."},
            },
            "required": ["nazwa"],
        },
    },
    {
        "name": "otworz_strone",
        "description": (
            "Otwiera stronę internetową albo wyszukiwanie w przeglądarce Opera GX. "
            "Sam uruchamia Operę, jeśli jest zamknięta — NIE wołaj wcześniej "
            "otworz_aplikacje, bo otworzą się dwa okna. "
            "Podaj 'adres', gdy użytkownik chce konkretną stronę: zamień nazwę "
            "serwisu na pełny adres (Gmail -> https://mail.google.com, "
            "YouTube -> https://www.youtube.com). "
            "Podaj 'fraza', gdy chce coś wyszukać albo mówi 'wpisz w operze...' — "
            "trafi do Google. Gdy chce szukać wewnątrz konkretnego serwisu, zbuduj "
            "adres jego wyszukiwarki, np. "
            "https://www.youtube.com/results?search_query=lofi. "
            "Kilka stron naraz = kilka wywołań."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "adres": {"type": "string", "description": "Pełny adres strony."},
                "fraza": {"type": "string", "description": "Tekst do wyszukania w Google."},
            },
        },
    },
    {
        "name": "sterowanie_systemem",
        "description": (
            "Steruje komputerem. Dostępne polecenia: "
            "glosnosc (wymaga 'wartosc' 0-100), glosniej, ciszej, wycisz, "
            "przywroc_dzwiek, jasnosc (wymaga 'wartosc' 0-100), jasniej, "
            "ciemniej, zablokuj, uspij, restart, wylacz. "
            "Przy glosniej/ciszej/jasniej/ciemniej 'wartosc' jest opcjonalna — "
            "bez niej zmienia o 10 punktów. "
            "Blokada, uśpienie, restart i wyłączenie NIE wykonują się od razu, "
            "tylko gdy skończysz mówić. "
            "WAŻNE: przy restarcie i wyłączeniu też wywołaj to narzędzie OD RAZU "
            "i NIE pytaj wcześniej o zgodę. Samo wywołanie niczego nie wyłącza — "
            "dopiero jego wynik powie ci, o co masz zapytać, a program czeka "
            "na odpowiedź użytkownika dopiero po Twoim pytaniu. Jeśli zapytasz "
            "przed wywołaniem narzędzia, nikt tej odpowiedzi nie odsłucha."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "polecenie": {"type": "string", "description": "Nazwa polecenia z listy powyżej."},
                "wartosc": {"type": "integer", "description": "Procent 0-100 albo o ile zmienić."},
            },
            "required": ["polecenie"],
        },
    },
    {
        "name": "zapamietaj",
        "description": (
            "Zapisuje trwały fakt o użytkowniku, żeby pamiętać go w przyszłych "
            "rozmowach — imię, zajęcie, upodobania, nad czym pracuje. "
            "Jedno krótkie zdanie. Używaj dyskretnie, bez ogłaszania tego."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fakt": {"type": "string", "description": "Jedno zdanie do zapamiętania."},
            },
            "required": ["fakt"],
        },
    },
    {
        "name": "zakoncz_rozmowe",
        "description": (
            "Kończy rozmowę i odsyła Jarvisa do czuwania — od tej chwili "
            "znowu potrzebne będzie 'Hey Jarvis'. Użyj, gdy użytkownik daje "
            "do zrozumienia, że skończył: 'dzięki, to tyle', 'pa', 'wystarczy', "
            "'śpij', 'na razie', 'możesz spadać'. NIE używaj przy zwykłej "
            "przerwie w rozmowie ani gdy po prostu nic nie mówi. "
            "Najpierw pożegnaj się krótko, potem wywołaj to narzędzie."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "zapomnij",
        "description": (
            "Usuwa z pamięci fakty zawierające podany fragment. Używaj, gdy "
            "użytkownik powie, że coś jest nieaktualne."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fragment": {"type": "string", "description": "Fragment tekstu do usunięcia."},
            },
            "required": ["fragment"],
        },
    },
]


# Co model dostaje w odpowiedzi na polecenia, których TU nie wykonujemy.
#
# Blokada, uśpienie, restart i wyłączenie są wykonywane przez main.py dopiero
# po wypowiedzeniu odpowiedzi — inaczej komputer zasnąłby Jarvisowi w pół
# zdania, a pytanie o potwierdzenie nałożyłoby się na to, co akurat mówi
# (wyjaśnienie w system_control.py).
ODLOZONE_POLECENIA = {
    "zablokuj": "Ekran zostanie zablokowany, gdy skończysz mówić. Powiedz o tym krótko.",
    "uspij": "Komputer zostanie uśpiony, gdy skończysz mówić. Pożegnaj się krótko.",
    "restart": (
        "Wymaga potwierdzenia głosem. Zapytaj krótko, czy na pewno "
        "zrestartować komputer — i nie mów nic poza tym pytaniem."
    ),
    "wylacz": (
        "Wymaga potwierdzenia głosem. Zapytaj krótko, czy na pewno "
        "wyłączyć komputer — i nie mów nic poza tym pytaniem."
    ),
}


def _wykonaj_narzedzie(nazwa, parametry):
    """
    Uruchamia narzędzie i zwraca wynik jako tekst dla modelu.

    Wynik NIE jest przeznaczony dla użytkownika — trafia z powrotem do modelu,
    który dopiero na jego podstawie formułuje wypowiedź. Dlatego przy błędach
    zwracamy konkretny opis problemu: to jest informacja, na której model
    może oprzeć kolejną próbę.
    """
    try:
        if nazwa == "zagraj_piosenke":
            komunikat, sukces = spotify_controller.zagraj_piosenke(
                parametry.get("tytul"), parametry.get("wykonawca")
            )
        elif nazwa == "zagraj_album":
            komunikat, sukces = spotify_controller.zagraj_album(
                parametry.get("tytul"), parametry.get("wykonawca")
            )
        elif nazwa == "otworz_aplikacje":
            komunikat, sukces = app_launcher.otworz_aplikacje(parametry.get("nazwa"))
        elif nazwa == "zamknij_aplikacje":
            komunikat, sukces = app_launcher.zamknij_aplikacje(parametry.get("nazwa"))
        elif nazwa == "sterowanie_systemem":
            polecenie = (parametry.get("polecenie") or "").strip().lower()
            if polecenie in system_control.POLECENIA_PO_ODPOWIEDZI:
                komunikat, sukces = ODLOZONE_POLECENIA[polecenie], True
            else:
                komunikat, sukces = system_control.wykonaj(
                    polecenie, parametry.get("wartosc")
                )
        elif nazwa == "otworz_strone":
            komunikat, sukces = przegladarka.otworz_strone(
                parametry.get("adres"), parametry.get("fraza")
            )
        elif nazwa == "zapamietaj":
            komunikat, sukces = pamiec.zapamietaj_fakt(parametry.get("fakt")), True
        elif nazwa == "zapomnij":
            komunikat, sukces = pamiec.zapomnij_fakt(parametry.get("fragment")), True
        elif nazwa == "zakoncz_rozmowe":
            # Samo narzedzie nic nie robi - liczy sie to, ze zostalo wywolane.
            # Flage przechwytuje petla nizej i przekazuje do main.py.
            komunikat, sukces = "Rozmowa zakonczona.", True
        else:
            return f"Nieznane narzędzie: {nazwa}", False
    except Exception as e:
        # Wyjątek w narzędziu nie może zabić rozmowy. Oddajemy go modelowi
        # jako zwykły wynik — niech sam zdecyduje, co powiedzieć.
        logger.exception("Narzędzie %s wywaliło się", nazwa)
        return f"Narzędzie zawiodło: {e}", False

    logger.info("[NARZĘDZIE] %s(%s) -> %s", nazwa, parametry, komunikat)
    return komunikat, sukces


# ---------------------------------------------------------------
# Dzielenie odpowiedzi na zdania (dla mowy)
# ---------------------------------------------------------------

GRANICA_ZDANIA = re.compile(r"[.!?…]+[\s]")


def _tnij_na_zdania(bufor):
    """
    Wydziela z bufora kompletne zdania. Wymóg białego znaku po kropce sprawia,
    że "3.14" nie zostanie potraktowane jako koniec zdania.

    Zwraca: (lista gotowych zdań, reszta bufora).
    """
    zdania = []
    pozycja = 0

    for dopasowanie in GRANICA_ZDANIA.finditer(bufor):
        zdanie = bufor[pozycja:dopasowanie.end()].strip()
        if zdanie:
            zdania.append(zdanie)
        pozycja = dopasowanie.end()

    return zdania, bufor[pozycja:]


# ---------------------------------------------------------------
# Główna pętla agenta
# ---------------------------------------------------------------

_klient = None


def _utworz_klienta():
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "Brak ANTHROPIC_API_KEY. Dopisz go do pliku .env obok tego skryptu."
        )
    return anthropic.Anthropic()


def _opisz_blad_api(blad):
    """
    Zamienia błąd API na zdanie, które Jarvis powie na głos.

    Kiedyś każdy błąd kończył się "urwało mi się połączenie" — także brak
    środków na koncie. Przez to szukałbyś problemu z internetem, a trzeba
    było doładować konto. Każda przyczyna ma inne lekarstwo, więc mówimy,
    która to.
    """
    tresc = str(blad).lower()
    if "credit balance" in tresc:
        return "Skończyły się środki na koncie Anthropic. Doładuj je, a wracam do gry."
    if isinstance(blad, anthropic.AuthenticationError):
        return "Mój klucz do API nie działa. Sprawdź plik .env."
    if isinstance(blad, anthropic.RateLimitError):
        return "Za dużo zapytań naraz. Daj mi chwilę i spróbuj ponownie."
    if isinstance(blad, anthropic.APIConnectionError):
        return "Nie mogę się połączyć z serwerem. Sprawdź internet."
    return "Coś mi się urwało z połączeniem. Spróbuj jeszcze raz."


def _zaloguj_tokeny(uzycie):
    """
    Zapisuje do jarvis.log, ile tokenów zjadło jedno zapytanie.

    Tokeny WEJŚCIOWE to wszystko, co wysyłamy: instrukcje, opisy narzędzi,
    pamięć, historia rozmowy i wyniki wyszukiwania. Wysyłamy to PRZY KAŻDYM
    zapytaniu od nowa — model niczego nie pamięta między zapytaniami.
    Tokeny WYJŚCIOWE to to, co model napisał.
    """
    z_cache = getattr(uzycie, "cache_read_input_tokens", 0) or 0
    do_cache = getattr(uzycie, "cache_creation_input_tokens", 0) or 0
    szukania = getattr(getattr(uzycie, "server_tool_use", None),
                       "web_search_requests", 0) or 0
    logger.info(
        "[TOKENY] %s: wejście %d (+%d z cache, +%d zapis do cache), "
        "wyjście %d, wyszukiwań %d",
        MODEL, uzycie.input_tokens, z_cache, do_cache,
        uzycie.output_tokens, szukania,
    )


def odpowiedz(tekst_uzytkownika, historia=None):
    """
    GŁÓWNE WEJŚCIE TEGO MODUŁU — to woła main.py.

    tekst_uzytkownika — transkrypcja tego, co powiedziałeś
    historia          — dotychczasowe wiadomości rozmowy (lista w formacie API)

    To GENERATOR. Yielduje kolejne całe zdania do wypowiedzenia, a na samym
    końcu — jako OSTATNI element — słownik {"historia": [...]} z pełną,
    zaktualizowaną historią rozmowy.

    Ten mieszany typ wyniku jest kompromisem: chcemy mówić na bieżąco
    (więc generator), ale main.py potrzebuje też historii (której w chwili
    pierwszego zdania jeszcze nie ma). Rozwiązanie: historia przychodzi
    ostatnia, a main.py wie, że ma jej szukać.
    """
    global _klient

    historia = list(historia) if historia else []

    if not tekst_uzytkownika or not tekst_uzytkownika.strip():
        yield "Nie dosłyszałem."
        yield {"historia": historia}
        return

    if _klient is None:
        _klient = _utworz_klienta()

    wiadomosci = historia + [{"role": "user", "content": tekst_uzytkownika}]

    # Instrukcje i pamięć idą jako DWA osobne bloki, w tej kolejności.
    #
    # Instrukcje nie zmieniają się nigdy, więc stawiamy za nimi znacznik
    # cache_control: "zapamiętaj wszystko do tego miejsca". Pamięć (fakty)
    # zmienia się czasem w trakcie rozmowy — gdyby siedziała PRZED znacznikiem,
    # każdy nowy fakt unieważniałby cały zapamiętany początek. Dlatego jest za nim.
    #
    # Pamięć doklejamy przy KAŻDYM zapytaniu, bo fakty mogły dojść w trakcie
    # tej właśnie rozmowy.
    system = [{"type": "text", "text": SYSTEM_PROMPT}]
    if CACHE_PROMPTU:
        system[0]["cache_control"] = {"type": "ephemeral"}
    kontekst_pamieci = pamiec.kontekst_do_promptu().strip()
    if kontekst_pamieci:
        system.append({"type": "text", "text": kontekst_pamieci})

    zapowiedziano_szukanie = False
    zapowiedziane_narzedzia = set()
    koniec_rozmowy = False
    # Polecenie systemowe do wykonania przez main.py po wypowiedzeniu odpowiedzi
    # (blokada, uśpienie, restart, wyłączenie) — patrz ODLOZONE_POLECENIA.
    polecenie_systemowe = None

    for tura in range(MAX_TUR_NARZEDZI):
        bufor = ""
        powod = None
        wywolania = []

        try:
            with _klient.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=wiadomosci,
                tools=NARZEDZIA + [NARZEDZIE_WYSZUKIWANIA],
                output_config={"effort": WYSILEK},
                # Drugi znacznik, stawiany automatycznie na końcu rozmowy.
                # Następne zapytanie zaczyna się od tej samej historii,
                # więc ją też odczyta z cache zamiast płacić od nowa.
                **({"cache_control": {"type": "ephemeral"}} if CACHE_PROMPTU else {}),
            ) as strumien:
                for zdarzenie in strumien:
                    if zdarzenie.type == "text":
                        bufor += zdarzenie.text
                        gotowe, bufor = _tnij_na_zdania(bufor)
                        for zdanie in gotowe:
                            yield zdanie

                    elif zdarzenie.type == "content_block_start":
                        typ = zdarzenie.content_block.type

                        # Bloki tekstowe pomijamy: odpowiedź z cytowaniami bywa
                        # pocięta na kilka bloków w środku zdania, a domykanie
                        # na takiej granicy rozrywałoby wypowiedź na pół.
                        if typ == "text":
                            continue

                        # Model przerywa mówienie, żeby coś zrobić — to dowód,
                        # że poprzedni blok tekstu jest domknięty.
                        ogonek = bufor.strip()
                        bufor = ""
                        if ogonek:
                            yield ogonek

                        if typ == "server_tool_use" and not zapowiedziano_szukanie:
                            zapowiedziano_szukanie = True
                            yield KOMUNIKAT_WYSZUKIWANIA

                odpowiedz_modelu = strumien.get_final_message()
                powod = odpowiedz_modelu.stop_reason
                _zaloguj_tokeny(odpowiedz_modelu.usage)

        except anthropic.APIError as blad:
            logger.exception("Błąd Claude API")
            if not bufor.strip():
                yield _opisz_blad_api(blad)
            yield {"historia": historia}
            return

        # Obcięcie na limicie: zdania wypowiedziane wcześniej były kompletne,
        # ale resztę bufora trzeba porzucić — urywa się w pół słowa.
        if powod == "max_tokens":
            logger.warning("Odpowiedź obcięta na limicie %d tokenów.", MAX_TOKENS)
            bufor = ""

        wywolania = [b for b in odpowiedz_modelu.content if b.type == "tool_use"]

        # Model skończył mówić i niczego nie chce — koniec tury.
        if not wywolania:
            reszta = bufor.strip()
            if reszta:
                yield reszta

            wiadomosci = wiadomosci + [
                {"role": "assistant", "content": odpowiedz_modelu.content}
            ]
            yield {"historia": wiadomosci, "koniec": koniec_rozmowy,
                   "system": polecenie_systemowe}
            return

        # Zostało coś w buforze przed wywołaniem narzędzia — wypowiedz.
        reszta = bufor.strip()
        if reszta:
            yield reszta

        # Zapowiedzi dla narzędzi, które trwają długo. Model zwykle sam coś
        # powie ("już włączam"), ale nie zawsze — a wtedy cisza podczas
        # szukania utworu wygląda na zawieszenie.
        for wywolanie in wywolania:
            zapowiedz = ZAPOWIEDZI.get(wywolanie.name)
            if zapowiedz and wywolanie.name not in zapowiedziane_narzedzia and not reszta:
                zapowiedziane_narzedzia.add(wywolanie.name)
                yield zapowiedz

        wiadomosci = wiadomosci + [
            {"role": "assistant", "content": odpowiedz_modelu.content}
        ]

        wyniki = []
        for wywolanie in wywolania:
            if wywolanie.name == "zakoncz_rozmowe":
                koniec_rozmowy = True
            elif wywolanie.name == "sterowanie_systemem":
                polecenie = (wywolanie.input.get("polecenie") or "").strip().lower()
                if polecenie in system_control.POLECENIA_PO_ODPOWIEDZI:
                    polecenie_systemowe = polecenie
            komunikat, sukces = _wykonaj_narzedzie(wywolanie.name, wywolanie.input)
            wyniki.append({
                "type": "tool_result",
                "tool_use_id": wywolanie.id,
                "content": komunikat,
                # is_error mówi modelowi wprost, że próba się nie powiodła.
                # Bez tego musiałby się domyślać z treści komunikatu.
                "is_error": not sukces,
            })

        wiadomosci = wiadomosci + [{"role": "user", "content": wyniki}]

        # Pożegnanie już padło, więc nie wysyłamy wyniku z powrotem do modelu.
        #
        # Gdybyśmy to zrobili, zobaczyłby "rozmowa zakończona" i grzecznie
        # pożegnałby się JESZCZE RAZ — w testach wychodziło z tego
        # "Do usłyszenia. Do usłyszenia!". Przy okazji oszczędzamy
        # jedno zapytanie do API, czyli parę sekund ciszy na koniec.
        if koniec_rozmowy:
            yield {"historia": wiadomosci, "koniec": True,
                   "system": polecenie_systemowe}
            return

    # Wyczerpany limit tur — model w kółko sięga po narzędzia.
    logger.warning("Przekroczono %d tur narzędzi — przerywam.", MAX_TUR_NARZEDZI)
    yield "Coś mi się zapętliło. Spróbuj powiedzieć to inaczej."
    yield {"historia": historia}


# --- Test samego agenta: `python agent.py` (bez mikrofonu i bez głosu) ---
if __name__ == "__main__":
    import sys

    from logging_setup import skonfiguruj_logowanie

    skonfiguruj_logowanie(logging.INFO)

    # Rozmowa startuje od końcówki poprzedniej — dokładnie tak jak w main.py.
    historia = pamiec.poprzednia_rozmowa()
    if historia:
        print(f"[wznawiam poprzednią rozmowę: {len(historia)} wiadomości]\n")

    print("Pisz do Jarvisa. Pusta linia kończy.\n")

    while True:
        try:
            wejscie = input("TY     : ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not wejscie:
            break

        print("JARVIS : ", end="", flush=True)
        for element in odpowiedz(wejscie, historia):
            if isinstance(element, dict):
                historia = element["historia"]
            else:
                print(element, end=" ", flush=True)
        print("\n")

    pamiec.zapisz_rozmowe(historia)
    print("[rozmowa zapisana do pamięci]")
