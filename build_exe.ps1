# PowerShell-Build-Skript für die direkt startbare Windows-Desktop-Version (.exe)
# Lädt fehlende externe Tools/Modelle automatisch nach .\bin\ herunter und baut
# anschließend .\dist\AV1-VMAF-Studio\AV1-VMAF-Studio.exe via PyInstaller.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$BinDir = Join-Path $Root "bin"
$ModelDir = Join-Path $BinDir "model"
$TempDir = Join-Path $Root "build_temp"
New-Item -ItemType Directory -Force -Path $BinDir, $ModelDir, $TempDir | Out-Null

Write-Host "=== 1. Prüfe & lade externe Tools nach .\bin\ ===" -ForegroundColor Cyan

# 1a) FFmpeg & FFprobe (BtbN GPL Build inkl. libvmaf, libsvtav1, libx265, NVENC, QSV, AMF)
if (-not (Test-Path (Join-Path $BinDir "ffmpeg.exe"))) {
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
    if (-not (Test-Path $TargetModel)) {
        Write-Host "Lade VMAF-Modell $m ..."
        try {
            Invoke-WebRequest -Uri "https://raw.githubusercontent.com/Netflix/vmaf/master/model/$m" -OutFile $TargetModel
        } catch {
            Write-Warning "Modell $m nicht gefunden (optional): $_"
        }
    }
}

# 1c) MKVToolNix (mkvpropedit.exe & mkvmerge.exe für MKV-Statistik-Tags)
if (-not (Test-Path (Join-Path $BinDir "mkvpropedit.exe"))) {
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
if (-not (Test-Path (Join-Path $BinDir "dovi_tool.exe"))) {
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
if (-not (Test-Path (Join-Path $BinDir "hdr10plus_tool.exe"))) {
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

Write-Host "=== 2. Prüfe Python-Pakete (pyinstaller, pywebview) ===" -ForegroundColor Cyan
$PyCmd = if (Get-Command "python" -ErrorAction SilentlyContinue) { "python" } else { "py" }
& $PyCmd -m pip install --upgrade pip
& $PyCmd -m pip install --upgrade -r requirements.txt pyinstaller
try {
    & $PyCmd -m pip install --upgrade pywebview
} catch {
    Write-Warning "pywebview konnte nicht installiert werden – Edge/Chrome App-Modus wird genutzt."
}

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
    desktop_main.py
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller ist mit Exit-Code $LASTEXITCODE fehlgeschlagen."
}

$DistAppDir = Join-Path $Root "dist\AV1-VMAF-Studio"
$DistBinDir = Join-Path $DistAppDir "bin"
New-Item -ItemType Directory -Force -Path $DistBinDir | Out-Null
Copy-Item -Path (Join-Path $BinDir "*") -Destination $DistBinDir -Recurse -Force

Write-Host ""
Write-Host "Fertig! Die direkt startbare Anwendung liegt unter:" -ForegroundColor Green
Write-Host "  $DistAppDir\AV1-VMAF-Studio.exe" -ForegroundColor Yellow
Write-Host "Externe Tools & Modelle liegen unter:" -ForegroundColor Green
Write-Host "  $DistBinDir" -ForegroundColor Yellow
