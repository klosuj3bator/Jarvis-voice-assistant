"""
Kalendarz (kalendarz.py): API z zapasem iCal, dodawanie wydarzeń po "tak",
przypomnienia przed wydarzeniami i wiadomość, gdy Google odbierze dostęp.
Google i iCal zastępują atrapy.
"""

import datetime
import importlib
import json

import pytest

import agent
import google_kalendarz
import kalendarz
import konfiguracja
import potwierdzenia
import reminders
from atrapy import KlientClaude, przebieg, tura

W = kalendarz.STREFA
TERAZ = datetime.datetime(2026, 10, 14, 9, 0, tzinfo=W)          # środa


def wydarzenie(tytul, godzina, minuta=0, dzien=TERAZ.date(), id_=None, miejsce=""):
    od = datetime.datetime.combine(dzien, datetime.time(godzina, minuta), W)
    return {"id": id_ or tytul, "od": od, "do": od + datetime.timedelta(hours=1), "caly_dzien": False,
            "tytul": tytul, "miejsce": miejsce, "zaproszenie": False}


@pytest.fixture
def api(monkeypatch):
    """Kalendarz "połączony" z Google; wydarzenia i dodawanie — atrapy."""
    with open(konfiguracja.PLIK_GOOGLE_TOKENU, "w", encoding="utf-8") as plik:
        json.dump({"refresh_token": "x"}, plik)
    stan = {"wydarzenia": [], "dodane": [], "blad": None}

    def wydarzenia(od, do):
        if stan["blad"]:
            raise stan["blad"]
        return [w for w in stan["wydarzenia"] if od <= (w["od"] if w["caly_dzien"] is False else
                datetime.datetime.combine(w["od"], datetime.time(0), W)) < do]

    monkeypatch.setattr(google_kalendarz, "wydarzenia", wydarzenia)
    monkeypatch.setattr(google_kalendarz, "dodaj", lambda *a: stan["dodane"].append(a) or "id")
    monkeypatch.setattr(kalendarz, "_pamiec_api", {})
    monkeypatch.setattr(potwierdzenia, "_oczekujaca", None)
    return stan


# --- odczyt: API, a gdy nie działa — iCal --------------------------------

def test_odczyt_z_api(api):
    api["wydarzenia"] = [wydarzenie("Dentysta", 15), wydarzenie("Rozmowa", 10)]
    assert [w["tytul"] for w in kalendarz.wydarzenia_dnia(TERAZ.date())] == ["Rozmowa", "Dentysta"]


def test_gdy_api_nie_dziala_zostaje_ical(api, monkeypatch):
    api["blad"] = google_kalendarz.BladApi("Google nie odpowiedział")
    monkeypatch.setenv("GOOGLE_CALENDAR_ICAL_URL", "https://example.com/basic.ics")
    monkeypatch.setattr(kalendarz, "_z_ical", lambda od, do: [wydarzenie("Z iCal", 12)])
    assert [w["tytul"] for w in kalendarz.wydarzenia_dnia(TERAZ.date())] == ["Z iCal"]


def test_bez_api_i_bez_ical_jest_blad(api):
    api["blad"] = google_kalendarz.BladApi("awaria")
    with pytest.raises(kalendarz.BladKalendarza):
        kalendarz.wydarzenia_dnia(TERAZ.date())


def test_wygasniecie_dostepu_zglaszane_raz(api, monkeypatch):
    zgloszenia = []
    monkeypatch.setattr(kalendarz, "_przy_wygasnieciu", [zgloszenia.append])
    monkeypatch.setenv("GOOGLE_CALENDAR_ICAL_URL", "https://example.com/basic.ics")
    monkeypatch.setattr(kalendarz, "_z_ical", lambda od, do: [])

    def wygasl(od, do):
        with open(konfiguracja.PLIK_GOOGLE_TOKENU, "w", encoding="utf-8") as plik:
            json.dump({"refresh_token": "x", "wygasl": True}, plik)      # jak google_kalendarz
        raise google_kalendarz.TokenWygasl("wygasł")

    monkeypatch.setattr(google_kalendarz, "wydarzenia", wygasl)
    kalendarz.wydarzenia_dnia(TERAZ.date())
    kalendarz.wydarzenia_dnia(TERAZ.date() + datetime.timedelta(days=1))   # już przez iCal
    assert zgloszenia == [kalendarz.WYGASL_KOMUNIKAT]


