"""
zbuduj.py — buduje Jarvisa do dystrybucji: `python instalator/zbuduj.py`.

Kolejne kroki (każdy musi się udać, zanim ruszy następny):

  1. IKONA      — build/jarvis.ico w stylu HUD-a (dla Jarvis.exe i instalatora)
  2. PAKOWANIE  — PyInstaller w trybie folderu -> dist/Jarvis/ (instalator/jarvis.spec)
  3. PRYWATNOŚĆ — czy w paczce nie ma Twoich plików ani kluczy (sprawdz_paczke.py)
  4. AUTOTEST   — Jarvis.exe --autotest: czy paczka naprawdę działa (autotest.py)
  5. INSTALATOR — Inno Setup -> dist/JarvisSetup.exe (jarvis.iss), jeśli jest zainstalowany

Opcje:
  --bez-instalatora   pomiń krok 5
  --bez-autotestu     pomiń krok 4 (np. na komputerze bez karty dźwiękowej)
  --tylko-instalator  zmieniłeś tylko jarvis.iss — użyj gotowej paczki z dist/Jarvis
                      (kroki 3 i 5; sprawdzenie prywatności zawsze się wykonuje)
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

TU = os.path.dirname(os.path.abspath(__file__))
KOD = os.path.abspath(os.path.join(TU, ".."))
BUILD = os.path.join(KOD, "build")
DIST = os.path.join(KOD, "dist")
PACZKA = os.path.join(DIST, "Jarvis")

sys.path.insert(0, KOD)
sys.path.insert(0, TU)


def krok(numer, tytul):
    print(f"\n[{numer}/5] {tytul}", flush=True)


def ikona():
    """HUD-owy "reaktor" (jak w kreatorze) w rozmiarach, których używa Windows."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PIL import Image
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen, QRadialGradient

    app = QGuiApplication.instance() or QGuiApplication(sys.argv)  # noqa: F841 — Qt tego wymaga
    rozmiar = 256
    obraz = QImage(rozmiar, rozmiar, QImage.Format_ARGB32)
    obraz.fill(Qt.transparent)
    p = QPainter(obraz)
    p.setRenderHint(QPainter.Antialiasing)
    tlo = QRadialGradient(128, 128, 128)
    tlo.setColorAt(0.0, QColor(11, 36, 51))
    tlo.setColorAt(1.0, QColor(2, 6, 10))
    p.setBrush(tlo)
    p.setPen(Qt.NoPen)
    p.drawEllipse(QRectF(4, 4, 248, 248))
    for kolor, grubosc, margines, start, dlugosc in (
            ((58, 168, 222), 6, 18, 0, 360), ((86, 214, 255), 16, 40, 30, 250),
            ((255, 186, 64), 16, 40, 300, 50), ((86, 214, 255), 7, 70, 200, 280)):
        pioro = QPen(QColor(*kolor))
        pioro.setWidthF(grubosc)
        pioro.setCapStyle(Qt.FlatCap)
        p.setPen(pioro)
        p.drawArc(QRectF(margines, margines, rozmiar - 2 * margines, rozmiar - 2 * margines),
                  start * 16, dlugosc * 16)
    srodek = QRadialGradient(128, 128, 30)
    srodek.setColorAt(0.0, QColor(220, 242, 255))
    srodek.setColorAt(1.0, QColor(86, 214, 255, 0))
    p.setBrush(srodek)
    p.setPen(Qt.NoPen)
    p.drawEllipse(QRectF(98, 98, 60, 60))
    p.end()

    os.makedirs(BUILD, exist_ok=True)
    png = os.path.join(BUILD, "jarvis_256.png")
    obraz.save(png)
    Image.open(png).save(os.path.join(BUILD, "jarvis.ico"),
                         sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    print("  build/jarvis.ico")


def pakowanie():
    if os.path.exists(PACZKA):
        shutil.rmtree(PACZKA)
    start = time.monotonic()
    wynik = subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                            "--distpath", DIST, "--workpath", os.path.join(BUILD, "pyinstaller"),
                            os.path.join(TU, "jarvis.spec")], cwd=KOD)
    if wynik.returncode:
        raise SystemExit("PyInstaller zgłosił błąd — szczegóły wyżej.")
    rozmiar = sum(os.path.getsize(os.path.join(k, p)) for k, _, pl in os.walk(PACZKA) for p in pl)
    print(f"  dist/Jarvis — {rozmiar / 1e6:.0f} MB, {time.monotonic() - start:.0f} s")


