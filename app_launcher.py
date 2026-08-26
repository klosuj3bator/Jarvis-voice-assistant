"""
app_launcher.py — warstwa "rąk" Jarvisa do uruchamiania programów.

Znajduje aplikację po nazwie i ją uruchamia. Szuka w trzech miejscach,
zawsze w tej samej kolejności:

    1. apps_config.json  — Twoje ręczne wpisy. Mają PRIORYTET, bo skoro coś
                           wpisałeś sam, to znaczy że wiesz lepiej niż zgadywanka.
    2. apps_cache.json   — aplikacje znalezione automatycznie przy poprzednich
                           uruchomieniach. Program dopisuje je sam.
    3. Menu Start        — przeszukanie skrótów .lnk. Wolne (setki plików),
                           więc robimy to tylko gdy dwa pierwsze zawiodły,
                           a wynik od razu zapisujemy do cache.

Dzięki temu "otwórz Chrome" działa bez konfigurowania czegokolwiek, a druga
próba jest już natychmiastowa, bo leci prosto z cache.
"""

import difflib
import json
import logging
import os
import subprocess

import psutil
import pythoncom
import win32com.client

logger = logging.getLogger(__name__)

KATALOG = os.path.dirname(os.path.abspath(__file__))

# Twoje ręczne wpisy — tego pliku program nigdy nie nadpisuje.
SCIEZKA_CONFIGU = os.path.join(KATALOG, "apps_config.json")

# Wpisy znalezione automatycznie — tym plikiem zarządza wyłącznie program.
SCIEZKA_CACHE = os.path.join(KATALOG, "apps_cache.json")

# Foldery, w których Windows trzyma skróty Menu Start.
# Pierwszy jest wspólny dla wszystkich użytkowników, drugi Twój prywatny.
FOLDERY_MENU_START = [
    os.path.join(
        os.environ.get("ProgramData", r"C:\ProgramData"),
        r"Microsoft\Windows\Start Menu\Programs",
    ),
    os.path.join(
        os.environ.get("APPDATA", ""),
        r"Microsoft\Windows\Start Menu\Programs",
    ),
]

# Próg podobieństwa dla dopasowania przybliżonego (0.0-1.0).
# 0.6 to domyślna wartość difflib i w praktyce dobry kompromis:
# wyłapuje literówki i odmiany, ale nie podsuwa czegoś zupełnie innego.
# Podnieś, jeśli Jarvis otwiera nie to co trzeba; obniż, jeśli za często nie znajduje.
PROG_PODOBIENSTWA = 0.6

# Zamykanie ma WYŻSZY próg niż otwieranie i to jest celowe.
# Otworzenie złej aplikacji to drobna niedogodność — zamykasz okno i już.
# Zamknięcie złej aplikacji może kosztować niezapisaną pracę, więc przy
# wątpliwym dopasowaniu wolimy powiedzieć "nie znalazłem".
PROG_PODOBIENSTWA_ZAMYKANIE = 0.75


# ---------------------------------------------------------------
# Zabezpieczenia przy zamykaniu procesów
# ---------------------------------------------------------------
#
# Zamykanie procesów to jedyna operacja w całym Jarvisie, która potrafi
# narobić realnych szkód: ubicie niewłaściwego procesu może zabrać niezapisaną
# pracę, wywalić pulpit albo zdestabilizować system. Dlatego mamy tu
# CZTERY niezależne warstwy ochrony — każda działa, nawet gdyby trzy pozostałe
# zawiodły:
#
#   1. LISTA WYKLUCZEŃ (poniżej) — nazwy, których Jarvis nie tknie nigdy,
#      niezależnie od tego, jak dobrze pasują do wypowiedzianej nazwy.
#   2. TYLKO TWOJE PROCESY — pomijamy wszystko, co działa na koncie SYSTEM
#      albo innego użytkownika. Aplikacje, które uruchamiasz, działają na Twoim
#      koncie, więc nic sensownego przez to nie tracimy.
#   3. WYŻSZY PRÓG DOPASOWANIA — patrz PROG_PODOBIENSTWA_ZAMYKANIE powyżej.
#   4. OSTATNIA BARIERA TUŻ PRZED UBICIEM — nazwę każdego procesu sprawdzamy
#      jeszcze raz w _ubij_procesy(), już po całym dopasowywaniu. To bezpiecznik
#      na wypadek, gdyby przyszła ścieżka do kodu ominęła wcześniejsze kontrole.

