"""
chat.py — warstwa rozmowy Jarvisa.

Odpowiada za swobodną rozmowę: pytania, żarty, ciekawostki — wszystko,
co nie jest komendą do wykonania.


CZYM TO SIĘ RÓŻNI OD router.py
==============================

To dwa osobne moduły gadające z tym samym API, ale w zupełnie różnych celach:

    router.py            chat.py
    ---------            -------
    model Haiku          model Sonnet
    tekst -> JSON        tekst -> zdanie po polsku
    bez pamięci          z pamięcią rozmowy
    ma sklasyfikować     ma odpowiedzieć

Router to szybki segregator: dostaje zdanie i ma tylko zdecydować, którą
funkcję wywołać. Liczy się czas reakcji, więc leci na najszybszym modelu
i nie pamięta nic z poprzednich komend — bo i po co.

Chat ma prowadzić rozmowę, czyli rozumieć kontekst i sensownie odpowiadać.
Dlatego mocniejszy model i historia poprzednich wymian.


DLACZEGO ODPOWIEDZI SĄ KRÓTKIE
==============================

To ma być WYPOWIEDZIANE, nie przeczytane. Przy czytaniu z ekranu można
przeskoczyć wzrokiem akapit; przy słuchaniu trzeba wysłuchać wszystkiego,
sekunda po sekundzie. Odpowiedź na pięć zdań to ponad pół minuty gadania,
przez które nie da się przewinąć. Stąd nacisk w system prompcie na 1-3 zdania.
"""

import logging
import os
import re

import anthropic
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Sonnet, nie Haiku — rozmowa wymaga więcej niż klasyfikacja.
# Haiku zostaje w router.py, gdzie liczy się wyłącznie szybkość.
MODEL = "claude-sonnet-5"

# Limit długości odpowiedzi.
#
# Wcześniej było tu 300 i to okazało się błędem: przy dłuższej odpowiedzi
# (np. gdy sam poprosisz o wyjaśnienie czegoś szerzej) model dobijał do limitu
# i wypowiedź urywała się w połowie słowa.
#
# Kluczowe nieporozumienie, które to spowodowało: max_tokens NIE jest sposobem
# na skracanie odpowiedzi. To twarda gilotyna — po prostu ucina generowanie
# w losowym miejscu, bez dokończenia myśli. O zwięzłość dba SYSTEM PROMPT,
# który prosi o 1-3 zdania i który model rozumie. max_tokens ma być tylko
# bezpiecznikiem na wypadek, gdyby model kompletnie odjechał.
#
# 1024 tokeny to z grubsza 700-800 polskich słów — dużo więcej, niż Jarvis
# kiedykolwiek powinien powiedzieć, więc w normalnej pracy limit nie zadziała.
#
# Uwaga: przy włączonym wyszukiwaniu w ten sam limit wliczają się także bloki
# rozumowania modelu i wywołania narzędzia, nie tylko wypowiadany tekst.
# Dlatego zapas jest tu spory — sama odpowiedź to zwykle 50-100 tokenów.
MAX_TOKENS = 1024

# Wbudowane narzędzie wyszukiwania. "Wbudowane" znaczy, że całą robotę
# (zapytanie do wyszukiwarki, pobranie stron, streszczenie) wykonuje serwer
# Anthropica — my tylko deklarujemy, że model MOŻE go użyć. Nie ma tu nic
# do zaimplementowania po naszej stronie, żadnego klucza do wyszukiwarki
# ani obsługi wyników.
#
# Typ narzędzia jest WERSJONOWANY datą. Sprawdziłem, które wersje zna
# zainstalowane SDK i którą przyjmuje API — 20260318 jest najnowszą działającą.
# Gdyby kiedyś przestała być obsługiwana, w anthropic.types znajdziesz kolejne
# (szukaj nazw zaczynających się od WebSearchTool).
NARZEDZIE_WYSZUKIWANIA = {
    "type": "web_search_20260318",
    "name": "web_search",
    # JAK max_uses WPŁYWA NA MODEL
    # ============================
    # To twardy limit: ile razy w obrębie JEDNEJ odpowiedzi model może sięgnąć
    # do wyszukiwarki. Po wyczerpaniu limitu narzędzie przestaje być dostępne
    # i model musi odpowiedzieć na podstawie tego, co już zebrał.
    #
    # Model wie o tym limicie, więc działa on dwojako. Po pierwsze zapobiega
    # najgorszemu scenariuszowi: przy zawiłym pytaniu model potrafi szukać
    # raz za razem, doprecyzowując zapytanie, a każde szukanie to kilka sekund
    # ciszy z głośników. Po drugie zmienia sposób szukania — mając dwie próby
    # zamiast trzech, model formułuje zapytanie ostrożniej, zamiast liczyć,
    # że dopyta jeszcze kilka razy.
    #
    # To NIE jest jednak gwarancja szybkości: przy limicie 2 model nadal może
    # wykonać dwa wyszukiwania. Limit ucina ogon najgorszych przypadków,
    # a nie typowy czas odpowiedzi.
    "max_uses": 2,
}

