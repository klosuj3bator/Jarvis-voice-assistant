"""
system_control.py — sterowanie Windowsem: głośność, jasność ekranu,
blokada, uśpienie, restart i wyłączenie komputera.

Każda funkcja zwraca parę (komunikat, czy_się_udało) — tak jak
spotify_controller czy app_launcher. Komunikat trafia do modelu, a ten
dopiero układa z niego zdanie dla Ciebie.


CZEGO TEN MODUŁ NIE ROBI
========================

Nie pyta o potwierdzenie i nie mówi. Restart i wyłączenie są tu zwykłymi
funkcjami, które po prostu wykonują polecenie. O pytanie "czy na pewno?"
i odsłuchanie odpowiedzi dba main.py — i to z konkretnego powodu:

Narzędzia agenta wykonują się w wątku, który W TYM SAMYM CZASIE karmi
syntezator mowy zdaniami odpowiedzi. Gdyby moduł zaczął stąd mówić
i nasłuchiwać, jego pytanie nałożyłoby się na zdanie, które akurat leci
z głośników, a mikrofon nagrałby oba. Dlatego groźne polecenia są tylko
ZGŁASZANE podczas rozmowy, a wykonuje je main.py — po tym, jak Jarvis
skończy mówić (patrz POLECENIA_PO_ODPOWIEDZI niżej).


SKĄD SIĘ BIERZE STEROWANIE GŁOŚNOŚCIĄ
=====================================

Windows nie ma na to prostego polecenia. Głośność siedzi w systemowym
mikserze dostępnym przez COM — ten sam mechanizm, którym program obsługuje
np. Worda z zewnątrz. Biblioteka pycaw opakowuje go w normalne funkcje
Pythona, a my dokładamy tylko jedno: COM trzeba włączyć osobno w każdym
wątku, który go używa, a Jarvis woła to z wątku nasłuchu.
"""

import contextlib
import ctypes
import logging
import subprocess

logger = logging.getLogger(__name__)

# O ile procent zmienia się głośność i jasność na "głośniej" / "ciemniej".
KROK_GLOSNOSCI = 10
KROK_JASNOSCI = 10

# Ile sekund Windows odczeka przed restartem i wyłączeniem. To nie jest
# zwłoka dla ozdoby: przez ten czas da się wszystko odwołać poleceniem
# "shutdown /a" wpisanym w terminalu.
OPOZNIENIE_WYLACZENIA_S = 5

# Polecenia, które main.py wykonuje DOPIERO po wypowiedzeniu odpowiedzi.
# Gdyby komputer zasypiał w trakcie zdania, Jarvis urwałby je w pół słowa.
POLECENIA_PO_ODPOWIEDZI = {"zablokuj", "uspij", "restart", "wylacz"}

# Z tych main.py dodatkowo pyta na głos "czy na pewno?".
POLECENIA_DO_POTWIERDZENIA = {"restart", "wylacz"}


def _procent(wartosc, domyslna=None):
    """Sprowadza wartość do liczby całkowitej z zakresu 0-100."""
    if wartosc is None:
        return domyslna
    try:
        return max(0, min(100, int(round(float(wartosc)))))
    except (TypeError, ValueError):
        return domyslna


# ---------------------------------------------------------------
# Głośność
# ---------------------------------------------------------------

@contextlib.contextmanager
def _mikser():
    """
    Daje dostęp do głównego suwaka głośności Windows i sprząta po sobie.

    Importy są w środku, a nie na górze pliku, bo pycaw uruchamia przy nich
    COM — a Jarvis ma startować szybko i nie płacić za to, czego akurat
    nie używa.
    """
    import comtypes
    from pycaw.utils import AudioUtilities

    # CoInitialize = "w tym wątku będę używał COM". Bez tego wywołania
    # pycaw wywala się w wątku nasłuchu, choć w głównym działa.
    comtypes.CoInitialize()
    try:
        # EndpointVolume to gotowy uchwyt do suwaka głośności domyślnych
        # głośników — dokładnie tego, który widzisz na pasku zadań.
        yield AudioUtilities.GetSpeakers().EndpointVolume
    finally:
        comtypes.CoUninitialize()


def stan_glosnosci():
    """Zwraca (procent, czy_wyciszone) albo (None, None), gdy się nie udało."""
    try:
        with _mikser() as mikser:
            return (round(mikser.GetMasterVolumeLevelScalar() * 100),
                    bool(mikser.GetMute()))
    except Exception:
        logger.exception("Nie udało się odczytać głośności")
        return None, None


