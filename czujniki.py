"""
czujniki.py — odczyty sprzętowe pokazywane w rogu HUD-a: sieć i temperatury.

Wszystko, co ten moduł robi, sprowadza się do dwóch pytań: ile teraz płynie
przez łącze i jak gorąco jest w środku komputera. Odpowiedzi trafiają
do gui.py, a stamtąd na ekran.


DLACZEGO TEMPERATURA PROCESORA JEST TRUDNA NA WINDOWSIE
=======================================================

Na Linuksie wystarcza psutil.sensors_temperatures(). Na Windowsie tej funkcji
po prostu nie ma, bo system nie wystawia odczytów z czujników procesora
żadnym publicznym interfejsem. Sprawdziłem na tym komputerze wszystkie
zwyczajowe drogi:

    psutil.sensors_temperatures() .................. nie istnieje na Windows
    WMI: MSAcpi_ThermalZoneTemperature ............. "Not supported"
    WMI: Win32_PerfFormattedData...ThermalZone ..... pusto
    WMI: Win32_TemperatureProbe .................... brak czujników

Odczyt z czujników procesora wymaga sterownika działającego na poziomie
jądra — i właśnie taki instalują programy w rodzaju LibreHardwareMonitor.
Jeśli taki program działa, wystawia swoje odczyty przez WMI i wtedy bierzemy
je stąd. Jeśli nie działa, pokazujemy temperaturę karty graficznej, którą
NVIDIA udostępnia wprost, razem z progami bezpieczeństwa.

Kolejność prób jest więc taka:
    1. LibreHardwareMonitor / OpenHardwareMonitor (procesor ORAZ karta),
    2. NVML, czyli biblioteka NVIDII (sama karta),
    3. nic — wtedy HUD po prostu nie pokazuje tego wiersza.
"""

import logging
import threading

import psutil

logger = logging.getLogger(__name__)

# Przelicznik: bajty na sekundę -> megabity na sekundę. Prędkości łączy
# podaje się w megabitach (stąd "100 Mb/s" u operatora), a ruch mierzy
# się w bajtach — różnica ośmiokrotna, łatwa do przeoczenia.
BITY_W_BAJCIE = 8
MEGA = 1_000_000

# Powyżej tego ułamka limitu temperatura robi się bursztynowa na HUD-zie.
PROG_ALARMU_TEMPERATURY = 0.85

_nvml = None            # uchwyt do biblioteki NVIDII, gdy się uda
_karta = None
_nvml_sprawdzone = False
_blokada_nvml = threading.Lock()


# ---------------------------------------------------------------
# Sieć
# ---------------------------------------------------------------

class PomiarSieci:
    """
    Liczy bieżącą prędkość pobierania i wysyłania.

    System nie podaje "prędkości" wprost — trzyma tylko licznik bajtów, które
    przeszły od uruchomienia. Prędkość to różnica dwóch takich odczytów
    podzielona przez czas między nimi. Dlatego pierwszy odczyt zawsze zwraca
    zera: nie ma jeszcze z czym porównać.
    """

    def __init__(self):
        self._poprzednie = None
        self._czas = None
        self.szczyt_pobierania = 0.0
        self.szczyt_wysylania = 0.0

    @staticmethod
    def _liczniki_sieciowe():
        """
        Sumuje liczniki wszystkich PRAWDZIWYCH kart sieciowych.

        Pomijamy pętlę zwrotną (localhost) — leci przez nią ruch, który nigdy
        nie opuszcza komputera, m.in. rozmowa Jarvisa z jego własną drugą
        kopią. Doliczanie jej do "prędkości internetu" byłoby mylące.
        """
        wysłane = odebrane = 0
        stany = psutil.net_if_stats()
        for nazwa, licznik in psutil.net_io_counters(pernic=True).items():
            stan = stany.get(nazwa)
            if stan is None or not stan.isup or "loopback" in nazwa.lower():
                continue
            wysłane += licznik.bytes_sent
            odebrane += licznik.bytes_recv
        return wysłane, odebrane

    def odczyt(self):
        """
        Zwraca (pobieranie, wysyłanie) w megabitach na sekundę.

        Woła się to raz na sekundę z wątku statystyk gui.py.
        """
        import time

        teraz = time.monotonic()
        licznik = self._liczniki_sieciowe()

        if self._poprzednie is None or teraz <= self._czas:
            self._poprzednie, self._czas = licznik, teraz
            return 0.0, 0.0

        odstep = teraz - self._czas
        wyslane, odebrane = licznik
        wyslane_wczesniej, odebrane_wczesniej = self._poprzednie
        pobieranie = (odebrane - odebrane_wczesniej) * BITY_W_BAJCIE / odstep / MEGA
        wysylanie = (wyslane - wyslane_wczesniej) * BITY_W_BAJCIE / odstep / MEGA

        self._poprzednie, self._czas = licznik, teraz

        # Ujemne wartości zdarzają się po uśpieniu komputera, gdy licznik
        # systemowy startuje od zera. Wtedy po prostu pomijamy ten odczyt.
        pobieranie = max(0.0, pobieranie)
        wysylanie = max(0.0, wysylanie)

        self.szczyt_pobierania = max(self.szczyt_pobierania, pobieranie)
        self.szczyt_wysylania = max(self.szczyt_wysylania, wysylanie)
        return pobieranie, wysylanie