# Zapowiedź wypowiadana w chwili, gdy model sięga po narzędzie.
# Wyszukiwanie trwa kilkanaście sekund i bez tego zdania z głośników
# nie leci nic — a milczący asystent wygląda na zawieszonego.
KOMUNIKAT_WYSZUKIWANIA = "Dobra, sprawdzam, szefie."

# Ile ostatnich wiadomości trzymamy w historii (licząc pytania i odpowiedzi).
# Rozmowa głosowa rzadko wraca do tego, co padło dziesięć wymian temu,
# a każda wiadomość w historii to tokeny wysyłane przy KAŻDYM kolejnym
# zapytaniu — bez limitu koszt i czas odpowiedzi rosłyby w nieskończoność.
MAX_HISTORII = 20

SYSTEM_PROMPT = """Jesteś Jarvisem — asystentem głosowym. Rozmawiasz z użytkownikiem \
na głos, a nie na piśmie.

Twój charakter:
- Pomocny i konkretny. Odpowiadasz na pytanie, które padło, bez owijania.
- Zwięzły. Domyślnie 1-3 krótkie zdania. Nigdy więcej niż 4.
- Lekko dowcipny, z nutą sucharowatej uprzejmości — ale żart nigdy nie może \
zastąpić odpowiedzi ani jej wydłużyć.
- Mówisz po polsku, chyba że użytkownik odezwie się po angielsku.

Zasady wypowiedzi, bo to będzie odczytane przez syntezator mowy:
- Żadnego formatowania: bez list punktowanych, nagłówków, gwiazdek, emoji, \
bloków kodu i linków. Sam tekst, jak w rozmowie.
- Liczby, daty i jednostki zapisuj słowami tam, gdzie brzmi to naturalnie \
("około dwudziestu stopni", a nie "ok. 20°C").
- Nie wypisuj długich wyliczeń. Jeśli musisz wymienić kilka rzeczy, podaj \
najwyżej trzy i zaproponuj, że powiesz więcej, jeśli użytkownik chce.

Gdy czegoś nie wiesz, mów to wprost i krótko. Nie zmyślaj faktów, dat ani nazw.

Masz dostęp do wyszukiwarki internetowej i możesz z niej korzystać, gdy pytanie \
tego wymaga. Używaj jej OSZCZĘDNIE — tylko do informacji, które zmieniają się \
w czasie albo których po prostu nie znasz:
- pogoda, kursy walut, wyniki sportowe, ceny,
- bieżące wydarzenia i wiadomości,
- daty premier, nowe wersje produktów,
- cokolwiek, co wydarzyło się niedawno.

NIE szukaj rzeczy, które już wiesz — stolic państw, podstawowych faktów \
historycznych, definicji, prostych obliczeń, ogólnej wiedzy o muzyce czy filmach. \
Wyszukiwanie trwa kilka sekund, a użytkownik czeka na odpowiedź na głos, \
więc niepotrzebne szukanie to strata jego czasu.

Gdy już szukasz, rób to OSZCZĘDNIE. Przy prostym, jednoznacznym pytaniu \
faktograficznym ("jaka jest pogoda w Krakowie", "ile kosztuje euro", \
"kto wygrał wczorajszy mecz") wykonaj JEDNO precyzyjne wyszukiwanie \
i od razu odpowiedz na jego podstawie. Nie doprecyzowuj zapytania kolejnymi \
próbami, nie sprawdzaj dodatkowych źródeł "dla pewności" i nie szukaj \
kontekstu, o który nikt nie pytał. Każde dodatkowe wyszukiwanie to kilka \
sekund ciszy dla czekającego człowieka. Odpowiedź "wystarczająco dobra teraz" \
jest lepsza niż "wyczerpująca za dwadzieścia sekund".

Gdy skorzystasz z wyszukiwarki, odpowiedz normalnie, po ludzku, tak jak zwykle. \
Nie czytaj adresów stron ani nazw serwisów, nie wymieniaj źródeł, nie mów \
"według wyników wyszukiwania" — po prostu podaj to, czego użytkownik chciał. \
Jeśli informacja jest niepewna albo szybko się zmienia, wystarczy pół zdania \
zastrzeżenia, nie cały akapit."""