PROCESY_CHRONIONE = frozenset({
    # Jądro systemu i procesy pseudo-systemowe
    "system", "system idle process", "registry", "secure system",
    "memory compression", "idle",

    # Rdzeń rozruchu i sesji — ubicie któregokolwiek to natychmiastowy
    # niebieski ekran albo wylogowanie bez zapisania czegokolwiek.
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "lsaiso.exe", "userinit.exe", "logonui.exe",

    # Powłoka i pulpit — działają na TWOIM koncie, więc warstwa 2 ich nie złapie.
    # Bez tej listy "zamknij eksplorator" zabrałoby pasek zadań i ikony pulpitu.
    "explorer.exe", "dwm.exe", "sihost.exe", "ctfmon.exe", "fontdrvhost.exe",
    "startmenuexperiencehost.exe", "shellexperiencehost.exe",
    "searchhost.exe", "searchindexer.exe", "searchapp.exe",
    "taskhostw.exe", "runtimebroker.exe", "applicationframehost.exe",

    # Infrastruktura systemowa
    "svchost.exe", "spoolsv.exe", "conhost.exe", "dllhost.exe",
    "rundll32.exe", "audiodg.exe", "wudfhost.exe", "wmiprvse.exe",
    "sppsvc.exe", "trustedinstaller.exe", "tiworker.exe",

    # Zabezpieczenia — Jarvis nie ma prawa wyłączać ochrony komputera.
    "msmpeng.exe", "nissrv.exe", "securityhealthservice.exe",
    "securityhealthsystray.exe", "smartscreen.exe",

    # SAM JARVIS. Bez tego "zamknij pythona" ubiłoby asystenta w trakcie
    # wykonywania tej właśnie komendy.
    "python.exe", "pythonw.exe",
})


# Ta sama lista, ale bez rozszerzeń: {"explorer", "svchost", "csrss", ...}.
#
# Jest niezbędna, bo nazwy procesów krążą po kodzie w dwóch postaciach:
# psutil podaje "explorer.exe", a dopasowywanie po nazwach operuje na "explorer".
# Porównywanie tylko pełnych nazw przepuszczało wszystko, co przyszło bez .exe
# — i tak właśnie "zamknij explorer" potrafiło ubić powłokę Windows.
# Sprowadzenie obu stron do jednej postaci usuwa całą klasę takich pomyłek.
PROCESY_CHRONIONE_RDZENIE = frozenset(
    os.path.splitext(nazwa)[0] for nazwa in PROCESY_CHRONIONE
)


def _chroniony(nazwa_procesu):
    """
    Sprawdza, czy proces jest na liście nietykalnych.

    Porównanie idzie po nazwie BEZ rozszerzenia, więc "explorer", "explorer.exe"
    i "EXPLORER.EXE" są rozpoznawane tak samo.
    """
    if not nazwa_procesu:
        return True  # nie znamy nazwy, więc na wszelki wypadek nie ruszamy

    rdzen = os.path.splitext(nazwa_procesu.lower())[0]
    return rdzen in PROCESY_CHRONIONE_RDZENIE


# ---------------------------------------------------------------
# Wczytywanie i zapis plików JSON
# ---------------------------------------------------------------

def _wczytaj_json(sciezka, opis):
    """
    Wczytuje mapowanie nazwa -> ścieżka z pliku JSON.

    Klucze zaczynające się od podkreślnika pomijamy — JSON nie ma składni
    komentarzy, więc używamy ich jako notatek w pliku konfiguracyjnym.

    Zwraca: słownik {nazwa_małymi_literami: ścieżka}. Pusty słownik przy błędzie.
    """
    if not os.path.exists(sciezka):
        return {}

    try:
        with open(sciezka, encoding="utf-8") as plik:
            dane = json.load(plik)
    except json.JSONDecodeError as e:
        # Najczęstsza przyczyna: pojedyncze ukośniki w ścieżce albo przecinek
        # po ostatnim wpisie. Mówimy o tym wprost, zamiast wysypywać program.
        logger.error("Plik %s ma błąd składni: %s", opis, e)
        return {}

    return {
        nazwa.lower(): sciezka_exe
        for nazwa, sciezka_exe in dane.items()
        if not nazwa.startswith("_")
    }


