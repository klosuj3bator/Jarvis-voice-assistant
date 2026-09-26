"""
telegram_bridge.py — rozmowa z Jarvisem z telefonu przez Telegram.

Piszesz albo nagrywasz głosówkę do swojego bota, a Jarvis na komputerze
odpowiada tekstem i głosówką — tym samym "mózgiem" (agent.py) i z tymi samymi
narzędziami co przy rozmowie na głos. Przypomnienia przychodzą też na telefon.


JAK TO DZIAŁA: LONG POLLING
===========================

Telegram nie może sam "zadzwonić" do Twojego komputera — komputer siedzi za
routerem i nie ma publicznego adresu. Więc to komputer pyta Telegrama:
"są dla mnie jakieś wiadomości?". Zwykłe pytanie co sekundę marnowałoby
łącze, dlatego pytamy z długim czasem oczekiwania (long polling): Telegram
trzyma zapytanie otwarte do 25 sekund i odpowiada NATYCHMIAST, gdy tylko
przyjdzie wiadomość. Efekt jak przy powiadomieniach, a ruch sieciowy znikomy.

Wszystko robimy zwykłymi zapytaniami HTTP przez bibliotekę requests, która
i tak jest w projekcie — API Telegrama jest na tyle proste, że osobna
biblioteka do botów nie jest potrzebna.


BEZPIECZEŃSTWO — NAJWAŻNIEJSZA CZĘŚĆ TEGO PLIKU
===============================================

Ten bot daje zdalny dostęp do Twojego komputera: uruchomi program, zablokuje
ekran, zrestartuje system. A bota w Telegramie może znaleźć i napisać do
niego KAŻDY, kto zgadnie albo podejrzy jego nazwę. Dlatego:

  1. JEDEN WŁAŚCICIEL. Obsługujemy wyłącznie TELEGRAM_OWNER_ID z .env.
     Sprawdzamy i czat, i nadawcę — wiadomość z grupy, do której ktoś dodał
     bota, nie przejdzie, nawet jeśli Ty też w tej grupie jesteś.

  2. OBCYM NIE ODPOWIADAMY WCALE. Każda odpowiedź, nawet "odmowa dostępu",
     zdradzałaby, że bot działa i ktoś go słucha. Tylko wpis do dziennika.

  3. TOKEN NIGDY NIE TRAFIA DO DZIENNIKA. Telegram wkleja token w adres
     każdego zapytania, a biblioteka sieciowa cytuje ten adres w komunikatach
     błędów. Każdy taki komunikat przepuszczamy przez _bez_tokenu(),
     a oryginalny wyjątek odcinamy (`from None`), żeby nie wypłynął w śladzie
     błędu. Kto ma token, ten steruje botem — w dzienniku nie może go być.

  4. STARE WIADOMOŚCI POMIJAMY. Po starcie Telegram oddaje wszystko, co
     przyszło, gdy komputer był wyłączony. "Wyłącz komputer" wysłane wczoraj
     nie może wykonać się dziś rano — pomijamy wiadomości starsze niż
     MAKS_WIEK_WIADOMOSCI_S i mówimy Ci, ile ich było.

  5. RESTART I WYŁĄCZENIE WYMAGAJĄ ODPISANIA "tak". Dokładnie tego słowa,
     tekstem, w ciągu dwóch minut. Głosówka tego nie zatwierdzi — Whisper
     mógłby się przesłyszeć, a tu pomyłka kosztuje utratę niezapisanej pracy.


"stop" — DWA WĄTKI ZAMIAST JEDNEGO
==================================

Kiedyś jeden wątek i odbierał wiadomości, i na nie odpowiadał. Póki Jarvis
myślał, nikt nie pytał Telegrama o nowe wiadomości — więc "stop" dotarłoby
dopiero PO odpowiedzi, której miało zapobiec. Teraz:

    watek-telegram        — tylko odbiera; "stop" obsługuje od razu,
                            resztę odkłada do kolejki
    watek-telegram-praca  — po kolei odpowiada na wiadomości z kolejki

"stop" ustawia flagę przerwania bieżącej odpowiedzi (agent przestaje
generować, nic więcej nie zostaje wysłane) i wyrzuca z kolejki to, co
jeszcze czekało. W historii rozmowy zostaje ślad, że odpowiedź przerwano.
"""

