"""
Jarvis.pyw — uruchamia Jarvisa BEZ okna konsoli. Kliknij dwukrotnie.

Na czym polega sztuczka: rozszerzenie .pyw Windows otwiera programem
pythonw.exe zamiast python.exe. To ten sam Python, tylko bez przypisanego
okna konsoli — więc w tle nie wisi czarne okienko cmd.

Cała logika siedzi w main.py; ten plik tylko go uruchamia. Wszystko,
co Jarvis ma do powiedzenia, trafia do jarvis.log obok programu.

Chcesz widzieć dziennik na żywo (np. przy szukaniu błędu)? Uruchom wtedy
zwykłe `python main.py` w terminalu — to celowo pokazuje konsolę.
"""

import os
import sys

# Przy dwukliku katalog roboczy bywa inny niż folder programu, a Jarvis
# szuka .env, pamięci i konfiguracji obok siebie.
os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

import main  # noqa: E402

sys.exit(main.main())
