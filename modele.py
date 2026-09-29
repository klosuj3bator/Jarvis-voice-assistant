"""
modele.py — modele mowy: gdzie leżą i jak je pobrać.

Jarvis potrzebuje dwóch rodzajów modeli, razem ok. 560 MB:

  Whisper (faster-whisper)  — "small" rozpoznaje komendy (~480 MB),
                              "tiny" potwierdza niepewne "Hey Jarvis" (~75 MB)
  openWakeWord              — detektor "Hey Jarvis" i jego dwa modele
                              pomocnicze, plus VAD (wykrywanie mowy), ~5 MB

Nie pakujemy ich do instalatora: byłby 5 razy większy, a modele i tak nie
zmieniają się między wersjami Jarvisa. Instalowana wersja pobiera je przy
pierwszym uruchomieniu do %APPDATA%\\Jarvis\\modele, z paskiem postępu
w kreatorze (setup_wizard.py).

Wersja uruchamiana z kodu działa jak dawniej: bierze modele z cache
Hugging Face i z katalogu biblioteki openWakeWord — chyba że w
%APPDATA%\\Jarvis\\modele leży komplet, wtedy korzysta z niego.


SKĄD POBIERAMY
==============

Whisper — z Hugging Face, te same repozytoria, których używa faster-whisper
(Systran/faster-whisper-*). Hugging Face podaje przy każdym dużym pliku jego
sumę SHA-256, więc po pobraniu sprawdzamy, czy plik dotarł cały i niezmieniony.

openWakeWord — z wydania v0.5.1 na GitHubie, tego samego, z którego pobiera
biblioteka. Tu sprawdzamy rozmiar pliku.

Każdy plik ląduje najpierw jako ".part" i dopiero po sprawdzeniu dostaje
właściwą nazwę — przerwane pobieranie nie zostawi uszkodzonego modelu.
"""

import hashlib
import logging
import os
import time

import requests

import sciezki

logger = logging.getLogger(__name__)

WHISPER = {"small": "Systran/faster-whisper-small", "tiny": "Systran/faster-whisper-tiny"}

# Te same pliki, które pobiera faster-whisper (faster_whisper/utils.py).
PLIKI_WHISPERA = ("config.json", "preprocessor_config.json", "model.bin", "tokenizer.json")
WYMAGANE_WHISPERA = ("config.json", "model.bin", "tokenizer.json")

_GITHUB = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
OPENWAKEWORD = ("hey_jarvis_v0.1.onnx", "melspectrogram.onnx", "embedding_model.onnx",
                "silero_vad.onnx")

PORCJA = 1024 * 1024


class BladPobierania(Exception):
    """Pobieranie się nie udało — komunikat nadaje się do pokazania człowiekowi."""


# ---------------------------------------------------------------
# Gdzie leżą
# ---------------------------------------------------------------

def katalog_whisper(nazwa):
    return os.path.join(sciezki.MODELE, f"whisper-{nazwa}")


def plik_openwakeword(nazwa):
    return os.path.join(sciezki.MODELE, "openwakeword", nazwa)


def _whisper_komplet(nazwa):
    katalog = katalog_whisper(nazwa)
    if not all(os.path.exists(os.path.join(katalog, p)) for p in WYMAGANE_WHISPERA):
        return False
    # Słownik tokenów (vocabulary.txt albo .json) schodzi ostatni — przerwane
    # pobieranie mogło zostawić wszystko poza nim.
    return any(p.startswith("vocabulary.") and not p.endswith(".part")
               for p in os.listdir(katalog))


def _openwakeword_komplet():
    return all(os.path.exists(plik_openwakeword(p)) for p in OPENWAKEWORD)


def gotowe():
    """Czy w %APPDATA%\\Jarvis\\modele leży komplet."""
    return all(_whisper_komplet(n) for n in WHISPER) and _openwakeword_komplet()


def potrzebne_pobranie():
    """Czy trzeba pobrać modele przed startem — tylko wersja z instalatora."""
    return sciezki.SPAKOWANY and not gotowe()


def whisper(nazwa):
    """
    Co podać do WhisperModel(): katalog z modelem albo samą nazwę ("small"),
    gdy działamy z kodu i model jest w cache Hugging Face.
    """
    if sciezki.SPAKOWANY or _whisper_komplet(nazwa):
        return katalog_whisper(nazwa)
    return nazwa


def openwakeword_z_appdata():
    """Czy detektor ma brać modele z %APPDATA% (zamiast z katalogu biblioteki)."""
    return sciezki.SPAKOWANY or _openwakeword_komplet()


# ---------------------------------------------------------------
# Pobieranie
# ---------------------------------------------------------------