import io
import json
import logging
import os
import queue
import re
import threading
import time

import requests
from dotenv import load_dotenv

import pamiec
import system_control

load_dotenv()

logger = logging.getLogger(__name__)

# Biblioteka sieciowa na poziomie DEBUG wypisuje pełne adresy zapytań —
# razem z tokenem. Dziennik Jarvisa pracuje na poziomie INFO, więc to się
# i tak nie pokaże, ale wolimy mieć pewność niezależną od tego ustawienia.
logging.getLogger("urllib3").setLevel(logging.WARNING)

API = "https://api.telegram.org"

# Ile sekund Telegram trzyma otwarte pytanie o nowe wiadomości (long polling).
CZAS_POLLINGU_S = 25

# Wiadomości starsze niż tyle sekund pomijamy (punkt 4 w opisie wyżej).
MAKS_WIEK_WIADOMOSCI_S = 300

# Ile sekund czekamy na odpisane "tak" przy restarcie i wyłączeniu.
CZAS_NA_POTWIERDZENIE_S = 120

# Ile ostatnich wiadomości rozmowy z telefonu pamiętamy (parzyście: pytanie + odpowiedź).
MAKS_HISTORIA = 12

# Dłuższych głosówek nie przepisujemy — Whisper liczyłby je bardzo długo,
# a w tym czasie nie obsłużyłby mikrofonu przy komputerze.
MAKS_DLUGOSC_GLOSOWKI_S = 120

# Telegram przyjmuje najwyżej 4096 znaków w jednej wiadomości.
MAKS_DLUGOSC_WIADOMOSCI = 4000

# Dłuższych odpowiedzi (np. całe tłumaczenie ze schowka) nie czytamy
# w głosówce — minuta słuchania czegoś, co masz przed oczami w tekście,
# to strata czasu. 500 znaków to mniej więcej pół minuty mowy.
MAKS_ZNAKOW_GLOSOWKI = 500


class BladTelegrama(Exception):
    """Błąd komunikacji z Telegramem. Jego treść nigdy nie zawiera tokenu."""


def _pociete(tekst, dlugosc):
    """Dzieli długi tekst na kawałki, które zmieszczą się w wiadomości."""
    return [tekst[i:i + dlugosc] for i in range(0, len(tekst), dlugosc)] or [""]


def _to_tak(tekst):
    """
    Czy odpisano dokładnie "tak"? ("Tak!", "tak." też się liczą.)

    Celowo ostrzej niż przy potwierdzaniu głosem: tu nie ma przesłyszeń,
    więc wymagamy tego jednego słowa — "tak, ale..." to już nie jest zgoda.
    """
    return re.sub(r"[^a-ząćęłńóśźż]", "", (tekst or "").lower()) == "tak"


def _to_stop(tekst):
    """Czy wiadomość to samo "stop"? ("Stop!", "/stop" też się liczą.)"""
    return re.sub(r"[^a-z]", "", (tekst or "").lower()) == "stop"


# Co zapisujemy w historii zamiast odpowiedzi, której nie wysłaliśmy.
PRZERWANA_ODPOWIEDZ = ("[PRZERWANE: użytkownik napisał „stop”, zanim cokolwiek "
                       "wysłałem — nie dokończyłem odpowiedzi.]")


def _na_probki_16k(dane):
    """
    Plik dźwiękowy -> tablica float32, 16 kHz, mono — format dla Whispera.

    Głosówki z Telegrama to OGG z kodekiem Opus (48 kHz). PyAV rozpakowuje
    je i przelicza na 16 kHz tak samo jak każdy inny format.
    """
    import av
    import numpy as np

    kontener = av.open(io.BytesIO(dane))
    resampler = av.AudioResampler(format="flt", layout="mono", rate=16000)
    kawalki = []
    for ramka in kontener.decode(kontener.streams.audio[0]):
        for porcja in resampler.resample(ramka):
            kawalki.append(porcja.to_ndarray().reshape(-1))
    for porcja in resampler.resample(None):
        kawalki.append(porcja.to_ndarray().reshape(-1))
    kontener.close()

    if not kawalki:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(kawalki).astype(np.float32)


