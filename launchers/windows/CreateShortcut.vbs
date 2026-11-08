' ============================================================================
'   Update Manager  --  DEDICATED TO: WINDOWS
'   Creates a Desktop shortcut to the SILENT launcher (UpdateManager.vbs),
'   using Favicon.ico as its icon. Double-click this once to create it.
' ============================================================================
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

here   = fso.GetParentFolderName(WScript.ScriptFullName)            ' launchers\windows
appDir = fso.GetParentFolderName(fso.GetParentFolderName(here))     ' UpdateManager

target  = appDir & "\launchers\windows\UpdateManager.vbs"
iconPath = appDir & "\img\Favicon.ico"
linkPath = sh.SpecialFolders("Desktop") & "\Update Manager.lnk"

Set lnk = sh.CreateShortcut(linkPath)
lnk.TargetPath       = target
lnk.WorkingDirectory = appDir
lnk.Description      = "Update Manager"
lnk.WindowStyle      = 1
If fso.FileExists(iconPath) Then lnk.IconLocation = iconPath
lnk.Save

WScript.Echo "Shortcut created: " & linkPath