def wczytaj_config():
    """Wczytuje ręczne wpisy użytkownika z apps_config.json."""
    if not os.path.exists(SCIEZKA_CONFIGU):
        logger.error("Nie znalazłem pliku %s", SCIEZKA_CONFIGU)
        return {}
    return _wczytaj_json(SCIEZKA_CONFIGU, "apps_config.json")


def wczytaj_cache():
    """Wczytuje automatycznie znalezione aplikacje z apps_cache.json."""
    return _wczytaj_json(SCIEZKA_CACHE, "apps_cache.json")


def zapisz_do_cache(nazwa, sciezka_exe):
    """
    Dopisuje aplikację do cache, nie dublując wpisów.

    Klucze trzymamy zawsze małymi literami, więc "Chrome" i "chrome" to dla nas
    jeden i ten sam wpis. Dodatkowo przed zapisem usuwamy każdy istniejący klucz,
    który po zmniejszeniu liter wygląda tak samo — na wypadek gdybyś ręcznie
    wpisał coś dużymi literami do pliku cache.
    """
    klucz = nazwa.lower()

    # Czytamy surowy plik (bez filtrowania kluczy z podkreślnikiem),
    # żeby zapis nie skasował ewentualnych notatek.
    dane = {}
    if os.path.exists(SCIEZKA_CACHE):
        try:
            with open(SCIEZKA_CACHE, encoding="utf-8") as plik:
                dane = json.load(plik)
        except json.JSONDecodeError:
            logger.warning("Cache był uszkodzony — tworzę go od nowa.")
            dane = {}

    # Usuwamy duplikaty różniące się tylko wielkością liter.
    for istniejacy in [k for k in dane if k.lower() == klucz and k != klucz]:
        logger.info("Nadpisuję istniejący wpis w cache: %r", istniejacy)
        del dane[istniejacy]

    dane[klucz] = sciezka_exe

    try:
        with open(SCIEZKA_CACHE, "w", encoding="utf-8") as plik:
            json.dump(dane, plik, indent=2, ensure_ascii=False)
        logger.info("Zapisałem do cache: %s -> %s", klucz, sciezka_exe)
    except OSError as e:
        # Brak zapisu do cache nie jest powodem, żeby nie uruchomić aplikacji —
        # po prostu następnym razem znowu przeszukamy Menu Start.
        logger.error("Nie udało się zapisać cache: %s", e)


def usun_z_cache(nazwa):
    """Usuwa nieaktualny wpis (np. po odinstalowaniu aplikacji)."""
    klucz = nazwa.lower()

    if not os.path.exists(SCIEZKA_CACHE):
        return

    try:
        with open(SCIEZKA_CACHE, encoding="utf-8") as plik:
            dane = json.load(plik)
    except json.JSONDecodeError:
        return

    usuniete = [k for k in dane if k.lower() == klucz]
    if not usuniete:
        return

    for k in usuniete:
        del dane[k]

    try:
        with open(SCIEZKA_CACHE, "w", encoding="utf-8") as plik:
            json.dump(dane, plik, indent=2, ensure_ascii=False)
        logger.info("Usunąłem nieaktualny wpis z cache: %s", klucz)
    except OSError as e:
        logger.error("Nie udało się zaktualizować cache: %s", e)


# ---------------------------------------------------------------
# Przeszukiwanie Menu Start
# ---------------------------------------------------------------