class MostTelegram:
    """
    Jeden bot, jeden właściciel, jeden wątek w tle.

    Funkcje Jarvisa dostaje z zewnątrz (odpowiedz, przepisz, synteza_ogg,
    wykonaj_systemowe) zamiast importować je sam. Dzięki temu most da się
    przetestować z udawanym agentem i udawanym Telegramem — bez prawdziwego
    bota, bez mikrofonu i bez ryzyka, że test naprawdę zrestartuje komputer.
    """

    def __init__(self, token, wlasciciel, odpowiedz, przepisz=None,
                 synteza_ogg=None, wykonaj_systemowe=None, pomijane_zdania=()):
        self._token = token
        self._wlasciciel = int(wlasciciel)
        self._odpowiedz = odpowiedz
        self._przepisz = przepisz
        self._synteza_ogg = synteza_ogg
        self._wykonaj_systemowe = wykonaj_systemowe
        # Zapowiedzi w rodzaju "Już szukam." mają sens tylko w głośniku,
        # gdzie wypełniają ciszę. W wiadomości tekstowej to tylko śmieci.
        self._pomijane = set(pomijane_zdania)

        self._historia = []
        self._oczekujace = None      # (polecenie, od_kiedy) — czeka na "tak"
        self._offset = None
        self._zatrzymaj = threading.Event()
        self._watek = None
        self._sesja = requests.Session()

        # "stop" — opis w nagłówku modułu. _biezace to flaga przerwania
        # odpowiedzi, która właśnie powstaje (None, gdy nic się nie dzieje).
        # Blokada, bo _biezace i _oczekujace czytają oba wątki.
        self._kolejka = queue.Queue()
        self._biezace = None
        self._blokada = threading.Lock()
        self._watek_pracy = None

    # ---------------------------------------------------------------
    # Sieć
    # ---------------------------------------------------------------

    def _bez_tokenu(self, tekst):
        """Wycina token z dowolnego tekstu (punkt 3 w opisie modułu)."""
        return str(tekst).replace(self._token, "<TOKEN>")

    def _api(self, metoda, pliki=None, limit_s=15, **dane):
        """
        Jedno zapytanie do API Telegrama. WSZYSTKIE przechodzą tędy —
        dzięki temu jest jedno miejsce, w którym pilnujemy tokenu.

        Zwraca: pole "result" z odpowiedzi.
        """
        adres = f"{API}/bot{self._token}/{metoda}"
        try:
            odpowiedz = self._sesja.post(adres, data=dane, files=pliki, timeout=limit_s)
            wynik = odpowiedz.json()
        except (requests.RequestException, ValueError) as e:
            # "from None" odcina oryginalny wyjątek — jego treść zawiera adres,
            # a więc i token, i wypłynęłaby w śladzie błędu w dzienniku.
            raise BladTelegrama(f"{metoda}: {self._bez_tokenu(e)}") from None

        if not wynik.get("ok"):
            raise BladTelegrama(f"{metoda}: {wynik.get('description', 'nieznany błąd')}")
        return wynik["result"]

    def _pobierz_plik(self, file_id):
        """Pobiera plik wysłany do bota (np. głosówkę)."""
        info = self._api("getFile", file_id=file_id)
        adres = f"{API}/file/bot{self._token}/{info['file_path']}"
        try:
            odpowiedz = self._sesja.get(adres, timeout=30)
            odpowiedz.raise_for_status()
            return odpowiedz.content
        except requests.RequestException as e:
            raise BladTelegrama(f"pobieranie pliku: {self._bez_tokenu(e)}") from None

    def _wyslij_tekst(self, tekst):
        for kawalek in _pociete(tekst, MAKS_DLUGOSC_WIADOMOSCI):
            self._api("sendMessage", chat_id=self._wlasciciel, text=kawalek)

    def _wyslij_glos(self, tekst):
        """Wysyła odpowiedź jako głosówkę. Awaria syntezy nie jest błędem całości."""
        if self._synteza_ogg is None:
            return
        ogg = self._synteza_ogg(tekst)
        if not ogg:
            logger.warning("[TELEGRAM] Nie udało się przygotować głosówki — wysłałem sam tekst.")
            return
        self._api("sendVoice", chat_id=self._wlasciciel, limit_s=60,
                  pliki={"voice": ("jarvis.ogg", ogg, "audio/ogg")})

    def _wyslij_zdjecie(self, jpeg):
        """
        Wysyła zdjęcie (zrzut ekranu). Adresat jest zawsze ten sam — właściciel;
        nie ma tu parametru "komu", żeby nie dało się go pomylić ani podmienić.
        """
        self._api("sendPhoto", chat_id=self._wlasciciel, limit_s=60,
                  pliki={"photo": ("zrzut.jpg", jpeg, "image/jpeg")})

    def _status(self, akcja="typing"):
        """"Pisze..." / "Nagrywa..." pod nazwą bota, żeby było widać, że pracuje."""
        try:
            self._api("sendChatAction", chat_id=self._wlasciciel, action=akcja)
        except BladTelegrama:
            pass    # to tylko ozdobnik — jego brak niczego nie psuje

    # ---------------------------------------------------------------
    # Obsługa wiadomości
    # ---------------------------------------------------------------

    def _obsluz(self, aktualizacja):
        """
        Jedna aktualizacja od Telegrama.

        Zwraca: "obca", "stara" albo None — do liczenia pominiętych.
        """
        wiadomosc = aktualizacja.get("message")
        if not wiadomosc:
            return None     # edycje, reakcje itp. — nie obsługujemy

        czat = (wiadomosc.get("chat") or {}).get("id")
        nadawca = wiadomosc.get("from") or {}

        # Punkt 1 i 2 z opisu modułu: tylko właściciel, obcym zero odpowiedzi.
        if czat != self._wlasciciel or nadawca.get("id") != self._wlasciciel:
            logger.warning(
                "[TELEGRAM] Odrzucam wiadomość od obcego: nadawca %s (@%s), czat %s.",
                nadawca.get("id"), nadawca.get("username", "?"), czat)
            return "obca"

        # Punkt 4: nic sprzed startu.
        wiek = time.time() - wiadomosc.get("date", 0)
        if wiek > MAKS_WIEK_WIADOMOSCI_S:
            logger.info("[TELEGRAM] Pomijam wiadomość sprzed %.0f min.", wiek / 60)
            return "stara"

        # "stop" od razu, tutaj — resztę odkładamy dla wątku pracy
        # (opis w nagłówku modułu).
        if _to_stop(wiadomosc.get("text")):
            self._stop()
        else:
            self._kolejka.put(wiadomosc)
        return None

    def _stop(self):
        """"stop" z telefonu: przerywa odpowiedź w toku i porzuca to, co czekało."""
        porzucone = 0
        while True:
            try:
                self._kolejka.get_nowait()
                porzucone += 1
            except queue.Empty:
                break

        with self._blokada:
            biezace, oczekujace = self._biezace, self._oczekujace
            self._oczekujace = None     # "stop" anuluje też czekanie na "tak"

        if biezace is not None:
            biezace.set()
        logger.info("[TELEGRAM] stop — odpowiedź w toku: %s, porzucone z kolejki: %d%s.",
                    "tak" if biezace is not None else "nie", porzucone,
                    f", anulowane {oczekujace[0]}" if oczekujace else "")

        if biezace is not None or porzucone:
            self._wyslij_tekst("⏹ Przerwane.")
        elif oczekujace is not None:
            self._wyslij_tekst("Anulowane, nic nie robię.")
        else:
            self._wyslij_tekst("Nic teraz nie robię.")

    def _praca(self):
        """Wątek pracy: po kolei odpowiada na wiadomości z kolejki."""
        while not self._zatrzymaj.is_set():
            try:
                wiadomosc = self._kolejka.get(timeout=1)
            except queue.Empty:
                continue

            przerwanie = threading.Event()
            with self._blokada:
                self._biezace = przerwanie
            try:
                self._wykonaj(wiadomosc, przerwanie)
            except Exception:
                # Błąd jednej wiadomości nie może zatrzymać mostu.
                logger.exception("[TELEGRAM] Błąd przy obsłudze wiadomości")
                try:
                    self._wyslij_tekst("Coś poszło nie tak po mojej stronie — "
                                       "szczegóły są w dzienniku Jarvisa.")
                except BladTelegrama:
                    pass
            finally:
                with self._blokada:
                    self._biezace = None

    def _wykonaj(self, wiadomosc, przerwanie):
        """Jedna wiadomość z kolejki (już sprawdzona w _obsluz)."""
        if "text" in wiadomosc:
            self._obsluz_tekst(wiadomosc["text"], przerwanie)
        elif "voice" in wiadomosc or "audio" in wiadomosc:
            self._obsluz_glos(wiadomosc.get("voice") or wiadomosc["audio"], przerwanie)
        else:
            self._wyslij_tekst("Rozumiem tekst i wiadomości głosowe — tego jeszcze nie.")

    def _obsluz_tekst(self, tekst, przerwanie):
        tekst = tekst.strip()

        if self._obsluz_potwierdzenie(tekst):
            return

        # Polecenia samego Telegrama, np. /start przy pierwszym uruchomieniu bota.
        if tekst.startswith("/"):
            self._wyslij_tekst("Cześć! Pisz albo nagrywaj głosówki — przekażę je Jarvisowi. "
                               "„stop” przerywa odpowiedź, która właśnie powstaje.")
            return

        # Hasło wysłane w wiadomości nie może zostać w jarvis.log (opis w pamiec.py).
        logger.info("[TELEGRAM] [TY] %s", pamiec.ukryj_jesli_sekret(tekst))
        self._rozmawiaj(tekst, przerwanie)

    def _obsluz_glos(self, glos, przerwanie):
        # Punkt 5: głosówka NIE zatwierdza restartu — anuluje go.
        with self._blokada:
            oczekujace, self._oczekujace = self._oczekujace, None
        if oczekujace is not None:
            logger.info("[TELEGRAM] Głosówka zamiast „tak” — anuluję %s.", oczekujace[0])
            self._wyslij_tekst("Anulowane. Restart i wyłączenie potwierdzam tylko "
                               "odpisanym „tak”, nie głosówką.")
            return

        if self._przepisz is None:
            self._wyslij_tekst("Nie mam teraz dostępu do rozpoznawania mowy — napisz tekstem.")
            return

        if glos.get("duration", 0) > MAKS_DLUGOSC_GLOSOWKI_S:
            self._wyslij_tekst(f"Ta głosówka jest za długa — przepisuję najwyżej "
                               f"{MAKS_DLUGOSC_GLOSOWKI_S // 60} minuty.")
            return

        self._status("typing")
        audio = _na_probki_16k(self._pobierz_plik(glos["file_id"]))
        tekst = self._przepisz(audio)

        if not tekst:
            self._wyslij_tekst("Nie rozpoznałem słów w tym nagraniu.")
            return

        logger.info("[TELEGRAM] [TY głosem] %s", pamiec.ukryj_jesli_sekret(tekst))
        self._rozmawiaj(tekst, przerwanie, uslyszane=tekst)

    def _obsluz_potwierdzenie(self, tekst):
        """
        Jeśli czekamy na "tak" — rozstrzyga sprawę.

        Zwraca: True, jeśli wiadomość była odpowiedzią na pytanie
                o potwierdzenie (i nie trzeba jej już przekazywać Jarvisowi).
        """
        with self._blokada:
            oczekujace, self._oczekujace = self._oczekujace, None
        if oczekujace is None:
            return False

        polecenie, od_kiedy = oczekujace
        za_pozno = time.monotonic() - od_kiedy > CZAS_NA_POTWIERDZENIE_S

        if za_pozno:
            if _to_tak(tekst):
                self._wyslij_tekst("Minęło za dużo czasu, więc anulowałem. "
                                   "Poproś jeszcze raz, jeśli nadal chcesz.")
                return True
            return False    # zwykła nowa wiadomość — obsłużmy ją normalnie

        if not _to_tak(tekst):
            logger.info("[TELEGRAM] Brak „tak” — anuluję %s.", polecenie)
            self._wyslij_tekst("Anulowane, nic nie robię.")
            return True

        logger.warning("[TELEGRAM] Potwierdzone „tak” — wykonuję: %s", polecenie)
        komunikat, _ = self._wykonaj_systemowe(polecenie)
        self._wyslij_tekst(komunikat)
        return True

    def _rozmawiaj(self, tekst, przerwanie, uslyszane=None):
        """
        Przekazuje wiadomość Jarvisowi i odsyła odpowiedź: tekst + głosówka.

        przerwanie — flaga ustawiana przez "stop" (patrz _stop). Sprawdzamy ją
        przed każdym krokiem, który coś wysyła albo coś robi na komputerze.
        """
        if przerwanie.is_set():
            return
        self._status("typing")

        zdania, stan = [], {}
        for element in self._odpowiedz(tekst, list(self._historia), True, "telegram",
                                       przerwanie=przerwanie):
            if isinstance(element, dict):
                stan.update(element)
            elif element not in self._pomijane:
                zdania.append(element)

        if przerwanie.is_set():
            # "stop" przyszło, zanim cokolwiek wysłaliśmy: nic nie wysyłamy,
            # a w historii zostaje ślad, że odpowiedź przerwano.
            self._zapamietaj(tekst, PRZERWANA_ODPOWIEDZ)
            return

        odpowiedz = " ".join(zdania).strip()
        self._zapamietaj(tekst, odpowiedz)
        polecenie = stan.get("system")

        # Przy głosówce pokazujemy najpierw, co Jarvis zrozumiał — żeby było
        # widać, czy Whisper się nie przesłyszał.
        tresc = odpowiedz or "Nie mam nic do dodania."
        if uslyszane:
            tresc = f"🎙 „{uslyszane}”\n\n{tresc}"
        if polecenie in system_control.POLECENIA_DO_POTWIERDZENIA:
            tresc += "\n\nOdpisz „tak”, żeby potwierdzić."

        self._wyslij_tekst(tresc)

        # Tekst już poszedł — "stop" może jeszcze oszczędzić zdjęcia, głosówkę
        # i przede wszystkim polecenie systemowe (blokada, uśpienie).
        # Zrzuty ekranu od narzędzia screenshot — zaraz po tekście,
        # przed głosówką, żeby zdjęcie nie czekało na syntezę mowy.
        for jpeg in stan.get("zdjecia") or []:
            if przerwanie.is_set():
                break
            self._status("upload_photo")
            self._wyslij_zdjecie(jpeg)

        if przerwanie.is_set():
            logger.info("[TELEGRAM] Przerwane po wysłaniu tekstu — bez głosówki i poleceń.")
            return

        if odpowiedz and len(odpowiedz) <= MAKS_ZNAKOW_GLOSOWKI:
            self._status("record_voice")
            self._wyslij_glos(odpowiedz)
        elif odpowiedz:
            logger.info("[TELEGRAM] Odpowiedź ma %d znaków — bez głosówki, sam tekst.",
                        len(odpowiedz))

        if polecenie and not przerwanie.is_set():
            self._polecenie_systemowe(polecenie)

    def _polecenie_systemowe(self, polecenie):
        """
        Blokada, uśpienie, restart, wyłączenie — zgłoszone przez agenta.

        Restart i wyłączenie czekają na odpisane "tak". Blokada i uśpienie
        wykonują się od razu, tak samo jak przy rozmowie na głos.
        """
        if polecenie in system_control.POLECENIA_DO_POTWIERDZENIA:
            with self._blokada:
                self._oczekujace = (polecenie, time.monotonic())
            logger.info("[TELEGRAM] Czekam na „tak” dla: %s", polecenie)
            return

        if self._wykonaj_systemowe is None:
            return
        komunikat, sukces = self._wykonaj_systemowe(polecenie)
        if not sukces:
            self._wyslij_tekst(komunikat)

    def _zapamietaj(self, tekst, odpowiedz):
        """
        Dopisuje wymianę do historii rozmowy z telefonu.

        Trzymamy sam tekst, bez bloków narzędzi. Historia z narzędziami
        wymaga par "wywołanie + wynik" — przycięcie jej w środku takiej pary
        kończy się błędem API. Czysty tekst można ciąć gdziekolwiek.
        """
        self._historia += [
            {"role": "user", "content": f"[Telegram] {tekst}"},
            {"role": "assistant", "content": odpowiedz or "(bez odpowiedzi)"},
        ]
        self._historia = self._historia[-MAKS_HISTORIA:]

    # ---------------------------------------------------------------
    # Przypomnienia i wątek
    # ---------------------------------------------------------------

    def powiadom(self, tekst):
        """
        Wysyła przypomnienie na telefon. Rejestrowane w reminders.dodaj_odbiorce().

        W osobnym wątku, żeby wolna sieć nie opóźniała przypomnienia na głos.
        """
        def wyslij():
            try:
                self._wyslij_tekst(f"⏰ {tekst}")
            except BladTelegrama as e:
                logger.warning("[TELEGRAM] Nie wysłałem przypomnienia: %s", e)

        threading.Thread(target=wyslij, name="telegram-przypomnienie", daemon=True).start()

    def powiadom_glosem(self, tekst):
        """
        Powiadomienie tekstem I głosówką — np. o nowym mailu z czujki.

        Tekst idzie zwykłą wiadomością, bez formatowania: temat maila
        pochodzi od obcej osoby, więc nie dajemy mu szansy na sztuczki
        z pogrubieniami czy linkami udającymi coś innego.
        """
        def wyslij():
            try:
                self._wyslij_tekst(f"📧 {tekst}")
                self._wyslij_glos(tekst)
            except BladTelegrama as e:
                logger.warning("[TELEGRAM] Nie wysłałem powiadomienia: %s", e)

        threading.Thread(target=wyslij, name="telegram-powiadomienie", daemon=True).start()

    def _petla(self):
        """Wątek w tle: pyta Telegrama o nowe wiadomości i je obsługuje."""
        logger.info("[TELEGRAM] Most uruchomiony — czekam na wiadomości.")
        przerwa = 5

        while not self._zatrzymaj.is_set():
            parametry = {"timeout": CZAS_POLLINGU_S,
                         "allowed_updates": json.dumps(["message"])}
            if self._offset is not None:
                # offset = "potwierdzam wszystko przed tym numerem" — Telegram
                # nie odda już tych wiadomości drugi raz.
                parametry["offset"] = self._offset
            try:
                aktualizacje = self._api("getUpdates", limit_s=CZAS_POLLINGU_S + 10,
                                         **parametry)
                przerwa = 5
            except BladTelegrama as e:
                # Brak internetu, uśpiony komputer... Czekamy coraz dłużej,
                # żeby nie zasypywać dziennika co pięć sekund.
                logger.warning("[TELEGRAM] %s — ponawiam za %d s.", e, przerwa)
                self._zatrzymaj.wait(przerwa)
                przerwa = min(przerwa * 2, 60)
                continue

            pominiete = 0
            for aktualizacja in aktualizacje:
                self._offset = aktualizacja["update_id"] + 1
                try:
                    if self._obsluz(aktualizacja) == "stara":
                        pominiete += 1
                except Exception:
                    # Błąd jednej wiadomości nie może zatrzymać mostu.
                    logger.exception("[TELEGRAM] Błąd przy obsłudze wiadomości")
                    try:
                        self._wyslij_tekst("Coś poszło nie tak po mojej stronie — "
                                           "szczegóły są w dzienniku Jarvisa.")
                    except BladTelegrama:
                        pass

            if pominiete:
                try:
                    self._wyslij_tekst(
                        f"Pominąłem {pominiete} wiadomości wysłanych, gdy nie działałem. "
                        "Jeśli któraś jest nadal aktualna, napisz ją jeszcze raz.")
                except BladTelegrama:
                    pass

        logger.info("[TELEGRAM] Most zatrzymany.")

    def uruchom(self):
        # Dwa wątki — opis przy "stop" w nagłówku modułu.
        self._watek_pracy = threading.Thread(target=self._praca, name="watek-telegram-praca",
                                             daemon=True)
        self._watek_pracy.start()
        self._watek = threading.Thread(target=self._petla, name="watek-telegram",
                                       daemon=True)
        self._watek.start()

    def zatrzymaj(self):
        """Prosi wątek o zakończenie. Pytanie w toku może jeszcze trwać do 25 s,
        ale wątek jest w tle, więc nie blokuje zamknięcia programu."""
        self._zatrzymaj.set()


