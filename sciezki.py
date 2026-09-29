"""
sciezki.py — gdzie Jarvis trzyma swoje pliki.

Dwa światy, bo Jarvis działa na dwa sposoby:

  Z KODU (python main.py, Jarvis.pyw)   — tak, jak rozwijasz go Ty.
      Wszystko leży obok kodu, jak zawsze: pamięć, notatki, dziennik.
      Nic się tu nie zmienia i nic nie trzeba przenosić.

  Z INSTALATORA (Jarvis.exe w Program Files)
      Folder programu jest tylko do odczytu — Windows nie pozwoli w nim
      zapisywać bez uprawnień administratora. A nawet gdyby pozwolił,
      odinstalowanie albo aktualizacja zmiotłyby Twoją pamięć i notatki.
      Dlatego dane użytkownika idą do %APPDATA%\\Jarvis, a w folderze
      programu zostaje tylko sam program.

Trzy katalogi:
  ZASOBY — pliki, które są częścią programu (hud.qml, shader, ikona).
           Z kodu: folder z kodem. Z instalatora: wnętrze paczki PyInstallera.
  DANE   — wszystko, co Jarvis zapisuje: pamięć, notatki, przypomnienia,
           dziennik, tokeny Spotify. Z kodu: folder z kodem.
           Z instalatora: %APPDATA%\\Jarvis.
  MODELE — modele mowy pobierane przy pierwszym uruchomieniu (modele.py),
           zawsze %APPDATA%\\Jarvis\\modele.

Moduł jest celowo malutki i bez zależności — importuje go nawet
logging_setup.py, zanim cokolwiek innego wystartuje.
"""

import os
import sys

# Czy działamy z paczki PyInstallera. Zmienna JARVIS_JAK_PAKIET=1 udaje to
# przy uruchomieniu z kodu — do testowania ścieżek i pobierania modeli bez
# budowania całego instalatora.
SPAKOWANY = bool(getattr(sys, "frozen", False)) or os.environ.get("JARVIS_JAK_PAKIET") == "1"

KOD = os.path.dirname(os.path.abspath(__file__))
ZASOBY = getattr(sys, "_MEIPASS", KOD)
UZYTKOWNIK = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "Jarvis")
DANE = UZYTKOWNIK if SPAKOWANY else KOD
MODELE = os.path.join(UZYTKOWNIK, "modele")


def dane(*czesci):
    """Ścieżka do pliku z danymi użytkownika (katalog DANE tworzymy w razie potrzeby)."""
    os.makedirs(DANE, exist_ok=True)
    return os.path.join(DANE, *czesci)


def zasob(*czesci):
    """Ścieżka do pliku, który jest częścią programu (tylko do odczytu)."""
    return os.path.join(ZASOBY, *czesci)