def zbierz_skroty():
    """
    Przechodzi rekurencyjnie oba foldery Menu Start i zbiera pliki .lnk.

    NIE odczytujemy tu jeszcze, dokąd prowadzą — samo czytanie skrótu wymaga
    wywołania COM-a, co przy kilkuset plikach trwałoby zauważalnie długo.
    Najpierw dopasowujemy po NAZWACH, a rozwiązujemy dopiero zwycięzcę.

    Zwraca: słownik {nazwa_skrótu_małymi_literami: pełna ścieżka do .lnk}.
    """
    skroty = {}

    for folder in FOLDERY_MENU_START:
        if not folder or not os.path.isdir(folder):
            continue

        for katalog, _podkatalogi, pliki in os.walk(folder):
            for plik in pliki:
                if not plik.lower().endswith(".lnk"):
                    continue
                nazwa = os.path.splitext(plik)[0].lower()
                # Skróty o tej samej nazwie w obu drzewach: pierwszy wygrywa.
                skroty.setdefault(nazwa, os.path.join(katalog, plik))

    logger.info("Menu Start: znalazłem %d skrótów.", len(skroty))
    return skroty


def _cel_skrotu(sciezka_lnk):
    """
    Odczytuje, na jaki plik .exe wskazuje skrót .lnk.

    Robi to przez COM (WScript.Shell) z biblioteki pywin32 — to ten sam
    mechanizm, którego używa Windows, gdy klikasz skrót.

    UWAGA NA WĄTKI: COM trzeba zainicjalizować OSOBNO w każdym wątku, który
    go używa. Ten moduł jest wołany z wątku nasłuchu, a nie z głównego,
    więc bez CoInitialize() dostalibyśmy błąd "CoInitialize has not been called".
    Inicjalizacja jest lekka i można ją bezpiecznie powtarzać.

    Zwraca: ścieżkę do celu albo None, jeśli nie da się jej odczytać.
    """
    try:
        pythoncom.CoInitialize()
        powloka = win32com.client.Dispatch("WScript.Shell")
        skrot = powloka.CreateShortCut(sciezka_lnk)
        cel = skrot.Targetpath
    except Exception as e:
        # Skróty do aplikacji ze Sklepu Microsoft, do stron WWW albo do apletów
        # panelu sterowania nie mają zwykłego celu — to normalne, nie awaria.
        logger.debug("Nie odczytałem skrótu %s: %s", sciezka_lnk, e)
        return None

    if not cel or not cel.lower().endswith(".exe") or not os.path.exists(cel):
        return None

    return cel


def szukaj_w_menu_start(nazwa):
    """
    Szuka aplikacji w Menu Start i zwraca ścieżkę do jej pliku .exe.

    Dopasowanie idzie w trzech krokach, od najpewniejszego do najluźniejszego:

      1. TRAFIENIE DOKŁADNE — nazwa skrótu jest dokładnie taka, jak podana.

      2. ZAWIERANIE — nazwa skrótu zawiera podane słowo, np. "chrome"
         pasuje do "google chrome". Wybieramy najkrótszą taką nazwę, bo jest
         najbardziej konkretna ("word" -> "Word", a nie "Word Viewer Setup").
         Ten krok jest tu niezbędny: samo difflib by tego nie znalazło, bo
         podobieństwo "word" do "microsoft word" wynosi tylko 0.44 — poniżej progu.

      3. DOPASOWANIE PRZYBLIŻONE (difflib.get_close_matches) — porównuje ciągi
         znaków i zwraca te najbardziej podobne. Liczy tzw. współczynnik
         podobieństwa: 1.0 to identyczne teksty, 0.0 to zupełnie różne.
         Dzięki temu literówka albo przekręcenie przez Whispera ("spotifaj")
         nadal trafi we właściwą aplikację. Poniżej PROG_PODOBIENSTWA
         wolimy zwrócić None, niż otworzyć losowy program.

    Zwraca: (nazwa_skrótu, ścieżka_do_exe) albo (None, None).
    """
    skroty = zbierz_skroty()
    if not skroty:
        return None, None

    szukane = nazwa.lower().strip()
    kandydaci = []

    # Krok 1: trafienie dokładne.
    if szukane in skroty:
        kandydaci.append(szukane)

    # Krok 2: nazwa skrótu zawiera szukane słowo — od najkrótszej nazwy.
    zawierajace = sorted(
        (k for k in skroty if szukane in k and k not in kandydaci),
        key=len,
    )
    kandydaci.extend(zawierajace[:3])

    # Krok 3: dopasowanie przybliżone.
    for podobny in difflib.get_close_matches(
        szukane, list(skroty), n=3, cutoff=PROG_PODOBIENSTWA
    ):
        if podobny not in kandydaci:
            kandydaci.append(podobny)

    if not kandydaci:
        logger.info("Menu Start: nic nie pasuje do %r (próg %.2f).",
                    szukane, PROG_PODOBIENSTWA)
        return None, None

    # Rozwiązujemy skróty dopiero teraz i tylko dla kilku najlepszych kandydatów.
    # Pierwszy, który wskazuje na istniejący .exe, wygrywa — część skrótów
    # prowadzi do instalatorów, dokumentacji albo aplikacji ze Sklepu,
    # których nie da się uruchomić tą drogą.
    for kandydat in kandydaci:
        cel = _cel_skrotu(skroty[kandydat])
        if cel:
            logger.info("Menu Start: %r -> %r (%s)", szukane, kandydat, cel)
            return kandydat, cel

    logger.info("Menu Start: kandydaci %s nie prowadzą do pliku .exe.", kandydaci)
    return None, None


