' Run only the explicitly supplied Beacon interpreter/script, without a console.
' Wait for completion and preserve its exit code for Task Scheduler.
Option Explicit
Dim shell, cmd, i, arg
If WScript.Arguments.Count < 2 Then WScript.Quit 2
Set shell = CreateObject("WScript.Shell")
cmd = ""
For i = 0 To WScript.Arguments.Count - 1
  arg = WScript.Arguments(i)
  If InStr(arg, Chr(34)) > 0 Then WScript.Quit 2
  cmd = cmd & Chr(34) & arg & Chr(34) & " "
Next
WScript.Quit shell.Run(cmd, 0, True)