def lista_do_pobrania():
    """
    Co jeszcze trzeba pobrać: lista (adres, plik_docelowy, rozmiar, sha256 albo None).
    Pyta Hugging Face o listę plików modeli — to kilka małych zapytań.
    """
    lista = []
    for nazwa, repo in WHISPER.items():
        if _whisper_komplet(nazwa):
            continue
        try:
            odp = requests.get(f"https://huggingface.co/api/models/{repo}/tree/main", timeout=20)
            odp.raise_for_status()
            pliki = odp.json()
        except (requests.RequestException, ValueError):
            raise BladPobierania("Nie mogę połączyć się z Hugging Face — sprawdź internet.") from None
        for plik in pliki:
            sciezka = plik.get("path", "")
            if plik.get("type") != "file" or not (sciezka in PLIKI_WHISPERA
                                                   or sciezka.startswith("vocabulary.")):
                continue
            lfs = plik.get("lfs") or {}
            lista.append((f"https://huggingface.co/{repo}/resolve/main/{sciezka}",
                          os.path.join(katalog_whisper(nazwa), sciezka),
                          lfs.get("size") or plik.get("size"), lfs.get("oid")))

    for nazwa in OPENWAKEWORD:
        if not os.path.exists(plik_openwakeword(nazwa)):
            lista.append((_GITHUB + nazwa, plik_openwakeword(nazwa), None, None))
    return lista


def _rozmiar(adres):
    """Rozmiar pliku z GitHuba — z nagłówka odpowiedzi, bez pobierania treści."""
    try:
        odp = requests.head(adres, allow_redirects=True, timeout=20)
        return int(odp.headers.get("Content-Length") or 0) or None
    except (requests.RequestException, ValueError):
        return None


def pobierz(postep=None, stop=None):
    """
    Pobiera brakujące modele do %APPDATA%\\Jarvis\\modele.

    postep — funkcja(pobrano_bajtow, razem_bajtow, opis) wołana w trakcie
    stop   — threading.Event; ustawiony przerywa pobieranie (np. zamknięte okno)

    Rzuca BladPobierania z komunikatem dla człowieka.
    """
    lista = lista_do_pobrania()
    lista = [(a, c, r if r else _rozmiar(a), s) for a, c, r, s in lista]
    razem = sum(r or 0 for _, _, r, _ in lista)
    pobrano = 0
    logger.info("[MODELE] Do pobrania: %d plików, %.0f MB.", len(lista), razem / 1e6)

    for adres, cel, rozmiar, sha in lista:
        opis = f"{os.path.basename(os.path.dirname(cel))}/{os.path.basename(cel)}"
        os.makedirs(os.path.dirname(cel), exist_ok=True)
        tymczasowy = cel + ".part"
        skrot = hashlib.sha256()
        ostatnio = 0.0
        try:
            with requests.get(adres, stream=True, timeout=30) as odp:
                odp.raise_for_status()
                with open(tymczasowy, "wb") as plik:
                    for porcja in odp.iter_content(PORCJA):
                        if stop is not None and stop.is_set():
                            raise BladPobierania("Pobieranie przerwane.")
                        plik.write(porcja)
                        skrot.update(porcja)
                        pobrano += len(porcja)
                        if postep and time.monotonic() - ostatnio > 0.1:
                            postep(pobrano, razem, opis)
                            ostatnio = time.monotonic()
        except BladPobierania:
            _usun(tymczasowy)
            raise
        except (requests.RequestException, OSError) as e:
            _usun(tymczasowy)
            logger.warning("[MODELE] %s: %s", opis, type(e).__name__)
            raise BladPobierania(f"Nie udało się pobrać {opis} — sprawdź internet "
                                 "i miejsce na dysku, potem spróbuj jeszcze raz.") from None

        wielkosc = os.path.getsize(tymczasowy)
        if (sha and skrot.hexdigest() != sha) or (rozmiar and wielkosc != rozmiar):
            _usun(tymczasowy)
            raise BladPobierania(f"Plik {opis} dotarł uszkodzony — spróbuj jeszcze raz.")
        os.replace(tymczasowy, cel)
        logger.info("[MODELE] Pobrano %s (%.1f MB).", opis, wielkosc / 1e6)

    if postep:
        postep(razem, razem, "gotowe")


def _usun(sciezka):
    try:
        os.remove(sciezka)
    except OSError:
        pass


# --- `python modele.py` — co jest, czego brakuje ---
if __name__ == "__main__":
    print(f"Katalog modeli: {sciezki.MODELE}")
    print(f"Komplet: {gotowe()}  (wersja z instalatora: {sciezki.SPAKOWANY})")
    for adres, cel, rozmiar, _ in lista_do_pobrania():
        print(f"  brakuje: {os.path.relpath(cel, sciezki.MODELE)}"
              f"{f'  ({rozmiar / 1e6:.0f} MB)' if rozmiar else ''}")