# ---------------------------------------------------------------
# Główne wejście modułu
# ---------------------------------------------------------------

# Procesy uruchomione przez Jarvisa w TEJ sesji: {nazwa_jaką_powiedziałeś: PID}.
# Trzymamy to tylko w pamięci, celowo nie zapisujemy na dysk — po restarcie
# programu PID-y i tak byłyby bezwartościowe, bo system nadaje je od nowa
# i ten sam numer mógłby wskazywać na zupełnie inny proces.
_procesy_sesji = {}


def _uruchom(sciezka_exe, nazwa):
    """
    Odpala program spod podanej ścieżki i zapamiętuje jego PID.

    Zwraca: (komunikat, czy się udało).
    """
    try:
        # Popen uruchamia program i NIE czeka na jego zakończenie — Jarvis od razu
        # wraca do nasłuchu. subprocess.run() zablokowałby program do zamknięcia okna.
        proces = subprocess.Popen([sciezka_exe])
    except OSError as e:
        logger.error("Nie udało się uruchomić %s: %s", sciezka_exe, e)
        return f"Nie udało się uruchomić '{nazwa}': {e}", False

    # Zapamiętujemy PID, żeby późniejsze "zamknij to" trafiło dokładnie w ten
    # proces, bez zgadywania po nazwie.
    _procesy_sesji[nazwa.lower().strip()] = proces.pid
    logger.info("Uruchomiłem %s (PID %d)", sciezka_exe, proces.pid)

    return f"Otwieram {nazwa}.", True


def otworz_aplikacje(nazwa):
    """
    GŁÓWNE WEJŚCIE TEGO MODUŁU — to woła main.py.

    nazwa — to, co powiedziałeś (np. "chrome"), niekoniecznie nazwa pliku

    Zwraca: (komunikat dla użytkownika, czy się udało).
    Flaga sukcesu jest potrzebna GUI — decyduje, czy koło wróci spokojnie
    do idle, czy błyśnie na czerwono.
    """
    if not nazwa:
        return "Nie wiem, którą aplikację mam otworzyć.", False

    klucz = nazwa.lower().strip()

    # --- 1. Ręczna konfiguracja (najwyższy priorytet) ---
    sciezka = wczytaj_config().get(klucz)
    if sciezka:
        if os.path.exists(sciezka):
            logger.info("Znalazłem %r w apps_config.json.", klucz)
            return _uruchom(sciezka, nazwa)

        # Wpis jest Twój, więc go nie ruszamy — tylko mówimy, że jest nieaktualny.
        logger.warning("Wpis w apps_config.json wskazuje na nieistniejący plik: %s", sciezka)
        return (
            f"Ścieżka do '{nazwa}' w apps_config.json jest nieaktualna: {sciezka}",
            False,
        )

    # --- 2. Cache znalezionych wcześniej aplikacji ---
    sciezka = wczytaj_cache().get(klucz)
    if sciezka:
        # Ścieżkę z cache sprawdzamy PRZY KAŻDYM UŻYCIU, a nie tylko przy zapisie.
        # Cache to zdjęcie dysku sprzed tygodni: w międzyczasie mogłeś aplikację
        # odinstalować, przenieść, albo instalator mógł ją zaktualizować do nowego
        # katalogu z numerem wersji w nazwie. Uruchamianie w ciemno skończyłoby się
        # błędem systemu; my zamiast tego kasujemy zwietrzały wpis i szukamy od nowa,
        # dzięki czemu Jarvis sam się naprawia zamiast powtarzać ten sam błąd.
        if os.path.exists(sciezka):
            logger.info("Znalazłem %r w cache.", klucz)
            return _uruchom(sciezka, nazwa)

        logger.info("Wpis w cache jest nieaktualny (%s) — szukam ponownie.", sciezka)
        usun_z_cache(klucz)

    # --- 3. Przeszukanie Menu Start ---
    logger.info("Szukam %r w Menu Start...", klucz)
    znaleziona_nazwa, sciezka = szukaj_w_menu_start(klucz)

    if sciezka is None:
        return f"Nie znalazłem aplikacji o nazwie: {nazwa}", False

    # Zapisujemy pod nazwą, którą POWIEDZIAŁEŚ, a nie pod nazwą skrótu —
    # następnym razem powiesz zapewne tak samo, więc trafimy prosto w cache.
    zapisz_do_cache(klucz, sciezka)

    komunikat, sukces = _uruchom(sciezka, nazwa)

    # Jeśli nazwa skrótu różni się od tego, co powiedziałeś, warto to pokazać
    # — od razu widzisz, czy dopasowanie przybliżone trafiło we właściwy program.
    if sukces and znaleziona_nazwa and znaleziona_nazwa != klucz:
        komunikat = f"Otwieram {znaleziona_nazwa}."

    return komunikat, sukces