# --- dodawanie wydarzeń --------------------------------------------------

def test_bez_polaczenia_z_api_nie_da_sie_dodac():
    komunikat, ok = kalendarz.dodaj_wydarzenie({"tytul": "x", "dzien": "jutro", "godzina": "10:00"}, 1, TERAZ)
    assert not ok and "Połącz z Google" in komunikat


def test_dodanie_dopiero_po_tak_w_kolejnej_wypowiedzi(api):
    komunikat, ok = kalendarz.dodaj_wydarzenie(
        {"tytul": "Rozmowa rekrutacyjna", "dzien": "piątek", "godzina": "10"}, 1, TERAZ)
    assert komunikat.startswith(potwierdzenia.POTRZEBNE_POTWIERDZENIE)
    assert "„Rozmowa rekrutacyjna” — piątek, 16 października 2026, 10:00–11:00" in komunikat
    kod = komunikat.split("(kod ")[1].split(")")[0]
    assert potwierdzenia.potwierdz(kod, "tak", 1)[0].startswith("NIE WYKONANO")    # ta sama wypowiedź
    assert api["dodane"] == []
    assert potwierdzenia.potwierdz(kod, "tak, dodaj", 2) == (
        "Dodałem do kalendarza: „Rozmowa rekrutacyjna” — piątek, 16 października 2026, 10:00–11:00.", True)
    (tytul, od, do, miejsce), = api["dodane"]
    assert (tytul, od, do - od) == ("Rozmowa rekrutacyjna",
                                    datetime.datetime(2026, 10, 16, 10, 0, tzinfo=W), datetime.timedelta(hours=1))


def test_strefa_warszawska_takze_po_zmianie_czasu(api):
    for dzien, przesuniecie in (("20.10", 2), ("27.10", 1)):        # czas letni / zimowy
        komunikat, _ = kalendarz.dodaj_wydarzenie({"tytul": "x", "dzien": dzien, "godzina": "10:00"}, 1, TERAZ)
        potwierdzenia.potwierdz(komunikat.split("(kod ")[1].split(")")[0], "tak", 2)
        od = api["dodane"][-1][1]
        assert (od.hour, od.utcoffset()) == (10, datetime.timedelta(hours=przesuniecie))


def test_bez_godziny_caly_dzien_a_zly_dzien_i_przeszlosc_odrzucone(api):
    komunikat, _ = kalendarz.dodaj_wydarzenie({"tytul": "Urlop", "dzien": "jutro"}, 1, TERAZ)
    assert "cały dzień" in komunikat
    assert not kalendarz.dodaj_wydarzenie({"tytul": "x", "dzien": "kiedyś", "godzina": "10"}, 1, TERAZ)[1]
    assert not kalendarz.dodaj_wydarzenie({"tytul": "x", "dzien": "dzisiaj", "godzina": "8:00"}, 1, TERAZ)[1]
    assert not kalendarz.dodaj_wydarzenie({"tytul": "x", "dzien": "jutro", "godzina": "25:00"}, 1, TERAZ)[1]