def ustaw_glosnosc(procent):
    """Ustawia głośność systemową na podaną wartość (0-100)."""
    docelowa = _procent(procent)
    if docelowa is None:
        return "Nie podano, na ile ustawić głośność.", False

    try:
        with _mikser() as mikser:
            # Skalarna wartość 0.0-1.0 odpowiada suwakowi głośności
            # tak, jak go widzisz na pasku zadań.
            mikser.SetMasterVolumeLevelScalar(docelowa / 100.0, None)
            # Ustawienie głośności przy wyciszonym dźwięku nic nie da —
            # więc przy okazji zdejmujemy wyciszenie.
            if mikser.GetMute():
                mikser.SetMute(0, None)
    except Exception as e:
        logger.exception("Nie udało się ustawić głośności")
        return f"Nie udało się ustawić głośności: {e}", False

    logger.info("Głośność: %d%%", docelowa)
    return f"Głośność ustawiona na {docelowa}%.", True


def zmien_glosnosc(o_ile):
    """Podgłaśnia (wartość dodatnia) albo przycisza (ujemna) o podane procenty."""
    try:
        krok = int(round(float(o_ile)))
    except (TypeError, ValueError):
        return "Nie wiem, o ile zmienić głośność.", False

    teraz, _ = stan_glosnosci()
    if teraz is None:
        return "Nie mam dostępu do głośności systemowej.", False

    return ustaw_glosnosc(teraz + krok)


def ustaw_wyciszenie(czy_wyciszyc):
    """Wycisza dźwięk albo przywraca go."""
    try:
        with _mikser() as mikser:
            mikser.SetMute(1 if czy_wyciszyc else 0, None)
    except Exception as e:
        logger.exception("Nie udało się zmienić wyciszenia")
        return f"Nie udało się zmienić wyciszenia: {e}", False

    logger.info("Wyciszenie: %s", "włączone" if czy_wyciszyc else "wyłączone")
    return ("Dźwięk wyciszony." if czy_wyciszyc else "Dźwięk przywrócony."), True


# ---------------------------------------------------------------
# Jasność ekranu
# ---------------------------------------------------------------
#
# Uwaga na sprzęt: w laptopie jasność ustawia się zawsze. Monitor zewnętrzny
# musi mieć włączone DDC/CI (bywa w menu monitora jako "DDC/CI" albo
# "sterowanie z komputera"). Jeśli go nie obsługuje, biblioteka zgłasza błąd,
# a my mówimy o tym wprost, zamiast udawać, że coś się stało.

def stan_jasnosci():
    """Zwraca jasność pierwszego ekranu w procentach albo None."""
    try:
        import screen_brightness_control as sbc

        odczyty = sbc.get_brightness()
        return odczyty[0] if odczyty else None
    except Exception:
        logger.exception("Nie udało się odczytać jasności")
        return None


def ustaw_jasnosc(procent):
    """Ustawia jasność ekranu (0-100)."""
    docelowa = _procent(procent)
    if docelowa is None:
        return "Nie podano, na ile ustawić jasność.", False

    try:
        import screen_brightness_control as sbc

        sbc.set_brightness(docelowa)
    except Exception as e:
        logger.exception("Nie udało się ustawić jasności")
        return (
            f"Nie udało się zmienić jasności: {e}. "
            "Monitor zewnętrzny musi mieć włączone DDC/CI.",
            False,
        )

    logger.info("Jasność: %d%%", docelowa)
    return f"Jasność ustawiona na {docelowa}%.", True


def zmien_jasnosc(o_ile):
    """Rozjaśnia (wartość dodatnia) albo przyciemnia (ujemna) ekran."""
    try:
        krok = int(round(float(o_ile)))
    except (TypeError, ValueError):
        return "Nie wiem, o ile zmienić jasność.", False

    teraz = stan_jasnosci()
    if teraz is None:
        return "Nie mam dostępu do jasności tego ekranu.", False

    return ustaw_jasnosc(teraz + krok)


# ---------------------------------------------------------------
# Zasilanie i sesja
# ---------------------------------------------------------------

def zablokuj():
    """Blokuje ekran (jak Windows+L). Programy działają dalej."""
    if not ctypes.windll.user32.LockWorkStation():
        return "Nie udało się zablokować ekranu.", False
    logger.info("Ekran zablokowany.")
    return "Blokuję ekran.", True


def uspij():
    """
    Usypia komputer.

    Pierwszy argument to "czy hibernować" — zero znaczy zwykłe uśpienie.
    """
    try:
        ctypes.windll.powrprof.SetSuspendState(0, 1, 0)
    except Exception as e:
        logger.exception("Nie udało się uśpić komputera")
        return f"Nie udało się uśpić komputera: {e}", False

    logger.info("Komputer uśpiony.")
    return "Usypiam komputer.", True


