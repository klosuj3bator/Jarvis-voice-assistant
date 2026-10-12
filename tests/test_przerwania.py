"""
Przerwania: wejście Jarvisowi w słowo (agent.py) i "stop" z Telegrama
(telegram_bridge.py). Claude i Telegram to atrapy.
"""

import threading
import time

import agent
import pamiec
import telegram_bridge
from atrapy import KlientClaude, TelegramAPI, przebieg, tura

ZNACZNIK = ("[PRZERWANE W TYM MIEJSCU: użytkownik wszedł mi w słowo — użytkownik "
            "usłyszał tylko to, co przed znacznikiem.]")


# --- oznacz_przerwane: w historii zostaje to, co naprawdę zabrzmiało ------

def test_pelna_odpowiedz_zastapiona_tym_co_padlo():
    historia = [{"role": "user", "content": "opowiedz"},
                {"role": "assistant", "content": [{"type": "text", "text": "Raz. Dwa trzy. Cztery."}]}]
    wynik = agent.oznacz_przerwane(historia, "Raz. Dwa")
    assert len(wynik) == 2
    assert wynik[-1] == {"role": "assistant", "content": f"Raz. Dwa {ZNACZNIK}"}


def test_tekst_sprzed_narzedzia_sie_nie_dubluje():
    historia = [
        {"role": "user", "content": "puść coś"},
        {"role": "assistant", "content": [{"type": "text", "text": "Sprawdzam."},
                                          {"type": "tool_use", "id": "t1", "name": "x", "input": {}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]},
    ]
    wynik = agent.oznacz_przerwane(historia, "Sprawdzam. Włączam album")
    assert len(wynik) == 4                                  # para narzędzia nietknięta
    assert wynik[-1]["content"] == f"Włączam album {ZNACZNIK}"


def test_przerwane_zanim_cokolwiek_padlo():
    wynik = agent.oznacz_przerwane([{"role": "user", "content": "hej"}], "", powod="użytkownik napisał stop")
    assert wynik[-1]["content"] == "[PRZERWANE: użytkownik napisał stop, zanim cokolwiek powiedziałem.]"


def test_przerwana_wypowiedz_przetrwa_zapis_rozmowy():
    pamiec.zapisz_rozmowe(agent.oznacz_przerwane([{"role": "user", "content": "opowiedz"}], "Dawno temu"))
    assert pamiec.poprzednia_rozmowa()[-1]["content"].startswith("Dawno temu [PRZERWANE W TYM MIEJSCU")


# --- niewypowiedziana reszta ("mów dalej") ------------------------------

def test_reszta_pokazywana_tylko_gdy_swieza_i_nie_z_poczty():
    teraz = time.monotonic()
    assert agent._notatka_o_reszcie(None) is None
    assert agent._notatka_o_reszcie({"tekst": "  ", "kiedy": teraz}) is None
    assert agent._notatka_o_reszcie({"tekst": "Mail mówi…", "z_poczty": True, "kiedy": teraz}) is None
    assert agent._notatka_o_reszcie({"tekst": "Coś.", "kiedy": teraz - agent.WAZNOSC_RESZTY_S - 1}) is None
    notatka = agent._notatka_o_reszcie({"tekst": "Dalszy ciąg.", "niepelna": True, "kiedy": teraz})
    assert notatka.startswith("[NIEWYPOWIEDZIANA RESZTA") and "urywa się" in notatka
    assert notatka.endswith("Dalszy ciąg.")


def test_mow_dalej_reszta_jedzie_w_zapytaniu_a_nie_w_historii():
    agent._klient = klient = KlientClaude(tura("Trzecia sprawa to budżet. "))
    historia = agent.oznacz_przerwane([{"role": "user", "content": "trzy sprawy"}],
                                      "Pierwsza to czas. Druga")
    _, stan = przebieg(agent.odpowiedz("mów dalej", historia, True, przerwana_reszta={
        "tekst": "to ludzie. Trzecia to budżet.", "kiedy": time.monotonic()}))
    wyslane = klient.zapytania[0]["messages"][-1]["content"]
    assert wyslane[0]["text"].startswith("[NIEWYPOWIEDZIANA RESZTA")
    assert wyslane[1]["text"] == '[po "Hey Jarvis"] mów dalej'
    assert stan["historia"][-2] == {"role": "user", "content": '[po "Hey Jarvis"] mów dalej'}
    assert "NIEWYPOWIEDZIANA" not in str(stan["historia"])


# --- przerwanie w trakcie generowania -----------------------------------

def test_przerwanie_w_trakcie_odpowiedzi():
    przerwanie = threading.Event()
    agent._klient = KlientClaude(tura("Zdanie jeden. Zdanie d", "wa ciągnie się", " dalej.",
                                      przy_zdarzeniu=lambda numer: numer == 2 and przerwanie.set()))
    zdania, stan = przebieg(agent.odpowiedz("mów", [], przerwanie=przerwanie))
    assert zdania == ["Zdanie jeden."]
    assert stan["przerwane"] is True
    assert stan["niedokonczone"] == "Zdanie dwa ciągnie się"
    # Niedokończonej wiadomości modelu nie ma w historii — dopisze ją oznacz_przerwane.
    assert stan["historia"][-1]["role"] == "user"


def test_przerwanie_w_trakcie_narzedzia_nie_konczy_polecenia_systemowego(monkeypatch):
    przerwanie = threading.Event()

    def narzedzie(nazwa, parametry):
        przerwanie.set()               # wszedłeś w słowo, gdy narzędzie jeszcze działało
        return "Zablokuję po odpowiedzi.", True

    monkeypatch.setattr(agent, "_wykonaj_narzedzie", narzedzie)
    monkeypatch.setattr(agent.system_control, "wykonaj",
                        lambda *a: (_ for _ in ()).throw(AssertionError("prawdziwe polecenie systemowe!")))
    agent._klient = klient = KlientClaude(
        tura(narzedzia=[("t1", "sterowanie_systemem", {"polecenie": "zablokuj"})]))
    _, stan = przebieg(agent.odpowiedz("zablokuj ekran", [], przerwanie=przerwanie))
    assert stan["przerwane"] is True
    assert stan.get("system") is None          # main.py nie zablokuje ekranu po cichu
    assert len(klient.zapytania) == 1          # po przerwaniu nie pytamy modelu dalej


def test_obciecie_na_limicie_to_nie_przerwanie():
    pelny = "Pierwsze zdanie jest całe. Drugie urwało się w po"
    agent._klient = KlientClaude(tura(pelny, narzedzia=[("t9", "sterowanie_systemem", {"polecenie": "zablo"})],
                                      powod="max_tokens"))
    zdania, stan = przebieg(agent.odpowiedz("opowiedz długo", []))
    assert zdania == ["Pierwsze zdanie jest całe."]
    assert stan["historia"][-1]["content"] == [
        {"type": "text", "text": "Pierwsze zdanie jest całe. " + agent.ZNACZNIK_UCIECIA}]
    assert stan.get("przerwane") is None and stan.get("system") is None


def test_znacznik_trybu_sluchawek():
    agent._klient = klient = KlientClaude(tura("Jasne. "))
    przebieg(agent.odpowiedz("czekaj", [], po_wake_wordzie=False, wszedl_w_slowo=True))
    assert klient.zapytania[0]["messages"][-1]["content"] == "[wszedł ci w słowo] czekaj"


# --- "stop" z Telegrama ---------------------------------------------------

def most_telegrama(api):
    most = telegram_bridge.MostTelegram("123:atrapa", 111, odpowiedz=None)
    most._api = api
    return most


def test_stop_przerywa_odpowiedz_w_toku_i_czysci_kolejke():
    api = TelegramAPI()
    most = most_telegrama(api)
    biezace = threading.Event()
    most._biezace = biezace
    most._kolejka.put({"text": "czekająca wiadomość"})
    most._obsluz({"message": {"chat": {"id": 111}, "from": {"id": 111}, "date": int(time.time()),
                              "text": "Stop!"}})
    assert biezace.is_set()
    assert most._kolejka.empty()
    assert api.wyslane() == ["⏹ Przerwane."]


def test_stop_od_obcego_nic_nie_robi():
    api = TelegramAPI()
    most = most_telegrama(api)
    biezace = threading.Event()
    most._biezace = biezace
    assert most._obsluz({"message": {"chat": {"id": 999}, "from": {"id": 999},
                                     "date": int(time.time()), "text": "stop"}}) == "obca"
    assert not biezace.is_set() and api.wyslane() == []


def test_przerwana_przez_stop_odpowiedz_czeka_na_dokoncz():
    api = TelegramAPI()
    przerwanie = threading.Event()

    def agent_atrapa(tekst, historia, *argumenty, **opcje):
        yield "Pierwsza część."
        przerwanie.set()                          # w tym momencie przyszło "stop"
        yield {"przerwane": True, "niedokonczone": "Druga część"}

    most = telegram_bridge.MostTelegram("123:atrapa", 111, odpowiedz=agent_atrapa)
    most._api = api
    most._rozmawiaj("opowiedz", przerwanie)
    assert api.wyslane() == []                    # nic nie poszło
    assert most._reszta["tekst"] == "Pierwsza część. Druga część"
    assert most._historia[-1]["content"] == telegram_bridge.PRZERWANA_ODPOWIEDZ