# ---------------------------------------------------------------
# Zamykanie aplikacji
# ---------------------------------------------------------------

def _ubij_procesy(procesy, etykieta):
    """
    Zamyka podane procesy: najpierw grzecznie, potem siłą.

    RÓŻNICA MIĘDZY terminate() A kill()
    ===================================

    W założeniu (i tak to działa na Linuksie i macOS):
      - terminate() wysyła sygnał SIGTERM, czyli PROŚBĘ o zakończenie.
        Program może ją przechwycić, zapisać pliki, zamknąć połączenia i wyjść
        po swojemu. To wersja "zamknij się, proszę".
      - kill() wysyła SIGKILL, którego przechwycić się NIE DA. System po prostu
        usuwa proces z pamięci. Niezapisana praca przepada. To wersja
        "wyciągnij wtyczkę".

    Stąd wzorzec: najpierw terminate(), dajemy 3 sekundy na uprzątnięcie,
    a kill() zostawiamy dla programów, które się zawiesiły i nie reagują.

    UCZCIWA UWAGA O WINDOWS: tutaj ta różnica jest tylko teoretyczna.
    W dokumentacji psutil stoi wprost, że na Windows terminate() to alias
    dla kill() — oba wołają TerminateProcess, który jest natychmiastowy
    i nieprzechwytywalny. Prawdziwie grzeczne zamknięcie wymagałoby wysłania
    komunikatu WM_CLOSE do okna aplikacji, czego psutil nie robi.

    Zostawiam ten dwuetapowy schemat mimo wszystko: nic nie kosztuje, jest
    poprawny na innych systemach, a gdyby psutil kiedyś dorzucił na Windows
    łagodniejszą ścieżkę, kod skorzysta z niej bez zmian. Ale wiedz, że
    DZIŚ na Twoim komputerze zamknięcie aplikacji przez Jarvisa jest twarde
    — nie licz na to, że program zdąży zapytać "czy zapisać zmiany?".

    Zwraca: (komunikat, czy się udało).
    """
    do_ubicia = []

    for proces in procesy:
        try:
            # OSTATNIA BARIERA. Nazwę sprawdzamy jeszcze raz, tuż przed ubiciem,
            # już po całym dopasowywaniu. Gdyby jakakolwiek ścieżka w kodzie
            # ominęła wcześniejsze kontrole, ta jedna i tak zatrzyma zamach
            # na proces systemowy.
            if _chroniony(proces.name()):
                logger.error("ZABLOKOWANO próbę zamknięcia chronionego procesu: %s",
                             proces.name())
                continue
            do_ubicia.append(proces)
        except psutil.NoSuchProcess:
            continue

    if not do_ubicia:
        return f"Nie mogę zamknąć '{etykieta}' — to proces chroniony.", False

    for proces in do_ubicia:
        try:
            proces.terminate()
        except psutil.NoSuchProcess:
            pass  # sam się zakończył w międzyczasie — dobrze
        except psutil.AccessDenied:
            logger.warning("Brak uprawnień do zamknięcia PID %d", proces.pid)

    # wait_procs czeka na wszystkie naraz i zwraca (zakończone, wciąż żywe).
    _zakonczone, zywe = psutil.wait_procs(do_ubicia, timeout=3)

    for proces in zywe:
        logger.warning("PID %d nie zareagował w 3 s — używam kill().", proces.pid)
        try:
            proces.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            logger.error("Brak uprawnień do ubicia PID %d", proces.pid)
            return f"Nie mam uprawnień, żeby zamknąć '{etykieta}'.", False

    logger.info("Zamknąłem %s (%d proces(ów))", etykieta, len(do_ubicia))
    return f"Zamykam {etykieta}.", True