def _wylacz_polecenie(argument, opis):
    """
    Uruchamia systemowe polecenie shutdown.

    CREATE_NO_WINDOW jest tu istotne: bez niego Windows pokazałby na chwilę
    czarne okno konsoli, bo Jarvis działa bez własnej konsoli (Jarvis.pyw).
    """
    try:
        subprocess.run(
            ["shutdown", argument, "/t", str(OPOZNIENIE_WYLACZENIA_S)],
            check=True,
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as e:
        logger.exception("Nie udało się wykonać: shutdown %s", argument)
        return f"Nie udało się {opis}: {e}", False

    logger.warning("Zaplanowano: %s za %d s.", opis, OPOZNIENIE_WYLACZENIA_S)
    return f"{opis.capitalize()} za {OPOZNIENIE_WYLACZENIA_S} sekund.", True


def restart():
    """Restartuje komputer po krótkiej zwłoce."""
    return _wylacz_polecenie("/r", "restart komputera")


def wylacz():
    """Wyłącza komputer po krótkiej zwłoce."""
    return _wylacz_polecenie("/s", "wyłączenie komputera")


def odwolaj():
    """Odwołuje zaplanowany restart albo wyłączenie, jeśli zwłoka jeszcze trwa."""
    try:
        subprocess.run(["shutdown", "/a"], check=True, capture_output=True,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    except Exception:
        return "Nie było czego odwoływać.", False
    logger.info("Odwołano zaplanowane wyłączenie.")
    return "Odwołane.", True


# ---------------------------------------------------------------
# Jedno wejście dla całej reszty programu
# ---------------------------------------------------------------
#
# Nazwy poleceń są w JEDNYM miejscu — tutaj. Agent wypisuje je w opisie
# narzędzia, main.py sprawdza je przy potwierdzaniu, a testy po nich chodzą.

POLECENIA = {
    "glosnosc": lambda wartosc: ustaw_glosnosc(wartosc),
    "glosniej": lambda wartosc: zmien_glosnosc(wartosc or KROK_GLOSNOSCI),
    "ciszej": lambda wartosc: zmien_glosnosc(-(wartosc or KROK_GLOSNOSCI)),
    "wycisz": lambda wartosc: ustaw_wyciszenie(True),
    "przywroc_dzwiek": lambda wartosc: ustaw_wyciszenie(False),
    "jasnosc": lambda wartosc: ustaw_jasnosc(wartosc),
    "jasniej": lambda wartosc: zmien_jasnosc(wartosc or KROK_JASNOSCI),
    "ciemniej": lambda wartosc: zmien_jasnosc(-(wartosc or KROK_JASNOSCI)),
    "zablokuj": lambda wartosc: zablokuj(),
    "uspij": lambda wartosc: uspij(),
    "restart": lambda wartosc: restart(),
    "wylacz": lambda wartosc: wylacz(),
}


def wykonaj(polecenie, wartosc=None):
    """
    GŁÓWNE WEJŚCIE TEGO MODUŁU — wykonuje polecenie po nazwie.

    polecenie — jedna z nazw w POLECENIA
    wartosc   — procent (dla "glosnosc" i "jasnosc") albo o ile zmienić
                (dla "glosniej", "ciszej", "jasniej", "ciemniej")

    Zwraca: (komunikat, czy_się_udało).
    """
    funkcja = POLECENIA.get((polecenie or "").strip().lower())
    if funkcja is None:
        return (f"Nieznane polecenie systemowe: {polecenie!r}. "
                f"Dostępne: {', '.join(POLECENIA)}.", False)

    return funkcja(_procent(wartosc))


# --- Test: `python system_control.py` — sprawdza głośność i jasność ---
# Celowo NIE testuje blokady, uśpienia, restartu ani wyłączenia: to są
# polecenia, po których nie byłoby jak zobaczyć wyniku testu.
if __name__ == "__main__":
    import time

    from logging_setup import skonfiguruj_logowanie

    skonfiguruj_logowanie()

    glosnosc, wyciszone = stan_glosnosci()
    print(f"Głośność teraz: {glosnosc}% (wyciszenie: {wyciszone})")
    print(f"Jasność teraz: {stan_jasnosci()}%")

    print(wykonaj("glosnosc", 30))
    time.sleep(1)
    print(wykonaj("glosniej", 15))
    time.sleep(1)
    print(wykonaj("wycisz"))
    time.sleep(1)
    print(wykonaj("przywroc_dzwiek"))

    if glosnosc is not None:
        print("Przywracam poprzednią głośność:", wykonaj("glosnosc", glosnosc))