def _utworz_klienta():
    """
    Tworzy klienta Claude API.

    Klucz czytany jest z .env (ANTHROPIC_API_KEY) — biblioteka anthropic
    znajduje go sama w zmiennych środowiskowych, które ustawił load_dotenv().
    """
    if not os.getenv("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "Brak ANTHROPIC_API_KEY. Dopisz go do pliku .env obok tego skryptu "
            "(klucz wygenerujesz na console.anthropic.com)."
        )

    return anthropic.Anthropic()


# Klienta tworzymy raz i trzymamy tutaj — nie ma sensu budować go przy każdej wymianie.
_klient = None


def _przytnij_historie(historia):
    """
    Zostawia tylko MAX_HISTORII ostatnich wiadomości.

    Ważny szczegół: API wymaga, żeby rozmowa zaczynała się od wiadomości
    użytkownika i żeby role się przeplatały. Gdyby przycięcie wypadło
    w złym miejscu, historia zaczynałaby się od odpowiedzi asystenta
    i API odrzuciłoby zapytanie. Dlatego po przycięciu zsuwamy początek
    do najbliższej wiadomości użytkownika.
    """
    if len(historia) <= MAX_HISTORII:
        return historia

    przycieta = historia[-MAX_HISTORII:]

    while przycieta and przycieta[0]["role"] != "user":
        przycieta.pop(0)

    return przycieta


def odpowiedz_rozmowa(tekst_uzytkownika, historia=None):
    """
    GŁÓWNE WEJŚCIE TEGO MODUŁU — prowadzi rozmowę.

    tekst_uzytkownika — co powiedział użytkownik (transkrypcja z mikrofonu)
    historia          — lista poprzednich wymian w formacie API:
                        [{"role": "user", "content": "..."},
                         {"role": "assistant", "content": "..."}, ...]
                        Przy pierwszej wymianie podaj None albo pustą listę.

    Zwraca: (odpowiedź jako tekst, zaktualizowana historia).

    Historia jest ZWRACANA, a nie trzymana w module. Dzięki temu to program
    nadrzędny decyduje, kiedy rozmowa się kończy — wystarczy przestać
    przekazywać starą historię, żeby zacząć od nowa. Moduł nie ma ukrytego
    stanu, więc łatwiej go testować i nie zdarzy się, że dwie rozmowy
    niepostrzeżenie się zlepią.
    """
    global _klient

    historia = list(historia) if historia else []

    if not tekst_uzytkownika or not tekst_uzytkownika.strip():
        return "Nie dosłyszałem.", historia

    if _klient is None:
        _klient = _utworz_klienta()

    # Dopisujemy nową wypowiedź do kopii historii. Gdyby zapytanie się nie udało,
    # oryginalna historia zostaje nietknięta — nie zaśmiecimy jej wiadomością,
    # na którą nigdy nie było odpowiedzi.
    wiadomosci = _przytnij_historie(historia + [
        {"role": "user", "content": tekst_uzytkownika}
    ])

    try:
        odpowiedz = _klient.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=wiadomosci,
        )
    except anthropic.APIError:
        logger.exception("Błąd Claude API podczas rozmowy")
        return "Coś mi się urwało z połączeniem. Spróbuj jeszcze raz.", historia

    # Odpowiedź to lista bloków treści — bierzemy tekst z pierwszego bloku typu "text".
    tekst = next(
        (blok.text for blok in odpowiedz.content if blok.type == "text"), ""
    ).strip()

    if not tekst:
        logger.warning("Model zwrócił pustą odpowiedź.")
        return "Zabrakło mi słów. Powtórz, proszę.", historia

    nowa_historia = wiadomosci + [{"role": "assistant", "content": tekst}]

    logger.info("Rozmowa: %r -> %r", tekst_uzytkownika, tekst)
    return tekst, nowa_historia


