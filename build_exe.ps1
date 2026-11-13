# PowerShell-Build-Skript für die direkt startbare Windows-Desktop-Version (.exe)
# Lädt fehlende externe Tools/Modelle automatisch nach .\bin\ herunter und baut
# anschließend .\dist\AV1-VMAF-Studio\AV1-VMAF-Studio.exe via PyInstaller.

param(
    [string]$Python = "py",
    [switch]$SkipToolDownloads,
    [switch]$BuildInstaller
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

# Isolate frozen builds from global Python.Runtime.dll / package mismatches.
# py defaults to the newest interpreter (possibly unsupported Python 3.14).
$PythonArgs = if ((Split-Path -Leaf $Python) -in @("py", "py.exe")) { @("-3.12") } else { @() }
& $Python @PythonArgs -c "import sys,struct; assert sys.version_info[:2] == (3,12) and struct.calcsize('P') == 8, 'Build requires CPython 3.12 x64'"
if ($LASTEXITCODE -ne 0) { throw "Bitte CPython 3.12 x64 installieren oder -Python mit dessen Pfad angeben." }
$Venv = Join-Path $Root ".venv-desktop"
& $Python @PythonArgs -m venv $Venv
if ($LASTEXITCODE -ne 0) { throw "Desktop-Build-Umgebung konnte nicht angelegt werden." }
$PyCmd = Join-Path $Venv "Scripts\python.exe"
& $PyCmd -m pip install -r (Join-Path $Root "requirements-desktop.txt")
if ($LASTEXITCODE -ne 0) { throw "Desktop-Abhängigkeiten konnten nicht installiert werden." }
& $PyCmd -m pip check
if ($LASTEXITCODE -ne 0) { throw "Desktop-Abhängigkeiten sind inkonsistent." }

$BinDir = Join-Path $Root "bin"
$ModelDir = Join-Path $BinDir "model"
$TempDir = Join-Path $Root "build_temp"
New-Item -ItemType Directory -Force -Path $BinDir, $ModelDir, $TempDir | Out-Null

Write-Host "=== 1. Prüfe & lade externe Tools nach .\bin\ ===" -ForegroundColor Cyan

# 1a) FFmpeg & FFprobe (BtbN GPL Build inkl. libvmaf, libsvtav1, libx265, NVENC, QSV, AMF)
if (-not $SkipToolDownloads -and -not (Test-Path (Join-Path $BinDir "ffmpeg.exe"))) {
    Write-Host "Lade FFmpeg (BtbN GPL x64) herunter ..."
    $FfZip = Join-Path $TempDir "ffmpeg.zip"
    Invoke-WebRequest -Uri "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" -OutFile $FfZip
    Expand-Archive -Path $FfZip -DestinationPath $TempDir -Force
    $ExtractedBin = Get-ChildItem -Path $TempDir -Filter "ffmpeg.exe" -Recurse | Select-Object -First 1
    if ($ExtractedBin) {
        Copy-Item $ExtractedBin.FullName -Destination (Join-Path $BinDir "ffmpeg.exe") -Force
        $ProbePath = Join-Path $ExtractedBin.DirectoryName "ffprobe.exe"
        if (Test-Path $ProbePath) {
            Copy-Item $ProbePath -Destination (Join-Path $BinDir "ffprobe.exe") -Force
        }
    }
}

# 1b) VMAF-Modelle (v0.6.1 + NEG + 4K + v1.0.16) nach .\bin\model\
$VmafModels = @(
    "vmaf_v0.6.1.json",
    "vmaf_v0.6.1neg.json",
    "vmaf_4k_v0.6.1.json",
    "vmaf_4k_v0.6.1neg.json",
    "vmaf_v1.0.16_3d0h.json",
    "vmaf_v1.0.16_1d5h_2160.json"
)
foreach ($m in $VmafModels) {
    $TargetModel = Join-Path $ModelDir $m
    if (-not $SkipToolDownloads -and -not (Test-Path $TargetModel)) {
        Write-Host "Lade VMAF-Modell $m ..."
        try {
            Invoke-WebRequest -Uri "https://raw.githubusercontent.com/Netflix/vmaf/master/model/$m" -OutFile $TargetModel
        } catch {
            Write-Warning "Modell $m nicht gefunden (optional): $_"
        }
    }
}

# 1c) MKVToolNix (mkvpropedit.exe & mkvmerge.exe für MKV-Statistik-Tags)
if (-not $SkipToolDownloads -and -not (Test-Path (Join-Path $BinDir "mkvpropedit.exe"))) {
    Write-Host "Lade mkvpropedit.exe & mkvmerge.exe (MKVToolNix) ..."
    try {
        $Mkv7z = Join-Path $TempDir "mkvtoolnix.7z"
        Invoke-WebRequest -Uri "https://mkvtoolnix.download/windows/releases/88.0/mkvtoolnix-64-bit-88.0.7z" -OutFile $Mkv7z
        if (Get-Command "7z" -ErrorAction SilentlyContinue) {
            & 7z e $Mkv7z "-o$BinDir" "mkvpropedit.exe" "mkvmerge.exe" -r -y | Out-Null
        }
    } catch {
        Write-Warning "MKVToolNix konnte nicht automatisch geladen werden (optional): $_"
    }
}

# 1c) dovi_tool.exe (Dolby Vision RPU Extraktion/Injektion)
if (-not $SkipToolDownloads -and -not (Test-Path (Join-Path $BinDir "dovi_tool.exe"))) {
    Write-Host "Lade dovi_tool.exe ..."
    try {
        $DoviZip = Join-Path $TempDir "dovi_tool.zip"
        Invoke-WebRequest -Uri "https://github.com/quietvoid/dovi_tool/releases/latest/download/dovi_tool-2.1.2-x86_64-pc-windows-msvc.zip" -OutFile $DoviZip
        Expand-Archive -Path $DoviZip -DestinationPath $TempDir -Force
        $DoviExe = Get-ChildItem -Path $TempDir -Filter "dovi_tool.exe" -Recurse | Select-Object -First 1
        if ($DoviExe) { Copy-Item $DoviExe.FullName -Destination (Join-Path $BinDir "dovi_tool.exe") -Force }
    } catch {
        Write-Warning "dovi_tool konnte nicht automatisch geladen werden (optional): $_"
    }
}

# 1d) hdr10plus_tool.exe (HDR10+ Metadaten)
if (-not $SkipToolDownloads -and -not (Test-Path (Join-Path $BinDir "hdr10plus_tool.exe"))) {
    Write-Host "Lade hdr10plus_tool.exe ..."
    try {
        $HdrZip = Join-Path $TempDir "hdr10plus_tool.zip"
        Invoke-WebRequest -Uri "https://github.com/quietvoid/hdr10plus_tool/releases/latest/download/hdr10plus_tool-1.6.1-x86_64-pc-windows-msvc.zip" -OutFile $HdrZip
        Expand-Archive -Path $HdrZip -DestinationPath $TempDir -Force
        $HdrExe = Get-ChildItem -Path $TempDir -Filter "hdr10plus_tool.exe" -Recurse | Select-Object -First 1
        if ($HdrExe) { Copy-Item $HdrExe.FullName -Destination (Join-Path $BinDir "hdr10plus_tool.exe") -Force }
    } catch {
        Write-Warning "hdr10plus_tool konnte nicht automatisch geladen werden (optional): $_"
    }
}

Remove-Item -Recurse -Force $TempDir -ErrorAction SilentlyContinue

# Entferne Mark-of-the-Web (Zone.Identifier), damit .NET Framework Assemblies
# (Python.Runtime.dll, WebView2) nicht mit HRESULT 0x80131515 blockiert werden.
Get-ChildItem -LiteralPath $Root -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue

Write-Host "=== 2. Prüfe gepinnte native Laufzeit ===" -ForegroundColor Cyan
& $PyCmd desktop_main.py --desktop-smoke-test
if ($LASTEXITCODE -ne 0) { throw "Native Desktop-Laufzeitprüfung fehlgeschlagen; siehe LOCALAPPDATA\AV1-VMAF-Studio\studio.log." }

Write-Host "=== 3. Baue AV1-VMAF-Studio.exe mit PyInstaller ===" -ForegroundColor Cyan
& $PyCmd -m PyInstaller `
    --noconfirm `
    --clean `
    --onedir `
    --windowed `
    --name "AV1-VMAF-Studio" `
    --add-data "templates;templates" `
    --add-data "static;static" `
    --collect-submodules "core" `
    --collect-submodules "uvicorn" `
    --collect-all "webview" `
    --collect-all "pythonnet" `
    --collect-all "clr_loader" `
    --collect-all "pystray" `
    --copy-metadata "pywebview" `
    --copy-metadata "pythonnet" `
    --copy-metadata "clr_loader" `
    --hidden-import "clr" `
    --hidden-import "_cffi_backend" `
    --hidden-import "webview.platforms.winforms" `
    --hidden-import "webview.platforms.edgechromium" `
    --hidden-import "pystray._win32" `
    desktop_main.py
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller ist mit Exit-Code $LASTEXITCODE fehlgeschlagen."
}

