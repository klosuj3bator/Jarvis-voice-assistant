; jarvis.iss — skrypt Inno Setup tworzący JarvisSetup.exe.
;
; Nie kompiluj go ręcznie: `python instalator/zbuduj.py` najpierw buduje
; paczkę PyInstallerem (dist\Jarvis), sprawdza ją i dopiero wtedy woła
; kompilator Inno Setup z numerem wersji z wersja.py.
;
; CO ROBI INSTALATOR
;   - kopiuje folder dist\Jarvis do Programów użytkownika
;     (%LOCALAPPDATA%\Programs\Jarvis) — BEZ pytania o uprawnienia
;     administratora; kto chce, może wybrać instalację dla wszystkich,
;   - wpis w menu Start (Jarvis + Odinstaluj Jarvisa),
;   - skrót na pulpicie (zaznaczony domyślnie, można odznaczyć),
;   - autostart z Windowsem (odznaczony domyślnie) — wpis w rejestrze
;     HKCU\...\Run, czyli tylko dla Ciebie, bez usług i Harmonogramu zadań,
;   - deinstalator (Ustawienia -> Aplikacje albo menu Start).
;
; CZEGO NIE ROBI
;   Modeli mowy tu nie ma — pobiera je kreator przy pierwszym uruchomieniu
;   do %APPDATA%\Jarvis\modele. Deinstalator pyta, czy usunąć %APPDATA%\Jarvis
;   (klucze, pamięć, notatki, modele); domyślnie NIE — przy ponownej instalacji
;   Jarvis pamięta wtedy wszystko.

#ifndef Wersja
  #define Wersja "1.0.0"
#endif
#define Nazwa "Jarvis"
#define Plik "Jarvis.exe"

[Setup]
; Stały identyfikator — po nim instalator rozpoznaje wcześniejszą wersję
; przy aktualizacji. Nie zmieniaj go.
AppId={{F3C6FCE4-58BC-4697-A04D-482B01352EE6}
AppName={#Nazwa}
AppVersion={#Wersja}
AppVerName={#Nazwa} {#Wersja}
AppPublisher=Maciek
AppPublisherURL=https://github.com/klosuj3bator
DefaultDirName={autopf}\{#Nazwa}
DefaultGroupName={#Nazwa}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
OutputDir=..\dist
OutputBaseFilename=JarvisSetup
SetupIconFile=..\build\jarvis.ico
UninstallDisplayIcon={app}\{#Plik}
UninstallDisplayName={#Nazwa} — asystent głosowy
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; Działający Jarvis blokuje swoje pliki — instalator poprosi o jego zamknięcie.
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "polski"; MessagesFile: "compiler:Languages\Polish.isl"

[Tasks]
Name: "pulpit"; Description: "Skrót na pulpicie"; GroupDescription: "Skróty:"
Name: "autostart"; Description: "Uruchamiaj Jarvisa razem z Windowsem"; GroupDescription: "Uruchamianie:"; Flags: unchecked

[Files]
Source: "..\dist\Jarvis\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#Nazwa}"; Filename: "{app}\{#Plik}"
Name: "{group}\Odinstaluj Jarvisa"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#Nazwa}"; Filename: "{app}\{#Plik}"; Tasks: pulpit

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#Nazwa}"; ValueData: """{app}\{#Plik}"""; Tasks: autostart; Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#Plik}"; Description: "Uruchom Jarvisa"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Jarvis działa w tle (ikona przy zegarku) — zamykamy go, zanim usuniemy pliki.
Filename: "{sys}\taskkill.exe"; Parameters: "/IM {#Plik} /F"; Flags: runhidden; RunOnceId: "ZamknijJarvisa"

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Dane: String;
begin
  if CurUninstallStep <> usPostUninstall then
    Exit;
  Dane := ExpandConstant('{userappdata}\Jarvis');
  if not DirExists(Dane) or UninstallSilent then
    Exit;
  if MsgBox('Usunąć także Twoje dane Jarvisa?' + #13#10#13#10 +
            'To klucze (Anthropic, Spotify, Telegram...), pamięć, notatki, przypomnienia ' +
            'i pobrane modele mowy (ok. 560 MB) z folderu:' + #13#10 + Dane + #13#10#13#10 +
            'Wybierz „Nie”, jeśli zamierzasz zainstalować Jarvisa ponownie.',
            mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
    DelTree(Dane, True, True, True);
end;
