' Start the FINDER service with no console window, ever.
'
' Plain "python.exe finder_service.py" is a console program, so Windows 11 opens a
' Windows Terminal window for it and leaves that black window sitting on the desktop
' for as long as the service runs. pythonw.exe is a GUI-subsystem binary: it never
' allocates a console at all, and windowStyle 0 keeps the launcher itself invisible.
' The service's output goes to finder_service.log next to the index instead.
'
' Run:  wscript.exe D:\Youtube\Beacon\start_finder_hidden.vbs
' Stop: taskkill /F /IM pythonw.exe  (or kill just the finder_service PID)
Option Explicit
Dim fso, sh, root, py
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
' Use only Beacon's own interpreter; never search another application's environment.
py = root & "\.venv\Scripts\python.exe"
If Not fso.FileExists(py) Then WScript.Quit 2
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = root      ' finder_service.py imports engine_link from this folder
sh.Run """" & py & """ """ & root & "\finder_service.py""", 0, False
