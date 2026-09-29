"""
sprawdz_paczke.py — czy w zbudowanej paczce nie ma Twoich prywatnych plików.

Woła go instalator/zbuduj.py po każdym budowaniu; osobno: `python instalator/sprawdz_paczke.py`.

Dwa sprawdzenia, bo każde łapie co innego:

  1. NAZWY — .env, tokeny Spotify, memory.json, notatki, przypomnienia,
     dziennik, cache, modele. Gdyby któryś plik trafił do paczki pod swoją
     nazwą, wyjdzie tutaj.

  2. ZAWARTOŚĆ — bierzemy PRAWDZIWE wartości Twoich kluczy (z .env,
     z Ustawień, z tokenów Spotify) i szukamy ich we WSZYSTKICH plikach
     paczki, bajt po bajcie. To łapie też przypadki, których nie da się
     przewidzieć po nazwie — np. klucz, który trafił do jakiegoś pliku
     przez pomyłkę w kodzie.

Wartości kluczy nigdy nie są wypisywane — tylko nazwa klucza i plik.
"""

import fnmatch
import json
import os
import re
import sys

KOD = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
UZYTKOWNIK = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Jarvis")

ZAKAZANE_NAZWY = {
    ".env", "config.env", ".spotify_cache", ".spotify_cache_slownik", "memory.json",
    "pamiec.json", "slownik.json", "reminders.json", "email_watches.json",
    "apps_cache.json", "apps_config.json", "jarvis.log", "notatki", "nierozpoznane",
    ".obsidian", ".claudian", ".git", "modele",
}
ZAKAZANE_WZORCE = [
    "jarvis.log.*", "*.wav", "*.base",
    # modele mowy (pobiera je kreator, nie instalator)
    "model.bin", "vocabulary.txt", "hey_jarvis*", "melspectrogram.*",
    "embedding_model.*", "silero_vad.onnx", "*.tflite", "models--systran*",
]


def _sekrety():
    """Prawdziwe wartości, których w paczce być nie może: {opis: wartość}."""
    from dotenv import dotenv_values

    wynik = {}
    for plik, zrodlo in ((os.path.join(KOD, ".env"), ".env"),
                         (os.path.join(UZYTKOWNIK, "config.env"), "Ustawienia")):
        if os.path.exists(plik):
            for klucz, wartosc in dotenv_values(plik).items():
                if wartosc and len(wartosc) >= 8 and not wartosc.startswith("http://127.0.0.1"):
                    wynik[f"{klucz} ({zrodlo})"] = wartosc
    for plik in (".spotify_cache", ".spotify_cache_slownik"):
        for katalog in (KOD, UZYTKOWNIK):
            try:
                with open(os.path.join(katalog, plik), encoding="utf-8") as f:
                    token = json.load(f)
                for pole in ("access_token", "refresh_token"):
                    if token.get(pole):
                        wynik[f"{pole} z {plik}"] = token[pole]
            except (OSError, ValueError):
                pass
    for katalog in (KOD, UZYTKOWNIK):
        try:
            with open(os.path.join(katalog, "memory.json"), encoding="utf-8") as f:
                for i, fakt in enumerate(json.load(f).get("fakty", [])):
                    if len(fakt.get("fakt", "")) >= 12:
                        wynik[f"fakt nr {i + 1} z memory.json"] = fakt["fakt"]
        except (OSError, ValueError):
            pass
    return wynik


def _zawartosci(katalog_paczki):
    """
    (opis, bajty) każdego pliku paczki — ORAZ każdego modułu Pythona
    z archiwum wewnątrz Jarvis.exe. Tam PyInstaller wkłada skompilowany kod
    Jarvisa, spakowany zlibem, więc bez rozpakowania wartość z kodu byłaby
    dla zwykłego przeszukania niewidoczna.
    """
    for korzen, _, pliki in os.walk(katalog_paczki):
        for nazwa in pliki:
            sciezka = os.path.join(korzen, nazwa)
            opis = os.path.relpath(sciezka, katalog_paczki)
            with open(sciezka, "rb") as f:
                yield opis, f.read()
            if nazwa.lower().endswith(".exe"):
                yield from _moduly_z_exe(sciezka, opis)


def _moduly_z_exe(sciezka, opis):
    try:
        from PyInstaller.archive.readers import PKG_ITEM_PYZ, CArchiveReader
    except ImportError:
        return
    try:
        archiwum = CArchiveReader(sciezka)
    except Exception:
        return                      # zwykły .exe, nie z PyInstallera
    for nazwa, wpis in archiwum.toc.items():
        try:
            if wpis[-1] == PKG_ITEM_PYZ:     # archiwum modułów Pythona
                pyz = archiwum.open_embedded_archive(nazwa)
                for modul in pyz.toc:
                    dane = pyz.extract(modul, raw=True)
                    if dane:
                        yield f"{opis} → moduł {modul}", dane
            else:
                yield f"{opis} → {nazwa}", archiwum.extract(nazwa)
        except Exception as e:
            yield f"{opis} → {nazwa}", b""
            print(f"  uwaga: nie udało się zajrzeć do {nazwa} w {opis} ({type(e).__name__})")


def sprawdz(katalog_paczki):
    """Zwraca listę problemów (pusta = paczka czysta)."""
    problemy = []
    for korzen, katalogi, pliki in os.walk(katalog_paczki):
        for nazwa in katalogi + pliki:
            maly = nazwa.lower()
            if maly in ZAKAZANE_NAZWY or any(fnmatch.fnmatch(maly, w) for w in ZAKAZANE_WZORCE):
                problemy.append(f"zakazany plik: {os.path.relpath(os.path.join(korzen, nazwa), katalog_paczki)}")

    sekrety = _sekrety()
    if sekrety:
        # Jedno przejście po każdym pliku: wszystkie wartości naraz (w UTF-8
        # i w UTF-16, w którym Windows zapisuje zasoby plików .exe).
        igly = {}
        for opis, wartosc in sekrety.items():
            igly[wartosc.encode("utf-8")] = opis
            igly[wartosc.encode("utf-16-le")] = opis
        wzor = re.compile(b"|".join(re.escape(i) for i in sorted(igly, key=len, reverse=True)))
        for opis, dane in _zawartosci(katalog_paczki):
            trafienie = wzor.search(dane)
            if trafienie:
                problemy.append(f"w {opis} jest wartość: {igly[trafienie.group(0)]}")
    return problemy, len(sekrety)


if __name__ == "__main__":
    paczka = sys.argv[1] if len(sys.argv) > 1 else os.path.join(KOD, "dist", "Jarvis")
    problemy, ile = sprawdz(paczka)
    print(f"Sprawdzono {paczka} (szukanych prywatnych wartości: {ile}).")
    for p in problemy:
        print("  PROBLEM:", p)
    print("Paczka czysta." if not problemy else f"{len(problemy)} problem(ów)!")
    sys.exit(1 if problemy else 0)