def prywatnosc():
    import sprawdz_paczke

    problemy, ile = sprawdz_paczke.sprawdz(PACZKA)
    print(f"  szukanych prywatnych wartości: {ile}")
    if problemy:
        for problem in problemy:
            print("  PROBLEM:", problem)
        raise SystemExit("W paczce są prywatne pliki albo klucze — nie buduję instalatora.")
    print("  paczka czysta")


def autotest():
    """Paczka uruchomiona "na czysto": pusty, tymczasowy %APPDATA% — jak u nowej osoby."""
    appdata = tempfile.mkdtemp(prefix="jarvis_autotest_")
    wynik_plik = os.path.join(BUILD, "autotest.json")
    srodowisko = {k: v for k, v in os.environ.items()
                  if not k.startswith(("ANTHROPIC", "SPOTIPY", "TELEGRAM", "GMAIL", "GOOGLE_CAL"))}
    srodowisko["APPDATA"] = appdata
    try:
        proces = subprocess.run([os.path.join(PACZKA, "Jarvis.exe"), f"--autotest={wynik_plik}"],
                                env=srodowisko, timeout=300)
        with open(wynik_plik, encoding="utf-8") as f:
            wyniki = json.load(f)
    except (subprocess.TimeoutExpired, OSError, ValueError) as e:
        raise SystemExit(f"Autotest nie zadziałał: {type(e).__name__}") from None
    finally:
        shutil.rmtree(appdata, ignore_errors=True)
    for nazwa, w in wyniki.items():
        print(f"  {'OK  ' if w['ok'] else 'BŁĄD'} {nazwa}: {w['opis']}")
    if proces.returncode or not all(w["ok"] for w in wyniki.values()):
        raise SystemExit("Autotest wykrył problem z paczką — szczegóły w build/autotest.json.")


def iscc():
    """Kompilator Inno Setup — w typowych miejscach albo w PATH."""
    for sciezka in (os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Inno Setup 6", "ISCC.exe"),
                    os.path.join(os.environ.get("ProgramFiles", ""), "Inno Setup 6", "ISCC.exe"),
                    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Inno Setup 6",
                                 "ISCC.exe")):
        if os.path.exists(sciezka):
            return sciezka
    return shutil.which("iscc")


def instalator():
    from wersja import WERSJA

    kompilator = iscc()
    if not kompilator:
        print("  Nie znalazłem Inno Setup 6 — instalator nie powstał (paczka w dist/Jarvis jest gotowa).\n"
              "  Zainstaluj go: winget install JRSoftware.InnoSetup  i uruchom to jeszcze raz.")
        return
    wynik = subprocess.run([kompilator, f"/DWersja={WERSJA}", os.path.join(TU, "jarvis.iss")])
    if wynik.returncode:
        raise SystemExit("Inno Setup zgłosił błąd — szczegóły wyżej.")
    plik = os.path.join(DIST, "JarvisSetup.exe")
    print(f"  {plik} — {os.path.getsize(plik) / 1e6:.0f} MB (wersja {WERSJA})")


if __name__ == "__main__":
    tylko_instalator = "--tylko-instalator" in sys.argv
    if tylko_instalator and not os.path.exists(os.path.join(PACZKA, "Jarvis.exe")):
        raise SystemExit("Nie ma gotowej paczki w dist/Jarvis — uruchom bez --tylko-instalator.")

    krok(1, "Ikona")
    if tylko_instalator and os.path.exists(os.path.join(BUILD, "jarvis.ico")):
        print("  pominięta (jest z poprzedniego budowania)")
    else:
        ikona()
    krok(2, "Pakowanie (PyInstaller, tryb folderu)")
    if tylko_instalator:
        print("  pominięte — używam gotowej paczki z dist/Jarvis")
    else:
        pakowanie()
    krok(3, "Prywatność paczki")
    prywatnosc()
    krok(4, "Autotest paczki")
    if "--bez-autotestu" in sys.argv or tylko_instalator:
        print("  pominięty")
    else:
        autotest()
    krok(5, "Instalator (Inno Setup)")
    if "--bez-instalatora" in sys.argv:
        print("  pominięty")
    else:
        instalator()
    print("\nGotowe.")
