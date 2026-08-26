"""
logging_setup.py — jedno miejsce, w którym konfigurujemy zapis komunikatów.


DLACZEGO logging ZAMIAST print()
================================

print() pisze na standardowe wyjście, czyli do okna konsoli. Dopóki uruchamiasz
Jarvisa przez `python main.py`, wszystko widać. Ale gdy program ma chodzić w tle,
bez konsoli (przez pythonw), to wyjście nie istnieje — komunikaty lecą donikąd.
Gdyby Jarvis wtedy przestał reagować, nie miałbyś ŻADNEJ informacji dlaczego.

logging rozwiązuje cztery rzeczy naraz:

1. ZAPIS DO PLIKU — komunikaty zostają na dysku, w jarvis.log. Możesz je
   przeczytać godzinę później, po tym jak coś się popsuło.

2. POZIOMY WAŻNOŚCI — DEBUG / INFO / WARNING / ERROR / CRITICAL. Możesz zapisywać
   wszystko do pliku, a na konsolę wypuszczać tylko rzeczy istotne. print()
   nie odróżnia "gram piosenkę" od "straciłem połączenie z API".

3. ZNACZNIKI CZASU I ŹRÓDŁO — każda linia mówi, KIEDY i Z KTÓREGO MODUŁU
   przyszła. Przy trzech wątkach działających jednocześnie to różnica między
   dziennikiem, który da się czytać, a kaszą.

4. PEŁNE ŚLADY WYJĄTKÓW — logger.exception() zapisuje cały traceback.
   print(e) pokazuje samą treść błędu, bez informacji, gdzie powstał.

Sposób użycia w pozostałych modułach jest zawsze taki sam:

    import logging
    logger = logging.getLogger(__name__)
    logger.info("coś się stało")

getLogger(__name__) daje logger nazwany jak moduł ("router", "gui"), dzięki
czemu w dzienniku widać pochodzenie każdej linii. Same moduły NIE konfigurują
niczego — robi to raz main.py, wołając skonfiguruj_logowanie() poniżej.
"""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

PLIK_LOGU = os.path.join(os.path.dirname(os.path.abspath(__file__)), "jarvis.log")

# Format jednej linii dziennika:
#   2026-08-24 21:03:11 | INFO     | router          | watek-nasluchu | Rozpoznano: play_song
FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-16s | %(threadName)-14s | %(message)s"
FORMAT_DATY = "%Y-%m-%d %H:%M:%S"

# Po przekroczeniu tego rozmiaru plik jest archiwizowany (jarvis.log.1),
# a zapis leci dalej do nowego. Bez tego dziennik rósłby w nieskończoność.
MAX_ROZMIAR_BAJTY = 1_000_000
LICZBA_ARCHIWOW = 3


def skonfiguruj_logowanie(poziom=logging.INFO):
    """
    Ustawia zapis dziennika. Wołane RAZ, na starcie programu.

    poziom — od jakiej ważności zapisywać (logging.DEBUG zapisze wszystko)

    Podpina dwa miejsca docelowe:
      - plik jarvis.log — zawsze,
      - konsola — tylko jeśli w ogóle istnieje.

    Ten drugi warunek jest tu sednem: uruchomiony przez pythonw (bez konsoli)
    program ma sys.stderr ustawione na None. Próba pisania tam wywaliłaby
    wyjątek, więc handler konsolowy dodajemy tylko wtedy, gdy jest dokąd pisać.
    Dzięki temu ten sam kod działa i w oknie konsoli, i w tle.
    """
    korzen = logging.getLogger()
    korzen.setLevel(poziom)

    # Gdyby ktoś zawołał tę funkcję dwa razy (np. moduł uruchamiany samodzielnie,
    # a potem importowany), każda linia trafiałaby do pliku podwójnie.
    if korzen.handlers:
        return

    formatter = logging.Formatter(FORMAT, datefmt=FORMAT_DATY)

    plik = RotatingFileHandler(
        PLIK_LOGU,
        maxBytes=MAX_ROZMIAR_BAJTY,
        backupCount=LICZBA_ARCHIWOW,
        encoding="utf-8",  # bez tego polskie znaki zamieniłyby się w krzaki
    )
    plik.setFormatter(formatter)
    korzen.addHandler(plik)

    if sys.stderr is not None:
        konsola = logging.StreamHandler(sys.stderr)
        konsola.setFormatter(formatter)
        korzen.addHandler(konsola)

    # Biblioteki potrafią być bardzo gadatliwe na poziomie DEBUG/INFO.
    # Uciszamy je, żeby w dzienniku było widać komunikaty Jarvisa, a nie szum.
    for halasliwy in ("urllib3", "httpx", "httpcore", "anthropic", "spotipy",
                      "faster_whisper", "matplotlib"):
        logging.getLogger(halasliwy).setLevel(logging.WARNING)

    logging.getLogger(__name__).info("--- Dziennik uruchomiony: %s ---", PLIK_LOGU)