# ---------------------------------------------------------------
# Wejście dla main.py
# ---------------------------------------------------------------

_most = None


def uruchom_z_env(odpowiedz, przepisz=None, synteza_ogg=None,
                  wykonaj_systemowe=None, pomijane_zdania=()):
    """
    Uruchamia most, jeśli w .env są TELEGRAM_JARVIS_TOKEN i TELEGRAM_OWNER_ID.

    Bez nich po prostu nic nie robi — Telegram jest dodatkiem, Jarvis
    ma działać normalnie także bez niego.

    Zwraca: obiekt MostTelegram albo None.
    """
    global _most

    token = os.getenv("TELEGRAM_JARVIS_TOKEN", "").strip()
    wlasciciel = os.getenv("TELEGRAM_OWNER_ID", "").strip()
    if not token or not wlasciciel:
        logger.info("Telegram wyłączony — brak TELEGRAM_JARVIS_TOKEN "
                    "albo TELEGRAM_OWNER_ID w .env.")
        return None

    try:
        wlasciciel = int(wlasciciel)
    except ValueError:
        logger.error("TELEGRAM_OWNER_ID musi być liczbą (Twoim ID z Telegrama). "
                     "Most do Telegrama NIE wystartował.")
        return None

    _most = MostTelegram(token, wlasciciel, odpowiedz, przepisz, synteza_ogg,
                         wykonaj_systemowe, pomijane_zdania)
    _most.uruchom()
    return _most


