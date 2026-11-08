' ============================================================================
'   Update Manager launcher  --  DEDICATED TO: WINDOWS  (silent, no console)
'   Double-click to start Update Manager without any terminal window flashing.
'   Uses a virtualenv if present: repo-local env\ first, then a shared ..\env.
' ============================================================================
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

here   = fso.GetParentFolderName(WScript.ScriptFullName)                 ' launchers\windows
appDir = fso.GetParentFolderName(fso.GetParentFolderName(here))         ' UpdateManager
parentDir = fso.GetParentFolderName(appDir)

localPyw  = appDir & "\env\Scripts\pythonw.exe"
sharedPyw = parentDir & "\env\Scripts\pythonw.exe"

If fso.FileExists(localPyw) Then
  pyw = """" & localPyw & """"
ElseIf fso.FileExists(sharedPyw) Then
  pyw = """" & sharedPyw & """"
Else
  pyw = "pythonw"
End If

sh.Run pyw & " """ & appDir & "\updatemanager.py""", 0, False
