# Build QBS for Windows: release\out\QBS-<version>-win64-setup.exe and -win64.zip
#
#   powershell -ExecutionPolicy Bypass -File release\build-windows.ps1
#   $env:PYTHON = "C:\Python312\python.exe"; ...
#
# Needs a Python with PySide6, pyserial and PyInstaller.  Inno Setup 6 is used
# for the installer if it is installed (ISCC.exe on the PATH or in Program
# Files); otherwise only the .zip is produced.
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
# Do not turn a native program's stderr output into a terminating error
$PSNativeCommandUseErrorActionPreference = $false

$Root = Split-Path -Parent $PSScriptRoot
$Out = Join-Path $Root "release\out"
$Python = if ($env:PYTHON) { $env:PYTHON } else { "python" }

$Info = & $Python (Join-Path $Root "release\buildinfo.py") | ConvertFrom-Json
$Version = $Info.display
$Bits = if ([Environment]::Is64BitOperatingSystem) { "64-bit" } else { "32-bit" }

Write-Host "QBS $Version, windows $Bits, $Python"

& $Python -c "import PySide6, serial, PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "missing build dependencies; run:"
    Write-Host "  $Python -m pip install -r $Root\requirements.txt pyinstaller"
    exit 1
}

& $Python (Join-Path $Root "release\make_icon.py")
& $Python -m PyInstaller --clean --noconfirm `
    --distpath (Join-Path $Out "dist") --workpath (Join-Path $Out "build") `
    (Join-Path $Root "release\qrss.spec")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$Dist = Join-Path $Out "dist\QBS"
if (-not (Test-Path (Join-Path $Dist "QBS.exe"))) {
    Write-Error "PyInstaller did not produce $Dist\QBS.exe"
}

$Zip = Join-Path $Out "QBS-$Version-win64.zip"
Remove-Item $Zip -ErrorAction SilentlyContinue
Compress-Archive -Path $Dist -DestinationPath $Zip
Write-Host "built $Zip"

# Inno Setup installer, if available
$Iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
if (-not $Iscc) {
    foreach ($p in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
                     "$env:ProgramFiles\Inno Setup 6\ISCC.exe")) {
        if ($p -and (Test-Path $p)) { $Iscc = $p; break }
    }
}
if ($Iscc) {
    $Exe = if ($Iscc -is [string]) { $Iscc } else { $Iscc.Source }
    & $Exe "/DAppVer=$Version" "/DSourceDir=$Dist" "/DOutDir=$Out" `
            "/DIconFile=$(Join-Path $Out 'icons\qbs.ico')" `
            (Join-Path $Root "release\windows\qbs.iss")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    Write-Host "built $(Join-Path $Out "QBS-$Version-win64-setup.exe")"
} else {
    Write-Host "Inno Setup not found - installer skipped (the .zip runs as is)"
}