def zatrzymaj():
    if _most is not None:
        _most.zatrzymaj()


# --- Pomocnik: `python telegram_bridge.py kto-ja` ---
#
# Sprawdza token i pokazuje Twoje ID do wpisania jako TELEGRAM_OWNER_ID.
# Uruchom, a potem napisz cokolwiek do swojego bota w Telegramie.
if __name__ == "__main__":
    import sys

    token = os.getenv("TELEGRAM_JARVIS_TOKEN", "").strip()
    if not token:
        print("Brak TELEGRAM_JARVIS_TOKEN w .env — najpierw dopisz token od @BotFather.")
        sys.exit(1)

    most = MostTelegram(token, 0, odpowiedz=None)
    try:
        bot = most._api("getMe")
    except BladTelegrama as e:
        print(f"Token nie działa: {e}")
        sys.exit(1)
    print(f"Token działa — bot: @{bot['username']}")

    if "kto-ja" not in sys.argv:
        sys.exit(0)

    print("Napisz teraz cokolwiek do tego bota w Telegramie (czekam 2 minuty)...")
    koniec = time.time() + 120
    offset = None
    while time.time() < koniec:
        parametry = {"timeout": 20}
        if offset is not None:
            parametry["offset"] = offset
        for aktualizacja in most._api("getUpdates", limit_s=30, **parametry):
            offset = aktualizacja["update_id"] + 1
            nadawca = (aktualizacja.get("message") or {}).get("from") or {}
            if nadawca:
                print(f"\nWiadomość od: {nadawca.get('first_name', '')} "
                      f"(@{nadawca.get('username', '?')})")
                print(f"Dopisz do .env:  TELEGRAM_OWNER_ID={nadawca['id']}")
                sys.exit(0)
    print("Nic nie przyszło.")