# ---------------------------------------------------------------
# Wersja strumieniowa
# ---------------------------------------------------------------
#
# Po co, skoro odpowiedz_rozmowa() działa? Bo tam Jarvis milczy przez cały czas
# generowania odpowiedzi, a potem dopiero zaczyna mówić. Przy trzech zdaniach
# to kilka sekund ciszy, w których nie wiadomo, czy w ogóle usłyszał.
#
# W wersji strumieniowej odbieramy tekst kawałek po kawałku i oddajemy go
# do wypowiedzenia od razu, gdy tylko uzbiera się CAŁE zdanie. Pierwsze zdanie
# leci do głośników, podczas gdy model dopiero układa drugie.
#
# Zdanie jest tu najmniejszą sensowną porcją: pojedyncze słowa brzmiałyby
# w syntezatorze poszarpanie, bo intonacja powstaje na poziomie zdania.

# Koniec zdania: znak przestankowy, po którym następuje biały znak.
# Wymóg białego znaku jest istotny — bez niego "3." w "3.14" albo kropka
# w skrócie ucinałyby zdanie w losowym miejscu. Skoro fragmenty z API
# przychodzą pocięte w środku słów, to samo "widzę kropkę" znaczy tylko tyle,
# że kropka jest ostatnim znakiem, jaki na razie dotarł.
GRANICA_ZDANIA = re.compile(r"[.!?…]+[\s]")


def _tnij_na_zdania(bufor):
    """
    Wydziela z bufora wszystkie KOMPLETNE zdania.

    Zwraca: (lista gotowych zdań, reszta bufora do dalszego zbierania).
    """
    zdania = []
    pozycja = 0

    for dopasowanie in GRANICA_ZDANIA.finditer(bufor):
        zdanie = bufor[pozycja:dopasowanie.end()].strip()
        if zdanie:
            zdania.append(zdanie)
        pozycja = dopasowanie.end()

    return zdania, bufor[pozycja:]


