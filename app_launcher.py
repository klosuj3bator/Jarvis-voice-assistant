"""
app_launcher.py — warstwa "rąk" Jarvisa do uruchamiania programów.

Czyta mapowanie nazwa -> ścieżka do .exe z pliku apps_config.json
i uruchamia wskazaną aplikację.

Konfiguracja siedzi w osobnym pliku JSON, a nie w kodzie, żebyś mógł dopisywać
swoje programy bez dotykania Pythona — i żeby ścieżki specyficzne dla Twojego
komputera nie mieszały się z logiką programu.
"""

import json
import os
import subprocess

# Ścieżka liczona względem tego pliku, nie względem katalogu, z którego uruchomiono
# program — dzięki temu `python main.py` zadziała niezależnie od tego, gdzie jesteś.
SCIEZKA_CONFIGU = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apps_config.json")


def wczytaj_config():
    """
    Wczytuje mapowanie aplikacji z apps_config.json.

    Klucze zaczynające się od podkreślnika pomijamy — JSON nie ma składni
    komentarzy, więc używamy ich jako notatek w pliku konfiguracyjnym.

    Zwraca: słownik {nazwa_małymi_literami: ścieżka}. Pusty słownik przy błędzie.
    """
    if not os.path.exists(SCIEZKA_CONFIGU):
        print(f"[LAUNCHER] Nie znalazłem pliku {SCIEZKA_CONFIGU}")
        return {}

    try:
        with open(SCIEZKA_CONFIGU, encoding="utf-8") as plik:
            dane = json.load(plik)
    except json.JSONDecodeError as e:
        # Najczęstsza przyczyna: pojedyncze ukośniki w ścieżce albo przecinek
        # po ostatnim wpisie. Mówimy o tym wprost, zamiast wysypywać program.
        print(f"[LAUNCHER] Plik apps_config.json ma błąd składni: {e}")
        return {}

    return {
        nazwa.lower(): sciezka
        for nazwa, sciezka in dane.items()
        if not nazwa.startswith("_")
    }


def otworz_aplikacje(nazwa):
    """
    Uruchamia aplikację o podanej nazwie.

    nazwa — to, co powiedziałeś (np. "kalkulator"), niekoniecznie nazwa pliku

    Zwraca: (komunikat dla użytkownika, czy się udało).
    Flaga sukcesu jest potrzebna GUI — decyduje, czy koło wróci spokojnie
    do idle, czy błyśnie na czerwono.
    """
    if not nazwa:
        return "Nie wiem, którą aplikację mam otworzyć.", False

    aplikacje = wczytaj_config()
    sciezka = aplikacje.get(nazwa.lower())

    # Aplikacji nie ma w konfiguracji — wypisujemy, co Jarvis w ogóle zna,
    # żebyś od razu wiedział, czym dysponujesz i co dopisać.
    if sciezka is None:
        if aplikacje:
            znane = ", ".join(sorted(aplikacje.keys()))
            return (
                f"Nie mam aplikacji '{nazwa}' w konfiguracji. "
                f"Znam: {znane}. Dopisz ją do apps_config.json.",
                False,
            )
        return (
            f"Nie mam aplikacji '{nazwa}' w konfiguracji — "
            "plik apps_config.json jest pusty albo nie dało się go wczytać.",
            False,
        )

    # Ścieżka jest w configu, ale plik nie istnieje — typowe po przeniesieniu
    # programu albo po literówce w ścieżce. Lepiej powiedzieć to wprost.
    if not os.path.exists(sciezka):
        return (
            f"Ścieżka do '{nazwa}' jest w konfiguracji, ale plik nie istnieje: {sciezka}",
            False,
        )

    try:
        # Popen uruchamia program i NIE czeka na jego zakończenie — Jarvis od razu
        # wraca do nasłuchu. subprocess.run() zablokowałby program do zamknięcia okna.
        subprocess.Popen([sciezka])
    except OSError as e:
        return f"Nie udało się uruchomić '{nazwa}': {e}", False

    return f"Otwieram {nazwa}.", True


# --- Test samego launchera: `python app_launcher.py` ---
if __name__ == "__main__":
    print(f"Konfiguracja: {SCIEZKA_CONFIGU}")

    aplikacje = wczytaj_config()
    print(f"Znane aplikacje: {list(aplikacje.keys()) or '(brak)'}\n")

    for nazwa in ("kalkulator", "czegos_takiego_nie_ma"):
        komunikat, sukces = otworz_aplikacje(nazwa)
        print(f"[{'OK ' if sukces else 'BŁĄD'}] {komunikat}")