def predkosc_lacza():
    """
    Prędkość aktywnej karty sieciowej w Mb/s albo None.

    UWAGA na interpretację: to prędkość POŁĄCZENIA z routerem (np. 144 Mb/s
    po Wi-Fi), a nie prędkość internetu od operatora. Internet bywa wolniejszy
    i tego stąd nie odczytamy — do tego trzeba zrobić prawdziwy test prędkości,
    czyli ściągnąć plik i zmierzyć czas.
    """
    najszybsza = None
    for nazwa, stan in psutil.net_if_stats().items():
        if not stan.isup or "loopback" in nazwa.lower() or stan.speed <= 0:
            continue
        if najszybsza is None or stan.speed > najszybsza:
            najszybsza = stan.speed
    return najszybsza


# ---------------------------------------------------------------
# Temperatury
# ---------------------------------------------------------------

def _z_monitora_sprzetu():
    """
    Odczyty z LibreHardwareMonitor albo OpenHardwareMonitor, jeśli działa.

    Te programy instalują sterownik czytający czujniki i wystawiają wyniki
    przez WMI — systemowy sposób, w jaki programy dzielą się danymi
    o komputerze. Korzystamy z pywin32, który i tak jest w projekcie.

    Zwraca: (temperatura_procesora, temperatura_karty) — każda może być None.
    """
    import win32com.client

    for przestrzen in ("LibreHardwareMonitor", "OpenHardwareMonitor"):
        try:
            wmi = win32com.client.GetObject(f"winmgmts:root\\{przestrzen}")
            czujniki = wmi.ExecQuery(
                "SELECT Identifier, Value FROM Sensor WHERE SensorType='Temperature'")
        except Exception:
            continue    # ten program nie działa — próbujemy następnego

        procesor, karta = None, None
        for czujnik in czujniki:
            nazwa = (czujnik.Identifier or "").lower()
            wartosc = float(czujnik.Value)
            # Identyfikatory wyglądają tak: /intelcpu/0/temperature/0
            if "cpu" in nazwa:
                procesor = max(procesor or 0.0, wartosc)
            elif "gpu" in nazwa:
                karta = max(karta or 0.0, wartosc)

        if procesor is not None or karta is not None:
            return procesor, karta

    return None, None


def _uchwyt_nvml():
    """
    Łączy się z biblioteką NVIDII (raz na cały program) i zwraca (nvml, karta).

    Korzystają z tego dwa wątki: odczyty w rogu HUD-a i narzędzie system_status
    agenta. Blokada pilnuje, żeby łączenie nie odbyło się dwa razy naraz.

    Zwraca: (moduł pynvml, uchwyt karty) albo (None, None), gdy karty nie ma.
    """
    global _nvml, _karta, _nvml_sprawdzone

    with _blokada_nvml:
        if not _nvml_sprawdzone:
            _nvml_sprawdzone = True
            try:
                import pynvml

                pynvml.nvmlInit()
                _karta = pynvml.nvmlDeviceGetHandleByIndex(0)
                _nvml = pynvml
                logger.info("Czujniki: karta %s", pynvml.nvmlDeviceGetName(_karta))
            except Exception as e:
                logger.info("Czujniki: brak odczytu z karty NVIDIA (%s).", e)

    return _nvml, _karta


