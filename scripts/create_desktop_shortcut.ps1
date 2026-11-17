param(
    [string]$ShortcutPath = (Join-Path ([Environment]::GetFolderPath("Desktop")) "Omnimex Hub.lnk")
)

$ErrorActionPreference = "Stop"
$RepoDir = Split-Path -Parent $PSScriptRoot
$VbsPath = Join-Path $RepoDir "Launch Omnimex.vbs"
$IcoPath = Join-Path $RepoDir "data\omnimex.ico"

if (-not (Test-Path $IcoPath)) {
    & (Join-Path $RepoDir ".venv\Scripts\python.exe") -c "from pathlib import Path; from omnimex.tray import ensure_tray_ico; ensure_tray_ico(Path(r'$IcoPath'))"
}

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = "wscript.exe"
$Shortcut.Arguments = "`"$VbsPath`""
$Shortcut.WorkingDirectory = $RepoDir
$Shortcut.Description = "Omnimex Cross-Agent Memory & Session Hub (Windowless Tray + App Mode)"
if (Test-Path $IcoPath) {
    $Shortcut.IconLocation = $IcoPath
}
$Shortcut.Save()
Write-Output "Created shortcut: $ShortcutPath"
