' uruchom_jarvis.vbs — uruchamia Jarvisa w tle, bez migającego okna konsoli.
'
' Kliknij dwukrotnie ten plik, żeby wystartować asystenta.
' Jarvis pojawi się jako kula na pulpicie i ikona w zasobniku systemowym.
' Wszystko, co program ma do powiedzenia, ląduje w pliku jarvis.log.
'
' Dlaczego .vbs, a nie .bat: plik .bat zawsze na moment otwiera okno konsoli,
' nawet jeśli uruchamia pythonw. WScript.Shell z parametrem 0 startuje proces
' całkowicie niewidocznie.

Set powloka = CreateObject("WScript.Shell")
Set system = CreateObject("Scripting.FileSystemObject")

' Katalog, w którym leży ten skrypt — dzięki temu działa niezależnie od tego,
' skąd go uruchomisz i gdzie przeniesiesz folder projektu.
katalog = system.GetParentFolderName(WScript.ScriptFullName)

powloka.CurrentDirectory = katalog

' pythonw.exe to Python bez okna konsoli (zwykły python.exe zawsze je tworzy).
' Ostatni argument 0 = uruchom ukryte, False = nie czekaj na zakończenie.
powloka.Run "pythonw.exe """ & katalog & "\main.py""", 0, False
