# -*- mode: python ; coding: utf-8 -*-
#
# jarvis.spec — przepis PyInstallera na Jarvisa. Nie uruchamiaj go ręcznie:
# robi to instalator/zbuduj.py, który przed nim generuje ikonę, a po nim
# sprawdza paczkę i uruchamia autotest.
#
#
# TRYB FOLDERU (onedir), NIE JEDEN PLIK
# =====================================
# Tryb jednego pliku przy KAŻDYM starcie rozpakowuje całą paczkę (kilkaset MB,
# z Qt i silnikiem Whispera) do folderu tymczasowego — Jarvis startowałby
# kilkadziesiąt sekund dłużej, a antywirusy lubią takie samorozpakowujące się
# pliki. W trybie folderu wszystko leży gotowe; instalator i tak dostarcza
# cały folder.
#
#
# CO TRAFIA DO PACZKI — WYŁĄCZNIE TO, CO WYMIENIONE
# =================================================
# Kod Jarvisa PyInstaller znajduje sam, idąc po importach od Jarvis.pyw.
# Plików danych NIE zbieramy z folderu projektu hurtem — tylko te trzy
# z listy poniżej. Dzięki temu Twój .env, pamięć, notatki, tokeny i dziennik
# nie mają jak się tu dostać. Sprawdza to jeszcze instalator/sprawdz_paczke.py.
#
# Modeli Whispera i openWakeWord też tu nie ma — pobiera je kreator
# przy pierwszym uruchomieniu (modele.py).

import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

KOD = os.path.abspath(os.path.join(SPECPATH, ".."))

# Części programu, których nie da się wyczytać z importów.
datas = [(os.path.join(KOD, plik), ".") for plik in ("hud.qml", "hud.frag.qsb", "jarvis_icon.png")]
# Mały model wykrywania ciszy, który jest częścią BIBLIOTEKI faster-whisper
# (vad_filter) — to nie jest model rozpoznawania mowy.
datas += collect_data_files("faster_whisper")
# Strefy czasowe (Europe/Warsaw) — Windows nie ma ich w systemie.
datas += collect_data_files("tzdata")

# Silnik Whispera to biblioteki DLL, których PyInstaller sam nie widzi.
binaries = collect_dynamic_libs("ctranslate2")

# Tego Jarvis nie używa, a potrafiłoby wciągnąć setki MB:
#   scikit-learn i scipy — openWakeWord potrzebuje ich tylko do TRENOWANIA
#   własnych słów; Jarvis tę część odcina (wake_word_listener._odetnij_scikit_learn)
#   torch i spółka — trening modeli openWakeWord
WYKLUCZONE = [
    "sklearn", "scipy", "torch", "torchaudio", "torch_audiomentations", "torchinfo",
    "torchmetrics", "openwakeword.train", "openwakeword.data",
    "openwakeword.custom_verifier_model", "tkinter", "matplotlib", "IPython",
    "pytest", "chat", "router",
    # pobieranie przez Hugging Face — Jarvis pobiera modele sam (modele.py)
    "hf_xet",
]

a = Analysis(
    [os.path.join(KOD, "Jarvis.pyw")],
    pathex=[KOD],
    binaries=binaries,
    datas=datas,
    hiddenimports=["autotest"],
    excludes=WYKLUCZONE,
)

# NIEUŻYWANE CZĘŚCI Qt
# PyInstaller zbiera wtyczki QML hurtem, a razem z nimi ich zależności — m.in.
# Qt WebEngine, czyli całą przeglądarkę Chromium (194 MB w jednym pliku DLL).
# HUD używa wyłącznie QtQuick i QtQuick.Window, kreator — QtWidgets. Resztę
# wycinamy; autotest (zbuduj.py) sprawdza potem, że HUD i kreator dalej powstają.
# Zmierzone 28.09: paczka 684 MB -> 396 MB.
NIEPOTRZEBNE_QT = (
    "WebEngine", "WebChannel", "WebSockets", "WebView", "Qt6Pdf", "QtPdf", "Quick3D", "Qt3D",
    "Multimedia", "SpatialAudio", "TextToSpeech", "Charts", "DataVisualization", "Graphs",
    "Location", "Positioning", "Sensors", "VirtualKeyboard", "Scxml", "RemoteObjects",
    "Bluetooth", "Nfc", "SerialPort", "QtTest", "Qt6Test", "Designer", "QtHelp", "Qt6Help",
    "Controls", "Dialogs", "Timeline", "LottieVector", "PySide6\\translations",
)


def potrzebne(wpisy):
    return [w for w in wpisy if not any(n.lower() in w[0].lower() for n in NIEPOTRZEBNE_QT)]


a.binaries = potrzebne(a.binaries)
a.datas = potrzebne(a.datas)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Jarvis",
    console=False,                                    # bez czarnego okna konsoli
    icon=os.path.join(KOD, "build", "jarvis.ico"),    # generuje zbuduj.py
    upx=False,                                        # UPX lubi psuć biblioteki Qt
)

coll = COLLECT(exe, a.binaries, a.datas, name="Jarvis", upx=False)