def test_agent_create_event_i_confirm_action(api, monkeypatch):
    monkeypatch.setattr(potwierdzenia.uuid, "uuid4", lambda: type("U", (), {"hex": "abc123ffff"})())
    monkeypatch.setattr(kalendarz.datetime, "datetime", type("D", (datetime.datetime,), {
        "now": staticmethod(lambda tz=None: TERAZ)}))
    agent._klient = KlientClaude(
        tura(narzedzia=[("e1", "create_event", {"tytul": "Rozmowa", "dzien": "piątek", "godzina": "10:00"})]),
        tura("Dodać rozmowę w piątek o dziesiątej?"))
    _, stan = przebieg(agent.odpowiedz("dodaj rozmowę w piątek o 10", []))
    assert api["dodane"] == []
    agent._klient = KlientClaude(tura(narzedzia=[("c1", "confirm_action", {"kod": "abc123"})]), tura("Dodane."))
    przebieg(agent.odpowiedz("tak", stan["historia"]))
    assert [a[0] for a in api["dodane"]] == ["Rozmowa"]


# --- przypomnienia przed wydarzeniami -----------------------------------

def test_przypomnienie_30_minut_wczesniej_tylko_raz(api):
    api["wydarzenia"] = [wydarzenie("Rozmowa", 9, 25, miejsce="Biuro"), wydarzenie("Później", 10, 30)]
    ogloszone = []
    assert kalendarz.sprawdz_przypomnienia(ogloszone.append, TERAZ) == ["Za 25 minut: Rozmowa, o 09:25, Biuro."]
    assert kalendarz.sprawdz_przypomnienia(ogloszone.append, TERAZ + datetime.timedelta(minutes=1)) == []
    assert ogloszone == ["Za 25 minut: Rozmowa, o 09:25, Biuro."]


def test_po_restarcie_przypomnienie_sie_nie_powtarza(api):
    api["wydarzenia"] = [wydarzenie("Rozmowa", 9, 20)]
    kalendarz.sprawdz_przypomnienia(lambda t: None, TERAZ)
    modul = importlib.reload(kalendarz)          # restart: pamięć modułu od nowa, plik zostaje
    assert modul.sprawdz_przypomnienia(lambda t: None, TERAZ + datetime.timedelta(minutes=5)) == []


def test_przeniesione_wydarzenie_przypomina_sie_o_nowej_godzinie(api):
    api["wydarzenia"] = [wydarzenie("Rozmowa", 9, 20, id_="r1")]
    kalendarz.sprawdz_przypomnienia(lambda t: None, TERAZ)
    api["wydarzenia"] = [wydarzenie("Rozmowa", 9, 28, id_="r1")]
    kalendarz._pamiec_api.clear()
    assert kalendarz.sprawdz_przypomnienia(lambda t: None, TERAZ) == ["Za 28 minut: Rozmowa, o 09:28."]


def test_czas_przypomnienia_z_env_a_calodniowe_i_trwajace_pominiete(api, monkeypatch):
    calodniowe = dict(wydarzenie("Urlop", 0), caly_dzien=True, od=TERAZ.date())
    api["wydarzenia"] = [wydarzenie("Za godzinę", 9, 55), wydarzenie("Już trwa", 8, 30), calodniowe]
    assert kalendarz.sprawdz_przypomnienia(lambda t: None, TERAZ) == []          # 55 min > 30
    monkeypatch.setenv(konfiguracja.PRZYPOMNIENIE_KALENDARZA, "60")
    assert kalendarz.sprawdz_przypomnienia(lambda t: None, TERAZ) == ["Za 55 minut: Za godzinę, o 09:55."]
    monkeypatch.setenv(konfiguracja.PRZYPOMNIENIE_KALENDARZA, "0")             # 0 = wyłączone
    api["wydarzenia"] = [wydarzenie("Zaraz", 9, 10)]
    assert kalendarz.sprawdz_przypomnienia(lambda t: None, TERAZ) == []


def test_oglos_mowi_i_wysyla_na_telegram(monkeypatch):
    powiedziane, telefon = [], []
    monkeypatch.setattr(reminders, "_mow", powiedziane.append)
    monkeypatch.setattr(reminders, "_odbiorcy", [telefon.append])
    reminders.oglos("Za 30 minut: Rozmowa, o 10:00.")
    assert powiedziane == telefon == ["Za 30 minut: Rozmowa, o 10:00."]
