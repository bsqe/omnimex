<#
.SYNOPSIS
    Reproducible end-to-end Windows packaging pipeline for Omnimex.

.DESCRIPTION
    1. Builds frontend/dist (unless -SkipFrontendBuild is specified).
    2. Ensures data/omnimex.ico and verifies data/fastembed_cache exist.
    3. Runs PyInstaller against packaging/omnimex.spec -> dist/Omnimex/.
    4. Runs Inno Setup 6 (ISCC.exe) against packaging/omnimex.iss -> dist/installer/.
    5. Runs an isolated temporary-directory smoke check for omnimex-cli.exe.
#>

[CmdletBinding()]
param(
    [switch]$SkipFrontendBuild,
    [switch]$SkipInstaller,
    [switch]$SkipSmokeTest
)

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VenvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $VenvPython)) {
    throw "Virtual environment Python not found at $VenvPython"
}

Push-Location $RepoRoot
try {
    $Version = (& $VenvPython -c "import omnimex; print(omnimex.__version__)").Trim()
    if (-not $Version) {
        $Version = "0.1.0"
    }
    Write-Host "=== Omnimex Packaging Pipeline v$Version ===" -ForegroundColor Cyan

    if (-not $SkipFrontendBuild) {
        Write-Host "`n[1/5] Building Vite/React frontend..." -ForegroundColor Yellow
        npm --prefix frontend run build
        if ($LASTEXITCODE -ne 0) {
            throw "Frontend build failed with exit code $LASTEXITCODE"
        }
    }
    else {
        Write-Host "`n[1/5] Skipping frontend build (-SkipFrontendBuild)." -ForegroundColor DarkGray
    }

    if (-not (Test-Path (Join-Path $RepoRoot "frontend\dist\index.html"))) {
        throw "frontend\dist\index.html is missing."
    }

    Write-Host "`n[2/5] Preparing tray/setup icon and verifying ONNX cache..." -ForegroundColor Yellow
    & $VenvPython -c "from pathlib import Path; from omnimex.tray import ensure_tray_ico; ensure_tray_ico(Path('data/omnimex.ico'))"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to generate data/omnimex.ico"
    }
    if (-not (Test-Path (Join-Path $RepoRoot "data\fastembed_cache"))) {
        throw "data\fastembed_cache is missing. Pre-download BAAI/bge-small-en-v1.5 before building."
    }

    $BundleDir = Join-Path $RepoRoot "dist\Omnimex"
    Get-Process -Name "Omnimex", "omnimex-cli" -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -and $_.Path.StartsWith($BundleDir, [System.StringComparison]::OrdinalIgnoreCase) } |
        ForEach-Object {
            Write-Host "Stopping running process $($_.ProcessName) (PID $($_.Id)) in $BundleDir..." -ForegroundColor DarkYellow
            Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        }

    Write-Host "`n[3/5] Running PyInstaller (dual-EXE --onedir bundle)..." -ForegroundColor Yellow
    & $VenvPython -m PyInstaller packaging\omnimex.spec --noconfirm --clean
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }

    $BundleDir = Join-Path $RepoRoot "dist\Omnimex"
    $TrayExe = Join-Path $BundleDir "Omnimex.exe"
    $CliExe = Join-Path $BundleDir "omnimex-cli.exe"
    if (-not (Test-Path $TrayExe) -or -not (Test-Path $CliExe)) {
        throw "Expected executables missing in $BundleDir"
    }

    if (-not $SkipInstaller) {
        Write-Host "`n[4/5] Compiling Inno Setup 6 installer..." -ForegroundColor Yellow
        $IsccCandidates = @(
            (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
            (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
            (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
        )
        $IsccPath = $IsccCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
        if (-not $IsccPath) {
            $IsccCmd = Get-Command "ISCC.exe" -ErrorAction SilentlyContinue
            if ($IsccCmd) {
                $IsccPath = $IsccCmd.Source
            }
        }
        if (-not $IsccPath) {
            throw "Inno Setup 6 ISCC.exe not found. Re-run with -SkipInstaller or install JRSoftware.InnoSetup."
        }

        & $IsccPath "/DAppVersion=$Version" packaging\omnimex.iss
        if ($LASTEXITCODE -ne 0) {
            throw "Inno Setup ISCC.exe failed with exit code $LASTEXITCODE"
        }
    }
    else {
        Write-Host "`n[4/5] Skipping Inno Setup installer (-SkipInstaller)." -ForegroundColor DarkGray
    }

    if (-not $SkipSmokeTest) {
        Write-Host "`n[5/5] Running isolated temporary-directory CLI sanity check..." -ForegroundColor Yellow
        $TempStage = Join-Path ([System.IO.Path]::GetTempPath()) ("omnimex-stage-" + [guid]::NewGuid().ToString("N"))
        $TempHome = Join-Path $TempStage "omnimex-home"
        $StageBundle = Join-Path $TempStage "Omnimex"
        try {
            Copy-Item -Path $BundleDir -Destination $StageBundle -Recurse
            $StagedCli = Join-Path $StageBundle "omnimex-cli.exe"
            $env:OMNIMEX_HOME = $TempHome
            & $StagedCli --help | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw "Staged omnimex-cli.exe --help exited with $LASTEXITCODE"
            }
            $StatusJson = & $StagedCli status --json
            if ($LASTEXITCODE -ne 0) {
                throw "Staged omnimex-cli.exe status --json exited with $LASTEXITCODE"
            }
            Write-Host ($StatusJson | Out-String).Trim()
        }
        finally {
            Remove-Item Env:OMNIMEX_HOME -ErrorAction SilentlyContinue
            Remove-Item -Path $TempStage -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    else {
        Write-Host "`n[5/5] Skipping smoke test (-SkipSmokeTest)." -ForegroundColor DarkGray
    }

    Write-Host "`nPackaging complete!" -ForegroundColor Green
    Write-Host "  Portable folder : dist\Omnimex\"
    Write-Host "  Tray GUI EXE    : dist\Omnimex\Omnimex.exe"
    Write-Host "  CLI / MCP EXE   : dist\Omnimex\omnimex-cli.exe"
    if (-not $SkipInstaller) {
        Write-Host "  Setup Installer : dist\installer\Omnimex-Setup-$Version.exe"
    }
}
finally {
    Pop-Location
}