def odpowiedz_rozmowa_stream(tekst_uzytkownika, historia=None):
    """
    To samo co odpowiedz_rozmowa(), ale oddaje odpowiedź zdanie po zdaniu,
    w miarę jak model ją generuje.

    tekst_uzytkownika — co powiedział użytkownik
    historia          — poprzednie wymiany (jak w odpowiedz_rozmowa)

    To GENERATOR — trzeba po nim iterować, np.:

        for zdanie in odpowiedz_rozmowa_stream("cześć", []):
            print(zdanie)

    Uwaga na różnicę wobec odpowiedz_rozmowa(): ta funkcja NIE zwraca
    zaktualizowanej historii. Nie może — w chwili, gdy oddaje pierwsze zdanie,
    reszty odpowiedzi jeszcze nie ma. Historię składa program nadrzędny
    z tekstu użytkownika i sklejonych zdań, które faktycznie padły.

    Yielduje: kolejne całe zdania (stringi).
    """
    global _klient

    historia = list(historia) if historia else []

    if not tekst_uzytkownika or not tekst_uzytkownika.strip():
        yield "Nie dosłyszałem."
        return

    if _klient is None:
        _klient = _utworz_klienta()

    wiadomosci = _przytnij_historie(historia + [
        {"role": "user", "content": tekst_uzytkownika}
    ])

    bufor = ""
    powod_zakonczenia = None

    # Czy zapowiedź "sprawdzam" już poszła. Model może sięgnąć po narzędzie
    # kilka razy w jednej odpowiedzi, a powtarzanie tego zdania brzmiałoby głupio.
    zapowiedziano = False

    try:
        # stream() zwraca menedżer kontekstu — blok `with` gwarantuje,
        # że połączenie zostanie domknięte także wtedy, gdy ktoś przerwie
        # iterowanie po generatorze w połowie.
        with _klient.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=wiadomosci,
            tools=[NARZEDZIE_WYSZUKIWANIA],
        ) as strumien:
            # Iterujemy po ZDARZENIACH, nie po strumien.text_stream.
            #
            # Przy włączonym wyszukiwaniu odpowiedź nie jest już jednym blokiem
            # tekstu. Sprawdziłem, co realnie przychodzi na pytanie o pogodę:
            # thinking, server_tool_use, web_search_tool_result,
            # code_execution_tool_result i dopiero na końcu bloki text.
            #
            # Nas interesują wyłącznie zdarzenia typu "text" — to one niosą
            # wypowiadaną treść. Cała technika wyszukiwania jest ignorowana
            # z automatu, bo po prostu nie pasuje do tego warunku.
            for zdarzenie in strumien:
                if zdarzenie.type == "text":
                    # Fragmenty NIE pokrywają się ze słowami ani zdaniami —
                    # potrafią urwać się w środku wyrazu ("kolorow" + "ych"),
                    # dlatego sklejamy je w buforze i dopiero na nim
                    # szukamy granic zdań.
                    bufor += zdarzenie.text
                    gotowe, bufor = _tnij_na_zdania(bufor)
                    for zdanie in gotowe:
                        logger.info("[STREAM] zdanie: %s", zdanie)
                        yield zdanie

                elif zdarzenie.type == "content_block_start":
                    typ_bloku = zdarzenie.content_block.type

                    # Bloki tekstowe świadomie POMIJAMY. Odpowiedź z cytowaniami
                    # bywa pocięta na kilka bloków text jeden po drugim, w środku
                    # zdania — domykanie zdania na takiej granicy rozrywałoby
                    # wypowiedź na pół i brzmiało jak zacinanie się.
                    # Bufor spokojnie płynie przez te granice.
                    if typ_bloku == "text":
                        continue

                    # Blok NIE-tekstowy: model przerywa mówienie, żeby coś zrobić
                    # (szukać, policzyć, pomyśleć). To dowód, że poprzedni blok
                    # tekstu jest już domknięty — więc ogonek w buforze
                    # jest kompletny i można go od razu wypowiedzieć.
                    ogonek = bufor.strip()
                    bufor = ""
                    if ogonek:
                        logger.info("[STREAM] zdanie (przed narzędziem): %s", ogonek)
                        yield ogonek

                    # Zapowiedź wyszukiwania — raz na odpowiedź.
                    #
                    # Reagujemy na KAŻDY server_tool_use, nie tylko na web_search.
                    # Pomiar pokazał, że model najpierw uruchamia code_execution
                    # (po ~2 s), a dopiero potem web_search (po ~7 s). Czekanie
                    # na ten drugi oznaczałoby pięć sekund niepotrzebnej ciszy.
                    if typ_bloku == "server_tool_use" and not zapowiedziano:
                        zapowiedziano = True
                        logger.info("[STREAM] model sięga po narzędzie — zapowiadam")
                        yield KOMUNIKAT_WYSZUKIWANIA

            # Dopiero po wyczerpaniu strumienia API mówi nam, DLACZEGO przestało
            # generować. get_final_message() trzeba wywołać jeszcze wewnątrz
            # bloku `with`, bo po jego zamknięciu połączenie już nie żyje.
            powod_zakonczenia = strumien.get_final_message().stop_reason

    except anthropic.APIError:
        logger.exception("Błąd Claude API podczas rozmowy strumieniowej")
        # Jeśli zdążyliśmy już coś powiedzieć, nie doklejamy komunikatu o błędzie
        # w środku wypowiedzi — brzmiałoby to absurdalnie. Milkniemy po prostu.
        if not bufor.strip():
            yield "Coś mi się urwało z połączeniem. Spróbuj jeszcze raz."
        return

    reszta = bufor.strip()

    # CZYM JEST stop_reason
    # =====================
    # To pole z odpowiedzi API mówiące, dlaczego model przestał pisać. Najważniejsze
    # wartości dla nas:
    #
    #   "end_turn"   — model skończył, bo powiedział wszystko, co miał. Normalny koniec.
    #   "max_tokens" — model NIE skończył. Trafił w limit max_tokens i został ucięty
    #                  w losowym miejscu, często w połowie słowa.
    #
    # DLACZEGO TO WAŻNE AKURAT PRZY STREAMINGU
    # ========================================
    # Bez streamingu dostajesz całą odpowiedź naraz i urwane zdanie widać
    # gołym okiem. Przy streamingu odbierasz strzępy tekstu i sam sklejasz
    # je w zdania — a wtedy urwany koniec wygląda DOKŁADNIE tak samo jak
    # normalne ostatnie zdanie bez spacji na końcu. Nie da się ich odróżnić
    # po samej treści; jedynym źródłem prawdy jest stop_reason.
    #
    # Bez tego sprawdzenia Jarvis wypowiadał urwane "...to zależy od tego, jak dłu"
    # i — co gorsza — zapisywał ten kaleki fragment do historii rozmowy,
    # gdzie zostawał na kolejne wymiany jako rzekoma jego wypowiedź.
    if powod_zakonczenia == "max_tokens":
        logger.warning(
            "Odpowiedź została OBCIĘTA na limicie %d tokenów (stop_reason=max_tokens). "
            "Pomijam niedokończoną końcówkę: %r",
            MAX_TOKENS, reszta[:120],
        )
        # Świadomie NIE oddajemy reszty bufora. Zdania wypowiedziane wcześniej
        # były kompletne, więc wypowiedź się urwie, ale przynajmniej nie
        # w połowie słowa. A skoro main.py buduje historię z tego, co faktycznie
        # padło, urwany fragment nie trafi też do kontekstu rozmowy.
        #
        # Bufor jest tu jedynym miejscem, gdzie urwana treść może się schować:
        # ogonki przed blokami narzędzi wypuszczaliśmy wyżej, ale tamte były
        # domknięte z definicji — dowodem był fakt, że model przeszedł
        # do kolejnego bloku.
        return

    # Ostatnie zdanie zwykle nie ma po sobie spacji (strumień się kończy),
    # więc pętla wyżej go nie wyłapie. Oddajemy resztę bufora tak, jak jest.
    if reszta:
        logger.info("[STREAM] zdanie (ostatnie): %s", reszta)
        yield reszta