def _moje_procesy():
    """
    Zwraca procesy działające na TWOIM koncie, pogrupowane po nazwie.

    Pomijamy procesy innych użytkowników i konta SYSTEM — to warstwa 2
    zabezpieczeń. Aplikacje, które sam uruchamiasz, działają na Twoim koncie,
    więc nic użytecznego przez to nie tracimy, a odcinamy całą klasę
    procesów systemowych, których Jarvis nie ma powodu dotykać.

    Zwraca: słownik {nazwa_bez_rozszerzenia: [obiekty Process]}.
    """
    try:
        ja = psutil.Process().username()
    except psutil.Error:
        ja = None

    moj_pid = os.getpid()
    pogrupowane = {}

    for proces in psutil.process_iter(["pid", "name", "username"]):
        try:
            info = proces.info
            nazwa = info.get("name")
            if not nazwa or info.get("pid") == moj_pid:
                continue
            if ja is not None and info.get("username") != ja:
                continue

            klucz = os.path.splitext(nazwa)[0].lower()
            pogrupowane.setdefault(klucz, []).append(proces)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            # Proces zniknął między wylistowaniem a odczytem — normalne.
            continue

    return pogrupowane


def _znajdz_dzialajacy(szukane):
    """
    Dopasowuje wypowiedzianą nazwę do jednego z działających procesów.

    Kolejność jak przy szukaniu w Menu Start: trafienie dokładne, potem
    zawieranie, na końcu difflib. Różnica jest w progu — przy zamykaniu
    wymagamy większej pewności (patrz PROG_PODOBIENSTWA_ZAMYKANIE).

    Procesów chronionych NIE odsiewamy na tym etapie celowo: chcemy je
    rozpoznać, żeby powiedzieć wprost "odmawiam", zamiast wprowadzać Cię
    w błąd komunikatem "nie znalazłem takiej aplikacji".

    Zwraca: (nazwa_procesu, [procesy]) albo (None, []).
    """
    procesy = _moje_procesy()
    if not procesy:
        return None, []

    if szukane in procesy:
        return szukane, procesy[szukane]

    # Nazwa procesu zaczyna się od szukanego słowa, ale MUSI się na nim kończyć
    # albo mieć po nim separator — od najkrótszej, czyli najbardziej konkretnej
    # ("league of legends" przed "league of legends helper").
    #
    # Przy otwieraniu wystarczało zwykłe zawieranie ("chrome" w "google chrome"),
    # ale przy zamykaniu jest za luźne: "host" pasowałoby do kilkunastu procesów
    # naraz. Samo dopasowanie od początku nazwy też nie wystarcza — "system"
    # trafiałoby wtedy w "systemsettings", czyli w aplikację Ustawień.
    # Wymóg separatora znaczy, że szukane słowo musi być OSOBNYM członem nazwy.
    granice = (" ", "-", "_", ".")
    zaczynajace = sorted(
        (k for k in procesy if any(k.startswith(szukane + z) for z in granice)),
        key=len,
    )
    if zaczynajace:
        return zaczynajace[0], procesy[zaczynajace[0]]

    podobne = difflib.get_close_matches(
        szukane, list(procesy), n=1, cutoff=PROG_PODOBIENSTWA_ZAMYKANIE
    )
    if podobne:
        return podobne[0], procesy[podobne[0]]

    return None, []