def karta_graficzna():
    """
    Pełny odczyt karty NVIDIA: obciążenie, pamięć, temperatura.

    Zwraca: słownik albo None, gdy karty NVIDIA nie ma.
    """
    nvml, karta = _uchwyt_nvml()
    if nvml is None:
        return None

    try:
        uzycie = nvml.nvmlDeviceGetUtilizationRates(karta)
        pamiec = nvml.nvmlDeviceGetMemoryInfo(karta)
        temperatura, limit = _z_nvidii()
        return {
            "nazwa": nvml.nvmlDeviceGetName(karta),
            "obciazenie": uzycie.gpu,                       # % mocy obliczeniowej
            "pamiec_uzyta_mb": pamiec.used / 1024 ** 2,
            "pamiec_calkowita_mb": pamiec.total / 1024 ** 2,
            "temperatura": temperatura,
            "limit": limit,
        }
    except Exception:
        logger.exception("Czujniki: nie udało się odczytać karty graficznej")
        return None


def _z_nvidii():
    """
    Temperatura karty NVIDIA i jej próg bezpieczeństwa, prosto z biblioteki NVML.

    To ta sama biblioteka, z której korzysta nvidia-smi, tylko bez uruchamiania
    osobnego programu: jeden odczyt trwa 0,17 ms zamiast kilkuset.

    Zwraca: (temperatura, limit) albo (None, None).
    """
    nvml, karta = _uchwyt_nvml()
    if nvml is None:
        return None, None

    try:
        temperatura = nvml.nvmlDeviceGetTemperature(karta, nvml.NVML_TEMPERATURE_GPU)
        # "Max Operating Temp" to temperatura, przy której karta zaczyna się
        # bronić. Wyżej są jeszcze progi spowalniania i awaryjnego wyłączenia.
        limit = nvml.nvmlDeviceGetTemperatureThreshold(
            karta, nvml.NVML_TEMPERATURE_THRESHOLD_GPU_MAX)
        return float(temperatura), float(limit)
    except Exception:
        logger.exception("Czujniki: nie udało się odczytać temperatury karty")
        return None, None


# Typowy próg, przy którym procesory zaczynają się spowalniać. Nie da się go
# odczytać bez sterownika, więc przyjmujemy wartość z dokumentacji Intela i AMD
# — służy tylko do narysowania paska, nie do straszenia.
LIMIT_PROCESORA_C = 100.0


def temperatura():
    """
    GŁÓWNE WEJŚCIE — najlepszy dostępny odczyt temperatury.

    Zwraca słownik: {"etykieta": "CPU"/"GPU", "teraz": °C, "limit": °C}
    albo None, gdy nic nie da się odczytać. HUD pomija wtedy ten wiersz.
    """
    procesor, karta = None, None
    try:
        procesor, karta = _z_monitora_sprzetu()
    except Exception:
        logger.debug("Czujniki: monitor sprzętu niedostępny", exc_info=True)

    if procesor is not None:
        return {"etykieta": "CPU", "teraz": procesor, "limit": LIMIT_PROCESORA_C}

    temperatura_karty, limit_karty = _z_nvidii()
    if temperatura_karty is not None:
        return {"etykieta": "GPU", "teraz": temperatura_karty,
                "limit": limit_karty or 90.0}

    if karta is not None:
        return {"etykieta": "GPU", "teraz": karta, "limit": 90.0}

    return None


# --- Test: `python czujniki.py` — pięć odczytów co sekundę ---
if __name__ == "__main__":
    import time

    from logging_setup import skonfiguruj_logowanie

    skonfiguruj_logowanie()

    print(f"Prędkość łącza: {predkosc_lacza()} Mb/s")
    pomiar = PomiarSieci()

    for _ in range(5):
        pobieranie, wysylanie = pomiar.odczyt()
        temp = temperatura()
        opis_temp = (f"{temp['etykieta']} {temp['teraz']:.0f} °C "
                     f"(limit {temp['limit']:.0f})") if temp else "brak odczytu"
        print(f"  ↓ {pobieranie:6.2f} Mb/s   ↑ {wysylanie:6.2f} Mb/s   {opis_temp}")
        time.sleep(1)

    print(f"Szczyt: ↓ {pomiar.szczyt_pobierania:.2f} / "
          f"↑ {pomiar.szczyt_wysylania:.2f} Mb/s")