# --- Test samego modułu: `python chat.py` (bez głosu, sama konsola) ---
if __name__ == "__main__":
    from logging_setup import skonfiguruj_logowanie

    skonfiguruj_logowanie(logging.WARNING)  # ciszej, żeby nie zaśmiecać rozmowy

    # Celowo dobrane tak, żeby sprawdzić PAMIĘĆ KONTEKSTU: druga i trzecia
    # wypowiedź nie mają sensu bez poprzednich, a czwarta wraca do pierwszej.
    WYMIANY = [
        "Cześć, mam na imię Klaudiusz.",
        "Jaka jest stolica Portugalii?",
        "A ile mniej więcej ma mieszkańców?",
        "Pamiętasz jak mam na imię?",
        "Jaka jest dziś pogoda w Warszawie?",   # sprawdza, czy przyzna się do braku dostępu
    ]

    historia = []

    print("=" * 66)
    print(f"Test rozmowy — model {MODEL}")
    print("=" * 66)

    for wypowiedz in WYMIANY:
        print(f"\n  TY     : {wypowiedz}")
        odpowiedz, historia = odpowiedz_rozmowa(wypowiedz, historia)
        print(f"  JARVIS : {odpowiedz}")

    print()
    print("=" * 66)
    print(f"Wiadomości w historii: {len(historia)} (limit: {MAX_HISTORII})")
    print("=" * 66)
