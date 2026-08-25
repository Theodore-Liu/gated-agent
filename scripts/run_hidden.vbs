' run_hidden.vbs - run the given .cmd with a hidden window (no console flash).
' Scheduled tasks that call cmd.exe directly pop a visible console; the
' documented run-hidden pattern is: task action = wscript.exe //B <this file>
' <payload.cmd>. WScript.Shell.Run with window style 0 keeps it invisible.
If WScript.Arguments.Count < 1 Then
  WScript.Quit 2
End If
Dim rc
rc = CreateObject("WScript.Shell").Run("""" & WScript.Arguments(0) & """", 0, True)
WScript.Quit rc
