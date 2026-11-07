' ============================================================================
'   Update Manager launcher  --  DEDICATED TO: WINDOWS  (silent, no console)
'   Double-click to start Update Manager without any terminal window flashing.
' ============================================================================
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

here   = fso.GetParentFolderName(WScript.ScriptFullName)                 ' launchers\windows
appDir = fso.GetParentFolderName(fso.GetParentFolderName(here))         ' UpdateManager
cryptoDir = fso.GetParentFolderName(appDir)

activate = cryptoDir & "\env\Scripts\pythonw.exe"
If fso.FileExists(activate) Then
  sh.Run """" & activate & """ """ & appDir & "\updatemanager.py""", 0, False
Else
  sh.Run "pythonw """ & appDir & "\updatemanager.py""", 0, False
End If