$DistAppDir = Join-Path $Root "dist\AV1-VMAF-Studio"
$DistBinDir = Join-Path $DistAppDir "bin"
New-Item -ItemType Directory -Force -Path $DistBinDir | Out-Null
Get-ChildItem -LiteralPath $BinDir | Copy-Item -Destination $DistBinDir -Recurse -Force
Get-ChildItem -LiteralPath $DistAppDir -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue

# The smoke test exercises the matching Python.Runtime assembly loader in the
# frozen process, without opening a window or changing file associations.
$FrozenExe = Join-Path $DistAppDir "AV1-VMAF-Studio.exe"
$Smoke = Start-Process -FilePath $FrozenExe -ArgumentList "--desktop-smoke-test" -PassThru
if (-not $Smoke.WaitForExit(60000)) {
    Stop-Process -Id $Smoke.Id -Force
    throw "Frozen-Smoke-Test antwortet nach 60 Sekunden nicht."
}
$Smoke.Refresh()
if ($Smoke.ExitCode -ne 0) {
    throw "Frozen-Smoke-Test fehlgeschlagen; siehe LOCALAPPDATA\AV1-VMAF-Studio\desktop-smoke.json und studio.log."
}

$IssFile = Join-Path $Root "installer.iss"
if ($BuildInstaller -and (Test-Path $IssFile)) {
    $IsccCandidates = @(
        (Get-Command "ISCC.exe" -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
    ) | Where-Object { $_ -and (Test-Path $_) }
    $Iscc = $IsccCandidates | Select-Object -First 1
    if (-not $Iscc) {
        throw "Inno Setup 6 (ISCC.exe) wurde nicht gefunden. Bitte Inno Setup 6 installieren oder ohne -BuildInstaller bauen."
    }
    Write-Host "=== 4. Erstelle Windows-Setup via Inno Setup 6 ===" -ForegroundColor Cyan
    & $Iscc $IssFile
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup ist mit Exit-Code $LASTEXITCODE fehlgeschlagen."
    }
}

Write-Host ""
Write-Host "Fertig! Die direkt startbare Anwendung liegt unter:" -ForegroundColor Green
Write-Host "  $DistAppDir\AV1-VMAF-Studio.exe" -ForegroundColor Yellow
Write-Host "Externe Tools & Modelle liegen unter:" -ForegroundColor Green
Write-Host "  $DistBinDir" -ForegroundColor Yellow
if ($BuildInstaller -and (Test-Path (Join-Path $Root "dist\AV1-VMAF-Studio-Setup.exe"))) {
    Write-Host "Windows-Installer:" -ForegroundColor Green
    Write-Host "  $Root\dist\AV1-VMAF-Studio-Setup.exe" -ForegroundColor Yellow
}
