"""
atrapy.py — udawane usługi zewnętrzne dla testów (opis w conftest.py).

    KlientClaude   — zamiast biblioteki anthropic: odtwarza z góry ułożone
                     odpowiedzi modelu i zapisuje, co agent wysłał
    tura()         — jedna odpowiedź modelu: tekst i/lub wywołania narzędzi
    SkrzynkaGmail  — zamiast IMAP: maile w pamięci, filtrowane jak w Gmailu
    TelegramAPI    — zamiast api.telegram.org: zapisuje wysłane wiadomości
    OsadzaczSlow   — zamiast modelu embeddingów: wektor ze słów tekstu

Atrapy naśladują dokładnie te kawałki prawdziwych usług, z których
korzysta Jarvis — nic więcej.
"""

import copy
import re
import zlib
from types import SimpleNamespace as NS

import numpy as np

# ---------------------------------------------------------------
# Claude
# ---------------------------------------------------------------

ZUZYCIE = NS(input_tokens=10, output_tokens=5, cache_read_input_tokens=0,
             cache_creation_input_tokens=0, server_tool_use=None)


class Strumien:
    """Jedna odpowiedź modelu, strumieniowana jak w messages.stream()."""

    def __init__(self, zdarzenia, tresc, powod, przy_zdarzeniu=None):
        self._zdarzenia, self._przy = zdarzenia, przy_zdarzeniu
        self._final = NS(content=tresc, stop_reason=powod, usage=ZUZYCIE)
        self.current_message_snapshot = NS(usage=ZUZYCIE)

    def __enter__(self):
        return self

    def __exit__(self, *wyjatek):
        return False

    def __iter__(self):
        for numer, zdarzenie in enumerate(self._zdarzenia):
            if self._przy:
                self._przy(numer)      # np. "w trakcie trzeciego kawałka ktoś przerwał"
            yield zdarzenie

    def get_final_message(self):
        return self._final


def tura(*kawalki, narzedzia=(), powod=None, przy_zdarzeniu=None):
    """
    Odpowiedź modelu: kawałki tekstu (tak, jak przychodzą w strumieniu)
    i wywołania narzędzi [(id, nazwa, parametry), ...].
    """
    zdarzenia = [NS(type="text", text=k) for k in kawalki]
    tresc = [NS(type="text", text="".join(kawalki))] if kawalki else []
    tresc += [NS(type="tool_use", id=i, name=n, input=p) for i, n, p in narzedzia]
    powod = powod or ("tool_use" if narzedzia else "end_turn")
    return Strumien(zdarzenia, tresc, powod, przy_zdarzeniu)


class KlientClaude:
    """agent._klient na czas testu. Każde zapytanie zabiera następną turę z listy."""

    def __init__(self, *tury):
        self._tury = list(tury)
        self.zapytania = []          # pełne parametry każdego zapytania
        self.messages = self

    def stream(self, **parametry):
        self.zapytania.append(copy.deepcopy({k: v for k, v in parametry.items() if k != "tools"}))
        if not self._tury:
            raise AssertionError("Agent zapytał Claude więcej razy, niż przewidział test.")
        return self._tury.pop(0)


def przebieg(generator):
    """Wynik agent.odpowiedz(): (lista zdań, słownik stanu z końca)."""
    zdania, stan = [], {}
    for element in generator:
        if isinstance(element, dict):
            stan.update(element)
        else:
            zdania.append(element)
    return zdania, stan


# ---------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------

class SkrzynkaGmail:
    """
    Skrzynki kilku kont w pamięci. Podpina się pod email_monitor w miejsce
    logowania (_polacz) i wyszukiwania IMAP (_szukaj).

    Rozumie tę część składni Gmaila, którą buduje email_monitor._zapytanie:
    after:<czas>, from:(nadawca) i słowa — "(a OR b)" albo "a b" (wszystkie).
    """

    def __init__(self, **maile_kont):
        self.maile = maile_kont        # {"adres": [mail, ...]} albo {"adres": "BLAD"}
        self.zapytania = []

    def podepnij(self, monkeypatch, email_monitor):
        monkeypatch.setattr(email_monitor, "_polacz", self._polacz)
        monkeypatch.setattr(email_monitor, "_szukaj", self._szukaj)

    def _polacz(self, konto):
        if self.maile.get(konto["adres"]) == "BLAD":
            raise OSError("brak połączenia (atrapa)")
        return NS(adres=konto["adres"], logout=lambda: None)

    def _szukaj(self, imap, zapytanie, limit=10):
        self.zapytania.append((imap.adres, zapytanie))
        return [m for m in self.maile.get(imap.adres, []) if self._pasuje(m, zapytanie)][:limit]

    @staticmethod
    def _pasuje(mail, zapytanie):
        po = re.search(r"after:(\d+)", zapytanie)
        if po and mail["kiedy"] <= int(po.group(1)):
            return False
        nadawca = re.search(r"from:\(([^)]*)\)", zapytanie)
        if nadawca and nadawca.group(1).strip('"').lower() not in (
                mail["nadawca"] + " " + mail["adres"]).lower():
            return False
        reszta = re.sub(r"after:\d+|from:\([^)]*\)", "", zapytanie).strip()
        if not reszta:
            return True
        temat = mail["temat"].lower()
        if reszta.startswith("("):
            slowa = [s.strip('"') for s in reszta.strip("()").split(" OR ")]
            return any(s.lower() in temat for s in slowa)
        return all(s.strip('"').lower() in temat for s in re.findall(r'"[^"]+"|\S+', reszta))


# ---------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------

class TelegramAPI:
    """Zamiast MostTelegram._api — zapisuje wywołania, nic nie wysyła."""

    def __init__(self):
        self.wywolania = []

    def __call__(self, metoda, pliki=None, limit_s=15, **dane):
        self.wywolania.append((metoda, dane))
        return {"file_path": "x.ogg"} if metoda == "getFile" else {}

    def wyslane(self):
        return [dane.get("text") for metoda, dane in self.wywolania if metoda == "sendMessage"]


# ---------------------------------------------------------------
# Model embeddingów (indeks_notatek.py)
# ---------------------------------------------------------------

class OsadzaczSlow:
    """
    Zamiast modelu z Hugging Face: wektor to policzone słowa tekstu (każde
    słowo z 3+ liter trafia do jednej z 256 przegródek). Teksty ze wspólnymi
    słowami są więc "podobne" — dość, żeby sprawdzić indeks i wyszukiwanie,
    bez pobierania 128 MB i bez liczenia na procesorze.
    """

    def __init__(self):
        self.policzone = []          # wszystkie teksty, dla których liczono wektory

    def wektory(self, teksty):
        self.policzone += list(teksty)
        macierz = np.zeros((len(teksty), 256), dtype=np.float32)
        for numer, tekst in enumerate(teksty):
            # Przedrostki e5 ("query: ", "passage: ") nic nie znaczą — pomijamy.
            tekst = re.sub(r"^(query|passage): ", "", tekst)
            for slowo in re.findall(r"\w{3,}", tekst.lower()):
                macierz[numer, zlib.crc32(slowo.encode("utf-8")) % 256] += 1
        return macierz / np.maximum(np.linalg.norm(macierz, axis=1, keepdims=True), 1e-12)
