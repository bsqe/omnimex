Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
repoDir = fso.GetParentFolderName(WScript.ScriptFullName)
trayExe = fso.BuildPath(repoDir, ".venv\Scripts\omnimex-tray.exe")
pywExe = fso.BuildPath(repoDir, ".venv\Scripts\pythonw.exe")
cfgPath = fso.BuildPath(repoDir, "omnimex.toml")

shell.CurrentDirectory = repoDir
Set env = shell.Environment("PROCESS")
If fso.FileExists(cfgPath) Then
    env("OMNIMEX_CONFIG") = cfgPath
End If

If fso.FileExists(trayExe) Then
    shell.Run Chr(34) & trayExe & Chr(34), 0, False
ElseIf fso.FileExists(pywExe) Then
    shell.Run Chr(34) & pywExe & Chr(34) & " -m omnimex.tray", 0, False
Else
    MsgBox "Omnimex virtual environment not found under " & repoDir & "\.venv", 16, "Omnimex Launcher"
End If