def zamknij_aplikacje(nazwa):
    """
    DRUGIE GŁÓWNE WEJŚCIE TEGO MODUŁU — to woła main.py przy akcji close_app.

    nazwa — to, co powiedziałeś (np. "league of legends")

    Szuka w dwóch miejscach, w tej kolejności:
      1. Procesy uruchomione przez Jarvisa w tej sesji — znamy dokładny PID,
         więc nie ma mowy o pomyłce.
      2. Wszystkie Twoje działające procesy, dopasowane po nazwie.

    Zwraca: (komunikat dla użytkownika, czy się udało).
    """
    if not nazwa:
        return "Nie wiem, którą aplikację mam zamknąć.", False

    klucz = nazwa.lower().strip()

    # --- 1. Proces uruchomiony przez Jarvisa (najpewniejsza ścieżka) ---
    pid = _procesy_sesji.get(klucz)
    if pid is not None:
        try:
            proces = psutil.Process(pid)
            if proces.is_running():
                logger.info("Zamykam proces z tej sesji: %s (PID %d)", klucz, pid)
                komunikat, sukces = _ubij_procesy([proces], nazwa)
                if sukces:
                    del _procesy_sesji[klucz]
                return komunikat, sukces
        except psutil.NoSuchProcess:
            pass

        # Proces już nie żyje — czyścimy wpis i szukamy dalej normalną drogą.
        # PID-y są przez system nadawane ponownie, więc trzymanie martwego
        # numeru groziłoby trafieniem w przypadkowy, nowy proces.
        logger.info("Proces %r z tej sesji już nie działa — czyszczę wpis.", klucz)
        del _procesy_sesji[klucz]

    # --- 2. Szukanie wśród działających procesów ---
    nazwa_procesu, procesy = _znajdz_dzialajacy(klucz)

    if not procesy:
        return f"Nie znalazłem uruchomionej aplikacji: {nazwa}", False

    # Odmowa jest tu świadomie GŁOŚNA — chcesz wiedzieć, że Jarvis rozpoznał
    # komendę i celowo jej nie wykonał, a nie że jej nie zrozumiał.
    if _chroniony(nazwa_procesu):
        logger.warning("Odmawiam zamknięcia chronionego procesu: %s", nazwa_procesu)
        return (
            f"Nie zamknę '{nazwa_procesu}' — to proces systemowy Windows "
            "i jest na liście chronionych.",
            False,
        )

    logger.info("Dopasowałem %r do działającego procesu %r (%d proces(ów))",
                klucz, nazwa_procesu, len(procesy))

    return _ubij_procesy(procesy, nazwa_procesu)


# --- Test samego launchera: `python app_launcher.py` ---
if __name__ == "__main__":
    import sys

    from logging_setup import skonfiguruj_logowanie

    skonfiguruj_logowanie()

    logger.info("Konfiguracja ręczna: %s", SCIEZKA_CONFIGU)
    logger.info("Cache automatyczny: %s", SCIEZKA_CACHE)
    logger.info("Wpisy ręczne: %s", list(wczytaj_config().keys()) or "(brak)")
    logger.info("Wpisy w cache: %s", list(wczytaj_cache().keys()) or "(brak)")

    # Podaj nazwę w argumencie, żeby naprawdę uruchomić aplikację:
    #     python app_launcher.py chrome
    # Bez argumentu robimy tylko SUCHY TEST dopasowania — pokazujemy,
    # co Jarvis by znalazł, ale niczego nie uruchamiamy.
    if len(sys.argv) > 1:
        komunikat, sukces = otworz_aplikacje(" ".join(sys.argv[1:]))
        logger.info("[%s] %s", "OK" if sukces else "BŁĄD", komunikat)
    else:
        logger.info("--- suchy test dopasowania (nic nie uruchamiam) ---")
        for probka in ("chrome", "notatnik", "kalkulator", "spotify", "czegos_takiego_nie_ma"):
            nazwa_skrotu, cel = szukaj_w_menu_start(probka)
            logger.info("%-22s -> %s", probka, cel or "(nie znaleziono)")
