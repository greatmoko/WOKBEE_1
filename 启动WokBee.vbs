' WokBee one-click launcher (no console, survives closing CMD)
Option Explicit

Dim fso, shell, root, mainPy, pythonw, iconIco, lnkPath, link
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")

root = fso.GetParentFolderName(WScript.ScriptFullName)
mainPy = root & "\main.py"
pythonw = root & "\.venv\Scripts\pythonw.exe"
iconIco = root & "\src\tokbee\resources\icon.ico"
lnkPath = root & "\WokBee.lnk"

If Not fso.FileExists(mainPy) Then
  MsgBox "main.py not found. Place this script in the WokBee project root.", vbCritical, "WokBee"
  WScript.Quit 1
End If

If Not fso.FileExists(pythonw) Then
  pythonw = "pythonw"
End If

' Shortcut carries the bee icon so taskbar/process uses logo, not pythonw
Set link = shell.CreateShortcut(lnkPath)
link.TargetPath = pythonw
link.Arguments = Chr(34) & mainPy & Chr(34)
link.WorkingDirectory = root
link.WindowStyle = 1
If fso.FileExists(iconIco) Then
  link.IconLocation = iconIco & ",0"
End If
link.Description = "WokBee"
link.Save

shell.CurrentDirectory = root
' 1 = normal focus for GUI; False = do not wait
shell.Run Chr(34) & lnkPath & Chr(34), 1, False
